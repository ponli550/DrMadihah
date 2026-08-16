"""Subtitles, timed from the delivered audio rather than estimated.

Cue timing comes from silence detection inside each rendered narration file,
then offset by where that scene actually sits on the resolved timeline. Two
reasons not to divide by character count: Malay word length correlates poorly
with duration, and any drift compounds across a four-minute cut.

Sentence text is taken from the recipe, so captions always match what was
actually spoken.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import log


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def speech_spans(path: Path, min_sil: float = 0.16) -> tuple:
    """[(start, end)] of speech within a file, plus its total length."""
    proc = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af",
         f"silencedetect=noise=-40dB:d={min_sil}", "-f", "null", "-"],
        capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    total = duration(path)

    spans, cur = [], 0.0
    for i, s in enumerate(starts):
        if s > cur + 0.05:
            spans.append((cur, s))
        cur = ends[i] if i < len(ends) else total
    if cur < total - 0.05:
        spans.append((cur, total))
    return spans, total


def fit(spans: list, n: int, total: float) -> list:
    """Force the detected spans to exactly n caption slots."""
    if not spans:
        step = total / max(1, n)
        return [(i * step, (i + 1) * step) for i in range(n)]
    spans = list(spans)
    while len(spans) > n:                       # merge across the smallest gap
        gaps = [(spans[i + 1][0] - spans[i][1], i) for i in range(len(spans) - 1)]
        _, idx = min(gaps)
        spans[idx] = (spans[idx][0], spans[idx + 1][1])
        del spans[idx + 1]
    while len(spans) < n:                       # split the longest span
        lengths = [(e - s, i) for i, (s, e) in enumerate(spans)]
        _, idx = max(lengths)
        s, e = spans[idx]
        mid = (s + e) / 2
        spans[idx] = (s, mid)
        spans.insert(idx + 1, (mid, e))
    return spans


def split_sentences(text: str, max_chars: int = 62) -> list:
    """Sentences, further wrapped so no caption line runs too long to read."""
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s.strip()]
    out = []
    for sent in sents:
        if len(sent) <= max_chars:
            out.append(sent)
            continue
        words, line = sent.split(), ""
        for w in words:
            if len(line) + len(w) + 1 > max_chars and line:
                out.append(line.strip() + ",")
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            out.append(line)
    return out or [text.strip()]


def ts(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60); t -= m * 60
    s = int(t)
    ms = int(round((t - s) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build(recipe: dict, resolved: dict, vo_dir: Path, out: Path,
          max_chars: int = 62) -> dict:
    """Write an SRT for the resolved timeline."""
    text_by_scene = {s["id"]: (s.get("narration") or "").strip()
                     for s in recipe.get("scenes") or []}
    extra = {s["id"]: (s.get("captions") or [])
             for s in recipe.get("scenes") or []}

    cues = []
    for entry in resolved.get("audio", []):
        if not entry["key"].startswith("vo:"):
            continue
        sid = entry["scene"]
        wav = vo_dir / f"{sid}.wav"
        text = text_by_scene.get(sid, "")
        if not text or not wav.exists():
            continue
        lines = split_sentences(text, max_chars)
        spans, total = speech_spans(wav)
        spans = fit(spans, len(lines), total)
        for (s, e), line in zip(spans, lines):
            cues.append((entry["start"] + s, entry["start"] + e, line))

    # scenes with no narration may still carry explicit captions
    for scene in resolved.get("scenes", []):
        for cap in extra.get(scene["scene"], []):
            cues.append((scene["start"] + float(cap.get("at", 0)),
                         scene["start"] + float(cap.get("at", 0)) +
                         float(cap.get("duration", 3)),
                         cap.get("text", "")))

    cues.sort(key=lambda c: c[0])
    for i in range(1, len(cues)):
        if cues[i][0] < cues[i - 1][1]:          # nudge overlaps apart
            cues[i] = (cues[i - 1][1] + 0.02,
                       max(cues[i][1], cues[i - 1][1] + 1.0), cues[i][2])

    body = "\n".join(
        f"{i}\n{ts(s)} --> {ts(e)}\n{txt}\n"
        for i, (s, e, txt) in enumerate(cues, 1))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")

    last = cues[-1][1] if cues else 0.0
    if last > resolved.get("total", 0) + 0.5:
        log.warn(f"last cue ends at {last:.1f}s but the cut is "
                 f"{resolved.get('total')}s — captions overrun the video")
    return {"file": str(out), "cues": len(cues), "last_end": round(last, 2)}
