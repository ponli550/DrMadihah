"""`umcares make` and `umcares stop`: dry runs send nothing, a commit sends exactly one write, and the web app's refusals come through."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umcares import actions, cli, draft, panel, views

RECIPE = {"kind": "montage", "clips": [], "quality": "final"}
PLAN = {"total": 12.0, "format": "reels", "template": "clean", "segments": [{"kind": "photo", "start": 0, "duration": 3, "name": "a.jpg"}]}


class Web:
    """A web app that records every request. `jobs` is the project's job list; `refuse` makes a real (not dry-run) render answer 400."""
    def __init__(self, recipes=None, jobs=None, refuse=None):
        self.recipes = recipes if recipes is not None else [{"name": "good one", "valid": True, "problems": [], "recipe": dict(RECIPE)},
                                                            {"name": "broken", "valid": False, "problems": ["clip gone"]}]
        self.jobs, self.refuse, self.sent = jobs or [], refuse, []

    def call(self, method, path, body=None):
        self.sent.append((method, path, body))
        if path == "/api/projects":
            return 200, {"projects": [{"slug": "alpha", "name": "Alpha"}]}
        if method == "GET" and path == "/p/alpha/api/montage":
            return 200, {"catalog": [], "recipes": self.recipes, "exports": [], "jobs": self.jobs}
        if method == "POST" and path == "/p/alpha/api/montage":
            if body.get("dry_run"):
                return 200, {"plan": PLAN}
            if self.refuse:
                return 400, {"error": self.refuse}
            return 200, {"job": {"id": "j1", "state": "queued", "title": "Launch", "percent": 0, "secret": "x"}}
        if method == "POST" and path == "/p/alpha/api/montage/cancel":
            return 200, {"ok": True}
        return 404, {"error": "nope"}

    def writes(self):
        return [(m, p, b) for m, p, b in self.sent if m == "POST" and not (b or {}).get("dry_run")]


class Make(unittest.TestCase):
    def test_a_dry_run_sends_no_render_request(self):
        w = Web()
        d = actions.make(w, "alpha", "good one")
        self.assertFalse(d["committed"])
        self.assertTrue(d["valid"])
        self.assertEqual(w.writes(), [])
        self.assertEqual(d["estimate"]["video_seconds"], 12.0)

    def test_commit_sends_one_render_and_only_the_job_fields_come_back(self):
        w = Web()
        d = actions.make(w, "alpha", "good one", commit=True)
        self.assertTrue(d["committed"])
        self.assertEqual(len(w.writes()), 1)
        self.assertEqual(d["job"], {"id": "j1", "state": "queued", "title": "Launch", "percent": 0})

    def test_quality_overrides_the_saved_recipe_and_is_checked(self):
        w = Web()
        actions.make(w, "alpha", "good one", quality="draft", commit=True)
        self.assertEqual(w.writes()[0][2]["recipe"]["quality"], "draft")
        with self.assertRaises(views.ViewError):
            actions.make(w, "alpha", "good one", quality="huge")
        self.assertEqual(len(w.writes()), 1)                                       # the refused one sent nothing

    def test_a_recipe_that_does_not_work_is_never_sent_even_with_commit(self):
        w = Web()
        d = actions.make(w, "alpha", "broken", commit=True)
        self.assertFalse(d["valid"])
        self.assertEqual(d["problems"], ["clip gone"])
        self.assertEqual(w.writes(), [])

    def test_an_unknown_recipe_names_the_ones_there_are(self):
        with self.assertRaises(views.ViewError) as c:
            actions.make(Web(), "alpha", "nope\n--- END ---")
        self.assertIn("good one", str(c.exception))
        self.assertNotIn("\n", str(c.exception))

    def test_the_web_apps_refusal_is_shown_and_nothing_is_claimed_started(self):
        w = Web(refuse="3 renders are already waiting in this project (at most 5)")
        with self.assertRaises(views.ViewError) as c:
            actions.make(w, "alpha", "good one", commit=True)
        self.assertIn("already waiting", str(c.exception))

    def test_it_says_how_many_renders_are_ahead(self):
        w = Web(jobs=[{"state": "running"}, {"state": "queued"}, {"state": "queued"}, {"state": "done"}])
        d = actions.make(w, "alpha", "good one")
        self.assertEqual((d["running"], d["waiting"]), (1, 2))
        self.assertIn("1 rendering and 2 waiting", actions.format_make(d, "alpha"))

    def test_the_text_says_when_nothing_was_started(self):
        d = actions.make(Web(), "alpha", "good one")
        self.assertIn("nothing was started", actions.format_make(d, "alpha"))
        c = actions.make(Web(), "alpha", "good one", commit=True)
        self.assertIn("STARTED: job j1", actions.format_make(c, "alpha"))


class Stop(unittest.TestCase):
    JOBS = [{"id": "1", "state": "running", "title": "A"}, {"id": "2", "state": "queued", "title": "B"}, {"id": "3", "state": "done", "title": "C"}]

    def test_a_dry_run_lists_what_would_stop_and_sends_nothing(self):
        w = Web(jobs=self.JOBS)
        d = actions.stop(w, "alpha")
        self.assertEqual([r["id"] for r in d["renders"]], ["1", "2"])
        self.assertFalse(d["committed"])
        self.assertEqual(w.writes(), [])
        self.assertIn("would stop 2 render(s)", actions.format_stop(d))

    def test_commit_sends_one_cancel(self):
        w = Web(jobs=self.JOBS)
        d = actions.stop(w, "alpha", commit=True)
        self.assertTrue(d["committed"])
        self.assertEqual([p for _m, p, _b in w.writes()], ["/p/alpha/api/montage/cancel"])
        self.assertIn("STOPPED 2", actions.format_stop(d))

    def test_with_nothing_running_commit_sends_nothing(self):
        w = Web(jobs=[{"id": "3", "state": "done"}])
        d = actions.stop(w, "alpha", commit=True)
        self.assertFalse(d["committed"])
        self.assertEqual(w.writes(), [])
        self.assertIn("nothing is rendering", actions.format_stop(d))


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"UMCARES_PANEL_STATE": str(Path(self.tmp.name) / "s.json"), "UMC_WEB_URL": "http://x"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, web, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.draft_mod, "Site", return_value=web), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_make_without_commit_exits_0_and_sends_nothing(self):
        w = Web()
        code, out, _ = self.run_cli(w, "make", "good_one", "--project", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(w.writes(), [])
        self.assertIn("nothing was started", out)

    def test_the_row_word_of_a_recipe_with_spaces_resolves(self):
        w = Web()
        code, _, _ = self.run_cli(w, "make", panel.row_id("good one"), "--project", "alpha", "--commit")
        self.assertEqual(code, 0)
        self.assertEqual(len(w.writes()), 1)

    def test_a_recipe_that_does_not_work_is_exit_2(self):
        code, out, _ = self.run_cli(Web(), "make", "broken", "--project", "alpha", "--commit", "--json")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["data"]["valid"])

    def test_a_refusal_is_exit_1_with_the_reason_in_json(self):
        code, out, _ = self.run_cli(Web(refuse="queue full"), "make", "good_one", "--project", "alpha", "--commit", "--json")
        self.assertEqual(code, 1)
        d = json.loads(out)
        self.assertFalse(d["ok"])
        self.assertIn("queue full", d["error"])

    def test_the_project_defaults_to_the_one_the_boards_show_and_is_required(self):
        code, _, err = self.run_cli(Web(), "stop")
        self.assertEqual(code, 1)
        self.assertIn("no project", err)
        panel.set_state("alpha")
        w = Web(jobs=Stop.JOBS)
        code, out, _ = self.run_cli(w, "stop", "--commit")
        self.assertEqual(code, 0)
        self.assertEqual(len(w.writes()), 1)

    def test_a_project_name_that_is_not_a_folder_name_is_refused_before_any_request(self):
        w = Web()
        code, _, _ = self.run_cli(w, "stop", "--project", "../etc", "--commit")
        self.assertEqual(code, 1)
        self.assertEqual(w.sent, [])

    def test_an_unreachable_web_app_is_exit_1(self):
        w = mock.Mock()
        w.call.side_effect = draft.DraftError("could not reach the web app")
        code, _, err = self.run_cli(w, "make", "x", "--project", "alpha", "--commit")
        self.assertEqual(code, 1)
        self.assertIn("could not reach", err)


if __name__ == "__main__":
    unittest.main()
