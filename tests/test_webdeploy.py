import hashlib
import json
import plistlib
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from umcares import montage, prepare, projects, web, webdeploy
from umcares.transport import Result

ROOT = "/Users/me/Projects/DrMadihah"


def real_sums():
    return "".join(f"{hashlib.sha256((webdeploy.PACKAGE_DIR / n).read_bytes()).hexdigest()}  {n}\n"
                   for n in webdeploy.PACKAGE_FILES)


class FakeTransport:
    """Records what the deploy does; answers like a healthy remote unless told otherwise."""
    def __init__(self, sums=None, existing_plist="", busy_answer="", ffmpeg=""):
        self.busy_answer, self.ffmpeg = busy_answer, ffmpeg
        self.sums = sums if sums is not None else real_sums()
        self.existing_plist = existing_plist
        self.commands, self.pushed, self.scripts = [], [], []

    def ensure_dir(self, d):
        self.commands.append(f"mkdir {d}")

    def push(self, local, remote):
        self.pushed.append(remote)

    def run(self, cmd, timeout=120):
        self.commands.append(cmd)
        if "/api/busy" in cmd:
            return Result(0 if self.busy_answer else 7, self.busy_answer)
        if cmd.startswith("pgrep"):
            return Result(0 if self.ffmpeg else 1, self.ffmpeg)
        if cmd.startswith("echo $HOME"):
            return Result(0, "/Users/me\n/usr/local/bin/python3\n501\n")
        if "shasum" in cmd:
            return Result(0, self.sums)
        if cmd.startswith("cat "):
            return Result(0, self.existing_plist) if self.existing_plist else Result(1, "")
        return Result(0, "")

    def run_script(self, script, timeout=600, shell="bash"):
        self.scripts.append(script)
        return Result(0, "http=200\nstate = running pid = 1 runs = 1\n/umcares proxy http://127.0.0.1:8733\n")


class PlistText(unittest.TestCase):
    def test_is_valid_and_carries_the_settings(self):
        d = plistlib.loads(webdeploy.plist_text("/usr/bin/python3", "/app", "/home/p", "me@x.com", 8733, "/log").encode())
        self.assertEqual(d["Label"], "com.umcares.web")
        self.assertEqual(d["ProgramArguments"], ["/usr/bin/python3", "-m", "umcares.web", "--home", "/home/p", "--port", "8733"])
        self.assertEqual(d["WorkingDirectory"], "/app")
        self.assertEqual(d["EnvironmentVariables"], {"UMC_WEB_OWNER": "me@x.com"})
        self.assertTrue(d["KeepAlive"] and d["RunAtLoad"])

    def test_special_characters_cannot_break_the_xml(self):
        d = plistlib.loads(webdeploy.plist_text("/py", "/a&b", "/h<x>", "a&b@x.com", 1, "/l").encode())
        self.assertEqual(d["WorkingDirectory"], "/a&b")
        self.assertEqual(d["EnvironmentVariables"]["UMC_WEB_OWNER"], "a&b@x.com")


class Deploy(unittest.TestCase):
    def test_a_push_that_arrives_wrong_aborts_before_launchd_is_touched(self):
        t = FakeTransport(sums=real_sums().replace("a", "b", 1))
        with self.assertRaises(RuntimeError) as cm:
            webdeploy.deploy(t, ROOT, owner="me@x.com")
        self.assertIn("differ after the push", str(cm.exception))
        self.assertEqual(t.scripts, [])
        self.assertFalse(any("launchctl" in c for c in t.commands))

    def test_a_first_deploy_writes_the_plist_and_bootstraps(self):
        t = FakeTransport()
        r = webdeploy.deploy(t, ROOT, owner="me@x.com")
        self.assertTrue(r["plist_changed"])
        self.assertTrue(any(p.endswith("com.umcares.web.plist") for p in t.pushed))
        self.assertEqual(r["http"], "200")
        self.assertIn("if true ||", t.scripts[0])

    def test_an_unchanged_plist_is_left_alone_and_the_service_is_kicked(self):
        want = webdeploy.plist_text("/usr/local/bin/python3", f"{ROOT}/web/app", "/Users/me/Projects/umcares-projects",
                                    "me@x.com", 8733, f"{ROOT}/web/web.log")
        t = FakeTransport(existing_plist=want)
        r = webdeploy.deploy(t, ROOT, owner="me@x.com")
        self.assertFalse(r["plist_changed"])
        self.assertFalse(any(p.endswith(".plist") for p in t.pushed))
        self.assertIn("if false ||", t.scripts[0])
        self.assertIn("kickstart -k", t.scripts[0])

    def test_the_existing_project_folder_is_registered_not_moved(self):
        t = FakeTransport()
        r = webdeploy.deploy(t, ROOT, owner="me@x.com")
        self.assertEqual(r["registered"], "drmadihah")
        self.assertIn("register drmadihah", t.scripts[0])

    def test_a_service_that_does_not_answer_is_an_error_not_a_success(self):
        t = FakeTransport()
        t.run_script = lambda *a, **k: Result(0, "http=000\n")
        with self.assertRaises(RuntimeError) as cm:
            webdeploy.deploy(t, ROOT, owner="me@x.com")
        self.assertIn("did not come up", str(cm.exception))

    def test_it_never_publishes_to_the_tailnet_itself(self):
        t = FakeTransport()
        webdeploy.deploy(t, ROOT, owner="me@x.com")
        self.assertNotIn("serve --bg", t.scripts[0])
        self.assertFalse(any("serve --bg" in c for c in t.commands))

    def test_the_package_files_it_ships_all_exist_and_import_nothing_unshipped(self):
        for n in webdeploy.PACKAGE_FILES:
            self.assertTrue((webdeploy.PACKAGE_DIR / n).is_file(), n)
        import re
        shipped = {Path(n).stem for n in webdeploy.PACKAGE_FILES}
        for n in webdeploy.PACKAGE_FILES:
            for mod in re.findall(r"^from \.(\w*) import|^from \. import (\w+)", (webdeploy.PACKAGE_DIR / n).read_text(), re.M):
                name = mod[0] or mod[1]
                self.assertTrue(not name or name in shipped, f"{n} imports {name}, which is not shipped")


def answer(render=(0, 0), prepare_=(0, 0)):
    r, p = {"running": render[0], "queued": render[1]}, {"running": prepare_[0], "queued": prepare_[1]}
    return json.dumps({"render": r, "prepare": p, "busy": bool(sum(render) + sum(prepare_))})


class BusyGuard(unittest.TestCase):
    """A restart kills a running render and loses the queue, so a deploy asks first."""

    def refused(self, t, **kw):
        with self.assertRaises(webdeploy.BusyError) as cm:
            webdeploy.deploy(t, ROOT, owner="me@x.com", **kw)
        self.assertEqual((t.pushed, t.scripts), ([], []), "nothing was pushed and launchd was not touched")
        self.assertFalse(any("launchctl" in c or "shasum" in c for c in t.commands))
        return str(cm.exception)

    def test_a_running_render_stops_the_deploy_before_anything_is_pushed(self):
        msg = self.refused(FakeTransport(busy_answer=answer(render=(1, 2))))
        self.assertIn("1 running and 2 queued render", msg)
        self.assertIn("--force", msg)

    def test_a_running_prepare_job_stops_it_too(self):
        self.assertIn("prepare", self.refused(FakeTransport(busy_answer=answer(prepare_=(1, 0)))))

    def test_an_idle_app_deploys(self):
        t = FakeTransport(busy_answer=answer())
        self.assertEqual(webdeploy.deploy(t, ROOT, owner="me@x.com")["http"], "200")

    def test_force_deploys_anyway_and_does_not_even_ask(self):
        t = FakeTransport(busy_answer=answer(render=(1, 0)))
        self.assertEqual(webdeploy.deploy(t, ROOT, owner="me@x.com", force=True)["http"], "200")
        self.assertFalse(any("/api/busy" in c for c in t.commands))

    def test_wait_lets_a_render_finish_then_deploys(self):
        t = FakeTransport(busy_answer=answer(render=(1, 0)))
        answers = [answer(render=(1, 0)), answer(render=(0, 1)), answer()]
        real = t.run
        t.run = lambda cmd, timeout=120: (Result(0, answers.pop(0)) if "/api/busy" in cmd else real(cmd, timeout))
        self.assertEqual(webdeploy.deploy(t, ROOT, owner="me@x.com", wait=60, poll=0)["http"], "200")
        self.assertEqual(answers, [], "it asked until the remote was idle")

    def test_wait_gives_up_when_the_render_does_not_finish(self):
        self.refused(FakeTransport(busy_answer=answer(render=(1, 0))), wait=0.05, poll=0.01)

    def test_an_older_app_without_the_endpoint_is_judged_by_a_running_ffmpeg(self):
        self.assertIn("ffmpeg", self.refused(FakeTransport(busy_answer="not found", ffmpeg="4242\n")))
        t = FakeTransport(busy_answer="not found")
        self.assertEqual(webdeploy.deploy(t, ROOT, owner="me@x.com")["http"], "200", "no ffmpeg: nothing to kill")

    def test_an_app_that_is_down_does_not_block_a_deploy_that_would_fix_it(self):
        t = FakeTransport()
        self.assertEqual(webdeploy.busy(t, 8733)["source"], "unknown")
        self.assertEqual(webdeploy.deploy(t, ROOT, owner="me@x.com")["http"], "200")


class BusyEndpoint(unittest.TestCase):
    """The real app's answer, which is what the deploy reads on the remote."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.srv = web.make_server(Path(self.home), "me@x.com", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/api/busy"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def get(self):
        with urllib.request.urlopen(self.url) as r:
            return json.load(r)

    def test_it_counts_running_and_queued_work_of_both_kinds(self):
        self.assertFalse(self.get()["busy"])
        r, p = montage.RENDERER, prepare.PREPARER
        r.jobs.append({"state": "running"}); r.jobs.append({"state": "queued"}); r.jobs.append({"state": "done"})
        p.jobs.append({"state": "queued"})
        try:
            d = self.get()
        finally:
            r.jobs[:] = [j for j in r.jobs if j.get("state") not in ("running", "queued") or "root" in j]
            p.jobs[:] = [j for j in p.jobs if "root" in j]
        self.assertEqual((d["render"], d["prepare"], d["busy"]), ({"running": 1, "queued": 1}, {"running": 0, "queued": 1}, True))

    def test_the_real_answer_is_what_the_deploy_understands(self):
        class T:
            def run(_, cmd, timeout=0):
                with urllib.request.urlopen(self.url) as r:
                    return Result(0, r.read().decode())
        self.assertEqual(webdeploy.busy(T(), 0)["source"], "app")


if __name__ == "__main__":
    unittest.main()
