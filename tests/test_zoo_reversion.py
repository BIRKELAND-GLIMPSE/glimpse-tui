import numpy as np
import pytest
from zoo_contract import check_model, ctx_for, synthetic_bars

from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import reversion as R

BY_ID = {m.id: m for m in R.MODELS}
# contrarian models whose signal must point against a sharp move in the last hours
FADERS = ("rsi_reversion", "bollinger_reversion", "vwap_reversion", "short_term_reversal", "channel_reversion",
          "stochastic_reversion", "cci_reversion", "volume_poc_magnet")


def shocked(move: float, hours: int = 12):
    """Flat synthetic history that ends with a steady move of `move` (log units) over the last `hours` bars."""
    bars = synthetic_bars().copy()
    ramp = np.zeros(len(bars))
    ramp[-hours:] = np.linspace(move / hours, move, hours)
    f = np.exp(ramp)
    prev = np.concatenate([[1.0], f[:-1]])
    bars["close"] *= f
    bars["open"] *= prev
    bars["high"] *= np.maximum(f, prev)
    bars["low"] *= np.minimum(f, prev)
    return bars


def lean(model_id: str, bars, hours: float = 12.0) -> float:
    ctx = ctx_for(bars, hours)
    return C.describe(ctx, BY_ID[model_id].fn(ctx))["lean"]


@pytest.mark.parametrize("m", R.MODELS, ids=lambda m: m.id)
def test_contract(m):
    check_model(m)


def test_the_module_ports_fourteen_models_in_two_families():
    assert len(R.MODELS) == 14
    assert {m.family for m in R.MODELS} == {"Mean reversion", "Factor & carry"}
    assert [m.id for m in R.MODELS if m.family == "Factor & carry"] == ["low_vol_anomaly", "skewness_premium"]


@pytest.mark.parametrize("model_id", FADERS)
def test_reversion_fades_a_sharp_move(model_id):
    assert lean(model_id, shocked(-0.08)) > 0.15, "a sell-off must lean the picture up"
    assert lean(model_id, shocked(+0.08)) < -0.15, "a rally must lean the picture down"


def test_reversion_lean_fades_with_the_horizon():
    bars = shocked(-0.08)
    assert lean("short_term_reversal", bars, 12.0) > lean("short_term_reversal", bars, 96.0) > 0


def test_support_bounces_and_resistance_rejects():
    bars = synthetic_bars()
    lv = R.daily_levels(bars).iloc[-1]
    for level, up in ((lv["S1"], True), (lv["R1"], False)):
        b = bars.copy()
        b.iloc[-1, b.columns.get_loc("close")] = level
        sig = R.support_resistance_signal(b).iloc[-1]
        assert sig == pytest.approx(1.0 if up else -1.0)
        got = lean("support_resistance_bounce", b)
        assert got > 0.15 if up else got < -0.15


def test_round_number_pulls_towards_the_nearest_thousand():
    bars = synthetic_bars()
    for close, up in ((77_800.0, True), (78_200.0, False)):
        b = bars.copy()
        b.iloc[-1, b.columns.get_loc("close")] = close
        got = lean("round_number_magnet", b)
        assert got > 0.15 if up else got < -0.15
    b = bars.copy()
    b.iloc[-1, b.columns.get_loc("close")] = 78_500.0          # halfway between two levels: no pull
    assert R.round_number_signal(b).iloc[-1] == pytest.approx(0.0)


def test_low_vol_anomaly_likes_calm_and_dislikes_turbulence():
    bars = synthetic_bars()
    logp = np.log(bars["close"].to_numpy())
    for mult, up in ((0.3, True), (3.0, False)):
        r = np.diff(logp)
        r[-168:] *= mult                                          # the last week is calmer or wilder than the rest
        b = bars.copy()
        b["close"] = np.exp(logp[0] + np.concatenate([[0.0], np.cumsum(r)]))
        b["close"] *= 78_000 / b["close"].iloc[-1]
        b["open"], b["high"], b["low"] = b["close"].shift(1).fillna(b["close"]), b["close"] * 1.001, b["close"] * 0.999
        got = lean("low_vol_anomaly", b, 24.0)
        assert got > 0.15 if up else got < -0.15


def test_skewness_premium_leans_against_a_month_of_spikes():
    bars = synthetic_bars()
    logp = np.log(bars["close"].to_numpy())
    for spike, up in ((-0.05, True), (+0.05, False)):
        r = np.diff(logp)
        r[[-600, -400, -200]] += spike
        b = bars.copy()
        b["close"] = np.exp(logp[-1] - r.sum() + np.concatenate([[0.0], np.cumsum(r)]))
        got = lean("skewness_premium", b, 24.0)
        assert got > 0.15 if up else got < -0.15


def ou_bars(level: float, last: float, kappa: float = 0.05):
    """A strongly mean-reverting log price around `level`, ending at `last`."""
    bars = synthetic_bars().copy()
    rng = np.random.default_rng(3)
    x = np.empty(len(bars))
    x[0] = 0.0
    for i in range(1, x.size):
        x[i] = x[i - 1] * (1 - kappa) + rng.normal(0, 0.004)
    x[-1] = np.log(last / level)
    close = level * np.exp(x)
    bars["close"] = close
    bars["open"] = np.concatenate([[close[0]], close[:-1]])
    bars["high"], bars["low"] = np.maximum(bars["open"], close) * 1.0005, np.minimum(bars["open"], close) * 0.9995
    return bars


def test_ou_level_leans_towards_the_fitted_level():
    # a ramp at the end of a random walk drags the fitted AR(1) level with it, so this one is tested on a real OU series
    assert lean("ou_level_reversion", ou_bars(78_000, 76_500)) > 0.15
    assert lean("ou_level_reversion", ou_bars(78_000, 79_500)) < -0.15


def test_range_box_pulls_towards_its_level_and_narrows_the_picture():
    below, above = ou_bars(78_000, 76_500), ou_bars(78_000, 79_500)
    assert R.fit_range_box(below)["active"]
    ctx = ctx_for(below, 30.0)
    d = C.describe(ctx, R.range_box_paths(ctx))
    assert d["lean"] > 0.15 and d["width"] < 0.95
    ctx = ctx_for(above, 30.0)
    assert C.describe(ctx, R.range_box_paths(ctx))["lean"] < -0.15


def test_range_box_without_a_pull_is_the_baseline():
    bars = synthetic_bars(drift=0.002)                             # a trending random walk: Dickey-Fuller cannot reject
    assert not R.fit_range_box(bars)["active"]
    ctx = ctx_for(bars, 30.0)
    assert np.allclose(R.range_box_paths(ctx), C.to_bins(ctx, C.base_pdf(ctx)))


def test_signals_are_cached_per_bars():
    ctx = ctx_for(hours=2.0)
    BY_ID["volume_poc_magnet"].fn(ctx)
    assert "volume_poc_magnet:signal" in ctx.cache
