"""`umcares panels install`: a dry run by default, backups before any rewrite, and no key that writes. panvim and umcares are stubs on PATH and
HOME is a temp folder, so the real panvim config is never read or written."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from umcares import cli, panel, panels
from tests.test_views import FakeSite

HOME_KEYS = "# home\n[global]\n/\tshell\tterm-float\tcd {proj}\nd\tdoctor\tterm-float-hold\tumcares doctor\nq\tquit\tquit\t\n"
WRAPPER = "#!/bin/bash\n# umcares-popup\nCFG=\"$HOME/.config/panvim/umcares\"\nexec panvim view\n"


def stub(path: Path, body: str):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class PanelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".local" / "bin").mkdir(parents=True)
        (self.home / ".config" / "panvim" / "umcares").mkdir(parents=True)
        (self.home / ".config" / "panvim" / "umcares" / "keys.tsv").write_text(HOME_KEYS)
        (self.home / ".local" / "bin" / "umcares-popup").write_text(WRAPPER)
        self.calls = self.home / "calls.log"
        self.bindir = self.home / "stubs"
        self.bindir.mkdir()
        self.exists = "[]"
        self.audit_exit = 0
        self.write_stubs()
        self.env = mock_env(HOME=str(self.home), PATH=f"{self.bindir}:/usr/bin:/bin")
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def write_stubs(self):
        stub(self.bindir / "panvim", f'echo "$@" >> {self.calls}\n'
             f'case "$1" in popups) echo \'{self.exists}\';; audit) echo "audit output"; exit {self.audit_exit};; esac\nexit 0\n')
        stub(self.bindir / "umcares", "exit 0\n")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["panels", "install", *argv])
        return code, out.getvalue(), err.getvalue()

    def calls_text(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def cfg(self, *parts):
        return self.home.joinpath(".config", "panvim", *parts)

    def test_dry_run_changes_nothing(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertFalse(plan["committed"])
        self.assertEqual(len(plan["boards"]), 6)
        self.assertNotIn("new ", self.calls_text())
        self.assertEqual(self.cfg("umcares", "keys.tsv").read_text(), HOME_KEYS)
        self.assertFalse(self.cfg("umcares-jobs").exists())
        self.assertEqual(sorted(p.name for p in self.cfg("umcares").iterdir()), ["keys.tsv"])

    def test_commit_creates_hidden_boards_with_the_row_rule(self):
        code, out, _ = self.run_cli("--commit")
        self.assertEqual(code, 0)
        news = [l for l in self.calls_text().splitlines() if l.startswith("new ")]
        self.assertEqual(len(news), 6)
        for l in news:
            self.assertIn("--hidden", l)
            self.assertIn("--commit", l)
            self.assertIn("^  (%S+)", l)
        self.assertTrue(any("umcares panel jobs" in l for l in news))
        self.assertIn("audit", self.calls_text())
        for name in panels.BOARDS:
            self.assertTrue(self.cfg(name, "keys.tsv").exists() and self.cfg(name, "syntax.tsv").exists())

    def test_an_existing_board_is_not_created_again(self):
        self.exists = json.dumps([{"name": "umcares-jobs"}])
        self.write_stubs()
        self.run_cli("--commit")
        news = [l for l in self.calls_text().splitlines() if l.startswith("new ")]
        self.assertEqual(len(news), 5)
        self.assertFalse(any(l.startswith("new umcares-jobs ") for l in news))
        self.assertTrue(self.cfg("umcares-jobs", "keys.tsv").exists())      # its keys are still brought up to date

    def test_no_key_writes_or_passes_commit(self):
        for name in panels.BOARDS:
            text = panels.board_keys(name)
            self.assertNotIn("--commit", text)
            for line in text.splitlines():
                if line.startswith("#") or line.startswith("["):
                    continue
                cols = line.split("\t")
                self.assertGreaterEqual(len(cols), 3, line)
                self.assertFalse(len(cols[0]) == 1 and cols[0].isupper(), f"{name}: an uppercase key {cols[0]}")
                self.assertNotRegex(line, r"umcares (render|push|pull|premiere|cancel)")
        for line in panels.home_block().splitlines():
            self.assertNotIn("--commit", line)

    def test_keys_use_row_only_where_a_row_exists(self):
        self.assertIn("{row}", panels.board_keys("umcares-projects"))
        self.assertIn("{row}", panels.board_keys("umcares-recipes"))
        for name in ("umcares-project", "umcares-plan", "umcares-exports"):
            self.assertNotIn("{row}", panels.board_keys(name))

    def test_a_board_does_not_link_to_itself(self):
        text = panels.board_keys("umcares-jobs")
        self.assertNotIn("popup\tumcares-jobs", text)
        self.assertIn("popup\tumcares-projects", text)

    def test_home_keys_keep_every_old_line_and_get_one_block(self):
        self.run_cli("--commit")
        text = self.cfg("umcares", "keys.tsv").read_text()
        for old in HOME_KEYS.splitlines():
            self.assertIn(old, text.splitlines())
        self.assertEqual(text.count(panels.START), 1)
        self.assertLess(text.index(panels.START), text.index("\nq\tquit"))
        self.run_cli("--commit")
        self.assertEqual(self.cfg("umcares", "keys.tsv").read_text(), text)        # idempotent

    def test_home_wrapper_gets_one_comment_once(self):
        self.run_cli("--commit")
        self.run_cli("--commit")
        w = (self.home / ".local" / "bin" / "umcares-popup").read_text()
        self.assertEqual(w.count("boards below read the web app"), 1)
        self.assertIn('exec panvim view', w)

    def test_backups_only_when_something_changes(self):
        _, out1, _ = self.run_cli("--commit")
        first = json.loads(out1)["backups"]
        self.assertEqual(len(first), 1)                                  # only the home keys existed
        self.assertTrue(Path(first[0]).read_text() == HOME_KEYS)
        self.exists = json.dumps([{"name": n} for n in panels.BOARDS])       # panvim now lists the boards the first run made
        self.write_stubs()
        _, out2, _ = self.run_cli("--commit")
        self.assertEqual(json.loads(out2)["backups"], [])
        self.cfg("umcares-jobs", "keys.tsv").write_text("hand edited\n")
        _, out3, _ = self.run_cli("--commit")
        b = json.loads(out3)["backups"]
        self.assertEqual(len(b), 1)
        self.assertEqual(Path(b[0]).read_text(), "hand edited\n")

    def test_missing_panvim_or_umcares_is_exit_1(self):
        (self.bindir / "panvim").unlink()
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("panvim is not on PATH", err)

    def test_audit_findings_are_exit_2_and_reported(self):
        self.audit_exit = 1
        self.write_stubs()
        code, out, _ = self.run_cli("--commit")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["panvim_audit"]["exit"], 1)

    def test_a_failed_panvim_new_is_exit_1_and_named(self):
        stub(self.bindir / "panvim", 'case "$1" in popups) echo "[]";; new) echo boom >&2; exit 9;; esac\nexit 0\n')
        code, out, _ = self.run_cli("--commit")
        self.assertEqual(code, 1)
        self.assertEqual(len(json.loads(out)["failures"]), 6)

    def test_the_web_url_is_baked_only_when_given(self):
        self.run_cli("--commit", "--url", "https://x.example/umcares")
        self.assertIn("UMC_WEB_URL=https://x.example/umcares umcares panel jobs", self.calls_text())

    def test_syntax_has_no_slash_in_any_pattern(self):
        for line in panels.SYNTAX.splitlines():
            if not line.startswith("#"):
                self.assertNotIn("/", line.split("\t", 2)[2])


def mock_env(**kw):
    from unittest import mock
    return mock.patch.dict(os.environ, kw)


class SetRecipeTest(unittest.TestCase):
    def test_set_recipe_keeps_the_project(self):
        from tests.test_panel import site
        with tempfile.TemporaryDirectory() as d, mock_env(UMCARES_PANEL_STATE=str(Path(d) / "s.json"), UMC_WEB_URL="http://x"):
            panel.set_state("alpha")
            args = SimpleNamespace(view="set", choice=[], url=None, recipe="good_one")
            out = io.StringIO()
            from unittest import mock
            with mock.patch.object(cli.draft_mod, "Site", return_value=site()), contextlib.redirect_stdout(out):
                code = cli.cmd_panel(args, SimpleNamespace(env={}))
            self.assertEqual(code, 0)
            self.assertEqual(panel.load_state(), {"project": "alpha", "recipe": "good one"})

    def test_set_recipe_without_a_project_is_exit_1(self):
        with tempfile.TemporaryDirectory() as d, mock_env(UMCARES_PANEL_STATE=str(Path(d) / "s.json"), UMC_WEB_URL="http://x"):
            from tests.test_panel import site
            from unittest import mock
            args = SimpleNamespace(view="set", choice=[], url=None, recipe="x")
            with mock.patch.object(cli.draft_mod, "Site", return_value=site()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.cmd_panel(args, SimpleNamespace(env={})), 1)


if __name__ == "__main__":
    unittest.main()
