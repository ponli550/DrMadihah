"""The kinetic-type look: a caption or title is spoken in big bold words that pop in one after another, over a darkened picture.

It is a different way of drawing the TEXT of a piece, not of the picture, so montage.py keeps composing photos and clips
exactly as before and asks this module for the words:

  * a caption is split into words (upper case, at most MAX_WORDS) and, if there are more than MAX_LINES, grouped two or
    three to a line, so the block never grows taller than the frame likes;
  * each line is drawn by macOS into its own transparent PNG (heavy condensed face, white, the LAST line in the template's
    accent colour), and laid over the picture with ffmpeg's `overlay`, each one arriving at its own time with a short
    ease-out lift and fade;
  * the times are on the beat when the video is cut to one (a line per beat, halving to half-beats if the caption is long),
    else spread evenly. The first line lands on the piece's first frame when beat-synced, so a hard cut and a word hit together.

Everything is relative to the frame, so a draft at half size is the same picture.
"""
from __future__ import annotations

import math
import re

from . import textimg

FONT = "HelveticaNeue-CondensedBlack"          # on every Mac checked (this one and the Premiere machine)
WHITE = "#ffffff"
MAX_WORDS = 10
MAX_LINES = 5
POP = 0.18                                     # seconds a line takes to arrive
LEAD = 0.08                                    # the first line's delay when not on the beat
LOWEST = 0.92                                  # no line goes below this fraction of the frame height
PHOTO_CENTRE = 0.68                            # captions over a picture sit low, where people have clothes, not faces
END_MARGIN = 0.3                               # the last line has landed this long before the piece ends, at least


def split_words(text: str) -> list:
    """Upper-case words of a caption, at most MAX_WORDS (a longer caption is cut: this look is for short lines)."""
    return re.findall(r"\S+", (text or "").upper())[:MAX_WORDS]


def lines_for(words: list) -> list:
    """One word per line, or two or three per line when that would make more than MAX_LINES."""
    if not words:
        return []
    group = math.ceil(len(words) / MAX_LINES)
    return [" ".join(words[i:i + group]) for i in range(0, len(words), group)]


def schedule(n: int, duration: float, period=None) -> list:
    """When each of n lines arrives (seconds into the piece). On the beat when `period` (seconds per beat) is given."""
    if n <= 0:
        return []
    first = 0.0 if period else LEAD
    latest = max(first, duration - END_MARGIN)
    if period:
        step = period
        while n > 1 and first + (n - 1) * step > latest and step > 0.1:
            step /= 2                                                   # half-beats, quarter-beats: still on the grid
    else:
        step = min(0.5, max(0.18, 0.7 * duration / n))
    if n > 1 and first + (n - 1) * step > latest:
        step = (latest - first) / (n - 1)                                 # a short piece: squeeze, off the grid, rather than overrun
    return [round(first + k * step, 3) for k in range(n)]


def overlay(workdir, name: str, text: str, W: int, H: int, fps: int, duration: float, accent: str, period, first_input: int,
            base: str, out: str, centre=0.52, ink: str = WHITE, shadow: bool = True):
    """The words as ffmpeg inputs and a filter fragment from label `base` to label `out`.

    Returns (extra input args, graph fragment without a leading ';', the lines drawn, their arrival times, y where the block ends)."""
    lines = lines_for(split_words(text))
    if not lines:
        return [], f"{base}null{out}", [], [], int(H * centre)
    times = schedule(len(lines), duration, period)
    line_h = int(min(H * 0.105, W * 0.2))
    block = len(lines) * line_h
    y0 = min(int(H * centre) - block // 2, int(H * LOWEST) - block)         # a tall block is pushed up, never off the bottom
    lift = max(6, int(line_h * 0.35))
    pad = max(4, int(W * 0.05))
    inputs, parts, cur = [], [], base
    for k, (line, ts) in enumerate(zip(lines, times)):
        png = workdir / f"{name}-w{k}.png"
        # textimg keeps `pad` free on every side, so the canvas is taller by the padding and shifted back up: otherwise the
        # padding eats the line's height and the font shrinks to a third of the size asked for
        textimg.render(line, png, W, line_h + 2 * pad, size=int(line_h * 0.92), font=FONT, valign="middle", pad=pad,
                       color=accent if k == len(lines) - 1 else ink, shadow=shadow)
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", str(duration), "-i", str(png)]
        idx = first_input + k
        parts.append(f"[{idx}:v]format=rgba,fade=t=in:st={ts}:d={POP}:alpha=1[w{k}]")
        nxt = out if k == len(lines) - 1 else f"[k{k}]"
        arrive = f"pow(1-min(1,max(0,(t-{ts})/{POP + 0.05})),3)"
        parts.append(f"{cur}[w{k}]overlay=0:'{y0 + k * line_h - pad}+{lift}*{arrive}':format=auto{nxt}")
        cur = nxt
    return inputs, ";".join(parts), lines, times, y0 + block
