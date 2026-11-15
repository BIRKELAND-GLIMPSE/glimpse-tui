"""Thin async data layer over the glimpse-markets SDK.

The SDK has no retries and its list endpoints paginate on limit/offset (its own
page/page_size arguments are ignored by the server), so this module owns paging,
429 backoff and the conversion of raw responses into the small records the UI needs.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field

import httpx
from glimpse_markets import AsyncClient, EnterMultiTopicLegGroup, TradeLeg
from glimpse_markets._base import DEFAULT_BASE_URL
from glimpse_markets.exceptions import (
    GlimpseAmbiguousTradeStateError,
    GlimpseAPIError,
    GlimpseAuthenticationError,
    GlimpseError,
    GlimpseRateLimitError,
    GlimpseTradingNotEligibleError,
)
from glimpse_markets.models import ExitLegReq, ExitMultiTopicLegGroup
from glimpse_markets.ratelimit import AsyncRateLimiter

from . import pricing

NMARKET = "/api/v1/nmarket"
PAGE = 48
WAVE = 4                        # pages in flight at once: a week of hourly closes (168) is 4 pages
COINBASE = "https://api.exchange.coinbase.com/products/{pair}"
PAIRS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XAU": "PAXG-USD"}
# Priced by Glimpse's own oracle rather than an exchange: WTI crude in satoshis a barrel (WTI in dollars over Bitcoin in
# dollars) and Tether Gold in satoshis a troy ounce. No exchange quotes them, so their candles and spot come from the market's
# own chart endpoint, the price it settles on.
ORACLE = ("WTIBTC", "XAUTBTC", "HASHPRICE", "DIFFICULTY", "ADJUSTMENT")
ASSETS = tuple(PAIRS) + ORACLE  # every asset with a price feed: what the bots and the forecast windows can run on
HOURLY = ("BTC", "XAU", "WTIBTC", "XAUTBTC")   # assets Glimpse runs an hourly series on: theirs is the plain ticker, the daily one is `1D`
CHART = "{base}/api/v1/nmarket/markets/{topic}/chart"
# Hashprice runs on a second service, one API shape under another host and prefix. Its markets settle on Bitview's `hash_price`
# on the hourly slot ending at the close, so that is its price feed. Its ladder is named in whole cents; this module turns the
# names into dollars on the way in, so every screen, bot and test reads dollars.
HASHPRICE = "HASHPRICE"
# Two more on that service settle on block height, not the clock: the difficulty a retarget sets, in terahash, and the percent
# change it makes. A step function has no candles worth a walk, so only the bots built for them draw these (see `Bot.draws`).
DIFFICULTY, ADJUSTMENT = "DIFFICULTY", "ADJUSTMENT"
RETARGET_SERIES = (DIFFICULTY, ADJUSTMENT)
TOPIC_TYPES = {"hashprice_daily": HASHPRICE, "btc_difficulty": DIFFICULTY, "adjustment_epoch": ADJUSTMENT}
DMARKET = "/api/v1/dmarket"
DMARKET_URL = "https://difficulty.bpmapi.io"
DMARKET_TOKEN_ENV = "GLIMPSE_DMARKET_TOKEN"     # a session token for that service, which does not read API keys (see NoServiceAuth)
DMARKET_LIST_S = 5.0            # how long the series list waits for that service
BITVIEW_SERIES = "https://bitview.space/api/series"
BITVIEW_INDEX = {60: "minute10", 300: "minute10", 900: "minute30", 1800: "minute30", 3600: "hour1", 14400: "hour4", 86400: "day1"}
BITVIEW_SLOT = {"minute10": 600, "minute30": 1800, "hour1": 3600, "hour4": 14400, "day1": 86400}
PERIODS = {60: "1min", 300: "5min", 900: "15min", 1800: "30min", 3600: "1h", 14400: "4h", 86400: "1day"}
WEEKEND_REACH = 3 * 86400       # how far back a weekend-only oracle window is widened to reach a weekday
TOPIC_TTL = 600.0               # how long an oracle series' topic id is kept before it is looked up again


class ApiError(Exception):
    """A message that is safe and useful to show in the status line."""


class KeyRejected(ApiError):
    """Glimpse refused the API key itself, as opposed to being slow or unreachable."""


class NoServiceAuth(ApiError):
    """The second service (hashprice, difficulty) refused to act on this account. It says nothing about the key: the main
    service may take it."""


class Transient(ApiError):
    """Glimpse did not answer, or answered that it cannot right now: a timeout, no connection, a 5xx, a 429. Nothing was
    wrong with the request, so it is worth asking again. `status` is the HTTP status when there was one."""

    def __init__(self, msg: str, status: int = 0) -> None:
        super().__init__(msg)
        self.status = status


class Ambiguous(ApiError):
    """An order was sent and its answer never came: it may or may not have filled. Only the account can say which."""


def asset_of(title: str) -> str:
    """Ticker named by a series or market title, or '' if it names none we know."""
    t = title.lower()
    if "hashprice" in t or "hash price" in t:
        return HASHPRICE            # "Bitcoin Hashprice · Daily": before the "bitcoin" below
    if "difficulty" in t:           # a market's own title; a series' title is ambiguous, so a series says by its topic type (Batch.kind)
        return ADJUSTMENT if "adjustment" in t else DIFFICULTY
    if re.search(r"\b(wti|oil|brent)\b", t) and re.search(r"\bbtc\b", t):
        return "WTIBTC"             # "Hourly WTI/BTC Prediction Markets": oil priced in Bitcoin, the only oil series there is
    if re.search(r"\bxaut\b|tether gold", t) or ("gold" in t and re.search(r"\b(sats?|satoshis?|btc)\b", t)):
        return "XAUTBTC"            # "Hourly Tether Gold in sats per troy ounce": gold priced in Bitcoin
    return next((sym for needle, sym in (("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"), ("gold", "XAU")) if needle in t), "")


def series_name(asset: str, daily: bool = False) -> str:
    """`XAU` with `daily` is `XAU 1D`, because gold has an hourly series too; `ETH` with `daily` stays `ETH`."""
    return f"{asset} 1D" if daily and asset in HOURLY else asset


@dataclass(frozen=True)
class Batch:
    batch_id: str
    title: str
    topic_count: int
    interval: float
    kind: str = ""                  # the asset, where the service says by topic type rather than by title

    @property
    def asset(self) -> str:
        return self.kind or asset_of(self.title)

    @property
    def hourly(self) -> bool:
        return "hour" in self.title.lower()

    @property
    def short(self) -> str:
        """The ticker alone. Bitcoin and gold have two series each, so the daily one is marked; the hourly one is plain."""
        if not self.asset:
            return self.title
        return series_name(self.asset, daily=not self.hourly)


@dataclass(frozen=True)
class MarketRow:
    topic_id: int
    title: str
    end_time_utc: int
    quote_mode: str
    volume_msat: int
    volume_24h_msat: int
    shares: tuple[float, ...]
    names: tuple[str, ...]
    option_ids: tuple[int, ...] = ()
    close_block: int = 0            # a retarget market: the height whose difficulty settles it
    trading_stops_block: int = 0    # a retarget market: trading is open while the tip is below this


@dataclass
class Book:
    """One market's full state, priced locally from shares."""
    topic_id: int
    title: str
    end_time_utc: int
    quote_mode: str
    volume_msat: int
    option_ids: list[int]
    bins: list[tuple[float, float]]
    shares: list[float]
    alpha: float
    prices: list[float] = field(default_factory=list)
    probs: list[float] = field(default_factory=list)
    fetched_at: float = 0.0
    close_block: int = 0            # a retarget market: the height whose difficulty settles it
    trading_stops_block: int = 0

    @classmethod
    def from_row(cls, row: MarketRow) -> Book:
        """A list row priced as a book: the v2 and batch listings carry every range's shares, so no quote call is needed."""
        b = cls(row.topic_id, row.title, row.end_time_utc, row.quote_mode, row.volume_msat,
                list(row.option_ids) or list(range(1, len(row.shares) + 1)),
                [parse_bin(n) for n in row.names], list(row.shares), pricing.alpha_for(max(len(row.shares), 2), row.topic_id),
                fetched_at=time.time(), close_block=row.close_block, trading_stops_block=row.trading_stops_block)
        b.reprice()
        return b

    def reprice(self) -> None:
        self.prices = pricing.prices(self.shares, self.alpha)
        self.probs = pricing.implied_probs(self.prices)

    @property
    def is_live(self) -> bool:
        return self.quote_mode == "live" and self.end_time_utc > time.time()


@dataclass(frozen=True)
class Position:
    topic_id: int
    option_id: int
    topic_title: str
    option_name: str
    shares: float
    cost_sats: float
    value_sats: float
    end_time: int
    status: str

    @property
    def pnl_sats(self) -> float:
        return self.value_sats - self.cost_sats


@dataclass(frozen=True)
class Summary:
    cost_sats: float
    value_sats: float
    pnl_sats: float
    active: int
    pending: int
    resolved: int


@dataclass(frozen=True)
class Wallet:
    balance_sats: float
    exposure_sats: float
    max_exposure_sats: float


@dataclass(frozen=True)
class Fill:
    topic_id: int
    trade_id: str
    cost_sats: float
    fee_sats: float
    error: str = ""


@dataclass(frozen=True)
class Candle:
    t: int          # bucket start, unix seconds UTC
    o: float
    h: float
    l: float  # noqa: E741
    c: float


@dataclass(frozen=True)
class Order:
    """`contracts` on each listed option of one market."""
    topic_id: int
    option_ids: tuple[int, ...]
    contracts: float

    def legs(self) -> list[TradeLeg]:
        return [TradeLeg(option_id=o, contracts=self.contracts) for o in self.option_ids]


async def candles(asset: str, granularity: int, topic_id: int = 0) -> list[Candle]:
    """Public Coinbase candles, oldest first, up to 350 buckets of `granularity` seconds; or, for an asset Glimpse prices
    itself, the oracle's own candles through `topic_id`, any open market of the series."""
    if asset in ORACLE:
        return await oracle_candles(topic_id, granularity) if topic_id else []
    pair = PAIRS.get(asset)
    if not pair:
        return []
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "glimpse-tui"}) as c:
        r = await c.get(COINBASE.format(pair=pair) + "/candles", params={"granularity": granularity})
        r.raise_for_status()
    return [Candle(int(x[0]), float(x[3]), float(x[2]), float(x[1]), float(x[4])) for x in sorted(r.json(), key=lambda x: x[0])]


def base_url() -> str:
    return (os.environ.get("GLIMPSE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


async def oracle_candles(topic_id: int, granularity: int, start: int | None = None, end: int | None = None) -> list[Candle]:
    """Glimpse's own price history behind a market it prices itself (WTI/BTC), oldest first: the last week of
    `granularity`-second candles (a minute to a day, see PERIODS), or `start`..`end` of them in unix seconds (2,000 hours
    arrive in one answer). Any market of the series names the series: the server reads the ticker, not the close, so a
    settled one serves. The last candle is the one in progress, so its close is the price now."""
    from .data import http

    period = PERIODS.get(granularity)
    if period is None:
        raise ValueError(f"the oracle has no {granularity}s candle; one of {sorted(PERIODS)}")
    if topic_id >= pricing.DMARKET_TOPIC:
        asset = _DM_ASSET.get(topic_id, HASHPRICE)
        return await (retarget_candles(asset, granularity, start, end) if asset in RETARGET_SERIES
                      else hashprice_candles(granularity, start, end))
    params: dict[str, str | int] = {"period": period}
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)
    url = CHART.format(base=base_url(), topic=topic_id)
    r = await http.client(url).get(url, params=params)
    since = None
    if r.status_code >= 500 and start is not None:
        # A window that falls wholly inside the oil weekend 502s (twelvedata has no WTI/USD candle in it), though the
        # server fills those hours itself once the window reaches back to Friday's close. Ask again from three days
        # earlier, which always does, and keep the hours asked for.
        since, params["start"] = int(start), int(start) - WEEKEND_REACH
        r = await http.client(url).get(url, params=params)
    r.raise_for_status()
    out = [Candle(int(c["time"]), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]))
           for c in r.json().get("candles") or [] if float(c.get("close") or 0) > 0]
    if since is not None:
        out = [c for c in out if c.t + granularity > since]
    return sorted(out, key=lambda c: c.t)


async def hashprice_candles(granularity: int, start: int | None = None, end: int | None = None) -> list[Candle]:
    """Bitview's hashprice as candles, oldest first: one value a slot, the value at the slot's end, opening where the slot
    before it closed, as the site draws them. The last slot is the one in progress, so its close is the reading now. A slot
    finer than Bitview's ten minutes is served ten-minute slots."""
    from .data import http

    index = BITVIEW_INDEX.get(granularity, "hour1")
    slot = BITVIEW_SLOT[index]
    now = int(time.time())
    first = now - 7 * 86400 if start is None else int(start)
    r = await http.client(BITVIEW_SERIES).get(f"{BITVIEW_SERIES}/bulk", params={
        "series": "hash_price,timestamp", "index": index, "start": -min((now - first) // slot + 3, 20_000)})
    r.raise_for_status()
    values, stamps = (x["data"] for x in r.json())
    out: list[Candle] = []
    last = None
    for t, v in zip(stamps, values, strict=True):
        if t is None or v is None or v <= 0:
            continue
        o = v if last is None else last
        out.append(Candle(int(t), o, max(o, v), min(o, v), v))
        last = v
    return [c for c in out if c.t >= first - slot and (end is None or c.t <= int(end))]


async def retarget_candles(asset: str, granularity: int, start: int | None = None, end: int | None = None) -> list[Candle]:
    """Bitview's difficulty as candles, oldest first, one a slot: terahash for DIFFICULTY, and for ADJUSTMENT the percent change
    the last retarget made. Both are steps: flat for two weeks, then a jump at a retarget, which is the candle it lands in."""
    from .data import http

    index = BITVIEW_INDEX.get(granularity, "hour1")
    slot = BITVIEW_SLOT[index]
    now = int(time.time())
    first = now - 7 * 86400 if start is None else int(start)
    lead = 3 * 14 * 86400                       # far enough back to have seen a retarget before the first candle
    r = await http.client(BITVIEW_SERIES).get(f"{BITVIEW_SERIES}/bulk", params={
        "series": "difficulty,timestamp", "index": index, "start": -min((now - first + lead) // slot + 3, 16_000)})
    r.raise_for_status()
    values, stamps = (x["data"] for x in r.json())
    out: list[Candle] = []
    last, before = None, None
    for t, v in zip(stamps, values, strict=True):
        if t is None or not v:
            continue
        if last is not None and v != last:
            before = last
        if asset == DIFFICULTY:
            shown = v / 1e12
        elif before is None:
            last = v
            continue
        else:
            shown = (v / before - 1) * 100
        o = out[-1].c if out else shown
        out.append(Candle(int(t), o, max(o, shown), min(o, shown), shown))
        last = v
    return [c for c in out if c.t >= first - slot and (end is None or c.t <= int(end))]


async def oracle_spot(topic_id: int) -> float:
    """The price now on an oracle-priced series: the close of the minute in progress, a few hundred bytes. 0 if none."""
    now = int(time.time())
    got = await oracle_candles(topic_id, 60, start=now - 900, end=now)
    if not got:
        got = await oracle_candles(topic_id, 3600, start=now - 3 * 3600, end=now)
    return got[-1].c if got else 0.0


_TOPICS: dict[str, tuple[float, int]] = {}


async def oracle_topic(asset: str) -> int:
    """`Glimpse.oracle_topic` for code with no client of its own (the bots' price feed): a public client for the call."""
    at, topic = _TOPICS.get(asset, (0.0, 0))
    if topic and time.time() - at < TOPIC_TTL:
        return topic
    g = Glimpse()
    try:
        topic = await g.oracle_topic(asset)
    finally:
        await g.close()
    _TOPICS[asset] = (time.time(), topic)
    return topic


def resample(candles: list[Candle], seconds: int, origin: int = 0) -> list[Candle]:
    """Merge candles into `seconds` buckets aligned to the epoch (so :00 and :30 for half hours), or to `origin`
    past it (weeks that start on Monday)."""
    out: list[Candle] = []
    for k in candles:
        t0 = k.t - (k.t - origin) % seconds
        if out and out[-1].t == t0:
            p = out[-1]
            out[-1] = Candle(t0, p.o, max(p.h, k.h), min(p.l, k.l), k.c)
        else:
            out.append(Candle(t0, k.o, k.h, k.l, k.c))
    return out


_SHARED: dict[tuple, tuple] = {}


def _shared(t: tuple) -> tuple:
    """Every close in a series carries the same 500 names and ids. Keep one copy, not one per close per refresh."""
    if len(_SHARED) > 64:
        _SHARED.clear()
    return _SHARED.setdefault(t, t)


def contiguous(rows: list[MarketRow], factor: float = 10.0) -> list[MarketRow]:
    """The closes up to the first gap more than `factor` times the series' usual spacing. The site lists a series as
    one unbroken run (hourly for a week, daily for months); the batch endpoint also returns a block of hourly closes
    some fifty days past the rest, which the site does not list and the bots should not trade."""
    if len(rows) < 3:
        return rows
    gaps = sorted(b.end_time_utc - a.end_time_utc for a, b in zip(rows, rows[1:]))
    usual = max(gaps[len(gaps) // 2], 1)
    for i, (a, b) in enumerate(zip(rows, rows[1:])):
        if b.end_time_utc - a.end_time_utc > factor * usual:
            return rows[: i + 1]
    return rows


def parse_bin(name: str) -> tuple[float, float]:
    """`3570-3580`, or `-30 to -29` where a range can run below zero."""
    name = name.replace(",", "")
    lo, _, hi = name.partition(" to ") if " to " in name else name.partition("-")
    return float(lo), float(hi)


def dollars(name: str) -> str:
    """A hashprice range's name, in the whole cents the service uses, in dollars: `3570-3580` is `35.70-35.80`."""
    lo, hi = parse_bin(name)
    return f"{lo / 100:.2f}-{hi / 100:.2f}"


_DM_ASSET: dict[int, str] = {}  # the second service's topics seen so far -> their series: only hashprice names ranges in cents


def range_name(name: str, topic_id: int, title: str = "") -> str:
    """A range's name as this terminal reads it, from the name its service gave it. `title` says which series a topic not yet
    listed belongs to."""
    if topic_id < pricing.DMARKET_TOPIC or not name:
        return name
    known = _DM_ASSET.get(topic_id) or asset_of(title)
    return dollars(name) if known in (HASHPRICE, "") else name


def _learn(m: dict) -> None:
    """What a second-service market says about itself that the other screens need: its series and its alpha."""
    topic = m.get("topic_id") or 0
    if topic >= pricing.DMARKET_TOPIC:
        _DM_ASSET[topic] = TOPIC_TYPES.get(m.get("topic_type") or "") or asset_of(m.get("title") or "")
        if m.get("alpha"):
            pricing.STATED_ALPHA[topic] = float(m["alpha"])


def _row(m: dict) -> MarketRow:
    outs = m.get("outcomes") or []
    topic = m["topic_id"]
    _learn(m)
    return MarketRow(
        topic_id=topic, title=m.get("title") or "", end_time_utc=int(m.get("end_time_utc") or 0),
        quote_mode=m.get("quote_mode") or "live",
        volume_msat=int(m.get("total_volume_millisats") or 0),
        volume_24h_msat=int(m.get("volume_24h_millisats") or 0),
        shares=tuple(float(o.get("shares") or 0) for o in outs),
        names=_shared(tuple(range_name(o.get("name") or "", topic) for o in outs)),
        option_ids=_shared(tuple(int(o.get("option_id") or i + 1) for i, o in enumerate(outs))),
        close_block=int(m.get("close_block_height") or 0), trading_stops_block=int(m.get("trading_closes_at_block") or 0),
    )


def _live(rows) -> list[MarketRow]:
    now = time.time()
    return sorted((r for r in rows if r.end_time_utc > now), key=lambda r: r.end_time_utc)


def _rows_file(batch_id: str):
    from .term.config import cache_dir
    return cache_dir() / f"markets-{batch_id}.json"


def save_rows(batch_id: str, rows: list[MarketRow]) -> None:
    """Keep the last market list of a series on disk, so the next start can draw it before the network answers.
    Every close in a series shares its 500 names and ids, so each distinct set is written once."""
    ladders: list[tuple[tuple, tuple]] = []
    out = []
    for r in rows:
        key = (r.names, r.option_ids)
        if key not in ladders:
            ladders.append(key)
        out.append([r.topic_id, r.title, r.end_time_utc, r.quote_mode, r.volume_msat, r.volume_24h_msat, r.shares,
                    ladders.index(key), r.close_block, r.trading_stops_block])
    try:
        f = _rows_file(batch_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": time.time(), "ladders": ladders, "rows": out}))
        tmp.replace(f)
    except (OSError, TypeError, ValueError):
        pass


def load_rows(batch_id: str, max_age: float) -> tuple[float, list[MarketRow]]:
    """(when it was saved, the closes still open) from the last saved list, or (0, []) if none is younger than max_age."""
    try:
        d = json.loads(_rows_file(batch_id).read_text())
        if time.time() - d["at"] > max_age:
            return 0.0, []
        ladders = [(_shared(tuple(n)), _shared(tuple(i))) for n, i in d["ladders"]]
        rows = [MarketRow(t, title, end, qm, vol, vol24, tuple(sh), *ladders[k], *more)
                for t, title, end, qm, vol, vol24, sh, k, *more in d["rows"]]
        return float(d["at"]), _live(rows)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return 0.0, []


def _friendly(e: Exception) -> ApiError:
    if isinstance(e, GlimpseAuthenticationError):
        return KeyRejected("API key rejected. Press L to enter a new one.")
    if isinstance(e, GlimpseTradingNotEligibleError):
        return ApiError(f"Trading not enabled on this account: {e.reason or e.message}. Finish onboarding on the website.")
    if isinstance(e, GlimpseAmbiguousTradeStateError):
        return Ambiguous("Connection dropped mid-trade. The order may or may not have filled: check the portfolio before retrying.")
    if isinstance(e, GlimpseRateLimitError):
        return Transient("Rate limited by Glimpse. Slowing down.", 429)
    if isinstance(e, GlimpseAPIError):
        status = e.status_code or 0
        msg = f"Glimpse {status}: {e.error or e.message or 'request failed'}"
        return Transient(msg, status) if status >= 500 else ApiError(msg)
    if isinstance(e, httpx.TimeoutException):
        return Transient("Glimpse timed out.")
    if isinstance(e, httpx.HTTPError):
        return Transient("Cannot reach Glimpse. Check the connection.")
    return ApiError(str(e) or e.__class__.__name__)


class _Dmarket(AsyncClient):
    """The SDK on the hashprice and difficulty service: the same API, every path under its own prefix. A trade write carries an idempotency
    key as the site's do, and this service reads a session token (DMARKET_TOKEN_ENV) where the main one reads an API key."""

    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        token = os.environ.get(DMARKET_TOKEN_ENV)
        if token:
            h["Authorization"] = token
        h["Idempotency-Key"] = str(uuid.uuid4())    # nothing here is retried: every request is its own attempt
        return h

    async def _get(self, path, params=None):
        return await super()._get(path.replace(NMARKET, DMARKET), params)

    async def _post(self, path, json_body):
        return await super()._post(path.replace(NMARKET, DMARKET), json_body)

    async def _post_mutating(self, path, json_body):
        return await super()._post_mutating(path.replace(NMARKET, DMARKET), json_body)


_DM_BATCHES: set[str] = set()   # the batches the second service listed: batch ids are uuids, so nothing else tells them apart


class Glimpse:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        kw = {"base_url": base_url} if base_url else {}
        self._c = AsyncClient(api_key=api_key, **kw)
        dm = {"base_url": os.environ.get("GLIMPSE_DMARKET_URL") or DMARKET_URL}
        self._dc = _Dmarket(api_key=api_key, **dm)
        self._dpub = _Dmarket(rate_limiter=AsyncRateLimiter(240, 60), **dm)
        # Market data is public and not limited per key, so it gets its own client and budget:
        # polling and pre-trade estimates never queue behind, or eat into, the 60/min keyed allowance.
        self._pub = AsyncClient(rate_limiter=AsyncRateLimiter(240, 60), **kw)
        self.authenticated = bool(api_key)
        self.closed = False
        self._topics: dict[str, tuple[float, int]] = {}     # oracle series -> (when looked up, an open market's topic id)

    async def close(self) -> None:
        self.closed = True
        for c in (self._c, self._pub, self._dc, self._dpub):
            await c.aclose() if hasattr(c, "aclose") else await c.close()

    def _reader(self, topic_id: int = 0, batch_id: str = "") -> AsyncClient:
        """The public client of the service a market or series lives on."""
        return self._dpub if topic_id >= pricing.DMARKET_TOPIC or batch_id in _DM_BATCHES else self._pub

    def _writer(self, topic_id: int) -> AsyncClient:
        return self._dc if topic_id >= pricing.DMARKET_TOPIC else self._c

    async def _trade(self, topic_id: int, fn, *a, **kw):
        """A trade on the service that owns `topic_id`. The main service's refusal means the key is bad. The second service's
        means only that it did not take these credentials, and must not read as a bad key, which would log the terminal out."""
        try:
            return await self._call(fn, *a, retry=False, **kw)
        except KeyRejected as e:
            if topic_id < pricing.DMARKET_TOPIC:
                raise
            raise NoServiceAuth(f"The hashprice and difficulty service does not accept this API key, so it cannot trade live here yet. "
                                f"Paper trading works. ({DMARKET_TOKEN_ENV} can hold a session token for it.)") from e

    async def _call(self, fn, *a, retry: bool = True, **kw):
        """Reads retry on 429/5xx/transport errors. Trades never retry: there is no idempotency key. A call made after
        `close` is cancelled quietly rather than crashing the app: a loader a timer started as the app shut down, or one
        paging through the market list when logging in or out swapped this client for another. Checked before every
        attempt, and httpx's own "client has been closed" is treated the same way."""
        for attempt in range(3 if retry else 1):
            if self.closed:
                raise asyncio.CancelledError("client closed")
            try:
                return await fn(*a, **kw)
            except RuntimeError as e:
                if self.closed or "client has been closed" in str(e):
                    raise asyncio.CancelledError("client closed") from None
                raise
            except GlimpseRateLimitError as e:
                if attempt == 2 or not retry:
                    raise _friendly(e) from None
                await asyncio.sleep(min(float(e.retry_after or 2), 10))
            except (GlimpseAuthenticationError, GlimpseTradingNotEligibleError, GlimpseAmbiguousTradeStateError) as e:
                raise _friendly(e) from None
            except GlimpseAPIError as e:
                if attempt == 2 or not retry or (e.status_code or 0) < 500:
                    raise _friendly(e) from None
                await asyncio.sleep(0.5 * (attempt + 1))
            except (httpx.HTTPError, GlimpseError) as e:
                if attempt == 2 or not retry or isinstance(e, GlimpseError):
                    raise _friendly(e) from None
                await asyncio.sleep(0.5 * (attempt + 1))

    # ── public ──────────────────────────────────────────────

    async def batches(self) -> list[Batch]:
        r, more = await asyncio.gather(self._call(self._pub.batches), self._hashprice_batches())
        out = [
            Batch(b.batch_id, b.main_topic_title or "?", b.topic_count or 0, float(b.outcome_interval or 0))
            for b in r.batches or []
        ] + more
        order = ["BTC", "BTC 1D", "ETH", "SOL", "XAU", "XAU 1D", "XAUTBTC", "WTIBTC", HASHPRICE, DIFFICULTY, ADJUSTMENT]
        return sorted(out, key=lambda b: order.index(b.short) if b.short in order else len(order))

    async def _hashprice_batches(self) -> list[Batch]:
        """The second service's series, hashprice with its interval in dollars. [] if it does not answer within a few seconds:
        an outage there must not cost, or hold up, the main service's series. A series says what it is by its topic type:
        "Bitcoin Difficulty Adjustment" is the difficulty in terahash, and "... Adjustment %" the percent change."""
        try:
            d = await asyncio.wait_for(self._call(self._dpub._get, f"{NMARKET}/batches", retry=False), DMARKET_LIST_S)
        except (ApiError, GlimpseError, httpx.HTTPError, TimeoutError):
            return []
        out = [Batch(b["batch_id"], b.get("main_topic_title") or "?", int(b.get("topic_count") or 0),
                     float(b.get("outcome_interval") or 0) / (100 if TOPIC_TYPES[b["topic_type"]] == HASHPRICE else 1),
                     TOPIC_TYPES[b["topic_type"]])
               for b in d.get("batches") or [] if b.get("topic_type") in TOPIC_TYPES]
        _DM_BATCHES.update(b.batch_id for b in out)
        return out

    async def oracle_topic(self, asset: str) -> int:
        """Any open market of an oracle-priced series (`WTIBTC`, `XAUTBTC`): what its chart endpoint wants. Kept ten minutes."""
        at, topic = self._topics.get(asset, (0.0, 0))
        if topic and time.time() - at < TOPIC_TTL:
            return topic
        batch = next((b for b in await self.batches() if b.asset == asset), None)
        if batch is None:
            raise ApiError(f"Glimpse has no {asset} series open right now")
        d = await self._call(self._reader(batch_id=batch.batch_id)._get, f"{NMARKET}/v2/batches/{batch.batch_id}/active-markets",
                             {"limit": 1, "offset": 0, "include_quotes": "false"})
        ms = d.get("markets") or []
        if not ms:
            raise ApiError(f"Glimpse has no {asset} close open right now")
        topic = int(ms[0]["topic_id"])
        _learn(ms[0])
        self._topics[asset] = (time.time(), topic)
        return topic

    async def markets(self, batch_id: str, limit: int = 96, on_rows=None) -> list[MarketRow]:
        """Nearest-expiry active markets. v2 rows carry shares, so the list is priced without per-market calls.

        A page of 48 closes is about 1.3 MB (500 ranges each, sent uncompressed) and takes seconds, so pages are asked
        for WAVE at a time instead of one after another: a week of hourly closes arrives in the time of one page, not
        four. `on_rows`, if given, is awaited with the nearest closes as soon as the first page lands, then with each
        longer run, so the screen fills while the far closes are still on the wire."""
        pages: list[list[MarketRow]] = []
        offset, total = 0, None
        while offset < min(limit, total if total is not None else limit):
            offsets = list(range(offset, min(limit, total if total is not None else limit), PAGE))[:WAVE]
            tasks = [asyncio.ensure_future(self._call(
                self._reader(batch_id=batch_id)._get, f"{NMARKET}/v2/batches/{batch_id}/active-markets",
                {"limit": min(PAGE, limit - o), "offset": o, **({"include_quotes": "true"} if batch_id in _DM_BATCHES else {})}))
                for o in offsets]
            try:
                more = True
                for task in tasks:                  # in order: the nearest closes are shown first
                    d = await task
                    pages.append([_row(m) for m in d.get("markets") or []])
                    total = int(d.get("total") or 0) or total
                    if on_rows is not None:
                        await on_rows(_live(r for page in pages for r in page))
                    if not d.get("has_more"):
                        more = False
                        break
            finally:
                for task in tasks:              # a page past a failure or the end is not wanted, or is already done
                    task.cancel() if not task.done() else task.cancelled() or task.exception()
            if not more:
                break
            offset = offsets[-1] + PAGE
        return _live(r for page in pages for r in page)

    async def markets_from(self, batch_id: str, offset: int, limit: int = 3) -> list[MarketRow]:
        """The `limit` closes from `offset` in the batch's active list, nearest first: with `offset` just under the number of
        closes known, the end of the list and whatever was launched since, in one small read (0.6 s, against 9 s for the
        whole list). What the watch's launch burst reads every second around the top of the hour."""
        d = await self._call(self._reader(batch_id=batch_id)._get, f"{NMARKET}/v2/batches/{batch_id}/active-markets",
                             {"limit": int(limit), "offset": int(offset), **({"include_quotes": "true"} if batch_id in _DM_BATCHES else {})})
        return [_row(m) for m in d.get("markets") or []]

    async def all_markets(self, batch_id: str) -> list[MarketRow]:
        """Every close of a series, with shares, in one request (the paged v2 list takes one per 48 closes, and on a slow
        day each page takes seconds). The one-shot endpoint also returns strays the site does not list, hourly closes
        months past the rest; `contiguous` drops them without a second request. Falls back to the paged list."""
        if batch_id in _DM_BATCHES:             # the second service's one-shot list has no outcomes; its paged list does
            return contiguous(await self.markets(batch_id, limit=1000))
        try:
            r = await self._call(self._pub.batch_active_markets, batch_id)
        except ApiError:
            return await self.markets(batch_id, limit=1000)
        now, rows = time.time(), []
        for m in r.markets or []:
            outs = m.outcomes or []
            end = int(m.end_time_utc or 0)
            if end <= now or not outs or m.is_resolved or m.is_active is False:
                continue
            rows.append(MarketRow(
                topic_id=m.topic_id, title=m.title or "", end_time_utc=end,
                quote_mode=str(getattr(m.quote_mode, "value", m.quote_mode) or "live"),
                volume_msat=int(m.total_volume_millisats or 0), volume_24h_msat=int(m.volume_24h_millisats or 0),
                shares=tuple(float(o.shares or 0) for o in outs),
                names=_shared(tuple(range_name(o.name or "", m.topic_id) for o in outs)),
                option_ids=_shared(tuple(int(o.option_id) for o in outs)),
            ))
        return contiguous(sorted(rows, key=lambda r: r.end_time_utc))

    async def book(self, topic_id: int) -> Book:
        if topic_id >= pricing.DMARKET_TOPIC:   # the SDK's quote model drops what this service adds: alpha and the heights
            d = await self._call(self._dpub._get, f"{NMARKET}/markets/{topic_id}/quotes")
            _learn(d)
            row = _row({**d, "end_time_utc": d.get("market_end_time_utc") or d.get("end_time_utc")})
            return Book.from_row(row)
        q = await self._call(self._reader(topic_id).market_quotes, topic_id)
        outs = q.outcomes or []
        b = Book(
            topic_id=topic_id, title=q.title or "", end_time_utc=int(q.market_end_time_utc or 0),
            quote_mode=str(getattr(q.quote_mode, "value", q.quote_mode) or "live"),
            volume_msat=int(q.total_volume_millisats or 0),
            option_ids=[o.option_id for o in outs],
            bins=[parse_bin(o.name) for o in outs],
            shares=[float(o.shares or 0) for o in outs],
            alpha=pricing.alpha_for(max(len(outs), 2), topic_id),
            fetched_at=time.time(),
        )
        b.reprice()
        return b

    async def estimate(self, book: Book, lo: int, hi: int, contracts: float) -> tuple[float, float]:
        """Server's (cost, fee) in sats for a range buy. Public and side-effect free."""
        return await self.estimate_order(Order(book.topic_id, tuple(book.option_ids[lo:hi + 1]), contracts))

    async def estimate_order(self, order: Order) -> tuple[float, float]:
        e = await self._call(self._reader(order.topic_id).estimate_trade, order.topic_id, "buy", order.legs())
        return (e.total_cost_millisats or 0) / 1000, (e.commission_millisats or 0) / 1000

    async def estimate_legs(self, topic_id: int, legs: list[tuple[int, float]]) -> tuple[float, float]:
        """Server's (cost, fee) in sats for (option id, contracts) legs of one market: what a bot's basket looks like."""
        e = await self._call(self._reader(topic_id).estimate_trade, topic_id, "buy", [TradeLeg(option_id=o, contracts=c) for o, c in legs])
        return (e.total_cost_millisats or 0) / 1000, (e.commission_millisats or 0) / 1000

    # ── authenticated ───────────────────────────────────────

    async def wallet(self) -> Wallet:
        """`balance` is millisats; the two exposure fields are already sats."""
        d = await self._call(self._c.wallet_balance) or {}
        return Wallet(
            balance_sats=float(d.get("balance") or 0) / 1000,
            exposure_sats=float(d.get("currentOpenExposureSats") or 0),
            max_exposure_sats=float(d.get("maxExposureSats") or 0),
        )

    async def positions(self) -> list[Position]:
        """Every open position, on both services. The second answers only if it takes these credentials; if it does not,
        its positions cannot be read and the rest still can."""
        r = await self._call(self._c.portfolio_active)
        rows = list(r.message or [])
        if self.authenticated or os.environ.get(DMARKET_TOKEN_ENV):
            try:
                rows += list((await self._call(self._dc.portfolio_active, retry=False)).message or [])
            except ApiError:
                pass
        return [
            Position(
                topic_id=p.topic_id, option_id=p.option_id, topic_title=p.topic_title or "",
                option_name=range_name(p.option_name or "", p.topic_id, p.topic_title or ""), shares=float(p.shares or 0),
                cost_sats=float(p.purchase_value or 0) / 1000, value_sats=float(p.current_value or 0) / 1000,
                end_time=int(p.market_end_time or 0), status=p.status or "",
            )
            for p in rows
        ]

    async def can_trade_second_service(self) -> bool:
        """Whether the hashprice and difficulty service takes these credentials: asked of a read that needs them, so no order is at stake."""
        try:
            await self._call(self._dc.portfolio_active, retry=False)
            return True
        except KeyRejected:
            return False

    async def summary(self) -> Summary:
        m = (await self._call(self._c.portfolio_summary)).message
        return Summary(
            cost_sats=(m.total_purchase_value or 0) / 1000, value_sats=(m.total_current_value or 0) / 1000,
            pnl_sats=(m.total_pnl or 0) / 1000, active=m.active_market_count or 0,
            pending=m.ended_unresolved_market_count or 0, resolved=m.resolved_market_count or 0,
        )

    async def buy(self, book: Book, lo: int, hi: int, contracts: float) -> Fill:
        fill = (await self.buy_orders([Order(book.topic_id, tuple(book.option_ids[lo:hi + 1]), contracts)]))[0]
        if fill.error:
            raise ApiError(f"Order rejected: {fill.error}")
        return fill

    async def buy_orders(self, orders: list[Order]) -> list[Fill]:
        """One request for every market. The server fills each market independently, so check each Fill.error."""
        t = orders[0].topic_id if orders else 0
        r = await self._trade(t, self._writer(t).enter_multi_topic_multi_leg,
                              [EnterMultiTopicLegGroup(topic_id=o.topic_id, legs=o.legs()) for o in orders])
        by_topic = {x.topic_id: x for x in r.results or []}
        fills = []
        for o in orders:
            x = by_topic.get(o.topic_id)
            if x is None or x.error:
                fills.append(Fill(o.topic_id, "", 0.0, 0.0, error=getattr(x, "error", None) or "no result returned"))
            else:
                fills.append(Fill(o.topic_id, str(x.trade_id), (x.total_cost_millisats or 0) / 1000, (x.commission_millisats or 0) / 1000))
        return fills

    async def buy_legs(self, topic_id: int, legs: list[tuple[int, float]]) -> Fill:
        """One market, a different size on every leg. Never retried. Check Fill.error."""
        r = await self._trade(topic_id, self._writer(topic_id).enter_multi_topic_multi_leg,
                              [EnterMultiTopicLegGroup(topic_id=topic_id, legs=[TradeLeg(option_id=o, contracts=c) for o, c in legs])])
        x = (r.results or [None])[0]
        if x is None or x.error:
            return Fill(topic_id, "", 0.0, 0.0, error=getattr(x, "error", None) or "no result returned")
        return Fill(topic_id, str(x.trade_id), (x.total_cost_millisats or 0) / 1000, (x.commission_millisats or 0) / 1000)

    async def sell(self, topic_id: int, option_id: int, shares: float | None = None) -> None:
        r = await self._trade(topic_id, self._writer(topic_id).exit_consolidated, topic_id, option_id, shares)
        if isinstance(r, dict) and (r.get("error") or r.get("success") is False):
            raise ApiError(f"Exit rejected: {r.get('error') or r.get('message') or 'unknown reason'}")

    async def buy_many(self, groups: list[tuple[int, list[tuple[int, float]]]]) -> list[Fill]:
        """(topic, [(option id, contracts)]) for many markets in one request: a bot's whole sweep of a series.
        The server fills each market on its own, so check each Fill.error. Never retried."""
        first = groups[0][0] if groups else 0
        r = await self._trade(first, self._writer(first).enter_multi_topic_multi_leg,
                              [EnterMultiTopicLegGroup(topic_id=t, legs=[TradeLeg(option_id=o, contracts=c) for o, c in legs])
                               for t, legs in groups])
        by_topic = {x.topic_id: x for x in r.results or []}
        out = []
        for t, _ in groups:
            x = by_topic.get(t)
            if x is None or x.error:
                out.append(Fill(t, "", 0.0, 0.0, error=getattr(x, "error", None) or "no result returned"))
            else:
                out.append(Fill(t, str(x.trade_id), (x.total_cost_millisats or 0) / 1000, (x.commission_millisats or 0) / 1000))
        return out

    async def sell_many(self, groups: list[tuple[int, list[tuple[int, float]]]]) -> list[Fill]:
        """Sell (option id, contracts) in many markets in one request. `cost_sats` of each Fill is the proceeds.
        Never retried; check each Fill.error."""
        first = groups[0][0] if groups else 0
        r = await self._trade(first, self._writer(first).exit_multi_topic_multi_leg,
                              [ExitMultiTopicLegGroup(topic_id=t, legs=[ExitLegReq(option_id=o, contracts=c) for o, c in legs])
                               for t, legs in groups])
        by_topic = {x.topic_id: x for x in r.results or []}
        out = []
        for t, _ in groups:
            x = by_topic.get(t)
            if x is None or x.error:
                out.append(Fill(t, "", 0.0, 0.0, error=getattr(x, "error", None) or "no result returned"))
            else:
                out.append(Fill(t, str(x.trade_id), (x.total_proceeds_millisats or 0) / 1000, (x.commission_millisats or 0) / 1000))
        return out
