import hashlib
import io
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from umcares import projects, uploads

HAS_GIT = shutil.which("git") is not None


class Names(unittest.TestCase):
    def test_photos_go_to_photos_and_videos_to_the_inbox(self):
        self.assertEqual(uploads.destination("IMG_1.HEIC"), "photos")
        self.assertEqual(uploads.destination("clip.MOV"), "inbox")

    def test_music_goes_to_the_music_folder(self):
        self.assertEqual(uploads.destination("Song.MP3"), "music")
        self.assertEqual(uploads.destination("beat.m4a"), "music")

    def test_anything_else_is_refused(self):
        for n in ("notes.txt", "run.sh", "archive.zip", "noext", "clip.avi"):
            with self.assertRaises(uploads.UploadError) as cm:
                uploads.destination(n)
            self.assertEqual(cm.exception.status, 400, n)

    def test_a_name_cannot_climb_out_or_hide(self):
        self.assertEqual(uploads.safe_name("../../etc/IMG_1.jpg"), "IMG_1.jpg")
        self.assertEqual(uploads.safe_name("..\\..\\win.jpg"), "win.jpg")
        self.assertEqual(uploads.safe_name(".hidden.jpg"), "hidden.jpg")
        self.assertEqual(uploads.safe_name("a<b>:c|d?.jpg"), "abcd.jpg")
        self.assertEqual(uploads.safe_name("a\x00b\n.jpg"), "ab.jpg")

    def test_a_name_with_nothing_left_is_refused(self):
        for n in ("", "...", None, 5):
            with self.assertRaises(uploads.UploadError):
                uploads.safe_name(n)

    def test_a_name_whose_extension_was_only_a_dot_prefix_is_refused_not_stored_extensionless(self):
        root = Path(tempfile.mkdtemp())
        for n in (".jpg", "..jpg", "...mp4"):
            with self.assertRaises(uploads.UploadError, msg=n):
                uploads.start(root, n, 5, 1)
        self.assertFalse((root / ".umcares").exists())
        shutil.rmtree(root, ignore_errors=True)

    def test_a_long_name_is_cut_but_keeps_its_extension(self):
        n = uploads.safe_name("x" * 300 + ".mp4")
        self.assertTrue(n.endswith(".mp4"))
        self.assertLessEqual(len(n), 104)

    def test_unicode_names_survive(self):
        self.assertEqual(uploads.safe_name("Majlis Hari Raya – 项目.jpg"), "Majlis Hari Raya – 项目.jpg")


class CaptureTime(unittest.TestCase):
    """The phone sends when the photo was taken; the file keeps that time so everything lists in the order it happened."""
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def up(self, name, data, mtime):
        s = uploads.start(self.root, name, len(data), mtime)
        uploads.append(self.root, s["id"], 0, io.BytesIO(data), len(data))
        return uploads.finish(self.root, s["id"])["file"]

    def test_the_file_gets_the_time_it_was_taken_not_the_time_it_was_uploaded(self):
        self.up("a.jpg", b"aaaa", 1_700_000_000_000)                       # 14 Nov 2023, in ms
        self.assertEqual(int((self.root / "assets" / "photos" / "a.jpg").stat().st_mtime), 1_700_000_000)

    def test_uploaded_in_the_wrong_order_they_still_list_in_the_order_they_were_taken(self):
        self.up("later.jpg", b"1111", 1_700_000_900_000)
        self.up("earlier.jpg", b"2222", 1_700_000_100_000)
        from umcares import captions
        self.assertEqual([i["name"] for i in captions.items(self.root)], ["earlier.jpg", "later.jpg"])

    def test_nonsense_times_are_ignored_and_never_break_the_upload(self):
        now = time.time()
        for i, bad in enumerate((0, 1, -5, "yesterday", None, True, 4_000_000_000_000, float("nan"), 10**30)):
            f = self.up(f"x{i}.jpg", f"data{i}".encode(), bad)
            m = (self.root / "assets" / "photos" / f["name"]).stat().st_mtime
            self.assertGreater(m, now - 60, f"{bad!r} must leave the upload time alone")

    def test_a_resumed_upload_keeps_its_original_capture_time(self):
        s = uploads.start(self.root, "r.jpg", 8, 1_700_000_000_000)
        uploads.append(self.root, s["id"], 0, io.BytesIO(b"1234"), 4)
        again = uploads.start(self.root, "r.jpg", 8, 1_700_000_000_000)       # the page reloaded
        uploads.append(self.root, again["id"], 4, io.BytesIO(b"5678"), 4)
        uploads.finish(self.root, again["id"])
        self.assertEqual(int((self.root / "assets" / "photos" / "r.jpg").stat().st_mtime), 1_700_000_000)


class Flow(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "assets" / "photos").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def send(self, name, data, mtime=1, chunk=4):
        """What the page does: start, chunks, finish. Returns the finish result."""
        s = uploads.start(self.root, name, len(data), mtime)
        if s["status"] == "have":
            return s
        off = s["offset"]
        while off < len(data):
            piece = data[off:off + chunk]
            off = uploads.append(self.root, s["id"], off, io.BytesIO(piece), len(piece))["offset"]
        return uploads.finish(self.root, s["id"])

    def photos(self):
        return sorted(p.name for p in (self.root / "assets" / "photos").iterdir())

    def test_a_chunked_upload_arrives_whole_under_its_name(self):
        r = self.send("a.jpg", b"0123456789")
        self.assertEqual(r["status"], "saved")
        self.assertEqual((self.root / "assets" / "photos" / "a.jpg").read_bytes(), b"0123456789")
        self.assertEqual(r["file"], {"name": "a.jpg", "folder": "photos", "size": 10})

    def test_a_video_goes_to_the_inbox_folder_which_is_created_on_demand(self):
        self.send("c.mp4", b"video bytes")
        self.assertEqual((self.root / "assets" / "inbox" / "c.mp4").read_bytes(), b"video bytes")

    def test_nothing_partial_is_ever_in_the_library(self):
        s = uploads.start(self.root, "a.jpg", 10, 1)
        uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        self.assertEqual(self.photos(), [])

    def test_the_wrong_offset_is_refused_and_reports_where_it_stands(self):
        s = uploads.start(self.root, "a.jpg", 10, 1)
        uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        with self.assertRaises(uploads.UploadError) as cm:
            uploads.append(self.root, s["id"], 2, io.BytesIO(b"xx"), 2)
        self.assertEqual((cm.exception.status, cm.exception.extra["offset"]), (409, 5))
        self.assertEqual(uploads.status(self.root, s["id"])["offset"], 5)

    def test_a_retried_chunk_after_a_lost_response_is_not_applied_twice(self):
        s = uploads.start(self.root, "a.jpg", 10, 1)
        uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        with self.assertRaises(uploads.UploadError) as cm:       # the client resends the same chunk
            uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        self.assertEqual(cm.exception.status, 409)
        uploads.append(self.root, s["id"], 5, io.BytesIO(b"56789"), 5)
        self.assertEqual(uploads.finish(self.root, s["id"])["status"], "saved")
        self.assertEqual((self.root / "assets" / "photos" / "a.jpg").read_bytes(), b"0123456789")

    def test_a_chunk_cut_short_leaves_the_file_as_last_acknowledged(self):
        s = uploads.start(self.root, "a.jpg", 10, 1)
        uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        with self.assertRaises(uploads.UploadError) as cm:       # promised 5 bytes, only 2 arrived
            uploads.append(self.root, s["id"], 5, io.BytesIO(b"56"), 5)
        self.assertEqual(cm.exception.extra["offset"], 5)
        self.assertEqual(uploads.status(self.root, s["id"])["offset"], 5)

    def test_a_dropped_connection_mid_chunk_is_handled_like_a_short_chunk(self):
        class Dropping:
            def __init__(self):
                self.n = 0

            def read(self, k):
                self.n += 1
                if self.n == 1:
                    return b"56"
                raise ConnectionResetError

        s = uploads.start(self.root, "a.jpg", 10, 1)
        uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        with self.assertRaises(uploads.UploadError):
            uploads.append(self.root, s["id"], 5, Dropping(), 5)
        self.assertEqual(uploads.status(self.root, s["id"])["offset"], 5)

    def test_more_data_than_declared_is_refused(self):
        s = uploads.start(self.root, "a.jpg", 4, 1)
        with self.assertRaises(uploads.UploadError) as cm:
            uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        self.assertEqual(cm.exception.status, 400)

    def test_an_oversized_or_empty_chunk_is_refused(self):
        s = uploads.start(self.root, "a.jpg", 10, 1)
        for n in (0, uploads.CHUNK_MAX + 1):
            with self.assertRaises(uploads.UploadError):
                uploads.append(self.root, s["id"], 0, io.BytesIO(b""), n)

    def test_finishing_early_is_refused_and_the_upload_can_continue(self):
        s = uploads.start(self.root, "a.jpg", 10, 1)
        uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        with self.assertRaises(uploads.UploadError) as cm:
            uploads.finish(self.root, s["id"])
        self.assertEqual((cm.exception.status, cm.exception.extra["offset"]), (409, 5))
        uploads.append(self.root, s["id"], 5, io.BytesIO(b"56789"), 5)
        self.assertEqual(uploads.finish(self.root, s["id"])["status"], "saved")

    def test_a_dropped_page_resumes_the_same_session_from_where_it_stopped(self):
        s = uploads.start(self.root, "a.jpg", 10, 7)
        uploads.append(self.root, s["id"], 0, io.BytesIO(b"01234"), 5)
        again = uploads.start(self.root, "a.jpg", 10, 7)             # the page reloaded and picked the file again
        self.assertEqual((again["status"], again["id"], again["offset"]), ("resume", s["id"], 5))

    def test_the_same_file_picked_again_is_recognised_before_any_bytes_move(self):
        self.send("a.jpg", b"0123456789", mtime=7)
        again = uploads.start(self.root, "a.jpg", 10, 7)
        self.assertEqual(again["status"], "have")
        self.assertEqual(self.photos(), ["a.jpg"])

    def test_the_same_content_under_another_name_is_stored_once(self):
        self.send("a.jpg", b"0123456789", mtime=1)
        r = self.send("copy of a.jpg", b"0123456789", mtime=2) if False else self.send("b.jpg", b"0123456789", mtime=2)
        self.assertEqual(r["status"], "duplicate")
        self.assertEqual(r["file"]["name"], "a.jpg")
        self.assertEqual(self.photos(), ["a.jpg"])
        self.assertEqual(list(uploads._dir(self.root).glob("*.part")), [])      # the duplicate's bytes are discarded

    def test_a_different_file_with_the_same_name_is_kept_under_a_new_name(self):
        self.send("a.jpg", b"0123456789", mtime=1)
        self.send("a.jpg", b"abcdefghij", mtime=2)
        self.assertEqual(self.photos(), ["a-1.jpg", "a.jpg"])
        self.assertEqual((self.root / "assets" / "photos" / "a.jpg").read_bytes(), b"0123456789")   # the first is untouched

    def test_a_file_already_in_the_folder_is_never_overwritten(self):
        (self.root / "assets" / "photos" / "a.jpg").write_bytes(b"precious")
        self.send("a.jpg", b"0123456789")
        self.assertEqual((self.root / "assets" / "photos" / "a.jpg").read_bytes(), b"precious")
        self.assertEqual(self.photos(), ["a-1.jpg", "a.jpg"])

    def test_the_index_survives_a_restart(self):
        self.send("a.jpg", b"0123456789", mtime=1)
        self.assertEqual(uploads.start(self.root, "a.jpg", 10, 1)["status"], "have")
        self.assertEqual(self.send("z.jpg", b"0123456789", mtime=9)["status"], "duplicate")

    def test_a_known_file_that_was_deleted_is_uploadable_again(self):
        self.send("a.jpg", b"0123456789", mtime=1)
        (self.root / "assets" / "photos" / "a.jpg").unlink()
        self.assertEqual(self.send("a.jpg", b"0123456789", mtime=1)["status"], "saved")

    def test_two_sessions_finishing_the_same_content_at_once_store_it_once(self):
        a = uploads.start(self.root, "a.jpg", 10, 1)
        b = uploads.start(self.root, "b.jpg", 10, 2)
        for s in (a, b):
            uploads.append(self.root, s["id"], 0, io.BytesIO(b"0123456789"), 10)
        results = []
        ts = [threading.Thread(target=lambda s=s: results.append(uploads.finish(self.root, s["id"])["status"])) for s in (a, b)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["duplicate", "saved"])
        self.assertEqual(len(self.photos()), 1)

    def test_an_unknown_session_is_a_404(self):
        for call in (lambda: uploads.status(self.root, "0" * 16),
                     lambda: uploads.append(self.root, "0" * 16, 0, io.BytesIO(b"x"), 1),
                     lambda: uploads.finish(self.root, "0" * 16)):
            with self.assertRaises(uploads.UploadError) as cm:
                call()
            self.assertEqual(cm.exception.status, 404)

    def test_a_file_that_would_fill_the_disk_is_refused_up_front(self):
        fake = shutil._ntuple_diskusage(100, 99, 1 << 30)            # 1 GB free, margin is 2 GB
        with mock.patch.object(uploads.shutil, "disk_usage", return_value=fake):
            with self.assertRaises(uploads.UploadError) as cm:
                uploads.start(self.root, "big.mp4", 1000, 1)
        self.assertEqual(cm.exception.status, 507)
        self.assertEqual(list(uploads._dir(self.root).glob("*.part")), [])

    def test_bad_sizes_are_refused(self):
        for size in (0, -1, "abc", None, uploads.MAX_FILE + 1):
            with self.assertRaises(uploads.UploadError):
                uploads.start(self.root, "a.jpg", size, 1)

    def test_a_session_nobody_resumed_for_a_week_is_swept(self):
        s = uploads.start(self.root, "old.mp4", 10, 1)
        old = time.time() - 8 * 86400
        import os
        os.utime(uploads._meta_path(self.root, s["id"]), (old, old))
        uploads.start(self.root, "new.mp4", 10, 2)                   # any start sweeps
        self.assertFalse(uploads._part(self.root, s["id"]).exists())

    def test_a_recent_session_is_not_swept(self):
        s = uploads.start(self.root, "keep.mp4", 10, 1)
        uploads.start(self.root, "other.mp4", 10, 2)
        self.assertTrue(uploads._part(self.root, s["id"]).exists())


@unittest.skipUnless(HAS_GIT, "needs git")
class GitStaysClean(unittest.TestCase):
    def test_uploads_never_dirty_a_project_repo(self):
        home = Path(tempfile.mkdtemp())
        root = Path(projects.create_project(home, "Demo")["path"])
        s = uploads.start(root, "a.jpg", 5, 1)
        uploads.append(root, s["id"], 0, io.BytesIO(b"12345"), 5)
        uploads.finish(root, s["id"])
        v = uploads.start(root, "c.mp4", 5, 2)
        uploads.append(root, v["id"], 0, io.BytesIO(b"12345"), 5)
        uploads.finish(root, v["id"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
        self.assertEqual(status, "")
        shutil.rmtree(home, ignore_errors=True)

    def test_an_older_project_without_the_new_ignore_lines_stays_clean_without_editing_its_gitignore(self):
        home = Path(tempfile.mkdtemp())
        root = Path(projects.create_project(home, "Old")["path"])
        gi = root / ".gitignore"
        gi.write_text(gi.read_text().replace("assets/inbox/\n", "").replace(".umcares/\n", ""))
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "older layout"], cwd=root, check=True)
        before = gi.read_text()
        s = uploads.start(root, "c.mp4", 5, 1)
        uploads.append(root, s["id"], 0, io.BytesIO(b"12345"), 5)
        uploads.finish(root, s["id"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
        self.assertEqual(status, "")
        self.assertEqual(gi.read_text(), before)                      # tracked file untouched
        shutil.rmtree(home, ignore_errors=True)

    def test_it_works_on_a_git_that_does_not_create_the_info_folder(self):
        home = Path(tempfile.mkdtemp())
        root = Path(projects.create_project(home, "Demo")["path"])
        shutil.rmtree(root / ".git" / "info", ignore_errors=True)
        self.assertTrue(projects.ensure_ignored(root))
        self.assertIn(".umcares/", (root / ".git" / "info" / "exclude").read_text().splitlines())
        shutil.rmtree(home, ignore_errors=True)

    def test_ensure_ignored_is_idempotent_and_skips_folders_that_are_not_repos(self):
        plain = Path(tempfile.mkdtemp())
        self.assertFalse(projects.ensure_ignored(plain))
        home = Path(tempfile.mkdtemp())
        root = Path(projects.create_project(home, "Demo")["path"])
        projects.ensure_ignored(root)
        self.assertFalse(projects.ensure_ignored(root))
        shutil.rmtree(plain, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
