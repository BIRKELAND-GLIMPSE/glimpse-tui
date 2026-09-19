"""Blockstream Esplora: the third Bitcoin fallback (SOURCES.md). Tip, fee estimates, blocks, transactions, addresses.

The method names and shapes match `data/mempool.py` for everything Esplora can answer, so a page that walks
`hub.bitcoin_order()` gets the same picture from either. What Esplora lacks (projected blocks, CPFP, RBF history,
mining, a block's `extras`) is simply absent: a caller catches `AttributeError` and moves on. There is no WebSocket.
Shapes are the ones recorded under tests/fixtures/sources/esplora/ on 19 Sep 2026.
"""
from __future__ import annotations

import math
from typing import Any

from .core import FOREVER, LIVE, Provenance, Source

TIP = 45.0
# recommended-fee key -> the confirmation target (blocks) whose estimate stands in for it
TARGETS = (("fastestFee", 1), ("halfHourFee", 3), ("hourFee", 6), ("economyFee", 144), ("minimumFee", 1008))


def recommended(estimates: dict[str, Any], precise: bool = False) -> dict[str, float]:
    """`/fee-estimates` is {confirmation target: sat/vB}. mempool's recommended shape is five named rates: whole
    sat/vB, never under 1. `precise` keeps three decimals instead, like `/api/v1/fees/precise`."""
    have = sorted((int(k), float(v)) for k, v in estimates.items() if str(k).isdigit())
    out: dict[str, float] = {}
    for key, target in TARGETS:
        rate = next((v for t, v in have if t >= target), have[-1][1] if have else 1.0)
        out[key] = round(rate, 3) if precise else max(math.ceil(rate - 1e-9), 1)
    return out


class Esplora(Source):
    def __init__(self, base_url: str = "https://blockstream.info", name: str = "blockstream.info", rate: float = 1.0) -> None:
        super().__init__(name=name, base_url=base_url.rstrip("/"), delay=LIVE, rate=rate, burst=3,
                         serves="fallback for tip, fee estimates, blocks, transactions and addresses")

    async def _j(self, path: str, ttl: float = TIP, text: bool = False, persist: bool = False) -> tuple[Any, Provenance]:
        v, at = await self.get(path, ttl=ttl, text=text, persist=persist)
        return v, self.prov(at)

    # fees and the mempool ───────────────────────────────────
    async def fees(self) -> tuple[dict[str, float], Provenance]:
        v, p = await self._j("/api/fee-estimates", 30)
        return recommended(v), p

    async def fees_precise(self) -> tuple[dict[str, float], Provenance]:
        v, p = await self._j("/api/fee-estimates", 30)
        return recommended(v, precise=True), p

    async def mempool(self): return await self._j("/api/mempool", 20)

    # blocks ─────────────────────────────────────────────────
    async def tip_height(self) -> tuple[int, Provenance]:
        v, p = await self._j("/api/blocks/tip/height", 15, text=True)
        return int(str(v).strip()), p

    async def blocks(self, start: int | None = None):
        """Ten blocks, newest first, from `start` down. No pool, fee or audit fields: a page shows a dash for those."""
        return await self._j(f"/api/blocks/{start}" if start is not None else "/api/blocks", 30)

    async def block(self, block_hash: str): return await self._j(f"/api/block/{block_hash}", FOREVER, persist=True)
    async def block_txs(self, block_hash: str, start: int = 0): return await self._j(f"/api/block/{block_hash}/txs/{start}", FOREVER)

    async def block_hash(self, height: int) -> tuple[str, Provenance]:
        v, p = await self._j(f"/api/block-height/{height}", FOREVER, text=True)
        return str(v).strip(), p

    # transactions and addresses ─────────────────────────────
    async def tx(self, txid: str) -> tuple[dict, Provenance]:
        v, p = await self._j(f"/api/tx/{txid}", 20)
        if v.get("status", {}).get("confirmed"):
            self._mem[self._key(f"/api/tx/{txid}", None)] = (p.fetched_at, p.fetched_at + 3600, v)     # confirmed: stop asking
        return v, p

    async def outspends(self, txid: str): return await self._j(f"/api/tx/{txid}/outspends", 60)
    async def address(self, a: str): return await self._j(f"/api/address/{a}", 30)
    async def address_txs(self, a: str): return await self._j(f"/api/address/{a}/txs", 30)
    async def address_txs_chain(self, a: str, last_txid: str): return await self._j(f"/api/address/{a}/txs/chain/{last_txid}", 60)
    async def address_utxo(self, a: str): return await self._j(f"/api/address/{a}/utxo", 30)
