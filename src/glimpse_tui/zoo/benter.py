"""Bill Benter's method, on Bitcoin's closes: a fundamental picture pooled with the public's odds, bets only at a discount.

Benter's Hong Kong syndicate did not out-guess the crowd from scratch. It built a fundamental model of each race, then
combined it with the public's own odds in a conditional logit, with weights fitted by maximum likelihood on past
races, because the crowd's odds carry information no model has. It bet only where the combined probability beat the
odds on offer by a margin, sized by fractional Kelly, on thousands of races, and let the long run do the rest.

Here the fundamental is the zoo's consensus of how far the price moves (seven volatility and jump models, equal
weight). The public is what glimpse.markets shows for the close, smoothed across neighbouring ranges so one trade's
spike does not count as an opinion. They are pooled in log space:

    log c_i = k · [ (1 − β·w) · log f_i  +  β·w · log π̃_i ]  + const

w is how much of a curve the market has formed (1 − its entropy over the ladder's maximum): an untouched close, flat
at the subsidy, says nothing and w ≈ 0, so the bot trusts its fundamental; a close traders have shaped gets the
weight β. k is the sharpness Benter's fit also found (a pooled picture is often too sure or too timid as a whole).
Until enough closes have settled, k = 1 and β = ½. The bot journals every close it looks at to disk (one snapshot per
close per horizon band) and, once 150 of them have settled, refits k and β by maximum likelihood on where the price
actually closed, once an hour, as Benter refitted on the season's results.
"""
from __future__ import annotations

import base64
import json
import math
import threading
import time
from pathlib import Path

import numpy as np

from .core import Ctx, Model, finish
from .opportunist import CONSENSUS_INPUTS, consensus

K0, BETA0 = 1.0, 0.5         # before a fit: plain log pooling, half weight on a fully formed market curve
SMOOTH = 1.5                 # Gaussian smoothing of the market's curve, in ranges (σ)
MIN_FIT = 150                # settled snapshots before the weights are fitted
SUPPORT = 1e-5               # ranges the fundamental gives less than this are not journalled; they settle as the floor
FLOOR = 1e-9
FUND_TTL = 30.0              # seconds before a close's fundamental is due a redraw: the consensus is the slow part
REDRAW_SHARE = 0.4           # at most this share of wall time redrawing stale fundamentals; the rest wait their turn


def smooth(pi: np.ndarray, width: float = SMOOTH) -> np.ndarray:
    """The market's curve with single-range spikes spread onto their neighbours, renormalised."""
    r = max(int(math.ceil(3 * width)), 1)
    x = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (x / width) ** 2)
    out = np.convolve(np.pad(pi, r, mode="edge"), k / k.sum(), mode="valid")
    out = np.maximum(out, FLOOR)
    return out / out.sum()


def formed(pi: np.ndarray) -> float:
    """0 for a flat ladder (nothing traded but the subsidy), towards 1 as the market's curve takes a shape."""
    p = pi[pi > 0]
    return float(np.clip(1 + (p * np.log(p)).sum() / math.log(len(pi)), 0.0, 1.0))


def pool(lf: np.ndarray, lp: np.ndarray, w: float, k: float, beta: float) -> np.ndarray:
    """Log-probabilities of the pooled picture, unnormalised."""
    return k * ((1 - beta * w) * lf + beta * w * lp)


# ── the journal and the fit ─────────────────────────────────

def _home() -> Path:
    from ..auth import config_dir

    return config_dir() / "benter"


def _enc(a: np.ndarray) -> str:
    return base64.b64encode(np.asarray(a, dtype=np.float16).tobytes()).decode()


def _dec(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype=np.float16).astype(float)


class Journal:
    """Snapshots of what the bot saw, and the weights fitted on the ones that have settled. Thread-safe: pictures are
    drawn in worker threads."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home
        self.lock = threading.Lock()
        self.seen: set[tuple] | None = None
        self.k, self.beta, self.n, self.fitted_at = K0, BETA0, 0, 0.0

    @property
    def dir(self) -> Path:
        return self.home or _home()

    def _load(self) -> None:
        if self.seen is not None:
            return
        self.seen = set()
        try:
            for line in (self.dir / "journal.jsonl").read_text().splitlines():
                r = json.loads(line)
                self.seen.add((r["asset"], r["end"], r["band"]))
        except (OSError, ValueError, KeyError):
            pass
        try:
            w = json.loads((self.dir / "weights.json").read_text())
            self.k, self.beta, self.n, self.fitted_at = float(w["k"]), float(w["beta"]), int(w["n"]), float(w["t"])
        except (OSError, ValueError, KeyError):
            pass

    def record(self, ctx: Ctx, f: np.ndarray, pi: np.ndarray, w: float) -> None:
        """One snapshot per close per horizon band (under 2 h, 2–4, 4–8, … hours out): enough to fit on, small on disk."""
        band = int(math.log2(max(ctx.hours, 1.0)))
        key = (ctx.asset, int(ctx.end), band)
        with self.lock:
            self._load()
            if key in self.seen:
                return
            self.seen.add(key)
            idx = np.flatnonzero(f > SUPPORT)
            steps = np.diff(ctx.edges)
            row = {"asset": ctx.asset, "end": int(ctx.end), "band": band, "t": round(ctx.now), "w": round(w, 4),
                   "e0": float(ctx.edges[0]), "n": len(f), "idx": idx.tolist(),
                   "lf": _enc(np.log(f[idx])), "lp": _enc(np.log(pi[idx]))}
            if np.allclose(steps, steps[0]):
                row["step"] = float(steps[0])
            else:
                row["edges"] = [float(x) for x in ctx.edges]
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                with (self.dir / "journal.jsonl").open("a") as fh:
                    fh.write(json.dumps(row) + "\n")
            except OSError:
                pass

    def weights(self, ctx: Ctx) -> tuple[float, float]:
        """(k, β): fitted once an hour on every snapshot whose close has settled, or the priors until there are enough."""
        with self.lock:
            self._load()
            if time.time() - self.fitted_at < 3600:
                return self.k, self.beta
            self.fitted_at = time.time()
        rows = self._settled(ctx)
        if len(rows) >= MIN_FIT:
            k, beta = fit(rows)
            with self.lock:
                self.k, self.beta, self.n = k, beta, len(rows)
                try:
                    (self.dir / "weights.json").write_text(json.dumps({"k": k, "beta": beta, "n": len(rows), "t": self.fitted_at}))
                except OSError:
                    pass
        return self.k, self.beta

    def _settled(self, ctx: Ctx) -> list[tuple[np.ndarray, np.ndarray, float, int]]:
        """(log f, log π̃, w, index of the range the price closed in, within the snapshot's support; −1 outside it)."""
        closes = {int(t.timestamp()) + 3600: float(c) for t, c in zip(ctx.bars.index, ctx.bars["close"])}
        out = []
        try:
            lines = (self.dir / "journal.jsonl").read_text().splitlines()
        except OSError:
            return out
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            price = closes.get(r["end"])
            if r.get("asset") != ctx.asset or price is None:
                continue
            if "step" in r:
                j = int(min(max((price - r["e0"]) // r["step"], 0), r["n"] - 1))
            else:
                j = int(min(max(np.searchsorted(r["edges"], price, side="right") - 1, 0), r["n"] - 1))
            idx = r["idx"]
            pos = idx.index(j) if j in idx else -1
            out.append((_dec(r["lf"]), _dec(r["lp"]), float(r["w"]), pos))
        return out


def fit(rows: list[tuple[np.ndarray, np.ndarray, float, int]]) -> tuple[float, float]:
    """Maximum likelihood (k, β) of the conditional logit over the settled snapshots, as Benter fitted his."""
    from scipy.optimize import minimize

    lfloor = math.log(FLOOR)

    def nll(x: np.ndarray) -> float:
        k, beta = float(x[0]), float(x[1])
        total = 0.0
        for lf, lp, w, pos in rows:
            z = pool(lf, lp, w, k, beta)
            mx = float(z.max())
            lse = mx + math.log(float(np.exp(z - mx).sum()))
            total -= (float(z[pos]) if pos >= 0 else k * lfloor) - lse
        return total / len(rows)

    res = minimize(nll, np.array([K0, BETA0]), method="L-BFGS-B", bounds=[(0.3, 2.5), (0.0, 1.0)])
    return (float(res.x[0]), float(res.x[1])) if res.success else (K0, BETA0)


JOURNAL = Journal()
_FUND: dict[tuple, tuple[float, np.ndarray]] = {}
_FUND_LOCK = threading.Lock()


_REDRAWS: list[tuple[float, float]] = []            # (when, seconds) of recent redraws


def fundamental(ctx: Ctx) -> np.ndarray:
    """The consensus for this close. The market half of the picture moves every second and is read every cycle; this
    half barely moves, and drawing it for a week of closes takes a minute. So a close with no fundamental yet is drawn
    at once, and a stale one (older than FUND_TTL) is redrawn only while redraws have used under REDRAW_SHARE of the
    last ten seconds; otherwise the stale one serves. Closes are asked nearest first, so the nearest, which move most
    with the spot, are redrawn most often."""
    key = (ctx.asset, round(ctx.end), float(ctx.edges[0]), float(ctx.edges[-1]), len(ctx.edges))
    now = time.time()
    with _FUND_LOCK:
        hit = _FUND.get(key)
        while _REDRAWS and _REDRAWS[0][0] < now - 10:
            _REDRAWS.pop(0)
        busy = sum(x for _, x in _REDRAWS) > 10 * REDRAW_SHARE
    if hit and (now - hit[0] < FUND_TTL or busy):
        return hit[1]
    began = time.time()
    f = np.asarray(consensus(ctx), dtype=float)
    with _FUND_LOCK:
        _FUND[key] = (time.time(), f)
        if hit:
            _REDRAWS.append((time.time(), time.time() - began))
        for k in [k for k, (t, _) in _FUND.items() if now - t > 3600]:
            del _FUND[k]
    return f


def benter(ctx: Ctx) -> np.ndarray:
    f = fundamental(ctx)
    if ctx.market is None or len(ctx.market) != len(f):
        return f                                           # no market to read: the fundamental alone
    pi = smooth(np.asarray(ctx.market, dtype=float))
    w = formed(pi)
    JOURNAL.record(ctx, f, pi, w)
    k, beta = JOURNAL.weights(ctx)
    z = pool(np.log(np.maximum(f, FLOOR)), np.log(pi), w, k, beta)
    return finish(np.exp(z - z.max()))


TRADING = {"objective": "edge", "interval_s": 5, "kelly": 0.15, "min_edge": 0.10, "fill_margin": 0.05, "exit_edge": 0.08,
           "edge_close_share": 0.015, "max_markets_per_order": 6,
           "picture_budget_s": 2.0}

MODELS = [
    Model(
        "benter", "Bill Benter", "Benter",
        "The crowd's odds pooled with a model; small bets, only at a discount",
        "Bill Benter's method, the one that beat the Hong Kong tote over thousands of races. It does not try to "
        "out-guess the market from scratch: it takes the curve the market's traders are already forming, smooths it, "
        "and pools it with the zoo's calibrated consensus of how far the price moves, trusting the market more the more "
        "of a shape its traders have given it. Then it buys only the ranges that combined picture says are selling at a "
        "discount, in small fractional-Kelly stakes across every close, and sells a range back when the market pays "
        "more than it is worth. Each bet is small and each edge is modest; the return is meant to come from the long "
        "run. It journals what it saw and, as closes settle, refits how much to trust the crowd against its model. Run "
        "it around the clock: glimpse-tui run benter --series all --live.",
        benter, kind="forecast", factors=("volatility", "tails", "market"),
        reference="Benter (1994), 'Computer based horse race handicapping and wagering systems: a report'; Bolton and "
                  "Chapman (1986), 'Searching for positive returns at the track'; Kelly (1956); Thorp (2006)",
        inputs=CONSENSUS_INPUTS + " And the market itself: every range's shares, drawn as the site draws them (the "
               "LS-LMSR softmax), for the close being priced, read afresh every cycle.",
        maths="log c_i = k·[(1 − β·w)·log f_i + β·w·log π̃_i] + const, renormalised. f is the consensus picture. π̃ is the "
              "market's curve smoothed by a Gaussian of σ = 1.5 ranges. w = 1 + Σ π̃ log π̃ / log n is how formed the "
              "market's curve is, 0 for a flat ladder. k and β start at 1 and ½; the bot keeps one snapshot of (f, π̃, w) "
              "per close per horizon band (under 2 h, 2–4 h, 4–8 h, … out) in ~/.config/glimpse/benter/journal.jsonl, and "
              "once 150 have settled it refits them hourly by maximising Σ log c_winner over the settled snapshots "
              "(L-BFGS-B, k in [0.3, 2.5], β in [0, 1]), the conditional logit Benter fitted on past races. The fitted "
              "weights are kept in weights.json beside it.",
        trades="Fractional Kelly at 0.15 on ranges whose pooled value, after both 2% fees, is at least 10% above the price, "
               "and never past the price at which a 5% discount would be gone, so every contract is bought under its "
               "value. At most 1.5% of the budget held in one close, six closes an order, a cycle every 5 seconds over "
               "every close of the series. A range the market later prices 8% or more above the picture is sold back "
               "down to value. These rules are its own: bots.toml does not change them.",
        pipeline="bins", reads_market=True, trading=TRADING,
    ),
]
