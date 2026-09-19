import numpy as np
import pytest
from scipy import stats
from zoo_contract import check_model, ctx_for, synthetic_bars

from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import scenarios as S

BY_ID = {m.id: m for m in S.MODELS}


def stance(model_id, ctx):
    return C.describe(ctx, BY_ID[model_id].fn(ctx))


@pytest.mark.parametrize("m", S.MODELS, ids=lambda m: m.id)
def test_contract(m):
    check_model(m)
    assert m.family == "Scenario" and m.kind == "view"


def test_the_family_is_complete():
    assert len(S.MODELS) == 15 and len(BY_ID) == 15


@pytest.mark.parametrize("hours", [2.0, 24.0])
def test_bullish_views_lean_up_and_bearish_views_lean_down_in_order(hours):
    ctx = ctx_for(synthetic_bars(n=2000), hours)
    lean = {i: stance(i, ctx)["lean"] for i in BY_ID}
    assert lean["view_breakout_up"] > lean["view_bull"] > lean["view_mild_bull"] > lean["view_slow_grind_up"] > 0.05
    assert lean["view_breakout_down"] < lean["view_bear"] < lean["view_mild_bear"] < lean["view_slow_bleed"] < -0.05
    assert stance("view_bull", ctx)["view"] == "bullish" and stance("view_bear", ctx)["view"] == "bearish"
    assert abs(lean["view_neutral"]) < 0.02 and abs(lean["view_high_vol"]) < 0.02


def test_quantile_drift_lands_near_the_normal_quantile_and_inside_the_bounds():
    ctx = ctx_for(synthetic_bars(n=2000), 1.0 + 40 / 60)
    for model_id, q in (("view_bull", 0.8), ("view_bear", 0.2), ("view_breakout_up", 0.9), ("view_slow_grind_up", 0.6)):
        z = S._quantile_z(ctx, model_id, q)
        assert abs(z - stats.norm.ppf(q)) < 0.35, (model_id, z)
        assert abs(z) <= S.Z_BOUND


def test_a_view_never_crosses_to_the_other_side_even_in_a_one_way_market():
    for drift, up_ok, down_ok in ((-0.002, False, True), (0.002, True, False)):
        ctx = ctx_for(synthetic_bars(drift=drift), 24.0)
        bull, bear = stance("view_bull", ctx)["lean"], stance("view_bear", ctx)["lean"]
        assert -0.02 < bull <= S.Z_BOUND + 0.05 and -S.Z_BOUND - 0.05 <= bear < 0.02
        assert (bull > 0.3) == up_ok and (bear < -0.3) == down_ok


def test_widths_follow_the_volatility_multiplier():
    ctx = ctx_for(hours=24.0)
    assert stance("view_high_vol", ctx)["width"] > 1.35 and stance("view_high_vol", ctx)["view"] == "volatile"
    assert stance("view_neutral", ctx)["width"] < 0.9 and stance("view_neutral", ctx)["view"] == "sideways"
    assert stance("view_breakout_up", ctx)["width"] > 1.15 and stance("view_slow_bleed", ctx)["width"] < 0.9


def test_scripted_paths_follow_the_script():
    assert S.v_rebound_z(24.0) == pytest.approx(-0.8) and S.v_rebound_z(168.0) == pytest.approx(0.3)
    assert S.dead_cat_bounce_z(24.0) == pytest.approx(0.5) and S.dead_cat_bounce_z(168.0) == pytest.approx(-0.5)
    bars = synthetic_bars()
    assert stance("view_v_rebound", ctx_for(bars, 24.0))["lean"] < -0.6
    assert stance("view_dead_cat_bounce", ctx_for(bars, 24.0))["lean"] > 0.35
    assert stance("view_v_rebound", ctx_for(bars, 160.0))["lean"] > 0.15
    assert stance("view_dead_cat_bounce", ctx_for(bars, 160.0))["lean"] < -0.3


def test_tail_scenarios_fatten_the_right_tails():
    ctx = ctx_for(hours=24.0)
    base = C.to_bins(ctx, C.base_pdf(ctx))
    z_mid = (ctx.edge_z[:-1] + ctx.edge_z[1:]) / 2
    left, right = z_mid < -2.0, z_mid > 2.0
    crash, melt, binary = (BY_ID[i].fn(ctx) for i in ("view_crash", "view_melt_up", "view_binary_event"))
    assert crash[left].sum() > 1.6 * base[left].sum() and crash[right].sum() < 1.2 * base[right].sum()
    assert melt[right].sum() > 1.6 * base[right].sum() and melt[left].sum() < 1.2 * base[left].sum()
    assert binary[left].sum() > 1.5 * base[left].sum() and binary[right].sum() > 1.5 * base[right].sum()
    assert np.isclose(binary[left].sum(), binary[right].sum(), rtol=0.2)
