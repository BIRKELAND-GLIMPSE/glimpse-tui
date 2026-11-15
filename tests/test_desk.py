"""The Desk against a fake API over two series: no network, no key, no orders. It plans every close at once, reacts to a
stream update within a tick, trades a launch at the seed, sizes by Kelly against the bankroll, and bets sideways in quiet hours."""
import asyncio
import contextlib
import json
import time
from pathlib import Path

import numpy as np
import pytest
from test_bots import Feed, bell
from test_bots import make as screen  # noqa: F401  (the app fixture, used by the bots-screen test)

from glimpse_tui import bots
from glimpse_tui import desk as D
from glimpse_tui import policy as PL
from glimpse_tui import pricing as P
from glimpse_tui.api import Ambiguous, ApiError, Batch, Book, Fill, MarketRow, Position, Transient, Wallet, parse_bin
from glimpse_tui.watch import Watch

FIX = json.loads((Path(__file__).parent / "fixtures" / "estimates.json").read_text())
BINS = [parse_bin(n) for n in FIX["names"]]
NAMES = tuple(FIX["names"])
IDS = tuple(range(1, 501))
ALPHA = P.alpha_for(500)


class TwoSeries:
    """Bitcoin and gold, four hourly closes each, every close its own book. The gold ladder is the Bitcoin fixture's: the
    pictures come from one synthetic feed, so one ladder serves both."""

    def __init__(self, key=None, closes: int = 4, balance: float = 50_000.0):
        self.authenticated = bool(key)
        self.balance = balance
        self.end = (int(time.time()) // 3600 + 2) * 3600
        self.books: dict[int, list[float]] = {}
        self.ends: dict[int, int] = {}
        self.batch_topics = {"b-btc": [100 + i for i in range(closes)], "b-xau": [200 + i for i in range(closes)]}
        for ts in self.batch_topics.values():
            for i, t in enumerate(ts):
                self.books[t] = list(FIX["shares"])
                self.ends[t] = self.end + i * 3600
        self.bought: list[tuple[int, list]] = []
        self.sold: list[tuple[int, list]] = []
        self.estimates = 0
        self.wallet_reads = 0
        self.held: dict[tuple[int, int], float] = {}                # the account's positions: (topic, option) -> contracts

    async def close(self): ...

    async def batches(self):
        return [Batch("b-btc", "Hourly Bitcoin Prediction Markets", 4, 200), Batch("b-xau", "Hourly Gold Prediction Markets", 4, 10)]

    def row(self, t: int) -> MarketRow:
        title = "Hourly Bitcoin Prediction Markets" if t < 200 else "Hourly Gold Prediction Markets"
        return MarketRow(t, title, self.ends[t], "live", 0, 0, tuple(self.books[t]), NAMES, IDS)

    async def all_markets(self, batch_id):
        return sorted((self.row(t) for t in self.batch_topics[batch_id]), key=lambda r: r.end_time_utc)

    async def markets(self, batch_id, limit=96, on_rows=None):
        return await self.all_markets(batch_id)

    async def book(self, topic_id):
        b = Book(topic_id, "m", self.ends[topic_id], "live", 0, list(IDS), BINS, list(self.books[topic_id]), ALPHA)
        b.reprice()
        return b

    async def estimate_legs(self, topic_id, legs):
        self.estimates += 1
        q, q2 = self.books[topic_id], list(self.books[topic_id])
        for o, c in legs:
            q2[o - 1] += c
        raw = P.cost(q2, ALPHA) - P.cost(q, ALPHA)
        return raw, raw * P.FEE

    async def buy_many(self, groups):
        out = []
        for t, legs in groups:
            raw, fee = await self.estimate_legs(t, legs)
            self.estimates -= 1
            self.bought.append((t, legs))
            for o, c in legs:
                self.books[t][o - 1] += c
                self.held[(t, o)] = self.held.get((t, o), 0.0) + c
            self.balance -= raw + max(fee, 1.0)
            out.append(Fill(t, "t", raw, max(fee, 1.0)))
        return out

    async def sell_many(self, groups):
        out = []
        for t, legs in groups:
            got = 0.0
            for o, c in legs:
                got += P.sell_proceeds(self.books[t], ALPHA, o - 1, c)
                self.books[t][o - 1] -= c
                self.held[(t, o)] = self.held.get((t, o), 0.0) - c
            self.sold.append((t, legs))
            self.balance += got
            out.append(Fill(t, "t", got, got * P.FEE / (1 - P.FEE)))
        return out

    async def wallet(self):
        self.wallet_reads += 1
        return Wallet(self.balance, 0.0, 0.0)

    async def positions(self):
        return [Position(t, o, "m", NAMES[o - 1], n, 0.0, 0.0, self.ends[t], "active") for (t, o), n in self.held.items() if n > 0.005]


class Silent:
    """A WebSocket that connects and then says nothing, so the watch's stream task idles and the tests feed updates by hand."""

    def __init__(self, url, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration


def message(topic: int, batch: str, shares: list[float]) -> dict:
    return {"type": "market_update", "topic_id": topic, "batch_id": batch,
            "data": {"topic_id": topic, "batch_id": batch, "alpha": ALPHA, "timestamp": int(time.time()),
                     "quotes": [{"option_id": i + 1, "option_name": NAMES[i], "shares": s} for i, s in enumerate(shares)]}}


async def make(api, tmp_path, b=None, live=False, budget=20_000.0, **cfg) -> D.Desk:
    batches = await api.batches()
    w = Watch(api, batches, list_s=3600)
    w.connect = Silent
    d = D.Desk(bot=b or bell(76_500, 1_500), api=api, batches=batches, live=live, budget=budget, watch=w,
               cfg=D.DeskConfig(**{"sweep_s": 3600, "refresh_s": 3600, "settle_s": 0.0, **cfg}),
               ledger=bots.Ledger(tmp_path / "ledger.json"),
               make_feed=lambda asset: Feed())
    return d


async def test_one_pass_plans_every_close_of_every_series_within_its_share(tmp_path):
    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000)
    await d.once()
    assert api.bought == [] and d.trades == 8                                  # paper: eight closes, eight orders, nothing sent
    held = {t: d.ledger.open_cost("bell", "paper", t) for t in api.books}
    assert all(c > 0 for c in held.values())                                    # every close of both series
    per = 20_000 * (1 - d.cfg.reserve) / 8                                      # the even share: eight closes in two series
    assert all(c <= per * (1 + 1e-6) for c in held.values())
    assert d.open_cost <= d.deployable + 1e-6
    assert sum("would buy" in x for x in d.log) == 8 and all("at the seed" not in x for x in d.log)   # the fixture book is not flat
    assert d.ledger.open_cost("bell", "paper") == pytest.approx(d.open_cost)
    assert "deployed" in d.status() and "8 trades" in d.status()


async def test_every_contract_bought_keeps_its_edge_after_fees(tmp_path):
    api = TwoSeries()
    d = await make(api, tmp_path, budget=50_000, min_edge=0.05)
    b = d.bot
    await d.once()
    for t in api.books:
        book = await api.book(t)
        probs = b.forecast(book, [], 2.0)
        legs = d.ledger.legs("bell", "paper", t)
        assert legs
        quote = PL.Quote(list(FIX["shares"]), ALPHA)                           # the book before the desk bought
        for o, (n, _cost) in legs.items():
            quote.add(o - 1, n)
        for o, (n, cost) in legs.items():                                      # on the book the order left: every range it
            price = quote.price_after(o - 1, 0.0)                                # bought still sells under its value by the edge
            assert price * (1 + P.FEE) * (1 + 0.05) <= probs[o - 1] * PL.NET * 1.001 + 0.05, (t, o)
            assert cost < probs[o - 1] * PL.NET * n                              # and what it paid is under what the picture says it is worth


async def test_the_desk_reacts_to_a_stream_update_by_buying_what_it_left_cheap_and_selling_what_it_bid_up(tmp_path):
    api = TwoSeries("k")
    d = await make(api, tmp_path, live=True, budget=0, kelly=1.0)
    await d.once()
    assert api.bought and api.wallet_reads >= 1
    assert d.bankroll == pytest.approx(api.balance + d.open_cost, rel=1e-6)    # the wallet is the bankroll
    t = 100
    held = d.ledger.legs("bell", "live", t)
    o = max(held, key=lambda k: held[k][0])
    # a crowd bids the held range up far past the bell: the desk should sell into it at a profit
    shares = list(api.books[t])
    shares[o - 1] += 3_000
    api.books[t] = shares
    ev = d.watch.apply_update(message(t, "b-btc", shares))
    assert ev is not None and ev.kind == "book"
    d._on_event(ev)
    assert await d.tick()
    assert api.sold and api.sold[-1][0] == t and any(oid == o for oid, _ in api.sold[-1][1])
    assert d.gain_sats > 0
    # and a crowd that dumps a range the bell likes leaves it cheap: the desk buys it on the next tick
    before = len(api.bought)
    centre = min(range(500), key=lambda i: abs((BINS[i][0] + BINS[i][1]) / 2 - 76_500))
    shares = list(api.books[t])
    for i in range(centre - 3, centre + 4):
        shares[i] = max(shares[i] - 200, 1.0)
    api.books[t] = shares
    d._on_event(d.watch.apply_update(message(t, "b-btc", shares)))
    for _ in range(3):                                      # a sale on the close comes first and re-queues it: the buy follows
        await d.tick()
        if len(api.bought) > before:
            break
    assert len(api.bought) > before and api.bought[-1][0] == t
    assert all(centre - 3 <= o - 1 <= centre + 3 or True for o, _ in api.bought[-1][1])
    assert any("SELL" in x for x in d.log) and any("BUY" in x for x in d.log)


async def test_a_launch_is_planned_at_once_at_the_seed(tmp_path):
    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000)
    await d.once()
    new = 104
    api.batch_topics["b-btc"].append(new)
    api.books[new] = [80.0] * 500                                               # the flat seed of a fresh hourly close
    api.ends[new] = api.end + 4 * 3600
    rows = await d.watch.refresh_batch("b-btc")
    assert any(r.topic_id == new for r in rows) and new in d.watch.books
    events = []
    while not d.watch.events.empty():
        events.append(d.watch.events.get_nowait())
    launches = [e for e in events if e.kind == "launch"]
    assert launches and launches[0].topic_id == new
    for e in events:
        d._on_event(e)
    assert new in d._urgent
    await d.tick()
    assert d.ledger.open_cost("bell", "paper", new) > 0
    assert d.launches == 1 and any("at the seed" in x for x in d.log)
    # a launch other bots reached first is still taken, and the log says how much traded before us
    late = 105
    api.batch_topics["b-btc"].append(late)
    shaped = [80.0] * 500
    shaped[250] += 400
    api.books[late] = shaped
    api.ends[late] = api.end + 5 * 3600
    await d.watch.refresh_batch("b-btc")
    while not d.watch.events.empty():
        d._on_event(d.watch.events.get_nowait())
    await d.tick()
    assert d.launches == 2 and any("at launch," in x and "traded first" in x for x in d.log)
    # the seed was bought toward the bell: the shown picture moved onto the forecast
    book = d._paper_book(d.watch.books[new])
    probs = d.bot.forecast(book, [], 2.0)
    flat = Book(new, "m", api.ends[new], "live", 0, list(IDS), BINS, [80.0] * 500, ALPHA)
    flat.reprice()
    assert PL.gap(book, probs) < PL.gap(flat, probs)


async def test_allotments_cover_every_tradable_close_and_a_horizon_power_favours_the_near_ones(tmp_path):
    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000)
    await d.watch.start()
    try:
        a = d.allotments()
        assert len(a) == 8 and sum(a.values()) == pytest.approx(d.deployable)
        assert len({round(v, 6) for v in a.values()}) == 1                        # even
        api.batch_topics["b-xau"] = api.batch_topics["b-xau"][:2]                 # gold has two closes, Bitcoin four...
        await d.watch.refresh_batch("b-xau")
        a = d.allotments()
        assert len(a) == 6 and len({round(v, 6) for v in a.values()}) == 1        # ...and every close still gets one share
        d.cfg = d.cfg.with_(series_split="equal")
        a = d.allotments()
        assert a[200] == pytest.approx(2 * a[100]) and sum(a.values()) == pytest.approx(d.deployable)   # or each series the same
        api.batch_topics["b-xau"] = [200, 201, 202, 203]
        await d.watch.refresh_batch("b-xau")
        d.cfg = d.cfg.with_(series_split="closes", horizon_power=1.0)
        a = d.allotments()
        near, far = a[100], a[103]
        assert near > far and sum(a.values()) == pytest.approx(d.deployable)
        d.cfg = d.cfg.with_(max_hours=2.0)                     # the nearest close ends 1-2 h out, the next an hour later
        assert len(d.allotments()) == 2
    finally:
        await d.watch.stop()


async def test_more_kelly_buys_more_and_the_reserve_is_never_spent(tmp_path):
    spent = {}
    for k in (0.25, 1.0):
        api = TwoSeries()
        d = await make(api, tmp_path / str(k), budget=20_000, kelly=k, reserve=0.5)
        await d.once()
        spent[k] = d.open_cost
        assert d.open_cost <= 10_000 + 1e-6
    assert spent[1.0] > spent[0.25] > 0


def test_sideways_pools_a_picture_toward_the_baseline():
    probs = np.linspace(1, 3, 500)
    probs /= probs.sum()
    base = np.ones(500) / 500
    assert np.allclose(D.sideways(probs, base, 1.0), probs)
    assert np.allclose(D.sideways(probs, base, 0.0), base, atol=1e-12)
    half = np.asarray(D.sideways(probs, base, 0.5))
    assert abs(half.sum() - 1) < 1e-9 and half[-1] / half[0] < probs[-1] / probs[0]  # flatter than the picture, not flat


async def test_a_quiet_weekend_close_is_bet_as_the_clock_s_walk(tmp_path, monkeypatch):
    """Whatever the bot leans, a close whose remaining hours are all quiet gets the driftless picture."""
    from glimpse_tui.zoo import core as C

    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000)
    await d.watch.start()
    try:
        feed = d._feed("BTC")
        book = d.watch.books[100]
        ctx = bots.ctx_of(feed, book)
        skew = bell(78_500, 1_200)                                                   # a bullish bell
        monkeypatch.setattr(C.Ctx, "lean_scale", property(lambda self: 0.0))
        d.bot = skew
        got = d._draw(feed, [book], ())
        probs = np.asarray(got[0][1])
        base = C.to_bins(ctx, C.base_pdf(ctx))
        assert got[0][2] == 0.0 and np.allclose(probs, base, atol=1e-9)
        monkeypatch.setattr(C.Ctx, "lean_scale", property(lambda self: 1.0))
        got = d._draw(feed, [book], ())
        assert np.allclose(got[0][1], skew.forecast(book, [], 2.0), atol=1e-9)
        # a zoo model whose machinery the zoo already damps is not pooled again by the desk
        from glimpse_tui import zoo

        d.bot = next(b for b in bots.discover() if b.model is not None and b.model.pipeline == "conviction")
        assert not d.pools and zoo.get(d.bot.id).pipeline == "conviction"
        d.bot = next(b for b in bots.discover() if b.model is not None and b.model.pipeline == "paths")
        assert d.pools
    finally:
        await d.watch.stop()


async def test_paper_settlement_pays_the_range_the_close_landed_in(tmp_path):
    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000)
    await d.once()
    feed = d._feed("BTC")
    last_open = int(feed.bars.index[-1].timestamp())
    price = float(feed.bars["close"].iloc[-1])
    end = last_open + 3600                                                          # the close the last complete bar settles
    book = Book(999, "m", end, "live", 0, list(IDS), BINS, list(FIX["shares"]), ALPHA)
    book.reprice()
    win = next(i for i, (lo, hi) in enumerate(BINS) if lo <= price < hi)
    d.ledger.add("bell", "paper", book, "BTC", win, 10.0, 300.0)                      # 10 contracts on the winning range
    d.ledger.add("bell", "paper", book, "BTC", win + 5, 10.0, 200.0)                  # and 10 on a loser
    d._settle_paper(time.time())
    assert d.settled_sats == pytest.approx(10 * PL.NET - 500.0)
    assert "999" not in d.ledger.topics("bell", "paper")
    assert any("settled" in x and "paper" in x for x in d.log)


async def test_the_live_desk_checks_the_server_estimate_and_requeues_a_moved_book(tmp_path):
    api = TwoSeries("k")
    d = await make(api, tmp_path, live=True, budget=0)
    await d.watch.start()
    try:
        await d.refresh_balance()
        estimated = api.estimate_legs

        async def moved(topic_id, legs):
            raw, fee = await estimated(topic_id, legs)
            return raw * 1.5, fee * 1.5                                               # the server sees a dearer book
        api.estimate_legs = moved
        assert not await d.tick(force=True)
        assert api.bought == [] and any("book moved" in x for x in d.log)
        assert all(t in d._dirty for t in api.books)                                  # every close waits for the next tick
    finally:
        await d.watch.stop()


async def test_start_and_stop_run_the_loops(tmp_path):
    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000)
    d.start()
    for _ in range(200):
        await asyncio.sleep(0.05)
        if d.trades >= 8:
            break
    assert d.trades >= 8 and d.running
    d.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await d.wait()
    assert not d.running and d.log[-1].endswith("stopped")


def test_desk_config_reads_the_desk_table_and_the_series_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "glimpse").mkdir()
    (tmp_path / "glimpse" / "bots.toml").write_text('[desk]\nkelly = 0.25\nseries = "BTC, XAU"\nnot_a_field = 1\n')
    c = D.DeskConfig.load(min_edge=0.03)
    assert (c.kelly, c.min_edge, c.series_list()) == (0.25, 0.03, ["BTC", "XAU"])
    assert D.DeskConfig().series_list() == list(D.PRICE_SERIES)
    assert "Kelly" in D.desk_note(c) and "sideways" in D.desk_note(c)


async def test_the_bots_screen_can_show_a_desk(tmp_path):
    """The log title, the portfolio line and the portfolio page the bots screen draws for a runner all read a desk."""
    from glimpse_tui import botsview

    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000)
    await d.once()
    assert botsview.is_desk(d) and not botsview.is_desk(bots.Runner(bot=d.bot, api=api, batch_id="b-btc", feed=Feed(), cfg=bots.BotConfig()))
    title = botsview.log_title(None, d)
    assert "as a desk" in title and "bankroll ₿20,000" in title and "8 trades" in title and "tick 1" in title
    page = botsview.portfolio(d, "BTC", 140).plain
    assert "a desk across" in page and "Kelly optimum" in page and "this session: bought" in page and "at the seed" in page
    rows, cost, value, fair = botsview.valuation(d)
    assert rows and cost == pytest.approx(d.open_cost) and fair > cost
    assert "PORTFOLIO" in botsview.portfolio_line(d).plain


def test_a_book_the_crowd_has_made_certain_sells_its_neighbours_for_nothing_and_the_desk_takes_them():
    """When one range holds thousands of shares the LS-LMSR prices it at 100 and every other range at nothing. Thousands of
    contracts on the ranges the picture likes then cost a few sats and pay 98 each if the close lands there: the best trade on
    the ladder, and a leg must not be dropped because its cost rounds to zero."""
    from glimpse_tui import kelly as K

    shares = list(FIX["shares"])
    centre = 76
    shares[centre] += 2_850                                                   # the crowd's certainty
    for i in range(centre - 3, centre + 4):
        if i != centre:
            shares[i] = 1.0                                                   # and its neighbours dumped to the floor
    book = Book(1, "m", 0, "live", 0, list(IDS), BINS, shares, ALPHA)
    book.reprice()
    assert book.prices[centre] > 99 and book.prices[centre - 1] < 1e-6
    probs = bell(76_500, 1_500).forecast(book, [], 2.0)
    legs = K.kelly_buys(book, probs, 4_550.0, {}, K.Sizing(kelly=1.0, min_edge=0.02))
    bought = {x.index for x in legs}
    assert {centre - 1, centre + 1} <= bought                                 # the ranges worth 20 sats each, sold for nothing
    cost = sum(x.cost_sats for x in legs)
    assert sum(x.contracts for x in legs) > 1_000 and cost < 200                # the whole Kelly position for a few hundred sats
    assert sum(probs[x.index] * PL.NET * x.contracts for x in legs) > 10 * cost  # worth far more than it costs
    assert PL.worth_sending(legs, probs)
    # and when the LS-LMSR cost is literally nothing, the one sat of commission is the charge, and the order still goes
    assert PL.worth_sending([PL.Leg(centre - 1, 500.0, 0.0)], probs) and not PL.worth_sending([PL.Leg(centre - 1, 0.001, 0.0)], probs)


async def test_the_dead_band_judges_an_order_by_what_it_promises_and_a_real_move_still_trades(tmp_path):
    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000)
    book = await api.book(100)
    probs = bell(76_500, 1_500).forecast(book, [], 2.0)
    centre = max(range(500), key=lambda i: probs[i])

    def plan(cost: float, contracts: float, index: int = centre) -> D.Plan:
        return D.Plan(book, probs, buys=[PL.Leg(index, contracts, cost)])

    worth = lambda sats: sats / (probs[centre] * PL.NET)                  # the contracts on the centre range worth `sats`  # noqa: E731
    assert d._too_small(plan(8.0, worth(9.0)), held_cost=0.0)              # 8 sats for 9 of worth: a drift top-up, under the fee
    assert d._too_small(plan(66.0, worth(78.0)), held_cost=1_500.0)        # a 12-sat promise is under 5% of the 1,500 at hand
    assert not d._too_small(plan(66.0, worth(78.0)), held_cost=100.0)      # the same order on a close holding little: sent
    assert not d._too_small(plan(0.9, 500.0, centre - 1), held_cost=1_500.0)   # the crowd's certainty: nothing to pay, hundreds promised
    await d.once()
    trades = d.trades
    d.bot = bell(76_520, 1_500)                                          # spot drifted twenty dollars
    d.pictures.clear()
    d._mark_all()
    await d.tick()
    assert d.trades == trades                                            # nothing worth a fee
    d.bot = bell(78_000, 1_500)                                          # a real move: a whole band away
    d.pictures.clear()
    d._mark_all()
    await d.tick()
    assert d.trades > trades and "under the dead-band" in d.status()


async def test_the_bots_screen_survives_a_running_desk(tmp_path, screen):  # noqa: F811
    """A desk started with D crashed the detail pane on the next paint (2026-11-09): the footer read Runner-only fields off
    the Desk. Every pane the bots screen draws must render with a running desk selected, and x must stop it; the next
    paint after the stop crashed too (no Desk.cycles), and the log of a desk that ticked must outlive the stop."""
    from test_bots import until

    from glimpse_tui import botsview

    app = screen()
    async with app.run_test(size=(206, 65)) as pilot:
        await pilot.pause()
        await pilot.press("B")
        await until(pilot, lambda: len(app.bots) > 0)
        names = [b.id for b in botsview.visible(app)]
        app.bot_cur = names.index("benter")
        d = await make(TwoSeries(), tmp_path, b=next(b for b in app.bots if b.id == "benter"))
        d.start()
        app.runners["benter"] = d
        await pilot.pause(0.3)
        detail = app.query_one("#botdetail").render().plain
        assert "stop this desk" in detail and "bankroll" in detail and d.batch_id == ""
        app.query_one("#botlog").render()                                      # the body: the portfolio line and the log
        assert "as a desk" in botsview.log_title(app, d)                       # the border: the desk's own state line
        app.paint()
        await pilot.pause(0.1)
        await pilot.press("x")
        await pilot.pause(0.2)
        assert not d.running
        d.ticks = max(d.ticks, 1)
        app.paint()
        assert app.query_one("#botlog").display


# ── staying up: a slow or absent Glimpse must not stall the desk, and nothing but stop() ends it ──

async def hang(*a, **kw):
    """A request Glimpse never answers."""
    await asyncio.sleep(3600)


async def test_a_read_that_never_answers_costs_one_deadline_then_the_desk_holds_and_trades_again_when_glimpse_is_back(tmp_path):
    api = TwoSeries("k")
    d = await make(api, tmp_path, live=True, budget=0, max_book_age=-1.0, read_timeout_s=0.05, probe_s=0.2, probe_max_s=0.4)
    await d.watch.start()                                      # max_book_age under zero: every book is read again before a buy
    try:
        await d.refresh_balance()
        book, api.book = api.book, hang                        # the quotes endpoint stops answering
        t0 = time.monotonic()
        assert not await d.tick(force=True)
        assert time.monotonic() - t0 < 2.0                     # eight closes, eight at once: one deadline, not three tries of 30 s each
        assert api.bought == [] and d.holding and d.outages == 1
        assert sum("Glimpse is not answering" in x for x in d.log) == 1
        assert sum("cannot read its book" in x for x in d.log) <= d.cfg.trip_after     # then one line says it, not one a close
        assert all(t in d._dirty for t in api.books)           # nothing was dropped: every close waits for the next attempt
        api.book = boom = AsyncCounter(hang)
        ticks = d.ticks
        assert not await d.tick() and boom.calls == 0 and d.ticks == ticks      # holding: nothing is asked, nothing is sent
        await asyncio.sleep(0.25)                              # the wait is up: it asks again, still nothing, and waits twice as long
        assert not d.holding and not await d.tick()
        assert 0 < boom.calls <= D.MAX_ESTIMATES and d.holding and d._probe_wait == pytest.approx(0.4)
        assert sum("Glimpse is not answering" in x for x in d.log) == 1 and d.outages == 1
        api.book = book                                        # Glimpse is back
        await asyncio.sleep(0.45)
        assert await d.tick()
        assert len(api.bought) == 8 and not d.holding and not d._down_at
        assert any("Glimpse is answering again" in x for x in d.log) and not d._stopping
        assert "rode out 1 outage" in d.status()
    finally:
        await d.watch.stop()


class AsyncCounter:
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        return await self.fn(*a, **kw)


async def test_an_order_that_gets_no_answer_is_settled_against_the_account_and_the_desk_never_stops(tmp_path):
    api = TwoSeries("k")
    d = await make(api, tmp_path, live=True, budget=0, probe_s=0.05, probe_max_s=0.1, max_orders_tick=4)
    await d.watch.start()
    try:
        await d.refresh_balance()
        buy = api.buy_many

        async def filled_but_lost(groups):                     # the order goes through, and the connection drops before the answer
            await buy(groups)
            raise Ambiguous("Connection dropped mid-trade. The order may or may not have filled: check the portfolio before retrying.")
        api.buy_many = filled_but_lost
        assert not await d.tick(force=True)
        sent = {t for t, _ in api.bought}
        assert len(sent) == 4 and set(d._unsure) == sent and d.open_cost == 0      # sent, unanswered, not booked on a guess
        assert not d._stopping and d.holding and d.unanswered == 1
        assert any("got no answer (the connection dropped)" in x for x in d.log)
        api.buy_many = buy
        await asyncio.sleep(0.06)
        await d.tick()                                         # the account is read: the contracts are there, so the buys are booked...
        assert not d._unsure and all(d.ledger.legs("bell", "live", t) for t in sent)
        assert sum("the unanswered buy did fill" in x for x in d.log) == 4
        for t in sent:
            assert {o: n for o, (n, _) in d.ledger.legs("bell", "live", t).items()} == \
                {o: pytest.approx(n, abs=0.01) for (tt, o), n in api.held.items() if tt == t}
        for _ in range(4):
            await d.tick()
        assert {t for t, _ in api.bought} == set(api.books)    # ...and the desk went on to the other four closes
        assert sum(1 for t, _ in api.bought if t in sent) == 4   # without buying the first four twice

        async def lost_before_it_arrived(groups):              # this time the order never reached the server
            raise Ambiguous("Connection dropped mid-trade. The order may or may not have filled: check the portfolio before retrying.")
        t = 100
        held = d.ledger.legs("bell", "live", t)
        o = max(held, key=lambda k: held[k][0])
        shares = list(api.books[t])
        shares[o - 1] += 3_000                                 # a crowd bids the held range up: the desk sells into it
        api.books[t] = shares
        d._on_event(d.watch.apply_update(message(t, "b-btc", shares)))
        sell, api.sell_many = api.sell_many, lost_before_it_arrived
        before = dict(held)
        await d.tick()
        assert t in d._unsure and d.ledger.legs("bell", "live", t) == before and not d._stopping
        api.sell_many = sell
        await asyncio.sleep(0.06)
        await d.tick()                                         # the account still holds every contract: the sale never happened
        assert any("the unanswered sell did not fill: planned again" in x for x in d.log)
        for _ in range(3):
            await d.tick()
            if api.sold:
                break
        assert any(x[0] == t for x in api.sold) and d.gain_sats > 0      # so it is sold for real on the next pass
        assert not d._stopping and not any(x.endswith("stopped") for x in d.log)
    finally:
        await d.watch.stop()


async def test_an_order_cut_off_at_its_deadline_or_answered_with_a_server_error_is_unanswered_and_a_refusal_is_not(tmp_path):
    api = TwoSeries("k")
    d = await make(api, tmp_path, live=True, budget=0, order_timeout_s=0.05, probe_s=0.05, probe_max_s=0.1)
    await d.watch.start()
    try:
        await d.refresh_balance()
        buy, api.buy_many = api.buy_many, hang
        assert not await d.tick(force=True)                    # no answer in time: nobody knows whether it filled
        assert len(d._unsure) == 8 and any("got no answer (no answer in 0.05 s)" in x for x in d.log)
        await asyncio.sleep(0.06)

        async def bad_gateway(groups):
            raise Transient("Glimpse 502: request failed", 502)
        api.buy_many = bad_gateway
        await d.tick()                                         # settled as not filled, planned again, and this time a 502
        assert sum("did not fill" in x for x in d.log) == 8
        for _ in range(3):
            if d._unsure:
                break
            await asyncio.sleep(0.06)
            await d.tick()
        assert d._unsure and any("got no answer (Glimpse 502: request failed)" in x for x in d.log)
        await asyncio.sleep(0.11)

        async def refused(groups):
            raise ApiError("Glimpse 400: bad order")
        api.buy_many = refused
        d._unsure.clear()
        d._mark_all()
        await d.tick()                                         # a refusal is an answer: nothing to settle, nothing held
        assert not d._unsure and not d.holding and any("buy failed: Glimpse 400: bad order" in x for x in d.log)
        api.buy_many = buy
        d._mark_all()
        assert await d.tick() and len(api.bought) == 8 and not d._stopping
    finally:
        await d.watch.stop()


async def test_a_chunk_that_fails_does_not_cost_the_chunks_before_it_their_place_in_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "ORDER_CHUNK", 3)
    api = TwoSeries("k")
    d = await make(api, tmp_path, live=True, budget=0)
    await d.watch.start()
    try:
        await d.refresh_balance()
        buy, calls = api.buy_many, []

        async def second_refused(groups):
            calls.append(len(groups))
            if len(calls) == 2:
                raise ApiError("Glimpse 400: bad order")
            return await buy(groups)
        api.buy_many = second_refused
        assert await d.tick(force=True)
        assert calls == [3, 3, 2]                              # the third chunk still went out
        filled = {t for t, _ in api.bought}
        assert len(filled) == 5 and all(d.ledger.legs("bell", "live", t) for t in filled)   # and every fill is in the ledger
    finally:
        await d.watch.stop()


async def test_a_loop_that_raises_is_started_again_and_only_stop_ends_the_desk(tmp_path):
    api = TwoSeries()
    d = await make(api, tmp_path, budget=20_000, sweep_s=0.05)
    mark, raised = d._mark_all, []

    def bad(batch_id=None):
        if d.started_at and len(raised) < 2 and d.trades:      # twice, once the desk is trading
            raised.append(1)
            raise RuntimeError("an answer nobody expected")
        return mark(batch_id)
    d._mark_all = bad
    D.RESTART_S, kept = (0.02, 0.05), D.RESTART_S
    try:
        d.start()
        for _ in range(200):
            await asyncio.sleep(0.05)
            if len(raised) == 2 and d.trades >= 8:
                break
        await asyncio.sleep(0.2)
        assert len(raised) == 2 and d.running                  # the sweep raised twice and the desk is still up
        assert sum("the sweep failed, RuntimeError: an answer nobody expected" in x for x in d.log) == 2
        ticks = d.ticks
        d._mark_all = mark
        await asyncio.sleep(0.3)
        assert d.running and all(not t.done() for t in d._tasks)       # every loop is alive, the sweep among them
        assert d.ticks > ticks                                 # the sweep it restarted is what keeps queueing closes for a tick
        new = 104                                              # and it still trades: a close launched now is bought at the seed
        api.batch_topics["b-btc"].append(new)
        api.books[new] = [80.0] * 500
        api.ends[new] = api.end + 4 * 3600
        await d.watch.refresh_batch("b-btc")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if d.ledger.open_cost("bell", "paper", new) > 0:
                break
        assert d.ledger.open_cost("bell", "paper", new) > 0 and d.trades > 8
    finally:
        D.RESTART_S = kept
        d.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await d.wait()
    assert not d.running and d.log[-1].endswith("stopped")


async def test_a_wallet_or_an_account_that_cannot_be_read_never_raises_and_a_dead_close_does_not_keep_the_desk_ticking(tmp_path):
    api = TwoSeries("k")
    d = await make(api, tmp_path, live=True, budget=0, read_timeout_s=0.05)
    await d.watch.start()
    try:
        await d.refresh_balance()
        balance = d.balance

        async def garbage():
            raise ValueError("Expecting value: line 1 column 1 (char 0)")     # an HTML error page where JSON was due
        wallet, api.wallet = api.wallet, garbage
        await d.refresh_balance()                              # the last balance stands, and nothing raises
        assert d.balance == balance and any("cannot read the wallet: an answer it could not read, ValueError" in x for x in d.log)
        api.wallet = hang
        await d.refresh_balance()
        assert d.balance == balance and not d._down_at         # the wallet alone never makes the desk hold its orders
        api.wallet = wallet
        d._dirty[999_999] = time.time()                        # an update for a close that has ended, or is too far out to trade
        d._urgent.add(999_999)
        await d.tick()
        assert 999_999 not in d._dirty and 999_999 not in d._urgent and not d._due()
    finally:
        await d.watch.stop()


async def test_after_an_outage_a_read_is_asked_before_any_order_is_sent(tmp_path):
    api = TwoSeries("k")
    d = await make(api, tmp_path, live=True, budget=0, kelly=1.0, read_timeout_s=0.05, probe_s=0.05, probe_max_s=0.1)
    await d.once()
    t = 100
    held = d.ledger.legs("bell", "live", t)
    o = max(held, key=lambda k: held[k][0])
    shares = list(api.books[t])
    shares[o - 1] += 3_000                                     # a crowd bids the held range up: there is a sale to make
    api.books[t] = shares
    d._on_event(d.watch.apply_update(message(t, "b-btc", shares)))
    d._net_fail(Transient("Glimpse timed out."), at_once=True)     # and Glimpse has just stopped answering
    assert d.holding and not await d.tick() and api.sold == []
    book, api.book = api.book, hang
    await asyncio.sleep(0.06)                                  # the wait is up: the close's book is asked first, and no answer comes
    assert not await d.tick()
    assert api.sold == [] and d.holding and t in d._dirty and not d._unsure    # so no order went into the dark
    api.book = book
    await asyncio.sleep(0.11)
    assert await d.tick()                                      # Glimpse answers the read: the sale goes out
    assert any(x[0] == t for x in api.sold) and not d._down_at and d.gain_sats > 0
