"""Preflight. Every check here exists because that thing actually broke once.

Run this before a long job. A 7-minute export that fails on a missing preset
is a worse outcome than a 20-second check that says so up front.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request

from . import log, session
from .config import Config
from .transport import SSHTransport, TmuxTransport, Transport, connect

# macOS TCC blocks these for an ssh session; anything the backend must read
# has to live outside them.
TCC_DIRS = ["~/Downloads", "~/Documents", "~/Desktop"]

# kept for backwards compatibility; the real value comes from Config.remote
PRESET_1080P50 = ("/Applications/Adobe Premiere Pro 2026/Adobe Premiere Pro 2026.app/"
                  "Contents/Settings/EncoderPresets/ConsolidateAndTranscode/"
                  "AVC-Intra Class100 1080 50p.epr")


class Check:
    def __init__(self, name, ok, detail="", fix=""):
        self.name, self.ok, self.detail, self.fix = name, ok, detail, fix

    def as_dict(self):
        return {"check": self.name, "ok": self.ok, "detail": self.detail, "fix": self.fix}


def json2video_auth(env: dict, timeout: int = 20) -> tuple:
    """Does the key actually authenticate? Returns (ok, detail).

    A key being *present* proves nothing — a rejected key looks exactly like a
    set key until the first render, which is the point where it has already
    cost a wait. So this posts a movie with no scenes: the API rejects the
    payload either way, and only an auth failure says so in the message.

    (The endpoint + header pair is spelled out here rather than reused from
    voice.py/cards.py, which each carry their own copy. Three copies is one too
    many; worth collapsing when something next touches all three.)
    """
    key = env.get("JSON2VIDEO_API_KEY")
    if not key:
        return False, "no key"
    url = env.get("JSON2VIDEO_ENDPOINT") or "https://api.json2video.com/v2/movies"
    req = urllib.request.Request(
        url, data=json.dumps({"resolution": "full-hd", "scenes": []}).encode(),
        method="POST",
        headers={"x-api-key": key, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = (r.read() or b"{}").decode(errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
    except Exception as e:                       # DNS, TLS, timeout, offline
        return False, f"unreachable: {str(e)[:80]}"
    if "invalid api key" in body.lower():
        return False, "key rejected"
    return True, "key accepted"


def _local(cfg: Config) -> list:
    out = []
    for tool, fix in (("tmux", "brew install tmux"),
                      ("ffmpeg", "brew install ffmpeg"),
                      ("ssh", "part of macOS"),
                      ("python3", "brew install python")):
        p = shutil.which(tool)
        out.append(Check(f"local {tool}", bool(p), p or "not found", "" if p else fix))

    for key in ("JSON2VIDEO_API_KEY",):
        v = cfg.optional(key)
        out.append(Check(f"env {key}", bool(v),
                         "set" if v else "missing",
                         "" if v else f"add {key} to .env (see .env.example)"))
        if v:
            ok, detail = json2video_auth(cfg.env)
            out.append(Check("json2video auth", ok, detail, "" if ok else
                             "check the key at json2video.com, or the endpoint "
                             "in JSON2VIDEO_ENDPOINT"))

    ingest_csv = cfg.optional("UMC_INGEST_CSV_URL")
    if ingest_csv:
        from . import ingest as ingest_mod
        try:
            text = ingest_mod.fetch_csv(ingest_csv, timeout=10)
            ok = bool(ingest_mod.parse_responses(text)["responses"])
            out.append(Check(
                "ingest CSV", ok,
                "reachable" if ok else "reachable but no response rows",
                "" if ok else "check the URL or re-share the sheet"))
        except Exception as e:
            out.append(Check(
                "ingest CSV", False, str(e)[:120],
                "fix the URL, or share the sheet with 'anyone with link'"))
    else:
        out.append(Check("ingest CSV", True, "not configured (optional)", ""))
    nb = cfg.optional("UMC_INGEST_NOTEBOOK_URL")
    out.append(Check("ingest notebook", True,
                     nb if nb else "not configured (optional)", ""))
    return out


def _transport(cfg: Config) -> tuple:
    """Report how we reach the remote. Either route is fine; only having
    NEITHER is a failure, so ssh-vs-tmux is reported, not graded."""
    checks, t = [], None
    ssh = SSHTransport.probe(cfg.remote)
    if ssh:
        t = ssh
    else:
        pane = TmuxTransport.find_pane(cfg.remote)
        checks.append(Check("tmux ssh pane", bool(pane), pane or "none found",
                            "" if pane else "run `umcares session` then log in on the right pane"))
        if pane:
            tm = TmuxTransport(pane)
            busy = tm.busy()
            checks.append(Check("remote pane idle", not busy,
                                "idle" if busy is False else "a command is running",
                                "" if not busy else "wait for it, or Ctrl-C in that pane"))
            if not busy:
                t = tm

    checks.insert(0, Check(
        "transport", bool(t),
        (f"ssh ({t.target})" if getattr(t, "target", None)
         else f"tmux pane {t.pane}" if t else "none available"),
        "" if t else ("run `umcares session`, log in on the remote pane, "
                      "or add an ssh key")))
    if not ssh:
        log.debug("no ssh key login; using the tmux pane (slower, still verified)")
    return checks, t


def _remote(t: Transport, cfg: Config) -> list:
    out = []
    r = t.run("hostname; sw_vers -productVersion 2>/dev/null | head -1", timeout=60)
    out.append(Check("remote reachable", r.ok, r.stdout.strip().replace("\n", " ")))
    if not r.ok:
        return out

    for tool in ("ffmpeg", "ffprobe", "node", "python3"):
        rr = t.run(f"command -v {tool} >/dev/null && echo yes || echo no", timeout=45)
        good = rr.stdout.strip().endswith("yes")
        out.append(Check(f"remote {tool}", good, "present" if good else "missing",
                         "" if good else f"brew install {tool}"))

    rr = t.run("pgrep -f 'MacOS/Adobe Premiere Pro' >/dev/null && echo yes || echo no", timeout=45)
    running = rr.stdout.strip().endswith("yes")
    out.append(Check("Premiere running", running, "yes" if running else "not running",
                     "" if running else "launch Premiere Pro on the remote machine"))

    rr = t.run(f"curl -s -m 5 http://127.0.0.1:{cfg.remote.cdp_port}/json/list "
               f"| head -c 200 || echo FAIL", timeout=60)
    cdp = "webSocketDebuggerUrl" in rr.stdout or "devtools" in rr.stdout
    out.append(Check("CEP debug port", cdp,
                     f"port {cfg.remote.cdp_port} responding" if cdp else "no CDP endpoint",
                     "" if cdp else "copy .debug into the panel's dist/ and restart Premiere"))

    preset = cfg.remote.preset_path
    rr = t.run(f'test -f "{preset}" && echo yes || echo no', timeout=45)
    have = rr.stdout.strip().endswith("yes")
    out.append(Check("export preset", have,
                     preset.rsplit("/", 1)[-1] if have else f"not found: {preset}",
                     "" if have else "pick another .epr under Contents/Settings/EncoderPresets"))

    rr = t.run(f"test -d {cfg.remote.root} && echo yes || echo no", timeout=45)
    have_root = rr.stdout.strip().endswith("yes")
    out.append(Check("project root", have_root, cfg.remote.root,
                     "" if have_root else f"mkdir -p {cfg.remote.root}"))

    rr = t.run("df -g ~ | tail -1", timeout=45)
    free_gb = None
    parts = rr.stdout.split()
    if len(parts) >= 4:
        try:
            free_gb = int(parts[3])
        except ValueError:
            free_gb = None
    enough = free_gb is None or free_gb >= 20
    out.append(Check("disk space", enough,
                     f"{free_gb} GB free" if free_gb is not None else "unknown",
                     "an AVC-Intra master runs several GB" if not enough else ""))

    blocked = []
    for d in TCC_DIRS:
        rr = t.run(f"ls {d} >/dev/null 2>&1 && echo ok || echo blocked", timeout=45)
        if rr.stdout.strip().endswith("blocked"):
            blocked.append(d)
    # Informational: an ssh session is always blocked from these on macOS.
    # It only matters if media is stored there, which the pipeline avoids.
    root_ok = have_root
    out.append(Check("TCC file access", True,
                     (f"blocked (expected): {', '.join(blocked)} — "
                      f"media lives under {cfg.remote.root}")
                     if blocked else "no blocked dirs",
                     ""))
    if blocked and not root_ok:
        out.append(Check("media location", False,
                         "project root missing AND home dirs are TCC-blocked",
                         f"mkdir -p {cfg.remote.root} and keep all media there"))
    return out


def run(cfg: Config, deep: bool = True) -> dict:
    checks = _local(cfg)

    st = session.status(cfg.remote)
    checks.append(Check("tmux session", st.get("exists", False),
                        f"{st.get('session')}: {len(st.get('panes', []))} panes"
                        if st.get("exists") else "not created",
                        "" if st.get("exists") else "umcares session"))

    tchecks, t = _transport(cfg)
    checks += tchecks

    if t and deep:
        checks += _remote(t, cfg)
    elif not t:
        checks.append(Check("remote checks", False, "skipped — no transport"))

    failed = [c for c in checks if not c.ok]
    for c in checks:
        (log.ok if c.ok else log.err)(f"{c.name}: {c.detail}")
        if not c.ok and c.fix:
            log.warn(f"    fix: {c.fix}")

    return {
        "ok": not failed,
        "passed": len(checks) - len(failed),
        "total": len(checks),
        "failures": [c.as_dict() for c in failed],
    }
