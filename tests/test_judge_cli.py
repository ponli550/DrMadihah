"""`umcares judge`: only the videos named, a verdict each, and exit codes a script can use."""
import contextlib
import io
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from umcares import cli, prepare, projects, web

FF = prepare.find_tool("ffmpeg")
NEED = unittest.skipUnless(FF and prepare.find_tool("ffprobe"), "needs ffmpeg and ffprobe")


def video(path, size="1080x1920", codec="libx264", audio=True, dur=1.5):
    cmd = [FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:d={dur}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={dur},loudnorm=I=-16:LRA=11:TP=-1.5"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p"] + (["-c:a", "aac", "-ar", "48000"] if audio else ["-an"]) + ["-t", str(dur), str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


@NEED
class Cli(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=False)["path"])
        d = self.root / "exports"
        d.mkdir(exist_ok=True)
        video(d / "good-20261014-000001.mp4")                                    # 1080x1920, loud enough, H.264: passes
        video(d / "small-20261014-000002.mp4", size="216x384")                   # not a standard size: a warning
        video(d / "mpeg-20261014-000003.mp4", codec="mpeg4")                     # not H.264: a failure
        self.srv = web.make_server(self.home, "me@x.com", 0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["judge", "demo", *args, "--url", self.base])
        return rc, out.getvalue(), err.getvalue()

    def test_exit_codes_say_pass_warn_or_fail(self):
        rc, out, _ = self.run_cli("good-20261014-000001.mp4")
        self.assertEqual(rc, 0, out)
        self.assertIn("pass", out)
        rc, out, _ = self.run_cli("small-20261014-000002.mp4")
        self.assertEqual(rc, 1, out)
        self.assertIn("standard sizes", out)
        rc, out, _ = self.run_cli("mpeg-20261014-000003.mp4")
        self.assertEqual(rc, 2, out)
        self.assertIn("not H.264", out)

    def test_the_worst_of_several_decides_the_exit_code_and_each_gets_a_line(self):
        rc, out, _ = self.run_cli("good-20261014-000001.mp4", "small-20261014-000002.mp4", "mpeg-20261014-000003.mp4")
        self.assertEqual(rc, 2)
        for n in ("good-20261014-000001.mp4", "small-20261014-000002.mp4", "mpeg-20261014-000003.mp4"):
            self.assertIn(n, out)

    def test_only_the_named_videos_are_checked_and_nothing_without_a_name_unless_all(self):
        rc, out, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("nothing is checked unless you choose it", err)
        self.assertFalse(list((self.root / "exports").glob("*.judge.json")), "no names, no checks")
        self.run_cli("good-20261014-000001.mp4")
        self.assertEqual([p.name for p in (self.root / "exports").glob("*.judge.json")], ["good-20261014-000001.judge.json"], "only the named video")
        rc, out, _ = self.run_cli("--all")
        self.assertEqual(len(list((self.root / "exports").glob("*.judge.json"))), 3)
        self.assertEqual(rc, 2)

    def test_an_unknown_name_and_an_unknown_project_are_refused(self):
        rc, _, err = self.run_cli("ghost-20261014.mp4")
        self.assertEqual(rc, 2)
        self.assertIn("ghost-20261014.mp4", err)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["judge", "nope", "x.mp4", "--url", self.base])
        self.assertEqual(rc, 2)
        self.assertIn("no such project", err.getvalue())

    def test_details_lists_the_checks_that_passed_too(self):
        _, quiet, _ = self.run_cli("good-20261014-000001.mp4")
        _, full, _ = self.run_cli("good-20261014-000001.mp4", "--details")
        self.assertNotIn("H.264 video", quiet)
        self.assertIn("H.264 video", full)


if __name__ == "__main__":
    unittest.main()
