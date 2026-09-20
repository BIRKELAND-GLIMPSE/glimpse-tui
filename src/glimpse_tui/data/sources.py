"""Every source the terminal reads, built once for the hub. SOURCES.md is the record of what each one serves,
its limits and its terms, checked live on 19 Sep 2026. Budgets follow TERMINAL.md 4.3.

A source that is optional (its module is not built yet, or fails to import) is skipped: the functions that need
it say so, and the rest of the terminal runs.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..term.hub import Hub

# key -> (module, class). The key is what `hub.sources[...]`, SRC and `bitcoin.order` use.
CATALOGUE = (
    ("mempool", "mempool", "Mempool"),
    ("bitview", "bitview", "Bitview"),
    ("esplora", "esplora", "Esplora"),
    ("coinbase", "prices", "Coinbase"),
    ("kraken", "prices", "Kraken"),
    ("bitstamp", "prices", "Bitstamp"),
    ("fred", "fred", "Fred"),
    ("treasury", "treasury", "Treasury"),
    ("ecb", "fx_ref", "Ecb"),
    ("deribit", "deribit", "Deribit"),
    ("sec", "sec", "SecData"),
    ("sec_www", "sec", "SecWww"),
    ("sec_efts", "sec", "SecSearch"),
    ("fng", "sentiment", "FearGreed"),
    ("yahoo", "yahoo", "Yahoo"),
)


def build(hub: Hub) -> None:
    for key, module, cls in CATALOGUE:
        try:
            mod = importlib.import_module(f"glimpse_tui.data.{module}")
            hub.add(key, getattr(mod, cls)())
        except (ModuleNotFoundError, AttributeError):
            continue
    try:
        from . import news
        news.build(hub)                             # one source per outlet: NEWS and the headline feed on SRC
    except ModuleNotFoundError:
        pass
