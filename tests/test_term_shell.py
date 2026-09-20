"""The launchpad view driven through the real app: the GO bar, panes, launchpads, and the promise that every key and
screen that existed before still works. Offline: the fake Glimpse API and the recorded sources."""
import time

import pytest
import test_app as T

import glimpse_tui.app as A
from glimpse_tui import auth
from glimpse_tui.api import Batch, MarketRow
from glimpse_tui.term import panes, registry

until = T.until


class SeriesApi(T.FakeApi):
    """The fake API with the series the default launchpad reads: hourly Bitcoin and daily gold, closing from the
    next hour (or the next day) on, so the horizons the bots are asked about are real ones."""

    async def batches(self):
        return [Batch("b-btch", "Hourly Bitcoin Markets", 96, 3600), Batch("b-btc", "Daily Bitcoin Markets", 30, 86400),
                Batch("b-xau", "Daily Gold Markets", 30, 86400)]

    async def markets(self, batch_id, limit=96):
        step = 3600 if batch_id == "b-btch" else 86400
        base = {"b-btch": 1000, "b-btc": 2000, "b-xau": 3000}.get(batch_id, 4000)
        first = (int(time.time()) // step + 1) * step
        return [MarketRow(base + i, f"{batch_id} - close {i}", first + i * step, "live", 0, 0,
                          tuple(T.FIX["shares"]), tuple(T.FIX["names"])) for i in range(min(96, limit))]


@pytest.fixture
def make(monkeypatch):
    def _make(key=None, launchpad=None):
        monkeypatch.setattr(A, "Glimpse", SeriesApi)
        monkeypatch.setattr(auth, "load_key", lambda: (key, "env" if key else "none"))
        monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
        return A.Terminal(launchpad=launchpad)
    return _make


def screen(app) -> str:
    return "\n".join(p.render().plain for p in app.shell.ws.panes)


async def type_go(pilot, text: str, open_with: str = "`") -> None:
    await pilot.press(open_with)
    for ch in text:
        await pilot.press("space" if ch == " " else ch)
    await pilot.press("enter")
    await pilot.pause(0.05)


def edge(pane) -> str:
    """The colour of a window's top-left corner, as drawn."""
    text = pane.render()
    return str(next(sp.style for sp in text.spans if sp.start == 0))


DASHBOARD = ["GP BTC 24H", "DIST BTC", "CONS BTC", "BOT BTC", "GP XAU 1D", "DIST XAU", "HASH", "DIFF", "NEWS", "QM GLOBAL",
             "MACRO", "RATES", "ECO", "FX", "GLCO", "FEES"]


async def test_the_default_is_a_scrolling_page_bitcoin_first_then_the_bots_gold_and_the_world(make, no_network):
    app = make(launchpad="BTC")
    async with app.run_test(size=(190, 50)) as pilot:
        await pilot.pause()                                   # the workspace is mounted on the first frame
        ws = app.shell.ws
        await until(pilot, lambda: len(ws.panes) == len(DASHBOARD) and "BTC" in app.shell.hub.quotes)
        assert app.view == "term" and [lf.command for lf in panes.leaves(ws.root)] == DASHBOARD
        head = app.query_one("#head").render().plain.split("\n")
        assert head[0].startswith(" GLIMPSE  TERMINAL ") and "SPC" in head[1] and "o  ODDS" in head[1] and "LADDER" not in head[1]
        chart, dist, cons, bot = ws.panes[:4]
        regions = {p.command(): p.region for p in ws.panes}
        assert regions["GP BTC 24H"].x == 0 and regions["DIST BTC"].x > 0 and regions["GP BTC 24H"].y == regions["DIST BTC"].y
        assert regions["CONS BTC"].y > regions["GP BTC 24H"].y                          # the bots: the second row, half of it
        assert regions["CONS BTC"].width + regions["BOT BTC"].width == ws.size.width
        assert regions["NEWS"].y > regions["GP XAU 1D"].y > regions["CONS BTC"].y       # gold, then the chain, then the news
        assert ws.page_height() > ws.size.height and regions["FEES"].y >= ws.size.height    # the page runs on below
        assert "screen 1 of" in app.query_one("#foot").render().plain
        await until(pilot, lambda: dist.views and chart.lines and ws.panes[8].items, tries=400)
        assert "Bitcoin · odds on the next hour" in dist.render().plain and "most likely" in dist.render().plain
        text = chart.render().plain
        assert "the last 24 hours and the next 24 hours" in text and "NOW" in text and "median" in text
        line = chart._ahead((time.time() + 86400, 80685.0, 80000.0, 81600.0), chart.lines[0]).plain
        assert "In 24 hours  80,685 median" in line and "80,000 – 81,600 likely (80%)" in line
        assert not [u for u in no_network.missing if "PAXG" not in u], no_network.missing


async def test_j_k_walk_the_page_the_highlight_follows_and_the_page_scrolls(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()                                   # the workspace is mounted on the first frame
        ws = app.shell.ws
        await until(pilot, lambda: len(ws.panes) == len(DASHBOARD))
        first = ws.pane
        await pilot.pause(0.05)
        assert "ff7d08" in edge(first).lower()
        await pilot.press("l")
        await pilot.pause(0.05)
        assert ws.pane.command() == "DIST BTC" and "ff7d08" in edge(ws.pane).lower() and "ff7d08" not in edge(first).lower()
        for _ in range(4):
            await pilot.press("j")
        assert ws.page_y > 0                                   # the page moved to keep the window in view
        await pilot.pause(0.05)
        r, top = ws.pane.region, ws.region.y                  # regions are on the screen: the page starts under the header
        assert r.y >= top and r.y + r.height <= top + ws.size.height
        await pilot.press("G")
        assert ws.pane.command() == "FEES" and ws.page_y == ws.page_height() - ws.size.height
        app.paint()
        assert "screen " in app.query_one("#foot").render().plain and "j k scroll" in app.query_one("#foot").render().plain
        await pilot.press("g")
        assert ws.pane is first and ws.page_y == 0
        await pilot.press("down", "right")
        assert ws.pane.command() == "BOT BTC"
        await pilot.press("enter")                             # open the window full screen: its keys are the page's now
        await pilot.pause(0.05)
        assert ws.inside and ws.zoomed and ws.pane.region.height == ws.size.height
        assert "-- INSIDE --" in app.query_one("#foot").render().plain
        await pilot.press("escape")
        assert not ws.inside and not ws.zoomed and ws.pane.command() == "BOT BTC"
        await pilot.press("2", "f")                            # f on a window with a series: the full forecast
        await until(pilot, lambda: app.view == "heatmap")
        await pilot.press("space", "o")                        # SPC o: the odds
        assert app.view == "main"
        await pilot.press("space", "t", "?")
        assert isinstance(app.screen, A.Help) and "j and k" in app.screen._body


async def test_news_reads_in_the_terminal_next_story_and_back(make, no_network):
    app = make(launchpad="BTC")
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()                                   # the workspace is mounted on the first frame
        ws = app.shell.ws
        await until(pilot, lambda: len(ws.panes) == len(DASHBOARD))
        news = ws.panes[DASHBOARD.index("NEWS")]
        await pilot.press("9", "enter")
        await until(pilot, lambda: news.items)
        target = next(i for i, it in enumerate(news.items) if "c63d7lexyym1o" in it.link)
        for _ in range(target):
            await pilot.press("ctrl+j" if _ % 2 else "j")      # ctrl-j is down too
        assert news.cur == target
        await pilot.press("l")                                 # l or enter reads it
        await until(pilot, lambda: ws.pane.code == "READ" and ws.pane.article)
        story = ws.pane
        assert "Greenland" in story.render().plain and ws.zoomed and "n p next / previous story" in story.hint()
        await pilot.press("n")                                 # the next headline, without going back to the list
        await until(pilot, lambda: ws.pane.code == "READ" and ws.pane is not story)
        assert news.cur == target + 1 and ws.pane.url == news.items[target + 1].link
        await pilot.press("h")                                 # h, esc or backspace: back to the headlines
        assert ws.pane is news and ws.inside
        await pilot.press("escape")                            # and back to the whole page
        assert not ws.inside and not ws.zoomed and ws.pane is news


async def test_colon_lists_everything_ctrl_j_ctrl_k_choose_and_enter_opens(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(132, 40)) as pilot:
        await pilot.pause()                                   # the workspace is mounted on the first frame
        ws = app.shell.ws
        await until(pilot, lambda: len(ws.panes) == len(DASHBOARD))
        await pilot.press("2")                                 # the odds window, which the back step returns to
        await until(pilot, lambda: ws.pane.views, tries=400)
        await pilot.press(":")
        picks = app.shell.line.picks
        assert len(picks) > 40 and picks[0].kind == "buffer" and app.shell.line.pick == 0
        assert "-- SEARCH --" in app.query_one("#foot").render().plain and "ctrl-j ctrl-k" in app.query_one("#foot").render().plain
        await pilot.press("ctrl+j", "ctrl+j", "down", "ctrl+k")
        assert app.shell.line.pick == 2
        for ch in "gold":
            await pilot.press(ch)
        assert {p.label for p in app.shell.line.picks} >= {"FCST XAU", "DIST XAU"}
        await pilot.press("escape", "space", "space")           # SPC SPC: the same list
        assert app.shell.go_focus
        await pilot.press("S", "R", "C", "enter")
        await until(pilot, lambda: ws.pane.code == "SRC")
        assert len(ws.panes) == len(DASHBOARD)                 # it ran in the focused window, not a new one
        await until(pilot, lambda: "mempool" in ws.pane.render().plain)
        await pilot.press("backspace")                         # back: the odds, still loaded
        assert ws.pane.command() == "DIST BTC" and ws.pane.views
        await type_go(pilot, "HELP SRC", ":")
        await until(pilot, lambda: ws.pane.code == "HELP" and "usage" in screen(app))
        await pilot.press(":", "up")
        assert app.shell.line.text == "HELP SRC"               # ↑ at the top of the list walks the history
        await pilot.press("escape")
        await type_go(pilot, "zzzz nothing")
        assert "Not a command" in app.flash or ws.pane.code == "ASK"


async def test_spc_windows_and_buffers_split_close_and_save(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(132, 40)) as pilot:
        await pilot.pause()                                   # the workspace is mounted on the first frame
        ws = app.shell.ws
        n = len(DASHBOARD)
        await until(pilot, lambda: len(ws.panes) == n)
        await pilot.press("space")
        assert "windows…" in app.query_one("#suggest").render().plain and "-- SPC --" in app.query_one("#foot").render().plain
        await pilot.press("w", "-")                            # a new window below, and the list opens to fill it
        assert len(ws.panes) == n + 1 and app.shell.go_focus
        for ch in "rates":
            await pilot.press(ch)
        await pilot.press("enter")
        assert ws.pane.code == "RATES" and len(ws.panes) == n + 1
        await pilot.press("space", "b", "b")                   # SPC b b: the buffers
        assert app.shell.only_buffers and all(p.kind == "buffer" for p in app.shell.line.picks)
        await pilot.press("escape", "space", "w", "d")
        assert len(ws.panes) == n
        await pilot.press("space", "w", "m")
        await pilot.pause(0.1)
        assert ws.zoomed and ws.pane.region.height == ws.size.height
        await pilot.press("escape")
        assert not ws.zoomed
        await type_go(pilot, "LP SAVE desk")
        assert "Saved DESK" in app.flash
        await type_go(pilot, "LP CHAIN")
        await until(pilot, lambda: app.shell.lp_name == "CHAIN" and len(ws.panes) == 4)
        await type_go(pilot, "LP desk")
        await until(pilot, lambda: app.shell.lp_name == "DESK" and len(ws.panes) == n and ws.page_height() > ws.size.height)
        for _ in range(8):
            await pilot.press("ctrl+w", "s")
        assert len(ws.panes) == panes.MAX_PANES                             # and no more than that


async def test_todays_screens_are_functions_and_keep_their_keys(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(160, 40)) as pilot:
        await until(pilot, lambda: len(app.shell.ws.panes) == len(DASHBOARD) and app.book)
        await type_go(pilot, "MKT")                                         # the old name still opens the odds
        assert app.view == "main" and app.query_one("#main").display and not app.query_one("#term").display
        top = app.query_one("#head").render().plain.split("\n")
        assert all(w in top[1] for w in ("FRONT PAGE", "FORECAST", "ODDS", "BOTS", "PORTFOLIO"))
        mode = app.b_cur
        await pilot.press("l", "v", "k")                                    # the odds screen's own keys, unchanged
        assert app.anchor == mode and app.b_cur == mode + 1                 # up the ladder is a higher price
        await pilot.press("escape")
        await type_go(pilot, "HM", ":")                                     # the vim command line hands HM to the GO bar
        assert app.view == "heatmap"
        await pilot.press("v", "l")
        assert app.hm_anchor is not None
        await pilot.press("escape", "t")                                    # t: back to the launchpad
        assert app.view == "term" and app.query_one("#term").display
        await pilot.press("p")
        assert app.view == "portfolio"
        await type_go(pilot, "BOTS")
        assert app.view == "bots"
        await type_go(pilot, "LP BTC")
        assert app.view == "term"
        await pilot.press("b", "+", "v", "x")                               # trading keys do nothing from a launchpad
        assert app.view == "term" and not app.screen_stack[1:] and app.contracts == 21.0


async def test_f_is_the_forecast_and_o_is_the_odds_from_any_window_of_the_page(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(190, 50)) as pilot:
        await pilot.pause()                                   # the workspace is mounted on the first frame
        ws = app.shell.ws
        await until(pilot, lambda: len(ws.panes) == len(DASHBOARD))
        await until(pilot, lambda: ws.panes[1].views, tries=400)
        await pilot.press("2", "o")                            # the odds on the series that window shows
        await until(pilot, lambda: app.view == "main")
        assert app.batch.short == "BTC" and app.pane == 1
        await pilot.press("o")                                 # o on the odds screen is its own: the other end of a range
        assert app.view == "main"
        await pilot.press("t")
        await until(pilot, lambda: app.view == "term")
        await pilot.press("5", "f")                            # gold's window: the full forecast, on gold
        await until(pilot, lambda: app.view == "heatmap")
        assert app.batch.short == "XAU"
        await pilot.press("t")
        await until(pilot, lambda: app.view == "term")
        await pilot.press("9", "o")                            # a window with no series of its own: the odds, as they were
        await until(pilot, lambda: app.view == "main")
        assert app.batch.short == "XAU"
        top = app.query_one("#head").render().plain.split("\n")[1]
        assert " o  ODDS " in top and " f  FORECAST " in top and " t  FRONT PAGE " in top


async def test_a_window_frame_says_how_old_its_numbers_are_not_who_served_them(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(190, 50)) as pilot:
        await pilot.pause()                                   # the workspace is mounted on the first frame
        ws = app.shell.ws
        await until(pilot, lambda: len(ws.panes) == len(DASHBOARD))
        qm = next(p for p in ws.panes if p.code == "QM")
        await until(pilot, lambda: qm.provs, tries=400)
        heads = [p.render().plain.split("\n")[0].lower() for p in ws.panes]
        assert any("live" in h for h in heads) and any("daily" in h for h in heads)
        vendors = ("yahoo", "coinbase", "kraken", "bitstamp", "fred", "treasury.gov", "mempool.space", "glimpse ·")
        for head in heads:                                     # the frame says how old, never who served it
            assert not [v for v in vendors if v in head], head
        assert not [v for v in vendors if v in qm.render().plain.lower()]        # nor does the quote table


async def test_terminal_without_a_launchpad_opens_where_it_always_did(make, no_network):
    app = make()
    async with app.run_test(size=(132, 36)) as pilot:
        await T.ready(app, pilot)
        assert app.view == "main" and not app.shell.started
        assert not no_network.seen                                          # the hub is idle until a launchpad shows


async def test_every_function_has_a_help_page_and_a_category():
    registry.load_all()
    assert not registry.failed(), registry.failed()
    for f in registry.every():
        assert f.category in registry.CATEGORIES and f.summary and len(f.help) > 80, f.code
        assert "—" not in f.help + f.summary, f"{f.code}: house style has no em-dashes"
        assert f.make is not None or f.legacy or f.code in ("EXP", "DEMO"), f.code     # these two the shell runs itself


async def test_the_demo_walks_real_commands_and_any_key_stops_it(make):
    from glimpse_tui.funcs import demo
    app = make(launchpad="BTC")
    async with app.run_test(size=(132, 36)) as pilot:
        await until(pilot, lambda: len(app.shell.ws.panes) == len(DASHBOARD))
        steps = (("MEMP", True, 1, "the mempool"), ("TX", True, 1, "a transaction"), ("LP MACRO", False, 1, "macro"),
                 ("HM", False, 1, "the heatmap"))
        await demo.run(app.shell, steps, pace=0.2)
        assert app.view == "heatmap" and not app.shell.demo and "That was the tour" in app.flash
        app.shell.run("DEMO")                                               # the registered function starts the full tour
        await until(pilot, lambda: app.shell.demo and "DEMO" in app.flash)
        await pilot.press("x")
        await until(pilot, lambda: not app.shell.demo)
        assert app.view == "term"                                           # the key stopped the tour and did nothing else


async def test_set_repoints_a_backend_without_a_restart(make, no_network):
    app = make(launchpad="CHAIN")
    async with app.run_test(size=(132, 36)) as pilot:
        await until(pilot, lambda: app.shell.hub.chain.height > 0)
        await type_go(pilot, "SET bitcoin.mempool http://umbrel.local:3006")
        assert app.shell.hub.sources["mempool"].base_url == "http://umbrel.local:3006"
        assert "in effect now" in app.flash
        from glimpse_tui.term import config
        assert config.load()["bitcoin"]["mempool"] == "http://umbrel.local:3006"


def test_the_odds_window_holds_one_outcome_for_every_row_the_pane_has():
    """DIST used to show the 80% band and half its width either side: four rows on hourly Bitcoin, whatever the
    pane's height. It now fills the pane, as fine as the market's own bins, and prices every row."""
    from glimpse_tui.funcs.forecast import DistPane

    v = A.summarise(MarketRow(1, "close", int(time.time()) + 1800, "live", 0, 0,
                              tuple(T.FIX["shares"]), tuple(T.FIX["names"]), tuple(range(1, 501))))
    pane, bin_w = DistPane.__new__(DistPane), v.bins[0][1] - v.bins[0][0]
    short, tall = pane._rows(v, v.median, 6), pane._rows(v, v.median, 18)
    assert len(short) == 6 and len(tall) == 18                              # the pane's height sets how many show
    steps = {round((b - a) / bin_w, 6) for a, b, _, _ in short + tall}
    assert steps <= {1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0}         # a whole, round number of the market's bins
    assert (tall[0][1] - tall[0][0]) < (short[0][1] - short[0][0])          # the taller pane reads the finer step
    assert [r[0] for r in tall] == sorted(r[0] for r in tall)
    for rows in (short, tall):
        assert rows[0][0] <= v.band[0] and rows[-1][1] >= v.band[1]         # the 80% band always fits, with shoulders
        assert v.bins[0][0] <= rows[0][0] and rows[-1][1] <= v.bins[-1][1]  # never off the market's own ladder
        assert all(o > 1.0 for _, _, _, o in rows)                          # every row is priced, tails included
        assert any(a <= v.median < b for a, b, _, _ in rows)                # the middle is always on screen
        assert max(r[2] for r in rows) > 4 * min(r[2] for r in rows)        # and the shape of the thing shows
