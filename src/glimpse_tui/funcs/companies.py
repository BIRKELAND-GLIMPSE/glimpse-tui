"""DES, FA, CF, CFS, TRSY, MINR, ETF, N and TOP: companies, their filings, and who holds Bitcoin (TERMINAL.md 9).

Everything here reads SEC EDGAR through `data/sec.py`, the curated lists in `data/treasuries.toml`, `miners.toml`
and `etfs.toml`, and the hub's quote board. Three rules shape every page (PLAN D38):

1. The SEC wants a contact on every request. Until `sec_user_agent` is set nothing is sent to the SEC, and the pane
   says why, what the SEC receives, and how to set it. A 403 is shown as a refused User-Agent and is not asked again.
2. A Bitcoin holding is never assumed. It is found in the company's own XBRL facts at run time, and the page shows
   the element's name, the filing and the date beside the number. A curated manual entry shows its URL instead.
3. Most equities have no open quote without a key (PLAN D36). A price is shown through a labelled proxy where one
   exists (`MSTR via MSTRx · proxy · live`), and otherwise the page says "no open quote" and leaves the cell empty.
"""
from __future__ import annotations

import asyncio
import re
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any

from rich.text import Text

from .. import charts, fmt
from ..data import quotes, sec
from ..data.core import Provenance, SourceError
from ..term import config, ui
from ..term.panes import INK, FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, RULE, TEXT

if TYPE_CHECKING:
    from ..term.hub import Hub, Quote

DASH = "–"
NO_QUOTE = "no open quote"
NO_CONTACT = "no SEC contact set"
FLOWS = "No issuer's terms allow automated reading of its holdings file (SOURCES.md), so flows are not shown."
JEV = "Headlines arrive with the Jev news hub (JEV_STARS.md), which is not built in this repo. Until then: filings as they land."
BLANK = Text("")


# ── the SEC contact ─────────────────────────────────────────

def contact(hub: Hub) -> str:
    return str(hub.cfg.get("sec_user_agent") or "").strip()


def has_contact(hub: Hub) -> bool:
    return sec.contact_ok(contact(hub))


def sync_contact(hub: Hub) -> None:
    """The configured contact onto the three SEC hosts, including an empty one: clearing the setting stops requests."""
    ua = contact(hub) if has_contact(hub) else ""
    for key in ("sec", "sec_www", "sec_efts"):
        if key in hub.sources:
            hub.sources[key].user_agent = ua


def wrap(text: str, w: int, style: str = DIM, indent: str = "") -> list[Text]:
    """`ui.wrap`, and a word longer than the pane (a URL, an XBRL concept name) breaks instead of running off the edge."""
    room = max(w - len(indent), 8)
    words = [word[i:i + room] for word in text.split(" ") for i in range(0, max(len(word), 1), room)]
    return ui.wrap(" ".join(words), w, style, indent)


def set_command(w: int) -> list[Text]:
    """The command to type, whole at any width: a narrow pane breaks it after the setting's name."""
    if w >= len(sec.HOW_TO_SET) + 2:
        return [ui.t(("  ", ""), (sec.HOW_TO_SET, f"bold {ORANGE}"))]
    head, _, tail = sec.HOW_TO_SET.partition(" Your")
    return [ui.t((head, f"bold {ORANGE}")), ui.t((f"  Your{tail}", f"bold {ORANGE}"))]


def contact_lines(w: int, brief: bool = False) -> list[Text]:
    """Why the page is empty, what the SEC receives, and how to set it. `brief` is the form under a table."""
    cmd = set_command(w)
    if brief:
        return [ui.section("SEC FILINGS ARE OFF", w), *wrap(
            "The SEC requires a contact in the User-Agent of every request, and the SEC receives it. Nothing is sent until you set it:", w, DIM),
            *cmd]
    out = [ui.section("THE SEC NEEDS A CONTACT FIRST", w), Text("")]
    out += wrap("The SEC requires every automated request to carry a User-Agent that names you and gives a contact email. "
                   "It refuses any request without one.", w, DIM)
    out += [Text("")] + wrap("The SEC receives that text on every request this terminal makes to sec.gov. Nobody else does.", w, DIM)
    out += [Text(""), ui.note("Set it once, in the GO bar:", DIM), Text(""), *cmd, Text("")]
    out += wrap("Nothing is sent to the SEC until it is set. It is saved in terminal.toml and takes effect at once, with no restart. "
                   "Your name and email replace the example.", w, DIM)
    return out


def refused_lines(w: int, hub: Hub) -> list[Text]:
    out = [ui.section("THE SEC REFUSED THE USER-AGENT", w), Text("")]
    out += wrap(f"The SEC refused the User-Agent it was sent, with HTTP 403: {contact(hub) or '(empty)'}", w, RED)
    out += [Text("")] + wrap("It wants a name and a contact email. The terminal does not ask again until the setting changes, so a refusal "
                                "never turns into a loop. Set a different one in the GO bar:", w, DIM)
    return out + [Text(""), *set_command(w)]


# ── the curated lists, tickers and CIKs ─────────────────────

@lru_cache(maxsize=16)
def _read_list(name: str, user_dir: str) -> tuple[dict, ...]:
    text = ""
    try:
        text = (config.config_dir() / f"{name}.toml").read_text()      # the user's file replaces the shipped one
    except OSError:
        try:
            text = resources.files("glimpse_tui.data").joinpath(f"{name}.toml").read_text()
        except (OSError, ModuleNotFoundError):
            return ()
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ()
    return tuple(row for rows in doc.values() if isinstance(rows, list) for row in rows if isinstance(row, dict) and row.get("ticker"))


def curated(name: str) -> list[dict]:
    """treasuries, miners or etfs: the rows of the curated file, in file order."""
    return list(_read_list(name, str(config.config_dir())))


async def fill_companies(hub: Hub) -> None:
    """company_tickers.json into `hub.book.companies`, so every SEC filer is a security the GO bar can resolve."""
    if not has_contact(hub) or "sec_www" not in hub.sources:
        return
    table, _ = await hub.sources["sec_www"].tickers()
    if len(hub.book.companies) != len(table):
        hub.book.companies.clear()
        hub.book.companies.update(table)


async def try_fill_companies(hub: Hub) -> str:
    """'' or the reason the ticker file could not be read. A refusal is raised: nothing else will work either."""
    try:
        await fill_companies(hub)
    except SourceError as e:
        if sec.is_refusal(e):
            raise
        return str(e)
    return ""


def cik_for(hub: Hub, ticker: str, entry_cik: str = "") -> tuple[str, str]:
    """(cik, where it came from). The SEC's own ticker file first; a curated CIK is used but called unconfirmed."""
    t = ticker.upper()
    if co := hub.book.companies.get(t):
        return co[0], "company_tickers.json"
    ins = hub.book.by_ticker.get(t)
    if ins and ins.cik:
        return ins.cik, "instruments.toml"
    if not entry_cik:
        entry_cik = next((str(r.get("cik")) for name in ("treasuries", "miners", "etfs") for r in curated(name)
                          if str(r.get("ticker", "")).upper() == t and r.get("cik")), "")
    return (entry_cik, "curated list, not confirmed against the SEC") if entry_cik else ("", "")


# ── prices ──────────────────────────────────────────────────

def quote_of(hub: Hub, ticker: str) -> Quote | None:
    """The quote board's entry for a share or a fund. `BTC` the Grayscale trust must never read Bitcoin's price."""
    ins = hub.book.by_ticker.get(ticker.upper())
    if ins is None or ins.cls not in ("EQUITY", "ETF"):
        return None
    return hub.quotes.get(ins.ticker)


def via_name(hub: Hub, via: str) -> str:
    """The stand-in as its venue spells it: the book upper-cases tickers, Kraken writes `MSTRx`."""
    ins = hub.book.by_ticker.get(via.upper())
    pair = ins.source("kraken") if ins else None
    return pair[:-3] if pair and pair.upper().startswith(via.upper()) and len(pair) > 3 else via


def quote_label(hub: Hub, q: Quote | None) -> str:
    if q is None:
        return NO_QUOTE
    return f"via {via_name(hub, q.via)} · proxy · {q.prov.delay}" if q.proxy_for else f"{q.prov.source} · {q.prov.delay}"


def cite(p: sec.Point) -> str:
    """`10-Q 0001050446-26-000080 filed 04 Aug 2026`: the filing a number came from."""
    return f"{p.form} {p.accn} filed {day(p.filed)}".strip()


async def want_quotes(hub: Hub, tickers: list[str]) -> None:
    """Ask the hub to keep these fresh, and fetch the ones it has never seen so the first paint has prices."""
    have = [t.upper() for t in tickers if (i := hub.book.by_ticker.get(t.upper())) and i.cls in ("EQUITY", "ETF") and (i.sources or i.proxy)]
    hub.watch.update(have)
    missing = [t for t in [*have, "BTC"] if t not in hub.quotes]
    if missing:
        try:
            await quotes.refresh(hub, missing)
        except Exception:                                   # the tape's own loop will try again
            pass


async def closes(hub: Hub, ticker: str, days: int = 31) -> list[float]:
    ins = hub.book.by_ticker.get(ticker.upper())
    if not ins or not (ins.sources or ins.proxy):
        return []
    try:
        _, vals, _ = await quotes.history(hub, ins, days)
    except (SourceError, KeyError):
        return []
    return [float(v) for v in vals[-days:] if v]


def change(vals: list[float]) -> float | None:
    return vals[-1] / vals[0] - 1 if len(vals) >= 2 and vals[0] else None


# ── small formatters ────────────────────────────────────────

def day(iso: str, year: bool = True) -> str:
    """2026-06-30 as `30 Jun 2026`, or `30 Jun` in a narrow column."""
    try:
        d = datetime.fromisoformat(iso[:10])
    except ValueError:
        return iso or DASH
    return d.strftime("%d %b %Y" if year else "%d %b")


def age(f: sec.Filing, now: float, short: bool = False) -> str:
    """`12m` when the feed carries the acceptance time; with a bare date, `today` (`<1d` in a narrow column), `1d`, `12d`."""
    if f.accepted:
        return ui.when(f.at, now)
    days = int((now - sec.day_ts(f.filed)) // 86400)
    return ("<1d" if short else "today") if days <= 0 else f"{days}d"


def millions(p: sec.Point | None, unit: str = "USD") -> Text:
    if p is None:
        return Text(DASH, style=FAINT)
    v = p.val if unit == "USD/shares" else p.val / 1e6
    s = f"{fmt.MINUS if v < 0 else ''}{abs(v):,.2f}" if unit == "USD/shares" else f"{fmt.MINUS if v < 0 else ''}{abs(v):,.1f}"
    return Text(s, style=RED if v < 0 else TEXT)


def filing_words(f: sec.Filing) -> str:
    """What a filing is about, from what the index carries: an 8-K's items in words, else its description or period."""
    if f.items:
        return sec.item_words(f.items)
    desc = f.description if f.description and f.description.upper() != f.form.upper() else ""
    return desc or (f"period {day(f.report)}" if f.report else "")


def flow(parts: list[Text], w: int, sep: str = "   ") -> list[Text]:
    """Parts side by side while they fit `w`, then on the next line: one row at 110 columns, three at 36."""
    out, cur = [], Text(no_wrap=True, overflow="crop")
    for p in parts:
        if not p.cell_len:
            continue
        if cur.cell_len and cur.cell_len + len(sep) + p.cell_len > w:
            out.append(cur)
            cur = Text(no_wrap=True, overflow="crop")
        if cur.cell_len:
            cur.append(sep)
        cur.append_text(p)
    return out + ([cur] if cur.cell_len else [])


def fit_table(cols: list[ui.Col], rows: list[list[Any]], w: int, drop: tuple[str, ...], cursor: int | None = None,
              flex_min: int = 18) -> list[Text]:
    """`ui.table` after dropping columns, in `drop` order, until the rest fits `w` with the flex column still at least
    `flex_min` wide. Never wraps."""
    keep = list(range(len(cols)))

    def need() -> int:
        total = 0
        for i in keep:
            cells = [r[i].cell_len if isinstance(r[i], Text) else len(str(r[i] or "")) for r in rows]
            width = cols[i].width or max([len(cols[i].head), *cells])
            total += min(width, flex_min) if cols[i].flex else width
        return total + 2 * (len(keep) - 1)

    for head in drop:
        if need() <= w:
            break
        keep = [i for i in keep if cols[i].head != head]
    return ui.table([cols[i] for i in keep], [[r[i] for i in keep] for r in rows], w, cursor)


# ── one company's SEC inputs ────────────────────────────────

@dataclass
class Holding:
    btc: float | None = None
    as_of: str = ""
    element: str = ""           # taxonomy:Concept, or "manual entry"
    cite: str = ""              # the filing, or the manual entry's URL
    accn: str = ""
    why: str = ""               # why discovery chose the element
    fair_value: sec.Point | None = None
    missing: str = ""           # why there is no number


@dataclass
class Row:
    entry: dict
    ticker: str
    name: str
    cik: str = ""
    cik_from: str = ""
    shares: sec.Point | None = None
    holding: Holding = field(default_factory=Holding)
    hist: list[float] = field(default_factory=list)
    prov: Provenance | None = None
    note: str = ""              # why the SEC inputs are missing
    refused: bool = False

    @property
    def short(self) -> str:
        return str(self.entry.get("short") or self.name)


def holding_from(crypto: sec.Crypto) -> Holding:
    if u := crypto.units:
        p = u.point
        return Holding(p.val, p.end, p.concept, cite(p), p.accn, u.why, crypto.fair_value.point if crypto.fair_value else None)
    if fv := crypto.fair_value:
        return Holding(None, fv.point.end, fv.point.concept, cite(fv.point), fv.point.accn, fv.why, fv.point,
                       "its SEC facts give a fair value but no count of coins")
    return Holding(missing="no crypto concept in its SEC facts")


async def load_row(hub: Hub, entry: dict) -> Row:
    """Everything TRSY, MINR and ETF need about one curated company. Never raises: a missing input is a reason."""
    ticker = str(entry.get("ticker", "")).upper()
    row = Row(entry, ticker, str(entry.get("name") or ticker))
    if entry.get("holdings") == "manual":
        btc = float(entry.get("btc") or 0)
        if btc > 0 and entry.get("source_url"):
            row.holding = Holding(btc, str(entry.get("as_of", "")), "manual entry", str(entry["source_url"]),
                                  why="read from the company's own page")
        else:
            row.holding = Holding(missing="no sourced figure yet: the curated entry is unverified")
        row.note = "not an SEC filer: no shares outstanding from an open source"
        return row
    if not has_contact(hub):
        row.holding, row.note = Holding(missing=NO_CONTACT), NO_CONTACT
        return row
    row.cik, row.cik_from = cik_for(hub, ticker, str(entry.get("cik") or ""))
    if not row.cik:
        row.note = "no CIK: the ticker is not in the SEC's ticker file"
        row.holding = Holding(missing=row.note)
        return row
    try:
        facts, row.prov = await hub.sources["sec"].facts(row.cik)
    except SourceError as e:
        row.refused = sec.is_refusal(e)
        row.note = "the SEC refused the User-Agent" if row.refused else f"SEC facts not loaded ({str(e)[:60]})"
        row.holding = Holding(missing=row.note)
        return row
    crypto = await asyncio.to_thread(sec.discover_crypto, facts, str(entry.get("xbrl_element") or ""))
    row.holding, row.shares = holding_from(crypto), sec.shares_outstanding(facts)
    if row.shares is None:
        row.note = "no dei shares outstanding in its SEC facts"
    return row


async def load_rows(pane: ListPane, entries: list[dict]) -> None:
    """Fill `pane.rows` in three steps, painting after each: the SEC inputs, then prices, then the 30-day histories.
    The slow part (one Kraken request a second for each history) never holds the table back."""
    hub, old = pane.hub, {r.ticker: r.hist for r in pane.rows}
    pane.rows = pane.order(list(await asyncio.gather(*[load_row(hub, e) for e in entries])))
    for r in pane.rows:
        r.hist = old.get(r.ticker, [])                      # an hourly reload keeps the sparkline it had until the new one arrives
    pane.n_rows = len(pane.rows)
    pane.bump()
    await want_quotes(hub, [r.ticker for r in pane.rows])
    pane.bump()
    for r in pane.rows:                                     # only the few with an open source or a proxy ask anything
        r.hist = await closes(hub, r.ticker)


@dataclass
class Metrics:
    price: float | None
    label: str
    cap: float | None
    value: float | None
    mnav: float | None
    sats_per_share: float | None
    why: list[str]


def metrics(hub: Hub, r: Row) -> Metrics:
    """mNAV and its inputs for one row, from cached state only. Market cap is dei shares outstanding x price. The
    Bitcoin is valued at the terminal's composite BTC price now, not at the fair value in the filing."""
    q = quote_of(hub, r.ticker)
    price, px = (q.price if q else None), hub.btc_price()
    shares, btc = (r.shares.val if r.shares else None), r.holding.btc
    why = [x for x in (r.holding.missing if btc is None else "", NO_QUOTE if price is None else "",
                       r.note if shares is None and r.note != r.holding.missing else "") if x]
    return Metrics(price, quote_label(hub, q), shares * price if shares and price else None, btc * px if btc and px else None,
                   sec.mnav(shares, price, btc, px), btc / shares * 1e8 if btc and shares else None, list(dict.fromkeys(why)))


def provs_of(hub: Hub, rows: list[Row]) -> list[Provenance]:
    out = [r.prov for r in rows if r.prov]
    out += [q.prov for r in rows if (q := quote_of(hub, r.ticker))]
    return list({(p.source, p.delay): p for p in out}.values())


def framed(pane: FuncPane, w: int, now: float, label: str, base) -> Text:
    """The base's frame with the provenance spelled out by the page (TERMINAL.md's TRSY mockup: `holdings: filings ·
    prices: pyth · delayed`). An error, a stale source, or a pane too narrow for the label gets the base's own frame."""
    if pane.error or not label or any(p.stale(now) for p in pane.provs):
        return base(w, now)
    edge = ORANGE if pane.active else RULE
    out = Text(no_wrap=True, overflow="crop")
    out.append("┌", style=edge)
    out.append(f"{pane.number} ", style=f"bold {ORANGE if pane.active else DIM}")
    out.append(f" {pane.code} ", style=f"bold {INK} on {ORANGE}" if pane.active else f"bold {ORANGE} on #2b1500")
    out.append(f" {pane.title} ", style=f"bold {TEXT}")
    room = w - out.cell_len - len(label) - 3
    if room < 1:
        return base(w, now)
    out.append("─" * room, style=edge)
    out.append(f" {label} ", style=DIM)
    out.append("┐", style=edge)
    return out


class ListPane(FuncPane):
    """What TRSY, MINR and ETF share: curated rows, a cursor, and a header that names holdings and prices apart."""
    every, selectable = 3600, True
    list_name = ""

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.rows: list[Row] = []
        self.go = ""

    def order(self, rows: list[Row]) -> list[Row]:
        return rows

    async def load(self) -> None:
        sync_contact(self.hub)
        entries = await asyncio.to_thread(curated, self.list_name)
        if not entries:
            raise SourceError(f"{self.list_name}.toml could not be read")
        try:
            await try_fill_companies(self.hub)
        except SourceError:
            pass                                            # a refusal: every row will say so
        await load_rows(self, entries)

    def prov_label(self) -> str:
        names = list(dict.fromkeys(q.prov.source for r in self.rows if (q := quote_of(self.hub, r.ticker))))
        delays = list(dict.fromkeys(q.prov.delay for r in self.rows if (q := quote_of(self.hub, r.ticker))))
        prices = f"{' + '.join(names[:2])} · {'/'.join(delays[:2])}" if names else NO_QUOTE
        return f"holdings: filings · prices: {prices}"

    def frame_top(self, w: int, now: float) -> Text:
        return framed(self, w, now, self.prov_label() if self.rows else "", super().frame_top)

    def row(self) -> Row | None:
        return self.rows[self.cur] if 0 <= self.cur < len(self.rows) else None

    def enter(self) -> str | None:
        r = self.row()
        if r and r.holding.accn and r.cik:
            return f"CF {r.ticker} {r.holding.accn}"
        return f"DES {r.ticker}" if r and r.cik else None

    def key(self, k: str, ch: str | None) -> bool:
        if ch == "D" and (r := self.row()):
            return run_go(self, f"DES {r.ticker}")
        return False

    def detail(self, r: Row, m: Metrics, w: int) -> list[Text]:
        """The cursor row's inputs, each with its as-of date: where the holding, the shares and the price came from."""
        h = r.holding
        out = [ui.section(f"{r.ticker}  {r.short}"[:max(w - 4, 8)], w)]
        if h.btc is not None:
            src = f"{h.element} ({h.why}) · {h.cite}" if h.element != "manual entry" else f"manual entry · {h.cite} · {h.why}"
            out += wrap(f"holding  {h.btc:,.0f} BTC as of {day(h.as_of)} · {src}", w, DIM, "")
        else:
            out += wrap(f"holding  {DASH} · {h.missing}", w, DIM)
        if h.fair_value is not None:
            out += wrap(f"filed fair value  {ui.usd(h.fair_value.val)} as of {day(h.fair_value.end)} · {h.fair_value.concept} · "
                           f"{cite(h.fair_value)}", w, FAINT)
        if r.shares is not None:
            extra = f" · {r.shares.derived}" if r.shares.derived else ""
            out += wrap(f"shares   {ui.big(r.shares.val)} as of {day(r.shares.end)} · dei:EntityCommonStockSharesOutstanding · "
                           f"{cite(r.shares)}{extra}", w, DIM)
        elif r.note:
            out += wrap(f"shares   {DASH} · {r.note}", w, DIM)
        px = self.hub.btc_price()
        out += wrap(f"price    {ui.px(m.price) if m.price is not None else DASH} · {m.label} · "
                       f"BTC {ui.px(px, 0) if px else DASH} composite, live", w, DIM)
        if r.cik:
            out.append(ui.note(f"CIK {r.cik} · {r.cik_from}"))
        if note := str(r.entry.get("note") or ""):
            out += wrap(f"curated note: {note}", w, FAINT)
        return out


def run_go(pane: FuncPane, command: str) -> bool:
    """Run a GO bar command from a page key. Off screen (tests) the command is only recorded on `pane.go`."""
    pane.go = command                                       # type: ignore[attr-defined]
    try:
        pane.app.shell.run(command)                         # type: ignore[attr-defined]
    except Exception:
        pass
    return True


# ── TRSY ────────────────────────────────────────────────────

class TrsyPane(ListPane):
    code, list_name = "TRSY", "treasuries"

    def order(self, rows: list[Row]) -> list[Row]:
        return sorted(rows, key=lambda r: -(r.holding.btc or 0))       # stable: unknown holdings keep the file's order

    async def load(self) -> None:
        self.title = "Bitcoin treasury companies"
        await super().load()

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.rows:
            return []
        hub = self.hub
        self.provs = provs_of(hub, self.rows)
        ms = [metrics(hub, r) for r in self.rows]
        body = []
        for i, (r, m) in enumerate(zip(self.rows, ms, strict=True)):
            hd = r.holding
            body.append([
                str(i + 1), r.short, r.ticker, Text(f"{hd.btc:,.0f}", style=f"bold {TEXT}") if hd.btc is not None else Text(DASH, style=FAINT),
                day(hd.as_of, year=False) if hd.as_of else DASH, (hd.element.split(":")[-1] if hd.btc is not None or hd.element else DASH)[:30],
                Text(ui.usd(m.value), style=ORANGE) if m.value else Text(DASH, style=FAINT), ui.usd(m.cap) if m.cap else Text(DASH, style=FAINT),
                Text(f"{m.mnav:.2f}", style=f"bold {TEXT}") if m.mnav else Text(DASH, style=FAINT),
                fmt.sats(m.sats_per_share) if m.sats_per_share else Text(DASH, style=FAINT),
                Text(charts.spark(r.hist, 8), style=GREEN if (change(r.hist) or 0) >= 0 else RED) if r.hist else Text(DASH, style=FAINT),
                Text("; ".join(m.why), style=FAINT)])
        cols = [ui.Col("#", style=FAINT), ui.Col("company", "left", flex=True), ui.Col("ticker", "left", style=f"bold {ORANGE}"),
                ui.Col("BTC held"),
                ui.Col("as of", style=DIM), ui.Col("source", "left", style=FAINT), ui.Col("BTC value"), ui.Col("mkt cap"), ui.Col("mNAV"),
                ui.Col("₿/share", style=DIM), ui.Col("30d", "left"), ui.Col("why a dash", "left", width=24)]
        if w >= 150:                                            # room to spare: the reasons get it, not the names
            cols[1], cols[-1] = ui.Col("company", "left", width=22), ui.Col("why a dash", "left", flex=True)
        out = fit_table(cols, body, w, ("why a dash", "source", "₿/share", "30d", "#", "as of", "mkt cap", "company", "BTC value"), self.cur)
        self.follow(2 + self.cur, max(h - 1, 1))
        out.append(Text(""))
        out += wrap("enter opens the filing behind the holding · D opens DES · each row shows its own as-of date", w, FAINT) + [Text("")]
        if (r := self.row()) and w >= 50:
            out += self.detail(r, ms[self.cur], w)
            out.append(Text(""))
        if not has_contact(hub):
            out += contact_lines(w, brief=True) + [Text("")]
        elif any(r.refused for r in self.rows):
            out += refused_lines(w, hub) + [Text("")]
        out += wrap("mNAV = market cap / value of the Bitcoin held. Market cap = dei shares outstanding x price. BTC value = BTC held x the "
                       "composite BTC price now. Holdings are quarter-end figures from filings and lag any purchase since. "
                       "A dash always has a reason: move the cursor to the row.", w, FAINT)
        return out

    def hint(self) -> str:
        return "enter filing · D DES"

    def menu(self) -> list[tuple[str, str]]:
        return [("MINR", "MINR"), ("ETF", "ETF"), ("N", "N"), ("BTC", "BTC")]

    def export(self):
        head = ["rank", "company", "ticker", "cik", "btc_held", "holding_as_of", "holding_source", "holding_filing_or_url", "btc_value_usd",
                "shares_outstanding", "shares_as_of", "price", "price_source", "market_cap_usd", "mnav", "sats_per_share", "missing"]
        rows = []
        for i, r in enumerate(self.rows):
            m = metrics(self.hub, r)
            rows.append([i + 1, r.name, r.ticker, r.cik, r.holding.btc, r.holding.as_of, r.holding.element, r.holding.cite, m.value,
                         r.shares.val if r.shares else None, r.shares.end if r.shares else "", m.price, m.label, m.cap, m.mnav,
                         m.sats_per_share, "; ".join(m.why)])
        return head, rows


# ── MINR ────────────────────────────────────────────────────

class MinrPane(ListPane):
    code, list_name = "MINR", "miners"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.btc_hist: list[float] = []

    async def load(self) -> None:
        self.title = "public Bitcoin miners"
        await super().load()
        if any(r.hist for r in self.rows) and (btc := self.hub.book.by_ticker.get("BTC")):
            try:
                _, vals, _ = await quotes.history(self.hub, btc, 31)
                self.btc_hist = [float(v) for v in vals[-31:] if v]
            except SourceError:
                self.btc_hist = []

    def versus_btc(self, r: Row) -> float | None:
        """The share's 30-day return over Bitcoin's: (1 + share) / (1 + BTC) - 1. None without both histories."""
        a, b = change(r.hist[-31:]), change(self.btc_hist[-31:])
        return (1 + a) / (1 + b) - 1 if a is not None and b is not None and b > -1 else None

    @staticmethod
    def hashrate(r: Row) -> tuple[float, str, str] | None:
        e = r.entry
        eh = float(e.get("hashrate_eh") or 0)
        return (eh, str(e.get("hashrate_as_of", "")), str(e["hashrate_url"])) if eh > 0 and e.get("hashrate_url") else None

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.rows:
            return []
        hub = self.hub
        self.provs = provs_of(hub, self.rows)
        ms = [metrics(hub, r) for r in self.rows]
        body = []
        for r, m in zip(self.rows, ms, strict=True):
            q, hd, hr = quote_of(hub, r.ticker), r.holding, self.hashrate(r)
            body.append([
                r.short, r.ticker, Text(ui.px(m.price), style=f"bold {TEXT}") if m.price is not None else Text(DASH, style=FAINT),
                ui.signed_pct(q.pct) if q else Text(DASH, style=FAINT),
                (f"via {via_name(hub, q.via)}" if q and q.proxy_for else q.prov.source if q else "none"),
                ui.usd(m.cap) if m.cap else Text(DASH, style=FAINT),
                Text(f"{hd.btc:,.0f}", style=f"bold {TEXT}") if hd.btc is not None else Text(DASH, style=FAINT),
                day(hd.as_of, year=False) if hd.as_of else DASH, f"{hr[0]:,.1f}" if hr else Text(DASH, style=FAINT),
                ui.signed_pct(self.versus_btc(r)), str(r.entry.get("filer_type", "")), Text("; ".join(m.why), style=FAINT)])
        cols = [ui.Col("company", "left", flex=True), ui.Col("ticker", "left", style=f"bold {ORANGE}"), ui.Col("price"), ui.Col("day"),
                ui.Col("quote", "left", style=FAINT), ui.Col("mkt cap"), ui.Col("BTC held"), ui.Col("as of", style=DIM), ui.Col("EH/s"),
                ui.Col("vs BTC 30d"), ui.Col("filer", "left", style=FAINT), ui.Col("why a dash", "left", width=26)]
        out = fit_table(cols, body, w, ("why a dash", "filer", "quote", "day", "as of", "vs BTC 30d", "EH/s", "mkt cap", "company"), self.cur)
        self.follow(2 + self.cur, max(h - 1, 1))
        out.append(Text(""))
        if (r := self.row()) and w >= 50:
            out += self.detail(r, ms[self.cur], w)
            hr = self.hashrate(r)
            out += wrap(f"hashrate {hr[0]:,.1f} EH/s self-reported as of {day(hr[1])} · {hr[2]}" if hr else
                           f"hashrate {DASH} · self-reported in monthly updates; no sourced entry in miners.toml yet", w, DIM)
            out.append(Text(""))
        if not has_contact(hub):
            out += contact_lines(w, brief=True) + [Text("")]
        elif any(r.refused for r in self.rows):
            out += refused_lines(w, hub) + [Text("")]
        out += wrap("Hashrate is what each company says in its monthly update, in EH/s, and only when miners.toml holds the figure with its "
                       "source. vs BTC 30d is the share's 30-day return over Bitcoin's. Most miners have no open quote without a key.", w, FAINT)
        return out

    def hint(self) -> str:
        return "enter filing · D DES"

    def menu(self) -> list[tuple[str, str]]:
        return [("TRSY", "TRSY"), ("MINE", "MINE"), ("ETF", "ETF"), ("N", "N")]

    def export(self):
        head = ["company", "ticker", "cik", "filer_type", "price", "price_source", "market_cap_usd", "btc_held", "holding_as_of",
                "holding_source", "holding_filing", "hashrate_eh", "hashrate_as_of", "hashrate_url", "vs_btc_30d", "missing"]
        rows = []
        for r in self.rows:
            m, hr = metrics(self.hub, r), self.hashrate(r) or (None, "", "")
            rows.append([r.name, r.ticker, r.cik, r.entry.get("filer_type", ""), m.price, m.label, m.cap, r.holding.btc, r.holding.as_of,
                         r.holding.element, r.holding.cite, *hr, self.versus_btc(r), "; ".join(m.why)])
        return head, rows


# ── ETF ─────────────────────────────────────────────────────

class EtfPane(ListPane):
    code, list_name = "ETF", "etfs"

    async def load(self) -> None:
        self.title = "US spot Bitcoin ETFs"
        await super().load()

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.rows:
            return []
        hub = self.hub
        self.provs = provs_of(hub, self.rows)
        ms = [metrics(hub, r) for r in self.rows]
        body = []
        for r, m in zip(self.rows, ms, strict=True):
            hd = r.holding
            body.append([
                r.ticker, r.name, str(r.entry.get("issuer", "")), Text(ui.px(m.price), style=f"bold {TEXT}") if m.price is not None
                else Text(DASH, style=FAINT), Text(DASH, style=FAINT), Text(DASH, style=FAINT),
                Text(f"{hd.btc:,.0f}", style=f"bold {TEXT}") if hd.btc is not None else Text(DASH, style=FAINT),
                day(hd.as_of, year=False) if hd.as_of else DASH, Text(ui.usd(m.value), style=ORANGE) if m.value else Text(DASH, style=FAINT),
                str(r.entry.get("issuer_file", "")), Text("; ".join(m.why), style=FAINT)])
        cols = [ui.Col("ticker", "left", style=f"bold {ORANGE}"), ui.Col("fund", "left", flex=True), ui.Col("issuer", "left", style=DIM),
                ui.Col("price"), ui.Col("volume"), ui.Col("prem/disc"), ui.Col("BTC held"), ui.Col("as of", style=DIM), ui.Col("BTC value"),
                ui.Col("issuer file", "left", style=FAINT), ui.Col("why a dash", "left", width=26)]
        out = fit_table(cols, body, w, ("why a dash", "issuer file", "BTC value", "as of", "issuer", "volume", "prem/disc", "fund"), self.cur)
        self.follow(2 + self.cur, max(h - 1, 1))
        out.append(Text(""))
        out += wrap(FLOWS, w, f"bold {TEXT}")
        out += wrap("Premium or discount needs the fund's daily NAV. The only publishers are the issuers, and none is cleared, so it is not "
                       "shown. Price and volume need an open quote: US-listed ETFs have none without a key (PLAN D36), and there is no "
                       "tokenised proxy for any of them.", w, DIM)
        out.append(Text(""))
        if (r := self.row()) and w >= 50:
            out += self.detail(r, ms[self.cur], w)
            terms = str(r.entry.get("terms_url") or "")
            out += wrap(f"issuer's holdings file: {r.entry.get('issuer_file', 'unclear')}" + (f" · terms {terms}" if terms else ""), w, FAINT)
            out.append(Text(""))
        if not has_contact(hub):
            out += contact_lines(w, brief=True)
        elif any(r.refused for r in self.rows):
            out += refused_lines(w, hub)
        return out

    def hint(self) -> str:
        return "enter filing · D DES"

    def menu(self) -> list[tuple[str, str]]:
        return [("TRSY", "TRSY"), ("MINR", "MINR"), ("N", "N"), ("BTC", "BTC")]

    def export(self):
        head = ["ticker", "fund", "issuer", "cik", "price", "price_source", "btc_held", "holding_as_of", "holding_source", "holding_filing",
                "issuer_file", "terms_url", "missing"]
        rows = []
        for r in self.rows:
            m = metrics(self.hub, r)
            rows.append([r.ticker, r.name, r.entry.get("issuer", ""), r.cik, m.price, m.label, r.holding.btc, r.holding.as_of, r.holding.element,
                         r.holding.cite, r.entry.get("issuer_file", ""), r.entry.get("terms_url", ""), "; ".join(m.why)])
        return head, rows


# ── one company: what DES, FA, CF and N share ───────────────

class CompanyPane(FuncPane):
    every = 3600

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.state = ""                 # "" | contact | refused
        self.cik, self.cik_from = "", ""
        self.go = ""

    @property
    def ticker(self) -> str:
        return self.security.ticker if self.security else ""

    async def begin(self) -> bool:
        """The checks every company page starts with. False: the page has its explanation to draw and nothing to load."""
        sync_contact(self.hub)
        if not has_contact(self.hub):
            self.state, self.provs = "contact", []
            return False
        try:
            await try_fill_companies(self.hub)
        except SourceError:
            self.state = "refused"
            return False
        self.state = ""
        if self.security:
            self.cik, self.cik_from = cik_for(self.hub, self.ticker, self.security.cik)
            if not self.cik:
                raise SourceError(f"{self.ticker}: no CIK. It is not in the SEC's ticker file or the curated lists.")
        return True

    def failed(self, e: SourceError) -> None:
        """A refusal becomes the page; anything else goes to the frame through the base."""
        if sec.is_refusal(e):
            self.state = "refused"
            return
        raise e

    def blocked(self, w: int) -> list[Text] | None:
        if self.state == "contact":
            return contact_lines(w)
        if self.state == "refused":
            return refused_lines(w, self.hub)
        return None


# ── DES ─────────────────────────────────────────────────────

class DesPane(CompanyPane):
    code = "DES"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.sub: dict = {}
        self.filings: list[sec.Filing] = []
        self.keys: list[tuple[str, sec.Point | None, sec.Point | None]] = []
        self.row: Row | None = None
        self.seen: tuple[str, ...] = ()
        self.base: list[Provenance] = []

    async def load(self) -> None:
        self.title = f"{self.ticker} · {self.security.name}" if self.security else "description"
        await want_quotes(self.hub, [self.ticker])
        if not await self.begin():
            return
        src, provs, last = self.hub.sources["sec"], [], None
        try:
            self.sub, p = await src.submissions(self.cik)
            self.filings = sec.filings(self.sub, self.ticker, self.cik)
            provs.append(p)
        except SourceError as e:
            last = e
            if sec.is_refusal(e):
                self.state = "refused"
                return
        try:
            facts, p = await src.facts(self.cik)
            provs.append(p)
            entry = next((r for name in ("treasuries", "miners", "etfs") for r in curated(name)
                          if str(r.get("ticker", "")).upper() == self.ticker), {})
            crypto = await asyncio.to_thread(sec.discover_crypto, facts, str(entry.get("xbrl_element") or ""))
            self.keys = await asyncio.to_thread(sec.key_financials, facts)
            self.row = Row(entry, self.ticker, self.sub.get("name") or self.security.name, self.cik, self.cik_from,
                           sec.shares_outstanding(facts),
                           holding_from(crypto), prov=p)
            self.seen = crypto.seen
        except SourceError as e:
            last = e
            if sec.is_refusal(e):
                self.state = "refused"
                return
        if not provs and last:
            raise last                                      # neither file came: the frame says why, the last picture stays
        self.base = provs
        if self.sub.get("name"):
            self.title = f"{self.ticker} · {self.sub['name']}"

    def draw(self, w: int, h: int) -> list[Text]:
        if block := self.blocked(w):
            return block
        if not self.sub and not self.row:
            return []
        hub, sub, wide = self.hub, self.sub, w >= 72
        q = quote_of(hub, self.ticker)
        self.provs = self.base + ([q.prov] if q else [])
        fye = str(sub.get("fiscalYearEnd") or "")
        fye = day(f"2000-{fye[:2]}-{fye[2:]}", year=False) if len(fye) == 4 and fye.isdigit() else DASH
        out = [ui.t((str(sub.get("name") or (self.security.name if self.security else "")), f"bold {ORANGE}"),
                    (f"   {self.ticker} · {' / '.join(sub.get('exchanges') or []) or DASH}", DIM))]
        facts_ = [ui.kv("industry", f"{sub.get('sicDescription') or DASH}" + (f" (SIC {sub['sic']})" if sub.get("sic") and wide else "")),
                  ui.kv("fiscal year ends", fye), ui.kv("CIK", f"{self.cik}" + (f" · {self.cik_from}" if wide else ""), DIM)]
        out += [x for f in flow(facts_, w) for x in ([f] if f.cell_len <= w else wrap(f.plain, w, DIM))]
        r = self.row or Row({}, self.ticker, self.ticker)
        m = metrics(hub, r)
        out += [Text(""), ui.section("PRICE AND SIZE", w, "market cap = shares outstanding x price" if wide else "")]
        price = ui.t((ui.px(m.price) if m.price is not None else DASH, f"bold {ORANGE}" if m.price is not None else FAINT), ("  ", ""),
                     ui.signed_pct(q.pct, 2) if q and q.pct is not None else "")
        out += flow([price, ui.note(f"{self.ticker} {m.label}" if q else NO_QUOTE)], w)
        since = f"as of {day(r.shares.end)} · {r.shares.form}" + (f" · {r.shares.derived}" if r.shares.derived else "") if r.shares else ""
        out += flow([ui.kv("shares out", ui.big(r.shares.val) if r.shares else DASH), ui.note(since or r.note)], w)
        lacks = [x for x in ("a price" if m.price is None else "", "shares outstanding" if not r.shares else "") if x]
        out += flow([ui.kv("market cap", ui.usd(m.cap) if m.cap else DASH, f"bold {TEXT}"),
                     ui.note(f"needs {' and '.join(lacks)}" if lacks else "")], w)
        hd = r.holding
        if hd.btc is not None or hd.fair_value is not None or self.seen:
            out += [Text(""), ui.section("BITCOIN HELD", w, "found in its XBRL facts, not assumed" if wide else "")]
            if hd.btc is not None:
                px = hub.btc_price()
                out += flow([ui.kv("held", f"{hd.btc:,.0f} BTC", f"bold {ORANGE}"), ui.note(f"as of {day(hd.as_of)}", DIM),
                             ui.note(f"worth {ui.usd(m.value)} at {ui.px(px, 0)} now" if m.value else "", DIM)], w)
                out += wrap(f"{hd.element} ({hd.why}) · {hd.cite}", w, FAINT, "  ")
            else:
                out += wrap(f"held {DASH} · {hd.missing}", w, DIM)
            if fv := hd.fair_value:
                out += flow([ui.kv("fair value as filed", ui.usd(fv.val)), ui.note(f"as of {day(fv.end)}", DIM)], w)
                out += wrap(f"{fv.concept} · {cite(fv)}", w, FAINT, "  ")
            nav = [ui.kv("mNAV", f"{m.mnav:.2f}" if m.mnav else DASH, f"bold {TEXT}")]
            if m.mnav:
                nav += [ui.note("market cap / Bitcoin value"), ui.note(f"{fmt.sats(m.sats_per_share)} a share" if m.sats_per_share else "")]
            else:
                nav.append(ui.note(f"needs {', '.join(m.why) or 'a BTC price'}"))
            out += flow(nav, w)
            if m.mnav and r.shares:
                out += wrap(f"inputs: shares {day(r.shares.end)} · holding {day(hd.as_of)} · price {m.label} · BTC composite, live",
                               w, FAINT, "  ")
        if self.keys:
            yr = next((a.end for _, a, _ in self.keys if a), "")
            qt = next((b.end for _, _, b in self.keys if b), "")
            out += [Text(""), ui.section("KEY FINANCIALS", w, "USD millions")]
            to = "to " if w >= 48 else ""
            cols = [ui.Col("", "left", flex=True, style=DIM), ui.Col(f"FY {to}{day(yr, False)[0 if to else 3:]} {yr[2:4]}" if yr else "annual"),
                    ui.Col(f"Q {to}{day(qt, False)[0 if to else 3:]} {qt[2:4]}" if qt else "quarter")]
            rows = [[label, millions(a), millions(b)] for label, a, b in self.keys]
            if wide:
                cols.append(ui.Col("latest annual from", "left", style=FAINT))
                rows = [[*row, f"{a.concept.split(':')[-1][:34]} · {a.form} filed {day(a.filed)}" if a else ""]
                        for row, (_, a, _) in zip(rows, self.keys, strict=True)]
            out += fit_table(cols, rows, min(w, 118), ("latest annual from",), flex_min=10)
        if self.filings:
            out += [Text(""), ui.section("LATEST FILINGS", w, "CF lists them all")]
            room = max(h - len(out) - 2, 4)
            cols = [ui.Col("filed", "left", style=DIM), ui.Col("form", "left", style=f"bold {TEXT}"),
                    ui.Col("about", "left", flex=True, style=DIM)]
            out += ui.table(cols, [[day(f.filed, wide), f.form, filing_words(f)] for f in self.filings[:room]], w, rule=False)
        return out

    def menu(self) -> list[tuple[str, str]]:
        t = self.ticker
        return [("FA", f"FA {t}"), ("CF", f"CF {t}"), ("N", f"N {t}"), ("TRSY", "TRSY")]

    def hint(self) -> str:
        return "1 FA · 2 CF · 3 N · 4 TRSY"

    def export(self):
        r = self.row
        rows = [["name", self.sub.get("name", "")], ["cik", self.cik], ["sic", self.sub.get("sic", "")],
                ["industry", self.sub.get("sicDescription", "")],
                ["exchanges", " / ".join(self.sub.get("exchanges") or [])], ["fiscal_year_end", self.sub.get("fiscalYearEnd", "")]]
        if r:
            m = metrics(self.hub, r)
            rows += [["shares_outstanding", r.shares.val if r.shares else None], ["shares_as_of", r.shares.end if r.shares else ""],
                     ["price", m.price], ["price_source", m.label], ["market_cap_usd", m.cap], ["btc_held", r.holding.btc],
                     ["btc_as_of", r.holding.as_of], ["btc_element", r.holding.element], ["btc_filing", r.holding.cite], ["mnav", m.mnav]]
        rows += [[f"{label}_annual", a.val if a else None] for label, a, _ in self.keys]
        rows += [[f"{label}_quarter", b.val if b else None] for label, _, b in self.keys]
        return ["field", "value"], rows


# ── FA ──────────────────────────────────────────────────────

class FaPane(CompanyPane):
    code, selectable = "FA", True
    ORDER = ("income", "balance", "cash")

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        up = [a.upper() for a in self.args]
        self.freq = "quarterly" if "Q" in up or "QUARTERLY" in up else "annual"
        self.which = next((k for k in self.ORDER if k.upper() in up or k[:2].upper() in up), "income")
        self.books: dict[str, dict[str, sec.Statement]] = {}
        self.pcur = 0                   # the focused period, 0 the newest
        self.base: list[Provenance] = []

    async def load(self) -> None:
        self._title()
        if not await self.begin():
            return
        try:
            facts, p = await self.hub.sources["sec"].facts(self.cik)
        except SourceError as e:
            return self.failed(e)
        self.books = {f: await asyncio.to_thread(sec.statements, facts, f) for f in ("annual", "quarterly")}
        self.base = [p]

    def _title(self) -> None:
        self.title = f"{self.ticker} · {sec.STATEMENTS[self.which][0].lower()} · {self.freq}"

    def statement(self) -> sec.Statement | None:
        return (self.books.get(self.freq) or {}).get(self.which)

    def lines(self) -> list[sec.Line]:
        st = self.statement()
        return [ln for ln in st.lines if ln.by_end] if st else []

    def draw(self, w: int, h: int) -> list[Text]:
        if block := self.blocked(w):
            return block
        st = self.statement()
        if st is None:
            return []
        self.provs = self.base
        tabs = Text(no_wrap=True, overflow="crop")
        for k in self.ORDER:
            on, name = k == self.which, sec.STATEMENTS[k][0]
            if on or w >= 60:                                   # a narrow pane names only the statement it shows
                tabs.append(f" {name.upper() if on else name.lower()} ", style=f"bold {INK} on {ORANGE}" if on else FAINT)
                tabs.append(" ")
        for extra in (f" {self.freq}", " · USD millions", " · per-share lines in USD"):
            if tabs.cell_len + len(extra) <= w:
                tabs.append(extra, style=FAINT)
        lines = self.lines()
        self.n_rows = len(lines)
        self.cur = min(self.cur, max(self.n_rows - 1, 0))
        if not lines:
            return [tabs, Text(""), ui.note(f"No {self.freq} {st.title.lower()} lines in this company's SEC facts under the us-gaap concepts "
                                            "the terminal reads. A foreign filer reports under ifrs-full.", DIM)]
        label_w = min(max(13, *(len(ln.label) for ln in lines)), 26 if w >= 60 else 16)
        n = max(1, min((w - label_w) // 13, len(st.periods)))
        self.pcur = max(0, min(self.pcur, len(st.periods) - 1))
        first = max(0, min(self.pcur - n + 1, len(st.periods) - n)) if self.pcur >= n else 0
        shown = st.periods[first:first + n]
        cols = [ui.Col("period ending", "left", width=label_w, style=DIM)] + [ui.Col(f"{day(e, False)} {e[2:4]}") for e in shown]
        rows = [[ln.label, *[millions(ln.by_end.get(e), ln.unit) for e in shown]] for ln in lines]
        table = ui.table(cols, rows, w, self.cur)
        widths = [max(len(c.head), *(r[i + 1].cell_len for r in rows)) for i, c in enumerate(cols[1:])]
        k = self.pcur - first                               # the focused period's header, in orange
        end = label_w + sum(2 + x for x in widths[:k + 1])
        table[0].stylize(f"bold {ORANGE}", end - len(cols[k + 1].head), end)
        out = [tabs, Text(""), *table, Text("")]
        self.follow(4 + self.cur, max(h - 4, 1), head=4)
        ln, end = lines[self.cur], st.periods[self.pcur]
        if p := ln.by_end.get(end):
            span = f"{day(p.start)} to {day(p.end)}" if p.start else f"at {day(p.end)}"
            out += flow([Text(ln.label, style=f"bold {TEXT}"), ui.note(span, DIM), millions(p, ln.unit), ui.note(f"{p.val:,.0f} {p.unit}")],
                        w, "  ")
            out += wrap(f"{p.concept} · {p.form} {p.accn} · filed {day(p.filed)}" + (f" · fiscal {p.fy} {p.fp}" if p.fy else ""), w, FAINT)
            if p.derived:
                out += wrap(f"derived, not stated in any filing: {p.derived}. The filing named is the one that states the larger period.",
                               w, ui.YELLOW)
        else:
            out.append(ui.note(f"{ln.label}: not reported for the period ending {day(end)}", DIM))
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("a", "q"):
            self.freq, self.pcur = ("annual" if ch == "a" else "quarterly"), 0
        elif ch == "s":
            self.which, self.cur = self.ORDER[(self.ORDER.index(self.which) + 1) % 3], 0
        elif ch == "l" or k == "right":
            self.pcur += 1
        elif ch == "h" or k == "left":
            self.pcur = max(self.pcur - 1, 0)
        else:
            return False
        self._title()
        return True

    def enter(self) -> str | None:
        st, lines = self.statement(), self.lines()
        if st and lines and st.periods:
            p = lines[min(self.cur, len(lines) - 1)].by_end.get(st.periods[min(self.pcur, len(st.periods) - 1)])
            return f"CF {self.ticker} {p.accn}" if p and p.accn else None
        return None

    def command(self) -> str:
        return f"FA {self.ticker} {self.which} {'q' if self.freq == 'quarterly' else 'a'}"

    def hint(self) -> str:
        return "a annual · q quarterly · s statement · h l period · enter filing"

    def menu(self) -> list[tuple[str, str]]:
        t = self.ticker
        return [("DES", f"DES {t}"), ("CF", f"CF {t}"), ("N", f"N {t}"), ("TRSY", "TRSY")]

    def export(self):
        st = self.statement()
        if not st:
            return None
        head = ["line", "unit"] + [x for e in st.periods for x in (e, f"{e} filing")]
        cells = lambda ln: [x for e in st.periods for x in ((ln.by_end[e].val, ln.by_end[e].accn) if e in ln.by_end else (None, ""))]  # noqa: E731
        return head, [[ln.label, ln.unit, *cells(ln)]
                      for ln in st.lines if ln.by_end]


# ── the in-pane filing reader (CF and CFS) ──────────────────

MAX_DOC_CHARS = 1_500_000
_HEADING = re.compile(r"^(item|part|note|section)\s+[\dIVX]", re.I)
_TABLE_ROW = re.compile(r"\S {2,}\S")


def wrap_doc(text: str, width: int) -> list[str]:
    """Prose wraps on words to `width`. A table row (two or more spaces inside it) is never wrapped: it scrolls sideways."""
    out: list[str] = []
    width = max(width, 20)
    for line in text.split("\n"):
        if len(line) <= width or _TABLE_ROW.search(line):
            out.append(line)
            continue
        indent, cur = ("  " if line.startswith("- ") else ""), ""
        for word in line.split(" "):
            if cur and len(cur) + 1 + len(word) > width:
                out.append(cur)
                cur = indent + word
            else:
                cur = f"{cur} {word}" if cur else word
        out.append(cur)
    return out


@dataclass
class Doc:
    title: str
    url: str
    text: str
    clipped: int = 0                        # characters left out of a very large filing
    wrapped: tuple[int, list[str]] = (0, [])


class ReaderPane(CompanyPane):
    """A list whose Enter opens a filing as clean text inside the same pane. Subclasses draw the list."""
    selectable = True

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.doc: Doc | None = None
        self.want: tuple[str, str, str, str] | None = None      # (cik, accession, document, title) waiting to be fetched
        self.doc_note = ""
        self.doc_provs: list[Provenance] = []
        self.xoff = 0
        self._list_at = (0, 0)

    def open_doc(self, cik: str, accession: str, document: str, title: str) -> None:
        """Ask for a document. The fetch runs in `load`, off the paint path; the shell's next tick starts it if this
        pane cannot start it itself."""
        self.want, self.doc_note, self.loaded_at = (cik, accession, document, title), f"opening {title}…", 0.0
        if self.is_attached and not self.fetching:
            self.run_worker(self.reload(), group=f"pane-{id(self)}")
        self.bump()

    async def load_doc(self) -> None:
        if not self.want:
            return
        (cik, accession, document, title), self.want = self.want, None
        try:
            text, prov = await self.hub.sources["sec_www"].document(cik, accession, document)
        except SourceError as e:
            self.doc_note = "the SEC refused the User-Agent" if sec.is_refusal(e) else f"could not open {title}: {e}"
            return
        clipped = max(len(text) - MAX_DOC_CHARS, 0)
        doc = Doc(title, sec.archive_url(cik, accession, document), text[:MAX_DOC_CHARS], clipped)
        width = (self.size.width - 4) if self.is_attached and self.size.width > 24 else 100
        doc.wrapped = (width, await asyncio.to_thread(wrap_doc, doc.text, width))
        if self.doc is None:
            self._list_at = (self.cur, self.top)
        self.doc, self.doc_note, self.doc_provs = doc, "", [prov]
        self.selectable, self.top, self.xoff = False, 0, 0      # j k now scroll the text

    def due(self, now: float) -> bool:
        return super().due(now) or (self.want is not None and not self.fetching)

    def close_doc(self) -> None:
        self.doc, self.selectable, self.xoff = None, True, 0
        self.cur, self.top = self._list_at

    def draw_doc(self, w: int, h: int) -> list[Text]:
        doc = self.doc
        assert doc is not None
        if doc.wrapped[0] != w:
            doc.wrapped = (w, wrap_doc(doc.text, w))
        body = doc.wrapped[1]
        head = [ui.t((doc.title, f"bold {ORANGE}")), ui.note(doc.url), Text("─" * w, style=RULE)]
        tail = [Text(""), ui.note(f"The filing is {doc.clipped:,} characters longer than the reader holds. The rest is at the URL above.")]
        tail = tail if doc.clipped else []
        total = len(head) + len(body) + len(tail)
        top = max(0, min(self.top, max(total - h, 0)))
        out: list[Text] = [BLANK] * total                       # only the lines on screen are built
        for i in range(top, min(top + h, total)):
            if i < len(head):
                out[i] = head[i]
            elif i < len(head) + len(body):
                raw = body[i - len(head)]
                line = raw[self.xoff:] if self.xoff else raw
                style = f"bold {TEXT}" if _HEADING.match(raw) or (raw.isupper() and len(raw) < 90) else TEXT if _TABLE_ROW.search(raw) else DIM
                out[i] = Text(line, style=style, no_wrap=True, overflow="crop")
            else:
                out[i] = tail[i - len(head) - len(body)]
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if self.doc is None:
            return False
        if k in ("escape", "backspace"):
            self.close_doc()
        elif ch == "l" or k == "right":
            self.xoff += 8
        elif ch == "h" or k == "left":
            self.xoff = max(self.xoff - 8, 0)
        else:
            return False
        return True

    def doc_hint(self) -> str:
        return "j k scroll · h l sideways · backspace list"


# ── CF ──────────────────────────────────────────────────────

class CfPane(ReaderPane):
    code = "CF"
    FILTERS = ("ALL", "10-K", "10-Q", "8-K", "S-1", "DEF 14A", "13F", "4")

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.filings: list[sec.Filing] = []
        self.filter = "ALL"
        self.base: list[Provenance] = []
        self.first = next((a for a in self.args if re.fullmatch(r"\d{10}-?\d{2}-?\d{6}", a)), "")     # CF MSTR <accession> opens it at once
        for a in self.args:
            if a.upper() in self.FILTERS:
                self.filter = a.upper()

    async def load(self) -> None:
        self.title = f"{self.ticker} · filings"
        if not await self.begin():
            return
        try:
            sub, p = await self.hub.sources["sec"].submissions(self.cik)
        except SourceError as e:
            return self.failed(e)
        self.filings, self.base = sec.filings(sub, self.ticker, self.cik), [p]
        if self.first:
            acc, self.first = sec.dashed(self.first), ""
            f = next((x for x in self.filings if x.accession == acc), None)
            title = f"{self.ticker} {f.form} filed {day(f.filed)}" if f else f"{self.ticker} {acc} (index)"
            self.want = (self.cik, acc, f.document if f else "", title)
            if f:
                self.cur = self.shown().index(f) if f in self.shown() else 0
        await self.load_doc()

    def shown(self) -> list[sec.Filing]:
        return [f for f in self.filings if sec.form_matches(f.form, self.filter)]

    def draw(self, w: int, h: int) -> list[Text]:
        if block := self.blocked(w):
            return block
        if self.doc:
            self.provs = self.doc_provs
            return self.draw_doc(w, h)
        if not self.filings:
            return []
        self.provs = self.base
        rows = self.shown()
        self.n_rows = len(rows)
        self.cur = min(self.cur, max(self.n_rows - 1, 0))
        bar = Text(no_wrap=True, overflow="crop")
        bar.append("f ", style=f"bold {ORANGE}")
        for name in self.FILTERS:
            n = sum(1 for f in self.filings if sec.form_matches(f.form, name))
            chip = f" {name} {n} "
            if name == self.filter or (w >= 60 and bar.cell_len + len(chip) <= w):      # a narrow pane shows only the filter in force
                bar.append(chip, style=f"bold {INK} on {ORANGE}" if name == self.filter else FAINT if n else f"dim {FAINT}")
        out = [bar, Text(self.doc_note, style=ui.YELLOW) if self.doc_note else Text("")]
        cols = [ui.Col("filed", "left", style=DIM), ui.Col("form", "left", style=f"bold {TEXT}"), ui.Col("period", "left", style=DIM),
                ui.Col("about", "left", flex=True), ui.Col("items", "left", style=FAINT), ui.Col("accession", "left", style=FAINT)]
        body = [[day(f.filed, w >= 60), f.form, day(f.report, False) + f" {f.report[2:4]}" if f.report else "", filing_words(f) or f.document,
                 f.items, f.accession] for f in rows]
        out += fit_table(cols, body, w, ("accession", "items", "period"), self.cur)
        if not rows:
            out.append(ui.note(f"No {self.filter} filings among the {len(self.filings)} most recent.", DIM))
        self.follow(4 + self.cur, max(h, 1), head=4)
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if super().key(k, ch):
            return True
        if ch == "f" and self.doc is None:
            live = [x for x in self.FILTERS if x == "ALL" or any(sec.form_matches(f.form, x) for f in self.filings)]
            self.filter, self.cur, self.top = live[(live.index(self.filter) + 1) % len(live)] if self.filter in live else "ALL", 0, 0
            return True
        return False

    def enter(self) -> str | None:
        rows = self.shown()
        if self.doc is None and rows:
            f = rows[min(self.cur, len(rows) - 1)]
            self.open_doc(f.cik or self.cik, f.accession, f.document, f"{self.ticker} {f.form} filed {day(f.filed)}")
        return None

    def command(self) -> str:
        return f"CF {self.ticker}" + (f" {self.filter}" if self.filter != "ALL" else "")

    def hint(self) -> str:
        return self.doc_hint() if self.doc else "f form filter · enter reads it here"

    def menu(self) -> list[tuple[str, str]]:
        t = self.ticker
        return [("DES", f"DES {t}"), ("FA", f"FA {t}"), ("N", f"N {t}"), ("CFS", f"CFS {t}")]

    def export(self):
        return (["filed", "form", "period", "description", "items", "accession", "url"],
                [[f.filed, f.form, f.report, f.description, f.items, f.accession, f.url] for f in self.shown()])


# ── CFS ─────────────────────────────────────────────────────

class CfsPane(ReaderPane):
    code, every = "CFS", 0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.asked = sec.parse_query(self.args)             # not `query`: Textual's Widget owns that name
        self.hits: list[sec.Hit] = []
        self.total = 0
        self.base: list[Provenance] = []

    async def load(self) -> None:
        q = self.asked
        self.title = " ".join(x for x in (q.words or "full-text search", f"form:{','.join(q.forms)}" if q.forms else "",
                                          f"since:{q.since}" if q.since else "", f"until:{q.until}" if q.until else "") if x)
        if not q.words.strip() or not await self.begin():
            return
        if not self.hits:
            try:
                self.hits, self.total, p = await self.hub.sources["sec_efts"].search(q)
            except SourceError as e:
                return self.failed(e)
            self.base, self.n_rows = [p], len(self.hits)
        await self.load_doc()

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.asked.words.strip():
            return [ui.section("FULL-TEXT SEARCH ACROSS SEC FILINGS"[:max(w - 2, 8)], w), Text(""),
                    *wrap("CFS <words> [form:8-K] [since:2026-01-01] [until:2026-06-30]", w, TEXT),
                    Text(""), *wrap('Example: CFS bitcoin treasury form:8-K since:2026-01-01. Put a phrase in double quotes: '
                                       'CFS "digital asset".'
                                       " The index covers 2001 to today.", w, DIM)]
        if block := self.blocked(w):
            return block
        if self.doc:
            self.provs = self.doc_provs
            return self.draw_doc(w, h)
        if not self.loaded_at:
            return []
        self.provs = self.base
        self.n_rows = len(self.hits)
        out = [*flow([ui.t((f"{self.total:,}", f"bold {TEXT}"), (" documents match", DIM)), ui.note(f"showing the first {len(self.hits)}")], w),
               Text(self.doc_note, style=ui.YELLOW) if self.doc_note else Text("")]
        cols = [ui.Col("filed", "left", style=DIM), ui.Col("form", "left", style=f"bold {TEXT}"),
                ui.Col("ticker", "left", style=f"bold {ORANGE}"),
                ui.Col("company", "left", flex=True), ui.Col("document", "left", style=FAINT)]
        body = [[day(x.filed, w >= 60), x.form, x.ticker, x.company, x.document] for x in self.hits]
        out += fit_table(cols, body, w, ("document", "ticker"), self.cur)
        self.follow(len(out) - len(self.hits) + self.cur, max(h, 1), head=4)
        return out

    def enter(self) -> str | None:
        if self.doc is None and self.hits:
            x = self.hits[min(self.cur, len(self.hits) - 1)]
            self.open_doc(x.cik, x.accession, x.document, f"{x.ticker or x.company} {x.form} filed {day(x.filed)}")
        return None

    def hint(self) -> str:
        return self.doc_hint() if self.doc else "enter reads it here"

    def export(self):
        return (["filed", "form", "ticker", "company", "cik", "accession", "document", "url"],
                [[x.filed, x.form, x.ticker, x.company, x.cik, x.accession, x.document, x.url] for x in self.hits])


# ── N and TOP: filings as they land ─────────────────────────

WATCH_MAX = 14


async def watched_filings(hub: Hub, per_company: int = 6) -> tuple[list[sec.Filing], list[Provenance], bool]:
    """(filings newest first, provenance, was the User-Agent refused) across the treasury and miner lists."""
    if not has_contact(hub):
        return [], [], False
    seen: dict[str, str] = {}
    for name in ("treasuries", "miners"):
        for e in await asyncio.to_thread(curated, name):
            t = str(e.get("ticker", "")).upper()
            if t not in seen and e.get("holdings") != "manual" and (cik := cik_for(hub, t, str(e.get("cik") or ""))[0]):
                seen[t] = cik
    pairs = list(seen.items())[:WATCH_MAX]
    got = await asyncio.gather(*[hub.sources["sec"].submissions(c) for _, c in pairs], return_exceptions=True)
    out, provs, refused = [], [], False
    for (t, c), res in zip(pairs, got, strict=True):
        if isinstance(res, BaseException):
            refused = refused or sec.is_refusal(res)
            continue
        out += sec.filings(res[0], t, c)[:per_company]
        provs.append(res[1])
    return sorted(out, key=lambda f: (f.filed, f.accepted), reverse=True), provs[:1], refused


class NewsPane(CompanyPane):
    code, selectable, every = "N", True, 900

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.filings: list[sec.Filing] = []
        self.base: list[Provenance] = []

    async def load(self) -> None:
        self.title = f"{self.ticker} · news and filings" if self.security else " ".join(self.args) or "filings across treasuries and miners"
        if not await self.begin():
            return
        if self.security:
            try:
                sub, p = await self.hub.sources["sec"].submissions(self.cik)
            except SourceError as e:
                return self.failed(e)
            self.filings, self.base = sec.filings(sub, self.ticker, self.cik), [p]
        else:
            self.filings, self.base, refused = await watched_filings(self.hub)
            if refused and not self.filings:
                self.state = "refused"
        self.n_rows = len(self.filings)

    def draw(self, w: int, h: int) -> list[Text]:
        head = wrap(JEV, w, FAINT)
        if block := self.blocked(w):
            return head + [Text("")] + block
        if not self.loaded_at:
            return []
        self.provs = self.base
        now = time.time()
        topic = " ".join(self.args)
        out = list(head)
        if topic:
            out.append(ui.note(f"A topic search ({topic}) needs headlines. CFS {topic} searches the text of filings now.", DIM))
        out.append(Text(""))
        cols = [ui.Col("age"), ui.Col("filed", "left", style=DIM), ui.Col("ticker", "left", style=f"bold {ORANGE}"),
                ui.Col("form", "left", style=f"bold {TEXT}"), ui.Col("about", "left", flex=True, style=DIM)]
        body = [[age(f, now), day(f.filed, False), f.ticker, f.form, filing_words(f)] for f in self.filings]
        at = len(out) + 2
        out += fit_table(cols, body, w, ("filed",), self.cur)
        self.follow(at + self.cur, max(h, 1), head=at)
        return out

    def enter(self) -> str | None:
        if self.filings:
            f = self.filings[min(self.cur, len(self.filings) - 1)]
            return f"CF {f.ticker} {f.accession}"
        return None

    def hint(self) -> str:
        return "enter opens the filing in CF"

    def menu(self) -> list[tuple[str, str]]:
        t = self.ticker
        if not t:
            return [("TRSY", "TRSY"), ("MINR", "MINR"), ("ETF", "ETF")]
        return [("DES", f"DES {t}"), ("CF", f"CF {t}"), ("FA", f"FA {t}"), ("TRSY", "TRSY")]

    def export(self):
        return (["filed", "ticker", "form", "about", "accession", "url"],
                [[f.filed, f.ticker, f.form, filing_words(f), f.accession, f.url] for f in self.filings])


class TopPane(FuncPane):
    """The launchpad's bottom-right corner: blocks as they are mined, alerts as they fire, filings as they land."""
    code, every, selectable, tick = "TOP", 900, True, 5.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.filings: list[sec.Filing] = []
        self.known: set[str] | None = None
        self.refused = False
        self.base: list[Provenance] = []
        self.items: list[tuple[float, str]] = []            # (time, GO command) of each row on screen

    async def load(self) -> None:
        self.title = "news · filings"
        sync_contact(self.hub)
        if not has_contact(self.hub):
            self.filings, self.base = [], []
            return
        try:
            await try_fill_companies(self.hub)
            self.filings, self.base, self.refused = await watched_filings(self.hub, per_company=3)
        except SourceError as e:
            self.refused = sec.is_refusal(e)
            return
        fresh = [f for f in self.filings if self.known is not None and f.accession not in self.known]
        for f in fresh[:5]:                                 # a filing that landed while the terminal was open is an event too
            self.hub.event("filing", ticker=f.ticker, form=f.form, accession=f.accession, text=filing_words(f))
        self.known = (self.known or set()) | {f.accession for f in self.filings}

    def cache_key(self) -> tuple:
        return (len(self.hub.events), self.hub.chain.height, len(self.filings))

    def frame_top(self, w: int, now: float) -> Text:
        c = self.hub.chain.prov
        parts = ([f"{c.source.split('.')[0]} {c.delay}"] if c else []) + (["sec daily"] if self.base else [])
        return framed(self, w, now, " · ".join(parts), super().frame_top)

    def _block(self, when: float, height: int, pool: str, txs: int, fees: float, w: int, now: float) -> Text:
        out = ui.t((f"{ui.when(when, now):>3} ", FAINT), ("■ ", ORANGE), (f"#{height:,}", f"bold {TEXT}"))
        tail = [f"  {txs:,} tx" if txs else "", f"  {fmt.sats(fees)}" if fees else ""]
        room = w - out.cell_len - 1
        for extra in list(tail):
            if len(extra) > room - min(len(pool), 8):
                tail.remove(extra)
            else:
                room -= len(extra)
        out.append(f" {pool[:max(room, 0)]}" if pool and room > 2 else "", style=DIM)
        out.append("".join(tail), style=FAINT)
        return out

    def draw(self, w: int, h: int) -> list[Text]:
        hub, now, c = self.hub, time.time(), self.hub.chain
        self.provs = [p for p in [c.prov, *self.base] if p]
        feed: list[tuple[float, Text, str]] = []
        heights = set()
        for e in hub.events:
            kind, at = e.get("kind"), float(e.get("at") or now)
            if kind == "block":
                heights.add(e.get("height"))
                line = self._block(at, int(e.get("height") or 0), str(e.get("pool") or ""), int(e.get("txs") or 0),
                                   float(e.get("fees") or 0), w, now)
                feed.append((at, line,
                             f"BLK {e.get('height')}"))
            elif kind != "filing":                          # a fired alert, or anything a later phase adds
                words = str(e.get("text") or e.get("message") or e.get("name") or kind)
                line = ui.t((f"{ui.when(at, now):>3} ", FAINT), ("● ", ui.YELLOW), (words[:max(w - 6, 8)], TEXT))
                feed.append((at, line, str(e.get("go") or "")))
        if c.height and c.height not in heights:            # the tip the terminal started on never fired an event
            feed.append((c.tip_time, self._block(c.tip_time, c.height, c.tip_pool, c.tip_txs, c.tip_fees_sat, w, now), f"BLK {c.height}"))
        for f in self.filings:
            line = ui.t((f"{age(f, now, short=True):>3} ", FAINT), ("▲ ", ui.CYAN), (f"{f.ticker} ", f"bold {ORANGE}"), (f.form, f"bold {TEXT}"))
            if (words := filing_words(f)) and w - line.cell_len - 2 >= 6:
                line.append(": ", style=f"bold {TEXT}")
                line.append(words if len(words) <= w - line.cell_len else words[:w - line.cell_len - 1] + "…", style=DIM)
            feed.append((f.at, line, f"CF {f.ticker} {f.accession}"))
        feed.sort(key=lambda x: -x[0])
        foot: list[Text] = []
        if not has_contact(hub):
            long = f"filings are off: the SEC needs a contact first · {sec.HOW_TO_SET}"
            foot = [ui.note(long if w >= len(long) else "SET sec_user_agent turns filings on")]
        elif self.refused:
            foot = [ui.note("the SEC refused the User-Agent", RED)]
        elif not self.filings and self.loaded_at:
            foot = [ui.note("no filings loaded yet")]
        if not feed:
            feed = [(now, ui.note("waiting for the first block…", DIM), "")]
        if c.fees and len(feed) + len(foot) < h:
            foot.insert(0, ui.t(("    next " if w < 44 else "    next block ", FAINT), (f"{c.fastest_fee:g} sat/vB", DIM),
                                (f" · {ui.vbytes(c.mempool_vsize)}" + (" waiting" if w >= 44 else ""), FAINT)))
        room = max(h - len(foot) - (1 if foot else 0), 1)
        self.n_rows = len(feed)
        self.cur = min(self.cur, max(self.n_rows - 1, 0))
        self.items = [(at, go) for at, _, go in feed]
        out = []
        for i, (_, line, _) in enumerate(feed):
            if i == self.cur and self.active:
                line = line.copy()
                line.pad_right(max(w - line.cell_len, 0))
                line.stylize(f"on {ui.CURSOR_BG}")
            out.append(line)
        if len(feed) <= room:                               # short feed: the foot sits on the bottom edge of a small pane
            out += [Text("")] * ((h - len(out) - len(foot)) if h <= 16 else 1) + foot
        else:
            self.follow(self.cur, room, head=0)
            out += [Text("")] + foot
        return out

    def enter(self) -> str | None:
        return self.items[self.cur][1] or None if 0 <= self.cur < len(self.items) else None

    def hint(self) -> str:
        return "enter opens"

    def export(self):
        return (["filed", "ticker", "form", "about", "accession"],
                [[f.filed, f.ticker, f.form, filing_words(f), f.accession] for f in self.filings])


# ── registration ────────────────────────────────────────────

_SEC = ("Every SEC number comes from SEC EDGAR: data.sec.gov for filing indexes and XBRL company facts, www.sec.gov for the ticker file and the "
        "filing archive, efts.sec.gov for full-text search. The terminal keeps to 5 requests a second across the three, half the SEC's ceiling. "
        "Tickers are cached for a day, filing indexes for an hour, company facts for a day. ")
_CONTACT = ("The SEC requires a contact in the User-Agent of every request, and the SEC receives it. Until you set one with "
            f"{sec.HOW_TO_SET}, nothing is sent to the SEC and the page explains this instead. If the SEC answers 403 the page says the SEC "
            "refused the User-Agent, and the terminal does not ask again until the setting changes. When the SEC is down the last good values "
            "stay on screen, dimmed, with their age. ")
_HOLDING = ("Bitcoin held is never assumed (PLAN D38). The terminal searches the company's own XBRL facts for concepts whose name or label "
            "mentions crypto, bitcoin or digital asset. It prefers a BTC-like unit or a count of units for the holding and USD for fair value, "
            "takes the most recent period, and shows the element's name, the form, the accession number and the filing date beside the number. "
            "Filed holdings are quarter-end figures and lag any purchase announced since in an 8-K. ")
_MNAV = ("mNAV is market cap divided by the value of the Bitcoin held. Market cap is the dei shares-outstanding fact from the latest cover page "
         "times the price. The Bitcoin is valued at the terminal's composite BTC price now. Each input shows its own as-of date. ")
_PRICE = ("Most equities have no open quote without a key (PLAN D36). Where Kraken lists a tokenised tracker the price is shown through it and "
          "labelled, as in MSTR via MSTRx, proxy, live. Otherwise the page says no open quote and the cells that need a price show a dash. ")

register(Function(
    "DES", "Description", "Companies", "a company: industry, shares, price, market cap, Bitcoin held, key financials, filings", DesPane,
    takes=("EQUITY", "ETF"), default_for=("EQUITY", "ETF"), needs=("filings", "company_facts", "quote"),
    help="The company page. Name, SIC industry, exchange and fiscal year end come from the SEC submissions file. Shares outstanding is "
         "dei:EntityCommonStockSharesOutstanding from the latest cover page, with its date. Key financials (revenue, net income, assets, "
         "cash, debt, equity) are the latest fiscal year and the latest quarter from XBRL company facts, in USD millions. The latest filings "
         "close the page. Digits open FA, CF, N and TRSY. " + _PRICE + _HOLDING + _MNAV + _SEC + "The page reloads every hour. " + _CONTACT))
register(Function(
    "FA", "Financial analysis", "Companies", "standardised income statement, balance sheet and cash flow, annual and quarterly", FaPane,
    takes=("EQUITY", "ETF"), args="[income|balance|cash] [a|q]", needs=("company_facts",),
    help="Three standardised statements built from XBRL company facts. Columns are periods, newest first, as many as fit. Rows are lines. "
         "Numbers are USD millions with thousands separators, negatives in red, per-share lines in USD. a shows fiscal years (twelve-month "
         "values and year-end balances that a 10-K reports). q shows single quarters. s moves to the next statement. h and l move the focused "
         "period. Each line is tied to the filing it came from: the bottom of the page shows the concept, the form, the accession number and "
         "the filing date of the focused cell, and Enter opens that filing in CF. When a filing restates a period the latest filing wins. "
         "No 10-K states a fourth quarter, and cash flows in a 10-Q run year to date, so those quarters are derived: the cumulative value less "
         "the one before it from the same fiscal-year start. A derived cell says so and says how. A line shows a dash when the company does not "
         "report any of the us-gaap concepts the line reads. Foreign filers that report under IFRS have no lines. " + _SEC
         + "The page reloads every hour. " + _CONTACT))
register(Function(
    "CF", "Company filings", "Companies", "the filings list with a form filter; Enter reads the document inside the pane", CfPane,
    takes=("EQUITY", "ETF"), args="[form] [accession]", needs=("filings", "archive"),
    help="The most recent filings from the SEC submissions file: date filed, form, period, what it is about (an 8-K's items in words), item "
         "numbers and accession number. f cycles the form filter: all, 10-K, 10-Q, 8-K, S-1, DEF 14A, 13F, 4. Enter opens the primary document "
         "inside the pane as clean text: scripts, styles and the hidden inline-XBRL header are dropped, paragraphs, headings and lists keep "
         "their breaks, and data tables become aligned columns. j, k, ctrl-d, ctrl-u, g and G scroll. h and l move sideways across a wide "
         "table. Backspace returns to the list. CF MSTR followed by an accession number opens that filing directly. The document comes from "
         "www.sec.gov/Archives and is cached for thirty days, because a filing never changes. A very large filing is cut at 1.5 million "
         "characters and the page gives the URL for the rest. " + _SEC + _CONTACT))
register(Function(
    "CFS", "Filing search", "Companies", "full-text search across SEC filings, with form: and since: filters", CfsPane,
    args="<words> [form:8-K] [since:2026-01-01] [until:2026-06-30]", needs=("search", "archive"),
    help="Full-text search across every filing since 2001, through efts.sec.gov/LATEST/search-index. Type the words after CFS. form:8-K limits "
         "the forms, and several are separated by commas. since:2026-01-01 and until:2026-06-30 limit the filing date. A phrase goes in double "
         "quotes. The table shows date, form, ticker, company and document. Enter opens the document inside the pane in the same reader as CF. "
         "Results are cached for ten minutes. " + _SEC + _CONTACT))
register(Function(
    "TRSY", "Bitcoin treasuries", "Companies", "treasury companies: BTC held, its filing and date, BTC value, market cap, mNAV, BTC per share",
    TrsyPane, needs=("company_facts", "quote"),
    help="Companies that hold Bitcoin, from the curated treasuries.toml, ranked by Bitcoin held. Each row shows its own as-of date and the "
         "source of the holding: the XBRL element for an SEC filer, or the manual entry for a company outside the SEC, whose URL and date "
         "the cursor shows. BTC value is the holding times the composite BTC price now. BTC per share is shown in sats. The 30-day "
         "sparkline appears when an open price history exists. A dash always has a reason, shown for the row under the cursor: no SEC "
         "contact set, no open quote, no CIK, or no sourced figure. Enter opens the filing behind the holding in CF. D opens DES. The frame "
         "names holdings and prices separately: holdings come from filings, prices from the quote board. " + _HOLDING + _MNAV + _PRICE + _SEC
         + "Prices refresh with the quote board every few seconds; SEC inputs reload every hour. " + _CONTACT))
register(Function(
    "MINR", "Public miners", "Companies", "public Bitcoin miners: price, market cap, BTC held, self-reported hashrate, performance against BTC",
    MinrPane, needs=("company_facts", "quote", "history"),
    help="Public miners from the curated miners.toml. Price, day change and market cap need an open quote. BTC held is found in SEC facts the "
         "same way as on TRSY. Hashrate is self-reported by each company in its monthly update, in EH/s. No open feed carries it, so it appears "
         "only when miners.toml holds the figure with its date and the URL it was read from, and is a dash otherwise. vs BTC 30d is the share's "
         "30-day return over Bitcoin's, from daily closes, when both histories exist. Foreign filers (20-F, 40-F, 6-K) may report under "
         "IFRS and then have no discoverable holding. Enter opens the filing behind the holding. D opens DES. " + _HOLDING + _PRICE + _SEC
         + "SEC inputs reload every hour. " + _CONTACT))
register(Function(
    "ETF", "Spot Bitcoin ETFs", "Companies", "US spot Bitcoin ETFs: issuer, price where open, holdings from SEC filings, why there are no flows",
    EtfPane, needs=("company_facts", "quote"),
    help="US spot Bitcoin ETFs from the curated etfs.toml: ticker, fund, issuer, and the verdict on the issuer's holdings file. " + FLOWS + " "
         "Phase 0 read each issuer's terms: BlackRock, Fidelity and Bitwise forbid automated access, and the rest could not be read, which the "
         "terminal treats as not permitted. Premium or discount needs a daily NAV, which only the issuers publish, so it is not shown. "
         "Price and volume need an open quote, and US-listed ETFs have none without a key. Holdings come from each trust's own SEC facts "
         "when a CIK is known and a crypto concept is found, with the filing and its date. The Grayscale Mini Trust's ticker is BTC: on "
         "this page that row never reads Bitcoin's own price. " + _HOLDING + _SEC + "SEC inputs reload every hour. " + _CONTACT))
register(Function(
    "N", "News", "Companies", "news for a ticker: its filings as they land; headlines arrive with the Jev hub", NewsPane,
    takes=("EQUITY", "ETF"), optional=True, args="[ticker or topic]", needs=("filings",),
    help=JEV + " With a ticker, N lists that company's filings, newest first, with their age. With no ticker it lists the latest filings across "
         "the treasury and miner lists. Age is exact when the SEC's index carries the acceptance time, and counted in days otherwise. Enter "
         "opens the filing in CF. A topic typed after N is kept for the headline search to come, and CFS searches the text of filings now. "
         + _SEC + "The list reloads every fifteen minutes; the SEC index itself is cached for an hour. " + _CONTACT))
register(Function(
    "TOP", "Top news", "Companies", "the newest events: blocks as they are mined, fired alerts, filings from the watched companies", TopPane,
    takes=("EQUITY", "ETF"), optional=True, needs=("tip", "filings"),
    help="The newest events first. New blocks and fired alerts come from the terminal's own event stream: a block shows its height, pool, "
         "transaction count and fees in sats. Filings come from the SEC for the companies in treasuries.toml and miners.toml, three each, "
         "with their age. The triangle marks a filing and carries no sentiment. Enter opens the block in BLK or the filing in CF. With no SEC "
         "contact the page still shows blocks as they are mined and the next-block fee, and one line says how to turn filings on. " + JEV + " "
         + _SEC + "Filings reload every fifteen minutes. " + _CONTACT))
