"""AL, NOTE, CALC and ASK (TERMINAL.md 11)."""
from __future__ import annotations

import json
import re
import time

from rich.text import Text

from .. import fmt
from ..data import btcmath
from ..term import alerts, config, registry, ui
from ..term.panes import FuncPane
from ..term.registry import CLASSES, Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT

_engine: alerts.Engine | None = None


def engine() -> alerts.Engine:
    """One alert engine for the process: the page edits it, the shell's tick evaluates it."""
    global _engine
    if _engine is None or _engine._path() != (config.config_dir() / "alerts.json"):
        _engine = alerts.Engine()
    return _engine


class AlPane(FuncPane):
    code, every, selectable, tick = "AL", 0, True, 1.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.note = ""
        if self.args:                                   # `AL BTC below 75000` adds the rule straight from the GO bar
            self._add(" ".join(([security.ticker] if security else []) + list(self.args)))

    def _add(self, text: str) -> None:
        try:
            r = engine().add(text)
            self.note = f"Added: {r.text}"
        except ValueError as e:
            self.note = str(e)

    async def load(self) -> None:
        self.title = "alerts"

    def cache_key(self) -> tuple:
        return tuple((r.armed, r.fired_at, r.last) for r in engine().rules)

    def draw(self, w: int, h: int) -> list[Text]:
        rules = engine().rules
        self.n_rows = len(rules)
        out = []
        if rules:
            rows = []
            for r in rules:
                state = Text("armed", style=GREEN) if r.armed and not r.fired_at else Text(
                    f"fired {ui.when(r.fired_at)} ago", style=ORANGE) if r.fired_at else Text("waiting", style=DIM)
                rows.append([r.text, r.kind, state, r.last])
            out += ui.table([ui.Col("rule", "left", flex=True), ui.Col("kind", "left", style=DIM), ui.Col("state", "left"),
                             ui.Col("last seen", "left", style=FAINT)], rows, w, self.cur)
        else:
            out += [ui.note("No alerts yet.", DIM), Text("")]
        out += [Text(""), ui.fit([("a", f"bold {ORANGE}"), (" add   ", DIM), ("x", f"bold {ORANGE}"), (" delete   ", DIM),
                                  ("or from the GO bar: AL BTC below 75000", FAINT)], w), Text("")]
        out += ui.wrap("BTC below 75000 · XAU above 4500 · fee under 3 · mempool over 120 · block from Foundry · block · difficulty · "
                       "MSTR files 8-K · odds on 80000 to 82000 at 16:00 above 20%", w, FAINT)
        if self.note:
            out += [Text(""), ui.note(self.note, ORANGE)]
        self.follow(self.cur + 2, h)
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if ch == "a":
            self.app.run_worker(self._ask())
        elif ch == "x" and engine().rules:
            engine().remove(self.cur)
            self.cur = max(0, min(self.cur, len(engine().rules) - 1))
        else:
            return False
        return True

    async def _ask(self) -> None:
        from ..app import Prompt
        examples = "BTC below 75000 · fee under 3 · block from Foundry · odds on 80000 to 82000 at 16:00 above 20%"
        v = await self.app.push_screen_wait(Prompt("New alert", examples))
        if v and v.strip():
            self._add(v)
            self.bump()

    def hint(self) -> str:
        return "a add · x delete"

    def export(self):
        return ["rule", "kind", "armed", "fired_at", "last"], [[r.text, r.kind, r.armed, r.fired_at, r.last] for r in engine().rules]


# ── NOTE ────────────────────────────────────────────────────

def _notes_path():
    return config.config_dir() / "notes.json"


def load_notes() -> dict[str, list[dict]]:
    try:
        return json.loads(_notes_path().read_text())
    except (OSError, ValueError):
        return {}


def save_notes(notes: dict[str, list[dict]]) -> None:
    try:
        config.config_dir().mkdir(parents=True, exist_ok=True)
        _notes_path().write_text(json.dumps(notes, indent=1))
    except OSError:
        pass


class NotePane(FuncPane):
    code, every, selectable = "NOTE", 0, True

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.subject = security.ticker if security else "GENERAL"
        if self.args:                                   # `NOTE BTC watching the 200-day` writes it at once
            notes = load_notes()
            notes.setdefault(self.subject, []).append({"at": time.time(), "text": " ".join(self.args)})
            save_notes(notes)
            self.args = ()

    async def load(self) -> None:
        self.title = f"notes · {self.subject}" if self.security else "notes · everything"

    def _rows(self) -> list[tuple[str, int, dict]]:
        notes = load_notes()
        keys = [self.subject] if self.security else sorted(notes)
        return [(k, i, n) for k in keys for i, n in enumerate(notes.get(k, []))][::-1]

    def draw(self, w: int, h: int) -> list[Text]:
        rows = self._rows()
        self.n_rows = len(rows)
        if not rows:
            return [ui.note("No notes yet.", DIM), Text(""), ui.note("NOTE BTC watching the 200-day average   ·   a adds one here", FAINT)]
        table = ui.table([ui.Col("when UTC", "left", style=DIM), ui.Col("on", "left", style=f"bold {ORANGE}"),
                          ui.Col("note", "left", flex=True)],
                         [[ui.stamp(n["at"])[:12], k, n["text"]] for k, _, n in rows], w, self.cur)
        self.follow(self.cur + 2, h)
        return table

    def key(self, k: str, ch: str | None) -> bool:
        if ch == "a":
            self.app.run_worker(self._ask())
        elif ch == "x" and (rows := self._rows()):
            key, i, _ = rows[self.cur]
            notes = load_notes()
            del notes[key][i]
            save_notes(notes)
            self.cur = max(0, self.cur - 1)
        else:
            return False
        return True

    async def _ask(self) -> None:
        from ..app import Prompt
        v = await self.app.push_screen_wait(Prompt(f"Note on {self.subject}", "Plain text. It stays on this computer."))
        if v and v.strip():
            notes = load_notes()
            notes.setdefault(self.subject, []).append({"at": time.time(), "text": v.strip()})
            save_notes(notes)
            self.bump()

    def command(self) -> str:
        return " ".join(["NOTE", *([self.security.ticker] if self.security else [])])

    def hint(self) -> str:
        return "a add · x delete"

    def export(self):
        return ["when", "on", "note"], [[ui.stamp(n["at"]), k, n["text"]] for k, _, n in self._rows()]


# ── CALC ────────────────────────────────────────────────────

def calculate(words: list[str], price: float | None) -> list[tuple[str, str]]:
    """(label, value) lines for a CALC expression. Raises ValueError with usage when it does not parse."""
    text = " ".join(words).lower().replace(",", "").replace("_", "")
    num = r"([0-9]*\.?[0-9]+)"
    if m := re.match(rf"^\$\s*{num}$|^{num}\s*(usd|dollars?)$", text):
        usd = float(m.group(1) or m.group(2))
        if not price:
            raise ValueError("No BTC price yet. It arrives with the tape.")
        return [("dollars", ui.usd(usd)), ("sats", fmt.sats(usd / price * 1e8)), ("BTC", f"{usd / price:.8f}"),
                ("at", f"{price:,.0f} USD per BTC")]
    if m := re.match(rf"^{num}\s*(sats?|btc|₿)?$", text):
        v, unit = float(m.group(1)), m.group(2) or "sats"
        sats = v * 1e8 if unit == "btc" else v
        out = [("sats", fmt.sats(sats)), ("BTC", f"{sats / 1e8:.8f}")]
        return out + ([("dollars", ui.usd(sats / 1e8 * price)), ("at", f"{price:,.0f} USD per BTC")] if price else [("dollars", "no price yet")])
    if m := re.match(rf"^fee {num} {num}$", text):
        vsize, rate = float(m.group(1)), float(m.group(2))
        cost = btcmath.fee_sats(vsize, rate)
        return [("size", f"{vsize:,.1f} vB"), ("rate", f"{rate:g} sat/vB"), ("fee", fmt.sats(cost)),
                ("dollars", ui.usd(cost / 1e8 * price) if price else "no price yet")]
    if m := re.match(rf"^tx (\d+) (\d+)(?: ([a-z0-9-]+))?(?: {num})?$", text):
        kind = m.group(3) or "p2wpkh"
        if kind not in btcmath.INPUT_VB:
            raise ValueError("Script types: " + ", ".join(btcmath.INPUT_VB))
        vsize = btcmath.tx_vsize(int(m.group(1)), int(m.group(2)), kind, kind if kind in btcmath.OUTPUT_VB else "p2wpkh")
        out = [("transaction", f"{m.group(1)} inputs, {m.group(2)} outputs, {kind}"), ("size", f"{vsize:,.1f} vB")]
        if m.group(4):
            cost = btcmath.fee_sats(vsize, float(m.group(4)))
            out += [("fee", f"{fmt.sats(cost)} at {float(m.group(4)):g} sat/vB"),
                    ("dollars", ui.usd(cost / 1e8 * price) if price else "no price yet")]
        return out
    if m := re.match(rf"^kelly {num}%? {num}x?(?: {num})?$", text):
        p, odds = float(m.group(1)), float(m.group(2))
        p = p / 100 if p > 1 else p
        f = btcmath.kelly(p, odds)
        out = [("win probability", f"{p:.1%}"), ("decimal odds", f"{odds:g}× (stake included)"), ("break-even", f"{1 / odds:.1%}"),
               ("edge", f"{p * odds - 1:+.1%} per unit staked"),
               ("full Kelly", f"{max(f, 0):.1%} of bankroll" if f > 0 else "no bet: the odds do not pay"),
               ("quarter Kelly", f"{max(f, 0) / 4:.1%} of bankroll (what the bots use)")]
        if m.group(3) and f > 0:
            bank = float(m.group(3))
            out += [("stake", f"{fmt.sats(bank * f / 4)} of a {fmt.sats(bank)} bankroll at quarter Kelly")]
        return out
    raise ValueError("usage")


class CalcPane(FuncPane):
    code, every, tick = "CALC", 0, 5.0

    async def load(self) -> None:
        self.title = " ".join(self.args) or "sats, fees, Kelly"

    def draw(self, w: int, h: int) -> list[Text]:
        out = []
        if self.args:
            try:
                for label, value in calculate(list(self.args), self.hub.btc_price()):
                    out.append(ui.t((f"{label:<18}", FAINT), (value, f"bold {TEXT}")))
            except ValueError as e:
                out.append(ui.note(str(e) if str(e) != "usage" else f"CALC could not read '{' '.join(self.args)}'.", RED))
            out.append(Text(""))
        out += [ui.section("CALC", w), *[ui.t((f"{a:<34}", f"bold {ORANGE}"), (b, DIM)) for a, b in (
            ("CALC 50000", "sats to BTC and dollars"), ("CALC 0.015 btc", "BTC to sats and dollars"), ("CALC $25", "dollars to sats"),
            ("CALC fee 140.5 12", "the fee for a size in vB at a rate in sat/vB"),
            ("CALC tx 2 2 p2wpkh 12", "inputs, outputs, script type, rate"),
            ("CALC kelly 0.6 2.5 200000", "probability, decimal odds, bankroll in sats"))]]
        if self.hub.btc_price():
            out += [Text(""), ui.note(f"Dollars use the composite BTC price, {self.hub.btc_price():,.0f}, from the tape.")]
        return out

    def enter(self) -> str | None:
        self.app.shell.focus_go("CALC ")       # type: ignore[attr-defined]
        return None

    def hint(self) -> str:
        return "enter types another"


# ── ASK ─────────────────────────────────────────────────────

ROUTES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("chart", "graph", "plot", "compare"), "GP"),
    (("mempool", "unconfirmed", "next block", "congest"), "MEMP"), (("fee", "sat/vb", "how much to send"), "FEES"),
    (("halving", "halvening"), "HALV"), (("supply", "inflation", "issuance", "how many bitcoin"), "SUPL"),
    (("hashrate", "hash rate"), "HASH"), (("difficulty", "retarget"), "DIFF"), (("hashprice", "miner revenue"), "HASHP"),
    (("pool", "who mined", "miners share"), "MINE"), (("lightning", "channels"), "LN"), (("replace", "rbf"), "RBF"),
    (("oracle", "on-chain price"), "ORCL"), (("mvrv", "nupl", "sopr", "realized", "on-chain", "onchain", "cycle"), "ONCH"),
    (("urpd", "cost basis distribution"), "URPD"), (("hodl", "waves", "coin age"), "WAVE"), (("correlat",), "CORR"),
    (("yield", "treasury curve", "rates", "2s10s", "fed funds"), "RATES"),
    (("liquidity", "balance sheet", "m2", "reverse repo", "tga"), "MACRO"),
    (("dollar index", "dxy", "fx ", "forex", "currenc"), "FX"), (("gold", "oil", "brent", "commodit", "silver", "wheat"), "GLCO"),
    (("indices", "indexes", "stock market", "s&p", "nasdaq", "nikkei"), "WEI"), (("cpi", "unemployment", "payroll", "gdp", "economic"), "ECO"),
    (("heatmap of", "performance", "winners", "losers"), "HMAP"), (("implied vol", "volatility", "dvol", "deribit"), "DVOL"),
    (("option", "strike", "digital", "greeks"), "OMON"), (("treasury compan", "who holds", "holdings", "mnav"), "TRSY"),
    (("miners", "mining stocks", "mining compan"), "MINR"), (("etf", "ibit", "spot bitcoin fund"), "ETF"),
    (("wallet", "balance", "receive", "invoice", "send sats", "deposit"), "WAL"), (("portfolio", "positions", "p&l"), "PORT"),
    (("forecast", "prediction", "probab", "odds", "bet"), "HM"), (("bot", "model zoo", "strateg"), "BOTS"),
    (("alert", "notify", "tell me when"), "AL"), (("source", "latency", "is .* down", "health"), "SRC"),
    (("setting", "tor", "proxy", "self-host", "my node", "backend"), "SET"), (("news", "headline", "filing"), "TOP"),
    (("price of",), "GP"), (("bitcoin", "btc"), "BTC"),
)


def route(words: str, book) -> tuple[str, str]:
    """(command, why) for plain English. A keyword router: it never guesses silently, the page shows what it chose."""
    low = " " + words.lower().strip() + " "
    tickers = [t for t in re.findall(r"[A-Za-z0-9.]{2,10}", words) if book.get(t) and t.upper() not in ("BTC", "AT", "ON", "IT", "ALL", "A")]
    if m := re.search(r"\b([0-9a-fA-F]{64})\b", words):
        return f"TX {m.group(1)}", "that is a transaction id"
    for keys, code in ROUTES:
        hit = next((k for k in keys if re.search(k, low)), None)
        if not hit:
            continue
        fn = registry.get(code)
        if fn is None:
            continue
        if code == "GP":
            secs = tickers or ["BTC"]
            return "GP " + " ".join(s.upper() for s in secs[:4]), f"'{hit.strip()}' reads as a chart"
        if fn.takes and tickers and book.get(tickers[0]).cls in fn.takes:
            return f"{tickers[0].upper()} {code}", f"'{hit.strip()}' and the ticker {tickers[0].upper()}"
        return code, f"'{hit.strip()}' is what {code} shows"
    if tickers:
        ins = book.get(tickers[0])
        fn = registry.default_for(ins.cls)
        return f"{ins.ticker} {fn.code if fn else 'GP'}", f"{ins.ticker} is {ins.name}"
    return "", ""


class AskPane(FuncPane):
    code, every = "ASK", 0

    async def load(self) -> None:
        self.words = " ".join(self.args)
        self.title = self.words[:50]
        self.cmd, self.why = route(self.words, self.hub.book)
        self.keyed = bool(config.secret("typesafe"))

    def draw(self, w: int, h: int) -> list[Text]:
        out = [ui.t(("you asked   ", FAINT), (getattr(self, "words", ""), TEXT)), Text("")]
        if getattr(self, "cmd", ""):
            out += [ui.t(("I read that as   ", FAINT), (f" {self.cmd} ", f"bold #0D0D0D on {ORANGE}"), (" <GO>", f"bold {GREEN}")),
                    ui.t(("because   ", FAINT), (self.why, DIM)), Text(""),
                    ui.t(("enter", f"bold {ORANGE}"), (" runs it. Nothing runs until you press it. ", DIM),
                         ("esc", f"bold {ORANGE}"), (" or another command leaves.", DIM))]
        else:
            out += [ui.note("I could not match that to a function.", ORANGE), Text(""),
                    ui.note("HELP lists every function. FIND <text> searches them.")]
        out += [Text("")] + ui.wrap("This is the keyword router: it matches your words against each function's subject and any ticker it "
                                    "recognises. Routing through Jev's function calling with a TypeSafe key is not built yet." +
                                    (" A TypeSafe key is stored, and is not used by this router." if getattr(self, "keyed", False) else ""),
                                    w, FAINT)
        return out

    def enter(self) -> str | None:
        return getattr(self, "cmd", "") or None

    def hint(self) -> str:
        return "enter runs the command shown"


_LOCAL = "It reads only what the terminal already holds in memory and makes no request of its own."
register(Function("AL", "Alerts", "Tools", "alerts on price, fees, blocks, the mempool, filings and Glimpse odds", AlPane, takes=CLASSES,
                  optional=True,
                  args="[rule in words]", needs=("quote", "fees", "tip", "glimpse closes"),
                  help="Rules in plain words: BTC below 75000, XAU above 4500, fee under 3, mempool over 120, block from Foundry, block, "
                       "difficulty, MSTR files 8-K, odds on 80000 to 82000 at 16:00 above 20%. The terminal checks every rule once a second "
                       "against the tape, the chain state and Glimpse's loaded closes. A level rule fires once when it becomes true and "
                       "re-arms when it stops being true. A block, difficulty or filing rule fires on each event. A fired alert rings the "
                       "terminal bell, shows on the status line for ten seconds and joins the TOP feed. Filing rules need the SEC contact "
                       "and a company page or TOP open. News rules wait for the Jev news hub. a adds, x deletes. Rules are kept in "
                       "~/.config/glimpse/alerts.json. " + _LOCAL))
register(Function("NOTE", "Notes", "Tools", "notes attached to a security, kept on this computer", NotePane, takes=CLASSES, optional=True,
                  args="[text]",
                  help="NOTE BTC watching the 200-day average writes a note on BTC. NOTE BTC lists BTC's notes and NOTE lists them all, "
                       "newest first. a adds one, x deletes the selected one. Notes are plain text in ~/.config/glimpse/notes.json and "
                       "never leave this computer. " + _LOCAL))
register(Function("CALC", "Calculator", "Tools", "sats and dollars, a transaction's fee, a Kelly stake", CalcPane, args="<expression>",
                  help="CALC 50000 or CALC 0.015 btc converts to BTC, sats and dollars. CALC $25 converts dollars to sats. CALC fee 140.5 12 "
                       "is the fee for 140.5 vB at 12 sat/vB. CALC tx 2 2 p2wpkh 12 sizes a transaction from its inputs, outputs and script "
                       "type, then prices it. CALC kelly 0.6 2.5 200000 gives the Kelly fraction for a 60% chance at 2.5× decimal odds, and "
                       "the quarter-Kelly stake from a bankroll in sats, which is what the bots use. Dollars use the composite BTC price "
                       "from the tape and say so. " + _LOCAL))
register(Function("ASK", "Ask", "Tools", "plain English to a function; it shows the command before running it", AskPane, args="<words>",
                  help="Anything typed into the GO bar that is not a command comes here. A keyword router matches the words against each "
                       "function's subject and any ticker it recognises, shows the command it chose and why, and runs it only when you "
                       "press Enter. It never runs anything by itself. Routing through Jev's function calling with a TypeSafe key is "
                       "planned and not built, so no words leave this computer. " + _LOCAL))
