"""Staged writes.

The scenario these defend against actually happened: two title cards were
deleted to force a regeneration, the render API turned out to be out of credits,
and the originals no longer existed. The property that matters is not "the write
succeeds" but "a FAILED write leaves the previous file exactly as it was".
"""
import tempfile
import unittest
from pathlib import Path

from umcares import safe


class PartPath(unittest.TestCase):
    def test_keeps_the_extension(self):
        """Encoders and Premiere both pick behaviour from the extension."""
        self.assertEqual(safe.part_path("/x/master.mxf"), "/x/master.part.mxf")
        self.assertEqual(safe.part_path("/x/a.b.mp4"), "/x/a.b.part.mp4")

    def test_handles_a_name_without_an_extension(self):
        self.assertEqual(safe.part_path("/x/master"), "/x/master.part")

    def test_is_a_sibling_so_the_move_is_atomic(self):
        self.assertEqual(Path(safe.part_path("/x/y/a.mp4")).parent, Path("/x/y"))

    def test_a_dot_in_a_directory_name_is_not_an_extension(self):
        self.assertEqual(safe.part_path("/x.y/master"), "/x.y/master.part")


class StagedLocal(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.dest = self.dir / "out.mp4"

    def test_commits_a_good_write(self):
        with safe.staged_local(self.dest) as tmp:
            tmp.write_bytes(b"new content")
        self.assertEqual(self.dest.read_bytes(), b"new content")

    def test_writes_go_to_a_temp_file_first(self):
        with safe.staged_local(self.dest) as tmp:
            tmp.write_bytes(b"x")
            self.assertFalse(self.dest.exists(), "destination touched too early")

    # -- the property that matters -----------------------------------------
    def test_a_failed_write_leaves_the_original_untouched(self):
        self.dest.write_bytes(b"the good original")
        with self.assertRaises(RuntimeError):
            with safe.staged_local(self.dest) as tmp:
                tmp.write_bytes(b"half a file")
                raise RuntimeError("render died")
        self.assertEqual(self.dest.read_bytes(), b"the good original")

    def test_a_failed_write_leaves_no_temp_litter(self):
        self.dest.write_bytes(b"original")
        with self.assertRaises(RuntimeError):
            with safe.staged_local(self.dest) as tmp:
                tmp.write_bytes(b"partial")
                raise RuntimeError("boom")
        self.assertFalse(Path(safe.part_path(str(self.dest))).exists())

    def test_an_empty_result_does_not_replace_a_good_file(self):
        self.dest.write_bytes(b"original")
        with self.assertRaises(SystemExit):
            with safe.staged_local(self.dest) as tmp:
                tmp.write_bytes(b"")
        self.assertEqual(self.dest.read_bytes(), b"original")

    def test_a_result_under_the_minimum_does_not_replace(self):
        self.dest.write_bytes(b"original")
        with self.assertRaises(SystemExit):
            with safe.staged_local(self.dest, min_bytes=1000) as tmp:
                tmp.write_bytes(b"too small")
        self.assertEqual(self.dest.read_bytes(), b"original")

    def test_a_write_that_never_happened_does_not_replace(self):
        self.dest.write_bytes(b"original")
        with self.assertRaises(SystemExit):
            with safe.staged_local(self.dest):
                pass
        self.assertEqual(self.dest.read_bytes(), b"original")

    def test_creates_missing_parent_directories(self):
        nested = self.dir / "a" / "b" / "out.mp4"
        with safe.staged_local(nested) as tmp:
            tmp.write_bytes(b"ok")
        self.assertTrue(nested.exists())

    def test_stale_temp_from_an_earlier_crash_is_ignored(self):
        Path(safe.part_path(str(self.dest))).write_bytes(b"stale junk")
        with safe.staged_local(self.dest) as tmp:
            tmp.write_bytes(b"fresh")
        self.assertEqual(self.dest.read_bytes(), b"fresh")


class BackupLocal(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.dest = self.dir / "subtitles.srt"

    def test_keeps_a_copy_of_hand_edited_content(self):
        self.dest.write_text("client corrections", encoding="utf-8")
        prev = safe.backup_local(self.dest)
        self.assertEqual(prev.read_text(encoding="utf-8"), "client corrections")

    def test_no_backup_when_there_is_nothing_to_lose(self):
        self.assertIsNone(safe.backup_local(self.dest))

    def test_backup_survives_a_regeneration(self):
        self.dest.write_text("old", encoding="utf-8")
        prev = safe.backup_local(self.dest)
        with safe.staged_local(self.dest) as tmp:
            tmp.write_text("new", encoding="utf-8")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "new")
        self.assertEqual(prev.read_text(encoding="utf-8"), "old")


class CommitRemote(unittest.TestCase):
    """`commit_remote` shells out, so the transport is faked."""

    class FakeT:
        def __init__(self, stdout):
            self._stdout = stdout
            self.commands = []

        def run(self, cmd, timeout=None):
            self.commands.append(cmd)
            return type("R", (), {"stdout": self._stdout, "stderr": ""})()

    def test_commits_when_the_staged_file_is_good(self):
        t = self.FakeT("COMMITTED\n")
        safe.commit_remote(t, "/x/out.mp4")
        self.assertIn("mv -f", t.commands[0])
        self.assertIn("out.part.mp4", t.commands[0])

    def test_refuses_and_cleans_up_when_the_staged_file_is_empty(self):
        t = self.FakeT("EMPTY\n")
        with self.assertRaises(SystemExit):
            safe.commit_remote(t, "/x/out.mp4")
        self.assertTrue(any("rm -f" in c for c in t.commands),
                        "the staged file must not be left behind")

    def test_never_touches_the_destination_when_refusing(self):
        t = self.FakeT("EMPTY\n")
        with self.assertRaises(SystemExit):
            safe.commit_remote(t, "/x/out.mp4")
        for c in t.commands:
            self.assertNotIn("mv -f '/x/out.part.mp4' '/x/out.mp4'", c)


if __name__ == "__main__":
    unittest.main()
