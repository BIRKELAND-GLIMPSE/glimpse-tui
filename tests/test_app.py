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


async def test_the_terminal_opens_on_the_odds_for_the_next_close(make):
    """The first screen anyone sees: the odds themselves, on the nearest close, ready to select a range."""
    app = make(None)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        assert app.view == "main" and app.pane == 1 and app.m_cur == 0
        assert app.book.probs[app.b_cur] == max(app.book.probs)  # and on the most likely range within it
        assert "closes in" in app.query_one("#ladder").border_title
        ladder = app.query_one("#ladder").render().plain
        assert "prob" in ladder and "odds" in ladder


async def test_vim_navigation_and_range_selection(make):
    app = make(None)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        mode = app.b_cur
        assert app.book.probs[mode] == max(app.book.probs)      # opens on the most likely range
        await pilot.press("h")                                 # h leaves the odds for the list of closes beside them
        assert app.pane == 0
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


async def test_o_opens_the_odds_and_still_swaps_the_ends_of_a_range(make):
    app = make(None)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        await pilot.press("f")
        await until(pilot, lambda: app.view == "heatmap")
        await pilot.press("o")                                  # no box on the chart: o is the odds screen
        await until(pilot, lambda: app.view == "main")
        assert app.pane == 1                                    # and it lands on the odds themselves
        mode = app.b_cur
        await pilot.press("v", "k", "k")
        assert app.selection == (mode, mode + 2)
        await pilot.press("o")                                  # on the odds screen o is still the other end
        assert app.anchor == mode + 2 and app.b_cur == mode
        await pilot.press("escape", "f")
        await until(pilot, lambda: app.view == "heatmap")
        await pilot.press("v", "l", "o")                         # a box on the chart: o swaps its corners
        assert app.view == "heatmap" and app.hm_anchor is not None


async def test_logged_out_cannot_buy(make):
    app = make(None)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        await pilot.press("l", "b")
        await until(pilot, lambda: "Read-only" in app.flash)
        assert not app.api.bought
        assert not isinstance(app.screen, A.OrderPreview)


async def test_buy_needs_confirmation_then_sends_the_selected_range(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        mode = app.b_cur
        await pilot.press("l", "v", "k", "b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        await pilot.press("n")
        await until(pilot, lambda: not isinstance(app.screen, A.OrderPreview))
        assert app.api.bought == []
        await pilot.press("b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        await pilot.press("y")
        await until(pilot, lambda: "Filled" in app.flash)
        assert app.api.bought == [(100, mode, mode + 1, 21.0)]


async def test_buy_aborts_when_the_server_price_has_moved(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(140, 34)) as pilot:
        await ready(app, pilot)
        app.api.drift = 0.05
        await pilot.press("l", "b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
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
        await pilot.press("tab")                                             # the terminal opens on the ladder: tab is the slip
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
        assert all(w in top[1] for w in ("FORECAST", "ODDS", "BOTS", "PORTFOLIO", "[", "]", "HELP", "QUIT"))
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


async def test_the_help_page_scrolls_and_only_a_closing_key_closes_it(make):
    """James could not read past the first screen of `?`. j k, the arrows, ctrl-d ctrl-u and g G move it now,
    and only esc, q or ? put it away: a scroll key must never dismiss the page it is scrolling."""
    app = make(None)
    async with app.run_test(size=(140, 30)) as pilot:
        await ready(app, pilot)
        await pilot.press("?")
        assert isinstance(app.screen, A.Help)
        body = app.screen.query_one("#help-body")
        assert body.max_scroll_y > 0                             # there is more of it than fits: worth scrolling
        assert "j k" in app.screen.query_one("#dialog-hint").render().plain
        await pilot.press("j", "j", "j")
        assert body.scroll_offset.y > 0 and isinstance(app.screen, A.Help)
        after_j = body.scroll_offset.y
        await pilot.press("k")
        assert body.scroll_offset.y < after_j
        await pilot.press("G")
        await pilot.pause(0.05)
        assert body.scroll_offset.y == body.max_scroll_y and isinstance(app.screen, A.Help)
        await pilot.press("g")
        await pilot.pause(0.05)
        assert body.scroll_offset.y == 0
        await pilot.press("ctrl+d")
        assert body.scroll_offset.y > 0
        for key in ("down", "up", "pagedown", "pageup", "ctrl+u", "ctrl+f", "ctrl+b"):
            await pilot.press(key)
            assert isinstance(app.screen, A.Help), key           # none of these is a way out
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, A.Help)


async def test_the_front_page_help_is_the_one_the_front_page_gets(make):
    app = make(None)
    async with app.run_test(size=(140, 30)) as pilot:
        await ready(app, pilot)
        await pilot.press("?")
        assert "THE ODDS (o) is where the terminal opens" in app.screen._body
        await pilot.press("escape", "t")
        await until(pilot, lambda: app.view == "term")
        await pilot.press("?")
        assert "IN BITCOIN" in app.screen._body and "the power law" in app.screen._body
        assert "the bots" not in app.screen._body.split("THE PAGE")[1].split("IN BITCOIN")[0].split("·")[0]


# ── every position, before the order is placed ──────────────

async def box(app, pilot, wide: int = 3):
    """A box on the forecast across `wide` closes, which is `wide` separate orders."""
    await pilot.press("f")
    await until(pilot, lambda: app.view == "heatmap")
    await pilot.press("v")
    for _ in range(wide - 1):
        await pilot.press("l")
    await pilot.press("k", "k")
    await pilot.pause(0.05)


async def test_a_box_across_closes_is_one_priced_position_per_close(make):
    app = make(None)
    async with app.run_test(size=(180, 44)) as pilot:
        await ready(app, pilot)
        await box(app, pilot)
        legs, tk = app.legs, app.ticket
        assert len(legs) == 3 and tk.markets == 3
        assert sum(leg.ticket.cost_sats for leg in legs) == pytest.approx(tk.cost_sats)
        assert sum(leg.ticket.payout_sats for leg in legs) == pytest.approx(tk.payout_sats)
        c0, c1, _, _ = app.hm_selection
        assert [leg.end_time for leg in legs] == [v.row.end_time_utc for v in app.views[c0:c1 + 1]]
        assert all(leg.ticket.contracts == app.contracts for leg in legs)


async def test_the_slip_lists_every_position_and_still_totals_them_correctly(make):
    """The list must not shadow the order's own ticket: an early draft reused the name and the TICKET block
    below it showed the last close's cost as the whole order's."""
    app = make(None)
    async with app.run_test(size=(180, 44)) as pilot:
        await ready(app, pilot)
        await box(app, pilot)
        slip = app.query_one("#slip").render().plain
        assert "3 POSITIONS" in slip and slip.count("payout") == 1
        legs, tk = app.legs, app.ticket
        for leg in legs:
            assert A.fmt.sats(leg.ticket.cost_sats) in slip
        assert A.fmt.sats(tk.cost_sats) in slip and "MAX PAYOUT" in slip
        assert A.fmt.sats(tk.payout_sats) in slip
        head = next(ln for ln in slip.splitlines() if "close" in ln and "cost" in ln)
        assert "profit" not in head                                         # close, cost, payout, roi and no more
        assert app.query_one("#slip").styles.width.value == A.SLIP_WIDTH    # legs never widen the slip


async def test_the_slip_stays_narrow_on_a_single_market(make):
    app = make(None)
    async with app.run_test(size=(180, 44)) as pilot:
        await ready(app, pilot)
        assert len(app.legs) == 1
        slip = app.query_one("#slip").render().plain
        assert "POSITIONS" not in slip and "PAYOUT" in slip
        assert app.query_one("#slip").styles.width.value == A.SLIP_WIDTH


async def test_p_prices_every_column_of_every_position_without_buying(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(180, 44)) as pilot:
        await ready(app, pilot)
        await box(app, pilot)
        await pilot.press("P")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        scr = app.screen
        assert not scr._confirm
        head = [c for c, _ in A.LEG_COLS]
        assert all(c in scr._head.plain + scr._rows[0].plain for c in head)
        body = "\n".join(r.plain for r in scr._rows)
        assert body.count("+") >= 3 and "TOTAL 3" in body
        for leg in app.legs:                                          # every close, priced, on its own row
            assert A.fmt.odds(leg.ticket.odds) in body and A.fmt.roi(leg.ticket.roi) in body
        await pilot.press("j", "k", "ctrl+d")                         # scrolling must not dismiss it
        assert isinstance(app.screen, A.OrderPreview)
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, A.OrderPreview))
        assert not app.api.bought


async def test_nothing_is_bought_without_the_whole_table_being_shown_first(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(180, 44)) as pilot:
        await ready(app, pilot)
        await box(app, pilot)
        legs = list(app.legs)
        await pilot.press("b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        scr = app.screen
        assert scr._confirm and "BUY · 3 ORDERS" in scr._title
        body = "\n".join(r.plain for r in scr._rows)
        assert len(scr._rows) == len(legs) + 4                        # header, rule, a row each, rule, total
        for leg in legs:
            assert A.fmt.sats(leg.ticket.cost_sats) in body
        assert "TOTAL 3" in body and "one a close, each at that close's own price" in scr._note.plain
        await pilot.press("n")
        await until(pilot, lambda: not isinstance(app.screen, A.OrderPreview))
        assert not app.api.bought                                     # n cancels, nothing sent


async def test_the_single_market_confirmation_shows_the_same_table(make):
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(180, 44)) as pilot:
        await ready(app, pilot)
        await pilot.press("v", "k", "b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        scr = app.screen
        assert scr._confirm and scr._title == "BUY" and len(scr._rows) == 3      # header, rule, the one position
        assert "fee" in scr._head.plain and "Lose" in scr._note.plain
        await pilot.press("y")
        await until(pilot, lambda: app.api.bought)


async def test_the_positions_list_scrolls_around_the_one_under_the_cursor(make):
    app = make(None)
    async with app.run_test(size=(180, 44)) as pilot:
        await ready(app, pilot)
        await box(app, pilot)
        assert app.leg_cur == 0
        await pilot.press("tab")                                      # onto the slip
        assert app.slip_focus
        await pilot.press("]", "]")
        assert app.leg_cur == 2
        await pilot.press("[")
        assert app.leg_cur == 1
        await pilot.press("]", "]", "]", "]")
        assert app.leg_cur == len(app.legs) - 1                       # and stops at the end
        assert "▸" in app.query_one("#slip").render().plain
