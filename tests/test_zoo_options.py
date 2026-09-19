import numpy as np
import pytest
from zoo_contract import check_model, ctx_for

from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import options as O

BY_ID = {m.id: m for m in O.MODELS}
BULLISH = ("opt_covered_call", "opt_protective_put", "opt_bull_call_spread", "opt_long_combo", "opt_bull_call_ladder", "opt_collar",
           "opt_bullish_seagull", "opt_strap")
BEARISH = ("opt_covered_put", "opt_protective_call", "opt_bear_put_spread", "opt_short_combo", "opt_bear_put_ladder",
           "opt_bearish_seagull", "opt_strip")


@pytest.fixture(scope="module")
def ctx():
    return ctx_for(hours=24.0)


def masses(ctx, p):
    """Probability left of -2 sigma, inside one sigma, right of +2 sigma."""
    z = (ctx.edge_z[:-1] + ctx.edge_z[1:]) / 2
    return p[z < -2.0].sum(), p[np.abs(z) < 1.0].sum(), p[z > 2.0].sum()


@pytest.mark.parametrize("m", O.MODELS, ids=lambda m: m.id)
def test_contract(m):
    check_model(m)
    assert m.family == "Options views" and m.kind == "view"


def test_the_family_is_complete():
    assert len(O.MODELS) == 25 and len(BY_ID) == 25


def test_bullish_positions_lean_up_and_bearish_ones_down(ctx):
    for i in BULLISH:
        assert C.describe(ctx, BY_ID[i].fn(ctx))["lean"] > 0.1, i
    for i in BEARISH:
        assert C.describe(ctx, BY_ID[i].fn(ctx))["lean"] < -0.1, i


def test_mirror_positions_draw_mirror_pictures(ctx):
    # spot sits mid-bin, so compare stances rather than bins
    for up, down in zip(BULLISH[:4], BEARISH[:4], strict=True):
        a, b = C.describe(ctx, BY_ID[up].fn(ctx)), C.describe(ctx, BY_ID[down].fn(ctx))
        assert a["lean"] == pytest.approx(-b["lean"], abs=0.03) and a["width"] == pytest.approx(b["width"], abs=0.03)


def test_long_volatility_is_wider_and_short_volatility_is_tighter(ctx):
    for i in ("opt_long_straddle", "opt_short_butterfly", "opt_strap", "opt_strip"):
        assert C.describe(ctx, BY_ID[i].fn(ctx))["width"] > 1.1, i
    for i in ("opt_short_strangle", "opt_long_butterfly", "opt_iron_condor"):
        d = C.describe(ctx, BY_ID[i].fn(ctx))
        assert d["width"] < 0.9 and abs(d["lean"]) < 0.03, i


def test_backspreads_grow_the_tail_they_own(ctx):
    base = masses(ctx, C.to_bins(ctx, C.base_pdf(ctx)))
    calls, puts = masses(ctx, BY_ID["opt_call_ratio_backspread"].fn(ctx)), masses(ctx, BY_ID["opt_put_ratio_backspread"].fn(ctx))
    assert calls[2] > 1.5 * base[2] and calls[0] < 1.3 * base[0]
    assert puts[0] > 1.5 * base[0] and puts[2] < 1.3 * base[2]


def test_ratio_spreads_cut_the_tail_they_are_short(ctx):
    base = masses(ctx, C.to_bins(ctx, C.base_pdf(ctx)))
    assert masses(ctx, BY_ID["opt_ratio_call_spread"].fn(ctx))[2] < 0.6 * base[2]
    assert masses(ctx, BY_ID["opt_ratio_put_spread"].fn(ctx))[0] < 0.6 * base[0]


def test_calendar_spread_pins_the_first_day_only():
    fn = BY_ID["opt_calendar_spread"].fn
    near, far = ctx_for(hours=12.0), ctx_for(hours=48.0)
    assert C.describe(near, fn(near))["width"] < 0.9
    assert np.allclose(fn(far), C.to_bins(far, C.base_pdf(far)))
