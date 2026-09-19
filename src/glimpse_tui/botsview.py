"""The bots screen: the marketplace, in a terminal. Pick a picture of the future on the left, read it on the right,
press enter twice and it trades from this computer.

Nothing here is a track record. The list shows what each model believes *now* about the nearest close (which way it
leans, how wide it is against the baseline). The pane on the right takes one model: what it is, its whole
distribution over the price ranges of the close you stop on against the market's prices, and under that a time
series chart: the hourly candles the model read, then its median path and 80% band across every close ahead, with
the market's median for contrast. `i` opens the model's full account of itself: the idea, what it reads, the
mathematics, what it trades, and the shared machinery. All computed on this machine from public candles.
"""
from __future__ import annotations

import math
import textwrap
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widget import Widget

from . import fmt
from . import pricing as P
from .api import Candle
from .heatmap import axis_labels
from .theme import DIM, FAINT, GREEN, ORANGE, RED, RULE, TEXT

if TYPE_CHECKING:
    from .app import RowView, Terminal
    from .bots import Bot

TABS = ("All", "Running", "Bullish", "Bearish", "Sideways", "Volatile", "Mine")
SORTS = ("family", "disagrees most with the market", "most bullish first")
LIST_WIDTH = 66
DETAIL_MIN_WIDTH = 112          # narrower terminals show the list alone
VIEW_MARK = {"bullish": ("▲", GREEN), "bearish": ("▼", RED), "sideways": ("◆", ORANGE), "volatile": ("◇", ORANGE), "neutral": ("·", DIM)}
MARKET = "#54bee8"              # the heatmap's median colour: the market's own middle, for contrast with the bot's
GUTTER = 9                      # price labels on the right of the chart
HOURS_PER_COL = (1, 2, 3, 4, 6, 8, 12, 24, 36, 48, 72, 96, 120, 168)
CHART_MIN_ROWS = 5
LADDER_MIN_ROWS = 7
LADDER_ROWS = 13


@dataclass
class Scan:
    """What one bot says about the nearest close, right now."""
    view: str = ""
    lean: float = 0.0            # median, in baseline sigmas from spot
    width: float = 1.0           # 80% band against the baseline's
    gap: float = 0.0             # total variation distance from the market's distribution, 0..1
    median: float = 0.0
    low: float = 0.0
    high: float = 0.0
    probs: tuple[float, ...] = ()
    error: str = ""


def matches(bot: Bot, needle: str) -> bool:
    hay = f"{bot.id} {bot.name} {bot.family} {bot.blurb} {' '.join(bot.factors)} {bot.kind}".lower()
    return all(w in hay for w in needle.lower().split())


def visible(t: Terminal) -> list[Bot]:
    """The bots under the current tab, filter and sort."""
    tab = TABS[t.bot_tab]
    out = []
    for b in t.bots:
        s = t.bot_scan.get(b.id)
        if tab == "Running" and not (b.id in t.runners and t.runners[b.id].running):
            continue
        if tab == "Mine" and b.family != "Mine":
            continue
        if tab in ("Bullish", "Bearish", "Sideways", "Volatile") and (s is None or s.view != tab.lower()):
            continue
        if t.bot_filter and not matches(b, t.bot_filter):
            continue
        out.append(b)
    sort = SORTS[t.bot_sort]
    if sort == SORTS[1]:
        out.sort(key=lambda b: -(t.bot_scan[b.id].gap if b.id in t.bot_scan else -1))
    elif sort == SORTS[2]:
        out.sort(key=lambda b: -(t.bot_scan[b.id].lean if b.id in t.bot_scan else -99))
    return out


def window(probs, rows: int, tail: float = 0.002) -> tuple[int, int, int]:
    """Which bins to draw and how many of them go on one line, so `rows` lines hold the picture's central mass.
    A tight picture is drawn bin by bin; a wide one groups neighbouring bins until it fits."""
    n = len(probs)
    cum, lo, hi = 0.0, 0, n - 1
    for i, p in enumerate(probs):
        cum += p
        if cum > tail:
            lo = i
            break
    cum = 0.0
    for i in range(n - 1, -1, -1):
        cum += probs[i]
        if cum > tail:
            hi = i
            break
    if hi < lo:
        lo = hi = max(range(n), key=lambda i: probs[i])
    step = max(1, -(-(hi - lo + 1) // rows))
    if step == 1:                                            # room to spare: a little quiet ground either side
        pad = min(rows - (hi - lo + 1), 4) // 2
        lo, hi = max(0, lo - pad), min(n - 1, hi + pad)
    return lo, hi, step


def overlay(bot: float, market: float, width: int) -> Text:
    """One bar holding both pictures: orange where they agree, green where the bot puts more on the range than the
    market charges (what it buys), red shading where the market charges more than the bot thinks it is worth."""
    out = Text(no_wrap=True)
    both = round(min(bot, market) * width)
    out.append("█" * both, style=ORANGE)
    if bot > market:
        out.append(fmt.bar(bot - both / width, width)[: width - both], style=GREEN)
    else:
        out.append("░" * (round(market * width) - both), style=RED)
    out.append(" " * max(width - out.cell_len, 0))
    return out


def ladder(probs, bins, spot: float, width: int, rows: int = 13, market=None, min_edge: float = 0.10) -> Text:
    """The bot's picture of the close, highest price on top. With the market's prices (price/100 per range, what a
    contract costs) each bar carries both, so the gap between them, which is what the bot trades, is the picture."""
    out = Text(no_wrap=True, overflow="crop")
    if not probs or not bins:
        return out
    lo, hi, step = window(probs, rows)
    groups = [(i, min(i + step - 1, hi)) for i in range(lo, hi + 1, step)]
    weights = [sum(probs[a:b + 1]) for a, b in groups]
    prices = [sum(market[a:b + 1]) for a, b in groups] if market else []
    top = max(max(weights), max(prices, default=0), 1e-9)
    barw = max(min(width - 60, 44), 8)
    out.append(f"  {'price at close':<22}{'bot':>7}{'market' if market else '':>9}   ", style=FAINT)
    out.append(f"{step} ranges per row\n" if step > 1 else "\n", style=FAINT)
    net = P.PAYOUT_SATS * (1 - P.FEE)
    for n in range(len(groups) - 1, -1, -1):                                       # highest price on top
        (a, b), p = groups[n], weights[n]
        g_lo, g_hi = bins[a][0], bins[b][1]
        here = g_lo <= spot < g_hi
        out.append(f"  {fmt.span(g_lo, g_hi):<16}", style=f"bold {TEXT}" if here else TEXT)
        out.append(f"{'◂ now' if here else '':<6}", style=f"bold {TEXT}")
        out.append(f"{fmt.pct(p):>7}", style=f"bold {ORANGE}")
        if market:
            m = prices[n]
            out.append(f"{fmt.pct(m):>9}   ", style=TEXT)
            out.append_text(overlay(p / top, m / top, barw))
            if m > 0 and p * net > m * P.PAYOUT_SATS * (1 + P.FEE) * (1 + min_edge):
                out.append(f"  bot +{(p - m) * 100:.0f} pts · buys", style=f"bold {GREEN}")
        else:
            out.append("   ")
            out.append(fmt.bar(p / top, barw).ljust(barw), style=ORANGE)
        out.append("\n")
    if market:
        out.append("\n  ")
        out.append("█", style=ORANGE)
        out.append(" bot and market agree     ", style=FAINT)
        out.append("█", style=GREEN)
        out.append(" bot above the market: it buys     ", style=FAINT)
        out.append("░", style=RED)
        out.append(" market above the bot\n", style=FAINT)
    return out


# ── the time series: history, then the bot's path ──────────

def columns(closes: list[RowView], history_hours: int, now: float, chart_w: int) -> tuple[int, int, int]:
    """(hours per column, history columns, forecast columns) so the closes ahead take the finest time scale at which
    they and a fair share of history (up to 38% of the width, or all there is) fit side by side."""
    if not closes or chart_w < 8:
        return 1, 0, 0
    span_h = max((closes[-1].row.end_time_utc - now) / 3600.0, 1e-6)
    share = math.ceil(0.38 * chart_w)
    for hpc in HOURS_PER_COL:
        fc = math.ceil(span_h / hpc)
        hc = min(history_hours // hpc, share)
        if fc + hc <= chart_w - 1:
            return hpc, min(history_hours // hpc, chart_w - 1 - fc), fc
    hpc = HOURS_PER_COL[-1]
    fc = min(math.ceil(span_h / hpc), chart_w - 1)
    return hpc, min(history_hours // hpc, chart_w - 1 - fc), fc


def round_step(x: float) -> float:
    """The smallest 1, 2, 2.5 or 5 × 10^k at or above `x`: where the chart draws a price rule."""
    k = 10 ** math.floor(math.log10(max(x, 1e-9)))
    return next((m * k for m in (1, 2, 2.5, 5, 10) if m * k >= x), 10 * k)


def pack(bars: list[Candle], t0: int, hpc: int, cols: int) -> list[Candle | None]:
    """History column k (0 is the one touching NOW) holds the bars that open in [t0 − (k + 1)·hpc, t0 − k·hpc) hours,
    merged into one candle. `bars` are hourly, oldest first, keyed by open time."""
    out: list[Candle | None] = [None] * cols
    by_col: dict[int, list[Candle]] = {}
    for k in bars:
        col = (t0 - k.t - 1) // (hpc * 3600)
        if 0 <= col < cols:
            by_col.setdefault(col, []).append(k)
    for col, ks in by_col.items():
        ks.sort(key=lambda k: k.t)
        out[col] = Candle(ks[0].t, ks[0].o, max(k.h for k in ks), min(k.l for k in ks), ks[-1].c)
    return out


def chart(bars: list[Candle], closes: list[RowView], scans: dict[int, Scan], cur: int, spot: float, now: float,
          width: int, rows: int) -> Text:
    """Price up, time across. Left of NOW the hourly candles the model read, merged to the chart's time scale; right
    of it the bot's median (─) and 80% band (░) at every close ahead, the market's median (·) for contrast, and the
    close the ladder shows (▒ █). One row per price step, labelled on the right where the step is a round number."""
    out = Text(no_wrap=True, overflow="crop")
    chart_w = width - GUTTER - 2
    if not closes or rows < 3 or chart_w < 8:
        return out
    t0 = int(now // 3600) * 3600                                            # the hour in progress: no complete bar yet
    hist_h = max(int((t0 - bars[0].t) // 3600), 0) if bars else 0
    hpc, hc, fc = columns(closes, hist_h, now, chart_w)
    x0 = chart_w - 1 - fc - hc                                              # blank on the left when history is short
    now_x = x0 + hc
    hist = pack(bars, t0, hpc, hc)

    ahead: dict[int, list[tuple[int, RowView, Scan | None]]] = {}          # column -> the closes that land in it
    for i, v in enumerate(closes):
        col = min(int((v.row.end_time_utc - now) / 3600.0 // hpc), fc - 1)
        ahead.setdefault(max(col, 0), []).append((i, v, scans.get(v.row.topic_id)))
    lows = [k.l for k in hist if k] + [s.low for cs in ahead.values() for _, _, s in cs if s and not s.error]
    highs = [k.h for k in hist if k] + [s.high for cs in ahead.values() for _, _, s in cs if s and not s.error]
    if spot > 0:
        lows.append(spot)
        highs.append(spot)
    if not lows:
        return out
    lo, hi = min(lows), max(highs)
    pad = max((hi - lo) * 0.06, spot * 0.002 if spot > 0 else 1e-9)
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / rows
    row_of = lambda p: min(max(int((hi - p) // step), 0), rows - 1)        # noqa: E731  0 is the top row
    rule_step = round_step(2 * step)
    ruled = {r: lo + (rows - r) * step for r in range(rows)}
    ruled = {r: p for r, p in ruled.items() if math.floor(p / rule_step) != math.floor((p - step) / rule_step)}
    for r in ruled:
        ruled[r] = math.floor(ruled[r] / rule_step) * rule_step
    spot_r = row_of(spot) if spot > 0 else -1

    cells: list[dict[int, tuple[str, str]]] = [{} for _ in range(rows)]
    for k_i, k in enumerate(hist):
        if k is None:
            continue
        x = now_x - 1 - k_i
        colour = GREEN if k.c >= k.o else RED
        for r in range(row_of(k.h), row_of(k.l) + 1):
            top, bottom = hi - r * step, hi - (r + 1) * step
            body = min(k.o, k.c) < top and max(k.o, k.c) >= bottom
            cells[r][x] = ("█" if body else "│", colour)
    for col, cs in ahead.items():                                          # band, then the market's median through it, then the bot's
        x = now_x + 1 + col
        here = any(i == cur for i, _, _ in cs)
        done = [s for _, _, s in cs if s and not s.error]
        if done:
            for r in range(row_of(max(s.high for s in done)), row_of(min(s.low for s in done)) + 1):
                cells[r][x] = ("▒" if here else "░", ORANGE)
        elif here:
            for r in range(rows):
                cells[r][x] = ("╎", DIM)
        for _, v, _ in cs:
            if v.median > 0:
                cells[row_of(v.median)][x] = ("·", f"bold {MARKET}")
        if done:
            cells[row_of(done[-1].median)][x] = ("█", f"bold {TEXT}") if here else ("─", f"bold {ORANGE}")
    for r in range(rows):
        cells[r][now_x] = ("│", f"bold {ORANGE}")

    for r in range(rows):
        base_ch, base_style = ("─", RULE) if r in ruled else (" ", RULE)
        out.append("  ")
        at = 0
        for x in sorted(cells[r]):
            if x >= chart_w:
                break
            if x > at:
                out.append(base_ch * (x - at), style=base_style)
            ch, style = cells[r][x]
            out.append(ch, style=style)
            at = x + 1
        if at < chart_w:
            out.append(base_ch * (chart_w - at), style=base_style)
        if r == spot_r:
            out.append(f"◂{fmt.price(spot):>{GUTTER - 2}} ", style=f"bold #000000 on {ORANGE}")
        elif r in ruled:
            out.append(f"┤{fmt.price(ruled[r]):>{GUTTER - 2}} ", style=DIM)
        else:
            out.append("│" + " " * (GUTTER - 1), style=FAINT)
        out.append("\n")
    spans = [(now_x - 1 - k, t0 - (k + 1) * hpc * 3600, hpc * 3600) for k in range(hc)]
    spans += [(now_x + 1 + c, int(now) + c * hpc * 3600, hpc * 3600) for c in range(fc)]
    out.append("  ")
    out.append_text(axis_labels(spans, chart_w, hpc < 24, now_x))
    out.append("\n")
    return out


def chart_header(hpc: int, n_closes: int, walking: bool) -> Text:
    out = Text(no_wrap=True, overflow="crop")
    out.append(f"  {hpc}h per column   ", style=FAINT)
    out.append("█", style=GREEN)
    out.append(" history  ", style=FAINT)
    out.append("░ ─", style=ORANGE)
    out.append(" bot 80% band, median  ", style=FAINT)
    out.append("·", style=f"bold {MARKET}")
    out.append(" market median  ", style=FAINT)
    out.append("▒ █", style=ORANGE)
    out.append(" this close", style=FAINT)
    if not walking:
        out.append("   tab", style=f"bold {ORANGE}")
        out.append(f" walk the {n_closes} closes", style=DIM)
    return out


# ── the about screen ────────────────────────────────────────

LABELS = {"idea": "IDEA", "reads": "READS", "maths": "MATHS", "trades": "TRADES", "now": "NOW", "machinery": "MACHINERY",
          "scale": "SCALE", "runner": "RUNNER", "reference": "REFERENCE"}


def sections(bot: Bot) -> list[tuple[str, str]]:
    """The about-screen sections of a bot: a zoo model explains itself; a file of your own shows its docstring."""
    if bot.model is not None:
        from .zoo.core import TRADING_NOTE, explain

        return explain(bot.model) if not bot.error else [("idea", bot.description), ("runner", TRADING_NOTE)]
    return [("idea", bot.description or bot.blurb)]


def stance(s: Scan | None, spot: float) -> str:
    """One line on what the picture says right now, against spot, the baseline and the market."""
    if s is None or s.error or not s.median:
        return ""
    lean = f"{s.lean:+.2f}σ" if abs(s.lean) >= 0.005 else "on spot"
    return (f"{s.view} · expects {fmt.price(s.median)} ({fmt.roi(s.median / spot - 1) if spot else ''}) · 8 in 10 between "
            f"{fmt.price(s.low)} and {fmt.price(s.high)} · centre {lean} from the baseline · {s.width:.2f}× its width · "
            f"{fmt.pct(s.gap)} of its probability placed differently from the market")


def about(bot: Bot, s: Scan | None, spot: float, width: int) -> Text:
    """The whole account of one model, wrapped to `width`: a label column and the text beside it."""
    out = Text(no_wrap=False)
    out.append(bot.name.upper(), style=f"bold {ORANGE}")
    kind = f" · {bot.kind}" if bot.model is not None else ""
    out.append(f"   {bot.family}{kind}" + (f" · {', '.join(bot.factors)}" if bot.factors else ""), style=DIM)
    out.append(f"\n{bot.blurb}\n", style=TEXT)
    label_w, body_w = 11, max(width - 13, 30)
    parts = sections(bot)
    if (now := stance(s, spot)):
        parts.insert(next((i for i, (k, _) in enumerate(parts) if k in ("machinery", "scale")), len(parts)), ("now", now))
    for key, text in parts:
        out.append("\n")
        lines = textwrap.wrap(" ".join(text.split()), body_w) or [""]
        for i, line in enumerate(lines):
            out.append(f"{LABELS.get(key, key.upper()) if i == 0 else '':<{label_w}}", style=f"bold {GREEN if key == 'now' else ORANGE}")
            out.append(line + "\n", style=TEXT if key in ("idea", "now") else DIM)
    return out


class BotListPane(Widget):
    can_focus = False
    DEFAULT_CSS = "BotListPane { text-wrap: nowrap; text-overflow: ellipsis; }"

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t, out = self.t, Text(no_wrap=True, overflow="ellipsis")
        w = max(self.size.width - 2, 40)
        for i, name in enumerate(TABS):
            n = sum(1 for r in t.runners.values() if r.running) if name == "Running" else None
            label = f" {name}{f' {n}' if n else ''} "
            out.append(label, style=f"bold #000000 on {ORANGE}" if i == t.bot_tab else DIM)
        out.append("\n")
        if not t.bots:
            out.append("\n  loading the model zoo…" if not t.bots_error else f"\n  {t.bots_error}", style=DIM)
            return out
        bots = visible(t)
        note = f"  {len(bots)} bots · sorted by {SORTS[t.bot_sort]}"
        if t.bot_filter:
            note += f" · /{t.bot_filter}"
        if t.bot_scanning:
            note += f" · reading the market {len(t.bot_scan)}/{len(t.bots)}"
        out.append(note + "\n", style=FAINT)
        name_w = max(w - 31, 14)
        out.append(f"    {'':<{name_w}} {'next close':<11}{'expects':>8}\n", style=FAINT)
        if not bots:
            empty = {"Running": "Nothing running. Pick a bot and press enter to deploy it.",
                     "Mine": f"Add your own: {t.bots_home}/<name>.py with forecast(book, closes, hours) -> probs"}
            out.append(f"\n  {empty.get(TABS[t.bot_tab], 'No bot matches. esc clears the filter.')}", style=DIM)
            return out
        t.bot_cur = max(0, min(t.bot_cur, len(bots) - 1))
        lines: list[tuple[int | None, Bot | str]] = []
        grouped = SORTS[t.bot_sort] == "family"
        family = None
        for i, b in enumerate(bots):
            if grouped and b.family != family:
                family = b.family
                lines.append((None, f"{family.upper()} · {sum(1 for x in bots if x.family == family)}"))
            lines.append((i, b))
        at = next(n for n, (i, _) in enumerate(lines) if i == t.bot_cur)
        h = max(self.size.height - 6, 3)
        start = max(0, min(at - h // 2, len(lines) - h))
        for i, item in lines[start:start + h]:
            if i is None:
                out.append(f"  {item} ".ljust(w - 1, "─") + "\n", style=FAINT)
                continue
            b, sel = item, i == t.bot_cur
            r = t.runners.get(b.id)
            on = bool(r and r.running)
            dot, dot_style = ("●", RED if r.live else GREEN) if on else ("○", FAINT)
            out.append("▸ " if sel else "  ", style=f"bold {ORANGE}")
            out.append(f"{dot} ", style=dot_style)
            out.append(f"{b.name[:name_w]:<{name_w}} ", style=f"bold {ORANGE}" if sel else TEXT if not b.error else RED)
            s = t.bot_scan.get(b.id)
            if b.error or (s and s.error):
                out.append(f"{(b.error or s.error)[:34]}", style=RED if b.error else DIM)
            elif s:
                mark, colour = VIEW_MARK.get(s.view, ("·", DIM))
                out.append(f"{mark} {s.view:<9}", style=colour)
                out.append(f"{fmt.price(s.median):>8}  ", style=TEXT)
                out.append(("LIVE" if r.live else "PAPER") if on else "", style=RED if on and r.live else GREEN)
            out.append("\n")
        return out


def selected(t: Terminal) -> Bot | None:
    """The bot the list is pointing at."""
    shown = visible(t) if t.bots else []
    return shown[max(0, min(t.bot_cur, len(shown) - 1))] if shown else None


def detail_title(t: Terminal) -> str:
    b = selected(t)
    n = len(t.bot_closes)
    if b is None:
        return "FORECAST"
    return f"FORECAST · {b.name} · {n} close{'' if n == 1 else 's'} ahead" if n else f"FORECAST · {b.name}"


def log_title(t: Terminal, r) -> str:
    """The runner's state, on the border of the pane that holds its log."""
    state = ("● LIVE · real sats" if r.live else "● ON PAPER") if r.running else "○ stopped"
    return (f"{state} · {r.name} · {r.series} · budget {fmt.sats(r.cfg.bankroll_sats)} · at risk {fmt.sats(r.open_cost)}"
            f" · bought {fmt.sats(r.paid_sats)} · sold {fmt.sats(r.sold_sats)} · {r.trades} trade{'' if r.trades == 1 else 's'}"
            f" · {r.cycles} cycle{'' if r.cycles == 1 else 's'}")


class BotLogPane(Widget):
    """What the running bot is doing, under its forecast: what it holds, then its log."""
    can_focus = False
    DEFAULT_CSS = "BotLogPane { text-wrap: nowrap; text-overflow: ellipsis; }"

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t, out = self.t, Text(no_wrap=True, overflow="ellipsis")
        b = selected(t)
        r = t.runners.get(b.id) if b else None
        if b is None or r is None:
            return out
        h = max(self.size.height - 2, 3)
        pos = r.ledger.positions(b.id, r.mode)
        for end, asset, p_lo, p_hi, contracts, cost in pos[:4]:
            out.append(f"  {fmt.question(asset or t.asset, end):<24}{fmt.span(p_lo, p_hi):<18}"
                       f"{fmt.contracts(contracts):>8} × cost {fmt.sats(cost)}\n", style=DIM)
        if len(pos) > 4:
            out.append(f"  … and {len(pos) - 4} more positions\n", style=FAINT)
        if pos:
            out.append("\n")
        for line in list(r.log)[-max(h - out.plain.count("\n"), 1):]:
            hot = any(x in line for x in ("BUY", "SELL", "filled"))
            out.append(f"  {line}\n", style=TEXT if hot else RED if "failed" in line or "rejected" in line else DIM)
        return out


FIXED_LINES = 17                # name, family, two of description, blank, question, expects, blank, the ladder's header,
                                # blank and legend, blank, the chart's header, axis and blank, the two run lines
CHART_MAX_ROWS = 18


def layout(height: int) -> tuple[int, int]:
    """(ladder rows, chart rows) for a pane `height` tall inside its border: the ladder takes a little over half of
    what is spare, up to 13 rows, the chart the rest up to 18. A pane too short for both keeps the ladder alone."""
    spare = height - FIXED_LINES
    if spare < LADDER_MIN_ROWS + CHART_MIN_ROWS:
        return max(min(spare + 1, LADDER_ROWS), 5), 0                         # the tab hint takes two lines, not three
    ladder_rows = max(min(round(spare * 0.55), LADDER_ROWS, spare - CHART_MIN_ROWS), LADDER_MIN_ROWS)
    return ladder_rows, min(spare - ladder_rows, CHART_MAX_ROWS)


class BotDetailPane(Widget):
    """One bot, top to bottom: what it is, what it expects of one close, that expectation against the market's
    prices, its path through every close ahead after the candles it read, and how to run it. Nothing else. While
    walking the closes (tab), the chart marks the close the ladder shows and the header names it."""
    can_focus = False
    DEFAULT_CSS = "BotDetailPane { text-wrap: nowrap; text-overflow: ellipsis; }"

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t, out = self.t, Text(no_wrap=True, overflow="ellipsis")
        b = selected(t)
        if b is None:
            return out
        w = max(self.size.width - 2, 30)
        closes = t.bot_closes
        cur = max(0, min(t.bot_when, len(closes) - 1))
        v = closes[cur] if closes else None
        s = t.ahead_of(b, v)
        walking = t.bot_pane == 1
        picture, chart_rows = layout(self.size.height - 2)

        mark, colour = VIEW_MARK.get(s.view if s and not s.error else "", ("", DIM))
        out.append(f"  {b.name}", style=f"bold {TEXT}")
        if mark:
            out.append(f"   {mark} {s.view}", style=colour)
        out.append(f"\n  {b.family} · {b.blurb}", style=DIM)
        out.append("   i", style=f"bold {ORANGE}")
        out.append(" how it works\n", style=DIM)
        idea = textwrap.wrap(" ".join((b.description or "").split()), max(w - 4, 20), max_lines=2, placeholder=" …")
        for line in (idea + ["", ""])[:2]:
            out.append(f"  {line}\n", style=FAINT)
        out.append("\n")

        if b.error:
            out.append(f"  {b.error}\n\n", style=RED)
        elif not closes:
            out.append("  reading the closes ahead…\n\n", style=DIM)
        else:
            out.append(f"  {fmt.question(t.asset, v.row.end_time_utc)}", style=f"bold {TEXT}")
            out.append(f"   closes in {fmt.countdown(v.row.end_time_utc)}   price now {fmt.price(t.spot)}\n", style=DIM)
            if s is None:
                out.append("  working out what the bot expects of this close…\n\n", style=DIM)
            elif s.error:
                out.append(f"  No picture for this close: {s.error}\n\n", style=DIM)
            else:
                out.append(f"  Bot expects {fmt.price(s.median)}", style=f"bold {ORANGE}")
                out.append(f"   8 in 10 chance between {fmt.price(s.low)} and {fmt.price(s.high)}\n\n", style=DIM)
                out.append_text(ladder(s.probs, v.bins, t.spot, w, picture, market=v.raw))
            out.append("\n")
            if chart_rows:
                now, bars = time.time(), t.bot_history()
                hist_h = max(int((int(now // 3600) * 3600 - bars[0].t) // 3600), 0) if bars else 0
                out.append_text(chart_header(columns(closes, hist_h, now, w - GUTTER - 2)[0], len(closes), walking))
                out.append("\n")
                out.append_text(chart(bars, closes, t.bot_pictures(b), cur, t.spot, now, w, chart_rows))
                out.append("\n")
            elif not walking:
                out.append("  tab", style=f"bold {ORANGE}")
                out.append(f"  see the next {len(closes)} closes\n\n", style=DIM)

        r = t.runners.get(b.id)
        if r and r.running:
            out.append(" x ", style=f"bold #000000 on {ORANGE}")
            out.append(f"  stop this bot   {'LIVE · real sats' if r.live else 'on paper'} · budget {fmt.sats(r.cfg.bankroll_sats)}",
                       style=f"bold {TEXT}")
            out.append("   X stops every bot\n", style=DIM)
        else:
            out.append(" enter ", style=f"bold #000000 on {ORANGE}")
            out.append(f"  run this bot · budget {fmt.sats(t.bot_budget(b.id))}", style=f"bold {TEXT}")
            out.append("   e change budget\n", style=DIM)
            if t.api.authenticated:
                out.append(f"  Trades real sats through your API key {t.key_mask} once you type LIVE, otherwise on paper.\n",
                           style=DIM)
            else:
                out.append("  No API key loaded: practice on paper.   ", style=DIM)
                out.append("L", style=f"bold {ORANGE}")
                out.append(" log in to trade real sats\n", style=DIM)
        return out


def rule_title(t: Terminal) -> str:
    running = [r for r in t.runners.values() if r.running]
    if not running:
        return "BOTS · runs on this computer · nothing running"
    live = sum(1 for r in running if r.live)
    return f"BOTS · {len(running)} running" + (f" · {live} LIVE" if live else " · dry run")

