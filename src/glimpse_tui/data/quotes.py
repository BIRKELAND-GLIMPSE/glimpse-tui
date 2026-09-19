"""The quote board: one `Quote` per instrument, from the first of its sources that answers (TERMINAL.md 4.4).

Live where an open feed exists (exchange tickers, Kraken's spot FX and tokenised shares), daily where only
official data exists (FRED, the Treasury, the ECB), computed where the brief says so (the dollar index), and
labelled either way. An instrument with no open source of its own is shown through its proxy and says `proxy`.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from . import fx_ref
from .core import LIVE, Provenance, SourceError
from .prices import Tick, composite

if TYPE_CHECKING:
    from ..term.hub import Hub, Quote
    from ..term.instruments import Instrument

EXCHANGES = ("coinbase", "kraken", "bitstamp")
SLOW_S = 6.0


def kraken_key(result_key: str) -> str:
    """Kraken answers `XBTUSD` as `XXBTZUSD` and `EURUSD` as `ZEURZUSD`; newer pairs keep their name."""
    k = result_key
    if len(k) == 8 and k[0] in "XZ" and k[4] in "XZ":
        return k[1:4] + k[5:8]
    return k


def _quote(hub: Hub, ins: Instrument, t: Tick, history: tuple[float, ...] = ()) -> Quote:
    from ..term.hub import Quote
    from ..term.tape import instrument_open
    return Quote(ins.ticker, t.price, t.prov, prev=t.open, high=t.high, low=t.low, history=history,
                 closed=not instrument_open(ins.session))


async def _exchange_tick(hub: Hub, kind: str, ident: str, kraken_batch: dict[str, Tick]) -> Tick | None:
    try:
        if kind == "kraken":
            return kraken_batch.get(ident.upper()) or kraken_batch.get(ident)
        if kind == "coinbase":
            return await hub.sources["coinbase"].tick(ident)
        if kind == "bitstamp":
            return await hub.sources["bitstamp"].tick(ident)
    except SourceError:
        return None
    return None


async def _daily(hub: Hub, ins: Instrument, kind: str, ident: str) -> Quote | None:
    from ..term.hub import Quote
    try:
        if kind == "fred":
            s = await hub.sources["fred"].series(ident, start=_years_ago(2))
            if s.last is None:
                return None
            return Quote(ins.ticker, s.last, s.prov, prev=s.prev, history=tuple(s.values[-30:]), closed=True)
        if kind == "treasury":
            days, prov = await hub.sources["treasury"].recent()
            vals = [d.rates[ident] for d in days if ident in d.rates]
            if not vals:
                return None
            return Quote(ins.ticker, vals[-1], Provenance(prov.source, prov.fetched_at, days[-1].date, prov.delay),
                         prev=vals[-2] if len(vals) > 1 else None, history=tuple(vals[-30:]), closed=True)
        if kind == "ecb":
            fixes, prov = await hub.sources["ecb"].fixes()
            vals = [v for f in fixes if (v := f.cross(ins.ticker)) is not None]
            if not vals:
                return None
            return Quote(ins.ticker, vals[-1], prov, prev=vals[-2] if len(vals) > 1 else None, history=tuple(vals[-30:]), closed=True)
    except SourceError:
        return None
    return None


def _years_ago(n: int) -> str:
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    return f"{now.year - n}-{now.month:02d}-01"


def expand(hub: Hub, tickers: list[str]) -> list[Instrument]:
    """The instruments to fetch: the ones asked for, their proxy chains, and the dollar index's six legs."""
    out: dict[str, Instrument] = {}

    def add(t: str, depth: int = 0) -> None:
        ins = hub.book.get(t)
        if not ins or ins.ticker in out or depth > 3:
            return
        out[ins.ticker] = ins
        if ins.proxy:
            add(ins.proxy, depth + 1)
        if "computed:dxy" in ins.sources:
            for leg in fx_ref.DXY_WEIGHTS:
                add(leg, depth + 1)

    for t in tickers:
        add(t)
    return list(out.values())


async def refresh(hub: Hub, tickers: list[str]) -> None:
    """Bring `hub.quotes` up to date for these tickers. One Kraken request covers every Kraken pair."""
    from ..term.hub import Quote
    want = expand(hub, tickers)
    pairs = [i.source("kraken") for i in want if i.source("kraken")]
    batch: dict[str, Tick] = {}
    if pairs and "kraken" in hub.sources:
        try:
            raw = await hub.sources["kraken"].ticks(pairs)
            batch = {kraken_key(k).upper(): v for k, v in raw.items()} | {k.upper(): v for k, v in raw.items()}
        except SourceError:
            batch = {}

    async def one(ins: Instrument) -> None:
        if ins.ticker == "BTC":                                     # the composite: every venue, the median
            got = await asyncio.gather(*[_exchange_tick(hub, s.split(":")[0], s.split(":")[1], batch) for s in ins.sources
                                         if s.split(":")[0] in EXCHANGES])
            names = [s.split(":")[0] for s in ins.sources if s.split(":")[0] in EXCHANGES]
            ticks = {n: t for n, t in zip(names, got, strict=True) if t}
            if (c := composite(ticks)):
                q = _quote(hub, ins, c)
                q.parts = tuple((n, t.price) for n, t in ticks.items())
                hub.quotes[ins.ticker] = q
            return
        if (pair := ins.source("kraken")) and (t := batch.get(pair.upper())):
            hub.quotes[ins.ticker] = _quote(hub, ins, t)               # already in hand from the one Kraken request
            return
        for s in ins.sources:
            kind, _, ident = s.partition(":")
            if kind in EXCHANGES:
                if t := await _exchange_tick(hub, kind, ident, batch):
                    hub.quotes[ins.ticker] = _quote(hub, ins, t)
                    return
            elif kind in ("fred", "treasury", "ecb"):
                # A slow official server (the Treasury takes 18 s) must not hold the live tape: give it a few seconds,
                # then let the fetch finish in the background. The next cycle finds it in the cache.
                task = asyncio.ensure_future(_daily(hub, ins, kind, ident))
                try:
                    q = await asyncio.wait_for(asyncio.shield(task), SLOW_S)
                except TimeoutError:
                    task.add_done_callback(lambda t, tk=ins.ticker: _late(hub, tk, t))
                    return
                if q:
                    hub.quotes[ins.ticker] = q
                    return

    await asyncio.gather(*[one(i) for i in want if "computed:dxy" not in i.sources], return_exceptions=True)

    for ins in want:                                                # computed and proxied, once their inputs are in
        if "computed:dxy" in ins.sources:
            legs = {p: hub.quotes[p] for p in fx_ref.DXY_WEIGHTS if p in hub.quotes}
            v = fx_ref.dxy({p: q.price for p, q in legs.items()})
            prev = fx_ref.dxy({p: (q.prev or q.price) for p, q in legs.items()})
            if v and legs:
                oldest = min(legs.values(), key=lambda q: q.prov.as_of).prov
                delay = LIVE if all(q.prov.delay == LIVE for q in legs.values()) else "daily"
                hub.quotes[ins.ticker] = Quote(ins.ticker, v, Provenance("computed", oldest.fetched_at, oldest.as_of, delay,
                                               "ICE formula over six FX legs"), prev=prev)
    for ins in want:
        if ins.ticker not in hub.quotes and ins.proxy:
            via = _resolve_proxy(hub, ins)
            if via:
                q = hub.quotes[via]
                hub.quotes[ins.ticker] = Quote(ins.ticker, q.price, q.prov, q.prev, q.high, q.low, q.history, proxy_for=ins.ticker,
                                               closed=q.closed, via=via)


def _late(hub: Hub, ticker: str, task: asyncio.Future) -> None:
    if not task.cancelled() and task.exception() is None and (q := task.result()):
        hub.quotes[ticker] = q
        hub.changed()


def _resolve_proxy(hub: Hub, ins: Instrument, depth: int = 0) -> str | None:
    """The first instrument down the proxy chain that has a quote of its own: SPY -> SPYx."""
    if depth > 3 or not ins.proxy:
        return None
    nxt = hub.book.get(ins.proxy)
    if not nxt:
        return None
    q = hub.quotes.get(nxt.ticker)
    if q and not q.proxy_for:
        return nxt.ticker
    return _resolve_proxy(hub, nxt, depth + 1)


async def history(hub: Hub, ins: Instrument, days: int = 400) -> tuple[list[float], list[float], Provenance]:
    """Daily closes, oldest first, from the first source that has them. Raises SourceError when none does."""
    last: Exception | None = None
    for s in ins.sources:
        kind, _, ident = s.partition(":")
        try:
            if kind == "kraken":
                rows, prov = await hub.sources["kraken"].ohlc(ident, 1440)
                rows = rows[-days:]
                return [float(r[0]) for r in rows], [float(r[4]) for r in rows], prov
            if kind == "coinbase":
                rows, prov = await hub.sources["coinbase"].candles(ident, 86400)
                rows = sorted(rows, key=lambda r: r[0])[-days:]
                return [float(r[0]) for r in rows], [float(r[4]) for r in rows], prov
            if kind == "fred":
                srs = await hub.sources["fred"].series(ident, start=_years_ago(max(days // 250 + 1, 2)))
                return srs.dates[-days:], srs.values[-days:], srs.prov
            if kind == "ecb":
                fixes, prov = await hub.sources["ecb"].fixes()
                pts = [(f.date, v) for f in fixes if (v := f.cross(ins.ticker)) is not None]
                return [p[0] for p in pts], [p[1] for p in pts], prov
            if kind == "treasury":
                cur, prov = await hub.sources["treasury"].curves(year=None)
                pts = [(d.date, d.rates[ident]) for d in cur if ident in d.rates][-days:]
                return [p[0] for p in pts], [p[1] for p in pts], prov
        except SourceError as e:
            last = e
    if ins.proxy and (nxt := hub.book.get(ins.proxy)):
        return await history(hub, nxt, days)
    raise SourceError(str(last) if last else f"{ins.ticker}: no open source carries its history")
