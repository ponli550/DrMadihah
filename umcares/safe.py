"""Staged writes: never destroy an output before its replacement exists.

The rule is simple and the reason is concrete. To regenerate two title cards I
deleted them — locally and on the remote — and only then discovered the render
API was out of credits. The old cards were gone and the new ones did not exist,
so recovering them meant extracting a text layer back out of a 6.8 GB master.

Anything that takes minutes, costs API credits, or holds a human's edits gets
written to a sibling `.part` file and moved into place only once it is complete.
Either the previous version survives untouched, or the new one lands whole.
There is no state in between, and no `-y` overwriting a good file with a
half-written one.

`os.replace` and `mv` on the same filesystem are atomic, which is why the temp
file is a sibling rather than something under /tmp.
"""
from __future__ import annotations

import os
import shlex
from contextlib import contextmanager
from pathlib import Path

from . import log

SUFFIX = ".part"


def part_path(dest: str) -> str:
    """Sibling temp path that keeps the original extension.

    The extension is preserved because encoders and Premiere both choose
    behaviour from it — `master.part.mxf`, not `master.mxf.part`.
    """
    head, dot, ext = dest.rpartition(".")
    if dot and "/" not in ext:
        return f"{head}{SUFFIX}.{ext}"
    return dest + SUFFIX


@contextmanager
def staged_local(dest: Path, min_bytes: int = 1):
    """Yield a temp path; move it over `dest` only on a clean, non-empty write."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(part_path(str(dest)))
    tmp.unlink(missing_ok=True)
    try:
        yield tmp
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if not tmp.exists() or tmp.stat().st_size < min_bytes:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"refusing to replace {dest.name}: "
                         f"the new file was empty or missing")
    os.replace(tmp, dest)


def commit_remote(t, dest: str, min_bytes: int = 1) -> None:
    """Move `dest.part.ext` over `dest` on the remote, if it is non-empty."""
    tmp = part_path(dest)
    r = t.run(
        f"if [ -s {shlex.quote(tmp)} ] && "
        f"[ $(wc -c < {shlex.quote(tmp)}) -ge {min_bytes} ]; then "
        f"mv -f {shlex.quote(tmp)} {shlex.quote(dest)} && echo COMMITTED; "
        f"else echo EMPTY; fi", timeout=300)
    if "COMMITTED" not in r.stdout:
        t.run(f"rm -f {shlex.quote(tmp)}", timeout=60)
        raise SystemExit(f"refusing to replace {dest}: "
                         f"the new file was empty or missing")


def discard_remote(t, dest: str) -> None:
    """Drop a staged file after a failure, leaving the original in place."""
    try:
        t.run(f"rm -f {shlex.quote(part_path(dest))}", timeout=60)
    except Exception:
        pass


def backup_local(dest: Path, keep: str = ".prev") -> Path | None:
    """Keep one previous copy beside a file a human may have edited.

    Used for the SRT: it is cheap to regenerate but expensive to re-edit, and a
    regeneration silently discarding someone's corrections is its own kind of
    data loss.
    """
    dest = Path(dest)
    if not dest.exists():
        return None
    prev = dest.with_name(dest.name + keep)
    try:
        prev.write_bytes(dest.read_bytes())
        return prev
    except OSError as e:
        log.warn(f"could not keep a previous copy of {dest.name}: {e}")
        return None
