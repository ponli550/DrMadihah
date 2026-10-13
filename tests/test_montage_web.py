"""The montage routes over HTTP, with real ffmpeg at a reduced size."""
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from umcares import captions, montage, prepare, projects, textimg, uploads, web
from tests.test_prepare import make_conformant, put

FF = prepare.find_tool("ffmpeg")
NEED = unittest.skipUnless(FF and prepare.find_tool("ffprobe") and textimg.available(), "needs ffmpeg and macOS text drawing")
JSON = {"Content-Type": "application/json", "X-Requested-With": "umcares"}


def recipe(clips, **kw):
    return {"kind": "montage", "title": "Web test", "format": "reels", "template": "punchy", "clips": clips, **kw}


@NEED
class Routes(unittest.TestCase):
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
        self.patches = [mock.patch.dict(montage.FORMATS["reels"], width=216, height=384)]
        for p in self.patches:
            p.start()
        montage._info_cache.clear()
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=True)["path"])
        for i, n in enumerate(("land.jpg", "port.jpg", "clip.mp4")):
            put(self.root, self.fx / n, mtime=1_700_000_000_000 + i * 60_000)
        put(self.root, self.fx / "song.m4a", mtime=1_700_000_500_000)
        self.srv = web.make_server(self.home, "me@x.com", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        montage.RENDERER.cancel(self.root)
        montage.RENDERER.wait_idle(30)
        self.srv.shutdown()
        self.srv.server_close()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        hdrs = r.headers
        c.close()
        return r.status, data, hdrs

    def get(self, path="/p/demo/api/montage", headers=None):
        status, body, _ = self.req("GET", path, headers)
        return status, (json.loads(body) if body[:1] in (b"{", b"[") else body)

    def post(self, payload, path="/p/demo/api/montage", headers=None):
        status, body, _ = self.req("POST", path, headers or JSON, json.dumps(payload).encode())
        return status, (json.loads(body) if body[:1] in (b"{", b"[") else body)

    def wait(self, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            d = self.get()[1]
            if not d["active"]:
                return d
            time.sleep(0.1)
        self.fail("the render never finished")

    # ------------------------------------------------------------ state
    def test_the_state_lists_what_can_be_used_and_what_is_offered(self):
        status, d = self.get()
        self.assertEqual(status, 200)
        self.assertEqual([(i["name"], i["kind"]) for i in d["catalog"]],
                         [("land.jpg", "image"), ("port.jpg", "image"), ("clip.mp4", "video"), ("song.m4a", "audio")])
        self.assertEqual(set(d["templates"]), set(montage.TEMPLATES))
        self.assertEqual(set(d["formats"]), {"reels", "square", "wide"})
        self.assertTrue(d["can_render"])
        self.assertEqual((d["jobs"], d["exports"], d["recipes"], d["active"]), ([], [], [], False))

    def test_the_state_never_leaks_a_path_on_the_machine(self):
        self.post({"recipe": recipe([{"media": "land.jpg"}])})
        self.wait()
        body = self.req("GET", "/p/demo/api/montage")[1] + self.req("POST", "/p/demo/api/montage", JSON, json.dumps({"recipe": recipe([{"media": "land.jpg"}]), "dry_run": True}).encode())[1]
        self.assertNotIn(str(self.home).encode(), body)
        self.assertNotIn(b"/assets/", body, "relative storage paths are not exposed either")

    # ------------------------------------------------------------ planning and making
    def test_a_dry_run_shows_the_timeline_without_rendering_anything(self):
        status, d = self.post({"recipe": recipe([{"media": "land.jpg", "caption": "Hi"}, {"media": "clip.mp4"}]), "dry_run": True})
        self.assertEqual(status, 200)
        p = d["plan"]
        self.assertEqual((p["format"], p["template"]), ("reels", "punchy"))
        self.assertEqual([s["kind"] for s in p["segments"]], ["photo", "video"])
        self.assertEqual(p["segments"][0]["text"], "Hi")
        self.assertGreater(p["total"], 2)
        self.assertEqual(self.get()[1]["jobs"], [], "nothing was queued")

    def test_the_preview_carries_the_beat_and_speech_facts_the_page_shows(self):
        d = self.post({"recipe": recipe([{"media": "land.jpg"}, {"media": "clip.mp4"}], music={"bpm": 75}, speech="follow"), "dry_run": True})[1]
        p = d["plan"]
        self.assertEqual((p["sync"]["bpm"], p["sync"]["tempo_only"], p["sync"]["beats_per_cut"]), (75.0, True, 2))
        self.assertIn("speech", p["segments"][1], "seconds of speech in a followed video clip")
        self.assertIsNone(p["segments"][0]["speech"], "a photo has no speech")
        self.assertIn("sound", p["segments"][1], "whether a clip keeps its sound reaches the page")
        self.assertIsNone(self.post({"recipe": recipe([{"media": "land.jpg"}]), "dry_run": True})[1]["plan"]["sync"])

    # ------------------------------------------------------------ deleting a finished video
    def exports_dir(self):
        d = self.root / "exports"
        d.mkdir(exist_ok=True)
        return d

    def fake_export(self, name="reel-20261013-101010.mp4", size=2000):
        d = self.exports_dir()
        (d / name).write_bytes(b"v" * size)
        (d / name).with_suffix(".json").write_text("{}")
        return name

    def test_deleting_removes_the_video_and_its_sidecar_and_nothing_else(self):
        name, other = self.fake_export(), self.fake_export("other-20261013-101011.mp4")
        (self.root / "recipes").mkdir(exist_ok=True)
        (self.root / "recipes" / "keep.montage.json").write_text("{}")
        status, d = self.post({"name": name}, "/p/demo/api/montage/delete-export")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(d["deleted"]), sorted([name, name.replace(".mp4", ".json")]))
        self.assertEqual(d["freed"], 2000 + 2)
        self.assertEqual(self.req("GET", f"/p/demo/exports/{name}")[0], 404)
        self.assertTrue((self.exports_dir() / other).is_file(), "another video is untouched")
        self.assertTrue((self.root / "recipes" / "keep.montage.json").is_file())
        self.assertEqual([x["name"] for x in self.get()[1]["exports"]], [other])

    def test_deleting_needs_the_guard_header_and_json(self):
        name = self.fake_export()
        self.assertEqual(self.post({"name": name}, "/p/demo/api/montage/delete-export", {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.post({"name": name}, "/p/demo/api/montage/delete-export", {"X-Requested-With": "umcares", "Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.req("GET", "/p/demo/exports/" + name)[0], 200, "a refused request deletes nothing")

    def test_no_name_can_reach_outside_the_exports_folder_or_a_half_written_video(self):
        self.fake_export()
        (self.root / "assets" / "photos" / "keep.mp4").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "assets" / "photos" / "keep.mp4").write_bytes(b"x")
        (self.exports_dir() / ".hidden-20261013.part.mp4").write_bytes(b"x")
        (self.exports_dir() / "notes.txt").write_text("x")
        (self.exports_dir() / "reel-20261013-999999.part.mp4").write_bytes(b"x")      # a video still being written
        for bad in ("../assets/photos/keep.mp4", "..%2Fassets%2Fphotos%2Fkeep.mp4", "/etc/passwd", "a/b.mp4", "..", "", None, 7, ["x.mp4"],
                    ".hidden-20261013.part.mp4", "reel-20261013-999999.part.mp4", "notes.txt", "reel-20261013-101010.json", "missing-20261013.mp4", "x" * 200 + ".mp4"):
            with self.subTest(bad=bad):
                status, d = self.post({"name": bad}, "/p/demo/api/montage/delete-export")
                self.assertEqual(status, 400, d)
        self.assertTrue((self.root / "assets" / "photos" / "keep.mp4").is_file())
        self.assertTrue((self.exports_dir() / ".hidden-20261013.part.mp4").is_file())
        self.assertTrue((self.exports_dir() / "reel-20261013-999999.part.mp4").is_file())
        self.assertTrue((self.exports_dir() / "notes.txt").is_file())
        self.assertTrue((self.exports_dir() / "reel-20261013-101010.mp4").is_file())

    def test_a_symlink_in_exports_is_never_followed(self):
        target = self.root / "assets" / "photos" / "precious.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"precious")
        os.symlink(target, self.exports_dir() / "link-20261013.mp4")
        self.assertEqual(self.post({"name": "link-20261013.mp4"}, "/p/demo/api/montage/delete-export")[0], 400)
        self.assertTrue(target.is_file())

    def test_a_visitor_who_is_not_the_owner_cannot_delete(self):
        name = self.fake_export()
        status, _, _ = self.req("POST", "/p/demo/api/montage/delete-export", {**JSON, "Tailscale-User-Login": "stranger@x.com"}, json.dumps({"name": name}).encode())
        self.assertEqual(status, 403)
        self.assertTrue((self.exports_dir() / name).is_file())

    def test_making_a_video_renders_it_and_the_file_can_be_played_with_seeking(self):
        status, d = self.post({"recipe": recipe([{"media": "land.jpg", "caption": "Opening"}, {"media": "port.jpg"}, {"media": "clip.mp4"}])})
        self.assertEqual(status, 200)
        self.assertEqual(d["job"]["state"], "queued")
        done = self.wait()
        job = done["jobs"][-1]
        self.assertEqual((job["state"], job["percent"]), ("done", 100), job["error"])
        self.assertEqual(done["exports"][0]["name"], job["output"])
        status, body, h = self.req("GET", f"/p/demo/exports/{job['output']}")
        self.assertEqual((status, h["Content-Type"]), (200, "video/mp4"))
        self.assertEqual(body[4:8], b"ftyp")
        status, part, h = self.req("GET", f"/p/demo/exports/{job['output']}", {"Range": "bytes=0-99"})
        self.assertEqual((status, len(part)), (206, 100))
        side = self.req("GET", f"/p/demo/exports/{job['output'][:-4]}.json")
        self.assertEqual(side[0], 200)
        self.assertEqual(json.loads(side[1])["plan"]["template"], "punchy")

    def test_a_bad_recipe_is_refused_with_every_problem_listed_and_queues_nothing(self):
        status, d = self.post({"recipe": {"clips": [{"media": "nope.jpg"}, {"media": "land.jpg", "hold": 99}], "format": "imax", "colour": 1}})
        self.assertEqual(status, 400)
        self.assertGreaterEqual(len(d["problems"]), 4)
        self.assertTrue(any("nope.jpg" in p for p in d["problems"]))
        self.assertEqual(self.get()[1]["jobs"], [])

    def test_a_clip_that_starts_past_its_end_is_a_readable_400(self):
        status, d = self.post({"recipe": recipe([{"media": "clip.mp4", "start": 50}])})
        self.assertEqual(status, 400)
        self.assertIn("past the end of clip.mp4", d["error"])

    def test_music_from_the_project_is_mixed_in(self):
        self.post({"recipe": recipe([{"media": "land.jpg"}, {"media": "port.jpg"}], music={"file": "song.m4a", "fade_out": 1})})
        done = self.wait()
        self.assertEqual(done["jobs"][-1]["state"], "done", done["jobs"][-1]["error"])
        self.assertTrue(done["exports"][0]["audio"])

    def test_cancel_with_nothing_running_is_harmless(self):
        self.assertEqual(self.post({}, "/p/demo/api/montage/cancel"), (200, {"cancelled": 0}))

    # ------------------------------------------------------------ serving exports
    def test_only_real_exports_are_served_never_a_path_or_an_in_flight_file(self):
        self.post({"recipe": recipe([{"media": "land.jpg"}])})
        name = self.wait()["exports"][0]["name"]
        (self.root / "exports" / "secret.txt").write_text("nope")
        (self.root / "exports" / ".hidden.mp4").write_bytes(b"x")
        (self.root / "exports" / "half.part.mp4").write_bytes(b"x")
        for bad in ("secret.txt", ".hidden.mp4", "half.part.mp4", "..%2Fproject.json", "%2e%2e/project.json", "nope.mp4", "a/b.mp4", name + "/"):
            self.assertEqual(self.req("GET", f"/p/demo/exports/{bad}")[0], 404, bad)
        self.assertEqual(self.req("GET", f"/p/demo/exports/{name}")[0], 200)

    # ------------------------------------------------------------ guards
    def test_writes_need_the_custom_header_and_json_and_the_right_login(self):
        r = {"recipe": recipe([{"media": "land.jpg"}])}
        self.assertEqual(self.post(r, headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.post(r, headers={"X-Requested-With": "umcares"})[0], 403)
        other = {"Tailscale-User-Login": "x@y.com"}
        self.assertEqual(self.post(r, headers={**JSON, **other})[0], 403)
        self.assertEqual(self.post({}, "/p/demo/api/montage/cancel", {**JSON, **other})[0], 403)
        self.assertEqual(self.post({"name": "x", "recipe": r["recipe"]}, "/p/demo/api/montage/recipes", {**JSON, **other})[0], 403)
        for path in ("/p/demo/api/montage", "/p/demo/api/montage/brief", "/montage.js"):
            self.assertEqual(self.req("GET", path, other)[0], 403, path)
        self.assertEqual(self.get()[1]["jobs"], [])

    def test_an_unknown_project_is_a_404(self):
        self.assertEqual(self.req("GET", "/p/nope/api/montage")[0], 404)
        self.assertEqual(self.post({"recipe": {}}, "/p/nope/api/montage")[0], 404)
        self.assertEqual(self.req("GET", "/p/nope/exports/x.mp4")[0], 404)

    def test_the_montage_script_is_served_and_the_page_loads_it(self):
        status, body, h = self.req("GET", "/montage.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function createMontage", body)
        self.assertIn(b'src="/umcares/montage.js"', self.req("GET", "/p/demo/")[1])

    # ------------------------------------------------------------ saved recipes and the brief
    def test_a_recipe_is_saved_listed_and_committed_to_git(self):
        status, d = self.post({"name": "Reel one", "recipe": recipe([{"media": "land.jpg"}])}, "/p/demo/api/montage/recipes")
        self.assertEqual((status, d), (200, {"saved": "Reel one.montage.json"}))
        listed = self.get()[1]["recipes"]
        self.assertEqual([(r["name"], r["valid"]) for r in listed], [("Reel one", True)])
        log = subprocess.run(["git", "log", "--format=%s"], cwd=self.root, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(log[0], "recipes: save Reel one.montage.json")

    def test_an_invalid_recipe_or_name_is_refused_and_nothing_is_written(self):
        status, d = self.post({"name": "bad", "recipe": recipe([{"media": "nope.jpg"}])}, "/p/demo/api/montage/recipes")
        self.assertEqual(status, 400)
        self.assertIn("problems", d)
        for name in ("../evil", "", "a/b", 5, None):
            self.assertEqual(self.post({"name": name, "recipe": recipe([{"media": "land.jpg"}])}, "/p/demo/api/montage/recipes")[0], 400, name)
        self.assertEqual(list((self.root / "recipes").glob("*.montage.json")), [])

    def test_the_brief_is_markdown_with_the_files_and_their_captions(self):
        sha = next(i["sha"] for i in captions.items(self.root) if i["name"] == "land.jpg")
        captions.set_detail(self.root, sha, "The dean opening the event")
        status, body, h = self.req("GET", "/p/demo/api/montage/brief")
        self.assertEqual((status, h["Content-Type"]), (200, "text/markdown; charset=utf-8"))
        text = body.decode()
        self.assertIn("The dean opening the event", text)
        self.assertIn(sha[:12], text)
        self.assertNotIn(str(self.home), text)


if __name__ == "__main__":
    unittest.main()
