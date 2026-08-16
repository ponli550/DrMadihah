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
import shlex
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


def setup_key(remote: Remote, transport=None) -> dict:
    """Create a keypair and install it on the remote.

    Primary path: push the public key through the transport that already
    works (usually the logged-in tmux pane). That needs no password at all,
    which matters because ssh-copy-id prompts interactively and therefore
    cannot run unattended.

    Falls back to ssh-copy-id only if no transport is available.
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

    pub = Path(str(key) + ".pub").read_text(encoding="utf-8").strip()

    if transport is not None:
        return _install_via_transport(transport, remote, key, pub)
    return _install_via_copy_id(remote, key)


def _install_via_transport(t, remote: Remote, key: Path, pub: str) -> dict:
    """Append the public key to authorized_keys over the live session."""
    log.step("installing the public key through the existing session")
    # `grep -qxF` keeps this idempotent; permissions matter or sshd ignores it
    script = f"""
set -e
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
if grep -qxF {shlex.quote(pub)} ~/.ssh/authorized_keys; then
  echo "ALREADY_PRESENT"
else
  printf '%s\n' {shlex.quote(pub)} >> ~/.ssh/authorized_keys
  echo "APPENDED"
fi
echo "KEYS=$(grep -c . ~/.ssh/authorized_keys)"
"""
    r = t.run_script(script, timeout=180)
    r.check("install key")
    state = "already present" if "ALREADY_PRESENT" in r.stdout else "appended"
    log.ok(f"public key {state} in ~/.ssh/authorized_keys")

    probe = SSHTransport.probe(remote)
    if not probe:
        log.warn("key installed but ssh still will not authenticate — the server "
                 "may disallow publickey, or the home directory may be group-writable")
        return {"key": str(key), "installed": state, "ssh_works": False}
    log.ok(f"key auth working via {probe.target} — the tmux pane is now only a fallback")
    return {"key": str(key), "installed": state, "ssh_works": True,
            "target": probe.target, "method": "key"}


def _install_via_copy_id(remote: Remote, key: Path) -> dict:
    target = remote.ssh_alias or f"{remote.user}@{remote.host}"
    argv = ["ssh-copy-id", "-i", f"{key}.pub",
            "-o", "StrictHostKeyChecking=accept-new", target]
    if remote.password:
        if not shutil.which("sshpass"):
            raise RuntimeError(
                "UMC_SSH_PASSWORD is set but sshpass is missing.\n"
                "  brew install hudochenkov/sshpass/sshpass")
        argv = ["sshpass", "-p", remote.password] + argv
        p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    else:
        log.warn("no transport available — ssh-copy-id will prompt for a password. "
                 "Run this from an interactive terminal.")
        p = subprocess.run(argv, timeout=300)

    out = getattr(p, "stderr", "") or ""
    if p.returncode != 0 and "already exist" not in out:
        raise RuntimeError(f"ssh-copy-id failed: {out.strip()[:300] or 'see output above'}")

    t = SSHTransport.probe(remote)
    if not t:
        raise RuntimeError("key installed but ssh still will not authenticate")
    log.ok("key auth working")
    return {"key": str(key), "target": t.target, "method": "key", "ssh_works": True}
