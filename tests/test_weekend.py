"""The weekend regime: the calendar damps a model's direction. A close inside the quiet hours (Saturday and Sunday for
Bitcoin, CME's closed hours for gold and oil) is bet sideways, a close partly through them keeps the busy share of its lean,
and the width multipliers are left to the clock, which already narrows quiet hours. Offline, synthetic bars."""
from __future__ import annotations

import numpy as np
import pytest
from test_clock import at, bars_with_quiet

from glimpse_tui import zoo
from glimpse_tui.zoo import core as C

EDGES = 28_000.0 + 200.0 * np.arange(501)


def ctx_at(bars, now: str, end: str, asset: str = "BTC", scale: C.Scale | None = None) -> C.Ctx:
    return C.Ctx(bars=bars, spot=float(bars["close"].iloc[-1]), edges=EDGES, now=at(now), end=at(end), asset=asset, scale=scale)


def bullish(ctx: C.Ctx) -> np.ndarray:
    return np.linspace(-1.0, 1.0, len(ctx.bars))


def test_a_close_inside_saturday_is_bet_sideways_whatever_the_signal_says():
    bars = bars_with_quiet("always")
    ctx = ctx_at(bars, "2026-10-31 08:00", "2026-10-31 12:00")                      # Saturday morning, a Saturday close
    assert ctx.busy_share == 0.0 and ctx.lean_scale == 0.0
    base = C.to_bins(ctx, C.base_pdf(ctx))
    pic = C.conviction_picture(ctx, bullish(ctx))
    d = C.describe(ctx, pic)
    assert np.allclose(pic, base) and abs(d["lean"]) < 0.02 and d["view"] in ("sideways", "neutral")
    assert np.allclose(C.view_picture(ctx, mean_z=1.0), base)                       # a stated view goes sideways too
    assert np.allclose(C.payoff_picture(ctx, lambda z, h: C.call(z, 0.5), conviction=0.5), base)
    wide = C.describe(ctx, C.conviction_picture(ctx, bullish(ctx), vol_mult=1.4))
    assert abs(wide["lean"]) < 0.02 and wide["width"] > 1.3                         # the width multiplier is not the calendar's


def test_a_close_across_the_weekend_keeps_the_busy_share_of_its_lean():
    bars = bars_with_quiet("always")
    over = ctx_at(bars, "2026-10-30 12:00", "2026-11-02 12:00")                     # Friday noon to Monday noon: 24 busy of 72
    week = ctx_at(bars, "2026-10-26 12:00", "2026-10-29 12:00")                     # Monday noon to Thursday noon, all busy
    assert over.busy_share == pytest.approx(1 / 3) and week.busy_share == 1.0
    lean_over = C.describe(over, C.conviction_picture(over, bullish(over)))["lean"]
    lean_week = C.describe(week, C.conviction_picture(week, bullish(week)))["lean"]
    assert lean_week > 0.3 and 0 < lean_over < lean_week
    assert lean_over / lean_week == pytest.approx(1 / 3, abs=0.08)
    view_over = C.describe(over, C.view_picture(over, mean_z=0.9))["lean"]
    view_week = C.describe(week, C.view_picture(week, mean_z=0.9))["lean"]
    assert view_over == pytest.approx(view_week / 3, abs=0.03)


def test_gold_damps_cme_closed_hours_and_a_clock_without_a_calendar_damps_nothing():
    gold = bars_with_quiet("cme", 0.0025, 0.0007)
    fri = ctx_at(gold, "2026-10-30 21:00", "2026-10-31 01:00", asset="XAU")        # 21:00 open, then CME shuts at 22:00
    assert fri.scale.session == "cme" and fri.busy_share == pytest.approx(0.25) and fri.lean_scale == pytest.approx(0.25)
    wed = ctx_at(gold, "2026-10-28 21:00", "2026-10-29 01:00", asset="XAU")        # only the 22:00 hour off
    assert wed.busy_share == pytest.approx(0.75)
    half = ctx_at(gold, "2026-10-30 21:30", "2026-10-30 22:30", asset="XAU")       # partial steps count their fraction
    assert half.busy_share == pytest.approx(0.5)
    reopen = ctx_at(gold, "2026-11-01 22:30", "2026-11-02 00:30", asset="XAU")     # CME reopens Sunday 23:00
    assert reopen.busy_share == pytest.approx(0.75)
    sat = ctx_at(gold, "2026-10-31 10:00", "2026-10-31 14:00", asset="XAU")
    assert sat.lean_scale == 0.0 and C.describe(sat, C.conviction_picture(sat, bullish(sat)))["view"] in ("sideways", "neutral")
    still = ctx_at(gold, "2026-10-31 10:00", "2026-11-01 14:00", asset="DIFFICULTY", scale=C.Scale.still())
    assert still.scale.hours_quiet(still.now, still.end) == 0.0 and still.busy_share == 1.0 and still.lean_scale == 1.0
    assert C.describe(still, C.conviction_picture(still, bullish(still)))["lean"] > 0.3
    done = ctx_at(gold, "2026-10-31 10:00", "2026-10-31 09:00", asset="XAU")        # a close already past: nothing to damp
    assert done.busy_share == 1.0


def test_weekend_lean_is_a_module_constant_the_desk_may_override(monkeypatch):
    bars = bars_with_quiet("always")
    sat = ctx_at(bars, "2026-10-31 08:00", "2026-10-31 12:00")
    over = ctx_at(bars, "2026-10-30 12:00", "2026-11-02 12:00")
    monkeypatch.setattr(C, "WEEKEND_LEAN", 0.4)
    assert sat.lean_scale == pytest.approx(0.4) and over.lean_scale == pytest.approx(0.4 + 0.6 / 3)
    assert C.describe(sat, C.conviction_picture(sat, bullish(sat)))["lean"] > 0.1
    monkeypatch.setattr(C, "WEEKEND_LEAN", 1.0)
    assert sat.lean_scale == 1.0


@pytest.mark.parametrize("mid", ["ema_crossover", "rsi_reversion", "view_bull", "opt_covered_call", "opt_bull_call_spread", "session_walk"])
def test_models_drawn_for_a_weekend_close_sit_on_the_narrowed_baseline(mid):
    bars = bars_with_quiet("always")
    sat = ctx_at(bars, "2026-10-31 08:00", "2026-10-31 20:00")
    m = zoo.get(mid)
    p = np.asarray(m.fn(sat), dtype=float)
    assert p.shape == (500,) and abs(p.sum() - 1.0) < 1e-9 and np.all(p > 0)
    d = C.describe(sat, p)
    assert abs(d["lean"]) < 0.05, (mid, d)
