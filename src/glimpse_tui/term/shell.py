"""The shell: what the GO bar's commands mean inside the app.

The app keeps its screens and its keys. This object adds the launchpad view beside them: it owns the hub, the
workspace, the GO bar's line and the `ctrl-w` chords, and it turns a parsed `Command` into a pane. Today's
screens run as functions (`ODDS`, `HM`, `SLIP`, `PORT`, `BOTS`) by switching the app to the view they always had,
so every key they had still works (PLAN D31).

It works like spacemacs. Every page is a buffer; windows show buffers. With windows showing, h j k l (or the arrows)
move between them and 1 to 9 jump to one. Enter goes inside a window, where the keys belong to its page; esc or
backspace goes back a page, and out. SPC opens the leader menu (SPC w windows, SPC b buffers, SPC t f o B p the
screens) and `:` a list of everything that can be opened.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text

from ..theme import DIM, FAINT, GREEN, ORANGE, TEXT
from . import config, gobar, panes, registry, tape
from .hub import Hub
from .panes import FuncPane, Leaf, Workspace

if TYPE_CHECKING:
    from ..app import Terminal

INK = "#0D0D0D"
PASS_THROUGH = {"q", "c", "?", "L", "O", "r", "p", "B", "f", "o"}      # the app's own keys that still work from a launchpad
ARROWS = {"left": "h", "down": "j", "up": "k", "right": "l", "ctrl+j": "j", "ctrl+k": "k"}      # ctrl-j ctrl-k are down and up
SCROLL = {"ctrl+d", "ctrl+u", "ctrl+f", "ctrl+b", "pagedown", "pageup"}
# Short on purpose: the key panel under it lists the rest, and a long line is cut on a narrow terminal.
WELCOME = "j k walk the page · f the forecast · o the odds · ? every key"
LEGACY_VIEW = {"ODDS": "main", "HM": "heatmap", "SLIP": "heatmap", "PORT": "portfolio", "BOTS": "bots"}
SCREENS = {"f": "HM", "o": "ODDS", "B": "BOTS", "p": "PORT"}
MAX_BUFFERS = 24
BUF = "\x00buf:"                                        # a command-list entry that switches to an open buffer
LEADER: dict[str, list[tuple[str, str]]] = {
    "": [("SPC", "open anything (same as :)"), ("1-9", "window 1 to 9"), ("TAB", "back a page"), ("w", "windows…"),
         ("b", "buffers…"), ("t", "the front page"), ("f", "the forecast"), ("o", "the odds"), ("B", "the bots"),
         ("p", "portfolio"), ("?", "help"), ("q", "quit")],
    "w": [("/", "split right"), ("-", "split below"), ("d", "close window"), ("m", "maximize / restore"), ("=", "even sizes"),
          ("h j k l", "move"), ("w", "next window")],
    "b": [("b", "list buffers"), ("n", "next buffer"), ("p", "previous buffer"), ("d", "close buffer"), ("r", "reload")],
}
PAGES = (("GP BTC 24H", "Bitcoin: the last 24 hours and the next 24"), ("DIST BTC", "Bitcoin's odds on the next hour"),
         ("CONS BTC", "what every bot on this machine expects of Bitcoin"), ("BOT BTC", "one bot at a time, [ ] walks the zoo"),
         ("GP XAU 1D", "gold: the last month and a half and the next month"), ("DIST XAU", "gold's odds on the next close"),
         ("FCST BTC", "Bitcoin forecast, next 48 hours"), ("FCST XAU", "gold's forecast as a heatmap"),
         ("FCST BTC 1D", "Bitcoin daily forecast"),
         ("FCST ETH", "Ether forecast"), ("FCST SOL", "Solana forecast"),
         ("NEWS", "headlines, read here in the terminal"))
SCREEN_ITEMS = (("HM", "the full forecast: every close, every price, select and bet (SPC f)"),
                ("ODDS", "the odds on every outcome, close by close, and bet (SPC o)"),
                ("BOTS", "the model zoo (SPC B)"), ("PORT", "your positions (SPC p)"))


class Shell:
    def __init__(self, app: Terminal) -> None:
        self.app = app
        self.hub = Hub()
        self.hub.forecast = self._forecast
        self.hub.account = self._account
        self.hub.handoff = self._handoff
        self.hub.glimpse = lambda: self.app.api
        self.hub.views = self._views
        self.line = gobar.Line()
        self.line.load_history()
        self.go_focus = False
        self.demo = False                       # the DEMO tour is running; any key stops it
        self.chord = ""                         # "ctrl+w" while waiting for its second key
        self.started = False
        self.lp_name = ""
        self._back_to = "term"                  # the view the GO bar was opened from, for Esc
        self.leader: str | None = None          # "" after SPC, "w" or "b" after SPC w / SPC b
        self.only_buffers = False               # the command list is showing SPC b b
        self._welcomed = False

    # wiring ─────────────────────────────────────────────────

    def _forecast(self) -> list[tuple[float, float, float, float]]:
        """The Glimpse market's median and 80% band for each hourly BTC close: the app's, or those a FCST or DIST pane loaded."""
        app = self.app
        views = app.views if app.asset == "BTC" and app.hourly else self.hub.series_views.get("BTC", [])
        return [(float(v.row.end_time_utc), v.median, v.band[0], v.band[1]) for v in views]

    def _views(self, series: str) -> list:
        """The app's own loaded closes, when it is on this series: FCST shares them instead of fetching them again."""
        app = self.app
        return app.views if app.batch and app.batch.short == series and app.views else []

    def open_series(self, series: str, view: str = "heatmap") -> None:
        """The full heatmap (or the ladder) on `series` (BTC, BTC 1D, XAU…): switch the app's series if it is on another."""
        app = self.app
        series = series.upper()
        idx = next((i for i, b in enumerate(app.batches) if b.short.upper() == series), None)
        if idx is None:
            app.say(f"Glimpse has no {series} series open. [ and ] on the heatmap walk the ones it has.")
            return
        if idx != app.batch_i:
            app.switch_batch(idx - app.batch_i)
        app.slip_focus = False
        if view == "heatmap":
            app.view, app.hm_col, app.hm_anchor = "heatmap", app.m_cur, None
            app.load_candles()
        else:
            app.view, app.pane = "main", 1              # o lands on the odds themselves, not on the list of closes
            app.load_book()
        app.paint()

    def _handoff(self, close: int, lo_bin: int, hi_bin: int) -> None:
        """OMON's strike becomes a box on the heatmap: one close, bins lo to hi. Nothing is ordered here. The user is
        left on the heatmap with the selection made, where the bet slip, Confirm and the estimate gate work as always."""
        app = self.app
        if not app.views:
            return
        close = max(0, min(close, len(app.views) - 1))
        top = len(app.views[close].bins) - 1
        lo_bin, hi_bin = max(0, min(lo_bin, top)), max(0, min(hi_bin, top))
        app.view, app.slip_focus = "heatmap", False
        app.hm_anchor, app.hm_col, app.hm_bin = (close, lo_bin), close, hi_bin
        # One bin a cell, so the box is exactly the strikes OMON showed, and no auto-fit moves the cursor off it.
        app.hm_bpc, app.hm_fit_pending, app.hm_touched = 1, False, True
        app.hm_gtop = (lo_bin + hi_bin) // 2 + app.hm_cells_v // 2
        app.hm_left = close
        app.load_candles()
        app.say("OMON handed this range to the heatmap. tab opens the bet slip, b bets after the usual confirmation.", 8)
        app.paint()

    def _account(self) -> dict[str, object]:
        app = self.app
        return {"authenticated": app.api.authenticated, "key_mask": app.key_mask, "wallet": app.wallet, "summary": app.summary,
                "positions": list(app.positions), "views": list(app.views), "asset": app.asset, "hourly": app.hourly}

    @property
    def ws(self) -> Workspace:
        return self.app.query_one("#term", Workspace)

    def ensure_started(self) -> None:
        """The hub's streams and polls begin the first time a launchpad is shown, never before: the app's older
        screens, and the tests that drive them, run without any of this."""
        if not self.started:
            self.started = True
            self.hub.listeners.append(self._changed)
            self.app.run_worker(self.hub.start(), group="hub", exclusive=True)

    def _changed(self) -> None:
        if self.app.view == "term":
            self.app.paint()

    async def stop(self) -> None:
        await self.hub.stop()

    def tick(self) -> None:
        """Once a second from the app: check the alerts, then reload any pane whose data is due. Rendering never waits."""
        if self.started:
            try:
                from ..funcs.toolbox import engine
                fired = engine().evaluate(self.hub)     # cached state only: no request is ever made for an alert
            except ImportError:
                fired = []
            for msg in fired:
                self.app.say(msg, 10)
                self.app.bell()
        if self.app.view != "term":
            return
        now = time.time()
        for p in self.ws.panes:
            if p.due(now) and p.size.width > 0:
                self.app.run_worker(p.reload(), group=f"pane-{id(p)}")

    # launchpads, windows and buffers ──────────────────────

    def show(self, name: str | None = None) -> None:
        """Switch the app to the terminal view, loading `name` (or the default) if nothing is open yet."""
        self.app.view = "term"
        self.ensure_started()
        if name or not self.ws.root:
            self.load_launchpad(name or self.hub.cfg.get("default_launchpad", "BTC"))
        if not self._welcomed:
            self._welcomed = True
            self.app.say(WELCOME, 15)

    def load_launchpad(self, name: str) -> bool:
        root = panes.load_layout(name)
        if root is None:
            self.app.say(f"No launchpad called {name.upper()}. LP lists them: {', '.join(panes.layout_names())}")
            return False
        ws = self.ws
        for p in list(ws.buffers):
            p.remove()
        ws.root, ws.zoomed, ws.inside, self.lp_name = root, False, False, name.upper()
        for lf in panes.leaves(root):
            self._fill(lf)
        ws.focused = panes.leaves(root)[0]
        ws.relayout()
        return True

    def _make(self, cmd: gobar.Command | None) -> tuple[FuncPane, str]:
        """The pane for a command. A command that no longer parses becomes HELP."""
        fn = registry.get(cmd.func) if cmd else None
        if not (cmd and fn and fn.make):
            cmd = gobar.Command("HELP", args=(), raw="HELP")
            fn = registry.get("HELP")
        return fn.make.from_command(self.hub, cmd), cmd.text()

    def _fill(self, leaf: Leaf, cmd: gobar.Command | None = None) -> None:
        """Put a fresh pane for `leaf.command` into the window, replacing (and closing) what it showed."""
        pane, leaf.command = self._make(cmd or gobar.parse(leaf.command, self.hub.book))
        if leaf.pane:
            leaf.pane.remove()
        leaf.pane = pane
        self.ws.mount(pane)

    def _shown(self) -> dict[int, Leaf]:
        return {id(lf.pane): lf for lf in panes.leaves(self.ws.root)} if self.ws.root else {}

    def _put(self, leaf: Leaf, pane: FuncPane, remember: bool = True) -> None:
        """Show `pane` in `leaf`. The page it showed stays open as a buffer, and back returns to it."""
        old = leaf.pane
        if old is pane:
            return
        elsewhere = self._shown().get(id(pane))
        if elsewhere is not None and elsewhere is not leaf:
            elsewhere.pane = old                     # already in another window: the two windows swap
        elif old is not None and remember and not isinstance(old, Scratch):
            leaf.back = [b for b in leaf.back if b is not old] + [old]
        elif isinstance(old, Scratch):
            old.remove()
        leaf.pane, leaf.command = pane, pane.command()
        self._trim()

    def _trim(self) -> None:
        """Close the oldest hidden buffers beyond MAX_BUFFERS."""
        ws, shown = self.ws, self._shown()
        hidden = [p for p in ws.buffers if id(p) not in shown]
        for p in hidden[:max(len(ws.buffers) - MAX_BUFFERS, 0)]:
            self._forget(p)

    def _forget(self, pane: FuncPane) -> None:
        for lf in panes.leaves(self.ws.root) if self.ws.root else []:
            lf.back = [b for b in lf.back if b is not pane]
        pane.remove()

    def open(self, cmd: gobar.Command, where: str = "") -> None:
        """Run a function in the focused window (the page it showed stays a buffer, back returns to it), or in a new
        window when `where` is 's' (below) or 'v' (to the right). A buffer already open for the same command is reused."""
        ws = self.ws
        if not ws.root:
            ws.root = ws.focused = Leaf(cmd.text())
            self._fill(ws.focused, cmd)
        elif where and len(panes.leaves(ws.root)) < panes.MAX_PANES and ws.focused:
            new = Leaf(cmd.text())
            ws.root = panes.split(ws.root, ws.focused, new, "col" if where == "s" else "row")
            self._fill(new, cmd)
            ws.focused = new
        else:
            if where:
                self.app.say(f"{panes.MAX_PANES} windows is the most the terminal holds. SPC w d closes one.")
            leaf = ws.focused or panes.leaves(ws.root)[0]
            shown = self._shown()
            same = next((p for p in ws.buffers if p.command() == cmd.text() and id(p) not in shown), None)
            if same is None:
                same, _ = self._make(cmd)
                ws.mount(same)
            self._put(leaf, same)
        ws.relayout()

    def split(self, where: str) -> None:
        """A new window beside (v) or below (s) the focused one, empty, with the list of everything open to fill it."""
        ws = self.ws
        if not ws.root or not ws.focused:
            return
        if len(panes.leaves(ws.root)) >= panes.MAX_PANES:
            self.app.say(f"{panes.MAX_PANES} windows is the most the terminal holds. SPC w d closes one.")
            return
        new = Leaf("NEW")
        ws.root = panes.split(ws.root, ws.focused, new, "col" if where == "s" else "row")
        new.pane = Scratch(self.hub)
        ws.mount(new.pane)
        ws.focused, ws.inside = new, False
        ws.relayout()
        self.focus_go()

    def close_window(self) -> None:
        ws = self.ws
        if not ws.root or not ws.focused:
            return
        if len(panes.leaves(ws.root)) <= 1:
            self.app.say("The last window stays. SPC b d closes its buffer; q quits.")
            return
        gone, order = ws.focused, panes.leaves(ws.root)
        nxt = order[(order.index(gone) + 1) % len(order)]
        ws.root = panes.close(ws.root, gone)
        if isinstance(gone.pane, Scratch):
            gone.pane.remove()                          # other buffers stay open: SPC b b reaches them
        ws.zoomed = ws.inside = False
        ws.focus_leaf(nxt)

    def back(self) -> bool:
        """The page this window showed before. The one it leaves stays open as a buffer."""
        ws = self.ws
        leaf = ws.focused
        if not leaf:
            return False
        shown = self._shown()
        while leaf.back:
            prev = leaf.back.pop()
            if id(prev) in shown or not prev.is_attached:
                continue
            old = leaf.pane
            leaf.pane, leaf.command = prev, prev.command()
            if isinstance(old, Scratch):
                old.remove()
            ws.relayout()
            return True
        return False

    def cycle_buffer(self, d: int) -> None:
        """SPC b n / SPC b p: the next hidden buffer in this window."""
        ws, shown = self.ws, self._shown()
        hidden = [p for p in ws.buffers if id(p) not in shown and not isinstance(p, Scratch)]
        if not hidden or not ws.focused:
            self.app.say("No other buffer is open. : opens one.")
            return
        self._put(ws.focused, hidden[0 if d > 0 else -1])
        ws.relayout()

    def kill_buffer(self) -> None:
        ws = self.ws
        leaf = ws.focused
        if not leaf or not leaf.pane:
            return
        gone, shown = leaf.pane, self._shown()
        spare = next((b for b in reversed(leaf.back) if id(b) not in shown and b is not gone), None)
        spare = spare or next((b for b in ws.buffers if id(b) not in shown and b is not gone), None)
        if spare is None:
            spare = Scratch(self.hub)
            ws.mount(spare)
        leaf.pane, leaf.command = spare, spare.command()
        self._forget(gone)
        ws.relayout()

    def show_buffer(self, pane: FuncPane) -> None:
        self.show()
        ws = self.ws
        if ws.focused and pane.is_attached:
            self._put(ws.focused, pane)
            ws.relayout()

    # running commands ───────────────────────────────────────

    def run(self, text: str, where: str = "") -> None:
        """GO. Parse, then open a pane, switch to one of today's screens, or hand the words to ASK."""
        text = text.strip()
        if not text:
            return
        self.line.remember(text)
        current = self.ws.pane.security if self.app.view == "term" and self.ws.pane else None
        cmd = gobar.parse(text, self.hub.book, current)
        if cmd is None:
            if registry.get("ASK"):
                cmd = gobar.Command("ASK", args=tuple(text.split()), raw=text)
            else:
                self.app.say(f"Not a command: {text}. HELP lists every function.")
                return
        self.execute(cmd, where)

    def execute(self, cmd: gobar.Command, where: str = "") -> None:
        app = self.app
        if cmd.func == "QUIT":
            app.exit()
            return
        if cmd.func == "LP":
            self._lp(cmd.args)
            return
        fn = registry.get(cmd.func)
        if fn is None:
            app.say(f"Not a function: {cmd.func}")
            return
        if fn.legacy:
            if fn.code in ("HM", "ODDS") and cmd.args:
                self.open_series(" ".join(cmd.args), "heatmap" if fn.code == "HM" else "main")     # HM XAU, ODDS BTC
            else:
                self._legacy(fn.code, cmd.args)
            return
        if fn.takes and not cmd.securities and not fn.optional:
            app.say(f"{fn.code} needs a security: {fn.code} {fn.args or '<ticker>'}")
            return
        handler = SHELL_RUN.get(fn.code)
        if handler and handler(self, cmd):
            return
        if fn.make is None:
            app.say(f"{fn.code} is not built yet. PLAN.md section 6d lists what is.")
            return
        self.show()
        self.open(cmd, where)
        app.paint()

    def _legacy(self, code: str, args: tuple[str, ...] = ()) -> None:
        app = self.app
        view = LEGACY_VIEW[code]
        if view == "heatmap" and app.view != "heatmap":
            app.view, app.hm_col, app.hm_anchor = "heatmap", app.m_cur, None
            app.load_candles()
        elif view == "bots":
            app.view = "bots"
            if args:                                    # BOTS <model id>: open the zoo on that model
                app.bot_filter, app.bot_cur, app.bot_tab, app.bot_pane = " ".join(args), 0, 0, 0
            app.load_bots()
            app.scan_bots()
        elif view == "heatmap":
            pass
        else:
            app.view = view
            if view == "portfolio":
                app.load_account()
            if view == "main":
                app.load_book()
        if code == "SLIP":
            app.slip_focus = app.size.width >= 118
            if not app.slip_focus:
                app.say("The bet slip needs 118 columns. The compact ticket under the chart shows the same numbers.")
        app.paint()

    def _lp(self, args: tuple[str, ...]) -> None:
        if args and args[0].upper() == "SAVE":
            if len(args) < 2 or not args[1].replace("_", "").replace("-", "").isalnum():
                self.app.say("LP SAVE <name>: letters, digits, - and _")
            elif not self.ws.root:
                self.app.say("Nothing to save: no launchpad is open.")
            else:
                for lf in panes.leaves(self.ws.root):
                    if lf.pane:
                        lf.command = lf.pane.command()
                path = panes.save_layout(args[1], self.ws.root)
                self.lp_name = args[1].upper()
                self.app.say(f"Saved {self.lp_name} to {path}")
            return
        if not args:
            self.app.say("Launchpads: " + "  ".join(panes.layout_names()) + "   ·   LP <name> loads, LP SAVE <name> saves")
            return
        self.app.view = "term"
        self.ensure_started()
        self.load_launchpad(args[0])
        self.app.paint()

    # the leader key, SPC ────────────────────────────────────

    def leader_start(self) -> None:
        self.leader = ""

    def leader_key(self, k: str, ch: str | None) -> None:
        """The key after SPC (or after SPC w, SPC b). Anything unknown closes the menu."""
        menu, self.leader = self.leader, None
        ch = ch or ""
        if k == "escape":
            return
        if menu == "":
            if k == "space" or ch == ":":
                self.focus_go()
            elif ch.isdigit() and ch != "0":
                self._to_window(int(ch))
            elif k == "tab":
                self._in_term(self.back)
            elif ch in ("w", "b"):
                self.leader = ch
            elif ch in SCREENS or ch == "t":
                self.screen(ch)
            elif ch == "?":
                self.app.help()
            elif ch == "q":
                self.app.exit()
        elif menu == "w":
            if ch in ("/", "v"):
                self._in_term(lambda: self.split("v"))
            elif ch in ("-", "s"):
                self._in_term(lambda: self.split("s"))
            elif ch in ("d", "q"):
                self._in_term(self.close_window)
            elif ch == "m":
                self._in_term(self._maximize)
            elif ch == "=":
                self._in_term(lambda: (panes.even(self.ws.root), self.ws.relayout()) if self.ws.root else None)
            elif ch in ("h", "j", "k", "l") or k in ARROWS:
                self._in_term(lambda: self.ws.move(ch if ch in "hjkl" and ch else ARROWS[k]))
            elif ch == "w" or k == "tab":
                self._in_term(lambda: self.ws.cycle(1))
        elif menu == "b":
            if ch == "b":
                self.focus_go(buffers=True)
            elif ch == "n":
                self._in_term(lambda: self.cycle_buffer(1))
            elif ch == "p":
                self._in_term(lambda: self.cycle_buffer(-1))
            elif ch == "d":
                self._in_term(self.kill_buffer)
            elif ch == "r":
                self._in_term(lambda: setattr(self.ws.pane, "loaded_at", 0.0) if self.ws.pane else None)

    def _in_term(self, fn) -> None:
        """Window and buffer commands act on the terminal view: go there first from any other screen."""
        if self.app.view != "term":
            self.show()
        fn()

    def next_story(self, d: int) -> None:
        """n / p in a story: the next or previous headline of the list it was opened from, without going back to it."""
        leaf = self.ws.focused
        news = next((b for b in reversed(leaf.back) if b.code == "NEWS"), None) if leaf else None
        if news is None or not getattr(news, "items", None):
            return
        i = news.cur + d
        if not 0 <= i < len(news.items):
            self.app.say("That was the last story in the list." if d > 0 else "That was the first story in the list.")
            return
        self.back()
        news.cur = i
        if go := news.enter():
            self.run(go)

    def enter_window(self) -> None:
        """Open the focused window full screen, with the keys going to its page."""
        ws = self.ws
        ws.inside = ws.zoomed = True
        ws.relayout()

    def leave_window(self) -> None:
        """Back to the whole page, the window still focused and in view."""
        ws = self.ws
        ws.inside = ws.zoomed = False
        ws.follow()
        ws.follow_pending = True
        ws.relayout()

    def _to_window(self, n: int) -> None:
        self.show()
        ls = panes.leaves(self.ws.root) if self.ws.root else []
        if n <= len(ls):
            self.ws.inside = self.ws.zoomed = False
            self.ws.focus_leaf(ls[n - 1])

    def _maximize(self) -> None:
        self.ws.zoomed = not self.ws.zoomed
        if not self.ws.zoomed:
            self.ws.inside = False
            self.ws.follow()
        self.ws.relayout()

    def screen(self, ch: str) -> None:
        """SPC t f o B p: the front page, the forecast, the odds, the bots, the portfolio."""
        if ch == "t":
            self.show()
        else:
            self._legacy(SCREENS[ch])

    def which_key(self, width: int) -> Text:
        """The menu SPC opens: every key that can follow, in columns, spacemacs style."""
        name = {"": "SPC", "w": "SPC w  windows", "b": "SPC b  buffers"}[self.leader or ""]
        items = LEADER[self.leader or ""]
        col_w = max(len(k) + len(label) + 5 for k, label in items)
        per_row = max(width // col_w, 1)
        rows = [ui_title(name, width)]
        for i in range(0, len(items), per_row):
            row = Text(no_wrap=True, overflow="crop")
            for k, label in items[i:i + per_row]:
                cell = Text(no_wrap=True)
                cell.append(f"  {k}", style=f"bold {ORANGE}")
                cell.append(f" → {label}", style=TEXT if label.endswith("…") else DIM)
                cell.pad_right(col_w - cell.cell_len)
                row.append_text(cell)
            rows.append(row)
        return Text("\n", no_wrap=True).join(rows)

    # the command list, : ────────────────────────────────────

    def focus_go(self, seed: str = "", buffers: bool = False) -> None:
        self._back_to = self.app.view
        self.go_focus = True
        self.only_buffers = buffers
        self.line.clear()
        if seed:
            self.line.set(seed)
        self._suggest()

    def _suggest(self) -> None:
        text = self.line.text
        if " " in text.strip():                                # typing arguments: the old completion, for tickers
            self.line.picks = gobar.complete(text, self.hub.book, panes.layout_names(), self.hub.series_seen)
            self.line.pick = -1
            return
        self.line.picks = self.items(text)
        self.line.pick = 0 if self.line.picks else -1

    def items(self, text: str) -> list[gobar.Suggestion]:
        """Everything `:` can open, filtered by what is typed: open buffers first, then the forecasts, the screens,
        every function and the layouts."""
        S = gobar.Suggestion
        out: list[gobar.Suggestion] = []
        if self.app.query("#term"):
            shown = self._shown()
            for i, p in enumerate(self.ws.buffers):
                if isinstance(p, Scratch):
                    continue
                where = f"in window {self.ws.number(shown[id(p)])}" if id(p) in shown else "open"
                out.append(S(f"{BUF}{i}", p.command(), f"{p.title or p.code} · {where}", kind="buffer"))
        if not self.only_buffers:
            out += [S(t, t, d, kind="page") for t, d in PAGES]
            out += [S(t, t, d, kind="screen") for t, d in SCREEN_ITEMS]
            seen = {t.split()[0] for t, _ in PAGES + SCREEN_ITEMS}
            for fn in registry.every():
                if fn.code in seen:
                    continue
                needs = bool(fn.takes and not fn.optional) or fn.args.startswith("<")
                out.append(S(fn.code + (" " if needs else ""), fn.code, f"{fn.name} · {fn.summary}" + (f" · type {fn.args}" if needs else ""),
                             kind="needs" if needs else "function"))
            out += [S(f"LP {n}", f"LP {n}", "layout", kind="layout") for n in panes.layout_names()]
        q = text.strip().lower()
        if q:
            out = [s for s in out if q in f"{s.label} {s.detail}".lower()]
            out.sort(key=lambda s: 0 if s.label.lower().startswith(q) else 1)
        return out

    def go_key(self, k: str, ch: str | None) -> None:
        """Every key while the command list is open."""
        line = self.line
        if k == "escape":
            self.go_focus = False
            line.clear()
            return
        k = {"ctrl+j": "down", "ctrl+k": "up"}.get(k, k)     # ctrl-j ctrl-k: down and up, as in spacemacs
        if k in ("down", "ctrl+n", "up", "ctrl+p", "pagedown", "pageup") and line.picks and line.h_at is None:
            step = {"down": 1, "ctrl+n": 1, "up": -1, "ctrl+p": -1, "pagedown": 10, "pageup": -10}[k]
            if not (k == "up" and line.pick <= 0):
                line.pick = max(0, min(line.pick + step, len(line.picks) - 1))
                return
            line.picks, line.pick = [], -1                # ↑ at the top of the list walks what you ran before
        if k == "enter":
            item = line.picks[line.pick] if 0 <= line.pick < len(line.picks) else None
            text = line.text
            exact = next((p for p in line.picks if p.label.lower() == text.strip().lower()), None)
            if exact is not None and line.pick == 0:
                item = exact                            # typed a name exactly: that, not the first row that starts with it
            self.go_focus = False
            line.clear()
            if item is None or (" " in text.strip()):
                self.run(text if item is None or item.kind != "instrument" else item.text)
            elif item.text.startswith(BUF):
                bufs = self.ws.buffers
                i = int(item.text[len(BUF):])
                if i < len(bufs):
                    self.show_buffer(bufs[i])
            elif item.kind == "needs":
                self.focus_go(item.text)                  # it needs a ticker or an id: keep the line open for it
                self.app.say(f"{item.label} needs {registry.get(item.label).args or 'a ticker'}: type it and press enter")
            else:
                self.run(item.text)
            return
        if line.key(k, ch) and k not in ("up", "down"):
            self._suggest()

    # keys ───────────────────────────────────────────────────

    def key(self, k: str, ch: str | None) -> bool:
        """A key in the terminal view. False hands it back to the app (for the keys in PASS_THROUGH only)."""
        ws = self.ws
        if self.chord:
            self.chord = ""
            self._window_key(k, ch or "")
            return True
        if k == "ctrl+w":
            self.chord = "ctrl+w"
        elif ch in ("`", ":"):
            self.focus_go()
        elif k == "f1":
            self.run(f"HELP {ws.pane.code}" if ws.pane else "HELP")
        elif k in ("tab", "shift+tab"):
            ws.inside = ws.zoomed = False
            ws.cycle(1 if k == "tab" else -1)
        elif ch in ("f", "o") and not ws.inside and (series := getattr(ws.pane, "series", "")):
            self.run(f"HM {series}" if ch == "f" else f"ODDS {series}")     # the full screen, on the series this window shows
        elif ws.inside:
            return self._inside_key(k, ch)
        else:
            return self._window_mode_key(k, ch)
        return True

    def _window_mode_key(self, k: str, ch: str | None) -> bool:
        """Windows showing, none entered: move between them, jump to one, go inside, or go back a page."""
        ws = self.ws
        if ch in ("h", "j", "k", "l") or k in ARROWS:
            ws.move(ch if ch in ("h", "j", "k", "l") else ARROWS[k])
        elif ch and ch.isdigit() and ch != "0":
            self._to_window(int(ch))
        elif k == "enter" and ws.pane:
            self.enter_window()
        elif ch == "g" or k == "home":
            self._to_window(1)
        elif ch == "G" or k == "end":
            self._to_window(len(panes.leaves(ws.root)) if ws.root else 1)
        elif k in ("backspace", "escape"):
            if ws.zoomed and k == "escape":
                self._maximize()
            elif not self.back() and k == "backspace":
                self.app.say("This window has no page before this one.")
        elif ch in ("J", "K") and ws.pane:
            ws.pane.on_key_(ch.lower(), ch.lower())    # scroll the focused window without going in
        elif k in SCROLL and ws.pane:
            ws.pane.on_key_(k, ch)
        elif ch == "o" and ws.pane and ws.pane.on_key_(k, ch):
            pass                                        # a page with an o of its own (ORCL) keeps it: o is the odds screen elsewhere
        elif ch in PASS_THROUGH or k in ("ctrl+c", "ctrl+l"):
            return False
        elif ws.pane and ws.pane.on_key_(k, ch):
            pass                                        # the page's own letters: [ ] for its window or topic, and so on
        return True

    def _inside_key(self, k: str, ch: str | None) -> bool:
        """Inside one window: its keys, its links on the digits, enter to open, esc or backspace to go back."""
        ws = self.ws
        pane = ws.pane
        if pane is None:
            return True
        if k in ("escape", "backspace") or (ch == "h" and pane.code == "READ"):
            if pane.on_key_(k, ch):
                pass                                    # the page had something of its own to leave: a filing
            elif not self.back():
                self.leave_window()
        elif ch and ch.isdigit() and ch != "0" and (menu := pane.menu()):
            i = int(ch) - 1
            if i < len(menu):
                self.run(menu[i][1])
        elif k == "enter" or (ch == "l" and pane.code == "NEWS"):
            if go := pane.enter():
                self.run(go)
        elif ch in ("n", "p") and pane.code == "READ":
            self.next_story(1 if ch == "n" else -1)
        elif ch == "g":
            pane.home()
        elif pane.on_key_(k, ch):
            pass
        elif ch in PASS_THROUGH or k in ("ctrl+c", "ctrl+l"):
            return False
        return True

    def _window_key(self, k: str, ch: str) -> None:
        """ctrl-w, as in vim: h j k l move, s v split, q close, o maximize, = even out, w next."""
        ws = self.ws
        if not ws.root:
            return
        if ch in ("h", "j", "k", "l"):
            ws.move(ch)
        elif k in ARROWS:
            ws.move(ARROWS[k])
        elif ch in ("s", "v") and ws.pane:
            if len(panes.leaves(ws.root)) >= panes.MAX_PANES:
                self.app.say(f"{panes.MAX_PANES} windows is the most the terminal holds. SPC w d closes one.")
                return
            self.open(gobar.parse(ws.pane.command(), self.hub.book) or gobar.Command("HELP"), where=ch)
        elif ch in ("q", "c"):
            self.close_window()
        elif ch == "o" or k == "ctrl+o":
            self._maximize()
        elif ch == "=":
            panes.even(ws.root)
            ws.relayout()
        elif ch == "w" or k == "ctrl+w":
            ws.cycle(1)

    # chrome ─────────────────────────────────────────────────

    def strip(self, width: int) -> Text:
        """The line under the header: the markets that matter at a glance, then the world's clocks."""
        return tape.strip(self.hub, width)

    def go_line(self, width: int) -> Text:
        """The command line at the bottom, vim style: `:FCST XAU` with the caret, then how to use it."""
        out = Text(no_wrap=True, overflow="crop")
        out.append(" :", style=f"bold {ORANGE}")
        text, c = self.line.text, self.line.caret
        out.append(text[:c], style=f"bold {TEXT}")
        out.append(text[c:c + 1] or " ", style=f"bold {INK} on {ORANGE}")
        out.append(text[c + 1:], style=f"bold {TEXT}")
        out.append("   type to filter · ↑ ↓ choose · enter opens · esc cancels", style=FAINT)
        return out

    def inside_rows(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """The focused page's own keys and links, for the key panel while inside it."""
        pane = self.ws.pane if self.app.view == "term" else None
        if pane is None:
            return []
        rows: list[tuple[str, list[tuple[str, str]]]] = []
        keys = [("", part) for part in pane.hint().split(" · ") if part]
        if keys:
            rows.append((pane.code, keys))
        menu = pane.menu()[:9]
        if menu:
            rows.append(("LINKS", [(str(i), label) for i, (label, _) in enumerate(menu, 1)]))
        return rows

    def suggestions(self, width: int, height: int = 14) -> Text:
        """The command list above the command line: a window of rows around the one chosen."""
        picks, at = self.line.picks, max(self.line.pick, 0)
        room = max(height - 1, 3)
        first = max(0, min(at - room // 2, len(picks) - room))
        rows = [ui_title(f"{len(picks)} {'buffers' if self.only_buffers else 'things to open'}", width,
                         "ctrl-j ctrl-k or ↑ ↓ · enter opens · esc")]
        kinds = {"buffer": ("open", GREEN), "page": ("page", ORANGE), "screen": ("screen", ORANGE), "layout": ("layout", DIM),
                 "needs": ("type", DIM), "function": ("page", DIM)}
        for i in range(first, min(first + room, len(picks))):
            s = picks[i]
            on = i == self.line.pick
            tag, colour = kinds.get(s.kind, ("", DIM))
            row = Text(no_wrap=True, overflow="crop")
            row.append(" ▶ " if on else "   ", style=f"bold {ORANGE}")
            row.append(f"{tag:<7}", style=colour)
            row.append(f"{s.label:<14}", style=f"bold {INK} on {ORANGE}" if on else f"bold {TEXT}")
            row.append(f"  {s.detail}", style=TEXT if on else DIM)
            rows.append(row)
        return Text("\n", no_wrap=True).join(rows)


class Scratch(FuncPane):
    """An empty window, waiting to be told what to show."""
    code, every = "NEW", 0

    async def load(self) -> None:
        self.title = "empty window"

    def draw(self, w: int, h: int) -> list[Text]:
        return [Text(""), Text("  An empty window.", style=f"bold {TEXT}"), Text(""),
                Text("  :        choose what to show here", style=DIM), Text("  SPC b b  one of the open buffers", style=DIM),
                Text("  SPC w d  close this window", style=DIM)]

    def command(self) -> str:
        return "HELP"


def ui_title(title: str, width: int, right: str = "") -> Text:
    out = Text(no_wrap=True, overflow="crop")
    out.append(f" {title} ", style=f"bold {INK} on {ORANGE}")
    if right and out.cell_len + len(right) + 4 < width:
        out.append(" " * (width - out.cell_len - len(right) - 1))
        out.append(right, style=FAINT)
    return out


# ── functions the shell runs itself rather than opening a pane ───────────────

def _set(shell: Shell, cmd: gobar.Command) -> bool:
    """`SET bitcoin.mempool http://umbrel.local:3006` writes terminal.toml and re-points the hub, no restart.
    `SET` alone opens the settings page."""
    if len(cmd.args) < 2:
        return False
    key, raw = cmd.args[0], " ".join(cmd.args[1:])
    if raw in ('""', "''", "none", "-"):
        raw = ""
    try:
        v = config.set_value(shell.hub.cfg, key, raw)
    except (KeyError, ValueError):
        shell.app.say(f"SET: no setting called {key}, or {raw!r} is the wrong kind of value. SET lists them.")
        return True
    config.save(shell.hub.cfg)
    shell.hub.apply_config()
    shell.app.say(f"{key} = {v if v != '' else '(empty)'} · saved, in effect now")
    for p in shell.ws.panes:
        p.loaded_at = 0.0                               # everything re-reads from the new backend
    return True


def _exp(shell: Shell, cmd: gobar.Command) -> bool:
    from ..funcs import tools
    pane = shell.ws.pane if shell.app.view == "term" else None
    shell.app.say(tools.export_pane(pane))
    return True


SHELL_RUN = {"SET": _set, "EXP": _exp}
