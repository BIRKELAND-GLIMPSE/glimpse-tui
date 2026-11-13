"""The Arbitrageurs: both sides of one close bought in two series for less than they pay, whichever way the price goes.

The runner that does this is `glimpse_tui.arb.Arbitrageur`, and the covering maths is in that module too. These entries
are how the bots appear in the zoo: each one's account of itself, and the picture it draws on the bots screen, which is
the zoo's consensus of how far the asset moves (Benter's fundamental, cached per close). The bots do not trade that
picture. They use it for one thing: to know how much probability a band of the price line leaves outside, so a cover
that is not of the whole line is taken only where the consensus says the stake is almost never lost.

Each entry names its scheme (`arb.SCHEMES`): which series it reads and which pairs of coinciding closes it covers.
`triangle` is the Triangulator (`glimpse_tui.tri`): one forecast of Bitcoin and gold, its likely band bought across the
three books that price it; `triangle_oil` the same for Bitcoin and WTI/BTC through a model of oil. `arbitrage` works
Bitcoin's three ladders; `arbitrage_gold` works gold's three (gold in dollars hourly and daily, and Tether Gold in
satoshis an ounce) against each other and against Bitcoin
and WTI/BTC.
"""
from __future__ import annotations

from .benter import fundamental
from .core import Model
from .opportunist import CONSENSUS_INPUTS, REF

TRADING = {"interval_s": 5, "min_edge": 0.05, "objective": "edge"}
REFERENCE = ("Hanson (2003), 'Combinatorial information market design'; Othman, Sandholm, Pennock and Reeves (2013), "
             "'A practical liquidity-sensitive automated market maker'; " + REF)
MATHS_COMMON = (
    "A cover is a set S_A of A ranges and S_B of B ranges such that every x in the band [L, U] either lies in S_A or has every "
    "B range its image can reach inside S_B; its cost at the margin is Σ_{S_A} p_A + Σ_{S_B} p_B. The cheapest is found by a "
    "dynamic programme along A's ranges with state M, the highest B range bought so far (reaches are monotone, so the bought B "
    "ranges below M that the next cell needs are already held): cell j either pays p_A[j] and keeps M, or pays Σ p_B over "
    "(max(k1_j, M + 1) .. k2_j) and moves M to k2_j. The cover is then priced at size d with the cost function, "
    "C(q + d·1_S) − C(q) on each book plus 2% (at least 1 sat), and d is the largest for which 98·d ≥ (1 + min_edge) × cost, "
    "found by doubling and bisection. Bands: the whole line either ladder can cover for certain, then the consensus's narrowest "
    "band holding all but 10⁻⁵, 10⁻⁴ and 10⁻³ of its mass, each widened to hold both books' own bands at that level; the widest "
    "whose cover returns min_edge at the margin is taken."
)

TRI_TRADING = {"interval_s": 5, "objective": "edge", "kelly": 0.25, "min_edge": 0.25, "exit_edge": 0.06, "edge_close_share": 0.03}
TRI_REFERENCE = ("Sklar (1959), the copula theorem; Genest and Zidek (1986), 'Combining probability distributions'; Kelly (1956), "
                 "'A new interpretation of information rate'; " + REFERENCE)
TRI_INPUTS_LENS = ("And whatever the lens reads: the Session Walk by default reads each series' hourly closes and the asset's "
                   "calendar (CME's week for gold and oil, every hour for Bitcoin); a smoothing or Benter lens reads the books too.")
TRI_MATHS_BET = (
    "The bet on a leg: with c the pooled picture, the band [lo, hi] is the narrowest run of ranges with Σ c ≥ band_mass (0.8), "
    "p = Σ_{lo..hi} c_i its chance, and a set is one contract on each range of it, paying 98 sats wherever inside it the close "
    "lands. Its marginal price after d sets is s(d) = Σ_{lo..hi} price_i(q + d·1_band) and its cost is C(q + d·1_band) − C(q) "
    "plus 2%. The stake is fractional Kelly on the band as one bet, f = (b·p − (1 − p))/b with b = 98/(1.02·s(0)) − 1, times "
    "kelly × the budget, capped by the close's share of the budget less what is held there and by the instant's room; d is the "
    "largest number of sets within the stake with 98·p ≥ 1.25 × 1.02 × s(d), found by bisection and floored to 0.01. "
    "Calibration, 31 October 2026, rolling fits over the last 900 hours: the walk's 80% band held the close 73 to 89% of the "
    "time across the four series and horizons of 1 to 48 hours, so min_edge at 25% leaves room for the misses."
)


def _tri_trades(cross: str, base: str, quote: str) -> str:
    return (
        "All legs of one instant in one request. First a cover (the Arbitrageurs' risk-free set of ranges across two of the books) "
        "when one pair offers it at 5% on cost. Then on every leg with a market: a position the market pays 6% more for than the "
        "triangle says it is worth, and more than it cost, is sold down; and the triangle's 80% band is bought as a set, the same "
        "contracts on every range of it, in every book that sells the set for less than it pays, filled until the last set returns "
        "25% on cost after both fees, at most 3% of the budget in one close and 10% in one instant, never beyond 48 hours out. "
        "Measured on 31 October 2026: within a few hours of a close the books are already sharper than the walk and a set of the "
        "band costs more than 98 × its chance, so it buys nothing there; a day or two out the books still sit near their flat seed "
        "and sell the band for 27 to 36 sats a set against 78 it pays in expectation, and gold's weekend closes for less, where the "
        "ladders are priced for trading hours that will not happen. Two thousand sats move those books, so the stakes stay small "
        "and spread over many closes; what it holds is marked at what the thin book would pay to take it back, which is far below "
        "what the set is worth to hold, so the account's value dips until the closes settle. It says nothing when every band costs "
        "within 25% of what it pays, which near a close is the rule."
    )


MODELS = [
    Model(
        "triangle", "Triangulator", "Arbitrage", "One forecast of Bitcoin and gold, bet across three books at once",
        "Three hourly series close at the same second and are bound by one identity: an ounce of Tether Gold in satoshis "
        "(XAUTBTC) is an ounce of gold in dollars (XAU) times 10⁸ over Bitcoin in dollars (BTC), give or take the small basis "
        "between the two gold tokens that the three spots show. So one forecast of Bitcoin and gold is a forecast of all three "
        "books, and the three crowds, different sizes moving at different speeds, rarely price it the same. The bot takes a "
        "lens, the Session Walk by default (a Student-t random walk whose σ adds up only the hours the asset's market trades, so "
        "gold's weekend closes come out as still as they are) or any baseline, smoothing or Benter model (m changes it on the "
        "advanced screen), draws its picture of each of the three closes, and for each leg pools that picture with the one the "
        "other two legs imply through the identity. The three pooled pictures are the triangle: one forecast of the instant. Then "
        "it bets on the likely outcomes. On each leg the band is the narrowest run of ranges holding 80% of the triangle's "
        "picture, the bet is the same number of contracts on every range of it (it pays 98 sats a set wherever inside the band the "
        "close lands, nothing outside), and the bot buys that set in whichever of the three books sells it for less than it pays, "
        "sized by fractional Kelly and filled only while the last set still returns a quarter over its cost, so it never buys a "
        "thin book up to its own fair value. A risk-free cover across two books comes first when there is one, and profit is "
        "taken wherever a book comes to pay more than the triangle says a position is worth. Watch it on the advanced screen (A): "
        "the three ladders side by side, the triangle over the market on each, the band, and every buy as it lands.",
        fundamental, kind="forecast", factors=("market", "volatility", "timing", "tails"), reference=TRI_REFERENCE,
        inputs="The three books of the closes at one instant, read fresh on every visit and priced with the LS-LMSR exactly as "
               "the server prices them. The three spots (Bitcoin and PAX Gold from Coinbase, Tether Gold in sats from Glimpse's "
               "own oracle), whose ratio is the basis between the gold tokens. The three series' hourly candles, from which it "
               "reads the correlation of each pair of legs' hourly log returns over their last 1,000 shared hours: Bitcoin against "
               "gold in dollars (small), Bitcoin against gold in sats (strongly negative), gold in dollars against gold in sats. "
               + TRI_INPUTS_LENS,
        maths="Let B, Q and R be the closes in dollars, dollars and satoshis, with R = c·Q/B and c = 10⁸ × basis. For each leg "
              "the other two legs' pictures p_a and p_b are joined by a Gaussian copula: z ~ N(0, [[1, ρ], [ρ, 1]]) with ρ the "
              "measured correlation of the two legs' hourly returns, u = Φ(z), and each u is pushed through its picture's "
              "inverse distribution function (log-uniform inside a range) to a price; the identity gives the leg's price, "
              "which is binned on its ladder (what lands past the ladder goes in its end ranges), smoothed by one range and "
              "floored at 10⁻⁹. That is the implied picture m. The leg's own picture o (the lens's) and m are pooled in log "
              "space, c_i ∝ o_i^(1/(1+λ)) · m_i^(λ/(1+λ)), with λ = cross_weight × (e_other + ε) / (e_own + e_other + 2ε), "
              "e_own the leg's formedness (1 + Σ p ln p / ln n on the book's raw curve, 0 flat), e_other the smaller of the "
              "other two books', ε = 0.02. The triangle gap is the sats book's median over the one the two dollar books' "
              "medians imply, − 1, read on the market's own curves, on the lens's pictures and on the pooled ones. "
              + TRI_MATHS_BET + " A cover across two of the books, when taken, is the Arbitrageurs' dynamic programme on the "
              "pooled picture: " + MATHS_COMMON,
        trades=_tri_trades("XAUTBTC", "BTC", "XAU"),
        pipeline="bins", arbitrage="triangle", trading=TRI_TRADING,
    ),
    Model(
        "triangle_oil", "Oil Triangulator", "Arbitrage", "One forecast of Bitcoin and oil, bet on WTI/BTC and Bitcoin",
        "Two series close at the same second and are bound by one identity: a barrel of WTI in satoshis (WTIBTC) is WTI in "
        "dollars × 10⁸ over Bitcoin in dollars. Glimpse has no WTI-in-dollars market, so the third leg of this triangle is a "
        "model: oil's dollar price is read from the two feeds that carry it (WTI/BTC in sats × Bitcoin in dollars), its hourly "
        "move is measured separately for the hours CME trades and the hours it does not, and the leg's picture at each close is "
        "a Student-t whose variance adds up the hours between, each at its own σ. At weekends oil stands still, so WTI/BTC is "
        "Bitcoin alone and the link between the two books is tightest. The bot takes a lens, the Session Walk by default or any "
        "baseline, smoothing or Benter model, draws its picture of the Bitcoin close and of the WTI/BTC close, pools each with "
        "the picture the other and the oil model imply, and bets on the likely outcomes: on each leg the band holding 80% of the "
        "triangle's picture, bought as a set of equal contracts in whichever book sells it for less than it pays, filled only "
        "while the last set still returns a quarter over its cost. A risk-free cover across the two books comes first when "
        "there is one, and profit is taken wherever a book overpays. Watch it on the advanced screen (A): Bitcoin's ladder, the "
        "oil model, and WTI/BTC side by side.",
        fundamental, kind="forecast", factors=("market", "volatility", "timing", "tails"), reference=TRI_REFERENCE,
        inputs="The two books of the closes at one instant, read fresh on every visit and priced with the LS-LMSR exactly as "
               "the server prices them. The spots of Bitcoin (Coinbase) and of WTI/BTC (Glimpse's own oracle), whose product is "
               "oil's dollar price. The two series' hourly candles, from which oil's dollar series is built bar by bar and its σ "
               "read: on 31 October 2026, 0.52% an hour while CME trades and 0.20% an hour when it does not, and the correlation "
               "of oil with Bitcoin and of WTI/BTC with Bitcoin over their last 1,000 shared hours. " + TRI_INPUTS_LENS,
        maths="Let B and R be the closes in dollars and satoshis and Q oil's dollar price, with R = 10⁸·Q/B. Q has no book: "
              "its picture is a Student-t of ln Q around today's price with variance Σ over the hours to the close of σ_open² or "
              "σ_closed² by CME's calendar (Sunday 23:00 to Friday 22:00 UTC, the 22:00 hour off each day), ν fitted to the open "
              "hours' standardised returns. For each market leg the other two legs are joined by a Gaussian copula (ρ the "
              "measured correlation of their hourly returns): u = Φ(z), each u pushed through its picture's inverse distribution "
              "(the ladder's, log-uniform inside a range, or the Student-t's), the identity gives the leg's price, binned on its "
              "ladder, smoothed by one range, floored at 10⁻⁹. That implied picture m and the lens's own o are pooled in log space, "
              "c_i ∝ o_i^(1/(1+λ)) · m_i^(λ/(1+λ)), λ = cross_weight × (e_other + ε) / (e_own + e_other + 2ε), e the books' "
              "formedness (the model counts as fully formed, so Bitcoin's book sets how far WTI/BTC is pulled). "
              + TRI_MATHS_BET + " A cover across the two books, when taken, is the Arbitrageurs' dynamic programme on the pooled "
              "picture: " + MATHS_COMMON,
        trades=_tri_trades("WTIBTC", "BTC", "WTI"),
        pipeline="bins", arbitrage="oil", trading=TRI_TRADING,
    ),
    Model(
        "arbitrage", "Arbitrageur", "Arbitrage", "Covers one close in two series for less than it pays, either way",
        "Three series settle on Bitcoin's price at the same instants: every hourly WTI/BTC close has an hourly Bitcoin "
        "close at the same second (oil in satoshis is oil in dollars over Bitcoin in dollars), and at 00:00 UTC the daily "
        "Bitcoin close joins them. Two books on one event should agree, and they are made by different traders at different "
        "speeds, so they don't: minutes before a close the hourly Bitcoin book has put half its weight on one range while "
        "the WTI/BTC book still sits near its flat seed. The bot buys, across the two books, the cheapest set of ranges that "
        "holds the settlement price wherever it lands in a band, so at least one contract pays 98 sats whichever way the "
        "price moves. When the two legs cost less than that after fees, the difference is locked in when they are bought. "
        "It takes no view on direction, tries the whole price line first, and otherwise covers the widest band the zoo's "
        "consensus says the price almost never leaves. Covers are held to the close.",
        fundamental, kind="forecast", factors=("market",), reference=REFERENCE,
        inputs="Both books of a pair of closes at one instant, read fresh on every visit: every range's shares, priced with the "
               "LS-LMSR exactly as the server prices them. The spot of Bitcoin and of WTI/BTC, whose product is the dollar "
               "price of oil in sats that links the two ladders, and the two series' hourly candles, from which it measures "
               "how much that dollar price moves in an hour. For the band it reads " + CONSENSUS_INPUTS[0].lower() + CONSENSUS_INPUTS[1:],
        maths="Ladder A is Bitcoin in dollars (the hourly or daily close), B the other ladder. A price x on A settles B at "
              "x·(1 ± s) (two Bitcoin markets, s the oracle slack) or at K·(1 ± m)/x (WTI/BTC, K = WTI/BTC spot × BTC spot, "
              "m = z·σ_w·√h + floor the oil leg's band, σ_w the hourly σ of the implied dollar price). " + MATHS_COMMON,
        trades="A cover, not a range: on the hourly Bitcoin ladder the ranges the WTI/BTC book prices dear relative to Bitcoin's, "
               "and on WTI/BTC the ranges that map to the rest of the band, typically its flat tails at a fraction of a sat "
               "each. In practice that is the last hour before a close, when the Bitcoin book has sharpened and the WTI/BTC "
               "book has not. It buys when the cover returns at least 5% on cost after fees, never more than a fifth of the "
               "budget in one pair of closes, and holds to the close. It says nothing when every cover costs more than it pays, "
               "which far from a close is the rule.",
        pipeline="bins", arbitrage="bitcoin", trading=TRADING,
    ),
    Model(
        "arbitrage_gold", "Gold Arbitrageur", "Arbitrage", "Covers gold's closes across dollars, satoshis, Bitcoin and oil",
        "Gold settles on four ladders at once. Every hour the gold-in-dollars close (XAU), the Tether Gold-in-satoshis close "
        "(XAUTBTC, an ounce in sats), the Bitcoin close and the WTI/BTC close land at the same second, and at 00:00 UTC the "
        "daily gold close joins them. An ounce in satoshis is an ounce in dollars over Bitcoin in dollars, so gold in dollars "
        "and gold in sats are one event seen through Bitcoin's price; Tether Gold in sats and Bitcoin in dollars are one event "
        "seen through gold's dollar price; and oil in sats and gold in sats are one event seen through the oil/gold ratio. "
        "Four books made by four crowds at four speeds disagree, and the sats-per-ounce book, the youngest and thinnest of "
        "them, is usually the one still near its flat seed when the others have formed. The bot buys, across any two of "
        "them, the cheapest set of ranges that holds the settlement wherever it lands in a band, so at least one contract "
        "pays 98 sats whichever way the price moves, and only when that costs less than it pays. It takes no view on gold, "
        "Bitcoin or oil, tries the whole price line first, and otherwise covers the widest band the zoo's consensus says the "
        "price almost never leaves. Covers are held to the close.",
        fundamental, kind="forecast", factors=("market",), reference=REFERENCE,
        inputs="Both books of a pair of closes at one instant, read fresh on every visit: every range's shares, priced with the "
               "LS-LMSR exactly as the server prices them. The spots of gold (PAX Gold on Coinbase), Tether Gold in sats "
               "(Glimpse's own oracle), Bitcoin and WTI/BTC, whose ratios and products link the ladders pair by pair, and each "
               "series' hourly candles, from which it measures how much the leg that moves between two ladders moves in an hour: "
               "about 0.4% for Bitcoin's price between gold in dollars and gold in sats, 0.2% for gold's dollar price between "
               "Bitcoin and Tether Gold, 0.5% for the oil/gold ratio between WTI/BTC and Tether Gold, measured on the first week "
               "of the sats-per-ounce series. Tether Gold trades about 0.2% under PAX Gold; that basis is in the spot ratio and "
               "its drift in the measured σ. For the band it reads " + CONSENSUS_INPUTS[0].lower() + CONSENSUS_INPUTS[1:],
        maths="Ladder A is the one the consensus is drawn on, B the other. A price x on A settles B at x·(1 ± s) (hourly and "
              "daily gold, s the oracle slack), at k·x·(1 ± m) (gold in sats from gold in dollars with k = XAUTBTC spot / XAU "
              "spot; WTI/BTC from Tether Gold with k = WTIBTC spot / XAUTBTC spot) or at K·(1 ± m)/x (Tether Gold from Bitcoin, "
              "K = XAUTBTC spot × BTC spot, the dollar price of an ounce in sats). m = z·σ·√h + floor is the free leg's band, σ "
              "the hourly σ of the log of B/A for a direct link and of B×A for an inverse one, read from the candles. "
              + MATHS_COMMON,
        trades="A cover, not a range: on the formed ladder the ranges the other book prices dear relative to it, and on the "
               "other the ranges that map to the rest of the band, typically the flat tails of the sats-per-ounce book at a "
               "fraction of a sat each. Five pairs at 00:00 UTC, three every other hour, the nearest pair visited every other "
               "time. It buys when the cover returns at least 5% on cost after fees, never more than a fifth of the budget in "
               "one pair of closes, and holds to the close. It says nothing when every cover costs more than it pays, which far "
               "from a close is the rule.",
        pipeline="bins", arbitrage="gold", trading=TRADING,
    ),
]
