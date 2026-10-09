"""umcares web: projects and their media, meant to run on the Premiere machine.

The media lives on that machine, so the UI does too; running it anywhere else would push
every byte through the tmux/ssh channel. It binds 127.0.0.1 only and is published to the
tailnet with `tailscale serve` (never Funnel).

Access follows the same rule as SekerinShotto's receiver. `tailscale serve` stamps every
proxied request with the sender's login (Tailscale-User-Login): only the configured owner
is let in, and with no owner configured nobody is — a tailnet can hold other people's
devices. A request with no such header came straight to 127.0.0.1 and is allowed only if
its Host is localhost, which is what stops a DNS-rebinding page from reading the library.

The writes are creating a project and uploading media. Because the login header is added automatically, a
page on another site open in the owner's browser could otherwise forge that POST, so it
must carry a custom header and a JSON body, which a cross-site form cannot send.

Files served are confined to a project's library folders under <project>/assets.
Uploads are resumable chunks handled by uploads.py; nothing partial is ever listed.

Run as a module (`python3 -m umcares.web`); stdlib only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import projects, uploads
from .layout import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS

DEFAULT_PORT = 8733      # 8732 is SekerinShotto's
BASE = "/umcares"        # where `tailscale serve` mounts us; it strips this before we see the path
CSRF_HEADER = ("X-Requested-With", "umcares")
MAX_BODY = 16 * 1024

# (folder under <project>/assets, label). Shallow listing: no recursion.
FOLDERS = [
    ("footage/A", "A-roll"),
    ("footage/B", "B-roll"),
    ("photos", "Photos"),
    ("inbox", "Inbox (new videos, unsorted)"),
    ("edit_ready", "Edit ready (H.264)"),
    ("music", "Music"),
    ("logos", "Logos"),
    ("vo", "Voiceover"),
    ("cards", "Cards"),
]
FOLDER_NAMES = {f for f, _ in FOLDERS}

CHUNK = 1 << 20

THUMB_W = 360                                    # px; a tile is ~20 KB instead of a video's first megabytes
FFMPEG_AT = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")   # launchd's PATH does not include brew
_thumb_slots = threading.BoundedSemaphore(2)     # a few ffmpeg at once, not 28 when a page opens
PUBLIC_FIELDS = ("slug", "name", "event_date", "notes", "status", "created", "external", "missing")


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return "file"


def authorized(headers, owner: str) -> bool:
    login = headers.get("Tailscale-User-Login")
    if login is not None:
        return bool(owner) and login.lower() == owner.lower()
    return (headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")


def public(p: dict) -> dict:
    """A project as the page may see it: never the absolute path on the machine."""
    return {k: p.get(k) for k in PUBLIC_FIELDS}


def project_root(home, slug: str):
    p = projects.find_project(home, slug)
    return Path(p["path"]) if p and not p["missing"] else None


def thumb_cache(home) -> Path:
    """Outside every project, so a thumbnail never shows up as an untracked file in a project's git repo."""
    return Path(home) / ".cache" / "thumbs"


def list_folder(d: Path) -> list:
    files = []
    if d.is_dir():
        for p in sorted(d.iterdir(), key=lambda p: p.name.lower()):
            if p.name.startswith(".") or ".part" in p.suffixes or not p.is_file():
                continue
            st = p.stat()
            files.append({"name": p.name, "size": st.st_size,
                          "mtime": int(st.st_mtime), "kind": kind_of(p.name)})
    return files


def list_library(root) -> dict:
    """Every library folder of one project with its files. Hidden files and in-flight `.part` files are skipped."""
    assets = Path(root) / "assets"
    out = []
    for folder, label in FOLDERS:
        d = assets / folder
        out.append({"folder": folder, "label": label, "exists": d.is_dir(), "files": list_folder(d)})
    return {"folders": out}


def resolve_media(root, rel: str):
    """The file at <project>/assets/<rel>, or None. Only library folders, only real files inside assets/.

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


def find_ffmpeg():
    return shutil.which("ffmpeg") or next((p for p in FFMPEG_AT if os.access(p, os.X_OK)), None)


def thumb_path(cache, src: Path):
    """Where this file's thumbnail lives (made or not), or None for a kind that has none."""
    if kind_of(src.name) not in ("video", "image"):
        return None
    st = src.stat()
    # resolved, so the same file asked for by two routes (or through a symlinked root) is one cache entry
    key = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:20]
    return Path(cache) / (key + ".jpg")


def thumb_for(cache, src: Path):
    """A cached JPEG for a video or image, or None if it cannot be made (no ffmpeg, unreadable, HEIC).

    The cache key includes size and mtime, so replacing a file replaces its thumbnail. The JPEG is
    written to a sibling .part and moved into place, so a request never sees half a file.
    """
    out = thumb_path(cache, src)
    if out is None:
        return None
    if out.is_file():
        return out
    ff = find_ffmpeg()
    if not ff:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.jpg")
    seek = ["-ss", "0.5"] if kind_of(src.name) == "video" else []
    cmd = [ff, "-v", "error", "-y", *seek, "-i", str(src), "-frames:v", "1",
           "-vf", f"scale={THUMB_W}:-2", "-q:v", "5", str(tmp)]
    with _thumb_slots:
        if out.is_file():                        # another request made it while we waited
            return out
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
            os.replace(tmp, out)
        except (subprocess.SubprocessError, OSError):
            tmp.unlink(missing_ok=True)
            return None
    return out


def thumb_batch(cache, root, folder: str, budget: int = 6) -> dict:
    """One folder's thumbnails as data URIs, in a single response.

    Through `tailscale serve` on a busy Mac every request costs 0.2-2 s, so fifty thumbnail requests
    are slower than one that carries them all. At most `budget` missing thumbnails are made per call;
    the rest come back in `pending` and the client asks again. `failed` ones are never retried by the page.
    """
    d = Path(root) / "assets" / folder
    thumbs, pending, failed, made = {}, [], [], 0
    for f in list_folder(d):
        if f["kind"] not in ("video", "image"):
            continue
        src = d / f["name"]
        t = thumb_path(cache, src)
        if t is not None and not t.is_file():
            if made >= budget:
                pending.append(f["name"])
                continue
            made += 1
            t = thumb_for(cache, src)
        if t is None:
            failed.append(f["name"])
        else:
            thumbs[f["name"]] = "data:image/jpeg;base64," + base64.b64encode(t.read_bytes()).decode()
    return {"thumbs": thumbs, "pending": pending, "failed": failed}


STYLE = """
:root{--bg:#f6f6f4;--card:#fff;--line:#dcdcd6;--fg:#1c1c1a;--dim:#6b6b66;--acc:#1f5fbf;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1b1e26;--line:#2a2d36;--fg:#e8e8ea;--dim:#9a9aa3;--acc:#7fb0ff;--err:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;padding:16px;font:16px -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:900px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}.dim{color:var(--dim);font-size:14px}
section,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:14px 0;padding:12px}
h2{font-size:15px;margin:0 0 8px;display:flex;justify-content:space-between;gap:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.item{min-width:0}.thumb{width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:6px;background:var(--line);display:block}
.name{font-size:13px;word-break:break-all;margin-top:4px}
a{color:var(--acc)}
.play{position:relative;cursor:pointer}
.badge{position:absolute;inset:0;display:grid;place-items:center;font-size:28px;color:#fff;text-shadow:0 1px 6px #000}
input,textarea,button{font:inherit;width:100%;padding:10px;margin:4px 0;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
button{background:var(--acc);color:#fff;border:0;font-weight:600;cursor:pointer}button:disabled{opacity:.5}
.err{color:var(--err);font-size:14px;min-height:1.2em}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;justify-content:space-between}
.btns{display:flex;gap:8px;flex-wrap:wrap}.btns button{flex:1;min-width:140px}
button.alt{background:var(--card);color:var(--acc);border:1px solid var(--acc)}
.urow{margin:10px 0;font-size:14px}progress{width:100%;height:8px}
"""

HOME_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>umcares</title>
<style>__STYLE__</style></head><body><main>
<h1>umcares</h1><div class="dim" id="status">Loading…</div>
<div id="list"></div>
<form class="card" id="new">
  <h2>New project</h2>
  <input name="name" placeholder="Name, e.g. Hari Raya open house" required maxlength="100" autocomplete="off">
  <input name="event_date" type="date" aria-label="Event date">
  <textarea name="notes" rows="2" placeholder="Notes (optional)" maxlength="2000"></textarea>
  <div class="err" id="err"></div>
  <button type="submit">Create project</button>
  <div class="dim">Makes a folder with its own git repo, .gitignore and README on this machine.</div>
</form>
</main>
<script>
const BASE = __BASE__;
function el(tag, props, ...kids){ const e = document.createElement(tag); Object.assign(e, props || {}); kids.forEach(k => e.append(k)); return e; }
const status = document.getElementById("status");
fetch(BASE + "/api/projects").then(r => { if(!r.ok) throw new Error(r.status); return r.json(); }).then(d => {
  const list = document.getElementById("list");
  d.projects.forEach(p => {
    const meta = [p.event_date || "no date", p.status].concat(p.external ? ["existing folder"] : []).concat(p.missing ? ["folder missing"] : []).join(" · ");
    list.append(el("div", {className:"card"}, el("div", {className:"row"},
      p.missing ? el("strong", {}, p.name) : el("a", {href: BASE + "/p/" + encodeURIComponent(p.slug) + "/"}, el("strong", {}, p.name)),
      el("span", {className:"dim"}, meta)), p.notes ? el("div", {className:"dim"}, p.notes) : ""));
  });
  status.textContent = d.projects.length ? d.projects.length + " project" + (d.projects.length > 1 ? "s" : "") : "No projects yet";
}).catch(e => { status.textContent = "Could not load projects (" + e.message + ")"; });
document.getElementById("new").addEventListener("submit", async ev => {
  ev.preventDefault();
  const f = ev.target, err = document.getElementById("err"), btn = f.querySelector("button");
  err.textContent = ""; btn.disabled = true;
  try {
    const r = await fetch(BASE + "/api/projects", {method:"POST",
      headers:{"Content-Type":"application/json","X-Requested-With":"umcares"},
      body: JSON.stringify({name:f.name.value, event_date:f.event_date.value, notes:f.notes.value})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    location.href = BASE + "/p/" + encodeURIComponent(d.project.slug) + "/";
  } catch (e) { err.textContent = e.message; btn.disabled = false; }
});
</script></body></html>
"""

PROJECT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>umcares project</title>
<style>__STYLE__</style></head><body><main>
<div class="dim"><a id="back">All projects</a></div>
<h1 id="title"></h1><div class="dim" id="status">Loading…</div>
<section class="card" id="add">
  <h2><span>Add photos or videos</span><span class="dim" id="speed"></span></h2>
  <div class="btns">
    <button id="pick" type="button">Choose from library</button>
    <button id="shoot" type="button" class="alt">Take photo</button>
    <button id="film" type="button" class="alt">Record video</button>
  </div>
  <input id="f_pick" type="file" accept="image/*,video/*" multiple hidden>
  <input id="f_shoot" type="file" accept="image/*" capture="environment" hidden>
  <input id="f_film" type="file" accept="video/*" capture="environment" hidden>
  <div id="queue"></div>
  <div class="dim" id="summary"></div>
  <button id="retry" type="button" class="alt" hidden>Try the failed ones again</button>
</section>
<div id="lib"></div>
</main>
<script>
const HOME = __HOME__;
const BASE = __BASE__;
const PROJECT = __PROJECT__;
document.title = PROJECT.name + " · umcares";
document.getElementById("back").href = HOME + "/";
document.getElementById("title").textContent = PROJECT.name;
const mb = n => n < 1048576 ? Math.max(1, Math.round(n/1024)) + " KB" : (n/1048576).toFixed(1) + " MB";
const url = (folder, name) => BASE + "/media/" + folder.split("/").concat([name]).map(encodeURIComponent).join("/");
function el(tag, props, ...kids){ const e = document.createElement(tag); Object.assign(e, props || {}); kids.forEach(k => e.append(k)); return e; }
let tiles = {};
function thumb(f, folder){
  const src = url(folder, f.name);
  if (f.kind === "audio") return el("audio", {src, preload:"none", controls:true, style:"width:100%"});
  if (f.kind === "file") return el("a", {href:src}, "open");
  const img = el("img", {className:"thumb", alt:f.name});
  (tiles[folder] = tiles[folder] || {})[f.name] = img;
  if (f.kind === "image") return el("a", {href:src, target:"_blank"}, img);
  const tile = el("div", {className:"play"}, img, el("span", {className:"badge"}, "▶"));
  tile.onclick = () => tile.replaceWith(el("video", {className:"thumb", src, controls:true, autoplay:true, playsInline:true}));
  return tile;
}
// One request per folder, one folder at a time: the tailnet proxy is slow per request, not per byte.
async function fillThumbs(folders){
  for (const folder of folders){
    for (let round = 0; round < 40; round++){
      let d;
      try { const r = await fetch(BASE + "/api/thumbs?folder=" + encodeURIComponent(folder)); if (!r.ok) break; d = await r.json(); }
      catch (e) { break; }
      for (const [name, uri] of Object.entries(d.thumbs)) { const i = (tiles[folder] || {})[name]; if (i && !i.src) i.src = uri; }
      if (!d.pending.length) break;
    }
  }
}
function loadLibrary(){
  return fetch(BASE + "/api/library").then(r => { if(!r.ok) throw new Error(r.status); return r.json(); }).then(d => {
    const lib = document.getElementById("lib");
    lib.replaceChildren(); tiles = {};
    let n = 0, bytes = 0;
    d.folders.forEach(g => {
      n += g.files.length; g.files.forEach(f => bytes += f.size);
      const grid = el("div", {className:"grid"});
      g.files.forEach(f => grid.append(el("div", {className:"item"}, thumb(f, g.folder),
        el("div", {className:"name"}, f.name), el("div", {className:"dim"}, mb(f.size)))));
      if (!g.files.length) grid.append(el("div", {className:"dim"}, g.exists ? "empty" : "folder missing"));
      lib.append(el("section", {}, el("h2", {}, el("span", {}, g.label), el("span", {className:"dim"}, g.files.length + " files")), grid));
    });
    document.getElementById("status").textContent = n + " files · " + mb(bytes);
    fillThumbs(d.folders.filter(g => g.files.some(f => f.kind === "video" || f.kind === "image")).map(g => g.folder));
  }).catch(e => { document.getElementById("status").textContent = "Could not load the library (" + e.message + ")"; });
}
loadLibrary();

// ---- upload: resumable chunks, two files at a time, adaptive chunk size ----
const CSRF = {"X-Requested-With": "umcares"};
const JSON_HEADERS = Object.assign({"Content-Type": "application/json"}, CSRF);
const BIN_HEADERS = Object.assign({"Content-Type": "application/octet-stream"}, CSRF);
const MIN_CHUNK = 256 * 1024, MAX_CHUNK = 4 * 1024 * 1024, PARALLEL = 2;
const BACKOFF = [1000, 3000, 8000, 15000, 30000];
const sleep = ms => new Promise(r => setTimeout(r, ms));
class Fatal extends Error {}
let waiting = [], running = 0, sentTotal = 0, planTotal = 0, samples = [], failed = [], timer = null, wake = null;

async function call(method, path, body, headers){
  const r = await fetch(BASE + path, {method, headers, body});
  let d = {}; try { d = await r.json(); } catch (e) {}
  return {ok: r.ok, status: r.status, d};
}
function speedText(){
  const now = Date.now();
  samples = samples.filter(s => now - s[0] < 15000);
  if (!samples.length) return "";
  const bytes = samples.reduce((a, s) => a + s[1], 0);
  const bps = bytes / Math.max(1, Math.min(15, (now - samples[0][0]) / 1000));
  return bps >= 1048576 ? (bps / 1048576).toFixed(1) + " MB/s" : Math.round(bps / 1024) + " KB/s";
}
function updateOverall(){
  const sp = document.getElementById("speed");
  if (!running && !waiting.length) { sp.textContent = ""; return; }
  const pct = planTotal ? Math.floor(100 * sentTotal / planTotal) : 0;
  sp.textContent = pct + "% · " + mb(sentTotal) + " of " + mb(planTotal) + " · " + (speedText() || "starting");
}
function setMsg(item, text){ item.msg.textContent = text; }
function advance(item, n){ item.sent += n; sentTotal += n; item.bar.value = item.sent; samples.push([Date.now(), n]); updateOverall(); }
function resync(item, from, to){ const d = to - from; item.sent += d; sentTotal += d; item.bar.value = item.sent; }

async function uploadOne(item){
  const f = item.file;
  setMsg(item, "checking…");
  const s = await call("POST", "/api/upload/start", JSON.stringify({name: f.name, size: f.size, mtime: f.lastModified}), JSON_HEADERS);
  if (!s.ok) throw new Fatal((s.d && s.d.error) || ("HTTP " + s.status));
  if (s.d.status === "have") { item.sent = f.size; item.bar.value = f.size; sentTotal += f.size; return {state: "skipped", text: "already uploaded"}; }
  const id = s.d.id;
  let offset = s.d.offset, chunk = MIN_CHUNK, fails = 0;
  if (offset) { item.sent = offset; sentTotal += offset; item.bar.value = offset; }
  setMsg(item, offset ? "resuming…" : "uploading…");
  while (offset < f.size) {
    const end = Math.min(f.size, offset + chunk), t0 = performance.now();
    try {
      const r = await call("PUT", "/api/upload/" + id + "?offset=" + offset, f.slice(offset, end), BIN_HEADERS);
      if (r.ok) {
        advance(item, r.d.offset - offset); offset = r.d.offset; fails = 0; setMsg(item, "uploading…");
        const dt = (performance.now() - t0) / 1000;
        chunk = dt < 4 ? Math.min(MAX_CHUNK, chunk * 2) : dt > 15 ? Math.max(MIN_CHUNK, chunk >> 1) : chunk;
      } else if (r.status === 409 && typeof r.d.offset === "number") {
        resync(item, offset, r.d.offset); offset = r.d.offset;          // the server is ahead or behind: trust it
      } else if (r.status >= 400 && r.status < 500 && r.status !== 400 && r.status !== 408 && r.status !== 429) {
        throw new Fatal(r.d.error || ("HTTP " + r.status));
      } else {
        throw new Error(r.d.error || ("HTTP " + r.status));            // 400 here is a chunk cut short: resend it
      }
    } catch (e) {
      if (e instanceof Fatal) throw e;
      if (++fails > BACKOFF.length) throw new Error("the connection keeps failing");
      setMsg(item, "retrying…");
      await sleep(BACKOFF[fails - 1]);
      const st = await call("GET", "/api/upload/" + id).catch(() => null);
      if (st && st.ok && typeof st.d.offset === "number") { resync(item, offset, st.d.offset); offset = st.d.offset; }
    }
  }
  const fin = await call("POST", "/api/upload/" + id + "/finish", "{}", JSON_HEADERS);
  if (!fin.ok) throw new Fatal((fin.d && fin.d.error) || ("HTTP " + fin.status));
  return fin.d.status === "duplicate"
    ? {state: "skipped", text: "same content as " + fin.d.file.name}
    : {state: "done", text: "saved in " + fin.d.file.folder};
}
function addRow(item){
  item.bar = el("progress", {max: item.file.size || 1, value: 0});
  item.msg = el("span", {className: "dim"}, "waiting");
  item.row = el("div", {className: "urow"}, el("div", {className: "row"}, el("span", {className: "name"}, item.file.name), item.msg), item.bar);
  document.getElementById("queue").append(item.row);
}
async function runItem(item){
  try {
    const r = await uploadOne(item);
    item.state = r.state; setMsg(item, (r.state === "done" ? "✓ " : "↷ ") + r.text);
  } catch (e) {
    item.state = "failed"; setMsg(item, "✗ " + (e.message || e)); failed.push(item);
  }
  running--;
  pump();
}
function pump(){
  while (running < PARALLEL && waiting.length) { running++; runItem(waiting.shift()); }
  updateOverall();
  if (!running && !waiting.length) allDone();
}
async function lockScreen(){
  try { if (navigator.wakeLock && !wake) { wake = await navigator.wakeLock.request("screen"); wake.addEventListener("release", () => { wake = null; }); } } catch (e) {}
}
function allDone(){
  if (timer) { clearInterval(timer); timer = null; }
  try { if (wake) wake.release(); } catch (e) {} wake = null;
  const rows = Array.from(document.querySelectorAll("#queue .urow"));
  const text = rows.map(r => r.querySelector(".dim").textContent);
  const saved = text.filter(t => t.startsWith("✓")).length, skipped = text.filter(t => t.startsWith("↷")).length;
  document.getElementById("summary").textContent = saved + " saved" + (skipped ? ", " + skipped + " already there" : "") + (failed.length ? ", " + failed.length + " failed" : "");
  document.getElementById("retry").hidden = !failed.length;
  document.getElementById("speed").textContent = "";
  if (saved) loadLibrary();
}
function enqueue(files){
  if (!files.length) return;
  document.getElementById("summary").textContent = "";
  document.getElementById("retry").hidden = true;
  if (!running && !waiting.length) { sentTotal = 0; planTotal = 0; samples = []; failed = []; document.getElementById("queue").replaceChildren(); }
  for (const f of files) { const item = {file: f, sent: 0, state: "waiting"}; addRow(item); waiting.push(item); planTotal += f.size; }
  lockScreen();
  if (!timer) timer = setInterval(updateOverall, 1000);
  pump();
}
for (const k of ["pick", "shoot", "film"]) {
  const input = document.getElementById("f_" + k);
  document.getElementById(k).addEventListener("click", () => input.click());
  input.addEventListener("change", () => { enqueue(Array.from(input.files)); input.value = ""; });
}
document.getElementById("retry").addEventListener("click", () => {
  const again = failed.map(i => i.file); failed = [];
  document.querySelectorAll("#queue .urow").forEach(r => { if (r.querySelector(".dim").textContent.startsWith("✗")) r.remove(); });
  planTotal = sentTotal; enqueue(again);
});
window.addEventListener("beforeunload", e => { if (running || waiting.length) { e.preventDefault(); e.returnValue = ""; } });
</script></body></html>
"""


def js(value) -> str:
    """A value as a JS literal that cannot close the surrounding <script>."""
    return json.dumps(value).replace("</", "<\\/")


def make_handler(home, owner: str):
    class H(BaseHTTPRequestHandler):
        server_version = "umcares-web"
        timeout = 300            # a connection that goes silent for 5 minutes is dropped, not held (an upload is chunked, so nothing is lost)

        def log_message(self, fmt, *args):       # one quiet line per request, on stderr
            sys.stderr.write("web: " + (fmt % args) + "\n")

        def _send(self, code, body: bytes, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, code, msg):
            self._send(code, (msg + "\n").encode(), "text/plain; charset=utf-8")

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _page(self, template: str, **tokens):
            page = template.replace("__STYLE__", STYLE)
            for k, v in tokens.items():
                page = page.replace(f"__{k.upper()}__", js(v))
            self._send(200, page.encode(), "text/html; charset=utf-8")

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

        def _project_get(self, slug: str, rest: str, query: dict):
            root = project_root(home, slug)
            if root is None:
                return self._text(404, "not found")
            base = f"{BASE}/p/{slug}"
            if rest.startswith("/api/upload/"):
                return self._upload(slug, rest, "GET", query)
            if rest == "/":
                p = projects.find_project(home, slug)
                return self._page(PROJECT_PAGE, home=BASE, base=base, project=public(p))
            if rest == "/api/library":
                return self._json(200, list_library(root))
            if rest == "/api/thumbs":
                folder = (query.get("folder") or [""])[0]
                if folder not in FOLDER_NAMES:
                    return self._text(404, "not found")
                try:
                    budget = max(1, min(8, int((query.get("budget") or ["6"])[0])))
                except ValueError:
                    budget = 6
                return self._json(200, thumb_batch(thumb_cache(home), root, folder, budget))
            if rest.startswith("/thumb/"):
                src = resolve_media(root, rest[len("/thumb/"):])
                t = thumb_for(thumb_cache(home), src) if src else None
                if not t:
                    return self._text(404, "no thumbnail")
                return self._send(200, t.read_bytes(), "image/jpeg", {"Cache-Control": "private, max-age=86400"})
            if rest.startswith("/media/"):
                p = resolve_media(root, rest[len("/media/"):])
                return self._media(p) if p else self._text(404, "not found")
            return self._text(404, "not found")

        def do_GET(self):
            if not authorized(self.headers, owner):
                return self._text(403, "forbidden")
            parts = urlsplit(self.path)
            path, query = unquote(parts.path), parse_qs(parts.query)
            if path in ("/", "/index.html"):
                return self._page(HOME_PAGE, base=BASE)
            if path == "/api/projects":
                return self._json(200, {"projects": [public(p) for p in projects.list_projects(home)]})
            m = re.fullmatch(r"/p/([a-z0-9-]+)(/.*)?", path)
            if m:
                return self._project_get(m.group(1), m.group(2) or "/", query)
            return self._text(404, "not found")

        do_HEAD = do_GET

        def _csrf_ok(self, json_body: bool) -> bool:
            """tailscale adds the owner's login to any request from the owner's browser, so a write must also
            carry a header a cross-site page cannot send (it would need a CORS preflight we never answer)."""
            if self.headers.get(CSRF_HEADER[0]) != CSRF_HEADER[1]:
                return False
            if json_body:
                return (self.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json"
            return True

        def _json_body(self):
            """The request's JSON object, or None after answering 400 (empty, oversized or malformed)."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if not 0 < n <= MAX_BODY:
                self._json(400, {"error": "empty or oversized request"})
                return None
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._json(400, {"error": "expected a JSON object"})
                return None
            return body

        def _create_project(self):
            body = self._json_body()
            if body is None:
                return
            name, date, notes = body.get("name"), body.get("event_date") or "", body.get("notes") or ""
            if not all(isinstance(x, str) for x in (name, date, notes)) or len(name) > 100 or len(notes) > 2000:
                return self._json(400, {"error": "name must be text of at most 100 characters"})
            try:
                made = projects.create_project(home, name, date, notes)
            except projects.ProjectError as e:
                return self._json(400, {"error": str(e)})
            except OSError:                      # two creates of one name raced; the loser's rename fails
                return self._json(409, {"error": "that project already exists"})
            return self._json(201, {"project": public({**made, "external": False, "missing": False}),
                                    "git": made["git"]})

        def _upload(self, slug: str, rest: str, method: str, query: dict):
            root = project_root(home, slug)
            if root is None:
                return self._text(404, "not found")
            m = re.fullmatch(r"/api/upload/(?:(start)|([0-9a-f]{16})(/finish)?)", rest)
            if not m:
                return self._text(404, "not found")
            if method != "GET" and not self._csrf_ok(json_body=(method == "POST")):
                return self._json(403, {"error": "forbidden"})
            try:
                if m.group(1):                                        # start
                    if method != "POST":
                        return self._text(405, "use POST")
                    body = self._json_body()
                    if body is None:
                        return
                    return self._json(200, uploads.start(root, body.get("name"), body.get("size"), body.get("mtime")))
                uid, finishing = m.group(2), bool(m.group(3))
                if method == "GET" and not finishing:
                    return self._json(200, uploads.status(root, uid))
                if method == "PUT" and not finishing:
                    try:
                        offset = int((query.get("offset") or [""])[0])
                        length = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        return self._json(400, {"error": "offset and length must be numbers"})
                    return self._json(200, uploads.append(root, uid, offset, self.rfile, length))
                if method == "POST" and finishing:
                    return self._json(200, uploads.finish(root, uid))
                return self._text(405, "wrong method for this address")
            except uploads.UploadError as e:
                return self._json(e.status, {"error": e.message, **e.extra})
            except OSError:
                return self._json(500, {"error": "the machine could not write the file"})

        def _write(self, method: str):
            if not authorized(self.headers, owner):
                return self._text(403, "forbidden")
            parts = urlsplit(self.path)
            path, query = unquote(parts.path), parse_qs(parts.query)
            if method == "POST" and path == "/api/projects":
                if not self._csrf_ok(json_body=True):
                    return self._json(403, {"error": "forbidden"})
                return self._create_project()
            m = re.fullmatch(r"/p/([a-z0-9-]+)(/api/upload/.*)", path)
            if m:
                return self._upload(m.group(1), m.group(2), method, query)
            return self._text(405, "read-only")

        def do_POST(self):
            self._write("POST")

        def do_PUT(self):
            self._write("PUT")

        def _refuse_write(self):
            self._text(405, "read-only")

        do_DELETE = do_PATCH = _refuse_write

    return H


def make_server(home, owner: str = "", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(home, owner))


def keep_awake():
    """Hold the Mac awake while this runs. Its idle sleep is 1 minute, and a sleeping Mac drops every upload."""
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except FileNotFoundError:                    # not macOS
        return None


def serve_command(port: int) -> list:
    return ["tailscale", "serve", "--bg", "--set-path", BASE, f"http://127.0.0.1:{port}"]


def run(home, owner: str = "", port: int = DEFAULT_PORT, awake: bool = True) -> int:
    if not owner:
        sys.stderr.write("web: no owner set (UMC_WEB_OWNER / --owner): tailnet requests will be refused, "
                         "only this machine can reach the page\n")
    Path(home).mkdir(parents=True, exist_ok=True)
    srv = make_server(home, owner, port)
    if awake:
        keep_awake()
    sys.stderr.write(f"web: projects in {home} on http://127.0.0.1:{port}\n")
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
    ap.add_argument("--home", default=os.environ.get("UMC_PROJECTS_HOME", str(projects.DEFAULT_HOME)),
                    help="folder that holds the projects")
    ap.add_argument("--owner", default=os.environ.get("UMC_WEB_OWNER", ""))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-caffeinate", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.home, a.owner, a.port, not a.no_caffeinate))
