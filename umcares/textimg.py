"""Text as a transparent PNG, drawn by macOS itself.

Why this exists: the ffmpeg on the Premiere machine has no `drawtext` (no libfreetype) and no libass, so burning
text into a video the usual way is impossible, and a cloud text service would be a new metered API. macOS can draw
text natively, with real fonts, shadows and any script (Malay, Indonesian, emoji), through AppKit. It is reached with
`osascript -l JavaScript`, which every Mac has, so nothing needs installing. The video renderer then lays the PNG
over the picture with ffmpeg's `overlay`.

The text is passed to the script as JSON on the command line, never spliced into script source, so quotes, newlines
and `$(...)` in a caption are just text.

Stdlib only; macOS only (it reports a clear error elsewhere).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_FONT = "HelveticaNeue-Bold"
MIN_SIZE = 18

SCRIPT = r"""
ObjC.import('AppKit');
function run(argv) {
  const spec = JSON.parse(argv[0]);
  const w = spec.width, h = spec.height, pad = spec.pad;
  const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
    $(), w, h, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
  const ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext(ctx);
  const para = $.NSMutableParagraphStyle.alloc.init;
  para.alignment = spec.align === 'left' ? 0 : 1;                  // modern AppKit (NSTextAlignment): 0 left, 1 centre, 2 right
  const rgb = spec.color;
  const color = $.NSColor.colorWithDeviceRedGreenBlueAlpha(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1);
  const str = $(spec.text);
  const maxW = w - 2 * pad, maxH = h - 2 * pad;
  let size = spec.size, attrs, bound;
  // a missing ObjC object is still a truthy wrapper in JXA, so check the font against the installed list
  const installed = ObjC.deepUnwrap($.NSFontManager.sharedFontManager.availableFonts) || [];
  const haveFont = installed.indexOf(spec.font) >= 0;
  for (;;) {                                                        // shrink until the text fits its box
    const font = haveFont ? $.NSFont.fontWithNameSize(spec.font, size) : $.NSFont.boldSystemFontOfSize(size);
    attrs = $.NSMutableDictionary.alloc.init;
    attrs.setObjectForKey(font, $.NSFontAttributeName);
    attrs.setObjectForKey(color, $.NSForegroundColorAttributeName);
    attrs.setObjectForKey(para, $.NSParagraphStyleAttributeName);
    bound = str.boundingRectWithSizeOptionsAttributes($.NSMakeSize(maxW, 100000), $.NSStringDrawingUsesLineFragmentOrigin, attrs);
    if (bound.size.height <= maxH || size <= spec.minSize) break;
    size -= 2;
  }
  const textH = Math.min(bound.size.height, maxH);
  const textW = spec.align === 'left' ? maxW : Math.min(bound.size.width, maxW);
  const x = spec.align === 'left' ? pad : (w - textW) / 2;
  const y = spec.valign === 'top' ? h - pad - textH : (spec.valign === 'bottom' ? pad : (h - textH) / 2);
  if (spec.plate) {                                                 // a soft dark plate so the text reads over any picture
    const p = spec.platePad;
    const pw = Math.min(w, Math.min(bound.size.width, maxW) + 2 * p), ph = Math.min(h, textH + 2 * p);
    const px = spec.align === 'left' ? Math.max(0, x - p) : (w - pw) / 2, py = Math.max(0, y - p);
    $.NSColor.colorWithDeviceRedGreenBlueAlpha(spec.plateColor[0] / 255, spec.plateColor[1] / 255, spec.plateColor[2] / 255, spec.plateAlpha).setFill;
    $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius($.NSMakeRect(px, py, pw, ph), p, p).fill;
  } else if (spec.shadow) {                                         // a soft shadow so light text reads over any picture
    const shadow = $.NSShadow.alloc.init;
    shadow.shadowColor = $.NSColor.colorWithCalibratedWhiteAlpha(0, 0.75);
    shadow.shadowOffset = $.NSMakeSize(0, -Math.max(2, size / 20));
    shadow.shadowBlurRadius = Math.max(4, size / 8);
    attrs.setObjectForKey(shadow, $.NSShadowAttributeName);
  }
  str.drawInRectWithAttributes($.NSMakeRect(x, y, textW + 2, textH + 4), attrs);
  $.NSGraphicsContext.restoreGraphicsState;
  const png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $.NSDictionary.dictionary);
  if (!png.writeToFileAtomically($(spec.out), true)) throw new Error('could not write ' + spec.out);
  return JSON.stringify({size: size, width: bound.size.width, height: textH});
}
"""


class TextError(Exception):
    """The text could not be drawn; the message is meant to be shown."""


def _hex(color: str):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", color or "")
    if not m:
        raise TextError("colour must look like #ffffff")
    n = int(m.group(1), 16)
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255]


def _script_path() -> Path:
    p = Path(tempfile.gettempdir()) / f"umcares-text-{hashlib.sha1(SCRIPT.encode()).hexdigest()[:10]}.js"
    if not p.is_file():
        tmp = p.with_name(p.name + f".{os.getpid()}.part")
        tmp.write_text(SCRIPT, encoding="utf-8")
        os.replace(tmp, p)
    return p


def available() -> bool:
    return shutil.which("osascript") is not None


def render(text: str, out, width: int, height: int, size: int = 64, font: str = DEFAULT_FONT, color: str = "#ffffff",
           align: str = "center", valign: str = "middle", plate: bool = False, plate_alpha: float = 0.45,
           pad: int = 40, min_size: int = MIN_SIZE, timeout: int = 30, plate_color: str = "#000000", shadow: bool = True) -> dict:
    """Draw `text` onto a transparent width x height PNG at `out`. Shrinks the font until the text fits.

    `plate` draws a rounded plate behind the text in `plate_color` (dark glass by default, cream paper for a collage label).
    `shadow` (default on, ignored with a plate) adds a soft dark shadow; turn it off for dark text on a light background.
    Returns {"size", "width", "height"} of the text drawn. Raises TextError with a readable message otherwise.
    """
    text = (text or "").strip()
    if not text:
        raise TextError("there is no text to draw")
    if len(text) > 400:
        raise TextError("the text is too long (400 characters at most)")
    if not (16 <= width <= 4096 and 16 <= height <= 4096):
        raise TextError("the image size is out of range")
    if align not in ("center", "left") or valign not in ("top", "middle", "bottom"):
        raise TextError("align must be center or left, valign top, middle or bottom")
    if not available():
        raise TextError("text overlays need macOS (osascript was not found)")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    spec = {"text": text, "out": str(out), "width": int(width), "height": int(height), "size": int(size), "font": font,
            "color": _hex(color), "align": align, "valign": valign, "plate": bool(plate), "plateAlpha": float(plate_alpha), "plateColor": _hex(plate_color),
            "platePad": max(8, int(size) // 4), "pad": int(pad), "minSize": int(min_size), "shadow": bool(shadow)}
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", str(_script_path()), json.dumps(spec)],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TextError("drawing the text took too long") from None
    except OSError as e:
        raise TextError(f"could not run osascript: {e}") from None
    if r.returncode != 0 or not out.is_file():
        raise TextError("could not draw the text: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1][:200])
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"size": size, "width": 0, "height": 0}
