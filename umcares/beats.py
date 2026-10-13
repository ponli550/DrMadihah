"""Find the beat of a song, so a video can cut on it.

Pure Python on top of ffmpeg (which decodes any format to raw samples); nothing to install and no cloud service.

How it works, and where it is weak:
  1. ffmpeg decodes the song to mono 8 kHz twice: once as it is, once low-passed (the kick drum).
  2. Each becomes a 100 Hz "how much louder did it just get" curve (log-energy rise per 10 ms).
  3. Autocorrelation of that curve gives a rough tempo (folded into 70-160 BPM).
  4. The tempo and the offset of the beat grid are then refined against the curve over the whole song.
  5. `confidence` says how much stronger the curve is on the grid than off it. Low means "no steady beat found".

It assumes ONE steady tempo, which holds for produced music and fails for live or rubato playing (the confidence then
drops, and the caller keeps the template pacing). Half/double tempo is not resolved on purpose: a cut every second beat
is still on a beat. Results are cached beside the project in .umcares/beats/ keyed by the file's identity.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from array import array
from pathlib import Path

from . import prepare

RATE = 8000
HOP = 80                       # samples per envelope step -> 100 steps per second
FPS = RATE / HOP
MAX_SECONDS = 240              # analyse at most this much of the song
MIN_BPM, MAX_BPM = 70.0, 160.0
MIN_CONFIDENCE = 1.5           # peak-to-mean ratio of the grid score; below this there is no steady beat
ONSET_LAG = 0.017              # seconds: the energy-rise step is stamped at the start of its frame; measured on synthetic kicks
VERSION = 2


class BeatError(Exception):
    """The beat could not be analysed; the message is meant to be shown."""


def _samples(path: Path, lowpass: bool) -> array:
    ffmpeg = prepare.find_tool("ffmpeg")
    if not ffmpeg:
        raise BeatError("ffmpeg is not installed on this machine")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-t", str(MAX_SECONDS), "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE)]
    if lowpass:
        cmd += ["-af", "lowpass=f=180"]
    cmd += ["-f", "s16le", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        raise BeatError(f"could not read {path.name}") from None
    if r.returncode != 0 or not r.stdout:
        raise BeatError(f"{path.name} has no readable audio")
    a = array("h")
    a.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    return a


def _novelty(pcm: array) -> list:
    """Log-energy rise per step: positive where the sound just got louder."""
    n = len(pcm) // HOP
    energy = []
    for i in range(n):
        seg = pcm[i * HOP:(i + 1) * HOP]
        energy.append(math.log(1.0 + sum(x * x for x in seg) / HOP))
    out = [0.0] * n
    for i in range(1, n):
        d = energy[i] - energy[i - 1]
        out[i] = d if d > 0 else 0.0
    return out


def _normalise(x: list) -> list:
    if not x:
        return x
    m = sum(x) / len(x)
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / len(x)) or 1.0
    return [max(0.0, (v - m) / sd) for v in x]


def _smooth(x: list, k: int = 2) -> list:
    n = len(x)
    out = []
    for i in range(n):
        lo, hi = max(0, i - k), min(n, i + k + 1)
        out.append(sum(x[lo:hi]) / (hi - lo))
    return out


def _fold(bpm: float) -> float:
    while bpm < MIN_BPM:
        bpm *= 2
    while bpm > MAX_BPM:
        bpm /= 2
    return bpm


def _at(curve: list, t: float) -> float:
    """The curve at time t seconds, linearly interpolated."""
    x = t * FPS
    i = int(x)
    if i < 0 or i + 1 >= len(curve):
        return 0.0
    f = x - i
    return curve[i] * (1 - f) + curve[i + 1] * f


def _grid_score(curve: list, period: float, phase: float, duration: float) -> float:
    total, count, t = 0.0, 0, phase
    while t < duration - 0.05:
        total += _at(curve, t)
        count += 1
        t += period
    return total / count if count else 0.0


def _rough_period(curve: list) -> float:
    """The tempo in seconds per beat from autocorrelation, before refining."""
    lo, hi = int(FPS * 60 / MAX_BPM), int(FPS * 60 / MIN_BPM)
    n = len(curve)
    best, best_lag = -1.0, lo
    for lag in range(lo, hi + 1):
        s = sum(curve[i] * curve[i + lag] for i in range(0, n - lag))
        s /= (n - lag)
        # a beat period also repeats at twice the lag, so reward that: it favours the real pulse over a sub-pulse
        if lag * 2 < n:
            s += 0.5 * sum(curve[i] * curve[i + 2 * lag] for i in range(0, n - 2 * lag, 2)) / ((n - 2 * lag) / 2)
        if s > best:
            best, best_lag = s, lag
    return best_lag / FPS


def analyse_samples(full: array, low: array) -> dict:
    """Beat grid from two decoded versions of a song. Returns {bpm, period, phase, confidence, duration}."""
    nf, nl = _normalise(_novelty(full)), _normalise(_novelty(low))
    n = min(len(nf), len(nl))
    curve = _smooth([nf[i] + nl[i] for i in range(n)])
    duration = n / FPS
    if duration < 6:
        raise BeatError("the song is too short to find a beat (under 6 seconds)")
    if max(curve) <= 0:
        raise BeatError("the song is silent")
    period0 = _rough_period(curve)
    best = (-1.0, period0, 0.0)
    scores_at_best = []
    for k in range(-30, 31):                                   # +-3% in 0.1% steps
        period = period0 * (1 + k / 1000)
        phases = [period * j / 40 for j in range(40)]
        scores = [_grid_score(curve, period, ph, duration) for ph in phases]
        top = max(range(len(scores)), key=scores.__getitem__)
        if scores[top] > best[0]:
            best, scores_at_best = (scores[top], period, phases[top]), scores
    score, period, phase = best
    # refine the phase around the winner in finer steps
    fine = [phase + (j - 10) * period / 400 for j in range(21)]
    fine_scores = [_grid_score(curve, period, p % period, duration) for p in fine]
    phase = fine[max(range(len(fine)), key=fine_scores.__getitem__)] % period
    mean = sum(scores_at_best) / len(scores_at_best) if scores_at_best else 0.0
    confidence = score / mean if mean > 0 else 0.0
    return {"bpm": round(60 / period, 2), "period": round(period, 5), "phase": round((phase + ONSET_LAG) % period, 4),
            "confidence": round(confidence, 2), "duration": round(duration, 2)}


def _cache_file(path: Path, cache_dir):
    if cache_dir is None:
        return None
    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}|{VERSION}".encode()).hexdigest()[:16]
    return Path(cache_dir) / f"{key}.json"


def analyse(path, cache_dir=None) -> dict:
    """Beat grid of a song file, cached under `cache_dir` when given."""
    path = Path(path)
    cf = _cache_file(path, cache_dir)
    if cf is not None and cf.is_file():
        try:
            return json.loads(cf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    result = analyse_samples(_samples(path, False), _samples(path, True))
    if cf is not None:
        try:
            cf.parent.mkdir(parents=True, exist_ok=True)
            tmp = cf.with_name(cf.name + ".part")
            tmp.write_text(json.dumps(result), encoding="utf-8")
            tmp.replace(cf)
        except OSError:
            pass
    return result


def steady(result: dict) -> bool:
    return result["confidence"] >= MIN_CONFIDENCE and MIN_BPM * 0.97 <= result["bpm"] <= MAX_BPM * 1.03


def first_beat(result: dict, at: float) -> float:
    """The time of the first beat at or after `at` seconds."""
    p, ph = result["period"], result["phase"]
    n = math.ceil((at - ph) / p - 1e-9)
    return round(ph + max(0, n) * p, 4)


def cut_unit(result: dict, minimum: float = 1.0) -> tuple:
    """(seconds, beats): the shortest whole number of beats, doubling, that lasts at least `minimum` seconds."""
    beats = 1
    while result["period"] * beats < minimum - 1e-6:
        beats *= 2
    return round(result["period"] * beats, 5), beats
