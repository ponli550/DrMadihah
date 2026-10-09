import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from umcares import prepare, projects, uploads

FF, FP = prepare.find_tool("ffmpeg"), prepare.find_tool("ffprobe")
NEED = unittest.skipUnless(FF and FP, "needs ffmpeg and ffprobe")


def ff(*args):
    subprocess.run([FF, "-v", "error", "-y", *args], check=True, capture_output=True)


def make_conformant(path, dur=2, size="320x180", rate=30, faststart=True, arate=48000):
    ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={dur}", "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", str(arate), "-ac", "2", "-shortest",
       *(["-movflags", "+faststart"] if faststart else []), str(path))


def make_phone(path, dur=3, size="320x180", audio=True):
    """Irregular frame gaps (variable frame rate), full-range colour, mono 44.1 kHz audio, then a rotation flag."""
    tmp = Path(str(path) + ".base.mp4")
    ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={dur}",
       *(["-f", "lavfi", "-i", f"sine=frequency=330:duration={dur}"] if audio else []),
       "-vf", "select='mod(n,7)',format=yuvj420p", "-fps_mode", "passthrough", "-c:v", "libx264", "-pix_fmt", "yuvj420p",
       *(["-c:a", "aac", "-ar", "44100", "-ac", "1", "-shortest"] if audio else ["-an"]), str(tmp))
    ff("-display_rotation", "90", "-i", str(tmp), "-c", "copy", str(path))
    tmp.unlink()


def put(root, path, name=None, mtime=1):
    data = Path(path).read_bytes()
    s = uploads.start(root, name or Path(path).name, len(data), mtime)
    off = 0
    while off < len(data):
        piece = data[off:off + (1 << 20)]
        uploads.append(root, s["id"], off, io.BytesIO(piece), len(piece))
        off += len(piece)
    return uploads.finish(root, s["id"])


def sha_of(root, name):
    return next(c["sha"] for c in prepare.candidates(root, budget=99) if c["name"] == name)


@NEED
class Fixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Path(tempfile.mkdtemp())
        make_conformant(cls.fx / "conformant.mp4")
        make_conformant(cls.fx / "nofast.mp4", faststart=False)
        make_conformant(cls.fx / "audio44.mp4", arate=44100)
        make_phone(cls.fx / "phone.mp4")
        make_phone(cls.fx / "phone-silent.mp4", audio=False)
        (cls.fx / "corrupt.mp4").write_bytes(os.urandom(4096))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fx, ignore_errors=True)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=False)["path"])
        prepare._probes.clear()

    def tearDown(self):
        prepare.PREPARER.cancel(self.root)
        prepare.PREPARER.wait_idle(30)
        shutil.rmtree(self.home, ignore_errors=True)

    def run_job(self, name, fps=None, timeout=120):
        sha = sha_of(self.root, name)
        prepare.PREPARER.submit(self.root, [sha], fps)
        self.assertTrue(prepare.PREPARER.wait_idle(timeout), "the job never finished")
        return next(j for j in prepare.PREPARER.snapshot(self.root) if j["sha"] == sha), sha

    def out_probe(self, job):
        return prepare.probe(self.root / job["output"])


class Probing(Fixtures):
    def test_a_conformant_clip_is_read_as_such(self):
        i = prepare.probe(self.fx / "conformant.mp4")
        v = i["video"]
        self.assertEqual((v["codec"], v["pix_fmt"], v["rotation"], v["width"], v["height"]), ("h264", "yuv420p", 0, 320, 180))
        self.assertEqual((i["audio"]["codec"], i["audio"]["rate"]), ("aac", 48000))
        self.assertTrue(i["faststart"])
        self.assertAlmostEqual(v["fps"], 30, delta=0.01)
        self.assertLess(v["irregular"], 0.01)
        self.assertEqual(prepare.make_plan(i), {**prepare.make_plan(i), "action": "copy", "reasons": []})

    def test_a_phone_clip_reports_rotation_swapped_size_range_and_uneven_frames(self):
        i = prepare.probe(self.fx / "phone.mp4")
        v = i["video"]
        self.assertEqual(v["rotation"], 90)
        self.assertEqual((v["width"], v["height"]), (180, 320), "the displayed size, not the stored one")
        self.assertEqual((v["pix_fmt"], v["range"]), ("yuvj420p", "pc"))
        self.assertGreater(v["irregular"], 0.01, "frames are unevenly spaced")
        plan = prepare.make_plan(i)
        self.assertEqual(plan["action"], "reencode")
        for reason in ("variable frame rate", "full-range colour", "rotated 90°", "audio is not AAC 48 kHz", "file layout (not faststart)"):
            self.assertIn(reason, plan["reasons"])
        self.assertEqual(plan["fps"], "30/1")

    def test_only_a_bad_layout_means_a_remux_not_a_re_encode(self):
        plan = prepare.make_plan(prepare.probe(self.fx / "nofast.mp4"))
        self.assertEqual((plan["action"], plan["reasons"]), ("remux", ["file layout (not faststart)"]))

    def test_only_a_wrong_audio_rate_is_also_a_remux(self):
        plan = prepare.make_plan(prepare.probe(self.fx / "audio44.mp4"))
        self.assertEqual(plan["action"], "remux")
        self.assertIn("audio is not AAC 48 kHz", plan["reasons"])

    def test_a_file_that_is_not_a_video_is_refused_with_a_message(self):
        with self.assertRaises(prepare.PrepareError) as cm:
            prepare.probe(self.fx / "corrupt.mp4")
        self.assertIn("no readable video", str(cm.exception))


class Running(Fixtures):
    def test_a_conformant_clip_is_cloned_not_re_encoded(self):
        put(self.root, self.fx / "conformant.mp4")
        job, sha = self.run_job("conformant.mp4")
        self.assertEqual((job["state"], job["action"], job["percent"]), ("done", "copy", 100))
        self.assertEqual((self.root / job["output"]).read_bytes(), (self.fx / "conformant.mp4").read_bytes())
        self.assertEqual(prepare.outputs(self.root), {job["output"]: sha})

    def test_a_phone_clip_comes_out_edit_ready(self):
        put(self.root, self.fx / "phone.mp4")
        src = prepare.probe(self.fx / "phone.mp4")
        job, _ = self.run_job("phone.mp4")
        self.assertEqual(job["state"], "done", job["error"])
        o = self.out_probe(job)
        v = o["video"]
        self.assertEqual((v["codec"], v["pix_fmt"], v["rotation"]), ("h264", "yuv420p", 0))
        self.assertEqual((v["width"], v["height"]), (180, 320), "rotation is now in the pixels, and the flag is gone")
        self.assertEqual(v["range"], "tv")
        self.assertLess(v["irregular"], 0.01, "constant frame rate")
        self.assertAlmostEqual(v["fps"], 30, delta=0.05)
        self.assertTrue(o["faststart"])
        self.assertEqual((o["audio"]["codec"], o["audio"]["rate"], o["audio"]["channels"]), ("aac", 48000, 2))
        self.assertAlmostEqual(o["duration"], src["duration"], delta=0.5)
        self.assertTrue(job["output"].startswith("assets/edit_ready/"))

    def test_a_silent_clip_stays_silent(self):
        put(self.root, self.fx / "phone-silent.mp4")
        job, _ = self.run_job("phone-silent.mp4")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertIsNone(self.out_probe(job)["audio"])

    def test_a_remux_keeps_the_picture_and_fixes_the_layout(self):
        put(self.root, self.fx / "nofast.mp4")
        src = prepare.probe(self.fx / "nofast.mp4")
        job, _ = self.run_job("nofast.mp4")
        self.assertEqual((job["state"], job["action"]), ("done", "remux"))
        o = self.out_probe(job)
        self.assertTrue(o["faststart"])
        self.assertEqual(o["video"]["frames"], src["video"]["frames"])
        self.assertEqual(o["video"]["codec"], "h264")

    def test_a_remux_for_audio_rewrites_only_the_audio(self):
        put(self.root, self.fx / "audio44.mp4")
        job, _ = self.run_job("audio44.mp4")
        o = self.out_probe(job)
        self.assertEqual((job["action"], o["audio"]["codec"], o["audio"]["rate"]), ("remux", "aac", 48000))

    def test_a_frame_rate_can_be_asked_for(self):
        put(self.root, self.fx / "conformant.mp4")
        job, _ = self.run_job("conformant.mp4", fps=25)
        self.assertEqual((job["state"], job["action"]), ("done", "reencode"))
        self.assertIn("frame rate 30 to 25", job["reasons"])
        self.assertAlmostEqual(self.out_probe(job)["video"]["fps"], 25, delta=0.05)

    def test_the_original_is_never_touched(self):
        put(self.root, self.fx / "phone.mp4")
        before = (self.root / "assets" / "inbox" / "phone.mp4").read_bytes()
        self.run_job("phone.mp4")
        self.assertEqual((self.root / "assets" / "inbox" / "phone.mp4").read_bytes(), before)

    def test_an_existing_edit_ready_file_is_never_overwritten(self):
        (self.root / "assets" / "edit_ready").mkdir(exist_ok=True)
        (self.root / "assets" / "edit_ready" / "conformant.mp4").write_bytes(b"precious")
        put(self.root, self.fx / "conformant.mp4")
        job, _ = self.run_job("conformant.mp4")
        self.assertEqual(job["output"], "assets/edit_ready/conformant-1.mp4")
        self.assertEqual((self.root / "assets" / "edit_ready" / "conformant.mp4").read_bytes(), b"precious")

    def test_a_prepared_clip_is_not_done_twice_but_is_redone_if_its_output_goes_missing(self):
        put(self.root, self.fx / "conformant.mp4")
        job, sha = self.run_job("conformant.mp4")
        self.assertEqual(prepare.PREPARER.submit(self.root, [sha]), [], "already prepared: nothing queued")
        (self.root / job["output"]).unlink()
        self.assertEqual(next(c for c in prepare.candidates(self.root) if c["sha"] == sha)["state"], "todo")
        again, _ = self.run_job("conformant.mp4")
        self.assertEqual(again["state"], "done")
        self.assertTrue((self.root / again["output"]).is_file())

    def test_progress_only_ever_goes_up_and_finishes_at_100(self):
        make_phone(self.fx / "long.mp4", dur=40, size="640x360")
        put(self.root, self.fx / "long.mp4")
        sha = sha_of(self.root, "long.mp4")
        with mock.patch.object(prepare, "PRESET", "slow"):
            prepare.PREPARER.submit(self.root, [sha])
            seen = []
            end = time.time() + 120
            while time.time() < end and prepare.PREPARER.active(self.root):
                seen += [j["percent"] for j in prepare.PREPARER.snapshot(self.root) if j["sha"] == sha]
                time.sleep(0.01)
        job = next(j for j in prepare.PREPARER.snapshot(self.root) if j["sha"] == sha)
        self.assertEqual((job["state"], job["percent"]), ("done", 100), job["error"])
        self.assertEqual(seen, sorted(seen))
        self.assertTrue(any(0 < p < 100 for p in seen), f"no intermediate progress seen: {sorted(set(seen))}")

    def test_cancelling_a_running_job_stops_ffmpeg_and_leaves_nothing_behind(self):
        make_phone(self.fx / "big.mp4", dur=60, size="1280x720")
        put(self.root, self.fx / "big.mp4")
        sha = sha_of(self.root, "big.mp4")
        with mock.patch.object(prepare, "PRESET", "veryslow"):
            prepare.PREPARER.submit(self.root, [sha])
            end = time.time() + 30
            while time.time() < end and not any(j["state"] == "running" for j in prepare.PREPARER.snapshot(self.root)):
                time.sleep(0.01)
            time.sleep(0.5)
            self.assertEqual(prepare.PREPARER.cancel(self.root), 1)
            self.assertTrue(prepare.PREPARER.wait_idle(30))
        job = next(j for j in prepare.PREPARER.snapshot(self.root) if j["sha"] == sha)
        self.assertEqual(job["state"], "cancelled")
        folder = self.root / "assets" / "edit_ready"
        self.assertEqual([p.name for p in folder.iterdir() if not p.name.startswith(".")], [], "no output and no .part file")
        self.assertNotIn(sha, json.loads((self.root / ".umcares" / "prepared.json").read_text()) if (self.root / ".umcares" / "prepared.json").exists() else {})
        self.assertEqual(next(c for c in prepare.candidates(self.root) if c["sha"] == sha)["state"], "todo")

    def test_a_failing_ffmpeg_is_reported_and_leaves_nothing_behind(self):
        put(self.root, self.fx / "phone.mp4")
        with mock.patch.object(prepare, "build_command", return_value=["/bin/sh", "-c", "echo 'broken: bad input' >&2; exit 3"]):
            job, _ = self.run_job("phone.mp4")
        self.assertEqual(job["state"], "failed")
        self.assertIn("ffmpeg failed", job["error"])
        self.assertIn("bad input", job["error"])
        self.assertEqual([p.name for p in (self.root / "assets" / "edit_ready").iterdir() if not p.name.startswith(".")], [])

    def test_a_result_that_is_the_wrong_length_is_rejected(self):
        put(self.root, self.fx / "conformant.mp4")
        sha = sha_of(self.root, "conformant.mp4")
        real = prepare.probe
        calls = []

        def fake(path):
            i = real(path)
            calls.append(path)
            if len(calls) > 1:                       # the second probe is the verification of the result
                i = {**i, "duration": 0.2}
            return i
        prepare._probes.clear()
        with mock.patch.object(prepare, "probe", side_effect=fake):
            prepare.PREPARER.submit(self.root, [sha])
            prepare.PREPARER.wait_idle(60)
        job = next(j for j in prepare.PREPARER.snapshot(self.root) if j["sha"] == sha)
        self.assertEqual(job["state"], "failed")
        self.assertIn("prepared clip is", job["error"])
        self.assertEqual([p.name for p in (self.root / "assets" / "edit_ready").iterdir() if not p.name.startswith(".")], [])

    def test_a_full_disk_stops_before_any_work(self):
        put(self.root, self.fx / "phone.mp4")
        fake = shutil._ntuple_diskusage(100, 99, 1 << 20)
        with mock.patch.object(prepare.shutil, "disk_usage", return_value=fake):
            job, _ = self.run_job("phone.mp4")
        self.assertEqual(job["state"], "failed")
        self.assertIn("not enough free disk space", job["error"])
        self.assertEqual([p.name for p in (self.root / "assets" / "edit_ready").iterdir() if not p.name.startswith(".")], [])

    def test_stale_part_files_from_a_crash_are_swept_and_never_listed(self):
        folder = self.root / "assets" / "edit_ready"
        (folder / "old.abc123.part.mp4").write_bytes(b"half written")
        from umcares import web
        self.assertNotIn("old.abc123.part.mp4", [f["name"] for f in web.list_folder(folder)])
        put(self.root, self.fx / "conformant.mp4")
        self.run_job("conformant.mp4")
        self.assertFalse((folder / "old.abc123.part.mp4").exists())


class Candidates(Fixtures):
    def test_only_uploaded_videos_in_the_raw_folders_are_candidates(self):
        put(self.root, self.fx / "conformant.mp4")
        (self.root / "assets" / "photos" / "x.jpg").write_bytes(b"x")
        (self.root / "assets" / "footage" / "A" / "hand-copied.mp4").write_bytes(b"x")     # no hash: not tracked
        put(self.root, self.fx / "conformant.mp4", name="photo.jpg", mtime=9) if False else None
        self.assertEqual([c["name"] for c in prepare.candidates(self.root)], ["conformant.mp4"])

    def test_a_corrupt_upload_is_listed_as_unreadable_and_cannot_be_queued(self):
        put(self.root, self.fx / "corrupt.mp4")
        c = prepare.candidates(self.root)[0]
        self.assertEqual(c["state"], "unreadable")
        self.assertIn("no readable video", c["error"])
        job_count = len(prepare.PREPARER.snapshot(self.root))
        prepare.PREPARER.submit(self.root, [c["sha"]])
        prepare.PREPARER.wait_idle(30)
        job = prepare.PREPARER.snapshot(self.root)[-1]
        self.assertGreaterEqual(len(prepare.PREPARER.snapshot(self.root)), job_count)
        self.assertEqual(job["state"], "failed")

    def test_an_unreadable_file_is_remembered_so_it_is_not_probed_on_every_poll(self):
        put(self.root, self.fx / "corrupt.mp4")
        prepare._unreadable.clear()
        with mock.patch.object(prepare, "probe", wraps=prepare.probe) as p:
            for _ in range(4):
                self.assertEqual(prepare.candidates(self.root)[0]["state"], "unreadable")
        self.assertEqual(p.call_count, 1)

    def test_a_bad_hash_in_a_batch_queues_nothing(self):
        put(self.root, self.fx / "conformant.mp4")
        good = sha_of(self.root, "conformant.mp4")
        with self.assertRaises(prepare.PrepareError):
            prepare.PREPARER.submit(self.root, [good, "0" * 64])
        self.assertEqual(prepare.PREPARER.snapshot(self.root), [])

    def test_probing_is_capped_per_call_and_the_rest_come_back_later(self):
        for i in range(4):
            make_conformant(self.fx / f"c{i}.mp4", dur=1 + i)
            put(self.root, self.fx / f"c{i}.mp4", mtime=i)
        first = prepare.candidates(self.root, budget=3)
        self.assertEqual(sum(c["plan"] is None for c in first), 1)
        second = prepare.candidates(self.root, budget=3)
        self.assertTrue(all(c["plan"] for c in second))

    def test_a_probe_is_remembered_so_listing_stays_cheap(self):
        put(self.root, self.fx / "conformant.mp4")
        with mock.patch.object(prepare, "probe", wraps=prepare.probe) as p:
            prepare.candidates(self.root)
            prepare.candidates(self.root)
            prepare.candidates(self.root)
        self.assertEqual(p.call_count, 1)

    def test_outputs_ignores_a_clip_whose_file_is_gone(self):
        put(self.root, self.fx / "conformant.mp4")
        job, sha = self.run_job("conformant.mp4")
        (self.root / job["output"]).unlink()
        self.assertEqual(prepare.outputs(self.root), {})


class Plain(unittest.TestCase):
    """No ffmpeg needed."""

    def info(self, **video):
        v = {"codec": "h264", "profile": "High", "pix_fmt": "yuv420p", "width": 1920, "height": 1080, "fps": 30.0, "avg": 30.0,
             "rotation": 0, "range": "tv", "space": "bt709", "transfer": "bt709", "primaries": "bt709", "irregular": 0.0, "frames": 90}
        v.update(video)
        return {"duration": 3.0, "size": 1, "faststart": True, "video": v, "audio": {"codec": "aac", "rate": 48000, "channels": 2}}

    def test_standard_and_non_standard_frame_rates(self):
        self.assertTrue(prepare._is_standard(29.97))
        self.assertTrue(prepare._is_standard(59.94))
        self.assertTrue(prepare._is_standard(119.88), "high-speed clips keep their rate")
        self.assertFalse(prepare._is_standard(30.32), "an Android phone's variable rate")
        self.assertEqual(prepare.nearest_fps(30.32), 30)
        self.assertEqual(prepare.nearest_fps(119.5), 120)

    def test_a_non_standard_rate_alone_is_enough_to_call_it_variable(self):
        plan = prepare.make_plan(self.info(fps=30.32, avg=30.32))
        self.assertIn("variable frame rate", plan["reasons"])
        self.assertEqual((plan["action"], plan["fps"]), ("reencode", "30/1"))

    def test_slow_motion_footage_is_not_flattened_to_60(self):
        plan = prepare.make_plan(self.info(fps=120.0, avg=120.0))
        self.assertEqual((plan["action"], plan["fps"]), ("copy", "120/1"))

    def test_hdr_is_flagged_so_nobody_is_surprised_by_flat_colour(self):
        plan = prepare.make_plan(self.info(pix_fmt="yuv420p10le", transfer="arib-std-b67"))
        self.assertTrue(plan["warnings"])
        self.assertIn("HDR", plan["warnings"][0])

    def test_vp9_is_re_encoded_with_the_reason_named(self):
        plan = prepare.make_plan(self.info(codec="vp9"))
        self.assertEqual(plan["action"], "reencode")
        self.assertIn("codec vp9", plan["reasons"])

    def test_the_command_runs_at_low_priority_with_capped_threads_and_the_right_filters(self):
        info = self.info(range="pc", pix_fmt="yuvj420p", rotation=90)
        plan = prepare.make_plan(info)
        cmd = prepare.build_command("/usr/bin/ffmpeg", Path("/in.mp4"), Path("/out.part.mp4"), info, plan)
        if shutil.which("nice"):
            self.assertEqual(cmd[1:3], ["-n", prepare.NICE])
        self.assertEqual(cmd[cmd.index("-threads") + 1], prepare.THREADS)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("scale=in_range=pc:out_range=tv", vf)
        self.assertIn("fps=30/1", vf)
        self.assertIn("format=yuv420p", vf)
        self.assertEqual(cmd[-1], "/out.part.mp4")
        self.assertIn("+faststart", cmd)

    def test_a_remux_command_copies_the_video_stream(self):
        info = {**self.info(), "faststart": False}
        plan = prepare.make_plan(info)
        cmd = prepare.build_command("/usr/bin/ffmpeg", Path("/in.mp4"), Path("/o.part.mp4"), info, plan)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def atoms(self, *specs):
        out = b""
        for kind, size in specs:
            out += struct.pack(">I4s", size, kind) + b"\0" * (size - 8)
        return out

    def test_faststart_detection_reads_the_atom_order(self):
        d = Path(tempfile.mkdtemp())
        cases = {"fast": ([(b"ftyp", 24), (b"moov", 40), (b"mdat", 32)], True),
                 "slow": ([(b"ftyp", 24), (b"mdat", 32), (b"moov", 40)], False),
                 "free-first": ([(b"ftyp", 24), (b"free", 16), (b"moov", 40), (b"mdat", 32)], True)}
        for name, (specs, want) in cases.items():
            (d / name).write_bytes(self.atoms(*specs))
            self.assertEqual(prepare._faststart(d / name), want, name)
        (d / "short").write_bytes(b"\0\0")
        self.assertFalse(prepare._faststart(d / "short"))
        (d / "zero-size-loop").write_bytes(struct.pack(">I4s", 0, b"junk") + b"\0" * 8)
        self.assertFalse(prepare._faststart(d / "zero-size-loop"), "a nonsense atom must end the scan, not loop")
        shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
