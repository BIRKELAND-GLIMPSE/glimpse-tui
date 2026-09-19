"""GP: chart anything. One instrument, several instruments rebased to compare, a Bitview series, or a mix on two axes.

`GP BTC` on the hourly view is the terminal's signature chart: price history up to NOW in braille, then the
Glimpse market's own forecast, its median in cyan through an 80% band, on the same axis and the same time scale.
"""
from __future__ import annotations

import time

from rich.text import Text

from .. import charts
from ..data import quotes
from ..data.core import LIVE, Provenance, SourceError
from ..term import ui
from ..term.instruments import Instrument
from ..term.panes import FuncPane
from ..term.registry import CLASSES, Function, register
from ..theme import DIM, FAINT, ORANGE, TEXT

VIEWS = ("1H", "1D", "1Y", "MAX")
DAYS = {"1D": 120, "1Y": 400, "MAX": 4000}
HOURS_BACK, HOURS_AHEAD = 60, 30


class Line:
    def __init__(self, ins: Instrument) -> None:
        self.ins = ins
        self.xs: list[float] = []
        self.ys: list[float | None] = []
        self.candles: list[charts.Candle] = []
        self.prov: Provenance | None = None
        self.error = ""
        self.unit = ""

    @property
    def last(self) -> float | None:
        return next((y for y in reversed(self.ys) if y is not None), None)


class GpPane(FuncPane):
    code, every, tick = "GP", 120, 5.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        words = [a.upper() for a in self.args]
        self.view = next((v for v in VIEWS if v in words), "")
        self.logy = "LOG" in words
        self.candle = "CANDLE" in words or "CANDLES" in words
        self.lines: list[Line] = []

    def _securities(self) -> list[Instrument]:
        """The securities the GO bar resolved, plus any leftover word read as a Bitview series name (`GP MVRV`)."""
        out = list(self.securities)
        for a in self.args:
            if a.upper() in VIEWS or a.upper() in ("LOG", "CANDLE", "CANDLES") or a.upper() in CLASSES:
                continue
            name = a.lower()
            if all(s.ticker.lower() != name for s in out):
                out.append(Instrument(name, name, "SERIES", quote="", sources=(f"bitview:{name}",)))
        return out[:6]

    async def _hourly(self, ln: Line) -> None:
        ins = ln.ins
        if pair := ins.source("coinbase"):
            rows, ln.prov = await self.hub.sources["coinbase"].candles(pair, 3600)
            rows = sorted(rows, key=lambda r: r[0])[-HOURS_BACK:]
            ln.candles = [charts.Candle(float(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4])) for r in rows]
        elif pair := ins.source("kraken"):
            rows, ln.prov = await self.hub.sources["kraken"].ohlc(pair, 60)
            ln.candles = [charts.Candle(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows[-HOURS_BACK:]]
        else:
            raise SourceError(f"{ins.ticker} has no hourly source. It is quoted once a day.")
        ln.xs, ln.ys = [c.t + 3600 for c in ln.candles], [c.c for c in ln.candles]

    async def _one(self, ln: Line, view: str) -> None:
        ins, hub = ln.ins, self.hub
        try:
            if ins.cls == "SERIES":
                bv = hub.sources.get("bitview")
                if bv is None:
                    raise SourceError("Bitview is not configured")
                name = ins.source("bitview") or ins.ticker.lower()
                info, _ = await bv.info(name)
                ln.unit = str(info.get("type", ""))
                start = {"1H": -120, "1D": -120, "1Y": -400, "MAX": 0}[view]
                s = await bv.series(name, "day1", start=start)
                ln.xs, ln.ys, ln.prov = s.times, s.values, s.prov
            elif view == "1H":
                await self._hourly(ln)
            else:
                ln.xs, ys, ln.prov = await quotes.history(hub, ins, DAYS[view])
                ln.ys = list(ys)
            ln.error = ""
        except SourceError as e:
            ln.error = str(e)

    async def load(self) -> None:
        secs = self._securities()
        if not secs:
            raise SourceError("GP needs something to chart: GP BTC · GP BTC XAU SPX · GP mvrv · GP realized_price BTC")
        if not self.view:
            hourly_ok = len(secs) == 1 and secs[0].cls == "CRYPTO"
            self.view = "1H" if hourly_ok else "1Y"
        if self.view == "1H" and any(not (s.source("coinbase") or s.source("kraken")) for s in secs):
            self.view = "1D"
        lines = [Line(s) for s in secs]
        for ln in lines:
            await self._one(ln, self.view)
            self.hub.watch.add(ln.ins.ticker) if ln.ins.cls != "SERIES" else None
        self.lines = lines
        self.title = " · ".join(s.ticker for s in secs) + f" · {self.view}"
        self.provs = [ln.prov for ln in lines if ln.prov]
        if all(ln.error for ln in lines):
            raise SourceError(lines[0].error)

    def cache_key(self) -> tuple:
        q = self.hub.quotes.get(self.lines[0].ins.ticker) if self.lines else None
        fc = self.hub.forecast() if self._forecasting() else []
        return (self.view, self.logy, self.candle, round(q.price, 0) if q else 0, fc[0][1] if fc else 0, len(fc))

    def _forecasting(self) -> bool:
        return self.view == "1H" and len(self.lines) == 1 and self.lines[0].ins.ticker == "BTC"

    def draw(self, w: int, h: int) -> list[Text]:
        lines = [ln for ln in self.lines if ln.xs and not ln.error]
        if not lines:
            return [ui.note(ln.error) for ln in self.lines if ln.error]
        now, out = time.time(), []
        single = len(lines) == 1
        two_axes = len(lines) == 2 and (lines[0].ins.cls == "SERIES") != (lines[1].ins.cls == "SERIES") or (
            len(lines) == 2 and all(ln.ins.cls == "SERIES" for ln in lines))
        rebased = not single and not two_axes
        plot = charts.Plot(w, max(h - 1 - sum(1 for ln in self.lines if ln.error), 4), log=self.logy and not rebased)
        parts: list[Text] = []
        for i, ln in enumerate(lines):
            colour = charts.SERIES[i % len(charts.SERIES)]
            xs, ys = charts.resample(ln.xs, ln.ys, max(w * 2, 40))
            q = self.hub.quotes.get(ln.ins.ticker)
            if q and ln.ins.cls != "SERIES" and q.prov.delay == LIVE and xs and now > xs[-1]:
                xs, ys = xs + [now], ys + [q.price]                         # the line ends at the live price
            if rebased:
                base = next((y for y in ys if y), None) or 1.0
                ys = [y / base * 100 if y is not None else None for y in ys]
            if single and self.candle and ln.candles:
                plot.candle(ln.candles)
            else:
                plot.line(xs, ys, colour, axis=1 if two_axes and i == 1 else 0, label=ln.ins.ticker)
            last = q.price if q and ln.ins.cls != "SERIES" else ln.last
            first = next((y for y in ln.ys if y), None)
            part = ui.t(("━ ", charts.snap(colour)), (f"{ln.ins.ticker} ", f"bold {TEXT}"),
                        (f"{charts.num(last) if last is not None else '–'} ", f"bold {charts.snap(colour)}"),
                        ui.signed_pct(last / first - 1) if first and last else "", "   ")
            parts.append(part)
        if rebased:
            parts.append(Text("rebased to 100", style=FAINT))
        elif two_axes:
            parts.append(Text(f"{lines[1].ins.ticker} on the left axis", style=FAINT))
        if single and lines[0].ins.cls != "SERIES":
            last = lines[0].last
            if (q := self.hub.quotes.get(lines[0].ins.ticker)) and q.prov.delay == LIVE:
                last = q.price
            if last:
                plot.hline(last, ORANGE, f"{last:,.0f}" if last >= 1000 else charts.num(last), glyph="┄")
        if self._forecasting():
            fc = [f for f in self.hub.forecast() if now < f[0] <= now + HOURS_AHEAD * 3600]
            start = lines[0].xs[-1] - (HOURS_BACK - 1) * 3600 if len(lines[0].xs) >= HOURS_BACK else lines[0].xs[0]
            plot.x_range = (start, (fc[-1][0] if fc else now) + 1800)
            plot.vline(now, ORANGE, "NOW")
            for (end, _med, lo, hi), nxt in zip(fc, [*fc[1:], None], strict=False):
                plot.band(end - 1800, (nxt[0] - 1800) if nxt else end + 1800, lo, hi, "#8a4a10", "░")
            if fc:
                plot.line([now] + [f[0] for f in fc], [fc[0][1]] + [f[1] for f in fc], charts.CYAN, label="Glimpse median")
                parts.append(ui.t(("to NOW, then Glimpse's median ", FAINT), ("━", charts.CYAN), (" and 80% band ", FAINT), ("░", "#8a4a10")))
            else:
                parts.append(Text("hourly to NOW · the Glimpse forecast joins when its closes load", style=FAINT))
        out.append(ui.fit(parts, w))
        out += plot.render().split("\n")
        for ln in self.lines:
            if ln.error:
                out.append(ui.note(f"{ln.ins.ticker}: {ln.error}", DIM))
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            self.view = VIEWS[(VIEWS.index(self.view) + (1 if ch == "]" else -1)) % len(VIEWS)] if self.view in VIEWS else "1Y"
            self.loaded_at = 0.0                                            # a new window is a new load
        elif ch == "L":
            self.logy = not self.logy
        elif ch == "C":
            self.candle = not self.candle
        else:
            return False
        return True

    def hint(self) -> str:
        return "[ ] window · L log · C candles"

    def menu(self) -> list[tuple[str, str]]:
        return [("DES", f"{self.security.ticker} DES")] if self.security and self.security.cls in ("EQUITY", "ETF") else []

    def command(self) -> str:
        return " ".join(["GP", *(s.ticker for s in self._securities())])

    def export(self):
        lines = [ln for ln in self.lines if ln.xs]
        if not lines:
            return None
        first = lines[0]
        from datetime import UTC, datetime
        rows = [[datetime.fromtimestamp(x, UTC).strftime("%Y-%m-%d %H:%M"), y] for x, y in zip(first.xs, first.ys, strict=False)]
        return ["time_utc", first.ins.ticker], rows


register(Function(
    "GP", "Graph", "On-chain", "chart any instrument or Bitview series; several on one chart", GpPane, takes=CLASSES, many=True,
    args="[1H|1D|1Y|MAX] [LOG] [CANDLES]", needs=("history", "series", "quote"), default_for=("CRYPTO", "CURNCY", "CMDTY", "INDEX", "GOVT"),
    help="One instrument draws as a braille line (C switches to candles where the source has them) with the last price marked on "
         "the axis. Several instruments are rebased to 100 at the left edge so they compare. A Bitview series is charted by its "
         "exact name (find one with FLDS); a series with an instrument, or two series, use two axes, the second on the left. "
         "[ and ] change the window: 1H (hourly, crypto only), 1D, 1Y, MAX. L toggles a log axis. "
         "GP BTC on the hourly window continues past NOW into the Glimpse market's own forecast: the cyan line is each close's "
         "median and the shaded band is where the market puts 80% of the probability, from the closes the terminal has loaded. "
         "History comes from the first of the instrument's sources that has it: Coinbase or Kraken candles, FRED, the ECB or "
         "the Treasury, and the frame names it with its delay. A live last price extends the line to now. Reloads every two "
         "minutes. When a source is down the last chart stays and the frame says stale."))
