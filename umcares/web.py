"""umcares web: a read-only library browser, meant to run on the Premiere machine.

The media lives on that machine, so the UI does too; running it anywhere else
would push every byte through the tmux/ssh channel. It binds 127.0.0.1 only and
is published to the tailnet with `tailscale serve` (never Funnel).

Access follows the same rule as SekerinShotto's receiver. `tailscale serve`
stamps every proxied request with the sender's login (Tailscale-User-Login):
only the configured owner is let in, and with no owner configured nobody is —
a tailnet can hold other people's devices. A request with no such header came
straight to 127.0.0.1 and is allowed only if its Host is localhost, which is
what stops a DNS-rebinding page from reading the library.

Phase 1 is GET-only. Nothing here writes, deletes or runs a command; the files
it serves are confined to the library folders under <root>/assets.

Stdlib only: this has to start on a machine that has Python and nothing else.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEFAULT_PORT = 8733      # 8732 is SekerinShotto's
BASE = "/umcares"        # where `tailscale serve` mounts us; it strips this before we see the path

# (folder under <root>/assets, label). Shallow listing: no recursion.
FOLDERS = [
    ("footage/A", "A-roll"),
    ("footage/B", "B-roll"),
    ("photos", "Photos"),
    ("edit_ready", "Edit ready (H.264)"),
    ("music", "Music"),
    ("logos", "Logos"),
    ("vo", "Voiceover"),
    ("cards", "Cards"),
]
FOLDER_NAMES = {f for f, _ in FOLDERS}

VIDEO = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
IMAGE = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}
AUDIO = {".mp3", ".wav", ".m4a", ".aac"}
CHUNK = 1 << 20


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in VIDEO:
        return "video"
    if ext in IMAGE:
        return "image"
    if ext in AUDIO:
        return "audio"
    return "file"


def authorized(headers, owner: str) -> bool:
    login = headers.get("Tailscale-User-Login")
    if login is not None:
        return bool(owner) and login.lower() == owner.lower()
    return (headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")


def list_library(root) -> dict:
    """Every library folder with its files. Hidden files and in-flight `.part` files are skipped."""
    assets = Path(root) / "assets"
    out = []
    for folder, label in FOLDERS:
        d = assets / folder
        files = []
        if d.is_dir():
            for p in sorted(d.iterdir(), key=lambda p: p.name.lower()):
                if p.name.startswith(".") or ".part" in p.suffixes or not p.is_file():
                    continue
                st = p.stat()
                files.append({"name": p.name, "size": st.st_size,
                              "mtime": int(st.st_mtime), "kind": kind_of(p.name)})
        out.append({"folder": folder, "label": label, "exists": d.is_dir(), "files": files})
    return {"folders": out}


def resolve_media(root, rel: str):
    """The file at <root>/assets/<rel>, or None. Only library folders, only real files inside assets/.

    `resolve()` follows symlinks before the containment check, so a link in a
    library folder cannot lead the server out of assets/.
    """
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 2 or any(p.startswith(".") or "\\" in p or "\x00" in p for p in parts):
        return None
    if "/".join(parts[:-1]) not in FOLDER_NAMES:
        return None
    assets = (Path(root) / "assets").resolve()
    try:
        p = assets.joinpath(*parts).resolve()
        p.relative_to(assets)
    except (ValueError, OSError):
        return None
    return p if p.is_file() else None


def parse_range(header, size: int):
    """(start, end) inclusive, None to ignore the header, ValueError if unsatisfiable."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    a, b = m.groups()
    if a == "":                                  # suffix form: the last N bytes
        n = int(b)
        if n == 0 or size == 0:
            raise ValueError("unsatisfiable")
        return max(0, size - n), size - 1
    start, end = int(a), min(int(b) if b else size - 1, size - 1)
    if start >= size or start > end:
        raise ValueError("unsatisfiable")
    return start, end


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>umcares</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--line:#dcdcd6;--fg:#1c1c1a;--dim:#6b6b66;--acc:#1f5fbf}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1b1e26;--line:#2a2d36;--fg:#e8e8ea;--dim:#9a9aa3;--acc:#7fb0ff}}
*{box-sizing:border-box}
body{margin:0;padding:16px;font:16px -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:900px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}.dim{color:var(--dim);font-size:14px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:14px 0;padding:12px}
h2{font-size:15px;margin:0 0 8px;display:flex;justify-content:space-between;gap:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.item{min-width:0}.thumb{width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:6px;background:var(--line);display:block}
.name{font-size:13px;word-break:break-all;margin-top:4px}
a{color:var(--acc)}
</style></head><body><main>
<h1>umcares</h1><div class="dim" id="status">Loading…</div>
<div id="lib"></div>
</main>
<script>
const BASE = __BASE__;
const mb = n => n < 1048576 ? Math.max(1, Math.round(n/1024)) + " KB" : (n/1048576).toFixed(1) + " MB";
const url = (folder, name) => BASE + "/media/" + folder.split("/").concat([name]).map(encodeURIComponent).join("/");
function el(tag, props, ...kids){
  const e = document.createElement(tag);
  Object.assign(e, props || {});
  kids.forEach(k => e.append(k));
  return e;
}
function thumb(f, folder){
  const src = url(folder, f.name);
  if (f.kind === "image") return el("img", {className:"thumb", src, loading:"lazy", alt:f.name});
  if (f.kind === "video") return el("video", {className:"thumb", src, preload:"metadata", controls:true, muted:true, playsInline:true});
  if (f.kind === "audio") return el("audio", {src, preload:"none", controls:true, style:"width:100%"});
  return el("a", {href:src}, "open");
}
fetch(BASE + "/api/library").then(r => { if(!r.ok) throw new Error(r.status); return r.json(); }).then(d => {
  const lib = document.getElementById("lib");
  let n = 0, bytes = 0;
  d.folders.forEach(g => {
    n += g.files.length; g.files.forEach(f => bytes += f.size);
    const grid = el("div", {className:"grid"});
    g.files.forEach(f => grid.append(el("div", {className:"item"}, thumb(f, g.folder),
      el("div", {className:"name"}, f.name), el("div", {className:"dim"}, mb(f.size)))));
    if (!g.files.length) grid.append(el("div", {className:"dim"}, g.exists ? "empty" : "folder missing"));
    lib.append(el("section", {}, el("h2", {}, el("span", {}, g.label), el("span", {className:"dim"}, g.files.length + " files")), grid));
  });
  document.getElementById("status").textContent = n + " files · " + mb(bytes) + " · read-only";
}).catch(e => { document.getElementById("status").textContent = "Could not load the library (" + e.message + ")"; });
</script></body></html>
"""


def make_handler(root, owner: str):
    class H(BaseHTTPRequestHandler):
        server_version = "umcares-web"

        def log_message(self, fmt, *args):       # one quiet line per request, on stderr
            sys.stderr.write("web: " + (fmt % args) + "\n")

        def _send(self, code, body: bytes, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, code, msg):
            self._send(code, (msg + "\n").encode(), "text/plain; charset=utf-8")

        def _media(self, path: Path):
            size = path.stat().st_size
            try:
                rng = parse_range(self.headers.get("Range"), size)
            except ValueError:
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            start, end = rng if rng else (0, size - 1)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD" or size == 0:
                return
            left = end - start + 1
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    while left > 0:
                        buf = f.read(min(CHUNK, left))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        left -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass                             # the viewer scrubbed or left; nothing to clean up

        def do_GET(self):
            if not authorized(self.headers, owner):
                return self._text(403, "forbidden")
            path = unquote(urlsplit(self.path).path)
            if path in ("/", "/index.html"):
                page = PAGE.replace("__BASE__", json.dumps(BASE).replace("</", "<\\/"))
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if path == "/api/library":
                return self._send(200, json.dumps(list_library(root)).encode(), "application/json")
            if path.startswith("/media/"):
                p = resolve_media(root, path[len("/media/"):])
                return self._media(p) if p else self._text(404, "not found")
            return self._text(404, "not found")

        do_HEAD = do_GET

        def _refuse_write(self):
            self._text(405, "read-only")

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse_write

    return H


def make_server(root, owner: str = "", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(root, owner))


def keep_awake():
    """Hold the Mac awake while this runs. Its idle sleep is 1 minute, and a sleeping Mac drops every upload."""
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except FileNotFoundError:                    # not macOS
        return None


def serve_command(port: int) -> list:
    return ["tailscale", "serve", "--bg", "--set-path", BASE, f"http://127.0.0.1:{port}"]


def run(root, owner: str = "", port: int = DEFAULT_PORT, awake: bool = True) -> int:
    if not owner:
        sys.stderr.write("web: no owner set (UMC_WEB_OWNER / --owner): tailnet requests will be refused, "
                         "only this machine can reach the page\n")
    srv = make_server(root, owner, port)
    if awake:
        keep_awake()
    sys.stderr.write(f"web: serving {Path(root) / 'assets'} on http://127.0.0.1:{port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="project root that holds assets/")
    ap.add_argument("--owner", default=os.environ.get("UMC_WEB_OWNER", ""))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-caffeinate", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.root, a.owner, a.port, not a.no_caffeinate))
