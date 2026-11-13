"""Funding: a bot's account, its equity over time, and a bankroll changed while the bot runs. No network, no orders."""
import time

import pytest
from test_bots import until
from test_desk import TwoSeries
from test_desk import make as make_desk
from test_noc import screen  # noqa: F401  the monitor's fake exchange

from glimpse_tui import app as A
from glimpse_tui import funding as F
from glimpse_tui import nocview


def test_what_is_typed_becomes_a_bankroll():
    f = F.Funds(100_000, 40_000, 41_000, 0, 50_000, 100_000)
    paper = lambda s: F.parse(s, f, live=False, desk=True)  # noqa: E731
    assert paper("+50k") == 150_000 and paper("-20,000") == 80_000 and paper("+25%") == 125_000 and paper("-10%") == 90_000
    assert paper("x2") == 200_000 and paper("1.5x") == 150_000 and paper("250k") == 250_000 and paper("₿1.5m sats") == 1_500_000
    for bad in ("", "lots", "25%", "max", "-100k", "5"):
        with pytest.raises(F.Refused):
            paper(bad)
    live = F.Funds(100_000, 40_000, 41_000, 0, 60_000, 100_000, wallet=70_000, ceiling=110_000)
    assert F.parse("max", live, live=True, desk=False) == 110_000            # a runner: what the wallet allows
    with pytest.raises(F.Refused, match="110,000"):
        F.parse("x2", live, live=True, desk=False)                           # more than the wallet holds
    assert F.parse("max", live, live=True, desk=True) == 0                   # a desk: no cap, the whole wallet
    whole = F.Funds(110_000, 40_000, 41_000, 0, 59_000, 0.0, wallet=70_000, ceiling=110_000)
    with pytest.raises(F.Refused, match="whole wallet"):
        F.parse("+25%", whole, live=True, desk=True)
    assert F.parse("80k", whole, live=True, desk=True) == 80_000             # but it can be capped


def test_equity_is_kept_across_sessions_and_capital_added_is_not_a_gain(tmp_path):
    e = F.Equity(tmp_path / "eq.json")
    assert e.net("bell:paper") is None
    assert e.sample("bell:paper", 1_000, 100_000, 100_000) and not e.sample("bell:paper", 1_030, 100_500, 100_000)   # a minute apart
    e.sample("bell:paper", 1_060, 101_000, 100_000)
    e.flow("bell:paper", 1_070, 50_000)                                      # a top-up
    e.sample("bell:paper", 1_120, 151_500, 150_000)
    made, capital, since = e.net("bell:paper")
    assert (made, capital, since) == (1_500, 150_000, 1_000)
    assert e.carry("bell:paper", 800.0, 12) == 0.0                           # this session has realised 800 in 12 trades
    assert e.carry("bell:paper", 0.0, 0) == 800.0                            # a new runner starts from zero: the 800 is carried
    assert e.carry("bell:paper", 50.0, 1) == 800.0
    e.save(force=True)
    again = F.Equity(tmp_path / "eq.json")
    assert again.points("bell:paper") == e.points("bell:paper") and again.net("bell:paper", 152_000)[0] == 2_000
    for i in range(F.MAX_POINTS + 10):
        again.sample("x", i * 60, i, 1)
    assert len(again.points("x")) <= F.MAX_POINTS and again.points("x")[-1][1] == F.MAX_POINTS + 9


async def test_a_larger_bankroll_buys_more_at_a_falling_return_and_a_desk_takes_it_without_stopping(tmp_path):
    api = TwoSeries()
    d = await make_desk(api, tmp_path, budget=20_000)
    await d.once()
    f = F.funds(d, value=d.open_cost)
    assert f.bankroll == 20_000 and f.deployed == pytest.approx(d.open_cost) and f.free == pytest.approx(d.deployable - d.open_cost)
    assert f.equity == pytest.approx(20_000) and 0 < f.used < 0.9
    before = (dict(d.ledger.data[d.key][d.mode]), d.trades, len(d.log))
    base, a, b = F.ladder(d, [40_000, 80_000])
    assert (dict(d.ledger.data[d.key][d.mode]), d.trades, len(d.log)) == before      # a reading: nothing bought, nothing said
    assert base.cost < a.cost < b.cost and a.closes > 0
    ra, rb = (a.worth - a.cost) / a.cost, (b.worth - b.cost) / b.cost
    assert ra > rb > d.cfg.min_edge                                             # more capital, a thinner return on it
    assert "more" in F.rung_line(base, a, "x2") and "on the sats added" in F.rung_line(base, a, "x2")
    text = F.briefing(d, f, [base, a, b])
    assert "Bankroll ₿20,000" in text and "x2" in text and "max" not in text    # paper: no wallet to take the most of

    cost = d.open_cost
    F.apply(d, 40_000)
    assert d.budget == 40_000 and d.bankroll == 40_000 and "bankroll set to ₿40,000" in d.log[-1]
    await d.tick(force=True)
    assert d.open_cost > cost * 1.5 and d.open_cost <= d.deployable + 1e-6      # the next pass puts it to work


def test_a_runners_caps_follow_its_new_budget():
    from test_bots import FakeApi, Feed, bell

    from glimpse_tui import bots

    r = bots.Runner(bot=bell(76_500), api=FakeApi(), batch_id="b", feed=Feed(), cfg=bots.BotConfig().with_budget(10_000))
    assert r.cfg.max_per_cycle_sats == 2_000
    F.apply(r, 50_000)
    assert r.cfg.bankroll_sats == 50_000 and r.cfg.max_per_cycle_sats == 10_000 and r.cfg.max_per_hour_sats == 500_000
    f = F.funds(r)
    assert f.bankroll == 50_000 and f.free == 50_000 and f.equity == 50_000 and f.ceiling is None


async def test_b_on_the_monitor_raises_a_running_desks_bankroll(tmp_path, screen):  # noqa: F811
    app = screen()
    async with app.run_test(size=(200, 60)) as pilot:
        await until(pilot, lambda: app.views)
        d = await make_desk(app.api, tmp_path, budget=20_000)
        d.ledger = app.ledger
        app.runners[d.bot.id] = d
        d.start()
        await pilot.press("M")
        await until(pilot, lambda: len(app.noc.units) == 2 and d.open_cost > 0)
        await pilot.press("j")
        await pilot.pause(1.2)
        fleet = app.query_one("#nocfleet").render().plain
        assert "FUNDS" in fleet and "bankroll ₿20,000" in fleet and "EQUITY" in fleet and "b adds" in fleet
        key = f"{d.bot.id}:paper"
        assert app.noc.equity.points(key) and app.noc.unit.funds.equity == pytest.approx(app.noc.equity.points(key)[-1][1], rel=0.05)

        cost = d.open_cost
        await pilot.press("b")
        await until(pilot, lambda: isinstance(app.screen, A.Prompt))
        hint = str(app.screen.query_one("#dialog-hint").render())
        assert "Bankroll ₿20,000" in hint and "A larger bankroll" in hint and "x2" in hint
        for ch in "x2":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: d.budget == 40_000)
        assert d.running and app.bot_budget(d.bot.id) == 40_000                # no stop, and remembered for the next start
        assert app.noc.equity.data[key]["flows"][-1][1] == 20_000
        made, capital, _ = app.noc.equity.net(key)
        assert capital == pytest.approx(40_000, rel=0.02) and abs(made) < 2_000  # the 20,000 added is capital, not a gain
        await until(pilot, lambda: d.open_cost > cost)                          # and it is put to work on the next pass
        assert "bankroll ₿40,000" in app.query_one("#nocfleet").render().plain

        await pilot.press("b")
        await until(pilot, lambda: isinstance(app.screen, A.Prompt))
        for ch in "max":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert d.budget == 40_000                                               # paper has no wallet: refused, unchanged
        d.stop()


async def test_a_live_raise_asks_first(tmp_path, screen):  # noqa: F811
    app = screen(key="k")
    async with app.run_test(size=(200, 60)) as pilot:
        await until(pilot, lambda: app.views and app.wallet)
        d = await make_desk(app.api, tmp_path, live=True, budget=10_000)
        d.ledger = app.ledger
        app.runners[d.bot.id] = d
        await d.refresh_balance()
        d.ticks = 1                                                             # it has run, and is stopped: a row of the fleet
        assert d.bankroll == 10_000                                             # the wallet holds 50,000; the cap is 10,000
        await pilot.press("M")
        await until(pilot, lambda: len(app.noc.units) >= 2 and app.noc.units[1].kind == "desk")
        await pilot.press("j", "b")
        await until(pilot, lambda: isinstance(app.screen, A.Prompt))
        assert "Wallet: ₿50,000 free" in str(app.screen.query_one("#dialog-hint").render())
        for ch in "max":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, A.Confirm))
        await pilot.press("n")
        await pilot.pause(0.2)
        assert d.budget == 10_000
        await pilot.press("b")
        await until(pilot, lambda: isinstance(app.screen, A.Prompt))
        for ch in "max":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, A.Confirm))
        await pilot.press("y")
        await until(pilot, lambda: d.budget == 0)
        assert d.bankroll == 50_000 and "(wallet)" in app.query_one("#nocfleet").render().plain
        assert time.time() - app.noc.equity.data[f"{d.bot.id}:live"]["flows"][-1][0] < 60
        assert nocview.funds_lines(app, app.noc.unit, 76)
