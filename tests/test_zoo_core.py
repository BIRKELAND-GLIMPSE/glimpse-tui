import numpy as np
import pytest
from zoo_contract import check_model, ctx_for, synthetic_bars

from glimpse_tui import zoo
from glimpse_tui.zoo import core as C


@pytest.mark.parametrize("m", zoo.catalog(), ids=lambda m: m.id)
def test_every_model_meets_the_contract(m):
    check_model(m)


def test_ids_are_unique_and_families_known():
    ms = zoo.catalog()
    assert len({m.id for m in ms}) == len(ms)
    assert {m.family for m in ms} <= set(zoo.FAMILIES)


def test_a_bullish_signal_leans_the_picture_up_and_a_flat_one_leaves_the_baseline():
    ctx = ctx_for(hours=24.0)
    n = len(ctx.bars)
    up = C.describe(ctx, C.conviction_picture(ctx, np.linspace(-1, 1, n)))
    down = C.describe(ctx, C.conviction_picture(ctx, np.linspace(1, -1, n)))
    assert up["lean"] > 0.3 and down["lean"] < -0.3 and up["view"] == "bullish" and down["view"] == "bearish"
    none = C.conviction_picture(ctx, np.full(n, np.nan))
    assert np.allclose(none, C.to_bins(ctx, C.base_pdf(ctx)))


def test_volatility_signal_changes_width_only():
    ctx = ctx_for(hours=24.0)
    wide = C.describe(ctx, C.vol_picture(ctx, np.linspace(0, 1, len(ctx.bars))))
    assert wide["width"] > 1.15 and abs(wide["lean"]) < 0.05


def test_tilt_respects_the_effective_sample_floor():
    ctx = ctx_for(hours=24.0)
    base = C.base_pdf(ctx)
    bent = C.tilt(base, C.call(C.Z, 1.0), 5.0)             # an absurd conviction is capped, not obeyed
    w = bent / np.maximum(base, 1e-300)
    ess = 1.0 / np.dot(bent * C.DZ, w)
    assert ess >= C.ESS_FLOOR - 0.02


def test_sigma_grows_with_the_horizon_and_the_first_hour_is_partial():
    bars = synthetic_bars()
    assert ctx_for(bars, 0.4).sigma < ctx_for(bars, 2.0).sigma < ctx_for(bars, 30.0).sigma


def test_a_signal_that_is_silent_reads_as_neutral():
    sig = np.zeros(1500)
    sig[::7] = np.linspace(-1, 1, len(sig[::7]))          # speaks one hour in seven
    sig[-1] = 0.0
    assert abs(C.strength_of(sig)) < 0.05
