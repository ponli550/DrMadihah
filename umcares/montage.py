"""Montage recipes: a short social video made from a project's photos and clips, rendered by ffmpeg on this machine.

It follows the rule of the main recipe format: **whoever writes the recipe (a person, a template or a model) declares
intent and never computes a timing.** A recipe says which pictures, in which order, which look, which words. This
module measures the real clips and works out every duration, overlap and offset, so the same recipe always renders
the same video.

    {"kind": "montage", "title": "CyberDSA", "format": "reels", "template": "slowmo",
     "clips": [{"media": "a1b2c3d4e5f6", "caption": "Opening speech"}, {"media": "IMG_204.jpg"}],
     "captions": true, "title_card": {"text": "CyberDSA 2026"}, "music": null}

How it is drawn, and why:
  * Every still and clip is composed onto a blurred copy of itself, so a portrait photo in a 9:16 frame is never cropped
    and a landscape one never loses its sides. (ffmpeg's ffprobe does not report an EXIF-rotated photo's true shape, so
    composing the same way every time is also simpler than detecting it.)
  * Text is drawn by macOS into a transparent PNG (textimg.py) and laid over with `overlay`, because this ffmpeg has no
    `drawtext`.
  * Each piece is rendered to its own intermediate clip, then joined (hard cuts, or crossfades). Pieces are small, the
    job can be cancelled between and during them, and one bad piece names itself in the error.
  * The output is written hidden and moved into place only when complete and verified. Rendering is low priority and
    one job at a time, shared with the video prepare step, because the machine is running Premiere.

Stdlib only: this runs on the Premiere machine.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import beats, captions, judge, kinetic, prepare, speech, textcards, textimg, uploads
from .layout import kind_of

FORMATS = {
    "reels": {"label": "Reels, Stories, TikTok (9:16)", "width": 1080, "height": 1920, "fps": 30, "caption_bottom": 0.80},
    "square": {"label": "Square post (1:1)", "width": 1080, "height": 1080, "fps": 30, "caption_bottom": 0.90},
    "wide": {"label": "Wide (16:9)", "width": 1920, "height": 1080, "fps": 30, "caption_bottom": 0.90},
}

# Looks, recreated from how they read on screen (CapCut's own templates cannot be imported). Pacing is in seconds of
# OUTPUT time. grade is an ffmpeg filter chain; vignette and grain are added after it.
TEMPLATES = {
    "slowmo": {"label": "Slow motion", "blurb": "Gentle zooms, clips slowed, soft crossfades, warm tone.",
               "photo_hold": 2.6, "video_max": 4.0, "video_speed": 0.75, "fade": 0.5, "zoom": 0.10, "vignette": True, "grain": 6,
               "grade": "eq=saturation=0.95:contrast=1.04,colorbalance=rs=.06:gs=.01:bs=-.06:rm=.04:bm=-.04", "bg": "#14110f",
               "accent": "#e8b86a", "moves": ["in", "pan_right", "out", "pan_left"]},
    "punchy": {"label": "Punchy cuts", "blurb": "Fast hard cuts, quick zoom, high contrast.",
               "photo_hold": 1.2, "video_max": 2.0, "video_speed": 1.0, "fade": 0.0, "zoom": 0.07, "vignette": False, "grain": 0,
               "grade": "eq=contrast=1.15:saturation=1.1:gamma=0.95", "bg": "#0b0b0d",
               "accent": "#ff5a4d", "moves": ["in", "out"]},
    "clean": {"label": "Clean", "blurb": "Even pacing, plain crossfades, natural colour.",
              "photo_hold": 3.0, "video_max": 4.0, "video_speed": 1.0, "fade": 0.4, "zoom": 0.05, "vignette": False, "grain": 0,
              "grade": "", "bg": "#111318",
              "accent": "#7fb0ff", "moves": ["in", "pan_right", "out", "pan_left"]},
    "melancholy": {"label": "Melancholy", "blurb": "Slow and quiet: long holds, cool desaturated tone, soft grain.",
                   "photo_hold": 3.4, "video_max": 4.5, "video_speed": 0.8, "fade": 0.9, "zoom": 0.12, "vignette": True, "grain": 10,
                   "grade": "eq=saturation=0.7:contrast=1.05,colorbalance=rs=-.05:bs=.07:rm=-.03:bm=.05", "bg": "#0d1117",
                   "accent": "#8fb3c9", "moves": ["in", "out"]},
    "kinetic": {"label": "Kinetic type",
                "blurb": "Big bold words pop in one by one (on the beat when you cut to one) over darkened pictures, hard cuts, slow punch-in.",
                "photo_hold": 2.0, "video_max": 3.0, "video_speed": 1.0, "fade": 0.0, "zoom": 0.10, "vignette": False, "grain": 0,
                "grade": "eq=brightness=-0.2:contrast=1.15:saturation=0.85", "bg": "#0a0a0c", "accent": "#ffe14d",
                "moves": ["in", "out"], "style": "kinetic"},
    "collage": {"label": "Paper collage (Vox style)",
                "blurb": "Cut-out photos on paper with tape and shadows, hard cuts, a stop-motion wobble, marker lettering.",
                "photo_hold": 2.2, "video_max": 3.5, "video_speed": 1.0, "fade": 0.0, "zoom": 0.0, "vignette": False, "grain": 0,
                "grade": "", "bg": "#f1ead8", "accent": "#ffd23f", "moves": ["in", "out", "pan_right", "pan_left"], "style": "collage"},
}

# How the camera moves over a photo (the words a recipe can use). Every move is eased (smoothstep), because a linear
# move reads as mechanical: the movement starts slowly, is fastest in the middle, and settles.
MOVES = ("in", "out", "pan_left", "pan_right", "still")

# A draft is the same video at half size, without the slow finishing (grain, vignette) and with a fast encode. Its timings
# are exactly the final's, so what you preview is what you will get; only the pixels are cheaper.
DRAFT_SCALE = 0.5
QUALITIES = ("final", "draft")

MAX_CLIPS = 60
DEFAULT_SECONDS, MAX_SECONDS_LIMIT = 60, 90
CARD_HOLD = 2.5
MIN_PIECE = 0.6
MIN_SOUND = 0.5                # seconds of speech a clip needs in its excerpt for its own sound to be kept
DUCK_GAIN = 0.2                # the music under speech: about -14 dB
DUCK_IN, DUCK_OUT = 0.25, 0.4  # seconds the music takes to dip before a phrase and to come back after it
NICE = prepare.NICE
THREADS = prepare.THREADS
ALLOWED_TOP = {"kind", "version", "title", "format", "template", "clips", "captions", "title_card", "end_card", "music",
               "max_seconds", "note", "quality", "speech", "clip_sound"}
ALLOWED_CLIP = {"media", "caption", "hold", "speed", "start", "zoom", "move", "quote", "by", "announce", "countdown", "audiogram", "image"}
ALLOWED_ANNOUNCE = {"title", "date", "time", "place", "note"}
CONTENT_KINDS = ("photo", "video", "quote", "announce", "countdown", "audiogram")      # what counts as a clip of the video (cards at the ends do not)
ALLOWED_CARD = {"text", "subtitle", "hold"}
ALLOWED_MUSIC = {"file", "start", "fade_out", "sync", "bpm"}


class MontageError(Exception):
    """The video could not be made; the message is meant to be shown."""


class RecipeError(MontageError):
    """The recipe has problems. `problems` lists every one, so a model can fix them all in one go."""
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems[:3]) + (f" (+{len(self.problems) - 3} more)" if len(self.problems) > 3 else ""))


# ------------------------------------------------------------------ the catalog: what a recipe can refer to
def _listdir(d: Path):
    if d.is_dir():
        for p in sorted(d.iterdir(), key=lambda p: p.name.lower()):
            if p.is_file() and not p.name.startswith(".") and ".part" not in p.suffixes:
                yield p


def catalog(root) -> list:
    """Every picture, clip and song a recipe can use, oldest first. A video that has been prepared for editing is
    listed once, pointing at its edit-ready copy. Files that were not uploaded through the web have no hash and are
    referred to by name."""
    root = Path(root)
    notes = captions.load(root)
    known = uploads.known_files(root)
    prepared_for = {sha: rel for rel, sha in prepare.outputs(root).items()}
    items, seen = [], set()
    for sha, rel in known.items():
        kind = kind_of(rel)
        if kind not in ("image", "video", "audio"):
            continue
        best = prepared_for.get(sha, rel) if kind == "video" else rel
        n = notes.get(sha, {})
        items.append({"ref": sha[:12], "sha": sha, "name": Path(rel).name, "kind": kind, "rel": best,
                      "folder": str(Path(best).parent.relative_to("assets")), "prepared": best != rel,
                      "caption": n.get("caption", ""), "mtime": int((root / rel).stat().st_mtime)})
        seen.update({rel, best})
    for folder in ("photos", "inbox", "edit_ready", "footage/A", "footage/B", "music"):
        for p in _listdir(root / "assets" / folder):
            rel = str(p.relative_to(root))
            kind = kind_of(p.name)
            if rel in seen or kind not in ("image", "video", "audio"):
                continue
            items.append({"ref": p.name, "sha": None, "name": p.name, "kind": kind, "rel": rel, "folder": folder,
                          "prepared": False, "caption": "", "mtime": int(p.stat().st_mtime)})
    return sorted(items, key=lambda i: (i["mtime"], i["name"]))


def _find(items: list, ref, kinds, where: str, problems: list):
    """The one catalog item `ref` means, or None after recording why not."""
    if not isinstance(ref, str) or not ref.strip():
        problems.append(f"{where}: name a file by its ref or filename")
        return None
    ref = ref.strip()
    pool = [i for i in items if i["kind"] in kinds]
    hits = []
    if re.fullmatch(r"[0-9a-fA-F]{8,64}", ref):
        hits = [i for i in pool if i["sha"] and i["sha"].startswith(ref.lower())]
    if not hits and "/" in ref:
        hits = [i for i in pool if f"{i['folder']}/{i['name']}" == ref]
    if not hits:
        hits = [i for i in pool if i["name"] == ref] or [i for i in pool if i["name"].lower() == ref.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        near = [i["name"] for i in pool if ref.lower()[:6] in i["name"].lower()][:5]
        problems.append(f"{where}: no {'/'.join(sorted(kinds))} file matches '{ref}'" + (f" (did you mean {', '.join(near)}?)" if near else ""))
    else:
        problems.append(f"{where}: '{ref}' matches {len(hits)} files ({', '.join(i['folder'] + '/' + i['name'] for i in hits[:4])}); use a ref or folder/name")
    return None


# ------------------------------------------------------------------ measuring
_info_cache: dict = {}


def quick_info(path: Path) -> dict:
    """Duration (and size) of a clip, cached by file identity. Stills have duration 0."""
    st = path.stat()
    key = (str(path), st.st_size, int(st.st_mtime))
    if key not in _info_cache:
        ffprobe = prepare.find_tool("ffprobe")
        if not ffprobe:
            raise MontageError("ffprobe is not installed on this machine")
        try:
            r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                               capture_output=True, text=True, timeout=60)
            data = json.loads(r.stdout or "{}")
        except (subprocess.SubprocessError, ValueError):
            raise MontageError(f"could not read {path.name}") from None
        if r.returncode != 0 or not data.get("streams"):
            raise MontageError(f"{path.name} is not a readable picture, clip or song")
        try:
            dur = float(data.get("format", {}).get("duration") or 0)
        except ValueError:
            dur = 0.0
        _info_cache[key] = {"duration": dur}
    return _info_cache[key]


# ------------------------------------------------------------------ validating and resolving
def _num(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def validate(recipe, root) -> dict:
    """Check a recipe against the project and return it normalised, or raise RecipeError listing every problem."""
    problems = []
    if not isinstance(recipe, dict):
        raise RecipeError(["the recipe must be a JSON object"])
    for k in sorted(set(recipe) - ALLOWED_TOP):
        problems.append(f"{k}: unknown field (allowed: {', '.join(sorted(ALLOWED_TOP))})")
    if recipe.get("kind", "montage") != "montage":
        problems.append("kind: must be 'montage'")
    title = recipe.get("title", "")
    if not isinstance(title, str) or len(title) > 100:
        problems.append("title: must be text of at most 100 characters")
    fmt = recipe.get("format", "reels")
    if fmt not in FORMATS:
        problems.append(f"format: must be one of {', '.join(FORMATS)}")
    tpl = recipe.get("template", "slowmo")
    if tpl not in TEMPLATES:
        problems.append(f"template: must be one of {', '.join(TEMPLATES)}")
    if not isinstance(recipe.get("captions", True), bool):
        problems.append("captions: must be true or false")
    if recipe.get("quality", "final") not in QUALITIES:
        problems.append(f"quality: must be one of {', '.join(QUALITIES)}")
    max_s = recipe.get("max_seconds", DEFAULT_SECONDS)
    if not _num(max_s, 5, MAX_SECONDS_LIMIT):
        problems.append(f"max_seconds: must be a number from 5 to {MAX_SECONDS_LIMIT}")
    clips_in = recipe.get("clips")
    if not isinstance(clips_in, list) or not clips_in:
        problems.append("clips: give at least one clip")
        clips_in = []
    if len(clips_in) > MAX_CLIPS:
        problems.append(f"clips: at most {MAX_CLIPS} clips")
        clips_in = clips_in[:MAX_CLIPS]
    items = catalog(root)
    clips = []
    for i, c in enumerate(clips_in):
        w = f"clips[{i}]"
        if not isinstance(c, dict):
            problems.append(f"{w}: must be an object like {{\"media\": \"IMG_1.jpg\"}}")
            continue
        for k in sorted(set(c) - ALLOWED_CLIP):
            problems.append(f"{w}.{k}: unknown field (allowed: {', '.join(sorted(ALLOWED_CLIP))})")
        given = [k for k in ("media", *textcards.KINDS, "audiogram") if k in c]
        if len(given) != 1:
            problems.append(f"{w}: give exactly one of media, quote, announce, countdown or audiogram")
            continue
        if "image" in c and given[0] != "audiogram":
            problems.append(f"{w}.image: only an audiogram has an image")
        if given[0] == "audiogram":
            before = len(problems)
            audio = _find(items, c["audiogram"], {"audio"}, f"{w}.audiogram", problems)
            image = _find(items, c["image"], {"image"}, f"{w}.image", problems) if "image" in c else None
            for k in ("speed", "zoom", "move", "by"):
                if k in c:
                    problems.append(f"{w}.{k}: does not apply to an audiogram")
            for k, lo, hi in (("hold", 0.5, 15), ("start", 0, 3600)):
                if k in c and not _num(c[k], lo, hi):
                    problems.append(f"{w}.{k}: must be a number from {lo} to {hi}")
            if c.get("caption") is not None and (not isinstance(c["caption"], str) or len(c["caption"]) > 200):
                problems.append(f"{w}.caption: must be text of at most 200 characters")
            if len(problems) == before and audio:
                clips.append({**c, "_audiogram": True, "_item": audio, "_image": image})
            continue
        if given[0] != "media":
            before = len(problems)
            _check_text_clip(c, given[0], w, problems)
            if len(problems) == before:
                clips.append({**c, "_card": given[0], "_item": None})
            continue
        if "by" in c:
            problems.append(f"{w}.by: only a quote card has a 'by'")
        item = _find(items, c.get("media"), {"image", "video"}, f"{w}.media", problems)
        for k, lo, hi in (("hold", 0.5, 15), ("speed", 0.25, 2.0), ("start", 0, 3600)):
            if k in c and not _num(c[k], lo, hi):
                problems.append(f"{w}.{k}: must be a number from {lo} to {hi}")
        cap = c.get("caption")
        if cap is not None and (not isinstance(cap, str) or len(cap) > 200):
            problems.append(f"{w}.caption: must be text of at most 200 characters")
        if "zoom" in c and not isinstance(c["zoom"], bool):
            problems.append(f"{w}.zoom: must be true or false (or use move)")
        if "move" in c and c["move"] not in MOVES:
            problems.append(f"{w}.move: must be one of {', '.join(MOVES)}")
        if item:
            if item["kind"] == "image" and ("speed" in c or "start" in c):
                problems.append(f"{w}: speed and start apply to videos, not to a photo")
            if item["kind"] == "video" and "move" in c:
                problems.append(f"{w}: move applies to photos; a video keeps its own camera movement")
            clips.append({**c, "_item": item})
    cards = {}
    for key in ("title_card", "end_card"):
        card = recipe.get(key)
        if card is None:
            continue
        if not isinstance(card, dict) or not isinstance(card.get("text"), str) or not card["text"].strip():
            problems.append(f"{key}: needs {{\"text\": \"...\"}}")
            continue
        for k in sorted(set(card) - ALLOWED_CARD):
            problems.append(f"{key}.{k}: unknown field (allowed: {', '.join(sorted(ALLOWED_CARD))})")
        if len(card["text"]) > 100 or (card.get("subtitle") is not None and (not isinstance(card["subtitle"], str) or len(card["subtitle"]) > 140)):
            problems.append(f"{key}: text up to 100 characters, subtitle up to 140")
        if "hold" in card and not _num(card["hold"], 0.8, 8):
            problems.append(f"{key}.hold: must be a number from 0.8 to 8")
        cards[key] = card
    if recipe.get("speech") not in (None, "follow"):
        problems.append('speech: must be "follow" (start clips where people speak) or left out')
    if recipe.get("clip_sound") not in (None, "speech"):
        problems.append('clip_sound: must be "speech" (keep the sound of clips where people speak) or left out')
    music = recipe.get("music")
    music_item, tempo = None, None
    if music is not None:
        if not isinstance(music, dict):
            problems.append("music: must be {\"file\": \"song.mp3\"} or null")
        else:
            for k in sorted(set(music) - ALLOWED_MUSIC):
                problems.append(f"music.{k}: unknown field (allowed: {', '.join(sorted(ALLOWED_MUSIC))})")
            if "bpm" in music:                                          # cut to a tempo with no audio: the song is added later, in the app
                if music.get("file") is not None:
                    problems.append("music: give a file or a bpm, not both")
                elif not _num(music["bpm"], 50, 200):
                    problems.append("music.bpm: must be a number from 50 to 200")
                else:
                    tempo = float(music["bpm"])
            else:
                music_item = _find(items, music.get("file"), {"audio"}, "music.file", problems)
            if music.get("sync") not in (None, "beat"):
                problems.append('music.sync: must be "beat" (cut on the beat) or left out')
            for k, lo, hi in (("start", 0, 3600), ("fade_out", 0, 10)):
                if k in music and not _num(music[k], lo, hi):
                    problems.append(f"music.{k}: must be a number from {lo} to {hi}")
    if problems:
        raise RecipeError(problems)
    return {"kind": "montage", "version": 1, "title": title.strip(), "format": fmt, "template": tpl,
            "captions": recipe.get("captions", True), "max_seconds": max_s, "clips": clips, "cards": cards,
            "quality": recipe.get("quality", "final"), "speech": recipe.get("speech"), "clip_sound": recipe.get("clip_sound"), "tempo": tempo,
            "music": None if music_item is None else {**music, "_item": music_item}, "note": recipe.get("note")}


def _check_text_clip(c: dict, kind: str, w: str, problems: list) -> None:
    """A clip with no picture: a quote, an announcement or a countdown."""
    for k in ("caption", "speed", "start", "zoom", "move"):
        if k in c:
            problems.append(f"{w}.{k}: applies to pictures and clips, not to a {kind} card")
    if "hold" in c and not _num(c["hold"], 0.5, 15):
        problems.append(f"{w}.hold: must be a number from 0.5 to 15")
    if "by" in c and kind != "quote":
        problems.append(f"{w}.by: only a quote card has a 'by'")
    v = c[kind]
    if kind == "quote":
        if not isinstance(v, str) or not v.strip() or len(v) > 160:
            problems.append(f"{w}.quote: text of 1 to 160 characters")
        if "by" in c and (not isinstance(c["by"], str) or len(c["by"]) > 60):
            problems.append(f"{w}.by: text of at most 60 characters")
    elif kind == "announce":
        if not isinstance(v, dict):
            problems.append(f"{w}.announce: an object like {{\"title\": \"Hackathon\", \"date\": \"12 Oct\", \"place\": \"Hall A\"}}")
            return
        for k in sorted(set(v) - ALLOWED_ANNOUNCE):
            problems.append(f"{w}.announce.{k}: unknown field (allowed: {', '.join(sorted(ALLOWED_ANNOUNCE))})")
        if not isinstance(v.get("title"), str) or not v["title"].strip() or len(v["title"]) > 80:
            problems.append(f"{w}.announce.title: text of 1 to 80 characters")
        for k in ("date", "time", "place", "note"):
            if k in v and (not isinstance(v[k], str) or len(v[k]) > 60):
                problems.append(f"{w}.announce.{k}: text of at most 60 characters")
    elif kind == "countdown":
        if isinstance(v, bool) or not isinstance(v, int) or not 2 <= v <= 5:
            problems.append(f"{w}.countdown: a whole number from 2 to 5 (it counts down to 1)")


def _audiogram_segment(c: dict, r: dict, root: Path) -> dict:
    """A song or voice clip drawn as a moving waveform (over a picture, if one is given). Its sound is always heard."""
    item, image = c["_item"], c.get("_image")
    path = root / item["rel"]
    if not path.is_file():
        raise MontageError(f"{item['name']} is no longer in the project")
    if image is not None and not (root / image["rel"]).is_file():
        raise MontageError(f"{image['name']} is no longer in the project")
    dur = quick_info(path)["duration"]
    start = float(c.get("start", 0))
    if dur - start < 0.5:
        raise MontageError(f"audiogram: start {start:g}s is past the end of {item['name']} ({dur:.1f}s long)")
    used = round(min(dur - start, float(c.get("hold", 8.0))), 3)
    text = c.get("caption") if c.get("caption") is not None else (image or {}).get("caption", "")
    return {"kind": "audiogram", "media": item["ref"], "name": item["name"], "rel": item["rel"], "image_rel": image["rel"] if image else None,
            "text": (text if r["captions"] else "").strip(), "src_start": start, "src_used": used, "speed": 1.0, "duration": used, "sound": True}


def _text_segment(c: dict) -> dict:
    kind = c["_card"]
    if kind == "quote":
        text = c["quote"].strip()
        return {"kind": "quote", "text": text, "by": (c.get("by") or "").strip(),
                "duration": float(c.get("hold", min(8.0, max(3.0, 1.2 + 0.4 * len(text.split())))))}
    if kind == "announce":
        a = c["announce"]
        lines = [prefix + a[k].strip() for k, prefix in (("date", "\U0001F4C5 "), ("time", "\U0001F558 "), ("place", "\U0001F4CD "), ("note", "")) if a.get(k, "").strip()]
        return {"kind": "announce", "text": a["title"].strip(), "lines": lines, "duration": float(c.get("hold", 3.0 + 0.5 * len(lines)))}
    n = int(c["countdown"])
    return {"kind": "countdown", "count": n, "text": "-".join(str(x) for x in range(n, 0, -1)), "duration": float(c.get("hold", n * 0.8))}


def resolve(validated: dict, root) -> dict:
    """Turn a validated recipe (the result of validate()) into an exact timeline. Deterministic: the same recipe and
    files give the same plan."""
    root = Path(root)
    r = validated
    tpl, fmt = TEMPLATES[r["template"]], FORMATS[r["format"]]
    draft = r.get("quality") == "draft"
    segs, warnings = [], []
    photos_seen = 0
    if "title_card" in r["cards"]:
        c = r["cards"]["title_card"]
        segs.append({"kind": "title", "text": c["text"].strip(), "subtitle": (c.get("subtitle") or "").strip(),
                     "duration": float(c.get("hold", CARD_HOLD))})
    for i, c in enumerate(r["clips"]):
        if c.get("_card"):
            segs.append(_text_segment(c))
            continue
        if c.get("_audiogram"):
            segs.append(_audiogram_segment(c, r, root))
            continue
        item = c["_item"]
        path = root / item["rel"]
        if not path.is_file():
            raise MontageError(f"{item['name']} is no longer in the project")
        text = (c.get("caption") if c.get("caption") is not None else item["caption"]) if r["captions"] else ""
        base = {"media": item["ref"], "name": item["name"], "rel": item["rel"], "text": (text or "").strip()}
        if item["kind"] == "image":
            # the move: what the recipe says, else (zoom:false means still), else the template's cycle so neighbours differ
            move = c.get("move") or ("still" if c.get("zoom") is False else tpl["moves"][photos_seen % len(tpl["moves"])])
            photos_seen += 1
            segs.append({**base, "kind": "photo", "move": move, "duration": float(c.get("hold", tpl["photo_hold"]))})
            continue
        dur = quick_info(path)["duration"]
        start = float(c.get("start", 0))
        spans = None
        if r.get("speech") == "follow" or r.get("clip_sound") == "speech":
            spans = _speech_spans(path, root, item["name"], warnings)
            if spans and r.get("speech") == "follow" and "start" not in c:                              # an explicit start is the author's call
                want = float(c.get("hold", tpl["video_max"])) * float(c.get("speed", tpl["video_speed"]))
                pick = speech.best_start(spans, dur, want)
                if pick:
                    start = pick[0]
        avail = dur - start
        if avail < 0.4:
            raise MontageError(f"clips[{i}]: start {start:g}s is past the end of {item['name']} ({dur:.1f}s long)")
        speed = float(c.get("speed", tpl["video_speed"]))
        want_out = float(c.get("hold", tpl["video_max"]))
        used = min(avail, want_out * speed)
        seg = {**base, "kind": "video", "src_start": start, "src_used": round(used, 3), "speed": speed,
               "duration": round(used / speed, 3)}
        if spans is not None:
            seg["speech"] = round(speech.covered(spans, start, start + used), 1)     # seconds of speech in the part used
            seg["_spans"] = spans                                                    # settled after cuts are placed (see _settle_sound)
        segs.append(seg)
    if "end_card" in r["cards"]:
        c = r["cards"]["end_card"]
        segs.append({"kind": "end", "text": c["text"].strip(), "subtitle": (c.get("subtitle") or "").strip(),
                     "duration": float(c.get("hold", CARD_HOLD))})
    fade = tpl["fade"]
    sync = _beat_sync(r, root, segs, warnings, fmt["fps"])
    if sync:
        fade = 0.0                                                      # on the beat means hard cuts
    for s in segs:
        s["duration"] = max(MIN_PIECE, round(s["duration"], 5 if sync else 3))      # a synced cut keeps its frame-exact time
        if fade:
            fade = min(fade, round(0.4 * s["duration"], 3))
    # keep within the length limit by dropping clips from the end (cards stay)
    limit, dropped, left_out = float(r["max_seconds"]), 0, []
    def total_of(ss):
        return sum(s["duration"] for s in ss) - fade * max(0, len(ss) - 1)
    while total_of(segs) > limit + 1e-6:
        idx = next((j for j in range(len(segs) - 1, -1, -1) if segs[j]["kind"] in CONTENT_KINDS), None)
        if idx is None or sum(1 for s in segs if s["kind"] in CONTENT_KINDS) <= 1:
            break
        gone = segs.pop(idx)
        left_out.insert(0, gone["text"] or gone.get("name", gone["kind"]))
        dropped += 1
    if dropped:
        names = ", ".join(left_out[:4]) + (f" and {dropped - 4} more" if dropped > 4 else "")
        warnings.append(f"{dropped} clip{'s' if dropped != 1 else ''} left out to stay within {limit:g} seconds: {names}")
    if total_of(segs) > limit + 1e-6:
        warnings.append(f"even with one clip the video is {total_of(segs):.1f}s, longer than the {limit:g}s limit: shorten the cards or raise max_seconds")
    t = 0.0
    for s in segs:
        s["start"] = round(t, 3)
        t += s["duration"] - fade
    total = round(sum(s["duration"] for s in segs) - fade * max(0, len(segs) - 1), 3)
    duck = _settle_sound(r, segs)
    music = None
    if r["music"]:
        m = r["music"]
        mdur = quick_info(root / m["_item"]["rel"])["duration"]
        mstart = float(m.get("start", 0))
        if mdur - mstart < 1:
            raise MontageError(f"music.start {mstart:g}s is past the end of {m['_item']['name']} ({mdur:.1f}s long)")
        if sync:
            mstart = sync["music_start"]
            if mdur - mstart < 1:
                raise MontageError(f"the first beat after music.start is at {mstart:g}s, past the end of {m['_item']['name']}")
        if mdur - mstart < total:
            warnings.append(f"the music ends {total - (mdur - mstart):.0f}s before the video does")
        music = {"name": m["_item"]["name"], "rel": m["_item"]["rel"], "start": mstart,
                 "fade_out": round(min(float(m.get("fade_out", 1.5)), total / 2), 3)}
    even = lambda n: max(2, int(round(n)) // 2 * 2)                      # x264 needs even sizes
    width, height = (even(fmt["width"] * DRAFT_SCALE), even(fmt["height"] * DRAFT_SCALE)) if draft else (fmt["width"], fmt["height"])
    return {"title": r["title"], "format": r["format"], "template": r["template"], "quality": "draft" if draft else "final",
            "width": width, "height": height,
            "fps": fmt["fps"], "fade": round(fade, 3), "total": total, "segments": segs, "duck": duck, "music": music, "sync": None if not sync else {k: v for k, v in sync.items() if k != "music_start"},
            "warnings": warnings,
            "recipe": {k: v for k, v in r.items() if k not in ("clips", "cards", "music")}}


def _settle_sound(r, segs: list) -> list:
    """With the cuts placed: re-measure the speech in each video excerpt, decide which clips keep their own sound
    (clip_sound "speech" and enough speech in the excerpt) and return the video-time windows the music must dip under."""
    windows = []
    for s in segs:
        if s["kind"] == "audiogram":                                    # its sound is the content: the music dips under all of it
            windows.append([round(s["start"], 3), round(s["start"] + s["duration"], 3)])
            continue
        spans = s.pop("_spans", None)
        if spans is None:
            continue
        lo, hi = s["src_start"], s["src_start"] + s["src_used"]
        s["speech"] = round(speech.covered(spans, lo, hi), 1)
        if r.get("clip_sound") == "speech" and speech.covered(spans, lo, hi) >= MIN_SOUND:
            s["sound"] = True
            for a, b, _ in spans:
                a, b = max(a, lo), min(b, hi)
                if b > a:
                    windows.append([round(s["start"] + (a - lo) / s["speed"], 3), round(min(s["start"] + (b - lo) / s["speed"], s["start"] + s["duration"]), 3)])
    merged = []
    for w in windows:                                                   # phrases a breath apart are one dip
        if merged and w[0] - merged[-1][1] < DUCK_IN + DUCK_OUT:
            merged[-1][1] = max(merged[-1][1], w[1])
        else:
            merged.append(w)
    return merged


def _speech_spans(path: Path, root: Path, name: str, warnings: list):
    """The speech spans of a clip ([] when it has none), or None when it could not be checked (a warning says so)."""
    try:
        return speech.analyse(path, root / ".umcares" / "speech")["spans"]
    except speech.SpeechError as e:
        warnings.append(f"could not check {name} for speech: {e}")
        return None


def _beat_sync(r, root: Path, segs: list, warnings: list, fps: int):
    """If the recipe asks for music.sync "beat": find the song's beat and stretch every piece to a whole number of beats,
    so each cut lands on one. Mutates `segs` (duration, and for a video the used length and speed). Returns the sync
    facts, or None when it was not asked for or the song has no steady beat (a warning says why; pacing stays as is)."""
    m, tempo = r["music"], r.get("tempo")
    if tempo:                                                           # a stated tempo, no song: the beat grid starts at the first frame
        grid = {"bpm": tempo, "period": 60.0 / tempo, "phase": 0.0, "confidence": None}
    elif m and m.get("sync") == "beat":
        try:
            grid = beats.analyse(root / m["_item"]["rel"], root / ".umcares" / "beats")
        except beats.BeatError as e:
            warnings.append(f"could not sync to the beat: {e}; kept the template pacing")
            return None
        if not beats.steady(grid):
            warnings.append(f"no steady beat found in {m['_item']['name']} (confidence {grid['confidence']:g}, needs {beats.MIN_CONFIDENCE:g}); "
                            "kept the template pacing")
            return None
    else:
        return None
    unit, per_cut = beats.cut_unit(grid)
    slowed, cum = 0, 0
    for s in segs:
        n = max(1, math.floor(s["duration"] / unit + 0.5))
        if s["kind"] == "audiogram":                                      # never slowed: the waveform and the sound must stay in step
            avail = quick_info(root / s["rel"])["duration"] - s["src_start"]
            if n * unit > avail + 1e-6:
                n = max(1, math.floor(avail / unit + 1e-6))                # shorter than one cut: it still fills a cut and its sound just ends early
        if s["kind"] == "video":
            avail = quick_info(root / s["rel"])["duration"] - s["src_start"]
            out_avail = avail / s["speed"]
            if n * unit > out_avail + 1e-6:
                fit = math.floor(out_avail / unit + 1e-6)
                if fit >= 1:
                    n = fit
                else:
                    n = 1
                    s["speed"] = round(avail / unit, 4)                 # a clip shorter than one cut is stretched to fill it
                    slowed += 1
        # every cut time is a whole frame, so rounding never drifts away from the beat across a long video
        s["duration"] = (round((cum + n) * unit * fps) - round(cum * unit * fps)) / fps
        cum += n
        if s["kind"] in ("video", "audiogram"):
            s["src_used"] = round(min(avail, s["duration"] * s["speed"]), 3)
    if slowed:
        warnings.append(f"{slowed} clip{'s' if slowed != 1 else ''} shorter than one beat-cut {'were' if slowed != 1 else 'was'} slowed down to fill it")
    return {"bpm": grid["bpm"], "beats_per_cut": per_cut, "cut_seconds": unit, "confidence": grid["confidence"],
            "music_start": beats.first_beat(grid, float(m.get("start", 0))) if m else 0.0, "tempo_only": bool(tempo)}


def plan_for(recipe, root) -> dict:
    """validate + resolve in one call (what the web page and the job runner use)."""
    return resolve(validate(recipe, root), root)


# ------------------------------------------------------------------ rendering
class Control:
    """Lets another thread stop a render: the flag is checked between steps and the running ffmpeg is terminated."""
    def __init__(self):
        self.cancelled = False
        self.proc = None
        self.lock = threading.Lock()

    def cancel(self):
        with self.lock:
            self.cancelled = True
            if self.proc:
                try:
                    self.proc.terminate()
                except OSError:
                    pass


def _base(ffmpeg):
    cmd = []
    nice = shutil.which("nice")
    if nice:
        cmd += [nice, "-n", NICE]
    return cmd + [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-progress", "pipe:1", "-nostats"]


def _run(cmd, seconds, ctl: Control, on_pct):
    if ctl.cancelled:
        raise MontageError("cancelled")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    with ctl.lock:
        ctl.proc = proc
        if ctl.cancelled:
            proc.terminate()
    errors = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    if seconds > 0:
                        on_pct(min(1.0, int(line.split("=", 1)[1]) / 1_000_000 / seconds))
                except ValueError:
                    pass
            elif line and "=" not in line.split(" ")[0]:
                errors.append(line)
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
    finally:
        proc.stdout.close()
        with ctl.lock:
            ctl.proc = None
    if ctl.cancelled:
        raise MontageError("cancelled")
    if rc != 0:
        raise MontageError("ffmpeg failed: " + (" ".join(errors[-3:]) or f"exit code {rc}")[:300])


def _compose(W, H):
    """Fit the picture inside W x H over a blurred, enlarged copy of itself. Starts from the default input."""
    sw, sh = max(16, W // 12), max(16, H // 12)
    return (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},scale={sw}:{sh},boxblur=3:2,scale={W}:{H}[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")


def _look(tpl, draft=False) -> str:
    """The colour grade, plus (final only) the slow finishing: vignette and grain cost far more than the grade does."""
    parts = [tpl["grade"]]
    if not draft:
        parts += ["vignette=PI/6" if tpl["vignette"] else "", f"noise=alls={tpl['grain']}:allf=t+u" if tpl["grain"] else ""]
    return ",".join(p for p in parts if p)


def _text_png(workdir: Path, text, name, width, height, **kw) -> Path:
    out = workdir / f"{name}.png"
    textimg.render(text, out, width, height, **kw)
    return out


ENCODE_PIECE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", "-an", "-threads", THREADS]
DRAFT_ENCODE_PIECE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p", "-an", "-threads", THREADS]


def _encode_piece(plan) -> list:
    return DRAFT_ENCODE_PIECE if plan.get("quality") == "draft" else ENCODE_PIECE


def _piece_command(ffmpeg, plan, seg, i, workdir: Path, out: Path):
    """The ffmpeg command(s) that render one piece of the timeline to `out`. Returns a list of (cmd, expected seconds)."""
    W, H, fps, d = plan["width"], plan["height"], plan["fps"], seg["duration"]
    tpl = TEMPLATES[plan["template"]]
    fmt = FORMATS[plan["format"]]
    if seg["kind"] == "audiogram":
        return _audiogram_command(ffmpeg, plan, seg, i, workdir, out, tpl)
    if seg["kind"] in textcards.KINDS:                        # no picture: drawn the same way in every look
        return textcards.piece_commands(_base, ffmpeg, plan, seg, i, workdir, out, _encode_piece(plan), tpl)
    if tpl.get("style") == "collage":                        # a different way of drawing the whole piece
        from . import collage
        return collage.piece_commands(_base, ffmpeg, plan, seg, i, workdir, out, _encode_piece(plan), root_of(plan))
    kin = tpl.get("style") == "kinetic"                      # same pictures, the words drawn as popping lines (kinetic.py)
    period = 60.0 / plan["sync"]["bpm"] if plan.get("sync") else None
    look = _look(tpl, plan.get("quality") == "draft")
    steps = []
    glide = lambda start, dur: f"pow(1-min(1,max(0,(t-{start})/{dur})),3)"          # 1 -> 0 along an ease-out curve
    if seg["kind"] in ("title", "end") and kin:
        inputs = ["-f", "lavfi", "-i", f"color=c={tpl['bg']}:s={W}x{H}:r={fps}:d={d}"]
        more, graph, lines, times, y_end = kinetic.overlay(workdir, f"seg{i}", seg["text"], W, H, fps, d, tpl["accent"], period, 1, "[0:v]", "[o]",
                                                           centre=0.44 if seg["subtitle"] else 0.5)
        inputs += more
        last = "[o]"
        if seg["subtitle"]:
            sub = _text_png(workdir, seg["subtitle"], f"seg{i}-s", W, int(H * 0.12), size=int(H * 0.03), valign="top", color="#d0d4da", pad=max(4, int(W * 0.05)))
            inputs += ["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(sub)]
            at = round(times[-1] + 0.25, 3)
            graph += (f";[{1 + len(lines)}:v]format=rgba,fade=t=in:st={at}:d=0.3:alpha=1[sb];"
                      f"[o][sb]overlay=0:{min(int(H * 0.88), y_end + int(H * 0.02))}:format=auto[o2]")
            last = "[o2]"
        graph += f";{last}format=yuv420p[out]"
        return [(_base(ffmpeg) + inputs + ["-filter_complex", graph, "-map", "[out]", "-t", str(d), "-r", str(fps)] + _encode_piece(plan) + [str(out)], d)]
    if seg["kind"] in ("title", "end"):
        # Title above, subtitle below, as two boxes that cannot overlap: the title text is bottom-aligned in a box that ends
        # at 52% of the height, the subtitle top-aligned in a box that starts at 55%. With no subtitle the title is centred.
        # The margin scales with the frame (a fixed margin would leave no room for text in a small frame).
        margin = max(4, int(W * 0.05))
        has_sub = bool(seg["subtitle"])
        title_h = int(H * 0.22)
        title_y = int(H * 0.30) if has_sub else (H - title_h) // 2
        lift = max(6, int(H * 0.018))                                              # how far a line glides up as it arrives
        big = _text_png(workdir, seg["text"], f"seg{i}-t", W, title_h, size=int(H * 0.055), valign="bottom" if has_sub else "middle", pad=margin)
        inputs = ["-f", "lavfi", "-i", f"color=c={tpl['bg']}:s={W}x{H}:r={fps}:d={d}", "-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(big)]
        out_fade = f"fade=t=out:st={max(0.3, d - 0.6):g}:d=0.4:alpha=1"
        graph = (f"[1:v]format=rgba,fade=t=in:st=0.15:d=0.5:alpha=1,{out_fade}[t1];"
                 f"[0:v][t1]overlay=0:'{title_y}+{lift}*{glide(0.15, 0.6)}':format=auto")
        if has_sub:
            sub = _text_png(workdir, seg["subtitle"], f"seg{i}-s", W, int(H * 0.15), size=int(H * 0.032), valign="top", color="#d0d4da", pad=margin)
            bar_w, bar_h = max(24, int(W * 0.12)), max(3, int(H * 0.0035))
            inputs += ["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(sub),
                       "-f", "lavfi", "-i", f"color=c={tpl['accent']}:s={bar_w}x{bar_h}:r={fps}:d={d}"]
            graph += (f"[a];[2:v]format=rgba,fade=t=in:st=0.5:d=0.5:alpha=1,{out_fade}[t2];"
                      f"[a][t2]overlay=0:'{int(H * 0.55)}+{lift}*{glide(0.5, 0.6)}':format=auto[b];"
                      f"[3:v]format=rgba,fade=t=in:st=0.35:d=0.4:alpha=1,{out_fade}[bar];"
                      f"[b][bar]overlay={(W - bar_w) // 2}:{int(H * 0.535)}:format=auto")
        graph += ",format=yuv420p[out]"
        return [(_base(ffmpeg) + inputs + ["-filter_complex", graph, "-map", "[out]", "-t", str(d), "-r", str(fps)] + _encode_piece(plan) + [str(out)], d)]
    overlay_in, overlay_graph = [], ""
    if seg["text"] and kin:
        overlay_in, frag, _, _, _ = kinetic.overlay(workdir, f"seg{i}", seg["text"], W, H, fps, d, tpl["accent"], period, 1, "[v]", "[o]",
                                                    centre=kinetic.PHOTO_CENTRE)
        overlay_graph = ";" + frag
    elif seg["text"]:
        cap_h = int(H * 0.22)
        png = _text_png(workdir, seg["text"], f"seg{i}-c", W, cap_h, size=int(H * 0.036), plate=True, valign="bottom", pad=int(W * 0.06))
        overlay_in = ["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(png)]
        y = int(H * fmt["caption_bottom"]) - cap_h
        rise = max(6, int(H * 0.014))
        overlay_graph = (f";[1:v]format=rgba,fade=t=in:st=0.25:d=0.35:alpha=1,fade=t=out:st={max(0.3, d - 0.6):g}:d=0.35:alpha=1[t];"
                         f"[v][t]overlay=0:'{y}+{rise}*{glide(0.25, 0.45)}':format=auto[o]")
    out_label = "[out]" if overlay_graph else "[v]"
    if seg["kind"] == "photo":
        move, zoom = seg["move"], tpl["zoom"]
        n = max(1, round(d * fps))
        u = f"(on/{n})"
        ease = f"(3*pow({u},2)-2*pow({u},3))"                                      # smoothstep: slow, fast, slow
        comp = workdir / f"seg{i}-compose.jpg"
        k = 1 if move == "still" else 2                                            # a moving shot is composed larger, then cropped
        steps.append((_base(ffmpeg) + ["-i", str(root_of(plan) / seg["rel"]), "-vf", _compose(W * k, H * k), "-frames:v", "1", "-q:v", "2", str(comp)], 0))
        centre_x, centre_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        pan = max(zoom, 0.08)                                                      # a pan needs room to travel
        motion = {
            "still": f"scale={W}:{H}",
            "in": f"zoompan=z='1+{zoom}*{ease}':x='{centre_x}':y='{centre_y}':d=1:s={W}x{H}:fps={fps}",
            "out": f"zoompan=z='1+{zoom}*(1-{ease})':x='{centre_x}':y='{centre_y}':d=1:s={W}x{H}:fps={fps}",
            "pan_right": f"zoompan=z='1+{pan}':x='(iw-iw/zoom)*{ease}':y='{centre_y}':d=1:s={W}x{H}:fps={fps}",
            "pan_left": f"zoompan=z='1+{pan}':x='(iw-iw/zoom)*(1-{ease})':y='{centre_y}':d=1:s={W}x{H}:fps={fps}",
        }[move]
        chain = ",".join(p for p in (motion, look, "scale=out_range=tv", "format=yuv420p") if p)
        graph = f"[0:v]{chain}[v]" + overlay_graph + (";[o]format=yuv420p[out]" if overlay_graph else "")
        steps.append((_base(ffmpeg) + ["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(comp)] + overlay_in +
                      ["-filter_complex", graph, "-map", out_label, "-t", str(d), "-r", str(fps)] + _encode_piece(plan) + [str(out)], d))
        return steps
    speed = seg["speed"]
    fps_f = f"fps={fps}" if speed >= 0.999 else f"framerate=fps={fps}"
    chain = ",".join(p for p in (f"setpts=(PTS-STARTPTS)/{speed}", fps_f, look, "scale=out_range=tv", "format=yuv420p") if p)
    graph = f"[0:v]{_compose(W, H)},{chain}[v]" + overlay_graph + (";[o]format=yuv420p[out]" if overlay_graph else "")
    steps.append((_base(ffmpeg) + ["-ss", str(seg["src_start"]), "-t", str(seg["src_used"]), "-i", str(root_of(plan) / seg["rel"])] + overlay_in +
                  ["-filter_complex", graph, "-map", out_label, "-t", str(d), "-r", str(fps)] + _encode_piece(plan) + [str(out)], d))
    return steps


def _audiogram_command(ffmpeg, plan, seg, i, workdir: Path, out: Path, tpl):
    """A moving waveform of the clip's own audio (ffmpeg's showwaves, black keyed out) over a darkened picture or the look's colour,
    with an optional name below. The piece itself is silent like every piece; the sound is placed by the soundtrack step."""
    W, H, fps, d = plan["width"], plan["height"], plan["fps"], seg["duration"]
    root = root_of(plan)
    picture = seg.get("image_rel")
    ink = textcards.LIGHT if picture else textcards.ink_for(tpl["bg"])
    wave = tpl["accent"] if (picture or ink == textcards.LIGHT) else textcards.accent_text(tpl["accent"], tpl["bg"])
    wave_h = int(H * 0.2)
    wave_y = int(H * 0.5) - wave_h // 2
    if picture:
        inputs = ["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(root / picture)]
        bg = f"[0:v]{_compose(W, H)},eq=brightness=-0.3:contrast=1.1,scale=out_range=tv,format=yuv420p[bg]"
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c={tpl['bg']}:s={W}x{H}:r={fps}:d={d}"]
        bg = "[0:v]null[bg]"
    inputs += ["-ss", str(seg["src_start"]), "-t", str(d), "-i", str(root / seg["rel"])]
    # The waveform's colour: the white waveform is used as the ALPHA of a solid accent colour (alphamerge). `colorkey` on a coloured
    # waveform turned the accent green (measured), so it is not used. The waveform is drawn small and enlarged with nearest-neighbour
    # so it reads as chunky bars, a look rather than a necessity (measured: a hairline keeps its colour just as well).
    k = max(3, W // 150)
    cw, ch = W // k, max(2, wave_h // k)
    ww, wh2 = cw * k, ch * k
    graph = (f"{bg};[1:a]aresample=48000,showwaves=s={cw}x{ch}:mode=cline:draw=full:rate={fps}:colors=white:scale=sqrt,"
             f"scale={ww}:{wh2}:flags=neighbor,format=gray[m];color=c=0x{wave.lstrip('#')}:s={ww}x{wh2}:r={fps}[c];[c][m]alphamerge,format=rgba[wv];"
             f"[bg][wv]overlay=({W}-w)/2:{wave_y}:format=auto[o]")
    last = "[o]"
    if seg["text"]:
        label_h = int(H * 0.12)
        pad = max(4, int(W * 0.06))
        png = _text_png(workdir, seg["text"], f"seg{i}-name", W, label_h + 2 * pad, size=int(H * 0.04), font=kinetic.FONT, color=ink, valign="middle",
                        pad=pad, shadow=ink == textcards.LIGHT)
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(png)]
        graph += f";[2:v]format=rgba,fade=t=in:st=0.2:d=0.4:alpha=1[nm];[o][nm]overlay=0:{int(H * 0.68) - pad}:format=auto[o2]"
        last = "[o2]"
    graph += f";{last}format=yuv420p[out]"
    return [(_base(ffmpeg) + inputs + ["-filter_complex", graph, "-map", "[out]", "-t", str(d), "-r", str(fps)] + _encode_piece(plan) + [str(out)], d)]


def root_of(plan) -> Path:
    return Path(plan["_root"])


def render(plan: dict, root, out: Path, ctl: Control, on_pct=lambda p: None) -> dict:
    """Render the plan to `out` (a .part file the caller moves into place). Returns what was made."""
    root = Path(root)
    plan = {**plan, "_root": str(root)}
    ffmpeg = prepare.find_tool("ffmpeg")
    if not ffmpeg:
        raise MontageError("ffmpeg is not installed on this machine")
    work = Path(tempfile.mkdtemp(prefix="montage-", dir=_workroot(root)))
    try:
        segs, total_d = plan["segments"], sum(s["duration"] for s in plan["segments"])
        done_d, pieces = 0.0, []
        for i, seg in enumerate(segs):
            piece = work / f"piece{i:03d}.mp4"
            for cmd, secs in _piece_command(ffmpeg, plan, seg, i, work, piece):
                base_d = done_d
                _run(cmd, secs, ctl, lambda f, base_d=base_d, d=seg["duration"]: on_pct(0.85 * (base_d + f * d) / total_d))
            pieces.append(piece)
            done_d += seg["duration"]
            on_pct(0.85 * done_d / total_d)
        fade, W, H, fps = plan["fade"], plan["width"], plan["height"], plan["fps"]
        cmd = _base(ffmpeg)
        if len(pieces) > 1 and fade > 0:
            for p in pieces:
                cmd += ["-i", str(p)]
            chain, label, acc = [], "[0:v]", 0.0
            for k in range(1, len(pieces)):
                acc += segs[k - 1]["duration"] - fade
                out_l = f"[x{k}]"
                chain.append(f"{label}[{k}:v]xfade=transition=fade:duration={fade}:offset={round(acc, 3)}{out_l}")
                label = out_l
            vgraph = ";".join(chain)
            quick = plan.get("quality") == "draft"
            vmap, venc = label, ["-c:v", "libx264", "-preset", "ultrafast" if quick else "medium", "-crf", "26" if quick else "18",
                                 "-pix_fmt", "yuv420p", "-r", str(fps), "-threads", THREADS,
                                 "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
            n_in = len(pieces)
        else:
            lst = work / "list.txt"
            lst.write_text("".join(f"file '{p.name}'\n" for p in pieces), encoding="utf-8")
            cmd += ["-f", "concat", "-safe", "0", "-i", str(lst)]
            vgraph, vmap, venc, n_in = "", "0:v", ["-c:v", "copy"], 1
        m = plan["music"]
        aenc = ["-an"]
        if any(s.get("sound") for s in segs):
            cmd += ["-i", str(_audio_mix(ffmpeg, plan, root, work, ctl))]
            aenc = ["-map", f"{n_in}:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        elif m:
            fo = m["fade_out"]
            cmd += ["-ss", str(m["start"]), "-t", str(plan["total"]), "-i", str(root / m["rel"])]
            aenc = ["-map", f"{n_in}:a:0", "-af", f"afade=t=in:st=0:d=0.6,afade=t=out:st={max(0, plan['total'] - fo):g}:d={fo:g},loudnorm=I=-16:LRA=11:TP=-1.5",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        if vgraph:
            cmd += ["-filter_complex", vgraph]
        cmd += ["-map", vmap] + venc + aenc + ["-t", str(plan["total"]), "-movflags", "+faststart", "-f", "mp4", str(out)]
        _run(cmd, plan["total"], ctl, lambda f: on_pct(0.85 + 0.15 * f))
        info = _verify(out, plan)
        on_pct(1.0)
        return info
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _duck_gain(windows: list) -> str:
    """An ffmpeg volume expression: 1 normally, DUCK_GAIN inside each window, with short ramps either side."""
    envs = [f"min(1,max(0,(t-({a}-{DUCK_IN}))/{DUCK_IN}))*min(1,max(0,({b}+{DUCK_OUT}-t)/{DUCK_OUT}))" for a, b in windows]
    env = envs[0]
    for e in envs[1:]:
        env = f"max({e},{env})"
    return f"1-{1 - DUCK_GAIN:g}*({env})"


def _audio_mix(ffmpeg, plan: dict, root: Path, work: Path, ctl: Control) -> Path:
    """The soundtrack when some clips keep their own sound: those clips' audio placed on the timeline, the music (if any)
    dipping under their speech, then loudness-normalised. Written as a WAV beside the pieces."""
    kept = [s for s in plan["segments"] if s.get("sound")]
    cmd, chains, labels = _base(ffmpeg), [], []
    for n, s in enumerate(kept):
        cmd += ["-ss", str(s["src_start"]), "-t", str(s["src_used"]), "-i", str(root / s["rel"])]
        ms, d = int(round(s["start"] * 1000)), s["duration"]
        f = f"[{n}:a:0]aresample=48000,aformat=channel_layouts=stereo"
        if abs(s["speed"] - 1) > 1e-3:
            f += f",atempo={s['speed']}"
        chains.append(f + f",atrim=0:{d:g},afade=t=in:d=0.03,afade=t=out:st={max(0, d - 0.03):g}:d=0.03,adelay={ms}|{ms}[v{n}]")
        labels.append(f"[v{n}]")
    chains.append("".join(labels) + f"amix=inputs={len(kept)}:normalize=0:duration=longest[voice]")
    m, total = plan["music"], plan["total"]
    tail = f"loudnorm=I=-16:LRA=11:TP=-1.5,apad=whole_dur={total:g},atrim=0:{total:g}[mix]"         # as long as the video, not as long as the last talking clip
    if m:
        cmd += ["-ss", str(m["start"]), "-t", str(total), "-i", str(root / m["rel"])]
        fo = m["fade_out"]
        dip = f",volume='{_duck_gain(plan['duck'])}':eval=frame" if plan.get("duck") else ""
        chains.append(f"[{len(kept)}:a:0]aresample=48000,aformat=channel_layouts=stereo,afade=t=in:st=0:d=0.6,"
                      f"afade=t=out:st={max(0, total - fo):g}:d={fo:g}{dip}[music]")
        chains.append("[voice][music]amix=inputs=2:normalize=0:duration=longest," + tail)
    else:
        chains.append("[voice]" + tail)
    out = work / "mix.wav"
    cmd += ["-filter_complex", ";".join(chains), "-map", "[mix]", "-c:a", "pcm_s16le", "-ar", "48000", str(out)]
    _run(cmd, total, ctl, lambda f: None)
    return out


def _workroot(root: Path) -> Path:
    d = root / ".umcares" / "montage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _verify(out: Path, plan: dict) -> dict:
    """Trust nothing: the file must be a video of the right size and about the planned length."""
    ffprobe = prepare.find_tool("ffprobe")
    r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(out)],
                       capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(r.stdout)
        v = next(s for s in data["streams"] if s["codec_type"] == "video")
        dur = float(data["format"]["duration"])
    except (ValueError, KeyError, StopIteration):
        raise MontageError("the rendered file is not a readable video") from None
    if (int(v["width"]), int(v["height"])) != (plan["width"], plan["height"]):
        raise MontageError(f"the video came out {v['width']}x{v['height']}, not {plan['width']}x{plan['height']}")
    if abs(dur - plan["total"]) > max(0.6, 0.03 * plan["total"]):
        raise MontageError(f"the video is {dur:.1f}s but the plan says {plan['total']:.1f}s")
    return {"duration": dur, "width": int(v["width"]), "height": int(v["height"]),
            "audio": any(s["codec_type"] == "audio" for s in data["streams"])}


# ------------------------------------------------------------------ jobs
def export_name(plan: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (plan["title"] or "montage").lower()).strip("-")[:40] or "montage"
    return f"{slug}-{plan['format']}{'-draft' if plan.get('quality') == 'draft' else ''}-{time.strftime('%Y%m%d-%H%M%S')}.mp4"


class Renderer:
    """One worker, one render at a time, and never alongside a prepare job (they share prepare.HEAVY)."""
    def __init__(self):
        self.cv = threading.Condition(threading.RLock())
        self.jobs: list = []
        self.thread = None
        self.ctls: dict = {}

    def submit(self, root, recipe) -> dict:
        root = Path(root)
        plan = plan_for(recipe, root)                       # raises RecipeError / MontageError before anything is queued
        key = json.dumps(recipe, sort_keys=True, default=str)
        with self.cv:
            same = next((j for j in self.jobs if j["root"] == str(root) and j["state"] in ("queued", "running") and j.get("_key") == key), None)
            if same:                                         # a second press of Make must not queue a second copy
                return self.public(same)
            job = {"id": f"{int(time.time() * 1000):x}{len(self.jobs)}", "root": str(root), "state": "queued", "percent": 0,
                   "error": None, "output": None, "title": plan["title"] or "Montage", "format": plan["format"],
                   "template": plan["template"], "quality": plan["quality"], "total": plan["total"], "pieces": len(plan["segments"]),
                   "warnings": plan["warnings"], "_plan": plan, "_recipe": recipe, "_key": key}
            self.jobs.append(job)
            self.jobs = self.jobs[-100:]
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, daemon=True)
                self.thread.start()
            self.cv.notify_all()
        return self.public(job)

    @staticmethod
    def public(job) -> dict:
        return {k: v for k, v in job.items() if not k.startswith("_") and k != "root"}

    def snapshot(self, root) -> list:
        with self.cv:
            return [self.public(j) for j in self.jobs if j["root"] == str(root)][-20:]

    def active(self, root=None) -> bool:
        with self.cv:
            return any(j["state"] in ("queued", "running") and (root is None or j["root"] == str(root)) for j in self.jobs)

    def cancel(self, root) -> int:
        n = 0
        with self.cv:
            for j in self.jobs:
                if j["root"] == str(root) and j["state"] in ("queued", "running"):
                    if j["state"] == "queued":
                        j["state"] = "cancelled"
                    elif j["id"] in self.ctls:
                        self.ctls[j["id"]].cancel()
                    n += 1
        return n

    def wait_idle(self, timeout=120.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if not self.active():
                return True
            time.sleep(0.05)
        return False

    def _work(self):
        while True:
            with prepare.HEAVY:
                with self.cv:
                    job = next((j for j in self.jobs if j["state"] == "queued"), None)
                    if job is None:
                        return
                    job["state"] = "running"
                    ctl = self.ctls[job["id"]] = Control()
                try:
                    self._run(job, ctl)
                except MontageError as e:
                    self._end(job, "cancelled" if ctl.cancelled else "failed", None if ctl.cancelled else str(e))
                except Exception as e:                      # a bug must not kill the worker and strand the queue
                    self._end(job, "failed", f"unexpected error: {e}")

    def _end(self, job, state, error=None, output=None):
        with self.cv:
            job["state"], job["error"] = state, error
            if output:
                job["output"], job["percent"] = output, 100
            self.ctls.pop(job["id"], None)
            self.cv.notify_all()

    def _run(self, job, ctl):
        root = Path(job["root"])
        plan = job["_plan"]
        for seg in plan["segments"]:
            if seg.get("image_rel") and not (root / seg["image_rel"]).is_file():
                raise MontageError(f"{seg['image_rel'].rsplit('/', 1)[-1]} is no longer in the project")
            if seg.get("rel") and not (root / seg["rel"]).is_file():
                raise MontageError(f"{seg['name']} is no longer in the project")
        if plan["music"] and not (root / plan["music"]["rel"]).is_file():
            raise MontageError(f"{plan['music']['name']} is no longer in the project")
        exports = root / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        for stale in exports.glob("*.part.mp4"):           # one worker: any .part file now is debris from a crash
            stale.unlink(missing_ok=True)
        free = shutil.disk_usage(exports).free
        if free < 2 << 30:
            raise MontageError("not enough free disk space on the machine to render")
        name = export_name(plan)
        final = uploads.claim_name(exports, name)
        final.unlink(missing_ok=True)                       # claim_name made an empty placeholder; the name is ours
        part = exports / (final.stem + ".part.mp4")
        try:
            def pct(f):
                with self.cv:
                    job["percent"] = max(job["percent"], min(99, int(f * 100)))
            info = render(plan, root, part, ctl, pct)
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        sidecar = final.with_suffix(".json")
        public_plan = {k: v for k, v in plan.items() if k != "_root"}
        sidecar.write_text(json.dumps({"recipe": job["_recipe"], "plan": public_plan, "result": info,
                                       "made": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2, ensure_ascii=False), encoding="utf-8")
        poster_for(final)                                   # best effort: a small first-frame picture to show while the video loads
        self._end(job, "done", output=final.name)


RENDERER = Renderer()


def exports(root) -> list:
    """Finished videos, newest first."""
    root = Path(root)
    out = []
    for p in sorted((root / "exports").glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True) if (root / "exports").is_dir() else []:
        if ".part" in p.suffixes:
            continue
        meta = {}
        side = p.with_suffix(".json")
        if side.is_file():
            try:
                d = json.loads(side.read_text(encoding="utf-8"))
                meta = {"template": d["plan"]["template"], "format": d["plan"]["format"], "duration": d["result"]["duration"],
                        "title": d["plan"]["title"], "audio": d["result"].get("audio", False),
                        "quality": d["plan"].get("quality", "final")}
            except (ValueError, KeyError):
                pass
        report = judge.cached(p)                                       # only a report that is already kept: listing never judges anything
        if report:
            meta = {**meta, "verdict": report["verdict"], "counts": report["counts"]}
        out.append({"name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime), **meta})
    return out


def poster_for(video: Path):
    """A small JPEG of a finished video (beside it, same name), made if missing; None if it cannot be made.

    Why: over a slow link a video element is a black box until enough of the file has arrived. A ~15 KB picture
    arrives in seconds, so the page shows the video's first frame at once and plays when it can.
    """
    video = Path(video)
    poster = video.with_suffix(".jpg")
    if poster.is_file():
        return poster
    ffmpeg = prepare.find_tool("ffmpeg")
    if not ffmpeg or not video.is_file() or ".part" in video.name:
        return None
    tmp = poster.with_name(poster.stem + ".part.jpg")
    try:
        subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", "0.8", "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "6", str(tmp)],
                       capture_output=True, timeout=30, check=True)
        if not tmp.is_file() or tmp.stat().st_size == 0:                # a clip shorter than the seek point gives no frame
            subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "6", str(tmp)],
                           capture_output=True, timeout=30, check=True)
        os.replace(tmp, poster)
    except (subprocess.SubprocessError, OSError):
        tmp.unlink(missing_ok=True)
        return None
    return poster


EXPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.mp4")


_JUDGE_LOCK = threading.Lock()
MAX_JUDGE_NAMES = 20


def judge_export(root, name, force: bool = False) -> dict:
    """The judge's report for one finished video the person chose (kept beside it; `force` judges it again). One at a time."""
    if not isinstance(name, str) or not EXPORT_NAME.fullmatch(name) or ".part" in name:
        raise MontageError("that is not the name of a finished video")
    video = Path(root) / "exports" / name
    if not video.is_file() or video.is_symlink():
        raise MontageError(f"{name} is not in this project's exports")
    with _JUDGE_LOCK:
        try:
            return {**judge.run(video, force=force), "name": name}
        except judge.JudgeError as e:
            raise MontageError(str(e)) from None


def export_sha256(root, name) -> dict:
    """{"sha256", "size"} of a finished video, so a phone that downloaded it in pieces can check it arrived whole.

    Computed once and kept beside the video (`<name>.sha256`, with the size and mtime it was computed for, so a replaced file is
    hashed again). Only a plain export name is accepted."""
    if not isinstance(name, str) or not EXPORT_NAME.fullmatch(name) or ".part" in name:
        raise MontageError("that is not the name of a finished video")
    video = Path(root) / "exports" / name
    if not video.is_file() or video.is_symlink():
        raise MontageError(f"{name} is not in this project's exports")
    st = video.stat()
    cache = video.with_suffix(".sha256")
    try:
        d = json.loads(cache.read_text(encoding="utf-8"))
        if d.get("size") == st.st_size and d.get("mtime") == int(st.st_mtime) and re.fullmatch(r"[0-9a-f]{64}", d.get("sha256", "")):
            return {"sha256": d["sha256"], "size": st.st_size}
    except (OSError, ValueError):
        pass
    h = hashlib.sha256()
    with open(video, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    out = {"sha256": h.hexdigest(), "size": st.st_size}
    try:
        tmp = cache.with_name(cache.name + ".part")
        tmp.write_text(json.dumps({**out, "mtime": int(st.st_mtime)}), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        pass                                                           # a read-only folder just means it is hashed again next time
    return out


def delete_export(root, name) -> dict:
    """Delete one finished video, its small .json sidecar, its .jpg poster, its .sha256 and its judge report from the project's exports folder, nothing else.

    A video is cheap to make again (its recipe is in the sidecar and in saved recipes), so this is a real delete, not a
    trash folder: the point is to give the disk back. Only a plain file name matching an export is accepted, so no path
    can be built from the name, and a video still being written (.part) is never touched.
    Returns {"deleted": [names], "freed": bytes}.
    """
    if not isinstance(name, str) or not EXPORT_NAME.fullmatch(name) or ".part" in name:
        raise MontageError("that is not the name of a finished video")
    folder = Path(root) / "exports"
    video = folder / name
    if not video.is_file() or video.is_symlink():
        raise MontageError(f"{name} is not in this project's exports")
    gone, freed = [], 0
    for f in (video, video.with_suffix(".json"), video.with_suffix(".jpg"), video.with_suffix(".sha256"), video.with_suffix(".judge.json")):
        if f.is_file() and not f.is_symlink():
            try:
                size = f.stat().st_size
                f.unlink()
            except OSError as e:
                raise MontageError(f"could not delete {f.name}: {e.strerror or e}") from None
            gone.append(f.name)
            freed += size
    return {"deleted": gone, "freed": freed}


# ------------------------------------------------------------------ saved recipes and the brief for an AI
RECIPE_SUFFIX = ".montage.json"


def _recipe_file(root, name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,59}", name):
        raise MontageError("a recipe name uses letters, digits, spaces, - and _ (up to 60 characters)")
    return Path(root) / "recipes" / (name.strip() + RECIPE_SUFFIX)


def save_recipe(root, name: str, recipe) -> str:
    """Save a recipe (it must be valid for this project). Returns the file name."""
    plan_for(recipe, root)
    p = _recipe_file(root, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    tmp.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p.name


def list_recipes(root) -> list:
    """Saved montage recipes, each with whether it is still valid (media can disappear)."""
    out = []
    d = Path(root) / "recipes"
    for p in sorted(d.glob("*" + RECIPE_SUFFIX)) if d.is_dir() else []:
        entry = {"name": p.name[:-len(RECIPE_SUFFIX)], "valid": True, "problems": []}
        try:
            recipe = json.loads(p.read_text(encoding="utf-8"))
            plan_for(recipe, root)
            entry["recipe"] = recipe
        except RecipeError as e:
            entry["valid"], entry["problems"] = False, e.problems
        except (MontageError, ValueError) as e:
            entry["valid"], entry["problems"] = False, [str(e)]
        out.append(entry)
    return out


EXAMPLE = {"kind": "montage", "title": "Event highlights", "format": "reels", "template": "slowmo",
           "captions": True, "title_card": {"text": "Event highlights", "subtitle": "9 October 2026"},
           "clips": [{"media": "<ref or filename>", "caption": "Opening speech"},
                     {"media": "<ref or filename>", "hold": 3, "move": "pan_right"},
                     {"media": "<video ref>", "start": 1.5, "speed": 0.6}],
           "end_card": {"text": "Thank you"}, "music": None, "max_seconds": 45}


def brief(root, project_name: str = "") -> str:
    """Everything an AI needs to write a montage recipe for this project, as markdown."""
    items = catalog(root)
    lines = [f"# Montage brief: {project_name or 'project'}", "",
             "Write one JSON recipe. **Declare intent only: never compute durations, offsets or timings.** The renderer measures "
             "the files and works out every timing, so the same recipe always renders the same video.", "",
             "## Files you can use", "", "Refer to a file by its `ref` (preferred) or its filename.", "",
             "| ref | file | kind | length | caption (what the owner said it shows) |", "|---|---|---|---|---|"]
    for i in items:
        length = ""
        if i["kind"] in ("video", "audio"):
            try:
                length = f"{quick_info(Path(root) / i['rel'])['duration']:.1f}s"
            except MontageError:
                length = "?"
        lines.append(f"| `{i['ref']}` | {i['name']}{' (prepared)' if i['prepared'] else ''} | {i['kind']} | {length} | {i['caption'] or '_none_'} |")
    lines += ["", "## Formats", ""] + [f"- `{k}`: {v['label']}" for k, v in FORMATS.items()]
    lines += ["", "## Templates (the look and the pacing)", ""] + [f"- `{k}`: {v['blurb']}" for k, v in TEMPLATES.items()]
    lines += ["", "## Fields", "",
              "- `clips[]` (required, in order): `media`; optional `caption` (overrides the file's caption), `hold` (seconds on screen), "
              "`speed` (videos only, 0.25 to 2; below 1 is slow motion), `start` (videos only, seconds into the clip), "
              f"`move` (photos only: {', '.join(MOVES)}; the camera glides, eased. Left out, neighbouring photos alternate on their own).",
              "- A clip can also be an audiogram: `{\"audiogram\": \"<a song or voice file>\", \"image\": \"<optional picture>\", \"caption\": \"a name\", \"start\": 0, \"hold\": 8}`. "
              "It draws the audio as a moving waveform (over the darkened picture if given, else on the look's colour), shows the caption below, and its sound is always heard "
              "with the music dipping under it. It is never slowed, so cut to a tempo it fills whole cuts and its sound just ends early if it is shorter.",
              "- A clip with no picture is a text card; give exactly one of `media`, `quote`, `announce`, `countdown` or `audiogram`:",
              "  `{\"quote\": \"text up to 160 characters\", \"by\": \"who said it\", \"hold\": 4}`; "
              "`{\"announce\": {\"title\": \"Hackathon CyberDSA\", \"date\": \"12 Okt 2026\", \"time\": \"9 pagi\", \"place\": \"Dewan A\", \"note\": \"Percuma\"}}` "
              "(title plus chips; every field but the title is optional); `{\"countdown\": 3}` (a whole number from 2 to 5, counts down to 1, one number per equal share, so on the beat when cut to one). "
              "They work in every look, take only `hold`, and can be dropped to fit `max_seconds` like any clip.",
              "- `captions`: true draws each clip's caption on screen; `title_card` / `end_card`: `{text, subtitle?, hold?}`.",
              "- `music`: `{file, start?, fade_out?, sync?}` or null. `\"sync\": \"beat\"` finds the song's beat, makes every piece a whole number of beats long and starts the song on a beat, so every cut lands on one (hard cuts; the plan reports the BPM, or warns when the song has no steady beat). Use only royalty-free music; a commercial song gets a posted video muted or blocked.",
              "- `music: {\"bpm\": 75}` (no file) cuts to that tempo with NO audio: every piece is a whole number of beats from the first frame, so a song added later in the app (a trending sound that cannot be downloaded) lines up if it is started on a beat. Use it when the song's BPM is known and its file is not.",
              "- `clip_sound`: `\"speech\"` keeps the own sound of a video clip whose excerpt has speech (others stay muted, so crowd noise does not leak in) and dips the music about 14 dB under the speech.",
              "- `speech`: `\"follow\"` starts each video clip (that has no explicit `start`) where people speak, so a long clip's excerpt holds the words, not dead air. It finds speech-like sound, not words.",
              f"- `max_seconds`: up to {MAX_SECONDS_LIMIT} (default {DEFAULT_SECONDS}); clips beyond it are dropped from the end.",
              "", "## Example", "", "```json", json.dumps(EXAMPLE, indent=2), "```", "",
              "## Rules", "", "- Use only files from the table above. Do not invent refs.",
              "- Prefer a caption the owner wrote over one you invent; do not state facts (names, numbers) that are not in the captions.",
              "- Vary the pacing: hold the strongest shots longer.",
              "", "## Structure that works for a short post", "",
              "- **Hook (first 2 to 3 seconds):** open with the single strongest, clearest shot, not a logo. A title card is fine only if it is short.",
              "- **Body:** one idea per clip. Change something every 2 to 4 seconds (a different shot, framing or move) so the eye keeps getting a reason to stay.",
              "- **Cover the joins:** put a calm detail or atmosphere shot between two busy ones.",
              "- **Close:** end on a clear final beat (an end card with the event name or a short thank-you), not mid-motion.",
              "- Keep captions short (a few words, never a paragraph); one caption per shot at most.",
              "- 15 to 30 seconds is plenty. A longer video must earn it."]
    return "\n".join(lines) + "\n"
