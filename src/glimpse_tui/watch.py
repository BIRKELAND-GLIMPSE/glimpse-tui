"""The watch: every open close of the watched series, kept current, for the desk to trade on.

The runner reads one close's book every five seconds, so a week of hourly closes is seen once in fourteen minutes.
Glimpse streams every trade of every market over one WebSocket (`/ws/nmarket-updates`): each `market_update` message
carries every range's shares for the market that just traded, about 44 KB, and nothing need be subscribed to, the
whole firehose arrives. The watch holds one book per open close of every batch it is given, writes each message's
shares into its book and reprices it, so the desk sees a trade within the second it happens. A listing poll reconciles
the books with the server every `list_s` seconds, one batch at a time so eight slow reads never land together, and
catches closes that opened or shut. Around the top of each hour, when the hourly closes are seeded (about thirty seconds
past, 80 shares a range), the watch probes the end of each hourly batch's list every `burst_s` second (one small page,
`api.markets_from`, not the 9-second whole list) from `launch_s[0]` seconds past the hour (3 s before it) to `launch_s[1]`
past, until a close later than any it knew appears; the daily batches get the same burst after midnight UTC. Measured
on 9 November 2026 the hourly closes were listed within 15 s of the hour and other bots had bought into every one within
a minute, so the second matters. A new close is the subsidy the desk wants first, so it is announced as a "launch" event as soon as
its book is read.

Every change is an `Event` on `events` (and to `on_event` if given): "book" when a close's shares moved, "launch" when
a close is first seen after the start, "list" when a batch was re-listed, "stream" when the socket connected or dropped.
The initial listing in `start` raises no events, so `books` is full before the first one. The watch never trades and
knows nothing of fills; `refresh_book` is the authoritative read the desk makes before sending an order. A `now`
attribute (the clock, `time.time`) lets tests set the time; nothing in the tasks raises, a failed read is one line in `log`.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

import websockets
from glimpse_markets.streaming import _ws_url

from .api import Batch, Book, Glimpse, MarketRow, base_url

HOUR = 3600.0
DAY = 86400.0
BACKOFF_S = (0.5, 30.0)         # the first wait after the socket drops, and the longest: doubled each time, with jitter
MAX_SIZE = 2**24                # a message carries 500 ranges with names and shares: 44 KB, far under this
STRAY = 10.0                    # a close more than this many usual spacings past the farthest listed is a stray, as api.contiguous says


@dataclass(frozen=True)
class Event:
    """One thing that changed. `kind` is "book" (a close's shares moved), "launch" (a close newly listed), "list" (a batch
    re-listed) or "stream" (the socket connected or dropped: `topic_id` 1 when up, 0 when down)."""
    kind: str
    topic_id: int = 0
    batch_id: str = ""
    at: float = 0.0


class Watch:
    """Every open close of the watched batches, kept current: one WebSocket for all of them, polling to reconcile and to
    catch closes that open or shut, a burst of polls around the top of the hour when the hourly closes are launched."""

    def __init__(self, api: Glimpse, batches: list[Batch], list_s: float = 60.0, launch_s: tuple[float, float] = (-3.0, 150.0),
                 burst_s: float = 1.0, on_event: Callable[[Event], None] | None = None, url: str = "") -> None:
        self.api = api
        self.batches: dict[str, Batch] = {b.batch_id: b for b in batches}
        self.list_s, self.launch_s, self.burst_s = float(list_s), (float(launch_s[0]), float(launch_s[1])), float(burst_s)
        self.on_event = on_event
        self.url = url or _ws_url(base_url())
        self.rows: dict[str, list[MarketRow]] = {b.batch_id: [] for b in batches}
        self.books: dict[int, Book] = {}
        self.updated_at: dict[int, float] = {}
        self.listed_at: dict[str, float] = {}
        self.batch_of: dict[int, Batch] = {}
        self.farthest: dict[str, int] = {}          # batch id -> the latest end among its closes: a later one is a launch
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.streaming = False
        self.messages = 0
        self.launches = 0
        self.connect: Callable = websockets.connect
        self.backoff_s = BACKOFF_S
        self.now: Callable[[], float] = time.time
        self.log: deque[str] = deque(maxlen=400)
        self._pos: dict[int, dict[int, int]] = {}   # topic -> option id -> index into the book's shares
        self._adopting: set[int] = set()
        self._ignore: set[int] = set()              # topics the stream names that are not closes to hold: ended, or strays
        self._burst_base: dict[str, tuple[int, int]] = {}   # batch id -> (the period index the burst is in, farthest when it began)
        self._burst_done: dict[str, int] = {}               # batch id -> the period index whose launch was seen (or given up on)
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self.started = False

    # ── lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        """List every batch once, quietly, then run the stream, the listing poll and the launch bursts."""
        self._stopping = False
        await asyncio.gather(*(self.refresh_batch(b, announce=False) for b in self.batches))
        self.started = True
        self._tasks = [asyncio.create_task(self._stream(), name="watch-stream"),
                       asyncio.create_task(self._poll(), name="watch-poll"),
                       asyncio.create_task(self._bursts(), name="watch-bursts")]

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.streaming:
            self.streaming = False
            self._emit(Event("stream", 0, "", self.now()))

    def say(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S', time.gmtime())}  {msg}")

    def _emit(self, ev: Event) -> None:
        self.events.put_nowait(ev)
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception as e:  # a listener's bug must not stop the watch
                self.say(f"on_event: {type(e).__name__}: {e}")

    # ── reading ─────────────────────────────────────────────

    def open_closes(self, batch_id: str, now: float | None = None, stop_before_s: float = 0.0) -> list[Book]:
        """The batch's live closes with more than `stop_before_s` seconds to run, nearest first."""
        t = self.now() if now is None else now
        out = []
        for r in self.rows.get(batch_id, ()):
            b = self.books.get(r.topic_id)
            if b is not None and b.quote_mode == "live" and b.end_time_utc - t > stop_before_s:
                out.append(b)
        return out

    def age(self, topic_id: int) -> float:
        """Seconds since the close's shares were last confirmed by the stream or a poll, or since its batch was listed;
        infinite for a close the watch does not hold."""
        now = self.now()
        at = self.updated_at.get(topic_id)
        if at is None:
            batch = self.batch_of.get(topic_id)
            if batch is None or batch.batch_id not in self.listed_at:
                return float("inf")
            at = self.listed_at[batch.batch_id]
        return max(now - at, 0.0)

    def _index(self, topic_id: int) -> dict[int, int]:
        pos = self._pos.get(topic_id)
        if pos is None:
            pos = self._pos[topic_id] = {o: i for i, o in enumerate(self.books[topic_id].option_ids)}
        return pos

    def _mirror(self, topic_id: int, batch_id: str, shares: tuple[float, ...]) -> None:
        """The row in `rows` shows what the book shows: the smoothing bots read the series from rows."""
        rs = self.rows.get(batch_id)
        if not rs:
            return
        for i, r in enumerate(rs):
            if r.topic_id == topic_id:
                if r.shares != shares:
                    rs[i] = replace(r, shares=shares)
                return

    # ── the stream ──────────────────────────────────────────

    def apply_update(self, msg: dict) -> Event | None:
        """One `market_update` message into the books. A known close gets the message's shares by option id, is repriced
        and mirrored into its row; the Event is "book" if any share moved, else None (the book was merely confirmed). A close
        not yet known, in a watched batch, is a "launch" Event for the caller to adopt. Anything else is ignored."""
        if not isinstance(msg, dict) or msg.get("type") != "market_update":
            return None
        data = msg.get("data") or {}
        topic = int(msg.get("topic_id") or data.get("topic_id") or 0)
        batch_id = str(msg.get("batch_id") or data.get("batch_id") or "")
        if not batch_id and topic in self.batch_of:
            batch_id = self.batch_of[topic].batch_id
        if not topic or batch_id not in self.batches or topic in self._ignore:
            return None
        now = self.now()
        book = self.books.get(topic)
        if book is None:
            return Event("launch", topic, batch_id, now)
        pos = self._index(topic)
        shares = book.shares
        moved = False
        for q in data.get("quotes") or ():
            i = pos.get(int(q.get("option_id") or 0))
            s = q.get("shares")
            if i is None or s is None:
                continue
            s = float(s)
            if not math.isfinite(s) or s < 0:     # a share count is a finite non-negative number; anything else would poison the prices
                continue
            if shares[i] != s:
                shares[i] = s
                moved = True
        self.messages += 1
        self.updated_at[topic] = now
        if not moved:
            return None
        book.reprice()
        self._mirror(topic, batch_id, tuple(shares))
        return Event("book", topic, batch_id, now)

    async def adopt(self, topic_id: int, batch_id: str) -> Book | None:
        """A close the stream named before any listing did: read its book, give it a row, count the launch. None if it is
        already known or being read, if its batch is not watched, if the read fails, if it has already ended, or if it is a
        stray (more than STRAY usual spacings past the farthest listed close, the block the site does not list); the ended
        and the strays are remembered so their later messages cost nothing. A listing that lands during the read announces
        the close itself; then the fresher book is kept and nothing is announced twice."""
        if topic_id in self.books or topic_id in self._adopting or topic_id in self._ignore or batch_id not in self.batches:
            return None
        self._adopting.add(topic_id)
        try:
            book = await self.refresh_book(topic_id)
        except Exception as e:  # ApiError from the client, or a book the parser cannot read: one line, and the stream goes on
            self.say(f"launch {topic_id}: {type(e).__name__}: {e}")
            return None
        finally:
            self._adopting.discard(topic_id)
        batch = self.batches[batch_id]
        rs = self.rows.setdefault(batch_id, [])
        if any(r.topic_id == topic_id for r in rs):      # a listing landed during the read and announced it: the book is the fresher one
            self.batch_of.setdefault(topic_id, batch)
            self._mirror(topic_id, batch_id, tuple(book.shares))
            return book
        far = self.farthest.get(batch_id, 0)
        if book.end_time_utc <= self.now() or (far and book.end_time_utc > far + STRAY * self._spacing(batch)):
            why = "ended" if book.end_time_utc <= self.now() else "a stray, far past the series"
            self.say(f"launch {batch.short} {topic_id}: {why}, not held")
            self._forget(topic_id)
            if len(self._ignore) > 10_000:
                self._ignore.clear()
            self._ignore.add(topic_id)
            return None
        self.batch_of[topic_id] = batch
        sibling = next((r for r in rs if len(r.names) == len(book.bins)), None)
        names = sibling.names if sibling is not None else tuple(f"{lo:g}-{hi:g}" for lo, hi in book.bins)
        row = MarketRow(topic_id, book.title, book.end_time_utc, book.quote_mode, book.volume_msat, 0, tuple(book.shares), names,
                        tuple(book.option_ids), book.close_block, book.trading_stops_block)
        rs.append(row)
        rs.sort(key=lambda r: (r.end_time_utc, r.topic_id))
        self.farthest[batch_id] = max(self.farthest.get(batch_id, 0), book.end_time_utc)
        self.launches += 1
        self.say(f"launch {batch.short} {topic_id}: {book.title}")
        self._emit(Event("launch", topic_id, batch_id, self.now()))
        return book

    def _spacing(self, batch: Batch) -> float:
        """The usual gap between the batch's closes: the median of what is listed, else how often it launches."""
        ends = sorted(r.end_time_utc for r in self.rows.get(batch.batch_id, ()))
        gaps = sorted(b - a for a, b in zip(ends, ends[1:], strict=False) if b > a)
        return float(gaps[len(gaps) // 2]) if gaps else self.period_of(batch)

    def backoff(self, attempt: int) -> float:
        """How long to wait before the `attempt`th reconnect: 0.5 s doubled each time up to 30 s, each scaled by 0.5 to 1.5
        so many watchers do not knock together, and never over the cap."""
        lo, cap = self.backoff_s
        return min(lo * 2.0 ** max(attempt - 1, 0) * (0.5 + random.random()), cap)

    async def _stream(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                async with self.connect(self.url, max_size=MAX_SIZE, open_timeout=10, ping_interval=25) as ws:
                    attempt = 0
                    self.streaming = True
                    self._emit(Event("stream", 1, "", self.now()))
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        try:
                            ev = self.apply_update(msg)
                        except Exception as e:  # one unreadable message must not drop the socket
                            self.say(f"stream message: {type(e).__name__}: {e}")
                            continue
                        if ev is not None:
                            if ev.kind == "launch":
                                self._tasks.append(asyncio.create_task(self.adopt(ev.topic_id, ev.batch_id)))
                                self._tasks = [t for t in self._tasks if not t.done()]
                            else:
                                self._emit(ev)
                        if self._stopping:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # the socket dropped or refused: say so and come back
                self.say(f"stream: {type(e).__name__}: {e}")
            if self.streaming:
                self.streaming = False
                self._emit(Event("stream", 0, "", self.now()))
            if self._stopping:
                break
            attempt += 1
            await asyncio.sleep(self.backoff(attempt))

    # ── polling ─────────────────────────────────────────────

    async def refresh_batch(self, batch_id: str, announce: bool = True) -> list[MarketRow]:
        """`all_markets` into `rows` and `books`. A close not seen before gets a book and, once the watch has started, a
        "launch" Event; one whose shares differ from the book's gets a "book" Event; one that has gone (closed, or ended) is
        dropped. A "list" Event says the batch was re-listed. `announce` False, the initial listing, raises no Event."""
        batch = self.batches.get(batch_id)
        if batch is None:
            return []
        asked = self.now()
        try:
            got = await self.api.all_markets(batch_id)
        except Exception as e:  # ApiError from the client, or a listing the parser cannot read: the rows stand
            self.say(f"list {batch.short}: {type(e).__name__}: {e}")
            return self.rows.get(batch_id, [])
        if not got and self.rows.get(batch_id):      # a series never empties: every close gone at once is a bad answer
            self.say(f"list {batch.short}: empty answer, keeping {len(self.rows[batch_id])} closes")
            return self.rows[batch_id]
        now = self.now()
        announce = announce and self.started
        launched: list[int] = []
        moved: list[int] = []
        rows: list[MarketRow] = []
        for r in got:
            book = self.books.get(r.topic_id)
            self._ignore.discard(r.topic_id)
            if book is None:
                self.books[r.topic_id] = Book.from_row(r)
                self.batch_of[r.topic_id] = batch
                launched.append(r.topic_id)
                self.updated_at[r.topic_id] = now
            elif self.updated_at.get(r.topic_id, 0) > asked:
                # The stream (or a book read) confirmed this close after the request went out: the listing's shares are the
                # older ones, so the book stands and the row shows it.
                r = replace(r, shares=tuple(book.shares))
            else:
                if tuple(book.shares) != r.shares:
                    book.shares = list(r.shares)
                    book.reprice()
                    moved.append(r.topic_id)
                book.volume_msat, book.quote_mode, book.end_time_utc = r.volume_msat, r.quote_mode, r.end_time_utc
                self.updated_at[r.topic_id] = now
            rows.append(r)
        keep = {r.topic_id for r in got}
        for r in self.rows.get(batch_id, ()):
            if r.topic_id in keep:
                continue
            if self.updated_at.get(r.topic_id, 0) > asked and r.topic_id in self.books:
                rows.append(r)                       # adopted or confirmed since the request went out: not gone, just younger than the list
            else:
                self._forget(r.topic_id)
        rows.sort(key=lambda r: (r.end_time_utc, r.topic_id))
        self.rows[batch_id] = rows
        self.listed_at[batch_id] = now
        if got:
            self.farthest[batch_id] = max(self.farthest.get(batch_id, 0), max(r.end_time_utc for r in got))
        if announce:
            for t in launched:
                self.launches += 1
                self.say(f"launch {batch.short} {t}")
                self._emit(Event("launch", t, batch_id, now))
            for t in moved:
                self._emit(Event("book", t, batch_id, now))
            self._emit(Event("list", 0, batch_id, now))
        return self.rows[batch_id]

    def _forget(self, topic_id: int) -> None:
        self.books.pop(topic_id, None)
        self._pos.pop(topic_id, None)
        self.updated_at.pop(topic_id, None)
        self.batch_of.pop(topic_id, None)

    async def refresh_book(self, topic_id: int) -> Book:
        """The close's book read from the server, into `books` and its row: what the desk reads before it trades. Raises
        `ApiError` as the client does; no Event, since the reader is about to act on it."""
        book = await self.api.book(topic_id)
        old = self.books.get(topic_id)
        if old is not None:
            if not book.title:
                book.title = old.title
            book.close_block, book.trading_stops_block = old.close_block, old.trading_stops_block
        self.books[topic_id] = book
        self._pos.pop(topic_id, None)
        self.updated_at[topic_id] = self.now()
        batch = self.batch_of.get(topic_id)
        if batch is not None:
            self._mirror(topic_id, batch.batch_id, tuple(book.shares))
        return book

    async def _poll(self) -> None:
        """Every `list_s` each batch is re-listed, one at a time, spread over the interval."""
        while not self._stopping:
            for batch_id in list(self.batches):
                await asyncio.sleep(self.list_s / max(len(self.batches), 1))
                if self._stopping:
                    return
                try:
                    await self.refresh_batch(batch_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # refresh_batch catches its own reads; this is the last guard, so the poll never dies
                    self.say(f"poll {batch_id}: {type(e).__name__}: {e}")

    # ── the launch burst ────────────────────────────────────

    @staticmethod
    def period_of(batch: Batch) -> float:
        """How often the batch launches a close: hourly series every hour, daily ones every day at 00:00 UTC."""
        return HOUR if batch.hourly else DAY

    def in_window(self, batch: Batch, now: float) -> bool:
        """Inside the launch window: from `launch_s[0]` seconds past the hour (a few seconds before it, when negative) to
        `launch_s[1]` past it."""
        start, end = self.launch_s
        return (now - start) % self.period_of(batch) <= end - start

    async def probe_batch(self, batch_id: str) -> bool:
        """The launch burst's read: the end of the batch's list, from one before the last close still open, in one small page
        (`api.markets_from`); a close not known yet is taken in with its book and announced as a launch. True when one was.
        Falls back to a full re-list on a client without the small read."""
        batch = self.batches.get(batch_id)
        if batch is None:
            return False
        now = self.now()
        probe = getattr(self.api, "markets_from", None)
        if probe is None:
            before = self.farthest.get(batch_id, 0)
            await self.refresh_batch(batch_id)
            return self.farthest.get(batch_id, 0) > before
        rs = self.rows.setdefault(batch_id, [])
        open_ = sum(1 for r in rs if r.end_time_utc > now)
        try:
            got = await probe(batch_id, max(open_ - 1, 0))
        except Exception as e:  # ApiError from the client, or an answer the parser cannot read: the next second tries again
            self.say(f"probe {batch.short}: {type(e).__name__}: {e}")
            return False
        far = self.farthest.get(batch_id, 0)
        taken = False
        for r in got:
            if r.topic_id in self.books or r.topic_id in self._ignore or r.end_time_utc <= now or len(r.shares) < 2:
                continue
            if far and r.end_time_utc > far + STRAY * self._spacing(batch):
                continue                                  # a stray far past the series, the block the site does not list
            self.books[r.topic_id] = Book.from_row(r)
            self.batch_of[r.topic_id] = batch
            self.updated_at[r.topic_id] = now
            rs.append(r)
            rs.sort(key=lambda x: (x.end_time_utc, x.topic_id))
            self.farthest[batch_id] = max(far, r.end_time_utc)
            self.launches += 1
            self.say(f"launch {batch.short} {r.topic_id}: probed")
            self._emit(Event("launch", r.topic_id, batch_id, now))
            taken = True
        return taken

    async def burst_tick(self, now: float | None = None) -> float:
        """One pass of the launch burst: every batch inside its window whose launch has not been seen this period is re-listed.
        A close later than the farthest known when the window opened ends the burst for that batch; so does the window closing.
        Returns how long to wait before the next pass: `burst_s` while any batch is bursting, else the time to the next window."""
        now = self.now() if now is None else now
        start, end = self.launch_s
        busy = False
        wait = float("inf")
        for batch_id, batch in self.batches.items():
            period = self.period_of(batch)
            k = int((now - start) // period)              # the window this instant belongs to (or the last one opened)
            if self._burst_done.get(batch_id) == k:
                wait = min(wait, (k + 1) * period + start - now)
                continue
            if not self.in_window(batch, now):
                wait = min(wait, period - (now - start) % period)
                continue
            base_k, base = self._burst_base.get(batch_id, (-1, 0))
            if base_k != k:
                base = self.farthest.get(batch_id, 0)
                self._burst_base[batch_id] = (k, base)
            await self.probe_batch(batch_id)
            if self.farthest.get(batch_id, 0) > base:
                self.say(f"launch burst {batch.short}: new close seen")
            elif (now - start) % period + self.burst_s > end - start:
                self.say(f"launch burst {batch.short}: window closed without a new close")
            else:
                busy = True
                continue
            self._burst_done[batch_id] = k
            wait = min(wait, (k + 1) * period + start - now)
        if busy:
            return self.burst_s
        return max(min(wait, DAY), 0.0) if wait != float("inf") else DAY

    async def _bursts(self) -> None:
        while not self._stopping:
            try:
                wait = await self.burst_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a bad read is one line; the next window is tried again
                self.say(f"launch burst: {type(e).__name__}: {e}")
                wait = self.burst_s
            await asyncio.sleep(wait)
