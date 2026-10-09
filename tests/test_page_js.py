"""The page's real upload script, run under node against a live server."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from umcares import projects, web

HARNESS = Path(__file__).with_name("js_upload_harness.js")
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "needs node")
class PageUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        projects.create_project(cls.home, "Demo", init_git=False)
        cls.root = cls.home / "demo"
        cls.srv = web.make_server(cls.home, "me@x.com", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def run_page(self, scenario):
        r = subprocess.run([NODE, str(HARNESS), str(self.port), "demo", scenario], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertNotIn("error", out, out.get("error"))
        return out

    def msgs(self, out):
        return {row["name"]: row["msg"] for row in out["rows"]}

    def test_1_a_normal_upload_saves_files_and_skips_the_duplicate(self):
        out = self.run_page("normal")
        m = self.msgs(out)
        self.assertTrue(m["one.jpg"].startswith("✓"), m)
        self.assertTrue(m["two.mp4"].startswith("✓"), m)
        self.assertIn("same content as one.jpg", m["copy.jpg"])
        self.assertEqual(sorted(p.name for p in (self.root / "assets" / "photos").iterdir() if not p.name.startswith(".")), ["one.jpg"])
        self.assertEqual([p.name for p in (self.root / "assets" / "inbox").iterdir() if not p.name.startswith(".")], ["two.mp4"])
        self.assertIn("2 saved", out["summary"])
        self.assertIn("1 already there", out["summary"])

    def test_2_the_bytes_on_disk_are_exactly_what_was_sent(self):
        big = bytes(((i * 7 + 3) & 255) for i in range(900 * 1024))
        self.assertEqual(hashlib.sha256((self.root / "assets" / "photos" / "one.jpg").read_bytes()).hexdigest(),
                         hashlib.sha256(big).hexdigest())

    def test_3_picking_the_same_file_again_is_recognised_without_resending(self):
        out = self.run_page("repick")
        self.assertIn("already uploaded", self.msgs(out)["one.jpg"])
        self.assertFalse(any(c.startswith("PUT") for c in out["calls"]), out["calls"])

    def test_4_a_flaky_network_still_delivers_every_byte_exactly_once(self):
        """Every third chunk request dies before the server sees it; the next one's reply is lost AFTER it was stored."""
        out = self.run_page("flaky")
        m = self.msgs(out)
        self.assertTrue(m["flaky.jpg"].startswith("✓"), m)
        self.assertTrue(m["flaky2.mp4"].startswith("✓"), m)
        other = bytes(((i * 11 + 5) & 255) for i in range(900 * 1024))
        self.assertEqual((self.root / "assets" / "photos" / "flaky.jpg").read_bytes(), other)
        self.assertEqual((self.root / "assets" / "inbox" / "flaky2.mp4").read_bytes(), b"more video " * 40000)
        self.assertGreater(sum(c.startswith("GET /api/upload/") for c in out["calls"]), 0, "it should have resynced from the server")

    def test_5_an_unsupported_file_fails_with_the_servers_reason_and_does_not_block_the_rest(self):
        out = self.run_page("refused")
        m = self.msgs(out)
        self.assertIn("only photos and videos", m["notes.txt"])
        self.assertTrue(m["notes.txt"].startswith("✗"))
        self.assertTrue(m["ok.jpg"].startswith("✓"), m)
        self.assertFalse(out["retryHidden"])
        self.assertIn("1 failed", out["summary"])

    def test_6_no_partial_files_are_left_in_the_library(self):
        for folder in ("photos", "inbox"):
            names = [p.name for p in (self.root / "assets" / folder).iterdir()]
            self.assertFalse([n for n in names if ".part" in n], names)


if __name__ == "__main__":
    unittest.main()
