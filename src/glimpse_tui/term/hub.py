"""The hub: every source, the live chain state, the quote board, and the loops that keep them fresh.

Panes never talk to the network on the paint path. They read `hub.chain` and `hub.quotes`, or await a source
through the hub inside `load`. Streams replace polling where a source offers one and fall back to polling when
they drop. Nothing here starts until a launchpad is first shown.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..data import core, http
from ..data.core import Provenance, Source
from . import config, instruments


@dataclass
class Quote:
    ticker: str
    price: float
    prov: Provenance
    prev: float | None = None               # the previous close, for change
    high: float | None = None
    low: float | None = None
    history: tuple[float, ...] = ()         # recent closes for a sparkline
    proxy_for: str = ""                     # set when another instrument is standing in for this one
    via: str = ""                           # the stand-in actually quoted: PAXG for XAU, SPYx for SPY
    closed: bool = False                    # its session is shut: the last trade, not a stale feed
    parts: tuple[tuple[str, float], ...] = ()   # a composite's inputs: (("coinbase", 81610.0), ...)

    @property
    def change(self) -> float | None:
        return self.price - self.prev if self.prev else None

    @property
    def pct(self) -> float | None:
        return (self.price / self.prev - 1) if self.prev else None


@dataclass
class ChainState:
    """What the status bar and half the Bitcoin pages read. Filled by the mempool stream or by polling."""
    height: int = 0
    tip_hash: str = ""
    tip_time: float = 0.0
    tip_pool: str = ""
    tip_txs: int = 0
    tip_fees_sat: float = 0.0
    block_seen_at: float = 0.0              # when this process first saw the tip: the flash
    fees: dict[str, float] = field(default_factory=dict)        # fastestFee, halfHourFee, hourFee, economyFee, minimumFee
    mempool_vsize: float = 0.0
    mempool_count: int = 0
    mempool_min_fee: float = 0.0
    mempool_blocks: list[dict[str, Any]] = field(default_factory=list)
    difficulty: dict[str, Any] = field(default_factory=dict)
    prov: Provenance | None = None
    streaming: bool = False

    @property
    def fastest_fee(self) -> float:
        return float(self.fees.get("fastestFee", 0) or 0)


class Hub:
    streams_default = True

    def __init__(self, cfg: dict[str, Any] | None = None, book: instruments.Book | None = None) -> None:
        self.cfg = cfg if cfg is not None else config.load()
        self.book = book if book is not None else instruments.load()
        self.sources: dict[str, Source] = {}
        self.chain = ChainState()
        self.quotes: dict[str, Quote] = {}
        self.watch: set[str] = set()                    # tickers some pane wants kept fresh, beyond the tape
        self.listeners: list[Callable[[], None]] = []
        self.events: deque[dict[str, Any]] = deque(maxlen=200)      # new blocks, filings, fired alerts: AL and the status line read these
        self.series_seen: list[tuple[str, str]] = []    # Bitview series a search has returned, for the GO bar's autocomplete
        self.streams_enabled = self.streams_default     # tests switch the WebSockets off
        self.projected: dict[str, Any] = {}             # txid -> ProjectedTx of the next block, while MEMP tracks it
        self.projected_version = 0
        self.rbf_latest: list[dict[str, Any]] = []
        self.stream_commands: asyncio.Queue = asyncio.Queue()
        # The Glimpse market's own forecast for hourly BTC, as (close time, median, band low, band high), nearest first.
        # The shell wires this to the app's loaded closes; without the app (tests, scripts) it is empty.
        self.forecast: Callable[[], list[tuple[float, float, float, float]]] = list
        self.account: Callable[[], dict[str, Any]] = dict       # the Glimpse account as the app last loaded it, for WAL
        # OMON's selection, handed to the heatmap as a box: (close index, lowest bin, highest bin). The order itself goes
        # through the heatmap's bet slip, Confirm and estimate gate, unchanged.
        self.handoff: Callable[[int, int, int], None] = lambda close, lo, hi: None
        self._tracking: dict[str, int] = {"block": 0, "rbf": 0}     # how many panes want each heavy subscription
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._build()

    # sources ────────────────────────────────────────────────

    def _build(self) -> None:
        from ..data import sources as S
        S.build(self)
        self.apply_config()

    def add(self, key: str, source: Source) -> Source:
        self.sources[key] = source
        return source

    def apply_config(self) -> None:
        """Re-point every Bitcoin backend at the configured URL and set the proxy. No restart (Phase 2 check)."""
        http.use_socks5(self.cfg.get("socks5", "") or "")
        b = self.cfg.get("bitcoin", {})
        for key in ("mempool", "bitview", "esplora"):
            if key in self.sources and b.get(key):
                self.sources[key].point_at(b[key])
                self.sources[key].name = self.sources[key].host
        for s in (self.sources[k] for k in ("sec", "sec_www", "sec_efts") if k in self.sources):
            s.user_agent = self.cfg.get("sec_user_agent", "") or ""        # clearing the setting clears it: nothing goes to the SEC

    def bitcoin_order(self) -> list[str]:
        return [k for k in self.cfg.get("bitcoin", {}).get("order", ["mempool", "bitview", "esplora"]) if k in self.sources]

    def is_public(self, key: str) -> bool:
        """True when a lookup through this backend leaves the user's own machines (rule 5)."""
        url = self.sources[key].base_url if key in self.sources else ""
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
        private = host in ("localhost", "127.0.0.1", "::1") or host.endswith((".local", ".onion", ".lan")) or host.startswith(
            ("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.30.", "172.31."))
        return not private and not http.socks5()

    # change notification ────────────────────────────────────

    def changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    def event(self, kind: str, **data: Any) -> None:
        self.events.append({"kind": kind, "at": time.time(), **data})

    # loops ──────────────────────────────────────────────────

    async def start(self) -> None:
        from ..data import feeds
        cache = config.cache_dir() / "terminal.db"
        if core._disk.path is None:
            core.use_disk_cache(str(cache))
        self._stopping = False
        self._tasks = [asyncio.create_task(c) for c in feeds.loops(self)]
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        await http.close_all()

    @property
    def stopping(self) -> bool:
        return self._stopping

    # heavy stream subscriptions, held only while a pane wants them ─────────────

    def stream_subscriptions(self) -> list[dict[str, Any]]:
        """What a fresh socket must ask for again after a reconnect."""
        out: list[dict[str, Any]] = []
        if self._tracking["block"] > 0:
            out.append({"track-mempool-block": 0})
        if self._tracking["rbf"] > 0:
            out.append({"track-rbf": "all"})
        return out

    def track(self, what: str, on: bool) -> None:
        """MEMP tracks the next block's transactions; RBF tracks replacements. The last pane to leave unsubscribes."""
        before = self._tracking[what]
        self._tracking[what] = max(before + (1 if on else -1), 0)
        if before == 0 and on:
            self.stream_commands.put_nowait({"track-mempool-block": 0} if what == "block" else {"track-rbf": "all"})
        elif before == 1 and not on:
            self.stream_commands.put_nowait({"track-mempool-block": -1} if what == "block" else {"track-rbf": "stop"})
            if what == "block":
                self.projected.clear()

    def tape_tickers(self) -> list[str]:
        want = [t.upper() for t in self.cfg.get("tape", []) if t.upper() != "MEMP"]
        return list(dict.fromkeys(["BTC", *want, *sorted(self.watch)]))

    def btc_price(self) -> float | None:
        q = self.quotes.get("BTC")
        return q.price if q else None
