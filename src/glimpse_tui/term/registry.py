"""The function registry (TERMINAL.md 4.5). A function is a code, a one-line summary, a HELP page and a pane.

Function modules under `funcs/` call `register` at import. `load_all` imports them once; a module that fails to
import is recorded and skipped, so one broken family never takes the terminal down.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CATEGORIES = ("Bitcoin", "On-chain", "Markets", "Companies", "Glimpse", "Wallet", "Tools")
CLASSES = ("CRYPTO", "CURNCY", "CMDTY", "INDEX", "GOVT", "EQUITY", "ETF", "SERIES")
FAMILIES = ("tools", "glimpse", "btc", "chain", "mining", "onchain", "markets", "companies", "wallet")


@dataclass(frozen=True)
class Function:
    code: str                           # "MEMP"
    name: str                           # "Mempool"
    category: str                       # one of CATEGORIES
    summary: str                        # one line for HELP and autocomplete
    make: Callable[..., Any] | None     # make(hub, security, args) -> FuncPane; None for functions the shell runs itself
    takes: tuple[str, ...] = ()         # instrument classes it accepts; () for none
    needs: tuple[str, ...] = ()         # capabilities it reads, for SRC and graceful degradation
    help: str = ""                      # the HELP page: what it shows, where each number comes from, refresh, when a source is down
    args: str = ""                      # usage after the code: "<txid>", "[height or hash]"
    many: bool = False                  # accepts several securities: GP BTC XAU SPX
    optional: bool = False              # runs without a security too: N [ticker]
    legacy: str = ""                    # one of today's screens: the app view it opens
    default_for: tuple[str, ...] = ()   # instrument classes whose bare ticker opens this function


_functions: dict[str, Function] = {}
_failed: dict[str, str] = {}
_loaded = False


def register(fn: Function) -> Function:
    _functions[fn.code] = fn
    return fn


def load_all() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for family in FAMILIES:
        try:
            importlib.import_module(f"glimpse_tui.funcs.{family}")
        except ModuleNotFoundError as e:
            if e.name != f"glimpse_tui.funcs.{family}":
                _failed[family] = f"{type(e).__name__}: {e}"
        except Exception as e:                       # a broken family is reported by SRC, not fatal
            _failed[family] = f"{type(e).__name__}: {e}"


def get(code: str) -> Function | None:
    load_all()
    return _functions.get(code.upper())


def every() -> list[Function]:
    load_all()
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(_functions.values(), key=lambda f: (order.get(f.category, 99), f.code))


def failed() -> dict[str, str]:
    return dict(_failed)


def default_for(cls: str) -> Function | None:
    load_all()
    return next((f for f in _functions.values() if cls in f.default_for), None)


def for_class(cls: str) -> list[Function]:
    """Functions that make sense for a security of this class: the menu beside the GO bar."""
    return [f for f in every() if cls in f.takes]
