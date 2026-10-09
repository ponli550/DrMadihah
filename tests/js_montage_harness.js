// Drives the REAL project page's Make-a-video card against a live server.
// usage: node js_montage_harness.js <port> <slug> <scenario>
const vm = require("vm");
const [port, slug, scenario] = [process.argv[2], process.argv[3], process.argv[4]];
const ORIGIN = `http://127.0.0.1:${port}`;
const realFetch = (path, init) => fetch(ORIGIN + path.replace(/^\/umcares/, ""), init);

function mk(tag) {
  return {
    tag, children: [], textContent: "", hidden: false, value: "", className: "", disabled: false, checked: false, _l: {}, files: [], title: "", src: "", href: "",
    append(...k) { for (const x of k) { if (typeof x === "string") this.textContent += x; else if (x) { x.parent = this; this.children.push(x); } } },
    replaceChildren(...k) { this.children = []; this.textContent = ""; this.append(...k); },
    replaceWith() {}, addEventListener(ev, f) { this._l[ev] = f; }, removeAttribute() {},
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    click() { return this._l.click && this._l.click(); },
  };
}
const walk = (n, f) => { if (!n || typeof n !== "object") return; f(n); (n.children || []).forEach(c => walk(c, f)); };
const all = (n, pred) => { const out = []; walk(n, x => { if (pred(x)) out.push(x); }); return out; };
const deepText = n => { let t = []; walk(n, x => { if (x.textContent) t.push(x.textContent); }); return t.join(" | "); };
const ids = {};
for (const id of ["back", "title", "status", "add", "speed", "summary", "queue", "retry", "lib", "pick", "shoot", "film", "f_pick", "f_shoot", "f_film", "details", "detailsBtn", "prepare", "montage"]) ids[id] = mk("x");
ids.retry.hidden = true; ids.detailsBtn.hidden = true;
const document = { title: "", getElementById: id => ids[id], createElement: mk };

const calls = [];
let clip = null;
const ctx = vm.createContext({
  document, console, performance, Date, Math, JSON, Object, Array, Promise, Error, Blob, File, URLSearchParams, Set,
  prompt: () => "My reel",
  navigator: { clipboard: { writeText: async t => { clip = t; } } }, window: { addEventListener() {} }, location: { search: "" },
  fetch: async (path, init = {}) => { calls.push((init.method || "GET").toUpperCase() + " " + path.replace(/^\/umcares\/p\/[^/]+/, "")); return realFetch(path, init); },
  setTimeout: (f, ms) => setTimeout(f, Math.min(ms, 50)), setInterval, clearInterval, encodeURIComponent,
});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const press = async b => { if (b._l.click) await b._l.click(); if (b.onclick) await b.onclick(); await sleep(250); };

const card = () => ids.montage.children[0] || null;
const button = text => card() && all(card(), n => n.tag === "button" && n.textContent === text)[0];
const selects = () => all(card(), n => n.tag === "select");
const labels = s => s.children.map(o => o.textContent);
const view = () => {
  const c = card();
  if (!c) return { open: false };
  const [fmt, tpl, music, max] = selects();
  const err = all(c, n => (n.className || "").split(" ").includes("notice"))[0];
  const at = c.children.indexOf(err);
  const rowsBox = c.children[c.children.indexOf(err) - 1];
  return {
    open: true, title: c.children[0].children[0].textContent,
    formats: labels(fmt), templates: labels(tpl), music: labels(music), lengths: labels(max),
    values: { format: fmt.value, template: tpl.value, music: music.value, max: max.value },
    rows: rowsBox.children.map(r => ({ text: r.children[0].children[0].textContent, checked: r.children[0].children[0].children[0].checked, sub: r.children[1] && r.children[1].textContent || "" })),
    notice: deepText(err), noticeClass: err.className, plan: deepText(c.children[at + 1]), jobs: deepText(c.children[c.children.length - 1]),
    video: (all(c, n => n.tag === "video")[0] || {}).src || null,
    links: all(c, n => n.tag === "a").map(a => a.href),
    buttons: all(c, n => n.tag === "button").map(b => b.textContent).filter(t => !["↑", "↓"].includes(t)),
    saved: all(c, n => n.className === "urow" && n.parent === c).map(r => deepText(r)),
  };
};
const check = (i, v) => { all(card(), n => n.tag === "input" && n.type === "checkbox")[i + 1].checked = v; };   // [0] is "show captions"

(async () => {
  const page = await (await realFetch(`/umcares/p/${slug}/`)).text();
  for (const m of page.matchAll(/<script(?:\s+src="([^"]+)")?>([\s\S]*?)<\/script>/g)) {
    vm.runInContext(m[1] ? await (await realFetch(m[1])).text() : m[2], ctx);
  }
  await sleep(700);
  let out = {};
  if (scenario === "card" || scenario === "none") {
    out.view = view();
  } else if (scenario === "reorder") {
    out.before = view();
    await press(all(card(), n => n.tag === "button" && n.textContent === "↓")[0]);
    out.after = view();
    await press(button("Preview plan"));
    out.preview = view();
  } else if (scenario === "untick") {
    check(1, false);
    await press(button("Preview plan"));
    out.view = view();
  } else if (scenario === "preview") {
    const [fmt, tpl] = selects();
    tpl.value = "punchy"; fmt.value = "square";
    all(card(), n => n.tag === "input" && n.type === "text")[0].value = "Hello CyberDSA";
    await press(button("Preview plan"));
    out.view = view();
  } else if (scenario === "make") {
    selects()[1].value = "punchy";
    await press(button("Make video"));
    out.during = view();                                            // right after pressing Make, before it finishes
    await press(button("Rendering…") || { _l: {}, onclick: null });  // a second press must do nothing
    const t0 = Date.now();
    while (Date.now() - t0 < 120000) { const v = view(); if (v.video) break; await sleep(150); }
    await sleep(400);
    out.view = view();
  } else if (scenario === "bad") {
    [0, 1, 2].forEach(i => check(i, false));
    await press(button("Make video"));
    out.view = view();
  } else if (scenario === "save") {
    selects()[1].value = "melancholy";
    await press(button("Save recipe…"));
    out.saved = view();
    selects()[1].value = "clean";                                   // change it, then load the saved one back
    const use = all(card(), n => n.tag === "button" && n.textContent === "Use")[0];
    await press(use);
    out.used = view();
  } else if (scenario === "brief") {
    await press(button("Copy brief for Claude"));
    out.view = view(); out.clip = clip;
  }
  out.calls = calls;
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(e => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
