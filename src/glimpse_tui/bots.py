"""Local bots. A bot is a picture of the future; the runner owns every safety rule.

A bot is either a model from the zoo (`glimpse_tui.zoo`, the Forecast Lab's models running on public hourly
candles) or one function in a file of your own:

    def forecast(book, closes, hours) -> list[float]   # one probability per bin, sums to 1

Drop a file defining `forecast` into ~/.config/glimpse/bots/ and it appears in the terminal under MINE.

Each cycle the runner prices the nearest closes, buys the bins its picture says are underpriced (fractional Kelly,
never past the price the picture justifies, every order checked against the server's estimate first), and sells a
position it bought once the market pays more for it than the picture says it is worth. It never touches a position
it did not open: what it owns is recorded in a ledger on this machine. It stops at its budget and its spend caps.
Dry run by default; a dry run keeps a paper ledger so it behaves as the live bot would.

A front end that wants more than log lines sets `on_event` on the runner (or on a bot) and receives one dict per
step of the cycle: `cycle_start`, `closed`, `forecast`, `candidates`, `intent_buy`, `intent_sell`, `estimate`, `fill`,
`fill_unknown`, `cap_hit`, `error`. A subclass may override `picture` (how the probabilities are produced) and `veto`
(a last check on the legs `decide` chose) without touching any other rule. Nothing changes when neither is used.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import time
import tomllib
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import fmt
from . import pricing as P
from .api import AmbiguousTrade, ApiError, Book, Glimpse
from .auth import config_dir

Forecast = Callable[[Book, list[float], float], list[float]]
OnEvent = Callable[[dict], None]


@dataclass
class BotConfig:
    markets: int = 2                 # nearest-expiry markets traded per cycle
    interval_s: int = 300
    bankroll_sats: float = 20_000    # the bot's budget: Kelly sizes against it, and open cost never exceeds it
    kelly: float = 0.25
    min_edge: float = 0.10           # net ev/cost; the round trip costs ~4% in fees alone
    exit_edge: float = 0.05          # sell when the market pays this much more than the picture says a position is worth
    max_per_cycle_sats: float = 2_000
    max_per_hour_sats: float = 5_000
    stop_before_close_s: int = 60
    estimate_tolerance: float = 0.005

    @classmethod
    def load(cls, **override: float) -> BotConfig:
        f = config_dir() / "bots.toml"
        raw = tomllib.loads(f.read_text()) if f.is_file() else {}
        known = {x.name for x in fields(cls)}
        return cls(**{k: v for k, v in {**raw, **override}.items() if k in known})

    def with_budget(self, sats: float) -> BotConfig:
        """One number to deploy a bot: the caps follow the budget unless bots.toml sets them lower."""
        return BotConfig(**{**asdict(self), "bankroll_sats": sats, "max_per_cycle_sats": min(self.max_per_cycle_sats, sats / 4),
                            "max_per_hour_sats": min(self.max_per_hour_sats, sats / 2)})


# ── the bots ────────────────────────────────────────────────

@dataclass(frozen=True)
class Bot:
    id: str
    name: str
    family: str
    blurb: str
    description: str
    kind: str = "forecast"
    factors: tuple[str, ...] = ()
    model: object | None = None             # a zoo Model
    forecast: Forecast | None = None        # a user file's function
    error: str = ""
    on_event: OnEvent | None = None         # told about every picture this bot draws, when set

    def probs(self, book: Book, ctx) -> list[float]:
        t0 = time.perf_counter()
        if self.model is not None:
            out = [float(x) for x in self.model.fn(ctx)]
        else:
            closes = [float(x) for x in ctx.bars["close"].to_numpy()[-336:]]
            out = list(self.forecast(book, closes, ctx.hours))
        if self.on_event is not None:
            self.on_event({"type": "forecast", "bot": self.id, "topic_id": book.topic_id, "end": book.end_time_utc, "ts": time.time(),
                           "data": {"model": self.id, "n": len(out), "ms": round((time.perf_counter() - t0) * 1000)}})
        return out


def bots_dir() -> Path:
    return config_dir() / "bots"


def user_bots() -> list[Bot]:
    out: list[Bot] = []
    d = bots_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"glimpse_bot_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "forecast", None)
            if callable(fn):
                doc = (mod.__doc__ or "").strip()
                out.append(Bot(f.stem, f.stem, "Mine", (doc.splitlines() or [str(f)])[0][:70], doc or str(f), forecast=fn))
        except Exception as e:
            out.append(Bot(f.stem, f.stem, "Mine", f"failed to load: {e.__class__.__name__}", f"{f}: {e}", error=str(e) or e.__class__.__name__))
    return out


def discover() -> list[Bot]:
    """Every bot the terminal can run: the zoo, then your own files. Loads numpy, pandas and scipy on first use."""
    from . import zoo

    return [Bot(m.id, m.name, m.family, m.blurb, m.description, m.kind, m.factors, model=m) for m in zoo.catalog()] + user_bots()


# ── ledger ──────────────────────────────────────────────────

class Ledger:
    """What each bot has bought, on this machine. {bot: {mode: {topic: {"end", "asset", "legs": {option: [contracts, cost, lo, hi]}}}}}"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "bot-ledger.json"
        try:
            self.data: dict = json.loads(self.path.read_text()) if self.path.is_file() else {}
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
        except OSError:
            pass

    def topics(self, bot: str, mode: str) -> dict:
        return self.data.setdefault(bot, {}).setdefault(mode, {})

    def prune(self, now: float) -> dict[str, dict[str, dict[str, dict]]]:
        """A closed market settles on the server; there is nothing left for a bot to manage. Returns what was removed,
        {bot: {mode: {topic: entry}}}, so a caller can go and find out how those markets resolved."""
        removed: dict[str, dict[str, dict[str, dict]]] = {}
        for bot, modes in self.data.items():
            for mode, topics in modes.items():
                for tid in [t for t, v in topics.items() if v.get("end", 0) <= now]:
                    removed.setdefault(bot, {}).setdefault(mode, {})[tid] = topics.pop(tid)
        return removed

    def add(self, bot: str, mode: str, book: Book, asset: str, index: int, contracts: float, cost: float) -> None:
        t = self.topics(bot, mode).setdefault(str(book.topic_id), {"end": book.end_time_utc, "asset": asset, "legs": {}})
        lo, hi = book.bins[index]
        leg = t["legs"].setdefault(str(book.option_ids[index]), [0.0, 0.0, lo, hi])
        leg[0], leg[1] = round(leg[0] + contracts, 2), leg[1] + cost

    def remove(self, bot: str, mode: str, topic_id: int, option_id: int) -> None:
        t = self.topics(bot, mode).get(str(topic_id))
        if t:
            t["legs"].pop(str(option_id), None)
            if not t["legs"]:
                del self.topics(bot, mode)[str(topic_id)]

    def legs(self, bot: str, mode: str, topic_id: int) -> dict[int, tuple[float, float]]:
        t = self.topics(bot, mode).get(str(topic_id)) or {}
        return {int(o): (v[0], v[1]) for o, v in (t.get("legs") or {}).items()}

    def open_cost(self, bot: str, mode: str, topic_id: int | None = None) -> float:
        ts = self.topics(bot, mode)
        return sum(v[1] for tid, t in ts.items() if topic_id is None or tid == str(topic_id) for v in t["legs"].values())

    def positions(self, bot: str, mode: str) -> list[tuple[int, str, float, float, float, float]]:
        """(close, asset, low, high, contracts, cost), soonest first."""
        out = [(t["end"], t.get("asset", ""), v[2], v[3], v[0], v[1]) for t in self.topics(bot, mode).values() for v in t["legs"].values()]
        return sorted(out)


# ── decision ────────────────────────────────────────────────

@dataclass
class Leg:
    index: int
    contracts: float
    cost_sats: float


def decide(book: Book, probs: list[float], cfg: BotConfig, budget_sats: float, held_cost: float = 0.0) -> list[Leg]:
    """Buys on bins the market underprices net of both fees, fractional Kelly, truthful-capped. `held_cost` is what
    the bot already has in this market: Kelly's stake is a target, not a top-up to repeat every cycle."""
    q = book.shares[:]
    px = book.prices
    net = P.PAYOUT_SATS * (1 - P.FEE)
    cands = []
    for i, p in enumerate(probs):
        c1 = px[i] * (1 + P.FEE)
        ev = p * net - c1
        if c1 <= 0 or ev <= 0 or ev / c1 < cfg.min_edge:
            continue
        b = net / c1 - 1
        f = (b * p - (1 - p)) / b
        if f > 0:
            cands.append((i, f, ev / c1))
    total_f = sum(f for _, f, _ in cands)
    if total_f <= 0:
        return []
    spend = min(cfg.kelly * min(total_f, 1.0) * cfg.bankroll_sats - held_cost, budget_sats)
    if spend < 1:
        return []
    legs: list[Leg] = []
    for i, f, _ in sorted(cands, key=lambda c: -c[2]):
        stake = spend * f / total_f
        target = probs[i] * net / (1 + P.FEE)       # price at which the edge is gone
        lo, hi = 0.0, 5000.0
        for _ in range(40):
            mid = (lo + hi) / 2
            q2 = q[:]
            q2[i] += mid
            over_price = P.prices(q2, book.alpha)[i] > target
            over_stake = (1 + P.FEE) * (P.cost(q2, book.alpha) - P.cost(q, book.alpha)) > stake
            lo, hi = (lo, mid) if over_price or over_stake else (mid, hi)
        d = math.floor(lo * 100) / 100
        if d <= 0:
            continue
        q2 = q[:]
        q2[i] += d
        c = (1 + P.FEE) * (P.cost(q2, book.alpha) - P.cost(q, book.alpha))
        if c < 0.01:
            continue
        legs.append(Leg(i, d, c))
        q = q2
    return legs


def exits(book: Book, probs: list[float], held: dict[int, tuple[float, float]], cfg: BotConfig) -> list[tuple[int, float, float]]:
    """(bin index, contracts, proceeds) for positions the market now pays more for than the picture says they are worth."""
    out = []
    net = P.PAYOUT_SATS * (1 - P.FEE)
    index = {o: i for i, o in enumerate(book.option_ids)}
    for option_id, (contracts, _) in held.items():
        i = index.get(option_id)
        if i is None or contracts <= 0:
            continue
        proceeds = P.sell_proceeds(book.shares, book.alpha, i, min(contracts, book.shares[i]))
        if proceeds >= 1 and proceeds > probs[i] * contracts * net * (1 + cfg.exit_edge):
            out.append((i, contracts, proceeds))
    return out


# ── runner ──────────────────────────────────────────────────

@dataclass
class Runner:
    bot: Bot
    api: Glimpse
    batch_id: str
    feed: object                                # zoo.data.Feed for the series' asset
    live: bool = False
    cfg: BotConfig = field(default_factory=BotConfig.load)
    ledger: Ledger = field(default_factory=Ledger)
    series: str = ""
    log: deque[str] = field(default_factory=lambda: deque(maxlen=400))
    spent: deque[tuple[float, float]] = field(default_factory=deque)   # (time, sats)
    cycles: int = 0
    trades: int = 0
    paid_sats: float = 0.0                      # this session
    sold_sats: float = 0.0
    stance: str = ""
    started_at: float = 0.0
    on_trade: Callable[[], None] | None = None
    on_log: Callable[[str], None] | None = None
    on_event: OnEvent | None = None             # one dict per step of the cycle, for a front end; see the module doc
    _task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return self.bot.name

    @property
    def mode(self) -> str:
        return "live" if self.live else "paper"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def open_cost(self) -> float:
        return self.ledger.open_cost(self.bot.id, self.mode)

    def say(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S', time.gmtime())}  {msg}"
        self.log.append(line)
        if self.on_log:
            self.on_log(line)

    def emit(self, type: str, book: Book | None = None, **data) -> None:
        """A structured event for `on_event`: {type, bot, mode, cycle, ts, topic_id?, end?, data}. Nothing when unset."""
        if self.on_event is None:
            return
        ev: dict = {"type": type, "bot": self.bot.id, "mode": self.mode, "cycle": self.cycles, "ts": time.time()}
        if book is not None:
            ev["topic_id"], ev["end"] = book.topic_id, book.end_time_utc
        ev["data"] = data
        self.on_event(ev)

    def leg_info(self, book: Book, leg: Leg) -> dict:
        lo, hi = book.bins[leg.index]
        return {"index": leg.index, "option_id": book.option_ids[leg.index], "lo": lo, "hi": hi,
                "contracts": leg.contracts, "cost_sats": leg.cost_sats}

    async def picture(self, book: Book, ctx) -> list[float]:
        """The probabilities the runner trades on: the bot's own, unless a subclass bends them."""
        return await asyncio.to_thread(self.bot.probs, book, ctx)

    def veto(self, book: Book, legs: list[Leg]) -> list[Leg]:
        """A last look at the legs `decide` chose. Returns them unchanged; a subclass may drop some or all."""
        return legs

    def start(self) -> None:
        if not self.running:
            self.started_at = time.time()
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self.say("stopped")

    async def wait(self) -> None:
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def spent_last_hour(self) -> float:
        cutoff = time.time() - 3600
        while self.spent and self.spent[0][0] < cutoff:
            self.spent.popleft()
        return sum(s for _, s in self.spent)

    async def _loop(self) -> None:
        self.say(f"started  {'LIVE' if self.live else 'dry run'}  budget {fmt.sats(self.cfg.bankroll_sats)}  every {self.cfg.interval_s}s")
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:      # one bad cycle must not stop the bot
                self.say(f"cycle failed: {e}")
            await asyncio.sleep(self.cfg.interval_s)

    async def cycle(self) -> None:
        self.cycles += 1
        self.emit("cycle_start", asof=time.time())
        await self.feed.refresh()
        if not self.feed.ready:
            self.say("no price data yet, waiting")
            self.emit("error", where="feed", message="no price data yet")
            return
        now = time.time()
        for tid, entry in self.ledger.prune(now).get(self.bot.id, {}).get(self.mode, {}).items():
            self.emit("closed", topic_id=int(tid), end=entry.get("end"), legs=entry.get("legs", {}))
        rows = await self.api.markets(self.batch_id, limit=max(self.cfg.markets, 24))
        budget = self.cfg.max_per_cycle_sats
        for row in rows[: self.cfg.markets]:
            left = row.end_time_utc - time.time()
            if left < self.cfg.stop_before_close_s:
                continue
            book = await self.api.book(row.topic_id)
            if not book.is_live or not book.bins:
                continue
            tag = fmt.question(self.feed.asset, book.end_time_utc)[:-4]
            try:
                ctx = self.feed.ctx(book.bins, book.end_time_utc)
                probs = await self.picture(book, ctx)
            except Exception as e:
                self.say(f"{tag}  no picture: {e}")
                self.emit("error", book, where="picture", message=str(e) or e.__class__.__name__)
                continue
            if len(probs) != len(book.bins) or abs(sum(probs) - 1) > 1e-6:
                self.say(f"{self.name}: forecast must return {len(book.bins)} probabilities summing to 1")
                self.emit("error", book, where="picture", message=f"forecast must return {len(book.bins)} probabilities summing to 1")
                return
            shape: dict = {"model": self.bot.id, "probs": [float(p) for p in probs]}
            if self.bot.model is not None:
                from .zoo.core import describe

                d = describe(ctx, probs)
                self.stance = f"{d['view']}  median {fmt.price(d['median'])}  80% {fmt.kprice(d['low'])}–{fmt.kprice(d['high'])}"
                shape.update(median=d["median"], band80=[d["low"], d["high"]], view=d["view"], lean=d["lean"], width=d["width"])
            self.emit("forecast", book, **shape)
            held = self.ledger.legs(self.bot.id, self.mode, book.topic_id)
            for i, contracts, proceeds in exits(book, probs, held, self.cfg):
                await self._exit(book, i, contracts, proceeds, tag)
            room = min(budget, self.cfg.max_per_hour_sats - self.spent_last_hour(), self.cfg.bankroll_sats - self.open_cost)
            if room < 1:
                self.say(f"{tag}  budget or spend cap reached, holding")
                self.emit("cap_hit", book, cap="room", limit=self.cfg.bankroll_sats, value=self.open_cost, action="hold")
                continue
            held_cost = self.ledger.open_cost(self.bot.id, self.mode, book.topic_id)
            legs = await asyncio.to_thread(decide, book, probs, self.cfg, room, held_cost)
            self.emit("candidates", book, legs=[self.leg_info(book, x) for x in legs], held_sats=held_cost, room_sats=room)
            legs = self.veto(book, legs)
            total = sum(x.cost_sats for x in legs)
            if not legs or total < 1:
                self.say(f"{tag}  no edge" + (f"  (holding {fmt.sats(held_cost)})" if held_cost else ""))
                continue
            budget -= await self._enter(book, legs, total, tag)
        self.ledger.save()

    async def _exit(self, book: Book, i: int, contracts: float, proceeds: float, tag: str) -> None:
        lo, hi = book.bins[i]
        leg = {"index": i, "option_id": book.option_ids[i], "lo": lo, "hi": hi, "contracts": contracts, "proceeds_sats": proceeds}
        self.say(f"{tag}  {'SELL' if self.live else 'would sell'} {fmt.span(lo, hi)}  {fmt.contracts(contracts)} for ₿{proceeds:,.0f}")
        self.emit("intent_sell", book, **leg)
        if self.live:
            try:
                await self.api.sell(book.topic_id, book.option_ids[i], contracts)
            except AmbiguousTrade as e:
                self.say(f"{tag}  {e}")
                self.emit("fill_unknown", book, side="sell", legs=[leg], reason=str(e))
                self.stop()                 # unknown fill state: a human should look before more orders go out
                return
            except ApiError as e:
                self.say(f"{tag}  {e}")
                self.emit("error", book, where="broker", message=str(e))
                return
            if self.on_trade:
                self.on_trade()
        self.ledger.remove(self.bot.id, self.mode, book.topic_id, book.option_ids[i])
        self.sold_sats += proceeds
        self.trades += 1
        self.emit("fill", book, side="sell", paper=not self.live, **leg)

    async def _enter(self, book: Book, legs: list[Leg], total: float, tag: str) -> float:
        order = [(book.option_ids[x.index], x.contracts) for x in legs]
        info = [self.leg_info(book, x) for x in legs]
        self.emit("intent_buy", book, legs=info, total_sats=total)
        raw, fee = await self.api.estimate_legs(book.topic_id, order)
        charge = raw + max(fee, P.MIN_FEE_SATS)
        tolerance = self.cfg.estimate_tolerance * total + 5
        ok = abs(charge - total) <= tolerance
        self.emit("estimate", book, side="buy", local_sats=total, server_sats=charge, ok=ok, tolerance_sats=tolerance)
        if not ok:
            self.say(f"{tag}  book moved (local {total:,.0f} vs server {charge:,.0f}), skipped")
            return 0.0
        top = max(legs, key=lambda x: x.cost_sats)
        lo, hi = book.bins[top.index]
        self.say(f"{tag}  {'BUY' if self.live else 'would buy'} {len(legs)} bins  ₿{charge:,.0f}  top {fmt.span(lo, hi)}")
        paid, fee_paid, trade_id = charge, max(fee, P.MIN_FEE_SATS), ""
        if self.live:
            try:
                fill = await self.api.buy_legs(book.topic_id, order)
            except AmbiguousTrade as e:
                self.say(f"{tag}  {e}")
                self.emit("fill_unknown", book, side="buy", legs=info, reason=str(e))
                self.stop()             # unknown fill state: a human should look before more orders go out
                return 0.0
            except ApiError as e:
                self.say(f"{tag}  {e}")
                self.emit("error", book, where="broker", message=str(e))
                return 0.0
            if fill.error:
                self.say(f"{tag}  rejected: {fill.error}")
                self.emit("error", book, where="broker", message=f"rejected: {fill.error}")
                return 0.0
            paid, fee_paid, trade_id = fill.cost_sats + fill.fee_sats, fill.fee_sats, fill.trade_id
            self.say(f"{tag}  filled ₿{paid:,.0f}")
        for x, leg in zip(legs, info, strict=True):
            leg["cost_sats"] = paid * x.cost_sats / total
            self.ledger.add(self.bot.id, self.mode, book, self.feed.asset, x.index, x.contracts, leg["cost_sats"])
        self.spent.append((time.time(), paid))
        self.paid_sats += paid
        self.trades += 1
        self.emit("fill", book, side="buy", trade_id=trade_id, legs=info, paid_sats=paid, fee_sats=fee_paid, paper=not self.live)
        if self.live and self.on_trade:
            self.on_trade()
        return paid
