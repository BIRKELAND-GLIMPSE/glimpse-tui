"""The bench the hashprice forecast is developed on: walk-forward backtests and a parameter search over real closes.

    glimpse-tui hashprice now           today's forecast of every open close against the market
    glimpse-tui hashprice backtest      the simulation against the plain baselines, close by close, on Bitview's history
    glimpse-tui hashprice fit [--save]  coordinate search on the training closes, scored on the closes it never saw

Every forecast is drawn from what was known at its origin: the blocks mined by then and the price bars closed by then. It
is scored against what the oracle read: `hash_price` on the `hour1` slot ending at a UTC midnight, the value Glimpse's
two settled markets paid out on to the digit. History comes from Bitview in pages (its weight limit is about 20,000 points
of an expensive series a request) and is kept in the cache directory, where later runs fetch only the new part.
"""
from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from . import hashprice as H

BITVIEW = "https://bitview.space/api/series"
PAGE = 15_000                   # points a request; difficulty weighs 16 a point against Bitview's limit of 320,000
FIRST_HEIGHT = 630_000          # the 2020 halving: everything since has the current kind of mining
FIRST_HOUR = 99_231             # position on `hour1` (hours since 2009-01-01) a little before it
BUCKETS = ((0, 24), (24, 48), (48, 72), (72, 120), (120, 168), (168, 240), (240, 336), (336, 576))
SPLIT = pd.Timestamp("2026-01-01", tz="UTC").timestamp()    # fit on closes before this, report on closes after it
ERA = pd.Timestamp("2024-05-01", tz="UTC").timestamp()      # after the last halving: the closes that resemble today's
LADDER = np.arange(0.0, 100.0 + 0.05, 0.10)                 # the market's ranges: $0 to $100 in 10 cent steps
FLOOR = 1e-4                    # the log score's floor: one catastrophe should cost a lot, not everything


# ── history ─────────────────────────────────────────────────

async def _pages(client, name: str, index: str, start: int, end: int) -> list:
    out: list = []
    for p in range(start, end, PAGE):
        r = await client.get(f"{BITVIEW}/{name}/{index}", params={"start": p, "end": min(p + PAGE, end)})
        r.raise_for_status()
        out += r.json()["data"]
    return out


async def _latest(client, name: str, index: str) -> int:
    r = await client.get(f"{BITVIEW}/{name}/{index}", params={"start": -1})
    r.raise_for_status()
    return int(r.json()["end"])


def _cache() -> Path:
    from .zoo.data import cache_dir

    return cache_dir() / "hashprice-history.npz"


@dataclass(frozen=True)
class History:
    blocks: H.Blocks
    bars: pd.DataFrame          # hourly, indexed by slot start: close (Bitview's price), difficulty, hp (the oracle's reading)


async def download() -> History:
    """Bitview's blocks and hourly series since the 2020 halving, from the cache plus whatever is new."""
    import httpx

    blk, hrs = {}, {}
    try:
        z = np.load(_cache())
        blk, hrs = {k[2:]: z[k] for k in z.files if k.startswith("b_")}, {k[2:]: z[k] for k in z.files if k.startswith("h_")}
    except (OSError, ValueError):
        pass
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "glimpse-tui"}) as c:
        b1, h1 = await _latest(c, "timestamp", "height"), await _latest(c, "timestamp", "hour1")
        b0 = FIRST_HEIGHT + len(blk.get("timestamp", ()))
        h0 = FIRST_HOUR + max(len(hrs.get("timestamp", ())) - 2, 0)     # the last slots were still forming: read them again
        for name in ("coinbase_usd", "coinbase", "fees", "difficulty", "timestamp"):
            got = np.array(await _pages(c, name, "height", b0, b1), dtype=float)
            blk[name] = np.concatenate([blk[name], got]) if name in blk else got
        keep = max(len(hrs.get("timestamp", ())) - 2, 0)
        for name in ("hash_price", "price_close", "difficulty", "timestamp"):
            vals = await _pages(c, name, "hour1", h0, h1)
            got = np.array([np.nan if v is None else v for v in vals], dtype=float)
            hrs[name] = np.concatenate([hrs[name][:keep], got]) if name in hrs else got
    np.savez_compressed(_cache(), **{f"b_{k}": v for k, v in blk.items()}, **{f"h_{k}": v for k, v in hrs.items()})
    return history(blk, hrs)


def history(blk: dict, hrs: dict) -> History:
    blocks = H.Blocks(FIRST_HEIGHT, np.maximum.accumulate(blk["timestamp"]), blk["coinbase_usd"], blk["coinbase"], blk["fees"],
                      blk["difficulty"])
    index = pd.to_datetime(hrs["timestamp"].astype("int64"), unit="s", utc=True)
    bars = pd.DataFrame({"open": hrs["price_close"], "high": hrs["price_close"], "low": hrs["price_close"], "close": hrs["price_close"],
                         "volume": 0.0, "difficulty": hrs["difficulty"], "hp": hrs["hash_price"]}, index=index)
    return History(blocks, bars)


# ── the forecasters ─────────────────────────────────────────

Case = tuple                    # (state, the closes to read, their settled values, origin, the oracle's past hourly readings)


def origins(h: History, since: float, until: float, every_h: float = 31.0) -> list[float]:
    """Forecast instants: every 31 hours, so they walk around the clock and through the days of the week."""
    last = float(h.bars.index[-1].timestamp())
    return list(np.arange(since, min(until, last), every_h * H.HOUR))


def case_at(h: History, now: float, horizon_days: int = 24) -> Case | None:
    """Everything known at `now`, and what the oracle later read at each UTC midnight of the next `horizon_days`."""
    t = h.bars.index.as_unit("s").asi8
    i = int(np.searchsorted(t, now - H.HOUR, side="right"))         # slots that have finished by now
    if i < H.MIN_HOURS:
        return None
    bars = h.bars.iloc[max(i - H.HISTORY_HOURS, 0): i]
    day = (now // H.DAY + 1) * H.DAY
    closes, settled = [], []
    for k in range(horizon_days):
        c = day + k * H.DAY
        j = int(np.searchsorted(t, c - H.HOUR))
        if j < len(t) and t[j] == c - H.HOUR and np.isfinite(h.bars["hp"].iat[j]) and c <= t[-1]:
            closes.append(c)
            settled.append(float(h.bars["hp"].iat[j]))
    if not closes:
        return None
    try:
        st = H.state_at(h.blocks, bars, now)
    except ValueError:
        return None
    return st, np.array(closes), np.array(settled), now, bars["hp"].to_numpy(dtype=float)


def walk_log_sds(past: np.ndarray, hours: np.ndarray, kind: str) -> np.ndarray:
    """A driftless log-normal walk on the hashprice itself: the standard deviation of the log move to each close."""
    v = np.log(past[np.isfinite(past)])
    if kind == "hourly":                # the zoo's clock: an EWMA of hourly moves, scaled by the root of time
        r = np.diff(v)
        s2 = float(np.mean(r[:100] ** 2))
        for x in r[100:]:
            s2 = 0.94 * s2 + 0.06 * x * x
        return np.sqrt(s2 * hours)
    if kind == "96h":                   # the site's clock: the last 45 days of 96-hour moves, scaled by the root of time
        w = v[-45 * 24:]
        return np.sqrt(np.var(w[96:] - w[:-96]) / 96.0 * hours)
    # every horizon's own measured spread over the history at hand: the best a walk can be sized
    return np.array([np.std(v[int(max(x, 1)):] - v[: -int(max(x, 1))]) if x < len(v) / 2 else np.nan for x in hours])


def score_samples(samples: np.ndarray, y: float) -> tuple[float, float, float, float]:
    """(CRPS as a share of the outcome, log probability of the range it landed in, inside the 80% band?, median error)."""
    x = np.sort(samples)
    n = x.size
    crps = float(np.mean(np.abs(x - y)) - np.sum((2 * np.arange(1, n + 1) - n - 1) * x) / (n * n))
    p = H.probabilities(x, LADDER)
    j = int(np.clip(np.searchsorted(LADDER, y, side="right") - 1, 0, len(p) - 1))
    q10, q50, q90 = np.quantile(x, (0.1, 0.5, 0.9))
    return crps / y, float(np.log(max(p[j], FLOOR))), float(q10 <= y <= q90), float(np.log(q50 / y))


def score_walk(sd: float, y: float, base: float) -> tuple[float, float, float, float]:
    """`score_samples` for a log-normal walk from `base`, on a fixed grid of its quantiles."""
    x = base * np.exp(sd * stats.norm.ppf((np.arange(800) + 0.5) / 800))
    return score_samples(x, y)


def _bucket(hours: float) -> tuple[int, int] | None:
    return next((b for b in BUCKETS if b[0] <= hours < b[1]), None)


def evaluate(cases: list[Case], p: H.Params, paths: int = 1500) -> dict:
    """{horizon bucket: [(which forecast, scores)]} for the simulation under `p`. Same seeds every time, so two sets of
    parameters are compared on the same luck."""
    out: dict[tuple[int, int], list] = {b: [] for b in BUCKETS}
    for k, (st, closes, settled, now, _) in enumerate(cases):
        got = H.simulate(st, closes, p, n=paths, seed=k)
        for c, y in zip(closes, settled, strict=True):
            if b := _bucket((c - now) / H.HOUR):
                out[b].append((k, score_samples(got[c], y)))
    return out


def evaluate_walk(cases: list[Case], kind: str) -> dict:
    out: dict[tuple[int, int], list] = {b: [] for b in BUCKETS}
    for k, (_, closes, settled, now, past) in enumerate(cases):
        hours = (closes - now) / H.HOUR
        sds = walk_log_sds(past, hours, kind)
        base = float(past[np.isfinite(past)][-1])
        for x, sd, y in zip(hours, sds, settled, strict=True):
            b = _bucket(x)
            if b and np.isfinite(sd) and sd > 0:
                out[b].append((k, score_walk(sd, y, base)))
    return out


def means(rows: dict) -> dict:
    return {b: np.mean([s for _, s in v], axis=0) for b, v in rows.items() if v}


def edge(a: dict, b: dict, block: int = 14, draws: int = 2000) -> dict:
    """{bucket: (mean log score of `a` over `b`, its 5th and 95th percentiles)}. Forecasts overlap, so the interval comes from
    resampling runs of `block` consecutive forecasts (about 18 days), not single closes. An interval clear of zero is an edge
    the luck of one period did not make."""
    rng = np.random.default_rng(1)
    out = {}
    for x in BUCKETS:
        other = {k: s[1] for k, s in b.get(x, [])}
        diff: dict[int, list[float]] = {}
        for k, s in a.get(x, []):
            if k in other:
                diff.setdefault(k, []).append(s[1] - other[k])
        if len(diff) < block:
            continue
        per = np.array([np.mean(diff[k]) for k in sorted(diff)])
        n = max(len(per) // block, 1)
        starts = rng.integers(0, len(per) - block + 1, (draws, n))
        boot = np.array([per[np.concatenate([np.arange(i, i + block) for i in row])].mean() for row in starts])
        out[x] = (float(per.mean()), float(np.quantile(boot, 0.05)), float(np.quantile(boot, 0.95)))
    return out


def label(b: tuple[int, int]) -> str:
    return f"{b[0]}-{b[1]}h" if b[1] <= 48 else f"{b[0] // 24}-{b[1] // 24}d"


def table(results: dict[str, dict]) -> list[str]:
    """One block of rows per metric, a column per forecaster. `results` holds `means`."""
    names = list(results)
    lines = []
    for title, k, cell in (("CRPS, % of the hashprice (lower is better)", 0, "{:9.3f}"),
                           ("log score on the market's 10c ranges (higher is better)", 1, "{:9.3f}"),
                           ("how often the outcome fell inside the 80% band", 2, "{:9.0%}")):
        lines.append(f"\n{title}")
        lines.append(f"{'':>9} " + " ".join(f"{n:>9}" for n in names))
        for b in BUCKETS:
            cells = []
            for n in names:
                r = results[n].get(b)
                cells.append(" " * 9 if r is None else cell.format(r[k] * (100 if k == 0 else 1)))
            lines.append(f"{label(b):>9} " + " ".join(cells))
    return lines


# ── the search ──────────────────────────────────────────────

GRID = {
    "vol_mult": (0.92, 1.0, 1.08, 1.16, 1.25, 1.35),
    "vol_tau_h": (12.0, 24.0, 48.0, 72.0, 120.0, 240.0, 480.0),
    "vol_of_vol": (0.0, 0.1, 0.2, 0.3, 0.4),
    "rate_tau_h": (72.0, 96.0, 144.0, 240.0, 336.0),
    "drift_base": (-0.001, 0.0, 0.0005, 0.001, 0.002),
    "drift_trend": (0.0, 0.15, 0.25, 0.4, 0.6, 0.8),
    "hash_sigma_day": (0.004, 0.008, 0.012, 0.016, 0.024),
    "elasticity": (0.0, 0.1, 0.2, 0.3, 0.45, 0.6),
    "elastic_tau_h": (72.0, 168.0, 336.0),
    "jitter": (0.0002, 0.0005, 0.0008, 0.0015, 0.003, 0.005),
}


_WORK: list = []


def _init(cases: list) -> None:
    _WORK[:] = cases


def _score(args: tuple) -> float:
    """The search's loss: minus the mean log score on the market's own ranges, each horizon bucket counting the same. A Kelly
    bettor's growth is that score, so this is the number the bot's sizing cares about. CRPS barely sees the tails it buys."""
    p, paths = args
    return -float(np.mean([r[1] for r in means(evaluate(_WORK, p, paths)).values()]))


def fit(cases: list[Case], start: H.Params, sweeps: int = 2, paths: int = 800, say=print) -> H.Params:
    """Coordinate descent on the grid above: one parameter at a time, the best value kept. Workers share the cases."""
    with ProcessPoolExecutor(max_workers=max((os.cpu_count() or 2) - 1, 1), initializer=_init, initargs=(cases,)) as ex:
        best = start
        loss = next(ex.map(_score, [(best, paths)]))
        say(f"start  loss {loss:.4f}")
        for sweep in range(sweeps):
            changed = False
            for name, values in GRID.items():
                trial = [best.overlay(**{name: v}) for v in values]
                scores = list(ex.map(_score, [(x, paths) for x in trial]))
                i = int(np.argmin(scores))
                if scores[i] < loss - 1e-4:
                    changed = changed or getattr(best, name) != values[i]
                    best, loss = trial[i], scores[i]
                say(f"sweep {sweep + 1}  {name:<15} " + " ".join(f"{v:g}:{s:.3f}" for v, s in zip(values, scores, strict=True))
                    + f"  -> {getattr(best, name):g}  loss {loss:.4f}")
            if not changed:
                break
    return best


# ── the command ─────────────────────────────────────────────

def cases_between(h: History, since: float, until: float, every_h: float = 31.0) -> list[Case]:
    return [c for c in (case_at(h, t) for t in origins(h, since, until, every_h)) if c is not None]


def backtest(h: History, p: H.Params, paths: int = 1500, say=print) -> dict[str, dict]:
    began = time.time()
    test = cases_between(h, SPLIT, float("inf"))
    say(f"{len(test)} forecasts since {pd.Timestamp(SPLIT, unit='s'):%d %b %Y}, every 31 hours, up to 24 closes each  "
        f"({time.time() - began:.0f}s to set up)")
    rows = {"sim": evaluate(test, p, paths),
            "no-ret": evaluate(test, p.overlay(retarget=False), paths),
            "no-known": evaluate(test, p.overlay(known=False), paths),
            "walk-h": evaluate_walk(test, "hourly"),
            "walk-96": evaluate_walk(test, "96h"),
            "walk-emp": evaluate_walk(test, "empirical")}
    for line in table({n: means(r) for n, r in rows.items()}):
        say(line)
    say("\nthe simulation's log score over the best-sized walk's, with a 90% interval from resampling 18-day runs of forecasts")
    say("(the log growth per close a full Kelly bettor would make if the market priced that walk; it does not, so this scores the")
    say("forecast, not the bot's profit. Clear of zero is an edge one lucky stretch did not make.)")
    for b, (m, lo, hi) in edge(rows["sim"], rows["walk-emp"]).items():
        say(f"{label(b):>9} {m:+7.3f}   [{lo:+.3f}, {hi:+.3f}]" + ("" if lo > 0 else "   not clear of zero"))
    say("\nsim: the simulation  no-ret: difficulty frozen  no-known: ignores the blocks already in a window  walk-h: a walk on the "
        "hourly clock  walk-96: the site's 96-hour clock  walk-emp: a walk sized to each horizon's measured spread")
    return rows


async def today(say=print) -> int:
    """Today's forecast of every open close against the market: how far apart they are, and the bot's best edge on each."""
    from datetime import UTC, datetime

    from . import fmt
    from .api import HASHPRICE, Book, Glimpse
    from .pricing import FEE, PAYOUT_SATS, signal

    src = H.Inputs()
    await src.refresh()
    if not src.ready:
        say("Bitview has not answered: no forecast.")
        return 1
    now = time.time()
    st = src.state(now)
    r = src.params.rate_tau_h
    at, change = st.retarget_estimate(r)
    subsidy = float(H.subsidy_btc(st.height))
    say(f"oracle now ${st.hashprice:.2f}   at today's price and difficulty ${H.K * (subsidy + st.fee) * st.price / st.difficulty:.2f}")
    say(f"Bitcoin ${st.price:,.0f}   subsidy {subsidy:g} BTC   fees {st.fee:.3f} BTC a block   difficulty {st.difficulty / 1e12:,.1f}T   "
        f"hash rate {st.rate(r) / st.difficulty:.1%} of schedule")
    say(f"next retarget #{st.next_retarget:,} in {st.blocks_left:,} blocks, about {datetime.fromtimestamp(at, UTC):%d %b %H:%M} UTC, "
        f"estimate {change:+.1%}\n")
    api = Glimpse()
    try:
        batch = next((b for b in await api.batches() if b.asset == HASHPRICE), None)
        if batch is None:
            say("Glimpse lists no hashprice series.")
            return 1
        rows = await api.all_markets(batch.batch_id)
    finally:
        await api.close()
    say(f"{'close':<8}{'model':>8}{'80% band':>15}{'market':>9}{'80% band':>15}{'gap':>8}{'best edge':>11}  range it would buy first")
    for row in rows:
        book = Book.from_row(row)
        x = src.samples(now, float(row.end_time_utc))
        lo, mid, hi = np.quantile(x, (0.1, 0.5, 0.9))
        probs = H.probabilities(x, np.array([book.bins[0][0]] + [b for _, b in book.bins]))
        c = np.cumsum(signal(book.probs))
        mids = [b for _, b in book.bins]
        q10, q50, q90 = (mids[int(np.searchsorted(c, v))] for v in (0.1, 0.5, 0.9))
        ev = probs * PAYOUT_SATS * (1 - FEE) / (np.array(book.prices) * (1 + FEE)) - 1
        i = int(np.argmax(ev))
        say(f"{datetime.fromtimestamp(row.end_time_utc, UTC):%d %b}  {mid:>8.2f}{fmt.span(lo, hi):>15}{q50:>9.2f}{fmt.span(q10, q90):>15}"
            f"{mid / q50 - 1:>+8.1%}{ev[i]:>+11.0%}  {fmt.span(*book.bins[i])} at {book.prices[i]:.2f} sats, worth {probs[i] * PAYOUT_SATS:.1f}")
    return 0


async def _main(cmd: str, save: bool, paths: int, say=print) -> int:
    if cmd == "now":
        return await today(say)
    say("reading Bitview's history...")
    h = await download()
    p = H.load_params()
    if cmd == "backtest":
        backtest(h, p, paths, say)
        return 0
    train = cases_between(h, ERA, SPLIT, 47.0)
    say(f"fitting on {len(train)} forecasts, {pd.Timestamp(ERA, unit='s'):%d %b %Y} to {pd.Timestamp(SPLIT, unit='s'):%d %b %Y}")
    best = fit(train, p, say=say)
    say("chosen: " + ", ".join(f"{k}={getattr(best, k):g}" for k in GRID))
    if save:
        say(f"saved to {H.save_params(best)}")
    backtest(h, best, paths, say)
    return 0


def main(cmd: str, save: bool = False, paths: int = 1500) -> int:
    return asyncio.run(_main(cmd, save, paths))
