"""The bots on the front page. `CONS` is what the whole zoo expects; `BOT` is one model at a time.

Glimpse is a market you can bring a machine to, so the terminal opens with the machines. Every model in the zoo
runs on this computer from public hourly candles: CONS asks all of them about the next hour, the next day and the
next three days and counts the bulls against the bears, then draws the one picture they make together beside the
market's own. BOT takes a single model and shows its picture of the nearest close against what the market charges
for the same ranges, with [ and ] to walk the zoo. Nothing here trades: enter opens the zoo, where a bot can be run.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from rich.text import Text

from .. import botsview, fmt
from ..term import ui
from ..term.registry import Function, register
from ..term.swarm import BEAR, BULL, FLAT
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT
from .forecast import NAMES, FcstPane

MARK = {BULL: ("▲", GREEN), BEAR: ("▼", RED), FLAT: ("·", DIM)}
SHADE = {"bullish": GREEN, "bearish": RED, "volatile": ORANGE, "sideways": ORANGE, "neutral": DIM}


class SwarmPane(FcstPane):
    """The closes of a series, and every bot's read of them. The closes come the way FCST gets them."""
    wants_candles = False
    every, tick = 60, 5.0

    @property
    def swarm(self):
        return self.hub.swarm(self.asset)

    async def load(self) -> None:
        await super().load()
        await self.swarm.refresh(self.asset, self.views, self.cadence, self.bump)
        self.title = self.caption(self.cadence < 86400)         # the zoo is counted once it has loaded

    def cache_key(self) -> tuple:
        s = self.swarm
        return (len(s.pictures), s.at, s.busy, len(s.bots), s.error, round(self.spot(), 2), int(time.time() // 30))

    def waiting(self, w: int, h: int) -> list[Text] | None:
        """The page before the first pass has finished: say what is happening, never an empty window."""
        s = self.swarm
        if s.pictures:
            return None
        if s.error:
            return ui.centre([s.error], w, h, RED)
        n = len(s.bots)
        return ui.centre([f"reading {n} bots…" if n else "loading the model zoo…",
                          "They run here, on this computer, from public hourly candles."], w, h, FAINT)

    def when(self, pic) -> str:
        return datetime.fromtimestamp(pic.end, UTC).strftime("%H:%M" if self.cadence < 86400 else "%a %d %b")

    def menu(self) -> list[tuple[str, str]]:
        return [("ZOO", "BOTS"), ("FORECAST", f"FCST {self.asset}"), ("ODDS", f"DIST {self.asset}")]


class ConsPane(SwarmPane):
    """CONS: how many bots lean which way, and the picture they make together against the market's."""
    code = "CONS"

    def caption(self, hourly: bool) -> str:
        n = len(self.hub.swarm(self.asset).bots)
        return f"{NAMES.get(self.asset, self.asset)} · what {n if n else 'the'} bots expect"

    def draw(self, w: int, h: int) -> list[Text]:
        if (wait := self.waiting(w, h)) is not None:
            return wait
        s, spot = self.swarm, self.spot()
        pics = s.pictures
        out = [self._headline(pics[0], spot, w)]
        room = h - len(out)
        rows = 4 if room >= 4 * len(pics) + len(pics) else 3 if room >= 3 * len(pics) + len(pics) else 2
        for pic in pics:
            if h - len(out) < rows:
                break
            out.append(Text(""))
            out += self._block(pic, spot, w, rows)
        if h - len(out) >= 5:
            out += [Text(""), *self._extremes(pics[0], w)]
        return out

    def _extremes(self, pic, w: int) -> list[Text]:
        """The three models worth opening in BOT: the two ends of the zoo, and the one furthest from the market."""
        good = [(bid, s) for bid, s in pic.scans.items() if not s.error and s.probs]
        if not good:
            return []
        names = {b.id: b.name for b in self.swarm.bots}
        picks = [("most bullish", max(good, key=lambda x: x[1].lean), GREEN),
                 ("most bearish", min(good, key=lambda x: x[1].lean), RED),
                 ("furthest from the market", max(good, key=lambda x: x[1].gap), ORANGE)]
        out = [ui.section("WHO STANDS OUT", w, f"on the {pic.label} · BOT opens one")]
        for label, (bid, sc), colour in picks:
            out.append(ui.fit([ui.t((f"  {label:<26}", FAINT), (f"{names.get(bid, bid)[:26]:<26}", f"bold {TEXT}")),
                               ui.t((f"   {fmt.price(sc.median):>9}", colour),
                                    (f"   {fmt.price(sc.low)} – {fmt.price(sc.high)}", DIM),
                                    (f"   {sc.gap:.0%} from the market", FAINT))], w))
        return out

    def _headline(self, pic, spot: float, w: int) -> Text:
        """`Now 81,010   130 of 130 bots priced the next hour   they agree 74%   apart from the market 12%`."""
        s = self.swarm
        parts = [ui.t(("Now ", FAINT), (fmt.price(spot) if spot else "–", f"bold {TEXT}")),
                 ui.t(("   ", ""), (f"{pic.n}", f"bold {ORANGE}"), (f" of {len(s.bots)} bots priced the ", FAINT),
                      (pic.label, f"bold {TEXT}"))]
        if pic.agreement:
            parts.append(ui.t(("   they agree ", FAINT), (f"{pic.agreement:.0%}", f"bold {TEXT}")))
        if pic.gap:
            parts.append(ui.t(("   apart from the market ", FAINT), (f"{pic.gap:.0%}", f"bold {ORANGE}")))
        if s.busy:
            parts.append(Text("   reading…", style=FAINT))
        return ui.fit(parts, w)

    def _block(self, pic, spot: float, w: int, rows: int) -> list[Text]:
        out = [ui.section(pic.label.upper(), w, f"{self.when(pic)} · in {fmt.countdown(int(pic.end))}")]
        out.append(self._counts(pic, w))
        if rows >= 4:
            out.append(self._bar(pic, w))
        if rows >= 3:
            out.append(self._numbers(pic, spot, w))
        return out

    def _counts(self, pic, w: int) -> Text:
        """`▲ 61 bullish    · 21 neutral    ▼ 48 bearish        12 volatile · 9 sideways`."""
        parts = []
        for stance in (BULL, FLAT, BEAR):
            glyph, colour = MARK[stance]
            parts.append(ui.t((f"  {glyph} ", colour), (f"{pic.counts.get(stance, 0):>3}", f"bold {colour}"),
                              (f" {stance}", DIM)))
        shades = " · ".join(f"{pic.shades[k]} {k}" for k in ("volatile", "sideways") if pic.shades.get(k))
        if shades:
            parts.append(ui.t(("      of the neutral: ", FAINT), (shades, DIM)))
        return ui.fit(parts, w)

    def _bar(self, pic, w: int) -> Text:
        """One bar of the whole zoo: green bulls, grey middle, red bears, in proportion."""
        room = max(w - 4, 10)
        out = Text("  ", no_wrap=True, overflow="crop")
        n = pic.n or 1
        widths = {k: round(room * pic.counts.get(k, 0) / n) for k in (BULL, FLAT, BEAR)}
        for stance, glyph, colour in ((BULL, "█", GREEN), (FLAT, "▒", DIM), (BEAR, "█", RED)):
            out.append(glyph * widths[stance], style=colour)
        return out

    def _numbers(self, pic, spot: float, w: int) -> Text:
        """`bots 81,120 +0.1%  80,900 – 81,350 (80%)    market 81,000  80,800 – 81,400`."""
        lean = pic.lean(spot)
        bots = ui.t(("  bots ", FAINT), (fmt.price(pic.median), f"bold {ORANGE}"), ("  ", ""),
                    ui.signed_pct(lean) if lean is not None else "",
                    (f"   {fmt.price(pic.band[0])} – {fmt.price(pic.band[1])}", DIM), (" (80%)", FAINT))
        market = ui.t(("    market ", FAINT), (fmt.price(pic.market_median), f"bold {TEXT}"),
                      (f"   {fmt.price(pic.market_band[0])} – {fmt.price(pic.market_band[1])}", DIM))
        side = ""
        if pic.market_median and pic.median:
            d = pic.median / pic.market_median - 1
            side = ui.t(("    ", ""), ("in line with the market", DIM) if abs(d) < 0.0005 else
                        (f"bots {abs(d) * 100:.1f}% {'above' if d > 0 else 'below'} the market", GREEN if d > 0 else RED))
        return ui.fit([bots, market, side] if side else [bots, market], w)

    def enter(self) -> str | None:
        return "BOTS"

    def hint(self) -> str:
        return "enter the model zoo · B bots"

    def command(self) -> str:
        return f"CONS {self.asset}"

    def export(self):
        rows = [[p.label, datetime.fromtimestamp(p.end, UTC).strftime("%Y-%m-%d %H:%M"), p.counts.get(BULL, 0),
                 p.counts.get(FLAT, 0), p.counts.get(BEAR, 0), round(p.median, 2), round(p.band[0], 2), round(p.band[1], 2),
                 round(p.market_median, 2)] for p in self.swarm.pictures]
        return ["horizon", "close_utc", "bullish", "neutral", "bearish", "bots_median", "band80_low", "band80_high",
                "market_median"], rows


class BotPane(SwarmPane):
    """BOT: one model's picture of the nearest close against the market's prices. [ and ] walk the zoo."""
    code = "BOT"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.at = 0                         # which bot of the zoo is showing
        self.pinned = next((a for a in self.args if not a.isdigit() and a.upper() not in NAMES and a.upper() != "1D"), "")

    def bot(self):
        s = self.swarm
        if not s.bots:
            return None
        if self.pinned:                     # `BOT BTC momentum_30d` opens on one model, once the zoo has loaded
            i = next((i for i, b in enumerate(s.bots) if b.id == self.pinned), None)
            self.at, self.pinned = (self.at if i is None else i), ""
        self.at %= len(s.bots)
        return s.bots[self.at]

    def caption(self, hourly: bool) -> str:
        return f"{NAMES.get(self.asset, self.asset)} · one bot at a time"

    def cache_key(self) -> tuple:
        return (*super().cache_key(), self.at)

    def draw(self, w: int, h: int) -> list[Text]:
        if (wait := self.waiting(w, h)) is not None:
            return wait
        s = self.swarm
        bot = self.bot()
        if bot is None:
            return ui.centre(["no bots"], w, h, FAINT)
        pics = s.pictures
        scan = pics[0].scans.get(bot.id)
        out = [self._who(bot, scan, w)]
        out += ui.wrap(bot.blurb, w, DIM, indent="  ")[:2]
        out.append(Text(""))
        if scan is not None and scan.error:
            out.append(ui.note(f"  this model could not price the close: {scan.error}", RED))
            return out
        band, market = w >= 56, w >= 64                     # a narrow window drops the 80% band, then the market
        head = f"  {'this bot':<15}{'median':>8}{'move':>8}" + (f"   {'80% band':<19}" if band else "") + \
               (f"{'market':>8}" if market else "")
        out.append(ui.note(head))
        for pic in pics:
            if (sc := pic.scans.get(bot.id)) is not None and not sc.error:
                out.append(self._horizon(pic, sc, w, band, market))
        out.append(Text(""))
        rows = h - len(out) - 2
        if scan is not None and scan.probs and rows >= 5:
            out += self._ladder(scan, pics[0], w, min(rows - 1, 13))
        return out

    def _ladder(self, scan, pic, w: int, rows: int) -> list[Text]:
        """The model's whole picture of the close against what the market charges for the same ranges, on one bar:
        orange where they agree, green where the model puts more on a range than the market does, which is what a
        running bot buys, and red where the market charges more than the model thinks the range is worth."""
        probs, market, bins, spot = list(scan.probs), list(pic.market), list(pic.bins), self.spot()
        lo, hi, step = botsview.window(probs, rows)
        groups = [(i, min(i + step - 1, hi)) for i in range(lo, hi + 1, step)]
        if not groups:
            return []
        mine = [sum(probs[a:b + 1]) for a, b in groups]
        theirs = [sum(market[a:b + 1]) for a, b in groups] if market else [0.0] * len(groups)
        top = max(max(mine), max(theirs), 1e-9)
        label_w = max(len(fmt.span(bins[a][0], bins[b][1])) for a, b in groups)
        show = w >= label_w + 32
        bar_w = max(w - label_w - (22 if show else 13), 6)
        out = [ui.note(f"  {'price at close':<{label_w}}{'bot':>8}" + (f"{'market':>8}" if show else "") +
                       ("   " + (f"{step} ranges a row" if step > 1 else "")).rstrip())]
        for i in range(len(groups) - 1, -1, -1):
            a, b = groups[i]
            here = bins[a][0] <= spot < bins[b][1]
            line = ui.t(("▸ " if here else "  ", f"bold {ORANGE}"),
                        (f"{fmt.span(bins[a][0], bins[b][1]):<{label_w}}", f"bold {TEXT}" if here else TEXT),
                        (f"{fmt.pct(mine[i]):>8}", f"bold {ORANGE}"), (f"{fmt.pct(theirs[i]):>8}" if show else "", TEXT), "  ")
            line.append_text(botsview.overlay(mine[i] / top, theirs[i] / top, bar_w))
            out.append(line)
        out.append(ui.fit([ui.t(("  █", ORANGE), (" both", FAINT), ("  █", GREEN), (" the bot buys", FAINT),
                                ("  ░", RED), (" dearer than the bot", FAINT))], w))
        return out

    def _who(self, bot, scan, w: int) -> Text:     # not _name: Textual's Widget keeps one of its own
        """The model's name, its family and which way it leans, with its place in the zoo on the right."""
        stance = Text("")
        if scan is not None and scan.view:
            stance = ui.t(("  ", ""), (botsview.VIEW_MARK.get(scan.view, ("·", DIM))[0], SHADE.get(scan.view, DIM)),
                          (f" {scan.view}", f"bold {SHADE.get(scan.view, DIM)}"))
        right = Text(f"bot {self.at + 1} of {len(self.swarm.bots)}", style=FAINT)
        if right.cell_len + 12 > w:
            right = Text("")
        left = ui.fit([ui.t((bot.name[:max(w - right.cell_len - 2, 8)], f"bold {TEXT}")), ui.t((f"  {bot.family}", FAINT)), stance],
                      max(w - right.cell_len - 1, 8))
        return ui.t(left, (" " * max(w - left.cell_len - right.cell_len, 1), ""), right)

    def _horizon(self, pic, scan, w: int, band: bool = True, market: bool = True) -> Text:
        """`next hour   ▲  81,240   +0.3%   80,950 – 81,500    81,010`: the bot's close, then the market's."""
        spot = self.spot()
        move = (scan.median / spot - 1) if spot else None
        glyph, colour = MARK.get({"bullish": BULL, "bearish": BEAR}.get(scan.view, FLAT), ("·", DIM))
        span = f"{fmt.price(scan.low)} – {fmt.price(scan.high)}"
        pct = ui.signed_pct(move) if move is not None else Text("–", style=FAINT)
        parts = [ui.t((f"  {pic.label:<13}", DIM), (glyph, colour), (f" {fmt.price(scan.median):>8}", f"bold {ORANGE}"),
                      (" " * max(8 - pct.cell_len, 1), "")), pct]
        parts += [ui.t((f"   {span:<19}", DIM))] if band else []
        parts += [ui.t((f"{fmt.price(pic.market_median):>8}", TEXT))] if market else []
        return ui.fit(parts, w)

    def key(self, k: str, ch: str | None) -> bool:
        s = self.swarm
        if not s.bots:
            return False
        if ch in ("]", "n", "\t") or k == "tab":
            self.at = (self.at + 1) % len(s.bots)
        elif ch in ("[", "p"):
            self.at = (self.at - 1) % len(s.bots)
        else:
            return False
        return True

    def enter(self) -> str | None:
        bot = self.bot()
        return f"BOTS {bot.id}" if bot else "BOTS"

    def hint(self) -> str:
        return "[ ] another bot · enter runs it in the zoo"

    def command(self) -> str:
        return f"BOT {self.asset}"

    def export(self):
        s = self.swarm
        bot = self.bot()
        if bot is None:
            return None
        rows = [[p.label, bot.id, sc.view, round(sc.median, 2), round(sc.low, 2), round(sc.high, 2), round(p.market_median, 2)]
                for p in s.pictures if (sc := p.scans.get(bot.id)) and not sc.error]
        return ["horizon", "bot", "view", "median", "band80_low", "band80_high", "market_median"], rows


_HOW = ("Every model runs on this machine from public hourly Coinbase candles: nothing about you, and nothing about "
        "what you hold, is sent anywhere. A pass over the whole zoo is recomputed when a new hourly bar closes. "
        "The closes themselves, and the market's own prices for them, come from the public Glimpse API.")

register(Function(
    "CONS", "Bot consensus", "Glimpse", "what every bot on this machine expects: bulls against bears, and the picture they agree on",
    ConsPane, takes=("CRYPTO", "CMDTY"), optional=True, args="[BTC|XAU|ETH|SOL]", needs=("glimpse", "history"),
    help="The whole model zoo read against the live market, for three horizons at once: the next hour, the next day and the next "
         "three days (a daily series reads the next close, a week and a month). Each block counts how many models lean bullish, "
         "how many bearish and how many neither, draws that as one bar, and then gives the picture they make together: the median "
         "of the average of every model's distribution, its 80% band, and the same two numbers from the market's own prices beside "
         "them. `they agree` is one minus the average distance from a model to the consensus, so a low number means the zoo is "
         "split; `apart from the market` is the same distance measured against what the market charges. enter opens the zoo, where "
         "a model can be read in full and run. " + _HOW))

register(Function(
    "BOT", "One bot", "Glimpse", "one model's picture of the next close against the market, [ and ] to walk the zoo", BotPane,
    takes=("CRYPTO", "CMDTY"), optional=True, args="[BTC|XAU] [model id]", needs=("glimpse", "history"),
    help="One model of the zoo at a time: what it is, which way it leans, and what it expects at each of the three horizons CONS "
         "reads, with the market's median beside each one. Under that is its whole picture of the nearest close: every price range "
         "with the chance the model gives it and the price the market charges for it on the same bar, so the gap between them, "
         "which is what a running bot trades, is the picture. [ and ] (or n and p) walk the zoo; `BOT BTC momentum_30d` opens on "
         "one model. enter opens it in the zoo, where enter runs it on paper and, with a key, for real. " + _HOW))
