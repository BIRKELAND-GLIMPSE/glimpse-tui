"""The hub's background loops: the chain (mempool stream, polling when it drops) and the quote board.

Streams reconnect with exponential backoff and jitter. While a stream is down the same state is polled at a
gentle rate, so the status bar keeps moving and the panels keep their last good values.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from . import quotes
from .core import SourceError
from .mempool import ProjectedTx, apply_delta, backoff

if TYPE_CHECKING:
    from ..term.hub import Hub

CHAIN_POLL_S = 20.0
QUOTE_POLL_S = 12.0


def loops(hub: Hub) -> list[Coroutine[Any, Any, None]]:
    return [chain_stream(hub), chain_poll(hub), quote_loop(hub)]


# ── the chain ───────────────────────────────────────────────

def apply_block(hub: Hub, b: dict[str, Any], fresh: bool) -> None:
    c = hub.chain
    if int(b.get("height", 0)) < c.height:
        return
    new = fresh and c.height and int(b["height"]) > c.height
    ex = b.get("extras") or {}
    c.height, c.tip_hash, c.tip_time = int(b["height"]), b.get("id", ""), float(b.get("timestamp", 0))
    c.tip_pool = (ex.get("pool") or {}).get("name", "")
    c.tip_txs, c.tip_fees_sat = int(b.get("tx_count", 0)), float(ex.get("totalFees", 0) or 0)
    if new:
        c.block_seen_at = time.time()
        hub.event("block", height=c.height, pool=c.tip_pool, txs=c.tip_txs, fees=c.tip_fees_sat, hash=c.tip_hash)


def apply_ws(hub: Hub, msg: dict[str, Any]) -> None:
    """One WebSocket message into the chain state. Keys as recorded in fixtures/sources/mempool/ws_*.json."""
    c, mp = hub.chain, hub.sources["mempool"]
    now = time.time()
    if info := msg.get("mempoolInfo"):
        c.mempool_count, c.mempool_vsize = int(info.get("size", 0)), float(info.get("bytes", 0))
        c.mempool_min_fee = float(info.get("mempoolminfee", 0)) * 1e5          # BTC/kvB to sat/vB
    if "mempool-blocks" in msg:
        c.mempool_blocks = msg["mempool-blocks"]
    if fees := msg.get("fees"):
        c.fees = fees
    if da := msg.get("da"):
        c.difficulty = da
    for b in msg.get("blocks") or []:
        apply_block(hub, b, fresh=False)
    if b := msg.get("block"):
        apply_block(hub, b, fresh=True)
        hub.projected.clear()                           # the picture starts again on the next template
    if pbt := msg.get("projected-block-transactions"):
        if "blockTransactions" in pbt:
            hub.projected.clear()
            for row in pbt["blockTransactions"]:
                tx = ProjectedTx.of(row)
                hub.projected[tx.txid] = tx
        elif "delta" in pbt:
            apply_delta(hub.projected, pbt["delta"])
        hub.projected_version += 1
    if rbf := msg.get("rbfLatest"):
        hub.rbf_latest = rbf
    c.prov = mp.prov(now)
    c.streaming = True


async def chain_stream(hub: Hub) -> None:
    attempt = 0
    while not hub.stopping:
        if not hub.streams_enabled:
            await asyncio.sleep(1.0)
            continue
        mp = hub.sources["mempool"]
        first = tuple(hub.stream_subscriptions())
        try:
            async for msg in mp.stream(commands=hub.stream_commands, first=first):
                attempt = 0
                apply_ws(hub, msg)
                hub.changed()
                if hub.stopping:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:                          # the socket dropped, or websockets is not importable: poll instead
            mp.health.last_error, mp.health.last_error_at = f"stream: {type(e).__name__}", time.time()
        hub.chain.streaming = False
        attempt += 1
        await asyncio.sleep(backoff(attempt))


async def poll_chain_once(hub: Hub) -> None:
    mp, c = hub.sources["mempool"], hub.chain
    try:
        c.fees, prov = await mp.fees()
        info, _ = await mp.mempool()
        c.mempool_count, c.mempool_vsize = int(info.get("count", 0)), float(info.get("vsize", 0))
        c.mempool_blocks, _ = await mp.mempool_blocks()
        blocks, _ = await mp.blocks()
        for b in reversed(blocks):
            apply_block(hub, b, fresh=bool(c.height))
        c.difficulty, _ = await mp.difficulty_adjustment()
        c.prov = prov
    except SourceError:
        pass                                            # health already has it; the last good state stays


async def chain_poll(hub: Hub) -> None:
    while not hub.stopping:
        if not hub.chain.streaming:
            await poll_chain_once(hub)
            hub.changed()
        await asyncio.sleep(CHAIN_POLL_S if hub.chain.height else 3.0)


# ── quotes ──────────────────────────────────────────────────

async def quote_loop(hub: Hub) -> None:
    while not hub.stopping:
        try:
            await quotes.refresh(hub, hub.tape_tickers())
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        hub.changed()
        await asyncio.sleep(QUOTE_POLL_S)
