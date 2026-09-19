"""The terminal's frame: a brand bar and a function bar on top, a status line and the key panel at the bottom.

The function bar names every screen and every series with the key that reaches it, so moving around needs no
memory. The key panel below lists what the current screen does, one labelled row per kind of action. Nothing
here holds state: each paint rebuilds it from the app, and it degrades by dropping the least useful parts first.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from . import fmt
from .theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT

if TYPE_CHECKING:
    from .app import Terminal

BAR = "#161616"             # the brand bar and status line
CHIP = "#1f1f1f"            # an unselected tab or group label
INK = "#0D0D0D"             # text on an orange or light chip
TAGLINE = "open-source forecast terminal · Glimpse API"

GROUP_W = 9                 # width of a group label in the key panel
GAP = "   "

# One labelled row per kind of action. Screens, series, help and quit live on the function bar, not here.
MORE = [("r", "refresh"), ("L O", "log in / out"), ("c", "colours"), ("ctrl-l", "redraw")]
VERT, SIDE = "j k · ↓ ↑ · s w", "h l · ← → · a d"      # vim, the arrow keys and WASD all move
KEYMAP: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {
    "heatmap": [
        ("MOVE", [("h j k l · ← ↓ ↑ → · a s w d", "move"), ("{ }", "a day"), ("( )", "next / prev midnight"),
                  ("0 $", "first / last close"), ("12|", "close no. 12"), ("gg G", "80% band top / bottom"), ("5j 12l", "counts")]),
        ("SELECT", [("v", "box"), ("V", "the 80% band"), ("o", "other corner"), ("gv", "reselect"), ("esc", "clear"),
                    ("ma 'a", "mark / jump"), ("''", "jump back")]),
        ("TRADE", [("tab", "bet slip"), ("+ -", "size ×2 ÷2"), ("S", "set size"), ("b", "BET"), ("enter", "open the close"),
                   ("/", "go to price"), (":", "command")]),
        ("VIEW", [("zf", "whole forecast"), ("zi zo", "zoom price"), ("< >", "zoom time"), ("za", "auto-fit"), ("zm", "median"),
                  ("zt zz zb", "place row"), ("zh zl zH zL", "sideways"), ("ctrl-d ctrl-u", "half page"),
                  ("ctrl-f ctrl-b", "page"), ("ctrl-e ctrl-y", "scroll")]),
        ("MORE", MORE),
    ],
    "main": [
        ("MOVE", [(VERT, "move"), (SIDE, "markets / ladder"), ("gg G", "top / bottom"), ("ctrl-d ctrl-u", "half page"),
                  ("ctrl-f ctrl-b", "page"), ("zz", "most likely range"), ("5j", "counts")]),
        ("SELECT", [("v", "select a range"), ("o", "other end"), ("esc", "clear")]),
        ("TRADE", [("tab", "bet slip"), ("+ -", "size ×2 ÷2"), ("S", "set size"), ("b enter", "BET"), ("/", "go to price"),
                   (":", "command")]),
        ("MORE", MORE),
    ],
    "portfolio": [
        ("MOVE", [(VERT, "move"), ("gg G", "top / bottom"), ("5j", "counts")]),
        ("ACT", [("x", "sell position"), ("enter", "open its market"), ("p esc", "back")]),
        ("MORE", MORE[:2]),
    ],
    "bots": [
        ("MOVE", [(VERT, "move"), (SIDE, "category"), ("gg G", "top / bottom"), ("ctrl-d ctrl-u", "half page"),
                  ("tab", "its forecast, close by close")]),
        ("READ", [("i", "how this model works: idea, data, maths, trades"), ("/", "search"), ("S", "sort"), ("esc", "clear search")]),
        ("RUN", [("enter", "run / stop"), ("e", "budget"), ("x", "stop"), ("X", "stop all"), ("L", "log in to trade real sats")]),
        ("MORE", [("r", "re-read the market")]),
    ],
    "bots-forecast": [
        ("MOVE", [(VERT, "a close"), (SIDE, "a day"), ("gg G", "first / last close"), ("tab esc", "back to the bots")]),
        ("READ", [("i", "how this model works: idea, data, maths, trades")]),
        ("RUN", [("enter", "run / stop this bot"), ("e", "budget"), ("x", "stop"), ("L", "log in to trade real sats")]),
        ("MORE", [("r", "re-read the market")]),
    ],
    "term": [
        ("GO", [("` :", "the GO bar"), ("BTC <GO>", "a security"), ("MEMP <GO>", "a function"), ("MSTR FA <GO>", "both"),
                ("F1", "help on this pane"), ("HELP", "every function")]),
        ("PANES", [("tab", "next pane"), ("ctrl-w h j k l", "move"), ("ctrl-w s v", "split"), ("ctrl-w q", "close"),
                   ("ctrl-w o", "zoom"), ("ctrl-w =", "even"), ("LP <name>", "launchpad"), ("LP SAVE <name>", "save")]),
        ("PAGE", [("j k", "scroll or move"), ("g G", "top / bottom"), ("enter", "open the row"), ("1-9", "menu item"),
                  ("EXP", "export to CSV")]),
        ("MORE", [("f", "forecast heatmap"), ("p", "portfolio"), ("B", "bots"), ("L O", "log in / out"), ("c", "colours"), ("q", "quit")]),
    ],
    "go": [
        ("GO", [("enter", "GO"), ("tab", "complete"), ("↑ ↓", "pick, or walk history"), ("esc", "leave"),
                ("ctrl-u ctrl-w", "clear line / word")]),
        ("TRY", [("BTC", "the Bitcoin page"), ("MEMP", "the mempool"), ("TX <txid>", "a transaction"), ("GP BTC XAU SPX", "compare"),
                 ("MSTR DES", "a company"), ("LP MACRO", "a launchpad"), ("FIND <text>", "search everything")]),
    ],
    "slip": [
        ("EDIT", [(VERT, "field"), (SIDE + " · - +", "step it"), ("5l", "counts"), ("enter", "type a value")]),
        ("TRADE", [("b", "BET"), ("tab esc", "back to the chart")]),
    ],
}


def fill(left: Text, right: Text, width: int, bg: str | None = None) -> Text:
    """`left` and `right` on one line of `width`, the gap between them painted `bg`."""
    out = Text(no_wrap=True, overflow="crop")
    out.append_text(left)
    out.append(" " * max(width - left.cell_len - right.cell_len, 1), style=f"on {bg}" if bg else "")
    out.append_text(right)
    if bg:
        out.stylize_before(f"on {bg}")  # under everything; chips keep their own background
    return out


# ── top ─────────────────────────────────────────────────────

def brand_bar(t: Terminal, width: int) -> Text:
    """GLIMPSE TERMINAL, the series and spot on the left; account, bots and the clock on the right."""
    left = Text(no_wrap=True)
    left.append(" GLIMPSE ", style=f"bold {INK} on {ORANGE}")
    left.append(" TERMINAL ", style=f"bold {ORANGE} on #2b1500")
    if t.batch:
        left.append(f"  {t.batch.short}", style=f"bold {TEXT}")
    if t.spot:
        left.append(f"  {fmt.price(t.spot)}", style=f"bold {ORANGE}")
        left.append(" spot", style=FAINT)

    right: list[tuple[int, Text]] = []          # (priority, part): lower numbers survive a narrow terminal
    def part(priority: int, *bits: tuple[str, str]) -> None:
        right.append((priority, Text.assemble(*bits, no_wrap=True)))

    if t.api.authenticated:
        if t.wallet:
            part(0, ("BAL ", FAINT), (fmt.sats(t.wallet.balance_sats), f"bold {ORANGE}"))
            part(3, ("EXP ", FAINT), (f"{fmt.sats(t.wallet.exposure_sats)}/{fmt.sats(t.wallet.max_exposure_sats)}", DIM))
        part(1, ("KEY ", FAINT), (t.key_mask, DIM))
    else:
        part(0, ("READ-ONLY ", f"bold {DIM}"), ("L", f"bold {ORANGE}"), (" log in", DIM))
    live = [r for r in t.runners.values() if r.running]
    if live:
        names = ", ".join(r.name for r in live[:2]) + (f" +{len(live) - 2}" if len(live) > 2 else "")
        part(2, ("BOTS ", FAINT), (names, f"bold {RED if any(r.live for r in live) else GREEN}"))
    part(0, (fmt.clock(), f"bold {TEXT}"))

    room = width - left.cell_len - 2
    keep = sorted(right, key=lambda p: p[0])
    while keep and sum(p.cell_len + 3 for _, p in keep) > room:
        keep.pop()                              # the least important part goes first
    kept = [p for _, p in right if any(p is k for _, k in keep)]
    tail = Text(no_wrap=True)
    for i, p in enumerate(kept):
        if i:
            tail.append(" │ ", style="#3a3a3a")
        tail.append_text(p)
    tail.append(" ")
    spare = width - left.cell_len - tail.cell_len
    if spare >= len(TAGLINE) + 6:               # centred in whatever is left over
        pad = (spare - len(TAGLINE)) // 2
        left.append(" " * pad + TAGLINE + " " * (spare - pad - len(TAGLINE)), style=FAINT)
    return fill(left, tail, width, BAR)


def tabs(t: Terminal) -> list[tuple[str, str, str, bool]]:
    """(label, short label, key that reaches it from here, showing now) for each screen."""
    back = {"heatmap": "f", "bots": "B", "portfolio": "p"}.get(t.view, "")
    return [("TERMINAL", "TERM", "t", t.view == "term"), ("FORECAST", "FCST", "f", t.view == "heatmap"),
            ("LADDER", "LDR", back or "f", t.view == "main"),
            ("BOTS", "BOTS", "B", t.view == "bots"), ("PORTFOLIO", "PORT", "p", t.view == "portfolio")]


def keycap(out: Text, key: str, label: str, on: bool) -> None:
    """A function key: the key in a solid chip, then its label, the whole thing lit when it is where you are."""
    if on:
        out.append(f" {key} ", style=f"bold {ORANGE} on {INK}")
        out.append(f" {label} " if label else "", style=f"bold {INK} on {ORANGE}")
    else:
        out.append(f" {key} ", style=f"bold {INK} on {ORANGE}")
        out.append(f" {label} " if label else "", style=f"{TEXT} on {CHIP}")


def nav_bar(t: Terminal, width: int) -> Text:
    """Every screen and every series, each with its key. Shortens itself to fit: labels, then the series list."""
    series = [(b.short, i == t.batch_i) for i, b in enumerate(t.batches)] or [(t.asset, True)]
    for short_tabs, all_series, words in ((False, True, True), (True, True, True), (True, True, False),
                                          (True, False, False)):
        left = Text(no_wrap=True)
        left.append(" ")
        for label, short, key, on in tabs(t):
            keycap(left, key, short if short_tabs else label, on)
            left.append(" ")
        left.append("  ")
        if words:
            left.append("SERIES ", style=f"bold {FAINT}")
        left.append(" [ ", style=f"bold {INK} on {ORANGE}")
        shown = series if all_series else [s for s in series if s[1]]
        for name, on in shown:
            left.append(f" {name} ", style=f"bold {ORANGE} on #2b1500" if on else DIM)
        left.append(" ] ", style=f"bold {INK} on {ORANGE}")
        right = Text(no_wrap=True)
        keycap(right, "?", "HELP" if words else "", False)
        right.append(" ")
        keycap(right, "q", "QUIT" if words else "", False)
        right.append(" ")
        if left.cell_len + right.cell_len + 2 <= width:
            return fill(left, right, width)
    return fill(left, Text(), width)


def header(t: Terminal, width: int) -> Text:
    return Text("\n", no_wrap=True).join([brand_bar(t, width), nav_bar(t, width)])


# ── bottom ──────────────────────────────────────────────────

def status_line(t: Terminal, mode: str, width: int) -> Text:
    """vim's status line: the mode, any message, and on the right the keys typed so far (5 then j, 12 then |)."""
    colour = {"NORMAL": DIM, "VISUAL": ORANGE, "SLIP": GREEN, "GO": ORANGE}[mode]
    left = Text(no_wrap=True)
    left.append(f" -- {mode} -- ", style=f"bold {INK} on {colour}")
    if t.flash:
        left.append(f"  {t.flash}", style=f"bold {ORANGE}")
    right = Text(no_wrap=True)
    typed = t.count + t.pending
    if typed:
        right.append(f" {typed}", style=f"bold {TEXT}")
    else:
        right.append("? ", style=f"bold {ORANGE}")
        right.append("every key ", style=FAINT)
    return fill(left, right, width, BAR)


def legend(view: str, width: int, max_lines: int = 5) -> Text:
    """The keys for this screen as a panel: one labelled row per kind of action. A row too long for the terminal
    carries on underneath rather than dropping keys; a terminal too short for every line keeps the first groups
    whole, and ? lists everything."""
    lines: list[Text] = []
    for name, keys in KEYMAP[view]:
        rows = [Text(no_wrap=True, overflow="crop")]
        rows[0].append(f" {name:<{GROUP_W - 2}} ", style=f"bold {TEXT} on {CHIP}")
        rows[0].append(" ")
        x = GROUP_W + 1
        for k, what in keys:
            item = len(k) + 1 + len(what)
            if x > GROUP_W + 1 and x + len(GAP) + item > width - 1:
                rows.append(Text(" " * (GROUP_W + 1), no_wrap=True, overflow="crop"))
                x = GROUP_W + 1
            if x > GROUP_W + 1:
                rows[-1].append(GAP)
                x += len(GAP)
            rows[-1].append(k, style=f"bold {ORANGE}")
            rows[-1].append(f" {what}", style=DIM)
            x += item
        if lines and len(lines) + len(rows) > max_lines:
            break
        lines += rows
    return Text("\n", no_wrap=True, overflow="crop").join(lines[:max_lines])
