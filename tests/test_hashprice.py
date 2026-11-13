"""The hashprice series and its bot. The oracle's identity is pinned to real blocks and Glimpse's two settled markets, local
prices to the server's own estimates, and the simulation to what the chain's rules say it must do."""
import asyncio
import json
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest
from zoo_contract import synthetic_bars

from glimpse_tui import api, bots, cli
from glimpse_tui import hashprice as H
from glimpse_tui import hashprice_lab as L
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, Book, MarketRow
from glimpse_tui.funcs.hashprice import HpfPane
from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import get
from glimpse_tui.zoo.hashprice import TRADING

FIX = Path(__file__).parent / "fixtures"
REAL = json.loads((FIX / "hashprice_blocks.json").read_text())
BOOK = json.loads((FIX / "estimates_hashprice.json").read_text())
NOW = float(max(REAL["timestamp"])) + 60.0


def real_blocks() -> H.Blocks:
    a = {k: np.array(REAL[k], dtype=float) for k in ("coinbase_usd", "coinbase", "fees", "difficulty", "timestamp")}
    return H.Blocks(REAL["first"], np.maximum.accumulate(a["timestamp"]), a["coinbase_usd"], a["coinbase"], a["fees"], a["difficulty"])


def bars_until(now: float, n: int = 1500, vol: float = 0.004) -> pd.DataFrame:
    b = synthetic_bars(n, vol=vol)
    b.index = pd.date_range(end=pd.Timestamp(int(now) // 3600 * 3600 - 3600, unit="s", tz="UTC"), periods=n, freq="h")
    return b.assign(difficulty=1.5e14)


def chain(height: int, difficulty: float = 1.5e14, seconds: float = 600.0, now: float = 1_800_000_000.0, n: int = 2000,
          price: float = 80_000.0, fee: float = 0.02) -> H.Blocks:
    """`n` blocks up to `height`, evenly spaced `seconds` apart, the last just before `now`."""
    ts = now - 60.0 - seconds * np.arange(n - 1, -1, -1.0)
    first = height - n + 1
    btc = H.subsidy_btc(first + np.arange(n)) + fee
    return H.Blocks(first, ts, btc * price, btc, np.full(n, fee), np.full(n, difficulty))


def state(height: int = 1_000_000 + 1500, seconds: float = 600.0, now: float = 1_800_000_000.0, **kw) -> H.State:
    return H.state_at(chain(height, seconds=seconds, now=now, **kw), bars_until(now), now)


def after(st: H.State, days: float) -> float:
    return (st.now // H.DAY + days) * H.DAY


# ── the oracle ──────────────────────────────────────────────

def test_the_identity_rebuilds_the_oracle_from_its_blocks():
    b = real_blocks()
    want = np.array(REAL["hash_price"], dtype=float)
    for i in range(len(want) - 1, 300, -37):           # a full day behind each block is in the fixture
        cut = H.Blocks(b.first, b.ts[: i + 1], b.usd[: i + 1], b.btc[: i + 1], b.fees[: i + 1], b.difficulty[: i + 1])
        assert cut.hashprice() == pytest.approx(want[i], abs=1e-4)


def test_a_market_settles_on_the_hourly_slot_ending_at_its_close():
    for m in BOOK["settled"]:
        assert m["resolved"] == pytest.approx(m["slot_ending_at_close"], abs=1e-6)
        assert m["resolved"] != pytest.approx(m["slot_after"], abs=1e-4)


def test_the_count_of_blocks_cancels_out_of_the_oracle():
    usual, lucky = chain(1_001_500), chain(1_001_500, seconds=300.0)
    assert usual.hashprice() == pytest.approx(lucky.hashprice(), rel=1e-9)
    assert len(usual.ts[usual.ts > usual.ts[-1] - H.DAY]) * 2 == pytest.approx(len(lucky.ts[lucky.ts > lucky.ts[-1] - H.DAY]), abs=2)


def test_the_subsidy_follows_the_schedule():
    assert H.subsidy_btc(974_000) == 3.125
    assert H.subsidy_btc(1_049_999) == 3.125
    assert H.subsidy_btc(1_050_000) == 1.5625


# ── the simulation ──────────────────────────────────────────

def test_a_close_hours_away_is_mostly_known_and_a_far_one_is_not():
    st = state()
    got = H.simulate(st, [after(st, 1), after(st, 5), after(st, 20)], n=2000)
    spreads = [float(np.std(np.log(got[after(st, d)]))) for d in (1, 5, 20)]
    assert spreads[0] < spreads[1] < spreads[2]
    # only the part of the window still to come can move: a walk from an hour's noise would be several times wider
    assert spreads[0] < 0.02


def test_the_same_inputs_and_seed_draw_the_same_readings():
    st = state()
    a, b = H.simulate(st, [after(st, 3)], n=500, seed=7), H.simulate(st, [after(st, 3)], n=500, seed=7)
    assert np.array_equal(a[after(st, 3)], b[after(st, 3)])


def test_a_slow_epoch_lifts_the_hashprice_at_the_retarget_and_the_rule_is_what_does_it():
    # 1,565 blocks of the epoch at 660 seconds apiece: 10% slow, so difficulty is due to fall by about that
    st = state(seconds=660.0)
    assert st.blocks_left == 451
    _, change = st.retarget_estimate()
    assert change == pytest.approx(600 / 660 - 1, abs=0.01)
    p = H.Params(hash_sigma_day=0.0, vol_of_vol=0.0, elasticity=0.0, drift_base=0.0, drift_trend=0.0, vol_mult=0.01)
    far, near = after(st, 10), after(st, 1)
    got = H.simulate(st, [near, far], p, n=500)
    assert np.median(got[far]) / np.median(got[near]) == pytest.approx(1 / (1 + change), rel=0.02)
    frozen = H.simulate(st, [near, far], p.overlay(retarget=False), n=500)
    assert np.median(frozen[far]) / np.median(frozen[near]) == pytest.approx(1.0, abs=0.003)


def test_a_halving_lands_on_its_block():
    st = state(height=H.HALVING * 5 - 300)
    calm = H.Params(hash_sigma_day=0.0, vol_of_vol=0.0, elasticity=0.0, drift_base=0.0, drift_trend=0.0, vol_mult=0.01, fee_sigma_day=0.0)
    got = H.simulate(st, [after(st, 1), after(st, 6)], calm, n=300)
    assert np.median(got[after(st, 6)]) / np.median(got[after(st, 1)]) == pytest.approx(0.5, abs=0.04)


def test_the_blocks_already_mined_go_in_as_they_are():
    # the price has doubled since yesterday's blocks: a close an hour on is still almost all yesterday's blocks
    st = H.state_at(chain(1_001_500), bars_until(1_800_000_000.0), 1_800_000_000.0, price=160_000.0)
    close = (st.now // H.HOUR + 1) * H.HOUR
    calm = H.Params(vol_mult=0.01, vol_of_vol=0.0, jitter=0.0)
    seen = np.median(H.simulate(st, [close], calm, n=300)[close])
    blind = np.median(H.simulate(st, [close], calm.overlay(known=False), n=300)[close])
    assert seen < 1.2 * st.hashprice < blind


def test_the_shocks_carry_no_drift_from_the_bars_they_came_from():
    bars = bars_until(1_800_000_000.0)
    up = bars.assign(close=bars["close"].to_numpy() * np.exp(np.arange(len(bars)) * 0.002))
    st = H.state_at(chain(1_001_500), up, 1_800_000_000.0)
    far = after(st, 20)
    got = H.simulate(st, [far], H.Params(elasticity=0.0, drift_base=0.0, drift_trend=0.0, hash_sigma_day=0.0), n=4000)[far]
    assert np.median(got) == pytest.approx(st.hashprice, rel=0.03)


def test_a_past_close_is_left_out():
    st = state()
    assert H.simulate(st, [st.now - 3600]) == {}


def test_probabilities_fill_the_ladder_and_sum_to_one():
    x = np.random.default_rng(1).lognormal(np.log(34.0), 0.03, 3000)
    p = H.probabilities(x, L.LADDER)
    assert p.shape == (1000,) and np.all(p > 0) and p.sum() == pytest.approx(1.0)
    assert 0.75 < p[(L.LADDER[:-1] >= 32.6) & (L.LADDER[1:] <= 35.5)].sum() < 0.9
    assert p[0] < 1e-8                                  # the bin from zero, whose edge has no logarithm


# ── the series on the API ───────────────────────────────────

def test_the_title_names_the_series_before_the_word_bitcoin_does():
    b = Batch("x", "Bitcoin Hashprice · Daily", 26, 0.1)
    assert (b.asset, b.short, b.hourly) == ("HASHPRICE", "HASHPRICE", False)
    assert api.asset_of("Bitcoin hashprice (daily) on 6 Nov 2026, 00:00 UTC") == "HASHPRICE"
    assert api.asset_of("Hourly Bitcoin Prediction Markets") == "BTC"
    assert "HASHPRICE" in api.ASSETS and "HASHPRICE" in api.ORACLE


def test_the_ladder_reads_in_dollars():
    assert api.dollars("3570-3580") == "35.70-35.80"
    assert api.range_name("3570-3580", P.DMARKET_TOPIC) == "35.70-35.80"
    assert api.range_name("3570-3580", 8000) == "3570-3580"
    assert api.parse_bin(api.dollars("0-10")) == (0.0, 0.1)


def test_local_prices_match_the_servers_estimates_on_the_real_ladder():
    assert P.alpha_for(1000, BOOK["topic"]) == pytest.approx(BOOK["alpha"], rel=1e-12)
    assert P.alpha_for(1000) != pytest.approx(BOOK["alpha"], rel=0.1)    # the main service's cap would be 2.2 times off
    q = BOOK["shares"]
    at = {o: i for i, o in enumerate(BOOK["option_ids"])}
    for e in BOOK["estimates"]:
        moved = list(q)
        moved[at[e["option"]]] += e["contracts"]
        local = P.cost(moved, BOOK["alpha"]) - P.cost(q, BOOK["alpha"])
        assert local == pytest.approx(e["cost_millisats"] / 1000, abs=0.01)


class SdkBatch:
    def __init__(self, batch_id, title, interval=10):
        self.batch_id, self.main_topic_title, self.topic_count, self.outcome_interval = batch_id, title, 3, interval


class SdkBatches:
    def __init__(self, *batches):
        self.batches = list(batches)


def with_batches(main, second):
    g = api.Glimpse()

    async def pub():
        return SdkBatches(*main)

    async def dpub(path, params=None):
        if isinstance(second, Exception):
            raise second
        return second

    g._pub.batches, g._dpub._get = pub, dpub
    return g


def test_the_series_list_gains_hashprice_alone_and_survives_its_service_being_down():
    main = [SdkBatch("b", "Hourly Bitcoin Prediction Markets", 200)]
    second = {"batches": [{"batch_id": "h", "topic_type": "hashprice_daily", "main_topic_title": "Bitcoin Hashprice · Daily", "outcome_interval": 10},
                          {"batch_id": "x", "topic_type": "none_we_know", "main_topic_title": "Something else"}]}
    got = asyncio.run(with_batches(main, second).batches())
    assert [b.short for b in got] == ["BTC", "HASHPRICE"]
    assert got[1].interval == pytest.approx(0.1)
    assert "h" in api._DM_BATCHES and "b" not in api._DM_BATCHES
    down = asyncio.run(with_batches(main, httpx.ConnectError("no")).batches())
    assert [b.short for b in down] == ["BTC"]


def test_the_second_service_gets_its_own_prefix_and_an_idempotency_key(monkeypatch):
    monkeypatch.setenv(api.DMARKET_TOKEN_ENV, "session-token")
    seen = []

    def answer(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"results": []})

    g = api.Glimpse("a-key")

    async def go():
        g._dc._http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        await g.buy_many([(P.DMARKET_TOPIC + 2, [(351, 5.0)])])
        await g._dc._http.aclose()

    asyncio.run(go())
    req = seen[0]
    assert req.url.host == "difficulty.bpmapi.io"
    assert req.url.path == "/api/v1/dmarket/enter-multi-topic-multi-leg"
    assert req.headers["Authorization"] == "session-token" and req.headers["Idempotency-Key"]


def test_a_refusal_from_the_second_service_never_reads_as_a_bad_key():
    def refuse(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Authorization header is required"})

    g = api.Glimpse("a-key")

    async def go():
        g._dc._http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        g._c._http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        assert not await g.can_trade_second_service()
        with pytest.raises(api.NoServiceAuth, match="Paper trading works"):
            await g.buy_many([(P.DMARKET_TOPIC + 2, [(351, 5.0)])])
        with pytest.raises(api.KeyRejected):                     # the main service's refusal still means the key
            await g.buy_many([(8000, [(1, 5.0)])])
        await g._dc._http.aclose()
        await g._c._http.aclose()

    asyncio.run(go())


def test_a_trailing_average_gets_a_wider_clock_than_its_hourly_moves_say():
    now = 1_800_000_000.0
    bars = bars_until(now, 2000)
    # a smooth series: a 24-hour average of a walk, whose hourly moves understate its week
    smooth = bars.assign(close=np.exp(pd.Series(np.log(bars["close"].to_numpy())).rolling(24, min_periods=1).mean().to_numpy()))
    hourly, trailing = C.Scale.fit(smooth, asset="BTC"), C.Scale.fit(smooth, asset="HASHPRICE")
    assert trailing.s2_next > 4 * hourly.s2_next
    assert trailing.quiet == 1.0 and np.all(trailing.profile == 1.0)


# ── the bot ─────────────────────────────────────────────────

class Parts(H.Inputs):
    """Live inputs with the network left out."""

    def __init__(self, st_now: float, height: int = 1_001_500, **kw) -> None:
        super().__init__(H.Params())
        self.blocks, self.bars = chain(height, now=st_now, **kw), bars_until(st_now)
        self.price = 80_000.0
        self.read_at = time.time()

    async def refresh(self) -> None: ...


def ladder() -> list[tuple[float, float]]:
    return [api.parse_bin(api.dollars(n)) for n in BOOK["names"]]


def test_the_bot_draws_from_its_parts_and_refuses_without_them():
    now = time.time()
    m = get("hashprice")
    edges = np.array([0.0] + [hi for _, hi in ladder()])
    bars = bars_until(now)
    ctx = C.Ctx(bars=bars, spot=34.0, edges=edges, now=now, end=float((now // H.DAY + 3) * H.DAY), asset="HASHPRICE")
    with pytest.raises(C.ModelUnavailable, match="HASHPRICE"):
        m.fn(ctx)
    ctx.parts = Parts(now)
    p = m.fn(ctx)
    assert p.shape == (1000,) and p.sum() == pytest.approx(1.0) and np.all(p > 0)
    middle = float(edges[np.argmax(np.cumsum(p) >= 0.5)])
    assert middle == pytest.approx(ctx.parts.state(now).hashprice, rel=0.1)
    assert np.allclose(p, m.fn(ctx))                     # one run serves every visit until a block lands
    ctx.parts.read_at = 0.0
    with pytest.raises(C.ModelUnavailable, match="out of date"):
        m.fn(ctx)


def test_one_simulation_reads_every_close(monkeypatch):
    now = time.time()
    src = Parts(now)
    runs = []
    real = H.simulate

    def counted(*a, **kw):
        runs.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(H, "simulate", counted)
    for k in range(1, 15):
        src.samples(now, float((now // H.DAY + k) * H.DAY), n=300)
    assert len(runs) == 1
    src.blocks = chain(1_001_501, now=now)                # a block lands
    src.samples(now, float((now // H.DAY + 1) * H.DAY), n=300)
    assert len(runs) == 2


def test_new_blocks_lay_over_the_old_by_height():
    b = chain(1_001_500, n=H.HISTORY_BLOCKS)
    d = {"first": 1_001_495, "timestamp": b.ts[-6:] + 1.0, "coinbase_usd": np.full(6, 9.0), "coinbase": np.full(6, 3.0),
         "fees": np.full(6, 0.0), "difficulty": np.full(6, 2e14)}
    d = {**d, **{k: np.concatenate([v, v[-1:]]) for k, v in d.items() if k != "first"}}    # and one new block on top
    m = H.merged(b, d)
    assert m.height == 1_001_501 and len(m.ts) == H.HISTORY_BLOCKS
    assert m.usd[-1] == 9.0 and m.difficulty[-7] == 2e14 and m.difficulty[-8] == 1.5e14


class FakeApi:
    closes = 20

    def __init__(self):
        self.authenticated = False
        now = int(time.time())
        self.ends = [(now // 86400 + 1 + i) * 86400 for i in range(self.closes)]
        self.names = tuple(api.dollars(n) for n in BOOK["names"])

    async def close(self): ...

    async def all_markets(self, batch_id):
        return [MarketRow(P.DMARKET_TOPIC + i, f"Bitcoin hashprice (daily) {i}", e, "live", 0, 0, tuple(BOOK["shares"]), self.names,
                          tuple(BOOK["option_ids"])) for i, e in enumerate(self.ends)]

    async def book(self, topic_id):
        b = Book(topic_id, "m", self.ends[topic_id - P.DMARKET_TOPIC], "live", 0, list(BOOK["option_ids"]), ladder(), list(BOOK["shares"]),
                 P.alpha_for(1000, topic_id))
        b.reprice()
        return b


class HashFeed:
    asset = "HASHPRICE"
    ready = True

    def __init__(self):
        self.now = time.time()
        self.parts = Parts(self.now)
        self.spot = self.parts.blocks.hashprice()

    async def refresh(self, max_age: float = 30.0) -> None: ...

    def ctx(self, bins, end, now=None):
        edges = np.array([bins[0][0]] + [hi for _, hi in bins], dtype=float)
        bars = bars_until(self.now, 500)
        return C.Ctx(bars, self.spot, edges, time.time() if now is None else now, float(end), asset="HASHPRICE",
                     scale=C.Scale.fit(bars, asset="HASHPRICE"), parts=self.parts)


def test_a_paper_visit_buys_only_under_value_on_the_near_closes():
    bot = next(b for b in bots.discover() if b.id == "hashprice")
    assert bot.only_series == "HASHPRICE"
    logs = []
    r = bots.Runner(bot=bot, api=FakeApi(), batch_id="h", feed=HashFeed(), cfg=bots.BotConfig().with_budget(100_000), series="HASHPRICE",
                    on_log=logs.append)
    asyncio.run(r.cycle())
    assert r.cfg.objective == "edge" and r.cfg.min_edge == TRADING["min_edge"]
    assert r.of == TRADING["markets"]                     # further out the backtest's edge is not reliably there: it does not trade
    assert r.step == 1 and any("HASHPRICE at" in x for x in logs)
    assert r.paid_sats <= 100_000 / TRADING["markets"] + 1
    book, feed = asyncio.run(r.api.book(P.DMARKET_TOPIC)), r.feed
    probs = bot.probs(book, feed.ctx(book.bins, book.end_time_utc))
    for _end, _, _topic, option, _, _, contracts, cost in r.holdings():
        i = BOOK["option_ids"].index(option)
        assert contracts > 0 and cost / contracts <= probs[i] * P.PAYOUT_SATS * (1 - P.FEE) / (1 + TRADING["min_edge"]) * 1.5


def test_the_command_line_keeps_the_bot_on_its_series_and_off_a_service_that_refuses(monkeypatch, capsys):
    class Client:
        authenticated = True

        def __init__(self, *a): ...

        async def close(self): ...

        async def batches(self):
            return [Batch("h", "Bitcoin Hashprice · Daily", 26, 0.1, "HASHPRICE"), Batch("b", "Hourly Bitcoin Prediction Markets", 3, 200)]

        async def can_trade_second_service(self):
            return False

    monkeypatch.setattr(cli, "Glimpse", Client)
    monkeypatch.setattr(cli.auth, "load_key", lambda: ("k", "env"))
    monkeypatch.setenv(cli.LIVE_ENV, "I_UNDERSTAND")
    assert cli.main(["run", "hashprice", "--series", "BTC", "--once"]) == 2
    assert "only draw HASHPRICE" in capsys.readouterr().err
    assert cli.main(["run", "hashprice", "--live", "--once"]) == 2
    assert "did not accept these credentials" in capsys.readouterr().err


def test_it_is_listed_only_on_its_own_series():
    from types import SimpleNamespace

    from glimpse_tui.botsview import visible

    ours = bots.discover()
    screen = SimpleNamespace(bots=ours, bot_scan={}, bot_tab=0, bot_filter="", bot_sort=0, runners={}, asset="BTC")
    assert "hashprice" not in {b.id for b in visible(screen)}
    screen.asset = "HASHPRICE"
    assert "hashprice" in {b.id for b in visible(screen)}


def test_the_about_screen_describes_its_own_machinery():
    sections = dict(C.explain(get("hashprice")))
    assert "scale" not in sections and "Poisson" in sections["machinery"] and "10 nearest closes" in sections["trades"]


def test_the_page_shows_the_oracle_its_parts_and_every_close():
    from glimpse_tui.app import summarise

    now = time.time()
    pane = HpfPane(hub=None)
    pane.inputs = Parts(now)
    end = int((now // H.DAY + 2) * H.DAY)
    row = MarketRow(P.DMARKET_TOPIC, "Bitcoin hashprice", end, "live", 0, 0, tuple(BOOK["shares"]), tuple(api.dollars(n) for n in BOOK["names"]),
                    tuple(BOOK["option_ids"]))
    pane.views = [summarise(row)]
    pane.state, pane.bands = pane._forecast([end])
    wide = "\n".join(t.plain for t in pane.draw(100, 30))
    assert "THE ORACLE" in wide and "next retarget" in wide and "#1,001,952" in wide and "80% band" in wide
    assert pane.views[0].median < 100 and f"{pane.bands[float(end)][1]:.2f}" in wide
    narrow = pane.draw(40, 30)
    assert narrow and all(t.no_wrap for t in narrow)


# ── the bench ───────────────────────────────────────────────

def small_history(days: int = 60) -> L.History:
    """A synthetic chain on the real series' shape: a block every ten minutes, a flat market."""
    end = 1_800_000_000 // 3600 * 3600
    n = days * 144
    first = 1_001_000
    ts = end - 600.0 * np.arange(n - 1, -1, -1.0)
    rng = np.random.default_rng(3)
    price = 80_000.0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    blk = {"timestamp": ts, "coinbase": np.full(n, 3.14), "coinbase_usd": 3.14 * price, "fees": np.full(n, 0.015),
           "difficulty": np.full(n, 1.5e14)}
    h = L.history(blk, {"timestamp": ts[5::6] - 3600.0, "price_close": price[5::6], "difficulty": np.full(n // 6, 1.5e14),
                        "hash_price": H.K * 3.14 * price[5::6] / 1.5e14})
    return L.History(H.Blocks(first, ts, blk["coinbase_usd"], blk["coinbase"], blk["fees"], blk["difficulty"]), h.bars)


def test_a_backtest_forecast_knows_only_what_was_known_at_its_origin():
    h = small_history()
    now = float(h.bars.index[-1].timestamp()) - 20 * H.DAY
    st, closes, settled, at, past = L.case_at(h, now)
    assert at == now and st.blocks.ts[-1] <= now
    assert len(past) >= H.MIN_HOURS and h.bars.index.as_unit("s").asi8[len(h.bars) - 1] > now
    assert np.all(closes > now) and np.all(closes % H.DAY == 0) and len(closes) == len(settled) <= 24


def test_the_score_rewards_a_sharp_right_answer_and_edge_says_when_it_is_luck():
    x = np.random.default_rng(1).normal(34.0, 0.05, 2000)
    sharp, wide = L.score_samples(x, 34.0), L.score_samples(x * np.exp(np.random.default_rng(2).normal(0, 0.05, 2000)), 34.0)
    assert sharp[0] < wide[0] and sharp[1] > wide[1] and sharp[2] == 1.0
    b = L.BUCKETS[0]
    good = {b: [(k, (0.0, -1.0, 1.0, 0.0)) for k in range(70)]}
    luck = np.random.default_rng(4).normal(0, 0.3, 70)
    even = {b: [(k, (0.0, -1.0 - float(luck[k]), 1.0, 0.0)) for k in range(70)]}
    worse = {b: [(k, (0.0, -1.5, 1.0, 0.0)) for k in range(70)]}
    assert L.edge(good, worse)[b][1] > 0.4
    assert L.edge(good, even)[b][1] < 0 < L.edge(good, even)[b][2]


def test_the_bench_scores_the_simulation_against_the_walks():
    h = small_history()
    last = float(h.bars.index[-1].timestamp())
    cases = L.cases_between(h, last - 26 * H.DAY, last, 96.0)
    assert cases
    sim = L.means(L.evaluate(cases, H.Params(), paths=200))
    walk = L.means(L.evaluate_walk(cases, "96h"))
    assert sim and walk and all(np.isfinite(v).all() for v in sim.values())
    lines = L.table({"sim": sim, "walk": walk})
    assert any("80% band" in x for x in lines)


def test_fitted_parameters_are_kept_and_read_back():
    p = H.Params(vol_mult=1.3)
    f = H.save_params(p)
    assert f.name == "params.json" and H.load_params().vol_mult == 1.3
    f.write_text('{"vol_mult": 0.9, "not_a_parameter": 1}')
    assert H.load_params().vol_mult == 0.9
    f.write_text("not json")
    assert H.load_params() == H.Params()
