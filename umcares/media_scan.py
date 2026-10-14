"""Make the media legible to an AI author.

The AI writes the recipe, so it has to know what it is working with. Metadata
alone is not enough — "C0020.mp4, 9.17s, h264" says nothing about whether that
clip shows the speaker or an empty room. So this produces two things:

  1. `manifest.json`  — every file, probed: codec, size, duration, audio,
                        loudness, and whether Premiere can actually use it.
  2. contact sheets   — tiled thumbnails pulled back to the local machine, so
                        a vision-capable model can literally look at the
                        footage and say "frame 6 is the red-shirt speaker".

Both land in a local `.umcares/` directory. The manifest is the durable record;
the sheets are how the AI sees.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from . import log
from .transport import Transport

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mts")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".heic")
AUDIO_EXT = (".wav", ".mp3", ".m4a", ".aac")


def _q(p: str) -> str:
    return shlex.quote(p)


def scan(t: Transport, directory: str, deep: bool = True) -> dict:
    """Probe every media file in a directory."""
    script = f'''
python3 - {_q(directory)} <<'PY'
import json, os, subprocess, sys
d = sys.argv[1]
VID = {list(VIDEO_EXT)!r}
PIC = {list(PHOTO_EXT)!r}
AUD = {list(AUDIO_EXT)!r}

def ff(args):
    return subprocess.run(["ffprobe","-v","error",*args],
                          capture_output=True, text=True).stdout.strip()

def loudness(p):
    out = subprocess.run(
        ["ffmpeg","-hide_banner","-i",p,"-af","ebur128=framelog=quiet","-f","null","-"],
        capture_output=True, text=True).stderr
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("I:"):
            return s.split("I:")[-1].strip()
    return None

if not os.path.isdir(d):
    print(json.dumps({{"error": "not a directory: " + d}})); raise SystemExit(0)

items = []
for name in sorted(os.listdir(d)):
    p = os.path.join(d, name)
    if not os.path.isfile(p) or name.startswith("."):
        continue
    ext = os.path.splitext(name)[1].lower()
    st = os.stat(p)
    rec = {{"file": name, "path": p, "bytes": st.st_size}}

    if ext in VID:
        rec["kind"] = "video"
        rec["codec"] = ff(["-select_streams","v:0","-show_entries","stream=codec_name",
                           "-of","default=nk=1:nw=1",p])
        wh = ff(["-select_streams","v:0","-show_entries","stream=width,height",
                 "-of","csv=p=0",p])
        rec["size"] = wh
        rec["fps"] = ff(["-select_streams","v:0","-show_entries","stream=r_frame_rate",
                         "-of","default=nk=1:nw=1",p])
        dur = ff(["-show_entries","format=duration","-of","default=nk=1:nw=1",p])
        rec["duration"] = round(float(dur or 0), 2)
        rec["audio_codec"] = ff(["-select_streams","a:0","-show_entries","stream=codec_name",
                                 "-of","default=nk=1:nw=1",p]) or None
        if {deep!r} and rec["audio_codec"]:
            rec["loudness"] = loudness(p)
        # the trap: Premiere imports non-h264 as AUDIO ONLY, silently
        rec["premiere_usable"] = (rec["codec"] == "h264")
        if not rec["premiere_usable"]:
            rec["warning"] = ("Premiere cannot decode %s in MP4 - imports as audio only"
                              % rec["codec"])
    elif ext in PIC:
        rec["kind"] = "photo"
        rec["size"] = ff(["-select_streams","v:0","-show_entries","stream=width,height",
                          "-of","csv=p=0",p])
    elif ext in AUD:
        rec["kind"] = "audio"
        dur = ff(["-show_entries","format=duration","-of","default=nk=1:nw=1",p])
        rec["duration"] = round(float(dur or 0), 2)
        if {deep!r}:
            rec["loudness"] = loudness(p)
    else:
        continue
    items.append(rec)

vids = [i for i in items if i["kind"] == "video"]
print(json.dumps({{
    "dir": d,
    "items": items,
    "counts": {{
        "video": len(vids),
        "photo": sum(1 for i in items if i["kind"] == "photo"),
        "audio": sum(1 for i in items if i["kind"] == "audio"),
    }},
    "video_seconds": round(sum(v.get("duration", 0) for v in vids), 1),
    "needs_transcode": [v["file"] for v in vids if not v.get("premiere_usable")],
}}))
PY
'''
    r = t.run_script(script, timeout=1800)
    r.check("scan")
    try:
        return json.loads(r.stdout.strip() or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(f"scan returned unparseable output: {r.stdout[:300]}")


def contact_sheet(t: Transport, directory: str, out_local: Path,
                  kind: str = "video", cols: int = 4, width: int = 400,
                  at_seconds: float = 1.0) -> dict:
    """Tile thumbnails into one image and bring it back locally.

    Frame order is the sorted filename order, and that order is returned so the
    AI can map "tile 6" back to a real filename.
    """
    exts = VIDEO_EXT if kind == "video" else PHOTO_EXT
    # `nocaseglob` already matches .MP4 as well as .mp4 — adding explicit
    # uppercase patterns makes every file match twice and duplicates the tiles.
    pattern = " ".join(f'"$D"/*{e}' for e in exts)
    remote_out = f"/tmp/umc_sheet_{kind}.jpg"

    script = f'''
set -e
D={_q(directory)}
T=$(mktemp -d /tmp/umc_cs.XXXXXX)
trap 'rm -rf "$T"' EXIT
shopt -s nullglob nocaseglob
i=0
: > "$T/order.txt"
for f in {pattern}; do
  [ -f "$f" ] || continue
  i=$((i+1))
  basename "$f" >> "$T/order.txt"
  if [ "{kind}" = "video" ]; then
    ffmpeg -y -loglevel error -ss {at_seconds} -i "$f" -frames:v 1 \
      -vf "scale={width}:-2" -q:v 5 "$(printf "$T/%03d.jpg" $i)" 2>/dev/null || \
    ffmpeg -y -loglevel error -i "$f" -frames:v 1 -vf "scale={width}:-2" -q:v 5 \
      "$(printf "$T/%03d.jpg" $i)"
  else
    ffmpeg -y -loglevel error -i "$f" -vf "scale={width}:-2" -q:v 5 \
      "$(printf "$T/%03d.jpg" $i)"
  fi
done
[ "$i" -gt 0 ] || {{ echo "NO_FILES"; exit 0; }}
rows=$(( (i + {cols} - 1) / {cols} ))
ffmpeg -y -loglevel error -pattern_type glob -i "$T/*.jpg" \
  -filter_complex "tile={cols}x$rows" -frames:v 1 -update 1 -q:v 6 {remote_out}
echo "COUNT=$i"
echo "---ORDER---"
cat "$T/order.txt"
'''
    r = t.run_script(script, timeout=1800)
    r.check("contact sheet")
    if "NO_FILES" in r.stdout:
        return {"kind": kind, "count": 0, "sheet": None, "order": []}

    order, seen = [], False
    count = 0
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith("COUNT="):
            count = int(s.split("=", 1)[1] or 0)
        elif s == "---ORDER---":
            seen = True
        elif seen and s:
            order.append(s)

    out_local.parent.mkdir(parents=True, exist_ok=True)
    t.pull(remote_out, out_local)
    return {"kind": kind, "count": count, "sheet": str(out_local),
            "cols": cols, "order": order}


def write_markdown(manifest: dict, sheets: list, out: Path) -> Path:
    """A briefing an AI (or a person) can read before writing a recipe."""
    lines = [
        "# Media inventory",
        "",
        f"Source: `{manifest.get('dir')}`",
        "",
        "| | count |",
        "|---|---|",
    ]
    for k, v in (manifest.get("counts") or {}).items():
        lines.append(f"| {k} | {v} |")
    lines += ["", f"Total video: **{manifest.get('video_seconds', 0)}s**", ""]

    bad = manifest.get("needs_transcode") or []
    if bad:
        lines += [
            "## Not usable as-is",
            "",
            "Premiere imports these as **audio only, with no error**. "
            "Run `umcares media prepare` before referencing them in a recipe.",
            "",
        ] + [f"- `{b}`" for b in bad] + [""]

    for s in sheets:
        if not s.get("sheet"):
            continue
        lines += [
            f"## Contact sheet — {s['kind']} ({s['count']} items, {s['cols']} per row)",
            "",
            f"![{s['kind']}]({Path(s['sheet']).name})",
            "",
            "Tile order (left to right, top to bottom):",
            "",
        ]
        lines += [f"{i}. `{n}`" for i, n in enumerate(s.get("order", []), 1)]
        lines.append("")

    lines += ["## Files", "", "| file | kind | detail |", "|---|---|---|"]
    for it in manifest.get("items", []):
        if it["kind"] == "video":
            detail = (f"{it.get('codec')} {it.get('size')} "
                      f"{it.get('duration')}s audio={it.get('audio_codec')}")
        elif it["kind"] == "audio":
            detail = f"{it.get('duration')}s {it.get('loudness') or ''}"
        else:
            detail = str(it.get("size"))
        lines.append(f"| `{it['file']}` | {it['kind']} | {detail} |")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def merge(manifests: list) -> dict:
    """Combine per-directory manifests into one.

    A recipe usually draws from several folders (edit_ready for clips, photos
    for stills, music for beds), so validation needs a single view. Filenames
    must stay unique across them — a duplicate is reported rather than
    silently shadowed, because picking the wrong one is a class of bug that
    only shows up in the finished video.
    """
    items, dirs, dupes = [], [], {}
    seen = {}
    for m in manifests:
        if not m or m.get("error"):
            continue
        dirs.append(m.get("dir"))
        for it in m.get("items", []):
            name = it["file"]
            if name in seen and seen[name] != it["path"]:
                dupes.setdefault(name, [seen[name]]).append(it["path"])
                continue
            seen[name] = it["path"]
            items.append(it)

    vids = [i for i in items if i["kind"] == "video"]
    return {
        "dir": " + ".join(d for d in dirs if d),
        "dirs": dirs,
        "items": items,
        "counts": {
            "video": len(vids),
            "photo": sum(1 for i in items if i["kind"] == "photo"),
            "audio": sum(1 for i in items if i["kind"] == "audio"),
        },
        "video_seconds": round(sum(v.get("duration", 0) for v in vids), 1),
        "needs_transcode": [v["file"] for v in vids if not v.get("premiere_usable")],
        "duplicate_names": dupes,
    }
