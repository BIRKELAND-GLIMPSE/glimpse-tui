"""Smoothing bots: the forecast surface as one Bayesian object, traded where a close is out of line with it.

glimpse.markets draws a series as a surface: one distribution per close, the closes side by side by horizon. Every
ticket moves one range of one close, so the surface is jagged where the latest tickets landed, and a close can sit out
of line with the ones either side of it. But the closes are not independent bets. The price that settles the 3 pm
close is the price that settles the 4 pm close, give or take an hour's move, and a random walk has one picture in
standardised units at every horizon. So a close whose curve disagrees with its neighbours' is carrying a trade, not an
opinion, and the ranges that trade left cheap are worth more than they cost.

These bots read the whole series the site shows, carry every neighbour's curve to the focused close's horizon, and
combine them with a time-series picture (the zoo's consensus of how far the price moves, Benter's fundamental) by
Bayesian methods, each bot one way:

    surface pool      the log-linear pool of the carried curves and the fundamental, weighted by evidence
    dirichlet blend   the conjugate update: each close's curve as pseudo-counts on a Dirichlet prior at the fundamental
    kalman surface    a Markov state-space model of the surface's lean and width across horizons, smoothed both ways
    particle bridge   Monte Carlo: paths of the price weighted by how well they fit every close's curve on the way
    band match        the fundamental's shape moved so its 80% band is the surface's

Their shared premise is that the market's bands are calibrated: a close's 10–90% band is right about 80% of the time.
So where the surface has formed, each picture keeps 80% of its mass inside the band the surface shows, and smoothing
decides the shape inside and outside it rather than the width. Where the focused close prices a range under that
picture, the bot buys it, by fractional Kelly against a bankroll set in advance, so each buy has positive expected
value and the surface it leaves behind is a little smoother. One close every 3 seconds, nearest to farthest, then
round again.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import benter
from .core import Ctx, Model, finish, from_samples, rng_for

WINDOW = 12            # closes read on each side of the focused one
KERNEL = 0.6           # a neighbour's weight fades as exp(−|ln(h / h_focused)| / KERNEL): twice as far out weighs 0.31
BETA = 0.5             # the most of a picture the surface takes against the fundamental, once fully formed
BAND_MASS = 0.8        # the premise: the surface's central band holds the close this often
MIN_FORMED = 0.02      # a close less formed than this is not evidence
FLOOR = 1e-9
PATHS = 2000           # particle bridge: paths simulated
TEMPER = 1.5           # particle bridge: the closes' curves are raised to exponents summing to this many formed closes
Q_LEAN, Q_WIDTH = 0.3**2, 0.2**2        # kalman: process variance per unit log-horizon of the lean (in σ) and the log width
R_LEAN, R_WIDTH = 0.5**2, 0.35**2       # kalman: observation variance of a fully formed close's lean and log width


# ── curves on the surface ───────────────────────────────────

def sigma_to(ctx: Ctx, end: float) -> float:
    """Baseline σ of the log move from now to `end`, from the shared volatility clock."""
    if abs(end - ctx.end) < 1:
        return ctx.sigma
    return float(max(np.sqrt(ctx.scale.step_variances(ctx.now, end).sum()), 1e-9))


def cdf_at(probs: np.ndarray, zedges: np.ndarray, z: np.ndarray) -> np.ndarray:
    """A curve's cumulative distribution, known at its range edges `zedges`, read at the standardised moves `z`."""
    c = np.concatenate([[0.0], np.cumsum(probs)])
    c /= c[-1]
    return np.interp(z, zedges, c, left=0.0, right=1.0)


def rebin(probs: np.ndarray, zedges: np.ndarray, z: np.ndarray) -> np.ndarray:
    """The curve whose cumulative distribution at the range edges is `probs`' at `z`: one probability per range."""
    at = cdf_at(probs, zedges, z)
    at[0], at[-1] = 0.0, 1.0
    return finish(np.diff(at))


def carry(probs: np.ndarray, sigma_from: float, sigma_to_: float, zedges: np.ndarray) -> np.ndarray:
    """A close's curve carried to another horizon: the same picture in standardised units. `zedges` are the ranges'
    edges in the destination's z; in the source's own z the same prices sit σ_to/σ_from as far out."""
    return rebin(probs, zedges * (sigma_to_ / sigma_from), zedges)


def formed(curve: np.ndarray, f: np.ndarray) -> float:
    """How much of the structure the fundamental expects a close's traders have formed: the curve's entropy below the
    flat ladder's, over the fundamental's. 0 flat, 1 as sharp as f or sharper."""
    def h(p: np.ndarray) -> float:
        return -float(np.dot(p, np.log(np.maximum(p, FLOOR))))

    n = math.log(len(curve))
    return float(np.clip((n - h(curve)) / max(n - h(f), 1e-9), 0.0, 1.0))


def pool(curves: list[np.ndarray], weights) -> np.ndarray:
    """The log-linear pool: Σ w ln p / Σ w, renormalised."""
    w = np.asarray(weights, dtype=float)
    lw = sum(wi * np.log(np.maximum(c, FLOOR)) for wi, c in zip(w, curves, strict=True)) / w.sum()
    return finish(np.exp(lw - lw.max()))


def strip(p: np.ndarray) -> np.ndarray:
    """The curve with the subsidy floor taken off, as the site draws it: the 25th-percentile mass subtracted."""
    lifted = np.maximum(p - np.quantile(p, 0.25), 0.0)
    return finish(lifted) if lifted.sum() > 0 else finish(p)


def quantiles(probs: np.ndarray, zedges: np.ndarray, levels) -> np.ndarray:
    """The standardised moves at which the curve's cumulative distribution crosses `levels`."""
    c = np.concatenate([[0.0], np.cumsum(probs)])
    c /= c[-1]
    return np.interp(levels, c, zedges)


def moments(probs: np.ndarray, zedges: np.ndarray) -> tuple[float, float]:
    """(median, half the 10–90% band) of a curve in standardised moves, the subsidy floor taken off first."""
    q10, q50, q90 = quantiles(strip(probs), zedges, [0.1, 0.5, 0.9])
    return float(q50), float(max((q90 - q10) / 2, 1e-6))


def band(probs: np.ndarray, mass: float = BAND_MASS) -> tuple[int, int]:
    """(first, last) range indices of the curve's central `mass`: the band the site shows for it."""
    c = np.cumsum(probs)
    n = len(probs) - 1
    lo = min(int(np.searchsorted(c, (1 - mass) / 2, side="left")), n)
    hi = min(int(np.searchsorted(c, 1 - (1 - mass) / 2, side="left")), n)
    return lo, max(hi, lo)


def pin(c: np.ndarray, ref: np.ndarray, strength: float, mass: float = BAND_MASS) -> np.ndarray:
    """`c` tempered (c^k, renormalised) so that it puts the same mass inside `ref`'s central band as `ref` does, fully at
    strength 1 (k ← 1 + strength·(k − 1)): the premise that the band is right. k is found by bisection in [0.4, 3]; a
    curve whose body sits outside the band is tempered as far as those bounds allow."""
    if strength <= 0:
        return c
    ref = strip(ref)
    lo, hi = band(ref, mass)
    target = float(ref[lo:hi + 1].sum())
    lc = np.log(np.maximum(c, FLOOR))
    lc -= lc.max()

    def inside(k: float) -> float:
        p = np.exp(k * lc)
        return float(p[lo:hi + 1].sum() / p.sum())

    a, b = 0.4, 3.0
    if inside(a) >= target:
        k = a
    elif inside(b) <= target:
        k = b
    else:
        for _ in range(40):
            m = (a + b) / 2
            a, b = (m, b) if inside(m) < target else (a, m)
        k = (a + b) / 2
    return finish(np.exp((1 + strength * (k - 1)) * lc))


@dataclass(eq=False)
class Close:
    """One close of the series as the surface reads it."""
    end: float
    hours: float
    sigma: float             # baseline σ of the log move from now to it
    curve: np.ndarray        # the site's curve for it over the series' ranges, smoothed
    carried: np.ndarray      # the same picture at the focused close's horizon
    formed: float            # how much of the structure the fundamental expects its traders have formed
    kernel: float            # 1 at the focused close, fading with distance in log-horizon

    @property
    def weight(self) -> float:
        return self.formed * self.kernel


class Surface:
    """The series as the site shows it, around the close being priced: the focused close first, then the WINDOW closes
    on either side of it, each carried to the focused horizon and weighed."""

    def __init__(self, ctx: Ctx, f: np.ndarray, window: int = WINDOW) -> None:
        self.ctx, self.f = ctx, f
        self.zedges = ctx.edge_z
        n = len(f)
        rows = sorted((s for s in ctx.series if len(s.probs) == n and s.end > ctx.now), key=lambda s: s.end)
        k = next((i for i, s in enumerate(rows) if abs(s.end - ctx.end) < 1), None)
        own = ctx.market if ctx.market is not None and len(ctx.market) == n else rows[k].probs if k is not None else None
        self.focus = (self._close(ctx.end, np.asarray(own, dtype=float), ctx.sigma) if own is not None
                      else Close(ctx.end, ctx.hours, ctx.sigma, f, f, 0.0, 1.0))
        self.closes = [self.focus]
        at = k if k is not None else int(np.searchsorted([s.end for s in rows], ctx.end))
        for i in range(max(at - window, 0), min(at + window, len(rows) - 1) + 1):
            if i != k:
                self.closes.append(self._close(rows[i].end, np.asarray(rows[i].probs, dtype=float), sigma_to(ctx, rows[i].end)))

    def _close(self, end: float, probs: np.ndarray, sigma: float) -> Close:
        """Smoothed, then the subsidy floor taken off (the site draws it that way too), so an untraded corner of a
        lightly traded neighbour is not carried over as an opinion about the tails. Formedness is measured on the
        close's own ladder with the floor still on, against the fundamental carried to that close's horizon: a lone
        ticket on a flat ladder is not a formed opinion however sharp what it leaves looks, and a flat ladder is none."""
        raw = benter.smooth(probs)
        curve = strip(raw)
        own = abs(end - self.ctx.end) < 1
        carried = curve if own else carry(curve, sigma, self.ctx.sigma, self.zedges)
        f_there = self.f if own else carry(self.f, self.ctx.sigma, sigma, self.zedges)
        hours = max((end - self.ctx.now) / 3600.0, 1e-3)
        return Close(end, hours, sigma, curve, carried, formed(raw, f_there), math.exp(-abs(math.log(hours / self.ctx.hours)) / KERNEL))

    @property
    def evidence(self) -> float:
        """How many formed closes' worth the surface has shown: 0 for a flat series, towards 1 as closes form."""
        return 1.0 - math.exp(-sum(c.weight for c in self.closes))

    def belief(self) -> np.ndarray | None:
        """The surface's own picture of the focused close: the pool of every close's carried curve, by weight."""
        ws = [c.weight for c in self.closes]
        return pool([c.carried for c in self.closes], ws) if sum(ws) > 0 else None

    def posterior(self, market: np.ndarray) -> np.ndarray:
        """The family's combination: the fundamental and `market`'s picture pooled by evidence, pinned to the band."""
        ref, w = self.belief(), self.evidence
        if ref is None or w <= 0:
            return self.f
        return pin(pool([self.f, market], [1 - BETA * w, BETA * w]), ref, w)


def _surface(ctx: Ctx) -> tuple[np.ndarray, Surface | None]:
    f = np.asarray(benter.fundamental(ctx), dtype=float)
    if ctx.market is None and not ctx.series:
        return f, None                                     # nothing of the series to read: the fundamental alone
    return f, Surface(ctx, f)


# ── the five ways ───────────────────────────────────────────

def surface_pool(ctx: Ctx) -> np.ndarray:
    f, s = _surface(ctx)
    if s is None or s.belief() is None:
        return f
    return s.posterior(s.belief())


def dirichlet_blend(ctx: Ctx) -> np.ndarray:
    f, s = _surface(ctx)
    if s is None:
        return f
    ws = np.array([c.weight for c in s.closes])
    w = s.evidence
    if ws.sum() <= 0 or w <= 0:
        return f
    mix = sum(wi / ws.sum() * c.carried for wi, c in zip(ws, s.closes, strict=True))
    return pin(finish((1 - BETA * w) * f + BETA * w * mix), s.belief(), w)


def rts(obs: list[list[tuple[float, float]]], du: np.ndarray, x0: float, p0: float, q: float) -> tuple[np.ndarray, np.ndarray]:
    """A scalar random walk across the closes, observed with noise and smoothed both ways (Rauch–Tung–Striebel).
    `obs[j]` lists the (value, variance) pairs witnessed at close j, folded in turn; `du[j]` is the distance in
    log-horizon from close j to j + 1, over which the walk adds variance q·du. Returns smoothed means and variances."""
    n = len(obs)
    xp, pp, xf, pf = (np.empty(n) for _ in range(4))
    x, p = x0, p0
    for j in range(n):
        if j:
            x, p = xf[j - 1], pf[j - 1] + q * du[j - 1]
        xp[j], pp[j] = x, p
        for y, r in obs[j]:
            g = p / (p + r)
            x, p = x + g * (y - x), (1 - g) * p
        xf[j], pf[j] = x, p
    xs, ps = xf.copy(), pf.copy()
    for j in range(n - 2, -1, -1):
        c = pf[j] / pp[j + 1] if pp[j + 1] > 0 else 0.0
        xs[j] = xf[j] + c * (xs[j + 1] - xp[j + 1])
        ps[j] = pf[j] + c * c * (ps[j + 1] - pp[j + 1])
    return xs, ps


def kalman_surface(ctx: Ctx) -> np.ndarray:
    f, s = _surface(ctx)
    if s is None:
        return f
    closes = sorted(s.closes, key=lambda c: c.hours)
    mf, sf = moments(f, s.zedges)
    lean_obs, width_obs = [], []
    for c in closes:
        lean, width = [(mf, R_LEAN)], [(0.0, R_WIDTH)]                 # the fundamental witnesses every close
        if c.formed >= MIN_FORMED:
            m, sd = moments(c.carried, s.zedges)
            lean.append((m, R_LEAN / c.formed))
            width.append((math.log(sd / sf), R_WIDTH / c.formed))
        lean_obs.append(lean)
        width_obs.append(width)
    du = np.abs(np.diff(np.log([c.hours for c in closes])))
    lean, _ = rts(lean_obs, du, mf, 1.0, Q_LEAN)
    width, _ = rts(width_obs, du, 0.0, 0.5, Q_WIDTH)
    k = next(i for i, c in enumerate(closes) if c is s.focus)
    return rebin(f, s.zedges, mf + (s.zedges - lean[k]) * math.exp(-width[k]))


def particle_bridge(ctx: Ctx) -> np.ndarray:
    f, s = _surface(ctx)
    if s is None:
        return f
    witnesses = [c for c in s.closes if c.formed >= MIN_FORMED]
    if not witnesses or s.evidence <= 0:
        return f
    rng = rng_for(ctx, "smooth_particles")
    # The paths are only ever read at the closes' times, so they are stepped from one close to the next: a step's shock
    # is the sum of one draw from the clock's standardised residual pool per hour of the step (a day's worth at most),
    # scaled to the baseline variance of the step. Hourly tails for an hourly series, a day's for a daily one, and a
    # close months out costs no more than one next week.
    times = sorted({c.end for c in witnesses} | {ctx.end})
    var = np.maximum(np.diff([0.0] + [sigma_to(ctx, t) ** 2 for t in times]), 1e-18)
    at, x, prev = {}, np.zeros(PATHS), ctx.now
    for t, v in zip(times, var, strict=True):
        k = int(min(max(round((t - prev) / 3600.0), 1), 24))
        x = x + rng.choice(ctx.scale.z_pool, size=(PATHS, k)).sum(axis=1) * math.sqrt(v / k)
        at[t], prev = x, t
    ledges = np.log(np.maximum(ctx.edges, 1e-9))
    total = sum(c.weight for c in witnesses)
    lw = np.zeros(PATHS)
    for c in witnesses:
        bins = np.clip(np.searchsorted(ledges, ctx.log_spot + at[c.end], side="right") - 1, 0, len(f) - 1)
        lw += TEMPER * c.weight / total * np.log(np.maximum(c.curve, FLOOR))[bins]
    w = np.exp(lw - lw.max())
    pick = rng.choice(PATHS, PATHS, p=w / w.sum())
    return s.posterior(from_samples(ctx, at[ctx.end][pick]))


def band_match(ctx: Ctx) -> np.ndarray:
    f, s = _surface(ctx)
    if s is None:
        return f
    ref, w = s.belief(), s.evidence
    if ref is None or w <= 0:
        return f
    levels = [(1 - BAND_MASS) / 2, 1 - (1 - BAND_MASS) / 2]
    bf, bm = quantiles(f, s.zedges, levels), quantiles(strip(ref), s.zedges, levels)
    lo, hi = (1 - w) * bf + w * bm
    cf, wf = (bf[0] + bf[1]) / 2, max(bf[1] - bf[0], 1e-6)
    return rebin(f, s.zedges, cf + (s.zedges - (lo + hi) / 2) * (wf / max(hi - lo, 1e-6)))


# ── the bots ────────────────────────────────────────────────

TRADING = {"objective": "edge", "interval_s": 3, "kelly": 0.2, "min_edge": 0.06, "fill_margin": 0.03, "exit_edge": 0.06,
           "edge_close_share": 0.02}

REF = ("Genest and Zidek (1986), 'Combining probability distributions: a critique and an annotated bibliography'; "
       "Rauch, Tung and Striebel (1965), 'Maximum likelihood estimates of linear dynamic systems'; Gordon, Salmond and "
       "Smith (1993), the bootstrap particle filter; Benter (1994), 'Computer based horse race handicapping and wagering "
       "systems'; Kelly (1956)")

INPUTS = ("Everything the consensus reads (hourly closes through the shared volatility clock, plus its seven members' "
          "fits, made once per set of candles), and the series itself: the shares of every open close, the 12 on either "
          "side of the one being priced, drawn as the site draws them (the LS-LMSR softmax over the ladder). The focused "
          "close comes from its book, read on this visit; the others from the runner's latest read of each, or the "
          "series listing where it has not visited one yet. It draws once the 400-bar minimum is met; shown no series "
          "(the bots screen's scan, a one-off bet) it draws the consensus alone.")

TRADES = ("Fractional Kelly at 0.2 on ranges whose value by the picture, after both 2% fees, is at least 6% above the "
          "price, never past the price at which a 3% discount would be gone, so every contract is bought under its value "
          "and the deepest dips in the surface are filled first. At most 2% of the budget held in one close, and never "
          "more than the close's even share of it; one close every 3 seconds, nearest to farthest and round again. It "
          "takes profit on a range the market later prices 6% or more above the picture and above what it cost, selling "
          "it back down, often only in part. These rules are its own: bots.toml does not change them, except "
          "profit_only = false, which also trims overpriced losers.")


def _bot(model_id: str, name: str, blurb: str, description: str, fn, *, maths: str,
         factors: tuple[str, ...] = ("volatility", "tails", "market")) -> Model:
    return Model(model_id, name, "Smoothing", blurb, description, fn, kind="forecast", factors=factors, reference=REF,
                 inputs=INPUTS, maths=maths, trades=TRADES, pipeline="surface", reads_market=True, reads_series=True,
                 trading=TRADING)


MODELS = [
    _bot("smooth_pool", "Surface Pool", "The series pooled in log space; buys where a close dips below it",
         "The workhorse of the family. It reads every close of the series the site shows, carries each neighbour's curve "
         "to the close it is pricing (a random walk looks the same in units of its own spread at every horizon, so a "
         "neighbour's curve is the same shape over a wider or narrower span of prices), and pools them with the zoo's "
         "consensus in log space, each close weighing what its traders have formed and how near it is in horizon. The "
         "pooled picture is the smooth surface the series is trying to draw. Where the focused close prices a range "
         "under it, a dip the last tickets left, the bot buys that range in a small Kelly stake; the surface it leaves is "
         "a little smoother and each buy was made under value. It keeps 80% of its picture inside the band the surface "
         "shows, on the premise that the market's bands are right four times in five.",
         surface_pool,
         maths="ln c_i = (1 − β·w)·ln f_i + β·w·ln π̄_i + const, with f the consensus, π̄ the surface's pool of the carried "
               "curves (Σ ω_j ln π̃_j / Σ ω_j, see the machinery), β = ½ and w = 1 − exp(−Σ ω_j) the surface's evidence. "
               "Then c is tempered, c_i ← c_i^k renormalised, with k ∈ [0.4, 3] found by bisection so that c puts the same "
               "mass inside π̄'s 10–90% band as π̄ does, at strength w (k ← 1 + w·(k − 1)). With no formed close in sight, "
               "w = 0 and the picture is f."),
    _bot("smooth_dirichlet", "Dirichlet Blend", "A conjugate update: each close's curve as pseudo-counts on a prior",
         "The textbook Bayesian update, with the series as the data. The close's probabilities over the ranges are a "
         "categorical; the prior on them is a Dirichlet whose mean is the zoo's consensus; every close the site shows, "
         "carried to this horizon, is a batch of pseudo-counts in proportion to how formed and how near it is. The "
         "posterior mean is a weighted average of the consensus and the neighbours' curves, a linear pool rather than "
         "the Surface Pool's log-linear one, so where a neighbour keeps a fat tail the blend keeps it too, and a single "
         "spike is diluted rather than sharpened. It buys the ranges the focused close prices under that average, in "
         "small Kelly stakes, and keeps 80% of its picture inside the surface's band.",
         dirichlet_blend,
         maths="c_i = (1 − β·w)·f_i + β·w·Σ_j (ω_j / Σω)·π̃_j,i: the posterior mean of a Dirichlet prior with mean f, updated "
               "with each carried curve π̃_j as pseudo-counts in proportion to its weight ω_j = formed_j × kernel_j, β = ½ "
               "and w the surface's evidence. Then c is tempered to put the same mass inside the surface pool π̄'s 10–90% "
               "band as π̄ does, at strength w, exactly as the Surface Pool is."),
    _bot("smooth_kalman", "Kalman Surface", "A Markov model of the surface's centre and width along the horizon",
         "The surface seen as a path: at each horizon the market's picture has a centre and a width, and from one close "
         "to the next both should drift, not jump. The bot treats the centre (in baseline σ) and the log width of each "
         "close as the hidden state of a random walk along the horizon axis, a Markov model observed through what each "
         "close's traders have drawn, noisily where a close is barely formed, with the zoo's consensus as a witness at "
         "every horizon. A Kalman filter runs from the nearest close to the farthest and a smoother runs back, so every "
         "close is told by the closes on both sides of it. The picture is the consensus shape set at the smoothed centre "
         "and width: a close whose traders have pushed it off the path is priced back onto it, and the ranges that "
         "push left cheap are bought.",
         kalman_surface, factors=("direction", "volatility", "market"),
         maths="State x_j = (δ_j, λ_j): the lean in baseline σ and the log width ratio of close j's picture. Between closes j "
               "and j + 1 (sorted by horizon) the state is a random walk in log-horizon, x_j+1 = x_j + η, Var η = (0.3², 0.2²)·"
               "|ln h_j+1 − ln h_j|. Each close with formed ≥ 0.02 observes its carried curve's median and ln(half 10–90% "
               "band / f's), the subsidy floor taken off, with variance (0.5², 0.35²)/formed; f is a witness at every close "
               "with (median_f, 0) and variance (0.5², 0.35²). The Kalman filter runs nearest to farthest from the prior "
               "(median_f, 0) with variance (1, 0.5), and the Rauch–Tung–Striebel smoother back. The picture is f with its "
               "median moved to δ_k and its band scaled by e^λ_k: F(z) = F_f(m_f + (z − δ_k)·e^(−λ_k))."),
    _bot("smooth_particles", "Particle Bridge", "Monte Carlo paths of the price, weighted by every close they cross",
         "Monte Carlo on the surface. The bot simulates thousands of paths of the price from now through every close in "
         "view, each step's move drawn from the volatility clock's own history of standardised hourly moves. Then it "
         "weighs each path by how well it fits the market: at every formed close, the probability the site gives the "
         "range the path is passing through, tempered so the closes count as one and a half formed closes between them, "
         "since neighbouring curves are not independent witnesses. Paths that thread the curves survive; paths that "
         "miss them fade. Where the surviving paths land at the focused close is the picture, pooled with the zoo's "
         "consensus and kept to the surface's band. A particle filter across the horizon, with the price path as the "
         "hidden Markov process and the market's curves as its likelihoods.",
         particle_bridge, factors=("volatility", "tails", "randomness", "market"),
         maths="2,000 paths of the log price from now to each formed close in view, stepped from one close's time to the next: "
               "a step's shock is the sum of one draw from the clock's standardised residual pool per hour of the step (a "
               "day's worth at most), scaled to the baseline variance of the step (seeded by the model, the last candle and "
               "the close). A path's log weight is Σ_j τ_j·ln π̃_j(range the path is in at t_j) over the closes with "
               "formed ≥ 0.02, π̃_j the close's smoothed curve over the ranges, floor off, and τ_j = 1.5·ω_j/Σω. The paths are resampled "
               "by weight and read at the focused close through 199 quantiles with linear tails (the paths machinery), then "
               "pooled with f, ln c = (1 − β·w)·ln f + β·w·ln(bridge), and tempered to the surface pool's 10–90% band at "
               "strength w, as the Surface Pool is."),
    _bot("smooth_band", "Band Match", "The market says where and how wide; the model says what shape",
         "The premise on its own. The site shows each close as a band that holds 80% of the probability, and the bot "
         "takes the market at its word on that: pooled over the neighbouring closes so one ticket cannot move it, the "
         "band's two edges are what the market knows. Everything else, how the probability falls off inside the band and "
         "how far the tails reach outside it, comes from the zoo's consensus, moved and stretched so its own 80% band "
         "sits exactly on the market's. Spikes and dips inside the band do not reach the picture at all, except through "
         "the band's edges, so against this picture every one of them is a mispricing, and the bot buys the dips.",
         band_match,
         maths="b_f = (q10, q90) of f in z, b_π̄ the same of the surface pool π̄ with the floor taken off; the target band is "
               "b = (1 − w)·b_f + w·b_π̄ with w the surface's evidence. f is moved and stretched onto it: with c the centre "
               "and d the width of a band, F(z) = F_f(c_f + (z − c_b)·(d_f / d_b)), read at the range edges. The market's "
               "curve enters only through the two quantiles."),
]
