"""DES, FA, CF, CFS, TRSY, MINR, ETF, N, TOP and the SEC EDGAR client.

Every SEC fixture under tests/fixtures/sources/sec/ except error_403_www.html is SYNTHETIC: hand-built from the SEC's
published API documentation, because the SEC refuses requests without a contact and none was sent (PLAN D38). The
tests prove the client's behaviour on the documented shapes. They prove nothing about what the SEC really sends.
No test touches the network.
"""
import json
import time

import httpx
import pytest
import test_app as T
from offline import ROOT, fixture
from rich.cells import cell_len

import glimpse_tui.app as A
from glimpse_tui import auth
from glimpse_tui.data import core, http, quotes, sec
from glimpse_tui.funcs import companies as C
from glimpse_tui.term import gobar, registry
from glimpse_tui.term.hub import Hub

CONTACT = "Test Harness test@example.invalid"
SYNTHETIC = "hand-built from the SEC's published API documentation on 2026-09-19; not a recorded response; values are illustrative"
ALL = ("DES MSTR", "FA MSTR", "CF MSTR", "CFS bitcoin treasury form:8-K", "TRSY", "MINR", "ETF", "N MSTR", "N", "TOP")


@pytest.fixture(autouse=True)
def forget_refusals():
    sec._Edgar.refused_ua = ""
    yield
    sec._Edgar.refused_ua = ""


def make_hub(contact: str = CONTACT) -> Hub:
    hub = Hub()
    if contact:
        hub.cfg["sec_user_agent"] = contact
        hub.apply_config()
    for s in hub.sources.values():
        s.retries = 0                                       # a company with no fixture answers 599 once, without the backoff sleep
    c = hub.chain
    c.height, c.tip_time, c.tip_pool, c.tip_txs, c.tip_fees_sat = 967_731, time.time() - 420, "Foundry USA", 3288, 2.1e6
    hub.chain.fees, hub.chain.mempool_vsize = {"fastestFee": 4}, 43e6
    return hub


async def open_pane(hub: Hub, text: str):
    cmd = gobar.parse(text, hub.book)
    pane = registry.get(cmd.func).make.from_command(hub, cmd)
    pane.active = True
    await pane.reload()
    return pane


def screen(pane, w: int = 110, h: int = 36) -> str:
    return "\n".join(ln.plain for ln in pane.draw(w, h))


def sec_requests(rec) -> list[str]:
    return [u for u in rec.seen if "sec.gov" in u]


# ── the fixtures say what they are ──────────────────────────

def test_every_sec_fixture_is_marked_synthetic_except_the_recorded_refusal():
    manifest = json.loads((ROOT / "sec" / "_manifest.json").read_text())["files"]
    for f in sorted((ROOT / "sec").iterdir()):
        if f.name in ("_manifest.json", "error_403_www.html"):
            continue
        assert f.name in manifest and manifest[f.name]["status"] == 200, f.name
        if f.suffix == ".json":
            assert json.loads(f.read_text())["_synthetic"] == SYNTHETIC, f.name
        else:
            assert f"<!-- _synthetic: {SYNTHETIC} -->" in f.read_text(), f.name
    assert manifest["error_403_www.html"]["status"] == 403 and "Request Rate Threshold Exceeded" in fixture("sec/error_403_www.html")


# ── the contact: nothing is sent without one ────────────────

async def test_without_a_contact_no_sec_host_is_asked_and_every_pane_says_how_to_set_it(no_network):
    hub = make_hub(contact="")
    for text in ALL:
        pane = await open_pane(hub, text)
        page = " ".join(screen(pane).split())
        assert "SET sec_user_agent" in page, text
        if pane.code != "TOP":                              # TOP keeps to one dim line; the others explain in full
            assert "Your Name you@example.com" in page and "SEC receives it" in page.replace("receives that text", "receives it"), text
            assert "Nothing is sent" in page, text
            assert "requires" in page and "contact" in page, text
    assert sec_requests(no_network) == []
    des = " ".join(screen(await open_pane(hub, "DES MSTR")).split())
    assert "The SEC receives that text on every request this terminal makes to sec.gov" in des
    assert "Nothing is sent to the SEC until it is set" in des


async def test_the_client_itself_refuses_to_send_without_a_contact_or_with_the_placeholder(no_network):
    hub = make_hub(contact="")
    for ua in ("", "   ", "Your Name you@example.com"):
        for key in ("sec", "sec_www", "sec_efts"):
            hub.sources[key].user_agent = ua
        with pytest.raises(core.SourceError, match="no contact set"):
            await hub.sources["sec"].submissions("0001050446")
        with pytest.raises(core.SourceError, match="no contact set"):
            await hub.sources["sec_www"].tickers()
        with pytest.raises(core.SourceError, match="no contact set"):
            await hub.sources["sec_efts"].search(sec.parse_query(["bitcoin"]))
    assert sec_requests(no_network) == []
    hub.cfg["sec_user_agent"] = "Your Name you@example.com"                 # typed in as is: still nothing goes out
    hub.apply_config()
    page = screen(await open_pane(hub, "DES MSTR"))
    assert "THE SEC NEEDS A CONTACT FIRST" in page and sec_requests(no_network) == []


async def test_clearing_the_contact_stops_requests_again(no_network):
    hub = make_hub()
    assert "Strategy Inc" in screen(await open_pane(hub, "DES MSTR"))
    n = len(sec_requests(no_network))
    hub.cfg["sec_user_agent"] = ""                          # hub.apply_config() leaves the old value on the sources; the panes clear it
    hub.apply_config()
    assert "THE SEC NEEDS A CONTACT FIRST" in screen(await open_pane(hub, "CF MSTR"))
    assert hub.sources["sec"].user_agent == "" and len(sec_requests(no_network)) == n


async def test_the_user_agent_sent_is_the_configured_one(no_network):
    sent: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.host, request.headers.get("user-agent", "")))
        return no_network(request)

    http.use_transport(httpx.MockTransport(handler))
    hub = make_hub()
    for text in ("DES MSTR", "CFS bitcoin form:8-K"):
        await open_pane(hub, text)
    cf = await open_pane(hub, "CF MSTR")
    cf.enter()
    await cf.reload()
    hosts = {h for h, _ in sent if h.endswith("sec.gov")}
    assert hosts == {"www.sec.gov", "data.sec.gov", "efts.sec.gov"}
    assert all(ua == CONTACT for h, ua in sent if h.endswith("sec.gov"))
    assert all(CONTACT not in ua for h, ua in sent if not h.endswith("sec.gov"))        # the contact goes to the SEC and nobody else


async def test_a_403_is_reported_as_a_refused_user_agent_and_never_asked_again(no_network):
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("sec.gov"):
            asked.append(str(request.url))
            return httpx.Response(403, text=fixture("sec/error_403_www.html"), headers={"content-type": "text/html"})
        return no_network(request)

    http.use_transport(httpx.MockTransport(handler))
    hub = make_hub()
    hub.sources["sec_www"].retries = 2                      # even with retries allowed, a 403 is asked once
    pane = await open_pane(hub, "DES MSTR")
    page = screen(pane)
    assert "THE SEC REFUSED THE USER-AGENT" in page and CONTACT in page and "SET sec_user_agent" in page
    assert asked == ["https://www.sec.gov/files/company_tickers.json"]
    await pane.reload()
    for text in ("TRSY", "CF MSTR", "N", "TOP"):
        assert "refused the User-Agent" in screen(await open_pane(hub, text)), text
    with pytest.raises(core.SourceError, match="refused the User-Agent"):
        await hub.sources["sec"].facts("0001050446")        # another SEC host: the same gate, so it is not asked either
    assert len(asked) == 1
    hub.cfg["sec_user_agent"] = "Someone Else test@example.invalid"         # a new contact is a new question
    hub.apply_config()
    await open_pane(hub, "CF MSTR")
    assert len(asked) == 2


# ── parsing the documented shapes ───────────────────────────

def test_archive_urls_and_identifiers():
    assert sec.cik10(1050446) == sec.cik10("CIK0001050446") == "0001050446"
    assert sec.dashed("000105044626000091") == sec.dashed("0001050446-26-000091") == "0001050446-26-000091"
    assert sec.archive_url("0001050446", "0001050446-26-000091", "mstr-20260914.htm") == (
        "https://www.sec.gov/Archives/edgar/data/1050446/000105044626000091/mstr-20260914.htm")
    assert sec.archive_url(1050446, "000105044626000091") == (
        "https://www.sec.gov/Archives/edgar/data/1050446/000105044626000091/0001050446-26-000091-index.htm")
    with pytest.raises(core.SourceError):
        sec.cik10("")


def test_filings_come_out_of_the_parallel_arrays_newest_first():
    rows = sec.filings(fixture("sec/submissions_CIK0001050446.json"), "MSTR")
    assert [f.form for f in rows] == ["8-K", "10-Q", "8-K", "4", "10-Q", "DEF 14A", "10-K"]
    first = rows[0]
    assert (first.accession, first.filed, first.report, first.document, first.items) == (
        "0001050446-26-000091", "2026-09-15", "2026-09-14", "mstr-20260914.htm", "8.01,9.01")
    assert first.url.endswith("/1050446/000105044626000091/mstr-20260914.htm") and first.ticker == "MSTR" and first.company == "Strategy Inc"
    assert sec.item_words(first.items) == "other events" and sec.item_words("2.02,9.01") == "results of operations"
    assert [f.form for f in rows if sec.form_matches(f.form, "10-Q")] == ["10-Q", "10-Q"]
    assert sec.form_matches("10-K/A", "10-K") and sec.form_matches("13F-HR", "13F") and not sec.form_matches("424B5", "4")
    # a shorter array, a missing one, and the acceptance time when the index carries it
    odd = sec.filings({"cik": "1", "filings": {"recent": {"accessionNumber": ["0000000001-26-000002", "0000000001-26-000001"],
                       "form": ["8-K"], "filingDate": ["2026-09-18", "2026-09-18"],
                       "acceptanceDateTime": ["2026-09-18T20:15:00.000Z", "2026-09-18T12:00:00.000Z"]}}})
    assert [f.form for f in odd] == ["8-K", ""] and odd[0].at - odd[1].at == 8.25 * 3600 and odd[0].items == ""
    assert sec.filings({}) == []


def test_company_tickers_become_securities_the_go_bar_resolves():
    table = sec.parse_tickers(fixture("sec/company_tickers.json"))
    assert table["MSTR"] == ("0001050446", "Strategy Inc", "") and "_SYNTHETIC" not in table and len(table) == 5


def test_annual_and_quarterly_statements_are_separate_and_the_fourth_quarter_is_derived():
    facts = sec.slim(fixture("sec/companyfacts_CIK0001050446.json"))
    yr, qt = sec.statements(facts, "annual"), sec.statements(facts, "quarterly")
    assert yr["income"].periods == ["2025-12-31", "2024-12-31"]
    rev = yr["income"].line("Revenue").by_end
    assert rev["2025-12-31"].val == 480e6 and rev["2025-12-31"].form == "10-K" and rev["2025-12-31"].accn == "0001050446-26-000012"
    assert rev["2024-12-31"].accn == "0001050446-26-000012"                 # restated comparatives: the latest filing wins
    assert not any(p.derived for ln in yr["income"].lines for p in ln.by_end.values())
    q = qt["income"].line("Revenue").by_end
    assert qt["income"].periods[:3] == ["2026-06-30", "2026-03-31", "2025-12-31"]
    assert [q[e].val / 1e6 for e in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31")] == [110, 115, 120, 135]
    assert q["2025-12-31"].derived == "12 months less 9 months" and q["2025-12-31"].form == "10-K" and q["2025-06-30"].derived == ""
    assert "2025-12-31" not in {e for e, p in q.items() if p.val == 480e6}     # the year never poses as a quarter
    gp = qt["income"].line("Gross profit").by_end["2025-12-31"]              # no year-to-date figures: the year less three quarters
    assert gp.val == 95e6 and gp.derived == "12 months less the three stated quarters"
    cfo = qt["cash"].line("Cash from operations").by_end                     # 10-Q cash flows run year to date: de-cumulated
    ends = ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30")
    assert [cfo[e].val / 1e6 for e in ends] == [10, 15, 20, 25, -5, 13]
    assert cfo["2025-06-30"].derived == "6 months less 3 months" and cfo["2025-03-31"].derived == ""
    assert yr["cash"].line("Cash from operations").by_end["2025-12-31"].val == 70e6
    bal_y, bal_q = yr["balance"], qt["balance"]
    assert bal_y.periods == ["2025-12-31", "2024-12-31"] and bal_q.periods[:3] == ["2026-06-30", "2026-03-31", "2025-12-31"]
    assert bal_q.line("Total assets").by_end["2026-06-30"].val == 70_000e6
    assert bal_q.line("Digital assets (found)").by_end["2026-06-30"].concept == "us-gaap:CryptoAssetFairValue"
    keys = {label: (a, b) for label, a, b in sec.key_financials(facts)}
    assert keys["revenue"][0].val == 480e6 and keys["revenue"][1].val == 130e6 and keys["debt"][1].val == 8_200e6


def test_crypto_holdings_are_discovered_with_the_element_name_and_the_filing():
    raw = fixture("sec/companyfacts_CIK0001050446.json")
    found = sec.discover_crypto(sec.slim(raw))
    u, fv = found.units, found.fair_value
    assert (u.concept, u.point.val, u.point.unit, u.point.end) == ("us-gaap:CryptoAssetNumberOfUnits", 600_000, "pure", "2026-06-30")
    assert (u.point.form, u.point.accn, u.point.filed) == ("10-Q", "0001050446-26-000080", "2026-08-04") and "candidate name" in u.why
    assert (fv.concept, fv.point.val, fv.point.unit) == ("us-gaap:CryptoAssetFairValue", 64_000e6, "USD")
    assert found.cost.concept == "us-gaap:CryptoAssetCost"
    # the decoys: coins pledged (a BTC unit, but a part of the holding), an impairment (a duration), a balance last reported in 2023
    decoys = {"mstr:BitcoinPledgedAsCollateralNumberOfUnits", "mstr:DigitalAssetImpairmentLosses", "mstr:DigitalAssetsCarryingValueLegacy"}
    assert decoys <= set(found.seen)
    pinned = sec.discover_crypto(raw, pinned="mstr:BitcoinPledgedAsCollateralNumberOfUnits")
    assert pinned.units.point.val == 30_000 and pinned.units.why == "pinned in the curated list"
    mara = sec.discover_crypto(fixture("sec/companyfacts_CIK0001507605.json"))
    assert mara.units.why == "unit BTC" and mara.units.point.val == 50_000
    assert mara.fair_value.concept == "us-gaap:CryptoAssetFairValueNoncurrent"
    none = sec.discover_crypto({"facts": {"us-gaap": {"Assets": {"label": "Assets", "units": {"USD": [{"end": "2026-06-30", "val": 1}]}}}}})
    assert none.units is None and none.fair_value is None and none.seen == ()
    assert "unverified" in open(sec.__file__).read().lower().split("unit_candidates =")[0][-400:]      # the candidates are labelled as such


def test_shares_outstanding_and_the_mnav_arithmetic():
    facts = fixture("sec/companyfacts_CIK0001050446.json")
    sh = sec.shares_outstanding(facts)
    assert (sh.val, sh.end, sh.form, sh.concept) == (280e6, "2026-07-30", "10-Q", "dei:EntityCommonStockSharesOutstanding")
    assert sec.mnav(280e6, 157.87, 600_000, 81_000.0) == pytest.approx(280e6 * 157.87 / (600_000 * 81_000.0))
    assert sec.mnav(280e6, None, 600_000, 81_000.0) is None and sec.mnav(280e6, 1.0, 0, 81_000.0) is None
    two = {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2026-07-30", "val": 100, "accn": "a", "form": "10-Q", "filed": "2026-08-04"},
        {"end": "2026-07-30", "val": 20, "accn": "a", "form": "10-Q", "filed": "2026-08-04"}]}}}}}
    both = sec.shares_outstanding(two)
    assert both.val == 120 and both.derived == "sum of 2 share classes"


def test_clean_text_reads_a_filing():
    text = sec.clean_text(fixture("sec/doc_mstr_8k.htm"))
    for gone in ("SCRIPT-MUST-NOT-APPEAR", "HIDDEN-HEADER", "SPAN-HIDDEN", "font-family", "_synthetic", "mstr-20260914", "<", "&amp;", "&nbsp;"):
        assert gone not in text, gone
    paras = text.split("\n\n")
    assert "UNITED STATES\nSECURITIES AND EXCHANGE COMMISSION" in paras and "FORM 8-K" in paras and "Item 8.01 Other Events." in paras
    assert "CURRENT REPORT pursuant to Section 13 or 15(d) of the Securities Exchange Act of 1934" in paras         # &nbsp; is a space
    body = next(p for p in paras if p.startswith("Bitcoin Holdings Update."))
    assert "offering & its preferred stock programs. The Company’s aggregate" in body and "\n" not in body and "  " not in body
    assert "- The figures above are unaudited.\n- They are stated at cost, not at fair value." in text
    table = next(p for p in paras if "Aggregate holdings" in p).split("\n")
    assert len(table) == 4 and len({len(r) for r in table[1:3]}) == 1
    assert table[2].split() == ["Aggregate", "holdings", "640,000", "$47,350.0"] and table[3].endswith("$(12.5)")
    assert table[0].split("  ")[-1].strip() == "Aggregate Purchase Price (in millions)"
    assert table[0].index("BTC Acquired") > len("Aggregate holdings")      # the colspan header sits over its numbers, not the labels
    assert table[1].index("2,000") + len("2,000") == table[2].index("640,000") + len("640,000")                       # numbers right-aligned
    assert sec.clean_text("<p>one<p>two<table><tr><td>never closed") == "one\n\ntwo\n\nnever closed"
    wrapped = C.wrap_doc(text, 50)
    assert max(len(ln) for ln in wrapped if "  " not in ln.strip()) <= 50 and table[2] in wrapped           # prose wraps, table rows never do


def test_cfs_arguments_and_search_hits():
    q = sec.parse_query(["bitcoin", "treasury", "form:8-K,10-q", "since:2026-01-01", "until:nonsense"])
    assert q == sec.Query("bitcoin treasury until:nonsense", ("8-K", "10-Q"), "2026-01-01", "")
    assert q.params("2026-09-19") == {"q": "bitcoin treasury until:nonsense", "forms": "8-K,10-Q", "dateRange": "custom",
                                      "startdt": "2026-01-01", "enddt": "2026-09-19"}
    assert sec.parse_query(["bitcoin"]).params("2026-09-19") == {"q": "bitcoin"}
    assert sec.parse_query(["x", "until:2026-06-30"]).params("2026-09-19")["startdt"] == "2001-01-01"
    hits, total = sec.parse_hits(fixture("sec/search_index_bitcoin_8k.json"))
    assert total == 2
    assert hits[0] == sec.Hit("Strategy Inc", "MSTR", "0001050446", "8-K", "2026-09-15", "0001050446-26-000091", "mstr-20260914.htm")
    assert hits[0].url == "https://www.sec.gov/Archives/edgar/data/1050446/000105044626000091/mstr-20260914.htm"
    assert sec.parse_hits({}) == ([], 0)


# ── the pages, on the synthetic fixtures ────────────────────

def test_the_nine_functions_are_registered_with_help_and_des_is_the_default_for_a_share():
    book = Hub().book
    for code in ("DES", "FA", "CF", "CFS", "TRSY", "MINR", "ETF", "N", "TOP"):
        fn = registry.get(code)
        assert fn and fn.category == "Companies" and fn.make and "sec_user_agent" in fn.help and "403" in fn.help, code
        assert "\u2014" not in fn.help, code
    assert registry.get("DES").default_for == ("EQUITY", "ETF") and registry.get("N").optional and registry.get("TOP").optional
    assert gobar.parse("MSTR", book).func == "DES" and gobar.parse("IBIT", book).func == "DES" and gobar.parse("BTC", book).func == "BTC"
    assert "dei shares-outstanding" in registry.get("TRSY").help and "as-of date" in registry.get("TRSY").help
    assert C.FLOWS in registry.get("ETF").help


async def test_des_shows_the_company_its_bitcoin_and_where_each_number_came_from(no_network):
    hub = make_hub()
    pane = await open_pane(hub, "DES MSTR")
    page = screen(pane)
    assert pane.title == "MSTR · Strategy Inc" and pane.error == ""
    for want in ("Services-Prepackaged Software", "Nasdaq", "fiscal year ends 31 Dec", "CIK 0001050446 · company_tickers.json",
                 "157.87", "MSTR via MSTRx · proxy · live", "shares out 280.00M", "as of 30 Jul 2026 · 10-Q",
                 "held 600,000 BTC", "as of 30 Jun 2026", "us-gaap:CryptoAssetNumberOfUnits (candidate name, unit pure)",
                 "10-Q 0001050446-26-000080 filed 04 Aug 2026", "fair value as filed $64.00B", "us-gaap:CryptoAssetFairValue",
                 "KEY FINANCIALS", "LATEST FILINGS", "other events", "results of operations"):
        assert want in page, want
    cap, px = 280e6 * 157.87, hub.btc_price()
    assert f"market cap ${cap / 1e9:,.2f}B" in page and f"mNAV {cap / (600_000 * px):.2f}" in page and f"at {px:,.0f} now" in page
    rows = {ln.split()[0]: ln.split() for ln in page.split("\n") if ln.split() and ln.split()[0] in ("revenue", "assets", "debt")}
    assert rows["revenue"][1:3] == ["480.0", "130.0"] and rows["assets"][1:3] == ["45,000.0", "70,000.0"]
    assert [p.source for p in pane.provs] == ["sec:0001050446", "sec:0001050446", "kraken"] and pane.provs[0].delay == "daily"
    assert pane.provs[0].as_of == sec.day_ts("2026-09-15")                  # the newest filing's date, not the fetch time
    assert pane.menu() == [("FA", "FA MSTR"), ("CF", "CF MSTR"), ("N", "N MSTR"), ("TRSY", "TRSY")]
    assert ["btc_held", 600_000.0] in pane.export()[1] and ["btc_element", "us-gaap:CryptoAssetNumberOfUnits"] in pane.export()[1]
    assert "ZZZT" in hub.book.companies and hub.book.get("ZZZT").cik == "0009999999"       # every SEC filer is now a security
    assert gobar.parse("ZZZT", hub.book).func == "DES"
    del hub.quotes["MSTR"]                                  # no open quote: the cells that need a price say so
    page = screen(pane)
    assert "no open quote" in page and "market cap –" in page and "needs a price" in page and "mNAV –" in page


async def test_fa_switches_statement_and_frequency_and_ties_each_cell_to_its_filing(no_network):
    hub = make_hub()
    pane = await open_pane(hub, "FA MSTR")
    page = screen(pane)
    assert "INCOME STATEMENT" in page and "31 Dec 25  31 Dec 24" in page and "annual" in pane.title
    assert "Net income      3,000.0   −1,100.0" in page.replace("  " * 3, "  ").replace("     ", " ") or "−1,100.0" in page
    assert "us-gaap:Revenues · 10-K 0001050446-26-000012 · filed 18 Feb 2026" in page
    neg = next(ln for ln in pane.draw(110, 36) if "−1,100.0" in ln.plain)
    assert any("#ff5470" in str(sp.style) for sp in neg.spans)             # a negative number is red
    assert pane.on_key_("q", "q") and "quarterly" in pane.title
    assert "30 Jun 26  31 Mar 26  31 Dec 25  30 Sep 25" in screen(pane)
    for _ in range(2):
        pane.on_key_("l", "l")                              # to the quarter ending 31 Dec 25: derived, and the page says how
    page = screen(pane)
    assert "derived, not stated in any filing: 12 months less 9 months" in page and "135.0" in page
    assert pane.enter() == "CF MSTR 0001050446-26-000012"
    pane.on_key_("s", "s")
    assert "BALANCE SHEET" in screen(pane) and "Digital assets (found)" in screen(pane) and "64,000.0" in screen(pane)
    pane.on_key_("s", "s")
    assert "CASH FLOW" in screen(pane) and pane.on_key_("a", "a") and "70.0" in screen(pane) and "annual" in pane.title
    assert pane.command() == "FA MSTR cash a" and (await open_pane(hub, "FA MSTR cash q")).freq == "quarterly"
    head, rows = pane.export()
    assert head[:4] == ["line", "unit", "2025-12-31", "2025-12-31 filing"]
    assert rows[0][:4] == ["Cash from operations", "USD", 70e6, "0001050446-26-000012"]


async def test_cf_lists_filters_and_reads_a_filing_inside_the_pane(no_network):
    hub = make_hub()
    pane = await open_pane(hub, "CF MSTR")
    page = screen(pane)
    assert " ALL 7 " in page and "8.01,9.01" in page and "0001050446-26-000091" in page and "DEF 14A" in page and pane.n_rows == 7
    for want in ("10-K", "10-Q", "8-K", "DEF 14A", "4", "ALL"):             # S-1 and 13F have no filings here, so f skips them
        assert pane.on_key_("f", "f") and pane.filter == want
    pane.on_key_("f", "f"), pane.on_key_("f", "f"), pane.on_key_("f", "f")
    assert pane.filter == "8-K" and pane.n_rows == 7 and len(pane.shown()) == 2
    assert pane.enter() is None and pane.want and "opening MSTR 8-K" in screen(pane)       # Enter asks; the fetch runs in load
    await pane.reload()
    page = screen(pane)
    assert pane.doc and not pane.selectable and pane.hint().endswith("backspace list")
    assert "MSTR 8-K filed 15 Sep 2026" in page and "/000105044626000091/mstr-20260914.htm" in page
    assert "Item 8.01 Other Events." in page and "640,000" in page and "SCRIPT-MUST-NOT-APPEAR" not in page and "HIDDEN" not in page
    pane.on_key_("j", "j")
    assert pane.top == 1 and pane.cur == 0                                  # j scrolls the text now
    assert pane.on_key_("l", "l") and pane.xoff == 8 and pane.on_key_("h", "h") and pane.xoff == 0
    tall = pane.draw(60, 5)
    assert len(tall) > 20 and sum(1 for ln in tall if ln.plain) <= 5       # only the lines on screen are built
    assert pane.on_key_("backspace", None) and pane.doc is None and pane.selectable and "ALL 7" in screen(pane)
    direct = await open_pane(hub, "CF MSTR 0001050446-26-000091")           # how TRSY, FA and N open a filing
    assert direct.doc and "Item 8.01" in screen(direct)
    assert pane.export()[1][0][-1].endswith("mstr-20260914.htm")


async def test_cfs_searches_with_form_and_date_filters_and_opens_a_hit(no_network):
    hub = make_hub()
    pane = await open_pane(hub, "CFS bitcoin treasury form:8-K since:2026-01-01")
    asked = httpx.URL(next(u for u in no_network.seen if "efts.sec.gov" in u))
    assert asked.path == "/LATEST/search-index" and dict(asked.params) | {"enddt": ""} == {
        "q": "bitcoin treasury", "forms": "8-K", "dateRange": "custom", "startdt": "2026-01-01", "enddt": ""}
    page = screen(pane)
    assert "2 documents match" in page and "Strategy Inc" in page and "MARA Holdings, Inc." in page and "mstr-20260914.htm" in page
    assert pane.title == "bitcoin treasury form:8-K since:2026-01-01"
    pane.enter()
    await pane.reload()
    assert pane.doc and "Bitcoin Holdings Update." in screen(pane)
    pane.on_key_("backspace", None)
    assert "2 documents match" in screen(pane)
    empty = await open_pane(hub, "CFS")
    assert "CFS <words> [form:8-K] [since:2026-01-01]" in screen(empty)


async def test_trsy_ranks_by_holding_and_shows_each_rows_source_date_and_reason(no_network):
    hub = make_hub()
    pane = await open_pane(hub, "TRSY")
    page = screen(pane, 130, 44)
    px = hub.btc_price()
    ranked = page.split("\n")[2:5]                          # by Bitcoin held: Strategy, MARA, then the manual Metaplanet entry
    assert [ln.split()[0] for ln in ranked] == ["1", "2", "3"]
    assert " MSTR " in ranked[0] and " MARA " in ranked[1] and " 3350.T " in ranked[2] and "Strategy" in ranked[0]
    mstr = next(ln for ln in page.split("\n") if " MSTR " in ln)
    cap = 280e6 * 157.87
    for want in ("600,000", "30 Jun", "CryptoAssetNumberOfUnits", f"${600_000 * px / 1e9:,.2f}B", f"${cap / 1e9:,.2f}B",
                 f"{cap / (600_000 * px):.2f}", f"₿{round(600_000 / 280e6 * 1e8):,}"):
        assert want in mstr, want
    meta = next(ln for ln in page.split("\n") if "3350.T" in ln)
    assert "43,000" in meta and "19 Sep" in meta and "manual entry" in meta
    assert "us-gaap:CryptoAssetNumberOfUnits (candidate name, unit pure) · 10-Q 0001050446-26-000080" in " ".join(page.split())
    assert "shares 280.00M as of 30 Jul 2026 · dei:EntityCommonStockSharesOutstanding" in " ".join(page.split())
    assert "mNAV = market cap / value of the Bitcoin held" in page
    top = pane.frame_top(130, time.time()).plain
    assert "holdings: filings · prices: kraken · live" in top and top.startswith("┌0  TRSY  Bitcoin treasury companies")
    assert pane.enter() == "CF MSTR 0001050446-26-000080" and pane.on_key_("D", "D") and pane.go == "DES MSTR"
    pane.cur = 2
    detail = " ".join(screen(pane, 130, 44).split())
    assert "manual entry · https://metaplanet.jp/en" in detail and "not an SEC filer" in detail and pane.enter() is None
    wide = screen(pane, 196, 30)
    assert "why a dash" in wide and "no CIK: the ticker is not in the SEC's ticker file" in wide and "no open quote" in wide
    head, out = pane.export()
    first = dict(zip(head, out[0], strict=True))
    assert first["ticker"] == "MSTR" and first["mnav"] == pytest.approx(cap / (600_000 * px))
    assert first["holding_source"] == "us-gaap:CryptoAssetNumberOfUnits" and first["price_source"] == "via MSTRx · proxy · live"
    assert first["shares_as_of"] == "2026-07-30" and first["holding_as_of"] == "2026-06-30"         # each input keeps its own date


async def test_trsy_without_a_contact_still_shows_the_manual_entries_and_why_the_rest_is_blank(no_network):
    hub = make_hub(contact="")
    pane = await open_pane(hub, "TRSY")
    page = screen(pane, 196, 40)
    first = page.split("\n")[2]
    assert "Metaplanet" in first and "43,000" in first and "no SEC contact set" in page and "SEC FILINGS ARE OFF" in page
    assert sec_requests(no_network) == []


async def test_minr_and_etf_say_what_is_missing_and_why(no_network):
    hub = make_hub()
    minr = await open_pane(hub, "MINR")
    page = screen(minr, 150, 40)
    mara = next(ln for ln in page.split("\n") if ln.startswith("MARA Holdings"))
    assert "50,000" in mara and "30 Jun" in mara and "no open quote" in mara and "domestic" in mara
    assert "us-gaap:CryptoAssetNumberOfUnits (unit BTC)" in page and "no sourced entry in miners.toml" in page
    sourced = {"hashrate_eh": 57.4, "hashrate_as_of": "2026-08-31", "hashrate_url": "https://example.invalid/update"}
    minr.rows[0].entry = {**minr.rows[0].entry, **sourced}
    assert "57.4" in screen(minr, 150, 40) and "https://example.invalid/update" in screen(minr, 150, 40)
    minr.rows[0].hist, minr.btc_hist = [10.0, 12.0], [100.0, 110.0]
    assert minr.versus_btc(minr.rows[0]) == pytest.approx(1.2 / 1.1 - 1)
    etf = await open_pane(hub, "ETF")
    page = screen(etf, 150, 40)
    assert C.FLOWS in page and "Premium or discount needs the fund's daily NAV" in page and "forbidden" in page and "unclear" in page
    assert [r.ticker for r in etf.rows][:4] == ["IBIT", "FBTC", "GBTC", "BTC"]
    assert "BTC" in hub.quotes and C.quote_of(hub, "BTC") is None           # the Grayscale trust never borrows Bitcoin's price
    grayscale = next(ln for ln in page.split("\n") if ln.startswith("BTC "))
    assert "81," not in grayscale and C.metrics(hub, etf.rows[3]).price is None
    assert etf.rows[5].ticker == "BITB" and etf.rows[5].cik == "0001763415" and "SEC facts not loaded" in etf.rows[5].note


async def test_n_lists_filings_and_says_headlines_wait_for_jev(no_network):
    hub = make_hub()
    one = await open_pane(hub, "N MSTR")
    page = screen(one)
    assert "Jev news hub" in page and "8-K" in page and "other events" in page and one.enter() == "CF MSTR 0001050446-26-000091"
    every = await open_pane(hub, "N")
    rows = [ln.split() for ln in screen(every).split("\n") if " 8-K " in ln or " 10-Q " in ln]
    assert [r[3] for r in rows[:3]] == ["MSTR", "MARA", "MARA"] and every.title == "filings across treasuries and miners"


async def test_top_renders_blocks_and_filings_at_36_by_11(no_network):
    hub = make_hub(contact="")
    hub.event("block", height=967_732, pool="AntPool", txs=2911, fees=1.8e6, hash="00")
    hub.event("block", height=967_733, pool="A Pool With A Very Long Name Indeed", txs=4012, fees=3.3e6, hash="01")
    hub.event("alert", text="BTC above 82,000 and still climbing fast, says the alert with too many words")
    pane = await open_pane(hub, "TOP")
    lines = pane.draw(36, 11)
    assert len(lines) == 11 and max(ln.cell_len for ln in lines) <= 36
    text = "\n".join(ln.plain for ln in lines)
    assert "■ #967,733" in text and "■ #967,732 AntPool" in text and "■ #967,731 Foundry USA" in text and "● BTC above 82,000" in text
    assert lines[-1].plain == "SET sec_user_agent turns filings on" and "sat/vB" in lines[-2].plain
    assert pane.cur == 0 and pane.enter() in ("", None) and sec_requests(no_network) == []
    pane.cur = 1
    assert pane.enter() == "BLK 967733"
    hub.cfg["sec_user_agent"] = CONTACT                     # SET: filings join the feed
    hub.apply_config()
    await pane.reload()
    lines = pane.draw(36, 11)
    text = "\n".join(ln.plain for ln in lines)
    assert len(lines) == 11 and max(ln.cell_len for ln in lines) <= 36
    assert "▲ MSTR 8-K: other events" in text and "▲ MARA 8-K: Regulation FD" in text and "SET sec_user_agent" not in text
    assert [go for _, go in pane.items][:5] == ["", "BLK 967733", "BLK 967732", "BLK 967731", "CF MSTR 0001050446-26-000091"]
    assert pane.title == "news · filings" and {p.source.split(":")[0] for p in pane.provs} == {"sec"}


async def test_every_page_fits_its_pane_with_one_cell_glyphs_and_no_em_dashes(no_network):
    for contact in ("", CONTACT):
        hub = make_hub(contact)
        hub.event("block", height=967_732, pool="AntPool", txs=2911, fees=1.8e6, hash="00")
        for text in (*ALL, "FA MSTR balance q", "CFS"):
            pane = await open_pane(hub, text)
            for w in (36, 48, 60, 80, 110, 150, 196):
                t0 = time.perf_counter()
                lines = pane.draw(w, 30)
                took = time.perf_counter() - t0
                assert lines and max(ln.cell_len for ln in lines) <= w, (text, w)
                assert all(cell_len(ch) == 1 for ln in lines for ch in ln.plain), (text, w)
                assert "\u2014" not in "".join(ln.plain for ln in lines), (text, w)
                assert took < 0.05, (text, w, took)         # the budget is 10 ms; five times that catches a regression, not a busy CI box
            assert pane.title and pane.hint() is not None and (pane.export() is None or len(pane.export()) == 2), text


async def test_facts_leave_memory_slim_and_a_second_pane_asks_nothing_new(no_network):
    hub = make_hub()
    await open_pane(hub, "DES MSTR")
    src = hub.sources["sec"]
    held = src._mem[src._key("/api/xbrl/companyfacts/CIK0001050446.json", None)][2]
    assert "mstr" in held["facts"] and set(held["facts"]["us-gaap"]) >= {"Revenues", "CryptoAssetNumberOfUnits"} and "_synthetic" not in held
    n = len(sec_requests(no_network))
    await open_pane(hub, "FA MSTR")
    await open_pane(hub, "CF MSTR")
    assert len(sec_requests(no_network)) == n               # tickers a day, filings an hour, facts a day: all from cache
    await quotes.refresh(hub, ["MSTR"])
    assert hub.quotes["MSTR"].via.upper() == "MSTRX" and C.via_name(hub, hub.quotes["MSTR"].via) == "MSTRx"


# ── through the real app ────────────────────────────────────

async def test_mstr_go_opens_des_and_enter_in_cf_reads_the_filing_in_the_pane(monkeypatch, no_network):
    monkeypatch.setattr(A, "Glimpse", T.FakeApi)
    monkeypatch.setattr(auth, "load_key", lambda: (None, "none"))
    monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
    app = A.Terminal(launchpad="BTC")
    app.shell.hub.cfg["sec_user_agent"] = CONTACT
    app.shell.hub.apply_config()
    for s in app.shell.hub.sources.values():
        s.retries = 0

    async def go(pilot, text):
        await pilot.press("`")
        for ch in text:
            await pilot.press("space" if ch == " " else ch)
        await pilot.press("enter")

    async with app.run_test(size=(140, 44)) as pilot:
        await T.until(pilot, lambda: len(app.shell.ws.panes) == 16)
        await go(pilot, "TOP")
        await T.until(pilot, lambda: app.shell.ws.pane.code == "TOP")
        top = app.shell.ws.pane
        await T.until(pilot, lambda: top.loaded_at and top.filings)
        assert "MSTR 8-K" in top.render().plain and "news · filings" in top.render().plain and "mempool live · sec daily" in top.render().plain
        await go(pilot, "MSTR")                             # a bare ticker opens DES
        await T.until(pilot, lambda: app.shell.ws.pane.code == "DES" and app.shell.ws.pane.loaded_at)
        assert "held 600,000 BTC" in app.shell.ws.pane.render().plain
        await pilot.press("enter", "2")                     # zoom into the page, then the digits menu: CF
        await T.until(pilot, lambda: app.shell.ws.pane.code == "CF" and app.shell.ws.pane.filings)
        cf = app.shell.ws.pane
        await pilot.press("enter")
        await T.until(pilot, lambda: cf.doc is not None)
        assert "Item 8.01 Other Events." in cf.render().plain and "backspace list" in cf.render().plain
        await pilot.press("j", "j", "G", "g", "backspace")
        assert cf.doc is None and "ALL 7" in cf.render().plain and app.shell.ws.pane is cf
