"""Burned-in subtitles, without libass.

The obvious way to burn subtitles is ffmpeg's `subtitles` filter. It is not
available here: both the local and the remote ffmpeg are built without libass
AND without libfreetype, so neither `subtitles` nor `drawtext` exists. That is
the same gap that forced the logo cards to be composited as images.

So captions are rendered the same way the logos are: as PNGs, overlaid. Text is
drawn with Pillow, which macOS already ships in its system Python, so this needs
nothing installed on either machine. The overlay filter has no external
dependencies at all.

Two details that keep the file small and the encode single-pass:

  * Each cue is a **band** (1920 x ~220) rather than a full frame. Fifty-odd
    full-frame RGBA inputs would cost ffmpeg roughly half a gigabyte of decoded
    image; bands cost a tenth of that.
  * The overlays are chained into the SAME filter graph as the music mix, so the
    delivery is still encoded exactly once. Burning as a second pass would cost
    a whole extra generation of H.264.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from . import log

# Bundled with macOS; the first that exists wins.
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def parse_srt(path: Path) -> list:
    """[(start, end, text)] from an SRT file."""
    def sec(stamp: str) -> float:
        h, m, rest = stamp.strip().split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, _, b = lines[1].partition("-->")
        cues.append((sec(a), sec(b), " ".join(lines[2:]).strip()))
    return cues


def _python_with_pil() -> str:
    """An interpreter that can import Pillow.

    The CLI may be running under a Homebrew python with no Pillow while the
    system python has it, so this looks rather than assumes.
    """
    for cand in (sys.executable, "/usr/bin/python3",
                 "/opt/homebrew/bin/python3", "python3"):
        if not cand:
            continue
        try:
            r = subprocess.run([cand, "-c", "import PIL"], capture_output=True)
        except OSError:
            continue
        if r.returncode == 0:
            return cand
    raise SystemExit(
        "burning subtitles needs Pillow and no interpreter here has it.\n"
        "  /usr/bin/python3 ships with it on macOS — check that it exists,\n"
        "  or set subtitle mode back to `soft`.")


_RENDER = r'''
import json, sys
from PIL import Image, ImageDraw, ImageFont

cfg = json.load(open(sys.argv[1]))
font = ImageFont.truetype(cfg["font"], cfg["size"])
W, BAND, PAD = cfg["width"], cfg["band"], cfg["pad"]
out = []

def wrap(draw, text, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= maxw or not cur:
            cur = trial
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines[:3]

probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
for i, cue in enumerate(cfg["cues"]):
    img = Image.new("RGBA", (W, BAND), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lines = wrap(probe, cue["text"], W - 2 * PAD)
    lh = int(cfg["size"] * 1.32)
    total = lh * len(lines)
    y = BAND - total - cfg["bottom"]
    for line in lines:
        tw = d.textlength(line, font=font)
        x = (W - tw) / 2
        # a plate behind the text keeps it readable over any footage; an
        # outline alone disappears against busy, bright frames
        d.rounded_rectangle([x - 18, y - 6, x + tw + 18, y + lh - 4],
                            radius=8, fill=(0, 0, 0, 150))
        d.text((x, y), line, font=font, fill=cfg["ink"],
               stroke_width=3, stroke_fill=(0, 0, 0, 235))
        y += lh
    p = cfg["dir"] + "/cue_%04d.png" % i
    img.save(p)
    out.append({"png": p, "start": cue["start"], "end": cue["end"]})

json.dump(out, open(cfg["out"], "w"))
'''


def render_cue_pngs(srt_path: Path, out_dir: Path, width: int = 1920,
                    band: int = 240, size: int = 46, bottom: int = 40,
                    pad: int = 140, ink: str = "#ffffff") -> list:
    """Draw every cue as a transparent PNG band. Returns [{png,start,end}]."""
    cues = parse_srt(srt_path)
    if not cues:
        return []
    font = next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
    if not font:
        raise SystemExit(f"no usable font found; looked for {FONT_CANDIDATES}")

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("cue_*.png"):
        old.unlink()

    cfg = {"font": font, "size": size, "width": width, "band": band,
           "bottom": bottom, "pad": pad, "ink": ink,
           "dir": str(out_dir), "out": str(out_dir / "cues.json"),
           "cues": [{"start": s, "end": e, "text": t} for s, e, t in cues]}
    cfg_path = out_dir / "render.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    script = out_dir / "_render.py"
    script.write_text(_RENDER, encoding="utf-8")

    py = _python_with_pil()
    r = subprocess.run([py, str(script), str(cfg_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"caption rendering failed:\n{r.stderr.strip()}")
    made = json.loads((out_dir / "cues.json").read_text(encoding="utf-8"))
    log.ok(f"rendered {len(made)} caption images ({width}x{band}) with {Path(font).name}")
    return made


def overlay_chain(entries: list, first_input: int, src_label: str,
                  out_label: str, height: int = 1080, band: int = 240) -> str:
    """ffmpeg filter chain overlaying each caption band at its own time.

    `first_input` is the ffmpeg input index of the first PNG. Each overlay is
    gated by `enable=between(t,...)`, so one chain covers the whole timeline.
    """
    if not entries:
        return ""
    y = height - band
    parts, prev = [], src_label
    for i, e in enumerate(entries):
        tag = out_label if i == len(entries) - 1 else f"sub{i}"
        parts.append(
            f"[{prev}][{first_input + i}:v]"
            f"overlay=x=0:y={y}:enable='between(t\\,{e['start']:.3f}\\,{e['end']:.3f})'"
            f"[{tag}]")
        prev = tag
    return ";".join(parts)


def patch_chain(patches: list, first_input: int, src_label: str,
                out_label: str) -> str:
    """Replace whole stretches of picture with another clip, in the same pass.

    Used when a visual changes but the Premiere master is expensive (or, as
    happened here, impossible) to re-export: a title card is opaque and
    full-frame, so overlaying it for exactly its own span replaces those frames
    outright. `setpts` shifts the patch onto its timeline position, and
    `enable` keeps it out of every other frame.

    Honest limitation: the MASTER still contains the old picture. This patches
    the DELIVERY only, so a master re-export is still the way to make the two
    agree.
    """
    if not patches:
        return ""
    parts, prev = [], src_label
    for i, p in enumerate(patches):
        idx = first_input + i
        at, dur = float(p["at"]), float(p["duration"])
        parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{at}/TB[p{i}]")
        tag = out_label if i == len(patches) - 1 else f"pv{i}"
        parts.append(
            f"[{prev}][p{i}]overlay=x=0:y=0:eof_action=pass:"
            f"enable='between(t\\,{at}\\,{at + dur})'[{tag}]")
        prev = tag
    return ";".join(parts)
