"""The bots on the front page: one pass of the zoo over a series, the consensus it makes, and the two windows
that read it. The models are the real ones and they run here; the candles are synthetic and nothing is fetched."""
import time

import pytest
import test_app as T
from test_term_shell import SeriesApi

import glimpse_tui.app as A
from glimpse_tui import auth
from glimpse_tui.term import swarm

until = T.until


@pytest.fixture
def make(monkeypatch):
    def _make(launchpad="BTC"):
        monkeypatch.setattr(A, "Glimpse", SeriesApi)
        monkeypatch.setattr(auth, "load_key", lambda: (None, "none"))
        monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
        return A.Terminal(launchpad=launchpad)
    return _make


def pane(app, code):
    return next(p for p in app.shell.ws.panes if p.code == code)


async def ready(pilot, app, tries=600):
    cons = pane(app, "CONS")
    await until(pilot, lambda: len(cons.swarm.pictures) == 3 and not cons.swarm.busy, tries=tries)
    return cons


# ── the pass ────────────────────────────────────────────────

def test_the_consensus_is_the_average_picture_and_the_counts_are_of_every_bot():
    from glimpse_tui.botsview import Scan
    bins = [(float(i), float(i + 1)) for i in range(10)]
    pic = swarm.Picture("next hour", time.time() + 3600, 1, tuple(bins), market=tuple([0.1] * 10))
    pic.scans = {"a": Scan("bullish", 0.4, 1.0, 0.0, 6.5, 5.0, 8.0, tuple([0.0] * 5 + [0.2] * 5)),
                 "b": Scan("bearish", -0.4, 1.0, 0.0, 3.5, 2.0, 5.0, tuple([0.2] * 5 + [0.0] * 5)),
                 "c": Scan("volatile", 0.0, 1.3, 0.0, 5.0, 1.0, 9.0, tuple([0.1] * 10)),
                 "d": Scan(error="numpy said no")}
    swarm.summarise(pic)
    assert pic.counts == {"bullish": 1, "neutral": 1, "bearish": 1} and pic.n == 3
    assert pic.shades == {"bullish": 1, "bearish": 1, "volatile": 1}
    assert pic.probs == pytest.approx([0.1] * 10)               # the three pictures average out to the flat one
    assert pic.median == pytest.approx(5.0) and pic.band == pytest.approx((1.0, 9.0))
    assert pic.market_median == pytest.approx(5.0) and pic.gap == pytest.approx(0.0)
    assert pic.agreement == pytest.approx(1 - (0.5 + 0.5 + 0.0) / 3)     # two disagree with it by half, one is it
    assert pic.lean(4.0) == pytest.approx(0.25)


def test_the_horizons_follow_the_series_and_a_close_is_only_asked_about_once():
    from glimpse_tui.term.hub import Hub

    class View:
        def __init__(self, end, topic):
            self.row = type("R", (), {"end_time_utc": end, "topic_id": topic})()

    now = time.time()
    s = swarm.Swarm(Hub())
    hourly = [View(now + 3600 * (i + 1), i) for i in range(100)]
    assert [lb for lb, _ in s.choose(hourly, 3600)] == ["next hour", "in 24 hours", "in 72 hours"]
    assert [v.row.topic_id for _, v in s.choose(hourly, 3600)] == [0, 23, 71]
    daily = [View(now + 86400 * (i + 1), i) for i in range(40)]
    assert [lb for lb, _ in s.choose(daily, 86400)] == ["next close", "in a week", "in a month"]
    assert [v.row.topic_id for _, v in s.choose(daily, 86400)] == [0, 6, 29]
    assert len(s.choose(hourly[:1], 3600)) == 1                 # one close: asked about once, not three times
    assert s.choose([], 3600) == []
    far = [View(now + 400 * 86400, 0)]                          # nothing near any horizon: nothing to ask about,
    assert s.choose(far, 3600) == [] and s.choose(far, 86400) == []     # and no model walks 400 days of paths


# ── the windows ─────────────────────────────────────────────

async def test_cons_counts_the_zoo_at_three_horizons_and_draws_what_it_agrees_on(make):
    app = make()
    async with app.run_test(size=(190, 50)) as pilot:
        cons = await ready(pilot, app)
        text = cons.render().plain
        assert "NEXT HOUR" in text and "IN 24 HOURS" in text and "IN 72 HOURS" in text
        assert "bullish" in text and "bearish" in text and "neutral" in text and "they agree" in text
        assert "WHO STANDS OUT" in text and "most bullish" in text and "furthest from the market" in text
        pic = cons.swarm.pictures[0]
        n = len(cons.swarm.bots)
        assert pic.n == n and f"{pic.n} of {n} bots priced the next hour" in text
        assert pic.band[0] < pic.median < pic.band[1] and pic.market_median > 0
        head, rows = cons.export()
        assert head[0] == "horizon" and len(rows) == 3 and rows[0][2] + rows[0][3] + rows[0][4] == n
        assert cons.enter() == "BOTS"


async def test_bot_shows_one_model_against_the_market_and_the_brackets_walk_the_zoo(make):
    app = make()
    async with app.run_test(size=(190, 50)) as pilot:
        await ready(pilot, app)
        bot = pane(app, "BOT")
        await until(pilot, lambda: bot.bot() is not None)
        first = bot.bot()
        text = bot.render().plain
        assert first.name in text and f"bot 1 of {len(bot.swarm.bots)}" in text
        assert "what the bots expect" not in pane(app, "CONS").title and "bots expect" in pane(app, "CONS").title
        assert "this bot" in text and "next hour" in text and "in 72 hours" in text and "market" in text
        assert "price at close" in text                          # its whole picture, against what the market charges
        await pilot.press("4", "]")                              # the page's own keys reach the window it is on
        await pilot.pause(0.05)
        assert bot.at == 1 and bot.bot() is not first
        assert bot.key("]", "]") and bot.at == 2
        assert bot.key("[", "[") and bot.key("[", "[") and bot.bot() is first
        assert bot.key("[", "[") and bot.at == len(bot.swarm.bots) - 1        # it wraps
        assert bot.enter() == f"BOTS {bot.bot().id}"
        assert not bot.key("z", "z")


async def test_one_pass_serves_both_windows_and_is_not_repeated_for_the_same_bar(make):
    app = make()
    async with app.run_test(size=(190, 50)) as pilot:
        cons = await ready(pilot, app)
        bot = pane(app, "BOT")
        assert bot.swarm is cons.swarm and app.shell.hub.swarm("BTC") is cons.swarm
        at, before = cons.swarm.at, len(cons.swarm.pictures)
        await cons.load()                                        # a second load finds the same bar and the same closes
        assert cons.swarm.at == at and len(cons.swarm.pictures) == before
        assert app.shell.hub.swarm("XAU") is not cons.swarm       # gold is a pass of its own


async def test_the_bots_window_opens_the_zoo_on_the_model_it_was_showing(make):
    app = make()
    async with app.run_test(size=(190, 50)) as pilot:
        await ready(pilot, app)
        bot = pane(app, "BOT")
        app.shell.run(bot.enter())
        await until(pilot, lambda: app.view == "bots")
        assert app.bot_filter == bot.bot().id and app.bot_cur == 0


async def test_both_windows_fit_every_width_and_say_what_they_are_doing_while_they_read(make):
    from rich.cells import cell_len

    app = make()
    async with app.run_test(size=(190, 50)) as pilot:
        await ready(pilot, app)
        both = [pane(app, "CONS"), pane(app, "BOT")]
        for p in both:
            assert p.error == "" and p.title and p.hint() and p.menu() and p.provs
            for w, h in ((40, 12), (52, 16), (60, 20), (76, 24), (84, 30), (132, 36), (196, 60)):
                lines = p.draw(w, h)
                assert lines, (p.code, w)
                for ln in lines:
                    assert cell_len(ln.plain) <= w, (p.code, w, ln.plain)
                    assert "\n" not in ln.plain
        swarm = both[0].swarm                                       # one pass, shared: emptying it empties both windows
        swarm.pictures, swarm.error = [], ""                        # before the first pass: never an empty window
        for p in both:
            assert "reading" in "\n".join(ln.plain for ln in p.draw(60, 20))
        swarm.error = "no hourly candles for BTC yet"
        for p in both:
            assert "no hourly candles" in "\n".join(ln.plain for ln in p.draw(60, 20))
