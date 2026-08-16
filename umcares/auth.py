"""SSH credentials.

Order of preference, best first:

1. A dedicated key installed on the remote (`umcares auth --setup-key`).
   Non-interactive, nothing secret sits in a file, works after reboots.
2. An agent identity you already have loaded.
3. `UMC_SSH_PASSWORD` in .env, used through sshpass. Works, but the password
   is then on disk — .env is gitignored, and that is the only thing protecting
   it. Treat it as a stopgap.
4. No ssh at all: the tmux pane fallback, where a human logs in by hand.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import log
from .config import Remote
from .transport import SSHTransport, TmuxTransport


def status(remote: Remote) -> dict:
    key = os.path.expanduser(remote.key_path)
    info = {
        "key_path": key,
        "key_exists": os.path.exists(key),
        "password_configured": bool(remote.password),
        "sshpass_installed": bool(shutil.which("sshpass")),
        "agent_identities": _agent_count(),
    }
    t = SSHTransport.probe(remote)
    info["ssh_works"] = bool(t)
    if t:
        info["ssh_target"] = t.target
        info["ssh_method"] = ("password" if t.password
                              else "key" if t.key else "agent")
    else:
        pane = TmuxTransport.find_pane(remote)
        info["tmux_pane"] = pane
        info["fallback"] = "tmux pane" if pane else "none"
    return info


def _agent_count() -> int:
    try:
        p = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return 0
        return len([l for l in p.stdout.splitlines() if l.strip()])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0


def setup_key(remote: Remote) -> dict:
    """Create a dedicated keypair and install it on the remote.

    Uses the configured password (via sshpass) if present so this can run
    unattended; otherwise ssh-copy-id will prompt once in your terminal.
    """
    key = Path(os.path.expanduser(remote.key_path))
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    if not key.exists():
        log.step(f"generating {key}")
        p = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "umcares", "-f", str(key)],
            capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise RuntimeError(f"ssh-keygen failed: {p.stderr.strip()[:200]}")
        key.chmod(0o600)
        log.ok("keypair created")
    else:
        log.ok(f"reusing existing key {key}")

    target = remote.ssh_alias or f"{remote.user}@{remote.host}"
    argv = ["ssh-copy-id", "-i", f"{key}.pub", "-o", "StrictHostKeyChecking=accept-new", target]
    if remote.password:
        if not shutil.which("sshpass"):
            raise RuntimeError(
                "UMC_SSH_PASSWORD is set but sshpass is missing.\n"
                "  brew install hudochenkov/sshpass/sshpass\n"
                "…or run without the password and type it when prompted.")
        argv = ["sshpass", "-p", remote.password] + argv
        log.step(f"installing key on {target} (using configured password)")
        p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    else:
        log.step(f"installing key on {target} — you will be asked for the password once")
        p = subprocess.run(argv, timeout=300)      # inherit tty so it can prompt

    out = getattr(p, "stderr", "") or ""
    if p.returncode != 0 and "already exist" not in out:
        raise RuntimeError(f"ssh-copy-id failed: {out.strip()[:300] or 'see output above'}")

    t = SSHTransport.probe(remote)
    if not t:
        raise RuntimeError("key installed but ssh still will not authenticate")
    log.ok("key auth working — the CLI no longer needs the tmux pane")
    return {"key": str(key), "target": t.target, "method": "key"}
