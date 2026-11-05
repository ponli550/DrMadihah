"""The text of the umcares panvim boards: `umcares panel <view>`.

panvim shows whatever a command writes to `$PANVIM_OUT` and re-runs it on a timer while the board is open. A board's rows are the lines that start with
two spaces and a word (panvim's row pattern, Lua `^  (%S+)`): the word is what a key's `{row}` stands for. So in every view here a row is exactly
the thing you can act on, and everything else (headings, details, messages) either has no indent or is indented by four or more.

These views use only the web app's HTTP API (no ssh, no billing, no credentials) and show counts and names, never captions or project notes: a
board stays on screen through a screen share. If the web app cannot be reached a board says so in one line and tries again on its next refresh; it
never raises.

Enter on a row picks what the next board shows (the project, then the recipe). That choice is kept in a small file (`$UMCARES_PANEL_STATE`, default
`~/.local/state/umcares/panel.json`), written by `umcares panel set`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import views
from .montage import clean_line

VIEWS = ("projects", "jobs", "project", "recipes", "plan", "exports")
ROW = re.compile(r"^  (\S+)")                       # panvim's row pattern, `^  (%S+)`: two spaces, then a word


def state_path() -> Path:
    return Path(os.environ.get("UMCARES_PANEL_STATE") or Path.home() / ".local" / "state" / "umcares" / "panel.json")


def load_state() -> dict:
    try:
        d = json.loads(state_path().read_text(encoding="utf-8"))
        return {"project": d.get("project") if isinstance(d.get("project"), str) else None, "recipe": d.get("recipe") if isinstance(d.get("recipe"), str) else None}
    except (OSError, ValueError, AttributeError):
        return {"project": None, "recipe": None}


def set_state(project: str, recipe: str | None = None) -> dict:
    """Remember the chosen project (and recipe). The name is checked as a plain folder name before anything is written."""
    views._slug(project)
    state = {"project": project, "recipe": recipe}
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)
    return state


def row_id(name: str) -> str:
    """The word a row starts with: a recipe name may hold spaces, a row's word may not."""
    return clean_line(name, 100).replace(" ", "_") or "-"


def resolve_recipe(site, project: str, token: str) -> str:
    """The saved recipe a row's word stands for: its exact name, else the one whose row word it is. Refuses when none or more than one."""
    names = [r["name"] for r in views.recipes(site, project)]
    if token in names:
        return token
    hits = [n for n in names if row_id(n) == token]
    if len(hits) != 1:
        raise views.ViewError(f"no saved recipe '{clean_line(token, 60)}' in {project}" if not hits else f"'{clean_line(token, 60)}' is more than one recipe in {project}")
    return hits[0]


def _current_project(site, state: dict) -> str | None:
    if state.get("project"):
        return state["project"]
    rows = [p for p in views.list_projects(site) if not p["missing"]]
    return rows[0]["slug"] if len(rows) == 1 else None


NO_PROJECT = "no project chosen\n    open the projects board and press Enter on one\n"


def _projects(site, state):
    rows = [p for p in views.list_projects(site) if not p["missing"]]
    if not rows:
        return "projects:\nnone yet\n"
    w = max(len(p["slug"]) for p in rows)
    cur = _current_project(site, state)
    out = ["projects:"]
    for p in rows:
        try:
            o = views.overview(site, p["slug"])
            pics = sum(f["images"] for f in o["folders"].values())
            clips = sum(f["videos"] for f in o["folders"].values())
            line = f"  {p['slug']:<{w}}  {pics} pictures  {clips} clips  {len(o['recipes'])} recipes  {len(o['exports'])} videos"
            if o["jobs_running"] or o["jobs_waiting"]:
                line += f"  rendering {o['jobs_running']}+{o['jobs_waiting']}"
        except views.ViewError:
            line = f"  {p['slug']:<{w}}  (could not read)"
        out.append(line + ("  (current)" if p["slug"] == cur else ""))
    return "\n".join(out) + "\n"


def _jobs(site, state):
    rows = views.jobs(site)
    running, waiting = sum(r["state"] == "running" for r in rows), sum(r["state"] == "queued" for r in rows)
    out = [f"render jobs: {running} running, {waiting} waiting"]
    if not rows:
        return out[0] + "\nnone\n"
    for r in rows:
        line = f"  {r['project']}  {r['state']:<8}"
        if r["state"] in ("running", "queued"):
            line += " " + views.bar(r["percent"], 12)
        line += f"  {r['title'] or '(untitled)'}"
        if r["template"]:
            line += f"  {r['template']}"
        if r["quality"] and r["quality"] != "final":
            line += f"  {r['quality']}"
        if isinstance(r["total"], (int, float)):
            line += f"  {r['total']:.0f}s"
        out.append(line)
        if r["state"] == "failed" and r["error"]:
            out.append(f"        ! {r['error']}")
        if r["state"] == "done" and r["output"]:
            out.append(f"        → {r['output']}")
    return "\n".join(out) + "\n"


def _project(site, state):
    slug = _current_project(site, state)
    if not slug:
        return NO_PROJECT
    o = views.overview(site, slug)
    out = [f"{slug}:", "files:"]
    if o["folders"]:
        w = max(len(f) for f in o["folders"])
        out += [f"    {f:<{w}}  {c['images']} pictures  {c['videos']} clips" + (f"  {c['audio']} audio" if c["audio"] else "") for f, c in sorted(o["folders"].items())]
    else:
        out.append("    none yet")
    bad = sum(not r["valid"] for r in o["recipes"])
    out.append(f"saved recipes: {len(o['recipes'])}" + (f", {bad} that no longer work" if bad else ""))
    out.append(f"finished videos: {len(o['exports'])}")
    out.append(f"rendering: {o['jobs_running']} running, {o['jobs_waiting']} waiting")
    return "\n".join(out) + "\n"


def _recipes(site, state):
    slug = _current_project(site, state)
    if not slug:
        return NO_PROJECT
    rows = views.recipes(site, slug)
    if not rows:
        return f"{slug}: saved recipes:\nnone yet\n"
    out = [f"{slug}: saved recipes:"]
    for r in rows:
        out.append(f"  {row_id(r['name'])}  " + ("✓" if r["valid"] else f"✗ {r['problems']} problem{'s' if r['problems'] != 1 else ''}") + ("  (current)" if r["name"] == state.get("recipe") else ""))
    return "\n".join(out) + "\n"


def _plan(site, state):
    slug = _current_project(site, state)
    if not slug:
        return NO_PROJECT
    if not state.get("recipe"):
        return f"{slug}: no recipe chosen\n    open the recipes board and press Enter on one\n"
    p = views.saved_plan(site, slug, state["recipe"])
    if not p["valid"]:
        return f"{slug}: {clean_line(p['recipe'], 80)} does not work\n" + "".join(f"    ✗ {x}\n" for x in p["problems"])
    plan, est = p["plan"], p["estimate"]
    out = [f"{slug}: {clean_line(p['recipe'], 80)}", f"{plan['total']:.1f} s, {plan['format']}, {plan['template']}", "timeline:"]
    for k, sg in enumerate(plan.get("segments", []), 1):
        out.append(f"    {k:>2}. {str(sg.get('kind', '')):<10} {sg.get('start', 0):>6.1f}s  {sg.get('duration', 0):>5.1f}s  {clean_line(sg.get('name') or sg.get('text') or '', 40)}")
    extras = [f"{k}: {plan[k]}" for k in ("transition", "join", "effect", "background", "depth") if plan.get(k)]
    if extras:
        out.append("options:")
        out.append("    " + "  ".join(extras))
    for w in plan.get("warnings") or []:
        out.append(f"warning: {clean_line(w, 160)}")
    lo, hi = est["render_seconds_roughly"]
    out.append(f"rendering takes roughly {lo} to {hi} s on the Air (a rough guide)")
    return "\n".join(out) + "\n"


def _exports(site, state):
    slug = _current_project(site, state)
    if not slug:
        return NO_PROJECT
    rows = views.overview(site, slug)["exports"]
    if not rows:
        return f"{slug}: finished videos:\nnone yet\n"
    out = [f"{slug}: finished videos (newest first):"]
    for e in rows:
        line = f"  {row_id(e.get('name', '?'))}"
        if isinstance(e.get("duration"), (int, float)):
            line += f"  {e['duration']:.0f}s"
        for k in ("quality", "template"):
            if e.get(k):
                line += f"  {e[k]}"
        out.append(line)
    return "\n".join(out) + "\n"


_VIEWS = {"projects": _projects, "jobs": _jobs, "project": _project, "recipes": _recipes, "plan": _plan, "exports": _exports}


def render(view: str, site, state: dict | None = None) -> str:
    """The board's text. Never raises: an unreachable web app or a missing project is one plain line, and the board tries again on its next refresh."""
    try:
        return _VIEWS[view](site, state if state is not None else load_state())
    except views.ViewError as e:
        msg = clean_line(str(e), 200)
        if msg.startswith("could not reach the web app"):
            return f"the umcares web app is not reachable:\n    {msg}\nthis board tries again on its next refresh\n"
        return f"{msg}\n"
