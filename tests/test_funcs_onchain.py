"""FLDS, ONCH, URPD, WAVE, CYC and CORR on the recorded sources, plus the arithmetic under URPD and CORR. No network."""
import math
import random
import time
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from offline import fixture
from rich.cells import cell_len

from glimpse_tui.data import core, quotes
from glimpse_tui.data.core import Provenance, SourceError
from glimpse_tui.funcs import onchain as oc
from glimpse_tui.term import registry
from glimpse_tui.term.hub import Hub, Quote

BV = "bitview/"
SIZES = ((44, 9), (60, 14), (74, 11), (100, 40), (196, 50))


def text(lines) -> str:
    return "\n".join(ln.plain for ln in lines)


def make_hub() -> Hub:
    """A hub that does not wait between recorded requests and does not retry a request that has no fixture."""
    hub = Hub()
    for s in hub.sources.values():
        s.retries, s.bucket = 0, core.TokenBucket(1000, 50)
    return hub


def series_asked(seen: list[str]) -> set[str]:
    """Every Bitview series identifier in the requests a test made: bulk lists and single series paths."""
    out: set[str] = set()
    for url in seen:
        u = urlsplit(url)
        if u.netloc != "bitview.space" or not u.path.startswith("/api/series/"):
            continue
        if u.path == "/api/series/bulk":
            out |= set(dict(parse_qsl(u.query))["series"].split(","))
        elif u.path.count("/") >= 4:                                         # /api/series/<name>/<index>
            out.add(unquote(u.path.split("/")[3]))
    return out


def fits(pane, sizes=SIZES) -> None:
    for w, h in sizes:
        lines = pane.draw(w, h)
        assert lines, (pane.code, w, h)
        wide = [ln.plain for ln in lines if cell_len(ln.plain) > w]
        assert not wide, (pane.code, w, h, wide[:2])


# ── the rule of the family ──────────────────────────────────

async def test_every_identifier_the_dashboards_request_is_in_onchain_toml(no_network):
    hub = make_hub()
    for cls in (oc.OnchPane, oc.WavePane, oc.CycPane):
        pane = cls(hub, None, ())
        await pane.reload()
        assert pane.error == "" and pane.bad == {}, (cls.code, pane.error)
    asked = series_asked(no_network.seen)
    assert len(asked) == 12 + 23 + 1                                         # price and 11 metrics, 23 age bands, and CYC's 200-day average
    assert asked <= oc.known_series(), asked - oc.known_series()
    assert no_network.missing == []                                         # and every request was a recorded one, matched in full


def test_the_six_functions_are_registered_with_help_and_no_em_dashes():
    for code in ("FLDS", "ONCH", "URPD", "WAVE", "CYC", "CORR"):
        fn = registry.get(code)
        assert fn is not None and fn.category == "On-chain" and fn.make is not None
        assert len(fn.help) > 400 and "—" not in fn.help and "—" not in fn.summary
    assert "Bitview is down" in registry.get("ONCH").help and "onchain.toml" in registry.get("WAVE").help


# ── ONCH ────────────────────────────────────────────────────

def test_regime_reads_use_the_textbook_thresholds():
    assert oc.regime("mvrv", 3.6)[0] == "hot" and oc.regime("mvrv", 0.9)[0] == "under cost" and oc.regime("mvrv", 1.5)[0] == "fair"
    assert [oc.regime("nupl", v)[0] for v in (-0.1, 0.1, 0.3, 0.6, 0.8)] == ["capitulation", "hope", "optimism", "belief", "euphoria"]
    assert oc.regime("sopr", 1.02)[0] == "in profit" and oc.regime("sopr", 0.98)[0] == "at a loss" and oc.regime("sopr", 1.0)[0] == "even"
    assert oc.regime("mayer_multiple", 2.5)[0] == "hot" and oc.regime("mayer_multiple", 0.7)[0] == "cheap"
    assert oc.regime("puell_multiple", 0.4)[0] == "cheap" and oc.regime("puell_multiple", 4.2)[0] == "hot"
    assert oc.regime("realized_price", 50_000, 80_000)[0] == "spot above" and oc.regime("sth_cost_basis", 90_000, 80_000)[0] == "spot below"
    assert oc.regime("liveliness", 0.6) is None and oc.regime("mvrv", None) is None


async def test_onch_shows_the_settled_day_with_changes_reads_the_oracle_and_the_urpd(no_network):
    hub = make_hub()
    pane = oc.OnchPane(hub, None, ())
    await pane.reload()
    assert pane.error == ""
    bulk = fixture(BV + "series_bulk_onch_day1_start-61.json")
    names = [oc.series_of(k) for k in pane.KEYS]
    mvrv, realized = bulk[names.index("mvrv")]["data"], bulk[names.index("realized_price")]["data"]
    assert (round(mvrv[-2], 6), round(realized[-2], 2)) == (1.523742, 53217.42)     # the last complete day, not today's forming point
    out = text(pane.draw(100, 40))
    assert "MVRV" in out and "1.52" in out and "53,217" in out and "71,505" in out and "fair" in out and "optimism" in out
    assert f"{(mvrv[-2] / mvrv[-32] - 1) * 100:+.1f}%" in out                   # the 30-day change, from the recorded points
    assert "18 Sep 2026" in out and "daily" in out and pane.title == "on-chain · 18 Sep 2026"
    assert f"{fixture(BV + 'oracle_price.json'):,.0f}" in out and "read from the chain alone" in out
    assert "▁" in out or "▂" in out                                          # sparklines inside the table
    urpd = fixture(BV + "urpd_all_2026-09-18_lin1000.json")
    assert f"◂ {urpd['close']:,.0f} close" in out and "URPD 18 Sep" in out    # no live quote in this hub: the day's own close is marked
    five = sum(b["supply"] for b in urpd["buckets"] if 80_000 <= b["price_floor"] < 85_000) / urpd["total_supply"]
    assert f" 80k {five * 100:4.1f}% ▏█" in out
    assert [p.delay for p in pane.provs] == ["daily"] * 3 and {p.source for p in pane.provs} == {"bitview.space"}
    assert pane.provs[0].as_of == pane.day                                   # the frame's as-of is the complete day
    assert pane.enter() == "GP realized_price"
    pane.on_key_("j", "j")
    assert pane.enter() == "GP mvrv"
    header, rows = pane.export()
    assert header[:4] == ["metric", "series", "day", "value"] and rows[1][1:4] == ["mvrv", "2026-09-18", mvrv[-2]]
    hub.quotes["BTC"] = Quote("BTC", 81_608.0, Provenance("coinbase", time.time(), time.time(), "live"))
    assert "◂ 81,608 spot" in text(pane.draw(100, 40))                       # with a live composite the marker is spot, as in the mockup


async def test_onch_fits_the_launchpad_pane_and_uses_the_room_when_zoomed(no_network):
    pane = oc.OnchPane(make_hub(), None, ())
    await pane.reload()
    small = pane.draw(74, 11)
    assert len(small) <= 11 and all(cell_len(ln.plain) <= 74 for ln in small)
    out = text(small)
    assert all(x in out for x in ("realized", "MVRV", "NUPL", "SOPR", "STH cost", "LTH cost", "Puell", "Mayer", "chain oracle", "URPD 18 Sep"))
    assert sum(1 for ln in small if "▏" in ln.plain) == 6 and "\n85k" in out and "\n60k" in out and "◂" in out    # six buckets around spot
    real = pane.draw(75, 9)                                                  # what the BTC launchpad really gives it at 132x36
    assert len(real) == 9 and sum(1 for ln in real if "▏" in ln.plain) == 5 and "from the chain alone" in text(real)
    big = text(pane.draw(196, 50))
    assert "CYCLE METRICS" in big and "sth_realized_price" in big and "supply in profit, BTC" in big and "spot above" in big
    assert sum(1 for ln in pane.draw(196, 50) if "▏" in ln.plain) > 40           # the URPD takes the height beside the table
    fits(pane)


async def test_onch_names_a_series_the_server_refuses_and_never_swaps_it(no_network, monkeypatch):
    hub = make_hub()
    bv = hub.sources["bitview"]
    single = bv.series

    async def refuse_bulk(names, index="day1", start=-400):
        raise SourceError("bitview.space: HTTP 404")

    async def series(name, index="day1", start=-400, end=None):
        if name == "liveliness":
            raise SourceError("bitview.space: HTTP 404")
        return await single(name, index, start, end)

    monkeypatch.setattr(bv, "bulk", refuse_bulk)
    monkeypatch.setattr(bv, "series", series)
    pane = oc.OnchPane(hub, None, ())
    await pane.reload()
    assert pane.error == "" and list(pane.bad) == ["liveliness"]
    out = text(pane.draw(120, 40))
    assert "liveliness: not served (bitview.space: HTTP 404)" in out and "never swapped" in out and "1.52" in out
    assert series_asked(no_network.seen) <= oc.known_series()


async def test_onch_says_so_when_bitview_is_down(no_network):
    no_network.fail.add("bitview.space")
    pane = oc.OnchPane(make_hub(), None, ())
    await pane.reload()
    assert "bitview.space" in pane.error and pane.draw(74, 11) == []         # nothing to show: the frame carries the error


# ── URPD ────────────────────────────────────────────────────

def test_urpd_bucket_arithmetic():
    pairs = ((0, 5), (1000, 1), (4000, 2), (5000, 3), (9000, 4))
    data = {"aggregation": "lin1000", "buckets": [{"price_floor": f, "supply": s} for f, s in pairs]}
    bins = oc.urpd_bins(data)
    assert [(b.floor, b.ceil) for b in bins][:2] == [(0.0, 1000.0), (1000.0, 2000.0)]
    merged = oc.merge_lin(bins, 5000.0)
    assert [(b.floor, b.ceil, b.supply) for b in merged] == [(0.0, 5000.0, 8.0), (5000.0, 10000.0, 7.0)]
    below, above = oc.profit_split(bins, 4500.0, log=False)                 # spot halves the 4,000 bucket
    assert (below, above) == (5 + 1 + 1.0, 1.0 + 3 + 4)
    logd = {"aggregation": "log10", "buckets": [{"price_floor": f, "supply": 1.0} for f in (0.0, 1.0, 1.26, 10.0, 12.59, 100.0)]}
    groups = oc.merge_log(oc.urpd_bins(logd), 10, 10)                       # ten buckets a decade, ten at a time: whole decades
    assert [(round(b.floor), round(b.ceil), b.supply) for b in groups] == [(1, 10, 2.0), (10, 100, 2.0), (100, 1000, 1.0)]
    lo, hi = oc.profit_split(oc.urpd_bins(logd)[3:4], 10 ** 1.05, log=True)  # half way through a log bucket, along log price
    assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)
    window = oc.around_spot(oc.merge_lin([oc.Bin(float(f), f + 5000.0, 1.0) for f in range(5000, 130000, 5000)], 5000.0), 81_000, 6)
    assert [b.floor for b in window] == [60000, 65000, 70000, 75000, 80000, 85000]
    assert oc.agg_step("log50") == ("log", 50.0) and oc.agg_step("raw") == ("", 0.0)
    assert oc.kprice(80_000) == "80k" and oc.kprice(79_432.82) == "79.4k" and oc.kprice(2.51) == "2.51"


async def test_urpd_draws_bars_with_spot_the_split_the_clusters_and_the_early_coins_apart(no_network):
    pane = oc.UrpdPane(make_hub(), None, ())
    await pane.reload()
    assert pane.error == "" and no_network.seen[-1].endswith("/api/urpd/all?agg=log10")      # always aggregated, never the 900 KB raw reply
    d = fixture(BV + "urpd_all_latest_log10.json")
    out = text(pane.draw(100, 26))
    assert "URPD all" in out and "daily" in out and "19 Sep 2026" in out and "10 a decade" in out
    assert f"◂ {d['close']:,.0f} close" in out
    share = {round(b["price_floor"]): b["supply"] / d["total_supply"] for b in d["buckets"]}
    assert f"63.1k {share[63096] * 100:4.1f}% ▏█" in out and f"79.4k {share[79433] * 100:4.1f}% ▏█" in out
    assert f"under $1, the earliest coins: {share[0]:.1%}" in out            # the floor-0 bucket is reported, not drawn as a bar
    assert not any(ln.plain.lstrip().startswith("0 ") for ln in pane.draw(100, 26))
    assert "largest clusters  63.1k to 79.4k 19.8%" in out
    below, above = oc.profit_split(oc.urpd_bins(d), d["close"], log=True)
    assert f"in profit ~{below / d['total_supply']:.1%}" in out and f"in loss ~{above / d['total_supply']:.1%}" in out
    assert 0.65 < below / d["total_supply"] < 0.80                           # Bitview's own supply in profit read 72% that day
    assert pane.export()[1][0][:4] == ["all", "2026-09-19", "log10", 0.0]
    fits(pane)


async def test_urpd_keys_cycle_cohorts_step_dates_and_switch_buckets(no_network):
    hub = make_hub()
    pane = oc.UrpdPane(hub, None, ())
    await pane.reload()
    pane.draw(100, 20)
    assert pane.on_key_("l", "l") and pane.loaded_at == 0.0                  # a key asks for a reload; draw never fetches
    await pane.reload()
    assert no_network.seen[-1].endswith("/api/urpd/all?agg=lin1000") and pane.data["aggregation"] == "lin1000"
    out = text(pane.draw(100, 20))
    assert "$10,000 buckets" in out and "under $10k" in out and "◂" in out and pane.command() == "URPD all lin"
    pane.on_key_("l", "l")
    pane.on_key_("[", "[")
    await pane.reload()
    assert "/api/urpd/all/2026-09-18?" in no_network.seen[-1] and pane.date == "2026-09-18" and "18 Sep 2026" in pane.title
    assert "◂ 81,090 close" in text(pane.draw(100, 20))                      # a past day is marked with its own close
    pane.on_key_("c", "c")
    await pane.reload()
    assert pane.cohort == "sth" and pane.date == "" and pane.data["cohort"] == "sth" and "URPD sth" in text(pane.draw(100, 20))
    assert set(pane.CYCLE) <= set(fixture(BV + "urpd_cohorts.json"))         # every cohort the key offers is one Bitview lists
    assert no_network.missing == []


async def test_urpd_picks_the_aggregation_from_the_pane_height_and_reports_an_unknown_cohort(no_network):
    hub = make_hub()
    pane = oc.UrpdPane(hub, None, ())
    await pane.reload()
    assert pane.data["aggregation"] == "log10"
    pane.draw(120, 50)                                                       # a tall pane wants finer buckets: it asks, the next tick loads
    assert pane.loaded_at == 0.0 and pane.agg() == "log50"
    await pane.reload()
    assert pane.data["aggregation"] == "log50" and no_network.seen[-1].endswith("agg=log50")
    tall = pane.draw(120, 50)
    assert sum(1 for ln in tall if "▏" in ln.plain) == 45 and pane.loaded_at > 0       # it asked once and is satisfied
    odd = oc.UrpdPane(hub, None, ("whales", "2026-09-18"))
    await odd.reload()
    assert odd.cohort == "all" and "no URPD cohort called whales" in text(odd.draw(100, 20))


# ── WAVE ────────────────────────────────────────────────────

async def test_wave_stacks_the_23_bands_and_lists_each_share_on_the_settled_day(no_network):
    pane = oc.WavePane(make_hub(), None, ())
    await pane.reload()
    assert pane.error == "" and pane.title == "HODL waves · 400 d · to 18 Sep 2026"
    bulk = fixture(BV + "series_bulk_waves_day1_start-401.json")
    bands = oc.wave_bands()
    assert len(bands) == 23 and len(pane.matrix) == 400 and len(pane.matrix[0]) == 23          # today's forming point is left out
    assert sum(pane.matrix[-1]) == pytest.approx(100.0, abs=0.01)
    lines = pane.draw(100, 40)
    out = text(lines)
    assert "HODL WAVES" in out and "daily" in out and "18 Sep 2026" in out and "23 age bands" in out and "summed" not in out
    assert f"> 15y      {bulk[22]['data'][-2]:.2f}%" in out and f"1h - 1d     {bulk[1]['data'][-2]:.2f}%" in out
    assert sum(1 for ln in lines if "▀" in ln.plain) == 38 and "100%" in out and "0%" in out
    warm, cool = oc.wave_colours(23)[0], oc.wave_colours(23)[-1]
    assert int(warm[1:3], 16) > int(warm[5:7], 16) and int(cool[5:7], 16) > int(cool[3:5], 16)  # young coins warm, old coins cool
    small = text(pane.draw(74, 11))
    assert "23 age bands summed into 8" in small
    labels, _, days = pane.bands(grouped=True)
    assert labels[0] == "< 1d" and labels[-1] == "> 10y" and sum(days[-1]) == pytest.approx(sum(pane.matrix[-1]))
    assert days[-1][7] == pytest.approx(sum(bulk[i]["data"][-2] for i in (20, 21, 22))) and f"> 10y {days[-1][7]:.1f}%" in small
    assert pane.export()[0][0] == "date" and pane.export()[1][-1][0] == "2026-09-18"
    fits(pane)


async def test_wave_window_keys_refetch_and_the_picture_is_cached(no_network):
    pane = oc.WavePane(make_hub(), None, ())
    await pane.reload()
    first = pane.draw(150, 45)
    t0 = time.perf_counter()
    again = pane.draw(150, 45)
    assert (time.perf_counter() - t0) < 0.010 and again[1].plain == first[1].plain
    assert pane.on_key_("[", "[") and pane.loaded_at == 0.0
    await pane.reload()
    assert pane.error == "" and "start=-61" in no_network.seen[-1] and len(pane.matrix) == 60 and "60 d" in pane.title
    assert "60 d" in text(pane.draw(100, 40)) and pane.command() == "WAVE 60d" and no_network.missing == []


async def test_wave_reads_all_of_history_on_the_weekly_index_dated_by_bitviews_date_series(no_network, monkeypatch):
    assert oc.WavePane.WINDOWS[-1] == ("all", "week1", 0)                    # 23 bands of daily history is refused: weight_exceeded
    assert 23 * 1462 * 8 < 320_000 < 23 * 6471 * 8                           # four years of days fit Bitview's limit, all of history does not
    monkeypatch.setattr(oc.WavePane, "WINDOWS", (("60 w", "week1", -60),))   # the same path on a recorded, smaller reply
    pane = oc.WavePane(make_hub(), None, ("60w",))
    await pane.reload()
    raw = fixture(BV + "series_bulk_waves_week1_start-60.json")
    assert pane.error == "" and no_network.missing == [] and raw[0]["type"] == "Date" and raw[0]["data"][-2:] == ["2026-09-07", "2026-09-14"]
    assert len(pane.matrix) == 59 and oc.iso_day(pane.times[-1]) == "2026-09-07"                 # the current week is left out
    assert pane.matrix[-1][22] == raw[23]["data"][-2] and sum(pane.matrix[-1]) == pytest.approx(100.0, abs=0.01)
    out = text(pane.draw(110, 36))
    assert "weekly" in out and "the week of 07 Sep 2026" in out and pane.title.endswith("to the week of 07 Sep 2026")
    assert series_asked(no_network.seen) <= oc.known_series() and "date" in series_asked(no_network.seen)


# ── CYC ─────────────────────────────────────────────────────

async def test_cyc_draws_the_bands_with_each_line_and_spots_distance_from_it(no_network):
    hub = make_hub()
    pane = oc.CycPane(hub, None, ())
    await pane.reload()
    assert pane.error == "" and pane.title == "valuation bands · 400 d · to 18 Sep 2026"
    bulk = fixture(BV + "series_bulk_cyc_day1_start-401.json")
    close, realized, sth, lth, sma = (b["data"][-2] for b in bulk)
    out = text(pane.draw(120, 40))
    assert "VALUATION BANDS" in out and "daily" in out and "log scale" in out and "⣀" in out and "┤" in out
    for v in (close, realized, sth, lth, sma):
        assert f"{v:,.0f}" in out
    assert f"{(close / realized - 1) * 100:+.1f}%" in out and f"{(close / sma - 1) * 100:+.1f}%" in out and "close vs line" in out
    assert "coins younger than 150 days" in out and "price_sma_200d" in out
    assert pane.on_key_("L", "L") and "linear scale" in text(pane.draw(120, 40))
    assert pane.enter() == "GP price_close"
    pane.on_key_("j", "j")
    assert pane.enter() == "GP realized_price"
    hub.quotes["BTC"] = Quote("BTC", 82_000.0, Provenance("coinbase", time.time(), time.time(), "live"))
    live = text(pane.draw(120, 40))
    assert "spot vs line" in live and f"{(82_000 / realized - 1) * 100:+.1f}%" in live and "◂spot" in live
    assert len(pane.export()[1]) == 400 and pane.export()[0] == ["date", "price_close", "realized_price", "sth_realized_price",
                                                                 "lth_realized_price", "price_sma_200d"]
    fits(pane)


async def test_cyc_redraws_inside_the_budget_and_keeps_the_picture_when_a_window_fails(no_network):
    pane = oc.CycPane(make_hub(), None, ())
    await pane.reload()
    pane.draw(196, 50)
    best = 1.0
    for _ in range(5):
        t0 = time.perf_counter()
        lines = pane.draw(196, 50)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.010 and len(lines) == 50
    pane.on_key_("]", "]")                                                   # the four-year window has no recorded reply
    await pane.reload()
    assert pane.error and "400 d" in text(pane.draw(120, 40))                # the frame says why; the last good picture stays


# ── CORR ────────────────────────────────────────────────────

def test_correlation_maths_against_a_hand_computed_example():
    assert oc.log_returns([100.0, 110.0, 99.0]) == [pytest.approx(math.log(1.1)), pytest.approx(math.log(0.9))]
    assert oc.log_returns([1.0, 0.0, 2.0]) == [None, None]
    assert oc.differences([4.10, 4.20, 4.15]) == [pytest.approx(0.10), pytest.approx(-0.05)]
    # x = 1,2,3 and y = 2,4,7: sxy = 5, sxx = 2, syy = 114/9, so r = 5 / sqrt(2 * 114 / 9)
    assert oc.pearson([1, 2, 3], [2, 4, 7]) == pytest.approx(5 / math.sqrt(2 * 114 / 9)) == pytest.approx(0.99339927)
    assert oc.pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0) and oc.pearson([1, 1, 1], [1, 2, 3]) is None
    assert oc.pearson([1, 2], [1, 2]) is None
    rng = random.Random(7)
    x = [rng.gauss(0, 1) for _ in range(60)]
    y = [a * 0.5 + rng.gauss(0, 1) for a in x]
    roll = oc.rolling_corr(x, y, 20)
    assert roll[:19] == [None] * 19 and len(roll) == 60
    for i in (19, 33, 59):
        assert roll[i] == pytest.approx(oc.pearson(x[i - 19:i + 1], y[i - 19:i + 1]), abs=1e-9)


def test_series_are_joined_on_the_other_assets_utc_dates():
    day = 86400.0
    fri = 1789689600.0                                                       # Fri 18 Sep 2026 00:00 UTC
    btc_t = [fri - day, fri, fri + day, fri + 2 * day, fri + 3 * day]        # Thu to Mon, every day
    btc_v = [100.0, 110.0, 120.0, 90.0, 99.0]
    spx_t = [fri - day + 3600, fri + 3600, fri + 3 * day + 3600]             # Thu, Fri, Mon: stamped an hour into the day
    spx_v = [50.0, 55.0, 49.5]
    t, a, b = oc.align(btc_t, btc_v, spx_t, spx_v)
    assert a == [100.0, 110.0, 99.0] and b == spx_v and len(t) == 3          # the weekend is skipped on both sides
    rt, rx, ry = oc.paired_returns(btc_t, btc_v, spx_t, spx_v)
    assert rx == [pytest.approx(math.log(1.1)), pytest.approx(math.log(0.9))]    # Friday to Monday is one three-day return
    assert ry == [pytest.approx(math.log(1.1)), pytest.approx(math.log(0.9))] and rt[-1] == fri + 3 * day
    _, _, dy = oc.paired_returns(btc_t, btc_v, spx_t, [4.10, 4.20, 4.15], diff=True)
    assert dy == [pytest.approx(0.10), pytest.approx(-0.05)]                 # a yield moves in differences
    assert oc.diverging(-0.5, 8).plain == "    ████│        " and oc.diverging(0.5, 8).plain == "        │████    "
    assert oc.diverging(None, 4).plain == "    │    "


async def test_corr_labels_every_source_and_matches_an_independent_computation(no_network):
    hub = make_hub()
    pane = oc.CorrPane(hub, None, ())
    await pane.reload()
    assert pane.error == "" and [r.ticker for r in pane.rows] == ["SPX", "NDX", "XAU", "DXY", "US10Y"]
    spx = pane.rows[0]
    bt, bv, bprov = await oc.btc_history(hub, 400)
    assert bprov.source == "kraken" and len(bt) == 400                       # the longer daily history
    if bt[-1] + 86400 > time.time():
        bt, bv = bt[:-1], bv[:-1]
    st, sv, _ = await quotes.history(hub, hub.book.get("SPX"), 400)
    _, rx, ry = oc.paired_returns(bt, bv, st, sv)
    assert spx.now[90] == pytest.approx(oc.pearson(rx[-90:], ry[-90:]), abs=1e-9) and -1 <= spx.now[90] <= 1
    assert spx.now[30] == pytest.approx(oc.pearson(rx[-30:], ry[-30:]), abs=1e-9)
    assert all(-1 <= v <= 1 for r in pane.rows for v in (r.now or {}).values() if v is not None)
    out = text(pane.draw(160, 40))
    assert "CORRELATION WITH BTC" in out and "daily" in out and "90-day window" in out and f"{spx.now[90]:+.2f}".replace("-", "−") in out
    assert "fred:SP500 · daily · FRED carries 10 years" in out and "fred:NASDAQ100" in out and "treasury.gov" in out
    assert "proxy PAXG" in out and "computed · six FX legs, ICE weights" in out        # gold's stand-in and the computed index say what they are
    assert "ROLLING 90 d" in out and "+1.0" in out and "-1.0" in out and "│" in out
    assert {p.delay for p in pane.provs} == {"daily"} and any(p.source == "fred:SP500" for p in pane.provs)
    assert pane.enter() == "GP BTC SPX"
    pane.on_key_("]", "]")
    wide = text(pane.draw(160, 40))
    assert "180-day window" in wide and "ROLLING 180 d" in wide and "fewer than 180 shared days" in wide and pane.command() == "CORR 180"
    narrow = text(pane.draw(44, 9))
    assert "SPX" in narrow and "US10Y" in narrow
    header, rows = pane.export()
    assert header == ["asset", "date", "corr_30d", "corr_90d", "corr_180d"] and rows[-1][0] == "US10Y"
    fits(pane)


# ── FLDS ────────────────────────────────────────────────────

async def test_flds_searches_describes_a_handful_and_feeds_the_go_bar(no_network):
    hub = make_hub()
    hub.series_seen[:] = [(f"old_{i}", "") for i in range(600)]
    pane = oc.FldsPane(hub, None, ("MVRV",))
    await pane.reload()
    assert pane.error == ""
    found = fixture(BV + "series_search_mvrv.json")
    assert pane.names == found and no_network.missing == []
    infos = [u for u in no_network.seen if "/api/series/" in u and "search" not in u and "bulk" not in u]
    assert len(infos) == 8                                                   # eight descriptions a load, not forty
    seen = dict(hub.series_seen)
    assert len(hub.series_seen) == 500 and set(found) <= set(seen)           # deduped and capped
    assert seen["mvrv"] == fixture(BV + "series_info_mvrv.json")["description"] and hub.series_seen[-1][0] == "p2pkh_mvrv"
    out = text(pane.draw(140, 30))
    latest = fixture(BV + "series_bulk_flds_mvrv_day1_start-2.json")
    assert "FLDS mvrv" in out and "40 series" in out and "StoredF32" in out and "16: height day1" in out
    row = next(ln.plain for ln in pane.draw(140, 30) if ln.plain.startswith("lth_mvrv"))
    assert "Uses long-term-holder UTXOs" in row and f" {latest[1]['data'][-2]:.2f} " in row and latest[1]["data"][-2] == 1.643634
    assert "mvrv: Market-value-to-realized-value (MVRV) ratio" in out        # the cursor row's description in full
    assert pane.enter() == "GP mvrv"
    for _ in range(12):
        pane.on_key_("j", "j")
    pane.draw(140, 30)
    assert pane.enter() == "GP rookie_mvrv" and pane.loaded_at == 0.0        # the cursor passed the described rows: one reload is asked for
    before = len(no_network.seen)
    await pane.reload()
    assert len(no_network.seen) - before <= 8 and "rookie_mvrv" in pane.info  # unrecorded names come back as errors, shown in red, never guessed
    pane.draw(140, 30)
    assert pane.loaded_at > 0                                                # and the page does not ask again for the same rows
    assert pane.export()[1][0][0] == "mvrv"
    fits(pane)


async def test_flds_with_no_words_shows_the_catalogue_size_and_the_curated_names(no_network):
    hub = make_hub()
    pane = oc.FldsPane(hub, None, ())
    await pane.reload()
    out = text(pane.draw(110, 36))
    assert pane.error == "" and f"{fixture(BV + 'series_count.json')['distinct']:,} series" in out
    assert "price_sma_200d_ratio" in out and "Mayer multiple" in out and "FLDS realized price" in out
    assert ("sopr_24h", "SOPR (24 hours)") in hub.series_seen and pane.enter() == "GP price_close"
    fits(pane)
    none = oc.FldsPane(hub, None, ("mayer",))
    await none.reload()
    assert none.error == "" and none.names == [] and "No series matches 'mayer'" in text(none.draw(100, 20))


# ── every page ──────────────────────────────────────────────

async def test_every_glyph_is_one_cell_wide_and_no_text_carries_an_em_dash(no_network):
    hub = make_hub()
    for cls, args in ((oc.OnchPane, ()), (oc.UrpdPane, ()), (oc.WavePane, ()), (oc.CycPane, ()), (oc.CorrPane, ()), (oc.FldsPane, ("mvrv",))):
        pane = cls(hub, None, args)
        await pane.reload()
        assert pane.title and pane.provs and pane.hint() and pane.menu() and pane.export() is not None, cls.code
        for w, h in ((74, 11), (196, 50)):
            body = text(pane.draw(w, h))
            assert "—" not in body and all(cell_len(ch) == 1 for ch in set(body) - {"\n"}), (cls.code, w, h)
