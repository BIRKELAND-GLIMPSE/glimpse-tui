"""The retarget series and their bots. The rule is pinned to real retargets, local prices to the live server's estimates, and the
forecast to what the chain's arithmetic requires of it. No network."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from glimpse_tui import api, bots, cli
from glimpse_tui import difficulty as H
from glimpse_tui import difficulty_lab as L
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, Book, MarketRow
from glimpse_tui.funcs.difficulty import DffPane, cover_quote
from glimpse_tui.zoo import core as C
from glimpse_tui.zoo.data import Feed
from glimpse_tui.zoo.difficulty import ADJUSTMENT_TRADING, DIFFICULTY_TRADING, adjustment_rule, difficulty_rule

FIX = Path(__file__).parent / "fixtures"
CHAIN = json.loads((FIX / "retarget_chain.json").read_text())
BOOKS = json.loads((FIX / "estimates_retarget.json").read_text())
TS = np.array(CHAIN["ts"], dtype=float)
EPOCHS = np.array(CHAIN["epochs"], dtype=float)
TIP = CHAIN["first"] + len(TS) - 1


def block_difficulty() -> np.ndarray:
    return EPOCHS[np.arange(CHAIN["first"], TIP + 1) // H.EPOCH]


def real_state(now: float | None = None) -> H.State:
    return H.state_from(CHAIN["first"], TS, block_difficulty(), EPOCHS, float(TS.max()) + 60.0 if now is None else now, 168.0)


def state(height: int = 975_000, pace: float = 1.0, into: float | None = None, d: float = 1.5e14, trend: float = 0.0) -> H.State:
    """A chain at `height` that has run at `pace` of schedule all epoch."""
    now = 1_800_000_000.0
    done = height - height // H.EPOCH * H.EPOCH
    start = now - done * 600.0 / pace if into is None else now - into
    return H.State(now, height, d, start, d * pace, trend)


# ── the rule ────────────────────────────────────────────────


def test_the_rule_rebuilds_the_real_retarget():
    """All of epoch 482 is in the fixture: the rule on its first and last blocks' times gives the difficulty epoch 483 was set to."""
    start, last = 482 * H.EPOCH - CHAIN["first"], 483 * H.EPOCH - 1 - CHAIN["first"]
    assert start >= 0
    assert EPOCHS[482] * H.TARGET / (TS[last] - TS[start]) == pytest.approx(EPOCHS[483], rel=3e-5)
    assert H.retarget_after(974_000) == 975_744 and H.retarget_after(975_743) == 975_744 and H.retarget_after(975_744) == 977_760


def test_a_slow_epoch_cuts_the_difficulty_by_what_the_rule_says():
    run = H.simulate(
        state(pace=0.9, height=975_000),
        H.Params(sigma_day=0.0, rate_se=0.0, vol_of_vol=0.0, stamp_s=0.0, growth_trend=0.0, growth_sd=0.0),
        epochs=1,
        n=4000,
    )
    assert np.median(run.adjustment_at(975_744)) == pytest.approx(0.9, abs=0.01)


def test_a_retarget_near_at_hand_is_nearly_known_and_a_far_one_is_not():
    p = H.Params()
    near, far = H.simulate(state(height=975_740), p, epochs=1, n=4000), H.simulate(state(height=973_800), p, epochs=1, n=4000)
    wide = lambda x: float(np.std(np.log(x)))  # noqa: E731
    assert wide(near.adjustment_at(975_744)) < 0.002 < 0.015 < wide(far.adjustment_at(975_744))


def test_both_ladders_read_one_number_on_the_next_retarget_and_paths_are_consistent_after_it():
    st = state(height=975_000)
    run = H.simulate(st, H.Params(), n=2000)
    assert list(run.heights[:2]) == [975_744, 977_760] and len(run.heights) == H.MARKETS
    np.testing.assert_allclose(run.adjustment_at(975_744) * st.difficulty, run.difficulty_at(975_744))
    np.testing.assert_allclose(run.adjustment_at(977_760) * run.difficulty_at(975_744), run.difficulty_at(977_760))
    assert (np.diff(run.at, axis=0) > 0).all()


def test_a_lucky_epoch_is_largely_taken_back_by_the_next():
    """Nothing in the simulation says so but the rule: pure block luck makes consecutive adjustments negatively correlated."""
    run = H.simulate(
        state(height=975_000 - 700), H.Params(sigma_day=1e-5, rate_se=0.0, vol_of_vol=0.0, tail_df=50, growth_sd=0.0), epochs=4, n=6000
    )
    a, b = np.log(run.adjustment_at(977_760)), np.log(run.adjustment_at(979_776))
    assert np.corrcoef(a, b)[0, 1] < -0.3
    spread = [float(np.std(np.log(run.difficulty[k]))) for k in (1, 3)]
    assert spread[1] < spread[0] * np.sqrt(2)


def test_growth_carries_the_median_forward():
    flat = H.simulate(state(height=975_000, trend=0.002), H.Params(growth_trend=0.0, growth_sd=0.0), n=3000)
    up = H.simulate(state(height=975_000, trend=0.002), H.Params(growth_trend=1.0, growth_sd=0.0), n=3000)
    assert np.median(up.difficulty[-1]) > 1.2 * np.median(flat.difficulty[-1])


def test_the_rate_is_carried_forward_by_its_own_lag():
    """An average of the last week on a rising chain reads last week's hash rate: without this the median runs low."""
    still = H.simulate(
        state(height=975_740, trend=0.0), H.Params(sigma_day=1e-6, rate_se=0.0, vol_of_vol=0.0, tail_df=50, growth_sd=0.0), epochs=2, n=2000
    )
    rising = H.simulate(
        state(height=975_740, trend=0.002), H.Params(sigma_day=1e-6, rate_se=0.0, vol_of_vol=0.0, tail_df=50, growth_sd=0.0), epochs=2, n=2000
    )
    lag = 0.002 * 168.0 / 24.0
    assert np.median(np.log(rising.adjustment_at(977_760) / still.adjustment_at(977_760))) == pytest.approx(lag + 0.002 * 7, abs=0.01)


def test_the_real_chains_state_reads_the_real_epoch():
    st = real_state()
    assert st.height == TIP and st.next_retarget == 975_744
    assert st.difficulty == EPOCHS[483]
    assert st.epoch_start == TS[483 * H.EPOCH - CHAIN["first"]]
    at, change = st.retarget_estimate()
    assert at > st.now and -0.15 < change < 0.15
    assert 0.8 < st.pace < 1.2 and 0.0 < st.trend < 0.01


def test_probabilities_cover_the_ladder_and_count_what_falls_off_it_in_its_ends():
    x = np.random.default_rng(0).normal(148, 3, 4000)
    edges = np.arange(50.0, 251.0)
    p = H.probabilities(x, edges)
    assert p.sum() == pytest.approx(1) and (p > 0).all() and 140 < edges[np.argmax(p)] < 156
    far = H.probabilities(np.full(4000, 300.0) + np.random.default_rng(1).normal(0, 1, 4000), edges)
    assert far[-1] > 0.99


# ── the series ──────────────────────────────────────────────


def test_a_series_says_what_it_is_by_its_topic_type_since_its_title_cannot():
    g = api.Glimpse()

    async def main():
        class R:
            batches = []

        return R()

    async def second(path, params=None):
        return {
            "batches": [
                {
                    "batch_id": "d",
                    "topic_type": "btc_difficulty",
                    "main_topic_title": "Bitcoin Difficulty Adjustment",
                    "topic_count": 12,
                    "outcome_interval": 1,
                },
                {
                    "batch_id": "a",
                    "topic_type": "adjustment_epoch",
                    "main_topic_title": "Bitcoin Difficulty Adjustment % · Epoch",
                    "topic_count": 12,
                    "outcome_interval": 1,
                },
                {
                    "batch_id": "h",
                    "topic_type": "hashprice_daily",
                    "main_topic_title": "Bitcoin Hashprice · Daily",
                    "topic_count": 26,
                    "outcome_interval": 10,
                },
                {"batch_id": "x", "topic_type": "something_new", "main_topic_title": "Who knows", "topic_count": 1},
            ]
        }

    g._pub.batches, g._dpub._get = main, second
    got = asyncio.run(g.batches())
    assert [(b.short, b.interval) for b in got] == [("HASHPRICE", 0.1), ("DIFFICULTY", 1.0), ("ADJUSTMENT", 1.0)]
    assert {"d", "a", "h"} <= api._DM_BATCHES


def test_market_titles_and_ranges_read_right_on_each_series():
    assert api.asset_of("Bitcoin difficulty at epoch 484") == "DIFFICULTY"
    assert api.asset_of("Bitcoin difficulty adjustment at epoch 484") == "ADJUSTMENT"
    assert api.asset_of("Bitcoin hashprice (daily) on 6 Nov 2026, 00:00 UTC") == "HASHPRICE"
    assert api.parse_bin("-30 to -29") == (-30.0, -29.0) and api.parse_bin("0 to 1") == (0.0, 1.0) and api.parse_bin("50-51") == (50.0, 51.0)
    row = api._row(
        {
            "topic_id": P.DMARKET_TOPIC + 777,
            "topic_type": "btc_difficulty",
            "title": "Bitcoin difficulty at epoch 484",
            "end_time_utc": 1,
            "close_block_height": 975744,
            "trading_closes_at_block": 975741,
            "alpha": 0.5,
            "outcomes": [{"option_id": 1, "name": "50-51", "shares": 1}],
        }
    )
    assert row.names == ("50-51",), "only hashprice names its ranges in cents"
    assert row.close_block == 975744 and row.trading_stops_block == 975741
    assert P.alpha_for(200, row.topic_id) == 0.5, "the alpha the service states is the alpha"
    assert api.range_name("3570-3580", P.DMARKET_TOPIC + 778, "Bitcoin hashprice") == "35.70-35.80"
    assert api.range_name("50-51", P.DMARKET_TOPIC + 779, "Bitcoin difficulty at epoch 490") == "50-51"
    del P.STATED_ALPHA[row.topic_id]


@pytest.mark.parametrize("series", ["DIFFICULTY", "ADJUSTMENT"])
def test_local_prices_match_the_live_servers_estimates(series):
    b = BOOKS[series]
    for case in b["cases"]:
        t = P.ticket(b["shares"], b["alpha"], case["lo"], case["hi"], case["contracts"], prob=0.0)
        assert t.cost_sats - t.fee_sats == pytest.approx(case["cost"], abs=0.02)
        assert t.fee_sats == pytest.approx(case["fee"], abs=0.02)


def test_the_book_reads_alpha_and_heights_the_sdk_would_drop():
    b = BOOKS["ADJUSTMENT"]
    g = api.Glimpse()

    async def quotes(path, params=None):
        assert path.endswith(f"/markets/{b['topic']}/quotes")
        return {
            "topic_id": b["topic"],
            "topic_type": "adjustment_epoch",
            "title": b["title"],
            "market_end_time_utc": 2_000_000_000,
            "alpha": b["alpha"],
            "close_block_height": b["close_block"],
            "trading_closes_at_block": b["trading_stops_block"],
            "quote_mode": "live",
            "outcomes": [{"option_id": o, "name": n, "shares": s} for o, n, s in zip(b["option_ids"], b["names"], b["shares"], strict=True)],
        }

    g._dpub._get = quotes
    book = asyncio.run(g.book(b["topic"]))
    assert book.alpha == b["alpha"] and book.close_block == b["close_block"] and book.bins[0] == (-30.0, -29.0)
    assert sum(book.probs) == pytest.approx(1)
    assert sum(book.prices) < 120, "V = 0.1 here, not 2: the lift across the ranges is small"
    P.STATED_ALPHA.pop(b["topic"], None)


def test_candles_step_at_the_retarget_on_both_series(monkeypatch):
    async def go(asset):
        data = [100e12] * 3 + [110e12] * 3 + [99e12] * 3
        stamps = [1_800_000_000 - 3600 * (8 - i) for i in range(9)]

        class Client:
            async def get(self, url, params=None):
                return httpx.Response(200, json=[{"data": data}, {"data": stamps}], request=httpx.Request("GET", url))

        from glimpse_tui.data import http

        monkeypatch.setattr(http, "client", lambda url: Client())
        return await api.retarget_candles(asset, 3600, start=stamps[0], end=stamps[-1])

    d = asyncio.run(go("DIFFICULTY"))
    assert [c.c for c in d[:3]] == [100, 100, 100] and d[3].c == 110 and d[3].o == 100 and d[-1].c == 99
    a = asyncio.run(go("ADJUSTMENT"))
    assert a[0].c == pytest.approx(10) and a[-1].c == pytest.approx(-10)


# ── the bots ────────────────────────────────────────────────


def book_of(series: str) -> Book:
    b = BOOKS[series]
    out = Book(
        b["topic"],
        b["title"],
        2_000_000_000,
        "live",
        0,
        list(b["option_ids"]),
        [api.parse_bin(n) for n in b["names"]],
        list(b["shares"]),
        b["alpha"],
        close_block=b["close_block"],
        trading_stops_block=b["trading_stops_block"],
    )
    out.reprice()
    return out


def live_inputs() -> H.Inputs:
    src = H.Inputs(H.Params())
    src.first, src.ts, src.diff, src.epochs, src.read_at = CHAIN["first"], TS, block_difficulty(), EPOCHS, time.time()
    return src


def ctx_for(series: str, book: Book, src: H.Inputs) -> C.Ctx:
    feed = Feed(series)
    feed.parts = src
    feed.bars = src.hourly_bars(500)
    feed.spot = 0.0 if series == "ADJUSTMENT" else 148.0
    return feed.ctx(book.bins, book.end_time_utc, now=float(TS.max()) + 60, height=book.close_block)


def test_only_the_retarget_bots_draw_the_retarget_series():
    every = bots.discover()
    assert [b.id for b in every if b.draws("DIFFICULTY")] == ["difficulty"]
    assert [b.id for b in every if b.draws("ADJUSTMENT")] == ["adjustment"]
    assert not next(b for b in every if b.id == "difficulty").draws("BTC")
    assert next(b for b in every if b.only_series == "").draws("BTC")


@pytest.mark.parametrize("series,fn", [("DIFFICULTY", difficulty_rule), ("ADJUSTMENT", adjustment_rule)])
def test_each_bot_reads_the_simulation_at_its_own_ladder(series, fn):
    book = book_of(series)
    src = live_inputs()
    p = fn(ctx_for(series, book, src))
    assert len(p) == len(book.bins) and p.sum() == pytest.approx(1) and (p > 0).all()
    mid = np.array([(a + b) / 2 for a, b in book.bins])
    centre = float(mid @ p)
    st = src.state(float(TS.max()) + 60)
    want = (st.difficulty / 1e12 * (1 + st.retarget_estimate()[1])) if series == "DIFFICULTY" else st.retarget_estimate()[1] * 100
    assert centre == pytest.approx(want, abs=4.0 if series == "DIFFICULTY" else 3.0)


def test_the_adjustments_picture_reads_in_percent_through_zero():
    # Its ranges run through zero and its spot is 0%: read in log price, the median and band were NaN and the
    # bots chart crashed placing them.
    book = book_of("ADJUSTMENT")
    ctx = ctx_for("ADJUSTMENT", book, live_inputs())
    p = adjustment_rule(ctx)
    d = C.describe(ctx, p)
    assert all(np.isfinite(d[k]) for k in ("median", "low", "high", "lean", "width"))
    assert -30 < d["low"] < d["median"] < d["high"] < 30
    mid = np.array([(a + b) / 2 for a, b in book.bins])
    assert d["median"] == pytest.approx(float(mid @ p), abs=1.5)


def test_the_adjustments_history_is_the_change_each_retarget_made():
    src = live_inputs()
    bars = src.adjustment_bars(500)
    last = (EPOCHS[TIP // H.EPOCH] / EPOCHS[TIP // H.EPOCH - 1] - 1) * 100
    assert bars["close"].iloc[-1] == pytest.approx(last)
    assert len(bars) == 500 and bars["close"].between(-30, 30).all()


def test_a_model_without_its_inputs_says_so_rather_than_guessing():
    book = book_of("DIFFICULTY")
    src = live_inputs()
    ctx = ctx_for("DIFFICULTY", book, src)
    src.read_at = 0.0
    with pytest.raises(C.ModelUnavailable, match="out of date"):
        difficulty_rule(ctx)
    src.read_at = time.time()
    ctx.height = 0
    with pytest.raises(C.ModelUnavailable, match="height"):
        difficulty_rule(ctx)
    ctx.parts = None
    with pytest.raises(C.ModelUnavailable, match="its own retarget"):
        difficulty_rule(ctx)


def simple_book(q: list[float] | None = None) -> Book:
    bins = [(float(i), float(i + 1)) for i in range(100, 200)]
    b = Book(P.DMARKET_TOPIC, "m", 2_000_000_000, "live", 0, list(range(1, 101)), bins, q or [10_000.0] * 100, 2 / (100 * np.log(100)))
    b.reprice()
    return b


def picture(centre: float = 150.0, sd: float = 6.0) -> list[float]:
    x = np.arange(100, 200) + 0.5
    p = np.exp(-0.5 * ((x - centre) / sd) ** 2) + 1e-9
    return list(p / p.sum())


def test_the_hedgers_side_starts_above_both_the_median_and_no_change():
    book = simple_book()
    side = bots.hedgers_side(book, picture(150.0), 140.0)
    assert not side[50] and side[51] and not side[0]  # above the median: 150 is the pivot
    side = bots.hedgers_side(book, picture(150.0), 160.0)
    assert not side[60] and side[61]  # a falling forecast: down to and through no change is still stable


def test_leaning_keeps_the_forecast_first():
    book, p = simple_book(), picture(150.0)
    cfg = bots.BotConfig(objective="edge", bankroll_sats=100_000, kelly=0.15, min_edge=0.10, fill_margin=0.05)
    plain = bots.decide(book, p, cfg, 20_000, 0.0, 150.0)
    assert any(book.bins[x.index][0] > 150 for x in plain) and any(book.bins[x.index][0] < 150 for x in plain)
    never = bots.decide(book, p, bots.BotConfig(**{**cfg.__dict__, "hedger_edge": float("inf")}), 20_000, 0.0, 150.0)
    assert never and all(book.bins[x.index][0] <= 150 for x in never), "never their side"
    # nothing is bought over its value, lean or not: a book already at the picture's prices gets no order at all
    rich = simple_book([10_000.0 + 2_000 * (x / max(p)) for x in p])
    rich_p = [x / sum(rich.prices) for x in rich.prices]
    assert bots.decide(rich, rich_p, bots.BotConfig(**{**cfg.__dict__, "hedger_edge": 0.30}), 20_000, 0.0, 150.0) == []
    for x in bots.decide(book, p, bots.BotConfig(**{**cfg.__dict__, "hedger_edge": 0.30}), 20_000, 0.0, 150.0):
        assert p[x.index] * 98 > book.prices[x.index] * 1.02


def test_a_finite_lean_still_buys_their_side_when_it_is_cheap_enough():
    book, p = simple_book(), picture(150.0)
    cfg = bots.BotConfig(objective="edge", bankroll_sats=100_000, kelly=0.15, min_edge=0.10, fill_margin=0.05, hedger_edge=0.30)
    legs = bots.decide(book, p, cfg, 20_000, 0.0, 150.0)
    assert any(book.bins[x.index][0] > 150 for x in legs), "a uniform seed leaves even the hedgers' side far under value"


def test_the_runner_stops_at_the_height_the_market_does():
    bot = next(b for b in bots.discover() if b.id == "difficulty")

    class Parts:
        tip = 975_740

    class F:
        asset, ready, spot, parts = "DIFFICULTY", True, 148.0, Parts()

        async def refresh(self, max_age=30.0): ...

    r = bots.Runner(bot=bot, api=None, batch_id="d", feed=F(), cfg=bots.BotConfig().with_budget(100_000))
    assert not r._stopped_at_block(975_741)
    F.parts.tip = 975_741
    assert r._stopped_at_block(975_741)
    assert not r._stopped_at_block(0), "a series that closes on the clock has no height to stop at"


class FakeApi:
    authenticated = False

    def __init__(self, series: str, closes: int = 2) -> None:
        self.series, self.closes = series, closes

    async def close(self): ...

    async def all_markets(self, batch_id):
        b = BOOKS[self.series]
        return [
            MarketRow(
                b["topic"] + i,
                b["title"],
                int(time.time()) + 86400 * (3 + 14 * i),
                "live",
                0,
                0,
                tuple(b["shares"]),
                tuple(b["names"]),
                tuple(b["option_ids"]),
                b["close_block"] + i * H.EPOCH,
                b["trading_stops_block"] + i * H.EPOCH,
            )
            for i in range(self.closes)
        ]

    async def book(self, topic_id):
        b = BOOKS[self.series]
        i = topic_id - b["topic"]
        out = Book(
            topic_id,
            b["title"],
            int(time.time()) + 86400 * (3 + 14 * i),
            "live",
            0,
            list(b["option_ids"]),
            [api.parse_bin(n) for n in b["names"]],
            list(b["shares"]),
            b["alpha"],
            close_block=b["close_block"] + i * H.EPOCH,
            trading_stops_block=b["trading_stops_block"] + i * H.EPOCH,
        )
        out.reprice()
        return out


class LiveFeed(Feed):
    async def refresh(self, max_age: float = 30.0) -> None:
        self.parts = self.parts or live_inputs()
        self.parts.read_at = time.time()
        self.bars = self.parts.hourly_bars(500)
        self.spot = 0.0 if self.asset == "ADJUSTMENT" else 148.0


@pytest.mark.parametrize("series,bot_id", [("DIFFICULTY", "difficulty"), ("ADJUSTMENT", "adjustment")])
def test_a_paper_visit_buys_under_value_and_never_at_a_loss_by_its_picture(series, bot_id):
    bot = next(b for b in bots.discover() if b.id == bot_id)
    logs: list[str] = []
    r = bots.Runner(
        bot=bot,
        api=FakeApi(series),
        batch_id="d",
        feed=LiveFeed(series),
        cfg=bots.BotConfig().with_budget(100_000),
        ledger=bots.Ledger(Path("/nonexistent/ledger")),
        series=series,
        on_log=logs.append,
    )
    asyncio.run(r.cycle())
    trading = DIFFICULTY_TRADING if series == "DIFFICULTY" else ADJUSTMENT_TRADING
    assert r.cfg.hedger_edge == float("inf") and r.cfg.objective == "edge" and r.cfg.markets == trading["markets"]
    assert r.step == 1 and logs, "it visited the nearest retarget"
    assert not any("no picture" in x or "visit failed" in x for x in logs), logs
    assert 0 < r.paid_sats <= r.cfg.bankroll_sats / min(2, trading["markets"]) + 1
    book = asyncio.run(r.api.book(BOOKS[series]["topic"]))
    theirs = bots.hedgers_side(
        book, bot.probs(book, r.feed.ctx(book.bins, book.end_time_utc, now=time.time(), height=book.close_block)), r.feed.spot
    )
    assert not any(theirs[book.option_ids.index(x[3])] for x in r.holdings()), "never the hedgers' side"


def test_the_command_line_keeps_each_bot_on_its_series_and_off_a_service_that_refuses(monkeypatch, capsys):
    class Client:
        authenticated = True

        def __init__(self, *a): ...

        async def close(self): ...

        async def batches(self):
            return [
                Batch("d", "Bitcoin Difficulty Adjustment", 12, 1.0, "DIFFICULTY"),
                Batch("a", "Bitcoin Difficulty Adjustment % · Epoch", 12, 1.0, "ADJUSTMENT"),
                Batch("b", "Hourly Bitcoin Prediction Markets", 3, 200),
            ]

        async def can_trade_second_service(self):
            return False

    monkeypatch.setattr(cli, "Glimpse", Client)
    monkeypatch.setattr(cli.auth, "load_key", lambda: ("k", "env"))
    monkeypatch.setenv(cli.LIVE_ENV, "I_UNDERSTAND")
    assert cli.main(["run", "difficulty", "--series", "ADJUSTMENT", "--once"]) == 2
    assert "only draw DIFFICULTY" in capsys.readouterr().err
    assert cli.main(["run", "adjustment", "--live", "--once"]) == 2
    assert "did not accept these credentials" in capsys.readouterr().err
    assert cli.main(["run", "tsmom_drift", "--series", "DIFFICULTY", "--once"]) == 2
    assert "no picture of DIFFICULTY" in capsys.readouterr().err


# ── the live inputs ─────────────────────────────────────────


def test_inputs_read_the_blocks_then_only_the_newest_and_keep_their_answer_on_failure(monkeypatch):
    src = H.Inputs(H.Params())
    seen: list[int] = []
    tip = {"h": 5000}
    fail = {"on": False}

    async def bulk(series, index, start):
        seen.append(start)
        if fail["on"]:
            raise httpx.ConnectError("no")
        if series == "difficulty":
            return [{"data": [1e14] * (tip["h"] // H.EPOCH + 1)}]
        n = -start
        first = tip["h"] + 1 - n
        return [{"start": first, "data": [1e9 + 600.0 * i for i in range(first, tip["h"] + 1)]}, {"data": [1e14] * n}]

    src._bulk = bulk
    asyncio.run(src.refresh())
    assert src.tip == 5000 and len(src.ts) == H.HISTORY_BLOCKS and src.ready and seen[0] == -H.HISTORY_BLOCKS
    tip["h"] = 5003
    src.read_at = 0.0
    asyncio.run(src.refresh())
    assert src.tip == 5003 and len(src.ts) == H.HISTORY_BLOCKS and seen[-1] == -12 and src.ts[-1] == 1e9 + 600.0 * 5003
    assert np.all(np.diff(src.ts) == 600.0), "the newest blocks replace the top, no hole and no double"
    fail["on"] = True
    src.read_at = time.time() - 40
    asyncio.run(src.refresh())
    assert src.tip == 5003, "what fails keeps its last answer"
    src.read_at = time.time() - H.STALE_AFTER - 1
    assert not src.ready


def test_a_read_tries_again_when_a_connection_times_out_and_stops_trying_in_the_end(monkeypatch):
    from glimpse_tui.data import http

    tries = []

    class Flaky:
        def __init__(self, fails):
            self.fails = fails

        async def get(self, url, params=None):
            tries.append(url)
            if len(tries) <= self.fails:
                raise httpx.ConnectTimeout("no")
            return httpx.Response(200, json=[{"data": [1.0]}], request=httpx.Request("GET", url))

    src = H.Inputs(H.Params())
    monkeypatch.setattr(http, "client", lambda url: Flaky(H.READ_TRIES - 1))
    assert asyncio.run(src._bulk("difficulty", "difficulty_epoch", 0)) == [{"data": [1.0]}] and len(tries) == H.READ_TRIES
    tries.clear()
    monkeypatch.setattr(http, "client", lambda url: Flaky(99))
    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(src._bulk("difficulty", "difficulty_epoch", 0))
    assert len(tries) == H.READ_TRIES


def test_one_run_serves_both_series_until_a_block_lands():
    src = live_inputs()
    now = float(TS.max()) + 60
    a = src.run(now, n=500)
    assert src.run(now + 5, n=500) is a
    src.ts = np.concatenate([src.ts, [TS.max() + 600]])
    src.diff = np.concatenate([src.diff, [src.diff[-1]]])
    assert src.run(now + 700, n=500) is not a


def test_the_shared_feed_is_ready_at_a_zero_percent_spot():
    feed = Feed("ADJUSTMENT")
    feed.parts = live_inputs()
    feed.bars = feed.parts.hourly_bars(500)
    feed.spot = 0.0
    assert feed.ready


# ── the page ────────────────────────────────────────────────


def test_the_page_draws_the_chain_both_tables_and_the_cover_price():
    from glimpse_tui.app import summarise

    src = live_inputs()
    now = float(TS.max()) + 60
    pane = DffPane(hub=None)
    pane.inputs, pane.state, pane.result = src, src.state(now), src.run(now, n=800)

    def views(series):
        b = BOOKS[series]
        P.STATED_ALPHA[b["topic"]] = b["alpha"]
        return [
            summarise(
                MarketRow(
                    b["topic"],
                    b["title"],
                    2_000_000_000,
                    "live",
                    0,
                    0,
                    tuple(b["shares"]),
                    tuple(b["names"]),
                    tuple(b["option_ids"]),
                    b["close_block"],
                    b["trading_stops_block"],
                )
            )
        ]

    pane.views, pane.adjustments = views("DIFFICULTY"), views("ADJUSTMENT")
    wide = "\n".join(t.plain for t in pane.draw(110, 30))
    for want in ("THE CHAIN", "THE DIFFICULTY", "THE ADJUSTMENT", "MINER COVER", "#975,744", "of schedule"):
        assert want in wide
    narrow = pane.draw(40, 30)
    assert narrow and all(t.no_wrap for t in narrow)
    for b in BOOKS.values():
        P.STATED_ALPHA.pop(b["topic"], None)


def test_cover_is_priced_on_the_real_book_and_a_sure_thing_is_worth_a_sat_a_sat():
    from glimpse_tui.app import summarise

    b = BOOKS["DIFFICULTY"]
    P.STATED_ALPHA[b["topic"]] = b["alpha"]
    v = summarise(MarketRow(b["topic"], b["title"], 2_000_000_000, "live", 0, 0, tuple(b["shares"]), tuple(b["names"]), tuple(b["option_ids"])))
    price, chance = cover_quote(v, [1.0 / 200] * 200, 155.0)
    assert price > 0 and 0.1 < chance < 0.2
    assert cover_quote(v, [1.0 / 200] * 200, 900.0) is None
    P.STATED_ALPHA.pop(b["topic"], None)


# ── the bench ───────────────────────────────────────────────


def history() -> L.History:
    """A synthetic chain on the real series' shape: on schedule, the difficulty flat."""
    n = 16 * H.EPOCH
    first = 330 * H.EPOCH - 3000
    ts = 1_600_000_000.0 + 600.0 * np.arange(n)
    return L.History(first, ts, ts, np.full(350, 1.0e14))


def test_scores_reward_being_right_and_being_sure_only_when_right():
    z = np.random.default_rng(0).standard_normal(3000)
    sharp, wide = 0.01 * z, 0.05 * z
    assert L.score(sharp, 0.001, 0.01)[1] > L.score(wide, 0.001, 0.01)[1]
    assert L.score(sharp, 0.03, 0.01)[1] < L.score(wide, 0.03, 0.01)[1]
    assert L.score(sharp, 0.0, 0.01)[0] < L.score(sharp, 0.05, 0.01)[0]
    assert L.score(sharp, 0.0, 0.01)[2] == 1.0 and L.score(sharp, 0.5, 0.01)[2] == 0.0


def test_a_forecast_row_is_scored_only_on_what_had_settled():
    h = history()
    L._init(h)
    tip_epoch = h.tip // H.EPOCH
    rows = L._sim_rows((340 * H.EPOCH + 100, H.Params(), 400, 0))
    assert len(rows) == min(H.MARKETS, len(h.epochs) - 1 - 340)
    assert rows[0][1] == "next, 1008-2016 blocks" and tip_epoch > 340
    assert L.bucket(0, 3) == "next, 0-36 blocks" and L.bucket(1, 2100) == "2nd" and L.bucket(11, 22_000) == "10th-12th"


def test_the_baselines_read_only_their_past():
    h = history()
    rows = L.site_rows(h, [340 * H.EPOCH + 100])
    assert rows and all(np.isfinite(r[3]) for r in rows)
    late = np.array(h.epochs)
    late[345:] *= 3.0  # a later retarget changes nothing an origin at epoch 340 may read, only what its far rows are scored on
    other = L.site_rows(L.History(h.first, h.ts, h.mono, late), [340 * H.EPOCH + 100])
    assert other[0] == rows[0], "the next retarget's score does not move"
    assert other[-1][2:] != rows[-1][2:], "the far retarget is scored on what really came"


def test_the_edge_interval_resamples_epochs():
    a = [(e, "2nd", 0, 0.5, 0, 0, 0, 0, 0, 0) for e in range(20)]
    b = [(e, "2nd", 0, 0.1, 0, 0, 0, 0, 0, 0) for e in range(20)]
    got = L.edge(a, b, 3)["2nd"]
    assert got[0] == pytest.approx(0.4) and got[1] == pytest.approx(0.4) and got[2] == pytest.approx(0.4)
    assert "2nd" not in L.edge(a[:3], b[:3], 3)


def test_a_hedge_case_settles_on_what_the_chain_did():
    h = history()
    L._init(h)
    p = H.Params()
    target_e = 340
    got = L._market(("ADJUSTMENT", target_e, "bot and miners", True, 0.0, p, 40_000.0))
    assert got["cover_cost"] > 0 and got["paid"] > 0
    # on schedule the retarget is 0% to +0.05%, which is under the cover's +2%: the cover pays nothing
    assert got["cover_pay"] == 0.0
    alone = L._market(("ADJUSTMENT", target_e, "miners, no bot", True, None, p, 40_000.0))
    assert alone["paid"] == 0 and alone["cover_cost"] > 0
