"""The monitor: the fleet read off the runners and the ledger, positions marked to the books, fills read off the ledger's
changes, and the screen itself over a fake exchange of two series. No network, no key, no orders."""
import json
import time

import pytest
from test_bots import Feed, until
from test_desk import BINS, IDS, Silent, TwoSeries
from test_desk import make as make_desk

from glimpse_tui import app as A
from glimpse_tui import auth, chrome, noc, nocview
from glimpse_tui import pricing as P
from glimpse_tui.api import Book, Position, Summary, Wallet


class Api(TwoSeries):
    def __init__(self, key=None, base_url=None):
        super().__init__(key, closes=6)

    async def summary(self):
        return Summary(1_000.0, 1_100.0, 100.0, 2, 0, 0)

    async def positions(self):
        return [Position(100, 77, "Hourly Bitcoin", "76,500-76,600", 10.0, 300.0, 330.0, self.ends[100], "live")] if self.authenticated else []

    async def wallet(self):
        return Wallet(50_000.0, 300.0, 100_000.0)


@pytest.fixture
def screen(monkeypatch):
    def _make(key=None):
        monkeypatch.setattr(A, "Glimpse", Api)
        monkeypatch.setattr(auth, "load_key", lambda: (key, "env" if key else "none"))
        monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
        monkeypatch.setattr(A.Terminal, "feed", lambda self, asset=None: self.feeds.setdefault("BTC", Feed()))
        monkeypatch.setattr(noc.Monitor, "connect", Silent)
        app = A.Terminal()
        app.spot = 76_500.0
        return app
    return _make


def test_a_position_is_marked_at_what_selling_it_back_would_fetch_and_a_paper_one_against_the_book_it_would_have_made():
    api = TwoSeries()
    book = Book(100, "m", api.ends[100], "live", 0, list(IDS), BINS, list(api.books[100]), P.alpha_for(500))
    book.reprice()
    live = noc.mark(book, {77: 40.0}, paper=False)
    price, value = live[77]
    assert price == pytest.approx(book.prices[76])
    assert value == pytest.approx(P.sell_proceeds(book.shares, book.alpha, 76, 40.0), rel=1e-6)
    paper = noc.mark(book, {77: 40.0}, paper=True)                # bought on paper: the book it would have made holds 40 more
    assert paper[77][0] > price and paper[77][1] > value
    assert book.shares == list(api.books[100])                    # the book itself is never touched
    assert noc.mark(book, {9_999: 5.0}, paper=False) == {}        # a range the book does not list is left unpriced


def test_the_sparkline_fits_its_width_and_a_flat_series_is_a_flat_line():
    assert nocview.spark([1, 2, 3, 4], 10) == "▁▃▆█" and nocview.spark([5.0] * 3, 10) == "▁▁▁"
    assert len(nocview.spark(list(range(100)), 12)) == 12 and nocview.spark([], 5) == ""


async def test_the_monitor_shows_a_running_desk_its_positions_and_its_fills(tmp_path, screen):
    app = screen()
    async with app.run_test(size=(210, 60)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("M")
        assert app.view == "noc"
        await until(pilot, lambda: len(app.noc.series(app)) == 2)             # its own watch has listed both series
        assert [u.kind for u in app.noc.units] == [noc.ALL]                    # nothing runs yet: the fleet row alone
        assert "no bots yet" in nocview.fleet_title(app) and "No bot is running" in app.query_one("#nocfleet").render().plain
        wall = app.query_one("#nocwall").render().plain
        assert " BTC " in wall and " XAU " in wall and "NOW" in wall and "6 closes" in wall

        d = await make_desk(app.api, tmp_path, budget=20_000)
        d.ledger = app.ledger
        app.runners[d.bot.id] = d
        app.noc.refresh(app)                                                   # the baseline: what it holds before it trades
        await d.once()
        app.noc.refresh(app)
        await pilot.pause(0.2)
        units = app.noc.units
        assert [u.kind for u in units] == [noc.ALL, "desk"] and units[1].name == d.name and not units[1].live
        desk = units[1]
        assert desk.cost == pytest.approx(d.open_cost) and desk.trades == d.trades == 12 and desk.closes == 12
        assert 0 < desk.value < desk.cost                                      # sold back at once it would lose both fees and the slippage
        assert all(h.price is not None and h.p is not None and h.series in ("BTC", "XAU") for h in desk.holdings)
        assert units[0].cost == pytest.approx(desk.cost) and units[0].trades == 12
        fills = app.noc.tape(units[0])
        assert len(fills) == len(desk.holdings) and all(f.contracts > 0 and f.sats > 0 and f.unit == desk.id for f in fills)
        assert sum(f.sats for f in fills) == pytest.approx(desk.cost)
        orders = app.noc.trades(units[0])                                      # the tape: an order a line, its ranges added up
        assert len(orders) == 12 and sum(o.option for o in orders) == len(fills) and all(o.option == 7 for o in orders)
        assert sum(o.sats for o in orders) == pytest.approx(desk.cost) and orders[0].lo == 73_000 and orders[0].hi == 80_000
        assert len(app.noc.flashes(units[0], time.time())) == len(fills) and app.noc.flashes(units[0], time.time() + 600) == []

        fleet = app.query_one("#nocfleet").render().plain
        assert "PAPER" in fleet and "ALL BOTS" in fleet and d.name in fleet and "bankroll ₿20,000" not in fleet   # the fleet row is selected
        await pilot.press("j")
        assert app.noc.unit is desk or app.noc.unit.id == desk.id
        assert "a desk across BTC, XAU" in app.query_one("#nocfleet").render().plain
        wall = app.query_one("#nocwall").render()
        assert "holds ₿" in wall.plain and any(ch in wall.plain for ch in "░▒▓█")
        styles = {str(s.style) for s in wall.spans}
        assert any(nocview.ORANGE in x and "bold" in x for x in styles)        # what it just bought is lit
        assert any(x.endswith(nocview.shade(1.0, True)) for x in styles) and any(x.endswith(nocview.shade(1.0, False)) for x in styles)
        pos = app.query_one("#nocpos").render().plain
        assert "6 closes" in pos and "━" in pos and "CLOSE" in pos
        tape = app.query_one("#noctape").render().plain
        assert "BUY" in tape and d.name[:10] in tape and "73,000–80,000" in tape and "7 rng" in tape
        assert "│" in tape and "BTC at " in tape                               # and the desk's own log beside the orders
        assert "MONITOR" in chrome.nav_bar(app, 210).plain

        await pilot.press("l")                                                 # the next series: its positions follow
        assert nocview.selected_series(app).short == "XAU" and "XAU" in nocview.positions_title(app)
        await pilot.press("m")
        assert app.noc.zoom and "XAU alone" in nocview.wall_title(app)
        assert " BTC " not in app.query_one("#nocwall").render().plain
        await pilot.press("-")
        assert noc.HORIZONS[app.noc.span] == 168
        await pilot.press("escape")
        assert not app.noc.zoom and app.view == "noc"

        o = desk.holdings[0]
        app.ledger.reduce(d.key, d.mode, o.topic, o.option, o.contracts)       # the desk sells one range whole
        app.noc.refresh(app)
        last = app.noc.fills[-1]
        assert last.contracts == pytest.approx(-o.contracts) and last.sats == pytest.approx(-o.cost) and last.topic == o.topic
        await pilot.pause(0.1)
        assert "SELL" in app.query_one("#noctape").render().plain

        await pilot.press("tab", "j")                                          # the positions: a row a close
        assert app.noc.focus == 1 and app.noc.pos_cur == 1
        await pilot.press("escape")
        assert app.noc.focus == 0 and app.view == "noc"
        await pilot.press("escape")
        assert app.view == "main"
        await until(pilot, lambda: app.noc.watch is None)                      # leaving the screen stops its watch


async def test_enter_stops_a_bot_and_starts_it_again_and_a_live_one_asks_first(tmp_path, screen):
    app = screen()
    async with app.run_test(size=(180, 50)) as pilot:
        await until(pilot, lambda: app.views)
        d = await make_desk(app.api, tmp_path, budget=20_000)
        d.ledger = app.ledger
        app.runners[d.bot.id] = d
        d.start()
        await pilot.press("M")
        await until(pilot, lambda: len(app.noc.units) == 2 and app.noc.units[1].running)
        assert "1 of 1 bot running" in nocview.fleet_title(app)
        await pilot.press("j", "enter")
        await until(pilot, lambda: not d.running and not app.noc.units[1].running)     # the fleet reads it again within a second
        assert nocview.state_mark(app.noc.units[1], time.time())[0] == "○"
        await pilot.press("enter")
        await until(pilot, lambda: d.running)
        await pilot.press("X")
        await until(pilot, lambda: not d.running)
        d.live = True                                                          # real sats: a y before it trades again
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, A.Confirm))
        await pilot.press("n")
        await pilot.pause(0.1)
        assert not d.running


async def test_a_bot_nobody_runs_and_the_account_are_rows_of_their_own(tmp_path, screen):
    app = screen(key="k")
    end = (int(time.time()) // 3600 + 3) * 3600
    app.ledger.path.parent.mkdir(parents=True, exist_ok=True)
    app.ledger.path.write_text(json.dumps({
        "kalman_trend": {"live": {"100": {"end": end, "asset": "BTC", "legs": {"77": [20.0, 500.0, 76_500.0, 76_600.0]}}}},
        "old": {"paper": {"5": {"end": int(time.time()) - 60, "asset": "BTC", "legs": {"1": [1.0, 1.0, 1.0, 2.0]}}}},   # ended: not held
        "portfolio": {"live": {"100": {"end": end, "asset": "BTC", "legs": {"77": [9.0, 9.0, 76_500.0, 76_600.0]}}}}}))
    async with app.run_test(size=(180, 50)) as pilot:
        await until(pilot, lambda: app.views and app.positions)
        await pilot.press("space", "M")                                        # the leader reaches it too
        assert app.view == "noc"
        await until(pilot, lambda: len(app.noc.series(app)) == 2 and len(app.noc.units) == 3
                    and app.noc.units[1].holdings[0].price is not None)        # marked, once the books are in
        kinds = {u.kind: u for u in app.noc.units}
        assert set(kinds) == {noc.ALL, "idle", noc.ACCOUNT}
        idle = kinds["idle"]
        assert idle.live and not idle.running and idle.cost == 500.0 and idle.holdings[0].series == "BTC"
        assert 0 < idle.value != 500.0 and idle.name == "Kalman Trend"         # sold back into the book; named by the zoo
        assert kinds[noc.ACCOUNT].cost == 300.0 and kinds[noc.ACCOUNT].value == 330.0
        assert kinds[noc.ALL].cost == 500.0                                    # the account is not counted among the bots
        fleet = app.query_one("#nocfleet").render().plain
        assert "ACCOUNT" in fleet and "LIVE" in fleet and "◌" in fleet and "0/1 on" in fleet
        await pilot.press("G")
        assert app.noc.unit.kind == noc.ACCOUNT and "your account" in nocview.wall_title(app)
        assert "everything your key holds" in app.query_one("#nocfleet").render().plain


async def test_the_go_bar_reaches_the_monitor_and_its_keys_are_listed(screen):
    from glimpse_tui.term import registry

    assert registry.get("NOC") is not None
    assert "noc" in chrome.KEYMAP and any(key == "M" for _, _, key, _ in chrome.tabs(type("T", (), {"view": "main"})()))
    assert "MONITOR (M)" in A.HELP
