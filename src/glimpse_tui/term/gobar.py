"""The GO bar (TERMINAL.md 3.1): `<TICKER> [<CLASS>] <FUNCTION> <GO>`, where Enter is GO.

This module is the grammar and the line editor's state. It parses, completes and remembers; it never runs
anything. The app turns a `Command` into a pane, so everything here is testable without a screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config, registry
from .instruments import Book, Instrument, looks_like

HISTORY_MAX = 500
SHELL = ("LP", "QUIT", "Q")                            # words the shell runs itself, beside the registry's functions


@dataclass(frozen=True)
class Command:
    func: str                                   # a function code, or LP / HELP / FIND / QUIT
    securities: tuple[Instrument, ...] = ()
    args: tuple[str, ...] = ()
    raw: str = ""

    @property
    def security(self) -> Instrument | None:
        return self.securities[0] if self.securities else None

    def text(self) -> str:
        """The canonical spelling, shown before ASK runs it and written to a saved launchpad."""
        return " ".join([self.func, *(s.ticker for s in self.securities), *self.args])


@dataclass(frozen=True)
class Suggestion:
    text: str                   # what Tab writes into the bar
    label: str                  # the left column
    detail: str                 # name · CLASS, or "function · summary"
    hints: tuple[str, ...] = () # function codes that suit it, on the right
    kind: str = "function"      # instrument | function | series | layout | history


def parse(text: str, book: Book, current: Instrument | None = None) -> Command | None:
    """None when the input parses as nothing: the caller hands it to ASK."""
    raw = text.strip()
    tokens = raw.split()
    if not tokens:
        return None
    head = tokens[0].upper()
    if head in ("Q", "QUIT", "Q!", "QA", "WQ", "X"):
        return Command("QUIT", raw=raw)
    if head == "LP":
        return Command("LP", args=tuple(tokens[1:]), raw=raw)
    if head == "H":
        head = "HELP"
    if fn := registry.get(head):
        rest, secs = tokens[1:], []
        if fn.takes:
            while rest and (ins := _instrument(rest[0], fn, book)) and (fn.many or not secs):
                secs.append(ins)
                rest = rest[1:]
                if rest and rest[0].upper() in registry.CLASSES:
                    rest = rest[1:]
            if not secs and current and current.cls in fn.takes and not rest:
                secs = [current]                                        # a bare function runs against the pane's security
        return Command(fn.code, tuple(secs), tuple(rest), raw)
    if ins := book.get(head):
        rest = tokens[1:]
        if rest and rest[0].upper() in registry.CLASSES:
            rest = rest[1:]                                             # the yellow key: BTC CRYPTO GP
        if rest and (fn := registry.get(rest[0])):
            return Command(fn.code, (ins,), tuple(rest[1:]), raw)
        if not rest:
            fn = registry.default_for(ins.cls) if ins.ticker != "BTC" else registry.get("BTC")
            return Command(fn.code if fn else "GP", (ins,), (), raw)
        return None
    if len(tokens) == 1:
        kind = looks_like(tokens[0])
        if kind:
            return Command({"txid": "TX", "address": "ADDR", "height": "BLK"}[kind], args=(tokens[0].replace(",", ""),), raw=raw)
    return None


def _instrument(token: str, fn: registry.Function, book: Book) -> Instrument | None:
    if registry.get(token) and not book.by_ticker.get(token.upper()):
        return None                                                     # a function code, not a company called HELP
    ins = book.get(token)
    if ins and ins.cls in fn.takes:
        return ins
    if "SERIES" in fn.takes and not ins and looks_like(token) == "" and ("_" in token or token.islower()):
        return Instrument(token, token, "SERIES", quote="", sources=(f"bitview:{token}",))
    return None


def complete(text: str, book: Book, layouts: list[str], series: list[tuple[str, str]] | None = None, limit: int = 8) -> list[Suggestion]:
    """Suggestions for the word being typed: instruments, SEC filers, functions, layouts, and Bitview series names
    that a recent FLDS or GP search has cached (the bar never waits on the network)."""
    tokens = text.split()
    if not tokens or text.endswith(" "):
        if tokens and (ins := book.get(tokens[0])) and len(tokens) == 1:
            return [Suggestion(f"{ins.ticker} {f.code}", f.code, f"function · {f.summary}") for f in registry.for_class(ins.cls)][:limit]
        return []
    word, before = tokens[-1].upper(), " ".join(tokens[:-1])
    join = (before + " ") if before else ""
    out: list[Suggestion] = []
    if before.upper() == "LP" or before.upper() == "LP SAVE":
        return [Suggestion(f"{join}{n}", n, "launchpad", kind="layout") for n in layouts if n.upper().startswith(word)][:limit]
    if before.upper() in ("HELP", "H"):
        return [Suggestion(f"{join}{f.code}", f.code, f"function · {f.summary}") for f in registry.every() if f.code.startswith(word)][:limit]
    first = len(tokens) == 1
    head_fn = registry.get(tokens[0]) if not first else None
    if first or (head_fn and head_fn.takes):
        for ins in book.search(word, limit):
            if head_fn and ins.cls not in head_fn.takes:
                continue
            hints = tuple(f.code for f in registry.for_class(ins.cls))[:6]
            where = f" · {ins.note}" if ins.cls == "EQUITY" and ins.note and len(ins.note) < 12 else ""
            out.append(Suggestion(f"{join}{ins.ticker}", ins.ticker, f"{ins.name} · {ins.cls}{where}", hints, "instrument"))
    if first or book.get(tokens[0]):
        fns = [f for f in registry.every() if f.code.startswith(word)]
        if not first and (sec := book.get(tokens[0])):
            fns = [f for f in fns if sec.cls in f.takes]
        out = out[:max(limit - min(len(fns), 4), 1)]                # instruments never crowd the functions out
        out += [Suggestion(f"{join}{f.code}", f.code, f"function · {f.summary}") for f in fns]
    if first:
        out += [Suggestion(f"LP {n}", f"LP {n}", "launchpad", kind="layout") for n in layouts if n.upper().startswith(word)]
    if series and head_fn and "SERIES" in head_fn.takes:
        out += [Suggestion(f"{join}{name}", name, f"series · {desc}", kind="series") for name, desc in series if word.lower() in name][:limit]
    exact = [s for s in out if s.label == word]
    return (exact + [s for s in out if s.label != word])[:limit]


def find(text: str, book: Book, layouts: list[str], limit: int = 60) -> list[Suggestion]:
    """FIND: everything the autocomplete searches, matched anywhere in the code, name or summary."""
    q = text.strip().upper()
    out = [Suggestion(f.code, f.code, f"function · {f.name}: {f.summary}") for f in registry.every()
           if not q or q in f.code or q in f.name.upper() or q in f.summary.upper()]
    out += [Suggestion(i.ticker, i.ticker, f"{i.name} · {i.cls}", tuple(f.code for f in registry.for_class(i.cls))[:5], "instrument")
            for i in book.search(q, limit)] if q else []
    out += [Suggestion(f"LP {n}", f"LP {n}", "launchpad", kind="layout") for n in layouts if q in n.upper()]
    return out[:limit]


@dataclass
class Line:
    """The bar's editing state: text, caret, suggestions and the history walk."""
    text: str = ""
    caret: int = 0
    history: list[str] = field(default_factory=list)
    h_at: int | None = None         # index into history while walking with ↑ ↓
    draft: str = ""                 # what was typed before the walk began
    picks: list[Suggestion] = field(default_factory=list)
    pick: int = -1                  # -1: nothing highlighted

    def load_history(self) -> None:
        try:
            self.history = [ln for ln in (config.config_dir() / "history").read_text().splitlines() if ln.strip()][-HISTORY_MAX:]
        except OSError:
            self.history = []

    def remember(self, entry: str) -> None:
        entry = entry.strip()
        if not entry or (self.history and self.history[-1] == entry):
            return
        self.history = (self.history + [entry])[-HISTORY_MAX:]
        try:
            config.config_dir().mkdir(parents=True, exist_ok=True)
            (config.config_dir() / "history").write_text("\n".join(self.history) + "\n")
        except OSError:
            pass

    def set(self, text: str) -> None:
        self.text, self.caret, self.pick = text, len(text), -1

    def clear(self) -> None:
        self.set("")
        self.h_at, self.draft, self.picks = None, "", []

    def key(self, k: str, ch: str | None) -> bool:
        """Edit the line. True when the key was an edit or a movement the bar owns (Enter and Esc belong to the app)."""
        if k in ("up", "down") and self.picks and (self.pick >= 0 or k == "down") and self.h_at is None:
            self.pick = max(-1, min(self.pick + (1 if k == "down" else -1), len(self.picks) - 1))
        elif k == "up":
            if self.history:
                if self.h_at is None:
                    self.draft, self.h_at = self.text, len(self.history)
                self.h_at = max(self.h_at - 1, 0)
                self.text = self.history[self.h_at]
                self.caret = len(self.text)
        elif k == "down":
            if self.h_at is not None:
                self.h_at += 1
                if self.h_at >= len(self.history):
                    self.h_at, self.text = None, self.draft
                else:
                    self.text = self.history[self.h_at]
                self.caret = len(self.text)
        elif k == "tab":
            if self.picks:
                self.set(self.picks[max(self.pick, 0)].text + " ")
        elif k == "left":
            self.caret = max(self.caret - 1, 0)
        elif k == "right":
            self.caret = min(self.caret + 1, len(self.text))
        elif k in ("home", "ctrl+a"):
            self.caret = 0
        elif k in ("end", "ctrl+e"):
            self.caret = len(self.text)
        elif k == "backspace":
            if self.caret:
                self.text, self.caret = self.text[:self.caret - 1] + self.text[self.caret:], self.caret - 1
        elif k == "delete":
            self.text = self.text[:self.caret] + self.text[self.caret + 1:]
        elif k == "ctrl+u":
            self.text, self.caret = self.text[self.caret:], 0
        elif k == "ctrl+w":
            head = self.text[:self.caret].rstrip()
            cut = head.rfind(" ") + 1
            self.text, self.caret = self.text[:cut] + self.text[self.caret:], cut
        elif ch and ch.isprintable() and len(ch) == 1:
            self.text, self.caret = self.text[:self.caret] + ch + self.text[self.caret:], self.caret + 1
        else:
            return False
        if k not in ("up", "down"):
            self.h_at, self.pick = None, -1
        return True

    def chosen(self) -> str:
        """What Enter runs: the highlighted suggestion if the arrows picked one, else the text as typed."""
        return self.picks[self.pick].text if 0 <= self.pick < len(self.picks) else self.text
