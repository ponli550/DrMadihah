// Runs the REAL service worker and the REAL share page against a live server, with a fake IndexedDB.
// usage: node js_share_harness.js <port> <scenario>
// Prints one JSON line describing what happened.
const vm = require("vm");

const [port, scenario] = [process.argv[2], process.argv[3]];
const ORIGIN = `http://127.0.0.1:${port}`;
const realFetch = (path, init) => fetch(ORIGIN + path.replace(/^\/umcares/, ""), init);

// ---------------- fake IndexedDB (just the calls the worker and the page make) ----------------
function fakeIdb(opts = {}) {
  const dbs = {}, state = { failOpen: !!opts.failOpen };
  const req = fn => {
    const r = { result: undefined, error: null, onsuccess: null, onerror: null };
    setTimeout(() => { try { r.result = fn(); r.onsuccess && r.onsuccess({ target: r }); } catch (e) { r.error = e; r.onerror && r.onerror({ target: r }); } }, 0);
    return r;
  };
  const makeTx = store => {
    const tx = { oncomplete: null, onerror: null, onabort: null, error: null };
    let timer;
    const touch = () => { clearTimeout(timer); timer = setTimeout(() => tx.oncomplete && tx.oncomplete(), 10); };
    tx.objectStore = () => ({
      put(o) { store.rows.set(o[store.keyPath], o); touch(); return req(() => o[store.keyPath]); },
      get(k) { touch(); return req(() => store.rows.get(k)); },
      delete(k) { store.rows.delete(k); touch(); return req(() => undefined); },
      getAll() { touch(); return req(() => [...store.rows.values()]); },
    });
    touch();
    return tx;
  };
  const makeDb = () => {
    const stores = {};
    return { stores, close() {}, createObjectStore(n, o) { stores[n] = { keyPath: o.keyPath, rows: new Map() }; }, transaction(n) { return makeTx(stores[n]); } };
  };
  return {
    state, dbs,
    open(name) {
      const r = { result: null, error: null, onsuccess: null, onerror: null, onupgradeneeded: null };
      setTimeout(() => {
        if (state.failOpen) { r.error = new Error("storage blocked"); r.onerror && r.onerror(); return; }
        let db = dbs[name];
        r.result = db || (dbs[name] = makeDb());
        if (!db) r.onupgradeneeded && r.onupgradeneeded();
        r.onsuccess && r.onsuccess({ target: r });
      }, 0);
      return r;
    },
    rows() { const db = dbs["umcares-share"]; return db ? [...db.stores.shares.rows.values()] : []; },
  };
}

// ---------------- run the service worker ----------------
async function loadWorker(idb) {
  const code = await (await realFetch("/umcares/sw.js")).text();
  const listeners = {};
  const self = {
    location: new URL("https://phone.example/umcares/sw.js"),
    addEventListener: (t, f) => { listeners[t] = f; },
    skipWaiting() {}, clients: { claim() {} },
  };
  const Response = { redirect: (u, status) => ({ status, location: u }) };      // browsers resolve a relative URL; node's does not
  vm.runInContext(code, vm.createContext({ self, indexedDB: idb, Response, URL, Date, Math, Promise, Error, console }));
  return {
    async fire(request) {                                                       // returns {handled, response}
      let promise = null;
      listeners.fetch({ request, respondWith: p => { promise = p; } });
      return promise ? { handled: true, response: await promise } : { handled: false };
    },
  };
}
const mkFile = (name, bytes, type, mtime = 1) => new File([Buffer.from(bytes)], name, { type, lastModified: mtime });
const shareRequest = (files, extra = {}) => {
  const form = new FormData();
  for (const f of files) form.append("media", f);
  for (const [k, v] of Object.entries(extra)) form.append(k, v);
  return { method: "POST", url: "https://phone.example/umcares/share-target", formData: async () => form };
};

// ---------------- minimal DOM for the share page ----------------
function makePage(idb, search, storage) {
  const all = [], inputs = [];
  const mk = tag => {
    const e = {
      tag, children: [], textContent: "", hidden: false, value: "", className: "", checked: false, _l: {}, name: "", id: "", href: "", src: "",
      append(...k) { for (const x of k) { if (typeof x === "string") this.textContent += x; else { x.parent = this; this.children.push(x); } } },
      replaceChildren(...k) { this.children = k; },
      addEventListener(ev, f) { this._l[ev] = f; },
      click() { if (this._l.click) return this._l.click(); },
      remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    };
    all.push(e); if (tag === "input") inputs.push(e);
    return e;
  };
  const ids = {};
  for (const id of ["status", "files", "count", "previews", "pickcard", "projects", "go", "discard", "uploadcard", "speed", "queue", "summary", "retry", "after", "home"]) {
    ids[id] = mk("x"); ids[id].id = id;
  }
  for (const id of ["files", "pickcard", "uploadcard", "retry"]) ids[id].hidden = true;       // as in the HTML
  const document = {
    title: "", getElementById: id => ids[id], createElement: mk,
    querySelector: sel => sel === "input[name=project]:checked" ? (inputs.find(i => i.name === "project" && i.checked) || null) : null,
  };
  const ls = { ...storage };
  return { ids, inputs, all, ctx: {
    document, indexedDB: idb, localStorage: { getItem: k => (k in ls ? ls[k] : null), setItem: (k, v) => { ls[k] = String(v); } },
    location: { search }, URL: { createObjectURL: f => "blob:" + f.name, revokeObjectURL() {} }, URLSearchParams,
    navigator: {}, window: { addEventListener() {} },
  }, ls };
}

async function runPage(idb, search, { storage = {}, fetchImpl } = {}) {
  const page = makePage(idb, search, storage);
  const calls = [];
  const ctx = vm.createContext({
    ...page.ctx, console, performance, Date, Math, JSON, Object, Array, Promise, Error, TypeError, Blob, File, Response,
    fetch: async (path, init = {}) => { const m = (init.method || "GET").toUpperCase(); calls.push(m + " " + path.replace(/^\/umcares\/p\/[^/]+/, "")); const custom = fetchImpl ? await fetchImpl(path, init, m) : null; return custom || realFetch(path, init); },
    setTimeout: (f, ms) => setTimeout(f, Math.min(ms, 20)), setInterval, clearInterval, encodeURIComponent,
  });
  const html = await (await realFetch("/umcares/share")).text();
  for (const m of html.matchAll(/<script(?:\s+src="([^"]+)")?>([\s\S]*?)<\/script>/g)) {
    vm.runInContext(m[1] ? await (await realFetch(m[1])).text() : m[2], ctx);
  }
  await new Promise(r => setTimeout(r, 300));                                   // the page's own async start-up
  return { page, calls };
}
const waitFor = async (cond, ms = 60000) => { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await new Promise(r => setTimeout(r, 25)); };
const snap = (page, idb, calls) => ({
  status: page.ids.status.textContent, hidden: { files: page.ids.files.hidden, pick: page.ids.pickcard.hidden, upload: page.ids.uploadcard.hidden },
  count: page.ids.count.textContent, previews: page.ids.previews.children.length,
  radios: page.inputs.filter(i => i.name === "project").map(i => ({ value: i.value, checked: i.checked })),
  summary: page.ids.summary.textContent, retryHidden: page.ids.retry.hidden,
  after: page.ids.after.children.map(c => ({ text: c.textContent, href: c.href })),
  queue: page.ids.queue.children.map(r => r.children[0].children[1].textContent),
  left: idb.rows().map(r => ({ id: r.id, files: r.files.map(f => f.name) })), ls: page.ls, calls,
});
// distinct bytes and names per scenario, so the server's dedupe never turns a scenario into "already there"
const photoBytes = fill => Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), Buffer.alloc(300 * 1024, fill)]);
const videoBytes = fill => Buffer.alloc(700 * 1024, fill);
const png = photoBytes(7), vid = videoBytes(9);

(async () => {
  const idb = fakeIdb({ failOpen: scenario === "sw-idbfail" });
  const sw = await loadWorker(idb);
  let out;

  if (scenario.startsWith("sw-")) {
    if (scenario === "sw-stash") {
      const r = await sw.fire(shareRequest([mkFile("a.jpg", png, "image/jpeg"), mkFile("b.mp4", vid, "video/mp4")], { title: "ignored", text: "ignored" }));
      out = { handled: r.handled, status: r.response.status, location: r.response.location, rows: idb.rows().map(x => ({ id: x.id, files: x.files.map(f => [f.name, f.size]) })) };
    } else if (scenario === "sw-other") {
      const cases = [
        { method: "GET", url: "https://phone.example/umcares/share-target" },
        { method: "POST", url: "https://phone.example/umcares/api/projects" },
        { method: "POST", url: "https://phone.example/umcares/p/demo/api/upload/start" },
        { method: "PUT", url: "https://phone.example/umcares/p/demo/api/upload/abc?offset=0" },
        { method: "GET", url: "https://phone.example/umcares/" },
        { method: "POST", url: "https://phone.example/elsewhere/share-target" },
      ];
      out = { handled: [] };
      for (const c of cases) out.handled.push((await sw.fire({ ...c, formData: async () => new FormData() })).handled);
      out.rows = idb.rows().length;
    } else if (scenario === "sw-empty") {
      const only = await sw.fire(shareRequest([], { title: "just text" }));
      const zero = await sw.fire(shareRequest([mkFile("empty.jpg", Buffer.alloc(0), "image/jpeg")]));
      out = { only: only.response.location, zero: zero.response.location, rows: idb.rows().length };
    } else if (scenario === "sw-idbfail") {
      const r = await sw.fire(shareRequest([mkFile("a.jpg", png, "image/jpeg")]));
      out = { handled: r.handled, status: r.response.status, location: r.response.location };
    } else if (scenario === "sw-expiry") {
      const db = await new Promise(res => { const r = idb.open("umcares-share"); r.onupgradeneeded = () => r.result.createObjectStore("shares", { keyPath: "id" }); r.onsuccess = () => res(r.result); });
      db.stores.shares.rows.set("old", { id: "old", at: Date.now() - 8 * 86400 * 1000, files: [] });
      db.stores.shares.rows.set("recent", { id: "recent", at: Date.now() - 86400 * 1000, files: [] });
      await sw.fire(shareRequest([mkFile("new.jpg", png, "image/jpeg")]));
      out = { ids: idb.rows().map(r => r.id).filter(i => i === "old" || i === "recent").sort(), total: idb.rows().length };
    }
  } else {
    // page scenarios: a share is stashed by the worker first, then the page is opened at the address it redirects to
    let search = "";
    if (["page-happy", "page-fail", "page-discard", "page-last", "page-hostile"].includes(scenario)) {
      const files = scenario === "page-hostile" ? [mkFile("<img src=x onerror=alert(1)>.jpg", photoBytes(27), "image/jpeg")]
        : scenario === "page-fail" ? [mkFile("fail-photo.jpg", photoBytes(17), "image/jpeg", 21), mkFile("fail-clip.mp4", videoBytes(19), "video/mp4", 22)]
        : [mkFile("shared-photo.jpg", png, "image/jpeg", 11), mkFile("shared-clip.mp4", vid, "video/mp4", 12)];
      const r = await sw.fire(shareRequest(files));
      search = r.response.location.slice(r.response.location.indexOf("?"));
    } else if (scenario === "page-unknown") search = "?id=doesnotexist";
    else if (scenario === "page-error") search = "?error=1";
    else if (scenario === "page-empty") search = "?empty=1";
    // page-noid: no query at all

    const storage = scenario === "page-last" ? { "umcares.lastProject": "second" } : {};
    const failVideoIds = new Set();
    // for page-fail, make every chunk upload of the video fail: identify its session from the start call
    const fetchImpl = scenario !== "page-fail" ? undefined : async (path, init, m) => {
      if (m === "POST" && path.endsWith("/api/upload/start")) {
        const res = await realFetch(path, init); const body = JSON.parse(init.body);
        if (body.name === "fail-clip.mp4") { const j = await res.clone().json(); if (j.id) failVideoIds.add(`/umcares/p/demo/api/upload/${j.id}`); }
        return res;
      }
      if (m === "PUT" && failVideoIds.has(path.split("?")[0])) return new Response(JSON.stringify({ error: "boom" }), { status: 500 });
      return null;
    };
    const { page, calls } = await runPage(idb, search, { storage, fetchImpl });
    if (["page-happy", "page-fail", "page-last", "page-hostile"].includes(scenario)) {
      const before = snap(page, idb, calls);
      await page.ids.go.click();
      await waitFor(() => page.ids.summary.textContent !== "");
      await new Promise(r => setTimeout(r, 400));
      out = { before, after: snap(page, idb, calls) };
    } else if (scenario === "page-discard") {
      const before = snap(page, idb, calls);
      await page.ids.discard.click();
      await new Promise(r => setTimeout(r, 100));
      out = { before, after: snap(page, idb, calls) };
    } else out = { after: snap(page, idb, calls) };
  }
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(e => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
