"""Encrypted .env support, via dotenvx.

dotenvx encrypts each VALUE in .env with a public key that stays in the file,
and keeps the matching private key in `.env.keys`. The upshot:

    .env        encrypted values + DOTENV_PUBLIC_KEY   -> safe to commit
    .env.keys   DOTENV_PRIVATE_KEY                     -> never commit

We deliberately do NOT implement the crypto here. Rolling your own is how
secrets get lost or quietly weakened; dotenvx is a maintained tool that does
ECIES properly. If it is not installed we fall back to a plain .env and say so,
rather than pretending the file is protected.

Detection is by content: an encrypted file contains DOTENV_PUBLIC_KEY and
values that start with "encrypted:".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import log

MARKER = "DOTENV_PUBLIC_KEY"
PREFIX = "encrypted:"


def dotenvx_cmd() -> list | None:
    """Return an argv prefix that runs dotenvx, or None."""
    if shutil.which("dotenvx"):
        return ["dotenvx"]
    if shutil.which("npx"):
        # works without a global install; slower on first run
        return ["npx", "--yes", "@dotenvx/dotenvx"]
    return None


def is_encrypted(env_path: Path) -> bool:
    if not env_path.exists():
        return False
    text = env_path.read_text(encoding="utf-8", errors="replace")
    return MARKER in text and PREFIX in text


def load_encrypted(env_path: Path) -> dict:
    """Decrypt via dotenvx and return the values.

    Raises with an actionable message rather than silently returning the
    ciphertext, which would fail much later as a confusing auth error.
    """
    cmd = dotenvx_cmd()
    if not cmd:
        raise SystemExit(
            f"{env_path.name} is encrypted but dotenvx is not installed.\n"
            "  npm install -g @dotenvx/dotenvx      (or ensure npx is available)")

    keys = env_path.parent / ".env.keys"
    if not keys.exists() and not os.environ.get("DOTENV_PRIVATE_KEY"):
        raise SystemExit(
            f"{env_path.name} is encrypted but no private key was found.\n"
            f"  expected {keys}, or DOTENV_PRIVATE_KEY in the environment")

    p = subprocess.run(cmd + ["get", "--format", "json", "-f", str(env_path)],
                       capture_output=True, text=True, timeout=120,
                       cwd=str(env_path.parent))
    if p.returncode != 0:
        raise SystemExit(f"dotenvx could not decrypt {env_path.name}: "
                         f"{p.stderr.strip()[:300]}")
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        raise SystemExit(f"dotenvx returned unparseable output: {p.stdout[:200]}")
    return {k: str(v) for k, v in data.items() if v is not None}


def status(env_path: Path) -> dict:
    keys = env_path.parent / ".env.keys"
    return {
        "env": str(env_path),
        "exists": env_path.exists(),
        "encrypted": is_encrypted(env_path),
        "keys_file": str(keys),
        "keys_file_exists": keys.exists(),
        "dotenvx": " ".join(dotenvx_cmd()) if dotenvx_cmd() else None,
        "private_key_in_env": bool(os.environ.get("DOTENV_PRIVATE_KEY")),
    }


def encrypt(env_path: Path) -> dict:
    """Encrypt .env in place, creating .env.keys."""
    cmd = dotenvx_cmd()
    if not cmd:
        raise SystemExit("dotenvx is not installed: npm install -g @dotenvx/dotenvx")
    if is_encrypted(env_path):
        log.ok(f"{env_path.name} is already encrypted")
        return status(env_path)

    backup = env_path.with_suffix(".env.plain.bak")
    backup.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")
    backup.chmod(0o600)
    log.step(f"backed up plaintext to {backup.name} (gitignored — delete it once happy)")

    p = subprocess.run(cmd + ["encrypt", "-f", str(env_path)],
                       capture_output=True, text=True, timeout=180,
                       cwd=str(env_path.parent))
    if p.returncode != 0:
        raise SystemExit(f"dotenvx encrypt failed: {p.stderr.strip()[:300]}")

    keys = env_path.parent / ".env.keys"
    if keys.exists():
        keys.chmod(0o600)
    log.ok("encrypted — .env is now safe to commit; .env.keys is NOT")
    return status(env_path)


def decrypt(env_path: Path) -> dict:
    cmd = dotenvx_cmd()
    if not cmd:
        raise SystemExit("dotenvx is not installed")
    p = subprocess.run(cmd + ["decrypt", "-f", str(env_path)],
                       capture_output=True, text=True, timeout=180,
                       cwd=str(env_path.parent))
    if p.returncode != 0:
        raise SystemExit(f"dotenvx decrypt failed: {p.stderr.strip()[:300]}")
    log.warn(f"{env_path.name} is now PLAINTEXT again — do not commit it")
    return status(env_path)


SECRETISH = ("KEY", "PASSWORD", "TOKEN", "SECRET")


def looks_secret(name: str) -> bool:
    return any(w in name.upper() for w in SECRETISH)


def mask(name: str, value: str) -> str:
    if not value:
        return ""
    if not looks_secret(name):
        return value
    return (value[:4] + "…" + value[-4:]) if len(value) > 12 else "…" * 3


def set_value(env_path: Path, key: str, value: str, encrypt_secrets: bool = False) -> dict:
    """Write KEY=value into .env.

    If the file is already encrypted we always go through `dotenvx set`, which
    encrypts the new value in place. If it is plaintext and the key looks like
    a credential, we encrypt the whole file first so the secret is never
    written in the clear — otherwise a later `secrets encrypt` would leave the
    plaintext sitting in git history or a backup.
    """
    cmd = dotenvx_cmd()
    encrypted = is_encrypted(env_path)

    # Encryption is opt-in while the repo .env is in active use for testing.
    if not encrypted and encrypt_secrets and looks_secret(key) and cmd:
        log.step(f"{key} looks like a credential — encrypting .env first")
        encrypt(env_path)
        encrypted = True

    if encrypted:
        if not cmd:
            raise SystemExit("dotenvx is required to write into an encrypted .env")
        p = subprocess.run(cmd + ["set", key, value, "-f", str(env_path)],
                           capture_output=True, text=True, timeout=180,
                           cwd=str(env_path.parent))
        if p.returncode != 0:
            raise SystemExit(f"dotenvx set failed: {p.stderr.strip()[:300]}")
        return {"key": key, "stored": "encrypted", "file": str(env_path)}

    # plaintext fallback: rewrite the line in place, preserving comments
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    done = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            done = True
            break
    if not done:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    if looks_secret(key):
        log.warn(f"{key} written in PLAINTEXT — {env_path.name} must stay "
                 f"gitignored (`umcares config set … --encrypt` to encrypt)")
    return {"key": key, "stored": "plaintext", "file": str(env_path)}


def init(env_path: Path) -> dict:
    """First run: create .env.keys by encrypting .env.

    The private key is generated locally by dotenvx and written to .env.keys.
    It exists in exactly one place — there is no server copy and no recovery
    if you lose it, which is why this prints where it lives and what to do.
    """
    if not env_path.exists():
        raise SystemExit(f"{env_path} does not exist — copy .env.example first")

    keys = env_path.parent / ".env.keys"
    if is_encrypted(env_path) and keys.exists():
        log.ok(".env is already encrypted and .env.keys is present")
        return status(env_path)

    if is_encrypted(env_path) and not keys.exists():
        raise SystemExit(
            ".env is encrypted but .env.keys is missing.\n"
            "  Restore it from your password manager / backup, or set "
            "DOTENV_PRIVATE_KEY in the environment.\n"
            "  Without it these values CANNOT be recovered.")

    res = encrypt(env_path)
    log.info(f"private key written to {keys}")
    log.warn("back this file up somewhere safe (password manager). "
             "It is gitignored and cannot be regenerated — losing it means "
             "losing every encrypted value.")
    return res


def rotate(env_path: Path, keep_backup: bool = True) -> dict:
    """Replace the keypair: decrypt with the old key, re-encrypt with a new one.

    Used when a key may have leaked. The old .env.keys is backed up (unless
    keep_backup=False) because if anything fails midway that file is the only
    way back to the values.
    """
    import time as _time

    cmd = dotenvx_cmd()
    if not cmd:
        raise SystemExit("dotenvx is not installed")
    if not is_encrypted(env_path):
        raise SystemExit(".env is not encrypted — run `umcares secrets init` first")

    keys = env_path.parent / ".env.keys"
    values = load_encrypted(env_path)          # fails loudly if the old key is gone
    if not values:
        raise SystemExit("refusing to rotate: decrypted 0 values")
    log.step(f"decrypted {len(values)} value(s) with the current key")

    stamp = _time.strftime("%Y%m%d-%H%M%S")
    if keys.exists() and keep_backup:
        backup = keys.with_name(f".env.keys.{stamp}.bak")
        backup.write_text(keys.read_text(encoding="utf-8"), encoding="utf-8")
        backup.chmod(0o600)
        log.step(f"old key backed up to {backup.name}")

    env_backup = env_path.with_name(f".env.{stamp}.bak")
    env_backup.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")
    env_backup.chmod(0o600)

    # write the values back as plaintext, dropping the old public key, then
    # encrypt afresh so dotenvx mints a brand new keypair
    lines = [f"{k}={v}" for k, v in values.items() if k != "DOTENV_PUBLIC_KEY"]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    if keys.exists():
        keys.unlink()

    try:
        encrypt(env_path)
    except SystemExit:
        env_path.write_text(env_backup.read_text(encoding="utf-8"), encoding="utf-8")
        log.err("re-encryption failed — .env restored from backup")
        raise

    after = load_encrypted(env_path)
    if set(after) != set(values):
        raise SystemExit("rotation changed the key set — check "
                         f"{env_backup.name} before deleting anything")

    log.ok(f"rotated: new .env.keys, {len(after)} value(s) intact")
    log.warn("the OLD key no longer decrypts this file — update any backup copy")
    return status(env_path)
