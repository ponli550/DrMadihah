#!/usr/bin/env python3
"""Render the scene 5 and scene 7 statistic cards via json2video.

Form: KPI row of stat tiles. Per the dataviz guidance, a handful of headline
numbers is a stat-tile row, not a bar chart -- the number IS the chart.

Colour roles (all validated for WCAG contrast on the #0d2847 surface):
  value  #ffffff  14.9:1   label  #a9b6c9  7.2:1   eyebrow  #ffd166  10.3:1
Values stay in primary ink; gold is reserved for the eyebrow, so colour never
carries the data.

COORDINATE MODEL (learned the hard way, costs credits to rediscover):
  x  -- absolute from left, but the text is CENTRED inside `width`
  y  -- an OFFSET FROM VERTICAL CENTRE, so on-screen centre = 540 + y
        (anything placed past y=540 silently falls off a 1080 canvas)
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

SURFACE = "#0d2847"
INK = "#ffffff"
MUTED = "#a9b6c9"
GOLD = "#ffd166"
FONT = "Inter"


def cy(screen_y):
    """Convert a real on-screen centre to json2video's y offset."""
    return screen_y - 540


def txt(text, x, screen_y, width, size, color, start, duration,
        weight=None, spacing=None):
    s = {"font-family": FONT, "font-size": "%dpx" % size, "color": color}
    if weight:
        s["font-weight"] = weight
    if spacing:
        s["letter-spacing"] = spacing
    return {"type": "text", "text": text, "position": "custom",
            "x": x, "y": cy(screen_y), "width": width,
            "start": start, "duration": duration, "settings": s}


def tile(value, label, x, width, value_y, label_y, value_size, label_size,
         start, duration):
    return [
        txt(value, x, value_y, width, value_size, INK, start, duration, weight="700"),
        txt(label, x, label_y, width, label_size, MUTED, start + 0.3, duration - 0.3),
    ]


def scene5(duration):
    els = [txt("IMPAK PROGRAM", 160, 215, 1600, 34, GOLD, 0, duration, spacing="7px")]
    els += tile("84%", "akan lebih berhati-hati menggunakan internet",
                200, 700, 470, 660, 180, 38, 2.5, duration - 2.5)
    els += tile("88%", "tahu tidak boleh tekan pautan pelik",
                1020, 700, 470, 660, 180, 38, 9.5, duration - 9.5)
    els.append(txt("Perbandingan sebelum dan selepas bengkel", 160, 880, 1600, 32, MUTED, 15.0, duration - 15.0))
    return {"background-color": SURFACE, "duration": duration, "elements": els}


def scene7(duration):
    els = [txt("MAKLUM BALAS PESERTA", 160, 165, 1600, 34, GOLD, 0, duration, spacing="7px")]
    # 2x2 grid. Rating leads -- it is the headline figure.
    els += tile("4.77", "rating kepuasan keseluruhan (daripada 5)",
                200, 700, 400, 530, 150, 34, 2.0, duration - 2.0)
    els += tile("92%", "bersetuju bengkel mudah difahami",
                1020, 700, 400, 530, 150, 34, 7.0, duration - 7.0)
    els += tile("89.3%", "mendapat ilmu yang berguna",
                200, 700, 730, 860, 150, 34, 12.0, duration - 12.0)
    els += tile("72%", "akan berkongsi dengan keluarga",
                1020, 700, 730, 860, 150, 34, 17.0, duration - 17.0)
    return {"background-color": SURFACE, "duration": duration, "elements": els}



def card_open(duration):
    """Opening title card. The band region 330-580 is left empty; the logo
    lockup strip is composited there afterwards with ffmpeg (neither ffmpeg
    build has drawtext, and json2video cannot read local images)."""
    return {"background-color": SURFACE, "duration": duration, "elements": [
        txt("PROGRAM KOMUNITI UM CARES", 160, 215, 1600, 34, GOLD, 0, duration, spacing="7px"),
        txt("Amanah di Dunia Digital", 160, 690, 1600, 74, INK, 0.6, duration - 0.6, weight="700"),
        txt("Lindungi Diri dan Keluarga daripada Scam Siber", 160, 780, 1600, 38, MUTED, 1.2, duration - 1.2),
        txt("No. Geran RU2025-T323A", 160, 870, 1600, 30, MUTED, 1.8, duration - 1.8),
    ]}


def card_close(duration):
    return {"background-color": SURFACE, "duration": duration, "elements": [
        txt("TERIMA KASIH KEPADA", 160, 235, 1600, 34, GOLD, 0, duration, spacing="7px"),
        txt("Yayasan Taqwa   |   ICYM   |   UM Press", 160, 700, 1600, 44, INK, 0.6, duration - 0.6, weight="700"),
        txt("Dihasilkan oleh Universiti Malaya x UM Cares", 160, 790, 1600, 36, MUTED, 1.2, duration - 1.2),
        txt("No. Geran RU2025-T323A", 160, 870, 1600, 30, MUTED, 1.8, duration - 1.8),
    ]}


def load_env():
    env = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    if not env.get("JSON2VIDEO_API_KEY"):
        sys.exit("JSON2VIDEO_API_KEY missing")
    return env


def api(env, method="POST", movie=None, pid=None):
    url = env.get("JSON2VIDEO_ENDPOINT") or "https://api.json2video.com/v2/movies"
    if pid:
        url += "?project=" + pid
    data = json.dumps(movie).encode() if movie else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": env["JSON2VIDEO_API_KEY"], "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--only", choices=["s5","s7","logos"])
    ap.add_argument("--status")
    # Durations come from the measured narration for those scenes.
    ap.add_argument("--s5", type=float, default=21.0)
    ap.add_argument("--s7", type=float, default=24.0)
    args = ap.parse_args()
    env = load_env()

    if args.status:
        d = api(env, "GET", pid=args.status)
        m = d.get("movie", {})
        print("status:", m.get("status"), "duration:", m.get("duration"))
        print("message:", repr(m.get("message"))[:400])
        print("url:", m.get("url"))
        print("quota:", d.get("remaining_quota"))
        return

    if args.only == "logos":
        scenes = [card_open(10.0), card_close(10.0)]
    elif args.only == "s5":
        scenes = [scene5(args.s5)]
    elif args.only == "s7":
        scenes = [scene7(args.s7)]
    else:
        scenes = [scene5(args.s5), scene7(args.s7)]
    movie = {"resolution": "full-hd", "scenes": scenes}
    if args.dry or not args.render:
        print(json.dumps(movie, indent=2)[:2000])
        print("\n(dry run -- pass --render to submit)")
        return
    print(json.dumps(api(env, "POST", movie=movie)))


if __name__ == "__main__":
    main()
