"""Narration: recipe text -> SSML -> Azure neural voice -> per-scene WAVs.

Rendering goes through json2video's `voice` element, which reaches Azure. That
matters because Azure has real `ms-MY` voices; the Qwen deployment available
here has no Malay at all.

Three things the SSML has to get right, all learned the hard way:

  * **Acronyms.** "UM Cares" read as a word comes out as "umcares". It needs
    `<say-as interpret-as="characters">`.
  * **Loanwords.** "scam siber" inside Malay is pronounced with Malay
    phonetics unless wrapped in `<lang xml:lang="en-US">` — and the text inside
    that tag must use the ENGLISH spelling ("scam cyber"), or the English voice
    mangles the Malay spelling.
  * **Sentence flow.** A full stop makes Azure fall to a terminal contour, so
    consecutive sentences sound like separate takes. Rewriting non-final stops
    as commas keeps the line open.

All scenes render as one movie (one scene each), then split on the silence
between them. One API call instead of N, and the gaps are unambiguous.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import log

DEFAULT_VOICE = "ms-MY-OsmanNeural"


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mark_text(text: str, english_terms: list, acronyms: list,
              spelling: dict | None = None) -> str:
    """Tag acronyms and English loanwords for correct pronunciation."""
    spelling = spelling or {"scam siber": "scam cyber"}
    out = esc(text)

    # acronyms first, so the <lang> wrapper cannot nest inside itself
    for acro in sorted(acronyms or [], key=len, reverse=True):
        head, _, tail = acro.partition(" ")
        if tail and head.isupper():
            # "UM Cares", "UM Press": spell the initialism, SAY the word after
            # it. Treating the whole thing as characters gives "U-M-P-R-E-S-S",
            # and treating none of it gives the Malay word "um".
            repl = (f'<lang xml:lang="en-US">'
                    f'<say-as interpret-as="characters">{esc(head)}</say-as>'
                    f' {esc(tail)}</lang>')
        else:
            repl = f'<say-as interpret-as="characters">{esc(acro)}</say-as>'
        out = re.sub(re.escape(esc(acro)), repl, out)

    for term in sorted(english_terms or [], key=len, reverse=True):
        spoken = spelling.get(term, term)
        out = re.sub(re.escape(esc(term)),
                     f'<lang xml:lang="en-US">{esc(spoken)}</lang>',
                     out, flags=re.IGNORECASE)
    return out


def flow(marked: str, fillers: bool = False) -> str:
    """Join sentences so the read carries forward instead of stopping dead."""
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', marked) if s.strip()]
    if len(sents) < 2:
        return marked
    out, last = [], len(sents) - 1
    words = ["errr", "aaa", "ermm"]
    fi = 0
    for i, sent in enumerate(sents):
        if i == last:
            out.append(sent)
            break
        out.append(re.sub(r'\.$', ',', sent))
        if fillers and i % 2 == 0:
            # no `volume` attribute: Azure rejects the whole document over it
            out.append(f'<break time="90ms"/>'
                       f'<prosody pitch="-6%" rate="-25%">{words[fi % 3]}</prosody>'
                       f'<break time="60ms"/>')
            fi += 1
        else:
            out.append('<break strength="none"/>')
    return " ".join(out)


def build_ssml(text: str, voice_cfg: dict, emphasis: str | None = None) -> str:
    name = voice_cfg.get("name") or DEFAULT_VOICE
    pitch = voice_cfg.get("pitch", "0%")
    rate = voice_cfg.get("rate", "0%")
    body = flow(
        mark_text(text, voice_cfg.get("english_terms") or [],
                  voice_cfg.get("acronyms") or []),
        fillers=bool(voice_cfg.get("fillers")))

    if emphasis:
        marked = mark_text(emphasis, voice_cfg.get("english_terms") or [],
                           voice_cfg.get("acronyms") or [])
        if marked in body:
            body = body.replace(
                marked,
                f'<emphasis level="strong">'
                f'<prosody pitch="+6%" rate="-12%">{marked}</prosody></emphasis>')

    return (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="ms-MY">'
            f'<voice name="{name}"><prosody pitch="{pitch}" rate="{rate}">'
            f'{body}</prosody></voice></speak>')


# ------------------------------------------------------------ json2video --
def _api(env: dict, method: str = "POST", movie=None, pid=None) -> dict:
    url = env.get("JSON2VIDEO_ENDPOINT") or "https://api.json2video.com/v2/movies"
    if pid:
        url += "?project=" + pid
    data = json.dumps(movie).encode() if movie is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": env["JSON2VIDEO_API_KEY"], "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def render(env: dict, scenes: list, voice_cfg: dict,
           poll: int = 8, timeout: int = 1800) -> dict:
    """Render narration for [(id, text, emphasis)] and return the movie URL."""
    name = voice_cfg.get("name") or DEFAULT_VOICE
    movie = {"resolution": "full-hd", "scenes": []}
    for sid, text, emph in scenes:
        movie["scenes"].append({
            "background-color": "#000000",
            "elements": [
                {"type": "voice", "model": "azure", "voice": name,
                 "text": build_ssml(text, voice_cfg, emph)},
                # a caption keeps the rendered movie legible when reviewing
                {"type": "text", "text": sid, "position": "custom",
                 "x": 160, "y": -40, "width": 1600,
                 "settings": {"font-family": "Inter", "font-size": "48px",
                              "color": "#ffffff"}},
            ],
        })

    out = _api(env, "POST", movie=movie)
    pid = out.get("project")
    if not pid:
        raise RuntimeError(f"json2video did not return a project id: {out}")
    log.debug(f"voice project {pid}")

    end = time.time() + timeout
    while time.time() < end:
        time.sleep(poll)
        st = _api(env, "GET", pid=pid).get("movie", {})
        state = st.get("status")
        if state == "done":
            return {"project": pid, "url": st.get("url"),
                    "duration": st.get("duration")}
        if state == "error":
            raise RuntimeError(f"voice render failed: {st.get('message')}")
    raise RuntimeError(f"voice render timed out after {timeout}s (project {pid})")


# ------------------------------------------------------------- splitting --
def download_and_split(url: str, scene_ids: list, out_dir: Path,
                       target_lufs: float = -16.0,
                       min_gap: float = 0.6) -> list:
    """Split the rendered movie back into one normalised WAV per scene.

    Scenes are separated by roughly a second of silence, so detecting gaps is
    more reliable than trusting per-scene durations from the API.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "_narration_full.mp4"
    with urllib.request.urlopen(url, timeout=600) as r, open(src, "wb") as fh:
        fh.write(r.read())

    proc = subprocess.run(
        ["ffmpeg", "-i", str(src), "-af",
         f"silencedetect=noise=-40dB:d={min_gap}", "-f", "null", "-"],
        capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]

    total = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(src)],
        capture_output=True, text=True).stdout.strip())

    # speech runs from 0 (or the end of a gap) to the start of the next gap
    spans, cur = [], 0.0
    for i, s in enumerate(starts):
        if s > cur + 0.05:
            spans.append((cur, s))
        cur = ends[i] if i < len(ends) else total
    if cur < total - 0.05:
        spans.append((cur, total))

    if len(spans) != len(scene_ids):
        log.warn(f"detected {len(spans)} speech spans for {len(scene_ids)} scenes — "
                 f"check {src.name} before trusting the split")

    results = []
    for sid, (s, e) in zip(scene_ids, spans):
        dest = out_dir / f"{sid}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", f"{max(0.0, s - 0.05):.3f}", "-to", f"{e + 0.10:.3f}", "-i", str(src),
             "-vn", "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
             "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "1", str(dest)],
            check=True)
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(dest)],
            capture_output=True, text=True).stdout.strip())
        results.append({"scene": sid, "file": str(dest), "duration": round(dur, 2)})
    return results


def from_recipe(recipe: dict) -> list:
    """[(scene_id, narration, emphasis)] for every scene that has narration."""
    out = []
    for scene in recipe.get("scenes") or []:
        text = (scene.get("narration") or "").strip()
        if text:
            out.append((scene["id"], text, scene.get("emphasis")))
    return out
