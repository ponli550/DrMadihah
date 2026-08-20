"""The same operations against a real tmux pane, to keep the fake honest.

`test_tmux_transport.py` fakes the pane at the `tmux` argv boundary, which
cannot test the one thing tmux itself does: wrap lines at the pane width and
then rejoin them for `capture-pane -J`. That is the mechanism the whole base64
protocol exists for, so it gets checked against the real thing — in a pane 60
columns wide, where every payload wraps many times over.

A fresh pane must be woken before it is used, and this cost an hour to learn.
`tmux new-session` returns before the shell has finished starting, and a long
command line typed into a not-yet-ready zsh is mangled: no markers are ever
printed, the call waits out its whole timeout, and the pane is left mid-command
so every later command times out too. Measured on a 120-column pane, a
2000-character command as the first thing a fresh pane sees returned 0 bytes
after 25s; the same command after one short answered command took 0.9s.

Production does not hit this because `TmuxTransport.probe()` runs
`echo __UMC_OK__; hostname` and requires an answer before handing the transport
over — that check doubles as the wake-up. Constructing `TmuxTransport(pane)`
directly, as this file does, skips it, so `_wake` does it explicitly.

Opt-in, because it spawns a tmux session and takes seconds rather than
milliseconds:

    UMC_TMUX_LIVE=1 python3 -m unittest tests.test_tmux_live -v

The session is local: this exercises the transport, not the remote.
"""
import os
import shutil
import subprocess
import time
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from umcares.transport import TmuxTransport

LIVE = os.environ.get("UMC_TMUX_LIVE") and shutil.which("tmux")


@unittest.skipUnless(LIVE, "set UMC_TMUX_LIVE=1 (and install tmux) to run")
class LivePane(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = f"umc_test_{uuid.uuid4().hex[:8]}"
        # 60 columns: narrow enough that every base64 payload wraps repeatedly,
        # which is the case -J has to put back together.
        subprocess.run(["tmux", "new-session", "-d", "-s", cls.session,
                        "-x", "60", "-y", "20"], check=True)
        p = subprocess.run(["tmux", "list-panes", "-t", cls.session,
                            "-F", "#{pane_id}"], capture_output=True, text=True)
        cls.pane = p.stdout.strip().splitlines()[0]
        cls.t = TmuxTransport(cls.pane)
        cls._wake(cls.t)

    @staticmethod
    def _wake(t, deadline: float = 30.0):
        """Block until the pane answers, the way `probe()` does.

        Without this the first real command lands in a shell that is still
        starting, and everything after it inherits a pane mid-command.
        """
        end = time.time() + deadline
        while time.time() < end:
            if t.run("echo __READY__", timeout=6).stdout.strip() == "__READY__":
                return
        raise unittest.SkipTest("pane never became ready")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "kill-session", "-t", cls.session],
                       capture_output=True)

    def setUp(self):
        # A test that times out leaves the pane mid-command. Re-waking here
        # keeps one failure from being reported as eight.
        self._wake(self.t)
        self.tmp = TemporaryDirectory(dir="/tmp")

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.tmp.name, name)

    # -- commands ----------------------------------------------------------
    def test_output_round_trips(self):
        self.assertEqual(self.t.run("echo hello").stdout.strip(), "hello")

    def test_exit_code_survives(self):
        self.assertEqual(self.t.run("( exit 42 )").rc, 42)

    def test_stderr_stays_separate(self):
        r = self.t.run("echo out; echo err >&2")
        self.assertEqual(r.stdout.strip(), "out")
        self.assertEqual(r.stderr.strip(), "err")

    def test_output_far_wider_than_the_pane(self):
        """60 columns, 20 rows of scrollback: this wraps well over 100 times."""
        payload = "".join(str(i % 10) for i in range(8000))
        self.assertEqual(self.t.run(f"printf '%s' {payload!r}").stdout, payload)

    def test_utf8_round_trips_through_a_terminal(self):
        text = "Amanah di Dunia Digital — 89.3% ✓"
        self.assertEqual(self.t.run(f"printf '%s' {text!r}").stdout, text)

    def test_ansi_in_the_output_is_preserved(self):
        r = self.t.run(r"printf '\033[31mred\033[0m'")
        self.assertEqual(r.stdout, "\x1b[31mred\x1b[0m")

    def test_the_pane_reads_as_idle(self):
        self.assertFalse(self.t.busy())

    # -- transfer ----------------------------------------------------------
    def test_a_multi_chunk_push_is_byte_exact(self):
        data = os.urandom(6000)                       # 8000 base64 chars = 4 chunks
        src = Path(self.tmp.name) / "push.bin"
        src.write_bytes(data)
        dest = self.path("push_out.bin")
        self.t.push(src, dest)
        self.assertEqual(Path(dest).read_bytes(), data)

    def test_a_binary_pull_is_byte_exact(self):
        data = os.urandom(9000)
        remote = Path(self.tmp.name) / "pull.bin"
        remote.write_bytes(data)
        local = Path(self.tmp.name) / "pulled.bin"
        self.t.pull(str(remote), local)
        self.assertEqual(local.read_bytes(), data)

    def test_run_script_round_trips_a_multiline_script(self):
        """Multi-line input must go through a pushed file; sent as one line the
        pane's shell waits on a heredoc terminator that never arrives."""
        r = self.t.run_script("set -e\nfor i in 1 2 3; do echo \"n=$i\"; done\n")
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.stdout.split(), ["n=1", "n=2", "n=3"])


if __name__ == "__main__":
    unittest.main()
