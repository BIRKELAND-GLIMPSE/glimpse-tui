"""PL: the power law. A log-log chart of a price against Bitcoin's age, with the regression that makes it a line.

Bitcoin's dollar price has spent its whole life close to a straight line on log-log axes — price against days
since the genesis block — which is what a power law is: `price = 10^a · days^n`. `PL BTC` draws the history, the
least-squares fit through it, and a channel one and two standard deviations of the residual either side, so the
question "expensive or cheap against its own history" has a number on it. `PL XAUBTC` does the same for gold
priced in Bitcoin, which slopes the other way: the same law seen from the other side of the trade.

Nothing here is a forecast the terminal will trade. A fit is a description of the past with its own error bars,
and the pane says how big those are: the exponent, R², the residual spread, and where the price sits in it now.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.text import Text

from .. import charts, fmt
from ..data import quotes
from ..data.core import Provenance, SourceError
from ..term import instruments, ui
from ..term.instruments import Instrument
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT

YELLOW = charts.YELLOW          # the rule at the price now: not the fit's cyan, not the price's orange

GENESIS = 1231006505.0          # the genesis block, 3 January 2009 18:15:05 UTC: day 0 of the only clock Bitcoin has
DAY = 86400.0
DAYS = 9000                     # ask for every daily close there is; Bitview's `price_close` starts in 2009
MIN_POINTS = 200                # a fit on less than most of a year says nothing about a decade
# …and neither does a long run of closes over a short stretch of the x axis. The x axis is log10(age), so what
# matters is how far along it the closes reach: 0.15 is a factor of 1.4 in age, about five years at Bitcoin's
# age now and two at half of it. Under that the slope is an extrapolation dressed as a measurement.
MIN_DECADES = 0.15
FIT_COLOUR = charts.CYAN
INNER, OUTER = "#6a6a6a", "#454545"     # ±1σ and ±2σ: the far pair is dimmer, and drops out of a short pane
SHORT_CHART = 14                        # rows below which four channel lines crowd the price line off the picture
DEFAULT = "BTC"
# Where the fit starts. Bitcoin's first two years are a handful of trades at fractions of a cent and they drag the
# exponent about, so the window is a choice the page makes visible rather than one it hides.
WINDOWS = (("all", 0), ("2011", 2011), ("2013", 2013), ("2015", 2015), ("2017", 2017))
# How much of that fit is *drawn*. The fit is the whole history either way; this only frames it. On a log x axis
# the recent years are squeezed into the right-hand quarter, so the default is a close view of where the price is
# now, with the line and its channel running through it. `<` opens it back out to the lot.
ZOOMS = (("1y", 1.0), ("2y", 2.0), ("4y", 4.0), ("10y", 10.0), ("all", 0.0))
DEFAULT_ZOOM = "4y"
AHEAD_YEARS = (1, 2, 4, 10)     # what the fit alone says, projected, with its own spread beside it


def days_of(ts: float) -> float:
    """Days since the genesis block. The x axis of every power law here."""
    return max((ts - GENESIS) / DAY, 1.0)


def at_days(d: float) -> float:
    return GENESIS + d * DAY


def signed(v: float, digits: int = 3) -> str:
    """A number with the house minus sign: −3.633, not -3.633."""
    return f"{fmt.MINUS if v < 0 else ''}{abs(v):.{digits}f}"


def year_start(year: int) -> float:
    return datetime(year, 1, 1, tzinfo=UTC).timestamp()


@dataclass(frozen=True)
class Fit:
    """`price = 10^a · days^n`, least squares on log10 of both. `sd` is the spread of the log10 residuals, which is
    a ratio once it is exponentiated: sd 0.25 means the typical miss is a factor of 1.8 either way."""
    a: float
    n: float
    r2: float
    sd: float
    points: int
    first: float
    last: float

    def at(self, ts: float) -> float:
        return 10 ** (self.a + self.n * math.log10(days_of(ts)))

    def band(self, ts: float, sigmas: float) -> float:
        return self.at(ts) * 10 ** (self.sd * sigmas)

    def sigmas(self, ts: float, price: float) -> float:
        """How many residual standard deviations above the line a price is. Negative is below it."""
        fair = self.at(ts)
        return (math.log10(price) - math.log10(fair)) / self.sd if fair > 0 and price > 0 and self.sd > 0 else 0.0

    def doubles_in(self) -> float | None:
        """Days for the fitted line to double from where it is now, or to halve when the exponent is negative:
        the pace the slope implies at today's age. A flat line never gets there."""
        if self.n == 0:
            return None
        d = days_of(self.last)
        return abs(d * (2 ** (1 / abs(self.n)) - 1)) if self.n > 0 else d * (2 ** (-1 / self.n) - 1)


def fit(ts: list[float], ps: list[float], since: float = 0.0) -> Fit | None:
    """Least squares of log10(price) on log10(days since genesis), over the points at or after `since`."""
    pts = [(days_of(t), p) for t, p in zip(ts, ps, strict=False) if p and p > 0 and t >= since and t > GENESIS]
    if len(pts) < MIN_POINTS:
        return None
    xs = [math.log10(d) for d, _ in pts]
    ys = [math.log10(p) for _, p in pts]
    if xs[-1] - xs[0] < MIN_DECADES:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    inter = my - slope * mx
    res = [y - (inter + slope * x) for x, y in zip(xs, ys, strict=True)]
    sst = sum((y - my) ** 2 for y in ys)
    ssr = sum(r * r for r in res)
    sd = math.sqrt(ssr / (n - 2)) if n > 2 else 0.0
    return Fit(inter, slope, 1 - ssr / sst if sst > 0 else 0.0, sd, n, at_days(pts[0][0]), at_days(pts[-1][0]))


def _fit_to(parts: list[Text], w: int, gap: str = "   ") -> Text:
    """As many parts as the width holds, in order, dropped from the end. A pane forty columns wide gets the
    exponent and stops; nothing here wraps and nothing is cut mid-number."""
    out = Text(no_wrap=True, overflow="crop")
    for part in parts:
        if out.cell_len and out.cell_len + len(gap) + part.cell_len > w:
            break
        if out.cell_len:
            out.append(gap)
        out.append_text(part)
    return out


def year_marks(t_lo: float, t_hi: float, room: int) -> list[tuple[float, str]]:
    """(days since genesis, label) for the years an axis has room to name, thinned out to fit."""
    lo, hi = datetime.fromtimestamp(t_lo, UTC).year, datetime.fromtimestamp(t_hi, UTC).year
    years = [y for y in range(lo, hi + 1) if year_start(y) >= t_lo]
    step = max(1, math.ceil(len(years) / max(room // 5, 1)))
    return [(days_of(year_start(y)), f"'{y % 100:02d}") for y in years[::step]]


class PlPane(FuncPane):
    code, every, tick = "PL", 1800, 30.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        words = [a.upper() for a in self.args]
        self.win = next((name for name, _ in WINDOWS if name.upper() in words), WINDOWS[0][0])   # the start by name,
        # not by position: a series whose history is short offers fewer starts, and the choice must survive that.
        self.zoom = next((name for name, _ in ZOOMS if f"@{name}".upper() in words), DEFAULT_ZOOM)
        self.table = "TABLE" in words or "PROJ" in words
        self.ts: list[float] = []
        self.ps: list[float] = []
        self.hist_prov: Provenance | None = None

    # what it is about ───────────────────────────────────────

    @property
    def ins(self) -> Instrument | None:
        want = self.securities[0].ticker if self.securities else DEFAULT
        return self.hub.book.get(want)

    @property
    def sats(self) -> bool:
        ins = self.ins
        return ins is not None and ins.quote == instruments.BASE

    def money(self, v: float | None) -> str:
        if v is None:
            return "–"
        return fmt.in_btc(v) if self.sats else ui.px(v, 2 if v < 100 else 0)

    def command(self) -> str:
        ins = self.ins
        zoom = f"@{self.zoom}" if self.zoom != DEFAULT_ZOOM else ""
        return " ".join(x for x in ("PL", ins.ticker if ins else DEFAULT, self.window[0], zoom, "TABLE" if self.table else "") if x)

    def cache_key(self) -> tuple:
        return (self.win, self.zoom, self.table, len(self.ts), round(self.spot() or 0, 6))

    def spot(self) -> float | None:
        ins = self.ins
        q = self.hub.quotes.get(ins.ticker) if ins else None
        if q is not None:
            return q.price
        return self.ps[-1] if self.ps else None

    # loading ───────────────────────────────────────────────

    async def load(self) -> None:
        ins = self.ins
        if ins is None:
            raise SourceError(f"{' '.join(self.args) or DEFAULT} is not an instrument this terminal knows")
        self.title = f"{ins.ticker} · {self.window[0]}"
        try:
            await quotes.refresh(self.hub, [ins.ticker, instruments.BASE])
        except SourceError:
            pass
        ts, ps, prov = await quotes.history(self.hub, ins, DAYS)
        pts = [(t, p) for t, p in zip(ts, ps, strict=False) if p and p > 0]
        if len(pts) < MIN_POINTS or fit([p[0] for p in pts], [p[1] for p in pts]) is None:
            span = (pts[-1][0] - pts[0][0]) / (365.25 * DAY) if pts else 0.0
            raise SourceError(f"{ins.ticker}: {len(pts)} daily closes over {span:.1f} years is too little history to fit "
                              f"a power law against Bitcoin's age")
        self.ts, self.ps = [p[0] for p in pts], [float(p[1]) for p in pts]
        self.hist_prov, self.provs = prov, [prov]
        self.title = f"{ins.ticker} · {self.window[0]}"

    def windows(self) -> tuple[tuple[str, int], ...]:
        """The starts worth offering: `all`, plus the years the loaded history actually reaches back past. Offering
        1911 on ten years of data would be four keys that change nothing."""
        if not self.ts:
            return WINDOWS
        return WINDOWS[:1] + tuple(w for w in WINDOWS[1:] if year_start(w[1]) > self.ts[0])

    @property
    def window(self) -> tuple[str, int]:
        wins = self.windows()
        return next((w for w in wins if w[0] == self.win), wins[-1])

    def fitted(self) -> Fit | None:
        since = year_start(self.window[1]) if self.window[1] else 0.0
        return fit(self.ts, self.ps, since)

    @property
    def years(self) -> float:
        """Years of the fit to draw, 0 for all of it."""
        return next((y for name, y in ZOOMS if name == self.zoom), 0.0)

    def drawn(self, f: Fit) -> tuple[list[float], list[float]]:
        """The stretch of history on screen: the fit's own range, cut to the zoom. The fit itself never changes —
        `[` `]` move that — so a close view is the same line, read where the price actually is."""
        cut = max(f.first, self.ts[-1] - self.years * 365.25 * DAY) if self.years else f.first
        pts = [(t, p) for t, p in zip(self.ts, self.ps, strict=True) if t >= cut]
        if len(pts) < 2:
            pts = list(zip(self.ts, self.ps, strict=True))[-2:]
        return [p[0] for p in pts], [p[1] for p in pts]

    # keys ──────────────────────────────────────────────────

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            wins = self.windows()
            at = next((i for i, w in enumerate(wins) if w[0] == self.win), 0)
            self.win = wins[max(0, min(at + (1 if ch == "]" else -1), len(wins) - 1))][0]
            ins = self.ins
            self.title = f"{ins.ticker if ins else DEFAULT} · {self.window[0]}"
            return True
        if ch in ("<", ">"):
            names = [n for n, _ in ZOOMS]
            at = names.index(self.zoom)
            self.zoom = names[max(0, min(at + (1 if ch == "<" else -1), len(names) - 1))]
            return True
        if ch == "T":
            self.table = not self.table
            return True
        return False

    def hint(self) -> str:
        return "< > zoom · [ ] where the fit starts · T projection"

    def menu(self) -> list[tuple[str, str]]:
        ins = self.ins
        other = "XAUBTC" if not self.sats else "BTC"
        return [("GP", f"GP {ins.ticker if ins else DEFAULT} MAX LOG"), ("PL", f"PL {other}"), ("QM", "QM GLOBAL SATS"), ("CYC", "CYC")]

    def export(self):
        f = self.fitted()
        if f is None:
            return None
        rows = [[ui.day(t), round(days_of(t)), p, f.at(t), round(f.sigmas(t, p), 3)] for t, p in zip(self.ts, self.ps, strict=True)]
        return ["date", "days_since_genesis", "price", "power_law_fit", "sigmas_from_fit"], rows

    # drawing ───────────────────────────────────────────────

    def draw(self, w: int, h: int) -> list[Text]:
        ins, f = self.ins, self.fitted()
        if ins is None or not self.ts:
            return []
        unit = "₿" if self.sats else "$"
        if f is None:
            return ui.wrap(f"{ins.ticker}: {len(self.ts)} closes reach back to {ui.day(self.ts[0])}, which is not enough history "
                           f"for the {self.window[0]} window. [ and ] move the start of the fit.", w, FAINT)
        head = f"{ins.ticker} POWER LAW" if w >= 44 else "POWER LAW"
        shown = f"all of it, from {ui.day(f.first)}" if not self.years else f"the last {self.zoom} of it"
        aside = (f"log-log · fit on {f.points:,} closes from {ui.day(f.first)} · showing {shown}" if w >= 92 else
                 f"log-log · {f.points:,} closes · showing {shown}" if w >= 62 else "log-log")
        out = [ui.section(head, w, aside)]
        out += self._numbers(f, w)
        rest = h - len(out) - (len(AHEAD_YEARS) + 2 if self.table else 0) - 1
        if rest >= 6:
            out += self._chart(f, w, min(rest, 30), unit)
        if self.table:
            out += self._projection(f, w)
        if h - len(out) >= 2 and w >= 78:
            drawn = "one and two standard deviations" if (not self.years and h - len(out) >= 2) else "one standard deviation"
            out += [Text("")] + ui.wrap(
                f"price = 10^{signed(f.a)} × days^{signed(f.n)}, least squares on log10 of both, days counted from the genesis "
                f"block. The dotted lines are {drawn} of the log residual either side of the fit, which is "
                f"×{10 ** f.sd:.2f}{f' and ×{10 ** (2 * f.sd):.2f}' if not self.years else ''}. A fit describes the past; it is "
                f"not a forecast, and the terminal never trades one. < and > change how much of it is on screen.", w, FAINT)
        return out

    def _numbers(self, f: Fit, w: int) -> list[Text]:
        """Two lines that shrink rather than wrap: the fit itself, then where the price stands in it."""
        spot, now = self.spot(), min(time.time(), self.ts[-1] + DAY)
        fair = f.at(now)
        sig = f.sigmas(now, spot) if spot else 0.0
        off = (spot / fair - 1) if spot and fair else None
        colour = RED if sig > 1 else GREEN if sig < -1 else TEXT
        sigma = f"{'+' if sig >= 0 else fmt.MINUS}{abs(sig):.2f}σ"
        first = [ui.t(("exponent ", FAINT), (signed(f.n), f"bold {ORANGE}")), ui.t(("R² ", FAINT), (f"{f.r2:.3f}", f"bold {TEXT}")),
                 ui.t(("spread ", FAINT), (f"×{10 ** f.sd:.2f}", DIM)), ui.t(("day ", FAINT), (f"{round(days_of(now)):,}", DIM))]
        second = [ui.t(("now ", FAINT), (self.money(spot), f"bold {TEXT}")),
                  ui.t(("fit ", FAINT), (self.money(fair), f"bold {FIT_COLOUR}"))]
        if off is not None:
            second.append(ui.t(ui.signed_pct(off, 0 if abs(off) >= 0.1 else 1), (f" {sigma}", f"bold {colour}")))
            second.append(ui.t(("above the line" if sig > 0 else "below the line", colour)))
        return [_fit_to(first, w), _fit_to(second, w)]

    def _chart(self, f: Fit, w: int, ch: int, unit: str) -> list[Text]:
        ts, ps = self.drawn(f)
        plot = charts.Plot(w, ch, log=True, times=False, xlog=True, grid=True)
        # One unit down the whole axis. `fmt.in_btc` would switch to whole bitcoin above a coin, so a gold chart
        # that starts at two bitcoin and ends at five million satoshis would label both on one axis; `charts.num`
        # keeps every label in satoshis, and the legend and the header say which unit that is.
        if not self.sats:
            plot.fmt = lambda v: ui.px(v, 2 if v < 100 else 0)
        # The fit and its channel first, so the price line lies on top of them. Only the inner pair is drawn in a
        # short pane or a zoomed one: a shallow braille line repeats its motif once per row it climbs, so five of
        # them across a four-year window read as hatching rather than as a channel.
        edges = [days_of(ts[0]), days_of(ts[-1])]
        wide_channel = ch >= SHORT_CHART and not self.years
        rules = [(1.0, INNER), (-1.0, INNER)] + ([(2.0, OUTER), (-2.0, OUTER)] if wide_channel else [])
        for sigmas, colour in rules:
            plot.line(edges, [f.band(at_days(d), sigmas) for d in edges], colour)
        plot.line(edges, [f.at(at_days(d)) for d in edges], FIT_COLOUR, label=f"fit  n={signed(f.n, 2)}")
        xs, ys = charts.resample([days_of(t) for t in ts], list(ps), max(w * 2, 60))
        plot.line(xs, ys, ORANGE, label=f"{unit} price")
        # Frame the price and the line, not the channel. Two standard deviations of a log residual is a factor of
        # four either way; letting it set the scale squashes four years of price into a couple of rows. The
        # channel still draws, and clips at the edges, which is what a zoom is for.
        floor = min(list(ps) + [f.at(at_days(d)) for d in edges])
        ceil = max(list(ps) + [f.at(at_days(d)) for d in edges])
        if (spot := self.spot()):
            floor, ceil = min(floor, spot), max(ceil, spot)
        pad = (ceil / floor) ** 0.06
        plot.y_range = (floor / pad, ceil * pad)
        if (spot := self.spot()) and ch >= 8:
            plot.hline(spot, YELLOW, f"now {self.money(spot)}", glyph="┄")    # where the present moment actually is
        marks = year_marks(ts[0], ts[-1], max(w - 12, 12))
        # A log x axis crowds the recent years: rule every other one when there are many, or the grid drowns the price.
        plot.x_ticks = marks
        plot.x_grid = [d for i, (d, _) in enumerate(marks) if len(marks) <= 8 or i % 2 == 0]
        lines = list(plot.render().split("\n"))
        legend = plot.legend()
        return lines + ([legend] if legend.cell_len and legend.cell_len <= w else [])

    def _projection(self, f: Fit, w: int) -> list[Text]:
        """What the fitted line alone reads at future dates, with the channel beside it. Not a forecast."""
        now = min(time.time(), self.ts[-1] + DAY)
        band, ratio = w >= 56, w >= 76               # a narrow pane keeps the line itself and drops the channel
        cols = [ui.Col("when", "left", 6), ui.Col("the line")]
        cols += [ui.Col("−1σ"), ui.Col("+1σ")] if band else []
        cols += [ui.Col("× today", "right", style=DIM)] if ratio else []
        rows = []
        here = f.at(now)
        for years in AHEAD_YEARS:
            t = now + years * 365.25 * DAY
            row: list = [f"+{years}y", Text(self.money(f.at(t)), style=f"bold {FIT_COLOUR}")]
            row += [self.money(f.band(t, -1)), self.money(f.band(t, 1))] if band else []
            row += [f"×{f.at(t) / here:,.1f}" if here > 0 else "–"] if ratio else []
            rows.append(row)
        pace = f.doubles_in()
        word = "doubles" if f.n > 0 else "halves"
        out = [Text(""), ui.section("WHAT THE LINE ALONE SAYS" if w >= 40 else "THE LINE AHEAD", w,
                                    f"{word} in {pace / 365.25:,.1f} years at this exponent" if pace and w >= 78 else "")]
        return out + ui.table(cols, rows, w)


register(Function(
    "PL", "Power law", "Markets", "log-log price against Bitcoin's age, with the regression through it", PlPane,
    takes=("CRYPTO", "CMDTY", "INDEX", "EQUITY", "ETF", "CURNCY"), optional=True, args="[ticker] [all|2011|2013|2015|2017] [table]",
    needs=("quote", "series"),
    help="Bitcoin's price has tracked a power law in its own age: a straight line on log-log axes, price against days since the "
         "genesis block (3 January 2009 18:15:05 UTC). This page fits `price = 10^a × days^n` by least squares on log10 of both "
         "and draws it: the price in orange, the fit in cyan, and dotted rules one and two standard deviations of the log "
         "residual either side. The numbers above the chart are the exponent, R², the residual spread as a multiplier, the price "
         "now, what the line reads today, and how far apart they are in percent and in standard deviations. [ and ] move the "
         "start of the fit (all history, 2011, 2013, 2015, 2017); the exponent moves with it, which is the point of showing it. "
         "T adds what the line alone reads one, two, four and ten years out, with the channel beside it.\n\n"
         "PL BTC is the dollar price. PL XAUBTC is gold priced in Bitcoin, a troy ounce in satoshis, which has its own and "
         "negative exponent: the same law from the other side. Any instrument the terminal knows can be asked for, in dollars "
         "(PL SPX) or in satoshis (PL SPXBTC).\n\n"
         "Bitcoin's daily closes come from Bitview's `price_close` for the whole history, which is its own on-chain oracle after "
         "height 340,000 and baked exchange prices before that, not an exchange ticker; an exchange endpoint only serves the last "
         "few hundred days. Other instruments come from their usual daily source, so a fit on them starts where that source does, "
         "and the frame says so. A ratio uses only the days on which both legs have a close.\n\n"
         "A fit is a description of the past with an error bar, not a forecast. The terminal does not trade it, and nothing on "
         "this page is an estimate the Glimpse market has priced. Bitview down: the page keeps the last picture and says so."))
