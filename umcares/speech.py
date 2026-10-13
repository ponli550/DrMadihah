"""Find where people are speaking in a clip, so a video can start on the words instead of on dead air.

Pure Python on top of ffmpeg (which decodes any clip to raw samples); no model, no cloud, nothing to install.

How it works, and where it is weak:
  1. ffmpeg decodes the clip's sound to mono 16 kHz and keeps only the speech band (about 250-3800 Hz).
  2. Loudness is measured every 20 ms. The quiet floor and the loud ceiling of THIS clip set the threshold, so a clip
     recorded quietly and one recorded loudly are judged alike.
  3. Loud runs are joined across short gaps (a breath) and short blips are dropped.
  4. Speech pulses: its loudness rises and falls a few times a second with the syllables. A run is kept only if its
     loudness varies enough (`pulse`), which is what separates a talking person from a steady hum, a fan or a crowd's wash,
     and not so spikily that it is percussion.

It finds *speech-like* sound, not words or speakers. Applause, a very animated crowd or dense real music (vocals, busy
percussion) can pass as speech; a whisper far from the microphone can fail. Treat the result as "probably someone talking
here", good enough to choose which part of a long clip to use. Results are cached beside the project in
.umcares/speech/ keyed by the file's identity.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from array import array
from pathlib import Path

from . import prepare

RATE = 16000
HOP = 320                      # samples per step -> 50 steps per second (20 ms)
FPS = RATE / HOP
MAX_SECONDS = 600              # analyse at most this much of a clip
MIN_RANGE_DB = 9.0             # a clip whose loud and quiet parts differ by less than this is steady noise or silence
MARGIN_DB = 8.0                # a step must be at least this far above the floor
SPEECH_DEPTH_DB = 22.0         # ... and within this of the clip's loud end (soft syllables sit ~10-20 dB under the peaks)
GAP = 0.30                     # seconds of quiet that still count as the same phrase
MIN_SPAN = 0.40                # seconds: shorter runs are blips, not speech
MIN_PULSE = 0.45               # coefficient of variation of the loudness within a run: steadier than this is a hum or a crowd wash
MAX_PULSE = 1.7                # ... spikier than this is percussion (clicks, hi-hats, claps), not syllables; speech measured 0.6-1.25
VERSION = 1


class SpeechError(Exception):
    """The clip could not be analysed; the message is meant to be shown."""


def _samples(path: Path):
    """Mono speech-band samples, or None when the clip has no sound at all."""
    ffmpeg = prepare.find_tool("ffmpeg")
    if not ffmpeg:
        raise SpeechError("ffmpeg is not installed on this machine")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-t", str(MAX_SECONDS), "-i", str(path), "-vn", "-map", "0:a:0",
           "-ac", "1", "-ar", str(RATE), "-af", "highpass=f=250,lowpass=f=3800", "-f", "s16le", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
    except (subprocess.SubprocessError, OSError):
        raise SpeechError(f"could not read {path.name}") from None
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace")
        if "matches no streams" in err or "Stream map" in err or "does not contain" in err:
            return None                                          # a clip with no audio track
        raise SpeechError(f"{path.name} could not be decoded")
    if not r.stdout:
        return None
    a = array("h")
    a.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    return a


def _levels(pcm: array) -> list:
    """(dB, amplitude) per 20 ms step."""
    out = []
    for i in range(len(pcm) // HOP):
        seg = pcm[i * HOP:(i + 1) * HOP]
        ms = sum(x * x for x in seg) / HOP
        out.append((10 * math.log10(1.0 + ms), math.sqrt(ms)))
    return out


def _percentile(sorted_vals: list, p: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(p * len(sorted_vals)))]


def analyse_samples(pcm) -> dict:
    """Speech spans of decoded samples: {"duration", "has_audio", "spans": [[start, end, pulse]], "speech_seconds"}."""
    if pcm is None or len(pcm) < HOP * 5:
        return {"duration": 0.0 if pcm is None else round(len(pcm) / RATE, 2), "has_audio": pcm is not None, "spans": [],
                "speech_seconds": 0.0}
    lv = _levels(pcm)
    n = len(lv)
    db = sorted(d for d, _ in lv)
    floor, ceiling = _percentile(db, 0.15), _percentile(db, 0.95)
    result = {"duration": round(n / FPS, 2), "has_audio": True, "spans": [], "speech_seconds": 0.0}
    if ceiling - floor < MIN_RANGE_DB:
        return result
    thr = max(floor + MARGIN_DB, ceiling - SPEECH_DEPTH_DB)             # relative to the loud speech, so a noise bed stays out
    active = [d > thr for d, _ in lv]
    runs, i = [], 0
    while i < n:                                                # loud runs
        if active[i]:
            j = i
            while j < n and active[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    merged = []                                                 # a breath between words does not end a phrase
    for r in runs:
        if merged and r[0] - merged[-1][1] <= GAP * FPS:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    for a, b in merged:
        if (b - a) / FPS < MIN_SPAN:
            continue
        amp = [x for _, x in lv[a:b]]
        mean = sum(amp) / len(amp)
        sd = math.sqrt(sum((x - mean) ** 2 for x in amp) / len(amp))
        pulse = sd / mean if mean > 0 else 0.0
        if MIN_PULSE <= pulse <= MAX_PULSE:
            result["spans"].append([round(a / FPS, 2), round(b / FPS, 2), round(pulse, 2)])
    result["speech_seconds"] = round(sum(e - s for s, e, _ in result["spans"]), 2)
    return result


def _cache_file(path: Path, cache_dir):
    if cache_dir is None:
        return None
    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}|{VERSION}".encode()).hexdigest()[:16]
    return Path(cache_dir) / f"{key}.json"


def analyse(path, cache_dir=None) -> dict:
    """Speech spans of a clip file, cached under `cache_dir` when given."""
    path = Path(path)
    cf = _cache_file(path, cache_dir)
    if cf is not None and cf.is_file():
        try:
            return json.loads(cf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    result = analyse_samples(_samples(path))
    if cf is not None:
        try:
            cf.parent.mkdir(parents=True, exist_ok=True)
            tmp = cf.with_name(cf.name + ".part")
            tmp.write_text(json.dumps(result), encoding="utf-8")
            tmp.replace(cf)
        except OSError:
            pass
    return result


def covered(spans: list, start: float, end: float) -> float:
    """Seconds of speech inside [start, end]."""
    return sum(max(0.0, min(e, end) - max(s, start)) for s, e, _ in spans)


def best_start(spans: list, duration: float, length: float, lead: float = 0.3):
    """Where to start a `length`-second excerpt of a `duration`-second clip so it holds the most speech.

    Candidates are the clip start and each phrase start (a little early, so the first word is not clipped). Ties go to
    the earliest. Returns (start, seconds_of_speech), or None when the clip has no speech to follow.
    """
    if not spans or length >= duration - 1e-6:
        return None
    best = None
    for c in [0.0] + [s - lead for s, _, _ in spans]:
        c = max(0.0, min(c, duration - length))
        got = covered(spans, c, c + length)
        if best is None or got > best[1] + 1e-6:
            best = (round(c, 3), round(got, 3))
    return best if best and best[1] > 0 else None
