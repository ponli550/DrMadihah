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
class Config:
    env: dict = field(default_factory=dict)
    remote: Remote = field(default_factory=Remote)
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
        )
        return cls(env=env, remote=r)

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
