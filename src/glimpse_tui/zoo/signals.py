"""Causal indicator helpers shared by the signal models: the lab's `_signals.py`, price and volume only.

Every function takes hourly bars indexed by bar open time and returns a series whose value at bar t uses bar t and
earlier bars only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HOURS_PER_YEAR = 24 * 365.25


def log_close(bars: pd.DataFrame) -> pd.Series:
    return np.log(bars["close"].astype(float))


def hourly_vol(bars: pd.DataFrame, span: int = 168) -> pd.Series:
    """Hourly log-return volatility (EWM standard deviation), floored."""
    return log_close(bars).diff().ewm(span=span, min_periods=48).std().clip(lower=1e-6)


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, min_periods=span).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def true_range(bars: pd.DataFrame) -> pd.Series:
    h, lo, c = (bars[k].astype(float) for k in ("high", "low", "close"))
    prev = c.shift(1)
    return pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)


def atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's average true range."""
    return true_range(bars).ewm(alpha=1.0 / n, min_periods=n).mean().clip(lower=1e-9)


def zscore(s: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    mp = min_periods or max(24, window // 4)
    m = s.rolling(window, min_periods=mp).mean()
    sd = s.rolling(window, min_periods=mp).std()
    return (s - m) / sd.where(sd > 1e-12)


def rolling_slope_t(y: pd.Series, n: int) -> tuple[pd.Series, pd.Series]:
    """OLS slope of y on time over the last n bars and the regression's R^2, by rolling sums."""
    t = pd.Series(np.arange(len(y), dtype=float), index=y.index)
    sy, st = y.rolling(n, min_periods=n).sum(), t.rolling(n, min_periods=n).sum()
    syy = (y * y).rolling(n, min_periods=n).sum()
    stt = (t * t).rolling(n, min_periods=n).sum()
    sty = (t * y).rolling(n, min_periods=n).sum()
    cov = sty - st * sy / n
    vt = stt - st * st / n
    vy = syy - sy * sy / n
    slope = cov / vt
    r2 = (cov * cov) / (vt * vy.where(vy > 1e-18))
    return slope, r2.clip(0, 1)


def daily_levels(bars: pd.DataFrame) -> pd.DataFrame:
    """Previous complete UTC day's high, low, close and the floor-trader pivots (P, R1, S1), per hourly bar."""
    h, lo, c = (bars[k].astype(float) for k in ("high", "low", "close"))
    day = pd.DatetimeIndex(bars.index).floor("D")
    daily = pd.DataFrame({"high": h.groupby(day).max(), "low": lo.groupby(day).min(), "close": c.groupby(day).last()})
    counts = c.groupby(day).count()
    prev = daily.shift(1)
    prev[counts.shift(1).fillna(0).to_numpy() < 24] = np.nan  # the previous day must be complete
    prev = prev.reindex(day)
    prev.index = bars.index
    p = (prev["high"] + prev["low"] + prev["close"]) / 3.0
    return pd.DataFrame(
        {"P": p, "R1": 2 * p - prev["low"], "S1": 2 * p - prev["high"], "H": prev["high"], "L": prev["low"]},
        index=bars.index,
    )


def heikin_ashi(bars: pd.DataFrame) -> pd.DataFrame:
    o, h, lo, c = (bars[k].astype(float).to_numpy() for k in ("open", "high", "low", "close"))
    ha_c = (o + h + lo + c) / 4.0
    ha_o = np.empty_like(ha_c)
    ha_o[0] = (o[0] + c[0]) / 2.0
    for i in range(1, ha_c.size):
        ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
    return pd.DataFrame({"open": ha_o, "close": ha_c}, index=bars.index)


def signed_run_length(sign: np.ndarray) -> np.ndarray:
    """+k after k consecutive positive values, -k after k negative ones, 0 on a zero."""
    out = np.zeros(sign.size)
    run = 0.0
    for i, s in enumerate(sign):
        if s > 0:
            run = run + 1 if run > 0 else 1
        elif s < 0:
            run = run - 1 if run < 0 else -1
        else:
            run = 0
        out[i] = run
    return out
