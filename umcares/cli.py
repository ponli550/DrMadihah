"""umcares — drive the remote Adobe Premiere video pipeline from one CLI.

Human progress goes to stderr, results go to stdout, so this composes:

    umcares media probe --dir ~/…/Photos | jq '.needs_transcode'
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (auth, doctor, inspect as inspect_mod, log, media, post, recipe as
               recipe_mod, render as render_mod, scaffold, secrets, session,
               spinner, stack)
from .example import EXAMPLE_RECIPE
from . import __version__
from .config import Config
from .premiere import Premiere
from .transport import connect

PRESET_1080P50 = doctor.PRESET_1080P50


def _transport(args, cfg):
    return connect(cfg.remote, prefer=args.transport)


# -- commands ---------------------------------------------------------------
def cmd_session(args, cfg):
    if args.reconnect:
        res = session.reconnect(cfg.remote)
        log.out(res)
        return 0 if res["state"] == "connected" else 1
    if args.status:
        log.out(session.status(cfg.remote))
        return 0
    session.create(cfg.remote, cwd=str(cfg.root), editor=args.editor,
                   attach=not args.no_attach, force=args.force)
    return 0


def cmd_auth(args, cfg):
    if args.setup_key:
        # use the working transport so no password prompt is needed
        try:
            t = _transport(args, cfg)
        except SystemExit:
            t = None
            log.warn("no transport — falling back to ssh-copy-id (interactive)")
        log.out(auth.setup_key(cfg.remote, transport=t))
        return 0
    info = auth.status(cfg.remote)
    if info["ssh_works"]:
        log.ok(f"ssh works via {info['ssh_target']} ({info['ssh_method']})")
    else:
        log.warn(f"ssh not available — falling back to: {info.get('fallback')}")
        log.warn("run `umcares auth --setup-key` for key login (recommended)")
    if info["password_configured"] and not info["sshpass_installed"]:
        log.warn("UMC_SSH_PASSWORD is set but sshpass is missing: "
                 "brew install hudochenkov/sshpass/sshpass")
    log.out(info)
    return 0


def cmd_config(args, cfg):
    env_path = cfg.root / ".env"
    if args.action == "init":
        from . import wizard
        return wizard.run(cfg, only=args.section, encrypt=args.encrypt)

    if args.action == "sections":
        from . import wizard
        for name, (title, fields) in wizard.SECTIONS.items():
            log.ok(f"{name:<12} {title} ({len(fields)} settings)")
        log.out({k: v[0] for k, v in wizard.SECTIONS.items()})
        return 0

    if args.action == "set":
        if not args.key or args.value is None:
            log.err("usage: umcares config set KEY VALUE")
            return 2
        res = secrets.set_value(env_path, args.key, args.value,
                                encrypt_secrets=args.encrypt)
        (log.ok if res["stored"] == "encrypted" else log.warn)(
            f"{args.key} stored ({res['stored']})")
        log.out(res)
        return 0

    if args.action == "get":
        val = cfg.optional(args.key)
        if not val:
            log.err(f"{args.key} is not set")
            return 1
        log.out(val if args.reveal else secrets.mask(args.key, val))
        return 0

    if args.action == "show":
        eff = cfg.effective()
        width = max(len(k) for k in eff)
        for k, info in eff.items():
            tag = info["from"]
            line = f"  {k:<{width}}  {info['value']}"
            (log.ok if tag in ("env", ".env") else log.step)(
                f"{line}   [{tag}]" if tag != "default" else line)
        log.out(eff)
        return 0

    # list
    rows = {k: secrets.mask(k, v) for k, v in sorted(cfg.env.items())}
    info = secrets.status(env_path)
    (log.ok if info["encrypted"] else log.warn)(
        ".env is encrypted" if info["encrypted"] else ".env is plaintext")
    log.out(rows)
    return 0


def cmd_secrets(args, cfg):
    env_path = cfg.root / ".env"
    if args.action == "status":
        info = secrets.status(env_path)
        if info["encrypted"]:
            log.ok(".env is encrypted (dotenvx) — safe to commit")
            if not info["keys_file_exists"] and not info["private_key_in_env"]:
                log.err("but .env.keys is missing — you cannot decrypt on this machine")
        else:
            log.warn(".env is PLAINTEXT — keep it gitignored, or run "
                     "`umcares secrets encrypt`")
        if not info["dotenvx"]:
            log.warn("dotenvx not found: npm install -g @dotenvx/dotenvx")
        log.out(info)
        return 0
    if args.action == "init":
        log.out(secrets.init(env_path))
        return 0
    if args.action == "rotate":
        log.out(secrets.rotate(env_path))
        return 0
    if args.action == "encrypt":
        log.out(secrets.encrypt(env_path))
        return 0
    if args.action == "decrypt":
        log.out(secrets.decrypt(env_path))
        return 0
    return 2


def cmd_init(args, cfg):
    """Create the project folder tree, locally and on the remote."""
    if args.plan:
        log.out(scaffold.plan(cfg.remote.root, cfg.root))
        return 0

    if not args.remote_only:
        made = scaffold.create_local(cfg.root)
        log.ok(f"local: {len(made)} folder(s) created under {cfg.root}"
               if made else "local: already in place")

    if args.local_only:
        return 0

    t = _transport(args, cfg)
    with spinner.spin("creating remote folder tree", 20):
        res = scaffold.create_remote(t, cfg.remote.root)
    log.ok(f"remote: {res['created']} folder(s) created, {res['free']} free")
    check = scaffold.verify(t, cfg.remote.root)
    if not check["ok"]:
        log.err(f"missing after create: {', '.join(check['missing'])}")
        return 1
    log.out({"local_root": str(cfg.root), **res})
    return 0


def cmd_stack(args, cfg):
    t = _transport(args, cfg)

    if args.action == "check":
        info = stack.preflight(t, cfg.remote)
        (log.ok if info["ok"] else log.err)(
            f"docker {info.get('docker','?')} · daemon {info.get('daemon')} · "
            f"compose {info.get('compose')}")
        log.out(info)
        return 0 if info["ok"] else 1

    if args.action == "up":
        pre = stack.preflight(t, cfg.remote)
        if not pre["ok"]:
            log.err("docker preflight failed — run `umcares stack check`")
            log.out(pre)
            return 1
        for port, owner in (pre.get("host_port_conflicts") or {}).items():
            log.warn(f"port {port} already held on the host by {owner}")
        if args.stop_host:
            with spinner.spin("stopping host-run backends", 20):
                stack.stop_host_services(t, cfg.remote)
        est = 1800 if args.build else 90
        with spinner.spin("starting MCP backend containers"
                          + (" (building — first run is slow)" if args.build else ""), est):
            res = stack.up(t, cfg.remote, build=args.build,
                           services=args.services, force=args.stop_host or args.force)
        log.ok(f"{res['running']} service(s) running")
        for port, state in res["ports"].items():
            (log.ok if state == "open" else log.warn)(f"port {port}: {state}")
        log.out(res)
        return 0

    if args.action == "down":
        log.out(stack.down(t, cfg.remote, volumes=args.volumes))
        return 0

    if args.action == "status":
        res = stack.status(t, cfg.remote)
        log.ok(f"{res['running']} service(s) running")
        log.out(res)
        return 0

    if args.action == "logs":
        log.out(stack.logs(t, cfg.remote, args.service or "", args.tail))
        return 0

    if args.action == "stdio":
        log.info("point an MCP client at this command:")
        log.out(stack.stdio_hint(cfg.remote))
        return 0

    return 2


def _workdir(cfg) -> Path:
    d = cfg.root / ".umcares"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_inspect(args, cfg):
    """Probe the media and render contact sheets so an AI can see it."""
    t = _transport(args, cfg)
    dirs = args.dir or [f"{cfg.remote.assets}/edit_ready",
                        f"{cfg.remote.assets}/photos",
                        f"{cfg.remote.assets}/music"]
    work = _workdir(cfg)

    parts = []
    for d in dirs:
        with spinner.spin(f"probing {d}", 60):
            m = inspect_mod.scan(t, d, deep=not args.fast)
        if m.get("error"):
            log.warn(m["error"])
            continue
        parts.append(m)
    if not parts:
        log.err("nothing to inspect — check the paths")
        return 1
    manifest = inspect_mod.merge(parts) if len(parts) > 1 else parts[0]
    for name, paths in (manifest.get("duplicate_names") or {}).items():
        log.warn(f"duplicate filename `{name}` in {len(paths)} places — "
                 f"a recipe reference would be ambiguous")

    counts = manifest.get("counts", {})
    log.ok(f"{counts.get('video',0)} video · {counts.get('photo',0)} photo · "
           f"{counts.get('audio',0)} audio · {manifest.get('video_seconds',0)}s footage")
    bad = manifest.get("needs_transcode") or []
    if bad:
        log.warn(f"{len(bad)} file(s) Premiere would import as AUDIO ONLY: "
                 f"{', '.join(bad[:5])}{' …' if len(bad) > 5 else ''}")
        log.warn("run `umcares media prepare` before referencing them in a recipe")

    sheets = []
    if not args.no_sheets:
        for d in dirs:
            tag = Path(d).name
            for kind in ("video", "photo"):
                with spinner.spin(f"contact sheet: {tag}/{kind}", 60):
                    sh = inspect_mod.contact_sheet(
                        t, d, work / f"sheet_{tag}_{kind}.jpg",
                        kind=kind, cols=args.cols)
                if sh.get("count"):
                    sh["dir"] = d
                    sheets.append(sh)

    (work / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    md = inspect_mod.write_markdown(manifest, sheets, work / "MEDIA.md")

    log.ok(f"wrote {md} — open it (or the sheets) to choose clips for a recipe")
    log.out({"manifest": str(work / "manifest.json"), "markdown": str(md),
             "sheets": [s["sheet"] for s in sheets if s.get("sheet")],
             "counts": counts, "needs_transcode": bad})
    return 0


def cmd_recipe(args, cfg):
    work = _workdir(cfg)

    if args.action == "example":
        out = Path(args.out) if args.out else (cfg.root / "recipe.example.json")
        recipe_mod.save(EXAMPLE_RECIPE, out)
        log.ok(f"wrote {out}")
        log.info("edit it, then: umcares recipe validate --file <path>")
        log.out(str(out))
        return 0

    if not args.file:
        log.err("--file is required")
        return 2
    rec = recipe_mod.apply_defaults(
        recipe_mod.load(Path(args.file).expanduser()), cfg.defaults())

    manifest = None
    mpath = work / "manifest.json"
    if mpath.exists():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    elif args.action == "validate":
        log.warn("no manifest yet — run `umcares inspect` to check media references too")

    if args.action == "validate":
        problems = recipe_mod.validate(rec, manifest)
        if problems:
            for pr in problems:
                log.err(pr)
            log.out({"ok": False, "problems": problems})
            return 1
        log.ok("recipe is valid")
        log.out({"ok": True, "scenes": len(rec.get("scenes") or [])})
        return 0

    if args.action == "resolve":
        # seed from the manifest (clips, music), then let measured values from
        # durations.json win — they come from files we actually rendered
        durations = {}
        if manifest:
            for item in manifest.get("items", []):
                if item.get("duration"):
                    kind = "clip" if item.get("kind") == "video" else "audio"
                    durations[f"{kind}:{item['file']}"] = item["duration"]
        dpath = work / "durations.json"
        if dpath.exists():
            durations.update(json.loads(dpath.read_text(encoding="utf-8")))
        resolved = recipe_mod.resolve(rec, durations)
        print(recipe_mod.summary(resolved), file=sys.stderr)
        if resolved["missing"]:
            log.warn(f"{len(resolved['missing'])} asset(s) have no measured duration "
                     f"— they must be rendered first")
        (work / "resolved.json").write_text(
            json.dumps(resolved, indent=2, ensure_ascii=False), encoding="utf-8")
        log.out(resolved)
        return 0

    return 2


def cmd_render(args, cfg):
    """Render a recipe into a video. Idempotent; re-runs redo only what changed."""
    work = _workdir(cfg)
    rec = recipe_mod.apply_defaults(
        recipe_mod.load(Path(args.file).expanduser()), cfg.defaults())

    manifest = None
    mpath = work / "manifest.json"
    if mpath.exists():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))

    problems = recipe_mod.validate(rec, manifest)
    if problems:
        for pr in problems:
            log.err(pr)
        log.err("recipe is not renderable — fix the above, or re-run "
                "`umcares inspect` if the media moved")
        return 1
    log.ok(f"recipe valid: {len(rec.get('scenes') or [])} scenes")

    stages = render_mod.STAGES
    if args.only:
        stages = args.only
    else:
        if args.from_stage:
            stages = stages[stages.index(args.from_stage):]
        if args.to_stage:
            stages = stages[:stages.index(args.to_stage) + 1]

    if args.dry_run:
        log.out({"would_run": stages, "scenes": len(rec.get("scenes") or [])})
        return 0

    t = _transport(args, cfg)
    res = render_mod.run(t, cfg, rec, work, manifest=manifest,
                         stages=stages, force=args.force)
    log.out(res)
    return 0


def cmd_doctor(args, cfg):
    res = doctor.run(cfg, deep=not args.quick)
    if args.json:
        log.out(res)
    else:
        (log.ok if res["ok"] else log.err)(
            f"{res['passed']}/{res['total']} checks passed")
    return 0 if res["ok"] else 1


def cmd_remote(args, cfg):
    t = _transport(args, cfg)
    log.debug(f"transport = {t.name}")
    r = t.run(" ".join(args.command), timeout=args.timeout)
    if r.stdout:
        log.out(r.stdout.rstrip())
    if r.stderr.strip():
        print(r.stderr.rstrip(), file=sys.stderr)
    return r.rc


def cmd_push(args, cfg):
    t = _transport(args, cfg)
    local = Path(args.local).expanduser()
    if not local.is_file():
        log.err(f"no such file: {local}")
        return 2
    est = max(5, local.stat().st_size / (60000 if t.name == "tmux" else 2_000_000))
    with spinner.spin(f"push {local.name} via {t.name}", est):
        t.push(local, args.remote)
    log.out({"pushed": str(local), "to": args.remote, "bytes": local.stat().st_size})
    return 0


def cmd_pull(args, cfg):
    t = _transport(args, cfg)
    local = Path(args.local).expanduser()
    with spinner.spin(f"pull {args.remote} via {t.name}", 30):
        t.pull(args.remote, local)
    log.out({"pulled": args.remote, "to": str(local), "bytes": local.stat().st_size})
    return 0


def cmd_premiere(args, cfg):
    t = _transport(args, cfg)
    p = Premiere(t, cfg.remote)

    if args.action == "heal":
        with spinner.spin("loading core.jsx + helpers", 20):
            res = p.heal()
        missing = [k for k, v in res.items() if v != "function"]
        if missing:
            log.warn(f"not installed: {', '.join(missing)}")
        log.out(res)
        return 0 if not missing else 1

    if args.action == "open":
        target = args.project or cfg.remote.project_path
        with spinner.spin(f"opening {target.rsplit('/', 1)[-1]}", 30):
            res = p.open_project(target)
        (log.ok if res.get("opened") else log.step)(
            f"{res.get('name')} {'opened' if res.get('opened') else 'already open'}")
        log.out(res)
        return 0

    if args.action == "presets":
        found = p.list_presets(args.filter or "1080")
        for f in found:
            log.step(f.rsplit("/", 1)[-1])
        log.out(found)
        return 0

    if args.action == "sequence":
        name = args.name or "umcares_edit"
        preset = args.preset or cfg.remote.sequence_preset
        if not preset and not args.match:
            log.err("give --preset (recommended) or --match <clip>")
            log.warn("without either, Premiere opens a modal dialog and hangs")
            log.warn("list options: umcares premiere presets")
            return 2
        with spinner.spin(f"creating sequence '{name}'", 30):
            res = p.create_sequence(name, preset=preset, match_clip=args.match or "")
        want_fps = args.fps or 0
        if want_fps and abs(res.get("fps", 0) - want_fps) > 0.01:
            log.warn(f"sequence is {res.get('fps')}fps but you asked for {want_fps} — "
                     f"createNewSequenceFromClips does not inherit frame rate; "
                     f"use an explicit --preset")
        log.ok(f"{res['name']}: {res['width']}x{res['height']} @ {res['fps']}fps")
        log.out(res)
        return 0

    if args.action == "status":
        try:
            log.out(p.ping())
        except RuntimeError as e:
            log.err(str(e))
            log.warn("try: umcares premiere heal")
            return 1
        return 0

    if args.action == "report":
        log.out(p.report())
        return 0

    if args.action == "import":
        if not args.files:
            log.err("give at least one remote path with --files")
            return 2
        with spinner.spin(f"importing {len(args.files)} file(s)", 5 * len(args.files)):
            n = p.import_files(args.files)
        log.out({"imported": n, "requested": len(args.files)})
        return 0 if n == len(args.files) else 1

    if args.action == "build":
        plan = json.loads(Path(args.plan).expanduser().read_text())
        with spinner.spin(f"building timeline ({len(plan.get('video', []))} clips)", 90):
            res = p.build(plan)
        if res.get("failures"):
            for f in res["failures"]:
                log.err(f)
        if res.get("gaps"):
            log.warn(f"gaps remain: {', '.join(res['gaps'])}")
        else:
            log.ok("no gaps")
        log.out(res)
        return 1 if res.get("failures") else 0

    if args.action == "export":
        out = args.out or f"{cfg.remote.exports}/master.mxf"
        preset = args.preset or PRESET_1080P50
        t.run(f"mkdir -p {cfg.remote.exports}", timeout=60)
        with spinner.spin("exporting master from Premiere", 450):
            res = p.export(out, preset, timeout=args.timeout)
        log.out(res)
        return 0

    log.err(f"unknown action {args.action}")
    return 2


def cmd_media(args, cfg):
    t = _transport(args, cfg)

    if args.action == "probe":
        res = media.probe(t, args.dir)
        bad = res.get("needs_transcode") or []
        if bad:
            log.warn(f"{len(bad)} file(s) NOT H.264 — Premiere would import these "
                     f"as audio-only with no error: {', '.join(bad[:6])}"
                     + (" …" if len(bad) > 6 else ""))
            log.warn("run: umcares media prepare")
        else:
            log.ok("all video is H.264")
        log.out(res)
        return 0

    if args.action == "prepare":
        dest = args.dest or cfg.remote.edit_ready
        with spinner.spin(f"transcoding -> {dest}", 600):
            res = media.prepare(t, args.dir, dest)
        log.out(res)
        return 0

    if args.action == "kenburns":
        if len(args.photos) < 2:
            log.err("need at least 2 photos")
            return 2
        with spinner.spin(f"ken burns {len(args.photos)} stills", 40 * len(args.photos)):
            res = media.kenburns(t, args.out, args.photos, args.seconds)
        log.out(res)
        return 0

    if args.action == "denoise":
        if not args.file or not args.out:
            log.err("need --file and --out")
            return 2
        with spinner.spin("cleaning speech", 60):
            res = media.denoise(t, args.file, args.out,
                                target_lufs=args.lufs, nr=args.nr)
        log.ok(f"{res.get('before')} -> {res.get('after')}")
        log.out(res)
        return 0

    if args.action == "extract-audio":
        if not args.file or not args.out:
            log.err("need --file and --out")
            return 2
        with spinner.spin("extracting audio", 30):
            res = media.extract_audio(t, args.file, args.out)
        log.out(res)
        return 0

    if args.action == "levels":
        windows = []
        for spec in args.window:
            parts = spec.split(":")
            if len(parts) != 3:
                log.err(f"bad --window '{spec}', expected label:start:duration")
                return 2
            windows.append((parts[0], float(parts[1]), float(parts[2])))
        log.out(media.levels(t, args.file, windows))
        return 0

    log.err(f"unknown action {args.action}")
    return 2


def cmd_post(args, cfg):
    t = _transport(args, cfg)

    if args.action == "mix":
        sections = None
        if args.sections:
            sections = json.loads(Path(args.sections).expanduser().read_text())
        with spinner.spin("mixing music + subtitles", 300):
            res = post.mix(t, args.master, args.music, args.srt, args.out,
                           sections=sections, timeout=args.timeout)
        log.out(res)
        return 0

    if args.action == "subs":
        with spinner.spin("replacing subtitle track", 45):
            res = post.mux_subtitles_only(t, args.src, args.srt, args.out)
        log.out(res)
        return 0

    if args.action == "verify-subs":
        res = post.verify_subtitles(t, args.file)
        (log.ok if res.get("has_subtitles") else log.err)(
            f"subtitles: {res.get('cues', 0)} cues")
        log.out(res)
        return 0

    log.err(f"unknown action {args.action}")
    return 2


# -- parser -----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="umcares",
        description="Drive the remote Adobe Premiere video pipeline.",
        epilog="Start with:  umcares session    then:  umcares doctor",
    )
    p.add_argument("--transport", choices=["auto", "ssh", "tmux"], default="auto",
                   help="how to reach the remote (default: auto = ssh, else tmux pane)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version",
                   version=f"umcares {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("session", help="create the 3-pane tmux layout")
    s.add_argument("--editor", default="nvim")
    s.add_argument("--force", action="store_true", help="kill and recreate")
    s.add_argument("--no-attach", action="store_true")
    s.add_argument("--status", action="store_true", help="describe, do not change")
    s.add_argument("--reconnect", action="store_true",
                   help="re-open ssh in the remote pane after it drops")
    s.set_defaults(func=cmd_session)

    rn = sub.add_parser("render", help="render a recipe into a video")
    rn.add_argument("--file", required=True, help="recipe .json or .yml")
    rn.add_argument("--from", dest="from_stage", choices=render_mod.STAGES)
    rn.add_argument("--to", dest="to_stage", choices=render_mod.STAGES)
    rn.add_argument("--only", nargs="*", choices=render_mod.STAGES)
    rn.add_argument("--force", action="store_true",
                    help="regenerate even if outputs already exist")
    rn.add_argument("--dry-run", action="store_true")
    rn.set_defaults(func=cmd_render)

    ins = sub.add_parser("inspect", help="probe media + contact sheets so an AI can see it")
    ins.add_argument("--dir", nargs="*",
                     help="remote media dirs (default: edit_ready, photos, music)")
    ins.add_argument("--cols", type=int, default=4)
    ins.add_argument("--fast", action="store_true", help="skip loudness analysis")
    ins.add_argument("--no-sheets", action="store_true")
    ins.set_defaults(func=cmd_inspect)

    rc = sub.add_parser("recipe", help="validate / resolve a recipe")
    rc.add_argument("action", choices=["example", "validate", "resolve"])
    rc.add_argument("--file")
    rc.add_argument("--out")
    rc.set_defaults(func=cmd_recipe)

    i = sub.add_parser("init", help="create the project folder tree")
    i.add_argument("--local-only", action="store_true")
    i.add_argument("--remote-only", action="store_true")
    i.add_argument("--plan", action="store_true", help="show what would be created")
    i.set_defaults(func=cmd_init)

    st = sub.add_parser("stack", help="MCP backend containers on the remote")
    st.add_argument("action",
                    choices=["check", "up", "down", "status", "logs", "stdio"])
    st.add_argument("--build", action="store_true",
                    help="rebuild images (first run is slow: Go+Rust+Python+Node)")
    st.add_argument("--stop-host", action="store_true",
                    help="stop host-run backends first so ports are free")
    st.add_argument("--force", action="store_true", help="ignore port conflicts")
    st.add_argument("--services", nargs="*", default=[])
    st.add_argument("--service", help="one service, for logs")
    st.add_argument("--tail", type=int, default=60)
    st.add_argument("--volumes", action="store_true")
    st.set_defaults(func=cmd_stack)

    a = sub.add_parser("auth", help="ssh credentials: status or key setup")
    a.add_argument("--setup-key", action="store_true",
                   help="generate a dedicated key and install it on the remote")
    a.set_defaults(func=cmd_auth)

    c = sub.add_parser("config", help="read/write settings and credentials")
    c.add_argument("action",
                   choices=["init", "sections", "set", "get", "list", "show"],
                   help="init = interactive wizard over every setting")
    c.add_argument("--section", action="append",
                   help="wizard: configure only this section (repeatable)")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    c.add_argument("--reveal", action="store_true", help="print secrets unmasked")
    c.add_argument("--encrypt", action="store_true",
                   help="encrypt .env via dotenvx before writing (opt-in for now)")
    c.set_defaults(func=cmd_config)

    se = sub.add_parser("secrets", help="encrypted .env via dotenvx")
    se.add_argument("action",
                    choices=["status", "init", "rotate", "encrypt", "decrypt"])
    se.set_defaults(func=cmd_secrets)

    d = sub.add_parser("doctor", help="preflight every dependency")
    d.add_argument("--json", action="store_true")
    d.add_argument("--quick", action="store_true", help="skip remote checks")
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("remote", help="run a command on the Adobe machine")
    r.add_argument("command", nargs=argparse.REMAINDER)
    r.add_argument("--timeout", type=int, default=300)
    r.set_defaults(func=cmd_remote)

    pu = sub.add_parser("push", help="copy a file to the remote (verified)")
    pu.add_argument("local")
    pu.add_argument("remote")
    pu.set_defaults(func=cmd_push)

    pl = sub.add_parser("pull", help="copy a file from the remote (verified)")
    pl.add_argument("remote")
    pl.add_argument("local")
    pl.set_defaults(func=cmd_pull)

    pr = sub.add_parser("premiere", help="control Premiere Pro")
    pr.add_argument("action",
                    choices=["heal", "status", "report", "open", "presets",
                             "sequence", "import", "build", "export"])
    pr.add_argument("--name", help="sequence name")
    pr.add_argument("--match", help="clip to match when no preset is given")
    pr.add_argument("--filter", help="substring filter for `presets`")
    pr.add_argument("--fps", type=float, help="expected fps; warns on mismatch")
    pr.add_argument("--project", help="path to a .prproj (default: config)")
    pr.add_argument("--files", nargs="*", default=[], help="remote paths to import")
    pr.add_argument("--plan", help="timeline plan JSON (for build)")
    pr.add_argument("--out", help="output path (for export)")
    pr.add_argument("--preset", help=".epr path (for export)")
    pr.add_argument("--timeout", type=int, default=1800)
    pr.set_defaults(func=cmd_premiere)

    m = sub.add_parser("media", help="probe / transcode / ken burns")
    m.add_argument("action", choices=["probe", "prepare", "kenburns", "levels",
                                      "denoise", "extract-audio"])
    m.add_argument("--lufs", type=float, default=-16.0)
    m.add_argument("--nr", type=int, default=20, help="denoise strength")
    m.add_argument("--dir", help="remote source directory")
    m.add_argument("--dest", help="remote output directory")
    m.add_argument("--out", help="output file")
    m.add_argument("--photos", nargs="*", default=[])
    m.add_argument("--seconds", type=float, default=8.0)
    m.add_argument("--file", help="media file to measure")
    m.add_argument("--window", nargs="*", default=[],
                   help="label:start:duration (repeatable)")
    m.set_defaults(func=cmd_media)

    po = sub.add_parser("post", help="music mix, subtitles, delivery")
    po.add_argument("action", choices=["mix", "subs", "verify-subs"])
    po.add_argument("--master")
    po.add_argument("--music")
    po.add_argument("--srt")
    po.add_argument("--src")
    po.add_argument("--out")
    po.add_argument("--file")
    po.add_argument("--sections", help="JSON file: [[until_seconds, dB], ...]")
    po.add_argument("--timeout", type=int, default=3600)
    po.set_defaults(func=cmd_post)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    log.set_verbose(args.verbose)
    cfg = Config.load()
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        log.err("interrupted")
        return 130
    except SystemExit:
        raise
    except Exception as e:            # never dump a traceback at a user
        log.err(str(e))
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
