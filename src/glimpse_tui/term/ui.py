"""The kit every function page is built from: label and value rows, dense right-aligned tables with thin rules,
section heads, change colouring. A pane's `draw` returns a list of `Text` lines made with these, so every page
reads like the same terminal (TERMINAL.md 3.4).
"""
from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.text import Text

from .. import charts, fmt
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT

CYAN, VIOLET, YELLOW = charts.CYAN, charts.VIOLET, charts.YELLOW
RULE = "#3a3a3a"
CURSOR_BG = "#2b1500"
Cell = str | Text | None


def t(*parts: tuple[str, str] | str | Text) -> Text:
    """Text.assemble with no wrapping: t(("FEES ", FAINT), ("3", f"bold {TEXT}"))."""
    return Text.assemble(*parts, no_wrap=True, overflow="crop")


def kv(label: str, value: str | Text, style: str = f"bold {TEXT}", gap: int = 1) -> Text:
    """`realized price 51,840`: a faint label and a bright value."""
    out = Text(no_wrap=True)
    out.append(label + " " * gap, style=FAINT)
    out.append_text(value) if isinstance(value, Text) else out.append(value, style=style)
    return out


def join(parts: Sequence[Text], sep: str = "   ") -> Text:
    return Text(sep, no_wrap=True).join(list(parts))


def section(title: str, width: int, right: str = "") -> Text:
    """`FEES ───────────── sat/vB`: a section head with a thin rule."""
    out = Text(no_wrap=True, overflow="crop")
    out.append(title + " ", style=f"bold {ORANGE}")
    out.append("─" * max(width - len(title) - 1 - (len(right) + 1 if right else 0), 0), style=RULE)
    if right:
        out.append(" " + right, style=FAINT)
    return out


def note(text: str, style: str = FAINT) -> Text:
    return Text(text, style=style, no_wrap=True, overflow="crop")


def wrap(text: str, width: int, style: str = DIM, indent: str = "") -> list[Text]:
    """Plain word wrap for HELP pages and filings."""
    out: list[Text] = []
    for para in text.split("\n"):
        line = indent
        for word in para.split(" "):
            if len(line) + len(word) + 1 > width and line.strip():
                out.append(Text(line.rstrip(), style=style, no_wrap=True))
                line = indent
            line += word + " "
        out.append(Text(line.rstrip(), style=style, no_wrap=True))
    return out


# ── numbers ─────────────────────────────────────────────────

def signed_pct(frac: float | None, digits: int = 1) -> Text:
    """+2.1% in green, −0.2% in red; a missing value is a faint dash."""
    if frac is None or math.isnan(frac):
        return Text("–", style=FAINT)
    s = f"{'+' if frac > 0 else fmt.MINUS if frac < 0 else ''}{abs(frac) * 100:.{digits}f}%"
    return Text(s, style=GREEN if frac > 0 else RED if frac < 0 else DIM)


def signed(v: float | None, digits: int = 2) -> Text:
    if v is None or math.isnan(v):
        return Text("–", style=FAINT)
    s = f"{'+' if v > 0 else fmt.MINUS if v < 0 else ''}{abs(v):,.{digits}f}"
    return Text(s, style=GREEN if v > 0 else RED if v < 0 else DIM)


def arrow(v: float | None) -> Text:
    if not v:
        return Text("·", style=FAINT)
    return Text("▲", style=GREEN) if v > 0 else Text("▼", style=RED)


def px(v: float | None, decimals: int = 2) -> str:
    """A price at its instrument's precision, thousands separated."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    return f"{v:,.{decimals}f}"


def usd(v: float | None) -> str:
    """$52.2B, $148.80, $0.0042."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    a, sign = abs(v), fmt.MINUS if v < 0 else ""
    for cut, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= cut:
            return f"{sign}${a / cut:,.2f}{unit}" if a / cut < 100 else f"{sign}${a / cut:,.1f}{unit}"
    if a >= 10_000:
        return f"{sign}${a:,.0f}"
    return f"{sign}${a:,.2f}" if a >= 0.01 or a == 0 else f"{sign}${a:.4f}"


def big(v: float | None, digits: int = 2) -> str:
    """1.25M, 52.1k, 640,000 stays 640,000 under a million."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    a = abs(v)
    for cut, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= cut:
            return f"{v / cut:,.{digits}f}{unit}"
    return f"{v:,.0f}" if a >= 1000 else f"{v:,.{digits}f}"


def sats_usd(sats: float, price: float | None) -> Text:
    """₿182,340 ($148.80): sats first, dollars beside them (rule 8)."""
    out = Text(fmt.sats(sats), style=f"bold {ORANGE}", no_wrap=True)
    if price:
        out.append(f" ({usd(sats / 1e8 * price)})", style=DIM)
    return out


def hashrate(hs: float) -> str:
    """Hashes a second, in EH/s."""
    return f"{hs / 1e18:,.1f} EH/s"


def vbytes(v: float) -> str:
    return f"{v / 1e6:,.1f} MvB" if v >= 1e5 else f"{v:,.0f} vB"


def when(ts: float, now: float | None = None) -> str:
    """`7m`, `3h`, `2d`: how long ago."""
    from ..data.core import ago
    return ago((time.time() if now is None else now) - ts)


def day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d %b %Y")


def stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d %b %H:%M UTC")


# ── tables ──────────────────────────────────────────────────

@dataclass
class Col:
    head: str
    align: str = "right"            # right | left
    width: int = 0                  # 0: fit the content
    style: str = TEXT
    flex: bool = False              # takes whatever width is left over (one per table)


def table(cols: Sequence[Col], rows: Sequence[Sequence[Cell]], width: int, cursor: int | None = None, gap: int = 2,
          rule: bool = True) -> list[Text]:
    """A dense table: faint headers over a thin rule, numbers right-aligned, the cursor row on burnt orange.
    Cells are strings or `Text` (which keep their own colours). The flex column absorbs or gives up width."""
    n = len(cols)
    cells = [[c if isinstance(c, Text) else Text("" if c is None else str(c)) for c in list(r)[:n]] + [Text("")] * (n - len(r)) for r in rows]
    widths = [max([c.width or len(c.head)] + ([x[i].cell_len for x in cells] if not c.width else [])) for i, c in enumerate(cols)]
    spare = width - sum(widths) - gap * (n - 1)
    for i, c in enumerate(cols):
        if c.flex:
            widths[i] = max(widths[i] + spare, min(widths[i], 6))

    def line(parts: Sequence[Text], styles: Sequence[str], bg: str = "") -> Text:
        out = Text(no_wrap=True, overflow="crop")
        for i, (p, c) in enumerate(zip(parts, cols, strict=True)):
            p = p.copy()
            if p.cell_len > widths[i]:
                p.truncate(widths[i], overflow="ellipsis")
            pad = " " * (widths[i] - p.cell_len)
            if not p.spans and not p.style:
                p.stylize(styles[i])
            out.append(pad) if c.align == "right" else None
            out.append_text(p)
            out.append(pad) if c.align != "right" else None
            if i < n - 1:
                out.append(" " * gap)
        if bg:
            out.pad_right(max(width - out.cell_len, 0))
            out.stylize(f"on {bg}")
        return out

    out = [line([Text(c.head) for c in cols], [FAINT] * n)]
    if rule:
        out.append(Text("─" * min(width, sum(widths) + gap * (n - 1)), style=RULE, no_wrap=True))
    for i, r in enumerate(cells):
        out.append(line(r, [c.style for c in cols], CURSOR_BG if cursor == i else ""))
    return out


def bar_row(label: str, frac: float, width: int, colour: str = ORANGE, label_w: int = 6, tail: str | Text = "") -> Text:
    """`80k ▏██████████▌  ◂ spot`: one row of a horizontal histogram."""
    out = Text(no_wrap=True, overflow="crop")
    out.append(f"{label:>{label_w}} ", style=DIM)
    out.append("▏", style=RULE)
    room = max(width - label_w - 2 - (len(tail) + 1 if tail else 0), 4)
    body = charts.hbar(frac, room)
    out.append(body, style=charts.snap(colour))
    if tail:
        out.append(" " * (room - len(body) + 1))
        out.append_text(tail) if isinstance(tail, Text) else out.append(tail, style=FAINT)
    return out


def centre(lines: Sequence[str | Text], width: int, height: int, style: str = DIM) -> list[Text]:
    """A message in the middle of an empty pane: loading, offline, or how to start."""
    body = [ln if isinstance(ln, Text) else Text(ln, style=style) for ln in lines]
    top = max((height - len(body)) // 2, 0)
    out = [Text("")] * top
    for ln in body:
        pad = max((width - ln.cell_len) // 2, 0)
        out.append(Text(" " * pad, no_wrap=True) + ln)
    return out
