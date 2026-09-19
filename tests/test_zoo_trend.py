"""Trend models: a sign error or a dead signal must fail here."""
from __future__ import annotations

import numpy as np
import pytest
from zoo_contract import check_model, ctx_for, synthetic_bars

from glimpse_tui.zoo import trend
from glimpse_tui.zoo.core import describe

UP, DOWN = synthetic_bars(drift=0.002), synthetic_bars(drift=-0.002)
REGIME_DEPENDENT = {"variance_ratio_trend"}      # follows or fades the last day, so a steady drift does not fix its sign
CHANGE_SIGNALS = {"macd_drift", "obv_trend"}     # read the change in the trend, so a steady drift averages to zero


def test_the_module_lists_twenty_trend_models():
    assert len(trend.MODELS) == 20 and len({m.id for m in trend.MODELS}) == 20
    assert all(m.family == "Trend" and m.factors == ("direction", "tails") for m in trend.MODELS)


@pytest.mark.parametrize("m", trend.MODELS, ids=lambda m: m.id)
def test_contract(m):
    check_model(m)


@pytest.mark.parametrize("m", [m for m in trend.MODELS if m.id not in REGIME_DEPENDENT], ids=lambda m: m.id)
def test_leans_with_the_trend(m):
    # a trend that has just turned: most signals sit at the top of their own history after the turn up
    n = 1500
    turn_up = synthetic_bars(n)
    since = 10 if m.id == "macd_drift" else 240        # the histogram fades once the new trend is steady
    step = np.where(np.arange(n) < n - since, -0.0005, 0.003)
    for bars, s in ((turn_up.copy(), 1.0), (turn_up.copy(), -1.0)):
        path = np.exp(np.cumsum(s * step))
        for k in ("open", "high", "low", "close"):
            bars[k] = bars[k] * path / path[-1]
        if s < 0:
            bars["high"], bars["low"] = bars[["high", "low"]].max(axis=1), bars[["high", "low"]].min(axis=1)
        lean = describe(ctx_for(bars, 24.0), m.fn(ctx_for(bars, 24.0)))["lean"]
        assert s * lean > 0.05, f"{m.id}: lean {lean:+.2f} after a turn {'up' if s > 0 else 'down'}"


@pytest.mark.parametrize("m", [m for m in trend.MODELS if m.id not in REGIME_DEPENDENT], ids=lambda m: m.id)
def test_signal_is_alive_and_signed(m):
    up = trend_signal(m.id, UP)
    down = trend_signal(m.id, DOWN)
    assert np.isfinite(up.iloc[-1]) and np.isfinite(down.iloc[-1])
    assert np.isfinite(up).sum() > 700 and up.std() > 0
    if m.id not in CHANGE_SIGNALS:
        assert up.iloc[-500:].mean() > 0 > down.iloc[-500:].mean()


def trend_signal(model_id, bars):
    ctx = ctx_for(bars, 2.0)
    next(m for m in trend.MODELS if m.id == model_id).fn(ctx)
    return ctx.cache[f"{model_id}:signal"]


def test_signals_are_causal():
    bars = synthetic_bars()
    for m in trend.MODELS:
        if m.id == "kalman_trend":
            continue                                   # filters the last 2,000 bars; with 1,500 it is the same window
        full, cut = trend_signal(m.id, bars), trend_signal(m.id, bars.iloc[:-50])
        assert np.allclose(full.iloc[:-50].to_numpy(), cut.to_numpy(), equal_nan=True), m.id


def test_kalman_slope_tracks_a_known_drift_and_hp_trend_follows_the_price():
    y = np.cumsum(np.full(1200, 0.001) + np.random.default_rng(1).normal(0, 0.004, 1200))
    _, slope = trend.local_linear_trend(y, 0.1, 4e-4)
    assert abs(slope[-400:].mean() - 0.001) < 0.0005
    hp = trend.one_sided_hp(y, 1.0e6)
    assert np.isfinite(hp[10:]).all() and abs(hp[-1] - y[-1]) < 0.05


def test_the_signal_is_computed_once_per_set_of_bars():
    ctx = ctx_for(UP, 2.0)
    trend.MODELS[0].fn(ctx)
    marker = ctx.cache["tsmom_drift:signal"]
    trend.MODELS[0].fn(ctx)
    assert ctx.cache["tsmom_drift:signal"] is marker
