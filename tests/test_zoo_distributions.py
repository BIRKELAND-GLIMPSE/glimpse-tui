import time

import numpy as np
import pytest
from zoo_contract import check_model, ctx_for, synthetic_bars

from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import distributions as D

BY_ID = {m.id: m for m in D.MODELS}


def z_stats(ctx, p):
    """Mean, sd, skewness and the mass beyond 3 sigma on each side of the picture, in standardised units."""
    z = (np.log(ctx.edges[:-1]) + np.log(ctx.edges[1:])) / 2
    z = (z - ctx.log_spot) / ctx.sigma
    m = float(np.dot(p, z))
    sd = float(np.sqrt(np.dot(p, (z - m) ** 2)))
    return m, sd, float(np.dot(p, ((z - m) / sd) ** 3)), float(p[z < -3].sum()), float(p[z > 3].sum())


def peaks(p, share=0.25):
    """Local maxima at least `share` as tall as the tallest bin."""
    mid = p[1:-1]
    return int(np.sum((mid > p[:-2]) & (mid >= p[2:]) & (mid > share * p.max())))


@pytest.mark.parametrize("m", D.MODELS, ids=lambda m: m.id)
def test_contract(m):
    check_model(m)


def test_all_26_lab_models_are_here_with_the_lab_kinds():
    assert len(D.MODELS) == 26 and all(m.family == "Distributions" and m.factors for m in D.MODELS)
    fitted = {"dist_student_t", "dist_skew_normal", "dist_merton_jumps", "dist_regime_mixture", "dist_empirical_history",
              "dist_variance_gamma"}
    assert {m.id for m in D.MODELS if m.kind == "forecast"} == fitted


def test_shapes_are_centred_and_unit_width_unless_width_is_the_point():
    ctx = ctx_for(hours=30.0)
    wide = {"dist_cauchy", "dist_barbell"}
    for m in D.MODELS:
        mean, sd, *_ = z_stats(ctx, m.fn(ctx))
        if m.id not in wide:
            assert abs(mean) < 0.12 and 0.85 < sd < 1.25, (m.id, mean, sd)
    assert z_stats(ctx, BY_ID["dist_barbell"].fn(ctx))[1] > 1.3


def test_skewed_shapes_point_the_way_their_names_say():
    ctx = ctx_for(hours=30.0)
    for up, down in (("dist_chi_squared_up", "dist_chi_squared_down"), ("dist_gumbel_up", "dist_gumbel_crash"),
                     ("dist_pareto_tail_up", "dist_pareto_tail_down")):
        pu, pd_ = BY_ID[up].fn(ctx), BY_ID[down].fn(ctx)
        su, sd_ = z_stats(ctx, pu), z_stats(ctx, pd_)
        assert su[2] > 0.3 and sd_[2] < -0.3, (up, su, sd_)
        assert su[4] > 2 * su[3] and sd_[3] > 2 * sd_[4]
        # a long right tail puts the median below the mean, and the other way round
        assert C.describe(ctx, pu)["median"] < ctx.spot < C.describe(ctx, pd_)["median"]
        assert abs(C.describe(ctx, pu)["lean"]) < 0.12                  # the lean is the mean, and these shapes are centred
    assert z_stats(ctx, BY_ID["dist_normal_in_price"].fn(ctx))[2] < -0.1        # log of a bell curve leans left
    # the triangle's peak sits above spot and nothing lies beyond its far edge
    p = BY_ID["dist_triangular"].fn(ctx)
    zc = (np.log(ctx.edges[1:]) - ctx.log_spot) / ctx.sigma
    assert zc[np.argmax(p)] > 0.3 and p[zc > 3.0].sum() < 1e-6


def test_tails_and_peaks_against_the_bell_curve():
    ctx = ctx_for(hours=30.0)
    normal = C.to_bins(ctx, C.t_pdf(200.0))
    tail = lambda p: z_stats(ctx, p)[3] + z_stats(ctx, p)[4]  # noqa: E731
    for mid in ("dist_student_t", "dist_laplace", "dist_cauchy", "dist_logistic", "dist_variance_gamma", "dist_pinned"):
        assert tail(BY_ID[mid].fn(ctx)) > 1.5 * tail(normal), mid
    for mid in ("dist_laplace", "dist_pinned"):
        assert BY_ID[mid].fn(ctx).max() > 1.2 * normal.max(), mid
    assert C.describe(ctx, BY_ID["dist_pinned"].fn(ctx))["width"] < 0.6
    assert tail(BY_ID["dist_uniform_box"].fn(ctx)) < 1e-6


def test_hollow_and_multimodal_shapes():
    ctx = ctx_for(hours=30.0)
    centre = int(np.searchsorted(ctx.edges, ctx.spot)) - 1
    for mid in ("dist_arcsine", "dist_barbell", "dist_bimodal_breakout"):
        p = BY_ID[mid].fn(ctx)
        assert p[centre] < 0.5 * p.max(), mid
    assert peaks(C.to_bins(ctx, C.t_pdf(200.0))) == 1
    assert peaks(BY_ID["dist_bimodal_breakout"].fn(ctx)) == 2
    assert peaks(BY_ID["dist_trimodal_scenarios"].fn(ctx)) == 3
    box = BY_ID["dist_uniform_box"].fn(ctx)
    inside = box[box > 0.8 * box.max()]
    assert inside.max() / inside.min() < 1.15                                   # flat top, in log price


def test_lattices_keep_their_spikes():
    ctx = ctx_for(hours=30.0)
    assert peaks(BY_ID["dist_binomial_lattice"].fn(ctx), share=0.01) == 7
    assert peaks(BY_ID["dist_poisson_lattice"].fn(ctx), share=0.05) >= 8
    comb = BY_ID["dist_round_number_comb"].fn(ctx)
    centres = (ctx.edges[:-1] + ctx.edges[1:]) / 2
    tops = centres[1:-1][(comb[1:-1] > comb[:-2]) & (comb[1:-1] >= comb[2:]) & (comb[1:-1] > 0.2 * comb.max())]
    assert len(tops) >= 3 and np.all(np.abs(tops / 1000.0 - np.round(tops / 1000.0)) <= 0.2)
    # the Poisson comb has fewer teeth close to the close than far from it
    near, far = ctx_for(hours=2.0), ctx_for(hours=30.0)
    assert D.dist_poisson_lattice(near).max() > D.dist_poisson_lattice(far).max()


def test_fits_follow_the_history():
    rng = np.random.default_rng(3)
    bars = synthetic_bars(n=2000)
    crash = bars.copy()
    r = np.diff(np.log(bars["close"].to_numpy()))
    hit = rng.choice(r.size, 40, replace=False)
    r[hit] -= 0.03                                                              # grind up, fall down stairs
    crash["close"] = bars["close"].iloc[0] * np.exp(np.concatenate([[0.0], np.cumsum(r)]))
    ctx = ctx_for(crash, hours=24.0, spot=78_000.0)
    assert D._skew_alpha(ctx) < -0.5
    assert z_stats(ctx, D.dist_skew_normal(ctx))[2] < -0.1
    assert z_stats(ctx, D.dist_empirical_history(ctx))[2] < -0.1
    lam, size = D._merton_fit(ctx)
    assert lam > D._merton_fit(ctx_for(bars, hours=24.0))[0] and size > 3.0


def test_merton_jump_count_scales_with_the_horizon():
    bars = synthetic_bars()
    short, long_ = ctx_for(bars, 2.0), ctx_for(bars, 30.0)
    assert not np.allclose(D.dist_merton_jumps(short), D.dist_student_t(short))
    k = lambda ctx: z_stats(ctx, D.dist_variance_gamma(ctx))[3]  # noqa: E731
    assert k(short) > k(long_)                                                  # variance-gamma tails fade with the horizon


def test_cached_calls_are_fast():
    ctx = ctx_for(hours=30.0)
    for m in D.MODELS:
        m.fn(ctx)
        t = time.perf_counter()
        m.fn(ctx)
        assert time.perf_counter() - t < 0.05, m.id
