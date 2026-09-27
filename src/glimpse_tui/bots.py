"""Local bots. A bot is a picture of the future; the runner owns every safety rule.

A bot is either a model from the zoo (`glimpse_tui.zoo`, the Forecast Lab's models running on public hourly
candles) or one function in a file of your own:

    def forecast(book, closes, hours) -> list[float]   # one probability per bin, sums to 1

Drop a file defining `forecast` into ~/.config/glimpse/bots/ and it appears in the terminal under MINE.

Each cycle the runner prices the nearest closes, buys the bins its picture says are underpriced (fractional Kelly,
or the bot's own opportunistic `policy.Policy`; never past the price the picture justifies; never an order whose
expected value the commission would eat; every order checked against the server's estimate first), and sells down a
position it bought while the market pays more for it than the picture says it is worth. A file of your own may set
`POLICY = {"max_price": 3, "region": "above", ...}` to trade opportunistically. It never touches a position
it did not open: what it owns is recorded in a ledger on this machine. It stops at its budget and its spend caps.
Dry run by default; a dry run keeps a paper ledger so it behaves as the live bot would.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import time
import tomllib
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import fmt
from . import policy as PL
from . import pricing as P
from .api import ApiError, Book, Glimpse, MarketRow
from .auth import config_dir
from .policy import Leg, Policy

Forecast = Callable[[Book, list[float], float], list[float]]


@dataclass
class BotConfig:
    objective: str = "match"         # match: buy until the site shows the bot's forecast; edge: buy only positive EV
    close_share: float = 0.02        # match: most spent on one close in one cycle, as a share of the budget
    match_gap: float = 0.03          # match: a close whose shown odds are this close to the picture is left alone
    markets: int = 0                 # closes traded per cycle, nearest first; 0 trades every open close of the series
    interval_s: float = 1            # a cycle a second: fresh spot, fresh book of every close, fresh picture, then trade
    bankroll_sats: float = 100_000   # the bot's budget: Kelly sizes against it, and open cost never exceeds it
    kelly: float = 0.5
    min_edge: float = 0.04           # net ev/cost, already after both fees: any real edge left over is traded
    fill_margin: float = 0.0         # edge: stop buying a range this far short of its value (0: buy up to fair value)
    edge_close_share: float = 1.0    # edge: most held in one close, as a share of the budget
    exit_edge: float = 0.02          # sell when the market pays this much more than the picture says a position is worth
    cycle_share: float = 0.20        # spend cap per cycle, as a share of the budget
    hour_share: float = 10.0         # spend cap per hour, as a share of the budget: exits recycle it, so it can turn over
    max_per_cycle_sats: float = float("inf")    # hard ceilings from bots.toml; the shares above set the working caps
    max_per_hour_sats: float = float("inf")
    picture_budget_s: float = 0.8    # seconds of model time a cycle: new closes first, then the nearest, then the stalest
    max_markets_per_order: int = 8   # closes bought in one cycle, largest expected profit first: each needs a server estimate
    stop_before_close_s: int = 60
    estimate_tolerance: float = 0.005

    @classmethod
    def load(cls, **override: float) -> BotConfig:
        f = config_dir() / "bots.toml"
        raw = tomllib.loads(f.read_text()) if f.is_file() else {}
        known = {x.name for x in fields(cls)}
        return cls(**{k: v for k, v in {**raw, **override}.items() if k in known})

    def for_bot(self, bot: Bot) -> BotConfig:
        """This config with the bot's own trading fields laid over it: a bot built to trade one way always does."""
        return BotConfig(**{**asdict(self), **(bot.trading or {})})

    def with_budget(self, sats: float) -> BotConfig:
        """One number to deploy a bot: the caps follow the budget unless bots.toml sets them lower."""
        return BotConfig(**{**asdict(self), "bankroll_sats": sats,
                            "max_per_cycle_sats": min(self.max_per_cycle_sats, sats * self.cycle_share),
                            "max_per_hour_sats": min(self.max_per_hour_sats, sats * self.hour_share)})


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
    policy: Policy | None = None            # an opportunistic trading rule; None trades fractional Kelly
    reads_market: bool = False              # the picture blends in what the site shows for the close
    trading: dict | None = None             # BotConfig fields of its own (see BotConfig.for_bot)

    def probs(self, book: Book, ctx) -> list[float]:
        if self.reads_market:
            import numpy as np

            ctx.market = PL.shown(np.asarray(book.shares, dtype=float), P.display_alpha(len(book.shares)))
        if self.model is not None:
            return [float(x) for x in self.model.fn(ctx)]
        closes = [float(x) for x in ctx.bars["close"].to_numpy()[-336:]]
        return list(self.forecast(book, closes, ctx.hours))


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
                raw = getattr(mod, "POLICY", None)
                pol = raw if isinstance(raw, Policy) else Policy(**raw) if isinstance(raw, dict) else None
                out.append(Bot(f.stem, f.stem, "Mine", (doc.splitlines() or [str(f)])[0][:70], doc or str(f), forecast=fn, policy=pol))
        except Exception as e:
            out.append(Bot(f.stem, f.stem, "Mine", f"failed to load: {e.__class__.__name__}", f"{f}: {e}", error=str(e) or e.__class__.__name__))
    return out


def discover() -> list[Bot]:
    """Every bot the terminal can run: the zoo, then your own files. Loads numpy, pandas and scipy on first use."""
    from . import zoo

    return [Bot(m.id, m.name, m.family, m.blurb, m.description, m.kind, m.factors, model=m, policy=m.policy,
                reads_market=m.reads_market, trading=m.trading) for m in zoo.catalog()] + user_bots()


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

    def prune(self, now: float) -> None:
        """A closed market settles on the server; there is nothing left for a bot to manage."""
        for modes in self.data.values():
            for topics in modes.values():
                for tid in [t for t, v in topics.items() if v.get("end", 0) <= now]:
                    del topics[tid]

    def add(self, bot: str, mode: str, book: Book, asset: str, index: int, contracts: float, cost: float) -> None:
        t = self.topics(bot, mode).setdefault(str(book.topic_id), {"end": book.end_time_utc, "asset": asset, "legs": {}})
        lo, hi = book.bins[index]
        leg = t["legs"].setdefault(str(book.option_ids[index]), [0.0, 0.0, lo, hi])
        leg[0], leg[1] = round(leg[0] + contracts, 2), leg[1] + cost

    def reduce(self, bot: str, mode: str, topic_id: int, option_id: int, contracts: float) -> None:
        """Part of a position sold: its cost shrinks in proportion. Selling all of it removes the leg."""
        t = self.topics(bot, mode).get(str(topic_id))
        leg = t and t["legs"].get(str(option_id))
        if not leg:
            return
        if contracts >= leg[0] - 0.005:
            self.remove(bot, mode, topic_id, option_id)
            return
        keep = (leg[0] - contracts) / leg[0]
        leg[0], leg[1] = round(leg[0] - contracts, 2), leg[1] * keep

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

    def holdings(self, bot: str, mode: str) -> list[tuple[int, str, int, int, float, float, float, float]]:
        """(close, asset, topic, option, low, high, contracts, cost), soonest first then lowest range."""
        out = [(t["end"], t.get("asset", ""), int(tid), int(o), v[2], v[3], v[0], v[1])
               for tid, t in self.topics(bot, mode).items() for o, v in t["legs"].items()]
        return sorted(out, key=lambda x: (x[0], x[4]))


# ── decision ────────────────────────────────────────────────

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
    spend = min(cfg.kelly * min(total_f, 1.0) * cfg.bankroll_sats - held_cost, budget_sats,
                cfg.edge_close_share * cfg.bankroll_sats - held_cost)
    if spend < 1:
        return []
    legs: list[Leg] = []
    quote = PL.Quote(q, book.alpha)
    for i, f, _ in sorted(cands, key=lambda c: -c[2]):
        stake = spend * f / total_f
        target = probs[i] * net / ((1 + P.FEE) * (1 + cfg.fill_margin))   # the dearest price that still leaves the margin
        d, c = PL.fill_to(quote, book.alpha, i, target, stake)
        if d <= 0 or c < 0.01:
            continue
        legs.append(Leg(i, d, c))
        quote.add(i, d)
    return legs if PL.worth_sending(legs, probs) else []


def plan(bot: Bot, book: Book, probs: list[float], budget_sats: float, spot: float, cfg: BotConfig | None = None) -> list[Leg]:
    """What `bot` would buy on this close right now with this budget and nothing held: the first cycle a runner would
    make here, and the order a one-off snapshot bet sends. Kelly bots size against the budget; policy bots by stake.
    Pass `cfg` (already sized to the budget) when planning many closes, so bots.toml is read once."""
    cfg = (cfg or BotConfig.load().with_budget(budget_sats)).for_bot(bot)
    if cfg.objective == "match":
        return PL.match(book, probs, min(cfg.close_share * cfg.bankroll_sats, cfg.max_per_cycle_sats))[0]
    if bot.policy is not None:
        return PL.decide(book, probs, bot.policy, cfg.bankroll_sats, cfg.max_per_cycle_sats, {}, 0.0, spot)
    return decide(book, probs, cfg, cfg.max_per_cycle_sats, 0.0)


def exits(book: Book, probs: list[float], held: dict[int, tuple[float, float]], cfg: BotConfig) -> list[tuple[int, float, float]]:
    """(bin index, contracts, proceeds): each position sold down while the market pays more for the next contract
    than the picture says it is worth, by `exit_edge`. Partial: an overpaid range is trimmed back to fair value."""
    return PL.sell_down(book, probs, held, cfg.exit_edge)


# ── runner ──────────────────────────────────────────────────

_POOL = None


def pool():
    """The runners' own worker threads. asyncio.to_thread shares one pool with everything else, and the bots screen
    queues hundreds of pictures in it when it opens: a runner waiting its turn behind them sat a minute on its first
    cycle without pricing a close. Runners draw in these threads instead, so trading never queues behind the screen."""
    global _POOL
    if _POOL is None:
        from concurrent.futures import ThreadPoolExecutor

        _POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bot")
    return _POOL


async def off_loop(fn, *args):
    """fn(*args) in a runner thread."""
    return await asyncio.get_running_loop().run_in_executor(pool(), fn, *args)

async def snapshot(api: Glimpse, batch_id: str, max_age: float) -> list[MarketRow]:
    """Every open close of a series, shared by every runner on the same client: one request a second serves them all,
    however many bots run. Kept on the client so a new client never sees another's rows."""
    cache = api.__dict__.setdefault("_bot_snapshots", {})
    at, rows, lock = cache.setdefault(batch_id, (0.0, [], asyncio.Lock()))
    async with lock:
        at, rows, _ = cache[batch_id]
        if time.time() - at >= max_age or not rows:
            rows = await api.all_markets(batch_id)
            cache[batch_id] = (time.time(), rows, lock)
    return rows


def stale(api: Glimpse, batch_id: str) -> None:
    """A trade moved the book: the next cycle, this bot's or another's, reads it afresh."""
    cache = api.__dict__.get("_bot_snapshots", {})
    if batch_id in cache:
        cache[batch_id] = (0.0, *cache[batch_id][1:])


def best_edge(book: Book, probs: list[float]) -> float:
    """The largest net expected return per sat on any one range of this close, after both fees."""
    net = P.PAYOUT_SATS * (1 - P.FEE)
    return max(((p * net - c) / c for p, c in ((p, x * (1 + P.FEE)) for p, x in zip(probs, book.prices)) if c > 0), default=0.0)


@dataclass
class Picture:
    """One close as the bot sees it this cycle."""
    book: Book
    probs: list[float]
    edge: float                                 # best_edge: how mispriced the close is by the bot's picture
    gap: float = 0.0                            # policy.gap: how far what the site shows is from the picture


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
    scanned: int = 0                            # closes priced last cycle
    mispriced: int = 0                          # of those, closes with a range past min_edge
    marks: dict[tuple[int, int], tuple[float, float, float]] = field(default_factory=dict)  # (topic, option) -> (price, p, sale value)
    on_trade: Callable[[], None] | None = None
    on_log: Callable[[str], None] | None = None
    _task: asyncio.Task | None = None
    _stopping: bool = False                     # set by stop(): a worker thread drawing pictures gives up at the next close
    _quiet_at: float = 0.0
    _probs: dict[int, tuple[list[float], float]] = field(default_factory=dict)   # topic -> (picture, when drawn)

    def __post_init__(self) -> None:
        if self.bot.trading:
            self.cfg = self.cfg.for_bot(self.bot)

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

    def start(self) -> None:
        if not self.running:
            self._stopping = False
            self.started_at = time.time()
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._stopping = True                   # a thread cannot be cancelled; it checks this between closes
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

    def tag(self, book: Book) -> str:
        return fmt.question(self.feed.asset, book.end_time_utc)[:-4]

    async def _loop(self) -> None:
        self.say(f"started  {'LIVE' if self.live else 'dry run: paper orders never reach the market'}  "
                 f"budget {fmt.sats(self.cfg.bankroll_sats)}  every close of {self.series or 'the series'}, every {self.cfg.interval_s:g}s  "
                 + ("buying until the site shows its forecast" if self.matching else "buying positive edge"))
        while True:
            began = time.monotonic()
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:      # one bad cycle must not stop the bot
                self.say(f"cycle failed: {e}")
            await asyncio.sleep(max(0.2, self.cfg.interval_s - (time.monotonic() - began)))   # a steady beat, not interval + work

    def _pictures(self, books: list[Book]) -> tuple[list[Picture], str]:
        """The bot's picture of every close, each against this second's book. Runs in a worker thread. A mixture model
        over a week of closes takes seconds (minutes on a cold start beside the bots screen), so pictures are drawn within
        `picture_budget_s` a cycle: closes not drawn yet (nearest first), then the nearest few, then the stalest, and a
        cold start trades the closes it has reached while the rest are still being drawn. A model that reads the market
        redraws every close it has already drawn, every cycle, before any new one. A fast model redraws them all."""
        live = {b.topic_id for b in books}
        for tid in [t for t in self._probs if t not in live]:
            del self._probs[tid]
        fresh = [b for b in books if b.topic_id not in self._probs]
        rest = [b for b in books if b.topic_id in self._probs]
        if self.bot.reads_market:       # its picture reads the market, which moves every cycle: every close it has
            must, maybe = rest, fresh   # drawn is redrawn (cheap: the model keeps its slow half), new ones while time lasts
        else:
            must, maybe = [], fresh + rest[:3] + sorted(rest[3:], key=lambda b: self._probs[b.topic_id][1])
        order = must + maybe            # nearest first within each: a cold start trades the nearest closes within seconds
        deadline, err = time.monotonic() + self.cfg.picture_budget_s, ""
        for n, book in enumerate(order):
            if self._stopping or (n >= max(len(must), 1) and time.monotonic() > deadline):
                break
            try:
                probs = self.bot.probs(book, self.feed.ctx(book.bins, book.end_time_utc))
            except Exception as e:
                err = err or f"{self.tag(book)}  no picture: {e}"
                continue
            if len(probs) != len(book.bins) or abs(sum(probs) - 1) > 1e-6:
                return [], f"{self.name}: forecast must return {len(book.bins)} probabilities summing to 1"
            self._probs[book.topic_id] = (probs, time.monotonic())
        out = []
        for book in books:
            probs = self._probs.get(book.topic_id, (None,))[0]
            if probs is not None and len(probs) == len(book.bins):
                out.append(Picture(book, probs, best_edge(book, probs), PL.gap(book, probs)))
        return out, err

    def _paper_book(self, row: MarketRow) -> Book:
        """The book as this bot sees it. A paper order never reaches the market, so on paper the bot's own holdings are
        added to the shares, as a live fill would have added them: otherwise it would buy the same edge every cycle."""
        book = Book.from_row(row)
        if not self.live:
            held = self.ledger.legs(self.bot.id, self.mode, book.topic_id)
            if held:
                at = {o: i for i, o in enumerate(book.option_ids)}
                for o, (n, _) in held.items():
                    if o in at:
                        book.shares[at[o]] += n
                book.reprice()
        return book

    def _legs(self, pic: Picture, room: float, held: dict, held_cost: float) -> list[Leg]:
        if self.matching:
            return PL.match(pic.book, pic.probs, min(room, self.cfg.close_share * self.cfg.bankroll_sats))[0]
        if self.bot.policy:
            return PL.decide(pic.book, pic.probs, self.bot.policy, self.cfg.bankroll_sats, room, held, held_cost, self.feed.spot)
        return decide(pic.book, pic.probs, self.cfg, room, held_cost)

    def _plans(self, pics: list[Picture], room: float, owned: dict[int, tuple[dict, float]]) -> list[tuple[float, Picture, list[Leg], float]]:
        """(score, close, legs, gap after) for every close worth buying, best first. Worker thread. Matching scores a
        close by how much of the gap its order closes; trading on edge, by expected profit."""
        net = P.PAYOUT_SATS * (1 - P.FEE)
        out = []
        for pic in pics:
            if self._stopping:
                break
            if self.matching:
                if pic.gap < self.cfg.match_gap:
                    continue
                legs, after = PL.match(pic.book, pic.probs, min(room, self.cfg.close_share * self.cfg.bankroll_sats))
                if legs and sum(x.cost_sats for x in legs) >= 1:
                    out.append((pic.gap - after, pic, legs, after))
                continue
            if pic.edge < self.cfg.min_edge and not self.bot.policy:
                continue
            legs = self._legs(pic, room, *owned.get(pic.book.topic_id, ({}, 0.0)))
            cost = sum(x.cost_sats for x in legs)
            if legs and cost >= 1:
                out.append((sum(pic.probs[x.index] * net * x.contracts for x in legs) - cost, pic, legs, pic.gap))
        return sorted(out, key=lambda x: -x[0])

    @property
    def matching(self) -> bool:
        return self.cfg.objective == "match"

    async def cycle(self) -> None:
        """One sweep: fresh spot, every open close of the series, the bot's picture of each; sell what the market
        overpays, then buy the closes the picture says are most underpriced, as many as the caps allow, in one order."""
        self.cycles += 1
        await self.feed.refresh(max_age=max(self.cfg.interval_s, 1.0))
        if not self.feed.ready:
            self.say("no price data yet, waiting")
            return
        now = time.time()
        self.ledger.prune(now)
        rows = await snapshot(self.api, self.batch_id, self.cfg.interval_s * 0.9)
        rows = [r for r in rows if r.shares and r.quote_mode == "live" and r.end_time_utc - now >= self.cfg.stop_before_close_s]
        if self.cfg.markets:
            rows = rows[: self.cfg.markets]
        pics, err = await off_loop(self._pictures, [self._paper_book(r) for r in rows])
        if err:
            self.say(err)
        if not pics:
            return
        if self.bot.model is not None:
            from .zoo.core import describe

            d = describe(self.feed.ctx(pics[0].book.bins, pics[0].book.end_time_utc), pics[0].probs)
            self.stance = f"{d['view']}  median {fmt.price(d['median'])}  80% {fmt.kprice(d['low'])}–{fmt.kprice(d['high'])}"
        self.scanned = len(pics)
        self.mispriced = sum(1 for x in pics if (x.gap >= self.cfg.match_gap if self.matching else x.edge >= self.cfg.min_edge))
        self._mark(pics)
        traded = False

        pol = None if self.matching else self.bot.policy     # matching: every bot trades its forecast the same way
        if not (pol and pol.hold):
            sells = [(pic.book, i, c, got) for pic in pics if (held := self.ledger.legs(self.bot.id, self.mode, pic.book.topic_id))
                     for i, c, got in (PL.match_sells(pic.book, pic.probs, held) if self.matching
                                       else exits(pic.book, pic.probs, held, self.cfg))]
            if sells:
                traded = await self._exit_many(sells) or traded

        room = min(self.cfg.max_per_cycle_sats, self.cfg.max_per_hour_sats - self.spent_last_hour(),
                   self.cfg.bankroll_sats - self.open_cost)
        if room >= 1:
            owned = {pic.book.topic_id: (self.ledger.legs(self.bot.id, self.mode, pic.book.topic_id),
                                         self.ledger.open_cost(self.bot.id, self.mode, pic.book.topic_id)) for pic in pics}
            chosen, left = [], room
            for _, pic, legs, after in await off_loop(self._plans, pics, room, owned):
                if len(chosen) >= self.cfg.max_markets_per_order or left < 1:
                    break
                cost = sum(x.cost_sats for x in legs)
                if cost > left:                     # the caps are shared across closes: resize the last one to what is left
                    legs = self._legs(pic, left, *owned[pic.book.topic_id])
                    cost = sum(x.cost_sats for x in legs)
                    if not legs or cost < 1:
                        continue
                value = sum(pic.probs[x.index] * P.PAYOUT_SATS * (1 - P.FEE) * x.contracts for x in legs)
                chosen.append((pic.book, legs, cost, pic.gap, after, value))
                left -= cost
            if chosen:
                traded = await self._enter_many(chosen) or traded
        if traded:
            stale(self.api, self.batch_id)
            if self.live and self.on_trade:
                self.on_trade()
        elif time.time() - self._quiet_at >= 15:
            self._quiet_at = time.time()
            if self.matching:
                worst = max(pics, key=lambda x: x.gap)
                why = ("budget fully deployed, waiting for exits" if room < 1 else
                       f"every close within {self.cfg.match_gap:.0%} of the forecast" if worst.gap < self.cfg.match_gap else "")
                self.say(f"scanned {len(pics)} closes  {self.mispriced} off the forecast  widest gap {worst.gap:.0%} at "
                         f"{self.tag(worst.book)}" + (f"  ·  {why}" if why else ""))
            else:
                best = max(pics, key=lambda x: x.edge)
                why = ("budget fully deployed, waiting for exits" if room < 1 else
                       "nothing it may buy under its rule" if pol else f"nothing past {self.cfg.min_edge:.0%}")
                self.say(f"scanned {len(pics)} closes  {self.mispriced} mispriced  best {best.edge:+.1%} at {self.tag(best.book)}  ·  {why}")
        self.ledger.save()

    def _mark(self, pics: list[Picture]) -> None:
        """For every range the bot holds: today's price, the picture's chance, and what the position would fetch sold
        back into the book now, after the exit fee. The last is the honest mark: the price alone overstates a big
        position in a thin book, since selling it walks the price back down."""
        for pic in pics:
            held = self.ledger.legs(self.bot.id, self.mode, pic.book.topic_id)
            if held:
                at = {o: i for i, o in enumerate(pic.book.option_ids)}
                q = PL.Quote(pic.book.shares, pic.book.alpha)
                for o, (n, _) in held.items():
                    if o in at:
                        i = at[o]
                        n = min(n, pic.book.shares[i])
                        self.marks[(pic.book.topic_id, o)] = (pic.book.prices[i], pic.probs[i],
                                                              max(q.base - q.cost_after(i, -n), 0.0) * (1 - P.FEE))

    async def _exit_many(self, sells: list[tuple[Book, int, float, float]]) -> bool:
        """Every sale of the cycle in one request, grouped by close."""
        groups: dict[int, list[tuple[Book, int, float, float]]] = {}
        for x in sells:
            groups.setdefault(x[0].topic_id, []).append(x)
        for (book, i, contracts, proceeds) in sells:
            held = self.ledger.legs(self.bot.id, self.mode, book.topic_id).get(book.option_ids[i], (contracts, 0.0))[0]
            part = "" if contracts >= held - 0.005 else f" of {fmt.contracts(held)}"
            self.say(f"{self.tag(book)}  {'SELL' if self.live else 'would sell'} {fmt.span(*book.bins[i])}  "
                     f"{fmt.contracts(contracts)}{part} for ₿{proceeds:,.0f}")
        got = {t: sum(x[3] for x in g) for t, g in groups.items()}
        if self.live:
            try:
                fills = await self.api.sell_many([(t, [(b.option_ids[i], c) for b, i, c, _ in g]) for t, g in groups.items()])
            except ApiError as e:
                self.say(f"sell failed: {e}")
                if "may or may not" in str(e):
                    self.stop()
                return False
            for f in fills:
                if f.error:
                    self.say(f"{self.tag(groups[f.topic_id][0][0])}  sell rejected: {f.error}")
                    del got[f.topic_id]
                else:
                    got[f.topic_id] = f.cost_sats
        for t, proceeds in got.items():
            for book, i, contracts, _ in groups[t]:
                self.ledger.reduce(self.bot.id, self.mode, t, book.option_ids[i], contracts)
            self.sold_sats += proceeds
            self.trades += 1
        return bool(got)

    async def _enter_many(self, chosen: list[tuple[Book, list[Leg], float, float, float, float]]) -> bool:
        """Every buy of the cycle in one request. Live, each close is checked against the server's estimate first; a
        close whose book moved since the snapshot is dropped, the rest go. On paper the local price is the fill."""
        orders = [[(b.option_ids[x.index], x.contracts) for x in legs] for b, legs, *_ in chosen]
        if self.live:
            ests = await asyncio.gather(*(self.api.estimate_legs(b.topic_id, o) for (b, *_), o in zip(chosen, orders)),
                                        return_exceptions=True)
        else:                           # a paper book carries the bot's paper holdings, which the server has never seen
            ests = [(total / (1 + P.FEE), total * P.FEE / (1 + P.FEE)) for _, _, total, *_ in chosen]
        ok = []
        for (book, legs, total, before, after, value), order, e in zip(chosen, orders, ests):
            if isinstance(e, Exception):
                self.say(f"{self.tag(book)}  {e}")
                continue
            raw, fee = e
            charge = raw + max(fee, P.MIN_FEE_SATS)
            if abs(charge - total) > self.cfg.estimate_tolerance * total + 5:
                self.say(f"{self.tag(book)}  book moved (local {total:,.0f} vs server {charge:,.0f}), skipped")
                continue
            top = max(legs, key=lambda x: x.cost_sats)
            paid = [book.prices[x.index] for x in legs]
            cheap = (f"  shown gap {before:.0%} → {after:.0%}" if self.matching else
                     f"  at {min(paid):.1f}–{max(paid):.1f} sats" if self.bot.policy else
                     f"  {1 - charge / value:.0%} under its value" if value > 0 else "")
            self.say(f"{self.tag(book)}  {'BUY' if self.live else 'would buy'} {len(legs)} bins  ₿{charge:,.0f}  "
                     f"top {fmt.span(*book.bins[top.index])}{cheap}")
            ok.append((book, legs, total, order, charge))
        if not ok:
            return False
        paid = {b.topic_id: charge for b, _, _, _, charge in ok}
        if self.live:
            try:
                fills = await self.api.buy_many([(b.topic_id, order) for b, _, _, order, _ in ok])
            except ApiError as e:
                self.say(f"buy failed: {e}")
                if "may or may not" in str(e):
                    self.stop()             # unknown fill state: a human should look before more orders go out
                return False
            for f in fills:
                if f.error:
                    self.say(f"{self.tag(next(b for b, *_ in ok if b.topic_id == f.topic_id))}  rejected: {f.error}")
                    del paid[f.topic_id]
                else:
                    paid[f.topic_id] = f.cost_sats + f.fee_sats
            if paid:
                self.say(f"filled {len(paid)} of {len(ok)} closes  ₿{sum(paid.values()):,.0f}")
        for book, legs, total, _, _ in ok:
            if book.topic_id not in paid:
                continue
            got = paid[book.topic_id]
            for x in legs:
                self.ledger.add(self.bot.id, self.mode, book, self.feed.asset, x.index, x.contracts, got * x.cost_sats / total)
            self.spent.append((time.time(), got))
            self.paid_sats += got
            self.trades += 1
        return bool(paid)
