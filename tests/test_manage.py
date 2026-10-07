"""A bot managing the account's whole portfolio: it sells positions it did not open when the market overpays them, at a
profit, and buys what its picture says is cheap. Fake API, synthetic candles: no network, no orders."""
import asyncio

import pytest
from test_bots import (
    BINS,
    FakeApi,
    Feed,
    bot,
    make,  # noqa: F401  (the screen fixture)
    until,
)

from glimpse_tui import bots, botsview
from glimpse_tui import policy as PL
from glimpse_tui.api import ApiError, Position


class Holder(FakeApi):
    """An account that already holds positions in the first close, bought by hand on the website."""

    def __init__(self, key="k", base_url=None):
        super().__init__(key, base_url)
        self.held: list[Position] = []
        self.reads = 0

    def hold(self, index: int, contracts: float, cost: float) -> None:
        self.held.append(Position(100, index + 1, "BTC", "", contracts, cost, cost, self.end, "active"))

    async def positions(self):
        self.reads += 1
        return list(self.held)


def manager(api, tmp_path, b, live=False, **cfg):
    cfg = {"objective": "match", "interval_s": 0, "kelly": 1.0, **cfg}      # match is the default: a manager trades on value anyway
    return bots.Runner(bot=b, api=api, batch_id="b-h", feed=Feed(), live=live, ledger=bots.Ledger(tmp_path / "ledger.json"),
                       cfg=bots.BotConfig(**cfg), manage=True, series="BTC")


def test_profit_only_never_sells_an_overpriced_position_below_what_it_cost():
    api = FakeApi()
    api.shares[90] += 400                                                  # the market pays up for range 90
    book = asyncio.run(api.book(100))
    worthless = [1e-9] * len(BINS)                                         # and the picture says it is worth nothing
    worthless[10] = 1 - 1e-9 * (len(BINS) - 1)
    o = book.option_ids[90]
    cheap, dear = {o: (50.0, 50 * 1.0)}, {o: (50.0, 50 * 99.0)}            # bought at 1 sat a contract, or at 99
    assert PL.sell_down(book, worthless, dear, 0.02) and not PL.sell_down(book, worthless, dear, 0.02, profit_only=True)
    sold = PL.sell_down(book, worthless, cheap, 0.02, profit_only=True)
    assert sold and sold[0][2] > 50 * 1.0                                  # sold, and for more than it cost


async def test_a_manager_sells_your_winner_keeps_your_loser_and_buys_what_is_cheap(tmp_path):
    api = Holder()
    api.hold(90, 50, 50 * 1.0)                                             # bought cheap: now a winner
    api.hold(199, 50, 50 * 99.0)                                           # bought dear: a loser even at today's price
    api.shares[90] += 400
    api.shares[199] += 400                                                 # the market overpays both, by the bot's picture
    r = manager(api, tmp_path, bot(10), live=True, bankroll_sats=20_000)
    assert r.cfg.objective == "edge"
    await r.cycle()
    assert [(t, o) for t, o, _ in api.sold] == [(100, 91)]                 # never touched by a bot, sold all the same
    assert api.sold[0][2] == pytest.approx(50)
    assert any("SELL" in x and "on cost" in x for x in r.log)
    assert api.bought and all(o == 11 for o, _ in api.bought[0])           # and the cheap range its picture favours, bought
    held = r.ledger.legs(bots.PORTFOLIO, "live", 100)
    assert 91 not in held and held[200][0] == pytest.approx(50) and held[11][0] > 0


async def test_without_profit_only_a_manager_also_trims_an_overpriced_loser(tmp_path):
    api = Holder()
    api.hold(199, 50, 50 * 99.0)
    api.shares[199] += 400
    r = manager(api, tmp_path, bot(10), live=True, bankroll_sats=20_000, profit_only=False)
    await r.cycle()
    assert [(t, o) for t, o, _ in api.sold] == [(100, 200)]


async def test_a_bot_of_its_own_still_never_touches_your_positions(tmp_path):
    api = Holder()
    api.hold(90, 50, 50.0)
    api.shares[90] += 400
    r = bots.Runner(bot=bot(10), api=api, batch_id="b-h", feed=Feed(), live=True, ledger=bots.Ledger(tmp_path / "l.json"),
                    cfg=bots.BotConfig(objective="edge", interval_s=0, bankroll_sats=0.5))
    await r.cycle()
    assert api.sold == [] and api.reads == 0


async def test_the_account_is_read_again_every_few_seconds_live_and_a_failed_read_trades_nothing(tmp_path):
    api = Holder()
    api.hold(90, 50, 50.0)
    r = manager(api, tmp_path, bot(10), live=True, bankroll_sats=20_000, portfolio_s=10)
    await r.cycle()
    await r.cycle()
    assert api.reads == 1                                                  # fills in between are booked locally
    api.held.clear()                                                       # sold by hand on the website
    r._synced_at -= 11
    await r.cycle()
    assert api.reads == 2 and 91 not in r.ledger.legs(bots.PORTFOLIO, "live", 100)

    async def down():
        raise ApiError("Glimpse is not answering")
    api.positions = down
    r._synced_at -= 11
    bought = len(api.bought)
    await r.cycle()
    assert len(api.bought) == bought and "cannot read your portfolio" in r.log[-1]


async def test_on_paper_a_manager_copies_your_portfolio_and_its_sales_fund_its_buys(tmp_path):
    api = Holder()
    api.hold(90, 50, 50.0)
    api.shares[90] += 400
    r = manager(api, tmp_path, bot(10), bankroll_sats=0.5, max_per_cycle_sats=1e6, max_per_hour_sats=1e6)
    row = (await api.all_markets("b-h"))[0]
    await r.sync([row])
    assert r._paper_book(row).shares[90] == pytest.approx(api.shares[90])     # your real contracts are in the book already
    await r.cycle()
    assert api.sold == [] and api.bought == []                             # paper: nothing reaches the market
    assert r.sold_sats > 50                                                # it sold your winner, on paper, at a profit
    assert 1 <= r.paid_sats <= r.sold_sats + 0.5 + 1e-6                    # and bought with the proceeds: 0.5 fresh sats alone buy nothing
    book = r._paper_book(row)
    assert book.shares[90] == pytest.approx(api.shares[90] - 50)           # the paper sale moved the paper book
    assert book.shares[10] > api.shares[10]                                # and so did the paper buy
    page = botsview.portfolio(r, "BTC", 120).plain
    assert "managing your BTC portfolio" in page and "fresh sats" in page
    assert "manages your BTC portfolio" in botsview.log_title(None, r)


async def test_r_lets_a_bot_manage_your_portfolio_once_you_are_logged_in(make):  # noqa: F811
    app = make()
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        assert "R let it manage your portfolio" in app.query_one("#botdetail").render().plain
        await pilot.press("R")
        await pilot.pause()
        assert not app.runners and "log in" in app.flash.lower()


async def test_r_manages_on_paper_or_live_and_only_one_manager_a_series(make):  # noqa: F811
    app = make("glp_live_" + "ab" * 32)
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        await pilot.press("slash", *"iron condor", "enter")
        await pilot.press("R", *"400", "enter")                        # fresh sats, then the real-or-paper question
        await pilot.press("enter")                                     # enter alone: paper
        await until(pilot, lambda: "opt_iron_condor" in app.runners and app.runners["opt_iron_condor"].cycles >= 1)
        r = app.runners["opt_iron_condor"]
        assert r.manage and not r.live and r.cfg.bankroll_sats == 400 and r.cfg.objective == "edge"
        assert "managing your portfolio" in app.query_one("#botdetail").render().plain
        assert "manages your BTC portfolio" in app.query_one("#botlog").border_title
        await pilot.press("escape", "slash", *"ema", "enter")          # a second manager on the same series is refused
        other = botsview.visible(app)[0]
        await pilot.press("R")
        await pilot.pause()
        assert other.id not in app.runners and "already manages" in app.flash
        await pilot.press("X", "escape", "slash", *"iron condor", "enter")
        await pilot.press("R", *"400", "enter", *"LIVE", "enter")
        await until(pilot, lambda: app.runners["opt_iron_condor"] is not r)
        assert app.runners["opt_iron_condor"].live and app.runners["opt_iron_condor"].manage
        await pilot.press("X")
