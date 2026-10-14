"""The real service worker and share page, run under node with a fake IndexedDB, against a live server."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from umcares import projects, web

HARNESS = Path(__file__).with_name("js_share_harness.js")
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "needs node")
class Share(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        projects.create_project(cls.home, "Demo", "2026-10-09", init_git=False)          # newest event: the default choice
        projects.create_project(cls.home, "Second", "2026-01-01", init_git=False)
        cls.srv = web.make_server(cls.home, "me@x.com", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def run_js(self, scenario):
        r = subprocess.run([NODE, str(HARNESS), str(self.port), scenario], capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertNotIn("error", out, out.get("error"))
        return out

    def files(self, project, folder):
        d = self.home / project / "assets" / folder
        return sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.is_dir() else []

    # ---- the service worker ----
    def test_a_share_is_stashed_locally_and_redirected_to_the_share_page(self):
        out = self.run_js("sw-stash")
        self.assertTrue(out["handled"])
        self.assertEqual(out["status"], 303)
        self.assertRegex(out["location"], r"^/umcares/share\?id=[a-z0-9]+$")
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual([f[0] for f in out["rows"][0]["files"]], ["a.jpg", "b.mp4"])
        self.assertEqual([f[1] for f in out["rows"][0]["files"]], [300 * 1024 + 8, 700 * 1024])

    def test_every_other_request_is_left_alone(self):
        out = self.run_js("sw-other")
        self.assertEqual(out["handled"], [False] * 6, "uploads, pages and look-alike URLs must never be intercepted")
        self.assertEqual(out["rows"], 0)

    def test_a_share_with_no_files_is_not_stored(self):
        out = self.run_js("sw-empty")
        self.assertEqual((out["only"], out["zero"]), ("/umcares/share?empty=1", "/umcares/share?empty=1"))
        self.assertEqual(out["rows"], 0)

    def test_blocked_storage_ends_on_an_error_page_not_a_blank_one(self):
        out = self.run_js("sw-idbfail")
        self.assertEqual((out["handled"], out["status"], out["location"]), (True, 303, "/umcares/share?error=1"))

    def test_shares_nobody_finished_for_a_week_are_forgotten_and_recent_ones_kept(self):
        out = self.run_js("sw-expiry")
        self.assertEqual(out["ids"], ["recent"])
        self.assertEqual(out["total"], 2)                      # the recent one and the new one

    # ---- the share page, end to end through the worker ----
    def test_a_share_uploads_into_the_chosen_project_and_is_then_forgotten(self):
        out = self.run_js("page-happy")
        b, a = out["before"], out["after"]
        self.assertFalse(b["hidden"]["files"])
        self.assertFalse(b["hidden"]["pick"])
        self.assertEqual(b["count"], "2 files · 1000 KB")        # 300 KB + 700 KB is just under 1 MB
        self.assertEqual(b["previews"], 2)
        self.assertEqual([(r["value"], r["checked"]) for r in b["radios"]], [("demo", True), ("second", False)])
        self.assertEqual(a["queue"], ["✓ saved in photos", "✓ saved in inbox"])
        self.assertEqual(a["summary"], "2 saved")
        self.assertEqual(a["left"], [], "the parked files are removed only after every one is stored")
        self.assertEqual(a["status"], "Done: Demo")
        self.assertEqual(a["after"], [{"text": "Open Demo and add details", "href": "/umcares/p/demo/?details=1"}])
        self.assertIn("shared-photo.jpg", self.files("demo", "photos"))      # other tests share this project, so no exact listing
        self.assertIn("shared-clip.mp4", self.files("demo", "inbox"))
        self.assertEqual((self.home / "demo" / "assets" / "inbox" / "shared-clip.mp4").stat().st_size, 700 * 1024)

    def test_a_failed_file_keeps_the_share_so_nothing_is_lost(self):
        out = self.run_js("page-fail")
        a = out["after"]
        self.assertEqual(a["queue"][0], "✓ saved in photos")
        self.assertTrue(a["queue"][1].startswith("✗"), a["queue"])
        self.assertEqual(a["summary"], "1 saved, 1 failed")
        self.assertFalse(a["retryHidden"])
        self.assertIn("did not upload", a["status"])
        self.assertEqual(a["after"], [])
        self.assertEqual(len(a["left"]), 1, "the share must still be parked for a retry")
        self.assertEqual(a["left"][0]["files"], ["fail-photo.jpg", "fail-clip.mp4"])
        self.assertEqual(self.files("demo", "photos").count("fail-photo.jpg"), 1)
        self.assertNotIn("fail-clip.mp4", self.files("demo", "inbox"))

    def test_the_last_project_used_is_remembered_and_preselected(self):
        out = self.run_js("page-last")
        self.assertEqual([(r["value"], r["checked"]) for r in out["before"]["radios"]], [("demo", False), ("second", True)])
        self.assertEqual(out["after"]["ls"]["umcares.lastProject"], "second")
        self.assertEqual(self.files("second", "photos"), ["shared-photo.jpg"])

    def test_discarding_forgets_the_files_and_uploads_nothing(self):
        out = self.run_js("page-discard")
        self.assertEqual(out["after"]["left"], [])
        self.assertEqual(out["after"]["status"], "Discarded.")
        self.assertTrue(out["after"]["hidden"]["pick"])
        self.assertFalse(any(c.startswith("POST /api/upload") for c in out["after"]["calls"]))

    def test_a_hostile_filename_is_cleaned_before_it_reaches_disk(self):
        out = self.run_js("page-hostile")
        self.assertEqual(out["after"]["summary"], "1 saved")
        saved = [n for n in self.files("demo", "photos") if "onerror" in n]
        self.assertEqual(len(saved), 1)
        self.assertFalse(set(saved[0]) & set('<>:"|?*'))

    def test_a_page_with_nothing_to_show_says_why(self):
        self.assertIn("already uploaded or have expired", self.run_js("page-unknown")["after"]["status"])
        self.assertIn("could not be saved", self.run_js("page-error")["after"]["status"])
        self.assertIn("Nothing that can be uploaded", self.run_js("page-empty")["after"]["status"])
        noid = self.run_js("page-noid")["after"]
        self.assertIn("share photos or videos", noid["status"])
        for scenario_out in (noid,):
            self.assertTrue(scenario_out["hidden"]["files"] and scenario_out["hidden"]["pick"])


if __name__ == "__main__":
    unittest.main()
