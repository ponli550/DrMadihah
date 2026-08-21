"""The tmux transport: marker parsing, and the chunked push.

This is the fiddly transport, and every rule in it is scar tissue. A pane wraps
lines at its width and injects ANSI, so raw output comes back corrupted — hence
base64 between markers. `send-keys` silently drops input when the remote shell
is busy, with no error and no exit code — hence per-chunk length verification
and truncate-and-resend. A corrupted binary got through before that existed.

The pane here is faked at the `tmux` argv boundary and nowhere above it:
`send-keys` really runs the line through bash, and the rendered result is
appended to a buffer that `capture-pane` returns. So `run`, `_capture`,
`_parse`, `size`, `push` and `pull` are the real code paths — only tmux itself
is absent. `tests/test_tmux_live.py` runs the same operations against a real
pane to check this fake is not lying.
"""
import base64
import os
import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from umcares.transport import Result, TmuxTransport, _b64_text


def done(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


class FakePane(TmuxTransport):
    """A pane whose keystrokes are executed by bash and echoed into a buffer."""

    PUSH_WAIT = 0.4                   # production waits 20s for a real shell

    def run(self, cmd: str, timeout: int = 5) -> Result:
        """Same protocol, shorter patience: lost input should fail the test in
        seconds rather than sitting out the production timeout."""
        return super().run(cmd, min(timeout, 0.6))

    def __init__(self, cwd: str):
        super().__init__("%fake")
        self.cwd = cwd
        self.buf = ""
        self.sent = []
        self.chunk_n = 0
        self.drop_chunks = set()      # chunk numbers tmux "loses" entirely
        self.halve_chunks = set()     # chunk numbers delivered half-typed
        self.ansi = False

    # -- the only seam ------------------------------------------------------
    def _tmux(self, *args, timeout: int = 15):
        if args[0] == "send-keys":
            return self._keys(args[3])
        if args[0] == "capture-pane":
            return done(self._render())
        if args[0] == "display-message":
            return done("bash")
        return done()

    def _keys(self, text: str):
        self.sent.append(text)
        if text.startswith("printf"):        # a bare chunk line, not a run()
            self.chunk_n += 1
            if self.chunk_n in self.drop_chunks:
                self.drop_chunks.discard(self.chunk_n)
                return done()                      # keystrokes went nowhere
            if self.chunk_n in self.halve_chunks:
                self.halve_chunks.discard(self.chunk_n)
                text = self._half(text)
        self.buf += text + "\n"                    # the shell echoes the line
        p = subprocess.run(["bash", "-c", text], capture_output=True,
                           text=True, cwd=self.cwd)
        self.buf += p.stdout
        return done()

    def _half(self, text: str) -> str:
        """The same append, with only half the payload — a part-typed chunk."""
        def cut(m):
            payload = m.group(2)
            return m.group(1) + payload[:len(payload) // 2] + m.group(3)
        return re.sub(r"(printf '%s' ')([A-Za-z0-9+/=]*)(')", cut, text, count=1)

    def _render(self) -> str:
        """What capture-pane returns: the same text, wearing colour.

        Line wrapping is deliberately not simulated. `capture-pane -J` joins
        wrapped lines back together, so a marker split across a pane boundary
        is not a state the real code can observe; the residual case — a payload
        arriving in pieces — is tested against `_parse` directly.
        """
        out = self.buf
        if self.ansi:
            out = "\x1b[32m" + out.replace("\n", "\x1b[0m\r\n\x1b[32m") + "\x1b[0m"
        return out


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.t = FakePane(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.tmp.name, name)


class RunRoundTrip(Fixture):
    def test_stdout_comes_back_clean(self):
        r = self.t.run("echo hello")
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.stdout.strip(), "hello")

    def test_exit_code_is_the_real_one(self):
        self.assertEqual(self.t.run("( exit 42 )").rc, 42)
        self.assertEqual(self.t.run("false").rc, 1)

    def test_a_bare_exit_never_reports(self):
        """The command runs in `{ ...; }` inside the pane's own interactive
        shell, so a bare `exit` exits that shell — which is the ssh session.
        No markers are ever printed and the call waits out its timeout. Pinned
        because it is the difference between `exit 3` and `( exit 3 )`.
        """
        r = self.t.run("exit 3", timeout=1)
        self.assertEqual(r.rc, 124)
        self.assertIn("timeout", r.stderr)

    def test_stderr_is_kept_apart_from_stdout(self):
        r = self.t.run("echo out; echo err >&2")
        self.assertEqual(r.stdout.strip(), "out")
        self.assertEqual(r.stderr.strip(), "err")

    def test_output_survives_ansi_colour(self):
        self.t.ansi = True
        self.assertEqual(self.t.run("echo tinted").stdout.strip(), "tinted")

    def test_ansi_inside_the_output_itself_is_preserved(self):
        """Stripping happens on the pane, not on the payload — a command whose
        output contains escapes must get them back byte for byte."""
        r = self.t.run(r"printf '\033[31mred\033[0m'")
        self.assertEqual(r.stdout, "\x1b[31mred\x1b[0m")

    def test_utf8_survives(self):
        for text in ("Amanah di Dunia Digital", "peratus — 89.3%", "café ✓"):
            self.assertEqual(self.t.run(f"printf '%s' {text!r}").stdout, text)

    def test_a_marker_shaped_string_in_the_output_does_not_end_the_block(self):
        """Output is decoded, not scanned, so it cannot terminate its own block."""
        r = self.t.run("echo 'UMCDEADBEEF_E and RC=99'")
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.stdout.strip(), "UMCDEADBEEF_E and RC=99")

    def test_empty_output_is_empty_not_garbage(self):
        r = self.t.run("true")
        self.assertEqual((r.rc, r.stdout, r.stderr), (0, "", ""))

    def test_large_output(self):
        n = 200_000
        r = self.t.run(f"head -c {n} /dev/zero | tr '\\0' 'a'")
        self.assertEqual(len(r.stdout), n)


class Parsing(unittest.TestCase):
    """`_parse` on hand-built buffers, for the cases a fake pane cannot stage."""

    def block(self, tag, rc, out=b"", err=b""):
        return (f"{tag}_S\nRC={rc}\n{base64.b64encode(out).decode()}\n"
                f"{tag}_M\n{base64.b64encode(err).decode()}\n{tag}_E\n")

    def test_the_last_block_wins(self):
        """Scrollback holds earlier blocks; the newest one is the answer."""
        t = TmuxTransport("%1")
        buf = self.block("UMCAAA", 1, b"stale") + self.block("UMCAAA", 0, b"fresh")
        r = t._parse(buf, "UMCAAA")
        self.assertEqual((r.rc, r.stdout), (0, "fresh"))

    def test_a_missing_block_is_reported_not_guessed(self):
        r = TmuxTransport("%1")._parse("nothing here", "UMCAAA")
        self.assertEqual(r.rc, 125)
        self.assertIn("could not parse", r.stderr)

    def test_a_block_without_an_rc_does_not_look_successful(self):
        t = TmuxTransport("%1")
        buf = "UMCAAA_S\n\nUMCAAA_M\n\nUMCAAA_E\n"
        self.assertEqual(t._parse(buf, "UMCAAA").rc, 126)

    def test_negative_exit_codes_parse(self):
        r = TmuxTransport("%1")._parse(self.block("UMCAAA", -1), "UMCAAA")
        self.assertEqual(r.rc, -1)

    def test_a_prompt_line_is_not_spliced_into_the_payload(self):
        """Filtering the whole region to the base64 alphabet looks equivalent
        and is not: `/`, letters and digits all survive it, so a prompt like
        `irpan@mac ~/DrMadihah %` used to contribute `irpanmacDrMadihah` to the
        payload and corrupt it with no error anywhere.
        """
        t = TmuxTransport("%1")
        b64 = base64.b64encode(b"payload").decode()
        buf = (f"UMCAAA_S\nRC=0\nirpan@mac ~/DrMadihah %\n{b64}\n"
               f"UMCAAA_M\n\nUMCAAA_E\n")
        self.assertEqual(t._parse(buf, "UMCAAA").stdout, "payload")

    def test_a_payload_split_across_lines_is_rejoined(self):
        """-J should have joined it, but a split payload is still readable."""
        t = TmuxTransport("%1")
        b64 = base64.b64encode(b"a longer payload here").decode()
        buf = (f"UMCAAA_S\nRC=0\n{b64[:8]}\n{b64[8:]}\n"
               f"UMCAAA_M\n\nUMCAAA_E\n")
        self.assertEqual(t._parse(buf, "UMCAAA").stdout, "a longer payload here")

    def test_blank_lines_and_carriage_returns_are_ignored(self):
        t = TmuxTransport("%1")
        b64 = base64.b64encode(b"clean").decode()
        buf = f"UMCAAA_S\nRC=0\n\n\r\n{b64}\n\nUMCAAA_M\n\nUMCAAA_E\n"
        self.assertEqual(t._parse(buf, "UMCAAA").stdout, "clean")


class Base64Text(unittest.TestCase):
    def test_missing_padding_is_restored(self):
        blob = base64.b64encode(b"abcde").decode().rstrip("=")
        self.assertEqual(_b64_text(blob), "abcde")

    def test_undecodable_input_is_empty_not_an_exception(self):
        self.assertEqual(_b64_text("!!!!"), "")

    def test_empty_is_empty(self):
        self.assertEqual(_b64_text(""), "")

    def test_invalid_utf8_is_replaced_not_fatal(self):
        self.assertIn("�", _b64_text(base64.b64encode(b"\xff\xfe").decode()))


class ChunkedPush(Fixture):
    def write(self, name: str, data: bytes) -> Path:
        p = Path(self.tmp.name) / name
        p.write_bytes(data)
        return p

    def chunks_sent(self) -> int:
        return len([s for s in self.t.sent if s.startswith("printf")])

    def test_a_small_file_is_one_chunk_and_lands_intact(self):
        src = self.write("small.txt", b"hello remote")
        dest = self.path("out.txt")
        self.t.push(src, dest)
        self.assertEqual(Path(dest).read_bytes(), b"hello remote")
        self.assertEqual(self.chunks_sent(), 1)

    def test_a_file_larger_than_one_chunk_is_split_and_reassembled(self):
        data = os.urandom(9000)                       # 12000 base64 chars
        src = self.write("big.bin", data)
        dest = self.path("big_out.bin")
        self.t.push(src, dest)
        self.assertEqual(Path(dest).read_bytes(), data)
        expected = -(-len(base64.b64encode(data)) // self.t.CHUNK)
        self.assertEqual(self.chunks_sent(), expected)
        self.assertEqual(expected, 6)

    def test_exactly_one_chunk_of_base64_is_not_split(self):
        """Off-by-one at the boundary would send an empty final chunk."""
        src = self.write("exact.bin", b"a" * ((self.t.CHUNK // 4) * 3))
        self.t.push(src, self.path("exact_out.bin"))
        self.assertEqual(self.chunks_sent(), 1)

    def test_a_dropped_chunk_is_resent_and_the_file_is_still_correct(self):
        """send-keys drops input when the shell is busy, silently."""
        data = os.urandom(9000)
        src = self.write("drop.bin", data)
        dest = self.path("drop_out.bin")
        self.t.drop_chunks = {3}
        self.t.push(src, dest)
        self.assertEqual(Path(dest).read_bytes(), data)
        self.assertEqual(self.chunks_sent(), 7)       # 6 + the one resend

    def test_a_half_typed_chunk_is_rewound_not_appended_to(self):
        """The nastiest case: input partly arrives, so the file is the wrong
        length rather than short by a whole chunk. Resending without rewinding
        would leave the half in place and corrupt the payload."""
        data = os.urandom(9000)
        src = self.write("half.bin", data)
        dest = self.path("half_out.bin")
        self.t.halve_chunks = {2}
        self.t.push(src, dest)
        self.assertEqual(Path(dest).read_bytes(), data)
        self.assertIn("truncate -s", " ".join(self.t.sent))

    def test_giving_up_is_loud(self):
        data = os.urandom(4000)
        src = self.write("stall.bin", data)
        self.t.drop_chunks = {1, 2, 3}                # every attempt lost
        with self.assertRaises(RuntimeError) as e:
            self.t.push(src, self.path("stall_out.bin"))
        self.assertIn("stalled", str(e.exception))

    def test_a_truncated_transfer_is_caught_by_the_size_check(self):
        """Belt and braces: even if every chunk 'succeeded', the sizes must match."""
        src = self.write("v.bin", os.urandom(3000))
        dest = self.path("v_out.bin")
        real_size = self.t.size

        def lying_size(path, timeout=30):
            return 1 if path == dest else real_size(path, timeout)

        self.t.size = lying_size
        with self.assertRaises(RuntimeError) as e:
            self.t.push(src, dest)
        self.assertIn("verify failed", str(e.exception))

    def test_an_empty_file_pushes(self):
        src = self.write("empty.bin", b"")
        dest = self.path("empty_out.bin")
        self.t.push(src, dest)
        self.assertEqual(Path(dest).read_bytes(), b"")


class Pull(Fixture):
    def test_binary_pull_is_byte_exact(self):
        data = os.urandom(12000)
        remote = Path(self.tmp.name) / "r.bin"
        remote.write_bytes(data)
        local = Path(self.tmp.name) / "l.bin"
        self.t.pull(str(remote), local)
        self.assertEqual(local.read_bytes(), data)

    def test_a_missing_remote_file_is_an_error_not_an_empty_file(self):
        with self.assertRaises(RuntimeError):
            self.t.pull(self.path("nope.bin"), Path(self.tmp.name) / "x.bin")

    def test_pull_creates_the_local_directory(self):
        remote = Path(self.tmp.name) / "r2.bin"
        remote.write_bytes(b"data")
        local = Path(self.tmp.name) / "deep" / "nest" / "l2.bin"
        self.t.pull(str(remote), local)
        self.assertEqual(local.read_bytes(), b"data")


class PaneState(unittest.TestCase):
    def test_a_shell_is_idle_and_anything_else_is_not(self):
        t = TmuxTransport("%1")
        for shell in ("ssh", "bash", "zsh", "-zsh", "fish", ""):
            t._tmux = lambda *a, s=shell, **k: done(s + "\n")
            self.assertFalse(t.busy(), shell)
        for proc in ("ffmpeg", "vim", "node"):
            t._tmux = lambda *a, p=proc, **k: done(p + "\n")
            self.assertTrue(t.busy(), proc)


if __name__ == "__main__":
    unittest.main()
