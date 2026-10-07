"""Bots that look after the positions you already hold rather than opening new ones.

The Profit Taker values every position in your account with the zoo's consensus of how far the price moves, and sells
the part of any position the market pays more for than it is worth and than it cost. It never buys. The runner that
does this is `glimpse_tui.bots.ProfitTaker`; this module is only the picture it values your positions with.
"""
from __future__ import annotations

from .core import Model
from .opportunist import CONSENSUS_INPUTS, CONSENSUS_MATHS, REF, consensus

# Its own pace: it visits only closes you hold, so it can afford one a second, and re-reading a large portfolio takes
# seconds (45,912 positions took 3-9 s on 2026-10-07), so the account is read once a minute and its sales are booked
# locally in between.
TRADING = {"interval_s": 1, "portfolio_s": 60}

MODELS = [
    Model(
        "take_profit", "Profit Taker", "Portfolio", "Sells what the crowd overpays you for, at a profit",
        "It never buys. It watches every position your account holds, in every series, whoever opened it. When other "
        "traders pile into a range you own and bid it up past what it is worth, it sells into them: as many of your "
        "contracts as the market overpays for, at a profit over what you paid after both 2% fees, and it keeps the rest. "
        "What a range is worth comes from the zoo's consensus of how far the price moves, seven volatility and jump "
        "models pooled with equal weight, so it takes no view on direction: a range is overpriced when the crowd prices "
        "it above the odds a calibrated picture gives it.",
        consensus, kind="forecast", factors=("volatility", "tails", "market"), reference=REF,
        inputs=CONSENSUS_INPUTS, maths=CONSENSUS_MATHS,
        trades="Nothing: it only sells. On each close you hold, it sells the contracts whose next sale fetches, after the 2% "
               "exit fee, more than 1.02 × p × 98 sats and more than their average cost, and keeps the rest to the close.",
        pipeline="bins", sells_only=True, trading=TRADING,
    ),
]
