"""Resumable, deduplicated uploads into a project. No HTTP in here: the web layer calls these.

The link to the Premiere machine can be slow and a phone drops connections, so a file is not one
request. `start` opens a session (or resumes one), `append` takes the next chunk only if its offset is
exactly where the partial file ends, and `finish` checks the size, hashes the result and moves it into
place. A lost response or a retry is therefore harmless: the server's file length is the single truth.

Two layers stop the same file being stored twice:
  * a fingerprint (name, size, modified time) is checked at `start`, before any bytes move, which
    catches the same file tapped twice or re-sent after a dropped connection;
  * a sha256 is checked at `finish`, which catches the same content under a different name.
Content already in a project's library from before this index existed is not compared; only uploads are.

Photos land in assets/photos, videos in assets/inbox (sorting into A-roll or B-roll is a judgement about
the shot, so it is not guessed at upload time). Nothing partial is ever visible in the library: a file
appears under its final name in one atomic rename, only after it is complete and verified.

Stdlib only: this runs on the Premiere machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from . import projects
from .layout import IMAGE_EXTS, VIDEO_EXTS

CHUNK_MAX = 8 << 20          # one request never carries more than this, so a handler never buffers more
MAX_FILE = 4 << 30
DISK_MARGIN = 2 << 30        # never fill the machine Premiere is working on
STALE_DAYS = 7
READ_BLOCK = 64 * 1024

_lock = threading.RLock()                 # the index, and creating/removing sessions
_session_locks: dict = {}                 # one chunk at a time per session


class UploadError(Exception):
    """A refusal the person should see: `status` is the HTTP code, `extra` rides along in the JSON."""
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def destination(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS:
        return "photos"
    if ext in VIDEO_EXTS:
        return "inbox"
    raise UploadError(400, "only photos and videos can be uploaded here")


def safe_name(name) -> str:
    """The file's own name, with nothing that could climb out of a folder or hide it."""
    if not isinstance(name, str):
        raise UploadError(400, "the file needs a name")
    base = re.split(r"[\\/]", name)[-1]
    base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*').strip().lstrip(".")
    base = re.sub(r"\s+", " ", base)
    stem, ext = os.path.splitext(base)
    if not stem:
        raise UploadError(400, "the file needs a name")
    return stem[:100].rstrip() + ext[:10]


def fingerprint(name: str, size: int, mtime) -> str:
    return hashlib.sha1(f"{name}|{size}|{mtime}".encode()).hexdigest()[:24]


def _dir(root) -> Path:
    return Path(root) / ".umcares" / "uploads"


def _part(root, uid: str) -> Path:
    return _dir(root) / f"{uid}.part"


def _meta_path(root, uid: str) -> Path:
    return _dir(root) / f"{uid}.json"


def _load_index(root) -> dict:
    try:
        idx = json.loads((_dir(root) / "index.json").read_text(encoding="utf-8"))
        return {"by_sha": dict(idx.get("by_sha", {})), "by_fp": dict(idx.get("by_fp", {}))}
    except (OSError, ValueError, AttributeError):
        return {"by_sha": {}, "by_fp": {}}


def _save_index(root, idx: dict) -> None:
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "index.json.part"
    tmp.write_text(json.dumps(idx), encoding="utf-8")
    os.replace(tmp, d / "index.json")


def _info(root, rel: str) -> dict:
    p = Path(root) / rel
    return {"name": p.name, "folder": str(Path(rel).parent.relative_to("assets")), "size": p.stat().st_size}


def known_files(root) -> dict:
    """{sha256: path relative to the project} for every uploaded file that is still on disk."""
    root = Path(root)
    with _lock:
        by_sha = _load_index(root)["by_sha"]
    return {sha: rel for sha, rel in by_sha.items() if (root / rel).is_file()}


def _sessions(root) -> list:
    out = []
    d = _dir(root)
    if d.is_dir():
        for f in d.glob("*.json"):
            if f.name == "index.json":
                continue
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def _sweep(root) -> None:
    """Forget sessions nobody resumed for a week, so abandoned partial videos do not fill the disk."""
    cutoff = time.time() - STALE_DAYS * 86400
    for meta in _sessions(root):
        mp = _meta_path(root, meta["id"])
        if mp.is_file() and mp.stat().st_mtime < cutoff:
            _drop(root, meta["id"])


def _drop(root, uid: str) -> None:
    _part(root, uid).unlink(missing_ok=True)
    _meta_path(root, uid).unlink(missing_ok=True)
    _session_locks.pop(uid, None)


def _meta(root, uid: str) -> dict:
    try:
        return json.loads(_meta_path(root, uid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UploadError(404, "unknown upload; start it again") from None


def start(root, name, size, mtime) -> dict:
    """Open an upload, resume one, or report that this exact file is already in the project."""
    root = Path(root)
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise UploadError(400, "size must be a number") from None
    if not 0 < size <= MAX_FILE:
        raise UploadError(400, f"empty or too large (limit {MAX_FILE >> 30} GB)")
    fname = safe_name(name)
    dest = destination(fname)              # from the cleaned name: "..jpg" must not pass on the raw one and be stored as "jpg"
    fp = fingerprint(fname, size, mtime)
    projects.ensure_ignored(root)
    with _lock:
        _sweep(root)
        idx = _load_index(root)
        rel = idx["by_sha"].get(idx["by_fp"].get(fp, ""), "")
        if rel and (root / rel).is_file():
            return {"status": "have", "file": _info(root, rel)}
        for meta in _sessions(root):
            if meta.get("fp") == fp and _part(root, meta["id"]).is_file():
                return {"status": "resume", "id": meta["id"], "size": size,
                        "offset": _part(root, meta["id"]).stat().st_size}
        if shutil.disk_usage(root).free - size < DISK_MARGIN:
            raise UploadError(507, "not enough free disk space on the machine for this file")
        uid = secrets.token_hex(8)
        _dir(root).mkdir(parents=True, exist_ok=True)
        _part(root, uid).touch()
        _meta_path(root, uid).write_text(json.dumps(
            {"id": uid, "name": fname, "dest": dest, "size": size, "fp": fp, "created": int(time.time())}), encoding="utf-8")
        return {"status": "new", "id": uid, "size": size, "offset": 0}


def status(root, uid: str) -> dict:
    meta = _meta(root, uid)
    part = _part(root, uid)
    return {"offset": part.stat().st_size if part.is_file() else 0, "size": meta["size"]}


def _session_lock(uid: str) -> threading.Lock:
    with _lock:
        return _session_locks.setdefault(uid, threading.Lock())


def append(root, uid: str, offset: int, stream, length: int) -> dict:
    """Add one chunk. It must start exactly where the partial file ends; otherwise nothing is written."""
    meta = _meta(root, uid)
    if not 0 < length <= CHUNK_MAX:
        raise UploadError(400, f"a chunk must be between 1 byte and {CHUNK_MAX >> 20} MB")
    with _session_lock(uid):
        part = _part(root, uid)
        if not part.is_file():
            raise UploadError(404, "unknown upload; start it again")
        cur = part.stat().st_size
        if offset != cur:
            raise UploadError(409, "that chunk does not start where the upload stands", offset=cur)
        if cur + length > meta["size"]:
            raise UploadError(400, "more data than the file declared")
        got = 0
        try:
            with open(part, "ab") as f:
                while got < length:
                    buf = stream.read(min(READ_BLOCK, length - got))
                    if not buf:
                        break
                    f.write(buf)
                    got += len(buf)
        except OSError:
            pass                                   # a dropped connection; handled as a short chunk below
        if got != length:
            with open(part, "r+b") as f:           # keep the file exactly as the server last acknowledged it
                f.truncate(cur)
            raise UploadError(400, "the chunk was cut short; send it again", offset=cur)
        return {"offset": cur + got, "size": meta["size"]}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _claim(dirpath: Path, fname: str) -> Path:
    """Reserve a free name by creating it, never check-then-write: uploads finish in parallel."""
    stem, ext = os.path.splitext(fname)
    n, p = 0, dirpath / fname
    while True:
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return p
        except FileExistsError:
            n += 1
            p = dirpath / f"{stem}-{n}{ext}"


def finish(root, uid: str) -> dict:
    """Verify the file is complete, then store it once: a duplicate of known content is dropped."""
    root = Path(root)
    meta = _meta(root, uid)
    with _session_lock(uid):
        part = _part(root, uid)
        have = part.stat().st_size if part.is_file() else 0
        if have != meta["size"]:
            raise UploadError(409, "the upload is not complete yet", offset=have, size=meta["size"])
        sha = _sha256(part)
        with _lock:
            idx = _load_index(root)
            known = idx["by_sha"].get(sha, "")
            if known and (root / known).is_file():
                idx["by_fp"][meta["fp"]] = sha
                _save_index(root, idx)
                _drop(root, uid)
                return {"status": "duplicate", "file": _info(root, known)}
            folder = root / "assets" / meta["dest"]
            folder.mkdir(parents=True, exist_ok=True)
            final = _claim(folder, meta["name"])
            try:
                os.replace(part, final)             # same filesystem (both under the project), so atomic
            except OSError:
                final.unlink(missing_ok=True)
                raise
            rel = str(final.relative_to(root))
            idx["by_sha"][sha] = rel
            idx["by_fp"][meta["fp"]] = sha
            _save_index(root, idx)
            _meta_path(root, uid).unlink(missing_ok=True)
            _session_locks.pop(uid, None)
            return {"status": "saved", "file": _info(root, rel)}
