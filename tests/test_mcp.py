"""`umcares mcp`: the MCP server, against a real local web server and over real stdio."""
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from umcares import captions, draft, mcpserver, montage, prepare, projects, textimg, web
from tests.test_prepare import put

FF = prepare.find_tool("ffmpeg")
NEED = unittest.skipUnless(FF and prepare.find_tool("ffprobe") and textimg.available(), "needs ffmpeg and macOS text drawing")
ROOT = Path(__file__).resolve().parent.parent
READ_ONLY = {"list_projects", "get_project", "get_recipe_brief", "get_thumbnails", "plan_recipe", "get_jobs", "judge_videos"}


class Client:
    """Talks to a Server in process, one JSON-RPC message at a time."""
    def __init__(self, server):
        self.server, self.n = server, 0

    def request(self, method, params=None):
        self.n += 1
        return self.server.handle({"jsonrpc": "2.0", "id": self.n, "method": method, **({"params": params} if params is not None else {})})

    def tool(self, tool_name, /, **args):
        r = self.request("tools/call", {"name": tool_name, "arguments": args})["result"]
        text = "\n".join(b["text"] for b in r["content"] if b["type"] == "text")
        return r["isError"], text, r["content"]


class Protocol(unittest.TestCase):
    """The JSON-RPC layer needs no web app: a site that must never be called proves a request was refused before any was made."""
    class NoSite:
        def call(self, *a, **k):
            raise AssertionError(f"the web app was called: {a}")

    def setUp(self):
        self.c = Client(mcpserver.Server(self.NoSite()))

    def test_initialize_echoes_a_protocol_it_knows_and_offers_the_newest_otherwise_and_describes_the_server(self):
        for asked, got in (("2024-11-05", "2024-11-05"), ("2025-03-26", "2025-03-26"), ("1999-01-01", mcpserver.PROTOCOLS[0])):
            with self.subTest(asked=asked):
                r = self.c.request("initialize", {"protocolVersion": asked, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})["result"]
                self.assertEqual(r["protocolVersion"], got)
        self.assertEqual(r["serverInfo"]["name"], "umcares")
        self.assertEqual(r["capabilities"], {"tools": {"listChanged": False}})
        self.assertIn("ask the person before render_recipe", r["instructions"])

    def test_a_notification_is_never_answered_and_ping_is(self):
        self.assertIsNone(self.c.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.c.request("ping")["result"], {})

    def test_bad_input_gets_the_json_rpc_error_codes_and_never_kills_the_server(self):
        self.assertEqual(self.c.request("nope/nothing")["error"]["code"], -32601)
        self.assertEqual(self.c.server.handle({"id": 1, "method": "ping"})["error"]["code"], -32600, "no jsonrpc field")
        self.assertEqual(self.c.server.handle(["not", "an", "object"])["error"]["code"], -32600)
        self.assertEqual(self.c.request("tools/call", {"name": "ghost", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.c.request("tools/call", {"name": "get_project", "arguments": ["x"]})["error"]["code"], -32602)
        out = io.StringIO()
        mcpserver.Server(self.NoSite()).serve(io.StringIO('{not json\n\n[{"jsonrpc":"2.0","id":7,"method":"ping"},{"jsonrpc":"2.0","method":"notifications/x"}]\n{"jsonrpc":"2.0","id":8,"method":"ping"}\n'), out)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700, "a line that is not JSON is a parse error")
        self.assertEqual(lines[1], [{"jsonrpc": "2.0", "id": 7, "result": {}}], "a batch is answered as a batch, without the notification in it")
        self.assertEqual(lines[2]["id"], 8, "and the server carried on afterwards")

    def test_every_tool_describes_itself_and_only_reading_ones_claim_to_be_read_only(self):
        tools = {t["name"]: t for t in self.c.request("tools/list")["result"]["tools"]}
        self.assertEqual(len(tools), 10)
        for name, t in tools.items():
            with self.subTest(tool=name):
                self.assertGreater(len(t["description"]), 30)
                s = t["inputSchema"]
                self.assertEqual((s["type"], s["additionalProperties"]), ("object", False))
                self.assertTrue(set(s["required"]) <= set(s["properties"]))
                self.assertEqual(t["annotations"]["readOnlyHint"], name in READ_ONLY)
                self.assertIs(t["annotations"]["openWorldHint"], False)
        self.assertIs(tools["cancel_render"]["annotations"]["destructiveHint"], True)
        self.assertFalse([n for n in tools if "delete" in n or "remove" in n], "nothing that deletes a person's videos is offered")
        self.assertIn("render_recipe", tools)
        self.assertIn("ask the person first", tools["render_recipe"]["description"])
        self.assertIn("agreed", tools["get_thumbnails"]["description"], "photos of people are only sent with consent")

    def test_a_project_name_that_is_not_a_plain_folder_name_is_refused_before_any_request(self):
        for bad in ("../etc", "a/b", "A B", "", None, 7, "x" * 70, "-x"):
            with self.subTest(project=bad):
                for tool in ("get_project", "get_recipe_brief", "get_jobs", "cancel_render"):
                    isError, text, _ = self.c.tool(tool, project=bad)
                    self.assertTrue(isError)
                    self.assertIn("project:", text)

    def test_arguments_are_checked_in_words_the_model_can_act_on(self):
        for tool, args, says in (("plan_recipe", {"project": "demo"}, "recipe:"), ("plan_recipe", {"project": "demo", "recipe": "x"}, "recipe:"),
                                 ("save_recipe", {"project": "demo", "recipe": {}}, "name:"), ("render_recipe", {"project": "demo", "recipe": {}, "quality": "ultra"}, "quality:"),
                                 ("judge_videos", {"project": "demo", "names": []}, "names:"), ("judge_videos", {"project": "demo", "names": [3]}, "names:"),
                                 ("get_thumbnails", {"project": "demo"}, "folder:"), ("get_thumbnails", {"project": "demo", "folder": "p", "limit": "many"}, "limit:")):
            with self.subTest(tool=tool, args=args):
                isError, text, _ = self.c.tool(tool, **args)
                self.assertTrue(isError)
                self.assertIn(says, text)

    def test_a_bug_inside_a_tool_is_reported_as_a_tool_error_and_the_server_carries_on(self):
        def boom(site, args):
            raise RuntimeError("something unexpected")
        c = Client(mcpserver.Server(self.NoSite(), extra={"boom": (boom, "a tool that fails", mcpserver._schema({}), {"readOnlyHint": True})}))
        isError, text, _ = c.tool("boom")
        self.assertTrue(isError)
        self.assertIn("failed unexpectedly", text)
        self.assertIn("something unexpected", text)
        self.assertEqual(c.request("ping")["result"], {})

    def test_an_unreachable_web_app_is_a_tool_error_not_a_crash(self):
        c = Client(mcpserver.Server(draft.Site("http://127.0.0.1:9")))
        isError, text, _ = c.tool("list_projects")
        self.assertTrue(isError)
        self.assertIn("could not reach the web app", text)
        self.assertEqual(c.request("ping")["result"], {}, "and the server is still there")


@NEED
class Tools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Path(tempfile.mkdtemp())
        for n, sz in (("land.jpg", "640x360"), ("port.jpg", "360x640")):
            subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={sz}", "-frames:v", "1", str(cls.fx / n)], check=True)

    def setUp(self):
        montage._info_cache.clear()
        self.home = Path(tempfile.mkdtemp())
        self.root = Path(projects.create_project(self.home, "Demo", init_git=True)["path"])
        for i, n in enumerate(("land.jpg", "port.jpg")):
            put(self.root, self.fx / n, mtime=1_700_000_000_000 + i * 60_000)
        sha = next(i["sha"] for i in captions.items(self.root) if i["name"] == "land.jpg")
        captions.set_detail(self.root, sha, "The registration desk")
        self.srv = web.make_server(self.home, "me@x.com", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.c = Client(mcpserver.Server(draft.Site(self.base)))
        self.refs = {i["name"]: i["ref"] for i in montage.catalog(self.root)}

    def tearDown(self):
        montage.RENDERER.cancel(self.root)
        montage.RENDERER.wait_idle(60)
        self.srv.shutdown()
        self.srv.server_close()

    def good(self, **kw):
        return {"kind": "montage", "title": "Demo", "format": "reels", "template": "punchy", "captions": True,
                "clips": [{"media": self.refs["port.jpg"]}, {"media": self.refs["land.jpg"], "caption": "Selamat datang"}], **kw}

    def test_projects_and_what_a_project_holds_are_listed_without_any_path_on_the_machine(self):
        isError, text, _ = self.c.tool("list_projects")
        self.assertFalse(isError)
        self.assertIn("demo", text)
        isError, text, _ = self.c.tool("get_project", project="demo")
        self.assertFalse(isError)
        d = json.loads(text)
        self.assertEqual({i["name"] for i in d["catalog"]}, {"land.jpg", "port.jpg"})
        self.assertEqual(next(i for i in d["catalog"] if i["name"] == "land.jpg")["caption"], "The registration desk")
        self.assertEqual(d["catalog"][0]["ref"], self.refs[d["catalog"][0]["name"]], "the refs a recipe needs")
        self.assertIn("punchy", d["looks"])
        for out in (text, self.c.tool("list_projects")[1]):
            self.assertNotIn(str(self.home), out)
            self.assertNotIn("/private/", out)
        self.assertTrue(self.c.tool("get_project", project="nothere")[0], "a project that is not there is a tool error")

    def test_the_brief_is_the_whole_recipe_format(self):
        isError, text, _ = self.c.tool("get_recipe_brief", project="demo")
        self.assertFalse(isError)
        for must in ("## Fields", "A look of your own", "background", "`join`"):
            self.assertIn(must, text)

    def test_a_good_recipe_plans_and_a_bad_one_comes_back_as_problems_to_fix(self):
        isError, text, _ = self.c.tool("plan_recipe", project="demo", recipe=self.good())
        self.assertFalse(isError)
        plan = json.loads(text)
        self.assertEqual(len(plan["segments"]), 2)
        self.assertGreater(plan["total"], 2)
        isError, text, _ = self.c.tool("plan_recipe", project="demo", recipe=self.good(template="no-such-look", effect="sparkle"))
        self.assertTrue(isError)
        self.assertIn("problems", text)
        self.assertIn("template", text)
        self.assertIn("effect", text)
        self.assertEqual(montage.RENDERER.snapshot(self.root), [], "planning never renders")

    def test_saving_checks_first_stores_under_the_name_and_a_bad_recipe_stores_nothing(self):
        isError, text, _ = self.c.tool("save_recipe", project="demo", name="from-llm", recipe=self.good())
        self.assertFalse(isError, text)
        names = [r["name"] for r in web.montage_state(self.root)["recipes"]]
        self.assertEqual(len(names), 1)
        self.assertIn("from-llm", names[0])
        isError, text, _ = self.c.tool("save_recipe", project="demo", name="broken", recipe=self.good(template="no-such-look"))
        self.assertTrue(isError)
        self.assertIn("not saved", text)
        self.assertEqual(len(web.montage_state(self.root)["recipes"]), 1, "nothing was stored for the bad one")
        self.assertEqual(montage.RENDERER.snapshot(self.root), [], "saving never renders")

    def test_render_starts_a_job_with_the_chosen_quality_and_get_jobs_and_judge_follow_it_to_a_checked_video(self):
        isError, text, _ = self.c.tool("render_recipe", project="demo", recipe=self.good(), quality="draft")
        self.assertFalse(isError, text)
        job = json.loads(text)
        self.assertEqual(job["quality"], "draft")
        montage.RENDERER.wait_idle(120)
        isError, text, _ = self.c.tool("get_jobs", project="demo")
        jobs = json.loads(text)["jobs"]
        self.assertEqual([j["state"] for j in jobs], ["done"], jobs)
        out = jobs[0]["output"]
        self.assertTrue(out.endswith(".mp4"))
        isError, text, _ = self.c.tool("judge_videos", project="demo", names=[out])
        self.assertFalse(isError, text)
        self.assertIn(out, text)
        self.assertEqual([e["name"] for e in json.loads(self.c.tool("get_project", project="demo")[1])["exports"]], [out])

    def test_a_render_that_cannot_start_says_why(self):
        isError, text, _ = self.c.tool("render_recipe", project="demo", recipe=self.good(template="no-such-look"))
        self.assertTrue(isError)
        self.assertIn("not rendered", text)
        self.assertIn("template", text)

    def test_cancel_stops_a_queued_render(self):
        with prepare.HEAVY:                                              # keeps the first job waiting
            self.c.tool("render_recipe", project="demo", recipe=self.good())
            isError, text, _ = self.c.tool("cancel_render", project="demo")
            self.assertFalse(isError, text)
        montage.RENDERER.wait_idle(60)
        self.assertNotEqual([j["state"] for j in montage.RENDERER.snapshot(self.root)], ["done"])

    def test_thumbnails_come_back_as_labelled_images_limited_and_filtered(self):
        isError, text, content = self.c.tool("get_thumbnails", project="demo", folder="photos")
        if not any(b["type"] == "image" for b in content):                 # they are made on demand: ask once more, as the page does
            isError, text, content = self.c.tool("get_thumbnails", project="demo", folder="photos")
        images = [b for b in content if b["type"] == "image"]
        self.assertFalse(isError, text)
        self.assertEqual(len(images), 2)
        for b in images:
            self.assertEqual(b["mimeType"], "image/jpeg")
            self.assertTrue(base64.b64decode(b["data"], validate=True).startswith(b"\xff\xd8"), "a real JPEG")
        self.assertEqual([b["text"] for b in content if b["type"] == "text"], ["photos/land.jpg", "photos/port.jpg"], "each picture is named before it")
        _, _, only = self.c.tool("get_thumbnails", project="demo", folder="photos", names=["port.jpg"])
        self.assertEqual([b["text"] for b in only if b["type"] == "text"], ["photos/port.jpg"])
        _, _, one = self.c.tool("get_thumbnails", project="demo", folder="photos", limit=1)
        self.assertEqual(len([b for b in one if b["type"] == "image"]), 1)


class Stdio(unittest.TestCase):
    """The real thing: `python -m umcares mcp` as a client would start it."""
    def run_mcp(self, stdin: str, env_url):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        env.pop("UMC_WEB_URL", None)
        if env_url:
            env["UMC_WEB_URL"] = env_url
        return subprocess.run([sys.executable, "-m", "umcares", "mcp"], input=stdin, capture_output=True, text=True, timeout=60, cwd=ROOT, env=env)

    def test_stdout_carries_only_json_rpc_and_the_banner_goes_to_stderr(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        r = self.run_mcp("\n".join(json.dumps(m) for m in msgs) + "\n", "http://127.0.0.1:9")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual([x["id"] for x in lines], [1, 2], "two answers, none for the notification, nothing else on stdout")
        self.assertEqual(len(lines[1]["result"]["tools"]), 10)
        self.assertIn("serving tools for http://127.0.0.1:9", r.stderr)

    def test_without_a_web_app_address_it_says_so_on_stderr_and_prints_nothing_to_stdout(self):
        r = self.run_mcp("", None)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertIn("UMC_WEB_URL", r.stderr)


if __name__ == "__main__":
    unittest.main()
