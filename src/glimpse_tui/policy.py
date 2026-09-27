"""How a bot turns its picture into orders: the buying and selling rules, pure numbers in and out.

A contract on a range costs its price x (sats, 0 to 100) plus the 2% commission, and pays 98 sats net of the
settlement fee if the close lands there. A bot whose picture gives the range probability p expects

    EV = p · 98 − x · 1.02    per contract,

so it buys only where EV > 0 by a margin, never past the price at which EV is gone, and sells a contract it holds
only while the market pays more for it (after the 2% exit fee) than p · 98. Across many small independent bets with
EV > 0 the law of large numbers does the rest: the average return converges on the average edge.

Two rules share that arithmetic:

- Kelly (every forecast bot unless it says otherwise, `bots.decide`): stakes a quarter of the Kelly fraction of the
  budget on every underpriced range, the largest edges first.
- An opportunistic `Policy` (this module): buys only ranges the market sells cheap, only where the picture says they
  are worth several times their price, only in the region of the ladder the bot is about, with a small fixed stake
  per range spread across many ranges. Risk small, win big, many times.

Both sell partially: a position is sold down only until the next contract would fetch less than the picture says it
is worth, so an overpaid range is trimmed back to fair value instead of dumped whole.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import pricing as P

NET = P.PAYOUT_SATS * (1 - P.FEE)       # 98 sats a winning contract pays after the settlement fee
REGIONS = {
    "any": "anywhere on the ladder",
    "near": "inside the middle half of its own picture, the ranges the close is most likely to land in",
    "above": "wholly above the spot price",
    "below": "wholly below the spot price",
    "tails": "outside the middle 80% of its own picture",
    "up-tail": "above the spot price and above the 90th percentile of its own picture",
    "down-tail": "below the spot price and below the 10th percentile of its own picture",
}


@dataclass(frozen=True)
class Policy:
    """An opportunistic trading rule. Prices are in sats per contract (a contract pays 100 gross)."""
    max_price: float = 5.0          # only ranges the market sells at or under this: at 5 sats a win pays 19.6×
    min_ratio: float = 1.5          # p·98 must be at least this many times price·1.02: EV per sat staked ≥ ratio − 1
    region: str = "any"             # key of REGIONS
    stake: float = 0.01             # target cost per range, as a share of the budget
    market_cap: float = 0.10        # most of the budget held in one close
    max_ranges: int = 40            # most ranges bought in one close per cycle
    hold: bool = False              # True: buy and hold to settlement; False: also sell down to fair value
    stance: str = ""                # what the bots list calls it: bullish, bearish, sideways, volatile, neutral

    def note(self) -> str:
        """The about screen's RUNNER section for a bot trading on this policy."""
        pays = NET / (self.max_price * (1 + P.FEE))
        sell = ("It never sells: every ticket is held to the close, win or lose." if self.hold else
                "It sells a range it holds only while the market pays more for the next contract, after the 2% exit fee, "
                "than p · 98, and only that many contracts: an overpriced range is trimmed back to fair value, not dumped.")
        return (
            "An opportunistic rule, not Kelly. A contract costs its price x plus 2% and pays 98 sats net if the close lands in "
            f"its range, so with the picture's probability p its expected value is p · 98 − 1.02 · x. It considers only ranges "
            f"{REGIONS.get(self.region, self.region)}, and only those the market sells at {self.max_price:g} sats or less "
            f"(a win pays at least {pays:.0f} times the stake). Of those it buys a range when p · 98 ≥ {self.min_ratio:g} × 1.02 · x: "
            f"every sat staked is expected back at least {self.min_ratio:g} times, which leaves room for the picture being "
            f"wrong in the tails. Best odds first, it spends up to {self.stake:.1%} of the budget on each range and never "
            f"buys past the price at which the ratio falls under {self.min_ratio:g} or the price rises over {self.max_price:g}; "
            f"at most {self.max_ranges} ranges a close and {self.market_cap:.0%} of the budget held in one close. An order "
            "is sent only if its expected value stays positive after the commission, including the 1 sat minimum. Most "
            f"tickets lose; the few that land pay for them many times over, and over many closes the average edge is what "
            f"remains. {sell}")


# ── where a policy looks ────────────────────────────────────

def region_mask(policy: Policy, probs, bins, spot: float) -> list[bool]:
    """Which ranges the policy may buy. Regions tied to the picture read its own quantiles, so they follow the
    picture's width at every horizon without knowing the volatility clock."""
    n = len(probs)
    cum, below = [0.0] * n, 0.0
    for i, p in enumerate(probs):
        below += p
        cum[i] = below                              # probability at or below the range's upper edge
    lower = [cum[i] - probs[i] for i in range(n)]   # probability below its lower edge
    r = policy.region
    out = []
    for i, (lo, hi) in enumerate(bins):
        if r == "near":
            ok = lower[i] < 0.75 and cum[i] > 0.25
        elif r == "above":
            ok = lo >= spot
        elif r == "below":
            ok = hi <= spot
        elif r == "tails":
            ok = cum[i] <= 0.10 or lower[i] >= 0.90
        elif r == "up-tail":
            ok = lo >= spot and lower[i] >= 0.90
        elif r == "down-tail":
            ok = hi <= spot and cum[i] <= 0.10
        else:
            ok = True
        out.append(ok)
    return out


def wants(policy: Policy, p: float, price: float) -> bool:
    """A range worth buying at this price: cheap, and worth `min_ratio` times its fee-inclusive cost."""
    return 0 < price <= policy.max_price and p * NET >= policy.min_ratio * price * (1 + P.FEE)


def picks(policy: Policy, probs, bins, prices, spot: float) -> list[int]:
    """Indices of the ranges the policy would buy at these prices, best odds first."""
    mask = region_mask(policy, probs, bins, spot)
    got = [i for i, p in enumerate(probs) if mask[i] and wants(policy, p, prices[i])]
    return sorted(got, key=lambda i: -probs[i] / prices[i])


# ── orders ──────────────────────────────────────────────────

@dataclass
class Leg:
    index: int
    contracts: float
    cost_sats: float                # fee-inclusive


class Quote:
    """One book's LS-LMSR, repriced fast when a single range's shares change: numpy over the ladder, O(n) a call
    instead of a Python loop, which is what lets a bot plan every close ahead in a moment. `add` commits a buy so
    the next range is priced after it, as the server prices a multi-leg order."""

    def __init__(self, q: list[float], alpha: float) -> None:
        import numpy as np

        self.np, self.alpha = np, alpha
        self.q = np.asarray(q, dtype=float).copy()
        self.s = float(self.q.sum())
        self.base = self.cost_after(0, 0.0)

    def _parts(self, i: int, d: float):
        np = self.np
        s = self.s + d
        b = self.alpha * s
        z = self.q / b                              # a fresh array: range i's term is replaced before the max is taken,
        z[i] = (self.q[i] + d) / b                  # so neither a buy nor a sale can push the other terms into underflow
        mx = float(z.max())
        ex = np.exp(z - mx)
        return s, b, ex, float(ex[i]), float(ex.sum()), mx

    def cost_after(self, i: int, d: float) -> float:
        """C(q + d at range i) in sats."""
        _, b, _, _, se, mx = self._parts(i, d)
        return P.PAYOUT_SATS * b * (mx + math.log(se))

    def price_after(self, i: int, d: float) -> float:
        """Range i's marginal price in sats once d contracts are added to it (d < 0 sells)."""
        return self.both_after(i, d)[0]

    def both_after(self, i: int, d: float) -> tuple[float, float]:
        """(range i's marginal price, C) once d contracts are added to it: one pass over the ladder for both."""
        s, b, ex, ei, se, mx = self._parts(i, d)
        lse = mx + math.log(se)
        sqe = float(self.q @ ex) + d * ei           # Σ q'·e with range i's shares moved by d
        return P.PAYOUT_SATS * self.alpha * lse + P.PAYOUT_SATS * (s * ei - sqe) / (s * se), P.PAYOUT_SATS * b * lse

    def add(self, i: int, d: float) -> None:
        self.q[i] += d
        self.s += d
        self.base = self.cost_after(0, 0.0)


def fill_to(q, alpha: float, i: int, target_price: float, stake: float) -> tuple[float, float]:
    """(contracts, fee-inclusive cost) of the largest buy on range i that neither lifts its price above
    `target_price` nor costs more than `stake`. Contracts to 2 decimal places, as the server takes them. `q` is a
    share list or a `Quote` of the book (pass the Quote when pricing several ranges of one order in turn)."""
    book = q if isinstance(q, Quote) else Quote(q, alpha)
    p0 = book.price_after(i, 0.0)
    if p0 > target_price or stake <= 0:
        return 0.0, 0.0
    # The price only rises as the range is bought, so d contracts cost at least d·p0 plus the fee: no search past that.
    lo, hi = 0.0, min(5000.0, stake / ((1 + P.FEE) * max(p0, 1e-12)) * (1 + 1e-9) + 1e-6)
    while hi - lo > 4e-9:                                   # as fine as the old 40 halvings of 5000
        mid = (lo + hi) / 2
        price, cost = book.both_after(i, mid)
        over = price > target_price or (1 + P.FEE) * (cost - book.base) > stake
        lo, hi = (lo, mid) if over else (mid, hi)
    d = math.floor(lo * 100) / 100
    if d <= 0:
        return 0.0, 0.0
    return d, (1 + P.FEE) * (book.cost_after(i, d) - book.base)


def worth_sending(legs: list[Leg], probs) -> bool:
    """An order earns its keep: expected payout beats the charge, the 1 sat minimum commission included."""
    total = sum(x.cost_sats for x in legs)
    if total < 0.01:
        return False
    raw = total / (1 + P.FEE)
    charge = raw + max(P.FEE * raw, P.MIN_FEE_SATS)
    return sum(probs[x.index] * NET * x.contracts for x in legs) > charge


def decide(book, probs, policy: Policy, bankroll: float, budget: float, held: dict[int, tuple[float, float]],
           held_cost: float, spot: float) -> list[Leg]:
    """The opportunistic buys for one close. `held` maps option id to (contracts, cost) the bot already owns there;
    a range already holding its stake is not topped up, so the same edge is not bought again every cycle."""
    room = min(budget, policy.market_cap * bankroll - held_cost)
    if room < 1:
        return []
    per_range = policy.stake * bankroll
    spent_on = {i: held.get(o, (0.0, 0.0))[1] for i, o in enumerate(book.option_ids)}
    quote = Quote(book.shares, book.alpha)
    legs: list[Leg] = []
    for i in picks(policy, probs, book.bins, book.prices, spot):
        if len(legs) >= policy.max_ranges or room < 1:
            break
        stake = min(per_range - spent_on[i], room)
        if stake < 1:
            continue
        target = min(policy.max_price, probs[i] * NET / ((1 + P.FEE) * policy.min_ratio))
        d, c = fill_to(quote, book.alpha, i, target, stake)
        if d <= 0 or c < 0.01:
            continue
        legs.append(Leg(i, d, c))
        quote.add(i, d)
        room -= c
    return legs if worth_sending(legs, probs) else []


def sell_down(book, probs, held: dict[int, tuple[float, float]], exit_edge: float) -> list[tuple[int, float, float]]:
    """(range index, contracts, proceeds) to sell: for each position, as many contracts as the market pays more for,
    after the exit fee, than (1 + exit_edge) · p · 98 each. Selling pushes the price down, so it stops where the next
    contract would fetch no more than the picture says it is worth: an overpaid range is trimmed, not dumped."""
    out = []
    index = {o: i for i, o in enumerate(book.option_ids)}
    quote = Quote(book.shares, book.alpha) if held else None
    for option_id, (contracts, _) in held.items():
        i = index.get(option_id)
        if i is None or contracts <= 0:
            continue
        floor = probs[i] * NET * (1 + exit_edge)                 # the least a contract must fetch, net of the exit fee
        most = min(contracts, book.shares[i])

        def fetches(x: float, i: int = i) -> float:             # net sats for the next contract after selling x
            return (1 - P.FEE) * quote.price_after(i, -x)

        if most <= 0 or fetches(0.0) <= floor:
            continue
        if fetches(most) > floor:
            x = most
        else:
            lo, hi = 0.0, most
            for _ in range(40):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if fetches(mid) > floor else (lo, mid)
            x = math.floor(lo * 100) / 100
        if x >= contracts - 0.01:
            x = contracts
        if x <= 0:
            continue
        proceeds = P.sell_proceeds(book.shares, book.alpha, i, x)
        if proceeds >= 1 and proceeds > probs[i] * NET * x:
            out.append((i, x, proceeds))
    return out


# ── matching the market to a picture ────────────────────────
#
# What glimpse.markets draws for a close is each range's LS-LMSR price with the common floor taken off (it subtracts
# the 25th-percentile price and scales every column to its peak), which leaves the book's softmax: exp(q_i / b) / Σ,
# with b = alpha · Σq. So "the market shows the bot's forecast" means that softmax equals the picture. Setting a
# range's shares to level + b · ln p makes it so for every range lifted; ranges the picture wants lower than the book
# already has them cannot be sold down (the bot does not own them) and fall as everything else rises.

def _cost(q, alpha: float) -> float:
    import numpy as np

    b = alpha * float(q.sum())
    z = q / b
    mx = float(z.max())
    return P.PAYOUT_SATS * b * (mx + math.log(float(np.exp(z - mx).sum())))


def shown(q, alpha: float):
    """The distribution the site draws from shares `q`: the softmax of q / (alpha · Σq), numpy."""
    import numpy as np

    z = q / (alpha * float(q.sum()))
    ex = np.exp(z - float(z.max()))
    return ex / float(ex.sum())


def gap(book, probs) -> float:
    """How far what the site shows for this close is from the picture: total variation, 0 (the same) to 1."""
    import numpy as np

    p = np.asarray(probs, dtype=float)
    return 0.5 * float(np.abs(shown(np.asarray(book.shares, dtype=float), P.display_alpha(len(book.shares))) - p / p.sum()).sum())


def _shaped(q0, lp, alpha: float, level: float):
    """Shares raised to level + b · ln p wherever that is more than the book has (never lowered: nothing is sold),
    with b = alpha · Σq of the result, found by fixed point."""
    import numpy as np

    b = alpha * float(q0.sum())
    for _ in range(40):
        q = np.maximum(q0, level + b * lp)
        nb = alpha * float(q.sum())
        if abs(nb - b) < 1e-7 * b:
            break
        b = nb
    return q


def match(book, probs, budget: float, tol: float = 0.002) -> tuple[list[Leg], float]:
    """(legs, gap after): the buys that bring what the site shows for this close closest to the picture for at most
    `budget` sats, fee-inclusive. One number is searched, the level the picture's shape is lifted to: higher lifts
    drown the ranges the bot cannot sell, and cost more. The cheapest level within `tol` of the best gap the budget
    reaches wins. EV is not the test: the point is that the market ends up showing the bot's forecast."""
    import numpy as np

    q0 = np.asarray(book.shares, dtype=float)
    p = np.asarray(probs, dtype=float)
    p = np.maximum(p / p.sum(), 1e-15)
    lp = np.log(p) - float(np.log(p).max())            # 0 at the mode, negative elsewhere
    a, da, c0 = book.alpha, P.display_alpha(len(q0)), _cost(q0, book.alpha)
    cap = budget / (1 + P.FEE)

    def gap_of(q) -> float:
        return 0.5 * float(np.abs(shown(q, da) - p).sum())

    b0 = da * float(q0.sum())
    levels = float(q0.max()) + b0 * np.concatenate([np.linspace(-8, 0, 17), np.geomspace(0.05, 400, 60)])
    found = []
    for level in levels:
        q = _shaped(q0, lp, da, float(level))
        c = _cost(q, a) - c0
        if c > cap:
            if found:
                break                                   # cost only rises with the level from here
            continue
        found.append((gap_of(q), c, q))
    if not found:                                       # even the lowest lift costs more than the budget: take part of it
        q = _shaped(q0, lp, da, float(levels[0]))
        lo, hi = 0.0, 1.0
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if _cost(q0 + mid * (q - q0), a) - c0 <= cap else (lo, mid)
        found.append((gap_of(q0 + lo * (q - q0)), 0.0, q0 + lo * (q - q0)))
    best = min(g for g, _, _ in found)
    _, _, q = min((x for x in found if x[0] <= best + tol), key=lambda x: x[1])
    d = np.floor((q - q0) * 100) / 100                  # contracts to 2 places, never more than planned
    quote, legs = Quote(list(q0), a), []
    for i in np.flatnonzero(d >= 0.01):
        before = quote.base
        quote.add(int(i), float(d[i]))
        legs.append(Leg(int(i), float(d[i]), (quote.base - before) * (1 + P.FEE)))
    return legs, gap_of(q0 + np.maximum(d, 0))


def match_sells(book, probs, held: dict[int, tuple[float, float]], slack: float = 0.25, floor: float = 0.002
                ) -> list[tuple[int, float, float]]:
    """(bin index, contracts, proceeds): a held range the site shows well above the picture (by `slack` of its chance
    and `floor` in absolute terms) is sold back down to the picture, or as far as the bot's holding goes. The matching
    counterpart of `sell_down`: it never undoes the buys that put the forecast on the board."""
    import numpy as np

    q = np.asarray(book.shares, dtype=float)
    p = np.asarray(probs, dtype=float)
    p = p / p.sum()
    da = P.display_alpha(len(q))
    s = shown(q, da)
    at = {o: i for i, o in enumerate(book.option_ids)}
    out = []
    for o, (n, _) in held.items():
        i = at.get(o)
        if i is None or n < 0.01 or s[i] <= p[i] * (1 + slack) + floor:
            continue
        lo, hi = 0.0, min(n, float(q[i]) - 0.01)
        for _ in range(30):
            mid = (lo + hi) / 2
            q2 = q.copy()
            q2[i] -= mid
            lo, hi = (mid, hi) if shown(q2, da)[i] > p[i] else (lo, mid)
        d = math.floor(lo * 100) / 100
        if d >= 0.01:
            q2 = q.copy()
            q2[i] -= d
            out.append((i, d, (_cost(q, book.alpha) - _cost(q2, book.alpha)) * (1 - P.FEE)))
    return out
