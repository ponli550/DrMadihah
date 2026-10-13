"""Mode C: `umcares draft` against a real local web server, with the model call faked."""
import base64
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from umcares import captions, cli, draft, montage, prepare, projects, textimg, web
from tests.test_prepare import put

FF = prepare.find_tool("ffmpeg")
NEED = unittest.skipUnless(FF and prepare.find_tool("ffprobe") and textimg.available(), "needs ffmpeg and macOS text drawing")


class Fake:
    """A stand-in for the model: returns canned answers in order and remembers what it was asked."""
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, line):
        self.asked.append(json.loads(line))
        return self.answers.pop(0)


def text_of(msg):
    return "\n".join(b["text"] for b in msg["message"]["content"] if b["type"] == "text")


@NEED
class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Path(tempfile.mkdtemp())
        for n, sz in (("land.jpg", "640x360"), ("port.jpg", "360x640")):
            subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={sz}", "-frames:v", "1", str(cls.fx / n)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fx, ignore_errors=True)

    def setUp(self):
        montage._info_cache.clear()
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=True)["path"])
        for i, n in enumerate(("land.jpg", "port.jpg")):
            put(self.root, self.fx / n, mtime=1_700_000_000_000 + i * 60_000)
        sha = next(i["sha"] for i in captions.items(self.root) if i["name"] == "land.jpg")
        captions.set_detail(self.root, sha, "The registration desk")
        self.srv = web.make_server(self.home, "me@x.com", 0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.site = draft.Site(self.base)
        self.refs = {i["name"]: i["ref"] for i in montage.catalog(self.root)}

    def tearDown(self):
        montage.RENDERER.wait_idle(10)
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def good(self, **kw):
        return json.dumps({"kind": "montage", "title": "Demo", "format": "reels", "template": "punchy", "captions": True,
                           "clips": [{"media": self.refs["port.jpg"]}, {"media": self.refs["land.jpg"], "caption": "Selamat datang"}], **kw})

    # -------------------------------------------------------------- what the model is shown
    def test_the_model_is_shown_each_picture_with_its_ref_and_caption_plus_the_brief_and_the_request(self):
        ask = Fake(self.good())
        res = draft.draft(self.site, "demo", "a 15 second reel, upbeat", ask=ask)
        msg = ask.asked[0]
        images = [b for b in msg["message"]["content"] if b["type"] == "image"]
        self.assertEqual(len(images), 2)
        self.assertTrue(all(base64.b64decode(b["source"]["data"])[:2] == b"\xff\xd8" for b in images), "real JPEG thumbnails")
        text = text_of(msg)
        for needle in (self.refs["land.jpg"], "land.jpg", "The registration desk", "a 15 second reel, upbeat", "Never invent a file", "montage"):
            self.assertIn(needle, text)
        self.assertEqual(res["images"], 2)

    def test_no_images_sends_only_names_and_captions(self):
        ask = Fake(self.good())
        res = draft.draft(self.site, "demo", "x", images=False, ask=ask)
        self.assertEqual([b for b in ask.asked[0]["message"]["content"] if b["type"] == "image"], [])
        self.assertIn("no picture shown", text_of(ask.asked[0]))
        self.assertEqual(res["images"], 0)

    def test_at_most_the_asked_number_of_pictures_are_sent(self):
        ask = Fake(self.good())
        draft.draft(self.site, "demo", "x", max_images=1, ask=ask)
        self.assertEqual(len([b for b in ask.asked[0]["message"]["content"] if b["type"] == "image"]), 1)

    # -------------------------------------------------------------- what comes back
    def test_a_valid_draft_is_saved_for_approval_and_nothing_is_rendered(self):
        res = draft.draft(self.site, "demo", "x", ask=Fake(self.good()))
        self.assertEqual(res["attempts"], 1)
        self.assertTrue(res["name"].startswith("claude-draft-"))
        state = web.montage_state(self.root)
        self.assertTrue(any(r["name"].startswith("claude-draft-") and r["valid"] for r in state["recipes"]))
        self.assertEqual((state["jobs"], state["exports"], state["active"]), ([], [], False), "a draft is never rendered")
        self.assertGreater(res["plan"]["total"], 2)

    def test_dry_run_checks_but_saves_nothing(self):
        res = draft.draft(self.site, "demo", "x", save=False, ask=Fake(self.good()))
        self.assertIsNone(res["name"])
        self.assertEqual(web.montage_state(self.root)["recipes"], [])

    def test_a_reply_with_prose_and_a_code_fence_is_still_read(self):
        res = draft.draft(self.site, "demo", "x", ask=Fake("Sure! Here you go:\n```json\n" + self.good() + "\n```\nHope it helps."))
        self.assertEqual(res["attempts"], 1)

    def test_a_bad_recipe_is_shown_its_problems_once_and_the_fixed_one_is_kept(self):
        bad = json.dumps({"kind": "montage", "title": "Demo", "template": "punchy", "clips": [{"media": "nope"}], "sparkle": True})
        ask = Fake(bad, self.good())
        res = draft.draft(self.site, "demo", "x", ask=ask)
        self.assertEqual(res["attempts"], 2)
        again = text_of(ask.asked[1])
        self.assertIn("previous answer was rejected", again)
        problems = again.split("Problems found by the checker:")[1]
        self.assertIn("unknown field", problems, "the checker's own wording is what the model is shown, not just its own answer")
        self.assertIn("sparkle", problems)

    def test_two_bad_answers_give_up_with_the_problems_and_save_nothing(self):
        bad = json.dumps({"kind": "montage", "title": "Demo", "template": "punchy", "clips": [{"media": "nope"}]})
        with self.assertRaises(draft.DraftError) as ctx:
            draft.draft(self.site, "demo", "x", ask=Fake(bad, bad))
        self.assertIn("did not pass the checker", str(ctx.exception))
        self.assertEqual(web.montage_state(self.root)["recipes"], [])

    def test_an_answer_that_is_not_json_is_corrected_once_then_refused(self):
        with self.assertRaises(draft.DraftError):
            draft.draft(self.site, "demo", "x", ask=Fake("I think you should use photos.", "Sorry, no."))
        self.assertEqual(web.montage_state(self.root)["recipes"], [])

    def test_a_recipe_naming_a_file_outside_the_project_cannot_get_through(self):
        evil = json.dumps({"kind": "montage", "title": "x", "template": "punchy", "clips": [{"media": "../../../etc/passwd"}]})
        with self.assertRaises(draft.DraftError):
            draft.draft(self.site, "demo", "x", ask=Fake(evil, evil))

    # -------------------------------------------------------------- the edges
    def test_the_usual_mistakes_are_clear_errors(self):
        with self.assertRaises(draft.DraftError) as c1:
            draft.draft(self.site, "demo", "   ", ask=Fake())
        self.assertIn("say what you want", str(c1.exception))
        with self.assertRaises(draft.DraftError) as c2:
            draft.draft(self.site, "nope", "x", ask=Fake())
        self.assertIn("no such project", str(c2.exception))
        with self.assertRaises(draft.DraftError) as c3:
            draft.Site("")
        self.assertIn("UMC_WEB_URL", str(c3.exception))
        with self.assertRaises(draft.DraftError) as c4:
            draft.draft(draft.Site("http://127.0.0.1:9"), "demo", "x", ask=Fake())
        self.assertIn("could not reach", str(c4.exception))

    def test_a_missing_claude_is_reported_before_any_slow_work(self):
        unreachable = draft.Site("http://127.0.0.1:9")                       # would say "could not reach" if it got that far
        with mock.patch.object(draft.shutil, "which", lambda n: None):
            with self.assertRaises(draft.DraftError) as ctx:
                draft.draft(unreachable, "demo", "x")
        self.assertIn("not installed", str(ctx.exception))

    def test_an_empty_project_is_refused_before_the_model_is_asked(self):
        projects.create_project(self.home, "Empty", init_git=False)
        ask = Fake()
        with self.assertRaises(draft.DraftError) as ctx:
            draft.draft(self.site, "empty", "x", ask=ask)
        self.assertIn("no pictures", str(ctx.exception))
        self.assertEqual(ask.asked, [])

    # -------------------------------------------------------------- the command line
    def test_the_command_prints_a_summary_and_returns_0_or_2(self):
        with mock.patch.object(draft, "ask_claude", lambda line, **k: self.good()):
            self.assertEqual(cli.main(["draft", "demo", "a", "short", "reel", "--url", self.base, "--dry-run"]), 0)
            self.assertEqual(cli.main(["draft", "demo", "x", "--url", "http://127.0.0.1:9"]), 2)
        self.assertEqual(web.montage_state(self.root)["recipes"], [], "--dry-run saved nothing")


class Model(unittest.TestCase):
    """ask_claude without a real model: the command it builds and how it reads the answer."""
    def runner(self, stdout, rc=0):
        seen = {}

        def run(cmd, **kw):
            seen["cmd"], seen["input"] = cmd, kw.get("input")
            return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")
        return run, seen

    def result(self, text, err=False):
        return json.dumps({"type": "system"}) + "\n" + json.dumps({"type": "result", "result": text, "is_error": err}) + "\n"

    def test_the_model_runs_once_with_no_tools_and_a_spending_cap(self):
        run, seen = self.runner(self.result('{"a": 1}'))
        out = draft.ask_claude("LINE", budget=0.25, model="opus", claude="/bin/claude", runner=run)
        self.assertEqual(out, '{"a": 1}')
        cmd = seen["cmd"]
        self.assertEqual(cmd[0], "/bin/claude")
        self.assertIn("-p", cmd)
        i = cmd.index("--tools")
        self.assertEqual(cmd[i + 1], "", "every tool is switched off: the model can only answer")
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.25")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(seen["input"], "LINE")

    def test_errors_timeouts_and_a_missing_claude_are_clear(self):
        run, _ = self.runner(self.result("budget exceeded", err=True))
        with self.assertRaises(draft.DraftError) as c1:
            draft.ask_claude("x", claude="/bin/claude", runner=run)
        self.assertIn("budget exceeded", str(c1.exception))
        run, _ = self.runner("not json at all\n")
        with self.assertRaises(draft.DraftError):
            draft.ask_claude("x", claude="/bin/claude", runner=run)

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        with self.assertRaises(draft.DraftError) as c2:
            draft.ask_claude("x", claude="/bin/claude", runner=slow, timeout=1)
        self.assertIn("did not answer", str(c2.exception))
        with mock.patch.object(draft.shutil, "which", lambda n: None):
            with self.assertRaises(draft.DraftError) as c3:
                draft.ask_claude("x")
        self.assertIn("not installed", str(c3.exception))

    def test_reading_a_recipe_out_of_a_reply(self):
        self.assertEqual(draft.extract_recipe(' {"a": 1} '), {"a": 1})
        self.assertEqual(draft.extract_recipe('Here: {"a": {"b": 2}} done'), {"a": {"b": 2}})
        for bad in ("no braces", "{broken", "[1, 2]", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(draft.DraftError):
                    draft.extract_recipe(bad)


if __name__ == "__main__":
    unittest.main()
