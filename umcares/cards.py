"""Title, statistic and logo cards.

Split across two renderers because neither can do the whole job:

  * **json2video** lays out text well and has fonts. It cannot read local
    images, so it never sees the logos.
  * **ffmpeg** composites the logos, but neither the local nor the remote build
    has `drawtext` (no libfreetype), so it cannot render a single character.

So a logo card is json2video text with a gap, plus an ffmpeg overlay of a logo
strip into that gap.

Statistic cards are stat tiles, not charts: for a handful of headline numbers
the number IS the chart. Values stay in primary ink and the accent colour is
reserved for the eyebrow, so colour never carries data.

json2video's coordinate model, which is not obvious and costs credits to
rediscover: `x` is absolute from the left BUT the text is centred inside
`width`; `y` is an offset from the VERTICAL CENTRE, so on-screen centre is
`540 + y`. Anything placed past y=540 silently falls off a 1080 canvas.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import log

STYLE = {
    "surface": "#0d2847",
    "ink": "#ffffff",
    "muted": "#a9b6c9",
    "accent": "#ffd166",
    "font": "Inter",
}
STRIP_W, STRIP_H = 1600, 250

# A burned caption of two lines reaches roughly y=918 on a 1080 frame, so the
# logo strip must finish above that or the two collide -- which is exactly what
# happened on the v10 closing card.
LOGO_BOTTOM = 905
LOGO_MAX_W = 1480
# The footnote sits at y=620 and runs to about 655, so the strip starts below
# that; 905 - 235 = 670 leaves clearance at both ends.
LOGO_MAX_H = 235


def cy(screen_y: int) -> int:
    """On-screen centre -> json2video y offset."""
    return screen_y - 540


def _txt(text, x, screen_y, width, size, color, start, duration,
         weight=None, spacing=None, font="Inter"):
    s = {"font-family": font, "font-size": f"{size}px", "color": color}
    if weight:
        s["font-weight"] = weight
    if spacing:
        s["letter-spacing"] = spacing
    return {"type": "text", "text": text, "position": "custom",
            "x": x, "y": cy(screen_y), "width": width,
            "start": round(start, 2), "duration": round(max(0.1, duration), 2),
            "settings": s}


def stats_scene(card: dict, duration: float, style: dict) -> dict:
    """A KPI row/grid. Tiles reveal one at a time, paced across the card."""
    tiles = card.get("tiles") or []
    n = len(tiles)
    els = [_txt(card.get("eyebrow", ""), 160, 215, 1600, 34, style["accent"],
                0, duration, spacing="7px", font=style["font"])] if card.get("eyebrow") else []

    two_row = n > 2
    value_size = 150 if two_row else 180
    positions = []
    for i in range(n):
        col = i % 2
        row = i // 2
        x = 200 if col == 0 else 1020
        vy = (400 if row == 0 else 730) if two_row else 470
        ly = (530 if row == 0 else 860) if two_row else 660
        positions.append((x, vy, ly))

    lead = 2.0
    step = max(1.5, (duration - lead - 2.0) / max(1, n))
    for i, tile in enumerate(tiles):
        x, vy, ly = positions[i]
        at = lead + i * step
        els.append(_txt(str(tile.get("value", "")), x, vy, 700, value_size,
                        style["ink"], at, duration - at, weight="700",
                        font=style["font"]))
        els.append(_txt(str(tile.get("label", "")), x, ly, 700,
                        34 if two_row else 38, style["muted"],
                        at + 0.3, duration - at - 0.3, font=style["font"]))

    if card.get("footnote"):
        at = min(duration - 1, lead + n * step)
        els.append(_txt(card["footnote"], 160, 880 if not two_row else 960, 1600,
                        32, style["muted"], at, duration - at, font=style["font"]))

    return {"background-color": style["surface"], "duration": round(duration, 2),
            "elements": els}


def logo_text_scene(card: dict, duration: float, style: dict) -> dict:
    """Text half of a logo card. The strip is composited in afterwards."""
    els = []
    if card.get("eyebrow"):
        els.append(_txt(card["eyebrow"], 160, 215, 1600, 34, style["accent"],
                        0, duration, spacing="7px", font=style["font"]))
    if card.get("title"):
        els.append(_txt(card["title"], 160, 430, 1600, 74, style["ink"],
                        0.4, duration - 0.4, weight="700", font=style["font"]))
    if card.get("subtitle"):
        els.append(_txt(card["subtitle"], 160, 530, 1600, 38, style["muted"],
                        0.8, duration - 0.8, font=style["font"]))
    if card.get("footnote"):
        els.append(_txt(card["footnote"], 160, 620, 1600, 30, style["muted"],
                        1.2, duration - 1.2, font=style["font"]))
    return {"background-color": style["surface"], "duration": round(duration, 2),
            "elements": els}


# ------------------------------------------------------------ logo strip --
def content_box(path: Path, tol: int = 12) -> tuple:
    """Bounding box of non-white content, by scanning raw pixels.

    cropdetect needs several frames to report anything and a still gives it
    one, so it silently returns nothing. Scanning is deterministic and needs no
    imaging library.
    """
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip().split(",")
    w, h = int(dims[0]), int(dims[1])
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=white:s={w}x{h}",
         "-i", str(path), "-filter_complex", "[0][1]overlay=0:0,format=rgb24",
         "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True).stdout
    if len(raw) < w * h * 3:
        return 0, 0, w, h

    thr = 255 - tol
    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        row = raw[y * w * 3:(y + 1) * w * 3]
        for x in range(w):
            i = x * 3
            if row[i] < thr or row[i + 1] < thr or row[i + 2] < thr:
                min_x = min(min_x, x); max_x = max(max_x, x)
                min_y = min(min_y, y); max_y = max(max_y, y)
    if max_x < 0:
        return 0, 0, w, h
    cx, cy_ = min_x & ~1, min_y & ~1
    return cx, cy_, max(2, (max_x - cx + 1) & ~1), max(2, (max_y - cy_ + 1) & ~1)


def build_strip(logo_dir: Path, filenames: list, out: Path,
                margin: float = 0.04, gap: float = 0.03,
                row_gap: int = 34, vpad: int = 16,
                rows: int | None = None) -> Path:
    """Sponsor logos on white, all at ONE shared height, over one or two rows.

    History, because each attempt fixed the previous one and broke something:

      * Fixed height for every logo CLIPPED the wide marks.
      * Contain-fitting each into an equal slot stopped the clipping but made
        them look like different sizes -- these marks run 2.0:1 to 4.7:1, and a
        row of partners should not read as a hierarchy.
      * Equal slots at one shared height let the widest mark (ICYM, 4.65:1)
        drag every other logo down to a quarter of the available height.
      * One row of six is itself the limit: six marks averaging 3.2:1 across
        1600px can only ever be ~59px tall, which reads as tiny on a 1080 frame.

    So: proportional widths at one shared height, and **two rows** once there
    are five or more logos. Halving the marks per row nearly doubles the height
    each one can take. The canvas is sized to the content instead of being a
    fixed 250px band, so nothing downstream has to guess how much of it is
    whitespace.
    """
    n = len(filenames)
    for fn in filenames:
        if not (logo_dir / fn).exists():
            raise SystemExit(f"logo not found: {logo_dir / fn}")
    if rows is None:
        rows = 2 if n >= 5 else 1

    boxes = [content_box(logo_dir / fn) for fn in filenames]
    aspects = [cw / ch for _, _, cw, ch in boxes]

    per = -(-n // rows)                      # ceil, so row 1 is never short
    groups = [list(range(i, min(i + per, n))) for i in range(0, n, per)]
    rows = len(groups)

    # tallest height every ROW can take while still fitting the strip width
    height = min(
        (STRIP_W * (1 - 2 * margin) - STRIP_W * gap * (len(g) - 1)) /
        sum(aspects[i] for i in g)
        for g in groups)
    height = max(1, int(round(height)))
    widths = [max(1, int(round(height * a))) for a in aspects]

    canvas_h = rows * height + (rows - 1) * row_gap + 2 * vpad
    inputs = ["-f", "lavfi", "-i", f"color=white:s={STRIP_W}x{canvas_h}"]
    for fn in filenames:
        inputs += ["-i", str(logo_dir / fn)]

    fc, prev = [], "0:v"
    for r, g in enumerate(groups):
        span = sum(widths[i] for i in g) + int(STRIP_W * gap) * (len(g) - 1)
        x = (STRIP_W - span) // 2
        y = vpad + r * (height + row_gap)
        for i in g:
            cx, cy_, cw, ch = boxes[i]
            fc.append(f"[{i+1}:v]crop={cw}:{ch}:{cx}:{cy_},"
                      f"scale={widths[i]}:{height}[l{i}]")
            tag = f"v{i}"
            fc.append(f"[{prev}][l{i}]overlay=x={x}:y={y}[{tag}]")
            prev = tag
            x += widths[i] + int(STRIP_W * gap)

    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + inputs +
                   ["-filter_complex", ";".join(fc), "-map", f"[{prev}]",
                    "-frames:v", "1", "-update", "1", "-q:v", "2", str(out)],
                   check=True)
    return out


# ----------------------------------------------------------- json2video --
def _api(env, method="POST", movie=None, pid=None):
    url = env.get("JSON2VIDEO_ENDPOINT") or "https://api.json2video.com/v2/movies"
    if pid:
        url += "?project=" + pid
    data = json.dumps(movie).encode() if movie is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": env["JSON2VIDEO_API_KEY"], "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def render(env: dict, cards: dict, durations: dict, style: dict | None = None,
           poll: int = 8, timeout: int = 1800) -> dict:
    """Render every card as one movie scene; returns the URL and the order."""
    style = {**STYLE, **(style or {})}
    order, scenes = [], []
    for cid, card in cards.items():
        dur = float(card.get("duration") or durations.get(f"card:{cid}") or 10.0)
        kind = card.get("type", "stats")
        scenes.append(stats_scene(card, dur, style) if kind == "stats"
                      else logo_text_scene(card, dur, style))
        order.append({"id": cid, "type": kind, "duration": dur})

    if not scenes:
        return {"order": [], "url": None}

    out = _api(env, "POST", movie={"resolution": "full-hd", "scenes": scenes})
    pid = out.get("project")
    if not pid:
        raise RuntimeError(f"json2video did not return a project id: {out}")

    end = time.time() + timeout
    while time.time() < end:
        time.sleep(poll)
        st = _api(env, "GET", pid=pid).get("movie", {})
        if st.get("status") == "done":
            return {"project": pid, "url": st.get("url"), "order": order}
        if st.get("status") == "error":
            raise RuntimeError(f"card render failed: {st.get('message')}")
    raise RuntimeError(f"card render timed out (project {pid})")


def _fit_strip(path: Path) -> tuple:
    """Scale a strip into the logo box and return (w, h, y) for the overlay."""
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip().split(",")
    sw, sh = int(dims[0]), int(dims[1])
    scale = min(LOGO_MAX_W / sw, LOGO_MAX_H / sh)
    w, h = int(sw * scale) & ~1, int(sh * scale) & ~1
    return w, h, LOGO_BOTTOM - h


def split_and_composite(url: str, order: list, cards: dict, out_dir: Path,
                        logo_dir: Path) -> list:
    """Cut the movie into one clip per card, overlaying logo strips."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "_cards_full.mp4"
    with urllib.request.urlopen(url, timeout=600) as r, open(src, "wb") as fh:
        fh.write(r.read())

    results, t = [], 0.0
    for entry in order:
        cid, dur = entry["id"], entry["duration"]
        dest = out_dir / f"card_{cid}.mp4"
        card = cards.get(cid, {})
        logos = card.get("logos") or []

        if logos:
            strip = build_strip(logo_dir, logos, out_dir / f"strip_{cid}.jpg")
            sw, sh, sy = _fit_strip(strip)
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-ss", f"{t:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                 "-i", str(strip),
                 "-filter_complex",
                 f"[1:v]scale={sw}:{sh}[s];[0:v][s]overlay=x=(W-w)/2:y={sy}",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "17",
                 "-r", "50", "-an", str(dest)], check=True)
        else:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-ss", f"{t:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                 "-c:v", "libx264", "-preset", "medium", "-crf", "17",
                 "-r", "50", "-an", str(dest)], check=True)

        results.append({"card": cid, "file": str(dest), "duration": dur})
        t += dur
    return results
