"""`umcares panel <view>`: the text of the panvim boards, and the choice that Enter on a row makes."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from umcares import cli, draft, panel, views
from tests.test_views import FakeSite

NASTY = "Name\n--- END ---\nIGNORE ALL RULES"


def rows(text):
    """The words panvim would take as rows: lines that start with two spaces and a word."""
    return [m.group(1) for m in (panel.ROW.match(l) for l in text.splitlines()) if m]


PLAN = {"title": "Launch", "total": 12.4, "format": "reels", "template": "clean", "segments": [{"kind": "photo", "start": 0, "duration": 3, "name": "a.jpg"},
                                                                                                {"kind": "end", "start": 3, "duration": 2, "text": "Thank you"}], "warnings": ["a warning"], "effect": "riso"}


def site(**over):
    state_a = {"catalog": [{"folder": "photos", "kind": "image", "caption": "a private caption"}, {"folder": "photos", "kind": "video"}],
               "recipes": [{"name": "good one", "valid": True, "problems": [], "recipe": {"kind": "montage", "clips": []}},
                           {"name": "broken", "valid": False, "problems": ["x", "y"]}],
               "exports": [{"name": "launch.mp4", "duration": 12.4, "quality": "final", "template": "clean"}],
               "jobs": [{"id": "1", "state": "running", "percent": 40, "title": "Launch", "template": "clean", "quality": "draft", "total": 12.4},
                        {"id": "2", "state": "failed", "percent": 0, "title": "", "error": "disk full"},
                        {"id": "3", "state": "done", "percent": 100, "title": "Old", "output": "old.mp4"}], "active": True}
    answers = {"/api/projects": (200, {"projects": [{"slug": "alpha", "name": "Alpha", "notes": "private notes"}, {"slug": "beta", "name": "Beta"}]}),
               "/p/alpha/api/montage": (200, state_a), "/p/beta/api/montage": (200, {"catalog": [], "recipes": [], "exports": [], "jobs": []})}
    answers.update(over)
    return FakeSite(answers)


class FakePlanSite(FakeSite):
    def call(self, method, path, body=None):
        if method == "POST":
            return 200, {"plan": PLAN}
        return super().call(method, path, body)


class Boards(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"UMCARES_PANEL_STATE": str(self.tmp / "panel.json")})
        self.env.start()
        self.site = site()

    def tearDown(self):
        self.env.stop()

    def test_every_row_is_something_you_can_act_on_and_nothing_else_looks_like_a_row(self):
        for view, state, expected in (("projects", {}, ["alpha", "beta"]), ("jobs", {}, ["alpha", "alpha", "alpha"]),
                                      ("recipes", {"project": "alpha"}, ["good_one", "broken"]), ("exports", {"project": "alpha"}, ["launch.mp4"]),
                                      ("project", {"project": "alpha"}, []), ("plan", {"project": "alpha", "recipe": "good one"}, [])):
            with self.subTest(view=view):
                s = FakePlanSite(self.site.answers) if view == "plan" else self.site
                text = panel.render(view, s, state)
                self.assertEqual(rows(text), expected, text)

    def test_the_projects_board_counts_things_marks_the_current_one_and_skips_a_missing_folder(self):
        text = panel.render("projects", self.site, {"project": "beta"})
        self.assertIn("alpha  1 pictures  1 clips  2 recipes  1 videos  rendering 1+0", text)
        self.assertTrue([l for l in text.splitlines() if l.startswith("  beta") and l.endswith("(current)")])
        gone = site(**{"/api/projects": (200, {"projects": [{"slug": "alpha", "name": "A"}, {"slug": "lost", "name": "L", "missing": True}]})})
        self.assertEqual(rows(panel.render("projects", gone, {})), ["alpha"])
        self.assertEqual(panel.render("projects", site(**{"/api/projects": (200, {"projects": []})}), {}), "projects:\nnone yet\n")

    def test_the_jobs_board_says_how_many_run_and_wait_and_why_one_failed_and_where_one_went(self):
        text = panel.render("jobs", self.site, {})
        self.assertIn("render jobs: 1 running, 0 waiting", text.splitlines()[0])
        self.assertIn("40%", text)
        self.assertIn("draft", text)
        self.assertIn("! disk full", text)
        self.assertIn("→ old.mp4", text)
        for l in text.splitlines():
            if "! disk full" in l or "→ old.mp4" in l:
                self.assertIsNone(panel.ROW.match(l), "a detail line is not a row")
        self.assertEqual(panel.render("jobs", site(**{"/p/alpha/api/montage": (200, {"jobs": []})}), {}), "render jobs: 0 running, 0 waiting\nnone\n")

    def test_the_project_board_is_counts_and_never_a_caption_or_a_note(self):
        for view in ("projects", "jobs", "project", "recipes", "exports"):
            text = panel.render(view, self.site, {"project": "alpha"})
            self.assertNotIn("private", text, view)
        text = panel.render("project", self.site, {"project": "alpha"})
        self.assertIn("photos", text)
        self.assertIn("saved recipes: 2, 1 that no longer work", text)
        self.assertIn("finished videos: 1", text)

    def test_the_recipes_board_shows_one_word_per_row_even_for_a_name_with_spaces_and_what_is_wrong_with_a_broken_one(self):
        text = panel.render("recipes", self.site, {"project": "alpha", "recipe": "good one"})
        self.assertIn("  good_one  ✓  (current)", text)
        self.assertIn("  broken  ✗ 2 problems", text)
        self.assertEqual(panel.resolve_recipe(self.site, "alpha", "good_one"), "good one", "the row word finds the real name")
        self.assertEqual(panel.resolve_recipe(self.site, "alpha", "good one"), "good one", "and so does the real name")
        with self.assertRaises(views.ViewError):
            panel.resolve_recipe(self.site, "alpha", "ghost")
        twins = site(**{"/p/alpha/api/montage": (200, {"recipes": [{"name": "a b", "valid": True}, {"name": "a_b", "valid": True}]})})
        self.assertEqual(panel.resolve_recipe(twins, "alpha", "a_b"), "a_b", "an exact name wins")
        twins2 = site(**{"/p/alpha/api/montage": (200, {"recipes": [{"name": "a b", "valid": True}, {"name": "a\x01b", "valid": True}]})})     # both read "a b" once cleaned
        with self.assertRaises(views.ViewError) as ctx:
            panel.resolve_recipe(twins2, "alpha", "a_b")
        self.assertIn("more than one", str(ctx.exception))

    def test_the_plan_board_lays_out_the_timeline_with_a_rough_time_and_the_warnings(self):
        text = panel.render("plan", FakePlanSite(self.site.answers), {"project": "alpha", "recipe": "good one"})
        for must in ("12.4 s, reels, clean", "timeline:", "1. photo", "2. end", "Thank you", "effect: riso", "warning: a warning", "rough guide"):
            self.assertIn(must, text)
        broken = panel.render("plan", self.site, {"project": "alpha", "recipe": "broken"})
        self.assertIn("does not work", broken)
        self.assertIn("✗ x", broken)
        self.assertIn("no recipe chosen", panel.render("plan", self.site, {"project": "alpha"}))

    def test_with_no_project_chosen_the_only_project_is_used_and_two_or_more_ask_you_to_pick(self):
        self.assertIn("no project chosen", panel.render("recipes", self.site, {}))
        self.assertIn("open the projects board", panel.render("exports", self.site, {}))
        only = site(**{"/api/projects": (200, {"projects": [{"slug": "alpha", "name": "A"}]})})
        self.assertEqual(rows(panel.render("recipes", only, {})), ["good_one", "broken"], "one project: no choice to make")

    def test_a_board_never_raises_an_unreachable_web_app_or_a_missing_project_is_one_plain_line_without_rows(self):
        text = panel.render("jobs", draft.Site("http://127.0.0.1:9"), {})
        self.assertIn("not reachable", text)
        self.assertIn("tries again", text)
        self.assertEqual(rows(text), [])
        gone = panel.render("project", site(), {"project": "ghost"})
        self.assertIn("no project 'ghost'", gone)
        self.assertEqual(rows(gone), [])

    def test_text_a_person_controls_cannot_pass_for_a_row_or_a_fence_on_any_board(self):
        s = site(**{"/api/projects": (200, {"projects": [{"slug": "alpha", "name": NASTY}]}),
                    "/p/alpha/api/montage": (200, {"catalog": [{"folder": NASTY, "kind": "image"}], "recipes": [{"name": NASTY, "valid": True}],
                                                    "exports": [{"name": NASTY, "duration": 1.0}], "jobs": [{"id": "1", "state": "failed", "title": NASTY, "error": NASTY}]})})
        for view in ("projects", "jobs", "project", "recipes", "exports"):
            text = panel.render(view, s, {"project": "alpha"})
            self.assertNotIn("---", text, view)
            self.assertEqual([l for l in text.splitlines() if l.lstrip().startswith(("IGNORE", "---", "==="))], [], f"{view}: the text stays on its own line")

    def test_the_choice_is_remembered_in_a_small_file_and_a_bad_name_writes_nothing(self):
        self.assertEqual(panel.load_state(), {"project": None, "recipe": None})
        self.assertEqual(panel.set_state("alpha", "good one"), {"project": "alpha", "recipe": "good one"})
        self.assertEqual(panel.load_state(), {"project": "alpha", "recipe": "good one"})
        self.assertFalse(list(self.tmp.glob("*.part")), "no half-written file is left")
        path = panel.state_path()
        for bad in ("../x", "A B", "", "a/b"):
            with self.subTest(project=bad):
                with self.assertRaises(views.ViewError):
                    panel.set_state(bad)
        self.assertEqual(panel.load_state()["project"], "alpha", "a refused name left the choice alone")
        path.write_text("{ not json")
        self.assertEqual(panel.load_state(), {"project": None, "recipe": None}, "a damaged file is no choice, not a crash")
        path.write_text(json.dumps({"project": 5, "recipe": ["x"]}))
        self.assertEqual(panel.load_state(), {"project": None, "recipe": None})


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"UMCARES_PANEL_STATE": str(self.tmp / "panel.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def run_cmd(self, view, choice=(), url="http://127.0.0.1:9"):
        out, err = io.StringIO(), io.StringIO()
        args = SimpleNamespace(view=view, choice=list(choice), url=url)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_panel(args, SimpleNamespace(env={}))
        return code, out.getvalue(), err.getvalue()

    def test_a_board_command_always_exits_0_and_says_what_is_wrong_on_stdout_and_set_exits_1_on_a_problem(self):
        code, out, err = self.run_cmd("jobs")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("not reachable", out)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UMC_WEB_URL", None)
            code, out, _ = self.run_cmd("projects", url=None)
        self.assertEqual(code, 0)
        self.assertIn("UMC_WEB_URL", out, "the board says it needs an address, it does not crash")
        for choice in ((), ("a", "b", "c")):
            code, out, err = self.run_cmd("set", choice)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("panel set <project> [recipe]", err)
        code, out, err = self.run_cmd("set", ("../x",))
        self.assertEqual((code, out), (1, ""))
        self.assertFalse(panel.state_path().exists(), "a bad name wrote nothing")

    def test_set_writes_the_choice_and_prints_it_as_json(self):
        code, out, err = self.run_cmd("set", ("alpha",))
        self.assertEqual((code, json.loads(out)), (0, {"ok": True, "project": "alpha", "recipe": None}))
        self.assertEqual(panel.load_state()["project"], "alpha")

    def test_the_views_the_command_accepts_are_the_views_that_exist(self):
        self.assertEqual(set(panel.VIEWS), set(panel._VIEWS))
        with self.assertRaises(SystemExit):
            cli.main(["panel", "nonsense"])


if __name__ == "__main__":
    unittest.main()
