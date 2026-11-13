"""The bench the retarget forecast is developed on: walk-forward backtests, a parameter search, and what leaning against miners does.

    glimpse-tui difficulty now              the coming retargets against both markets
    glimpse-tui difficulty backtest         the simulation against the plain baselines, on Bitview's history
    glimpse-tui difficulty fit [--save]     coordinate search on earlier retargets, scored on the ones it never saw
    glimpse-tui difficulty hedge            real retargets with an invented flow of miners buying cover: the bot's profit and their price

Every forecast is drawn from what was known at its origin: the blocks mined by then. It is scored on what the chain then set, on
each market's own ladder. The ladders are 1T on the difficulty and 1% on the adjustment; since the difficulty was far lower in the
past, its log score is taken on ranges of the width 1T has today.
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np

from . import difficulty as H

BITVIEW = H.BITVIEW
PAGE = 30_000  # block times a request, against Bitview's weight limit
FIRST_HEIGHT = 322_560  # October 2014: well into the ASIC era, and far enough back for two years of trend before any origin used
FIT_FROM, SPLIT = 330, 440  # epochs: fit on origins from January 2021 to March 2025, report on every epoch after it
ORIGIN_FROM = 312  # the 2020 halving: the first origin a baseline's history may read
TODAY_T = 148.0  # the terahash the difficulty ladder's 1T width is measured against
D_WIDTH, A_WIDTH = float(np.log(1 + 1 / TODAY_T)), 0.01
FLOOR = 1e-4  # the log score's floor: one catastrophe should cost a lot, not everything
NEAR = ((0, 36), (36, 144), (144, 432), (432, 1008), (1008, 2017))  # blocks to the next retarget
BUCKETS = tuple(f"next, {a}-{b - 1 if b > 2016 else b} blocks" for a, b in NEAR) + ("2nd", "3rd", "4th-6th", "7th-9th", "10th-12th")
WALK = 53  # retargets the site's baselines read: two years


def bucket(k: int, left: int) -> str:
    """Which row of the table a forecast belongs to: the next retarget by how near it is, later ones by how many away."""
    if k == 0:
        return next(BUCKETS[i] for i, (a, b) in enumerate(NEAR) if a <= left < b)
    n = k + 1
    return "2nd" if n == 2 else "3rd" if n == 3 else "4th-6th" if n <= 6 else "7th-9th" if n <= 9 else "10th-12th"


# ── history ─────────────────────────────────────────────────


def _cache():
    from .zoo.data import cache_dir

    return cache_dir() / "difficulty-history.npz"


@dataclass(frozen=True)
class History:
    first: int
    ts: np.ndarray  # raw block times, from height `first`
    mono: np.ndarray  # their running maximum
    epochs: np.ndarray  # the difficulty of every finished or running epoch

    @property
    def tip(self) -> int:
        return self.first + len(self.ts) - 1

    def state(self, height: int, tau_h: float) -> H.State:
        """What was known when `height` was mined."""
        idx = np.arange(max(height - H.HISTORY_BLOCKS + 1, self.first), height + 1)
        now = float(self.mono[height - self.first])
        epoch = height // H.EPOCH
        back = max(epoch - H.TREND_EPOCHS, 0)
        d = float(self.epochs[epoch])
        return H.State(
            now,
            height,
            d,
            float(self.ts[epoch * H.EPOCH - self.first]),
            H.rate_from(self.mono[idx - self.first], self.epochs[idx // H.EPOCH], now, tau_h),
            float(np.log(d / self.epochs[back]) / ((epoch - back) * 14.0)),
        )


async def download(say=print) -> History:
    """Bitview's block times since 2014 and every retarget's difficulty, from the cache plus whatever is new."""
    import httpx

    have = np.zeros(0)
    try:
        have = np.load(_cache())["ts"]
    except (OSError, ValueError, KeyError):
        pass
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": "glimpse-tui"}) as c:
        r = await c.get(f"{BITVIEW}/timestamp/height", params={"start": -1})
        r.raise_for_status()
        tip = int(r.json()["end"])
        start = FIRST_HEIGHT + len(have)
        got: list = []
        for p in range(start, tip, PAGE):
            r = await c.get(f"{BITVIEW}/timestamp/height", params={"start": p, "end": min(p + PAGE, tip)})
            r.raise_for_status()
            got += r.json()["data"]
        r = await c.get(f"{BITVIEW}/difficulty/difficulty_epoch", params={"start": 0})
        r.raise_for_status()
        epochs = np.array(r.json()["data"], dtype=float)
    ts = np.concatenate([have, np.array(got, dtype=float)])
    np.savez_compressed(_cache(), ts=ts)
    return History(FIRST_HEIGHT, ts, np.maximum.accumulate(ts), epochs)


# ── scoring ─────────────────────────────────────────────────


def score(x: np.ndarray, y: float, width: float) -> tuple[float, float, float, float]:
    """(CRPS in log points, log probability of the range it landed in, inside the 80% band?, above the median?), for samples of
    the log of the value. The ranges are `width` wide on the log, aligned to multiples of it."""
    s = np.sort(x)
    n = s.size
    crps = float(np.mean(np.abs(s - y)) - np.sum((2 * np.arange(1, n + 1) - n - 1) * s) / (n * n))
    levels = np.arange(1, 200) / 200.0
    q = np.maximum.accumulate(np.quantile(s, levels)) + np.arange(199) * 1e-12
    lo = min(float(s[0]), q[0] - (q[9] - q[0])) - 1e-9
    hi = max(float(s[-1]), q[-1] + (q[-1] - q[-10])) + 1e-9
    qq, lv = np.concatenate([[lo], q, [hi]]), np.concatenate([[0.0], levels, [1.0]])
    a = float(np.floor(y / width) * width)
    p = float(np.interp(a + width, qq, lv, left=0.0, right=1.0) - np.interp(a, qq, lv, left=0.0, right=1.0))
    q10, q50, q90 = np.quantile(s, (0.1, 0.5, 0.9))
    return crps, float(np.log(max(p, FLOOR))), float(q10 <= y <= q90), float(y > q50)


NORMAL = None


def _normal() -> np.ndarray:
    global NORMAL
    if NORMAL is None:
        from scipy import stats

        NORMAL = stats.norm.ppf((np.arange(800) + 0.5) / 800)
    return NORMAL


Row = tuple  # (origin's epoch, bucket, 4 scores on the difficulty, 4 on the adjustment)


def origins(a: int, b: int, every: int, tip: int) -> list[int]:
    """Heights to forecast from: every `every` blocks through epochs a to b, none in the epoch's first few blocks."""
    return [h for h in range(a * H.EPOCH + 36, b * H.EPOCH, every) if h <= tip]


_HIST: list = []


def _init(h: History) -> None:
    _HIST[:] = [h]


def _sim_rows(args: tuple) -> list[Row]:
    height, p, paths, seed = args
    h = _HIST[0]
    e = height // H.EPOCH
    settled = len(h.epochs) - 1 - e
    k_max = min(H.MARKETS, settled)
    if k_max < 1:
        return []
    run = H.simulate(h.state(height, p.rate_tau_h), p, epochs=k_max, n=paths, seed=seed)
    out = []
    for k in range(k_max):
        at = (e + k + 1) * H.EPOCH
        out.append(
            (
                e,
                bucket(k, at - height),
                *score(np.log(run.difficulty[k]), float(np.log(h.epochs[e + k + 1])), D_WIDTH),
                *score(np.log(run.adjustment_at(at)), float(np.log(h.epochs[e + k + 1] / h.epochs[e + k])), A_WIDTH),
            )
        )
    return out


def evaluate(pool, heights: list[int], p: H.Params, paths: int) -> list[Row]:
    """The simulation under `p`, on these origins. Same seeds every time, so two sets of parameters meet the same luck."""
    args = [(x, p, paths, i) for i, x in enumerate(heights)]
    return [r for rows in pool.map(_sim_rows, args, chunksize=8) for r in rows]


def pace_change(h: History, height: int) -> float:
    """The log change the running epoch's own pace implies: mempool.space's estimate, and the site's."""
    e = height // H.EPOCH
    start = e * H.EPOCH
    took = max(float(h.ts[height - h.first] - h.ts[start - h.first]), 600.0) * (H.EPOCH - 1) / max(height - start, 1)
    return float(np.log(H.TARGET / took))


def site_rows(h: History, heights: list[int]) -> list[Row]:
    """The site's indicative markets (lib/difficulty.ts): a log-normal around the pace estimate, as wide as the k-epoch moves of
    the last two years. What a careful reader of mempool.space would quote."""
    z = _normal()
    out = []
    for height in heights:
        e = height // H.EPOCH
        k_max = min(H.MARKETS, len(h.epochs) - 1 - e)
        logs = np.log(h.epochs[max(e - WALK + 1, 0) : e + 1])
        steps = logs[1:] - logs[:-1]
        mu = float(np.log(h.epochs[e]) + pace_change(h, height))
        left = (e + 1) * H.EPOCH - height
        s0 = max(0.004, float(np.std(steps, ddof=1)) * np.sqrt(left / H.EPOCH))
        for k in range(k_max):
            at = (e + k + 1) * H.EPOCH
            sp = float(np.std(logs[k:] - logs[:-k], ddof=1)) if k else 0.0
            sd = float(np.sqrt(s0 * s0 + sp * sp))
            a_sd = s0 if k == 0 else float(np.std(steps, ddof=1))
            a_mu = pace_change(h, height) if k == 0 else float(np.mean(steps))
            out.append(
                (
                    e,
                    bucket(k, at - height),
                    *score(mu + sd * z, float(np.log(h.epochs[e + k + 1])), D_WIDTH),
                    *score(a_mu + a_sd * z, float(np.log(h.epochs[e + k + 1] / h.epochs[e + k])), A_WIDTH),
                )
            )
    return out


def resid_rows(h: History, heights: list[int], every: int, p: H.Params) -> list[Row]:
    """The strongest plain baseline: the simulation's own point estimate, with no simulating, and around it the errors that point
    estimate actually made over the last two years, by the same distance from the retarget. Walk-forward: an error joins the
    pool only once its retarget has passed."""
    past: list[tuple[int, str, float, float]] = []  # (the epoch its answer was known, bucket, error on D, error on A)

    def point(height: int) -> list[tuple[float, float]]:
        st = h.state(height, p.rate_tau_h)
        _, change = st.retarget_estimate()
        g = p.growth_trend * st.trend * 14.0
        return [(float(np.log(st.difficulty) + np.log1p(change) + k * g), float(np.log1p(change)) if k == 0 else g) for k in range(H.MARKETS)]

    start = min(heights) if heights else 0
    for height in range(ORIGIN_FROM * H.EPOCH + 36, start, every):
        e = height // H.EPOCH
        for k, (d, a) in enumerate(point(height)):
            if e + k + 1 < len(h.epochs):
                past.append(
                    (
                        e + k + 1,
                        bucket(k, (e + k + 1) * H.EPOCH - height),
                        float(np.log(h.epochs[e + k + 1])) - d,
                        float(np.log(h.epochs[e + k + 1] / h.epochs[e + k])) - a,
                    )
                )
    out = []
    for height in heights:
        e = height // H.EPOCH
        k_max = min(H.MARKETS, len(h.epochs) - 1 - e)
        points = point(height)
        for k in range(k_max):
            b = bucket(k, (e + k + 1) * H.EPOCH - height)
            pool = [(x[2], x[3]) for x in past if x[1] == b and e - 2 * 26 * 2 < x[0] <= e]
            if len(pool) < 40:
                continue
            d_err, a_err = np.array(pool).T
            out.append(
                (
                    e,
                    b,
                    *score(points[k][0] + d_err, float(np.log(h.epochs[e + k + 1])), D_WIDTH),
                    *score(points[k][1] + a_err, float(np.log(h.epochs[e + k + 1] / h.epochs[e + k])), A_WIDTH),
                )
            )
        for k, (d, a) in enumerate(points):
            if e + k + 1 < len(h.epochs):
                past.append(
                    (
                        e + k + 1,
                        bucket(k, (e + k + 1) * H.EPOCH - height),
                        float(np.log(h.epochs[e + k + 1])) - d,
                        float(np.log(h.epochs[e + k + 1] / h.epochs[e + k])) - a,
                    )
                )
    return out


def means(rows: list[Row]) -> dict[str, np.ndarray]:
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r[1], []).append(r[2:])
    return {b: np.mean(v, axis=0) for b, v in by.items()}


def edge(a: list[Row], b: list[Row], col: int, draws: int = 2000) -> dict[str, tuple[float, float, float]]:
    """{bucket: (mean log score of `a` over `b`, its 5th and 95th percentiles)}, resampling whole epochs of origins, since one
    retarget's forecasts are one outcome seen many times. An interval clear of zero is an edge no one lucky stretch made."""
    rng = np.random.default_rng(1)

    def by_epoch(rows: list[Row]) -> dict[tuple[str, int], float]:
        got: dict[tuple[str, int], list[float]] = {}
        for r in rows:
            got.setdefault((r[1], r[0]), []).append(r[col])
        return {k: float(np.mean(v)) for k, v in got.items()}

    x, y = by_epoch(a), by_epoch(b)
    out = {}
    for name in BUCKETS:
        diff = np.array([v - y[k] for k, v in x.items() if k[0] == name and k in y])
        if len(diff) < 8:
            continue
        boot = diff[rng.integers(0, len(diff), (draws, len(diff)))].mean(axis=1)
        out[name] = (float(diff.mean()), float(np.quantile(boot, 0.05)), float(np.quantile(boot, 0.95)))
    return out


METRICS = (
    ("the difficulty: CRPS, % (lower is better)", 0, 100, "{:8.2f}"),
    ("the difficulty: log score on 1T ranges (higher is better)", 1, 1, "{:8.3f}"),
    ("the difficulty: how often inside the 80% band", 2, 100, "{:7.0f}%"),
    ("the difficulty: how often above the median (50% is unbiased)", 3, 100, "{:7.0f}%"),
    ("the adjustment: CRPS, percentage points (lower is better)", 4, 100, "{:8.2f}"),
    ("the adjustment: log score on 1% ranges (higher is better)", 5, 1, "{:8.3f}"),
    ("the adjustment: how often inside the 80% band", 6, 100, "{:7.0f}%"),
    ("the adjustment: how often above the median (50% is unbiased)", 7, 100, "{:7.0f}%"),
)


def table(results: dict[str, dict[str, np.ndarray]]) -> list[str]:
    lines = []
    for title, k, scale, cell in METRICS:
        lines += [f"\n{title}", f"{'':>22} " + " ".join(f"{n:>8}" for n in results)]
        for b in BUCKETS:
            cells = [" " * 8 if b not in results[n] else cell.format(results[n][b][k] * scale) for n in results]
            lines.append(f"{b:>22} " + " ".join(cells))
    return lines


# ── the search ──────────────────────────────────────────────

GRID = {
    "rate_tau_h": (96.0, 120.0, 168.0, 240.0, 336.0),
    "rate_se": (0.0, 0.01, 0.02, 0.03, 0.04),
    "growth_trend": (0.25, 0.5, 0.75, 1.0, 1.25),
    "growth_sd": (0.0, 0.0003, 0.0006, 0.001),
    "sigma_day": (0.006, 0.008, 0.01, 0.012, 0.015, 0.02),
    "vol_of_vol": (0.0, 0.2, 0.3, 0.45, 0.6),
    "tail_df": (3.0, 4.0, 6.0, 12.0, 50.0),
}


def loss(rows: list[Row]) -> float:
    """Minus the mean log score over both ladders, each row of the table counting the same. A Kelly bettor's growth is that score,
    so it is the number the bots' sizing cares about."""
    m = means(rows)
    return -float(np.mean([(v[1] + v[5]) / 2 for v in m.values()]))


def fit(pool, heights: list[int], start: H.Params, sweeps: int = 2, paths: int = 800, say=print) -> H.Params:
    best, score_now = start, loss(evaluate(pool, heights, start, paths))
    say(f"start  loss {score_now:.4f}")
    for sweep in range(sweeps):
        changed = False
        for name, values in GRID.items():
            scores = [loss(evaluate(pool, heights, best.overlay(**{name: v}), paths)) for v in values]
            i = int(np.argmin(scores))
            if scores[i] < score_now - 2e-3:  # below this the search is reading its own noise
                changed = changed or getattr(best, name) != values[i]
                best, score_now = best.overlay(**{name: values[i]}), scores[i]
            say(
                f"sweep {sweep + 1}  {name:<14} "
                + " ".join(f"{v:g}:{s:.3f}" for v, s in zip(values, scores, strict=True))
                + f"  -> {getattr(best, name):g}  loss {score_now:.4f}"
            )
        if not changed:
            break
    return best


# ── the commands ────────────────────────────────────────────


def backtest(pool, h: History, p: H.Params, paths: int = 1500, say=print) -> dict[str, list[Row]]:
    every = 101
    heights = origins(SPLIT, len(h.epochs) - 1, every, h.tip)
    say(f"{len(heights)} forecasts, every {every} blocks, on the {len(h.epochs) - 1 - SPLIT} retargets since the fit stopped")
    rows = {"sim": evaluate(pool, heights, p, paths), "points": resid_rows(h, heights, every, p), "site": site_rows(h, heights)}
    for line in table({n: means(r) for n, r in rows.items()}):
        say(line)
    for title, col in (("the difficulty", 3), ("the adjustment", 7)):
        say(f"\nthe simulation's log score on {title} over the site's, with a 90% interval from resampling whole epochs")
        for b, (m, lo, hi) in edge(rows["sim"], rows["site"], col).items():
            say(f"{b:>22} {m:+7.3f}   [{lo:+.3f}, {hi:+.3f}]" + ("" if lo > 0 else "   not clear of zero"))
    say(
        "\nsim: the simulation  points: its point estimate with the errors that estimate made over the last two years  site: the site's "
        "indicative log-normal around the pace estimate"
    )
    return rows


async def today(say=print) -> int:
    """The coming retargets against both markets."""
    from datetime import UTC, datetime

    from .api import ADJUSTMENT, DIFFICULTY, Book, Glimpse
    from .pricing import FEE, PAYOUT_SATS, quantile, signal

    src = H.Inputs()
    await src.refresh()
    if not src.ready:
        say("Bitview has not answered: no forecast.")
        return 1
    now = time.time()
    st = src.state(now)
    run = src.run(now)
    at, change = st.retarget_estimate()
    say(
        f"difficulty {st.difficulty / 1e12:,.2f}T   tip #{st.height:,}   hash rate {st.pace:.1%} of schedule   "
        f"next retarget #{st.next_retarget:,} "
        f"in {st.blocks_left:,} blocks, about {datetime.fromtimestamp(at, UTC):%d %b %H:%M} UTC, estimate {change:+.2%}"
    )
    api = Glimpse()
    try:
        every = await api.batches()
        for asset in (DIFFICULTY, ADJUSTMENT):
            batch = next((b for b in every if b.asset == asset), None)
            if batch is None:
                say(f"Glimpse lists no {asset} series.")
                continue
            rows = await api.all_markets(batch.batch_id)
            unit = "T" if asset == DIFFICULTY else "%"
            say(
                f"\n{asset}, in {unit}\n{'retarget':<10}{'about':<9}{'model':>8}{'80% band':>16}{'market':>9}{'80% band':>16}"
                f"{'up model':>10}{'market':>8}{'best edge':>11}  range"
            )
            for row in rows:
                book = Book.from_row(row)
                edges = np.array([book.bins[0][0]] + [b for _, b in book.bins])
                if asset == DIFFICULTY:
                    x, unchanged = run.difficulty_at(row.close_block) / 1e12, st.difficulty / 1e12
                    probs = H.probabilities(x, edges)
                else:
                    x, unchanged = (run.adjustment_at(row.close_block) - 1) * 100, 0.0
                    probs = H.probabilities(1 + x / 100, 1 + edges / 100)
                lo, mid, hi = np.quantile(x, (0.1, 0.5, 0.9))
                sig = signal(book.probs)
                q10, q50, q90 = (quantile(book.bins, sig, v) for v in (0.1, 0.5, 0.9))
                up_model, up_market = float((x > unchanged).mean()), sum(p for p, (a, _) in zip(sig, book.bins, strict=True) if a >= unchanged)
                ev = probs * PAYOUT_SATS * (1 - FEE) / (np.array(book.prices) * (1 + FEE)) - 1
                i = int(np.argmax(ev))
                say(
                    f"#{row.close_block:<9,}{datetime.fromtimestamp(float(np.median(run.arrival_at(row.close_block))), UTC):%d %b}  {mid:>8.2f}"
                    f"{f'{lo:.1f} to {hi:.1f}':>16}{q50:>9.2f}{f'{q10:.1f} to {q90:.1f}':>16}{up_model:>10.0%}{up_market:>8.0%}{ev[i]:>+11.0%}  "
                    f"{book.bins[i][0]:g} to {book.bins[i][1]:g} at {book.prices[i]:.2f} sats"
                )
    finally:
        await api.close()
    return 0


# ── miners buying cover ─────────────────────────────────────

LADDERS = {
    "DIFFICULTY": (0.0018873916581775485, 10_000.0, [(50.0 + i, 51.0 + i) for i in range(200)]),
    "ADJUSTMENT": (0.0004070655611266205, 100_000.0, [(-30.0 + i, -29.0 + i) for i in range(60)]),
}
OPEN_EPOCHS = 4  # how long before its retarget a market here is traded
VISIT = 144  # blocks between visits: about a day
NEVER = float("inf")  # the edge the bots ask of a range on the hedgers' side: none is enough
CASES = (
    ("miners, no bot", True, None),
    ("bot, no miners", False, 0.0),
    ("bot, no lean", True, 0.0),
    ("lean: 30% edge", True, 0.30),
    ("lean: 100% edge", True, 1.0),
    ("the bots' rule", True, NEVER),
)


def _market(args: tuple) -> dict:
    """One real retarget, from the live seed, with what was known at each visit. Hedgers come first at each visit, buying equal
    contracts on every range from 3% over today's level, at whatever the book asks, whatever the picture says."""
    from . import bots as B
    from . import pricing as P
    from .api import Book

    series, e, name, miners, lean, p, flow = args
    h = _HIST[0]
    alpha, q0, bins = LADDERS[series]
    book = Book(1, "", 0, "live", 0, list(range(1, len(bins) + 1)), list(bins), [q0] * len(bins), alpha)
    book.reprice()
    k = e + 1
    target = k * H.EPOCH
    y = h.epochs[k] / 1e12 if series == "DIFFICULTY" else (h.epochs[k] / h.epochs[k - 1] - 1) * 100
    win = next((i for i, (a, b) in enumerate(bins) if a <= y < b), None)
    cfg = B.BotConfig(
        objective="edge",
        bankroll_sats=400_000,
        kelly=0.15,
        min_edge=0.10,
        fill_margin=0.05,
        exit_edge=0.08,
        edge_close_share=0.25,
        hedger_edge=lean or 0.0,
    )
    held: dict[int, tuple[float, float]] = {}
    paid = sold = cover_cost = cover_pay = cover_fair = 0.0
    visits = range(target - OPEN_EPOCHS * H.EPOCH + 36, target - 3, VISIT)
    for height in visits:
        st = h.state(height, p.rate_tau_h)
        run = H.simulate(st, p, epochs=k - height // H.EPOCH, n=1000, seed=height)
        edges = np.array([bins[0][0]] + [b for _, b in bins])
        if series == "DIFFICULTY":
            probs, spot = H.probabilities(run.difficulty_at(target) / 1e12, edges), st.difficulty / 1e12
            strike = spot * 1.03
        else:
            probs, spot, strike = H.probabilities(run.adjustment_at(target), 1 + edges / 100), 0.0, 2.0
        if miners:
            cover = [i for i, (a, _) in enumerate(bins) if strike <= a < strike * 1.3 + 8]
            contracts = P.contracts_for_budget(book.shares, alpha, cover[0], cover[-1], flow / len(visits)) if cover else 0.0
            if contracts > 0:
                cover_cost += P.ticket(book.shares, alpha, cover[0], cover[-1], contracts, prob=0.0).cost_sats
                cover_pay += contracts * PL_NET if win in cover else 0.0
                cover_fair += contracts * PL_NET * float(probs[cover].sum())
                for i in cover:
                    book.shares[i] += contracts
                book.reprice()
        if lean is None:
            continue
        pr = [float(v) for v in probs]
        for i, n, proceeds in B.exits(book, pr, held, cfg):
            o = book.option_ids[i]
            have, cost = held[o]
            held[o] = (have - n, cost * (have - n) / have)
            book.shares[i] -= n
            sold += proceeds
            book.reprice()
        spent = sum(v[1] for v in held.values())
        for x in B.decide(book, pr, cfg, min(100_000 - spent, 25_000.0), spent, spot):
            o = book.option_ids[x.index]
            have, cost = held.get(o, (0.0, 0.0))
            held[o] = (have + x.contracts, cost + x.cost_sats)
            book.shares[x.index] += x.contracts
            paid += x.cost_sats
            book.reprice()
    payout = held[win + 1][0] * PL_NET if win is not None and win + 1 in held else 0.0
    lower = sum(v[1] for o, v in held.items() if bins[o - 1][0] <= spot)
    return {
        "series": series,
        "e": e,
        "case": name,
        "paid": paid,
        "sold": sold,
        "payout": payout,
        "cover_cost": cover_cost,
        "cover_pay": cover_pay,
        "cover_fair": cover_fair,
        "held_down": lower,
        "held": sum(v[1] for v in held.values()),
    }


PL_NET = 98.0


def hedge(pool, h: History, p: H.Params, flow: float = 200_000.0, say=print) -> list[dict]:
    """What the bot earns and what cover costs, retarget by retarget, on the epochs the fit never saw. The flow is invented: nobody
    has a record of real miners on these markets. Only the outcomes and the forecasts are real. There are no other traders here, so
    most of the bot's profit is the uniform seed, which a real market's traders would share or take first: read the difference
    between the rows, not the level."""
    args = [(s, e, name, miners, lean, p, flow) for s in LADDERS for e in range(SPLIT, len(h.epochs) - 1) for name, miners, lean in CASES]
    rows = list(pool.map(_market, args, chunksize=4))
    say(
        f"{len(range(SPLIT, len(h.epochs) - 1))} real retargets, each market from the live seed over its last {OPEN_EPOCHS} epochs, "
        "visited daily. "
        f"Invented: miners spending ₿{flow:,.0f} a market on cover from +3% up, at any price."
    )
    for s in LADDERS:
        say(
            f"\n{s}\n{'':<18}{'bot paid':>10}{'bot P&L':>10}{'P&L, 90%':>22}{'return':>8}{'held down':>10}"
            f"{'cover paid':>11}{'pays out':>10}{'worth':>10}{'price/worth':>12}"
        )
        for name, _, _ in CASES:
            got = [r for r in rows if r["series"] == s and r["case"] == name]
            pnl = np.array([r["payout"] + r["sold"] - r["paid"] for r in got])
            paid = np.mean([r["paid"] for r in got])
            rng = np.random.default_rng(1)
            boot = pnl[rng.integers(0, len(pnl), (2000, len(pnl)))].mean(axis=1)
            cover = [np.mean([r[c] for r in got]) for c in ("cover_cost", "cover_pay", "cover_fair")]
            down = sum(r["held_down"] for r in got) / max(sum(r["held"] for r in got), 1.0)
            say(
                f"{name:<18}{paid:>10,.0f}{pnl.mean():>+10,.0f}{f'[{np.quantile(boot, 0.05):+,.0f}, {np.quantile(boot, 0.95):+,.0f}]':>22}"
                f"{pnl.mean() / paid if paid else 0:>+8.0%}{down:>10.0%}{cover[0]:>11,.0f}{cover[1]:>10,.0f}{cover[2]:>10,.0f}"
                f"{cover[0] / cover[2] if cover[2] else 0:>12.2f}"
            )
    say(
        "\nNo other traders are simulated, so most of the profit is the uniform seed: read the differences between rows, not the level. "
        "price/worth is what cover cost over what it was worth by the forecast."
    )
    return rows


def main(cmd: str, save: bool = False, paths: int = 1500) -> int:
    if cmd == "now":
        return asyncio.run(today())
    print("reading Bitview's history...")
    h = asyncio.run(download())
    p = H.load_params()
    with ProcessPoolExecutor(max_workers=max((os.cpu_count() or 2) - 1, 1), initializer=_init, initargs=(h,)) as pool:
        if cmd == "backtest":
            backtest(pool, h, p, paths)
        elif cmd == "hedge":
            hedge(pool, h, p)
        else:
            train = origins(FIT_FROM, SPLIT, 288, h.tip)
            print(f"fitting on {len(train)} forecasts from epochs {FIT_FROM} to {SPLIT}")
            p = fit(pool, train, p)
            print("chosen: " + ", ".join(f"{k}={getattr(p, k):g}" for k in GRID))
            if save:
                print(f"saved to {H.save_params(p)}")
            backtest(pool, h, p, paths)
    return 0
