// Drives the REAL project page (uploader + caption pass) against a live server.
// usage: node js_details_harness.js <port> <slug> <scenario>
const vm = require("vm");
const [port, slug, scenario] = [process.argv[2], process.argv[3], process.argv[4]];
const ORIGIN = `http://127.0.0.1:${port}`;
const realFetch = (path, init) => fetch(ORIGIN + path.replace(/^\/umcares/, ""), init);

function mk(tag) {
  return {
    tag, children: [], textContent: "", hidden: false, value: "", className: "", disabled: false, _l: {}, files: [], title: "", src: "", href: "",
    append(...k) { for (const x of k) { if (typeof x === "string") this.textContent += x; else if (x) { x.parent = this; this.children.push(x); } } },
    replaceChildren(...k) { this.children = []; this.append(...k); },
    replaceWith() {},
    addEventListener(ev, f) { this._l[ev] = f; },
    removeAttribute() {},
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    click() { return this._l.click && this._l.click(); },
  };
}
const walk = (n, f) => { if (!n || typeof n !== "object") return; f(n); (n.children || []).forEach(c => walk(c, f)); };
const all = (n, pred) => { const out = []; walk(n, x => { if (pred(x)) out.push(x); }); return out; };
const deepText = n => { let t = ""; walk(n, x => { t += x.textContent + " "; }); return t.trim().replace(/\s+/g, " "); };
const ids = {};
for (const id of ["back", "title", "status", "add", "speed", "summary", "queue", "retry", "lib", "pick", "shoot", "film", "f_pick", "f_shoot", "f_film", "details", "detailsBtn", "prepare"]) ids[id] = mk("x");
ids.retry.hidden = true; ids.detailsBtn.hidden = true;
const document = { title: "", getElementById: id => ids[id], createElement: mk };

const calls = [];
let failSaves = false;
async function fakeFetch(path, init = {}) {
  const m = (init.method || "GET").toUpperCase();
  calls.push(m + " " + path.replace(/^\/umcares\/p\/[^/]+/, ""));
  if (failSaves && m === "POST" && /\/api\/media\//.test(path)) return new Response(JSON.stringify({ error: "disk is full" }), { status: 500 });
  return realFetch(path, init);
}
const ctx = vm.createContext({
  document, console, performance, Date, Math, JSON, Object, Array, Promise, Error, Blob, File, URLSearchParams,
  fetch: fakeFetch, setTimeout, setInterval, clearInterval, encodeURIComponent,
  navigator: {}, window: { addEventListener() {} }, location: { search: scenario === "auto-open" ? "?details=1" : "" },
});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const press = async b => { if (b._l.click) await b._l.click(); if (b.onclick) await b.onclick(); await sleep(150); };

const card = () => ids.details.children[0] || null;
const button = (text) => card() && all(card(), n => n.tag === "button" && n.textContent === text)[0];
const view = () => {
  const c = card();
  if (!c) return { open: false, banner: { hidden: ids.detailsBtn.hidden, text: ids.detailsBtn.textContent } };
  const h2 = c.children[0];
  const area = all(c, n => n.tag === "textarea")[0];
  return {
    open: true, title: h2.children[0].textContent, position: h2.children[1] ? h2.children[1].textContent : "",
    name: all(c, n => n.className === "name")[0] ? all(c, n => n.className === "name")[0].textContent : "",
    caption: area ? area.value : null, error: all(c, n => n.className === "err")[0] ? all(c, n => n.className === "err")[0].textContent : "",
    buttons: all(c, n => n.tag === "button").map(b => ({ text: b.textContent, hidden: !!b.hidden, disabled: !!b.disabled })),
    text: deepText(c), banner: { hidden: ids.detailsBtn.hidden, text: ids.detailsBtn.textContent },
  };
};
const typeAndSave = async (text, label) => { all(card(), n => n.tag === "textarea")[0].value = text; await press(button(label)); };
const tiles = () => all(ids.lib, n => n.className === "item").map(t => ({
  name: all(t, n => n.className === "name")[0].textContent, hasEdit: all(t, n => n.className === "edit").length > 0, dim: all(t, n => n.className === "dim").map(d => d.textContent).join(" | "),
}));

(async () => {
  const page = await (await realFetch(`/umcares/p/${slug}/`)).text();
  for (const m of page.matchAll(/<script(?:\s+src="([^"]+)")?>([\s\S]*?)<\/script>/g)) {
    vm.runInContext(m[1] ? await (await realFetch(m[1])).text() : m[2], ctx);
  }
  await sleep(500);
  let out = {};
  if (scenario === "tiles") {
    out = { tiles: tiles(), view: view(), status: ids.status.textContent };
  } else if (scenario === "pass") {
    await press(ids.detailsBtn);
    out.first = view();
    await typeAndSave("Dr Madihah opening the session", "Save & next");
    out.second = view();
    await press(button("Skip (no detail)"));
    out.third = view();
    await typeAndSave("Participants at the registration desk", "Save & finish");
    out.done = view();
  } else if (scenario === "empty") {
    await press(ids.detailsBtn);
    await press(button("Save & next"));
    out.view = view();
  } else if (scenario === "back") {
    await press(ids.detailsBtn);
    await typeAndSave("first caption", "Save & next");
    await press(button("Back"));
    out.view = view();
  } else if (scenario === "save-fails") {
    await press(ids.detailsBtn);
    failSaves = true;
    await typeAndSave("this will not save", "Save & next");
    out.failed = view();
    failSaves = false;
    await press(button("Save & next"));
    out.retried = view();
  } else if (scenario === "stop") {
    await press(ids.detailsBtn);
    await typeAndSave("one done", "Save & next");
    await press(button("Stop for now"));
    out.view = view();
  } else if (scenario === "edit-one") {
    const first = JSON.parse(await (await realFetch(`/umcares/p/${slug}/api/media`)).text()).items[0];
    vm.runInContext(`details.start({only: ["${first.sha}"]})`, ctx);
    await sleep(300);
    out.view = view();
    await typeAndSave("a better caption", "Save & finish");
    out.done = view();
  } else if (scenario === "auto-open") {
    out.view = view();
  } else if (scenario === "after-upload") {
    out.before = { pending: ids.detailsBtn.textContent };
    ctx.__f = [new File([Buffer.from("brand new upload content")], "NEW-upload.jpg", { lastModified: 99 })];
    vm.runInContext("uploader.enqueue(__f)", ctx);
    const t0 = Date.now();
    while (Date.now() - t0 < 30000 && !(card())) await sleep(50);
    await sleep(300);
    out.view = view();
  }
  out.calls = calls;
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(e => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
