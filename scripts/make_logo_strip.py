#!/usr/bin/env python3
"""Composite a row of logos onto a white strip, sized so they read evenly.

Two problems this solves, both seen in the first cut:
  * Yayasan Taqwa is 334x87 (very wide). Matching logo HEIGHTS made it 576px
    wide inside a 533px slot, so it was clipped.
  * MOHE is 474x474 and UM is 500x300, but both are a small mark surrounded by
    a lot of empty canvas, so height-matching made the actual marks look tiny
    next to AYG.

So: auto-trim the surrounding whitespace first, then CONTAIN each logo inside
its slot box (fit by width AND height) rather than matching a fixed height.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGOS = ROOT / "assets" / "logos"
OUT = ROOT / "video" / "cards"
OUT.mkdir(parents=True, exist_ok=True)

W, H = 1600, 250
PAD_X, PAD_Y = 0.86, 0.74   # fraction of the slot a logo may occupy


def dims(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def content_box(path, tol=12):
    """Bounding box of non-white content, by scanning raw pixels.

    cropdetect needs several frames to report and a still image only gives it
    one, so it silently returned nothing. Decoding to rgb24 over white and
    scanning is deterministic and needs no external imaging library.
    """
    w, h = dims(path)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error",
         "-f", "lavfi", "-i", "color=white:s=%dx%d" % (w, h),
         "-i", str(path),
         "-filter_complex", "[0][1]overlay=0:0,format=rgb24",
         "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    if len(raw) < w * h * 3:
        return 0, 0, w, h

    thr = 255 - tol
    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        row = raw[y * w * 3:(y + 1) * w * 3]
        for x in range(w):
            i = x * 3
            if row[i] < thr or row[i + 1] < thr or row[i + 2] < thr:
                if x < min_x: min_x = x
                if x > max_x: max_x = x
                if y < min_y: min_y = y
                if y > max_y: max_y = y
    if max_x < 0:
        return 0, 0, w, h
    # even numbers keep the scaler happy
    cx, cy = min_x & ~1, min_y & ~1
    cw = max(2, (max_x - cx + 1) & ~1)
    ch = max(2, (max_y - cy + 1) & ~1)
    return cx, cy, cw, ch


def strip(out_name, filenames):
    n = len(filenames)
    slot_w = W // n
    box_w = int(slot_w * PAD_X)
    box_h = int(H * PAD_Y)

    inputs = ["-f", "lavfi", "-i", "color=white:s=%dx%d" % (W, H)]
    for fn in filenames:
        p = LOGOS / fn
        if not p.exists():
            sys.exit("missing logo: %s" % p)
        inputs += ["-i", str(p)]

    fc = []
    prev = "0:v"
    for i, fn in enumerate(filenames):
        p = LOGOS / fn
        cx, cy, cw, ch = content_box(p)
        # contain: scale so the trimmed mark fits inside box_w x box_h
        scale = min(box_w / cw, box_h / ch)
        tw, th = max(1, int(cw * scale)), max(1, int(ch * scale))
        print("  %-24s trim %dx%d -> %dx%d" % (fn, cw, ch, tw, th))
        fc.append("[%d:v]crop=%d:%d:%d:%d,scale=%d:%d[l%d]"
                  % (i + 1, cw, ch, cx, cy, tw, th, i))
        cxpos = slot_w * i + slot_w // 2
        tag = "v%d" % i
        fc.append("[%s][l%d]overlay=x=%d-overlay_w/2:y=(H-overlay_h)/2[%s]"
                  % (prev, i, cxpos, tag))
        prev = tag

    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + [
        "-filter_complex", ";".join(fc), "-map", "[%s]" % prev,
        "-frames:v", "1", "-update", "1", "-q:v", "2", str(OUT / out_name)]
    subprocess.run(cmd, check=True)
    print("wrote %s (%d bytes)\n" % (out_name, (OUT / out_name).stat().st_size))


print("opening strip:")
strip("strip_open.jpg", ["logo_um_1.png", "logo_mohe.png", "logo_ayg.png"])
print("closing strip:")
strip("strip_close.jpg", ["logo_yayasan_taqwa.png", "logo_icym.png", "logo_um_press.png"])
