"""Console output.

Rules this module enforces:
  * Human status goes to STDERR. Machine-readable results go to STDOUT.
    That way `umcares media probe --json | jq` works while you still see
    progress on screen.
  * Colour only when stderr is a TTY, so piping and CI logs stay clean.
  * Every long-running step prints a start line and a result line, because a
    silent CLI driving a 7-minute Premiere export is indistinguishable from a
    hung one.
"""
from __future__ import annotations

import json
import os
import sys
import time

_COLOR = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
_VERBOSE = False


def set_verbose(on: bool) -> None:
    global _VERBOSE
    _VERBOSE = on


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(_c("36", "==>") + " " + msg, file=sys.stderr, flush=True)


def step(msg: str) -> None:
    print(_c("34", "  ·") + " " + msg, file=sys.stderr, flush=True)


def ok(msg: str) -> None:
    print(_c("32", "  ✓") + " " + msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(_c("33", "  ! ") + msg, file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(_c("31", "  ✗") + " " + msg, file=sys.stderr, flush=True)


def debug(msg: str) -> None:
    if _VERBOSE:
        print(_c("90", "  ~ ") + msg, file=sys.stderr, flush=True)


def out(data) -> None:
    """The actual result. Only this goes to stdout."""
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False), flush=True)
    else:
        print(data, flush=True)


class Timer:
    """Context manager that reports how long a step took, success or fail."""

    def __init__(self, label: str):
        self.label = label
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.time()
        step(self.label + " …")
        return self

    def __exit__(self, exc_type, exc, tb):
        dt = time.time() - self.t0
        if exc_type is None:
            ok(f"{self.label} ({dt:.1f}s)")
        else:
            err(f"{self.label} failed after {dt:.1f}s: {exc}")
        return False
