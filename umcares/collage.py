"""The paper-collage look (Vox style): cut-out photos on paper, tape, shadows, stop-motion jitter, marker lettering.

What makes the look, and how each part is made with ffmpeg alone:
  * Paper: a flat cream colour with static grain. (No vignette: a vignette turns paper grey and kills the look.)
  * A photo is a cut-out card: halftone print texture, white border, soft drop shadow, two strips of tape, a slight tilt.
    A still's card is built once, as a picture, then animated; a video's card is built on every frame.
  * Stop-motion "boil": the card's tilt and position change a little 12 times a second, like hand-animated paper.
  * Drop-in: each piece arrives from a side (the recipe's `move` picks which) on an ease-out curve while it settles
    from a tilt.
  * Captions are marker lettering on a cream paper label. A title is marker lettering on a tilted highlighter swipe.

Everything is relative to the frame, so a draft at half size is the same picture. Hard cuts: collage never crossfades.

Stdlib only; used by montage.py for templates whose style is "collage".
"""
from __future__ import annotations

import math
from pathlib import Path

from . import textimg

PAPER = "#f1ead8"
INK = "#16130f"
LABEL = "#fffaf0"                 # brighter than the paper, so the label lifts off the page
HIGHLIGHT = "#ffd23f"
TAPE = "#f2e08a"
FONT = "MarkerFelt-Wide"
FONT_THIN = "MarkerFelt-Thin"
BOIL = 12                      # stop-motion frames per second

# where the card may sit, as fractions of the frame: (width, height, centre y when there is a caption, when there is not)
BOX = {"reels": (0.80, 0.50, 0.42, 0.47), "square": (0.76, 0.56, 0.42, 0.48), "wide": (0.60, 0.66, 0.44, 0.50)}
# which way a piece drops in, by the recipe's `move` (the same vocabulary as the other looks): (x, y) direction
ENTRANCE = {"in": (1, 0), "out": (-1, 0), "pan_right": (0, 1), "pan_left": (0, -1), "still": (0, 0)}

_ROT = "ow='hypot(iw,ih)':oh='hypot(iw,ih)'"


def _rand(seed: float) -> str:
    """A pseudo-random number in [0,1) that takes a new value on every stop-motion frame."""
    return f"mod(abs(sin(floor(t*{BOIL})*{seed:.3f})*43758.5453),1)"


def _jit(seed: float, amount: float) -> str:
    return f"{2 * amount:.4f}*({_rand(seed)}-0.5)"


def _drop(start: float, dur: float) -> str:
    """1 -> 0 along an ease-out curve between start and start+dur."""
    return f"pow(1-min(1,max(0,(t-{start})/{dur})),3)"


def _card_body(src: str, *, bw: int, bh: int, border: int, margin: int, period: float, halftone: bool, tapew: int, taph: int,
               tag: str) -> str:
    """Filter text that turns `src` (a label like [0:v]) into [body<tag>]: the taped, bordered, shadowed cut-out.
    Tape colour inputs are expected as the last two inputs referred to as [tp1<tag>in] and [tp2<tag>in]."""
    k = 2 * math.pi / period
    pic = f"{src}scale={bw}:{bh}:force_original_aspect_ratio=decrease,format=yuv420p,eq=contrast=1.1:saturation=0.85"
    if halftone:                                                  # a fine dot screen, like a printed photograph
        pic += f",geq=lum='lum(X,Y)*(0.78+0.22*(0.5+0.5*sin(X*{k:.4f})*sin(Y*{k:.4f})))':cb='cb(X,Y)':cr='cr(X,Y)'"
    off = max(4, border // 2)
    blur = max(3, border)
    return (f"{pic},format=rgba,pad=iw+{2 * border}:ih+{2 * border}:{border}:{border}:color=white,"
            f"pad=iw+{2 * margin}:ih+{2 * margin}:{margin}:{margin}:color=0x00000000,split[c1{tag}][c2{tag}];"
            f"[c2{tag}]colorchannelmixer=rr=0:gg=0:bb=0:aa=0.38,boxblur={blur}:2,pad=iw+{off}:ih+{off}:{off}:{off}:color=0x00000000[sh{tag}];"
            f"[c1{tag}]pad=iw+{off}:ih+{off}:0:0:color=0x00000000[cd{tag}];"
            f"[sh{tag}][cd{tag}]overlay=0:0:format=auto[bd{tag}];"
            f"[tp1{tag}in]format=rgba,colorchannelmixer=aa=0.82,rotate=-0.42:c=0x00000000:{_ROT}[t1{tag}];"
            f"[tp2{tag}in]format=rgba,colorchannelmixer=aa=0.82,rotate=0.5:c=0x00000000:{_ROT}[t2{tag}];"
            f"[bd{tag}][t1{tag}]overlay=x={max(2, margin - tapew // 3)}:y={max(2, margin - taph)}:format=auto[b1{tag}];"
            f"[b1{tag}][t2{tag}]overlay=x='W-w-{max(2, margin - tapew // 3)}':y={max(2, margin - taph)}:format=auto[body{tag}]")


def _tape_inputs(tapew: int, taph: int, d: float, fps: int) -> list:
    return ["-f", "lavfi", "-i", f"color=c={TAPE}:s={tapew}x{taph}:r={fps}:d={d}",
            "-f", "lavfi", "-i", f"color=c={TAPE}:s={tapew}x{taph}:r={fps}:d={d}"]


def _paper(W: int, H: int, fps: int, d: float) -> list:
    return ["-f", "lavfi", "-i", f"color=c={PAPER}:s={W}x{H}:r={fps}:d={d}"]


def _label_png(workdir: Path, text: str, name: str, W: int, H: int) -> tuple:
    """The caption as marker lettering on a cream paper label; returns (png, width, height)."""
    lw, lh = int(W * 0.86), int(H * 0.17)
    png = workdir / f"{name}.png"
    textimg.render(text, png, lw, lh, size=max(14, int(H * 0.040)), font=FONT, color=INK, plate=True, plate_alpha=0.98,
                   plate_color=LABEL, valign="middle", pad=max(4, int(W * 0.04)))
    return png, lw, lh


def _scene(paper: str, card: str, label: str | None, *, cy: float, fx: int, fy: int, tilt: float, seeds: tuple, label_y: float,
           flip: int, W: int, H: int) -> str:
    """The shared second half of a card piece: tilt, wobble and drop the card on the paper, then the marker label.
    `paper`, `card` and `label` are the filter-graph references of those three inputs (the label may be None)."""
    s1, s2, s3, s4 = seeds
    drop = _drop(0, 0.4)
    rot = f"{tilt}+{0.12 * (fx or fy or flip)}*{drop}+{_jit(s1, 0.008)}"
    xexpr = f"(W-w)/2+{fx}*0.45*W*{drop}+{_jit(s2, W * 0.006)}"
    yexpr = f"{cy}*H-h/2+{fy}*0.35*H*{drop}+{_jit(s3, H * 0.004)}"
    graph = (f"{paper}noise=alls=8:allf=u[bg];{card}rotate=a='{rot}':c=0x00000000:{_ROT}[cr];"
             f"[bg][cr]overlay=x='{xexpr}':y='{yexpr}':format=auto[a]")
    last = "[a]"
    if label:
        # a soft shadow under the label (without it, cream on cream disappears), then tilt and wobble it like the card
        sh = max(3, int(min(W, H) * 0.008))
        graph += (f";{label}format=rgba,split[la][lb0];[lb0]colorchannelmixer=rr=0:gg=0:bb=0:aa=0.30,boxblur={sh}:1,"
                  f"pad=iw+{sh}:ih+{sh}:{sh}:{sh}:color=0x00000000[lsh];[la]pad=iw+{sh}:ih+{sh}:0:0:color=0x00000000[lbody];"
                  f"[lsh][lbody]overlay=0:0:format=auto,rotate=a='{-0.04 * flip}+{_jit(s4, 0.008)}':c=0x00000000:{_ROT}[lb];"
                  f"[a][lb]overlay=x='(W-w)/2-0.6*W*{_drop(0.3, 0.4)}+{_jit(s2, W * 0.004)}':"
                  f"y='{label_y}*H-h/2+{_jit(s3, H * 0.003)}':format=auto[l]")
        last = "[l]"
    return graph + f";{last}scale=out_range=tv,format=yuv420p[out]"


def piece_commands(base, ffmpeg: str, plan: dict, seg: dict, i: int, workdir: Path, out: Path, encode: list, root: Path) -> list:
    """The ffmpeg command(s) for one piece of a collage. `base` is montage._base, `encode` the piece encoder arguments."""
    W, H, fps, d = plan["width"], plan["height"], plan["fps"], seg["duration"]
    fx, fy = ENTRANCE.get(seg.get("move") or "in", (1, 0))
    flip = 1 if i % 2 == 0 else -1                                   # alternate the tilt so neighbours do not match
    seeds = (12.9898 + i * 0.37, 78.233 + i * 0.21, 39.346 + i * 0.53, 5.123 + i * 0.11)
    s1, s2, s3, s4 = seeds
    border = max(4, int(min(W, H) * 0.028))
    margin = border * 2
    tapew, taph = max(16, int(W * 0.24)), max(5, int(W * 0.065))
    period = max(4.0, W * 0.0125)
    loop = ["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i"]

    # ---- title and end cards: marker lettering on a tilted highlighter swipe. Rotated images get a square canvas, so every
    # overlay below positions by the image's CENTRE (-h/2), never by its top edge.
    if seg["kind"] in ("title", "end"):
        tw, th = int(W * 0.92), int(H * 0.12)
        title = workdir / f"seg{i}-t.png"
        r = textimg.render(seg["text"], title, tw, th, size=max(14, int(H * 0.066)), font=FONT, color=INK, pad=max(4, int(W * 0.02)))
        hw, hh = int(r["width"]) + int(W * 0.08), int(r["height"]) + int(H * 0.022)
        inputs = _paper(W, H, fps, d) + loop + [str(title), "-f", "lavfi", "-i", f"color=c={HIGHLIGHT}:s={hw}x{hh}:r={fps}:d={d}"]
        has_sub = bool(seg["subtitle"])
        cy = 0.40 if has_sub else 0.46
        pos_x = f"(W-w)/2-(W*0.5)*{_drop(0.05, 0.35)}+{_jit(s2, W * 0.004)}"
        pos_y = f"{cy}*H-h/2+{_jit(s3, H * 0.003)}"
        tilt = f"{-0.03 * flip}+{_jit(s1, 0.002)}"
        graph = (f"[0:v]noise=alls=8:allf=u[bg];"
                 f"[2:v]format=rgba,rotate=a='{tilt}':c=0x00000000:{_ROT}[hl];"
                 f"[1:v]format=rgba,rotate=a='{tilt}':c=0x00000000:{_ROT}[tt];"
                 f"[bg][hl]overlay=x='{pos_x}':y='{pos_y}':format=auto[a];"
                 f"[a][tt]overlay=x='{pos_x}':y='{pos_y}':format=auto[b]")
        last = "[b]"
        if has_sub:
            sub = workdir / f"seg{i}-s.png"
            textimg.render(seg["subtitle"], sub, tw, int(H * 0.08), size=max(12, int(H * 0.036)), font=FONT_THIN, color=INK, pad=max(4, int(W * 0.02)))
            inputs += loop + [str(sub)]
            graph += (f";[3:v]format=rgba,rotate=a='{0.02 * flip}':c=0x00000000:{_ROT}[ss];"
                      f"[b][ss]overlay=x='(W-w)/2+{_jit(s4, W * 0.003)}-(W*0.5)*{_drop(0.3, 0.35)}':"
                      f"y='{cy + 0.085}*H-h/2+{_jit(s3, H * 0.003)}':format=auto[c]")
            last = "[c]"
        graph += f";{last}scale=out_range=tv,format=yuv420p[out]"
        return [(base(ffmpeg) + inputs + ["-filter_complex", graph, "-map", "[out]", "-t", str(d), "-r", str(fps)] + encode + [str(out)], d)]

    # ---- a photo or a clip: the cut-out card, with a marker label when it has a caption
    bwf, bhf, cy_cap, cy_nocap = BOX.get(plan["format"], BOX["reels"])
    bw, bh = int(W * bwf) // 2 * 2, int(H * bhf) // 2 * 2
    cy = cy_cap if seg["text"] else cy_nocap
    label_png = _label_png(workdir, seg["text"], f"seg{i}-label", W, H)[0] if seg["text"] else None
    label_y = min(0.90, cy + bhf / 2 + 0.12)
    common = dict(cy=cy, fx=fx, fy=fy, tilt=0.035 * flip, seeds=seeds, label_y=label_y, flip=flip, W=W, H=H)

    if seg["kind"] == "photo":
        # the card is built ONCE, as a picture (the halftone is a per-pixel filter: too slow to run on every frame), then animated
        card = workdir / f"seg{i}-card.png"
        build = _card_body("[0:v]", bw=bw, bh=bh, border=border, margin=margin, period=period, halftone=True, tapew=tapew, taph=taph, tag="")
        build = build.replace("[tp1in]", "[1:v]").replace("[tp2in]", "[2:v]")
        step1 = base(ffmpeg) + ["-i", str(root / seg["rel"])] + _tape_inputs(tapew, taph, 1, fps) + \
            ["-filter_complex", build, "-map", "[body]", "-frames:v", "1", str(card)]
        inputs = _paper(W, H, fps, d) + loop + [str(card)] + (loop + [str(label_png)] if label_png else [])
        graph = _scene("[0:v]", "[1:v]format=rgba,", "[2:v]" if label_png else None, **common)
        return [(step1, 0), (base(ffmpeg) + inputs + ["-filter_complex", graph, "-map", "[out]", "-t", str(d), "-r", str(fps)] + encode + [str(out)], d)]

    # a clip: its card is built on every frame, inside the one command (inputs: 0 clip, 1 and 2 tape, 3 paper, 4 label)
    speed = seg["speed"]
    fps_f = f"fps={fps}" if speed >= 0.999 else f"framerate=fps={fps}"
    body = _card_body(f"[0:v]setpts=(PTS-STARTPTS)/{speed},{fps_f},", bw=bw, bh=bh, border=border, margin=margin, period=period,
                      halftone=False, tapew=tapew, taph=taph, tag="v").replace("[tp1vin]", "[1:v]").replace("[tp2vin]", "[2:v]")
    inputs = ["-ss", str(seg["src_start"]), "-t", str(seg["src_used"]), "-i", str(root / seg["rel"])] + \
        _tape_inputs(tapew, taph, d, fps) + _paper(W, H, fps, d) + (loop + [str(label_png)] if label_png else [])
    graph = body + ";" + _scene("[3:v]", "[bodyv]", "[4:v]" if label_png else None, **common)
    return [(base(ffmpeg) + inputs + ["-filter_complex", graph, "-map", "[out]", "-t", str(d), "-r", str(fps)] + encode + [str(out)], d)]
