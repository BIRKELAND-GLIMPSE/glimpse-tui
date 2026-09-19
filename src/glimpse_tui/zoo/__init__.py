"""The model zoo: the Forecast Lab's pictures of the future, runnable on a laptop from public hourly candles.

    from glimpse_tui import zoo
    zoo.catalog()            # every Model, in list order
    zoo.get("ema_crossover")

Importing this package is cheap; numpy, pandas and scipy load on the first call to `catalog()`.
"""
from __future__ import annotations

import importlib
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Model

# One module per lab family; each exposes MODELS: list[Model]. Order here is order on screen.
MODULES = ("baseline", "trend", "reversion", "volatility", "calendar", "regimes", "learning", "distributions", "scenarios", "options")
FAMILIES = ("Baseline", "Trend", "Mean reversion", "Volatility", "Calendar & cycles", "Factor & carry", "Regime & jumps",
            "Pattern & ML", "GARCH", "HAR", "Power Law", "Distributions", "Scenario", "Options views")


@lru_cache(maxsize=1)
def catalog() -> tuple[Model, ...]:
    out: list[Model] = []
    seen: set[str] = set()
    for name in MODULES:
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"{__name__}.{name}":
                raise
            continue
        for m in mod.MODELS:
            if m.id in seen:
                raise ValueError(f"duplicate model id {m.id}")
            seen.add(m.id)
            out.append(m)
    return tuple(out)


def get(model_id: str) -> Model | None:
    return next((m for m in catalog() if m.id == model_id), None)
