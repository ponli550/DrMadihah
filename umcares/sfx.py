"""Sound effects on cuts and words, synthesised by ffmpeg: no sound files, no licences, nothing to download.

Two halves:
  * WHEN: `events()` reads a finished plan and lists where each effect lands: on every cut (the moment one piece gives way to the
    next; the middle of the overlap on a crossfading look), on every word line the kinetic look pops in (the same times
    `kinetic.schedule` gives the picture) and on every countdown number. On a tempo the cuts and words are already on the beat, so
    the effects are too.
  * WHAT: `render_wav()` makes one short WAV per kind of effect, from an ffmpeg expression (a noise swell for a whoosh, a falling
    chirp for a pop, a low thump for a hit, a very short high click for a tick).

The montage's soundtrack step places the events and mixes them with any kept clip sound and the music.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import explainer, kinetic, prepare

CUT_SOUNDS = ("whoosh", "pop", "hit")
WORD_SOUNDS = ("tick", "pop")
DEFAULT_LEVEL = -12.0                     # dB: the effects sit under the music and speech, never on top of them
LEVEL_RANGE = (-30.0, 0.0)

# name -> (ffmpeg source expression, filters, seconds the sound starts BEFORE its event so its body lands on the event)
SOUNDS = {
    "whoosh": ("aevalsrc='(random(0)*2-1)*pow(sin(PI*t/0.5),2)':d=0.5:s=48000", "highpass=f=350,lowpass=f=7000", 0.25),
    "pop": ("aevalsrc='sin(2*PI*(1100-4200*t)*t)*exp(-28*t)':d=0.14:s=48000", "", 0.0),
    "hit": ("aevalsrc='0.7*(sin(2*PI*58*t)+0.35*(random(0)*2-1)*exp(-35*t))*exp(-8*t)':d=0.5:s=48000", "", 0.0),
    "tick": ("aevalsrc='sin(2*PI*2600*t)*exp(-140*t)':d=0.06:s=48000", "", 0.0),
}


class SfxError(Exception):
    """An effect could not be made; the message is meant to be shown."""


def lead(name: str) -> float:
    return SOUNDS[name][2]


def render_wav(name: str, out) -> Path:
    """Write one effect as a 48 kHz stereo WAV."""
    if name not in SOUNDS:
        raise SfxError(f"there is no sound effect called {name}")
    ffmpeg = prepare.find_tool("ffmpeg")
    if not ffmpeg:
        raise SfxError("ffmpeg is not installed on this machine")
    expr, flt, _ = SOUNDS[name]
    af = ",".join(x for x in (flt, "alimiter=limit=0.9:level=disabled", "aformat=channel_layouts=stereo") if x)
    out = Path(out)
    try:
        r = subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", expr, "-af", af, "-ar", "48000", "-c:a", "pcm_s16le", str(out)],
                           capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        raise SfxError(f"could not make the {name} sound: {e}") from None
    if r.returncode != 0 or not out.is_file():
        raise SfxError(f"could not make the {name} sound: {(r.stderr or '').strip()[-200:]}")
    return out


def events(segs: list, total: float, fade: float, spec: dict, kinetic_look: bool, period=None) -> list:
    """Where each effect lands: [{"t", "kind": "cut" | "word", "sound"}], in time order, none past the end of the video.

    `segs` are the plan's segments (with start and duration), `fade` the plan's crossfade (0 on hard cuts), `spec` the recipe's
    sfx ({"cuts": name|None, "words": name|None}), `kinetic_look` whether captions pop in word by word, `period` seconds per beat
    when cut to a tempo."""
    out = []
    cuts, words = spec.get("cuts"), spec.get("words")
    if cuts:
        for s in segs[1:]:
            out.append({"t": round(float(s["start"]) + fade / 2, 3), "kind": "cut", "sound": cuts})
    if words:
        for s in segs:
            start, d = float(s["start"]), float(s["duration"])
            if s["kind"] == "countdown":
                per = d / max(1, int(s["count"]))
                out += [{"t": round(start + k * per, 3), "kind": "word", "sound": words} for k in range(int(s["count"]))]
            elif s["kind"] in explainer.KINDS:
                out += [{"t": round(start + t, 3), "kind": "word", "sound": words} for t in explainer.events(s)]
            elif kinetic_look and s.get("text") and s["kind"] in ("photo", "video", "title", "end", "announce"):
                lines = kinetic.lines_for(kinetic.split_words(s["text"]))
                out += [{"t": round(start + t, 3), "kind": "word", "sound": words} for t in kinetic.schedule(len(lines), d, period)]
    return sorted((e for e in out if 0 <= e["t"] < total), key=lambda e: (e["t"], e["kind"]))
