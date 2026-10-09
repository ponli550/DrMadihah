"""The prepare routes over HTTP, and the prepare card driven under node, with real ffmpeg clips."""
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from umcares import captions, prepare, projects, uploads, web
from tests.test_prepare import make_conformant, make_phone, put

import http.client

NEED = unittest.skipUnless(prepare.find_tool("ffmpeg") and prepare.find_tool("ffprobe"), "needs ffmpeg")
NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("js_prepare_harness.js")


class Base(unittest.TestCase):
    JSON = {"Content-Type": "application/json", "X-Requested-With": "umcares"}

    @classmethod
    def setUpClass(cls):
        cls.fx = Path(tempfile.mkdtemp())
        make_conformant(cls.fx / "conformant.mp4", dur=1)
        make_phone(cls.fx / "phone.mp4", dur=2)
        (cls.fx / "corrupt.mp4").write_bytes(b"\x00" * 2048)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fx, ignore_errors=True)

    def setUp(self):
        prepare._probes.clear()
        prepare._unreadable.clear()
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=False)["path"])
        self.srv = web.make_server(self.home, "me@x.com", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        prepare.PREPARER.cancel(self.root)
        prepare.PREPARER.wait_idle(30)
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def get(self, path="/p/demo/api/prepare", headers=None):
        status, body = self.req("GET", path, headers)
        return status, json.loads(body) if body[:1] in (b"{", b"[") else body

    def post(self, payload, path="/p/demo/api/prepare", headers=None):
        return self.req("POST", path, headers or self.JSON, json.dumps(payload).encode())

    def wait_done(self, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            d = self.get()[1]
            if not d["active"]:
                return d
            time.sleep(0.1)
        self.fail("the jobs never finished")


@NEED
class Routes(Base):
    def test_a_project_with_no_videos_has_nothing_to_prepare(self):
        status, d = self.get()
        self.assertEqual((status, d), (200, {"candidates": [], "jobs": [], "active": False}))

    def test_uploaded_videos_are_listed_with_what_each_needs(self):
        put(self.root, self.fx / "conformant.mp4")
        put(self.root, self.fx / "phone.mp4", mtime=2)
        d = self.get()[1]
        by = {c["name"]: c for c in d["candidates"]}
        self.assertEqual(by["conformant.mp4"]["plan"]["action"], "copy")
        self.assertEqual(by["phone.mp4"]["plan"]["action"], "reencode")
        self.assertIn("rotated 90°", by["phone.mp4"]["plan"]["reasons"])
        self.assertTrue(all(c["state"] == "todo" for c in d["candidates"]))

    def test_preparing_everything_produces_edit_ready_files_that_inherit_the_caption(self):
        put(self.root, self.fx / "conformant.mp4")
        put(self.root, self.fx / "phone.mp4", mtime=2)
        sha = next(i["sha"] for i in captions.items(self.root) if i["name"] == "phone.mp4")
        captions.set_detail(self.root, sha, "Speaker at the podium")
        status, body = self.post({"all": True})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["queued"]), 2)
        d = self.wait_done()
        self.assertEqual({j["state"] for j in d["jobs"]}, {"done"}, d["jobs"])
        self.assertEqual({c["state"] for c in d["candidates"]}, {"prepared"})
        lib = json.loads(self.req("GET", "/p/demo/api/library")[1])
        ready = next(g for g in lib["folders"] if g["folder"] == "edit_ready")["files"]
        self.assertEqual(sorted(f["name"] for f in ready), ["conformant.mp4", "phone.mp4"])
        self.assertEqual(next(f for f in ready if f["name"] == "phone.mp4")["caption"], "Speaker at the podium")
        self.assertEqual(next(f for f in ready if f["name"] == "phone.mp4")["sha"], sha, "editing it edits the source's caption")

    def test_the_answers_never_leak_a_path_on_the_machine(self):
        put(self.root, self.fx / "conformant.mp4")
        self.post({"all": True})
        self.wait_done()
        body = self.req("GET", "/p/demo/api/prepare")[1] + self.post({"all": True})[1]
        self.assertNotIn(str(self.home).encode(), body)
        self.assertNotIn(b'"root"', body)

    def test_asking_again_when_everything_is_prepared_queues_nothing(self):
        put(self.root, self.fx / "conformant.mp4")
        self.post({"all": True})
        self.wait_done()
        status, body = self.post({"all": True})
        self.assertEqual((status, json.loads(body)["queued"]), (200, []))

    def test_one_clip_can_be_asked_for_by_hash(self):
        put(self.root, self.fx / "conformant.mp4")
        sha = self.get()[1]["candidates"][0]["sha"]
        status, body = self.post({"shas": [sha]})
        self.assertEqual((status, len(json.loads(body)["queued"])), (200, 1))
        self.wait_done()

    def test_a_frame_rate_can_be_requested_and_is_validated(self):
        put(self.root, self.fx / "conformant.mp4")
        self.assertEqual(self.post({"all": True, "fps": 25})[0], 200)
        self.wait_done()
        for bad in (5, 1000, "30", True):
            self.assertEqual(self.post({"all": True, "fps": bad})[0], 400, bad)

    def test_bad_requests_are_refused_with_a_message(self):
        put(self.root, self.fx / "conformant.mp4")
        for payload in ({}, {"shas": "abc"}, {"shas": []}, {"shas": [5]}, {"shas": ["0" * 64]}, {"shas": ["x"] * 201}):
            status, body = self.post(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", json.loads(body))
        self.assertEqual(self.get()[1]["jobs"], [], "a bad request must queue nothing")

    def test_writes_need_the_custom_header_and_json_and_the_right_login(self):
        put(self.root, self.fx / "conformant.mp4")
        self.assertEqual(self.post({"all": True}, headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.post({"all": True}, headers={"X-Requested-With": "umcares"})[0], 403)
        other = {"Tailscale-User-Login": "x@y.com"}
        self.assertEqual(self.post({"all": True}, headers={**self.JSON, **other})[0], 403)
        self.assertEqual(self.req("GET", "/p/demo/api/prepare", other)[0], 403)
        self.assertEqual(self.post({}, "/p/demo/api/prepare/cancel", {**self.JSON, **other})[0], 403)
        self.assertEqual(self.get()[1]["jobs"], [])

    def test_an_unknown_project_is_a_404(self):
        self.assertEqual(self.req("GET", "/p/nope/api/prepare")[0], 404)
        self.assertEqual(self.post({"all": True}, "/p/nope/api/prepare")[0], 404)

    def test_cancel_with_nothing_running_is_harmless(self):
        status, body = self.post({}, "/p/demo/api/prepare/cancel")
        self.assertEqual((status, json.loads(body)), (200, {"cancelled": 0}))

    def test_a_corrupt_upload_is_listed_as_unreadable_and_not_offered(self):
        put(self.root, self.fx / "corrupt.mp4")
        c = self.get()[1]["candidates"][0]                  # the page lists first, which is when the file is looked at
        self.assertEqual(c["state"], "unreadable")
        status, body = self.post({"all": True})
        self.assertEqual((status, json.loads(body)["queued"]), (200, []))

    def test_the_prepare_script_is_served_and_the_page_loads_it(self):
        status, body = self.req("GET", "/prepare.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function createPrepare", body)
        self.assertEqual(self.req("GET", "/prepare.js", {"Tailscale-User-Login": "x@y.com"})[0], 403)
        self.assertIn(b'src="/umcares/prepare.js"', self.req("GET", "/p/demo/")[1])


@NEED
@unittest.skipUnless(NODE, "needs node")
class Card(Base):
    def run_js(self, scenario):
        r = subprocess.run([NODE, str(HARNESS), str(self.port), "demo", scenario], capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertNotIn("error", out, out.get("error"))
        return out

    def test_no_videos_means_no_card(self):
        self.assertFalse(self.run_js("none")["view"]["open"])

    def test_the_card_says_what_each_video_needs_in_words(self):
        put(self.root, self.fx / "conformant.mp4")
        put(self.root, self.fx / "phone.mp4", mtime=2)
        v = self.run_js("todo")["view"]
        self.assertEqual((v["title"], v["count"]), ("Prepare videos for editing", "0 of 2 ready"))
        rows = {r["name"]: r["text"] for r in v["rows"]}
        self.assertEqual(rows["conformant.mp4"], "ready as it is")
        self.assertTrue(rows["phone.mp4"].startswith("needs re-encoding: "), rows)
        self.assertIn("variable frame rate", rows["phone.mp4"])
        self.assertIn("rotated 90°", rows["phone.mp4"])
        self.assertEqual(v["buttons"], ["Prepare 2 videos"])

    def test_pressing_prepare_runs_them_and_the_library_refreshes(self):
        put(self.root, self.fx / "conformant.mp4")
        put(self.root, self.fx / "phone.mp4", mtime=2)
        out = self.run_js("run")
        v = out["view"]
        self.assertEqual(v["count"], "2 of 2 ready")
        self.assertEqual([r["text"] for r in v["rows"]], ["✓ prepared", "✓ prepared"])
        self.assertEqual(v["buttons"], [], "nothing left to do")
        self.assertGreaterEqual(sum(c.endswith("/api/library") for c in out["calls"]), 2, "the library reloaded to show the new files")
        self.assertEqual(sorted(p.name for p in (self.root / "assets" / "edit_ready").iterdir() if not p.name.startswith(".")),
                         ["conformant.mp4", "phone.mp4"])

    def test_an_unreadable_video_shows_its_reason_and_offers_nothing(self):
        put(self.root, self.fx / "corrupt.mp4")
        v = self.run_js("fail")["view"]
        self.assertTrue(v["rows"][0]["text"].startswith("✗ "))
        self.assertIn("no readable video", v["rows"][0]["text"])
        self.assertEqual(v["buttons"], [])


if __name__ == "__main__":
    unittest.main()
