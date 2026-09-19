"""API key storage. The key never reaches a log, a traceback or the screen.

Lookup order: GLIMPSE_API_KEY env var, OS keychain, then ~/.config/glimpse/credentials
(mode 0600) for headless servers where no keychain backend exists.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

SERVICE = "glimpse-tui"
ACCOUNT = "api-key"
ENV_VAR = "GLIMPSE_API_KEY"
KEYS_URL = "https://glimpse.markets"   # register, pass KYC, then Settings -> Developer API Keys


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "glimpse"


def _cred_file() -> Path:
    return config_dir() / "credentials"


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
        return keyring
    except Exception:
        return None


def load_key() -> tuple[str | None, str]:
    """Returns (key, source). Source is 'env', 'keychain', 'file' or 'none'."""
    if key := os.environ.get(ENV_VAR, "").strip():
        return key, "env"
    if kr := _keyring():
        try:
            if key := kr.get_password(SERVICE, ACCOUNT):
                return key, "keychain"
        except Exception:
            pass
    f = _cred_file()
    if f.is_file():
        if f.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            f.chmod(0o600)
        if key := f.read_text().strip():
            return key, "file"
    return None, "none"


def save_key(key: str) -> str:
    """Persist the key; returns where it went ('keychain' or 'file')."""
    key = key.strip()
    if kr := _keyring():
        try:
            kr.set_password(SERVICE, ACCOUNT, key)
            return "keychain"
        except Exception:
            pass
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    f = _cred_file()
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    return "file"


def forget_key() -> None:
    if kr := _keyring():
        try:
            kr.delete_password(SERVICE, ACCOUNT)
        except Exception:
            pass
    _cred_file().unlink(missing_ok=True)


def looks_like_key(key: str) -> bool:
    key = key.strip()
    return len(key) >= 16 and " " not in key


def mask(key: str | None) -> str:
    """Safe to display: prefix and last four only."""
    if not key:
        return "-"
    return f"{key[:9]}…{key[-4:]}" if len(key) > 16 else "…"


def load_state() -> dict:
    """Small non-secret UI state (the last series viewed). Never holds the key."""
    try:
        return json.loads((config_dir() / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        (config_dir() / "state.json").write_text(json.dumps(state))
    except OSError:
        pass
