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
        $("after").append(el("a", {href: ROOT + "/p/" + encodeURIComponent(slug) + "/"}, "Open " + name));
      },
    });
    up.enqueue(files);
  });
})();
</script></body></html>
"""
