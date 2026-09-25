"""The opportunistic zoo and its trading rules: cheap ranges only, the bot's own region, small stakes, fees respected."""
import math

import pytest
from zoo_contract import check_model, ctx_for, synthetic_bars

from glimpse_tui import policy as PL
from glimpse_tui import pricing as P
from glimpse_tui.api import Book
from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import opportunist

BINS = [(60_000.0 + 200 * i, 60_200.0 + 200 * i) for i in range(200)]     # spot 80,000 sits in range 100
SPOT = 80_050.0


def book(shares) -> Book:
    b = Book(1, "m", 0, "live", 0, list(range(1, 201)), BINS, list(shares), P.alpha_for(200))
    b.reprice()
    return b


def flat_market() -> Book:
    """Nobody has traded: every range at the same seeded price, 1.5 sats on a 200-range ladder."""
    return book([10.0] * 200)


def crowded_market() -> Book:
    """Traders have piled into the four ranges around spot: everything else is left cheap."""
    return book([10 + 3000 * math.exp(-0.5 * ((i - 100) / 4) ** 2) for i in range(200)])


def bell(width: float = 15.0) -> list[float]:
    w = [math.exp(-0.5 * ((i - 100) / width) ** 2) + 1e-9 for i in range(200)]
    s = sum(w)
    return [x / s for x in w]


@pytest.mark.parametrize("m", opportunist.MODELS, ids=lambda m: m.id)
def test_every_opportunist_is_a_zoo_model_with_a_policy_and_its_own_runner_text(m):
    check_model(m)
    assert m.policy is not None and m.policy.region in PL.REGIONS
    runner = dict(C.explain(m))["runner"]
    assert "opportunistic" in runner and f"{m.policy.max_price:g} sats" in runner


def test_the_consensus_is_the_average_of_its_members_and_is_computed_once_per_close():
    from glimpse_tui import zoo
    ctx = ctx_for(synthetic_bars(), 5.0)
    got = opportunist.consensus(ctx)
    members = [zoo.get(i).fn(ctx_for(synthetic_bars(), 5.0)) for i in opportunist.CONSENSUS]
    assert got == pytest.approx(C.finish(sum(members) / len(members)), abs=1e-12)
    assert opportunist.consensus(ctx) is got                                   # cached for the next bot on this close


def test_a_policy_buys_only_cheap_ranges_in_its_region_worth_its_ratio():
    b = crowded_market()
    probs = bell()
    floor = min(b.prices)
    for region, keep in (("above", lambda i: BINS[i][0] >= SPOT), ("below", lambda i: BINS[i][1] <= SPOT),
                         ("near", lambda i: 89 <= i <= 111), ("up-tail", lambda i: i > 118), ("down-tail", lambda i: i < 82),
                         ("tails", lambda i: i < 82 or i > 118)):
        pol = PL.Policy(max_price=5, min_ratio=1.5, region=region)
        got = PL.picks(pol, probs, BINS, b.prices, SPOT)
        assert got and all(keep(i) for i in got), region
        assert all(probs[i] * PL.NET >= 1.5 * b.prices[i] * (1 + P.FEE) for i in got)
        assert got == sorted(got, key=lambda i: -probs[i] / b.prices[i])      # best odds first
    assert PL.picks(PL.Policy(max_price=floor / 2), probs, BINS, b.prices, SPOT) == []   # nothing that cheap


def test_opportunistic_orders_are_small_capped_and_not_repeated():
    b = crowded_market()
    probs = bell()
    pol = PL.Policy(max_price=5, min_ratio=1.5, region="any", stake=0.01, market_cap=0.10, max_ranges=10)
    legs = PL.decide(b, probs, pol, 20_000, 5_000, {}, 0.0, SPOT)
    assert 0 < len(legs) <= 10
    assert all(x.cost_sats <= 200 + 1e-6 for x in legs)                         # 1% of the budget per range
    assert sum(x.cost_sats for x in legs) <= 2_000 + 1e-6                       # 10% in one close
    for x in legs:                                                              # paid no more than the ratio allows, fees in
        assert x.cost_sats / x.contracts <= probs[x.index] * PL.NET / 1.5 + 1e-6
    held = {b.option_ids[x.index]: (x.contracts, x.cost_sats) for x in legs}
    again = PL.decide(b, probs, pol, 20_000, 5_000, held, sum(x.cost_sats for x in legs), SPOT)
    assert not {x.index for x in again} & {x.index for x in legs if x.cost_sats >= 199}   # a full range is not topped up


def test_an_order_the_commission_would_eat_is_not_sent():
    probs = bell()
    tiny = [PL.Leg(100, 0.05, 0.2)]                                              # 0.2 sats of stake, 1 sat minimum fee
    assert not PL.worth_sending(tiny, probs)
    assert PL.worth_sending([PL.Leg(100, 10.0, 20.0)], probs)


def test_selling_trims_an_overpaid_range_back_to_fair_value():
    shares = [10.0] * 200
    shares[100] = 300.0                                                         # the market has bid range 100 up
    b = book(shares)
    probs = bell()
    value = probs[100] * PL.NET
    assert (1 - P.FEE) * b.prices[100] > value * 1.05
    held = {b.option_ids[100]: (500.0, 1_000.0)}
    ((i, x, proceeds),) = PL.sell_down(b, probs, held, 0.05)
    assert i == 100 and 0 < x < 300                                             # part of it, not all
    q = shares[:]
    q[100] -= x
    assert (1 - P.FEE) * P.prices(q, b.alpha)[100] == pytest.approx(value * 1.05, rel=0.01)   # stops at fair value
    assert proceeds > value * x
    assert PL.sell_down(flat_market(), probs, held, 0.05) == []            # a fair or cheap market: nothing to sell


def test_the_fast_quote_prices_exactly_as_the_ls_lmsr_formula_even_selling_nearly_everything():
    import numpy as np
    rng = np.random.default_rng(5)
    for _ in range(60):
        n = int(rng.choice([50, 200, 500]))
        sh = list(rng.uniform(5, 60, n))
        k = int(rng.integers(n))
        sh[k] += float(rng.uniform(0, 5000))                                   # one crowded range, the underflow case
        a, q = P.alpha_for(n), PL.Quote(sh, P.alpha_for(n))
        for d in (-sh[k] * 0.999, -sh[k] * 0.5, -1.0, 0.0, 3.0, 400.0, 4000.0):
            q2 = sh[:]
            q2[k] += d
            assert q.price_after(k, d) == pytest.approx(P.prices(q2, a)[k], abs=1e-9)
            assert q.cost_after(k, d) == pytest.approx(P.cost(q2, a), rel=1e-12)


def test_the_chart_spreads_each_range_over_its_rows_as_the_plain_loop_does():
    import numpy as np

    from glimpse_tui.botsview import row_mass

    def plain(probs, bins, hi, step, rows):
        out, lo = [0.0] * rows, hi - rows * step
        for p, (a, b) in zip(probs, bins, strict=True):
            if p <= 0 or b <= lo or a >= hi or not b > a:
                continue
            for r in range(int((hi - min(b, hi)) // step), min(int((hi - max(a, lo)) // step), rows - 1) + 1):
                out[r] += p * max(min(b, hi - r * step) - max(a, hi - (r + 1) * step), 0.0) / (b - a)
        return out

    rng = np.random.default_rng(3)
    for t in range(400):
        n, w = int(rng.integers(2, 60)), float(rng.choice([50.0, 200.0, 1000.0]))
        bins = [(w * i, w * (i + 1)) for i in range(n)]
        if t % 5 == 0:
            bins[1] = (bins[1][0], bins[1][0])                                   # an empty range
        if t % 7 == 0:
            bins = [(a + 300 * (i > n // 2), b + 300 * (i > n // 2)) for i, (a, b) in enumerate(bins)]   # a gap
        p = rng.dirichlet(np.ones(n))
        rows, hi, step = int(rng.integers(3, 25)), float(rng.uniform(0, w * n * 1.3)), float(rng.uniform(10, w * n / 4))
        assert row_mass(p, bins, hi, step, rows) == pytest.approx(plain(p, bins, hi, step, rows), abs=1e-12)
