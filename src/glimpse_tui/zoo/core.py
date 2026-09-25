"""What every zoo model is built from: the market context, the baseline picture, and the ways to bend it.

The Forecast Lab (`glimpse-time-series-forecasting-bot`) draws each model's picture by simulating thousands of
price paths against a feature database. A laptop bot has neither, so the terminal keeps the lab's *ideas* (the same
signals, the same conviction rule, the same payoffs and named distributions) and replaces the machinery with a
density on a grid:

    z      the move from spot to the close, in units of the baseline's standard deviation to that close
    pdf    an unnormalised density over `Z`, the grid of z

A model takes a `Ctx` and returns one probability per price bin. Most are one call to `conviction_picture`,
`vol_picture`, `view_picture` or `payoff_picture`; shaped processes simulate and call `from_samples`; named
distributions call `from_cdf` or write the bins themselves.

Scale (how wide today's picture is) always comes from the market, exactly as in the lab: an EWMA variance of
hourly returns deseasonalised by an hour-of-day profile, summed over the steps to the close with a partial first
hour. Shape is the model's idea.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property, lru_cache

import numpy as np
import pandas as pd
from scipy import stats
from scipy.optimize import minimize_scalar
from scipy.special import gammaln

from ..policy import Policy

Z = np.linspace(-14.0, 14.0, 5601)         # standardised move; 0.005 apart
DZ = float(Z[1] - Z[0])
PROB_FLOOR = 1e-9
VOL_MULT_BOUNDS = (0.7, 1.6)
ESS_FLOOR = 0.25                            # a tilt may never lean on less than this share of the baseline
MIN_BARS = 400                              # hourly bars a model needs before it says anything
MIN_HISTORY = 200                           # finite past readings a signal needs before it earns conviction

FAMILY_SHAPE = {
    # how each family bends the picture on top of its drift (lab decision D-092)
    "Trend": "trend", "Mean reversion": "revert", "Positioning": "squeeze", "Sentiment & flows": "squeeze",
    "Factor & carry": "trend", "Calendar & cycles": "none", "Pattern & ML": "trend", "Volatility": "none",
    "Regime & jumps": "none",
}
FAMILY_TIMING = {
    # when a family's idea is supposed to pay off
    "Trend": "ride", "Factor & carry": "ride", "Pattern & ML": "ride", "Mean reversion": "snap",
    "Positioning": "unwind", "Sentiment & flows": "unwind", "Calendar & cycles": "soon", "Volatility": "ride",
    "Regime & jumps": "ride",
}


@dataclass(frozen=True)
class Model:
    """One entry in the zoo. `fn(ctx)` returns one probability per bin of `ctx.edges`, summing to 1.

    The four texts document the model for the terminal's "about" screen: `description` is the idea, `inputs` what it
    reads from the candles, `maths` how the reading becomes a distribution of the close (the model's own part; the
    shared machinery is `PIPELINES[pipeline]` and `SCALE_NOTE`), and `trades` which ranges the picture buys against
    the market's prices and when it says nothing."""
    id: str                      # the lab's registry id, unchanged
    name: str                    # what the list shows
    family: str                  # the lab's family: Trend, Mean reversion, Volatility, Distributions, ...
    blurb: str                   # one line, under 70 characters, plain words
    description: str             # the idea, in plain words: what the model believes and why
    fn: Callable[[Ctx], np.ndarray]
    kind: str = "forecast"       # "forecast" is fitted to data; "view" is a stated opinion, scored the same way
    factors: tuple[str, ...] = ()            # direction, volatility, shape, tails, randomness, timing, market
    reference: str = ""
    inputs: str = ""             # the data it reads: columns, windows, how many bars before it says anything
    maths: str = ""              # the model's own mathematics, with the formulas and parameters the code uses
    trades: str = ""             # what the picture buys against the market, and when it leaves the baseline
    pipeline: str = ""           # key into PIPELINES: the shared machinery this picture goes through
    policy: Policy | None = None  # an opportunistic trading rule (glimpse_tui.policy); None trades fractional Kelly


SCALE_NOTE = (
    "Every picture is sized by one volatility clock, fitted once per set of candles and shared by every model. Hourly "
    "log returns r = ln(C_t) − ln(C_t−1) are divided by a 24-hour profile (the median |r| of each UTC hour, normalised "
    "so the mean squared multiplier is 1), then an exponentially weighted variance s² ← 0.94·s² + 0.06·r² gives the "
    "next hour's variance. The variance from now to the close sums profile[h]²·s²·(fraction of the hour) over every "
    "hourly step, partial first and last steps included; σ is its square root. Models work in standardised units "
    "z = (ln P_close − ln spot)/σ on a grid from −14 to +14 in steps of 0.005. The baseline is a driftless random walk "
    "whose hourly shock is Student-t with ν fitted by maximum likelihood to the standardised residuals; a sum of n "
    "such steps keeps 1/n of the excess kurtosis, so the baseline for a close n hours out is Student-t with "
    "ν_n = 4 + n·(ν − 4) (the one-hour excess kurtosis is capped at 12, so a fitted ν at or below 4.5 gives "
    "ν_n = 4 + n/2), thinning towards a bell curve as the close moves further out. The finished density is "
    "integrated between the market's price edges (the cumulative distribution at each edge, differenced), whatever "
    "falls outside the ladder goes into the end ranges, and every range keeps at least 10⁻⁹ so the picture sums to 1."
)

PIPELINES = {
    "conviction": (
        "The signal is one number per hourly bar, built causally (each reading uses only bars up to its own hour). "
        "Strength s ∈ [−1, 1] is 2·rank − 1, where rank is the mid-rank percentile of the latest reading among every "
        "finite past reading of the same signal on the loaded candles: a reading in the middle of its own history is "
        "0, the highest ever +1, the lowest ever −1. It needs at least 200 readings, otherwise s = 0 and the picture "
        "is the baseline. Drift: the centre of the picture moves by z = sign·s·0.6 baseline σ, times the family's "
        "timing multiplier for h hours to the close (ride 1; snap min(1, √(24/h)); unwind min(1, h/48); soon "
        "min(1, h/6)·min(1, √(12/h))), capped at ±1.5σ. Shape: the baseline density is exponentially tilted, weights "
        "∝ exp(θ·f(z)), with θ solved so the expected value of the family's payoff f rises by 0.45·|s| of its own "
        "baseline standard deviation, while the effective sample size stays at or above 25% of the baseline (trend and "
        "factor: f = max(z − 0.5, 0) − max(−0.5 − z, 0) on the signal's side, a fatter tail that way; reversion: "
        "f = −|z|, mass pulled to the middle; positioning and flows: f = max(z − 1, 0) − max(−1 − z, 0), a sharper move "
        "the signal's way; calendar, volatility and regime families do not bend the shape). The tilted shape is "
        "re-centred and rescaled to unit width, then shifted by the drift and widened by a multiplier m ∈ [0.7, 1.6] "
        "(1 unless the model also carries a volatility signal, then m = exp(0.25·sign·s_vol))."
    ),
    "vol": (
        "A volatility model changes the width of the baseline and nothing else. Strength s ∈ [−1, 1] is 2·rank − 1, "
        "the mid-rank percentile of the latest reading of the volatility signal among its own history on the loaded "
        "candles (at least 200 readings, otherwise s = 0). The baseline density is scaled by m = exp(0.25·sign·s), so "
        "the highest reading ever widens the picture to 1.28 times the volatility clock's spread and the lowest narrows "
        "it to 0.78 (a model that sets the multiplier directly is clipped to [0.7, 1.6]). The centre stays on spot."
    ),
    "view": (
        "A view is a stated opinion, not a fit: the baseline Student-t density is shifted so its centre sits mean_z "
        "baseline σ from spot (a fixed number, or a function of the hours to the close for a scripted path) and "
        "stretched by a fixed width multiplier. It is scored exactly like a fitted model, so a view that is right earns "
        "and one that is wrong pays."
    ),
    "payoff": (
        "The picture is the one a holder of this option position is implicitly betting on: the baseline density is "
        "exponentially tilted, weights ∝ exp(θ·payoff(z)), with the strikes in baseline σ from spot and θ solved so "
        "the expected payoff rises by the stated conviction times its own baseline standard deviation, while the "
        "effective sample size stays at or above the stated floor of the baseline. Where the payoff is large the "
        "picture puts more probability than the baseline; where it is zero, less."
    ),
    "paths": (
        "The model simulates thousands of hourly paths of the log price from spot to the close. Each hour's shock is "
        "drawn from the same volatility clock as the baseline (the standardised residual pool, redrawn at random, "
        "times that hour's standard deviation), and the model's own dynamics act on top of it. The simulated log moves "
        "are summarised by 199 quantiles joined by straight lines, with linear tails out to the most extreme path, and "
        "that curve is read at the market's price edges, so a few thousand paths do not leave holes in a 500-range "
        "ladder. The random draws are seeded by the model, the last candle and the close, so the same inputs draw the "
        "same picture."
    ),
    "distribution": (
        "The distribution of the move to the close is written directly from a named family instead of bending the "
        "baseline. The shape is described by its 199 quantiles (levels 0.005 to 0.995); it is centred on the mean of "
        "those quantiles and divided by their root-mean-square spread, so a long-tailed shape is sized by its body, "
        "then multiplied by the market's σ. Shapes with a closed-form distribution function are evaluated straight at "
        "the price edges so hard edges stay sharp; lattices place their spikes on the ladder directly, each smeared by a "
        "small Gaussian so a spike never falls between two ranges."
    ),
    "baseline": (
        "The picture is the baseline itself: the driftless Student-t random walk of the volatility clock, or a bell "
        "curve at the same σ, placed on the grid with no shift and unit width. Nothing is read beyond the clock, so "
        "the only way it differs from the market is in shape and width."
    ),
    "bins": (
        "The model writes its probabilities straight onto the market's price ranges from the data, with no density "
        "on the standardised grid in between."
    ),
}

TRADING_NOTE = (
    "The runner compares the picture with the market's price per range (a contract on a range costs its price and "
    "pays 98 sats net of the settlement fee if the close lands there). It buys a range when p_bot × 98 exceeds the "
    "price × 1.02 by at least 10% (the edge net of both fees), sizes the stake by a quarter-Kelly fraction of the "
    "budget, and never buys past the price at which the picture's edge is gone; an order whose expected value the "
    "commission (at least 1 sat) would eat is not sent. It sells a range it holds while the market pays 5% more for the "
    "next contract, after the exit fee, than the picture says it is worth, and only that many contracts: an overpaid "
    "range is trimmed back to fair value. A picture that agrees with the market buys nothing."
)


def explain(m: Model) -> list[tuple[str, str]]:
    """The about-screen sections of a model, in order, with the shared machinery filled in."""
    out = [("idea", m.description)]
    if m.inputs:
        out.append(("reads", m.inputs))
    if m.maths:
        out.append(("maths", m.maths))
    if m.trades:
        out.append(("trades", m.trades))
    if m.pipeline in PIPELINES:
        out.append(("machinery", PIPELINES[m.pipeline]))
    out.append(("scale", SCALE_NOTE))
    out.append(("runner", m.policy.note() if m.policy else TRADING_NOTE))
    if m.reference:
        out.append(("reference", m.reference))
    return out


class ModelUnavailable(Exception):
    """The model cannot draw a picture from the data it was given. The runner logs the reason and skips the cycle."""


# ── context ─────────────────────────────────────────────────

@dataclass
class Scale:
    """The lab's volatility clock, fitted once per set of bars and shared by every model."""
    profile: np.ndarray          # 24 multipliers by UTC hour of the bar start, mean(profile²) = 1
    s2_next: float               # EWMA variance of the next deseasonalised hourly return
    z_pool: np.ndarray           # standardised residuals of history
    nu: float                    # Student-t degrees of freedom of one hour's standardised move

    @classmethod
    def fit(cls, bars: pd.DataFrame, lam: float = 0.94) -> Scale:
        logp = np.log(bars["close"].to_numpy(dtype=float))
        r = pd.Series(np.diff(logp), index=bars.index[1:])
        profile = hour_of_day_profile(r)
        r_d = r.to_numpy() / profile[np.asarray(pd.DatetimeIndex(r.index).hour)]
        s2, s2_next = ewma_variance(r_d, lam)
        z = r_d / np.sqrt(s2)
        z = z[24:] if z.size > 48 else z
        z = z[np.isfinite(z)]
        return cls(profile, s2_next, z, fit_student_df(z))

    def step_variances(self, now: float, end: float) -> np.ndarray:
        """Variance of each hourly step from `now` to `end`; only the first and last steps can be partial."""
        if end <= now:
            return np.array([self.s2_next * 1e-6])
        bounds = np.concatenate([[now], np.arange((now // 3600 + 1) * 3600, end, 3600.0), [end]])
        bounds = bounds[np.concatenate([[True], np.diff(bounds) > 1e-6])]
        hours = (bounds[:-1] // 3600 % 24).astype(int)
        return self.profile[hours] ** 2 * self.s2_next * np.diff(bounds) / 3600.0


@dataclass
class Ctx:
    """Everything a model may look at. `bars` holds complete hourly bars only, oldest first, indexed by the bar's
    open time (UTC), with float columns open, high, low, close, volume: the lab's `complete_hourly_bars`."""
    bars: pd.DataFrame
    spot: float
    edges: np.ndarray            # n + 1 ascending price edges of the market's n bins
    now: float                   # unix seconds
    end: float                   # the market's close, unix seconds
    asset: str = "BTC"
    scale: Scale | None = None
    cache: dict = field(default_factory=dict)      # per-bars scratch space shared between models (signals, fits)

    def __post_init__(self) -> None:
        if len(self.bars) < MIN_BARS:
            raise ModelUnavailable(f"only {len(self.bars)} hourly bars; need {MIN_BARS}")
        if self.scale is None:
            self.scale = Scale.fit(self.bars)

    @property
    def hours(self) -> float:
        return max((self.end - self.now) / 3600.0, 1e-3)

    @property
    def log_spot(self) -> float:
        return float(np.log(self.spot))

    @cached_property
    def step_var(self) -> np.ndarray:
        """Baseline variance of each hourly step from now to the close."""
        return self.scale.step_variances(self.now, self.end)

    @cached_property
    def sigma(self) -> float:
        """Baseline standard deviation of the log move from now to the close."""
        return float(max(np.sqrt(self.step_var.sum()), 1e-9))

    @property
    def n_steps(self) -> int:
        return len(self.step_var)

    @cached_property
    def edge_z(self) -> np.ndarray:
        """The market's bin edges as standardised moves from spot."""
        return (np.log(np.maximum(self.edges, 1e-9)) - self.log_spot) / self.sigma

    @property
    def close_time(self) -> datetime:
        return datetime.fromtimestamp(self.end, UTC)


# ── the lab's volatility utilities, unchanged ───────────────

def hour_of_day_profile(returns: pd.Series, min_per_hour: int = 10) -> np.ndarray:
    prof = np.ones(24)
    if returns.empty:
        return prof
    hours = pd.DatetimeIndex(returns.index).hour.to_numpy()
    absr = np.abs(returns.to_numpy(dtype=float))
    overall = float(np.median(absr)) if absr.size else 1.0
    for h in range(24):
        m = hours == h
        prof[h] = float(np.median(absr[m])) if m.sum() >= min_per_hour else overall
    prof = np.where(prof > 0, prof, overall if overall > 0 else 1.0)
    return prof / np.sqrt(np.mean(prof**2))


def ewma_variance(x: np.ndarray, lam: float = 0.94, init_window: int = 100) -> tuple[np.ndarray, float]:
    """sigma2[t] built from x[:t] (never sees x[t]) plus the next-period value."""
    x = np.asarray(x, dtype=float)
    n = x.size
    if n == 0:
        return np.array([]), 0.0
    init = float(np.mean(x[: min(init_window, n)] ** 2))
    if not np.isfinite(init) or init <= 0:
        init = float(np.var(x)) if n > 1 else 1e-8
    s2 = np.empty(n)
    s2[0] = init
    for t in range(1, n):
        s2[t] = lam * s2[t - 1] + (1.0 - lam) * x[t - 1] ** 2
    s2_next = lam * s2[-1] + (1.0 - lam) * x[-1] ** 2
    return np.maximum(s2, 1e-12), max(float(s2_next), 1e-12)


def fit_student_df(z: np.ndarray, lo: float = 2.5, hi: float = 200.0) -> float:
    """MLE of the degrees of freedom of a unit-variance Student-t."""
    z = np.asarray(z, dtype=float)
    z = z[np.isfinite(z)]
    if z.size < 20:
        return 5.0
    z = z / max(float(np.std(z)), 1e-12)

    def nll(nu: float) -> float:
        s = np.sqrt((nu - 2.0) / nu)
        t = z / s
        ll = gammaln((nu + 1) / 2) - gammaln(nu / 2) - 0.5 * np.log(nu * np.pi) - np.log(s)
        return -float(np.sum(ll - (nu + 1) / 2 * np.log1p(t * t / nu)))

    res = minimize_scalar(nll, bounds=(lo, hi), method="bounded", options={"xatol": 1e-3})
    return float(max(lo, min(hi, res.x)))


# ── densities on the grid ───────────────────────────────────

def normalise(pdf: np.ndarray) -> np.ndarray:
    p = np.maximum(np.asarray(pdf, dtype=float), 0.0)
    s = p.sum() * DZ
    if not np.isfinite(s) or s <= 0:
        raise ModelUnavailable("degenerate picture")
    return p / s


def t_pdf(nu: float) -> np.ndarray:
    """Unit-variance Student-t on the grid. Read-only and shared: nearly every model starts from the baseline, whose
    shape depends on ν alone, so each ν is evaluated once rather than once per model per close."""
    return _t_pdf(max(float(nu), 2.2))


@lru_cache(maxsize=1024)
def _t_pdf(nu: float) -> np.ndarray:
    s = np.sqrt((nu - 2.0) / nu)
    out = normalise(stats.t.pdf(Z / s, nu) / s)
    out.flags.writeable = False
    return out


def base_pdf(ctx: Ctx) -> np.ndarray:
    """The baseline picture: a random walk whose one-hour move is Student-t with the fitted tails. A sum of n such
    steps keeps 1/n of the excess kurtosis, so the tails thin towards a bell curve as the close moves further out,
    which is what the lab's bootstrap does by construction."""
    nu = ctx.scale.nu
    k1 = 6.0 / (nu - 4.0) if nu > 4.5 else 12.0
    return t_pdf(4.0 + 6.0 / max(k1 / max(ctx.n_steps, 1), 1e-3))


def moments(pdf: np.ndarray) -> tuple[float, float]:
    p = normalise(pdf) * DZ
    m = float(np.dot(p, Z))
    return m, float(np.sqrt(max(np.dot(p, (Z - m) ** 2), 1e-18)))


def transform(pdf: np.ndarray, shift: float = 0.0, scale: float = 1.0) -> np.ndarray:
    """The density of scale * z + shift."""
    scale = max(float(scale), 1e-6)
    return normalise(np.interp((Z - shift) / scale, Z, pdf, left=0.0, right=0.0) / scale)


def call(z: np.ndarray, k: float) -> np.ndarray:
    return np.maximum(z - k, 0.0)


def put(z: np.ndarray, k: float) -> np.ndarray:
    return np.maximum(k - z, 0.0)


def tilt(pdf: np.ndarray, payoff: np.ndarray, conviction: float, min_ess: float = ESS_FLOOR) -> np.ndarray:
    """The minimum relative-entropy reweighting of `pdf` under which the payoff's expected value rises by
    `conviction` of its own baseline standard deviations: weights proportional to exp(theta * f). Theta is capped so
    the effective sample size never falls below `min_ess` of the baseline (the lab's `solve_tilt`)."""
    p = normalise(pdf) * DZ
    f = np.asarray(payoff, dtype=float)
    mean = float(np.dot(p, f))
    sd = float(np.sqrt(max(np.dot(p, (f - mean) ** 2), 0.0)))
    if sd <= 1e-12 or conviction <= 0:
        return normalise(pdf)
    fs = (f - mean) / sd

    def at(theta: float) -> tuple[np.ndarray, float, float]:
        a = theta * fs
        w = p * np.exp(a - a.max())
        w /= w.sum()
        ratio = np.divide(w, p, out=np.zeros_like(w), where=p > 0)
        return w, float(np.dot(w, fs)), float(1.0 / max(np.dot(w, ratio), 1e-300))

    lo, hi = 0.0, 1.0
    while at(hi)[1] < conviction and at(hi)[2] >= min_ess and hi < 64:
        hi *= 2
    for _ in range(50):
        mid = (lo + hi) / 2
        _, m, ess = at(mid)
        lo, hi = (mid, hi) if m < conviction and ess >= min_ess else (lo, mid)
    return normalise(at(lo)[0] / DZ)


def timing_profile(style: str, hours: float) -> float:
    """Multiplier on the conviction drift by horizon: each family's idea pays off on its own clock."""
    h = max(float(hours), 1e-6)
    if style == "snap":          # the snap-back happens inside a day, then the price sits at its level
        return min(1.0, np.sqrt(24.0 / h))
    if style == "unwind":        # a crowded trade takes a day or two to break
        return min(1.0, h / 48.0)
    if style == "soon":          # lands within half a day and does not compound
        return min(1.0, h / 6.0) * min(1.0, np.sqrt(12.0 / h))
    return 1.0                   # "ride"


def shape_payoff(style: str, direction: float) -> np.ndarray | None:
    if style == "trend":         # fatten the tail the trend points to, thin the other
        return direction * (call(Z, 0.5) - put(Z, -0.5))
    if style == "revert":        # pull mass towards the middle
        return -np.abs(Z)
    if style == "squeeze":       # a violent move on the signal's side
        return direction * (call(Z, 1.0) - put(Z, -1.0))
    return None


# ── signals to pictures ─────────────────────────────────────

def strength_of(signal: pd.Series | np.ndarray) -> float:
    """Where the latest reading sits in the signal's own history: -1 (lowest ever) to +1 (highest ever). A
    percentile, not a z-score, so one wild reading cannot swamp the picture and every signal is on one scale."""
    v = np.asarray(signal, dtype=float)
    if v.size == 0 or not np.isfinite(v[-1]):
        return 0.0
    hist = np.sort(v[np.isfinite(v)])
    if hist.size < MIN_HISTORY:
        return 0.0
    # mid-rank: a signal that is silent most of the time (exactly zero) must read as neutral while silent, which
    # the lab's left-rank does not give
    rank = 0.5 * float(np.searchsorted(hist, v[-1], side="left") + np.searchsorted(hist, v[-1], side="right")) / hist.size
    return float(np.clip(2.0 * rank - 1.0, -1.0, 1.0))


def conviction_picture(
    ctx: Ctx, signal: pd.Series | np.ndarray | None, *, sign: int = 1, family: str = "Trend", conviction: float = 0.6,
    shape_conviction: float = 0.45, timing: str | None = None, shape: str | None = None,
    vol_signal: pd.Series | np.ndarray | None = None, vol_sign: int = 1, vol_conviction: float = 0.5,
    vol_mult: float = 1.0,
) -> np.ndarray:
    """The picture a strategy claims, at a strength set by where its signal sits in its own history.

    Drift: z = sign * strength * conviction, on the family's clock. Shape: the family's tilt, at
    shape_conviction * |strength|. Width: exp(0.5 * vol_sign * vol_strength * vol_conviction), within [0.7, 1.6].
    A signal with no reading, or too little history, leaves the baseline.
    """
    pdf = base_pdf(ctx)
    s = strength_of(signal) if signal is not None else 0.0
    mult = float(vol_mult)
    if vol_signal is not None:
        mult *= float(np.exp(0.5 * vol_sign * strength_of(vol_signal) * vol_conviction))
    mult = float(np.clip(mult, *VOL_MULT_BOUNDS))
    direction = float(sign * s)
    style = shape if shape is not None else FAMILY_SHAPE.get(family, "none")
    f = shape_payoff(style, 1.0 if direction >= 0 else -1.0)
    if f is not None and abs(s) > 1e-6:
        pdf = tilt(pdf, f, shape_conviction * abs(s))
        m, sd = moments(pdf)
        pdf = transform(pdf, shift=-m / sd, scale=1.0 / sd)       # the tilt bends the shape; drift and width are set below
    z = float(np.clip(direction * conviction, -1.5, 1.5)) * timing_profile(timing or FAMILY_TIMING.get(family, "ride"), ctx.hours)
    return to_bins(ctx, transform(pdf, shift=z * mult, scale=mult))


def vol_picture(ctx: Ctx, vol_signal: pd.Series | np.ndarray, *, sign: int = 1, vol_conviction: float = 0.5) -> np.ndarray:
    """A volatility model's whole picture is its width."""
    return conviction_picture(ctx, None, family="Volatility", vol_signal=vol_signal, vol_sign=sign, vol_conviction=vol_conviction)


def view_picture(ctx: Ctx, mean_z: float | Callable[[float], float] = 0.0, vol_mult: float = 1.0,
                 pdf: np.ndarray | None = None) -> np.ndarray:
    """A fixed view: the median sits `mean_z` baseline sigmas from spot (a number, or a function of hours to the
    close for scripted paths), with the spread scaled by `vol_mult`."""
    mz = float(mean_z(ctx.hours)) if callable(mean_z) else float(mean_z)
    return to_bins(ctx, transform(base_pdf(ctx) if pdf is None else pdf, shift=mz, scale=vol_mult))


def payoff_picture(ctx: Ctx, payoff: Callable[[np.ndarray, float], np.ndarray], conviction: float = 0.3,
                   min_ess: float = ESS_FLOOR) -> np.ndarray:
    """The picture the holder of an option position is betting on: `payoff(z, hours)` with strikes in sigmas."""
    return to_bins(ctx, tilt(base_pdf(ctx), payoff(Z, ctx.hours), conviction, min_ess))


# ── onto the market's bins ──────────────────────────────────

def finish(probs: np.ndarray) -> np.ndarray:
    p = np.maximum(np.nan_to_num(np.asarray(probs, dtype=float), nan=0.0, posinf=0.0), 0.0)
    if p.sum() <= 0:
        raise ModelUnavailable("the picture puts no probability on this market's price range")
    p = np.maximum(p / p.sum(), PROB_FLOOR)
    return p / p.sum()


def to_bins(ctx: Ctx, pdf: np.ndarray) -> np.ndarray:
    """One probability per bin. Whatever falls outside the ladder goes into the end bins, so the result sums to 1."""
    c = np.cumsum(normalise(pdf)) * DZ
    c /= c[-1]
    at = np.interp(ctx.edge_z, Z, c, left=0.0, right=1.0)
    at[0], at[-1] = 0.0, 1.0
    return finish(np.diff(at))


def from_cdf(ctx: Ctx, cdf: Callable[[np.ndarray], np.ndarray], shift: float = 0.0, scale: float = 1.0) -> np.ndarray:
    """Bins from a closed-form CDF of the standardised move, evaluated at the bin edges so hard edges stay sharp."""
    at = np.maximum.accumulate(np.clip(np.asarray(cdf((ctx.edge_z - shift) / max(scale, 1e-6)), dtype=float), 0.0, 1.0))
    at[0], at[-1] = 0.0, 1.0
    return finish(np.diff(at))


def from_samples(ctx: Ctx, log_moves: np.ndarray, smooth: bool = True) -> np.ndarray:
    """Bins from simulated log moves (close / spot, in log units, not standardised). 199 quantiles joined by straight
    lines, as the lab converts its paths, so a few thousand paths do not leave holes in a 500-bin ladder."""
    x = np.asarray(log_moves, dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 200:
        raise ModelUnavailable("too few simulated paths")
    if not smooth:
        counts, _ = np.histogram(ctx.log_spot + x, bins=np.log(np.maximum(ctx.edges, 1e-9)))
        return finish(counts.astype(float))
    levels = np.arange(1, 200) / 200.0
    q = np.maximum.accumulate(np.quantile(x, levels)) + np.arange(199) * 1e-12
    # linear tails beyond the 0.5% and 99.5% quantiles, out to the most extreme path
    q = np.concatenate([[min(x.min(), q[0]) - 1e-9], q, [max(x.max(), q[-1]) + 1e-9]])
    lv = np.concatenate([[0.0], levels, [1.0]])
    at = np.interp(np.log(np.maximum(ctx.edges, 1e-9)) - ctx.log_spot, q, lv, left=0.0, right=1.0)
    at[0], at[-1] = 0.0, 1.0
    return finish(np.diff(at))


def rng_for(ctx: Ctx, model_id: str) -> np.random.Generator:
    """Reproducible per (model, last bar, close): the same inputs draw the same picture."""
    import zlib

    return np.random.default_rng([zlib.crc32(model_id.encode()), int(ctx.bars.index[-1].timestamp()), int(ctx.end)])


def cached(ctx: Ctx, key: str, build: Callable[[], object]) -> object:
    """Fits and signal series depend on the bars, not on the close: compute once per bar and share across closes.
    `ctx.cache` is handed from one Ctx to the next for as long as the bars are unchanged."""
    if key not in ctx.cache:
        ctx.cache[key] = build()
    return ctx.cache[key]


# ── reading a picture ───────────────────────────────────────

def describe(ctx: Ctx, probs: np.ndarray) -> dict[str, float | str]:
    """The stance of a picture against the baseline: where its middle sits and how wide it is."""
    mid = np.log(ctx.edges[1:])                 # the cumulative sum is the probability below each bin's upper edge
    c = np.cumsum(probs)
    q10, q50, q90 = (float(np.interp(lv, c, mid)) for lv in (0.1, 0.5, 0.9))
    base = cached(ctx, f"base-band:{ctx.end}", lambda: np.interp((0.1, 0.9), np.cumsum(to_bins(ctx, base_pdf(ctx))), mid))
    centre = np.log((ctx.edges[:-1] + ctx.edges[1:]) / 2.0)
    lean = (float(np.dot(probs, centre)) - ctx.log_spot) / ctx.sigma     # the mean: a skewed shape's median sits on its thin side
    width = (q90 - q10) / max(float(base[1] - base[0]), 1e-12)
    if lean > 0.10:
        view = "bullish"
    elif lean < -0.10:
        view = "bearish"
    elif width > 1.12:
        view = "volatile"
    elif width < 0.92:
        view = "sideways"
    else:
        view = "neutral"
    return {"lean": float(lean), "width": float(width), "view": view, "median": float(np.exp(q50)),
            "low": float(np.exp(q10)), "high": float(np.exp(q90))}
