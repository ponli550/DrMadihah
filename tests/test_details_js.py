"""The real project page (uploader + caption pass) driven under node against a live server."""
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from umcares import captions, projects, uploads, web

HARNESS = Path(__file__).with_name("js_details_harness.js")
NODE = shutil.which("node")
FILES = (("IMG_1.jpg", b"photo one"), ("IMG_2.jpg", b"photo two"), ("VID_1.mp4", b"video one"))


def put(root, name, data, mtime):
    s = uploads.start(root, name, len(data), mtime)
    uploads.append(root, s["id"], 0, io.BytesIO(data), len(data))
    uploads.finish(root, s["id"])


@unittest.skipUnless(NODE, "needs node")
class Details(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=False)["path"])
        for i, (n, d) in enumerate(FILES):
            put(self.root, n, d, i + 1)
        self.srv = web.make_server(self.home, "me@x.com", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def run_js(self, scenario):
        r = subprocess.run([NODE, str(HARNESS), str(self.port), "demo", scenario], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertNotIn("error", out, out.get("error"))
        return out

    def state(self):
        return {i["name"]: (i["caption"], i["skipped"], i["pending"]) for i in captions.items(self.root)}

    def test_stepping_through_a_bulk_upload_captioning_or_skipping_each(self):
        out = self.run_js("pass")
        a, b, c, d = out["first"], out["second"], out["third"], out["done"]
        self.assertEqual((a["position"], a["name"]), ("1 of 3", "IMG_1.jpg"))
        self.assertEqual([x["text"] for x in a["buttons"] if not x["hidden"]], ["Save & next", "Skip (no detail)", "Stop for now"])
        self.assertTrue(next(x for x in a["buttons"] if x["text"] == "Back")["hidden"], "no Back on the first file")
        self.assertEqual((b["position"], b["name"]), ("2 of 3", "IMG_2.jpg"))
        self.assertEqual((c["position"], c["name"]), ("3 of 3", "VID_1.mp4"))
        self.assertIn("Save & finish", [x["text"] for x in c["buttons"]])
        self.assertEqual(d["title"], "All done")
        self.assertIn("2 captioned, 1 skipped", d["text"])
        self.assertTrue(d["banner"]["hidden"], "nothing left pending")
        self.assertEqual(self.state(), {"IMG_1.jpg": ("Dr Madihah opening the session", False, False),
                                        "IMG_2.jpg": ("", True, False),
                                        "VID_1.mp4": ("Participants at the registration desk", False, False)})
        self.assertEqual(sum(c.startswith("POST /api/media/") for c in out["calls"]), 3)

    def test_an_empty_caption_is_not_saved_and_explains_the_choice(self):
        out = self.run_js("empty")
        self.assertIn("Type a caption, or tap Skip (no detail).", out["view"]["error"])
        self.assertEqual(out["view"]["position"], "1 of 3")
        self.assertFalse(any(c.startswith("POST /api/media/") for c in out["calls"]))
        self.assertEqual(captions.pending_count(self.root), 3)

    def test_back_returns_to_the_previous_file_with_its_caption_filled_in(self):
        out = self.run_js("back")
        self.assertEqual((out["view"]["position"], out["view"]["caption"]), ("1 of 3", "first caption"))

    def test_a_failed_save_keeps_you_on_the_file_and_the_buttons_work_again(self):
        out = self.run_js("save-fails")
        f = out["failed"]
        self.assertTrue(f["error"].startswith("Not saved: disk is full"), f["error"])
        self.assertEqual(f["position"], "1 of 3")
        self.assertFalse(any(b["disabled"] for b in f["buttons"]))
        self.assertEqual(out["retried"]["position"], "2 of 3", "the retry succeeded and moved on")
        self.assertEqual(self.state()["IMG_1.jpg"][0], "this will not save")

    def test_stopping_for_now_leaves_the_rest_pending_and_shows_how_many(self):
        out = self.run_js("stop")
        self.assertFalse(out["view"]["open"])
        self.assertEqual(out["view"]["banner"], {"hidden": False, "text": "Add details (2)"})
        self.assertEqual(captions.pending_count(self.root), 2)

    def test_editing_one_file_prefills_its_caption_and_can_replace_it(self):
        sha = captions.items(self.root)[0]["sha"]
        captions.set_detail(self.root, sha, "old caption")
        out = self.run_js("edit-one")
        v = out["view"]
        self.assertEqual((v["position"], v["caption"]), ("1 of 1", "old caption"))
        self.assertEqual([b["text"] for b in v["buttons"] if not b["hidden"]], ["Save & finish", "Remove caption", "Stop for now"])
        self.assertEqual(self.state()["IMG_1.jpg"][0], "a better caption")
        self.assertIn("1 captioned", out["done"]["text"])

    def test_the_details_address_opens_the_pass_on_load(self):
        out = self.run_js("auto-open")
        self.assertEqual((out["view"]["open"], out["view"]["position"]), (True, "1 of 3"))

    def test_a_new_upload_opens_the_pass_for_only_the_files_it_added(self):
        out = self.run_js("after-upload")
        self.assertEqual(out["before"]["pending"], "Add details (3)")
        v = out["view"]
        self.assertEqual((v["open"], v["position"], v["name"]), (True, "1 of 1", "NEW-upload.jpg"))
        self.assertEqual(captions.pending_count(self.root), 4, "the three older files are still pending, just not pushed at you")

    def test_tiles_show_the_caption_an_edit_button_and_what_still_needs_detail(self):
        captions.set_detail(self.root, captions.items(self.root)[0]["sha"], "Opening speech")
        out = self.run_js("tiles")
        tiles = out["tiles"]
        import re
        file_of = lambda x: (re.search(r"[A-Za-z][\w-]*\.\w+", x["dim"]) or re.search(r".*", x["name"])).group(0)      # on the small line after the size, or the label itself when that is the name
        by_file = {file_of(x): x for x in tiles}
        self.assertEqual(len(tiles), 3)
        self.assertTrue(all(x["hasEdit"] for x in tiles))
        self.assertEqual(by_file["IMG_1.jpg"]["name"], "Opening speech", "the caption is the main label")
        self.assertEqual(by_file["IMG_2.jpg"]["name"], "No caption yet", "with no caption it says so, not a raw filename")
        self.assertRegex(by_file["IMG_1.jpg"]["dim"], r"^\d+(\.\d)? (KB|MB) · IMG_1\.jpg$", "size first, then the file name")
        self.assertTrue(out["status"].startswith("3 files"))

    def test_one_file_is_not_called_one_files(self):
        shutil.rmtree(self.root / "assets" / "inbox")
        for n in ("IMG_2.jpg",):
            (self.root / "assets" / "photos" / n).unlink()
        out = self.run_js("tiles")
        self.assertTrue(out["status"].startswith("1 file ·"), out["status"])


if __name__ == "__main__":
    unittest.main()
