"""umcares — drive the remote Adobe Premiere video pipeline from one CLI.

Human progress goes to stderr, results go to stdout, so this composes:

    umcares media probe --dir ~/…/Photos | jq '.needs_transcode'
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import auth, doctor, log, media, post, scaffold, secrets, session, spinner, stack
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
        log.out(auth.setup_key(cfg.remote))
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
        est = 900 if args.build else 90
        with spinner.spin("starting MCP backend containers"
                          + (" (building — first run is slow)" if args.build else ""), est):
            res = stack.up(t, cfg.remote, build=args.build, services=args.services)
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
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("session", help="create the 3-pane tmux layout")
    s.add_argument("--editor", default="nvim")
    s.add_argument("--force", action="store_true", help="kill and recreate")
    s.add_argument("--no-attach", action="store_true")
    s.add_argument("--status", action="store_true", help="describe, do not change")
    s.add_argument("--reconnect", action="store_true",
                   help="re-open ssh in the remote pane after it drops")
    s.set_defaults(func=cmd_session)

    i = sub.add_parser("init", help="create the project folder tree")
    i.add_argument("--local-only", action="store_true")
    i.add_argument("--remote-only", action="store_true")
    i.add_argument("--plan", action="store_true", help="show what would be created")
    i.set_defaults(func=cmd_init)

    st = sub.add_parser("stack", help="MCP backend containers on the remote")
    st.add_argument("action",
                    choices=["check", "up", "down", "status", "logs", "stdio"])
    st.add_argument("--build", action="store_true", help="rebuild images")
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
    c.add_argument("action", choices=["set", "get", "list"])
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
                    choices=["heal", "status", "report", "import", "build", "export"])
    pr.add_argument("--files", nargs="*", default=[], help="remote paths to import")
    pr.add_argument("--plan", help="timeline plan JSON (for build)")
    pr.add_argument("--out", help="output path (for export)")
    pr.add_argument("--preset", help=".epr path (for export)")
    pr.add_argument("--timeout", type=int, default=1800)
    pr.set_defaults(func=cmd_premiere)

    m = sub.add_parser("media", help="probe / transcode / ken burns")
    m.add_argument("action", choices=["probe", "prepare", "kenburns", "levels"])
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
