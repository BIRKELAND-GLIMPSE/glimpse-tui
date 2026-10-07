"""The Profit Taker: it reads every position the account holds, in every series, and sells the part the market overpays at
a profit. It never buys. Fake API with two series, synthetic candles: no network, no orders."""
import time

import numpy as np
import pytest
from test_bots import BINS, FIX, Feed, make, until  # noqa: F401  (make: the screen fixture)

from glimpse_tui import bots, botsview
from glimpse_tui import policy as PL
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, Book, Fill, MarketRow, Position

SERIES = {"b-h": ("Hourly Bitcoin Prediction Markets", 100), "b-x": ("Hourly Gold Prediction Markets", 200)}


class Account:
    """Bitcoin's hourly series and gold's, five closes each, closing at the same hours; every close has its own book."""

    authenticated = True

    def __init__(self):
        self.start = int(time.time()) + 2 * 3600
        self.books: dict[int, list[float]] = {}
        self.held: list[Position] = []
        self.sold, self.bought, self.read = [], [], []

    def end(self, topic_id: int) -> int:
        return self.start + (topic_id % 100) * 3600

    def shares_of(self, topic_id: int) -> list[float]:
        return self.books.setdefault(topic_id, list(FIX["shares"]))

    def hold(self, topic_id: int, index: int, contracts: float, cost: float) -> None:
        self.held.append(Position(topic_id, index + 1, "", "", contracts, cost, cost, self.end(topic_id), "active"))

    async def batches(self):
        return [Batch(b, title, 5, 3600) for b, (title, _) in SERIES.items()]

    async def all_markets(self, batch_id):
        first = SERIES[batch_id][1]
        return [MarketRow(t, SERIES[batch_id][0], self.end(t), "live", 0, 0, tuple(self.shares_of(t)), tuple(FIX["names"]),
                          tuple(range(1, 501))) for t in range(first, first + 5)]

    async def book(self, topic_id):
        self.read.append(topic_id)
        b = Book(topic_id, "m", self.end(topic_id), "live", 0, list(range(1, 501)), BINS, list(self.shares_of(topic_id)),
                 P.alpha_for(500))
        b.reprice()
        return b

    async def positions(self):
        return list(self.held)

    async def sell_many(self, groups):
        out = []
        for t, legs in groups:
            got = 0.0
            for o, c in legs:
                self.sold.append((t, o, c))
                got += P.sell_proceeds(self.shares_of(t), P.alpha_for(500), o - 1, c)
                self.shares_of(t)[o - 1] -= c
            out.append(Fill(t, "t", got, got * P.FEE / (1 - P.FEE)))
        return out

    async def buy_many(self, groups):
        self.bought.append(groups)
        raise AssertionError("the Profit Taker never buys")

    estimate_legs = buy_many


def bell() -> bots.Bot:
    """A wide bell around the fixture's spot."""
    def forecast(book, closes, hours):
        w = [np.exp(-0.5 * (((lo + hi) / 2 - 76_500) / 4_000) ** 2) + 1e-9 for lo, hi in book.bins]
        return [x / sum(w) for x in w]
    return bots.Bot("seller", "Seller", "Portfolio", "takes profit", "test", forecast=forecast, sells_only=True)


def feeds(asset):
    f = Feed()
    f.asset = asset
    return f


def taker(api, tmp_path, live=True, **cfg):
    return bots.ProfitTaker(bot=bell(), api=api, batch_id="", feed=None, live=live, make_feed=feeds,
                            ledger=bots.Ledger(tmp_path / "ledger.json"), cfg=bots.BotConfig(interval_s=0, **cfg))


def overpay(api, topic_id, index, contracts, cost, probs, want=2.0):
    """Hold `contracts` bought for `cost`, then let other traders bid the range up until the Profit Taker would sell
    at least `want` of them, but not all."""
    api.hold(topic_id, index, contracts, cost)
    for _ in range(20_000):
        api.shares_of(topic_id)[index] += 1
        b = Book(topic_id, "m", 0, "live", 0, list(range(1, 501)), BINS, list(api.shares_of(topic_id)), P.alpha_for(500))
        b.reprice()
        got = PL.sell_down(b, probs, {index + 1: (contracts, cost)}, 0.02, profit_only=True)
        if got and got[0][1] >= want:
            assert got[0][1] < contracts
            return got[0][1]
    raise AssertionError("never overpaid")


def probs_of():
    b = Book(0, "m", 0, "live", 0, list(range(1, 501)), BINS, list(FIX["shares"]), P.alpha_for(500))
    return bell().forecast(b, [], 2.0)


async def test_it_sells_the_overpaid_part_of_your_positions_in_every_series_at_a_profit_and_never_buys(tmp_path):
    api, probs = Account(), probs_of()
    i = max(range(len(probs)), key=probs.__getitem__)                          # the likeliest range, bought cheap
    overpay(api, 101, i, 40, 40 * 2.0, probs)                                  # Bitcoin: other traders bid it up
    overpay(api, 202, i + 3, 30, 30 * 2.0, probs)                              # gold: the same, a few ranges higher
    api.hold(103, i, 40, 40 * 99.0)                                            # and one bought dear: overpaid, but a loss
    api.shares_of(103)[i] += 400
    r = taker(api, tmp_path)
    for _ in range(3):                                                         # the three closes held, once each
        await r.cycle()
    assert api.bought == []
    sold = {(t, o): c for t, o, c in api.sold}
    assert set(sold) == {(101, i + 1), (202, i + 4)}                           # both series; never the loser
    assert 0 < sold[(101, i + 1)] < 40 and 0 < sold[(202, i + 4)] < 30         # part of each, the rest kept
    assert r.gain_sats > 0 and r.sold_sats > r.gain_sats                       # at a profit over what it cost
    held = r.ledger.legs(bots.PORTFOLIO, "live", 101)
    assert held[i + 1][0] == pytest.approx(40 - sold[(101, i + 1)], abs=0.01)
    assert any("SELL" in x and "XAU at" in x and "on cost" in x for x in r.log)
    assert any("BTC at" in x and "nothing to sell" in x and "(its cost)" in x for x in r.log)   # why the loser stays


async def test_it_visits_only_the_closes_you_hold_by_close_time_across_series(tmp_path):
    api, probs = Account(), probs_of()
    i = max(range(len(probs)), key=probs.__getitem__)
    for t in (203, 101, 201):                                                  # 101 and 201 close at the same hour
        api.hold(t, i, 5, 5 * 2.0)
    r = taker(api, tmp_path)
    for _ in range(4):
        await r.cycle()
    assert api.read == [101, 201, 203, 101]                                    # nearest first, ties by topic, then round again
    assert r.rounds == 1 and r.of == 3 and "round 1 done" in "\n".join(r.log)
    assert all("bought" not in x for x in r.log)


async def test_on_paper_it_sells_nothing_real_and_counts_the_paper_sale(tmp_path):
    api, probs = Account(), probs_of()
    i = max(range(len(probs)), key=probs.__getitem__)
    want = overpay(api, 101, i, 40, 40 * 2.0, probs)
    r = taker(api, tmp_path, live=False)
    await r.cycle()
    assert api.sold == [] and api.bought == []
    assert r.ledger.legs(bots.PORTFOLIO, "paper", 101)[i + 1][0] == pytest.approx(40 - want, abs=0.05)
    assert r.gain_sats > 0 and any("would sell" in x for x in r.log)
    page = botsview.portfolio(r, "BTC", 120).plain
    assert "taking profit on your whole portfolio" in page and "never buys" in page
    assert "takes profit on your whole portfolio" in botsview.log_title(None, r)


async def test_with_nothing_held_it_says_so_once_a_minute_not_every_visit(tmp_path):
    api = Account()
    r = taker(api, tmp_path)
    for _ in range(5):
        await r.cycle()
    assert api.read == [] and sum("nothing to sell" in x for x in r.log) == 1


def test_it_is_a_bot_of_its_own_that_plans_no_buys_and_stays_out_of_the_swarm():
    found = {b.id: b for b in bots.discover()}
    b = found["take_profit"]
    assert b.sells_only and b.family == "Portfolio" and b.policy is None
    book = Book(0, "m", 0, "live", 0, list(range(1, 501)), BINS, list(FIX["shares"]), P.alpha_for(500))
    book.reprice()
    assert bots.plan(b, book, probs_of(), 100_000, 76_500) == []
    runner = dict(botsview.sections(b))["runner"]
    assert "never buys" in runner.lower() and "never sells at a loss" in runner


async def test_enter_on_the_profit_taker_needs_your_key_then_runs_on_paper_without_a_budget(make):  # noqa: F811
    app = make()
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        await pilot.press("slash", *"profit taker", "enter", "enter")
        await pilot.pause()
        assert "take_profit" not in app.runners and "log in" in app.flash.lower()


async def test_enter_runs_the_profit_taker_and_a_manager_cannot_run_beside_it(make):  # noqa: F811
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        await pilot.press("slash", *"profit taker", "enter")
        await pilot.press("b")
        await pilot.pause()
        assert "never buys" in app.flash
        await pilot.press("enter", "enter")                            # no budget asked: straight to real-or-paper; paper
        await until(pilot, lambda: "take_profit" in app.runners and app.runners["take_profit"].cycles >= 1)
        r = app.runners["take_profit"]
        assert isinstance(r, bots.ProfitTaker) and not r.live and r.manage
        assert "takes profit on your whole portfolio" in app.query_one("#botlog").border_title
        await pilot.press("escape", "slash", *"iron condor", "enter", "R")
        await pilot.pause()
        assert "opt_iron_condor" not in app.runners and "already manages" in app.flash
        await pilot.press("X")
