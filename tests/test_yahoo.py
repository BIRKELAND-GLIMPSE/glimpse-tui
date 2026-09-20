"""Yahoo Finance's public chart API, on recorded answers: the quote board, closed markets, history, and the off switch."""
import pytest
from offline import ROOT, fixture

from glimpse_tui.data import quotes, yahoo
from glimpse_tui.term import config
from glimpse_tui.term.hub import Hub


def test_spark_parses_price_previous_close_range_and_closes():
    got = yahoo.parse_spark(fixture("yahoo/spark_all.json"))
    spx = got["^GSPC"]
    assert spx.price == 7650.5 and spx.prev == 7637.76 and spx.name == "S&P 500" and len(spx.closes) >= 20
    assert spx.low <= spx.price <= spx.high and spx.closed                     # recorded on a Saturday
    assert got["GC=F"].price > 4000 and got["^TNX"].price == pytest.approx(4.998)
    assert yahoo.parse_spark({}) == {} and yahoo.parse_spark({"spark": {"result": [{"symbol": "X"}]}}) == {}


@pytest.mark.yahoo
async def test_the_board_quotes_indices_futures_yields_and_shares_through_one_request_per_twenty(no_network):
    hub = Hub()
    await quotes.refresh(hub, ["SPX", "XAU", "US10Y", "UKX", "MSTR", "BRENT", "USDJPY", "EURUSD", "DXY"])
    q = hub.quotes
    assert q["SPX"].prov.source == "yahoo" and q["SPX"].price == 7650.5 and q["SPX"].prev == 7637.76 and len(q["SPX"].history) >= 20
    assert q["XAU"].prov.source == "yahoo" and not q["XAU"].proxy_for             # COMEX gold, not the PAXG stand-in
    assert q["UKX"].prov.source == "yahoo" and q["MSTR"].price == 153.92          # no open source had these before
    assert q["US10Y"].price == pytest.approx(4.998) and q["BRENT"].prov.source == "yahoo"
    assert q["EURUSD"].prov.source.startswith("kraken")                           # a live exchange still comes first
    spark = [u for u in no_network.seen if "/v7/finance/spark" in u]
    assert len(spark) == 1


@pytest.mark.yahoo
async def test_history_reads_yahoo_and_the_off_switch_falls_back_to_fred(no_network):
    hub = Hub()
    xs, ys, prov = await quotes.history(hub, hub.book.get("SPX"), 400)
    assert prov.source == "yahoo" and len(ys) > 300 and xs == sorted(xs)
    hub.cfg["sources"]["yahoo"] = False
    await quotes.refresh(hub, ["SPX"])
    assert hub.quotes["SPX"].prov.source.startswith("fred")
    assert config.set_value(hub.cfg, "sources.yahoo", "false") is False


def test_the_fixture_is_whole():
    assert len(fixture("yahoo/spark_all.json")["spark"]["result"]) == 72 and (ROOT / "yahoo" / "chart_gspc_2y.json").is_file()


@pytest.mark.yahoo
async def test_commodities_read_futures_with_exchange_units_and_skip_fred(no_network):
    from glimpse_tui.funcs import markets
    hub = Hub()
    pane = markets.GlcoPane(hub)
    await pane.reload()
    text = "\n".join(t.plain for t in pane.draw(120, 40))
    assert "$/bbl" in text and "¢/bu" in text and "Front-month futures" in text and "yahoo" not in text
    assert "XAG" in text and "67.15" in text                                   # silver, which no open source had before
    assert not [u for u in no_network.seen if "fredgraph" in u]                 # nothing asked of FRED
