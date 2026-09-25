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
