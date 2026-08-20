"""SSH connection multiplexing: the argv, and the reasons to go without it.

Every command this pipeline runs was a fresh TCP connect plus key exchange —
measured at ~310ms each over the tailnet, against ~77ms once a master is
reused. These tests pin the argv (so scp cannot silently stop sharing the
master) and the fallbacks, since multiplexing is an optimisation and every
reason it cannot run is a reason to carry on without it.

No network: what is under test is how the command is *assembled*.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from umcares import transport
from umcares.transport import SSHTransport


def opts(argv: list) -> dict:
    """The -o KEY=VALUE pairs in an argv, as a dict."""
    out = {}
    for i, a in enumerate(argv):
        if a == "-o" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, v = argv[i + 1].split("=", 1)
            out[k] = v
    return out


class Argv(unittest.TestCase):
    def setUp(self):
        # A socket path has a hard byte limit, so the fixture has to be
        # short — /var/folders/... alone is 60 characters on macOS.
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.patch = mock.patch.object(transport, "control_dir",
                                       lambda: Path(self.tmp.name) / "cm")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_ssh_declares_a_master(self):
        o = opts(SSHTransport("host")._base())
        self.assertEqual(o["ControlMaster"], "auto")
        self.assertEqual(o["ControlPersist"], "10m")
        self.assertTrue(o["ControlPath"].endswith("cm-%C"))

    def test_scp_shares_the_same_master(self):
        """scp opening its own connection wastes half the handshakes in a render."""
        t = SSHTransport("host")
        self.assertEqual(opts(t._scp())["ControlPath"],
                         opts(t._base())["ControlPath"])

    def test_scp_keeps_the_key_and_host_policy(self):
        argv = SSHTransport("host", key="/k")._scp()
        self.assertEqual(argv[:1], ["scp"])
        self.assertIn("-i", argv)
        self.assertEqual(opts(argv)["StrictHostKeyChecking"], "accept-new")

    def test_persist_is_configurable(self):
        self.assertEqual(opts(SSHTransport("host", persist="30s")._base())
                         ["ControlPersist"], "30s")

    def test_password_auth_still_multiplexes(self):
        argv = SSHTransport("host", password="p")._base()
        self.assertEqual(argv[0], "sshpass")
        self.assertIn("ControlPath", opts(argv))

    def test_batchmode_survives(self):
        """A prompt is a hang for an unattended run; mux must not displace it."""
        self.assertIn("BatchMode=yes", SSHTransport("host")._base())

    def test_the_socket_dir_is_private(self):
        SSHTransport("host")
        self.assertEqual((Path(self.tmp.name) / "cm").stat().st_mode & 0o777,
                         0o700)


class GoWithout(unittest.TestCase):
    def test_disabled_means_no_control_options(self):
        argv = SSHTransport("host", mux=False)._base()
        self.assertNotIn("ControlPath", opts(argv))
        self.assertNotIn("ControlMaster", opts(argv))

    def test_disabled_scp_is_still_a_valid_scp(self):
        argv = SSHTransport("host", mux=False)._scp()
        self.assertEqual(argv[:2], ["scp", "-q"])
        self.assertNotIn("ControlPath", opts(argv))

    def test_an_unmakeable_directory_is_not_fatal(self):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            self.assertEqual(SSHTransport("host")._socket, "")

    def test_a_path_too_long_for_a_unix_socket_disables_it(self):
        """ssh reports this as "too long for Unix domain socket" and fails; %C
        expands to 40 hex characters, so the budget is checked up front."""
        deep = Path("/" + "d" * (transport.CONTROL_MAX - transport.CONTROL_HASH))
        with mock.patch.object(transport, "control_dir", lambda: deep), \
             mock.patch.object(Path, "mkdir"):
            self.assertEqual(SSHTransport("host")._socket, "")

    def test_a_short_path_is_kept(self):
        with mock.patch.object(transport, "control_dir", lambda: Path("/tmp/c")), \
             mock.patch.object(Path, "mkdir"):
            self.assertTrue(SSHTransport("host")._socket)


class WedgedMaster(unittest.TestCase):
    """A stale socket ssh handles itself; a live-but-wedged master it does not."""

    def setUp(self):
        # A socket path has a hard byte limit, so the fixture has to be
        # short — /var/folders/... alone is 60 characters on macOS.
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.dir = Path(self.tmp.name) / "cm"
        self.patch = mock.patch.object(transport, "control_dir", lambda: self.dir)
        self.patch.start()
        self.t = SSHTransport("host")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_master_errors_are_recognised(self):
        self.assertTrue(self.t._mux_wedged(
            "mux_client_request_session: read from master failed: Broken pipe"))

    def test_ordinary_failures_are_not(self):
        """Retrying a command that genuinely failed would run it twice."""
        self.assertFalse(self.t._mux_wedged("ffmpeg: No such file or directory"))
        self.assertFalse(self.t._mux_wedged(""))

    def test_nothing_is_wedged_when_nothing_is_multiplexed(self):
        self.assertFalse(SSHTransport("host", mux=False)
                         ._mux_wedged("mux_client: broken"))

    def test_dropping_the_master_removes_the_socket(self):
        sock = self.dir / "cm-abc"
        sock.write_text("")
        with mock.patch("subprocess.run") as run:
            self.assertTrue(self.t._drop_master())
        self.assertFalse(sock.exists())
        self.assertIn("-O", run.call_args[0][0])

    def test_a_wedged_command_is_retried_once_on_a_fresh_connection(self):
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            if len(calls) == 1:
                return mock.Mock(returncode=255, stdout="",
                                 stderr="mux_client_hello_exchange: write packet")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        self.t._cached_path = "/usr/bin:/bin"
        with mock.patch("subprocess.run", side_effect=fake):
            res = self.t.run("true")
        self.assertEqual(res.rc, 0)
        self.assertEqual(res.stdout, "ok")
        self.assertEqual(len(calls), 3)          # fail, `ssh -O exit`, retry

    def test_a_real_failure_is_not_retried(self):
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=1, stdout="", stderr="no such file")

        self.t._cached_path = "/usr/bin:/bin"
        with mock.patch("subprocess.run", side_effect=fake):
            res = self.t.run("cat missing")
        self.assertEqual(res.rc, 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
