"""Funding: what each bot's book is worth over time, and changing a running bot's bankroll in one step.

Two things the runners never did. A bot's budget was typed once, when it was started, and changing it meant stopping the
bot; and nothing kept what the book was worth from one hour to the next, so "is this growing" had no answer after a
restart.

- `Funds` is a bot's account at one instant: the bankroll it sizes against, what it has deployed at cost, what that
  would fetch now, what it may still buy with, and its equity (the bankroll, plus what its sales and paper settlements
  have made, plus the open positions' gain or loss). Live, it also says how much of the wallet the bot could run on.
- `Equity` is that figure over time, a sample a minute per bot (and one for the account: the wallet's free balance plus
  its positions' value), kept in ~/.config/glimpse/bot-equity.json across sessions. Every change of bankroll is recorded
  beside it as a flow, so the gain shown is what trading made, not what was added.
- `parse` reads the change as it is typed (`+50k`, `+25%`, `x2`, `250k`, `max`), `apply` makes it on a running bot
  without stopping it, and `ladder` says, for a desk, what a larger bankroll would buy right now by the desk's own
  pictures and sizing: more capital pushes each range nearer the model's value, so every added sat earns less than the
  one before, and the ladder shows where that stops being worth it.

Nothing here sends an order: a bot with a larger bankroll buys on its own next pass, under every rule it already had.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import fmt
from . import policy as PL
from .auth import config_dir

SAMPLE_S = 60.0                 # an equity sample per bot this often
SAVE_S = 60.0                   # the file is written at most this often
MAX_POINTS = 2000               # per bot; past it the older half is thinned to every other sample
STEPS = (1.25, 1.5, 2.0)        # the bankrolls the ladder tries, as multiples of the one it has
ACCOUNT = "account"


def is_desk(r) -> bool:
    return hasattr(r, "allotments")


class Refused(ValueError):
    """What was typed cannot be done; the message says why."""


# ── a bot's account ─────────────────────────────────────────

@dataclass(frozen=True)
class Funds:
    bankroll: float                 # what it sizes against now
    deployed: float                 # open positions at cost
    value: float                    # what they would fetch sold back now
    realised: float                 # this session: sales over cost, and paper settlements
    free: float                     # what it may still buy with
    setting: float                  # the figure that is typed: a budget, or a live desk's cap on the wallet (0: the whole wallet)
    wallet: float | None = None     # live: the wallet's free balance
    ceiling: float | None = None    # live: the most this bot's bankroll can be with the wallet as it is
    equity: float = 0.0

    @property
    def used(self) -> float:
        return self.deployed / self.bankroll if self.bankroll > 0 else 0.0


def funds(r, value: float | None = None, wallet: float | None = None, carried: float = 0.0) -> Funds | None:
    """The runner's account now; None for one with no bankroll (the Profit Taker only sells). `value` is what its open
    positions would fetch (their cost when unknown), `wallet` the account's free balance, `carried` what earlier
    sessions realised (see `Equity.carry`)."""
    if not getattr(r, "buys", True):
        return None
    cost = float(r.open_cost)
    value = cost if value is None else value
    if is_desk(r):
        realised = r.gain_sats + (0.0 if r.live else r.settled_sats)
        free = max(r.deployable - cost, 0.0)
        if r.live:                              # the wallet already holds what was realised
            return Funds(r.bankroll, cost, value, realised, free, r.budget, r.balance, r.balance + cost, r.bankroll + value - cost)
        return Funds(r.bankroll, cost, value, realised, free, r.budget, equity=r.budget + carried + realised + value - cost)
    budget = float(r.cfg.bankroll_sats)
    committed = float(r.committed)
    equity = budget + carried + r.gain_sats + value - cost
    if r.live and wallet is not None:
        return Funds(budget, cost, value, r.gain_sats, max(budget - committed, 0.0), budget, wallet, wallet + max(committed, 0.0), equity)
    return Funds(budget, cost, value, r.gain_sats, max(budget - committed, 0.0), budget, equity=equity)


# ── changing it ─────────────────────────────────────────────

def _amount(s: str) -> float:
    mult = 1.0
    if s.endswith("k"):
        s, mult = s[:-1], 1e3
    elif s.endswith("m"):
        s, mult = s[:-1], 1e6
    return float(s) * mult


def parse(text: str, f: Funds, live: bool, desk: bool) -> float:
    """The new setting, from what was typed: `+50k` and `-20k` add and take sats, `+25%` and `-10%` a share of the
    bankroll, `x2` multiplies it, `250k` or `1.5m` sets it, `max` is everything the wallet allows (for a live desk:
    no cap, the whole wallet; on paper there is no wallet to take the most of). Raises `Refused` with a sentence."""
    s = text.strip().lower().replace(",", "").replace("₿", "").replace(" ", "")
    s = s.removesuffix("sats").removesuffix("sat")
    if not s:
        raise Refused("Nothing typed: the bankroll stays as it is.")
    whole = live and desk and f.setting <= 0
    if s in ("max", "all", "wallet"):
        if not live:
            raise Refused("On paper there is no wallet: type an amount, like 250k or x2.")
        return 0.0 if desk else float(f.ceiling or f.bankroll)
    if (s[0] in "+-x×*" or s.endswith(("x", "×", "%"))) and whole:
        raise Refused(f"It already runs on the whole wallet ({fmt.sats(f.bankroll)}): deposit to the wallet to grow it, "
                      "or type an amount to cap it.")
    if s.endswith("%") and s[0] not in "+-":
        raise Refused("A share needs a sign: +25% adds a quarter, -10% takes a tenth.")
    try:
        if s[0] in "x×*" or s.endswith(("x", "×")):
            new = f.bankroll * float(s.strip("x×*"))
        elif s.endswith("%"):
            new = f.bankroll * (1 + float(s[:-1]) / 100)
        elif s[0] in "+-":
            new = f.bankroll + _amount(s)
        else:
            new = _amount(s)
    except ValueError:
        raise Refused("Type +50k, +25%, x2, an amount like 250k, or max.") from None
    if new != new or new < 10:
        raise Refused("A bankroll is at least 10 sats.")
    if live and not desk and f.ceiling is not None and new > f.ceiling + 0.5:
        raise Refused(f"The wallet holds {fmt.sats(f.wallet or 0)} free: with what it has deployed, the most this bot can "
                         f"run on is {fmt.sats(f.ceiling)}. Type max for that.")
    return float(new)


def effective(r, setting: float, f: Funds) -> float:
    """The bankroll a setting gives: a live desk's is the wallet, capped by it."""
    if is_desk(r) and r.live:
        return min(f.ceiling or 0.0, setting) if setting > 0 else float(f.ceiling or 0.0)
    return setting


def apply(r, setting: float) -> None:
    """Give a runner its new bankroll, running or not. A desk re-plans every close at once; a runner's spend caps follow
    its budget as they did when it was started (bots.toml's hard ceilings still hold)."""
    if is_desk(r):
        r.budget = float(setting)
        r.say(f"bankroll {'is the whole wallet' if r.live and setting <= 0 else 'set to ' + fmt.sats(r.bankroll)}"
              + (f" (the wallet, capped at {fmt.sats(setting)})" if r.live and setting > 0 else ""))
        if r.running:
            r._mark_all()
        return
    from .bots import BotConfig

    hard = BotConfig.load()
    c = r.cfg
    r.cfg = BotConfig(**{**asdict(c), "bankroll_sats": float(setting),
                         "max_per_cycle_sats": min(hard.max_per_cycle_sats, setting * c.cycle_share),
                         "max_per_hour_sats": min(hard.max_per_hour_sats, setting * c.hour_share)})
    r.say(f"budget set to {fmt.sats(setting)}")


# ── what more would buy ─────────────────────────────────────

@dataclass(frozen=True)
class Rung:
    bankroll: float
    cost: float                     # what the desk would buy right now on this bankroll
    worth: float                    # what that is worth by its pictures, after the settlement fee
    closes: int                     # closes it would buy on


def capacity(d, bankroll: float, now: float | None = None) -> Rung:
    """What the desk would buy this instant on `bankroll`: every tradable close it has a picture of, sized exactly as a
    tick sizes it (the close's share less what it holds there, the pool, the Kelly fraction, the edge floor, the
    dead-band), against the books as they stand. Nothing is sent or recorded. The hourly fuse is left out: it limits how
    fast a bankroll is put to work, not how much of it."""
    from .desk import Plan
    from .kelly import Sizing, kelly_buys

    now = time.time() if now is None else now
    have = d.bankroll
    if have <= 0 or bankroll <= 0 or d.watch is None:
        return Rung(bankroll, 0.0, 0.0, 0)
    scale = bankroll / have
    allot = d.allotments(now)
    pool = bankroll * (1.0 - d.cfg.reserve) - d.open_cost
    sizing = Sizing(kelly=d.cfg.kelly, min_edge=d.cfg.min_edge)
    cost = worth = 0.0
    closes = 0
    for b in d.batches:
        for book in d.tradable(b, now):
            pic = d.pictures.get(book.topic_id)
            if pic is None or pool < 1:
                continue
            held = d.ledger.legs(d.key, d.mode, book.topic_id)
            held_cost = sum(c for _, c in held.values())
            free = min(allot.get(book.topic_id, 0.0) * scale - held_cost, pool)
            if free < 1:
                continue
            paper = d._paper_book(book)
            try:
                legs = kelly_buys(paper, pic.probs, free, held, sizing)
            except (ValueError, FloatingPointError, IndexError):
                continue
            if not legs or d._too_small(Plan(paper, pic.probs, buys=legs, free=free), held_cost):
                continue
            spent = sum(x.cost_sats for x in legs)
            cost += spent
            pool -= spent
            worth += sum(pic.probs[x.index] * PL.NET * x.contracts for x in legs)
            closes += 1
    return Rung(bankroll, cost, worth, closes)


def ladder(d, bankrolls: list[float]) -> list[Rung]:
    """`capacity` at the desk's own bankroll, then at each of `bankrolls`. Run it off the event loop, with `reading`."""
    now = time.time()
    return [capacity(d, x, now) for x in [d.bankroll, *bankrolls]]


_READER = None


async def reading(fn, *args):
    """fn(*args) in a thread of its own: not the bots' pool, where it would hold up a desk's planning, and not asyncio's,
    where it would wait behind whatever the screens have queued."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    global _READER
    if _READER is None:
        _READER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="funding")
    return await asyncio.get_running_loop().run_in_executor(_READER, fn, *args)


def rung_line(base: Rung, r: Rung, label: str) -> str:
    """`+50%  ₿150,000   buys ₿12,400 more on 31 closes   expects +₿740 (6.0% on the sats added)`."""
    more, gain = r.cost - base.cost, (r.worth - r.cost) - (base.worth - base.cost)
    if more < 1:
        return f"  {label:<5} {fmt.sats(r.bankroll):>12}   buys nothing more right now: nothing else is far enough under value"
    return (f"  {label:<5} {fmt.sats(r.bankroll):>12}   buys {fmt.sats(more)} more ({r.closes} closes)   expects "
            f"{fmt.sats(gain, signed=True)} ({gain / more:+.1%} on the sats added)")


def briefing(r, f: Funds, rungs: list[Rung] | None = None) -> str:
    """The dialog's text: the account as it stands, what more would buy, and what may be typed."""
    desk = is_desk(r)
    word = "Bankroll" if desk else "Budget"
    lines = [f"{word} {fmt.sats(f.bankroll)}"
             + (" (the whole wallet)" if desk and r.live and f.setting <= 0 else f" (the wallet, capped at {fmt.sats(f.setting)})"
                if desk and r.live else "")
             + f" · deployed {fmt.sats(f.deployed)} ({f.used:.0%}) · free to buy {fmt.sats(f.free)} · equity {fmt.sats(f.equity)}"]
    if r.live and f.wallet is not None:
        lines.append(f"Wallet: {fmt.sats(f.wallet)} free. With what it has deployed, this bot can run on up to {fmt.sats(f.ceiling or 0)}.")
    if desk:
        lines.append(f"{r.cfg.reserve:.0%} of the bankroll is kept back; the rest is shared over the open closes, and each close is "
                     f"bought to {r.cfg.kelly:g} of its Kelly stake while a contract still returns {r.cfg.min_edge:.0%} after fees.")
        if rungs and len(rungs) > 1:
            base = rungs[0]
            lines.append("")
            lines.append(f"Right now it would buy {fmt.sats(base.cost)} more as it is. A larger bankroll, by its own pictures:"
                         if base.cost >= 1 else "As it is, it holds what it wants. A larger bankroll, by its own pictures:")
            for x in rungs[1:]:
                lines.append(rung_line(base, x, f"x{x.bankroll / base.bankroll:.3g}" if base.bankroll else ""))
            if all(x.cost - base.cost < 1 for x in rungs[1:]):
                lines.append(f"More would sit idle for now: it is short of edge, not of capital ({fmt.sats(f.deployed + base.cost)} of "
                             f"the {fmt.sats(f.bankroll * (1 - r.cfg.reserve))} it may deploy finds a use). A raise is spent only "
                             "when the books move away from its pictures.")
            else:
                lines.append("Each added sat buys a little nearer the model's value, so the return on what is added falls as the "
                             "bankroll grows: add while that return is worth having.")
        elif not r.running:
            lines.append("It is stopped, so there is no reading of what more would buy: the change takes effect when it starts.")
    else:
        n = max(int(getattr(r, "of", 0) or 0), 1)
        lines.append(f"Each of the {n} close{'' if n == 1 else 's'} in its round may hold {fmt.sats(f.bankroll / n)}; it buys only "
                     "what its forecast says is under value, so a larger budget is used only where there is edge to take.")
    lines.append("")
    lines.append("Type +50k or +25% to add, x2 to double, an amount like 250k to set it"
                 + (", max for the most the wallet allows" if r.live else "")
                 + ". A lower figure works too: the bot then stops buying until it is under it, and sells nothing because of it.")
    return "\n".join(lines)


# ── equity over time ────────────────────────────────────────

class Equity:
    """Each bot's equity, a sample a minute, and every change of its bankroll, kept across sessions.
    {key: {"points": [[t, equity, bankroll]], "flows": [[t, sats]], "carry": x, "real": y, "trades": n}}"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "bot-equity.json"
        try:
            self.data: dict = json.loads(self.path.read_text()) if self.path.is_file() else {}
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}
        self._saved_at = 0.0
        self._dirty = False

    def _of(self, key: str) -> dict:
        d = self.data.get(key)
        if not isinstance(d, dict):
            d = self.data[key] = {}
        d.setdefault("points", [])
        d.setdefault("flows", [])
        return d

    def carry(self, key: str, realised: float, trades: int) -> float:
        """What earlier sessions of this bot realised. A runner counts its sales' gains from zero each time it is built;
        when its trade count falls, a new one has taken over, and what the last one had made is carried forward."""
        d = self._of(key)
        if trades < int(d.get("trades", 0)):
            d["carry"] = float(d.get("carry", 0.0)) + float(d.get("real", 0.0))
            self._dirty = True
        if d.get("real") != realised or d.get("trades") != trades:
            d["real"], d["trades"] = float(realised), int(trades)
            self._dirty = True
        return float(d.get("carry", 0.0))

    def sample(self, key: str, now: float, equity: float, bankroll: float, force: bool = False) -> bool:
        pts = self._of(key)["points"]
        if pts and not force and now - pts[-1][0] < SAMPLE_S:
            return False
        pts.append([round(now), round(equity, 1), round(bankroll, 1)])
        if len(pts) > MAX_POINTS:
            half = len(pts) // 2
            pts[:half] = pts[:half:2]
        self._dirty = True
        return True

    def flow(self, key: str, now: float, sats: float) -> None:
        """Capital added to (or taken from) the bot's bankroll: not a gain."""
        if abs(sats) >= 0.5:
            self._of(key)["flows"].append([round(now), round(sats, 1)])
            self._dirty = True

    def points(self, key: str) -> list[list[float]]:
        d = self.data.get(key)
        return d.get("points", []) if isinstance(d, dict) else []

    def net(self, key: str, equity: float | None = None) -> tuple[float, float, float] | None:
        """(what trading has made since the first sample, the capital it was made on, when that sample was taken): the
        change in equity less every sat added since. None before there is a sample."""
        pts = self.points(key)
        if not pts:
            return None
        t0, e0 = pts[0][0], pts[0][1]
        added = sum(s for t, s in self.data[key].get("flows", []) if t >= t0)
        return (pts[-1][1] if equity is None else equity) - e0 - added, e0 + added, t0

    def save(self, now: float | None = None, force: bool = False) -> None:
        now = time.time() if now is None else now
        if not self._dirty or (not force and now - self._saved_at < SAVE_S):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
            self._saved_at, self._dirty = now, False
        except OSError:
            pass
