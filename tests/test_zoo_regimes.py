import numpy as np
import pandas as pd
from zoo_contract import NOW, check_model, ctx_for

from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import regimes as R


def bars_from(r: np.ndarray) -> pd.DataFrame:
    close = 78_000 * np.exp(np.cumsum(r) - np.sum(r))
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range(end=pd.Timestamp((int(NOW) // 3600 - 1) * 3600, unit="s", tz="UTC"), periods=r.size, freq="h")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.0005, "low": np.minimum(open_, close) * 0.9995,
                         "close": close, "volume": np.full(r.size, 100.0)}, index=idx)


def blocks(values: list[float], length: int) -> np.ndarray:
    return np.repeat(np.array(values), length)


def test_every_model_meets_the_contract():
    assert len({m.id for m in R.MODELS}) == len(R.MODELS)
    for m in R.MODELS:
        check_model(m)


def test_markov_fit_recovers_two_volatility_states():
    rng = np.random.default_rng(1)
    sd = blocks([1.0, 3.0] * 5, 200)
    fit = R.markov_switching_fit(rng.standard_normal(sd.size) * sd, switching_mean=False)
    lo, hi = np.sort(np.sqrt(fit["var"]))
    assert 0.85 < lo < 1.15 and 2.6 < hi < 3.4 and min(fit["stay"]) > 0.95
    storm = fit["filtered"][:, int(np.argmax(fit["var"]))]
    assert storm[sd == 3.0].mean() > 0.9 and storm[sd == 1.0].mean() < 0.1


def test_markov_regime_leans_with_the_state_the_market_is_in():
    rng = np.random.default_rng(2)
    noise = rng.standard_normal(1600) * 0.002
    up = C.describe(*picture(R.markov_regime, bars_from(noise + blocks([-0.002, 0.002] * 4, 200))))
    down = C.describe(*picture(R.markov_regime, bars_from(noise + blocks([0.002, -0.002] * 4, 200))))
    assert up["lean"] > 0.2 and down["lean"] < -0.2


def picture(fn, bars, hours: float = 24.0):
    ctx = ctx_for(bars, hours)
    return ctx, fn(ctx)


def test_changepoint_drift_follows_a_fresh_regime():
    rng = np.random.default_rng(3)
    noise = rng.standard_normal(1500) * 0.003
    turn = np.concatenate([np.zeros(1380), np.full(120, 0.0015)])
    assert C.describe(*picture(R.changepoint_drift, bars_from(noise + turn)))["lean"] > 0.3
    assert C.describe(*picture(R.changepoint_drift, bars_from(noise - turn)))["lean"] < -0.3


def tail_mass(ctx: C.Ctx, p: np.ndarray, k: float = 3.0) -> float:
    z = ctx.edge_z
    return float(p[z[1:] < -k].sum() + p[z[:-1] > k].sum())


def tail_ratio(ctx: C.Ctx, p: np.ndarray) -> float:
    """The 1%-99% span over the 25%-75% span: how heavy the tails are for the picture's own width."""
    q = np.interp([0.01, 0.25, 0.75, 0.99], np.cumsum(p), ctx.edge_z[1:])
    return float((q[3] - q[0]) / (q[2] - q[1]))


def test_kou_adds_jump_tails_only_when_history_has_jumps():
    rng = np.random.default_rng(4)
    r = rng.standard_normal(1500) * 0.003
    ctx, p = picture(R.kou_jump_paths, bars_from(r), 6.0)
    assert not R.kou_structure(ctx)["active"] and np.allclose(p, C.to_bins(ctx, C.base_pdf(ctx)))
    r[rng.choice(np.arange(200, 1400), 25, replace=False)] += rng.choice([-1, 1], 25) * 0.03
    ctx, p = picture(R.kou_jump_paths, bars_from(r), 6.0)
    assert R.kou_structure(ctx)["active"]
    assert tail_mass(ctx, p) > 2 * tail_mass(ctx, C.to_bins(ctx, C.base_pdf(ctx)))


def test_hawkes_needs_clustered_jumps():
    rng = np.random.default_rng(5)
    r = rng.standard_normal(1800) * 0.003
    for start in (900, 1200, 1500, 1790):                        # bursts of big moves, the last one just now
        r[start : start + 8] = rng.choice([-1, 1], 8) * 0.02
    ctx, p = picture(R.hawkes_jump_paths, bars_from(r), 6.0)
    s = R.hawkes_structure(ctx)
    assert s["active"] and s["lambda_now"] > 3 * s["mu"]
    assert tail_mass(ctx, p, 2.0) > tail_mass(ctx, C.to_bins(ctx, C.base_pdf(ctx)), 2.0)
    scattered = rng.standard_normal(1800) * 0.003
    scattered[np.arange(800, 1800, 40)] = 0.02
    assert not R.hawkes_structure(ctx_for(bars_from(scattered)))["active"]


def test_regime_switching_knows_which_mood_it_is_in():
    rng = np.random.default_rng(6)
    z = rng.standard_normal(1600)
    storm_now = ctx_for(bars_from(z * blocks([0.002, 0.006] * 4, 200)), 24.0)
    calm_now = ctx_for(bars_from(z * blocks([0.006, 0.002] * 4, 200)), 24.0)
    s, c = R.regime_switching_structure(storm_now), R.regime_switching_structure(calm_now)
    assert s["active"] and c["active"] and s["p_storm_now"] > 0.9 and c["p_storm_now"] < 0.1
    assert 2.2 < s["vol_ratio"] < 3.8
    ds, dc = C.describe(storm_now, R.regime_switching_paths(storm_now)), C.describe(calm_now, R.regime_switching_paths(calm_now))
    assert np.log(ds["high"] / ds["low"]) > 1.5 * np.log(dc["high"] / dc["low"])
    # a calm market keeps a tail for a storm arriving
    assert tail_ratio(calm_now, R.regime_switching_paths(calm_now)) > 1.1 * tail_ratio(calm_now, C.to_bins(calm_now, C.base_pdf(calm_now)))


def test_shaped_models_leave_the_baseline_when_there_is_no_structure():
    rng = np.random.default_rng(8)
    ctx = ctx_for(bars_from(rng.standard_normal(1500) * 0.003), 6.0)
    assert not R.fbm_structure(ctx)["active"]
    assert np.allclose(R.fbm_hurst_paths(ctx), C.to_bins(ctx, C.base_pdf(ctx)))


def test_fbm_finds_persistence():
    rng = np.random.default_rng(9)
    e = rng.standard_normal(1600)
    r = 0.003 * np.convolve(e, np.ones(12) / np.sqrt(12), mode="same")         # strongly persistent returns
    ctx = ctx_for(bars_from(r), 24.0)
    assert R.fbm_structure(ctx)["hurst"] > 0.6
    assert C.describe(ctx, R.fbm_hurst_paths(ctx))["width"] > 1.2


def gjr_returns(n: int, seed: int, omega=0.02, alpha=0.03, gamma=0.12, beta=0.88) -> np.ndarray:
    rng = np.random.default_rng(seed)
    h, y = omega / (1 - alpha - gamma / 2 - beta), np.empty(n)
    for t in range(n):
        y[t] = np.sqrt(h) * rng.standard_normal()
        h = omega + (alpha + gamma * (y[t] < 0)) * y[t] ** 2 + beta * h
    return y


def test_gjr_fit_recovers_the_leverage_effect():
    fit = R.gjr_fit(gjr_returns(4000, 10))
    assert fit["gamma"] > 0.05 and 0.8 < fit["beta"] < 0.95 and 0.9 < fit["persistence"] < 0.999
    assert abs(float(np.mean(fit["z"] ** 2)) - 1.0) < 0.1


def test_garch_is_wider_after_a_sell_off_than_after_a_quiet_spell():
    y = gjr_returns(1500, 11) * 0.003
    quiet, shaken = y.copy(), y.copy()
    quiet[-48:] *= 0.3
    shaken[-6:] = -0.012
    # the baseline's own EWMA reacts too, so compare the two pictures in dollars, not in baseline widths
    band = []
    for r in (quiet, shaken):
        ctx, p = picture(R.garch_gjr_skewt, bars_from(r), 6.0)
        d = C.describe(ctx, p)
        band.append(np.log(d["high"] / d["low"]))
    assert band[1] > 2 * band[0]


def test_stochastic_vol_pulls_volatility_back_to_its_long_run_level():
    y = gjr_returns(2000, 12) * 0.003
    quiet, loud = y.copy(), y.copy()
    quiet[-72:] *= 0.3
    loud[-72:] *= 3.0
    cq, cl = ctx_for(bars_from(quiet), 96.0), ctx_for(bars_from(loud), 96.0)
    assert R.stochastic_vol_structure(cq)["active"] and R.stochastic_vol_structure(cl)["active"]
    assert C.describe(cq, R.stochastic_vol_paths(cq))["width"] > 1.15
    assert C.describe(cl, R.stochastic_vol_paths(cl))["width"] < 0.9
