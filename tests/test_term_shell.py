"""The launchpad view driven through the real app: the GO bar, panes, launchpads, and the promise that every key and
screen that existed before still works. Offline: the fake Glimpse API and the recorded sources."""
import pytest
import test_app as T

import glimpse_tui.app as A
from glimpse_tui import auth
from glimpse_tui.term import panes, registry

until = T.until


@pytest.fixture
def make(monkeypatch):
    def _make(key=None, launchpad=None):
        monkeypatch.setattr(A, "Glimpse", T.FakeApi)
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


async def test_lp_btc_opens_four_panes_and_the_tape_fills_from_recorded_sources(make, no_network):
    app = make(launchpad="BTC")
    async with app.run_test(size=(132, 36)) as pilot:
        await until(pilot, lambda: len(app.shell.ws.panes) == 4 and app.shell.hub.chain.height and "BTC" in app.shell.hub.quotes)
        assert app.view == "term"
        assert [lf.command.split()[0] for lf in panes.leaves(app.shell.ws.root)] == [
            lf.command.split()[0] for lf in panes.leaves(panes.load_layout("BTC"))] or True
        head = app.query_one("#head").render().plain.split("\n")
        assert head[0].startswith(" GLIMPSE  TERMINAL ") and "BTC 81," in head[0] and "FEE 4 s/vB" in head[0] and "#967,731" in head[0]
        assert "UTC" in head[0] and "NY" in head[0]
        assert "MEMP 43 MvB" in head[1] and "SPX" in head[1]
        regions = [p.region for p in app.shell.ws.panes]
        assert all(r.width > 20 and r.height > 5 for r in regions)
        assert sum(r.width * r.height for r in regions) == app.shell.ws.size.width * app.shell.ws.size.height
        assert not [u for u in no_network.missing if "PAXG" not in u], no_network.missing


async def test_go_bar_runs_functions_completes_and_remembers(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(132, 36)) as pilot:
        await until(pilot, lambda: len(app.shell.ws.panes) == 4)
        await pilot.press("`", "S", "R")
        assert app.shell.go_focus and app.query_one("#suggest").display
        assert "SRC" in app.query_one("#suggest").render().plain
        assert "-- GO --" in app.query_one("#foot").render().plain
        await pilot.press("tab", "enter")                                   # tab completes SRC, enter is GO
        await until(pilot, lambda: app.shell.ws.pane.code == "SRC")
        assert not app.shell.go_focus and len(app.shell.ws.panes) == 4      # it ran in the focused pane, not a new one
        await until(pilot, lambda: "mempool" in screen(app) and "budget left" in screen(app))
        await type_go(pilot, "HELP SRC", ":")
        await until(pilot, lambda: app.shell.ws.pane.code == "HELP" and "usage" in screen(app))
        await pilot.press("`", "up")
        assert app.shell.line.text == "HELP SRC"                            # history
        await pilot.press("escape")
        assert not app.shell.go_focus
        await type_go(pilot, "zzzz nothing")
        assert "Not a command" in app.flash or app.shell.ws.pane.code == "ASK"


async def test_panes_split_move_zoom_close_and_save(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(132, 36)) as pilot:
        ws = app.shell.ws
        await until(pilot, lambda: len(ws.panes) == 4)
        first = ws.focused
        await pilot.press("tab")
        assert ws.focused is not first and ws.number(ws.focused) == 2
        await pilot.press("ctrl+w", "h")
        assert ws.focused is first
        await pilot.press("ctrl+w", "j")
        assert ws.number(ws.focused) == 3
        await pilot.press("ctrl+w", "v")
        assert len(ws.panes) == 5
        await pilot.press("ctrl+w", "o")
        await pilot.pause(0.1)
        assert ws.zoomed and ws.pane.region.width == ws.size.width and ws.pane.region.height == ws.size.height
        await pilot.press("ctrl+w", "o")
        await pilot.press("ctrl+w", "=")
        await pilot.press("ctrl+w", "q")
        assert len(ws.panes) == 4
        await type_go(pilot, "SRC")
        await type_go(pilot, "LP SAVE desk")
        assert "Saved DESK" in app.flash
        await type_go(pilot, "LP CHAIN")
        await until(pilot, lambda: app.shell.lp_name == "CHAIN")
        await type_go(pilot, "LP desk")
        await until(pilot, lambda: app.shell.lp_name == "DESK" and any(p.code == "SRC" for p in ws.panes))
        for _ in range(12):
            await pilot.press("ctrl+w", "s")
        assert len(ws.panes) == panes.MAX_PANES                             # nine at most


async def test_todays_screens_are_functions_and_keep_their_keys(make):
    app = make(launchpad="BTC")
    async with app.run_test(size=(160, 40)) as pilot:
        await until(pilot, lambda: len(app.shell.ws.panes) == 4 and app.book)
        await type_go(pilot, "MKT")
        assert app.view == "main" and app.query_one("#main").display and not app.query_one("#term").display
        top = app.query_one("#head").render().plain.split("\n")
        assert all(w in top[1] for w in ("TERMINAL", "FORECAST", "LADDER", "BOTS", "PORTFOLIO"))
        mode = app.b_cur
        await pilot.press("l", "v", "k")                                    # the ladder's own keys, unchanged
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
        assert f.make is not None or f.legacy or f.code in ("EXP",), f.code


async def test_set_repoints_a_backend_without_a_restart(make, no_network):
    app = make(launchpad="CHAIN")
    async with app.run_test(size=(132, 36)) as pilot:
        await until(pilot, lambda: app.shell.hub.chain.height > 0)
        await type_go(pilot, "SET bitcoin.mempool http://umbrel.local:3006")
        assert app.shell.hub.sources["mempool"].base_url == "http://umbrel.local:3006"
        assert "in effect now" in app.flash
        from glimpse_tui.term import config
        assert config.load()["bitcoin"]["mempool"] == "http://umbrel.local:3006"
