"""The runner against a fake API and synthetic candles: no network, no key, no orders."""
import json
import time
from pathlib import Path

import numpy as np
import pytest
from zoo_contract import synthetic_bars

from glimpse_tui import app as A
from glimpse_tui import auth, bots, botsview, fmt
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
    for i in range(hours):
        end = t0 + (i + 1) * 3600 + 1800
        sig = p * 0.006 * np.sqrt(i + 1)
        row = MarketRow(100 + i, "x", end, "live", 0, 0, (), (), ())
        closes.append(A.RowView(row, p * (1 + 0.00005 * i), (p - 1.28 * sig, p + 1.28 * sig)))
        if i < drawn:
            m = p * (1 + 0.0004 * i)
            scans[100 + i] = botsview.Scan("bullish", 0.3, 1.0, 0.1, m, m - 1.28 * sig, m + 1.28 * sig)
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
    assert "█" in body and "░" in body and "─" in body and "·" in body          # candles, band, median, market
    assert "▒" in body and lines[-1].count("NOW") == 1                          # the chosen close, one NOW
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
    assert ladder == 10 and chart == 9 and ladder + chart + botsview.FIXED_LINES == 36
    assert botsview.layout(26) == (10, 0)                                         # too short for both: the ladder alone
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
        assert "▒" in detail and "░" in detail and "█" in detail                # the chart marks this close inside the bot's band
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


async def test_runner_emits_one_structured_event_per_step_and_none_without_a_listener(tmp_path):
    api = FakeApi()
    quiet = runner(api, tmp_path, bot(90), kelly=1.0)
    quiet.cfg = quiet.cfg.with_budget(2_000)
    await quiet.cycle()                                               # no on_event: exactly the old behaviour
    assert quiet.trades == 1

    events = []
    r = runner(FakeApi(), tmp_path / "b", bot(90), kelly=1.0)
    r.cfg = r.cfg.with_budget(2_000)
    r.on_event = events.append
    await r.cycle()
    assert [e["type"] for e in events] == ["cycle_start", "forecast", "candidates", "intent_buy", "estimate", "fill"]
    fill = events[-1]
    assert fill["bot"] == "sure" and fill["mode"] == "paper" and fill["cycle"] == 1 and fill["topic_id"] == 100
    assert fill["data"]["paper"] is True and fill["data"]["side"] == "buy"
    legs = fill["data"]["legs"]
    assert legs and legs[0]["index"] == 90 and legs[0]["option_id"] == 91 and (legs[0]["lo"], legs[0]["hi"]) == BINS[90]
    assert sum(x["cost_sats"] for x in legs) == pytest.approx(fill["data"]["paid_sats"])
    assert sum(x["cost_sats"] for x in legs) == pytest.approx(r.open_cost)      # the event is the ledger
    est = events[4]["data"]
    assert est["ok"] and abs(est["local_sats"] - est["server_sats"]) <= est["tolerance_sats"]
    assert len(events[1]["data"]["probs"]) == 500

    r.bot = bot(10)                                                   # the market overpays for bin 90 now
    r.api.shares[90] += 400
    events.clear()
    await r.cycle()
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["cycle_start", "forecast"] and "intent_sell" in kinds and "fill" in kinds
    sell = next(e for e in events if e["type"] == "fill")
    assert sell["data"]["side"] == "sell" and sell["data"]["option_id"] == 91
    assert 91 not in r.ledger.legs("sure", "paper", 100)


async def test_a_subclass_can_bend_the_picture_and_veto_the_legs(tmp_path):
    class Bent(bots.Runner):
        async def picture(self, book, ctx):
            p = await super().picture(book, ctx)
            return p[1:] + p[:1]                                      # everything one bin down: bin 90 is now bin 89

        def veto(self, book, legs):
            self.vetoed = list(legs)
            return []

    api = FakeApi()
    events = []
    r = Bent(bot=bot(90), api=api, batch_id="b-h", feed=Feed(), ledger=bots.Ledger(tmp_path / "l.json"), cfg=bots.BotConfig(kelly=1.0))
    r.cfg = r.cfg.with_budget(2_000)
    r.on_event = events.append
    await r.cycle()
    assert r.vetoed and r.vetoed[0].index == 89
    assert r.trades == 0 and r.open_cost == 0 and api.bought == []
    assert [e["type"] for e in events] == ["cycle_start", "forecast", "candidates"]


async def test_an_ambiguous_fill_is_a_typed_event_and_stops_the_bot(tmp_path):
    from glimpse_tui.api import AmbiguousTrade

    class Drops(FakeApi):
        async def buy_legs(self, topic_id, legs):
            raise AmbiguousTrade("Connection dropped mid-trade. The order may or may not have filled: check the portfolio before retrying.")

    events = []
    r = runner(Drops("k"), tmp_path, bot(90), live=True, bankroll_sats=60, kelly=1.0, max_per_cycle_sats=60, max_per_hour_sats=60)
    r.on_event = events.append
    stopped = []
    r.stop = lambda: stopped.append(True)
    await r.cycle()
    unknown = [e for e in events if e["type"] == "fill_unknown"]
    assert len(unknown) == 1 and unknown[0]["data"]["side"] == "buy" and unknown[0]["data"]["legs"]
    assert stopped and r.open_cost == 0 and "fill" not in [e["type"] for e in events]


def test_prune_returns_what_it_removed(tmp_path):
    led = bots.Ledger(tmp_path / "l.json")
    book = Book(7, "m", 100, "live", 0, [1, 2], [(1.0, 2.0), (2.0, 3.0)], [40.0, 40.0], P.alpha_for(2))
    led.add("a", "paper", book, "BTC", 1, 3.0, 12.0)
    assert led.prune(50) == {}
    gone = led.prune(100)
    assert gone == {"a": {"paper": {"7": {"end": 100, "asset": "BTC", "legs": {"2": [3.0, 12.0, 2.0, 3.0]}}}}}
    assert led.positions("a", "paper") == []
