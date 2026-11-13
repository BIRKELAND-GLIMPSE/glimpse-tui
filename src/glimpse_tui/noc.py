"""The monitor: every bot trading on this machine as one desk, and what the fleet holds across every market.

The bots screen shows one model on one series; the advanced screen one bot across a few. Neither answers the question a
desk asks all day: what is running, what does it hold, what is it worth, and what did it just do. The monitor does. It
keeps one row per bot (a `Unit`): every runner the terminal started (a bot walking a series, a portfolio manager, a
desk, a Triangulator, an Arbitrageur, the Profit Taker), every bot the ledger says still holds positions although nothing
here runs it (a bot from an earlier session, or one a `glimpse-tui run` in another shell is trading: the ledger file is
read again when it changes), and the account itself, as the API reports it, whoever opened its positions.

Positions are marked to what selling them back into the book would fetch now, after the exit fee, against books the
monitor keeps current while its screen shows: a `watch.Watch` of every price series (the market stream plus a listing
every two minutes), less whatever series a running desk already watches, whose books are read in place. A ledger is
compared with what it held a second ago, so every buy and sale of every bot is a `Fill` on one tape, whatever kind of
runner made it and without the runners knowing they are watched.

Nothing here trades. `Monitor` holds the screen's state (the cursors, the tape, the P&L samples); `nocview` draws it.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import funding
from . import policy as PL
from . import pricing as P
from .api import Batch, Book, Candle, parse_bin
from .bots import PORTFOLIO

if TYPE_CHECKING:
    from .app import Terminal

HORIZONS = (24, 72, 168, 0)     # hours of closes a tile draws; 0 is every open close
FLASH_S = 120.0                 # a fill stays lit on the wall this long
SAMPLE_S = 10.0                 # how often each unit's open P&L is sampled for its sparkline
DISK_S = 15.0                   # the ledger file is looked at this often for what other processes wrote
LIST_S = 120.0                  # the monitor's own watch lists each series this often; the stream keeps it current between
QUIET_S = 180.0                 # a running bot that has logged nothing for this long is shown as quiet
ALERT_S = 600.0                 # an error line is an alert for this long
ERROR_WORDS = ("failed", "rejected", "cannot", "no estimate")
ALL, ACCOUNT = "all", "account"


def is_desk(r) -> bool:
    return hasattr(r, "allotments")


@dataclass
class Holding:
    """One range a unit holds in one close, marked."""
    topic: int
    option: int
    end: int
    asset: str
    lo: float
    hi: float
    contracts: float
    cost: float
    value: float                    # what selling it back into the book now would fetch after the fee; its cost when unpriced
    price: float | None = None      # the range's price now, in sats
    p: float | None = None          # the bot's own chance of it, where the bot has drawn the close
    series: str = ""                # the series' short name, where the monitor knows the close; else the asset


@dataclass
class Unit:
    """One row of the fleet: a bot and everything it holds."""
    id: str
    name: str
    kind: str                       # desk | runner | manager | taker | triangle | arbitrage | idle | account | all
    live: bool
    running: bool
    where: str = ""
    runner: object | None = None
    holdings: list[Holding] = field(default_factory=list)
    trades: int = 0
    paid: float = 0.0               # this session: bought
    sold: float = 0.0               # this session: what its sales fetched
    taken: float = 0.0              # this session: sales over the cost of what was sold, plus paper settlements
    budget: float = 0.0
    last_at: float = 0.0            # when its log last gained a line
    alert: str = ""                 # its latest error line, while it is recent
    note: str = ""
    shown: bool = True              # False for a runner that has neither run nor bought: tracked, so its first fill is seen, not listed
    funds: funding.Funds | None = None      # its bankroll, what is deployed and free, its equity; None where there is no bankroll
    key: str = ""                   # its name in the equity record

    @property
    def cost(self) -> float:
        return sum(h.cost for h in self.holdings)

    @property
    def value(self) -> float:
        return sum(h.value for h in self.holdings)

    @property
    def pnl(self) -> float:
        return self.value - self.cost

    @property
    def closes(self) -> int:
        return len({h.topic for h in self.holdings})

    @property
    def is_bot(self) -> bool:
        return self.kind not in (ALL, ACCOUNT)


@dataclass(frozen=True)
class Fill:
    """A change in what a unit holds: contracts and sats above zero are a buy, below a sale."""
    at: float
    unit: str
    name: str
    live: bool
    topic: int
    option: int
    end: int
    asset: str
    lo: float
    hi: float
    contracts: float
    sats: float


def kind_of(r) -> str:
    if is_desk(r):
        return "desk"
    if not r.buys:
        return "taker"
    if getattr(r, "triangle", None) is not None:
        return "triangle"
    if r.bot.arbitrage:
        return "arbitrage"
    return "manager" if r.manage else "runner"


def where_of(r) -> str:
    if is_desk(r):
        return f"{len(r.batches)} series"
    s = str(getattr(r, "series", "") or "")
    return s if len(s) <= 9 else f"{len(s.split(','))} series"


def mark(book: Book, legs: dict[int, float], paper: bool) -> dict[int, tuple[float, float]]:
    """option id -> (the range's price in sats, what selling `contracts` of it back would fetch after the exit fee) for
    every leg the book lists. A paper position was never bought on the server, so its contracts are laid over the book
    first: it is valued against the book it would have made, as a paper runner values it."""
    at = {o: i for i, o in enumerate(book.option_ids)}
    shares = list(book.shares)
    if paper:
        for o, n in legs.items():
            if o in at:
                shares[at[o]] += n
    q = PL.Quote(shares, book.alpha)
    prices = P.prices(shares, book.alpha) if paper else book.prices
    out = {}
    for o, n in legs.items():
        if o in at:
            i = at[o]
            n = min(n, shares[i])
            out[o] = (prices[i], max(q.base - q.cost_after(i, -n), 0.0) * (1 - P.FEE))
    return out


def candles(feed) -> list[Candle]:
    """A feed's hourly bars as candles, oldest first. Rebuilt only when a new bar has closed."""
    bars = getattr(feed, "bars", None)
    if bars is None or not len(bars):
        return []
    key = (bars.index[-1], len(bars))
    got = getattr(feed, "_noc_candles", None)
    if got is None or got[0] != key:
        t = bars.index.as_unit("s").asi8.tolist()
        o, h, lo, c = (bars[k].to_numpy(dtype=float).tolist() for k in ("open", "high", "low", "close"))
        got = (key, [Candle(int(t[i]), o[i], h[i], lo[i], c[i]) for i in range(len(t))])
        try:
            feed._noc_candles = got
        except AttributeError:
            pass
    return got[1]


class Monitor:
    """The monitor screen's state and its reading of the fleet. `refresh` is called once a second; everything else reads."""
    connect = None                  # the tests' WebSocket; None is the real one

    def __init__(self) -> None:
        self.cur = 0                # the fleet row under the cursor: 0 is every bot together
        self.series_cur = 0         # the tile under the cursor
        self.zoom = False           # the selected series alone on the wall
        self.span = 1               # index into HORIZONS
        self.focus = 0              # 0 the fleet, 1 the positions
        self.pos_cur = 0
        self.units: list[Unit] = []
        self.fills: deque[Fill] = deque(maxlen=6000)        # a range a fill: one order on one close is often twenty
        self.history: dict[str, deque[tuple[float, float]]] = {}
        self.version = 0            # bumps when a holding changed: keys the wall's cache with the books' stamps
        self.watch = None
        self._watch_task: asyncio.Task | None = None
        self._closing: list[asyncio.Task] = []
        self._prev: dict[str, dict[tuple[int, int], tuple]] = {}
        self._marks: dict[tuple[str, int], tuple[tuple, dict[int, tuple[float, float]]]] = {}
        self._seen: dict[str, tuple[str, float, str, float]] = {}      # unit -> (last log line, when it appeared, alert, alert at)
        self._sampled_at = 0.0
        self._disk: dict | None = None
        self._disk_at = 0.0
        self._disk_mtime = 0.0
        self.equity = funding.Equity()                      # each bot's equity over time, kept across sessions

    # ── the books ───────────────────────────────────────────

    def sources(self, t: Terminal) -> list:
        """Every watch whose books the monitor may read: its own, and each running desk's."""
        out = [r.watch for r in t.runners.values() if is_desk(r) and r.running and r.watch is not None and r.watch.listed_at]
        if self.watch is not None and self.watch.listed_at:
            out.append(self.watch)
        return out

    def book(self, t: Terminal, topic: int) -> Book | None:
        for w in self.sources(t):
            b = w.books.get(topic)
            if b is not None:
                return b
        return None

    def batch_of(self, t: Terminal, topic: int) -> Batch | None:
        for w in self.sources(t):
            b = w.batch_of.get(topic)
            if b is not None:
                return b
        return None

    def stamp(self, t: Terminal, topic: int) -> float:
        """When the close's book last changed, as far as any watch knows."""
        for w in self.sources(t):
            if topic in w.books:
                b = w.batch_of.get(topic)
                return max(w.updated_at.get(topic, 0.0), w.listed_at.get(b.batch_id, 0.0) if b else 0.0)
        return 0.0

    def series(self, t: Terminal) -> list[Batch]:
        """The series the wall draws, in the terminal's order: every one a watch holds open closes of."""
        seen, out = set(), []
        ws = self.sources(t)
        for b in t.batches:
            if b.batch_id not in seen and any(w.rows.get(b.batch_id) for w in ws):
                seen.add(b.batch_id)
                out.append(b)
        for w in ws:                                    # a desk may trade a batch the terminal has not listed
            for b in w.batches.values():
                if b.batch_id not in seen and w.rows.get(b.batch_id):
                    seen.add(b.batch_id)
                    out.append(b)
        return out

    def closes(self, t: Terminal, batch_id: str, now: float) -> list[Book]:
        for w in self.sources(t):
            if w.rows.get(batch_id):
                return w.open_closes(batch_id, now)
        return []

    def streaming(self, t: Terminal) -> bool | None:
        ws = self.sources(t)
        return any(w.streaming for w in ws) if ws else None

    def ensure_watch(self, t: Terminal) -> None:
        """The monitor's own watch over the price series no running desk already watches. Started when the screen first
        shows, rebuilt when the set changes, stopped by `stop_watch` when the screen is left."""
        from .desk import PRICE_SERIES
        from .watch import Watch

        covered = {bid for r in t.runners.values() if is_desk(r) and r.running and r.watch is not None for bid in r.watch.batches}
        need = [b for b in t.batches if b.short in PRICE_SERIES and b.batch_id not in covered]
        have = set(self.watch.batches) if self.watch is not None else None
        if have == {b.batch_id for b in need}:
            return
        self.stop_watch()
        if not need:
            return
        self.watch = w = Watch(t.api, need, list_s=LIST_S)
        if self.connect is not None:
            w.connect = self.connect
        self._watch_task = asyncio.create_task(self._start(w), name="noc-watch")

    @staticmethod
    async def _start(w) -> None:
        try:
            await w.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:      # a failed first listing: the screen says there are no books, the next visit tries again
            w.say(f"start: {type(e).__name__}: {e}")

    def stop_watch(self) -> None:
        w, task, self.watch, self._watch_task = self.watch, self._watch_task, None, None
        if w is None:
            return

        async def close() -> None:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await w.stop()

        try:
            self._closing = [x for x in self._closing if not x.done()] + [asyncio.create_task(close(), name="noc-watch-stop")]
        except RuntimeError:        # no loop: nothing was started
            pass

    async def close(self) -> None:
        self.equity.save(force=True)
        self.stop_watch()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    # ── the fleet ───────────────────────────────────────────

    def _ledger_data(self, t: Terminal, now: float) -> dict:
        """The ledger as the file has it, for bots this terminal is not running: another process may be trading them."""
        if now - self._disk_at >= DISK_S:
            self._disk_at = now
            try:
                m = t.ledger.path.stat().st_mtime
                if m != self._disk_mtime:
                    self._disk, self._disk_mtime = json.loads(t.ledger.path.read_text()), m
            except (OSError, ValueError):
                pass
        return self._disk if isinstance(self._disk, dict) else t.ledger.data

    def _marked(self, t: Terminal, unit: str, topic: int, legs: dict[int, float], paper: bool) -> dict[int, tuple[float, float]]:
        book = self.book(t, topic)
        if book is None:
            return {}
        key = (self.stamp(t, topic), paper, tuple(sorted(legs.items())))
        got = self._marks.get((unit, topic))
        if got is None or got[0] != key:
            try:
                got = self._marks[(unit, topic)] = (key, mark(book, legs, paper))
            except (ValueError, FloatingPointError, IndexError):
                return {}
        return got[1]

    def _holdings(self, t: Terminal, unit: str, rows, paper: bool, marks: dict | None = None) -> list[Holding]:
        """`rows` are the ledger's (close, asset, topic, option, low, high, contracts, cost)."""
        by_topic: dict[int, dict[int, float]] = {}
        for _, _, topic, option, _, _, n, _ in rows:
            by_topic.setdefault(topic, {})[option] = n
        out = []
        for end, asset, topic, option, lo, hi, n, cost in rows:
            fresh = self._marked(t, unit, topic, by_topic[topic], paper).get(option)
            old = (marks or {}).get((topic, option))
            price, value = fresh if fresh is not None else (old[0], old[2]) if old is not None else (None, cost)
            batch = self.batch_of(t, topic)
            out.append(Holding(topic, option, int(end), asset, lo, hi, n, cost, value, price, old[1] if old is not None else None,
                               batch.short if batch is not None else asset))
        return out

    def _log_state(self, uid: str, r, now: float) -> tuple[float, str]:
        """(when the runner's log last gained a line, its latest error line while recent)."""
        last = r.log[-1] if r.log else ""
        line, at, alert, alert_at = self._seen.get(uid, ("", now, "", 0.0))
        if last != line:
            at = now
            hit = next((x for x in reversed(list(r.log)[-40:]) if any(w in x for w in ERROR_WORDS)), "")
            if hit and hit != alert:
                alert, alert_at = hit, now
            self._seen[uid] = (last, at, alert, alert_at)
        return at, alert if now - alert_at < ALERT_S else ""

    def build(self, t: Terminal, now: float) -> list[Unit]:
        units: list[Unit] = []
        owned: set[tuple[str, str]] = set()
        names = {b.id: b.name for b in t.bots}
        for bot_id, r in t.runners.items():
            owned.add((r.key, r.mode))
            rows = r.holdings()
            uid = f"bot:{bot_id}"
            last_at, alert = self._log_state(uid, r, now)
            desk = is_desk(r)
            u = Unit(uid, r.name, kind_of(r), r.live, r.running, where_of(r), r, self._holdings(t, uid, rows, not r.live, r.marks),
                     r.trades, r.paid_sats, r.sold_sats, r.gain_sats + (getattr(r, "settled_sats", 0.0) if desk else 0.0),
                     r.bankroll if desk else r.cfg.bankroll_sats, last_at, alert, shown=bool(r.running or r.cycles or rows),
                     key=f"{bot_id}:{r.mode}")
            if r.buys:
                carried = 0.0 if desk and r.live else self.equity.carry(u.key, u.taken, r.trades)
                u.funds = funding.funds(r, u.value, t.wallet.balance_sats if t.wallet else None, carried)
            units.append(u)
        for key, modes in self._ledger_data(t, now).items():
            if key == PORTFOLIO or not isinstance(modes, dict):
                continue                                # a manager's copy of the account: the account row is the truth
            for mode, topics in modes.items():
                if (key, mode) in owned or not isinstance(topics, dict):
                    continue
                rows = sorted(((x["end"], x.get("asset", ""), int(tid), int(o), v[2], v[3], v[0], v[1])
                               for tid, x in topics.items() if x.get("end", 0) > now for o, v in (x.get("legs") or {}).items()),
                              key=lambda x: (x[0], x[4]))
                if rows:
                    uid = f"ledger:{key}:{mode}"
                    units.append(Unit(uid, names.get(key, key), "idle", mode == "live", False, "", None,
                                      self._holdings(t, uid, rows, mode != "live"), note="holds positions; no bot here is running it"))
        hidden, bots_ = [u for u in units if not u.shown], [u for u in units if u.shown]
        head = Unit(ALL, "ALL BOTS", ALL, any(u.live for u in bots_), any(u.running for u in bots_),
                    holdings=[h for u in bots_ for h in u.holdings], trades=sum(u.trades for u in bots_), paid=sum(u.paid for u in bots_),
                    sold=sum(u.sold for u in bots_), taken=sum(u.taken for u in bots_), last_at=max((u.last_at for u in bots_), default=0.0))
        out = [head, *sorted(bots_, key=lambda u: (not u.running, not u.live, u.kind == "idle", u.name))]
        if t.api.authenticated and t.positions:
            hs = []
            for p in t.positions:
                if p.shares < 0.01 or (p.end_time and p.end_time <= now):
                    continue
                try:
                    lo, hi = parse_bin(p.option_name)
                except (ValueError, IndexError):
                    lo = hi = 0.0
                batch = self.batch_of(t, p.topic_id)
                price = p.value_sats / p.shares if p.shares else None
                hs.append(Holding(p.topic_id, p.option_id, int(p.end_time), batch.asset if batch else "", lo, hi, p.shares, p.cost_sats,
                                  p.value_sats, price, None, batch.short if batch else ""))
            if hs:
                acct = Unit(ACCOUNT, "ACCOUNT", ACCOUNT, True, False, holdings=hs, budget=t.wallet.balance_sats if t.wallet else 0.0,
                            note="everything your key holds, whoever opened it", key=funding.ACCOUNT)
                if t.wallet:                            # the account's equity: what the wallet holds free, plus its positions
                    w = t.wallet.balance_sats
                    acct.funds = funding.Funds(w + acct.cost, acct.cost, acct.value, 0.0, w, 0.0, w, None, w + acct.value)
                out.append(acct)
        return out + hidden

    def refresh(self, t: Terminal, now: float | None = None) -> None:
        """Read the fleet again, put what changed on the tape, and sample each unit's open P&L."""
        now = time.time() if now is None else now
        every = self.build(t, now)
        self.units = [u for u in every if u.shown]
        for u in every:
            if u.kind == ALL:
                continue
            snap = {(h.topic, h.option): (h.contracts, h.cost, h.end, h.asset, h.lo, h.hi) for h in u.holdings}
            prev = self._prev.get(u.id)
            self._prev[u.id] = snap
            if prev is None or prev == snap:
                continue                                # first sight is the baseline: what it already held is not a trade
            self.version += 1
            changed = []
            for k in snap.keys() | prev.keys():
                n1, c1, end, asset, lo, hi = snap.get(k, (0.0, 0.0, *prev.get(k, (0, 0, 0, "", 0.0, 0.0))[2:]))
                n0, c0 = prev.get(k, (0.0, 0.0))[:2]
                if abs(n1 - n0) < 0.005 or (k not in snap and end <= now + 5):
                    continue                            # unchanged, or a close that ended: settled, not sold
                changed.append(Fill(now, u.id, u.name, u.live, k[0], k[1], end, asset, lo, hi, n1 - n0, c1 - c0))
            self.fills.extend(sorted(changed, key=lambda f: (f.end, f.topic, f.lo)))
        live = {u.id for u in every}
        for gone in [k for k in self._prev if k not in live]:
            del self._prev[gone]
        if now - self._sampled_at >= SAMPLE_S and self.sources(t):
            self._sampled_at = now
            for u in self.units:
                self.history.setdefault(u.id, deque(maxlen=720)).append((now, u.pnl + u.taken))
        for u in every:                                 # equity, a sample a minute: a bot that runs or holds, and the account
            if u.funds is not None and u.key and (u.running or u.holdings or u.kind == ACCOUNT):
                self.equity.sample(u.key, now, u.funds.equity, u.funds.bankroll)
        self.equity.save(now)
        self.cur = max(0, min(self.cur, len(self.units) - 1))

    # ── the selection ───────────────────────────────────────

    @property
    def unit(self) -> Unit | None:
        return self.units[max(0, min(self.cur, len(self.units) - 1))] if self.units else None

    def held(self, u: Unit | None, topics: set[int] | None = None) -> dict[int, list[Holding]]:
        out: dict[int, list[Holding]] = {}
        for h in (u.holdings if u else ()):
            if topics is None or h.topic in topics:
                out.setdefault(h.topic, []).append(h)
        return out

    def tape(self, u: Unit | None) -> list[Fill]:
        """The selected unit's fills, newest last; every bot's for the fleet row (the account's own row repeats the live ones)."""
        if u is None:
            return []
        return [f for f in self.fills if (f.unit != ACCOUNT if u.kind == ALL else f.unit == u.id)]

    def trades(self, u: Unit | None) -> list[Fill]:
        """The tape as orders: the fills one reading found on one close, on one side, as a single line spanning the
        ranges it touched, with their contracts and sats added up. Newest last."""
        out: list[Fill] = []
        legs: list[int] = []
        for f in self.tape(u):
            g = out[-1] if out else None
            if g is not None and (g.at, g.unit, g.topic, g.contracts > 0) == (f.at, f.unit, f.topic, f.contracts > 0):
                out[-1] = Fill(g.at, g.unit, g.name, g.live, g.topic, legs[-1] + 1, g.end, g.asset, min(g.lo, f.lo), max(g.hi, f.hi),
                               g.contracts + f.contracts, g.sats + f.sats)
                legs[-1] += 1
            else:
                out.append(f)
                legs.append(1)
        return [Fill(g.at, g.unit, g.name, g.live, g.topic, n, g.end, g.asset, g.lo, g.hi, g.contracts, g.sats)
                for g, n in zip(out, legs, strict=True)]            # `option` carries the number of ranges

    def flashes(self, u: Unit | None, now: float) -> list[Fill]:
        """The selection's fills of the last FLASH_S seconds, range by range: what the wall lights up."""
        out = []
        for f in reversed(self.fills):
            if now - f.at > FLASH_S:
                break
            if u is not None and (f.unit != ACCOUNT if u.kind == ALL else f.unit == u.id):
                out.append(f)
        return out

    def by_close(self, u: Unit | None, series: str) -> list[tuple[int, int, str, float, float, int, float, float]]:
        """The unit's positions in one series, a row a close: (close, topic, asset, lowest held price, highest, legs, cost,
        value), soonest first."""
        rows: dict[int, list] = {}
        for h in (u.holdings if u else ()):
            if series and h.series != series:
                continue
            r = rows.setdefault(h.topic, [h.end, h.topic, h.asset, h.lo, h.hi, 0, 0.0, 0.0])
            r[3], r[4], r[5], r[6], r[7] = min(r[3], h.lo), max(r[4], h.hi), r[5] + 1, r[6] + h.cost, r[7] + h.value
        return sorted((tuple(r) for r in rows.values()), key=lambda r: (r[0], r[1]))
