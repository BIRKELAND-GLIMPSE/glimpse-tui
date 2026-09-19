"""LS-LMSR pricing, recomputed locally from outstanding shares.

The public quotes endpoint rounds `yes_price` to a whole sat, which reads as 0 for
most of a 500-bin market. Every price, probability and ticket figure shown in the
terminal is therefore derived from `shares` here. Pure functions, no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

FEE = 0.02              # commission: added on top of buy cost, taken from sell proceeds and redemption
PAYOUT_SATS = 100.0     # gross sats per winning contract
MIN_FEE_SATS = 1.0      # the enter route charges at least one sat of commission; it has no minimum ticket
V = 2.0                 # LS-LMSR sensitivity; alpha = V / (n ln n)


def alpha_for(n: int) -> float:
    return V / (n * math.log(n))


def cost(q: list[float], alpha: float) -> float:
    """C(q) in sats, max-shifted so a dominant bin cannot overflow exp()."""
    b = alpha * sum(q)
    z = [x / b for x in q]
    mx = max(z)
    return PAYOUT_SATS * b * (mx + math.log(sum(math.exp(v - mx) for v in z)))


def prices(q: list[float], alpha: float) -> list[float]:
    """Unrounded marginal price of every bin in sats per contract."""
    s = sum(q)
    b = alpha * s
    z = [x / b for x in q]
    mx = max(z)
    ex = [math.exp(v - mx) for v in z]
    se = sum(ex)
    first = PAYOUT_SATS * alpha * (mx + math.log(se))
    sqe = sum(qq * e for qq, e in zip(q, ex, strict=True))
    den = s * se
    return [first + PAYOUT_SATS * (s * e - sqe) / den for e in ex]


def implied_probs(px: list[float]) -> list[float]:
    """Prices sum to more than 100 on LS-LMSR; normalise for display only."""
    s = sum(px)
    return [p / s for p in px] if s > 0 else [0.0] * len(px)


@dataclass(frozen=True)
class Ticket:
    """What buying `contracts` on every bin in [lo, hi] costs and can return."""
    contracts: float
    bins: int
    prob: float          # normalised market probability of the range
    cost_sats: float     # fee inclusive
    fee_sats: float
    payout_sats: float   # net of the redemption fee, if any bin in the range wins
    avg_price: float     # fee-inclusive sats per contract per bin
    markets: int = 1     # >1: a box across several closes; payout is then the most it can pay, if every market lands

    @property
    def profit_sats(self) -> float:
        return self.payout_sats - self.cost_sats

    @property
    def roi(self) -> float:
        return self.profit_sats / self.cost_sats if self.cost_sats > 0 else 0.0

    @property
    def odds(self) -> float:
        """Decimal odds: total returned per sat staked."""
        return self.payout_sats / self.cost_sats if self.cost_sats > 0 else 0.0

    @property
    def breakeven_prob(self) -> float:
        return self.cost_sats / self.payout_sats if self.payout_sats > 0 else 0.0


def ticket(q: list[float], alpha: float, lo: int, hi: int, contracts: float, prob: float | None = None) -> Ticket:
    """Price a range bet: `contracts` on each bin index lo..hi inclusive. Pass `prob` if already known."""
    lo, hi = min(lo, hi), max(lo, hi)
    if prob is None:
        prob = sum(implied_probs(prices(q, alpha))[lo:hi + 1])
    q2 = q[:]
    for i in range(lo, hi + 1):
        q2[i] += contracts
    raw = cost(q2, alpha) - cost(q, alpha)
    fee = max(FEE * raw, MIN_FEE_SATS) if raw > 0 else 0.0
    n = hi - lo + 1
    total = raw + fee
    return Ticket(
        contracts=contracts,
        bins=n,
        prob=prob,
        cost_sats=total,
        fee_sats=fee,
        payout_sats=contracts * PAYOUT_SATS * (1 - FEE),
        avg_price=total / (contracts * n) if contracts > 0 else 0.0,
    )


def combine(tickets: list[Ticket]) -> Ticket:
    """One ticket for the same range bought in several markets. `prob` becomes the chance that all of
    them land, treating the closes as independent (as the web app does), which flatters nobody."""
    first = tickets[0]
    cost_sats = sum(t.cost_sats for t in tickets)
    return Ticket(
        contracts=first.contracts, bins=first.bins, prob=math.prod(t.prob for t in tickets),
        cost_sats=cost_sats, fee_sats=sum(t.fee_sats for t in tickets),
        payout_sats=sum(t.payout_sats for t in tickets),
        avg_price=cost_sats / (first.contracts * first.bins * len(tickets)) if first.contracts > 0 else 0.0,
        markets=len(tickets),
    )


def chances(probs: list[float]) -> tuple[float, float, float]:
    """(all land, at least one lands, expected number landing) for closes landing with these probabilities, treated
    as independent: the market prices each close on its own and says nothing about how they move together."""
    return math.prod(probs), 1 - math.prod(1 - p for p in probs), sum(probs)


def bisect_budget(cost_of, budget_sats: float) -> float:
    """Largest contract count (2 dp) with cost_of(contracts) <= budget. `cost_of` must be increasing."""
    a, b = 0.0, 1.0
    while cost_of(b) < budget_sats and b < 1e7:
        b *= 2
    for _ in range(40):
        mid = (a + b) / 2
        a, b = (mid, b) if cost_of(mid) <= budget_sats else (a, mid)
    return math.floor(a * 100) / 100


def contracts_for_budget(q: list[float], alpha: float, lo: int, hi: int, budget_sats: float) -> float:
    """Largest contract count (2 dp) whose fee-inclusive range cost fits the budget."""
    return bisect_budget(lambda n: ticket(q, alpha, lo, hi, n, prob=0.0).cost_sats, budget_sats)


def sell_proceeds(q: list[float], alpha: float, i: int, contracts: float) -> float:
    """Net sats for selling `contracts` on bin i, after the exit fee."""
    q2 = q[:]
    q2[i] -= contracts
    return (1 - FEE) * (cost(q, alpha) - cost(q2, alpha))


def quantile(bin_edges: list[tuple[float, float]], probs: list[float], p: float) -> float:
    """Price level at cumulative probability p, linear inside the bin."""
    acc = 0.0
    for (lo, hi), w in zip(bin_edges, probs, strict=True):
        if acc + w >= p and w > 0:
            return lo + (hi - lo) * (p - acc) / w
        acc += w
    return bin_edges[-1][1]


def signal(probs: list[float]) -> list[float]:
    """Market belief with the subsidy floor removed.

    Every bin is seeded with the same shares, so untraded bins each hold a sliver of
    probability that adds up across 500 bins. Subtracting the 25th-percentile mass and
    renormalising leaves what traders actually expressed. Display only, never for pricing.
    """
    floor = sorted(probs)[len(probs) // 4]
    lifted = [max(p - floor, 0.0) for p in probs]
    s = sum(lifted)
    return [p / s for p in lifted] if s > 0 else probs[:]


def hdi(probs: list[float], mass: float = 0.8) -> tuple[int, int]:
    """Narrowest contiguous bin window holding at least `mass`. Two-pointer, O(n)."""
    best = (0, len(probs) - 1)
    acc, lo = 0.0, 0
    for hi, p in enumerate(probs):
        acc += p
        while acc - probs[lo] >= mass:
            acc -= probs[lo]
            lo += 1
        if acc >= mass and hi - lo < best[1] - best[0]:
            best = (lo, hi)
    return best
