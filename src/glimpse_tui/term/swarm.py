"""The swarm: what every bot on this machine thinks of one Glimpse series, computed once and shared by the pages.

Glimpse is a prediction market with machines in it, so the terminal's front page is half bots. The zoo's models
run here, on public hourly candles: a whole pass over 130 pictures of three closes costs about a second, and
nothing leaves the computer. One pass per series per hourly bar is kept; `CONS` reads the counts and the
consensus, `BOT` reads one picture at a time.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..botsview import Scan

# (seconds ahead, how the row is labelled) for an hourly series and for a daily one.
HOURLY = ((3600.0, "next hour"), (86400.0, "in 24 hours"), (3 * 86400.0, "in 72 hours"))
DAILY = ((86400.0, "next close"), (7 * 86400.0, "in a week"), (30 * 86400.0, "in a month"))
BULL, BEAR, FLAT = "bullish", "bearish", "neutral"
STANCE = {"bullish": BULL, "bearish": BEAR, "neutral": FLAT, "sideways": FLAT, "volatile": FLAT}


def catalog():
    """Every forecasting bot this machine can run. The tests narrow it to a handful, so a pass stays quick. Bots
    trading on an opportunistic policy are left out: their stance is where they buy, not what they forecast, and
    the zoo's pooled pictures would count their members twice."""
    from .. import bots as B
    return [b for b in B.discover() if b.policy is None]


def make_feed(asset: str):
    """The hourly candles every model reads. Replaced in the tests, which never reach a network."""
    from ..zoo.data import Feed
    return Feed(asset)


def quantile(probs: list[float] | tuple[float, ...], bins, q: float) -> float:
    """The price with `q` of the probability below it, interpolated inside the bin it lands in."""
    cum = 0.0
    for (lo, hi), p in zip(bins, probs, strict=False):
        if cum + p >= q:
            return lo + (hi - lo) * ((q - cum) / p if p > 0 else 0.5)
        cum += p
    return bins[-1][1] if bins else 0.0


def distance(a, b) -> float:
    """Total variation distance between two pictures of the same close: 0 the same, 1 no overlap at all."""
    return 0.5 * sum(abs(x - y) for x, y in zip(a, b, strict=False))


@dataclass
class Picture:
    """Every bot's read of one close, and the one picture they make together."""
    label: str                                  # "in 24 hours"
    end: float                                  # the close's settlement time, UTC seconds
    topic: int
    bins: tuple[tuple[float, float], ...] = ()
    scans: dict[str, Scan] = field(default_factory=dict)        # bot id -> its picture, errors included
    probs: tuple[float, ...] = ()               # the consensus: the mean of every picture that computed
    market: tuple[float, ...] = ()              # what the market charges for the same ranges
    median: float = 0.0
    band: tuple[float, float] = (0.0, 0.0)      # 80% of the consensus
    market_median: float = 0.0
    market_band: tuple[float, float] = (0.0, 0.0)
    counts: dict[str, int] = field(default_factory=dict)        # bullish / neutral / bearish
    shades: dict[str, int] = field(default_factory=dict)        # the zoo's own five: volatile and sideways inside neutral
    agreement: float = 0.0                      # 1 − the mean distance from a bot to the consensus
    gap: float = 0.0                            # the consensus against the market's prices

    @property
    def n(self) -> int:
        return sum(self.counts.values())

    def lean(self, spot: float) -> float | None:
        return (self.median / spot - 1) if spot and self.median else None


class Swarm:
    """One pass of the zoo over one series, kept until a new hourly bar or a new set of closes arrives."""

    def __init__(self, hub) -> None:
        self.hub = hub
        self.bots: list[Any] = []
        self.error = ""
        self.busy = False
        self.pictures: list[Picture] = []
        self.at = 0.0                           # when the last complete pass finished
        self._key: tuple = ()
        self._feeds: dict[str, Any] = {}
        self._loading = asyncio.Lock()

    # data ───────────────────────────────────────────────────

    def feed(self, asset: str):
        if asset not in self._feeds:
            self._feeds[asset] = make_feed(asset)
        return self._feeds[asset]

    async def discover(self) -> None:
        """The zoo, loaded once however many pages ask for it. Imports numpy, pandas and scipy: never on the paint path."""
        async with self._loading:
            if self.bots:
                return
            try:
                self.bots = await asyncio.to_thread(catalog)
            except Exception as e:              # a zoo that will not import must not take the page down
                self.error = f"the model zoo would not load: {type(e).__name__}: {e}"

    def horizons(self, cadence: int):
        return HOURLY if cadence < 86400 else DAILY

    def choose(self, views: list, cadence: int) -> list[tuple[str, Any]]:
        """The closes the front page asks about: the live close nearest each horizon, each one taken once.

        A close far outside the horizon it would stand for is left out rather than mislabelled — and a model that
        simulates paths costs time in proportion to how far ahead it looks, so this is also what keeps a series
        whose next close is years away from taking a pass with it."""
        now = time.time()
        live = [v for v in views if v.row.end_time_utc - now > 60]
        out: list[tuple[str, Any]] = []
        for ahead, label in self.horizons(cadence):
            if not live:
                break
            v = min(live, key=lambda v: abs(v.row.end_time_utc - (now + ahead)))
            if any(v is chosen for _, chosen in out) or abs(v.row.end_time_utc - (now + ahead)) > 2 * ahead:
                continue
            out.append((label, v))
        return out

    async def refresh(self, asset: str, views: list, cadence: int, changed=lambda: None) -> None:
        """One pass: every bot against each chosen close, nearest first, painting as each close fills."""
        if not views:
            return
        await self.discover()                   # both windows wait for the zoo, but only one of them walks it
        if not self.bots or self.busy:
            return
        chosen = self.choose(views, cadence)
        if not chosen:
            return
        feed = self.feed(asset)
        first = not self.pictures               # a later pass keeps the last one on screen until it has them all
        self.busy = True

        def notify() -> None:
            changed()
            self.hub.changed()                  # the other window reads the same pass: repaint it too
        try:
            await feed.refresh(max_age=60)
            if not getattr(feed, "ready", False):
                self.error = f"no hourly candles for {asset} yet"
                return
            key = (asset, feed.bars.index[-1], tuple(v.row.topic_id for _, v in chosen), len(self.bots))
            if key == self._key:
                return
            self.error = ""
            done: list[Picture] = []
            for label, view in chosen:
                pic = await self._one(label, view, feed)
                if pic is None:
                    return                      # the pass was abandoned: keep what the page already has
                done.append(pic)
                if first:
                    self.pictures = [*done]     # the first pass fills the page a horizon at a time
                    notify()
            self.pictures, self._key, self.at = done, key, time.time()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.busy = False
            notify()

    async def _one(self, label: str, view, feed) -> Picture | None:
        from .. import pricing as P
        from ..app import book_of, scan_one

        book = book_of(view)
        market = P.signal(book.probs)
        ctx = feed.ctx(book.bins, book.end_time_utc)
        pic = Picture(label, float(view.row.end_time_utc), int(view.row.topic_id), tuple(book.bins), market=tuple(market))
        scans: dict[str, Scan] = {}
        for bot in self.bots:
            if self.hub.stopping:
                return None
            scans[bot.id] = await asyncio.to_thread(scan_one, bot, book, ctx, market)
        pic.scans = scans
        summarise(pic)
        return pic


def summarise(pic: Picture) -> None:
    """The counts, the consensus picture, and how far it sits from the market's prices."""
    good = [s for s in pic.scans.values() if not s.error and s.probs]
    pic.counts = {BULL: 0, FLAT: 0, BEAR: 0}
    pic.shades = {}
    for s in good:
        pic.counts[STANCE.get(s.view, FLAT)] += 1               # bullish, bearish, or neither
        pic.shades[s.view] = pic.shades.get(s.view, 0) + 1      # …and the zoo's own five, for the aside
    if not good:
        return
    n = len(good)
    probs = [sum(col) / n for col in zip(*(s.probs for s in good), strict=False)]
    total = sum(probs) or 1.0
    pic.probs = tuple(p / total for p in probs)
    pic.median = quantile(pic.probs, pic.bins, 0.5)
    pic.band = (quantile(pic.probs, pic.bins, 0.1), quantile(pic.probs, pic.bins, 0.9))
    if pic.market:
        m = [p / (sum(pic.market) or 1.0) for p in pic.market]
        pic.market_median = quantile(m, pic.bins, 0.5)
        pic.market_band = (quantile(m, pic.bins, 0.1), quantile(m, pic.bins, 0.9))
        pic.gap = distance(pic.probs, m)
    pic.agreement = 1.0 - sum(distance(s.probs, pic.probs) for s in good) / n
