"""Mode C: ask Claude to draft a video recipe for a project, from what the project's pictures actually look like.

Runs on THIS machine (where Claude Code is installed and logged in), not on the Premiere machine: nothing is installed
there and no login is placed there. It talks to the web app over the tailnet:

  1. read the project's catalog, captions and the recipe brief (the same text "Copy brief for Claude" gives you),
  2. fetch the pictures' thumbnails (small JPEGs, one request per folder),
  3. run `claude -p` once with NO tools: the brief, the pictures as images, and your request. It can only answer,
  4. check the answer with the web app's own dry run (the same validation a hand-written recipe gets); if it has
     problems, show them to Claude once and let it fix them,
  5. save the recipe under a name. It is NEVER rendered here: you open the page, look at the plan and press Make.

The recipe is data, validated by the server, so even a hostile caption cannot make the model do anything: it has no
tools, and what it returns is checked before it is kept. What does leave the machine is the thumbnails and captions (they
go to Anthropic through your Claude Code login); pass --no-images to send only names and captions.

Stdlib only. The model call and the HTTP calls are injectable so tests run without either.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_IMAGES = 12
DEFAULT_BUDGET = 0.5                  # dollars of API-rate usage the single call may not exceed (a safety cap)
CLAUDE_TIMEOUT = 240


class DraftError(Exception):
    """The draft could not be made; the message is meant to be shown."""


# ------------------------------------------------------------------ talking to the web app
class Site:
    """The web app at `base` (e.g. https://host.ts.net/umcares), for one project."""

    def __init__(self, base: str, timeout: int = 90):
        base = (base or "").strip().rstrip("/")
        if not re.match(r"https?://", base):
            raise DraftError("set UMC_WEB_URL to the web app's address, e.g. https://<machine>.<tailnet>.ts.net/umcares")
        self.base, self.timeout = base, timeout
        self._ctx = None

    def _context(self):
        if self._ctx is None:
            self._ctx = ssl.create_default_context()
        return self._ctx

    def _open(self, req):
        try:
            return urllib.request.urlopen(req, timeout=self.timeout, context=self._context())
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                self._ctx = ssl.create_default_context(cafile="/etc/ssl/cert.pem")          # python's own bundle can be stale on macOS
                return urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx)
            raise

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Requested-With": "umcares"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self._open(req) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            e.close()
            try:
                d = json.loads(raw)
            except ValueError:
                d = {"error": f"HTTP {e.code}"}
            return e.code, d
        except (urllib.error.URLError, OSError) as e:
            raise DraftError(f"could not reach the web app at {self.base}: {getattr(e, 'reason', e)}") from None
        return 200, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode("utf-8", "replace"))


def gather(site, slug: str, max_images: int = MAX_IMAGES, images: bool = True, need_media: bool = True) -> dict:
    """Catalog, brief and (optionally) thumbnails of a project. Thumbnails are matched to catalog items by folder and name."""
    base = f"/p/{slug}"
    code, state = site.call("GET", base + "/api/montage")
    if code != 200:
        raise DraftError(f"no such project '{slug}' on the web app" if code == 404 else f"the web app refused the project ({code})")
    code, brief = site.call("GET", base + "/api/montage/brief")
    if code != 200 or not isinstance(brief, str):
        raise DraftError("could not read the recipe brief from the web app")
    items = [i for i in state["catalog"] if i["kind"] in ("image", "video")]
    if not items and need_media:
        raise DraftError("this project has no pictures or clips to make a video from")
    pics = []
    if images:
        wanted = items[:max_images]
        for folder in sorted({i["folder"] for i in wanted}):
            for _ in range(6):                                          # the server makes a few thumbnails per call; ask again for the rest
                code, d = site.call("GET", f"{base}/api/thumbs?folder={urllib.parse.quote(folder, safe='')}&budget=8")
                if code != 200 or not isinstance(d, dict):
                    break
                for item in wanted:
                    uri = d.get("thumbs", {}).get(item["name"]) if item["folder"] == folder else None
                    if uri and not any(p[0]["ref"] == item["ref"] for p in pics):
                        pics.append((item, base64.b64decode(uri.split(",", 1)[1])))
                if not d.get("pending"):
                    break
                time.sleep(0.5)
    return {"slug": slug, "state": state, "brief": brief, "items": items, "pics": pics}


# ------------------------------------------------------------------ the question
RULES = """You are drafting ONE video recipe for the project below. The recipe format, the limits and advice on structure
are in the brief. Rules:
- Reply with ONLY the recipe as one JSON object. No prose before or after, no code fences.
- Refer to pictures and clips by their `ref` exactly as listed. Never invent a file. You may leave some out.
- Look at the pictures: choose and order them so the video has a hook, a body and an ending; put the strongest picture first.
- Keep captions short, in the language the existing captions use. Keep an existing caption unless it is unusable.
- Prefer the options the brief offers for polish (a look that fits, a title card, an end card) over inventing fields.
"""


REFINE = """You are REVISING the recipe below, not starting again. Apply the request to it and return the whole revised recipe.
Change only what the request asks for; keep every other clip, caption, option and the order exactly as they are."""


def base_recipe(ctx: dict, name: str) -> dict:
    """A saved recipe of the project to revise: by name, or "latest" for the newest valid claude-draft-*."""
    saved = [r for r in ctx["state"].get("recipes", []) if r.get("recipe")]
    if name == "latest":                                        # only drafts carry a timestamp in their name, so only they can be ordered
        drafts = sorted((r for r in saved if r["name"].startswith("claude-draft-")), key=lambda r: r["name"])
        if not drafts:
            raise DraftError("this project has no drafted recipe to revise: draft one first, or name a saved recipe with --from")
        return drafts[-1]["recipe"]
    for r in saved:
        if r["name"] == name:
            return r["recipe"]
    bad = next((r for r in ctx["state"].get("recipes", []) if r["name"] == name), None)
    if bad:
        raise DraftError(f"recipe '{name}' is no longer valid ({'; '.join(bad.get('problems') or ['a file it uses is gone'])[:200]})")
    raise DraftError(f"no saved recipe called '{name}' in this project")


def build_input(ctx: dict, request: str, repair=None, base=None) -> str:
    """One stream-json line for `claude -p`: the pictures (each labelled) and the instructions."""
    content = []
    for item, jpeg in ctx["pics"]:
        content.append({"type": "text", "text": f"Picture ref={item['ref']} name={item['name']} kind={item['kind']}"
                                                 + (f" caption={item['caption']!r}" if item["caption"] else "")})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
    shown = {p[0]["ref"] for p in ctx["pics"]}
    listing = "\n".join(f"- ref={i['ref']} name={i['name']} kind={i['kind']}" + (f" caption={i['caption']!r}" if i["caption"] else "")
                        + ("" if i["ref"] in shown else " (no picture shown)") for i in ctx["items"])
    text = (ctx.get("rules", RULES) + "\n## Files in the project\n" + listing + "\n\n## Brief\n" + ctx["brief"])
    if base is not None:
        text += "\n\n## Recipe to revise\n" + REFINE + "\n" + json.dumps(base)
    text += "\n\n## Request\n" + request.strip()
    if repair:
        text += ("\n\n## Your previous answer was rejected\n" + repair["answer"] + "\n\nProblems found by the checker:\n"
                 + "\n".join(f"- {p}" for p in repair["problems"]) + "\n\nReturn the corrected recipe, JSON only.")
    content.append({"type": "text", "text": text})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}})


def ask_claude(line: str, budget: float = DEFAULT_BUDGET, model=None, timeout: int = CLAUDE_TIMEOUT, claude=None, runner=subprocess.run,
               tools: str = "") -> str:
    """Run `claude -p` once and return the text of its answer. With every tool switched off unless `tools` names some (the research step
    names only the two read-only web tools, and nothing that can write, run or edit)."""
    exe = claude or shutil.which("claude")
    if not exe:
        raise DraftError("Claude Code (`claude`) is not installed on this machine")
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--tools", tools,
           "--no-session-persistence", "--max-budget-usd", str(budget)]
    if tools:
        cmd += ["--allowedTools", tools]                                 # headless: nothing could answer a permission prompt
    if model:
        cmd += ["--model", model]
    try:
        r = runner(cmd, input=line, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DraftError(f"Claude did not answer within {timeout} s") from None
    except OSError as e:
        raise DraftError(f"could not run claude: {e}") from None
    result = None
    for ln in (r.stdout or "").splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "result":
            result = d
    if result is None or result.get("is_error"):
        why = (result or {}).get("result") or (r.stderr or "").strip()[-300:] or "no answer"
        raise DraftError(f"Claude returned an error: {why}")
    return str(result.get("result") or "")


def extract_recipe(text: str) -> dict:
    """The JSON object in a reply, tolerating a code fence or a sentence around it."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    elif not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise DraftError("Claude's answer had no recipe in it")
        t = t[i:j + 1]
    try:
        d = json.loads(t)
    except ValueError:
        raise DraftError("Claude's answer was not valid JSON") from None
    if not isinstance(d, dict):
        raise DraftError("Claude's answer was not a recipe object")
    return d


ASKED_LENGTH = re.compile(r"(\d+(?:[.,]\d+)?)[\s-]*(seconds?|secs?|saat|s|minutes?|mins?|minit)\b", re.I)


def asked_seconds(request: str):
    """The length the request names ("a 20 second reel", "2 minit"), in seconds, or None when it names none."""
    m = ASKED_LENGTH.search(request or "")
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    return n * 60 if m.group(2).lower().startswith("m") else n


def length_warning(request: str, plan: dict):
    """A sentence when the drafted video is clearly shorter than the length the request named, else None. A cap such as max_seconds is not
    checked: a recipe may stay under its cap, but a named length is a promise."""
    want = asked_seconds(request)
    if not want or want < 5 or plan["total"] >= want * 0.8:
        return None
    return (f"you asked for about {want:g} s but the clips and cards only make {plan['total']:g} s; "
            f"add more pictures or clips, give a clip a longer hold, or accept the shorter video")


# ------------------------------------------------------------------ the whole job
def draft(site, slug: str, request: str, max_images: int = MAX_IMAGES, images: bool = True, budget: float = DEFAULT_BUDGET,
          model=None, save: bool = True, ask=None, claude=None, revise=None, ctx=None) -> dict:
    """Draft, check, (repair once), and save a recipe. With `revise` (a saved recipe's name, or "latest") the request is applied to that
    recipe instead of drafting from scratch; the original is left as it is and the result is saved under a new name. Returns {"name", "recipe", "plan", "attempts", "images"}."""
    if not request or not request.strip():
        raise DraftError("say what you want, e.g. \"a 20 second reel of the opening, upbeat\"")
    if ask is None:
        claude = claude or shutil.which("claude")
        if not claude:                                                  # before the slow part, not after it
            raise DraftError("Claude Code (`claude`) is not installed on this machine")
        ask = lambda line: ask_claude(line, budget=budget, model=model, claude=claude)
    ctx = ctx or gather(site, slug, max_images, images)
    base = base_recipe(ctx, revise) if revise else None
    repair, last = None, None
    for attempt in (1, 2):
        answer = ask(build_input(ctx, request, repair, base))
        try:
            recipe = extract_recipe(answer)
        except DraftError as e:
            repair, last = {"answer": answer[:2000], "problems": [str(e)]}, str(e)
            continue
        code, d = site.call("POST", f"/p/{slug}/api/montage", {"recipe": recipe, "dry_run": True})
        if code == 200:
            name = None
            if save:
                name = "claude-draft-" + time.strftime("%Y%m%d-%H%M%S")
                code, s = site.call("POST", f"/p/{slug}/api/montage/recipes", {"name": name, "recipe": recipe})
                if code != 200:
                    raise DraftError("the recipe is valid but could not be saved: " + str(s.get("error", code)))
            return {"name": name, "recipe": recipe, "plan": d["plan"], "attempts": attempt, "images": len(ctx["pics"]), "revised": bool(base),
                    "warnings": [w for w in [length_warning(request, d["plan"])] if w]}
        problems = d.get("problems") or [d.get("error", f"HTTP {code}")]
        repair, last = {"answer": json.dumps(recipe)[:4000], "problems": problems}, "; ".join(problems[:3])
    raise DraftError("Claude's recipe did not pass the checker after one correction: " + (last or "unknown problem"))


# ------------------------------------------------------------------ a topic, researched
WEB_TOOLS = "WebSearch,WebFetch"
RESEARCH_BUDGET = 1.0
RESEARCH_TIMEOUT = 600

RESEARCH = """Research the topic below on the web and write a FACT SHEET for a short explainer video. Rules:
- Search first, then read the best pages. Prefer the vendor's or standards body's own documentation, then reputable engineering or news sources.
- 8 to 14 facts, each ONE plain sentence a viewer could understand, each followed by its source URL in parentheses. A fact you could not
  find a source for is left out, not guessed.
- No statistics, percentages or dates unless the source states them; when one is used, quote it as the source does.
- Add a final section "## Not settled" listing anything the sources disagree on or that is a matter of opinion.
- Write in English. Reply with ONLY the fact sheet in markdown.
Topic: """

EXPLAIN_RULES = """You are drafting ONE video recipe that EXPLAINS a topic, using ONLY the fact sheet below. The recipe format, the limits and the
card kinds are in the brief. Rules:
- Reply with ONLY the recipe as one JSON object. No prose before or after, no code fences.
- The project has no pictures: build the whole video from cards (quote, announce, countdown, checklist, progress, typing, list, flow,
  versus) and use no `media` clips. Give it a hook first, a body of 5 to 8 cards that each carry one idea, and an ending.
- Every claim on a card must come from the fact sheet; do not add numbers, names or dates it does not contain, and drop what is "Not settled".
- Use the right card for the idea: `flow` for a sequence, `versus` for before and after, `checklist` for a list of benefits, `typing` for commands.
- End with an `end_card` that names the sources (their sites, not full URLs).
- `quote` is for a real quote with a real source in the fact sheet, and `by` names that source. A framing line of your own (a hook, a question, a
  summary) is an `announce` card or a plain `quote` with no `by`; never credit your own line to the topic, the title or the sources.
- Write every word shown on screen in this language: """


def research(topic: str, ask) -> str:
    """The fact sheet for a topic. `ask` takes a stream-json line and returns text. A sheet with fewer than two source URLs is refused: it
    means the model answered from memory, and a video built on that would carry invented facts."""
    if not topic or not topic.strip():
        raise DraftError("say what the video is about, e.g. \"how DevOps works\"")
    line = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": RESEARCH + topic.strip()}]}})
    sheet = ask(line).strip()
    if len(set(re.findall(r"https?://[^\s)\]>]+", sheet))) < 2:
        raise DraftError("the research came back without sources (fewer than two links), so nothing was drafted; try again or narrow the topic")
    return sheet


def explain(site, slug: str, topic: str, lang: str = "English", budget: float = DEFAULT_BUDGET, model=None, save: bool = True,
            ask=None, research_ask=None, claude=None) -> dict:
    """Research a topic, then draft a cards-only recipe from the fact sheet through `draft`. Returns draft()'s result plus "sheet"."""
    if research_ask is None or ask is None:
        claude = claude or shutil.which("claude")
        if not claude:
            raise DraftError("Claude Code (`claude`) is not installed on this machine")
        if research_ask is None:
            research_ask = lambda line: ask_claude(line, budget=RESEARCH_BUDGET, model=model, timeout=RESEARCH_TIMEOUT, claude=claude, tools=WEB_TOOLS)
    sheet = research(topic, research_ask)
    ctx = gather(site, slug, 0, False, need_media=False)
    ctx["rules"] = EXPLAIN_RULES + (lang or "English") + "\n\n## Fact sheet\n" + sheet + "\n"
    res = draft(site, slug, f"Explain: {topic.strip()}", budget=budget, model=model, save=save, ask=ask, claude=claude, ctx=ctx)
    res["sheet"] = sheet
    return res


# ------------------------------------------------------------------ judging chosen videos
def judge_remote(site, slug: str, names: list, force: bool = False, batch: int = 5) -> dict:
    """Ask the web app to judge the named videos (a few per request, so one request never takes minutes).
    Returns {"results": {name: report}, "errors": {name: message}}."""
    out = {"results": {}, "errors": {}}
    for i in range(0, len(names), batch):
        code, d = site.call("POST", f"/p/{slug}/api/montage/judge", {"names": names[i:i + batch], "force": bool(force)})
        if code != 200:
            raise DraftError("the web app refused to judge: " + str(d.get("error", code)))
        out["results"].update(d.get("results", {}))
        out["errors"].update(d.get("errors", {}))
    return out

