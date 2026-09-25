"""Opportunistic bots: honest pictures, traded for bargains.

A bargain is a range the market sells for much less than it is likely to be worth. Where the imbalance comes from
does not matter (a crowd leaning one way, the subsidy's floor spread thin, nobody having traded a corner of the
ladder yet); what matters is that the price is low, the picture says the range is worth several times that price
after both fees, and the bet is small. Many small bets with positive expected value, held across many closes, is
the whole strategy: the law of large numbers turns an average edge into a return.

The picture a bargain hunter trusts has to be calibrated in the places it bets, which for cheap ranges means the
shoulders and the tails. So these bots do not invent a view. They pool the zoo's volatility and jump models:

    consensus     the equal-weight mixture of seven models of how far Bitcoin moves (bootstrap, Student-t, GJR-GARCH,
                  HAR, Merton jumps, Kou jumps, stochastic volatility): no direction, carefully sized tails
    jumps         the equal-weight mixture of the four built to price sudden moves (Merton, Kou, Hawkes,
                  stochastic volatility): the fattest honest tails in the zoo
    signals       half consensus, half the zoo's multi-signal combination: a lean, but only half a lean

What makes one bot bullish, bearish or neutral is not the picture but where its `Policy` lets it buy: above spot,
below it, near it, or out in the tails. Every rule is in `glimpse_tui.policy`.
"""
from __future__ import annotations

import numpy as np

from ..policy import Policy
from .core import Ctx, Model, ModelUnavailable, cached, finish

CONSENSUS = ("rw_bootstrap", "rw_student_t", "garch_gjr_skewt", "har_rv", "dist_merton_jumps", "kou_jump_paths",
             "stochastic_vol_paths")
JUMPS = ("dist_merton_jumps", "kou_jump_paths", "hawkes_jump_paths", "stochastic_vol_paths")
SIGNALS = "alpha_combo"
REF = ("Bates and Granger (1969), 'The combination of forecasts'; Kelly (1956) and Thorp (2006) on betting with an "
       "edge; Benter (1994), 'Computer based horse race handicapping and wagering systems'")


def mixture(ctx: Ctx, ids: tuple[str, ...]) -> np.ndarray:
    """The equal-weight average of the members' bin probabilities. A member that cannot draw is left out; a mixture
    with no member raises. Computed once per close and shared by every bot that pools the same members."""
    def build() -> np.ndarray:
        from . import get

        got = []
        for mid in ids:
            m = get(mid)
            try:
                got.append(np.asarray(m.fn(ctx), dtype=float))
            except ModelUnavailable:
                continue
        if not got:
            raise ModelUnavailable("no member of the mixture could draw")
        return finish(np.mean(got, axis=0))

    key = f"opp:{'+'.join(ids)}:{ctx.end}:{ctx.now}:{ctx.spot}:{ctx.edges[0]}:{ctx.edges[-1]}:{len(ctx.edges)}"
    return cached(ctx, key, build)


def consensus(ctx: Ctx) -> np.ndarray:
    return mixture(ctx, CONSENSUS)


def jumps(ctx: Ctx) -> np.ndarray:
    return mixture(ctx, JUMPS)


def signals(ctx: Ctx) -> np.ndarray:
    return finish(0.5 * consensus(ctx) + 0.5 * mixture(ctx, (SIGNALS,)))


# ── the texts ───────────────────────────────────────────────

CONSENSUS_INPUTS = ("Everything its seven members read, which is hourly closes through the shared volatility clock plus each "
                    "member's own fit: the replayed residual pool (bootstrap), the fitted degrees of freedom (Student-t), a "
                    "GJR-GARCH(1,1) fit, the HAR regression of realised variance on its daily, weekly and monthly averages, "
                    "the jump fits of Merton and Kou, and the stochastic-volatility fit. Fits are made once per set of "
                    "candles and shared with the member bots. It draws once the 400-bar minimum is met.")
JUMPS_INPUTS = ("Everything its four members read: hourly closes through the shared volatility clock, the jump intensity and "
                "jump-size fits of Merton and Kou, the self-exciting jump fit of Hawkes, and the stochastic-volatility fit. "
                "Fits are made once per set of candles. It draws once the 400-bar minimum is met.")
SIGNALS_INPUTS = ("Everything the seven consensus members read (hourly closes through the shared volatility clock plus each "
                  "member's own fit), and everything the zoo's Alpha Combo reads: its trend, reversion and volume signals, each "
                  "ranked against its own history. Fits are made once per set of candles. It draws once the 400-bar minimum "
                  "is met.")
CONSENSUS_MATHS = ("p_i = (1/7)·Σ_m p_i^(m), the plain average of the seven members' probabilities for range i, renormalised with "
                   "every range kept at 10⁻⁹ or more. An equal-weight pool (Bates and Granger) rather than a fitted one: on a "
                   "few thousand bars fitted weights chase noise, and a pool is never worse calibrated than its worst member "
                   "in the tails, where bargains live. No member leans, so the centre stays on spot; the pool's tails are an "
                   "average of seven ways of sizing them.")
JUMPS_MATHS = ("p_i = (1/4)·Σ_m p_i^(m), the plain average of the four jump and stochastic-volatility members' probabilities "
               "for range i, renormalised with every range kept at 10⁻⁹ or more. Each member puts more probability than the "
               "Student-t baseline beyond about 2.5σ, by jumps (Merton, Kou), by jumps that breed jumps (Hawkes) or by paths "
               "whose volatility wanders (stochastic volatility), so the pool is the fattest-tailed honest picture the zoo "
               "draws. No member leans on purpose, so the centre stays near spot.")
SIGNALS_MATHS = ("p_i = ½·c_i + ½·a_i, with c the seven-member consensus (the equal-weight average of bootstrap, Student-t, "
                 "GJR-GARCH, HAR, Merton, Kou and stochastic volatility) and a the Alpha Combo picture, which leans with the "
                 "combined trend, reversion and volume reading. Halving the lean keeps the tails honest while letting the "
                 "signals decide which side's bargains are real, renormalised with every range kept at 10⁻⁹ or more.")


def _bot(model_id: str, name: str, blurb: str, description: str, fn, policy: Policy, *, inputs: str, maths: str,
         trades: str, factors: tuple[str, ...]) -> Model:
    return Model(model_id, name, "Opportunistic", blurb, description, fn, kind="forecast", factors=factors,
                 reference=REF, inputs=inputs, maths=maths, trades=trades, pipeline="bins", policy=policy)


MODELS = [
    _bot("opp_bargain_near", "Bargain Hunter · Near Spot", "Cheap ranges where the close will most likely land",
         "The neutral bargain hunter. Bitcoin spends most hours going nowhere much, so the ranges around the spot price are "
         "where the close usually lands, and the market sometimes sells them cheap while its traders chase the wings. It "
         "buys those, a little each, from the zoo's consensus of how far Bitcoin moves, and sells them back if the market "
         "later pays more than they are worth. It profits when the price trades sideways.",
         consensus, Policy(max_price=8.0, min_ratio=1.3, region="near", stake=0.01, market_cap=0.10, max_ranges=20,
                           stance="sideways"),
         inputs=CONSENSUS_INPUTS, maths=CONSENSUS_MATHS, factors=("volatility", "tails", "market"),
         trades="Only the middle half of its own picture, where the close is likeliest, at 8 sats a contract or less, when the "
                "consensus says the range is worth at least 1.3 times what it costs after fees. With $200 ranges near spot "
                "worth a few percent each, these are the cheapest likely outcomes on the ladder; nothing is bought when the "
                "market prices the middle as the consensus does."),
    _bot("opp_bargain_up", "Bargain Hunter · Upside", "Cheap ranges above spot, bought only when they are real value",
         "The bullish bargain hunter. It holds no opinion that Bitcoin will rise; it holds a calibrated picture of how far "
         "Bitcoin moves, and buys only ranges above the spot price that the market sells for much less than that picture "
         "says they are worth. It profits when Bitcoin goes up, and it only pays for the upside when the upside is on sale.",
         consensus, Policy(max_price=5.0, min_ratio=1.4, region="above", stake=0.0075, market_cap=0.08, max_ranges=30,
                           stance="bullish"),
         inputs=CONSENSUS_INPUTS, maths=CONSENSUS_MATHS, factors=("direction", "tails", "market"),
         trades="Ranges wholly above spot at 5 sats a contract or less (a win pays about 19 times), when the consensus says "
                "the range is worth at least 1.4 times its fee-inclusive price. Typically the first few σ above spot when the "
                "market leans bearish, or its thin upper shoulder when traders crowd the centre."),
    _bot("opp_bargain_down", "Bargain Hunter · Downside", "Cheap ranges below spot: a bearish hedge bought on sale",
         "The bearish bargain hunter, and a hedge. It buys only ranges below the spot price that the market sells for much "
         "less than the zoo's consensus says they are worth. Held beside a long Bitcoin position it pays out when the "
         "price falls, and because it only buys the downside on sale, the hedge costs less than it is expected to return.",
         consensus, Policy(max_price=5.0, min_ratio=1.4, region="below", stake=0.0075, market_cap=0.08, max_ranges=30,
                           stance="bearish"),
         inputs=CONSENSUS_INPUTS, maths=CONSENSUS_MATHS, factors=("direction", "tails", "market"),
         trades="Ranges wholly below spot at 5 sats a contract or less, when the consensus says the range is worth at least "
                "1.4 times its fee-inclusive price. Typically the first few σ below spot when the market leans bullish, or "
                "its thin lower shoulder when traders crowd the centre."),
    _bot("opp_discount_sweep", "Discount Sweep", "Every range on sale, anywhere on the ladder, a little each",
         "Buys everything on the ladder the market sells at a discount to the zoo's consensus: near spot or far from it, "
         "above or below. Each purchase is a small, fixed share of the budget, so no single range matters and the edge is "
         "collected over dozens of ranges and many closes. It sells a range back whenever the market pays more for it than "
         "it is worth: buy low, sell high.",
         consensus, Policy(max_price=10.0, min_ratio=1.25, region="any", stake=0.005, market_cap=0.12, max_ranges=60,
                           stance="neutral"),
         inputs=CONSENSUS_INPUTS, maths=CONSENSUS_MATHS, factors=("volatility", "tails", "market"),
         trades="Any range at 10 sats a contract or less that the consensus values at 1.25 times its fee-inclusive price or "
                "more, best odds first, up to 60 ranges a close. The widest net of the opportunistic bots and the smallest "
                "stake per range."),
    _bot("opp_signal_bargains", "Signal Bargains", "Bargains on the side the signals point to",
         "A bargain hunter with half an opinion. Its picture is half the zoo's consensus and half the Alpha Combo, which "
         "leans with the combined trend, reversion and volume signals, so the ranges on the side the signals favour are "
         "worth a little more to it and the other side a little less. It buys whichever ranges are then on sale, and sells "
         "them back when the market overpays.",
         signals, Policy(max_price=6.0, min_ratio=1.4, region="any", stake=0.0075, market_cap=0.10, max_ranges=40),
         inputs=SIGNALS_INPUTS, maths=SIGNALS_MATHS, factors=("direction", "volatility", "market"),
         trades="Any range at 6 sats a contract or less that the half-and-half picture values at 1.4 times its fee-inclusive "
                "price or more. When the signals are silent it trades like the Discount Sweep with a stricter ratio; when "
                "they lean, most of what it finds sits on their side of spot."),
    _bot("opp_longshot", "Longshot Collector", "Risk a sat to win a hundred, far out in both tails",
         "Risk small, win big. It buys only ranges the market sells for a sat or less, far out in the tails of its own "
         "picture, where a win pays a hundred times the stake or more, and only when the zoo's jump models say the range is "
         "worth at least twice that. Almost every ticket loses. It holds every one to the close anyway, because the few "
         "that land are meant to pay for all the rest and then some.",
         jumps, Policy(max_price=1.0, min_ratio=2.0, region="tails", stake=0.0025, market_cap=0.05, max_ranges=40,
                       hold=True, stance="volatile"),
         inputs=JUMPS_INPUTS, maths=JUMPS_MATHS, factors=("tails", "randomness", "market"),
         trades="Ranges outside the middle 80% of its picture at 1 sat a contract or less (a win pays about 96 times), when the "
                "jump pool says the range is worth at least twice its fee-inclusive price. A quarter of a percent of the "
                "budget per range, no more than 5% in one close, held to settlement."),
    _bot("opp_crash_insurance", "Crash Insurance", "Cheap tickets on a crash, held to the close",
         "Bearish tail insurance. It buys cheap ranges far below the spot price, past the 10th percentile of the zoo's "
         "jump models, only when those models say the crash is priced at two thirds or less of its odds, and holds them to the "
         "close. Beside a long Bitcoin position it pays out in the hours that hurt most, and it only buys the insurance "
         "when the premium is lower than the risk.",
         jumps, Policy(max_price=2.0, min_ratio=1.5, region="down-tail", stake=0.005, market_cap=0.05, max_ranges=30,
                       hold=True, stance="bearish"),
         inputs=JUMPS_INPUTS, maths=JUMPS_MATHS, factors=("tails", "direction", "market"),
         trades="Ranges below spot and below the 10th percentile of its picture, at 2 sats a contract or less (a win pays "
                "about 48 times), when the jump pool values them at 1.5 times their fee-inclusive price or more. Held to "
                "settlement: insurance is not sold because the fire looks less likely."),
    _bot("opp_moon_tickets", "Moon Tickets", "Cheap tickets on a squeeze, held to the close",
         "The mirror of Crash Insurance: cheap ranges far above the spot price, past the 90th percentile of the zoo's jump "
         "models, bought only when those models say a squeeze is priced at two thirds or less of its odds, and held to the "
         "close. It profits from the sudden rallies Bitcoin is known for, paying little for the chance.",
         jumps, Policy(max_price=2.0, min_ratio=1.5, region="up-tail", stake=0.005, market_cap=0.05, max_ranges=30,
                       hold=True, stance="bullish"),
         inputs=JUMPS_INPUTS, maths=JUMPS_MATHS, factors=("tails", "direction", "market"),
         trades="Ranges above spot and above the 90th percentile of its picture, at 2 sats a contract or less, when the jump "
                "pool values them at 1.5 times their fee-inclusive price or more. Held to settlement."),
]
