"""The two things a person can start or stop from the terminal and the panvim boards: render a saved recipe, and stop a project's renders.

`umcares make` and `umcares stop` follow the house rule: every write is a dry run until `--commit`. The dry run says what would happen (the plan and a rough
time; the renders that would be stopped) and changes nothing on the web app. They go through the same HTTP API as the web page and `umcares mcp`, so the
render queue cap and the recipe checks are the web app's, not copied here.

(Not `umcares render`, which already exists: that is the json2video / Premiere pipeline.)
"""
from __future__ import annotations

from . import draft, views
from .montage import clean_line
from .views import ViewError

QUALITIES = ("final", "share", "draft")


def _post(site, path: str, body: dict):
    try:
        return site.call("POST", path, body)
    except draft.DraftError as e:
        raise ViewError(str(e)) from None


def _queue(state: dict) -> tuple[int, int]:
    jobs = state.get("jobs", [])
    return sum(j.get("state") == "running" for j in jobs), sum(j.get("state") == "queued" for j in jobs)


def make(site, slug: str, name: str, quality: str | None = None, commit: bool = False) -> dict:
    """Render a saved recipe. Without `commit`: its plan, a rough time and how many renders are ahead of it, and nothing is started.
    -> {"committed", "recipe", "valid", "plan", "estimate", "running", "waiting", ["job"]} or {"recipe", "valid": False, "problems"}."""
    if quality is not None and quality not in QUALITIES:
        raise ViewError("quality: final, share (a smaller file) or draft (half size, fast), or leave it out")
    state = views._state(site, slug)
    saved = {r.get("name"): r for r in state.get("recipes", [])}
    if name not in saved:
        raise ViewError(f"no saved recipe '{clean_line(name, 60)}' in {slug}; it has: " + (", ".join(clean_line(n, 60) for n in saved) or "none"))
    r = saved[name]
    if not r.get("valid") or not r.get("recipe"):
        return {"committed": False, "recipe": name, "valid": False, "problems": [clean_line(p, 200) for p in (r.get("problems") or ["it no longer works"])]}
    recipe = dict(r["recipe"])
    if quality:
        recipe["quality"] = quality
    code, res = _post(site, f"/p/{slug}/api/montage", {"recipe": recipe, "dry_run": True})
    if code != 200:
        return {"committed": False, "recipe": name, "valid": False, "problems": [clean_line(p, 200) for p in (res.get("problems") or [res.get("error", f"HTTP {code}")])]}
    plan = res["plan"]
    running, waiting = _queue(state)
    out = {"committed": False, "recipe": name, "valid": True, "plan": plan, "estimate": views.estimate_render(plan, recipe.get("quality")),
           "quality": recipe.get("quality") or "final", "running": running, "waiting": waiting}
    if not commit:
        return out
    code, res = _post(site, f"/p/{slug}/api/montage", {"recipe": recipe})
    if code != 200:
        raise ViewError("not rendered: " + clean_line(res.get("error") or "; ".join(map(str, res.get("problems") or [])) or f"HTTP {code}", 300))
    job = res.get("job") or {}
    return {**out, "committed": True, "job": {k: (clean_line(v, 80) if isinstance(v, str) else v) for k, v in job.items() if k in ("id", "state", "title", "percent")}}


def stop(site, slug: str, commit: bool = False) -> dict:
    """Stop a project's running and waiting renders (the web app stops them all, not one). Without `commit`: which ones would stop.
    -> {"committed", "project", "renders": [{id, state, title}]}"""
    jobs = [j for j in views._state(site, slug).get("jobs", []) if j.get("state") in ("running", "queued")]
    renders = [{"id": clean_line(j.get("id"), 40), "state": clean_line(j.get("state"), 16), "title": clean_line(j.get("title"), 60)} for j in jobs]
    out = {"committed": False, "project": slug, "renders": renders}
    if not commit or not renders:                         # nothing to stop: nothing is sent, even with --commit
        return out
    code, res = _post(site, f"/p/{slug}/api/montage/cancel", {})
    if code != 200:
        raise ViewError("not stopped: " + clean_line(res.get("error") or f"HTTP {code}", 300))
    return {**out, "committed": True}


def format_make(d: dict, slug: str) -> str:
    if not d["valid"]:
        return views.format_plan({"recipe": d["recipe"], "valid": False, "problems": d["problems"]})
    head = f"{slug}: {clean_line(d['recipe'], 80)} ({d['quality']})\n"
    body = views.format_plan({"recipe": d["recipe"], "valid": True, "plan": d["plan"], "estimate": d["estimate"]})
    ahead = f"  {d['running']} rendering and {d['waiting']} waiting in this project now\n"
    if d["committed"]:
        return head + body + ahead + f"  STARTED: job {d['job'].get('id', '?')}\n"
    return head + body + ahead + "  nothing was started (add --commit)\n"


def format_stop(d: dict) -> str:
    if not d["renders"]:
        return f"{d['project']}: nothing is rendering or waiting\n"
    lines = [f"{d['project']}: " + ("STOPPED" if d["committed"] else "would stop") + f" {len(d['renders'])} render(s), the running one and every one waiting:"]
    lines += [f"  {r['state']:<8} {r['title'] or '(untitled)'}" for r in d["renders"]]
    if not d["committed"]:
        lines.append("  nothing was stopped (add --commit)")
    return "\n".join(lines) + "\n"
