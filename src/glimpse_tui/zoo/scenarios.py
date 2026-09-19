"""Scenario views: fixed pictures somebody may want to bet on. None of them reads a signal.

Three kinds, as in the lab's `directional.py` and `zoo_views.py`:

- quantile views: the median follows a chosen historical quantile of Bitcoin's standardised forward return (0.8 for
  the bull case, 0.2 for the bear case), estimated from the bars at the lab's horizons and joined the way the lab
  joins them, with the spread scaled by the view's volatility multiplier;
- scripted paths: the median follows z(hours to the close), in baseline standard deviations;
- tail scenarios: the minimum relative-entropy tilt towards a deep out-of-the-money option's payoff.

The terminal holds about 2,000 bars where the lab holds a year, so a strong trend in the window can drag a long
horizon's quantile across zero. A view's drift is therefore kept on its own side of zero (a bull case never leans
down) as well as inside the lab's drift bounds.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from .core import Ctx, Model, cached, call, ewma_variance, hour_of_day_profile, payoff_picture, put, view_picture

HORIZONS = (1, 4, 12, 24, 72, 168)          # the lab's DEFAULT_HORIZONS
Z_BOUND = 1.5                               # the lab's bound on a drift, in sigma
MIN_EXTRA = 200                             # a horizon needs this many bars beyond its own length

QUANTILE_REF = "Historical conditional quantile view (filtered historical simulation, Barone-Adesi et al. 1999)"
PATH_REF = "Scripted scenario path on the filtered historical simulation baseline"
ENTROPY_REF = "minimum relative-entropy views (Meucci 2008, Risk, 'Fully Flexible Views')"


# ── quantile views ──────────────────────────────────────────

def _forward_returns(ctx: Ctx) -> dict[int, np.ndarray]:
    """Standardised h-hour forward returns per lab horizon: (logp[t+h] - logp[t]) / (sigma[t] * sqrt(h)), where
    sigma[t] is the EWMA volatility built from returns before bar t's close (the lab's `_inputs` and `refit`)."""
    logp = np.log(ctx.bars["close"].to_numpy(dtype=float))
    r = pd.Series(np.diff(logp), index=ctx.bars.index[1:])
    profile = hour_of_day_profile(r)
    s2, s2_next = ewma_variance(r.to_numpy() / profile[np.asarray(pd.DatetimeIndex(r.index).hour)])
    sigma = np.maximum(np.concatenate([[np.sqrt(s2[0])], np.sqrt(s2), [np.sqrt(s2_next)]])[: logp.size], 1e-6)
    out: dict[int, np.ndarray] = {}
    n = logp.size
    for h in HORIZONS:
        if n <= h + MIN_EXTRA:
            continue
        y = (logp[h:] - logp[: n - h]) / (sigma[: n - h] * np.sqrt(h))
        out[h] = y[np.isfinite(y)]
    return out


def _quantile_z(ctx: Ctx, model_id: str, quantile: float) -> float:
    """The view's drift at this close, in baseline sigmas. The lab joins the horizons in log-price units (straight
    lines from zero through z_h * sigma(h), constant z beyond the last), so the same is done here."""
    def build() -> tuple[np.ndarray, np.ndarray]:
        fwd = cached(ctx, "scenarios:forward", lambda: _forward_returns(ctx))
        hs = np.array([h for h in HORIZONS if h in fwd and fwd[h].size], dtype=float)
        return hs, np.array([np.quantile(fwd[int(h)], quantile) for h in hs])

    hs, z_h = cached(ctx, f"{model_id}:z", build)
    if hs.size == 0:
        return 0.0
    hours = ctx.hours
    if hours > hs[-1]:
        z = float(z_h[-1])
    else:
        sig = np.array([np.sqrt(ctx.scale.step_variances(ctx.now, ctx.now + h * 3600.0).sum()) for h in hs])
        z = float(np.interp(hours, np.concatenate([[0.0], hs]), np.concatenate([[0.0], z_h * sig]))) / ctx.sigma
    lo, hi = (0.0, Z_BOUND) if quantile >= 0.5 else (-Z_BOUND, 0.0)
    return float(np.clip(z, lo, hi)) if np.isfinite(z) else 0.0


def _quantile_view(model_id: str, quantile: float | None, vol_mult: float) -> Callable[[Ctx], np.ndarray]:
    def fn(ctx: Ctx) -> np.ndarray:
        return view_picture(ctx, 0.0 if quantile is None else _quantile_z(ctx, model_id, quantile), vol_mult)

    fn.__name__ = model_id
    return fn


# ── scripted paths ──────────────────────────────────────────

def v_rebound_z(hours: float) -> float:
    return float(np.where(hours <= 24, -0.8 * hours / 24, -0.8 + 1.1 * np.clip((hours - 24) / 144, 0, 1)))


def dead_cat_bounce_z(hours: float) -> float:
    return float(np.where(hours <= 24, 0.5 * hours / 24, 0.5 - 1.0 * np.clip((hours - 24) / 144, 0, 1)))


def view_v_rebound(ctx: Ctx) -> np.ndarray:
    return view_picture(ctx, v_rebound_z)


def view_dead_cat_bounce(ctx: Ctx) -> np.ndarray:
    return view_picture(ctx, dead_cat_bounce_z)


# ── tail scenarios ──────────────────────────────────────────

def view_crash(ctx: Ctx, conviction: float = 0.8) -> np.ndarray:
    return payoff_picture(ctx, lambda z, h: put(z, -2.0), conviction)


def view_melt_up(ctx: Ctx, conviction: float = 0.8) -> np.ndarray:
    return payoff_picture(ctx, lambda z, h: call(z, 2.0), conviction)


def view_binary_event(ctx: Ctx, conviction: float = 0.8) -> np.ndarray:
    return payoff_picture(ctx, lambda z, h: call(z, 1.5) + put(z, -1.5), conviction)


# ── the list ────────────────────────────────────────────────

FIXED_INPUTS = ("Nothing from the candles of its own: only the shared volatility clock σ, fitted once from the loaded closes, and "
                "the hours to the close, which together set the baseline density this view reshapes. It always draws once the "
                "400-bar minimum is met.")
PATH_INPUTS = ("Nothing from the candles of its own: only the shared volatility clock σ and the hours to the close, which picks "
               "the point of the script that applies to this market. It always draws once the 400-bar minimum is met.")
QUANTILE_INPUTS = ("Hourly closes over every loaded bar (about 2,000, where the lab uses a year): forward log returns at horizons "
                   "of 1, 4, 12, 24, 72 and 168 hours, each needing 200 bars beyond its own length (all six are present once "
                   "the 400-bar minimum is met), computed once per set of candles; then σ and the hours to the close.")


def _q_maths(quantile: float | None, vol_mult: float) -> str:
    width = f"The baseline density is shifted by mean_z and its width multiplied by vol_mult = {vol_mult}."
    if quantile is None:
        return f"mean_z = 0: the centre stays on spot. {width} Nothing is estimated; the view is entirely assumed."
    lo, hi, side = ("0", "+1.5", "up") if quantile >= 0.5 else ("−1.5", "0", "down")
    return (f"Forward returns y_h(t) = (ℓ_t+h − ℓ_t)/(v_t·√h), with ℓ = ln(close), h ∈ {{1, 4, 12, 24, 72, 168}} hours and v_t "
            "the deseasonalised EWMA hourly volatility (λ = 0.94, seeded by the mean of the first 100 squared returns, the "
            f"hour-of-day profile not put back) from returns completed before bar t. z_h is the {quantile} quantile of y_h. "
            "For a close h* hours out: if h* > 168, mean_z = z_168; otherwise the points (0, 0) and (h, z_h·σ_h), σ_h the "
            "baseline standard deviation of the log move over the next h hours, are joined by straight lines, read at h* and "
            f"divided by σ_h*. mean_z is clipped to [{lo}, {hi}]: the lab's ±1.5σ drift bound, plus the view's own side of "
            f"zero, because on 2,000 bars a trend can drag a long horizon's quantile across zero. {width} A quantile "
            f"{'above' if quantile >= 0.5 else 'below'} 0.5 leans the picture {side}.")


def _q(model_id: str, name: str, blurb: str, description: str, quantile: float | None, vol_mult: float, trades: str) -> Model:
    return Model(model_id, name, "Scenario", blurb, description, _quantile_view(model_id, quantile, vol_mult), kind="view",
                 factors=("direction", "volatility"), reference=QUANTILE_REF,
                 inputs=FIXED_INPUTS if quantile is None else QUANTILE_INPUTS, maths=_q_maths(quantile, vol_mult), trades=trades,
                 pipeline="view")


def _view(model_id: str, name: str, blurb: str, description: str, fn: Callable[[Ctx], np.ndarray], factors: tuple[str, ...],
          reference: str, *, inputs: str, maths: str, trades: str, pipeline: str = "view") -> Model:
    return Model(model_id, name, "Scenario", blurb, description, fn, kind="view", factors=factors, reference=reference,
                 inputs=inputs, maths=maths, trades=trades, pipeline=pipeline)


PAYOFF_FACTORS = ("shape", "direction", "tails")
TAIL_SOLVE = ("θ is solved at the close's own horizon (the lab solves it once at 24 hours and reuses it), by bisection on the "
              "expected rise with the effective-sample-size floor as the stop.")

MODELS = [
    _q("view_bull", "Bull Case", "A good week: the median rides the 80th percentile of past returns",
       "The bull case as a fixed view: the centre of the picture sits where Bitcoin's own history puts a good outcome, the "
       "80th percentile of its past standardised returns over the same horizon, with the usual spread around it. It is not "
       "a prediction that this will happen; it is what a good week has looked like, drawn from the data, for someone who "
       "wants to bet that this one is good.", 0.8, 1.0,
       trades="With the centre d baseline σ above spot (d between 0 and 1.5σ; on a driftless sample about 0.2σ to 0.7σ, "
              "larger at short horizons), every range above d/2 gets more weight than the baseline, the ratio growing with "
              "distance, so it buys the ranges from a few tenths of a σ above spot upward and most of all the far upside; "
              "every range below spot is thinned and never bought. Only the quantiles, not today's direction, set d."),
    _q("view_mild_bull", "Mild Bull", "Tilts up without calling a rally: the 65th percentile of history",
       "The mildly bullish case: the centre follows the 65th percentile of Bitcoin's past standardised returns at each "
       "horizon, with the usual spread. A picture for someone who thinks the week tilts up but is not calling a rally: the "
       "lean is a fraction of a standard deviation and the shape is unchanged.", 0.65, 1.0,
       trades="The centre sits a small distance d above spot (about 0.2σ to 0.3σ at short horizons on a driftless sample, "
              "possibly 0 at long ones), so the ratio to the baseline rises slowly with z: the ranges above roughly d/2 "
              "carry more weight, but the runner's 10% edge is only reached well above spot, so it buys upper ranges "
              "sparingly, thins everything below spot and buys nothing when d is 0."),
    _q("view_slow_grind_up", "Slow Grind Up", "The boring bull market: gentle, steady, no drama",
       "The boring bull market: a gentle upward drift, the 60th percentile of past standardised returns, and a spread 15 "
       "percent tighter than the baseline. Quiet, steady buying and no drama: the price edges up and does not wander far "
       "in either direction.", 0.6, 0.85,
       trades="It puts more weight than the baseline on a band about 0.9σ either side of a centre slightly above spot (a "
              "drift near 0.1σ to 0.2σ, or 0), and less on both tails beyond that band. The most it can exceed the baseline "
              "by is 1/0.85 ≈ 1.18 at the centre, barely past the runner's 10% edge, so it buys only the ranges nearest the "
              "centre and only when the market prices them close to the baseline."),
    _q("view_neutral", "Neutral Range", "No drift and a spread 15 percent tighter: a quiet week",
       "The neutral, range-bound view: no drift and a spread 15 percent tighter than the baseline, the picture of a quiet "
       "week in which the price stays close to where it is. A bet that the market's own volatility clock is running a "
       "little fast.", None, 0.85,
       trades="More weight than the baseline on the ranges within about 0.9σ of spot on both sides, less on both tails "
              "beyond that. The ratio peaks at 1/0.85 ≈ 1.18 at spot, so it buys only the central ranges and only when the "
              "market's prices there are near the baseline's; the tails are never bought."),
    _q("view_slow_bleed", "Slow Bleed", "No crash, just a market quietly losing interest",
       "The slow bleed: a gentle downward drift, the 40th percentile of past standardised returns, with a spread 15 percent "
       "tighter than the baseline. No capitulation, just a market losing interest hour by hour, drifting lower without a "
       "burst of volatility.", 0.4, 0.85,
       trades="More weight than the baseline on a band about 0.9σ either side of a centre a little below spot (a drift of "
              "roughly −0.2σ to −0.5σ on a driftless sample), less on both tails. The ratio never exceeds about 1.18, so it "
              "buys only the ranges just below spot and near the centre, and only when the market prices them near the "
              "baseline."),
    _q("view_mild_bear", "Mild Bear", "A soft week without a sell-off: the 35th percentile of history",
       "The mildly bearish case: the centre follows the 35th percentile of Bitcoin's past standardised returns at each "
       "horizon, with the usual spread. A picture for someone who expects a soft week without calling a sell-off: a lean "
       "of a fraction of a standard deviation, shape unchanged.", 0.35, 1.0,
       trades="With the centre d below spot (about −0.3σ to −0.7σ on a driftless sample), every range below d/2 carries "
              "more weight than the baseline, the ratio growing with distance, so it buys ranges from a few tenths of a σ "
              "below spot downward and most of all the far downside; everything above spot is thinned and never bought."),
    _q("view_bear", "Bear Case", "A bad week: the median rides the 20th percentile of past returns",
       "The bear case as a fixed view: the centre sits where Bitcoin's own history puts a bad outcome, the 20th percentile "
       "of its past standardised returns over the same horizon, with the usual spread around it. A hedge-minded picture "
       "of a bad week, drawn from the data rather than from a signal.", 0.2, 1.0,
       trades="With the centre d below spot (on a driftless sample −0.6σ to −1.1σ, never below −1.5σ), every range below d/2 gets more "
              "weight than the baseline, the ratio growing with distance, so it buys from roughly half a σ below spot "
              "downward and most of all the far downside; every range above spot is thinned and never bought."),
    _q("view_breakout_up", "Breakout Up", "Leaves the range upwards: 90th percentile drift, 25% wider",
       "An upside breakout: the centre follows the 90th percentile of past standardised returns and the spread is 25 "
       "percent wider than the baseline, the picture of a market about to leave its range to the upside with momentum "
       "and noise. Direction and volatility both bet on.", 0.9, 1.25,
       trades="With a drift d of roughly +0.4σ to +1.1σ on a driftless sample (never above +1.5σ) and a wider spread, it "
              "puts more weight than the baseline on every range from about 0.7σ above spot upward, most on the far upside, "
              "and buys those; the wider spread also lifts the far downside beyond about 2.5σ below spot a little when d is "
              "small. The middle and the near downside are thinned and not bought."),
    _q("view_breakout_down", "Breakdown", "Support gives way: 10th percentile drift, 25% wider",
       "A downside breakdown: the centre follows the 10th percentile of past standardised returns and the spread is 25 "
       "percent wider than the baseline, the picture of support giving way and volatility picking up on the way down. "
       "Direction and volatility both bet on.", 0.1, 1.25,
       trades="With a drift d of roughly −1.1σ to −1.4σ on a driftless sample (never below −1.5σ) and a wider spread, it "
              "puts more weight than the baseline on every range from about 0.7σ below spot downward, most on the far "
              "downside, and buys those; the far upside beyond about 2.5σ gains a little only when d is small. The middle "
              "and the near upside are thinned and not bought."),
    _q("view_high_vol", "High Volatility", "Something big is coming, direction unknown: 45% wider",
       "Something big is coming, direction unknown: no drift and a spread 45 percent wider than the baseline. The picture "
       "for a trader who expects a volatile week but will not guess which way it breaks: a pure bet that the market's "
       "volatility clock is running slow.", None, 1.45,
       trades="More weight than the baseline on both tails beyond roughly 1.2σ from spot, the ratio growing with "
              "distance, and less on the ranges within that band. It buys the outer ranges on both sides, the far ones "
              "with the largest edge, and never the central ranges. It never leans."),
    _view("view_v_rebound", "V-Shaped Rebound", "A flush lower inside a day, then a recovery through the week",
          "A V-shaped rebound: the centre slides 0.8 standard deviations lower over the next day, as if a flush of "
          "liquidations runs its course, then recovers through the week to finish 0.3 standard deviations above today's "
          "price. The picture for a buyer of the dip who expects the bottom inside a day. Every market is given the point "
          "of that script matching its own hours to the close.",
          view_v_rebound, ("direction", "volatility"), PATH_REF, inputs=PATH_INPUTS,
          maths="mean_z(h) = −0.8·h/24 for h ≤ 24 hours to the close, and −0.8 + 1.1·min((h − 24)/144, 1) beyond: the centre "
                "falls linearly to −0.8σ at 24 hours, rises linearly to +0.3σ at 168 hours, crossing zero at about 129 hours "
                "(5.4 days), and stays at +0.3σ for any later close. The shift is in each close's own baseline σ (σ grows "
                "with the horizon), not in a common weekly unit. vol_mult = 1, the default: the shape is the baseline's. "
                "Positive mean_z leans the picture up.",
          trades="For closes up to about 5.4 days out the centre is below spot, so ranges more than half the shift below "
                 "spot get more weight than the baseline and are bought, the far downside most, while ranges above spot are "
                 "thinned and not bought; the lean is strongest for closes about a day out (−0.8σ) and nearly nil for closes "
                 "an hour away (−0.03σ). For closes beyond 5.4 days the lean turns to at most +0.3σ up, a small extra weight "
                 "on ranges above spot."),
    _view("view_dead_cat_bounce", "Dead-Cat Bounce", "A relief rally that fails, then a drift lower all week",
          "A dead-cat bounce: the centre rises 0.5 standard deviations over the next day, a relief rally that fails, then "
          "drifts down through the week to finish 0.5 standard deviations below today's price. The picture for a seller "
          "who expects any bounce to be sold. Every market is given the point of that script matching its own hours to "
          "the close.",
          view_dead_cat_bounce, ("direction", "volatility"), PATH_REF, inputs=PATH_INPUTS,
          maths="mean_z(h) = 0.5·h/24 for h ≤ 24 hours to the close, and 0.5 − 1.0·min((h − 24)/144, 1) beyond: the centre "
                "rises linearly to +0.5σ at 24 hours, falls linearly through zero at 96 hours (4 days) to −0.5σ at 168 "
                "hours, and stays at −0.5σ for any later close. The shift is in each close's own baseline σ. vol_mult = 1, "
                "the default: the shape is the baseline's. Positive mean_z leans the picture up.",
          trades="For closes up to 4 days out the centre is above spot (most, +0.5σ, for closes a day out), so ranges more "
                 "than a quarter of a σ above spot get more weight than the baseline and are bought, the upper tail most, "
                 "while ranges below spot are thinned; for closes an hour away the lean is only +0.02σ and it buys nothing. "
                 "For closes beyond 4 days the lean is down, reaching −0.5σ at a week: ranges below spot are bought, ranges "
                 "above are not."),
    _view("view_crash", "Crash", "A much bigger chance of a fall beyond two standard deviations",
          "The crash scenario: a much bigger chance than usual that Bitcoin falls more than two standard deviations by the "
          "close, with the rest of the picture left in the baseline's proportions. It is what owning deep out-of-the-money "
          "puts is a bet on: nothing about the middle, everything about the left tail.",
          view_crash, PAYOFF_FACTORS, f"Tail scenario as a deep out-of-the-money put; {ENTROPY_REF}", inputs=FIXED_INPUTS,
          maths="Payoff f(z) = max(−2 − z, 0), a put struck 2σ below spot, passed to payoff_picture with conviction 0.8 and "
                "min_ess 0.25 (the default floor). Because f is zero above the strike, every range above −2σ is multiplied "
                "by one common factor a little below 1 and keeps the baseline's shape; below −2σ the weight grows as "
                "exp(θ·(−2 − z)). At conviction 0.8 the 25% effective-sample floor usually binds before the target rise, "
                "leaving roughly two to three times the baseline's probability beyond −2σ. " + TAIL_SOLVE
                + " A positive θ leans the picture down.",
          trades="It buys only ranges more than about 2σ below spot, with an edge that grows the further down the range "
                 "sits (many times the baseline in the far tail); every range above −2σ carries a single factor of about "
                 "0.95 to 0.98 and is never bought. The lean is the same for every horizon in σ terms."),
    _view("view_melt_up", "Melt-Up", "A much bigger chance of a rise beyond two standard deviations",
          "The melt-up scenario: a much bigger chance than usual that Bitcoin rises more than two standard deviations by "
          "the close, a short squeeze or a buying frenzy, with the rest of the picture left in the baseline's proportions. "
          "It is what owning deep out-of-the-money calls is a bet on.",
          view_melt_up, PAYOFF_FACTORS, f"Tail scenario as a deep out-of-the-money call; {ENTROPY_REF}", inputs=FIXED_INPUTS,
          maths="Payoff f(z) = max(z − 2, 0), a call struck 2σ above spot, passed to payoff_picture with conviction 0.8 and "
                "min_ess 0.25 (the default floor). Because f is zero below the strike, every range below +2σ is multiplied "
                "by one common factor a little below 1 and keeps the baseline's shape; above +2σ the weight grows as "
                "exp(θ·(z − 2)). At conviction 0.8 the 25% effective-sample floor usually binds before the target rise, "
                "leaving roughly two to three times the baseline's probability beyond +2σ. " + TAIL_SOLVE
                + " A positive θ leans the picture up.",
          trades="It buys only ranges more than about 2σ above spot, with an edge that grows the further up the range sits "
                 "(many times the baseline in the far tail); every range below +2σ carries a single factor of about 0.95 "
                 "to 0.98 and is never bought. The lean is the same for every horizon in σ terms."),
    _view("view_binary_event", "Binary Event", "A verdict sends it sharply one way: thin middle, fat tails",
          "A binary event: a verdict, an approval or a policy surprise that sends the price sharply one way or the other, "
          "so the middle of the picture thins out and both tails fatten by the same rule. It is what a wide long strangle "
          "is a bet on: a large move, direction unknown.",
          view_binary_event, PAYOFF_FACTORS, f"Wide long strangle; {ENTROPY_REF}", inputs=FIXED_INPUTS,
          maths="Payoff f(z) = max(z − 1.5, 0) + max(−1.5 − z, 0), a strangle with strikes 1.5σ either side of spot, passed "
                "to payoff_picture with conviction 0.8 and min_ess 0.25 (the default floor). Every range within ±1.5σ is "
                "multiplied by one common factor below 1 and keeps the baseline's shape; beyond either strike the weight "
                "grows as exp(θ·(|z| − 1.5)), symmetrically. At conviction 0.8 the 25% effective-sample floor often binds, "
                "leaving roughly one and a half to two times the baseline's probability beyond ±1.5σ. " + TAIL_SOLVE
                + " The picture does not lean.",
          trades="It buys ranges more than about 1.7σ from spot on either side, the edge growing with distance and equal "
                 "on both sides; every range within ±1.5σ carries a single factor of about 0.85 to 0.9 and is never bought."),
]
