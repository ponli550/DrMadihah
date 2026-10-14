import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umcares import judge, montage, prepare
from tests.test_montage import Fixtures, NEED as NEED_RENDER, recipe

FF = prepare.find_tool("ffmpeg")
NEED = unittest.skipUnless(FF and prepare.find_tool("ffprobe"), "needs ffmpeg and ffprobe")
W, H = 216, 384
SOUND = "sine=frequency=330:duration={d},loudnorm=I=-16:LRA=11:TP=-1.5"          # a steady tone at about -16 LUFS


def ff(*a):
    subprocess.run([FF, "-v", "error", "-y", *a], check=True, capture_output=True)


def plan(total=4.0, **kw):
    base = {"width": W, "height": H, "fps": 30, "total": total, "music": {"name": "song.m4a"}, "warnings": [], "sync": None,
            "segments": [{"kind": "photo", "start": 0.0, "duration": 2.0}, {"kind": "video", "start": 2.0, "duration": 2.0}],
            "recipe": {"max_seconds": 30}}
    base.update(kw)
    return base


def level_of(report, cid):
    return next(c["level"] for c in report["checks"] if c["id"] == cid)


@NEED
class Judging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def make(self, name, video="testsrc2=size=216x384:rate=30", dur=4, audio=SOUND, vopts=(), aopts=(), vf=None, a_in=None):
        """A small video with a picture and (unless audio=None) a sound track."""
        out = self.d / name
        if out.exists():
            return out
        cmd = ["-f", "lavfi", "-i", f"{video}:d={dur}" if ":d=" not in video else video]
        if audio:
            cmd += ["-f", "lavfi", "-i", a_in or audio.format(d=dur)]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", *vopts]
        cmd += ["-c:a", "aac", "-ar", "48000", *aopts] if audio else ["-an"]
        ff(*cmd, "-t", str(dur), str(out))
        return out

    # ---------------------------------------------------------------- a good video
    def test_a_well_made_video_passes_every_check(self):
        r = judge.judge(self.make("good.mp4"), plan())
        self.assertEqual(r["verdict"], "pass", [c for c in r["checks"] if c["level"] != "ok"])
        self.assertEqual(r["counts"]["fail"] + r["counts"]["warn"], 0)
        ids = {c["id"] for c in r["checks"]}
        for must in ("container", "video-codec", "pixel-format", "size", "frame-rate", "duration", "bitrate", "audio-present", "loudness", "clipping",
                     "black-frames", "frozen-video"):
            self.assertIn(must, ids)
        self.assertAlmostEqual(r["stats"]["lufs"], -16, delta=2)

    # ---------------------------------------------------------------- format
    def test_the_wrong_size_is_a_failure_that_says_both_sizes(self):
        r = judge.judge(self.make("size.mp4", video="testsrc2=size=320x240:rate=30"), plan())
        self.assertEqual(level_of(r, "size"), "fail")
        msg = next(c["message"] for c in r["checks"] if c["id"] == "size")
        self.assertIn("320x240", msg)
        self.assertIn(f"{W}x{H}", msg)

    def test_a_full_range_pixel_format_fails_with_the_reason(self):
        out = self.d / "yuvj.mp4"
        ff("-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=30:d=4", "-f", "lavfi", "-i", SOUND.format(d=4), "-c:v", "libx264", "-pix_fmt", "yuvj420p", "-c:a", "aac", "-t", "4", str(out))
        r = judge.judge(out, plan())
        self.assertEqual(level_of(r, "pixel-format"), "fail")
        self.assertIn("full range", next(c["message"] for c in r["checks"] if c["id"] == "pixel-format"))

    def test_a_codec_that_is_not_h264_fails(self):
        out = self.d / "mpeg4.mp4"
        ff("-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=30:d=4", "-f", "lavfi", "-i", SOUND.format(d=4), "-c:v", "mpeg4", "-c:a", "aac", "-t", "4", str(out))
        self.assertEqual(level_of(judge.judge(out, plan()), "video-codec"), "fail")

    def test_a_different_frame_rate_warns(self):
        self.assertEqual(level_of(judge.judge(self.make("fps.mp4", video="testsrc2=size=216x384:rate=25"), plan()), "frame-rate"), "warn")

    def test_the_length_is_compared_with_the_plan_and_the_limit(self):
        long = self.make("long.mp4", dur=6)
        self.assertEqual(level_of(judge.judge(long, plan(total=4.0)), "duration"), "fail", "two seconds off is a failure")
        self.assertEqual(level_of(judge.judge(self.make("near.mp4", dur=4), plan(total=3.6)), "duration"), "warn", "0.4 s off is a warning")
        r = judge.judge(long, plan(total=6.0, recipe={"max_seconds": 5}))
        self.assertEqual(level_of(r, "length-limit"), "fail")

    def test_sound_and_picture_of_different_lengths_are_caught(self):
        out = self.d / "avlen.mp4"
        ff("-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=30:d=6", "-f", "lavfi", "-i", SOUND.format(d=3), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out))
        self.assertIn(level_of(judge.judge(out, plan(total=6.0)), "audio-length"), ("warn", "fail"))

    def test_a_thin_and_a_heavy_bitrate_both_warn(self):
        thin = self.make("thin.mp4", vopts=("-b:v", "4k", "-maxrate", "4k", "-bufsize", "8k"))
        self.assertEqual(level_of(judge.judge(thin, plan()), "bitrate"), "warn")
        heavy = self.make("heavy.mp4", video=f"nullsrc=size={W}x{H}:rate=30:d=2,geq=random(1)*255:128:128", vopts=("-crf", "0", "-preset", "ultrafast"), dur=2)
        r = judge.judge(heavy, plan(total=2.0, segments=[{"kind": "photo", "start": 0.0, "duration": 2.0}]))
        self.assertEqual(level_of(r, "bitrate"), "warn")

    # ---------------------------------------------------------------- sound
    def test_a_silent_soundtrack_fails_when_the_plan_wanted_sound(self):
        silent = self.make("silent.mp4", audio="anullsrc=r=48000:cl=mono")
        r = judge.judge(silent, plan())
        self.assertEqual(level_of(r, "silent-audio"), "fail")

    def test_no_sound_at_all_fails_when_wanted_and_warns_when_not_wanted(self):
        mute = self.make("mute.mp4", audio=None)
        self.assertEqual(level_of(judge.judge(mute, plan()), "audio-present"), "fail")
        no_music = plan(music=None, segments=[{"kind": "photo", "start": 0.0, "duration": 4.0}])
        self.assertEqual(level_of(judge.judge(mute, no_music), "audio-present"), "ok", "silent, as planned")
        self.assertEqual(level_of(judge.judge(self.make("good.mp4"), no_music), "audio-present"), "warn", "a sound track nobody asked for")

    def test_too_quiet_and_too_loud_are_graded(self):
        quiet = self.make("quiet.mp4", audio="sine=frequency=330:duration=4")                     # about -21 LUFS
        r = judge.judge(quiet, plan())
        self.assertEqual(level_of(r, "loudness"), "warn")
        self.assertIn("too quiet", next(c["message"] for c in r["checks"] if c["id"] == "loudness"))
        inaudible = self.make("inaudible.mp4", audio="sine=frequency=330:duration=4,volume=-21dB")  # about -43 LUFS: effectively silent
        self.assertEqual(level_of(judge.judge(inaudible, plan()), "silent-audio"), "fail")
        loud = self.make("loud.mp4", audio="sine=frequency=330:duration=4,volume=18dB")
        r = judge.judge(loud, plan())
        self.assertIn(level_of(r, "loudness"), ("warn", "fail"))
        self.assertIn("too loud", next(c["message"] for c in r["checks"] if c["id"] == "loudness"))

    def test_clipping_is_caught_by_the_true_peak(self):
        hot = self.make("hot.mp4", audio="sine=frequency=330:duration=4,volume=20dB:precision=fixed,alimiter=limit=1:level=disabled")
        r = judge.judge(hot, plan())
        self.assertIn(level_of(r, "clipping"), ("warn", "fail"))

    def test_a_silent_gap_under_the_music_warns_but_silence_with_no_music_does_not(self):
        gap = self.make("gap.mp4", dur=7, audio="aevalsrc=if(between(t\\,2.5\\,5)\\,0\\,0.3*sin(2*PI*330*t)):s=48000:d=7,loudnorm=I=-16:LRA=11:TP=-1.5")
        segs = [{"kind": "photo", "start": 0.0, "duration": 7.0}]
        self.assertEqual(level_of(judge.judge(gap, plan(total=7.0, segments=segs)), "silence"), "warn")
        no_music = plan(total=7.0, segments=segs, music=None)
        self.assertNotIn("silence", {c["id"] for c in judge.judge(gap, no_music)["checks"]}, "the checks that do not apply are not run")

    # ---------------------------------------------------------------- picture
    def test_black_frames_where_a_picture_should_be_are_caught_but_a_dark_card_is_not(self):
        black = self.make("black.mp4", video=f"color=c=black:s={W}x{H}:r=30")
        r = judge.judge(black, plan(segments=[{"kind": "photo", "start": 0.0, "duration": 4.0}]))
        self.assertEqual(level_of(r, "black-frames"), "fail", "a whole piece that is black")
        card = judge.judge(black, plan(segments=[{"kind": "title", "start": 0.0, "duration": 4.0}]))
        self.assertEqual(level_of(card, "black-frames"), "ok", "a title card is allowed to be dark")
        part = self.make("partblack.mp4", vf="drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,0.5,2.0)'")
        self.assertEqual(level_of(judge.judge(part, plan(segments=[{"kind": "photo", "start": 0.0, "duration": 4.0}])), "black-frames"), "warn")

    def test_a_clip_that_freezes_is_caught_but_a_still_card_is_not(self):
        still = self.make("still.mp4", video=f"color=c=0x336699:s={W}x{H}:r=30")
        r = judge.judge(still, plan(segments=[{"kind": "video", "start": 0.0, "duration": 4.0}]))
        self.assertEqual(level_of(r, "frozen-video"), "warn")
        self.assertEqual(level_of(judge.judge(still, plan(segments=[{"kind": "quote", "start": 0.0, "duration": 4.0}])), "frozen-video"), "ok")

    # ---------------------------------------------------------------- what the plan said
    def test_plan_warnings_are_carried_into_the_verdict(self):
        r = judge.judge(self.make("good.mp4"), plan(warnings=["2 clips left out to stay within 20 seconds: a, b"]))
        self.assertEqual(r["verdict"], "warn")
        self.assertTrue(any(c["id"] == "plan-warning" and "left out" in c["message"] for c in r["checks"]))

    def test_cuts_are_checked_against_the_beat_grid(self):
        on = plan(sync={"bpm": 75.0, "cut_seconds": 1.6}, segments=[{"kind": "photo", "start": s, "duration": 1.6} for s in (0.0, 1.6, 3.2)], total=4.0)
        off = plan(sync={"bpm": 75.0, "cut_seconds": 1.6}, segments=[{"kind": "photo", "start": s, "duration": 1.6} for s in (0.0, 1.6, 3.5)], total=4.0)
        good = self.make("good.mp4")
        self.assertEqual(level_of(judge.judge(good, on), "beat-grid"), "ok")
        r = judge.judge(good, off)
        self.assertEqual(level_of(r, "beat-grid"), "warn")
        self.assertIn("3.5", next(c["message"] for c in r["checks"] if c["id"] == "beat-grid"))

    def test_without_a_plan_it_still_judges_what_it_can(self):
        r = judge.judge(self.make("good.mp4"), None)
        self.assertFalse(r["plan"])
        self.assertEqual(level_of(r, "size"), "warn", "216x384 is not a standard size")
        for c in r["checks"]:
            self.assertNotIn(c["id"], ("audio-present", "beat-grid", "plan-warning"))

    def test_the_worst_check_decides_the_verdict(self):
        r = judge.judge(self.make("size.mp4", video="testsrc2=size=320x240:rate=30"), plan(warnings=["w"]))
        self.assertEqual(r["verdict"], "fail")
        self.assertEqual(r["counts"]["fail"], 1)
        self.assertGreaterEqual(r["counts"]["warn"], 1)
        self.assertTrue(judge.brief(r).startswith("fail: "))
        self.assertEqual(judge.brief(judge.judge(self.make("good.mp4"), plan())), "pass")

    def test_an_unreadable_file_is_refused_with_a_message(self):
        p = self.d / "junk.mp4"
        p.write_bytes(b"not a video")
        with self.assertRaises(judge.JudgeError):
            judge.judge(p, plan())


@NEED
class Keeping(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.v = self.d / "reel-20261014-000001.mp4"
        ff("-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=30:d=3", "-f", "lavfi", "-i", SOUND.format(d=3), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "3", str(self.v))
        self.v.with_suffix(".json").write_text(json.dumps({"plan": plan(total=3.0, segments=[{"kind": "photo", "start": 0.0, "duration": 3.0}])}))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_the_report_is_kept_beside_the_video_and_not_recomputed(self):
        first = judge.run(self.v)
        self.assertTrue(self.v.with_suffix(".judge.json").is_file())
        with mock.patch.object(judge, "judge", side_effect=AssertionError("judged again")):
            self.assertEqual(judge.run(self.v), first)

    def test_a_replaced_video_is_judged_again(self):
        first = judge.run(self.v)
        ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:d=3", "-f", "lavfi", "-i", SOUND.format(d=3), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "3", str(self.v))
        second = judge.run(self.v)
        self.assertNotEqual(second["verdict"], first["verdict"])
        self.assertEqual(level_of(second, "size"), "fail")

    def test_force_judges_again_and_the_plan_comes_from_the_sidecar(self):
        r = judge.run(self.v)
        self.assertTrue(r["plan"])
        self.assertIsNotNone(judge.cached(self.v))
        with mock.patch.object(judge, "judge", return_value={"verdict": "pass", "counts": {}, "checks": [], "stats": {}, "plan": True}) as m:
            judge.run(self.v, force=True)
        self.assertEqual(m.call_count, 1)

    def test_a_video_with_no_sidecar_is_judged_without_a_plan(self):
        self.v.with_suffix(".json").unlink()
        self.assertFalse(judge.run(self.v)["plan"])


@NEED_RENDER
class OwnOutput(Fixtures):
    """The judge must not cry wolf at what the renderer makes: every look, with music, a clip and cards, must pass."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # a clip with fresh sensor-like noise in every frame, as camera footage has (freezedetect compares luma, so a hue rotation counts as frozen): the synthetic test clip changes only a small, partly dark area, which the
        # kinetic look's darkening turns into identical frames (a real camera clip never does)
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:d=4,noise=alls=30:allf=t", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "48000", "-t", "4", str(cls.fx / "moving.mp4"))

    def test_every_look_renders_a_video_that_passes(self):
        self.add("land.jpg", "port.jpg", "moving.mp4", music=["song.m4a"])
        for tpl in montage.TEMPLATES:
            with self.subTest(template=tpl):
                rec = recipe([{"media": "land.jpg"}, {"media": "moving.mp4"}, {"quote": "Hi", "by": "Me"}, {"media": "port.jpg"}], template=tpl,
                             title_card={"text": "Hello"}, end_card={"text": "Bye"}, music={"file": "song.m4a"}, max_seconds=40)
                job, out = self.render(rec)
                self.assertEqual(job["state"], "done", job["error"])
                r = judge.run(out)
                self.assertNotEqual(r["verdict"], "fail", [c for c in r["checks"] if c["level"] == "fail"])
                self.assertEqual(r["counts"]["warn"], 0, [c for c in r["checks"] if c["level"] == "warn"])
                self.assertAlmostEqual(r["stats"]["lufs"], -16, delta=2.5)

    def test_a_silent_tempo_only_video_and_a_tempo_cut_video_pass_too(self):
        self.add("land.jpg", "port.jpg")
        job, out = self.render(recipe([{"media": "land.jpg"}, {"media": "port.jpg"}, {"countdown": 2}], template="kinetic", music={"bpm": 75}))
        r = judge.run(out)
        self.assertEqual(r["verdict"], "pass", [c for c in r["checks"] if c["level"] != "ok"])
        self.assertEqual(level_of(r, "beat-grid"), "ok")
        self.assertEqual(level_of(r, "audio-present"), "ok")


if __name__ == "__main__":
    unittest.main()
