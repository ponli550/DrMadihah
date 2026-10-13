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


def gather(site, slug: str, max_images: int = MAX_IMAGES, images: bool = True) -> dict:
    """Catalog, brief and (optionally) thumbnails of a project. Thumbnails are matched to catalog items by folder and name."""
    base = f"/p/{slug}"
    code, state = site.call("GET", base + "/api/montage")
    if code != 200:
        raise DraftError(f"no such project '{slug}' on the web app" if code == 404 else f"the web app refused the project ({code})")
    code, brief = site.call("GET", base + "/api/montage/brief")
    if code != 200 or not isinstance(brief, str):
        raise DraftError("could not read the recipe brief from the web app")
    items = [i for i in state["catalog"] if i["kind"] in ("image", "video")]
    if not items:
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


def build_input(ctx: dict, request: str, repair=None) -> str:
    """One stream-json line for `claude -p`: the pictures (each labelled) and the instructions."""
    content = []
    for item, jpeg in ctx["pics"]:
        content.append({"type": "text", "text": f"Picture ref={item['ref']} name={item['name']} kind={item['kind']}"
                                                 + (f" caption={item['caption']!r}" if item["caption"] else "")})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
    shown = {p[0]["ref"] for p in ctx["pics"]}
    listing = "\n".join(f"- ref={i['ref']} name={i['name']} kind={i['kind']}" + (f" caption={i['caption']!r}" if i["caption"] else "")
                        + ("" if i["ref"] in shown else " (no picture shown)") for i in ctx["items"])
    text = (RULES + "\n## Files in the project\n" + listing + "\n\n## Brief\n" + ctx["brief"] + "\n\n## Request\n" + request.strip())
    if repair:
        text += ("\n\n## Your previous answer was rejected\n" + repair["answer"] + "\n\nProblems found by the checker:\n"
                 + "\n".join(f"- {p}" for p in repair["problems"]) + "\n\nReturn the corrected recipe, JSON only.")
    content.append({"type": "text", "text": text})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}})


def ask_claude(line: str, budget: float = DEFAULT_BUDGET, model=None, timeout: int = CLAUDE_TIMEOUT, claude=None, runner=subprocess.run) -> str:
    """Run `claude -p` once, with every tool switched off, and return the text of its answer."""
    exe = claude or shutil.which("claude")
    if not exe:
        raise DraftError("Claude Code (`claude`) is not installed on this machine")
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--tools", "",
           "--no-session-persistence", "--max-budget-usd", str(budget)]
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


# ------------------------------------------------------------------ the whole job
def draft(site, slug: str, request: str, max_images: int = MAX_IMAGES, images: bool = True, budget: float = DEFAULT_BUDGET,
          model=None, save: bool = True, ask=None, claude=None) -> dict:
    """Draft, check, (repair once), and save a recipe. Returns {"name", "recipe", "plan", "attempts", "images"}."""
    if not request or not request.strip():
        raise DraftError("say what you want, e.g. \"a 20 second reel of the opening, upbeat\"")
    if ask is None:
        claude = claude or shutil.which("claude")
        if not claude:                                                  # before the slow part, not after it
            raise DraftError("Claude Code (`claude`) is not installed on this machine")
        ask = lambda line: ask_claude(line, budget=budget, model=model, claude=claude)
    ctx = gather(site, slug, max_images, images)
    repair, last = None, None
    for attempt in (1, 2):
        answer = ask(build_input(ctx, request, repair))
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
            return {"name": name, "recipe": recipe, "plan": d["plan"], "attempts": attempt, "images": len(ctx["pics"])}
        problems = d.get("problems") or [d.get("error", f"HTTP {code}")]
        repair, last = {"answer": json.dumps(recipe)[:4000], "problems": problems}, "; ".join(problems[:3])
    raise DraftError("Claude's recipe did not pass the checker after one correction: " + (last or "unknown problem"))
