"""Kelly sizing on one close: the growth-optimal buy across every range of an LS-LMSR ladder, pure numbers in and out.

A close has n ranges and exactly one of them wins. A contract on range i costs its marginal price x_i plus the 2%
commission and pays 98 sats net of the settlement fee if the close lands there. A bot with `free` sats set aside for
this close, a picture p (p_i the chance range i wins) and `held` contracts already on each range asks how many more
to buy on each, dq ≥ 0. If range i wins its wealth ends at

    W_i = free − (1 + fee) · (C(q + dq) − C(q)) + 98 · (held_i + dq_i),

where C is the LS-LMSR cost function of the book's shares q, so C(q + dq) − C(q) is what the whole order costs before
commission (the server prices a multi-leg order the same way). Kelly picks dq to maximise the expected log of that
wealth, G = Σ p_i ln W_i: the one rule that grows a bankroll fastest over many closes and can never lose it all, because
a W_i of zero is minus infinity. G is concave in dq (C is convex, ln of a concave positive function is concave), so
there is one best order and a hill climb finds it.

Its gradient says what to buy next. With Λ = Σ_j p_j / W_j (the marginal value of one more sat, averaged over who wins),

    ∂G/∂dq_k = −(1 + fee) · π_k(q + dq) · Λ + 98 · p_k / W_k,

π_k the marginal price after the order so far. Divided by (1 + fee) · Λ that is a price gap in sats a contract: the
price range k is worth to this bankroll, 98 · p_k / ((1 + fee) · W_k · Λ), minus the price the book asks. Every range
is bought until its gap closes or stays unbought because the gap was never positive. The optimiser takes Newton steps
over the ranges in play (those bought or with a positive gap, a dozen of five hundred) using the exact Hessian, the
LS-LMSR price Jacobian (diagonal plus rank four, written out in `_jacobian`) and the wealth terms, so a range priced a
hundredth of its worth is reached in a handful of steps rather than one contract at a time; a backtracking line
search accepts a step only where every W_i stays positive, no outcome's wealth falls under a quarter of what it was,
and the growth rises. Gains are summed as p_i · log1p(δ_i / W_i), δ_i the change in wealth, so a bankroll of a trillion
sats still resolves a sat. A picture that gives a range one chance in 10¹² asks Kelly to stake the whole allotment
against it; the climb floors every range at `Sizing.floor` (one in a million), which keeps a sliver of the allotment
alive whichever range wins and keeps the climb off the wall where the Newton model says nothing.

Bet your beliefs: with no fee and capital so deep that W_i ≈ free for every i, Λ ≈ 1 / free and the gap closes where
π_k = 100 · p_k, the LS-LMSR price of every range bought equals the picture's probability, and every range left alone
was already priced at or above it. That is what "the public forecast shows the private model" means, and it is a
property of the optimum, not a target the bot aims at. Finite capital stops short of it (W_k rises with dq_k, so the
worth of range k falls as it is bought) and the 2% commission lowers every worth by the fee. `Sizing.kelly` then buys
that fraction of the whole optimal position: a half-Kelly bot holding what it bought last time finds nothing left to
buy, because the fraction applies to held plus new, not to new alone.

`kelly_buys` turns dq into legs a server will take: contracts to two decimal places, each leg sized with
`policy.fill_to` so its last contract still returns `Sizing.min_edge` on its fee-inclusive cost. The server prices a
multi-leg order as one, C(q + dq) − C(q), so a range's last contract costs its price once every leg is in, not its
price alone on the fresh book: buying the ranges beside it lowers it (a range priced at 24 sats alone sits at 13
once its neighbours are bought). A leg capped alone would buy half the position and leave the forecast short of the
model until the next pass, so each leg is refilled given all the others, in cost order, in passes until none
changes. The order is sent only if `policy.worth_sending` says the expected payout beats the charge, the one-sat
minimum commission included, and that minimum is kept back from `free` before Kelly sees it. `allot` splits one
bankroll across the closes a series has open, evenly by default, so the same price path is not bet many times over.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import policy as PL
from . import pricing as P
from .policy import Leg

ARMIJO = 1e-4           # the fraction of the gradient's promise a step must deliver to be accepted
BACKTRACKS = 60         # halvings before a step is given up (2^-60 of the first try: nothing moves any more)
SHRINK = 0.25           # the least of any outcome's wealth one step may leave, as a share of what it was
PASSES = 8              # refills of every leg given the others before `kelly_buys` settles for what it has
LEAST_HOURS = 1 / 60    # a close a minute or less away is weighted as one minute away in `allot`


@dataclass(frozen=True)
class Sizing:
    """How much of the optimum is bought and how it is sent."""
    kelly: float = 0.5           # fraction of the Kelly optimum that is bought (1 = full Kelly)
    min_edge: float = 0.02       # the last contract of every leg must still return this much on cost after both fees
    max_iter: int = 500
    tol: float = 1e-6            # converged when no range's price gap exceeds tol · 98 sats
    floor: float = 1e-6          # the least chance the climb gives any range: a sliver of the allotment survives every outcome


# ── the ladder, vectorised ──────────────────────────────────

def _book(q: np.ndarray, alpha: float):
    """One pass over shares `q`: (marginal prices, C, b, softmax, centred z, softmax variance of z). The prices and C
    are pricing.prices and pricing.cost, max-shifted so a dominant range cannot overflow exp(). With z = q / b,
    σ = softmax(z) and z̄ = Σ σ z, the LS-LMSR price is 100 · (alpha · ln Σ e^z + σ_k − alpha · z̄); the last three
    returns are what its Jacobian needs."""
    s = float(q.sum())
    b = alpha * s
    z = q / b
    mx = float(z.max())
    zs = z - mx
    ex = np.exp(zs)
    se = float(ex.sum())
    sig = ex / se
    zc = zs - float(sig @ zs)                       # z − z̄, unchanged by the shift
    lse = mx + math.log(se)
    px = P.PAYOUT_SATS * (alpha * lse + sig - alpha * float(sig @ z))
    return px, P.PAYOUT_SATS * b * lse, b, sig, zc, float(sig @ zc**2)


def _jacobian(alpha: float, b: float, sig: np.ndarray, zc: np.ndarray, vz: float, idx: np.ndarray) -> np.ndarray:
    """∂π_k/∂q_l of the LS-LMSR for the ranges `idx`: (100 / b) · (diag σ − σσᵀ − alpha (v1ᵀ + 1vᵀ) + alpha² V_z 11ᵀ),
    v = σ (z − z̄), V_z the softmax variance of z. Symmetric, as a cost function's Hessian is; its null direction is
    q itself (C is homogeneous of degree one), so a block over fewer than all the ranges is positive definite."""
    s, v = sig[idx], sig[idx] * zc[idx]
    one = np.ones(len(idx))
    return (P.PAYOUT_SATS / b) * (np.diag(s) - np.outer(s, s) - alpha * (np.outer(v, one) + np.outer(one, v)) + alpha**2 * vz)


def _arrays(q, probs, held, dq=None) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    q = np.asarray(q, dtype=float)
    p = np.asarray(probs, dtype=float)
    h = np.zeros_like(q) if held is None else np.asarray(held, dtype=float)
    d = np.zeros_like(q) if dq is None else np.asarray(dq, dtype=float)
    return q, p, h, d


def wealth(q, alpha: float, probs, held, free: float, dq, fee: float = P.FEE, net: float = PL.NET) -> np.ndarray:
    """W_i = free − (1 + fee) · (C(q + dq) − C(q)) + net · (held_i + dq_i): terminal wealth if range i wins."""
    q, _, h, d = _arrays(q, probs, held, dq)
    c0, c1 = _book(q, alpha)[1], _book(q + d, alpha)[1]
    return free - (1 + fee) * (c1 - c0) + net * (h + d)


def growth(q, alpha: float, probs, held, free: float, dq, fee: float = P.FEE, net: float = PL.NET) -> float:
    """Σ p_i ln W_i, the expected log wealth; −inf if any range would leave nothing."""
    q, p, h, d = _arrays(q, probs, held, dq)
    w = wealth(q, alpha, p, h, free, d, fee, net)
    if np.any(w <= 0):
        return -math.inf
    return float(p @ np.log(w))


def gradient(q, alpha: float, probs, held, free: float, dq, fee: float = P.FEE, net: float = PL.NET) -> np.ndarray:
    """∂G/∂dq_k = −(1 + fee) · π_k(q + dq) · Σ_j p_j / W_j + net · p_k / W_k, π the LS-LMSR marginal price."""
    q, p, h, d = _arrays(q, probs, held, dq)
    w = wealth(q, alpha, p, h, free, d, fee, net)
    px = _book(q + d, alpha)[0]
    lam = float((p / w).sum())
    return -(1 + fee) * px * lam + net * p / w


def hessian(q, alpha: float, probs, held, free: float, dq, fee: float = P.FEE, net: float = PL.NET) -> np.ndarray:
    """∂²G/∂dq_k ∂dq_l: −(1 + fee) Λ J − (1 + fee)² M ππᵀ + (1 + fee) net (πuᵀ + uπᵀ) − net² diag u, with J the price
    Jacobian, Λ = Σ p_j / W_j, M = Σ p_j / W_j², u_j = p_j / W_j². Negative definite wherever growth is finite: the
    hill has one top."""
    q, p, h, d = _arrays(q, probs, held, dq)
    w = wealth(q, alpha, p, h, free, d, fee, net)
    px, _, b, sig, zc, vz = _book(q + d, alpha)
    return _curvature(alpha, b, sig, zc, vz, px, p, w, np.arange(len(q)), fee, net)


def _curvature(alpha, b, sig, zc, vz, px, p, w, idx, fee, net) -> np.ndarray:
    lam, u = float((p / w).sum()), p / w**2
    m = float(u.sum())
    pk, uk = px[idx], u[idx]
    return (-(1 + fee) * lam * _jacobian(alpha, b, sig, zc, vz, idx) - (1 + fee) ** 2 * m * np.outer(pk, pk)
            + (1 + fee) * net * (np.outer(pk, uk) + np.outer(uk, pk)) - net**2 * np.diag(uk))


def _newton(alpha, b, sig, zc, vz, px, p, w, idx, grad, fee, net) -> np.ndarray:
    """The Newton step over the ranges `idx`, solving −H d = g with −H split as B + c ππᵀ: B holds the price curvature
    and the wealth terms of the ranges in play, c = (1 + fee)² Σ p_i / W_i² over every other outcome. When a thin
    outcome's wealth nears zero c explodes and swamps a plain solve; Sherman–Morrison keeps the step exact, and in
    the limit it is the Newton step at zero marginal cost, a reallocation between the ranges bought, which is all
    the wall allows. Raises LinAlgError when B will not solve."""
    lam, u = float((p / w).sum()), p / w**2
    pk, uk = px[idx], u[idx]
    m_in = float(uk.sum())
    c = (1 + fee) ** 2 * (float(u.sum()) - m_in)
    bmat = ((1 + fee) * lam * _jacobian(alpha, b, sig, zc, vz, idx) + (1 + fee) ** 2 * m_in * np.outer(pk, pk)
            - (1 + fee) * net * (np.outer(pk, uk) + np.outer(uk, pk)) + net**2 * np.diag(uk))
    sol = np.linalg.solve(bmat, np.column_stack([grad[idx], pk]))
    dg, dv = sol[:, 0], sol[:, 1]
    return dg - dv * (c * float(pk @ dg) / (1 + c * float(pk @ dv)))


# ── the optimum ─────────────────────────────────────────────

class _Point:
    """The climb's view of one dq: wealth, prices, the price each range is worth to this bankroll, and the growth
    gain G(dq) − G(0) summed as p · log1p(δ / W0), exact where ln W itself would round the change away."""
    __slots__ = ("ok", "gain", "w", "px", "worth", "b", "sig", "zc", "vz", "lam")

    def __init__(self, q, alpha, p, w0, c0, dq, fee, net) -> None:
        self.px, c1, self.b, self.sig, self.zc, self.vz = _book(q + dq, alpha)
        delta = -(1 + fee) * (c1 - c0) + net * dq
        self.w = w0 + delta
        self.ok = not np.any(self.w <= 0)
        if not self.ok:
            self.gain, self.worth, self.lam = -math.inf, None, math.inf
            return
        self.gain = float(p @ np.log1p(delta / w0))
        self.lam = float((p / self.w).sum())
        self.worth = net * p / ((1 + fee) * self.w * self.lam)


def optimum(q, alpha: float, probs, held, free: float, sizing: Sizing, fee: float = P.FEE, net: float = PL.NET) -> np.ndarray:
    """The full-Kelly dq* ≥ 0 maximising growth, deterministic and vectorised: a few milliseconds for 500 ranges.

    A projected Newton climb on a concave hill. At each step the ranges in play are those already bought or whose
    price gap (worth − price, in sats a contract) is positive; the exact Hessian over just those ranges, usually a
    dozen of five hundred, gives the step, and a backtracking line search on the growth gain accepts it only where
    every W_i stays positive, no outcome's wealth falls to under a quarter of what it was (the log of a wealth is
    quadratic-like only within a factor of a few of zero, so a step past that would land where the model says
    nothing, pinned against the wall with no room to reallocate), and the growth rises by a fraction of what the
    gradient promised. Should the Hessian
    block fail to factor, the step falls back to moving every range toward its worth in log-price, the gradient
    scaled by a positive diagonal, which is always an ascent direction. Converged when no range's gap (max(gap, 0)
    for one not bought) exceeds tol · net, or after max_iter steps, or when no step improves the growth any more.
    Zeros when nothing is +EV, when there is nothing free to spend, or when the picture is empty.

    The picture is floored at `sizing.floor` a range and renormalised. Kelly takes a picture's zero at its word and
    would stake the whole allotment against it, and a picture is a model: one chance in a million a range keeps a
    sliver of the allotment alive whichever range wins (about a fifth of a percent of it over 500 ranges, nothing
    unless the whole allotment was about to go), and it keeps the climb off the wall where that sliver is spent,
    which the growth barely feels but the Newton model cannot see."""
    q, p, h, _ = _arrays(q, probs, held)
    n = len(q)
    dq = np.zeros(n)
    total = float(p.sum())
    if n == 0 or free <= 0 or total <= 0 or float(q.sum()) <= 0:
        return dq
    p = np.maximum(p / total, sizing.floor)
    p = p / float(p.sum())
    w0 = free + net * h
    if float(w0.min()) <= 0:
        return dq
    c0 = _book(q, alpha)[1]
    at = _Point(q, alpha, p, w0, c0, dq, fee, net)
    bound = sizing.tol * net
    for _ in range(sizing.max_iter):
        gap = at.worth - at.px
        projected = np.where(dq > 0, gap, np.maximum(gap, 0.0))
        if float(np.abs(projected).max()) <= bound:
            break
        grad = (1 + fee) * at.lam * gap                     # ∂G/∂dq in its own units, for the line search's promise
        idx = np.flatnonzero((p > 0) & ((dq > 0) | (gap > 0)))
        step = np.zeros(n)
        try:
            step[idx] = _newton(alpha, at.b, at.sig, at.zc, at.vz, at.px, p, at.w, idx, grad, fee, net)
            if not np.all(np.isfinite(step[idx])) or float(grad[idx] @ step[idx]) <= 0:
                raise np.linalg.LinAlgError
        except np.linalg.LinAlgError:
            step[idx] = _toward_worth(at, p, idx, fee, net)
        t, moved = 1.0, False
        for _ in range(BACKTRACKS):
            new = np.maximum(dq + t * step, 0.0)
            change = new - dq
            if not np.any(change):
                break
            trial = _Point(q, alpha, p, w0, c0, new, fee, net)
            if trial.ok and float(trial.w.min()) >= SHRINK * float(at.w.min()) and trial.gain - at.gain >= ARMIJO * float(grad @ change):
                dq, at, moved = new, trial, True
                break
            t *= 0.5
        if not moved:
            break
    return dq


def _toward_worth(at: _Point, p: np.ndarray, idx: np.ndarray, fee: float, net: float) -> np.ndarray:
    """The fallback step for the ranges `idx`: each toward the price it is worth in log-price, ln(worth / π) over how
    fast its log gap closes per contract (the price's own log-slope, about 1 / b, its winning wealth rising, and Λ
    rising as the cost comes out of every outcome's wealth), never less than 1 / b so the scaling stays positive."""
    px, w, m = at.px[idx], at.w[idx], float((p / at.w**2).sum())
    slope = np.maximum(1 / at.b + (net - (1 + fee) * px) / w + ((1 + fee) * px * m - net * p[idx] / w**2) / at.lam, 1 / at.b)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log(at.worth[idx] / np.maximum(px, 1e-300)) / slope


# ── orders ──────────────────────────────────────────────────

def kelly_buys(book, probs, free: float, held: dict[int, tuple[float, float]] | None, sizing: Sizing) -> list[Leg]:
    """The legs to send on one close. `held` maps option id to (contracts, cost) already owned there; `free` is what
    the close may still spend. The full-Kelly position is held plus `optimum`; `sizing.kelly` of that whole, less what
    is held, is bought, so a position already at its fraction is not topped up. Each leg is sized with
    `policy.fill_to` to the dearest price that still returns `min_edge` on the fee-inclusive cost of its last
    contract, 98 · p / ((1 + fee) (1 + min_edge)), and to its planned cost, given every other leg of the order: the
    server prices the order as one, so a range's last contract costs its price once the whole order is in, which
    the legs beside it lower. The legs are passed over largest planned cost first, each refilled against the others,
    until a pass changes none of them. Contracts are floored to two decimals, the server's unit; legs under 0.01
    contracts are dropped (a leg's cost may be next to nothing, see below); the one-sat minimum commission is kept
    back from `free` when the order's 2% would come to less; and the order is sent only if `policy.worth_sending`
    agrees."""
    q = np.asarray(book.shares, dtype=float)
    p = np.maximum(np.asarray(probs, dtype=float), 0.0)
    n = len(q)
    if n == 0 or free < 0.01 or not np.all(np.isfinite(p)) or float(p.sum()) <= 0:
        return []
    p = p / float(p.sum())
    h = np.zeros(n)
    index = {o: i for i, o in enumerate(book.option_ids)}
    for option_id, (contracts, _) in (held or {}).items():
        i = index.get(option_id)
        if i is not None:
            h[i] += contracts
    room = free - max(0.0, P.MIN_FEE_SATS - P.FEE * free / (1 + P.FEE))     # what the minimum commission leaves to spend
    if room < 0.01:
        return []
    dq = optimum(q, book.alpha, p, h, room, sizing)
    want = np.floor(np.maximum(sizing.kelly * (h + dq) - h, 0.0) * 100) / 100
    picked = np.flatnonzero(want >= 0.01)
    if len(picked) == 0:
        return []
    quote = PL.Quote(list(q), book.alpha)
    planned = {int(i): (1 + P.FEE) * (quote.cost_after(int(i), float(want[i])) - quote.base) for i in picked}
    order = sorted(planned, key=lambda k: -planned[k])
    # Where capital is deep the cap binds on every leg at once, and refilling them one at a time closes the gap only a
    # fifth a pass. The book where every bought range sits exactly at its cap is this module's own bet-your-beliefs
    # optimum with no fee, a bottomless bankroll and the cap as the payout; the passes start from there, under `want`.
    capped = optimum(q, book.alpha, p, None, 1e12, Sizing(kelly=1.0, tol=sizing.tol, floor=0.0), fee=0.0,
                     net=PL.NET / ((1 + P.FEE) * (1 + sizing.min_edge)))
    got = {i: float(min(want[i], math.floor(capped[i] * 100) / 100)) for i in order}
    for i in order:
        if got[i]:
            quote.add(i, got[i])
    for _ in range(PASSES):
        changed = False
        for i in order:
            if got[i]:
                quote.add(i, -got[i])
            target = p[i] * PL.NET / ((1 + P.FEE) * (1 + sizing.min_edge))
            stake = (1 + P.FEE) * (quote.cost_after(i, float(want[i])) - quote.base) * 1.001 + 0.01   # its cost given the others
            d, _ = PL.fill_to(quote, book.alpha, i, target, stake)
            d = min(d, float(want[i]))
            d = d if d >= 0.01 else 0.0
            if d:
                quote.add(i, d)
            changed = changed or abs(d - got[i]) >= 0.005
            got[i] = d
        if not changed:
            break
    fresh = PL.Quote(list(q), book.alpha)                  # each leg's cost in order, summing to the order's cost
    legs: list[Leg] = []
    for i in order:
        d = got[i]
        if d < 0.01:
            continue
        before = fresh.base
        fresh.add(i, d)
        legs.append(Leg(i, d, max(fresh.base - before, 0.0) * (1 + P.FEE)))     # a leg may cost next to nothing: when the
    return legs if legs and PL.worth_sending(legs, p) else []                   # crowd has made one range certain, the
    # others sell for nothing, and thousands of contracts for a sat are the best trade on the ladder; worth_sending judges
    # the order as a whole, the one-sat minimum commission included.


def allot(bankroll: float, hours: list[float], power: float = 0.0) -> list[float]:
    """Per-close capital for closes `hours` out: bankroll · w_h / Σ w with w_h = max(h, 1/60)^(−power). Power 0 is an
    even share, what makes one bankroll cover every close without betting the same price path many times over; power 1
    gives a close an hour out twice what one two hours out gets. A close a minute or less away counts as a minute."""
    if not hours:
        return []
    w = [max(float(h), LEAST_HOURS) ** (-power) for h in hours]
    total = sum(w)
    return [bankroll * x / total for x in w]
