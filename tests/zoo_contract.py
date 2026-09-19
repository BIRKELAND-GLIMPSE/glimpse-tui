"""What every zoo model must satisfy, on synthetic bars, offline. Import `check_model` from family tests."""
from __future__ import annotations

import numpy as np
import pandas as pd

from glimpse_tui.zoo.core import Ctx, Model, describe

NOW = 1_789_689_600.0 + 1200          # 20 minutes into a UTC hour


def synthetic_bars(n: int = 1500, seed: int = 7, drift: float = 0.0, vol: float = 0.004) -> pd.DataFrame:
    """Hourly OHLCV with volatility clustering and an intraday profile, ending at the last complete hour before NOW."""
    rng = np.random.default_rng(seed)
    s2, r = vol**2, np.empty(n)
    for i in range(n):
        s2 = 0.9 * s2 + 0.1 * vol**2 * (0.5 + rng.standard_t(5) ** 2 / 3)
        r[i] = drift + np.sqrt(s2) * rng.standard_t(5) / np.sqrt(5 / 3) * (1.3 if i % 24 in (13, 14, 15) else 0.9)
    close = 78_000 * np.exp(np.cumsum(r) - np.cumsum(r)[-1])
    open_ = np.concatenate([[close[0]], close[:-1]])
    span = np.abs(rng.normal(0, vol, n)) * close
    idx = pd.date_range(end=pd.Timestamp((int(NOW) // 3600 - 1) * 3600, unit="s", tz="UTC"), periods=n, freq="h")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + span, "low": np.minimum(open_, close) - span,
                         "close": close, "volume": rng.gamma(2.0, 50.0, n) * (1 + 40 * np.abs(r) / vol / 10)}, index=idx)


def ctx_for(bars: pd.DataFrame | None = None, hours: float = 2.0, spot: float | None = None) -> Ctx:
    bars = synthetic_bars() if bars is None else bars
    edges = 28_000.0 + 200.0 * np.arange(501)            # the production hourly BTC ladder: 500 bins of $200
    end = (int(NOW) // 3600 + 1) * 3600 + (hours - 1) * 3600 if hours >= 1 else NOW + hours * 3600
    return Ctx(bars=bars, spot=float(bars["close"].iloc[-1]) if spot is None else spot, edges=edges, now=NOW, end=float(end))


def check_model(m: Model) -> None:
    assert m.id and m.name and m.family and m.description and callable(m.fn)
    assert 0 < len(m.blurb) <= 70, f"{m.id}: blurb must be one short line ({len(m.blurb)} chars)"
    assert m.kind in ("forecast", "view")
    bars = synthetic_bars()
    for hours in (0.4, 2.0, 30.0):
        ctx = ctx_for(bars, hours)
        p = np.asarray(m.fn(ctx), dtype=float)
        assert p.shape == (500,), f"{m.id}: {p.shape}"
        assert np.all(np.isfinite(p)) and np.all(p > 0), f"{m.id}: probabilities must be finite and positive"
        assert abs(p.sum() - 1.0) < 1e-9, f"{m.id}: sums to {p.sum()}"
        d = describe(ctx, p)
        assert abs(d["lean"]) < 3.0 and 0.2 < d["width"] < 4.0, f"{m.id} at {hours}h: implausible picture {d}"
        q = np.asarray(m.fn(ctx_for(bars, hours)), dtype=float)
        assert np.allclose(p, q), f"{m.id}: same inputs must draw the same picture"
