// Runs the REAL project-page script against a live server, with a minimal DOM and a scripted network.
// usage: node js_upload_harness.js <port> <slug> <scenario>
// Prints one JSON line: {rows:[{name,msg}], summary, retryHidden, calls:[...]}
const vm = require("vm");
const http = require("http");

const [port, slug, scenario] = [process.argv[2], process.argv[3], process.argv[4]];
const ORIGIN = `http://127.0.0.1:${port}`;

function mk(tag) {
  const e = {
    tag, children: [], textContent: "", hidden: false, value: 0, className: "", _l: {}, files: [], style: "",
    append(...k) { for (const x of k) { if (typeof x === "string") this.textContent += x; else this.children.push(x); } },
    replaceChildren(...k) { this.children = k; },
    addEventListener(ev, f) { this._l[ev] = f; },
    click() { if (this._l.click) this._l.click(); },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    replaceWith() {},
    querySelector(sel) { return find(this, sel.slice(1)); },
  };
  return e;
}
function find(node, cls) {
  for (const c of node.children || []) {
    if (c && typeof c === "object") {
      if ((c.className || "").split(" ").includes(cls)) return c;
      const d = find(c, cls); if (d) return d;
    }
  }
  return null;
}
const ids = {};
for (const id of ["back", "title", "status", "add", "speed", "summary", "queue", "retry", "lib", "pick", "shoot", "film", "f_pick", "f_shoot", "f_film"]) ids[id] = mk("x");
ids.retry.hidden = true;
const document = {
  title: "", getElementById: id => ids[id],
  createElement: mk,
  querySelectorAll: sel => sel === "#queue .urow" ? ids.queue.children.filter(c => (c.className || "") === "urow") : [],
};
// children appended through el() need a parent pointer for remove()
const origMk = mk;
document.createElement = tag => { const e = origMk(tag); const app = e.append.bind(e); e.append = (...k) => { k.forEach(x => { if (x && typeof x === "object") x.parent = e; }); app(...k); }; return e; };
for (const id in ids) { const e = ids[id]; const app = e.append.bind(e); e.append = (...k) => { k.forEach(x => { if (x && typeof x === "object") x.parent = e; }); app(...k); }; }

// ---- scripted network: strip the tailscale mount, call the real server, optionally misbehave ----
const calls = [];
let putCount = 0;
async function realFetch(path, init) {
  const target = ORIGIN + path.replace(/^\/umcares/, "");
  return fetch(target, init);
}
async function fakeFetch(path, init = {}) {
  const method = (init.method || "GET").toUpperCase();
  calls.push(method + " " + path.replace(/^\/umcares\/p\/[^/]+/, ""));
  if (scenario === "flaky" && method === "PUT") {
    putCount++;
    if (putCount % 3 === 1) throw new TypeError("network down (before the server saw it)");
    if (putCount % 3 === 2) { await realFetch(path, init); throw new TypeError("response lost (the server DID store the chunk)"); }
  }
  return realFetch(path, init);
}

const ctx = vm.createContext({
  document, console, performance, Date, Math, JSON, Object, Array, Promise, Error, TypeError, Blob, File, URL,
  fetch: fakeFetch,
  setTimeout: (f, ms) => setTimeout(f, Math.min(ms, 20)),     // keep the retry backoff short in a test
  setInterval, clearInterval,
  navigator: {}, window: { addEventListener() {} }, location: {},
  encodeURIComponent,
});

(async () => {
  const page = await (await realFetch(`/umcares/p/${slug}/`)).text();
  for (const m of page.matchAll(/<script(?:\s+src="([^"]+)")?>([\s\S]*?)<\/script>/g)) {   // external scripts first, in page order
    const code = m[1] ? await (await realFetch(m[1])).text() : m[2];
    vm.runInContext(code, ctx);
  }
  await new Promise(r => setTimeout(r, 300));                    // let the initial library load settle

  const mkFile = (name, bytes, mtime) => new File([Buffer.from(bytes)], name, { lastModified: mtime });
  const big = Buffer.alloc(900 * 1024); for (let i = 0; i < big.length; i++) big[i] = (i * 7 + 3) & 255;
  const other = Buffer.alloc(900 * 1024); for (let i = 0; i < other.length; i++) other[i] = (i * 11 + 5) & 255;   // different content from `big`
  const small = Buffer.from("tiny image bytes");
  const files = {
    normal: () => [mkFile("one.jpg", big, 1), mkFile("two.mp4", Buffer.from("video bytes ".repeat(5000)), 2), mkFile("copy.jpg", big, 3)],
    flaky: () => [mkFile("flaky.jpg", other, 4), mkFile("flaky2.mp4", Buffer.from("more video ".repeat(40000)), 5)],
    repick: () => [mkFile("one.jpg", big, 1)],
    refused: () => [mkFile("notes.txt", small, 6), mkFile("ok.jpg", small, 7)],
  }[scenario === "flaky" ? "flaky" : scenario]();

  ctx.__files = files;
  vm.runInContext("uploader.enqueue(__files)", ctx);
  const t0 = Date.now();
  while (Date.now() - t0 < 60000) {
    const busy = vm.runInContext("uploader.busy()", ctx);
    if (!busy) break;
    await new Promise(r => setTimeout(r, 25));
  }
  await new Promise(r => setTimeout(r, 200));                    // allDone() and the library reload

  const rows = ids.queue.children.map(r => ({ name: find(r, "name").textContent, msg: find(r, "dim").textContent }));
  console.log(JSON.stringify({ rows, summary: ids.summary.textContent, retryHidden: ids.retry.hidden, calls }));
  process.exit(0);
})().catch(e => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
