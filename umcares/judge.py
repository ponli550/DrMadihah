"""A judge for finished videos: objective checks that say pass, warn or fail, each with a reason you can act on.

What it does NOT do: say whether a video is *good*. A score like "7/10" would be false confidence; this reports facts that can
be measured and compared with what was asked for. What it can see:

  format     size, frame rate, codec, pixel format and length against the video's own plan (or the standard formats if it has none),
             audio present exactly when the plan wanted it, audio and video the same length, bitrate per pixel
  sound      integrated loudness (about -16 LUFS is the target), true peak (clipping), a soundtrack that is silent, long silent
             gaps under a song
  picture    black frames where a picture or clip should be, a clip that freezes
  the plan   every warning the plan carried (clips dropped, a clip slowed, no steady beat found), and on a tempo that every cut
             really sits on the beat grid

It reads the file with ffprobe and two ffmpeg analysis passes (picture and sound), a few seconds for a minute of video, no model,
no network. Reports are cached beside the video (`<name>.judge.json`, keyed by its size and time).

Every threshold is a constant below, so what counts as a problem is written down rather than hidden.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from . import prepare

VERSION = 1
LEVELS = ("ok", "warn", "fail")

STANDARD_SIZES = {(1080, 1920), (1080, 1080), (1920, 1080)}      # reels, square, wide
EXPECTED_FPS = 30.0
LOUD_OK = (-19.0, -13.0)                  # integrated LUFS that need no comment (the target is -16)
LOUD_WARN = (-23.0, -10.0)                # outside OK but inside this is a warning; outside it is a failure
SILENT_LUFS = -40.0                       # a soundtrack quieter than this is silent
PEAK_WARN, PEAK_FAIL = -1.0, 0.0          # true peak, dBTP
DURATION_WARN, DURATION_FAIL = 0.25, 1.0  # seconds away from the planned length
AV_WARN, AV_FAIL = 0.3, 1.0               # seconds the audio and video lengths may differ
BPP = (0.03, 0.5)                         # bits per pixel per frame: below is soft and blocky, above is unusually heavy
BLACK_WARN, BLACK_FAIL = 0.6, 1.5         # seconds of black over a picture or clip (the fail also needs most of the piece)
FREEZE_WARN = 1.5                         # seconds a clip may sit frozen
SILENCE_GAP = 1.5                         # seconds of near-silence that is worth saying under a song
PICTURE_KINDS = ("photo", "video")        # where black or frozen frames are a fault (cards are meant to be dark and still)


class JudgeError(Exception):
    """The file could not be judged; the message is meant to be shown."""


# ------------------------------------------------------------------ measuring
def _probe(path: Path) -> dict:
    ffprobe = prepare.find_tool("ffprobe")
    if not ffprobe:
        raise JudgeError("ffprobe is not installed on this machine")
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout or "{}")
    except (subprocess.SubprocessError, ValueError):
        raise JudgeError(f"could not read {path.name}") from None
    if r.returncode != 0 or not data.get("streams"):
        raise JudgeError(f"{path.name} is not a readable video")
    return data


def _ffmpeg(args: list, timeout: int = 300) -> str:
    ffmpeg = prepare.find_tool("ffmpeg")
    if not ffmpeg:
        raise JudgeError("ffmpeg is not installed on this machine")
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-nostdin", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise JudgeError("the analysis took too long") from None
    except OSError as e:
        raise JudgeError(f"could not run ffmpeg: {e}") from None
    return r.stderr


def _black_scan(path: Path) -> list:
    """Black stretches of the picture, [[start, end]] in seconds."""
    log = _ffmpeg(["-i", str(path), "-map", "0:v:0", "-vf", "blackdetect=d=0.3:pix_th=0.10", "-an", "-f", "null", "-"])
    return [[float(a), float(b)] for a, b in re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", log)]


def _frozen_in(path: Path, start: float, duration: float) -> float:
    """Seconds a stretch of the video sits frozen, judged on that stretch ALONE.

    Frozen means identical frames (-70 dB), which is what a stalled decode or a held frame looks like. Each clip is decoded by
    itself because freezedetect's start/end lines run across cuts and pair up wrongly in a whole file (seen: a start glued to a
    later end). Returns 0.0 when it does not freeze for FREEZE_WARN seconds."""
    log = _ffmpeg(["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path), "-map", "0:v:0", "-vf", f"freezedetect=n=-70dB:d={FREEZE_WARN}", "-an", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", log)]
    if not starts:
        return 0.0
    durs = [float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", log)]
    longest = max(durs) if durs else 0.0
    if len(starts) > len(durs):                                        # a freeze that runs to the end of the stretch has no end line
        longest = max(longest, duration - starts[len(durs)])
    return longest


def _sound_scan(path: Path) -> dict:
    """Loudness, true peak and silent stretches of the soundtrack."""
    log = _ffmpeg(["-i", str(path), "-map", "0:a:0", "-af", f"ebur128=peak=true:framelog=quiet,silencedetect=n=-48dB:d={SILENCE_GAP}", "-vn", "-f", "null", "-"])
    m = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+|-inf)\s*LUFS", log)
    p = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", log)
    num = lambda g: None if g is None or g.group(1) == "-inf" else float(g.group(1))
    silences = [[float(a), float(a) + float(d)] for a, d in zip(re.findall(r"silence_start:\s*(-?[\d.]+)", log), re.findall(r"silence_duration:\s*([\d.]+)", log))]
    return {"lufs": num(m) if m else None, "peak": num(p) if p else None, "silences": silences}


def _overlap(a, b) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


# ------------------------------------------------------------------ judging
def _check(level: str, cid: str, message: str) -> dict:
    return {"level": level, "id": cid, "message": message}


def judge(path, plan=None) -> dict:
    """Judge one video file against its plan (the plan stored in its sidecar), or against the standard formats if it has none."""
    path = Path(path)
    data = _probe(path)
    v = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        raise JudgeError(f"{path.name} has no picture")
    fmt = data.get("format", {})
    size = path.stat().st_size
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    try:
        num, den = str(v.get("r_frame_rate", "0/1")).split("/")
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = float(fmt.get("duration") or v.get("duration") or 0)
    checks = []
    add = lambda *x: checks.append(_check(*x))

    # --- format
    add("ok" if "mp4" in str(fmt.get("format_name", "")) else "fail", "container",
        "an MP4 file" if "mp4" in str(fmt.get("format_name", "")) else f"the container is {fmt.get('format_name')}, not MP4")
    add("ok" if v.get("codec_name") == "h264" else "fail", "video-codec",
        "H.264 video" if v.get("codec_name") == "h264" else f"the video is {v.get('codec_name')}, not H.264 (Instagram and TikTok expect H.264)")
    pix = v.get("pix_fmt")
    if pix == "yuv420p":
        add("ok", "pixel-format", "yuv420p, the format every phone plays")
    else:
        add("fail", "pixel-format", f"the pixel format is {pix}, not yuv420p" + (" (full range: colours can look wrong in some apps)" if pix == "yuvj420p" else ""))
    if plan:
        want = (int(plan["width"]), int(plan["height"]))
        add("ok" if (w, h) == want else "fail", "size", f"{w}x{h}" if (w, h) == want else f"{w}x{h}, but the plan says {want[0]}x{want[1]}")
    else:
        add("ok" if (w, h) in STANDARD_SIZES else "warn", "size", f"{w}x{h}" if (w, h) in STANDARD_SIZES else f"{w}x{h} is not one of the standard sizes (1080x1920, 1080x1080, 1920x1080)")
    want_fps = float(plan.get("fps", EXPECTED_FPS)) if plan else EXPECTED_FPS
    add("ok" if abs(fps - want_fps) < 0.05 else "warn", "frame-rate", f"{fps:g} fps" if abs(fps - want_fps) < 0.05 else f"{fps:g} fps, expected {want_fps:g}")

    # --- length
    if plan:
        off = abs(duration - float(plan["total"]))
        level = "ok" if off <= max(DURATION_WARN, 0.01 * plan["total"]) else ("warn" if off <= DURATION_FAIL else "fail")
        add(level, "duration", f"{duration:.1f} s, as planned" if level == "ok" else f"{duration:.1f} s but the plan says {plan['total']:.1f} s")
        limit = (plan.get("recipe") or {}).get("max_seconds")
        if limit and duration > float(limit) + 0.05:
            add("fail", "length-limit", f"{duration:.1f} s is longer than the {float(limit):g} s limit")
    else:
        add("ok", "duration", f"{duration:.1f} s (no plan to compare with)")
    if a is not None:
        adur = float(a.get("duration") or fmt.get("duration") or 0)
        gap = abs(adur - duration)
        add("ok" if gap <= AV_WARN else ("warn" if gap <= AV_FAIL else "fail"), "audio-length",
            "the sound is as long as the picture" if gap <= AV_WARN else f"the sound is {adur:.1f} s and the picture {duration:.1f} s")

    # --- bitrate per pixel
    if duration > 0 and w and h and fps:
        try:
            vbits = float(v.get("bit_rate") or 0)
        except ValueError:
            vbits = 0.0
        if not vbits:                                                  # no per-stream rate: the whole file minus the sound track
            vbits = max(0.0, size * 8 / duration - float((a or {}).get("bit_rate") or 0))
        bpp = vbits / (w * h * fps)
        level = "ok" if BPP[0] <= bpp <= BPP[1] else "warn"
        msg = f"video {vbits / 1e6:.1f} Mbit/s" if level == "ok" else (
            f"video {vbits / 1e6:.2f} Mbit/s is thin for this size: it will look soft or blocky" if bpp < BPP[0]
            else f"video {vbits / 1e6:.1f} Mbit/s is unusually heavy for this size: slow to move and upload")
        add(level, "bitrate", msg)

    # --- sound
    segs = (plan or {}).get("segments", []) if plan else []
    wants_audio = bool(plan and (plan.get("music") or any(s.get("sound") for s in segs)))
    stats = {"width": w, "height": h, "fps": round(fps, 2), "duration": round(duration, 2), "size": size,
             "lufs": None, "true_peak": None, "has_audio": a is not None}
    if plan:
        if wants_audio and a is None:
            add("fail", "audio-present", "the plan wants sound (music or a clip's own sound) but the file has none")
        elif not wants_audio and a is not None:
            add("warn", "audio-present", "the file has a sound track the plan did not ask for")
        else:
            add("ok", "audio-present", "sound where the plan wanted it" if a is not None else "silent, as planned")
    if a is not None:
        codec_ok = a.get("codec_name") == "aac" and int(a.get("sample_rate", 0) or 0) in (44100, 48000)
        add("ok" if codec_ok else "warn", "audio-codec", f"AAC, {a.get('sample_rate')} Hz" if codec_ok else f"{a.get('codec_name')} at {a.get('sample_rate')} Hz: AAC at 44.1 or 48 kHz is the safe choice")
        sound = _sound_scan(path)
        lufs, peak = sound["lufs"], sound["peak"]
        stats["lufs"], stats["true_peak"] = lufs, peak
        if lufs is None or lufs < SILENT_LUFS:
            add("fail" if wants_audio or not plan else "warn", "silent-audio", "the soundtrack is silent")
        else:
            if LOUD_OK[0] <= lufs <= LOUD_OK[1]:
                add("ok", "loudness", f"{lufs:.1f} LUFS (the target is -16)")
            elif LOUD_WARN[0] <= lufs <= LOUD_WARN[1]:
                add("warn", "loudness", f"{lufs:.1f} LUFS is {'too quiet' if lufs < LOUD_OK[0] else 'too loud'} (the target is -16)")
            else:
                add("fail", "loudness", f"{lufs:.1f} LUFS is far {'too quiet' if lufs < LOUD_OK[0] else 'too loud'} (the target is -16)")
        if peak is not None:
            level = "fail" if peak > PEAK_FAIL else ("warn" if peak > PEAK_WARN else "ok")
            add(level, "clipping", f"true peak {peak:.1f} dBTP" if level == "ok" else f"true peak {peak:.1f} dBTP: the sound clips or is about to")
        if plan and plan.get("music") and lufs is not None and lufs >= SILENT_LUFS:
            gaps = [g for g in sound["silences"] if g[1] - g[0] >= SILENCE_GAP]
            # speech clips that keep their own sound are allowed to sit under quiet music; only a gap in a song is worth saying
            if gaps:
                add("warn", "silence", "a silent stretch under the music at " + ", ".join(f"{g[0]:.1f}-{g[1]:.1f} s" for g in gaps[:3]))
            else:
                add("ok", "silence", "no silent gaps under the music")

    # --- picture
    black = _black_scan(path)
    pic_segs = [s for s in segs if s.get("kind") in PICTURE_KINDS]
    if plan:
        worst = None
        for s in pic_segs:
            window = [float(s["start"]), float(s["start"]) + float(s["duration"])]
            covered = sum(_overlap(b, window) for b in black)
            if covered >= BLACK_WARN:
                level = "fail" if covered >= BLACK_FAIL and covered >= 0.7 * (window[1] - window[0]) else "warn"
                if worst is None or LEVELS.index(level) > LEVELS.index(worst[0]):
                    worst = (level, window, covered)
        if worst:
            add(worst[0], "black-frames", f"black for {worst[2]:.1f} s where a picture or clip should be, around {worst[1][0]:.1f} s")
        else:
            add("ok", "black-frames", "no blank frames where a picture or clip should be")
        frozen = None
        for s in segs:
            if s.get("kind") != "video":
                continue
            covered = _frozen_in(path, float(s["start"]), float(s["duration"]))
            if covered >= FREEZE_WARN and (frozen is None or covered > frozen[1]):
                frozen = (float(s["start"]), covered)
        add("warn" if frozen else "ok", "frozen-video",
            f"a clip sits frozen for {frozen[1]:.1f} s from about {frozen[0]:.1f} s" if frozen else "no clip freezes")
    else:
        long_black = [b for b in black if b[1] - b[0] >= BLACK_FAIL]
        add("warn" if long_black else "ok", "black-frames", f"black for {long_black[0][1] - long_black[0][0]:.1f} s from {long_black[0][0]:.1f} s" if long_black else "no long black stretches")

    # --- what the plan itself said
    if plan:
        for warning in plan.get("warnings", []):
            add("warn", "plan-warning", f"the plan warned: {warning}")
        sync = plan.get("sync")
        if sync and sync.get("cut_seconds"):
            unit, frame = float(sync["cut_seconds"]), 1.0 / float(plan.get("fps", 30)) + 1e-6
            off = [s for s in segs if min(float(s["start"]) % unit, unit - float(s["start"]) % unit) > frame]
            add("warn" if off else "ok", "beat-grid",
                f"{len(off)} cut{'s' if len(off) != 1 else ''} not on the beat grid (first at {float(off[0]['start']):.1f} s)" if off else f"every cut is on the {sync.get('bpm'):g} BPM grid")

    counts = {lvl: sum(1 for c in checks if c["level"] == lvl) for lvl in LEVELS}
    verdict = "fail" if counts["fail"] else ("warn" if counts["warn"] else "pass")
    return {"verdict": verdict, "counts": counts, "checks": checks, "stats": stats, "version": VERSION, "judged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "plan": bool(plan)}


# ------------------------------------------------------------------ a project's exports
def _plan_of(video: Path):
    side = video.with_suffix(".json")
    try:
        return json.loads(side.read_text(encoding="utf-8")).get("plan")
    except (OSError, ValueError):
        return None


def cached(video) -> dict | None:
    """The saved report of a video if it is still for this exact file, else None."""
    video = Path(video)
    try:
        d = json.loads(video.with_suffix(".judge.json").read_text(encoding="utf-8"))
        st = video.stat()
        if d.get("version") == VERSION and d.get("size") == st.st_size and d.get("mtime") == int(st.st_mtime):
            return d["report"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def run(video, force: bool = False) -> dict:
    """Judge a video (its plan comes from its sidecar), keeping the report beside it."""
    video = Path(video)
    if not force:
        got = cached(video)
        if got is not None:
            return got
    report = judge(video, _plan_of(video))
    try:
        st = video.stat()
        out = video.with_suffix(".judge.json")
        tmp = out.with_name(out.name + ".part")
        tmp.write_text(json.dumps({"version": VERSION, "size": st.st_size, "mtime": int(st.st_mtime), "report": report}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(out)
    except OSError:
        pass                                                           # a read-only folder: the report is just not kept
    return report


def brief(report: dict) -> str:
    """One line for a list: the verdict and what is wrong."""
    c = report["counts"]
    if report["verdict"] == "pass":
        return "pass"
    bad = [x["message"] for x in report["checks"] if x["level"] != "ok"]
    return f"{report['verdict']}: " + "; ".join(bad[:3]) + (f" (+{len(bad) - 3} more)" if len(bad) > 3 else "")
