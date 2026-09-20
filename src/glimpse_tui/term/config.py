"""The terminal's settings: ~/.config/glimpse/terminal.toml over built-in defaults (TERMINAL.md appendix B).

Nothing here is secret. Keys and tokens live in the keychain (`secrets` below follows auth.py's pattern); this
file only records that one exists. Every Bitcoin backend is a URL, so a self-hosted one is a one-line change,
and `SET` rewrites the file without a restart.
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

from .. import auth

DEFAULTS: dict[str, Any] = {
    "tape": ["BTC", "XAU", "SPX", "NDX", "DXY", "US10Y", "BRENT", "EURUSD", "USDJPY", "ETH"],
    "clocks": ["UTC", "America/New_York", "Europe/London", "Asia/Tokyo"],
    "default_launchpad": "BTC",
    "socks5": "",
    "sec_user_agent": "",
    "privacy_ack": False,           # the one-line note about public lookups has been shown
    "bitcoin": {
        "order": ["mempool", "bitview", "esplora"],
        "mempool": "https://mempool.space",
        "bitview": "https://bitview.space",
        "esplora": "https://blockstream.info",
        "electrum": "",
        "core_rpc": "",
    },
    "keys": {"fred": False, "typesafe": False, "stooq": False},
    "sources": {"yahoo": True},     # Yahoo Finance's public chart API: indices, yields, futures, FX, shares
    "lists": {},                    # user watchlists for QM: name -> [tickers]
    "gold_stock_tonnes": 216_265,   # World Gold Council above-ground stock, end 2024; RV says so on screen
}


def config_dir() -> Path:
    return auth.config_dir()


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "glimpse"


def path() -> Path:
    return config_dir() / "terminal.toml"


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load() -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        _merge(cfg, tomllib.loads(path().read_text()))
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return cfg


# ── writing TOML (tomllib only reads; the schema is small enough to emit by hand) ─────────────

def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, list | tuple):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else _value(k)


def dumps(cfg: dict[str, Any]) -> str:
    """Scalars and lists first, then one [table] per nested dict. Round-trips through tomllib."""
    out = [f"{_key(k)} = {_value(v)}" for k, v in cfg.items() if not isinstance(v, dict)]
    for k, v in cfg.items():
        if isinstance(v, dict):
            out += ["", f"[{_key(k)}]"] + [f"{_key(a)} = {_value(b)}" for a, b in v.items()]
    return "\n".join(out) + "\n"


def save(cfg: dict[str, Any]) -> None:
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        path().write_text(dumps(cfg))
    except OSError:
        pass


def set_value(cfg: dict[str, Any], dotted: str, raw: str) -> Any:
    """`SET bitcoin.mempool http://umbrel.local:3006`: coerce `raw` to the type of the default and store it."""
    *parents, leaf = dotted.split(".")
    node, default = cfg, DEFAULTS
    for p in parents:
        if not isinstance(node.get(p), dict):
            raise KeyError(dotted)
        node, default = node[p], default.get(p, {}) if isinstance(default, dict) else {}
    like = default.get(leaf) if isinstance(default, dict) else None
    if leaf not in node and like is None:
        raise KeyError(dotted)
    if isinstance(like, bool):
        v: Any = raw.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(like, int):
        v = int(raw.replace(",", "").replace("_", ""))
    elif isinstance(like, float):
        v = float(raw.replace(",", ""))
    elif isinstance(like, list):
        v = [x for x in raw.replace(",", " ").split() if x]
    else:
        v = raw.strip()
    node[leaf] = v
    return v


# ── secrets: the keychain, then a 0600 file, never a log (auth.py's pattern) ───────────────────

def secret(name: str) -> str | None:
    """An optional key or token: env GLIMPSE_<NAME>, the keychain, then ~/.config/glimpse/secrets/<name>."""
    if v := os.environ.get(f"GLIMPSE_{name.upper()}", "").strip():
        return v
    if kr := auth._keyring():
        try:
            if v := kr.get_password(auth.SERVICE, name):
                return v
        except Exception:
            pass
    f = config_dir() / "secrets" / name
    try:
        return f.read_text().strip() or None
    except OSError:
        return None


def save_secret(name: str, value: str) -> str:
    if kr := auth._keyring():
        try:
            kr.set_password(auth.SERVICE, name, value.strip())
            return "keychain"
        except Exception:
            pass
    d = config_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value.strip() + "\n")
    return "file"


def forget_secret(name: str) -> None:
    if kr := auth._keyring():
        try:
            kr.delete_password(auth.SERVICE, name)
        except Exception:
            pass
    (config_dir() / "secrets" / name).unlink(missing_ok=True)
