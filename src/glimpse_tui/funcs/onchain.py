"""FLDS, ONCH, URPD, WAVE, CYC and CORR: on-chain research (TERMINAL.md section 6).

The rule of this family: never invent a series identifier. Every name asked of Bitview comes from `data/onchain.toml`
(found and checked in Phase 0) or from a live `search`. A name the server refuses is named on screen and is never
swapped for another. Every page is daily, says so, and names the day its values describe: the last complete UTC day,
because the final point of a day1 series is today and still forming.
"""
from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from os.path import commonprefix
from typing import Any

from rich.text import Text

from .. import charts
from ..data import fx_ref, quotes
from ..data.bitview import SeriesData, onchain_catalogue
from ..data.core import DAILY, Provenance, SourceError
from ..term import ui
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT

DAY = 86400.0
CYAN, VIOLET, YELLOW = ui.CYAN, ui.VIOLET, ui.YELLOW
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── the catalogue ───────────────────────────────────────────

@lru_cache(maxsize=1)
def catalogue() -> dict[str, Any]:
    return onchain_catalogue()


def metric(key: str) -> dict[str, Any]:
    return catalogue().get("metrics", {}).get(key, {})


def series_of(key: str) -> str:
    """The Bitview identifier behind a metric key of onchain.toml, or '' when the file does not carry it."""
    return str(metric(key).get("series", ""))


def wave_bands() -> list[tuple[str, str]]:
    """(label, series) for the age bands of onchain.toml, youngest first."""
    return [(str(b.get("label", "")), str(b.get("series", ""))) for b in catalogue().get("waves", []) if b.get("series")]


def known_series() -> set[str]:
    dates = str(catalogue().get("source", {}).get("date_series", ""))
    return {str(m.get("series")) for m in catalogue().get("metrics", {}).values()} | {s for _, s in wave_bands()} | ({dates} if dates else set())


# ── small things every page uses ────────────────────────────

def iso_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def iso_ts(day: str) -> float:
    try:
        return datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return 0.0


def short_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d %b")


def fit(lines: list[Text], w: int) -> list[Text]:
    """Nothing wraps and nothing runs past the pane: a line that is too long loses its tail."""
    for ln in lines:
        if ln.cell_len > w:
            ln.truncate(w)
    return lines


def pick(room: int, *options: str) -> str:
    """The longest wording that fits."""
    return next((o for o in options if len(o) <= room), "")


def beside(left: list[Text], right: list[Text], lw: int, gap: int = 3) -> list[Text]:
    out = []
    for i in range(max(len(left), len(right))):
        a = left[i].copy() if i < len(left) else Text("", no_wrap=True)
        a.truncate(lw)
        a.pad_right(lw - a.cell_len + gap)
        if i < len(right):
            a.append_text(right[i])
        out.append(a)
    return out


def flow(items: Sequence[Text], w: int, gap: int = 3) -> list[Text]:
    """Items left to right, a new line when the next one does not fit."""
    out: list[Text] = []
    line = Text(no_wrap=True)
    for it in items:
        if line.cell_len and line.cell_len + gap + it.cell_len > w:
            out.append(line)
            line = Text(no_wrap=True)
        if line.cell_len:
            line.append(" " * gap)
        line.append_text(it)
    return out + ([line] if line.cell_len else [])


def kprice(v: float) -> str:
    """A bucket edge: 80k, 79.4k, 1.26k, 630, 2.51."""
    if v >= 1000:
        return f"{v / 1000:.3g}k"
    return f"{v:.3g}"


def settled_at(s: SeriesData) -> int:
    """Index of the last complete day: the final day1 point is today."""
    return len(s.values) - 2 if len(s.values) > 1 else len(s.values) - 1


async def fetch_many(bv: Any, names: list[str], start: int) -> tuple[dict[str, SeriesData], dict[str, str]]:
    """One bulk request. When the server refuses the whole request (a 4xx: one unknown name sinks it), each series is
    asked for alone so the page can say which name failed. Returns (series by name, error by name)."""
    try:
        return {s.name: s for s in await bv.bulk(names, "day1", start)}, {}
    except SourceError as e:
        if "HTTP 4" not in str(e):
            raise
    got: dict[str, SeriesData] = {}
    bad: dict[str, str] = {}
    for n in names:
        try:
            got[n] = await bv.series(n, "day1", start)
        except SourceError as e:
            bad[n] = str(e)
    if not got:
        raise SourceError(next(iter(bad.values()), "bitview: no series answered"))
    return got, bad


class OnchainPane(FuncPane):
    """What the six pages share: the Bitview source, the day on the frame, a picture cache and lazy reloads."""

    every = 900.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.day = 0.0                              # the complete UTC day the values describe
        self.bad: dict[str, str] = {}               # series the server refused, by name
        self._have: Any = None                      # the state the loaded data answers
        self._asked: Any = None                     # the last state a draw asked a reload for
        self._pic: tuple[tuple, Any] | None = None
        self.stamp = 0                              # counts good loads: the data version a cached picture belongs to

    def bitview(self) -> Any:
        bv = self.hub.sources.get("bitview")
        if bv is None:
            raise SourceError("Bitview is not configured: set bitcoin.bitview in terminal.toml")
        return bv

    def want(self, state: Any) -> None:
        """From `draw`: the picture wants data that is not loaded. Ask once for each state, so a host that is down is
        not asked again on every repaint. The shell's next tick runs the load; `draw` itself never fetches."""
        if state != self._have and state != self._asked and not self.fetching:
            self._asked, self.loaded_at = state, 0.0

    def again(self) -> None:
        """From `key`: the user changed what the page shows."""
        self._asked, self.loaded_at = None, 0.0

    def pic(self, key: tuple, make: Callable[[], Any]) -> Any:
        """A heavy picture, drawn once for each (size, data version, view)."""
        if self._pic is None or self._pic[0] != key:
            self._pic = (key, make())
        return self._pic[1]

    def spot(self, fallback: float | None = None) -> tuple[float | None, str]:
        """The live composite when the quote board has it, otherwise the daily close the page already holds."""
        p = self.hub.btc_price()
        return (p, "spot") if p else (fallback, "close")

    def cache_key(self) -> tuple:
        return (round(self.hub.btc_price() or 0),)

    def bad_lines(self) -> list[Text]:
        return [ui.note(f"{n}: not served ({e}). The name is from onchain.toml and is never swapped for another.", RED)
                for n, e in self.bad.items()]


# ── URPD: shared by ONCH and URPD ───────────────────────────

@dataclass
class Bin:
    floor: float
    ceil: float
    supply: float


def agg_step(agg: str) -> tuple[str, float]:
    """('log', buckets a decade) or ('lin', dollars a bucket) from Bitview's aggregation name."""
    m = re.match(r"^(log|lin)(\d+)$", agg or "")
    return (m.group(1), float(m.group(2))) if m else ("", 0.0)


def urpd_bins(data: dict[str, Any]) -> list[Bin]:
    """The reply's buckets, ascending, each with the upper edge its aggregation implies."""
    kind, n = agg_step(str(data.get("aggregation", "")))
    out = []
    for b in data.get("buckets", []):
        lo, s = float(b.get("price_floor", 0) or 0), float(b.get("supply", 0) or 0)
        hi = lo + n if kind == "lin" else (lo * 10 ** (1 / n) if lo > 0 else 1.0) if kind == "log" and n else lo
        out.append(Bin(lo, hi, s))
    return sorted(out, key=lambda b: b.floor)


def merge_lin(bins: list[Bin], step: float) -> list[Bin]:
    out: dict[float, Bin] = {}
    for b in bins:
        lo = math.floor(b.floor / step + 1e-9) * step
        out.setdefault(lo, Bin(lo, lo + step, 0.0)).supply += b.supply
    return [out[k] for k in sorted(out)]


def merge_log(bins: list[Bin], per_decade: float, m: int) -> list[Bin]:
    """Every `m` adjacent log buckets as one. Buckets under one dollar are left out: they are shown apart."""
    out: dict[int, Bin] = {}
    for b in bins:
        if b.floor < 1:
            continue
        g = round(math.log10(b.floor) * per_decade) // m
        lo, hi = 10 ** (g * m / per_decade), 10 ** ((g + 1) * m / per_decade)
        out.setdefault(g, Bin(lo, hi, 0.0)).supply += b.supply
    return [out[k] for k in sorted(out)]


def profit_split(bins: list[Bin], spot: float, log: bool) -> tuple[float, float]:
    """(supply below spot, supply above spot). The bucket spot sits in is divided where spot cuts it: along the log of
    price for log buckets, along price for linear ones. It is an estimate, as fine as the buckets are."""
    below = above = 0.0
    for b in bins:
        if b.ceil <= spot:
            below += b.supply
        elif b.floor >= spot:
            above += b.supply
        else:
            if log and b.floor > 0:
                f = (math.log(spot) - math.log(b.floor)) / (math.log(b.ceil) - math.log(b.floor))
            else:
                f = (spot - b.floor) / (b.ceil - b.floor) if b.ceil > b.floor else 1.0
            below, above = below + b.supply * f, above + b.supply * (1 - f)
    return below, above


def lin_step(top: float, rows: int) -> float:
    """The finest round bucket that keeps most of the distribution on screen."""
    return next((s for s in (1000.0, 2000.0, 5000.0, 10000.0, 20000.0) if top / s + 1 <= rows * 1.6), 50000.0)


def around_spot(bins: list[Bin], spot: float, rows: int) -> list[Bin]:
    """`rows` adjacent buckets with spot about a quarter of the way down, as on the launchpad."""
    if len(bins) <= rows:
        return bins
    at = max((i for i, b in enumerate(bins) if b.floor <= spot), default=0)
    hi = min(at + max(rows // 4, 1), len(bins) - 1)
    lo = max(hi - rows + 1, 0)
    return bins[lo:lo + rows]


def urpd_bars(bins: list[Bin], total: float, spot: float | None, w: int, word: str = "spot") -> list[Text]:
    """Horizontal bars, highest price first: `  80k 17.8% ▏██████████▌  ◂ 81,608 spot`. Every bar shares one scale,
    shortened just enough for the marker to fit after the spot bucket's bar."""
    if not bins or total <= 0:
        return []
    rows = list(reversed(bins))
    at = next((i for i, b in enumerate(rows) if spot is not None and b.floor <= spot), None)
    if at is not None and spot is not None and spot >= rows[at].ceil and at > 0:
        at = None                                                           # spot sits in a gap between buckets
    labels = [kprice(b.floor) for b in rows]
    lw = max(len(s) for s in labels)
    top = max(b.supply for b in rows) or 1.0
    full = max(w - lw - 9, 4)                                               # label, space, 12.3%, space, the axis tick
    room = full
    marker = ""
    if at is not None and spot is not None:
        marker = pick(max(full - 5, 0), f"◂ {spot:,.0f} {word}", f"◂ {spot:,.0f}", "◂")
        frac = rows[at].supply / top
        if frac * full + len(marker) + 1 > full:
            room = max(int((full - len(marker) - 1) / max(frac, 1e-9)), 4)
    out = []
    for i, (b, label) in enumerate(zip(rows, labels, strict=True)):
        here = i == at
        colour = ORANGE if here or spot is None or b.floor <= spot else RED
        ln = Text(no_wrap=True, overflow="crop")
        ln.append(f"{label:>{lw}} ", style=f"bold {TEXT}" if here else DIM)
        ln.append(f"{b.supply / total * 100:4.1f}% ", style=TEXT if here else FAINT)
        ln.append("▏", style=ui.RULE)
        ln.append(charts.hbar(b.supply / top, room), style=charts.snap(colour))
        if here and marker:
            ln.append(" " + marker, style=f"bold {CYAN}")
        out.append(ln)
    return out


# ── ONCH ────────────────────────────────────────────────────

# key in onchain.toml, label at 8 columns, at 10, in the full table, decimals
ONCH_METRICS = (
    ("realized_price", "realized", "realized", "realized price", 0),
    ("mvrv", "MVRV", "MVRV", "MVRV", 2),
    ("nupl", "NUPL", "NUPL", "NUPL", 3),
    ("sopr", "SOPR", "SOPR 24h", "SOPR, 24 hours", 3),
    ("sth_cost_basis", "STH cost", "STH cost", "STH cost basis", 0),
    ("lth_cost_basis", "LTH cost", "LTH cost", "LTH cost basis", 0),
    ("puell_multiple", "Puell", "Puell", "Puell multiple", 2),
    ("mayer_multiple", "Mayer", "Mayer", "Mayer multiple", 2),
    ("supply_in_profit_pct", "profit", "in profit", "supply in profit", 1),
    ("supply_in_profit", "", "", "supply in profit, BTC", 0),              # the full table only
    ("liveliness", "liveli.", "liveliness", "liveliness", 3),
)


def regime(key: str, v: float | None, price: float | None = None) -> tuple[str, str] | None:
    """The one-word read where a classic threshold exists, with its colour. Thresholds are the textbook ones."""
    if v is None:
        return None
    if key == "mvrv":
        return ("hot", RED) if v > 3.5 else ("warm", YELLOW) if v > 2.4 else ("under cost", GREEN) if v < 1 else ("fair", DIM)
    if key == "nupl":
        return (("euphoria", RED) if v > 0.75 else ("belief", YELLOW) if v > 0.5 else ("optimism", DIM) if v > 0.25
                else ("hope", DIM) if v >= 0 else ("capitulation", GREEN))
    if key == "sopr":
        return ("even", DIM) if abs(v - 1) < 0.001 else ("in profit", GREEN) if v > 1 else ("at a loss", RED)
    if key == "mayer_multiple":
        return ("hot", RED) if v > 2.4 else ("cheap", GREEN) if v < 0.8 else ("above 200d", DIM) if v >= 1 else ("below 200d", YELLOW)
    if key == "puell_multiple":
        return ("hot", RED) if v > 4 else ("cheap", GREEN) if v < 0.5 else ("normal", DIM)
    if key == "supply_in_profit_pct":
        return ("hot", RED) if v > 95 else ("stress", GREEN) if v < 50 else ("normal", DIM)
    if key in ("realized_price", "sth_cost_basis", "lth_cost_basis") and price:
        return ("spot above", GREEN) if price >= v else ("spot below", RED)
    return None


def metric_text(v: float | None, unit: str, digits: int) -> str:
    if v is None:
        return "–"
    if unit == "pct":
        return f"{v:.{digits}f}%"
    if unit == "btc":
        return ui.big(v)
    return f"{v:,.{digits}f}"


def change_text(row: OnchRow) -> Text:
    if row.change is None:
        return Text("–", style=FAINT)
    if row.unit == "pct":                                   # a share moves in points, not in percent of itself
        out = ui.signed(row.change, 1)
        out.append("pp")
        return out
    return ui.signed_pct(row.change, 0 if abs(row.change) >= 1 else 1)


@dataclass
class OnchRow:
    key: str
    series: str
    short: str
    mid: str
    label: str
    unit: str
    value: float | None
    text: str
    change: float | None            # over 30 settled days: a fraction, or points for a share
    spark: list[float | None]
    read: tuple[str, str] | None


def onch_rows(got: dict[str, SeriesData]) -> list[OnchRow]:
    price_s = got.get(series_of("price_close"))
    price = price_s.settled() if price_s else None
    out = []
    for key, short, mid, label, digits in ONCH_METRICS:
        s = got.get(series_of(key))
        if s is None:
            continue
        unit, i = str(metric(key).get("unit", "")), settled_at(s)
        v = s.values[i] if i >= 0 else None
        old = s.values[i - 30] if i >= 30 else None
        chg = None if v is None or old in (None, 0) else (v - old) if unit == "pct" else v / old - 1
        out.append(OnchRow(key, s.name, short, mid, label, unit, v, metric_text(v, unit, digits), chg,
                           list(s.values[max(i - 59, 0):i + 1]), regime(key, v, price)))
    return out


class OnchPane(OnchainPane):
    code, every, selectable = "ONCH", 300, True
    KEYS = ("price_close", *[m[0] for m in ONCH_METRICS])

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.rows: list[OnchRow] = []
        self.oracle: float | None = None
        self.close: float | None = None                     # the settled daily close, for the oracle's gap
        self.urpd: dict[str, Any] = {}
        self.shown: list[OnchRow] = []
        self.note = ""

    async def load(self) -> None:
        self.title = "on-chain"
        bv = self.bitview()
        names = [n for k in self.KEYS if (n := series_of(k))]
        if not names:
            raise SourceError("onchain.toml carries no metrics")
        got, self.bad = await fetch_many(bv, names, -61)
        self.rows = onch_rows(got)
        first = next(iter(got.values()))
        self.day = first.times[settled_at(first)]
        if (p := got.get(series_of("price_close"))) is not None:
            self.close = p.settled()
        provs = [replace(first.prov, as_of=self.day)]
        try:
            self.oracle, p2 = await bv.oracle_price()
            provs.append(replace(p2, delay=DAILY))          # the page is a daily one; the oracle line says it is live
        except SourceError:
            pass
        try:                                                # the distribution of the same complete day
            dates, _ = await bv.urpd_dates("all")
            want = iso_day(self.day)
            day = want if want in dates else (dates[-1] if dates else "")
            self.urpd, p3 = await bv.urpd("all", day, "lin1000")
            provs.append(p3)
            self.note = ""
        except SourceError as e:
            self.note = f"URPD: {e}"
        self.provs = provs
        self.title = f"on-chain · {ui.day(self.day)}"

    # drawing ────────────────────────────────────────────────

    def _urpd(self, w: int, rows: int) -> list[Text]:
        d = self.urpd
        if not d:
            return [ui.section("URPD", w), ui.note(self.note or "URPD: not loaded", RED if self.note else FAINT)]
        day = short_day(iso_ts(str(d.get("date", ""))) or self.day)
        close = float(d.get("close", 0) or 0)
        spot, word = self.spot(close)
        bins = merge_lin(urpd_bins(d), 5000.0 if rows <= 26 else 2000.0 if rows <= 64 else 1000.0)
        step = bins[0].ceil - bins[0].floor if bins else 5000.0
        bins = [b for b in bins if b.floor > 0]             # the first bucket holds the early coins and would flatten the rest
        right = pick(w - 14, f"share of supply by the price it last moved at · {step / 1000:g}k buckets",
                     "share of supply by the price it last moved at", "supply by last-moved price", "by last-moved price")
        out = [ui.section(f"URPD {day}", w, right)]
        return out + urpd_bars(around_spot(bins, spot or close, max(rows, 1)), float(d.get("total_supply", 0) or 0), spot, w, word)

    def _oracle(self, room: int) -> Text | None:
        if not self.oracle:
            return None
        gap = self.oracle / self.close - 1 if self.close else None
        full = ui.t(ui.kv("chain oracle", ui.px(self.oracle, 0), f"bold {CYAN}"), ("  ", ""), ui.signed_pct(gap, 2) if gap is not None else "",
                    (f" on the close of {short_day(self.day)} · read from the chain alone", FAINT))
        mid = ui.t(ui.kv("chain oracle", ui.px(self.oracle, 0), f"bold {CYAN}"), ("  read from the chain alone", FAINT))
        less = ui.t(ui.kv("oracle", ui.px(self.oracle, 0), f"bold {CYAN}"), (" from the chain alone", FAINT))
        small = ui.kv("oracle", ui.px(self.oracle, 0), f"bold {CYAN}")
        return next((x for x in (full, mid, less, small) if x.cell_len <= room), small)

    def _grid(self, w: int, dense: bool = False) -> list[Text]:
        """Metrics in columns. A short pane gets label and value only, so the URPD under it keeps its bars."""
        rows = self.shown
        ncols = max(1, min(4, (w + 2) // (19 if dense and w < 106 else 36 if w >= 106 else 23)))
        cw = (w - 2 * (ncols - 1)) // ncols
        lw = 8 if cw < 26 else 10
        with_chg = cw >= lw + 15
        with_read = cw >= 46
        sw = min(cw - lw - 15 - (11 if with_read else 0) - 1, 20) if with_chg else 0
        lines: list[Text] = []
        for i in range(0, len(rows), ncols):
            line = Text(no_wrap=True)
            for j, r in enumerate(rows[i:i + ncols]):
                cell = Text(no_wrap=True)
                cell.append(f"{(r.short if lw == 8 else r.mid):<{lw}}", style=FAINT)
                cell.append(f"{r.text:>8}", style=f"bold {r.read[1] if r.read and r.read[1] != DIM else TEXT}")
                if with_chg:
                    c = change_text(r)
                    cell.append(" " * max(7 - c.cell_len, 1))
                    cell.append_text(c)
                if sw >= 6:
                    cell.append(" " + charts.spark(r.spark, sw), style=ORANGE)
                if with_read and r.read:
                    cell.pad_right(max(cw - 11 - cell.cell_len, 0))
                    cell.append(f" {r.read[0]:<10}", style=r.read[1])
                cell.truncate(cw)
                cell.pad_right(cw - cell.cell_len)
                if i + j == self.cur:
                    cell.stylize(f"on {ui.CURSOR_BG}")
                if j:
                    line.append("  ")
                line.append_text(cell)
            lines.append(line)
        used = (len(rows) % ncols) * (cw + 2) if len(rows) % ncols else w + 1
        if (o := self._oracle(w - used if w - used >= 13 else w)) is not None:
            if w - used >= 13 and lines:
                lines[-1].pad_right(used - lines[-1].cell_len)
                lines[-1].append_text(o)
            else:
                lines.append(o)
        return lines

    def _table(self, w: int) -> list[Text]:
        rows = self.shown
        wide, narrow = w >= 104, w < 50
        fixed = (10 if narrow else 21) + 7 + 6 + 12 + (22 if wide else 0)
        sw = min(w - fixed - 2 * (5 if wide else 4), 60)
        cols = [ui.Col("metric", "left", style=DIM), ui.Col("settled"), ui.Col("30 d")]
        if sw >= 6:
            cols.append(ui.Col("60 days", "left"))
        cols.append(ui.Col("read", "left"))
        if wide:
            cols.append(ui.Col("series", "left", style=FAINT))
        body = []
        for r in rows:
            cells: list[ui.Cell] = [(r.mid or "in BTC") if narrow else r.label, Text(r.text, style=f"bold {TEXT}"), change_text(r)]
            if sw >= 6:
                cells.append(Text(charts.spark(r.spark, sw), style=ORANGE))
            cells.append(Text(r.read[0], style=r.read[1]) if r.read else Text("·", style=FAINT))
            if wide:
                cells.append(r.series)
            body.append(cells)
        out = [ui.section("CYCLE METRICS", w, pick(w - 16, f"daily · the complete day {ui.day(self.day)} UTC", f"daily · {ui.day(self.day)}"))]
        out += ui.table(cols, body, w, cursor=self.cur)
        if (o := self._oracle(w)) is not None:
            out.append(o)
        return out

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.rows:
            return []
        table = h >= 22 and w >= 44
        self.shown = [r for r in self.rows if table or r.short]
        self.n_rows = len(self.shown)
        self.cur = min(self.cur, max(self.n_rows - 1, 0))
        if table:
            side = w >= 130
            lw = min(max(w * 6 // 10, 84), 124) if side else w
            left = self._table(lw) + self.bad_lines()
            self.follow(3 + self.cur, h)
            if side:
                return fit(beside(left, self._urpd(w - lw - 3, h - 1), lw), w)
            return fit(left + [Text("")] + self._urpd(w, max(h - len(left) - 2, 4)), w)
        grid = self._grid(w, dense=h < 11) + self.bad_lines()
        return fit(grid + self._urpd(w, max(h - len(grid) - 1, 3)), w)

    def enter(self) -> str | None:
        return f"GP {self.shown[self.cur].series}" if self.shown and self.cur < len(self.shown) else None

    def menu(self) -> list[tuple[str, str]]:
        return [("URPD", "URPD"), ("CYC", "CYC"), ("WAVE", "WAVE"), ("CORR", "CORR"), ("FLDS", "FLDS")]

    def hint(self) -> str:
        return "j k metric · enter charts it"

    def export(self):
        return (["metric", "series", "day", "value", "change_30d", "read"],
                [[r.label, r.series, iso_day(self.day), r.value, r.change, r.read[0] if r.read else ""] for r in self.rows]
                + ([["chain oracle (live)", "/api/oracle/price", "", self.oracle, None, ""]] if self.oracle else []))


# ── URPD ────────────────────────────────────────────────────

class UrpdPane(OnchainPane):
    code, every = "URPD", 1800
    CYCLE = ("all", "sth", "lth", "epoch_4", "epoch_3", "class_2026", "class_2025", "p2tr")

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        words = [a.lower() for a in self.args]
        self.date = next((a for a in words if DATE.match(a)), "")           # '' is the latest
        self.logb = "lin" not in words and "linear" not in words
        rest = [a for a in words if not DATE.match(a) and a not in ("lin", "linear", "log")]
        self.cohort = rest[0] if rest else "all"
        self.cohorts: list[str] = []
        self.dates: list[str] = []
        self.data: dict[str, Any] = {}
        self.note = ""
        self.rows_seen = 0

    def agg(self) -> str:
        """Log buckets at ten a decade for a small pane and fifty for a tall one; linear ones always at $1,000."""
        return "lin1000" if not self.logb else "log50" if self.rows_seen >= 30 else "log10"

    def state(self) -> tuple:
        return (self.cohort, self.date, self.agg())

    async def load(self) -> None:
        bv, state = self.bitview(), self.state()
        self.title = f"URPD · {self.cohort}"
        self.cohorts, _ = await bv.urpd_cohorts()
        self.note = ""
        if self.cohort not in self.cohorts:
            self.note = f"Bitview has no URPD cohort called {self.cohort}. Showing all. c cycles the cohorts it has."
            self.cohort = "all"
        cohort, date, agg = self.cohort, state[1], state[2]
        try:
            self.dates, _ = await bv.urpd_dates(cohort)
        except SourceError:
            self.dates = []
        if date and self.dates and date not in self.dates:
            self.note = f"No distribution for {date}. Showing the latest. [ and ] step through the days that exist."
            date = self.date = ""
        latest = not date or (self.dates and date == self.dates[-1])
        self.data, prov = await bv.urpd(cohort, "" if latest else date, agg)
        self.day = prov.as_of
        self.provs = [prov]
        self._have = (cohort, self.date, agg)
        self.title = f"URPD · {cohort} · {ui.day(self.day)}"

    def command(self) -> str:
        return " ".join(x for x in ("URPD", self.cohort, self.date, "" if self.logb else "lin") if x)

    def key(self, k: str, ch: str | None) -> bool:
        if ch == "c":
            ring = [c for c in dict.fromkeys((*self.CYCLE, self.cohort)) if not self.cohorts or c in self.cohorts]
            self.cohort = ring[(ring.index(self.cohort) + 1) % len(ring)] if self.cohort in ring else ring[0]
            self.date = ""
        elif ch in ("[", "]") and self.dates:
            at = self.dates.index(self.date) if self.date in self.dates else len(self.dates) - 1
            at = max(0, min(at + (1 if ch == "]" else -1), len(self.dates) - 1))
            self.date = "" if at == len(self.dates) - 1 else self.dates[at]
        elif ch == "l":
            self.logb = not self.logb
        else:
            return False
        self.again()
        return True

    def hint(self) -> str:
        return "c cohort · [ ] day · l log/linear"

    def menu(self) -> list[tuple[str, str]]:
        return [("ONCH", "ONCH"), ("CYC", "CYC"), ("WAVE", "WAVE")]

    def view(self, rows: int) -> tuple[list[Bin], Bin | None, Bin | None, str]:
        """(bars ascending, the bucket shown apart, everything folded under the lowest bar, the bucket wording)."""
        d = self.data
        kind, n = agg_step(str(d.get("aggregation", "")))
        raw = urpd_bins(d)
        if kind == "log":
            m = max(math.ceil(1.5 * n / max(rows, 1)), 1)
            bins = merge_log(raw, n, m)
            old = [b for b in raw if b.floor < 1]
            apart = Bin(0.0, 1.0, sum(b.supply for b in old)) if old else None
            word = f"log buckets, {n / m:g} a decade"
        else:
            step = lin_step(raw[-1].floor if raw else 0.0, rows)
            merged = merge_lin(raw, step)
            apart = merged[0] if merged and merged[0].floor == 0 else None
            bins = [b for b in merged if b.floor > 0]
            word = f"${step:,.0f} buckets"
        cut = bins[:-rows] if len(bins) > rows else []
        under = Bin(cut[0].floor, cut[-1].ceil, sum(b.supply for b in cut)) if cut else None
        return bins[-rows:], apart, under, word

    def draw(self, w: int, h: int) -> list[Text]:
        d = self.data
        small = h < 12                                      # a small pane keeps the bars and drops the two lines of prose
        rows = max(h - (3 if small else 5) - (1 if self.note else 0), 3)
        self.rows_seen = rows
        self.want(self.state())
        if not d or not d.get("buckets"):
            return [ui.note(self.note, RED)] if self.note else []
        total, close = float(d.get("total_supply", 0) or 0), float(d.get("close", 0) or 0)
        log = agg_step(str(d.get("aggregation", "")))[0] == "log"
        spot, word = (self.spot(close) if not self.date else (close, "close"))
        bins, apart, under, buckets = self.view(rows)
        forming = " (today, still forming)" if iso_day(time.time()) == str(d.get("date", "")) else ""
        head = f"URPD {d.get('cohort', self.cohort)}"
        day = ui.day(self.day)
        out = [ui.section(head, w, pick(w - len(head) - 3, f"daily · {day}{forming} · {buckets}", f"daily · {day} · {buckets}", f"daily · {day}",
                                        short_day(self.day)))]
        if self.note:
            out.append(ui.note(self.note, RED))
        if total > 0 and spot:
            below, above = profit_split(urpd_bins(d), spot, log)
            wide = w >= 72
            than = f" below {'spot' if word == 'spot' else 'the close'}" if wide else ""
            out += flow([ui.t(ui.kv("in profit", f"~{below / total:.1%}", f"bold {GREEN}"), (than, FAINT)),
                         ui.t(ui.kv("in loss", f"~{above / total:.1%}", f"bold {RED}"), (" above" if wide else "", FAINT)),
                         ui.kv(word, ui.px(spot, 0), f"bold {CYAN}"), ui.kv("supply", f"{ui.big(total)} BTC")], w, 3 if wide else 2)[:1]
        parts = []
        if apart and total > 0:
            what = "under $1, the earliest coins" if log else f"under ${kprice(apart.ceil)}"
            parts.append(f"{what}: {apart.supply / total:.1%}" + (f" ({ui.big(apart.supply)} BTC)" if w >= 96 else ""))
        if under and total > 0:
            parts.append(f"${kprice(under.floor)} to ${kprice(under.ceil)}: {under.supply / total:.1%}")
        if parts and not small:
            out.append(ui.t(("shown apart  ", FAINT), (" · ".join(parts), DIM)))
        out += urpd_bars(bins, total, spot, w, word)
        if total > 0 and bins and not small:
            line = Text("largest clusters  " if w >= 60 else "clusters  ", style=FAINT, no_wrap=True)
            for i, b in enumerate(sorted(bins, key=lambda b: -b.supply)[:3]):
                span = f"{kprice(b.floor)} to {kprice(b.ceil)} "
                item = ui.t((" · " if i else "", FAINT), (span, f"bold {TEXT}"), (f"{b.supply / total:.1%}", ORANGE))
                if line.cell_len + item.cell_len > w:
                    break
                line.append_text(item)
            out.append(line)
        return fit(out, w)

    def export(self):
        d = self.data
        return (["cohort", "date", "aggregation", "price_floor", "supply_btc", "share", "realized_cap", "unrealized_pnl"],
                [[d.get("cohort"), d.get("date"), d.get("aggregation"), b.get("price_floor"), b.get("supply"),
                  (b.get("supply") or 0) / (d.get("total_supply") or 1), b.get("realized_cap"), b.get("unrealized_pnl")]
                 for b in d.get("buckets", [])])


# ── WAVE ────────────────────────────────────────────────────

WAVE_STOPS: tuple[tuple[float, charts.RGB], ...] = (
    (0.0, (235, 60, 50)), (0.18, (255, 125, 8)), (0.36, (255, 208, 72)), (0.54, (11, 217, 138)), (0.7, (84, 190, 232)),
    (0.85, (95, 135, 255)), (1.0, (180, 140, 255)))
WAVE_GROUPS = (("< 1d", 0, 2), ("1d - 1m", 2, 4), ("1m - 6m", 4, 9), ("6m - 1y", 9, 11), ("1y - 2y", 11, 13), ("2y - 5y", 13, 16),
               ("5y - 10y", 16, 20), ("> 10y", 20, 23))


def wave_colours(n: int) -> list[str]:
    """Young coins warm, old coins cool."""
    return [charts.hex_of(charts.ramp(WAVE_STOPS, i / max(n - 1, 1))) for i in range(n)]


def wave_groups(n: int) -> list[tuple[str, int, int]]:
    """The 23 bands of onchain.toml in eight display bands. Any other count is cut into runs of three."""
    if n == 23:
        return list(WAVE_GROUPS)
    return [(f"bands {a + 1}-{min(a + 3, n)}", a, min(a + 3, n)) for a in range(0, n, 3)]


class WavePane(OnchainPane):
    code, every = "WAVE", 3600
    # Bitview weighs a request at eight bytes a value and refuses more than 320,000: 23 bands fit four years of days
    # (269,008) but not all of history (1,190,664). The whole history is read on the weekly index instead.
    WINDOWS = (("60 d", "day1", -61), ("400 d", "day1", -401), ("4 y", "day1", -1462), ("all", "week1", 0))

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        words = [a.lower() for a in self.args]
        self.win = next((i for i, w in enumerate(self.WINDOWS) if w[0].replace(" ", "") in words), 1)
        self.weekly = False
        self.labels: list[str] = []
        self.names: list[str] = []
        self.times: list[float] = []
        self.matrix: list[list[float]] = []                 # one row a complete day: the share of every band, youngest first

    async def load(self) -> None:
        self.title = "HODL waves"
        bands, win = wave_bands(), self.win
        if not bands:
            raise SourceError("onchain.toml carries no age bands")
        name, index, start = self.WINDOWS[win]
        if index == "day1":
            got, self.bad = await fetch_many(self.bitview(), [s for _, s in bands], start)
            kept = [(label, s) for label, s in bands if s in got]
            first = got[kept[0][1]]
            n = settled_at(first) + 1                       # today's point is left out: it is still forming
            times, prov = first.times[:n], first.prov
            matrix = [[got[s].values[i] or 0.0 if i < len(got[s].values) else 0.0 for _, s in kept] for i in range(n)]
        else:
            kept, self.bad = bands, {}
            times, matrix, prov = await self._coarse([s for _, s in bands], index, start)
        self.labels, self.names = [b[0] for b in kept], [b[1] for b in kept]
        self.times, self.matrix, self.weekly = times, matrix, index != "day1"
        self.day = self.times[-1] if self.times else 0.0
        self.provs = [replace(prov, as_of=self.day)]
        self._have, self.stamp = win, self.stamp + 1
        self.title = f"HODL waves · {name} · to {'the week of ' if self.weekly else ''}{ui.day(self.day)}"

    async def _coarse(self, names: list[str], index: str, start: int) -> tuple[list[float], list[list[float]], Provenance]:
        """An index with no fixed calendar (week1): the catalogue's own date series, asked for in the same request, dates
        each point. The client's `bulk` keeps numbers only, so this reads the envelopes itself."""
        bv = self.bitview()
        dates = str(catalogue().get("source", {}).get("date_series", ""))
        if not dates:
            raise SourceError("onchain.toml names no date series, so weekly points cannot be dated")
        raw, at = await bv.get("/api/series/bulk", {"series": ",".join([dates, *names]), "index": index, "start": start}, ttl=3600, persist=True)
        if not isinstance(raw, list) or len(raw) != len(names) + 1 or not all(isinstance(e, dict) and "data" in e for e in raw):
            raise SourceError(f"{bv.name}: bulk answered {len(raw) if isinstance(raw, list) else 0} of {len(names) + 1} series")
        n = max(len(raw[0]["data"]) - 1, 0)                 # the last period is the current one, still forming
        times = [iso_ts(str(d)) for d in raw[0]["data"][:n]]
        cols = [[float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0 for v in e["data"][:n]] for e in raw[1:]]
        matrix = [[c[i] if i < len(c) else 0.0 for c in cols] for i in range(n)]
        return times, matrix, Provenance(bv.name, at, times[-1] if times else at, DAILY)

    def command(self) -> str:
        return f"WAVE {self.WINDOWS[self.win][0].replace(' ', '')}"

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            new = max(0, min(self.win + (1 if ch == "]" else -1), len(self.WINDOWS) - 1))
            if new != self.win:
                self.win = new
                self.again()
            return True
        return False

    def cache_key(self) -> tuple:
        return ()

    def hint(self) -> str:
        return "[ ] window: 60d 400d 4y all"

    def menu(self) -> list[tuple[str, str]]:
        return [("URPD", "URPD"), ("ONCH", "ONCH"), ("CYC", "CYC")]

    def bands(self, grouped: bool) -> tuple[list[str], list[str], list[list[float]]]:
        """(labels, colours, one row a day) for the 23 bands or for the eight display bands."""
        colours = wave_colours(len(self.labels))
        if not grouped:
            return self.labels, colours, self.matrix
        groups = wave_groups(len(self.labels))
        return ([g[0] for g in groups], [colours[(a + b - 1) // 2] for _, a, b in groups],
                [[sum(row[a:b]) for _, a, b in groups] for row in self.matrix])

    def _picture(self, pw: int, ph: int, colours: list[str], days: list[list[float]]) -> list[Text]:
        n = len(days)
        cols = [days[round(x * (n - 1) / max(pw - 1, 1))] for x in range(pw)]
        pic = charts.stacked(cols, colours, ph)
        ticks = {0: "100%", ph // 2: "50%", ph - 1: "0%"}
        for r, ln in enumerate(pic):
            ln.append(f"┤{ticks.get(r, ''):<4}", style=FAINT)
        span = self.times[-1] - self.times[0] if n > 1 else 0.0
        axis = [" "] * pw
        for frac in (0.0, 0.5, 1.0):
            label = charts.time_label(self.times[0] + span * frac, span)
            at = min(max(round((pw - 1) * frac) - (len(label) if frac == 1 else len(label) // 2 if frac else 0), 0), max(pw - len(label), 0))
            if all(c == " " for c in axis[max(at - 1, 0):at + len(label) + 1]):
                axis[at:at + len(label)] = label
        return pic + [Text("".join(axis)[:pw], style=FAINT, no_wrap=True)]

    def draw(self, w: int, h: int) -> list[Text]:
        self.want(self.win)
        if not self.matrix:
            return []
        side = w >= 100 and h >= 12
        grouped = not (h >= 27 if side else h >= 38)
        labels, colours, days = self.bands(grouped)
        now, then = days[-1], days[0]
        said = f"{len(self.labels)} age bands summed into {len(labels)}" if grouped else f"{len(labels)} age bands"
        name = self.WINDOWS[self._have if isinstance(self._have, int) else self.win][0]
        pace, to = ("weekly", f"the week of {ui.day(self.day)}") if self.weekly else ("daily", ui.day(self.day))
        head = ui.section("HODL WAVES", w, pick(w - 13, f"{pace} · {name} to {to} · share of supply by coin age · {said}",
                                                f"{pace} · {name} to {to} · {said}", f"{pace} · to {ui.day(self.day)} · {said}",
                                                f"{pace} · {short_day(self.day)} · {len(labels)} bands", short_day(self.day)))
        out = [head] + self.bad_lines()
        if side:
            tw = 60 if w >= 150 else 36
            sw = tw - 36
            cols = [ui.Col("age", "left"), ui.Col("share"), ui.Col(f"{name} chg")]
            if sw >= 8:
                cols.append(ui.Col(name, "left"))
            body = []
            for i in reversed(range(len(labels))):              # oldest first, as the picture stacks them
                cells: list[ui.Cell] = [ui.t(("█ ", charts.snap(colours[i])), (labels[i], DIM)), Text(f"{now[i]:.2f}%", style=f"bold {TEXT}"),
                                        ui.t(ui.signed(now[i] - then[i], 2), ("pp", FAINT))]
                if sw >= 8:
                    cells.append(Text(charts.spark([d[i] for d in days], sw), style=charts.snap(colours[i])))
                body.append(cells)
            pw, ph = w - tw - 3 - 5, max(h - len(out) - 1, 4)
            pic = self.pic((pw, ph, self.stamp, grouped), lambda: self._picture(pw, ph, colours, days))
            return fit(out + beside(pic, ui.table(cols, body, tw), pw + 5), w)
        tight = w < 60
        names = [x.replace(" ", "") if tight else x for x in labels]
        items = [ui.t(("█ ", charts.snap(colours[i])), (f"{names[i]} ", DIM), (f"{now[i]:.1f}%", f"bold {TEXT}"))
                 for i in reversed(range(len(labels)))]
        cell = max(it.cell_len for it in items)
        for it in items:
            it.pad_right(cell - it.cell_len)
        legend = flow(items, w, 1 if tight else 2)
        pw, ph = w - 5, max(h - len(out) - len(legend) - 1, 4)
        pic = self.pic((pw, ph, self.stamp, grouped), lambda: self._picture(pw, ph, colours, days))
        return fit(out + pic + legend, w)

    def export(self):
        return ["date", *self.labels], [[iso_day(t), *row] for t, row in zip(self.times, self.matrix, strict=True)]


# ── CYC ─────────────────────────────────────────────────────

CYC_LINES = (("price_close", "price, daily close", ORANGE), ("realized_price", "realized price", CYAN),
             ("sth_cost_basis", "STH cost basis", YELLOW), ("lth_cost_basis", "LTH cost basis", VIOLET),
             ("price_sma_200d", "200-day average", GREEN))
CYC_SHORT = {"price_close": "price", "realized_price": "realized", "sth_cost_basis": "STH", "lth_cost_basis": "LTH", "price_sma_200d": "200d"}
CYC_NOTES = {"price_close": "Bitview's daily close: its chain oracle, not an exchange ticker",
             "realized_price": "the average price at which every coin last moved",
             "sth_cost_basis": "realized price of coins younger than 150 days",
             "lth_cost_basis": "realized price of coins at least 150 days old",
             "price_sma_200d": "the simple average of the last 200 days"}


class CycPane(OnchainPane):
    code, every, selectable = "CYC", 1800, True
    WINDOWS = (("400 d", -401), ("4 y", -1462), ("all", 0))

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        words = [a.lower() for a in self.args]
        self.win = next((i for i, (n, _) in enumerate(self.WINDOWS) if n.replace(" ", "") in words), 0)
        self.logy = "lin" not in words and "linear" not in words
        self.lines: list[tuple[str, str, str, str, list[float], list[float | None]]] = []   # key, series, label, colour, xs, ys

    async def load(self) -> None:
        self.title = "valuation bands"
        win = self.win
        names = {k: n for k, _, _ in CYC_LINES if (n := series_of(k))}
        got, self.bad = await fetch_many(self.bitview(), list(names.values()), self.WINDOWS[win][1])
        lines = []
        for key, label, colour in CYC_LINES:
            s = got.get(names.get(key, ""))
            if s is None:
                continue
            n = settled_at(s) + 1                           # complete days only
            lines.append((key, s.name, label, colour, s.times[:n], [v if v else None for v in s.values[:n]]))
        if not lines:
            raise SourceError("bitview: none of the valuation series answered")
        self.lines = lines
        self.day = lines[0][4][-1]
        first = next(iter(got.values()))
        self.provs = [replace(first.prov, as_of=self.day)]
        self._have, self.stamp = win, self.stamp + 1
        self.title = f"valuation bands · {self.WINDOWS[win][0]} · to {ui.day(self.day)}"

    def command(self) -> str:
        return " ".join(x for x in ("CYC", self.WINDOWS[self.win][0].replace(" ", ""), "" if self.logy else "lin") if x)

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            new = max(0, min(self.win + (1 if ch == "]" else -1), len(self.WINDOWS) - 1))
            if new != self.win:
                self.win = new
                self.again()
            return True
        if ch == "L":
            self.logy = not self.logy
            return True
        return False

    def hint(self) -> str:
        return "[ ] window · L log/linear · enter charts a line"

    def menu(self) -> list[tuple[str, str]]:
        return [("ONCH", "ONCH"), ("URPD", "URPD"), ("GP", "GP BTC"), ("WAVE", "WAVE")]

    def enter(self) -> str | None:
        return f"GP {self.lines[self.cur][1]}" if self.cur < len(self.lines) else None

    def _chart(self, w: int, ph: int, spot: float | None, word: str) -> list[Text]:
        plot = charts.Plot(w, ph, log=self.logy)
        for _, _, label, colour, xs, ys in reversed(self.lines):         # price is drawn last, so it lies on top
            rx, ry = charts.resample(xs, ys, max(w * 2, 40))
            plot.line(rx, ry, colour, label=label)
        if spot and word == "spot":
            plot.hline(spot, CYAN, "spot", glyph="┄")           # the legend carries the number; a row is coarser than a dollar
        return plot.render().split("\n")

    def draw(self, w: int, h: int) -> list[Text]:
        self.want(self.win)
        if not self.lines:
            return []
        self.n_rows = len(self.lines)
        self.cur = min(self.cur, self.n_rows - 1)
        close = next((y for y in reversed(self.lines[0][5]) if y is not None), None) if self.lines[0][0] == "price_close" else None
        spot, word = self.spot(close)
        name = self.WINDOWS[self._have if isinstance(self._have, int) else self.win][0]
        scale = "log" if self.logy else "linear"
        out = [ui.section("VALUATION BANDS", w, pick(w - 17, f"daily · {name} to {ui.day(self.day)} UTC · {scale} scale",
                                                     f"daily · {name} to {ui.day(self.day)} · {scale}", f"daily · to {short_day(self.day)}"))]
        out += self.bad_lines()
        rows = []
        for key, series, label, colour, _, ys in self.lines:
            v = next((y for y in reversed(ys) if y is not None), None)
            rows.append((series, label, colour, v, (spot / v - 1) if spot and v else None, key))
        table = h >= 15
        if table:
            cols = [ui.Col("line", "left"), ui.Col(short_day(self.day)), ui.Col(f"{word} vs line")]
            if w >= 84:
                cols.append(ui.Col("series", "left", style=FAINT))
            if w >= 120:
                cols.append(ui.Col("what it is", "left", style=FAINT, flex=True))
            body = [[ui.t(("━━ ", charts.snap(c)), (label, DIM)), Text(ui.px(v, 0), style=f"bold {TEXT}"), ui.signed_pct(d),
                     *([series] if w >= 84 else []), *([CYC_NOTES.get(key, "")] if w >= 120 else [])] for series, label, c, v, d, key in rows]
            legend = ui.table(cols, body, w, cursor=self.cur)
            if spot:
                legend.append(ui.t((f"{word} ", FAINT), (ui.px(spot, 0), f"bold {CYAN}"),
                                   ("  the live composite" if word == "spot" else f"  the daily close of {ui.day(self.day)}", FAINT)))
        else:
            items = []
            for i, (_, label, c, v, d, key) in enumerate(rows):
                it = ui.t(("━ ", charts.snap(c)), (f"{CYC_SHORT.get(key, label)} ", DIM), (ui.px(v, 0), f"bold {TEXT}"))
                if d:
                    it.append(" ")
                    it.append_text(ui.signed_pct(d, 0))
                if i == self.cur:
                    it.stylize(f"on {ui.CURSOR_BG}")
                items.append(it)
            legend = flow(items, w, 2)
        ph = max(h - len(out) - len(legend), 4)
        key = (w, ph, self.stamp, self.logy, int(math.log(spot) * 400) if spot and word == "spot" else 0)   # redrawn for a 0.25% move
        out += self.pic(key, lambda: self._chart(w, ph, spot, word))
        if table:
            self.follow(len(out) + 2 + self.cur, h)
        return fit(out + legend, w)

    def export(self):
        if not self.lines:
            return None
        xs = self.lines[0][4]
        return ["date", *[ln[1] for ln in self.lines]], [[iso_day(t), *[ln[5][i] if i < len(ln[5]) else None for ln in self.lines]]
                                                          for i, t in enumerate(xs)]


# ── CORR: the maths ─────────────────────────────────────────

def log_returns(values: Sequence[float]) -> list[float | None]:
    """ln(p[i] / p[i-1]), one shorter than the input. A price at or under zero gives None."""
    return [math.log(b / a) if a > 0 and b > 0 else None for a, b in zip(values, values[1:], strict=False)]


def differences(values: Sequence[float]) -> list[float | None]:
    """First differences, for a yield: a move from 4.10 to 4.20 is +0.10 whatever the level."""
    return [b - a for a, b in zip(values, values[1:], strict=False)]


def align(base_t: Sequence[float], base_v: Sequence[float], other_t: Sequence[float], other_v: Sequence[float],
          ) -> tuple[list[float], list[float], list[float]]:
    """Join on the UTC date. Bitcoin trades every day and the others only on business days, so the join keeps the
    other's dates: a Friday to Monday return of the S&P meets Bitcoin's return over the same three days."""
    have = {int(t // DAY): v for t, v in zip(base_t, base_v, strict=True)}
    rows = [(int(t // DAY), v) for t, v in zip(other_t, other_v, strict=True) if int(t // DAY) in have]
    rows = sorted(dict(rows).items())
    return [d * DAY for d, _ in rows], [have[d] for d, _ in rows], [v for _, v in rows]


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n < 3 or n != len(y):
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return max(-1.0, min(1.0, sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / math.sqrt(sxx * syy)))


def rolling_corr(x: Sequence[float], y: Sequence[float], n: int) -> list[float | None]:
    """Pearson over each trailing window of `n` points, in one pass with running sums. None until a window is full."""
    out: list[float | None] = [None] * len(x)
    if n < 3 or len(x) != len(y):
        return out
    sx = sy = sxx = syy = sxy = 0.0
    for i, (a, b) in enumerate(zip(x, y, strict=True)):
        sx, sy, sxx, syy, sxy = sx + a, sy + b, sxx + a * a, syy + b * b, sxy + a * b
        if i >= n:
            p, q = x[i - n], y[i - n]
            sx, sy, sxx, syy, sxy = sx - p, sy - q, sxx - p * p, syy - q * q, sxy - p * q
        if i >= n - 1:
            vx, vy = sxx - sx * sx / n, syy - sy * sy / n
            if vx > 1e-18 and vy > 1e-18:
                out[i] = max(-1.0, min(1.0, (sxy - sx * sy / n) / math.sqrt(vx * vy)))
    return out


def paired_returns(base_t: Sequence[float], base_v: Sequence[float], other_t: Sequence[float], other_v: Sequence[float],
                   diff: bool = False) -> tuple[list[float], list[float], list[float]]:
    """(time, Bitcoin's log return, the other's return) on the shared dates. `diff` takes first differences of the other."""
    t, a, b = align(base_t, base_v, other_t, other_v)
    ra, rb = log_returns(a), (differences(b) if diff else log_returns(b))
    rows = [(t[i + 1], p, q) for i, (p, q) in enumerate(zip(ra, rb, strict=True)) if p is not None and q is not None]
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


def diverging(v: float | None, half: int) -> Text:
    """A bar from the centre: red to the left for a negative correlation, green to the right for a positive one."""
    out = Text(no_wrap=True)
    if v is None:
        out.append(" " * half + "│" + " " * half, style=FAINT)
        return out
    if v < 0:
        halves = round(min(-v, 1.0) * half * 2)
        body = ("▐" if halves % 2 else "") + "█" * (halves // 2)
        out.append(" " * (half - len(body)))
        out.append(body, style=charts.snap(RED))
        out.append("│", style=FAINT)
        out.append(" " * half)
    else:
        body = charts.hbar(min(v, 1.0), half)
        out.append(" " * half)
        out.append("│", style=FAINT)
        out.append(body, style=charts.snap(GREEN))
        out.append(" " * (half - len(body)))
    return out


# ── CORR: the page ──────────────────────────────────────────

async def btc_history(hub: Any, days: int) -> tuple[list[float], list[float], Provenance]:
    """Bitcoin's daily closes, Kraken first: it answers 720 days where Coinbase stops at 300, and a 180-day window of
    business days needs the longer run."""
    btc = hub.book.get("BTC")
    if btc is None:
        raise SourceError("BTC is not in instruments.toml")
    t, v, p = await quotes.history(hub, replace(btc, sources=tuple(sorted(btc.sources, key=lambda s: not s.startswith("kraken:")))), days)
    return list(t), list(v), p

CORR_ASSETS = (("SPX", "S&P 500", False), ("NDX", "Nasdaq 100", False), ("XAU", "gold", False), ("DXY", "dollar index", False),
               ("US10Y", "10-year yield", True))


@dataclass
class CorrRow:
    ticker: str
    label: str
    colour: str
    source: str = ""
    error: str = ""
    times: list[float] | None = None
    now: dict[int, float | None] | None = None              # window -> the latest value
    rolling: dict[int, list[float | None]] | None = None
    points: int = 0


class CorrPane(OnchainPane):
    code, every, selectable = "CORR", 3600, True
    WINDOWS = (30, 90, 180)

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        nums = [int(a) for a in self.args if a.isdigit() and int(a) in self.WINDOWS]
        self.win = self.WINDOWS.index(nums[0]) if nums else 1
        self.rows: list[CorrRow] = []

    async def _history(self, ticker: str, days: int) -> tuple[list[float], list[float], Provenance, str]:
        """Daily closes and a plain-words source. A stand-in (PAXG for gold) and a computed index say what they are."""
        hub = self.hub
        ins = hub.book.get(ticker)
        if ins is None:
            raise SourceError(f"{ticker} is not in instruments.toml")
        if "computed:dxy" in ins.sources:
            legs: dict[str, dict[int, float]] = {}
            provs, mixed = [], False
            for pair in fx_ref.DXY_WEIGHTS:
                leg = hub.book.get(pair)
                if leg is None:
                    raise SourceError(f"dollar index: {pair} is not in instruments.toml")
                t, v, p = await quotes.history(hub, leg, days)
                legs[pair] = {int(x // DAY): y for x, y in zip(t, v, strict=True)}
                provs.append(p)
                if len(t) < 200 and (sid := leg.source("fred")) and not p.source.startswith("fred"):
                    try:                                    # the ECB file carries 90 days: FRED's daily rate fills in the older ones
                        t2, v2, _ = await quotes.history(hub, replace(leg, sources=(f"fred:{sid}",), proxy=""), days)
                        legs[pair] = {int(x // DAY): y for x, y in zip(t2, v2, strict=True)} | legs[pair]
                        mixed = True
                    except SourceError:
                        pass
            shared = sorted(set.intersection(*[set(m) for m in legs.values()]))
            pts = [(d * DAY, val) for d in shared if (val := fx_ref.dxy({p: legs[p][d] for p in legs}))]
            if len(pts) < 5:
                raise SourceError("dollar index: the six FX legs share too few days")
            oldest = min(provs, key=lambda p: p.fetched_at)
            prov = Provenance("computed", oldest.fetched_at, pts[-1][0], DAILY, "ICE formula over six FX legs")
            how = "ECB and FRED rates, exchange closes" if mixed else "daily rates, exchange closes"
            return [p[0] for p in pts], [p[1] for p in pts], prov, f"computed · six FX legs, ICE weights · {how} · {len(pts)} days"
        via = ""
        try:
            t, v, p = await quotes.history(hub, replace(ins, proxy=""), days)
        except SourceError:
            nxt = hub.book.get(ins.proxy) if ins.proxy else None
            if nxt is None:
                raise
            t, v, p = await quotes.history(hub, nxt, days)
            via = f"proxy {nxt.ticker} · "
        note = " · FRED carries 10 years" if p.source.startswith("fred:SP500") else ""
        return list(t), list(v), p, f"{via}{p.source} · {p.delay if p.source.startswith(('fred', 'treasury', 'ecb')) else 'daily closes'}{note}"

    async def load(self) -> None:
        self.title = "correlation with BTC"
        hub, now = self.hub, time.time()
        bt, bv, bprov = await btc_history(hub, 400)
        if bt and bt[-1] + DAY > now:                       # today's candle is still forming
            bt, bv = bt[:-1], bv[:-1]
        provs, rows = [bprov], []
        for i, (ticker, label, diff) in enumerate(CORR_ASSETS):
            row = CorrRow(ticker, label, charts.SERIES[i % len(charts.SERIES)])
            try:
                t, v, prov, row.source = await self._history(ticker, 400)
                provs.append(prov)
                rt, rx, ry = paired_returns(bt, bv, t, v, diff)
                row.times, row.points = rt, len(rt)
                row.rolling = {n: rolling_corr(rx, ry, n) for n in self.WINDOWS}
                row.now = {n: (r[-1] if r else None) for n, r in row.rolling.items()}
            except SourceError as e:
                row.error = str(e)
            rows.append(row)
        if all(r.error for r in rows):
            raise SourceError(rows[0].error)
        self.rows, self.stamp = rows, self.stamp + 1
        ends = [r.times[-1] for r in rows if r.times]
        self.day = max(ends) if ends else 0.0
        self.provs = [replace(p, delay=DAILY) for p in provs]
        self.title = f"correlation with BTC · to {ui.day(self.day)}"

    def cache_key(self) -> tuple:
        return ()

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            self.win = max(0, min(self.win + (1 if ch == "]" else -1), len(self.WINDOWS) - 1))
            return True
        return False

    def command(self) -> str:
        return f"CORR {self.WINDOWS[self.win]}"

    def hint(self) -> str:
        return "[ ] window 30 90 180 · enter compares"

    def enter(self) -> str | None:
        return f"GP BTC {self.rows[self.cur].ticker}" if self.cur < len(self.rows) else None

    def menu(self) -> list[tuple[str, str]]:
        return [("GP", "GP BTC XAU SPX"), ("CYC", "CYC"), ("ONCH", "ONCH")]

    def _chart(self, w: int, ph: int, n: int) -> list[Text]:
        plot = charts.Plot(w, ph)
        plot.y_range = (-1.05, 1.05)
        plot.fmt = lambda v: f"{v:+.1f}" if abs(v) > 1e-9 else "0"
        for r in self.rows:
            if r.times and r.rolling:
                pts = [(t, c) for t, c in zip(r.times, r.rolling[n], strict=True) if c is not None]
                if len(pts) > 1:
                    xs, ys = charts.resample([p[0] for p in pts], [p[1] for p in pts], max(w * 2, 40))
                    plot.line(xs, ys, r.colour, label=r.label)
        if not plot.lines:
            return [ui.note(f"No asset has more than {n} shared days yet, so there is no rolling line to draw.")]
        plot.hline(0.0, FAINT, "0", glyph="┄")
        return plot.render().split("\n")

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.rows:
            return []
        n = self.WINDOWS[self.win]
        self.n_rows = len(self.rows)
        self.cur = min(self.cur, self.n_rows - 1)
        day = ui.day(self.day)
        out = [ui.section("CORRELATION WITH BTC", w, pick(w - 22, f"daily · {n}-day window of shared trading days · to {day} UTC",
                                                          f"daily · {n}-day window · to {day}", f"daily · {n} d · {short_day(self.day)}",
                                                          f"{n} d"))]
        half = 6 if w < 60 else 10 if w < 110 else 16
        cols = [ui.Col("asset", "left"), ui.Col(f"{n} d"), ui.Col("-1" + " " * (half - 2) + "0" + " " * (half - 2) + "+1", "left")]
        others = [x for x in self.WINDOWS if x != n] if w >= 72 else []
        cols += [ui.Col(f"{x} d", style=DIM) for x in others]
        if w >= 72:
            cols.append(ui.Col("obs"))
        if w >= 120:
            cols.append(ui.Col("source", "left", style=FAINT, flex=True))
        body = []
        for r in self.rows:
            if r.error or not r.now:
                body.append([ui.t(("━ ", charts.snap(r.colour)), (r.label, DIM)), Text("–", style=FAINT), diverging(None, half),
                             *[""] * (len(others) + (1 if w >= 72 else 0)), *([Text(r.error, style=RED)] if w >= 120 else [])])
                continue
            v = r.now.get(n)
            name = ui.t(("━ ", charts.snap(r.colour)), (r.label if w >= 60 else r.ticker, DIM))
            cells: list[ui.Cell] = [name, ui.signed(v, 2), diverging(v, half)]
            cells += [ui.signed(r.now.get(x), 2) for x in others]
            if w >= 72:
                cells.append(Text(str(r.points), style=FAINT))
            if w >= 120:
                cells.append(r.source)
            body.append(cells)
        out += ui.table(cols, body, w, cursor=self.cur)
        self.follow(3 + self.cur, h)
        notes: list[Text] = []
        short = [r.label for r in self.rows if r.now and r.now.get(n) is None and not r.error]
        if short:
            notes.append(ui.note(f"{', '.join(short)}: fewer than {n} shared days of history, so no {n}-day value."))
        if w < 120:
            notes += [ui.note(f"{r.label}: {r.error or r.source}"[:w], RED if r.error else FAINT) for r in self.rows]
        notes.append(ui.note(pick(w, "Bitcoin's daily log returns against each asset's (first differences for the yield), on shared dates.",
                                  "Daily log returns on shared dates. The yield uses first differences.", "Daily log returns, shared dates.")))
        ph = h - len(out) - 1 - len(notes)
        if ph < 5:
            notes, ph = notes[-1:], h - len(out) - 2
        if ph >= 4:
            out.append(ui.section(f"ROLLING {n} d", w, pick(w - 16, "one point a shared trading day")))
            out += self.pic((w, ph, self.stamp, n), lambda: self._chart(w, ph, n))
        return fit(out + notes, w)

    def export(self):
        rows = []
        for r in self.rows:
            for i, t in enumerate(r.times or []):
                rows.append([r.ticker, iso_day(t), *[(r.rolling or {}).get(n, [None] * (i + 1))[i] for n in self.WINDOWS]])
        return ["asset", "date", *[f"corr_{n}d" for n in self.WINDOWS]], rows


# ── FLDS ────────────────────────────────────────────────────

class FldsPane(OnchainPane):
    code, every, selectable = "FLDS", 0, True
    LIMIT, BATCH = 40, 8

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.words = " ".join(self.args).strip().lower()
        self.names: list[str] | None = None
        self.info: dict[str, dict[str, Any]] = {}
        self.latest: dict[str, float | None] = {}
        self.count = 0
        self.curated: list[tuple[str, str, str]] = []       # series, label, unit

    def _remember(self, pairs: Sequence[tuple[str, str]]) -> None:
        """Into the GO bar's autocomplete: newest last, one entry a name, 500 at most."""
        seen = dict(self.hub.series_seen)
        for name, text in pairs:
            if text:
                seen.pop(name, None)                        # a described name moves to the fresh end
            if text or name not in seen:
                seen[name] = text
        self.hub.series_seen[:] = list(seen.items())[-500:]

    def _batch(self) -> list[str]:
        """The next names to describe: the rows at and under the cursor that nobody has asked about yet."""
        names = self.names or []
        return [n for n in names[max(self.cur - 2, 0):self.cur + 14] if n not in self.info][:self.BATCH]

    async def load(self) -> None:
        bv = self.bitview()
        if not self.words:
            self.title = "series search"
            metrics = catalogue().get("metrics", {})
            self.curated = [(str(m.get("series")), str(m.get("label", k)), str(m.get("unit", ""))) for k, m in metrics.items()]
            self._remember([(s, label) for s, label, _ in self.curated])
            self.count, prov = await bv.count()
            self.provs = [prov]
            return
        self.title = f"series · {self.words}"
        if self.names is None:
            self.names, prov = await bv.search(self.words, self.LIMIT)
            self.provs = [prov]
            self._remember([(n, "") for n in self.names])
        batch = self._batch()
        for name in batch:                                  # a handful a load, each cached for a day: never one a keystroke
            try:
                self.info[name], _ = await bv.info(name)
            except SourceError as e:
                self.info[name] = {"error": str(e)}
        daily = [n for n in batch if "day1" in self.info[n].get("indexes", [])]
        if daily:
            try:
                for s in await bv.bulk(daily, "day1", -2):
                    self.latest[s.name] = s.settled()
                    self.day = s.times[settled_at(s)]
            except SourceError:
                pass                                        # the value column stays empty; the descriptions still stand
        self._remember([(n, str(self.info[n].get("description", ""))) for n in batch if self.info[n].get("description")])

    def _start(self, w: int, h: int) -> list[Text]:
        out = [ui.section("SERIES SEARCH", w, pick(w - 16, f"{self.count:,} series in Bitview's catalogue today", f"{self.count:,} series"))]
        out += ui.wrap("Type FLDS and a few words: FLDS mvrv, FLDS realized price, FLDS sth supply. Bitview matches the words against its "
                       "series names and answers with the closest. Enter on a row charts it with GP. Names found here complete in the GO bar.",
                       w, DIM)
        out += [Text(""), ui.section("STARTING POINTS", w, pick(w - 18, "the identifiers checked into onchain.toml", "onchain.toml"))]
        cols = [ui.Col("series", "left", style=f"bold {TEXT}"), ui.Col("unit", "left", style=FAINT),
                ui.Col("what it is", "left", style=DIM, flex=True)]
        rows = [[s, unit, label] for s, label, unit in self.curated]
        if w < 60:
            cols, rows = [cols[0], cols[2]], [[r[0], r[2]] for r in rows]
        self.n_rows = len(rows)
        self.follow(len(out) + 2 + self.cur, h)
        return out + ui.table(cols, rows, w, cursor=self.cur)

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.words:
            return fit(self._start(w, h), w) if self.count or self.curated else []
        if self.names is None:
            return []
        names = self.names
        self.n_rows = len(names)
        self.cur = min(self.cur, max(self.n_rows - 1, 0))
        if names and names[self.cur] not in self.info:
            self.want(tuple(self._batch()[:1]))
        out = [ui.section(f"FLDS {self.words}", w, pick(w - len(self.words) - 8, f"{len(names)} series, best match first · values are daily"
                                                        + (f", {ui.day(self.day)}" if self.day else ""), f"{len(names)} series"))]
        if not names:
            return out + [ui.note(f"No series matches {self.words!r}. Try fewer or other words: FLDS realized, FLDS supply profit.", DIM),
                          ui.note("The terminal never guesses a name the catalogue does not carry.")]
        if h >= 14:
            meta = self.info.get(names[self.cur], {})
            text = meta.get("error") or meta.get("description") or "loading the description…"
            about = ui.wrap(f"{names[self.cur]}: {text}", w, RED if meta.get("error") else DIM)
            if len(about) > 3:
                about = about[:3]
                about[2].truncate(w - 1)
                about[2].append("…")
            out += about + [Text("")]
        cols = [ui.Col("series", "left", style=f"bold {TEXT}", width=min(max(len(n) for n in names), max(w // 3, 16))), ui.Col("settled")]
        if w >= 96:
            cols.append(ui.Col("type", "left", style=FAINT, width=10))
        if w >= 110:
            cols.append(ui.Col("indexes", "left", style=FAINT, width=22))
        if w >= 60:
            cols.append(ui.Col("description", "left", style=DIM, flex=True))
        body = []
        texts = [str(m.get("description", "")) for m in self.info.values() if m.get("description")]
        shared = len(commonprefix(texts)) if len(texts) > 1 else 0      # a family of cohorts shares its opening: show what differs
        shared = texts[0].rfind(". ", 0, shared) + 2 if shared >= 40 else 0     # back to the start of the sentence that differs
        for n in names:
            meta = self.info.get(n)
            v = self.latest.get(n)
            ix = list(meta.get("indexes", [])) if meta else []
            row: list[ui.Cell] = [n, Text("·", style=FAINT) if meta is None else charts.num(v) if v is not None else Text("–", style=FAINT)]
            if w >= 96:
                row.append(str(meta.get("type", "")) if meta else "")
            if w >= 110:
                row.append(f"{len(ix)}: " + " ".join(i for i in ("height", "day1", "week1", "month1") if i in ix) if ix else "")
            if w >= 60:
                bad = meta.get("error") if meta else None
                about = str(meta.get("description", "")) if meta else ""
                row.append(Text(str(bad), style=RED) if bad else (about[shared:] if 1 < shared < len(about) else about))
            body.append(row)
        self.follow(len(out) + 2 + self.cur, h)
        return fit(out + ui.table(cols, body, w, cursor=self.cur), w)

    def enter(self) -> str | None:
        if not self.words:
            return f"GP {self.curated[self.cur][0]}" if self.cur < len(self.curated) else None
        return f"GP {self.names[self.cur]}" if self.names and self.cur < len(self.names) else None

    def hint(self) -> str:
        return "j k row · enter charts it"

    def menu(self) -> list[tuple[str, str]]:
        return [("ONCH", "ONCH"), ("CYC", "CYC"), ("WAVE", "WAVE"), ("URPD", "URPD")]

    def cache_key(self) -> tuple:
        return ()

    def export(self):
        if not self.words:
            return ["series", "label", "unit"], [list(c) for c in self.curated]
        return (["series", "type", "indexes", "settled", "description"],
                [[n, self.info.get(n, {}).get("type"), " ".join(self.info.get(n, {}).get("indexes", [])), self.latest.get(n),
                  self.info.get(n, {}).get("description")] for n in self.names or []])


# ── registration ────────────────────────────────────────────

_DAILY = ("Every value is daily. The last point of a Bitview day1 series is today and still moving, so the page reads the last complete "
          "UTC day and names it. ")
_DOWN = ("When Bitview is down the last values stay on screen, dimmed, with their age, and the frame names the error. Point "
         "bitcoin.bitview at your own bitviewd to read from your own node. A series name the server refuses is named on screen; the "
         "terminal never swaps in another name.")
register(Function(
    "FLDS", "Series search", "On-chain", "search Bitview's series, like a field search: name, type, indexes, latest value", FldsPane,
    args="<words>", needs=("series",),
    help="FLDS and a few words searches the names of every series Bitview carries (/api/series/search, 40 results, best match first). "
         "For the rows near the cursor the page then asks /api/series/{name} for the description, the value type and the indexes the "
         "series exists on, eight names at a time, each answer cached for a day. Series with a daily index get their last complete "
         "day's value from one bulk request. Moving the cursor past the described rows loads the next eight on the next tick, never "
         "one request a keystroke. Enter charts the row with GP. Every name found is remembered for the GO bar's autocomplete, up to "
         "500. With no words the page shows how many series the catalogue holds (/api/series/count) and the identifiers checked into "
         "onchain.toml as starting points. It loads once and does not refresh on a timer. " + _DOWN))
register(Function(
    "ONCH", "On-chain dashboard", "On-chain", "the classic cycle metrics, the chain oracle and a compact URPD", OnchPane,
    needs=("series", "urpd", "oracle"),
    help="Realized price (realized_price), MVRV (mvrv), NUPL (nupl), SOPR over 24 hours (sopr_24h), short-term and long-term holder "
         "cost basis (sth_realized_price, lth_realized_price: coins younger and older than 150 days), the Puell multiple "
         "(puell_multiple, subsidy only), the Mayer multiple (price_sma_200d_ratio: price over its 200-day average), supply in profit "
         "(supply_in_profit_share and supply_in_profit) and liveliness (liveliness). The identifiers come from onchain.toml and "
         "arrive in one bulk request. Each metric shows the last complete day, its change over the 30 days before (points for a "
         "share), a 60-day sparkline and a one-word read where a textbook threshold exists: MVRV above 3.5 hot, above 2.4 warm, under "
         "1 under cost; NUPL under 0 capitulation, then hope, optimism from 0.25, belief from 0.5, euphoria above 0.75; SOPR against "
         "1; Mayer above 2.4 hot and under 0.8 cheap; Puell above 4 hot and under 0.5 cheap; supply in profit above 95% hot and under "
         "50% stress; spot above or below each cost basis. The chain oracle is Bitview's live BTC/USD price read from round-dollar "
         "outputs, with its gap to the settled close. The URPD below is the same complete day's distribution for all coins in "
         "$5,000 buckets around spot (lin1000 from /api/urpd/all/{date}, summed locally); spot is the live composite when the quote "
         "board has it and that day's close otherwise, and buckets above it are red. A small pane shows a grid; a tall one a table, "
         "and a wide tall one puts the URPD beside it. j and k pick a metric and Enter charts it with GP. Refreshes every five "
         "minutes; Bitview's daily series are cached for an hour. " + _DAILY + _DOWN))
register(Function(
    "URPD", "Realized price distribution", "On-chain", "share of supply by the price it last moved at, spot marked", UrpdPane,
    args="[cohort] [yyyy-mm-dd] [lin]", needs=("urpd",),
    help="The UTXO realized price distribution: each bar is the share of the cohort's supply that last moved inside that price "
         "bucket. Spot is marked on its bucket; buckets above spot (coins held at a loss) are red. The line above the bars estimates "
         "the supply below and above spot by dividing the spot bucket where spot cuts it, so it is as fine as the buckets are. The "
         "three largest clusters are called out under the bars. The terminal always asks for an aggregation, because the raw reply is "
         "9,000 buckets and 900 KB: log10 in a small pane, log50 in a tall one, lin1000 for linear buckets, then sums neighbours to "
         "fit the rows it has. Coins that last moved under one dollar (or, in linear mode, under the first bucket) are shown apart so "
         "they do not flatten the chart, and so is whatever lies under the lowest bar. c cycles all, sth, lth, the last two halving "
         "epochs, the last two year classes and p2tr, each only if Bitview lists it (/api/urpd). [ and ] step through the days "
         "Bitview holds (/api/urpd/{cohort}/dates). l switches between log and linear buckets. A past day is marked with its own "
         "close; the latest day with the live composite. The latest day is today and still forming, and the page says so. Refreshes "
         "every 30 minutes. " + _DOWN))
register(Function(
    "WAVE", "HODL waves", "On-chain", "supply by coin age over time, as stacked bands", WavePane, args="[60d|400d|4y|all]", needs=("series",),
    help="The share of all unspent supply in each of Bitview's 23 age bands (utxos_<band>_old_supply_dominance, listed in "
         "onchain.toml; they are disjoint and sum to 100%), stacked over time with the youngest coins at the bottom in warm colours "
         "and the oldest on top in cool ones. One bulk request fetches every band. The table or legend gives each band's share on the "
         "last complete day and its change in points across the window. A small pane sums neighbouring bands into eight display "
         "bands (under a day, to a month, to six months, to a year, to two, to five, to ten, older) and says so. [ and ] change the "
         "window: 60 days, 400 days, four years, all of history. Bitview refuses a request of more than 320,000 bytes of values, "
         "and 23 bands over all of history is four times that, so the whole history is read on the weekly index (week1) with "
         "Bitview's date series dating each week, and the page then says weekly and names the last complete week. "
         "Refreshes hourly. " + _DAILY + _DOWN))
register(Function(
    "CYC", "Valuation bands", "On-chain", "price with realized price, holder cost bases and the 200-day average", CycPane,
    args="[400d|4y|all] [lin]", needs=("series",),
    help="The daily close (price_close) with four valuation lines on one chart: realized price (realized_price) in cyan, the "
         "short-term holder cost basis (sth_realized_price) in yellow, the long-term holder cost basis (lth_realized_price) in violet "
         "and the 200-day average (price_sma_200d) in green. One bulk request, identifiers from onchain.toml. The legend gives each "
         "line's value on the last complete day and how far spot stands from it, where spot is the live composite when the quote "
         "board has it (then also drawn as a dotted cyan rule) and the daily close otherwise. Log scale by default; L switches to "
         "linear. [ and ] change the window: 400 days, four years, all. j and k pick a line and Enter charts it with GP. Bitview's "
         "price is its own chain oracle after height 340,000, not an exchange ticker. Refreshes every 30 minutes. " + _DAILY + _DOWN))
register(Function(
    "CORR", "Correlations", "On-chain", "rolling correlation of BTC with stocks, gold, the dollar and yields", CorrPane, args="[30|90|180]",
    needs=("history",),
    help="Rolling Pearson correlation of Bitcoin's daily log returns (Kraken's daily closes, which reach back 720 days) with the "
         "S&P 500 (FRED SP500, which carries 10 years), the "
         "Nasdaq 100 (FRED NASDAQ100), gold, the dollar index and the 10-year Treasury yield (treasury.gov, then FRED DGS10). Gold has "
         "no open spot feed, so PAXG, a gold token, stands in and the page says which venue answered. The dollar index has no open "
         "history, so it is computed day by day from the six FX legs with the ICE weights, over the days all six share; legs that "
         "come from the ECB carry about 90 days, and the page says how many days it has. The yield uses first differences instead of "
         "log returns. Bitcoin trades every day and the others on business days, so series are joined on the other asset's UTC dates: "
         "a Friday to Monday return meets Bitcoin's return over the same three days. Today's unfinished Bitcoin candle is left out. "
         "A window is 30, 90 or 180 shared trading days ([ and ]); the table shows all three, the bar runs from -1 (red) to +1 "
         "(green), and the chart draws the rolling value for the chosen window. Enter compares the row with Bitcoin in GP. All "
         "inputs are daily closes and are labelled with their source. Refreshes hourly. When a source is down its row says so and "
         "the others still show."))
