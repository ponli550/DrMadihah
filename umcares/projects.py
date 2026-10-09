"""Projects: one folder per event or video, each with its own git repo, .gitignore and README.

A project is a folder, nothing more: the standard tree (see layout.py), a project.json, and
a git repo that versions the small things (recipes, notes, logos) and ignores the media.
There is no database; the list of projects is whatever is on disk, plus a registry of
folders that live elsewhere (the original DrMadihah folder is registered, never moved).

Creation is all-or-nothing. The project is built in a hidden sibling folder and renamed
into place only when complete, so a failure leaves nothing half-made — the same rule as
safe.py, for the same reason.

Stdlib only: this runs on the Premiere machine, where the web app creates projects.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path

from .layout import TREE

DEFAULT_HOME = Path.home() / "Projects" / "umcares-projects"
PROJECT_FILE = "project.json"
REGISTRY_FILE = "registry.json"
RESERVED = {"registry", "trash"}
EXTRA_DIRS = [("recipes", "recipe JSON files: scenes, narration, cards, music")]
STATUSES = ("active", "delivered", "archived")

# Media stays on disk, out of git: it is large, regenerable, and not the history worth keeping.
GITIGNORE = """\
# Media stays on disk, not in git. Recipes, notes and small brand sources are versioned.
assets/footage/
assets/photos/
assets/edit_ready/
assets/music/
assets/vo/
assets/cards/
exports/
project/
.trash/
*.part
*.part.*
.DS_Store
"""


class ProjectError(Exception):
    """A request that cannot be honoured; the message is meant to be shown to the person."""


def slugify(name: str) -> str:
    """Lowercase ASCII with hyphens: safe as a folder name and as a URL segment."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:40].strip("-")
    if not slug:
        raise ProjectError("the name needs at least one letter or digit")
    if slug in RESERVED:
        raise ProjectError(f"'{slug}' is reserved; pick another name")
    return slug


def _check_date(event_date: str) -> str:
    if event_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", event_date):
        raise ProjectError("the event date must look like 2026-10-09")
    return event_date


def readme_text(meta: dict) -> str:
    rows = "\n".join(f"| `{rel}/` | {purpose} |" for rel, purpose in TREE + EXTRA_DIRS)
    return f"""# {meta['name']}

{meta['notes'] or '_No notes yet._'}

| | |
|---|---|
| Event date | {meta['event_date'] or 'not set'} |
| Created | {meta['created']} |
| Status | {meta['status']} |

## Layout

| Folder | What goes there |
|---|---|
{rows}

## What is in git

Recipes, notes and the small brand sources under `assets/logos/`. **Media is not in
git**: footage, photos, voiceover, cards, exports and the Premiere project stay on
disk (see `.gitignore`), so this repo is small and a clone does not contain them.
Back media up separately.
"""


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("git") or "/usr/bin/git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run([exe, *args], cwd=path, capture_output=True, text=True, timeout=60, env=env)


def _init_git(path: Path, message: str) -> dict:
    """git init + first commit. A failure here never fails project creation; it is reported."""
    try:
        if _git(path, "init", "-q", "-b", "main").returncode != 0:
            return {"initialised": False, "committed": False, "note": "git init failed"}
        _git(path, "add", "-A")
        identity = []
        if not _git(path, "config", "user.name").stdout.strip() or not _git(path, "config", "user.email").stdout.strip():
            # a fresh machine has no identity and `git commit` would refuse; borrow one for this commit only
            identity = ["-c", "user.name=umcares", "-c", "user.email=umcares@localhost"]
        r = _git(path, *identity, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
        if r.returncode != 0:
            return {"initialised": True, "committed": False, "note": r.stderr.strip()[:200] or "commit failed"}
        return {"initialised": True, "committed": True, "note": ""}
    except (OSError, subprocess.SubprocessError) as e:
        return {"initialised": False, "committed": False, "note": f"git unavailable: {e}"}


def _write_tree(root: Path, meta: dict) -> None:
    for rel, purpose in TREE + EXTRA_DIRS:
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / ".what-goes-here").write_text(purpose + "\n", encoding="utf-8")
    (root / PROJECT_FILE).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / "README.md").write_text(readme_text(meta), encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def create_project(home, name: str, event_date: str = "", notes: str = "", init_git: bool = True) -> dict:
    """Make <home>/<slug>/ with the tree, project.json, README, .gitignore and a git repo."""
    name = " ".join(name.split())
    if not name:
        raise ProjectError("the project needs a name")
    slug = slugify(name)
    event_date = _check_date(event_date)
    home = Path(home)
    final = home / slug
    if final.exists() or any(p["slug"] == slug for p in list_projects(home)):
        raise ProjectError(f"a project called '{slug}' already exists")
    meta = {"slug": slug, "name": name, "event_date": event_date, "notes": notes.strip(),
            "status": "active", "created": time.strftime("%Y-%m-%d")}
    home.mkdir(parents=True, exist_ok=True)
    building = home / f".{slug}.part"
    shutil.rmtree(building, ignore_errors=True)          # debris from an earlier crash, never a real project
    try:
        _write_tree(building, meta)
        git = _init_git(building, f"project: create {name}") if init_git else \
            {"initialised": False, "committed": False, "note": "skipped"}
        os.rename(building, final)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return {**meta, "path": str(final), "git": git}


def _read_registry(home: Path) -> list:
    try:
        return json.loads((home / REGISTRY_FILE).read_text(encoding="utf-8")).get("projects", [])
    except (OSError, ValueError):
        return []


def register_external(home, slug: str, name: str, path, event_date: str = "") -> dict:
    """Add a folder that already exists somewhere else. It is recorded, not moved or modified."""
    home = Path(home)
    slug = slugify(slug)
    path = Path(path)
    if not path.is_dir():
        raise ProjectError(f"{path} is not a folder")
    entries = [e for e in _read_registry(home) if e.get("slug") != slug]
    entry = {"slug": slug, "name": name, "path": str(path), "event_date": _check_date(event_date), "status": "active"}
    entries.append(entry)
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / (REGISTRY_FILE + ".part")
    tmp.write_text(json.dumps({"projects": entries}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, home / REGISTRY_FILE)
    return entry


def list_projects(home) -> list:
    """Projects on disk under home, plus registered external ones; newest event first."""
    home = Path(home)
    out = []
    if home.is_dir():
        for d in sorted(home.iterdir()):
            f = d / PROJECT_FILE
            if d.name.startswith(".") or not f.is_file():
                continue
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({**meta, "slug": d.name, "path": str(d), "external": False, "missing": False})
    seen = {p["slug"] for p in out}
    for e in _read_registry(home):
        if e.get("slug") and e["slug"] not in seen:
            out.append({**e, "external": True, "missing": not Path(e.get("path", "")).is_dir()})
    return sorted(out, key=lambda p: (p.get("event_date") or "", p["name"].lower()), reverse=True)


def find_project(home, slug: str):
    return next((p for p in list_projects(home) if p["slug"] == slug), None)


def _main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="umcares.projects")
    ap.add_argument("--home", default=os.environ.get("UMC_PROJECTS_HOME", str(DEFAULT_HOME)))
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("name")
    n.add_argument("--date", default="")
    n.add_argument("--notes", default="")
    n.add_argument("--no-git", action="store_true")
    sub.add_parser("list")
    r = sub.add_parser("register", help="record an existing folder without moving it")
    r.add_argument("slug")
    r.add_argument("name")
    r.add_argument("path")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "new":
            res = create_project(a.home, a.name, a.date, a.notes, not a.no_git)
        elif a.cmd == "register":
            res = register_external(a.home, a.slug, a.name, a.path)
        else:
            res = list_projects(a.home)
    except ProjectError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
