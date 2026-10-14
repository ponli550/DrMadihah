"""Which project a command acts on, and the rule that stops a command acting on the wrong one.

The pipeline commands (`premiere build`, `media prepare`, `post mix`, ...) act on one remote project folder,
`UMC_REMOTE_ROOT`. Now that the web app can create more projects on the Premiere machine, a build run without saying
which project could clear the timeline of a finished edit in the wrong one. So:

  * `--project SLUG` (before the command) points the command at that project's folder on the remote.
  * A command that WRITES or BUILDS, run without `--project`, is refused as soon as more than one project exists.
    With a single project (or none registered) nothing changes, so the old workflow keeps working.
  * Read-only commands (`premiere status`, `media probe`, `inspect`, `doctor`, ...) never need it.

The list of projects is read from the remote (the web app's own project code runs there). If it cannot be read, a command
without `--project` goes ahead with a warning, because refusing every build on a machine where the web app was never
deployed would break the single-project workflow; `--project` itself needs the list and says so when it is missing.
"""
from __future__ import annotations

import json
import shlex

# (command, action) pairs that write or build. None as the action means every action of that command.
WRITES = {
    "premiere": {"build", "import", "sequence", "open", "export"},
    "media": {"prepare", "kenburns", "denoise", "extract-audio"},
    "post": {"mix", "export", "import"},
    "render": None,
    "init": None,
    "ingest": None,
}
LIST_HOME = '"$HOME/Projects/umcares-projects"'


class ScopeError(Exception):
    """The command is refused or the project is unknown; the message says what to do."""


def writes(cmd: str, action) -> bool:
    if cmd not in WRITES:
        return False
    actions = WRITES[cmd]
    return True if actions is None else action in actions


def remote_projects(t, root: str):
    """The projects on the remote ([{"slug", "name", "path", "missing", ...}]), or None when they cannot be listed."""
    app = shlex.quote(root.rstrip("/") + "/web/app")
    try:
        r = t.run(f"cd {app} && python3 -m umcares.projects --home {LIST_HOME} list", timeout=60)
    except Exception:
        return None
    if not r.ok:
        return None
    try:
        data = json.loads(r.stdout[r.stdout.index("["):])
    except (ValueError, TypeError):
        return None
    return [p for p in data if isinstance(p, dict) and p.get("slug")] if isinstance(data, list) else None


def _names(projects) -> str:
    return ", ".join(sorted(p["slug"] for p in projects))


def choose(projects, slug, is_write: bool):
    """Pure decision. Returns the project's folder to use, or None to keep the configured one; raises ScopeError."""
    if slug:
        if projects is None:
            raise ScopeError("could not list the projects on the remote (is the web app deployed there? try: umcares web deploy)")
        found = next((p for p in projects if p["slug"] == slug), None)
        if not found:
            raise ScopeError(f"no project '{slug}' on the remote (have: {_names(projects) or 'none'})")
        if found.get("missing") or not found.get("path"):
            raise ScopeError(f"project '{slug}' is registered but its folder is missing: {found.get('path') or '(no path)'}")
        return found["path"]
    if is_write and projects and len(projects) > 1:
        raise ScopeError(f"{len(projects)} projects exist on the remote ({_names(projects)}): say which one with "
                         f"--project <slug>, e.g.  umcares --project {sorted(p['slug'] for p in projects)[0]} ...")
    return None


def apply(args, cfg, connect, warn=lambda m: None):
    """Point cfg at the project the command names, or refuse. `connect` opens a transport (called only when needed)."""
    slug = getattr(args, "project_slug", None)
    is_write = writes(getattr(args, "cmd", ""), getattr(args, "action", None))
    if not slug and not is_write:
        return cfg
    projects = remote_projects(connect(), cfg.remote.root)
    if projects is None and not slug:
        warn(f"could not check the remote for other projects; continuing with {cfg.remote.root}")
        return cfg
    path = choose(projects, slug, is_write)
    if path:
        cfg.remote.root = path
    return cfg
