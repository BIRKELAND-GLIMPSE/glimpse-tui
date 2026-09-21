"""Heatmap: pure grid logic, then the real app driven against a fake API. Offline."""
import asyncio
import json
from pathlib import Path

import pytest

from glimpse_tui import app as A
from glimpse_tui import auth
from glimpse_tui import heatmap as H
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, Book, Candle, Fill, MarketRow, Position, Summary, Wallet, parse_bin

FIX = json.loads((Path(__file__).parent / "fixtures" / "estimates.json").read_text())
T0 = 2_000_000_000 - 2_000_000_000 % 86400          # a future UTC midnight: every fake market is live
KEY = "glp_live_" + "ab" * 32


def test_glyph_ramp_is_four_shade_blocks_on_one_absolute_log_scale():
    assert H.GLYPHS == "░▒▓█" and len(H.GLYPHS) == 4                         # four steps, one hue: no ramp to decode
    assert H.level(H.P_FLOOR) == -1                                          # seed, not belief: blank
    assert H.level(0.0061) == 0 and H.level(0.0999) == H.KNEE - 1 and H.level(0.10) == H.KNEE
    assert H.level(1.0) == len(H.GLYPHS) - 1 and H.GLYPHS[-1] == "█"
    levels = [H.level(p) for p in (0.007, 0.01, 0.02, 0.05, 0.09, 0.1, 0.3, 0.6, 1.0)]
    assert levels == sorted(levels)                                          # a solider block always means more probable
    for i in (1, H.KNEE, len(H.GLYPHS) - 1):
        assert H.level(H.threshold(i) * 1.001) == i and H.level(H.threshold(i) * 0.999) == i - 1
    for pal in (H.TRUECOLOR, H.ANSI256):
        assert [pal.heat(i) for i in range(len(H.GLYPHS))] == list(pal.tiers)   # one colour a block, in order
        lum = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in pal.tiers]
        assert lum == sorted(lum) and max(lum) < 200                         # it brightens, and never reaches white
        assert all(r > g > b for r, g, b in pal.tiers)                       # every step is the same orange
    assert H.BAND == "#{:02x}{:02x}{:02x}".format(*H.TRUECOLOR.tiers[0])     # what GP paints an 80% band in


def test_256_palette_survives_a_256_colour_terminal():
    """Apple Terminal and most SSH sessions snap colours to the 256 palette. Every colour in the safe
    palette must come back unchanged, or browns turn olive and dark red as they did before."""
    from rich.color import Color, ColorSystem

    pal = H.ANSI256
    colours = [*pal.tiers, pal.median, pal.rule, pal.select, pal.held, pal.up, pal.down, pal.cursor, pal.cursor_hist]
    for rgb in colours:
        assert tuple(Color.from_rgb(*rgb).downgrade(ColorSystem.EIGHT_BIT).get_truecolor()) == rgb, rgb
    assert tuple(Color.from_rgb(*H.TRUECOLOR.tiers[0]).downgrade(ColorSystem.EIGHT_BIT).get_truecolor()) != H.TRUECOLOR.tiers[0]


def test_colour_depth_detection():
    assert not H.wants_truecolor({})                                          # nothing advertised (typical over SSH): be safe
    assert H.wants_truecolor({"COLORTERM": "truecolor"})
    assert H.wants_truecolor({"TERM_PROGRAM": "iTerm.app"}) and H.wants_truecolor({"TERM": "xterm-kitty"})
    assert not H.wants_truecolor({"TERM_PROGRAM": "Apple_Terminal", "TERM_PROGRAM_VERSION": "455.1"})
    assert H.wants_truecolor({"TERM_PROGRAM": "Apple_Terminal", "TERM_PROGRAM_VERSION": "465"})
    assert not H.wants_truecolor({"COLORTERM": "truecolor", "GLIMPSE_COLORS": "256"})      # an explicit choice wins
    assert H.wants_truecolor({}, saved="truecolor") and not H.wants_truecolor({"COLORTERM": "truecolor"}, saved="256")


def test_nice_step_lands_on_round_prices_that_are_whole_cells():
    assert H.nice_step(200, 6) == 2000 and H.nice_step(1000, 6) == 10_000 and H.nice_step(2, 6) == 20
    assert H.nice_step(30, 6) == 300 and H.nice_step(400, 6) == 4000 and H.nice_step(15, 6) == 150


def test_the_price_axis_rules_and_labels_whatever_the_zoom_and_the_ladder_offset():
    """A live ladder rarely starts on a round price. Rows used to be ruled only when their own price was exactly a
    multiple of the step, so those ladders drew no rules and no labels at all: the y-axis simply vanished."""
    for y_lo in (60_000.0, 60_153.0, 73_212.5):
        for bpc in (1, 2, 4, 8, 20):                                # 20 bins a cell: the ladder is shorter than the window
            grid = H.Grid(y_lo, 100, 500, bpc)
            rules = H.ruled_rows(grid, 60, 45)
            rows = sorted(rules)
            assert len(rows) >= (2 if grid.cells >= 45 else 1), (y_lo, bpc)      # an axis, always
            gaps = {b - a for a, b in zip(rows, rows[1:])}
            assert len(gaps) <= 1 and all(g >= 4 for g in gaps)     # evenly spaced graph paper, never crowded
            step = H.nice_step(grid.bin_size * bpc, 4)
            for g, price in rules.items():
                lo, hi = grid.prices_of(g)
                assert lo <= price < hi                             # the label is a price inside the row it labels
                assert abs(price / step - round(price / step)) < 1e-9       # and a round one


def test_grid():
    g = H.Grid(25_000, 200, 500, 5)
    assert g.cells == 100 and g.bins_of(3) == (15, 19) and g.prices_of(3) == (28_000, 29_000)
    assert g.cell_of_price(28_999) == 3 and g.cell_of_price(1) == 0 and g.cell_of_price(9e9) == 99
    assert H.Grid(0, 1000, 7, 5).bins_of(1) == (5, 6)           # ragged last cell


def test_candle_parts():
    k = Candle(0, o=100, h=130, l=80, c=110)
    assert H.candle_part(k, 100, 105) == "body" and H.candle_part(k, 120, 125) == "wick"
    assert H.candle_part(k, 80, 85) == "wick" and H.candle_part(k, 131, 140) == "" and H.candle_part(k, 70, 80) == ""
    assert H.candle_part(Candle(0, 100, 100, 100, 100), 100, 101) == "body"     # a doji still draws


def test_fit_zoom_and_axis():
    assert H.fit_zoom(20, 60) == 1 and H.fit_zoom(60, 60) == 2 and H.fit_zoom(10_000, 60) == 50
    cols = [(x * 2, T0 + x * 3600, 3600) for x in range(40)]
    line = H.axis_labels(cols, 90, hourly=True, now_x=10).plain
    assert line[10:13] == "NOW" and "12:00" in line and line.count("NOW") == 1
    assert len(line) == 90
    wide = [(x * 2, T0 + 3600 + x * 6 * 3600, 6 * 3600) for x in range(40)]      # six hours a column: 01:00 is never a start
    days = H.axis_labels(wide, 90, hourly=True, now_x=None).plain.split()
    assert sum(w.isdigit() for w in days) >= 8                                  # every midnight inside a column is dated
    assert H.crossing(T0 + 3600, 6 * 3600, 86400) is None and H.crossing(T0 - 3600, 6 * 3600, 86400) == T0
    assert H.scales(3600)[0] == (1, 12) and H.scales(3600)[-1] == (6, 1) and H.scales(86400)[:2] == [(1, 7), (1, 2)]


def test_combined_ticket():
    q = FIX["shares"]
    a = P.ticket(q, P.alpha_for(500), 74, 78, 10)
    both = P.combine([a, a])
    assert both.markets == 2 and both.cost_sats == pytest.approx(2 * a.cost_sats) and both.payout_sats == 2 * a.payout_sats
    assert both.prob == pytest.approx(a.prob ** 2) and both.odds == pytest.approx(a.odds)
    n = P.bisect_budget(lambda c: P.combine([P.ticket(q, P.alpha_for(500), 74, 78, c)] * 2).cost_sats, 1000)
    assert P.combine([P.ticket(q, P.alpha_for(500), 74, 78, n)] * 2).cost_sats <= 1000


class FakeApi:
    def __init__(self, key=None, base_url=None):
        self.authenticated, self.orders, self.drift, self.reject = bool(key), [], 0.0, set()

    async def close(self): ...

    async def batches(self):
        return [Batch("b-btc", "Daily Bitcoin Markets", 6, 1000)]

    async def markets(self, batch_id, limit=96):
        return [MarketRow(100 + i, f"d{i}", T0 + (i + 1) * 86400, "live", 0, 0, tuple(FIX["shares"]),
                          tuple(FIX["names"]), tuple(range(1, 501))) for i in range(6)]

    async def book(self, topic_id):
        b = Book(topic_id, f"market {topic_id}", T0 + 86400, "live", 0, list(range(1, 501)),
                 [parse_bin(n) for n in FIX["names"]], list(FIX["shares"]), P.alpha_for(500))
        b.reprice()
        return b

    async def estimate_order(self, order):
        lo = order.option_ids[0] - 1
        t = P.ticket(FIX["shares"], P.alpha_for(500), lo, lo + len(order.option_ids) - 1, order.contracts)
        raw = (t.cost_sats - t.fee_sats) * (1 + self.drift)
        return raw, raw * 0.02

    async def buy_orders(self, orders):
        self.orders = orders
        self.requests = getattr(self, "requests", []) + [len(orders)]
        return [Fill(o.topic_id, "", 0, 0, error="market not tradable") if o.topic_id in self.reject
                else Fill(o.topic_id, "t", 100, 2) for o in orders]

    async def wallet(self):
        return Wallet(500_000, 0, 1_000_000)

    async def positions(self):
        return [Position(101, 80, "d1", "79000-80000", 5, 50, 60, T0 + 2 * 86400, "active")]

    async def summary(self):
        return Summary(50, 60, 10, 1, 0, 0)


@pytest.fixture
def make(monkeypatch):
    async def candles(asset, granularity):
        return [Candle(T0 - (40 - i) * 86400, 76_000 + 50 * i, 77_500 + 50 * i, 75_200 + 50 * i, 76_300 + 50 * i) for i in range(40)]

    def _make(key):
        monkeypatch.setattr(A, "Glimpse", FakeApi)
        monkeypatch.setattr(A.api_mod, "candles", candles)
        monkeypatch.setattr(auth, "load_key", lambda: (key, "env" if key else "none"))
        monkeypatch.setattr(A.Terminal, "load_spot", lambda self: setattr(self, "spot", 76_500.0))
        return A.Terminal()
    return _make


async def until(pilot, cond, tries=100):
    """Wait for a condition instead of sleeping a guessed interval: workers run at their own pace under load."""
    for _ in range(tries):
        if cond():
            return
        await pilot.pause(0.03)
    raise AssertionError("condition never became true")


async def opened(app, pilot):
    for _ in range(60):
        await pilot.pause(0.05)
        if app.views:
            break
    app.load_spot()
    await pilot.press("f")
    for _ in range(60):
        await pilot.pause(0.05)
        if app.candles and not app.hm_fit_pending:
            break
    app.hm_bpc = 1                                   # the fit picks a zoom for the window; the tests count in single bins
    app.hm_gtop = app.hm_bin + app.hm_cells_v // 2   # and keep the cursor mid-screen at that zoom
    app.paint()
    await pilot.pause(0.05)


async def test_opens_on_spot_with_history_left_of_now(make):
    app = make(None)
    async with app.run_test(size=(140, 40)) as pilot:
        await opened(app, pilot)
        assert app.view == "heatmap" and app.hm_col == 0
        assert app.views[0].bins[app.hm_bin] == (76_000, 77_000)          # the cursor starts on the bin holding spot
        assert len(app.hm_hist) == 40 and app.hm_left < 0                 # closed daily candles, some on screen
        text = app.query_one("#heatmap").render().plain
        assert "│" in text and "NOW" in text and "◂" in text and "76,000–77,000" in text and "█" in text
        assert "┤" in text and "80,000" in text and "─" in text             # ruled grid, labelled on round prices
        assert any(ch in text for ch in "▓█") and any(ch in text for ch in "░▒")   # solid at the mode, thin in the tails
        pane = app.query_one("#heatmap")

        def drawn(rgb):
            want = H._style.__wrapped__(rgb).color
            return any(getattr(sp.style, "color", None) == want or getattr(sp.style, "bgcolor", None) == want
                       for sp in pane.render().spans)

        assert drawn(pane.palette.tiers[1]) and drawn(pane.palette.median) and drawn(pane.palette.up)
        legend = pane.legend().plain
        assert "p/bin" in legend and "10.0%" in legend and "median" in legend and "░▒▓" in legend
        await pilot.press("z", "m")
        assert not drawn(pane.palette.median) and drawn(pane.palette.tiers[1])
        assert app.ticket.markets == 1 and app.ticket.bins == 1           # a single cell is already a priced ticket


async def test_hjkl_box_selection_prices_every_close(make):
    app = make(None)
    async with app.run_test(size=(140, 40)) as pilot:
        await opened(app, pilot)
        b = app.hm_bin
        await pilot.press("l", "v", "l", "l", "k", "k")
        assert app.hm_selection == (1, 3, b, b + 2)
        tk = app.ticket
        assert (tk.markets, tk.bins) == (3, 3)
        one = P.ticket(list(FIX["shares"]), P.alpha_for(500), b, b + 2, 21)
        assert tk.cost_sats == pytest.approx(3 * one.cost_sats) and tk.payout_sats == 3 * one.payout_sats
        assert "3 closes" in app.query_one("#ticket").border_title
        await pilot.press("escape")
        assert app.hm_anchor is None and app.view == "heatmap"            # first esc drops the box, not the view
        await pilot.press("z", "o")
        assert app.hm_bpc == 2 and app.ticket.bins == 2                   # zoomed out, a cell is two bins
        await pilot.press("z", "i", "dollar_sign")
        assert app.hm_col == 5
        await pilot.press("0", "G")
        assert app.views[0].bins[app.hm_bin][0] == app.views[0].band[0]   # G: bottom of this close's 80% band


async def test_history_is_navigable_but_not_tradable(make):
    app = make(KEY)
    async with app.run_test(size=(140, 40)) as pilot:
        await opened(app, pilot)
        await pilot.press("h", "h")
        assert app.hm_col == -2 and app.hm_selection is None and app.ticket is None
        head = app.query_one("#heatmap").render().plain.split("\n")[0]
        assert "history" in head and "O " in head and "C " in head
        await pilot.press("v", "b")
        await pilot.pause(0.1)
        assert app.hm_anchor is None and not isinstance(app.screen, A.OrderPreview) and app.api.orders == []
        await pilot.press(*["h"] * 60)
        assert app.hm_col == -40                                          # stops at the oldest candle


async def test_box_buy_sends_one_order_per_close_after_confirmation(make):
    app = make(KEY)
    async with app.run_test(size=(140, 40)) as pilot:
        await opened(app, pilot)
        b = app.hm_bin
        await pilot.press("v", "l", "k", "b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        assert app.api.orders == []
        await pilot.press("y")
        await until(pilot, lambda: "Filled" in app.flash)
        assert [(o.topic_id, o.option_ids, o.contracts) for o in app.api.orders] == [
            (100, (b + 1, b + 2), 21.0), (101, (b + 1, b + 2), 21.0)]
        assert "Filled 2 closes" in app.flash and app.hm_anchor is None


async def test_box_buy_refuses_when_price_moved_and_reports_partial_fills(make):
    app = make(KEY)
    async with app.run_test(size=(140, 40)) as pilot:
        await opened(app, pilot)
        app.api.drift = 0.05
        await pilot.press("v", "l", "b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        await pilot.press("y")
        await until(pilot, lambda: "Price moved" in app.flash)
        assert app.api.orders == []
        app.api.drift, app.api.reject = 0.0, {101}
        await pilot.press("b")
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        await pilot.press("y")
        await until(pilot, lambda: "Filled" in app.flash)
        assert "Filled 1 of 2" in app.flash and "market not tradable" in app.flash


async def test_a_wide_box_is_one_ticket_sent_as_several_requests(make, monkeypatch):
    monkeypatch.setattr(A, "MARKETS_PER_REQUEST", 2)
    app = make(KEY)
    async with app.run_test(size=(140, 40)) as pilot:
        await opened(app, pilot)
        await pilot.press("v", "l", "l", "l", "l", "b")                 # five closes
        assert app.ticket is not None and app.ticket.markets == 5 and app.ticket_hint == ""
        await until(pilot, lambda: isinstance(app.screen, A.OrderPreview))
        await pilot.press("y")
        await until(pilot, lambda: "Filled" in app.flash)
        assert app.api.requests == [2, 2, 1] and "Filled 5 closes" in app.flash


async def test_enter_opens_the_close_in_the_ladder_and_held_cells_show(make):
    app = make(KEY)
    async with app.run_test(size=(140, 40)) as pilot:
        await opened(app, pilot)
        for _ in range(40):
            await pilot.pause(0.05)
            if app.held:
                break
        pane = app.query_one("#heatmap")
        held = H._style.__wrapped__(pane.palette.held).color
        assert any(getattr(sp.style, "color", None) == held or getattr(sp.style, "bgcolor", None) == held
                   for sp in pane.render().spans)
        await pilot.press("l", "l", "enter")
        await pilot.pause(0.2)
        assert (app.view, app.pane, app.m_cur) == ("main", 1, 2)


async def test_bet_slip_sits_on_the_right_and_edits_the_box(make):
    app = make(None)
    async with app.run_test(size=(160, 56)) as pilot:
        await opened(app, pilot)
        slip = app.query_one("#slip")
        assert slip.display and not app.query_one("#ticket").display
        assert slip.region.x > app.query_one("#heatmap").region.x + 80       # right of the chart, like the web
        b = app.hm_bin
        text = slip.render().plain
        assert "YOUR PREDICTION" in text and "Low" in text and "76,000" in text and "77,000" in text and "ODDS" in text

        await pilot.press("tab")
        assert app.slip_focus and "active" in slip.classes
        await pilot.press("j", "l", "l")                                     # High, up two bins
        assert app.hm_selection == (0, 0, b, b + 2)
        await pilot.press("k", "h")                                          # Low, down one
        assert app.hm_selection == (0, 0, b - 1, b + 2)
        await pilot.press("l", "l", "l", "l", "l")                           # Low cannot cross High
        assert app.hm_selection == (0, 0, b + 2, b + 2)
        await pilot.press("j", "j", "j", "l", "l")                           # To: two closes later
        assert app.hm_selection == (0, 2, b + 2, b + 2) and app.ticket.markets == 3
        await pilot.press("k", "l", "l", "l")                                # From cannot pass To
        assert app.hm_selection == (2, 2, b + 2, b + 2)
        await pilot.press("j", "j", "plus", "plus", "minus")                 # Size
        assert app.contracts == 22
        assert [f[2] for f in app.slip_fields()][:2] == ["78,000", "79,000"]
        await pilot.press("tab")
        assert not app.slip_focus
        await pilot.press("k")                                               # back on the chart, k moves the cursor again
        assert app.hm_bin == b + 3


async def test_slip_price_can_be_typed(make):
    app = make(None)
    async with app.run_test(size=(160, 46)) as pilot:
        await opened(app, pilot)
        await pilot.press("tab", "j", "enter")
        await until(pilot, lambda: isinstance(app.screen, A.Prompt))
        await pilot.press(*"81,500", "enter")
        await until(pilot, lambda: not isinstance(app.screen, A.Prompt))
        await until(pilot, lambda: app.slip_fields()[1][2] == "82,000")      # snaps to the bin holding 81,500
        assert app.slip_fields()[0][2] == "76,000"


async def test_narrow_terminal_keeps_the_compact_ticket(make):
    app = make(None)
    async with app.run_test(size=(100, 30)) as pilot:
        await opened(app, pilot)
        assert not app.query_one("#slip").display and app.query_one("#ticket").display
        await pilot.press("tab")
        assert not app.slip_focus


async def test_a_stale_quote_from_the_previous_series_cannot_wreck_the_fit(make):
    app = make(None)
    async with app.run_test(size=(160, 46)) as pilot:
        await opened(app, pilot)
        bpc = app.hm_bpc
        app.spot, app.hm_fit_pending = 3.5, True                             # e.g. another asset's price, arriving late
        app.paint()
        await pilot.pause(0.1)
        assert app.hm_bpc == bpc and app.views[0].bins[app.hm_bin][0] > 50_000


async def test_c_switches_palette_and_is_remembered(make):
    app = make(None)
    async with app.run_test(size=(160, 46)) as pilot:
        await opened(app, pilot)
        pane = app.query_one("#heatmap")
        assert pane.palette is H.ANSI256 and not app.truecolor               # nothing advertised in the test environment
        await pilot.press("c")
        assert pane.palette is H.TRUECOLOR and app.truecolor and auth.load_state()["colors"] == "truecolor"
        assert any(ch in pane.render().plain for ch in H.GLYPHS)
        await pilot.press("c")
        assert pane.palette is H.ANSI256 and auth.load_state()["colors"] == "256"
    assert make(None).truecolor is False                                     # the choice survives a restart


async def test_the_heatmap_is_plain_ascii_in_a_few_colours_and_is_not_redrawn_when_idle(make):
    app = make(None)
    async with app.run_test(size=(200, 60)) as pilot:
        await opened(app, pilot)
        pane = app.query_one("#heatmap")
        text = pane.render()
        rows = text.plain.split("\n")[1:-1]
        chart = "".join(line[:pane.size.width - H.GUTTER] for line in rows)
        assert set(chart) <= set(H.GLYPHS + " │─┼█┃")                          # glyphs, grid, candles, cursor: nothing else
        assert len(text.spans) < 14 * pane.size.height                       # a handful of colour runs a row, not one per cell
        colours = {sp.style.color.triplet for sp in text.spans if getattr(sp.style, "color", None) and sp.style.color.triplet}
        assert len(colours) <= 12                                            # heat is 4 of them

        before = pane.draws
        for _ in range(5):
            app.tick()
            pane.render()
        assert pane.draws == before                                          # the clock ticking redraws nothing
        await pilot.press("l")
        pane.render()
        assert pane.draws == before + 1                                      # a cursor move redraws once


async def test_a_refresh_with_no_changes_costs_nothing_and_never_overlaps(make):
    app = make(None)
    async with app.run_test(size=(160, 46)) as pilot:
        await opened(app, pilot)
        calls, gate = [], asyncio.Event()
        real = app.api.markets

        async def slow(batch_id, limit=96):
            calls.append(limit)
            await gate.wait()
            return await real(batch_id, limit)

        app.api.markets = slow
        version, views = app.data_version, list(app.views)
        for _ in range(4):
            app.load_markets()                                               # timers and keys piling up behind a slow link
            await pilot.pause(0.02)
        assert calls == [A.CLOSES_MAX]                                       # one request in flight, not four restarts
        gate.set()
        await until(pilot, lambda: app._loading is None)
        assert app.data_version == version                                   # nothing traded: caches stay valid
        assert all(a is b for a, b in zip(app.views, views, strict=True))    # and no close was re-summarised
        assert app.views[0].bins is app.views[-1].bins                       # the 500 bins exist once, not once per close


def week_of_hours(monkeypatch, hist_hours=300):
    """An hourly series seven days deep, as the live API has it: 168 closes, each an hour apart."""
    hour0 = T0 + 3600

    async def batches(self):
        return [Batch("b-h", "Hourly Bitcoin Prediction Markets", 168, 200)]

    async def markets(self, batch_id, limit=96):
        return [MarketRow(100 + i, f"h{i}", hour0 + (i + 1) * 3600, "live", 0, 0, tuple(FIX["shares"]),
                          tuple(FIX["names"]), tuple(range(1, 501))) for i in range(min(168, limit))]

    async def candles(asset, granularity):
        n = 80 if granularity == 900 else hist_hours
        return [Candle(hour0 - (n - i) * granularity, 76_000, 76_400, 75_800, 76_200) for i in range(n)]

    monkeypatch.setattr(FakeApi, "batches", batches)
    monkeypatch.setattr(FakeApi, "markets", markets)
    return hour0, candles


async def test_every_close_of_the_week_is_loaded_and_zf_shows_them_all(make, monkeypatch):
    hour0, candles = week_of_hours(monkeypatch)
    app = make(None)
    monkeypatch.setattr(A.api_mod, "candles", candles)
    async with app.run_test(size=(140, 40)) as pilot:
        await opened(app, pilot)
        assert len(app.views) == 168                                          # seven days of hourly closes, not three
        assert app.views[-1].row.end_time_utc - app.views[0].row.end_time_utc == 167 * 3600
        await pilot.press("z", "f")
        pane = app.query_one("#heatmap")
        chart_w = pane.size.width - H.GUTTER
        assert app.hm_per > 1 and app.hm_pos(167) + app.hm_w <= chart_w      # the last close is on screen
        assert app.hm_left < 0                                                # with history to its left
        text = pane.render().plain
        assert "NOW" in text and text.count("CLOSES") == 1                    # the cursor cell is a column of closes
        assert "168 closes to" in pane.border_title and "/col" in pane.border_title


async def test_zooming_time_out_groups_closes_by_the_clock(make, monkeypatch):
    hour0, candles = week_of_hours(monkeypatch)
    app = make(None)
    monkeypatch.setattr(A.api_mod, "candles", candles)
    async with app.run_test(size=(200, 50)) as pilot:
        await opened(app, pilot)
        for _ in range(3):
            await pilot.press("less_than_sign")                               # 2 → 1 character, then 2 and 3 hours a column
        assert (app.hm_w, app.hm_per) == (1, 3)
        gid, first, last = app.hm_groups
        starts = {app.views[first[c]].row.end_time_utc - 3600 for c in range(168)}
        assert all((t - hour0) % 3 == 0 or t % (3 * 3600) == 0 for t in starts)
        assert all(t % (3 * 3600) == 0 for t in starts if t != app.views[0].row.end_time_utc - 3600)
        assert app.hm_pos(first[5]) == app.hm_pos(last[5])                    # closes in a column share its character
        assert app.hm_col == 0 and app.hm_selection[:2] == (0, last[0])       # the cursor cell prices the whole column
        await pilot.press("l")
        assert app.hm_col == last[0] + 1 and app.hm_selection[:2] == (last[0] + 1, last[last[0] + 1])
        await pilot.press("h", "h")
        assert app.hm_col == -1 and app.hm_hist[-1].t + 3 * 3600 <= app.hm_col_time(0)[0]
        assert all(k.t % (3 * 3600) == 0 for k in app.hm_hist)                # history at the same three hours a column
        assert len(app.hm_hist) > 80 // 12                                    # from the hourly candles, not the half hours
        for _ in range(9):
            await pilot.press("greater_than_sign")
        assert (app.hm_w, app.hm_per) == (6, 1)


async def test_vim_counts_motions_and_the_status_line(make):
    app = make(None)
    async with app.run_test(size=(200, 60)) as pilot:
        await opened(app, pilot)
        b = app.hm_bin
        foot = lambda: app.query_one("#foot").render().plain                 # noqa: E731
        assert "-- NORMAL --" in foot() and "zt zz zb" in foot() and "gv" in foot() and "ma 'a" in foot()   # every binding is listed
        await pilot.press("3")
        assert foot().split("\n")[0].rstrip().endswith("3")                  # the pending count shows, as vim's showcmd does
        await pilot.press("l")
        assert app.hm_col == 3
        await pilot.press("1", "0", "k", "2", "j")
        assert app.hm_bin == b + 8
        await pilot.press("0")
        assert app.hm_col == 0                                               # a bare 0 is a motion, not a count
        await pilot.press("5", "vertical_line")
        assert app.hm_col == 4                                               # 5| is the fifth close
        await pilot.press("dollar_sign", "circumflex_accent")
        assert app.hm_col == 0
        await pilot.press("right_parenthesis")
        assert app.hm_col > 0 and (app.views[app.hm_col].row.end_time_utc - 86400) % (7 * 86400) == 4 * 86400   # a Monday open
        await pilot.press("ctrl+d")
        assert app.hm_bin == b + 8 - app.hm_cells_v // 2
        await pilot.press("0")
        col, cell = app.hm_col, app.hm_bin
        await pilot.press("w", "d", "d", "2", "s")                          # w a s d are the arrow keys, counts and all
        assert (app.hm_col, app.hm_bin) == (col + 2, cell - app.hm_bpc)
        await pilot.press("a", "a", "w")
        assert (app.hm_col, app.hm_bin) == (col, cell)
        await pilot.press("m", "a", "l", "apostrophe", "a")                 # a still names a mark after m and '
        assert (app.hm_col, app.hm_bin) == (col, cell)


async def test_vim_selection_marks_scrolling_and_commands(make):
    app = make(None)
    async with app.run_test(size=(200, 60)) as pilot:
        await opened(app, pilot)
        b = app.hm_bin
        band = app.views[0].band
        await pilot.press("V")                                               # the close's whole 80% band
        sel = app.hm_selection
        assert (app.views[0].bins[sel[2]][0], app.views[0].bins[sel[3]][1]) == band
        assert "-- VISUAL --" in app.query_one("#foot").render().plain
        top = app.hm_bin
        await pilot.press("o")
        assert app.hm_bin == sel[2] and app.hm_anchor == (0, top)             # o: the other corner
        await pilot.press("escape")
        assert app.hm_anchor is None
        await pilot.press("g", "v")
        assert app.hm_selection == sel                                       # gv: the last selection, back
        await pilot.press("escape", "0")
        app.hm_bin = b

        await pilot.press("m", "a", "4", "l", "6", "k", "apostrophe", "a")
        assert (app.hm_col, app.hm_bin) == (0, b)                            # 'a returns to the mark
        await pilot.press("apostrophe", "apostrophe")
        assert (app.hm_col, app.hm_bin) == (4, b + 6)                        # '' jumps back

        await pilot.press("z", "t")
        assert app.hm_gtop - app.hm_bin <= 2                                 # cursor row at the top
        await pilot.press("z", "b")
        assert app.hm_gtop - app.hm_bin >= app.hm_cells_v - 3
        await pilot.press("z", "z")
        gtop, here = app.hm_gtop, app.hm_bin
        await pilot.press("3", "ctrl+e")
        assert app.hm_gtop == gtop - 3 and app.hm_bin == here                # the view scrolls, the cursor stays

        for typed, check in (("81500", lambda: app.views[0].bins[app.hm_bin] == (81_000, 82_000)),
                             ("size 50", lambda: app.contracts == 50)):
            await pilot.press("colon")
            await until(pilot, lambda: isinstance(app.screen, A.Prompt))
            await pilot.press(*typed.replace(" ", "\x00").replace("\x00", " "), "enter") if False else await pilot.press(
                *["space" if ch == " " else ch for ch in typed], "enter")
            await until(pilot, check)
        await pilot.press("slash")
        await until(pilot, lambda: isinstance(app.screen, A.Prompt))
        await pilot.press(*"70,200", "enter")
        await until(pilot, lambda: app.views[0].bins[app.hm_bin] == (70_000, 71_000))


async def test_slip_is_the_odds_then_the_prediction_then_five_plain_figures_at_the_bottom(make):
    app = make(None)
    async with app.run_test(size=(200, 60)) as pilot:
        await opened(app, pilot)
        text = app.query_one("#slip").render().plain
        lines = [line.strip() for line in text.split("\n")]
        order = [next(i for i, line in enumerate(lines) if line.startswith(label))
                 for label in ("MARKET PROBABILITY", "YOUR PREDICTION", "Low", "Size", "TICKET", "COST", "PAYOUT", "PROFIT",
                               "ODDS", "ROI")]
        assert order == sorted(order) and order[9] - order[5] == 4           # the odds, the prediction, then five figures
        assert lines[-1].startswith("b  Bet") or lines[-1].startswith("L")   # the ticket and its button sit on the bottom edge
        assert "" in lines[order[3]:order[4]]                                # with the gap above the ticket, not below it
        assert not set(text) & set("█▀▄")                                    # ordinary text, no block numerals
        tk = app.ticket
        assert lines[order[5]].endswith(f"₿{round(tk.cost_sats):,}") and lines[order[8]].endswith("×")
        for gone in ("risk", "implied", "breakeven", "impact", "ev at", "avg fill", "fee"):
            assert gone not in text.lower()
        await pilot.press("v", "l", "l")
        assert "MAX PAYOUT" in app.query_one("#slip").render().plain


def test_resample_pairs_quarter_hours_into_half_hours():
    from glimpse_tui.api import resample

    base = 1_800_000_000 - 1_800_000_000 % 3600
    quarter = [Candle(base + i * 900, 100 + i, 110 + i, 90 + i, 101 + i) for i in range(5)]
    half = resample(quarter, 1800)
    assert [k.t - base for k in half] == [0, 1800, 3600]
    assert (half[0].o, half[0].h, half[0].l, half[0].c) == (100, 111, 90, 102)      # open of the first, close of the last
    assert (half[2].o, half[2].c) == (104, 105)                                     # a lone trailing bar stands alone


async def test_hourly_history_is_half_hour_candles_on_the_same_time_scale(make, monkeypatch):
    hour0 = T0 + 3600

    async def batches(self):
        return [Batch("b-h", "Hourly Bitcoin Prediction Markets", 6, 200)]

    async def markets(self, batch_id, limit=96):
        return [MarketRow(100 + i, f"h{i}", hour0 + (i + 1) * 3600, "live", 0, 0, tuple(FIX["shares"]),
                          tuple(FIX["names"]), tuple(range(1, 501))) for i in range(6)]

    asked = []

    async def candles(asset, granularity):
        asked.append(granularity)
        return [Candle(hour0 - (80 - i) * 900, 76_000, 76_400, 75_800, 76_200) for i in range(80)]

    monkeypatch.setattr(FakeApi, "batches", batches)
    monkeypatch.setattr(FakeApi, "markets", markets)
    app = make(None)
    monkeypatch.setattr(A.api_mod, "candles", candles)
    async with app.run_test(size=(200, 60)) as pilot:
        await opened(app, pilot)
        assert asked[0] == 900 and app.candle_s == 1800                      # 15-minute bars in, half hours held
        assert (app.hm_w, app.hm_sub, app.hm_cw) == (2, 2, 1)                # two one-character candles under each two-character close
        hist = app.hm_hist
        assert len(hist) == 40 and all(k.t % 1800 == 0 for k in hist) and hist[-1].t + 1800 == hour0
        assert app.hm_pos(-2) == -2 and app.hm_pos(1) == 3                   # an hour is two characters on both sides of NOW
        await pilot.press("h")
        head = app.query_one("#heatmap").render().plain.split("\n")[0]
        assert "history" in head and hist[-1].t == hour0 - 1800
        await pilot.press("less_than_sign")
        assert (app.hm_w, app.hm_sub) == (1, 1) and len(app.hm_hist) == 20    # too narrow to split: hourly candles again
        assert all(k.t % 3600 == 0 for k in app.hm_hist)


async def test_short_terminals_keep_the_key_panel_to_four_lines(make):
    app = make(None)
    async with app.run_test(size=(120, 30)) as pilot:
        await opened(app, pilot)
        foot = app.query_one("#foot").render().plain.split("\n")
        assert len(foot) == 5 and foot[0].startswith(" -- NORMAL --") and "every key" in foot[0]
        assert [f.split()[0] for f in foot[1:] if not f.startswith("  ")] == ["MOVE", "SELECT", "TRADE"]   # most used first
        assert "…" not in "".join(foot)                                      # a long row wraps; no key is cut off
        assert app.query_one("#heatmap").size.height >= 18                   # the chart keeps the room
