"""Explainer cards for the montage: pieces that show an idea step by step instead of a picture. No picture, so they work in every look.

The same technique as textcards.py (text drawn to PNGs by macOS, laid on the template's background with timed `overlay` expressions),
for the scenes of an animated explainer that a text card cannot do:

  * checklist   a title, then items arriving one after another, each followed by a tick;
  * progress    a label, a number that counts up and a bar that fills as it does;
  * typing      a code-editor window (traffic-light dots, a title bar) in which text types itself;
  * list        rows that slide in from the side one after another: meeting invites, notifications;
  * flow        2 to 5 steps as boxes joined by arrows that light up one after another: a pipeline;
  * versus      a "before" panel (red crosses) and an "after" panel (green ticks): the before dims when the after arrives.

As everywhere in the montage, the recipe says WHAT to show and the renderer works out every time: when each item arrives follows from how
many there are and how long the piece is, the count is eased and sampled, and nothing here is a number the recipe has to compute.
"""
from __future__ import annotations

from . import kinetic, textcards, textimg

KINDS = ("checklist", "progress", "typing", "list", "flow", "versus")
MAX_ITEMS = 6
ITEM_STEP = 0.6                    # seconds from one item to the next (less when the piece is short)
FIRST_ITEM = 0.7                   # the title has landed by then
TICK_DELAY = 0.3                   # a tick follows its item
COUNT_STEPS = 24                   # a count is shown in at most this many steps (each is a PNG the renderer must draw)
COUNT_START = 0.4
TYPE_STEPS = 48                    # typing is shown in at most this many pictures (each is a PNG the renderer must draw)
TYPE_START = 0.6
MAX_LINES, MAX_LINE = 8, 44        # a code window: lines, and characters a line (so the type stays big enough to read on a phone)
MONO = "Menlo-Regular"             # on every Mac; a character is 0.602 of the type size wide
MONO_W = 0.602
MAX_STEPS = 5
RED, GREEN = "#ff5f56", "#27c93f"       # what is wrong and what is right keep their colours in every look (darkened as text on a light one)
NBSP = "\u00a0"
ALLOWED = {"checklist": {"title", "items"}, "progress": {"label", "to", "from", "unit"}, "typing": {"text", "title"}, "list": {"title", "items", "from"},
           "flow": {"title", "steps"}, "versus": {"title", "before", "after"}}
SIDE_ALLOWED = {"label", "items"}


# ------------------------------------------------------------------ the recipe
def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e7


def check(v, kind: str, w: str, problems: list) -> None:
    """Problems with the value of a `checklist` or `progress` clip, in the montage's own wording."""
    if not isinstance(v, dict):
        example = {"checklist": '{"title": "Tools", "items": ["Board", "Git"]}', "progress": '{"label": "Done", "to": 100, "unit": "%"}',
                   "typing": '{"title": "sprint.ts", "text": "const x = 1;"}', "list": '{"title": "Hari ini", "items": ["Standup 09:00", "Retro 16:00"]}',
                   "flow": '{"steps": ["Kod", "Binaan", "Ujian", "Keluaran"]}',
                   "versus": '{"before": {"label": "Dulu", "items": ["Manual"]}, "after": {"label": "Kini", "items": ["Automatik"]}}'}[kind]
        problems.append(f"{w}.{kind}: an object like {example}")
        return
    for k in sorted(set(v) - ALLOWED[kind]):
        problems.append(f"{w}.{kind}.{k}: unknown field (allowed: {', '.join(sorted(ALLOWED[kind]))})")
    if kind in ("checklist", "list"):
        if kind == "checklist" or "title" in v:
            if not isinstance(v.get("title"), str) or not v["title"].strip() or len(v["title"]) > 80:
                problems.append(f"{w}.{kind}.title: text of 1 to 80 characters")
        items, longest = v.get("items"), 40 if kind == "checklist" else 50
        if not isinstance(items, list) or not 2 <= len(items) <= MAX_ITEMS:
            problems.append(f"{w}.{kind}.items: a list of 2 to {MAX_ITEMS} items")
        elif any(not isinstance(x, str) or not x.strip() or len(x) > longest for x in items):
            problems.append(f"{w}.{kind}.items: each item is text of 1 to {longest} characters")
        if kind == "list" and v.get("from", "right") not in ("right", "left"):
            problems.append(f"{w}.list.from: right or left (the side the rows slide in from)")
        return
    if kind == "flow":
        if "title" in v and (not isinstance(v["title"], str) or not v["title"].strip() or len(v["title"]) > 60):
            problems.append(f"{w}.flow.title: text of 1 to 60 characters")
        steps = v.get("steps")
        if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_STEPS:
            problems.append(f"{w}.flow.steps: a list of 2 to {MAX_STEPS} steps")
        elif any(not isinstance(x, str) or not x.strip() or len(x) > 18 for x in steps):
            problems.append(f"{w}.flow.steps: each step is text of 1 to 18 characters (they sit in boxes)")
        return
    if kind == "versus":
        if "title" in v and (not isinstance(v["title"], str) or not v["title"].strip() or len(v["title"]) > 60):
            problems.append(f"{w}.versus.title: text of 1 to 60 characters")
        for side in ("before", "after"):
            part = v.get(side)
            if not isinstance(part, dict):
                problems.append(f"{w}.versus.{side}: an object like {{\"label\": \"Dulu\", \"items\": [\"Manual\"]}}")
                continue
            for k in sorted(set(part) - SIDE_ALLOWED):
                problems.append(f"{w}.versus.{side}.{k}: unknown field (allowed: items, label)")
            if not isinstance(part.get("label"), str) or not part["label"].strip() or len(part["label"]) > 20:
                problems.append(f"{w}.versus.{side}.label: text of 1 to 20 characters")
            its = part.get("items")
            if not isinstance(its, list) or not 1 <= len(its) <= 4:
                problems.append(f"{w}.versus.{side}.items: a list of 1 to 4 items")
            elif any(not isinstance(x, str) or not x.strip() or len(x) > 30 for x in its):
                problems.append(f"{w}.versus.{side}.items: each item is text of 1 to 30 characters")
        return
    if kind == "typing":
        text = v.get("text")
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{w}.typing.text: the text that types itself, up to {MAX_LINES} lines of {MAX_LINE} characters")
        else:
            lines = text.rstrip("\n").split("\n")
            if len(lines) > MAX_LINES or any(len(x) > MAX_LINE for x in lines):
                problems.append(f"{w}.typing.text: at most {MAX_LINES} lines of {MAX_LINE} characters (it has to stay big enough to read on a phone)")
        if "title" in v and (not isinstance(v["title"], str) or not v["title"].strip() or len(v["title"]) > 30):
            problems.append(f"{w}.typing.title: the window's title, 1 to 30 characters, e.g. sprint.ts")
        return
    if not isinstance(v.get("label"), str) or not v["label"].strip() or len(v["label"]) > 40:
        problems.append(f"{w}.progress.label: text of 1 to 40 characters")
    if not _is_number(v.get("to")) or v["to"] < 0:
        problems.append(f"{w}.progress.to: the number it counts up to (0 or more)")
    if "from" in v and (not _is_number(v["from"]) or v["from"] < 0):
        problems.append(f"{w}.progress.from: the number it starts at (0 or more)")
    if "unit" in v and (not isinstance(v["unit"], str) or len(v["unit"]) > 4):
        problems.append(f"{w}.progress.unit: text of at most 4 characters, e.g. %")
    if _is_number(v.get("to")) and _is_number(v.get("from", 0)) and v["to"] == v.get("from", 0):
        problems.append(f"{w}.progress: it must count from one number to a different one")
    if v.get("unit") == "%" and _is_number(v.get("to")) and v["to"] > 100:
        problems.append(f"{w}.progress.to: a percentage goes up to 100")


def times(n: int, d: float) -> list:
    """When each of n items arrives (seconds into a piece of length d): evenly, ending a second before the piece does."""
    first = min(FIRST_ITEM, max(0.2, d * 0.2))
    step = min(ITEM_STEP, max(0.2, (d - first - 0.9) / max(1, n)))
    return [round(first + k * step, 3) for k in range(n)]


def count_window(d: float) -> tuple:
    """(when the count starts, how long it takes) in a piece of length d: it is done a second before the end where there is room."""
    start = min(COUNT_START, d * 0.2)
    return start, round(max(0.5, min(2.4, d - start - 0.8)), 3)


def _ease(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def count_steps(frm: float, to: float, d: float) -> list:
    """[(value, from second, to second)] of the number shown: sampled from an eased count, never more than COUNT_STEPS pictures, the last one
    exactly `to` and held until the end."""
    start, T = count_window(d)
    steps = int(min(COUNT_STEPS, max(2, abs(to - frm))))
    values = []
    for k in range(steps + 1):
        v = frm + (to - frm) * _ease(k / steps)
        v = round(v) if float(frm).is_integer() and float(to).is_integer() else round(v, 1)
        values.append(float(to) if k == steps else v)
    out, last = [], None
    for k, v in enumerate(values):
        at = 0.0 if k == 0 else round(start + T * k / steps, 3)               # the first value is there from the start; value k lands as the bar passes k/steps
        if v == last:
            continue
        out.append([v, at])
        last = v
    return [(v, at, out[j + 1][1] if j + 1 < len(out) else d) for j, (v, at) in enumerate(out)]


def type_window(d: float) -> tuple:
    """(when the typing starts, how long it takes) in a piece of length d: it is done a second before the end, however long the piece is held."""
    return TYPE_START, round(max(0.6, d - TYPE_START - 1.0), 3)


def type_cuts(text: str) -> list:
    """How much of the text is shown at each step: [0, n1, n2, ..., len(text)], at most TYPE_STEPS steps, each a picture."""
    n = len(text)
    steps = min(TYPE_STEPS, n)
    return [0] + sorted({round(n * k / steps) for k in range(1, steps + 1)})


def line_starts(text: str) -> list:
    """Where each line of the text begins, as a character offset."""
    out, at = [], 0
    for line in text.split("\n"):
        out.append(at)
        at += len(line) + 1
    return out


def flow_times(n: int, d: float) -> tuple:
    """(when each of n boxes is drawn dim, when each lights up): the whole pipeline is seen first, then it runs, a second left to see it whole."""
    drawn = [round(0.25 + 0.1 * k, 3) for k in range(n)]
    first = round(0.25 + 0.1 * n + 0.35, 3)
    step = min(0.7, max(0.3, (d - first - 1.0) / max(1, n)))
    return drawn, [round(first + k * step, 3) for k in range(n)]


def versus_times(nb: int, na: int, d: float) -> tuple:
    """(arrival of each before item, arrival of each after item, when the before dims): the after begins a beat after the before is done."""
    first, gap = 0.8, 0.6
    step = min(0.55, max(0.25, (d - first - gap - 1.1) / max(1, nb + na)))
    before = [round(first + k * step, 3) for k in range(nb)]
    start_after = first + nb * step + gap
    after = [round(start_after + k * step, 3) for k in range(na)]
    return before, after, round(start_after - 0.3, 3)


def segment(c: dict) -> dict:
    """The plan's segment for an explainer clip."""
    kind = c["_card"]
    v = c[kind]
    if kind == "flow":
        steps = [x.strip() for x in v["steps"]]
        title = (v.get("title") or "").strip()
        return {"kind": "flow", "text": title or " → ".join(steps), "title": title, "steps": steps,
                "duration": float(c.get("hold", round(0.25 + 0.1 * len(steps) + 0.35 + 0.6 * len(steps) + 1.3, 2)))}
    if kind == "versus":
        side = lambda p: {"label": p["label"].strip(), "items": [x.strip() for x in p["items"]]}
        before, after = side(v["before"]), side(v["after"])
        title = (v.get("title") or "").strip()
        return {"kind": "versus", "text": title or f"{before['label']} → {after['label']}", "title": title, "before": before, "after": after,
                "duration": float(c.get("hold", round(0.8 + 0.55 * (len(before["items"]) + len(after["items"])) + 0.6 + 1.4, 2)))}
    if kind == "typing":
        code = v["text"].rstrip("\n")
        title = (v.get("title") or "").strip()
        default = round(TYPE_START + min(7.0, 0.5 + 0.04 * len(code)) + 1.0, 2)
        return {"kind": "typing", "text": title or code.split("\n")[0].strip(), "code": code, "title": title, "duration": float(c.get("hold", default))}
    if kind == "list":
        items = [x.strip() for x in v["items"]]
        return {"kind": "list", "text": (v.get("title") or items[0]).strip(), "title": (v.get("title") or "").strip(), "items": items, "from": v.get("from", "right"),
                "duration": float(c.get("hold", round(FIRST_ITEM + 0.2 + 0.5 * len(items) + 1.2, 2)))}
    if kind == "checklist":
        items = [x.strip() for x in v["items"]]
        return {"kind": "checklist", "text": v["title"].strip(), "items": items, "duration": float(c.get("hold", round(FIRST_ITEM + 0.2 + ITEM_STEP * len(items) + 1.2, 2)))}
    frm = float(v.get("from", 0))
    return {"kind": "progress", "text": v["label"].strip(), "unit": v.get("unit", ""), "from": frm, "to": float(v["to"]), "duration": float(c.get("hold", 3.4))}


def events(seg: dict) -> list:
    """When, inside the piece, a tick sound belongs: as each checklist item is ticked, and when a count lands."""
    d = float(seg["duration"])
    if seg["kind"] == "checklist":
        return [round(t + TICK_DELAY, 3) for t in times(len(seg["items"]), d)]
    if seg["kind"] == "list":
        return times(len(seg["items"]), d)
    if seg["kind"] == "flow":
        return flow_times(len(seg["steps"]), d)[1]
    if seg["kind"] == "versus":
        b, a, _ = versus_times(len(seg["before"]["items"]), len(seg["after"]["items"]), d)
        return b + a
    if seg["kind"] == "typing":
        start, T = type_window(d)
        n = max(1, len(seg["code"]))
        return [round(start + T * at / n, 3) for at in line_starts(seg["code"])]
    start, T = count_window(d)
    return [round(start + T, 3)]


def _mix(a: str, b: str, share: float) -> str:
    """The colour `share` of the way from b to a (hex in, hex out)."""
    x, y = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(f"{round(int(y[i:i + 2], 16) + (int(x[i:i + 2], 16) - int(y[i:i + 2], 16)) * share):02x}" for i in (0, 2, 4))


# ------------------------------------------------------------------ the picture
def write_sequence(workdir, name: str, frames: list, fps: int):
    """A concat listing of [(png path, from second, to second)]: ffmpeg reads it as one picture that changes at those times."""
    lines = ["ffconcat version 1.0"]
    for path, a, b in frames:
        lines += [f"file '{path}'", f"duration {max(1 / fps, round(b - a, 3)):g}"]
    lines.append(f"file '{frames[-1][0]}'")                          # the concat demuxer wants the last file repeated
    listing = workdir / f"{name}.txt"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return listing


def piece_commands(base, ffmpeg, plan, seg, i, workdir, out, encode, tpl):
    """ffmpeg command for one explainer card, as [(command, expected seconds)]."""
    W, H, fps, d = plan["width"], plan["height"], plan["fps"], seg["duration"]
    bg, ink = tpl["bg"], textcards.ink_for(tpl["bg"])
    accent = tpl["accent"]
    acc_text = textcards.accent_text(accent, bg)
    shadow = ink == textcards.LIGHT
    inputs = ["-f", "lavfi", "-i", f"color=c={bg}:s={W}x{H}:r={fps}:d={d}"]
    pad = max(4, int(W * 0.06))
    parts, cur, n_in = [], "[0:v]", 1

    def add(path):
        nonlocal n_in
        inputs.extend(["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(path)])
        n_in += 1
        return n_in - 1

    def put(idx, label, start, x, y_expr, dur=0.3):
        nonlocal cur
        parts.append(textcards._fade(idx, label, start, dur))
        nxt = f"[c{len(parts)}]"
        parts.append(f"{cur}[{label}]overlay={x}:'{y_expr}':format=auto{nxt}")
        cur = nxt

    lift = max(5, int(H * 0.015))
    if seg["kind"] == "checklist":
        title, th = textcards._png(workdir, f"seg{i}-title", seg["text"], W, int(H * 0.13), pad, size=int(H * 0.05), font=textcards.SANS, color=ink,
                                   valign="middle", shadow=shadow)
        put(add(title), "ttl", 0.0, 0, f"{int(H * 0.16) - pad}+{textcards._lift(0.0, 0.5, lift)}", 0.35)
        n = len(seg["items"])
        pitch = int(min(H * 0.115, H * 0.6 / n))
        row_h = int(pitch * 0.8)
        size = int(min(H * 0.042, row_h * 0.55))
        tick_w = int(size * 1.3)
        for j, (item, at) in enumerate(zip(seg["items"], times(n, d))):
            y = int(H * 0.32) + j * pitch
            margin = int(W * 0.1)                                           # the whole list is one column near the middle, its items aligned on the left
            text_pad = margin + tick_w
            png, _ = textcards._png(workdir, f"seg{i}-item{j}", item, W, row_h, text_pad, size=size, font=textcards.SANS, color=ink, align="left",
                                    valign="middle", shadow=shadow)
            put(add(png), f"it{j}", at, 0, f"{y - text_pad}+{textcards._lift(at, 0.4, lift)}", 0.3)
            tick, _ = textcards._png(workdir, f"seg{i}-tick{j}", "✓", W, row_h, margin, size=int(size * 1.15), font=textcards.SANS, color=acc_text, align="left",
                                     valign="middle", shadow=shadow)
            put(add(tick), f"tk{j}", at + TICK_DELAY, 0, f"{y - margin}", 0.15)
    elif seg["kind"] == "list":
        n = len(seg["items"])
        top = 0.32
        if seg["title"]:
            title, th = textcards._png(workdir, f"seg{i}-title", seg["title"], W, int(H * 0.13), pad, size=int(H * 0.05), font=textcards.SANS, color=ink,
                                       valign="middle", shadow=shadow)
            put(add(title), "ttl", 0.0, 0, f"{int(H * 0.16) - pad}+{textcards._lift(0.0, 0.5, lift)}", 0.35)
        else:
            top = 0.26
        pitch = int(min(H * 0.115, H * 0.6 / n))
        chip_h = int(pitch * 0.74)
        size = int(min(H * 0.036, chip_h * 0.5))
        plate = _mix(ink, bg, 0.16)                                         # a row is a plate a little lighter than the background (darker on a light look)
        side = 1 if seg.get("from", "right") == "right" else -1
        for j, (item, at) in enumerate(zip(seg["items"], times(n, d))):
            y = int(H * top) + j * pitch
            png, _ = textcards._png(workdir, f"seg{i}-row{j}", item, W, chip_h, pad, size=size, font=textcards.SANS, color=ink, valign="middle",
                                    plate=True, plate_color=plate, plate_alpha=1.0, shadow=False)
            idx = add(png)
            parts.append(f"[{idx}:v]format=rgba[r{j}]")
            nxt = f"[c{len(parts)}]"
            slide = f"{side}*W*pow(1-min(1,max(0,(t-{at:g})/0.5)),3)"          # eased: it arrives fast and settles
            parts.append(f"{cur}[r{j}]overlay=x='{slide}':y={y - pad}:format=auto{nxt}")
            cur = nxt
    elif seg["kind"] == "flow":
        n = len(seg["steps"])
        if seg["title"]:
            title, th = textcards._png(workdir, f"seg{i}-title", seg["title"], W, int(H * 0.13), pad, size=int(H * 0.05), font=textcards.SANS, color=ink,
                                       valign="middle", shadow=shadow)
            put(add(title), "ttl", 0.0, 0, f"{int(H * 0.15) - pad}+{textcards._lift(0.0, 0.5, lift)}", 0.35)
            top = 0.3
        else:
            top = 0.22
        pitch = int(min(H * 0.15, H * 0.62 / n))
        chip_h = int(pitch * 0.6)
        size = int(min(H * 0.05, chip_h * 0.6))
        top = max(top, 0.5 - n * pitch / 2 / H + (0.04 if seg["title"] else 0.0))                      # the pipeline sits in the middle of the frame
        width = max(len(x) for x in seg["steps"]) + 4
        dim_plate, dim_text, dim_arrow = _mix(ink, bg, 0.16), _mix(ink, bg, 0.55), _mix(ink, bg, 0.35)
        drawn, lit = flow_times(n, d)
        for k, step in enumerate(seg["steps"]):
            y = int(H * top) + k * pitch
            label = step.strip().center(width, NBSP)                                        # every box the same width
            if k:
                arrow = lambda colour, name: textcards._png(workdir, f"seg{i}-{name}{k}", "↓", W, pitch - chip_h, pad, size=int((pitch - chip_h) * 0.8), font=textcards.SANS,
                                                          color=colour, valign="middle", shadow=False)[0]
                put(add(arrow(dim_arrow, "ad")), f"ad{k}", drawn[k], 0, f"{y - (pitch - chip_h) - pad}", 0.25)
                put(add(arrow(acc_text, "aa")), f"aa{k}", lit[k] - 0.05, 0, f"{y - (pitch - chip_h) - pad}", 0.15)
            dim_png, _ = textcards._png(workdir, f"seg{i}-d{k}", label, W, chip_h, pad, size=size, font=textcards.SANS, color=dim_text, valign="middle",
                                        plate=True, plate_color=dim_plate, plate_alpha=1.0, shadow=False)
            put(add(dim_png), f"dp{k}", drawn[k], 0, f"{y - pad}+{textcards._lift(drawn[k], 0.35, lift)}", 0.25)
            lit_png, _ = textcards._png(workdir, f"seg{i}-l{k}", label, W, chip_h, pad, size=size, font=textcards.SANS, color=textcards.DARK, valign="middle",
                                        plate=True, plate_color=accent, plate_alpha=1.0, shadow=False)
            put(add(lit_png), f"lp{k}", lit[k], 0, f"{y - pad}", 0.15)
    elif seg["kind"] == "versus":
        nb, na = len(seg["before"]["items"]), len(seg["after"]["items"])
        t_before, t_after, t_dim = versus_times(nb, na, d)
        red, green = textcards.accent_text(RED, bg), textcards.accent_text(GREEN, bg)
        row = int(H * 0.075)
        margin = int(W * 0.1)
        longest = max(len(x) for x in seg["before"]["items"] + seg["after"]["items"])
        avail = W - 2 * (margin + int(H * 0.032 * 1.3))                                    # what the text may fill: the same margin is kept on both sides
        size = int(max(H * 0.026, min(H * 0.032, avail / (longest * 0.55))))               # sized to the longest item so a row stays one line where it can
        mark_w = int(size * 1.3)
        if seg["title"]:
            title, th = textcards._png(workdir, f"seg{i}-title", seg["title"], W, int(H * 0.12), pad, size=int(H * 0.045), font=textcards.SANS, color=ink,
                                       valign="middle", shadow=shadow)
            put(add(title), "ttl", 0.0, 0, f"{int(H * 0.11) - pad}+{textcards._lift(0.0, 0.5, lift)}", 0.35)
        block = (nb + na + 3.4) * row + int(H * 0.03)                                                    # both panels, one above the other, centred in the frame
        before_top = max(0.2, 0.5 - block / 2 / H + (0.03 if seg["title"] else 0.0))
        after_top = before_top + ((nb + 1.7) * row + int(H * 0.03)) / H
        panels = [("before", "✗", red, before_top, t_before, -1), ("after", "✓", green, after_top, t_after, 1)]
        for name, mark, colour, top, arrivals, side in panels:
            part = seg[name]
            head, _ = textcards._png(workdir, f"seg{i}-{name}-h", f"{mark}{NBSP}{NBSP}{part['label']}", W, int(row * 1.1), margin, size=int(size * 1.35), font=textcards.SANS,
                                     color=colour, align="left", valign="middle", shadow=shadow)
            hat = arrivals[0] - 0.35 if name == "before" else t_dim
            put(add(head), f"{name}h", max(0.1, hat), 0, f"{int(H * top) - margin}", 0.3)
            for j, (item, at) in enumerate(zip(part["items"], arrivals)):
                y = int(H * top) + int(row * 1.15) + j * row
                text_pad = margin + mark_w
                png, _ = textcards._png(workdir, f"seg{i}-{name}-i{j}", item, W, row, text_pad, size=size, font=textcards.SANS, color=ink, align="left", valign="middle", shadow=shadow)
                idx = add(png)
                parts.append(f"[{idx}:v]format=rgba[{name}r{j}]")
                nxt = f"[c{len(parts)}]"
                parts.append(f"{cur}[{name}r{j}]overlay=x='{side}*W*pow(1-min(1,max(0,(t-{at:g})/0.45)),3)':y={y - text_pad}:format=auto{nxt}")
                cur = nxt
                mk, _ = textcards._png(workdir, f"seg{i}-{name}-m{j}", mark, W, row, margin, size=int(size * 1.15), font=textcards.SANS, color=colour, align="left", valign="middle", shadow=shadow)
                put(add(mk), f"{name}m{j}", at + 0.25, 0, f"{y - margin}", 0.15)
        # when the after arrives the before steps back: a veil of the background colour over its panel
        veil_h = int((after_top - before_top) * H + row * 0.1)
        inputs.extend(["-f", "lavfi", "-i", f"color=c={bg}:s={W}x{veil_h}:r={fps}:d={d}"])
        n_in += 1
        parts.append(f"[{n_in - 1}:v]format=rgba,colorchannelmixer=aa=0.62,fade=t=in:st={t_dim:g}:d=0.4:alpha=1[veil]")
        nxt = f"[c{len(parts)}]"
        parts.append(f"{cur}[veil]overlay=0:{int(H * before_top) - int(row * 0.2)}:format=auto{nxt}")
        cur = nxt
    elif seg["kind"] == "typing":
        code = seg["code"]
        lines_ = code.split("\n")
        longest = max(len(x) for x in lines_)
        win_w = int(W * 0.86) // 2 * 2
        win_x = (W - win_w) // 2
        inner = int(W * 0.04)
        size = int(min(H * 0.03, (win_w - 2 * inner) / (max(longest, 12) * MONO_W)))
        line_h = int(size * 1.5)
        title_h = int(H * 0.05) // 2 * 2
        body_h = (len(lines_) * line_h + 2 * inner) // 2 * 2
        win_y = int(H * 0.46 - (title_h + body_h) / 2)
        start, T = type_window(d)
        # the window: a title bar and a body, each a faint plate on the background, fading in together
        for k, (h_, alpha, y_) in enumerate(((title_h, 0.2, win_y), (body_h, 0.1, win_y + title_h))):
            inputs.extend(["-f", "lavfi", "-i", f"color=c=0x{ink.lstrip('#')}:s={win_w}x{h_}:r={fps}:d={d}"])
            n_in += 1
            parts.append(f"[{n_in - 1}:v]format=rgba,colorchannelmixer=aa={alpha},fade=t=in:st=0:d=0.35:alpha=1[w{k}]")
            nxt = f"[c{len(parts)}]"
            parts.append(f"{cur}[w{k}]overlay={win_x}:{y_}:format=auto{nxt}")
            cur = nxt
        dot = int(title_h * 0.5)
        for k, colour in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
            png, h_ = textcards._png(workdir, f"seg{i}-dot{k}", "●", W, title_h, win_x + int(inner * 0.8) + int(dot * 1.5) * k, size=dot, font=textcards.SANS,
                                     color=colour, align="left", valign="middle", shadow=False)
            put(add(png), f"dt{k}", 0.1 + 0.05 * k, 0, f"{win_y - (h_ - title_h) // 2}", 0.3)
        if seg.get("title"):
            dim = _mix(ink, bg, 0.5)
            png, h_ = textcards._png(workdir, f"seg{i}-wt", seg["title"], W, title_h, pad, size=int(title_h * 0.42), font=MONO, color=dim, valign="middle", shadow=False)
            put(add(png), "wt", 0.2, 0, f"{win_y - (h_ - title_h) // 2}", 0.3)
        frames = []
        cuts = type_cuts(code)
        for k, cut in enumerate(cuts):
            at = 0.0 if k == 0 else round(start + T * cut / len(code), 3)
            end = round(start + T * cuts[k + 1] / len(code), 3) if k + 1 < len(cuts) else d
            if k == 0:
                end = round(start + T * cuts[1] / len(code), 3) if len(cuts) > 1 else d
            png, h_ = textcards._png(workdir, f"seg{i}-t{k}", code[:cut] + "▌", W, body_h, win_x + inner, size=size, font=MONO, color=ink, align="left",
                                     valign="top", shadow=False)
            frames.append((png, at, end))
        pad_t = win_x + inner
        inputs.extend(["-f", "concat", "-safe", "0", "-i", str(write_sequence(workdir, f"seg{i}-typed", frames, fps))])
        n_in += 1
        parts.append(f"[{n_in - 1}:v]fps={fps},format=rgba,fade=t=in:st=0.3:d=0.2:alpha=1[typed]")
        parts.append(f"{cur}[typed]overlay=0:{win_y + title_h + inner - pad_t}:format=auto[ctp]")
        cur = "[ctp]"
    else:
        label, lh = textcards._png(workdir, f"seg{i}-label", seg["text"], W, int(H * 0.1), pad, size=int(H * 0.045), font=textcards.SANS, color=ink,
                                   valign="middle", shadow=shadow)
        put(add(label), "lb", 0.0, 0, f"{int(H * 0.27) - pad}+{textcards._lift(0.0, 0.5, lift)}", 0.35)
        frm, to, unit = float(seg["from"]), float(seg["to"]), seg.get("unit", "")
        size = int(min(H * 0.2, W * 0.5))
        number_h = int(size * 1.2)
        ny = int(H * 0.46) - number_h // 2
        steps = count_steps(frm, to, d)
        frames = []
        for k, (value, a, b) in enumerate(steps):
            png, nh = textcards._png(workdir, f"seg{i}-n{k}", fmt(value) + unit, W, number_h, pad, size=size, font=kinetic.FONT,
                                     color=acc_text if k == len(steps) - 1 else ink, valign="middle", shadow=shadow)
            frames.append((png, a, b))
        inputs.extend(["-f", "concat", "-safe", "0", "-i", str(write_sequence(workdir, f"seg{i}-count", frames, fps))])
        n_in += 1
        parts.append(f"[{n_in - 1}:v]fps={fps},format=rgba[nums]")
        parts.append(f"{cur}[nums]overlay=0:{ny - pad}:format=auto[cn]")
        cur = "[cn]"
        bx, bw, bh = int(W * 0.1), int(W * 0.8) // 2 * 2, int(H * 0.03) // 2 * 2
        by = int(H * 0.62)
        f0, f1 = (frm / 100, to / 100) if unit == "%" else (0.0, 1.0)
        start, T = count_window(d)
        ink_rgb = ink.lstrip("#")
        inputs.extend(["-f", "lavfi", "-i", f"color=c=0x{ink_rgb}:s={bw}x{bh}:r={fps}:d={d}"])
        n_in += 1
        parts.append(f"[{n_in - 1}:v]format=rgba,colorchannelmixer=aa=0.18[track]")                 # a faint track (the colour source's own alpha is dropped)
        parts.append(f"{cur}[track]overlay={bx}:{by}:format=auto[ct]")
        inputs.extend(["-f", "lavfi", "-i", f"color=c={accent}:s={bw}x{bh}:r={fps}:d={d}"])
        n_in += 1
        ease = f"st(0,min(1,max(0,(t-{start:g})/{T:g})));ld(0)*ld(0)*(3-2*ld(0))"
        parts.append(f"[{n_in - 1}:v]format=rgba,scale=w='max(2,trunc({bw}*({f0:g}+({f1 - f0:g})*({ease}))))':h={bh}:eval=frame[fill]")
        parts.append(f"[ct][fill]overlay={bx}:{by}:format=auto[cf]")
        cur = "[cf]"
    graph = ";".join(parts) + f";{cur}format=yuv420p[out]"
    return [(base(ffmpeg) + inputs + ["-filter_complex", graph, "-map", "[out]", "-t", str(d), "-r", str(fps)] + encode + [str(out)], d)]
