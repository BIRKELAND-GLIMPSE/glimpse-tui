"""SEC EDGAR: company tickers, filing indexes, XBRL company facts, full-text search and the filing archive.

READ THIS FIRST (PLAN D38). In Phase 0 every SEC host answered HTTP 403, because the SEC wants a User-Agent with a
contact on every request and no contact may be sent until the user sets one. So no real SEC response has been
recorded. This client is written against the SEC's published API documentation
(https://www.sec.gov/search-filings/edgar-application-programming-interfaces), its fixtures under
tests/fixtures/sources/sec/ are hand-built in the documented shapes and marked synthetic, and nothing here treats a
crypto XBRL element as a verified fact: the elements are discovered in the company's own facts file at run time and
the page shows the element's name and filing beside every number.

Manners: one budget of 5 requests a second across the three hosts (half the SEC's ceiling of 10), tickers cached a
day, filings an hour, company facts a day, all kept in SQLite. With no User-Agent the client refuses to send anything.
A 403 is reported as a refused User-Agent and is not asked again until the User-Agent changes.
"""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any

from . import core
from .core import DAILY, Provenance, Source, SourceError, TokenBucket

DAY, HOUR = 86400.0, 3600.0
PLACEHOLDER = "you@example.com"                 # the text the help shows; it is never sent
HOW_TO_SET = f"SET sec_user_agent Your Name {PLACEHOLDER}"
NO_CONTACT = "SEC: no contact set, so nothing was sent. " + HOW_TO_SET
REFUSED = "the SEC refused the User-Agent (HTTP 403). Check sec_user_agent. Not asked again until it changes."


def contact_ok(user_agent: str) -> bool:
    """True when a request may go out: a contact is set and it is not the placeholder from the help text."""
    ua = (user_agent or "").strip()
    return bool(ua) and PLACEHOLDER not in ua.lower()


class _Budget(TokenBucket):
    """One token bucket for every SEC host: the SEC counts requests per user, not per host. The lock is remade when
    the running loop changes, so a second event loop (tests, a restarted hub) never meets a lock bound to the first."""

    def __init__(self, rate: float, burst: float) -> None:
        super().__init__(rate, burst)
        self._loop: asyncio.AbstractEventLoop | None = None

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._lock, self._loop = asyncio.Lock(), loop
        await super().acquire()


_BUDGET = _Budget(5.0, 5.0)


class _Edgar(Source):
    """The manners every SEC host shares."""

    refused_ua = ""                             # the User-Agent the SEC last answered 403 to, on any of its hosts

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bucket = _BUDGET

    async def _fetch(self, path: str, params: dict | None, text: bool) -> Any:
        if not contact_ok(self.user_agent):
            raise SourceError(NO_CONTACT)       # before anything touches the network
        if _Edgar.refused_ua and _Edgar.refused_ua == self.user_agent:
            raise SourceError(REFUSED)          # one refusal covers the three hosts: the same gate stands in front of them
        try:
            return await super()._fetch(path, params, text)
        except SourceError as e:
            if "HTTP 403" in str(e):
                _Edgar.refused_ua = self.user_agent
                raise SourceError(REFUSED) from None
            raise
        except ValueError:                      # a 200 that is not JSON: the SEC's refusal pages are HTML
            raise SourceError(f"{self.name}: answered with something that is not JSON") from None


def is_refusal(error: str | Exception) -> bool:
    return "refused the User-Agent" in str(error)


def is_no_contact(error: str | Exception) -> bool:
    return "no contact set" in str(error)


# ── identifiers and URLs ────────────────────────────────────

def cik10(cik: str | int) -> str:
    """Ten digits, zero padded: the form the JSON APIs want."""
    digits = re.sub(r"\D", "", str(cik))
    if not digits:
        raise SourceError("SEC: no CIK for this company")
    return digits.zfill(10)


def dashed(accession: str) -> str:
    """0001050446-26-000012, whether or not the dashes came with it."""
    a = re.sub(r"\D", "", accession)
    return f"{a[:10]}-{a[10:12]}-{a[12:]}" if len(a) == 18 else accession


def archive_path(cik: str | int, accession: str, document: str = "") -> str:
    """/Archives/edgar/data/<cik without zeros>/<accession without dashes>/<document>. Without a document name the
    filing's index page, which lists every document in it."""
    acc = dashed(accession)
    return f"/Archives/edgar/data/{int(cik10(cik))}/{acc.replace('-', '')}/{document or acc + '-index.htm'}"


def archive_url(cik: str | int, accession: str, document: str = "") -> str:
    return "https://www.sec.gov" + archive_path(cik, accession, document)


def day_ts(iso: str) -> float:
    """A YYYY-MM-DD date (or an ISO timestamp) as unix seconds, UTC. 0 when it does not parse."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=UTC).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def _days(start: str, end: str) -> int:
    try:
        return (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days
    except ValueError:
        return -1


# ── filings ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Filing:
    cik: str
    accession: str
    form: str
    filed: str                  # YYYY-MM-DD
    report: str = ""            # the period the filing reports on
    document: str = ""          # the primary document's file name
    description: str = ""
    items: str = ""             # an 8-K's item numbers: "2.02,9.01"
    accepted: str = ""          # ISO timestamp, when the feed carries one
    ticker: str = ""
    company: str = ""

    @property
    def at(self) -> float:
        return day_ts(self.accepted) or day_ts(self.filed)

    @property
    def url(self) -> str:
        return archive_url(self.cik, self.accession, self.document)


# The official titles of the 8-K items a reader meets most, shortened. Form 8-K, General Instructions.
ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy", "2.01": "acquisition or disposal",
    "2.02": "results of operations", "2.03": "new financial obligation", "2.05": "exit or disposal costs", "2.06": "material impairment",
    "3.01": "listing notice", "3.02": "unregistered equity sale", "3.03": "holders' rights modified", "4.01": "auditor change",
    "4.02": "non-reliance on statements", "5.01": "change in control", "5.02": "officers and directors", "5.03": "charter or bylaws amended",
    "5.07": "shareholder vote", "7.01": "Regulation FD", "8.01": "other events", "9.01": "statements and exhibits",
}


def item_words(items: str, limit: int = 3) -> str:
    """`8.01,9.01` as `other events`: the items that say something, in words."""
    codes = [c.strip() for c in items.split(",") if c.strip()]
    words = [ITEMS_8K.get(c, c) for c in codes if c != "9.01"] or [ITEMS_8K.get(c, c) for c in codes]
    return ", ".join(words[:limit])


def filings(submissions: dict, ticker: str = "", cik: str = "") -> list[Filing]:
    """The `filings.recent` parallel arrays as rows, newest first (the order the SEC sends)."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    acc = recent.get("accessionNumber") or []
    cik, name = str(submissions.get("cik") or cik), str(submissions.get("name", ""))

    def col(key: str) -> list:
        v = recent.get(key) or []
        return list(v) + [""] * (len(acc) - len(v))

    cols = [col(k) for k in ("form", "filingDate", "reportDate", "primaryDocument", "primaryDocDescription", "items", "acceptanceDateTime")]
    out = [Filing(cik, str(a), *(str(c[i] or "") for c in cols), ticker=ticker, company=name) for i, a in enumerate(acc)]
    return sorted(out, key=lambda f: (f.filed, f.accepted), reverse=True)


def form_matches(form: str, wanted: str) -> bool:
    """`10-K` also takes 10-K/A and 10-KT, `13F` takes 13F-HR; `4` takes only 4 and 4/A."""
    f, w = form.upper(), wanted.upper()
    if not w or w == "ALL":
        return True
    if w in ("3", "4", "5"):
        return f in (w, w + "/A")
    return f == w or f.startswith(w)


# ── company tickers ─────────────────────────────────────────

def parse_tickers(raw: dict) -> dict[str, tuple[str, str, str]]:
    """company_tickers.json -> {ticker: (cik, name, exchange)}, the shape `Book.companies` holds. This file has no
    exchange; the submissions file does."""
    out: dict[str, tuple[str, str, str]] = {}
    for row in raw.values():
        if isinstance(row, dict) and row.get("ticker") and row.get("cik_str") is not None:
            out.setdefault(str(row["ticker"]).upper(), (cik10(row["cik_str"]), str(row.get("title", "")), ""))
    return out


# ── company facts ───────────────────────────────────────────

@dataclass(frozen=True)
class Point:
    """One reported value and the filing it came from."""
    concept: str                # "us-gaap:Revenues"
    unit: str
    end: str
    val: float
    accn: str = ""
    form: str = ""
    filed: str = ""
    fy: int | None = None
    fp: str = ""
    start: str = ""             # empty for a balance-sheet instant
    derived: str = ""           # how a value that no filing states was computed: "12 months less 9 months"
    label: str = ""

    def cite(self) -> str:
        return f"{self.form} {self.accn} filed {self.filed}".strip()


ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"})
CRYPTO_RX = re.compile(r"crypto|bitcoin|digital\s*asset", re.I)

# Candidate element names from the FASB 2024 and 2025 taxonomies (ASU 2023-08, crypto assets at fair value).
# CANDIDATES, UNVERIFIED AGAINST A LIVE FILE (PLAN D38). They only order what discovery finds in a company's own
# file. Nothing is looked up by these names alone, and the page always shows the element it actually used.
UNIT_CANDIDATES = ("CryptoAssetNumberOfUnits",)
FAIR_VALUE_CANDIDATES = ("CryptoAssetFairValue", "CryptoAssetFairValueCurrent", "CryptoAssetFairValueNoncurrent")
COST_CANDIDATES = ("CryptoAssetCost",)
_NOT_A_BALANCE = re.compile(r"gain|loss|impairment|purchase|sale|proceeds|payment|expense|revenue|income|receivable|payable|borrow|"
                            r"collateral|liabilit|fee|cost|price|percent|rate|number|units", re.I)
_BTC_UNIT = re.compile(r"^(btc|xbt|bitcoins?)$", re.I)
_COUNT_WORDS = re.compile(r"number|units|quantity|held|holdings", re.I)
_A_SUBSET = re.compile(r"pledg|collateral|restrict|purchas|acquir|sold|sale|mined|receiv|loan|lent|borrow|customer|safeguard", re.I)

# The standardised statements: a label, then us-gaap concepts in the order they are preferred. A company that changed
# concept over the years (Revenues, then RevenueFromContractWithCustomer…) is stitched together period by period.
Spec = tuple[str, tuple[str, ...], str]
INCOME: tuple[Spec, ...] = (
    ("Revenue", ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax",
                 "SalesRevenueNet"), "USD"),
    ("Cost of revenue", ("CostOfRevenue", "CostOfGoodsAndServicesSold"), "USD"),
    ("Gross profit", ("GrossProfit",), "USD"),
    ("Research and development", ("ResearchAndDevelopmentExpense",), "USD"),
    ("Selling, general, admin", ("SellingGeneralAndAdministrativeExpense",), "USD"),
    ("Operating expenses", ("OperatingExpenses", "CostsAndExpenses"), "USD"),
    ("Operating income", ("OperatingIncomeLoss",), "USD"),
    ("Interest expense", ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"), "USD"),
    ("Pretax income", ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                       "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"), "USD"),
    ("Income tax", ("IncomeTaxExpenseBenefit",), "USD"),
    ("Net income", ("NetIncomeLoss", "ProfitLoss"), "USD"),
    ("EPS basic", ("EarningsPerShareBasic",), "USD/shares"),
    ("EPS diluted", ("EarningsPerShareDiluted",), "USD/shares"),
    ("Diluted shares", ("WeightedAverageNumberOfDilutedSharesOutstanding",), "shares"),
)
BALANCE: tuple[Spec, ...] = (
    ("Cash and equivalents", ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"), "USD"),
    ("Current assets", ("AssetsCurrent",), "USD"),
    ("Total assets", ("Assets",), "USD"),
    ("Current liabilities", ("LiabilitiesCurrent",), "USD"),
    ("Long-term debt", ("LongTermDebt", "LongTermDebtNoncurrent", "ConvertibleNotesPayableNoncurrent", "NotesPayable"), "USD"),
    ("Total liabilities", ("Liabilities",), "USD"),
    ("Shareholders' equity", ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), "USD"),
    ("Liabilities and equity", ("LiabilitiesAndStockholdersEquity",), "USD"),
)
CASHFLOW: tuple[Spec, ...] = (
    ("Cash from operations", ("NetCashProvidedByUsedInOperatingActivities",), "USD"),
    ("Cash from investing", ("NetCashProvidedByUsedInInvestingActivities",), "USD"),
    ("Cash from financing", ("NetCashProvidedByUsedInFinancingActivities",), "USD"),
    ("Capital expenditure", ("PaymentsToAcquirePropertyPlantAndEquipment",), "USD"),
    ("Depreciation, amortisation", ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization"), "USD"),
    ("Stock compensation", ("ShareBasedCompensation",), "USD"),
    ("Net change in cash", ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
                            "CashAndCashEquivalentsPeriodIncreaseDecrease"), "USD"),
)
STATEMENTS: dict[str, tuple[str, tuple[Spec, ...], str, str]] = {        # key -> (title, lines, anchor line, kind)
    "income": ("Income statement", INCOME, "Net income", "duration"),
    "balance": ("Balance sheet", BALANCE, "Total assets", "instant"),
    "cash": ("Cash flow", CASHFLOW, "Cash from operations", "duration"),
}
_WANTED = frozenset(c for _, specs, _, _ in STATEMENTS.values() for _, names, _ in specs for c in names)


def slim(raw: dict) -> dict:
    """A company-facts file is megabytes. Keep the concepts the terminal reads (the statement lines, the cover-page
    share count and anything that mentions crypto) in the same documented shape, and let the rest go."""
    out: dict[str, Any] = {"cik": raw.get("cik"), "entityName": raw.get("entityName", ""), "facts": {}}
    for tax, concepts in (raw.get("facts") or {}).items():
        if not isinstance(concepts, dict):
            continue
        keep = {name: node for name, node in concepts.items() if isinstance(node, dict) and (
            name in _WANTED or (tax == "dei" and name == "EntityCommonStockSharesOutstanding")
            or CRYPTO_RX.search(name) or CRYPTO_RX.search(str(node.get("label") or "")))}
        if keep:
            out["facts"][tax] = keep
    return out


def points(facts: dict, concept: str, unit: str | None = "USD") -> list[Point]:
    """Every reported value of `taxonomy:Concept` in one unit (None: every unit)."""
    tax, _, name = concept.partition(":")
    node = ((facts.get("facts") or {}).get(tax) or {}).get(name) or {}
    out = []
    for u, rows in (node.get("units") or {}).items():
        if unit is not None and u != unit:
            continue
        for r in rows:
            try:
                out.append(Point(concept, u, str(r["end"]), float(r["val"]), str(r.get("accn", "")), str(r.get("form", "")),
                                 str(r.get("filed", "")), r.get("fy"), str(r.get("fp") or ""), str(r.get("start") or ""),
                                 label=str(node.get("label") or "")))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _latest_filed(pts: list[Point]) -> list[Point]:
    """One value a period: the most recently filed, so a restatement wins over the first print."""
    best: dict[tuple[str, str], Point] = {}
    for p in pts:
        k = (p.start, p.end)
        if k not in best or (p.filed, p.accn) > (best[k].filed, best[k].accn):
            best[k] = p
    return list(best.values())


def _is_year(p: Point) -> bool:
    return bool(p.start) and 340 <= _days(p.start, p.end) <= 380


def _is_quarter(start: str, end: str) -> bool:
    return 75 <= _days(start, end) <= 105


def _months(p: Point) -> int:
    return max(round(_days(p.start, p.end) / 30.4), 1)


def annual(pts: list[Point]) -> dict[str, Point]:
    """{period end: value} for fiscal years: twelve-month durations, or instants, that a 10-K (or 20-F, 40-F) reports."""
    year_ends = {p.end for p in pts if p.form in ANNUAL_FORMS and (_is_year(p) or not p.start)}
    return {p.end: p for p in _latest_filed(pts) if p.end in year_ends and (_is_year(p) or not p.start)}


def quarterly(pts: list[Point]) -> dict[str, Point]:
    """{period end: value} for single quarters.

    A balance-sheet instant is its own quarter. A duration is a quarter when the filing states three months. Where
    a filing states only a cumulative figure (cash flows in a 10-Q run year to date, and no 10-K states a fourth
    quarter), the quarter is the cumulative value less the one before it from the same fiscal-year start, and the
    point says so in `derived`."""
    best = _latest_filed(pts)
    out = {p.end: p for p in best if not p.start or _is_quarter(p.start, p.end)}
    by_start: dict[str, list[Point]] = defaultdict(list)
    for p in best:
        if p.start:
            by_start[p.start].append(p)
    for chain in by_start.values():
        chain.sort(key=lambda p: p.end)
        for prev, cur in zip(chain, chain[1:], strict=False):
            if cur.end not in out and _is_quarter(prev.end, cur.end):
                out[cur.end] = replace(cur, val=cur.val - prev.val, start=prev.end, derived=f"{_months(cur)} months less {_months(prev)} months")
    for fy in (p for p in best if _is_year(p) and p.end not in out):        # no year-to-date figure: the year less its three quarters
        inside = [q for q in best if q.start and _is_quarter(q.start, q.end) and q.start >= fy.start and q.end < fy.end]
        if len(inside) == 3:
            out[fy.end] = replace(fy, val=fy.val - sum(q.val for q in inside), start=max(q.end for q in inside),
                                  derived="12 months less the three stated quarters")
    return out


@dataclass
class Line:
    label: str
    unit: str
    by_end: dict[str, Point] = field(default_factory=dict)


@dataclass
class Statement:
    key: str
    title: str
    freq: str                   # annual | quarterly
    periods: list[str]          # period ends, newest first
    lines: list[Line]

    def line(self, label: str) -> Line | None:
        return next((ln for ln in self.lines if ln.label == label), None)


def statement(facts: dict, key: str, freq: str = "annual") -> Statement:
    title, specs, anchor, kind = STATEMENTS[key]
    pick = annual if freq == "annual" else quarterly
    lines = []
    for label, names, unit in specs:
        merged: dict[str, Point] = {}
        for name in names:                                  # first concept in the list wins a period it reports
            for end, p in pick(points(facts, f"us-gaap:{name}", unit)).items():
                merged.setdefault(end, p)
        lines.append(Line(label, unit, merged))
    if key == "balance" and (fv := discover_crypto(facts).fair_value):     # the crypto line, under whatever name the filer uses
        lines.insert(2, Line("Digital assets (found)", "USD", pick(points(facts, fv.point.concept, "USD"))))
    anchored = next((ln.by_end for ln in lines if ln.label == anchor and ln.by_end), None)
    ends = anchored if anchored else {e for ln in lines for e in ln.by_end}
    return Statement(key, title, freq, sorted(ends, reverse=True), lines)


def statements(facts: dict, freq: str = "annual") -> dict[str, Statement]:
    return {k: statement(facts, k, freq) for k in STATEMENTS}


def shares_outstanding(facts: dict) -> Point | None:
    """`dei:EntityCommonStockSharesOutstanding` from the latest cover page. A filer with several share classes may
    state one number a class at the same date in the same filing: those are added and the point says so."""
    pts = points(facts, "dei:EntityCommonStockSharesOutstanding", "shares")
    if not pts:
        return None
    last = max(pts, key=lambda p: (p.end, p.filed, p.accn))
    same = {p.val for p in pts if (p.end, p.accn) == (last.end, last.accn)}
    return replace(last, val=sum(same), derived=f"sum of {len(same)} share classes") if len(same) > 1 else last


KEY_LINES = (("revenue", "income", "Revenue"), ("net income", "income", "Net income"), ("assets", "balance", "Total assets"),
             ("cash", "balance", "Cash and equivalents"), ("debt", "balance", "Long-term debt"), ("equity", "balance", "Shareholders' equity"))


def key_financials(facts: dict) -> list[tuple[str, Point | None, Point | None]]:
    """(label, latest annual, latest quarter) for the six lines DES shows."""
    yr, qt = statements(facts, "annual"), statements(facts, "quarterly")
    out = []
    for label, key, line in KEY_LINES:
        a, q = yr[key].line(line), qt[key].line(line)
        out.append((label, a.by_end[max(a.by_end)] if a and a.by_end else None, q.by_end[max(q.by_end)] if q and q.by_end else None))
    return out


# ── crypto holdings, discovered ─────────────────────────────

@dataclass(frozen=True)
class CryptoFact:
    point: Point
    why: str                    # why discovery chose it: "unit BTC", "candidate name", "label mentions fair value"

    @property
    def concept(self) -> str:
        return self.point.concept


@dataclass(frozen=True)
class Crypto:
    units: CryptoFact | None = None             # how many coins
    fair_value: CryptoFact | None = None        # their fair value in USD, as filed
    cost: CryptoFact | None = None
    seen: tuple[str, ...] = ()                  # every concept in the file that matched, for the page to list


def _choose(found: list[tuple[int, Point, str]]) -> CryptoFact | None:
    """Recency first: only concepts reported within 200 days of the newest stay in the running. Among those the
    best-ranked wins, and a tie goes to the later period, then the later filing."""
    if not found:
        return None
    newest = max(p.end for _, p, _ in found)
    live = [f for f in found if 0 <= _days(f[1].end, newest) <= 200]
    best = min(f[0] for f in live)
    _, p, why = max((f for f in live if f[0] == best), key=lambda f: (f[1].end, f[1].filed))
    return CryptoFact(p, why)


def discover_crypto(facts: dict, pinned: str = "") -> Crypto:
    """Search the company's own facts for crypto concepts (PLAN D38: found, never assumed).

    A concept matches when its name or label mentions crypto, bitcoin or digital asset. Holdings are a non-USD unit:
    a BTC-like unit first, then a candidate name, then a label that reads like a count, and a concept that names a part of the
    holding (pledged, purchased, mined) ranks below all of those. Fair value is a USD instant:
    a candidate name first, then a label that mentions fair value, then any other crypto balance. `pinned` is a
    curated `taxonomy:Concept` from treasuries.toml that overrides the ranking for the unit count."""
    units, values, costs, seen = [], [], [], []
    for tax, concepts in (facts.get("facts") or {}).items():
        for name, node in (concepts or {}).items():
            label = str((node or {}).get("label") or "")
            if not (CRYPTO_RX.search(name) or CRYPTO_RX.search(label)):
                continue
            concept = f"{tax}:{name}"
            seen.append(concept)
            bitcoin = 0 if re.search(r"bitcoin|btc", f"{name} {label}", re.I) else 1
            for unit in (node.get("units") or {}):
                pts = [p for p in _latest_filed(points(facts, concept, unit)) if not p.start]
                if not pts:
                    continue
                last = max(pts, key=lambda p: (p.end, p.filed))
                if unit == "USD":
                    named = name in COST_CANDIDATES
                    if named or re.search(r"\bcost\b", label, re.I):
                        costs.append((0 if named else 1, last, "candidate name" if named else "label says cost"))
                    elif name in FAIR_VALUE_CANDIDATES:
                        values.append((FAIR_VALUE_CANDIDATES.index(name), last, "candidate name, USD"))
                    elif not _NOT_A_BALANCE.search(name):
                        fair = re.search(r"fair\s*value", f"{name} {label}", re.I)
                        values.append(((10 if fair else 20) + bitcoin, last, "mentions fair value, USD" if fair else "a crypto balance in USD"))
                elif "/" not in unit and unit.lower() != "shares":
                    part = 10 if _A_SUBSET.search(name) else 0          # coins pledged, bought or mined are not the whole holding
                    if concept == pinned:
                        units.append((-1, last, "pinned in the curated list"))
                    elif _BTC_UNIT.match(unit):
                        units.append((part, last, f"unit {unit}"))
                    elif name in UNIT_CANDIDATES:
                        units.append((1 + part, last, f"candidate name, unit {unit}"))
                    elif _COUNT_WORDS.search(f"{name} {label}"):
                        units.append((2 + bitcoin + part, last, f"reads like a count, unit {unit}"))
    return Crypto(_choose(units), _choose(values), _choose(costs), tuple(sorted(seen)))


def mnav(shares: float | None, price: float | None, btc: float | None, btc_price: float | None) -> float | None:
    """Market cap over the value of the Bitcoin held: (shares outstanding x price) / (BTC held x BTC price)."""
    if not (shares and price and btc and btc_price) or btc * btc_price <= 0:
        return None
    return shares * price / (btc * btc_price)


# ── full-text search ────────────────────────────────────────

@dataclass(frozen=True)
class Query:
    words: str = ""
    forms: tuple[str, ...] = ()
    since: str = ""
    until: str = ""

    def params(self, today: str) -> dict[str, str]:
        out = {"q": self.words}
        if self.forms:
            out["forms"] = ",".join(self.forms)
        if self.since or self.until:
            out |= {"dateRange": "custom", "startdt": self.since or "2001-01-01", "enddt": self.until or today}
        return out


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_query(args: tuple[str, ...] | list[str]) -> Query:
    """`bitcoin treasury form:8-K since:2026-01-01`: the words, then optional form:, since: and until: tokens.
    A token that is not a date is kept as a word, so nothing typed is silently dropped."""
    words, forms, since, until = [], [], "", ""
    for a in args:
        key, _, val = a.partition(":")
        k = key.lower()
        if k in ("form", "forms") and val:
            forms += [f.upper() for f in val.split(",") if f]
        elif k in ("since", "from", "after") and _DATE.match(val):
            since = val
        elif k in ("until", "to", "before") and _DATE.match(val):
            until = val
        else:
            words.append(a)
    return Query(" ".join(words), tuple(forms), since, until)


_DISPLAY = re.compile(r"^(?P<name>.*?)\s*(?:\((?P<tickers>[A-Z0-9 ,.\-]+)\))?\s*\(CIK (?P<cik>\d+)\)\s*$")


@dataclass(frozen=True)
class Hit:
    company: str
    ticker: str
    cik: str
    form: str
    filed: str
    accession: str
    document: str

    @property
    def url(self) -> str:
        return archive_url(self.cik, self.accession, self.document)


def parse_hits(raw: dict) -> tuple[list[Hit], int]:
    """`hits.hits[]`: `_id` is "<accession>:<file name>", `_source` carries display_names, ciks, form, file_date, adsh."""
    box = raw.get("hits") or {}
    total = box.get("total") or {}
    out = []
    for h in box.get("hits") or []:
        src = h.get("_source") or {}
        adsh, _, doc = str(h.get("_id", "")).partition(":")
        display = str((src.get("display_names") or [""])[0])
        m = _DISPLAY.match(display)
        ciks = src.get("ciks") or ([m["cik"]] if m else [])
        if not (adsh or src.get("adsh")) or not ciks:
            continue
        out.append(Hit((m["name"] if m else display).strip(), ((m["tickers"] or "") if m else "").split(",")[0].strip(), cik10(ciks[0]),
                       str(src.get("form", "")), str(src.get("file_date", "")), dashed(str(src.get("adsh") or adsh)), doc))
    return out, int(total.get("value", len(out)) if isinstance(total, dict) else total or len(out))


# ── a filing as clean text ──────────────────────────────────

_SKIP = frozenset({"script", "style", "head", "noscript", "template", "svg", "ix:header", "ix:hidden"})
_VOID = frozenset({"br", "hr", "img", "input", "meta", "link", "col", "area", "base", "embed", "source", "track", "wbr"})
_BLOCK = frozenset({"p", "div", "section", "article", "blockquote", "pre", "ul", "ol", "dl", "dt", "dd", "center", "address", "form",
                    "header", "footer", "main", "nav", "aside", "figure", "figcaption", "title", "body", "html"})
_HEAD = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_SPACE = re.compile("[ \t\r\n\f\v\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
_ZERO = re.compile("[\u200b-\u200d\u2060\ufeff\u00ad]")     # zero-width marks and soft hyphens
_NUMBER = re.compile("^[($\u20ac\u00a3\\-\u2212\u2013]*\\s?\\d[\\d,.]*\\s?[)%x]*$|^[\\-\u2013\u2014]$")     # a dash cell means nil
_CURRENCY = ("$", "\u20ac", "\u00a3", "\u00a5")
LABEL_MAX = 56
_LOOKS_HTML = re.compile(r"<(html|body|div|p|table|span|font|br)\b", re.I)


def _render_table(rows: list[list[dict]]) -> tuple[list[str], bool]:
    """(lines, was it a data table). A data table comes out as aligned text. A layout table (one column, or cells
    that hold whole paragraphs) comes out as its paragraphs, in order."""
    dense = all(len(c["lines"]) <= 3 and sum(map(len, c["lines"])) <= 220 and not c["nested"] for r in rows for c in r)
    if dense:
        grid = []
        for r in rows:
            cells: list[str] = []
            for c in r:
                cells += [" ".join(c["lines"]).strip()] + [""] * (c["span"] - 1)
            if any(cells):
                grid.append(cells)
        n = max((len(r) for r in grid), default=0)
        grid = [r + [""] * (n - len(r)) for r in grid]
        for r in grid:                                      # "$" joins the number after it, ")" and "%" the one before
            for j, cell in enumerate(r):
                if cell in _CURRENCY:
                    k = next((k for k in range(j + 1, min(j + 3, n)) if r[k]), None)
                    if k is not None:
                        r[k], r[j] = cell + r[k], ""
                elif cell in (")", "%", ")%"):
                    k = next((k for k in range(j - 1, max(j - 3, -1), -1) if r[k]), None)
                    if k is not None:
                        r[k], r[j] = r[k] + cell, ""
        # Fold neighbours that no row uses together: spacer columns, and a colspan header with the numbers under it.
        # Right to left, so a header joins its numbers, and the row labels in the first column are never folded into.
        j = n - 2
        while j >= 0:
            a, b = [r[j] for r in grid], [r[j + 1] for r in grid]
            if all(not (x and y) for x, y in zip(a, b, strict=True)) and (j > 0 or not any(a) or not any(b)):
                for r in grid:
                    r[j] = r[j] or r[j + 1]
                    del r[j + 1]
                n -= 1
            j -= 1
        if n >= 2 and len(grid) >= 2:
            for r in grid:
                if len(r[0]) > LABEL_MAX:
                    r[0] = r[0][:LABEL_MAX - 1] + "…"
            widths = [max(len(r[j]) for r in grid) for j in range(n)]
            return ["  ".join(c.rjust(widths[j]) if _NUMBER.match(c) else c.ljust(widths[j]) for j, c in enumerate(r)).rstrip()
                    for r in grid], True
        return [" ".join(c for c in r if c) for r in grid], False
    out: list[str] = []
    for r in rows:
        for c in r:
            out += [ln for ln in c["lines"]] + [""]
    return out, False


class _FilingReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: list[str] = []
        self.sinks: list[list[str]] = [self.root]
        self.buf: list[str] = []
        self.prefix = ""
        self.skip_tag, self.skip_depth = "", 0
        self.tables: list[dict] = []

    # text ───────────────────────────────────────────────────

    def flush(self, blank: bool = True) -> None:
        text = _SPACE.sub(" ", _ZERO.sub("", "".join(self.buf))).strip()
        self.buf = []
        if not text:
            return
        in_cell = len(self.sinks) > 1
        listed, self.prefix = bool(self.prefix), ""
        self.sinks[-1].append(("- " if listed else "") + text)
        if blank and not listed and not in_cell:
            self.sinks[-1].append("")

    def handle_data(self, data: str) -> None:
        if not self.skip_tag:
            self.buf.append(data)

    # structure ──────────────────────────────────────────────

    def _close_cell(self) -> None:
        t = self.tables[-1] if self.tables else None
        if t and t["open"]:
            self.flush()
            self.sinks.pop()
            t["open"] = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_tag:
            self.skip_depth += tag == self.skip_tag
            return
        a = dict(attrs)
        if tag not in _VOID and (tag in _SKIP or "hidden" in a or _HIDDEN.search(a.get("style") or "")):
            self.flush()
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag == "table":
            self.flush()
            if self.tables and self.tables[-1]["open"]:
                self.tables[-1]["rows"][-1][-1]["nested"] = True
            self.tables.append({"rows": [], "open": False})
        elif tag == "tr" and self.tables:
            self._close_cell()
            self.tables[-1]["rows"].append([])
        elif tag in ("td", "th") and self.tables:
            self._close_cell()
            t = self.tables[-1]
            if not t["rows"]:
                t["rows"].append([])
            try:
                span = max(1, min(int(a.get("colspan") or 1), 40))
            except ValueError:
                span = 1
            cell = {"lines": [], "span": span, "nested": False}
            t["rows"][-1].append(cell)
            self.sinks.append(cell["lines"])
            t["open"] = True
        elif tag == "br":
            self.buf.append(" ") if len(self.sinks) > 1 else self.flush(blank=False)
        elif tag == "li":
            self.flush()
            self.prefix = "- "
        elif tag in _HEAD:
            self.flush()
            if len(self.sinks) == 1:
                self.sinks[-1].append("")
        elif tag in _BLOCK or tag == "hr":
            self.flush()

    def handle_endtag(self, tag: str) -> None:
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self.skip_tag = ""
            return
        if tag == "table" and self.tables:
            self._close_cell()
            lines, _ = _render_table(self.tables.pop()["rows"])
            self.sinks[-1] += [*lines, ""]
        elif tag in ("td", "th") and self.tables:
            self._close_cell()
        elif tag == "tr" and self.tables:
            self._close_cell()
        elif tag == "li":
            self.flush(blank=False)
        elif tag in ("ul", "ol"):
            self.flush()
            if len(self.sinks) == 1:
                self.sinks[-1].append("")
        elif tag in _BLOCK or tag in _HEAD:
            self.flush()

    def text(self) -> str:
        while self.tables:                                  # a table the document never closed
            self.handle_endtag("table")
        self.flush()
        out: list[str] = []
        for ln in self.root:
            if ln or (out and out[-1]):
                out.append(ln)
        return "\n".join(out).strip()


def clean_text(html: str) -> str:
    """A filing as text a person can read in a pane: scripts, styles, the hidden inline-XBRL header and anything
    set not to display are dropped; paragraphs, headings and list items keep their breaks; a data table becomes
    aligned columns; whitespace collapses and entities decode. Standard library only. A large filing takes more
    than a few milliseconds, so callers run this through `asyncio.to_thread`."""
    reader = _FilingReader()
    try:
        reader.feed(html)
        reader.close()
    except Exception:                                       # a filing the parser chokes on still shows what was read so far
        pass
    return reader.text()


# ── the three hosts ─────────────────────────────────────────

class SecData(_Edgar):
    def __init__(self) -> None:
        super().__init__(name="data.sec.gov", base_url="https://data.sec.gov", delay=DAILY, rate=5.0, burst=5.0, retries=1,
                         serves="SEC EDGAR filing indexes and XBRL company facts (needs sec_user_agent)")
        self._slim: dict[str, tuple[float, dict, str]] = {}     # cik -> (fetched_at, the slimmed facts, newest filed date)

    async def submissions(self, cik: str | int) -> tuple[dict, Provenance]:
        """Name, SIC, exchanges, fiscal year end and the recent filings. Cached an hour."""
        c = cik10(cik)
        sub, at = await self.get(f"/submissions/CIK{c}.json", ttl=HOUR, persist=True)
        if not isinstance(sub, dict):
            raise SourceError("data.sec.gov: submissions came back in an unexpected shape")
        newest = max(((sub.get("filings") or {}).get("recent") or {}).get("filingDate") or [""])
        return sub, self.prov(at, day_ts(newest) or at, DAILY, f"sec:{c}")

    async def facts(self, cik: str | int) -> tuple[dict, Provenance]:
        """XBRL company facts, slimmed to what the terminal reads. Cached a day. The raw file leaves memory once
        slimmed: a treasury list of twenty raw files would not fit the terminal's memory budget."""
        c = cik10(cik)
        path = f"/api/xbrl/companyfacts/CIK{c}.json"
        raw, at = await self.get(path, ttl=DAY, persist=True)
        if not isinstance(raw, dict):
            raise SourceError("data.sec.gov: company facts came back in an unexpected shape")
        hit = self._slim.get(c)
        if not hit or hit[0] != at:
            small = await asyncio.to_thread(slim, raw)
            hit = self._slim[c] = (at, small, _newest_filed(small))
        key = self._key(path, None)
        if key in self._mem:
            self._mem[key] = (*self._mem[key][:2], hit[1])  # memory keeps the slim copy; SQLite keeps the whole file
        return hit[1], self.prov(at, day_ts(hit[2]) or at, DAILY, f"sec:{c}")


def _newest_filed(facts: dict) -> str:
    return max((str(r.get("filed", "")) for tax in (facts.get("facts") or {}).values() for node in tax.values()
                for rows in (node.get("units") or {}).values() for r in rows), default="")


class SecWww(_Edgar):
    def __init__(self) -> None:
        super().__init__(name="www.sec.gov", base_url="https://www.sec.gov", delay=DAILY, rate=5.0, burst=5.0, retries=1,
                         serves="SEC company tickers and the filing archive (needs sec_user_agent)")
        self._tickers: tuple[float, dict[str, tuple[str, str, str]]] | None = None

    async def tickers(self) -> tuple[dict[str, tuple[str, str, str]], Provenance]:
        """{ticker: (cik, name, exchange)} for every SEC filer with a ticker. Cached a day."""
        raw, at = await self.get("/files/company_tickers.json", ttl=DAY, persist=True)
        if not isinstance(raw, dict):
            raise SourceError("www.sec.gov: company tickers came back in an unexpected shape")
        if not self._tickers or self._tickers[0] != at:
            self._tickers = (at, await asyncio.to_thread(parse_tickers, raw))
        return self._tickers[1], self.prov(at, at, DAILY, "sec:tickers")

    async def document(self, cik: str | int, accession: str, document: str = "") -> tuple[str, Provenance]:
        """One document from the archive as clean text. A filing never changes, so it is cached for thirty days, and
        both caches keep the clean text in place of the raw HTML."""
        path = archive_path(cik, accession, document)
        body, at = await self.get(path, ttl=30 * DAY, persist=True, text=True)
        if isinstance(body, str) and _LOOKS_HTML.search(body[:20000]):
            body = await asyncio.to_thread(clean_text, body)
            key = self._key(path, None)
            if key in self._mem:
                self._mem[key] = (*self._mem[key][:2], body)
                core._disk.put(key, *self._mem[key])
        return body, self.prov(at, at, DAILY, f"sec:{cik10(cik)}")


class SecSearch(_Edgar):
    def __init__(self) -> None:
        super().__init__(name="efts.sec.gov", base_url="https://efts.sec.gov", delay=DAILY, rate=5.0, burst=5.0, retries=1,
                         serves="SEC EDGAR full-text search, 2001 to today (needs sec_user_agent)")

    async def search(self, query: Query) -> tuple[list[Hit], int, Provenance]:
        """(hits, total matches). Cached ten minutes."""
        if not query.words.strip():
            raise SourceError("CFS needs words to search for: CFS bitcoin treasury form:8-K since:2026-01-01")
        raw, at = await self.get("/LATEST/search-index", query.params(datetime.now(UTC).strftime("%Y-%m-%d")), ttl=600.0)
        if not isinstance(raw, dict):
            raise SourceError("efts.sec.gov: the search came back in an unexpected shape")
        hits, total = parse_hits(raw)
        return hits, total, self.prov(at, at, DAILY, "sec:search")
