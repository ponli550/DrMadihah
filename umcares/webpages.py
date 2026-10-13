"""Browser-side code the web app serves: the shared uploader, the service worker and the share page.

Kept out of web.py so the server stays readable. Plain strings; web.py fills in the few placeholders.
"""
from __future__ import annotations

# The upload client, used by the project page and by the share page, so both behave identically.
# Resumable chunks (the server's partial-file length is the single truth), two files at a time,
# chunk size adapting to the link: doubles under 4 s, halves over 15 s, between 256 KB and 4 MB.
UPLOADER_JS = r"""
function createUploader(opts) {
  const BASE = opts.base, queueEl = opts.queue, summaryEl = opts.summary, speedEl = opts.speed, retryEl = opts.retry;
  const CSRF = {"X-Requested-With": "umcares"};
  const JSON_HEADERS = Object.assign({"Content-Type": "application/json"}, CSRF);
  const BIN_HEADERS = Object.assign({"Content-Type": "application/octet-stream"}, CSRF);
  const MIN_CHUNK = 256 * 1024, MAX_CHUNK = 4 * 1024 * 1024, PARALLEL = 2;
  const BACKOFF = [1000, 3000, 8000, 15000, 30000];
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  class Fatal extends Error {}
  let waiting = [], running = 0, sentTotal = 0, planTotal = 0, samples = [], failed = [], timer = null, wake = null;

  const mb = n => n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB";
  function el(tag, props, ...kids) {
    const e = document.createElement(tag); Object.assign(e, props || {}); kids.forEach(k => e.append(k)); return e;
  }
  async function call(method, path, body, headers) {
    const r = await fetch(BASE + path, {method, headers, body});
    let d = {}; try { d = await r.json(); } catch (e) {}
    return {ok: r.ok, status: r.status, d};
  }
  function speedText() {
    const now = Date.now();
    samples = samples.filter(s => now - s[0] < 15000);
    if (!samples.length) return "";
    const bytes = samples.reduce((a, s) => a + s[1], 0);
    const bps = bytes / Math.max(1, Math.min(15, (now - samples[0][0]) / 1000));
    return bps >= 1048576 ? (bps / 1048576).toFixed(1) + " MB/s" : Math.round(bps / 1024) + " KB/s";
  }
  function updateOverall() {
    if (!running && !waiting.length) { speedEl.textContent = ""; return; }
    const pct = planTotal ? Math.floor(100 * sentTotal / planTotal) : 0;
    speedEl.textContent = pct + "% · " + mb(sentTotal) + " of " + mb(planTotal) + " · " + (speedText() || "starting");
  }
  const setMsg = (item, text) => { item.msg.textContent = text; };
  function advance(item, n) { item.sent += n; sentTotal += n; item.bar.value = item.sent; samples.push([Date.now(), n]); updateOverall(); }
  function resync(item, from, to) { const d = to - from; item.sent += d; sentTotal += d; item.bar.value = item.sent; }

  async function uploadOne(item) {
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
  function addRow(item) {
    item.bar = el("progress", {max: item.file.size || 1, value: 0});
    item.msg = el("span", {className: "dim"}, "waiting");
    item.row = el("div", {className: "urow"}, el("div", {className: "row"}, el("span", {className: "name"}, item.file.name), item.msg), item.bar);
    queueEl.append(item.row);
  }
  async function runItem(item) {
    try {
      const r = await uploadOne(item);
      item.state = r.state; setMsg(item, (r.state === "done" ? "✓ " : "↷ ") + r.text);
    } catch (e) {
      item.state = "failed"; setMsg(item, "✗ " + (e.message || e)); failed.push(item);
    }
    running--;
    pump();
  }
  function pump() {
    while (running < PARALLEL && waiting.length) { running++; runItem(waiting.shift()); }
    updateOverall();
    if (!running && !waiting.length) allDone();
  }
  async function lockScreen() {
    try { if (navigator.wakeLock && !wake) { wake = await navigator.wakeLock.request("screen"); wake.addEventListener("release", () => { wake = null; }); } } catch (e) {}
  }
  function allDone() {
    if (timer) { clearInterval(timer); timer = null; }
    try { if (wake) wake.release(); } catch (e) {} wake = null;
    const texts = Array.from(queueEl.children).filter(r => r.className === "urow").map(r => r.children[0].children[1].textContent);
    const saved = texts.filter(t => t.startsWith("✓")).length, skipped = texts.filter(t => t.startsWith("↷")).length;
    summaryEl.textContent = saved + " saved" + (skipped ? ", " + skipped + " already there" : "") + (failed.length ? ", " + failed.length + " failed" : "");
    retryEl.hidden = !failed.length;
    speedEl.textContent = "";
    if (saved && opts.onSaved) opts.onSaved();
    if (opts.onDone) opts.onDone({saved, skipped, failed: failed.length});
  }
  function enqueue(files) {
    if (!files.length) return;
    summaryEl.textContent = "";
    retryEl.hidden = true;
    if (!running && !waiting.length) { sentTotal = 0; planTotal = 0; samples = []; failed = []; queueEl.replaceChildren(); }
    for (const f of files) { const item = {file: f, sent: 0, state: "waiting"}; addRow(item); waiting.push(item); planTotal += f.size; }
    lockScreen();
    if (!timer) timer = setInterval(updateOverall, 1000);
    pump();
  }
  retryEl.addEventListener("click", () => {
    const again = failed.map(i => i.file); failed = [];
    Array.from(queueEl.children).forEach(r => { if (r.className === "urow" && r.children[0].children[1].textContent.startsWith("✗")) r.remove(); });
    planTotal = sentTotal; enqueue(again);
  });
  window.addEventListener("beforeunload", e => { if (running || waiting.length) { e.preventDefault(); e.returnValue = ""; } });
  return {enqueue, busy: () => running > 0 || waiting.length > 0};
}
"""


# The service worker does two things. It makes the page installable (a fetch listener is what Chrome checks),
# and it catches a share from the phone's share sheet. Android POSTs the shared files to the share-target URL;
# the worker takes them locally, parks them in IndexedDB and redirects to the share page. The files therefore
# never travel in one big request over a link that can stall for a minute: the share page uploads them with the
# resumable uploader. Every other request is left alone: nothing is cached, nothing is rewritten.
SW = r"""
const SCOPE = new URL('./', self.location).pathname;            // "/umcares/"
const DB = 'umcares-share', STORE = 'shares', KEEP_MS = 7 * 86400 * 1000;
function openDb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE, {keyPath: 'id'});
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}
function finished(tx) {
  return new Promise((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onerror = tx.onabort = () => reject(tx.error); });
}
async function stash(request) {
  try {
    const form = await request.formData();
    const files = form.getAll('media').filter(f => f && typeof f === 'object' && 'size' in f && f.size > 0);
    if (!files.length) return Response.redirect(SCOPE + 'share?empty=1', 303);
    const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    store.put({id, at: Date.now(), files});
    const old = store.getAll();                                   // forget shares nobody finished within a week
    old.onsuccess = () => old.result.forEach(r => { if (Date.now() - r.at > KEEP_MS) store.delete(r.id); });
    await finished(tx);
    db.close();
    return Response.redirect(SCOPE + 'share?id=' + id, 303);
  } catch (e) {
    return Response.redirect(SCOPE + 'share?error=1', 303);
  }
}
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const r = event.request;
  if (r.method === 'POST' && new URL(r.url).pathname === SCOPE + 'share-target') event.respondWith(stash(r));
});
"""

# Where a share lands: choose the project, then upload. The files wait in IndexedDB until every one is saved,
# so closing the page, losing signal or a failed file never loses them; reopening the same address resumes.
SHARE_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
__PWA__
<title>Share to umcares</title>
<style>__STYLE__</style></head><body><main>
<h1>Share to umcares</h1>
<div class="dim" id="status">Loading…</div>
<section class="card" id="files" hidden>
  <h2><span>Shared files</span><span class="dim" id="count"></span></h2>
  <div class="grid" id="previews"></div>
</section>
<section class="card" id="pickcard" hidden>
  <h2>Which project?</h2>
  <div id="projects"></div>
  <button id="go" type="button">Upload</button>
  <button id="discard" type="button" class="alt">Discard these files</button>
</section>
<section class="card" id="uploadcard" hidden>
  <h2><span>Uploading</span><span class="dim" id="speed"></span></h2>
  <div id="queue"></div>
  <div class="dim" id="summary"></div>
  <button id="retry" type="button" class="alt" hidden>Try the failed ones again</button>
  <div id="after"></div>
</section>
<div class="dim"><a id="home">All projects</a></div>
</main>
<script src="__ROOT__/uploader.js"></script>
<script>
const ROOT = __BASE__;
const DB = "umcares-share", STORE = "shares";
document.getElementById("home").href = ROOT + "/";
const $ = id => document.getElementById(id);
const mb = n => n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB";
function el(tag, props, ...kids) { const e = document.createElement(tag); Object.assign(e, props || {}); kids.forEach(k => e.append(k)); return e; }
function openDb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE, {keyPath: "id"});
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}
async function getShare(id) {
  const db = await openDb();
  return new Promise(resolve => {
    const r = db.transaction(STORE).objectStore(STORE).get(id);
    r.onsuccess = () => { db.close(); resolve(r.result || null); };
    r.onerror = () => { db.close(); resolve(null); };
  });
}
async function dropShare(id) {
  const db = await openDb();
  return new Promise(resolve => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = tx.onabort = () => { db.close(); resolve(); };
  });
}
const say = text => { $("status").textContent = text; };
(async () => {
  const params = new URLSearchParams(location.search), id = params.get("id");
  if (params.get("error")) return say("The files could not be saved on this phone (storage full, or blocked). Open a project and use “Choose from library” instead.");
  if (params.get("empty")) return say("Nothing that can be uploaded came with that share. Only photos and videos are accepted.");
  if (!id) return say("To use this, share photos or videos to umcares from your gallery or camera.");
  let share = null;
  try { share = await getShare(id); } catch (e) {}
  if (!share || !share.files || !share.files.length) return say("These files were already uploaded or have expired. Share them again.");
  const files = share.files;
  say("");
  $("files").hidden = false; $("pickcard").hidden = false;
  $("count").textContent = files.length + (files.length === 1 ? " file" : " files") + " · " + mb(files.reduce((a, f) => a + f.size, 0));
  for (const f of files.slice(0, 12)) {
    const tile = el("div", {className: "item"});
    if ((f.type || "").startsWith("image/")) tile.append(el("img", {className: "thumb", src: URL.createObjectURL(f), alt: f.name}));
    tile.append(el("div", {className: "name"}, f.name), el("div", {className: "dim"}, mb(f.size)));
    $("previews").append(tile);
  }
  let list = [];
  try { const r = await fetch(ROOT + "/api/projects"); list = (await r.json()).projects.filter(p => !p.missing); } catch (e) {}
  if (!list.length) { say("No projects yet. Create one first, then share again."); $("pickcard").hidden = true; return; }
  let last = null; try { last = localStorage.getItem("umcares.lastProject"); } catch (e) {}
  list.forEach((p, i) => {
    const radio = el("input", {type: "radio", name: "project", value: p.slug, id: "p_" + p.slug});
    radio.checked = p.slug === last || (!last && i === 0);
    $("projects").append(el("label", {className: "row", style: "padding:6px 0"}, el("span", {}, radio, " " + p.name), el("span", {className: "dim"}, p.event_date || "")));
  });
  $("discard").addEventListener("click", async () => {
    await dropShare(id); $("files").hidden = true; $("pickcard").hidden = true; say("Discarded.");
  });
  $("go").addEventListener("click", () => {
    const chosen = document.querySelector("input[name=project]:checked");
    if (!chosen) return;
    const slug = chosen.value, name = list.find(p => p.slug === slug).name;
    try { localStorage.setItem("umcares.lastProject", slug); } catch (e) {}
    $("pickcard").hidden = true; $("uploadcard").hidden = false;
    say("Uploading to " + name);
    const up = createUploader({
      base: ROOT + "/p/" + encodeURIComponent(slug),
      queue: $("queue"), summary: $("summary"), speed: $("speed"), retry: $("retry"),
      onDone: async r => {
        if (r.failed) { say(r.failed + " file(s) did not upload. They are kept: try again, or reopen this page later."); return; }
        await dropShare(id);                                         // only once every file is safely stored
        say("Done: " + name);
        $("after").append(el("a", {href: ROOT + "/p/" + encodeURIComponent(slug) + "/?details=1"}, "Open " + name + " and add details"));
      },
    });
    up.enqueue(files);
  });
})();
</script></body></html>
"""


# The caption pass: one file at a time, with a preview, a caption box and four ways out. A file ends in one of
# three states: captioned, skipped ("no detail", never asked again) or still pending.
DETAILS_JS = r"""
function createDetails(opts) {
  const BASE = opts.base, mount = opts.mount, banner = opts.banner;
  const HEADERS = {"Content-Type": "application/json", "X-Requested-With": "umcares"};
  let list = [], idx = 0, saved = 0, skipped = 0, busy = false;

  const mb = n => n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB";
  function el(tag, props, ...kids) {
    const e = document.createElement(tag); Object.assign(e, props || {}); kids.forEach(k => e.append(k)); return e;
  }
  const seg = (folder, name) => folder.split("/").concat([name]).map(encodeURIComponent).join("/");

  async function fetchItems() {
    const r = await fetch(BASE + "/api/media");
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  async function refresh() {
    try {
      const d = await fetchItems();
      banner.hidden = d.pending === 0;
      banner.textContent = "Add details (" + d.pending + ")";
    } catch (e) { /* the banner just stays as it was */ }
  }
  function message(title, text, buttons) {
    mount.replaceChildren(el("section", {className: "card"}, el("h2", {}, el("span", {}, title)), el("div", {className: "dim"}, text), ...buttons));
  }
  function close() { mount.replaceChildren(); refresh(); if (opts.onChange) opts.onChange(); }
  function finish() {
    message("All done", saved + " captioned, " + skipped + " skipped.", [el("button", {type: "button", onclick: close}, "Close")]);
    refresh(); if (opts.onChange) opts.onChange();
  }
  async function start(opt) {
    opt = opt || {};
    let d;
    try { d = await fetchItems(); }
    catch (e) { message("Add details", "Could not load the files (" + e.message + ").", [el("button", {type: "button", className: "alt", onclick: close}, "Close")]); return; }
    list = d.items.filter(i => opt.only ? opt.only.includes(i.sha) : i.pending);
    if (!list.length) { message("Add details", "Nothing is waiting for details.", [el("button", {type: "button", className: "alt", onclick: close}, "Close")]); return; }
    idx = 0; saved = 0; skipped = 0; render();
  }
  async function save(it, body, err, buttons) {
    if (busy) return false;
    busy = true; buttons.forEach(b => { b.disabled = true; }); err.textContent = "";
    try {
      const r = await fetch(BASE + "/api/media/" + it.sha, {method: "POST", headers: HEADERS, body: JSON.stringify(body)});
      let d = {}; try { d = await r.json(); } catch (e) {}
      if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
      return true;
    } catch (e) {
      err.textContent = "Not saved: " + (e.message || e) + ". Try again.";
      return false;
    } finally { busy = false; buttons.forEach(b => { b.disabled = false; }); }
  }
  function render() {
    const it = list[idx], last = idx + 1 >= list.length;
    const poster = BASE + "/thumb/" + seg(it.folder, it.name), full = BASE + "/media/" + seg(it.folder, it.name);
    const img = el("img", {className: "thumb", alt: it.name, src: poster});
    img.onerror = () => { img.onerror = null; img.removeAttribute("src"); };
    const preview = el("a", {href: full, target: "_blank"}, img);
    const area = el("textarea", {rows: 3, maxLength: 500, value: it.caption || "", placeholder: "Who or what is this? e.g. Dr Madihah opening the session"});
    const err = el("div", {className: "err"});
    const next = el("button", {type: "button"}, last ? "Save & finish" : "Save & next");
    const skip = el("button", {type: "button", className: "alt"}, it.caption ? "Remove caption" : "Skip (no detail)");
    const back = el("button", {type: "button", className: "alt"}, "Back");
    const stop = el("button", {type: "button", className: "alt"}, "Stop for now");
    const all = [next, skip, back, stop];
    const advance = () => { if (last) finish(); else { idx++; render(); } };
    next.onclick = async () => {
      if (!area.value.trim()) { err.textContent = "Type a caption, or tap " + skip.textContent + "."; return; }
      if (await save(it, {caption: area.value}, err, all)) { it.caption = area.value.trim(); saved++; advance(); }
    };
    skip.onclick = async () => { if (await save(it, {skipped: true}, err, all)) { it.caption = ""; skipped++; advance(); } };
    back.onclick = () => { if (idx > 0) { idx--; render(); } };
    stop.onclick = close;
    back.hidden = idx === 0;
    mount.replaceChildren(el("section", {className: "card"},
      el("h2", {}, el("span", {}, "Add details"), el("span", {className: "dim"}, (idx + 1) + " of " + list.length)),
      el("progress", {max: list.length, value: idx}), preview,
      el("div", {className: "name"}, it.name), el("div", {className: "dim"}, it.kind + " · " + mb(it.size)),
      area, err, next, skip, back, stop));
  }
  banner.addEventListener("click", () => start({}));
  return {start, refresh};
}
"""


# The prepare card: lists the videos that can be made edit-ready, says in words what each one needs, runs them
# one at a time on the machine and follows the progress. It only appears when the project has such videos.
PREPARE_JS = r"""
function createPrepare(opts) {
  const BASE = opts.base, mount = opts.mount;
  const HEADERS = {"Content-Type": "application/json", "X-Requested-With": "umcares"};
  let timer = null, wasActive = false;
  const mb = n => n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB";
  function el(tag, props, ...kids) {
    const e = document.createElement(tag); Object.assign(e, props || {}); kids.forEach(k => e.append(k)); return e;
  }
  async function call(method, path, body) {
    const r = await fetch(BASE + path, {method, headers: method === "GET" ? undefined : HEADERS, body});
    let d = {}; try { d = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  const WORDS = {copy: "ready as it is", remux: "needs a light fix", reencode: "needs re-encoding"};
  function describe(c) {
    if (c.state === "unreadable") return "✗ " + c.error;
    if (c.state === "prepared") return "✓ prepared";
    if (!c.plan) return "checking…";
    const why = c.plan.reasons.length ? ": " + c.plan.reasons.join(", ") : "";
    return WORDS[c.plan.action] + why;
  }
  function row(c, job) {
    let text = describe(c), bar = null;
    if (job && (job.state === "queued" || job.state === "running")) {
      text = job.state === "queued" ? "waiting" : "preparing " + job.percent + "%";
      bar = el("progress", {max: 100, value: job.percent});
    } else if (job && job.state === "failed") { text = "✗ " + job.error; }
    else if (job && job.state === "cancelled") { text = "cancelled"; }
    const kids = [el("div", {className: "row"}, el("span", {className: "name"}, c.name), el("span", {className: "dim"}, text))];
    if (bar) kids.push(bar);
    const warn = (job && job.warnings || (c.plan && c.plan.warnings) || []).filter(Boolean);
    warn.forEach(w => kids.push(el("div", {className: "dim"}, "⚠ " + w)));
    return el("div", {className: "urow"}, ...kids);
  }
  function render(d) {
    if (!d.candidates.length) { mount.replaceChildren(); return; }
    const latest = {};
    d.jobs.forEach(j => { latest[j.sha] = j; });
    const todo = d.candidates.filter(c => c.state === "todo" && !(latest[c.sha] && ["queued", "running"].includes(latest[c.sha].state)));
    const prepared = d.candidates.filter(c => c.state === "prepared").length;
    const rows = d.candidates.map(c => row(c, latest[c.sha]));
    const buttons = [];
    if (todo.length && !d.active) {
      const go = el("button", {type: "button"}, "Prepare " + todo.length + (todo.length === 1 ? " video" : " videos"));
      go.addEventListener("click", async () => { go.disabled = true; try { await call("POST", "/api/prepare", JSON.stringify({all: true})); } catch (e) { alert(e.message); } refresh(); });
      buttons.push(go);
    }
    if (d.active) {
      const stop = el("button", {type: "button", className: "alt"}, "Cancel");
      stop.addEventListener("click", async () => { stop.disabled = true; try { await call("POST", "/api/prepare/cancel", "{}"); } catch (e) {} refresh(); });
      buttons.push(stop);
    }
    mount.replaceChildren(el("section", {className: "card"},
      el("h2", {}, el("span", {}, "Prepare videos for editing"), el("span", {className: "dim"}, prepared + " of " + d.candidates.length + " ready")),
      el("div", {className: "dim"}, "Makes edit-ready copies in “Edit ready”: constant frame rate, upright, standard colour. The originals stay. One at a time, at low priority, so Premiere is not slowed much."),
      ...rows, ...buttons));
  }
  function schedule(ms) { if (!timer) timer = setTimeout(() => { timer = null; refresh(); }, ms); }
  async function refresh() {
    try {
      const d = await call("GET", "/api/prepare");
      render(d);
      const checking = d.candidates.some(c => c.state === "todo" && !c.plan);
      if (d.active) schedule(1500); else if (checking) schedule(800);
      if (wasActive && !d.active && opts.onChange) opts.onChange();
      wasActive = d.active;
    } catch (e) { if (wasActive) schedule(5000); }
  }
  return {refresh};
}
"""


# The Make-a-video card. It builds a montage recipe from what the person picks and sends it to the server, which
# validates it and works out every timing. Nothing here computes a duration.
MONTAGE_JS = r"""
function createMontage(opts) {
  const BASE = opts.base, mount = opts.mount;
  const HEADERS = {"Content-Type": "application/json", "X-Requested-With": "umcares"};
  let st = null, order = [], picked = new Set(), rows = [], ctl = null, timer = null;
  let form = {format: "reels", template: "slowmo", title: "", captions: true, music: "", max: "30"};
  let jobsBox = null, planBox = null, noticeBox = null, listBox = null, goBtn = null, draftBtn = null, runBtns = [];

  const mb = n => n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB";
  const secs = n => (Math.round(n * 10) / 10) + " s";
  function el(tag, props, ...kids) {
    const e = document.createElement(tag); Object.assign(e, props || {}); kids.forEach(k => e.append(k)); return e;
  }
  async function call(method, path, body) {
    const r = await fetch(BASE + path, {method, headers: method === "GET" ? undefined : HEADERS, body});
    let d = {}; try { d = await r.json(); } catch (e) {}
    if (!r.ok) { const err = new Error(d.error || ("HTTP " + r.status)); err.problems = d.problems || []; throw err; }
    return d;
  }
  async function load() {
    st = await call("GET", "/api/montage");
    const refs = st.catalog.filter(i => i.kind !== "audio").map(i => i.ref);
    const known = new Set(order);
    order = order.filter(r => refs.includes(r)).concat(refs.filter(r => !known.has(r)));
    refs.forEach(r => { if (!known.has(r)) picked.add(r); });
    Array.from(picked).forEach(r => { if (!refs.includes(r)) picked.delete(r); });
  }
  function syncForm() {
    if (!ctl) return;
    form = {format: ctl.format.value, template: ctl.template.value, title: ctl.title.value, captions: !!ctl.captions.checked, music: ctl.music.value, max: ctl.max.value};
    rows.forEach(r => { if (r.check.checked) picked.add(r.ref); else picked.delete(r.ref); });
  }
  function buildRecipe() {
    syncForm();
    const rec = {kind: "montage", title: form.title.trim() || opts.name || "Montage", format: form.format, template: form.template,
                 captions: form.captions, max_seconds: Number(form.max), clips: order.filter(r => picked.has(r)).map(r => ({media: r}))};
    if (form.title.trim()) rec.title_card = {text: form.title.trim()};
    if (form.music) rec.music = {file: form.music};
    return rec;
  }
  function useRecipe(rec) {
    const byRef = m => st.catalog.find(i => i.ref === m || i.name === m || (m.length >= 8 && i.ref.startsWith(m.slice(0, 12))));
    const used = (rec.clips || []).map(c => byRef(c.media)).filter(Boolean).map(i => i.ref);
    order = used.concat(order.filter(r => !used.includes(r)));
    picked = new Set(used);
    form = {format: rec.format || "reels", template: rec.template || "slowmo", title: rec.title_card ? rec.title_card.text : "",
            captions: rec.captions !== false, music: rec.music ? rec.music.file : "", max: String(rec.max_seconds || 30)};
    render();
    say("Recipe loaded. Change anything, then Preview or Make video.");
  }
  function say(text, problems, isError) {
    if (!noticeBox) return;
    noticeBox.className = "notice " + (isError ? "err" : "ok");       // only a failure is red
    noticeBox.replaceChildren(text);
    (problems || []).forEach(p => noticeBox.append(el("div", {className: "dim"}, "• " + p)));
  }
  function move(ref, by) {
    syncForm();
    const i = order.indexOf(ref), j = i + by;
    if (i < 0 || j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    renderList();
  }
  function renderList() {
    rows = [];
    const byRef = {}; st.catalog.forEach(i => { byRef[i.ref] = i; });
    listBox.replaceChildren(...order.map((ref, idx) => {
      const i = byRef[ref];
      const check = el("input", {type: "checkbox", checked: picked.has(ref)});
      rows.push({ref, check});
      const up = el("button", {type: "button", className: "mv", title: "Move earlier"}, "↑");
      const down = el("button", {type: "button", className: "mv", title: "Move later"}, "↓");
      up.addEventListener("click", () => move(ref, -1));
      down.addEventListener("click", () => move(ref, 1));
      return el("div", {className: "urow"}, el("div", {className: "row"},
        el("span", {}, check, " " + (i.kind === "video" ? "🎞 " : "🖼 ") + (i.caption || i.name)),
        el("span", {}, up, down)),
        i.caption ? el("div", {className: "dim"}, i.name) : "");
    }));
  }
  async function preview() {
    try {
      const d = await call("POST", "/api/montage", JSON.stringify({recipe: buildRecipe(), dry_run: true}));
      const p = d.plan;
      planBox.replaceChildren(el("div", {}, p.segments.length + " pieces · " + secs(p.total) + " · " + (p.fade ? "crossfades" : "hard cuts")),
        ...p.warnings.map(w => el("div", {className: "dim"}, "⚠ " + w)),
        ...p.segments.map(s => el("div", {className: "dim"}, secs(s.start) + "  " + (s.kind === "title" || s.kind === "end" ? s.kind + " card: " + s.text : s.name + (s.text ? " — " + s.text : "")) + "  (" + secs(s.duration) + ")")));
      say("");
    } catch (e) { planBox.replaceChildren(); say("Cannot make this yet: " + e.message, e.problems, true); }
  }
  async function make(rec) {
    if (st && st.active) { say("A video is already rendering. It must finish (or be cancelled) first."); return; }
    try {
      await call("POST", "/api/montage", JSON.stringify({recipe: rec || buildRecipe()}));
      say("Rendering. This runs on the Premiere machine at low priority, one video at a time.");
      planBox.replaceChildren();
      poll();
    } catch (e) { say("Cannot make this yet: " + e.message, e.problems, true); }
  }
  async function save() {
    const name = prompt("Name this recipe (letters, digits, spaces, - and _)");
    if (!name) return;
    try {
      const d = await call("POST", "/api/montage/recipes", JSON.stringify({name, recipe: buildRecipe()}));
      await load(); render();                                       // render() builds a fresh notice area, so say() comes after it
      say("Saved " + d.saved + " in recipes/.");
    }
    catch (e) { say("Not saved: " + e.message, e.problems, true); }
  }
  async function brief() {
    try {
      const r = await fetch(BASE + "/api/montage/brief"); const text = await r.text();
      try { await navigator.clipboard.writeText(text); say("Brief copied. Paste it to Claude: it lists your files, their captions and the recipe format. Save what it writes as a recipe, or paste it into recipes/ on the machine."); }
      catch (e) { say("Select and copy this brief:"); noticeBox.append(el("textarea", {rows: 8, value: text, readOnly: true})); }
    } catch (e) { say("Could not load the brief (" + e.message + ")", [], true); }
  }
  async function cancel() { try { await call("POST", "/api/montage/cancel", "{}"); } catch (e) {} poll(); }
  function renderJobs() {
    if (!jobsBox || !st) return;
    if (goBtn) { goBtn.disabled = !st.can_render || st.active; goBtn.textContent = st.active ? "Rendering…" : "Make video"; }
    if (draftBtn) draftBtn.disabled = !st.can_render || st.active;
    runBtns.forEach(b => { b.disabled = st.active; });
    const kids = [];
    st.jobs.slice(-4).reverse().forEach((j, n) => {
      const line = el("div", {className: "row"}, el("span", {className: "name"}, j.title + " · " + j.template + " · " + j.format + (j.quality === "draft" ? " · draft" : "")),
        el("span", {className: "dim"}, j.state === "running" ? "rendering " + j.percent + "%" : j.state === "queued" ? "waiting" : j.state === "done" ? "✓ done" : j.state === "failed" ? "✗ " + j.error : "cancelled"));
      kids.push(el("div", {className: "urow"}, line, (j.state === "running" || j.state === "queued") ? el("progress", {max: 100, value: j.percent}) : ""));
      if (n === 0) (j.warnings || []).forEach(w => kids.push(el("div", {className: "dim"}, "⚠ " + w)));     // only the newest job's
    });
    const latest = st.jobs.filter(j => j.state === "done").slice(-1)[0];
    if (latest) kids.push(el("video", {className: "thumb", controls: true, playsInline: true, src: BASE + "/exports/" + encodeURIComponent(latest.output)}));
    if (st.active) { const stop = el("button", {type: "button", className: "alt"}, "Cancel"); stop.addEventListener("click", cancel); kids.push(stop); }
    if (st.exports.length) {
      kids.push(el("div", {className: "dim"}, "Finished videos"));
      st.exports.slice(0, 6).forEach(x => kids.push(el("div", {className: "row"},
        el("a", {href: BASE + "/exports/" + encodeURIComponent(x.name), target: "_blank"}, x.name),
        el("span", {className: "dim"}, (x.quality === "draft" ? "draft · " : "") + (x.duration ? secs(x.duration) + " · " : "") + mb(x.size)))));
    }
    jobsBox.replaceChildren(...kids);
  }
  function schedule(ms) { if (!timer) timer = setTimeout(() => { timer = null; poll(); }, ms); }
  async function poll() {
    try { await load(); renderJobs(); if (st.active) schedule(1500); } catch (e) { schedule(5000); }
  }
  function render() {
    if (!st) return;
    const media = st.catalog.filter(i => i.kind !== "audio");
    if (!media.length) { mount.replaceChildren(); return; }
    const select = (options, value) => { const s = el("select", {}); options.forEach(([v, label]) => s.append(el("option", {value: v}, label))); s.value = value; return s; };
    ctl = {
      format: select(Object.entries(st.formats).map(([k, f]) => [k, f.label]), form.format),
      template: select(Object.entries(st.templates).map(([k, t]) => [k, t.label + " — " + t.blurb]), form.template),
      title: el("input", {type: "text", maxLength: 100, placeholder: "Title card text (optional)", value: form.title}),
      captions: el("input", {type: "checkbox", checked: form.captions}),
      music: select([["", "No music (add a track in the app when you post)"]].concat(st.catalog.filter(i => i.kind === "audio").map(i => [i.name, i.name])), form.music),
      max: select([["15", "up to 15 s"], ["30", "up to 30 s"], ["45", "up to 45 s"], ["60", "up to 60 s"], ["90", "up to 90 s"]], form.max),
    };
    listBox = el("div", {}); planBox = el("div", {}); noticeBox = el("div", {className: "notice ok"}); jobsBox = el("div", {});
    const go = el("button", {type: "button"}, "Make video");
    goBtn = go; runBtns = [];
    const draft = el("button", {type: "button", className: "alt"}, "Quick draft (half size, fast)");
    draftBtn = draft;
    const look = el("button", {type: "button", className: "alt"}, "Preview plan");
    const keep = el("button", {type: "button", className: "alt"}, "Save recipe…");
    const ask = el("button", {type: "button", className: "alt"}, "Copy brief for Claude");
    go.addEventListener("click", () => make()); look.addEventListener("click", preview);
    draft.addEventListener("click", () => make(Object.assign(buildRecipe(), {quality: "draft"})));
    keep.addEventListener("click", save); ask.addEventListener("click", brief);
    if (!st.can_render) { go.disabled = true; look.disabled = true; }
    const saved = st.recipes.map(r => {
      const use = el("button", {type: "button", className: "mv"}, "Use"), run = el("button", {type: "button", className: "mv"}, "Make");
      if (r.valid) { use.addEventListener("click", () => useRecipe(r.recipe)); run.addEventListener("click", () => make(r.recipe)); runBtns.push(run); }
      return el("div", {className: "urow"}, el("div", {className: "row"}, el("span", {className: "name"}, r.name),
        r.valid ? el("span", {}, use, run) : el("span", {className: "dim"}, "needs fixing")),
        ...(r.valid ? [] : r.problems.map(p => el("div", {className: "dim"}, "• " + p))));
    });
    mount.replaceChildren(el("section", {className: "card"},
      el("h2", {}, el("span", {}, "Make a video"), el("span", {className: "dim"}, "for Instagram, Reels, TikTok")),
      el("div", {className: "dim"}, "Pick the look and the pictures; every timing is worked out for you."),
      el("div", {className: "dim"}, "Format"), ctl.format, el("div", {className: "dim"}, "Look"), ctl.template,
      ctl.title, el("label", {className: "row"}, el("span", {}, ctl.captions, " Show captions on screen")),
      el("div", {className: "dim"}, "Music"), ctl.music, el("div", {className: "dim"}, "Length"), ctl.max,
      el("div", {className: "dim"}, "Pictures and clips, in order (tick to include)"), listBox,
      noticeBox, planBox, go, draft, look, keep, ask,
      ...(saved.length ? [el("div", {className: "dim"}, "Saved recipes")].concat(saved) : []),
      jobsBox));
    renderList(); renderJobs();
  }
  async function refresh() {
    try { syncForm(); await load(); render(); if (st.active) schedule(1500); } catch (e) { /* the card just stays as it was */ }
  }
  return {refresh};
}
"""
