"""Read-only views of the umcares web app, for the terminal.

One code path: the CLI commands (`umcares projects | jobs | show | plan`), the panvim boards and anything else that wants to look at umcares use
these functions, so what a board shows is what the command says. Everything goes through the web app's HTTP API (the same one the page and
`umcares mcp` use): no ssh, no billing, no credentials.

What is deliberately left out: captions and project notes. They are text people wrote about other people, a board stays on screen through a
screen share, and a count or a name is enough to know where to look. (`umcares mcp` has them, for a model, cleaned and marked as data.)
"""
from __future__ import annotations

import re

from . import draft
from .montage import clean_line

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class ViewError(Exception):
    """Something the person can act on, in words: the web app is unreachable, the project or recipe is not there."""


def estimate_render(plan: dict, quality: str | None) -> dict:
    """A rough guide to how long a render takes, from the plan alone. The multiples come from timings on the Air (8 GB): cards about 0.9x the video's
    length, still pictures about 0.4x, real photos with camera moves up to about 2x; riso about 2.4x and neon 2.5 to 5x a plain picture render."""
    kinds = {sg.get("kind") for sg in plan.get("segments", [])}
    lo, hi = (0.4, 2.0) if kinds & {"photo", "video"} else (0.8, 1.3)
    f = {"neon": (2.5, 5.0), "riso": (1.2, 2.4), "glitch": (1.2, 1.4), "dither": (1.0, 1.1)}.get(plan.get("effect"), (1.0, 1.0))
    lo, hi = lo * f[0], hi * f[1]
    if quality == "draft":
        lo, hi = lo / 2, hi / 2
    total = float(plan.get("total", 0))
    return {"video_seconds": round(total, 1), "render_seconds_roughly": [max(1, round(total * lo)), max(2, round(total * hi))],
            "note": "a rough guide measured on the Air; the real time is shown by get_jobs"}


def _get(site, path: str):
    try:
        code, d = site.call("GET", path)
    except draft.DraftError as e:
        raise ViewError(str(e)) from None
    if code == 404:
        raise ViewError("not found on the web app")
    if code != 200 or not isinstance(d, dict):
        raise ViewError(f"the web app answered {code}")
    return d


def _slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ViewError("a project name is the folder name: lower case letters, digits and dashes (umcares projects lists them)")
    return slug


def list_projects(site) -> list:
    d = _get(site, "/api/projects")
    return [{"slug": clean_line(p.get("slug"), 64), "name": clean_line(p.get("name"), 80), "status": clean_line(p.get("status"), 20),
             "event_date": clean_line(p.get("event_date"), 20), "missing": bool(p.get("missing"))} for p in d.get("projects", [])]


def _state(site, slug: str) -> dict:
    try:
        return _get(site, f"/p/{_slug(slug)}/api/montage")
    except ViewError as e:
        if "not found" in str(e):
            raise ViewError(f"no project '{slug}' on the web app (umcares projects lists them)") from None
        raise


def overview(site, slug: str) -> dict:
    """What a project holds, as counts and names. No captions."""
    d = _state(site, slug)
    folders: dict = {}
    for i in d.get("catalog", []):
        f = clean_line(i.get("folder"), 60) or "-"
        row = folders.setdefault(f, {"images": 0, "videos": 0, "audio": 0})
        k = {"image": "images", "video": "videos", "audio": "audio"}.get(i.get("kind"))
        if k:
            row[k] += 1
    jobs = d.get("jobs", [])
    return {"project": slug, "folders": folders,
            "recipes": [{"name": clean_line(r.get("name"), 100), "valid": bool(r.get("valid")), "problems": len(r.get("problems") or [])} for r in d.get("recipes", [])],
            "exports": [{k: (clean_line(e[k], 100) if isinstance(e[k], str) else e[k]) for k in ("name", "template", "format", "duration", "quality") if k in e}
                        for e in d.get("exports", [])],
            "rendering": bool(d.get("active")), "jobs_running": sum(j.get("state") == "running" for j in jobs), "jobs_waiting": sum(j.get("state") == "queued" for j in jobs)}


def _job_row(slug: str, j: dict) -> dict:
    return {"project": slug, "id": clean_line(j.get("id"), 40), "state": clean_line(j.get("state"), 16), "percent": j.get("percent") if isinstance(j.get("percent"), (int, float)) else 0,
            "title": clean_line(j.get("title"), 60), "template": clean_line(j.get("template"), 40), "format": clean_line(j.get("format"), 20),
            "quality": clean_line(j.get("quality"), 12), "total": j.get("total"), "output": clean_line(j.get("output"), 120), "error": clean_line(j.get("error"), 160)}


ORDER = {"running": 0, "queued": 1, "failed": 2, "done": 3}


def jobs(site, slug: str | None = None) -> list:
    """Render jobs across the projects (or one), running first, then waiting, failed, done; newest last within each."""
    slugs = [_slug(slug)] if slug else [p["slug"] for p in list_projects(site) if not p["missing"]]
    rows = []
    for s in slugs:
        rows += [_job_row(s, j) for j in _state(site, s).get("jobs", [])]
    return sorted(rows, key=lambda r: ORDER.get(r["state"], 9))


def saved_plan(site, slug: str, name: str) -> dict:
    """The plan of a saved recipe, or its problems: {"recipe", "valid", "plan", "estimate"} / {"recipe", "valid": False, "problems"}."""
    d = _state(site, slug)
    saved = {r.get("name"): r for r in d.get("recipes", [])}
    if name not in saved:
        raise ViewError(f"no saved recipe '{clean_line(name, 60)}' in {slug}; it has: " + (", ".join(clean_line(n, 60) for n in saved) or "none"))
    r = saved[name]
    if not r.get("valid") or not r.get("recipe"):
        return {"recipe": name, "valid": False, "problems": [clean_line(p, 200) for p in (r.get("problems") or ["it no longer works"])]}
    try:
        code, res = site.call("POST", f"/p/{slug}/api/montage", {"recipe": r["recipe"], "dry_run": True})
    except draft.DraftError as e:
        raise ViewError(str(e)) from None
    if code != 200:
        return {"recipe": name, "valid": False, "problems": [clean_line(p, 200) for p in (res.get("problems") or [res.get("error", f"HTTP {code}")])]}
    plan = res["plan"]
    return {"recipe": name, "valid": True, "plan": plan, "estimate": estimate_render(plan, r["recipe"].get("quality"))}


# ------------------------------------------------------------------ plain text for a terminal or a panvim board (no colour codes)
def bar(percent, width: int = 20) -> str:
    p = max(0, min(100, int(percent or 0)))
    full = round(width * p / 100)
    return "█" * full + "░" * (width - full) + f" {p:>3}%"


def format_projects(rows: list) -> str:
    if not rows:
        return "no projects yet\n"
    w = max(len(r["slug"]) for r in rows)
    return "".join(f"  {r['slug']:<{w}}  {r['name']}" + (f"  [{r['status']}]" if r["status"] else "") + ("  (folder missing)" if r["missing"] else "") + "\n" for r in rows)


def format_overview(o: dict) -> str:
    out = [f"{o['project']}:"]
    out.append("  files:")
    if o["folders"]:
        w = max(len(f) for f in o["folders"])
        out += [f"    {f:<{w}}  {c['images']} pictures  {c['videos']} clips" + (f"  {c['audio']} audio" if c["audio"] else "") for f, c in sorted(o["folders"].items())]
    else:
        out.append("    none yet")
    out.append("  saved recipes:")
    out += [f"    {'✓' if r['valid'] else '✗'} {r['name']}" + ("" if r["valid"] else f"  ({r['problems']} problem{'s' if r['problems'] != 1 else ''})") for r in o["recipes"]] or ["    none"]
    out.append("  finished videos (newest first):")
    out += [f"    {e.get('name', '?')}" + (f"  {e['duration']:.0f}s" if isinstance(e.get("duration"), (int, float)) else "") + (f"  {e['quality']}" if e.get("quality") else "")
            for e in o["exports"][:10]] or ["    none yet"]
    out.append(f"  rendering: {o['jobs_running']} running, {o['jobs_waiting']} waiting")
    return "\n".join(out) + "\n"


def format_jobs(rows: list) -> str:
    if not rows:
        return "no render jobs\n"
    out = []
    for r in rows:
        line = f"  {r['state']:<8} {bar(r['percent'], 12) if r['state'] in ('running', 'queued') else '':<18} {r['project']}  {r['title'] or '(untitled)'}"
        if r["template"]:
            line += f"  {r['template']}"
        if r["quality"] and r["quality"] != "final":
            line += f"  {r['quality']}"
        if isinstance(r["total"], (int, float)):
            line += f"  {r['total']:.0f}s"
        out.append(line)
        if r["state"] == "failed" and r["error"]:
            out.append(f"           ! {r['error']}")
        if r["state"] == "done" and r["output"]:
            out.append(f"           → {r['output']}")
    return "\n".join(out) + "\n"


def format_plan(p: dict) -> str:
    if not p["valid"]:
        return f"{p['recipe']}: does not work\n" + "".join(f"  ✗ {x}\n" for x in p["problems"])
    plan, est = p["plan"], p["estimate"]
    out = [f"{p['recipe']}: {plan.get('title') or ''}".rstrip(": ") + f"  ({plan['total']:.1f} s, {plan['format']}, {plan['template']})"]
    for k, sg in enumerate(plan.get("segments", []), 1):
        label = sg.get("name") or sg.get("text") or ""
        out.append(f"  {k:>2}. {str(sg.get('kind', '')):<10} {sg.get('start', 0):>6.1f}s  {sg.get('duration', 0):>5.1f}s  {clean_line(label, 40)}")
    extras = [f"{k}: {plan[k]}" for k in ("transition", "join", "effect", "background", "depth") if plan.get(k)]
    if extras:
        out.append("  " + "  ".join(extras))
    for w in plan.get("warnings") or []:
        out.append(f"  ! {clean_line(w, 160)}")
    lo, hi = est["render_seconds_roughly"]
    out.append(f"  rendering takes roughly {lo} to {hi} s on the Air (a rough guide)")
    return "\n".join(out) + "\n"
