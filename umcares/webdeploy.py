"""Put the web app on the Premiere machine and keep it running there.

What this exists to prevent: starting the server by hand over ssh leaves a stray instance holding
the port; the next restart then fails with "Address already in use" while the old server keeps
answering, which looks like a flaky proxy. So the app runs only as a LaunchAgent, and a deploy is:
push the package files, check they arrived byte-for-byte, rewrite the plist only if it changed,
let launchd (re)start it, and wait until it actually answers.

It never publishes to the tailnet by itself; `tailscale serve` is reported, not run.
"""
from __future__ import annotations

import hashlib
import shlex
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import log, projects
from .web import BASE, DEFAULT_PORT

LABEL = "com.umcares.web"
PACKAGE_FILES = ["__init__.py", "layout.py", "projects.py", "uploads.py", "prepare.py", "textimg.py", "beats.py", "speech.py", "collage.py", "montage.py", "captions.py", "webpages.py", "web.py"]
PACKAGE_DIR = Path(__file__).resolve().parent


def plist_text(python: str, app: str, home: str, owner: str, port: int, logfile: str) -> str:
    args = [python, "-m", "umcares.web", "--home", home, "--port", str(port)]
    items = "".join(f"<string>{escape(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{items}</array>
  <key>WorkingDirectory</key><string>{escape(app)}</string>
  <key>EnvironmentVariables</key><dict><key>UMC_WEB_OWNER</key><string>{escape(owner)}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{escape(logfile)}</string>
  <key>StandardOutPath</key><string>{escape(logfile)}</string>
</dict></plist>
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deploy(t, remote_root: str, owner: str = "", port: int = DEFAULT_PORT, legacy_name: str = "") -> dict:
    """Deploy to the machine behind transport `t`. Returns a report; raises RuntimeError on a real failure."""
    root = remote_root.rstrip("/")
    app = f"{root}/web/app"
    logfile = f"{root}/web/web.log"

    facts = t.run("echo $HOME; command -v python3; id -u", timeout=60).check("read the remote environment")
    home_dir, python, uid = (facts.stdout.split() + ["", "", ""])[:3]
    if not (home_dir and python and uid):
        raise RuntimeError(f"could not read HOME, python3 and uid on the remote: {facts.stdout!r}")
    projects_home = f"{home_dir}/Projects/umcares-projects"

    if not owner:
        found = t.run("/usr/local/bin/tailscale status --json 2>/dev/null | python3 -c "
                      "\"import sys,json;d=json.load(sys.stdin);print(d['User'][str(d['Self']['UserID'])]['LoginName'])\"",
                      timeout=60)
        owner = found.stdout.strip() if found.ok else ""
        if not owner:
            raise RuntimeError("no owner: pass --owner or set UMC_WEB_OWNER (the Tailscale login allowed in)")
        log.info(f"owner not given; using the remote's own Tailscale login: {owner}")

    t.ensure_dir(f"{app}/umcares")
    for name in PACKAGE_FILES:
        t.push(PACKAGE_DIR / name, f"{app}/umcares/{name}")
    sums = t.run(f"cd {shlex.quote(app)}/umcares && shasum -a 256 {' '.join(PACKAGE_FILES)}", timeout=60).check("verify the push")
    remote_sums = {line.split()[1]: line.split()[0] for line in sums.stdout.splitlines() if len(line.split()) == 2}
    bad = [n for n in PACKAGE_FILES if remote_sums.get(n) != _sha(PACKAGE_DIR / n)]
    if bad:
        raise RuntimeError(f"files differ after the push: {', '.join(bad)}")

    plist_path = f"{home_dir}/Library/LaunchAgents/{LABEL}.plist"
    want = plist_text(python, app, projects_home, owner, port, logfile)
    have = t.run(f"cat {shlex.quote(plist_path)} 2>/dev/null", timeout=30)
    plist_changed = (not have.ok) or have.stdout.strip() != want.strip()
    if plist_changed:
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / f"{LABEL}.plist"
            local.write_text(want, encoding="utf-8")
            t.push(local, plist_path)

    legacy_slug = projects.slugify(Path(root).name)
    legacy = legacy_name or Path(root).name
    target = f"gui/{uid}/{LABEL}"
    script = f"""
set -u
mkdir -p {shlex.quote(projects_home)}
cd {shlex.quote(app)} && python3 -m umcares.projects --home {shlex.quote(projects_home)} register {shlex.quote(legacy_slug)} {shlex.quote(legacy)} {shlex.quote(root)} >/dev/null || echo "register-failed"
if {'true' if plist_changed else 'false'} || ! launchctl print {target} >/dev/null 2>&1; then
  launchctl bootout {target} 2>/dev/null
  launchctl bootstrap gui/{uid} {shlex.quote(plist_path)} || echo "bootstrap-failed"
else
  launchctl kickstart -k {target} || echo "kickstart-failed"
fi
code=000
for i in $(seq 1 20); do
  code=$(curl -s -m 2 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/api/projects)
  [ "$code" = 200 ] && break
  sleep 1
done
echo "http=$code"
launchctl print {target} 2>/dev/null | grep -E '^\\s+(state|pid|runs) =' | tr -d '\\t' | tr '\\n' ' '
echo
/usr/local/bin/tailscale serve status 2>/dev/null | grep -F {shlex.quote(BASE)} || echo "not-published"
"""
    out = t.run_script(script, timeout=180).stdout
    report = {"owner": owner, "home": projects_home, "plist_changed": plist_changed,
              "files": len(PACKAGE_FILES), "http": next((ln.split("=")[1] for ln in out.splitlines() if ln.startswith("http=")), "?"),
              "published": "not-published" not in out, "registered": legacy_slug}
    if "register-failed" in out:
        report["warning"] = "could not register the existing project folder"
    if any(x in out for x in ("bootstrap-failed", "kickstart-failed")) or report["http"] != "200":
        raise RuntimeError(f"the service did not come up (http={report['http']}): {out.strip()[-400:]}")
    return report
