"""Instruments, their classes and the category lists (TERMINAL.md 4.4).

`data/instruments.toml` ships in the package; `~/.config/glimpse/instruments.toml` overrides or adds to it. Each
instrument names its sources in order (`pyth:<feed id>`, `fred:DGS10`, `coinbase:BTC-USD`). Every identifier in
the shipped file was read from the source's own catalogue in Phase 0 (SOURCES.md); none is invented.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from importlib import resources

from . import config
from .registry import CLASSES

TXID = re.compile(r"^[0-9a-fA-F]{64}$")
ADDRESS = re.compile(r"^(bc1[ac-hj-np-z02-9]{11,87}|tb1[ac-hj-np-z02-9]{11,87}|[13][1-9A-HJ-NP-Za-km-z]{25,39})$")


@dataclass(frozen=True)
class Instrument:
    ticker: str
    name: str
    cls: str                                # one of registry.CLASSES
    quote: str = "USD"
    decimals: int = 2
    session: str = "24x7"                   # 24x7 | 24x5 | us_equity | daily
    sources: tuple[str, ...] = ()
    proxy: str = ""                         # the ETF shown beside an index with no open live level, labelled as a proxy
    note: str = ""
    aliases: tuple[str, ...] = ()
    cik: str = ""                           # SEC filers

    @property
    def available(self) -> bool:
        return bool(self.sources)

    def source(self, kind: str) -> str | None:
        """The identifier for one kind of source: source("pyth") -> the feed id."""
        return next((s.split(":", 1)[1] for s in self.sources if s.startswith(kind + ":")), None)


@dataclass
class Book:
    """Every instrument the terminal knows, by ticker and alias, plus the category lists."""
    by_ticker: dict[str, Instrument] = field(default_factory=dict)
    alias: dict[str, str] = field(default_factory=dict)
    lists: dict[str, list[str]] = field(default_factory=dict)
    companies: dict[str, tuple[str, str, str]] = field(default_factory=dict)    # SEC ticker -> (cik, name, exchange)

    def add(self, ins: Instrument) -> None:
        self.by_ticker[ins.ticker.upper()] = ins
        for a in ins.aliases:
            self.alias[a.upper()] = ins.ticker.upper()

    def get(self, ticker: str) -> Instrument | None:
        t = ticker.upper()
        if hit := self.by_ticker.get(t) or self.by_ticker.get(self.alias.get(t, "")):
            return hit
        if co := self.companies.get(t):                 # any SEC filer is a security, with filings if not a price
            return Instrument(t, co[1], "EQUITY", session="us_equity", cik=co[0], note=co[2])
        return None

    def with_cik(self, ins: Instrument) -> Instrument:
        co = self.companies.get(ins.ticker.upper())
        return replace(ins, cik=co[0]) if co and not ins.cik else ins

    def list(self, name: str) -> list[Instrument]:
        return [i for t in self.lists.get(name.upper(), []) if (i := self.get(t))]

    def search(self, text: str, limit: int = 12) -> list[Instrument]:
        """Ticker prefix first, then words of the name. Curated instruments rank above the long tail of SEC filers."""
        q = text.upper()
        if not q:
            return []
        out = [i for t, i in self.by_ticker.items() if t.startswith(q)]
        out += [self.by_ticker[t] for a, t in self.alias.items() if a.startswith(q) and self.by_ticker[t] not in out]
        out += [i for i in self.by_ticker.values() if q in i.name.upper() and i not in out]
        seen = {i.ticker for i in out}
        if len(out) < limit:
            out += [self.get(t) for t in self.companies if t.startswith(q) and t not in seen][: limit - len(out)]
        if len(out) < limit and len(q) >= 3:
            seen = {i.ticker for i in out}
            out += [self.get(t) for t, co in self.companies.items() if q in co[1].upper() and t not in seen][: limit - len(out)]
        return out[:limit]


def _parse(text: str, book: Book) -> None:
    doc = tomllib.loads(text)
    for row in doc.get("instrument", []):
        cls = str(row.get("class", "")).upper()
        if not row.get("ticker") or cls not in CLASSES:
            continue
        book.add(Instrument(
            ticker=str(row["ticker"]).upper(), name=str(row.get("name", row["ticker"])), cls=cls, quote=str(row.get("quote", "USD")),
            decimals=int(row.get("decimals", 2)), session=str(row.get("session", "24x7")), sources=tuple(row.get("sources", ())),
            proxy=str(row.get("proxy", "")).upper(), note=str(row.get("note", "")), aliases=tuple(row.get("aliases", ())),
            cik=str(row.get("cik", ""))))
    for name, tickers in doc.get("lists", {}).items():
        book.lists[name.upper()] = [str(t).upper() for t in tickers]


def load() -> Book:
    book = Book()
    try:
        _parse(resources.files("glimpse_tui.data").joinpath("instruments.toml").read_text(), book)
    except (OSError, tomllib.TOMLDecodeError, ModuleNotFoundError):
        pass
    try:
        _parse((config.config_dir() / "instruments.toml").read_text(), book)
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return book


def looks_like(token: str) -> str:
    """'txid', 'address', 'height' or '' for a bare token typed into the GO bar."""
    if TXID.match(token):
        return "txid"
    if ADDRESS.match(token):
        return "address"
    if token.replace(",", "").isdigit() and int(token.replace(",", "")) < 10_000_000:
        return "height"
    return ""
