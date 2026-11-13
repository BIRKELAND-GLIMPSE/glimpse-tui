"""The hashprice bot: a picture of the market's oracle built from its parts, traded only at a discount.

Every other picture in the zoo starts from a walk on the series' own candles. A hashprice is not one: it is a trailing
24-hour figure with known blocks in it, and a retarget in it that the chain has half decided already. `glimpse_tui.hashprice`
simulates the price, the hash rate, the difficulty rule, the subsidy and the fees instead, and this module reads that
simulation at the market's ranges.
"""
from __future__ import annotations

import numpy as np

from ..api import HASHPRICE
from ..hashprice import Inputs, probabilities
from .core import Ctx, Model, ModelUnavailable, finish


def hashprice_parts(ctx: Ctx) -> np.ndarray:
    src = ctx.parts
    if not isinstance(src, Inputs):
        raise ModelUnavailable("it reads the hashprice's parts, and draws only the HASHPRICE series")
    if not src.ready:
        raise ModelUnavailable("Bitview's blocks are out of date")
    return finish(probabilities(src.samples(ctx.now, ctx.end), ctx.edges))


TRADING = {"objective": "edge", "interval_s": 5, "kelly": 0.15, "min_edge": 0.10, "fill_margin": 0.05, "exit_edge": 0.08, "markets": 10}

MODELS = [
    Model(
        "hashprice", "Hashprice From Its Parts", "Hashprice",
        "Bitcoin's price, the next retarget and the subsidy, simulated",
        "Hashprice is not a price with a life of its own: the oracle's number is the block reward, valued at each block's "
        "price, over the last day's blocks, divided by their difficulty. So this bot forecasts the parts. Blocks already in "
        "the window are known. The price is a random walk at Bitcoin's own volatility. The next retarget is largely decided "
        "by how fast this epoch has run, and Bitcoin's rule sets the rest. The subsidy is a schedule. A hashprice market "
        "that prices a walk from today's number misses all of that: it misses that tomorrow is mostly known, and it misses "
        "the step at the retarget. The bot buys what it then finds on sale, in the closes where the backtest says it knows "
        "more than a walk does, and sells back what the market overpays.",
        hashprice_parts, kind="forecast", factors=("direction", "volatility", "market"),
        reference="Bitview, hash_price (bitview.space/api); Bitcoin's retarget rule (Nakamoto 2008, Bitcoin Core "
                  "CalculateNextWorkRequired); Kelly (1956)",
        inputs="From Bitview: the last 2,600 blocks' coinbase in dollars and bitcoin, fees, difficulty and time, every visit; "
               "2,000 hours of its price and difficulty. From Coinbase: Bitcoin's quote now, which the simulation starts from. "
               "It stops drawing if Bitview has not answered for 15 minutes.",
        maths="HP(T) = K·Σ (subsidy(h_i) + fees_i)·price_i / Σ difficulty_i over the blocks of the 24 hours to T, "
              "K = 144·600·10¹⁵/2³². For each path, each hour: ln price moves by σ_t·m·z, z redrawn from Bitcoin's de-meaned "
              "standardised residuals, σ_t² = clock_t²·(e^(−t/τ) + (1 − e^(−t/τ))·long-run/now), m a per-path volatility "
              "multiplier; ln hash rate = ln H₀ + g·t + wander + e·(price change, lagged); blocks ~ Poisson(6·H/D); on "
              "reaching the retarget height D ← D·clip(1,209,600 / seconds the epoch took on this path, ¼, 4). "
              "`glimpse-tui hashprice fit` chooses τ, m, g, e and the rest on past closes; its choice is kept in "
              "~/.config/glimpse/hashprice/params.json.",
        trades="Fractional Kelly at 0.15 on ranges whose value, after both 2% fees, is at least 10% above the price, and never "
               "past the price at which a 5% discount would be gone. The 10 nearest closes only: on the 2026 closes held back from "
               "its fit, the forecast beats a well-sized walk clearly out to 10 days and less reliably beyond. An even share of the "
               "budget a close, one close every 5 seconds. Every close leans on one Bitcoin price and one retarget, so its "
               "positions rise and fall together: the budget is the risk. It takes "
               "profit on a range the market later prices 8% or more above the picture and above what it cost. These rules are "
               "its own: bots.toml does not change them.",
        pipeline="parts", trading=TRADING, only_series=HASHPRICE,
    ),
]
