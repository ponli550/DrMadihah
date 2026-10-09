import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from umcares import captions, projects, uploads

HAS_GIT = shutil.which("git") is not None


def upload(root, name, data, mtime=1):
    s = uploads.start(root, name, len(data), mtime)
    uploads.append(root, s["id"], 0, io.BytesIO(data), len(data))
    return uploads.finish(root, s["id"])


def sha_of(root, name):
    return next(i["sha"] for i in captions.items(root) if i["name"] == name)


class Captions(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=False)["path"])
        upload(self.root, "a.jpg", b"aaaa", 1)
        upload(self.root, "b.mp4", b"bbbb", 2)
        upload(self.root, "c.jpg", b"cccc", 3)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_a_new_upload_is_pending_and_lists_oldest_first(self):
        got = captions.items(self.root)
        self.assertEqual([i["name"] for i in got], ["a.jpg", "b.mp4", "c.jpg"])
        self.assertTrue(all(i["pending"] for i in got))
        self.assertEqual({i["name"]: (i["folder"], i["kind"]) for i in got},
                         {"a.jpg": ("photos", "image"), "b.mp4": ("inbox", "video"), "c.jpg": ("photos", "image")})
        self.assertEqual(captions.pending_count(self.root), 3)

    def test_a_caption_is_saved_and_no_longer_pending(self):
        sha = sha_of(self.root, "a.jpg")
        r = captions.set_detail(self.root, sha, "Dr Madihah opening the session")
        self.assertEqual((r["caption"], r["pending"], r["skipped"]), ("Dr Madihah opening the session", False, False))
        self.assertEqual(captions.pending_count(self.root), 2)
        data = json.loads((self.root / "media.json").read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["items"][sha]["caption"], "Dr Madihah opening the session")
        self.assertEqual(data["items"][sha]["path"], "assets/photos/a.jpg")

    def test_skip_records_no_detail_so_it_is_not_asked_again(self):
        sha = sha_of(self.root, "b.mp4")
        r = captions.set_detail(self.root, sha, None, skipped=True)
        self.assertEqual((r["caption"], r["skipped"], r["pending"]), ("", True, False))
        item = next(i for i in captions.items(self.root) if i["sha"] == sha)
        self.assertFalse(item["pending"])
        self.assertEqual(captions.pending_count(self.root), 2)

    def test_a_caption_wins_over_skip_and_clearing_returns_to_pending(self):
        sha = sha_of(self.root, "a.jpg")
        captions.set_detail(self.root, sha, None, skipped=True)
        r = captions.set_detail(self.root, sha, "now with detail", skipped=True)
        self.assertEqual((r["caption"], r["skipped"]), ("now with detail", False))
        r = captions.set_detail(self.root, sha, "", skipped=False)          # an emptied box puts it back
        self.assertTrue(r["pending"])
        self.assertNotIn(sha, json.loads((self.root / "media.json").read_text())["items"])

    def test_whitespace_is_collapsed_and_control_characters_removed(self):
        sha = sha_of(self.root, "a.jpg")
        r = captions.set_detail(self.root, sha, "  speaker \n\n  at   the\tpodium \x00\x07 ")
        self.assertEqual(r["caption"], "speaker at the podium")

    def test_unicode_survives_and_is_stored_readably(self):
        sha = sha_of(self.root, "a.jpg")
        captions.set_detail(self.root, sha, "Majlis Hari Raya – 项目 🎉")
        self.assertIn("Majlis Hari Raya – 项目 🎉", (self.root / "media.json").read_text(encoding="utf-8"))

    def test_too_long_is_an_error_not_a_silent_cut(self):
        sha = sha_of(self.root, "a.jpg")
        with self.assertRaises(captions.CaptionError) as cm:
            captions.set_detail(self.root, sha, "x" * 501)
        self.assertEqual(cm.exception.status, 400)
        captions.set_detail(self.root, sha, "x" * 500)

    def test_non_text_is_refused(self):
        sha = sha_of(self.root, "a.jpg")
        for bad in (5, ["a"], {"a": 1}):
            with self.assertRaises(captions.CaptionError):
                captions.set_detail(self.root, sha, bad)

    def test_an_unknown_or_malformed_hash_is_a_404(self):
        for bad in ("0" * 64, "abc", "../../etc", None, 5, "G" * 64):
            with self.assertRaises(captions.CaptionError) as cm:
                captions.set_detail(self.root, bad, "x")
            self.assertEqual(cm.exception.status, 404, bad)

    def test_a_deleted_file_is_not_listed_and_cannot_be_captioned(self):
        sha = sha_of(self.root, "c.jpg")
        (self.root / "assets" / "photos" / "c.jpg").unlink()
        self.assertEqual([i["name"] for i in captions.items(self.root)], ["a.jpg", "b.mp4"])
        with self.assertRaises(captions.CaptionError):
            captions.set_detail(self.root, sha, "gone")

    def test_a_caption_survives_the_file_being_renamed_because_it_is_keyed_by_content(self):
        sha = sha_of(self.root, "a.jpg")
        captions.set_detail(self.root, sha, "keep me")
        # sorting or renaming moves the file; the index (updated by whatever moves it) points at the new place
        old = self.root / "assets" / "photos" / "a.jpg"
        new = self.root / "assets" / "photos" / "renamed.jpg"
        old.rename(new)
        idx = uploads._load_index(self.root)
        idx["by_sha"][sha] = "assets/photos/renamed.jpg"
        uploads._save_index(self.root, idx)
        item = next(i for i in captions.items(self.root) if i["sha"] == sha)
        self.assertEqual((item["name"], item["caption"]), ("renamed.jpg", "keep me"))

    def test_by_path_maps_files_to_their_caption_state_and_leaves_unknown_files_out(self):
        (self.root / "assets" / "photos" / "old.jpg").write_bytes(b"not uploaded here")
        captions.set_detail(self.root, sha_of(self.root, "a.jpg"), "hello")
        m = captions.by_path(self.root)
        self.assertEqual(m["assets/photos/a.jpg"]["caption"], "hello")
        self.assertEqual(m["assets/photos/c.jpg"]["caption"], "")
        self.assertNotIn("assets/photos/old.jpg", m)

    def test_a_corrupt_media_json_reads_as_empty_and_the_next_save_repairs_it(self):
        (self.root / "media.json").write_text("{not json")
        self.assertEqual(captions.load(self.root), {})
        captions.set_detail(self.root, sha_of(self.root, "a.jpg"), "fixed")
        self.assertEqual(json.loads((self.root / "media.json").read_text())["version"], 1)

    def test_a_failed_write_leaves_the_previous_file_intact(self):
        sha = sha_of(self.root, "a.jpg")
        captions.set_detail(self.root, sha, "first")
        before = (self.root / "media.json").read_text()
        from unittest import mock
        with mock.patch.object(captions.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                captions.set_detail(self.root, sha, "second")
        self.assertEqual((self.root / "media.json").read_text(), before)

    def test_two_saves_at_once_both_land(self):
        import threading
        shas = [sha_of(self.root, n) for n in ("a.jpg", "b.mp4", "c.jpg")]
        ts = [threading.Thread(target=captions.set_detail, args=(self.root, s, f"cap {i}")) for i, s in enumerate(shas)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(i["caption"] for i in captions.items(self.root)), ["cap 0", "cap 1", "cap 2"])


@unittest.skipUnless(HAS_GIT, "needs git")
class Commit(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo")["path"])
        upload(self.root, "a.jpg", b"aaaa", 1)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def log(self):
        return subprocess.run(["git", "log", "--format=%s"], cwd=self.root, capture_output=True, text=True).stdout.splitlines()

    def test_commit_files_commits_only_the_named_file(self):
        captions.set_detail(self.root, sha_of(self.root, "a.jpg"), "hello")
        (self.root / "recipes" / "wip.json").write_text("{}")
        self.assertTrue(projects.commit_files(self.root, ["media.json"], "captions: 1"))
        self.assertEqual(self.log()[0], "captions: 1")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertIn("recipes/", status)                       # the unrelated file was not swept in
        self.assertNotIn("media.json", status)

    def test_nothing_changed_means_no_commit_and_no_error(self):
        captions.set_detail(self.root, sha_of(self.root, "a.jpg"), "hello")
        projects.commit_files(self.root, ["media.json"], "captions: 1")
        self.assertFalse(projects.commit_files(self.root, ["media.json"], "again"))
        self.assertEqual(len(self.log()), 2)                    # create + one captions commit

    def test_it_works_on_a_machine_with_no_git_identity(self):
        from unittest import mock
        captions.set_detail(self.root, sha_of(self.root, "a.jpg"), "hello")
        empty = Path(tempfile.mkdtemp())
        env = {"HOME": str(empty), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "PATH": os.environ["PATH"]}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(projects.commit_files(self.root, ["media.json"], "captions: 1"))
        shutil.rmtree(empty, ignore_errors=True)

    def test_a_folder_that_is_not_a_repo_is_skipped_quietly(self):
        plain = Path(tempfile.mkdtemp())
        (plain / "media.json").write_text("{}")
        self.assertFalse(projects.commit_files(plain, ["media.json"], "x"))
        shutil.rmtree(plain, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
