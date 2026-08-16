"""Project folder layout, created identically on both machines.

One tree, two locations:

  * **remote** — where Premiere and all the heavy media live. This is the one
    that matters; Premiere imports from here.
  * **local**  — scripts, plans, subtitles and small artefacts. Deliberately
    does NOT hold source media: 281 MB of footage has no business in a git
    working copy, and .gitignore already excludes it.

Everything sits under the project root and never under ~/Downloads,
~/Documents or ~/Desktop — macOS TCC blocks an ssh session from reading those,
so media stored there is invisible to the pipeline (and fails in confusing
ways much later).
"""
from __future__ import annotations

import shlex
from pathlib import Path

from . import log
from .transport import Transport

# (path, purpose) — purpose doubles as the README line written into the tree
TREE = [
    ("assets",              "all source and generated media"),
    ("assets/footage",      "camera video, split by role"),
    ("assets/footage/A",    "A-roll: speakers, interviews, primary action"),
    ("assets/footage/B",    "B-roll: cutaways, atmosphere, filler"),
    ("assets/photos",       "source stills"),
    ("assets/edit_ready",   "H.264 transcodes — the ONLY folder Premiere imports from"),
    ("assets/logos",        "brand marks (source, committed)"),
    ("assets/music",        "licensed music beds"),
    ("assets/vo",           "voiceover renders"),
    ("assets/cards",        "generated title / statistic / logo cards"),
    ("presets",             "copies of the .epr and .sqpreset actually used"),
    ("subtitles",           "SRT files"),
    ("exports",             "masters and delivery files"),
    ("project",             "the .prproj"),
]

# Local gets the light half only — no source media in the repo.
LOCAL_ONLY = {
    "assets", "assets/logos", "assets/cards", "assets/vo",
    "presets", "subtitles", "exports", "project",
}


def plan(remote_root: str, local_root: Path) -> dict:
    return {
        "remote": [f"{remote_root}/{p}" for p, _ in TREE],
        "local": [str(local_root / p) for p, _ in TREE if p in LOCAL_ONLY],
    }


def create_local(local_root: Path, write_readme: bool = True) -> list:
    made = []
    for rel, purpose in TREE:
        if rel not in LOCAL_ONLY:
            continue
        d = local_root / rel
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            made.append(str(d))
        if write_readme:
            marker = d / ".what-goes-here"
            if not marker.exists():
                marker.write_text(purpose + "\n", encoding="utf-8")
    return made


def create_remote(t: Transport, remote_root: str, write_readme: bool = True) -> dict:
    """Create the tree remotely and report what was new."""
    paths = " ".join(shlex.quote(f"{remote_root}/{p}") for p, _ in TREE)
    readme_lines = "\n".join(
        f'printf %s\\\\n {shlex.quote(purpose)} > {shlex.quote(f"{remote_root}/{p}/.what-goes-here")}'
        for p, purpose in TREE
    ) if write_readme else ""

    script = f"""
set -e
ROOT={shlex.quote(remote_root)}
before=$(find "$ROOT" -type d 2>/dev/null | wc -l | tr -d ' ')
mkdir -p {paths}
{readme_lines}
after=$(find "$ROOT" -type d 2>/dev/null | wc -l | tr -d ' ')
echo "---TREE---"
find "$ROOT" -maxdepth 2 -type d 2>/dev/null | sed "s|^$ROOT|.|" | sort
echo "---COUNT--- $before $after"
df -g "$ROOT" | tail -1 | awk '{{print "---FREE--- " $4}}'
"""
    r = t.run_script(script, timeout=300)
    r.check("create remote tree")

    dirs, before, after, free = [], 0, 0, None
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith("---COUNT---"):
            parts = s.split()
            before, after = int(parts[1]), int(parts[2])
        elif s.startswith("---FREE---"):
            free = s.split()[-1] + " GB"
        elif s.startswith("."):
            dirs.append(s)
    return {"root": remote_root, "dirs": dirs,
            "created": max(0, after - before), "free": free}


def verify(t: Transport, remote_root: str) -> dict:
    """Check the tree exists and report which folders actually hold anything."""
    script = f"""
ROOT={shlex.quote(remote_root)}
for d in {" ".join(shlex.quote(p) for p, _ in TREE)}; do
  full="$ROOT/$d"
  if [ -d "$full" ]; then
    n=$(find "$full" -maxdepth 1 -type f ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    echo "OK|$d|$n"
  else
    echo "MISSING|$d|0"
  fi
done
"""
    r = t.run_script(script, timeout=300)
    rows, missing = [], []
    for line in r.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            state, path, n = parts
            rows.append({"path": path, "exists": state == "OK", "files": int(n)})
            if state != "OK":
                missing.append(path)
    return {"root": remote_root, "folders": rows, "missing": missing,
            "ok": not missing}
