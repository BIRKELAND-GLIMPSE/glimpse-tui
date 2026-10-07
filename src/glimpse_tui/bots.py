"""Local bots. A bot is a picture of the future; the runner owns every safety rule.

A bot is either a model from the zoo (`glimpse_tui.zoo`, the Forecast Lab's models running on public hourly
candles) or one function in a file of your own:

    def forecast(book, closes, hours) -> list[float]   # one probability per bin, sums to 1

Drop a file defining `forecast` into ~/.config/glimpse/bots/ and it appears in the terminal under MINE.

The runner visits the series one close at a time, every `interval_s` seconds, nearest to farthest and then round again.
On each visit it reads that close's book fresh, draws the bot's picture of that close alone, and trades both ways:

- it takes profit: a position it holds is sold down, part of it if that is all the market overpays for, while the
  market pays more for the next contract, after the exit fee, than the picture says it is worth and more than it
  cost (`profit_only = false` in bots.toml also trims an overpriced position held at a loss);
- it buys what has positive expected value: what the picture says is underpriced (matching the site's odds to the
  picture, fractional Kelly, or the bot's own opportunistic `policy.Policy`; never past the price the picture
  justifies; never an order whose expected value the commission would eat; every order checked against the server's
  estimate first), up to that close's even share of the budget, so one round reaches every close. What a sale frees
  of the budget it may buy with again.

A bot may read the market too. One with `reads_market` is handed what the site shows for the close it is pricing
(`ctx.market`); one with `reads_series` is handed every open close of the series as the site shows it (`ctx.series`,
built by `shown_series`), the close it is visiting fresh from its book and the rest as the runner last saw them, so
the smoothing bots can tell a close that is out of line with its neighbours.

Every visit says what it did, or why it did nothing. A file of your own may set
`POLICY = {"max_price": 3, "region": "above", ...}` to trade opportunistically. It never touches a position
it did not open: what it owns is recorded in a ledger on this machine. It stops at its budget and its spend caps.
Dry run by default; a dry run keeps a paper ledger so it behaves as the live bot would.

Any bot can instead manage your portfolio (`Runner(manage=True)`): it reads every position your account holds in the
series' open closes, whoever opened it, and rebalances them toward its picture. It sells what the market pays more for
than the picture says a position is worth, only at a gain over what it cost unless `profit_only` is off, and buys
what the market sells under its value. Its budget is the fresh sats it may add: what it sells, it may buy with again.

The Profit Taker (`ProfitTaker`, the zoo's `take_profit`) only sells. It reads every position your account holds in every
series, visits only the closes you hold something in, and sells the part of each position the market overpays at a
profit, net of both fees. It never buys and has no budget.
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
from typing import ClassVar

from . import fmt
from . import policy as PL
from . import pricing as P
from .api import PAIRS, ApiError, Batch, Book, Glimpse, MarketRow, Position, parse_bin
from .auth import config_dir
from .policy import Leg, Policy

Forecast = Callable[[Book, list[float], float], list[float]]
PORTFOLIO = "portfolio"                 # the ledger's name for the account's own positions, as a manager sees them


@dataclass
class BotConfig:
    objective: str = "match"         # match: buy toward the bot's forecast on the site, never past a range's value; edge: Kelly on EV
    match_gap: float = 0.03          # match: a close whose shown odds are this close to the picture is left alone
    markets: int = 0                 # closes in the round, nearest first; 0 visits every open close of the series
    interval_s: float = 5            # one close a visit, one visit every five seconds: fresh book, fresh picture, then trade
    list_s: float = 300              # how often the list of open closes is read again (each visit reads its own close's book)
    bankroll_sats: float = 100_000   # the bot's budget in this series: each close gets an even share, and open cost never exceeds it
    kelly: float = 0.5
    min_edge: float = 0.04           # net ev/cost, already after both fees: any real edge left over is traded
    fill_margin: float = 0.0         # edge: stop buying a range this far short of its value (0: buy up to fair value)
    edge_close_share: float = 1.0    # edge: most held in one close, as a share of the budget (the even share caps it too)
    exit_edge: float = 0.02          # sell when the market pays this much more than the picture says a position is worth
    cycle_share: float = 0.20        # spend cap per visit, as a share of the budget
    hour_share: float = 10.0         # spend cap per hour, as a share of the budget: exits recycle it, so it can turn over
    max_per_cycle_sats: float = float("inf")    # hard ceilings from bots.toml; the shares above set the working caps
    max_per_hour_sats: float = float("inf")
    stop_before_close_s: int = 60
    estimate_tolerance: float = 0.005
    profit_only: bool = True         # every bot: sell only at a gain over cost (take profit); False also trims overpriced losers
    portfolio_s: float = 10          # managing your portfolio, live: how often the account's positions are read afresh

    @classmethod
    def load(cls, **override: float) -> BotConfig:
        f = config_dir() / "bots.toml"
        raw = tomllib.loads(f.read_text()) if f.is_file() else {}
        known = {x.name for x in fields(cls)}
        return cls(**{k: v for k, v in {**raw, **override}.items() if k in known})

    def for_bot(self, bot: Bot) -> BotConfig:
        """This config with the bot's own trading fields laid over it: a bot built to trade one way always does."""
        known = {x.name for x in fields(BotConfig)}
        return BotConfig(**{**asdict(self), **{k: v for k, v in (bot.trading or {}).items() if k in known}})

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
    reads_series: bool = False              # the picture reads every close of the series as the site shows it (shown_series)
    trading: dict | None = None             # BotConfig fields of its own (see BotConfig.for_bot)
    sells_only: bool = False                # never buys: it takes profit on your whole portfolio (ProfitTaker runs it)

    def probs(self, book: Book, ctx, series=()) -> list[float]:
        """The bot's picture of `book`'s close. `series` is what `shown_series` gives for the series' open closes; a bot
        that reads the series is handed it as `ctx.series`, the rest never see it."""
        if self.reads_market:
            import numpy as np

            ctx.market = PL.shown(np.asarray(book.shares, dtype=float), P.display_alpha(len(book.shares)))
        if self.reads_series:
            ctx.series = tuple(series)
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
                reads_market=m.reads_market, reads_series=m.reads_series, trading=m.trading, sells_only=m.sells_only)
            for m in zoo.catalog()] + user_bots()


def shown_series(closes) -> tuple:
    """Every open close of a series as the site draws it, for a bot that reads the whole series (`Bot.reads_series`):
    one `zoo.core.Shown` per close, nearest first. `closes` are (close time, bins, shares) triples in any order; every
    close of a series shares one ladder, so its edges are built once."""
    import numpy as np

    from .zoo.core import Shown

    out, edges_of = [], {}
    for end, bins, shares in closes:
        if len(shares) < 2 or len(bins) != len(shares):
            continue
        key = (bins[0][0], bins[-1][1], len(bins))
        if key not in edges_of:
            edges_of[key] = np.array([bins[0][0]] + [hi for _, hi in bins], dtype=float)
        q = np.asarray(shares, dtype=float)
        out.append(Shown(float(end), edges_of[key], PL.shown(q, P.display_alpha(len(q)))))
    return tuple(sorted(out, key=lambda s: s.end))


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

    def replace(self, bot: str, mode: str, rows: list[MarketRow], asset: str | Callable[[int], str], held: list[Position]) -> None:
        """Every close in `rows` set to exactly the positions `held` lists there: the account's own record of what it
        owns, read from the server, wins over whatever the ledger had. Closes not in `rows` are left alone, so managers
        of different series can share one ledger. `asset` names the closes' asset, or gives it for each close."""
        topics = self.topics(bot, mode)
        by_id = {r.topic_id: r for r in rows}
        for tid in [t for t in topics if int(t) in by_id]:
            del topics[tid]
        for p in held:
            r = by_id.get(p.topic_id)
            ids = list(r.option_ids) or list(range(1, len(r.shares) + 1)) if r else []
            if r is None or p.shares < 0.01 or p.option_id not in ids:
                continue
            lo, hi = parse_bin(r.names[ids.index(p.option_id)])
            t = topics.setdefault(str(p.topic_id), {"end": r.end_time_utc, "legs": {},
                                                    "asset": asset if isinstance(asset, str) else asset(p.topic_id)})
            leg = t["legs"].setdefault(str(p.option_id), [0.0, 0.0, lo, hi])
            leg[0], leg[1] = round(leg[0] + p.shares, 2), leg[1] + p.cost_sats

    def legs(self, bot: str, mode: str, topic_id: int) -> dict[int, tuple[float, float]]:
        t = self.topics(bot, mode).get(str(topic_id)) or {}
        return {int(o): (v[0], v[1]) for o, v in (t.get("legs") or {}).items()}

    def open_cost(self, bot: str, mode: str, topic_id: int | None = None, among: set[int] | None = None) -> float:
        """What is held at cost: in one close, in the closes `among` (one series), or everywhere."""
        ts = self.topics(bot, mode)
        return sum(v[1] for tid, t in ts.items() if (topic_id is None or tid == str(topic_id)) and (among is None or int(tid) in among)
                   for v in t["legs"].values())

    def positions(self, bot: str, mode: str) -> list[tuple[int, str, float, float, float, float]]:
        """(close, asset, low, high, contracts, cost), soonest first."""
        out = [(t["end"], t.get("asset", ""), v[2], v[3], v[0], v[1]) for t in self.topics(bot, mode).values() for v in t["legs"].values()]
        return sorted(out)

    def holdings(self, bot: str, mode: str, among: set[int] | None = None) -> list[tuple[int, str, int, int, float, float, float, float]]:
        """(close, asset, topic, option, low, high, contracts, cost), soonest first then lowest range; `among` keeps one series."""
        out = [(t["end"], t.get("asset", ""), int(tid), int(o), v[2], v[3], v[0], v[1])
               for tid, t in self.topics(bot, mode).items() if among is None or int(tid) in among for o, v in t["legs"].items()]
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


def share(cfg: BotConfig, closes: int) -> float:
    """One close's even part of the budget, the most a bot holds in it: what lets one round reach every close."""
    return min(cfg.bankroll_sats / max(closes, 1), cfg.max_per_cycle_sats)


def buys(bot: Bot, cfg: BotConfig, book: Book, probs: list[float], room: float, spot: float,
         held: dict | None = None, held_cost: float = 0.0, bankroll: float | None = None) -> list[Leg]:
    """What `bot` buys on this close for at most `room` sats, given what it holds there. Matching buys toward the
    forecast on the site, trimmed so no contract costs more than the picture says it is worth; a policy bot buys by
    its rule; otherwise fractional Kelly on edge. `bankroll` is what Kelly and the policies size against."""
    if room < 1:
        return []
    if cfg.objective == "match":
        legs = PL.valued(book, probs, PL.match(book, probs, room)[0])
        return legs if PL.worth_sending(legs, probs) else []
    sizing = cfg if bankroll is None else BotConfig(**{**asdict(cfg), "bankroll_sats": bankroll})
    if bot.policy is not None:
        return PL.decide(book, probs, bot.policy, sizing.bankroll_sats, room, held or {}, held_cost, spot)
    return decide(book, probs, sizing, room, held_cost)


def plan(bot: Bot, book: Book, probs: list[float], budget_sats: float, spot: float, cfg: BotConfig | None = None,
         closes: int = 1) -> list[Leg]:
    """What `bot` would buy on this close right now with this budget and nothing held: the visit a runner would make
    here, with the close's even share of the budget across `closes`, and the order a one-off snapshot bet sends.
    Pass `cfg` (already sized to the budget) when planning many closes, so bots.toml is read once."""
    if bot.sells_only:
        return []
    cfg = (cfg or BotConfig.load().with_budget(budget_sats)).for_bot(bot)
    return buys(bot, cfg, book, probs, share(cfg, closes), spot)


def exits(book: Book, probs: list[float], held: dict[int, tuple[float, float]], cfg: BotConfig
          ) -> list[tuple[int, float, float]]:
    """(bin index, contracts, proceeds): the profit to take on this close. Each position is sold down while the market
    pays more for the next contract than the picture says it is worth, by `exit_edge`, and, with `profit_only`, more
    than it cost. Partial: an overpaid range is trimmed back to fair value or to its cost, not dumped. Every bot sells
    this way, whatever it buys by: selling only ever swaps a position for more sats than it is worth to the bot."""
    return PL.sell_down(book, probs, held, cfg.exit_edge, cfg.profit_only)


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


async def listing(api: Glimpse, batch_id: str, max_age: float) -> list[MarketRow]:
    """Every open close of a series: the list a round walks. Shared by every runner on the same client and read again
    every `max_age` seconds; each visit reads its own close's book afresh, so this only says which closes exist.
    Kept on the client so a new client never sees another's rows."""
    cache = api.__dict__.setdefault("_bot_snapshots", {})
    at, rows, lock = cache.setdefault(batch_id, (0.0, [], asyncio.Lock()))
    async with lock:
        at, rows, _ = cache[batch_id]
        if time.time() - at >= max_age or not rows:
            rows = await api.all_markets(batch_id)
            cache[batch_id] = (time.time(), rows, lock)
    return rows


def best_edge(book: Book, probs: list[float]) -> float:
    """The largest net expected return per sat on any one range of this close, after both fees."""
    net = P.PAYOUT_SATS * (1 - P.FEE)
    return max(((p * net - c) / c for p, c in ((p, x * (1 + P.FEE)) for p, x in zip(probs, book.prices, strict=False)) if c > 0), default=0.0)


@dataclass
class Picture:
    """One close as the bot sees it on this visit."""
    book: Book
    probs: list[float]
    edge: float                                 # best_edge: how mispriced the close is by the bot's picture
    gap: float = 0.0                            # policy.gap: how far what the site shows is from the picture


def _round() -> dict:
    return {"at": time.time(), "visited": 0, "bought": 0, "paid": 0.0, "sold_on": 0, "sold": 0.0, "gain": 0.0}


@dataclass
class Runner:
    """One bot trading one series, a close at a time: see the module's docstring."""
    buys: ClassVar[bool] = True                 # False for the Profit Taker, which only sells
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
    cycles: int = 0                             # visits made
    trades: int = 0
    paid_sats: float = 0.0                      # this session
    sold_sats: float = 0.0
    gain_sats: float = 0.0                      # this session: what its sales fetched over what the contracts sold had cost
    started_at: float = 0.0
    rounds: int = 0                             # rounds finished: every close of the series visited once
    step: int = 0                               # the close visited last, counted from the nearest
    of: int = 0                                 # open closes in the round
    marks: dict[tuple[int, int], tuple[float, float, float]] = field(default_factory=dict)  # (topic, option) -> (price, p, sale value)
    manage: bool = False                        # manage the account's whole portfolio in the series, not only its own buys
    on_trade: Callable[[], None] | None = None
    on_log: Callable[[str], None] | None = None
    _task: asyncio.Task | None = None
    _stopping: bool = False                     # set by stop(): a picture still drawing in its thread is thrown away
    _closes: list[MarketRow] = field(default_factory=list)     # the round: open closes the bot may trade, nearest first
    _series: set[int] = field(default_factory=set)             # every open close of the series: its budget and portfolio
    _cursor: tuple[int, int] = (0, 0)           # (close time, topic) of the close visited last: the next visit takes the one after
    _round: dict = field(default_factory=_round)
    _synced_at: float = 0.0                     # managing: when the account's positions were last read
    _seed: dict[int, dict[int, float]] = field(default_factory=dict)   # managing on paper: topic -> {option: contracts} read at the start
    _shown: dict[int, tuple[float, list[float]]] = field(default_factory=dict)   # topic -> (when, shares) as the bot last saw the close

    def __post_init__(self) -> None:
        if self.bot.trading:
            self.cfg = self.cfg.for_bot(self.bot)
        if self.manage:                         # rebalancing is about value: buy under it, sell over it. Matching spends
            self.cfg = BotConfig(**{**asdict(self.cfg), "objective": "edge"})   # toward a picture, so never here

    @property
    def name(self) -> str:
        return self.bot.name

    @property
    def mode(self) -> str:
        return "live" if self.live else "paper"

    @property
    def key(self) -> str:
        """Whose positions in the ledger: the bot's own, or the account's portfolio that every manager shares."""
        return PORTFOLIO if self.manage else self.bot.id

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def open_cost(self) -> float:
        """What it holds in this series, at cost. What the same bot holds in another series is another budget's."""
        return self.ledger.open_cost(self.key, self.mode, among=self._series or None)

    def holdings(self) -> list[tuple[int, str, int, int, float, float, float, float]]:
        """Its positions in this series, soonest first: `Ledger.holdings` for the series' open closes."""
        return self.ledger.holdings(self.key, self.mode, self._series or None)

    @property
    def committed(self) -> float:
        """What counts against the budget. A bot of its own: the cost of what it holds. A manager: the fresh sats it has
        put in this session, bought less sold, since the portfolio it found was already yours and its sales fund its buys."""
        return self.paid_sats - self.sold_sats if self.manage else self.open_cost

    @property
    def matching(self) -> bool:
        return self.cfg.objective == "match"

    def say(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S', time.gmtime())}  {msg}"
        self.log.append(line)
        if self.on_log:
            self.on_log(line)

    def start(self) -> None:
        if not self.running:
            self._stopping = False
            self.started_at = time.time()
            self._round = _round()
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._stopping = True                   # a thread cannot be cancelled; its picture is thrown away when it lands
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

    def tag(self, book: Book | MarketRow) -> str:
        """`12/168  XAU at 07 Oct 03:00`: which close of the round, and which close."""
        end = book.end_time_utc
        return (f"{self.step}/{self.of}  " if self.of else "") + fmt.question(self._label_of(book.topic_id), end)[:-4]

    def _asset_of(self, topic_id: int) -> str:
        """The asset a close settles on: its price feed, and what the ledger records."""
        return self.feed.asset

    def _label_of(self, topic_id: int) -> str:
        """What the log calls a close's series."""
        return self.feed.asset

    def _intro(self) -> str:
        """The first line a runner logs: what it is about to do."""
        how = (("managing your portfolio: selling what the market overpays" + (" at a profit" if self.cfg.profit_only else "")
                + ", buying what it underprices") if self.manage else
               "selling what the market overpays" + (" at a profit" if self.cfg.profit_only else "") + ", " +
               ("buying toward its forecast, never past a range's value" if self.matching else "buying positive edge"))
        return (f"budget {fmt.sats(self.cfg.bankroll_sats)} in {self.series or 'the series'}  ·  one close every "
                f"{self.cfg.interval_s:g}s, nearest to farthest, then round again  ·  {how}")

    async def _loop(self) -> None:
        self.say(f"started  {'LIVE' if self.live else 'dry run: paper orders never reach the market'}  {self._intro()}")
        while True:
            began = time.monotonic()
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:      # one bad visit must not stop the bot
                self.say(f"visit failed: {type(e).__name__}: {e}")
            await asyncio.sleep(max(0.2, self.cfg.interval_s - (time.monotonic() - began)))   # a steady beat, not interval + work

    def _paper_book(self, book: Book | MarketRow) -> Book:
        """The book as this bot sees it. A paper order never reaches the market, so on paper the bot's own holdings are
        added to the shares, as a live fill would have added them: otherwise it would buy the same edge every visit."""
        if isinstance(book, MarketRow):
            book = Book.from_row(book)
        if not self.live:
            held = {o: n for o, (n, _) in self.ledger.legs(self.key, self.mode, book.topic_id).items()}
            for o, n in self._seed.get(book.topic_id, {}).items():     # the account's real positions are in the server's
                held[o] = held.get(o, 0.0) - n                         # shares already: only paper trades move the book
            if any(abs(n) >= 0.005 for n in held.values()):
                at = {o: i for i, o in enumerate(book.option_ids)}
                for o, n in held.items():
                    if o in at:
                        book.shares[at[o]] = max(book.shares[at[o]] + n, 0.0)
                book.reprice()
        return book

    async def sync(self, rows: list[MarketRow]) -> bool:
        """Managing: the account's positions in this series' closes, into the ledger. Live, read afresh every
        `portfolio_s` (fills in between are booked locally); on paper, once at the start, after which paper trades move
        it. False while it cannot be read: a manager does not trade a portfolio it cannot see."""
        if not self.manage or (self._synced_at and (not self.live or time.time() - self._synced_at < self.cfg.portfolio_s)):
            return True
        try:
            held = await self.api.positions() if self.api.authenticated else []
        except ApiError as e:
            self.say(f"cannot read your portfolio: {e}")
            if self.live:
                return False
            held = []
        first = not self._synced_at
        self.ledger.replace(PORTFOLIO, self.mode, rows, self._asset_of, held)
        self._synced_at = time.time()
        ts = {r.topic_id for r in rows}
        if not self.live:
            self._seed = {t: {o: n for o, (n, _) in self.ledger.legs(PORTFOLIO, self.mode, t).items()} for t in ts}
        if first:
            mine = [x for x in self.ledger.holdings(PORTFOLIO, self.mode) if x[2] in ts]
            self.say(f"your {self.series + ' ' if self.series else ''}portfolio: {len(mine)} positions in {len({x[2] for x in mine})} closes, "
                     f"cost {fmt.sats(sum(x[7] for x in mine))}"
                     + ("" if self.api.authenticated else "  ·  no API key: a paper portfolio that starts empty"))
        return True

    # one visit ──────────────────────────────────────────────

    async def cycle(self) -> None:
        """One visit: the next open close of the series, nearest to farthest and then round again. Its book is read
        fresh and the bot draws its picture of that close alone; it sells what the market overpays there, then buys
        what the picture says is underpriced, up to the close's even share of the budget, and says what it did."""
        self.cycles += 1
        await self.feed.refresh(max_age=max(self.cfg.interval_s, 1.0))
        if not self.feed.ready:
            self.say("no price data yet, waiting")
            return
        now = time.time()
        self.ledger.prune(now)
        try:
            rows = await listing(self.api, self.batch_id, self.cfg.list_s)
        except ApiError as e:
            self.say(f"cannot read the series' closes: {e}")
            return
        self._series = {r.topic_id for r in rows}
        if not await self.sync(rows):
            return
        closes = [r for r in rows if r.quote_mode == "live" and r.end_time_utc - now >= self.cfg.stop_before_close_s]
        self._closes = closes[: self.cfg.markets] if self.cfg.markets else closes
        if not self._closes:
            self.say("no open close to trade")
            return
        await self._at(self._next(), self.feed)

    async def _at(self, row: MarketRow, feed) -> None:
        """The visit to one close: its book read now, the bot's picture of it from `feed`, then its trades."""
        try:
            book = self._paper_book(await self.api.book(row.topic_id))      # this close, read now
        except ApiError as e:
            self.say(f"{self.tag(row)}  cannot read its book: {e}")
            return
        if not book.bins or book.quote_mode != "live" or book.end_time_utc - time.time() < self.cfg.stop_before_close_s:
            self.say(f"{self.tag(book)}  closed to trading, skipped")
            return
        self._remember(book)
        began = time.monotonic()
        try:
            probs = await off_loop(self._draw, feed, book, self._shown_series(book))
        except Exception as e:
            self.say(f"{self.tag(book)}  no picture: {e}")
            return
        if self._stopping:
            return
        if len(probs) != len(book.bins) or abs(sum(probs) - 1) > 1e-6:
            self.say(f"{self.name}: forecast must return {len(book.bins)} probabilities summing to 1")
            return
        took = time.monotonic() - began
        self._round["visited"] += 1
        traded = await self._visit(Picture(book, probs, best_edge(book, probs), PL.gap(book, probs)), took)
        if traded and self.live and self.on_trade:
            self.on_trade()
        self.ledger.save()

    def _draw(self, feed, book: Book, series=()) -> list[float]:
        """The bot's picture of one close. Runs in a runner thread: a model that simulates paths takes seconds."""
        return [float(x) for x in self.bot.probs(book, feed.ctx(book.bins, book.end_time_utc), series)]

    def _remember(self, book: Book) -> None:
        """The close as the bot saw it on this visit: what a bot that reads the series is shown for it until the next."""
        self._shown[book.topic_id] = (time.time(), list(book.shares))

    def _shown_series(self, book: Book) -> tuple:
        """The series as this bot sees the site drawing it, for a bot that reads every close (`shown_series`): the close
        being visited from its book; every other open close from this runner's latest read of it (the paper book, so a
        paper bot's own fills count) where that is fresher than the series listing, else from the listing with the paper
        fills laid over. A bot that reads only its close is handed nothing."""
        if not self.bot.reads_series:
            return ()
        listed_at = self.api.__dict__.get("_bot_snapshots", {}).get(self.batch_id, (0.0,))[0]
        out = []
        for r in self._closes:
            if r.topic_id == book.topic_id:
                out.append((book.end_time_utc, book.bins, book.shares))
                continue
            at, q = self._shown.get(r.topic_id, (0.0, None))
            if q is None or at < listed_at:
                q = list(r.shares)
                if not self.live:
                    held = {o: n for o, (n, _) in self.ledger.legs(self.key, self.mode, r.topic_id).items()}
                    for o, n in self._seed.get(r.topic_id, {}).items():
                        held[o] = held.get(o, 0.0) - n
                    if held:
                        index = {o: i for i, o in enumerate(list(r.option_ids) or range(1, len(q) + 1))}
                        for o, n in held.items():
                            if o in index:
                                q[index[o]] = max(q[index[o]] + n, 0.0)
            bins = book.bins if len(q) == len(book.bins) else [parse_bin(n) for n in r.names]
            out.append((r.end_time_utc, bins, q))
        return shown_series(out)

    def _next(self) -> MarketRow:
        """The close after the one visited last, by close time. Past the farthest the round ends and the next one starts
        from the nearest. A close listed since the round began joins it at the far end; one that has closed is skipped."""
        ahead = [r for r in self._closes if (r.end_time_utc, r.topic_id) > self._cursor]
        if not ahead:
            self._finish_round()
            ahead = self._closes
        row = ahead[0]
        self._cursor = (row.end_time_utc, row.topic_id)
        self.step, self.of = self._closes.index(row) + 1, len(self._closes)
        return row

    def _finish_round(self) -> None:
        r, self.rounds = self._round, self.rounds + 1
        held, took = self.holdings(), int(time.time() - r["at"])
        span = f"{took // 3600}h {took % 3600 // 60:02d}m" if took >= 3600 else f"{took // 60}m {took % 60:02d}s"
        self.say(f"round {self.rounds} done in {span}: {r['visited']} closes visited, "
                 + (f"bought on {r['bought']} for {fmt.sats(r['paid'])}, " if self.buys else "")
                 + f"sold on {r['sold_on']} for {fmt.sats(r['sold'])}"
                 + (f" ({fmt.sats(r['gain'], signed=True)} on cost)" if r["sold_on"] else "") + "  ·  "
                 f"holds {fmt.sats(sum(x[7] for x in held))} in {len({x[2] for x in held})} closes"
                 + (f", {fmt.sats(self.cfg.bankroll_sats / max(len(self._closes), 1))} a close" if self.buys else ""))
        self._round = _round()

    def _room(self, book: Book) -> tuple[float, float, float, str]:
        """(sats this visit may spend on the close, its even share, the capital sizing reads, why nothing if nothing).
        Each close's share is the budget over the closes in the round, so a round reaches every one of them. A manager's
        share is of all the capital it manages here: fresh sats left plus what the portfolio cost."""
        held_cost = self.ledger.open_cost(self.key, self.mode, book.topic_id)
        left = self.cfg.bankroll_sats - self.committed
        capital = max(left, 0.0) + self.open_cost if self.manage else self.cfg.bankroll_sats
        per = share(BotConfig(**{**asdict(self.cfg), "bankroll_sats": capital}), len(self._closes))
        hour = self.cfg.max_per_hour_sats - self.spent_last_hour()
        room = min(per - held_cost, left, hour)
        why = ("" if room >= 1 else
               (f"fresh sats spent ({fmt.sats(self.cfg.bankroll_sats)}), waiting to sell at a profit" if self.manage else
                f"budget fully deployed: {fmt.sats(self.open_cost)} held in this series, waiting for sales and settlement")
               if left < 1 else
               f"holds {fmt.sats(held_cost)} here, its full share of {fmt.sats(per)} a close" if per - held_cost < 1 else
               f"hourly cap of {fmt.sats(self.cfg.max_per_hour_sats)} reached")
        return room, per, capital, why

    async def _visit(self, pic: Picture, took: float) -> bool:
        """Sell what the market overpays on this close, then buy what the picture says is cheap; one line either way."""
        book, probs, traded = pic.book, pic.probs, False
        self._mark(pic)
        held = self.ledger.legs(self.key, self.mode, book.topic_id)
        pol = None if self.matching else self.bot.policy     # matching: every bot trades its forecast the same way
        if held and (self.manage or not (pol and pol.hold)):  # only a policy that says hold keeps what the market overpays
            sells = exits(book, probs, held, self.cfg)
            if sells and await self._exit(book, sells):
                traded = True
                try:                                            # the sale moved this book: buy against the book it left
                    book = self._paper_book(await self.api.book(book.topic_id))
                except ApiError as e:
                    self.say(f"{self.tag(book)}  cannot read its book again after the sale: {e}")
                    return traded
                self._remember(book)
                pic = Picture(book, probs, best_edge(book, probs), PL.gap(book, probs))
                held = self.ledger.legs(self.key, self.mode, book.topic_id)
        slow = f"  ·  picture took {took:.0f}s" if took >= 10 else ""
        room, per, capital, why = self._room(book)
        if why:
            self.say(f"{self.tag(book)}  {why}{slow}")
            return traded
        if self.matching and pic.gap < self.cfg.match_gap:
            self.say(f"{self.tag(book)}  the site shows its forecast already, {pic.gap:.0%} apart{slow}")
            return traded
        held_cost = self.ledger.open_cost(self.key, self.mode, book.topic_id)
        legs = buys(self.bot, self.cfg, book, probs, room, self.feed.spot, held, held_cost, capital)
        if not legs or sum(x.cost_sats for x in legs) < 1:
            part = f" with the {fmt.sats(room)} left of its {fmt.sats(per)} share" if held_cost >= 1 else ""
            self.say(f"{self.tag(book)}  " + (
                (f"{pic.gap:.0%} off its forecast, nothing worth buying{part}" if part else
                 f"{pic.gap:.0%} off its forecast, but no range sells under its value") if self.matching else
                "nothing it may buy under its rule" if pol else
                f"nothing {self.cfg.min_edge:.0%} under value: best edge {pic.edge:+.0%}") + slow)
            return traded
        return await self._enter(pic, legs, per) or traded

    def _mark(self, pic: Picture) -> None:
        """For every range the bot holds in this close: today's price, the picture's chance, and what the position would
        fetch sold back into the book now, after the exit fee. The last is the honest mark: the price alone overstates a
        big position in a thin book, since selling it walks the price back down."""
        held = self.ledger.legs(self.key, self.mode, pic.book.topic_id)
        if held:
            at = {o: i for i, o in enumerate(pic.book.option_ids)}
            q = PL.Quote(pic.book.shares, pic.book.alpha)
            for o, (n, _) in held.items():
                if o in at:
                    i = at[o]
                    n = min(n, pic.book.shares[i])
                    self.marks[(pic.book.topic_id, o)] = (pic.book.prices[i], pic.probs[i],
                                                          max(q.base - q.cost_after(i, -n), 0.0) * (1 - P.FEE))

    async def _exit(self, book: Book, sells: list[tuple[int, float, float]]) -> bool:
        """Every sale on this close in one request."""
        got, basis = sum(x[2] for x in sells), 0.0
        for i, contracts, proceeds in sells:
            held, cost = self.ledger.legs(self.key, self.mode, book.topic_id).get(book.option_ids[i], (contracts, 0.0))
            part = "" if contracts >= held - 0.005 else f" of {fmt.contracts(held)}"
            gain = proceeds - cost * contracts / held if held > 0 and cost > 0 else None
            basis += cost * contracts / held if held > 0 else 0.0
            self.say(f"{self.tag(book)}  {'SELL' if self.live else 'would sell'} {fmt.span(*book.bins[i])}  "
                     f"{fmt.contracts(contracts)}{part} for ₿{proceeds:,.0f}"
                     + ("" if gain is None else f"  {fmt.sats(gain, signed=True)} on cost"))
        if self.live:
            try:
                fills = await self.api.sell_many([(book.topic_id, [(book.option_ids[i], c) for i, c, _ in sells])])
            except ApiError as e:
                self.say(f"{self.tag(book)}  sell failed: {e}")
                if "may or may not" in str(e):
                    self.stop()
                return False
            if fills[0].error:
                self.say(f"{self.tag(book)}  sell rejected: {fills[0].error}")
                return False
            got = fills[0].cost_sats
        for i, contracts, _ in sells:
            self.ledger.reduce(self.key, self.mode, book.topic_id, book.option_ids[i], contracts)
        self.sold_sats += got
        self.gain_sats += got - basis
        self.trades += 1
        self._round["sold_on"] += 1
        self._round["sold"] += got
        self._round["gain"] += got - basis
        return True

    async def _enter(self, pic: Picture, legs: list[Leg], per: float) -> bool:
        """The buy on this close, in one request. Live, it is checked against the server's estimate first, and dropped if
        the book moved since it was read. On paper the local price is the fill."""
        book, probs = pic.book, pic.probs
        total = sum(x.cost_sats for x in legs)
        order = [(book.option_ids[x.index], x.contracts) for x in legs]
        if self.live:
            try:
                raw, fee = await self.api.estimate_legs(book.topic_id, order)
            except ApiError as e:
                self.say(f"{self.tag(book)}  no estimate: {e}")
                return False
        else:                           # a paper book carries the bot's paper holdings, which the server has never seen
            raw, fee = total / (1 + P.FEE), total * P.FEE / (1 + P.FEE)
        charge = raw + max(fee, P.MIN_FEE_SATS)
        if abs(charge - total) > self.cfg.estimate_tolerance * total + 5:
            self.say(f"{self.tag(book)}  book moved (local {total:,.0f} vs server {charge:,.0f}), skipped")
            return False
        if self.live:
            try:
                fill = (await self.api.buy_many([(book.topic_id, order)]))[0]
            except ApiError as e:
                self.say(f"{self.tag(book)}  buy failed: {e}")
                if "may or may not" in str(e):
                    self.stop()             # unknown fill state: a human should look before more orders go out
                return False
            if fill.error:
                self.say(f"{self.tag(book)}  buy rejected: {fill.error}")
                return False
            charge = fill.cost_sats + fill.fee_sats
        value = sum(probs[x.index] * PL.NET * x.contracts for x in legs)
        top = max(legs, key=lambda x: x.cost_sats)
        paid = [book.prices[x.index] for x in legs]
        moved = _after(book, legs)
        after = PL.gap(moved, probs) if self.matching else 0.0
        why = (f"  shown gap {pic.gap:.0%} → {after:.0%}" if self.matching else
               f"  at {min(paid):.1f}–{max(paid):.1f} sats" if self.bot.policy else "")
        self.say(f"{self.tag(book)}  {'BUY' if self.live else 'would buy'} {len(legs)} range{'' if len(legs) == 1 else 's'}  "
                 f"{fmt.sats(charge)} of its {fmt.sats(per)} share  worth {fmt.sats(value)} by its picture  "
                 f"top {fmt.span(*book.bins[top.index])}{why}")
        for x in legs:
            self.ledger.add(self.key, self.mode, book, self.feed.asset, x.index, x.contracts, charge * x.cost_sats / total)
        self._mark(Picture(moved, probs, 0.0))         # marked now, not a round from now
        self._remember(moved)
        self.spent.append((time.time(), charge))
        self.paid_sats += charge
        self.trades += 1
        self._round["bought"] += 1
        self._round["paid"] += charge
        return True


def _after(book: Book, legs: list[Leg]) -> Book:
    """The book once `legs` are bought: what the site will show next."""
    b = Book(book.topic_id, book.title, book.end_time_utc, book.quote_mode, book.volume_msat, list(book.option_ids),
             list(book.bins), list(book.shares), book.alpha)
    for x in legs:
        b.shares[x.index] += x.contracts
    b.reprice()
    return b


# ── the Profit Taker ────────────────────────────────────────

@dataclass
class ProfitTaker(Runner):
    """Takes profit on your whole portfolio and never buys. It reads every position your account holds, in every series
    and whoever opened it, and walks only the closes you hold something in, by close time across every series, nearest
    to farthest and round again. On each visit it reads that close's book fresh, draws the bot's picture of it, and sells
    the part of each position the market pays more for, after the exit fee, than the picture says it is worth (by
    `exit_edge`) and than it cost: `exits`, the same rule every bot sells by. When nothing sells it says how near the
    nearest position is. Build it with `batch_id=""` and `feed=None`: it keeps a feed per asset (`make_feed`)."""
    buys: ClassVar[bool] = False
    make_feed: Callable[[str], object] | None = None    # asset -> its Feed (the terminal passes its own, shared); else a new one
    batches: list[Batch] = field(default_factory=list)  # the series it checks; empty: every series with a price feed
    feeds: dict[str, object] = field(default_factory=dict)
    _where: dict[int, tuple[str, str]] = field(default_factory=dict)  # topic -> (asset, series name)
    _quiet: tuple[str, float] = ("", 0.0)               # the last idle line and when it was said

    def __post_init__(self) -> None:
        self.manage = True                      # it trades the account's own positions, as a portfolio manager does
        super().__post_init__()

    def _asset_of(self, topic_id: int) -> str:
        return self._where.get(topic_id, ("", ""))[0]

    def _label_of(self, topic_id: int) -> str:
        return self._where.get(topic_id, ("", ""))[1]

    def _intro(self) -> str:
        return (f"taking profit on your whole portfolio, every series: it sells the part of a position the market pays "
                f"more for than {self.bot.name}'s picture says it is worth"
                + (" and than it cost" if self.cfg.profit_only else "")
                + f", and never buys  ·  one close you hold every {self.cfg.interval_s:g}s, nearest to farthest, then round again")

    def _feed(self, asset: str):
        if asset not in self.feeds:
            if self.make_feed is None:
                from .zoo.data import Feed

                self.feeds[asset] = Feed(asset)
            else:
                self.feeds[asset] = self.make_feed(asset)
        return self.feeds[asset]

    def _idle(self, msg: str) -> None:
        """A line for a visit with nothing to visit: said once, then once a minute while it stays true."""
        if msg != self._quiet[0] or time.time() - self._quiet[1] >= 60:
            self.say(msg)
            self._quiet = (msg, time.time())

    async def _every_close(self) -> list[MarketRow]:
        """Every open close of every series it checks, each read again every `list_s` (shared with other runners)."""
        if not self.batches:
            self.batches = [b for b in await self.api.batches() if b.asset in PAIRS]
        got = await asyncio.gather(*(listing(self.api, b.batch_id, self.cfg.list_s) for b in self.batches))
        rows: list[MarketRow] = []
        for b, rs in zip(self.batches, got, strict=True):
            for r in rs:
                self._where[r.topic_id] = (b.asset, b.short)
            rows += rs
        return rows

    async def cycle(self) -> None:
        """One visit: the next close you hold a position in, across every series, by close time and round again."""
        self.cycles += 1
        now = time.time()
        self.ledger.prune(now)
        try:
            rows = await self._every_close()
        except ApiError as e:
            self.say(f"cannot read the open closes: {e}")
            return
        self._series = {r.topic_id for r in rows}
        if not await self.sync(rows):
            return
        held = {x[2] for x in self.holdings()}
        self._closes = sorted((r for r in rows if r.topic_id in held and r.quote_mode == "live"
                               and r.end_time_utc - now >= self.cfg.stop_before_close_s), key=lambda r: (r.end_time_utc, r.topic_id))
        if not self._closes:
            self._idle("nothing to sell: you hold no position in a close still open to trading" if not held else
                       f"nothing to sell: your {len(held)} close{'' if len(held) == 1 else 's'} with positions "
                       f"{'is' if len(held) == 1 else 'are'} about to close; the server settles them")
            return
        self._quiet = ("", 0.0)
        row = self._next()
        asset = self._asset_of(row.topic_id)
        feed = self._feed(asset)
        await feed.refresh(max_age=max(self.cfg.interval_s, 1.0))
        if not feed.ready:
            self.say(f"{self.tag(row)}  no {asset} price data yet, skipped")
            return
        await self._at(row, feed)

    async def _visit(self, pic: Picture, took: float) -> bool:
        """Sell what the market overpays on this close at a profit; otherwise say how near the nearest position is."""
        self._mark(pic)
        book, probs = pic.book, pic.probs
        held = self.ledger.legs(self.key, self.mode, book.topic_id)
        sells = exits(book, probs, held, self.cfg)
        if sells:
            return await self._exit(book, sells)
        slow = f"  ·  picture took {took:.0f}s" if took >= 10 else ""
        self.say(f"{self.tag(book)}  {self._nearest(book, probs, held)}{slow}")
        return False

    def _nearest(self, book: Book, probs: list[float], held: dict[int, tuple[float, float]]) -> str:
        """Why nothing sold here: of the ranges held, the one closest to selling, what its next contract fetches after
        the exit fee and what it would have to fetch (its worth by the picture plus `exit_edge`, or its cost)."""
        at = {o: i for i, o in enumerate(book.option_ids)}
        best = None
        for o, (n, cost) in held.items():
            i = at.get(o)
            if i is None or n <= 0:
                continue
            fetch = (1 - P.FEE) * book.prices[i]
            worth = probs[i] * PL.NET * (1 + self.cfg.exit_edge)
            paid = cost / n if self.cfg.profit_only else 0.0
            need = max(worth, paid, 1e-9)
            if best is None or fetch / need > best[0]:
                best = (fetch / need, i, fetch, need, "its cost" if paid >= worth else "its worth")
        if best is None:
            return "nothing held here any more"
        _, i, fetch, need, what = best
        k = len(held)
        return (f"nothing to sell: {k} range{'' if k == 1 else 's'} held, nearest {fmt.span(*book.bins[i])} fetches "
                f"{fetch:.2f} a contract, must beat {need:.2f} ({what})")
