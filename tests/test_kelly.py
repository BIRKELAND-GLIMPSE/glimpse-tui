"""Kelly sizing on one close: pure maths against the fixture ladder and a flat 500-range book. No network, no orders."""
import json
import time
from pathlib import Path

import numpy as np
import pytest

from glimpse_tui import kelly as K
from glimpse_tui import policy as PL
from glimpse_tui import pricing as P
from glimpse_tui.api import Book, parse_bin

FIX = json.loads((Path(__file__).parent / "fixtures" / "estimates.json").read_text())
BINS = [parse_bin(n) for n in FIX["names"]]
MIDS = np.array([(lo + hi) / 2 for lo, hi in BINS])
ALPHA = P.alpha_for(500)
FULL = K.Sizing(kelly=1.0)


def bell(center: float, width: float = 1_500) -> np.ndarray:
    """A bell over the ladder, as test_bots' bell bot forecasts, with the same 1e-12 floor."""
    w = np.exp(-0.5 * ((MIDS - center) / width) ** 2) + 1e-12
    return w / w.sum()


def flat(shares: float = 80.0) -> np.ndarray:
    """A freshly seeded hourly close: every range at the same shares."""
    return np.full(500, shares)


def book(q) -> Book:
    b = Book(1, "m", 0, "live", 0, list(range(1, 501)), BINS, list(map(float, q)), ALPHA)
    b.reprice()
    return b


def spent(legs: list[PL.Leg]) -> float:
    return sum(x.cost_sats for x in legs)


def moved(q, legs: list[PL.Leg]) -> np.ndarray:
    q = np.array(q, dtype=float)
    for x in legs:
        q[x.index] += x.contracts
    return q


# ── the maths ───────────────────────────────────────────────

def test_gradient_and_hessian_are_the_derivatives_of_growth():
    q, p = np.array(FIX["shares"], dtype=float), bell(78_000)
    dq = np.zeros(500)
    dq[70:85] = 3.0
    held = np.zeros(500)
    held[75] = 2.0
    g = K.gradient(q, ALPHA, p, held, 1e5, dq)
    h = K.hessian(q, ALPHA, p, held, 1e5, dq)
    eps = 1e-5
    for k in (70, 74, 77, 84, 90):
        e = np.zeros(500)
        e[k] = eps
        fd = (K.growth(q, ALPHA, p, held, 1e5, dq + e) - K.growth(q, ALPHA, p, held, 1e5, dq - e)) / (2 * eps)
        assert g[k] == pytest.approx(fd, rel=1e-4)
        fdh = (K.gradient(q, ALPHA, p, held, 1e5, dq + e) - K.gradient(q, ALPHA, p, held, 1e5, dq - e)) / (2 * eps)
        assert np.allclose(h[k, 60:95], fdh[60:95], rtol=1e-3, atol=1e-12 * np.abs(h).max())
    assert np.allclose(h, h.T)
    assert np.all(np.linalg.eigvalsh(h) < 0)                   # concave: the hill has one top


def test_the_vectorised_prices_and_cost_match_pricing():
    q = np.array(FIX["shares"], dtype=float)
    px, c, *_ = K._book(q, ALPHA)
    assert np.allclose(px, P.prices(list(q), ALPHA), rtol=1e-12, atol=1e-12)
    assert c == pytest.approx(P.cost(list(q), ALPHA), rel=1e-12)
    w = K.wealth(q, ALPHA, bell(78_000), None, 1000.0, np.zeros(500))
    assert np.all(w == 1000.0)
    assert K.growth(q, ALPHA, bell(78_000), None, 1.0, np.full(500, 50.0)) == -np.inf     # spends more than it has


def test_a_market_that_already_shows_the_picture_gets_no_order():
    """probs = the book's own implied probabilities: no range pays more than it costs, so nothing is bought."""
    for q in (flat(), np.array(FIX["shares"], dtype=float)):
        b = book(q)
        assert not np.any(K.optimum(q, ALPHA, b.probs, None, 1e5, FULL))
        assert K.kelly_buys(b, b.probs, 1e5, {}, K.Sizing()) == []
        breakeven = np.array(b.prices) * (1 + P.FEE) / PL.NET         # the probability at which each range is fair
        assert not np.any(K.optimum(q, ALPHA, breakeven / breakeven.sum(), None, 1e5, FULL))


def test_bet_your_beliefs_with_deep_capital_and_no_fee():
    """Full Kelly, no fees, a trillion sats: every range bought ends priced at 100 · p_i, every range left alone was
    already priced at or above it. The public forecast shows the private model."""
    q, p = flat(), bell(78_000)
    dq = K.optimum(q, ALPHA, p, None, 1e12, K.Sizing(kelly=1.0, tol=1e-9, floor=0.0), fee=0.0, net=P.PAYOUT_SATS)
    px = np.array(P.prices(list(q + dq), ALPHA))
    bought = dq > 0.01
    assert 5 <= bought.sum() <= 20
    assert np.allclose(px[bought], 100 * p[bought], rtol=1e-3)
    assert np.all(px[~bought] >= 100 * p[~bought] - 1e-6)


def test_with_the_real_fee_and_edge_no_bought_range_is_priced_past_its_value():
    """Every leg's last contract still returns min_edge on its fee-inclusive cost once the whole order is in."""
    for q in (flat(), np.array(FIX["shares"], dtype=float)):
        b, p = book(q), bell(78_000)
        legs = K.kelly_buys(b, p, 1e5, {}, K.Sizing())
        assert legs
        px = P.prices(list(moved(q, legs)), ALPHA)
        for x in legs:
            assert px[x.index] * (1 + P.FEE) * (1 + 0.02) <= PL.NET * p[x.index] + 1e-6


def test_the_optimum_spends_within_its_means_and_beats_every_other_feasible_order():
    rng = np.random.default_rng(7)
    q, p = np.array(FIX["shares"], dtype=float), bell(77_000, 2_000)
    for free in (500.0, 5_000.0, 50_000.0):
        dq = K.optimum(q, ALPHA, p, None, free, FULL)
        cost = (1 + P.FEE) * (P.cost(list(q + dq), ALPHA) - P.cost(list(q), ALPHA))
        assert 0 < cost <= free
        best = K.growth(q, ALPHA, p, None, free, dq)
        assert best >= K.growth(q, ALPHA, p, None, free, np.zeros(500))
        for _ in range(20):
            other = rng.random(500) * (rng.random(500) < 0.05)       # a few random ranges
            other *= rng.random() * free / max((1 + P.FEE) * (P.cost(list(q + other), ALPHA) - P.cost(list(q), ALPHA)), 1e-9)
            assert K.growth(q, ALPHA, p, None, free, other) <= best + 1e-9


def test_more_capital_never_spends_less_and_a_smaller_fraction_spends_less():
    b, p = book(flat()), bell(78_000)
    spends = [spent(K.kelly_buys(b, p, free, {}, FULL)) for free in (200.0, 500.0, 1_000.0, 3_000.0, 10_000.0, 1e5)]
    assert spends == sorted(spends) and spends[0] > 0
    quarter, half, full = (spent(K.kelly_buys(b, p, 5_000.0, {}, K.Sizing(kelly=k))) for k in (0.25, 0.5, 1.0))
    assert 0 < quarter < half < full


def test_what_is_held_is_the_stake_not_a_top_up():
    """Buy once; hold exactly that with the book moved by it; the next call finds nothing worth adding."""
    b, p = book(flat()), bell(78_000)
    first = K.kelly_buys(b, p, 2_000.0, {}, K.Sizing())
    assert first
    held = {b.option_ids[x.index]: (x.contracts, x.cost_sats) for x in first}
    again = K.kelly_buys(book(moved(b.shares, first)), p, 2_000.0 - spent(first), held, K.Sizing())
    assert again == [] or spent(again) < 0.01 * spent(first)


def test_every_leg_is_sent_in_the_server_s_unit_and_keeps_its_edge_in_the_whole_order():
    """The server prices the order as one, so a leg's last contract costs its price once every leg is in: that price
    still clears min_edge for every leg; the leg costs sum to the order's cost in the order sent; contracts in hundredths."""
    b, p = book(np.array(FIX["shares"], dtype=float)), bell(78_000)
    legs = K.kelly_buys(b, p, 3_000.0, {}, K.Sizing(min_edge=0.05))
    assert legs and PL.worth_sending(legs, p)
    alone = PL.Quote(b.shares, ALPHA)
    planned = [alone.cost_after(x.index, x.contracts) - alone.base for x in legs]    # each leg priced alone on the book
    assert all(a >= 0.9 * b for a, b in zip(planned[:-1], planned[1:], strict=True))  # the big legs go first
    after = P.prices(list(moved(b.shares, legs)), ALPHA)
    quote = PL.Quote(b.shares, ALPHA)
    for x in legs:
        assert x.contracts >= 0.01 and x.cost_sats >= 0.01
        assert round(x.contracts * 100) == pytest.approx(x.contracts * 100)
        assert after[x.index] * (1 + P.FEE) * 1.05 <= PL.NET * p[x.index] + 1e-6
        c = quote.cost_after(x.index, x.contracts)
        assert x.cost_sats == pytest.approx((1 + P.FEE) * (c - quote.base), rel=1e-9)
        quote.add(x.index, x.contracts)
    assert spent(legs) == pytest.approx((1 + P.FEE) * (P.cost(list(moved(b.shares, legs)), ALPHA) - P.cost(b.shares, ALPHA)), rel=1e-9)


def test_one_order_buys_the_whole_position_whatever_binds():
    """Capped alone on the fresh book a leg buys half of what it should, since the legs beside it lower its price once
    they are in; sized against the whole order, the first call leaves the second nothing, at every fraction, on a real
    book, with capital so deep that the min_edge cap binds on every leg and with capital that runs out first."""
    for q in (flat(), np.array(FIX["shares"], dtype=float)):
        for kelly in (1.0, 0.5):
            for free in (2_000.0, 20_000.0):
                b, p = book(q), bell(78_000)
                first = K.kelly_buys(b, p, free, {}, K.Sizing(kelly=kelly))
                assert first
                held = {b.option_ids[x.index]: (x.contracts, x.cost_sats) for x in first}
                again = K.kelly_buys(book(moved(q, first)), p, free - spent(first), held, K.Sizing(kelly=kelly))
                assert again == [] or spent(again) < 0.01 * spent(first)
                after = P.prices(list(moved(q, first)), ALPHA)
                for x in first:
                    assert after[x.index] * (1 + P.FEE) * 1.02 <= PL.NET * p[x.index] + 1e-6
    # SOL's 650 ranges are priced with the 500-range alpha; the cap holds there too
    n, a = 650, P.alpha_for(650)
    mids = np.arange(n) + 0.5
    p = np.exp(-0.5 * ((mids - n / 2) / 5) ** 2) + 1e-12
    p /= p.sum()
    sol = Book(1, "m", 0, "live", 0, list(range(1, n + 1)), [(i, i + 1) for i in range(n)], [80.0] * n, a)
    sol.reprice()
    legs = K.kelly_buys(sol, p, 1e4, {}, FULL)
    assert len(legs) > 10
    after = P.prices(list(moved(sol.shares, legs)), a)
    assert all(after[x.index] * (1 + P.FEE) * 1.02 <= PL.NET * p[x.index] + 1e-6 for x in legs)


def test_the_one_sat_minimum_commission_is_kept_back_and_bad_pictures_are_refused():
    """An order whose 2% comes to under a sat is charged a sat: the real charge still fits `free`. A picture with a
    NaN is no picture; negative entries count as zero."""
    b, p = book(flat()), bell(78_000)
    assert K.kelly_buys(b, p, 0.5, {}, FULL) == []
    for free in (1.5, 10.0, 30.0, 49.0, 60.0):
        legs = K.kelly_buys(b, p, free, {}, FULL)
        assert legs
        raw = spent(legs) / (1 + P.FEE)
        assert raw + max(P.FEE * raw, P.MIN_FEE_SATS) <= free + 1e-9
    nan = p.copy()
    nan[3] = np.nan
    assert K.kelly_buys(b, nan, 1e4, {}, FULL) == []
    neg = p.copy()
    neg[200:300] = -0.5
    clipped = np.maximum(neg, 0.0)
    assert [(x.index, x.contracts) for x in K.kelly_buys(b, neg, 1e4, {}, FULL)] == \
        [(x.index, x.contracts) for x in K.kelly_buys(b, clipped, 1e4, {}, FULL)]


def test_a_sure_thing_buys_one_range_hard_but_never_past_its_value():
    p = np.full(500, 1e-9)
    p[90] = 1 - 499e-9
    b = book(flat())
    legs = K.kelly_buys(b, p, 10_000.0, {}, FULL)
    assert [x.index for x in legs] == [90]
    px = P.prices(list(moved(b.shares, legs)), ALPHA)
    assert 60 < px[90] <= PL.NET / ((1 + P.FEE) * 1.02) + 1e-9


def test_nothing_to_spend_or_no_picture_means_no_order():
    b = book(flat())
    assert K.kelly_buys(b, bell(78_000), 0.0, {}, FULL) == []
    assert K.kelly_buys(b, np.zeros(500), 1e5, {}, FULL) == []
    assert not np.any(K.optimum(b.shares, ALPHA, bell(78_000), None, 0.0, FULL))
    held = {b.option_ids[i]: (1.0, 5.0) for i in range(60, 100)}      # already long the whole bell
    assert spent(K.kelly_buys(b, bell(78_000), 100.0, held, K.Sizing())) < 100.0


def test_the_optimum_is_fast_and_deterministic():
    q, p = flat(), bell(78_000)
    times = []
    for _ in range(5):
        t = time.perf_counter()
        a = K.optimum(q, ALPHA, p, None, 1e5, FULL)
        times.append(time.perf_counter() - t)
    b = K.optimum(q, ALPHA, p, None, 1e5, FULL)
    assert np.array_equal(a, b)
    print(f"\noptimum over 500 ranges: {min(times) * 1e3:.2f} ms best of 5, {max(times) * 1e3:.2f} ms worst")
    assert min(times) < 0.25
    for free in (300.0, 1_000.0, 5_000.0):                         # a thin bankroll against a confident bell: the wall cases
        t = time.perf_counter()
        K.optimum(q, ALPHA, p, None, free, FULL)
        assert time.perf_counter() - t < 0.25


def test_allot_shares_one_bankroll_across_the_closes():
    even = K.allot(1_000.0, [0.5, 1.5, 2.5, 3.5])
    assert sum(even) == pytest.approx(1_000.0) and len(set(round(x, 9) for x in even)) == 1
    near = K.allot(1_000.0, [0.5, 1.5, 2.5, 3.5], power=1.0)
    assert sum(near) == pytest.approx(1_000.0) and near == sorted(near, reverse=True)
    assert near[0] == pytest.approx(3 * near[1])                    # an hour and a half out gets a third of half an hour out
    zero = K.allot(1_000.0, [0.0, 1.0], power=1.0)
    assert sum(zero) == pytest.approx(1_000.0) and zero[0] > zero[1]
    assert K.allot(1_000.0, []) == []
