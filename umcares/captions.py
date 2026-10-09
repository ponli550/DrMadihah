"""Captions: a line of detail for each uploaded file, kept in <project>/media.json.

Phone files arrive with names like 17915543098925884857360743224657.jpg, which tell neither a person nor the AI that
writes the recipe anything. A caption ("Dr Madihah opening the session") is what lets the recipe step tell the
speaker from the crowd shot.

Each file is in one of three states: captioned, skipped ("no detail", so it is not asked again), or pending.
Entries are keyed by the file's sha256, not its name or place, so renaming a file or sorting it from the inbox
into A-roll keeps its caption. Only files uploaded through the web are known (they are the ones with a hash).

media.json is small and is the real history of a project, so git tracks it, unlike the media.

Stdlib only: this runs on the Premiere machine.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from . import prepare, uploads
from .layout import kind_of

FILE = "media.json"
MAX_CAPTION = 500
_lock = threading.RLock()


class CaptionError(Exception):
    """A refusal the person should see: `status` is the HTTP code."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _path(root) -> Path:
    return Path(root) / FILE


def load(root) -> dict:
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
        items = data.get("items", {})
        return items if isinstance(items, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _save(root, items: dict) -> None:
    tmp = _path(root).with_name(FILE + ".part")
    tmp.write_text(json.dumps({"version": 1, "items": items}, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, _path(root))


def clean(caption) -> str:
    """One paragraph: control characters out, whitespace collapsed, and a length limit that is an error, not a silent cut."""
    if caption is None:
        return ""
    if not isinstance(caption, str):
        raise CaptionError(400, "the caption must be text")
    text = re.sub(r"\s+", " ", "".join(c for c in caption if c.isprintable() or c in "\n\t")).strip()
    if len(text) > MAX_CAPTION:
        raise CaptionError(400, f"keep the caption under {MAX_CAPTION} characters")
    return text


def items(root) -> list:
    """Every uploaded file still on disk, oldest first, with its caption state."""
    root = Path(root)
    notes = load(root)
    out = []
    for sha, rel in uploads.known_files(root).items():
        p = root / rel
        st = p.stat()
        n = notes.get(sha, {})
        caption, skipped = n.get("caption", ""), bool(n.get("skipped"))
        out.append({"sha": sha, "name": p.name, "folder": str(Path(rel).parent.relative_to("assets")),
                    "kind": kind_of(p.name), "size": st.st_size, "mtime": int(st.st_mtime),
                    "caption": caption, "skipped": skipped, "pending": not caption and not skipped})
    return sorted(out, key=lambda i: (i["mtime"], i["name"]))


def by_path(root) -> dict:
    """{relative path: caption state}, for the library listing to show captions next to files."""
    notes = load(root)
    out = {}
    for sha, rel in uploads.known_files(root).items():
        n = notes.get(sha, {})
        out[rel] = {"sha": sha, "caption": n.get("caption", ""), "skipped": bool(n.get("skipped"))}
    for rel, sha in prepare.outputs(root).items():          # an edit-ready copy carries the caption of its source
        n = notes.get(sha, {})
        out.setdefault(rel, {"sha": sha, "caption": n.get("caption", ""), "skipped": bool(n.get("skipped"))})
    return out


def set_detail(root, sha: str, caption=None, skipped: bool = False) -> dict:
    """Caption a file, skip it, or (empty caption, not skipped) put it back to pending."""
    root = Path(root)
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise CaptionError(404, "unknown file")
    text = clean(caption)
    with _lock:
        rel = uploads.known_files(root).get(sha)
        if rel is None:
            raise CaptionError(404, "unknown file")
        notes = load(root)
        if text:
            notes[sha] = {"name": Path(rel).name, "path": rel, "caption": text, "skipped": False,
                          "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
        elif skipped:
            notes[sha] = {"name": Path(rel).name, "path": rel, "caption": "", "skipped": True,
                          "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
        else:
            notes.pop(sha, None)
        _save(root, notes)
        return {"sha": sha, "caption": text, "skipped": bool(skipped and not text), "pending": not text and not skipped}


def pending_count(root) -> int:
    return sum(1 for i in items(root) if i["pending"])
