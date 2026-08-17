"""Media preparation on the remote machine (all ffmpeg, all remote-side).

The one rule that matters here: **Premiere cannot decode VP9 in an MP4.** It
imports the file without any error, places the AUDIO on the timeline, and
silently drops the video — `overwriteClip` even returns success. Anything that
came out of Google Photos is likely VP9. So `probe` flags it and `prepare`
transcodes everything to H.264 before it is allowed near Premiere.
"""
from __future__ import annotations

import json
import shlex

from . import log
from .transport import Transport

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mts", ".mxf")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".heic")


def _q(p: str) -> str:
    return shlex.quote(p)


def probe(t: Transport, directory: str) -> dict:
    """Inventory a directory: codecs, durations, and anything unusable."""
    script = f'''
D={_q(directory)}
python3 - "$D" <<'PY'
import json, os, subprocess, sys
d = sys.argv[1]
vids, pics, bad = [], [], []
def ff(args):
    return subprocess.run(["ffprobe","-v","error",*args], capture_output=True, text=True).stdout.strip()
if not os.path.isdir(d):
    print(json.dumps({{"error":"not a directory: "+d}})); raise SystemExit(0)
for name in sorted(os.listdir(d)):
    p = os.path.join(d, name)
    if not os.path.isfile(p): continue
    ext = os.path.splitext(name)[1].lower()
    if ext in {list(VIDEO_EXT)!r}:
        codec = ff(["-select_streams","v:0","-show_entries","stream=codec_name","-of","default=nk=1:nw=1",p])
        wh    = ff(["-select_streams","v:0","-show_entries","stream=width,height","-of","csv=p=0",p])
        fps   = ff(["-select_streams","v:0","-show_entries","stream=r_frame_rate","-of","default=nk=1:nw=1",p])
        dur   = ff(["-show_entries","format=duration","-of","default=nk=1:nw=1",p])
        acodec= ff(["-select_streams","a:0","-show_entries","stream=codec_name","-of","default=nk=1:nw=1",p])
        rec = {{"file":name,"codec":codec,"size":wh,"fps":fps,
               "duration":round(float(dur or 0),2),"audio":acodec or None}}
        if codec and codec != "h264":
            rec["problem"] = "Premiere cannot decode %s in MP4 -- imports as audio only" % codec
            bad.append(name)
        vids.append(rec)
    elif ext in {list(PHOTO_EXT)!r}:
        wh = ff(["-select_streams","v:0","-show_entries","stream=width,height","-of","csv=p=0",p])
        pics.append({{"file":name,"size":wh}})
print(json.dumps({{"dir":d,"videos":vids,"photos":pics,
                  "needs_transcode":bad,
                  "total_video_seconds":round(sum(v["duration"] for v in vids),1)}}))
PY
'''
    r = t.run_script(script, timeout=600)
    r.check("probe")
    try:
        return json.loads(r.stdout.strip() or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(f"probe returned unparseable output: {r.stdout[:300]}")


def prepare(t: Transport, src: str, dest: str, crf: int = 16, fps: int = 50) -> dict:
    """Transcode anything that is not H.264 into `dest`; copy the rest."""
    script = f'''
set -e
S={_q(src)}; O={_q(dest)}
mkdir -p "$O"
n_copy=0; n_trans=0; report=""
shopt -s nullglob nocaseglob
for f in "$S"/*.mp4 "$S"/*.mov "$S"/*.m4v "$S"/*.avi "$S"/*.mts; do
  b=$(basename "$f"); stem="${{b%.*}}"
  codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=nk=1:nw=1 "$f")
  out="$O/$stem.mp4"
  if [ "$codec" = "h264" ]; then
    cp -f "$f" "$out"; n_copy=$((n_copy+1)); report="$report$stem:copy "
  else
    ffmpeg -y -loglevel error -i "$f" \
      -c:v libx264 -preset medium -crf {crf} -pix_fmt yuv420p -r {fps} \
      -c:a aac -b:a 192k -movflags +faststart "$out"
    n_trans=$((n_trans+1)); report="$report$stem:$codec->h264 "
  fi
done
echo "{{\\"copied\\":$n_copy,\\"transcoded\\":$n_trans,\\"detail\\":\\"$report\\",\\"dest\\":\\"$O\\"}}"
'''
    r = t.run_script(script, timeout=3600)
    r.check("prepare")
    line = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
    return json.loads(line[-1]) if line else {"raw": r.stdout[-400:]}


def kenburns(t: Transport, out: str, photos: list, seconds: float,
             xfade: float = 0.6, crf: int = 17, fps: int = 50) -> dict:
    """Build a slow zoom/pan sequence from stills, crossfaded together.

    zoompan MUST use d=1 (one output frame per input frame) with -t governing
    the length. Using d=<frames> multiplies duration by that number — it once
    turned a requested 10.3s clip into 373s.
    """
    if len(photos) < 2:
        raise ValueError("kenburns needs at least 2 photos")
    n = len(photos)
    per = round((seconds + xfade * (n - 1)) / n, 3)

    parts = []
    for i, p in enumerate(photos):
        zoom = (f"min(1+0.00022*on,1.16)" if i % 2 == 0 else f"max(1.16-0.00022*on,1.0)")
        parts.append(
            f'ffmpeg -y -loglevel error -loop 1 -framerate {fps} -t {per} -i {_q(p)} '
            f'-vf "scale=2560:-2,zoompan=z=\'{zoom}\':d=1:'
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps={fps},"
            f'setsar=1,format=yuv420p" '
            f'-c:v libx264 -preset veryfast -crf {crf} -an "$T/p{i:02d}.mp4"'
        )

    inputs = " ".join(f'-i "$T/p{i:02d}.mp4"' for i in range(n))
    chain, prev = [], "0"
    for i in range(1, n):
        offset = round(per * i - xfade * i, 3)
        tag = f"x{i}" if i < n - 1 else ""
        seg = f"[{prev}][{i}]xfade=transition=fade:duration={xfade}:offset={offset}"
        chain.append(seg + (f"[{tag}]" if tag else ""))
        prev = tag
    filt = ";".join(chain)

    script = f'''
set -e
T=$(mktemp -d /tmp/umc_kb.XXXXXX)
trap 'rm -rf "$T"' EXIT
{chr(10).join(parts)}
ffmpeg -y -loglevel error {inputs} -filter_complex "{filt}" \
  -c:v libx264 -preset medium -crf {crf} -r {fps} -an {_q(out)}
ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 {_q(out)}
'''
    r = t.run_script(script, timeout=1800)
    r.check("kenburns")
    got = float(r.stdout.strip().splitlines()[-1])
    if abs(got - seconds) > 1.0:
        log.warn(f"kenburns produced {got:.2f}s, expected ~{seconds:.2f}s")
    return {"out": out, "duration": got, "photos": len(photos), "requested": seconds}


def levels(t: Transport, path: str, windows: list) -> list:
    """Mean/peak level per time window.

    Always check per section, never just integrated LUFS: a mix once measured
    a perfectly normal -16 LUFS overall while its opening card sat at -39 dB.
    """
    rows = []
    for label, start, dur in windows:
        r = t.run(
            f"ffmpeg -hide_banner -ss {start} -t {dur} -i {_q(path)} "
            f"-af volumedetect -f null - 2>&1 | grep -E 'mean_volume|max_volume'",
            timeout=180)
        mean = peak = None
        for line in r.stdout.splitlines():
            if "mean_volume" in line:
                mean = line.split("mean_volume:")[-1].strip()
            elif "max_volume" in line:
                peak = line.split("max_volume:")[-1].strip()
        rows.append({"section": label, "start": start, "mean": mean, "peak": peak})
    return rows


def denoise(t: Transport, src: str, out: str, target_lufs: float = -16.0,
            nr: int = 20, presence: float = 3.5, highpass: int = 85,
            timeout: int = 900) -> dict:
    """Clean up a spoken-word recording and bring it to a usable level.

    Aimed at room recordings (testimonials, hall audio), which typically arrive
    with HVAC rumble, boxy low-mids and a level well under the narration.

    Chain, in order and for a reason:
      highpass    kill rumble the mic picked up but nobody wants
      afftdn      adaptive broadband denoise (tn=1 tracks a changing floor)
      -3dB @180   remove boxiness
      +N  @2.6k   presence lift: this is the intelligibility band
      -2dB @7k    tame sibilance the presence lift exaggerates
      compress    even out a speaker who moves relative to the mic
      loudnorm    TWO PASS -- see below

    loudnorm runs in two passes. Single-pass is a streaming normaliser and
    lands several dB off: asking for -16 gave -20.9 in practice. The first pass
    only measures; the second feeds those measurements back so the filter can
    apply a fixed correction and actually hit the target.

    Honest limit: normalising lifts the residual noise floor along with the
    speech. This makes a hall recording usable, not studio-clean.
    """
    clean = (
        f"highpass=f={highpass},"
        f"afftdn=nr={nr}:nf=-32:tn=1,"
        f"equalizer=f=180:t=q:w=1.2:g=-3,"
        f"equalizer=f=2600:t=q:w=1.4:g={presence},"
        f"equalizer=f=7000:t=q:w=2:g=-2,"
        f"acompressor=threshold=-20dB:ratio=3:attack=8:release=180:makeup=2"
    )
    script = f"""
set -e
SRC={_q(src)}; OUT={_q(out)}; TMP=$(mktemp /tmp/umc_dn.XXXXXX).wav
trap 'rm -f "$TMP"' EXIT

measure () {{
  ffmpeg -hide_banner -i "$1" -af ebur128=framelog=quiet -f null - 2>&1 \
    | grep -E "^\\s+I:" | tail -1 | sed 's/.*I: *//'
}}
echo "BEFORE=$(measure "$SRC")"

# pass 1: clean, then MEASURE the cleaned audio
ffmpeg -y -loglevel error -i "$SRC" -vn -af "{clean}" \
  -acodec pcm_s16le -ar 48000 -ac 1 "$TMP"

# parse loudnorm's JSON with python: sed through several escaping layers is
# how this produced a filter arg of '?' and a cryptic Eval error
ARGS=$(ffmpeg -hide_banner -i "$TMP" \
  -af loudnorm=I={target_lufs}:TP=-1.5:LRA=9:print_format=json \
  -f null - 2>&1 | python3 -c "
import json, sys
raw = sys.stdin.read()
# find the JSON block by index: brace escaping through an f-string, a heredoc
# and a shell -c is three chances to get it subtly wrong
a, b = raw.find(chr(123)), raw.rfind(chr(125))
if a < 0 or b < a:
    sys.exit(0)
d = json.loads(raw[a:b + 1])
print(':'.join([
    'measured_I=' + d['input_i'],
    'measured_TP=' + d['input_tp'],
    'measured_LRA=' + d['input_lra'],
    'measured_thresh=' + d['input_thresh'],
    'offset=' + d['target_offset'],
]))
")

if [ -n "$ARGS" ]; then
  ffmpeg -y -loglevel error -i "$TMP" \
    -af "loudnorm=I={target_lufs}:TP=-1.5:LRA=9:$ARGS:linear=true" \
    -acodec pcm_s16le -ar 48000 -ac 1 "$OUT"
  echo "PASSES=2"
else
  ffmpeg -y -loglevel error -i "$TMP" -af "loudnorm=I={target_lufs}:TP=-1.5:LRA=9" \
    -acodec pcm_s16le -ar 48000 -ac 1 "$OUT"
  echo "PASSES=1"
fi

echo "AFTER=$(measure "$OUT")"
echo "DUR=$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$OUT")"
"""
    r = t.run_script(script, timeout=timeout)
    r.check("denoise")
    info = {"src": src, "out": out, "target": f"{target_lufs} LUFS"}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.strip().split("=", 1)
            info[k.lower()] = v.strip()
    try:
        got = float(str(info.get("after", "")).split()[0])
        if abs(got - target_lufs) > 1.5:
            log.warn(f"landed at {got} LUFS, target {target_lufs} — check the file")
    except (ValueError, IndexError):
        pass
    return info


def extract_audio(t: Transport, src: str, out: str, timeout: int = 600) -> dict:
    """Pull the audio out of a clip so it can live on its own track.

    Needed when a clip's own voice should lead (a testimonial) while its video
    sits among muted b-roll: the sync audio gets silenced with everything else,
    and the cleaned copy is placed separately.
    """
    r = t.run_script(f"""
set -e
ffmpeg -y -loglevel error -i {_q(src)} -vn -acodec pcm_s16le -ar 48000 -ac 1 {_q(out)}
ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 {_q(out)}
""", timeout=timeout)
    r.check("extract audio")
    return {"src": src, "out": out,
            "duration": float(r.stdout.strip().splitlines()[-1])}
