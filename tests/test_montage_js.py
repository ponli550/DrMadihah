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
        self.assertEqual(v["music"], ["No music (add a track in the app when you post)", "song.m4a"])
        self.assertEqual([r["text"] for r in v["rows"]], [" 🖼 The dean opening the event", " 🖼 port.jpg", " 🎞 clip.mp4"])
        self.assertEqual(v["rows"][0]["sub"], "land.jpg", "the filename sits under a caption")
        self.assertTrue(all(r["checked"] for r in v["rows"]))
        self.assertEqual(v["values"], {"format": "reels", "template": "slowmo", "music": "", "max": "30"})
        for b in ("Make video", "Preview plan", "Save recipe…", "Copy brief for Claude"):
            self.assertIn(b, v["buttons"])

    def test_cut_on_the_beat_is_only_available_with_music_and_reaches_the_server(self):
        out = self.run_js("sync")
        self.assertTrue(out["disabledAtStart"], "nothing to sync to without a song")
        self.assertTrue(out["enabledAfterMusic"])
        self.assertIn("no steady beat", out["withSync"]["plan"], "the option was sent; a plain tone has no beat to cut to")
        self.assertEqual(out["afterRemoval"], {"disabled": True, "checked": False}, "removing the song clears the option")

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


if __name__ == "__main__":
    unittest.main()
