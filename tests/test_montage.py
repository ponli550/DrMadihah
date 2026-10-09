import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from umcares import captions, montage, prepare, projects, textimg, uploads
from tests.test_prepare import make_conformant, put

FF, FP = prepare.find_tool("ffmpeg"), prepare.find_tool("ffprobe")
NEED = unittest.skipUnless(FF and FP and textimg.available(), "needs ffmpeg, ffprobe and macOS text drawing")
TINY = {"reels": (216, 384), "square": (216, 216), "wide": (384, 216)}


def ff(*a):
    subprocess.run([FF, "-v", "error", "-y", *a], check=True, capture_output=True)


def stats(path):
    d = json.loads(subprocess.run([FP, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                                  capture_output=True, text=True).stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": v["r_frame_rate"], "codec": v["codec_name"], "pix": v["pix_fmt"],
            "range": v.get("color_range"),
            "dur": float(d["format"]["duration"]), "audio": a["codec_name"] if a else None}


def frame(path, at, w, h):
    return subprocess.run([FF, "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, check=True).stdout


def visible(d):
    """Files in a folder, not counting the scaffold's hidden notes."""
    return [p.name for p in Path(d).iterdir() if not p.name.startswith(".")]


def recipe(clips, **kw):
    return {"kind": "montage", "title": "Test", "format": "reels", "template": "slowmo", "clips": clips, **kw}


class Fixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Path(tempfile.mkdtemp())
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360", "-frames:v", "1", str(cls.fx / "land.jpg"))
        ff("-f", "lavfi", "-i", "testsrc2=size=360x640", "-frames:v", "1", str(cls.fx / "port.jpg"))
        ff("-f", "lavfi", "-i", "smptebars=size=400x400", "-frames:v", "1", str(cls.fx / "square.jpg"))
        make_conformant(cls.fx / "clip.mp4", dur=4, size="640x360")
        make_conformant(cls.fx / "short.mp4", dur=1, size="360x640")
        ff("-f", "lavfi", "-i", "sine=frequency=330:duration=30", "-c:a", "aac", "-ar", "48000", str(cls.fx / "song.m4a"))
        ff("-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-c:a", "aac", "-ar", "48000", str(cls.fx / "jingle.m4a"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fx, ignore_errors=True)

    def setUp(self):
        self._p = mock.patch.dict(montage.FORMATS["reels"], width=TINY["reels"][0], height=TINY["reels"][1])
        self._p2 = mock.patch.dict(montage.FORMATS["square"], width=TINY["square"][0], height=TINY["square"][1])
        self._p3 = mock.patch.dict(montage.FORMATS["wide"], width=TINY["wide"][0], height=TINY["wide"][1])
        for p in (self._p, self._p2, self._p3):
            p.start()
        montage._info_cache.clear()
        prepare._probes.clear()
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=False)["path"])

    def tearDown(self):
        montage.RENDERER.cancel(self.root)
        montage.RENDERER.wait_idle(30)
        for p in (self._p, self._p2, self._p3):
            p.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def add(self, *names, music=()):
        for i, n in enumerate(names):
            put(self.root, self.fx / n, mtime=1_700_000_000_000 + i * 60_000)      # a phone's lastModified, in ms
        for m in music:
            (self.root / "assets" / "music").mkdir(parents=True, exist_ok=True)
            shutil.copy(self.fx / m, self.root / "assets" / "music" / m)
            os.utime(self.root / "assets" / "music" / m, (1_700_001_000, 1_700_001_000))

    def sha(self, name):
        return next(i["sha"] for i in captions.items(self.root) if i["name"] == name)

    def render(self, rec, timeout=180):
        montage.RENDERER.submit(self.root, rec)
        self.assertTrue(montage.RENDERER.wait_idle(timeout), "the render never finished")
        job = montage.RENDERER.snapshot(self.root)[-1]
        return job, (self.root / "exports" / job["output"] if job["output"] else None)


@NEED
class Catalog(Fixtures):
    def test_lists_pictures_clips_and_songs_oldest_first_with_refs_and_captions(self):
        self.add("land.jpg", "clip.mp4", music=["song.m4a"])
        captions.set_detail(self.root, self.sha("land.jpg"), "The registration desk")
        items = montage.catalog(self.root)
        self.assertEqual([(i["name"], i["kind"]) for i in items], [("land.jpg", "image"), ("clip.mp4", "video"), ("song.m4a", "audio")])
        self.assertEqual(items[0]["caption"], "The registration desk")
        self.assertEqual(len(items[0]["ref"]), 12)
        self.assertIsNone(items[2]["sha"], "a file not uploaded through the web is referred to by name")

    def test_a_prepared_video_is_listed_once_and_points_at_its_edit_ready_copy(self):
        self.add("clip.mp4")
        sha = self.sha("clip.mp4")
        prepare.PREPARER.submit(self.root, [sha])
        prepare.PREPARER.wait_idle(60)
        vids = [i for i in montage.catalog(self.root) if i["kind"] == "video"]
        self.assertEqual(len(vids), 1)
        self.assertTrue(vids[0]["prepared"])
        self.assertTrue(vids[0]["rel"].startswith("assets/edit_ready/"))

    def test_hidden_and_in_flight_files_are_not_offered(self):
        self.add("land.jpg")
        (self.root / "assets" / "photos" / ".hidden.jpg").write_bytes(b"x")
        (self.root / "assets" / "edit_ready").mkdir(exist_ok=True)
        (self.root / "assets" / "edit_ready" / "x.abc.part.mp4").write_bytes(b"x")
        self.assertEqual([i["name"] for i in montage.catalog(self.root)], ["land.jpg"])


@NEED
class Validation(Fixtures):
    def test_a_minimal_recipe_gets_its_defaults(self):
        self.add("land.jpg")
        r = montage.validate({"clips": [{"media": "land.jpg"}]}, self.root)
        self.assertEqual((r["format"], r["template"], r["captions"], r["max_seconds"]), ("reels", "slowmo", True, 60))

    def test_every_way_to_refer_to_a_file_works(self):
        self.add("land.jpg", "port.jpg")
        sha = self.sha("land.jpg")
        for ref in (sha, sha[:8], sha[:12], "land.jpg", "LAND.JPG", "photos/land.jpg"):
            r = montage.validate({"clips": [{"media": ref}]}, self.root)
            self.assertEqual(r["clips"][0]["_item"]["name"], "land.jpg", ref)

    def test_all_the_problems_come_back_at_once_each_with_its_path(self):
        self.add("land.jpg", "clip.mp4")
        bad = {"kind": "slideshow", "title": 5, "format": "imax", "template": "disco", "captions": "yes", "max_seconds": 500,
               "colour": "red", "clips": [
                   {"media": "nope.jpg"}, {"media": "land.jpg", "hold": 99}, {"media": "land.jpg", "speed": 0.5},
                   {"media": "clip.mp4", "speed": 9}, {"media": "land.jpg", "caption": "x" * 201}, {"media": "land.jpg", "wobble": 1}, "land.jpg", {}],
               "title_card": {"subtitle": "no text"}, "end_card": {"text": "ok", "hold": 99}, "music": {"file": "missing.mp3"}}
        with self.assertRaises(montage.RecipeError) as cm:
            montage.validate(bad, self.root)
        text = "\n".join(cm.exception.problems)
        for needle in ("kind:", "title:", "format:", "template:", "captions:", "max_seconds:", "colour: unknown field",
                       "clips[0].media: no image/video file matches 'nope.jpg'", "clips[1].hold", "clips[2]: speed and start apply to videos",
                       "clips[3].speed", "clips[4].caption", "clips[5].wobble: unknown field", "clips[6]: must be an object", "clips[7].media",
                       "title_card:", "end_card.hold", "music.file: no audio file matches 'missing.mp3'"):
            self.assertIn(needle, text, needle)

    def test_an_unknown_file_suggests_what_was_probably_meant(self):
        self.add("land.jpg")
        with self.assertRaises(montage.RecipeError) as cm:
            montage.validate({"clips": [{"media": "land.JPEG"}]}, self.root)
        self.assertIn("did you mean land.jpg", cm.exception.problems[0])

    def test_a_name_that_matches_two_files_is_refused_with_both_listed(self):
        self.add("land.jpg")
        (self.root / "assets" / "edit_ready").mkdir(exist_ok=True)
        shutil.copy(self.fx / "land.jpg", self.root / "assets" / "edit_ready" / "land.jpg")
        with self.assertRaises(montage.RecipeError) as cm:
            montage.validate({"clips": [{"media": "land.jpg"}]}, self.root)
        self.assertIn("matches 2 files", cm.exception.problems[0])
        self.assertIn("edit_ready/land.jpg", cm.exception.problems[0])

    def test_a_pasted_hash_prefix_that_is_too_short_is_a_name_not_a_guess(self):
        self.add("land.jpg")
        with self.assertRaises(montage.RecipeError):
            montage.validate({"clips": [{"media": self.sha("land.jpg")[:6]}]}, self.root)

    def test_no_clips_or_far_too_many_are_refused(self):
        self.add("land.jpg")
        for clips in ([], None, "land.jpg"):
            with self.assertRaises(montage.RecipeError):
                montage.validate({"clips": clips}, self.root)
        with self.assertRaises(montage.RecipeError) as cm:
            montage.validate({"clips": [{"media": "land.jpg"}] * 61}, self.root)
        self.assertIn("at most 60", cm.exception.problems[0])

    def test_a_recipe_that_is_not_an_object_is_refused(self):
        for bad in ([], "x", 5, None):
            with self.assertRaises(montage.RecipeError):
                montage.validate(bad, self.root)

    def test_booleans_are_not_numbers(self):
        self.add("clip.mp4")
        with self.assertRaises(montage.RecipeError):
            montage.validate({"clips": [{"media": "clip.mp4", "hold": True}]}, self.root)


@NEED
class Resolving(Fixtures):
    def plan(self, clips, **kw):
        return montage.plan_for(recipe(clips, **kw), self.root)

    def test_durations_come_from_the_template_and_the_real_clip(self):
        self.add("land.jpg", "clip.mp4")
        p = self.plan([{"media": "land.jpg"}, {"media": "clip.mp4"}])
        photo, video = p["segments"]
        self.assertEqual((photo["kind"], photo["duration"]), ("photo", 2.6))
        self.assertEqual((video["kind"], video["speed"], video["src_used"], video["duration"]), ("video", 0.75, 3.0, 4.0))
        self.assertEqual(p["fade"], 0.5)
        self.assertEqual(p["total"], round(2.6 + 4.0 - 0.5, 3))

    def test_a_short_clip_is_used_whole_not_stretched_past_its_end(self):
        self.add("short.mp4")
        v = self.plan([{"media": "short.mp4"}])["segments"][0]
        self.assertAlmostEqual(v["src_used"], 1.0, delta=0.1)
        self.assertAlmostEqual(v["duration"], v["src_used"] / 0.75, delta=0.01)

    def test_hold_speed_and_start_are_honoured(self):
        self.add("land.jpg", "clip.mp4")
        p = self.plan([{"media": "land.jpg", "hold": 5}, {"media": "clip.mp4", "start": 1.0, "speed": 0.5, "hold": 2}])
        self.assertEqual(p["segments"][0]["duration"], 5)
        v = p["segments"][1]
        self.assertEqual((v["src_start"], v["speed"], v["src_used"], v["duration"]), (1.0, 0.5, 1.0, 2.0))

    def test_start_times_account_for_the_crossfade_overlap(self):
        self.add("land.jpg", "port.jpg", "square.jpg")
        p = self.plan([{"media": "land.jpg"}, {"media": "port.jpg"}, {"media": "square.jpg"}])
        self.assertEqual([s["start"] for s in p["segments"]], [0.0, 2.1, 4.2])
        self.assertEqual(p["total"], 6.8)                                # 3 x 2.6 minus two 0.5 s overlaps

    def test_hard_cut_templates_have_no_overlap(self):
        self.add("land.jpg", "port.jpg")
        p = self.plan([{"media": "land.jpg"}, {"media": "port.jpg"}], template="punchy")
        self.assertEqual((p["fade"], p["total"]), (0, 2.4))

    def test_a_crossfade_is_never_longer_than_the_pieces_it_joins(self):
        self.add("land.jpg", "port.jpg")
        p = self.plan([{"media": "land.jpg", "hold": 0.8}, {"media": "port.jpg", "hold": 0.8}], template="melancholy")
        self.assertLessEqual(p["fade"], 0.4 * 0.8 + 1e-9)

    def test_the_same_recipe_always_gives_the_same_plan(self):
        self.add("land.jpg", "clip.mp4")
        r = recipe([{"media": "land.jpg", "caption": "A"}, {"media": "clip.mp4"}], title_card={"text": "T"})
        a, b = montage.plan_for(r, self.root), montage.plan_for(json.loads(json.dumps(r)), self.root)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_too_long_drops_clips_from_the_end_with_a_warning_and_keeps_the_cards(self):
        self.add("land.jpg", "port.jpg", "square.jpg")
        p = self.plan([{"media": "land.jpg"}, {"media": "port.jpg"}, {"media": "square.jpg"}], max_seconds=9,
                      title_card={"text": "T"}, end_card={"text": "E"})
        self.assertLessEqual(p["total"], 9)
        kinds = [s["kind"] for s in p["segments"]]
        self.assertEqual((kinds[0], kinds[-1]), ("title", "end"))
        self.assertEqual(kinds.count("photo"), 2, "one clip left out, from the end")
        self.assertEqual([s["name"] for s in p["segments"] if s["kind"] == "photo"], ["land.jpg", "port.jpg"])
        self.assertEqual(p["warnings"], ["1 clip left out to stay within 9 seconds: square.jpg"], "it names what was dropped")

    def test_the_warning_names_the_dropped_clips_by_caption_and_shortens_a_long_list(self):
        self.add("land.jpg", "port.jpg")
        clips = [{"media": "land.jpg" if i % 2 else "port.jpg", "caption": f"c{i}", "hold": 6} for i in range(7)]
        p = self.plan(clips, max_seconds=10, template="punchy")
        self.assertEqual(len(p["segments"]), 1)
        self.assertEqual(p["warnings"], ["6 clips left out to stay within 10 seconds: c1, c2, c3, c4 and 2 more"])

    def test_when_even_one_clip_and_the_cards_are_too_long_it_says_so_instead_of_pretending(self):
        self.add("land.jpg", "port.jpg")
        p = self.plan([{"media": "land.jpg"}, {"media": "port.jpg"}], max_seconds=6, title_card={"text": "T"}, end_card={"text": "E"})
        self.assertGreater(p["total"], 6)
        self.assertTrue(any("even with one clip" in w and "longer than the 6s limit" in w for w in p["warnings"]), p["warnings"])

    def test_a_clip_is_never_dropped_to_nothing(self):
        self.add("land.jpg")
        p = self.plan([{"media": "land.jpg", "hold": 15}], max_seconds=5)
        self.assertEqual(len(p["segments"]), 1)

    def test_starting_past_the_end_of_a_clip_is_an_error_that_names_it(self):
        self.add("clip.mp4")
        with self.assertRaises(montage.MontageError) as cm:
            self.plan([{"media": "clip.mp4", "start": 9}])
        self.assertIn("past the end of clip.mp4", str(cm.exception))

    def test_captions_follow_the_flag_and_a_clip_caption_beats_the_file_caption(self):
        self.add("land.jpg", "port.jpg")
        captions.set_detail(self.root, self.sha("land.jpg"), "From the owner")
        on = self.plan([{"media": "land.jpg"}, {"media": "land.jpg", "caption": "Mine"}, {"media": "port.jpg"}])
        self.assertEqual([s["text"] for s in on["segments"]], ["From the owner", "Mine", ""])
        off = self.plan([{"media": "land.jpg"}], captions=False)
        self.assertEqual(off["segments"][0]["text"], "")

    def test_music_that_is_too_short_warns_and_one_started_past_its_end_is_refused(self):
        self.add("land.jpg", music=["jingle.m4a", "song.m4a"])
        p = self.plan([{"media": "land.jpg"}], music={"file": "jingle.m4a"})
        self.assertTrue(any("music ends" in w for w in p["warnings"]))
        with self.assertRaises(montage.MontageError):
            self.plan([{"media": "land.jpg"}], music={"file": "song.m4a", "start": 29.5})

    def test_a_file_that_vanished_is_reported_by_name(self):
        self.add("land.jpg")
        v = montage.validate({"clips": [{"media": "land.jpg"}]}, self.root)
        (self.root / "assets" / "photos" / "land.jpg").unlink()
        with self.assertRaises(montage.MontageError) as cm:
            montage.resolve(v, self.root)
        self.assertIn("land.jpg is no longer in the project", str(cm.exception))


@NEED
class Rendering(Fixtures):
    def test_a_reel_comes_out_the_right_size_rate_codec_and_length(self):
        self.add("land.jpg", "port.jpg", "clip.mp4")
        rec = recipe([{"media": "land.jpg", "caption": "Opening"}, {"media": "port.jpg"}, {"media": "clip.mp4", "caption": "Panel"}],
                     title_card={"text": "CyberDSA", "subtitle": "Highlights"}, end_card={"text": "Terima kasih"})
        plan = montage.plan_for(rec, self.root)
        job, out = self.render(rec)
        self.assertEqual(job["state"], "done", job["error"])
        s = stats(out)
        self.assertEqual((s["w"], s["h"], s["fps"], s["codec"], s["pix"], s["audio"]), (216, 384, "30/1", "h264", "yuv420p", None))
        self.assertEqual(s["range"], "tv", "limited range: full-range colour looks wrong on Premiere and on social platforms")
        self.assertAlmostEqual(s["dur"], plan["total"], delta=0.35)
        self.assertEqual(job["pieces"], 5)

    def test_the_other_formats_have_their_own_shape(self):
        self.add("land.jpg")
        for fmt in ("square", "wide"):
            job, out = self.render(recipe([{"media": "land.jpg"}], format=fmt, template="punchy"))
            self.assertEqual(job["state"], "done", job["error"])
            self.assertEqual((stats(out)["w"], stats(out)["h"]), TINY[fmt])
            self.assertEqual((stats(out)["pix"], stats(out)["range"]), ("yuv420p", "tv"), fmt)       # the hard-cut path too

    def test_every_template_renders_so_none_has_a_broken_filter(self):
        self.add("land.jpg", "clip.mp4")
        for name in montage.TEMPLATES:
            job, out = self.render(recipe([{"media": "land.jpg", "caption": "x"}, {"media": "clip.mp4"}], template=name))
            self.assertEqual(job["state"], "done", f"{name}: {job['error']}")
            self.assertEqual(stats(out)["codec"], "h264", name)

    def test_music_is_mixed_in_and_the_video_keeps_its_length(self):
        self.add("land.jpg", "port.jpg", music=["song.m4a"])
        rec = recipe([{"media": "land.jpg"}, {"media": "port.jpg"}], music={"file": "song.m4a", "start": 2, "fade_out": 1})
        plan = montage.plan_for(rec, self.root)
        job, out = self.render(rec)
        self.assertEqual(job["state"], "done", job["error"])
        s = stats(out)
        self.assertEqual(s["audio"], "aac")
        self.assertAlmostEqual(s["dur"], plan["total"], delta=0.35)

    def test_captions_are_really_drawn_on_the_picture(self):
        self.add("land.jpg")
        base = recipe([{"media": "land.jpg", "caption": "Selamat datang ke CyberDSA"}], template="clean")
        _, with_text = self.render(base)
        _, without = self.render({**base, "captions": False})
        a, b = frame(with_text, 1.2, 216, 384), frame(without, 1.2, 216, 384)
        self.assertEqual(len(a), len(b))
        differing = sum(1 for i in range(0, len(a), 3) if abs(a[i] - b[i]) > 40)
        self.assertGreater(differing, 150, "the caption plate and text should change a few hundred pixels")
        top = slice(0, 216 * 3 * 100)                                    # the top of the frame carries no caption
        self.assertEqual(sum(1 for i in range(0, len(a[top]), 3) if abs(a[i] - b[i]) > 40), 0)

    def bands(self, path, at, w=216, h=384):
        """Runs of rows that contain bright (text) pixels, top to bottom, as (first_row, last_row)."""
        raw = subprocess.run([FF, "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True, check=True).stdout
        rows = [y for y in range(h) if any(raw[y * w + x] > 180 for x in range(w))]
        runs = []
        for y in rows:
            if runs and y - runs[-1][1] <= 3:
                runs[-1][1] = y
            else:
                runs.append([y, y])
        return runs, raw

    def test_a_title_card_puts_the_title_above_the_subtitle_with_a_gap_and_both_inside_the_frame(self):
        self.add("land.jpg")
        _, out = self.render(recipe([{"media": "land.jpg"}], title_card={"text": "CyberDSA 2026", "subtitle": "Hari terbuka"},
                                    template="punchy"))
        runs, _ = self.bands(out, 1.2)
        self.assertEqual(len(runs), 2, f"a title band and a subtitle band, not one overlapping blob: {runs}")
        (t0, t1), (s0, s1) = runs
        self.assertGreater(s0 - t1, 6, "a gap between them")
        self.assertGreater(t1 - t0, s1 - s0, "the title is bigger than the subtitle")
        self.assertTrue(t0 > 20 and s1 < 364, "inside the frame")

    def test_a_title_card_without_a_subtitle_is_centred(self):
        self.add("land.jpg")
        _, out = self.render(recipe([{"media": "land.jpg"}], title_card={"text": "CyberDSA 2026"}, template="punchy"))
        runs, raw = self.bands(out, 1.2)
        self.assertEqual(len(runs), 1, runs)
        self.assertAlmostEqual((runs[0][0] + runs[0][1]) / 2, 192, delta=25)
        cols = [x for x in range(216) if any(raw[y * 216 + x] > 180 for y in range(runs[0][0], runs[0][1] + 1))]
        self.assertGreater(cols[0], 8, "not touching the left edge")
        self.assertLess(cols[-1], 207, "not touching the right edge")

    def test_a_photo_with_an_exif_orientation_flag_renders(self):
        src = (self.fx / "land.jpg").read_bytes()
        tiff = b"MM\x00\x2a\x00\x00\x00\x08" + struct.pack(">H", 1) + struct.pack(">HHI", 0x0112, 3, 1) + struct.pack(">HH", 6, 0) + b"\x00\x00\x00\x00"
        exif = b"Exif\x00\x00" + tiff
        (self.fx / "exif6.jpg").write_bytes(src[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + src[2:])
        self.add("exif6.jpg")
        job, out = self.render(recipe([{"media": "exif6.jpg"}]))
        self.assertEqual(job["state"], "done", job["error"])

    def test_the_sidecar_records_the_recipe_and_the_resolved_plan(self):
        self.add("land.jpg")
        rec = recipe([{"media": "land.jpg"}], note="for the owner")
        job, out = self.render(rec)
        side = json.loads(out.with_suffix(".json").read_text())
        self.assertEqual(side["recipe"]["note"], "for the owner")
        self.assertEqual(side["plan"]["segments"][0]["duration"], 2.6)
        self.assertNotIn(str(self.home), json.dumps(side), "no path of the machine in what is kept")
        listed = montage.exports(self.root)
        self.assertEqual((listed[0]["name"], listed[0]["format"], listed[0]["template"]), (out.name, "reels", "slowmo"))

    def test_nothing_is_left_behind_after_a_render(self):
        self.add("land.jpg", "clip.mp4")
        self.render(recipe([{"media": "land.jpg"}, {"media": "clip.mp4"}]))
        self.assertEqual(list((self.root / ".umcares" / "montage").iterdir()), [], "the working folder is removed")
        self.assertEqual([p.name for p in (self.root / "exports").iterdir() if ".part" in p.name], [])

    def test_two_renders_in_the_same_second_never_overwrite_each_other(self):
        self.add("land.jpg")
        rec = recipe([{"media": "land.jpg"}], template="punchy")
        montage.RENDERER.submit(self.root, rec)
        montage.RENDERER.submit(self.root, {**rec, "template": "clean"})      # same title and format, so the same file name
        montage.RENDERER.wait_idle(120)
        done = [j for j in montage.RENDERER.snapshot(self.root) if j["state"] == "done"]
        self.assertEqual(len({j["output"] for j in done}), 2)

    def test_cancelling_mid_render_stops_ffmpeg_and_leaves_nothing_behind(self):
        self.add("land.jpg", "port.jpg", "square.jpg", "clip.mp4")
        slow = ["-c:v", "libx264", "-preset", "placebo", "-crf", "10", "-pix_fmt", "yuv420p", "-an"]
        with mock.patch.object(montage, "ENCODE_PIECE", slow):
            montage.RENDERER.submit(self.root, recipe([{"media": n} for n in ("land.jpg", "port.jpg", "square.jpg", "clip.mp4")],
                                                      title_card={"text": "T"}, end_card={"text": "E"}))
            end = time.time() + 30
            while time.time() < end and not any(j["state"] == "running" for j in montage.RENDERER.snapshot(self.root)):
                time.sleep(0.01)
            time.sleep(0.4)
            self.assertEqual(montage.RENDERER.cancel(self.root), 1)
            self.assertTrue(montage.RENDERER.wait_idle(60))
        job = montage.RENDERER.snapshot(self.root)[-1]
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(visible(self.root / "exports"), [])
        self.assertEqual(list((self.root / ".umcares" / "montage").iterdir()), [])

    def test_a_failing_ffmpeg_is_reported_and_leaves_nothing_behind(self):
        self.add("land.jpg")
        bad = [(["/bin/sh", "-c", "echo 'filter exploded' >&2; exit 4"], 1.0)]
        with mock.patch.object(montage, "_piece_command", return_value=bad):
            job, out = self.render(recipe([{"media": "land.jpg"}]))
        self.assertEqual(job["state"], "failed")
        self.assertIn("filter exploded", job["error"])
        self.assertIsNone(out)
        self.assertEqual(visible(self.root / "exports"), [])

    def test_a_file_deleted_after_queuing_fails_early_and_names_it(self):
        self.add("land.jpg")
        with prepare.HEAVY:                                              # hold the queue so the job cannot start yet
            montage.RENDERER.submit(self.root, recipe([{"media": "land.jpg"}]))
            (self.root / "assets" / "photos" / "land.jpg").unlink()
        montage.RENDERER.wait_idle(60)
        job = montage.RENDERER.snapshot(self.root)[-1]
        self.assertEqual(job["state"], "failed")
        self.assertIn("land.jpg is no longer in the project", job["error"])

    def test_a_full_disk_stops_before_any_work(self):
        self.add("land.jpg")
        fake = shutil._ntuple_diskusage(100, 99, 1 << 20)
        with mock.patch.object(montage.shutil, "disk_usage", return_value=fake):
            job, _ = self.render(recipe([{"media": "land.jpg"}]))
        self.assertEqual(job["state"], "failed")
        self.assertIn("not enough free disk space", job["error"])

    def test_pressing_make_twice_with_the_same_recipe_is_one_job_not_two(self):
        self.add("land.jpg")
        rec = recipe([{"media": "land.jpg"}], template="punchy")
        with prepare.HEAVY:                                              # keep the first one waiting
            a = montage.RENDERER.submit(self.root, rec)
            b = montage.RENDERER.submit(self.root, json.loads(json.dumps(rec)))
            c = montage.RENDERER.submit(self.root, {**rec, "template": "clean"})
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], c["id"], "a different recipe is a different job")
        self.assertEqual(len(montage.RENDERER.snapshot(self.root)), 2)
        montage.RENDERER.wait_idle(120)
        again = montage.RENDERER.submit(self.root, rec)                    # finished, so asking again is a new video
        self.assertNotEqual(again["id"], a["id"])
        montage.RENDERER.wait_idle(120)
        self.assertEqual(len([j for j in montage.RENDERER.snapshot(self.root) if j["state"] == "done"]), 3)

    def test_a_bad_recipe_is_refused_at_submit_and_queues_nothing(self):
        with self.assertRaises(montage.RecipeError):
            montage.RENDERER.submit(self.root, {"clips": [{"media": "nope.jpg"}]})
        self.assertEqual(montage.RENDERER.snapshot(self.root), [])

    def test_a_render_waits_for_a_prepare_job_instead_of_competing_with_it(self):
        self.add("land.jpg")
        with prepare.HEAVY:                                              # a prepare job would hold this while it runs
            montage.RENDERER.submit(self.root, recipe([{"media": "land.jpg"}], template="punchy"))
            time.sleep(0.8)
            self.assertEqual(montage.RENDERER.snapshot(self.root)[-1]["state"], "queued", "must not run alongside a prepare")
        self.assertTrue(montage.RENDERER.wait_idle(120))
        self.assertEqual(montage.RENDERER.snapshot(self.root)[-1]["state"], "done")

    def test_the_answers_never_leak_a_path_on_the_machine(self):
        self.add("land.jpg")
        job, _ = self.render(recipe([{"media": "land.jpg"}], template="punchy"))
        self.assertNotIn(str(self.home), json.dumps(job))


@NEED
class Recipes(Fixtures):
    def test_a_recipe_is_saved_listed_and_loaded(self):
        self.add("land.jpg")
        name = montage.save_recipe(self.root, "My reel", recipe([{"media": "land.jpg"}]))
        self.assertEqual(name, "My reel.montage.json")
        listed = montage.list_recipes(self.root)
        self.assertEqual([(r["name"], r["valid"]) for r in listed], [("My reel", True)])
        self.assertEqual(listed[0]["recipe"]["template"], "slowmo")

    def test_an_invalid_recipe_is_not_saved(self):
        self.add("land.jpg")
        with self.assertRaises(montage.RecipeError):
            montage.save_recipe(self.root, "bad", recipe([{"media": "nope.jpg"}]))
        self.assertFalse((self.root / "recipes" / "bad.montage.json").exists())

    def test_a_name_cannot_climb_out_of_the_recipes_folder(self):
        self.add("land.jpg")
        for bad in ("../evil", "a/b", ".hidden", "", "x" * 61, "a\x00b"):
            with self.assertRaises(montage.MontageError, msg=repr(bad)):
                montage.save_recipe(self.root, bad, recipe([{"media": "land.jpg"}]))

    def test_a_saved_recipe_turns_invalid_when_its_media_disappears_and_says_why(self):
        self.add("land.jpg")
        montage.save_recipe(self.root, "r", recipe([{"media": "land.jpg"}]))
        (self.root / "assets" / "photos" / "land.jpg").unlink()
        r = montage.list_recipes(self.root)[0]
        self.assertFalse(r["valid"])
        self.assertIn("no image/video file matches 'land.jpg'", r["problems"][0])

    def test_a_hand_written_recipe_file_is_picked_up(self):
        self.add("land.jpg")
        (self.root / "recipes").mkdir(exist_ok=True)
        (self.root / "recipes" / "by-claude.montage.json").write_text(json.dumps(recipe([{"media": "land.jpg"}])))
        (self.root / "recipes" / "broken.montage.json").write_text("{not json")
        got = {r["name"]: r["valid"] for r in montage.list_recipes(self.root)}
        self.assertEqual(got, {"broken": False, "by-claude": True})

    def test_the_brief_lists_the_files_their_captions_and_a_valid_example_shape(self):
        self.add("land.jpg", "clip.mp4", music=["song.m4a"])
        captions.set_detail(self.root, self.sha("land.jpg"), "Opening speech by the dean")
        text = montage.brief(self.root, "CyberDSA")
        self.assertIn("# Montage brief: CyberDSA", text)
        self.assertIn(self.sha("land.jpg")[:12], text)
        self.assertIn("Opening speech by the dean", text)
        self.assertIn("song.m4a", text)
        self.assertIn("never compute durations", text)
        self.assertNotIn(str(self.home), text)
        example = json.loads(text.split("```json")[1].split("```")[0])
        self.assertEqual(example["kind"], "montage")
        self.assertEqual(set(example) - montage.ALLOWED_TOP, set())


class Plain(unittest.TestCase):
    def test_every_template_has_what_the_renderer_needs(self):
        need = {"label", "blurb", "photo_hold", "video_max", "video_speed", "fade", "zoom", "vignette", "grain", "grade", "bg"}
        for name, t in montage.TEMPLATES.items():
            self.assertEqual(need - set(t), set(), name)
            self.assertTrue(0 <= t["fade"] < 2 and 0 < t["video_speed"] <= 2 and t["photo_hold"] >= 1, name)

    def test_formats_have_even_sizes_x264_can_encode(self):
        for name, f in montage.FORMATS.items():
            self.assertEqual((f["width"] % 2, f["height"] % 2), (0, 0), name)

    def test_export_names_are_safe_slugs(self):
        n = montage.export_name({"title": "CyberDSA 2026 — Hari Raya!/..", "format": "reels"})
        self.assertRegex(n, r"^[a-z0-9-]+-reels-\d{8}-\d{6}\.mp4$")
        self.assertRegex(montage.export_name({"title": "", "format": "wide"}), r"^montage-wide-")


if __name__ == "__main__":
    unittest.main()
