"""Driving Adobe Premiere Pro on the remote machine.

We do NOT go through the AdobePremiereProMCP tool layer. That server is broken
in ways that cannot be worked around from the outside:

  * `premiere.jsx` never parses — ExtendScript is an ES3 engine and the file
    uses `package` as an identifier, so it throws
    `SyntaxError: Illegal use of reserved word 'package'`. It defines
    `mcpDispatch`, which every command routes through, so all 72 tools fail.
  * The panel loads its jsx from `__dirname/host/...`, but CEP sets
    `__dirname` to the extension ROOT while the files live under `src/host/`.
  * The Go tool schemas were written against premiere.jsx, so their argument
    names do not match `core.jsx` (e.g. `premiere_place_clip` sends
    `source_path`; core.jsx reads `projectItemIndex`, an integer). Unknown
    arguments are dropped silently, so calls "succeed" while doing the wrong
    thing.

Instead we attach to the CEP panel's Chrome DevTools port and evaluate
ExtendScript directly. `heal()` loads core.jsx from the correct path and
installs a small dispatcher plus the helpers the CLI needs.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import log
from .config import Remote
from .transport import Transport

# Node helper that talks CDP. Kept on the remote; refreshed when out of date.
DRIVER_JS = r"""
// umcares CDP driver: evaluate an expression inside the CEP panel.
// usage: node umc_cdp.js <exprFile> [timeoutMs]
const fs = require('fs');
const http = require('http');
const PORT = process.env.UMC_CDP_PORT || 9241;
const WS_PATHS = [
  '%(mcp)s/cep-panel/dist/node_modules/ws',
  '%(mcp)s/ts-bridge/node_modules/ws',
];
let WebSocket = null;
for (const p of WS_PATHS) { try { WebSocket = require(p); break; } catch (e) {} }
if (!WebSocket) { console.error('UMC_ERR ws module not found'); process.exit(3); }

const exprFile = process.argv[2];
const timeoutMs = parseInt(process.argv[3] || '300000', 10);
const expr = fs.readFileSync(exprFile, 'utf8');

http.get({host: '127.0.0.1', port: PORT, path: '/json/list'}, (res) => {
  let body = '';
  res.on('data', (c) => (body += c));
  res.on('end', () => {
    let pages;
    try { pages = JSON.parse(body); } catch (e) { console.error('UMC_ERR bad /json/list'); process.exit(4); }
    const page = pages.find((p) => (p.title || '').indexOf('MCP') !== -1) || pages[0];
    if (!page) { console.error('UMC_ERR no CEP page on port ' + PORT); process.exit(5); }
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    const done = (code) => { try { ws.close(); } catch (e) {} process.exit(code); };
    ws.on('open', () => {
      ws.send(JSON.stringify({id: 1, method: 'Runtime.enable'}));
      ws.send(JSON.stringify({id: 2, method: 'Runtime.evaluate', params: {
        expression: expr, awaitPromise: true, returnByValue: true, timeout: timeoutMs}}));
    });
    ws.on('message', (data) => {
      let d; try { d = JSON.parse(data.toString()); } catch (e) { return; }
      if (d.id !== 2) return;
      const r = d.result || {};
      if (r.exceptionDetails) {
        console.error('UMC_ERR ' + JSON.stringify(r.exceptionDetails).slice(0, 800));
        return done(6);
      }
      const v = r.result && 'value' in r.result ? r.result.value : r.result;
      process.stdout.write(typeof v === 'string' ? v : JSON.stringify(v));
      done(0);
    });
    ws.on('error', (e) => { console.error('UMC_ERR ws ' + e.message); done(7); });
  });
}).on('error', (e) => { console.error('UMC_ERR http ' + e.message); process.exit(8); });
setTimeout(() => { console.error('UMC_ERR driver timeout'); process.exit(9); }, timeoutMs + 20000);
"""

# ExtendScript helpers installed by heal(). Assigned onto $.global so they
# survive: a bare `function` inside an evalScript wrapper is function-scoped
# and disappears the moment the call returns.
HELPERS_JSX = r"""
$.global.mcpDispatch = function (fn, argsJson) {
  try {
    var f = $.global[fn];
    if (typeof f !== "function") {
      return JSON.stringify({success:false, error:"'" + fn + "' unavailable (premiere.jsx does not parse)"});
    }
    return f(argsJson === undefined ? "" : argsJson);
  } catch (e) { return JSON.stringify({success:false, error:String(e)}); }
};

$.global.umFind = function (mediaPath) {
  var hit = null;
  var walk = function (bin) {
    var kids = bin.children; if (!kids) return;
    for (var i = 0; i < kids.numItems; i++) {
      if (hit) return;
      var it = kids[i], p = "";
      try { p = it.getMediaPath ? it.getMediaPath() : ""; } catch (e) { p = ""; }
      if (p && p === mediaPath) { hit = it; return; }
      if (it.children && it.children.numItems > 0) walk(it);
    }
  };
  walk(app.project.rootItem);
  return hit;
};

$.global.umClear = function () {
  var seq = app.project.activeSequence, n = 0;
  var strip = function (t) {
    if (!t) return;
    for (var k = t.clips.numItems - 1; k >= 0; k--) {
      try { t.clips[k].remove(false, false); n++; } catch (e) {}
    }
  };
  for (var v = 0; v < seq.videoTracks.numTracks; v++) strip(seq.videoTracks[v]);
  for (var a = 0; a < seq.audioTracks.numTracks; a++) strip(seq.audioTracks[a]);
  return n;
};

$.global.umPlace = function (mediaPath, startSec, vIdx) {
  var seq = app.project.activeSequence;
  var item = umFind(mediaPath);
  if (!item) return "NOTFOUND " + mediaPath;
  var tr = seq.videoTracks[vIdx || 0];
  if (!tr) return "NOTRACK";
  try { tr.overwriteClip(item, startSec); } catch (e) { return "ERR " + String(e); }
  return "OK";
};

$.global.umPlaceAudio = function (mediaPath, startSec, aIdx) {
  var seq = app.project.activeSequence;
  var item = umFind(mediaPath);
  if (!item) return "NOTFOUND " + mediaPath;
  var tr = seq.audioTracks[aIdx || 0];
  if (!tr) return "NOTRACK";
  try { tr.overwriteClip(item, startSec); } catch (e) { return "ERR " + String(e); }
  return "OK";
};

// Premiere's Volume>Level is NORMALISED, not dB. Default reads 0.17782794,
// which is 10^(-15/20), so the parameter maps +15 dB to 1.0.
$.global.umDbToLevel = function (db) { return Math.pow(10, (db - 15) / 20); };

$.global.umLevelProp = function (clip) {
  for (var ci = 0; ci < clip.components.numItems; ci++) {
    var comp = clip.components[ci];
    for (var pi = 0; pi < comp.properties.numItems; pi++) {
      var p = comp.properties[pi];
      if (String(p.displayName) === "Level") return p;   // [0] is Bypass, not Level
    }
  }
  return null;
};

$.global.umSetDb = function (clip, db) {
  var p = umLevelProp(clip);
  if (!p) return false;
  try { p.setValue(umDbToLevel(db), true); return true; } catch (e) { return false; }
};

$.global.umReport = function () {
  var seq = app.project.activeSequence;
  if (!seq) return JSON.stringify({error: "no active sequence"});
  var v0 = seq.videoTracks[0], spans = [], names = [];
  for (var k = 0; k < v0.clips.numItems; k++) {
    spans.push([v0.clips[k].start.seconds, v0.clips[k].end.seconds]);
    names.push(v0.clips[k].name);
  }
  spans.sort(function (a, b) { return a[0] - b[0]; });
  var gaps = [], cur = 0;
  for (var m = 0; m < spans.length; m++) {
    if (spans[m][0] - cur > 0.2) {
      gaps.push(Math.round(cur * 10) / 10 + "->" + Math.round(spans[m][0] * 10) / 10);
    }
    if (spans[m][1] > cur) cur = spans[m][1];
  }
  return JSON.stringify({
    sequence: seq.name,
    fps: 254016000000 / Number(seq.timebase),
    width: seq.frameSizeHorizontal, height: seq.frameSizeVertical,
    clips: v0.clips.numItems, end: Math.round(cur * 10) / 10,
    gaps: gaps, names: names
  });
};
"""


class Premiere:
    def __init__(self, t: Transport, remote: Remote):
        self.t = t
        self.remote = remote
        self._driver_ready = False

    # -- plumbing -----------------------------------------------------------
    def _ensure_driver(self) -> None:
        if self._driver_ready:
            return
        js = DRIVER_JS % {"mcp": self.remote.mcp_repo}
        self._write_remote("/tmp/umc_cdp.js", js)
        self._driver_ready = True

    def _write_remote(self, path: str, text: str) -> None:
        """Write a text file remotely.

        Delegates to Transport.push so both routes are covered by one
        implementation: scp over ssh, or the length-verified chunked base64
        over tmux. Avoids re-inventing (and re-breaking) the chunking.
        """
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(text)
            tmp = Path(fh.name)
        try:
            self.t.push(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def evaluate(self, expression: str, timeout: int = 300) -> str:
        """Evaluate JS inside the CEP panel; returns its string result."""
        self._ensure_driver()
        self._write_remote("/tmp/umc_expr.js", expression)
        node = self.t.resolve_bin("node")
        env = f"UMC_CDP_PORT={self.remote.cdp_port}"
        r = self.t.run(
            f"{env} {node} /tmp/umc_cdp.js /tmp/umc_expr.js {timeout * 1000}",
            timeout=timeout + 60)
        if not r.ok:
            raise RuntimeError(f"CDP evaluate failed: {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout.strip()

    def eval_jsx(self, jsx: str, timeout: int = 300) -> str:
        """Evaluate ExtendScript (not panel JS) and return its result."""
        wrapper = (
            "new Promise(function(resolve){"
            "  window.__adobe_cep__.evalScript(" + json.dumps(jsx) + ", function(r){ resolve(String(r)); });"
            "  setTimeout(function(){ resolve('UMC_TIMEOUT'); }, " + str(timeout * 1000 - 5000) + ");"
            "})"
        )
        return self.evaluate(wrapper, timeout=timeout)

    # -- operations ---------------------------------------------------------
    def heal(self) -> dict:
        """Load core.jsx from the correct path and install our helpers."""
        core = f"{self.remote.cep_ext_dir}/src/host/core.jsx"
        jsx = (
            "(function(){ try {"
            f"  $.evalFile(new File({json.dumps(core)}));"
            "  " + HELPERS_JSX +
            "  return JSON.stringify({core: typeof importFiles, dispatch: typeof mcpDispatch,"
            "    place: typeof umPlace, report: typeof umReport});"
            "} catch(e){ return 'ERR ' + String(e); } })()"
        )
        res = self.eval_jsx(jsx, timeout=90)
        if res.startswith("ERR") or res == "UMC_TIMEOUT":
            raise RuntimeError(f"heal failed: {res}")
        return json.loads(res)

    def open_project(self, path: str) -> dict:
        """Open a .prproj.

        Not via the MCP `premiere_open` tool: that returns `already_running`
        and never opens the file when Premiere is already up. core.jsx's
        openProject does the real work.
        """
        jsx = (
            "(function(){ try {"
            f"  var f = new File({json.dumps(path)});"
            "  if (!f.exists) return 'ERR not found';"
            "  if (app.project && app.project.path === f.fsName)"
            "    return JSON.stringify({already: true, name: app.project.name});"
            f"  app.openDocument({json.dumps(path)});"
            "  return JSON.stringify({opened: true,"
            "    name: app.project ? app.project.name : null,"
            "    path: app.project ? app.project.path : null});"
            "} catch(e){ return 'ERR ' + String(e); } })()"
        )
        res = self.eval_jsx(jsx, timeout=180)
        if not res.startswith("{"):
            raise RuntimeError(f"open failed: {res}")
        return json.loads(res)

    def list_presets(self, filter_text: str = "1080") -> list:
        """Sequence presets available in this Premiere install."""
        root = f"{self.remote.app_dir}/Contents/Settings/SequencePresets"
        r = self.t.run(
            f'find {json.dumps(root)} -name "*.sqpreset" 2>/dev/null | grep -i '
            f'{json.dumps(filter_text)} | head -40', timeout=120)
        return [l.strip() for l in r.stdout.splitlines() if l.strip().endswith(".sqpreset")]

    def list_export_presets(self, filter_text: str = "1080") -> list:
        """Export presets (.epr) this install ships with."""
        root = f"{self.remote.app_dir}/Contents/Settings/EncoderPresets"
        r = self.t.run(
            f'find {json.dumps(root)} -name "*.epr" 2>/dev/null | grep -i '
            f'{json.dumps(filter_text)} | head -40', timeout=120)
        return [l.strip() for l in r.stdout.splitlines() if l.strip().endswith(".epr")]

    def create_sequence(self, name: str, preset: str = "",
                        match_clip: str = "") -> dict:
        """Create a sequence, WITHOUT opening Premiere's modal dialog.

        `createNewSequence(name, preset)` takes a .sqpreset PATH as its second
        argument. Passing the sequence name there — which the upstream helper
        does — makes Premiere fall back to its New Sequence dialog, which
        blocks every further ExtendScript call until a human dismisses it.

        Prefer an explicit preset. `createNewSequenceFromClips` is the
        fallback, but note it does NOT inherit frame rate: matching a 50fps
        clip that way produced a 23.976fps sequence.
        """
        jsx = (
            "(function(){ try {"
            "  if (!app.project) return 'ERR no project open';"
            f"  var name = {json.dumps(name)};"
            f"  var preset = {json.dumps(preset)};"
            f"  var match = {json.dumps(match_clip)};"
            "  if (preset) {"
            "    if (!new File(preset).exists) return 'ERR preset not found: ' + preset;"
            "    app.project.createNewSequence(name, preset);"
            "  } else {"
            "    var item = match ? umFind(match) : null;"
            "    if (!item) return 'ERR no preset given and no clip to match "
            "(a bare createNewSequence would open a modal dialog)';"
            "    try { app.project.createNewSequenceFromClips(name, [item], app.project.rootItem); }"
            "    catch(e2) { app.project.createNewSequenceFromClips(name, [item]); }"
            "  }"
            "  var seq = app.project.activeSequence;"
            "  if (!seq) return 'ERR sequence created but not active';"
            "  return JSON.stringify({name: seq.name, id: String(seq.sequenceID),"
            "    width: seq.frameSizeHorizontal, height: seq.frameSizeVertical,"
            "    fps: Math.round((254016000000/Number(seq.timebase))*1000)/1000});"
            "} catch(e){ return 'ERR ' + String(e); } })()"
        )
        res = self.eval_jsx(jsx, timeout=240)
        if not res.startswith("{"):
            raise RuntimeError(f"create sequence failed: {res}")
        return json.loads(res)

    def ping(self) -> dict:
        jsx = ("(function(){ try { return JSON.stringify({"
               "  version: app.version,"
               "  project: (app.project && app.project.name) ? app.project.name : null,"
               "  path: (app.project && app.project.path) ? app.project.path : null,"
               "  sequence: app.project.activeSequence ? app.project.activeSequence.name : null"
               "}); } catch(e){ return 'ERR ' + String(e); } })()")
        res = self.eval_jsx(jsx, timeout=60)
        if res.startswith("ERR") or res == "UMC_TIMEOUT":
            raise RuntimeError(f"ping failed: {res} (is a project open?)")
        return json.loads(res)

    def report(self) -> dict:
        res = self.eval_jsx("umReport()", timeout=90)
        if not res.startswith("{"):
            raise RuntimeError(f"report failed: {res} — run `umcares premiere heal` first")
        return json.loads(res)

    def import_files(self, paths: list, bin_name: str = "") -> int:
        jsx_paths = json.dumps(paths)
        jsx = (
            "(function(){ try {"
            f"  var paths = {jsx_paths}; var n = 0;"
            "  for (var i = 0; i < paths.length; i++) {"
            "    try { if (app.project.importFiles([paths[i]], true,"
            "          app.project.getInsertionBin(), false)) n++; } catch(e){}"
            "  }"
            "  return String(n);"
            "} catch(e){ return 'ERR ' + String(e); } })()"
        )
        res = self.eval_jsx(jsx, timeout=300)
        if res.startswith("ERR"):
            raise RuntimeError(f"import failed: {res}")
        return int(res or 0)

    def build(self, plan: dict) -> dict:
        """Lay out video/audio from a plan, then tidy and report.

        plan = {"video": [[path, start], ...],
                "audio": [[path, start, track], ...],
                "mute_sync_db": -60}
        """
        video = plan.get("video", [])
        audio = plan.get("audio", [])
        mute_db = plan.get("mute_sync_db", -60)
        # [[substring, dB], ...] — first match wins, else mute_db. This is how
        # a testimonial keeps its own voice while b-roll ambience is silenced.
        levels = plan.get("audio_levels", [])
        starts = sorted({round(float(v[1]), 2) for v in video})

        jsx = (
            "(function(){ try {"
            "  umClear();"
            f"  var V = {json.dumps(video)}; var A = {json.dumps(audio)};"
            "  var fails = [];"
            "  for (var i = 0; i < V.length; i++) {"
            "    var r = umPlace(V[i][0], V[i][1], 0);"
            "    if (r !== 'OK') fails.push('V ' + V[i][0] + ' ' + r);"
            "  }"
            "  for (var j = 0; j < A.length; j++) {"
            "    var r2 = umPlaceAudio(A[j][0], A[j][1], A[j][2]);"
            "    if (r2 !== 'OK') fails.push('A ' + A[j][0] + ' ' + r2);"
            "  }"
            # overwriteClip leaves an orphan tail whenever a later clip covers
            # only part of an earlier one; drop anything not at a planned start
            f"  var planned = {json.dumps(starts)};"
            "  var isPlanned = function(t){"
            "    for (var p = 0; p < planned.length; p++) { if (Math.abs(planned[p]-t) < 0.15) return true; }"
            "    return false; };"
            "  var seq = app.project.activeSequence, removed = 0;"
            "  var sweep = function(tr){ for (var k = tr.clips.numItems-1; k >= 0; k--) {"
            "      if (!isPlanned(tr.clips[k].start.seconds)) { try { tr.clips[k].remove(false,false); removed++; } catch(e){} } } };"
            "  sweep(seq.videoTracks[0]); sweep(seq.audioTracks[0]);"
            f"  var a0 = seq.audioTracks[0], muted = 0, db = {mute_db};"
            f"  var rules = {json.dumps(levels)};"
            "  for (var m = 0; m < a0.clips.numItems; m++) {"
            "    var c = a0.clips[m], nm = String(c.name), lvl = db;"
            "    for (var q = 0; q < rules.length; q++) {"
            "      if (nm.indexOf(rules[q][0]) !== -1) { lvl = rules[q][1]; break; }"
            "    }"
            "    if (umSetDb(c, lvl)) muted++;"
            "  }"
            "  app.project.save();"
            "  var rep = JSON.parse(umReport());"
            "  rep.failures = fails; rep.orphansRemoved = removed; rep.syncMuted = muted;"
            "  return JSON.stringify(rep);"
            "} catch(e){ return 'ERR ' + String(e); } })()"
        )
        res = self.eval_jsx(jsx, timeout=600)
        if not res.startswith("{"):
            raise RuntimeError(f"build failed: {res}")
        return json.loads(res)

    def export(self, out_path: str, preset: str, timeout: int = 1800) -> dict:
        jsx = (
            "(function(){ try {"
            f"  if (!new File({json.dumps(preset)}).exists) return 'ERR preset missing';"
            "  var seq = app.project.activeSequence;"
            "  if (!seq) return 'ERR no active sequence';"
            "  var t0 = new Date().getTime();"
            f"  var r = seq.exportAsMediaDirect({json.dumps(out_path)}, {json.dumps(preset)}, 0);"
            "  return JSON.stringify({result: String(r),"
            "    seconds: Math.round((new Date().getTime()-t0)/1000)});"
            "} catch(e){ return 'ERR ' + String(e); } })()"
        )
        res = self.eval_jsx(jsx, timeout=timeout)
        if not res.startswith("{"):
            raise RuntimeError(f"export failed: {res}")
        info = json.loads(res)
        if not self.t.exists(out_path):
            raise RuntimeError("export reported success but no file was written")
        info["bytes"] = self.t.size(out_path)
        return info
