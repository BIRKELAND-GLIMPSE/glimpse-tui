"""Thin async data layer over the glimpse-markets SDK.

The SDK has no retries and its list endpoints paginate on limit/offset (its own
page/page_size arguments are ignored by the server), so this module owns paging,
429 backoff and the conversion of raw responses into the small records the UI needs.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
from glimpse_markets import AsyncClient, EnterMultiTopicLegGroup, TradeLeg
from glimpse_markets.exceptions import (
    GlimpseAmbiguousTradeStateError,
    GlimpseAPIError,
    GlimpseAuthenticationError,
    GlimpseError,
    GlimpseRateLimitError,
    GlimpseTradingNotEligibleError,
)
from glimpse_markets.ratelimit import AsyncRateLimiter

from . import pricing

NMARKET = "/api/v1/nmarket"
PAGE = 48
COINBASE = "https://api.exchange.coinbase.com/products/{pair}"
PAIRS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XAU": "PAXG-USD"}


class ApiError(Exception):
    """A message that is safe and useful to show in the status line."""


def asset_of(title: str) -> str:
    """Ticker named by a series or market title, or '' if it names none we know."""
    t = title.lower()
    return next((sym for needle, sym in (("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"), ("gold", "XAU")) if needle in t), "")


@dataclass(frozen=True)
class Batch:
    batch_id: str
    title: str
    topic_count: int
    interval: float

    @property
    def asset(self) -> str:
        return asset_of(self.title)

    @property
    def hourly(self) -> bool:
        return "hour" in self.title.lower()

    @property
    def short(self) -> str:
        """The ticker alone. Bitcoin has two series, so the daily one is marked; the hourly one is plain BTC."""
        if not self.asset:
            return self.title
        return f"{self.asset} 1D" if self.asset == "BTC" and not self.hourly else self.asset


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


async def candles(asset: str, granularity: int) -> list[Candle]:
    """Public Coinbase candles, oldest first. Up to 350 buckets of `granularity` seconds."""
    pair = PAIRS.get(asset)
    if not pair:
        return []
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "glimpse-tui"}) as c:
        r = await c.get(COINBASE.format(pair=pair) + "/candles", params={"granularity": granularity})
        r.raise_for_status()
    return [Candle(int(x[0]), float(x[3]), float(x[2]), float(x[1]), float(x[4])) for x in sorted(r.json(), key=lambda x: x[0])]


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


def parse_bin(name: str) -> tuple[float, float]:
    lo, _, hi = name.replace(",", "").partition("-")
    return float(lo), float(hi)


def _friendly(e: Exception) -> ApiError:
    if isinstance(e, GlimpseAuthenticationError):
        return ApiError("API key rejected. Press L to enter a new one.")
    if isinstance(e, GlimpseTradingNotEligibleError):
        return ApiError(f"Trading not enabled on this account: {e.reason or e.message}. Finish onboarding on the website.")
    if isinstance(e, GlimpseAmbiguousTradeStateError):
        return ApiError("Connection dropped mid-trade. The order may or may not have filled: check the portfolio before retrying.")
    if isinstance(e, GlimpseRateLimitError):
        return ApiError("Rate limited by Glimpse. Slowing down.")
    if isinstance(e, GlimpseAPIError):
        return ApiError(f"Glimpse {e.status_code}: {e.error or e.message or 'request failed'}")
    if isinstance(e, httpx.TimeoutException):
        return ApiError("Glimpse timed out.")
    if isinstance(e, httpx.HTTPError):
        return ApiError("Cannot reach Glimpse. Check the connection.")
    return ApiError(str(e) or e.__class__.__name__)


class Glimpse:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        kw = {"base_url": base_url} if base_url else {}
        self._c = AsyncClient(api_key=api_key, **kw)
        # Market data is public and not limited per key, so it gets its own client and budget:
        # polling and pre-trade estimates never queue behind, or eat into, the 60/min keyed allowance.
        self._pub = AsyncClient(rate_limiter=AsyncRateLimiter(240, 60), **kw)
        self.authenticated = bool(api_key)

    async def close(self) -> None:
        for c in (self._c, self._pub):
            await c.aclose() if hasattr(c, "aclose") else await c.close()

    async def _call(self, fn, *a, retry: bool = True, **kw):
        """Reads retry on 429/5xx/transport errors. Trades never retry: there is no idempotency key."""
        for attempt in range(3 if retry else 1):
            try:
                return await fn(*a, **kw)
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
        r = await self._call(self._pub.batches)
        out = [
            Batch(b.batch_id, b.main_topic_title or "?", b.topic_count or 0, float(b.outcome_interval or 0))
            for b in r.batches or []
        ]
        order = ["BTC", "BTC 1D", "ETH", "SOL", "XAU"]
        return sorted(out, key=lambda b: order.index(b.short) if b.short in order else len(order))

    async def markets(self, batch_id: str, limit: int = 96) -> list[MarketRow]:
        """Nearest-expiry active markets. v2 rows carry shares, so the list is priced without per-market calls."""
        rows: list[MarketRow] = []
        offset = 0
        while len(rows) < limit:
            d = await self._call(
                self._pub._get, f"{NMARKET}/v2/batches/{batch_id}/active-markets",
                {"limit": min(PAGE, limit - len(rows)), "offset": offset},
            )
            for m in d.get("markets") or []:
                outs = m.get("outcomes") or []
                rows.append(MarketRow(
                    topic_id=m["topic_id"], title=m.get("title") or "", end_time_utc=int(m.get("end_time_utc") or 0),
                    quote_mode=m.get("quote_mode") or "live",
                    volume_msat=int(m.get("total_volume_millisats") or 0),
                    volume_24h_msat=int(m.get("volume_24h_millisats") or 0),
                    shares=tuple(float(o.get("shares") or 0) for o in outs),
                    names=_shared(tuple(o.get("name") or "" for o in outs)),
                    option_ids=_shared(tuple(int(o.get("option_id") or i + 1) for i, o in enumerate(outs))),
                ))
            if not d.get("has_more"):
                break
            offset = d.get("next_offset") or offset + PAGE
        now = time.time()
        return sorted((r for r in rows if r.end_time_utc > now), key=lambda r: r.end_time_utc)

    async def book(self, topic_id: int) -> Book:
        q = await self._call(self._pub.market_quotes, topic_id)
        outs = q.outcomes or []
        b = Book(
            topic_id=topic_id, title=q.title or "", end_time_utc=int(q.market_end_time_utc or 0),
            quote_mode=str(getattr(q.quote_mode, "value", q.quote_mode) or "live"),
            volume_msat=int(q.total_volume_millisats or 0),
            option_ids=[o.option_id for o in outs],
            bins=[parse_bin(o.name) for o in outs],
            shares=[float(o.shares or 0) for o in outs],
            alpha=pricing.alpha_for(max(len(outs), 2)),
            fetched_at=time.time(),
        )
        b.reprice()
        return b

    async def estimate(self, book: Book, lo: int, hi: int, contracts: float) -> tuple[float, float]:
        """Server's (cost, fee) in sats for a range buy. Public and side-effect free."""
        return await self.estimate_order(Order(book.topic_id, tuple(book.option_ids[lo:hi + 1]), contracts))

    async def estimate_order(self, order: Order) -> tuple[float, float]:
        e = await self._call(self._pub.estimate_trade, order.topic_id, "buy", order.legs())
        return (e.total_cost_millisats or 0) / 1000, (e.commission_millisats or 0) / 1000

    async def estimate_legs(self, topic_id: int, legs: list[tuple[int, float]]) -> tuple[float, float]:
        """Server's (cost, fee) in sats for (option id, contracts) legs of one market: what a bot's basket looks like."""
        e = await self._call(self._pub.estimate_trade, topic_id, "buy", [TradeLeg(option_id=o, contracts=c) for o, c in legs])
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
        r = await self._call(self._c.portfolio_active)
        return [
            Position(
                topic_id=p.topic_id, option_id=p.option_id, topic_title=p.topic_title or "",
                option_name=p.option_name or "", shares=float(p.shares or 0),
                cost_sats=float(p.purchase_value or 0) / 1000, value_sats=float(p.current_value or 0) / 1000,
                end_time=int(p.market_end_time or 0), status=p.status or "",
            )
            for p in r.message or []
        ]

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
        r = await self._call(
            self._c.enter_multi_topic_multi_leg,
            [EnterMultiTopicLegGroup(topic_id=o.topic_id, legs=o.legs()) for o in orders], retry=False,
        )
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
        r = await self._call(
            self._c.enter_multi_topic_multi_leg,
            [EnterMultiTopicLegGroup(topic_id=topic_id, legs=[TradeLeg(option_id=o, contracts=c) for o, c in legs])], retry=False,
        )
        x = (r.results or [None])[0]
        if x is None or x.error:
            return Fill(topic_id, "", 0.0, 0.0, error=getattr(x, "error", None) or "no result returned")
        return Fill(topic_id, str(x.trade_id), (x.total_cost_millisats or 0) / 1000, (x.commission_millisats or 0) / 1000)

    async def sell(self, topic_id: int, option_id: int, shares: float | None = None) -> None:
        r = await self._call(self._c.exit_consolidated, topic_id, option_id, shares, retry=False)
        if isinstance(r, dict) and (r.get("error") or r.get("success") is False):
            raise ApiError(f"Exit rejected: {r.get('error') or r.get('message') or 'unknown reason'}")

