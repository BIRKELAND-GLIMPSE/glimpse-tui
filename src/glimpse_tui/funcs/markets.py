"""Markets and macro (TERMINAL.md 7): QM, WEI, FX, GLCO, RATES, MACRO, ECO, HMAP, RV and DVOL.

Pyth needs a key now (PLAN D36), so nothing here uses it. What is left is a mix: crypto and five FX majors are live,
the yen and the krona are daily ECB fixes, indices, oil and rates are daily official closes, copper and the grains
are monthly averages, gold shows through PAXG, a few shares show through Kraken's tokenised trackers, and some
instruments have no open source at all. Every row therefore says what it is: `live`, `closed`, `daily 18 Sep`,
`monthly Jul`, `proxy PAXG`, `computed`, or a dim `no open source`. A proxy is never passed off as the instrument
it stands in for. That labelling is `status()` below, and every page uses it.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

from rich.text import Text

from .. import charts, fmt
from ..data import btcmath, deribit, fred, fx_ref, quotes, sentiment
from ..data.core import DAILY, LIVE, MONTHLY, WEEKLY, Provenance, SourceError
from ..data.treasury import TENORS, Curve
from ..term import ui
from ..term.instruments import Instrument
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT

DAY = 86400.0
INK = "#0D0D0D"
QUARTERLY = "quarterly"
YEAR_LINE = "#6a6a6a"                        # the curve a year ago: present, but behind the others
TROY_OZ_PER_TONNE = 32_150.7466
GOLD_STOCK_SOURCE = "World Gold Council above-ground stock, end 2024, set in config"

# What FRED publishes each series in and how often, as SOURCES.md records them from the series pages. FRED's CSV
# carries neither, and the quote engine files everything from FRED under `daily`, so the pages correct it here.
FRED_META: dict[str, tuple[str, str]] = {
    "DCOILBRENTEU": ("$/bbl", DAILY), "DCOILWTICO": ("$/bbl", DAILY), "DHHNGSP": ("$/MMBtu", DAILY),
    "PCOPPUSDM": ("$/mt", MONTHLY), "PSOYBUSDM": ("$/mt", MONTHLY), "PMAIZMTUSDM": ("$/mt", MONTHLY), "PWHEAMTUSDM": ("$/mt", MONTHLY),
    "PSUGAISAUSDM": ("cents/lb", MONTHLY),
    "WALCL": ("$ millions", WEEKLY), "WTREGEN": ("$ millions", WEEKLY), "RRPONTSYD": ("$ billions", DAILY), "M2SL": ("$ billions", MONTHLY),
    "CPIAUCSL": ("index", MONTHLY), "UNRATE": ("%", MONTHLY), "PAYEMS": ("thousands", MONTHLY), "GDP": ("$ billions, annual rate", QUARTERLY),
    "GDPC1": ("chained 2017 $ billions, annual rate", QUARTERLY),
    "DFF": ("%", DAILY), "SOFR": ("%", DAILY), "DFII10": ("%", DAILY), "T10YIE": ("%", DAILY), "T10Y2Y": ("%", DAILY),
}


# ── honesty: what a number is, where it came from, how old it is ─────────────

def cadence(prov: Provenance) -> str:
    """The true rhythm of a value: FRED's monthly and weekly series arrive labelled `daily`."""
    if prov.source.startswith("fred:"):
        return FRED_META.get(prov.source[5:], ("", prov.delay))[1]
    return prov.delay


def relabel(prov: Provenance, delay: str | None = None) -> Provenance:
    d = delay or cadence(prov)
    return prov if d == prov.delay else Provenance(prov.source, prov.fetched_at, prov.as_of, d if d != QUARTERLY else MONTHLY, prov.note)


SLOWEST_FIRST = {MONTHLY: 0, WEEKLY: 1, DAILY: 2, "delayed": 3, LIVE: 4}


def distinct(provs: Sequence[Provenance | None], slowest_first: bool = True) -> list[Provenance]:
    """One Provenance per source. The slowest kind leads: a pane too narrow to name its sources shows only the first
    one's delay, and a page that mixes live and daily values must not call itself live."""
    out: dict[str, Provenance] = {}
    for p in provs:
        if p is not None:
            out.setdefault(p.source, p)
    return sorted(out.values(), key=lambda p: SLOWEST_FIRST.get(p.delay, 2)) if slowest_first else list(out.values())


def asof(ts: float, cad: str = DAILY, year: bool = False) -> str:
    d = datetime.fromtimestamp(ts, UTC)
    if cad == MONTHLY:
        return d.strftime("%b %Y") if year else d.strftime("%b")
    if cad == QUARTERLY:
        return f"Q{(d.month - 1) // 3 + 1} {d:%Y}" if year else f"Q{(d.month - 1) // 3 + 1} {d:%y}"
    return d.strftime("%d %b %Y") if year else d.strftime("%d %b")


def show(hub: Any, ticker: str) -> str:
    """A ticker as its venue writes it: Kraken's tokenised shares end in a lower-case x (SPYx)."""
    ins = hub.book.get(ticker)
    if ins and "xStocks" in ins.name and ticker.upper().endswith("X"):
        return ticker[:-1].upper() + "x"
    return ticker.upper()


def reachable(hub: Any, ins: Instrument | None, depth: int = 0) -> bool:
    """Does any keyless source carry this instrument, itself or down its proxy chain?"""
    if ins is None or depth > 3:
        return False
    from ..data.quotes import yahoo_on
    live = [x for x in ins.sources if not x.startswith("yahoo:") or yahoo_on(hub)]
    return bool(live) or (bool(ins.proxy) and reachable(hub, hub.book.get(ins.proxy), depth + 1))


def dxy_mix(hub: Any, short: bool = False) -> str:
    """`EUR GBP CAD CHF live, JPY SEK daily`: which legs of the computed dollar index are live right now."""
    live, slow = [], []
    for pair in fx_ref.DXY_WEIGHTS:
        q = hub.quotes.get(pair)
        if q:
            (live if q.prov.delay == LIVE else slow).append(pair.replace("USD", ""))
    if short:
        return ", ".join(x for x in (f"{len(live)} legs live" if live else "", f"{len(slow)} daily" if slow else "") if x)
    return ", ".join(x for x in (f"{' '.join(live)} live" if live else "", f"{' '.join(slow)} daily" if slow else "") if x)


def status(hub: Any, ticker: str, wide: bool = False) -> Text:
    """The label every row carries. Narrow forms fit 14 cells; wide ones add the as-of date and the session."""
    ins, q = hub.book.get(ticker), hub.quotes.get(ticker)
    if q is None:
        return Text("no open source" if not reachable(hub, ins) else "no answer yet", style=FAINT, no_wrap=True)
    out, cad = Text(no_wrap=True), cadence(q.prov)
    if q.via:
        out.append("proxy ", style=ui.YELLOW)
        out.append(show(hub, q.via), style=f"bold {DIM}")
        if not wide:
            return out
        out.append(" · ", style=FAINT)
    elif q.prov.source == "computed":
        out.append("computed", style=ui.CYAN)
        if wide and (mix := dxy_mix(hub, True)):
            out.append(f" · {mix}", style=FAINT)
        return out
    if cad == LIVE:
        out.append("closed" if q.closed and not wide else "live", style=DIM if q.closed else GREEN)
        if q.closed and wide:
            out.append(" · closed", style=DIM)
    else:
        out.append(cad, style=DIM)
        out.append(f" {asof(q.prov.as_of, cad, wide and cad != DAILY)}", style=FAINT)
    return out


def price(ins: Instrument | None, v: float | None, decimals: int | None = None) -> str:
    if v is None:
        return "–"
    d = (ins.decimals if ins else 2) if decimals is None else decimals
    return ui.px(v, d) + ("%" if ins and ins.cls == "GOVT" else "")


def pct_of(hub: Any, ticker: str) -> Text:
    """Change for a row: yields move in basis points, everything else in percent."""
    q, ins = hub.quotes.get(ticker), hub.book.get(ticker)
    if q is None:
        return Text("–", style=FAINT)
    if ins and ins.cls == "GOVT":
        bp = delta(q.change * 100 if q.change is not None else None, 0)
        return bp if bp.plain == "–" else Text(bp.plain + "bp", style=bp.style)
    return delta_pct(q.pct, 2 if q.pct is not None and abs(q.pct) < 0.0995 else 1)


def delta(v: float | None, digits: int = 2) -> Text:
    """`ui.signed`, except that a change too small to show at this precision reads 0.00, not −0.00."""
    return ui.signed(0.0 if v is not None and not math.isnan(v) and round(v, digits) == 0 else v, digits)


def delta_pct(frac: float | None, digits: int = 1) -> Text:
    return ui.signed_pct(0.0 if frac is not None and not math.isnan(frac) and round(frac * 100, digits) == 0 else frac, digits)


def spark_cell(values: Sequence[float], width: int) -> Text:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return Text("")
    return Text(charts.spark(vals, width), style=GREEN if vals[-1] >= vals[0] else RED, no_wrap=True)


def range_cell(q: Any, ins: Instrument | None, track: int = 5) -> Text:
    """`80,822 ──┿── 81,925`: the day's low and high with the last trade marked between them."""
    if not (q and q.low and q.high and q.high > q.low):
        return Text("–", style=FAINT)
    d = 0 if q.high >= 1000 else (ins.decimals if ins else 2)
    pos = max(0, min(track - 1, round((q.price - q.low) / (q.high - q.low) * (track - 1))))
    out = Text(no_wrap=True)
    out.append(f"{ui.px(q.low, d)} ", style=DIM)
    out.append("─" * pos, style=ui.RULE)
    out.append("┿", style=f"bold {ORANGE}")
    out.append("─" * (track - 1 - pos), style=ui.RULE)
    out.append(f" {ui.px(q.high, d)}", style=DIM)
    return out


def grid(cols: Sequence[ui.Col], rows: Sequence[Sequence[ui.Cell] | Text], width: int, cursor: int | None = None, gap: int = 2) -> list[Text]:
    """`ui.table`, with whole-line rows allowed between the table rows: section heads, a proxy beneath its index,
    an instrument with no open source. The cursor counts every row."""
    real = [r for r in rows if not isinstance(r, Text)]
    at = None
    if cursor is not None and 0 <= cursor < len(rows) and not isinstance(rows[cursor], Text):
        at = sum(1 for r in rows[:cursor] if not isinstance(r, Text))
    lines = ui.table(cols, real, width, at, gap)
    out, body = lines[:2], iter(lines[2:])
    for i, r in enumerate(rows):
        if isinstance(r, Text):
            ln = r.copy()
            ln.truncate(width)
            if cursor == i:
                ln.pad_right(max(width - ln.cell_len, 0))
                ln.stylize(f"on {ui.CURSOR_BG}")
            out.append(ln)
        else:
            out.append(next(body))
    return out


def unavailable(hub: Any, ticker: str, width: int, label_w: int = 8) -> Text:
    """An instrument nobody publishes openly stays on the page, dim, and says so."""
    ins = hub.book.get(ticker)
    out = ui.t((f"{ticker:<{label_w}} ", FAINT), (status(hub, ticker).plain, FAINT))
    name = ins.name if ins else ""
    if name and out.cell_len + len(name) + 3 <= width:
        out.append(f" · {name}", style="#6a6a6a")
    return out


# ── arithmetic the pages share ──────────────────────────────

def start_years(n: int) -> str:
    """The `cosd` the quote engine also asks FRED for, so a page and the tape share one cached response."""
    now = datetime.now(UTC)
    return f"{now.year - n}-{now.month:02d}-01"


def at_or_before(dates: Sequence[float], values: Sequence[float], ts: float) -> tuple[float, float] | None:
    hit = None
    for d, v in zip(dates, values, strict=False):
        if d > ts:
            break
        hit = (d, v)
    return hit


def change_over(dates: Sequence[float], values: Sequence[float], days: float, last: float | None = None) -> float | None:
    """The fractional change over `days`, from the close at or before that day. None when the series has no
    observation near it: a monthly average has no one-day or one-week change, and saying so beats inventing one."""
    if len(values) < 2:
        return None
    end = values[-1] if last is None else last
    if days <= 1:
        base = (dates[-2], values[-2])
        tol = 5 * DAY
        target = dates[-1] - DAY
    else:
        target = dates[-1] - days * DAY
        base = at_or_before(dates, values, target)
        tol = max(4 * DAY, days * DAY * 0.2)
    if base is None or target - base[0] > tol or not base[1]:
        return None
    return end / base[1] - 1


def ytd(dates: Sequence[float], values: Sequence[float], last: float | None = None) -> float | None:
    """Year to date from the first close of the latest observation's year. None when the history does not reach
    back to the first days of January."""
    if not values:
        return None
    year = datetime.fromtimestamp(dates[-1], UTC).year
    jan1 = datetime(year, 1, 1, tzinfo=UTC).timestamp()
    first = next(((d, v) for d, v in zip(dates, values, strict=False) if d >= jan1), None)
    if first is None or first[0] - jan1 > 10 * DAY or not first[1]:
        return None
    return (values[-1] if last is None else last) / first[1] - 1


def dxy_history(fixes: Sequence[fx_ref.Fix]) -> tuple[list[float], list[float]]:
    """The dollar index on each ECB fix: daily, all six legs from one official source."""
    pts = [(f.date, v) for f in fixes if (v := fx_ref.dxy({p: f.cross(p) or 0.0 for p in fx_ref.DXY_WEIGHTS}))]
    return [p[0] for p in pts], [p[1] for p in pts]


def yoy(dates: Sequence[float], values: Sequence[float]) -> tuple[list[float], list[float]]:
    """Year-over-year change of a monthly index, matched on the calendar month a year before."""
    by_month = {(d.year, d.month): v for d, v in ((datetime.fromtimestamp(t, UTC), v) for t, v in zip(dates, values, strict=True))}
    out_d, out_v = [], []
    for t, v in zip(dates, values, strict=True):
        d = datetime.fromtimestamp(t, UTC)
        if base := by_month.get((d.year - 1, d.month)):
            out_d.append(t)
            out_v.append(v / base - 1)
    return out_d, out_v


def annualised_growth(values: Sequence[float]) -> list[float]:
    """Quarter on quarter at an annual rate, the way the BEA headlines GDP: (Q / Q-1)^4 − 1."""
    return [(b / a) ** 4 - 1 for a, b in zip(values, values[1:], strict=False) if a > 0]


def net_liquidity_series(walcl: fred.Series, tga: fred.Series, rrp: fred.Series) -> tuple[list[float], list[float]]:
    """Net liquidity in dollars on each WALCL date, using the last known value of the other two. Dates before
    either of them has an observation are left out."""
    out_d, out_v, i, j = [], [], -1, -1
    for d, w in zip(walcl.dates, walcl.values, strict=True):
        while i + 1 < len(tga.dates) and tga.dates[i + 1] <= d:
            i += 1
        while j + 1 < len(rrp.dates) and rrp.dates[j + 1] <= d:
            j += 1
        if i >= 0 and j >= 0:
            out_d.append(d)
            out_v.append(fred.net_liquidity(w, tga.values[i], rrp.values[j]))
    return out_d, out_v


def gold_market_cap(tonnes: float, usd_per_oz: float) -> float:
    return tonnes * TROY_OZ_PER_TONNE * usd_per_oz


def btc_at_gold_share(share: float, gold_cap: float, btc_supply: float) -> float:
    """BTC's price if its market cap were `share` of gold's."""
    return gold_cap * share / btc_supply if btc_supply > 0 else 0.0


def curve_on(curves: Sequence[Curve], ts: float) -> Curve | None:
    hit = None
    for c in curves:
        if c.date > ts:
            break
        hit = c
    return hit


def spread_bp(curve: Curve | None, long: str = "BC_10YEAR", short: str = "BC_2YEAR") -> float | None:
    if curve is None or curve.get(long) is None or curve.get(short) is None:
        return None
    return (curve.rates[long] - curve.rates[short]) * 100


def glimpse_vols(forecast: Sequence[tuple[float, float, float, float]], now: float) -> list[tuple[float, float]]:
    """(seconds to close, annualised volatility in percent) for every open Glimpse close, nearest first."""
    out = []
    for end, med, lo, hi in forecast:
        iv = btcmath.implied_vol(med, lo, hi, end - now)
        if iv is not None:
            out.append((end - now, iv * 100))
    return sorted(out)


def nearest_horizon(vols: Sequence[tuple[float, float]], seconds: float, tol: float = 0.25) -> tuple[float, float] | None:
    """The close nearest a horizon, or None when the market has no close within a quarter of it."""
    if not vols:
        return None
    hit = min(vols, key=lambda v: abs(v[0] - seconds))
    return hit if abs(hit[0] - seconds) <= seconds * tol else None


def horizon(seconds: float) -> str:
    return f"{seconds / 60:.0f}m" if seconds < 5400 else f"{seconds / 3600:.0f}h" if seconds < 2 * DAY else f"{seconds / DAY:.0f}d"


def signed_usd(v: float | None) -> Text:
    if v is None or math.isnan(v):
        return Text("–", style=FAINT)
    s = ui.usd(abs(v))
    return Text(("+" if v > 0 else fmt.MINUS if v < 0 else "") + s, style=GREEN if v > 0 else RED if v < 0 else DIM)


def _now() -> float:
    """The clock DVOL reads: option expiries and Glimpse closes are measured against it, and a test can set it."""
    return time.time()


def plot_lines(plot: charts.Plot) -> list[Text]:
    return list(plot.render().split("\n"))


# ── the base ────────────────────────────────────────────────

class MarketPane(FuncPane):
    """What the ten pages share: tickers kept fresh by the hub's quote loop while the pane is open, slow fetches
    that finish in the background, and daily histories cached on the pane."""

    heading: ClassVar[str] = ""

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.title = self.heading                           # short: a 44-column frame still has room for the delay beside it
        self.watching: set[str] = set()
        self.bg: list[asyncio.Task] = []
        self.hist: dict[str, tuple[list[float], list[float], Provenance]] = {}

    def keep_fresh(self, tickers: Sequence[str]) -> None:
        new = {t.upper() for t in tickers} - self.hub.watch
        self.watching |= new
        self.hub.watch |= new

    def on_unmount(self) -> None:
        self.hub.watch -= self.watching
        self.watching = set()
        for t in self.bg:
            t.cancel()

    def later(self, coro) -> None:
        """Run a slow fetch (the Treasury takes 18 s) without holding the first paint. It stores its own result."""
        task = asyncio.ensure_future(coro)
        self.bg = [t for t in self.bg if not t.done()] + [task]
        task.add_done_callback(lambda t: (t.cancelled() or t.exception(), self.bump()))

    def bump(self) -> None:
        """A background fetch can finish while the pane is being taken down; a repaint that finds no screen is not an error."""
        try:
            super().bump()
        except Exception:
            pass

    def sources(self) -> list[Provenance]:
        """Every source behind what is on screen, one Provenance each. Read again at every draw: quotes move on."""
        return []

    async def reload(self) -> None:
        try:
            await super().reload()
        except Exception:                                   # `loading` is also a Textual reactive: clearing it on a pane that was
            return                                          # closed mid-load finds no screen. The pane is gone; nothing to show.
        self.provs = self.sources()

    async def settle(self) -> None:
        """Wait for the background fetches: tests and scripts call this; the terminal never does."""
        await asyncio.gather(*self.bg, return_exceptions=True)
        self.provs = self.sources()

    async def pull(self, tickers: Sequence[str]) -> None:
        try:
            await quotes.refresh(self.hub, list(tickers))
        except SourceError:
            pass

    async def history(self, ticker: str, days: int = 400) -> tuple[list[float], list[float], Provenance] | None:
        """Daily closes for an instrument, through its proxy when it has no source of its own. Cached on the pane."""
        t = ticker.upper()
        ins = self.hub.book.get(t)
        if ins is None or not reachable(self.hub, ins):
            return None
        try:
            if "computed:dxy" in ins.sources:
                fixes, prov = await self.hub.sources["ecb"].fixes()
                ds, vs = dxy_history(fixes)
                got = (ds, vs, Provenance("computed", prov.fetched_at, prov.as_of, DAILY, "ICE formula over six ECB fixes"))
            else:
                ds, vs, prov = await quotes.history(self.hub, ins, days)
                got = (list(ds), list(vs), relabel(prov, DAILY if cadence(prov) == LIVE else None))
        except (SourceError, KeyError):
            return self.hist.get(t)
        if got[1]:
            self.hist[t] = got
        return self.hist.get(t)

    def quote_provs(self, tickers: Sequence[str]) -> list[Provenance]:
        return [relabel(q.prov) for t in tickers if (q := self.hub.quotes.get(t.upper()))]

    def quote_key(self, tickers: Sequence[str]) -> tuple:
        return tuple(q.price for t in tickers if (q := self.hub.quotes.get(t)))


# ── QM ──────────────────────────────────────────────────────

class QmPane(MarketPane):
    code, every, selectable, tick = "QM", 60, True, 2.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.list_name = "GLOBAL"
        names = self.lists()
        want = [a.upper() for a in self.args]
        if want and want[0] in names:
            self.list_name = want[0]
        elif want and (adhoc := [i.ticker for a in want if (i := hub.book.get(a))]):
            self.custom = adhoc
            self.list_name = "CUSTOM"
        self.title = self.list_name

    custom: Sequence[str] = ()

    def lists(self) -> dict[str, list[str]]:
        """The category lists, then the user's own from `[lists]` in terminal.toml. A user list of the same name wins."""
        out = {k.upper(): list(v) for k, v in self.hub.book.lists.items()}
        for k, v in (self.hub.cfg.get("lists") or {}).items():
            if isinstance(v, list | tuple):
                out[str(k).upper()] = [str(t).upper() for t in v]
        if self.custom:
            out["CUSTOM"] = list(self.custom)
        return out

    def tickers(self) -> list[str]:
        return self.lists().get(self.list_name, [])

    def command(self) -> str:
        return " ".join(["QM", *(self.custom or [self.list_name])])

    def cache_key(self) -> tuple:
        return (self.list_name, self.quote_key(self.tickers()))

    async def load(self) -> None:
        tickers = self.tickers()
        self.title = self.list_name
        self.keep_fresh(tickers)
        await self.pull(tickers)
        self.bump()
        for t in tickers:                                   # sparklines for the live rows; daily ones carry their own
            q = self.hub.quotes.get(t)
            if q is not None and not q.history and t not in self.hist:
                await self.history(t, 30)
                self.bump()
        if tickers and not any(t in self.hub.quotes for t in tickers) and any(reachable(self.hub, self.hub.book.get(t)) for t in tickers):
            raise SourceError("no quote source answered")

    def closes(self, ticker: str) -> Sequence[float]:
        q = self.hub.quotes.get(ticker)
        if q and q.history:
            return q.history
        return self.hist[ticker][1][-30:] if ticker in self.hist else ()

    def sources(self) -> list[Provenance]:
        return distinct(self.quote_provs(self.tickers()))

    def draw(self, w: int, h: int) -> list[Text]:
        hub, tickers, names = self.hub, self.tickers(), list(self.lists())
        self.n_rows = len(tickers)
        self.provs = self.sources()
        wide = w >= 96
        spark_w = 30 if w >= 150 else 16 if w >= 110 else 10 if w >= 56 else 0
        gap = 1 if w < 60 else 2
        tw = max([6] + [len(t) for t in tickers])
        cols = [ui.Col("ticker", "left", tw, style=f"bold {ORANGE}")]
        cols += [ui.Col("name", "left", 26, style=DIM)] if w >= 140 else []
        cols += [ui.Col("last")]
        cols += [ui.Col("chg")] if w >= 72 else []
        cols += [ui.Col("% chg")]
        cols += [ui.Col("day range")] if w >= 96 else []
        cols += [ui.Col("30 closes", "left", spark_w)] if spark_w else []
        cols += [ui.Col("delay · as of" if wide else "delay", "left", flex=True)]
        heads = [c.head for c in cols]
        rows: list[list[ui.Cell] | Text] = []
        for t in tickers:
            ins, q = hub.book.get(t), hub.quotes.get(t)
            if q is None:
                rows.append(unavailable(hub, t, w, tw + gap - 1))
                continue
            cell = {"ticker": t, "name": ins.name if ins else "", "last": Text(price(ins, q.price), style=f"bold {TEXT}"),
                    "chg": delta(q.change, ins.decimals if ins else 2),
                    "% chg": pct_of(hub, t), "day range": range_cell(q, ins), "30 closes": spark_cell(self.closes(t), spark_w),
                    "delay": status(hub, t), "delay · as of": status(hub, t, True)}
            rows.append([cell.get(hd) for hd in heads])
        at = names.index(self.list_name) + 1 if self.list_name in names else 1
        out = [ui.section(f"{self.list_name} · QUOTE MONITOR" if w >= 60 else self.list_name, w, f"list {at}/{len(names)}")]
        out += grid(cols, rows, w, self.cur, gap)
        if not tickers:
            out += ui.wrap(f"The list {self.list_name} is empty. Lists live under [lists] in terminal.toml.", w, FAINT)
        if w >= 96 and h - len(out) >= 3:
            out += [Text("")] + ui.wrap("proxy: a stand-in is quoted and named, never the instrument itself · daily and monthly values show "
                                        "the date they describe · closed: the last trade of a shut session", w, FAINT)
        self.follow(self.cur + 3, h)
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            names = [n for n in self.lists() if n != "CUSTOM" or self.custom]
            i = names.index(self.list_name) if self.list_name in names else 0
            self.list_name = names[(i + (1 if ch == "]" else -1)) % len(names)]
            self.cur = self.top = 0
            self.loaded_at = 0.0                        # due at once: the shell's next tick loads the new list
            self.title = self.list_name
            return True
        return False

    def enter(self) -> str | None:
        tickers = self.tickers()
        return f"GP {tickers[self.cur]}" if tickers and self.cur < len(tickers) else None

    def hint(self) -> str:
        return "[ ] lists · enter chart"

    def menu(self) -> list[tuple[str, str]]:
        tickers = self.tickers()
        t = tickers[min(self.cur, len(tickers) - 1)] if tickers else "BTC"
        return [("GP", f"GP {t}"), ("WEI", "WEI"), ("FX", "FX"), ("GLCO", "GLCO"), ("HMAP", "HMAP")]

    def export(self):
        rows = []
        for t in self.tickers():
            q, ins = self.hub.quotes.get(t), self.hub.book.get(t)
            rows.append([t, ins.name if ins else "", q.price if q else None, q.change if q else None, q.pct if q else None,
                         q.low if q else None, q.high if q else None, q.prov.source if q else "", status(self.hub, t, True).plain,
                         show(self.hub, q.via) if q and q.via else ""])
        return ["ticker", "name", "last", "change", "pct_change", "day_low", "day_high", "source", "delay", "proxy_quoted"], rows


# ── WEI ─────────────────────────────────────────────────────

REGIONS = (("AMERICAS", ("SPX", "NDX", "RUT", "DJI")), ("EMEA", ("SX5E", "DAX", "UKX")), ("ASIA", ("NKY", "HSI", "KOSPI", "NIFTY")))


class WeiPane(MarketPane):
    code, every, tick = "WEI", 120, 5.0
    heading = "world equity indices"

    def tickers(self) -> list[str]:
        return [t for _, ts in REGIONS for t in ts]

    def cache_key(self) -> tuple:
        return self.quote_key(self.tickers() + ["SPY", "QQQ"])

    async def load(self) -> None:
        tickers = self.tickers()
        self.keep_fresh(tickers)
        await self.pull(tickers)
        self.bump()
        for t in tickers:                                   # year to date needs the first close of the year
            ins = self.hub.book.get(t)
            if ins and ins.sources:
                await self.history(t, 400)
            if ins and ins.proxy and (pq := self.hub.quotes.get(ins.proxy)) and pq.via:
                await self.history(pq.via, 400)
        if not any(t in self.hub.quotes for t in tickers):
            raise SourceError("no index source answered")

    def ytd_of(self, ticker: str, last: float | None) -> float | None:
        h = self.hist.get(ticker.upper())
        if not h or not h[1] or (last and abs(last / h[1][-1] - 1) > 0.25):     # a history on another scale is not this instrument's
            return None
        return ytd(h[0], h[1], last)

    def proxy_of(self, ticker: str):
        """The ETF quoted beneath an index: (ETF ticker, its quote). A quote here is always a labelled stand-in."""
        ins = self.hub.book.get(ticker)
        q = self.hub.quotes.get(ins.proxy) if ins and ins.proxy else None
        return (ins.proxy, q) if q is not None and ins is not None else None

    def sources(self) -> list[Provenance]:
        tickers = self.tickers()
        return distinct(self.quote_provs([t for t in tickers if t in self.hub.quotes] + [p[0] for t in tickers if (p := self.proxy_of(t))]))

    def draw(self, w: int, h: int) -> list[Text]:
        hub = self.hub
        self.provs = self.sources()
        wide = w >= 84
        gap = 1 if w < 60 else 2
        cols = [ui.Col("index", "left", 5, style=f"bold {ORANGE}")]
        cols += [ui.Col("name", "left", 28, style=DIM)] if w >= 110 else []
        cols += [ui.Col("last")]
        cols += [ui.Col("chg")] if w >= 64 else []
        cols += [ui.Col("% chg"), ui.Col("YTD")]
        cols += [ui.Col("30 closes", "left", 14)] if w >= 120 else []
        cols += [ui.Col("delay · as of" if wide else "delay", "left", flex=True)]
        heads = [c.head for c in cols]
        rows: list[list[ui.Cell] | Text] = []
        for region, tickers in REGIONS:
            rows.append(ui.section(region, w))
            for t in tickers:
                ins, q = hub.book.get(t), hub.quotes.get(t)
                own = q is not None and not q.via
                if own:
                    tight = w < 44                          # 40 columns: whole index points above 10,000, one decimal of change
                    cell = {"index": t, "name": ins.name if ins else "",
                            "last": Text(price(ins, q.price, 0 if tight and q.price >= 10_000 else None), style=f"bold {TEXT}"),
                            "chg": delta(q.change, 2), "% chg": delta_pct(q.pct, 1 if tight else 2),
                            "YTD": delta_pct(self.ytd_of(t, q.price)),
                            "30 closes": spark_cell(q.history, 14),
                            "delay": status(hub, t), "delay · as of": status(hub, t, True)}
                    rows.append([cell.get(hd) for hd in heads])
                proxy = self.proxy_of(t)
                if proxy and proxy[1].via:
                    rows.append(self._proxy_line(t, proxy[0], proxy[1], own, w))
                elif not own:
                    rows.append(unavailable(hub, t, w, 4 + gap))
        out = grid(cols, rows, w, None, gap)
        if h - len(out) >= 2 or w >= 100:
            out += [Text("")] + ui.wrap("An index level is its official daily close. The line beneath it is a tokenised ETF trading now: a "
                                        "proxy, in its own price, never the index.", w, FAINT)
        return out

    def _proxy_line(self, index: str, etf: str, q, own: bool, w: int) -> Text:
        hub = self.hub
        lead = "  " if own else f"{index:<{5 + (1 if w < 60 else 2)}}"
        out = ui.t((lead, FAINT if own else f"bold {FAINT}"), (f"{etf} via {show(hub, q.via)}", DIM), ("  ", ""),
                   (ui.px(q.price, 2), TEXT), (" ", ""), delta_pct(q.pct, 2))
        y = self.ytd_of(q.via, q.price)
        if w >= 64 and y is not None:
            out.append("  YTD ", style=FAINT)
            out.append_text(delta_pct(y))
        out.append(" · " if w >= 64 else " ", style=FAINT)
        out.append("proxy", style=ui.YELLOW)
        out.append(" · " if w >= 64 else " ", style=FAINT)
        out.append("closed" if q.closed else "live", style=DIM if q.closed else GREEN)
        if not own and w >= 64:
            out.append(f" · {index} itself has no open source", style=FAINT)
        return out

    def hint(self) -> str:
        return "daily closes · proxies labelled"

    def menu(self) -> list[tuple[str, str]]:
        return [("QM", "QM INDICES"), ("GP", "GP SPX"), ("HMAP", "HMAP"), ("FX", "FX"), ("RATES", "RATES")]

    def export(self):
        rows = []
        for region, tickers in REGIONS:
            for t in tickers:
                q, proxy = self.hub.quotes.get(t), self.proxy_of(t)
                own = q is not None and not q.via
                rows.append([region, t, q.price if own else None, q.change if own else None, q.pct if own else None,
                             self.ytd_of(t, q.price) if own else None, q.prov.source if own else "", status(self.hub, t, True).plain if own
                             else "no open source", f"{proxy[0]} via {show(self.hub, proxy[1].via)}" if proxy and proxy[1].via else "",
                             proxy[1].price if proxy and proxy[1].via else None])
        return ["region", "index", "last", "change", "pct_change", "ytd", "source", "delay", "proxy", "proxy_last"], rows


# ── FX ──────────────────────────────────────────────────────

class FxPane(MarketPane):
    code, every, tick = "FX", 60, 2.0
    heading = "currencies"

    def pairs(self) -> list[str]:
        return [i.ticker for i in self.hub.book.list("FX")] or list(fx_ref.DXY_WEIGHTS)

    def cache_key(self) -> tuple:
        return self.quote_key(self.pairs() + ["DXY", "BTC"])

    async def load(self) -> None:
        want = self.pairs() + ["DXY", "BTC"]
        self.keep_fresh(want)
        await self.pull(want)
        if not any(p in self.hub.quotes for p in self.pairs()):
            raise SourceError("no FX source answered")

    def btc_in(self, pair: str) -> tuple[str, float, float | None] | None:
        """BTC priced in the pair's other currency: the composite BTC/USD times USDxxx, or divided by xxxUSD."""
        btc, q = self.hub.quotes.get("BTC"), self.hub.quotes.get(pair)
        if not (btc and q and q.price > 0):
            return None
        inverse = pair.endswith("USD")
        ccy = pair[:3] if inverse else pair[3:6]
        v = btc.price / q.price if inverse else btc.price * q.price
        pct = None
        if btc.pct is not None and q.pct is not None:
            pct = (1 + btc.pct) / (1 + q.pct) - 1 if inverse else (1 + btc.pct) * (1 + q.pct) - 1
        return ccy, v, pct

    def sources(self) -> list[Provenance]:
        return distinct(self.quote_provs(self.pairs() + ["DXY", "BTC"]))

    def draw(self, w: int, h: int) -> list[Text]:
        hub, pairs = self.hub, self.pairs()
        self.provs = self.sources()
        wide, gap = w >= 84, 1 if w < 60 else 2
        cols = [ui.Col("pair", "left", 6, style=f"bold {ORANGE}")]
        cols += [ui.Col("name", "left", 30, style=DIM)] if w >= 140 else []
        cols += [ui.Col("last")]
        cols += [ui.Col("chg")] if w >= 64 else []
        cols += [ui.Col("% chg")]
        cols += [ui.Col("day range")] if w >= 96 else []
        cols += [ui.Col("delay · as of" if wide else "delay", "left", flex=True)]
        heads = [c.head for c in cols]
        rows: list[list[ui.Cell] | Text] = []
        for t in pairs + ["DXY"]:
            ins, q = hub.book.get(t), hub.quotes.get(t)
            if q is None:
                rows.append(unavailable(hub, t, w, 5 + gap))
                continue
            cell = {"pair": t, "name": ins.name if ins else "", "last": Text(price(ins, q.price), style=f"bold {TEXT}"),
                    "chg": delta(q.change, ins.decimals if ins else 4), "% chg": delta_pct(q.pct, 2), "day range": range_cell(q, ins),
                    "delay": status(hub, t), "delay · as of": status(hub, t, True)}
            rows.append([cell.get(hd) for hd in heads])
        out = [ui.section("PAIRS", w, "per unit of the first currency")] + grid(cols, rows, w, None, gap)

        dxy = hub.quotes.get("DXY")
        out += [Text(""), ui.section("DOLLAR INDEX", w, "computed" if w < 60 else "computed · ICE formula, six legs")]
        if dxy:
            out.append(ui.t((ui.px(dxy.price, 3), f"bold {ORANGE}"), ("  ", ""), delta_pct(dxy.pct, 2), ("  computed", ui.CYAN),
                            (f" · as of {asof(dxy.prov.as_of)}" if dxy.prov.delay != LIVE else "", FAINT)))
            out += ui.wrap(f"legs: {dxy_mix(hub)}", w, FAINT)
        else:
            out.append(ui.note("computed once all six legs have a quote"))
        legs = []
        for pair, weight in fx_ref.DXY_WEIGHTS.items():
            q = hub.quotes.get(pair)
            legs.append([pair, f"{abs(weight) * 100:.1f}%", f"{weight:+.3f}".replace("-", fmt.MINUS),
                         price(hub.book.get(pair), q.price) if q else "–", delta_pct(q.pct, 2) if q else None, status(hub, pair, wide)])
        leg_cols = [ui.Col("leg", "left", style=f"bold {TEXT}"), ui.Col("weight"), ui.Col("exponent", style=DIM), ui.Col("rate"),
                    ui.Col("% chg"), ui.Col("delay", "left", flex=True)]
        if w < 60:
            leg_cols, legs = leg_cols[:2] + leg_cols[3:], [r[:2] + r[3:] for r in legs]
        out += ui.table(leg_cols, legs, w, None, gap)

        btc = hub.quotes.get("BTC")
        out += [Text(""), ui.section("BTC IN EACH CURRENCY", w, "composite BTC/USD × the pair")]
        brow: list[list[ui.Cell]] = []
        if btc:
            brow.append(["USD", Text(ui.px(btc.price, 0), style=f"bold {ORANGE}"), delta_pct(btc.pct, 2), status(hub, "BTC", wide)])
        for pair in pairs:
            if hit := self.btc_in(pair):
                ccy, v, pct = hit
                q = hub.quotes[pair]
                tag = status(hub, pair, wide)
                if q.via:                               # BTC/CNH through the onshore rate is BTC/CNY, and says so
                    ccy = show(hub, q.via)[3:6] or ccy
                brow.append([ccy, Text(ui.px(v, 0), style=f"bold {TEXT}"), delta_pct(pct, 2), tag])
        if brow:
            out += ui.table([ui.Col("in", "left", style=f"bold {ORANGE}"), ui.Col("1 BTC"), ui.Col("% chg"),
                             ui.Col("delay of the pair", "left", flex=True)], brow, w, None, gap)
        else:
            out += ui.wrap("needs the BTC composite and at least one pair", w, FAINT)
        if wide:
            out += [Text("")] + ui.wrap(
                "EUR, GBP, CAD, CHF and AUD trade live on Kraken's spot FX books. The yen and the krona have no usable open book, so they "
                "are the ECB's daily reference fix, published about 16:00 CET. USDCNH has no open feed: the onshore USDCNY fix stands in, "
                "labelled.", w, FAINT)
        return out

    def hint(self) -> str:
        return "JPY SEK are daily ECB fixes"

    def menu(self) -> list[tuple[str, str]]:
        return [("QM", "QM FX"), ("GP", "GP EURUSD"), ("DXY", "GP DXY"), ("RATES", "RATES"), ("HMAP", "HMAP")]

    def export(self):
        rows = []
        for t in self.pairs() + ["DXY"]:
            q = self.hub.quotes.get(t)
            b = self.btc_in(t) if t != "DXY" else None
            rows.append([t, q.price if q else None, q.pct if q else None, q.prov.source if q else "", status(self.hub, t, True).plain,
                         fx_ref.DXY_WEIGHTS.get(t), b[1] if b else None])
        return ["pair", "last", "pct_change", "source", "delay", "dxy_exponent", "btc_in_currency"], rows


# ── GLCO ────────────────────────────────────────────────────

FUTURES_UNIT = {"GC=F": "$/oz", "SI=F": "$/oz", "HG=F": "$/lb", "CL=F": "$/bbl", "BZ=F": "$/bbl", "NG=F": "$/MMBtu",
                "ZC=F": "¢/bu", "ZW=F": "¢/bu", "ZS=F": "¢/bu", "SB=F": "¢/lb"}      # front-month futures, as the exchange quotes them


class GlcoPane(MarketPane):
    code, every, tick = "GLCO", 120, 5.0
    heading = "commodities"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.series: dict[str, fred.Series] = {}

    def tickers(self) -> list[str]:
        return [i.ticker for i in self.hub.book.list("COMMODITIES")]

    def cache_key(self) -> tuple:
        return self.quote_key(["XAU", "BTC"])

    async def load(self) -> None:
        hub = self.hub
        want = [*self.tickers(), "BTC"]
        self.keep_fresh(want)
        await self.pull(want)                               # futures through Yahoo when it is on: one request for all of them
        self.bump()
        last: SourceError | None = None
        for t in self.tickers():                            # otherwise the EIA and IMF series, straight from FRED
            ins = hub.book.get(t)
            sid = ins.source("fred") if ins else None
            if not sid or self.futures(t):
                continue
            try:
                self.series[t] = await hub.sources["fred"].series(sid, start=start_years(2))
                self.bump()
            except (SourceError, KeyError) as e:
                last = e if isinstance(e, SourceError) else last
        if not self.series and "XAU" not in hub.quotes:
            raise last or SourceError("no commodity source answered")

    def futures(self, ticker: str):
        """The Yahoo futures quote for this commodity, when there is one."""
        q = self.hub.quotes.get(ticker)
        return q if q and q.prov.source == "yahoo" and not q.proxy_for else None

    def unit(self, ticker: str) -> str:
        ins = self.hub.book.get(ticker)
        if self.futures(ticker) and ins and (sym := ins.source("yahoo")):
            return FUTURES_UNIT.get(sym, "")
        if ticker == "XAU":
            return "$/oz"
        ins = self.hub.book.get(ticker)
        return FRED_META.get((ins.source("fred") if ins else "") or "", ("", ""))[0]

    def sources(self) -> list[Provenance]:
        return distinct(self.quote_provs([*self.tickers(), "BTC"]) + [relabel(s.prov) for t, s in self.series.items() if not self.futures(t)])

    def draw(self, w: int, h: int) -> list[Text]:
        hub = self.hub
        gold, btc = hub.quotes.get("XAU"), hub.quotes.get("BTC")
        self.provs = self.sources()
        wide, gap = w >= 84, 1 if w < 60 else 2
        spark_w = 24 if w >= 130 else 12 if w >= 72 else 0
        cols = [ui.Col("cmdty", "left", 8, style=f"bold {ORANGE}")]
        cols += [ui.Col("name", "left", 22, style=DIM)] if w >= 132 else []
        cols += [ui.Col("last"), ui.Col("unit", "left", style=FAINT)]
        cols += [ui.Col("% chg")] if w >= 54 else []
        cols += [ui.Col("30 obs", "left", spark_w)] if spark_w else []
        cols += [ui.Col("delay · as of" if wide else "delay", "left", flex=True)]
        heads = [c.head for c in cols]
        rows: list[list[ui.Cell] | Text] = []
        for t in self.tickers():
            ins, s = hub.book.get(t), self.series.get(t)
            if fq := self.futures(t):
                cell = {"cmdty": t, "name": ins.name if ins else "", "last": Text(ui.px(fq.price, 2), style=f"bold {TEXT}"),
                        "unit": self.unit(t), "% chg": delta_pct(fq.pct, 2), "30 obs": spark_cell(fq.history, spark_w),
                        "delay": status(hub, t), "delay · as of": status(hub, t, True)}
            elif t == "XAU" and gold:
                cell = {"cmdty": t, "name": ins.name if ins else "", "last": Text(ui.px(gold.price, 2), style=f"bold {TEXT}"), "unit": "$/oz",
                        "% chg": delta_pct(gold.pct, 2), "30 obs": spark_cell(gold.history, spark_w),
                        "delay": status(hub, t), "delay · as of": status(hub, t, True)}
            elif s and s.last is not None:
                cad = cadence(s.prov)
                tag = ui.t((cad, ui.YELLOW if cad == MONTHLY else DIM), (f" {asof(s.prov.as_of, cad, wide and cad == MONTHLY)}", FAINT),
                           (" average" if wide and cad == MONTHLY else "", FAINT))
                cell = {"cmdty": t, "name": ins.name if ins else "",
                        "last": Text(ui.px(s.last, ins.decimals if ins else 2), style=f"bold {TEXT}"), "unit": self.unit(t),
                        "% chg": delta_pct(s.last / s.prev - 1 if s.prev else None, 1),
                        "30 obs": spark_cell(s.values[-30:], spark_w), "source": s.prov.source, "delay": tag, "delay · as of": tag}
            else:
                rows.append(unavailable(hub, t, w, 7 + gap))
                continue
            rows.append([cell.get(hd) for hd in heads])
        out = grid(cols, rows, w, None, gap)
        via = f"gold via {show(hub, gold.via)} · proxy" if gold and gold.via else ""
        out += ([Text("")] if h >= 18 else []) + [ui.section("GOLD IN BTC", w, via)]
        if gold and btc and gold.price > 0 and btc.price > 0:
            out.append(ui.t(ui.kv("1 BTC =", f"{btc.price / gold.price:,.2f} oz"), ("   ", ""),
                            ui.kv("1 oz =", f"{gold.price / btc.price:.5f} BTC"),
                            (f"   {fmt.sats(gold.price / btc.price * 1e8)}" if w >= 60 else "", f"bold {ORANGE}")))
            if wide and gold.via:
                out += ui.wrap("Gold is PAX Gold here, a token of one troy ounce that trades through the weekend and can sit a little "
                               "off spot.", w, FAINT)
        else:
            out += ui.wrap("needs the BTC composite and the gold proxy", w, FAINT)
        if w >= 60 and any(self.futures(t) for t in self.tickers()):
            out += [Text("")] + ui.wrap("Front-month futures: COMEX metals, NYMEX energy, CBOT grains, ICE sugar. Grains are in US "
                                        "cents a bushel, as the exchange quotes them. HELP GLCO names the feed behind each row.", w, FAINT)
        elif w >= 60:
            out += [Text("")] + ui.wrap(
                "Oil and gas are official spot prices, daily and a few days late. Copper, the grains and sugar are IMF monthly "
                "averages, about two months late: the month is shown. The % change of a monthly row is month on month.", w, FAINT)
        return out

    def hint(self) -> str:
        return "futures" if self.futures("XAU") else "gold is PAXG · grains monthly"

    def menu(self) -> list[tuple[str, str]]:
        return [("QM", "QM COMMODITIES"), ("RV", "RV"), ("GP", "GP XAU"), ("BRENT", "GP BRENT"), ("HMAP", "HMAP")]

    def export(self):
        rows = []
        for t in self.tickers():
            s, q = self.series.get(t), self.hub.quotes.get(t)
            if s and s.last is not None:
                rows.append([t, s.last, self.unit(t), s.prov.source, cadence(s.prov), asof(s.prov.as_of, DAILY, True), ""])
            elif q:
                rows.append([t, q.price, self.unit(t), q.prov.source, cadence(q.prov), "", show(self.hub, q.via)])
            else:
                rows.append([t, None, "", "", "no open source", "", ""])
        return ["commodity", "last", "unit", "source", "delay", "as_of", "proxy_quoted"], rows


# ── RATES ───────────────────────────────────────────────────

RATE_SERIES = (("DFF", "fed funds", "effective, daily"), ("SOFR", "SOFR", "secured overnight"), ("DFII10", "10Y real", "TIPS yield"),
               ("T10YIE", "10Y breakeven", "nominal less real"), ("T10Y2Y", "2s10s", "FRED's own"))
BACK = (("1w", 7, ui.CYAN), ("1m", 30, ui.VIOLET), ("1y", 365, YEAR_LINE))


class RatesPane(MarketPane):
    code, every, tick = "RATES", 1800, 30.0
    heading = "Treasury curve"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.curves: dict[float, Curve] = {}
        self.real: list[Curve] = []
        self.series: dict[str, fred.Series] = {}
        self.tsy_prov: Provenance | None = None
        self.tsy_error = ""
        self.asked: set[int] = set()

    async def load(self) -> None:
        hub = self.hub
        tsy = hub.sources.get("treasury")
        if tsy is not None:                                 # each file takes the Treasury about 18 s: none of them holds the paint
            self._years(tsy, datetime.now(UTC).timestamp())
            self.later(self._real(tsy))
        for sid, _, _ in RATE_SERIES:
            try:
                self.series[sid] = await hub.sources["fred"].series(sid, start=start_years(2))
            except (SourceError, KeyError):
                pass
        self.bump()
        if tsy is not None:
            await self._curves(tsy.recent(), keep_prov=True)
            if self.curves:                                 # early in January the curve a month or a year back sits one file further
                self._years(tsy, max(self.curves))
        if not self.curves and not self.series:
            raise SourceError(self.tsy_error or "neither the Treasury nor FRED answered")

    def _years(self, tsy, latest: float) -> None:
        """Ask, once each and in the background, for the year files that hold the curve a month and a year before `latest`."""
        for back in (30, 365):
            year = datetime.fromtimestamp(latest - back * DAY, UTC).year
            if year not in self.asked:
                self.asked.add(year)
                self.later(self._curves(tsy.curves(year=year)))

    async def _curves(self, call, keep_prov: bool = False) -> None:
        try:
            days, prov = await call
        except SourceError as e:
            self.tsy_error = str(e)
            return
        self.curves.update({c.date: c for c in days})
        if keep_prov or self.tsy_prov is None:
            self.tsy_prov = prov

    async def _real(self, tsy) -> None:
        try:
            self.real, _ = await tsy.recent("daily_treasury_real_yield_curve")
        except SourceError:
            pass

    def picks(self) -> list[tuple[str, str, Curve | None]]:
        """(label, colour, curve) for today and a week, a month and a year before the latest business day."""
        days = sorted(self.curves.values(), key=lambda c: c.date)
        if not days:
            return []
        today = days[-1]
        out = [("today", ORANGE, today)]
        for label, back, colour in BACK:
            c = curve_on(days, today.date - back * DAY)
            out.append((label, colour, c if c is not None and today.date - c.date <= (back + 10) * DAY else None))
        return out

    def real_10y(self) -> tuple[float, float, str] | None:
        """(value, as-of, source): the Treasury's real curve when it has arrived and is newer, else FRED DFII10."""
        s = self.series.get("DFII10")
        best = (s.last, s.dates[-1], "fred:DFII10") if s and s.last is not None else None
        if self.real and (v := self.real[-1].get("TC_10YEAR")) is not None and (best is None or self.real[-1].date >= best[1]):
            best = (v, self.real[-1].date, "treasury.gov real curve")
        return best

    def sources(self) -> list[Provenance]:
        return distinct([self.tsy_prov if self.curves else None] + [relabel(s.prov) for s in self.series.values()])

    def draw(self, w: int, h: int) -> list[Text]:
        picks = self.picks()
        self.provs = self.sources()
        out: list[Text] = []
        today = picks[0][2] if picks else None
        tenors = [(label, field) for label, field, _ in TENORS if today is not None and today.get(field) is not None]
        if today is not None and len(tenors) >= 3:
            out.append(ui.t(("PAR CURVE ", f"bold {ORANGE}"), (asof(today.date, DAILY, w >= 60), f"bold {TEXT}"),
                            (" · official daily close" if w >= 56 else " · daily", FAINT)))
            ch = max(5, min(h - 9, 16))
            plot = charts.Plot(w, ch + 1, times=False, gutter=6)
            plot.fmt = lambda v: f"{v:.2f}"
            for label, colour, c in reversed(picks):        # today last, so it draws over the others
                if c is not None:
                    plot.line(list(range(len(tenors))), [c.get(f) for _, f in tenors], colour, label=label)
            body = plot_lines(plot)[:-1]
            pw = max(w - 6, 4)
            axis = [" "] * pw
            for i, (label, _) in enumerate(tenors):
                at = min(max(round(i / (len(tenors) - 1) * (pw - 1)) - len(label) // 2, 0), pw - len(label))
                if all(ch_ == " " for ch_ in axis[max(at - 1, 0):at + len(label) + 1]):
                    axis[at:at + len(label)] = label
            out += body + [Text("".join(axis), style=FAINT, no_wrap=True)]
            legend = Text(no_wrap=True)
            for label, colour, c in picks:
                legend.append("━ ", style=charts.snap(colour))
                legend.append(f"{label} " if c is not None else f"{label} loading " if self.bg and not all(t.done() for t in self.bg)
                              else f"{label} n/a ", style=FAINT)
                if c is not None and w >= 72:
                    legend.append(f"{asof(c.date, DAILY, label == '1y')}   ", style="#6a6a6a")
            out.append(legend)
        elif self.tsy_error:
            out += ui.wrap(f"The Treasury curve did not load: {self.tsy_error}. FRED's rates are below.", w, RED)
        else:
            out += ui.wrap("The Treasury's server takes about 18 s. The curve draws when it answers.", w, FAINT)

        stats: list[Text] = []
        s2s10 = spread_bp(today)
        if s2s10 is None and (s := self.series.get("T10Y2Y")) and s.last is not None:
            s2s10 = s.last * 100
        if s2s10 is not None:
            wk = spread_bp(picks[1][2]) if len(picks) > 1 else None
            stats.append(ui.t(ui.kv("2s10s", f"{s2s10:+.0f}bp".replace("-", fmt.MINUS), f"bold {GREEN if s2s10 >= 0 else RED}"),
                              *(((" 1w ", FAINT), delta(s2s10 - wk, 0)) if wk is not None else ())))
        tight, days = w < 60, []                            # a narrow pane drops each date and says the span of them once
        for sid, label, _ in RATE_SERIES[:2]:
            if (s := self.series.get(sid)) and s.last is not None:
                stats.append(ui.t(ui.kv("FF" if tight and sid == "DFF" else label, f"{s.last:.2f}%"),
                                  ("" if tight else f" {asof(s.dates[-1])}", FAINT)))
                days.append(s.dates[-1])
        if real := self.real_10y():
            stats.append(ui.t(ui.kv("10Y real", f"{real[0]:.2f}%"), ("" if tight else f" {asof(real[1])}", FAINT)))
            days.append(real[1])
        if (s := self.series.get("T10YIE")) and s.last is not None:
            stats.append(ui.t(ui.kv("BE" if tight else "10Y breakeven", f"{s.last:.2f}%"), ("" if tight else f" {asof(s.dates[-1])}", FAINT)))
            days.append(s.dates[-1])
        if tight and days:
            a, b = asof(min(days)), asof(max(days))
            stats.append(Text(f"daily {a}" if a == b else f"daily {a[:2]} to {b}", style=FAINT, no_wrap=True))
        line = Text(no_wrap=True)
        for part in stats:                                  # flow the key rates into as few lines as the width allows
            if line.cell_len and line.cell_len + 3 + part.cell_len > w:
                out.append(line)
                line = Text(no_wrap=True)
            if line.cell_len:
                line.append("  " if tight else "   ")
            line.append_text(part)
        if line.cell_len:
            out.append(line)

        if today is not None:
            out += [Text(""), ui.section("BY TENOR", w, "percent · changes in bp")]
            wide = w >= 72
            cols = [ui.Col("tenor", "left", style=f"bold {ORANGE}"), ui.Col("today", style=f"bold {TEXT}")]
            for label, _, _ in picks[1:]:
                cols += ([ui.Col(label, style=DIM)] if wide else []) + [ui.Col(f"Δ{label}")]
            rows = []
            for label, field in tenors:
                row: list[ui.Cell] = [label, f"{today.rates[field]:.2f}"]
                for _, _, c in picks[1:]:
                    then = c.get(field) if c is not None else None
                    row += ([f"{then:.2f}" if then is not None else "–"] if wide else [])
                    row.append(delta((today.rates[field] - then) * 100, 0) if then is not None else None)
                rows.append(row)
            out += ui.table(cols, rows, w, None, 1 if w < 60 else 2)
        if self.series and (w >= 72 or today is None):
            out += [Text(""), ui.section("POLICY AND REAL RATES", w, "daily")]
            rows = []
            for sid, label, what in RATE_SERIES:
                if (s := self.series.get(sid)) and s.last is not None:
                    rows.append([label, f"{s.last:.2f}%", delta((s.last - s.prev) * 100 if s.prev is not None else None, 0),
                                 spark_cell(s.values[-60:], 20), asof(s.dates[-1], DAILY, True), what])
            out += ui.table([ui.Col("rate", "left", style=f"bold {TEXT}"), ui.Col("last"), ui.Col("Δbp"), ui.Col("60 days", "left", 20),
                             ui.Col("as of", "left", style=DIM), ui.Col("", "left", flex=True, style=FAINT)], rows, w)
        return out

    def hint(self) -> str:
        return "official daily closes"

    def menu(self) -> list[tuple[str, str]]:
        return [("QM", "QM RATES"), ("GP", "GP US10Y"), ("MACRO", "MACRO"), ("ECO", "ECO"), ("FX", "FX")]

    def export(self):
        picks = self.picks()
        if not picks:
            return ["series", "last", "as_of"], [[sid, s.last, asof(s.dates[-1], DAILY, True)] for sid, s in self.series.items() if s.values]
        head = ["tenor"] + [f"{label}_{asof(c.date, DAILY, True).replace(' ', '_')}" if c else label for label, _, c in picks]
        return head, [[label] + [c.get(field) if c else None for _, _, c in picks] for label, field, _ in TENORS]


# ── MACRO ───────────────────────────────────────────────────

LIQUIDITY = (("WALCL", "Fed", "Fed balance sheet"), ("WTREGEN", "TGA", "Treasury General Account"),
             ("RRPONTSYD", "RRP", "overnight reverse repo"), ("M2SL", "M2", "M2 money stock"))
WINDOWS = ((1, "1y"), (2, "2y"), (5, "5y"))


class MacroPane(MarketPane):
    code, every, tick = "MACRO", 3600, 30.0
    heading = "dollar liquidity"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.series: dict[str, fred.Series] = {}
        self.net: tuple[list[float], list[float]] = ([], [])
        self.span = 0

    async def load(self) -> None:
        hub, last = self.hub, None
        for sid, _, _ in LIQUIDITY:
            try:
                self.series[sid] = await hub.sources["fred"].series(sid, start=start_years(6))
            except (SourceError, KeyError) as e:
                last = e
        if all(k in self.series for k in ("WALCL", "WTREGEN", "RRPONTSYD")):
            self.net = net_liquidity_series(self.series["WALCL"], self.series["WTREGEN"], self.series["RRPONTSYD"])
        self.bump()
        await self.history("BTC", 400)
        if not self.series:
            raise SourceError(str(last) if last else "FRED did not answer")

    @staticmethod
    def changes(dates: Sequence[float], values: Sequence[float]) -> tuple[float | None, float | None]:
        """(4-week change, 52-week change) against the observation at or before 28 and 364 days earlier."""
        if not values:
            return None, None
        out = []
        for back in (28, 364):
            base = at_or_before(dates, values, dates[-1] - back * DAY)
            out.append(values[-1] - base[1] if base else None)
        return out[0], out[1]

    def rows(self) -> list[tuple[str, str, str, float, float | None, float | None, float, str]]:
        """(id, short, name, latest $, 4w $, 52w $, as-of, cadence), net liquidity first."""
        out = []
        if self.net[1]:
            d4, d52 = self.changes(*self.net)
            out.append(("computed", "NET", "Fed − TGA − RRP", self.net[1][-1], d4, d52, self.net[0][-1], WEEKLY))
        for sid, short, name in LIQUIDITY:
            if (s := self.series.get(sid)) and s.values:
                vals = [fred.dollars(sid, v) for v in s.values]
                d4, d52 = self.changes(s.dates, vals)
                out.append((sid, short, name, vals[-1], d4, d52, s.dates[-1], cadence(s.prov)))
        return out

    def sources(self) -> list[Provenance]:
        return distinct([relabel(s.prov) for s in self.series.values()] + ([self.hist["BTC"][2]] if "BTC" in self.hist else []), False)

    def draw(self, w: int, h: int) -> list[Text]:
        btc = self.hist.get("BTC")
        self.provs = self.sources()
        rows = self.rows()
        if not rows:
            return []
        out: list[Text] = []
        years, label = WINDOWS[self.span]
        if self.net[1]:
            d4 = rows[0][4]
            out.append(ui.t(("NET LIQUIDITY ", f"bold {ORANGE}"), (ui.usd(self.net[1][-1]), f"bold {TEXT}"), ("  4w ", FAINT), signed_usd(d4),
                            (f"  computed · weekly · {asof(self.net[0][-1])}" if w >= 72 else "", FAINT)))
            start = self.net[0][-1] - years * 365.25 * DAY
            pts = [(d, v) for d, v in zip(*self.net, strict=True) if d >= start]
            ch = max(4, min(h - 10, 18))
            plot = charts.Plot(w, ch + 1)
            xs, ys = charts.resample([p[0] for p in pts], [p[1] for p in pts], max(w, 20) * 2)
            plot.line(xs, ys, ORANGE, label="net liquidity")
            seen = [(d, v) for d, v in zip(btc[0], btc[1], strict=True) if d >= start] if btc else []
            if seen:
                bx, by = charts.resample([p[0] for p in seen], [p[1] for p in seen], max(w, 20) * 2)
                plot.line(bx, by, ui.CYAN, axis=1, label="BTC, daily close")
            out += plot_lines(plot)
            out.append(ui.t(("━ ", ORANGE), ("net liq (right) ", FAINT), ("━ ", ui.CYAN), ("BTC (left) " if btc else "BTC loading ", FAINT),
                            (f"· {label}", FAINT)))
        wide = w >= 84
        cols = [ui.Col("", "left", style=f"bold {ORANGE}")]
        named = w >= 110
        cols += [ui.Col("series", "left", style=DIM)] if named else []
        cols += [ui.Col("latest", style=f"bold {TEXT}"), ui.Col("4w chg"), ui.Col("52w chg"), ui.Col("as of", "left", style=DIM)]
        cols += [ui.Col("every", "left", style=FAINT), ui.Col("source · unit at source", "left", flex=True, style=FAINT)] if wide else []
        body = []
        for sid, short, name, latest, d4, d52, when, cad in rows:
            row: list[ui.Cell] = [short] + ([name] if named else []) + [ui.usd(latest), signed_usd(d4), signed_usd(d52), asof(when, cad, wide)]
            if wide:
                row += [cad, "computed, units converted" if sid == "computed" else f"fred:{sid} · {FRED_META[sid][0]}"]
            body.append(row)
        out += [Text("")] if self.net[1] and h >= 20 else []
        out += ui.table(cols, body, w, None, 1 if w < 60 else 2)
        if w >= 60:
            out += [Text("")] + ui.wrap(
                "Net liquidity is the Fed's balance sheet less what sits in the Treasury's account and in the overnight reverse repo "
                "facility. FRED publishes WALCL and WTREGEN in millions of dollars and RRPONTSYD in billions: they are converted to dollars "
                "before subtracting. Weekly points fall on WALCL's Wednesdays with the last known value of the other two.", w, FAINT)
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            self.span = (self.span + (1 if ch == "]" else -1)) % len(WINDOWS)
            return True
        return False

    def cache_key(self) -> tuple:
        return (self.span,)

    def hint(self) -> str:
        return "[ ] 1y 2y 5y"

    def menu(self) -> list[tuple[str, str]]:
        return [("RATES", "RATES"), ("ECO", "ECO"), ("GP", "GP BTC"), ("FX", "FX"), ("RV", "RV")]

    def export(self):
        return (["series", "name", "latest_usd", "chg_4w_usd", "chg_52w_usd", "as_of", "frequency"],
                [[sid, name, latest, d4, d52, asof(when, DAILY, True), cad] for sid, _, name, latest, d4, d52, when, cad in self.rows()])


# ── ECO ─────────────────────────────────────────────────────

ECO_SERIES = ("CPIAUCSL", "UNRATE", "PAYEMS", "GDPC1", "GDP", "DFF")


class EcoPane(MarketPane):
    code, every, tick = "ECO", 3600, 60.0
    heading = "US economic prints"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.series: dict[str, fred.Series] = {}

    async def load(self) -> None:
        last = None
        for sid in ECO_SERIES:
            try:
                self.series[sid] = await self.hub.sources["fred"].series(sid, start=start_years(4))
                self.bump()
            except (SourceError, KeyError) as e:
                last = e
        if not self.series:
            raise SourceError(str(last) if last else "FRED did not answer")

    def prints(self) -> list[dict[str, Any]]:
        """One row a print: the value, the one before, the period it describes and a history for the sparkline."""
        out: list[dict[str, Any]] = []

        def add(sid: str, label: str, name: str, dates, values, unit: str, cad: str, digits: int = 1) -> None:
            if len(values) >= 1:
                out.append({"id": sid, "label": label, "name": name, "value": values[-1], "prev": values[-2] if len(values) > 1 else None,
                            "date": dates[-1], "unit": unit, "cad": cad, "digits": digits, "hist": list(values[-36:])})

        if s := self.series.get("CPIAUCSL"):
            d, v = yoy(s.dates, s.values)
            add("CPIAUCSL", "CPI YoY", "consumer prices, year on year", d, [x * 100 for x in v], "%", MONTHLY)
        if s := self.series.get("UNRATE"):
            add("UNRATE", "Unemploy", "unemployment rate", s.dates, s.values, "%", MONTHLY)
        if (s := self.series.get("PAYEMS")) and len(s.values) > 1:
            add("PAYEMS", "Payrolls", "nonfarm payrolls, monthly change", s.dates[1:],
                [b - a for a, b in zip(s.values, s.values[1:], strict=False)], "k", MONTHLY, 0)
        if (s := self.series.get("GDPC1")) and len(s.values) > 1:
            add("GDPC1", "Real GDP", "real GDP, quarter on quarter, annual rate", s.dates[1:], [x * 100 for x in annualised_growth(s.values)],
                "%", QUARTERLY)
        if (s := self.series.get("GDP")) and len(s.values) > 1:
            add("GDP", "Nom GDP", "nominal GDP, quarter on quarter, annual rate", s.dates[1:], [x * 100 for x in annualised_growth(s.values)],
                "%", QUARTERLY)
        if s := self.series.get("DFF"):
            add("DFF", "Fed funds", "effective federal funds rate", s.dates, s.values, "%", DAILY, 2)
        return out

    def sources(self) -> list[Provenance]:
        return distinct([relabel(s.prov) for s in self.series.values()])

    def draw(self, w: int, h: int) -> list[Text]:
        self.provs = self.sources()
        prints = self.prints()
        if not prints:
            return []
        wide = w >= 84
        spark_w = 24 if w >= 110 else 12 if w >= 56 else 0
        cols = [ui.Col("print", "left", style=f"bold {ORANGE}")]
        cols += [ui.Col("what", "left", style=DIM)] if w >= 132 else []
        cols += [ui.Col("value", style=f"bold {TEXT}"), ui.Col("prev", style=DIM), ui.Col("chg"), ui.Col("period", "left", style=DIM)]
        cols += [ui.Col("3 years", "left", spark_w)] if spark_w else []
        cols += [ui.Col("", "left", flex=True, style=FAINT)] if wide else []
        rows = []
        for p in prints:
            dg, unit = p["digits"], p["unit"]

            def f(v, dg=dg, unit=unit):
                if v is None:
                    return "–"
                return (f"{v:+,.0f}".replace("-", fmt.MINUS) + "k") if unit == "k" else f"{v:.{dg}f}%"
            chg = delta(p["value"] - p["prev"], 0 if unit == "k" else 2) if p["prev"] is not None else None
            row: list[ui.Cell] = [p["label"]] + ([p["name"]] if w >= 132 else []) + [f(p["value"]), f(p["prev"]), chg,
                                                                                    asof(p["date"], p["cad"], True) if p["cad"] != DAILY
                                                                                    else asof(p["date"])]
            row += [spark_cell(p["hist"], spark_w)] if spark_w else []
            row += [str(p["cad"])] if wide else []
            rows.append(row)
        out = [ui.section("LATEST PRINTS", w, "the period each describes")] + ui.table(cols, rows, w, None, 1 if w < 60 else 2)
        out += [Text("")] + ui.wrap("The release calendar needs a free FRED key and is not shown." + (
            " CPI is the year-on-year change of CPIAUCSL. Payrolls is the monthly change of PAYEMS in thousands. GDP growth is quarter on "
            "quarter at an annual rate, real from GDPC1 and nominal from GDP. Each row shows the period it describes, not the day it was "
            "released." if w >= 60 else ""), w, FAINT)
        return out

    def hint(self) -> str:
        return "calendar needs a FRED key"

    def menu(self) -> list[tuple[str, str]]:
        return [("MACRO", "MACRO"), ("RATES", "RATES"), ("FX", "FX"), ("WEI", "WEI")]

    def export(self):
        return (["series", "print", "value", "previous", "unit", "period", "frequency"],
                [[p["id"], p["name"], p["value"], p["prev"], p["unit"], asof(p["date"], p["cad"], True), p["cad"]] for p in self.prints()])


# ── HMAP ────────────────────────────────────────────────────

HEAT_LISTS = ("GLOBAL", "INDICES", "STOCKS", "COMMODITIES", "CRYPTO")
PERIODS = (("1D", 1, 0.03), ("1W", 7, 0.07), ("1M", 30, 0.15), ("YTD", 0, 0.40))


def heat_cell(v: float | None, span: float, width: int) -> Text:
    """One cell of the heatmap: the change on a background from red through neutral to green, with ink chosen
    for contrast after the colour has been snapped to the palette in use."""
    if v is None or math.isnan(v):
        return Text("–".center(width), style="#6a6a6a", no_wrap=True)
    bg = charts.snap(charts.heat_colour(v, span))
    r, g, b = charts.rgb_of(bg)
    ink = INK if 0.299 * r + 0.587 * g + 0.114 * b > 120 else TEXT
    pct = abs(v) * 100
    s = ("+" if v > 0 else fmt.MINUS if v < 0 else "") + (f"{pct:.0f}" if pct >= 99.95 else f"{pct:.1f}")
    return Text(f"{s:>{width - 1}} ", style=f"{ink} on {bg}", no_wrap=True)


class HmapPane(MarketPane):
    code, every, tick = "HMAP", 900, 30.0
    heading = "performance heatmap"

    def lists(self) -> list[tuple[str, list[str]]]:
        return [(n, [i.ticker for i in self.hub.book.list(n)]) for n in HEAT_LISTS if self.hub.book.list(n)]

    def tickers(self) -> list[str]:
        return list(dict.fromkeys(t for _, ts in self.lists() for t in ts))

    async def load(self) -> None:
        tickers = self.tickers()
        self.later(self.pull(tickers))                   # live last prices sharpen the 1D column; the grid does not wait for them

        async def one(t: str) -> None:
            await self.history(t, 400)
            self.bump()

        await asyncio.gather(*[one(t) for t in tickers if reachable(self.hub, self.hub.book.get(t))], return_exceptions=True)
        if not self.hist:
            raise SourceError("no history source answered")

    def performance(self, ticker: str) -> list[float | None]:
        h = self.hist.get(ticker)
        if not h:
            return [None] * len(PERIODS)
        q = self.hub.quotes.get(ticker)
        last = q.price if q is not None and cadence(q.prov) == LIVE and h[1] and abs(q.price / h[1][-1] - 1) < 0.25 else None
        return [ytd(h[0], h[1], last) if days == 0 else change_over(h[0], h[1], days, last) for _, days, _ in PERIODS]

    def tag(self, ticker: str, room: int) -> Text:
        """What the row's numbers are made of: its own closes, a proxy's, a monthly average, a computed index."""
        ins, h = self.hub.book.get(ticker), self.hist.get(ticker)
        if h is None:
            return Text("")
        if self.hub.quotes.get(ticker) is not None:         # the row's own label, cut to its first word when the block is narrow
            full = status(self.hub, ticker)
            return full if full.cell_len <= room else full[:full.plain.index(" ")] if " " in full.plain else full
        if ins and not ins.sources and ins.proxy:
            via = self.hub.quotes.get(ticker)
            name = show(self.hub, via.via) if via is not None and via.via else show(self.hub, ins.proxy)
            return ui.t(("proxy", ui.YELLOW), (f" {name}" if room >= 6 + len(name) else "", DIM))
        if h[2].source == "computed":
            return Text("computed", style=ui.CYAN)
        cad = cadence(h[2])
        return Text(cad, style=ui.YELLOW if cad == MONTHLY else "#6a6a6a")

    def _block(self, name: str, tickers: list[str], bw: int) -> list[Text]:
        cw = 7 if bw >= 46 else 6
        lw = 9 if bw >= 46 else 8
        room = bw - lw - cw * len(PERIODS) - 1
        out = [ui.section(name, bw), ui.t((" " * lw, ""), *[(f"{p:>{cw - 1}} ", FAINT) for p, _, _ in PERIODS], (" basis", FAINT))]
        for t in tickers:
            if t not in self.hist:
                waiting = reachable(self.hub, self.hub.book.get(t))
                out.append(ui.t((f"{t:<{lw}}", FAINT), ("loading" if waiting and self.fetching else "no answer yet" if waiting
                                                        else "no open source", "#6a6a6a")))
                continue
            line = ui.t((f"{t:<{lw}}", f"bold {TEXT}"))
            for v, (_, _, span) in zip(self.performance(t), PERIODS, strict=True):
                line.append_text(heat_cell(v, span, cw))
            if room >= 5:
                line.append(" ")
                line.append_text(self.tag(t, room))
            line.truncate(bw)
            out.append(line)
        return out

    def sources(self) -> list[Provenance]:
        return sorted(distinct([p for _, _, p in self.hist.values()], False), key=lambda p: p.delay != DAILY)

    def draw(self, w: int, h: int) -> list[Text]:
        self.provs = self.sources()
        lists = self.lists()
        bw = w if w < 46 else 46
        across = max(1, (w + 3) // (bw + 3))
        blocks = [self._block(n, ts, bw) for n, ts in lists]
        out: list[Text] = []
        for i in range(0, len(blocks), across):
            group = blocks[i:i + across]
            tall = max(len(b) for b in group)
            for r in range(tall):
                line = Text(no_wrap=True)
                for j, b in enumerate(group):
                    part = b[r].copy() if r < len(b) else Text("")
                    if j < len(group) - 1:
                        part.pad_right(max(bw + 3 - part.cell_len, 0))
                    line.append_text(part)
                out.append(line)
            out.append(Text(""))
        legend = Text(no_wrap=True)
        legend.append("% change  ", style=FAINT)
        for v in (-1.0, -0.5, 0.0, 0.5, 1.0):
            legend.append_text(heat_cell(v * 0.03, 0.03, 6))
        legend.append("  full colour at ±3% 1D, ±7% 1W, ±15% 1M, ±40% YTD" if w >= 96 else "", style=FAINT)
        out.append(legend)
        if w >= 60:
            out += ui.wrap("Periods are measured on daily closes: a week is the close at or before seven days back, YTD starts at the first "
                           "close of the year. A monthly average has no daily or weekly change, so those cells stay empty. A row tagged "
                           "proxy is the stand-in's performance, not the instrument's.", w, FAINT)
        return out

    def hint(self) -> str:
        return "daily closes · proxies tagged"

    def menu(self) -> list[tuple[str, str]]:
        return [("QM", "QM GLOBAL"), ("WEI", "WEI"), ("GLCO", "GLCO"), ("FX", "FX"), ("RV", "RV")]

    def export(self):
        rows = []
        for name, tickers in self.lists():
            for t in tickers:
                rows.append([name, t, *self.performance(t), self.tag(t, 40).plain if t in self.hist else "no open source"
                             if not reachable(self.hub, self.hub.book.get(t)) else "no answer yet"])
        return ["list", "ticker", *(p for p, _, _ in PERIODS), "basis"], rows


# ── RV ──────────────────────────────────────────────────────

SHARES = (0.10, 0.25, 0.50, 1.00)


class RvPane(MarketPane):
    code, every, tick = "RV", 300, 5.0
    heading = "BTC against gold"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.tip = 0
        self.tip_prov: Provenance | None = None

    def cache_key(self) -> tuple:
        return (*self.quote_key(["BTC", "XAU"]), self.hub.chain.height)

    async def load(self) -> None:
        hub = self.hub
        self.keep_fresh(["BTC", "XAU"])
        await self.pull(["BTC", "XAU"])
        if not hub.chain.height:                            # the chain loop normally has the tip; a script or a cold start may not
            for key in hub.bitcoin_order():
                try:
                    self.tip, self.tip_prov = await hub.sources[key].tip_height()
                    break
                except (SourceError, AttributeError):
                    continue
        self.bump()
        await self.history("BTC", 400)
        await self.history("XAU", 400)
        if "BTC" not in hub.quotes:
            raise SourceError("no exchange answered for BTC")

    def numbers(self) -> dict[str, float] | None:
        hub = self.hub
        btc, gold = hub.quotes.get("BTC"), hub.quotes.get("XAU")
        height = hub.chain.height or self.tip
        if not (btc and gold and gold.price > 0):
            return None
        tonnes = float(hub.cfg.get("gold_stock_tonnes", 0) or 0)
        supply = btcmath.issued_sats(height) / 1e8 if height else 0.0
        gold_cap = gold_market_cap(tonnes, gold.price)
        return {"btc": btc.price, "gold": gold.price, "tonnes": tonnes, "supply": supply, "btc_cap": supply * btc.price, "gold_cap": gold_cap,
                "ratio": supply * btc.price / gold_cap if gold_cap and supply else 0.0, "oz": btc.price / gold.price, "height": height}

    def ratio_history(self) -> tuple[list[float], list[float]]:
        b, g = self.hist.get("BTC"), self.hist.get("XAU")
        if not (b and g):
            return [], []
        gold = dict(zip(g[0], g[1], strict=True))
        pts = [(d, v / gold[d]) for d, v in zip(b[0], b[1], strict=True) if gold.get(d)]
        return [p[0] for p in pts], [p[1] for p in pts]

    def sources(self) -> list[Provenance]:
        return distinct(self.quote_provs(["BTC", "XAU"]) + [self.hub.chain.prov or self.tip_prov] + [x[2] for x in self.hist.values()])

    def draw(self, w: int, h: int) -> list[Text]:
        hub, n = self.hub, self.numbers()
        gold_q = hub.quotes.get("XAU")
        self.provs = self.sources()
        if n is None:
            return [] if not self.loaded_at else ui.centre(["RV needs the BTC composite and the gold proxy.", "Neither has answered yet."], w, h)
        via = show(hub, gold_q.via) if gold_q is not None and gold_q.via else ""
        proxy = ui.t(("via ", FAINT), (via, f"bold {DIM}"), (" · ", FAINT), ("proxy", ui.YELLOW)) if via else Text("")
        out = [ui.t(ui.kv("BTC ", ui.px(n["btc"], 0), f"bold {ORANGE}"), ("  ", ""), ui.kv("cap", ui.usd(n["btc_cap"]) if n["supply"] else "–"),
                    (f"  {n['supply']:,.0f} BTC issued at #{n['height']:,}" if w >= 72 and n["supply"] else "", FAINT)),
               ui.t(ui.kv("GOLD", ui.px(n["gold"], 0) + "/oz", f"bold {ui.YELLOW}"), ("  ", ""), proxy),
               ui.t(ui.kv("gold cap", ui.usd(n["gold_cap"])), (f"  {n['tonnes']:,.0f} t × {TROY_OZ_PER_TONNE:,.4f} oz/t × price" if w >= 72
                                                                else f"  {n['tonnes']:,.0f} t", FAINT))]
        out += ui.wrap(str(hub.cfg.get("gold_stock_source") or GOLD_STOCK_SOURCE), w, "#6a6a6a")
        out.append(ui.t(ui.kv("BTC / gold cap", f"{n['ratio']:.2%}" if n["supply"] else "–", f"bold {ORANGE}"), ("   ", ""),
                        ui.kv("1 BTC =", f"{n['oz']:,.2f} oz")))
        xs, ys = self.ratio_history()
        table_h = len(SHARES) + 3
        if len(ys) >= 10 and (h - len(out) - table_h >= 5 or h >= 30):
            ch = max(5, min(h - len(out) - table_h - 2, 16))
            plot = charts.Plot(w, ch + 1)
            rx, ry = charts.resample(xs, ys, max(w, 20) * 2)
            plot.line(rx, ry, ORANGE, label="BTC in ounces of gold")
            plot.hline(n["oz"], ui.CYAN, f"{n['oz']:,.1f}")
            out += [Text(""), ui.t(("BTC IN OUNCES ", f"bold {ORANGE}"), (f"daily closes · gold {('via ' + via) if via else ''}", FAINT))]
            out += plot_lines(plot)
        elif len(ys) >= 2:
            out.append(ui.t(("oz/BTC ", FAINT), (charts.spark(ys, max(w - 22, 8)), ORANGE), (f" {len(ys)}d", FAINT)))
        out += ([Text("")] if h >= 18 else []) + [ui.section("BTC AT A SHARE OF GOLD", w, "price per BTC" if w >= 50 else "")]
        rows = []
        for share in SHARES:
            target = btc_at_gold_share(share, n["gold_cap"], n["supply"])
            rows.append([f"{share:.0%}", Text(ui.px(target, 0) if target else "–", style=f"bold {TEXT}"),
                         f"{target / n['btc']:.1f}×" if target else "–", ui.usd(n["gold_cap"] * share)])
        cols = [ui.Col("of gold", "left", style=f"bold {ORANGE}"), ui.Col("BTC price"), ui.Col("× now", style=DIM), ui.Col("BTC cap", style=DIM)]
        out += ui.table(cols, rows, w, None, 1 if w < 60 else 2)
        if w >= 72:
            out += [Text("")] + ui.wrap(
                "Supply is the protocol maximum issued at the tip height, a little more than what can be spent. Gold's price is PAX Gold, a "
                "token of one troy ounce, because no open source publishes spot gold: the comparison is labelled proxy for that reason. "
                "Change gold_stock_tonnes with SET.", w, FAINT)
        return out

    def hint(self) -> str:
        return "gold via PAXG, a proxy"

    def menu(self) -> list[tuple[str, str]]:
        return [("GLCO", "GLCO"), ("GP", "GP BTC XAU"), ("BTC", "BTC"), ("MACRO", "MACRO"), ("HMAP", "HMAP")]

    def export(self):
        n = self.numbers()
        if n is None:
            return None
        rows = [["btc_price_usd", n["btc"]], ["gold_price_usd_oz_via_proxy", n["gold"]], ["btc_supply", n["supply"]],
                ["btc_market_cap_usd", n["btc_cap"]],
                ["gold_stock_tonnes", n["tonnes"]], ["troy_oz_per_tonne", TROY_OZ_PER_TONNE], ["gold_market_cap_usd", n["gold_cap"]],
                ["btc_share_of_gold_cap", n["ratio"]], ["ounces_per_btc", n["oz"]]]
        rows += [[f"btc_price_at_{s:.0%}_of_gold", btc_at_gold_share(s, n["gold_cap"], n["supply"])] for s in SHARES]
        return ["measure", "value"], rows


# ── DVOL ────────────────────────────────────────────────────

class DvolPane(MarketPane):
    code, every, tick = "DVOL", 300, 5.0
    heading = "BTC implied vol"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.rows_: list[list[float]] = []
        self.atm: list[deribit.AtmVol] = []
        self.deribit_index: float | None = None
        self.fng: list[sentiment.Reading] = []
        self.extra: list[Provenance] = []

    def cache_key(self) -> tuple:
        fc = self.hub.forecast()
        return (len(fc), round(fc[0][1]) if fc else 0)

    async def load(self) -> None:
        hub, provs, last = self.hub, [], None
        if (db := hub.sources.get("deribit")) is not None:
            try:
                self.rows_, p = await db.dvol("1D", *deribit.window(90, _now()))
                provs.append(p)
                self.bump()
                book, p2 = await db.book_summary("option")
                self.atm = await asyncio.to_thread(deribit.atm_vols, book, _now(), 6)     # a thousand rows: off the loop
                self.deribit_index, _ = await db.index_price()
            except SourceError as e:
                last = e
        if (fg := hub.sources.get("fng")) is not None:
            try:
                self.fng, p = await fg.index(30)
                provs.append(p)
            except SourceError:
                pass
        self.extra = provs
        if not self.rows_ and not self.atm and not hub.forecast():
            raise last or SourceError("Deribit did not answer")

    def sources(self) -> list[Provenance]:
        return distinct(self.extra, False)                  # Deribit leads: the page is live, the sentiment line is the daily one

    def draw(self, w: int, h: int) -> list[Text]:
        hub, now = self.hub, _now()
        self.provs = self.sources()
        vols = glimpse_vols(hub.forecast(), now)
        out: list[Text] = []
        if not (self.rows_ or self.atm or self.fng or vols) and self.error:
            return []                                       # nothing loaded and nothing cached: the frame carries the error
        if self.rows_:
            closes = [r[4] for r in self.rows_]
            latest, prev = closes[-1], closes[-2] if len(closes) > 1 else None
            out.append(ui.t(("DVOL ", f"bold {ORANGE}"), (f"{latest:.2f}", f"bold {TEXT}"), ("  ", ""),
                            delta(latest - prev, 2) if prev is not None else "", (" 1d" if prev is not None else "", FAINT),
                            (f"  {len(closes)}d {min(r[3] for r in self.rows_):.1f} – {max(r[2] for r in self.rows_):.1f}", DIM),
                            ("  deribit · 30-day implied, % a year" if w >= 84 else "", FAINT)))
            out += self._glimpse_line(vols, w)               # the two numbers this page compares, together at the top
            rest = (1 if self.atm else 0) + ((1 if len(vols) >= 3 else 0) + (2 if self.fng else 0) if h >= 14 else 0)
            ch = max(3, min(h - len(out) - rest - 1, 16))
            plot = charts.Plot(w, ch + 1)
            xs, ys = charts.resample([r[0] / 1000 for r in self.rows_], closes, max(w, 20) * 2)
            plot.line(xs, ys, ORANGE, label="DVOL")
            near = nearest_horizon(vols, DAY)
            if near and min(closes) * 0.5 < near[1] < max(closes) * 2:
                plot.hline(near[1], ui.CYAN, f"G {near[1]:.1f}")
            out += plot_lines(plot)
        else:
            out += ui.wrap("Deribit's DVOL did not load. The Glimpse side is below.", w, FAINT) + self._glimpse_line(vols, w)
        if self.atm:
            line = ui.t(("ATM IV ", f"bold {FAINT}"))
            for a in self.atm:
                part = ui.t((f"{a.label[:-2]} ", FAINT), (f"{a.iv:.1f}  ", f"bold {TEXT}"))
                if line.cell_len + part.cell_len > w:
                    break
                line.append_text(part)
            out.append(line)
        if vols and len(vols) >= 3:
            out.append(ui.t(("term   ", f"bold {FAINT}"), (charts.spark([v for _, v in vols], max(min(w - 30, 60), 8)), ui.CYAN),
                            (f" {horizon(vols[0][0])} to {horizon(vols[-1][0])}", FAINT)))
        if self.fng:
            r = self.fng[-1]
            colour = GREEN if r.value >= 55 else RED if r.value <= 45 else DIM
            out.append(ui.t(("FEAR & GREED " if w >= 60 else "F&G ", f"bold {FAINT}"), (f"{r.value}", f"bold {colour}"), (f" {r.label}", colour),
                            (f" · {sentiment.ATTRIBUTION}", DIM), (f" · {asof(r.date)}" if w >= 60 else "", FAINT)))
            if w >= 60:
                out.append(ui.t(("30 days      ", FAINT), (charts.spark([x.value for x in self.fng], min(w - 16, 30)), ui.VIOLET)))
        if (self.atm or vols) and (h >= 30 or w >= 100):
            out += self._term_structure(vols, now, w, h)
        return out

    def _glimpse_line(self, vols: list[tuple[float, float]], w: int) -> list[Text]:
        if not vols:
            return ui.wrap("open the BTC hourly series (o, or FCST BTC) to load Glimpse's closes", w, ui.CYAN)
        line = ui.t(("GLIMPSE ", f"bold {ui.CYAN}"), (f"{horizon(vols[0][0])} ", FAINT), (f"{vols[0][1]:.1f}  ", f"bold {TEXT}"))
        for label, secs in (("24h", DAY), ("7d", 7 * DAY)):
            hit = nearest_horizon(vols, secs)
            line.append(f"{label} ", style=FAINT)
            line.append(f"{hit[1]:.1f}  " if hit else "–  ", style=f"bold {TEXT}" if hit else FAINT)
        if w >= 72:
            line.append("% a year, from each close's 80% band", style=FAINT)
        return [line]

    def _term_structure(self, vols: list[tuple[float, float]], now: float, w: int, h: int) -> list[Text]:
        out = [Text(""), ui.section("TERM STRUCTURE", w, "x: days ahead, log scale · y: % a year")]
        plot = charts.Plot(w, max(6, min(h // 3, 12)), times=False)
        if self.atm:
            pts = [(math.log10(max((a.expiry - now) / DAY, 1 / 48)), a.iv) for a in self.atm if a.expiry > now]
            plot.line([p[0] for p in pts], [p[1] for p in pts], ORANGE, label="Deribit ATM by expiry")
        if vols:
            plot.line([math.log10(max(s / DAY, 1 / 48)) for s, _ in vols], [v for _, v in vols], ui.CYAN, label="Glimpse by close")
        out += plot_lines(plot)[:-1] + [plot.legend()]
        rows = [["Deribit", a.label, horizon(a.expiry - now), f"{a.iv:.1f}", f"{a.strike:,.0f}", f"{a.forward:,.0f}"]
                for a in self.atm if a.expiry > now]
        for label, secs in (("nearest", vols[0][0] if vols else 0), ("24h", DAY), ("7d", 7 * DAY)):
            hit = nearest_horizon(vols, secs) if secs else None
            if hit:
                rows.append(["Glimpse", label, horizon(hit[0]), f"{hit[1]:.1f}", "", ""])
        out += ui.table([ui.Col("market", "left", style=f"bold {TEXT}"), ui.Col("expiry", "left", style=DIM), ui.Col("ahead"), ui.Col("IV %"),
                         ui.Col("ATM strike", style=DIM), ui.Col("forward", style=DIM)], rows, w)
        if self.deribit_index:
            out += ui.wrap(f"Deribit BTC index {self.deribit_index:,.1f}. ATM is mark_iv interpolated in strike to each expiry's forward.",
                           w, FAINT)
        return out

    def hint(self) -> str:
        return "DVOL 90d · Glimpse cyan"

    def menu(self) -> list[tuple[str, str]]:
        return [("ODDS", "ODDS"), ("HM", "HM"), ("GP", "GP BTC"), ("BTC", "BTC"), ("OMON", "OMON")]

    def export(self):
        now = _now()
        rows = [["dvol", datetime.fromtimestamp(r[0] / 1000, UTC).strftime("%Y-%m-%d"), r[4]] for r in self.rows_]
        rows += [["deribit_atm_iv", a.label, a.iv] for a in self.atm]
        rows += [["glimpse_iv", f"{s / 3600:.2f}h", v] for s, v in glimpse_vols(self.hub.forecast(), now)]
        return ["measure", "when", "percent_a_year"], rows


# ── the registry ────────────────────────────────────────────

_LABELS = ("Every row says what it is. `live` is an exchange's last trade. `closed` is the last trade of a session that is shut. `daily`, "
           "`weekly` and `monthly` values show the date or month they describe. `proxy` names the stand-in that is actually quoted, and a "
           "proxy is never shown as the instrument itself. `computed` is derived here. `no open source` means nobody publishes it without "
           "a key, so the row stays, dim and empty. Pyth would fill most gaps but has needed an API key since 26 Aug 2026 and is not used. ")
_DOWN = ("When a source is down its last good values stay on screen, the frame says how stale they are, and the rows that depend on it "
         "keep their old date. SRC shows every source's health.")

register(Function(
    "QM", "Quote monitor", "Markets", "watchlists: last, change, range, sparkline, source and delay", QmPane, args="[list or tickers]",
    needs=("quote",),
    help="One row an instrument: last, change, percent change, the day's range with the last trade marked on it, thirty daily closes as a "
         "sparkline, the source and the delay. QM GLOBAL, INDICES, STOCKS, COMMODITIES, CRYPTO, FX, DXY_LEGS, RATES, BTC_ETFS and MINERS "
         "are the shipped lists; your own live under [lists] in terminal.toml, and QM BTC ETH XAU makes one on the spot. [ and ] cycle "
         "the lists, enter charts the row with GP. " + _LABELS +
         "Crypto is the median of Coinbase, Kraken and Bitstamp (BTC) or the first exchange that answers. EUR, GBP, CAD, CHF and AUD "
         "are Kraken's spot FX books. USDJPY and USDSEK are the ECB's daily fix. SPX, NDX and NKY are FRED's daily closes (SP500, "
         "NASDAQ100, NIKKEI225), owned by S&P Dow Jones Indices, Nasdaq and Nikkei. Yields are the Treasury's official daily par "
         "curve and move in basis points. Brent, WTI and natural gas are EIA spot prices through FRED, a few days late. Gold is PAX "
         "Gold, a proxy. Five stocks show through Kraken's tokenised xStocks, as proxies. While the pane is open its tickers join "
         "the hub's quote loop, which refreshes every 12 s; daily sources are cached for hours. " + _DOWN))
register(Function(
    "WEI", "World equity indices", "Markets", "indices by region: last, change, YTD, with labelled proxies", WeiPane, needs=("quote", "series"),
    help="Americas (SPX, NDX, RUT, DJI), EMEA (SX5E, DAX, UKX) and Asia (NKY, HSI, KOSPI, NIFTY). Levels are official daily closes from "
         "FRED: SP500, NASDAQ100, DJIA and NIKKEI225, each a business day old and copyright of its owner (S&P Dow Jones Indices, Nasdaq, "
         "Nikkei), shown for personal use. Year to date is measured from the first close of the year in the same series. Beneath an "
         "index, a line such as `SPY via SPYx · proxy · live` is Kraken's tokenised tracker of the ETF, trading now, in its own price: "
         "it is a proxy and is never shown as the index. RUT, SX5E, DAX, UKX, HSI, KOSPI and NIFTY have no open index level, so they "
         "stay on the page as `no open source`. Reloads every 2 minutes; FRED is cached for 6 hours. " + _LABELS + _DOWN))
register(Function(
    "FX", "Currencies", "Markets", "major pairs, the computed dollar index and its legs, BTC in each currency", FxPane, needs=("quote",),
    help="Major pairs with last, change and the day's range. EURUSD, GBPUSD, USDCAD, USDCHF and AUDUSD are live from Kraken's spot FX "
         "books, which trade through the weekend; the row says `closed` when the interbank session is shut. USDJPY and USDSEK have no "
         "usable open book, so they are the ECB's euro reference fix crossed to dollars, daily, published about 16:00 CET, with the fix "
         "date shown. USDCNH has no open feed: the onshore USDCNY fix stands in and is labelled a proxy. The dollar index is computed "
         "here with ICE's formula, 50.14348112 × EURUSD^-0.576 × USDJPY^0.136 × GBPUSD^-0.119 × USDCAD^0.091 × USDSEK^0.042 × "
         "USDCHF^0.036, and is labelled `computed` with which legs are live and which are daily. It is not the ICE index itself. BTC "
         "in each currency is the composite BTC/USD times the pair (or divided by it for EUR, GBP and AUD), and carries the pair's "
         "delay. Quotes refresh every 12 s while the pane is open; the ECB file is cached for an hour. " + _DOWN))
register(Function(
    "GLCO", "Commodities", "Markets", "gold, oil, gas, copper, grains, sugar; gold priced in BTC", GlcoPane, needs=("quote", "series"),
    help="Gold is PAX Gold (PAXG), a token of one troy ounce on Coinbase, Kraken and Bitstamp: a labelled proxy, because no open source "
         "publishes spot gold. Silver has no open source at all and says so. Brent (DCOILBRENTEU), WTI (DCOILWTICO) in dollars a barrel "
         "and Henry Hub natural gas (DHHNGSP) in dollars per MMBtu are EIA spot prices through FRED: daily, usually about four days "
         "late, with the date shown. Copper (PCOPPUSDM), soybeans (PSOYBUSDM), corn (PMAIZMTUSDM) and wheat (PWHEAMTUSDM) in dollars "
         "per metric ton, and sugar (PSUGAISAUSDM) in US cents per pound, are IMF monthly averages through FRED, about two months "
         "late: the row says `monthly` and names the month, and its percent change is month on month. Gold in BTC is ounces per BTC "
         "and BTC per ounce from the composite BTC price and the gold proxy. Reloads every 2 minutes; FRED is cached for 6 hours. "
         + _DOWN))
register(Function(
    "RATES", "Rates", "Markets", "the Treasury curve now against a week, a month and a year ago; 2s10s; policy and real rates", RatesPane,
    needs=("series",),
    help="The official par yield curve from home.treasury.gov for the latest business day (orange) against a week (cyan), a month "
         "(violet) and a year (grey) earlier, with every tenor from 1 month to 30 years evenly spaced, then the table by tenor with "
         "changes in basis points. 2s10s is the 10-year less the 2-year from the same curve (FRED T10Y2Y when the curve is missing). "
         "Fed funds is FRED DFF, SOFR is FRED SOFR, the 10-year real yield is the Treasury's real curve or FRED DFII10, whichever is "
         "newer, and the 10-year breakeven is FRED T10YIE. Everything is a daily official value, one business day old at best, and "
         "each shows its date. The Treasury's server takes about 18 s a file, so the month loads first and this year's and last "
         "year's files arrive in the background: the legend says `loading` until they do. Reloads every 30 minutes; the files are "
         "cached for 3 hours. If the Treasury is down the FRED rates still show. " + _DOWN))
register(Function(
    "MACRO", "Liquidity", "Markets", "Fed balance sheet, TGA, reverse repo, M2 and net liquidity against BTC", MacroPane, needs=("series",),
    help="Net liquidity is WALCL − WTREGEN − RRPONTSYD: the Fed's balance sheet, less the Treasury General Account, less the overnight "
         "reverse repo facility. FRED publishes WALCL and WTREGEN in millions of dollars and RRPONTSYD in billions, so each is "
         "converted to dollars before subtracting. The chart is weekly, on WALCL's Wednesday dates, using the last known value of the "
         "other two, with BTC's daily close (Coinbase candles) on the left axis. [ and ] switch between one, two and five years. The "
         "table gives the latest value, the change over 4 and 52 weeks, and the as-of date of each input: WALCL and WTREGEN are "
         "weekly, RRPONTSYD daily, M2 (M2SL) monthly and about two months late. Net liquidity is labelled computed. Reloads hourly; "
         "FRED is cached for 6 hours. " + _DOWN))
register(Function(
    "ECO", "Economic prints", "Markets", "latest US prints: CPI, unemployment, payrolls, GDP, fed funds", EcoPane, needs=("series",),
    help="The latest value, the one before, the change and the period each describes, from FRED's keyless CSV. CPI is the "
         "year-on-year change of CPIAUCSL. Unemployment is UNRATE. Payrolls is the monthly change of PAYEMS, in thousands. Real GDP "
         "growth is quarter on quarter at an annual rate from GDPC1, and nominal growth the same from GDP, which FRED publishes in "
         "billions at an annual rate. Fed funds is DFF, daily. The period is the month or quarter the number describes, not its "
         "release day. The release calendar needs a free FRED API key and is not shown. Reloads hourly; FRED is cached for 6 hours. "
         + _DOWN))
register(Function(
    "HMAP", "Performance heatmap", "Markets", "five lists over a day, a week, a month and the year to date", HmapPane, needs=("series", "quote"),
    help="One block a list (GLOBAL, INDICES, STOCKS, COMMODITIES, CRYPTO), one row an instrument, one coloured cell a period: red "
         "through neutral to green, at full colour at ±3% for a day, ±7% for a week, ±15% for a month and ±40% for the year. Periods "
         "are measured on daily closes from the instrument's own history: exchange candles for crypto, Kraken for FX and tokenised "
         "shares, FRED for indices and commodities, the Treasury for yields (shown as the relative change of the yield), the ECB for "
         "the yen, and the computed dollar index over the ECB's fixes. A live last price replaces the final close. A row tagged "
         "`proxy` is the stand-in's performance. A monthly average has no daily or weekly change, so those cells are empty. "
         "Instruments with no open source stay, dim. Histories load once and refresh every 15 minutes. " + _DOWN))
register(Function(
    "RV", "Relative value", "Markets", "BTC against gold: market caps, BTC in ounces, BTC at a share of gold", RvPane, needs=("quote", "tip"),
    help="BTC's market cap is the protocol maximum issued at the tip height times the composite price. Gold's market cap is the "
         "above-ground stock in tonnes (gold_stock_tonnes in config: World Gold Council, end 2024) times 32,150.7466 troy ounces a "
         "tonne times the gold price. The gold price is PAX Gold, a labelled proxy, because no open source publishes spot gold. The "
         "page shows the ratio of the two caps, one BTC in ounces, the history of BTC in ounces from daily closes, and what BTC "
         "would cost at 10, 25, 50 and 100 percent of gold's market cap. Quotes refresh every 12 s; histories every 5 minutes. "
         + _DOWN))
register(Function(
    "DVOL", "Implied volatility", "Markets", "Deribit's DVOL and ATM vols against the volatility in Glimpse's closes", DvolPane,
    needs=("quote",),
    help="DVOL is Deribit's 30-day implied volatility index for BTC, in percent a year: the latest value and 90 daily bars from "
         "public/get_volatility_index_data. ATM IV is mark_iv from public/get_book_summary_by_currency for the nearest expiries, "
         "interpolated in strike to each expiry's forward. The Glimpse side reads each hourly close's 80% band as a lognormal and "
         "scales its width to a year: the nearest close, the one about 24 hours out and the one about 7 days out, with a term "
         "structure. A horizon with no close near it shows a dash. If no Glimpse closes are loaded the page says to open the BTC "
         "hourly series the terminal has open. The cyan rule on the chart is Glimpse's 24 hour volatility. The Fear and Greed index is "
         "alternative.me's, shown with its attribution. Deribit is polled every 5 minutes at no more than one request a second. "
         + _DOWN))
