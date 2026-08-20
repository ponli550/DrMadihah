"""Configuration: .env secrets plus remote/project paths.

Everything is overridable by environment variable so the CLI works on another
machine (or another operator's remote) without editing code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> dict:
    """Parse .env, decrypting first if it is a dotenvx-encrypted file.

    Encrypted and plaintext .env files are handled identically by callers;
    the difference is invisible above this function.
    """
    path = path or (ROOT / ".env")
    env: dict[str, str] = {}
    if not path.exists():
        return env

    from . import secrets as _secrets
    if _secrets.is_encrypted(path):
        return _secrets.load_encrypted(path)

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _get(env: dict, key: str, default: str = "") -> str:
    # real environment wins over .env, so `KEY=x umcares ...` works
    return os.environ.get(key) or env.get(key) or default


@dataclass
class Remote:
    """Where the Adobe machine is and how to reach it."""
    host: str = "dsaopjfs-macbook-air.taile5a4c9.ts.net"
    user: str = "irpan"
    ssh_alias: str = "personal"
    tmux_session: str = ROOT.name        # defaults to the project dir name
    tmux_pane: str = ""          # explicit pane id like '%3'; else auto-detect
    cdp_port: int = 9241         # CEP panel Chrome DevTools port
    root: str = "/Users/irpan/Projects/DrMadihah"
    mcp_repo: str = "/Users/irpan/Projects/personal/VD/AdobePremiereProMCP"
    password: str = ""           # optional; key auth is strongly preferred
    key_path: str = "~/.ssh/id_ed25519_umcares"
    ssh_mux: bool = True         # reuse one ssh connection for every command
    ssh_persist: str = "10m"     # how long the master lingers after the last use

    # Adobe install + project. Derived from `user` unless overridden, so
    # pointing the CLI at a different machine or a scratch project is config,
    # never a code edit.
    premiere_app: str = "Adobe Premiere Pro 2026"
    cep_ext_id: str = "com.premierpro.mcp.bridge"
    project: str = ""            # .prproj to open; empty = whatever is open
    preset: str = ""             # export .epr; empty = the 1080p50 default
    sequence_preset: str = ""    # .sqpreset used when creating a sequence

    @property
    def home(self) -> str:
        return f"/Users/{self.user}"

    @property
    def cep_ext_dir(self) -> str:
        return (f"{self.home}/Library/Application Support/Adobe/CEP/"
                f"extensions/{self.cep_ext_id}")

    @property
    def app_dir(self) -> str:
        return f"/Applications/{self.premiere_app}/{self.premiere_app}.app"

    @property
    def preset_path(self) -> str:
        if self.preset:
            return self.preset
        return (f"{self.app_dir}/Contents/Settings/EncoderPresets/"
                f"ConsolidateAndTranscode/AVC-Intra Class100 1080 50p.epr")

    @property
    def project_path(self) -> str:
        return self.project or f"{self.root}/project/project.prproj"

    @property
    def assets(self) -> str:
        return f"{self.root}/assets"

    @property
    def exports(self) -> str:
        return f"{self.root}/exports"

    @property
    def edit_ready(self) -> str:
        return f"{self.assets}/edit_ready"


@dataclass
class Sequence:
    """How the timeline itself is built."""
    fps: float = 50.0
    width: int = 1920
    height: int = 1080
    scene_pad: float = 0.5        # breathing room after a scene's narration
    narration_lead: float = 0.5   # visual starts before the voice does


@dataclass
class Subtitles:
    generate: bool = True
    language: str = "msa"         # ISO-639-2, what the mp4 track is tagged with
    max_chars: int = 62           # a longer line outruns the time it is on screen
    mode: str = "soft"            # soft = selectable track, burn = pixels
    min_silence: float = 0.16     # gap that separates one cue from the next


@dataclass
class Audio:
    target_lufs: float = -16.0    # broadcast-ish; loud enough on a phone speaker
    keep_db: float = 0.0          # a clip whose own audio leads
    duck_db: float = -18.0        # present, but under the narration
    mute_db: float = -60.0        # effectively silent (not removed, so it re-syncs)
    music_start: float = 25.0     # music entering at 0 fights the opening card
    music_db: float = -22.0


@dataclass
class Voice:
    name: str = "ms-MY-OsmanNeural"
    pitch: str = "0%"
    rate: str = "0%"
    fillers: bool = False         # "errr"/"aaa" between sentences
    english_terms: list = field(default_factory=lambda: ["scam siber"])
    acronyms: list = field(default_factory=lambda: ["UM Cares"])


@dataclass
class Style:
    surface: str = "#0d2847"
    ink: str = "#ffffff"
    muted: str = "#a9b6c9"
    accent: str = "#ffd166"
    font: str = "Inter"


@dataclass
class Config:
    env: dict = field(default_factory=dict)
    remote: Remote = field(default_factory=Remote)
    sequence: Sequence = field(default_factory=Sequence)
    subtitles: Subtitles = field(default_factory=Subtitles)
    audio: Audio = field(default_factory=Audio)
    voice: Voice = field(default_factory=Voice)
    style: Style = field(default_factory=Style)
    root: Path = ROOT

    @classmethod
    def load(cls) -> "Config":
        env = load_env()
        r = Remote(
            host=_get(env, "UMC_REMOTE_HOST", Remote.host),
            user=_get(env, "UMC_REMOTE_USER", Remote.user),
            ssh_alias=_get(env, "UMC_SSH_ALIAS", Remote.ssh_alias),
            tmux_session=_get(env, "UMC_TMUX_SESSION", Remote.tmux_session),
            tmux_pane=_get(env, "UMC_TMUX_PANE", ""),
            cdp_port=int(_get(env, "UMC_CDP_PORT", str(Remote.cdp_port))),
            root=_get(env, "UMC_REMOTE_ROOT", Remote.root),
            mcp_repo=_get(env, "UMC_MCP_REPO", Remote.mcp_repo),
            password=_get(env, "UMC_SSH_PASSWORD", ""),
            key_path=_get(env, "UMC_SSH_KEY", Remote.key_path),
            ssh_mux=_get(env, "UMC_SSH_MUX", "").strip().lower()
                    not in ("0", "false", "no", "off"),
            ssh_persist=_get(env, "UMC_SSH_PERSIST", Remote.ssh_persist),
            premiere_app=_get(env, "UMC_PREMIERE_APP", Remote.premiere_app),
            cep_ext_id=_get(env, "UMC_CEP_EXT_ID", Remote.cep_ext_id),
            project=_get(env, "UMC_PROJECT", ""),
            preset=_get(env, "UMC_PRESET", ""),
            sequence_preset=_get(env, "UMC_SEQUENCE_PRESET", ""),
        )
        def num(key, dflt):
            try:
                return type(dflt)(_get(env, key, str(dflt)))
            except ValueError:
                return dflt

        def flag(key, dflt):
            v = _get(env, key, "").strip().lower()
            return dflt if not v else v in ("1", "true", "yes", "on")

        def items(key, dflt):
            v = _get(env, key, "").strip()
            return [x.strip() for x in v.split(",") if x.strip()] if v else list(dflt)

        seq = Sequence(
            fps=num("UMC_FPS", Sequence.fps),
            width=num("UMC_WIDTH", Sequence.width),
            height=num("UMC_HEIGHT", Sequence.height),
            scene_pad=num("UMC_SCENE_PAD", Sequence.scene_pad),
            narration_lead=num("UMC_NARRATION_LEAD", Sequence.narration_lead))
        subs = Subtitles(
            generate=flag("UMC_SUBS_GENERATE", Subtitles.generate),
            language=_get(env, "UMC_SUBS_LANG", Subtitles.language),
            max_chars=num("UMC_SUBS_MAX_CHARS", Subtitles.max_chars),
            mode=_get(env, "UMC_SUBS_MODE", Subtitles.mode),
            min_silence=num("UMC_SUBS_MIN_SILENCE", Subtitles.min_silence))
        aud = Audio(
            target_lufs=num("UMC_TARGET_LUFS", Audio.target_lufs),
            keep_db=num("UMC_KEEP_DB", Audio.keep_db),
            duck_db=num("UMC_DUCK_DB", Audio.duck_db),
            mute_db=num("UMC_MUTE_DB", Audio.mute_db),
            music_start=num("UMC_MUSIC_START", Audio.music_start),
            music_db=num("UMC_MUSIC_DB", Audio.music_db))
        vo = Voice(
            name=_get(env, "UMC_VOICE_NAME", Voice.name),
            pitch=_get(env, "UMC_VOICE_PITCH", Voice.pitch),
            rate=_get(env, "UMC_VOICE_RATE", Voice.rate),
            fillers=flag("UMC_VOICE_FILLERS", Voice.fillers),
            english_terms=items("UMC_VOICE_ENGLISH", ["scam siber"]),
            acronyms=items("UMC_VOICE_ACRONYMS", ["UM Cares"]))
        st = Style(
            surface=_get(env, "UMC_STYLE_SURFACE", Style.surface),
            ink=_get(env, "UMC_STYLE_INK", Style.ink),
            muted=_get(env, "UMC_STYLE_MUTED", Style.muted),
            accent=_get(env, "UMC_STYLE_ACCENT", Style.accent),
            font=_get(env, "UMC_STYLE_FONT", Style.font))
        return cls(env=env, remote=r, sequence=seq, subtitles=subs,
                   audio=aud, voice=vo, style=st)

    # -- recipe defaults ----------------------------------------------------
    def defaults(self) -> dict:
        """Config as recipe-shaped blocks.

        A recipe always wins over these — the config is what applies when the
        recipe stays silent, so a per-video override never needs a config edit
        and a house style never needs repeating in every recipe.
        """
        s, sub, a, v, y = (self.sequence, self.subtitles, self.audio,
                           self.voice, self.style)
        return {
            "meta": {"fps": s.fps, "scene_pad": s.scene_pad,
                     "narration_lead": s.narration_lead,
                     "width": s.width, "height": s.height},
            "subtitles": {"generate": sub.generate, "language": sub.language,
                          "max_chars": sub.max_chars, "mode": sub.mode,
                          "min_silence": sub.min_silence},
            "audio": {"keep_db": a.keep_db, "duck_db": a.duck_db,
                      "mute_db": a.mute_db},
            "music": {"start": a.music_start, "db": a.music_db},
            "voice": {"name": v.name, "pitch": v.pitch, "rate": v.rate,
                      "fillers": v.fillers, "english_terms": v.english_terms,
                      "acronyms": v.acronyms},
            "style": {"surface": y.surface, "ink": y.ink, "muted": y.muted,
                      "accent": y.accent, "font": y.font},
            "output": {"target_lufs": a.target_lufs},
        }

    # -- secrets ------------------------------------------------------------
    def require(self, key: str) -> str:
        v = _get(self.env, key)
        if not v:
            raise SystemExit(
                f"{key} is not set. Add it to {self.root / '.env'} "
                f"(see .env.example) or export it."
            )
        return v

    def optional(self, key: str, default: str = "") -> str:
        return _get(self.env, key, default)


    # -- introspection ------------------------------------------------------
    def effective(self) -> dict:
        """Every resolved setting and where it came from.

        Config that cannot be inspected is config people guess at, so this
        reports the value AND its source: an explicit env var, the .env file,
        or the built-in default.
        """
        import os as _os

        def src(key: str) -> str:
            if _os.environ.get(key):
                return "env"
            if self.env.get(key):
                return ".env"
            return "default"

        r = self.remote
        q, b, a, v, y = (self.sequence, self.subtitles, self.audio,
                         self.voice, self.style)
        rows = {
            "remote.host":     (r.host, src("UMC_REMOTE_HOST")),
            "remote.user":     (r.user, src("UMC_REMOTE_USER")),
            "remote.ssh_alias": (r.ssh_alias, src("UMC_SSH_ALIAS")),
            "remote.key_path": (r.key_path, src("UMC_SSH_KEY")),
            "remote.ssh_mux":  (str(r.ssh_mux), src("UMC_SSH_MUX")),
            "remote.ssh_persist": (r.ssh_persist, src("UMC_SSH_PERSIST")),
            "remote.password": ("set" if r.password else "", src("UMC_SSH_PASSWORD")),
            "tmux.session":    (r.tmux_session, src("UMC_TMUX_SESSION")),
            "tmux.pane":       (r.tmux_pane or "auto-detect", src("UMC_TMUX_PANE")),
            "cdp.port":        (str(r.cdp_port), src("UMC_CDP_PORT")),
            "paths.root":      (r.root, src("UMC_REMOTE_ROOT")),
            "paths.assets":    (r.assets, "derived"),
            "paths.exports":   (r.exports, "derived"),
            "paths.edit_ready": (r.edit_ready, "derived"),
            "paths.local_root": (str(self.root), "derived"),
            "premiere.app":    (r.premiere_app, src("UMC_PREMIERE_APP")),
            "premiere.cep_ext": (r.cep_ext_dir, "derived"),
            "premiere.project": (r.project_path, src("UMC_PROJECT")),
            "premiere.preset": (r.preset_path, src("UMC_PRESET")),
            "mcp.repo":        (r.mcp_repo, src("UMC_MCP_REPO")),
            "sequence.fps":    (str(q.fps), src("UMC_FPS")),
            "sequence.size":   (f"{q.width}x{q.height}", src("UMC_WIDTH")),
            "sequence.preset": (r.sequence_preset or "(ask Premiere)",
                                src("UMC_SEQUENCE_PRESET")),
            "sequence.scene_pad": (str(q.scene_pad), src("UMC_SCENE_PAD")),
            "sequence.narration_lead": (str(q.narration_lead),
                                        src("UMC_NARRATION_LEAD")),
            "subs.generate":   (str(b.generate), src("UMC_SUBS_GENERATE")),
            "subs.language":   (b.language, src("UMC_SUBS_LANG")),
            "subs.max_chars":  (str(b.max_chars), src("UMC_SUBS_MAX_CHARS")),
            "subs.mode":       (b.mode, src("UMC_SUBS_MODE")),
            "subs.min_silence": (str(b.min_silence), src("UMC_SUBS_MIN_SILENCE")),
            "audio.target_lufs": (str(a.target_lufs), src("UMC_TARGET_LUFS")),
            "audio.keep_db":   (str(a.keep_db), src("UMC_KEEP_DB")),
            "audio.duck_db":   (str(a.duck_db), src("UMC_DUCK_DB")),
            "audio.mute_db":   (str(a.mute_db), src("UMC_MUTE_DB")),
            "audio.music_start": (str(a.music_start), src("UMC_MUSIC_START")),
            "audio.music_db":  (str(a.music_db), src("UMC_MUSIC_DB")),
            "voice.name":      (v.name, src("UMC_VOICE_NAME")),
            "voice.pitch":     (v.pitch, src("UMC_VOICE_PITCH")),
            "voice.rate":      (v.rate, src("UMC_VOICE_RATE")),
            "voice.fillers":   (str(v.fillers), src("UMC_VOICE_FILLERS")),
            "voice.english":   (", ".join(v.english_terms), src("UMC_VOICE_ENGLISH")),
            "voice.acronyms":  (", ".join(v.acronyms), src("UMC_VOICE_ACRONYMS")),
            "style.surface":   (y.surface, src("UMC_STYLE_SURFACE")),
            "style.accent":    (y.accent, src("UMC_STYLE_ACCENT")),
            "style.font":      (y.font, src("UMC_STYLE_FONT")),
            "api.json2video":  ("set" if _get(self.env, "JSON2VIDEO_API_KEY")
                                else "MISSING", src("JSON2VIDEO_API_KEY")),
            "ingest.csv_url":  ("set" if _get(self.env, "UMC_INGEST_CSV_URL")
                                else "", src("UMC_INGEST_CSV_URL")),
            "ingest.notebook": (_get(self.env, "UMC_INGEST_NOTEBOOK_URL") or "",
                                src("UMC_INGEST_NOTEBOOK_URL")),
        }
        return {k: {"value": v, "from": s} for k, (v, s) in rows.items()}
