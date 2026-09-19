"""AL: alert rules in plain words, evaluated against the hub's cached state once a second (TERMINAL.md 11).

    BTC below 75000            fee under 3                 block from Foundry
    XAU above 4500             mempool over 120            block
    odds on 80000 to 82000 at 16:00 above 20%              difficulty
    MSTR files 8-K

A price, fee, mempool or odds rule fires once when its condition becomes true and re-arms when it stops being true.
A block, difficulty or filing rule fires on each event. Evaluation reads only what is already in memory: it never
makes a request. Rules live in ~/.config/glimpse/alerts.json.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    from .hub import Hub

ABOVE, BELOW = ("above", "over", ">", "gt"), ("below", "under", "<", "lt")


@dataclass
class Rule:
    text: str
    kind: str                   # price | fee | mempool | block | difficulty | filing | odds | news
    subject: str = ""           # ticker, pool name, form type
    op: str = ""                # above | below
    level: float = 0.0
    lo: float = 0.0             # odds: the range
    hi: float = 0.0
    when: str = ""              # odds: HH:MM UTC of the close
    armed: bool = True
    fired_at: float = 0.0
    last: str = ""              # the last value it saw, for the page


def parse(text: str) -> Rule:
    """Words to a rule. Raises ValueError with a sentence the page can show."""
    t = " ".join(text.strip().split())
    low = t.lower()
    num = r"([0-9][0-9,_.]*)\s*(k|m)?"

    def value(m: re.Match, g: int = 1) -> float:
        v = float(m.group(g).replace(",", "").replace("_", ""))
        return v * {"k": 1e3, "m": 1e6, None: 1}[m.group(g + 1)]

    def op_of(word: str) -> str:
        return "above" if word in ABOVE else "below"

    ops = "|".join(re.escape(w) for w in ABOVE + BELOW)
    if m := re.match(rf"^odds (?:on )?{num} (?:to|-|–) {num} (?:at (?:the )?)?(\d{{1,2}}:\d{{2}})(?: close)? ({ops}) ([0-9.]+)\s*%?$", low):
        return Rule(t, "odds", "BTC", op_of(m.group(6)), float(m.group(7)) / 100, value(m, 1), value(m, 3), m.group(5).zfill(5))
    if m := re.match(rf"^(?:next[- ]block )?fees? ({ops}) {num}(?: sat/vb)?$", low):
        return Rule(t, "fee", "", op_of(m.group(1)), value(m, 2))
    if m := re.match(rf"^mempool(?: size)? ({ops}) {num}(?: mvb)?$", low):
        return Rule(t, "mempool", "", op_of(m.group(1)), value(m, 2))
    if m := re.match(r"^(?:a )?block(?: (?:from|by) (.+))?$", low):
        return Rule(t, "block", (m.group(1) or "").strip())
    if low in ("difficulty", "retarget", "difficulty adjustment"):
        return Rule(t, "difficulty")
    if m := re.match(r"^([a-z0-9.]{1,10}) files?(?: an?)? ([a-z0-9-]+)$", low):
        return Rule(t, "filing", m.group(1).upper(), level=0, op=m.group(2).upper())
    if m := re.match(r"^news (.+)$", low):
        return Rule(t, "news", m.group(1))
    if m := re.match(rf"^([a-z0-9.]{{1,10}}) ({ops}) \$?{num}$", low):
        return Rule(t, "price", m.group(1).upper(), op_of(m.group(2)), value(m, 3))
    raise ValueError("Try: BTC below 75000 · fee under 3 · mempool over 120 · block from Foundry · MSTR files 8-K · "
                     "odds on 80000 to 82000 at 16:00 above 20%")


def _hit(op: str, v: float, level: float) -> bool:
    return v > level if op == "above" else v < level


class Engine:
    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self._seen_events = time.time()                 # only what happens from now on
        self.load()

    def _path(self):
        return config.config_dir() / "alerts.json"

    def load(self) -> None:
        try:
            self.rules = [Rule(**r) for r in json.loads(self._path().read_text())]
        except (OSError, ValueError, TypeError):
            self.rules = []

    def save(self) -> None:
        try:
            config.config_dir().mkdir(parents=True, exist_ok=True)
            self._path().write_text(json.dumps([asdict(r) for r in self.rules], indent=1))
        except OSError:
            pass

    def add(self, text: str) -> Rule:
        rule = parse(text)
        self.rules.append(rule)
        self.save()
        return rule

    def remove(self, i: int) -> None:
        if 0 <= i < len(self.rules):
            del self.rules[i]
            self.save()

    def evaluate(self, hub: Hub, now: float | None = None) -> list[str]:
        """The messages to show for rules that fired on this tick. Reads cached state only."""
        now = now or time.time()
        out: list[str] = []
        events = [e for e in hub.events if e["at"] > self._seen_events]
        if events:
            self._seen_events = max(e["at"] for e in events)
        for r in self.rules:
            msg = self._check(r, hub, events, now)
            if msg:
                r.fired_at = now
                out.append(msg)
                hub.event("alert", text=msg)
        if out:
            self.save()
        return out

    def _level(self, r: Rule, value: float, shown: str, what: str) -> str:
        r.last = shown
        hit = _hit(r.op, value, r.level)
        if hit and r.armed:
            r.armed = False
            return f"ALERT  {what}"
        if not hit:
            r.armed = True
        return ""

    def _check(self, r: Rule, hub: Hub, events: list[dict[str, Any]], now: float) -> str:
        c = hub.chain
        if r.kind == "price":
            q = hub.quotes.get(r.subject)
            if not q:
                hub.watch.add(r.subject)                # the quote loop will fetch it
                r.last = "waiting for a quote"
                return ""
            return self._level(r, q.price, f"{q.price:,.2f}", f"{r.subject} is {r.op} {r.level:,.0f}: {q.price:,.2f} ({q.prov.source})")
        if r.kind == "fee" and c.fastest_fee:
            return self._level(r, c.fastest_fee, f"{c.fastest_fee:g} sat/vB",
                               f"the next-block fee is {r.op} {r.level:g}: {c.fastest_fee:g} sat/vB")
        if r.kind == "mempool" and c.mempool_vsize:
            mvb = c.mempool_vsize / 1e6
            return self._level(r, mvb, f"{mvb:,.0f} MvB", f"the mempool is {r.op} {r.level:g} MvB: {mvb:,.0f} MvB")
        if r.kind in ("block", "difficulty"):
            for e in events:
                if e.get("kind") != "block":
                    continue
                r.last = f"#{e.get('height', 0):,} {e.get('pool', '')}"
                if r.kind == "difficulty" and int(e.get("height", 1)) % 2016 == 0:
                    return f"ALERT  difficulty retargeted at block #{e['height']:,}"
                if r.kind == "block" and (not r.subject or r.subject in str(e.get("pool", "")).lower()):
                    return f"ALERT  block #{e.get('height', 0):,} mined by {e.get('pool') or 'an unknown pool'}"
            return ""
        if r.kind == "filing":
            for e in events:
                if (e.get("kind") == "filing" and str(e.get("ticker", "")).upper() == r.subject
                        and str(e.get("form", "")).upper().startswith(r.op)):
                    return f"ALERT  {r.subject} filed a {e.get('form')}: {str(e.get('title', ''))[:60]}"
            r.last = r.last or "watching filings (needs the SEC contact, and a company page or TOP open)"
            return ""
        if r.kind == "odds":
            return self._odds(r, hub, now)
        if r.kind == "news":
            r.last = "needs the Jev news hub, which is not built in this repository"
        return ""

    def _odds(self, r: Rule, hub: Hub, now: float) -> str:
        from ..funcs.options import distribution
        views = [v for v in (hub.account().get("views") or []) if v.row.end_time_utc > now
                 and datetime.fromtimestamp(v.row.end_time_utc, UTC).strftime("%H:%M") == r.when]
        if not views or hub.account().get("asset") != "BTC":
            r.last = "waiting for Glimpse's BTC closes"
            return ""
        bins, probs = distribution(views[0])
        p = sum(pr for (lo, hi), pr in zip(bins, probs, strict=True) if lo >= r.lo - 1e-9 and hi <= r.hi + 1e-9)
        return self._level(r, p, f"{p:.1%}", f"odds on {r.lo:,.0f} to {r.hi:,.0f} at the {r.when} UTC close are {r.op} {r.level:.0%}: {p:.1%}")
