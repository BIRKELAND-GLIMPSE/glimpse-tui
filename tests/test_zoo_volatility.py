import numpy as np
import pandas as pd
import pytest
from zoo_contract import check_model, ctx_for, synthetic_bars

from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import volatility as V
from glimpse_tui.zoo.core import Ctx

BY_ID = {m.id: m for m in V.MODELS}


def width(ctx: Ctx, mid: str) -> float:
    d = C.describe(ctx, BY_ID[mid].fn(ctx))
    assert abs(d["lean"]) < 0.05, (mid, d)            # a volatility model never leans
    return d["width"]


def with_last_day(bars: pd.DataFrame, move: float = 0.0, vol_mult: float = 1.0, volume_mult: float = 1.0) -> pd.DataFrame:
    """The same bars with the last 24 hours rewritten: a total log move, rescaled returns, rescaled volume."""
    r = np.diff(np.log(bars["close"].to_numpy()), prepend=np.log(bars["close"].iloc[0]))
    r[-24:] = r[-24:] * vol_mult + move / 24
    close = bars["close"].iloc[0] * np.exp(np.cumsum(r) - r[0])
    k = close / bars["close"].to_numpy()
    out = bars.assign(**{c: bars[c] * k for c in ("open", "high", "low", "close")})
    out.loc[out.index[-24:], "volume"] *= volume_mult
    return out


@pytest.mark.parametrize("m", V.MODELS, ids=lambda m: m.id)
def test_contract(m):
    check_model(m)
    assert m.factors == ("volatility",)


def test_a_sell_off_widens_and_a_rally_narrows():
    bars = synthetic_bars()
    assert width(ctx_for(with_last_day(bars, move=-0.08), 24.0), "leverage_effect_vol") > 1.1
    assert width(ctx_for(with_last_day(bars, move=+0.08), 24.0), "leverage_effect_vol") < 0.9


def test_high_volatility_narrows_the_reversion_picture_and_quiet_widens_it():
    bars = synthetic_bars()
    r = np.diff(np.log(bars["close"].to_numpy()), prepend=np.log(bars["close"].iloc[0]))
    for mult, wider in ((3.0, False), (0.2, True)):
        rr = r.copy()
        rr[-72:] *= mult
        close = 78_000 * np.exp(np.cumsum(rr) - np.cumsum(rr)[-1])
        b = bars.assign(open=close, high=close * 1.001, low=close * 0.999, close=close)
        assert (width(ctx_for(b, 24.0), "vol_mean_reversion") > 1.0) == wider


def test_a_squeeze_widens():
    bars = synthetic_bars()
    assert width(ctx_for(with_last_day(bars, vol_mult=0.02), 24.0), "vol_squeeze") > 1.1
    assert width(ctx_for(with_last_day(bars, vol_mult=6.0), 24.0), "vol_squeeze") < 0.95


def test_heavy_volume_widens_and_thin_volume_narrows():
    bars = synthetic_bars()
    assert width(ctx_for(with_last_day(bars, volume_mult=5.0), 24.0), "volume_surge_vol") > 1.1
    assert width(ctx_for(with_last_day(bars, volume_mult=0.2), 24.0), "volume_surge_vol") < 0.9


def test_range_vol_follows_the_sign_history_gives_it():
    bars = synthetic_bars()
    sig = V.range_signal(bars)
    sign = V.history_sign(bars, sig)
    assert sign in (1, -1)
    assert V.history_sign(bars, -sig) == -sign
    assert V.history_sign(bars.iloc[:150], sig.iloc[:150]) == 0          # too little history: no opinion
    ctx = ctx_for(bars, 24.0)
    expect = np.exp(0.25 * sign * C.strength_of(sig))
    assert width(ctx, "range_vol") == pytest.approx(expect, rel=0.05)


def test_weekday_ratio_is_causal_and_sees_a_planted_busy_day():
    bars = synthetic_bars(2000)
    idx = pd.DatetimeIndex(bars.index)
    r = np.diff(np.log(bars["close"].to_numpy()), prepend=np.log(bars["close"].iloc[0]))
    busy = (idx[-1] + pd.Timedelta(hours=12)).dayofweek
    r = r * np.where(idx.dayofweek == busy, 3.0, 1.0)
    b = bars.assign(close=78_000 * np.exp(np.cumsum(r)))
    sig = V.weekday_hour_ratio(b)
    assert sig.iloc[-1] > 0.2
    assert np.allclose(V.weekday_hour_ratio(b.iloc[:-60]).dropna(), sig.iloc[:-60].dropna())
    assert V.weekday_hour_ratio(synthetic_bars(1200)).notna().sum() == 0          # under eight weeks: silent


def test_event_vol_widens_into_a_release_and_keeps_the_baseline_otherwise():
    def at(hours_back: float) -> Ctx:
        bars = synthetic_bars()
        bars.index = bars.index - pd.Timedelta(hours=hours_back)
        c = ctx_for(bars, 2.0)
        return Ctx(bars=bars, spot=c.spot, edges=c.edges, now=c.now - hours_back * 3600, end=c.end - hours_back * 3600)

    eve = at(34)                                   # 16 September 2026 14:20 UTC, the Fed decides at 18:00
    assert width(eve, "event_vol") > 1.1
    quiet = ctx_for()                              # 18 September: the next release is payrolls on 2 October
    assert np.allclose(V.event_vol(quiet), C.to_bins(quiet, C.base_pdf(quiet)))
    off = at(-24 * 200)                            # April 2027: CPI and payrolls dates are not listed
    assert np.allclose(V.event_vol(off), C.to_bins(off, C.base_pdf(off)))


def test_har_rv_width_follows_the_candle_ranges():
    bars = synthetic_bars()
    mid = (bars["high"] + bars["low"]) / 2
    half = (bars["high"] - bars["low"]) / 2

    def ranged(k: float) -> pd.DataFrame:
        return bars.assign(high=mid + k * half, low=mid - k * half)

    narrow, wide = (width(ctx_for(ranged(k), 6.0), "har_rv") for k in (0.3, 1.5))
    assert narrow < 0.9 < 1.1 < wide
    fit = V.har_fit(bars)
    assert V.har_variance(fit, 0.0, 3600.0) == pytest.approx(fit["iv"][0] * fit["profile"][0] ** 2)
    assert V.har_variance(fit, 0.0, 7200.0) > V.har_variance(fit, 1800.0, 7200.0) > 0
    with pytest.raises(C.ModelUnavailable):
        V.har_fit(bars.assign(high=bars["close"], low=bars["close"]))
