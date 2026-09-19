"""HELP, FIND, SRC, SET and EXP: the functions that describe and steer the terminal itself."""
from __future__ import annotations

import csv
import time
from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text

from ..data.core import ago
from ..term import config, gobar, panes, registry, ui
from ..term.panes import FuncPane
from ..term.registry import CATEGORIES, Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT


class HelpPane(FuncPane):
    code, every, selectable = "HELP", 0, True

    def _topic(self) -> Function | None:
        return registry.get(self.args[0]) if self.args else None

    async def load(self) -> None:
        fn = self._topic()
        self.title = f"{fn.code} · {fn.name}" if fn else "every function, by category"

    def _rows(self) -> list[Function]:
        return registry.every()

    def draw(self, w: int, h: int) -> list[Text]:
        fn = self._topic()
        if fn:
            self.n_rows = 0
            usage = " ".join(x for x in ((("<ticker>" + (" …" if fn.many else "")) if fn.takes else ""), fn.args) if x)
            out = [ui.t((f" {fn.code} ", f"bold #0D0D0D on {ORANGE}"), (f"  {fn.name}", f"bold {TEXT}"), (f"   {fn.category}", FAINT)), Text("")]
            out.append(ui.kv("usage  ", f"{fn.code} {usage}".strip() + " <GO>" + (f"    or  <ticker> {fn.code} <GO>" if fn.takes else "")))
            if fn.takes:
                out.append(ui.kv("takes  ", "  ".join(fn.takes), DIM))
            if fn.needs:
                out.append(ui.kv("reads  ", "  ".join(fn.needs), DIM))
            out += [Text("")] + ui.wrap(fn.help or fn.summary, w, DIM)
            out += [Text(""), ui.note("HELP lists every function · esc or another command leaves this page")]
            return out
        rows, out, line_of = self._rows(), [], {}
        self.n_rows = len(rows)
        i = 0
        for cat in CATEGORIES:
            fns = [f for f in rows if f.category == cat]
            if not fns:
                continue
            out.append(ui.section(cat.upper(), w))
            for f in fns:
                line_of[i] = len(out)
                on = i == self.cur
                row = Text(no_wrap=True, overflow="crop")
                row.append(f" {f.code:<6}", style=f"bold #0D0D0D on {ORANGE}" if on else f"bold {ORANGE}")
                row.append(f" {f.name:<24.24}", style=f"bold {TEXT}")
                row.append(f" {f.summary}", style=TEXT if on else DIM)
                out.append(row)
                i += 1
            out.append(Text(""))
        for fam, err in registry.failed().items():
            out.append(ui.note(f"funcs/{fam} did not load: {err}", RED))
        self.follow(line_of.get(self.cur, 0), h)
        return out

    def enter(self) -> str | None:
        rows = self._rows()
        return f"HELP {rows[self.cur].code}" if not self._topic() and rows else "HELP"

    def hint(self) -> str:
        return "enter opens a page" if not self._topic() else "enter back to the list"


class FindPane(FuncPane):
    code, every, selectable = "FIND", 0, True

    async def load(self) -> None:
        self.title = " ".join(self.args) or "everything"
        self.hits = gobar.find(" ".join(self.args), self.hub.book, panes.layout_names())

    def draw(self, w: int, h: int) -> list[Text]:
        hits = getattr(self, "hits", [])
        self.n_rows = len(hits)
        if not hits:
            return ui.centre([f"Nothing matches '{' '.join(self.args)}'.",
                              "FIND searches functions, instruments, companies and launchpads."], w, h)
        rows = [[Text(s.label, style=f"bold {ORANGE}"), s.detail, "  ".join(s.hints)] for s in hits]
        out = ui.table([ui.Col("code", "left", 10), ui.Col("what", "left", flex=True, style=DIM), ui.Col("functions", "left", style=FAINT)],
                       rows, w, self.cur)
        self.follow(self.cur + 2, h)
        return out

    def enter(self) -> str | None:
        hits = getattr(self, "hits", [])
        return hits[self.cur].text if hits else None

    def hint(self) -> str:
        return "enter runs it"


class SrcPane(FuncPane):
    code, every, tick = "SRC", 5, 1.0

    async def load(self) -> None:
        self.title = "every source: health, latency, budget"

    def draw(self, w: int, h: int) -> list[Text]:
        now, rows = time.time(), []
        for key, s in self.hub.sources.items():
            hl = s.health
            state = {"ok": Text("● ok", style=GREEN), "down": Text("● down", style=RED), "limited": Text("● limited", style=RED),
                     "idle": Text("○ idle", style=FAINT)}[hl.state]
            rows.append([Text(key, style=f"bold {ORANGE}"), state, s.host, f"{hl.latency_ms:,.0f} ms" if hl.latency_ms else "–",
                         f"{s.bucket.left:.1f}/{s.bucket.burst:g} @ {s.rate:g}/s", f"{hl.ok:,}", f"{hl.errors:,}",
                         ago(now - hl.last_ok_at) if hl.last_ok_at else "–", s.delay, s.serves])
        out = ui.table([ui.Col("source", "left"), ui.Col("state", "left"), ui.Col("host", "left", style=DIM), ui.Col("latency"),
                        ui.Col("budget left", style=DIM), ui.Col("ok"), ui.Col("err"), ui.Col("last ok", style=DIM),
                        ui.Col("delay", "left", style=DIM), ui.Col("serves", "left", flex=True, style=FAINT)], rows, w)
        errs = [(k, s.health) for k, s in self.hub.sources.items() if s.health.last_error]
        if errs:
            out += [Text(""), ui.section("LAST ERRORS", w)]
            out += [ui.t((f"{k:<10}", f"bold {ORANGE}"), (f" {ago(now - hl.last_error_at)} ago  ", FAINT), (hl.last_error, RED))
                    for k, hl in errs]
        c = self.hub.chain
        out += [Text(""), ui.section("STREAMS AND ROUTE", w)]
        out.append(ui.join([ui.kv("mempool stream", "live" if c.streaming else "polling", GREEN if c.streaming else DIM),
                            ui.kv("bitcoin order", " → ".join(self.hub.bitcoin_order()), DIM),
                            ui.kv("proxy", self.hub.cfg.get("socks5") or "none (direct)", DIM)]))
        out += [Text(""), ui.note("Self-host any backend: SET bitcoin.mempool http://umbrel.local:3006 · it takes effect at once. "
                                  "SET socks5 socks5h://127.0.0.1:9050 routes every request through Tor.")]
        for fam, err in registry.failed().items():
            out.append(ui.note(f"funcs/{fam} did not load: {err}", RED))
        return out

    def export(self):
        return (["source", "host", "state", "ok", "errors", "latency_ms", "last_error"],
                [[k, s.host, s.health.state, s.health.ok, s.health.errors, round(s.health.latency_ms), s.health.last_error]
                 for k, s in self.hub.sources.items()])


SETTINGS = (
    ("tape", "instruments on the ticker tape, in order (MEMP is the mempool's size)"),
    ("clocks", "time zones for the status bar, IANA names"),
    ("default_launchpad", "the launchpad the terminal opens on"),
    ("socks5", "one proxy for every request: socks5h://127.0.0.1:9050 is Tor"),
    ("sec_user_agent", "\"Your Name you@example.com\". The SEC requires a contact on every request and receives it"),
    ("bitcoin.order", "which backend answers first: mempool bitview esplora (prepend core, electrum with a node)"),
    ("bitcoin.mempool", "a mempool instance, yours or the public one"),
    ("bitcoin.bitview", "a Bitview (BRK) server: bitviewd serves http://localhost:3110"),
    ("bitcoin.esplora", "an Esplora server"),
    ("bitcoin.electrum", "ssl://your-server:50002"),
    ("bitcoin.core_rpc", "http://127.0.0.1:8332, cookie auth by default"),
    ("wallet.barkd", "your barkd daemon: http://127.0.0.1:3000. Its token lives in the keychain"),
    ("wallet.send_cap_sats_per_day", "the most the terminal will send from barkd in one UTC day"),
    ("wallet.watch_public_ok", "true lets watch-only addresses be looked up on a public backend"),
)


class SetPane(FuncPane):
    code, every, selectable = "SET", 0, True

    async def load(self) -> None:
        self.title = str(config.path())

    def _get(self, dotted: str):
        node = self.hub.cfg
        for p in dotted.split("."):
            node = node.get(p, "") if isinstance(node, dict) else ""
        return node

    def draw(self, w: int, h: int) -> list[Text]:
        self.n_rows = len(SETTINGS)
        rows = []
        for key, what in SETTINGS:
            v = self._get(key)
            shown = " ".join(map(str, v)) if isinstance(v, list) else str(v)
            rows.append([Text(key, style=f"bold {ORANGE}"), Text(shown or "(empty)", style=TEXT if shown else FAINT), what])
        out = ui.table([ui.Col("setting", "left"), ui.Col("value", "left", 44), ui.Col("what it does", "left", flex=True, style=FAINT)],
                       rows, w, self.cur)
        out += [Text(""), ui.section("OPTIONAL KEYS", w)]
        for name in ("fred", "typesafe", "stooq", "barkd"):
            have = bool(config.secret(name))
            out.append(ui.t((f"{name:<10}", f"bold {ORANGE}"), ("in the keychain" if have else "not set", GREEN if have else FAINT),
                            ("   the terminal works without it", FAINT) if not have else ""))
        out += [Text(""), ui.note("enter puts SET <setting> on the GO bar · values save to terminal.toml and take effect at once, no restart"),
                ui.note("Lookups on a public backend tell that backend what you looked at. Your own node, or Tor, keeps them private.")]
        self.follow(self.cur + 2, h)
        return out

    def enter(self) -> str | None:
        self.app.shell.focus_go(f"SET {SETTINGS[self.cur][0]} ")     # type: ignore[attr-defined]
        return None

    def hint(self) -> str:
        return "enter edits"


def export_pane(pane: FuncPane | None) -> str:
    """EXP: the focused pane's table or series as CSV under ~/Downloads/glimpse/. Returns the status line message."""
    if pane is None:
        return "EXP exports the focused pane. Open a launchpad first."
    data = pane.export()
    if not data or not data[1]:
        return f"{pane.code} has no table to export."
    header, rows = data
    d = Path.home() / "Downloads" / "glimpse"
    try:
        d.mkdir(parents=True, exist_ok=True)
        name = "_".join([pane.code, *(s.ticker for s in pane.securities), datetime.now(UTC).strftime("%Y%m%d_%H%M%S")]) + ".csv"
        with open(d / name, "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(header)
            wr.writerows(rows)
            wr.writerow([])
            wr.writerow(["# sources"] + [f"{p.source} ({p.delay}, as of {datetime.fromtimestamp(p.as_of, UTC):%Y-%m-%d %H:%M} UTC)"
                                         for p in pane.provs])
    except OSError as e:
        return f"EXP could not write: {e}"
    return f"Exported {len(rows):,} rows to {d / name}"


_DOWN = "It reads nothing from the network."
register(Function("HELP", "Help", "Tools", "every function by category; HELP <FUNC> opens its page", HelpPane, args="[function]",
                  help="Lists every function with its one-line summary. Enter opens a function's page: what it shows, where each "
                       "number comes from, how often it refreshes and what happens when a source is down. F1 opens the page for "
                       "the focused pane. " + _DOWN))
register(Function("FIND", "Find", "Tools", "search functions, instruments, companies and launchpads", FindPane, args="<text>",
                  help="Searches everything the GO bar's autocomplete searches, with more room: function codes, names and "
                       "summaries, instruments and their names, SEC company tickers once a company function has loaded them, "
                       "and launchpads. Enter runs the row. " + _DOWN))
register(Function("SRC", "Sources", "Tools", "every data source: health, latency, budget left, last error", SrcPane,
                  help="One row per host: its state, the latency of the last request, what is left of its request budget, how "
                       "many requests succeeded and failed, and what it serves. Below: the last error from each source, whether "
                       "the mempool stream is live or polling, the Bitcoin backend order and the proxy. Every Bitcoin backend "
                       "is a URL you can point at your own node with SET. Refreshes every 5 s from counters; makes no requests."))
register(Function("SET", "Settings", "Tools", "backends, keys, Tor, the tape, clocks", SetPane, args="[setting value]",
                  help="Shows ~/.config/glimpse/terminal.toml. SET <setting> <value> changes one value, saves the file and applies "
                       "it at once: switching bitcoin.mempool to a self-hosted URL needs no restart. Secrets are never stored in "
                       "this file; they live in the OS keychain. " + _DOWN))
register(Function("EXP", "Export", "Tools", "export the focused pane's table to CSV", None,
                  help="Writes the focused pane's table or series to ~/Downloads/glimpse/<FUNC>_<ticker>_<time>.csv, with a last "
                       "row naming each source, its delay and its as-of time. " + _DOWN))
