"""An MCP server (stdio) for umcares, so Claude and other MCP clients can drive it as typed tools.

MCP over stdio is newline-delimited JSON-RPC 2.0: a client sends `initialize`, then `notifications/initialized`, then calls `tools/list` and
`tools/call`. That is all a tool server needs, so it is written here with the standard library only (no `mcp` package to install).

Phase 1 is the video side: the tools are a thin layer over the web app's own API (the same one the page and `umcares draft` use), so a recipe
an LLM writes goes through exactly the validation a hand-written one gets. Nothing is rendered unless `render_recipe` is called, and nothing
is deleted: there is deliberately no tool for deleting an export.

stdout carries the protocol and nothing else; anything to say to a human goes to stderr.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from urllib.parse import quote

from . import draft

VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")       # newest first: the client's own is echoed back when we know it
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MAX_THUMBS = 6                                                # pictures per call: each one is a photo of someone sent to the model

INSTRUCTIONS = """umcares makes short social videos from a project's pictures, clips and cards, rendered with ffmpeg on the machine that runs the web app.
A good order: get_recipe_brief (the recipe format, once), get_project (what the project holds), plan_recipe until it has no problems, save_recipe, then
ask the person before render_recipe (it uses that machine's CPU for minutes), then get_jobs, then judge_videos on the result.
get_thumbnails sends the person's photos to you: only call it when they have agreed. A plan with problems is a normal answer, not a failure: fix
the recipe from the list and plan again."""


class ToolError(Exception):
    """A failure the model should read and act on (shown as a tool result with isError, not as a protocol error)."""


def _schema(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


PROJECT = {"type": "string", "description": "the project's folder name on the web app, e.g. cyberdsa (list_projects shows them)"}
RECIPE = {"type": "object", "description": "a montage recipe (get_recipe_brief documents the format)"}


def _project(args: dict) -> str:
    slug = args.get("project")
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ToolError("project: the project's folder name, lower case letters, digits and dashes (list_projects shows them)")
    return slug


def _recipe(args: dict) -> dict:
    r = args.get("recipe")
    if not isinstance(r, dict):
        raise ToolError("recipe: an object (get_recipe_brief documents the format)")
    return r


def _call(site, method: str, path: str, body=None):
    try:
        return site.call(method, path, body)
    except draft.DraftError as e:
        raise ToolError(str(e)) from None


def _ok(code, d, what: str):
    if code != 200:
        problems = d.get("problems") if isinstance(d, dict) else None
        raise ToolError(what + ": " + (json.dumps({"problems": problems}) if problems else str(d.get("error", code) if isinstance(d, dict) else code)))
    return d


# ------------------------------------------------------------------ the tools: each takes (site, args) and returns a list of MCP content blocks
def _text(obj) -> list:
    return [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)}]


def t_list_projects(site, args):
    d = _ok(*_call(site, "GET", "/api/projects"), "list_projects")
    return _text(d)


def t_get_project(site, args):
    slug = _project(args)
    d = _ok(*_call(site, "GET", f"/p/{slug}/api/montage"), "get_project")
    return _text({
        "project": slug,
        "catalog": [{k: i.get(k) for k in ("ref", "name", "kind", "folder", "caption")} for i in d.get("catalog", [])],
        "recipes": [{k: r.get(k) for k in ("name", "valid", "problems")} for r in d.get("recipes", [])],
        "exports": [{k: e.get(k) for k in ("name", "template", "format", "duration", "title", "quality") if k in e} for e in d.get("exports", [])],
        "jobs": d.get("jobs", []), "rendering": d.get("active", False),
        "looks": d.get("templates", {}), "formats": d.get("formats", {})})


def t_get_recipe_brief(site, args):
    slug = _project(args)
    code, d = _call(site, "GET", f"/p/{slug}/api/montage/brief")
    if code != 200 or not isinstance(d, str):
        raise ToolError("get_recipe_brief: the web app did not return the brief")
    return _text(d)


def t_get_thumbnails(site, args):
    slug = _project(args)
    folder = args.get("folder")
    if not isinstance(folder, str) or not folder or "\n" in folder:
        raise ToolError("folder: a folder name from get_project's catalog (e.g. photos)")
    wanted = args.get("names")
    if wanted is not None and not (isinstance(wanted, list) and all(isinstance(x, str) for x in wanted)):
        raise ToolError("names: a list of file names, or leave it out")
    try:
        limit = max(1, min(int(args.get("limit", MAX_THUMBS)), MAX_THUMBS))
    except (TypeError, ValueError):
        raise ToolError(f"limit: a number from 1 to {MAX_THUMBS}") from None
    code, d = _call(site, "GET", f"/p/{slug}/api/thumbs?folder={quote(folder, safe='')}&budget=8")
    d = _ok(code, d if isinstance(d, dict) else {}, "get_thumbnails")
    out, shown = [], 0
    for name, uri in sorted(d.get("thumbs", {}).items()):
        if wanted is not None and name not in wanted:
            continue
        if shown >= limit or not isinstance(uri, str) or "," not in uri:
            continue
        try:
            base64.b64decode(uri.split(",", 1)[1], validate=True)
        except ValueError:
            continue
        out += [{"type": "text", "text": f"{folder}/{name}"}, {"type": "image", "data": uri.split(",", 1)[1], "mimeType": "image/jpeg"}]
        shown += 1
    if not out:
        return _text({"thumbnails": 0, "pending": bool(d.get("pending")), "note": "none ready yet, ask again in a moment" if d.get("pending") else "no pictures matched"})
    return out


def t_plan_recipe(site, args):
    slug, recipe = _project(args), _recipe(args)
    d = _ok(*_call(site, "POST", f"/p/{slug}/api/montage", {"recipe": recipe, "dry_run": True}), "the recipe has problems")
    return _text(d["plan"])


def t_save_recipe(site, args):
    slug, recipe = _project(args), _recipe(args)
    name = args.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        raise ToolError("name: a name for the saved recipe, 1 to 80 characters (a saved recipe of the same name is replaced)")
    d = _ok(*_call(site, "POST", f"/p/{slug}/api/montage/recipes", {"name": name, "recipe": recipe}), "not saved")      # the web app checks the recipe before it keeps it
    return _text(d)


def t_render_recipe(site, args):
    slug, recipe = _project(args), dict(_recipe(args))
    q = args.get("quality")
    if q is not None:
        if q not in ("final", "share", "draft"):
            raise ToolError("quality: final, share (a smaller file) or draft (half size, fast), or leave it out")
        recipe["quality"] = q
    d = _ok(*_call(site, "POST", f"/p/{slug}/api/montage", {"recipe": recipe}), "not rendered")
    return _text(d["job"])


def t_get_jobs(site, args):
    slug = _project(args)
    d = _ok(*_call(site, "GET", f"/p/{slug}/api/montage"), "get_jobs")
    return _text({"rendering": d.get("active", False), "jobs": d.get("jobs", [])})


def t_cancel_render(site, args):
    slug = _project(args)
    return _text(_ok(*_call(site, "POST", f"/p/{slug}/api/montage/cancel", {}), "cancel_render"))


def t_judge_videos(site, args):
    slug = _project(args)
    names = args.get("names")
    if not (isinstance(names, list) and 1 <= len(names) <= 20 and all(isinstance(x, str) and x for x in names)):
        raise ToolError("names: 1 to 20 finished video file names (get_project lists them under exports)")
    try:
        return _text(draft.judge_remote(site, slug, names, force=bool(args.get("force", False))))
    except draft.DraftError as e:
        raise ToolError(str(e)) from None


READ, WRITE = {"readOnlyHint": True}, {"readOnlyHint": False, "destructiveHint": False}
TOOLS = {
    "list_projects": (t_list_projects, "List the projects on the umcares web app (name, folder name, counts).", _schema({}), READ),
    "get_project": (t_get_project, "What a project holds: its pictures and clips (ref, name, kind, folder, caption), saved recipes and whether each still "
                    "works, finished videos, render jobs, and the looks and formats it offers. Use the refs in a recipe's clips.", _schema({"project": PROJECT}, ["project"]), READ),
    "get_recipe_brief": (t_get_recipe_brief, "The recipe format in full: every field, card kind, look, effect and limit, with an example. Read it before writing a recipe.",
                         _schema({"project": PROJECT}, ["project"]), READ),
    "get_thumbnails": (t_get_thumbnails, "Small pictures from a project folder so you can choose and order them. These are the person's photos (often of people) and "
                       "are sent to you: only call this when they have agreed.", _schema({"project": PROJECT, "folder": {"type": "string", "description": "a folder from get_project, e.g. photos"},
                                                                                           "names": {"type": "array", "items": {"type": "string"}, "description": "only these file names"},
                                                                                           "limit": {"type": "integer", "minimum": 1, "maximum": MAX_THUMBS}}, ["project", "folder"]), READ),
    "plan_recipe": (t_plan_recipe, "Check a recipe without rendering: returns the exact timeline (pieces, seconds, joins, warnings), or the list of problems to fix. "
                    "Free and safe to repeat.", _schema({"project": PROJECT, "recipe": RECIPE}, ["project", "recipe"]), READ),
    "save_recipe": (t_save_recipe, "Save a recipe in the project under a name so the person can open it on the page (Make a video, Saved recipes). Checked first; a saved "
                    "recipe of the same name is replaced. Does not render.", _schema({"project": PROJECT, "name": {"type": "string"}, "recipe": RECIPE}, ["project", "name", "recipe"]), WRITE),
    "render_recipe": (t_render_recipe, "Render a recipe to a video on the web app's machine. It takes the machine's CPU for minutes: ask the person first. Returns a job; "
                      "poll get_jobs. quality: final (default), share (a smaller file for posting) or draft (half size, fast).",
                      _schema({"project": PROJECT, "recipe": RECIPE, "quality": {"type": "string", "enum": ["final", "share", "draft"]}}, ["project", "recipe"]), WRITE),
    "get_jobs": (t_get_jobs, "The project's render jobs: state, percent done, error, and the output file name when done.", _schema({"project": PROJECT}, ["project"]), READ),
    "cancel_render": (t_cancel_render, "Stop the project's queued and running renders.", _schema({"project": PROJECT}, ["project"]), {"readOnlyHint": False, "destructiveHint": True}),
    "judge_videos": (t_judge_videos, "Check finished videos you name (format, sound, picture) and get pass, warn or fail with reasons. It checks that a video is technically "
                     "sound, not that it looks good.", _schema({"project": PROJECT, "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
                                                                  "force": {"type": "boolean", "description": "judge again even if a report is kept"}}, ["project", "names"]), READ),
}


# ------------------------------------------------------------------ JSON-RPC
class Server:
    def __init__(self, site, extra=None):
        self.site = site
        self.tools = dict(TOOLS)
        if extra:
            self.tools.update(extra)

    def handle(self, msg):
        """One incoming JSON-RPC message -> the response object, or None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return self._error(msg.get("id") if isinstance(msg, dict) else None, -32600, "not a JSON-RPC 2.0 request")
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if "id" not in msg:                                           # a notification: never answered
            return None
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            return self._result(mid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {"listChanged": False}},
                                      "serverInfo": {"name": "umcares", "version": VERSION}, "instructions": INSTRUCTIONS})
        if method == "ping":
            return self._result(mid, {})
        if method == "tools/list":
            return self._result(mid, {"tools": [{"name": n, "description": d, "inputSchema": s, "annotations": {**a, "openWorldHint": False}}
                                                for n, (_, d, s, a) in self.tools.items()]})
        if method == "tools/call":
            name, args = (params.get("name"), params.get("arguments") or {}) if isinstance(params, dict) else (None, {})
            if name not in self.tools:
                return self._error(mid, -32602, f"unknown tool: {name}")
            if not isinstance(args, dict):
                return self._error(mid, -32602, "arguments must be an object")
            try:
                return self._result(mid, {"content": self.tools[name][0](self.site, args), "isError": False})
            except ToolError as e:
                return self._result(mid, {"content": _text(str(e)), "isError": True})
            except Exception as e:                                    # a bug here must not kill the server the client is talking to
                print(f"umcares mcp: {name} failed: {e!r}", file=sys.stderr)
                return self._result(mid, {"content": _text(f"{name} failed unexpectedly: {e}"), "isError": True})
        return self._error(mid, -32601, f"method not found: {method}")

    @staticmethod
    def _result(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _error(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def serve(self, rin, rout):
        for line in rin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = self._error(None, -32700, "parse error")
            else:
                reply = [r for r in (self.handle(m) for m in msg) if r] if isinstance(msg, list) else self.handle(msg)
            if reply:
                rout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                rout.flush()
