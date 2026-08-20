"""The script: one editable view of narration, and a guard against drift.

Two things kept going wrong between the recipe and the delivery:

  * The subtitle file got hand-edited. `video_subtitles.srt` was fixed up for
    readability, the recipe's `narration` was not, and the next render put the
    old wording back — silently, because nothing compared them.
  * Reviewing the flow meant reading JSON. Scene order, wording and captions
    live in a recipe that is otherwise all timings and asset keys, so the one
    part a human actually rewrites was the hardest part to read.

So this module does the round trip:

    umcares script export   recipe  -> script.md   (narration + captions, editable)
    umcares script import   script.md -> recipe    (only those two fields)
    umcares script check    recipe vs an SRT       (drift, per scene)

Import deliberately writes back *only* narration and captions. Durations,
visuals and audio routing are resolved from measured media (see `recipe.py`),
and a markdown table is not a safe place to edit numbers that the resolver is
meant to compute.

`check` classifies rather than just passing/failing, because the two kinds of
difference call for opposite actions:

    ok      the SRT is what the recipe says
    edited  same words, different punctuation or case -- a proofread; adopt it
    drift   words differ -- someone rewrote one side; decide which is right
    missing narration with no cue over its scene window -- captions never built
"""
from __future__ import annotations

import difflib
import re

# The scene marker is an HTML comment so it survives a human editing the
# heading text right above it. Headings are for reading; this is the anchor.
SCENE_MARK = re.compile(r"<!--\s*umcares:scene\s+id=([^\s>]+?)\s*-->")
FIELD_HEAD = re.compile(r"^\*\*(\w[\w ]*)\*\*\s*$")

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


# ------------------------------------------------------------ comparison --
def collapse(text: str) -> str:
    """Whitespace-normalised text. Line wrapping must not read as a change."""
    return " ".join((text or "").split())


def words(text: str) -> list:
    """Lowercase word list, punctuation dropped — the 'did anyone rewrite it?'
    view. Proofreading commas and casing are noise at this level."""
    return _PUNCT.sub(" ", (text or "").lower()).split()


def word_diff(a: str, b: str) -> dict:
    """Which words `b` lost and gained relative to `a`."""
    wa, wb = words(a), words(b)
    removed, added = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=wa, b=wb).get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(wa[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(wb[j1:j2])
    return {"removed": removed, "added": added}


def aliases(recipe: dict) -> list:
    """[[spoken, displayed], …] pairs the check must treat as the same thing.

    Narration is TTS input and a caption is display text, so they are *meant*
    to differ in places: the voice says "seratus" because "100" reads as
    "satu kosong kosong", and the caption shows "100" because that is what a
    number looks like. Without a way to declare those pairs the check flags
    them every run, and a check that cries wolf gets muted.

        "subtitles": { "aliases": [["seratus", "100"]] }
    """
    subs = recipe.get("subtitles") or {}
    out = []
    for pair in subs.get("aliases") or []:
        if isinstance(pair, dict):
            spoken, shown = pair.get("spoken"), pair.get("shown")
        else:
            spoken, shown = (list(pair) + ["", ""])[:2]
        if spoken and shown:
            out.append([str(spoken), str(shown)])
    return out


def canonical(text: str, pairs: list) -> str:
    """Fold every declared alias onto its spoken form, on both sides."""
    out = collapse(text)
    for spoken, shown in pairs:
        for a, b in ((shown, spoken), (spoken, spoken)):
            out = re.sub(rf"(?<!\w){re.escape(a)}(?!\w)", b, out,
                         flags=re.IGNORECASE)
    return out


def attribute(cues: list, spans: list) -> tuple:
    """Give each cue to the one scene window it overlaps most.

    Counting a cue in every window it touches made a caption that straddles a
    boundary appear as a whole extra sentence in the earlier scene — reported
    as drift, when the cue is simply 0.3s long on the wrong side of a cut. A
    cue belongs to one scene; the biggest overlap decides which.

    `spans` is [(scene_id, start, end)]. Cues that overlap none come back as
    orphans, which is a real finding: they belong to a cut that no longer
    exists.
    """
    out = {sid: [] for sid, _, _ in spans}
    orphans = []
    for a, b, t in cues:
        best, cover = None, 0.0
        for sid, s0, e0 in spans:
            ov = min(b, e0) - max(a, s0)
            if ov > cover:
                best, cover = sid, ov
        if best is None:
            orphans.append((a, b, t))
        else:
            out[best].append((a, b, t))
    return out, orphans


def _contains(hay: list, needle: list) -> bool:
    """Is `needle` a contiguous run inside `hay`?"""
    if not needle or len(needle) > len(hay):
        return False
    first = needle[0]
    for i in range(len(hay) - len(needle) + 1):
        if hay[i] == first and hay[i:i + len(needle)] == needle:
            return True
    return False


def classify(narration: str, subs: str, all_subs: str = "") -> str:
    """Which of the four situations this scene is in.

    `all_subs` is every cue in the file, and it separates the two failures that
    look identical from inside one window: the words were rewritten, or the
    words are intact but sitting somewhere else. The first means deciding whose
    wording wins; the second means the SRT belongs to another cut and needs
    rebuilding — opposite fixes, so they get different names.
    """
    if not collapse(subs):
        return "missing"
    if collapse(narration) == collapse(subs):
        return "ok"
    if words(narration) == words(subs):
        return "edited"
    if all_subs and _contains(words(all_subs), words(narration)):
        return "shifted"
    return "drift"


def check(recipe: dict, resolved: dict, cues: list) -> dict:
    """Compare every scene's declared text against the cues that belong to it.

    `resolved` supplies the windows; without it a scene has no time span and
    nothing to compare, which is why this takes a resolved timeline rather
    than guessing from cue order.

    A scene declares its text either as `narration` (spoken, so the captions
    are split from it) or as `captions` (a testimonial's own audio, captioned
    by hand). Both end up as cues in the same SRT, so both are checked — the
    caption-only scenes are exactly where hand edits land.
    """
    spans = {sc["scene"]: sc for sc in (resolved.get("scenes") or [])}
    pairs = aliases(recipe)

    # Which scenes carry text, and where they sit. Only these compete for cues:
    # a cue that lands mostly inside a silent card is not a caption anyone
    # wrote, so it should surface as an orphan rather than be quietly absorbed.
    declared, windows = {}, []
    for scene in recipe.get("scenes") or []:
        sid = scene.get("id")
        narration = collapse(scene.get("narration") or "")
        caps = [collapse(c.get("text", "") if isinstance(c, dict) else c)
                for c in (scene.get("captions") or [])]
        caps = [c for c in caps if c]
        if narration:
            declared[sid] = ("narration", narration)
        elif caps:
            declared[sid] = ("captions", " ".join(caps))
        else:
            continue
        span = spans.get(sid)
        if span:
            windows.append((sid, float(span["start"]), float(span["end"])))

    mine, orphan_cues = attribute(cues, windows)
    all_subs = collapse(" ".join(t for _, _, t in cues))

    findings = []
    for sid, (field, text) in declared.items():
        span = spans.get(sid)
        if not span:
            findings.append({"scene": sid, "field": field,
                             "status": "unresolved",
                             "detail": "no window in resolved.json — "
                                       "run `umcares recipe resolve` first"})
            continue
        scene_cues = mine.get(sid) or []
        subs = collapse(" ".join(t for _, _, t in scene_cues))
        status = classify(canonical(text, pairs), canonical(subs, pairs),
                          canonical(all_subs, pairs))
        finding = {"scene": sid, "field": field, "status": status,
                   "start": round(float(span["start"]), 2),
                   "end": round(float(span["end"]), 2),
                   "cues": len(scene_cues),
                   "narration": text, "subs": subs}
        if status in ("edited", "shifted", "drift"):
            finding["diff"] = word_diff(canonical(text, pairs),
                                        canonical(subs, pairs))
        findings.append(finding)

    orphans = [{"start": round(a, 2), "end": round(b, 2), "text": t}
               for a, b, t in orphan_cues]
    failed = [f for f in findings
              if f["status"] in ("drift", "shifted", "missing", "unresolved")]
    return {"ok": not failed, "scenes": findings, "orphans": orphans,
            "failed": failed, "aliases": pairs,
            "srt_end": round(max((b for _, b, _ in cues), default=0.0), 2),
            "total": round(float(resolved.get("total") or 0), 2),
            "counts": {st: sum(1 for f in findings if f["status"] == st)
                       for st in ("ok", "edited", "shifted", "drift",
                                  "missing", "unresolved")}}


def report(result: dict) -> list:
    """One line per scene, for stderr."""
    mark = {"ok": "ok    ", "edited": "edited", "shifted": "SHIFTED",
            "drift": "DRIFT ", "missing": "MISSING", "unresolved": "NO SPAN"}
    lines = []
    for f in result["scenes"]:
        head = (f"  {mark.get(f['status'], f['status']):<7} {f['scene']:<18}"
                f"{f.get('field', ''):<10}")
        if f["status"] == "unresolved":
            lines.append(f"{head} {f.get('detail', '')}")
            continue
        lines.append(f"{head} {f['start']:>7.1f} - {f['end']:>7.1f}  "
                     f"{f['cues']} cue(s)")
        if f["status"] in ("edited", "shifted", "drift"):
            lines.append(f"           recipe: {f['narration']}")
            lines.append(f"           subs:   {f['subs']}")
            d = f.get("diff") or {}
            if d.get("removed") or d.get("added"):
                lines.append(f"           words:  -{' '.join(d['removed'])}"
                             f"  +{' '.join(d['added'])}")
    for o in result["orphans"]:
        lines.append(f"  ORPHAN  {o['start']:>7.1f} - {o['end']:>7.1f}  "
                     f"{o['text']}")
    return lines


# ---------------------------------------------------------------- export --
def _num(x) -> str:
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return str(x)


def _row_cells(line: str) -> list:
    """Cells of a markdown table row, outer pipes stripped."""
    s = line.strip()
    if not s.startswith("|"):
        return []
    return [c.strip() for c in s.strip("|").split("|")]


def _visual_row(v: dict) -> str:
    kind = next((k for k in ("clip", "card", "kenburns", "still", "black")
                 if k in v), "?")
    ref = v.get(kind)
    if isinstance(ref, dict):
        ref = ref.get("id") or ref.get("image") or ""
    dur = v.get("duration")
    return (f"| {kind} | {ref or ''} | "
            f"{'' if dur is None else f'{float(dur):g}'} | "
            f"{v.get('audio', 'mute')} |")


def export_markdown(recipe: dict, recipe_path: str = "",
                    resolved: dict | None = None) -> str:
    """Render the editable script. Visuals are shown, but read-only."""
    meta = recipe.get("meta") or {}
    title = meta.get("title") or meta.get("name") or "Script"
    spans = {s["scene"]: s for s in ((resolved or {}).get("scenes") or [])}

    out = [f"# {title}", ""]
    if recipe_path:
        out.append(f"<!-- umcares:script recipe={recipe_path} -->")
    out += [
        "",
        "Edit the **Narration** paragraphs and the **Captions** table, "
        "then:",
        "",
        "```",
        f"umcares script import --md script.md"
        + (f" --file {recipe_path}" if recipe_path else ""),
        "```",
        "",
        "Everything else here is read-only: durations and visual order are "
        "resolved from measured media, not from this file.",
        "",
    ]

    for scene in recipe.get("scenes") or []:
        sid = scene.get("id", "")
        span = spans.get(sid)
        head = f"## {sid}"
        if span:
            head += (f"  ({span['start']:.1f}–{span['end']:.1f}s, "
                     f"{span['duration']:.1f}s)")
        out += [head, f"<!-- umcares:scene id={sid} -->", ""]

        out += ["**Narration**", ""]
        out += [collapse(scene.get("narration") or "") or "_(silent)_", ""]

        out += ["**Captions**", ""]
        caps = scene.get("captions") or []
        if caps:
            # A caption is not just text: `at` and `duration` are authored too
            # (a testimonial's own audio decides them), so they round-trip in
            # the same table rather than being lost to a bullet list.
            out += ["| at | duration | text |", "|---:|---:|---|"]
            out += [f"| {_num(c.get('at', 0))} | {_num(c.get('duration', 3))} "
                    f"| {collapse(c.get('text', '')).replace('|', '/')} |"
                    if isinstance(c, dict) else
                    f"| 0 | 3 | {collapse(c).replace('|', '/')} |"
                    for c in caps]
        else:
            out.append("_(none)_")
        out.append("")

        visuals = scene.get("visuals") or []
        if visuals:
            out += ["**Visuals**", "",
                    "| kind | ref | duration | audio |",
                    "|---|---|---|---|"]
            out += [_visual_row(v) for v in visuals]
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def timeline_markdown(resolved: dict) -> str:
    """The resolved timeline as a table — the flow, reviewable at a glance."""
    out = ["# Timeline", "",
           f"- total: {resolved['total']:.1f}s "
           f"({resolved['total'] / 60:.1f} min)",
           f"- fps: {resolved.get('fps')}",
           f"- visuals: {len(resolved.get('video') or [])}, "
           f"audio: {len(resolved.get('audio') or [])}",
           "",
           "## Scenes",
           "",
           "| scene | start | end | duration | narration |",
           "|---|---:|---:|---:|---:|"]
    for s in resolved.get("scenes") or []:
        out.append(f"| {s['scene']} | {s['start']:.1f} | {s['end']:.1f} | "
                   f"{s['duration']:.1f} | {s['narration']:.1f} |")
    out.append("")
    out += ["## Visuals", "",
            "| start | kind | ref | duration | scene | audio |",
            "|---:|---|---|---:|---|---|"]
    for v in resolved.get("video") or []:
        out.append(f"| {v['start']:.1f} | {v['kind']} | {v['ref']} | "
                   f"{v['duration']:.1f} | {v['scene']} | {v['audio']} |")
    if resolved.get("missing"):
        out += ["", "**Missing durations** (render these first):", ""]
        out += [f"- `{k}`" for k in resolved["missing"]]
    for sh in resolved.get("short") or []:
        out += ["", f"**Short**: `{sh['scene']}` narration {sh['narration']}s "
                    f"but visuals {sh['visuals']}s "
                    f"(shortfall {sh['shortfall']}s)"]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- import --
def parse_markdown(text: str) -> dict:
    """{scene_id: {"narration": str, "captions": [str]}} from a script.md.

    Scenes are found by their marker comment, fields by their bold heading, so
    a human reflowing paragraphs or retitling a heading changes nothing about
    how this parses.
    """
    marks = list(SCENE_MARK.finditer(text))
    scenes = {}
    for i, m in enumerate(marks):
        sid = m.group(1)
        body = text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        fields, current, buf = {}, None, []
        for line in body.splitlines():
            head = FIELD_HEAD.match(line.strip())
            if head:
                if current:
                    fields[current] = buf
                current, buf = head.group(1).strip().lower(), []
                continue
            if line.strip().startswith("## "):
                break
            if current:
                buf.append(line)
        if current:
            fields[current] = buf

        narration = collapse(" ".join(fields.get("narration") or []))
        if narration in ("_(silent)_", ""):
            narration = ""
        caps = []
        for line in fields.get("captions") or []:
            cells = _row_cells(line)
            if len(cells) < 3:
                continue
            at, dur = cells[0], cells[1]
            cap = " ".join(cells[2:]).strip()   # a text cell may contain a pipe
            if not cap or set(at) <= set("-: ") or at.lower() == "at":
                continue                       # header or separator row
            try:
                caps.append({"at": float(at), "duration": float(dur),
                             "text": collapse(cap)})
            except ValueError:
                continue
        scenes[sid] = {"narration": narration, "captions": caps}
    return scenes


def apply_markdown(recipe: dict, parsed: dict) -> tuple:
    """Fold parsed narration/captions into a copy of the recipe.

    Returns (recipe, changes, unknown). Only scenes present in *both* are
    touched: a scene the markdown never mentions keeps whatever the recipe
    says, so an author can edit one section of the file in isolation.
    """
    out = dict(recipe)
    scenes, changes = [], []
    for scene in recipe.get("scenes") or []:
        sid = scene.get("id")
        edit = parsed.get(sid)
        if not edit:
            scenes.append(scene)
            continue
        new = dict(scene)
        before = collapse(scene.get("narration") or "")
        if edit["narration"] != before:
            if edit["narration"]:
                new["narration"] = edit["narration"]
            else:
                new.pop("narration", None)
            changes.append({"scene": sid, "field": "narration",
                            "before": before, "after": edit["narration"],
                            "diff": word_diff(before, edit["narration"])})
        old_caps = [{"at": float(c.get("at", 0)),
                     "duration": float(c.get("duration", 3)),
                     "text": collapse(c.get("text", ""))}
                    if isinstance(c, dict) else
                    {"at": 0.0, "duration": 3.0, "text": collapse(c)}
                    for c in (scene.get("captions") or [])]
        if edit["captions"] != old_caps:
            if edit["captions"]:
                new["captions"] = edit["captions"]
            else:
                new.pop("captions", None)
            changes.append({"scene": sid, "field": "captions",
                            "before": old_caps, "after": edit["captions"]})
        scenes.append(new)
    out["scenes"] = scenes
    known = {s.get("id") for s in recipe.get("scenes") or []}
    return out, changes, sorted(set(parsed) - known)
