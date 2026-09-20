"""FCST: a Glimpse market's forecast as a heatmap you can watch from a launchpad. `FCST BTC`, `FCST XAU`, `FCST BTC 1D`.

The same picture as the full heatmap (f), fitted to a pane: price history left of NOW, then the next two days of
hourly closes (a month of daily ones) in the columns that are left, each cell one shade block  ░ ▒ ▓ █  by the market's
chance the close lands in that cell, with the median traced through it and spot marked on the price axis. Nothing here
trades: enter (or f) opens the full heatmap on this series, where the bet slip lives.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.text import Text

from .. import charts, fmt
from .. import pricing as P
from ..api import Candle, resample
from ..data.core import LIVE, Provenance, SourceError
from ..heatmap import ANSI256, BAND, GLYPHS, TRUECOLOR, candle_part, level
from ..term import ui
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, ORANGE, TEXT

GUTTER = 9                      # price labels on the right
HISTORY_SHARE = 0.3             # of the chart's width, left of NOW
LIMIT = {True: 168, False: 90}  # closes fetched when the pane loads its own: a week hourly, three months daily
PAIRS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XAU": "PAXG-USD"}
NAMES = {"BTC": "Bitcoin", "ETH": "Ether", "SOL": "Solana", "XAU": "Gold"}
BATCHES_TTL = 600.0
HORIZON = {True: 48, False: 31}  # closes drawn: two days of hourly, a month of daily. The full heatmap (f) has the rest


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _sliver(frac: float, width: int) -> str:
    """`fmt.bar`, except that anything above zero keeps a mark: a tail at long odds is still an outcome on the board."""
    return fmt.bar(frac, width) or ("▏" if frac > 0 else "")


@dataclass
class Column:
    """One character of forecast: one close, or several packed together."""
    start: float                # the first close's end time
    end: float                  # the last close's end time
    cum: list[float]            # cumulative probability over the 500 bins, averaged across the closes
    median: float


class FcstPane(FuncPane):
    code, every, tick = "FCST", 60, 5.0
    wants_candles = True                # the pages that only want the closes (CONS, BOT) skip the price history

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        asset = security.ticker if security else next((a.upper() for a in self.args if a.upper() in PAIRS), "BTC")
        asset = "XAU" if asset == "PAXG" else asset
        daily = any(a.upper() in ("1D", "D", "DAILY") for a in self.args)
        self.asset = asset
        self.series = f"{asset} 1D" if asset == "BTC" and daily else asset
        self.views: list = []                   # RowView, nearest close first
        self.candles: list[Candle] = []
        self.cadence = 3600
        self.shared = False                     # reading the app's own loaded closes
        self.title = f"{NAMES.get(asset, asset)} · Glimpse forecast"

    # loading ────────────────────────────────────────────────

    async def _batch(self, api):
        """The series list, read once for every FCST pane on this hub and kept ten minutes."""
        at, got = getattr(self.hub, "fcst_batches", (0.0, []))
        if not got or time.time() - at > BATCHES_TTL:
            got = await api.batches()
            self.hub.fcst_batches = (time.time(), got)
        return next((b for b in got if b.short == self.series), None)

    async def load(self) -> None:
        from ..api import ApiError
        from ..app import summarise

        api = self.hub.glimpse()
        if api is None:
            raise SourceError("the Glimpse API is not connected")
        try:
            batch = await self._batch(api)
        except ApiError as e:
            raise SourceError(f"Glimpse: {e}") from None
        if batch is None:
            self.views = []
            raise SourceError(f"Glimpse has no {self.series} series open right now")
        self.cadence = 3600 if batch.hourly else 86400
        shared = self.hub.views(self.series)
        if shared:
            self.views, self.shared = shared, True
        else:
            try:
                rows = await api.markets(batch.batch_id, limit=LIMIT[batch.hourly])
            except ApiError as e:
                raise SourceError(f"Glimpse: {e}") from None
            rows = [r for r in rows if r.shares]
            self.views = await asyncio.to_thread(lambda: [summarise(r) for r in rows])
            self.shared = False
        self.every = 30 if self.shared else 60 if batch.hourly else 300
        self.hub.series_views[self.series] = self.views
        provs = [Provenance("glimpse", time.time(), time.time(), LIVE)]
        coinbase = self.hub.sources.get("coinbase") if self.wants_candles else None
        if coinbase and (pair := PAIRS.get(self.asset)):
            try:
                rows, _ = await coinbase.candles(pair, self.cadence)     # history is context: the frame names the forecast
                self.candles = [Candle(int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]))
                                for r in sorted(rows, key=lambda r: r[0])]
            except SourceError:
                pass                                # history is context: the forecast stands without it
        self.hub.watch.add("PAXG" if self.asset == "XAU" else self.asset)
        self.title = self.caption(batch.hourly)
        self.provs = provs

    def caption(self, hourly: bool) -> str:
        return f"{NAMES.get(self.asset, self.asset)} · " + ("next 48 hours" if hourly else "next month")

    def due(self, now: float) -> bool:
        if self.shared and self.hub.views(self.series) is not self.views and not self.fetching:
            return True                             # the app refreshed its closes: pick them up at once
        return super().due(now)

    # numbers ────────────────────────────────────────────────

    def spot(self) -> float:
        # The market settles on Coinbase's price: PAXG for gold, not COMEX futures, which sit a carry above spot.
        q = self.hub.quotes.get("PAXG" if self.asset == "XAU" else self.asset)
        if q:
            return q.price
        return self.candles[-1].c if self.candles else 0.0

    def live(self, now: float) -> list:
        return [v for v in self.views if v.row.end_time_utc > now]

    @staticmethod
    def p_above(v, price: float) -> float:
        """The market's probability that this close lands above `price`."""
        total = v.total or 1.0
        out = 0.0
        for (lo, hi), p in zip(v.bins, v.raw, strict=False):
            if lo >= price:
                out += p
            elif hi > price:
                out += p * (hi - price) / (hi - lo)
        return out / total

    def horizon(self, live: list, now: float):
        """The close nearest a day ahead on an hourly series, a week ahead on a daily one."""
        want = now + (86400 if self.cadence < 86400 else 7 * 86400)
        return min(live, key=lambda v: abs(v.row.end_time_utc - want)) if live else None

    def cache_key(self) -> tuple:
        now = time.time()
        return (id(self.views), len(self.views), len(self.candles), round(self.spot(), 2), int(now // 60), charts.truecolor)

    # drawing ────────────────────────────────────────────────

    def draw(self, w: int, h: int) -> list[Text]:
        now = time.time()
        live = self.live(now)
        if not live:
            return []
        spot, hourly = self.spot(), self.cadence < 86400
        out = [*self._readout(live[0], spot, hourly, w), Text("")]
        rows = h - len(out) - 1
        if rows < 4 or w < GUTTER + 20:
            return out
        out += self._chart(live, spot, now, w, rows, hourly)
        return out

    def _readout(self, v, spot: float, hourly: bool, w: int) -> list[Text]:
        """The price now, and where the market expects the next close: one line, or two in a narrow window."""
        when = datetime.fromtimestamp(v.row.end_time_utc, UTC).strftime("%H:%M" if hourly else "%a %d %b")
        lo, hi = v.band
        now = ui.t(("Now ", FAINT), (fmt.price(spot) if spot else "–", f"bold {TEXT}"), (" PAXG" if self.asset == "XAU" else "", FAINT))
        nxt = ui.t((f"{when} close  ", FAINT), (f"{fmt.price(lo)} – {fmt.price(hi)}", f"bold {ORANGE}"), (" likely", DIM), (" (80%)", FAINT))
        if now.cell_len + 5 + nxt.cell_len <= w:
            return [ui.t(now, "     ", nxt)]
        return [now, ui.fit([nxt], w)]

    def _columns(self, live: list, n_cols: int) -> tuple[list[Column], int, int]:
        """(columns, closes per column, characters per column). Few closes spread out, up to three characters each;
        many pack several closes into one character, averaged."""
        if len(live) <= n_cols:
            per, cw = 1, max(1, min(n_cols // max(len(live), 1), 3))
        else:
            per, cw = math.ceil(len(live) / max(n_cols, 1)), 1
        cols = []
        for i in range(0, len(live), per):
            group = live[i:i + per]
            raw = group[0].raw if len(group) == 1 else [sum(x) / len(group) for x in zip(*(g.raw for g in group), strict=False)]
            total = sum(raw) or 1.0
            cum, run = [0.0], 0.0
            for p in raw:
                run += p / total
                cum.append(run)
            cols.append(Column(group[0].row.end_time_utc, group[-1].row.end_time_utc, cum, sum(g.median for g in group) / len(group)))
        return cols, per, cw

    def _chart(self, live: list, spot: float, now: float, w: int, rows: int, hourly: bool) -> list[Text]:
        pal = TRUECOLOR if charts.truecolor else ANSI256
        chart_w = w - GUTTER
        hist_w = max(int(chart_w * HISTORY_SHARE), 6)
        fc_w = chart_w - hist_w - 1
        live = live[:min(HORIZON[hourly], fc_w)]           # a narrow pane shows fewer closes rather than averaging them
        cols, per, cw = self._columns(live, fc_w)
        span_s = self.cadence * per
        hist = resample(self.candles, span_s) if per > 1 else list(self.candles)
        hist = hist[-(hist_w // cw):]
        bins = live[0].bins
        bin_lo, bin_w = bins[0][0], (bins[0][1] - bins[0][0]) or 1.0

        # the price axis: the 80% bands of most of the closes drawn, and spot, then as much of the history as fits within
        # half that again. A few far closes priced wide (or not traded yet) do not squash the rest.
        lows, highs = sorted(v.band[0] for v in live), sorted(v.band[1] for v in live)
        f_lo = min([lows[len(lows) // 5]] + ([spot] if spot else []))
        f_hi = max([highs[-1 - len(highs) // 5]] + ([spot] if spot else []))
        reach = (f_hi - f_lo) * 0.5
        y_lo = max(min([f_lo] + [k.l for k in hist]), f_lo - reach)
        y_hi = min(max([f_hi] + [k.h for k in hist]), f_hi + reach)
        pad = (y_hi - y_lo) * 0.06 or bin_w * 4
        y_lo, y_hi = y_lo - pad, y_hi + pad
        dy = (y_hi - y_lo) / rows

        def row_of(price: float) -> int:
            return max(0, min(rows - 1, int((y_hi - price) / dy)))

        spot_row = row_of(spot) if spot else -1
        grid: list[list[tuple[str, str]]] = [[(" ", "")] * w for _ in range(rows)]

        # history candles, right-aligned against NOW, one to a column
        x0 = hist_w - len(hist) * cw
        for i, k in enumerate(hist):
            colour = _hex(pal.up if k.c >= k.o else pal.down)
            for r in range(rows):
                top = y_hi - r * dy
                part = candle_part(k, top - dy, top)
                if part:
                    grid[r][x0 + i * cw + cw // 2] = ("┃" if part == "body" else "│", colour)
        for r in range(rows):
            grid[r][hist_w] = ("│", ORANGE)

        # the forecast: probability per bin in each cell, then the median through it
        n = len(bins)
        for c, col in enumerate(cols):
            x = hist_w + 1 + c * cw
            if x + cw > chart_w:
                break
            for r in range(rows):
                top, bottom = y_hi - r * dy, y_hi - (r + 1) * dy
                a = max(0.0, min(float(n), (bottom - bin_lo) / bin_w))
                b = max(0.0, min(float(n), (top - bin_lo) / bin_w))
                if b <= a:
                    continue
                ia, ib = int(a), min(int(b), n - 1)
                mass = (col.cum[ib] - col.cum[ia]) + (col.cum[ib + 1] - col.cum[ib]) * (b - ib) - (col.cum[ia + 1] - col.cum[ia]) * (a - ia)
                lvl = level(mass)                   # the chance the close lands in this cell
                if lvl >= 0:
                    for d in range(cw):
                        grid[r][x + d] = (GLYPHS[lvl], _hex(pal.heat(lvl)))
            for d in range(cw):
                grid[row_of(col.median)][x + d] = ("─", _hex(pal.median))

        # the price axis
        step = max(rows // 5, 2)
        for r in range(rows):
            label = ""
            if r == spot_row:
                label, style = f"◂{fmt.kprice(spot) if spot >= 10_000 else fmt.price(spot)}", f"bold {ORANGE}"
            elif r % step == step // 2 and abs(r - spot_row) > 1:
                mid = y_hi - (r + 0.5) * dy
                label, style = f" {fmt.price(mid)}", FAINT
            if label:
                for j, ch in enumerate(label[:GUTTER]):
                    grid[r][chart_w + j] = (ch, style)
            if r == spot_row:
                for x in range(hist_w + 1, chart_w):
                    if grid[r][x][0] == " ":
                        grid[r][x] = ("┄", "#6b3a0c")

        out = []
        for line in grid:
            t = Text(no_wrap=True, overflow="crop")
            run, style = "", None
            for ch, st in line:
                if st != style and run:
                    t.append(run, style=style or "")
                    run = ""
                run, style = run + ch, st
            if run:
                t.append(run, style=style or "")
            out.append(t)
        out.append(self._axis(cols, hist, hist_w, chart_w, span_s, cw, hourly))
        return out

    def _axis(self, cols: list[Column], hist: list[Candle], hist_w: int, chart_w: int, span_s: int, cw: int, hourly: bool) -> Text:
        """Time under the chart: NOW at the divider, then times at even spacing on either side."""
        line = [" "] * chart_w
        styles = [FAINT] * chart_w

        def put(x: int, label: str, style: str = FAINT) -> None:
            if 0 <= x and x + len(label) <= chart_w and all(c == " " for c in line[max(x - 1, 0):x + len(label) + 1]):
                for j, ch in enumerate(label):
                    line[x + j], styles[x + j] = ch, style

        fmt_of = "%a %H:%M" if hourly and span_s < 86400 else "%d %b"
        put(max(hist_w - 1, 0), "NOW", f"bold {ORANGE}")
        gap = max(14 // cw, 1)
        for c in range(gap, len(cols), gap):
            put(hist_w + 1 + c * cw, datetime.fromtimestamp(cols[c].end, UTC).strftime(fmt_of))
        x0 = hist_w - len(hist) * cw
        for i in range(len(hist) - gap, -1, -gap):
            put(x0 + i * cw, datetime.fromtimestamp(hist[i].t, UTC).strftime(fmt_of))
        out = Text(no_wrap=True, overflow="crop")
        for ch, st in zip(line, styles, strict=True):
            out.append(ch, style=st)
        return out

    # keys ───────────────────────────────────────────────────

    def enter(self) -> str | None:
        return f"HM {self.series}"

    def hint(self) -> str:
        return "f the full forecast · o the odds"

    def menu(self) -> list[tuple[str, str]]:
        return [("FORECAST", f"HM {self.series}"), ("ODDS", f"ODDS {self.series}"), ("NEXT CLOSE", f"DIST {self.asset}")]

    def command(self) -> str:
        return f"FCST {self.asset}" + (" 1D" if self.series == "BTC 1D" else "")

    def export(self):
        rows = [[datetime.fromtimestamp(v.row.end_time_utc, UTC).strftime("%Y-%m-%d %H:%M"), v.median, v.band[0], v.band[1]]
                for v in self.views]
        return ["close_utc", "median", "band80_low", "band80_high"], rows


class DistPane(FcstPane):
    """DIST: the odds on every outcome of the next close, range by range, as the odds screen reads them."""
    code = "DIST"

    def caption(self, hourly: bool) -> str:
        return f"{NAMES.get(self.asset, self.asset)} · " + ("odds on the next hour" if hourly else "odds on the next close")

    def cache_key(self) -> tuple:
        return (*super().cache_key(), int(time.time()))           # the countdown ticks

    def draw(self, w: int, h: int) -> list[Text]:
        now = time.time()
        live = self.live(now)
        if not live:
            return []
        v, spot, hourly = live[0], self.spot(), self.cadence < 86400
        when = datetime.fromtimestamp(v.row.end_time_utc, UTC).strftime("%H:%M" if hourly else "%a %d %b")
        head = ui.t((f"{when} close", f"bold {TEXT}"), (f"  in {fmt.countdown(v.row.end_time_utc, now)}", DIM))
        def read(height: int):                                    # one outcome per row the window can hold
            got = self._rows(v, spot, max(height, 3))
            if not got:
                return got, None, Text()
            top = max(got, key=lambda r: r[2])
            return got, top, ui.t(("most likely ", FAINT), (f"{fmt.price(top[0])} – {fmt.price(top[1])}", f"bold {ORANGE}"),
                                  (f"  {top[2]:.0%}", f"bold {TEXT}"))

        rows, best, likely = read(h - 2)
        if not rows:
            return []
        one = head.cell_len + 5 + likely.cell_len <= w
        if not one:                                               # the heading took a second line: one outcome fewer
            rows, best, likely = read(h - 3)
        out = [ui.t(head, "     ", likely) if one else head] + ([] if one else [ui.fit([likely], w)]) + [Text("")]
        label_w = max(len(f"{fmt.price(a)} – {fmt.price(b)}") for a, b, _, _ in rows) + 2
        odds_w = 8 if w >= label_w + 34 else 0                    # the odds column is what makes a tail row worth reading
        bar_w = max(w - label_w - 7 - odds_w - 8, 4)
        top = best[2] or 1.0
        lo80, hi80 = v.band
        for a, b, m, o in reversed(rows):
            inside = a < hi80 and b > lo80
            line = Text(no_wrap=True, overflow="crop")
            line.append(f"{fmt.price(a)} – {fmt.price(b)}".rjust(label_w - 2) + "  ", style=TEXT if inside else FAINT)
            line.append(_sliver(m / top, bar_w).ljust(bar_w), style=ORANGE if inside else BAND)
            line.append(f"{m:>6.1%}", style=f"bold {TEXT}" if (a, b) == best[:2] else DIM)
            if odds_w:
                line.append(f"{fmt.odds(o):>{odds_w}}", style=ORANGE if inside else FAINT)
            if spot and a <= spot < b:
                line.append("  ◂ now", style=f"bold {ORANGE}")
            out.append(line)
        return out

    def _rows(self, v, spot: float, n: int) -> list[tuple[float, float, float, float]]:
        """(low, high, probability, odds) for `n` round price ranges, lowest first.

        The window is as fine as the market's own bins and as tall as the pane, so every row the pane can hold is
        an outcome that can be bet on: the shoulders and the tails are on screen beside the middle, not cropped to
        the 80% band. The step only coarsens — in round multiples of a bin — when the band would not otherwise fit.
        It reaches no further than twice the 80% band either side of the middle, and than anything anyone has
        traded, so a market whose bins are coarser than its own distribution does not fill the pane with identical
        rows of untraded seed.
        """
        bins = v.bins
        bin_lo, bin_w = bins[0][0], (bins[0][1] - bins[0][0]) or 1.0
        lo, hi = v.band
        a, b = min(lo, spot or lo), max(hi, spot or hi)
        step = bin_w
        for m in (1, 2, 5, 10, 20, 50, 100, 200):        # the finest round step whose n rows still hold the band
            step = m * bin_w
            if n * step >= (b - a) * 1.4:
                break
        mid = (a + b) / 2                                # the window is centred on the band and the price now
        reach = max(hi - lo, bin_w)
        traded = v.extent if v.extent[1] > v.extent[0] else (a, b)
        far = max(b - mid, mid - a, 2 * reach, traded[1] - mid, mid - traded[0])   # half the window, at most
        n = min(n, max(int(math.ceil(2 * far / step)), 3))
        total = v.total or 1.0
        cum, run = [0.0], 0.0
        for p in v.raw:
            run += p / total
            cum.append(run)

        def mass_below(price: float) -> float:
            x = max(0.0, min(float(len(bins)), (price - bin_lo) / bin_w))
            i = min(int(x), len(bins) - 1)
            return cum[i] + (cum[i + 1] - cum[i]) * (x - i)

        ladder_lo, ladder_hi = bins[0][0], bins[-1][1]
        start = math.floor((mid - n * step / 2) / step) * step
        if start + n * step > ladder_hi:                            # slide the window back inside the ladder
            start = math.floor((ladder_hi - n * step) / step) * step
        start = max(start, math.ceil(ladder_lo / step) * step)
        out, x = [], start
        while len(out) < n and x < ladder_hi:
            mass = mass_below(x + step) - mass_below(x)
            price = mass * total * 100                              # sats to buy the range, before fees
            out.append((x, x + step, mass, P.PAYOUT_SATS * (1 - P.FEE) / (price * (1 + P.FEE)) if price > 0 else 0.0))
            x += step
        return out

    def enter(self) -> str | None:
        return f"ODDS {self.series}"

    def hint(self) -> str:
        return "o or enter to bet on these odds · f the forecast"

    def menu(self) -> list[tuple[str, str]]:
        return [("ODDS", f"ODDS {self.series}"), ("FORECAST", f"HM {self.series}"), ("BOTS", f"CONS {self.asset}")]

    def command(self) -> str:
        return f"DIST {self.asset}" + (" 1D" if self.series == "BTC 1D" else "")


register(Function(
    "DIST", "Odds", "Glimpse", "the odds on every outcome of the next close: Bitcoin's next hour, gold's next day", DistPane,
    takes=("CRYPTO", "CMDTY"), optional=True, args="[BTC|XAU|ETH|SOL] [1D]", needs=("glimpse",),
    help="The Glimpse market's probability for each price range at the next close, read the way the odds screen reads it. Each row "
         "is a round range of prices with a bar, the chance the close lands in it and what a contract on it pays, from the market's "
         "own prices (the seed that keeps every bin tradeable included, as on the odds screen). The window holds as many outcomes as "
         "the pane is tall, as fine as the market's own bins, centred on the 80% range and the price now, so the shoulders and the "
         "tails are on screen beside the middle; it reaches no further than twice the 80% range either side, or than whatever "
         "anyone has traded. Rows "
         "inside the market's 80% range are bright. The top line says when "
         "the close settles, how long is left, and the most likely range. The marker shows where the price is now. Enter opens the "
         "odds screen on this series, where a range can be selected and bet on after a confirmation. f opens the full forecast. DIST BTC is "
         "the next hour of Bitcoin, DIST XAU the next daily close of gold. Data: the public Glimpse API, as FCST reads it."))


register(Function(
    "FCST", "Glimpse forecast", "Glimpse", "a Glimpse market's forecast as a heatmap: Bitcoin, gold, any open series", FcstPane,
    takes=("CRYPTO", "CMDTY"), optional=True, args="[BTC|XAU|ETH|SOL] [1D]", needs=("glimpse",),
    help="The Glimpse prediction market's own forecast for one series, drawn the way the full heatmap draws it. Left of NOW is price "
         "history (Coinbase candles; gold is PAXG, a token backed by gold, labelled as a proxy). Right of NOW are the next two days of "
         "closes on an hourly series, the next month on a daily one (f shows every close): each character is a price cell of one close, "
         "or of several closes averaged when the pane is narrow, and how solid it is drawn is the market's chance the close "
         "lands in that cell:  ░  from 0.6%,  ▒  from 1.5%,  ▓  from 3.9%,  █  above 10%. The cyan trace is each close's median. "
         "The dotted orange rule and the marker on the price axis are spot. The top line reads the price now and the range the market "
         "puts 80% of the next close's probability in. FCST BTC is hourly Bitcoin, FCST BTC "
         "1D daily Bitcoin, FCST XAU daily gold. Prices come from the public Glimpse API: when the app already has the series loaded it is "
         "shared, otherwise the pane reads it every minute (every five on a daily series). Enter, or f, opens the full heatmap on this "
         "series, where v draws a box and tab opens the bet slip. Trading needs an API key (L)."))
