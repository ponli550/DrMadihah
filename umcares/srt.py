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


def _to_wall(spans: list, x: float, is_start: bool) -> float:
    """Map a position measured in SPEECH time onto the real timeline.

    Silences are skipped, so `x` seconds of speaking maps to wherever the
    speaker actually was at that point. A boundary landing exactly on the end
    of a span is pushed to the start of the next one when it begins a cue, so
    a caption never appears during a pause.
    """
    rem = x
    for i, (a, b) in enumerate(spans):
        d = b - a
        if rem < d - 1e-6:
            return a + rem
        if abs(rem - d) <= 1e-6:
            if is_start and i + 1 < len(spans):
                return spans[i + 1][0]
            return b
        rem -= d
    return spans[-1][1]


def allocate(spans: list, lines: list, total: float) -> list:
    """Give each caption line a slice of the detected speech.

    Forcing the detected spans to equal the number of lines -- merging across
    the smallest gap, splitting the longest span down the middle -- is a guess,
    and it is wrong whenever the counts differ, which is most of the time: a
    speaker pauses mid-sentence, or runs two sentences together. The midpoint
    of a span has no relationship to where one sentence ends and the next
    begins, so captions drift early or late.

    Instead each line takes a share of the total SPEAKING time proportional to
    its length, and that share is mapped back onto the timeline with the
    silences skipped. Longer lines get more time because they take longer to
    say, and every cue still lands inside real speech.
    """
    if not lines:
        return []
    if not spans:
        step = total / len(lines)
        return [(i * step, (i + 1) * step) for i in range(len(lines))]

    # A floor on the weight, not just on the final duration: a five-character
    # remnant like "kuiz." is proportionally almost nothing, and a caption that
    # flashes for a quarter of a second is worse than a slightly uneven one.
    weights = [max(MIN_WEIGHT, len(l)) for l in lines]
    tot_w = float(sum(weights))
    speech = sum(b - a for a, b in spans)

    bounds, acc = [0.0], 0.0
    for w in weights:
        acc += w
        bounds.append(speech * acc / tot_w)

    out = []
    for i in range(len(lines)):
        s0 = _to_wall(spans, bounds[i], True)
        e0 = _to_wall(spans, bounds[i + 1], False)
        if e0 <= s0:
            e0 = min(total, s0 + 0.6)
        out.append((s0, e0))
    return out


MIN_WEIGHT = 14          # shortest line length used for timing purposes


def _wrap(sent: str, width: int) -> list:
    words, out, line = sent.split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > width and line:
            out.append(line.strip())
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line.strip())
    return out


def split_sentences(text: str, max_chars: int = 62) -> list:
    """Sentences, wrapped into balanced caption lines.

    Greedy wrapping fills each line to the limit and leaves the remainder on the
    last one, which strands orphans: "...cemerlang dalam kuiz." wraps to a final
    line of just "kuiz.". An orphan reads badly and, once cue timing follows
    text length, it also gets almost no time on screen.

    So wrap twice: once greedily to learn how many lines the sentence needs,
    then again at an even width for that many lines.
    """
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s.strip()]
    out = []
    for sent in sents:
        if len(sent) <= max_chars:
            out.append(sent)
            continue
        n = len(_wrap(sent, max_chars))
        even = min(max_chars, max(20, -(-len(sent) // n) + 6))
        lines = _wrap(sent, even)
        while len(lines) > n and even < max_chars:      # keep the line count
            even = min(max_chars, even + 3)
            lines = _wrap(sent, even)
        # No invented punctuation on continuation lines. Appending a comma
        # produced captions like "serta hadiah kepada," — text the speaker
        # never said, in a file the client proofreads line by line.
        out.extend(lines)
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
          max_chars: int | None = None, min_silence: float | None = None) -> dict:
    """Write an SRT for the resolved timeline.

    Both knobs fall back to the recipe (which config has already filled in), so
    a caller only passes them to override for one run.
    """
    subs = recipe.get("subtitles") or {}
    max_chars = int(max_chars or subs.get("max_chars") or 62)
    min_silence = float(min_silence if min_silence is not None
                        else subs.get("min_silence") or 0.16)
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
        spans, total = speech_spans(wav, min_sil=min_silence)
        spans = allocate(spans, lines, total)
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
