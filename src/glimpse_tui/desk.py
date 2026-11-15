"""The Desk: one bot across every series at once, trading on every update, sized by fractional Kelly against the wallet.

The bots screen's Runner walks a series one close at a time, every five seconds, nearest to farthest: a week of hourly
closes takes fourteen minutes a round, the list of closes is read again every five minutes, and the budget is a number
unrelated to what the account holds. The Desk is the same bot run the way a trading desk would run it:

- One view of the whole market. A `watch.Watch` keeps every open close of every series current: Glimpse streams every
  trade of every market over one WebSocket (each message carries the whole book of the close that traded), and the
  series are listed again every minute to reconcile, and the end of each list is probed every second around the top of
  the hour, when the new hourly closes are listed and seeded flat.
- A reaction to every update. When a trade moves a book, the Desk re-plans that close within a second against the bot's
  fresh picture: it sells what the trade bid up past value, at a profit, and buys what the trade left under value. When a
  close is newly listed it is planned at once, while the book is still the flat seed. When a series' spot moves, every
  close of the series is redrawn and re-planned, and every close is re-planned every `sweep_s` seconds whatever happens.
- Fractional Kelly on the wallet. Live, the bankroll is the wallet's balance plus what the Desk holds at cost, read every
  `wallet_s` seconds and after every fill; on paper it is the budget. Less a reserve, it is shared out over the open
  closes of the series traded (an even share by default, `horizon_power` tilts it to the near ones), and on each close
  the buy is the exact growth-optimal position for mutually exclusive outcomes on the LS-LMSR (`kelly.kelly_buys`),
  taken at the Kelly fraction, with no contract ever bought past the price at which its edge after both fees falls under
  `min_edge`. Full Kelly with deep capital would move every range it buys to the model's own probability, so the market
  would show the bot's forecast; the fraction and the capital say how far toward that it goes.
- Sideways at the weekend. In the hours the calendar calls quiet (Saturday and Sunday for Bitcoin, Ethereum and Solana;
  CME's closed hours for gold and oil) the volatility clock already narrows every picture; the Desk also pools the bot's
  picture toward the clock's own driftless walk by the share of busy hours left to the close (`weekend_lean` keeps a part
  of the lean), so whatever model is running bets that the price stays where it is.

Every order is checked against the server's estimate first and dropped if the book moved; every close's orders go out
in one request, and every close planned in a tick shares one request. The ledger is the bots' (`bots.Ledger`, the same
key as the Runner's, so a position the Runner opened is managed here too). Dry run by default: a paper Desk keeps a paper
ledger, lays its paper fills over the books it sees, and settles them against the price feed when a close ends.

A live Desk is built to stay up through a slow or absent Glimpse, because a desk that waits on one request trades nothing:

- No request holds a tick. A book read, an estimate and the wallet are asked once and given `read_timeout_s` to answer; an
  order and the account's positions are given `order_timeout_s`. A close whose read went unanswered is queued again.
  (The client's own three tries of 30 s each once held a tick for minutes on six closes. Measured on 14 November 2026 a
  read takes 0.7 s on a warm connection and about 3 s on a new one, 5.4 s at worst.)
- When Glimpse stops answering (`trip_after` requests in a row, or one order) the desk holds its orders instead of sending
  more into the dark, asks again after `probe_s` seconds, then twice as long each time up to `probe_max_s`, and re-plans
  every close the moment an answer comes. What it asks is a read (an estimate, a book), never an order. Two lines in the
  log, one when it goes and one when it is back.
- An order whose answer never came may or may not have filled. The desk does not stop for it: it leaves those closes alone,
  reads the account's positions, books the order if its contracts are there and plans it again if they are not (`_settle`).
- Each of its loops is restarted if it ever raises, so one bad answer cannot take the desk down. Only `stop()` ends it, or
  a service that refuses the account outright.
"""
from __future__ import annotations

import asyncio
import time
import tomllib
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields

import numpy as np

from . import fmt
from . import policy as PL
from . import pricing as P
from .api import Ambiguous, ApiError, Batch, Book, Glimpse, NoServiceAuth, Position, Transient
from .auth import config_dir
from .bots import PORTFOLIO, Bot, Ledger, ctx_of, off_loop, shown_series
from .policy import Leg
from .watch import Event, Watch

PRICE_SERIES = ("BTC", "BTC 1D", "ETH", "SOL", "XAU", "XAU 1D", "XAUTBTC", "WTIBTC")   # every series with a price feed and a walk
MAX_ESTIMATES = 8                   # pre-trade estimates in flight at once (the public client allows 240 a minute)
ORDER_CHUNK = 12                    # markets in one enter or exit request
SLOW_TICK_S = 30.0                  # a tick that took this long is said in the log: nothing else traded while it ran
RESTART_S = (1.0, 30.0)             # a loop that raised is started again after this long, doubled each time up to this


@dataclass(frozen=True)
class DeskConfig:
    """How the Desk trades. Read from a `[desk]` table in ~/.config/glimpse/bots.toml; the CLI's flags lay over it."""
    kelly: float = 0.5               # fraction of the Kelly optimum bought (1 is full Kelly; 0.5 half)
    min_edge: float = 0.02           # the last contract of every leg must keep this return on cost after both fees
    exit_edge: float = 0.02          # sell a position while the market pays this much more than the picture says it is worth
    profit_only: bool = True         # ...and only at a gain over what it cost
    reserve: float = 0.10            # share of the bankroll kept out of the market: fees, and room for the next launch
    horizon_power: float = 0.0       # a close's share of the bankroll ∝ hours^−power; 0 is an even share
    series_split: str = "closes"     # closes: one share per tradable close, whatever its series; equal: each series gets the same
    max_hours: float = 720.0         # closes further out than this are watched, not traded: capital would sit for months
    series: str = "all"              # the series traded: all (every price series), or a comma list like "BTC,XAU"
    wallet_s: float = 30.0           # live: how often the wallet balance is read (and after every fill)
    refresh_s: float = 15.0          # a series' pictures are redrawn at least this often...
    respot: float = 0.0005           # ...and whenever its spot has moved this much (a fraction) since they were drawn
    sweep_s: float = 20.0            # every tradable close is re-planned at least this often
    settle_s: float = 1.0            # a close is re-planned at most once in this many seconds (its updates are coalesced)
    list_s: float = 60.0             # the watch lists each series again this often
    max_book_age: float = 20.0       # a book not confirmed by the stream or a listing for longer is read again before a buy
    estimate_tolerance: float = 0.005
    stop_before_close_s: int = 60
    hour_turnover: float = 3.0       # a fuse: most bought in one hour, as a multiple of the bankroll (sales recycle it)
    min_gain_sats: float = 10.0      # the least expected profit, after both fees and the 1 sat minimum, an order must promise
    rebalance_share: float = 0.05    # a top-up on a close already held must promise this share of the close's cost at hand in profit
    max_orders_tick: int = 48        # most closes bought on in one tick; the rest stay queued for the next
    weekend_lean: float = 0.0        # how much of the bot's directional lean survives a quiet hour (0: sideways)
    portfolio_s: float = 60.0        # managing: how often the account's positions are read
    read_timeout_s: float = 10.0     # live: a book read, an estimate or the wallet is asked once and given this long to answer
    order_timeout_s: float = 30.0    # live: an order, or the account's positions, is given this long; an order past it is unanswered
    trip_after: int = 3              # requests in a row Glimpse does not answer before the desk holds its orders
    probe_s: float = 2.0             # holding: the wait before asking again, doubled each time it still does not answer...
    probe_max_s: float = 30.0        # ...up to this

    @classmethod
    def load(cls, **override) -> DeskConfig:
        f = config_dir() / "bots.toml"
        raw = tomllib.loads(f.read_text()).get("desk", {}) if f.is_file() else {}
        known = {x.name for x in fields(cls)}
        return cls(**{k: v for k, v in {**raw, **override}.items() if k in known and v is not None})

    def with_(self, **kw) -> DeskConfig:
        return DeskConfig(**{**asdict(self), **{k: v for k, v in kw.items() if v is not None}})

    def series_list(self) -> list[str]:
        if self.series.strip().lower() in ("", "all"):
            return list(PRICE_SERIES)
        return [s.strip() for s in self.series.split(",") if s.strip()]


@dataclass
class Pic:
    """The bot's picture of one close as the Desk last drew it."""
    probs: list[float]
    at: float
    spot: float
    lean_scale: float = 1.0


@dataclass
class Plan:
    """What one tick decided for one close."""
    book: Book
    probs: list[float]
    sells: list[tuple[int, float, float]] = field(default_factory=list)     # (range, contracts, proceeds)
    buys: list[Leg] = field(default_factory=list)
    free: float = 0.0
    why: str = ""                                                            # when nothing is bought: the reason


@dataclass
class Unsure:
    """An order that was sent and never answered: the connection dropped, no reply came in time, or the reply was a server
    error. It may or may not have filled, and its close is left alone until the account says which."""
    side: str                        # "buy" or "sell"
    batch: Batch
    plan: Plan
    sats: float                      # a buy: its charge as the server's estimate confirmed it; a sale: its planned proceeds


def lasted(seconds: float) -> str:
    """`45 s`, `3m 10s`, `2h 05m`: how long an outage ran."""
    n = int(max(seconds, 0))
    return f"{n} s" if n < 60 else f"{n // 60}m {n % 60:02d}s" if n < 3600 else f"{n // 3600}h {n % 3600 // 60:02d}m"


def sideways(probs, base, scale: float) -> list[float]:
    """The picture pooled toward the clock's driftless walk in log space, p ∝ probs^scale · base^(1−scale): at scale 1 the
    bot's own picture, at 0 the baseline, so a close whose remaining hours are all quiet is bet as standing still."""
    if scale >= 1.0 - 1e-9:
        return list(probs)
    lp = np.log(np.maximum(np.asarray(probs, dtype=float), 1e-300))
    lb = np.log(np.maximum(np.asarray(base, dtype=float), 1e-300))
    lw = scale * lp + (1.0 - scale) * lb
    out = np.exp(lw - lw.max())
    out = np.maximum(out / out.sum(), 1e-12)
    return (out / out.sum()).tolist()


@dataclass
class Desk:
    """One bot, every series, every update. Build it with the batches to trade; `start()` runs it until `stop()`."""
    bot: Bot
    api: Glimpse
    batches: list[Batch]
    live: bool = False
    budget: float = 100_000.0        # paper: the bankroll; live: a cap on the wallet's balance as the bankroll (0: none)
    cfg: DeskConfig = field(default_factory=DeskConfig.load)
    ledger: Ledger = field(default_factory=Ledger)
    manage: bool = False             # trade the account's whole portfolio in these series, not only what the Desk opened
    make_feed: Callable[[str], object] | None = None
    on_log: Callable[[str], None] | None = None
    on_trade: Callable[[], None] | None = None
    watch: Watch | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=600))
    # state
    feeds: dict[str, object] = field(default_factory=dict)
    pictures: dict[int, Pic] = field(default_factory=dict)
    drawn: dict[str, tuple[float, float]] = field(default_factory=dict)      # batch id -> (spot, when) of its last series pass
    balance: float = 0.0             # live: the wallet's free balance when last read
    balance_at: float = 0.0
    marks: dict[tuple[int, int], tuple[float, float, float]] = field(default_factory=dict)
    spent: deque[tuple[float, float]] = field(default_factory=deque)
    trades: int = 0
    paid_sats: float = 0.0
    sold_sats: float = 0.0
    gain_sats: float = 0.0
    settled_sats: float = 0.0        # paper: what settled closes paid out over what their positions cost
    launches: int = 0                # closes planned while still at their seed
    skipped_small: int = 0           # plans left unsent because they fell under the dead-band (min_gain_sats, rebalance_share)
    ticks: int = 0
    outages: int = 0                 # times Glimpse stopped answering and the desk held its orders until it did
    unanswered: int = 0              # orders sent and never answered, settled against the account
    started_at: float = 0.0
    _task: asyncio.Task | None = None
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _stopping: bool = False
    _dirty: dict[int, float] = field(default_factory=dict)                   # topic -> when it was marked
    _urgent: set[int] = field(default_factory=set)                            # launches: planned without waiting to settle
    _launched: set[int] = field(default_factory=set)                          # launches not yet bought on: the first buy says so
    _planned_at: dict[int, float] = field(default_factory=dict)
    _small: set[int] = field(default_factory=set)                            # closes whose last plan fell under the dead-band
    _seed: dict[int, dict[int, float]] = field(default_factory=dict)         # managing on paper: the account's own shares
    _synced_at: float = 0.0
    _saved_at: float = 0.0
    _quiet: tuple[str, float] = ("", 0.0)
    _fails: int = 0                                                           # requests in a row that Glimpse did not answer
    _down_at: float = 0.0                                                     # when it stopped answering; 0 while it answers
    _probe_at: float = 0.0                                                    # holding: when it is asked again
    _probe_wait: float = 0.0
    _unsure: dict[int, Unsure] = field(default_factory=dict)                 # topic -> the order there that was never answered
    _settle_at: float = 0.0                                                   # when the account is next read to settle them
    _settle_wait: float = 0.0
    _sync_at: float = 0.0                                                     # managing: a failed portfolio read is not repeated before this
    _sync_wait: float = 0.0

    # ── identity ────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.bot.name

    @property
    def mode(self) -> str:
        return "live" if self.live else "paper"

    @property
    def key(self) -> str:
        return PORTFOLIO if self.manage else self.bot.id

    @property
    def series(self) -> str:
        return ", ".join(b.short for b in self.batches)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def batch_id(self) -> str:
        """The bots screen asks every runner which series it is bound to; a desk is bound to none."""
        return ""

    @property
    def cycles(self) -> int:
        """The bots screen keeps a stopped runner's log while it has made visits; a desk's visits are its ticks."""
        return self.ticks

    def say(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S', time.gmtime())}  {msg}"
        self.log.append(line)
        if self.on_log:
            self.on_log(line)

    def _idle(self, msg: str) -> None:
        if msg != self._quiet[0] or time.time() - self._quiet[1] >= 60:
            self.say(msg)
            self._quiet = (msg, time.time())

    # ── the bankroll ────────────────────────────────────────

    @property
    def topics(self) -> set[int]:
        """Every close the Desk watches."""
        return set(self.watch.books) if self.watch else set()

    @property
    def open_cost(self) -> float:
        return self.ledger.open_cost(self.key, self.mode, among=self.topics or None)

    def holdings(self) -> list[tuple[int, str, int, int, float, float, float, float]]:
        return self.ledger.holdings(self.key, self.mode, self.topics or None)

    @property
    def bankroll(self) -> float:
        """Live: the wallet's free balance plus what the Desk holds at cost, capped by the budget if one was given. Paper:
        the budget, plus what paper settlements have paid."""
        if self.live:
            b = self.balance + self.open_cost
            return min(b, self.budget) if self.budget > 0 else b
        return max(self.budget + self.settled_sats, 0.0)

    @property
    def deployable(self) -> float:
        return self.bankroll * (1.0 - self.cfg.reserve)

    def spent_last_hour(self) -> float:
        cutoff = time.time() - 3600
        while self.spent and self.spent[0][0] < cutoff:
            self.spent.popleft()
        return sum(s for _, s in self.spent)

    async def refresh_balance(self) -> None:
        """Live: the wallet's balance, asked once. While it cannot be read the last balance stands."""
        if not self.live:
            return
        try:
            w = await self._ask(self.api.wallet(), trips=False)
        except ApiError as e:
            if not self._down_at:
                self._idle(f"cannot read the wallet: {e}")
            return
        self.balance, self.balance_at = w.balance_sats, time.time()

    # ── staying up ──────────────────────────────────────────

    @property
    def holding(self) -> bool:
        """Glimpse has stopped answering and the next attempt is not due: until it is, nothing is asked and no order is sent."""
        return self._down_at > 0 and time.time() < self._probe_at

    @property
    def link(self) -> str:
        """`stream up`, `polling`, or how long Glimpse has not answered."""
        if self.live and self._down_at:
            return f"Glimpse not answering for {lasted(time.time() - self._down_at)}, orders held"
        return "stream up" if self.watch and self.watch.streaming else "polling"

    def _net_ok(self) -> None:
        """Glimpse answered: if the desk was holding, it trades again and every close is planned afresh."""
        if self._down_at:
            self.say(f"Glimpse is answering again after {lasted(time.time() - self._down_at)}: every close re-planned")
            self._down_at = 0.0
            self._mark_all()
        self._fails, self._probe_wait = 0, 0.0

    def _net_fail(self, e: ApiError, at_once: bool = False) -> None:
        """Glimpse did not answer a request. After `trip_after` in a row (or one order, `at_once`) the desk holds: nothing more
        is sent until `probe_s` has passed, and each attempt that still gets no answer doubles the wait, up to `probe_max_s`."""
        now = time.time()
        self._fails += 1
        if self._down_at:
            if now >= self._probe_at:           # asked again, and still nothing
                self._probe_wait = min(max(self._probe_wait * 2, self.cfg.probe_s), self.cfg.probe_max_s)
                self._probe_at = now + self._probe_wait
            return
        if at_once or self._fails >= self.cfg.trip_after:
            self._down_at, self._probe_wait = now, self.cfg.probe_s
            self._probe_at = now + self._probe_wait
            self.outages += 1
            self.say(f"Glimpse is not answering ({str(e).rstrip('.')}): orders held, asking again every few seconds until it does")

    async def _ask(self, request, timeout: float | None = None, trips: bool = True):
        """One read with a deadline: its answer, or `Transient` if none came within `timeout` seconds (`read_timeout_s`), the
        client's own retries included, or if what came could not be read. Any answer, a refusal too, says Glimpse is up; with
        `trips`, none counts toward holding. Nothing but `ApiError` leaves here, so a bad answer is a line in the log."""
        limit = self.cfg.read_timeout_s if timeout is None else timeout
        try:
            out = await asyncio.wait_for(request, limit)
        except TimeoutError:
            e = Transient(f"no answer in {limit:g} s")
        except Transient as x:
            e = x
        except ApiError:
            self._net_ok()
            raise
        except Exception as x:
            e = Transient(f"an answer it could not read, {type(x).__name__}: {x}")
        else:
            self._net_ok()
            return out
        if trips:
            self._net_fail(e)
        raise e

    async def _order(self, request):
        """An order sent with a deadline of `order_timeout_s`. A refusal is an answer (`ApiError`), and so is being told to
        slow down. Anything else that is not a fill leaves it unknown whether the order went through, and is `Ambiguous`: no
        answer in time, a dropped connection, a server error, or an answer that cannot be read."""
        limit = self.cfg.order_timeout_s
        try:
            out = await asyncio.wait_for(request, limit)
        except TimeoutError:
            raise Ambiguous(f"no answer in {limit:g} s") from None
        except Ambiguous:
            raise Ambiguous("the connection dropped") from None
        except Transient as e:
            if e.status == 429:
                self._net_fail(e)
                raise
            raise Ambiguous(str(e).rstrip(".")) from None
        except ApiError:
            self._net_ok()
            raise
        except Exception as e:
            raise Ambiguous(f"an answer it could not read, {type(e).__name__}: {e}") from None
        self._net_ok()
        return out

    def _requeue(self, topics) -> None:
        """These closes are planned again on the next tick (once Glimpse answers, if the desk is holding)."""
        now = time.time()
        for t in topics:
            self._dirty[t] = now

    def _unanswered(self, side: str, items: list[tuple[Batch, Plan, float]], e: ApiError) -> None:
        """An order went out and no answer came back. Its closes are left alone until the account says whether it filled
        (`_settle`), and the desk holds at once: a connection that lost one order is not one to send the next through."""
        for b, p, sats in items:
            self._unsure[p.book.topic_id] = Unsure(side, b, p, sats)
        self.unanswered += 1
        self._settle_at = 0.0
        one = len(items) == 1
        self.say(f"{side} on {len(items)} close{'' if one else 's'} got no answer ({e}): {'it' if one else 'they'} may or may not "
                 f"have filled, so {'that close waits' if one else 'those closes wait'} until the account says which")
        self._net_fail(e, at_once=True)

    def _believed(self, topic_id: int, option_id: int) -> float:
        """The contracts the account is believed to hold on one range, before an unanswered order: managing, the portfolio as
        last read and traded since; otherwise what every bot on this machine holds there with real sats, by the ledger."""
        owners = [PORTFOLIO] if self.manage else [k for k in list(self.ledger.data) if k != PORTFOLIO]
        return sum(self.ledger.legs(k, "live", topic_id).get(option_id, (0.0, 0.0))[0] for k in owners)

    async def _settle(self) -> None:
        """Every order that was sent and never answered, settled against the account. The account's positions are read, and
        on each order's ranges what it holds now is set against what it was believed to hold before: where the contracts of
        a buy have arrived the buy filled, and is booked at the charge the server's estimate confirmed; where the contracts
        of a sale have gone it filled, and is booked at its planned proceeds; otherwise the order never happened and its
        close is planned again. An order counts as filled when at least half its contracts moved. Until the account can be
        read those closes are left alone, and it is asked again after `probe_s`, doubling to `probe_max_s`."""
        now = time.time()
        for t in [t for t, u in self._unsure.items() if u.plan.book.end_time_utc <= now]:
            del self._unsure[t]                 # the close ended: the server settles whatever the account holds there
        if not self._unsure or now < self._settle_at:
            return
        n = len(self._unsure)
        try:
            held = await self._ask(self.api.positions(), self.cfg.order_timeout_s)
        except ApiError as e:
            self._settle_wait = min(max(self._settle_wait * 2, self.cfg.probe_s), self.cfg.probe_max_s)
            self._settle_at = time.time() + self._settle_wait
            if not self._down_at:
                self._idle(f"cannot read the account to settle {n} unanswered order{'' if n == 1 else 's'}: {e}")
            return
        self._settle_wait = 0.0
        have: dict[tuple[int, int], float] = {}
        for x in held:
            have[(x.topic_id, x.option_id)] = have.get((x.topic_id, x.option_id), 0.0) + x.shares
        for t, u in list(self._unsure.items()):
            del self._unsure[t]
            book, sign = u.plan.book, 1.0 if u.side == "buy" else -1.0
            legs = ([(book.option_ids[x.index], x.contracts) for x in u.plan.buys] if u.side == "buy" else
                    [(book.option_ids[i], c) for i, c, _ in u.plan.sells])
            moved = sum(min(max(sign * (have.get((t, o), 0.0) - self._believed(t, o)), 0.0), c) for o, c in legs)
            tag = fmt.question(u.batch.short, book.end_time_utc)[:-4]
            if moved < 0.5 * sum(c for _, c in legs):
                self.say(f"{tag}  the unanswered {u.side} did not fill: planned again")
            elif u.side == "buy":
                self.say(f"{tag}  the unanswered buy did fill: booked at the {fmt.sats(u.sats)} the server's estimate confirmed")
                self._book(u.batch, u.plan, u.sats, u.sats)
            else:
                self.say(f"{tag}  the unanswered sell did fill: booked at its planned {fmt.sats(u.sats)}")
                self._book_sale(u.plan, u.sats)
            self._dirty[t] = time.time()
        if self.manage:
            self._adopt(held)                   # the account's own record wins over what was booked
        self.ledger.save()
        self._saved_at = time.time()
        if self.on_trade:
            self.on_trade()

    def tradable(self, batch: Batch, now: float | None = None) -> list[Book]:
        """The batch's closes the Desk may trade now: open, not about to close, within max_hours."""
        now = time.time() if now is None else now
        out = self.watch.open_closes(batch.batch_id, now, self.cfg.stop_before_close_s) if self.watch else []
        return [b for b in out if b.end_time_utc - now <= self.cfg.max_hours * 3600]

    def allotments(self, now: float | None = None) -> dict[int, float]:
        """Each tradable close's share of the deployable bankroll. By default every tradable close gets one share whatever
        its series (`series_split = "closes"`: the hourly series, with the most closes, the fastest settlement and a launch
        every hour, carry most of the bankroll); `"equal"` gives each series the same and shares it over its closes. Within
        that, `kelly.allot` weights by horizon."""
        from .kelly import allot

        now = time.time() if now is None else now
        per = {b.batch_id: self.tradable(b, now) for b in self.batches}
        total = sum(len(v) for v in per.values())
        out: dict[int, float] = {}
        for b in self.batches:
            closes = per[b.batch_id]
            if not closes:
                continue
            pot = (self.deployable / max(len(self.batches), 1) if self.cfg.series_split == "equal"
                   else self.deployable * len(closes) / max(total, 1))
            hours = [max((c.end_time_utc - now) / 3600.0, 1 / 60) for c in closes]
            for c, a in zip(closes, allot(pot, hours, self.cfg.horizon_power), strict=True):
                out[c.topic_id] = a
        return out

    # ── lifecycle ───────────────────────────────────────────

    def start(self) -> None:
        if not self.running:
            self._stopping = False
            self.started_at = time.time()
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        if self._task:
            self._task.cancel()
            self.say("stopped")

    async def wait(self) -> None:
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _intro(self) -> str:
        return (f"{'LIVE' if self.live else 'dry run: paper orders never reach the market'}  ·  {self.series}  ·  bankroll "
                f"{fmt.sats(self.bankroll)}{' (the wallet)' if self.live else ''}, {self.cfg.reserve:.0%} kept back, the rest shared over "
                f"every open close within {self.cfg.max_hours:g} h  ·  Kelly {self.cfg.kelly:g}, every contract {self.cfg.min_edge:.0%} "
                f"under value after fees  ·  re-plans a close on every trade in it, every launch at once, every close every "
                f"{self.cfg.sweep_s:g} s  ·  sells what the market overpays by {self.cfg.exit_edge:.0%}"
                + (" at a profit" if self.cfg.profit_only else "") + "  ·  quiet hours bet sideways")

    def _feed(self, asset: str):
        if asset not in self.feeds:
            if self.make_feed is None:
                from .zoo.data import Feed

                self.feeds[asset] = Feed(asset)
            else:
                self.feeds[asset] = self.make_feed(asset)
        return self.feeds[asset]

    async def _open_watch(self) -> None:
        """The watch, built if none was given and started if it has not listed the series yet."""
        if self.watch is None:
            self.watch = Watch(self.api, self.batches, list_s=self.cfg.list_s)     # its events are drained from the queue
        if not self.watch.listed_at:
            await self.watch.start()

    def _set_lean(self) -> None:
        """The zoo's quiet-hour lean follows the desk's: `Ctx.lean_scale` reads the module constant on every picture."""
        from .zoo import core

        core.WEEKEND_LEAN = float(self.cfg.weekend_lean)

    async def _keep(self, name: str, loop) -> None:
        """One of the desk's loops, for as long as the desk runs: if it ever raises, that is one line in the log and the loop
        starts again, after a second at first and up to half a minute if it keeps failing. Only `stop()` ends it."""
        wait = RESTART_S[0]
        while not self._stopping:
            began = time.monotonic()
            try:
                await loop()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if time.monotonic() - began > 60:
                    wait = RESTART_S[0]             # it ran a while before this: not a loop that fails as it starts
                self.say(f"{name} failed, {type(e).__name__}: {e}; starting it again in {wait:g} s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, RESTART_S[1])

    async def _run(self) -> None:
        self._set_lean()
        wait = RESTART_S[0]
        while True:                             # a desk started while Glimpse is away waits for it; it does not give up
            try:
                await self._open_watch()
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.say(f"cannot list the series, {type(e).__name__}: {e}; trying again in {wait:g} s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, RESTART_S[1])
        for b in self.batches:
            self._feed(b.asset or "BTC")
        await asyncio.gather(*(f.refresh(max_age=1.0) for f in self.feeds.values()), return_exceptions=True)
        await self.refresh_balance()
        self.say(f"started  {self._intro()}")
        self._mark_all()
        self._tasks = [asyncio.create_task(self._keep(name, loop)) for name, loop in (
            ("the trading loop", self._events), ("the price feeds", self._feeds_loop), ("the sweep", self._sweeps),
            ("the wallet loop", self._wallet_loop))]
        try:
            await asyncio.gather(*self._tasks)
        finally:
            for t in self._tasks:
                t.cancel()
            await self.watch.stop()

    async def once(self) -> None:
        """One pass: list every series, draw every picture, plan and trade every tradable close, then stop."""
        self._set_lean()
        await self._open_watch()
        for b in self.batches:
            self._feed(b.asset or "BTC")
        await asyncio.gather(*(f.refresh(max_age=1.0) for f in self.feeds.values()), return_exceptions=True)
        await self.refresh_balance()
        self.say(f"one pass  {self._intro()}")
        self._mark_all()
        await self.tick(force=True)
        for _ in range(100):                    # the per-tick cap re-queues the rest: keep going until every close has had its turn
            if not self._due():
                break
            await self.tick()
        await self.watch.stop()

    # ── events ──────────────────────────────────────────────

    def _on_event(self, ev: Event) -> None:
        if ev.kind == "book":
            self._dirty.setdefault(ev.topic_id, ev.at or time.time())
        elif ev.kind == "launch":
            self._dirty[ev.topic_id] = ev.at or time.time()
            self._urgent.add(ev.topic_id)
            self._launched.add(ev.topic_id)
        elif ev.kind == "list":
            pass
        elif ev.kind == "stream":
            self.say("stream connected" if self.watch and self.watch.streaming else "stream dropped: polling until it is back")

    def _mark_all(self, batch_id: str | None = None) -> None:
        for b in self.batches:
            if batch_id is None or b.batch_id == batch_id:
                for c in self.tradable(b):
                    self._dirty.setdefault(c.topic_id, time.time())

    async def _events(self) -> None:
        """Consume the watch's events and run a tick whenever a dirty close is due."""
        while not self._stopping:
            try:
                ev = await asyncio.wait_for(self.watch.events.get(), timeout=0.25)
                self._on_event(ev)
                while not self.watch.events.empty():
                    self._on_event(self.watch.events.get_nowait())
            except TimeoutError:
                pass
            if self._due() or self._owed():
                began = time.monotonic()
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:                  # one bad tick must not stop the desk
                    self.say(f"tick failed: {type(e).__name__}: {e}")
                took = time.monotonic() - began
                if took >= SLOW_TICK_S:
                    self.say(f"that tick took {took:.0f} s, and nothing else traded while it ran")

    def _owed(self) -> bool:
        """An order was never answered and it is time to ask the account whether it filled: a tick is due for that alone."""
        return bool(self._unsure) and time.time() >= self._settle_at and not self.holding

    def _due(self) -> list[int]:
        """The dirty closes whose turn it is: a launch at once, a re-queued close at once, the rest once `settle_s` has passed
        since they were last planned, so a burst of updates on one close is planned once."""
        now = time.time()
        return [t for t, at in self._dirty.items()
                if t in self._urgent or at < self._planned_at.get(t, 0.0) or now - self._planned_at.get(t, 0.0) >= self.cfg.settle_s]

    async def _sweeps(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.cfg.sweep_s)
            self._mark_all()

    async def _wallet_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.cfg.wallet_s)
            await self.refresh_balance()
            if self.manage:
                await self.sync()

    async def _feeds_loop(self) -> None:
        """Spot and candles every few seconds; a series whose spot moved, or whose pictures are old, is redrawn whole."""
        while not self._stopping:
            try:
                await asyncio.gather(*(f.refresh(max_age=3.0) for f in self.feeds.values()), return_exceptions=True)
                now = time.time()
                for b in self.batches:
                    f = self._feed(b.asset or "BTC")
                    if not f.ready:
                        continue
                    spot, at = self.drawn.get(b.batch_id, (0.0, 0.0))
                    moved = spot > 0 and abs(f.spot / spot - 1) >= self.cfg.respot
                    if moved or now - at >= self.cfg.refresh_s:
                        self.drawn[b.batch_id] = (float(f.spot), now)
                        self._mark_all(b.batch_id)
                        for c in self.tradable(b, now):
                            self.pictures.pop(c.topic_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.say(f"price feed: {type(e).__name__}: {e}")
            await asyncio.sleep(3.0)

    # ── pictures ────────────────────────────────────────────

    def _paper_book(self, book: Book) -> Book:
        """A copy of the watch's book with the Desk's paper fills laid over it, as a live fill would have moved it."""
        b = Book(book.topic_id, book.title, book.end_time_utc, book.quote_mode, book.volume_msat, list(book.option_ids),
                 list(book.bins), list(book.shares), book.alpha, fetched_at=book.fetched_at, close_block=book.close_block,
                 trading_stops_block=book.trading_stops_block)
        if not self.live:
            held = {o: n for o, (n, _) in self.ledger.legs(self.key, self.mode, b.topic_id).items()}
            for o, n in self._seed.get(b.topic_id, {}).items():
                held[o] = held.get(o, 0.0) - n
            if any(abs(n) >= 0.005 for n in held.values()):
                at = {o: i for i, o in enumerate(b.option_ids)}
                for o, n in held.items():
                    if o in at:
                        b.shares[at[o]] = max(b.shares[at[o]] + n, 0.0)
        b.reprice()
        return b

    def _series_for(self, batch: Batch) -> tuple:
        """The series as the site shows it, for a bot that reads every close."""
        if not self.bot.reads_series:
            return ()
        return shown_series((b.end_time_utc, b.bins, b.shares) for b in (self._paper_book(x) for x in self.watch.open_closes(batch.batch_id)))

    @property
    def pools(self) -> bool:
        """Whether the Desk itself pools the bot's picture toward the baseline in quiet hours. The zoo already damps the
        drift, tilt and stated view of the pictures that go through its conviction, volatility, view and payoff machinery
        (`zoo.core.Ctx.lean_scale`), so those are left as they come; every other picture (simulated paths, named
        distributions, pooled or surface pictures, a file of your own) is pooled here by the same share."""
        m = self.bot.model
        return m is None or getattr(m, "pipeline", "") not in ("conviction", "vol", "view", "payoff")

    def _draw(self, feed, books: list[Book], series) -> list[tuple[int, list[float], float]]:
        """In a runner thread: the bot's picture of every book, pooled toward the baseline in quiet hours where the zoo
        has not already damped it."""
        from .zoo.core import base_pdf, to_bins

        out = []
        for book in books:
            if self._stopping:
                break
            ctx = ctx_of(feed, book)
            probs = [float(x) for x in self.bot.probs(book, ctx, series)]
            if len(probs) != len(book.bins) or abs(sum(probs) - 1) > 1e-6:
                raise ValueError(f"{self.bot.name} must return {len(book.bins)} probabilities summing to 1")
            scale = float(getattr(ctx, "lean_scale", 1.0))
            if self.pools and scale < 1.0 - 1e-9:
                probs = sideways(probs, to_bins(ctx, base_pdf(ctx)), scale)
            out.append((book.topic_id, probs, scale))
        return out

    async def pictures_for(self, batch: Batch, books: list[Book]) -> dict[int, Pic]:
        """Fresh pictures of `books`, drawn in one pass where missing."""
        feed = self._feed(batch.asset or "BTC")
        missing = [b for b in books if b.topic_id not in self.pictures]
        if missing and feed.ready:
            try:
                got = await off_loop(self._draw, feed, missing, self._series_for(batch))
            except Exception as e:
                self.say(f"{batch.short}: no picture: {e}")
                got = []
            now = time.time()
            for topic, probs, scale in got:
                self.pictures[topic] = Pic(probs, now, float(feed.spot), scale)
        return {b.topic_id: self.pictures[b.topic_id] for b in books if b.topic_id in self.pictures}

    # ── planning ────────────────────────────────────────────

    def plan_close(self, book: Book, probs: list[float], free: float) -> Plan:
        """What to do on one close: sell what the market overpays, then buy by Kelly with the free share."""
        from .kelly import Sizing, kelly_buys

        plan = Plan(book, probs, free=free)
        held = self.ledger.legs(self.key, self.mode, book.topic_id)
        if held:
            plan.sells = PL.sell_down(book, probs, held, self.cfg.exit_edge, self.cfg.profit_only)
        if plan.sells:
            return plan                         # the sale moves the book: buy against the book it leaves, next tick
        if free < 1:
            plan.why = "no free share"
            return plan
        legs = kelly_buys(book, probs, free, held, Sizing(kelly=self.cfg.kelly, min_edge=self.cfg.min_edge))
        if legs:                                # already worth sending, the 1 sat minimum commission included: an order can
            plan.buys = legs                    # cost well under a sat when the crowd has priced one range as certain
        else:
            plan.why = "nothing under value" if not held else "holds its Kelly stake"
        return plan

    async def tick(self, force: bool = False) -> bool:
        """Plan every due close, then send every sale and every buy, each close's legs in one order, all closes in one request.
        While Glimpse is not answering nothing is planned or sent: what is due stays queued for the next attempt."""
        now = time.time()
        if self.live and (self.holding or (self.manage and now < self._sync_at)):
            return False
        if self.live and self._unsure:
            await self._settle()                # orders never answered, settled against the account before anything is planned
            if self.holding:
                return False
        self.ticks += 1
        due = set(self._due()) if not force else set()
        if force:
            for b in self.batches:
                due |= {c.topic_id for c in self.tradable(b, now)}
        if not due:
            return False
        if self.manage and not await self.sync():
            return False
        self.ledger.prune(now)
        self._settle_paper(now)
        allot = self.allotments(now)
        hour_left = self.cfg.hour_turnover * self.bankroll - self.spent_last_hour()
        pool_left = self.deployable - self.open_cost
        plans: list[tuple[Batch, Plan]] = []
        seen: set[int] = set()
        for b in self.batches:
            books = [c for c in self.tradable(b, now) if c.topic_id in due]
            for t in [c.topic_id for c in books]:
                self._dirty.pop(t, None)
                self._planned_at[t] = now
                seen.add(t)
            books = [c for c in books if c.topic_id not in self._unsure]     # an order there was never answered: left alone
            if not books:
                continue
            pics = await self.pictures_for(b, books)
            for book in books:
                pic = pics.get(book.topic_id)
                if pic is None:
                    continue
                paper = self._paper_book(book)
                held_cost = self.ledger.open_cost(self.key, self.mode, book.topic_id)
                free = min(allot.get(book.topic_id, 0.0) - held_cost, pool_left, hour_left)
                plan = await off_loop(self.plan_close, paper, pic.probs, max(free, 0.0))
                self._mark(paper, pic.probs)
                if plan.buys and self._too_small(plan, held_cost):
                    plan.buys, plan.why = [], "under the dead-band"
                    self.skipped_small += 1
                    self._small.add(book.topic_id)
                else:
                    self._small.discard(book.topic_id)
                if plan.sells or plan.buys:
                    plans.append((b, plan))
        for t in [t for t in due if t in self._urgent]:
            self._urgent.discard(t)
        for t in due - seen:                    # a close the desk may not trade (ended, about to, too far out) has no turn to wait for
            self._dirty.pop(t, None)
        if not plans:
            return False
        traded = await self._sell(plans)
        traded = await self._buy([(b, p) for b, p in plans if p.buys], pool_left, hour_left) or traded
        if traded:
            self._save()
            if self.live and self.on_trade:
                self.on_trade()
        return traded

    def _too_small(self, plan: Plan, held_cost: float) -> bool:
        """The dead-band, judged on what an order promises, not what it costs: its expected profit is what the picture says the
        contracts are worth less the charge (the cost, the commission at least one sat). An order promising under
        `min_gain_sats` is not sent, and a top-up on a close already held must promise `rebalance_share` of what that close
        already cost. Spot drifts and the clock ticks move every picture a little every few seconds; without this a held
        close would be topped up by a few sats on every pass, each order paying the minimum fee for next to no edge. A real
        move (a trade in the book, a jump in spot) promises far more and goes through; so does a near-free order on a book
        the crowd has priced as certain, whose cost is nothing and whose promise is hundreds of sats."""
        cost = sum(x.cost_sats for x in plan.buys)
        raw = cost / (1 + P.FEE)
        gain = sum(plan.probs[x.index] * PL.NET * x.contracts for x in plan.buys) - (raw + max(P.FEE * raw, P.MIN_FEE_SATS))
        if gain < self.cfg.min_gain_sats:
            return True
        return held_cost > 0 and gain < self.cfg.rebalance_share * held_cost

    def _tag(self, book: Book) -> str:
        b = self.watch.batch_of.get(book.topic_id) if self.watch else None
        return fmt.question(b.short if b else "", book.end_time_utc)[:-4]

    # ── execution ───────────────────────────────────────────

    async def _sell(self, plans: list[tuple[Batch, Plan]]) -> bool:
        groups = [(b, p) for b, p in plans if p.sells]
        if not groups:
            return False
        for _, p in groups:
            book = p.book
            for i, contracts, proceeds in p.sells:
                held, cost = self.ledger.legs(self.key, self.mode, book.topic_id).get(book.option_ids[i], (contracts, 0.0))
                part = "" if contracts >= held - 0.005 else f" of {fmt.contracts(held)}"
                gain = proceeds - cost * contracts / held if held > 0 and cost > 0 else None
                self.say(f"{self._tag(book)}  {'SELL' if self.live else 'would sell'} {fmt.span(*book.bins[i])}  "
                         f"{fmt.contracts(contracts)}{part} for ₿{proceeds:,.0f}"
                         + ("" if gain is None else f"  {fmt.sats(gain, signed=True)} on cost"))
        if self.live and self._down_at and self.watch:      # Glimpse had stopped answering and the wait is up: a book read
            try:                                            # asks whether it is back, so that an order is never the question
                await self._ask(self.watch.refresh_book(groups[0][1].book.topic_id))
            except ApiError:
                pass
            if self._down_at:
                self._requeue(p.book.topic_id for _, p in groups)
                return False
        traded = False
        for k in range(0, len(groups), ORDER_CHUNK):
            chunk = groups[k:k + ORDER_CHUNK]
            got = {p.book.topic_id: sum(x[2] for x in p.sells) for _, p in chunk}
            if self.live:
                if self.holding:                    # an order before this one went unanswered: these wait for Glimpse
                    self._requeue(p.book.topic_id for _, p in chunk)
                    continue
                try:
                    fills = await self._order(self.api.sell_many(
                        [(p.book.topic_id, [(p.book.option_ids[i], c) for i, c, _ in p.sells]) for _, p in chunk]))
                except Ambiguous as e:              # it may or may not have filled: the account says which, the desk goes on
                    self._unanswered("sell", [(b, p, got[p.book.topic_id]) for b, p in chunk], e)
                    continue
                except ApiError as e:
                    self.say(f"sell failed: {e}")
                    if isinstance(e, NoServiceAuth):
                        self.stop()
                        return traded
                    if isinstance(e, Transient):
                        self._requeue(p.book.topic_id for _, p in chunk)
                    continue
                ok = {}
                for f in fills:
                    if f.error:
                        self.say(f"sell rejected on {f.topic_id}: {f.error}")
                    else:
                        ok[f.topic_id] = f.cost_sats
                got = ok
            for _, p in chunk:
                if p.book.topic_id in got:
                    self._book_sale(p, got[p.book.topic_id])
                    traded = True
        if traded and self.live:
            await self.refresh_balance()
        return traded

    def _book_sale(self, p: Plan, got: float) -> None:
        """A filled sale out of the ledger and into the session's figures; its close is planned again against the book it left."""
        basis = 0.0
        for i, contracts, _ in p.sells:
            held, cost = self.ledger.legs(self.key, self.mode, p.book.topic_id).get(p.book.option_ids[i], (contracts, 0.0))
            basis += cost * contracts / held if held > 0 else 0.0
            self.ledger.reduce(self.key, self.mode, p.book.topic_id, p.book.option_ids[i], contracts)
        self.sold_sats += got
        self.gain_sats += got - basis
        self.trades += 1
        self._dirty[p.book.topic_id] = time.time()          # buy against the book the sale left, next tick
        if self.live and self.watch:
            asyncio.ensure_future(self._reread(p.book.topic_id))

    async def _reread(self, topic_id: int) -> None:
        """The close's book read again after a sale moved it; a close the watch has since dropped (it ended) is left alone."""
        if topic_id not in self.watch.books:
            return
        try:
            await self._ask(self.watch.refresh_book(topic_id), trips=False)
        except (ApiError, asyncio.CancelledError):
            pass

    async def _buy(self, plans: list[tuple[Batch, Plan]], pool_left: float, hour_left: float) -> bool:
        if not plans:
            return False
        plans.sort(key=lambda bp: -sum(x.cost_sats for x in bp[1].buys))
        kept: list[tuple[Batch, Plan, float]] = []
        total = 0.0
        for b, p in plans[: self.cfg.max_orders_tick]:
            cost = sum(x.cost_sats for x in p.buys)
            if total + cost > min(pool_left, hour_left) + 1e-6:
                continue
            kept.append((b, p, cost))
            total += cost
        for _, p in plans[self.cfg.max_orders_tick:]:
            self._dirty[p.book.topic_id] = self._planned_at.get(p.book.topic_id, time.time()) - 1   # its turn comes next tick
        if not kept:
            return False
        charges: dict[int, float] = {}
        if self.live:
            kept = await self._estimated(kept)
            if not kept:
                return False
            for k in range(0, len(kept), ORDER_CHUNK):  # a chunk that fails never costs the chunks before it their place in the ledger
                chunk = kept[k:k + ORDER_CHUNK]
                if self.holding:                    # an order before this one went unanswered: these wait for Glimpse
                    self._requeue(p.book.topic_id for _, p, _ in chunk)
                    continue
                try:
                    fills = await self._order(self.api.buy_many(
                        [(p.book.topic_id, [(p.book.option_ids[x.index], x.contracts) for x in p.buys]) for _, p, _ in chunk]))
                except Ambiguous as e:              # it may or may not have filled: the account says which, the desk goes on
                    self._unanswered("buy", chunk, e)
                    continue
                except ApiError as e:
                    self.say(f"buy failed: {e}")
                    if isinstance(e, NoServiceAuth):
                        self.stop()
                        break
                    if isinstance(e, Transient):
                        self._requeue(p.book.topic_id for _, p, _ in chunk)
                    continue
                for f in fills:
                    if f.error:
                        self.say(f"buy rejected on {f.topic_id}: {f.error}")
                    else:
                        charges[f.topic_id] = f.cost_sats + f.fee_sats
        else:                                   # a paper fill pays what the server would: the cost, the fee at least one sat
            charges = {p.book.topic_id: cost / (1 + P.FEE) + max(cost * P.FEE / (1 + P.FEE), P.MIN_FEE_SATS) for _, p, cost in kept}
        done = False
        for b, p, local in kept:
            charge = charges.get(p.book.topic_id)
            if charge is None:
                continue
            done = True
            self._book(b, p, charge, local)
        if done and self.live:
            await self.refresh_balance()
        return done

    async def _estimated(self, kept: list[tuple[Batch, Plan, float]]) -> list[tuple[Batch, Plan, float]]:
        """Each close's order against the server's estimate, concurrently; a book that moved is read again and its close
        re-queued rather than bought at a price the plan did not see."""
        sem = asyncio.Semaphore(MAX_ESTIMATES)

        async def one(item):
            b, p, local = item
            async with sem:
                if self.holding:                    # Glimpse stopped answering the closes before this one: it waits its turn
                    self._requeue([p.book.topic_id])
                    return None, ""
                if self.watch and self.watch.age(p.book.topic_id) > self.cfg.max_book_age:
                    try:
                        fresh = self._paper_book(await self._ask(self.watch.refresh_book(p.book.topic_id)))
                    except ApiError as e:
                        return None, self._unchecked(p, "cannot read its book", e)
                    q0 = PL.Quote(fresh.shares, fresh.alpha)
                    cost = 0.0
                    for x in p.buys:
                        before = q0.base
                        q0.add(x.index, x.contracts)
                        cost += (q0.base - before) * (1 + P.FEE)
                    if abs(cost - local) > self.cfg.estimate_tolerance * local + 5:
                        self._dirty[p.book.topic_id] = time.time()
                        return None, f"{self._tag(p.book)}  book moved since it was planned, re-queued"
                try:
                    raw, fee = await self._ask(self.api.estimate_legs(
                        p.book.topic_id, [(p.book.option_ids[x.index], x.contracts) for x in p.buys]))
                except ApiError as e:
                    return None, self._unchecked(p, "no estimate", e)
                charge = raw + max(fee, P.MIN_FEE_SATS)
                if abs(charge - local) > self.cfg.estimate_tolerance * local + 5:
                    self._dirty[p.book.topic_id] = time.time()
                    asyncio.ensure_future(self._reread(p.book.topic_id))
                    return None, f"{self._tag(p.book)}  book moved (local {local:,.0f} vs server {charge:,.0f}), re-queued"
                return item, ""

        out = []
        for item, note in await asyncio.gather(*(one(x) for x in kept)):
            if note:
                self.say(note)
            if item is not None:
                out.append(item)
        return out

    def _unchecked(self, p: Plan, what: str, e: ApiError) -> str:
        """The line for a close whose order could not be checked against the server. If Glimpse merely did not answer, the
        close is planned again on the next tick, and once the desk is holding its one line has said so already."""
        if isinstance(e, Transient):
            self._requeue([p.book.topic_id])
            if self._down_at:
                return ""
        return f"{self._tag(p.book)}  {what}: {e}"

    def _book(self, batch: Batch, p: Plan, charge: float, local: float) -> None:
        """A filled buy into the ledger, the marks and the log."""
        book, probs = p.book, p.probs
        value = sum(probs[x.index] * PL.NET * x.contracts for x in p.buys)
        top = max(p.buys, key=lambda x: x.cost_sats)
        # A buy on a close the watch just announced: by the time a close is listed (about ten seconds past the hour) other
        # bots have usually traded it already, so the log says how much got there first; a book still at its uniform seed
        # earns the plain tag. Either way it counts as a launch taken.
        fresh = book.topic_id in self._launched
        seed = all(abs(s - book.shares[0]) < 1e-9 for s in book.shares)
        if fresh:
            self._launched.discard(book.topic_id)
            self.launches += 1
        gap0 = PL.gap(book, probs)
        moved = Book(book.topic_id, book.title, book.end_time_utc, book.quote_mode, book.volume_msat, list(book.option_ids),
                     list(book.bins), list(book.shares), book.alpha)
        for x in p.buys:
            moved.shares[x.index] += x.contracts
        moved.reprice()
        self.say(f"{self._tag(book)}  {'BUY' if self.live else 'would buy'} {len(p.buys)} range{'' if len(p.buys) == 1 else 's'}  "
                 f"{fmt.sats(charge)} of {fmt.sats(p.free)} free  worth {fmt.sats(value)} by its picture  "
                 f"top {fmt.span(*book.bins[top.index])}  shown gap {gap0:.0%} → {PL.gap(moved, probs):.0%}"
                 + ("  ·  at the seed" if fresh and seed else
                    f"  ·  at launch, {fmt.sats(book.volume_msat / 1000)} traded first" if fresh else ""))
        for x in p.buys:                        # the charge shared out by each leg's planned cost (evenly if the plan cost nothing)
            part = x.cost_sats / local if local > 0 else 1.0 / len(p.buys)
            self.ledger.add(self.key, self.mode, book, batch.asset or "BTC", x.index, x.contracts, charge * part)
        self._mark(moved, probs)
        self.spent.append((time.time(), charge))
        self.paid_sats += charge
        self.trades += 1

    def _mark(self, book: Book, probs: list[float]) -> None:
        held = self.ledger.legs(self.key, self.mode, book.topic_id)
        if not held:
            return
        at = {o: i for i, o in enumerate(book.option_ids)}
        q = PL.Quote(book.shares, book.alpha)
        for o, (n, _) in held.items():
            if o in at:
                i = at[o]
                n = min(n, book.shares[i])
                self.marks[(book.topic_id, o)] = (book.prices[i], probs[i], max(q.base - q.cost_after(i, -n), 0.0) * (1 - P.FEE))

    def _save(self) -> None:
        """The ledger to disk, at most every two seconds: a large ledger takes a while to write."""
        if time.time() - self._saved_at >= 2.0:
            self.ledger.save()
            self._saved_at = time.time()

    # ── settlement on paper ─────────────────────────────────

    def _settle_paper(self, now: float) -> None:
        """A paper close that has ended is paid out against the price feed: 98 sats a contract on the range the close
        landed in, nothing elsewhere. The feed's hourly bars are complete bars, so a close settles once its hour has closed."""
        if self.live:
            return
        topics = self.ledger.topics(self.key, self.mode)
        for tid in [t for t, v in topics.items() if v.get("end", 0) <= now]:
            t = topics[tid]
            feed = self.feeds.get(t.get("asset", ""))
            bars = getattr(feed, "bars", None)
            if bars is None or not len(bars):
                continue
            end = int(t["end"])
            opens = bars.index.as_unit("s").asi8                     # each complete bar's open time, unix seconds
            if opens[-1] + 3600 < end:
                continue                                              # the bar that settles it is not in yet
            at = min(int(np.searchsorted(opens, end - 3600, side="right")) - 1, len(bars) - 1)
            price = float(bars["close"].iloc[max(at, 0)])
            paid = sum(PL.NET * v[0] for v in t["legs"].values() if v[2] <= price < v[3])
            cost = sum(v[1] for v in t["legs"].values())
            self.settled_sats += paid - cost
            self.say(f"{fmt.question(t.get('asset', ''), end)[:-4]}  settled at {fmt.price(price)}: paid {fmt.sats(paid)} on "
                     f"{fmt.sats(cost)} at cost  {fmt.sats(paid - cost, signed=True)}  ·  paper")
            del topics[tid]

    # ── managing the account's portfolio ────────────────────

    async def sync(self) -> bool:
        """Managing: the account's positions into the ledger, read every `portfolio_s` when live. False while they cannot be
        read: a desk does not trade a portfolio it cannot see, and asks again after `probe_s`, doubling to `probe_max_s`."""
        if not self.manage or (self._synced_at and (not self.live or time.time() - self._synced_at < self.cfg.portfolio_s)):
            return True
        if self.live and time.time() < self._sync_at:
            return False
        try:
            held = await self._ask(self.api.positions(), self.cfg.order_timeout_s) if self.api.authenticated else []
        except ApiError as e:
            if self.live:
                self._sync_wait = min(max(self._sync_wait * 2, self.cfg.probe_s), self.cfg.probe_max_s)
                self._sync_at = time.time() + self._sync_wait
                if not self._down_at:
                    self.say(f"cannot read your portfolio: {e}; asking again in {self._sync_wait:g} s")
                return False
            self.say(f"cannot read your portfolio: {e}")
            held = []
        self._sync_wait = 0.0
        self._adopt(held)
        return True

    def _adopt(self, held: list[Position]) -> None:
        """The account's positions, as just read, become the managed portfolio in the ledger."""
        rows = [r for b in self.batches for r in self.watch.rows.get(b.batch_id, [])]
        self.ledger.replace(PORTFOLIO, self.mode, rows, lambda t: (self.watch.batch_of[t].asset or "BTC") if t in self.watch.batch_of else "BTC",
                            held)
        self._synced_at = time.time()
        if not self.live:
            self._seed = {r.topic_id: {o: n for o, (n, _) in self.ledger.legs(PORTFOLIO, self.mode, r.topic_id).items()} for r in rows}

    # ── status ──────────────────────────────────────────────

    def status(self) -> str:
        """One line: the bankroll, what is deployed where, what the session did, and the stream's state."""
        held = self.holdings()
        closes = {x[2] for x in held}
        w = self.watch
        stream = ("stream up" if w and w.streaming else "stream down, polling") + (f", {w.messages} updates, {w.launches} launches" if w else "")
        if self.live and self._down_at:
            stream += f"  ·  Glimpse not answering for {lasted(time.time() - self._down_at)}, orders held"
        if self._unsure:
            stream += f"  ·  {len(self._unsure)} closes wait on an unanswered order"
        if self.outages or self.unanswered:
            stream += (f"  ·  rode out {self.outages} outage{'' if self.outages == 1 else 's'}, "
                       f"{self.unanswered} unanswered order{'' if self.unanswered == 1 else 's'}")
        n = sum(len(self.tradable(b)) for b in self.batches)
        return (f"bankroll {fmt.sats(self.bankroll)}  deployed {fmt.sats(self.open_cost)} in {len(closes)} of {n} closes  ·  "
                f"bought {fmt.sats(self.paid_sats)}  sold {fmt.sats(self.sold_sats)} ({fmt.sats(self.gain_sats, signed=True)} on cost)"
                + (f"  settled {fmt.sats(self.settled_sats, signed=True)}" if not self.live else "")
                + f"  ·  {self.trades} trades, {self.launches} at the seed, {len(self._small & self.topics)} closes under the dead-band, "
                f"{self.ticks} ticks  ·  {stream}")


def desk_note(cfg: DeskConfig | None = None) -> str:
    """The about screen's account of how a Desk trades, for the README and the CLI."""
    c = cfg or DeskConfig()
    return (
        f"The Desk runs one bot across every series at once. One WebSocket carries every trade of every market, so every open "
        f"close's book is current; the series are listed again every {c.list_s:g} s, with a burst of polls after the top of the "
        f"hour (a one-page probe every second from 3 s before it) to catch the new closes the moment they are seeded. A close is "
        f"re-planned within a second of any trade in it, "
        f"at once when it is launched, whenever its series' spot moves {c.respot:.2%}, and every {c.sweep_s:g} s regardless. The "
        f"bankroll is the wallet's balance plus what the Desk holds (on paper, the budget); {c.reserve:.0%} is kept back and the "
        f"rest is shared over the open closes of the series traded, one share a close by default. On each close it first sells the part of a "
        f"position the market pays {c.exit_edge:.0%} more for than the picture says it is worth"
        + (" and more than it cost" if c.profit_only else "") + f", then buys the exact growth-optimal position for mutually "
        f"exclusive outcomes on the LS-LMSR, at {c.kelly:g} of the Kelly optimum, never a contract whose return after both fees "
        f"falls under {c.min_edge:.0%}. Every close's legs go in one order, every close planned in a tick in one request, each "
        f"checked first against the server's estimate. In quiet hours (the weekend; CME's closed hours for gold and oil) the "
        f"picture is pooled toward the clock's driftless walk by the share of busy hours left, so the bot bets sideways. A live "
        f"desk does not stop for a slow or absent Glimpse: a read is asked once and given {c.read_timeout_s:g} s, after "
        f"{c.trip_after} unanswered in a row it holds its orders and asks again every {c.probe_s:g} to {c.probe_max_s:g} s until "
        f"an answer comes, and an order that was sent and never answered is settled against the account's positions (booked if "
        f"its contracts are there, planned again if not) rather than left for a human."
    )


def chosen_batches(every: list[Batch], cfg: DeskConfig, bot: Bot) -> list[Batch]:
    """The batches a Desk trades: the configured series, among those the bot can draw."""
    want = [s.lower() for s in cfg.series_list()]
    return [b for b in every if b.short.lower() in want and bot.draws(b.asset or "BTC")]
