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
    replaceWith() {}, addEventListener(ev, f) { this._l[ev] = f; }, removeAttribute() {}, setAttribute(k, v) { (this.attrs = this.attrs || {})[k] = v; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    click() { return this._l.click && this._l.click(); },
  };
}
const walk = (n, f) => { if (!n || typeof n !== "object") return; f(n); (n.children || []).forEach(c => walk(c, f)); };
const all = (n, pred) => { const out = []; walk(n, x => { if (pred(x)) out.push(x); }); return out; };
const deepText = n => { let t = []; walk(n, x => { if (x.textContent) t.push(x.textContent); }); return t.join(" | "); };
const ids = {};
for (const id of ["back", "title", "status", "add", "speed", "summary", "queue", "retry", "lib", "pick", "shoot", "film", "song", "f_pick", "f_shoot", "f_film", "f_song", "details", "detailsBtn", "prepare", "montage"]) ids[id] = mk("x");
ids.retry.hidden = true; ids.detailsBtn.hidden = true;
const document = { title: "", getElementById: id => ids[id], createElement: mk };

const calls = [];
const confirms = [];
let answer = true;
let clip = null;
const ctx = vm.createContext({
  document, console, performance, Date, Math, JSON, Object, Array, Promise, Error, Blob, File, URLSearchParams, Set,
  prompt: () => "My reel",
  confirm: m => { confirms.push(m); return answer; },
  navigator: { clipboard: { writeText: async t => { clip = t; } } }, window: { addEventListener() {} }, location: { search: "" },
  fetch: async (path, init = {}) => { calls.push((init.method || "GET").toUpperCase() + " " + path.replace(/^\/umcares\/p\/[^/]+/, "")); return realFetch(path, init); },
  setTimeout: (f, ms) => setTimeout(f, Math.min(ms, 50)), setInterval, clearInterval, encodeURIComponent,
});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const press = async b => { if (b._l.click) await b._l.click(); if (b.onclick) await b.onclick(); await sleep(250); };

const card = () => ids.montage.children[0] || null;
const button = text => card() && all(card(), n => n.tag === "button" && n.textContent === text)[0];
const named = (kind, name) => all(card(), n => n.attrs && n.attrs["data-" + kind] === name);
const ctlOf = name => named("ctl", name)[0];                    // controls and parts are found by name, never by position: the layout is free to change
const partOf = name => named("part", name)[0];
const pickBoxes = () => named("ctl", "pick");
const labels = s => s.children.map(o => o.textContent);
const view = () => {
  const c = card();
  if (!c) return { open: false };
  const [fmt, tpl, music, max] = ["format", "template", "music", "max"].map(ctlOf);
  const err = partOf("notice"), rowsBox = partOf("list");
  return {
    open: true, title: partOf("title").textContent,
    formats: labels(fmt), templates: labels(tpl), music: labels(music), lengths: labels(max),
    values: { format: fmt.value, template: tpl.value, music: music.value, max: max.value },
    rows: rowsBox.children.map(r => ({ text: r.children[0].children[0].textContent, checked: r.children[0].children[0].children[0].checked, sub: r.children[1] && r.children[1].textContent || "" })),
    card: deepText(c), notice: deepText(err), noticeClass: err.className, plan: deepText(partOf("plan")), jobs: deepText(partOf("jobs")),
    video: (all(c, n => n.tag === "video")[0] || {}).src || null,
    poster: (all(c, n => n.tag === "video")[0] || {}).poster || null, preload: (all(c, n => n.tag === "video")[0] || {}).preload || null,
    links: all(c, n => n.tag === "a").map(a => a.href),
    buttons: all(c, n => n.tag === "button").map(b => b.textContent).filter(t => !["↑", "↓"].includes(t)),
    saved: all(partOf("saved") || {}, n => n.className === "urow").map(r => deepText(r)),
  };
};
const check = (i, v) => { pickBoxes()[i].checked = v; };

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
    const fmt = ctlOf("format"), tpl = ctlOf("template");
    tpl.value = "punchy"; fmt.value = "square";
    ctlOf("title").value = "Hello CyberDSA";
    await press(button("Preview plan"));
    out.view = view();
  } else if (scenario === "sync") {
    const sync = ctlOf("sync");
    out.disabledAtStart = !!sync.disabled;
    out.speechDefault = ctlOf("speech").checked;
    out.soundDefault = ctlOf("sound").checked;
    out.tightenDefault = ctlOf("tighten").checked;
    const music = ctlOf("music");
    music.value = "song.m4a";
    if (music._l.change) await music._l.change();
    out.enabledAfterMusic = !sync.disabled;
    sync.checked = true;
    ctlOf("template").value = "punchy";
    await press(button("Preview plan"));
    out.withSync = view();
    music.value = "";
    if (music._l.change) await music._l.change();
    out.afterRemoval = { disabled: !!sync.disabled, checked: !!sync.checked };
  } else if (scenario === "zoomcut") {
    ctlOf("cut").value = "zoom";
    await press(button("Preview plan"));
    out.view = view();
  } else if (scenario === "effect") {
    ctlOf("effect").value = "glitch";
    await press(button("Preview plan"));
    out.view = view();
  } else if (scenario === "threed") {
    ctlOf("cut").value = "flip";
    ctlOf("depth").checked = true;
    await press(button("Preview plan"));
    out.view = view();
  } else if (scenario === "sfx") {
    ctlOf("fxcuts").value = "hit";
    await press(button("Preview plan"));
    out.view = view();
  } else if (scenario === "tempo") {
    ctlOf("bpm").value = "75";
    ctlOf("template").value = "punchy";
    await press(button("Preview plan"));
    out.view = view();
  } else if (scenario === "make") {
    ctlOf("template").value = "punchy";
    await press(button("Make video"));
    out.during = view();                                            // right after pressing Make, before it finishes
    await press(button("Rendering…") || { _l: {}, onclick: null });  // a second press must do nothing
    const t0 = Date.now();
    while (Date.now() - t0 < 120000) { const v = view(); if (v.video) break; await sleep(150); }
    await sleep(400);
    out.view = view();
  } else if (scenario === "delete") {
    ctlOf("template").value = "punchy";
    await press(button("Make video"));
    const t0 = Date.now();
    while (Date.now() - t0 < 120000) { const v = view(); if (v.video) break; await sleep(150); }
    await sleep(400);
    out.before = view();
    answer = false;                                                   // "Cancel" in the confirm box: nothing may happen
    await press(button("Delete"));
    out.declined = view();
    answer = true;
    await press(button("Delete"));
    await sleep(400);
    out.after = view();
    out.confirms = confirms;
  } else if (scenario === "draft" || scenario === "share") {
    await press(button(scenario === "draft" ? "Quick draft (half size, fast)" : "Smaller file for sharing"));
    const t0 = Date.now();
    while (Date.now() - t0 < 120000) { const v = view(); if (v.video) break; await sleep(150); }
    await sleep(400);
    out.view = view();
  } else if (scenario === "bad") {
    [0, 1, 2].forEach(i => check(i, false));
    await press(button("Make video"));
    out.view = view();
  } else if (scenario === "allcards" || scenario === "cardops") {
    // type into a field the way a person does: the value changes, then the page hears an input event
    const type = (name, v) => { const e = named("ctl", name).slice(-1)[0]; e.value = v; if (e._l.input) e._l.input(); if (e._l.change) e._l.change(); };
    const nPictures = pickBoxes().length;                                                    // the pictures that were there before any card was added
    const add = async kind => { const k = ctlOf("cardKind"); k.value = kind; if (k._l.change) await k._l.change(); await press(ctlOf("cardAdd")); };
    if (scenario === "allcards") {
      await add("quote"); type("cf-quote", "Belajar tanpa henti."); type("cf-by", "Dr Madihah");
      await add("announce"); type("cf-title", "Hackathon CyberDSA"); type("cf-date", "12 Okt 2026"); type("cf-place", "Dewan A");
      await add("countdown"); type("cf-n", "4");
      await add("checklist"); type("cf-title", "Alat yang tepat"); type("cf-items", "Task Board\n  Version Control  \n\nFocus Timer\n");
      await add("progress"); type("cf-label", "Produktiviti"); type("cf-from", "22"); type("cf-to", "100"); type("cf-unit", "%");
      await add("typing"); type("cf-title", "sprint.ts"); type("cf-text", "const a = 1;\n  ship(a);\n");
      await add("list"); type("cf-title", "Hari ini"); type("cf-items", "Standup 09:00\nRetro 16:00"); type("cf-from", "left");
      await add("flow"); type("cf-title", "Dari kod ke pengguna"); type("cf-steps", "Kod\nBinaan\n\n Ujian \nKeluaran");
      await add("versus"); type("cf-title", "Sebelum dan selepas"); type("cf-blabel", "Cara lama"); type("cf-bitems", "Uji manual\nRalat manusia"); type("cf-alabel", "Kini"); type("cf-aitems", "Ujian automatik")
      pickBoxes().slice(0, nPictures).forEach(b => { b.checked = false; });                // leave out the pictures: only the cards
    } else {
      await add("quote"); type("cf-quote", "Satu");
      await add("quote"); type("cf-quote", "Dua");
      await add("quote"); type("cf-quote", "Tiga");
      const rowsNow = () => view().rows.map(r => r.text);
      out.before = rowsNow();
      const downs = all(card(), n => n.tag === "button" && n.textContent === "↑");
      await press(downs[downs.length - 1]);                                                  // "Tiga" moves above "Dua"
      out.moved = view().card;
      const removes = all(card(), n => n.tag === "button" && n.textContent === "Remove card");
      await press(removes[0]);                                                               // one card is removed
      out.afterRemove = view().card;
      pickBoxes().slice(0, nPictures).forEach(b => { b.checked = false; });                  // only the cards are ticked
      ctlOf("template").value = "punchy";
    }
    await press(button("Save recipe…"));
    out.saved = view();
  } else if (scenario === "cardsurvive") {
    const k = ctlOf("cardKind"); k.value = "quote"; if (k._l.change) await k._l.change(); await press(ctlOf("cardAdd"));
    const q = named("ctl", "cf-quote").slice(-1)[0]; q.value = "Tahan uji"; if (q._l.input) q._l.input();
    out.before = view().rows.map(r => r.text);
    await press(button("Quick draft (half size, fast)"));                                    // making a video reloads the page's data from the server
    const t0 = Date.now();
    while (Date.now() - t0 < 120000) { const v = view(); if (v.video) break; await sleep(150); }
    await sleep(400);
    out.video = !!view().video;
    await press(ctlOf("cardAdd"));                                                          // the next edit redraws the list from the reloaded data
    out.after = view().rows.map(r => r.text);
  } else if (scenario === "usecards") {
    const use = all(card(), n => n.tag === "button" && n.textContent === "Use")[0];
    await press(use);
    out.used = view();
    out.editors = all(card(), n => n.attrs && n.attrs["data-part"] === "card-editor").length;
    await press(all(card(), n => n.tag === "button" && n.textContent === "Preview plan")[0]);
    out.plan = view().plan;
  } else if (scenario === "save") {
    ctlOf("template").value = "melancholy";
    await press(button("Save recipe…"));
    out.saved = view();
    ctlOf("template").value = "clean";                                   // change it, then load the saved one back
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
