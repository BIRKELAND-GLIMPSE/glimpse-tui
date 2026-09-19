"""The markets family on the recorded internet: QM, WEI, FX, GLCO, RATES, MACRO, ECO, HMAP, RV and DVOL, plus the
Deribit and Fear and Greed clients. Every assertion is a recorded value or an honesty label. No network."""
import math
import statistics
from datetime import UTC, datetime

import pytest
from offline import ROOT, fixture
from rich.cells import cell_len

from glimpse_tui.data import btcmath, core, deribit, fred, sentiment, treasury
from glimpse_tui.funcs import markets as M
from glimpse_tui.term import registry
from glimpse_tui.term.hub import Hub

RECORDED_AT = 1789844345.0              # when the Deribit fixtures were taken (their `usIn`): 19 Sep 2026, 19:39 UTC
TSY = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve"
PANES = {"QM": M.QmPane, "WEI": M.WeiPane, "FX": M.FxPane, "GLCO": M.GlcoPane, "RATES": M.RatesPane, "MACRO": M.MacroPane,
         "ECO": M.EcoPane, "HMAP": M.HmapPane, "RV": M.RvPane, "DVOL": M.DvolPane}


@pytest.fixture(autouse=True)
def routes(no_network, monkeypatch):
    """Fixtures recorded without a route in tests/offline.py. The year-ago curve is last year's whole file."""
    api = "/api/v2/public"
    no_network.by_path[("www.deribit.com", f"{api}/get_volatility_index_data")] = ROOT / "deribit/dvol_btc_1d.json"
    no_network.by_path[("www.deribit.com", f"{api}/get_book_summary_by_currency")] = ROOT / "deribit/book_summary_btc_option.json"
    no_network.by_path[("www.deribit.com", f"{api}/get_index_price")] = ROOT / "deribit/index_price_btc_usd.json"
    no_network.by_path[("www.deribit.com", f"{api}/get_instruments")] = ROOT / "deribit/instruments_btc_option.json"
    no_network.by_path[("api.alternative.me", "/fng/")] = ROOT / "alternative_me/fng_limit30.json"
    no_network.by_path[("api.exchange.coinbase.com", "/products/PAXG-USD/candles")] = ROOT / "coinbase/candles_paxgusd_1d.json"
    no_network.by_url[f"{TSY}&field_tdr_date_value=2025"] = ROOT / "treasury/yield_curve_2025.xml"
    monkeypatch.setattr(M, "_now", lambda: RECORDED_AT)
    return no_network


def make_hub() -> Hub:
    hub = Hub()
    for s in hub.sources.values():                      # nothing here is a real host: no need to queue politely or retry a 599
        s.bucket, s.retries = core.TokenBucket(1000, 1000), 0
    return hub


def forecast(now: float = RECORDED_AT, sigma: float = 0.40, hours: int = 200):
    """A synthetic Glimpse market: hourly closes whose 80% band is exactly a lognormal of `sigma` a year."""
    out = []
    for i in range(1, hours + 1):
        end = now + i * 3600
        s = sigma * math.sqrt(i * 3600 / btcmath.YEAR_S) * btcmath.Z80
        out.append((end, 81_400.0, 81_400.0 * math.exp(-s), 81_400.0 * math.exp(s)))
    return out


async def loaded(code: str, args=(), hub: Hub | None = None):
    pane = PANES[code](hub or make_hub(), None, args)
    await pane.reload()
    await pane.settle()
    return pane


def screen(pane, w: int = 100, h: int = 40) -> str:
    return "\n".join(ln.plain for ln in pane.draw(w, h))


def series(sid: str) -> tuple[list[float], list[float]]:
    return fred.parse_csv(fixture(f"fred/{sid}.csv"))


# ── the clients ─────────────────────────────────────────────

async def test_deribit_parses_dvol_rows_the_index_and_the_envelope():
    db = make_hub().sources["deribit"]
    assert db.base_url == "https://www.deribit.com" and db.rate <= 1.0 and db.delay == "live"
    rows, prov = await db.dvol("1D", *deribit.window(90, RECORDED_AT))
    assert rows[-1] == [1789776000000, 35.46, 36.52, 34.28, 36.31]             # the last 1D bar SOURCES.md quotes
    assert 85 <= len(rows) <= 92 and rows == sorted(rows) and prov.source == "deribit" and prov.as_of == 1789776000
    start, end = deribit.window(90, RECORDED_AT)
    assert end % 3_600_000 == 0 and end - start == 90 * 86_400_000 and end > RECORDED_AT * 1000
    price, _ = await db.index_price()
    assert price == 81413.6
    assert len((await db.instruments())[0]) == 40


async def test_deribit_picks_the_nearest_expiries_and_their_atm_vol(no_network):
    book, _ = await make_hub().sources["deribit"].book_summary("option")
    assert deribit.parse_instrument("BTC-20SEP26-79500-C") == ("20SEP26", datetime(2026, 9, 20, 8, tzinfo=UTC).timestamp(), 79500.0, "C")
    assert deribit.parse_instrument("BTC-27NOV26") is None and deribit.parse_instrument("BTC-5OCT26-80000-P")[2] == 80000.0
    vols = deribit.atm_vols(book, RECORDED_AT, 4)
    assert [v.label for v in vols] == ["20SEP26", "21SEP26"] and vols[0].expiry < vols[1].expiry
    first = vols[0]
    ivs, forwards = {}, []
    for r in book:                                                              # the same answer, worked out longhand
        label, _, strike, _ = deribit.parse_instrument(r["instrument_name"])
        if label == "20SEP26":
            ivs.setdefault(strike, []).append(r["mark_iv"])
            forwards.append(r["underlying_price"])
    lo, hi = max(k for k in ivs if k <= first.forward), min(k for k in ivs if k >= first.forward)
    a, b = sum(ivs[lo]) / len(ivs[lo]), sum(ivs[hi]) / len(ivs[hi])
    assert first.iv == pytest.approx(a + (b - a) * (first.forward - lo) / (hi - lo))
    assert first.forward == statistics.median(forwards) and 81_400 < first.forward < 81_450
    assert min(a, b) <= first.iv <= max(a, b) and abs(first.strike - first.forward) <= 500
    assert deribit.atm_vols(book, RECORDED_AT + 3 * 86400) == []               # both expiries have passed: nothing is invented


async def test_deribit_errors_and_outages_are_source_errors(no_network):
    db = make_hub().sources["deribit"]
    no_network.fail.add("www.deribit.com")
    with pytest.raises(core.SourceError):
        await db.index_price()


async def test_fear_and_greed_carries_its_attribution():
    fg = make_hub().sources["fng"]
    assert fg.note == "source: alternative.me" == sentiment.ATTRIBUTION
    rows, prov = await fg.index(30)
    assert len(rows) == 30 and rows[-1] == sentiment.Reading(1789776000.0, 71, "Greed") and rows[-2].value == 56
    assert prov.note == "source: alternative.me" and prov.delay == "daily" and prov.as_of == 1789776000.0
    assert sentiment.parse({"data": [{"value": "x"}]}) == ([], 3600.0)


# ── arithmetic ──────────────────────────────────────────────

def test_net_liquidity_units_on_the_recorded_values():
    (_, walcl), (_, tga), (_, rrp) = series("WALCL"), series("WTREGEN"), series("RRPONTSYD")
    assert (walcl[-1], tga[-1], rrp[-1]) == (6746548.0, 877028.0, 0.576)
    net = fred.net_liquidity(walcl[-1], tga[-1], rrp[-1])
    assert net == pytest.approx(5_868_944e6)                                    # SOURCES.md: 5,868,944 millions
    assert net != pytest.approx((walcl[-1] - tga[-1] - rrp[-1]) * 1e6, rel=1e-9)     # the slip the conversion prevents


def test_net_liquidity_series_aligns_on_walcl_with_the_last_known_inputs():
    s = {k: fred.Series(k, *series(k), core.Provenance(f"fred:{k}", 0, 0, "daily")) for k in ("WALCL", "WTREGEN", "RRPONTSYD")}
    dates, values = M.net_liquidity_series(s["WALCL"], s["WTREGEN"], s["RRPONTSYD"])
    assert dates[-1] == s["WALCL"].dates[-1] and set(dates) <= set(s["WALCL"].dates)
    assert dates[0] >= s["RRPONTSYD"].dates[0]                                  # no point before every input exists
    rrp_then = s["RRPONTSYD"].at_or_before(dates[-1])
    assert values[-1] == pytest.approx(6746548e6 - 877028e6 - rrp_then * 1e9) and 5.8e12 < values[-1] < 5.9e12
    d4, d52 = M.MacroPane.changes(dates, values)
    assert d4 == pytest.approx(values[-1] - values[-5]) and d52 == pytest.approx(values[-1] - values[-53])


def test_2s10s_from_the_recorded_curve():
    today = treasury.parse(fixture("treasury/yield_curve_202609.xml"))[-1]
    assert (today.get("BC_10YEAR"), today.get("BC_2YEAR")) == (5.01, 4.76)
    assert M.spread_bp(today) == pytest.approx(25.0)
    assert series("T10Y2Y")[1][-1] == 0.25                                      # FRED's own spread agrees
    assert M.spread_bp(None) is None and M.spread_bp(treasury.Curve(0, {"BC_10YEAR": 5.0})) is None


def test_cpi_year_on_year_and_annualised_gdp():
    dates, values = series("CPIAUCSL")
    d, v = M.yoy(dates, values)
    year_ago = values[[datetime.fromtimestamp(t, UTC).strftime("%Y-%m") for t in dates].index("2025-08")]
    assert d[-1] == dates[-1] and v[-1] == pytest.approx(334.131 / year_ago - 1) and 0.02 < v[-1] < 0.05
    assert len(v) == len(values) - 12
    gdp = series("GDP")[1]
    assert M.annualised_growth(gdp)[-1] == pytest.approx((gdp[-1] / gdp[-2]) ** 4 - 1)


def test_ytd_and_period_changes_refuse_to_invent_a_base():
    dates, values = series("SP500")
    first = next(v for t, v in zip(dates, values, strict=True) if datetime.fromtimestamp(t, UTC).year == 2026)
    assert M.ytd(dates, values) == pytest.approx(7637.76 / first - 1)
    assert M.ytd(dates, values, last=8000.0) == pytest.approx(8000.0 / first - 1)
    assert M.ytd(*series("NASDAQ100")) is None                                  # that file starts in June: no first close of the year
    assert M.change_over(dates, values, 1) == pytest.approx(7637.76 / values[-2] - 1)
    cd, cv = series("PCOPPUSDM")                                                # a monthly average
    assert M.change_over(cd, cv, 1) is None and M.change_over(cd, cv, 7) is None
    assert M.change_over(cd, cv, 30) == pytest.approx(cv[-1] / cv[-2] - 1)


def test_gold_market_cap_arithmetic():
    cap = M.gold_market_cap(216_265, 4365.0)
    assert cap == pytest.approx(216_265 * 32_150.7466 * 4365.0) and 30.3e12 < cap < 30.4e12
    supply = btcmath.issued_sats(967_731) / 1e8
    assert M.btc_at_gold_share(1.0, cap, supply) == pytest.approx(cap / supply)
    assert M.btc_at_gold_share(0.10, cap, supply) == pytest.approx(cap / supply / 10) and M.btc_at_gold_share(0.5, cap, 0) == 0.0


def test_glimpse_implied_vol_from_a_synthetic_forecast():
    vols = M.glimpse_vols(forecast(sigma=0.40), RECORDED_AT)
    assert len(vols) == 200 and all(v == pytest.approx(40.0) for _, v in vols)  # the band was built from 40% a year
    assert M.nearest_horizon(vols, 86400)[0] == 86400 and M.nearest_horizon(vols, 7 * 86400)[0] == 7 * 86400
    assert M.nearest_horizon(vols[:6], 86400) is None                           # six hours of closes say nothing about 24 h
    assert M.glimpse_vols([(RECORDED_AT - 60, 81_000, 80_000, 82_000)], RECORDED_AT) == []


def test_the_dollar_index_history_uses_every_ecb_fix():
    from glimpse_tui.data import fx_ref
    fixes = fx_ref.parse(fixture("ecb/eurofxref-hist-90d.xml"))
    dates, values = M.dxy_history(fixes)
    assert len(values) == len(fixes) and round(values[-1], 1) == 100.5 and dates[-1] == fixes[-1].date


# ── the registry ────────────────────────────────────────────

def test_all_ten_functions_register_with_a_real_help_page():
    for code, cls in PANES.items():
        fn = registry.get(code)
        assert fn is not None and fn.make is cls and fn.category == "Markets" and cls.code == code
        assert len(fn.help) > 400 and "—" not in fn.help + fn.summary + fn.name
        assert "source" in fn.help.lower()
    assert "DCOILBRENTEU" in registry.get("GLCO").help and "WALCL" in registry.get("MACRO").help
    assert "free FRED" in registry.get("ECO").help and "alternative.me" in registry.get("DVOL").help
    assert not registry.failed().get("markets")


# ── the pages ───────────────────────────────────────────────

async def test_qm_global_is_honest_about_every_row(no_network):
    pane = await loaded("QM")
    assert pane.error == "" and pane.title == "GLOBAL"
    text = screen(pane)
    assert "81,423.45" in text and "7,637.76" in text and "157.89" in text and "130.80" in text and "5.01%" in text and "+7bp" in text
    rows = {ln.split()[0]: ln for ln in text.splitlines() if ln.strip()}
    assert "proxy PAXG" in rows["XAU"] and "4,365.00" in rows["XAU"]            # gold is the token, and says so
    assert "daily 17 Sep" in rows["SPX"] and "daily 18 Sep" in rows["USDJPY"] and "daily 15 Sep" in rows["BRENT"]
    assert "computed" in rows["DXY"] and "legs live" in rows["DXY"]
    assert "no open source" in rows["XAG"] and "live" in rows["BTC"]
    assert ("closed" in rows["EURUSD"]) or ("live" in rows["EURUSD"])           # Kraken's FX book, by the day of the week
    assert "80,822" in rows["BTC"] and "┿" in rows["BTC"]                       # the day range with its marker
    assert {"XAU", "BTC", "SPX"} <= pane.hub.watch
    assert {p.source for p in pane.provs} >= {"fred:SP500", "treasury.gov", "ecb", "computed", "kraken"}
    assert not no_network.missing


async def test_qm_cycles_lists_takes_user_lists_and_charts_the_row():
    hub = make_hub()
    hub.cfg["lists"] = {"mine": ["btc", "us10y"]}
    pane = await loaded("QM", ("MINE",), hub)
    assert pane.tickers() == ["BTC", "US10Y"] and pane.command() == "QM MINE" and pane.enter() == "GP BTC"
    pane.cur = 1
    assert pane.enter() == "GP US10Y" and pane.menu()[0] == ("GP", "GP US10Y")
    names = list(pane.lists())
    assert names[:2] == ["GLOBAL", "INDICES"] and names[-1] == "MINE"
    assert pane.key("]", "]") and pane.list_name == "GLOBAL" and pane.cur == 0 and pane.due(0)
    assert pane.key("[", "[") and pane.list_name == "MINE" and not pane.key("x", "x")
    adhoc = M.QmPane(hub, None, ("eth", "xau", "nonsense"))
    assert adhoc.list_name == "CUSTOM" and adhoc.tickers() == ["ETH", "XAU"] and adhoc.command() == "QM ETH XAU"
    pane.on_unmount()
    assert "US10Y" not in hub.watch


async def test_qm_stocks_shows_tokens_as_proxies_and_the_rest_as_unavailable():
    pane = await loaded("QM", ("STOCKS",))
    rows = {ln.split()[0]: ln for ln in screen(pane, 120).splitlines() if ln.strip()}
    assert "proxy NVDAx" in rows["NVDA"] and "222.04" in rows["NVDA"] and "proxy MSTRx" in rows["MSTR"]
    assert "no open source" in rows["MSFT"] and "no open source" in rows["COIN"]
    header, data = pane.export()
    nvda = dict(zip(header, next(r for r in data if r[0] == "NVDA"), strict=True))
    assert nvda["proxy_quoted"] == "NVDAx" and nvda["last"] == 222.04 and "proxy" in nvda["delay"]


async def test_wei_keeps_index_and_proxy_apart():
    pane = await loaded("WEI")
    assert pane.error == ""
    text = screen(pane)
    lines = text.splitlines()
    dates, values = series("SP500")
    assert f"{M.ytd(dates, values) * 100:+.1f}%" in next(ln for ln in lines if ln.startswith("SPX"))
    spx = next(i for i, ln in enumerate(lines) if ln.startswith("SPX"))
    assert "7,637.76" in lines[spx] and "daily 17 Sep" in lines[spx] and "fred:SP500" in lines[spx]
    assert "SPY via SPYx" in lines[spx + 1] and "proxy" in lines[spx + 1] and "763.98" in lines[spx + 1] and "7,637" not in lines[spx + 1]
    assert "QQQ via QQQx" in text and "29,644.17" in text and "65,018.95" in text and "51,682.64" in text
    for region in ("AMERICAS", "EMEA", "ASIA"):
        assert region in text
    for gone in ("RUT", "SX5E", "DAX", "UKX", "HSI", "KOSPI", "NIFTY"):
        assert "no open source" in next(ln for ln in lines if ln.startswith(gone))
    assert text.index("AMERICAS") < text.index("EMEA") < text.index("ASIA")


async def test_fx_labels_the_computed_index_and_prices_btc_in_each_currency():
    pane = await loaded("FX")
    assert pane.error == ""
    text = screen(pane)
    assert "1.1493" in text and "157.89" in text and "9.8530" in text and "daily 18 Sep" in text
    assert "computed" in text and "EUR GBP CAD CHF live, JPY SEK daily" in text and "100.282" in text
    assert "57.6%" in text and "13.6%" in text and "−0.576" in text
    assert "proxy USDCNY" in text                                               # no open CNH feed: the onshore fix stands in, labelled
    btc, jpy, eur = pane.hub.quotes["BTC"].price, pane.hub.quotes["USDJPY"].price, pane.hub.quotes["EURUSD"].price
    assert pane.btc_in("USDJPY")[:2] == ("JPY", btc * jpy) and pane.btc_in("EURUSD")[:2] == ("EUR", btc / eur)
    assert f"{btc * jpy:,.0f}" in text and f"{btc / eur:,.0f}" in text


async def test_glco_shows_units_months_and_the_gold_proxy(no_network):
    pane = await loaded("GLCO")
    assert pane.error == ""
    text = screen(pane)
    rows = {ln.split()[0]: ln for ln in text.splitlines() if ln.strip()}
    assert "proxy PAXG" in rows["XAU"] and "$/oz" in rows["XAU"] and "no open source" in rows["XAG"]
    assert "130.80" in rows["BRENT"] and "$/bbl" in rows["BRENT"] and "daily 15 Sep" in rows["BRENT"]
    assert "107.02" in rows["WTI"] and "2.970" in rows["NATGAS"] and "$/MMBtu" in rows["NATGAS"]
    for t, last in (("COPPER", "13,542.82"), ("SOYBEANS", "442.45"), ("CORN", "213.19"), ("WHEAT", "228.74")):
        assert last in rows[t] and "$/mt" in rows[t] and "monthly Jul 2026" in rows[t]
    assert "14.81" in rows["SUGAR"] and "cents/lb" in rows["SUGAR"] and "monthly" in rows["SUGAR"]
    assert "gold via PAXG · proxy" in text and "18.65 oz" in text and "0.05361 BTC" in text
    assert {p.delay for p in pane.provs if p.source.startswith("fred:P")} == {"monthly"}
    assert not no_network.missing


async def test_rates_draws_four_curves_and_the_key_rates(no_network):
    pane = await loaded("RATES")
    assert pane.error == ""
    picks = {label: c for label, _, c in pane.picks()}
    assert [datetime.fromtimestamp(picks[k].date, UTC).strftime("%Y-%m-%d") for k in ("today", "1w", "1m", "1y")] == [
        "2026-09-18", "2026-09-11", "2026-08-19", "2025-09-18"]
    assert picks["1y"].get("BC_10YEAR") == 4.11                                 # from last year's file
    text = screen(pane)
    assert "18 Sep 2026" in text and "2s10s +25bp" in text and "fed funds 3.88%" in text and "SOFR 3.85%" in text
    assert "10Y real 2.68%" in text and "10Y breakeven 2.33%" in text           # the Treasury's real curve is a day newer than DFII10
    ten = next(ln for ln in text.splitlines() if ln.split()[:2] == ["10Y", "5.01"])
    assert "+90" in ten and "4.11" in ten and "4.96" in ten and "+5" in ten      # a year ago, and a week ago, in basis points
    assert "1M" in text and "30Y" in text and any(0x2800 < ord(ch) <= 0x28FF for ch in text)     # a braille chart with a tenor axis
    assert "daily" in text and [p.source for p in pane.provs][0] == "treasury.gov"
    assert any("field_tdr_date_value=2025" in u for u in no_network.seen) and not no_network.missing


async def test_rates_without_the_treasury_still_shows_fred(no_network):
    no_network.fail.add("home.treasury.gov")
    pane = await loaded("RATES")
    text = screen(pane)
    assert pane.error == "" and "did not load" in text and "fed funds" in text and "3.88%" in text
    assert "2s10s +25bp" in text                                                # FRED's T10Y2Y stands in for the curve's own spread


async def test_macro_converts_units_and_dates_every_input():
    pane = await loaded("MACRO")
    assert pane.error == ""
    rows = {r[0]: r for r in pane.rows()}
    assert rows["WALCL"][3] == 6746548e6 and rows["WTREGEN"][3] == 877028e6 and rows["RRPONTSYD"][3] == 0.576e9
    assert rows["M2SL"][3] == 23218.0e9 and rows["computed"][3] == pane.net[1][-1]
    assert [rows[k][7] for k in ("computed", "WALCL", "WTREGEN", "RRPONTSYD", "M2SL")] == ["weekly", "weekly", "weekly", "daily", "monthly"]
    text = screen(pane)
    assert "NET LIQUIDITY $5.8" in text and "$6.75T" in text and "$877.0B" in text and "$576.0M" in text and "$23.22T" in text
    assert "16 Sep 2026" in text and "18 Sep 2026" in text and "Jul 2026" in text and "weekly" in text and "monthly" in text
    assert "computed" in text and "BTC (left)" in text and "fred:WALCL · $ millions" in text and "fred:RRPONTSYD · $ billions" in text
    assert pane.key("]", "]") and pane.span == 1 and "2y" in screen(pane)
    assert {p.delay for p in pane.provs if p.source == "fred:WALCL"} == {"weekly"}


async def test_eco_shows_the_prints_and_says_the_calendar_needs_a_key():
    pane = await loaded("ECO")
    assert pane.error == ""
    prints = {p["id"]: p for p in pane.prints()}
    cpi = series("CPIAUCSL")
    assert prints["CPIAUCSL"]["value"] == pytest.approx(M.yoy(*cpi)[1][-1] * 100)
    pay = series("PAYEMS")[1]
    assert prints["PAYEMS"]["value"] == pay[-1] - pay[-2] == 162.0 and prints["PAYEMS"]["prev"] == pay[-2] - pay[-3]
    assert prints["UNRATE"]["value"] == 4.1 and prints["DFF"]["value"] == 3.88
    real = series("GDPC1")[1]
    assert prints["GDPC1"]["value"] == pytest.approx(((real[-1] / real[-2]) ** 4 - 1) * 100)
    text = screen(pane)
    assert "+162k" in text and "4.1%" in text and "3.88%" in text and "Aug 2026" in text and "Q2 2026" in text
    assert "release calendar needs a free FRED key" in text and "monthly" in text and "quarterly" in text and "fred:GDPC1" in text


async def test_hmap_colours_cells_and_keeps_what_it_cannot_measure_dim(no_network):
    pane = await loaded("HMAP")
    assert pane.error == ""
    lines = pane.draw(100, 60)
    text = "\n".join(ln.plain for ln in lines)
    for name in M.HEAT_LISTS:
        assert name in text
    assert "no open source" in text and "monthly" in text and "proxy" in text and "computed" in text and "daily" in text
    dates, values = series("SP500")
    perf = pane.performance("SPX")
    assert perf[0] == pytest.approx(7637.76 / values[-2] - 1) and perf[3] == pytest.approx(M.ytd(dates, values))
    assert pane.performance("COPPER")[:2] == [None, None] and pane.performance("COPPER")[2] is not None
    assert pane.performance("MSFT") == [None] * 4
    spx = next(ln for ln in lines if ln.plain.startswith("SPX"))
    assert any(" on #" in str(s.style) for s in spx.spans)                      # background-coloured cells
    up, down, none = M.heat_cell(0.05, 0.03, 6), M.heat_cell(-0.05, 0.03, 6), M.heat_cell(None, 0.03, 6)
    assert up.plain.strip() == "+5.0" and down.plain.strip() == "−5.0" and none.plain.strip() == "–"
    assert str(up.style).startswith(M.INK.lower()) or str(up.style).startswith(M.INK)      # dark ink on the bright green
    assert "on #" not in str(none.style) and up.cell_len == down.cell_len == none.cell_len == 6
    assert all("candles" in u for u in no_network.missing)                      # ETH and SOL candles were never recorded; Kraken answers


async def test_rv_sizes_btc_against_gold_and_names_the_constant(no_network):
    pane = await loaded("RV")
    assert pane.error == ""
    n = pane.numbers()
    assert n["height"] == 967_731 and n["supply"] == btcmath.issued_sats(967_731) / 1e8 and n["tonnes"] == 216_265
    assert n["gold_cap"] == pytest.approx(216_265 * 32_150.7466 * 4365.0) and n["btc_cap"] == pytest.approx(n["supply"] * 81423.45)
    assert n["ratio"] == pytest.approx(n["btc_cap"] / n["gold_cap"]) and n["oz"] == pytest.approx(81423.45 / 4365.0)
    text = screen(pane)
    assert "via PAXG · proxy" in text and "216,265 t × 32,150.7466 oz/t" in text and "$30.35T" in text and "18.65 oz" in text
    assert "World Gold Council above-ground stock, end 2024, set in config" in text
    for share in (0.10, 0.25, 0.50, 1.00):
        assert f"{n['gold_cap'] * share / n['supply']:,.0f}" in text
    xs, ys = pane.ratio_history()
    assert len(ys) == 350 and ys[-1] == pytest.approx(81408.71 / 4364.76) and "BTC IN OUNCES" in text
    pane.hub.cfg["gold_stock_tonnes"] = 108_132.5
    assert pane.numbers()["gold_cap"] == pytest.approx(n["gold_cap"] / 2)
    assert not no_network.missing


async def test_dvol_sets_deribit_beside_glimpse_with_the_attribution(no_network):
    hub = make_hub()
    hub.forecast = lambda: forecast(sigma=0.40)
    pane = await loaded("DVOL", hub=hub)
    assert pane.error == "" and [a.label for a in pane.atm] == ["20SEP26", "21SEP26"] and pane.deribit_index == 81413.6
    text = screen(pane)
    assert "DVOL 36.31" in text and "+0.85" in text and "ATM IV" in text and "20SEP" in text
    assert "GLIMPSE" in text and "24h 40.0" in text and "7d 40.0" in text and "TERM STRUCTURE" in text
    fng = next(ln for ln in text.splitlines() if "71" in ln and "Greed" in ln)
    assert "source: alternative.me" in fng                                      # right beside the number, as their terms ask
    assert "source: alternative.me" in screen(pane, 42, 14)
    header, rows = pane.export()
    assert ["dvol", "2026-09-19", 36.31] in rows and any(r[0] == "glimpse_iv" for r in rows) and any(r[0] == "deribit_atm_iv" for r in rows)
    assert {p.source for p in pane.provs} == {"deribit", "alternative.me"} and not no_network.missing


async def test_dvol_without_glimpse_closes_says_where_to_load_them(no_network):
    pane = await loaded("DVOL")
    assert pane.error == "" and "open the BTC hourly series in MKT to load Glimpse's closes" in screen(pane)
    no_network.fail.update({"www.deribit.com", "api.alternative.me"})
    down = await loaded("DVOL")
    assert "deribit" in down.error and down.draw(100, 40) == []


# ── every page, every width ─────────────────────────────────

@pytest.mark.parametrize("code", list(PANES))
async def test_every_page_fits_its_pane_and_exports(code):
    hub = make_hub()
    hub.forecast = lambda: forecast()
    pane = await loaded(code, hub=hub)
    assert pane.error == "" and pane.title and pane.provs and pane.hint() and pane.menu()
    for w, h in ((42, 14), (40, 12), (60, 20), (84, 30), (132, 36), (196, 60)):
        lines = pane.draw(w, h)
        assert lines, (code, w)
        for ln in lines:
            assert cell_len(ln.plain) <= w, (code, w, ln.plain)
            assert cell_len(ln.plain) == len(ln.plain) and "—" not in ln.plain and "\n" not in ln.plain, (code, ln.plain)
    assert len({p.source for p in pane.provs}) == len(pane.provs)               # one Provenance per distinct source
    header, rows = pane.export()
    assert rows and all(len(r) == len(header) for r in rows)
    assert not pane.key("q", "q") or code in ("QM", "MACRO")


async def test_the_launchpad_pane_is_forty_columns_and_its_frame_names_the_slowest_delay():
    """LP MACRO at 132x36 gives each pane 44x14, so 40x12 inside. A frame that narrow shows only the first delay."""
    wei = await loaded("WEI")
    spx = next(ln for ln in screen(wei, 40, 12).splitlines() if ln.startswith("SPX"))
    assert "7,637.76" in spx and "+11.4%" in spx and spx.endswith("daily 17 Sep")
    assert "proxy live" in screen(wei, 40, 12) and "no open source" in screen(wei, 40, 12)
    rates = screen(await loaded("RATES"), 40, 12)
    assert "2s10s +25bp" in rates and "FF 3.88%" in rates and "BE 2.33%" in rates and "daily 17 to 18 Sep" in rates
    glco = await loaded("GLCO")
    assert glco.provs[0].delay == "monthly" and glco.provs[-1].delay == "live"
    assert [ln.split()[-2:] for ln in screen(glco, 40, 12).splitlines() if ln.startswith("WHEAT")] == [["monthly", "Jul"]]
    assert (await loaded("QM")).provs[0].delay == "daily" and (await loaded("QM", ("CRYPTO",))).provs[0].delay == "live"
    assert (await loaded("MACRO")).provs[0].source == "fred:WALCL" and (await loaded("DVOL")).provs[0].source == "deribit"
    for code in PANES:
        assert 0 < len(PANES[code](make_hub(), None, ()).title) <= 23, code      # titled before its first load, and briefly


async def test_pages_start_empty_and_survive_a_dead_internet(no_network):
    no_network.fail.update({"fred.stlouisfed.org", "home.treasury.gov", "api.kraken.com", "api.exchange.coinbase.com", "www.bitstamp.net",
                            "www.ecb.europa.eu", "www.deribit.com", "api.alternative.me", "mempool.space", "bitview.space", "blockstream.info"})
    for code, cls in PANES.items():
        fresh = cls(make_hub(), None, ())
        assert isinstance(fresh.draw(42, 14), list)                             # before any load: nothing to show, and no crash
        pane = await loaded(code)
        assert pane.error, code                                                 # the frame says which source failed
        assert isinstance(pane.draw(100, 40), list)


# ── through the real shell ──────────────────────────────────

async def test_lp_macro_opens_six_market_panes_in_the_app(monkeypatch, no_network):
    import test_app as T

    import glimpse_tui.app as A
    from glimpse_tui import auth
    monkeypatch.setattr(A, "Glimpse", T.FakeApi)
    monkeypatch.setattr(auth, "load_key", lambda: (None, "none"))
    monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
    app = A.Terminal(launchpad="MACRO")
    async with app.run_test(size=(132, 36)) as pilot:
        await T.until(pilot, lambda: len(app.shell.ws.panes) == 6 and "BTC" in app.shell.hub.quotes)
        panes = app.shell.ws.panes
        assert [type(p) for p in panes] == [M.RatesPane, M.MacroPane, M.FxPane, M.GlcoPane, M.WeiPane, M.QmPane]
        assert all(p.size.width == 44 and p.size.height == 14 for p in panes)
        qm = panes[-1]
        await T.until(pilot, lambda: "81,423.45" in qm.render().plain)
        text = qm.render().plain
        assert "QM  GLOBAL" in text and "live" in text and "proxy PAXG" in text and "daily" in text.split("\n")[0]
        assert all(cell_len(ln) == 44 for ln in text.split("\n"))               # the frame and every row fill the pane exactly
        await T.until(pilot, lambda: {"XAG", "NDX"} <= app.shell.hub.watch)     # kept fresh by the hub's quote loop while open
        assert qm.command() == "QM GLOBAL" and qm.enter() == "GP BTC"
