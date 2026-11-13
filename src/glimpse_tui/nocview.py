"""The monitor screen: the fleet of bots, the wall of markets they trade, what they hold and what they just did.

Four panes. FLEET, top left: one row per bot (`noc.Unit`), with what it holds at cost, what that would fetch now, and
the difference; every bot together on the first row, the account itself on the last. WALL, top right: one tile per
series, drawn like the site's home page: the candles left of NOW, right of it the market's forecast (green above spot,
red below, brighter where likelier), and over it in white every range the selected bot holds, close by close; a range
it bought in the last two minutes is orange, one it sold is cyan. POSITIONS, bottom left: the selected bot's positions
in the selected series, a row a close, like the site's portfolio page. TAPE, bottom right: every fill of the selection
as it happens, and beside it the bots' own logs.

j k choose a bot, h l a series, m shows that series alone, - + change how far ahead the tiles look.
"""
from __future__ import annotations

import math
import time
from functools import lru_cache
from types import SimpleNamespace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widget import Widget

from . import charts, fmt
from .botsview import columns, pack, round_step
from .chrome import CHIP, INK
from .heatmap import axis_labels
from .noc import ACCOUNT, ALL, FLASH_S, HORIZONS, QUIET_S, Monitor, Unit, candles, is_desk
from .theme import DIM, FAINT, GREEN, ORANGE, RED, RULE, TEXT

if TYPE_CHECKING:
    from .api import Batch, Book
    from .app import Terminal

LEFT_WIDTH = 78
LEFT_MIN_TERMINAL = 132         # a narrower terminal gives the left column 45% of its width instead
TAPE_HEIGHT = 11
TILE_MIN_W, TILE_MIN_H = 46, 10
GUTTER = 9                      # price labels on the right of a tile
SOLD = "#54bee8"
HELD = "#ffffff"
UP = ((0.0, (5, 34, 22)), (0.45, (9, 84, 54)), (0.8, (12, 140, 90)), (1.0, (70, 200, 140)))
DOWN = ((0.0, (40, 9, 14)), (0.45, (104, 22, 36)), (0.8, (170, 40, 60)), (1.0, (230, 96, 112)))
CLOUD_FLOOR = 0.05              # rows holding under 5% of their column's likeliest row stay dark
SPARKS = "▁▂▃▄▅▆▇█"
KIND = {"desk": "desk", "runner": "bot", "manager": "manager", "taker": "taker", "triangle": "triangle", "arbitrage": "arb",
        "idle": "idle", ACCOUNT: "account", ALL: ""}


@lru_cache(maxsize=256)
def _shade(level: int, up: bool, truecolor: bool) -> str:
    return charts.snap(charts.ramp(UP if up else DOWN, level / 15))


def shade(t: float, up: bool) -> str:
    """A forecast cell's background: green above spot, red below, `t` of the way from least to most likely."""
    return _shade(int(min(max(t, 0.0), 1.0) * 15), up, charts.truecolor)


def spark(values: list[float], width: int) -> str:
    """`values` as a sparkline `width` characters wide: the last `width` of them, or all of them thinned to fit."""
    if not values or width <= 0:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[min(int((i + 1) * step) - 1, len(values) - 1)] for i in range(width)]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARKS[0] * len(values)
    return "".join(SPARKS[min(int((v - lo) / (hi - lo) * 8), 7)] for v in values)


def _line(width: int, *parts: tuple[str, str]) -> Text:
    out = Text(no_wrap=True, overflow="ellipsis")
    for s, style in parts:
        if s:
            out.append(s, style=style)
    out.truncate(width, overflow="ellipsis", pad=True)
    return out


def _window(cursor: int, n: int, height: int) -> int:
    return max(0, min(cursor - height // 2, n - height)) if n > height else 0


def _pnl(x: float) -> str:
    return GREEN if x >= 0 else RED


def sats(n: float, signed: bool = False) -> str:
    """Sats for a table column: whole below a million, `₿1.16m` above, so a wallet-sized figure keeps to its column."""
    if abs(n) < 1e6:
        return fmt.sats(n, signed)
    sign = fmt.MINUS if n < 0 else "+" if signed else ""
    return f"{sign}₿{abs(n) / 1e6:.2f}m" if abs(n) < 1e7 else f"{sign}₿{abs(n) / 1e6:.1f}m"


def age(seconds: float) -> str:
    s = max(int(seconds), 0)
    return f"{s}s" if s < 100 else f"{s // 60}m" if s < 6000 else f"{s // 3600}h"


def selected_series(t: Terminal) -> Batch | None:
    m, series = t.noc, t.noc.series(t)
    if not series:
        return None
    m.series_cur = max(0, min(m.series_cur, len(series) - 1))
    return series[m.series_cur]


# ── the fleet ───────────────────────────────────────────────

def state_mark(u: Unit, now: float) -> tuple[str, str]:
    """The glyph before a bot's name: ● running (red on real sats, green on paper), ◐ running but silent for a while,
    ○ stopped, ◌ holding positions with nothing running it, ◆ the fleet, ◇ the account."""
    if u.kind == ALL:
        return "◆", ORANGE
    if u.kind == ACCOUNT:
        return "◇", TEXT
    if u.kind == "idle":
        return "◌", FAINT
    if not u.running:
        return "○", FAINT
    if u.last_at and now - u.last_at > QUIET_S:
        return "◐", ORANGE
    return "●", RED if u.live else GREEN


def totals(t: Terminal, width: int) -> list[Text]:
    """Real sats and paper sats never share a line: what the live bots hold and are up, then the paper ones, then the
    wallet and the state of the market stream, then the latest thing that went wrong."""
    m, out = t.noc, []
    bots_ = [u for u in m.units if u.is_bot]
    for live in (True, False):
        us = [u for u in bots_ if u.live == live]
        if not us:
            continue
        cost, value, taken = sum(u.cost for u in us), sum(u.value for u in us), sum(u.taken for u in us)
        on = sum(1 for u in us if u.running)
        out.append(_line(width, (" LIVE  " if live else " PAPER ", f"bold {INK} on {RED if live else GREEN}"),
                         (f" {on}/{len(us)} on", f"bold {TEXT}"), ("  cost ", FAINT), (sats(cost), TEXT),
                         ("  value ", FAINT), (sats(value), TEXT), (f"  {sats(value - cost, signed=True)}", f"bold {_pnl(value - cost)}"),
                         ("  taken ", FAINT), (sats(taken, signed=True), _pnl(taken)),
                         (f"  {sum(u.trades for u in us):,} trades", DIM)))
    stream = m.streaming(t)
    feed = ("stream up", GREEN) if stream else ("reading the markets…", FAINT) if stream is None else ("stream down, polling", ORANGE)
    parts: list[tuple[str, str]] = [(" MARKET ", f"bold {TEXT} on {CHIP}"), (f" {feed[0]}", feed[1])]
    n = sum(len(w.books) for w in m.sources(t))
    if n:
        parts.append((f" · {n:,} closes of {len(m.series(t))} series kept current", DIM))
    if not t.api.authenticated:
        parts.append(("   read-only: L logs in", FAINT))
    out.append(_line(width, *parts))
    alert = next(((u.name, u.alert) for u in bots_ if u.alert), None)
    if alert:
        out.append(_line(width, (" ALERT  ", f"bold {INK} on {ORANGE}"), (f" {alert[0]}: ", f"bold {ORANGE}"), (alert[1], RED)))
    return out


FLEET_COLS = (("where", 10), ("cost", 9), ("value", 9), ("p&l", 9), ("trades", 7), ("last", 5))


def fleet_cols(width: int) -> tuple[int, list[tuple[str, int]]]:
    """(the name column's width, the number columns that fit after it)."""
    keep = list(FLEET_COLS)
    for drop in ("last", "where", "trades"):
        if 5 + 14 + 6 + sum(w for _, w in keep) <= width:
            break
        keep = [c for c in keep if c[0] != drop]
    return max(min(width - 5 - 6 - sum(w for _, w in keep), 26), 10), keep


def unit_line(u: Unit, sel: bool, focused: bool, now: float, width: int) -> Text:
    name_w, cols = fleet_cols(width)
    glyph, colour = state_mark(u, now)
    out = Text(no_wrap=True, overflow="crop")
    out.append(" ▸ " if sel else "   ", style=f"bold {ORANGE}")
    out.append(f"{glyph} ", style=colour)
    name = u.name if len(u.name) <= name_w - 1 else u.name[:name_w - 2] + "…"
    out.append(f"{name:<{name_w}}", style=f"bold {ORANGE}" if sel and focused else f"bold {TEXT}" if sel or u.kind == ALL else TEXT)
    mode = "" if u.kind == ALL else "LIVE" if u.live else "paper"
    out.append(f"{mode:<6}", style=(RED if u.live else GREEN) if u.running or u.kind == ACCOUNT else FAINT)
    cells = {"where": u.where or KIND.get(u.kind, ""), "cost": sats(u.cost), "value": sats(u.value),
             "p&l": sats(u.pnl, signed=True), "trades": f"{u.trades:,}" if u.is_bot or u.kind == ALL else "",
             "last": age(now - u.last_at) if u.running and u.last_at else ""}
    for name_, w in cols:
        txt = cells[name_]
        style = _pnl(u.pnl) if name_ == "p&l" else DIM if name_ in ("where", "trades", "last") else TEXT
        out.append(f"{txt:<{w}}" if name_ == "where" else f"{txt:>{w}}", style=style)
    out.truncate(width, overflow="crop", pad=True)
    if sel:
        out.stylize(f"on {CHIP}")
    return out


def detail(t: Terminal, u: Unit, width: int) -> list[Text]:
    """Two lines about the selected row: what it is and what it did this session, then its P&L over the time watched."""
    m, r = t.noc, u.runner
    if u.kind == ALL:
        n = sum(1 for x in m.units if x.is_bot)
        head = (f" {n} bot{'' if n == 1 else 's'} · {len(u.holdings):,} positions in {u.closes:,} closes"
                f"\n this session: bought {fmt.sats(u.paid)} · sold {fmt.sats(u.sold)}") if n else \
            " No bot is running. B opens the zoo: enter runs a bot there,\n R lets it manage your portfolio, D runs it as a desk."
    elif u.kind == ACCOUNT:
        s = t.summary
        head = (f" {u.note}\n {len(u.holdings):,} positions in {u.closes:,} closes"
                + (f" · live {s.active} · pending {s.pending} · resolved {s.resolved}" if s else ""))
    elif r is None:
        head = f" {u.note}\n {len(u.holdings):,} positions in {u.closes:,} closes · B finds the bot, enter there runs it"
    else:
        from .botsview import progress

        what = {"desk": f"a desk across {r.series}", "manager": f"manages your {r.series} portfolio", "taker": "takes profit on your portfolio",
                "triangle": f"the triangle across {r.series}", "arbitrage": f"covers across {r.series}"}.get(u.kind, f"trades {r.series}")
        head = (f" {what}" + ("" if u.funds else f" · {'bankroll' if is_desk(r) else 'budget'} {fmt.sats(u.budget)}") + "\n"
                f" {progress(r)} · bought {fmt.sats(u.paid)} · sold {fmt.sats(u.sold)} ({fmt.sats(u.taken, signed=True)} on cost)")
    out = [_line(width, (x, DIM)) for x in head.split("\n")]
    out += funds_lines(t, u, width)
    hist = [v for _, v in m.history.get(u.id, ())]
    if len(hist) >= 2 and u.funds is None:
        since = time.strftime("%H:%M", time.gmtime(m.history[u.id][0][0]))
        total = u.pnl + u.taken
        out.append(_line(width, (" P&L ", f"bold {TEXT} on {CHIP}"), (f" {spark(hist, max(width - 44, 8))} ", _pnl(total)),
                         (fmt.sats(total, signed=True), f"bold {_pnl(total)}"), (f" open + taken, watched since {since}", FAINT)))
    return out


def funds_lines(t: Terminal, u: Unit, width: int) -> list[Text]:
    """The selected row's money: the bankroll and how much of it is at work, then its equity over the time recorded (kept
    across sessions), with what trading has made on the capital put in."""
    f = u.funds
    if f is None:
        return []
    if u.kind == ACCOUNT:
        first = _line(width, (" FUNDS  ", f"bold {TEXT} on {CHIP}"), (" wallet ", FAINT), (sats(f.free), f"bold {TEXT}"),
                      (" free  ·  positions ", FAINT), (sats(f.value), TEXT), ("  ·  equity ", FAINT), (sats(f.equity), f"bold {TEXT}"))
    else:
        whole = is_desk(u.runner) and u.live and f.setting <= 0
        first = _line(width, (" FUNDS  ", f"bold {TEXT} on {CHIP}"), (" bankroll ", FAINT), (sats(f.bankroll), f"bold {TEXT}"),
                      (" (wallet)" if whole else "", FAINT), ("  deployed ", FAINT), (f"{sats(f.deployed)} {f.used:.0%}", TEXT),
                      ("  free ", FAINT), (sats(f.free), TEXT), ("  b adds", FAINT))
    out = [first]
    pts = t.noc.equity.points(u.key)
    net = t.noc.equity.net(u.key, f.equity)
    if net is not None:
        made, capital, t0 = net
        since = time.strftime("%d %b %H:%M", time.gmtime(t0))
        pct = f" ({made / capital:+.1%})" if capital > 0 else ""
        out.append(_line(width, (" EQUITY ", f"bold {TEXT} on {CHIP}"),
                         (f" {spark([p[1] for p in pts] + [f.equity], max(width - 62, 8))} ", _pnl(made)),
                         (sats(f.equity), f"bold {TEXT}"), (f"  {sats(made, signed=True)}{pct}", f"bold {_pnl(made)}"),
                         (f" since {since}", FAINT)))
    return out


class FleetPane(Widget):
    can_focus = False
    DEFAULT_CSS = "FleetPane { text-wrap: nowrap; text-overflow: clip; }"

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t, m = self.t, self.t.noc
        width, height = max(self.size.width - 2, 30), max(self.size.height - 2, 4)
        now = time.time()
        lines = totals(t, width)
        lines.append(Text(""))
        name_w, cols = fleet_cols(width)
        head = Text(no_wrap=True, overflow="crop")
        head.append(f"     {'BOT':<{name_w}}{'':<6}", style=FAINT)
        for name, w in cols:
            head.append(f"{name.upper():<{w}}" if name == "where" else f"{name.upper():>{w}}", style=FAINT)
        lines.append(head)
        u = m.unit
        foot = detail(t, u, width) if u else []
        room = max(height - len(lines) - len(foot) - 1, 1)
        start = _window(m.cur, len(m.units), room)
        for i, x in enumerate(m.units[start:start + room], start):
            lines.append(unit_line(x, i == m.cur, m.focus == 0, now, width))
        lines.append(Text(""))
        lines += foot
        return Text("\n", no_wrap=True).join(lines[:height])


def fleet_title(t: Terminal) -> str:
    bots_ = [u for u in t.noc.units if u.is_bot]
    on = [u for u in bots_ if u.running]
    live = sum(1 for u in on if u.live)
    if not bots_:
        return "FLEET · no bots yet"
    return f"FLEET · {len(on)} of {len(bots_)} bot{'' if len(bots_) == 1 else 's'} running" + (f" · {live} LIVE" if live else "")


def fleet_height(t: Terminal, available: int) -> int:
    """Rows the fleet pane wants, borders included: the totals, the table and the two lines about the selection."""
    return max(min(len(t.noc.units) + 13, available * 3 // 5), 9)


# ── the wall ────────────────────────────────────────────────

_EDGES: dict[tuple[int, float, float], object] = {}


def _edges(book: Book):
    """The ladder's n + 1 edges as an array. Every close of a series shares one ladder, so it is built once."""
    import numpy as np

    key = (len(book.bins), book.bins[0][0], book.bins[-1][1])
    got = _EDGES.get(key)
    if got is None:
        if len(_EDGES) > 64:
            _EDGES.clear()
        got = _EDGES[key] = np.array([book.bins[0][0]] + [hi for _, hi in book.bins], dtype=float)
    return got


def _belief(book: Book):
    """The book's probabilities with the subsidy floor taken off (`pricing.signal`, as an array); None for a close nobody
    has traded, which has no shape to draw."""
    import numpy as np

    p = np.asarray(book.probs, dtype=float)
    if len(p) < 4:
        return None
    lifted = np.maximum(p - np.partition(p, len(p) // 4)[len(p) // 4], 0.0)
    s = float(lifted.sum())
    return lifted / s if s > 1e-12 else None


def _rows(p, edges, hi: float, step: float, rows: int):
    """The share of `p` (one number per range) in each chart row, row 0 on top: each range's share spread over its width."""
    import numpy as np

    cum = np.concatenate(([0.0], np.cumsum(p)))
    at = np.interp(hi - step * np.arange(rows + 1), edges, cum)
    return at[:-1] - at[1:]


def label(x: float) -> str:
    return f"{x / 1e6:,.2f}m" if abs(x) >= 1e6 else fmt.kprice(x) if abs(x) >= 10_000 else fmt.price(x)


def span(lo: float, hi: float) -> str:
    """A held range to the dollar: `73,600–73,800`, or in millions where the price is one (`5.400–5.410m`)."""
    return f"{lo / 1e6:.3f}–{hi / 1e6:.3f}m" if abs(hi) >= 1e6 else fmt.span(lo, hi)


def exact(x: float) -> str:
    """A price in full where it fits a tile's gutter: spot, which the eye reads to the dollar."""
    return f"{x / 1e6:,.3f}m" if abs(x) >= 1e6 else fmt.price(x)


def tile(t: Terminal, batch: Batch, u: Unit | None, width: int, height: int, sel: bool, now: float) -> list[Text]:
    """One series as `height` lines of `width`: a header (the series, spot, the market's chance of up and down at the end
    of the window, what the selection holds here), the chart, and the time axis."""
    import numpy as np

    m = t.noc
    books = m.closes(t, batch.batch_id, now)
    span = HORIZONS[m.span]
    shown = [b for b in books if not span or b.end_time_utc - now <= span * 3600] or books[:1]
    feed = t.feed(batch.asset or "BTC")
    spot = float(getattr(feed, "spot", 0.0) or 0.0)
    topics = {b.topic_id for b in books}
    held = m.held(u, topics)
    mine = [h for hs in held.values() for h in hs]
    cost, value = sum(h.cost for h in mine), sum(h.value for h in mine)

    head = Text(no_wrap=True, overflow="crop")
    head.append(f" {batch.short} ", style=f"bold {INK} on {ORANGE}" if sel else f"bold {TEXT} on {CHIP}")
    if spot > 0:
        head.append(f" {exact(spot)}", style=f"bold {TEXT}")
    beliefs = {b.topic_id: _belief(b) for b in shown}
    last = next((b for b in reversed(shown) if beliefs[b.topic_id] is not None), None)
    if last is not None and spot > 0:
        e = _edges(last)
        up = float(beliefs[last.topic_id][(e[:-1] + e[1:]) / 2 >= spot].sum())
        head.append(f"  ▲{up:.0%}", style=GREEN)
        head.append(f" ▼{1 - up:.0%}", style=RED)
    if mine:
        head.append(f"  holds {fmt.sats(cost)}", style=TEXT)
        head.append(f" {fmt.sats(value - cost, signed=True)}", style=f"bold {_pnl(value - cost)}")
        head.append(f" · {len(held)}/{len(books)}", style=FAINT)
    else:
        head.append(f"  {len(books)} closes", style=FAINT)
    head.truncate(width - 1, overflow="ellipsis")
    head.truncate(width, pad=True)
    lines = [head]
    rows, chart_w = height - 2, width - 1 - GUTTER
    if rows < 3 or chart_w < 10 or not shown:
        return (lines + [Text(" " * width)] * height)[:height]

    bars = candles(feed)
    t0 = int(now // 3600) * 3600
    hist_h = max(int((t0 - bars[0].t) // 3600), 0) if bars else 0
    hpc, _, fc = columns([SimpleNamespace(row=shown[-1])], 0, now, max(chart_w * 11 // 20, 8))
    fc = max(fc, 1)
    k = max(1, min(6, (chart_w * 11 // 20) // fc))     # a short window is stretched: the forecast takes about half the tile
    now_x = max(chart_w - 1 - fc * k, 0)
    hc = min(hist_h // hpc, now_x)                      # history fills what is left, at one column per `hpc` hours
    hist = pack(bars, t0, hpc, hc)

    lows, highs = [c.l for c in hist if c], [c.h for c in hist if c]
    if spot > 0:
        lows.append(spot)
        highs.append(spot)
    formed = [beliefs[b.topic_id] for b in shown if beliefs[b.topic_id] is not None and len(beliefs[b.topic_id]) == len(shown[0].bins)]
    if formed:                                          # the middle 96% of every close's belief together: one stray trade far out
        e, cum = _edges(shown[0]), np.cumsum(np.mean(formed, axis=0))      # on a thin close does not flatten the whole picture
        lows.append(float(e[min(int(np.searchsorted(cum, 0.02)), len(cum) - 1)]))
        highs.append(float(e[min(int(np.searchsorted(cum, 0.98)) + 1, len(cum))]))
    if not lows:
        lows, highs = [shown[0].bins[0][0]], [shown[0].bins[-1][1]]
    lo, hi = min(lows), max(highs)
    reach = (hi - lo) * 0.5                             # a position a little outside the picture widens it; a far tail does not
    near = [h for h in mine if h.hi > h.lo and h.lo >= lo - reach and h.hi <= hi + reach]
    lo, hi = min([lo] + [h.lo for h in near]), max([hi] + [h.hi for h in near])
    pad = max((hi - lo) * 0.06, abs(spot) * 0.002, 1e-9)
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / rows
    row_of = lambda p: min(max(int((hi - p) // step), 0), rows - 1)        # noqa: E731
    rule_step = round_step(3 * step)
    ruled = {}
    for r in range(rows):
        p = lo + (rows - r) * step
        if math.floor(p / rule_step) != math.floor((p - step) / rule_step):
            ruled[r] = math.floor(p / rule_step) * rule_step
    spot_r = row_of(spot) if spot > 0 else -1

    cells: list[dict[int, tuple[str, str]]] = [{} for _ in range(rows)]
    for i, c in enumerate(hist):
        x = now_x - 1 - i
        if c is None or x < 0:
            continue
        colour = GREEN if c.c >= c.o else RED
        for r in range(row_of(c.h), row_of(c.l) + 1):
            top, bottom = hi - r * step, hi - (r + 1) * step
            cells[r][x] = ("█" if min(c.o, c.c) < top and max(c.o, c.c) >= bottom else "│", colour)

    col_of = lambda end: max(min(int((end - now) / 3600.0 // hpc), fc - 1), 0)     # noqa: E731
    ahead: dict[int, list[Book]] = {}
    for b in shown:
        ahead.setdefault(col_of(b.end_time_utc), []).append(b)
    drawn: dict[int, tuple] = {}                        # column -> (the forecast's share per row, its median's row, contracts per row)
    for col, bs in ahead.items():
        mass = mid = got = None
        ps = [beliefs[b.topic_id] for b in bs if beliefs[b.topic_id] is not None and len(beliefs[b.topic_id]) == len(bs[0].bins)]
        if ps:
            mean = ps[0] if len(ps) == 1 else np.mean(ps, axis=0)
            mass = _rows(mean, _edges(bs[0]), hi, step, rows)
            mid = row_of(float(_edges(bs[0])[min(int(np.searchsorted(np.cumsum(mean), 0.5)), len(mean) - 1)]))
        for b in bs:
            hs = held.get(b.topic_id)
            if not hs:
                continue
            at = {o: i for i, o in enumerate(b.option_ids)}
            v = np.zeros(len(b.bins))
            for h in hs:
                if h.option in at:
                    v[at[h.option]] += h.contracts
                elif h.hi > h.lo:                       # the account's row names a range by its prices
                    v[min(max(int(np.searchsorted(_edges(b), (h.lo + h.hi) / 2)) - 1, 0), len(v) - 1)] += h.contracts
            part = _rows(v, _edges(b), hi, step, rows)
            got = part if got is None else got + part
        drawn[col] = (mass, mid, got)
    top_stake = max((float(g.max()) for _, _, g in drawn.values() if g is not None), default=0.0)
    shades: dict[tuple[int, int], str] = {}
    src = None
    for col in range(fc - 1, -1, -1):                   # a column between two closes shows the close it runs up to
        src = drawn.get(col, src)
        if src is None:
            continue
        mass, mid, got = src
        for x in range(now_x + 1 + col * k, min(now_x + 1 + (col + 1) * k, chart_w)):
            if mass is not None:
                peak = float(mass.max()) or 1.0
                for r in range(rows):
                    if mass[r] / peak >= CLOUD_FLOOR:
                        shades[r, x] = shade(mass[r] / peak, spot <= 0 or hi - (r + 0.5) * step >= spot)
                        cells[r][x] = (" ", "")
                cells[mid][x] = ("─", f"bold {TEXT}")
            if got is not None and top_stake > 0:
                for r in range(rows):
                    if got[r] > 1e-9:
                        share = got[r] / top_stake
                        cells[r][x] = ("█" if share >= 0.6 else "▓" if share >= 0.3 else "▒" if share >= 0.1 else "░", f"bold {HELD}")
    for f in m.flashes(u, now):
        if f.topic in topics and lo < (f.lo + f.hi) / 2 < hi and f.end - now <= (span * 3600 if span else 1e18):
            for x in range(now_x + 1 + col_of(f.end) * k, min(now_x + 1 + (col_of(f.end) + 1) * k, chart_w)):
                cells[row_of((f.lo + f.hi) / 2)][x] = ("█", f"bold {ORANGE}") if f.contracts > 0 else ("▒", f"bold {SOLD}")
    for (r, x), bg in shades.items():
        ch, style = cells[r][x]
        cells[r][x] = (ch, f"{style} on {bg}".strip())
    for r in range(rows):
        cells[r][now_x] = ("│", f"bold {ORANGE}")

    for r in range(rows):
        base_ch = "─" if r in ruled else " "
        line = Text(no_wrap=True, overflow="crop")
        at = 0
        for x in sorted(cells[r]):
            if x >= chart_w:
                break
            if x > at:
                line.append(base_ch * (x - at), style=RULE)
            line.append(*cells[r][x])
            at = x + 1
        if at < chart_w:
            line.append(base_ch * (chart_w - at), style=RULE)
        if r == spot_r:
            line.append(f"◂{exact(spot):>{GUTTER - 2}} ", style=f"bold {INK} on {ORANGE}")
        elif r in ruled:
            line.append(f"┤{label(ruled[r]):>{GUTTER - 2}} ", style=DIM)
        else:
            line.append("│" + " " * (GUTTER - 1), style=FAINT)
        line.truncate(width, overflow="crop", pad=True)
        lines.append(line)
    spans = [(now_x - 1 - i, t0 - (i + 1) * hpc * 3600, hpc * 3600) for i in range(hc)]
    spans += [(now_x + 1 + c * k + j, int(now + (c + j / k) * hpc * 3600), max(hpc * 3600 // k, 1)) for c in range(fc) for j in range(k)
              if now_x + 1 + c * k + j < chart_w]
    axis = axis_labels(spans, chart_w, hpc < 24, now_x)
    axis.truncate(width, overflow="crop", pad=True)
    lines.append(axis)
    return (lines + [Text(" " * width)] * height)[:height]


def grid(n: int, width: int, height: int) -> tuple[int, int, int, int]:
    """(tiles across, rows of tiles on screen, a tile's width, a tile's height) for `n` series."""
    across = max(1, min(n, width // TILE_MIN_W))
    need = math.ceil(n / across)
    down = max(1, min(need, height // TILE_MIN_H))
    return across, down, width // across, height // down


class WallPane(Widget):
    can_focus = False
    DEFAULT_CSS = "WallPane { text-wrap: nowrap; text-overflow: clip; }"

    def __init__(self, id: str) -> None:
        super().__init__(id=id)
        self._cache: dict[str, tuple[tuple, list[Text]]] = {}

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def _tile(self, batch: Batch, u: Unit | None, w: int, h: int, sel: bool, now: float) -> list[Text]:
        t, m = self.t, self.t.noc
        feed = t.feed(batch.asset or "BTC")
        key = (w, h, sel, u.id if u else "", m.version, m.span, len(m.fills), int(now // 5), charts.truecolor, getattr(feed, "spot", 0.0),
               tuple((x.messages, max(x.listed_at.values(), default=0.0)) for x in m.sources(t)), len(u.holdings) if u else 0)
        got = self._cache.get(batch.batch_id)
        if got is None or got[0] != key:
            got = self._cache[batch.batch_id] = (key, tile(t, batch, u, w, h, sel, now))
        return got[1]

    def render(self) -> Text:
        t, m = self.t, self.t.noc
        width, height = max(self.size.width - 2, 20), max(self.size.height - 2, 4)
        series = m.series(t)
        if not series:
            w = m.watch
            last = w.log[-1] if w is not None and w.log else ""
            return Text(f"\n  Reading every open close of every series…  {last}" if t.batches else "\n  Listing the series…", style=DIM)
        cur, u, now = selected_series(t), m.unit, time.time()
        if m.zoom:
            return Text("\n", no_wrap=True).join(self._tile(cur, u, width, height, True, now))
        across, down, tw, th = grid(len(series), width, height)
        first = _window(m.series_cur // across, math.ceil(len(series) / across), down)
        lines: list[Text] = []
        for row in range(first, first + down):
            tiles = [self._tile(b, u, tw, th, b is cur, now) for b in series[row * across:(row + 1) * across]]
            for i in range(th):
                line = Text(no_wrap=True, overflow="crop")
                for tl in tiles:
                    line.append_text(tl[i])
                lines.append(line)
        return Text("\n", no_wrap=True).join(lines[:height])


def wall_title(t: Terminal) -> str:
    m, u = t.noc, t.noc.unit
    span = HORIZONS[m.span]
    b = selected_series(t)
    who = "every bot" if u is None or u.kind == ALL else "your account" if u.kind == ACCOUNT else u.name
    what = f"{b.short} alone" if m.zoom and b else "every series"
    return f"MARKETS · {what} · what {who} holds over the market's forecast · {f'next {span}h' if span else 'every open close'}"


def wall_legend() -> Text:
    out = Text(no_wrap=True)
    for txt, style in ((" ", f"on {shade(0.8, True)}"), (" above spot ", FAINT), (" ", f"on {shade(0.8, False)}"),
                       (" below: the market's forecast  ", FAINT),
                       ("░▒▓█", f"bold {HELD}"), (" held  ", FAINT), ("█", f"bold {ORANGE}"), (" just bought  ", FAINT),
                       ("▒", f"bold {SOLD}"), (" just sold  ", FAINT), ("─", f"bold {TEXT}"), (" median ", FAINT)):
        out.append(txt, style=style)
    return out


# ── positions ───────────────────────────────────────────────

POS_COLS = (("close", 14), ("in", 9), ("held", 18), ("range", 15), ("legs", 5), ("cost", 9), ("value", 9), ("p&l", 9))


def pos_cols(width: int) -> list[tuple[str, int]]:
    keep = list(POS_COLS)
    for drop in ("legs", "range", "in", "held"):
        if 2 + sum(w for _, w in keep) <= width:
            break
        keep = [c for c in keep if c[0] != drop]
    return keep


def range_bar(lo: float, hi: float, w_lo: float, w_hi: float, spot: float, width: int) -> Text:
    """The held span of prices on the series' window, with spot marked: the site's portfolio bar."""
    out = Text(no_wrap=True)
    span = max(w_hi - w_lo, 1e-9)
    x = lambda p: min(max(int((p - w_lo) / span * width), 0), width - 1)       # noqa: E731
    a, b, s = x(lo), x(hi - 1e-9), x(spot) if spot > 0 else -1
    for i in range(width):
        if i == s:
            out.append("┃", style=f"bold {ORANGE}")
        elif a <= i <= b:
            out.append("━", style=f"bold {HELD}")
        else:
            out.append("─", style=RULE)
    return out


class PositionsPane(Widget):
    can_focus = False
    DEFAULT_CSS = "PositionsPane { text-wrap: nowrap; text-overflow: clip; }"

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t, m = self.t, self.t.noc
        width, height = max(self.size.width - 2, 30), max(self.size.height - 2, 3)
        u, b = m.unit, selected_series(t)
        rows = m.by_close(u, b.short if b else "")
        out = Text(no_wrap=True, overflow="crop")
        if u is None or not rows:
            who = "No bot" if u is None or u.kind == ALL else u.name
            out.append(f"\n  {who} holds nothing in {b.short if b else 'any series'}." if u is None or u.holdings or u.kind == ALL else
                       f"\n  {who} holds nothing yet.", style=DIM)
            if u is not None and u.holdings and b:
                elsewhere = sorted({h.series for h in u.holdings if h.series and h.series != b.short})
                out.append(f"\n  It holds positions in {', '.join(elsewhere)}: h l choose the series.", style=FAINT)
            return out
        cols = pos_cols(width)
        spot = float(getattr(t.feed(b.asset or "BTC"), "spot", 0.0) or 0.0) if b else 0.0
        w_lo, w_hi = min([r[3] for r in rows] + ([spot] if spot > 0 else [])), max([r[4] for r in rows] + ([spot] if spot > 0 else []))
        pad = (w_hi - w_lo) * 0.05
        w_lo, w_hi = w_lo - pad, w_hi + pad
        cost, value = sum(r[6] for r in rows), sum(r[7] for r in rows)
        out.append(f" {len(rows)} closes  cost {fmt.sats(cost)}  value {fmt.sats(value)}  ", style=TEXT)
        out.append(fmt.sats(value - cost, signed=True), style=f"bold {_pnl(value - cost)}")
        out.append("\n")
        out.append("  " + "".join(f"{n.upper():<{w}}" if n in ("close", "in", "held", "range") else f"{n.upper():>{w}}" for n, w in cols) + "\n",
                   style=FAINT)
        room = max(height - 2, 1)
        m.pos_cur = max(0, min(m.pos_cur, len(rows) - 1))
        start = _window(m.pos_cur, len(rows), room)
        for i, (end, _, _, lo, hi, legs, c, v) in enumerate(rows[start:start + room], start):
            sel = i == m.pos_cur and m.focus == 1
            line = Text(no_wrap=True, overflow="crop")
            line.append("▸ " if sel else "  ", style=f"bold {ORANGE}")
            for n, w in cols:
                if n == "held":
                    line.append_text(range_bar(lo, hi, w_lo, w_hi, spot, w - 2))
                    line.append("  ")
                    continue
                txt = {"close": fmt.close_label(end, True), "in": fmt.countdown(end), "range": span(lo, hi),
                       "legs": str(legs), "cost": sats(c), "value": sats(v), "p&l": sats(v - c, signed=True)}[n]
                style = (_pnl(v - c) if n == "p&l" else (f"bold {ORANGE}" if sel else TEXT) if n == "close" else
                         DIM if n in ("in", "legs") else TEXT)
                line.append(f"{txt:<{w}}" if n in ("close", "in", "range") else f"{txt:>{w}}", style=style)
            line.truncate(width, overflow="crop", pad=True)
            if sel:
                line.stylize(f"on {CHIP}")
            out.append_text(line)
            out.append("\n")
        return out


def positions_title(t: Terminal) -> str:
    u, b = t.noc.unit, selected_series(t)
    who = "every bot" if u is None or u.kind == ALL else "your account" if u.kind == ACCOUNT else u.name
    return f"POSITIONS · {who} · {b.short if b else 'no series'} · a row a close"


# ── the tape ────────────────────────────────────────────────

def fill_line(f, now: float, width: int) -> Text:
    buy = f.contracts > 0
    return _line(width, (f" {time.strftime('%H:%M:%S', time.gmtime(f.at))} ", ORANGE if now - f.at <= FLASH_S else FAINT),
                 ("● ", RED if f.live else GREEN), (f"{f.name[:11]:<12}", TEXT), ("BUY  " if buy else "SELL ", f"bold {GREEN if buy else RED}"),
                 (f"{f.asset + ' ' + fmt.close_label(f.end, True):<21}", DIM), (f"{span(f.lo, f.hi):<15}", TEXT),
                 (f"{f.option:>3} rng", FAINT), (f"{fmt.contracts(abs(f.contracts)):>8}", TEXT), (f"{sats(abs(f.sats)):>9}", DIM))


def log_lines(t: Terminal, u: Unit | None, n: int) -> list[tuple[str, str]]:
    """(who, line) for the last `n` log lines of the selection: one bot's, or every running bot's in the order they were said."""
    if u is None:
        return []
    if u.runner is not None:
        return [("", x) for x in list(u.runner.log)[-n:]]
    if u.kind != ALL:
        return []
    merged = [(x[:8], i, r.name, x) for r in t.runners.values() for i, x in enumerate(list(r.log)[-n:])]
    return [(name, x) for _, _, name, x in sorted(merged, key=lambda y: (y[0], y[1]))[-n:]]


class TapePane(Widget):
    can_focus = False
    DEFAULT_CSS = "TapePane { text-wrap: nowrap; text-overflow: clip; }"

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t, m = self.t, self.t.noc
        width, height = max(self.size.width - 2, 30), max(self.size.height - 2, 2)
        u, now = m.unit, time.time()
        fills = m.trades(u)[-height:]                   # an order a line: `option` is its number of ranges
        logs = log_lines(t, u, height)
        split = width >= 124 and bool(logs)
        fw = 90 if split else width
        left = [fill_line(f, now, fw) for f in fills]
        if not left:
            left = [_line(fw, (" No fills yet: every buy and sale of the selection lands here as it happens.", FAINT))]
        left += [Text(" " * fw)] * (height - len(left))
        if not split:
            if not fills and logs:                      # a narrow terminal with nothing filled yet: the log says why
                left = [_line(fw, (f" {who + '  ' if who else ''}{x}", DIM)) for who, x in logs]
            return Text("\n", no_wrap=True).join(left[:height])
        out = []
        logs = [("", "")] * (height - len(logs)) + logs
        for a, (who, x) in zip(left[:height], logs, strict=False):
            hot = any(k in x for k in ("BUY", "SELL", "would buy", "would sell"))
            bad = any(k in x for k in ("failed", "rejected", "cannot"))
            row = Text(no_wrap=True, overflow="crop")
            row.append_text(a)
            row.append("│ ", style=RULE)
            row.append_text(_line(width - fw - 2, (f"{who[:12]:<13}" if who else "", ORANGE), (x, RED if bad else TEXT if hot else DIM)))
            out.append(row)
        return Text("\n", no_wrap=True).join(out)


def tape_title(t: Terminal) -> str:
    u = t.noc.unit
    who = "every bot" if u is None or u.kind == ALL else "your account" if u.kind == ACCOUNT else u.name
    log = " │ the log" if u is not None and (u.runner is not None or u.kind == ALL) else ""
    return f"TAPE · {who} · orders as they fill: close, the prices spanned, ranges, contracts, sats{log}"


__all__ = ["FleetPane", "WallPane", "PositionsPane", "TapePane", "Monitor"]
