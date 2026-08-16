"""A spinner for the long jobs (exports run into minutes).

Design notes:
  * Draws on STDERR only, so piping stdout to jq is never polluted.
  * When stderr is not a TTY (CI, log file, `2>file`) it degrades to one plain
    heartbeat line every 30s instead of thousands of escape codes.
  * Shows elapsed time and, when we know roughly how long the job takes, a
    friendly estimate. Cycles through a few messages so a long wait does not
    look frozen.
"""
from __future__ import annotations

import itertools
import os
import random
import shutil
import sys
import threading
import time

TTY = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
QUIET = os.environ.get("UMC_NO_SPINNER") == "1"

# a cat, running back and forth
CAT = ["ᓚᘏᗢ   ", " ᓚᘏᗢ  ", "  ᓚᘏᗢ ", "   ᓚᘏᗢ", "  ᓚᘏᗢ ", " ᓚᘏᗢ  "]
DOTS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

CHATTER = [
    "jap ye, {mins} minit je…",
    "sapkok lu, tengah proses…",
    "sabar… komputer tu pun penat",
    "jangan tutup terminal ye",
    "still cooking…",
    "almost… tapi tak lagi",
]

DONE = ["siap!", "dah settle", "beres", "done"]


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


class Spinner:
    def __init__(self, label: str, estimate_seconds: float | None = None):
        self.label = label
        self.estimate = estimate_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.t0 = 0.0

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "Spinner":
        self.t0 = time.time()
        if QUIET:
            print(f"  · {self.label} …", file=sys.stderr, flush=True)
        elif TTY:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(f"  · {self.label} …", file=sys.stderr, flush=True)
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        dt = time.time() - self.t0
        if TTY and not QUIET:
            sys.stderr.write("\r" + " " * (shutil.get_terminal_size((80, 20)).columns - 1) + "\r")
        if exc_type is None:
            word = random.choice(DONE)
            print(f"\033[32m  ✓\033[0m {self.label} — {word} ({_fmt(dt)})"
                  if TTY else f"  ✓ {self.label} — {word} ({_fmt(dt)})",
                  file=sys.stderr, flush=True)
        else:
            print(f"\033[31m  ✗\033[0m {self.label} — gagal after {_fmt(dt)}: {exc}"
                  if TTY else f"  ✗ {self.label} — failed after {_fmt(dt)}: {exc}",
                  file=sys.stderr, flush=True)
        return False

    # -- renderers ----------------------------------------------------------
    def _message(self, elapsed: float) -> str:
        if self.estimate:
            left = max(0.0, self.estimate - elapsed)
            mins = max(1, int(round(left / 60)))
            if left > 20:
                return CHATTER[0].format(mins=mins)
        idx = int(elapsed // 8) % len(CHATTER)
        return CHATTER[idx].format(mins=max(1, int((self.estimate or 60) / 60)))

    def _spin(self) -> None:
        cats = itertools.cycle(CAT)
        dots = itertools.cycle(DOTS)
        while not self._stop.is_set():
            elapsed = time.time() - self.t0
            cols = shutil.get_terminal_size((80, 20)).columns
            line = (f"\033[36m{next(dots)}\033[0m {self.label} "
                    f"\033[90m{next(cats)}\033[0m "
                    f"\033[90m{_fmt(elapsed)} · {self._message(elapsed)}\033[0m")
            # crude but effective width guard: escape codes are not printable
            visible = len(self.label) + len(_fmt(elapsed)) + len(self._message(elapsed)) + 14
            if visible > cols - 2:
                line = f"\033[36m{next(dots)}\033[0m {self.label} \033[90m{_fmt(elapsed)}\033[0m"
            sys.stderr.write("\r" + " " * (cols - 1) + "\r" + line)
            sys.stderr.flush()
            self._stop.wait(0.12)

    def _heartbeat(self) -> None:
        """Non-TTY: one line every 30s so CI logs show progress without spam."""
        while not self._stop.wait(30):
            print(f"    … {self.label} still running ({_fmt(time.time() - self.t0)})",
                  file=sys.stderr, flush=True)


def spin(label: str, estimate_seconds: float | None = None) -> Spinner:
    return Spinner(label, estimate_seconds)
