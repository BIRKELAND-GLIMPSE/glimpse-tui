"""Forecast heatmap: time runs across, price runs up, and probability is how solid the orange is.

Each cell is one shade block  ░ ▒ ▓ █  in one hue, so the forecast reads as a single orange cloud
thickening towards the closes the market is sure of — the same picture the site draws, and the same
one the front page's chart puts behind its band. Four steps, not nine, and one hue rather than a
heat gradient: the shape of the distribution is the thing to see, not a colour to decode. It still
costs almost nothing to draw, because a text row is a handful of colour runs of ordinary characters
rather than hundreds of individually coloured blocks. The ramp is one absolute log scale of
probability per bin (three steps from the 0.6% seed floor to 10%, then one for everything above),
the same in every column and at every zoom. What each close draws is computed once when data
arrives and the drawn chart is cached, so a clock tick redraws nothing and a cursor move redraws once.

History candles sit left of the NOW divider on the same price axis. Terminals without 24-bit colour
snap colours to the 256 palette, so there are two small palettes: one for truecolor, and one using
only colours the 256 palette holds exactly.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.widget import Widget

from . import fmt
from . import pricing as P
from .api import Candle
from .theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT

if TYPE_CHECKING:
    from .app import Terminal

RGB = tuple[int, int, int]

P_FLOOR = 0.006                # probability per bin below which a bin is market-maker seed, not belief
P_KNEE = 0.10
GLYPHS = "░▒▓█"                # thin to solid
KNEE = 3                       # GLYPHS[:KNEE] span P_FLOOR..P_KNEE, the rest span P_KNEE..1
BAND = "#8a4a10"               # tier 0 as hex: what a chart paints an 80% band in (GP), so both pictures match
ZOOMS = (1, 2, 5, 10, 20, 50)  # bins per cell
WIDTHS = (1, 2, 4, 6)          # characters per close; even widths split into two half-hour candles on hourly series
PACKS = {3600: (12, 6, 4, 3, 2), 86400: (7, 2)}    # zoomed out: closes per character, by cadence (hours, days)
WEEK_ORIGIN = 4 * 86400        # the epoch is a Thursday: week-long columns start on Monday


def scales(cadence: int) -> list[tuple[int, int]]:
    """Every horizontal zoom, widest view first, as (characters per column, closes per column)."""
    return [(1, p) for p in PACKS.get(cadence, ())] + [(w, 1) for w in WIDTHS]


def origin(span: int) -> int:
    return WEEK_ORIGIN if span % (7 * 86400) == 0 else 0


def crossing(ts: int, span: int, period: int, offset: int = 0) -> int | None:
    """The first instant `offset` mod `period` inside [ts, ts + span), if any: a midnight, a Monday, a noon."""
    b = ts + (offset - ts) % period
    return b if b < ts + span else None
GUTTER = 10                    # price labels on the right


def level(p: float) -> int:
    """Index into GLYPHS for a per-bin probability, -1 below the floor. Log spaced on each side of the knee."""
    if p <= P_FLOOR:
        return -1
    if p < P_KNEE:
        return min(int(math.log(p / P_FLOOR) / math.log(P_KNEE / P_FLOOR) * KNEE), KNEE - 1)
    high = len(GLYPHS) - KNEE
    return KNEE + min(int(math.log(p / P_KNEE) / math.log(1 / P_KNEE) * high), high - 1)


def threshold(i: int) -> float:
    """Lowest probability drawn as GLYPHS[i]."""
    if i < KNEE:
        return P_FLOOR * (P_KNEE / P_FLOOR) ** (i / KNEE)
    return P_KNEE * (1 / P_KNEE) ** ((i - KNEE) / (len(GLYPHS) - KNEE))


@dataclass(frozen=True)
class Palette:
    name: str
    tiers: tuple[RGB, RGB, RGB, RGB]   # one colour per shade block, thin to solid: the only colours the heat uses
    median: RGB
    rule: RGB          # grid lines
    select: RGB        # background of a box selection
    held: RGB
    up: RGB
    down: RGB
    cursor: RGB = (255, 255, 255)
    cursor_hist: RGB = (148, 148, 148)

    def heat(self, lvl: int) -> RGB:
        return self.tiers[min(max(lvl, 0), len(self.tiers) - 1)]


# One hue, four steps. The thinnest is BAND, the colour a chart paints an 80% band in, so the front page's
# forecast and this one are the same orange; the solid one is the terminal's orange. Nothing goes white: a
# bright cell is the mode, not an alarm.
TRUECOLOR = Palette(
    name="truecolor", tiers=((138, 74, 16), (176, 86, 12), (216, 104, 10), (255, 140, 26)),
    median=(84, 190, 232), rule=(46, 46, 46), select=(70, 42, 14), held=(11, 217, 138), up=(14, 173, 105), down=(222, 60, 75),
)
# Every colour here is an exact xterm-256 entry (cube levels 0/95/135/175/215/255 or the grey ramp), so a
# 256-colour terminal draws it as written instead of snapping it to a neighbour of a different hue.
ANSI256 = Palette(
    name="256", tiers=((135, 95, 0), (175, 95, 0), (215, 95, 0), (255, 135, 0)),
    median=(95, 175, 215), rule=(58, 58, 58), select=(68, 68, 68), held=(0, 215, 135), up=(0, 175, 95), down=(215, 95, 95),
)

_TRUECOLOR_PROGRAMS = {"iterm.app", "ghostty", "wezterm", "vscode", "hyper", "tabby", "rio", "warpterminal", "zed"}


def wants_truecolor(env: dict[str, str] | None = None, saved: str = "") -> bool:
    """Whether to draw in 24-bit colour. An explicit choice wins (GLIMPSE_COLORS, then the `c` key's saved
    setting); otherwise believe the terminal. SSH rarely forwards COLORTERM, so the fallback is the safe palette."""
    env = os.environ if env is None else env
    want = env.get("GLIMPSE_COLORS", "").lower() or saved
    if want in ("truecolor", "24bit", "256"):
        return want != "256"
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    term, prog = env.get("TERM", "").lower(), env.get("TERM_PROGRAM", "").lower()
    if prog in _TRUECOLOR_PROGRAMS or any(x in term for x in ("kitty", "alacritty", "ghostty", "direct", "foot", "wezterm")):
        return True
    if prog == "apple_terminal":                                   # 24-bit colour arrived with macOS 26 (Terminal build 460+)
        build = env.get("TERM_PROGRAM_VERSION", "0").split(".")[0]
        return build.isdigit() and int(build) >= 460
    return False


def nice_step(cell: float, min_cells: int) -> float:
    """A round price step that is a whole number of cells and at least `min_cells` apart.
    Prefers 1/2/2.5/4/5 x 10^k; bin sizes like 30 or 15 need 1.5/3/6 to land on a cell edge at all."""
    k0 = 10 ** math.floor(math.log10(max(cell * min_cells, 1e-9)))
    for mults in ((1, 2, 2.5, 4, 5), (1, 1.5, 2, 2.5, 3, 4, 5, 6)):
        for k in (k0, k0 * 10):
            for m in mults:
                step = m * k
                if step >= cell * min_cells and abs(step / cell - round(step / cell)) < 1e-9:
                    return step
    return cell * min_cells


def ruled_rows(grid: Grid, gtop: int, rows: int) -> dict[int, float]:
    """The horizontal rules of the price axis: visible cell → the round price it is labelled with. A cell is ruled
    when its own price span crosses a multiple of the step, not when it starts exactly on one, so a ladder that does
    not begin on a round price — most of them — still carries an axis, evenly spaced, at every zoom."""
    cell = grid.bin_size * grid.bpc
    step = nice_step(cell, 4)
    out: dict[int, float] = {}
    for g in range(max(gtop - rows + 1, 0), min(gtop, grid.cells - 1) + 1):
        lo = grid.y_lo + g * cell
        mark = math.ceil(lo / step - 1e-9) * step
        if mark < lo + cell - 1e-9:
            out[g] = mark
    return out


@dataclass(frozen=True)
class Grid:
    """Price axis: `n` bins of `bin_size` from `y_lo`, drawn `bpc` bins to a cell."""
    y_lo: float
    bin_size: float
    n: int
    bpc: int

    @property
    def cells(self) -> int:
        return -(-self.n // self.bpc)

    def cell_of_price(self, price: float) -> int:
        return max(0, min(self.cells - 1, int((price - self.y_lo) // (self.bin_size * self.bpc))))

    def bins_of(self, g: int) -> tuple[int, int]:
        return g * self.bpc, min((g + 1) * self.bpc, self.n) - 1

    def prices_of(self, g: int) -> tuple[float, float]:
        lo, hi = self.bins_of(g)
        return self.y_lo + lo * self.bin_size, self.y_lo + (hi + 1) * self.bin_size


def candle_part(k: Candle, lo: float, hi: float) -> str:
    """Which part of candle `k` crosses the price cell [lo, hi): 'body', 'wick' or ''."""
    b_lo, b_hi = min(k.o, k.c), max(k.o, k.c)
    if b_lo < hi and b_hi >= lo:
        return "body"
    if k.l < hi and k.h >= lo:
        return "wick"
    return ""


def fit_zoom(span_bins: float, cells_visible: int) -> int:
    """Smallest bins-per-cell that shows `span_bins` with some headroom."""
    for z in ZOOMS:
        if span_bins * 1.3 / z <= cells_visible:
            return z
    return ZOOMS[-1]


def rules_at(ts: int, span: int, cadence: int, chars_per_s: float) -> bool:
    """Whether a column starting at `ts` carries a vertical rule: midnights on hourly series and Mondays on daily
    ones, stepping up to Mondays and then month starts when zoomed out so far that rules would crowd together."""
    day = cadence < 86400 and 86400 * chars_per_s >= 6
    week = not day and 7 * 86400 * chars_per_s >= 6
    if day:
        return crossing(ts, span, 86400) is not None
    if week:
        return crossing(ts, span, 7 * 86400, WEEK_ORIGIN) is not None
    return any(datetime.fromtimestamp(d, UTC).day == 1 for d in range(ts - ts % 86400, ts + span, 86400) if d >= ts)


def axis_labels(cols: list[tuple[int, int, int]], width: int, hourly: bool, now_x: int | None) -> Text:
    """Time labels under the grid. `cols` is (x, column start time, seconds the column spans); the most important
    labels claim space first. A column spanning several closes is labelled by the boundary it contains."""
    line = [" "] * width
    placed: list[tuple[int, int, str]] = []
    cands: list[tuple[int, int, str, str]] = []
    if now_x is not None:
        cands.append((0, now_x, "NOW", f"bold {ORANGE}"))
    for x, ts, span in cols:
        if hourly:
            if (b := crossing(ts, span, 86400)) is not None:
                cands.append((1, x, datetime.fromtimestamp(b, UTC).strftime("%d %b"), DIM))
            elif crossing(ts, span, 43200) is not None:
                cands.append((2, x, "12:00", FAINT))
            elif (b := crossing(ts, span, 21600)) is not None:
                cands.append((3, x, datetime.fromtimestamp(b, UTC).strftime("%H:00"), FAINT))
        elif (b := next((d for d in range(ts, ts + span, 86400) if datetime.fromtimestamp(d, UTC).day == 1), None)) is not None:
            cands.append((1, x, datetime.fromtimestamp(b, UTC).strftime("%b"), DIM))
        elif (b := crossing(ts, span, 7 * 86400, WEEK_ORIGIN)) is not None:
            cands.append((2, x, datetime.fromtimestamp(b, UTC).strftime("%d %b"), FAINT))
    for _, x, label, style in sorted(cands, key=lambda c: (c[0], c[1])):
        a, b = x, x + len(label)
        if a < 0 or b > width or any(ch != " " for ch in line[max(a - 1, 0):min(b + 1, width)]):
            continue
        line[a:b] = label
        placed.append((a, b, style))
    out = Text("".join(line), no_wrap=True)
    for a, b, style in placed:
        out.stylize(style, a, b)
    return out


@lru_cache(maxsize=512)
def _style(fg: RGB, bg: RGB | None = None) -> Style:
    return Style(color=Color.from_rgb(*fg), bgcolor=Color.from_rgb(*bg) if bg else None)


class HeatmapPane(Widget):
    can_focus = False

    def __init__(self, id: str) -> None:
        super().__init__(id=id)
        self.palette = TRUECOLOR if self.t.truecolor else ANSI256
        self._key: tuple | None = None
        self._body = Text()
        self._levels: dict[tuple[int, int], list[int]] = {}
        self._levels_for = -1
        self.draws = 0                  # how many times the chart body was actually redrawn (the rest were cache hits)

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def levels(self, c0: int, c1: int, bpc: int) -> list[int]:
        """Glyph level of every cell in closes c0..c1 (one close, or the several a zoomed-out column holds), by mean
        probability per bin across them. Worked out once per data change."""
        t = self.t
        if self._levels_for != t.data_version:
            self._levels, self._levels_for = {}, t.data_version
        got = self._levels.get((c0, c1, bpc))
        if got is None:
            raws = [t.views[c].raw for c in range(c0, c1 + 1)]
            raw = raws[0] if len(raws) == 1 else [sum(x) / len(raws) for x in zip(*raws, strict=True)]
            got = self._levels[(c0, c1, bpc)] = [level(sum(raw[i:i + bpc]) / bpc) for i in range(0, len(raw), bpc)]
        return got

    def render(self) -> Text:
        t = self.t
        if not t.views:
            return Text(" loading…" if not t.error else f" {t.error}", style=DIM)
        rows = max(self.size.height - 2, 2)
        chart_w = max(self.size.width - GUTTER, 8)
        if t.hm_fit_pending:
            t.hm_fit(chart_w, rows)
        t.hm_follow(chart_w, rows)
        grid = t.hm_grid
        hist = t.hm_hist
        spot_g = grid.cell_of_price(t.spot) if t.spot > 0 else -1
        key = (t.data_version, len(hist), hist[-1].t if hist else 0, t.hm_sub, t.hm_col, t.hm_bin, t.hm_anchor, t.hm_left, t.hm_gtop,
               t.hm_w, t.hm_per, t.hm_bpc, self.size, spot_g, round(t.spot), t.held_version, t.hm_median, self.palette.name)
        if key != self._key:
            self._key, self._body = key, self._draw(grid, hist, rows, chart_w, spot_g)
            self.draws += 1
        readout = self._readout(grid, chart_w)
        readout.truncate(self.size.width, overflow="ellipsis")      # a line longer than the pane would wrap and push the chart down
        return Text.assemble(readout, "\n", self._body, no_wrap=True, overflow="crop")

    # ── drawing ─────────────────────────────────────────────

    def _draw(self, grid: Grid, hist: list[Candle], rows: int, chart_w: int, spot_g: int) -> Text:
        t, pal, w = self.t, self.palette, self.t.hm_w
        cadence, gtop = t.hm_cadence, t.hm_gtop
        cell_price = grid.bin_size * grid.bpc

        # columns on screen, with a one-character NOW divider before column 0
        layout: list[tuple[int, int]] = []
        x, now_x, c = 0, None, t.hm_left if t.hm_left < 0 else t.hm_gstart(t.hm_left)
        while c < len(t.views):
            if c == 0:
                if x + 1 > chart_w:
                    break
                now_x, x = x, x + 1
            cw = t.hm_width(c)                  # history candles are narrower than closes when two share an hour
            if x + cw > chart_w:
                break
            layout.append((c, x))
            x, c = x + cw, (c + 1 if c < 0 else t.hm_gend(c) + 1)     # a zoomed-out column holds several closes

        ruled_g = ruled_rows(grid, gtop, rows)
        spans = [(cx, *t.hm_col_time(c)) for c, cx in layout if c >= 0 or -c <= len(hist)]
        v_xs = {cx for cx, ts, span in spans if rules_at(ts, span, cadence, w / t.hm_span)}
        plain = "".join("│" if i in v_xs else " " for i in range(chart_w))
        ruled = "".join("┼" if i in v_xs else "─" for i in range(chart_w))

        sel = t.hm_selection if t.hm_anchor is not None else None
        ladder = grid.n * grid.bin_size
        cols = []                                   # per forecast column: (x, levels, median cell, held cells, selected cell range)
        for c, cx in layout:
            if c < 0:
                continue
            c1 = t.hm_gend(c)
            v = t.views[(c + c1) // 2]              # a zoomed-out column draws the median of its middle close
            med = grid.cell_of_price(v.median) if t.hm_median and v.band[1] - v.band[0] < 0.4 * ladder else -1
            held = {i // grid.bpc for u in t.views[c:c1 + 1] for i, oid in enumerate(u.row.option_ids)
                    if t.held.get((u.row.topic_id, oid))} if t.held else ()
            pick = (sel[2] // grid.bpc, sel[3] // grid.bpc) if sel and sel[0] <= c <= sel[1] else None
            cols.append((c, cx, self.levels(c, c1, grid.bpc), med, held, pick))
        body_w = max(t.hm_cw - 1, 1)                # a wide history candle keeps its last character as a gap
        candles = [(c, cx, hist[c]) for c, cx in layout if c < 0 and -c <= len(hist)]

        rule, now = _style(pal.rule), Style.parse(ORANGE)
        heat = [_style(pal.heat(i)) for i in range(len(GLYPHS))]
        heat_sel = [_style(pal.heat(i), pal.select) for i in range(len(GLYPHS))]
        g_cur = t.hm_bin // grid.bpc
        out = Text(no_wrap=True, overflow="crop")
        for r in range(rows):
            g = gtop - r
            base = ruled if g in ruled_g else plain
            cells: dict[int, tuple[str, Style]] = {}
            if 0 <= g < grid.cells:
                lo_p, hi_p = grid.prices_of(g)
                for c, cx, k in candles:
                    part = candle_part(k, lo_p, hi_p)
                    if part:
                        st = _style(pal.up if k.c >= k.o else pal.down)
                        if part == "body":         # one character wide: a heavy rule, so neighbouring candles do not fuse
                            cells[cx] = ("┃" if t.hm_cw == 1 else "█" * body_w, st)
                        else:
                            cells[cx + (body_w - 1) // 2] = ("│", st)
                    if c == t.hm_col and g == g_cur:
                        cells[cx] = ("█" * body_w, _style(pal.cursor_hist))
                for c, cx, lv, med, held, pick in cols:
                    lvl = lv[g]
                    picked = pick is not None and pick[0] <= g <= pick[1]
                    if c == t.hm_col and g == g_cur:
                        cells[cx] = ((GLYPHS[lvl] if lvl >= 0 else " ") * w, _style((0, 0, 0), pal.cursor))
                    elif g in held:
                        cells[cx] = ((GLYPHS[lvl] if lvl >= 0 else "o") * w, _style(pal.held, pal.select if picked else None))
                    elif g == med:
                        cells[cx] = ("─" * w, _style(pal.median, pal.select if picked else None))
                    elif lvl >= 0:
                        cells[cx] = (GLYPHS[lvl] * w, (heat_sel if picked else heat)[lvl])
                    elif picked:
                        cells[cx] = (" " * w, _style(pal.rule, pal.select))
            if now_x is not None:
                cells[now_x] = ("│", now)
            at, run, run_style = 0, "", None
            for xi in sorted(cells):
                text, st = cells[xi]
                if xi < at or xi + len(text) > chart_w:
                    continue
                if xi > at:
                    if run:
                        out.append(run, style=run_style)
                        run = ""
                    out.append(base[at:xi], style=rule)
                if st is not run_style and run:
                    out.append(run, style=run_style)
                    run = ""
                run, run_style, at = run + text, st, xi + len(text)
            if run:
                out.append(run, style=run_style)
            if at < chart_w:
                out.append(base[at:chart_w], style=rule)
            out.append_text(self._price_label(grid, g, g_cur, spot_g, ruled_g.get(g)))
            out.append("\n")
        out.append_text(axis_labels(spans, chart_w, cadence < 86400, now_x))
        return out

    def _price_label(self, grid: Grid, g: int, g_cur: int, spot_g: int, ruled: float | None) -> Text:
        """Spot and the cursor's row always get a label; otherwise only ruled rows, at the round price they cross."""
        def axis(x: float) -> str:          # one format down the whole axis, decimals only when a cell is under a unit
            return f"{x:,.0f}" if grid.bin_size * grid.bpc >= 1 else f"{x:,.2f}"

        if g == spot_g:
            return Text(f"◂{fmt.price(self.t.spot):>{GUTTER - 2}} ", style=f"bold #000000 on {ORANGE}")
        if g == g_cur:
            return Text(f" {axis(grid.prices_of(g)[0]):>{GUTTER - 2}} ", style=f"bold {TEXT}")
        if ruled is not None:
            return Text(f"┤{axis(ruled):>{GUTTER - 2}} ", style=DIM)
        return Text("│" + " " * (GUTTER - 1), style=FAINT)

    def _readout(self, grid: Grid, width: int) -> Text:
        """What is under the cursor. Rebuilt every paint (it holds a countdown); the chart below it is cached."""
        t, out = self.t, Text(no_wrap=True, overflow="ellipsis")
        c = t.hm_col
        lo_p, hi_p = grid.prices_of(t.hm_bin // grid.bpc)
        if c < 0:
            if -c > len(t.hm_hist):
                return out
            k = t.hm_hist[c]
            d = datetime.fromtimestamp(k.t, UTC)
            chg = (k.c / k.o - 1) if k.o else 0.0
            style = GREEN if k.c >= k.o else RED
            out.append(f" {d.strftime('%a %d %b %H:%M') if t.hm_cadence < 86400 else d.strftime('%a %d %b')}", style=f"bold {TEXT}")
            out.append("  history   ", style=FAINT)
            for label, v in (("O", k.o), ("H", k.h), ("L", k.l), ("C", k.c)):
                out.append(f"{label} ", style=FAINT)
                out.append(f"{fmt.price(v)}  ", style=style)
            out.append(fmt.roi(chg), style=f"bold {style}")
            return out
        sel = t.hm_selection if t.hm_anchor is not None or t.hm_gend(c) > c else None
        if sel:                                 # a box, or a zoomed-out cell of several closes: what the market gives all of it
            every, anyone, _ = P.chances(t.sel_probs)
            n, bins, short = sel[1] - sel[0] + 1, t.views[0].bins, t.hm_cadence < 86400
            out.append(" SELECTION " if t.hm_anchor is not None else f" {n} CLOSES ", style=f"bold #0D0D0D on {ORANGE}")
            if t.hm_anchor is None:
                out.append(f" {fmt.close_label(t.views[sel[0]].row.end_time_utc, short)} → "
                           f"{fmt.close_label(t.views[sel[1]].row.end_time_utc, short)}", style=f"bold {TEXT}")
            out.append(f" {fmt.span(bins[sel[2]][0], bins[sel[3]][1])}", style=f"bold {ORANGE}")
            out.append(f" × {n} closes" if n > 1 and t.hm_anchor is not None else "", style=DIM)
            out.append("   P(all land) " if n > 1 else "   prob ", style=FAINT)
            out.append(fmt.pct(every), style=f"bold {TEXT}")
            if n > 1:
                out.append("   P(any) ", style=FAINT)
                out.append(fmt.pct(anyone), style=f"bold {TEXT}")
            out.append("   │  cursor", style=FAINT)
        v = t.views[c]
        b_lo, b_hi = grid.bins_of(t.hm_bin // grid.bpc)
        raw = sum(v.raw[b_lo:b_hi + 1])
        odds = P.PAYOUT_SATS * (1 - P.FEE) / (raw * 100 * (1 + P.FEE)) if raw > 0 else 0.0
        out.append(f" {fmt.close_label(v.row.end_time_utc, t.hm_cadence < 86400)}", style=f"bold {TEXT}")
        out.append("   " if sel else f"  closes in {fmt.countdown(v.row.end_time_utc)}   ", style=DIM)
        out.append(fmt.span(lo_p, hi_p), style=f"bold {ORANGE}")
        out.append("   prob ", style=FAINT)
        out.append(fmt.pct(raw / v.total), style=f"bold {TEXT}")
        out.append("   odds ", style=FAINT)
        out.append(fmt.odds(odds), style=f"bold {ORANGE}")
        if sel:
            return out                      # the selection's figures come first; the close's own are one keypress away
        if width >= 120 and v.band[1] - v.band[0] < 0.4 * grid.n * grid.bin_size:
            # An 80% band is ±1.2816σ of a normal: read the market's σ off it, and annualise it over the time left.
            sigma = (v.band[1] - v.band[0]) / 2.5631
            years = max(v.row.end_time_utc - time.time(), 600) / 31_557_600
            out.append("   σ ", style=FAINT)
            out.append(f"±{fmt.price(sigma)}", style=TEXT)
            out.append("   iv ", style=FAINT)
            out.append(f"{sigma / v.median / math.sqrt(years):.0%}" if v.median > 0 else "-", style=f"bold {TEXT}")
        if width >= 100:
            out.append("   median ", style=FAINT)
            out.append(fmt.price(v.median), style=_style(self.palette.median))
            out.append("   80% ", style=FAINT)
            out.append(f"{fmt.kprice(v.band[0])}–{fmt.kprice(v.band[1])}", style=TEXT)
        return out

    def legend(self) -> Text:
        """The ramp with the probability per bin where it starts, turns solid, and tops out."""
        pal, out = self.palette, Text(no_wrap=True)
        out.append(f" p/bin {fmt.pct(P_FLOOR)} ", style=DIM)
        for i, ch in enumerate(GLYPHS):
            if i == KNEE:
                out.append(f" {fmt.pct(P_KNEE)} ", style=DIM)
            out.append(f"{ch}", style=_style(pal.heat(i)))
        out.append(" 100%  ", style=DIM)
        out.append("─", style=_style(pal.median))
        out.append(" median ", style=DIM)
        return out
