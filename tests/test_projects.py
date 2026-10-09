import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umcares import layout, projects

HAS_GIT = shutil.which("git") is not None


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True).stdout


class Slugify(unittest.TestCase):
    def test_lowercase_ascii_with_hyphens(self):
        self.assertEqual(projects.slugify("UM Cares: Sesi 2"), "um-cares-sesi-2")

    def test_accents_are_folded_not_dropped(self):
        self.assertEqual(projects.slugify("Café Ráya"), "cafe-raya")

    def test_a_traversal_attempt_is_just_a_name(self):
        self.assertEqual(projects.slugify("../../etc/passwd"), "etc-passwd")

    def test_a_name_with_no_letters_is_refused(self):
        with self.assertRaises(projects.ProjectError):
            projects.slugify("项目")
        with self.assertRaises(projects.ProjectError):
            projects.slugify("---")

    def test_reserved_names_are_refused(self):
        with self.assertRaises(projects.ProjectError):
            projects.slugify("Registry")

    def test_long_names_are_cut_without_a_trailing_hyphen(self):
        s = projects.slugify("a" * 39 + " b" * 5)
        self.assertLessEqual(len(s), 40)
        self.assertFalse(s.endswith("-"))


class Create(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_makes_the_standard_tree_and_the_three_files(self):
        r = projects.create_project(self.home, "Hari Raya 2026", "2026-04-01", "Open house")
        root = Path(r["path"])
        self.assertEqual(root.name, "hari-raya-2026")
        for rel, _ in layout.TREE:
            self.assertTrue((root / rel).is_dir(), rel)
        self.assertTrue((root / "recipes").is_dir())
        for f in ("project.json", "README.md", ".gitignore"):
            self.assertTrue((root / f).is_file(), f)
        meta = json.loads((root / "project.json").read_text())
        self.assertEqual((meta["name"], meta["event_date"], meta["status"]), ("Hari Raya 2026", "2026-04-01", "active"))

    def test_the_readme_states_that_media_is_not_in_git(self):
        r = projects.create_project(self.home, "Demo", notes="Some notes")
        text = (Path(r["path"]) / "README.md").read_text()
        self.assertIn("# Demo", text)
        self.assertIn("Some notes", text)
        self.assertIn("Media is not in", text)

    def test_a_duplicate_name_is_refused_and_the_first_is_untouched(self):
        projects.create_project(self.home, "Demo", notes="first")
        with self.assertRaises(projects.ProjectError):
            projects.create_project(self.home, "demo!", notes="second")
        meta = json.loads((self.home / "demo" / "project.json").read_text())
        self.assertEqual(meta["notes"], "first")

    def test_a_bad_date_is_refused_before_anything_is_made(self):
        with self.assertRaises(projects.ProjectError):
            projects.create_project(self.home, "Demo", "9 Oct")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_failure_while_building_leaves_nothing_behind(self):
        with mock.patch.object(projects, "_write_tree", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                projects.create_project(self.home, "Demo")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_leftover_from_a_crash_does_not_block_a_retry(self):
        (self.home / ".demo.part").mkdir()
        (self.home / ".demo.part" / "junk").write_text("x")
        projects.create_project(self.home, "Demo")
        self.assertTrue((self.home / "demo" / "project.json").is_file())
        self.assertFalse((self.home / ".demo.part").exists())

    @unittest.skipUnless(HAS_GIT, "needs git")
    def test_is_a_git_repo_with_one_commit_and_media_ignored(self):
        r = projects.create_project(self.home, "Demo")
        root = Path(r["path"])
        self.assertTrue(r["git"]["committed"], r["git"])
        self.assertEqual(len(git(root, "log", "--oneline").splitlines()), 1)
        (root / "assets" / "footage" / "A" / "C0001.mp4").write_bytes(b"x" * 100)
        (root / "exports" / "master.mp4").write_bytes(b"x")
        (root / "recipes" / "v1.json").write_text("{}")
        status = git(root, "status", "--porcelain")
        self.assertNotIn("C0001", status)
        self.assertNotIn("master.mp4", status)
        self.assertIn("recipes/", status)             # recipes are versioned

    @unittest.skipUnless(HAS_GIT, "needs git")
    def test_commits_on_a_machine_with_no_git_identity(self):
        """The Premiere machine has none: a plain `git commit` there refuses."""
        empty = Path(tempfile.mkdtemp())
        env = {"HOME": str(empty), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
               "PATH": os.environ["PATH"]}
        with mock.patch.dict(os.environ, env, clear=True):
            r = projects.create_project(self.home, "Demo")
        self.assertTrue(r["git"]["committed"], r["git"])
        shutil.rmtree(empty, ignore_errors=True)

    def test_a_missing_git_does_not_fail_creation(self):
        with mock.patch.object(projects, "_git", side_effect=FileNotFoundError("git")):
            r = projects.create_project(self.home, "Demo")
        self.assertFalse(r["git"]["initialised"])
        self.assertTrue((Path(r["path"]) / "project.json").is_file())

    def test_no_git_when_asked(self):
        r = projects.create_project(self.home, "Demo", init_git=False)
        self.assertFalse((Path(r["path"]) / ".git").exists())


class Listing(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_lists_projects_newest_event_first(self):
        projects.create_project(self.home, "Old", "2026-01-01", init_git=False)
        projects.create_project(self.home, "New", "2026-09-01", init_git=False)
        projects.create_project(self.home, "Undated", init_git=False)
        self.assertEqual([p["slug"] for p in projects.list_projects(self.home)], ["new", "old", "undated"])

    def test_ignores_hidden_folders_stray_files_and_broken_json(self):
        projects.create_project(self.home, "Ok", init_git=False)
        (self.home / ".hidden").mkdir()
        (self.home / "stray.txt").write_text("x")
        (self.home / "broken").mkdir()
        (self.home / "broken" / "project.json").write_text("{nope")
        self.assertEqual([p["slug"] for p in projects.list_projects(self.home)], ["ok"])

    def test_an_external_project_is_registered_not_moved(self):
        ext = Path(tempfile.mkdtemp())
        (ext / "keep.txt").write_text("mine")
        projects.register_external(self.home, "drmadihah", "UM Cares", ext)
        p = projects.find_project(self.home, "drmadihah")
        self.assertTrue(p["external"])
        self.assertFalse(p["missing"])
        self.assertEqual(sorted(x.name for x in ext.iterdir()), ["keep.txt"])      # untouched
        shutil.rmtree(ext)
        self.assertTrue(projects.find_project(self.home, "drmadihah")["missing"])

    def test_a_registered_slug_blocks_creating_the_same_name(self):
        ext = Path(tempfile.mkdtemp())
        projects.register_external(self.home, "drmadihah", "UM Cares", ext)
        with self.assertRaises(projects.ProjectError):
            projects.create_project(self.home, "DrMadihah", init_git=False)
        shutil.rmtree(ext)

    def test_registering_a_path_that_is_not_a_folder_is_refused(self):
        with self.assertRaises(projects.ProjectError):
            projects.register_external(self.home, "x", "X", self.home / "nope")

    def test_an_unknown_slug_is_none(self):
        self.assertIsNone(projects.find_project(self.home, "nope"))


if __name__ == "__main__":
    unittest.main()
