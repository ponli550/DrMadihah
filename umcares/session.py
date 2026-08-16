"""Create the working tmux layout.

    +---------------------+-------------+
    |  editor  (nvim)     |             |
    +---------------------+   remote    |
    |  cli     (shell)    |  ssh+Adobe  |
    +---------------------+-------------+

The right-hand pane is the one the tmux transport drives, so it must hold a
live ssh session to the Adobe machine. If ssh keys work it logs in by itself;
if not it leaves the command typed and ready so a human can enter a password.
"""
from __future__ import annotations

import subprocess
import time

from . import log
from .config import Remote

EDITOR = "editor"
CLI = "cli"
REMOTE = "remote"


def _tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=20)
    if check and p.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)}: {p.stderr.strip()}")
    return p


def have_tmux() -> bool:
    try:
        return subprocess.run(["tmux", "-V"], capture_output=True, timeout=10).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def session_exists(name: str) -> bool:
    return _tmux("has-session", "-t", name).returncode == 0


def pane_ids(session: str) -> dict:
    """Map our pane titles to live pane ids."""
    p = _tmux("list-panes", "-t", session, "-F", "#{pane_id}\t#{pane_title}")
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


def create(remote: Remote, cwd: str, editor: str = "nvim",
           attach: bool = True, force: bool = False) -> dict:
    """Build the layout. Idempotent unless force=True."""
    if not have_tmux():
        raise SystemExit("tmux is not installed. `brew install tmux`")

    name = remote.tmux_session

    if session_exists(name):
        if not force:
            log.ok(f"session '{name}' already exists — reusing it")
            info = pane_ids(name)
            if attach:
                _attach(name)
            return info
        log.warn(f"killing existing session '{name}' (--force)")
        _tmux("kill-session", "-t", name)

    log.step(f"creating session '{name}'")
    # window 'main' with the editor pane
    _tmux("new-session", "-d", "-s", name, "-n", "main", "-c", cwd, check=True)
    left = _tmux("list-panes", "-t", name, "-F", "#{pane_id}").stdout.strip().splitlines()[0]

    # right column: the ssh/Adobe pane, 45% of the width
    _tmux("split-window", "-h", "-p", "45", "-t", left, "-c", cwd, check=True)
    right = _current_pane(name, exclude=[left])

    # split the left column: editor on top, cli underneath
    _tmux("split-window", "-v", "-p", "35", "-t", left, "-c", cwd, check=True)
    bottom = _current_pane(name, exclude=[left, right])

    for pid, title in ((left, EDITOR), (bottom, CLI), (right, REMOTE)):
        _tmux("select-pane", "-t", pid, "-T", title)

    # keep pane titles visible so it is obvious which pane is which
    _tmux("set-option", "-t", name, "pane-border-status", "top")
    _tmux("set-option", "-t", name, "pane-border-format", " #{pane_title} ")
    # generous scrollback: exports and ffmpeg are chatty
    _tmux("set-option", "-t", name, "history-limit", "100000")

    _tmux("send-keys", "-t", left, editor, "C-m")
    _tmux("send-keys", "-t", bottom, f"cd {cwd}", "C-m")

    _login(right, remote)

    _tmux("select-pane", "-t", bottom)
    log.ok(f"session '{name}' ready — editor / cli / remote")

    ids = {EDITOR: left, CLI: bottom, REMOTE: right}
    if attach:
        _attach(name)
    return ids


def _current_pane(session: str, exclude: list) -> str:
    for pid in _tmux("list-panes", "-t", session, "-F", "#{pane_id}").stdout.split():
        if pid not in exclude:
            return pid
    raise RuntimeError("could not identify the new pane")


def _login(pane: str, remote: Remote) -> None:
    """Start the ssh session in the remote pane.

    Tries the ssh alias first, then user@host. The command is sent either way;
    if keys are missing the operator just types a password into that pane and
    everything else keeps working.
    """
    target = remote.ssh_alias or f"{remote.user}@{remote.host}"
    log.step(f"opening ssh to {target} in the remote pane")
    _tmux("send-keys", "-t", pane,
          f"ssh -o ConnectTimeout=10 {target} || ssh {remote.user}@{remote.host}", "C-m")
    time.sleep(2)


def _attach(name: str) -> None:
    """Attach, or switch if we are already inside tmux."""
    import os
    if os.environ.get("TMUX"):
        log.info(f"already inside tmux — switching to '{name}'")
        _tmux("switch-client", "-t", name)
    else:
        log.info(f"attaching to '{name}' (detach with Ctrl-b d)")
        os.execvp("tmux", ["tmux", "attach-session", "-t", name])


def status(remote: Remote) -> dict:
    """Describe the layout without changing anything."""
    name = remote.tmux_session
    if not have_tmux():
        return {"tmux": False}
    if not session_exists(name):
        return {"tmux": True, "session": name, "exists": False}
    p = _tmux("list-panes", "-t", name, "-F",
              "#{pane_id}\t#{pane_title}\t#{pane_current_command}\t#{pane_width}x#{pane_height}")
    panes = []
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 4:
            panes.append({"id": f[0], "title": f[1], "cmd": f[2], "size": f[3]})
    return {"tmux": True, "session": name, "exists": True, "panes": panes}


# Never type into these: the keystrokes become editor input.
EDITORS = {"nvim", "vim", "vi", "emacs", "nano", "helix", "hx", "less", "man"}


def remote_pane(remote: Remote) -> str | None:
    """Find the pane that should hold the ssh session.

    Order: an explicitly titled 'remote' pane, then a pane already running
    ssh, then the RIGHTMOST pane (that is where the layout puts it). Editors
    are excluded outright — picking one and sending it an ssh command types
    that command into the buffer.
    """
    ids = pane_ids(remote.tmux_session)
    if REMOTE in ids:
        return ids[REMOTE]

    p = _tmux("list-panes", "-t", remote.tmux_session, "-F",
              "#{pane_id}\t#{pane_current_command}\t#{pane_left}")
    panes = []
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 3:
            panes.append({"id": f[0], "cmd": f[1].strip(), "left": int(f[2] or 0)})

    for pane in panes:
        if pane["cmd"] == "ssh":
            return pane["id"]

    usable = [x for x in panes if x["cmd"] not in EDITORS]
    if not usable:
        return None
    return max(usable, key=lambda x: x["left"])["id"]


def reconnect(remote: Remote, wait: int = 25) -> dict:
    """Re-open ssh in the remote pane after the session drops.

    Long-lived interactive ssh sessions die — network blips, the laptop
    sleeping, a stray Ctrl-C. When that happens the pane silently reverts to a
    LOCAL shell, so this is worth having one command away. With key auth it is
    fully automatic; with a password you type it once.
    """
    import time as _t
    pane = remote_pane(remote)
    if not pane:
        raise SystemExit(f"no pane found in session '{remote.tmux_session}' — "
                         "run `umcares session` first")

    cmd = _tmux("display-message", "-p", "-t", pane,
                "#{pane_current_command}").stdout.strip()
    if cmd in EDITORS:
        raise SystemExit(f"pane {pane} is running {cmd} — refusing to type into an "
                         f"editor. Pin the right pane with UMC_TMUX_PANE=%id")

    target = remote.ssh_alias or f"{remote.user}@{remote.host}"
    log.step(f"reconnecting {pane} ({cmd}) -> {target}")
    # get back to a clean prompt first, whatever state the pane is in
    _tmux("send-keys", "-t", pane, "C-c")
    _t.sleep(1)
    _tmux("send-keys", "-t", pane, "", "C-m")
    _t.sleep(1)
    _tmux("send-keys", "-t", pane,
          f"ssh -o ConnectTimeout=10 -o ServerAliveInterval=30 "
          f"-o ServerAliveCountMax=6 {target} || "
          f"ssh -o ServerAliveInterval=30 {remote.user}@{remote.host}", "C-m")

    deadline = _t.time() + wait
    while _t.time() < deadline:
        _t.sleep(2)
        cap = _tmux("capture-pane", "-p", "-t", pane).stdout
        if "assword" in cap.splitlines()[-1] if cap.splitlines() else False:
            log.warn("the remote is asking for a password — type it in that pane")
            return {"pane": pane, "state": "password_required"}
        if remote.host.split(".")[0] in cap:
            log.ok("reconnected")
            return {"pane": pane, "state": "connected"}
    log.warn("could not confirm the connection; check the pane")
    return {"pane": pane, "state": "unknown"}
