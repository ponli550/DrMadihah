"""Explainer cards for the montage: pieces that show an idea step by step instead of a picture. No picture, so they work in every look.

The same technique as textcards.py (text drawn to PNGs by macOS, laid on the template's background with timed `overlay` expressions),
for the scenes of an animated explainer that a text card cannot do:

  * checklist   a title, then items arriving one after another, each followed by a tick;
  * progress    a label, a number that counts up and a bar that fills as it does.

(Not built yet: a window that types text, and rows that slide in from the side.)

As everywhere in the montage, the recipe says WHAT to show and the renderer works out every time: when each item arrives follows from how
many there are and how long the piece is, the count is eased and sampled, and nothing here is a number the recipe has to compute.
"""
from __future__ import annotations

from . import kinetic, textcards, textimg

KINDS = ("checklist", "progress")
MAX_ITEMS = 6
ITEM_STEP = 0.6                    # seconds from one item to the next (less when the piece is short)
FIRST_ITEM = 0.7                   # the title has landed by then
TICK_DELAY = 0.3                   # a tick follows its item
COUNT_STEPS = 24                   # a count is shown in at most this many steps (each is a PNG the renderer must draw)
COUNT_START = 0.4
ALLOWED = {"checklist": {"title", "items"}, "progress": {"label", "to", "from", "unit"}}


# ------------------------------------------------------------------ the recipe
def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e7


def check(v, kind: str, w: str, problems: list) -> None:
    """Problems with the value of a `checklist` or `progress` clip, in the montage's own wording."""
    if not isinstance(v, dict):
        example = '{"title": "Tools", "items": ["Board", "Git"]}' if kind == "checklist" else '{"label": "Done", "to": 100, "unit": "%"}'
        problems.append(f"{w}.{kind}: an object like {example}")
        return
    for k in sorted(set(v) - ALLOWED[kind]):
        problems.append(f"{w}.{kind}.{k}: unknown field (allowed: {', '.join(sorted(ALLOWED[kind]))})")
    if kind == "checklist":
        if not isinstance(v.get("title"), str) or not v["title"].strip() or len(v["title"]) > 80:
            problems.append(f"{w}.checklist.title: text of 1 to 80 characters")
        items = v.get("items")
        if not isinstance(items, list) or not 2 <= len(items) <= MAX_ITEMS:
            problems.append(f"{w}.checklist.items: a list of 2 to {MAX_ITEMS} items")
        elif any(not isinstance(x, str) or not x.strip() or len(x) > 40 for x in items):
            problems.append(f"{w}.checklist.items: each item is text of 1 to 40 characters")
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


def segment(c: dict) -> dict:
    """The plan's segment for a checklist or progress clip."""
    kind = c["_card"]
    v = c[kind]
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
    start, T = count_window(d)
    return [round(start + T, 3)]


# ------------------------------------------------------------------ the picture
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
    else:
        label, lh = textcards._png(workdir, f"seg{i}-label", seg["text"], W, int(H * 0.1), pad, size=int(H * 0.045), font=textcards.SANS, color=ink,
                                   valign="middle", shadow=shadow)
        put(add(label), "lb", 0.0, 0, f"{int(H * 0.27) - pad}+{textcards._lift(0.0, 0.5, lift)}", 0.35)
        frm, to, unit = float(seg["from"]), float(seg["to"]), seg.get("unit", "")
        size = int(min(H * 0.2, W * 0.5))
        number_h = int(size * 1.2)
        ny = int(H * 0.46) - number_h // 2
        steps = count_steps(frm, to, d)
        lines = ["ffconcat version 1.0"]
        for k, (value, a, b) in enumerate(steps):
            last = k == len(steps) - 1
            png, nh = textcards._png(workdir, f"seg{i}-n{k}", fmt(value) + unit, W, number_h, pad, size=size, font=kinetic.FONT,
                                     color=acc_text if last else ink, valign="middle", shadow=shadow)
            lines += [f"file '{png}'", f"duration {max(1 / fps, round(b - a, 3)):g}"]
        lines.append(f"file '{png}'")                                      # the concat demuxer wants the last file repeated
        listing = workdir / f"seg{i}-count.txt"
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        inputs.extend(["-f", "concat", "-safe", "0", "-i", str(listing)])
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
