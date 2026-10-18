"""The real Make-a-video card driven under node against a live server."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from umcares import captions, montage, prepare, projects, textimg, web
from tests.test_prepare import make_conformant, put

FF = prepare.find_tool("ffmpeg")
NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("js_montage_harness.js")
NEED = unittest.skipUnless(FF and prepare.find_tool("ffprobe") and textimg.available() and NODE, "needs ffmpeg, node and macOS text drawing")


@NEED
class Card(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Path(tempfile.mkdtemp())
        for n, sz in (("land.jpg", "640x360"), ("port.jpg", "360x640")):
            subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={sz}", "-frames:v", "1", str(cls.fx / n)], check=True)
        make_conformant(cls.fx / "clip.mp4", dur=3, size="640x360")
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=20", "-c:a", "aac", str(cls.fx / "song.m4a")], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fx, ignore_errors=True)

    def setUp(self):
        self.patch = mock.patch.dict(montage.FORMATS["reels"], width=216, height=384)
        self.patch.start()
        mock.patch.dict(montage.FORMATS["square"], width=216, height=216).start()
        montage._info_cache.clear()
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=False)["path"])
        self.empty = Path(projects.create_project(self.home, "Empty", init_git=False)["path"])
        for i, n in enumerate(("land.jpg", "port.jpg", "clip.mp4")):
            put(self.root, self.fx / n, mtime=1_700_000_000_000 + i * 60_000)
        put(self.root, self.fx / "song.m4a", mtime=1_700_000_500_000)
        sha = next(i["sha"] for i in captions.items(self.root) if i["name"] == "land.jpg")
        captions.set_detail(self.root, sha, "The dean opening the event")
        self.srv = web.make_server(self.home, "me@x.com", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        montage.RENDERER.cancel(self.root)
        montage.RENDERER.wait_idle(30)
        self.srv.shutdown()
        self.srv.server_close()
        mock.patch.stopall()
        shutil.rmtree(self.home, ignore_errors=True)

    def run_js(self, scenario, slug="demo"):
        r = subprocess.run([NODE, str(HARNESS), str(self.port), slug, scenario], capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertNotIn("error", out, out.get("error"))
        return out

    def test_no_pictures_means_no_card(self):
        self.assertFalse(self.run_js("none", "empty")["view"]["open"])

    def test_the_card_offers_formats_looks_music_and_every_picture_in_order(self):
        v = self.run_js("card")["view"]
        self.assertEqual(v["title"], "Make a video")
        self.assertEqual(len(v["formats"]), 3)
        self.assertTrue(any("9:16" in f for f in v["formats"]))
        self.assertEqual(len(v["templates"]), len(montage.TEMPLATES), "every look the server knows is offered")
        self.assertTrue(any(t.startswith("Slow motion") for t in v["templates"]))
        self.assertTrue(any(t.startswith("Paper collage (Vox style)") for t in v["templates"]))
        self.assertEqual(v["music"], ["No music (add one when you post)", "song.m4a"])
        self.assertEqual([r["text"] for r in v["rows"]], [" 🖼 The dean opening the event", " 🖼 port.jpg", " 🎞 clip.mp4"])
        self.assertEqual(v["rows"][0]["sub"], "land.jpg", "the filename sits under a caption")
        self.assertTrue(all(r["checked"] for r in v["rows"]))
        self.assertEqual(v["values"], {"format": "reels", "template": "slowmo", "music": "", "max": "30"})
        for b in ("Make video", "Preview plan", "Save recipe…", "Copy brief for Claude"):
            self.assertIn(b, v["buttons"])

    def test_cut_on_the_beat_is_only_available_with_music_and_reaches_the_server(self):
        out = self.run_js("sync")
        self.assertTrue(out["disabledAtStart"], "nothing to sync to without a song")
        self.assertTrue(out["speechDefault"], "starting clips where people speak is on by default")
        self.assertTrue(out["soundDefault"], "keeping a speaking clip's sound is on by default")
        self.assertFalse(out["tightenDefault"], "cutting dead air changes what is said and when, so it is off until chosen")
        self.assertTrue(out["enabledAfterMusic"])
        self.assertIn("no steady beat", out["withSync"]["plan"], "the option was sent; a plain tone has no beat to cut to")
        self.assertEqual(out["afterRemoval"], {"disabled": True, "checked": False}, "removing the song clears the option")

    def test_the_card_says_which_boxes_take_text_and_which_take_a_number_or_a_pick(self):
        card = self.run_js("card")["view"]["card"]
        self.assertIn("pick a song from this list", card)
        self.assertIn("Add a song", card, "and says where a song comes from")
        self.assertIn("numbers only", card, "the BPM box says it takes a number, because a browser silently drops letters typed into it")

    def test_the_card_offers_a_cut_style_and_it_reaches_the_plan(self):
        card = self.run_js("card")["view"]["card"]
        self.assertIn("Zoom through", card)
        self.assertIn("Plain cuts", card)
        self.assertIn("zoom cuts", self.run_js("zoomcut")["view"]["plan"], "the choice reached the server and came back in the plan")

    def test_the_picture_effects_are_offered_and_reach_the_plan(self):
        card = self.run_js("card")["view"]["card"]
        for label in ("No picture effect", "Dither", "Glitch", "Neon"):
            self.assertIn(label, card)
        self.assertIn("glitch effect", self.run_js("effect")["view"]["plan"], "the choice reached the server and came back in the plan")

    # ---------------------------------------------------------------- cards
    def saved(self, out, name="My reel"):
        return json.loads((self.root / "recipes" / f"{name}.montage.json").read_text())

    def test_every_kind_of_card_can_be_added_and_becomes_the_clip_the_server_expects(self):
        out = self.run_js("allcards")
        clips = self.saved(out)["clips"]
        self.assertEqual(clips, [
            {"quote": "Belajar tanpa henti.", "by": "Dr Madihah"},
            {"announce": {"title": "Hackathon CyberDSA", "date": "12 Okt 2026", "place": "Dewan A"}},
            {"countdown": 4},
            {"checklist": {"title": "Alat yang tepat", "items": ["Task Board", "Version Control", "Focus Timer"]}},
            {"progress": {"label": "Produktiviti", "from": 22, "to": 100, "unit": "%"}},
            {"typing": {"title": "sprint.ts", "text": "const a = 1;\n  ship(a);"}},
            {"list": {"title": "Hari ini", "items": ["Standup 09:00", "Retro 16:00"], "from": "left"}},
            {"flow": {"title": "Dari kod ke pengguna", "steps": ["Kod", "Binaan", "Ujian", "Keluaran"]}},
            {"versus": {"title": "Sebelum dan selepas", "before": {"label": "Cara lama", "items": ["Uji manual", "Ralat manusia"]}, "after": {"label": "Kini", "items": ["Ujian automatik"]}}},
        ])
        plan = montage.plan_for(self.saved(out), self.root)
        self.assertEqual([s["kind"] for s in plan["segments"]], ["quote", "announce", "countdown", "checklist", "progress", "typing", "list", "flow", "versus"],
                         "the server accepts exactly what the page built")

    def test_empty_optional_fields_are_left_out_and_blank_lines_and_spaces_are_cleaned(self):
        clips = self.saved(self.run_js("allcards"))["clips"]
        self.assertNotIn("note", clips[1]["announce"])                                   # an announcement field left empty is not sent
        self.assertEqual(clips[3]["checklist"]["items"], ["Task Board", "Version Control", "Focus Timer"], "trimmed, blank lines dropped")
        self.assertEqual(clips[5]["typing"]["text"], "const a = 1;\n  ship(a);", "the indentation of code is kept; trailing newlines are not")

    def test_cards_move_with_the_arrows_and_can_be_removed_like_pictures_can_be_left_out(self):
        out = self.run_js("cardops")
        clips = self.saved(out)["clips"]
        self.assertEqual([c["quote"] for c in clips], ["Tiga", "Dua"], "Tiga moved above Dua, and the first card (Satu) was removed")

    def test_a_card_survives_making_a_video_which_reloads_the_page_data(self):
        out = self.run_js("cardsurvive")
        self.assertTrue(out["video"], "the draft was made, with the card in it")
        self.assertTrue(any("Quote" in t for t in out["before"]))
        self.assertEqual(out["after"][:len(out["before"])], out["before"], "the first card is still in the list, in its place, after the reload")
        self.assertEqual(len(out["after"]), len(out["before"]) + 1, "and the new one is added after it")

    def test_a_cards_row_says_what_it_shows(self):
        out = self.run_js("allcards")
        card = out["saved"]["card"]
        for says in ("Belajar tanpa henti.", "Hackathon CyberDSA", "Alat yang tepat", "Produktiviti", "sprint.ts", "Hari ini", "Dari kod ke pengguna", "Sebelum dan selepas"):
            self.assertIn(says, card)

    def test_a_saved_recipe_with_cards_loads_back_and_a_clip_without_media_no_longer_breaks_Use(self):
        recipe = {"kind": "montage", "title": "Cards", "format": "reels", "template": "punchy",
                  "clips": [{"quote": "Satu", "by": "Aku"}, {"checklist": {"title": "Alat", "items": ["A", "B"]}}, {"countdown": 3}]}
        (self.root / "recipes").mkdir(exist_ok=True)
        (self.root / "recipes" / "with-cards.montage.json").write_text(json.dumps(recipe))
        out = self.run_js("usecards")
        texts = [r["text"] for r in out["used"]["rows"]]
        self.assertEqual(sum(1 for t in texts if "Quote" in t or "Checklist" in t or "Countdown" in t), 3, texts)
        self.assertTrue(texts[0].strip().endswith("Quote") and "Checklist" in texts[1] and "Countdown" in texts[2], "in the recipe's order, first")
        self.assertIn("checklist card: Alat", out["plan"])
        self.assertEqual(out["editors"], 0, "loaded cards start folded")

    def test_the_join_choices_are_offered_and_reach_the_plan(self):
        card = self.run_js("card")["view"]["card"]
        for label in ("The look decides", "Hard cut", "Slide in", "Circle reveal", "Dip to black"):
            self.assertIn(label, card)
        self.assertIn("slide in joins", self.run_js("join")["view"]["plan"], "the choice reached the server and came back in the plan")

    def test_the_3d_choices_are_offered_and_reach_the_plan(self):
        card = self.run_js("card")["view"]["card"]
        self.assertIn("3D card flip", card)
        self.assertIn("3D cards that sway", card)
        plan = self.run_js("threed")["view"]["plan"]
        self.assertIn("card flips", plan, "the flip reached the server and came back in the plan")
        self.assertIn("tilted cards", plan, "and so did the card look")

    def test_sound_effects_on_cuts_are_offered_and_reach_the_plan(self):
        self.assertIn("Whoosh on every cut", self.run_js("card")["view"]["card"])
        self.assertRegex(self.run_js("sfx")["view"]["plan"], r"\d+ sound effects")

    def test_a_typed_tempo_cuts_to_that_grid_without_audio(self):
        plan = self.run_js("tempo")["view"]["plan"]
        self.assertIn("75 BPM grid", plan)
        self.assertIn("no audio", plan)

    def test_the_player_shows_a_poster_at_once_and_loads_only_the_metadata(self):
        v = self.run_js("make")["view"]
        self.assertTrue(v["video"].endswith(".mp4"))
        self.assertEqual(v["poster"], v["video"].replace(".mp4", ".jpg"))
        self.assertEqual(v["preload"], "metadata")

    def test_deleting_a_finished_video_asks_first_and_then_removes_it_from_the_page_and_the_disk(self):
        out = self.run_js("delete")
        name = out["before"]["links"][0].rsplit("/", 1)[-1]
        self.assertTrue(name.endswith(".mp4"))
        self.assertIn("Delete", out["before"]["buttons"])
        self.assertEqual(len(out["confirms"]), 2)
        self.assertIn(name, out["confirms"][0], "the box says which video")
        self.assertIn("cannot be undone", out["confirms"][0])
        self.assertEqual(out["declined"]["links"], out["before"]["links"], "answering Cancel deletes nothing")
        self.assertTrue(out["declined"]["video"])
        self.assertEqual(out["after"]["links"], [], "the video is gone from the list")
        self.assertIsNone(out["after"]["video"], "and its player is not left pointing at a missing file")
        self.assertFalse((self.root / "exports" / name).exists())
        self.assertFalse((self.root / "exports" / name).with_suffix(".json").exists())
        self.assertIn("Deleted " + name, out["after"]["notice"])

    def test_moving_a_picture_later_changes_the_order_of_the_plan(self):
        out = self.run_js("reorder")
        self.assertEqual(out["before"]["rows"][0]["text"], " 🖼 The dean opening the event")
        self.assertEqual(out["after"]["rows"][1]["text"], " 🖼 The dean opening the event")
        plan = out["preview"]["plan"]
        self.assertLess(plan.index("port.jpg"), plan.index("land.jpg"), "port.jpg now comes first")

    def test_an_unticked_picture_is_left_out(self):
        plan = self.run_js("untick")["view"]["plan"]
        self.assertIn("2 pieces", plan)
        self.assertNotIn("port.jpg", plan)

    def test_the_preview_shows_the_timeline_the_server_worked_out(self):
        v = self.run_js("preview")["view"]
        plan = v["plan"]
        self.assertIn("pieces", plan)
        self.assertIn("hard cuts", plan, "punchy has no crossfade")
        self.assertIn("title card: Hello CyberDSA", plan)
        self.assertIn("The dean opening the event", plan, "the caption shows on its clip")
        self.assertEqual(v["notice"], "")

    def test_making_a_video_shows_progress_then_plays_it_and_lists_it(self):
        out = self.run_js("make")
        v = out["view"]
        during = out["during"]
        self.assertIn("Rendering…", during["buttons"], "Make is replaced while a render is running")
        self.assertNotIn("Make video", during["buttons"])
        self.assertEqual(len([c for c in out["calls"] if c == "POST /api/montage"]), 1, "the second press queued nothing")
        self.assertIn("Make video", v["buttons"], "and it comes back when the render finishes")
        self.assertIsNotNone(v["video"], v["jobs"])
        self.assertRegex(v["video"], r"^/umcares/p/demo/exports/[a-z0-9-]+-reels-\d{8}-\d{6}\.mp4$")
        self.assertIn("✓ done", v["jobs"])
        self.assertTrue(any(l.startswith("/umcares/p/demo/exports/") for l in v["links"]), "the finished video is listed to open or download")
        files = [p.name for p in (self.root / "exports").glob("*.mp4") if ".part" not in p.name]
        self.assertEqual(len(files), 1)
        self.assertIn(files[0], v["video"])

    def test_the_smaller_file_button_makes_a_share_copy_labelled_as_one(self):
        v = self.run_js("share")["view"]
        self.assertIn("Smaller file for sharing", v["buttons"])
        self.assertRegex(v["video"] or "", r"-reels-share-\d{8}-\d{6}\.mp4$")
        self.assertIn("· share copy", v["jobs"], "the job line says what it is")
        self.assertIn("share copy ·", v["jobs"], "and so does the finished-videos list")

    def test_quick_draft_makes_a_half_size_video_labelled_as_a_draft(self):
        v = self.run_js("draft")["view"]
        self.assertIn("Quick draft (half size, fast)", v["buttons"])
        self.assertRegex(v["video"] or "", r"-reels-draft-\d{8}-\d{6}\.mp4$")
        self.assertIn("· draft", v["jobs"], "the job line says it is a draft")
        self.assertIn("draft ·", v["jobs"], "and so does the finished-videos list")

    def test_a_recipe_that_cannot_be_made_says_why_in_the_card(self):
        v = self.run_js("bad")["view"]
        self.assertIn("err", v["noticeClass"].split(), "a failure is red")
        self.assertIn("Cannot make this yet", v["notice"])
        self.assertIn("clips: give at least one clip", v["notice"])
        self.assertEqual(list((self.root / "exports").glob("*.mp4")), [])

    def test_a_recipe_can_be_saved_and_loaded_back_into_the_form(self):
        out = self.run_js("save")
        self.assertIn("Saved My reel.montage.json", out["saved"]["notice"])
        self.assertTrue(any("My reel" in s for s in out["saved"]["saved"]))
        self.assertEqual(out["used"]["values"]["template"], "melancholy", "Use restores what was saved, not what is in the form now")
        self.assertTrue((self.root / "recipes" / "My reel.montage.json").is_file())

    def test_the_brief_for_claude_is_copied_with_the_files_and_captions(self):
        out = self.run_js("brief")
        self.assertIn("Brief copied", out["view"]["notice"])
        self.assertIn("ok", out["view"]["noticeClass"].split(), "good news is not shown in the error colour")
        self.assertNotIn("err", out["view"]["noticeClass"].split())
        self.assertIn("# Montage brief: Demo", out["clip"])
        self.assertIn("The dean opening the event", out["clip"])
        self.assertIn("song.m4a", out["clip"])



@unittest.skipUnless(NODE, "needs node")
class TapTempo(unittest.TestCase):
    """The pure tap-to-BPM function from the page's script, run under node."""
    def run_cases(self, cases):
        import subprocess, tempfile
        from umcares import webpages
        script = ("const vm = require('vm'); const ctx = vm.createContext({}); vm.runInContext(process.argv[1], ctx);"
                  "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(c => ctx.tempoFromTaps(c))));")
        r = subprocess.run([NODE, "-e", script, webpages.MONTAGE_JS, json.dumps(cases)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def taps(self, gap, n=8, start=1000):
        return [start + i * gap for i in range(n)]

    def test_even_taps_give_the_tempo(self):
        got = self.run_cases([self.taps(500), self.taps(800), self.taps(600)])
        self.assertEqual(got, [120.0, 75.0, 100.0])

    def test_one_clumsy_tap_does_not_move_it(self):
        late = self.taps(500)
        late[4] += 180
        self.assertEqual(self.run_cases([late]), [120.0])

    def test_a_missed_beat_does_not_halve_the_tempo(self):
        missed = [1000, 1500, 2000, 2500, 3500, 4000, 4500, 5000]            # one beat not tapped: a 1000 ms gap among 500 ms ones
        self.assertEqual(self.run_cases([missed]), [120.0], "the median ignores it; a mean would say about 105")

    def test_too_few_taps_say_nothing_and_extremes_are_folded_into_50_to_200(self):
        got = self.run_cases([self.taps(500, n=3), self.taps(250), self.taps(1500), self.taps(200)])
        self.assertEqual(got, [None, 120.0, 80.0, 150.0], "240 is halved to 120, 40 is doubled to 80, 300 is halved to 150")

    def test_only_the_recent_taps_count(self):
        slow_then_fast = [1000 + i * 1000 for i in range(5)]
        t = slow_then_fast[-1]
        fast = [t + (i + 1) * 500 for i in range(12)]
        self.assertEqual(self.run_cases([slow_then_fast + fast]), [120.0])


if __name__ == "__main__":
    unittest.main()
