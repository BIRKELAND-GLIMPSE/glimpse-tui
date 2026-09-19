import numpy as np
import pandas as pd
import pytest
from zoo_contract import NOW, check_model, ctx_for, synthetic_bars

from glimpse_tui.zoo import calendar as cal
from glimpse_tui.zoo import core as C
from glimpse_tui.zoo.core import Ctx

BY_ID = {m.id: m for m in cal.MODELS}


def shifted_ctx(hours_back: float, n: int = 1500, hours: float = 2.0) -> Ctx:
    """The synthetic bars and clock moved `hours_back` hours into the past, to stand at a chosen calendar moment."""
    bars = synthetic_bars(n)
    bars.index = bars.index - pd.Timedelta(hours=hours_back)
    base = ctx_for(bars, hours)
    return Ctx(bars=bars, spot=base.spot, edges=base.edges, now=base.now - hours_back * 3600, end=base.end - hours_back * 3600)


@pytest.mark.parametrize("m", cal.MODELS, ids=lambda m: m.id)
def test_contract(m):
    check_model(m)
    assert m.family == "Calendar & cycles" and m.factors == ("direction", "timing")


def test_cycle_and_ssa_follow_the_direction_of_the_price():
    for mid in ("ssa_trend", "fourier_cycle"):
        leans = []
        for phase in (0.0, np.pi):          # a 96-hour wave, rising at the last bar or falling
            bars = synthetic_bars(vol=0.0005)
            t = np.arange(len(bars), dtype=float)
            wave = np.exp(0.03 * np.sin(2 * np.pi * (t - t[-1]) / 96.0 + phase))
            bars = bars.assign(**{k: bars[k] * wave for k in ("open", "high", "low", "close")})
            ctx = ctx_for(bars, hours=6.0)
            leans.append(C.describe(ctx, BY_ID[mid].fn(ctx))["lean"])
        assert leans[0] > 0.1 and leans[1] < -0.1, (mid, leans)


def test_hour_of_day_picks_up_a_planted_strong_hour():
    bars = synthetic_bars()
    hours = pd.DatetimeIndex(bars.index).hour.to_numpy()
    nxt = (hours[-1] + 1 + np.arange(12)) % 24                    # the twelve hours after the last bar
    r = np.diff(np.log(bars["close"].to_numpy()), prepend=np.log(bars["close"].iloc[0]))
    for sign in (1, -1):
        planted = 78_000 * np.exp(np.cumsum(r + sign * 0.002 * np.isin(hours, nxt) - sign * 0.002 * ~np.isin(hours, nxt)))
        b = bars.assign(open=planted, high=planted * 1.001, low=planted * 0.999, close=planted)
        ctx = ctx_for(b, hours=6.0, spot=float(planted[-1]))
        edges = planted[-1] * np.exp(np.linspace(-0.2, 0.2, 501))
        ctx = Ctx(bars=b, spot=float(planted[-1]), edges=edges, now=ctx.now, end=ctx.end)
        assert np.sign(C.describe(ctx, cal.hour_of_day_drift(ctx))["lean"]) == sign


def test_day_of_week_signal_is_causal_and_signed():
    bars = synthetic_bars(2000)
    idx = pd.DatetimeIndex(bars.index)
    r = np.diff(np.log(bars["close"].to_numpy()), prepend=np.log(bars["close"].iloc[0]))
    tomorrow = (idx.dayofweek[-1] + 1) % 7
    planted = 78_000 * np.exp(np.cumsum(r + 0.001 * (idx.dayofweek.to_numpy() == tomorrow)))
    sig = cal.day_of_week_signal(bars.assign(close=planted))
    assert sig.iloc[-1] > 0
    cut = cal.day_of_week_signal(bars.assign(close=planted).iloc[:-100])
    assert np.allclose(cut.dropna(), sig.iloc[:-100].dropna())


def test_turn_of_month_window():
    idx = pd.date_range("2026-08-28", "2026-09-05", freq="h", tz="UTC")
    w = cal.turn_of_month_weight(idx)
    assert w[pd.Timestamp("2026-08-29 12:00", tz="UTC")] == 0.0
    assert w[pd.Timestamp("2026-08-30 23:00", tz="UTC")] == 1.0          # the next 24 bars all open on the 31st
    assert w[pd.Timestamp("2026-09-02 23:00", tz="UTC")] == 0.0
    assert 0 < w[pd.Timestamp("2026-09-02 10:00", tz="UTC")] < 1
    inside = shifted_ctx(24 * 16)                                         # 2 September: inside the window
    assert C.describe(inside, cal.turn_of_month(inside))["lean"] > 0.1
    outside = ctx_for()                                                   # 18 September: nowhere near it
    assert np.allclose(cal.turn_of_month(outside), C.to_bins(outside, C.base_pdf(outside)))


def test_pre_fomc_leans_up_on_the_eve_only_and_keeps_the_baseline_off_calendar():
    fomc = cal.macro_events()["fomc"]
    assert pd.Timestamp(fomc[5]) == pd.Timestamp("2026-09-16 18:00", tz="UTC")     # 14:00 New York, daylight time
    assert pd.Timestamp(fomc[0]) == pd.Timestamp("2026-01-28 19:00", tz="UTC")     # standard time
    assert pd.Timestamp(NOW, unit="s", tz="UTC") == pd.Timestamp("2026-09-18 00:20", tz="UTC")
    eve = shifted_ctx(34)                                                 # 16 September 14:20 UTC, decision at 18:00
    assert C.describe(eve, cal.pre_fomc_drift(eve))["lean"] > 0.1
    after = ctx_for()
    assert np.allclose(cal.pre_fomc_drift(after), C.to_bins(after, C.base_pdf(after)))
    beyond = shifted_ctx(-24 * 500)                                       # 2028: the list has run out
    assert not cal.covers_now(beyond, ("fomc",))
    assert np.allclose(cal.pre_fomc_drift(beyond), C.to_bins(beyond, C.base_pdf(beyond)))


def test_stepped_signals_are_causal():
    bars = synthetic_bars(1000)
    full = cal.ssa_signal(bars)
    cal._STEP_MEMO.clear()
    cut = cal.ssa_signal(bars.iloc[:-50])
    assert full.notna().sum() > 200
    assert np.allclose(cut.dropna(), full.iloc[:-50].dropna())
