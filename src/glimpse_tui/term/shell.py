"""The shell: what the GO bar's commands mean inside the app.

The app keeps its screens and its keys. This object adds the launchpad view beside them: it owns the hub, the
workspace, the GO bar's line and the `ctrl-w` chords, and it turns a parsed `Command` into a pane. Today's
screens run as functions (`MKT`, `HM`, `SLIP`, `PORT`, `BOTS`) by switching the app to the view they always had,
so every key they had still works (PLAN D31).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text

from ..theme import DIM, FAINT, ORANGE, TEXT
from . import config, gobar, panes, registry, tape
from .hub import Hub
from .instruments import Instrument
from .panes import FuncPane, Leaf, Workspace

if TYPE_CHECKING:
    from ..app import Terminal

INK = "#0D0D0D"
PASS_THROUGH = {"q", "c", "?", "L", "O", "r", "p", "B", "f"}      # the app's own keys that still work from a launchpad
LEGACY_VIEW = {"MKT": "main", "HM": "heatmap", "SLIP": "heatmap", "PORT": "portfolio", "BOTS": "bots"}


class Shell:
    def __init__(self, app: Terminal) -> None:
        self.app = app
        self.hub = Hub()
        self.line = gobar.Line()
        self.line.load_history()
        self.go_focus = False
        self.chord = ""                         # "ctrl+w" while waiting for its second key
        self.started = False
        self.lp_name = ""
        self._back_to = "term"                  # the view the GO bar was opened from, for Esc

    # wiring ─────────────────────────────────────────────────

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
        """Once a second from the app: reload any pane whose data is due. Rendering never waits for these."""
        if self.app.view != "term":
            return
        now = time.time()
        for p in self.ws.panes:
            if p.due(now) and p.size.width > 0:
                self.app.run_worker(p.reload(), group=f"pane-{id(p)}")

    # launchpads and panes ───────────────────────────────────

    def show(self, name: str | None = None) -> None:
        """Switch the app to the launchpad view, loading `name` (or the default) if nothing is open yet."""
        self.app.view = "term"
        self.ensure_started()
        if name or not self.ws.root:
            self.load_launchpad(name or self.hub.cfg.get("default_launchpad", "BTC"))

    def load_launchpad(self, name: str) -> bool:
        root = panes.load_layout(name)
        if root is None:
            self.app.say(f"No launchpad called {name.upper()}. LP lists them: {', '.join(panes.layout_names())}")
            return False
        ws = self.ws
        for p in list(ws.panes):
            p.remove()
        ws.root, ws.zoomed, self.lp_name = root, False, name.upper()
        for lf in panes.leaves(root):
            self._fill(lf)
        ws.focused = panes.leaves(root)[0]
        ws.relayout()
        return True

    def _fill(self, leaf: Leaf, cmd: gobar.Command | None = None) -> None:
        """Put the pane for `leaf.command` into the workspace. A command that no longer parses becomes HELP."""
        cmd = cmd or gobar.parse(leaf.command, self.hub.book)
        fn = registry.get(cmd.func) if cmd else None
        if not (fn and fn.make):
            cmd = gobar.Command("HELP", args=(), raw="HELP")
            fn = registry.get("HELP")
        pane: FuncPane = fn.make.from_command(self.hub, cmd)
        leaf.command = cmd.text()
        if leaf.pane:
            leaf.pane.remove()
        leaf.pane = pane
        self.ws.mount(pane)

    def open(self, cmd: gobar.Command, where: str = "") -> None:
        """Run a function in the focused pane, or in a new split when `where` is 's' or 'v'."""
        ws = self.ws
        if not ws.root:
            ws.root = ws.focused = Leaf(cmd.text())
            self._fill(ws.focused, cmd)
        elif where and len(ws.panes) < panes.MAX_PANES and ws.focused:
            new = Leaf(cmd.text())
            ws.root = panes.split(ws.root, ws.focused, new, "col" if where == "s" else "row")
            self._fill(new, cmd)
            ws.focused = new
        else:
            if where:
                self.app.say("Nine panes is the most a launchpad holds. ctrl-w q closes one.")
            self._fill(ws.focused or panes.leaves(ws.root)[0], cmd)
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
            self._legacy(fn.code)
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

    def _legacy(self, code: str) -> None:
        app = self.app
        view = LEGACY_VIEW[code]
        if view == "heatmap" and app.view != "heatmap":
            app.view, app.hm_col, app.hm_anchor = "heatmap", app.m_cur, None
            app.load_candles()
        elif view == "bots":
            app.view = "bots"
            app.load_bots()
            app.scan_bots()
        else:
            app.view = view
            if view == "portfolio":
                app.load_account()
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

    # keys ───────────────────────────────────────────────────

    def focus_go(self, seed: str = "") -> None:
        self._back_to = self.app.view
        self.go_focus = True
        self.line.clear()
        if seed:
            self.line.set(seed)
        self._suggest()

    def _suggest(self) -> None:
        self.line.picks = gobar.complete(self.line.text, self.hub.book, panes.layout_names(), self.hub.series_seen)

    def go_key(self, k: str, ch: str | None) -> None:
        """Every key while the GO bar has focus."""
        if k == "escape":
            self.go_focus = False
            self.line.clear()
        elif k == "enter":
            text = self.line.chosen()
            self.go_focus = False
            self.line.clear()
            self.run(text)
        elif self.line.key(k, ch):
            if k not in ("up", "down"):
                self._suggest()

    def key(self, k: str, ch: str | None) -> bool:
        """A key in the launchpad view. False hands it back to the app (for the keys in PASS_THROUGH only)."""
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
        elif k == "tab":
            ws.cycle(1)
        elif k == "shift+tab":
            ws.cycle(-1)
        elif k == "escape":
            if ws.zoomed:
                ws.zoomed = False
                ws.relayout()
        elif ch and ch.isdigit() and ch != "0" and ws.pane and (menu := ws.pane.menu()):
            i = int(ch) - 1
            if i < len(menu):
                self.run(menu[i][1])
        elif ch and ch.isdigit() and ch != "0" and int(ch) <= len(ws.panes):
            ws.focus_leaf(panes.leaves(ws.root)[int(ch) - 1])       # no menu on this page: the digits pick a pane
        elif k == "enter" and ws.pane:
            if go := ws.pane.enter():
                self.run(go)
        elif ch == "g" and ws.pane:
            ws.pane.home()
        elif ws.pane and ws.pane.on_key_(k, ch):
            pass
        elif ch in PASS_THROUGH or k in ("ctrl+c", "ctrl+l"):
            return False
        return True

    def _window_key(self, k: str, ch: str) -> None:
        ws = self.ws
        if not ws.root:
            return
        if ch in ("h", "j", "k", "l"):
            ws.move(ch)
        elif k in ("left", "down", "up", "right"):
            ws.move({"left": "h", "down": "j", "up": "k", "right": "l"}[k])
        elif ch in ("s", "v") and ws.pane:
            if len(ws.panes) >= panes.MAX_PANES:
                self.app.say("Nine panes is the most a launchpad holds. ctrl-w q closes one.")
                return
            self.open(gobar.parse(ws.pane.command(), self.hub.book) or gobar.Command("HELP"), where=ch)
        elif ch in ("q", "c") and ws.focused:
            if len(ws.panes) <= 1:
                self.app.say("The last pane stays. LP <name> loads another launchpad; q quits.")
                return
            gone, order = ws.focused, panes.leaves(ws.root)
            nxt = order[(order.index(gone) + 1) % len(order)]
            ws.root = panes.close(ws.root, gone)
            if gone.pane:
                gone.pane.remove()
            ws.zoomed = False
            ws.focus_leaf(nxt)
        elif ch == "o" or k == "ctrl+o":
            ws.zoomed = not ws.zoomed
            ws.relayout()
        elif ch == "=":
            panes.even(ws.root)
            ws.relayout()
        elif ch == "w" or k == "ctrl+w":
            ws.cycle(1)

    # chrome ─────────────────────────────────────────────────

    def header(self, width: int) -> Text:
        app = self.app
        bal = app.wallet.balance_sats if app.api.authenticated and app.wallet else None
        return Text("\n", no_wrap=True).join([tape.status_bar(self.hub, width, bal, app.api.authenticated), tape.tape(self.hub, width)])

    def go_line(self, width: int) -> Text:
        """` > BTC <GO>   BTC Bitcoin · CRYPTO   1 DES  2 MEMP …`: the bar, then the focused pane's security and menu."""
        out = Text(no_wrap=True, overflow="crop")
        out.append(" > ", style=f"bold {ORANGE}")
        if self.go_focus:
            text, c = self.line.chosen() if self.line.pick >= 0 else self.line.text, self.line.caret
            if self.line.pick >= 0:
                c = len(text)
            out.append(text[:c], style=f"bold {TEXT}")
            out.append(text[c:c + 1] or " ", style=f"bold {INK} on {ORANGE}")
            out.append(text[c + 1:], style=f"bold {TEXT}")
            out.append(" ")
            out.append("<GO>", style=f"bold {INK} on #0bd98a")
            out.append("   enter go · tab complete · ↑ ↓ pick or history · esc leave", style=FAINT)
            return out
        pane = self.ws.pane if self.app.view == "term" else None
        if pane is None:
            out.append("` or : opens the GO bar", style=FAINT)
            return out
        sec: Instrument | None = pane.security
        out.append(f"{sec.ticker if sec else pane.code} ", style=f"bold {ORANGE}")
        out.append("<GO>", style=f"bold {INK} on #0bd98a")
        if sec:
            out.append(f"   {sec.ticker} ", style=f"bold {TEXT}")
            out.append(f"{sec.name} · {sec.cls}", style=DIM)
        else:
            fn = registry.get(pane.code)
            out.append(f"   {fn.name if fn else pane.code}", style=DIM)
        out.append("  ")
        for i, (label, _) in enumerate(pane.menu()[:9], 1):
            out.append(f" {i} ", style=f"bold {ORANGE}")
            out.append(label, style=TEXT)
        if self.lp_name and out.cell_len + len(self.lp_name) + 6 < width:
            out.append(" " * (width - out.cell_len - len(self.lp_name) - 5))
            out.append(f"LP {self.lp_name} ", style=FAINT)
        return out

    def suggestions(self, width: int) -> Text:
        rows = []
        for i, s in enumerate(self.line.picks):
            on = i == self.line.pick
            row = Text(no_wrap=True, overflow="crop")
            row.append(f"   {s.label:<9}", style=f"bold {INK} on {ORANGE}" if on else f"bold {ORANGE}")
            row.append(f" {s.detail}", style=TEXT if on else DIM)
            hints = "  ".join(s.hints)
            if hints and row.cell_len + len(hints) + 3 < width:
                row.append(" " * (width - row.cell_len - len(hints) - 2))
                row.append(hints, style=FAINT)
            rows.append(row)
        return Text("\n", no_wrap=True).join(rows)


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
