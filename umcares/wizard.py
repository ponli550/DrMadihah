"""The interactive configuration wizard.

`umcares config init` walks every setting the pipeline has, in sections, and
writes the answers to `.env`.

Three rules it follows, each because of a specific way config goes wrong:

  * **Nothing is written until the end.** Every answer is held in memory and
    shown as a diff first. A wizard that saves as it goes leaves a half-configured
    machine behind when someone hits Ctrl-C on question nine.
  * **Enter keeps the current value.** Re-running to change one setting must not
    mean re-typing the other forty-three.
  * **Only what changed gets written.** Answering a prompt with the same value
    it already had leaves `.env` untouched, so the file records decisions rather
    than a snapshot of the defaults.

Values that a live system can answer are fetched rather than typed: sequence and
export presets come from the Premiere install, so a preset that does not exist
cannot be entered.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import log, secrets

# --------------------------------------------------------------- the fields --
@dataclass
class Field:
    key: str                     # the .env variable
    label: str
    kind: str = "text"           # text | int | float | bool | choice | list | secret
    choices: tuple = ()
    help: str = ""
    unit: str = ""
    lookup: str = ""             # a live source: "sequence_presets" | "export_presets"


SECTIONS: dict[str, tuple[str, list]] = {
    "connection": ("Remote machine and how to reach it", [
        Field("UMC_REMOTE_HOST", "Remote host", help="the Mac running Premiere"),
        Field("UMC_REMOTE_USER", "Remote user"),
        Field("UMC_SSH_ALIAS", "SSH alias", help="an entry in ~/.ssh/config, if any"),
        Field("UMC_SSH_KEY", "SSH key path"),
        Field("UMC_TMUX_SESSION", "tmux session"),
        Field("UMC_TMUX_PANE", "tmux pane id",
              help="blank = auto-detect the ssh pane"),
        Field("UMC_CDP_PORT", "CEP debug port", "int",
              help="the Premiere panel's Chrome DevTools port"),
    ]),
    "paths": ("Where the media and the project live", [
        Field("UMC_REMOTE_ROOT", "Remote project root",
              help="assets/, exports/, subtitles/ hang off this"),
        Field("UMC_PROJECT", "Premiere project (.prproj)",
              help="blank = whatever is already open"),
        Field("UMC_MCP_REPO", "MCP bridge repo on the remote"),
    ]),
    "sequence": ("Video sequence: format, timing, presets", [
        Field("UMC_FPS", "Frame rate", "float", unit="fps"),
        Field("UMC_WIDTH", "Width", "int", unit="px"),
        Field("UMC_HEIGHT", "Height", "int", unit="px"),
        Field("UMC_SEQUENCE_PRESET", "Sequence preset", lookup="sequence_presets",
              help="the .sqpreset used when creating a sequence"),
        Field("UMC_PRESET", "Export preset", lookup="export_presets",
              help="the .epr Premiere renders the master with"),
        Field("UMC_NARRATION_LEAD", "Narration lead", "float", unit="s",
              help="how long a visual is up before its voice starts"),
        Field("UMC_SCENE_PAD", "Scene pad", "float", unit="s",
              help="how long it holds after the voice ends"),
    ]),
    "subtitles": ("Subtitles", [
        Field("UMC_SUBS_GENERATE", "Generate subtitles", "bool"),
        Field("UMC_SUBS_MODE", "Subtitle mode", "choice", ("soft", "burn"),
              help="soft = a selectable track; burn = painted into the picture"),
        Field("UMC_SUBS_LANG", "Language tag",
              help="ISO-639-2, e.g. msa for Malay, eng for English"),
        Field("UMC_SUBS_MAX_CHARS", "Max characters per cue", "int",
              help="beyond ~62 the line outruns the time it is on screen"),
        Field("UMC_SUBS_MIN_SILENCE", "Cue split silence", "float", unit="s",
              help="a gap this long ends a cue; lower = more, shorter cues"),
    ]),
    "audio": ("Audio levels and music", [
        Field("UMC_TARGET_LUFS", "Loudness target", "float", unit="LUFS",
              help="-16 is the usual target for online delivery"),
        Field("UMC_KEEP_DB", "Clip audio: keep", "float", unit="dB",
              help="a clip whose own sound leads, e.g. a testimonial"),
        Field("UMC_DUCK_DB", "Clip audio: duck", "float", unit="dB",
              help="audible under the narration"),
        Field("UMC_MUTE_DB", "Clip audio: mute", "float", unit="dB",
              help="silent but still on the timeline, so sync survives"),
        Field("UMC_MUSIC_START", "Music starts at", "float", unit="s",
              help="music from 0 competes with the opening card"),
        Field("UMC_MUSIC_DB", "Music bed level", "float", unit="dB"),
    ]),
    "voice": ("Narration voice", [
        Field("UMC_VOICE_NAME", "Azure voice"),
        Field("UMC_VOICE_PITCH", "Pitch", help="e.g. 0%, +6%, -4%"),
        Field("UMC_VOICE_RATE", "Rate", help="e.g. 0%, -10%"),
        Field("UMC_VOICE_FILLERS", "Filler sounds between sentences", "bool",
              help="'errr', 'aaa' — more conversational, less authoritative"),
        Field("UMC_VOICE_ENGLISH", "English loanwords", "list",
              help="read with English phonetics, comma separated"),
        Field("UMC_VOICE_ACRONYMS", "Acronyms", "list",
              help="spelled out letter by letter, comma separated"),
    ]),
    "style": ("Card look", [
        Field("UMC_STYLE_SURFACE", "Background"),
        Field("UMC_STYLE_INK", "Primary text"),
        Field("UMC_STYLE_MUTED", "Secondary text"),
        Field("UMC_STYLE_ACCENT", "Accent",
              help="eyebrows and highlights — never used to carry data"),
        Field("UMC_STYLE_FONT", "Font family"),
    ]),
    "keys": ("API credentials", [
        Field("JSON2VIDEO_API_KEY", "json2video API key", "secret",
              help="renders the narration and the cards"),
        Field("UMC_SSH_PASSWORD", "SSH password", "secret",
              help="leave blank if key auth works — it should"),
    ]),
}


# ------------------------------------------------------------------ prompts --
class Abort(Exception):
    pass


def _ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    try:
        got = input(f"    {prompt}{shown}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise Abort()
    return got or default


def _parse(field: Field, raw: str, default: str) -> str:
    """Validate one answer, returning the string to store."""
    raw = raw.strip()
    if field.kind in ("int", "float"):
        if not raw:
            return ""
        try:
            (int if field.kind == "int" else float)(raw)
        except ValueError:
            raise ValueError(f"{raw!r} is not a number")
        return raw
    if field.kind == "bool":
        v = raw.lower()
        if v in ("y", "yes", "true", "1", "on"):
            return "true"
        if v in ("n", "no", "false", "0", "off"):
            return "false"
        raise ValueError("answer yes or no")
    if field.kind == "choice" and raw and raw not in field.choices:
        raise ValueError(f"pick one of {', '.join(field.choices)}")
    if field.kind == "list":
        return ", ".join(x.strip() for x in raw.split(",") if x.strip())
    return raw


def _prompt_field(field: Field, current: str, options: list | None) -> str:
    if field.help:
        log.step(f"  {field.help}")
    if options:
        for i, opt in enumerate(options[:20], 1):
            log.step(f"    {i:>2}. {opt}")
        if len(options) > 20:
            log.step(f"    ... and {len(options) - 20} more (type a path instead)")

    label = field.label
    if field.kind == "choice":
        label += f" ({'/'.join(field.choices)})"
    elif field.kind == "bool":
        label += " (y/n)"
    elif field.unit:
        label += f" ({field.unit})"

    while True:
        shown = secrets.mask(field.key, current) if field.kind == "secret" else current
        raw = _ask(label, shown)
        # the masked value came back untouched -> the real one is unchanged
        if field.kind == "secret" and raw == shown:
            return current
        if options and raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        try:
            return _parse(field, raw, current)
        except ValueError as e:
            log.err(f"  {e}")


# ------------------------------------------------------------ live lookups --
def _lookups(cfg, want: set) -> dict:
    """Fetch preset lists from the remote. Never fatal — typing still works."""
    if not want:
        return {}
    found: dict = {}
    try:
        from .premiere import Premiere
        from .transport import connect
        log.step("  asking the remote for presets...")
        t = connect(cfg.remote)
        p = Premiere(t, cfg.remote)
        if "sequence_presets" in want:
            found["sequence_presets"] = p.list_presets()[:60]
        if "export_presets" in want:
            found["export_presets"] = p.list_export_presets()[:60]
    except Exception as e:
        log.warn(f"  could not read presets from the remote ({e}) — type a path")
    return {k: v for k, v in found.items() if v}


# --------------------------------------------------------------------- run --
def run(cfg, only: list | None = None, encrypt: bool = False) -> int:
    if not sys.stdin.isatty():
        log.err("config init needs a terminal. "
                "Non-interactive? use `umcares config set KEY VALUE`.")
        return 2

    sections = {k: v for k, v in SECTIONS.items() if not only or k in (only or [])}
    if only:
        unknown = [s for s in only if s not in SECTIONS]
        if unknown:
            log.err(f"unknown section(s): {', '.join(unknown)}")
            log.step(f"available: {', '.join(SECTIONS)}")
            return 2

    log.info("umcares configuration")
    log.step("  Enter keeps the current value. Ctrl-C aborts without writing.")

    wanted = {f.lookup for _, fields in sections.values()
              for f in fields if f.lookup}
    live = _lookups(cfg, wanted)

    # a setting nobody wrote still has a value in force; `_default_for` offers
    # that one, so Enter keeps what is working instead of blanking it
    current = {k: str(v) for k, v in cfg.env.items()}
    answers: dict[str, str] = {}

    try:
        for name, (title, fields) in sections.items():
            log.info(f"{name} — {title}")
            for f in fields:
                builtin = _default_for(cfg, f.key)
                cur = current.get(f.key, builtin)
                val = _prompt_field(f, cur, live.get(f.lookup))
                if val == current.get(f.key, ""):
                    continue                       # unchanged
                if f.key not in current and val == builtin:
                    continue                       # Enter on a default: keep it
                                                   # implicit rather than pinning
                                                   # 44 defaults into .env
                answers[f.key] = val
    except Abort:
        print()
        log.warn("aborted — nothing was written")
        return 130

    if not answers:
        log.ok("no changes")
        return 0

    log.info(f"{len(answers)} change(s)")
    for k, v in answers.items():
        old = current.get(k, "")
        show = (lambda x: secrets.mask(k, x) if x else "(unset)")
        log.step(f"  {k}: {show(old)} -> {show(v)}")

    try:
        if _ask("write these to .env? (y/n)", "y").lower() not in ("y", "yes"):
            log.warn("not written")
            return 1
    except Abort:
        print()
        log.warn("aborted — nothing was written")
        return 130

    env_path = cfg.root / ".env"
    for k, v in answers.items():
        secrets.set_value(env_path, k, v, encrypt_secrets=encrypt)
    log.ok(f"wrote {len(answers)} setting(s) to {env_path}")
    log.out({"written": sorted(answers), "file": str(env_path)})
    return 0


def _default_for(cfg, key: str) -> str:
    """The value in force right now, so Enter keeps it rather than blanking it."""
    r, q, b, a, v, y = (cfg.remote, cfg.sequence, cfg.subtitles, cfg.audio,
                        cfg.voice, cfg.style)
    table = {
        "UMC_REMOTE_HOST": r.host, "UMC_REMOTE_USER": r.user,
        "UMC_SSH_ALIAS": r.ssh_alias, "UMC_SSH_KEY": r.key_path,
        "UMC_TMUX_SESSION": r.tmux_session, "UMC_TMUX_PANE": r.tmux_pane,
        "UMC_CDP_PORT": str(r.cdp_port), "UMC_REMOTE_ROOT": r.root,
        "UMC_PROJECT": r.project, "UMC_MCP_REPO": r.mcp_repo,
        "UMC_PRESET": r.preset, "UMC_SEQUENCE_PRESET": r.sequence_preset,
        "UMC_FPS": str(q.fps), "UMC_WIDTH": str(q.width),
        "UMC_HEIGHT": str(q.height), "UMC_SCENE_PAD": str(q.scene_pad),
        "UMC_NARRATION_LEAD": str(q.narration_lead),
        "UMC_SUBS_GENERATE": str(b.generate).lower(), "UMC_SUBS_MODE": b.mode,
        "UMC_SUBS_LANG": b.language, "UMC_SUBS_MAX_CHARS": str(b.max_chars),
        "UMC_SUBS_MIN_SILENCE": str(b.min_silence),
        "UMC_TARGET_LUFS": str(a.target_lufs), "UMC_KEEP_DB": str(a.keep_db),
        "UMC_DUCK_DB": str(a.duck_db), "UMC_MUTE_DB": str(a.mute_db),
        "UMC_MUSIC_START": str(a.music_start), "UMC_MUSIC_DB": str(a.music_db),
        "UMC_VOICE_NAME": v.name, "UMC_VOICE_PITCH": v.pitch,
        "UMC_VOICE_RATE": v.rate, "UMC_VOICE_FILLERS": str(v.fillers).lower(),
        "UMC_VOICE_ENGLISH": ", ".join(v.english_terms),
        "UMC_VOICE_ACRONYMS": ", ".join(v.acronyms),
        "UMC_STYLE_SURFACE": y.surface, "UMC_STYLE_INK": y.ink,
        "UMC_STYLE_MUTED": y.muted, "UMC_STYLE_ACCENT": y.accent,
        "UMC_STYLE_FONT": y.font,
    }
    return table.get(key, "")
