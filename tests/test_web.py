import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from umcares import web


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


class Live(unittest.TestCase):
    """The real server over HTTP, on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        p = cls.root / "assets" / "photos"
        p.mkdir(parents=True)
        (p / "p.jpg").write_bytes(b"0123456789")
        (cls.root / "secret.txt").write_text("nope")
        cls.srv = web.make_server(cls.root, "me@x.com", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.headers, body

    def test_the_page_and_the_api_load_locally(self):
        self.assertEqual(self.req("GET", "/")[0], 200)
        status, _, body = self.req("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertIn(b"p.jpg", body)

    def test_a_tailnet_request_from_another_login_is_refused(self):
        self.assertEqual(self.req("GET", "/api/library", {"Tailscale-User-Login": "x@y.com"})[0], 403)

    def test_the_owner_over_tailscale_is_served(self):
        self.assertEqual(self.req("GET", "/api/library", {"Tailscale-User-Login": "me@x.com"})[0], 200)

    def test_a_rebound_host_is_refused(self):
        self.assertEqual(self.req("GET", "/api/library", {"Host": "evil.example"})[0], 403)

    def test_a_media_file_is_served_whole_and_by_range(self):
        status, _, body = self.req("GET", "/media/photos/p.jpg")
        self.assertEqual((status, body), (200, b"0123456789"))
        status, h, body = self.req("GET", "/media/photos/p.jpg", {"Range": "bytes=2-4"})
        self.assertEqual((status, body), (206, b"234"))
        self.assertEqual(h["Content-Range"], "bytes 2-4/10")
        self.assertEqual(self.req("GET", "/media/photos/p.jpg", {"Range": "bytes=50-"})[0], 416)

    def test_traversal_is_a_404_even_when_percent_encoded(self):
        self.assertEqual(self.req("GET", "/media/photos/..%2F..%2Fsecret.txt")[0], 404)
        self.assertEqual(self.req("GET", "/media/photos/%2e%2e/%2e%2e/secret.txt")[0], 404)

    def test_every_write_method_is_refused(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.req(m, "/api/library")[0], 405, m)


if __name__ == "__main__":
    unittest.main()
