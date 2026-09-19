"""Options views: one per distinct outlook in Chapter 2 of Kakushadze and Serur, *151 Trading Strategies* (2018).

Each view is the picture the holder of an option position is betting on: for a payoff f(z) of the standardised move
to the close, with strikes in standard deviations, the minimum relative-entropy reweighting of the baseline under
which the payoff's expected value rises by `conviction` of its own standard deviations (weights proportional to
exp(theta * f), with a floor on the effective sample size). Payoffs and convictions are the lab's.

The lab solves theta once at the 24-hour reference horizon and reuses it at every horizon. On the terminal's grid
the standardised baseline is nearly the same at every horizon, so theta is solved at the close's own horizon.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np

from .core import Ctx, Model, call, payoff_picture, put

BOOK = "Kakushadze and Serur, 151 Trading Strategies (2018)"
ENTROPY_REF = "minimum relative-entropy views (Meucci 2008, Risk, 'Fully Flexible Views')"
OPTION_INPUTS = ("Nothing from the candles of its own: only the shared volatility clock σ, fitted once from the loaded closes, and "
                 "the hours to the close, which set the baseline density the payoff bends; the payoff itself ignores the hours. "
                 "It always draws once the 400-bar minimum is met.")

Payoff = Callable[[np.ndarray, float], np.ndarray]


def _opt(model_id: str, name: str, blurb: str, description: str, sections: str, payoff: Payoff, conviction: float = 0.3, *,
         maths: str, trades: str, inputs: str = OPTION_INPUTS) -> Model:
    def fn(ctx: Ctx) -> np.ndarray:
        return payoff_picture(ctx, payoff, conviction)

    fn.__name__ = model_id
    solve = (f" Passed to payoff_picture with conviction {conviction} (the lab's) and min_ess 0.25, the default floor; θ is solved "
             "by bisection at the close's own horizon, where the lab solves it once at 24 hours and reuses it.")
    return Model(model_id, name, "Options views", blurb, description, fn, kind="view", factors=("shape", "direction", "tails"),
                 reference=f"{sections}, {BOOK}; {ENTROPY_REF}", inputs=inputs, maths=maths + solve, trades=trades,
                 pipeline="payoff")


MODELS = [
    _opt("opt_covered_call", "Covered Call", "Leans up, but gives away the rally beyond one sigma",
         "The covered call: own Bitcoin and sell a call one standard deviation above the price. The book calls the outlook "
         "neutral to bullish: gains are welcome up to the strike, and a rally beyond it is given away for the premium. The "
         "picture leans up, and because the position gains nothing more beyond the strike, the upper tail keeps the "
         "baseline's shape instead of fattening the way a plain upward drift would.",
         "Covered call, Section 2.2", lambda z, h: z - call(z, 1.0),
         maths="f(z) = z − max(z − 1, 0) = min(z, 1): linear in the move up to a strike at +1σ, flat at 1 beyond it. The "
               "tilt's ratio to the baseline, exp(θ·f) up to a constant, therefore rises with z from the far downside up to "
               "+1σ and is the same for every range above +1σ. A positive θ leans the picture up.",
         trades="More weight than the baseline on every range from just above spot upward, the ratio rising to about 1.45 "
                "at +1σ and staying there for every range beyond, so it buys from roughly +0.4σ up through the whole upper "
                "tail at one common edge. Every range below spot is thinned, the further down the more (about 0.66 at −1σ, "
                "0.45 at −2σ), and never bought."),
    _opt("opt_covered_put", "Covered Put", "Leans down, but gives away the collapse beyond one sigma",
         "The covered put: short Bitcoin and sell a put one standard deviation below the price. Neutral to bearish: a "
         "decline pays up to the strike, and a collapse beyond it is given away for the premium. The picture leans down, "
         "and because the position gains nothing more beyond the strike, the lower tail keeps the baseline's shape instead "
         "of fattening.",
         "Covered put, Section 2.3", lambda z, h: -z - put(z, -1.0),
         maths="f(z) = −z − max(−1 − z, 0) = min(−z, 1): linear in the fall down to a strike at −1σ, flat at 1 below it. The "
               "ratio to the baseline rises as z falls, from the far upside down to −1σ, and is the same for every range "
               "below −1σ. A positive θ leans the picture down.",
         trades="More weight than the baseline on every range from just below spot downward, the ratio rising to about "
                "1.45 at −1σ and staying there for every range beyond, so it buys from roughly −0.4σ down through the whole "
                "lower tail at one common edge. Every range above spot is thinned, the further up the more, and never "
                "bought."),
    _opt("opt_protective_put", "Protective Put", "Bullish, with the crash scenario kept on the table",
         "The protective put: own Bitcoin and buy a put one standard deviation below as insurance. The outlook is bullish, "
         "but the holder pays to keep the crash scenario on the table: losses stop at the strike. The picture leans up and "
         "the upper tail fattens, while the ranges below the strike are all thinned by the same modest factor rather than "
         "progressively.",
         "Protective put, Section 2.4", lambda z, h: z + put(z, -1.0),
         maths="f(z) = z + max(−1 − z, 0) = max(z, −1): flat at −1 below a strike at −1σ, linear in the move above it with no "
               "cap. The ratio to the baseline is one common factor for every range below −1σ and rises without limit as z "
               "grows above it. A positive θ leans the picture up.",
         trades="More weight than the baseline on every range from about +0.2σ upward, the ratio growing with distance "
                "(about 1.8 at +2σ, several times the baseline in the far tail), so it buys from roughly +0.6σ up and the "
                "far upside most. Ranges between −1σ and spot are thinned progressively, and every range below −1σ shares "
                "one factor of about 0.68; none of those is bought."),
    _opt("opt_protective_call", "Protective Call", "Bearish, with a short squeeze respected",
         "The protective call: short Bitcoin and buy a call one standard deviation above as insurance. Bearish, but a short "
         "squeeze is respected: losses stop at the strike. The picture leans down and the lower tail fattens, while the "
         "ranges above the strike are all thinned by the same modest factor rather than progressively.",
         "Protective call, Section 2.5", lambda z, h: -z + call(z, 1.0),
         maths="f(z) = −z + max(z − 1, 0) = max(−z, −1): flat at −1 above a strike at +1σ, linear in the fall below it with no "
               "cap. The ratio to the baseline is one common factor for every range above +1σ and rises without limit as z "
               "falls below it. A positive θ leans the picture down.",
         trades="More weight than the baseline on every range from about −0.2σ downward, the ratio growing with distance "
                "(about 1.8 at −2σ, several times the baseline in the far tail), so it buys from roughly −0.6σ down and the "
                "far downside most. Ranges between spot and +1σ are thinned progressively, and every range above +1σ shares "
                "one factor of about 0.68; none of those is bought."),
    _opt("opt_bull_call_spread", "Bull Call Spread", "Bullish to a target 1.5 sigma up, not a moonshot",
         "The bull spread: buy an at-the-money call and sell one 1.5 standard deviations higher (the bull put spread has "
         "the same shape). Bullish with a target: the gain is capped at the upper strike, so the picture moves weight into "
         "the zone between the price and the target, and the ranges beyond the target all share the target's boost rather "
         "than a growing one.",
         "Bull call spread and bull put spread, Sections 2.6 and 2.7", lambda z, h: call(z, 0.0) - call(z, 1.5),
         maths="f(z) = max(z, 0) − max(z − 1.5, 0), the move clipped to [0, 1.5]: zero below spot, linear from spot to a strike "
               "at +1.5σ, flat at 1.5 above. The ratio to the baseline is one common factor for every range below spot, "
               "rises from spot to +1.5σ, and is one (larger) common factor for every range above +1.5σ. A positive θ leans "
               "the picture up.",
         trades="More weight than the baseline from about +0.45σ upward, the ratio reaching about 1.75 at +1.5σ and staying "
                "there for the whole upper tail, so it buys from roughly +0.7σ up at an edge that stops growing past the "
                "target. Every range below spot shares one factor of about 0.8 and is never bought."),
    _opt("opt_bear_put_spread", "Bear Put Spread", "Bearish to a target 1.5 sigma down, not a crash",
         "The bear spread: buy an at-the-money put and sell one 1.5 standard deviations lower (the bear call spread has the "
         "same shape). Bearish with a target: the gain is capped at the lower strike, so the picture moves weight into the "
         "zone between the price and the downside target, and the ranges beyond the target all share the target's boost.",
         "Bear call spread and bear put spread, Sections 2.8 and 2.9", lambda z, h: put(z, 0.0) - put(z, -1.5),
         maths="f(z) = max(−z, 0) − max(−1.5 − z, 0), the fall clipped to [0, 1.5]: zero above spot, linear from spot to a "
               "strike at −1.5σ, flat at 1.5 below. The ratio to the baseline is one common factor for every range above "
               "spot, rises from spot down to −1.5σ, and is one (larger) common factor for every range below −1.5σ. A "
               "positive θ leans the picture down.",
         trades="More weight than the baseline from about −0.45σ downward, the ratio reaching about 1.75 at −1.5σ and "
                "staying there for the whole lower tail, so it buys from roughly −0.7σ down at an edge that stops growing "
                "past the target. Every range above spot shares one factor of about 0.8 and is never bought."),
    _opt("opt_long_combo", "Long Combo (Risk Reversal)", "Upside moves bigger than downside ones: right tail grows",
         "The long combo, or long risk reversal: buy a call half a standard deviation above and sell a put half a standard "
         "deviation below. Bullish with a view on skew: upside moves are expected to be bigger than downside ones, so the "
         "right tail of the picture grows and the left tail shrinks, while the band between the strikes is barely touched.",
         "Long combo, Section 2.12; Volatility skew – long risk reversal, Section 7.5",
         lambda z, h: call(z, 0.5) - put(z, -0.5),
         maths="f(z) = max(z − 0.5, 0) − max(−0.5 − z, 0): zero between strikes at −0.5σ and +0.5σ, z − 0.5 above the upper "
               "strike, z + 0.5 (negative) below the lower one; monotone increasing and uncapped both ways. The ratio to "
               "the baseline is one common factor inside the band, rises without limit above +0.5σ and falls towards zero "
               "below −0.5σ. A positive θ leans the picture up.",
         trades="More weight than the baseline from about +0.6σ upward, the ratio growing fast with distance (about 1.85 "
                "at +2σ, four to five times in the far tail), so it buys from roughly +0.9σ up and the far upside most. The "
                "band within ±0.5σ of spot shares one factor of about 0.96 and every range below −0.5σ is thinned "
                "progressively (about 0.5 at −2σ); none of those is bought."),
    _opt("opt_short_combo", "Short Combo (Risk Reversal)", "Downside moves bigger than upside ones: left tail grows",
         "The short combo, or short risk reversal: sell a call half a standard deviation above and buy a put half a "
         "standard deviation below. Bearish with a view on skew: downside moves are expected to be bigger than upside "
         "ones, so the left tail grows and the right tail shrinks, while the band between the strikes is barely touched.",
         "Short combo, Section 2.13", lambda z, h: put(z, -0.5) - call(z, 0.5),
         maths="f(z) = max(−0.5 − z, 0) − max(z − 0.5, 0): zero between strikes at −0.5σ and +0.5σ, −0.5 − z above zero below "
               "the lower strike, 0.5 − z (negative) above the upper one; monotone decreasing and uncapped both ways. The "
               "ratio to the baseline is one common factor inside the band, rises without limit below −0.5σ and falls "
               "towards zero above +0.5σ. A positive θ leans the picture down.",
         trades="More weight than the baseline from about −0.6σ downward, the ratio growing fast with distance (about 1.85 "
                "at −2σ, four to five times in the far tail), so it buys from roughly −0.9σ down and the far downside most. "
                "The band within ±0.5σ of spot shares one factor of about 0.96 and every range above +0.5σ is thinned "
                "progressively; none of those is bought."),
    _opt("opt_bull_call_ladder", "Bull Call Ladder", "A modest rise pays, a runaway rally hurts",
         "The bull call ladder: a bull call spread financed by selling another call further out. The book describes the "
         "outlook as conservatively bullish with low volatility expected: a modest rise pays, a runaway rally hurts. The "
         "picture lifts the zone from just above the price to two standard deviations up and trims the far right tail "
         "below the baseline.",
         "Bull call ladder and bull put ladder, Sections 2.14 and 2.15",
         lambda z, h: call(z, 0.0) - call(z, 1.0) - call(z, 2.0),
         maths="f(z) = max(z, 0) − max(z − 1, 0) − max(z − 2, 0), strikes at spot, +1σ and +2σ: zero below spot, rising to 1 "
               "at +1σ, flat at 1 to +2σ, then 3 − z, through zero at +3σ and negative beyond, uncapped. The ratio to the "
               "baseline is one common factor below spot, peaks on the plateau between +1σ and +2σ, and falls without "
               "limit past +2σ. A positive θ leans the picture up in the middle and thins the far upside.",
         trades="More weight than the baseline between about +0.35σ and +2.6σ, peaking at about 1.58 across the plateau "
                "from +1σ to +2σ, so it buys the ranges from roughly +0.55σ to +2.45σ, most between +1σ and +2σ. Every range "
                "below spot shares one factor of about 0.78, and the ranges beyond +3σ are thinned progressively; none of "
                "those is bought."),
    _opt("opt_bear_put_ladder", "Bear Put Ladder", "A modest decline pays, a crash hurts",
         "The bear put ladder: a bear put spread financed by selling another put further down. Conservatively bearish with "
         "low volatility expected: a modest decline pays, a crash hurts. The picture lifts the zone from just below the "
         "price to two standard deviations down and trims the far left tail below the baseline.",
         "Bear call ladder and bear put ladder, Sections 2.16 and 2.17",
         lambda z, h: put(z, 0.0) - put(z, -1.0) - put(z, -2.0),
         maths="f(z) = max(−z, 0) − max(−1 − z, 0) − max(−2 − z, 0), strikes at spot, −1σ and −2σ: zero above spot, rising to "
               "1 at −1σ, flat at 1 to −2σ, then 3 + z, through zero at −3σ and negative beyond, uncapped. The ratio to the "
               "baseline is one common factor above spot, peaks on the plateau between −1σ and −2σ, and falls without "
               "limit past −2σ. A positive θ leans the picture down in the middle and thins the far downside.",
         trades="More weight than the baseline between about −0.35σ and −2.6σ, peaking at about 1.58 across the plateau "
                "from −1σ to −2σ, so it buys the ranges from roughly −0.55σ to −2.45σ, most between −1σ and −2σ. Every "
                "range above spot shares one factor of about 0.78, and the ranges beyond −3σ are thinned progressively; "
                "none of those is bought."),
    _opt("opt_calendar_spread", "Calendar Spread", "Pinned near today's price for a day, normal after that",
         "The calendar spread: sell a short-dated at-the-money option and buy a longer-dated one at the same strike. The "
         "best case is the price sitting right at the strike when the short option expires, so the picture is pinned near "
         "today's price for closes within the next day and is simply the baseline for any close further out.",
         "Calendar call spread and calendar put spread, Sections 2.18 and 2.19",
         lambda z, h: -np.abs(z) if h <= 24.0 else np.zeros_like(z),
         inputs=("Nothing from the candles of its own: only the shared volatility clock σ and the hours to the close, which here "
                 "decides whether the payoff applies at all (24 hours or less) or the picture is the untouched baseline. It "
                 "always draws once the 400-bar minimum is met."),
         maths="f(z, h) = −|z| when the close is at most 24 hours away, 0 otherwise. Within a day the ratio to the baseline "
               "peaks at spot and falls symmetrically with |z|; beyond a day the payoff is constant, its standard deviation "
               "is zero, and the tilt returns the baseline unchanged. A positive θ pulls the picture into the middle "
               "without leaning it.",
         trades="For closes within 24 hours it puts more weight than the baseline on the ranges within about 0.7σ of spot "
                "(about 1.5 times at spot), so it buys the ranges within roughly ±0.45σ, and thins both sides beyond that "
                "progressively (0.1 or less in the tails), never buying them. For closes more than 24 hours out the picture "
                "is the baseline and it buys nothing."),
    _opt("opt_long_straddle", "Long Straddle", "A big move either way: hollow middle, mass on both sides",
         "The long straddle: buy an at-the-money call and an at-the-money put (strangles and guts are variations). The "
         "outlook is neutral on direction and long volatility: it pays if the price moves a lot either way. The picture "
         "hollows out around today's price and pushes weight out to both sides equally.",
         "Long straddle, strangle and guts, Sections 2.22 to 2.24; synthetic straddles, 2.28 and 2.29",
         lambda z, h: np.abs(z),
         maths="f(z) = |z|, a call and a put both struck at spot: the ratio to the baseline is lowest at spot and rises "
               "without limit and symmetrically with |z|. A positive θ widens the picture without leaning it.",
         trades="More weight than the baseline on every range beyond about 0.9σ from spot on either side, the ratio growing "
                "with distance (about 1.6 at ±2σ, four times in the far tails), so it buys the ranges beyond roughly ±1.2σ "
                "on both sides, the far ones most. The ranges within ±0.9σ are thinned (about 0.7 at spot) and never bought."),
    _opt("opt_short_strangle", "Short Strangle", "The price stays inside one sigma either side: thin tails",
         "The short strangle: sell a call one standard deviation above and a put one standard deviation below (short "
         "straddles and guts are variations). Neutral and short volatility: it earns the premiums as long as the price "
         "stays inside the band. The picture lifts the whole band by one factor and thins both tails, the further out the "
         "more.",
         "Short straddle, strangle and guts, Sections 2.25 to 2.27; short synthetic straddles, 2.30 and 2.31",
         lambda z, h: -(call(z, 1.0) + put(z, -1.0)),
         maths="f(z) = −max(z − 1, 0) − max(−1 − z, 0): zero between strikes at −1σ and +1σ, falling linearly and without "
               "limit beyond either. The ratio to the baseline is one common (largest) factor for every range inside ±1σ "
               "and falls towards zero outside. A positive θ narrows the picture without leaning it.",
         trades="Every range within ±1σ of spot carries one factor of about 1.18, so it buys all of them together at a "
                "thin edge that just clears the runner's 10% threshold when the market prices them near the baseline. "
                "Beyond ±1σ the ranges are thinned progressively (about 0.2 at ±2σ, near zero in the tails) and never "
                "bought."),
    _opt("opt_strap", "Strap", "A big move expected, and a move up pays double",
         "The strap: two at-the-money calls and one put. A volatility bet with a bullish tilt: a big move is expected and "
         "a big move up pays twice as much. The picture widens with more weight on the upside, the near downside slightly "
         "thinned and the far downside still fattened.",
         "Strap, Section 2.34", lambda z, h: 2 * call(z, 0.0) + put(z, 0.0),
         maths="f(z) = 2·max(z, 0) + max(−z, 0): 2z above spot, |z| below it, a V with the steeper arm on the upside, both "
               "arms uncapped. The ratio to the baseline is lowest at spot and rises with distance twice as fast on the "
               "upside as on the downside. A positive θ widens the picture and leans it up.",
         trades="More weight than the baseline above about +0.7σ and below about −1.3σ, the ratio growing with distance "
                "(about 1.85 at +2σ, five to six times in the far upside), so it buys the ranges from roughly +1σ up and "
                "from roughly −1.9σ down, the upside with the larger edge. The ranges between −1.3σ and +0.7σ are thinned "
                "(about 0.74 at spot) and never bought."),
    _opt("opt_strip", "Strip", "A big move expected, and a move down pays double",
         "The strip: one at-the-money call and two puts. A volatility bet with a bearish tilt: a big move is expected and a "
         "big move down pays twice as much. The picture widens with more weight on the downside, the near upside slightly "
         "thinned and the far upside still fattened.",
         "Strip, Section 2.35", lambda z, h: call(z, 0.0) + 2 * put(z, 0.0),
         maths="f(z) = max(z, 0) + 2·max(−z, 0): z above spot, 2|z| below it, a V with the steeper arm on the downside, both "
               "arms uncapped. The ratio to the baseline is lowest at spot and rises with distance twice as fast on the "
               "downside as on the upside. A positive θ widens the picture and leans it down.",
         trades="More weight than the baseline below about −0.7σ and above about +1.3σ, the ratio growing with distance "
                "(about 1.85 at −2σ, five to six times in the far downside), so it buys the ranges from roughly −1σ down "
                "and from roughly +1.9σ up, the downside with the larger edge. The ranges between −0.7σ and +1.3σ are "
                "thinned (about 0.74 at spot) and never bought."),
    _opt("opt_call_ratio_backspread", "Call Ratio Backspread", "Strongly bullish: same centre, fat right tail",
         "The call ratio backspread: sell one call at the money and buy two one standard deviation higher. The book's "
         "outlook is strongly bullish: it loses a little if the price drifts up to the long strike and wins big on a rally "
         "beyond it. The picture keeps everything below spot in proportion, digs a hollow around one standard deviation up "
         "and grows a fat right tail.",
         "Call ratio backspread, Section 2.36", lambda z, h: 2 * call(z, 1.0) - call(z, 0.0),
         maths="f(z) = 2·max(z − 1, 0) − max(z, 0), strikes at spot and +1σ: zero below spot, −z from spot to +1σ (a trough "
               "of −1 at +1σ), z − 2 above it, through zero at +2σ and rising without limit. The ratio to the baseline is one "
               "common factor for every range below spot, lowest at +1σ, and rises without limit past +2σ. A positive θ "
               "fattens the far upside and hollows the near upside.",
         trades="More weight than the baseline beyond about +1.8σ, the ratio growing fast with distance (eight to ten "
                "times in the far tail), so it buys the ranges from roughly +2σ up with the far upside carrying the largest "
                "edge; every range below spot shares one factor of about 1.15 to 1.2, a marginal buy of the whole downside "
                "only when the market prices it near the baseline. The zone from a little above spot to about +1.8σ is "
                "thinned (about 0.45 to 0.5 at +1σ) and never bought."),
    _opt("opt_put_ratio_backspread", "Put Ratio Backspread", "Strongly bearish: same centre, fat left tail",
         "The put ratio backspread: sell one put at the money and buy two one standard deviation lower. Strongly bearish: "
         "it loses a little if the price drifts down to the long strike and wins big on a crash beyond it. The picture "
         "keeps everything above spot in proportion, digs a hollow around one standard deviation down and grows a fat "
         "left tail.",
         "Put ratio backspread, Section 2.37", lambda z, h: 2 * put(z, -1.0) - put(z, 0.0),
         maths="f(z) = 2·max(−1 − z, 0) − max(−z, 0), strikes at spot and −1σ: zero above spot, z from spot to −1σ (a trough "
               "of −1 at −1σ), −2 − z below it, through zero at −2σ and rising without limit. The ratio to the baseline is "
               "one common factor for every range above spot, lowest at −1σ, and rises without limit past −2σ. A positive "
               "θ fattens the far downside and hollows the near downside.",
         trades="More weight than the baseline beyond about −1.8σ, the ratio growing fast with distance (eight to ten "
                "times in the far tail), so it buys the ranges from roughly −2σ down with the far downside carrying the "
                "largest edge; every range above spot shares one factor of about 1.15 to 1.2, a marginal buy of the whole "
                "upside only when the market prices it near the baseline. The zone from a little below spot to about −1.8σ "
                "is thinned (about 0.45 to 0.5 at −1σ) and never bought."),
    _opt("opt_ratio_call_spread", "Ratio Call Spread", "Flat to slightly lower; a strong rally is ruled out",
         "The ratio call spread: buy one call half a standard deviation in the money and sell two a quarter of a standard "
         "deviation above the price. The book's outlook is neutral to bearish: the position is happiest with the price "
         "just above where it is, content with a flat or slightly lower one, and loses on a strong rally. The picture "
         "piles weight around the short strike and cuts the rally tail hard.",
         "Ratio call spread, Section 2.38", lambda z, h: call(z, -0.5) - 2 * call(z, 0.25),
         maths="f(z) = max(z + 0.5, 0) − 2·max(z − 0.25, 0), strikes at −0.5σ and +0.25σ: zero below −0.5σ, z + 0.5 up to a "
               "peak of 0.75 at +0.25σ, then 1 − z, through zero at +1σ and falling without limit. The ratio to the "
               "baseline is one common factor for every range below −0.5σ, highest at +0.25σ, and falls towards zero past "
               "+1σ. A positive θ concentrates the picture just above spot and thins the upside.",
         trades="More weight than the baseline between about −0.3σ and +0.8σ, peaking at about 1.66 at +0.25σ, so it buys "
                "the ranges from roughly −0.15σ to +0.65σ. Every range below −0.5σ shares one factor of about 0.83 and the "
                "ranges above +0.8σ are thinned progressively (about 0.33 at +2σ, near zero in the far upside); none of "
                "those is bought."),
    _opt("opt_ratio_put_spread", "Ratio Put Spread", "Flat to slightly higher; a sharp sell-off is ruled out",
         "The ratio put spread: buy one put half a standard deviation in the money and sell two a quarter of a standard "
         "deviation below the price. Neutral to bullish: the position is happiest with the price just below where it is, "
         "content with a flat or slightly higher one, and loses on a sharp sell-off. The picture piles weight around the "
         "short strike and cuts the crash tail hard.",
         "Ratio put spread, Section 2.39", lambda z, h: put(z, 0.5) - 2 * put(z, -0.25),
         maths="f(z) = max(0.5 − z, 0) − 2·max(−0.25 − z, 0), strikes at +0.5σ and −0.25σ: zero above +0.5σ, 0.5 − z down to "
               "a peak of 0.75 at −0.25σ, then 1 + z, through zero at −1σ and falling without limit. The ratio to the "
               "baseline is one common factor for every range above +0.5σ, highest at −0.25σ, and falls towards zero past "
               "−1σ. A positive θ concentrates the picture just below spot and thins the downside.",
         trades="More weight than the baseline between about −0.8σ and +0.3σ, peaking at about 1.66 at −0.25σ, so it buys "
                "the ranges from roughly −0.65σ to +0.15σ. Every range above +0.5σ shares one factor of about 0.83 and the "
                "ranges below −0.8σ are thinned progressively (about 0.33 at −2σ, near zero in the far downside); none of "
                "those is bought."),
    _opt("opt_long_butterfly", "Long Butterfly", "The price ends exactly where it is: a sharp peak at spot",
         "The long butterfly: buy calls one standard deviation either side of the price and sell two at the money (the put "
         "butterfly and the short iron butterfly share the shape). Neutral: it pays most if the price ends exactly where "
         "it is and nothing beyond the wings. The picture becomes a sharp peak at today's price, with both tails thinned "
         "by the same factor.",
         "Long call and put butterflies, Sections 2.40 and 2.41; 'Long' iron butterfly, Section 2.44",
         lambda z, h: call(z, -1.0) - 2 * call(z, 0.0) + call(z, 1.0),
         maths="f(z) = max(z + 1, 0) − 2·max(z, 0) + max(z − 1, 0) = max(1 − |z|, 0), a tent with strikes at −1σ, spot and "
               "+1σ: peak 1 at spot, zero beyond either wing. The ratio to the baseline is highest at spot, falls linearly "
               "in the exponent to the wings, and is one common factor for every range beyond ±1σ. A positive θ sharpens "
               "the picture at spot without leaning it.",
         trades="More weight than the baseline on the ranges within about 0.57σ of spot, peaking at about 1.64 at spot, so "
                "it buys the ranges within roughly ±0.4σ. The ranges between 0.57σ and 1σ out are thinned progressively "
                "and every range beyond ±1σ shares one factor of about 0.7; none of those is bought."),
    _opt("opt_short_butterfly", "Short Butterfly", "A bounded move of unknown direction: the centre is carved out",
         "The short butterfly: the reverse trade, which pays when the price moves away from today's level by a standard "
         "deviation or more, in either direction, and pays no more for going further. The picture carves out the centre "
         "and lifts everything beyond the wings by the same factor: a move of unknown direction, with the tails kept in "
         "the baseline's proportions.",
         "Short call and put butterflies, Sections 2.42 and 2.43; 'Short' iron butterfly, Section 2.45",
         lambda z, h: -(call(z, -1.0) - 2 * call(z, 0.0) + call(z, 1.0)),
         maths="f(z) = −max(1 − |z|, 0), the tent turned over, strikes at −1σ, spot and +1σ: a trough of −1 at spot, zero "
               "beyond either wing. The ratio to the baseline is lowest at spot, rises linearly in the exponent to the "
               "wings, and is one common (largest) factor for every range beyond ±1σ. A positive θ hollows the picture at "
               "spot without leaning it.",
         trades="Every range beyond about 0.68σ from spot gets more weight than the baseline, and every range beyond ±1σ "
                "shares one factor of about 1.35, so it buys the whole of both tails from roughly ±0.83σ outward at one "
                "common edge. The ranges within ±0.68σ are thinned (about 0.54 at spot) and never bought."),
    _opt("opt_iron_condor", "Iron Condor", "Gets paid while the price stays inside a range",
         "The iron condor: sell a strangle one standard deviation wide and buy protective wings two standard deviations "
         "out (long condors share the shape). Neutral: it collects premium while the price stays in the range and caps "
         "the loss beyond the wings. The picture lifts the whole inner range by one factor, thins the shoulders "
         "progressively, and thins both tails beyond the wings by one common factor.",
         "Long call and put condors, Sections 2.46 and 2.47; Long iron condor, Section 2.50",
         lambda z, h: -(call(z, 1.0) - call(z, 2.0) + put(z, -1.0) - put(z, -2.0)),
         maths="f(z) = −(max(z − 1, 0) − max(z − 2, 0)) − (max(−1 − z, 0) − max(−2 − z, 0)) = −min(max(|z| − 1, 0), 1), "
               "strikes at ±1σ and ±2σ: zero inside ±1σ, falling linearly to −1 at ±2σ, flat at −1 beyond. The ratio to the "
               "baseline is one common (largest) factor inside ±1σ, falls across the shoulders, and is one common (smallest) "
               "factor for every range beyond ±2σ. A positive θ narrows the picture without leaning it.",
         trades="Every range within ±1σ of spot carries one factor of about 1.17, so it buys all of them together at a thin "
                "edge that just clears the runner's threshold when the market prices them near the baseline. The shoulders "
                "from ±1σ to ±2σ are thinned progressively and every range beyond ±2σ shares one factor of about 0.22; "
                "none of those is bought."),
    _opt("opt_collar", "Collar", "A protected, steady gain: leans up with both tails trimmed",
         "The collar: own Bitcoin, buy a put one standard deviation below and pay for it by selling a call one standard "
         "deviation above. The book's outlook is moderately bullish: both tails are given up for a protected, steady gain. "
         "The picture leans up between the strikes, and each tail beyond its strike keeps the baseline's proportions, the "
         "upper one lifted and the lower one thinned.",
         "Collar, Section 2.53", lambda z, h: z + put(z, -1.0) - call(z, 1.0),
         maths="f(z) = z + max(−1 − z, 0) − max(z − 1, 0), the move clipped to [−1, 1]: flat at −1 below a strike at −1σ, "
               "linear between the strikes, flat at 1 above +1σ. The ratio to the baseline is one common factor for every "
               "range below −1σ, rises from −1σ to +1σ, and is one (larger) common factor for every range above +1σ. A "
               "positive θ leans the picture up.",
         trades="More weight than the baseline from about +0.1σ upward, the ratio reaching about 1.47 at +1σ and staying "
                "there for the whole upper tail, so it buys from roughly +0.4σ up at an edge that stops growing past the "
                "strike. Ranges from −1σ to spot are thinned progressively and every range below −1σ shares one factor of "
                "about 0.62; none of those is bought."),
    _opt("opt_bullish_seagull", "Bullish Seagull", "Bullish to a target; the sell-off risk is left as it is",
         "The bullish seagull: a bull call spread (at the money to 1.5 standard deviations up) paid for by selling a put "
         "1.5 standard deviations down. Bullish to a target, with an unhedged sell-off accepted as the price of admission. "
         "The picture lifts the zone up to the target, holds every range beyond the target at the target's boost, leaves "
         "the band between the short put and spot slightly thinned, and thins the crash tail progressively.",
         "Bullish short seagull spread and bullish long seagull spread, Sections 2.54 and 2.57",
         lambda z, h: -put(z, -1.5) + call(z, 0.0) - call(z, 1.5),
         maths="f(z) = max(z, 0) − max(z − 1.5, 0) − max(−1.5 − z, 0), strikes at −1.5σ, spot and +1.5σ: z + 1.5 (negative, "
               "uncapped) below −1.5σ, zero from −1.5σ to spot, z from spot to +1.5σ, flat at 1.5 above. The ratio to the "
               "baseline falls towards zero below −1.5σ, is one common factor between −1.5σ and spot, rises to +1.5σ and "
               "is one (largest) common factor above it. A positive θ leans the picture up.",
         trades="More weight than the baseline from about +0.4σ upward, the ratio reaching about 1.75 at +1.5σ and staying "
                "there for the whole upper tail, so it buys from roughly +0.7σ up at an edge that stops growing past the "
                "target. The band from −1.5σ to spot shares one factor of about 0.81 and the ranges below −1.5σ are "
                "thinned progressively; none of those is bought."),
    _opt("opt_bearish_seagull", "Bearish Seagull", "Bearish to a target; the rally risk is left as it is",
         "The bearish seagull: a bear put spread (at the money to 1.5 standard deviations down) paid for by selling a call "
         "1.5 standard deviations up. Bearish to a target, with an unhedged rally accepted as the price of admission. The "
         "picture lifts the zone down to the target, holds every range beyond the target at the target's boost, leaves the "
         "band between spot and the short call slightly thinned, and thins the rally tail progressively.",
         "Bearish long seagull spread and bearish short seagull spread, Sections 2.55 and 2.56",
         lambda z, h: -call(z, 1.5) + put(z, 0.0) - put(z, -1.5),
         maths="f(z) = max(−z, 0) − max(−1.5 − z, 0) − max(z − 1.5, 0), strikes at −1.5σ, spot and +1.5σ: 1.5 − z (negative, "
               "uncapped) above +1.5σ, zero from spot to +1.5σ, −z from spot to −1.5σ, flat at 1.5 below. The ratio to the "
               "baseline falls towards zero above +1.5σ, is one common factor between spot and +1.5σ, rises down to −1.5σ "
               "and is one (largest) common factor below it. A positive θ leans the picture down.",
         trades="More weight than the baseline from about −0.4σ downward, the ratio reaching about 1.75 at −1.5σ and "
                "staying there for the whole lower tail, so it buys from roughly −0.7σ down at an edge that stops growing "
                "past the target. The band from spot to +1.5σ shares one factor of about 0.81 and the ranges above +1.5σ "
                "are thinned progressively; none of those is bought."),
]
