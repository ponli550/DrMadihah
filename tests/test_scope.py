import json
import unittest
from unittest import mock

from umcares import cli, scope
from umcares.config import Config
from umcares.transport import Result

PROJECTS = [
    {"slug": "dormathon", "name": "Dormathon", "path": "/r/umcares-projects/dormathon", "missing": False},
    {"slug": "cyberdsa", "name": "CyberDSA", "path": "/r/umcares-projects/cyberdsa", "missing": False},
    {"slug": "drmadihah", "name": "DrMadihah", "path": "/r/DrMadihah", "external": True, "missing": False},
]


class FakeT:
    def __init__(self, projects=PROJECTS, rc=0, stdout=None):
        self.cmds = []
        self.projects, self.rc, self.stdout = projects, rc, stdout

    def run(self, cmd, timeout=120):
        self.cmds.append(cmd)
        return Result(self.rc, json.dumps(self.projects) if self.stdout is None else self.stdout)


class Args:
    def __init__(self, cmd, action=None, project_slug=None):
        self.cmd, self.action, self.project_slug = cmd, action, project_slug


def cfg_at(root="/r/DrMadihah"):
    c = Config()
    c.remote.root = root
    return c


class Which(unittest.TestCase):
    def test_what_counts_as_writing_or_building(self):
        for cmd, action in (("premiere", "build"), ("premiere", "import"), ("premiere", "sequence"), ("premiere", "open"), ("premiere", "export"),
                            ("media", "prepare"), ("media", "kenburns"), ("post", "mix"), ("render", None), ("init", None), ("ingest", None)):
            self.assertTrue(scope.writes(cmd, action), (cmd, action))
        for cmd, action in (("premiere", "status"), ("premiere", "heal"), ("premiere", "report"), ("premiere", "presets"), ("media", "probe"),
                            ("media", "levels"), ("post", "check"), ("inspect", None), ("doctor", None), ("verify", None), ("web", "deploy"),
                            ("config", "show"), ("remote", None)):
            self.assertFalse(scope.writes(cmd, action), (cmd, action))


class Deciding(unittest.TestCase):
    def test_a_named_project_gives_its_folder(self):
        self.assertEqual(scope.choose(PROJECTS, "cyberdsa", True), "/r/umcares-projects/cyberdsa")
        self.assertEqual(scope.choose(PROJECTS, "drmadihah", False), "/r/DrMadihah", "a registered external project resolves to where it lives")

    def test_an_unknown_project_is_refused_and_the_real_names_are_listed(self):
        with self.assertRaises(scope.ScopeError) as ctx:
            scope.choose(PROJECTS, "nope", True)
        for name in ("cyberdsa", "dormathon", "drmadihah"):
            self.assertIn(name, str(ctx.exception))

    def test_a_registered_project_whose_folder_is_gone_is_refused(self):
        gone = [{**PROJECTS[0], "missing": True}]
        with self.assertRaises(scope.ScopeError) as ctx:
            scope.choose(gone, "dormathon", True)
        self.assertIn("missing", str(ctx.exception))

    def test_writing_with_several_projects_and_no_name_is_refused(self):
        with self.assertRaises(scope.ScopeError) as ctx:
            scope.choose(PROJECTS, None, True)
        msg = str(ctx.exception)
        self.assertIn("3 projects", msg)
        self.assertIn("--project", msg)

    def test_with_one_project_or_none_nothing_changes(self):
        self.assertIsNone(scope.choose(PROJECTS[:1], None, True))
        self.assertIsNone(scope.choose([], None, True))

    def test_reading_never_needs_a_name(self):
        self.assertIsNone(scope.choose(PROJECTS, None, False))

    def test_naming_a_project_needs_the_list(self):
        with self.assertRaises(scope.ScopeError) as ctx:
            scope.choose(None, "cyberdsa", True)
        self.assertIn("web deploy", str(ctx.exception))


class Applying(unittest.TestCase):
    def test_a_read_only_command_without_a_name_does_not_even_connect(self):
        called = []
        out = scope.apply(Args("premiere", "status"), cfg_at(), lambda: called.append(1))
        self.assertEqual(called, [])
        self.assertEqual(out.remote.root, "/r/DrMadihah")

    def test_a_named_project_repoints_the_root_and_the_lookup_uses_the_original_root(self):
        t = FakeT()
        out = scope.apply(Args("premiere", "build", "cyberdsa"), cfg_at(), lambda: t)
        self.assertEqual(out.remote.root, "/r/umcares-projects/cyberdsa")
        self.assertIn("/r/DrMadihah/web/app", t.cmds[0], "the web app is found from the configured root, before it is replaced")

    def test_a_build_with_several_projects_and_no_name_is_refused_and_the_root_is_untouched(self):
        cfg = cfg_at()
        with self.assertRaises(scope.ScopeError):
            scope.apply(Args("premiere", "build"), cfg, lambda: FakeT())
        self.assertEqual(cfg.remote.root, "/r/DrMadihah")

    def test_a_build_with_a_single_project_goes_ahead(self):
        out = scope.apply(Args("premiere", "build"), cfg_at(), lambda: FakeT(PROJECTS[2:]))
        self.assertEqual(out.remote.root, "/r/DrMadihah")

    def test_when_the_list_cannot_be_read_a_write_goes_ahead_with_a_warning_but_naming_a_project_fails(self):
        warned = []
        out = scope.apply(Args("media", "prepare"), cfg_at(), lambda: FakeT(rc=1, stdout=""), warn=warned.append)
        self.assertEqual(out.remote.root, "/r/DrMadihah")
        self.assertTrue(warned and "could not check" in warned[0])
        with self.assertRaises(scope.ScopeError):
            scope.apply(Args("media", "prepare", "cyberdsa"), cfg_at(), lambda: FakeT(stdout="garbage"))

    def test_noise_before_the_json_is_tolerated(self):
        t = FakeT(stdout="Last login: today\n" + json.dumps(PROJECTS))
        self.assertEqual(len(scope.remote_projects(t, "/r/DrMadihah")), 3)

    def test_an_unreachable_remote_is_not_a_crash(self):
        class Boom:
            def run(self, *a, **k):
                raise OSError("no route")
        self.assertIsNone(scope.remote_projects(Boom(), "/r/DrMadihah"))


class Parsing(unittest.TestCase):
    def test_the_global_flag_and_the_premiere_prproj_flag_do_not_collide(self):
        a = cli.build_parser().parse_args(["--project", "cyberdsa", "premiere", "build", "--project", "/x/y.prproj"])
        self.assertEqual(a.project_slug, "cyberdsa")
        self.assertEqual(a.project, "/x/y.prproj")
        b = cli.build_parser().parse_args(["premiere", "status"])
        self.assertIsNone(b.project_slug)

    def test_main_refuses_with_exit_2_and_never_runs_the_command(self):
        ran = []
        with mock.patch.object(cli, "_transport", lambda a, c: FakeT()), mock.patch.object(cli, "cmd_premiere", lambda a, c: ran.append(1) or 0), \
                mock.patch.object(cli.Config, "load", classmethod(lambda cls: cfg_at())):
            parser_patch = cli.build_parser()
            for act in parser_patch._subparsers._group_actions[0].choices.values():
                if act.prog.endswith("premiere"):
                    act.set_defaults(func=lambda a, c: ran.append(1) or 0)
            with mock.patch.object(cli, "build_parser", lambda: parser_patch):
                rc = cli.main(["premiere", "build"])
        self.assertEqual(rc, 2)
        self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
