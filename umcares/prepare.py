"""Prepare uploaded videos for editing: the same footage, but in a form Premiere handles predictably.

A phone clip is usually not edit-ready even when it is H.264: variable frame rate drifts audio against picture,
a rotation flag instead of rotated pixels, full-range colour (yuvj420p) and a moov atom at the end of the file all
cause trouble. Each clip is probed and gets the least work that makes it safe:

  copy      already conformant: an instant clone (APFS clones share the data, so it costs no disk)
  remux     the picture is fine but the audio or the file layout is not: rewrite the container, no re-encode
  reencode  anything wrong with the picture: libx264, constant frame rate, rotation applied, limited-range yuv420p

The settings match the project's existing media prepare (libx264, CRF 16, AAC 192k, faststart). The Premiere
machine is usually busy, so one job runs at a time, at low priority (`nice`), with capped threads, and a job can
be cancelled. Output is written to a hidden .part file and moved into place only when complete and verified, so
edit_ready/ never holds a half-written clip.

Stdlib only: this runs on the Premiere machine.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from fractions import Fraction
from pathlib import Path

from . import uploads
from .layout import kind_of

TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")        # launchd's PATH does not include brew
# 120 and 240 are kept as they are: flattening slow-motion footage to 60 would throw away the slow motion
STANDARD_FPS = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
                Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(120), Fraction(240)]
SOURCE_FOLDERS = ("inbox", "footage/A", "footage/B")        # where unprocessed video lives
OUT_FOLDER = "edit_ready"
PRESET, CRF, AUDIO_KBPS, THREADS = "medium", "16", "192", "4"
NICE = "15"
DISK_MARGIN = 2 << 30
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


class PrepareError(Exception):
    """The clip cannot be prepared; the message is meant to be shown."""


def find_tool(name: str):
    return shutil.which(name) or next((f"{d}/{name}" for d in TOOL_DIRS if os.access(f"{d}/{name}", os.X_OK)), None)


# ---------------------------------------------------------------- probing
def _frac(text) -> Fraction:
    try:
        f = Fraction(str(text))
        return f if f.denominator else Fraction(0)
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def _faststart(path: Path) -> bool:
    """True when the moov atom comes before the media data, so the file can start playing before it is all read."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            pos = 0
            while pos < size:
                f.seek(pos)
                head = f.read(16)
                if len(head) < 8:
                    return False
                n, kind = struct.unpack(">I4s", head[:8])
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if n == 1 and len(head) >= 16:
                    n = struct.unpack(">Q", head[8:16])[0]
                if n < 8:
                    return False
                pos += n
    except OSError:
        pass
    return False


def _timing(ffprobe: str, path: Path) -> dict:
    """How evenly the frames are spaced: constant frame rate has one gap, variable frame rate has several."""
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=120)
    pts = []
    for line in r.stdout.splitlines():
        try:
            pts.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue
    pts.sort()
    gaps = [b - a for a, b in zip(pts, pts[1:]) if b - a > 1e-6]
    if len(gaps) < 2:
        return {"frames": len(pts), "median_gap": gaps[0] if gaps else 0.0, "irregular": 0.0}
    gaps_sorted = sorted(gaps)
    median = gaps_sorted[len(gaps_sorted) // 2]
    off = sum(1 for g in gaps if abs(g - median) > 0.2 * median)
    return {"frames": len(pts), "median_gap": median, "irregular": off / len(gaps)}


def probe(path) -> dict:
    """What matters for editing, from ffprobe. Raises PrepareError for a file that is not a readable video."""
    path = Path(path)
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        raise PrepareError("ffprobe is not installed on this machine")
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout or "{}")
    except (subprocess.SubprocessError, ValueError) as e:
        raise PrepareError(f"could not read the file: {e}") from None
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if r.returncode != 0 or v is None:
        raise PrepareError("this file has no readable video")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    rot = 0
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"]))) % 360
    if not rot and "rotate" in v.get("tags", {}):
        try:
            rot = int(float(v["tags"]["rotate"])) % 360
        except ValueError:
            pass
    timing = _timing(ffprobe, path)
    width, height = int(v.get("width", 0)), int(v.get("height", 0))
    swapped = rot in (90, 270)
    try:
        duration = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    except ValueError:
        duration = 0.0
    avg, real = _frac(v.get("avg_frame_rate")), _frac(v.get("r_frame_rate"))
    base = 1.0 / timing["median_gap"] if timing["median_gap"] else float(real or avg)
    return {
        "duration": duration, "size": path.stat().st_size, "faststart": _faststart(path),
        "video": {"codec": v.get("codec_name"), "profile": v.get("profile"), "pix_fmt": v.get("pix_fmt"),
                  "width": height if swapped else width, "height": width if swapped else height,
                  "fps": base, "avg": float(avg), "rotation": rot, "range": v.get("color_range"),
                  "space": v.get("color_space"), "transfer": v.get("color_transfer"), "primaries": v.get("color_primaries"),
                  "irregular": timing["irregular"], "frames": timing["frames"]},
        "audio": None if a is None else {"codec": a.get("codec_name"), "rate": int(a.get("sample_rate") or 0),
                                          "channels": int(a.get("channels") or 0)},
    }


# ---------------------------------------------------------------- planning
def nearest_fps(fps: float) -> Fraction:
    return min(STANDARD_FPS, key=lambda s: abs(float(s) - fps))


def _is_standard(fps: float) -> bool:
    return abs(float(nearest_fps(fps)) - fps) / max(fps, 1e-9) < 0.003


def make_plan(info: dict, fps=None) -> dict:
    """Decide the least work that makes the clip safe, and say why in words a person can read."""
    v, a = info["video"], info["audio"]
    reasons, warnings = [], []
    variable = v["irregular"] > 0.01 or not _is_standard(v["fps"]) or (v["avg"] and abs(v["avg"] - v["fps"]) / v["fps"] > 0.02)
    target = Fraction(fps).limit_denominator(1001) if fps else nearest_fps(v["fps"])
    if v["codec"] != "h264":
        reasons.append(f"codec {v['codec']}")
    if v["pix_fmt"] != "yuv420p":
        reasons.append("full-range colour" if v["pix_fmt"] == "yuvj420p" else f"pixel format {v['pix_fmt']}")
    if variable:
        reasons.append("variable frame rate")
    elif fps and abs(float(target) - v["fps"]) > 0.01:
        reasons.append(f"frame rate {round(v['fps'], 2):g} to {float(target):g}")
    if v["rotation"]:
        reasons.append(f"rotated {v['rotation']}°")
    if v["transfer"] in HDR_TRANSFERS:
        warnings.append("HDR clip: it is converted to standard colour without tone mapping, so it may look flat")
    audio_fix = a is not None and (a["codec"] != "aac" or a["rate"] != 48000 or a["channels"] > 2)
    if audio_fix:
        reasons.append("audio is not AAC 48 kHz")
    if not info["faststart"]:
        reasons.append("file layout (not faststart)")
    picture_fix = bool(reasons) and any(not r.startswith(("audio", "file layout")) for r in reasons)
    action = "reencode" if picture_fix else ("remux" if reasons else "copy")
    return {"action": action, "reasons": reasons, "warnings": warnings, "fps": f"{target.numerator}/{target.denominator}",
            "fps_value": float(target), "range_fix": v["range"] == "pc" or v["pix_fmt"] == "yuvj420p"}


def build_command(ffmpeg: str, src: Path, dst: Path, info: dict, plan: dict) -> list:
    """The ffmpeg command for this plan (never used for `copy`). Low priority and capped threads: Premiere is running."""
    v = info["video"]
    cmd = []
    nice = shutil.which("nice")
    if nice:
        cmd += [nice, "-n", NICE]
    cmd += [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-progress", "pipe:1", "-nostats", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "-1"]
    if plan["action"] == "reencode":
        vf = []
        if plan["range_fix"]:
            vf.append("scale=in_range=pc:out_range=tv")
        vf += [f"fps={plan['fps']}", "format=yuv420p"]
        space = v["space"] if v["space"] in ("bt709", "bt470bg", "smpte170m", "bt2020nc") else "bt709"
        cmd += ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-threads", THREADS, "-colorspace", space,
                "-color_primaries", v["primaries"] if v["primaries"] in ("bt709", "bt470bg", "smpte170m", "bt2020") else "bt709",
                "-color_trc", "bt709" if v["transfer"] in (None, "unknown") or v["transfer"] in HDR_TRANSFERS else v["transfer"],
                "-color_range", "tv"]
    else:
        cmd += ["-c:v", "copy"]
    if info["audio"] is None:
        cmd += ["-an"]
    elif plan["action"] == "reencode" or any(r.startswith("audio") for r in plan["reasons"]):
        cmd += ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", "48000", "-ac", "2"]
        if plan["action"] == "reencode":
            cmd += ["-af", "aresample=async=1:first_pts=0"]
    else:
        cmd += ["-c:a", "copy"]
    return cmd + ["-movflags", "+faststart", "-f", "mp4", str(dst)]


# ---------------------------------------------------------------- state on disk
_lock = threading.RLock()
_probes: dict = {}                       # sha -> probe, so listing the candidates does not re-probe every time
_unreadable: dict = {}                   # sha -> why; content never changes under a hash, so a bad file stays bad


def _state_path(root) -> Path:
    return Path(root) / ".umcares" / "prepared.json"


def _load(root) -> dict:
    try:
        d = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(root, state: dict) -> None:
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, p)


def outputs(root) -> dict:
    """{edit-ready file (relative path): sha of the clip it was made from}, for files that still exist."""
    root = Path(root)
    with _lock:
        return {e["output"]: sha for sha, e in _load(root).items() if (root / e["output"]).is_file()}


def _cached_probe(root, sha: str, rel: str) -> dict:
    with _lock:
        if sha in _unreadable:
            raise PrepareError(_unreadable[sha])
        if sha not in _probes:
            try:
                _probes[sha] = probe(Path(root) / rel)
            except PrepareError as e:
                _unreadable[sha] = str(e)
                raise
        return _probes[sha]


def _basics(root) -> list:
    """Uploaded videos in the raw folders and whether each is already prepared. No probing: this is cheap."""
    root = Path(root)
    state = _load(root)
    out = []
    for sha, rel in uploads.known_files(root).items():
        folder = str(Path(rel).parent.relative_to("assets"))
        if kind_of(rel) != "video" or folder not in SOURCE_FOLDERS:
            continue
        entry = state.get(sha)
        done = bool(entry and (root / entry["output"]).is_file())
        out.append({"sha": sha, "rel": rel, "name": Path(rel).name, "folder": folder, "size": (root / rel).stat().st_size,
                    "state": "prepared" if done else "todo", "output": entry["output"] if done else None,
                    "plan": {k: entry.get(k) for k in ("action", "reasons")} if done else None, "error": None})
    return out


def candidates(root, fps=None, budget: int = 3) -> list:
    """Videos that can be prepared, with what preparing would do. At most `budget` not-yet-probed clips are probed
    per call (a long clip takes seconds to scan); the rest come back with plan None and the page asks again."""
    root = Path(root)
    out = []
    for item in _basics(root):
        if item["state"] == "todo":
            with _lock:
                cached = item["sha"] in _probes or item["sha"] in _unreadable
            if cached or budget > 0:
                if not cached:
                    budget -= 1
                try:
                    item["plan"] = make_plan(_cached_probe(root, item["sha"], item["rel"]), fps)
                except PrepareError as e:
                    item["state"], item["error"] = "unreadable", str(e)
        item.pop("rel")
        out.append(item)
    return sorted(out, key=lambda i: (i["state"] != "todo", i["name"].lower()))


# ---------------------------------------------------------------- running jobs
def _clone(src: Path, dst: Path) -> None:
    """An instant copy-on-write clone on APFS (no extra disk), an ordinary copy anywhere else."""
    cp = shutil.which("cp")
    if cp and subprocess.run([cp, "-c", str(src), str(dst)], capture_output=True).returncode == 0:
        return
    shutil.copyfile(src, dst)


class Preparer:
    """One worker, one clip at a time: the machine is shared with Premiere."""

    def __init__(self):
        self.cv = threading.Condition(threading.RLock())
        self.jobs: list = []
        self.thread = None
        self.proc = None
        self.cancel_flag = False

    # -- public
    def submit(self, root, shas, fps=None) -> list:
        """Queue clips for preparing. Validates every hash before queuing any, so a bad request queues nothing."""
        root = Path(root)
        known = {c["sha"]: c for c in _basics(root)}
        for sha in shas:
            if sha not in known:
                raise PrepareError("that is not a video that can be prepared")
        made = []
        with self.cv:
            for sha in shas:
                c = known[sha]
                if c["state"] == "prepared":
                    continue
                if any(j["root"] == str(root) and j["sha"] == sha and j["state"] in ("queued", "running") for j in self.jobs):
                    continue
                job = {"id": f"{int(time.time() * 1000):x}{len(self.jobs)}", "root": str(root), "sha": sha, "name": c["name"],
                       "state": "queued", "percent": 0, "error": None, "output": None, "fps": fps,
                       "action": None, "reasons": [], "warnings": []}
                self.jobs.append(job)
                made.append(job)
            self.jobs = self.jobs[-200:]
            if made and (self.thread is None or not self.thread.is_alive()):
                self.cancel_flag = False
                self.thread = threading.Thread(target=self._work, daemon=True)
                self.thread.start()
            self.cv.notify_all()
        return [dict(j) for j in made]

    def snapshot(self, root) -> list:
        with self.cv:
            return [dict(j) for j in self.jobs if j["root"] == str(root)][-30:]

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
                    n += 1
            running = next((j for j in self.jobs if j["root"] == str(root) and j["state"] == "running"), None)
            if running:
                self.cancel_flag = True
                if self.proc:
                    try:
                        self.proc.terminate()
                    except OSError:
                        pass
        return n

    def wait_idle(self, timeout=60.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if not self.active():
                return True
            time.sleep(0.05)
        return False

    # -- worker
    def _next(self):
        with self.cv:
            for j in self.jobs:
                if j["state"] == "queued":
                    j["state"] = "running"
                    return j
        return None

    def _work(self):
        while True:
            job = self._next()
            if job is None:
                return
            try:
                self._run(job)
            except PrepareError as e:
                self._finish(job, "failed", str(e))
            except Exception as e:                       # a bug must not kill the worker and strand the queue
                self._finish(job, "failed", f"unexpected error: {e}")

    def _finish(self, job, state, error=None, output=None):
        with self.cv:
            job["state"], job["error"] = state, error
            if output:
                job["output"], job["percent"] = output, 100
            self.proc = None
            self.cancel_flag = False
            self.cv.notify_all()

    def _run(self, job):
        root = Path(job["root"])
        rel = uploads.known_files(root).get(job["sha"])
        if rel is None:
            raise PrepareError("the file is gone")
        src = root / rel
        info = _cached_probe(root, job["sha"], rel)
        plan = make_plan(info, job["fps"])
        with self.cv:
            job["action"], job["reasons"], job["warnings"] = plan["action"], plan["reasons"], plan["warnings"]
        folder = root / "assets" / OUT_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free - 2 * src.stat().st_size < DISK_MARGIN:
            raise PrepareError("not enough free disk space on the machine to prepare this clip")
        for stale in folder.glob("*.part.mp4"):          # one worker, so any .part file now is debris from a crash
            stale.unlink(missing_ok=True)
        stem = uploads.safe_name(src.stem + ".mp4")
        fd, name = tempfile.mkstemp(prefix=Path(stem).stem + ".", suffix=".part.mp4", dir=folder)
        os.close(fd)
        part = Path(name)
        try:
            if plan["action"] == "copy":
                _clone(src, part)
                self._set_percent(job, 90)
            else:
                ffmpeg = find_tool("ffmpeg")
                if not ffmpeg:
                    raise PrepareError("ffmpeg is not installed on this machine")
                self._encode(job, build_command(ffmpeg, src, part, info, plan), info["duration"])
            self._verify(part, info)
            final = uploads.claim_name(folder, stem)
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            if self.cancel_flag:
                self._finish(job, "cancelled")
                return
            raise
        rel_out = str(final.relative_to(root))
        with _lock:
            state = _load(root)
            state[job["sha"]] = {"output": rel_out, "action": plan["action"], "reasons": plan["reasons"],
                                 "fps": plan["fps"], "at": int(time.time()), "output_size": final.stat().st_size}
            _save(root, state)
        self._finish(job, "done", output=rel_out)

    def _set_percent(self, job, pct):
        with self.cv:
            job["percent"] = max(job["percent"], min(99, int(pct)))

    def _encode(self, job, cmd, duration):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        with self.cv:
            self.proc = proc
            if self.cancel_flag:
                proc.terminate()
        errors = []
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        secs = int(line.split("=", 1)[1]) / 1_000_000
                        if duration > 0:
                            self._set_percent(job, 100 * secs / duration)
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
            proc.stdout.close()                          # a cancelled or failed encode must not leak its pipe
        if self.cancel_flag:
            raise PrepareError("cancelled")
        if rc != 0:
            raise PrepareError("ffmpeg failed: " + (" ".join(errors[-3:]) or f"exit code {rc}")[:300])

    def _verify(self, part: Path, src_info: dict) -> None:
        """Trust nothing: the result must be a readable video about as long as the source."""
        got = probe(part)
        if abs(got["duration"] - src_info["duration"]) > max(1.0, 0.03 * src_info["duration"]):
            raise PrepareError(f"the prepared clip is {got['duration']:.1f} s but the original is {src_info['duration']:.1f} s")


PREPARER = Preparer()
