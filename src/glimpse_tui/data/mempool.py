"""The mempool-compatible REST API and WebSocket: mempool.space, a self-hosted mempool, and the explorer paths
Bitview serves in the same shape.

Every method returns `(value, Provenance)`. Blocks and confirmed transactions are cached for good; anything that
depends on the tip lives until about the next block. Shapes are the ones recorded under
tests/fixtures/sources/mempool/ on 19 Sep 2026.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from .core import FOREVER, LIVE, Provenance, Source

TIP = 45.0          # seconds a tip-dependent view stays fresh between stream pushes


@dataclass
class ProjectedTx:
    """One transaction of a projected block, from the stream's compact array: txid, fee, vsize, value, rate, flags, time."""
    txid: str
    fee: float
    vsize: float
    value: float
    rate: float
    flags: int = 0
    time: float = 0.0

    @classmethod
    def of(cls, row: list[Any]) -> ProjectedTx:
        return cls(str(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                   int(row[5]) if len(row) > 5 and row[5] is not None else 0, float(row[6]) if len(row) > 6 and row[6] else 0.0)


def apply_delta(txs: dict[str, ProjectedTx], delta: dict[str, Any]) -> None:
    """`track-mempool-block` sends the whole block once, then deltas: added rows, removed txids, changed [txid, rate, …]."""
    for txid in delta.get("removed", []):
        txs.pop(txid if isinstance(txid, str) else txid[0], None)
    for row in delta.get("added", []):
        tx = ProjectedTx.of(row)
        txs[tx.txid] = tx
    for row in delta.get("changed", []):
        if (tx := txs.get(row[0])) and len(row) > 1:
            tx.rate = float(row[1])
            if len(row) > 2 and row[2] is not None:
                tx.flags = int(row[2])


class Mempool(Source):
    def __init__(self, base_url: str = "https://mempool.space", name: str = "mempool.space", rate: float = 1.0) -> None:
        super().__init__(name=name, base_url=base_url.rstrip("/"), delay=LIVE, rate=rate, burst=4,
                         serves="blocks, transactions, addresses, projected blocks, fees, RBF, mining, difficulty, Lightning")

    async def _j(self, path: str, ttl: float = TIP, params: dict | None = None, persist: bool = False, text: bool = False
                 ) -> tuple[Any, Provenance]:
        v, at = await self.get(path, params, ttl=ttl, persist=persist, text=text)
        return v, self.prov(at)

    # fees and the mempool ───────────────────────────────────
    async def fees(self): return await self._j("/api/v1/fees/recommended", 20)
    async def fees_precise(self): return await self._j("/api/v1/fees/precise", 20)
    async def mempool(self): return await self._j("/api/mempool", 20)
    async def mempool_blocks(self): return await self._j("/api/v1/fees/mempool-blocks", 20)
    async def fee_rates(self, period: str = "1w"): return await self._j(f"/api/v1/mining/blocks/fee-rates/{period}", 600)

    # blocks ─────────────────────────────────────────────────
    async def tip_height(self) -> tuple[int, Provenance]:
        v, p = await self._j("/api/blocks/tip/height", 15, text=True)
        return int(str(v).strip()), p

    async def blocks(self, start: int | None = None):
        return await self._j(f"/api/v1/blocks/{start}" if start is not None else "/api/v1/blocks", 30)

    async def block(self, block_hash: str): return await self._j(f"/api/v1/block/{block_hash}", FOREVER, persist=True)
    async def block_txs(self, block_hash: str, start: int = 0): return await self._j(f"/api/block/{block_hash}/txs/{start}", FOREVER)
    async def audit_summary(self, block_hash: str): return await self._j(f"/api/v1/block/{block_hash}/audit-summary", FOREVER)

    async def block_hash(self, height: int) -> tuple[str, Provenance]:
        v, p = await self._j(f"/api/block-height/{height}", FOREVER, text=True)
        return str(v).strip(), p

    # transactions and addresses ─────────────────────────────
    async def tx(self, txid: str) -> tuple[dict, Provenance]:
        v, p = await self._j(f"/api/tx/{txid}", 20)
        if v.get("status", {}).get("confirmed"):
            self._mem[self._key(f"/api/tx/{txid}", None)] = (p.fetched_at, p.fetched_at + 3600, v)   # confirmed: stop asking
        return v, p

    async def outspends(self, txid: str): return await self._j(f"/api/tx/{txid}/outspends", 60)
    async def cpfp(self, txid: str): return await self._j(f"/api/v1/cpfp/{txid}", 30)
    async def rbf(self, txid: str): return await self._j(f"/api/v1/tx/{txid}/rbf", 30)
    async def tx_times(self, txids: list[str]): return await self._j("/api/v1/transaction-times", 300, params={"txId[]": txids})
    async def address(self, a: str): return await self._j(f"/api/address/{a}", 30)
    async def address_txs(self, a: str): return await self._j(f"/api/address/{a}/txs", 30)
    async def address_utxo(self, a: str): return await self._j(f"/api/address/{a}/utxo", 30)
    async def replacements(self, full: bool = False):
        return await self._j("/api/v1/fullrbf/replacements" if full else "/api/v1/replacements", 15)


    # mining ─────────────────────────────────────────────────
    async def pools(self, period: str = "1w"): return await self._j(f"/api/v1/mining/pools/{period}", 600)
    async def pool(self, slug: str): return await self._j(f"/api/v1/mining/pool/{slug}", 600)
    async def pool_blocks(self, slug: str): return await self._j(f"/api/v1/mining/pool/{slug}/blocks", 120)
    async def hashrate(self, period: str = "3m"): return await self._j(f"/api/v1/mining/hashrate/{period}", 1800, persist=True)
    async def difficulty_adjustment(self): return await self._j("/api/v1/difficulty-adjustment", 60)
    async def difficulty_adjustments(self, period: str = "1y"):
        return await self._j(f"/api/v1/mining/difficulty-adjustments/{period}", 3600, persist=True)

    async def rewards(self, period: str = "1w"): return await self._j(f"/api/v1/mining/blocks/rewards/{period}", 600)
    async def block_fees(self, period: str = "1w"): return await self._j(f"/api/v1/mining/blocks/fees/{period}", 600)
    async def reward_stats(self, blocks: int = 144): return await self._j(f"/api/v1/mining/reward-stats/{blocks}", 300)
    async def prices(self): return await self._j("/api/v1/prices", 60)

    # Lightning ──────────────────────────────────────────────
    async def ln_latest(self): return await self._j("/api/v1/lightning/statistics/latest", 1800)
    async def ln_stats(self, period: str = "3m"): return await self._j(f"/api/v1/lightning/statistics/{period}", 3600, persist=True)
    async def ln_rankings(self): return await self._j("/api/v1/lightning/nodes/rankings", 3600)
    async def ln_countries(self): return await self._j("/api/v1/lightning/nodes/countries", 3600)
    async def ln_isps(self): return await self._j("/api/v1/lightning/nodes/isp-ranking", 3600)

    # the stream ─────────────────────────────────────────────

    @property
    def ws_url(self) -> str:
        return ("wss://" if self.base_url.startswith("https") else "ws://") + self.host + "/api/v1/ws"

    async def stream(self, want: tuple[str, ...] = ("blocks", "mempool-blocks", "stats"),
                     commands: asyncio.Queue | None = None, first: tuple[dict, ...] = ()) -> AsyncIterator[dict[str, Any]]:
        """Yield every message from the WebSocket until it drops. The caller reconnects with backoff, or polls.
        `commands` carries later subscriptions (`{"track-mempool-block": 0}`, `{"track-rbf": "all"}`) to the socket."""
        import websockets

        async with websockets.connect(self.ws_url, open_timeout=10, ping_interval=25, max_size=2**24) as ws:
            await ws.send(json.dumps({"action": "init"}))
            await ws.send(json.dumps({"action": "want", "data": list(want)}))
            for cmd in first:
                await ws.send(json.dumps(cmd))
            self.health.ok += 1

            async def pump() -> None:
                while commands is not None:
                    await ws.send(json.dumps(await commands.get()))

            sender = asyncio.create_task(pump())
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        yield msg
            finally:
                sender.cancel()


def backoff(attempt: int, cap: float = 60.0) -> float:
    """Exponential with jitter: 1, 2, 4 … `cap` seconds, each scaled by 0.5 to 1.5."""
    return min(2.0 ** attempt, cap) * (0.5 + random.random())


async def sleep_backoff(attempt: int) -> None:
    await asyncio.sleep(backoff(attempt))
