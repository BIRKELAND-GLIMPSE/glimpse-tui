"""The data core, the GO bar's grammar, the split tree, config and charts. No screen, no network."""
import asyncio
import time

import pytest
from offline import fixture
from rich.cells import cell_len

from glimpse_tui import charts
from glimpse_tui.data import core, fred, fx_ref, prices, quotes, treasury
from glimpse_tui.data.mempool import ProjectedTx, apply_delta
from glimpse_tui.term import config, gobar, instruments, panes, registry, tape
from glimpse_tui.term.hub import Hub


@pytest.fixture
def book():
    return instruments.load()


# ── the data contract ───────────────────────────────────────

async def test_source_caches_and_serves_the_last_good_value_when_the_host_goes_down(no_network):
    hub = Hub()
    mp = hub.sources["mempool"]
    fees, prov = await mp.fees()
    assert fees == fixture("mempool/fees_recommended.json") and prov.source == "mempool.space" and prov.delay == "live"
    n = len(no_network.seen)
    await mp.fees()
    assert len(no_network.seen) == n and mp.health.cached == 1              # inside its ttl: no second request
    mp._mem = {k: (at, 0.0, v) for k, (at, _, v) in mp._mem.items()}        # expire it
    no_network.fail.add("mempool.space")
    mp.retries = 0
    again, prov2 = await mp.fees()
    assert again == fees and prov2.fetched_at == prov.fetched_at            # the old value, with its old fetch time
    assert mp.health.state == "down" and "503" in mp.health.last_error
    with pytest.raises(core.SourceError):
        await mp.pools("1w")                                                # nothing cached to fall back on


async def test_a_429_parks_the_host_for_its_retry_after(no_network):
    hub = Hub()
    no_network.limited.add("mempool.space")
    with pytest.raises(core.SourceError, match="rate limited"):
        await hub.sources["mempool"].fees()
    assert hub.sources["mempool"].health.state == "limited"
    before = len(no_network.seen)
    with pytest.raises(core.SourceError, match="resting"):
        await hub.sources["mempool"].mempool()
    assert len(no_network.seen) == before                                   # parked: it did not even ask


async def test_token_bucket_spaces_requests():
    b = core.TokenBucket(rate=50, burst=2)
    t0 = time.monotonic()
    for _ in range(6):
        await b.acquire()
    assert time.monotonic() - t0 >= 4 / 50 * 0.9                            # two at once, then four at 50 a second


def test_provenance_staleness_depends_on_the_kind_of_value():
    now = 1_000_000.0
    assert core.Provenance("x", now - 120, now, "live").stale(now)
    assert not core.Provenance("fred:DGS10", now - 86400, now, "daily").stale(now)
    assert core.ago(45) == "45s" and core.ago(7 * 60) == "7m" and core.ago(3 * 86400) == "3d"


async def test_switching_a_backend_needs_no_restart(no_network):
    hub = Hub()
    hub.cfg["bitcoin"]["mempool"] = "http://umbrel.local:3006"
    hub.apply_config()
    mp = hub.sources["mempool"]
    assert mp.base_url == "http://umbrel.local:3006" and mp.ws_url == "ws://umbrel.local:3006/api/v1/ws"
    assert not hub.is_public("mempool") and hub.is_public("bitview")
    mp.retries = 0
    with pytest.raises(core.SourceError):
        await mp.fees()
    assert no_network.seen[-1].startswith("http://umbrel.local:3006/api/v1/fees/recommended")


# ── quotes ──────────────────────────────────────────────────

async def test_the_quote_board_fills_from_recorded_sources_with_honest_labels(no_network):
    hub = Hub()
    await quotes.refresh(hub, hub.tape_tickers())
    btc = hub.quotes["BTC"]
    assert btc.prov.source == "bitstamp+coinbase+kraken" and btc.prov.delay == "live"
    assert sorted(p for _, p in btc.parts)[1] == btc.price                  # the median of three venues
    assert hub.quotes["SPX"].prov.source == "fred:SP500" and hub.quotes["SPX"].prov.delay == "daily"
    assert "S&P" in hub.quotes["SPX"].prov.note                             # the owner is named where FRED asks for it
    assert hub.quotes["US10Y"].prov.source == "treasury.gov"
    xau = hub.quotes["XAU"]
    assert xau.proxy_for == "XAU" and xau.via == "PAXG"                     # gold has no open feed: the proxy says it is one
    line = tape.tape(hub, 200).plain
    assert "GOLD" in line and "via PAXG" in line and "S&P" in line and " d" in line and "BTC 81,423 " in line


def test_the_dollar_index_matches_the_ice_formula_on_the_recorded_ecb_fix():
    fix = fx_ref.parse(fixture("ecb/eurofxref-daily.xml"))[-1]
    legs = {p: fix.cross(p) for p in fx_ref.DXY_WEIGHTS}
    assert legs["EURUSD"] == 1.1460 and round(legs["USDJPY"], 2) == round(180.94 / 1.1460, 2)
    assert round(fx_ref.dxy(legs), 1) == 100.5                              # Phase 0 computed 100.52 from the same fix
    assert fx_ref.dxy({**legs, "USDSEK": 0}) is None


def test_net_liquidity_converts_millions_and_billions_before_subtracting():
    walcl, tga, rrp = (fred.parse_csv(fixture(f"fred/{s}.csv"))[1][-1] for s in ("WALCL", "WTREGEN", "RRPONTSYD"))
    net = fred.net_liquidity(walcl, tga, rrp)
    assert net == walcl * 1e6 - tga * 1e6 - rrp * 1e9
    assert 3e12 < net < 9e12                                                # trillions of dollars, not a unit slip
    assert fred.dollars("RRPONTSYD", 2.5) == 2.5e9 and fred.dollars("WALCL", 6_600_000) == 6.6e12


def test_fred_csv_skips_missing_observations():
    dates, values = fred.parse_csv("observation_date,DGS10\n2026-09-15,5.00\n2026-09-16,\n2026-09-17,.\n2026-09-18,4.94\n")
    assert values == [5.00, 4.94] and len(dates) == 2


def test_treasury_curve_parses_every_tenor():
    days = treasury.parse(fixture("treasury/yield_curve_202609.xml"))
    assert len(days) >= 10 and days[0].date < days[-1].date
    assert days[0].rates["BC_10YEAR"] == 4.79 and days[0].rates["BC_2YEAR"] == 4.39
    assert all(f in days[-1].rates for _, f, _ in treasury.TENORS)


def test_kraken_result_keys_map_back_to_the_pairs_asked_for():
    assert quotes.kraken_key("XXBTZUSD") == "XBTUSD" and quotes.kraken_key("ZEURZUSD") == "EURUSD"
    assert quotes.kraken_key("PAXGUSD") == "PAXGUSD" and quotes.kraken_key("SPYxUSD") == "SPYxUSD"
    assert prices.is_tokenised("SPYxUSD") and not prices.is_tokenised("XBTUSD")


def test_projected_block_deltas():
    full = fixture("mempool/ws_projected_block_full.json")["projected-block-transactions"]["blockTransactions"]
    txs = {t.txid: t for t in map(ProjectedTx.of, full)}
    first = full[0][0]
    assert txs[first].fee == full[0][1] and txs[first].rate == full[0][4]
    apply_delta(txs, {"added": [["ab" * 32, 500, 120.5, 9000, 4.1, 0, 1]], "removed": [first], "changed": [[full[1][0], 99.5]]})
    assert first not in txs and txs["ab" * 32].vsize == 120.5 and txs[full[1][0]].rate == 99.5


# ── the GO bar ──────────────────────────────────────────────

def test_grammar(book):
    p = lambda s, cur=None: gobar.parse(s, book, cur)                       # noqa: E731
    assert p("SRC").func == "SRC" and p("src").func == "SRC"
    assert p("HELP SRC").args == ("SRC",) and p("H").func == "HELP"
    assert p("LP MACRO").func == "LP" and p("LP SAVE mine").args == ("SAVE", "mine")
    assert p("q").func == "QUIT" and p("") is None and p("what is the fee") is None
    assert p("a" * 64).func == "TX" and p("967653").func == "BLK" and p("967,653").args == ("967653",)
    assert p("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4").func == "ADDR"
    assert p("SET bitcoin.mempool http://x:3006").args == ("bitcoin.mempool", "http://x:3006")


def test_history_and_editing(tmp_path):
    ln = gobar.Line()
    for ch in "MEMX":
        ln.key(ch.lower(), ch)
    ln.key("backspace", None)
    ln.key("p", "P")
    assert ln.text == "MEMP" and ln.caret == 4
    ln.remember("MEMP")
    ln.remember("BTC")
    ln.remember("BTC")                                                      # no consecutive duplicates
    fresh = gobar.Line()
    fresh.load_history()
    assert fresh.history == ["MEMP", "BTC"]                                 # kept between sessions
    fresh.set("dra")
    fresh.key("up", None)
    assert fresh.text == "BTC"
    fresh.key("up", None)
    assert fresh.text == "MEMP"
    fresh.key("down", None)
    fresh.key("down", None)
    assert fresh.text == "dra"                                              # back to what was being typed
    fresh.key("ctrl+u", None)
    assert fresh.text == ""


def test_autocomplete_covers_instruments_functions_and_layouts(book):
    registry.load_all()
    labels = [s.label for s in gobar.complete("S", book, panes.layout_names())]
    assert "SRC" in labels and "SET" in labels and any(book.get(x) for x in labels)
    assert [s.label for s in gobar.complete("LP M", book, panes.layout_names())] == ["MINER", "MACRO"]           # the shipped order
    assert gobar.complete("HELP SR", book, [])[0].text == "HELP SRC"
    assert any(s.kind == "instrument" and s.label == "BTC" for s in gobar.find("bitcoin", book, []))


# ── panes ───────────────────────────────────────────────────

def test_split_tree_rectangles_tile_the_area_exactly():
    root = panes.load_layout("MACRO")
    ls = panes.leaves(root)
    assert [lf.command for lf in ls] == ["RATES", "MACRO", "FX", "GLCO", "WEI", "QM GLOBAL"]
    rs = panes.rects(root, 0, 0, 132, 31)
    assert sum(r.width * r.height for _, r in rs) == 132 * 31               # no gaps, no overlap
    root = panes.split(root, ls[1], panes.Leaf("FEES"), "col")
    assert len(panes.leaves(root)) == 7
    root = panes.close(root, ls[0])
    assert [lf.command for lf in panes.leaves(root)] == ["MACRO", "FEES", "FX", "GLCO", "WEI", "QM GLOBAL"]
    panes.even(root)
    assert panes.from_doc(panes.to_doc(root)) is not None
    assert [lf.command for lf in panes.leaves(panes.from_doc(panes.to_doc(root)))] == ["MACRO", "FEES", "FX", "GLCO", "WEI", "QM GLOBAL"]


def test_the_default_page_is_taller_than_the_screen_and_scrolls_to_the_focused_window():
    root = panes.load_layout("BTC")
    assert root.row_height == 21 and len(root.children) == 8 and panes.from_doc(panes.to_doc(root)).row_height == 21
    ls = [lf.command for lf in panes.leaves(root)]
    # Bitcoin's chart and its odds, then gold's, then the chain: the most significant thing is the first thing.
    assert ls[:6] == ["GP BTC 24H", "DIST BTC", "GP XAU 1D", "DIST XAU", "HASH", "DIFF"]
    assert ls[-3:] == ["FX", "GLCO SATS", "FEES"] and len(ls) == 16 <= panes.MAX_PANES
    assert not [c for c in ls if c.startswith(("CONS", "BOT"))]          # the bots have their own screen (B), not a window here
    assert ["PL BTC", "PL XAUBTC"] == [c for c in ls if c.startswith("PL")]
    assert "GLCO SATS" in ls                                             # commodities in satoshis; `$` on the window is dollars

def test_launchpads_save_and_load_and_every_shipped_one_parses():
    for name in panes.SHIPPED:
        assert panes.load_layout(name) is not None, name
    root = panes.load_layout("CHAIN")
    path = panes.save_layout("mine", root)
    assert path.endswith("layouts/mine.toml") and "MINE" in panes.layout_names()
    assert [lf.command for lf in panes.leaves(panes.load_layout("MINE"))] == [lf.command for lf in panes.leaves(root)]


# ── config ──────────────────────────────────────────────────

def test_config_round_trips_and_coerces_types():
    cfg = config.load()
    assert cfg["bitcoin"]["mempool"] == "https://mempool.space" and cfg["default_launchpad"] == "BTC"
    assert cfg["opens_on"] == "odds"                        # a bare `glimpse-tui` opens on the odds, not the front page
    config.set_value(cfg, "bitcoin.mempool", "http://umbrel.local:3006")
    config.set_value(cfg, "gold_stock_tonnes", "250,000")
    config.set_value(cfg, "tape", "BTC MEMP XAU")
    config.set_value(cfg, "privacy_ack", "true")
    config.save(cfg)
    again = config.load()
    assert again["bitcoin"]["mempool"] == "http://umbrel.local:3006" and again["gold_stock_tonnes"] == 250_000
    assert again["tape"] == ["BTC", "MEMP", "XAU"] and again["privacy_ack"] is True
    with pytest.raises(KeyError):
        config.set_value(cfg, "bitcoin.nonsense", "x")


def test_secrets_never_land_in_the_config_file(monkeypatch):
    monkeypatch.setattr("glimpse_tui.auth._keyring", lambda: None)
    config.save_secret("fred", "key_supersecret")
    assert config.secret("fred") == "key_supersecret"
    f = config.config_dir() / "secrets" / "fred"
    assert f.stat().st_mode & 0o077 == 0                                    # 0600, like the API key
    config.save(config.load())
    assert "key_supersecret" not in config.path().read_text()
    config.forget_secret("fred")
    assert config.secret("fred") is None


# ── sessions ────────────────────────────────────────────────

def test_sessions_by_the_clock():
    from datetime import UTC, datetime
    sat = datetime(2026, 9, 19, 18, 0, tzinfo=UTC)
    mon = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)                          # 11:00 New York
    assert not tape.session_open("NYSE", sat) and tape.session_open("NYSE", mon)
    assert not tape.instrument_open("24x5", sat) and tape.instrument_open("24x5", mon) and tape.instrument_open("24x7", sat)
    assert tape.clock("Asia/Tokyo", sat) == ("TYO", "03:00")


# ── charts ──────────────────────────────────────────────────

def test_every_glyph_is_one_cell_wide():
    xs = list(range(60))
    p = charts.Plot(80, 14).line(xs, [50 + (x % 17) * 3 for x in xs]).band(40, 59, 60, 90).hline(70, label="70").vline(40, label="NOW")
    p.candle([charts.Candle(x, 50, 58, 44, 55 if x % 2 else 47) for x in range(0, 40, 2)])
    text = p.render().plain + charts.spark([1, 5, 2, 8]) + charts.hbar(0.37, 20) + "".join(r.plain for r in charts.vbars([1, 2, 3], 3))
    text += "".join(r.plain for r in charts.pixels([[(1, 2, 3)] * 4] * 4)) + "░▒▲▼◂✦✧₿│┤┄─"
    wide = [ch for ch in set(text) if ch != "\n" and cell_len(ch) != 1]
    assert not wide, f"double-width glyphs: {wide}"
    assert all(len(ln) == 80 for ln in p.render().plain.split("\n")[:-1])   # every row is exactly the width asked for


def test_256_colour_snap_lands_on_exact_palette_entries():
    charts.truecolor = False
    try:
        for c in ("#FF7D08", "#54bee8", "#0bd98a", "#2e2e2e", "#b48cff"):
            r, g, b = charts.rgb_of(charts.snap(c))
            cube = all(v in (0, 95, 135, 175, 215, 255) for v in (r, g, b))
            grey = r == g == b and (r - 8) % 10 == 0
            assert cube or grey, c
    finally:
        charts.truecolor = True
    assert charts.snap("#FF7D08") == "#ff7d08"


def test_plot_draws_in_well_under_the_redraw_budget():
    xs = list(range(2000))
    ys = [100 + (x * 7919 % 97) for x in xs]
    xs2, ys2 = charts.resample(xs, ys, 240)
    t0 = time.perf_counter()
    for _ in range(5):
        charts.Plot(120, 30).line(xs2, ys2).hline(150).render()
    assert (time.perf_counter() - t0) / 5 < 0.010                           # rule 10: a redraw stays under 10 ms


def test_sparkline_and_bars():
    assert charts.spark([1, 2, 3, 4, 5, 6, 7, 8]) == "▁▂▃▄▅▆▇█" and charts.spark([]) == "" and charts.spark([3, 3]) == "▄▄"
    assert charts.hbar(0.5, 10) == "█████" and charts.hbar(0, 10) == ""
    assert len(charts.stacked([[0.2, 0.8]] * 12, charts.SERIES, 5)) == 5


def test_asyncio_queue_is_created_outside_a_loop():
    assert isinstance(Hub().stream_commands, asyncio.Queue)


def test_a_bare_glimpse_tui_opens_on_the_odds_and_the_front_page_is_behind_a_flag():
    """James: start easy with the odds page; the terminal is the world behind the curtain, for those who look."""
    from glimpse_tui import opening_launchpad

    cfg = config.load()
    assert opening_launchpad([], cfg) is None                       # None means the odds screen
    assert opening_launchpad(["MEMP"], cfg) is None                 # a GO bar command still opens on the odds under it
    assert opening_launchpad(["--terminal"], cfg) == "BTC"
    assert opening_launchpad(["-t"], cfg) == "BTC"
    front = {**cfg, "opens_on": "terminal", "default_launchpad": "MACRO"}
    assert opening_launchpad([], front) == "MACRO"                  # the setting decides when no flag does
    assert opening_launchpad(["--odds"], front) is None             # and a flag beats the setting
    assert opening_launchpad(["--markets"], front) is None          # the old name for it still works


def test_j_and_k_move_exactly_one_row_whatever_the_width():
    """A cell is about three times wider than it is tall. Ranking the two distances together let a window half
    the page across beat the whole row below it, so `j` skipped a row; the gap along the way now comes first."""
    root = panes.load_layout("BTC")
    rows = [[lf.command for lf in panes.leaves(child)] for child in root.children]
    for width in (80, 100, 120, 160, 190, 240):
        ws = _Fake(root, width, 21 * len(rows))
        seen, at = [], panes.leaves(root)[0]
        ws.focused = at
        for _ in range(len(rows) * 2):
            panes.Workspace.move(ws, "j")
            seen.append(ws.focused.command)
        walked = [c for i, c in enumerate(seen) if i == 0 or c != seen[i - 1]]
        assert [next(r for r in rows if c in r) for c in walked] == rows[1:], width
        up = []
        for _ in range(len(rows) * 2):
            panes.Workspace.move(ws, "k")
            up.append(ws.focused.command)
        walked_up = [c for i, c in enumerate(up) if i == 0 or c != up[i - 1]]
        assert [next(r for r in rows if c in r) for c in walked_up] == rows[-2::-1], width


class _Fake:
    """Enough of a Workspace for `move`: a tree, a size and a focus. Driving the real widget needs a running app."""

    def __init__(self, root, width, height):
        from textual.geometry import Size
        self.root, self.size, self.focused = root, Size(width, height), None

    def page_height(self):
        return max(self.size.height, self.root.row_height * len(self.root.children))

    def focus_leaf(self, leaf):
        if leaf is not None:
            self.focused = leaf
