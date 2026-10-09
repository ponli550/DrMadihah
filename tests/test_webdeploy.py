import hashlib
import plistlib
import unittest
from pathlib import Path

from umcares import webdeploy
from umcares.transport import Result

ROOT = "/Users/me/Projects/DrMadihah"


def real_sums():
    return "".join(f"{hashlib.sha256((webdeploy.PACKAGE_DIR / n).read_bytes()).hexdigest()}  {n}\n"
                   for n in webdeploy.PACKAGE_FILES)


class FakeTransport:
    """Records what the deploy does; answers like a healthy remote unless told otherwise."""
    def __init__(self, sums=None, existing_plist=""):
        self.sums = sums if sums is not None else real_sums()
        self.existing_plist = existing_plist
        self.commands, self.pushed, self.scripts = [], [], []

    def ensure_dir(self, d):
        self.commands.append(f"mkdir {d}")

    def push(self, local, remote):
        self.pushed.append(remote)

    def run(self, cmd, timeout=120):
        self.commands.append(cmd)
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


if __name__ == "__main__":
    unittest.main()
