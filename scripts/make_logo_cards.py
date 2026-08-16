#!/usr/bin/env python3
"""Build the opening and closing logo cards as 1920x1080 stills, with ffmpeg.

Design notes:
  * Brand surface is the same navy (#0d2847) used by the statistic cards.
  * Logos sit on a white lockup band. Several marks (notably AYG) are black
    artwork on transparency and would disappear straight onto navy.
  * Text uses the same role colours as the stat cards: gold eyebrow,
    white primary, muted secondary.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGOS = ROOT / "assets" / "logos"
OUT = ROOT / "video" / "cards"
OUT.mkdir(parents=True, exist_ok=True)

NAVY = "0x0d2847"
GOLD = "0xffd166"
INK = "0xffffff"
MUTED = "0xa9b6c9"
FONT = "/tmp/ff_regular.ttf"
FONT_B = "/tmp/ff_bold.ttf"

BAND_H = 250


def esc(t):
    return t.replace(":", "\\:").replace("'", "")


def build(out_name, logos, band_y, eyebrow, lines):
    """logos: list of (filename, target_height). lines: (text, y, size, colour, bold)."""
    inputs = ["-f", "lavfi", "-i", "color=c=%s:s=1920x1080" % NAVY]
    for fn, _ in logos:
        p = LOGOS / fn
        if not p.exists():
            sys.exit("missing logo: %s" % p)
        inputs += ["-i", str(p)]

    # white lockup band behind the logo row
    fc = ["[0:v]drawbox=x=160:y=%d:w=1600:h=%d:color=white@1:t=fill[bg]" % (band_y, BAND_H)]

    # lay the logos out evenly across the band
    n = len(logos)
    slot = 1600 // n
    prev = "bg"
    for i, (fn, h) in enumerate(logos):
        fc.append("[%d:v]scale=-1:%d[l%d]" % (i + 1, h, i))
        cx = 160 + slot * i + slot // 2
        cy = band_y + BAND_H // 2
        tag = "v%d" % i
        fc.append("[%s][l%d]overlay=x=%d-overlay_w/2:y=%d-overlay_h/2[%s]"
                  % (prev, i, cx, cy, tag))
        prev = tag

    # eyebrow + text lines
    fc.append("[%s]drawtext=fontfile=%s:text='%s':fontcolor=%s:fontsize=34:"
              "x=(w-text_w)/2:y=%d[e]" % (prev, FONT, esc(eyebrow), GOLD, band_y - 150))
    prev = "e"
    for i, (text, y, size, colour, bold) in enumerate(lines):
        tag = "t%d" % i
        fc.append("[%s]drawtext=fontfile=%s:text='%s':fontcolor=%s:fontsize=%d:"
                  "x=(w-text_w)/2:y=%d[%s]"
                  % (prev, FONT_B if bold else FONT, esc(text), colour, size, y, tag))
        prev = tag

    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + [
        "-filter_complex", ";".join(fc), "-map", "[%s]" % prev,
        "-frames:v", "1", "-update", "1", str(OUT / out_name)]
    subprocess.run(cmd, check=True)
    print("wrote", OUT / out_name)


def main():
    # Opening: organiser + ministry + partner
    build("card_open.png",
          [("logo_um_1.png", 150), ("logo_mohe.png", 150), ("logo_ayg.png", 110)],
          band_y=330,
          eyebrow="PROGRAM KOMUNITI UM CARES",
          lines=[
              ("Amanah di Dunia Digital", 660, 76, INK, True),
              ("Lindungi Diri dan Keluarga daripada Scam Siber", 760, 40, MUTED, False),
              ("No. Geran RU2025-T323A", 850, 30, MUTED, False),
          ])

    # Closing: sponsors and thanks
    build("card_close.png",
          [("logo_yayasan_taqwa.png", 150), ("logo_icym.png", 130), ("logo_um_press.png", 120)],
          band_y=360,
          eyebrow="TERIMA KASIH KEPADA",
          lines=[
              ("Yayasan Taqwa  |  ICYM  |  UM Press", 700, 44, INK, True),
              ("Dihasilkan oleh Universiti Malaya x UM Cares", 790, 36, MUTED, False),
              ("No. Geran RU2025-T323A", 870, 30, MUTED, False),
          ])


if __name__ == "__main__":
    main()
