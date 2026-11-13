"""The WTI/BTC series: a barrel of WTI crude in satoshis, priced by Glimpse's own oracle rather than an exchange. The title
names it, it orders last, its candles and spot read the market's chart endpoint through any open close, the forecast
windows and the heatmap draw from the same place, and so do the bots' hourly bars. No network: the oracle answers from
tests/fixtures/sources/glimpse/."""
import json
import math
import time

import httpx
import pandas as pd
import pytest
import test_app as T
from offline import ROOT, fixture

import glimpse_tui.app as A
from glimpse_tui import api, auth
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, Candle, MarketRow
from glimpse_tui.funcs import forecast, gp
from glimpse_tui.term import gobar
from glimpse_tui.term.hub import Hub
from glimpse_tui.term.shell import PAGES
from glimpse_tui.zoo import data

WTI = Batch("b-wti", "Hourly WTI/BTC Prediction Markets", 169, 1000)
CHART = ("main.bpmapi.io", "/api/v1/nmarket/markets/8422/chart")
HOUR = fixture("glimpse/chart_wtibtc_1h.json")["candles"]
MINUTE = fixture("glimpse/chart_wtibtc_1min.json")["candles"]


class SdkBatch:
    """A batch as the SDK hands it back."""

    def __init__(self, batch_id: str, title: str) -> None:
        self.batch_id, self.main_topic_title, self.topic_count, self.outcome_interval = batch_id, title, 3, 1000


class SdkBatches:
    batches = [SdkBatch("w", "Hourly WTI/BTC Prediction Markets"), SdkBatch("g", "Hourly Gold Prediction Markets"),
               SdkBatch("b", "Hourly Bitcoin Prediction Markets"), SdkBatch("e", "Daily Ethereum Market")]


class WtiApi(T.FakeApi):
    """The fake API with the WTI/BTC series alone, so the terminal opens on it without reaching Coinbase for Bitcoin."""

    async def batches(self):
        return [WTI]

    async def markets(self, batch_id, limit=96, on_rows=None):
        first = (int(time.time()) // 3600 + 1) * 3600
        return [MarketRow(8422 + i, f"Hourly WTI/BTC Prediction Markets - 2026-10-27 {14 + i}:00", first + i * 3600, "live", 0, 0,
                          tuple(T.FIX["shares"]), tuple(T.FIX["names"])) for i in range(3)]

    async def oracle_topic(self, asset):
        return 8422


class Close:
    """A loaded Glimpse close, as the hub hands one to a chart."""

    def __init__(self, end: float, median: float, lo: float, hi: float) -> None:
        self.row, self.median, self.band = type("R", (), {"end_time_utc": end, "topic_id": int(end)})(), median, (lo, hi)


def text(lines) -> str:
    return "\n".join(t.plain for t in lines)


def test_the_title_names_the_series_and_it_is_an_hourly_series_with_a_feed_but_no_exchange_pair():
    assert api.asset_of("Hourly WTI/BTC Prediction Markets") == "WTIBTC"
    assert api.asset_of("Hourly WTI/BTC Prediction Markets - 2026-10-27 14:00") == "WTIBTC"
    assert api.asset_of("Hourly Oil/BTC Markets") == "WTIBTC"                  # the wording the first oil markets used
    assert api.asset_of("Hourly Bitcoin Prediction Markets") == "BTC"          # "btc" alone is not oil
    assert api.asset_of("Daily WTI Markets") == ""                             # oil in dollars is no series we know
    assert WTI.short == "WTIBTC" and WTI.hourly and WTI.asset == "WTIBTC"
    assert "WTIBTC" in api.ASSETS and "WTIBTC" in api.HOURLY and "WTIBTC" in api.ORACLE and "WTIBTC" not in api.PAIRS
    assert api.series_name("WTIBTC", daily=True) == "WTIBTC 1D"
    assert {"DIST WTIBTC", "FCST WTIBTC", "GP WTIBTC 24H"} <= {label for label, _ in PAGES}     # on the search list


async def test_the_series_list_puts_wti_btc_last_and_any_open_close_names_it_to_the_oracle(monkeypatch):
    g, calls = api.Glimpse(), []

    async def call(fn, *a, **kw):
        calls.append(a)
        return SdkBatches() if not a else {"markets": [{"topic_id": 8422}]}

    monkeypatch.setattr(g, "_call", call)
    try:
        assert [b.short for b in await g.batches()] == ["BTC", "ETH", "XAU", "WTIBTC"]
        calls.clear()
        assert await g.oracle_topic("WTIBTC") == 8422
        asked = [c for c in calls if c and "active-markets" in c[0]]              # the series list asks both services
        assert len(asked) == 1 and asked[0][0].endswith("/v2/batches/w/active-markets") and asked[0][1]["limit"] == 1
        before = len(calls)
        assert await g.oracle_topic("WTIBTC") == 8422 and len(calls) == before    # kept: not asked again for ten minutes
        with pytest.raises(api.ApiError, match="no SOL series"):
            await g.oracle_topic("SOL")
    finally:
        await g.close()


async def test_the_oracle_chart_answers_candles_and_the_price_now(no_network):
    no_network.by_path[CHART] = ROOT / "glimpse/chart_wtibtc_1h.json"
    got = await api.oracle_candles(8422, 3600)
    assert len(got) == len(HOUR) == 48 and got[0].t == HOUR[0]["time"] and got[-1].c == HOUR[-1]["close"]
    assert all(b.t - a.t == 3600 for a, b in zip(got, got[1:], strict=False))
    assert all(c.l <= min(c.o, c.c) <= max(c.o, c.c) <= c.h for c in got)
    assert 50_000 < got[-1].c < 300_000                                          # satoshis a barrel, on the market's ladder
    assert "main.bpmapi.io" in no_network.seen[-1] and "period=1h" in no_network.seen[-1]
    assert await api.candles("WTIBTC", 3600, 8422) == got                        # the general call, through a close
    assert await api.candles("WTIBTC", 3600) == [] and len(no_network.seen) == 2  # no close named: nothing to ask for
    with pytest.raises(ValueError):
        await api.oracle_candles(8422, 7200)                                     # not a candle the oracle serves
    no_network.by_path[CHART] = ROOT / "glimpse/chart_wtibtc_1min.json"
    assert await api.oracle_spot(8422) == MINUTE[-1]["close"]                    # the minute in progress is the price now
    assert "period=1min" in no_network.seen[-1] and "start=" in no_network.seen[-1] and "end=" in no_network.seen[-1]


async def test_a_window_inside_the_oil_weekend_is_asked_again_from_a_weekday(no_network, monkeypatch):
    # The server 502s a window with no WTI/USD candle in it (Saturday noon to now) but answers one that reaches Friday.
    no_network.by_path[CHART] = ROOT / "glimpse/chart_wtibtc_1h.json"
    start = HOUR[-3]["time"]
    answer = type(no_network).__call__

    def weekend(self, request):
        if request.url.params.get("start") == str(start):
            self.seen.append(str(request.url))
            return httpx.Response(502, json={"error": "twelvedata returned HTTP 400: No data is available"})
        return answer(self, request)

    monkeypatch.setattr(type(no_network), "__call__", weekend)
    got = await api.oracle_candles(8422, 3600, start=start, end=HOUR[-1]["time"] + 600)
    assert [c.t for c in got] == [c["time"] for c in HOUR[-3:]]                 # the hours asked for, none earlier
    assert f"start={start - api.WEEKEND_REACH}" in no_network.seen[-1] and len(no_network.seen) == 2
    no_network.by_path[CHART] = ROOT / "glimpse/chart_wtibtc_1min.json"
    assert await api.oracle_spot(8422) == MINUTE[-1]["close"]                    # the price now, on a Saturday too


@pytest.fixture
def terminal(monkeypatch):
    """The terminal on the WTI/BTC series, with the oracle answering from a synthetic week around the fixture ladder."""
    asked = []

    async def oracle_candles(topic_id, granularity, start=None, end=None):
        asked.append((topic_id, granularity))
        if granularity == 60:
            t = int(time.time()) // 60 * 60
            return [Candle(t, 76_480, 76_520, 76_470, 76_500.0)]
        if granularity == 900:
            n, step = 672, 900
            t0 = int(time.time()) // 1800 * 1800 - 335 * 1800      # from a half hour, so the quarter hours pair up whole
        else:
            n, step = 168, 3600
            t0 = int(time.time()) // 3600 * 3600 - 167 * 3600
        return [Candle(t0 + i * step, 76_000 + i % 7, 76_050 + i % 7, 75_950 + i % 7, 76_010.0 + i % 7) for i in range(n)]

    monkeypatch.setattr(A.api_mod, "oracle_candles", oracle_candles)
    monkeypatch.setattr(A, "Glimpse", WtiApi)
    monkeypatch.setattr(auth, "load_key", lambda: (None, "none"))
    app = A.Terminal()
    app.asked = asked
    return app


async def test_the_odds_and_the_heatmap_open_on_wti_btc_with_the_oracle_as_spot_and_history(terminal):
    app = terminal
    async with app.run_test(size=(140, 40)) as pilot:
        await T.ready(app, pilot)
        await T.until(pilot, lambda: bool(app.views))
        assert app.batch.short == "WTIBTC" and app.asset == "WTIBTC" and app.hourly
        assert app.slip_title.startswith("WTIBTC at ") and app.slip_title.endswith(" UTC")
        assert "WTIBTC" in str(app.query_one("#head").render()) and "Hourly" not in str(app.query_one("#head").render())
        app.load_spot()
        await T.until(pilot, lambda: app.spot == 76_500.0)                      # the oracle's minute in progress
        await T.until(pilot, lambda: "76,500" in str(app.query_one("#head").render()))      # on the next repaint
        await pilot.press("f")
        await T.until(pilot, lambda: bool(app.candles) and bool(app.candles_long))
        assert app.candle_s == 1800 and len(app.candles) == 336 and len(app.candles_long) == 168     # 15s paired up; a week of hours
        assert set(app.asked) >= {(8422, 60), (8422, 900), (8422, 3600)}
        assert all(topic == 8422 for topic, _ in app.asked)                     # always through the nearest close on screen


async def test_fcst_and_dist_wti_btc_read_the_oracle_for_history_and_spot(no_network):
    hub = Hub()
    hub.glimpse = lambda: WtiApi(None)
    no_network.by_path[CHART] = ROOT / "glimpse/chart_wtibtc_1h.json"
    pane = forecast.FcstPane(hub, None, ("WTIBTC",))
    assert pane.series == "WTIBTC" and pane.command() == "FCST WTIBTC"
    await pane.load()
    assert len(pane.candles) == 48 and pane.spot() == HOUR[-1]["close"]          # the candle in progress is the price now
    assert pane.title == "WTI/BTC · next 48 hours" and pane.cadence == 3600
    assert not {"WTIBTC", "WTI", "PAXG"} & set(hub.watch)                         # no quote-board feed stands in for the oracle
    out = text(pane.draw(110, 22))
    assert "NOW" in out and "Now " in out and "likely (80%)" in out
    assert "/markets/8422/chart" in no_network.seen[-1]                          # named through the nearest open close
    no_network.by_path[CHART] = ROOT / "glimpse/chart_wtibtc_1min.json"
    dist = forecast.DistPane(hub, None, ("WTIBTC",))
    await dist.load()
    assert dist.command() == "DIST WTIBTC" and dist.spot() == MINUTE[-1]["close"]
    cons = forecast.FcstPane(hub, None, ("WTIBTC",))
    cons.wants_candles = False                                                   # CONS and BOT: the closes, no history
    await cons.load()
    assert cons.candles == [] and cons.spot() == MINUTE[-1]["close"]             # and still a price now, the cheap way
    assert "period=1min" in no_network.seen[-1]


async def test_gp_wti_btc_charts_the_oracle_and_runs_into_the_hourly_series(no_network):
    hub = Hub()
    hub.glimpse = lambda: WtiApi(None)
    no_network.by_path[CHART] = ROOT / "glimpse/chart_wtibtc_1h.json"
    now = time.time()
    hub.series_views["WTIBTC"] = [Close(now + 3600 * (i + 1), 126_000 + i, 125_000 - i, 127_000 + i) for i in range(48)]
    pane = gp.GpPane.from_command(hub, gobar.Command("GP", (hub.book.get("WTIBTC"),), ("24H",)))
    await pane.reload()
    ln = pane.lines[0]
    assert pane.view == "24H" and ln.ins.ticker == "WTIBTC" and ln.error == ""
    assert len(ln.candles) == 24 and ln.candles[-1].c == HOUR[-1]["close"] and ln.prov.source == "glimpse"
    assert pane.series == "WTIBTC" and pane.command() == "GP WTIBTC 24H"
    assert pane.title == "WTI/BTC · the last 24 hours and the next 24 hours"      # named as FCST names it, as gold is "Gold"
    assert pane._fc() and pane._fc()[-1][0] <= now + 24 * 3600                  # the hourly closes inside the window
    assert "NOW" in text(pane.draw(110, 22))
    plain = gp.GpPane.from_command(hub, gobar.Command("GP", (hub.book.get("WTIBTC"),)))
    await plain.reload()
    assert plain.view == "1H"                                                    # no window named: hourly, as for Bitcoin


async def test_the_bots_hourly_bars_for_wti_btc_come_from_the_oracle_in_one_page(no_network, monkeypatch):
    asked = []

    async def topic(asset):
        return 8422

    async def oracle_candles(topic_id, granularity, start=None, end=None):
        asked.append((topic_id, granularity, start, end))
        if granularity == 60:
            return [Candle(end // 60 * 60, 125_900, 125_950, 125_850, 125_909.0)]
        t0 = start // 3600 * 3600
        return [Candle(t, 120_000 + (t // 3600) % 50, 120_100 + (t // 3600) % 50, 119_900 + (t // 3600) % 50,
                       120_010.0 + (t // 3600) % 50) for t in range(t0, end + 1, 3600)]

    monkeypatch.setattr(data, "oracle_topic", topic)
    monkeypatch.setattr(data, "oracle_candles", oracle_candles)
    monkeypatch.setattr(api, "oracle_candles", oracle_candles)
    monkeypatch.setattr(data, "_MEM", {})
    monkeypatch.setattr(data, "_LOCKS", {})
    now = 1_793_108_000.0
    df = await data.hourly_bars("WTIBTC", now=now)
    assert len(asked) == 1 and asked[0][:2] == (8422, 3600) and asked[0][3] == int(now)      # one request, not seven pages
    assert asked[0][2] <= int(now) - data.HOURS * 3600
    assert data.HOURS - 2 <= len(df) <= data.HOURS and int(df.index[-1].timestamp()) == (int(now) // 3600 - 1) * 3600
    assert (df.index.to_series().diff().dropna() == pd.Timedelta("1h")).all()   # unbroken: no weekend gap on the ratio
    assert (df["volume"] == 0).all() and df["close"].between(120_000, 120_100).all()
    assert (data.cache_dir() / "bars-WTIBTC-1h.csv").is_file()
    again = await data.hourly_bars("WTIBTC", now=now)
    assert len(asked) == 1 and len(again) == len(df)                            # the second call is served from memory
    assert await data.spot_price("WTIBTC") == 125_909.0
    with pytest.raises(ValueError, match="no price feed"):
        await data.hourly_bars("DOGE", now=now)


# ── pricing: a 250-range ladder is priced on its own length ─────────────

EST = json.loads((ROOT.parent / "estimates_wtibtc.json").read_text())


def test_alpha_follows_the_ladder_up_to_500_ranges():
    """The server's alpha is V / (m ln m) with m the ladder's length capped at 500: WTI/BTC's 250 ranges are priced on
    250, SOL's 650 on 500. Pricing every ladder on 500 made the bots skip every WTI/BTC buy as "book moved"."""
    assert P.alpha_for(250) == pytest.approx(2 / (250 * math.log(250)))
    assert P.alpha_for(500) == pytest.approx(2 / (500 * math.log(500)))
    assert P.alpha_for(650) == P.alpha_for(500)
    assert P.alpha_for(250) > P.alpha_for(500)
    assert P.display_alpha(250) == P.alpha_for(250)
    row = MarketRow(8424, "Hourly WTI/BTC Prediction Markets - 2026-10-27 16:00", 0, "live", 0, 0, tuple(EST["shares"]),
                    tuple(EST["names"]), tuple(range(1, 251)))
    assert api.Book.from_row(row).alpha == P.alpha_for(250)


@pytest.mark.parametrize("case", EST["cases"], ids=lambda c: f"{c['lo']}-{c['hi']}x{c['contracts']}")
def test_wtibtc_ticket_matches_server_estimate(case):
    """Server estimates recorded live on the 2026-10-27 16:00 close (topic 8424, 250 ranges, 80-share seed): the heavy
    range at spot, a 3-range box over it, tail ranges and both ends of the ladder, at one to a hundred contracts."""
    t = P.ticket(EST["shares"], P.alpha_for(len(EST["shares"])), case["lo"], case["hi"], case["contracts"])
    assert t.cost_sats - t.fee_sats == pytest.approx(case["cost_sats"], abs=0.002)
    assert t.fee_sats == pytest.approx(max(case["fee_sats"], P.MIN_FEE_SATS), abs=0.002)


def test_wtibtc_basket_passes_the_bots_book_moved_check():
    """What the runner compares before a live buy: its local total against the server's charge, within 0.5% + 5 sats.
    Priced on 500 ranges the local total was 5 to 13 times off and every buy was skipped."""
    sh, a = EST["shares"], P.alpha_for(len(EST["shares"]))
    for case in EST["cases"]:
        t = P.ticket(sh, a, case["lo"], case["hi"], case["contracts"])
        server = case["cost_sats"] + max(case["fee_sats"], P.MIN_FEE_SATS)
        assert abs(server - t.cost_sats) <= 0.005 * t.cost_sats + 5
    wrong = P.ticket(sh, 2 / (500 * math.log(500)), 117, 117, 100)
    assert abs((EST["cases"][5]["cost_sats"] + EST["cases"][5]["fee_sats"]) - wrong.cost_sats) > 0.005 * wrong.cost_sats + 5
