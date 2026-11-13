"""The retarget bots: a simulation of the coming retargets from Bitcoin's own rule, traded only at a discount, and leaning against
the miners who buy cover.

A retarget market has no candles worth a walk: the number is flat for two weeks and then steps. `glimpse_tui.difficulty` simulates
the blocks still to come instead, and these two read that one simulation at each market's ladder, so the two series never disagree.
"""
from __future__ import annotations

import numpy as np

from ..api import ADJUSTMENT, DIFFICULTY
from ..difficulty import Inputs, probabilities
from .core import Ctx, Model, ModelUnavailable, finish


def _run(ctx: Ctx):
    src = ctx.parts
    if not isinstance(src, Inputs):
        raise ModelUnavailable("it reads the chain's blocks, and draws only its own retarget series")
    if not src.ready:
        raise ModelUnavailable("Bitview's blocks are out of date")
    if not ctx.height:
        raise ModelUnavailable("this market does not name its retarget height")
    return src.run(ctx.now)


def difficulty_rule(ctx: Ctx) -> np.ndarray:
    """The difficulty the market's retarget sets, in terahash."""
    return finish(probabilities(_run(ctx).difficulty_at(ctx.height) / 1e12, ctx.edges))


def adjustment_rule(ctx: Ctx) -> np.ndarray:
    """The change the market's retarget makes: percent edges read as factors, since the paths are positive and the ladder is not."""
    return finish(probabilities(_run(ctx).adjustment_at(ctx.height), 1.0 + ctx.edges / 100.0))


# On retargets the fit never saw, the forecast is clearly better than the site's out to the 3rd retarget on the difficulty, and
# only on the next one on the adjustment. Beyond those the bots do not trade. `hedger_edge`: no edge is enough for the hedgers' side.
TRADING = {"objective": "edge", "interval_s": 5, "kelly": 0.15, "min_edge": 0.10, "fill_margin": 0.05, "exit_edge": 0.08,
           "hedger_edge": float("inf")}
DIFFICULTY_TRADING = {**TRADING, "markets": 3}
ADJUSTMENT_TRADING = {**TRADING, "markets": 1}

_INPUTS = ("From Bitview: the last 4,000 blocks' times and difficulty, every visit, and the difficulty at every past retarget. Nothing "
           "else: no price, no season, because neither improved the forecast on retargets held back from its fit. It stops drawing "
           "if Bitview has not answered for 15 minutes.")
_MATHS = ("D′ = D·1,209,600 / (t_last − t_first), clipped to ¼ and 4, on each path. ln H(s) = ln H₀ + ε + g·s + W(s): H₀ the "
          "exponentially weighted hash rate carried forward by its own lag, ε its estimation error, g the difficulty's two-year growth "
          "with a per-path error, W a fat-tailed walk with a per-path scale. The time for m blocks is Gamma(m, 600·D/H). "
          "`glimpse-tui difficulty fit` "
          "chooses these on past "
          "retargets; its choice is kept in ~/.config/glimpse/difficulty/params.json.")
_TRADES = ("Fractional Kelly at 0.15 on ranges whose value, after both 2% fees, is at least 10% above the price, and never past the "
           "price at which a 5% discount would be gone: the forecast comes first, and nothing is bought at a loss by its own "
           "picture. Then it stays off the hedgers' side. Miners buy cover against a rising difficulty, so it never buys a range "
           "wholly above "
           "both the picture's median and today's level, however cheap: it bets on down and stable, and sells back to the book what "
           "the market overpays for. That does not make cover much cheaper: on LS-LMSR every purchase lifts the price of every range, so "
           "only the seed truly adds depth. It trades only the retargets where its forecast has shown a clear edge, and stops at the "
           "height the market stops trading. Every position leans on one hash rate, and the two series on one retarget, so these "
           "rise and fall together: the budget is the risk. These rules are its own: bots.toml does not change them.")

MODELS = [
    Model(
        "difficulty", "Difficulty From The Rule", "Retarget",
        "The difficulty each coming retarget sets, simulated from the chain",
        "The difficulty a retarget sets is decided by Bitcoin's own rule: the old difficulty times two weeks over the time the epoch "
        "took. So this bot simulates the blocks still to come at the hash rate the recent blocks show, and applies the rule on every "
        "path. Partway through an epoch most of the answer is already on the chain. It buys what the market sells under that picture, "
        "on the down and stable side only, the opposite side from miners hedging a rising difficulty.",
        difficulty_rule, kind="forecast", factors=("direction", "volatility", "market"),
        reference="Bitcoin Core CalculateNextWorkRequired; Bitview, difficulty (bitview.space/api); Kelly (1956)",
        inputs=_INPUTS, maths=_MATHS, trades=_TRADES, pipeline="rule", trading=DIFFICULTY_TRADING, only_series=DIFFICULTY,
    ),
    Model(
        "adjustment", "Adjustment From The Rule", "Retarget",
        "The percent change each coming retarget makes, simulated from the chain",
        "The same simulation as the difficulty bot, read as the percent change a retarget makes. On the next retarget the two markets "
        "ask one question on two ladders. Further out they differ: a far adjustment depends on how fast that one epoch runs, not on how "
        "far the difficulty has come by then, and a lucky epoch is largely taken back by the next. It trades the next retarget only, "
        "where its forecast has shown a clear edge, and like the difficulty bot stays off the side miners buying cover are on.",
        adjustment_rule, kind="forecast", factors=("direction", "volatility", "market"),
        reference="Bitcoin Core CalculateNextWorkRequired; Bitview, difficulty (bitview.space/api); Kelly (1956)",
        inputs=_INPUTS, maths=_MATHS, trades=_TRADES, pipeline="rule", trading=ADJUSTMENT_TRADING, only_series=ADJUSTMENT,
    ),
]
