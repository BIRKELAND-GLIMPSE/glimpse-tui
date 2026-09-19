"""Charts in text (TERMINAL.md 3.4): braille lines at two by four dots a cell, candles, bands, bars, sparklines,
histograms, and half-block pixels for block pictures and QR codes.

Everything here is pure: numbers in, `rich.text.Text` out, no widgets and no clock. Colours are hex strings.
In a 256-colour terminal `snap` moves each colour onto the nearest exact xterm-256 entry before it is drawn, so
the picture is the same one Rich would produce, chosen here where a test can see it.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from rich.text import Text

from .theme import FAINT, GREEN, ORANGE, RED

RGB = tuple[int, int, int]
SPARKS = "▁▂▃▄▅▆▇█"
EIGHTHS = " ▏▎▍▌▋▊▉█"
CYAN, VIOLET, YELLOW, BLUE, PINK, TEAL = "#54bee8", "#b48cff", "#ffd048", "#5f87ff", "#ff6fae", "#76b2b2"
SERIES = (ORANGE, CYAN, VIOLET, GREEN, YELLOW, PINK, BLUE, TEAL, RED)       # line colours, in the order series are added
GRID = "#2e2e2e"

truecolor = True            # the app flips this with `c`; every colour below goes through snap()

_CUBE = (0, 95, 135, 175, 215, 255)


@lru_cache(maxsize=4096)
def _snap256(rgb: RGB) -> RGB:
    """The nearest exact xterm-256 entry: the 6×6×6 cube or the 24-step grey ramp."""
    cube = tuple(min(_CUBE, key=lambda v: abs(v - c)) for c in rgb)
    g = min(range(8, 239, 10), key=lambda v: sum(abs(v - c) for c in rgb))
    dist = lambda p: sum((a - b) ** 2 for a, b in zip(p, rgb, strict=True))   # noqa: E731
    return cube if dist(cube) <= dist((g, g, g)) else (g, g, g)


def rgb_of(colour: str) -> RGB:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def hex_of(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def snap(colour: str | RGB) -> str:
    rgb = rgb_of(colour) if isinstance(colour, str) else colour
    return hex_of(rgb if truecolor else _snap256(rgb))


def ramp(stops: Sequence[tuple[float, RGB]], t: float) -> RGB:
    """Linear interpolation through (position, colour) stops; `t` is clamped to the ends."""
    if t <= stops[0][0]:
        return stops[0][1]
    for (a, ca), (b, cb) in zip(stops, stops[1:], strict=False):
        if t <= b:
            f = (t - a) / (b - a) if b > a else 0.0
            return tuple(round(x + (y - x) * f) for x, y in zip(ca, cb, strict=True))  # type: ignore[return-value]
    return stops[-1][1]


# mempool.space's fee ramp, low to high: deep blue, teal, green, yellow, orange, red, magenta
FEE_STOPS: tuple[tuple[float, RGB], ...] = (
    (0.0, (24, 58, 140)), (0.18, (18, 130, 150)), (0.36, (40, 170, 90)), (0.54, (190, 200, 50)),
    (0.72, (255, 150, 20)), (0.88, (235, 60, 50)), (1.0, (200, 40, 170)))


def fee_colour(rate: float) -> RGB:
    """A fee rate in sat/vB on a log scale from 1 to 500."""
    return ramp(FEE_STOPS, math.log(max(rate, 1.0)) / math.log(500.0))


HEAT_STOPS: tuple[tuple[float, RGB], ...] = ((0.0, (190, 40, 60)), (0.35, (90, 30, 40)), (0.5, (34, 34, 34)),
                                             (0.65, (20, 90, 60)), (1.0, (11, 217, 138)))


def heat_colour(change: float, span: float) -> RGB:
    """Red through neutral to green for a change of -span..+span."""
    return ramp(HEAT_STOPS, 0.5 + max(-1.0, min(1.0, change / span if span else 0.0)) / 2)


# ── small marks ─────────────────────────────────────────────

def spark(values: Sequence[float | None], width: int | None = None) -> str:
    """`▁▂▃▅▇` for a row of a table. Resampled to `width` by taking the last value of each slice."""
    vals = [v for v in values if v is not None and not math.isnan(v)]
    if not vals:
        return ""
    if width and len(vals) > width:
        vals = [vals[min(int((i + 1) * len(vals) / width) - 1, len(vals) - 1)] for i in range(width)]
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return SPARKS[3] * len(vals)
    return "".join(SPARKS[min(int((v - lo) / (hi - lo) * 8), 7)] for v in vals)


def hbar(frac: float, width: int) -> str:
    """A horizontal bar with eighth-block resolution."""
    eighths = round(max(0.0, min(1.0, frac)) * width * 8)
    full, part = divmod(eighths, 8)
    return "█" * full + (EIGHTHS[part] if part else "")


def vbars(values: Sequence[float], height: int, colour: str = ORANGE, colours: Sequence[str] | None = None,
          top: float | None = None) -> list[Text]:
    """A histogram: one column per value, `height` rows tall, eighth-block tops."""
    hi = top if top is not None else max(values, default=0.0)
    rows = [Text(no_wrap=True) for _ in range(height)]
    for i, v in enumerate(values):
        eighths = round(max(0.0, min(1.0, v / hi if hi > 0 else 0.0)) * height * 8)
        style = snap(colours[i] if colours else colour)
        for r in range(height):
            e = max(0, min(8, eighths - (height - 1 - r) * 8))
            rows[r].append(" " if e == 0 else SPARKS[e - 1], style=style)
    return rows


def pixels(grid: Sequence[Sequence[RGB | None]], blank: RGB = (13, 13, 13)) -> list[Text]:
    """Half-block pixels: each character is `▀` with the upper pixel as foreground and the lower as background.
    Runs of identical cells share one style span, so a block picture costs a few dozen spans a row."""
    out: list[Text] = []
    for y in range(0, len(grid), 2):
        up, down = grid[y], grid[y + 1] if y + 1 < len(grid) else [None] * len(grid[y])
        line, run, style = Text(no_wrap=True), 0, ""
        for a, b in zip(up, down, strict=True):
            s = f"{snap(a or blank)} on {snap(b or blank)}"
            if s != style and run:
                line.append("▀" * run, style=style)
                run = 0
            style, run = s, run + 1
        if run:
            line.append("▀" * run, style=style)
        out.append(line)
    return out


def qr(matrix: Sequence[Sequence[bool]], dark: RGB = (13, 13, 13), light: RGB = (244, 244, 244), quiet: int = 2) -> list[Text]:
    """A QR code in half blocks, light modules on a quiet zone so a phone camera reads it off a dark terminal."""
    w = len(matrix[0]) + 2 * quiet if matrix else 0
    blank = [False] * w
    rows = [blank] * quiet + [[False] * quiet + list(r) + [False] * quiet for r in matrix] + [blank] * quiet
    return pixels([[dark if m else light for m in r] for r in rows], blank=light)


# ── axes ────────────────────────────────────────────────────

def nice_step(span: float, target: int) -> float:
    """A round step (1, 2, 2.5, 5 × 10ⁿ) giving about `target` ticks across `span`."""
    if span <= 0 or target <= 0:
        return 1.0
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    return next((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), 10 * mag)


def num(v: float) -> str:
    """A compact axis label: 81,608 · 1.57 · 0.0042 · 3.2T."""
    a = abs(v)
    if a >= 1e13:
        return f"{v / 1e12:,.0f}T"
    if a >= 1e12:
        return f"{v / 1e12:,.2f}T"
    if a >= 1e10:
        return f"{v / 1e9:,.0f}B"
    if a >= 1e9:
        return f"{v / 1e9:,.2f}B"
    if a >= 1e7:
        return f"{v / 1e6:,.0f}M"
    if a >= 1e6:
        return f"{v / 1e6:,.2f}M"
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:,.1f}"
    if a >= 1:
        return f"{v:,.2f}"
    if a == 0:
        return "0"
    return f"{v:.3f}" if a >= 0.01 else f"{v:.2e}"


def time_label(ts: float, span: float) -> str:
    d = datetime.fromtimestamp(ts, UTC)
    if span <= 3 * 86400:
        return d.strftime("%d %b %H:%M")
    if span <= 400 * 86400:
        return d.strftime("%d %b")
    return d.strftime("%b %Y") if span <= 6 * 365 * 86400 else d.strftime("%Y")


@dataclass
class Candle:
    t: float
    o: float
    h: float
    l: float    # noqa: E741
    c: float


@dataclass
class _Line:
    xs: Sequence[float]
    ys: Sequence[float | None]
    colour: str
    axis: int           # 0 the right-hand axis, 1 a second scale labelled on the left
    label: str


class Plot:
    """One chart. Add layers, then `render()`. X is any number (unix seconds draw as dates); Y is linear or log.

    Layers paint in a fixed order whatever order they were added in: grid, bands, candles, lines, rules, markers.
    The right gutter holds the price axis (`┤83,000`) and markers (`◂81,608`); a second axis labels on the left.
    """

    def __init__(self, width: int, height: int, log: bool = False, times: bool = True, gutter: int | None = None,
                 grid: bool = True) -> None:
        self.w, self.h, self.log, self.times, self.grid = max(width, 12), max(height, 3), log, times, grid
        self.gutter = gutter
        self.lines: list[_Line] = []
        self.candles: list[Candle] = []
        self.bands: list[tuple[float, float, float, float, str, str]] = []
        self.hlines: list[tuple[float, str, str, str]] = []
        self.vlines: list[tuple[float, str, str]] = []
        self.x_range: tuple[float, float] | None = None
        self.y_range: tuple[float, float] | None = None
        self.fmt = num

    # layers ─────────────────────────────────────────────────

    def line(self, xs: Sequence[float], ys: Sequence[float | None], colour: str | None = None, axis: int = 0, label: str = "") -> Plot:
        self.lines.append(_Line(xs, ys, colour or SERIES[len(self.lines) % len(SERIES)], axis, label))
        return self

    def candle(self, candles: Sequence[Candle]) -> Plot:
        self.candles = list(candles)
        return self

    def band(self, x0: float, x1: float, lo: float, hi: float, colour: str = ORANGE, glyph: str = "░") -> Plot:
        self.bands.append((x0, x1, lo, hi, colour, glyph))
        return self

    def hline(self, y: float, colour: str = CYAN, label: str = "", glyph: str = "─") -> Plot:
        self.hlines.append((y, colour, label, glyph))
        return self

    def vline(self, x: float, colour: str = ORANGE, label: str = "") -> Plot:
        self.vlines.append((x, colour, label))
        return self

    # scales ─────────────────────────────────────────────────

    def _extent(self, axis: int) -> tuple[float, float]:
        ys = [y for ln in self.lines if ln.axis == axis for y in ln.ys if y is not None and not math.isnan(y) and (not self.log or y > 0)]
        if axis == 0:
            ys += [v for c in self.candles for v in (c.h, c.l)]
            ys += [v for b in self.bands for v in (b[2], b[3])]
            ys += [h[0] for h in self.hlines]
        if not ys:
            return 0.0, 1.0
        lo, hi = min(ys), max(ys)
        if hi <= lo:
            pad = abs(hi) * 0.01 or 1.0
            return lo - pad, hi + pad
        if self.log and lo > 0:
            k = (hi / lo) ** 0.04
            return lo / k, hi * k
        pad = (hi - lo) * 0.04
        return lo - pad, hi + pad

    def _xs(self) -> tuple[float, float]:
        if self.x_range:
            return self.x_range
        xs = [x for ln in self.lines for x in (ln.xs[0], ln.xs[-1]) if len(ln.xs)] + [c.t for c in self.candles]
        xs += [v for b in self.bands for v in (b[0], b[1])] + [v[0] for v in self.vlines]
        return (min(xs), max(xs)) if xs and max(xs) > min(xs) else (0.0, 1.0)

    # drawing ────────────────────────────────────────────────

    def render(self) -> Text:
        y0 = self.y_range or self._extent(0)
        y1 = self._extent(1)
        two = any(ln.axis == 1 for ln in self.lines)
        x_lo, x_hi = self._xs()
        rows = self.h - 1                                   # the last row is the time axis
        step = nice_step(y0[1] - y0[0], max(rows // 4, 2))
        ticks = [] if self.log else [t * step for t in range(math.ceil(y0[0] / step), math.floor(y0[1] / step) + 1)]
        if self.log and y0[0] > 0:
            ticks = _log_ticks(*y0)
        labels = [self.fmt(t) for t in ticks] + [h[2] or self.fmt(h[0]) for h in self.hlines]
        gut = self.gutter if self.gutter is not None else max((len(s) for s in labels), default=4) + 2
        left = (max(len(self.fmt(v)) for v in y1) + 1) if two else 0
        pw = max(self.w - gut - left, 4)                    # plot width in cells

        def fy(v: float, ext: tuple[float, float]) -> float:
            lo, hi = ext
            if self.log and lo > 0 and v > 0:
                return (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo))
            return (v - lo) / (hi - lo) if hi > lo else 0.5

        def col(x: float) -> float:
            return (x - x_lo) / (x_hi - x_lo) * (pw - 1) if x_hi > x_lo else 0.0

        def row(v: float, ext: tuple[float, float] = y0) -> int:
            return rows - 1 - round(fy(v, ext) * (rows - 1))

        cells: list[list[tuple[str, str]]] = [[(" ", "")] * pw for _ in range(rows)]
        if self.grid:
            for t in ticks:
                r = row(t)
                if 0 <= r < rows:
                    cells[r] = [("┄" if i % 2 == 0 else " ", GRID) for i in range(pw)]
        for x0_, x1_, lo, hi, colour, glyph in self.bands:
            c0, c1 = max(0, round(col(x0_))), min(pw - 1, round(col(x1_)))
            r0, r1 = sorted((row(hi), row(lo)))
            for r in range(max(r0, 0), min(r1, rows - 1) + 1):
                for c in range(c0, c1 + 1):
                    cells[r][c] = (glyph, colour)
        if self.candles:
            self._draw_candles(cells, col, lambda v: fy(v, y0), rows, pw)
        dots: dict[tuple[int, int], tuple[int, str]] = {}
        for ln in self.lines:
            ext = y1 if ln.axis == 1 else y0
            prev = None
            for x, y in zip(ln.xs, ln.ys, strict=False):
                if y is None or math.isnan(y) or (self.log and y <= 0):
                    prev = None
                    continue
                px, py = round(col(x) * 2), round((1 - fy(y, ext)) * (rows * 4 - 1))
                for qx, qy in (_segment(prev, (px, py)) if prev else [(px, py)]):
                    if 0 <= qx < pw * 2 and 0 <= qy < rows * 4:
                        key = (qy // 4, qx // 2)
                        dots[key] = (dots.get(key, (0, ""))[0] | _DOT[qy % 4][qx % 2], ln.colour)
                prev = (px, py)
        for (r, c), (bits, colour) in dots.items():
            cells[r][c] = (chr(0x2800 + bits), colour)
        for x, colour, _ in self.vlines:
            c = round(col(x))
            if 0 <= c < pw:
                for r in range(rows):
                    cells[r][c] = ("│", colour)
        marks: dict[int, tuple[str, str]] = {}
        for y, colour, label, glyph in self.hlines:
            r = row(y)
            if 0 <= r < rows:
                start = max((round(col(v[0])) + 1 for v in self.vlines), default=0) if self.vlines else 0
                for c in range(start, pw):
                    if cells[r][c][0] in (" ", "┄", "░", "▒"):
                        cells[r][c] = (glyph, colour)
                marks[r] = (f"◂{label or self.fmt(y)}", colour)

        out = Text(no_wrap=True, overflow="crop")
        tick_rows = {row(t): self.fmt(t) for t in ticks if 0 <= row(t) < rows}
        l_ticks = {}
        if two:
            l_ticks = {0: self.fmt(y1[1]), rows - 1: self.fmt(y1[0]), rows // 2: self.fmt(_mid(y1, self.log))}
        for r in range(rows):
            if two:
                out.append(f"{l_ticks.get(r, ''):>{left - 1}} ", style=snap(_axis_colour(self.lines, 1)))
            _append_runs(out, cells[r])
            if r in marks:
                out.append(f"{marks[r][0]:<{gut}}"[:gut], style=f"bold {snap(marks[r][1])}")
            elif r in tick_rows:
                out.append(f"┤{tick_rows[r]:<{gut - 1}}"[:gut], style=FAINT)
            else:
                out.append(" " * gut)
            out.append("\n")
        out.append(" " * left)
        out.append_text(self._time_axis(x_lo, x_hi, pw))
        return out

    def _draw_candles(self, cells, col, fy, rows: int, pw: int) -> None:
        sub = rows * 2                                          # half-row resolution for bodies
        for c in self.candles:
            x = round(col(c.t))
            if not 0 <= x < pw:
                continue
            colour = GREEN if c.c >= c.o else RED
            hi, lo = (1 - fy(c.h)) * (sub - 1), (1 - fy(c.l)) * (sub - 1)
            top, bot = sorted(((1 - fy(c.o)) * (sub - 1), (1 - fy(c.c)) * (sub - 1)))
            for r in range(max(int(hi // 2), 0), min(int(lo // 2), rows - 1) + 1):
                a, b = r * 2, r * 2 + 1                         # the two half rows of this text row
                body_a, body_b = top - 0.5 <= a <= bot + 0.5, top - 0.5 <= b <= bot + 0.5
                glyph = "█" if body_a and body_b else "▀" if body_a else "▄" if body_b else "│"
                cells[r][x] = (glyph, colour)

    def _time_axis(self, x_lo: float, x_hi: float, pw: int) -> Text:
        axis = [" "] * pw
        if self.times and x_hi > x_lo:
            span, n = x_hi - x_lo, max(pw // 22, 2)
            for i in range(n + 1):
                label = time_label(x_lo + span * i / n, span)
                at = min(max(round((pw - 1) * i / n) - (len(label) if i == n else len(label) // 2 if i else 0), 0), pw - len(label))
                if all(ch == " " for ch in axis[max(at - 1, 0):at + len(label) + 1]):
                    axis[at:at + len(label)] = label
        out = Text("".join(axis), style=FAINT, no_wrap=True)
        for x, colour, label in self.vlines:
            c = round((x - x_lo) / (x_hi - x_lo) * (pw - 1)) if x_hi > x_lo else 0
            if label and 0 <= c < pw:
                at = min(max(c - len(label) // 2, 0), pw - len(label))
                out = Text.assemble(out[:max(at - 1, 0)], " " if at else "", (label, f"bold {snap(colour)}"), " ", out[at + len(label) + 1:])
        return out

    def legend(self) -> Text:
        out = Text(no_wrap=True)
        for ln in self.lines:
            if ln.label:
                out.append("━━ ", style=snap(ln.colour))
                out.append(ln.label + ("  (left axis)" if ln.axis == 1 else "") + "   ", style=FAINT)
        return out


_DOT = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))      # braille bit by (row, column) inside a cell


def _segment(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """Every dot on the straight line from a to b (Bresenham), so a steep move draws as a stroke, not two dots."""
    (x0, y0), (x1, y1) = a, b
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = 1 if x0 < x1 else -1, 1 if y0 < y1 else -1
    err, out = dx + dy, []
    for _ in range(4000):
        out.append((x0, y0))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err, x0 = err + dy, x0 + sx
        if e2 <= dx:
            err, y0 = err + dx, y0 + sy
    return out


def _log_ticks(lo: float, hi: float) -> list[float]:
    out = []
    for e in range(math.floor(math.log10(lo)), math.ceil(math.log10(hi)) + 1):
        out += [m * 10 ** e for m in ((1, 2, 5) if hi / lo < 1000 else (1,))]
    return [t for t in out if lo <= t <= hi]


def _mid(ext: tuple[float, float], log: bool) -> float:
    return math.sqrt(ext[0] * ext[1]) if log and ext[0] > 0 else (ext[0] + ext[1]) / 2


def _axis_colour(lines: list[_Line], axis: int) -> str:
    return next((ln.colour for ln in lines if ln.axis == axis), FAINT)


def _append_runs(out: Text, cells: list[tuple[str, str]]) -> None:
    run, style = "", None
    for ch, colour in cells:
        s = snap(colour) if colour else ""
        if s != style and run:
            out.append(run, style=style or None)
            run = ""
        style, run = s, run + ch
    if run:
        out.append(run, style=style or None)


def stacked(columns: Sequence[Sequence[float]], colours: Sequence[str], height: int) -> list[Text]:
    """Stacked bands (HODL waves): each column is a list of shares, first band at the bottom. Two pixels a row."""
    px = height * 2
    grid: list[list[RGB | None]] = [[None] * len(columns) for _ in range(px)]
    for x, shares in enumerate(columns):
        total = sum(shares) or 1.0
        acc, y = 0.0, px
        for band, share in enumerate(shares):
            acc += share / total
            top = px - round(acc * px)
            for yy in range(top, y):
                grid[yy][x] = rgb_of(colours[band % len(colours)])
            y = top
    return pixels(grid)


def resample(xs: Sequence[float], ys: Sequence[float | None], n: int) -> tuple[list[float], list[float | None]]:
    """At most `n` points, keeping the last value of each slice, so a 5,000-point series draws in a millisecond."""
    if len(xs) <= n or n <= 0:
        return list(xs), list(ys)
    idx = [min(int((i + 1) * len(xs) / n) - 1, len(xs) - 1) for i in range(n)]
    return [xs[i] for i in idx], [ys[i] for i in idx]
