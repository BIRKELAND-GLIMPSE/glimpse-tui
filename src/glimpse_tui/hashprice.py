"""Bitcoin's hashprice from its parts: what the oracle reads, and a simulation of what it will read at a future close.

Glimpse settles a hashprice market on Bitview's `hash_price` at the `hour1` slot that ends at the close. That series is
an identity over the blocks of the trailing 24 hours (rebuilt to 4e-6 dollars, block by block, on 5 November 2026):

    hash_price = K · Σ coinbase_usd_i / Σ difficulty_i        K = 144 · 600 · 10¹⁵ / 2³² dollars per unit of difficulty

    coinbase_usd_i = (subsidy(height_i) + fees_i) · price_i   each block valued at its own block's price

The number of blocks in the window cancels, top and bottom, so a lucky day does not move it. What moves it is the price
averaged over the window, the difficulty the window's blocks were mined at, and, a little, the fees: three things with
very different futures. So instead of drawing one random walk on the hashprice, which is smooth for a day and then steps
at a retarget, this module simulates the parts, hour by hour:

    price       a random walk at the Bitcoin volatility clock, reverting to the long-run volatility, with a per-path
                volatility multiplier for fat long-horizon tails
    hash rate   the current rate, measured from the recent blocks, drifting with the difficulty's own trend, responding
                with a lag to the price, and wandering. Blocks arrive as a Poisson process at hash rate over difficulty
    difficulty  Bitcoin's own rule, applied when a path reaches the retarget height: the old difficulty times two weeks
                over the time the epoch took on that path, known in part already
    subsidy     the schedule, by height; a halving lands on the right block
    fees        per-block fees, reverting from today's level to the recent median

The blocks already mined inside a future window are known exactly and go in as they are, which is why a close a few hours
away has hardly any spread. The same simulation reads every close at once, so a series of closes costs one run.
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

from .zoo.core import PROB_FLOOR, Scale

K = 144 * 600 * 1e15 / 2**32    # dollars per PH/s per day, per dollar of mean block revenue per unit of difficulty
HOUR, DAY = 3600.0, 86400.0
EPOCH = 2016                    # blocks between retargets
TARGET = 2016 * 600.0           # the seconds an epoch is meant to take
HALVING = 210_000
WINDOW = DAY                    # the oracle's trailing window
MAX_STEP = 4.0                  # a retarget moves difficulty by at most this factor, either way
HISTORY_BLOCKS = 2600           # blocks the state reads: the epoch so far, however far along, plus the rate and fee windows
HISTORY_HOURS = 2000            # hours of Bitcoin's price and of difficulty the state reads
MIN_HOURS = 400


def subsidy_btc(height: np.ndarray | float) -> np.ndarray:
    """The block subsidy at `height`, in bitcoin."""
    return 50.0 / 2.0 ** np.floor_divide(height, HALVING)


@dataclass(frozen=True)
class Params:
    """Everything `simulate` can be tuned on. `hashprice_lab.fit` chooses these on past closes; the defaults are its choice
    on the closes from May 2024 to the end of 2025, maximising the log score on the market's ranges. Only the volatility
    terms and the hash rate's memory move that score by more than the noise in the search; the rest are near a flat optimum."""
    vol_mult: float = 1.16          # on the Bitcoin volatility clock
    vol_tau_h: float = 24.0         # hours for today's Bitcoin volatility to fade into its long-run level
    vol_of_vol: float = 0.20        # sd of a path's log volatility multiplier: long-horizon tails
    rate_tau_h: float = 240.0       # memory of the hash rate estimate from recent blocks, hours
    drift_base: float = 0.001       # log hash rate growth a day before the trend
    drift_trend: float = 0.25       # the share of difficulty's growth a day over the last 12 weeks that carries on
    hash_sigma_day: float = 0.008   # how far the log hash rate wanders in a day
    elasticity: float = 0.20        # log hash rate per log price, after a lag
    elastic_tau_h: float = 168.0    # the lag: miners come and go over about this many hours
    fee_tau_h: float = 48.0         # hours for fees to fall back to their recent median
    fee_sigma_day: float = 0.25     # how far log fees wander in a day
    jitter: float = 0.0008          # what the hourly model cannot see (the exact last block, the price within an hour)
    retarget: bool = True           # False: difficulty stays put, the comparison that says what the retarget is worth
    known: bool = True              # False: ignores the blocks already in a window, the comparison for that

    def overlay(self, **kw) -> Params:
        known = {f.name for f in fields(self)}
        return Params(**{**asdict(self), **{k: v for k, v in kw.items() if k in known}})


def params_file() -> Path:
    from .auth import config_dir

    return config_dir() / "hashprice" / "params.json"


def load_params() -> Params:
    """The defaults, with whatever `glimpse-tui hashprice fit --save` chose laid over them."""
    try:
        return Params().overlay(**json.loads(params_file().read_text()))
    except (OSError, ValueError, TypeError):
        return Params()


def save_params(p: Params) -> Path:
    f = params_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(asdict(p), indent=1))
    return f


@dataclass(frozen=True)
class Blocks:
    """Per-block history, oldest first, heights consecutive from `first`. `ts` is the monotonic timestamp: the running
    maximum, which is what the oracle's 24-hour window is measured on."""
    first: int
    ts: np.ndarray
    usd: np.ndarray             # coinbase value in dollars, at the block's own price
    btc: np.ndarray             # coinbase value in bitcoin
    fees: np.ndarray            # transaction fees in bitcoin
    difficulty: np.ndarray

    def until(self, now: float) -> Blocks:
        """The blocks mined by `now`: what was known then."""
        n = int(np.searchsorted(self.ts, now, side="right"))
        return Blocks(self.first, self.ts[:n], self.usd[:n], self.btc[:n], self.fees[:n], self.difficulty[:n])

    def tail(self, n: int) -> Blocks:
        n = min(n, len(self.ts))
        cut = len(self.ts) - n
        return Blocks(self.first + cut, self.ts[cut:], self.usd[cut:], self.btc[cut:], self.fees[cut:], self.difficulty[cut:])

    @property
    def height(self) -> int:
        """The height of the last block."""
        return self.first + len(self.ts) - 1

    def trailing(self, since: float) -> tuple[float, float]:
        """(Σ coinbase dollars, Σ difficulty) of the blocks after `since`."""
        i = int(np.searchsorted(self.ts, since, side="right"))
        return float(self.usd[i:].sum()), float(self.difficulty[i:].sum())

    def hashprice(self) -> float:
        """The oracle's number at the last block, from the blocks alone."""
        usd, diff = self.trailing(float(self.ts[-1]) - WINDOW)
        return K * usd / diff if diff > 0 else 0.0


@dataclass(frozen=True)
class State:
    """What is known at `now`: all a forecast starts from."""
    now: float
    blocks: Blocks
    price: float                # Bitcoin's price now, in dollars
    scale: Scale                # Bitcoin's volatility clock
    lr_ratio: float             # the long-run hourly variance over today's: what the clock's variance fades to
    trend: float                # log difficulty growth a day over the last 12 weeks
    difficulty: float
    epoch_start: float          # when the epoch's first block was mined
    next_retarget: int          # the height of the first block at the new difficulty
    fee: float                  # fees per block now, in bitcoin: the last day's average
    fee_level: float            # the median of the daily averages in the block history: what fees revert to

    @property
    def height(self) -> int:
        return self.blocks.height

    @property
    def hashprice(self) -> float:
        return self.blocks.hashprice()

    @property
    def blocks_left(self) -> int:
        return self.next_retarget - self.height - 1

    def rate(self, tau_h: float) -> float:
        """The hash rate now, in units of difficulty per 600 seconds, so rate / difficulty = 1 is on schedule: the recent
        blocks' work over the time it took, exponentially weighted with a memory of `tau_h`, idle time since the last block
        counted."""
        b = self.blocks
        age = np.maximum(self.now - b.ts, 0.0)
        tau = tau_h * HOUR
        span = max(float(age[0]), 1.0)
        return float(np.dot(np.exp(-age / tau), b.difficulty)) * 600.0 / (tau * (1.0 - np.exp(-span / tau)))

    def retarget_estimate(self, tau_h: float = Params.rate_tau_h) -> tuple[float, float]:
        """(when the retarget lands, the change it makes) if the rest of the epoch runs at the current rate."""
        left = self.next_retarget - self.height - 1
        at = max(self.now, float(self.blocks.ts[-1])) + left * 600.0 * self.difficulty / max(self.rate(tau_h), 1e-9)
        took = at - self.epoch_start
        return at, float(np.clip(TARGET / max(took, 1.0), 1 / MAX_STEP, MAX_STEP) - 1)


def state_at(blocks: Blocks, bars: pd.DataFrame, now: float, price: float | None = None, difficulty_12w: float | None = None) -> State:
    """The state at `now` from the blocks and Bitcoin's complete hourly bars up to then. `price` overrides the last
    block's (a live quote is fresher). `difficulty_12w` is the difficulty 12 weeks back; without it the bars' own
    `difficulty` column is read, if they have one."""
    if len(bars) < MIN_HOURS:
        raise ValueError(f"only {len(bars)} hours of Bitcoin's price; need {MIN_HOURS}")
    b = blocks.until(now).tail(HISTORY_BLOCKS)
    if len(b.ts) < 288:
        raise ValueError("too few blocks to read the hash rate from")
    d = float(b.difficulty[-1])
    # the epoch's first block; a short history leaves the estimate on the block history's first
    start_h = b.height - b.height % EPOCH
    i0 = max(start_h - b.first, 0)
    # Bitcoin's volatility: the clock's EWMA for now, the whole bar history for the long run
    scale = Scale.fit(bars, asset="BTC")
    closes = bars["close"].to_numpy(dtype=float)
    r = np.diff(np.log(closes))
    lr = float(np.mean(r[np.isfinite(r)] ** 2))
    if difficulty_12w is None:
        difficulty_12w = float(bars["difficulty"].iloc[max(len(bars) - 2016, 0)]) if "difficulty" in bars else d
    trend = float(np.log(d / max(difficulty_12w, 1.0)) / 84.0)
    # fees: a day's average now, and the median day of the history
    daily = np.convolve(b.fees, np.ones(144) / 144.0, mode="valid")
    fee = float(max(daily[-1], 1e-6))
    level = float(max(np.median(daily), 1e-6))
    spot = float(price) if price else float(b.usd[-1] / max(b.btc[-1], 1e-9))
    return State(now=float(now), blocks=b, price=spot, scale=scale, lr_ratio=float(np.clip(lr / max(scale.s2_next, 1e-12), 0.05, 20.0)),
                 trend=trend, difficulty=d, epoch_start=float(b.ts[i0]), next_retarget=start_h + EPOCH,
                 fee=fee, fee_level=level)


def simulate(st: State, closes: np.ndarray | list[float], p: Params | None = None, n: int = 4000, seed: int = 0) -> dict[float, np.ndarray]:
    """{close: `n` simulated readings of the oracle at that close}. Closes are unix seconds on the hour, as the markets'
    are; ones already past are left out. One run reads them all."""
    p = p or Params()
    want = sorted({float(c) for c in closes if float(c) > st.now})
    if not want:
        return {}
    rng = np.random.default_rng(seed)
    start, end = st.now, want[-1]
    bounds = np.concatenate([[start], np.arange((start // HOUR + 1) * HOUR, end, HOUR), [end]])
    bounds = bounds[np.concatenate([[True], np.diff(bounds) > 1e-6])]
    dt = np.diff(bounds) / HOUR
    ahead = np.cumsum(dt)
    fade = np.exp(-ahead / p.vol_tau_h)
    var = st.scale.step_variances(start, end)[: len(dt)] * (fade + (1.0 - fade) * st.lr_ratio) * p.vol_mult**2
    # the readings a window needs: its close, and the instant it opens if that is still ahead
    saves = {c for c in want} | {c - WINDOW for c in want if c - WINDOW > start}
    kept: dict[float, tuple[np.ndarray, np.ndarray]] = {}

    pool = st.scale.z_pool
    if pool.size < 50:
        pool = rng.standard_normal(4096)
    pool = pool - float(np.mean(pool))      # the shocks' shape, not their recent drift: a run-up must not read as momentum
    pool = pool / max(float(np.std(pool)), 1e-9)
    wide = np.exp(p.vol_of_vol * rng.standard_normal(n) - 0.5 * p.vol_of_vol**2) if p.vol_of_vol > 0 else np.ones(n)
    drift = (p.drift_base + p.drift_trend * st.trend) / 24.0

    logp0 = float(np.log(st.price))
    logp = np.full(n, logp0)
    lag = np.zeros(n)
    lograte0 = float(np.log(max(st.rate(p.rate_tau_h), 1.0)))
    wander = np.zeros(n)
    fee = np.full(n, float(np.log(st.fee)))
    fee_to = float(np.log(st.fee_level))
    d = np.full(n, st.difficulty)
    height = np.full(n, float(st.height))
    retarget_at = np.full(n, float(st.next_retarget))
    epoch_start = np.full(n, st.epoch_start)
    usd = np.zeros(n)
    work = np.zeros(n)
    t_elastic = p.elastic_tau_h
    sqrt_day = p.hash_sigma_day / np.sqrt(24.0)
    fee_noise = p.fee_sigma_day / np.sqrt(24.0)

    for i, step in enumerate(dt):
        logp = logp + np.sqrt(var[i]) * wide * pool[rng.integers(0, pool.size, n)]
        lag += (logp - logp0 - lag) * min(step / t_elastic, 1.0)
        wander += sqrt_day * np.sqrt(step) * rng.standard_normal(n)
        fee += (fee_to - fee) * min(step / p.fee_tau_h, 1.0) + fee_noise * np.sqrt(step) * rng.standard_normal(n)
        lam = np.exp(lograte0 + drift * ahead[i] + wander + p.elasticity * lag) / d * 6.0 * step
        got = rng.poisson(np.minimum(lam, 400.0)).astype(float)
        value = (subsidy_btc(height) + np.exp(fee)) * np.exp(logp)
        usd += got * value
        to_go = retarget_at - height - 1.0
        crossing = p.retarget & (got >= to_go)
        if np.any(crossing):
            pre = np.where(crossing, np.maximum(to_go, 0.0), got)
            work += pre * d
            when = bounds[i] + (pre / np.maximum(got, 1.0)) * (bounds[i + 1] - bounds[i])
            # the rule measures to the last block of the epoch, one interval short of the first block of the next
            took = when - epoch_start - 600.0 * d / np.maximum(np.exp(lograte0 + drift * ahead[i] + wander + p.elasticity * lag), 1.0)
            d = np.where(crossing, d * np.clip(TARGET / np.maximum(took, 1.0), 1 / MAX_STEP, MAX_STEP), d)
            work += (got - pre) * d
            epoch_start = np.where(crossing, when, epoch_start)
            retarget_at = np.where(crossing, retarget_at + EPOCH, retarget_at)
        else:
            work += got * d
        height += got
        t = float(bounds[i + 1])
        if t in saves:
            kept[t] = (usd.copy(), work.copy())

    out: dict[float, np.ndarray] = {}
    noise = rng.standard_normal(n)
    for c in want:
        i_usd, i_work = kept[c]
        opens = c - WINDOW
        if opens > start:
            a_usd, a_work = kept[opens]
            top, bottom = i_usd - a_usd, i_work - a_work
        else:
            known_usd, known_work = st.blocks.trailing(opens) if p.known else (0.0, 0.0)
            top, bottom = i_usd + known_usd, i_work + known_work
        with np.errstate(divide="ignore", invalid="ignore"):
            hp = K * top / bottom
        hp = np.where(np.isfinite(hp) & (hp > 0), hp, st.hashprice)
        out[c] = hp * np.exp(p.jitter * noise)
    return out


def probabilities(samples: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """One probability per range of the ladder from simulated readings: 199 quantiles joined by straight lines with linear
    tails out to the most extreme path, so a few thousand paths leave no holes in a 1,000-range ladder. Sums to 1."""
    x = np.log(np.maximum(np.asarray(samples, dtype=float), 1e-9))
    levels = np.arange(1, 200) / 200.0
    q = np.maximum.accumulate(np.quantile(x, levels)) + np.arange(199) * 1e-12
    # a tail as wide again as the 0.5% to 5% stretch past the last quantile, or to the farthest path if that is further
    lo = min(float(x.min()), q[0] - (q[9] - q[0])) - 1e-9
    hi = max(float(x.max()), q[-1] + (q[-1] - q[-10])) + 1e-9
    q = np.concatenate([[lo], q, [hi]])
    lv = np.concatenate([[0.0], levels, [1.0]])
    at = np.interp(np.log(np.maximum(edges, 1e-9)), q, lv, left=0.0, right=1.0)
    at[0], at[-1] = 0.0, 1.0
    p = np.maximum(np.diff(at), PROB_FLOOR)
    return p / p.sum()


# ── live ────────────────────────────────────────────────────

BITVIEW = "https://bitview.space/api/series"
BLOCK_SERIES = ("coinbase_usd", "coinbase", "fees", "difficulty", "timestamp")
STALE_AFTER = 900.0             # a forecast this far behind Bitview's answers is not one to trade on
READ_EVERY = 30.0               # seconds between reads of the newest blocks: one lands every ten minutes or so
RUN_EVERY = 60.0                # seconds a simulation serves for, unless a new block or a price move replaces it sooner
PATHS = 6000
AHEAD_DAYS = 32                 # the closes one run reads: past the farthest the series lists


class Inputs:
    """What the hashprice bot reads, kept fresh: the recent blocks, Bitcoin's hourly bars and quote, and one simulation of
    every coming close. Bitview's weight limit is comfortably above these requests. `refresh` is called from the runner's
    event loop, `samples` from its drawing threads."""

    def __init__(self, params: Params | None = None) -> None:
        self.params = params or load_params()
        self.blocks: Blocks | None = None
        self.bars: pd.DataFrame | None = None
        self.price = 0.0
        self.read_at = 0.0
        self.bars_at = 0.0
        self._run: dict[float, np.ndarray] = {}
        self._run_key: tuple = ()
        self._run_at = 0.0
        self._lock = threading.Lock()

    async def _blocks(self, start: int) -> dict[str, np.ndarray] | None:
        from .data import http

        r = await http.client(BITVIEW).get(f"{BITVIEW}/bulk", params={"series": ",".join(BLOCK_SERIES), "index": "height",
                                                                      "start": start})
        r.raise_for_status()
        got = r.json()
        return {"first": int(got[0]["start"]), **{n: np.array(x["data"], dtype=float) for n, x in zip(BLOCK_SERIES, got, strict=True)}}

    async def _hours(self) -> pd.DataFrame:
        from .data import http

        r = await http.client(BITVIEW).get(f"{BITVIEW}/bulk", params={"series": "price_close,difficulty,timestamp", "index": "hour1",
                                                                      "start": -HISTORY_HOURS - 1})
        r.raise_for_status()
        close, diff, ts = (np.array(x["data"], dtype=float) for x in r.json())
        # the last slot is still forming: everything the clock reads is a complete hour
        return pd.DataFrame({"close": close[:-1], "difficulty": diff[:-1]},
                            index=pd.to_datetime(ts[:-1].astype("int64"), unit="s", utc=True)).ffill()

    async def refresh(self) -> None:
        """Bitview's newest blocks, and every quarter of an hour its hourly bars. What fails keeps its last answer; `ready`
        turns false once that has grown old."""
        from .zoo.data import spot_price

        now = time.time()
        try:
            if self.blocks is None:
                d = await self._blocks(-HISTORY_BLOCKS)
                self.blocks = Blocks(d["first"], np.maximum.accumulate(d["timestamp"]), d["coinbase_usd"], d["coinbase"], d["fees"],
                                     d["difficulty"])
                self.read_at = time.time()
            elif now - self.read_at >= READ_EVERY:
                d = await self._blocks(-12)         # a reorg rewrites the top few, so they are read again rather than appended
                self.blocks = merged(self.blocks, d)
                self.read_at = time.time()
            if self.bars is None or now - self.bars_at > 900:
                self.bars, self.bars_at = await self._hours(), now
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            pass
        quote = await spot_price("BTC")
        if quote and self.blocks is not None:
            block_price = float(self.blocks.usd[-1] / max(self.blocks.btc[-1], 1e-9))
            # a quote far from the chain's own price is a bad quote more often than it is news
            self.price = float(quote) if abs(quote / block_price - 1) < 0.05 else block_price

    @property
    def ready(self) -> bool:
        return self.blocks is not None and self.bars is not None and time.time() - self.read_at < STALE_AFTER

    def state(self, now: float | None = None) -> State:
        return state_at(self.blocks, self.bars, time.time() if now is None else now, price=self.price or None)

    def samples(self, now: float, close: float, n: int = PATHS) -> np.ndarray:
        """Simulated readings of the oracle at `close`. One run reads every coming close, so a round of the series costs one
        simulation: a new one when a block lands or the price has moved 0.2%, or after RUN_EVERY if nothing has."""
        with self._lock:
            key = (self.blocks.height, round(np.log(max(self.price, 1.0)) * 500))
            if key != self._run_key or now - self._run_at > RUN_EVERY or float(close) not in self._run:
                day = (now // DAY + 1) * DAY
                closes = {day + k * DAY for k in range(AHEAD_DAYS)} | {float(close)}
                self._run = simulate(self.state(now), sorted(closes), self.params, n=n, seed=self.blocks.height)
                self._run_key, self._run_at = key, now
            return self._run[float(close)]


def merged(have: Blocks, d: dict) -> Blocks:
    """The block history with the newest blocks laid over it, by height, and the oldest dropped."""
    keep = max(int(d["first"]) - have.first, 0)
    ts = np.maximum.accumulate(np.concatenate([have.ts[:keep], d["timestamp"]]))
    return Blocks(have.first, ts, np.concatenate([have.usd[:keep], d["coinbase_usd"]]), np.concatenate([have.btc[:keep], d["coinbase"]]),
                  np.concatenate([have.fees[:keep], d["fees"]]), np.concatenate([have.difficulty[:keep], d["difficulty"]])).tail(HISTORY_BLOCKS)
