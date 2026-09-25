"""The runner against a fake API and synthetic candles: no network, no key, no orders."""
import dataclasses
import json
import time
from pathlib import Path

import numpy as np
import pytest
from zoo_contract import synthetic_bars

from glimpse_tui import app as A
from glimpse_tui import auth, bots, botsview, fmt
from glimpse_tui import policy as PL
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, Book, Fill, MarketRow, parse_bin
from glimpse_tui.zoo import core as C

FIX = json.loads((Path(__file__).parent / "fixtures" / "estimates.json").read_text())
BINS = [parse_bin(n) for n in FIX["names"]]


class Feed:
    """Synthetic candles whose last close sits inside the fixture market's ladder."""
    asset = "BTC"

    def __init__(self):
        bars = synthetic_bars()
        k = 76_500 / bars["close"].iloc[-1]
        self.bars = bars.assign(**{c: bars[c] * k for c in ("open", "high", "low", "close")})
        self.spot, self._scale, self._cache = 76_500.0, None, {}

    ready = True

    async def refresh(self, max_age=30.0): ...

    def ctx(self, bins, end, now=None):
        self._scale = self._scale or C.Scale.fit(self.bars)
        edges = np.array([bins[0][0]] + [hi for _, hi in bins], dtype=float)
        return C.Ctx(self.bars, self.spot, edges, time.time() if now is None else now, float(end), scale=self._scale, cache=self._cache)


class FakeApi:
    closes = 1                                                     # how many hourly closes the series holds

    def __init__(self, key=None, base_url=None):
        self.authenticated = bool(key)
        self.shares = list(FIX["shares"])
        self.bought, self.sold = [], []
        self.end = int(time.time()) + 2 * 3600
        self.ends = [self.end + i * 3600 for i in range(self.closes)]

    async def close(self): ...

    async def batches(self):
        return [Batch("b-h", "Hourly Bitcoin Prediction Markets", 3, 1000)]

    async def markets(self, batch_id, limit=96):
        return [MarketRow(100 + i, "Hourly Bitcoin Prediction Markets - x", e, "live", 0, 0, tuple(self.shares), tuple(FIX["names"]),
                          tuple(range(1, 501))) for i, e in enumerate(self.ends)]

    async def book(self, topic_id):
        b = Book(topic_id, "m", self.ends[topic_id - 100], "live", 0, list(range(1, 501)), BINS, list(self.shares), P.alpha_for(500))
        b.reprice()
        return b

    async def estimate_legs(self, topic_id, legs):
        q2 = self.shares[:]
        for o, c in legs:
            q2[o - 1] += c
        raw = P.cost(q2, P.alpha_for(500)) - P.cost(self.shares, P.alpha_for(500))
        return raw, raw * 0.02

    async def buy_legs(self, topic_id, legs):
        raw, fee = await self.estimate_legs(topic_id, legs)
        self.bought.append(legs)
        for o, c in legs:
            self.shares[o - 1] += c
        return Fill(topic_id, "t", raw, max(fee, 1.0))

    async def sell(self, topic_id, option_id, shares=None):
        self.sold.append((topic_id, option_id, shares))

    async def wallet(self): ...
    async def positions(self): return []
    async def summary(self): ...


def bot(index: int) -> bots.Bot:
    """Certain of one bin."""
    def forecast(book, closes, hours):
        p = [1e-9] * len(book.bins)
        p[index] = 1 - 1e-9 * (len(p) - 1)
        return p
    return bots.Bot("sure", "Sure Thing", "Mine", "certain of one bin", "test", forecast=forecast)


def runner(api, tmp_path, b, live=False, **cfg):
    return bots.Runner(bot=b, api=api, batch_id="b-h", feed=Feed(), live=live, ledger=bots.Ledger(tmp_path / "ledger.json"),
                       cfg=bots.BotConfig(**cfg))


async def test_dry_run_keeps_a_paper_ledger_and_does_not_buy_the_same_edge_every_cycle(tmp_path):
    api = FakeApi()
    r = runner(api, tmp_path, bot(90), kelly=1.0)
    r.cfg = r.cfg.with_budget(2_000)
    await r.cycle()
    assert api.bought == [] and r.trades == 1 and "would buy" in " ".join(r.log)
    first = r.open_cost
    assert 0 < first <= 500                                        # a quarter of the budget per cycle
    for _ in range(12):
        await r.cycle()
    assert r.open_cost <= 2_000 + 1e-6                            # never more at risk than the budget
    assert bots.Ledger(tmp_path / "ledger.json").open_cost("sure", "paper") == pytest.approx(r.open_cost)   # survives a restart


async def test_live_bot_buys_small_tickets_then_sells_when_the_market_overpays(tmp_path):
    api = FakeApi("k")
    r = runner(api, tmp_path, bot(90), live=True, bankroll_sats=60, kelly=1.0, max_per_cycle_sats=60, max_per_hour_sats=60)
    await r.cycle()
    assert len(api.bought) == 1 and 0 < r.paid_sats <= 60          # well under the old 100 sat floor
    held = r.ledger.legs("sure", "live", 100)
    assert list(held) == [91]
    r.bot = bot(10)                                                # the picture changes: bin 90 is now worth nothing to it
    api.shares[90] += 400                                          # and the market has bid it up
    await r.cycle()
    assert api.sold and api.sold[0][:2] == (100, 91) and api.sold[0][2] == pytest.approx(held[91][0])
    assert 91 not in r.ledger.legs("sure", "live", 100)


async def test_runner_never_sells_what_it_did_not_buy(tmp_path):
    api = FakeApi("k")
    r = runner(api, tmp_path, bot(10), live=True, bankroll_sats=0.5)   # no budget: it can only manage
    api.shares[90] += 400
    await r.cycle()
    assert api.sold == [] and api.bought == []


def spread_bot(policy) -> bots.Bot:
    """A wide bell around the fixture's spot, trading on an opportunistic policy."""
    def forecast(book, closes, hours):
        w = [np.exp(-0.5 * (((lo + hi) / 2 - 76_500) / 4_000) ** 2) + 1e-9 for lo, hi in book.bins]
        return [x / sum(w) for x in w]
    return bots.Bot("cheap", "Cheap", "Mine", "bargains", "test", forecast=forecast, policy=policy)


async def test_a_policy_bot_buys_only_cheap_ranges_in_its_region_and_a_holder_never_sells(tmp_path):
    api = FakeApi("k")
    pol = bots.Policy(max_price=5, min_ratio=1.3, region="above", stake=0.01, market_cap=0.10, hold=True)
    r = runner(api, tmp_path, spread_bot(pol), live=True, bankroll_sats=20_000, max_per_cycle_sats=5_000, max_per_hour_sats=10_000)
    before = (await api.book(100)).prices
    await r.cycle()
    assert len(api.bought) == 1
    legs = api.bought[0]
    assert all(BINS[o - 1][0] >= 76_500 and before[o - 1] <= 5 for o, _ in legs)          # above spot, and cheap
    held = r.ledger.legs("cheap", "live", 100)
    assert all(cost <= 200 + 1e-6 for _, cost in held.values())                           # 1% of the budget a range
    assert r.open_cost <= 2_000 + 1e-6                                                     # 10% in one close
    o = next(iter(held))
    api.shares[o - 1] += 5_000                                                             # the market bids one up hard
    await r.cycle()
    assert api.sold == []                                                                  # held to the close


async def test_a_trader_sells_only_the_overpaid_part_of_a_position(tmp_path):
    api = FakeApi("k")
    pol = bots.Policy(max_price=5, min_ratio=1.3, region="above", stake=0.01, market_cap=0.10)
    r = runner(api, tmp_path, spread_bot(pol), live=True, bankroll_sats=20_000, max_per_cycle_sats=5_000, max_per_hour_sats=10_000)
    await r.cycle()
    held = r.ledger.legs("cheap", "live", 100)
    o = max(held, key=lambda k: held[k][0])
    probs = r.bot.forecast(await api.book(100), [], 2.0)
    for _ in range(2_000):                                                                 # bid up just enough to overpay part of it
        api.shares[o - 1] += 1
        got = PL.sell_down(await api.book(100), probs, {o: held[o]}, r.cfg.exit_edge)
        if got:
            break
    assert got and got[0][1] < held[o][0]
    await r.cycle()
    sold = [x for x in api.sold if x[1] == o]
    assert sold and 0 < sold[0][2] < held[o][0]                                            # part of it
    assert r.ledger.legs("cheap", "live", 100)[o][0] == pytest.approx(held[o][0] - sold[0][2], abs=0.01)


def test_budget_sets_the_caps():
    c = bots.BotConfig().with_budget(400)
    assert (c.bankroll_sats, c.max_per_cycle_sats, c.max_per_hour_sats) == (400, 100, 200)


def test_every_zoo_model_is_a_bot_and_user_files_join_them(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "glimpse" / "bots"
    d.mkdir(parents=True)
    flat = '"""Every bin the same."""\ndef forecast(book, closes, hours):\n    return [1 / len(book.bins)] * len(book.bins)\n'
    (d / "flat.py").write_text(flat)
    (d / "broken.py").write_text("def forecast(:\n")
    found = {b.id: b for b in bots.discover()}
    assert len(found) > 100 and found["flat"].blurb == "Every bin the same." and found["broken"].error
    assert {"ema_crossover", "dist_merton_jumps", "opt_iron_condor", "view_bull", "rsi_reversion"} <= set(found)
    assert found["opp_longshot"].policy.hold and found["ema_crossover"].policy is None
    (d / "cheap.py").write_text('"""Cheap."""\nPOLICY = {"max_price": 2, "region": "below"}\n' + flat.split("\n", 1)[1])
    mine = {b.id: b for b in bots.user_bots()}["cheap"]
    assert mine.policy == bots.Policy(max_price=2, region="below") and "opportunistic" in dict(botsview.sections(mine))["runner"]


# ── the screen ──────────────────────────────────────────────

@pytest.fixture
def make(monkeypatch):
    def _make(key=None, closes=1):
        monkeypatch.setattr(A, "Glimpse", FakeApi)
        monkeypatch.setattr(FakeApi, "closes", closes)
        monkeypatch.setattr(auth, "load_key", lambda: (key, "env" if key else "none"))
        monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
        monkeypatch.setattr(A.Terminal, "feed", lambda self, asset=None: self.feeds.setdefault("BTC", Feed()))
        app = A.Terminal()
        app.spot = 76_500.0
        return app
    return _make


async def until(pilot, cond, tries=400):
    for _ in range(tries):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition never became true")


async def test_bots_screen_lists_the_zoo_reads_the_market_and_deploys_in_two_keys(make):
    app = make()
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        assert len(app.bots) > 100
        failed = {k: s.error for k, s in app.bot_scan.items() if s.error}
        assert not failed, failed
        listing = app.query_one("#botlist").render().plain
        assert "BASELINE" in listing and "All" in listing and "neutral" in listing
        await pilot.press("j", "down", "5", "j", "k")                  # vim keys, arrows and counts
        assert app.bot_cur == 6
        await pilot.press("s", "s", "w")                               # and WASD
        assert app.bot_cur == 7
        await pilot.press("l")                                         # next category
        assert botsview.TABS[app.bot_tab] == "Running" and botsview.visible(app) == []
        await pilot.press("right")
        assert botsview.TABS[app.bot_tab] == "Bullish" and all(app.bot_scan[b.id].view == "bullish" for b in botsview.visible(app))
        await pilot.press("a", "h", "g", "g")
        assert (app.bot_tab, app.bot_cur) == (0, 0)
        detail = app.query_one("#botdetail").render().plain
        assert "BTC at " in detail and "Bot expects" in detail and "price at close" in detail
        assert "market" in detail and "it buys" in detail                   # the market's prices on the bot's own bars
        assert "No API key loaded" in detail and "L log in" in detail
        assert "8 in 10 chance" in detail and "i how it works" in detail
        assert "h per column" in detail and "NOW" in detail and "walk the 1 closes" in detail   # the time series under the ladder
        assert any(len(ln) > 60 and ln.startswith("  ") for ln in detail.splitlines()[2:4])   # the idea, wrapped under the name
        await pilot.press("i")                                         # the model's account of itself
        await pilot.pause()
        about = app.screen
        assert isinstance(about, A.About) and about._title.startswith("ABOUT · ")
        page = about._body.plain
        assert "MATHS" in page and "MACHINERY" in page and "NOW" in page and "SCALE" in page
        await pilot.press("j", "k", "escape")
        await pilot.pause()
        assert app.screen is not about
        await pilot.press("enter")                                     # deploy…
        await pilot.press("enter")                                     # …accepting the budget shown
        first = app.bots[0]
        await until(pilot, lambda: first.id in app.runners and app.runners[first.id].cycles >= 1)
        r = app.runners[first.id]
        assert r.running and not r.live and r.cfg.bankroll_sats == 20_000
        log = app.query_one("#botlog")
        assert log.display and "ON PAPER" in log.border_title                  # the runner lives in its own container
        assert "stop this bot" in app.query_one("#botdetail").render().plain
        await pilot.press("x")
        assert not r.running


async def test_search_and_a_key_lets_a_bot_trade_real_sats_only_after_typing_live(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        await pilot.press("slash", *"iron condor", "enter")
        assert [b.id for b in botsview.visible(app)] == ["opt_iron_condor"]
        assert "through your API key glp_live_…abab" in app.query_one("#botdetail").render().plain
        await pilot.press("enter", *"400", "enter")                    # budget, then the real-or-paper question
        await pilot.press("enter")                                     # enter alone: paper
        await until(pilot, lambda: "opt_iron_condor" in app.runners)
        r = app.runners["opt_iron_condor"]
        assert not r.live and r.cfg.bankroll_sats == 400
        await pilot.press("x", "enter", "enter", *"LIVE", "enter")
        await until(pilot, lambda: app.runners["opt_iron_condor"] is not r)
        assert app.runners["opt_iron_condor"].live and app.runners["opt_iron_condor"].api is app.api
        await pilot.press("X", "escape")
        assert app.bot_filter == "" and app.view == "bots"


def _series(hours: int, drawn: int, now: float):
    """A synthetic hourly series: 300 candles of history, `hours` closes ahead, the first `drawn` of them pictured."""
    t0 = int(now // 3600) * 3600
    rng = np.random.default_rng(1)
    bars, p = [], 74_000.0
    for i in range(300):
        c = p * float(np.exp(rng.normal(0.0003, 0.006)))
        bars.append(A.Candle(t0 - (300 - i) * 3600, p, max(p, c) * 1.002, min(p, c) * 0.998, c))
        p = c
    closes, scans = [], {}
    edges = np.arange(p - 30_000, p + 30_000, 200.0)
    bins = tuple((float(a), float(a + 200)) for a in edges)
    for i in range(hours):
        end = t0 + (i + 1) * 3600 + 1800
        sig = p * 0.006 * np.sqrt(i + 1)
        row = MarketRow(100 + i, "x", end, "live", 0, 0, (), (), ())
        closes.append(A.RowView(row, p * (1 + 0.00005 * i), (p - 1.28 * sig, p + 1.28 * sig), bins=bins))
        if i < drawn:
            m = p * (1 + 0.0004 * i)
            w = np.exp(-0.5 * ((edges + 100 - m) / sig) ** 2)
            scans[100 + i] = botsview.Scan("bullish", 0.3, 1.0, 0.1, m, m - 1.28 * sig, m + 1.28 * sig, tuple(w / w.sum()))
    return bars, closes, scans, p


def test_the_time_series_chart_runs_history_into_the_bots_path():
    """Candles left of NOW, the bot's band and median right of it, the market's median through it, the chosen close
    marked, price rules labelled on the right, and a time scale that keeps the closes ahead in the finest columns
    that leave room for history."""
    now = time.time()
    bars, closes, scans, spot = _series(168, 120, now)
    hist_h = int((int(now // 3600) * 3600 - bars[0].t) // 3600)
    hpc, hc, fc = botsview.columns(closes, hist_h, now, 118 - botsview.GUTTER - 2)
    assert hpc == 3 and fc in (56, 57) and hc == 107 - 1 - fc                  # 168 closes at 3h a column; history fills the rest
    assert botsview.columns(closes[:12], hist_h, now, 107)[0] == 1            # a short series keeps hourly columns
    text = botsview.chart(bars, closes, scans, 30, spot, now, 118, 10)
    lines = text.plain.splitlines()
    assert len(lines) == 11 and all(len(ln) == 118 for ln in lines[:-1])      # rows plus the axis, every row full width
    body = "".join(lines[:-1])
    assert "█" in body and "░" in body and "─" in body and "·" in body          # candles, the chosen close, median, market
    assert lines[-1].count("NOW") == 1
    shades = {str(sp.style) for sp in text.spans if " on #" in str(sp.style)}
    assert len(shades) >= 4                                                    # the heat runs through several shades, not one
    assert f"◂{fmt.price(spot):>7}" in body                                    # spot labelled on its row
    assert sum("┤" in ln for ln in lines[:-1]) >= 2                            # round-number rules
    now_x = lines[-1].index("NOW")                                             # the axis carries the same two-space indent
    assert all(ln[now_x] == "│" for ln in lines[:-1])                         # the divider runs the full height
    assert not any("█" in ln[now_x + 1 + 45:now_x + 1 + fc] for ln in lines[:-1])   # nothing drawn past the 120 pictured closes
    packed = botsview.pack(bars, int(now // 3600) * 3600, 3, 4)
    assert all(k is not None for k in packed) and packed[0].c == bars[-1].c    # the column touching NOW ends on the last close
    assert packed[0].h == max(k.h for k in bars[-3:]) and packed[0].o == bars[-3].o
    empty = botsview.chart([], closes[:5], {}, 0, spot, now, 118, 6)           # no history and nothing drawn yet: still a chart
    assert "NOW" in empty.plain and "╎" in empty.plain


def test_the_detail_pane_shares_its_rows_between_ladder_and_chart():
    ladder, chart = botsview.layout(36)
    assert ladder == 10 and chart == 8 and ladder + chart + botsview.FIXED_LINES == 36
    assert botsview.layout(27) == (10, 0)                                         # too short for both: the ladder alone
    assert botsview.layout(60)[1] == botsview.CHART_MAX_ROWS


def test_about_wraps_every_section_of_a_model_with_its_stance():
    b = next(b for b in bots.discover() if b.id == "ema_crossover")
    s = botsview.Scan("bullish", 0.31, 0.97, 0.12, 76_939.0, 74_376.0, 79_651.0)
    text = botsview.about(b, s, 76_500.0, 120).plain
    for label in ("IDEA", "READS", "MATHS", "TRADES", "NOW", "MACHINERY", "SCALE", "RUNNER", "REFERENCE"):
        assert label in text, label
    assert "α = 2/(n + 1)" in text and "Brock, Lakonishok, LeBaron" in text        # the model's own maths and its reference
    assert b.name.upper() in text and "expects 76,939" in text and "+0.31σ" in text
    assert max(len(ln) for ln in text.splitlines()) <= 120
    mine = bots.Bot("flat", "flat", "Mine", "Every bin the same.", "Every bin the same.\n\nA docstring.", forecast=lambda *a: [])
    assert "MATHS" not in botsview.about(mine, None, 0.0, 80).plain and "A docstring." in botsview.about(mine, None, 0.0, 80).plain


@pytest.mark.parametrize("size", [(170, 46), (100, 40)])
async def test_the_about_page_wraps_to_the_width_the_dialog_really_has(make, size):
    """Every line of a section sits beside its label: none spills past the edge and back to column 0."""
    app = make()
    async with app.run_test(size=size) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning)
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, A.About) and app.screen._width)
        text = app.screen.query_one("#about-text")
        lines = app.screen._body.plain.splitlines()
        assert max(len(ln) for ln in lines) <= text.content_region.width
        assert text.virtual_size.height == len(app.screen._body.plain.split("\n"))   # the widget wrapped no line again


def test_the_detail_ladder_draws_the_bots_whole_picture_at_any_width():
    """Tight pictures are drawn bin by bin; wide ones add up neighbouring bins until the shape fits the rows."""
    bins = tuple((60_000.0 + 200 * i, 60_000.0 + 200 * (i + 1)) for i in range(500))
    mids = np.array([(a + b) / 2 for a, b in bins])
    for sigma, grouped in ((300.0, False), (900.0, True), (12_000.0, True)):
        w = np.exp(-(((mids - 76_500.0) / sigma) ** 2) / 2)
        probs = tuple(w / w.sum())
        lo, hi, step = botsview.window(probs, 13)
        assert (step > 1) is grouped
        assert sum(probs[lo:hi + 1]) > 0.99                              # the drawn span holds the picture
        rows = [ln for ln in botsview.ladder(probs, bins, 76_500.0, 100).plain.splitlines() if "–" in ln]
        assert 6 <= len(rows) <= 13
        assert sum("◂ now" in ln for ln in rows) == 1              # the price now sits in exactly one drawn range
        drawn = sum(float(ln.split("%")[0].split()[-1].replace("<", "")) for ln in rows)
        assert drawn > 99.0 if not grouped else drawn > 98.0             # the rows add up to the whole picture


async def test_the_forecast_pane_walks_every_close_ahead(make):
    """tab moves to the right-hand pane; there h l j k walk the closes, each drawing that bot's own picture of it."""
    app = make(closes=12)
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        await pilot.press("slash", *"donchian", "enter")
        assert len(app.bot_closes) == 12 and app.bot_pane == 0
        await until(pilot, lambda: len(app.bot_ahead) >= len(app.bots) + 11)      # the walk ahead ran for this bot
        await pilot.press("tab")
        assert app.bot_pane == 1 and app.query_one("#botdetail").has_class("active")
        assert "12 closes ahead" in app.query_one("#botdetail").border_title
        near = app.ahead_of(app._selected_bot(), app.bot_closes[0])
        far = app.ahead_of(app._selected_bot(), app.bot_closes[11])
        assert far.high - far.low > 2 * (near.high - near.low)                    # the picture widens with the horizon
        await pilot.press("j", "j", "j")                                          # j k walk a close at a time
        assert app.bot_when == 3 and botsview.TABS[app.bot_tab] == "All"          # not the category, in this pane
        detail = app.query_one("#botdetail").render().plain
        here = app.ahead_of(app._selected_bot(), app.bot_closes[3])
        assert fmt.question("BTC", app.bot_closes[3].row.end_time_utc) in detail and f"Bot expects {fmt.price(here.median)}" in detail
        assert "░" in detail and "█" in detail                                  # the chart marks this close inside the bot's heat
        assert "walk the" not in detail                                           # the tab hint goes while walking
        await pilot.press("l")                                                    # h l jump a day: past the last close
        assert app.bot_when == 11 and app.bot_day == 24
        await pilot.press("h")
        assert app.bot_when == 0
        await pilot.press("G")
        assert app.bot_when == 11
        await pilot.press("g", "g")
        assert app.bot_when == 0
        await pilot.press("escape")
        assert app.bot_pane == 0 and app.query_one("#botlist").has_class("active")
        await pilot.press("l")
        assert botsview.TABS[app.bot_tab] == "Running"                            # back in the list, l is the category


def test_the_trades_view_shades_what_the_bot_would_stake_and_zoom_widens_the_prices():
    now = time.time()
    bars, closes, scans, spot = _series(24, 24, now)
    for k, s in scans.items():                                                    # buys on the median's range, more as the close nears
        i = max(range(len(s.probs)), key=lambda j: s.probs[j])
        scans[k] = dataclasses.replace(s, buys=((i, 10.0, 200.0 - (k - 100) * 8),)) if k % 3 else dataclasses.replace(s, buys=())
    trades = botsview.chart(bars, closes, scans, 0, spot, now, 118, 12, "trades")
    forecast = botsview.chart(bars, closes, scans, 0, spot, now, 118, 12)
    greens = {str(sp.style) for sp in trades.spans if " on #" in str(sp.style)}
    assert len(greens) >= 3 and greens != {str(sp.style) for sp in forecast.spans if " on #" in str(sp.style)}
    shaded = lambda t: sum(" on #" in str(sp.style) for sp in t.spans)          # noqa: E731
    assert shaded(trades) < shaded(forecast)                                       # black where it buys nothing
    labels = lambda t: [float(ln.split("┤")[1].replace(",", "")) for ln in t.plain.splitlines() if "┤" in ln]  # noqa: E731
    wide = botsview.chart(bars, closes, scans, 0, spot, now, 118, 12, zoom=2)
    assert max(labels(wide)) - min(labels(wide)) > 1.8 * (max(labels(forecast)) - min(labels(forecast)))
    assert botsview.window([0.0] * 200 + [0.2] * 5 + [0.0] * 295, 10, zoom=3)[1] - botsview.window(
        [0.0] * 200 + [0.2] * 5 + [0.0] * 295, 10)[1] > 5                           # the ladder zooms out too


def test_a_256_colour_terminal_gets_exact_palette_heat_never_olive(monkeypatch):
    monkeypatch.setattr(botsview.charts, "truecolor", False)
    got = {botsview.heat(t / 20) for t in range(21)} | {botsview.heat(t / 20, trades=True) for t in range(21)}
    assert got <= set(botsview.HEAT_256) | set(botsview.BUY_HEAT_256) and "#5f5f00" not in got


async def test_m_flips_to_trades_and_b_bets_the_bots_order_once(make, tmp_path):
    app = make(key="glp_live_test", closes=3)
    async with app.run_test(size=(170, 46)) as pilot:
        app.ledger = bots.Ledger(tmp_path / "ledger.json")
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        await pilot.press("slash", *"discount sweep", "enter")
        b = app._selected_bot()
        assert b.id == "opp_discount_sweep"
        await until(pilot, lambda: all(app.ahead_of(b, v) and app.ahead_of(b, v).buys is not None for v in app.bot_closes))
        detail = app.query_one("#botdetail").render().plain
        assert "Would buy now" in detail and "FORECAST" in detail
        await pilot.press("m")
        assert app.bot_mode == "trades" and "TRADES" in app.query_one("#botdetail").render().plain
        await pilot.press("minus", "minus")
        assert app.bot_zoom == 2
        await pilot.press("0")
        assert app.bot_zoom == 0
        plan = app.ahead_of(b, app.bot_closes[0]).buys
        await pilot.press("b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        await pilot.press("y")
        await until(pilot, lambda: app.api.bought)
        assert len(app.api.bought) == 1 and {o - 1 for o, _ in app.api.bought[0]} == {i for i, _, _ in plan}
        await until(pilot, lambda: app.ledger.open_cost(b.id, "live") > 0)
        assert not app.runners                                                      # a snapshot, not a running bot
