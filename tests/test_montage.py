import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from umcares import captions, kinetic, montage, prepare, projects, sfx, textimg, uploads
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
                       "clips[3].speed", "clips[4].caption", "clips[5].wobble: unknown field", "clips[6]: must be an object", "clips[7]: give exactly one of media",
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

    def bands(self, path, at, w=216, h=384, thresh=180):
        """Runs of rows that contain bright (text) pixels, top to bottom, as (first_row, last_row)."""
        raw = subprocess.run([FF, "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True, check=True).stdout
        rows = [y for y in range(h) if any(raw[y * w + x] > thresh for x in range(w))]
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
        self.assertIn("Hook (first 2 to 3 seconds)", text)
        self.assertIn("pan_left", text)
        self.assertIn("one caption per shot at most", text)
        self.assertNotIn(str(self.home), text)
        example = json.loads(text.split("```json")[1].split("```")[0])
        self.assertEqual(example["kind"], "montage")
        self.assertEqual(set(example) - montage.ALLOWED_TOP, set())


def gray(path, at, w, h):
    return subprocess.run([FF, "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, check=True).stdout


def mean_diff(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def best_shift(a, b, w, h, rng=24):
    """How far the picture moved sideways between two frames: the shift of the middle row that matches best."""
    row = lambda f: list(f[(h // 2) * w:(h // 2 + 1) * w])
    ra, rb = row(a), row(b)
    best, arg = None, 0
    for d in range(-rng, rng + 1):
        lo, hi = max(0, d), min(w, w + d)
        err = sum(abs(ra[x - d] - rb[x]) for x in range(lo + rng, hi - rng)) / max(1, hi - lo - 2 * rng)
        if best is None or err < best:
            best, arg = err, d
    return arg


@NEED
class Motion(Fixtures):
    """The camera and the text move on eased curves, with real pixels as the proof."""
    W, H = 216, 384

    def photo(self, move, hold=3.0, name="land.jpg", template="clean", **kw):
        self.add(name)
        _, out = self.render(recipe([{"media": name, "move": move, "hold": hold, **kw.pop("clip", {})}], template=template, captions=False, **kw))
        return out

    def test_the_moves_are_part_of_the_vocabulary_and_wrong_ones_are_refused_with_a_reason(self):
        self.add("land.jpg", "clip.mp4")
        for mv in montage.MOVES:
            self.assertEqual(montage.validate({"clips": [{"media": "land.jpg", "move": mv}]}, self.root)["clips"][0]["move"], mv)
        with self.assertRaises(montage.RecipeError) as cm:
            montage.validate({"clips": [{"media": "land.jpg", "move": "spin"}]}, self.root)
        self.assertIn("move: must be one of in, out, pan_left, pan_right, still", cm.exception.problems[0])
        for mv in montage.VIDEO_MOVES:
            self.assertEqual(montage.validate({"clips": [{"media": "clip.mp4", "move": mv}]}, self.root)["clips"][0]["move"], mv, "a clip can zoom in or out")
        for mv in ("pan_left", "pan_right", "still"):
            with self.assertRaises(montage.RecipeError) as cm:
                montage.validate({"clips": [{"media": "clip.mp4", "move": mv}]}, self.root)
            self.assertIn("panning and still are for photos", cm.exception.problems[0])

    def test_neighbouring_photos_get_different_moves_unless_told_otherwise(self):
        self.add("land.jpg", "port.jpg", "square.jpg", "clip.mp4")
        p = montage.plan_for(recipe([{"media": "land.jpg"}, {"media": "clip.mp4"}, {"media": "port.jpg"}, {"media": "square.jpg"},
                                     {"media": "land.jpg", "move": "still"}]), self.root)
        moves = [s["move"] for s in p["segments"] if s["kind"] == "photo"]
        self.assertEqual(moves, ["in", "pan_right", "out", "still"], "the cycle counts photos only, and a named move wins")

    def test_zoom_false_still_means_a_still_photo(self):
        self.add("land.jpg")
        p = montage.plan_for(recipe([{"media": "land.jpg", "zoom": False}]), self.root)
        self.assertEqual(p["segments"][0]["move"], "still")

    def test_a_still_does_not_move_and_a_zoom_does(self):
        still, zoom = self.photo("still"), None
        self.root2 = None
        a, b = gray(still, 0.8, self.W, self.H), gray(still, 2.2, self.W, self.H)
        self.assertLess(mean_diff(a, b), 0.6, "nothing moves in a still")
        moving = self.photo("in", name="port.jpg")
        c, d = gray(moving, 0.8, self.W, self.H), gray(moving, 2.2, self.W, self.H)
        self.assertGreater(mean_diff(c, d), 4 * mean_diff(a, b) + 1.0)

    def test_the_zoom_starts_slowly_and_is_fastest_in_the_middle(self):
        """A linear zoom moves at one speed all the way; an eased one barely moves at first. That difference is the polish."""
        with mock.patch.dict(montage.TEMPLATES["clean"], zoom=0.3):         # a big zoom, so the motion stands well above the encoder's noise floor
            out = self.photo("in", hold=3.0)
        start = mean_diff(gray(out, 0.05, self.W, self.H), gray(out, 0.25, self.W, self.H))
        middle = mean_diff(gray(out, 1.4, self.W, self.H), gray(out, 1.6, self.W, self.H))
        end = mean_diff(gray(out, 2.75, self.W, self.H), gray(out, 2.95, self.W, self.H))
        self.assertLess(start, 0.55 * middle, f"slow start: start={start:.2f} middle={middle:.2f}")
        self.assertLess(end, 0.55 * middle, f"gentle settle: end={end:.2f} middle={middle:.2f}")

    def test_a_pan_moves_the_picture_sideways_the_right_way(self):
        self.add("square.jpg")
        shifts = {}
        for mv in ("pan_right", "pan_left"):
            _, out = self.render(recipe([{"media": "square.jpg", "move": mv, "hold": 3}], template="clean", captions=False))
            shifts[mv] = best_shift(gray(out, 0.1, self.W, self.H), gray(out, 2.85, self.W, self.H), self.W, self.H)
        self.assertLessEqual(shifts["pan_right"], -3, "the camera moves right, so the picture slides left")
        self.assertGreaterEqual(shifts["pan_left"], 3, "the camera moves left, so the picture slides right")

    def test_a_caption_glides_up_into_place_and_then_stays_put(self):
        flat = self.fx / "flat.jpg"
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=640x360", "-frames:v", "1", str(flat)], check=True)
        self.add("flat.jpg")
        with mock.patch.dict(montage.FORMATS["reels"], width=432, height=768):
            _, out = self.render(recipe([{"media": "flat.jpg", "caption": "Selamat datang", "move": "still", "hold": 3}], template="clean"))
            def top(at):
                f = gray(out, at, 432, 768)                              # decode the frame once, then scan it
                return next((y for y in range(768) if any(abs(f[y * 432 + x] - 128) > 6 for x in range(150, 280, 4))), None)
            early, settled, later = top(0.35), top(1.0), top(1.5)
        self.assertIsNotNone(early)
        self.assertGreater(early, settled, "it starts lower and rises")
        self.assertEqual(settled, later, "then it stays where it is")

    def test_a_title_card_has_an_accent_line_between_the_title_and_the_subtitle_and_glides_in(self):
        self.add("land.jpg")
        _, out = self.render(recipe([{"media": "land.jpg"}], template="slowmo", title_card={"text": "CyberDSA 2026", "subtitle": "Hari terbuka"}))
        def rgb(at):
            return subprocess.run([FF, "-v", "error", "-ss", str(at), "-i", str(out), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                  capture_output=True, check=True).stdout
        frame = rgb(1.5)
        w = 216
        accent = [(y, x) for y in range(384) for x in range(w)
                  if abs(frame[(y * w + x) * 3] - 232) < 28 and abs(frame[(y * w + x) * 3 + 1] - 184) < 28 and abs(frame[(y * w + x) * 3 + 2] - 106) < 28]
        self.assertGreater(len(accent), 20, "the template's amber accent line is on screen")
        rows = sorted({y for y, _ in accent})
        runs, _ = Rendering.bands(self, out, 1.5)
        (t0, t1), (s0, s1) = runs[0], runs[-1]
        self.assertTrue(t1 < rows[0] and rows[-1] < s0, f"the line sits between the title ({t1}) and the subtitle ({s0}): {rows[0]}-{rows[-1]}")
        cols = sorted({x for _, x in accent})
        self.assertAlmostEqual((cols[0] + cols[-1]) / 2, 108, delta=6, msg="centred")
        early_runs, _ = Rendering.bands(self, out, 0.4, thresh=45)          # half faded in, so dimmer than settled text
        self.assertGreater(early_runs[0][0], runs[0][0], "the title arrives from slightly below")


@NEED
class Draft(Fixtures):
    """A quick draft: half the pixels and none of the slow finishing, but exactly the final's timings."""

    def rec(self, **kw):
        return recipe([{"media": "land.jpg", "caption": "Selamat datang"}, {"media": "port.jpg"}, {"media": "clip.mp4"}],
                      template="slowmo", title_card={"text": "CyberDSA", "subtitle": "Hari terbuka"}, **kw)

    def test_quality_is_final_share_or_draft_and_nothing_else(self):
        self.add("land.jpg")
        self.assertEqual(montage.validate({"clips": [{"media": "land.jpg"}]}, self.root)["quality"], "final")
        self.assertEqual(montage.validate({"clips": [{"media": "land.jpg"}], "quality": "draft"}, self.root)["quality"], "draft")
        with self.assertRaises(montage.RecipeError) as cm:
            montage.validate({"clips": [{"media": "land.jpg"}], "quality": "ultra"}, self.root)
        self.assertIn("quality: must be one of final, share, draft", cm.exception.problems[0])

    def test_a_draft_has_exactly_the_same_timings_as_the_final_at_half_the_size(self):
        self.add("land.jpg", "port.jpg", "clip.mp4")
        final = montage.plan_for(self.rec(), self.root)
        draft = montage.plan_for(self.rec(quality="draft"), self.root)
        strip = lambda p: [(s["kind"], s["start"], s["duration"], s.get("move")) for s in p["segments"]]
        self.assertEqual(strip(draft), strip(final), "what you preview is what you get")
        self.assertEqual((draft["total"], draft["fade"]), (final["total"], final["fade"]))
        self.assertEqual((final["width"], final["height"]), (216, 384))
        self.assertEqual((draft["width"], draft["height"]), (108, 192))
        self.assertEqual((final["quality"], draft["quality"]), ("final", "draft"))

    def test_a_draft_leaves_out_the_slow_finishing_and_encodes_fast(self):
        self.add("land.jpg", "port.jpg", "clip.mp4")
        def commands(rec):
            plan = montage.plan_for(rec, self.root)
            plan["_root"] = str(self.root)
            work = Path(tempfile.mkdtemp())
            out = []
            for i, seg in enumerate(plan["segments"]):
                for cmd, _ in montage._piece_command("/usr/bin/ffmpeg", plan, seg, i, work, work / f"p{i}.mp4"):
                    out.append(" ".join(map(str, cmd)))
            shutil.rmtree(work, ignore_errors=True)
            return "\n".join(out)
        final, draft = commands(self.rec()), commands(self.rec(quality="draft"))
        self.assertIn("noise=alls=6", final)
        self.assertIn("vignette=PI/6", final)
        self.assertIn("-preset veryfast", final)
        self.assertNotIn("noise=", draft)
        self.assertNotIn("vignette", draft)
        self.assertIn("colorbalance", draft, "the colour grade is kept, so the look is still the look")
        self.assertIn("-preset ultrafast", draft)

    def test_a_draft_renders_at_half_size_is_named_and_listed_as_a_draft_and_is_smaller(self):
        self.add("land.jpg", "port.jpg", "clip.mp4", music=["song.m4a"])
        base = self.rec(music={"file": "song.m4a"})
        job_f, out_f = self.render(base)
        job_d, out_d = self.render({**base, "quality": "draft"})
        self.assertEqual((job_d["state"], job_d["quality"], job_f["quality"]), ("done", "draft", "final"), job_d["error"])
        self.assertNotIn("-draft-", out_f.name)
        self.assertIn("-reels-draft-", out_d.name)
        s_f, s_d = stats(out_f), stats(out_d)
        self.assertEqual((s_f["w"], s_f["h"]), (216, 384))
        self.assertEqual((s_d["w"], s_d["h"]), (108, 192))
        self.assertAlmostEqual(s_d["dur"], s_f["dur"], delta=0.1, msg="the same length")
        self.assertEqual(s_d["audio"], "aac", "the music is in the draft too")
        self.assertLess(out_d.stat().st_size, out_f.stat().st_size)
        listed = {e["name"]: e["quality"] for e in montage.exports(self.root)}
        self.assertEqual(listed, {out_f.name: "final", out_d.name: "draft"})

    def test_a_draft_and_a_final_of_the_same_recipe_are_two_jobs_not_one(self):
        self.add("land.jpg")
        base = recipe([{"media": "land.jpg"}], template="punchy")
        with prepare.HEAVY:
            a = montage.RENDERER.submit(self.root, base)
            b = montage.RENDERER.submit(self.root, {**base, "quality": "draft"})
        self.assertNotEqual(a["id"], b["id"])
        montage.RENDERER.wait_idle(120)

    def test_the_title_card_layout_holds_at_draft_size(self):
        self.add("land.jpg")
        with mock.patch.dict(montage.FORMATS["reels"], width=432, height=768):
            _, out = self.render(recipe([{"media": "land.jpg"}], template="punchy", quality="draft",
                                        title_card={"text": "CyberDSA 2026", "subtitle": "Hari terbuka"}))
        runs, _ = Rendering.bands(self, out, 1.2)
        self.assertEqual(len(runs), 2, f"a title band and a subtitle band, still not overlapping: {runs}")


def rgb_frame(path, at):
    return subprocess.run([FF, "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, check=True).stdout


PAPER_RGB = (241, 234, 216)


def px(frame, w, x, y):
    i = (y * w + x) * 3
    return frame[i], frame[i + 1], frame[i + 2]


def near(c, target, tol):
    return all(abs(a - b) <= tol for a, b in zip(c, target))


def count_near(frame, target, tol):
    return sum(1 for i in range(0, len(frame), 3) if near((frame[i], frame[i + 1], frame[i + 2]), target, tol))


def centroid_of_non_paper(frame, w, h, tol=45):
    xs = ys = n = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if not near(px(frame, w, x, y), PAPER_RGB, tol):
                xs, ys, n = xs + x, ys + y, n + 1
    return (xs / n, ys / n, n) if n else (None, None, 0)


@NEED
class Collage(Fixtures):
    """The paper-collage (Vox style) look, measured in pixels."""
    W, H = 432, 768

    def setUp(self):
        super().setUp()
        self._big = mock.patch.dict(montage.FORMATS["reels"], width=self.W, height=self.H)
        self._big.start()

    def tearDown(self):
        self._big.stop()
        super().tearDown()

    def shot(self, name="land.jpg", caption="Selamat datang", move="in", hold=2.5, **kw):
        if name not in {i["name"] for i in montage.catalog(self.root)}:       # a test may take several shots of the same picture
            self.add(name)
        clip = {"media": name, "move": move, "hold": hold}
        if caption:
            clip["caption"] = caption
        _, out = self.render(recipe([clip], template="collage", **kw))
        return out

    def test_the_collage_template_is_listed_and_cuts_hard(self):
        self.assertIn("collage", montage.TEMPLATES)
        self.assertEqual(montage.TEMPLATES["collage"]["fade"], 0.0, "a collage never crossfades")
        self.add("land.jpg", "port.jpg")
        p = montage.plan_for(recipe([{"media": "land.jpg"}, {"media": "port.jpg"}], template="collage"), self.root)
        self.assertEqual((p["fade"], p["total"]), (0, 4.4))
        self.assertEqual([s["move"] for s in p["segments"]], ["in", "out"], "neighbours drop in from different sides")

    def test_the_paper_is_bright_and_flat_not_a_vignetted_grey(self):
        out = self.shot()
        f = rgb_frame(out, 1.4)
        for corner in ((4, 4), (self.W - 5, 4), (4, self.H - 5), (self.W - 5, self.H - 5)):
            self.assertTrue(near(px(f, self.W, *corner), PAPER_RGB, 14), f"{corner}: {px(f, self.W, *corner)} is not the cream paper")

    def test_the_photo_is_a_bordered_cut_out_with_a_shadow_and_tape(self):
        out = self.shot()
        f = rgb_frame(out, 1.4)
        self.assertGreater(count_near(f, (255, 255, 255), 3), 800, "a white border around the photo")
        self.assertGreater(count_near(f, (242, 224, 138), 30), 120, "strips of yellow tape")
        darker = sum(1 for i in range(0, len(f), 3) if f[i] < 226 and abs(f[i] - f[i + 2] - 25) < 14)       # paper-toned but darker: the shadow
        self.assertGreater(darker, 400, "a soft shadow on the paper")

    def test_a_caption_is_a_cream_marker_label_and_no_caption_means_no_label(self):
        with_label = rgb_frame(self.shot(caption="Selamat datang"), 1.4)
        without = rgb_frame(self.shot(caption=None), 1.4)
        label = (255, 250, 240)
        self.assertGreater(count_near(with_label, label, 3), 3000, "a bright label, clearly lighter than the cream paper")
        self.assertLess(count_near(without, label, 3), 300, "none without a caption")
        self.assertGreater(count_near(with_label, (22, 19, 15), 40), 300, "with dark marker lettering on it")
        self.assertLess(count_near(without, (22, 19, 15), 40), 60, "and no lettering without one")
        self.assertGreater(sum(1 for c in (label,) if not near(label, PAPER_RGB, 12)), 0, "the label is not the paper colour")

    def test_the_piece_drops_in_from_the_side_the_move_names(self):
        for move, axis, sign in (("in", 0, +1), ("out", 0, -1), ("pan_right", 1, +1), ("pan_left", 1, -1)):
            f = rgb_frame(self.shot(move=move, caption=None), 0.1)
            cx, cy, n = centroid_of_non_paper(f, self.W, self.H)
            centre = (self.W / 2, self.H / 2)[axis]
            got = (cx, cy)[axis] if n else None
            self.assertIsNotNone(got, f"{move}: nothing on screen yet?")
            self.assertGreater((got - centre) * sign, 8, f"{move}: arrives from the {'+' if sign > 0 else '-'} side of axis {axis}")
            settled = centroid_of_non_paper(rgb_frame(self.out_of_last(), 1.4), self.W, self.H)
            self.assertAlmostEqual(settled[0], self.W / 2, delta=self.W * 0.08, msg=f"{move} settles in the middle")

    def out_of_last(self):
        return sorted((self.root / "exports").glob("*.mp4"))[-1]

    def test_the_card_wobbles_in_steps_about_twelve_times_a_second_not_continuously(self):
        """Stop-motion holds a pose for a few frames and then jumps. Decode a whole second of consecutive frames, follow the
        card's white border, and count the jumps: about 12 means stop-motion, 29 would mean smooth continuous drift."""
        out = self.shot(hold=3.0, caption=None)
        raw = subprocess.run([FF, "-v", "error", "-ss", "1.0", "-i", str(out), "-t", "1.0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        size = self.W * self.H * 3
        frames = [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]
        self.assertGreaterEqual(len(frames), 28)
        centres = []
        for f in frames[:30]:
            xs = ys = n = 0
            for y in range(0, self.H, 3):
                for x in range(0, self.W, 3):
                    i = (y * self.W + x) * 3
                    if f[i] >= 253 and f[i + 1] >= 253 and f[i + 2] >= 253:
                        xs, ys, n = xs + x, ys + y, n + 1
            centres.append((xs / n, ys / n))
        steps = [abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(centres, centres[1:])]
        jumps = sum(1 for d in steps if d > 0.6)
        holds = sum(1 for d in steps if d < 0.25)
        self.assertTrue(5 <= jumps <= 15, f"about 12 jumps a second (some are small), got {jumps}: {[round(d, 1) for d in steps]}")
        self.assertGreaterEqual(holds, 8, f"and it holds still between them ({holds} still frames of {len(steps)}); smooth drift would have almost none")

    def test_a_title_card_is_marker_lettering_on_a_highlighter_with_the_subtitle_right_under_it(self):
        self.add("land.jpg")
        _, out = self.render(recipe([{"media": "land.jpg"}], template="collage", title_card={"text": "CyberDSA 2026", "subtitle": "Hari terbuka"}))
        f = rgb_frame(out, 1.6)
        self.assertGreater(count_near(f, (255, 210, 63), 12), 2500, "a yellow highlighter swipe")
        ink = [y for y in range(self.H) if sum(1 for x in range(0, self.W, 2) if near(px(f, self.W, x, y), (22, 19, 15), 40)) > 2]
        runs = []
        for y in ink:
            if runs and y - runs[-1][1] <= 3:
                runs[-1][1] = y
            else:
                runs.append([y, y])
        self.assertEqual(len(runs), 2, f"title lettering, then subtitle lettering: {runs}")
        gap = runs[1][0] - runs[0][1]
        self.assertLess(gap, self.H * 0.09, f"the subtitle sits right under the title ({gap}px), not far below it")
        self.assertGreater(gap, 2)

    def test_a_clip_gets_the_same_card_and_a_label(self):
        self.add("clip.mp4")
        _, out = self.render(recipe([{"media": "clip.mp4", "caption": "Panel perbincangan"}], template="collage"))
        f = rgb_frame(out, 1.4)
        self.assertGreater(count_near(f, (255, 255, 255), 3), 800, "the clip has the white border")
        self.assertGreater(count_near(f, (242, 224, 138), 30), 120, "and tape")
        self.assertGreater(count_near(f, (246, 239, 220), 3), 3000, "and a label")

    def test_a_collage_draft_keeps_the_look_at_half_size(self):
        out = self.shot(quality="draft")
        s = stats(out)
        self.assertEqual((s["w"], s["h"]), (self.W // 2, self.H // 2))
        f = rgb_frame(out, 1.4)
        self.assertTrue(near(px(f, s["w"], 3, 3), PAPER_RGB, 14), "still cream paper")
        self.assertGreater(count_near(f, (255, 255, 255), 3), 200, "still a bordered photo")

    def test_every_format_renders_as_a_collage(self):
        self.add("land.jpg")
        for fmt in ("square", "wide"):
            with mock.patch.dict(montage.FORMATS[fmt], width={"square": 324, "wide": 576}[fmt], height={"square": 324, "wide": 324}[fmt]):
                job, out = self.render(recipe([{"media": "land.jpg", "caption": "x"}], template="collage", format=fmt))
            self.assertEqual(job["state"], "done", f"{fmt}: {job['error']}")


@NEED
class BeatSync(Fixtures):
    """music.sync = "beat": every cut lands on a beat of the song, or the plan says plainly why it did not."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from tests.test_beats import song
        for name, bpm, off in (("b120.m4a", 120, 0.13), ("b80.m4a", 80, 0.2), ("b97.m4a", 97, 0.05)):
            wav = cls.fx / (name + ".wav")
            song(wav, bpm, seconds=40, offset=off)
            ff("-i", str(wav), "-c:a", "aac", "-ar", "48000", str(cls.fx / name))

    def plan(self, clips, **kw):
        return montage.plan_for(recipe(clips, **kw), self.root)

    def off_grid(self, p):
        """Largest distance (seconds) of any cut from a beat of the plan's music, in video time."""
        period = 60.0 / p["sync"]["bpm"]
        worst = 0.0
        for s in p["segments"]:
            d = s["start"] % period
            worst = max(worst, min(d, period - d))
        return worst

    def test_every_cut_lands_on_a_beat_and_the_music_starts_on_one(self):
        self.add("land.jpg", "port.jpg", "square.jpg", "clip.mp4", music=["b120.m4a"])
        p = self.plan([{"media": n} for n in ("land.jpg", "port.jpg", "square.jpg", "clip.mp4")], template="punchy",
                      title_card={"text": "Hi"}, end_card={"text": "Bye"}, music={"file": "b120.m4a", "sync": "beat"})
        self.assertAlmostEqual(p["sync"]["bpm"], 120, delta=1.5)
        self.assertEqual(p["sync"]["beats_per_cut"], 2)
        self.assertEqual(p["fade"], 0.0)
        self.assertAlmostEqual(p["music"]["start"], 0.13, delta=0.015, msg="the song starts on its first beat")
        self.assertLess(self.off_grid(p), 1 / p["fps"] + 1e-6)
        for s in p["segments"]:
            n = s["duration"] / p["sync"]["cut_seconds"]
            self.assertLess(abs(n - round(n)), 1.5 / p["fps"] / p["sync"]["cut_seconds"], f"{s['kind']} lasts a whole number of cuts")

    def test_a_fading_template_is_forced_to_hard_cuts_when_synced(self):
        self.add("land.jpg", "port.jpg", music=["b120.m4a"])
        free = self.plan([{"media": "land.jpg"}, {"media": "port.jpg"}], template="slowmo", music={"file": "b120.m4a"})
        self.assertGreater(free["fade"], 0)
        self.assertIsNone(free["sync"])
        p = self.plan([{"media": "land.jpg"}, {"media": "port.jpg"}], template="slowmo", music={"file": "b120.m4a", "sync": "beat"})
        self.assertEqual(p["fade"], 0.0)
        self.assertLess(self.off_grid(p), 1 / p["fps"] + 1e-6)

    def test_cuts_stay_on_the_beat_over_a_long_video_at_an_awkward_tempo(self):
        names = ["land.jpg", "port.jpg", "square.jpg"] * 6
        self.add("land.jpg", "port.jpg", "square.jpg", music=["b97.m4a"])
        p = self.plan([{"media": n} for n in names], template="punchy", max_seconds=90, music={"file": "b97.m4a", "sync": "beat"})
        self.assertEqual(len(p["segments"]), 18)
        self.assertLess(self.off_grid(p), 1 / p["fps"] + 1e-6, "no drift from rounding to frames")
        for s in p["segments"]:
            self.assertAlmostEqual(s["start"] * p["fps"], round(s["start"] * p["fps"]), delta=0.03, msg="every cut is on a whole frame")

    def test_a_clip_shorter_than_a_cut_is_slowed_to_fill_it_and_a_long_one_is_trimmed_to_whole_cuts(self):
        self.add("short.mp4", "clip.mp4", music=["b80.m4a"])
        p = self.plan([{"media": "short.mp4"}, {"media": "clip.mp4"}], template="punchy", music={"file": "b80.m4a", "sync": "beat"})
        short, clip = p["segments"]
        unit = p["sync"]["cut_seconds"]
        self.assertAlmostEqual(unit, 1.5, delta=0.02)
        self.assertAlmostEqual(short["duration"], unit, delta=1 / p["fps"])
        self.assertLess(short["speed"], 1.0)
        self.assertAlmostEqual(short["src_used"] / short["speed"], short["duration"], delta=0.05, msg="the source fits the slot exactly")
        self.assertTrue(any("slowed" in w for w in p["warnings"]))
        self.assertLessEqual(clip["src_used"], 4.0 + 1e-6)
        self.assertAlmostEqual(clip["src_used"] / clip["speed"], clip["duration"], delta=0.05)

    def test_no_steady_beat_keeps_the_pacing_and_says_so(self):
        self.add("land.jpg", "port.jpg", music=["song.m4a"])                     # a plain sine tone: nothing to cut to
        free = self.plan([{"media": "land.jpg"}, {"media": "port.jpg"}], template="punchy", music={"file": "song.m4a"})
        p = self.plan([{"media": "land.jpg"}, {"media": "port.jpg"}], template="punchy", music={"file": "song.m4a", "sync": "beat"})
        self.assertIsNone(p["sync"])
        self.assertTrue(any("beat" in w for w in p["warnings"]), p["warnings"])
        self.assertEqual([s["duration"] for s in p["segments"]], [s["duration"] for s in free["segments"]])
        self.assertEqual(p["music"]["start"], 0)

    def test_without_the_option_nothing_changes(self):
        self.add("land.jpg", music=["b120.m4a"])
        p = self.plan([{"media": "land.jpg"}], template="punchy", music={"file": "b120.m4a"})
        self.assertIsNone(p["sync"])
        self.assertEqual(p["music"]["start"], 0)

    def test_sync_needs_music_and_only_knows_beat(self):
        self.add("land.jpg", music=["b120.m4a"])
        p = self.plan([{"media": "land.jpg"}], template="punchy", music=None)
        self.assertIsNone(p["sync"])
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan([{"media": "land.jpg"}], music={"file": "b120.m4a", "sync": "tempo"})
        self.assertIn("music.sync", str(ctx.exception))

    def test_a_stated_tempo_cuts_on_that_grid_from_the_first_frame_with_no_audio(self):
        self.add("land.jpg", "port.jpg", "square.jpg", "clip.mp4")
        p = self.plan([{"media": n} for n in ("land.jpg", "port.jpg", "square.jpg", "clip.mp4")], template="punchy",
                      title_card={"text": "Hi"}, music={"bpm": 75})
        self.assertIsNone(p["music"], "no audio: the song is added later in the app")
        self.assertTrue(p["sync"]["tempo_only"])
        self.assertEqual(p["sync"]["bpm"], 75.0)
        self.assertAlmostEqual(p["sync"]["cut_seconds"], 1.6, delta=0.001, msg="two beats at 75 BPM")
        self.assertEqual(p["fade"], 0.0)
        for s in p["segments"]:
            self.assertAlmostEqual(s["start"] % 0.8, 0, delta=1 / p["fps"] + 1e-6) if s["start"] % 0.8 < 0.4 else self.assertAlmostEqual(s["start"] % 0.8, 0.8, delta=1 / p["fps"] + 1e-6)
        self.assertEqual(p["segments"][0]["start"], 0)

    def test_a_tempo_needs_a_sane_number_and_cannot_also_name_a_song(self):
        self.add("land.jpg", music=["b120.m4a"])
        for bad in ({"bpm": 20}, {"bpm": 400}, {"bpm": "fast"}, {"bpm": 75, "file": "b120.m4a"}):
            with self.subTest(bad):
                with self.assertRaises(montage.RecipeError) as ctx:
                    self.plan([{"media": "land.jpg"}], music=bad)
                self.assertIn("music", str(ctx.exception))

    def test_a_tempo_only_video_renders_silent_at_the_planned_length(self):
        self.add("land.jpg", "port.jpg")
        job, out = self.render(recipe([{"media": "land.jpg"}, {"media": "port.jpg"}], template="punchy", quality="draft", music={"bpm": 90}))
        self.assertEqual(job["state"], "done", job["error"])
        st = stats(out)
        self.assertIsNone(st["audio"])
        self.assertAlmostEqual(st["dur"], job["total"], delta=0.1)

    def test_a_synced_video_renders_with_its_song_at_the_planned_length(self):
        self.add("land.jpg", "port.jpg", "clip.mp4", music=["b120.m4a"])
        job, out = self.render(recipe([{"media": "land.jpg"}, {"media": "port.jpg"}, {"media": "clip.mp4"}], template="punchy",
                                      quality="draft", music={"file": "b120.m4a", "sync": "beat"}))
        self.assertEqual(job["state"], "done", job["error"])
        st = stats(out)
        self.assertEqual(st["audio"], "aac")
        self.assertAlmostEqual(st["dur"], job["total"], delta=0.1)


NEED_SAY = unittest.skipUnless(shutil.which("say"), "needs macOS `say` for real speech")


@NEED
@NEED_SAY
class SpeechFollow(Fixtures):
    """speech = "follow": a long clip's excerpt starts where people speak, not at second 0."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        subprocess.run(["say", "-o", str(cls.fx / "talk.aiff"), "Selamat datang ke hari terbuka CyberDSA dua ribu dua puluh enam. Terima kasih kerana hadir."], check=True)
        ff("-i", str(cls.fx / "talk.aiff"), "-ar", "48000", "-ac", "1", str(cls.fx / "talk.wav"))
        ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "6", str(cls.fx / "quiet6.wav"))
        ff("-i", str(cls.fx / "quiet6.wav"), "-i", str(cls.fx / "talk.wav"), "-filter_complex", "concat=n=2:v=0:a=1", str(cls.fx / "late.wav"))
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:d=12.2", "-i", str(cls.fx / "late.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(cls.fx / "speaker.mp4"))                       # 6 s of silence, then the talk
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:d=12", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-c:v", "libx264",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "12", str(cls.fx / "silent.mp4"))

    def plan(self, clips, **kw):
        return montage.plan_for(recipe(clips, template="punchy", **kw), self.root)

    def test_the_excerpt_starts_where_the_talking_does(self):
        self.add("speaker.mp4")
        p = self.plan([{"media": "speaker.mp4"}], speech="follow")
        seg = p["segments"][0]
        self.assertAlmostEqual(seg["src_start"], 6.0 - 0.3, delta=0.5)
        self.assertGreater(seg["speech"], 1.5, "almost all of the 2 s excerpt is speech")
        self.assertEqual(p["recipe"]["speech"], "follow")

    def test_without_the_option_the_clip_starts_at_the_beginning_as_before(self):
        self.add("speaker.mp4")
        seg = self.plan([{"media": "speaker.mp4"}])["segments"][0]
        self.assertEqual(seg["src_start"], 0)
        self.assertNotIn("speech", seg)

    def test_an_explicit_start_is_the_authors_call(self):
        self.add("speaker.mp4")
        seg = self.plan([{"media": "speaker.mp4", "start": 1.0}], speech="follow")["segments"][0]
        self.assertEqual(seg["src_start"], 1.0)
        self.assertEqual(seg["speech"], 0.0, "the plan still says there is no speech in the part used")

    def test_a_clip_with_nothing_to_follow_is_left_alone(self):
        self.add("silent.mp4", "clip.mp4")                                   # silence, and a clip with no audio track at all
        for name in ("silent.mp4", "clip.mp4"):
            with self.subTest(name):
                seg = self.plan([{"media": name}], speech="follow")["segments"][0]
                self.assertEqual(seg["src_start"], 0)
                self.assertEqual(seg["speech"], 0.0)

    def test_photos_are_not_affected_and_other_values_are_refused(self):
        self.add("land.jpg", "speaker.mp4")
        p = self.plan([{"media": "land.jpg"}, {"media": "speaker.mp4"}], speech="follow")
        self.assertNotIn("speech", p["segments"][0])
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan([{"media": "speaker.mp4"}], speech="loud")
        self.assertIn("speech", str(ctx.exception))

    # ---------------------------------------------------------------- keeping the clips' own sound
    def test_only_a_clip_with_speech_in_its_excerpt_keeps_its_sound_and_the_music_dips_under_it(self):
        self.add("speaker.mp4", "silent.mp4", "clip.mp4", music=["song.m4a"])
        p = self.plan([{"media": "speaker.mp4"}, {"media": "silent.mp4"}, {"media": "clip.mp4"}], speech="follow", clip_sound="speech",
                      music={"file": "song.m4a"})
        talker, quiet, mute = p["segments"]
        self.assertTrue(talker["sound"])
        self.assertNotIn("sound", quiet, "a quiet clip stays muted")
        self.assertNotIn("sound", mute, "a clip with no sound track stays muted")
        self.assertTrue(p["duck"])
        for a, b in p["duck"]:
            self.assertGreaterEqual(a, talker["start"] - 1e-6)
            self.assertLessEqual(b, talker["start"] + talker["duration"] + 1e-6)
        self.assertEqual(p["recipe"]["clip_sound"], "speech")

    def test_without_the_option_no_clip_sound_is_kept(self):
        self.add("speaker.mp4")
        p = self.plan([{"media": "speaker.mp4"}], speech="follow")
        self.assertNotIn("sound", p["segments"][0])
        self.assertEqual(p["duck"], [])

    def test_clip_sound_works_without_following_and_an_unknown_value_is_refused(self):
        self.add("speaker.mp4")
        p = self.plan([{"media": "speaker.mp4", "start": 5.0, "hold": 4}], clip_sound="speech")
        self.assertTrue(p["segments"][0]["sound"], "the excerpt from 5 s holds the talk that starts at 6 s")
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan([{"media": "speaker.mp4"}], clip_sound="all")
        self.assertIn("clip_sound", str(ctx.exception))

    def test_the_music_really_is_about_14_db_quieter_inside_the_dip_and_comes_back(self):
        self.add("silent.mp4", music=["song.m4a"])
        p = self.plan([{"media": "silent.mp4", "hold": 8}], music={"file": "song.m4a"}, clip_sound="speech")
        p["segments"][0]["sound"] = True                                           # a silent clip stands in for the voice
        p["duck"] = [[3.0, 4.0]]
        work = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(work, ignore_errors=True))
        out = montage._audio_mix(FF, p, self.root, work, montage.Control())

        def level(a, b):
            r = subprocess.run([FF, "-hide_banner", "-ss", str(a), "-t", str(b - a), "-i", str(out), "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True).stderr
            return float(re.search(r"mean_volume: (-?[\d.]+)", r).group(1))
        before, inside, after = level(0.8, 1.8), level(3.2, 3.8), level(5.5, 6.5)
        self.assertAlmostEqual(before - inside, 14, delta=3, msg=f"{before} vs {inside}")
        self.assertAlmostEqual(before, after, delta=1.0, msg="the music comes back at full level")

    def test_a_video_with_kept_sound_renders_with_audio_even_without_music(self):
        self.add("speaker.mp4", "land.jpg")
        job, out = self.render(recipe([{"media": "speaker.mp4"}, {"media": "land.jpg"}], template="punchy", quality="draft",
                                      speech="follow", clip_sound="speech"))
        self.assertEqual(job["state"], "done", job["error"])
        st = stats(out)
        self.assertEqual(st["audio"], "aac")
        self.assertAlmostEqual(st["dur"], job["total"], delta=0.15)
        adur = float(subprocess.run([prepare.find_tool("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(out)],
                                    capture_output=True, text=True).stdout.strip())
        self.assertAlmostEqual(adur, job["total"], delta=0.2, msg="the audio runs as long as the video, not just as long as the talking clip")
        r = subprocess.run([FF, "-hide_banner", "-ss", "0.2", "-t", "1.5", "-i", str(out), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
        self.assertGreater(float(re.search(r"mean_volume: (-?[\d.]+)", r).group(1)), -45, "the speech is audible in the first seconds")

    def test_a_followed_clip_renders(self):
        self.add("speaker.mp4", "land.jpg")
        job, out = self.render(recipe([{"media": "speaker.mp4"}, {"media": "land.jpg"}], template="punchy", quality="draft", speech="follow"))
        self.assertEqual(job["state"], "done", job["error"])
        self.assertAlmostEqual(stats(out)["dur"], job["total"], delta=0.1)


@NEED
class Kinetic(Fixtures):
    """The kinetic-type look: words pop in one after another, the last in the accent colour, all inside the frame."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ff("-f", "lavfi", "-i", "color=c=0x202020:s=640x360", "-frames:v", "1", str(cls.fx / "dark.jpg"))   # so any bright pixel is text

    def counts(self, path, at, w=216, h=384):
        """(white-ish pixels, accent-yellow pixels, white-ish pixels in the outer 3% of the width) in the frame at `at` seconds."""
        d = frame(path, at, w, h)
        white = accent = edge = 0
        margin = max(2, int(w * 0.03))
        for i in range(0, len(d), 3):
            r, g, b = d[i], d[i + 1], d[i + 2]
            x = (i // 3) % w
            if r > 200 and g > 200 and b > 200:
                white += 1
                if x < margin or x >= w - margin:
                    edge += 1
            elif r > 200 and g > 170 and b < 130:
                accent += 1
        return white, accent, edge

    def make(self, caption, hold=3.0, **kw):
        self.add("dark.jpg")
        captions.set_detail(self.root, self.sha("dark.jpg"), caption)
        job, out = self.render(recipe([{"media": "dark.jpg", "hold": hold}], template="kinetic", **kw))
        self.assertEqual(job["state"], "done", job["error"])
        return job, out

    def test_the_words_arrive_one_after_another_and_the_last_is_in_the_accent_colour(self):
        job, out = self.make("Dua hari satu")
        before, mid, later, end = (self.counts(out, t) for t in (0.02, 0.45, 0.95, 2.0))
        self.assertEqual(before[0], 0, "nothing is on screen before the first word")
        self.assertGreater(mid[0], 0, "the first word has arrived")
        self.assertGreater(later[0], mid[0], "a second word has arrived")
        self.assertEqual((before[1], mid[1], later[1]), (0, 0, 0), "the accent colour is only for the last word, which has not arrived yet")
        self.assertGreater(end[1], 50, "the third and last word arrives in the accent colour")
        self.assertEqual(end[0], later[0], "so it adds accent pixels, not white ones")

    def test_no_word_touches_the_edge_even_for_a_long_caption(self):
        job, out = self.make("Hackathon dibuka oleh Dekan hari ini dengan penuh semangat", hold=4.0)
        white, accent, edge = self.counts(out, 3.6)
        self.assertGreater(white + accent, 100)
        self.assertEqual(edge, 0, "text is kept off the outer 3% of the frame")

    def vertical_centre(self, path, at, w=216, h=384):
        """Mean height (0 top, 1 bottom) of the white-ish pixels, and the lowest such row, as fractions of the frame."""
        d = frame(path, at, w, h)
        rows = [(i // 3) // w for i in range(0, len(d), 3) if d[i] > 200 and d[i + 1] > 200 and d[i + 2] > 200]
        return sum(rows) / len(rows) / h, max(rows) / h

    def test_picture_captions_sit_low_so_they_do_not_cover_faces_and_never_leave_the_frame(self):
        job, out = self.make("Dua hari satu cabaran lagi", hold=3.5)
        mean, lowest = self.vertical_centre(out, 3.2)
        self.assertGreater(mean, 0.55, "over the lower part of the picture")
        self.assertLessEqual(lowest, kinetic.LOWEST + 0.02, "and not off the bottom")

    def test_a_tall_block_is_pushed_up_not_clipped_at_the_bottom(self):
        job, out = self.make("Hackathon dibuka oleh Dekan hari ini dengan penuh semangat", hold=4.0)
        mean, lowest = self.vertical_centre(out, 3.7)
        self.assertLessEqual(lowest, kinetic.LOWEST + 0.02)

    def test_captions_off_means_no_words(self):
        job, out = self.make("Dua hari satu", captions=False)
        self.assertEqual(self.counts(out, 2.0)[:2], (0, 0))

    def test_a_title_card_has_words_and_a_subtitle_and_a_draft_has_the_same_timings(self):
        self.add("dark.jpg")
        rec = recipe([{"media": "dark.jpg", "hold": 2}], template="kinetic", title_card={"text": "Hari Terbuka", "subtitle": "Universiti Malaya"},
                     end_card={"text": "Jumpa lagi"})
        job, out = self.render(rec)
        self.assertEqual(job["state"], "done", job["error"])
        white, accent, _ = self.counts(out, 2.2)
        self.assertGreater(white, 100, "the title's words and its subtitle are on screen by the end of the title card")
        self.assertGreater(accent, 50)
        final = montage.plan_for(rec, self.root)
        draft = montage.plan_for({**rec, "quality": "draft"}, self.root)
        self.assertEqual([(s["start"], s["duration"]) for s in final["segments"]], [(s["start"], s["duration"]) for s in draft["segments"]])

    def test_it_renders_in_every_format_and_cuts_hard(self):
        self.add("dark.jpg")
        captions.set_detail(self.root, self.sha("dark.jpg"), "Dua hari satu cabaran")
        self.assertEqual(montage.plan_for(recipe([{"media": "dark.jpg"}, {"media": "dark.jpg"}], template="kinetic"), self.root)["fade"], 0.0)
        for fmt, size in (("square", 324), ("wide", 576)):
            with mock.patch.dict(montage.FORMATS[fmt], width=size, height=324):
                job, out = self.render(recipe([{"media": "dark.jpg"}], template="kinetic", format=fmt))
            self.assertEqual(job["state"], "done", f"{fmt}: {job['error']}")

    def test_on_a_tempo_the_words_land_on_the_beat_grid(self):
        self.add("dark.jpg")
        captions.set_detail(self.root, self.sha("dark.jpg"), "Satu dua tiga")
        job, out = self.render(recipe([{"media": "dark.jpg"}], template="kinetic", music={"bpm": 75}))
        self.assertEqual(job["state"], "done", job["error"])
        p = montage.plan_for(recipe([{"media": "dark.jpg"}], template="kinetic", music={"bpm": 75}), self.root)
        self.assertEqual(p["sync"]["bpm"], 75.0)
        half = 60 / 75 / 2                                                  # three words in a 1.6 s piece: half-beats, still on the grid
        times = kinetic.schedule(3, p["segments"][0]["duration"], 60 / 75)
        self.assertEqual(times[0], 0.0)
        self.assertTrue(all(abs(round(t / half) * half - t) < 0.01 for t in times), times)
        self.assertGreater(self.counts(out, p["segments"][0]["duration"] - 0.1)[0], 100, "all three words are up by the end")


@NEED
class TextCards(Fixtures):
    """Quote, announcement and countdown pieces: no picture, every look."""

    def plan(self, clips, **kw):
        return montage.plan_for(recipe(clips, **kw), self.root)

    def problems(self, clips, **kw):
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan(clips, **kw)
        return " | ".join(ctx.exception.problems)

    # ---------------------------------------------------------------- validation
    def test_a_clip_is_exactly_one_of_a_picture_a_quote_an_announcement_or_a_countdown(self):
        self.add("land.jpg")
        self.assertIn("exactly one", self.problems([{"hold": 2}]))
        self.assertIn("exactly one", self.problems([{"media": "land.jpg", "quote": "Hi"}]))
        self.assertIn("exactly one", self.problems([{"quote": "Hi", "countdown": 3}]))

    def test_a_quote_is_checked(self):
        for bad, why in (({"quote": ""}, "quote"), ({"quote": "x" * 161}, "quote"), ({"quote": 5}, "quote"), ({"quote": "Hi", "by": "y" * 61}, "by"),
                         ({"quote": "Hi", "caption": "no"}, "caption"), ({"quote": "Hi", "move": "in"}, "move"), ({"quote": "Hi", "hold": 99}, "hold")):
            with self.subTest(bad=bad):
                self.assertIn(why, self.problems([bad]))
        self.assertEqual(self.plan([{"quote": "x" * 160, "by": "y" * 60}])["segments"][0]["kind"], "quote")

    def test_an_announcement_is_checked(self):
        self.assertIn("title", self.problems([{"announce": {"date": "12 Oct"}}]))
        self.assertIn("unknown field", self.problems([{"announce": {"title": "T", "venue": "Hall"}}]))
        self.assertIn("announce", self.problems([{"announce": "Hackathon"}]))
        self.assertIn("place", self.problems([{"announce": {"title": "T", "place": "p" * 61}}]))
        self.assertIn("title", self.problems([{"announce": {"title": "t" * 81}}]))

    def test_a_countdown_is_a_whole_number_from_2_to_5(self):
        for bad in (1, 6, "3", True, 2.5, None):
            with self.subTest(bad=bad):
                self.assertIn("countdown", self.problems([{"countdown": bad}]))
        for ok in (2, 3, 5):
            self.assertEqual(self.plan([{"countdown": ok}])["segments"][0]["count"], ok)

    def test_by_belongs_to_a_quote_only(self):
        self.add("land.jpg")
        self.assertIn("by", self.problems([{"announce": {"title": "T"}, "by": "x"}]))
        self.assertIn("by", self.problems([{"media": "land.jpg", "by": "x"}]))

    # ---------------------------------------------------------------- the plan
    def test_how_long_each_card_lasts_and_what_it_says(self):
        p = self.plan([{"quote": "Satu dua tiga empat"}, {"quote": " ".join(["kata"] * 20)}, {"quote": "Pendek", "hold": 5, "by": " Dr M "},
                       {"announce": {"title": " Hari Terbuka ", "date": "12 Okt", "time": "9 pagi", "place": "Dewan A", "note": ""}}, {"countdown": 3}],
                      max_seconds=90)
        quote, long_quote, held, announce, count = p["segments"]
        self.assertEqual((quote["kind"], quote["text"], quote["duration"]), ("quote", "Satu dua tiga empat", 3.0))
        self.assertEqual(long_quote["duration"], 8.0, "capped")
        self.assertEqual((held["duration"], held["by"]), (5.0, "Dr M"))
        self.assertEqual((announce["text"], announce["duration"]), ("Hari Terbuka", 4.5), "3 lines: 3 s + half a second each")
        self.assertEqual(announce["lines"], ["\U0001F4C5 12 Okt", "\U0001F558 9 pagi", "\U0001F4CD Dewan A"], "empty fields are left out")
        self.assertEqual((count["kind"], count["text"], count["duration"]), ("countdown", "3-2-1", 2.4))
        self.assertEqual(p["fade"], 0.0 if p["template"] != "slowmo" else p["fade"])

    def test_cards_mix_with_pictures_in_the_order_given(self):
        self.add("land.jpg", "port.jpg")
        p = self.plan([{"countdown": 3}, {"media": "land.jpg"}, {"quote": "Hi", "by": "Me"}, {"media": "port.jpg"}], title_card={"text": "T"}, end_card={"text": "E"})
        self.assertEqual([s["kind"] for s in p["segments"]], ["title", "countdown", "photo", "quote", "photo", "end"])
        self.assertTrue(all(s["start"] >= 0 for s in p["segments"]))

    def test_a_card_can_be_dropped_to_fit_the_limit_and_the_warning_names_it(self):
        p = self.plan([{"quote": "Pertama " + "x " * 20}, {"quote": "Kedua " + "x " * 20}, {"quote": "Ketiga " + "x " * 20}], max_seconds=10)
        self.assertGreaterEqual(len(p["segments"]), 1)
        self.assertLess(len(p["segments"]), 3)
        self.assertTrue(any("left out" in w and "Ketiga" in w for w in p["warnings"]), p["warnings"])
        self.assertLessEqual(p["total"], 10 + 1e-6)

    def test_a_countdown_cut_to_a_tempo_lasts_a_whole_number_of_beat_cuts(self):
        p = self.plan([{"countdown": 3}, {"quote": "Hi"}], template="punchy", music={"bpm": 75})
        unit = p["sync"]["cut_seconds"]
        for s in p["segments"]:
            self.assertAlmostEqual(s["duration"] / unit, round(s["duration"] / unit), delta=0.02, msg=s["kind"])

    def test_the_preview_shows_cards_by_kind(self):
        from umcares import web
        p = self.plan([{"quote": "Hi"}, {"countdown": 3}])
        shown = [{k: s.get(k) for k in ("kind", "name", "text", "duration", "start", "speech", "sound")} for s in p["segments"]]
        self.assertEqual([x["kind"] for x in shown], ["quote", "countdown"])
        self.assertEqual([x["text"] for x in shown], ["Hi", "3-2-1"])

    # ---------------------------------------------------------------- the pictures they make
    def count(self, path, at, test, w=216, h=384):
        d = frame(path, at, w, h)
        return sum(1 for i in range(0, len(d), 3) if test(d[i], d[i + 1], d[i + 2]))

    WHITE = staticmethod(lambda r, g, b: r > 245 and g > 245 and b > 245)
    DARK = staticmethod(lambda r, g, b: r < 90 and g < 90 and b < 90)
    YELLOW = staticmethod(lambda r, g, b: r > 230 and g > 200 and b < 120)
    MUSTARD = staticmethod(lambda r, g, b: 110 < r < 200 and 90 < g < 170 and b < 90)

    def render_cards(self, clips, template, **kw):
        job, out = self.render(recipe(clips, template=template, **kw))
        self.assertEqual(job["state"], "done", job["error"])
        return out

    def test_a_quote_on_a_dark_look_is_white_with_its_attribution_in_the_accent_arriving_last(self):
        out = self.render_cards([{"quote": "Keselamatan siber bermula hari ini", "by": "Dr Madihah"}], "kinetic")
        self.assertEqual(self.count(out, 0.02, self.WHITE), 0, "nothing before it fades in")
        body = self.count(out, 1.2, self.WHITE)
        self.assertGreater(body, 150, "the quote is up")
        self.assertGreater(self.count(out, 2.8, self.YELLOW), 20, "the attribution and the opening mark are in the accent")

    def test_a_quote_on_the_light_collage_look_is_dark_type_never_white_type(self):
        out = self.render_cards([{"quote": "Keselamatan siber bermula hari ini", "by": "Dr Madihah"}], "collage")
        self.assertGreater(self.count(out, 2.8, self.DARK), 150, "dark type on the cream paper")
        self.assertEqual(self.count(out, 2.8, self.WHITE), 0, "no white type on a light background")
        self.assertGreater(self.count(out, 2.8, self.MUSTARD), 20, "the accent is darkened to stay readable on cream")
        self.assertEqual(self.count(out, 2.8, self.YELLOW), 0, "the raw highlighter yellow is not used as text on cream")

    def test_an_announcement_shows_its_title_then_its_chips_one_after_another(self):
        out = self.render_cards([{"announce": {"title": "Hackathon CyberDSA", "date": "12 Okt", "time": "9 pagi", "place": "Dewan A"}, "hold": 5}], "kinetic")
        early, middle, late = (self.count(out, t, self.YELLOW) for t in (0.5, 1.4, 2.4))        # the chips arrive at about 1.0, 1.3 and 1.6 s
        self.assertGreater(self.count(out, 4.6, self.WHITE), 150, "the title words are up")
        self.assertLess(early, middle, "chips arrive after the title")
        self.assertLess(middle, late, "and one after another")

    def test_a_countdown_shows_each_number_in_turn_and_the_last_in_the_accent(self):
        out = self.render_cards([{"countdown": 3}], "kinetic")
        first, second, third = (self.count(out, t, self.WHITE) for t in (0.3, 1.2, 1.9))
        self.assertGreater(first, 100)
        self.assertGreater(second, 100)
        self.assertEqual(self.count(out, 0.3, self.YELLOW), 0)
        self.assertGreater(self.count(out, 1.95, self.YELLOW), 100, "the 1 is in the accent")
        self.assertEqual(self.count(out, 1.95, self.WHITE), 0, "and only one number is on screen at a time")

    def test_every_look_and_format_draws_all_three_cards_and_a_draft_has_the_same_timings(self):
        clips = [{"quote": "Hi", "by": "Me"}, {"announce": {"title": "T", "date": "d"}}, {"countdown": 2}]
        for tpl in montage.TEMPLATES:
            with self.subTest(template=tpl):
                job, out = self.render(recipe(clips, template=tpl, quality="draft"))
                self.assertEqual(job["state"], "done", f"{tpl}: {job['error']}")
        for fmt, size in (("square", 324), ("wide", 576)):
            with mock.patch.dict(montage.FORMATS[fmt], width=size, height=324):
                job, out = self.render(recipe(clips, format=fmt, quality="draft"))
            self.assertEqual(job["state"], "done", f"{fmt}: {job['error']}")
        final = montage.plan_for(recipe(clips, template="kinetic"), self.root)
        draft = montage.plan_for(recipe(clips, template="kinetic", quality="draft"), self.root)
        self.assertEqual([(s["start"], s["duration"]) for s in final["segments"]], [(s["start"], s["duration"]) for s in draft["segments"]])


@NEED
class Audiogram(Fixtures):
    """A song or voice clip drawn as a moving waveform, its sound always heard, the music dipping under it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ff("-f", "lavfi", "-i", "sine=frequency=440:duration=2,volume=3", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-filter_complex",
           "[0:a]aresample=48000[a];[1:a]atrim=0:2[b];[a][b]concat=n=2:v=0:a=1", "-c:a", "aac", str(cls.fx / "gap.m4a"))        # 2 s of tone, 2 s of silence
        ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "4", "-c:a", "aac", str(cls.fx / "hush.m4a"))
        ff("-f", "lavfi", "-i", "sine=frequency=330:duration=0.7", "-c:a", "aac", "-ar", "48000", str(cls.fx / "blip.m4a"))

    def plan(self, clips, **kw):
        return montage.plan_for(recipe(clips, template="kinetic", **kw), self.root)

    def problems(self, clips, **kw):
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan(clips, **kw)
        return " | ".join(ctx.exception.problems)

    # ---------------------------------------------------------------- validation
    def test_the_audio_must_be_audio_and_the_image_an_image(self):
        self.add("land.jpg", "clip.mp4", "gap.m4a")
        self.assertIn("audiogram", self.problems([{"audiogram": "land.jpg"}]))
        self.assertIn("audiogram", self.problems([{"audiogram": "clip.mp4"}]))
        self.assertIn("image", self.problems([{"audiogram": "gap.m4a", "image": "gap.m4a"}]))
        self.assertIn("image", self.problems([{"audiogram": "gap.m4a", "image": "clip.mp4"}]))
        self.assertIn("audiogram", self.problems([{"audiogram": "missing.m4a"}]))

    def test_a_clip_is_exactly_one_kind_and_only_an_audiogram_has_an_image(self):
        self.add("land.jpg", "gap.m4a")
        self.assertIn("exactly one", self.problems([{"audiogram": "gap.m4a", "media": "land.jpg"}]))
        self.assertIn("exactly one", self.problems([{"audiogram": "gap.m4a", "quote": "Hi"}]))
        self.assertIn("only an audiogram has an image", self.problems([{"media": "land.jpg", "image": "land.jpg"}]))
        self.assertIn("only an audiogram has an image", self.problems([{"quote": "Hi", "image": "land.jpg"}]))

    def test_the_fields_an_audiogram_does_not_take_are_refused_and_the_ranges_are_checked(self):
        self.add("gap.m4a")
        for k, v in (("speed", 1.5), ("zoom", True), ("move", "in"), ("by", "x")):
            with self.subTest(k=k):
                self.assertIn(k, self.problems([{"audiogram": "gap.m4a", k: v}]))
        self.assertIn("hold", self.problems([{"audiogram": "gap.m4a", "hold": 99}]))
        self.assertIn("start", self.problems([{"audiogram": "gap.m4a", "start": -1}]))
        self.assertIn("caption", self.problems([{"audiogram": "gap.m4a", "caption": "x" * 201}]))

    # ---------------------------------------------------------------- the plan
    def test_an_audiogram_lasts_as_long_as_asked_or_as_its_audio_and_its_sound_is_always_kept(self):
        self.add("gap.m4a", "land.jpg")
        p = self.plan([{"audiogram": "gap.m4a"}, {"audiogram": "gap.m4a", "start": 1.0, "hold": 2, "caption": "Dekan"}])
        whole, part = p["segments"]
        self.assertEqual((whole["kind"], whole["sound"], whole["src_start"], whole["duration"]), ("audiogram", True, 0.0, whole["src_used"]))
        self.assertAlmostEqual(whole["duration"], 4.0, delta=0.15, msg="the whole of a 4 s clip (the default hold of 8 s is capped by the audio)")
        self.assertEqual((part["src_start"], part["duration"], part["text"], part["speed"]), (1.0, 2.0, "Dekan", 1.0))
        self.assertEqual(part["name"], "gap.m4a")
        self.assertTrue(part["rel"].endswith("gap.m4a"))

    def test_a_start_past_the_end_is_refused_with_the_files_name(self):
        self.add("gap.m4a")
        with self.assertRaises(montage.MontageError) as ctx:
            self.plan([{"audiogram": "gap.m4a", "start": 3.9}])
        self.assertIn("gap.m4a", str(ctx.exception))

    def test_the_caption_comes_from_the_picture_when_none_is_given_and_not_at_all_with_captions_off(self):
        self.add("land.jpg", "gap.m4a")
        captions.set_detail(self.root, self.sha("land.jpg"), "The Dean")
        self.assertEqual(self.plan([{"audiogram": "gap.m4a", "image": "land.jpg"}])["segments"][0]["text"], "The Dean")
        self.assertEqual(self.plan([{"audiogram": "gap.m4a", "image": "land.jpg", "caption": "Mine"}])["segments"][0]["text"], "Mine")
        self.assertEqual(self.plan([{"audiogram": "gap.m4a", "image": "land.jpg"}], captions=False)["segments"][0]["text"], "")

    def test_the_music_dips_under_the_whole_audiogram_but_not_after_it(self):
        self.add("gap.m4a", "land.jpg", music=["song.m4a"])
        p = self.plan([{"audiogram": "gap.m4a", "hold": 3}, {"media": "land.jpg"}], music={"file": "song.m4a"})
        self.assertEqual(p["duck"], [[0.0, 3.0]])
        self.assertTrue(any(s.get("sound") for s in p["segments"]))

    # ---------------------------------------------------------------- on the beat
    def test_cut_to_a_tempo_it_fills_whole_cuts_and_never_runs_past_its_audio(self):
        self.add("gap.m4a", "land.jpg")
        p = self.plan([{"audiogram": "gap.m4a", "hold": 3}, {"media": "land.jpg"}], music={"bpm": 75})
        unit = p["sync"]["cut_seconds"]
        seg = p["segments"][0]
        self.assertAlmostEqual(seg["duration"] / unit, round(seg["duration"] / unit), delta=0.02)
        self.assertAlmostEqual(seg["src_used"], min(4.0, seg["duration"]), delta=0.02, msg="the sound taken follows the snapped length")
        self.assertEqual(seg["speed"], 1.0, "never slowed: the waveform and the sound must stay in step")

    def test_an_audio_clip_shorter_than_one_cut_still_takes_a_whole_cut_and_later_cuts_stay_on_the_grid(self):
        self.add("blip.m4a", "land.jpg")
        p = self.plan([{"audiogram": "blip.m4a"}, {"media": "land.jpg"}], music={"bpm": 75})
        unit = p["sync"]["cut_seconds"]
        blip, photo = p["segments"]
        self.assertAlmostEqual(blip["duration"], unit, delta=1 / p["fps"] + 1e-6)
        self.assertAlmostEqual(blip["src_used"], 0.7, delta=0.1, msg="its sound is just shorter than its slot")
        self.assertAlmostEqual(photo["start"] / unit, round(photo["start"] / unit), delta=0.03, msg="the next cut is still on the beat")

    # ---------------------------------------------------------------- the pictures and the sound
    ACCENT = staticmethod(lambda r, g, b: r > 215 and g > 190 and b < 130)
    MUSTARD = staticmethod(lambda r, g, b: 110 < r < 200 and 90 < g < 170 and b < 90)

    def count(self, path, at, test, w=216, h=384):
        d = frame(path, at, w, h)
        return sum(1 for i in range(0, len(d), 3) if test(d[i], d[i + 1], d[i + 2]))

    def level(self, path, a, b):
        r = subprocess.run([FF, "-hide_banner", "-ss", str(a), "-t", str(b - a), "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
        m = re.search(r"mean_volume: (-?[\d.]+)", r)
        return float(m.group(1)) if m else -91.0

    def test_the_waveform_is_solid_in_the_accent_colour_and_moves_with_the_sound(self):
        self.add("gap.m4a")
        job, out = self.render(recipe([{"audiogram": "gap.m4a"}], template="kinetic"))
        self.assertEqual(job["state"], "done", job["error"])
        loud, quiet = self.count(out, 1.0, self.ACCENT), self.count(out, 3.0, self.ACCENT)
        self.assertGreater(loud, 150, "a solid yellow waveform while the tone plays")
        self.assertGreater(loud, quiet * 3, "and a flat line while it is silent")
        d = frame(out, 1.0, 216, 384)
        band = [tuple(d[(y * 216 + x) * 3:(y * 216 + x) * 3 + 3]) for y in range(int(384 * 0.38), int(384 * 0.62)) for x in range(216)]
        bright = [p for p in band if p[0] > 100 or p[1] > 100]
        pure = sum(1 for p in bright if self.ACCENT(*p))
        self.assertGreater(pure / len(bright), 0.6, "the waveform is the accent colour (colorkey on a coloured waveform turned it green)")
        st = stats(out)
        self.assertEqual(st["pix"], "yuv420p", "not the full-range yuvj420p a picture piece gave before its range was set")
        self.assertIn(st["range"], (None, "tv"))

    def test_on_a_light_look_the_waveform_is_the_darkened_accent(self):
        self.add("gap.m4a")
        job, out = self.render(recipe([{"audiogram": "gap.m4a"}], template="collage"))
        self.assertEqual(job["state"], "done", job["error"])
        self.assertGreater(self.count(out, 1.0, self.MUSTARD), 150)
        self.assertEqual(self.count(out, 1.0, self.ACCENT), 0, "not the raw highlighter yellow on cream")

    def test_the_name_below_the_waveform_is_the_caption_and_absent_without_one(self):
        self.add("gap.m4a")
        white = lambda r, g, b: r > 235 and g > 235 and b > 235
        job, out = self.render(recipe([{"audiogram": "gap.m4a", "caption": "Dekan Fakulti"}], template="kinetic"))
        self.assertEqual(job["state"], "done", job["error"])
        d = frame(out, 3.0, 216, 384)                                                        # the label sits at about 68% of the height
        in_band = sum(1 for y in range(int(384 * 0.62), int(384 * 0.78)) for x in range(216) if white(*d[(y * 216 + x) * 3:(y * 216 + x) * 3 + 3]))
        self.assertGreater(in_band, 40, "the caption is drawn below the waveform")
        job, out = self.render(recipe([{"audiogram": "gap.m4a", "caption": "I"}], template="kinetic"))
        d = frame(out, 3.0, 216, 384)
        short = sum(1 for y in range(int(384 * 0.62), int(384 * 0.78)) for x in range(216) if white(*d[(y * 216 + x) * 3:(y * 216 + x) * 3 + 3]))
        self.assertGreater(in_band, short * 3, "the label shows what the caption SAYS: a long caption draws far more than a one-letter one")
        job, out = self.render(recipe([{"audiogram": "gap.m4a"}], template="kinetic"))
        d = frame(out, 3.0, 216, 384)
        none = sum(1 for y in range(int(384 * 0.62), int(384 * 0.78)) for x in range(216) if white(*d[(y * 216 + x) * 3:(y * 216 + x) * 3 + 3]))
        self.assertEqual(none, 0, "and nothing is drawn when there is no caption")

    def test_over_a_picture_with_a_name_and_the_sound_in_the_video(self):
        self.add("land.jpg", "gap.m4a")
        job, out = self.render(recipe([{"audiogram": "gap.m4a", "image": "land.jpg", "caption": "Dekan"}], template="kinetic"))
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(stats(out)["audio"], "aac")
        self.assertEqual(stats(out)["pix"], "yuv420p", "a picture piece is limited range, not the full-range yuvj420p")
        self.assertGreater(self.level(out, 0.2, 1.8), -40, "the tone is heard")
        self.assertLess(self.level(out, 2.4, 3.8), -60, "and then silence")
        self.assertGreater(self.count(out, 1.0, self.ACCENT), 100)

    def test_the_music_is_about_14_db_quieter_during_the_audiogram_and_returns_after_it(self):
        self.add("hush.m4a", "land.jpg", music=["song.m4a"])
        # a short window: over a long silent one, loudnorm's loudness-range compression lifts the dipped part of a mix that has no
        # voice in it (here the audiogram is silent so that only the music is measured)
        job, out = self.render(recipe([{"audiogram": "hush.m4a", "hold": 1.5}, {"media": "land.jpg", "hold": 4}], template="punchy", quality="draft",
                                      music={"file": "song.m4a"}))
        self.assertEqual(job["state"], "done", job["error"])
        during, after = self.level(out, 0.4, 1.3), self.level(out, 3.0, 5.0)
        self.assertAlmostEqual(after - during, 14, delta=4, msg=f"{during} dB during, {after} dB after")

    def test_every_look_and_format_draws_it_and_a_draft_has_the_same_timings(self):
        self.add("gap.m4a")
        clips = [{"audiogram": "gap.m4a", "hold": 2, "caption": "Nama"}]
        for tpl in montage.TEMPLATES:
            with self.subTest(template=tpl):
                job, out = self.render(recipe(clips, template=tpl, quality="draft"))
                self.assertEqual(job["state"], "done", f"{tpl}: {job['error']}")
        for fmt, size in (("square", 324), ("wide", 576)):
            with mock.patch.dict(montage.FORMATS[fmt], width=size, height=324):
                job, out = self.render(recipe(clips, format=fmt, quality="draft"))
            self.assertEqual(job["state"], "done", f"{fmt}: {job['error']}")
        final = montage.plan_for(recipe(clips, template="kinetic"), self.root)
        draft = montage.plan_for(recipe(clips, template="kinetic", quality="draft"), self.root)
        self.assertEqual([(x["start"], x["duration"]) for x in final["segments"]], [(x["start"], x["duration"]) for x in draft["segments"]])


@NEED
class Zoom(Fixtures):
    """Zoom in and out: a video clip's own move, and the zoom cut between pieces. Measured on a bright square."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        box = "color=c=0x101010:s=640x360,drawbox=x=200:y=60:w=240:h=240:color=white:t=fill"
        ff("-f", "lavfi", "-i", box, "-frames:v", "1", str(cls.fx / "box.jpg"))
        ff("-f", "lavfi", "-i", box.replace("color=c=0x101010:s=640x360", "color=c=0x101010:s=640x360:r=30:d=4"), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.fx / "boxclip.mp4"))

    def plan(self, clips, **kw):
        return montage.plan_for(recipe(clips, template="clean", **kw), self.root)

    def square(self, path, at, w=216, h=384):
        """Width in pixels of the bright square on the middle row of the frame at `at` seconds."""
        d = frame(path, at, w, h)
        row = h // 2
        xs = [x for x in range(w) if d[(row * w + x) * 3] > 200 and d[(row * w + x) * 3 + 1] > 200]
        return (xs[-1] - xs[0] + 1) if xs else 0

    def render_one(self, clip, **kw):
        job, out = self.render(recipe([clip], template="clean", **kw))
        self.assertEqual(job["state"], "done", job["error"])
        return job, out

    # ---------------------------------------------------------------- validation and the plan
    def test_transition_must_be_one_of_the_three_and_a_video_move_is_kept_in_the_plan(self):
        self.add("box.jpg", "boxclip.mp4")
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan([{"media": "box.jpg"}], transition="spin")
        self.assertIn("transition", str(ctx.exception))
        for t in montage.TRANSITIONS:
            self.assertEqual(self.plan([{"media": "box.jpg"}], transition=t)["transition"], t)
        self.assertIsNone(self.plan([{"media": "box.jpg"}])["transition"])
        p = self.plan([{"media": "boxclip.mp4", "move": "in"}, {"media": "boxclip.mp4"}])
        self.assertEqual(p["segments"][0]["move"], "in")
        self.assertNotIn("move", p["segments"][1])

    def test_a_zoom_cut_never_changes_a_timing(self):
        self.add("box.jpg", "boxclip.mp4")
        clips = [{"media": "box.jpg"}, {"media": "boxclip.mp4", "move": "out"}, {"media": "box.jpg"}]
        plain = self.plan(clips)
        for t in montage.TRANSITIONS:
            zoomed = self.plan(clips, transition=t)
            self.assertEqual([(s["start"], s["duration"]) for s in plain["segments"]], [(s["start"], s["duration"]) for s in zoomed["segments"]])

    # ---------------------------------------------------------------- what it does to the picture
    def test_without_a_transition_a_still_photo_does_not_change_size(self):
        self.add("box.jpg")
        job, out = self.render_one({"media": "box.jpg", "move": "still", "hold": 3})
        a, b, c = (self.square(out, t) for t in (0.03, 1.5, 2.9))
        self.assertGreater(a, 20)
        self.assertAlmostEqual(a / b, 1.0, delta=0.03)
        self.assertAlmostEqual(c / b, 1.0, delta=0.03)

    def test_zoom_out_starts_bigger_and_settles(self):
        self.add("box.jpg")
        job, out = self.render_one({"media": "box.jpg", "move": "still", "hold": 3}, transition="zoom_out")
        start, mid, end = (self.square(out, t) for t in (0.0, 1.5, 2.9))
        self.assertAlmostEqual(start / mid, 1 + montage.PUNCH, delta=0.03, msg=f"{start} vs {mid}")
        self.assertAlmostEqual(end / mid, 1.0, delta=0.03, msg="it settled and stays settled")

    def test_zoom_in_pushes_in_at_the_end_only(self):
        self.add("box.jpg")
        job, out = self.render_one({"media": "box.jpg", "move": "still", "hold": 3}, transition="zoom_in")
        start, mid, end = (self.square(out, t) for t in (0.0, 1.5, 2.95))
        self.assertAlmostEqual(start / mid, 1.0, delta=0.03)
        self.assertGreater(end / mid, 1.05, "pushing in as the piece ends (0.25 s before the cut it is at 58% of its push)")

    def test_zoom_does_both(self):
        self.add("box.jpg")
        job, out = self.render_one({"media": "box.jpg", "move": "still", "hold": 3}, transition="zoom")
        start, mid, end = (self.square(out, t) for t in (0.0, 1.5, 2.95))
        self.assertGreater(start / mid, 1.08)
        self.assertGreater(end / mid, 1.05)

    def test_a_video_clip_can_zoom_in_or_out_over_its_whole_length(self):
        self.add("boxclip.mp4")
        job, out = self.render_one({"media": "boxclip.mp4", "move": "in", "hold": 3.5})
        a, b = self.square(out, 0.1), self.square(out, 3.3)
        self.assertGreater(b / a, 1.04, f"{a} -> {b}: zooming in")
        self.assertLess(b / a, 1.09, "by about the look's zoom amount, not more")
        self.root_clean()
        job, out = self.render_one({"media": "boxclip.mp4", "move": "out", "hold": 3.5})
        a, b = self.square(out, 0.1), self.square(out, 3.3)
        self.assertLess(b / a, 0.96, f"{a} -> {b}: zooming out")

    def root_clean(self):
        for p in (self.root / "exports").glob("*"):
            p.unlink()

    def test_a_video_clip_with_no_move_does_not_zoom_and_a_cut_still_applies_to_it(self):
        self.add("boxclip.mp4")
        job, out = self.render_one({"media": "boxclip.mp4", "hold": 3}, transition="zoom_out")
        start, mid, end = (self.square(out, t) for t in (0.0, 1.5, 2.8))
        self.assertGreater(start / mid, 1.08)
        self.assertAlmostEqual(end / mid, 1.0, delta=0.03)

    def test_every_look_and_format_renders_with_a_zoom_cut_and_a_draft_matches(self):
        self.add("box.jpg", "boxclip.mp4")
        clips = [{"media": "box.jpg", "caption": "Hi"}, {"media": "boxclip.mp4", "move": "in"}, {"quote": "A quote"}, {"countdown": 2}]
        for tpl in montage.TEMPLATES:
            with self.subTest(template=tpl):
                job, out = self.render(recipe(clips, template=tpl, transition="zoom", quality="draft"))
                self.assertEqual(job["state"], "done", f"{tpl}: {job['error']}")
        for fmt, size in (("square", 324), ("wide", 576)):
            with mock.patch.dict(montage.FORMATS[fmt], width=size, height=324):
                job, out = self.render(recipe(clips, format=fmt, transition="zoom", quality="draft"))
            self.assertEqual(job["state"], "done", f"{fmt}: {job['error']}")


NEED_SAY2 = unittest.skipUnless(shutil.which("say"), "needs macOS `say` for real speech")


@NEED
@NEED_SAY2
class TightenGaps(Fixtures):
    """Jump cuts: the dead air cut out of a talking clip, with its sound and the music dip following."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        subprocess.run(["say", "-o", str(cls.fx / "ta.aiff"), "Selamat datang ke hari terbuka CyberDSA dua ribu dua puluh enam."], check=True)
        subprocess.run(["say", "-o", str(cls.fx / "tb.aiff"), "Terima kasih kerana hadir hari ini."], check=True)
        for n in ("ta", "tb"):
            ff("-i", str(cls.fx / f"{n}.aiff"), "-ar", "48000", "-ac", "1", str(cls.fx / f"{n}.wav"))
        ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "3.5", str(cls.fx / "gap35.wav"))
        ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "2.5", str(cls.fx / "gap25.wav"))
        ff("-i", str(cls.fx / "ta.wav"), "-i", str(cls.fx / "gap35.wav"), "-i", str(cls.fx / "tb.wav"), "-i", str(cls.fx / "gap25.wav"),
           "-filter_complex", "concat=n=4:v=0:a=1", str(cls.fx / "gappy.wav"))
        dur = float(subprocess.run([prepare.find_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(cls.fx / "gappy.wav")],
                                   capture_output=True, text=True).stdout)
        cls.gappy_len = dur
        ff("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:d={dur},noise=alls=30:allf=t", "-i", str(cls.fx / "gappy.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(cls.fx / "gappy.mp4"))

    def plan(self, clips, **kw):
        return montage.plan_for(recipe(clips, template="punchy", max_seconds=60, **kw), self.root)

    # ---------------------------------------------------------------- validation
    def test_tighten_is_gaps_or_nothing_and_a_clip_can_opt_out_but_a_photo_cannot_use_it(self):
        self.add("gappy.mp4", "land.jpg")
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan([{"media": "gappy.mp4"}], tighten="all")
        self.assertIn("tighten", str(ctx.exception))
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan([{"media": "gappy.mp4", "tighten": "yes"}])
        self.assertIn("true or false", str(ctx.exception))
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan([{"media": "land.jpg", "tighten": True}])
        self.assertIn("video clips", str(ctx.exception))

    # ---------------------------------------------------------------- the plan
    def test_the_dead_air_between_phrases_is_cut_and_the_plan_says_how_much(self):
        self.add("gappy.mp4")
        before = self.plan([{"media": "gappy.mp4", "hold": 15}])["segments"][0]
        after = self.plan([{"media": "gappy.mp4", "hold": 15}], tighten="gaps")["segments"][0]
        self.assertNotIn("ranges", before)
        self.assertEqual(after["cuts"], 1, "two phrases: one jump cut")
        self.assertGreater(after["trimmed"], 2.0, "most of the 3.5 s silence between them is gone")
        self.assertLess(after["duration"], before["duration"] - 2.0)
        self.assertAlmostEqual(after["duration"], sum(b - a for a, b in after["ranges"]) / after["speed"], delta=0.01)
        (a1, b1), (a2, b2) = after["ranges"]
        self.assertLess(b1, a2)
        self.assertLessEqual(a1, 0.3, "the first phrase starts at the start of the clip")
        self.assertGreater(after["speech"], 5.0)

    def test_a_clip_can_opt_out_and_one_without_speech_is_left_alone(self):
        self.add("gappy.mp4", "short.mp4", "clip.mp4")
        p = self.plan([{"media": "gappy.mp4", "tighten": False}, {"media": "short.mp4"}, {"media": "clip.mp4"}], tighten="gaps")
        for seg in p["segments"]:
            self.assertNotIn("ranges", seg)
        p = self.plan([{"media": "gappy.mp4", "hold": 15, "tighten": True}])
        self.assertIn("ranges", p["segments"][0], "per clip, without the recipe-level switch")

    def test_the_hold_still_caps_the_length_of_a_tightened_clip(self):
        self.add("gappy.mp4")
        seg = self.plan([{"media": "gappy.mp4", "hold": 4}], tighten="gaps")["segments"][0]
        self.assertAlmostEqual(seg["duration"], 4.0, delta=0.05)
        self.assertAlmostEqual(sum(b - a for a, b in seg["ranges"]), 4.0, delta=0.05)

    def test_a_tightened_clip_keeps_its_sound_even_without_the_clip_sound_option(self):
        self.add("gappy.mp4")
        self.assertTrue(self.plan([{"media": "gappy.mp4", "hold": 15}], tighten="gaps")["segments"][0]["sound"])
        self.assertNotIn("sound", self.plan([{"media": "gappy.mp4", "hold": 15}])["segments"][0])

    def test_sound_and_the_music_dip_follow_the_joined_clip_not_the_original_timeline(self):
        self.add("gappy.mp4", "land.jpg", music=["song.m4a"])
        p = self.plan([{"media": "gappy.mp4", "hold": 15}, {"media": "land.jpg"}], tighten="gaps", clip_sound="speech", music={"file": "song.m4a"})
        seg = p["segments"][0]
        self.assertTrue(seg["sound"])
        self.assertTrue(p["duck"])
        for a, b in p["duck"]:
            self.assertGreaterEqual(a, seg["start"] - 1e-6)
            self.assertLessEqual(b, seg["start"] + seg["duration"] + 1e-6)
        self.assertLessEqual(max(b for a, b in p["duck"]), seg["duration"] + 1e-6, "no dip beyond the end of the shortened clip")
        self.assertTrue(all(a < b for a, b in p["duck"]), "every dip window is well-formed")
        self.assertLessEqual(p["duck"][0][0], seg["start"] + 0.6, "the first dip starts where the first phrase does (the clip's own timeline, not the source's)")
        self.assertGreater(sum(b - a for a, b in p["duck"]), seg["speech"] * 0.8, "and together they cover the speech that is heard")

    def test_it_composes_with_a_tempo_and_never_slows_on_its_own(self):
        self.add("gappy.mp4", "land.jpg")
        p = self.plan([{"media": "gappy.mp4", "hold": 15}, {"media": "land.jpg"}], tighten="gaps", music={"bpm": 75})
        unit = p["sync"]["cut_seconds"]
        seg = p["segments"][0]
        self.assertAlmostEqual(seg["duration"] / unit, round(seg["duration"] / unit), delta=0.03)
        self.assertLessEqual(seg["src_used"], seg["tight_total"] + 1e-6)
        self.assertEqual(seg["speed"], 1.0)

    def test_a_zoom_move_and_a_zoom_cut_do_not_stop_it(self):
        self.add("gappy.mp4")
        seg = self.plan([{"media": "gappy.mp4", "hold": 15, "move": "in"}], tighten="gaps", transition="zoom")["segments"][0]
        self.assertEqual((seg["move"], seg["cuts"]), ("in", 1))

    # ---------------------------------------------------------------- the video it makes
    def test_the_rendered_video_is_as_short_as_planned_with_its_sound_and_the_joined_file_is_cached(self):
        self.add("gappy.mp4")
        rec = recipe([{"media": "gappy.mp4", "hold": 15}], template="punchy", tighten="gaps", quality="draft", max_seconds=60)
        planned = montage.plan_for(rec, self.root)["total"]
        job, out = self.render(rec)
        self.assertEqual(job["state"], "done", job["error"])
        st = stats(out)
        self.assertAlmostEqual(st["dur"], planned, delta=0.4)
        self.assertLess(st["dur"], self.gappy_len - 2.0, "shorter than the clip by the dead air")
        self.assertEqual(st["audio"], "aac", "the clip's own sound is kept without asking: a tightened talking clip is useless silent")
        cached = list((self.root / ".umcares" / "tight").glob("*.mp4"))
        self.assertEqual(len(cached), 1)
        stamp = cached[0].stat().st_mtime_ns
        job, out = self.render({**rec, "title": "again"})
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(cached[0].stat().st_mtime_ns, stamp, "the joined clip was reused, not made again")

    def test_the_joined_clip_has_no_silent_gap_where_the_dead_air_was(self):
        self.add("gappy.mp4")
        job, out = self.render(recipe([{"media": "gappy.mp4", "hold": 15}], template="punchy", tighten="gaps", quality="draft", max_seconds=60))
        self.assertEqual(job["state"], "done", job["error"])
        log = subprocess.run([FF, "-hide_banner", "-i", str(out), "-af", "silencedetect=n=-45dB:d=1.2", "-f", "null", "-"], capture_output=True, text=True).stderr
        long_silences = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", log)]
        self.assertEqual([d for d in long_silences if d < 5], [], f"no silence of 1.2 s or more is left inside the video: {long_silences}")

    def test_a_talking_clip_with_its_own_sound_and_effects_in_one_mix(self):
        self.add("gappy.mp4", "land.jpg")
        rec = recipe([{"media": "gappy.mp4", "hold": 15}, {"media": "land.jpg", "hold": 3, "move": "still"}], template="punchy", tighten="gaps",
                     sfx={"cuts": "hit"}, quality="draft", max_seconds=60)
        p = montage.plan_for(rec, self.root)
        cut = p["segments"][1]["start"]
        job, out = self.render(rec)
        self.assertEqual(job["state"], "done", job["error"])

        def level(a, b):
            r = subprocess.run([FF, "-hide_banner", "-ss", str(a), "-t", str(b - a), "-i", str(out), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
            return float(re.search(r"mean_volume: (-?[\d.]+)", r).group(1))
        self.assertGreater(level(0.3, 2.0), -40, "the speech is still heard")
        self.assertGreater(level(cut, cut + 0.4), -50, "and so is the hit at the cut")
        self.assertLess(level(cut + 0.8, cut + 2.5), -60, "and nothing else after the clip: the effects were not given a normalised boost")

    def test_a_tightened_clip_in_every_look_renders(self):
        self.add("gappy.mp4")
        for tpl in montage.TEMPLATES:
            with self.subTest(template=tpl):
                job, out = self.render(recipe([{"media": "gappy.mp4", "hold": 15}], template=tpl, tighten="gaps", quality="draft", max_seconds=60))
                self.assertEqual(job["state"], "done", f"{tpl}: {job['error']}")


@NEED
class ShareQuality(Fixtures):
    """`quality: "share"`: the same video at full size and with the same timings, in a much smaller file."""

    def setUp(self):
        super().setUp()
        self.add("land.jpg", "port.jpg")
        self.clips = [{"media": "land.jpg", "hold": 2}, {"media": "port.jpg", "hold": 2}, {"media": "land.jpg", "hold": 2}]

    def plan(self, **kw):
        return montage.plan_for(recipe(self.clips, template="punchy", **kw), self.root)

    def test_it_is_a_quality_the_recipe_can_name_and_a_wrong_one_is_refused(self):
        self.assertEqual(self.plan(quality="share")["quality"], "share")
        self.assertEqual(self.plan()["quality"], "final")
        with self.assertRaises(montage.RecipeError) as ctx:
            self.plan(quality="tiny")
        self.assertIn("share", str(ctx.exception))

    def test_it_is_not_a_draft_so_the_size_and_the_timings_are_the_finals(self):
        final, share, draft = self.plan(), self.plan(quality="share"), self.plan(quality="draft")
        self.assertEqual((share["width"], share["height"]), (final["width"], final["height"]), "full size, unlike a draft")
        self.assertLess(draft["width"], final["width"])
        self.assertEqual([(x["start"], x["duration"]) for x in share["segments"]], [(x["start"], x["duration"]) for x in final["segments"]])

    def test_the_encode_is_capped_and_a_final_is_not(self):
        self.assertIn("-maxrate", montage._encode_piece({"quality": "share"}))
        self.assertIn("23", montage._encode_piece({"quality": "share"}))
        self.assertNotIn("-maxrate", montage._encode_piece({"quality": "final"}))
        self.assertNotIn("-maxrate", montage._encode_piece({"quality": "draft"}))
        self.assertIn("14", montage._encode_piece({"quality": "final"}), "the final is as it was")

    def test_the_file_is_smaller_the_same_length_and_named_and_listed_as_a_share_copy(self):
        sizes, durs = {}, {}
        for q in ("final", "share"):
            job, out = self.render(recipe(self.clips, template="punchy", quality=q))
            self.assertEqual(job["state"], "done", job["error"])
            sizes[q], durs[q] = out.stat().st_size, stats(out)["dur"]
            if q == "share":
                self.assertIn("-share-", out.name)
                self.assertEqual(next(e for e in montage.exports(self.root) if e["name"] == out.name)["quality"], "share")
        self.assertLess(sizes["share"], 0.8 * sizes["final"], f"{sizes}")
        self.assertAlmostEqual(durs["share"], durs["final"], delta=0.1)

    def test_a_crossfading_look_is_capped_too(self):
        """Crossfades re-encode the whole video in one pass, which is a different code path from the stream copy of hard cuts."""
        job, out = self.render(recipe(self.clips, template="clean", quality="share"))
        self.assertEqual(job["state"], "done", job["error"])
        job2, out2 = self.render(recipe(self.clips, template="clean"))
        self.assertLess(out.stat().st_size, 0.8 * out2.stat().st_size)


@NEED
class ThreeD(Fixtures):
    """The card flip between pieces (not into the first piece, not out of the last) and the swaying card look: measured on a bright square, since both turn the picture about its vertical axis."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ff("-f", "lavfi", "-i", "color=c=0x101010:s=640x360,drawbox=x=200:y=60:w=240:h=240:color=white:t=fill", "-frames:v", "1", str(cls.fx / "box.jpg"))

    def clips(self, n=2):
        return [{"media": "box.jpg", "move": "still", "hold": 3} for _ in range(n)]

    def plan(self, **kw):
        return montage.plan_for(recipe(self.clips(), template="punchy", **kw), self.root)

    def square(self, path, at, w=216, h=384):
        d = frame(path, at, w, h)
        row = h // 2
        xs = [x for x in range(w) if d[(row * w + x) * 3] > 200 and d[(row * w + x) * 3 + 1] > 200]
        return (xs[-1] - xs[0] + 1) if xs else 0

    def made(self, **kw):
        if not getattr(self, "_box", False):                  # the same file cannot be uploaded twice
            self.add("box.jpg")
            self._box = True
        job, out = self.render(recipe(self.clips(), template="punchy", **kw))
        self.assertEqual(job["state"], "done", job["error"])
        return out

    # ---------------------------------------------------------------- the recipe
    def test_the_words_are_checked(self):
        self.add("box.jpg")
        for kw, why in (({"depth": "sphere"}, "depth"), ({"depth": True}, "depth"), ({"transition": "cube"}, "transition")):
            with self.subTest(kw=kw):
                with self.assertRaises(montage.RecipeError) as ctx:
                    self.plan(**kw)
                self.assertIn(why, str(ctx.exception))
        p = self.plan(transition="flip", depth="card")
        self.assertEqual((p["transition"], p["depth"]), ("flip", "card"))
        self.assertIsNone(self.plan()["depth"])

    def test_flip_and_depth_never_change_a_timing(self):
        self.add("box.jpg")
        a, b = self.plan(), self.plan(transition="flip", depth="card")
        self.assertEqual([(s["start"], s["duration"]) for s in a["segments"]], [(s["start"], s["duration"]) for s in b["segments"]])
        self.assertEqual(a["total"], b["total"])

    def test_the_flip_does_not_turn_the_first_piece_in_or_the_last_one_out(self):
        enter_only = montage._flip_fragment("[a]", "[b]", 30, 3.0, True, False)
        leave_only = montage._flip_fragment("[a]", "[b]", 30, 3.0, False, True)
        self.assertEqual(montage._flip_fragment("[a]", "[b]", 30, 3.0, False, False), "", "a lone piece has nothing to flip")
        self.assertIn("pow(1-min(1,", enter_only)
        self.assertNotIn("pow(max(0,", enter_only)
        self.assertIn("pow(max(0,", leave_only)
        self.assertNotIn("pow(1-min(1,", leave_only)

    # ---------------------------------------------------------------- what it does to the picture
    def test_flip_swings_each_piece_in_and_out_and_leaves_the_middle_alone(self):
        out = self.made(transition="flip")
        end = self.plan()["total"]
        first = self.plan()["segments"][1]["start"]
        first_start, first_mid, first_end = (self.square(out, t) for t in (0.0, first / 2, first - 0.05))
        second_start, second_mid, second_end = (self.square(out, t) for t in (first, (first + end) / 2, end - 0.07))
        self.assertGreater(first_start, 0.9 * first_mid, "the first piece is just there: nothing to flip in from")
        self.assertLess(first_end, 0.5 * first_mid, "it swings away before the cut")
        self.assertLess(second_start, 0.5 * second_mid, "and the next one swings in")
        self.assertAlmostEqual(second_mid / first_mid, 1.0, delta=0.05)
        self.assertGreater(second_end, 0.9 * second_mid, "the last piece stays: the video does not end on a sliver")

    def test_without_flip_the_cut_is_a_cut(self):
        out = self.made()
        first = self.plan()["segments"][1]["start"]
        a, b, c = (self.square(out, t) for t in (first - 0.05, first, first + 1.5))
        self.assertAlmostEqual(a / c, 1.0, delta=0.03)
        self.assertAlmostEqual(b / c, 1.0, delta=0.03)

    def test_a_card_is_smaller_than_the_frame_and_sways(self):
        base = self.square(self.made(), 1.5)
        self.root_clean()
        out = self.made(depth="card")
        rest, swayed = self.square(out, 0.05), self.square(out, 0.75)
        self.assertAlmostEqual(rest / base, 0.8, delta=0.06, msg=f"{rest} vs {base}")
        self.assertLess(swayed, 0.95 * rest, "turned a quarter of the way through its sway")

    def root_clean(self):
        for p in (self.root / "exports").glob("*"):
            p.unlink()

    def test_card_and_flip_together_and_every_look_and_format_render(self):
        self.add("box.jpg", "land.jpg")
        clips = [{"media": "box.jpg", "caption": "Hi"}, {"media": "land.jpg"}, {"quote": "A quote"}, {"countdown": 2}]
        for tpl in montage.TEMPLATES:
            with self.subTest(template=tpl):
                job, out = self.render(recipe(clips, template=tpl, transition="flip", depth="card", quality="draft"))
                self.assertEqual(job["state"], "done", f"{tpl}: {job['error']}")
        for fmt, size in (("square", 324), ("wide", 576)):
            with mock.patch.dict(montage.FORMATS[fmt], width=size, height=324):
                job, out = self.render(recipe(clips, format=fmt, transition="flip", depth="card", quality="draft"))
            self.assertEqual(job["state"], "done", f"{fmt}: {job['error']}")


@NEED
class SoundEffects(Fixtures):
    """Sound effects on cuts and words: where they land, how loud, and that nothing else changes."""

    def plan(self, clips, **kw):
        return montage.plan_for(recipe(clips, template=kw.pop("template", "punchy"), **kw), self.root)

    def level(self, path, a, b, key="mean_volume"):
        r = subprocess.run([FF, "-hide_banner", "-ss", str(a), "-t", str(b - a), "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
        m = re.search(key + r": (-?[\d.]+)", r)
        return float(m.group(1)) if m else -91.0

    def made(self, clips, **kw):
        job, out = self.render(recipe(clips, template=kw.pop("template", "punchy"), **kw))
        self.assertEqual(job["state"], "done", job["error"])
        return out

    # ---------------------------------------------------------------- validation and the plan
    def test_the_spec_is_checked(self):
        self.add("land.jpg")
        for bad, why in (("loud", "sfx"), ({"cuts": "laser"}, "sfx.cuts"), ({"words": "whoosh"}, "sfx.words"), ({"level": 5}, "sfx.level"), ({"level": "x"}, "sfx.level"),
                         ({"volume": 3}, "unknown field")):
            with self.subTest(bad=bad):
                with self.assertRaises(montage.RecipeError) as ctx:
                    self.plan([{"media": "land.jpg"}], sfx=bad)
                self.assertIn(why, str(ctx.exception))

    def test_the_plan_lists_the_events_and_an_unrequested_plan_has_none(self):
        self.add("land.jpg", "port.jpg")
        clips = [{"media": "land.jpg", "hold": 2}, {"media": "port.jpg", "hold": 2}, {"countdown": 2}]
        self.assertIsNone(self.plan(clips)["sfx"])
        p = self.plan(clips, sfx={"cuts": "whoosh", "words": "tick", "level": -9})
        self.assertEqual(p["sfx"]["level"], -9.0)
        kinds = [(e["kind"], e["sound"]) for e in p["sfx"]["events"]]
        self.assertIn(("cut", "whoosh"), kinds)
        self.assertIn(("word", "tick"), kinds, "a countdown ticks in every look")
        self.assertEqual(self.plan(clips, sfx={"cuts": "pop"})["sfx"]["level"], sfx.DEFAULT_LEVEL)

    def test_many_effects_do_not_cut_the_soundtrack_short(self):
        """Regression: with ~20 delayed effects the mix ended at the last effect, leaving the last seconds of the picture silent."""
        self.add("land.jpg", "port.jpg")
        clips = [{"countdown": 5}, {"countdown": 5}, {"media": "land.jpg", "hold": 3}, {"media": "port.jpg", "hold": 3}]
        out = self.made(clips, sfx={"cuts": "whoosh", "words": "tick"})
        plan = self.plan(clips, sfx={"cuts": "whoosh", "words": "tick"})
        self.assertGreaterEqual(len(plan["sfx"]["events"]), 10)
        r = subprocess.run([FP, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
        self.assertAlmostEqual(float(r.stdout.strip()), plan["total"], delta=0.25)

    def test_effects_never_change_a_timing(self):
        self.add("land.jpg", "port.jpg")
        clips = [{"media": "land.jpg"}, {"media": "port.jpg"}, {"countdown": 3}]
        a, b = self.plan(clips), self.plan(clips, sfx={"cuts": "hit", "words": "tick"})
        self.assertEqual([(x["start"], x["duration"]) for x in a["segments"]], [(x["start"], x["duration"]) for x in b["segments"]])
        self.assertEqual(a["total"], b["total"])

    # ---------------------------------------------------------------- what is heard
    def test_a_hit_lands_on_the_cut_and_there_is_silence_elsewhere(self):
        self.add("land.jpg", "port.jpg")
        out = self.made([{"media": "land.jpg", "hold": 2, "move": "still"}, {"media": "port.jpg", "hold": 2, "move": "still"}], sfx={"cuts": "hit"})
        self.assertEqual(stats(out)["audio"], "aac", "effects alone give the video a soundtrack")
        on_cut, elsewhere = self.level(out, 1.98, 2.4), self.level(out, 0.2, 1.6)
        self.assertGreater(on_cut, -45, "the hit is audible at the cut")
        self.assertLess(elsewhere, -65, "and it is silent before it")
        self.assertGreater(on_cut - elsewhere, 25)

    def test_a_whoosh_swells_into_the_cut_so_it_is_louder_before_the_cut_than_just_after_its_tail(self):
        self.add("land.jpg", "port.jpg")
        out = self.made([{"media": "land.jpg", "hold": 3, "move": "still"}, {"media": "port.jpg", "hold": 3, "move": "still"}], sfx={"cuts": "whoosh"})
        body = self.level(out, 2.8, 3.1)                               # its middle is on the cut at 3.0
        before, after = self.level(out, 2.76, 2.9), self.level(out, 3.35, 3.9)
        self.assertGreater(body, -40)
        self.assertGreater(before, after + 12, "the swell comes before the cut and is gone shortly after it")
        self.assertLess(self.level(out, 0.3, 2.4), -65)

    def test_the_word_ticks_pop_with_the_words_on_the_kinetic_look_only(self):
        self.add("land.jpg")
        captions.set_detail(self.root, self.sha("land.jpg"), "Satu dua tiga")
        out = self.made([{"media": "land.jpg", "hold": 3, "move": "still"}], template="kinetic", sfx={"words": "tick"})
        for t in (0.08, 0.58, 1.08):                                   # the schedule of three lines in a 3 s piece
            self.assertGreater(self.level(out, t - 0.01, t + 0.07), self.level(out, t + 0.2, t + 0.4) + 15, f"a tick at {t}")
        plain = self.render(recipe([{"media": "land.jpg", "hold": 3}], template="punchy", sfx={"words": "tick"}))[1]
        self.assertIsNone(stats(plain)["audio"], "no word pops on this look, so no word ticks, so no soundtrack")

    def test_a_countdown_ticks_on_each_number_in_any_look(self):
        out = self.made([{"countdown": 3}], template="clean", sfx={"words": "pop"})
        for t in (0.0, 0.8, 1.6):
            self.assertGreater(self.level(out, t, t + 0.12), self.level(out, t + 0.3, t + 0.7) + 15, f"a pop at {t}")

    def test_the_level_is_the_loudness_of_the_effects_and_they_are_limited_not_normalised_up(self):
        self.add("land.jpg", "port.jpg")
        clips = [{"media": "land.jpg", "hold": 2, "move": "still"}, {"media": "port.jpg", "hold": 2, "move": "still"}]
        quiet = self.level(self.made(clips, sfx={"cuts": "hit", "level": -30}), 1.98, 2.4, "max_volume")
        loud = self.level(self.made(clips, sfx={"cuts": "hit", "level": -12}), 1.98, 2.4, "max_volume")
        self.assertAlmostEqual(loud - quiet, 18, delta=3, msg=f"{quiet} dB vs {loud} dB")
        self.assertLess(loud, -3, "a few sparse effects are not boosted toward -16 LUFS")

    def test_with_music_the_effects_ride_on_top_of_it_in_one_normalised_mix(self):
        self.add("land.jpg", "port.jpg", music=["song.m4a"])
        out = self.made([{"media": "land.jpg", "hold": 2}, {"media": "port.jpg", "hold": 2}], music={"file": "song.m4a"}, sfx={"cuts": "hit"})
        from umcares import judge
        r = judge.judge(out, json.loads(out.with_suffix(".json").read_text())["plan"])
        self.assertAlmostEqual(r["stats"]["lufs"], -16, delta=2.5, msg="the music is still at the target loudness")
        self.assertEqual(next(c["level"] for c in r["checks"] if c["id"] == "audio-present"), "ok")

    def test_the_judge_knows_an_effects_only_video_is_meant_to_have_sound(self):
        self.add("land.jpg", "port.jpg")
        out = self.made([{"media": "land.jpg", "hold": 2}, {"media": "port.jpg", "hold": 2}], sfx={"cuts": "pop"})
        from umcares import judge
        r = judge.judge(out, json.loads(out.with_suffix(".json").read_text())["plan"])
        self.assertEqual(next(c["level"] for c in r["checks"] if c["id"] == "audio-present"), "ok")
        self.assertNotEqual(r["verdict"], "fail", [c for c in r["checks"] if c["level"] == "fail"])

    def test_every_look_and_format_renders_with_both_kinds_of_effect(self):
        self.add("land.jpg", "port.jpg")
        clips = [{"media": "land.jpg", "caption": "Hi there"}, {"media": "port.jpg"}, {"quote": "Q"}, {"countdown": 2}]
        for tpl in montage.TEMPLATES:
            with self.subTest(template=tpl):
                job, out = self.render(recipe(clips, template=tpl, sfx={"cuts": "whoosh", "words": "tick"}, quality="draft"))
                self.assertEqual(job["state"], "done", f"{tpl}: {job['error']}")
        for fmt, size in (("square", 324), ("wide", 576)):
            with mock.patch.dict(montage.FORMATS[fmt], width=size, height=324):
                job, out = self.render(recipe(clips, format=fmt, sfx={"cuts": "hit"}, quality="draft"))
            self.assertEqual(job["state"], "done", f"{fmt}: {job['error']}")


def gray_frame(path, at):
    return subprocess.run([FF, "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, check=True).stdout


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
