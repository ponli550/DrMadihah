"""Text-only pieces for the montage: a quote, an announcement, a countdown. No picture, so they work in every look.

(Not to be confused with cards.py, the json2video title/statistic/logo cards of the Premiere pipeline.)

A card is drawn on the template's background colour with the template's accent, in the kinetic type face; text colour
follows the background (white on a dark look, near-black on a light one such as the paper collage).

  * quote       a big opening mark, the quote wrapped and centred (it fades and lifts in), then " - who said it" in the accent;
  * announce    the title as kinetic lines (words popping in, the last in the accent), then chips for the date, time, place and a
                note, each a rounded accent plate with dark text, arriving one after another;
  * countdown   3, 2, 1 one number per equal share of the piece (so on the beat when the piece is cut to one), the last in the accent.

Everything is relative to the frame, so a draft at half size is the same picture. The ffmpeg graph is plain `overlay` with time
expressions, the same technique as the other looks.
"""
from __future__ import annotations

from . import kinetic, textimg

SERIF = "Georgia-Bold"
SANS = "HelveticaNeue-Bold"
KINDS = ("quote", "announce", "countdown")
DARK = "#16161a"
LIGHT = "#ffffff"


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ink_for(bg: str) -> str:
    """Text colour that reads on the background: white on a dark one, near-black on a light one."""
    return DARK if luminance(bg) > 0.55 else LIGHT


def accent_text(accent: str, bg: str) -> str:
    """The accent as a TEXT colour: as is on a dark background, darkened on a light one (a highlighter yellow is unreadable on cream)."""
    if luminance(bg) <= 0.55:
        return accent
    h = accent.lstrip("#")
    return "#" + "".join(f"{int(int(h[i:i + 2], 16) * 0.62):02x}" for i in (0, 2, 4))


def _png(workdir, name, text, W, height, pad, **kw):
    """A text PNG whose canvas is taller by the padding on both sides (textimg keeps `pad` free all round, which would otherwise
    squeeze a one-line box and shrink the type). Returns (path, canvas height)."""
    out = workdir / f"{name}.png"
    h = height + 2 * pad
    textimg.render(text, out, W, h, pad=pad, **kw)
    return out, h


def _fade(idx: int, label: str, start: float, dur: float = 0.3, end=None) -> str:
    fade_out = f",fade=t=out:st={max(start + dur, end - 0.2):g}:d=0.2:alpha=1" if end else ""
    return f"[{idx}:v]format=rgba,fade=t=in:st={start:g}:d={dur:g}:alpha=1{fade_out}[{label}]"


def _lift(start: float, dur: float, px: int) -> str:
    return f"{px}*pow(1-min(1,max(0,(t-{start:g})/{dur:g})),3)"


BACKGROUNDS = ("gradient", "grid", "glow", "glass", "vapor")
VAPOR_HORIZON = 0.78                                    # where the horizon is, as a fraction of the height: low, so the middle of the frame stays clear sky for text


def _blend(a: str, b: str, share: float) -> str:
    """The colour `share` of the way from b to a (hex in, hex out)."""
    x, y = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(f"{round(int(y[i:i + 2], 16) + (int(x[i:i + 2], 16) - int(y[i:i + 2], 16)) * share):02x}" for i in (0, 2, 4))


def _vapor(bg: str, accent: str, W: int, H: int, fps, d) -> str:
    """The synthwave scene as one frame computed with `geq` (a sky that warms towards the horizon, a slatted sun on it, a perspective grid floor
    that fades in from the horizon so it does not alias), repeated for the length of the piece."""
    rgb = lambda h: tuple(int(h.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    top, glow = rgb(bg), rgb(_blend(accent, bg, 0.55))
    sun_a, sun_b = rgb(_blend(accent, "#ffffff", 0.45)), rgb(accent)
    line, floor = rgb(_blend(accent, "#ffffff", 0.35)), rgb(_blend(bg, "#000000", 0.35))
    yh = H * VAPOR_HORIZON
    R = W * 0.17
    ys = yh - R * 0.10
    slat = H * 0.012
    near = f"max(Y-{yh:.1f},0.5)"

    def chan(k):
        sky = f"({top[k]}+({glow[k]}-{top[k]})*pow(clip((Y-{H * 0.35:.1f})/{yh - H * 0.35:.1f},0,1),2.2))"
        sun = f"({sun_a[k]}+({sun_b[k]}-{sun_a[k]})*clip((Y-{ys - R:.1f})/{2 * R:.1f},0,1))"
        return (f"st(0,hypot(X-{W / 2:.1f},Y-{ys:.1f})/{R:.1f});"
                f"st(1,lt(ld(0),1)*lt(Y,{yh:.1f})*gt(mod(Y,{slat:.2f}),{slat:.2f}*clip((Y-{ys - R * 0.2:.1f})/{R * 1.2:.1f},0,0.8)));"            # the sun, cut by slats that thicken downwards
                f"st(2,max(pow(1-abs(2*mod({H * 0.072:.2f}/{near},1)-1),8),pow(1-abs(2*mod((X-{W / 2:.1f})/{near}*0.9,1)-1),14))"            # cross lines and lines that run to the vanishing point
                f"*clip((Y-{yh:.1f})/{H * 0.035:.1f},0,1));"                                                                                    # fading in from the horizon
                f"if(gt(Y,{yh:.1f}),{floor[k]}+({line[k]}-{floor[k]})*clip(ld(2),0,1),{sky}+({sun}-{sky})*ld(1))")
    expr = ":".join(f"{c}='{chan(k)}'" for c, k in zip("rgb", range(3)))
    frames = max(1, round(float(d) * float(fps)))
    return (f"color=c=black:s={W}x{H}:r={fps}:d=1,trim=end_frame=1,format=gbrp,geq={expr},"
            f"loop=loop=-1:size=1,setpts=N/({fps}*TB),trim=end_frame={frames}")


def backdrop(kind, bg: str, accent: str, W: int, H: int, fps, d) -> str:
    """The lavfi source a card is drawn on: the flat colour, or a quiet gradient, grid or glow of the same colour (kind from the recipe)."""
    if kind == "gradient":      # a diagonal drift from the background towards a tint of the accent
        return f"gradients=s={W}x{H}:r={fps}:d={d}:c0={bg}:c1={_blend(accent, bg, 0.16)}:x0=0:y0=0:x1={W}:y1={H}:speed=0"
    if kind == "glow":          # a soft pool of accent light behind the middle of the card
        return f"gradients=s={W}x{H}:r={fps}:d={d}:c0={_blend(accent, bg, 0.22)}:c1={bg}:x0={W // 2}:y0={int(H * 0.45)}:x1={W // 2}:y1={int(H * 0.45 + W * 0.9)}:type=radial:speed=0"
    if kind == "grid":          # faint lines, an engineering-paper look
        cell = max(8, W // 12)
        return f"color=c={bg}:s={W}x{H}:r={fps}:d={d},drawgrid=w={cell}:h={cell}:t={max(1, W // 360)}:c={ink_for(bg)[:7]}@0.13"
    if kind == "vapor":         # a sunset over a perspective grid: the vaporwave look
        return _vapor(bg, accent, W, H, fps, d)
    if kind == "glass":         # soft coloured light, with a frosted plate over most of the frame: the light is blurred and veiled inside it
        rot = lambda c, k: "#" + c.lstrip("#")[2 * k:] + c.lstrip("#")[:2 * k]               # the accent with its channels rotated: two more hues
        blobs = ((accent, 0.28, 0.28, 0.24), (rot(accent, 1), 0.80, 0.72, 0.32), (rot(accent, 2), 0.74, 0.16, 0.22))
        parts = []
        for k, (col, cx, cy, size) in enumerate(blobs):
            parts.append(f"gradients=s={W}x{H}:r={fps}:d=1:c0={_blend(col, bg, 0.34)}:c1={bg}:x0={int(W * cx)}:y0={int(H * cy)}:x1={int(W * cx)}:y1={int(H * cy + W * size * 2.4)}"
                         f":type=radial:speed=0[g{k}]")
        for k, (cx, cy) in enumerate(((0.035, 0.88), (0.80, 0.88)), start=len(blobs)):                    # two small bright dots, one in the margin and one under the plate:
            parts.append(f"gradients=s={W}x{H}:r={fps}:d=1:c0={_blend(accent, '#ffffff', 0.75)}:c1={bg}:x0={int(W * cx)}:y0={int(H * cy)}:x1={int(W * cx)}:y1={int(H * cy + W * 0.03)}"
                         f":type=radial:speed=0[g{k}]")                                                    # sharp outside the plate, a smear inside it: that is the frost
        px, py = int(W * 0.07) // 2 * 2, int(H * 0.10) // 2 * 2
        pw, ph = (W - 2 * px) // 2 * 2, int(H * 0.80) // 2 * 2
        frames = max(1, round(float(d) * float(fps)))
        return (";".join(parts) + f";[g0][g1]blend=all_mode=lighten[m];[m][g2]blend=all_mode=lighten[m2];[m2][g3]blend=all_mode=lighten[m3];[m3][g4]blend=all_mode=lighten,trim=end_frame=1,split[sh][bl];"
                f"[bl]gblur=sigma={max(8, W // 18)}:steps=2,crop={pw}:{ph}:{px}:{py}[fr];[sh][fr]overlay={px}:{py},"
                f"drawbox=x={px}:y={py}:w={pw}:h={ph}:color=white@0.07:t=fill,drawbox=x={px}:y={py}:w={pw}:h={ph}:color=white@0.28:t={max(1, W // 540)},"
                f"loop=loop=-1:size=1,setpts=N/({fps}*TB),trim=end_frame={frames}")
    return f"color=c={bg}:s={W}x{H}:r={fps}:d={d}"


def piece_commands(base, ffmpeg, plan, seg, i, workdir, out, encode, tpl):
    """ffmpeg command for one text card, as [(command, expected seconds)]."""
    W, H, fps, d = plan["width"], plan["height"], plan["fps"], seg["duration"]
    ink, bg = ink_for(tpl["bg"]), tpl["bg"]
    accent = tpl["accent"]                                   # plates and fills
    acc_text = accent_text(accent, bg)                       # accent as the colour of text
    shadow = ink == LIGHT                                    # a shadow helps light type and smears dark type
    inputs = ["-f", "lavfi", "-i", backdrop(plan.get("background"), bg, accent, W, H, fps, d)]
    pad = max(4, int(W * 0.06))
    parts, cur, n_in = [], "[0:v]", 1

    def add(path):
        nonlocal n_in
        inputs.extend(["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(path)])
        n_in += 1
        return n_in - 1

    def put(idx, label, start, x, y_expr, dur=0.3, end=None):
        nonlocal cur
        parts.append(_fade(idx, label, start, dur, end))
        nxt = f"[c{len(parts)}]"
        parts.append(f"{cur}[{label}]overlay={x}:'{y_expr}':format=auto{nxt}")
        cur = nxt

    kind = seg["kind"]
    if kind == "quote":
        mark_h = int(H * 0.14)
        mark, mh = _png(workdir, f"seg{i}-mark", "“", W, mark_h, pad, size=int(H * 0.2), font=SERIF, color=acc_text, valign="middle", shadow=shadow)
        body_h = int(H * 0.36)
        body, bh = _png(workdir, f"seg{i}-q", seg["text"], W, body_h, pad, size=int(H * 0.05), font=SERIF, color=ink, valign="middle", shadow=shadow)
        lift = max(6, int(H * 0.02))
        put(add(mark), "mk", 0.0, 0, f"{int(H * 0.2) - pad}", 0.3)
        put(add(body), "qb", 0.25, 0, f"{int(H * 0.36) - pad}+{_lift(0.25, 0.6, lift)}", 0.4)
        if seg.get("by"):
            by, byh = _png(workdir, f"seg{i}-by", "— " + seg["by"], W, int(H * 0.06), pad, size=int(H * 0.032), font=SANS, color=acc_text, valign="middle", shadow=shadow)
            at = min(max(0.9, d * 0.35), max(0.9, d - 1.0))
            put(add(by), "by", at, 0, f"{int(H * 0.76) - pad}+{_lift(at, 0.5, lift)}", 0.4)
    elif kind == "announce":
        more, graph, lines, times, y_end = kinetic.overlay(workdir, f"seg{i}", seg["text"], W, H, fps, d, acc_text, None, 1, "[0:v]", "[t0]", centre=0.3,
                                                           ink=ink, shadow=shadow)
        inputs.extend(more)
        n_in += len(lines)
        parts.append(graph)
        cur = "[t0]"
        chip_h = int(H * 0.06)
        gap = int(H * 0.085)
        y = max(y_end + int(H * 0.04), int(H * 0.5))
        t = (times[-1] if times else 0.0) + 0.45
        lift = max(5, int(H * 0.015))
        for j, line in enumerate(seg.get("lines", [])):
            png, h = _png(workdir, f"seg{i}-chip{j}", line, W, chip_h, pad, size=int(H * 0.034), font=SANS, color=DARK, valign="middle",
                          plate=True, plate_color=accent, plate_alpha=1.0)
            at = min(t + j * 0.3, max(0.3, d - 0.5))
            put(add(png), f"ch{j}", at, 0, f"{y + j * gap - pad}+{_lift(at, 0.4, lift)}", 0.3)
    elif kind == "countdown":
        count = int(seg["count"])
        per = d / count
        size = int(min(H * 0.34, W * 0.7))
        for k in range(count):
            number = count - k
            png, h = _png(workdir, f"seg{i}-n{number}", str(number), W, int(size * 1.15), pad, size=size, font=kinetic.FONT,
                          color=acc_text if number == 1 else ink, valign="middle", shadow=shadow)
            idx = add(png)
            a, b = round(k * per, 3), round((k + 1) * per, 3)
            parts.append(f"[{idx}:v]format=rgba,fade=t=in:st={a:g}:d=0.1:alpha=1,fade=t=out:st={max(a + 0.1, b - 0.08):g}:d=0.08:alpha=1[n{k}]")
            nxt = f"[c{len(parts)}]"
            parts.append(f"{cur}[n{k}]overlay=0:{(H - h) // 2}:enable='between(t,{a:g},{b:g})':format=auto{nxt}")
            cur = nxt
    graph = ";".join(parts) + f";{cur}format=yuv420p[out]"
    return [(base(ffmpeg) + inputs + ["-filter_complex", graph, "-map", "[out]", "-t", str(d), "-r", str(fps)] + encode + [str(out)], d)]
