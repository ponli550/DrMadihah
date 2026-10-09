import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from umcares import projects, web


class Headers(dict):
    """Case-insensitive enough for the one header the check reads."""
    def get(self, k, d=None):
        for key, v in self.items():
            if key.lower() == k.lower():
                return v
        return d


class Authorized(unittest.TestCase):
    def test_the_owner_through_tailscale_is_let_in(self):
        self.assertTrue(web.authorized(Headers({"Tailscale-User-Login": "Me@x.com"}), "me@x.com"))

    def test_another_login_on_the_tailnet_is_refused(self):
        self.assertFalse(web.authorized(Headers({"Tailscale-User-Login": "other@x.com"}), "me@x.com"))

    def test_with_no_owner_configured_nobody_on_the_tailnet_gets_in(self):
        self.assertFalse(web.authorized(Headers({"Tailscale-User-Login": "me@x.com"}), ""))

    def test_a_direct_local_request_is_allowed(self):
        self.assertTrue(web.authorized(Headers({"Host": "127.0.0.1:8733"}), ""))
        self.assertTrue(web.authorized(Headers({"Host": "localhost:8733"}), ""))

    def test_a_rebound_hostname_with_no_login_is_refused(self):
        """DNS rebinding: a web page resolves its own name to 127.0.0.1."""
        self.assertFalse(web.authorized(Headers({"Host": "evil.example:8733"}), "me@x.com"))


class Library(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        a = self.root / "assets" / "footage" / "A"
        a.mkdir(parents=True)
        (a / "C0001.mp4").write_bytes(b"x" * 10)
        (a / ".hidden.mp4").write_bytes(b"x")
        (a / "render.part.mp4").write_bytes(b"x")
        (a / "sub").mkdir()
        (self.root / "assets" / "photos").mkdir()
        (self.root / "assets" / "photos" / "p.JPG").write_bytes(b"abc")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_lists_files_with_kind_and_size(self):
        lib = {g["folder"]: g for g in web.list_library(self.root)["folders"]}
        self.assertEqual([f["name"] for f in lib["footage/A"]["files"]], ["C0001.mp4"])
        self.assertEqual(lib["footage/A"]["files"][0]["kind"], "video")
        self.assertEqual(lib["footage/A"]["files"][0]["size"], 10)
        self.assertEqual(lib["photos"]["files"][0]["kind"], "image")

    def test_hides_dotfiles_in_flight_part_files_and_subfolders(self):
        names = [f["name"] for g in web.list_library(self.root)["folders"] for f in g["files"]]
        self.assertNotIn(".hidden.mp4", names)
        self.assertNotIn("render.part.mp4", names)
        self.assertNotIn("sub", names)

    def test_a_missing_folder_is_reported_not_an_error(self):
        lib = {g["folder"]: g for g in web.list_library(self.root)["folders"]}
        self.assertFalse(lib["music"]["exists"])
        self.assertEqual(lib["music"]["files"], [])


class ResolveMedia(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "assets" / "photos").mkdir(parents=True)
        (self.root / "assets" / "photos" / "p.jpg").write_bytes(b"abc")
        (self.root / "secret.txt").write_text("nope")
        (self.root / "assets" / "other").mkdir()
        (self.root / "assets" / "other" / "x.jpg").write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_serves_a_library_file(self):
        self.assertEqual(web.resolve_media(self.root, "photos/p.jpg").name, "p.jpg")

    def test_refuses_traversal_out_of_assets(self):
        self.assertIsNone(web.resolve_media(self.root, "photos/../../secret.txt"))
        self.assertIsNone(web.resolve_media(self.root, "../secret.txt"))

    def test_refuses_a_folder_that_is_not_in_the_library(self):
        self.assertIsNone(web.resolve_media(self.root, "other/x.jpg"))

    def test_refuses_dotfiles_and_missing_files(self):
        self.assertIsNone(web.resolve_media(self.root, "photos/.env"))
        self.assertIsNone(web.resolve_media(self.root, "photos/missing.jpg"))

    def test_refuses_a_symlink_that_leaves_assets(self):
        (self.root / "assets" / "photos" / "link.txt").symlink_to(self.root / "secret.txt")
        self.assertIsNone(web.resolve_media(self.root, "photos/link.txt"))


class ParseRange(unittest.TestCase):
    def test_closed_open_and_suffix_forms(self):
        self.assertEqual(web.parse_range("bytes=0-3", 10), (0, 3))
        self.assertEqual(web.parse_range("bytes=4-", 10), (4, 9))
        self.assertEqual(web.parse_range("bytes=-3", 10), (7, 9))

    def test_an_end_past_the_file_is_clamped(self):
        self.assertEqual(web.parse_range("bytes=5-999", 10), (5, 9))

    def test_a_start_past_the_file_is_unsatisfiable(self):
        with self.assertRaises(ValueError):
            web.parse_range("bytes=10-", 10)

    def test_no_header_or_garbage_is_ignored(self):
        self.assertIsNone(web.parse_range(None, 10))
        self.assertIsNone(web.parse_range("items=0-1", 10))


class Server(unittest.TestCase):
    """Base: a real server over HTTP on an ephemeral port, with one project and one registered folder."""
    OWNER = "me@x.com"

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        made = projects.create_project(cls.home, "Demo", "2026-10-09", "first event", init_git=False)
        cls.demo = Path(made["path"])
        (cls.demo / "assets" / "photos" / "p.jpg").write_bytes(b"0123456789")
        (cls.home / "secret.txt").write_text("nope")
        cls.ext = Path(tempfile.mkdtemp())
        (cls.ext / "assets" / "photos").mkdir(parents=True)
        (cls.ext / "assets" / "photos" / "e.jpg").write_bytes(b"external")
        projects.register_external(cls.home, "legacy", "Legacy project", cls.ext)
        cls.srv = web.make_server(cls.home, cls.OWNER, 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.ext, ignore_errors=True)

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.headers, data


class Browse(Server):
    def test_the_home_page_and_the_project_list_load_locally(self):
        self.assertEqual(self.req("GET", "/")[0], 200)
        status, _, body = self.req("GET", "/api/projects")
        self.assertEqual(status, 200)
        slugs = [p["slug"] for p in json.loads(body)["projects"]]
        self.assertIn("demo", slugs)
        self.assertIn("legacy", slugs)

    def test_the_list_never_leaks_a_path_on_the_machine(self):
        _, _, body = self.req("GET", "/api/projects")
        self.assertNotIn(str(self.home).encode(), body)
        self.assertNotIn(str(self.ext).encode(), body)
        self.assertNotIn(b'"path"', body)

    def test_a_project_page_and_its_library(self):
        status, _, body = self.req("GET", "/p/demo/")
        self.assertEqual(status, 200)
        self.assertIn(b"Demo", body)
        status, _, body = self.req("GET", "/p/demo/api/library")
        self.assertEqual(status, 200)
        self.assertIn(b"p.jpg", body)

    def test_a_registered_folder_is_browsable_like_any_project(self):
        status, _, body = self.req("GET", "/p/legacy/api/library")
        self.assertEqual(status, 200)
        self.assertIn(b"e.jpg", body)

    def test_an_unknown_project_or_a_malformed_slug_is_a_404(self):
        self.assertEqual(self.req("GET", "/p/nope/")[0], 404)
        self.assertEqual(self.req("GET", "/p/Demo/")[0], 404)
        self.assertEqual(self.req("GET", "/p/..%2F/api/library")[0], 404)

    def test_a_media_file_is_served_whole_and_by_range(self):
        status, _, body = self.req("GET", "/p/demo/media/photos/p.jpg")
        self.assertEqual((status, body), (200, b"0123456789"))
        status, h, body = self.req("GET", "/p/demo/media/photos/p.jpg", {"Range": "bytes=2-4"})
        self.assertEqual((status, body), (206, b"234"))
        self.assertEqual(h["Content-Range"], "bytes 2-4/10")
        self.assertEqual(self.req("GET", "/p/demo/media/photos/p.jpg", {"Range": "bytes=50-"})[0], 416)

    def test_one_project_cannot_read_anothers_files(self):
        self.assertEqual(self.req("GET", "/p/legacy/media/photos/p.jpg")[0], 404)

    def test_traversal_is_a_404_even_when_percent_encoded(self):
        self.assertEqual(self.req("GET", "/p/demo/media/photos/..%2F..%2F..%2Fsecret.txt")[0], 404)
        self.assertEqual(self.req("GET", "/p/demo/media/photos/%2e%2e/%2e%2e/%2e%2e/secret.txt")[0], 404)

    def test_a_hostile_project_name_cannot_close_the_script_tag(self):
        name = "</script><script>alert(1)</script>"
        projects.create_project(self.home, name, init_git=False)
        status, _, body = self.req("GET", f"/p/{projects.slugify(name)}/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"</script><script>alert", body)
        self.assertIn(b"<\\/script>", body)


class Access(Server):
    def test_another_tailnet_login_is_refused_everywhere(self):
        for path in ("/", "/api/projects", "/p/demo/", "/p/demo/api/library", "/p/demo/media/photos/p.jpg"):
            self.assertEqual(self.req("GET", path, {"Tailscale-User-Login": "x@y.com"})[0], 403, path)

    def test_the_owner_over_tailscale_is_served(self):
        self.assertEqual(self.req("GET", "/api/projects", {"Tailscale-User-Login": "me@x.com"})[0], 200)

    def test_a_rebound_host_is_refused(self):
        self.assertEqual(self.req("GET", "/api/projects", {"Host": "evil.example"})[0], 403)

    def test_put_delete_and_patch_are_refused(self):
        for m in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.req(m, "/api/projects")[0], 405, m)


class CreateProject(Server):
    OK = {"Content-Type": "application/json", "X-Requested-With": "umcares"}

    def post(self, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.req("POST", "/api/projects", headers or self.OK, body)

    def test_creates_a_project_that_then_appears_in_the_list(self):
        status, _, body = self.post({"name": "Hari Raya 2026", "event_date": "2026-04-01", "notes": "open house"})
        self.assertEqual(status, 201)
        d = json.loads(body)
        self.assertEqual(d["project"]["slug"], "hari-raya-2026")
        self.assertNotIn("path", d["project"])
        self.assertTrue((self.home / "hari-raya-2026" / "project.json").is_file())
        self.assertIn(b"hari-raya-2026", self.req("GET", "/api/projects")[2])

    def test_a_forged_cross_site_post_is_refused(self):
        """The login header is added by tailscale, so a page on another site could otherwise forge this."""
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.post(b"name=Evil", form)[0], 403)
        self.assertEqual(self.post({"name": "Evil"}, {"Content-Type": "application/json"})[0], 403)    # no custom header
        self.assertEqual(self.post({"name": "Evil"}, {"X-Requested-With": "umcares"})[0], 403)         # not JSON
        self.assertFalse((self.home / "evil").exists())

    def test_another_login_cannot_create(self):
        status, _, _ = self.post({"name": "Intruder"}, {**self.OK, "Tailscale-User-Login": "x@y.com"})
        self.assertEqual(status, 403)
        self.assertFalse((self.home / "intruder").exists())

    def test_bad_input_is_a_400_with_a_message(self):
        for payload in ({"name": ""}, {"name": "x", "event_date": "9 Oct"}, {"name": "项目"},
                        {"name": 5}, {"nope": 1}, {"name": "x" * 101}, b"{not json", b"[]"):
            status, _, body = self.post(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", json.loads(body))

    def test_a_duplicate_name_is_a_400_and_leaves_the_first_alone(self):
        status, _, body = self.post({"name": "demo"})
        self.assertEqual(status, 400)
        self.assertIn(b"already exists", body)
        self.assertEqual(json.loads((self.demo / "project.json").read_text())["notes"], "first event")

    def test_an_oversized_declaration_is_refused_without_reading_it(self):
        status, _, _ = self.req("POST", "/api/projects", {**self.OK, "Content-Length": "20000"}, b"{}")
        self.assertEqual(status, 400)

    def test_post_anywhere_else_is_405(self):
        self.assertEqual(self.req("POST", "/api/library", self.OK, b"{}")[0], 405)


class UploadRoutes(Server):
    JSON = {"Content-Type": "application/json", "X-Requested-With": "umcares"}
    BIN = {"Content-Type": "application/octet-stream", "X-Requested-With": "umcares"}

    def start(self, name, size, mtime=1, slug="demo", headers=None):
        body = json.dumps({"name": name, "size": size, "mtime": mtime}).encode()
        return self.req("POST", f"/p/{slug}/api/upload/start", headers or self.JSON, body)

    def test_a_whole_upload_over_http_lands_in_the_library(self):
        status, _, body = self.start("web1.jpg", 10)
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual((d["status"], d["offset"]), ("new", 0))
        up = f"/p/demo/api/upload/{d['id']}"
        self.assertEqual(self.req("PUT", up + "?offset=0", self.BIN, b"01234")[0], 200)
        status, _, body = self.req("GET", up)
        self.assertEqual(json.loads(body), {"offset": 5, "size": 10})
        self.assertEqual(self.req("PUT", up + "?offset=5", self.BIN, b"56789")[0], 200)
        status, _, body = self.req("POST", up + "/finish", self.JSON, b"{}")
        self.assertEqual((status, json.loads(body)["status"]), (200, "saved"))
        self.assertEqual((self.demo / "assets" / "photos" / "web1.jpg").read_bytes(), b"0123456789")
        self.assertIn(b"web1.jpg", self.req("GET", "/p/demo/api/library")[2])

    def test_a_wrong_offset_is_a_409_that_says_where_the_upload_stands(self):
        d = json.loads(self.start("web2.jpg", 10)[2])
        up = f"/p/demo/api/upload/{d['id']}"
        self.req("PUT", up + "?offset=0", self.BIN, b"01234")
        status, _, body = self.req("PUT", up + "?offset=0", self.BIN, b"01234")        # a retry of an acknowledged chunk
        self.assertEqual((status, json.loads(body)["offset"]), (409, 5))

    def test_writes_without_the_custom_header_are_refused(self):
        self.assertEqual(self.start("web3.jpg", 5, headers={"Content-Type": "application/json"})[0], 403)
        d = json.loads(self.start("web3.jpg", 5)[2])
        up = f"/p/demo/api/upload/{d['id']}"
        self.assertEqual(self.req("PUT", up + "?offset=0", {"Content-Type": "application/octet-stream"}, b"12345")[0], 403)
        self.assertEqual(self.req("POST", up + "/finish", {"Content-Type": "application/json"}, b"{}")[0], 403)
        self.assertEqual(self.req("POST", up + "/finish", {"X-Requested-With": "umcares"}, b"{}")[0], 403)   # not JSON
        self.assertEqual(self.req("GET", up)[1].get("Content-Type"), "application/json")      # reading status needs no header

    def test_another_login_cannot_upload_or_read_status(self):
        other = {"Tailscale-User-Login": "x@y.com"}
        self.assertEqual(self.start("web4.jpg", 5, headers={**self.JSON, **other})[0], 403)
        d = json.loads(self.start("web4.jpg", 5)[2])
        self.assertEqual(self.req("GET", f"/p/demo/api/upload/{d['id']}", other)[0], 403)
        self.assertEqual(self.req("PUT", f"/p/demo/api/upload/{d['id']}?offset=0", {**self.BIN, **other}, b"12345")[0], 403)

    def test_an_unknown_project_or_session_is_a_404(self):
        self.assertEqual(self.start("a.jpg", 5, slug="nope")[0], 404)
        self.assertEqual(self.req("GET", "/p/demo/api/upload/" + "0" * 16)[0], 404)
        self.assertEqual(self.req("PUT", "/p/demo/api/upload/" + "0" * 16 + "?offset=0", self.BIN, b"x")[0], 404)
        self.assertEqual(self.req("GET", "/p/demo/api/upload/not-an-id")[0], 404)

    def test_unsupported_types_and_bad_sizes_are_a_400_with_a_message(self):
        for name, size in (("notes.txt", 5), ("a.jpg", 0), ("a.jpg", "big"), ("a.jpg", -3)):
            status, _, body = self.start(name, size)
            self.assertEqual(status, 400, (name, size))
            self.assertIn("error", json.loads(body))

    def test_a_non_string_name_is_a_400_not_a_crash(self):
        body = json.dumps({"name": 5, "size": 5}).encode()
        self.assertEqual(self.req("POST", "/p/demo/api/upload/start", self.JSON, body)[0], 400)

    def test_an_upload_cannot_write_outside_the_library(self):
        d = json.loads(self.start("../../../escape.jpg", 5)[2])
        up = f"/p/demo/api/upload/{d['id']}"
        self.req("PUT", up + "?offset=0", self.BIN, b"12345")
        status, _, body = self.req("POST", up + "/finish", self.JSON, b"{}")
        self.assertEqual(json.loads(body)["file"]["name"], "escape.jpg")
        self.assertFalse((self.home / "escape.jpg").exists())
        self.assertTrue((self.demo / "assets" / "photos" / "escape.jpg").is_file())

    def test_an_oversized_chunk_declaration_is_refused(self):
        d = json.loads(self.start("web5.jpg", 100)[2])
        status, _, _ = self.req("PUT", f"/p/demo/api/upload/{d['id']}?offset=0", {**self.BIN, "Content-Length": str(uploads_max() + 1)}, b"x")
        self.assertEqual(status, 400)

    def test_the_inbox_is_listed_in_the_library(self):
        folders = [g["folder"] for g in json.loads(self.req("GET", "/p/demo/api/library")[2])["folders"]]
        self.assertIn("inbox", folders)


class Installable(Server):
    """What Chrome checks before it offers 'Install app'."""

    def png_size(self, data):
        import struct
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        return struct.unpack(">II", data[16:24])

    def pixel(self, data, x, y):
        import struct
        import zlib
        w, h = struct.unpack(">II", data[16:24])
        pos, idat = 8, b""
        while pos < len(data):
            n, kind = struct.unpack(">I4s", data[pos:pos + 8])
            if kind == b"IDAT":
                idat += data[pos + 8:pos + 8 + n]
            pos += 12 + n
        raw = zlib.decompress(idat)
        row = raw[y * (1 + w * 3) + 1:(y + 1) * (1 + w * 3)]
        return tuple(row[x * 3:x * 3 + 3])

    def test_the_manifest_is_valid_and_scoped_to_the_mount(self):
        status, h, body = self.req("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertEqual(h["Content-Type"], "application/manifest+json")
        m = json.loads(body)
        self.assertEqual((m["display"], m["start_url"], m["scope"]), ("standalone", "/umcares/", "/umcares/"))
        self.assertTrue(m["name"] and m["short_name"])
        sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
        self.assertTrue({("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes)

    def test_every_icon_the_manifest_lists_exists_and_has_the_size_it_claims(self):
        for icon in json.loads(self.req("GET", "/manifest.webmanifest")[2])["icons"]:
            self.assertTrue(icon["src"].startswith("/umcares/"))
            status, h, body = self.req("GET", icon["src"][len("/umcares"):])
            self.assertEqual(status, 200, icon["src"])
            self.assertEqual(h["Content-Type"], "image/png")
            w = int(icon["sizes"].split("x")[0])
            self.assertEqual(self.png_size(body), (w, w), icon["src"])

    def test_the_icon_is_drawn_not_blank(self):
        _, _, std = self.req("GET", "/icon-512.png")
        self.assertEqual(self.pixel(std, 5, 5), (17, 19, 24))          # outside the card: the dark background
        self.assertEqual(self.pixel(std, 90, 256), (127, 176, 255))    # on the card: the accent
        self.assertEqual(self.pixel(std, 220, 256), (17, 19, 24))      # inside the triangle: dark again
        _, _, mask = self.req("GET", "/icon-maskable-512.png")
        self.assertEqual(self.pixel(mask, 5, 5), (127, 176, 255))      # maskable is full bleed, so the corner is accent

    def test_an_unknown_icon_is_a_404(self):
        self.assertEqual(self.req("GET", "/icon-999.png")[0], 404)
        self.assertEqual(self.req("GET", "/icon-192.png.bak")[0], 404)

    def test_the_service_worker_is_javascript_that_never_touches_a_request(self):
        status, h, body = self.req("GET", "/sw.js")
        self.assertEqual(status, 200)
        self.assertTrue(h["Content-Type"].startswith("text/javascript"))
        self.assertEqual(h["Cache-Control"], "no-cache")       # a new worker must be noticed
        text = body.decode()
        self.assertIn("addEventListener('fetch'", text)
        self.assertNotIn("respondWith", text)                  # nothing is cached or rewritten
        self.assertNotIn("caches", text)

    def test_both_pages_link_the_manifest_and_register_the_worker(self):
        for path in ("/", "/p/demo/"):
            body = self.req("GET", path)[2].decode()
            self.assertIn('rel="manifest" href="/umcares/manifest.webmanifest"', body, path)
            self.assertIn('rel="apple-touch-icon" href="/umcares/icon-192.png"', body, path)
            self.assertIn('serviceWorker.register("/umcares/sw.js", {scope: "/umcares/"})', body, path)
            self.assertIn('name="theme-color"', body, path)
            self.assertNotIn("__PWA__", body)
            self.assertNotIn("__ROOT__", body)

    def test_the_install_files_are_not_public(self):
        other = {"Tailscale-User-Login": "x@y.com"}
        for path in ("/manifest.webmanifest", "/sw.js", "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"):
            self.assertEqual(self.req("GET", path, other)[0], 403, path)


def uploads_max():
    from umcares import uploads
    return uploads.CHUNK_MAX


@unittest.skipUnless(web.find_ffmpeg(), "needs ffmpeg")
class Thumbnails(Server):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        v = cls.demo / "assets" / "edit_ready"
        subprocess.run([web.find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2",
                        "-pix_fmt", "yuv420p", str(v / "c.mp4")], check=True)
        (v / "bad.mp4").write_bytes(b"not a video")

    def test_a_video_gets_a_small_jpeg(self):
        status, h, body = self.req("GET", "/p/demo/thumb/edit_ready/c.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(h["Content-Type"], "image/jpeg")
        self.assertTrue(body.startswith(b"\xff\xd8"))
        self.assertLess(len(body), 100_000)
        self.assertEqual(h.get_all("Cache-Control"), ["private, max-age=86400"])

    def test_the_cache_lives_outside_the_project_so_git_stays_clean(self):
        self.req("GET", "/p/demo/thumb/edit_ready/c.mp4")
        self.assertTrue(list((self.home / ".cache" / "thumbs").glob("*.jpg")))
        self.assertFalse((self.demo / "web").exists())

    def test_the_second_request_reuses_the_cached_file(self):
        self.req("GET", "/p/demo/thumb/edit_ready/c.mp4")
        t = web.thumb_path(web.thumb_cache(self.home), self.demo / "assets" / "edit_ready" / "c.mp4")
        before = t.stat().st_mtime_ns
        self.req("GET", "/p/demo/thumb/edit_ready/c.mp4")
        self.assertEqual(t.stat().st_mtime_ns, before)

    def test_the_same_file_by_two_routes_is_one_cache_entry(self):
        self.req("GET", "/p/demo/thumb/edit_ready/c.mp4")
        self.req("GET", "/p/demo/api/thumbs?folder=edit_ready")
        t = web.thumb_path(web.thumb_cache(self.home), self.demo / "assets" / "edit_ready" / "c.mp4")
        self.assertEqual([p.name for p in (self.home / ".cache" / "thumbs").glob("*.jpg")], [t.name])

    def test_a_file_ffmpeg_cannot_read_is_a_404_and_leaves_no_part_file(self):
        self.assertEqual(self.req("GET", "/p/demo/thumb/edit_ready/bad.mp4")[0], 404)
        self.assertEqual(list((self.home / ".cache" / "thumbs").glob("*.part*")), [])

    def test_thumbnails_are_confined_like_media(self):
        self.assertEqual(self.req("GET", "/p/demo/thumb/edit_ready/..%2F..%2Fproject.json")[0], 404)
        self.assertEqual(self.req("GET", "/p/demo/thumb/nope/c.mp4")[0], 404)

    def test_a_folder_comes_back_in_one_response_as_data_uris(self):
        status, _, body = self.req("GET", "/p/demo/api/thumbs?folder=edit_ready&budget=8")
        d = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(d["thumbs"]["c.mp4"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(d["failed"], ["bad.mp4"])
        self.assertEqual(d["pending"], [])

    def test_the_budget_caps_work_per_call_and_the_rest_is_pending(self):
        root = Path(tempfile.mkdtemp())
        cache = root / "cache"
        v = root / "assets" / "cards"
        v.mkdir(parents=True)
        for n in ("a", "b", "c"):
            subprocess.run([web.find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=1",
                            "-pix_fmt", "yuv420p", str(v / (n + ".mp4"))], check=True)
        first = web.thumb_batch(cache, root, "cards", budget=1)
        self.assertEqual((len(first["thumbs"]), len(first["pending"])), (1, 2))
        second = web.thumb_batch(cache, root, "cards", budget=1)
        self.assertEqual((len(second["thumbs"]), len(second["pending"])), (2, 1))
        third = web.thumb_batch(cache, root, "cards", budget=8)
        self.assertEqual((len(third["thumbs"]), third["pending"]), (3, []))
        shutil.rmtree(root, ignore_errors=True)

    def test_an_unknown_folder_is_a_404(self):
        self.assertEqual(self.req("GET", "/p/demo/api/thumbs?folder=..")[0], 404)
        self.assertEqual(self.req("GET", "/p/demo/api/thumbs?folder=nope")[0], 404)
        self.assertEqual(self.req("GET", "/p/demo/api/thumbs")[0], 404)


if __name__ == "__main__":
    unittest.main()
