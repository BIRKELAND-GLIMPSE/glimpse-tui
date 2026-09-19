"""Drives the real app against a fake API: no network, no key, no orders."""
import json
from pathlib import Path

import pytest

from glimpse_tui import app as A
from glimpse_tui import auth
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, Book, Fill, MarketRow, Position, Summary, Wallet, parse_bin

FIX = json.loads((Path(__file__).parent / "fixtures" / "estimates.json").read_text())


class FakeApi:
    def __init__(self, key=None, base_url=None):
        self.authenticated = bool(key)
        self.bought = []
        self.sold = []
        self.drift = 0.0

    async def close(self): ...

    async def batches(self):
        return [Batch("b-btc", "Daily Bitcoin Markets", 3, 1000), Batch("b-eth", "Daily Ethereum Market", 3, 30)]

    async def markets(self, batch_id, limit=96):
        return [MarketRow(100 + i, f"Daily Bitcoin Markets - d{i}", 2**31 - i, "live", 0, 0,
                          tuple(FIX["shares"]), tuple(FIX["names"])) for i in range(3)]

    async def book(self, topic_id):
        b = Book(topic_id, f"market {topic_id}", 2**31, "live", 5_000_000, list(range(1, 501)),
                 [parse_bin(n) for n in FIX["names"]], list(FIX["shares"]), P.alpha_for(500))
        b.reprice()
        return b

    async def estimate(self, book, lo, hi, contracts):
        t = P.ticket(book.shares, book.alpha, lo, hi, contracts)
        raw = (t.cost_sats - t.fee_sats) * (1 + self.drift)
        return raw, raw * 0.02

    async def wallet(self):
        return Wallet(50_000, 1_000, 100_000)

    async def positions(self):
        return [Position(100, 77, "market 100", "76000-77000", 10, 400, 450, 2**31, "active")]

    async def summary(self):
        return Summary(400, 450, 50, 1, 0, 0)

    async def buy(self, book, lo, hi, contracts):
        self.bought.append((book.topic_id, lo, hi, contracts))
        return Fill(book.topic_id, "t", 100, 2)

    async def sell(self, topic_id, option_id, shares=None):
        self.sold.append((topic_id, option_id))


@pytest.fixture
def make(monkeypatch):
    def _make(key):
        monkeypatch.setattr(A, "Glimpse", FakeApi)
        monkeypatch.setattr(auth, "load_key", lambda: (key, "env" if key else "none"))
        monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
        return A.Terminal()
    return _make


async def until(pilot, cond, tries=150):
    """Wait for a condition rather than a guessed interval: workers run at their own pace under load."""
    for _ in range(tries):
        if cond():
            return
        await pilot.pause(0.03)
    raise AssertionError("condition never became true")


async def ready(app, pilot):
    for _ in range(40):
        await pilot.pause(0.05)
        if app.book:
            return


async def test_vim_navigation_and_range_selection(make):
    app = make(None)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        mode = app.b_cur
        assert app.book.probs[mode] == max(app.book.probs)      # opens on the most likely range
        await pilot.press("j")
        assert app.m_cur == 1
        await pilot.press("down", "G")
        assert app.m_cur == 2
        await pilot.press("g", "g", "l", "k", "k", "v", "j", "j", "j", "j")
        assert (app.m_cur, app.pane) == (0, 1)
        await ready(app, pilot)
        assert app.selection == (mode - 2, mode + 2)            # k is up the ladder, which is up in price
        assert app.ticket.bins == 5
        await pilot.press("plus", "plus", "minus")
        assert app.contracts == 42
        await pilot.press("escape", "z", "z")
        assert app.anchor is None and app.b_cur == mode
        assert "76,000–77,000" in app.query_one("#ladder").render().plain


async def test_logged_out_cannot_buy(make):
    app = make(None)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        await pilot.press("l", "b")
        await until(pilot, lambda: "Read-only" in app.flash)
        assert not app.api.bought
        assert not isinstance(app.screen, A.Confirm)


async def test_buy_needs_confirmation_then_sends_the_selected_range(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        mode = app.b_cur
        await pilot.press("l", "v", "k", "b")
        await until(pilot, lambda: isinstance(app.screen, A.Confirm))
        await pilot.press("n")
        await until(pilot, lambda: not isinstance(app.screen, A.Confirm))
        assert app.api.bought == []
        await pilot.press("b")
        await until(pilot, lambda: isinstance(app.screen, A.Confirm))
        await pilot.press("y")
        await until(pilot, lambda: "Filled" in app.flash)
        assert app.api.bought == [(100, mode, mode + 1, 21.0)]


async def test_buy_aborts_when_the_server_price_has_moved(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        app.api.drift = 0.05
        await pilot.press("l", "b")
        await until(pilot, lambda: isinstance(app.screen, A.Confirm))
        await pilot.press("y")
        await until(pilot, lambda: "Price moved" in app.flash)
        assert app.api.bought == []


async def test_portfolio_and_sell(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        await pilot.press("p")
        await until(pilot, lambda: app.positions and app.summary)
        text = app.query_one("#portfolio").render().plain
        assert "76000–77000" in text and "+₿50" in text and "₿980" in text
        await pilot.press("x")
        await until(pilot, lambda: isinstance(app.screen, A.Confirm))
        await pilot.press("y")
        await until(pilot, lambda: app.api.sold == [(100, 77)])


async def test_key_is_never_rendered(make):
    key = "glp_live_" + "ab" * 32
    app = make(key)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        head = str(app.query_one("#head").render())
        assert "glp_live_…abab" in head and key not in head


async def test_opens_on_hourly_btc_then_remembers_the_last_series(make, monkeypatch):
    async def batches(self):
        return [Batch("b-h", "Hourly Bitcoin Prediction Markets", 3, 200), Batch("b-d", "Daily Bitcoin Markets", 3, 1000)]
    monkeypatch.setattr(FakeApi, "batches", batches)
    app = make(None)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        assert app.batch.short == "BTC" and app.hourly
        assert "BTC at " in app.slip_title and app.slip_title.endswith(" UTC")     # the question, not the series name
        assert "Hourly" not in str(app.query_one("#head").render())
        await pilot.press("right_square_bracket")
        assert app.batch.short == "BTC 1D" and not app.hourly
    again = make(None)
    async with again.run_test(size=(140, 34)) as pilot:
        await ready(again, pilot)
        assert again.batch.short == "BTC 1D"


async def test_ladder_slip_and_market_rows_do_not_wrap(make, monkeypatch):
    async def batches(self):
        return [Batch("b-h", "Hourly Bitcoin Prediction Markets", 3, 200)]
    monkeypatch.setattr(FakeApi, "batches", batches)
    app = make(None)
    async with app.run_test(size=(160, 40)) as pilot:
        await ready(app, pilot)
        pane = app.query_one("#markets")
        assert max(len(line) for line in pane.render().plain.split("\n")) <= pane.size.width    # hourly labels are the widest
        mode = app.b_cur
        await pilot.press("tab", "tab")                                      # markets → ladder → slip
        assert app.slip_focus
        await pilot.press("j", "l")                                          # High up one bin
        assert app.selection == (mode, mode + 1) and app.ticket.bins == 2
        await pilot.press("j", "l")                                          # Close: the next market
        assert app.m_cur == 1
        await pilot.press("escape")
        assert not app.slip_focus


def test_chances_of_several_closes():
    every, anyone, expect = P.chances([0.5, 0.5])
    assert (every, anyone, expect) == (0.25, 0.75, 1.0)


async def test_function_bar_and_market_probability(make):
    app = make(None)
    async with app.run_test(size=(160, 40)) as pilot:
        await ready(app, pilot)
        top = app.query_one("#head").render().plain.split("\n")
        assert top[0].startswith(" GLIMPSE  TERMINAL ")
        assert all(w in top[1] for w in ("FORECAST", "LADDER", "BOTS", "PORTFOLIO", "[", "]", "HELP", "QUIT"))
        mode = app.b_cur
        await pilot.press("l", "v", "k")
        slip = app.query_one("#slip").render().plain
        want = sum(app.book.probs[mode:mode + 2]) / sum(app.book.probs)
        assert "MARKET PROBABILITY" in slip and A.fmt.pct(want) in slip
        await pilot.press("escape", "f")
        await ready(app, pilot)
        await pilot.press("v", "l", "l")                                      # one cell across three closes
        slip = app.query_one("#slip").render().plain
        every, anyone, _ = P.chances(app.sel_probs)
        assert len(app.sel_probs) == 3 and "ALL 3 LAND" in slip and A.fmt.pct(every) in slip and A.fmt.pct(anyone) in slip
        assert "SELECTION" in app.query_one("#heatmap").render().plain
