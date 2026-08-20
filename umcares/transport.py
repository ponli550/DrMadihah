"""Getting commands onto the Adobe machine, reliably.

Two transports, tried in order:

1. SSHTransport   — a real ssh connection. Clean stdout/stderr, real exit
                    codes, fast. Used whenever ssh can authenticate.
2. TmuxTransport  — drives an ssh session that is already open in a tmux pane.
                    This is the fallback for when no SSH key is available and
                    the only live session is one a human logged in by hand.

The tmux path is the fiddly one, and these are the rules that make it work:

  * Command output is written to a temp file, then base64'd back between
    unique markers. The pane wraps lines at its width and injects ANSI, which
    silently corrupts raw output; base64 survives because we strip every
    whitespace character before decoding.
  * Exit codes are captured explicitly and returned, so callers can branch.
  * Input is sent in size-limited chunks and the accumulated length is checked
    after every chunk. tmux send-keys drops input when the remote shell is
    busy, with no error — this cost us a corrupted binary before we caught it.
  * Nothing is ever typed into a pane that is running a foreground process,
    because the keystrokes land inside that process (they ended up inside
    ffmpeg's interactive prompt more than once).

The ssh path multiplexes. Without it every command is a fresh TCP connect plus
key exchange, and this pipeline does not issue a handful of commands — it probes
each asset for existence, pushes each caption PNG, polls each render. Over a
tailnet that handshake is 100-300ms, so a render spent a visible share of its
wall clock shaking hands. One master connection is reused by every later
command and by scp, which is also why `push_many` batching and this work
together rather than overlapping.
"""
from __future__ import annotations

import base64
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import log
from .config import Remote

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\a]*\a")

# A unix socket path caps at 104 bytes on macOS (108 on Linux) and ssh fails
# with "too long for Unix domain socket" rather than anything self-explanatory.
# %C expands to a 40-character SHA1 hex of (user, host, port) — measured, not
# assumed — so the directory holding it has to stay short.
CONTROL_MAX = 100
CONTROL_HASH = 40

# stderr fragments that mean the master is unusable, as opposed to the command
# itself having failed. A *stale* socket needs nothing from us: OpenSSH 10.2 was
# observed to unlink it and open a fresh master, silently and successfully. This
# is for the case it does not handle — a master that is alive but wedged, whose
# own TCP connection died without it noticing — which was not reproduced here
# and is guarded rather than tested.
MUX_BROKEN = ("mux_client", "read from master failed")


def control_dir() -> Path:
    """Where master sockets live. Its own directory, 0700, never world-readable."""
    return Path.home() / ".ssh" / "umcares-cm"


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def check(self, what: str) -> "Result":
        if not self.ok:
            detail = (self.stderr or self.stdout).strip()[:400]
            raise RuntimeError(f"{what} failed (rc={self.rc}): {detail}")
        return self


class Transport:
    name = "base"

    def run(self, cmd: str, timeout: int = 120) -> Result:
        raise NotImplementedError

    def push(self, local: Path, remote: str) -> None:
        raise NotImplementedError

    def pull(self, remote: str, local: Path) -> None:
        raise NotImplementedError

    def ensure_dir(self, remote_dir: str) -> None:
        """`mkdir -p` a remote directory, once per session.

        scp will not create a missing destination directory; it fails with a
        bare "No such file or directory" that reads like the SOURCE is missing.
        Results are cached because the alternative is an extra round trip on
        every single file.
        """
        seen = getattr(self, "_dirs_made", None)
        if seen is None:
            seen = self._dirs_made = set()
        if remote_dir in seen:
            return
        self.run(f"mkdir -p {shlex.quote(remote_dir)}", timeout=60)
        seen.add(remote_dir)

    def push_many(self, locals_: list, remote_dir: str) -> int:
        """Upload many files into one directory. Override for a faster path."""
        self.ensure_dir(remote_dir)
        for f in locals_:
            self.push(Path(f), f"{remote_dir.rstrip('/')}/{Path(f).name}")
        return len(locals_)

    # -- helpers shared by both transports ---------------------------------
    def run_script(self, script: str, timeout: int = 600, shell: str = "bash") -> Result:
        """Run a multi-line script remotely.

        Anything with a heredoc, loop or multiple lines MUST come through here.
        Sending it as a single tmux line makes the remote shell sit waiting for
        a heredoc terminator that never arrives, and the call hangs forever.
        Writing a file and executing it sidesteps shell quoting entirely.
        """
        import tempfile
        import uuid as _uuid
        remote_path = f"/tmp/.umc_script_{_uuid.uuid4().hex[:8]}.sh"
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(script)
            local = Path(fh.name)
        try:
            self.push(local, remote_path)
            # NOTE: never `exit` here. On the tmux transport the command runs
            # in the interactive login shell, and `exit` would close the ssh
            # session itself. `(exit $__s)` sets $? from a subshell instead.
            return self.run(
                f"{shell} {remote_path}; __s=$?; rm -f {remote_path}; (exit $__s)",
                timeout=timeout)
        finally:
            local.unlink(missing_ok=True)

    def resolve_bin(self, name: str) -> str:
        """Absolute path to a remote binary, or the bare name if not found.

        A non-interactive ssh session does not source the login profile, so
        version managers (nvm, pyenv, rbenv) are invisible and `node` is simply
        "command not found" — even though it works fine in an interactive
        shell. Try the plain lookup, then a login shell, then the usual
        version-manager locations.
        """
        cached = getattr(self, "_bin_cache", None)
        if cached is None:
            cached = {}
            self._bin_cache = cached
        if name in cached:
            return cached[name]

        probes = [
            f"command -v {name} 2>/dev/null",
            f"zsh -lc 'command -v {name}' 2>/dev/null",
            f"bash -lc 'command -v {name}' 2>/dev/null",
            f"ls -1 ~/.nvm/versions/node/*/bin/{name} 2>/dev/null | tail -1",
            f"ls -1 /opt/homebrew/bin/{name} /usr/local/bin/{name} 2>/dev/null | head -1",
        ]
        for probe in probes:
            r = self.run(probe, timeout=45)
            path = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
            if path.startswith("/"):
                log.debug(f"resolved {name} -> {path}")
                cached[name] = path
                return path
        log.debug(f"could not resolve {name}; using bare name")
        cached[name] = name
        return name

    def exists(self, remote_path: str) -> bool:
        r = self.run(f"test -e {shlex.quote(remote_path)} && echo Y || echo N", timeout=30)
        return r.stdout.strip().endswith("Y")

    def size(self, remote_path: str) -> int:
        r = self.run(f"wc -c < {shlex.quote(remote_path)} 2>/dev/null || echo -1", timeout=30)
        return self._trailing_int(r.stdout)

    @staticmethod
    def _trailing_int(out: str) -> int:
        """The last whitespace-separated integer in some output, or -1."""
        try:
            return int((out or "").strip().split()[-1])
        except (ValueError, IndexError):
            return -1


# --------------------------------------------------------------------------
class SSHTransport(Transport):
    name = "ssh"

    def __init__(self, target: str, password: str = "", key: str = "",
                 mux: bool = True, persist: str = "10m"):
        self.target = target
        self.password = password
        self.key = key
        self.persist = persist
        self._socket = self._control_path() if mux else ""

    def _control_path(self) -> str:
        """The master socket path, or "" when it would not fit / cannot be made.

        Multiplexing is an optimisation, so every reason it cannot run is a
        reason to carry on without it rather than to fail.
        """
        try:
            d = control_dir()
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as e:
            log.debug(f"no control dir, multiplexing off: {e}")
            return ""
        path = str(d / "cm-%C")
        # Budget for the expansion before ssh discovers the name is too long,
        # because what it says then is about Unix domain sockets, not about ssh.
        if len(path) - len("%C") + CONTROL_HASH > CONTROL_MAX:
            log.debug(f"control path would exceed {CONTROL_MAX} bytes, "
                      "multiplexing off")
            return ""
        return path

    def _mux(self) -> list:
        if not self._socket:
            return []
        return ["-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._socket}",
                "-o", f"ControlPersist={self.persist}"]

    def _base(self, batch: bool = True) -> list:
        """ssh argv, optionally wrapped in sshpass for password auth."""
        cmd = ["ssh"]
        if self.key:
            cmd += ["-i", self.key, "-o", "IdentitiesOnly=yes"]
        if self.password:
            # sshpass keeps it non-interactive; a key is still the better answer
            cmd = ["sshpass", "-p", self.password] + cmd
        elif batch:
            cmd += ["-o", "BatchMode=yes"]
        cmd += ["-o", "StrictHostKeyChecking=accept-new"] + self._mux()
        return cmd

    def _scp(self) -> list:
        """scp argv sharing the same master, so a transfer costs no handshake.

        scp takes the same -o options as ssh; without them the file transfers
        opened their own connections while ssh reused one, which is half the
        commands in a render.
        """
        return ((["sshpass", "-p", self.password] if self.password else [])
                + ["scp", "-q"]
                + (["-i", self.key, "-o", "IdentitiesOnly=yes"] if self.key else [])
                + ["-o", "StrictHostKeyChecking=accept-new"] + self._mux())

    def _drop_master(self) -> bool:
        """Tear down a wedged master so the next command opens a fresh one.

        Asks ssh to exit it first, which also reaps the background process, then
        unlinks the socket as a backstop in case the master was too wedged to
        answer that.
        """
        if not self._socket:
            return False
        subprocess.run(["ssh", "-o", f"ControlPath={self._socket}",
                        "-O", "exit", self.target],
                       capture_output=True, timeout=15)
        for f in control_dir().glob("cm-*"):
            try:
                f.unlink()
            except OSError:
                pass
        log.debug("dropped the ssh master socket")
        return True

    @staticmethod
    def probe(remote: Remote, timeout: int = 8) -> "SSHTransport | None":
        """Return a working SSH transport, or None. Never prompts interactively.

        Tries, in order: an explicit key, the ssh alias / user@host with
        whatever the agent offers, and finally a configured password via
        sshpass. Anything that would block on a prompt is skipped.
        """
        import os
        import shutil as _sh

        key = os.path.expanduser(remote.key_path) if remote.key_path else ""
        key = key if key and os.path.exists(key) else ""
        pw = remote.password
        if pw and not _sh.which("sshpass"):
            log.debug("UMC_SSH_PASSWORD set but sshpass is not installed")
            pw = ""

        # If we have a key and the agent appears empty, load the key once.
        # This fixes the common case where scripts pass transport=None and the
        # agent has not been populated yet.
        if key and not SSHTransport._agent_has_identities():
            try:
                subprocess.run(["ssh-add", key], capture_output=True, timeout=15)
                log.debug("ssh-add loaded configured key")
            except Exception as e:
                log.debug(f"ssh-add failed: {e}")

        targets = [c for c in (remote.ssh_alias, f"{remote.user}@{remote.host}") if c]
        for use_pw in ([False, True] if pw else [False]):
            for target in targets:
                cand = SSHTransport(target, pw if use_pw else "", key,
                                    mux=getattr(remote, "ssh_mux", True),
                                    persist=getattr(remote, "ssh_persist", "10m"))
                try:
                    argv = cand._base(batch=not use_pw) + [
                        "-o", f"ConnectTimeout={timeout}", target, "echo __UMC_OK__"]
                    p = subprocess.run(argv, capture_output=True, text=True,
                                       timeout=timeout + 8)
                    if p.returncode == 0 and "__UMC_OK__" in p.stdout:
                        how = "password" if use_pw else ("key" if key else "agent")
                        log.debug(f"ssh works via '{target}' ({how})")
                        return cand
                    log.debug(f"ssh '{target}' rc={p.returncode}: {p.stderr.strip()[:120]}")
                except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                    log.debug(f"ssh '{target}' unavailable: {e}")
        return None

    @staticmethod
    def _agent_has_identities() -> bool:
        try:
            p = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=10)
            return p.returncode == 0
        except Exception:
            return False

    def _login_path(self) -> str:
        """PATH as an interactive login shell sees it.

        A non-interactive ssh session skips the login profile, so Homebrew and
        every version manager vanish: ffmpeg, ffprobe, node and docker are all
        "command not found" even though they work when you log in by hand.
        Fetch the real PATH once and prepend it to everything after that.
        """
        if getattr(self, "_cached_path", None):
            return self._cached_path
        for shell in ("zsh", "bash"):
            p = subprocess.run(
                self._base() + [self.target, f"{shell} -lc 'printf %s \"$PATH\"'"],
                capture_output=True, text=True, timeout=45)
            path = p.stdout.strip()
            if p.returncode == 0 and ":" in path and "/bin" in path:
                self._cached_path = path
                log.debug(f"login PATH resolved ({len(path.split(':'))} entries)")
                return path
        # last resort: the usual suspects
        self._cached_path = ("/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
                             "/usr/sbin:/sbin")
        return self._cached_path

    def run(self, cmd: str, timeout: int = 120) -> Result:
        wrapped = f'export PATH={shlex.quote(self._login_path())}; {cmd}'
        p = subprocess.run(self._base() + [self.target, wrapped],
                           capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0 and self._mux_wedged(p.stderr):
            # The command never reached the remote, so re-running it is safe
            # here in a way that a general retry would not be.
            self._drop_master()
            p = subprocess.run(self._base() + [self.target, wrapped],
                               capture_output=True, text=True, timeout=timeout)
        return Result(p.returncode, p.stdout, p.stderr)

    def _mux_wedged(self, stderr: str) -> bool:
        return bool(self._socket) and any(m in (stderr or "") for m in MUX_BROKEN)

    def push(self, local: Path, remote: str) -> None:
        parent = remote.rsplit("/", 1)[0]
        if parent and parent != remote:
            self.ensure_dir(parent)
        argv = self._scp() + [str(local), f"{self.target}:{remote}"]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=900)
        if p.returncode != 0:
            raise RuntimeError(f"scp push failed: {p.stderr.strip()[:300]}")

    def push_many(self, locals_: list, remote_dir: str) -> int:
        """One scp for the whole batch — 54 files should not be 54 connections."""
        if not locals_:
            return 0
        self.ensure_dir(remote_dir)
        argv = self._scp() + [str(f) for f in locals_] + \
               [f"{self.target}:{remote_dir.rstrip('/')}/"]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError(f"scp batch push failed: {p.stderr.strip()[:300]}")
        return len(locals_)

    def pull(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        argv = self._scp() + [f"{self.target}:{remote}", str(local)]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=900)
        if p.returncode != 0:
            raise RuntimeError(f"scp pull failed: {p.stderr.strip()[:300]}")


# --------------------------------------------------------------------------
class TmuxTransport(Transport):
    name = "tmux"

    CHUNK = 2000          # base64 chars per send-keys; larger stalls the PTY

    def __init__(self, pane: str):
        self.pane = pane

    # -- discovery ----------------------------------------------------------
    @staticmethod
    def find_pane(remote: Remote) -> "str | None":
        if remote.tmux_pane:
            return remote.tmux_pane
        try:
            p = subprocess.run(
                ["tmux", "list-panes", "-a", "-F",
                 "#{pane_id}\t#{session_name}\t#{pane_current_command}"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if p.returncode != 0:
            return None
        best = None
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            pane_id, session, cmd = parts[0], parts[1], parts[2]
            if cmd.strip() == "ssh":
                # prefer a pane in the configured session
                if session == remote.tmux_session:
                    return pane_id
                best = best or pane_id
        return best

    @staticmethod
    def probe(remote: Remote) -> "TmuxTransport | None":
        pane = TmuxTransport.find_pane(remote)
        if not pane:
            return None
        t = TmuxTransport(pane)
        if t.busy():
            log.warn(f"tmux pane {pane} is running something; waiting for it to finish")
            if not t.wait_idle(timeout=60):
                log.warn(f"pane {pane} still busy — commands would be typed into it")
                return None
        r = t.run("echo __UMC_OK__; hostname", timeout=25)
        if not (r.ok and "__UMC_OK__" in r.stdout):
            return None

        # A pane whose ssh session has died silently falls back to a LOCAL
        # shell, and every "remote" command would then run on this machine.
        # Refuse unless the pane really is somewhere else.
        import socket
        here = socket.gethostname().split(".")[0].lower()
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        there = (lines[-1].split(".")[0].lower() if lines else "")
        if not there or there == here:
            log.err(f"pane {pane} is a LOCAL shell ({there or 'unknown'}), not the remote — "
                    f"its ssh session has probably dropped")
            log.warn(f"reconnect ssh in that pane, then retry")
            return None

        expected = remote.host.split(".")[0].lower()
        if expected and there != expected:
            log.warn(f"pane {pane} is on '{there}', expected '{expected}' — continuing anyway")
        log.debug(f"tmux transport ready on pane {pane} (host {there})")
        return t

    # -- pane state ---------------------------------------------------------
    def _tmux(self, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)

    def busy(self) -> bool:
        """True when a foreground process owns the pane (never type into it)."""
        p = self._tmux("display-message", "-p", "-t", self.pane, "#{pane_current_command}")
        cmd = p.stdout.strip()
        return cmd not in ("ssh", "bash", "zsh", "sh", "-zsh", "-bash", "fish", "")

    def wait_idle(self, timeout: int = 120) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if not self.busy():
                return True
            time.sleep(2)
        return not self.busy()

    def _capture(self, lines: int = 4000) -> str:
        p = self._tmux("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", self.pane, timeout=30)
        return ANSI.sub("", p.stdout.replace("\r", ""))

    def _send(self, text: str) -> None:
        self._tmux("send-keys", "-t", self.pane, text, "C-m")

    # -- command execution --------------------------------------------------
    def run(self, cmd: str, timeout: int = 120) -> Result:
        """Run remotely; return real exit code and untouched output.

        Output travels back base64-encoded between markers so that pane
        wrapping and ANSI escapes cannot corrupt it.

        One sharp edge, inherited from where this runs: the command is executed
        by the pane's own interactive shell, inside `{ ...; }` rather than a
        subshell. So a command containing a bare `exit` exits *that* shell —
        which is the ssh session — and this call then waits out its timeout
        with the connection already gone. Wrap it yourself: `( exit 3 )`.
        """
        tag = "UMC" + uuid.uuid4().hex[:10].upper()
        tmp = f"/tmp/.umc_{tag}"
        # The markers are assembled from a shell variable at RUNTIME. If the
        # literal "TAG_E" appeared in the command text, tmux would show it the
        # moment the line is echoed -- before the command has run -- and the
        # poller would parse the echo as output. Referencing ${__t} keeps the
        # expanded marker out of the echoed line entirely.
        wrapped = (
            f"__t={tag}; {{ {cmd} ; }} > {tmp}.out 2> {tmp}.err; __rc=$?; "
            f'echo "${{__t}}_S"; echo RC=$__rc; '
            f"base64 < {tmp}.out | tr -d '\\n'; echo; "
            f'echo "${{__t}}_M"; '
            f"base64 < {tmp}.err | tr -d '\\n'; echo; "
            f'echo "${{__t}}_E"; '
            f"rm -f {tmp}.out {tmp}.err"
        )
        self._send(wrapped)

        end = time.time() + timeout
        while time.time() < end:
            buf = self._capture()
            if f"{tag}_E" in buf:
                return self._parse(buf, tag)
            time.sleep(0.4)
        return Result(124, "", f"timeout after {timeout}s")

    def _parse(self, buf: str, tag: str) -> Result:
        # capture-pane also contains the ECHOED command, which embeds the
        # marker literals and (for pushes) base64 payload. Always take the
        # LAST block, which is the real output.
        try:
            e = buf.rindex(f"{tag}_E")
            m = buf.rindex(f"{tag}_M", 0, e)
            s = buf.rindex(f"{tag}_S", 0, m)
            head = buf[s + len(f"{tag}_S"):m]
            errpart = buf[m + len(f"{tag}_M"):e]
        except ValueError:
            return Result(125, "", "could not parse marker block")

        m = re.search(r"RC=(-?\d+)", head)
        rc = int(m.group(1)) if m else 126
        return Result(rc, _b64_text(_payload(head[m.end():] if m else head)),
                      _b64_text(_payload(errpart)))

    # -- file transfer ------------------------------------------------------
    def push(self, local: Path, remote: str) -> None:
        data = base64.b64encode(local.read_bytes()).decode()
        total = len(data)
        stage = f"/tmp/.umc_push_{uuid.uuid4().hex[:8]}"
        self.run(f": > {stage}", timeout=30).check("stage file")

        sent = 0
        offset = 0
        while offset < total:
            part = data[offset:offset + self.CHUNK]
            want = offset + len(part)
            for attempt in range(3):
                # The append and its own length check travel as ONE command,
                # through the marker protocol. The previous shape — type the
                # chunk, sleep a fixed 0.35s, then type a separate `wc -c` —
                # lost that second command whenever the shell was still
                # consuming the first: measured across 40, 80 and 200 column
                # panes, 0.35s lost it every time and 1.5s never did. Guessing
                # a longer pause would cost that on every chunk of every file;
                # asking once and waiting for the answer costs nothing and
                # cannot race, because run() polls until its end marker lands.
                r = self.run(f"printf '%s' '{part}' >> {stage}; wc -c < {stage}",
                             timeout=90)
                got = self._trailing_int(r.stdout) if r.ok else -1
                if got == want:
                    break
                # send-keys silently drops input when the shell is busy;
                # rewind to the known-good prefix and resend this chunk
                log.debug(f"chunk resend at {offset} (got {got}, want {want})")
                self.run(f"truncate -s {offset} {stage}", timeout=30)
            else:
                raise RuntimeError(f"push stalled at byte {offset} of {total}")
            offset = want
            sent += 1

        self.run(f"base64 -d < {stage} > {shlex.quote(remote)} && rm -f {stage}",
                 timeout=120).check("decode")
        local_n = local.stat().st_size
        remote_n = self.size(remote)
        if remote_n != local_n:
            raise RuntimeError(f"push verify failed: local {local_n} != remote {remote_n}")
        log.debug(f"pushed {local.name} in {sent} chunks, {local_n} bytes verified")

    def pull(self, remote: str, local: Path) -> None:
        n = self.size(remote)
        if n < 0:
            raise RuntimeError(f"remote file not found: {remote}")
        r = self.run(f"base64 < {shlex.quote(remote)} | tr -d '\\n'", timeout=600)
        r.check("read remote file")
        blob = re.sub(r"[^A-Za-z0-9+/=]", "", r.stdout)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(base64.b64decode(blob))
        if local.stat().st_size != n:
            raise RuntimeError(
                f"pull verify failed: remote {n} != local {local.stat().st_size}")


PURE_B64 = re.compile(r"[A-Za-z0-9+/=]+")


def _payload(region: str) -> str:
    """The base64 lines in a marker region, with pane furniture left out.

    Stripping every non-alphabet character from the whole region and gluing the
    rest together looks equivalent and is not: `/`, letters and digits are all
    base64, so a prompt like `irpan@mac ~/DrMadihah %` contributes
    `irpanmacDrMadihah` to the payload and corrupts it silently.

    A line either is the payload or it is not. The command echoes its base64 on
    a line of its own, so keeping only whole lines that are nothing but base64
    excludes furniture while still tolerating a payload split across several
    lines (which `capture-pane -J` should already have joined).
    """
    return "".join(l.strip() for l in region.splitlines()
                   if l.strip() and PURE_B64.fullmatch(l.strip()))


def _b64_text(blob: str) -> str:
    if not blob:
        return ""
    try:
        return base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


# --------------------------------------------------------------------------
def connect(remote: Remote, prefer: str | None = "auto") -> Transport:
    """Pick the best available transport.

    'auto' tries ssh first because it gives clean stdout and real exit codes,
    then falls back to an already-open tmux ssh pane.
    """
    prefer = prefer or "auto"
    if prefer in ("auto", "ssh"):
        t = SSHTransport.probe(remote)
        if t:
            log.debug("transport: ssh")
            return t
        if prefer == "ssh":
            raise SystemExit("ssh transport requested but no key-based login works.")
        log.debug("ssh unavailable, falling back to tmux")

    if prefer in ("auto", "tmux"):
        t = TmuxTransport.probe(remote)
        if t:
            log.debug(f"transport: tmux pane {t.pane}")
            return t

    raise SystemExit(
        "No transport available.\n"
        "  ssh  : no key-based login (try `ssh-add`, or grant the key)\n"
        "  tmux : no pane running an ssh session was found\n"
        "Run `umcares session` to create the working layout, then log in on the "
        "right-hand pane."
    )
