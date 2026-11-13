"""The Triangulator: the triangulation maths, the runner against a fake exchange of the three series, and the advanced
screen. No network, no key, no orders."""
import numpy as np
import pytest
from test_arb import GOLD, GOLD_SPOT, HOURLY, SPOT, XAUT, XAUT_SPOT, Exchange, bell, book, edges, feeds, flat, sharp
from test_bots import until

from glimpse_tui import advview, arb, auth, bots, botsview, tri
from glimpse_tui import app as A
from glimpse_tui import policy as PL
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, MarketRow

BASIS = XAUT_SPOT * SPOT / (GOLD_SPOT * 1e8)          # 1 in the fixture: Tether Gold at PAX Gold
T = tri.TRIANGLE


def gold_bell(bins=GOLD, c=GOLD_SPOT, sd=80.0):
    return bell(bins, c, sd)


# ── the maths ───────────────────────────────────────────────

def test_the_identity_runs_both_ways_and_the_basis_is_read_from_the_spots():
    spots = {"BTC": SPOT, "XAU": GOLD_SPOT, "XAUTBTC": XAUT_SPOT}
    assert T.basis(spots) == pytest.approx(1.0)
    assert T.implied_price("XAUTBTC", spots, 1.0) == pytest.approx(XAUT_SPOT)
    assert T.implied_price("XAU", spots, 1.0) == pytest.approx(GOLD_SPOT)
    assert T.implied_price("BTC", spots, 1.0) == pytest.approx(SPOT)
    assert T.basis({"BTC": SPOT, "XAU": GOLD_SPOT, "XAUTBTC": XAUT_SPOT * 0.998}) == pytest.approx(0.998)
    assert T.basis({"BTC": SPOT, "XAU": 0.0, "XAUTBTC": XAUT_SPOT}) == 0.0
    assert T.others("XAU") == ("BTC", "XAUTBTC") and T.terms("XAUTBTC") == (("XAU", 1), ("BTC", -1))


def test_two_legs_imply_the_third_through_the_copula_and_a_tight_pair_implies_a_tight_third():
    rng = np.random.default_rng(1)
    pb, pg = bell(HOURLY, SPOT, 1_500), gold_bell()
    imp = tri.implied(edges(XAUT), [(pg, edges(GOLD), +1), (pb, edges(HOURLY), -1)], T.constant("XAUTBTC", 1.0), 0.2, rng)
    assert len(imp) == 500 and sum(imp) == pytest.approx(1.0) and min(imp) > 0
    assert abs(tri.median(XAUT, imp) / XAUT_SPOT - 1) < 0.003                     # centred where gold over Bitcoin says
    lo, hi = P.quantile(XAUT, imp, 0.1), P.quantile(XAUT, imp, 0.9)
    assert 0.03 < (hi - lo) / XAUT_SPOT < 0.12                                     # as wide as the two legs' moves together
    # gold in dollars from gold in sats and Bitcoin: they move against each other, so the product barely moves
    px = bell(XAUT, XAUT_SPOT, 120_000)
    tight = tri.implied(edges(GOLD), [(px, edges(XAUT), +1), (pb, edges(HOURLY), +1)], T.constant("XAU", 1.0), -0.95, rng)
    loose = tri.implied(edges(GOLD), [(px, edges(XAUT), +1), (pb, edges(HOURLY), +1)], T.constant("XAU", 1.0), 0.0, rng)
    width = lambda p: P.quantile(GOLD, p, 0.9) - P.quantile(GOLD, p, 0.1)       # noqa: E731
    assert abs(tri.median(GOLD, tight) / GOLD_SPOT - 1) < 0.003 and width(tight) < 0.6 * width(loose)
    # what lands past the ladder goes in its end ranges, and a ladder starting at 0 still has a log width
    far = tri.implied(edges(HOURLY), [(bell(GOLD, 7_900.0, 20), edges(GOLD), +1), (bell(XAUT, 2_600_000.0, 20_000), edges(XAUT), -1)],
                      T.constant("BTC", 1.0), 0.0, rng)                          # gold dear and gold in sats cheap: Bitcoin past 125k
    assert far[-1] > 0.5
    assert np.isfinite(tri.log_edges([0.0, 1_000.0, 2_000.0])).all()


def test_the_pool_leans_on_the_implied_picture_where_the_other_books_have_formed_and_this_one_has_not():
    assert tri.weight(0.0, 0.8) > 0.9 and tri.weight(0.8, 0.0) < 0.1 and tri.weight(0.5, 0.5) == pytest.approx(0.5)
    assert tri.weight(0.0, 0.0) == pytest.approx(0.5) and tri.weight(0.0, 0.8, cross_weight=2.0) > 1.8
    rng = np.random.default_rng(2)
    imp = tri.implied(edges(XAUT), [(gold_bell(), edges(GOLD), +1), (bell(HOURLY, SPOT, 1_500), edges(HOURLY), -1)],
                      T.constant("XAUTBTC", 1.0), 0.2, rng)
    own = [1 / 500] * 500                                                          # a lens with no view: the flat ladder
    pooled = tri.pooled(own, imp, tri.weight(0.0, 0.8))
    near = sum(p for p, (lo, hi) in zip(pooled, XAUT, strict=True) if abs((lo + hi) / 2 / XAUT_SPOT - 1) < 0.03)
    assert near > 0.5 and sum(pooled) == pytest.approx(1.0)
    assert tri.pooled(own, imp, 0.0) == pytest.approx(own)                        # no weight: the own picture stands
    assert tri.pooled(imp, imp, 1.0) == pytest.approx(imp)


def test_the_band_is_bought_as_equal_contracts_and_the_fill_stops_where_the_last_set_no_longer_returns_the_edge():
    bk = book(1, XAUT, flat(XAUT))                                                 # a flat sats book: every range at the seed price
    pic = bell(XAUT, XAUT_SPOT, 60_000)
    lo, hi = tri.band(pic, 0.8)
    p = sum(pic[lo:hi + 1])
    assert 0.8 <= p < 0.85 and XAUT[lo][0] < XAUT_SPOT < XAUT[hi][1]
    price0 = tri.set_price(bk.shares, bk.alpha, lo, hi)
    assert price0 == pytest.approx(sum(bk.prices[lo:hi + 1])) and tri.pays(p, price0) > 3   # the flat book sells the band for little
    d, cost = tri.band_fill(bk, lo, hi, p, 5_000.0, 0.25)
    assert d >= 0.01 and 0 < cost <= 5_000
    target = PL.NET * p / ((1 + P.FEE) * 1.25)
    assert tri.set_price(bk.shares, bk.alpha, lo, hi, d) <= target < tri.set_price(bk.shares, bk.alpha, lo, hi, d + 0.02) or \
        tri.set_cost(bk.shares, bk.alpha, lo, hi, d + 0.02) > 5_000                   # the marginal edge or the stake stops it
    assert cost == pytest.approx(tri.set_cost(bk.shares, bk.alpha, lo, hi, d))
    legs = tri.band_legs(bk, lo, hi, d, cost)
    assert [x.index for x in legs] == list(range(lo, hi + 1)) and all(x.contracts == d for x in legs)
    assert sum(x.cost_sats for x in legs) == pytest.approx(cost) and PL.worth_sending(legs, pic)
    # a stake too small for a hundredth of a contract, or a band already dearer than it pays, buys nothing
    assert tri.band_fill(bk, lo, hi, p, 0.5, 0.25) == (0.0, 0.0)
    dear = book(2, XAUT, sharp(XAUT, XAUT_SPOT, 1.0, 2000))
    lo2, hi2 = tri.band(pic, 0.8)
    assert tri.set_price(dear.shares, dear.alpha, lo2, hi2) > PL.NET * p / 1.02 and tri.band_fill(dear, lo2, hi2, p, 5_000.0, 0.0) == (0.0, 0.0)
    # Kelly on the band as one bet: more when it pays more, nothing when it does not pay
    assert tri.band_stake(0.8, 30.0, 0.25, 100_000) > tri.band_stake(0.8, 60.0, 0.25, 100_000) > 0 == tri.band_stake(0.8, 90.0, 0.25, 100_000)


def test_the_session_walk_knows_the_weekend_and_is_a_baseline_model_of_the_zoo():
    from datetime import UTC, datetime

    import pandas as pd
    from zoo_contract import check_model, ctx_for, synthetic_bars

    from glimpse_tui import clock, zoo

    m = zoo.get("session_walk")
    assert m is not None and m.family == "Baseline" and m.id in {b.id for b in bots.discover() if b.family in tri.LENS_FAMILIES}
    check_model(m)
    assert clock.session_of("XAU") == clock.session_of("WTIBTC") == "cme" and clock.session_of("BTC") == "always"
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()      # noqa: E731
    rng = np.random.default_rng(3)
    idx = pd.date_range("2026-08-01", periods=2000, freq="h", tz="UTC")
    r = np.where([clock.cme_open(t.timestamp()) for t in idx], rng.normal(0, 0.003, 2000), rng.normal(0, 0.0005, 2000))
    bars = pd.DataFrame({"close": 4_000 * np.exp(np.cumsum(r))}, index=idx)
    gold = clock.Clock.fit(bars, "cme")
    sat, wed = at("2026-10-31 12:00"), at("2026-10-28 12:00")
    assert gold.sigma_to(sat, sat + 6 * 3600) < 0.3 * gold.sigma_to(wed, wed + 6 * 3600)           # the weekend barely moves
    assert gold.hours_open(at("2026-10-30 20:00"), at("2026-10-31 02:00")) == pytest.approx(2.0)   # Friday 20:00 to 22:00
    edges_ = np.array([3_000.0 + 10 * i for i in range(501)])
    weekend = gold.picture(3_945.0, gold.sigma_to(sat, sat + 6 * 3600), edges_)
    weekday = gold.picture(3_945.0, gold.sigma_to(wed, wed + 6 * 3600), edges_)
    assert sum(weekend) == pytest.approx(1.0) and max(weekend) > 2 * max(weekday)
    ctx = ctx_for(synthetic_bars(), 6.0)
    ctx.asset = "XAU"
    assert "session_clock" not in ctx.cache and len(m.fn(ctx)) == 500 and ctx.cache["session_clock"].session == "cme"


def test_correlation_reads_the_feeds_hourly_returns_and_needs_shared_history():
    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=1500, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    b = rng.normal(0, 0.004, 1500)
    g = 0.3 * b + rng.normal(0, 0.002, 1500)
    btc = pd.DataFrame({"close": 80_000 * np.exp(np.cumsum(b))}, index=idx)
    gold = pd.DataFrame({"close": 4_000 * np.exp(np.cumsum(g))}, index=idx)
    xaut = pd.DataFrame({"close": gold["close"].to_numpy() * 1e8 / btc["close"].to_numpy()}, index=idx)
    assert 0.35 < tri.correlation(btc, gold) < 0.65
    assert tri.correlation(btc, xaut) < -0.7
    assert tri.correlation(btc.iloc[:100], gold.iloc[:100]) == 0.0


def test_rules_come_from_a_triangle_table_in_bots_toml():
    d = auth.config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "bots.toml").write_text('interval_s = 9\n[triangle]\nlens = "benter"\ninstant_share = 0.05\ncovers = false\nnonsense = 1\n')
    r = tri.Rules.load()
    assert r.lens == "benter" and r.instant_share == 0.05 and r.covers is False and r.cross_weight == tri.Rules().cross_weight
    assert r.band_mass == 0.8 and r.max_hours == 48 and r.cover_edge == 0.05 and tri.Rules().lens == "session_walk"
    assert bots.BotConfig.load().interval_s == 9 and arb.Rules.load() == arb.Rules()


# ── the oil leg ─────────────────────────────────────────────

def test_oil_trades_on_cmes_week_and_the_clock_adds_up_the_hours_at_their_own_sigma():
    from datetime import UTC, datetime

    import pandas as pd

    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()      # noqa: E731
    assert tri.oil_open(at("2026-10-28 15:00")) and not tri.oil_open(at("2026-10-28 22:00"))      # Wednesday, and its daily break
    assert not tri.oil_open(at("2026-10-30 22:30")) and tri.oil_open(at("2026-10-30 21:30"))      # Friday evening closes
    assert not tri.oil_open(at("2026-10-31 12:00")) and not tri.oil_open(at("2026-11-01 20:00"))  # Saturday, Sunday afternoon
    assert tri.oil_open(at("2026-11-01 23:30")) and tri.oil_open(at("2026-11-02 03:00"))           # Sunday night reopens
    rng = np.random.default_rng(4)
    idx = pd.date_range("2026-08-01", periods=2000, freq="h", tz="UTC")
    is_open = np.array([tri.oil_open(t.timestamp()) for t in idx])
    r = np.where(is_open, rng.normal(0, 0.005, 2000), rng.normal(0, 0.0005, 2000))
    bars = pd.DataFrame({"close": 100 * np.exp(np.cumsum(r))}, index=idx)
    clock = tri.Clock.fit(bars, "cme")
    assert 0.004 < clock.s_open < 0.006 and clock.s_closed < 0.001 and clock.nu > 2.5 and clock.session == "cme"
    assert "CME open" in clock.describe(bars.index[-1].timestamp() + 1800, bars.index[-1].timestamp() + 5 * 3600)
    every = tri.Clock.fit(bars)                                                  # a market that never closes: quiet at the weekend
    assert every.session == "always" and every.s_closed < every.s_open and "weekdays" in every.describe(0.0, 3600.0)
    wed = at("2026-10-28 10:00")
    sat = at("2026-10-31 10:00")
    assert clock.sigma_to(wed, wed + 4 * 3600) == pytest.approx(clock.s_open * 2, rel=0.01)       # four open hours
    assert clock.sigma_to(sat, sat + 4 * 3600) == pytest.approx(clock.s_closed * 2, rel=0.01)     # four closed hours
    assert clock.sigma_to(wed, wed + 1800) == pytest.approx(clock.s_open * np.sqrt(0.5), rel=0.01)   # a partial step
    m = tri.Synthetic(102.0, 0.02, clock.nu)
    u = np.linspace(0.001, 0.999, 999)
    draws = np.exp(m.draw(u))
    assert np.median(draws) == pytest.approx(102.0, rel=1e-3) and 0.015 < np.std(np.log(draws)) < 0.03
    bins = m.ladder()
    pic = m.picture(bins)
    assert len(bins) == 60 and sum(pic) == pytest.approx(1.0) and abs(tri.median(bins, pic) / 102 - 1) < 0.002
    assert bins[0][0] == pytest.approx(102 * np.exp(-0.08)) and bins[-1][1] == pytest.approx(102 * np.exp(0.08))


def test_the_oil_triangle_has_a_synthetic_quote_leg_whose_basis_is_one():
    t = tri.OIL
    assert t.legs == ("BTC", "WTI", "WTIBTC") and t.series == ("BTC", "WTIBTC") and t.synthetic == "WTI"
    assert t.basis({"BTC": SPOT, "WTI": 103.2, "WTIBTC": 103.2e8 / SPOT}) == pytest.approx(1.0)
    assert t.implied_price("WTIBTC", {"BTC": SPOT, "WTI": 103.2}, 1.0) == pytest.approx(103.2e8 / SPOT)
    assert arb.OIL.series == ("BTC", "WTIBTC") and tri.triangle_of(type("B", (), {"arbitrage": "oil"})()) is t
    assert tri.triangle_of(type("B", (), {"arbitrage": "bitcoin"})()) is None
    found = {b.id: b for b in bots.discover()}
    b = found["triangle_oil"]
    assert b.arbitrage == "oil" and b.family == "Arbitrage" and arb.scheme_of(b) is arb.OIL
    note = dict(botsview.sections(b))["runner"]
    assert "a model on the price those two feeds imply" in note and "CME" in note and "every leg with a market" in note


def test_a_synthetic_leg_feeds_the_copula_as_a_sampler():
    rng = np.random.default_rng(5)
    oil = tri.Synthetic(103.2, 0.01, 5.0)
    pb = bell(HOURLY, SPOT, 1_500)
    WTI_BINS = [(25_000.0 + 1000 * i, 26_000.0 + 1000 * i) for i in range(250)]
    imp = tri.implied(edges(WTI_BINS), [(oil, None, +1), (pb, edges(HOURLY), -1)], tri.OIL.constant("WTIBTC", 1.0), 0.0, rng)
    assert sum(imp) == pytest.approx(1.0) and abs(tri.median(WTI_BINS, imp) / (103.2e8 / SPOT) - 1) < 0.01


async def test_the_oil_triangulator_reads_two_books_derives_oil_from_their_feeds_and_buys_the_flat_sats_book(tmp_path):
    api = Exchange()
    r = tri.Triangulator(bot=bots.Bot("triangle_oil", "Oil Triangulator", "Arbitrage", "oil", "test", forecast=lens_bot().forecast,
                                      arbitrage="oil"),
                         api=api, batch_id="", feed=None, make_feed=feeds, lens=lens_bot(), ledger=bots.Ledger(tmp_path / "oil.json"),
                         cfg=bots.BotConfig(interval_s=0, objective="edge", kelly=0.25, min_edge=0.25,
                                            edge_close_share=0.03).with_budget(100_000),
                         tri=tri.Rules(samples=4_000, covers=False))
    assert r.triangle is tri.OIL and r.scheme is arb.OIL and r.series == "BTC · WTIBTC"
    await r.cycle()
    assert api.read[:2] == [101, 301] and r.instants == [api.e1, api.e1 + 3600]
    v = r.last
    assert v is not None and list(v.sides) == ["BTC", "WTI", "WTIBTC"] and v.basis == pytest.approx(1.0)
    oil = v.sides["WTI"]
    assert oil.synthetic and oil.book.topic_id == -1 and oil.spot == pytest.approx(103.2) and "CME open" in oil.note
    assert oil.final is oil.own and oil.implied is None and not oil.buys and not oil.held
    w = v.sides["WTIBTC"]
    assert w.formed < 0.05 and w.weight > 0.8 and abs(tri.median(w.book.bins, w.implied) / (103.2e8 / SPOT) - 1) < 0.01
    assert w.buys and 301 in {x[2] for x in r.holdings()} and -1 not in {x[2] for x in r.holdings()}
    assert 0 < r.open_cost <= 10_000 + 1e-6 and r.trades == 1
    log = "\n".join(r.log)
    assert "BTC·WTI·WTIBTC at" in log and "WTI model σ" in log and "would buy" in log and "WTIBTC" in log.split("would buy")[1]
    assert abs(v.gaps["final"]) < 0.02
    await r.cycle()
    await r.cycle()
    await r.cycle()
    assert r.last.end == api.e1 + 3600 and api.read[-2:] == [102, 302]


def test_the_triangulator_is_a_bot_of_its_own_on_the_triangle_scheme_that_plans_no_single_close_bet():
    found = {b.id: b for b in bots.discover()}
    b = found["triangle"]
    assert b.arbitrage == "triangle" and arb.scheme_of(b) is arb.TRIANGLE and arb.TRIANGLE.series == ("BTC", "XAU", "XAUTBTC")
    assert b.family == "Arbitrage" and b.policy is None and not b.sells_only
    assert [x.id for x in bots.discover() if x.arbitrage][:2] == ["triangle", "triangle_oil"]   # first on the advanced screen
    assert bots.plan(b, book(0, HOURLY, sharp(HOURLY, SPOT)), bell(HOURLY), 100_000, SPOT) == []
    note = dict(botsview.sections(b))["runner"]
    assert "pools it with the leg's own picture" in note and "All legs go in one request" in note and "[triangle]" in note
    assert "80% of the triangle's picture" in note and "25% on cost" in note and "weekend" in note
    assert "Gaussian copula" in dict(botsview.sections(b))["maths"]
    lenses = [x for x in bots.discover() if x.family in tri.LENS_FAMILIES]
    assert {x.id for x in lenses} >= {"smooth_pool", "smooth_kalman", "benter"} and tri.Rules().lens in {x.id for x in lenses}


# ── the runner ──────────────────────────────────────────────

def lens_bot() -> bots.Bot:
    """A lens with a view of its own: a bell on spot, a sixtieth of the ladder wide, on whichever series it is handed."""
    def forecast(book, closes, hours):
        spot = closes[-1] if book.bins[0][0] < closes[-1] < book.bins[-1][1] else (book.bins[0][0] + book.bins[-1][1]) / 2
        return bell(book.bins, spot, (book.bins[-1][1] - book.bins[0][0]) / 60)
    return bots.Bot("lens", "Test Lens", "Smoothing", "a bell on spot", "test", forecast=forecast)


def triangle_bot() -> bots.Bot:
    return bots.Bot("triangle", "Triangulator", "Arbitrage", "the triangle", "test", forecast=lens_bot().forecast, arbitrage="triangle")


def runner(api, tmp_path, live=False, budget=100_000, watch=False, ledger="ledger", **rules):
    return tri.Triangulator(bot=triangle_bot(), api=api, batch_id="", feed=None, live=live, make_feed=feeds, lens=lens_bot(), watch=watch,
                            ledger=bots.Ledger(tmp_path / f"{ledger}.json"),
                            cfg=bots.BotConfig(interval_s=0, objective="edge", kelly=0.25, min_edge=0.25,
                                               edge_close_share=0.03).with_budget(budget),
                            tri=tri.Rules(samples=4_000, **rules))


async def test_on_paper_it_reads_the_three_books_at_one_instant_and_buys_the_flat_sats_book_toward_the_triangle(tmp_path):
    api = Exchange()
    r = runner(api, tmp_path, covers=False)
    await r.cycle()
    assert api.read[:3] == [101, 401, 501] and api.bought == [] and r.trades == 1
    assert r.instants == [api.e1, api.e1 + 3600] and r.step == 1 and r.of == 2
    v = r.last
    assert v is not None and v.end == api.e1 and set(v.sides) == {"BTC", "XAU", "XAUTBTC"} and v.basis == pytest.approx(1.0)
    s = v.sides["XAUTBTC"]
    assert s.formed < 0.05 < v.sides["BTC"].formed and s.weight > 0.8 > v.sides["BTC"].weight     # the flat book takes the triangle
    assert abs(tri.median(XAUT, s.implied) / XAUT_SPOT - 1) < 0.01
    assert sum(s.final[i] for i in range(500) if abs((XAUT[i][0] + XAUT[i][1]) / 2 / XAUT_SPOT - 1) < 0.05) > 0.8
    assert s.buys and all(abs((XAUT[x.index][0] + XAUT[x.index][1]) / 2 / XAUT_SPOT - 1) < 0.1 for x in s.buys)
    assert {x[2] for x in r.holdings()} == {501} or {x[2] for x in r.holdings()} <= {101, 401, 501}
    assert 501 in {x[2] for x in r.holdings()} and 0 < r.open_cost <= 0.1 * 100_000 + 1e-6
    # the flat book's median is off what the dollar books imply; the lens's and the triangle's agree
    assert v.gaps["market"] < -0.01 and abs(v.gaps["own"]) < 0.01 and abs(v.gaps["final"]) < 0.01
    log = "\n".join(r.log)
    assert "1/2  BTC·XAU·XAUTBTC at" in log and "would buy" in log and "from what BTC and XAU imply" in log and "formed BTC" in log
    assert "XAUTBTC" in log.split("would buy")[1] and "ranges" in log.split("would buy")[1]
    assert "worth" in log and "by the triangle" in log and "band" in log and "a range for" in log and "pays" in log
    legs = s.buys
    assert len({round(x.contracts, 2) for x in legs}) == 1 and [x.index for x in legs] == list(range(legs[0].index, legs[-1].index + 1))
    assert s.band is not None and (legs[0].index, legs[-1].index) == s.band and s.band_p >= 0.8 and s.edge > 0.25
    held_before = r.open_cost
    await r.cycle()                                    # the walk starts at the nearest; its lifted book stops a repeat
    await r.cycle()                                                                # the nearest again
    assert r.last.end == api.e1 and r.step == 1
    await r.cycle()                                                                # then the second instant
    assert r.last.end == api.e1 + 3600 and r.step == 2 and api.read[-3:] == [102, 402, 502]
    for _ in range(6):
        await r.cycle()
    assert r.open_cost <= 100_000 + 1e-6 and r.open_cost >= held_before
    assert bots.Ledger(tmp_path / "ledger.json").open_cost("triangle", "paper") == pytest.approx(r.open_cost)


async def test_it_visits_the_nearest_instant_every_other_time_and_watching_draws_the_instant_the_screen_asks_for(tmp_path):
    api = Exchange()
    r = runner(api, tmp_path, instant_share=0.0, covers=False)                     # no room: visits only
    for _ in range(6):
        await r.cycle()
    reads = [tuple(api.read[i:i + 3]) for i in range(0, len(api.read), 3)]
    assert reads == [(101, 401, 501), (101, 401, 501), (101, 401, 501), (102, 402, 502), (101, 401, 501), (101, 401, 501)]
    assert all("full share" in x for x in r.log if "BTC·XAU·XAUTBTC at" in x)
    api = Exchange()
    w = runner(api, tmp_path, watch=True)
    await w.cycle()
    assert w.last is not None and w.last.end == api.e1 and w.last.sides["XAUTBTC"].buys and w.trades == 0 and w.open_cost == 0
    assert "watching" in w.last.note and not any("would buy" in x for x in w.log)
    w.focus_end = api.e1 + 3600
    await w.cycle()
    assert w.last.end == api.e1 + 3600 and api.read[-3:] == [102, 402, 502]


async def test_live_it_buys_every_leg_in_one_request_after_the_estimates_and_a_rejected_leg_leaves_the_rest_standing(tmp_path):
    api = Exchange("k")
    r = runner(api, tmp_path, live=True, covers=False)
    await r.cycle()
    assert r.trades == 1 and len(api.bought) >= 1 and {t for t, _ in api.bought} <= {101, 401, 501} and 501 in {t for t, _ in api.bought}
    assert set(api.estimated) >= {t for t, _ in api.bought}
    bought = [x for x in r.log if "BUY " in x]
    assert bought and "XAUTBTC" in bought[0].split("BUY ")[1] and r.open_cost == pytest.approx(r.paid_sats)
    # the sats book formed on the same prices as the dollar books: the triangle agrees with every leg
    api = Exchange("k")
    api.books[501] = sharp(XAUT, XAUT_SPOT, 1.5, 500)
    r = runner(api, tmp_path, live=True, covers=False, cross_weight=0.5, ledger="formed")
    await r.cycle()
    assert r.last.sides["XAUTBTC"].formed > 0.3
    # a leg the server refuses is dropped and the others stand
    api = Exchange("k", reject={501})
    r = runner(api, tmp_path, live=True, covers=False, ledger="rejected")
    r.cfg = bots.BotConfig(**{**r.cfg.__dict__, "min_edge": 0.0})                 # any edge at all, so a dollar leg is in the order too
    await r.cycle()
    assert any("XAUTBTC rejected" in x for x in r.log)
    assert 501 not in {x[2] for x in r.holdings()}


async def test_a_risk_free_cover_is_taken_first_when_a_pair_of_books_offers_one(tmp_path):
    api = Exchange()
    r = runner(api, tmp_path)
    await r.cycle()
    assert r.last is not None
    took_cover = bool(r.last.cover)
    assert took_cover == any("would buy cover" in x for x in r.log)
    assert r.trades >= 1 and 501 in {x[2] for x in r.holdings()}
    if took_cover:
        assert "XAU·XAUTBTC" in r.last.cover or "BTC·XAUTBTC" in r.last.cover


async def test_instants_past_max_hours_are_left_alone_and_a_book_dearer_than_its_band_pays_is_not_bought(tmp_path):
    api = Exchange()
    r = runner(api, tmp_path, covers=False, max_hours=1.5)
    await r.cycle()
    assert r.instants == [api.e1] and r.of == 1
    # every book sharp on spot: the band costs more than 98 × its chance, so nothing is bought and the log says why
    api = Exchange()
    api.books[501] = sharp(XAUT, XAUT_SPOT, 1.0, 2000)
    api.books[101] = sharp(HOURLY, SPOT, 1.0, 2000)
    api.books[401] = sharp(GOLD, GOLD_SPOT, 1.0, 2000)
    r = runner(api, tmp_path, covers=False, ledger="dear")
    await r.cycle()
    assert r.trades == 0 and r.open_cost == 0 and api.bought == []
    assert "no band 25% over its price" in r.last.note and "sats a set pays" in r.last.note
    assert all(s.edge < 0.25 and s.band is not None for s in r.last.sides.values())


async def test_the_lens_may_read_the_whole_series_and_the_consensus_is_the_lens_when_none_is_given(tmp_path):
    seen = {}

    class SeriesLens:
        """A lens that reads the series: it records what it is handed and answers with a bell."""
        id, name, family, error, reads_series, reads_market, model = "series_lens", "Series Lens", "Smoothing", "", True, False, None

        def probs(self, book, ctx, series=()):
            seen[book.topic_id] = series
            return lens_bot().forecast(book, [ctx.spot], ctx.hours)

    api = Exchange()
    r = runner(api, tmp_path, covers=False)
    r.lens = SeriesLens()
    await r.cycle()
    assert set(seen) == {101, 401, 501} and all(len(s) == 2 for s in seen.values())        # two open closes a series
    assert [round(x.end) for x in seen[101]] == [api.e1, api.e1 + 3600]
    r2 = tri.Triangulator(bot=triangle_bot(), api=Exchange(), batch_id="", feed=None, make_feed=feeds,
                          ledger=bots.Ledger(tmp_path / "l2.json"), cfg=bots.BotConfig(interval_s=0, objective="edge").with_budget(1_000),
                          tri=tri.Rules(samples=2_000, covers=False))
    assert r2.lens_name == "the consensus"
    await r2.cycle()
    assert r2.last is not None and r2.last.sides["BTC"].own is not None


# ── the screen ──────────────────────────────────────────────

class Desk(Exchange):
    """The exchange as the terminal reads it: the series list, the market list of one series, the account."""
    async def markets(self, batch_id, limit=96, on_rows=None):
        return await self.all_markets(batch_id)

    async def close(self): ...
    async def wallet(self): ...
    async def summary(self): ...


@pytest.fixture
def desk(monkeypatch):
    def _make(key=None):
        monkeypatch.setattr(A, "Glimpse", lambda k=None, base_url=None: Desk(k))
        monkeypatch.setattr(auth, "load_key", lambda: (key, "env" if key else "none"))
        monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
        monkeypatch.setattr(A.Terminal, "feed", lambda self, asset=None: self.feeds.setdefault(asset or "BTC", feeds(asset or "BTC")))
        monkeypatch.setattr(tri.Rules, "load", classmethod(lambda cls, **o: cls(samples=2_000, **o)))
        app = A.Terminal()
        app.spot = SPOT
        return app
    return _make


async def test_a_opens_the_advanced_screen_watches_the_triangle_on_paper_and_enter_runs_the_triangulator(desk):
    app = desk()
    async with app.run_test(size=(190, 50)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("A")
        assert app.view == "advanced"
        await until(pilot, lambda: app.bots)
        names = [b.name for b in advview.advanced(app)]
        assert names[:4] == ["Triangulator", "Oil Triangulator", "Arbitrageur", "Gold Arbitrageur"]
        assert app.lens_bot() is not None and app.lens_bot().id == "session_walk"
        await until(pilot, lambda: app.adv_watch is not None and app.adv_watch.last is not None, tries=1200)
        w = app.adv_watch
        assert w.watch and w.running and w.trades == 0 and w.lens.id == "session_walk"
        desk_text = app.query_one("#advdesk").render().plain
        assert "ADVANCED BOTS" in desk_text and "TRIANGLE" in desk_text and "XAUTBTC = XAU × 10⁸ / BTC" in desk_text
        for label in ("BTC", "XAU", "XAUTBTC"):
            assert f" {label} " in desk_text or desk_text.count(label) >= 2
        assert "price at close" in desk_text and "median" in desk_text and "gap  by the market" in desk_text and "buys" in desk_text
        assert "implied by BTC" in desk_text and "triangle" in desk_text
        assert "lens Session Walk" in desk_text and "watching" in desk_text
        assert "band" in desk_text and " pays " in desk_text and "the likely outcomes it bets on" in desk_text
        assert "WATCHING on paper" in app.query_one("#advlog").border_title
        assert "ADVANCED" in app.query_one("#head").render().plain
        await pilot.press("l")                                                    # the next instant
        await until(pilot, lambda: app.adv_watch.last.end == app.adv_watch.instants[1], tries=600)
        await pilot.press("h")
        await pilot.press("m")                                                    # the next lens
        await pilot.pause()
        assert app.adv_lens and app.adv_lens != "session_walk" and "Lens:" in app.flash
        await until(pilot, lambda: app.adv_watch is not w and app.adv_watch.lens.id == app.adv_lens)
        await pilot.press("i")
        await pilot.pause()
        about = app.screen
        assert isinstance(about, A.About) and about._title.startswith("ABOUT · Triangulator")
        assert "copula" in about._body.plain
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("enter", "enter")                                       # run it: the budget prompt, then its default; paper
        await until(pilot, lambda: "triangle" in app.runners and app.runners["triangle"].last is not None, tries=1200)
        r = app.runners["triangle"]
        assert isinstance(r, tri.Triangulator) and not r.live and not r.watch and r.lens.id == app.adv_lens
        assert not app.adv_watch.running                                          # the watcher stands down for the runner
        assert "trades the triangle" in app.query_one("#advlog").border_title and "ON PAPER" in app.query_one("#advlog").border_title
        log_text = app.query_one("#advlog").render().plain
        assert "PORTFOLIO" in log_text
        desk_text = app.query_one("#advdesk").render().plain
        assert "Triangulator" in desk_text and ("holds" in desk_text or "buys" in desk_text)
        await pilot.press("j")                                                    # the Oil Triangulator: a third column that is a model
        await pilot.pause()
        assert advview.selected(app).id == "triangle_oil"
        await until(pilot, lambda: app.adv_watch is not None and app.adv_watch.bot.id == "triangle_oil" and app.adv_watch.last is not None,
                    tries=1200)
        oil = app.query_one("#advdesk").render().plain
        assert "BTC·WTI·WTIBTC" in oil and "a model: no market on Glimpse" in oil and "CME open" in oil and "WTI a model" in oil
        assert "implied by BTC" in oil and "(model)" in oil and oil.count("price at close") == 3
        await pilot.press("j")                                                    # the Arbitrageur: its columns are its scheme's series
        await pilot.pause()
        assert advview.selected(app).id == "arbitrage"
        assert "Press enter to run Arbitrageur" in app.query_one("#advdesk").render().plain
        await pilot.press("k", "k", "x")
        await pilot.pause()
        assert not r.running
        await until(pilot, lambda: app.adv_watch is not None and app.adv_watch.running)       # and comes back when it stops
        await pilot.press("escape")
        await pilot.pause()
        assert app.view == "main" and not app.adv_watch.running                    # leaving the screen stops the watcher


async def test_the_go_bar_and_the_leader_reach_the_advanced_screen(desk):
    app = desk()
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("colon", *"ADV", "enter")
        await pilot.pause()
        assert app.view == "advanced"
        await pilot.press("escape")
        await pilot.press("space", "A")
        await pilot.pause()
        assert app.view == "advanced"
        await pilot.press("A")
        assert app.view == "main"


def test_arbitrageur_columns_are_its_books_as_last_read_with_the_cover_legs_as_holdings(tmp_path):
    from test_arb import runner as arb_runner

    api = Exchange()
    r = arb_runner(api, tmp_path, scheme="gold")
    cols, end = advview.arb_columns(r)
    assert cols == [] and end == 0
    import asyncio

    asyncio.run(r.cycle())
    cols, end = advview.arb_columns(r)
    assert end == api.e1 and [c.label for c in cols] == ["XAU", "XAU 1D", "XAUTBTC", "BTC", "WTIBTC"]
    held = {c.label: c.held for c in cols}
    assert held["XAUTBTC"] and all(c.final is None for c in cols)
    lines = advview.column_lines(cols[2], 60, 12, False)
    assert any("holds" in ln.plain for ln in lines) and "XAUTBTC" in lines[0].plain and "k–" in lines[3].plain


def test_a_column_prints_the_triangle_over_the_market_with_its_buys_and_in_thousands_past_a_million():
    bk = book(5, XAUT, flat(XAUT))
    final = bell(XAUT, XAUT_SPOT, 100_000)
    c = advview.Column("XAUTBTC", bk, XAUT_SPOT, tri.market_picture(bk), final, final, final, 0.0, 0.9,
                       buys={250: (3.0, 120.0)}, held={251: (1.0, 50.0)}, sold={252: 0.5})
    lines = advview.column_lines(c, 70, 14, True)
    text = "\n".join(ln.plain for ln in lines)
    assert "XAUTBTC" in text and "spot" in text and "formed 0%" in text and "implied weight 0.90" in text
    assert "median" in text and "triangle" in text and "k–" in text and "◂" in text
    assert "buys 3 · ₿120" in text or "buys 3.00 · ₿120" in text or "buys" in text
    assert all(ln.cell_len <= 70 for ln in lines)
    narrow = advview.column_lines(c, 40, 8, True)
    assert all(ln.cell_len <= 40 for ln in narrow) and "buys" not in "\n".join(ln.plain for ln in narrow)
    assert advview.pspan(2_500_000, 2_510_000) == "2,500k–2,510k" and advview.pspan(76_400, 76_600) == "76,400–76,600"
    assert advview.pspan(102.73, 102.91) == "102.73–102.91"


def test_the_help_and_the_key_panel_know_the_screen():
    from glimpse_tui import chrome

    assert "advanced" in chrome.KEYMAP and any(key == "A" for _, _, key, _ in chrome.tabs(type("T", (), {"view": "main"})()))
    assert "ADVANCED (A)" in A.HELP and "SPC A" in A.TERM_HELP
    legend = chrome.legend("advanced", 160, 8).plain
    assert "run / stop" in legend and "the lens" in legend


def test_batches_of_the_triangle_are_the_three_hourly_series():
    short = {Batch("1", "Hourly Bitcoin Prediction Markets", 1, 200).short, Batch("2", "Hourly Gold Prediction Markets", 1, 10).short,
             Batch("3", "Hourly Tether Gold in sats per troy ounce", 1, 10000).short}
    assert short == set(arb.TRIANGLE.series)
    row = MarketRow(1, "m", 0, "live", 0, 0, (1.0, 1.0), ("0-1", "1-2"), (1, 2))
    assert PL.NET == 98.0 and row.topic_id == 1
