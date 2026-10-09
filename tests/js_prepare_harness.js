// Drives the REAL project page (including the prepare card) against a live server.
// usage: node js_prepare_harness.js <port> <slug> <scenario>
const vm = require("vm");
const [port, slug, scenario] = [process.argv[2], process.argv[3], process.argv[4]];
const ORIGIN = `http://127.0.0.1:${port}`;
const realFetch = (path, init) => fetch(ORIGIN + path.replace(/^\/umcares/, ""), init);

function mk(tag) {
  return {
    tag, children: [], textContent: "", hidden: false, value: "", className: "", disabled: false, _l: {}, files: [], title: "", src: "", href: "",
    append(...k) { for (const x of k) { if (typeof x === "string") this.textContent += x; else if (x) { x.parent = this; this.children.push(x); } } },
    replaceChildren(...k) { this.children = []; this.append(...k); },
    replaceWith() {}, addEventListener(ev, f) { this._l[ev] = f; }, removeAttribute() {},
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    click() { return this._l.click && this._l.click(); },
  };
}
const walk = (n, f) => { if (!n || typeof n !== "object") return; f(n); (n.children || []).forEach(c => walk(c, f)); };
const all = (n, pred) => { const out = []; walk(n, x => { if (pred(x)) out.push(x); }); return out; };
const ids = {};
for (const id of ["back", "title", "status", "add", "speed", "summary", "queue", "retry", "lib", "pick", "shoot", "film", "f_pick", "f_shoot", "f_film", "details", "detailsBtn", "prepare", "montage"]) ids[id] = mk("x");
ids.retry.hidden = true; ids.detailsBtn.hidden = true;
const document = { title: "", getElementById: id => ids[id], createElement: mk };

const calls = [];
const ctx = vm.createContext({
  document, console, performance, Date, Math, JSON, Object, Array, Promise, Error, Blob, File, URLSearchParams,
  alert: m => { calls.push("ALERT " + m); },
  fetch: async (path, init = {}) => { calls.push((init.method || "GET").toUpperCase() + " " + path.replace(/^\/umcares\/p\/[^/]+/, "")); return realFetch(path, init); },
  setTimeout: (f, ms) => setTimeout(f, Math.min(ms, 50)),          // keep the polling quick in a test
  setInterval, clearInterval, encodeURIComponent,
  navigator: {}, window: { addEventListener() {} }, location: { search: "" },
});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const view = () => {
  const c = ids.prepare.children[0];
  if (!c) return { open: false };
  const h2 = c.children[0];
  return {
    open: true, title: h2.children[0].textContent, count: h2.children[1].textContent,
    rows: all(c, n => n.className === "urow").map(r => ({ name: all(r, n => n.className === "name")[0].textContent, text: all(r, n => n.className === "dim").map(d => d.textContent).join(" | ") })),
    buttons: all(c, n => n.tag === "button").map(b => b.textContent),
  };
};

(async () => {
  const page = await (await realFetch(`/umcares/p/${slug}/`)).text();
  for (const m of page.matchAll(/<script(?:\s+src="([^"]+)")?>([\s\S]*?)<\/script>/g)) {
    vm.runInContext(m[1] ? await (await realFetch(m[1])).text() : m[2], ctx);
  }
  await sleep(600);
  let out = {};
  if (scenario === "run") {
    const go = all(ids.prepare, n => n.tag === "button" && n.textContent.startsWith("Prepare"))[0];
    await go.click();
    const t0 = Date.now();
    while (Date.now() - t0 < 120000) {
      const v = view();
      if (v.open && v.rows.length && v.rows.every(r => r.text === "✓ prepared") && !v.buttons.length) break;
      await sleep(100);
    }
    await sleep(500);                                              // the library reload that follows
  }
  out.view = view(); out.calls = calls;
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(e => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
