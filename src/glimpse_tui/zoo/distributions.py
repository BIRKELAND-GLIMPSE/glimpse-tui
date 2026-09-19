"""Distribution-shape models: pictures drawn from a named distribution (the lab's `zoo_distributions.py`).

Every other model bends a bell-like baseline. These never start from one. Each writes the distribution of the move
to the close directly, from a named family, with

- the scale taken from the live market (`ctx.sigma`: the same EWMA volatility, hour-of-day profile and partial
  first hour the baseline uses), so the picture is the right size for today and for this close;
- the shape from the family itself: Poisson and binomial lattices with visible spikes, chi-squared and Gumbel
  skews, Student-t and Cauchy tails, uniform and triangular blocks, arcsine and barbell U-shapes, two- and
  three-mode mixtures, Merton's jump mixture, and the raw distribution of what the price has actually done.

The lab describes each shape by 199 standardised quantiles and centres and scales them with the mean and spread of
those quantiles. The terminal applies the same centring and scaling to the shape's density on `core.Z`, so the
picture is the same shape at the same size. Shapes with a closed-form CDF go straight onto the bin edges, and the
lattices write the bins themselves, so hard edges and spikes stay sharp.

Shapes whose parameters are estimated from history are `kind="forecast"`; shapes that are a choice are `kind="view"`.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from scipy import stats

from .core import Ctx, Model, Z, cached, finish, from_cdf, from_samples, normalise, t_pdf, to_bins, transform

LEVELS = np.arange(1, 200) / 200.0          # the lab's QUANTILE_LEVELS
FAMILY = "Distributions"

Comps = list[tuple[float, float, float]]    # a mixture of normals: (weight, mean, sd)


# ── the lab's standardisation, applied to a density ─────────

def _mean_sd(q: np.ndarray) -> tuple[float, float]:
    """The lab's `standardize`: the mean and spread of the 199 quantiles, not of the whole distribution, so a
    shape with a very long tail is sized by its body."""
    q = np.asarray(q, dtype=float)
    m = float(np.mean(q))
    return m, max(float(np.sqrt(np.mean((q - m) ** 2))), 1e-9)


def _standardised(q: np.ndarray, pdf: Callable[[np.ndarray], np.ndarray], mirror: bool = False) -> np.ndarray:
    """Density on Z of (X - m) / s, where X has density `pdf` and m, s come from its quantiles `q`. `mirror` draws
    -(X - m) / s, the lab's `-standardize(q)[::-1]`."""
    m, s = _mean_sd(q)
    return normalise(pdf(m - s * Z if mirror else m + s * Z) * s)


def _standardised_grid(pdf: np.ndarray) -> np.ndarray:
    """The same for a shape known only as a density on the grid (the lab's `ppf_from_pdf` then `standardize`)."""
    c = np.cumsum(np.maximum(pdf, 0.0))
    m, s = _mean_sd(np.interp(LEVELS, c / c[-1], Z))
    return transform(pdf, shift=-m / s, scale=1.0 / s)


def _mixture(comps: Comps) -> np.ndarray:
    """A mixture of normals, standardised as the lab does: quantiles read off the CDF on the grid."""
    comps = [(w, m, max(s, 1e-6)) for w, m, s in comps]
    total = sum(w for w, _, _ in comps)
    cdf = sum((w / total) * stats.norm.cdf((Z - m) / s) for w, m, s in comps)
    q = np.interp(LEVELS, np.maximum.accumulate(np.clip(cdf, 0.0, 1.0)), Z)
    return _standardised(q, lambda x: sum((w / total) * stats.norm.pdf((x - m) / s) / s for w, m, s in comps))


def _logp(ctx: Ctx) -> np.ndarray:
    return np.log(ctx.bars["close"].to_numpy(dtype=float))


# ── fitted shapes (kind: forecast) ──────────────────────────

def dist_student_t(ctx: Ctx) -> np.ndarray:
    df = float(np.clip(ctx.scale.nu, 2.2, 200.0))      # the lab's fit_student_df on the same residuals
    return to_bins(ctx, t_pdf(df))


def _skew_alpha(ctx: Ctx) -> float:
    r24 = np.diff(_logp(ctx)[::24])
    r24 = r24[np.isfinite(r24)]
    skew = float(np.clip(stats.skew(r24), -0.9, 0.9)) if r24.size > 60 else 0.0
    if not np.isfinite(skew):
        return 0.0
    # method of moments for the skew-normal shape parameter
    b = np.sign(skew) * (2 * abs(skew) / (4 - np.pi)) ** (1 / 3)
    delta = float(np.clip(b / np.sqrt(1 + b * b) * np.sqrt(np.pi / 2), -0.995, 0.995))
    return float(delta / np.sqrt(max(1 - delta**2, 1e-6)))


def dist_skew_normal(ctx: Ctx) -> np.ndarray:
    a = cached(ctx, "dist_skew_normal:alpha", lambda: _skew_alpha(ctx))
    return to_bins(ctx, _standardised(stats.skewnorm.ppf(LEVELS, a), lambda x: stats.skewnorm.pdf(x, a)))


def _merton_fit(ctx: Ctx, thr: float = 4.0) -> tuple[float, float]:
    r = np.diff(_logp(ctx))
    zz = ctx.scale.z_pool[np.isfinite(ctx.scale.z_pool)]
    jumps = np.abs(zz) > thr
    n = int(jumps.sum())
    lam = max(n / max(zz.size, 1), 1e-5)
    size = float(np.mean(np.abs(r[-zz.size:][jumps]))) if n else 0.0
    sd = float(np.std(r)) or 1e-6
    return lam, float(size / sd) if size else 3.0


def dist_merton_jumps(ctx: Ctx) -> np.ndarray:
    lam_h, jump_sigmas = cached(ctx, "dist_merton_jumps:fit", lambda: _merton_fit(ctx))
    hours = ctx.hours
    lam = lam_h * max(hours, 1e-6)                       # the expected number of jumps grows with the horizon
    js = jump_sigmas / np.sqrt(max(hours, 1.0))          # jumps are absolute, not vol-scaled
    w = stats.poisson.pmf(np.arange(8), min(lam, 6.0))
    comps = [(float(w[k]), 0.0, float(np.sqrt(1.0 + k * (js * 0.5) ** 2))) for k in range(8) if w[k] > 1e-6]
    return to_bins(ctx, _mixture(comps))


def _regime_fit(ctx: Ctx) -> tuple[float, float, float]:
    r = pd.Series(np.diff(_logp(ctx))).rolling(168, min_periods=48).std().dropna().to_numpy()
    if r.size == 0:
        return 0.7, 1.8, 0.3
    calm, storm, mid = (float(np.quantile(r, lv)) for lv in (0.2, 0.9, 0.5))
    if not mid > 0:
        return 0.7, 1.8, 0.3
    p_storm = float(np.clip((float(r[-1]) - calm) / max(storm - calm, 1e-9), 0.05, 0.95))
    return calm / mid, storm / mid, p_storm


def dist_regime_mixture(ctx: Ctx) -> np.ndarray:
    calm, storm, p = cached(ctx, "dist_regime_mixture:fit", lambda: _regime_fit(ctx))
    return to_bins(ctx, _mixture([(1 - p, 0.0, calm), (p, 0.0, storm)]))


def dist_empirical_history(ctx: Ctx) -> np.ndarray:
    h = max(int(round(ctx.hours)), 1)

    def build() -> np.ndarray | None:
        lp = _logp(ctx)
        if lp.size <= h + 200:
            return None
        r = lp[h:] - lp[:-h]
        r = r[np.isfinite(r)]
        if r.size < 200:
            return None
        m, s = _mean_sd(np.quantile(r, LEVELS))
        return (r - m) / s

    rs = cached(ctx, f"dist_empirical_history:{h}", build)
    if rs is None:                                       # too little history for this horizon: the lab falls back to a bell curve
        return to_bins(ctx, stats.norm.pdf(Z))
    # from_samples joins the same 199 quantiles with straight lines, which is how the lab puts them on the bins
    return from_samples(ctx, ctx.sigma * rs)


def _vg_pdf(nu: float) -> np.ndarray:
    """A bell curve whose variance is gamma distributed: 400 equally likely amounts of business time."""
    g = np.maximum(stats.gamma.ppf(np.linspace(0.001, 0.999, 400), a=1.0 / nu, scale=nu), 1e-6)
    inv = 1.0 / np.sqrt(g)[:, None]
    return _standardised_grid((np.exp(-0.5 * (Z[None, :] * inv) ** 2) * inv).mean(axis=0))


def dist_variance_gamma(ctx: Ctx) -> np.ndarray:
    def fit() -> float:
        z = ctx.scale.z_pool[np.isfinite(ctx.scale.z_pool)]
        k = float(np.clip(np.nan_to_num(stats.kurtosis(z, fisher=True), nan=1.5), 0.2, 12.0))
        return float(np.clip(k / 3.0, 0.05, 4.0))

    nu = cached(ctx, "dist_variance_gamma:nu", fit) / max(np.sqrt(max(ctx.hours, 1.0)), 1.0)   # kurtosis fades with horizon
    nu = round(float(np.clip(nu, 0.02, 4.0)), 3)         # rounded so nearby closes share one mixture
    return to_bins(ctx, cached(ctx, f"dist_variance_gamma:pdf:{nu}", lambda: _vg_pdf(nu)))


# ── chosen shapes (kind: view) ──────────────────────────────

def dist_laplace(ctx: Ctx) -> np.ndarray:
    return to_bins(ctx, stats.laplace.pdf(Z, scale=1.0 / np.sqrt(2.0)))


def dist_cauchy(ctx: Ctx) -> np.ndarray:
    # no variance to standardise by, so the lab matches the interquartile range of its quantiles to a bell curve's
    q = stats.cauchy.ppf(LEVELS)
    iqr = float(np.percentile(q, 75) - np.percentile(q, 25))
    return to_bins(ctx, stats.cauchy.pdf(Z, scale=1.349 / iqr))


def dist_uniform_box(ctx: Ctx) -> np.ndarray:
    return from_cdf(ctx, lambda z: stats.uniform.cdf(z, loc=-np.sqrt(3.0), scale=2 * np.sqrt(3.0)))


def dist_triangular(ctx: Ctx, peak: float = 0.6) -> np.ndarray:
    c = float(np.clip((peak + 2.0) / 4.0, 0.01, 0.99))
    lv = np.linspace(1e-4, 1 - 1e-4, 4001)
    raw = stats.triang.ppf(lv, c, loc=-2.0, scale=4.0)
    m, s = _mean_sd(raw)
    return from_cdf(ctx, lambda z: np.interp(z, (raw - m) / s, lv, left=0.0, right=1.0))


def dist_arcsine(ctx: Ctx) -> np.ndarray:
    return from_cdf(ctx, lambda z: stats.arcsine.cdf(z, loc=-np.sqrt(2.0), scale=2 * np.sqrt(2.0)))   # unit variance


def dist_barbell(ctx: Ctx) -> np.ndarray:
    return from_cdf(ctx, lambda z: stats.beta.cdf(z, 0.45, 0.45, loc=-2.05, scale=4.1))


def dist_bimodal_breakout(ctx: Ctx) -> np.ndarray:
    return to_bins(ctx, _mixture([(0.5, -1.15, 0.45), (0.5, 1.15, 0.45)]))


def dist_trimodal_scenarios(ctx: Ctx) -> np.ndarray:
    return to_bins(ctx, _mixture([(0.20, -2.0, 0.35), (0.45, 0.0, 0.5), (0.35, 1.5, 0.4)]))


def dist_pinned(ctx: Ctx) -> np.ndarray:
    return to_bins(ctx, _mixture([(0.85, 0.0, 0.18), (0.15, 0.0, 1.4)]))


def dist_chi_squared_up(ctx: Ctx, df: float = 3.0, mirror: bool = False) -> np.ndarray:
    return to_bins(ctx, _standardised(stats.chi2.ppf(LEVELS, df), lambda x: stats.chi2.pdf(x, df), mirror))


def dist_chi_squared_down(ctx: Ctx, df: float = 3.0) -> np.ndarray:
    return dist_chi_squared_up(ctx, df, mirror=True)


def dist_gumbel_up(ctx: Ctx, mirror: bool = False) -> np.ndarray:
    return to_bins(ctx, _standardised(stats.gumbel_r.ppf(LEVELS), stats.gumbel_r.pdf, mirror))


def dist_gumbel_crash(ctx: Ctx) -> np.ndarray:
    return dist_gumbel_up(ctx, mirror=True)


def dist_pareto_tail_up(ctx: Ctx, tail_alpha: float = 3.0, side: float = 1.0) -> np.ndarray:
    pdf = stats.norm.pdf(Z)
    far = side * Z > 1.0
    pdf[far] = stats.norm.pdf(1.0) * np.abs(Z[far]) ** (-tail_alpha)      # joins the bell curve at one sigma
    return to_bins(ctx, _standardised_grid(pdf))


def dist_pareto_tail_down(ctx: Ctx, tail_alpha: float = 3.0) -> np.ndarray:
    return dist_pareto_tail_up(ctx, tail_alpha, side=-1.0)


def dist_normal_in_price(ctx: Ctx, s: float = 0.35) -> np.ndarray:
    # normal in price: y = log(1 + s * x) / s with x a bell curve; s is the price-space sd as a share of the sigma unit
    q = np.log1p(np.clip(s * stats.norm.ppf(LEVELS), -0.95, None)) / s
    return to_bins(ctx, _standardised(q, lambda y: stats.norm.pdf(np.expm1(s * y) / s) * np.exp(s * y)))


def dist_logistic(ctx: Ctx) -> np.ndarray:
    return to_bins(ctx, stats.logistic.pdf(Z, scale=np.sqrt(3.0) / np.pi))


# ── lattice shapes: discrete spikes written straight onto the bins ──

def _lattice_bins(ctx: Ctx, points: np.ndarray, weights: np.ndarray, smear: float) -> np.ndarray:
    """Mass sitting on lattice points (in standardised units), each smeared by a small Gaussian so the spikes land
    on the ladder without falling between bins."""
    w = np.asarray(weights, dtype=float)
    w = w / max(w.sum(), 1e-12)
    keep = w > 1e-9
    x = (np.log(np.maximum(ctx.edges, 1e-9)) - ctx.log_spot) / ctx.sigma
    cdf = w[keep] @ stats.norm.cdf((x[None, :] - np.asarray(points, dtype=float)[keep, None]) / max(smear, 1e-6))
    probs = np.diff(np.maximum.accumulate(cdf))
    return finish(probs if probs.sum() > 0 else np.ones(len(ctx.edges) - 1))


def dist_poisson_lattice(ctx: Ctx, rate_per_day: float = 9.0) -> np.ndarray:
    lam = max(rate_per_day * ctx.hours / 24.0, 0.3)
    tick = 1.0 / np.sqrt(lam)                            # in sigma units, so the variance matches the fitted scale
    ks = np.arange(0, int(lam + 6 * np.sqrt(lam)) + 3)
    return _lattice_bins(ctx, (ks - lam) * tick, stats.poisson.pmf(ks, lam), smear=max(0.16 * tick, 0.02))


def dist_binomial_lattice(ctx: Ctx, steps: int = 6) -> np.ndarray:
    ks = np.arange(steps + 1)
    return _lattice_bins(ctx, (2 * ks - steps) / np.sqrt(steps), stats.binom.pmf(ks, steps, 0.5), smear=max(0.14 / np.sqrt(steps), 0.02))


def dist_round_number_comb(ctx: Ctx, level_usd: float = 1000.0, tightness: float = 0.22) -> np.ndarray:
    spot, sigma = float(ctx.spot), ctx.sigma
    reach = 5.0 * sigma * spot
    lo = max(np.floor((spot - reach) / level_usd) * level_usd, level_usd)
    hi = np.ceil((spot + reach) / level_usd) * level_usd
    levels = np.arange(lo, hi + level_usd, level_usd)
    points = (np.log(levels) - ctx.log_spot) / sigma
    smear = max(tightness * level_usd / spot / sigma, 0.03)
    return _lattice_bins(ctx, points, stats.norm.pdf(points), smear=smear)




# ── catalogue ───────────────────────────────────────────────

LATTICE_FACTORS = ("shape", "randomness")
FIXED_INPUTS = ("Nothing from the candles beyond the shared volatility clock (σ to the close) and the market's price edges; no fitted "
                "quantity, so it draws as soon as the 400-bar minimum is met and the shape is the same at every close.")
TWELVE_WEEKS = "the loaded candles (about twelve weeks here, not the lab's year)"


def _m(mid: str, name: str, blurb: str, description: str, fn: Callable[[Ctx], np.ndarray], factors: tuple[str, ...],
       reference: str, kind: str = "view", inputs: str = FIXED_INPUTS, maths: str = "", trades: str = "") -> Model:
    return Model(mid, name, FAMILY, blurb, description, fn, kind=kind, factors=factors, reference=reference,
                 inputs=inputs, maths=maths, trades=trades, pipeline="distribution")


MODELS = [
    _m("dist_student_t", "Student-t", "Fat tails, with the fatness fitted to Bitcoin's own hourly shocks",
       "Bitcoin's hourly shocks are fat-tailed, and this picture keeps them fat all the way to the close instead of letting them wash "
       "out into a bell curve. It is Student's t with the degrees of freedom fitted to the market's own standardised hourly moves: "
       "below about six, the everyday range is tighter than a normal distribution's and the rare large move is far more likely. The "
       "shape to hold if you think extreme hours cluster rather than average away.",
       dist_student_t, ("tails",), "Student's t-distribution (Gosset 1908); heavy tails in returns (Mandelbrot 1963)",
       kind="forecast",
       inputs="Only the shared volatility clock: σ to the close and its ν, the maximum-likelihood Student-t fit to the standardised "
              "hourly residuals of the loaded candles (hourly log returns divided by the hour-of-day profile and the EWMA volatility, "
              "the first 24 dropped). With fewer than 20 residuals ν = 5.",
       maths="Density on z: t(z/s; ν)/s with s = √((ν − 2)/ν), so the variance is exactly one market σ² whatever ν is; "
             "ν = clip(clock ν, 2.2, 200), and the fit itself is bounded to [2.5, 200]. The density is symmetric and unit-variance by "
             "construction, so no quantile centring is applied, and it reaches the price edges through the grid. Unlike the baseline, "
             "ν does not grow with the hours to the close (the baseline uses ν_n = 4 + n·(ν − 4)), so a weekly close keeps the same "
             "tail as a one-hour one. Symmetric: no direction.",
       trades="Against a smooth curve of the same width it puts more weight on the ranges within about ±0.65σ of spot and on "
              "everything beyond about ±2.6σ, and buys those; it thins the shoulders from ±0.7σ to ±2.5σ and never buys them. The "
              "further the close, the more it disagrees with the baseline's thinning tails. A ν fitted above about 30 makes it a "
              "bell curve and it buys nothing."),
    _m("dist_skew_normal", "Skew-Normal", "A bell curve pulled to the side Bitcoin's daily moves have leaned",
       "Bitcoin's daily moves have not been symmetric: long grinds one way and fast moves the other. This picture is a bell curve "
       "pulled to the side the loaded history has leaned, with the pull fitted to the sample skewness of non-overlapping 24-hour "
       "log returns. With positive skew the peak sits a little below spot and the upside tail runs long; with negative skew the "
       "mirror. It bets that the recent asymmetry persists to the close.",
       dist_skew_normal, ("shape", "direction"), "Skew-normal distribution (Azzalini 1985, Scandinavian Journal of Statistics)",
       kind="forecast",
       inputs=f"Hourly closes sampled every 24th bar, so non-overlapping 24-hour log returns over {TWELVE_WEEKS}. The skew is fitted "
              "only from more than 60 such returns, i.e. at least 1465 bars or about 61 days of candles; below that, or if the "
              "skewness is not finite, α = 0 and it draws a bell curve. Cached per set of candles.",
       maths="Sample skewness g of the 24-hour returns (scipy's biased Fisher-Pearson g₁), clipped to [−0.9, 0.9]. Method of moments "
             "for the skew-normal shape: b = sign(g)·(2|g|/(4 − π))^(1/3), δ = clip(b/√(1 + b²)·√(π/2), −0.995, 0.995), "
             "α = δ/√(1 − δ²), which inverts the skew-normal's skewness formula exactly; |g| = 0.9 gives α ≈ ±6.4. The shape is "
             "skewnorm(α): its 199 quantiles set the centre m and rms spread s, the density is read at m + s·z times s, and it reaches "
             "the price edges through the grid. Positive g gives α > 0: the mode sits below spot (about −0.3σ at α = 2, −0.7σ at "
             "α = 5), the tail runs above, and the thin side dies out within 3σ to 5σ. sign: the lean follows the sign of g.",
       trades="With positive fitted skew it puts more weight than a smooth curve on the ranges from about −1.5σ up to just below spot "
              "(the displaced peak) and on the upside beyond about +1.7σ, and buys those; it thins the near-upside from spot to +1.7σ "
              "and the downside below −1.5σ and never buys them. Negative skew mirrors this. With too few candles or a skewness near "
              "zero it is a bell curve and buys nothing."),
    _m("dist_merton_jumps", "Merton Jumps", "Prices in the sudden gap that nobody saw coming",
       "Most hours are ordinary diffusion; a few are gaps, when a headline or a liquidation cascade moves the price in one go. Merton's "
       "model adds a Poisson number of such jumps to the bell curve, and this picture writes the mixture out directly: a tight core "
       "for no jump and progressively wider layers for one, two, three jumps. Over a few hours the layers show as heavy tails; over "
       "a week they blur into a mildly fat-tailed bell. The jump rate and size come from the hours Bitcoin moved more than 4σ.",
       dist_merton_jumps, ("randomness", "tails", "shape"), "Jump-diffusion option pricing (Merton 1976, Journal of Financial Economics)",
       kind="forecast",
       inputs=f"Hourly log returns and the clock's standardised residual pool over {TWELVE_WEEKS}; a jump hour has |z| > 4. With no "
              "such hour the rate is 10⁻⁵ per hour and the jump size 3 hourly standard deviations, which draws a bell curve. The fit "
              "is cached per set of candles; the mixture is rebuilt per close.",
       maths="Fit: λ_h = max(n_jumps/n_residuals, 10⁻⁵) jumps per hour; J = mean |r| over the jump hours divided by the plain "
             "standard deviation of all hourly log returns, a jump size in hourly sd units. Per close h hours out: λ = λ_h·h, capped at "
             "6 when drawing the Poisson weights; j = J/√max(h, 1) converts the jump into the σ unit of the whole move (jumps are "
             "absolute, not volatility-scaled). Layers k = 0 … 7 with weight w_k = Poisson(k; λ), each a normal centred on spot with "
             "sd √(1 + k·(j/2)²), layers with w_k < 10⁻⁶ dropped. The mixture's 199 quantiles set the centre and rms spread, so the "
             "total width is the market's σ and the jump layers narrow the core rather than widen the whole. All layers are centred: "
             "no direction.",
       trades="It puts more weight than the market on the far ranges beyond about ±2σ, and a little on the centre, and buys those; "
              "it thins the shoulders around ±1σ and does not buy them. With a fraction of a percent of jump hours and a close a day "
              "or more out, the layers add so little that it is a bell a shade wider than σ and buys next to nothing; with no 4σ "
              "hours at all it is a bell curve."),
    _m("dist_regime_mixture", "Calm or Storm", "Two markets in one: a calm peak with stormy shoulders",
       "Volatility comes in regimes, quiet weeks and stormy weeks, and the market is always in one while at risk of switching to the "
       "other. This picture is a mixture of two normals: a calm component and a stormy one, with their widths taken from the "
       "quietest and busiest stretches of the loaded history and the storm's weight from where the last week's volatility sits "
       "between them. A tight peak with broad shoulders, the honest shape of a market that is calm right now but might not stay so.",
       dist_regime_mixture, ("randomness", "volatility", "tails"),
       "Mixture of normals for returns (Hamilton 1989; Kon 1984, Journal of Finance)",
       kind="forecast",
       inputs=f"Hourly log returns over {TWELVE_WEEKS}, as a rolling 168-hour (7 day) standard deviation with at least 48 bars per "
              "window; cached per set of candles. If the rolling series is empty or its median is not positive it falls back to "
              "widths 0.7 and 1.8 with a 30% storm weight.",
       maths="Let v_t be the rolling 168-hour std of hourly log returns. calm, mid and storm are its 0.2, 0.5 and 0.9 quantiles over "
             "the loaded history, and the storm probability is p = clip((v_last − calm)/(storm − calm), 0.05, 0.95). Mixture: weight "
             "1 − p on N(0, (calm/mid)²) and weight p on N(0, (storm/mid)²); its 199 quantiles set the centre and rms spread, so the "
             "mixture is rescaled to the market's σ and only the ratio storm/calm and p shape it. Both components are centred: no "
             "direction; the same shape for every close.",
       trades="It puts more weight than a smooth curve on the ranges within about ±0.6σ of spot and beyond about ±2.3σ, and buys "
              "those; it thins the shoulders from ±1σ to ±2σ and never buys them. The disagreement is largest when the calm and storm "
              "widths are far apart and p is mid-range; when the quiet and busy stretches were alike the mixture is a single bell "
              "and the bot buys little or nothing."),
    _m("dist_empirical_history", "Empirical History", "No formula: what Bitcoin actually did over this many hours",
       "No formula: the picture is the distribution of what Bitcoin actually did over this many hours in the loaded candles, with "
       "its average move removed and its spread rescaled to today's volatility. It is lumpy where history was lumpy and asymmetric "
       "where history was asymmetric. For closes an hour or a day out that is thousands of overlapping samples of fat-tailed hourly "
       "moves; for a weekly close it is a dozen independent weeks, and the lumps are as much noise as evidence.",
       dist_empirical_history, ("shape", "tails"),
       "Filtered historical simulation (Barone-Adesi, Giannopoulos and Vosper 1999, Journal of Futures Markets)",
       kind="forecast",
       inputs=f"Hourly log closes over all of {TWELVE_WEEKS}. For a close h hours out (h rounded to the nearest hour, at least 1) it "
              "needs more than h + 200 bars and at least 200 finite h-hour returns, otherwise it draws a unit bell curve; the "
              "standardised sample is cached per candles and h.",
       maths="Overlapping h-hour log returns r_i = ln C_i+h − ln C_i over the whole history; m and s are the mean and rms spread of "
             "their 199 sample quantiles, the sample is standardised to (r − m)/s and multiplied by σ. The bins come from from_samples: "
             "the 199 quantiles of the rescaled sample joined by straight lines, with linear tails out to the most extreme return, read "
             "at the price edges. Removing m strips history's drift; the asymmetry, the kurtosis and the lumps stay. No parameters and "
             "no smoothing beyond the quantile interpolation.",
       trades="It buys wherever the loaded history put more h-hour moves than a smooth curve: for short closes the ranges near spot "
              "and the far tails, since hourly moves are peaked and fat-tailed; for any close the side history leaned once the mean is "
              "removed, and any lump in the sample. The longer the close, the more the trades follow the accidents of twelve weeks. "
              "With too little history for the horizon it is a bell curve and buys nothing."),
    _m("dist_variance_gamma", "Variance Gamma", "The market's clock runs at random speed: sharp peak, heavy tails",
       "The market's clock does not tick evenly: information and trading arrive in bursts, so a bell curve in business time looks "
       "peaked and fat-tailed in calendar time. The variance-gamma picture makes that explicit: a normal whose variance is a "
       "gamma-distributed amount of business time. The gamma's spread is fitted to the excess kurtosis of Bitcoin's own hourly "
       "shocks and fades as the close moves out, since a sum of many bursty hours is closer to a bell than one is.",
       dist_variance_gamma, ("tails", "shape"), "Variance gamma process (Madan and Seneta 1990, Journal of Business)", kind="forecast",
       inputs="The clock's standardised hourly residual pool from the loaded candles, for its excess kurtosis (cached per set of "
              "candles), the hours to the close, and the shared σ. Nothing else; a non-finite kurtosis is treated as 1.5.",
       maths="Excess kurtosis κ of the residuals, clipped to [0.2, 12]; ν₁ = clip(κ/3, 0.05, 4), the method of moments for one hour "
             "since a symmetric variance-gamma has excess kurtosis 3ν. For a close h hours out ν = clip(ν₁/√max(h, 1), 0.02, 4), "
             "rounded to three decimals so nearby closes share one cached shape (note the fade is 1/√h, slower than the 1/h of a "
             "variance-gamma process summed over h hours). Business time g takes 400 equally likely values, the gamma(shape 1/ν, "
             "scale ν) quantiles at levels 0.001 to 0.999 (mean 1, variance ν, floored at 10⁻⁶), and the density is the average over "
             "them of exp(−z²/(2g))/√g. The grid density's 199 quantiles set the centre and rms spread. Symmetric: no direction.",
       trades="It puts more weight than a smooth curve on the ranges within about ±0.5σ of spot and beyond about ±2σ, and buys those; "
              "it thins the shoulders from ±0.5σ to ±2σ and never buys them. The lean is strongest for closes a few hours out with a "
              "high fitted kurtosis (ν near 1 gives a peak two thirds taller than a bell's); at ν of 0.1 or below, a low-kurtosis "
              "fit or a far close, it is a bell curve and buys nothing."),
    _m("dist_laplace", "Laplace Spike", "Quiet until it isn't: a sharp spike with exponential sides",
       "The most likely outcome is very little change, and yet a large move is considerably more likely than a bell curve allows: "
       "both at once, with nothing special about the ranges in between. The double-exponential, or Laplace, distribution says exactly "
       "that: a sharp spike at today's price with straight-line exponential sides. The shape of a market that sits still until "
       "something happens, and a common fit for high-frequency returns.",
       dist_laplace, ("shape", "tails"), "Laplace distribution (Laplace 1774); Linnik and Laplace models of returns",
       maths="Density on z: Laplace with scale b = 1/√2, f(z) = exp(−√2·|z|)/√2, which has unit variance, so no quantile centring is "
             "applied and the width is the market's σ exactly. Peak 0.707 against a unit bell's 0.399; excess kurtosis 3. It reaches "
             "the price edges through the grid. Symmetric: no direction.",
       trades="It puts more weight than a bell of the same width on the ranges within about ±0.5σ of spot and on everything beyond "
              "about ±2.3σ, and buys those; it thins the shoulders from ±0.5σ to ±2.3σ and never buys them. A fixed shape never says "
              "nothing: it disagrees with a smooth market curve at every close, the more so when the market's own tails are thin."),
    _m("dist_cauchy", "Cauchy Wild Card", "A normal-looking core with tails that never give up",
       "A Cauchy distribution has no variance at all: however far out you look there is still real probability there, and the mean "
       "of many draws is no better behaved than one. Scaled so its middle half matches a bell curve's, it keeps a normal-looking core "
       "while assigning far more weight to a violent move than any well-behaved model would. The wild card: wrong most of the time by "
       "a little, right on the rare day by a lot.",
       dist_cauchy, ("tails",), "Cauchy distribution; stable laws for price changes (Mandelbrot 1963)",
       maths="There is no variance to standardise by, so the scale matches the interquartile range: γ = 1.349/IQR, where IQR is the "
             "75th minus the 25th percentile of the 199 standard-Cauchy quantiles (1.969, so γ ≈ 0.685) and 1.349 is a unit bell's "
             "interquartile range. Density f(z) = γ/(π·(γ² + z²)) on the grid, then to the price edges. The grid stops at ±14σ: about "
             "3% of the Cauchy's mass would lie beyond it, that is dropped and the rest renormalised, so the tails are cut off 14 "
             "standard deviations from spot. Symmetric: no direction.",
       trades="It puts more weight than a bell on the ranges within about ±0.35σ of spot and on everything beyond about ±2σ, by far "
              "the most on the far tails, and buys those; it thins the shoulders from ±0.4σ to ±2σ and never buys them. The far-tail "
              "ranges are where it stakes most against the market, and where it loses on almost every close."),
    _m("dist_uniform_box", "Uniform Box", "Every price inside the band is equally likely, nothing outside",
       "Inside a band every price is equally likely; outside it, nothing. Maximum ignorance on a bounded interval, the shape to draw "
       "when you believe the market will stay in a range but have no idea where inside it the price ends. A flat block instead of a "
       "hump, so the market's central peak is what it disagrees with.",
       dist_uniform_box, ("shape",), "Maximum-entropy distribution on a bounded interval (Jaynes 1957)",
       maths="Uniform on [−√3, +√3] in σ units (loc −√3, width 2√3), which has unit variance, so the block is exactly as wide as the "
             "market's σ. Its closed-form CDF is evaluated at the price edges, so the two hard edges stay sharp; density 1/(2√3) ≈ 0.289 "
             "inside and zero outside (the 10⁻⁹ range floor is all that remains beyond ±1.73σ). Symmetric: no direction.",
       trades="It puts more weight than a bell on the ranges from about ±0.8σ out to ±1.73σ on both sides and buys those; it thins "
              "the centre within ±0.8σ and puts nothing beyond ±1.73σ, and never buys either. It pays most on a close near the edge "
              "of the band and loses on a close at spot or outside the band."),
    _m("dist_triangular", "Triangle Up", "Rises in a straight line to a target above spot, then stops dead",
       "A trader with a target above spot who thinks the market walks towards it, and refuses to pay for anything beyond it, draws "
       "a triangle: probability rising in a straight line to a peak above today's price and then dropping to zero. This is that "
       "picture with the mean held at spot, so the lean is in the shape rather than in a drift: a longer, thinner downside and a "
       "compact upside that ends dead.",
       dist_triangular, ("shape", "direction"), "Triangular distribution (Kotz and van Dorp 2004, Beyond Beta)",
       maths="Triangular on [−2, 2] with its mode at peak = 0.6, scipy's c = (0.6 + 2)/4 = 0.65. 4001 quantiles at levels 10⁻⁴ to "
             "1 − 10⁻⁴ (not the usual 199) give the centre m ≈ 0.20 and rms spread s ≈ 0.829, and the standardised CDF is read at the "
             "price edges by interpolating (quantile − m)/s against the levels. In σ units: support from −2.65σ to +2.17σ, peak at "
             "+0.48σ with density 0.414, mean on spot. Direction: up in the mode, balanced in the mean.",
       trades="It puts more weight than a bell on the ranges from about +0.3σ to +1.9σ above spot and, less obviously, from −1.2σ to "
              "−2.5σ below, and buys both; it thins the centre from −1.2σ to +0.3σ, puts nothing above +2.2σ or below −2.65σ, and never "
              "buys those. A close at spot or beyond the target loses."),
    _m("dist_arcsine", "Arcsine U", "Hollow in the middle, heaviest at both edges of the range",
       "Lévy's arcsine law is the most counter-intuitive fact about random walks: over a stretch of time a walk spends most of it on "
       "one side, and the moment of its maximum, or of its last visit to the start, is most likely near either end of the interval, "
       "not the middle. This picture takes that U as the shape of the close: hollow where everyone expects the peak and heaviest at "
       "both edges of the band.",
       dist_arcsine, ("shape", "randomness"), "Arcsine laws for Brownian motion (Lévy 1939)",
       maths="Arcsine on [−√2, +√2] in σ units (loc −√2, width 2√2), which has unit variance. Density 1/(π·√(2 − z²)), 0.225 at spot "
             "and unbounded at the two edges; the closed-form CDF (2/π)·arcsin(√((z + √2)/(2√2))) is evaluated at the price edges, so "
             "each singular edge lands in exactly one range. Symmetric: no direction.",
       trades="It puts more weight than a bell on the ranges from about ±0.8σ out to the edges at ±1.41σ, most of all on the two "
              "ranges that contain the edges, and buys those; it thins the centre within ±0.8σ (39% of its mass against a bell's 59%) "
              "and puts nothing beyond ±1.41σ, and never buys either."),
    _m("dist_barbell", "Barbell", "Almost nothing at today's price, two heavy lobes at the edges",
       "A pending decision, a listing, a ruling: the market will be somewhere else by the close, and the only open question is which "
       "side. The barbell puts almost no weight at today's price and two heavy lobes out at the edges of a band, heavier the closer "
       "to the edge. Unlike the bimodal breakout, each side is a spike against a wall, not a hump.",
       dist_barbell, ("shape", "tails"), "Beta(0.5, 0.5) U-shaped prior; event-driven bimodality",
       maths="Beta(0.45, 0.45) stretched onto [−2.05, +2.05] in σ units (loc −2.05, width 4.1): U-shaped with integrable "
             "singularities at both ends and its minimum, 0.144, at spot. This shape is not standardised: its standard deviation is "
             "1.49σ, wider than the market's, and only 30% of its mass lies within ±1σ. The closed-form CDF is read at the price "
             "edges so the walls are hard. Symmetric: no direction.",
       trades="It puts more weight than a bell on the ranges from about ±1.24σ out to the walls at ±2.05σ, most of all on the two "
              "ranges that touch the walls, and buys those; it thins everything within ±1.24σ of spot and puts nothing beyond "
              "±2.05σ, and never buys either. A close anywhere near spot loses the stake."),
    _m("dist_bimodal_breakout", "Bimodal Breakout", "Two humps: the range breaks one way or the other, then runs",
       "A market coiled inside a range breaks one way or the other and then runs: the close is far more likely to be well above or "
       "well below spot than near it. Two humps with a dip between them and equal odds on each side. Unlike the barbell, each "
       "outcome is a proper hump rather than a spike at a wall, so it pays across a zone rather than at a single price.",
       dist_bimodal_breakout, ("shape",), "Two-component normal mixture (Pearson 1894)",
       maths="Mixture of two normals, weights 0.5 and 0.5, means ∓1.15 and sds 0.45 in raw units. The mixture's 199 quantiles, read "
             "off its CDF on the grid, give an rms spread of 1.226, so after standardising the humps sit at ±0.94σ with sd 0.37σ each; "
             "the density at spot is about a tenth of a bell's and the peaks reach 0.54 against a bell's 0.40. Symmetric: no "
             "direction.",
       trades="It puts more weight than a bell on the ranges from about ±0.6σ to ±1.6σ on both sides and buys those; it thins the "
              "centre within ±0.6σ and everything beyond ±1.6σ and never buys them. A close at spot is its worst case."),
    _m("dist_trimodal_scenarios", "Three Scenarios", "20% sell-off, 45% quiet, 35% rally: three separate humps",
       "People think about what comes next in scenarios, not in one smooth curve: a sharp sell-off, a quiet stretch, a rally. This "
       "picture puts 20% on the sell-off, 45% on the quiet stretch near today's price and 35% on the rally, each as its own hump "
       "with a gap between, and holds the mean at spot. Three humps of comparable height where a standard model draws one.",
       dist_trimodal_scenarios, ("shape",), "Scenario mixture (finite normal mixture, Pearson 1894)",
       maths="Mixture of three normals in raw units: weight 0.20 at mean −2.0 with sd 0.35, 0.45 at 0.0 with sd 0.5, and 0.35 at "
             "+1.5 with sd 0.4. The mixture's 199 quantiles give centre 0.126 and rms spread 1.317, so after standardising the humps "
             "sit at −1.61σ (sd 0.27σ), −0.10σ (sd 0.38σ) and +1.04σ (sd 0.30σ) and the mean sits on spot. Weights and positions are "
             "choices, not fits. Direction: none in the mean, but the rally hump is nearer spot than the sell-off hump.",
       trades="It puts more weight than a bell on three bands and buys them: the sell-off from about −2.1σ to −1.3σ, the tight "
              "centre from −0.35σ to +0.15σ and the rally from +0.7σ to +1.55σ. It thins the gaps between the humps (around −0.9σ "
              "and +0.4σ) and everything beyond −2.2σ or +1.6σ, and never buys those."),
    _m("dist_pinned", "Pinned", "Bets the market has gone to sleep",
       "Option traders know pin risk: near a big strike, hedging flows hold the price in place into expiry. This picture bets the "
       "market has gone to sleep: a tall narrow spike on today's price holding 85% of the probability, with the other 15% spread very "
       "wide as the admission that if it does wake up it can go anywhere. If nothing happens it wins by a mile; if anything happens "
       "it loses on the spike and gets a little back only far out on the tails.",
       dist_pinned, ("shape", "volatility"), "Pinning at strikes (Avellaneda and Lipkin 2003, Quantitative Finance)",
       maths="Mixture of two normals centred on spot: weight 0.85 with sd 0.18 and weight 0.15 with sd 1.4 in raw units. The 199 "
             "quantiles give an rms spread of 0.509, so after standardising the spike has sd 0.35σ and the wide component sd 2.75σ: "
             "the far tails are heavier than a bell's, not thinner, even though the body is far narrower. Peak density 0.98, two and a "
             "half times a bell's. Symmetric: no direction.",
       trades="It puts more weight than a bell on the ranges within about ±0.5σ of spot and, by a small margin, on everything beyond "
              "about ±2.6σ, and buys those; it thins the whole band from ±0.5σ to ±2.6σ and never buys it. Nearly all of the stake "
              "goes on the ranges next to spot, and a move of more than half a σ loses it."),
    _m("dist_chi_squared_up", "Chi-Squared Up", "Most likely a small drop, with a long run of upside",
       "Some markets bleed gently most of the time and occasionally explode higher, which is how Bitcoin bull runs have tended to "
       "arrive. A chi-squared shape says exactly that: the most likely outcome is a small drop, the downside is capped hard not far "
       "below spot, and the right side runs a long way. A stated view about the shape of the up-move, not a fit.",
       dist_chi_squared_up, ("shape", "direction", "tails"), "Chi-squared distribution (Pearson 1900)",
       maths="χ² with df = 3: its 199 quantiles give centre m ≈ 2.97 and rms spread s ≈ 2.35 (the exact mean and sd are 3 and √6); "
             "the density is read at m + s·z times s, then reaches the price edges through the grid. In σ units: a hard floor at "
             "−m/s ≈ −1.26σ, the mode at (1 − m)/s ≈ −0.84σ with density 0.57, the mean on spot and an exponential tail above. "
             "Direction: mode below spot, skew up.",
       trades="It puts more weight than a bell on the ranges from about −1.25σ up to just below spot and on the upside beyond about "
              "+2σ, and buys both; it thins the ranges from spot to +2σ, puts nothing below −1.26σ, and never buys those. It pays on a "
              "small dip or a large rally and loses on a moderate rise or any drop past the floor."),
    _m("dist_chi_squared_down", "Chi-Squared Down", "Most likely a small rise, with a long thin tail of disaster",
       "The mirror: a market that grinds up and falls down stairs. The most likely outcome is a small rise, the upside is capped hard "
       "not far above spot, and the left side runs a long way into disaster. It is the shape most sellers of downside options are "
       "implicitly betting against, drawn as a stated view rather than a fit.",
       dist_chi_squared_down, ("shape", "direction", "tails"), "Chi-squared distribution (Pearson 1900)",
       maths="The χ² (df = 3) shape of Chi-Squared Up mirrored: the density is read at m − s·z with the same m ≈ 2.97 and s ≈ 2.35 "
             "(the lab's −standardize(q)[::−1]). In σ units: a hard ceiling at +1.26σ, the mode at +0.84σ with density 0.57, the mean "
             "on spot and an exponential tail below. Direction: mode above spot, skew down.",
       trades="It puts more weight than a bell on the ranges from just above spot up to about +1.25σ and on the downside beyond about "
              "−2σ, and buys both; it thins the ranges from spot down to −2σ, puts nothing above +1.26σ, and never buys those. It pays "
              "on a small rise or a crash and loses on a moderate drop or any rally past the ceiling."),
    _m("dist_gumbel_up", "Gumbel Up", "Extreme-value shape: the upside record is the one at risk",
       "Gumbel's distribution describes the largest of many moves, so it has a compact left side that falls away doubly "
       "exponentially and a right side that stretches out exponentially. As a picture of the close it says the typical outcome is a "
       "touch below spot and unremarkable, and that the record most at risk of being broken is the upside one. A stated extreme-value "
       "view, not a fit.",
       dist_gumbel_up, ("tails", "direction"), "Extreme value theory (Gumbel 1958, Statistics of Extremes)",
       maths="The standard right-skewed Gumbel: its 199 quantiles give centre m ≈ 0.567 and rms spread s ≈ 1.236 (the exact mean and "
             "sd are 0.577 and π/√6 ≈ 1.283); the density is read at m + s·z times s, then reaches the price edges through the grid. "
             "In σ units: the mode at −m/s ≈ −0.46σ with density 0.455, the mean on spot, skewness 1.14, the left side effectively "
             "gone by −3σ. Direction: mode below spot, skew up.",
       trades="It puts more weight than a bell on the ranges from about −1.45σ up to spot and on the upside beyond about +2σ, and "
              "buys both; it thins the ranges from spot to +2σ and everything below −1.5σ, and never buys those."),
    _m("dist_gumbel_crash", "Gumbel Crash", "Extreme-value shape: the record likely to break is the drawdown",
       "Gumbel's extreme-value shape mirrored onto the downside: a compact right side and a long left one. The market where the "
       "record most likely to break is the drawdown, not the high: the typical close is a touch above spot, and the tail that "
       "matters is below it.",
       dist_gumbel_crash, ("tails", "direction"), "Extreme value theory (Gumbel 1958, Statistics of Extremes)",
       maths="The standard Gumbel of Gumbel Up mirrored: the density is read at m − s·z with m ≈ 0.567 and s ≈ 1.236. In σ units: "
             "the mode at +0.46σ with density 0.455, the mean on spot, skewness −1.14, the right side effectively gone by +3σ. "
             "Direction: mode above spot, skew down.",
       trades="It puts more weight than a bell on the ranges from spot up to about +1.45σ and on the downside beyond about −2σ, and "
              "buys both; it thins the ranges from spot down to −2σ and everything above +1.5σ, and never buys those."),
    _m("dist_pareto_tail_up", "Pareto Tail Up", "A normal core with a power-law tail bolted onto the upside",
       "Ordinary days look ordinary, but the largest moves in every market follow a power law rather than the exponential decay a "
       "bell curve assumes: a five-standard-deviation move is thousands of times more likely than the bell claims. This picture "
       "keeps the bell in the body and bolts a power-law tail onto the upside, joined at one standard deviation, as the view that "
       "the big surprise, if it comes, is up.",
       dist_pareto_tail_up, ("tails",), "Power-law tails in returns (Gabaix, Gopikrishnan, Plerou, Stanley 2003, Nature)",
       maths="On the grid: f(z) = φ(z) for z ≤ 1 and f(z) = φ(1)·z^(−3) for z > 1, continuous at the join (tail exponent α = 3, so "
             "the variance exists; at z = 5 the tail is φ(1)/125 ≈ 0.0019 against a bell's 1.5·10⁻⁶). Because z^(−3) falls faster "
             "than the bell at first, the power law carries less mass than the bell from 1σ to about 2.6σ and more beyond. The grid "
             "density's 199 quantiles give centre ≈ −0.035 and rms spread ≈ 1.028, so after standardising the join sits at about "
             "+1.0σ, the mode at about +0.04σ, and the tail runs to the grid's end near +13.7σ. Direction: up, in the far tail only.",
       trades="It puts more weight than a bell on the far upside beyond about +2.6σ and, slightly, on the body within about ±1σ, "
              "and buys those; it thins the near upside from +1σ to +2.6σ as well as the downside below −1σ, and never buys those. "
              "It pays only on a large rally."),
    _m("dist_pareto_tail_down", "Pareto Tail Down", "A normal core with a power-law tail on the downside",
       "The same normal core with the power-law tail on the downside: the everyday picture is unchanged, but crashes decay as a "
       "power of their size rather than exponentially, which is what the record of Bitcoin drawdowns looks like and what a bell "
       "curve cannot represent at all.",
       dist_pareto_tail_down, ("tails",), "Power-law tails in returns (Gabaix, Gopikrishnan, Plerou, Stanley 2003, Nature)",
       maths="The mirror of Pareto Tail Up: f(z) = φ(z) for z ≥ −1 and φ(1)·|z|^(−3) for z < −1 (side = −1, α = 3), standardised by "
             "the grid density's 199 quantiles: the join at about −1.0σ, the mode at about −0.04σ, the tail to −13.7σ, and less mass "
             "than the bell from −1σ to −2.6σ. Direction: down, in the far tail only.",
       trades="It puts more weight than a bell on the far downside beyond about −2.6σ and, slightly, on the body within about ±1σ, "
              "and buys those; from −1σ to −2.6σ the power law is thinner than the bell, so it thins that band and the upside above "
              "+1σ, and never buys them. It pays only on a crash."),
    _m("dist_normal_in_price", "Normal in Dollars", "A bell curve in dollars, so lopsided in log price",
       "Every other shape here treats the log price as the thing that wanders. Bachelier's original model let the dollar price "
       "wander instead: a bell curve in dollars, which in the log-price space the market is priced in becomes lopsided, with a long "
       "thin tail on the downside (a fixed dollar fall is a bigger log move the lower the price goes) and a compact upside. A small "
       "assumption with a shape you can see.",
       dist_normal_in_price, ("shape",), "Bachelier's arithmetic Brownian motion (Bachelier 1900)",
       maths="Let x be a unit normal and the dollar return P/spot − 1 = s·x with s = 0.35; the log move is y = ln(1 + s·x)/s, with "
             "s·x floored at −0.95 so the price never falls below 5% of spot. The density of y is φ((e^(s·y) − 1)/s)·e^(s·y). The 199 "
             "quantiles of y give centre ≈ −0.21 and rms spread ≈ 1.20, so after standardising the mode sits at about +0.43σ, the mean "
             "on spot, the upside is effectively gone by +3σ (the transform compresses it) and the downside tail runs to the grid's "
             "end. Since the shape is rescaled to σ, s sets only the asymmetry, not the width. Direction: mode above spot, skew down.",
       trades="It puts more weight than a bell on the ranges from about −0.15σ to +1.3σ above spot and on the far downside beyond "
              "about −2.2σ, and buys both; it thins the ranges from −0.15σ down to −2.2σ and everything above +1.3σ, and never buys "
              "those. It pays on a moderate rise or a crash."),
    _m("dist_logistic", "Logistic", "A bell curve with a sharper peak and heavier tails",
       "The logistic distribution is the bell curve's slightly wilder cousin: the same symmetric hump with a sharper peak and "
       "heavier tails, an excess kurtosis of 1.2 where the normal has none. The gentle correction for anyone who thinks the bell "
       "picture is roughly right but a little too confident about the middle and the extremes.",
       dist_logistic, ("shape", "tails"), "Logistic distribution (Verhulst 1845; Balakrishnan 1992)",
       maths="Logistic with scale b = √3/π ≈ 0.551 on z, f(z) = e^(−z/b)/(b·(1 + e^(−z/b))²), which has unit variance, so no quantile "
             "centring is applied. Peak 0.453 against a bell's 0.399; excess kurtosis 1.2; the tails decay exponentially at rate "
             "π/√3 ≈ 1.81 per σ, so far out it is close to the Laplace and well above the bell. It reaches the price edges through the "
             "grid. Symmetric: no direction.",
       trades="It puts more weight than a bell on the ranges within about ±0.7σ of spot and on everything beyond about ±2.4σ, and "
              "buys those, in each case by a modest margin; it thins the shoulders from ±0.7σ to ±2.4σ and never buys them. The "
              "smallest disagreement in the family: against a market curve with any fat in its tails it buys little or nothing."),
    _m("dist_poisson_lattice", "Poisson Ticks", "A comb of spikes, one per possible number of ticks",
       "Suppose the price moves in whole ticks and the number of ticks in a given time is a Poisson count, nine a day on average. The "
       "close then lands on one of a comb of separate prices, one per possible count, rather than anywhere on a smooth curve. This "
       "is what a market that moves in jumps rather than continuously looks like; over a few hours the comb is coarse and lopsided, "
       "and as the close moves out it fills in towards a bell.",
       dist_poisson_lattice, LATTICE_FACTORS, "Poisson distribution (Poisson 1837); lattice models of tick moves",
       inputs="Nothing from the candles beyond the shared σ to the close and the market's price edges, plus the hours to the close, "
              "which set the expected count; no fitted quantity.",
       maths="λ = max(9·h/24, 0.3) for h hours to the close; tick = 1/√λ in σ units, so the count's variance λ·tick² is exactly one "
             "σ². Counts k = 0 … int(λ + 6√λ) + 2 with weights Poisson(k; λ) sit at z_k = (k − λ)·tick, so the mean is on spot; each "
             "spike is a normal with sd max(0.16·tick, 0.02σ) and the mixture's CDF is evaluated at the price edges. Examples: for a "
             "one-hour close λ = 0.375, tick 1.63σ, spikes at −0.61σ (69%), +1.02σ (26%) and +2.65σ (5%); for a daily close λ = 9, "
             "tick 0.33σ, smear 0.053σ; for a weekly close λ = 63, tick 0.126σ, smear at the 0.02σ floor. Direction: none in the mean, "
             "but at short horizons the lowest spike carries most of the mass and the skew is up.",
       trades="It buys the ranges that contain a spike and nothing between them: for a close a few hours out, one range just below "
              "spot holding most of the weight and one or two above it; for a daily close, every second or third range out to about "
              "±3σ, where the market's smooth curve is cheaper than the spike. Where the market's ranges are wider than a tick the "
              "comb averages out into a bell and it buys little."),
    _m("dist_binomial_lattice", "Binomial Tree", "Six up-or-down steps, so seven visible spikes",
       "The binomial tree from every option textbook, drawn as a distribution of the close: in each of six periods the price goes up "
       "or down one step with equal odds, so it ends on one of seven prices, heaviest in the middle. Seven visible spikes instead of a "
       "smooth hump, a reminder that the continuous picture is an approximation of something discrete, and one that never reaches "
       "the far tails at all.",
       dist_binomial_lattice, LATTICE_FACTORS, "Binomial option pricing (Cox, Ross and Rubinstein 1979)",
       maths="steps = 6, k = 0 … 6 up-moves with weights Binomial(6, ½): 1/64, 6/64, 15/64, 20/64, 15/64, 6/64, 1/64. Spike k sits at "
             "z_k = (2k − 6)/√6, i.e. 0, ±0.82σ, ±1.63σ and ±2.45σ, which gives unit variance. Each spike is a normal with sd "
             "max(0.14/√6, 0.02) = 0.057σ and the mixture's CDF is evaluated at the price edges. The same seven spikes for every close; "
             "only their dollar spacing follows σ. Symmetric: no direction.",
       trades="It buys the ranges that contain one of the seven spikes, spot, ±0.82σ, ±1.63σ and ±2.45σ, most of all the range at "
              "spot with 31% of the mass, and nothing between them; it puts nothing beyond ±2.5σ and never buys the far tails. Where "
              "the market's ranges are wider than 0.8σ the spikes fall into neighbouring ranges and the comb reads as a coarse bell."),
    _m("dist_round_number_comb", "Round-Number Comb", "Weight in narrow bands around each thousand-dollar level",
       "Prices cluster at round numbers because orders do: stops, limits and targets pile up at every thousand dollars, so a close "
       "lands near one of them more often than a smooth curve says. This picture puts most of its weight in narrow bands around "
       "each thousand-dollar level inside a normal envelope of today's width, so the outcome is drawn as a comb of psychological "
       "levels instead of a smooth curve.",
       dist_round_number_comb, ("shape",), "Price clustering at round numbers (Osler 2003, Journal of Finance)",
       inputs="Spot and the market's price edges in dollars, and the shared σ to the close, which sets both the envelope and the "
              "reach; no fitted quantity. It works in dollars, so it is written for BTC-sized prices where $1000 levels are a few "
              "tenths of a σ apart on a daily close.",
       maths="level_usd = 1000, tightness = 0.22. Levels: every $1000 from max(floor((spot − 5σ·spot)/1000)·1000, 1000) up to "
             "ceil((spot + 5σ·spot)/1000)·1000. Each level's position is z_L = (ln L − ln spot)/σ and its weight φ(z_L), a unit-normal "
             "envelope read at the level and normalised over the levels; each tooth is a normal in z with sd max(0.22·1000/(spot·σ), "
             "0.03σ), i.e. 22% of the $1000 spacing in σ units, and the mixture's CDF is evaluated at the price edges. At spot $78k: "
             "for a daily close (σ ≈ 2%) the levels are 0.65σ apart with tooth sd 0.14σ, a clear comb; within an hour (σ ≈ 0.35%) "
             "they are 3.6σ apart with tooth sd 0.8σ, one or two broad humps on the nearest thousands. Direction: none from the "
             "envelope, but lopsided when the nearest thousands sit to one side of spot.",
       trades="It buys the ranges that straddle a thousand-dollar level, the ones nearest spot most, and nothing in the ranges "
              "between the thousands. On a daily or longer close that is a comb of bands a couple of hundred dollars wide either "
              "side of each level out to about ±2.5σ; within an hour or two the nearest level takes nearly all the weight, and when "
              "it sits to one side of spot it buys only that side."),
]
