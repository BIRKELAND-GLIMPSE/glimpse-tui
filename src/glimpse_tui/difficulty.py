"""Bitcoin's difficulty from its own rule: a simulation of what the coming retargets will set.

Glimpse runs two markets on every retarget: the difficulty it sets, in terahash, and the percent change it makes. Both are
the same number seen two ways, and the chain decides it (rebuilt to 2e-5 on every retarget since 2014):

    D_next = D · 1,209,600 / (t[last block of the epoch] − t[first block of the epoch])

So a forecast only has to say how long the blocks still to come will take, and that is hash rate over difficulty. Height, not
the clock, is what this module steps in: every path reaches every retarget at the same height, at its own time.

    hash rate   the rate now, measured from the recent blocks and known only to a few percent, growing at the difficulty's own
                two-year trend, itself uncertain, and wandering, with a per-path scale and fat-tailed steps for the rare collapse
    blocks      the time for m blocks at hash rate H is Gamma(m, 600·D/H): the luck of the draw, which alone moves a whole
                epoch by 2.2%
    difficulty  the rule above, on each path. A lucky epoch sets the difficulty too high and the next one takes it back, which
                is why the spread over k epochs grows slower than the root of k without anything being fitted to say so

What did not earn a place in the search or on later retargets: the season, the hash price against its averages, Bitcoin's price
momentum, the last adjustment, the hash rate's own short-term momentum, and a separate part of the hash rate that comes and goes.
The next retarget is the rate and the luck.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from .zoo.core import PROB_FLOOR

HOUR, DAY = 3600.0, 86400.0
EPOCH = 2016                    # blocks between retargets
TARGET = 2016 * 600.0           # the seconds the rule says an epoch should take
MAX_STEP = 4.0                  # a retarget moves difficulty by at most this factor, either way
STEP = 36                       # blocks a simulation step: about six hours
HISTORY_BLOCKS = 4000           # blocks the state reads: more than the slowest rate memory the search may try
TREND_EPOCHS = 52               # the difficulty's trend is read over this many retargets, two years
MARKETS = 12                    # retargets ahead the series lists


@dataclass(frozen=True)
class Params:
    """Everything `simulate` can be tuned on. `difficulty_lab.fit` chooses these on past retargets, January 2021 to March 2025,
    maximising the log score on both markets' ranges. Growth is held to a share of the chain's own trend, with no constant: that
    period's best constant (+0.1% a day) assumed its boom would last, and was the worst far-out choice on the retargets after it."""
    rate_tau_h: float = 168.0       # memory of the hash rate estimate from recent blocks, hours
    rate_se: float = 0.03           # how far that estimate may be from the true rate, as a log
    growth_trend: float = 1.0       # the share of the difficulty's two-year growth a day that carries on
    growth_sd: float = 0.0006       # how unsure that growth is, a log a day, per path: nothing lets a far retarget assume today's pace
    sigma_day: float = 0.012        # how far the log hash rate wanders in a day
    vol_of_vol: float = 0.45        # sd of a path's log scale on that wander: some periods are calmer than others
    tail_df: float = 4.0            # degrees of freedom of its steps: the rare collapse or rush
    stamp_s: float = 300.0          # noise in the timestamps the rule reads, seconds

    def overlay(self, **kw) -> Params:
        known = {f.name for f in fields(self)}
        return Params(**{**asdict(self), **{k: v for k, v in kw.items() if k in known}})


def params_file() -> Path:
    from .auth import config_dir

    return config_dir() / "difficulty" / "params.json"


def load_params() -> Params:
    """The defaults, with whatever `glimpse-tui difficulty fit --save` chose laid over them."""
    try:
        return Params().overlay(**json.loads(params_file().read_text()))
    except (OSError, ValueError, TypeError):
        return Params()


def save_params(p: Params) -> Path:
    f = params_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(asdict(p), indent=1))
    return f


def retarget_after(height: int) -> int:
    """The first retarget height above `height`: the block that first carries the new difficulty."""
    return (height // EPOCH + 1) * EPOCH


@dataclass(frozen=True)
class State:
    """What is known at `now`: all a forecast starts from."""
    now: float
    height: int                 # the tip
    difficulty: float
    epoch_start: float          # the raw timestamp of the epoch's first block, which the rule reads
    rate: float                 # hash rate now, in units of difficulty per 600 seconds: rate / difficulty = 1 is on schedule
    trend: float                # log difficulty growth a day over the last two years

    @property
    def next_retarget(self) -> int:
        return retarget_after(self.height)

    @property
    def blocks_left(self) -> int:
        """Blocks until the new difficulty takes effect."""
        return self.next_retarget - self.height

    @property
    def pace(self) -> float:
        """The hash rate against the difficulty: 1 is on schedule."""
        return self.rate / self.difficulty

    def retarget_estimate(self) -> tuple[float, float]:
        """(when the retarget lands, the change it makes) if the rest of the epoch runs at the current rate."""
        last = self.next_retarget - 1
        left = last - self.height
        at = self.now + left * 600.0 / max(self.pace, 1e-9)
        change = TARGET / max(at - self.epoch_start, 1.0)
        return at + 600.0 / max(self.pace, 1e-9), float(np.clip(change, 1 / MAX_STEP, MAX_STEP) - 1)


def rate_from(ts: np.ndarray, difficulty: np.ndarray, now: float, tau_h: float) -> float:
    """The hash rate at `now` from the blocks' work over the time it took, exponentially weighted with a memory of `tau_h`,
    idle time since the last block counted. `ts` must be monotonic: the running maximum of the block times."""
    age = np.maximum(now - ts, 0.0)
    tau = tau_h * HOUR
    span = max(float(age[0]), 1.0)
    return float(np.dot(np.exp(-age / tau), difficulty)) * 600.0 / (tau * (1.0 - np.exp(-span / tau)))


def state_from(first: int, ts: np.ndarray, diff: np.ndarray, epochs: np.ndarray, now: float, tau_h: float) -> State:
    """The state at `now` from raw block times from height `first`, each block's difficulty, and every epoch's difficulty."""
    n = int(np.searchsorted(np.maximum.accumulate(ts), now, side="right"))
    if n < 288:
        raise ValueError("too few blocks to read the hash rate from")
    ts, diff = ts[:n], diff[:n]
    height = first + n - 1
    epoch = height // EPOCH
    i0 = epoch * EPOCH - first
    if i0 < 0:
        raise ValueError("the block history does not reach the start of this epoch")
    back = max(epoch - TREND_EPOCHS, 0)
    return State(float(now), height, float(diff[-1]), float(ts[i0]), rate_from(np.maximum.accumulate(ts), diff, now, tau_h),
                 float(np.log(diff[-1] / epochs[back]) / (max(epoch - back, 1) * 14.0)))


@dataclass(frozen=True)
class Run:
    """One simulation: the coming retargets, each with its height and every path's difficulty and arrival time."""
    heights: np.ndarray         # k retarget heights, nearest first
    difficulty: np.ndarray      # k × n: the difficulty each retarget sets, on each path
    at: np.ndarray              # k × n: when each retarget block is mined, unix seconds
    before: float               # the difficulty in force now

    def _k(self, height: int) -> int:
        k = int(np.searchsorted(self.heights, height))
        if k >= len(self.heights) or int(self.heights[k]) != height:
            raise KeyError(height)
        return k

    def difficulty_at(self, height: int) -> np.ndarray:
        """Paths of the difficulty the retarget at `height` sets."""
        return self.difficulty[self._k(height)]

    def adjustment_at(self, height: int) -> np.ndarray:
        """Paths of the change the retarget at `height` makes, as a factor: 1.05 is +5%."""
        k = self._k(height)
        return self.difficulty[k] / (self.difficulty[k - 1] if k else self.before)

    def arrival_at(self, height: int) -> np.ndarray:
        return self.at[self._k(height)]


def simulate(st: State, p: Params | None = None, epochs: int = MARKETS, n: int = 4000, seed: int = 0) -> Run:
    """`epochs` coming retargets on `n` paths. Heights are shared by every path; time is what differs."""
    p = p or Params()
    rng = np.random.default_rng(seed)
    first = st.next_retarget
    heights = first + EPOCH * np.arange(epochs)
    scale = np.exp(p.vol_of_vol * rng.standard_normal(n) - 0.5 * p.vol_of_vol**2) if p.vol_of_vol > 0 else np.ones(n)
    tail = np.sqrt((p.tail_df - 2.0) / p.tail_df) if p.tail_df > 2 else 1.0     # keeps a step's variance at sigma² whatever the tail
    mean_g = p.growth_trend * st.trend / DAY
    g = mean_g + p.growth_sd / DAY * rng.standard_normal(n)
    # the rate is an average of the last week or so: on a rising chain it reads what the hash rate was then, so it is carried to now
    x0 = float(np.log(max(st.rate, 1.0))) + mean_g * p.rate_tau_h * HOUR + p.rate_se * rng.standard_normal(n)
    wander = np.zeros(n)
    t = np.full(n, float(st.now))
    d = np.full(n, float(st.difficulty))
    began = np.full(n, float(st.epoch_start))
    out_d, out_t = np.empty((epochs, n)), np.empty((epochs, n))
    sig = p.sigma_day * scale / np.sqrt(DAY)

    def mine(blocks: int) -> None:
        """Advance every path by `blocks` blocks at the difficulty it is on."""
        nonlocal wander, t
        left = blocks
        while left > 0:
            m = min(left, STEP)
            dt = rng.gamma(m, 600.0 * d / np.exp(x0 + g * (t - st.now) + wander))
            shock = rng.standard_t(p.tail_df, n) * tail if p.tail_df > 2 else rng.standard_normal(n)
            wander = wander + sig * np.sqrt(dt) * shock
            t = t + dt
            left -= m

    last = int(first) - 1 - st.height            # blocks until the epoch's last block, the one the rule reads
    for k in range(epochs):
        mine(max(last, 0))
        stamp = p.stamp_s * rng.standard_normal(n) if p.stamp_s > 0 else 0.0
        took = np.maximum(t + stamp - began, 1.0)
        d = d * np.clip(TARGET / took, 1 / MAX_STEP, MAX_STEP)
        # the retarget block itself: the first at the new difficulty, and the first of the next epoch's measure
        mine(1)
        out_d[k], out_t[k] = d, t
        began = t
        last = EPOCH - 1
    return Run(heights.astype(int), out_d, out_t, float(st.difficulty))


def probabilities(samples: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """One probability per range of the ladder from simulated values, all positive: 199 quantiles of the log joined by straight
    lines, with linear tails out to the farthest path, so a few thousand paths leave no holes in the ladder. What falls
    outside the ladder is counted in its end ranges. Sums to 1."""
    x = np.log(np.maximum(np.asarray(samples, dtype=float), 1e-12))
    levels = np.arange(1, 200) / 200.0
    q = np.maximum.accumulate(np.quantile(x, levels)) + np.arange(199) * 1e-12
    lo = min(float(x.min()), q[0] - (q[9] - q[0])) - 1e-9
    hi = max(float(x.max()), q[-1] + (q[-1] - q[-10])) + 1e-9
    q = np.concatenate([[lo], q, [hi]])
    lv = np.concatenate([[0.0], levels, [1.0]])
    at = np.interp(np.log(np.maximum(edges, 1e-12)), q, lv, left=0.0, right=1.0)
    at[0], at[-1] = 0.0, 1.0
    p = np.maximum(np.diff(at), PROB_FLOOR)
    return p / p.sum()


# ── live ────────────────────────────────────────────────────

BITVIEW = "https://bitview.space/api/series"
STALE_AFTER = 900.0             # a forecast this far behind Bitview's answers is not one to trade on
READ_TRIES = 3                  # attempts at one read of Bitview
READ_EVERY = 30.0               # seconds between reads of the newest blocks: one lands every ten minutes or so
RUN_EVERY = 60.0                # seconds a simulation serves for, unless a new block replaces it sooner
PATHS = 6000


class Inputs:
    """What the retarget bots read, kept fresh: the recent blocks, the difficulty at every past retarget, and one simulation
    of every coming retarget, shared by both series. `refresh` is called from the runner's event loop, `run` from its drawing
    threads."""

    def __init__(self, params: Params | None = None) -> None:
        self.params = params or load_params()
        self.first = 0
        self.ts: np.ndarray | None = None       # raw block times: the rule reads them as they are
        self.diff: np.ndarray | None = None     # difficulty of each block
        self.epochs: np.ndarray | None = None   # the difficulty of every epoch so far, by epoch number
        self.read_at = 0.0
        self._run: Run | None = None
        self._run_key: tuple = ()
        self._run_at = 0.0
        self._lock = threading.Lock()

    async def _bulk(self, series: str, index: str, start: int) -> list[dict]:
        from .data import http

        for attempt in range(READ_TRIES):       # Bitview's connections time out now and then, and the next one is fine
            try:
                r = await http.client(BITVIEW).get(f"{BITVIEW}/bulk", params={"series": series, "index": index, "start": start})
                break
            except httpx.TransportError:
                if attempt == READ_TRIES - 1:
                    raise
        r.raise_for_status()
        return r.json()

    async def refresh(self) -> None:
        """Bitview's newest blocks, and the retargets when one has passed. What fails keeps its last answer; `ready` turns false
        once that has grown old. A reorg rewrites the top few blocks, so they are read again rather than appended."""
        now = time.time()
        try:
            if self.ts is None or now - self.read_at >= READ_EVERY:
                got = await self._bulk("timestamp,difficulty", "height", -HISTORY_BLOCKS if self.ts is None else -12)
                first = int(got[0]["start"])
                ts, diff = (np.array(x["data"], dtype=float) for x in got)
                if self.ts is None:
                    self.first, self.ts, self.diff = first, ts, diff
                else:
                    keep = max(first - self.first, 0)
                    self.ts = np.concatenate([self.ts[:keep], ts])[-HISTORY_BLOCKS:]
                    self.diff = np.concatenate([self.diff[:keep], diff])[-HISTORY_BLOCKS:]
                    self.first = first + len(ts) - len(self.ts)
                height = self.first + len(self.ts) - 1
                if self.epochs is None or len(self.epochs) <= height // EPOCH:
                    self.epochs = np.array((await self._bulk("difficulty", "difficulty_epoch", 0))[0]["data"], dtype=float)
                self.read_at = time.time()
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            pass

    @property
    def ready(self) -> bool:
        return self.ts is not None and self.epochs is not None and time.time() - self.read_at < STALE_AFTER

    @property
    def tip(self) -> int:
        return self.first + len(self.ts) - 1 if self.ts is not None else 0

    def hourly_bars(self, hours: int) -> pd.DataFrame:
        """The difficulty in terahash an hour, the last `hours`: what names the hour for the rest of the terminal."""
        end = int(time.time()) // 3600 * 3600
        index = pd.to_datetime(end - 3600 * np.arange(hours, 0, -1), unit="s", utc=True)
        at = np.searchsorted(np.maximum.accumulate(self.ts), index.as_unit("s").asi8 + 3600, side="right") - 1
        close = self.diff[np.maximum(at, 0)] / 1e12
        return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 0.0}, index=index)

    def adjustment_bars(self, hours: int) -> pd.DataFrame:
        """`hourly_bars` read as the adjustment market charts them: the percent change the retarget in force made, a step at each
        retarget."""
        end = int(time.time()) // 3600 * 3600
        index = pd.to_datetime(end - 3600 * np.arange(hours, 0, -1), unit="s", utc=True)
        at = np.searchsorted(np.maximum.accumulate(self.ts), index.as_unit("s").asi8 + 3600, side="right") - 1
        epoch = np.clip((self.first + np.maximum(at, 0)) // EPOCH, 1, len(self.epochs) - 1)
        close = (self.epochs[epoch] / self.epochs[epoch - 1] - 1.0) * 100.0
        return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 0.0}, index=index)

    def state(self, now: float | None = None) -> State:
        return state_from(self.first, self.ts, self.diff, self.epochs, time.time() if now is None else now, self.params.rate_tau_h)

    def run(self, now: float, n: int = PATHS) -> Run:
        """The simulation of every coming retarget: a new one when a block lands, or after RUN_EVERY if none has."""
        with self._lock:
            key = (self.tip,)
            if key != self._run_key or now - self._run_at > RUN_EVERY or self._run is None:
                self._run = simulate(self.state(now), self.params, n=n, seed=self.tip)
                self._run_key, self._run_at = key, now
            return self._run


_SHARED: Inputs | None = None


def shared_inputs() -> Inputs:
    """One set of inputs per process, so the two series read one picture of one chain."""
    global _SHARED
    if _SHARED is None:
        _SHARED = Inputs()
    return _SHARED
