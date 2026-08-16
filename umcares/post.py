"""Delivery: music bed, ducking, subtitle mux, final encode.

Deliberately separate from the Premiere export. The export is slow (minutes)
and rarely changes; the music balance needs several attempts. Keeping them
apart turns a 12-minute retry into a 4-minute one.

Ducking is a static envelope rather than a sidechain compressor because the
narration positions are known exactly, and a fixed envelope is predictable and
reviewable. The one thing to get right is that ducking must extend past the
END of the last narration line — lifting the music at a scene boundary while a
sentence is still playing is what buried the closing lines once.
"""
from __future__ import annotations

import json
import shlex

from . import log
from .transport import Transport


def _q(p: str) -> str:
    return shlex.quote(p)


def db_to_lin(db: float) -> float:
    return round(10 ** (db / 20.0), 5)


def build_envelope(sections: list) -> str:
    """sections = [[until_seconds, db], ...] -> nested ffmpeg if() expression.

    The final entry's db applies to everything after the previous boundary.
    """
    if not sections:
        return "1.0"
    expr = str(db_to_lin(float(sections[-1][1])))
    for until, db in reversed(sections[:-1]):
        expr = f"if(lt(t,{until}),{db_to_lin(float(db))},{expr})"
    return expr


DEFAULT_SECTIONS = [
    # [until_seconds, dB]  -- music level in each stretch
    [11,   -3],    # opening card: nothing competing, let it play
    [161,  -20],   # under narration
    [185,  -25],   # testimonials: participants' own voices lead
    [228,  -20],   # under narration, INCLUDING the closing lines
    [9999, -3],    # closing card
]


def mix(t: Transport, master: str, music: str, srt: str, out: str,
        sections: list | None = None, music_start: float = 25.0,
        fade_out_at: float | None = None, crf: int = 18,
        lang: str = "msa", timeout: int = 3600) -> dict:
    """Lay music under a master, mux soft subtitles, encode delivery H.264."""
    sections = sections or DEFAULT_SECTIONS
    gain = build_envelope(sections)

    dur = _duration(t, master)
    mdur = _duration(t, music)
    fade_at = fade_out_at if fade_out_at is not None else max(0.0, dur - 3.5)

    # The track is usually shorter than the cut, so crossfade it into itself
    # rather than hard-looping (an audible click otherwise).
    body = max(1.0, mdur - music_start - 2)
    need = dur - body + 3
    second = max(5.0, min(need, mdur - 2))

    filt = (
        f"[1:a]atrim={music_start}:{music_start + body},asetpts=PTS-STARTPTS[m1];"
        f"[2:a]atrim=20:{20 + second},asetpts=PTS-STARTPTS[m2];"
        f"[m1][m2]acrossfade=d=3[mus];"
        f"[mus]volume=eval=frame:volume='{gain}',"
        f"afade=t=in:st=0:d=2,afade=t=out:st={fade_at}:d=3.3,"
        f"aformat=sample_rates=48000:channel_layouts=stereo[musg];"
        f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo[voice];"
        f"[voice][musg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )

    sub_in = f"-i {_q(srt)}" if srt else ""
    sub_map = "-map 2:0" if False else ("-map 3:0" if srt else "")
    sub_codec = (f"-c:s mov_text -metadata:s:s:0 language={lang} "
                 f'-metadata:s:s:0 title="Bahasa Malaysia"') if srt else ""

    script = f'''
set -e
ffmpeg -y -loglevel error -i {_q(master)} -i {_q(music)} -i {_q(music)} {sub_in} \
  -filter_complex "{filt}" \
  -map 0:v:0 -map "[aout]" {sub_map} \
  -c:v libx264 -preset medium -crf {crf} -pix_fmt yuv420p \
  -c:a aac -b:a 192k -ac 2 {sub_codec} \
  -movflags +faststart {_q(out)}
echo "---RESULT---"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate \
  -show_entries format=duration,size -of json {_q(out)}
ffmpeg -hide_banner -i {_q(out)} -af ebur128=framelog=quiet -f null - 2>&1 \
  | grep -E "^\\s+(I|LRA):" | head -2
'''
    r = t.run_script(script, timeout=timeout)
    r.check("mix")
    return _parse_result(r.stdout, out)


def mux_subtitles_only(t: Transport, src: str, srt: str, out: str,
                       lang: str = "msa", timeout: int = 1800) -> dict:
    """Replace the subtitle track without touching video or audio.

    Stream-copies both, so this is fast and cannot change the mix.
    """
    script = f'''
set -e
ffmpeg -y -loglevel error -i {_q(src)} -i {_q(srt)} \
  -map 0:v:0 -map 0:a:0 -map 1:0 -c:v copy -c:a copy \
  -c:s mov_text -metadata:s:s:0 language={lang} \
  -movflags +faststart {_q(out)}
echo "---RESULT---"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
  -show_entries format=duration,size -of json {_q(out)}
'''
    r = t.run_script(script, timeout=timeout)
    r.check("subtitle mux")
    return _parse_result(r.stdout, out)


def verify_subtitles(t: Transport, path: str) -> dict:
    """Pull the subtitle track back out and count cues — proves it really shipped."""
    script = f'''
tmp=$(mktemp /tmp/umc_sub.XXXXXX.srt)
ffmpeg -v error -y -i {_q(path)} -map 0:s:0 -c:s srt "$tmp" 2>/dev/null || echo NO_SUBS
if [ -s "$tmp" ]; then
  echo "CUES=$(grep -c ' --> ' "$tmp")"
  echo "FIRST=$(sed -n '3p' "$tmp")"
fi
rm -f "$tmp"
'''
    r = t.run_script(script, timeout=300)
    info = {"has_subtitles": "NO_SUBS" not in r.stdout}
    for line in r.stdout.splitlines():
        if line.startswith("CUES="):
            info["cues"] = int(line.split("=", 1)[1] or 0)
        elif line.startswith("FIRST="):
            info["first_cue"] = line.split("=", 1)[1]
    return info


def _duration(t: Transport, path: str) -> float:
    r = t.run(f"ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 {_q(path)}",
              timeout=120)
    try:
        return float(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(f"could not read duration of {path}")


def _parse_result(stdout: str, out: str) -> dict:
    info = {"out": out}
    if "---RESULT---" in stdout:
        tail = stdout.split("---RESULT---", 1)[1]
        brace = tail.find("{")
        if brace >= 0:
            depth, end = 0, None
            for i, ch in enumerate(tail[brace:], brace):
                depth += (ch == "{") - (ch == "}")
                if depth == 0:
                    end = i + 1
                    break
            if end:
                try:
                    probe = json.loads(tail[brace:end])
                    fmt = probe.get("format", {})
                    st = (probe.get("streams") or [{}])[0]
                    info.update({
                        "duration": round(float(fmt.get("duration", 0)), 2),
                        "bytes": int(fmt.get("size", 0)),
                        "size": f"{st.get('width')}x{st.get('height')}",
                        "fps": st.get("r_frame_rate"),
                    })
                except Exception:
                    pass
        for line in tail.splitlines():
            s = line.strip()
            if s.startswith("I:"):
                info["lufs"] = s.split("I:")[-1].strip()
            elif s.startswith("LRA:"):
                info["lra"] = s.split("LRA:")[-1].strip()
    return info
