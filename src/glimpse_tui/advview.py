"""The advanced screen: bots that trade several markets at once, and those markets side by side while they do.

The bots screen shows one model on one series. The bots here read a family of linked series together and trade across
them: the Triangulators (one forecast of Bitcoin and gold, or Bitcoin and oil, its likely band bought across the books
that price it) and the Arbitrageurs (covers across pairs of coinciding closes). The desk draws every series the selected
bot trades as a column: the close at one instant, the market's picture of it, the bot's picture of it where the bot has
one, the band it bets on and what a set of it costs, and on each range what the bot holds, what it bought or would buy,
and what it sold. Above the columns the triangle itself: the three medians, what two of them imply for the third, and
how far the market, the lens and the triangle each sit from agreement.

Before a bot is run the screen watches: a Triangulator that draws everything and sends nothing, on the instant you pick
with h and l. Once the bot runs, the desk follows its visits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widget import Widget

from . import arb, fmt, tri
from .api import Book
from .botsview import overlay, portfolio_line, progress, window
from .theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT
from .tri import pspan

if TYPE_CHECKING:
    from .app import Terminal
    from .bots import Bot

LOG_HEIGHT = 12
MIN_COL = 36                    # a column narrower than this is dropped, farthest series first
SPAN_W, PCT_W = 15, 7


@dataclass
class Column:
    """One market the bot trades, at the instant the desk shows."""
    label: str
    book: Book
    spot: float
    market: list[float]                                 # the close as the site draws it
    final: list[float] | None = None                    # the bot's picture of it, if it has one
    own: list[float] | None = None
    implied: list[float] | None = None
    formed: float = 0.0
    weight: float = 0.0
    buys: dict[int, tuple[float, float]] = field(default_factory=dict)   # range -> (contracts, sats) bought or planned this visit
    held: dict[int, tuple[float, float]] = field(default_factory=dict)   # range -> (contracts, cost)
    sold: dict[int, float] = field(default_factory=dict)                 # range -> contracts sold this visit
    synthetic: bool = False                             # a model, not a market: no market column, nothing to buy or hold
    note: str = ""
    band: tuple[int, int] | None = None                 # the likely band the bot bets on: (first, last) range
    band_p: float = 0.0                                 # the bot's chance the close lands in it
    band_price: float = 0.0                             # what one contract on each of its ranges costs now, before the fee


def advanced(t: Terminal) -> list[Bot]:
    """The bots that trade across series, in the zoo's order: the Triangulator first."""
    return [b for b in t.bots if b.arbitrage]


def selected(t: Terminal) -> Bot | None:
    shown = advanced(t)
    return shown[max(0, min(t.adv_cur, len(shown) - 1))] if shown else None


def source(t: Terminal, b: Bot | None):
    """Where the desk's data comes from: the bot's runner if it has run, else the screen's watcher for it."""
    if b is None:
        return None
    r = t.runners.get(b.id)
    if r is not None and (r.running or r.cycles):
        if r.running or getattr(r, "last", None) is not None or getattr(t, "adv_watch", None) is None:
            return r
    w = getattr(t, "adv_watch", None)
    return w if w is not None and w.bot.id == b.id else r


def pmed(x: float) -> str:
    return f"{x / 1000:,.0f}k" if x >= 1_000_000 else fmt.price(x)


def _index_of(book: Book, legs: dict) -> dict[int, tuple[float, float]]:
    at = {o: i for i, o in enumerate(book.option_ids)}
    return {at[o]: x for o, x in legs.items() if o in at}


def triangle_columns(v) -> list[Column]:
    """The three sides of a Triangulator's visit as columns, in the triangle's order."""
    out = []
    for label, s in v.sides.items():
        out.append(Column(label, s.book, s.spot, s.market, s.final, s.own, s.implied, s.formed, s.weight,
                          {x.index: (x.contracts, x.cost_sats) for x in s.buys}, dict(s.held),
                          {i: c for i, c, _ in s.sold}, s.synthetic, s.note, s.band, s.band_p, s.band_price))
    return out


def arb_columns(r) -> tuple[list[Column], int]:
    """An Arbitrageur's series at its nearest instant: each book as the bot last saw it, with the legs of its covers
    marked as holdings. (columns, the instant's close time; 0 before its first listing)."""
    from .tri import market_picture

    pairs = r._pairs
    if not pairs:
        return [], 0
    end = pairs[0].end
    rows = {}
    for p in pairs:
        if p.end != end:
            break
        rows.setdefault(p.label_a, p.a)
        rows.setdefault(p.label_b, p.b)
    out = []
    for label in r.scheme.series:
        row = rows.get(label)
        if row is None:
            continue
        book = Book.from_row(row)
        _, q = r._shown.get(row.topic_id, (0.0, None))
        if q is not None and len(q) == len(book.shares):
            book.shares = list(q)
            book.reprice()
        feed = r.feeds.get(r._assets.get(label, label))
        out.append(Column(label, book, float(getattr(feed, "spot", 0.0) or 0.0), market_picture(book), formed=arb.formed(book.probs),
                          held=_index_of(book, r.ledger.legs(r.key, r.mode, row.topic_id))))
    return out, end


def columns(t: Terminal, b: Bot | None) -> tuple[list[Column], int, object]:
    """(columns, the instant's close time, the visit) for the selected bot."""
    src = source(t, b)
    if src is None:
        return [], 0, None
    if b is not None and tri.triangle_of(b) is not None:
        v = getattr(src, "last", None)
        if v is None and src is not t.adv_watch and t.adv_watch is not None and t.adv_watch.bot.id == b.id:
            v = t.adv_watch.last
        return (triangle_columns(v), v.end, v) if v is not None else ([], 0, None)
    cols, end = arb_columns(src)
    return cols, end, None


def desk_title(t: Terminal) -> str:
    b = selected(t)
    return f"ADVANCED · {b.name} · {b.blurb}" if b else "ADVANCED · bots across several markets"


def log_title(t: Terminal) -> str:
    from .botsview import log_title as bots_log_title

    b = selected(t)
    r = t.runners.get(b.id) if b else None
    if b is None:
        return "LOG"
    if r is not None and (r.running or r.cycles):
        return bots_log_title(t, r)
    w = t.adv_watch
    if w is not None and w.bot.id == b.id:
        return f"○ WATCHING on paper · {b.name} through {w.lens_name} · nothing is sent until you run it (enter)"
    return f"○ NOT RUNNING · {b.name} · enter runs it"


# ── drawing ─────────────────────────────────────────────────

def _line(width: int, *parts: tuple[str, str]) -> Text:
    out = Text(no_wrap=True, overflow="ellipsis")
    for s, style in parts:
        out.append(s, style=style)
    out.truncate(width, overflow="ellipsis", pad=True)
    return out


def column_lines(c: Column, width: int, rows: int, has_pictures: bool) -> list[Text]:
    """One market as a column: its name, spot and formedness; its medians; then its ladder, highest price on top, the
    market's chance and the bot's on each row, both as one bar, and on the right what the bot does there."""
    lines: list[Text] = []
    head = [(f" {c.label}", f"bold {ORANGE}")]
    if c.spot > 0:
        head += [(f"  {pmed(c.spot) if c.spot >= 1e6 else fmt.price(c.spot)}", f"bold {TEXT}"), (" spot", FAINT)]
    if c.synthetic:
        head.append(("  ·  a model: no market on Glimpse", DIM))
    else:
        head.append((f"  formed {c.formed:.0%}", DIM))
        if c.final is not None:
            head.append((f"  ·  implied weight {c.weight:.2f}", DIM))
    lines.append(_line(width, *head))
    from .tri import median

    if c.synthetic:
        parts = c.note.split(" · ")
        lines.append(_line(width, (" " + " · ".join(parts[:2]), DIM)))
        lines.append(_line(width, (" " + " · ".join(parts[2:]), DIM), (f"   median {pmed(median(c.book.bins, c.final))}", TEXT)))
        has_pictures = False
    else:
        lines.append(_line(width, (" median", FAINT), (f"  market {pmed(median(c.book.bins, c.market))}", DIM),
                           (f" · lens {pmed(median(c.book.bins, c.own))}" if c.own is not None else "", DIM)))
        if c.final is not None:
            implied_ = f" implied {pmed(median(c.book.bins, c.implied))}" if c.implied is not None else ""
            lines.append(_line(width, ("        ", FAINT), (implied_, DIM), (f" · triangle {pmed(median(c.book.bins, c.final))}", TEXT)))
        if c.band is not None:
            lo_, hi_ = c.band
            ratio = tri.pays(c.band_p, c.band_price)
            a_set = " a set" if width >= 64 else ""
            lines.append(_line(width, (" band", FAINT), (f"  {pspan(c.book.bins[lo_][0], c.book.bins[hi_][1])}", f"bold {TEXT}"),
                               (f" · {hi_ - lo_ + 1} ranges · p {c.band_p:.0%}", DIM),
                               (f" · {c.band_price:,.0f} sats{a_set} pays {ratio:.2f}×", f"bold {GREEN}" if ratio >= 1.25 else DIM)))
    picture = c.final if c.final is not None else c.market
    rows = max(rows, 3)
    lo, hi, step = window(picture, rows, zoom=0)
    groups = [(i, min(i + step - 1, hi)) for i in range(lo, hi + 1, step)]
    mk = [sum(c.market[a:b + 1]) for a, b in groups]
    bt = [sum(c.final[a:b + 1]) for a, b in groups] if c.final is not None and not c.synthetic else []
    top = max(max(mk), max(bt, default=0.0), 1e-9)
    tag_w = 18 if width >= 60 and not c.synthetic else 0
    barw = max(width - SPAN_W - 2 - PCT_W - (PCT_W if has_pictures else 0) - 2 - tag_w - 1, 6)
    lines.append(_line(width, (f" {'price at close':<{SPAN_W}} ", FAINT), (f"{'model' if c.synthetic else 'market':>{PCT_W}}", FAINT),
                       (f"{'bot':>{PCT_W}}" if has_pictures else "", FAINT),
                       (f"  {step} ranges per row" if step > 1 else "", FAINT)))
    for n in range(len(groups) - 1, -1, -1):
        (a, b), m = groups[n], mk[n]
        g_lo, g_hi = c.book.bins[a][0], c.book.bins[b][1]
        here = g_lo <= c.spot < g_hi
        row = Text(no_wrap=True, overflow="crop")
        row.append(f"{'◂' if here else ' '}{pspan(g_lo, g_hi):<{SPAN_W}} ", style=f"bold {TEXT}" if here else TEXT)
        row.append(f"{fmt.pct(m):>{PCT_W}}", style=TEXT)
        if has_pictures:
            row.append(f"{fmt.pct(bt[n]) if bt else '':>{PCT_W}}", style=f"bold {ORANGE}")
        row.append("  ")
        if bt:
            row.append_text(overlay(bt[n] / top, m / top, barw))
        else:
            row.append(fmt.bar(m / top, barw).ljust(barw), style=ORANGE)
        if tag_w:
            bought = [c.buys[i] for i in range(a, b + 1) if i in c.buys]
            held = [c.held[i] for i in range(a, b + 1) if i in c.held]
            sold = [c.sold[i] for i in range(a, b + 1) if i in c.sold]
            if bought:
                row.append(f" buys {fmt.contracts(sum(x for x, _ in bought))} · ₿{sum(s for _, s in bought):,.0f}", style=f"bold {GREEN}")
            elif sold:
                row.append(f" sold {fmt.contracts(sum(sold))}", style=f"bold {RED}")
            elif held:
                row.append(f" holds {fmt.contracts(sum(x for x, _ in held))}", style=ORANGE)
            elif c.band is not None and a <= c.band[1] and b >= c.band[0]:
                row.append(" band", style=DIM)
        row.truncate(width, overflow="crop", pad=True)
        lines.append(row)
    return lines


def strip(t: Terminal, b: Bot, src, cols: list[Column], end: int, v, width: int) -> list[Text]:
    """The lines above the columns: which bots, which instant, and for the triangle how far apart its sides sit."""
    out: list[Text] = []
    tabs: list[tuple[str, str]] = [(" ADVANCED BOTS ", f"bold #000000 on {ORANGE}"), ("  ", "")]
    for x in advanced(t):
        r = t.runners.get(x.id)
        on = bool(r and r.running)
        sel = x is b
        tabs.append(("▸ " if sel else "  ", f"bold {ORANGE}"))
        tabs.append(("● " if on else "○ ", (RED if r.live else GREEN) if on else FAINT))
        tabs.append((x.name, f"bold {ORANGE}" if sel else TEXT))
        tabs.append(("   ", ""))
    if tri.triangle_of(b) is not None:
        tabs += [("   lens ", FAINT), (t.lens_bot().name if t.lens_bot() else "the consensus", f"bold {TEXT}"), ("  m", f"bold {ORANGE}"),
                 (" change", FAINT)]
    out.append(_line(width, *tabs))
    if src is None:
        out.append(_line(width, (f" Press enter to run {b.name}: {b.blurb}.", DIM)))
        return out
    if tri.triangle_of(b) is not None:
        tri_ = src.triangle
        if v is None:
            last = list(src.log)[-1] if src.log else ""
            out.append(_line(width, (" reading the three books…", DIM), (f"  {last}", FAINT)))
            return out
        k = src.instants.index(end) + 1 if end in src.instants else 0
        out.append(_line(width, (" TRIANGLE ", f"bold #000000 on {ORANGE}"),
                         (f"  {'·'.join(tri_.legs)} at {fmt.question('', end)[4:]}", f"bold {TEXT}"),
                         (f" · closes in {fmt.countdown(end)}", DIM),
                         (f" · instant {k} of {len(src.instants)}" + ("" if src.running and not src.watch else "  h l walk them"), DIM),
                         (f"    {tri_.cross} = {tri_.quote} × 10⁸ / {tri_.base}" + (f", {tri_.quote} a model" if tri_.synthetic else
                                                                                   f" × {v.basis:.4f}"), FAINT)))
        meds = {label: s.medians() for label, s in v.sides.items()}
        mk = {k_: m["market"] for k_, m in meds.items()}
        implied_ = tri_.implied_price(tri_.cross, mk, v.basis) if v.basis > 0 else 0.0
        parts: list[tuple[str, str]] = [(" gap  ", FAINT)]
        for which, name in (("market", "the market"), ("own", "the lens"), ("final", "the triangle")):
            if any(which not in m for m in meds.values()):
                continue
            gap = v.gaps.get(which, 0.0)
            parts.append((f"by {name} ", f"bold {TEXT}" if which == "final" else DIM))
            parts.append((fmt.roi(gap), f"bold {GREEN if abs(gap) < 0.005 else ORANGE if abs(gap) < 0.02 else RED}"))
            parts.append(("   ", ""))
        parts.append((f"  the {tri_.cross} book's median {pmed(mk[tri_.cross])} against {pmed(implied_)} implied by {tri_.base} "
                      f"{pmed(mk[tri_.base])} and {tri_.quote} {pmed(mk[tri_.quote])}" + (" (model)" if tri_.synthetic else ""), FAINT))
        out.append(_line(width, *parts))
        note = " · ".join(x for x in (v.cover, v.note) if x)
        rho = "  ·  ρ " + ", ".join(f"{a}/{b_} {x:+.2f}" for (a, b_), x in v.rho.items()) if v.rho else ""
        out.append(_line(width, (" " + note, GREEN if "buy" in note.lower() else DIM), (rho, FAINT)))
        return out
    out.append(_line(width, (f" {b.name} ", f"bold #000000 on {ORANGE}"),
                     (f"  covers pairs of closes at one instant across {src.series}", TEXT),
                     (f" · nearest instant {fmt.question('', end)[4:]}" if end else " · listing the series…", DIM),
                     (f" · {progress(src)} · {src.trades} cover{'' if src.trades == 1 else 's'}", DIM)))
    out.append(_line(width, (" the ranges it holds in each book are the legs of its covers; the books are as the bot last read them", FAINT)))
    return out


class AdvancedPane(Widget):
    """The desk: the strip, then one column per market the selected bot trades, then the legend."""
    can_focus = False
    DEFAULT_CSS = "AdvancedPane { text-wrap: nowrap; text-overflow: clip; }"

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t = self.t
        width, height = max(self.size.width - 2, 40), max(self.size.height - 2, 8)
        out = Text(no_wrap=True, overflow="crop")
        if not t.bots:
            out.append("\n  loading the model zoo…" if not t.bots_error else f"\n  {t.bots_error}", style=DIM)
            return out
        b = selected(t)
        if b is None:
            out.append("\n  No bot here trades across markets.", style=DIM)
            return out
        src = source(t, b)
        cols, end, v = columns(t, b)
        head = strip(t, b, src, cols, end, v, width)
        lines = list(head)
        if cols:
            n = max(1, min(len(cols), width // MIN_COL))
            cols = cols[:n]
            colw = width // n
            rows = height - len(head) - 1 - 4
            has_pictures = any(c.final is not None and not c.synthetic for c in cols)
            drawn = [column_lines(c, colw, rows, has_pictures) for c in cols]
            for r in range(max(len(d) for d in drawn)):
                row = Text(no_wrap=True, overflow="crop")
                for d in drawn:
                    row.append_text(d[r] if r < len(d) else Text(" " * colw))
                lines.append(row)
            legend = Text(no_wrap=True, overflow="crop")
            legend.append(" █", style=ORANGE)
            legend.append(" market and bot agree   ", style=FAINT)
            legend.append("█", style=GREEN)
            legend.append(" bot above the market: it buys   ", style=FAINT)
            legend.append("░", style=RED)
            legend.append(" market above the bot   ", style=FAINT)
            legend.append("buys", style=f"bold {GREEN}")
            legend.append(" this visit   ", style=FAINT)
            legend.append("holds", style=ORANGE)
            legend.append(" its position   ", style=FAINT)
            legend.append("band", style=DIM)
            legend.append(" the likely outcomes it bets on   ", style=FAINT)
            legend.append("◂", style=f"bold {TEXT}")
            legend.append(" spot", style=FAINT)
            lines.append(legend)
        return Text("\n", no_wrap=True).join(lines[:height])


class AdvLogPane(Widget):
    """Under the desk: what the running bot holds and its log, or what the watcher is doing."""
    can_focus = False
    DEFAULT_CSS = "AdvLogPane { text-wrap: nowrap; text-overflow: ellipsis; }"

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t, out = self.t, Text(no_wrap=True, overflow="ellipsis")
        b = selected(t)
        if b is None:
            return out
        h = max(self.size.height - 2, 3)
        r = t.runners.get(b.id)
        if r is None or not (r.running or r.cycles):
            w = t.adv_watch
            if tri.triangle_of(b) is not None:
                out.append(f"  Watching on paper through {w.lens_name if w else 'the lens'}: the ladders show what {b.name} would buy at this "
                           "instant; nothing is sent until you run it.\n", style=DIM)
            else:
                out.append(f"  {b.name} is not running: the desk fills with the books it reads once it is.\n", style=DIM)
            out.append("  enter", style=f"bold {ORANGE}")
            out.append(" run it (budget, then paper or LIVE)   ", style=DIM)
            if tri.triangle_of(b) is not None:
                out.append("m", style=f"bold {ORANGE}")
                out.append(" the lens   ", style=DIM)
                out.append("h l", style=f"bold {ORANGE}")
                out.append(" walk the instants   ", style=DIM)
            out.append("i", style=f"bold {ORANGE}")
            out.append(" how it works   ", style=DIM)
            out.append("e", style=f"bold {ORANGE}")
            out.append(" budget\n", style=DIM)
            if w is not None and w.bot.id == b.id:
                for line in list(w.log)[-max(h - 3, 1):]:
                    out.append(f"  {line}\n", style=FAINT)
            return out
        out.append_text(portfolio_line(r))
        out.append("   P full portfolio\n", style=FAINT)
        pos = r.holdings()
        for end, asset, _, _, p_lo, p_hi, contracts, cost in pos[:2]:
            out.append(f"  {fmt.question(asset, end):<28}{pspan(p_lo, p_hi):<18}{fmt.contracts(contracts):>8} × cost {fmt.sats(cost)}\n",
                       style=DIM)
        if len(pos) > 2:
            out.append(f"  … and {len(pos) - 2} more positions\n", style=FAINT)
        for line in list(r.log)[-max(h - out.plain.count("\n"), 1):]:
            hot = any(x in line for x in ("BUY", "SELL", "would buy", "would sell"))
            out.append(f"  {line}\n", style=TEXT if hot else RED if "failed" in line or "rejected" in line else DIM)
        return out
