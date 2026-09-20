"""The Glimpse terminal. One screen, three panes, every action a key."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

import httpx
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from . import api as api_mod
from . import auth, bots, botsview, charts, chrome, fmt
from . import pricing as P
from .api import ApiError, Batch, Book, Candle, Glimpse, MarketRow, Order, Position, Summary, Wallet, parse_bin
from .botsview import SORTS, TABS, BotDetailPane, BotListPane, BotLogPane, Scan
from .heatmap import ANSI256, P_FLOOR, TRUECOLOR, ZOOMS, Grid, HeatmapPane, fit_zoom, origin, scales, wants_truecolor
from .slip import WIDTH as SLIP_WIDTH
from .slip import SlipPane
from .term.panes import Workspace
from .term.shell import Shell
from .theme import BURNT, DIM, FAINT, GREEN, ORANGE, RED, RULE, TEXT

BOOK_EVERY, LIST_EVERY, ACCOUNT_EVERY, SPOT_EVERY, CANDLES_EVERY = 5, 60, 15, 10, 120
CANDLE_S = 1800                 # history candles on hourly series: half an hour
CLOSES_MAX = 400                # closes loaded: every live one (168 hourly = 7 days, ~172 daily); ~25 KB each a refresh
ESTIMATE_TOLERANCE = 0.005
MARKETS_PER_REQUEST = 24        # a box across more closes goes out as several requests: the server fills the closes of one
                                # request in turn, so a timeout mid-request would leave every close in it in doubt
DEFAULT_SERIES = "BTC"
OLD_SERIES = {"Hourly BTC": "BTC", "Daily BTC": "BTC 1D", "Daily ETH": "ETH", "Daily SOL": "SOL", "Daily XAU": "XAU"}
SLIP_MIN_WIDTH = 118            # narrower terminals keep the compact ticket under the chart
WASD = {"w": "up", "a": "left", "s": "down", "d": "right"}

HELP = """\
 The bar along the top names every screen and series with the key that reaches it: t the front page, f the
 forecast, o the odds, B bots, p portfolio, [ and ] the previous and next series (BTC, BTC 1D, ETH, SOL, XAU).
 The panel at the bottom lists what the screen you are on does, one row per kind of action. Above it is a
 vim status line: the mode (NORMAL, VISUAL while a box is selected, SLIP on the bet slip), messages, and
 on the right the keys typed so far, so 5 then j moves five cells and 12| goes to the twelfth close.

 PROBABILITY   The bet slip opens with what the market's prices say about your selection: the chance the
               close lands in range, or across several closes the chance all of them land, at least one lands,
               and how many to expect. Closes are treated as independent; the market prices each on its own.

 ALL KEYS  h j k l, ← ↓ ↑ → or a s w d move (left down up right), counts (5j)   { } a day   ( ) next / prev midnight
           0 ^ $ first / last close   12| close 12   gg G band top / bottom   ctrl-d ctrl-u half page   ctrl-f ctrl-b page
           ctrl-e ctrl-y scroll   zt zz zb place the row   zh zl zH zL scroll sideways   zi zo zoom price   < > zoom time
           zf the whole forecast   za auto-fit   zm median   v box   V the 80% band   o other corner   gv reselect   esc clear
           ma 'a mark, jump   '' jump back   enter open the close   tab bet slip   + - S size   b BET   / : price, command   [ ] series
           t front page   f forecast   o odds   p portfolio   B bots   L O log in / out   c colours   r refresh   ctrl-l redraw   q ZZ :q quit

 THE HEATMAP   Each character is one price cell of one close, one orange thickening with the market's
               belief:  ░  from 0.6% a bin,  ▒  from 1.5%,  ▓  from 3.9%,  █  above 10%. Blank is untraded.
               The cyan rule is that close's median. Candles left of NOW are price history. Every live close
               is loaded (a week of hourly closes, half a year of daily ones): < zooms time out until one
               character holds several closes (drawn as their average, and bet together), zf fits them all.

 BOTS (B)      Every model from the Glimpse Forecast Lab that can run on hourly candles alone, plus your own files.
               Each row shows what the bot believes about the nearest close right now: which way it leans and where
               it puts the middle of its picture. tab moves to the forecast on the right, where h l j k walk every
               close ahead — hour by hour, or day by day on a daily series — and each one draws that bot's whole
               distribution for that close. Under the ladder a chart runs the candles the model read into its
               median path and 80% band across every close ahead, with the market's median for contrast.
               i opens the model's own account of itself: the idea, the data it reads, the mathematics that
               turns the reading into a distribution, what it buys against the market, and the machinery every
               picture shares. esc goes back to the list. Nothing here is a track record.
               enter deploys the bot on this computer with a budget in sats; it buys what its picture says is cheap,
               sells what the market overpays for, and only ever touches positions it opened itself.
               With your API key loaded (L) it asks whether to trade real sats: type LIVE. Otherwise it practises on paper.
               Bots stop when the terminal closes; to keep one running
               without the screen:  glimpse-tui run <bot> --series BTC --budget 20000

 COMMANDS  :q quit   :help   :78000 or /78000 go to a price   :size 50   :size 500s (a budget in sats)
           :buy   :series eth   :noh clear the selection

 Prices are recomputed locally from outstanding shares, so they are exact where the API's rounded
 figures read zero. Cost includes the 2% fee. Payout is net of the 2% settlement fee: a winning
 contract pays 98 sats. There is no minimum bet; the fee is never less than 1 sat. Every order is checked
 against the server's own estimate before it is sent.
 All times UTC.

 No account? Register at glimpse.markets, complete verification, then create a key under
 Settings → Developer API Keys. Market data needs no key."""


TERM_HELP = """\
 The front page (t) is one long page, most important at the top: Bitcoin, the bots, gold, the chain, the news,
 then the world's markets. j and k walk it; everything else on this list works from anywhere.

 ON THE PAGE   j and k (or ↓ ↑, or ctrl-j ctrl-k) move down and up, window by window; the page scrolls to follow.
               h and l move across. g and G go to the top and the bottom. 1 to 9 jump to a window.
               The window you are on has an orange frame. The mouse wheel scrolls too.
 IN A WINDOW   enter opens it full screen with a green frame: j and k scroll or choose, enter opens what is chosen.
               esc goes back a step, and out to the page.
 ANY WINDOW    f opens the full forecast on the series that window shows, o the odds on every outcome of a close.
               Both are where betting happens; the page itself never trades.
 NEWS          enter on the news, then j k to a headline and enter (or l) to read it here. n and p go to the next
               and previous story; h or esc goes back to the headlines.
 SEARCH        : (or SPC SPC) lists everything that can be opened. Type to filter, ctrl-j ctrl-k to choose, enter.
 SPC MENU      SPC w windows (/ - split, d close, m maximize)   SPC b buffers (b list, n p, d close)
               SPC t this page   SPC f the forecast   SPC o the odds   SPC B bots   SPC p portfolio

 THE PAGE      Bitcoin's next 24 hours and the odds on its next hour · what the bots expect of the next hour,
               day and three days, and one bot at a time ([ ] walks the zoo) · gold, and the odds on its next
               close · hashrate and the difficulty adjustment · the news and world prices · dollar liquidity
               and the Treasury curve · currencies, commodities and world indices · fees, last.

 Numbers are live where a free feed exists and daily where only official data does; each window says which on
 its frame, and dims when it is out of date. SRC names every source behind them. Trading needs a key: L."""


@dataclass
class RowView:
    """One close, reduced once to what the screens draw. Nothing here is recomputed while you navigate."""
    row: MarketRow
    median: float
    band: tuple[float, float]                # 80% of the probability, after removing the subsidy floor
    raw: tuple[float, ...] = ()              # price/100 per bin, unnormalised
    bins: tuple[tuple[float, float], ...] = ()
    extent: tuple[float, float] = (0.0, 0.0)  # everything priced above the seed floor: where anyone has traded
    total: float = 1.0                        # sum(raw): divide by it to get a probability


@lru_cache(maxsize=16)
def _bins(names: tuple[str, ...]) -> tuple[tuple[float, float], ...]:
    """Every close in a series has the same 500 bins: parse them once and share the tuple."""
    return tuple(parse_bin(n) for n in names)


def summarise(row: MarketRow) -> RowView:
    px = P.prices(list(row.shares), P.alpha_for(max(len(row.shares), 2)))
    sig = P.signal(P.implied_probs(px))
    bins = _bins(row.names)
    lo, hi = P.hdi(sig)
    raw = tuple(x / 100 for x in px)
    live = [i for i, p in enumerate(raw) if p > P_FLOOR]
    extent = (bins[live[0]][0], bins[live[-1]][1]) if live else (bins[lo][0], bins[hi][1])
    return RowView(row, P.quantile(list(bins), sig, 0.5), (bins[lo][0], bins[hi][1]), raw, bins, extent, sum(raw) or 1.0)


# ── panes ───────────────────────────────────────────────────

class Pane(Widget):
    can_focus = False

    def __init__(self, id: str) -> None:
        super().__init__(id=id)

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    @property
    def rows(self) -> int:
        return max(self.size.height - 2, 1)


def position_question(p: Position) -> str:
    asset = api_mod.asset_of(p.topic_title)
    return fmt.question(asset, p.end_time) if asset and p.end_time else p.topic_title[-26:]


def window(cursor: int, n: int, height: int) -> int:
    """First visible index keeping the cursor centred where possible."""
    return max(0, min(cursor - height // 2, n - height))


class MarketsPane(Pane):
    def render(self) -> Text:
        t, out = self.t, Text(no_wrap=True, overflow="ellipsis")
        if not t.views:
            return Text(" loading…" if not t.error else f" {t.error}", style=DIM)
        hourly = t.hourly
        wide = self.size.width >= 46                  # narrow terminals drop the band column
        out.append(f"  {'close':<13}{'median':>8}  " + (f"{'80% band':<13}" if wide else "") + f"{'in':>8}\n", style=FAINT)
        h = self.rows - 1
        start = window(t.m_cur, len(t.views), h)
        for i, v in enumerate(t.views[start:start + h], start):
            sel = i == t.m_cur
            style = f"bold {ORANGE}" if sel else TEXT
            line = (f"{'▸' if sel else ' '} {fmt.close_label(v.row.end_time_utc, hourly):<13}"
                    f"{fmt.price(v.median):>8}  " + (f"{fmt.kprice(v.band[0]) + '–' + fmt.kprice(v.band[1]):<13}" if wide else "") +
                    f"{fmt.countdown(v.row.end_time_utc):>8}")
            out.append(line + "\n", style=style if t.pane == 0 or not sel else ORANGE)
        return out


class LadderPane(Pane):
    def render(self) -> Text:
        t, out = self.t, Text(no_wrap=True, overflow="ellipsis")
        b = t.book
        if b is None:
            return Text(" loading…", style=DIM)
        n = len(b.bins)
        w = self.size.width
        rw, pw, ow, reserve = (19, 8, 9, 15) if w >= 64 else (16, 7, 7, 3)
        out.append(f"  {'range':<{rw}}{'prob':>{pw}}{'odds':>{ow}}  \n", style=FAINT)
        h = self.rows - 1
        floor = sorted(b.probs)[n // 4]              # untraded bins all sit here; bars show belief above it
        top = (max(b.probs) - floor) or 1
        barw = max(w - (4 + rw + pw + ow) - reserve, 2)   # reserve leaves room for the held and spot markers
        lo, hi = t.selection
        view_cur = n - 1 - t.b_cur                    # highest price on top
        start = window(view_cur, n, h)
        spot_i = t.spot_bin
        for row in range(start, min(start + h, n)):
            i = n - 1 - row
            p = b.probs[i]
            cur, in_sel = i == t.b_cur, lo <= i <= hi and t.anchor is not None
            o = P.PAYOUT_SATS * (1 - P.FEE) / (b.prices[i] * (1 + P.FEE)) if b.prices[i] > 0 else 0
            style = f"bold {ORANGE}" if cur else TEXT if p * n > 1.5 else DIM
            line = f"{'▸' if cur else '┃' if in_sel else ' '} {fmt.span(*b.bins[i]):<{rw}}{fmt.pct(p):>{pw}}{fmt.odds(o):>{ow}}  "
            out.append(line, style=f"{style} on {BURNT}" if in_sel else style)
            out.append(fmt.bar((p - floor) / top, barw).ljust(barw), style=ORANGE)
            held = t.held.get((b.topic_id, b.option_ids[i]))
            if held:
                out.append(f" ●{fmt.contracts(held)}" if reserve > 3 else " ●", style=GREEN)
            if i == spot_i and not (held and reserve <= 3):
                out.append(f" ◂ {fmt.price(t.spot)}" if reserve > 3 else " ◂", style=TEXT)
            out.append("\n")
        return out


class TicketPane(Pane):
    def render(self) -> Text:
        t = self.t
        tk = t.ticket
        out = Text(no_wrap=True, overflow="ellipsis")
        if tk is None:
            return Text(f" {t.ticket_hint}", style=DIM) if t.ticket_hint else out
        multi = tk.markets > 1
        def kv(k: str, v: str, style: str = TEXT) -> None:
            out.append(f"{k} ", style=FAINT)
            out.append(f"{v}   ", style=f"bold {style}")
        out.append(" ")
        narrow = self.size.width < 108
        kv("size", f"{fmt.contracts(tk.contracts)} × {tk.bins} bin{'s' if tk.bins > 1 else ''}" + (f" × {tk.markets} closes" if multi else ""))
        kv("all land" if multi else "prob", fmt.pct(tk.prob), ORANGE)
        if multi:
            kv("any", fmt.pct(P.chances(t.sel_probs)[1]))
        kv("cost", fmt.sats(tk.cost_sats))
        if narrow and multi:
            out.append("\n ")
        kv("max payout" if multi else "payout", fmt.sats(tk.payout_sats), ORANGE)
        if narrow and not multi:
            out.append("\n ")
        kv("profit", fmt.sats(tk.profit_sats, signed=True), GREEN if tk.profit_sats > 0 else RED)
        kv("max odds" if multi and not narrow else "odds", fmt.odds(tk.odds), ORANGE)
        kv("max roi" if multi and not narrow else "roi", fmt.roi(tk.roi), GREEN if tk.roi > 0 else RED)
        if not narrow:
            out.append("\n ")
        note, style = f"pays if the close lands in range · fee {fmt.sats(tk.fee_sats)} included · breakeven {fmt.pct(tk.breakeven_prob)}", DIM
        if multi:
            note = (f"each close pays {fmt.sats(tk.payout_sats / tk.markets)} if it lands in range · max needs all {tk.markets}"
                    f" · fee {fmt.sats(tk.fee_sats)} included")
        if not t.ticket_live:
            note, style = "market closed, awaiting settlement", RED
        elif tk.profit_sats <= 0:
            note, style = "costs more than it can pay: narrow the range or pick a less crowded one", RED
        elif t.wallet and tk.cost_sats > t.wallet.balance_sats:
            note, style = f"exceeds balance {fmt.sats(t.wallet.balance_sats)}", RED
        if narrow:
            note = note if style == RED and not multi else "" if multi else f"breakeven {fmt.pct(tk.breakeven_prob)}"
        out.append(note, style=style)
        return out


class PortfolioPane(Pane):
    def render(self) -> Text:
        t, out = self.t, Text(no_wrap=True, overflow="ellipsis")
        if not t.api.authenticated:
            return Text("\n  Not logged in. Press L to enter an API key.", style=DIM)
        s = t.summary
        if s:
            out.append(f"  cost {fmt.sats(s.cost_sats)}   value {fmt.sats(s.value_sats)}   ", style=TEXT)
            out.append(f"p&l {fmt.sats(s.pnl_sats, signed=True)}", style=f"bold {GREEN if s.pnl_sats >= 0 else RED}")
            out.append(f"   live {s.active} · pending {s.pending} · resolved {s.resolved}\n\n", style=DIM)
        if not t.positions:
            out.append("  No open positions.", style=DIM)
            return out
        out.append(f"  {'market':<28}{'range':<20}{'contracts':>10}{'cost':>10}{'value':>10}{'p&l':>10}{'max payout':>12}\n", style=FAINT)
        h = self.rows - 3
        start = window(t.p_cur, len(t.positions), h)
        for i, p in enumerate(t.positions[start:start + h], start):
            sel = i == t.p_cur
            name = p.option_name.replace("-", "–")
            out.append(f"{'▸' if sel else ' '} {position_question(p):<28}{name:<20}{fmt.contracts(p.shares):>10}"
                       f"{fmt.sats(p.cost_sats):>10}{fmt.sats(p.value_sats):>10}", style=f"bold {ORANGE}" if sel else TEXT)
            out.append(f"{fmt.sats(p.pnl_sats, signed=True):>10}", style=GREEN if p.pnl_sats >= 0 else RED)
            out.append(f"{fmt.sats(p.shares * P.PAYOUT_SATS * (1 - P.FEE)):>12}\n", style=DIM)
        return out


# ── modals ──────────────────────────────────────────────────

class Prompt(ModalScreen[str | None]):
    def __init__(self, title: str, hint: str, password: bool = False, placeholder: str = "") -> None:
        super().__init__()
        self._title, self._hint, self._password, self._ph = title, hint, password, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._title, id="dialog-title")
            yield Static(self._hint, id="dialog-hint")
            yield Input(password=self._password, placeholder=self._ph)

    def on_input_submitted(self, e: Input.Submitted) -> None:
        self.dismiss(e.value)

    def on_key(self, e: events.Key) -> None:
        if e.key == "escape":
            self.dismiss(None)


class Confirm(ModalScreen[bool]):
    def __init__(self, title: str, body: Text) -> None:
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._title, id="dialog-title")
            yield Static(self._body)
            yield Static("y confirm      n / esc cancel", id="dialog-hint")

    def on_key(self, e: events.Key) -> None:
        if e.key in ("y", "Y"):
            self.dismiss(True)
        elif e.key in ("n", "N", "escape", "q"):
            self.dismiss(False)


class Help(ModalScreen[None]):
    def __init__(self, body: str = HELP) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Static("GLIMPSE TERMINAL", id="dialog-title")
            yield Static(self._body)

    def on_key(self, e: events.Key) -> None:
        self.dismiss(None)


class About(ModalScreen[None]):
    """One model's account of itself: the idea, what it reads, the mathematics, what it trades, the machinery every
    picture shares, and what it says right now. j k scroll; any other key closes it."""

    def __init__(self, body: Text, title: str) -> None:
        super().__init__()
        self._body, self._title = body, title

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Static(self._title, id="dialog-title")
            with VerticalScroll(id="about-body"):
                yield Static(self._body)
            yield Static("j k scroll      any other key closes", id="dialog-hint")

    def on_key(self, e: events.Key) -> None:
        e.stop()                        # the app's own handler must not see the key that closes this screen
        scroll = self.query_one("#about-body", VerticalScroll)
        if e.key in ("j", "down"):
            scroll.scroll_relative(y=3, animate=False)
        elif e.key in ("k", "up"):
            scroll.scroll_relative(y=-3, animate=False)
        elif e.key in ("ctrl+d", "pagedown"):
            scroll.scroll_page_down(animate=False)
        elif e.key in ("ctrl+u", "pageup"):
            scroll.scroll_page_up(animate=False)
        else:
            self.dismiss(None)


def book_of(view: RowView) -> Book:
    """The priced book of one close, as the models and the runner see it."""
    row = view.row
    book = Book(row.topic_id, row.title, row.end_time_utc, row.quote_mode, row.volume_msat,
                list(row.option_ids), list(view.bins), list(row.shares), P.alpha_for(len(row.shares)))
    book.reprice()
    return book


def scan_one(bot: bots.Bot, book: Book, ctx, market: list[float]) -> Scan:
    """One bot's picture of one close, summarised. Runs in a worker thread."""
    from .zoo.core import describe

    if bot.error:
        return Scan(error=bot.error)
    try:
        probs = bot.probs(book, ctx)
        if len(probs) != len(market) or abs(sum(probs) - 1) > 1e-6:
            return Scan(error=f"returned {len(probs)} probabilities summing to {sum(probs):.3f}")
        d = describe(ctx, probs)
    except Exception as e:
        return Scan(error=str(e) or e.__class__.__name__)
    gap = 0.5 * sum(abs(a - b) for a, b in zip(probs, market, strict=True))
    return Scan(d["view"], d["lean"], d["width"], gap, d["median"], d["low"], d["high"], tuple(probs))


# ── app ─────────────────────────────────────────────────────

class Terminal(App):
    TITLE = "Glimpse"
    ENABLE_COMMAND_PALETTE = False
    CSS = f"""
    Screen {{ background: #0D0D0D; color: {TEXT}; }}
    #head {{ height: 2; }}
    #strip {{ height: 1; display: none; }}
    #gobar {{ height: 1; display: none; background: #111111; }}
    #suggest {{ height: auto; max-height: 16; display: none; background: #111111; }}
    #term {{ display: none; }}
    #foot {{ height: auto; max-height: 9; color: {DIM}; }}
    Pane, SlipPane {{ border: round {RULE}; border-title-color: {DIM}; }}
    SlipPane.active {{ border: round {ORANGE}; border-title-color: {ORANGE}; }}
    Pane.active {{ border: round {ORANGE}; border-title-color: {ORANGE}; }}
    #main, #portfolio, #bots, #heatmap {{ height: 1fr; }}
    HeatmapPane {{ border: round {RULE}; border-title-color: {DIM}; border-subtitle-color: {DIM}; }}
    HeatmapPane.active {{ border: round {ORANGE}; border-title-color: {ORANGE}; }}
    #body {{ height: 1fr; }}
    #stage {{ width: 1fr; }}
    #slip {{ width: {SLIP_WIDTH}; }}
    #markets {{ width: 52; }}
    #ladder {{ width: 1fr; }}
    #ticket {{ height: 4; }}
    #portfolio, #bots, #heatmap {{ display: none; }}
    BotListPane, BotDetailPane, BotLogPane {{ border: round {RULE}; border-title-color: {DIM}; }}
    BotListPane, BotDetailPane {{ height: 1fr; }}
    BotListPane.active, BotDetailPane.active {{ border: round {ORANGE}; border-title-color: {ORANGE}; }}
    BotListPane {{ width: {botsview.LIST_WIDTH}; }}
    #botright {{ width: 1fr; }}
    BotLogPane {{ height: 12; display: none; }}
    ModalScreen {{ align: center middle; background: #0D0D0D 70%; }}
    #dialog {{ width: 72; height: auto; border: round {ORANGE}; background: #111111; padding: 1 2; }}
    #dialog.wide {{ width: 132; }}
    #about-body {{ height: auto; max-height: 80vh; }}
    #dialog-title {{ color: {ORANGE}; text-style: bold; margin-bottom: 1; }}
    #dialog-hint {{ color: {DIM}; margin: 1 0; }}
    Input {{ border: tall #222222; background: #0D0D0D; }}
    Input:focus {{ border: tall {ORANGE}; }}
    """

    def __init__(self, launchpad: str | None = None, go: str = "") -> None:
        super().__init__()
        self.launchpad = launchpad      # the launchpad to open on; None opens on the markets and ladder, as before
        self.go = go                    # a GO bar command to run at launch: `glimpse-tui MEMP`
        self.shell = Shell(self)        # the GO bar, the panes and the hub (TERMINAL.md); idle until a launchpad shows
        key, self.key_source = auth.load_key()
        self.key_mask = auth.mask(key)
        self.api = Glimpse(key, os.environ.get("GLIMPSE_BASE_URL"))
        self.batches: list[Batch] = []
        self.batch_i = 0
        self.views: list[RowView] = []
        self.m_cur = 0
        self.book: Book | None = None
        self.b_cur = 0
        self.anchor: int | None = None
        self.contracts = 21.0
        self.pane = 0                   # 0 markets, 1 ladder
        self.view = "main"              # main | heatmap | portfolio | bots | term (the launchpad)
        self.wallet: Wallet | None = None
        self.summary: Summary | None = None
        self.positions: list[Position] = []
        self.held: dict[tuple[int, int], float] = {}
        self.p_cur = 0
        self.spot = 0.0
        self.error = ""
        self.flash = ""
        self.flash_until = 0.0
        self.pending, self.count = "", ""               # vim: a chord prefix waiting for its second key, and a count
        self.marks: dict[str, tuple[int, int]] = {}     # letter -> (topic id, bin)
        self.hm_prev: tuple[int, int] | None = None     # where '' jumps back to
        self.hm_last_sel: tuple[tuple[int, int], tuple[int, int]] | None = None     # what gv restores
        self.hm_cols_v, self.hm_chart_w = 60, 120
        self.candle_s = 3600                            # seconds per loaded history candle
        self.bots: list[bots.Bot] = []                  # the zoo and your own files; loaded off the UI thread
        self.bots_error = ""
        self.bots_home = bots.bots_dir()
        self.runners: dict[str, bots.Runner] = {}       # by bot id
        self.bot_live: dict[str, bool] = {}
        self.bot_cur, self.bot_tab, self.bot_sort, self.bot_filter = 0, 0, 0, ""
        self.bot_scan: dict[str, Scan] = {}             # what each bot says about the nearest close
        self.bot_scanning = False
        self._scan_key: tuple | None = None
        self.bot_pane = 0                               # 0 the list, 1 the forecast on the right
        self.bot_when = 0                               # which close ahead the forecast pane is showing
        self.bot_ahead: dict[tuple[str, int], Scan] = {}   # (bot id, topic id) -> that bot's picture of that close
        self._ahead_for: tuple | None = None            # the bot and data the walk ahead was started for
        self.feeds: dict[str, object] = {}              # asset -> zoo.data.Feed
        self.ledger = bots.Ledger()
        self.data_version = 0           # bumps only when a market actually changed; keys every derived cache
        self.held_version = 0
        self._loading: str | None = None  # batch id of the market refresh in flight, if any
        self.candles: list[Candle] = []
        self.candles_long: list[Candle] = []            # hourly candles on hourly series: history for zoomed-out columns
        self.hm_col, self.hm_bin = 0, 0                 # cursor: column (negative is history) and price bin
        self.hm_anchor: tuple[int, int] | None = None   # where a box selection started
        self.hm_w, self.hm_per, self.hm_bpc = 2, 1, 1   # characters per column, closes per column, bins per cell
        self.hm_left, self.hm_gtop = 0, 0               # viewport: first column, top cell
        self.hm_cells_v = 40
        self.hm_fit_pending, self.hm_touched = True, False
        self.hm_median = True                           # draw the median trace through the forecast
        self.slip_focus, self.slip_cur = False, 0       # keyboard focus on the bet slip, and the field under it
        self.truecolor = wants_truecolor(saved=auth.load_state().get("colors", ""))
        charts.truecolor = self.truecolor
        self._hm_ticket: tuple[tuple, P.Ticket | None] | None = None
        self._hm_hist: tuple[tuple, list[Candle]] | None = None
        self._hm_groups: tuple[tuple, tuple[list[int], list[int], list[int]]] | None = None

    # derived ────────────────────────────────────────────────

    @property
    def batch(self) -> Batch | None:
        return self.batches[self.batch_i] if self.batches else None

    @property
    def hourly(self) -> bool:
        return bool(self.batch and self.batch.hourly)

    @property
    def asset(self) -> str:
        return (self.batch.asset if self.batch else "") or "BTC"

    @property
    def selection(self) -> tuple[int, int]:
        a = self.b_cur if self.anchor is None else self.anchor
        return min(a, self.b_cur), max(a, self.b_cur)

    @property
    def ticket(self) -> P.Ticket | None:
        if self.view == "heatmap":
            return self.hm_ticket
        if self.book is None:
            return None
        lo, hi = self.selection
        return P.ticket(self.book.shares, self.book.alpha, lo, hi, self.contracts)

    @property
    def sel_probs(self) -> list[float]:
        """The market's probability that each selected close lands in the selected range, nearest close first."""
        if self.view == "heatmap":
            sel = self.hm_selection
            if sel is None:
                return []
            c0, c1, b0, b1 = sel
            return [sum(v.raw[b0:b1 + 1]) / v.total for v in self.views[c0:c1 + 1]]
        tk = self.ticket if self.view == "main" else None
        return [tk.prob] if tk else []

    @property
    def ticket_live(self) -> bool:
        if self.view == "heatmap":
            sel = self.hm_selection
            return bool(sel) and all(self.views[c].row.end_time_utc > time.time() for c in range(sel[0], sel[1] + 1))
        return bool(self.book and self.book.is_live)

    @property
    def ticket_hint(self) -> str:
        if self.view != "heatmap":
            return ""
        sel = self.hm_selection
        if sel is None:
            return "history · move right of NOW (l, or 0) to price a close"
        return ""

    # bet slip ───────────────────────────────────────────────

    @property
    def slip_title(self) -> str:
        """The question being bet on: BTC at 18 Sep 11:00 UTC."""
        if self.view == "heatmap":
            sel = self.hm_selection
            if sel is None:
                return self.asset
            if sel[1] > sel[0]:
                return f"{self.asset} · {sel[1] - sel[0] + 1} closes"
            return fmt.question(self.asset, self.views[sel[0]].row.end_time_utc)
        return fmt.question(self.asset, self.book.end_time_utc) if self.book else self.asset

    @property
    def slip_when(self) -> list[str]:
        short = self.hm_cadence < 86400 if self.views else False
        if self.view == "heatmap":
            sel = self.hm_selection
            if sel is None:
                return ["price history"]
            a, b = self.views[sel[0]].row.end_time_utc, self.views[sel[1]].row.end_time_utc
            n = sel[1] - sel[0] + 1
            if n == 1:
                return [f"ends in {fmt.countdown(b)}"]
            return [f"{fmt.close_label(a, short)} → {fmt.close_label(b, short)} UTC", f"ends in {fmt.countdown(b)}"]
        if self.book:
            return [f"ends in {fmt.countdown(self.book.end_time_utc)}"]
        return []

    def slip_fields(self) -> list[tuple[str, str, str]]:
        """(key, label, value) for each editable edge of the bet."""
        size = ("size", "Size", f"{fmt.contracts(self.contracts)} contracts")
        if self.view == "heatmap":
            sel = self.hm_selection
            if sel is None:
                return []
            bins, short = self.views[0].bins, self.hm_cadence < 86400
            return [("low", "Low", fmt.price(bins[sel[2]][0])), ("high", "High", fmt.price(bins[sel[3]][1])),
                    ("from", "From", fmt.close_label(self.views[sel[0]].row.end_time_utc, short)),
                    ("to", "To", fmt.close_label(self.views[sel[1]].row.end_time_utc, short)), size]
        if self.view == "main" and self.book:
            lo, hi = self.selection
            return [("low", "Low", fmt.price(self.book.bins[lo][0])), ("high", "High", fmt.price(self.book.bins[hi][1])),
                    ("close", "Close", fmt.close_label(self.book.end_time_utc, self.hm_cadence < 86400)), size]
        return []

    def slip_adjust(self, key: str, d: int) -> None:
        """Step one edge of the bet by one cell, close or contract. Edges cannot cross."""
        if key == "size":
            self.contracts = max(1.0, round(self.contracts + d, 2)) if self.contracts >= 1 else max(0.01, round(self.contracts + d * 0.1, 2))
        elif self.view == "heatmap":
            sel = self.hm_selection
            if sel is None:
                return
            c0, c1, b0, b1 = sel
            step, top = self.hm_bpc, self.hm_grid.n - 1
            if key == "low":
                b0 = max(0, min(b0 + d * step, b1 - step + 1))
            elif key == "high":
                b1 = max(b0 + step - 1, min(b1 + d * step, top))
            elif key == "from":
                c0 = max(0, min(c0 + d, c1))
            elif key == "to":
                c1 = max(c0, min(c1 + d, len(self.views) - 1))
            self.hm_anchor, self.hm_col, self.hm_bin, self.hm_touched = (c0, b0), c1, b1, True
        elif self.book:
            lo, hi = self.selection
            if key == "low":
                lo = max(0, min(lo + d, hi))
            elif key == "high":
                hi = max(lo, min(hi + d, len(self.book.bins) - 1))
            elif key == "close":
                new = max(0, min(self.m_cur + d, len(self.views) - 1))
                if new != self.m_cur:
                    self.m_cur = new
                    self.load_book()
                return
            self.anchor, self.b_cur = lo, hi

    def slip_key(self, k: str, ch: str | None, n: int = 1) -> bool:
        fields = self.slip_fields()
        if k in ("tab", "escape"):
            self.slip_focus = False
        elif not fields:
            return k in ("j", "k", "h", "l", "up", "down", "left", "right", "enter")
        elif k in ("j", "down"):
            self.slip_cur = (self.slip_cur + 1) % len(fields)
        elif k in ("k", "up"):
            self.slip_cur = (self.slip_cur - 1) % len(fields)
        elif k in ("h", "left") or ch in ("-", "_"):
            self.slip_adjust(fields[self.slip_cur][0], -n)
        elif k in ("l", "right") or ch in ("+", "="):
            self.slip_adjust(fields[self.slip_cur][0], n)
        elif k == "enter":
            self.slip_edit(fields[self.slip_cur][0])
        else:
            return False
        return True

    @work
    async def slip_edit(self, key: str) -> None:
        if key == "size":
            self.set_size()
            return
        if key not in ("low", "high"):
            self.say("Step From, To and Close with h and l.")
            return
        v = await self.push_screen_wait(Prompt(key.upper(), f"Type the {key} price of your range. It snaps to the market's price bins."))
        if not v:
            return
        try:
            price = float(v.strip().replace(",", "").replace("$", ""))
        except ValueError:
            self.say("Enter a price, like 78000.")
            return
        bins = self.views[0].bins if self.view == "heatmap" else self.book.bins if self.book else ()
        if not bins:
            return
        size = bins[0][1] - bins[0][0]
        target = max(0, min(int((price - bins[0][0] - (1e-9 if key == "high" else 0)) // size), len(bins) - 1))
        fields = dict((f[0], i) for i, f in enumerate(self.slip_fields()))
        cur = (self.hm_selection[2 if key == "low" else 3] if self.view == "heatmap" else self.selection[0 if key == "low" else 1])
        step = self.hm_bpc if self.view == "heatmap" else 1
        self.slip_adjust(key, round((target - cur) / step))
        self.slip_cur = fields.get(key, self.slip_cur)
        self.paint()

    def toggle_colors(self) -> None:
        """For terminals that misreport what they can draw. Remembered."""
        from rich.color import ColorSystem

        self.truecolor = not self.truecolor
        self.console._color_system = ColorSystem.TRUECOLOR if self.truecolor else ColorSystem.EIGHT_BIT
        charts.truecolor = self.truecolor
        for p in self.shell.ws.panes:
            p.bump()
        self.query_one("#heatmap", HeatmapPane).palette = TRUECOLOR if self.truecolor else ANSI256
        auth.save_state({**auth.load_state(), "colors": "truecolor" if self.truecolor else "256"})
        self.say("24-bit colour. If the chart now looks wrong, press c again." if self.truecolor else "256-colour palette.")
        self.refresh(layout=True)

    # heatmap ────────────────────────────────────────────────

    @property
    def hm_cadence(self) -> int:
        ends = [v.row.end_time_utc for v in self.views[:12]]
        gaps = sorted(b - a for a, b in zip(ends, ends[1:], strict=False) if b > a)
        return gaps[len(gaps) // 2] if gaps else (3600 if self.hourly else 86400)

    @property
    def hm_sub(self) -> int:
        """History candles per forecast column: two half-hour candles under an hourly close, when a column is wide
        enough to split evenly. Otherwise one candle per column."""
        n = self.hm_cadence // self.candle_s if self.candle_s else 1
        return n if n > 1 and self.hm_w % n == 0 else 1

    @property
    def hm_cw(self) -> int:
        """Characters per history candle. Time runs at the same scale on both sides of NOW."""
        return max(self.hm_w // self.hm_sub, 1)

    @property
    def hm_span(self) -> int:
        """Seconds one forecast column covers: a close, or several when zoomed out."""
        return self.hm_cadence * self.hm_per

    @property
    def hm_groups(self) -> tuple[list[int], list[int], list[int]]:
        """Per close: its column's number on the clock, and the first and last close in that column. Zoomed out, a
        column holds the closes starting in one clock-aligned span (00:00-06:00, a Monday-to-Sunday week), so the
        grid lines and history candles line up with it."""
        n, per, cad = len(self.views), self.hm_per, self.hm_cadence
        key = (self.data_version, n, self.views[0].row.topic_id if n else 0, per, cad)
        if self._hm_groups is None or self._hm_groups[0] != key:
            if per == 1:
                gid = list(range(n))
                first, last = gid, gid
            else:
                span, org = cad * per, origin(cad * per)
                gid = [(v.row.end_time_utc - cad - org) // span for v in self.views]
                first, last = [0] * n, [0] * n
                for i in range(n):
                    first[i] = first[i - 1] if i and gid[i] == gid[i - 1] else i
                for i in reversed(range(n)):
                    last[i] = last[i + 1] if i < n - 1 and gid[i] == gid[i + 1] else i
            self._hm_groups = (key, (gid, first, last))
        return self._hm_groups[1]

    def hm_gstart(self, c: int) -> int:
        return self.hm_groups[1][c] if 0 <= c < len(self.views) and self.hm_per > 1 else c

    def hm_gend(self, c: int) -> int:
        return self.hm_groups[2][c] if 0 <= c < len(self.views) and self.hm_per > 1 else c

    def hm_col_time(self, c: int) -> tuple[int, int]:
        """(start, seconds covered) of column `c`: a close, a zoomed-out group of closes, or a history candle."""
        if c < 0:
            step = self.hm_span // self.hm_sub
            return self.hm_hist[c].t, step
        if self.hm_per == 1:
            return self.views[c].row.end_time_utc - self.hm_cadence, self.hm_cadence
        span = self.hm_span
        return self.hm_groups[0][c] * span + origin(span), span

    def hm_pos(self, c: int) -> int:
        """Character offset of column `c` from the NOW divider (history is negative)."""
        if c < 0:
            return c * self.hm_cw
        if self.hm_per == 1:
            return 1 + c * self.hm_w
        gid = self.hm_groups[0]
        return 1 + (gid[min(c, len(gid) - 1)] - gid[0]) * self.hm_w

    def hm_width(self, c: int) -> int:
        return self.hm_w if c >= 0 else self.hm_cw

    @property
    def hm_hist(self) -> list[Candle]:
        """Closed candles before the first live column. Column -1 is hist[-1]."""
        if not self.views or not self.candles:
            return []
        cad, sub, per = self.hm_cadence, self.hm_sub, self.hm_per
        long = per > 1 and bool(self.candles_long)          # zoomed out, the half hours run out after three days
        src, src_s = (self.candles_long, 3600) if long else (self.candles, self.candle_s)
        key = (len(src), src[-1].t, self.views[0].row.end_time_utc, cad, sub, per)
        if self._hm_hist is None or self._hm_hist[0] != key:
            step = cad * per // sub
            ks = src if step == src_s else api_mod.resample(src, step, origin(step))
            start = self.hm_col_time(0)[0]
            self._hm_hist = (key, [k for k in ks if k.t + step <= start])
        return self._hm_hist[1]

    @property
    def hm_grid(self) -> Grid:
        bins = self.views[0].bins
        return Grid(bins[0][0], bins[0][1] - bins[0][0], len(bins), self.hm_bpc)

    @property
    def hm_selection(self) -> tuple[int, int, int, int] | None:
        """(first column, last column, low bin, high bin), or None while the cursor is in history."""
        if not self.views:
            return None
        ac, ab = self.hm_anchor or (self.hm_col, self.hm_bin)
        c0, c1 = max(min(ac, self.hm_col), 0), min(max(ac, self.hm_col), len(self.views) - 1)
        if c1 < 0 or c0 > c1:
            return None
        c0, c1 = self.hm_gstart(c0), self.hm_gend(c1)       # zoomed out, a cell is every close in its column
        g = self.hm_grid
        return c0, c1, g.bins_of(min(ab, self.hm_bin) // g.bpc)[0], g.bins_of(max(ab, self.hm_bin) // g.bpc)[1]

    def hm_price(self, sel: tuple[int, int, int, int], contracts: float) -> P.Ticket:
        c0, c1, b0, b1 = sel
        parts = []
        for v in self.views[c0:c1 + 1]:
            q = list(v.row.shares)
            parts.append(P.ticket(q, P.alpha_for(len(q)), b0, b1, contracts, prob=sum(v.raw[b0:b1 + 1]) / v.total))
        return parts[0] if len(parts) == 1 else P.combine(parts)

    @property
    def hm_ticket(self) -> P.Ticket | None:
        sel = self.hm_selection
        if sel is None:
            return None
        key = (self.data_version, sel, self.contracts)
        if self._hm_ticket is None or self._hm_ticket[0] != key:
            self._hm_ticket = (key, self.hm_price(sel, self.contracts))
        return self._hm_ticket[1]

    def hm_fit(self, chart_w: int, cells_v: int) -> None:
        """Frame the near-term forecast, recent history and spot; put the cursor on the focused close at spot."""
        self.hm_fit_pending = False
        one = Grid(self.hm_grid.y_lo, self.hm_grid.bin_size, self.hm_grid.n, 1)
        ladder = one.n * one.bin_size
        near = [v.band for v in self.views[:max(chart_w * 2 // 3 // self.hm_w * self.hm_per, 1)] if v.band[1] - v.band[0] < 0.4 * ladder]
        y_hi = one.y_lo + ladder
        ref = self.views[0].median              # a spot far from the nearest close's median is not this market's price
        anchor = self.spot if 0.5 * ref < self.spot < 1.5 * ref else ref
        lo = min([b[0] for b in near] + [anchor * 0.97])
        hi = max([b[1] for b in near] + [anchor * 1.03])
        recent = [k for k in self.hm_hist[-max(chart_w // 3 // self.hm_cw, 1):] if 0.5 * ref < k.l and k.h < min(1.5 * ref, y_hi)]
        if recent:
            lo, hi = min(lo, min(k.l for k in recent)), max(hi, max(k.h for k in recent))
        self.hm_bpc = fit_zoom((hi - lo) / one.bin_size, cells_v)
        g = self.hm_grid
        self.hm_gtop = g.cell_of_price((lo + hi) / 2) + cells_v // 2
        self.hm_col = max(0, min(self.hm_col, len(self.views) - 1))
        self.hm_bin = one.cell_of_price(anchor)
        self.hm_left = self.hm_col
        while self.hm_left > -len(self.hm_hist) and self.hm_pos(self.hm_col) - self.hm_pos(self.hm_left - 1) <= chart_w // 3:
            self.hm_left -= 1                   # a third of the chart is history

    def hm_follow(self, chart_w: int, cells_v: int) -> None:
        """Scroll just enough to keep the cursor on screen. Columns have two widths (history candles are narrower),
        so this works in characters."""
        self.hm_cells_v, self.hm_chart_w, self.hm_cols_v = cells_v, chart_w, max(chart_w // self.hm_w, 1)
        if (self.hm_w, self.hm_per) not in scales(self.hm_cadence):
            self.hm_per = 1                     # another series: its cadence has other zoomed-out steps
        g, n_hist, last = self.hm_grid, len(self.hm_hist), len(self.views) - 1
        edge = min(2 * self.hm_w, chart_w // 4)
        self.hm_col = self.hm_gstart(max(-n_hist, min(self.hm_col, last)))
        self.hm_left = self.hm_gstart(max(-n_hist, min(self.hm_left, last)))
        while self.hm_left > -n_hist and self.hm_pos(self.hm_col) - self.hm_pos(self.hm_left) < edge:
            self.hm_left -= 1
        while self.hm_left < last and self.hm_pos(self.hm_col) + self.hm_width(self.hm_col) - self.hm_pos(self.hm_left) > chart_w - edge:
            self.hm_left += 1
        while self.hm_left > -n_hist and self.hm_pos(last) + self.hm_w - self.hm_pos(self.hm_left - 1) <= chart_w:
            self.hm_left -= 1                   # never leave blank space on the right while there is more to show
        mg = min(2, cells_v // 4)
        g_cur = self.hm_bin // g.bpc
        if g_cur > self.hm_gtop - mg:
            self.hm_gtop = g_cur + mg
        if g_cur < self.hm_gtop - cells_v + 1 + mg:
            self.hm_gtop = g_cur + cells_v - 1 - mg
        self.hm_gtop = max(min(cells_v - 1, g.cells - 1), min(self.hm_gtop, g.cells - 1))

    def hm_key(self, k: str, ch: str | None, n: int = 1, counted: bool = False) -> bool:
        if not self.views:
            return False
        day = 24 if self.hm_cadence < 86400 else 7
        step = {"h": -1, "left": -1, "l": 1, "right": 1}.get(k) or {"{": -day, "}": day}.get(ch or "")
        if step and ch not in ("{", "}"):
            self.hm_col = self.hm_next(self.hm_col, step * n)
        elif step:
            self.hm_col += step * n
        elif ch in (")", "("):
            cad, c = self.hm_cadence, self.hm_col
            for _ in range(n):                      # the next (or previous) close that opens a new day, or week on daily series
                c += 1 if ch == ")" else -1
                while 0 <= c < len(self.views):
                    d = datetime.fromtimestamp(self.views[c].row.end_time_utc - cad, UTC)
                    if (d.hour == 0) if cad < 86400 else (d.weekday() == 0):
                        break
                    c += 1 if ch == ")" else -1
            self.hm_prev, self.hm_col = (self.hm_col, self.hm_bin), max(0, min(c, len(self.views) - 1))
        elif ch in ("0", "^"):
            self.hm_col = 0
        elif ch == "$":
            self.hm_col = len(self.views) - 1
        elif ch == "|":
            self.hm_col = max(0, min(n - 1, len(self.views) - 1))
        elif k in ("ctrl+e", "ctrl+y"):
            self.hm_scroll(0, -n if k == "ctrl+e" else n)
        elif ch == "v":
            if self.hm_anchor is not None:
                self.hm_clear_selection()
            elif self.hm_col >= 0:
                self.hm_anchor = (self.hm_col, self.hm_bin)
        elif ch == "V" and self.hm_col >= 0:
            one = Grid(self.hm_grid.y_lo, self.hm_grid.bin_size, self.hm_grid.n, 1)
            band = self.views[self.hm_col].band
            self.hm_anchor, self.hm_bin = (self.hm_col, one.cell_of_price(band[0])), one.cell_of_price(band[1] - 1e-9)
        elif ch == "o" and self.hm_anchor is not None:
            self.hm_anchor, (self.hm_col, self.hm_bin) = (self.hm_col, self.hm_bin), self.hm_anchor
        elif ch in ("<", ">", ",", "."):
            self.hm_scale(1 if ch in (">", ".") else -1)
        elif k == "enter" and self.hm_anchor is None and self.hm_col >= 0:
            self.m_cur, self.view, self.pane = self.hm_col, "main", 1
            self.load_book()
        elif ch == "b" or k == "enter":
            self.buy_heatmap()
        else:
            return False
        self.hm_touched = True
        return True

    def hm_next(self, c: int, d: int) -> int:
        """`d` columns on from `c`. Zoomed out, a forecast column is several closes, so this steps by column."""
        if self.hm_per == 1:
            return c + d
        for _ in range(abs(d)):
            c = c + 1 if c < 0 and d > 0 else c - 1 if c <= 0 and d < 0 else \
                self.hm_gend(c) + 1 if d > 0 else self.hm_gstart(self.hm_gstart(c) - 1)
        return min(c, len(self.views) - 1)

    def hm_scale(self, d: int) -> None:
        """Step the time axis: < zooms out (narrower closes, then several closes to a character), > zooms in."""
        ladder = scales(self.hm_cadence)
        here = (self.hm_w, self.hm_per)
        i = ladder.index(here) if here in ladder else ladder.index((self.hm_w, 1))
        self.hm_w, self.hm_per = ladder[max(0, min(i + d, len(ladder) - 1))]
        self.say(self.hm_scale_label() + " per column", 2)

    def hm_scale_label(self) -> str:
        span = self.hm_span
        return f"{span // 3600}h" if span < 86400 else f"{span // 86400}d" if span % (7 * 86400) else f"{span // (7 * 86400)}w"

    def hm_fit_all(self) -> None:
        """zf: the whole forecast on screen. The widest time scale that shows every close with a little history
        left of NOW, and a price zoom that holds every close's 80% band."""
        chart_w, cells_v = self.hm_chart_w, self.hm_cells_v
        keep = chart_w - max(chart_w // 8, 2)
        for w, per in reversed(scales(self.hm_cadence)):     # the most detail that still fits
            self.hm_w, self.hm_per = w, per
            if self.hm_pos(len(self.views) - 1) + w <= keep:
                break
        one = Grid(self.hm_grid.y_lo, self.hm_grid.bin_size, self.hm_grid.n, 1)
        ladder = one.n * one.bin_size
        bands = [v.band for v in self.views if v.band[1] - v.band[0] < 0.4 * ladder] or [self.views[0].band]
        lo, hi = min(b[0] for b in bands), max(b[1] for b in bands)
        self.hm_bpc = fit_zoom((hi - lo) / one.bin_size, cells_v)
        self.hm_gtop = self.hm_grid.cell_of_price((lo + hi) / 2) + cells_v // 2
        self.hm_col, self.hm_left = 0, 0                    # the follow pulls history in to fill the left
        self.say(f"All {len(self.views)} closes · {self.hm_scale_label()} per column", 3)

    @property
    def spot_bin(self) -> int:
        if not self.book or self.spot <= 0:
            return -1
        return next((i for i, (lo, hi) in enumerate(self.book.bins) if lo <= self.spot < hi), -1)

    # layout ─────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(id="head")
        yield Static(id="strip")
        with Horizontal(id="body"):
            with Vertical(id="stage"):
                yield Workspace("term")
                with Horizontal(id="main"):
                    yield MarketsPane("markets")
                    yield LadderPane("ladder")
                yield HeatmapPane("heatmap")
                yield TicketPane("ticket")
                yield PortfolioPane("portfolio")
                with Horizontal(id="bots"):
                    yield BotListPane(id="botlist")
                    with Vertical(id="botright"):
                        yield BotDetailPane(id="botdetail")
                        yield BotLogPane(id="botlog")
            yield SlipPane(id="slip")
        yield Static(id="suggest")
        yield Static(id="gobar")
        yield Static(id="foot")

    def on_mount(self) -> None:
        self.paint()
        self.load_batches()
        self.set_interval(1, self.tick)
        self.set_interval(BOOK_EVERY, self.load_book)
        self.set_interval(LIST_EVERY, self.load_markets)
        self.set_interval(ACCOUNT_EVERY, self.load_account)
        self.set_interval(SPOT_EVERY, self.load_spot)
        self.set_interval(CANDLES_EVERY, lambda: self.view == "heatmap" and self.load_candles())
        self.load_account()
        if self.launchpad:
            self.shell.show(self.launchpad)
        if self.go:
            self.shell.run(self.go)
        self.paint()

    def tick(self) -> None:
        if self.flash and time.time() > self.flash_until:
            self.flash = ""
        self.shell.tick()
        self.paint()

    def say(self, msg: str, seconds: float = 6) -> None:
        self.flash, self.flash_until = msg, time.time() + seconds
        self.paint()

    def paint(self) -> None:
        if not self.query("#head"):
            return                      # shutting down: a cancelled worker's last paint lands after the screen is gone
        w, sh = self.size.width, self.shell
        term = self.view == "term"
        self.query_one("#head", Static).update(chrome.header(self, w))     # one header on every screen
        strip = self.query_one("#strip", Static)
        strip.display = term
        if term:
            strip.update(sh.strip(w))
        go, sug = self.query_one("#gobar", Static), self.query_one("#suggest", Static)
        go.display = sh.go_focus                        # the command line opens at the bottom, as in vim
        if go.display:
            go.update(sh.go_line(w))
        sug.display = (sh.go_focus and bool(sh.line.picks)) or sh.leader is not None
        if sh.leader is not None:
            sug.update(sh.which_key(w))
        elif sug.display:
            sug.update(sh.suggestions(w, 15 if self.size.height >= 36 else 8))
        self.query_one("#term").display = term
        visual = (self.hm_anchor if self.view == "heatmap" else self.anchor) is not None
        inside = term and sh.ws.inside and sh.ws.pane is not None
        mode = ("SPC" if sh.leader is not None else "SEARCH" if sh.go_focus else "SLIP" if self.slip_focus
                else "VISUAL" if visual and self.view in ("main", "heatmap") else "INSIDE" if inside else "NORMAL")
        view = ("go" if sh.go_focus else "slip" if self.slip_focus else "bots-forecast" if self.view == "bots" and self.bot_pane == 1
                else "term-inside" if inside else self.view)
        rows = 8 if self.size.height >= 40 else 4 if self.size.height >= 26 else 2
        extra = sh.inside_rows() if view == "term-inside" else []
        where = sh.ws.where() if term and not sh.go_focus else ""
        foot = Text("\n", no_wrap=True).join([chrome.status_line(self, mode, w, where), chrome.legend(view, w, rows, extra)])
        self.query_one("#foot", Static).update(foot)

        self.query_one("#main").display = self.view == "main"
        self.query_one("#heatmap").display = self.view == "heatmap"
        trading = self.view in ("main", "heatmap")
        slip = self.query_one("#slip", SlipPane)
        slip.display = trading and self.size.width >= SLIP_MIN_WIDTH
        if not slip.display:
            self.slip_focus = False
        self.query_one("#ticket").display = trading and not slip.display
        slip.border_title = "BET SLIP"
        slip.set_class(self.slip_focus, "active")
        self.slip_cur = max(0, min(self.slip_cur, len(self.slip_fields()) - 1))
        self.query_one("#portfolio").display = self.view == "portfolio"
        self.query_one("#bots").display = self.view == "bots"

        mk, ld = self.query_one("#markets", Pane), self.query_one("#ladder", Pane)
        want = 36 if self.size.width < 110 else 50 if slip.display else 52
        if getattr(self, "_mk_width", None) != want:     # assigning a style re-runs layout for the whole screen, even to the same value
            self._mk_width = mk.styles.width = want
        mk.set_class(self.pane == 0 and not self.slip_focus, "active")
        ld.set_class(self.pane == 1 and not self.slip_focus, "active")
        self.query_one("#heatmap").set_class(not self.slip_focus, "active")
        mk.border_title = f"MARKETS · {len(self.views)}"
        if self.book:
            ld.border_title = (f"{fmt.question(self.asset, self.book.end_time_utc)} · closes in {fmt.countdown(self.book.end_time_utc)}"
                               f" · vol {fmt.sats(self.book.volume_msat / 1000)}")
        lo, hi = self.selection
        tp = self.query_one("#ticket", Pane)
        tp.set_class((self.hm_anchor if self.view == "heatmap" else self.anchor) is not None, "active")
        if self.view == "heatmap":
            hp, sel = self.query_one("#heatmap", HeatmapPane), self.hm_selection
            zoom = f"{fmt.price(self.hm_grid.bin_size * self.hm_bpc)}/cell" if self.views else ""
            ahead = fmt.close_label(self.views[-1].row.end_time_utc, self.hm_cadence < 86400) if self.views else ""
            hp.border_title = (f"FORECAST · {self.batch.short if self.batch else ''} · {len(self.views)} closes to {ahead}"
                               f" · {self.hm_scale_label()}/col · {zoom}")
            hp.border_subtitle = hp.legend()
            tp.border_title = "TICKET"
            if sel:
                bins, short = self.views[0].bins, self.hm_cadence < 86400
                when = fmt.close_label(self.views[sel[0]].row.end_time_utc, short)
                if sel[1] > sel[0]:
                    when += f" → {fmt.close_label(self.views[sel[1]].row.end_time_utc, short)} · {sel[1] - sel[0] + 1} closes"
                tp.border_title = f"TICKET · {fmt.span(bins[sel[2]][0], bins[sel[3]][1])} · {when}"
        elif self.book:
            tp.border_title = f"TICKET · {fmt.span(self.book.bins[lo][0], self.book.bins[hi][1])}"
        self.query_one("#portfolio", Pane).border_title = "PORTFOLIO"
        bl, bd = self.query_one("#botlist"), self.query_one("#botdetail")
        bl.border_title = botsview.rule_title(self)
        bd.display = self.size.width >= botsview.DETAIL_MIN_WIDTH
        if getattr(self, "_bl_wide", None) != (not bd.display):
            self._bl_wide = not bd.display
            bl.styles.width = "1fr" if self._bl_wide else botsview.LIST_WIDTH
        if not bd.display:
            self.bot_pane = 0
        self.bot_when = max(0, min(self.bot_when, max(len(self.bot_closes) - 1, 0)))
        bd.border_title = botsview.detail_title(self)
        bl.set_class(self.bot_pane == 0, "active")
        bd.set_class(self.bot_pane == 1, "active")
        lg, r = self.query_one("#botlog", BotLogPane), self.runners.get(getattr(botsview.selected(self), "id", ""))
        lg.display = bd.display and bool(r and (r.running or r.cycles))
        if lg.display:
            want = max(min(len(r.log) + min(len(r.ledger.positions(r.bot.id, r.mode)), 4) + 4, 16), 8)
            if getattr(self, "_lg_height", None) != want:
                self._lg_height = lg.styles.height = want
            lg.border_title = botsview.log_title(self, r)
        if self.view == "bots":
            self.ensure_ahead()
            bl.refresh()
            bd.refresh()
            lg.refresh()
        for p in self.query(Pane):
            p.refresh()
        if self.view == "heatmap":
            self.query_one("#heatmap").refresh()
        slip.refresh()

    # data ───────────────────────────────────────────────────

    @work(exclusive=True, group="batches")
    async def load_batches(self) -> None:
        try:
            self.batches = await self.api.batches()
        except ApiError as e:
            self.error = str(e)
            self.say(str(e), 30)
            return
        want = auth.load_state().get("series", DEFAULT_SERIES)
        want = OLD_SERIES.get(want, want)               # state written before the series were renamed to tickers
        self.batch_i = next((i for i, b in enumerate(self.batches) if b.short == want),
                            next((i for i, b in enumerate(self.batches) if b.short == DEFAULT_SERIES), 0))
        self.load_markets()
        self.load_spot()

    @work(group="markets")
    async def load_markets(self) -> None:
        """Refresh the market list. A refresh for the same series never overlaps or restarts one in flight:
        on a slow link that used to cancel and re-download forever, at full CPU."""
        if not self.batch:
            return
        bid = self.batch.batch_id
        if self._loading == bid:
            return
        self._loading = bid
        try:
            rows = await self.api.markets(bid, limit=CLOSES_MAX)
            if not self.batch or bid != self.batch.batch_id:
                return
            old = {v.row.topic_id: v for v in self.views}
            fresh = [r for r in rows if r.shares and not (r.topic_id in old and old[r.topic_id].row.shares == r.shares)]
            done = {r.topic_id: v for r, v in zip(fresh, await asyncio.to_thread(lambda: [summarise(r) for r in fresh]), strict=True)}
            if not self.batch or bid != self.batch.batch_id:
                return
            views = [done.get(r.topic_id) or old[r.topic_id] for r in rows if r.shares]      # untouched closes keep their RowView
        except ApiError as e:
            self.say(str(e))
            return
        finally:
            if self._loading == bid:
                self._loading = None
        changed = bool(done) or [v.row.topic_id for v in views] != [v.row.topic_id for v in self.views]
        keep = self.views[self.m_cur].row.topic_id if self.views and self.m_cur < len(self.views) else None
        self.views = views
        if changed:
            self.data_version += 1
        self.m_cur = next((i for i, v in enumerate(views) if v.row.topic_id == keep), 0)
        if self.book is None or self.book.topic_id != self.focused_topic:
            self.load_book()
        if self.view == "heatmap" and not self.candles:
            self.load_candles()
        if self.view == "bots":
            self.scan_bots()
        self.paint()

    @property
    def focused_topic(self) -> int | None:
        return self.views[self.m_cur].row.topic_id if self.views and self.m_cur < len(self.views) else None

    @work(exclusive=True, group="book")
    async def load_book(self) -> None:
        tid = self.focused_topic
        if tid is None or self.view in ("bots", "heatmap"):
            return
        try:
            b = await self.api.book(tid)
        except ApiError as e:
            self.say(str(e))
            return
        if tid != self.focused_topic or not b.bins:
            return
        fresh = self.book is None or self.book.topic_id != tid
        self.book = b
        if fresh:
            self.anchor = None
            self.b_cur = max(range(len(b.probs)), key=lambda i: b.probs[i])
        self.paint()

    @work(exclusive=True, group="account")
    async def load_account(self) -> None:
        if not self.api.authenticated:
            return
        try:
            self.wallet, self.positions, self.summary = await asyncio.gather(
                self.api.wallet(), self.api.positions(), self.api.summary())
        except ApiError as e:
            self.say(str(e))
            return
        self.positions.sort(key=lambda p: (p.end_time, p.option_id))
        held = {(p.topic_id, p.option_id): p.shares for p in self.positions}
        if held != self.held:
            self.held, self.held_version = held, self.held_version + 1
        self.p_cur = min(self.p_cur, max(len(self.positions) - 1, 0))
        self.paint()

    @work(exclusive=True, group="candles")
    async def load_candles(self) -> None:
        if not self.views:
            return
        asset = self.asset
        try:
            hourly = self.hm_cadence < 86400
            got = await api_mod.candles(asset, 900 if hourly else 86400)      # Coinbase has no 30-minute bar: pair up 15s
            if hourly:
                got = api_mod.resample(got, CANDLE_S)
                try:                                # 300 hours: a week of history to set against a week of forecast
                    self.candles_long = await api_mod.candles(asset, 3600)
                except Exception:
                    self.candles_long = []
        except Exception:
            return                      # history is context; the forecast grid stands without it
        if asset != self.asset or not got:
            return
        first = not self.candles
        self.candles, self.candle_s = got, (CANDLE_S if hourly else 86400)
        if first and not self.hm_touched:
            self.hm_fit_pending = True
        self.paint()

    @work(exclusive=True, group="spot")
    async def load_spot(self) -> None:
        pair = api_mod.PAIRS.get(self.asset)
        if not pair:
            return
        asset, first = self.asset, self.spot <= 0
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(api_mod.COINBASE.format(pair=pair) + "/ticker")
                price = float(r.json()["price"])
        except Exception:
            return                      # spot is decoration; the market data does not depend on it
        if asset != self.asset:
            return                      # the series changed while this was in flight: a BTC price is not a SOL price
        self.spot = price
        if first and not self.hm_touched:
            self.hm_fit_pending = True  # the first fit had no spot to centre on

    # keys ───────────────────────────────────────────────────

    def on_key(self, e: events.Key) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        k, ch = e.key, e.character
        e.stop()
        if getattr(self.shell, "demo", False):          # any key stops the tour, and is otherwise swallowed
            self.shell.demo = False
            return
        if self.shell.go_focus:                         # the command list owns every key until Enter or Esc
            self.shell.go_key(k, ch)
            self.paint()
            return
        if self.shell.leader is not None:               # the key after SPC
            self.shell.leader_key(k, ch)
            self.paint()
            return
        if k == "space" and not self.pending and not self.slip_focus:
            self.shell.leader_start()                   # SPC: the leader menu, on every screen
            self.paint()
            return
        if self.view == "term":
            if self.shell.key(k, ch):
                self.paint()
                return
        elif ch == "t" and not self.pending and not self.slip_focus:
            self.shell.show()                           # t: the launchpad, from any of today's screens
            self.paint()
            return
        elif ch == "`" and not self.pending and self.view != "heatmap":
            self.shell.focus_go()                       # on the heatmap ` still jumps to a mark; : reaches the GO bar there
            self.paint()
            return
        if k in WASD and not self.pending:
            k, ch = WASD[k], None                       # w a s d are the arrow keys everywhere; ma, zh and 'a still chord
        if self.slip_focus and not self.pending and not (ch and ch.isdigit()) and self.slip_key(k, ch, int(self.count or 1)):
            self.count = ""
            self.paint()
            return
        if ch and ch.isdigit() and not self.pending and (ch != "0" or self.count):
            self.count = (self.count + ch)[-4:]          # a count: 5j, 12|
            self.paint()
            return
        n, counted = int(self.count or 1), bool(self.count)
        prefix, self.pending, self.count = self.pending, "", ""
        if prefix:
            self.chord(prefix, ch or "", n)
        elif ch in ("g", "z", "Z") or (ch in ("m", "'", "`") and self.view == "heatmap"):
            self.pending, self.count = ch, str(n) if counted else ""
        else:
            self.normal_key(k, ch, n, counted)
        self.paint()

    def chord(self, prefix: str, ch: str, n: int) -> None:
        """The second key of g, z, Z, m, ' and `."""
        hm = self.view == "heatmap" and bool(self.views)
        if prefix == "g":
            if ch == "g":
                self.move(-10**9)
            elif ch == "v" and hm and self.hm_last_sel:
                self.hm_anchor, (self.hm_col, self.hm_bin) = self.hm_last_sel
        elif prefix == "Z":
            if ch in ("Z", "Q"):
                self.exit()
        elif prefix == "z" and not hm:
            if ch == "z" and self.book:
                self.b_cur = max(range(len(self.book.probs)), key=lambda i: self.book.probs[i])
        elif prefix == "z":
            rows, cols, g = self.hm_cells_v, self.hm_cols_v, self.hm_bin // self.hm_bpc
            edge = min(2, rows // 4)
            if ch in ("z", "t", "b"):
                self.hm_gtop = g + {"z": rows // 2, "t": edge, "b": rows - 1 - edge}[ch]
            elif ch in ("h", "l", "H", "L"):
                self.hm_scroll((n if ch in "hl" else cols // 2) * (1 if ch in "lL" else -1), 0)
            elif ch in ("i", "o"):
                z = ZOOMS.index(self.hm_bpc) + (1 if ch == "o" else -1)
                self.hm_bpc = ZOOMS[max(0, min(z, len(ZOOMS) - 1))]
                self.hm_gtop = self.hm_bin // self.hm_bpc + rows // 2     # keep the cursor centred through the zoom
            elif ch == "a":
                self.hm_fit_pending = True
            elif ch == "f":
                self.hm_fit_all()
            elif ch == "m":
                self.hm_median = not self.hm_median
            self.hm_touched = True
        elif prefix == "m" and hm and ch.isalpha() and self.hm_col >= 0:
            self.marks[ch] = (self.views[self.hm_col].row.topic_id, self.hm_bin)
            self.say(f"mark {ch} set", 2)
        elif prefix in ("'", "`") and hm:
            here = (self.hm_col, self.hm_bin)
            if ch in ("'", "`") and self.hm_prev:
                (self.hm_col, self.hm_bin), self.hm_prev = self.hm_prev, here
            elif ch in self.marks:
                topic, b = self.marks[ch]
                col = next((i for i, v in enumerate(self.views) if v.row.topic_id == topic), None)
                if col is None:
                    self.say(f"mark {ch} is on a close that has ended")
                else:
                    self.hm_prev, self.hm_col, self.hm_bin = here, col, b
            elif ch.isalpha():
                self.say(f"mark {ch} not set")

    def hm_scroll(self, dcols: int, dcells: int) -> None:
        """Move the view, not the cursor; like vim, the cursor is dragged along only to stay on screen."""
        rows, chart_w, g = self.hm_cells_v, self.hm_chart_w, self.hm_grid
        n_hist, last = len(self.hm_hist), len(self.views) - 1
        edge, mg = min(2 * self.hm_w, chart_w // 4), min(2, rows // 4)
        self.hm_left = max(-n_hist, min(self.hm_next(self.hm_gstart(self.hm_left), dcols), last))
        while self.hm_col < last and self.hm_pos(self.hm_col) - self.hm_pos(self.hm_left) < edge:
            self.hm_col += 1
        while self.hm_col > -n_hist and self.hm_pos(self.hm_col) + self.hm_width(self.hm_col) - self.hm_pos(self.hm_left) > chart_w - edge:
            self.hm_col -= 1
        self.hm_gtop = max(min(rows - 1, g.cells - 1), min(self.hm_gtop + dcells, g.cells - 1))
        cell = max(self.hm_gtop - rows + 1 + mg, min(self.hm_bin // g.bpc, self.hm_gtop - mg))
        if cell != self.hm_bin // g.bpc:
            self.hm_bin = g.bins_of(cell)[0]
        self.hm_touched = True

    def hm_clear_selection(self) -> None:
        if self.hm_anchor is not None:
            self.hm_last_sel, self.hm_anchor = (self.hm_anchor, (self.hm_col, self.hm_bin)), None

    def goto_price(self, price: float) -> None:
        bins = self.views[0].bins if self.view == "heatmap" and self.views else self.book.bins if self.book else ()
        if not bins:
            return
        i = max(0, min(int((price - bins[0][0]) // (bins[0][1] - bins[0][0])), len(bins) - 1))
        if self.view == "heatmap":
            self.hm_prev, self.hm_bin, self.hm_touched = (self.hm_col, self.hm_bin), i, True
        else:
            self.b_cur, self.pane = i, 1

    @work
    async def command_line(self, price_only: bool = False) -> None:
        v = await self.push_screen_wait(Prompt(
            "/" if price_only else ":", "Go to a price, e.g. 78000." if price_only else
            "q   help   78000 (go to a price)   size 50   size 500s   buy   series eth   noh", placeholder="78000" if price_only else ""))
        cmd = (v or "").strip().lstrip(":/")
        if not cmd:
            return
        word, _, arg = cmd.partition(" ")
        number = cmd.replace(",", "").replace("$", "")
        if word in ("q", "q!", "qa", "wq", "x", "quit"):
            self.exit()
        elif word in ("h", "help"):
            self.push_screen(Help())
        elif number.replace(".", "", 1).isdigit():
            self.goto_price(float(number))
        elif word in ("size", "s") and arg:
            self.apply_size(arg)
        elif word in ("buy", "b", "bet"):
            self.buy_heatmap() if self.view == "heatmap" else self.buy()
        elif word == "series" and arg:
            want = arg.lower().split()
            hit = next((i for i, b in enumerate(self.batches) if all(x in f"{b.short} {b.title}".lower() for x in want)), None)
            if hit is None:
                self.say(f"No series matches '{arg}'. Try: {', '.join(b.short for b in self.batches)}")
            else:
                self.switch_batch(hit - self.batch_i)
        elif word in ("noh", "nohlsearch"):
            self.hm_clear_selection()
            self.anchor = None
        else:
            self.shell.run(cmd)                         # anything else is a GO bar command: MEMP, BTC, LP MACRO, TX <txid>
        self.paint()

    def help(self) -> None:
        self.push_screen(Help(TERM_HELP if self.view == "term" else HELP))

    def normal_key(self, k: str, ch: str | None, n: int, counted: bool) -> None:
        page = self.hm_cells_v if self.view == "heatmap" else 20
        if ch == "q" or k == "ctrl+c":
            self.exit()
        elif ch == "c":
            self.toggle_colors()
        elif ch == "?":
            self.help()
        elif ch == "/" and self.view == "bots":
            self.bot_search()
        elif ch == ":" or ch == "/":
            self.command_line(price_only=ch == "/")
        elif k == "ctrl+l":
            self.query_one("#heatmap", HeatmapPane)._key = None
            self.refresh(layout=True)
        elif ch == "L":
            self.login()
        elif ch == "O":
            self.logout()
        elif ch == "r":
            self.load_markets(); self.load_book(); self.load_account(); self.say("refreshed", 2)
            if self.view == "bots":
                self._scan_key = None
                self.scan_bots()
        elif ch == "p":
            self.view = "main" if self.view == "portfolio" else "portfolio"
            self.load_account()
        elif ch == "B":
            self.view = "main" if self.view == "bots" else "bots"
            if self.view == "bots":
                self.load_bots()
                self.scan_bots()
        elif ch == "f":
            if self.view == "heatmap":
                self.leave_heatmap()
            else:
                self.view, self.hm_col, self.hm_anchor = "heatmap", self.m_cur, None
                self.load_candles()
        elif ch == "o" and self.view != "main" and not (self.view == "heatmap" and self.hm_anchor is not None):
            self.view, self.pane = "main", 1            # o: the odds on every outcome of a close (o on a box swaps its corners)
            self.load_book()
        elif k == "escape":
            if self.view == "heatmap" and self.hm_anchor is not None:
                self.hm_clear_selection()
            elif self.view == "heatmap":
                self.leave_heatmap()
            elif self.view == "bots" and self.bot_pane == 1:
                self.bot_pane = 0
            elif self.view == "bots" and self.bot_filter:
                self.bot_filter, self.bot_cur = "", 0
            elif self.view != "main":
                self.view = "main"
            else:
                self.anchor = None
        elif ch == "G":
            self.move(10**9)
        elif k in ("j", "down"):
            self.move(n)
        elif k in ("k", "up"):
            self.move(-n)
        elif k in ("ctrl+d", "ctrl+u"):
            self.move((page // 2) * n * (1 if k == "ctrl+d" else -1))
        elif k in ("ctrl+f", "pagedown", "ctrl+b", "pageup"):
            self.move(page * n * (1 if k in ("ctrl+f", "pagedown") else -1))
        elif k == "tab" and self.view in ("main", "heatmap") and self.query_one("#slip").display and (
                self.view == "heatmap" or self.pane == 1):
            self.slip_focus = True          # markets → ladder → slip → markets; chart ↔ slip
            if self.view == "main":
                self.pane = 0
        elif self.view == "heatmap" and self.hm_key(k, ch, n, counted):
            pass
        elif self.view == "portfolio":
            if ch == "x":
                self.sell()
            elif k == "enter":
                self.open_position()
        elif self.view == "bots":
            if k == "enter":
                self.toggle_bot()
            elif ch == "i":
                self.about_bot()
            elif ch == "e":
                self.edit_bot_budget()
            elif ch == "x":
                self.stop_bot()
            elif ch == "X":
                self.stop_bot(everything=True)
            elif ch == "S":
                self.bot_sort, self.bot_cur = (self.bot_sort + 1) % len(SORTS), 0
            elif k in ("tab", "shift+tab"):
                self.bot_pane = 1 - self.bot_pane if self.query_one("#botdetail").display else 0
            elif k in ("l", "right", "h", "left") or ch in ("{", "}"):
                fwd = k in ("l", "right") or ch == "}"
                if self.bot_pane == 1:
                    self.move(self.bot_day * n * (1 if fwd else -1))
                elif ch not in ("{", "}"):
                    self.bot_tab, self.bot_cur = (self.bot_tab + (1 if fwd else -1)) % len(TABS), 0
            elif ch in ("[", "]"):
                self.switch_batch(1 if ch == "]" else -1)
        elif k in ("h", "left"):
            self.pane = 0
        elif k in ("l", "right"):
            self.pane = 1
        elif k == "tab":
            self.pane = 1 - self.pane
        elif ch == "[":
            self.switch_batch(-1)
        elif ch == "]":
            self.switch_batch(1)
        elif ch == "v":
            self.pane = 1
            self.anchor = None if self.anchor is not None else self.b_cur
        elif ch == "o" and self.anchor is not None:
            self.anchor, self.b_cur = self.b_cur, self.anchor
        elif ch in ("+", "="):
            self.contracts = min(self.contracts * 2, 1_000_000)
        elif ch in ("-", "_"):
            self.contracts = max(round(self.contracts / 2, 2), 0.01)
        elif ch == "S":
            self.set_size()
        elif ch == "b" or (k == "enter" and self.pane == 1):
            self.buy()
        elif k == "enter":
            self.pane = 1

    def move(self, d: int) -> None:
        if self.view == "heatmap":
            if not self.views:
                return
            self.hm_touched = True
            if abs(d) > 10**6 and self.hm_col >= 0:          # gg / G: the edges of this close's 80% band
                band = self.views[self.hm_col].band
                self.hm_bin = Grid(self.hm_grid.y_lo, self.hm_grid.bin_size, self.hm_grid.n, 1).cell_of_price(
                    band[1] - 1e-9 if d < 0 else band[0])
            else:
                self.hm_bin = max(0, min(self.hm_bin - d * self.hm_bpc, self.hm_grid.n - 1))
        elif self.view == "portfolio":
            self.p_cur = max(0, min(self.p_cur + d, len(self.positions) - 1))
        elif self.view == "bots":
            if self.bot_pane == 1:
                self.bot_when = max(0, min(self.bot_when + d, len(self.bot_closes) - 1))
            else:
                self.bot_cur = max(0, min(self.bot_cur + d, len(botsview.visible(self)) - 1))
        elif self.pane == 0:
            new = max(0, min(self.m_cur + d, len(self.views) - 1))
            if new != self.m_cur:
                self.m_cur = new
                self.load_book()
        elif self.book:
            self.b_cur = max(0, min(self.b_cur - d, len(self.book.bins) - 1))   # down the screen is down in price

    def switch_batch(self, d: int) -> None:
        if not self.batches:
            return
        self.batch_i = (self.batch_i + d) % len(self.batches)
        auth.save_state({**auth.load_state(), "series": self.batch.short})    # reopen on the series you were last watching
        self.views, self.book, self.m_cur, self.anchor, self.spot = [], None, 0, None, 0.0
        self.bot_ahead, self._ahead_for, self.bot_when, self.bot_pane = {}, None, 0, 0
        self.candles, self.hm_col, self.hm_anchor, self.hm_fit_pending, self.hm_touched = [], 0, None, True, False
        self.candles_long, self._hm_hist = [], None
        self.load_markets()
        self.load_spot()

    def leave_heatmap(self) -> None:
        self.view = "main"
        if self.hm_col >= 0 and self.hm_col != self.m_cur:
            self.m_cur = self.hm_col                     # come back to the close you were looking at
        self.load_book()

    # actions ────────────────────────────────────────────────

    @work
    async def login(self) -> None:
        await self._login()

    async def _login(self) -> bool:
        key = await self.push_screen_wait(Prompt(
            "LOG IN", "Paste your Glimpse API key. It is stored in your OS keychain, never shown or logged.\n"
            "No key yet? Register at glimpse.markets → Settings → Developer API Keys.",
            password=True, placeholder="glp_live_…"))
        if not key:
            return False
        if not auth.looks_like_key(key):
            self.say("That does not look like an API key.")
            return False
        probe = Glimpse(key.strip(), os.environ.get("GLIMPSE_BASE_URL"))
        try:
            wallet = await probe.wallet()
        except ApiError as e:
            await probe.close()
            self.say(str(e))
            return False
        where = auth.save_key(key)
        await self.api.close()
        self.api, self.wallet, self.key_mask, self.key_source = probe, wallet, auth.mask(key.strip()), where
        for r in self.runners.values():
            r.stop()
        self.runners.clear()
        self.say(f"Logged in. Key saved to {'the OS keychain' if where == 'keychain' else '~/.config/glimpse/credentials (0600)'}.")
        self.load_account()
        return True

    @work
    async def logout(self) -> None:
        if not self.api.authenticated:
            return
        if self.key_source == "env":
            self.say(f"Key comes from ${auth.ENV_VAR}; unset it in your shell to log out.")
            return
        if not await self.push_screen_wait(Confirm("LOG OUT", Text("Remove the stored API key from this machine?"))):
            return
        for r in self.runners.values():
            r.stop()
        self.runners.clear()
        auth.forget_key()
        await self.api.close()
        self.api = Glimpse(None, os.environ.get("GLIMPSE_BASE_URL"))
        self.wallet = self.summary = None
        self.positions, self.held, self.key_mask = [], {}, "-"
        self.say("Logged out. Key removed.")

    @work
    async def set_size(self) -> None:
        v = await self.push_screen_wait(Prompt(
            "SIZE", "Contracts per bin (e.g. 50), or a total budget in sats with an s (e.g. 500s).",
            placeholder=fmt.contracts(self.contracts)))
        if v:
            self.apply_size(v)

    @work
    async def apply_size(self, v: str) -> None:
        if not (self.book or self.view == "heatmap"):
            return
        v = v.strip().lower().replace(",", "").replace("₿", "")
        try:
            if v.endswith("s") and self.view == "heatmap":
                sel = self.hm_selection
                if sel is None:
                    raise ValueError
                n = await asyncio.to_thread(P.bisect_budget, lambda c: self.hm_price(sel, c).cost_sats, float(v[:-1]))
            elif v.endswith("s"):
                lo, hi = self.selection
                n = P.contracts_for_budget(self.book.shares, self.book.alpha, lo, hi, float(v[:-1]))
            else:
                n = float(v)
            if n <= 0:
                raise ValueError
        except ValueError:
            self.say("Enter a positive number, or a budget like 500s.")
            return
        self.contracts = round(n, 2)
        self.paint()

    @work
    async def buy(self) -> None:
        b, tk = self.book, self.ticket
        if b is None or tk is None:
            return
        if not self.api.authenticated:
            self.say("Read-only. Press L to log in with an API key (glimpse.markets → Settings → Developer API Keys).")
            return
        if not b.is_live:
            self.say("This market is closed.")
            return
        lo, hi = self.selection
        body = Text()
        body.append(f"{fmt.question(self.asset, b.end_time_utc)}\n", style=DIM)
        body.append(f"{fmt.span(b.bins[lo][0], b.bins[hi][1])}", style=f"bold {TEXT}")
        body.append(f"   {fmt.contracts(tk.contracts)} contracts × {tk.bins} bins\n\n")
        body.append(f"cost    {fmt.sats(tk.cost_sats):>12}   (fee {fmt.sats(tk.fee_sats)} included)\n")
        body.append(f"payout  {fmt.sats(tk.payout_sats):>12}   if the close lands in range ({fmt.pct(tk.prob)})\n", style=ORANGE)
        body.append(f"profit  {fmt.sats(tk.profit_sats, signed=True):>12}   {fmt.odds(tk.odds)}  {fmt.roi(tk.roi)}\n",
                    style=GREEN if tk.profit_sats > 0 else RED)
        body.append(f"lose    {fmt.sats(tk.cost_sats):>12}   otherwise", style=DIM)
        if not await self.push_screen_wait(Confirm("BUY", body)):
            return
        try:
            cost, fee = await self.api.estimate(b, lo, hi, tk.contracts)
            if abs(cost + fee - tk.cost_sats) > ESTIMATE_TOLERANCE * tk.cost_sats + 5:
                self.say(f"Price moved: now {fmt.sats(cost + fee)}, was {fmt.sats(tk.cost_sats)}. Not sent. Review and press b again.")
                self.load_book()
                return
            fill = await self.api.buy(b, lo, hi, tk.contracts)
        except ApiError as e:
            self.say(str(e), 12)
            self.load_account()
            return
        self.anchor = None
        self.say(f"Filled. Paid {fmt.sats(fill.cost_sats + fill.fee_sats)} for {fmt.span(b.bins[lo][0], b.bins[hi][1])}.", 10)
        self.load_book()
        self.load_account()

    @work
    async def buy_heatmap(self) -> None:
        sel, tk = self.hm_selection, self.ticket
        if sel is None or tk is None:
            self.say(self.ticket_hint or "Nothing to buy here.")
            return
        if not self.api.authenticated:
            self.say("Read-only. Press L to log in with an API key (glimpse.markets → Settings → Developer API Keys).")
            return
        if not self.ticket_live:
            self.say("A selected market has closed.")
            return
        c0, c1, b0, b1 = sel
        views, bins = self.views[c0:c1 + 1], self.views[0].bins
        orders = [Order(v.row.topic_id, tuple(v.row.option_ids[b0:b1 + 1]), tk.contracts) for v in views]
        short = self.hm_cadence < 86400
        body = Text()
        body.append(fmt.question(self.asset, views[0].row.end_time_utc), style=DIM)
        if len(views) > 1:
            body.append(f" → {fmt.close_label(views[-1].row.end_time_utc, short)} UTC · {len(views)} closes", style=DIM)
        body.append(f"\n{fmt.span(bins[b0][0], bins[b1][1])}", style=f"bold {TEXT}")
        closes = f" × {len(views)} closes" if len(views) > 1 else ""
        body.append(f"   {fmt.contracts(tk.contracts)} contracts × {tk.bins} bins{closes}\n\n")
        body.append(f"cost    {fmt.sats(tk.cost_sats):>12}   (fee {fmt.sats(tk.fee_sats)} included)\n")
        if len(views) > 1:
            body.append(f"payout  {fmt.sats(tk.payout_sats / len(views)):>12}   per close that lands in range\n", style=ORANGE)
            body.append(f"max     {fmt.sats(tk.payout_sats):>12}   if all {len(views)} land ({fmt.pct(tk.prob)})   {fmt.odds(tk.odds)}\n",
                        style=ORANGE)
        else:
            body.append(f"payout  {fmt.sats(tk.payout_sats):>12}   if the close lands in range ({fmt.pct(tk.prob)})   {fmt.odds(tk.odds)}\n",
                        style=ORANGE)
        body.append(f"lose    {fmt.sats(tk.cost_sats):>12}   if none do", style=DIM)
        if not await self.push_screen_wait(Confirm("BUY", body)):
            return
        try:
            ests = []
            for i in range(0, len(orders), MARKETS_PER_REQUEST):        # the public limiter allows 240 a minute
                ests += await asyncio.gather(*(self.api.estimate_order(o) for o in orders[i:i + MARKETS_PER_REQUEST]))
            now = sum(c + f for c, f in ests)
            if abs(now - tk.cost_sats) > ESTIMATE_TOLERANCE * tk.cost_sats + 5:
                self.say(f"Price moved: now {fmt.sats(now)}, was {fmt.sats(tk.cost_sats)}. Not sent. Review and press b again.")
                self.load_markets()
                return
            fills = []
            for i in range(0, len(orders), MARKETS_PER_REQUEST):
                fills += await self.api.buy_orders(orders[i:i + MARKETS_PER_REQUEST])
        except ApiError as e:
            self.say(f"{e} Sent so far: {len(fills)} of {len(orders)} closes." if fills else str(e), 15)
            self.load_account()
            return
        ok = [f for f in fills if not f.error]
        paid = sum(f.cost_sats + f.fee_sats for f in ok)
        if len(ok) == len(fills):
            self.hm_anchor = None
            self.say(f"Filled {len(ok)} close{'s' if len(ok) > 1 else ''}. Paid {fmt.sats(paid)}.", 10)
        else:
            bad = next(f for f in fills if f.error)
            self.say(f"Filled {len(ok)} of {len(fills)} closes, paid {fmt.sats(paid)}. First rejection: {bad.error}", 15)
        self.load_markets()
        self.load_account()

    @work
    async def sell(self) -> None:
        if not self.positions:
            return
        p = self.positions[self.p_cur]
        body = Text()
        body.append(f"{position_question(p)}\n", style=DIM)
        body.append(f"{p.option_name.replace('-', '–')}   {fmt.contracts(p.shares)} contracts\n\n", style=f"bold {TEXT}")
        body.append(f"paid {fmt.sats(p.cost_sats)} · worth about {fmt.sats(p.value_sats * (1 - P.FEE))} after the 2% exit fee\n")
        body.append("Sells the whole position at the current market price.", style=DIM)
        if not await self.push_screen_wait(Confirm("SELL", body)):
            return
        try:
            await self.api.sell(p.topic_id, p.option_id)
        except ApiError as e:
            self.say(str(e), 12)
        else:
            self.say(f"Sold {p.option_name.replace('-', '–')}.")
        self.load_account()
        self.load_book()

    def open_position(self) -> None:
        if not self.positions:
            return
        p = self.positions[self.p_cur]
        i = next((i for i, v in enumerate(self.views) if v.row.topic_id == p.topic_id), None)
        if i is None:
            self.say("That market is in another series: use [ ] to switch.")
            return
        self.m_cur, self.view, self.pane = i, "main", 1
        self.load_book()

    # bots ─────────────────────────────────────────────────

    def feed(self, asset: str | None = None):
        from .zoo.data import Feed

        asset = asset or self.asset
        if asset not in self.feeds:
            self.feeds[asset] = Feed(asset)
        return self.feeds[asset]

    def bot_budget(self, bot_id: str) -> float:
        saved = auth.load_state().get("bot_budgets", {})
        return float(saved.get(bot_id) or bots.BotConfig.load().bankroll_sats)

    def _selected_bot(self) -> bots.Bot | None:
        shown = botsview.visible(self)
        return shown[max(0, min(self.bot_cur, len(shown) - 1))] if shown else None

    @work(exclusive=True, group="bots")
    async def load_bots(self) -> None:
        if self.bots:
            return
        try:
            self.bots = await asyncio.to_thread(bots.discover)       # imports numpy, pandas and scipy: not on the UI thread
        except Exception as e:
            self.bots_error = f"Could not load the model zoo: {e}"
        self.paint()
        self.scan_bots()

    @work(group="scan")
    async def scan_bots(self) -> None:
        """Ask every bot what it thinks of the nearest close. One background pass per new bar, close or series; the
        models cache their own signals, so only the first pass after a new hour does real work."""
        if self.bot_scanning or not self.bots or not self.views:
            return
        view = next((v for v in self.views if v.row.end_time_utc - time.time() > 90), None)
        if view is None:
            return
        feed = self.feed()
        self.bot_scanning = True
        try:
            await feed.refresh(max_age=60)
            if not feed.ready:
                return
            key = (feed.asset, view.row.topic_id, feed.bars.index[-1])
            if key == self._scan_key:
                return
            asset = feed.asset
            book = book_of(view)
            market = P.signal(book.probs)
            ctx = feed.ctx(book.bins, book.end_time_utc)
            self.bot_scan = {}
            self.bot_ahead, self._ahead_for = {}, None       # new bar: every picture ahead is stale
            for i, b in enumerate(self.bots):
                if asset != self.asset:
                    return                          # the series changed mid-pass: that pass is for another market
                self.bot_scan[b.id] = await asyncio.to_thread(scan_one, b, book, ctx, market)
                self.bot_ahead[b.id, view.row.topic_id] = self.bot_scan[b.id]
                if i % 12 == 11 and self.view == "bots":
                    self.paint()
            self._scan_key = key
        except Exception as e:
            self.say(f"Could not read the market for the bots: {e}")
        finally:
            self.bot_scanning = False
            self.paint()

    @property
    def bot_closes(self) -> list[RowView]:
        """Every close still far enough out to forecast: what the pane on the right walks through."""
        return [v for v in self.views if v.row.end_time_utc - time.time() > 90]

    @property
    def bot_day(self) -> int:
        """Closes to a day on this series: what h and l jump in the forecast."""
        c = self.bot_closes
        gap = c[1].row.end_time_utc - c[0].row.end_time_utc if len(c) > 1 else 3600
        return max(round(86400 / gap), 1) if gap < 86400 else 7

    def ahead_of(self, bot: bots.Bot | None, view: RowView | None) -> Scan | None:
        """One bot's picture of one close, if it has been computed yet."""
        if bot is None or view is None:
            return None
        return self.bot_ahead.get((bot.id, view.row.topic_id))

    def bot_pictures(self, bot: bots.Bot) -> dict[int, Scan]:
        """Every close this bot has drawn so far, by topic id: what the time series chart plots."""
        return {topic: s for (bid, topic), s in self.bot_ahead.items() if bid == bot.id}

    def bot_history(self) -> list[Candle]:
        """The hourly candles the bots read, as the chart draws them. Rebuilt only when a new bar has closed."""
        feed = self.feeds.get(self.asset)
        bars = getattr(feed, "bars", None)
        if bars is None or not len(bars):
            return []
        key = (self.asset, bars.index[-1], len(bars))
        if getattr(self, "_bot_hist_key", None) != key:
            t = bars.index.as_unit("s").asi8.tolist()          # the index may be kept in seconds or nanoseconds
            o, h, lo, c = (bars[k].to_numpy(dtype=float).tolist() for k in ("open", "high", "low", "close"))
            self._bot_hist_key = key
            self._bot_hist = [Candle(int(t[i]), o[i], h[i], lo[i], c[i]) for i in range(len(t))]
        return self._bot_hist

    def ensure_ahead(self) -> None:
        """Keep the selected bot's walk ahead current: one background pass per bot, per data refresh."""
        b = self._selected_bot()
        want = (b.id, len(self.views), self._scan_key) if b else None
        if want != self._ahead_for:
            self._ahead_for = want
            if b is not None:
                self.walk_ahead(b)

    @work(exclusive=True, group="ahead")
    async def walk_ahead(self, bot: bots.Bot) -> None:
        """What this bot expects of every close ahead, nearest first, so the forecast draws as it fills. The models
        cache their signals per bar, so the closes after the first cost almost nothing."""
        closes = self.bot_closes
        if bot.error or not closes:
            return
        feed = self.feed()
        try:
            await feed.refresh(max_age=60)
            if not feed.ready:
                self._ahead_for = None
                return
            asset = feed.asset
            for i, v in enumerate(closes):
                if asset != self.asset or self._selected_bot() is not bot:
                    return                          # the series or the bot changed: this pass is for another picture
                if (bot.id, v.row.topic_id) not in self.bot_ahead:
                    book = book_of(v)
                    self.bot_ahead[bot.id, v.row.topic_id] = await asyncio.to_thread(
                        scan_one, bot, book, feed.ctx(book.bins, book.end_time_utc), P.signal(book.probs))
                    if self.view == "bots" and (i < 3 or i % 6 == 5):
                        self.paint()
        except Exception as e:
            self._ahead_for = None
            self.say(f"Could not read the closes ahead: {e}")
        finally:
            if self.view == "bots":
                self.paint()

    def about_bot(self) -> None:
        b = self._selected_bot()
        if b is None:
            return
        v = self.bot_closes[max(0, min(self.bot_when, len(self.bot_closes) - 1))] if self.bot_closes else None
        body = botsview.about(b, self.ahead_of(b, v), self.spot, min(self.size.width - 8, 128))
        when = f" · {fmt.question(self.asset, v.row.end_time_utc)}" if v else ""
        self.push_screen(About(body, f"ABOUT · {b.name}{when}"))

    @work
    async def bot_search(self) -> None:
        v = await self.push_screen_wait(Prompt("/", "Search bots by name, family or idea: trend, reversion, tails, options, garch…",
                                               placeholder=self.bot_filter))
        if v is not None:
            self.bot_filter, self.bot_cur, self.bot_tab = v.strip(), 0, 0 if v.strip() else self.bot_tab
            self.paint()

    @work
    async def toggle_bot(self) -> None:
        b = self._selected_bot()
        if b is None or b.error or not self.batch:
            return
        r = self.runners.get(b.id)
        if r and r.running:
            r.stop()
            self.paint()
            return
        every = bots.BotConfig.load().interval_s // 60
        v = await self.push_screen_wait(Prompt(
            f"RUN · {b.name}", f"{b.blurb}\n\nRuns on this computer while the terminal is open, checking {self.asset} every {every} minutes.\n"
            "Budget in sats: the most it may have at risk. Small is fine. Enter accepts the figure shown.",
            placeholder=f"{self.bot_budget(b.id):,.0f}"))
        if v is None:
            return
        budget = self._parse_budget(v, self.bot_budget(b.id))
        if budget is None:
            return
        live = False
        if self.api.authenticated:
            c = bots.BotConfig.load().with_budget(budget)
            typed = await self.push_screen_wait(Prompt(
                "REAL SATS OR PAPER?", f"Type LIVE to let {b.name} trade real sats through your API key {self.key_mask}: at most "
                f"{fmt.sats(c.bankroll_sats)} at risk, {fmt.sats(c.max_per_cycle_sats)} per cycle, {fmt.sats(c.max_per_hour_sats)} per hour.\n"
                "Press enter alone to practise on paper instead.", placeholder="LIVE"))
            if typed is None:
                return
            live = typed.strip() == "LIVE"
        self.bot_live[b.id] = live
        self._save_budget(b.id, budget)
        self.runners[b.id] = r = bots.Runner(
            bot=b, api=self.api, batch_id=self.batch.batch_id, feed=self.feed(), live=live, ledger=self.ledger,
            cfg=bots.BotConfig.load().with_budget(budget), series=self.batch.short, on_trade=self.load_account)
        r.start()
        self.say(f"{b.name} is running on {self.asset} with {fmt.sats(budget)}: " + ("LIVE, real sats." if live else
                 "on paper." + ("" if self.api.authenticated else " Press L to log in with your API key for real trading.")), 10)

    def _parse_budget(self, v: str, default: float) -> float | None:
        v = v.strip().lower().replace(",", "").replace("₿", "").removesuffix("sats").removesuffix("s").strip()
        if not v:
            return default
        try:
            n = float(v[:-1]) * 1000 if v.endswith("k") else float(v)
        except ValueError:
            n = 0
        if n < 10:
            self.say("Enter a budget in sats, like 5000 or 20k.")
            return None
        return n

    def _save_budget(self, bot_id: str, sats: float) -> None:
        state = auth.load_state()
        auth.save_state({**state, "bot_budgets": {**state.get("bot_budgets", {}), bot_id: sats}})

    @work
    async def edit_bot_budget(self) -> None:
        b = self._selected_bot()
        if b is None:
            return
        if b.id in self.runners and self.runners[b.id].running:
            self.say("Stop the bot before changing its budget.")
            return
        v = await self.push_screen_wait(Prompt(f"BUDGET · {b.name}", "The most this bot may have at risk, in sats. Small is fine.",
                                               placeholder=f"{self.bot_budget(b.id):,.0f}"))
        budget = self._parse_budget(v, self.bot_budget(b.id)) if v is not None else None
        if budget is not None:
            self._save_budget(b.id, budget)
            self.paint()

    def stop_bot(self, everything: bool = False) -> None:
        b = self._selected_bot()
        hit = [r for k, r in self.runners.items() if r.running and (everything or (b and k == b.id))]
        for r in hit:
            r.stop()
        self.say(f"Stopped {len(hit)} bot{'s' if len(hit) != 1 else ''}." if hit else "Nothing to stop.", 3)

    async def on_unmount(self) -> None:
        for r in self.runners.values():
            r.stop()
        await self.shell.stop()
        await self.api.close()
