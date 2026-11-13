"""The watch against a fake API and a fake WebSocket: no network, no key, no orders."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from glimpse_tui import pricing as P
from glimpse_tui import watch as W
from glimpse_tui.api import ApiError, Batch, Book, MarketRow, parse_bin

FIX = json.loads((Path(__file__).parent / "fixtures" / "estimates.json").read_text())
BINS = [parse_bin(n) for n in FIX["names"]]
IDS = tuple(range(1, 501))
T0 = 1_800_000_000                                   # a round second to hang the fake clock on: 2027-01-15 08:00:00 UTC
HOURLY = Batch("b-h", "Hourly Bitcoin Prediction Markets", 3, 1000)
DAILY = Batch("b-d", "Daily Bitcoin Prediction Markets", 3, 1000)


class FakeApi:
    """Two series over the fixture ladder: three hourly closes and two daily ones. Tests move shares, add a close or take
    one away between calls, and count what the watch asked for."""

    def __init__(self, now: float = T0):
        self.now = now
        self.ends = {"b-h": {100 + i: int(now) + (i + 1) * 3600 for i in range(3)},
                     "b-d": {200 + i: int(now) + (i + 1) * 86400 for i in range(2)}}
        self.shares = {t: list(FIX["shares"]) for ends in self.ends.values() for t in ends}
        self.lists: list[str] = []
        self.booked: list[int] = []
        self.fail_book = False

    def batch_of(self, topic_id: int) -> str:
        return next(b for b, ends in self.ends.items() if topic_id in ends)

    async def all_markets(self, batch_id):
        self.lists.append(batch_id)
        ends = self.ends[batch_id]
        return [MarketRow(t, f"{batch_id} close {t}", e, "live", 0, 0, tuple(self.shares[t]), tuple(FIX["names"]), IDS)
                for t, e in sorted(ends.items(), key=lambda x: x[1])]

    async def book(self, topic_id):
        self.booked.append(topic_id)
        if self.fail_book:
            raise ApiError("Glimpse timed out.")
        end = self.ends[self.batch_of(topic_id)][topic_id]
        b = Book(topic_id, f"book {topic_id}", end, "live", 0, list(IDS), BINS, list(self.shares[topic_id]), P.alpha_for(500))
        b.reprice()
        return b

    def add_close(self, batch_id: str) -> int:
        """The next close of the series, as the server lists it about thirty seconds past the hour."""
        ends = self.ends[batch_id]
        t = max(ends) + 1
        ends[t] = max(ends.values()) + (3600 if batch_id == "b-h" else 86400)
        self.shares[t] = list(FIX["shares"])
        return t


class FakeSocket:
    """What `connect(url)` hands back: `async with` gives an object whose `async for` yields the messages of one connection,
    then ends, as a server closing the socket would. Each call takes the next list of messages; a connection past the last
    list waits until `release` is set, as a quiet socket would."""

    def __init__(self, *connections: list[dict]):
        self.connections = list(connections)
        self.opened = 0
        self.urls: list[str] = []
        self.kwargs: list[dict] = []
        self.release = asyncio.Event()

    def __call__(self, url, **kw):
        self.urls.append(url)
        self.kwargs.append(kw)
        return self

    async def __aenter__(self):
        self.opened += 1
        self.batch = self.connections.pop(0) if self.connections else None
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        if self.batch is None:
            await self.release.wait()
            return
        for m in self.batch:
            yield json.dumps(m)
            await asyncio.sleep(0)


def update(topic: int, batch_id: str, moved: dict[int, float], base=None) -> dict:
    """A `market_update` as the firehose sends it: every range's shares, the moved ones at their new values."""
    shares = list(base if base is not None else FIX["shares"])
    for o, s in moved.items():
        shares[o - 1] = s
    return {"type": "market_update", "topic_id": topic, "batch_id": batch_id,
            "data": {"topic_id": topic, "batch_id": batch_id, "alpha": P.alpha_for(500), "timestamp": T0,
                     "quotes": [{"option_id": o, "option_name": n, "shares": s} for o, n, s in zip(IDS, FIX["names"], shares, strict=True)]}}


def watch(api, **kw) -> W.Watch:
    """A watch on the fake clock. The clock sits exactly on the hour, inside the launch window the watch opens 3 s before it,
    so unless a test is about the burst the window is moved to 20 s past the hour and the burst to every 2 s, out of its way."""
    kw = {"launch_s": (20.0, 150.0), "burst_s": 2.0, **kw}
    w = W.Watch(api, [HOURLY, DAILY], url="ws://test/ws/nmarket-updates", **kw)
    w.now = lambda: api.now
    w.backoff_s = (0.001, 0.002)
    return w


def drain(w: W.Watch) -> list[W.Event]:
    out = []
    while not w.events.empty():
        out.append(w.events.get_nowait())
    return out


async def until(cond, timeout=2.0):
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timed out waiting"
        await asyncio.sleep(0.005)


def test_the_stream_url_is_the_api_base_with_the_scheme_swapped(monkeypatch):
    monkeypatch.setenv("GLIMPSE_BASE_URL", "https://main.bpmapi.io/")
    w = W.Watch(FakeApi(), [HOURLY])
    assert w.url == "wss://main.bpmapi.io/ws/nmarket-updates"
    assert w.connect is W.websockets.connect


async def test_start_lists_every_batch_and_fills_the_books_before_any_event():
    api = FakeApi()
    sock = FakeSocket()
    w = watch(api)
    w.connect = sock
    await w.start()
    try:
        assert sorted(w.books) == [100, 101, 102, 200, 201]
        assert [r.topic_id for r in w.rows["b-h"]] == [100, 101, 102] and [r.topic_id for r in w.rows["b-d"]] == [200, 201]
        assert w.batch_of[201] is DAILY and w.farthest["b-h"] == api.ends["b-h"][102]
        assert all(b.prices and abs(sum(b.probs) - 1) < 1e-6 for b in w.books.values())
        assert w.launches == 0 and api.lists == ["b-h", "b-d"]
        await until(lambda: w.streaming)
        assert [e.kind for e in drain(w)] == ["stream"]                 # the socket coming up is the first event
        assert sock.urls == ["ws://test/ws/nmarket-updates"] and sock.kwargs[0]["max_size"] == 2**24
    finally:
        sock.release.set()
        await w.stop()
    assert not w.streaming and all(t.done() for t in w._tasks) or w._tasks == []


async def test_apply_update_moves_shares_and_prices_mirrors_the_row_and_says_so():
    api = FakeApi()
    w = watch(api)
    await w.refresh_batch("b-h", announce=False)
    before = list(w.books[100].prices)
    ev = w.apply_update(update(100, "b-h", {91: 480.0, 92: 300.0}))
    assert ev is not None and ev.kind == "book" and (ev.topic_id, ev.batch_id, ev.at) == (100, "b-h", T0)
    b = w.books[100]
    assert b.shares[90] == 480.0 and b.shares[91] == 300.0 and b.prices[90] > before[90] and b.prices[0] < before[0]
    assert abs(sum(b.probs) - 1) < 1e-6
    row = next(r for r in w.rows["b-h"] if r.topic_id == 100)
    assert row.shares[90] == 480.0 and row.shares == tuple(b.shares) and row.names == tuple(FIX["names"])
    assert w.updated_at[100] == T0 and w.messages == 1 and w.age(100) == 0.0
    # The same message again confirms the book and moves nothing: no event, but the book is counted as fresh.
    api.now = T0 + 7
    assert w.apply_update(update(100, "b-h", {91: 480.0, 92: 300.0})) is None
    assert w.messages == 2 and w.updated_at[100] == T0 + 7 and w.age(101) == 7.0       # 101 falls back to the listing time
    # Noise is ignored: another type, a batch not watched, a topic with no id.
    assert w.apply_update({"type": "heartbeat"}) is None
    assert w.apply_update(update(100, "b-other", {91: 1.0})) is None
    assert w.apply_update({"type": "market_update", "batch_id": "b-h", "data": {}}) is None
    assert w.messages == 2


async def test_an_unknown_close_in_a_watched_batch_is_adopted_from_its_book():
    api = FakeApi()
    w = watch(api)
    await w.refresh_batch("b-h", announce=False)
    t = api.add_close("b-h")
    ev = w.apply_update(update(t, "b-h", {}))
    assert ev is not None and ev.kind == "launch" and ev.topic_id == t
    assert t not in w.books and w.launches == 0                              # pure: nothing is fetched until adopted
    book = await w.adopt(t, "b-h")
    assert book is w.books[t] and api.booked == [t] and w.launches == 1 and w.batch_of[t] is HOURLY
    assert [r.topic_id for r in w.rows["b-h"]] == [100, 101, 102, t] and w.rows["b-h"][-1].names == tuple(FIX["names"])
    assert w.farthest["b-h"] == api.ends["b-h"][t]
    assert [(e.kind, e.topic_id) for e in drain(w)] == [("launch", t)]
    assert await w.adopt(t, "b-h") is None and w.launches == 1              # known now: nothing more
    assert await w.adopt(999, "b-other") is None and api.booked == [t]
    api.fail_book = True
    t2 = api.add_close("b-h")
    assert await w.adopt(t2, "b-h") is None and w.launches == 1 and "timed out" in w.log[-1]


async def test_the_stream_applies_messages_adopts_launches_and_reconnects_with_backoff():
    api = FakeApi()
    t = api.add_close("b-h")                                                   # listed by the server, not yet by the watch
    api.ends["b-h"].pop(t)
    sock = FakeSocket([update(100, "b-h", {91: 480.0}), update(300, "b-other", {91: 480.0})], [update(101, "b-h", {50: 200.0})])
    w = watch(api)
    w.connect = sock
    await w.start()
    try:
        await until(lambda: sock.opened >= 3)                                  # two connections ended, the third is quiet
        assert w.books[100].shares[90] == 480.0 and w.books[101].shares[49] == 200.0 and w.messages == 2
        kinds = [(e.kind, e.topic_id) for e in drain(w)]
        assert kinds.count(("stream", 1)) == 3 and kinds.count(("stream", 0)) == 2
        assert ("book", 100) in kinds and ("book", 101) in kinds
        assert w.streaming
        # A launch through the socket: the book is read and the close takes its place in the series.
        api.ends["b-h"][t] = max(api.ends["b-h"].values()) + 3600
        ev = w.apply_update(update(t, "b-h", {}))
        assert ev is not None and ev.kind == "launch"
    finally:
        sock.release.set()
        await w.stop()
    assert not w.streaming and [e.kind for e in drain(w)] == ["stream"]


async def test_a_listing_announces_new_closes_moved_books_and_drops_the_closed():
    api = FakeApi()
    w = watch(api)
    await w.start()
    try:
        drain(w)
        api.ends["b-h"].pop(100)                                               # the nearest close has settled
        api.shares[101][90] += 50                                              # a trade the stream missed
        t = api.add_close("b-h")
        api.now = T0 + 30
        rows = await w.refresh_batch("b-h")
        assert [r.topic_id for r in rows] == [101, 102, t] and rows is w.rows["b-h"]
        assert 100 not in w.books and 100 not in w.batch_of and 100 not in w.updated_at
        assert w.books[101].shares[90] == FIX["shares"][90] + 50 and w.updated_at[102] == T0 + 30 and w.listed_at["b-h"] == T0 + 30
        assert w.launches == 1 and w.books[t].end_time_utc == api.ends["b-h"][t]
        assert [(e.kind, e.topic_id) for e in drain(w)] == [("launch", t), ("book", 101), ("list", 0)]
        api.now = T0 + 31
        assert w.age(t) == 1.0 and w.age(101) == 1.0
    finally:
        await w.stop()


async def test_a_failed_listing_keeps_the_rows_and_notes_it():
    api = FakeApi()
    w = watch(api)
    await w.refresh_batch("b-h", announce=False)

    async def boom(batch_id):
        raise ApiError("Cannot reach Glimpse. Check the connection.")
    api.all_markets = boom
    rows = await w.refresh_batch("b-h")
    assert [r.topic_id for r in rows] == [100, 101, 102] and "Cannot reach" in w.log[-1] and drain(w) == []
    assert await w.refresh_batch("b-none") == []


async def test_the_launch_burst_polls_only_inside_the_window_until_a_later_close_appears():
    api = FakeApi()
    w = watch(api, launch_s=(20.0, 150.0), burst_s=2.0)
    await w.start()
    try:
        api.lists.clear()
        hour = T0 - T0 % 3600 + 3600
        api.now = hour + 5                                                     # before the window: wait for it
        assert await w.burst_tick() == pytest.approx(15.0) and api.lists == []
        api.now = hour + 25                                                    # in the window, nothing new yet: poll and come back in 2 s
        assert await w.burst_tick() == 2.0 and api.lists == ["b-h"]            # the daily batch is not in a window at 08:00
        api.now = hour + 27
        assert await w.burst_tick() == 2.0 and api.lists == ["b-h", "b-h"]
        t = api.add_close("b-h")                                               # the server seeds the next close
        api.now = hour + 29
        wait = await w.burst_tick()
        assert api.lists == ["b-h", "b-h", "b-h"] and w.launches == 1 and t in w.books
        assert wait == pytest.approx(3600 + 20 - 29)                           # done for this hour: sleep to the next window
        api.now = hour + 31
        assert await w.burst_tick() == pytest.approx(3600 + 20 - 31) and len(api.lists) == 3
        # The next hour with no new close: the burst gives up when the window closes.
        api.now = hour + 3600 + 149
        assert await w.burst_tick() == pytest.approx(3600 + 20 - 149) and api.lists[-1] == "b-h" and len(api.lists) == 4
        assert "window closed" in w.log[-1]
    finally:
        await w.stop()


async def test_daily_batches_burst_in_the_first_window_after_midnight():
    api = FakeApi()
    w = watch(api)
    await w.start()
    try:
        api.lists.clear()
        midnight = T0 - T0 % 86400 + 86400
        api.now = midnight + 40
        assert await w.burst_tick() == 2.0 and sorted(api.lists) == ["b-d", "b-h"]      # both series launch on the hour
        api.add_close("b-d")
        api.now = midnight + 42
        await w.burst_tick()
        assert w.launches == 1 and api.lists.count("b-d") == 2 and w.batch_of[max(api.ends["b-d"])] is DAILY
        api.now = midnight + 44
        await w.burst_tick()
        assert api.lists.count("b-d") == 2 and api.lists.count("b-h") == 3              # the daily burst is done, the hourly goes on
    finally:
        await w.stop()


async def test_open_closes_honours_stop_before_s_and_the_quote_mode():
    api = FakeApi()
    w = watch(api)
    await w.refresh_batch("b-h", announce=False)
    assert [b.topic_id for b in w.open_closes("b-h")] == [100, 101, 102]
    assert [b.topic_id for b in w.open_closes("b-h", stop_before_s=3600)] == [101, 102]
    assert [b.topic_id for b in w.open_closes("b-h", now=T0 + 2 * 3600 + 1)] == [102]
    w.books[102].quote_mode = "paused"
    assert [b.topic_id for b in w.open_closes("b-h", stop_before_s=3600)] == [101]
    assert w.open_closes("b-d") == []


async def test_refresh_book_is_the_authoritative_read_and_mirrors_the_row_without_an_event():
    api = FakeApi()
    w = watch(api)
    await w.start()
    try:
        drain(w)
        api.shares[100][10] += 25
        api.now = T0 + 3
        b = await w.refresh_book(100)
        assert b is w.books[100] and b.shares[10] == FIX["shares"][10] + 25 and api.booked == [100]
        assert w.rows["b-h"][0].shares[10] == FIX["shares"][10] + 25 and w.updated_at[100] == T0 + 3 and drain(w) == []
        ev = w.apply_update(update(100, "b-h", {}, base=api.shares[100]))
        assert ev is None                                                      # the stream agrees with the fresh read
        with pytest.raises(ApiError):
            api.fail_book = True
            await w.refresh_book(101)
    finally:
        await w.stop()


async def test_stop_cancels_a_hanging_socket_cleanly():
    api = FakeApi()
    sock = FakeSocket()                                                        # connects, then yields nothing until released
    w = watch(api)
    w.connect = sock
    await w.start()
    await until(lambda: w.streaming)
    tasks = list(w._tasks)
    await asyncio.wait_for(w.stop(), 1.0)
    assert all(t.done() for t in tasks) and w._tasks == [] and not w.streaming
    assert [e.kind for e in drain(w)] == ["stream", "stream"]
    await w.stop()                                                             # a second stop is harmless


def test_backoff_doubles_from_half_a_second_to_the_cap_with_jitter():
    w = W.Watch(FakeApi(), [HOURLY], url="ws://x/ws")
    waits = [w.backoff(k) for k in range(1, 12)]
    assert 0.25 <= waits[0] <= 0.75 and 1.0 <= waits[2] <= 3.0 and all(x <= 30.0 for x in waits) and waits[-1] >= 15.0


def test_on_event_hears_every_event_and_its_bugs_do_not_stop_the_watch():
    heard = []

    def on_event(ev):
        heard.append(ev)
        raise RuntimeError("listener bug")
    w = W.Watch(FakeApi(), [HOURLY], url="ws://x/ws", on_event=on_event)
    w._emit(W.Event("list", 0, "b-h", T0))
    assert heard[0].kind == "list" and w.events.qsize() == 1 and "listener bug" in w.log[-1]


async def test_a_listing_that_lands_during_an_adoption_does_not_double_the_close():
    """The launch burst lists every two seconds while the stream delivers the new close's first trade: whichever read
    finishes second must find the row the first one made, not add another."""
    api = FakeApi()
    w = watch(api)
    await w.start()
    drain(w)
    t = api.add_close("b-h")
    gate = asyncio.Event()
    real_book = api.book

    async def slow_book(topic):
        await gate.wait()
        return await real_book(topic)
    api.book = slow_book
    try:
        assert w.apply_update(update(t, "b-h", {})).kind == "launch"
        task = asyncio.create_task(w.adopt(t, "b-h"))
        await asyncio.sleep(0)                                                 # the adoption is waiting on its book read
        await w.refresh_batch("b-h")                                           # the burst's listing lands first
        gate.set()
        assert await task is w.books[t]
        assert [r.topic_id for r in w.rows["b-h"]] == [100, 101, 102, t] and w.launches == 1
        assert [(e.kind, e.topic_id) for e in drain(w)] == [("launch", t), ("list", 0)]
        # The other order: a listing that went out before the server seeded the close must not drop it once adopted.
        t2 = api.add_close("b-h")
        api.ends["b-h"].pop(t2)
        stale = asyncio.Event()
        real_list = api.all_markets

        async def slow_list(batch_id):
            rows = await real_list(batch_id)                                   # the answer the server composed when asked
            await stale.wait()
            return rows
        api.all_markets = slow_list
        listing = asyncio.create_task(w.refresh_batch("b-h"))
        await asyncio.sleep(0)
        api.ends["b-h"][t2] = max(api.ends["b-h"].values()) + 3600
        api.now = T0 + 1
        assert await w.adopt(t2, "b-h") is not None
        stale.set()
        await listing
        assert t2 in w.books and [r.topic_id for r in w.rows["b-h"]] == [100, 101, 102, t, t2] and w.launches == 2
    finally:
        await w.stop()


async def test_a_slow_listing_does_not_overwrite_what_the_stream_moved_since():
    api = FakeApi()
    w = watch(api)
    await w.start()
    drain(w)
    gate = asyncio.Event()
    real_list = api.all_markets

    async def slow_list(batch_id):
        rows = await real_list(batch_id)
        await gate.wait()                                                      # nine seconds on a slow day
        return rows
    api.all_markets = slow_list
    try:
        listing = asyncio.create_task(w.refresh_batch("b-h"))
        await asyncio.sleep(0)
        api.now = T0 + 4
        assert w.apply_update(update(100, "b-h", {91: 480.0})).kind == "book"  # a trade while the listing is in flight
        api.shares[100][90] = 480.0
        gate.set()
        await listing
        assert w.books[100].shares[90] == 480.0 and w.rows["b-h"][0].shares[90] == 480.0
        assert w.updated_at[100] == T0 + 4 and w.updated_at[101] == T0 + 4     # 101 was confirmed by the listing itself
        assert [(e.kind, e.topic_id) for e in drain(w)] == [("list", 0)]          # no stale "book" event for 100
    finally:
        await w.stop()


async def test_an_empty_listing_is_a_bad_answer_not_a_series_that_closed():
    api = FakeApi()
    w = watch(api)
    await w.start()
    drain(w)

    async def empty(batch_id):
        return []
    real = api.all_markets
    api.all_markets = empty
    try:
        assert [r.topic_id for r in await w.refresh_batch("b-h")] == [100, 101, 102] and sorted(w.books) == [100, 101, 102, 200, 201]
        assert "empty answer" in w.log[-1] and drain(w) == []
        api.all_markets = real
        await w.refresh_batch("b-h")
        assert w.launches == 0 and [e.kind for e in drain(w)] == ["list"]      # nothing is relaunched
    finally:
        await w.stop()


async def test_a_bad_share_count_is_skipped_and_a_bad_listing_does_not_kill_the_poll():
    api = FakeApi()
    w = watch(api, list_s=0.02)
    await w.start()
    drain(w)
    try:
        assert w.apply_update(update(100, "b-h", {91: float("nan"), 92: -5.0, 93: float("inf")})) is None
        assert all(x == y for x, y in zip(w.books[100].shares, FIX["shares"], strict=True)) and abs(sum(w.books[100].probs) - 1) < 1e-6
        ev = w.apply_update(update(100, "b-h", {91: float("nan"), 92: 300.0}))
        assert ev is not None and ev.kind == "book" and w.books[100].shares[91] == 300.0 and w.books[100].shares[90] == FIX["shares"][90]

        async def boom(batch_id):
            raise ValueError("a range name the parser cannot read")
        api.all_markets = boom
        await until(lambda: any("ValueError" in line for line in w.log))
        poll = next(t for t in w._tasks if t.get_name() == "watch-poll")
        assert not poll.done() and [r.topic_id for r in w.rows["b-h"]] == [100, 101, 102]
    finally:
        await w.stop()


async def test_strays_and_ended_closes_named_by_the_stream_are_not_held_and_cost_one_read():
    api = FakeApi()
    w = watch(api)
    await w.start()
    drain(w)
    try:
        stray = 9000                                                           # the batch endpoint's block of closes fifty days out
        api.ends["b-h"][stray] = int(api.now) + 50 * 86400
        api.shares[stray] = list(FIX["shares"])
        assert w.apply_update(update(stray, "b-h", {})).kind == "launch"
        assert await w.adopt(stray, "b-h") is None and stray not in w.books and stray not in w.batch_of
        assert w.farthest["b-h"] == api.ends["b-h"][102] and w.launches == 0 and "stray" in w.log[-1]
        assert w.apply_update(update(stray, "b-h", {})) is None and len(api.booked) == 1   # remembered: no second read
        # A close that ended: the listing drops it, the stream still sends its settlement, one read says it is over.
        api.ends["b-h"].pop(100)
        api.now = T0 + 3601
        await w.refresh_batch("b-h")
        api.ends["b-h"][100] = T0 + 3600
        api.booked.clear()
        for _ in range(3):
            ev = w.apply_update(update(100, "b-h", {}))
            if ev is not None:
                assert await w.adopt(100, "b-h") is None
        assert api.booked == [100] and 100 not in w.books and "ended" in w.log[-1]
        # Listed again by the server (it was a hiccup): the listing wins and the topic is held once more.
        api.ends["b-h"][100] = T0 + 7200
        await w.refresh_batch("b-h")
        assert 100 in w.books and w.apply_update(update(100, "b-h", {91: 1.0})).kind == "book"
        assert w.age(424242) == float("inf")                                   # a close the watch does not hold was never confirmed
    finally:
        await w.stop()


class Probing(FakeApi):
    """A client with the small end-of-list read the burst prefers: `markets_from`, counted apart from full listings."""

    def __init__(self, now: float = T0):
        super().__init__(now)
        self.probes: list[tuple[str, int]] = []

    async def markets_from(self, batch_id, offset, limit=3):
        self.probes.append((batch_id, offset))
        rows = await super().all_markets(batch_id)
        self.lists.pop()                                                       # not a full listing
        return rows[offset:offset + limit]


async def test_the_launch_burst_probes_the_end_of_the_list_from_just_before_the_hour():
    api = Probing()
    w = watch(api, launch_s=(-3.0, 150.0), burst_s=1.0)
    await w.start()
    try:
        api.lists.clear()
        hour = T0 - T0 % 3600 + 3600
        api.now = hour - 10                                                    # before the window opens, 3 s before the hour
        assert await w.burst_tick() == pytest.approx(7.0) and api.probes == [] and api.lists == []
        api.now = hour - 2                                                     # the window is open: probe, nothing new yet
        assert await w.burst_tick() == 1.0 and api.probes == [("b-h", 2)] and api.lists == []   # from one before the last open close
        api.now = hour + 0.5                                                   # the hh:00 close has ended; the server seeds the next
        t = api.add_close("b-h")
        del api.ends["b-h"][100]
        assert await w.burst_tick() == pytest.approx(3600 - 3 - 0.5)
        assert api.probes[-1] == ("b-h", 1) and api.lists == []               # one small read, no full listing
        assert t in w.books and w.launches == 1 and w.batch_of[t].batch_id == "b-h"
        events = []
        while not w.events.empty():
            events.append(w.events.get_nowait())
        assert [e.kind for e in events if e.topic_id == t] == ["launch"]
        assert w.rows["b-h"][-1].topic_id == t and w.farthest["b-h"] == api.ends["b-h"][t]
        api.now = hour + 5
        assert await w.burst_tick() == pytest.approx(3600 - 3 - 5) and len(api.probes) == 2   # done for this hour
    finally:
        await w.stop()
