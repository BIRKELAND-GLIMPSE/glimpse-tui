"""Regimes and jumps: processes fitted on the bars that change the shape of the random walk, plus GJR-GARCH.

The lab's shaped-path models (`models/zoo_shapes.py`) fit a structure at refit time, shrink it towards the random
walk and simulate paths with it; an unsupported structure gives the baseline. The terminal keeps the fits and the
`active` tests as they are and simulates a few thousand paths to the close with numpy. Two lab models lean on
statsmodels' MarkovRegression and one on the `arch` package; the terminal has neither, so this module carries a
small two-state Markov-switching fit (Hamilton filter, Kim smoother, EM) and a Gaussian quasi-maximum-likelihood
GJR-GARCH(1,1).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.signal import lfilter
from scipy.stats import norm

from .core import (
    Ctx,
    Model,
    ModelUnavailable,
    base_pdf,
    cached,
    conviction_picture,
    ewma_variance,
    from_samples,
    hour_of_day_profile,
    rng_for,
    to_bins,
)
from .signals import hourly_vol, log_close

FAMILY = "Regime & jumps"
N_PATHS = 4000


# ── shared inputs ───────────────────────────────────────────

def _inputs(ctx: Ctx) -> dict:
    """The lab's `_inputs`: log closes, the hour-of-day profile, deseasonalised returns and the EWMA volatility per bar."""
    def build() -> dict:
        logp = np.log(ctx.bars["close"].to_numpy(dtype=float))
        r = np.diff(logp)
        idx = pd.DatetimeIndex(ctx.bars.index[1:])
        profile = hour_of_day_profile(pd.Series(r, index=idx))
        rd = r / profile[idx.hour.to_numpy()]
        s2, s2_next = ewma_variance(rd, 0.94)
        sigma = np.concatenate([[np.sqrt(s2[0])], np.sqrt(s2), [np.sqrt(s2_next)]])[: logp.size]
        return {"logp": logp, "r": r, "rd": rd, "profile": profile, "sigma": np.maximum(sigma, 1e-6)}

    return cached(ctx, "regimes:inputs", build)


def _standardized(ctx: Ctx) -> tuple[np.ndarray, np.ndarray]:
    """Raw hourly log returns and the same returns divided by the hour-of-day profile and the EWMA volatility."""
    i = _inputs(ctx)
    return i["r"], i["rd"] / i["sigma"][1:]


class _Steps:
    """The hourly steps to the close. `sd` is the baseline standard deviation of each step, `frac` the share of an
    hour it covers, `unit` = profile * sqrt(frac) (what a process with its own hourly volatility is multiplied by)
    and `profile` the hour-of-day multiplier alone."""

    def __init__(self, ctx: Ctx) -> None:
        self.sd = np.sqrt(ctx.scale.step_variances(ctx.now, ctx.end))
        frac, t = [], ctx.now
        while t < ctx.end - 1e-6:
            nxt = min((t // 3600 + 1) * 3600, ctx.end)
            frac.append((nxt - t) / 3600)
            t = nxt
        self.frac = np.array(frac) if len(frac) == self.sd.size else np.full(self.sd.size, 1e-6)
        self.unit = self.sd / np.sqrt(ctx.scale.s2_next)
        self.profile = self.unit / np.sqrt(self.frac)
        self.n = self.sd.size


def _pool(ctx: Ctx) -> np.ndarray:
    return ctx.scale.z_pool / max(float(np.std(ctx.scale.z_pool)), 1e-12)


def _baseline(ctx: Ctx) -> np.ndarray:
    return to_bins(ctx, base_pdf(ctx))


def _draw(rng: np.random.Generator, pool: np.ndarray, n_steps: int) -> np.ndarray:
    return pool[rng.integers(0, pool.size, size=(N_PATHS, n_steps))]


# ── two-state Markov switching (replaces statsmodels' MarkovRegression) ─────

def markov_switching_fit(x: np.ndarray, switching_mean: bool, n_iter: int = 40, tol: float = 1e-6) -> dict:
    """Two Gaussian states with their own variance (and mean, when `switching_mean`), a first-order Markov chain
    between them, fitted by EM: Hamilton's filter forward, Kim's smoother backward. Returns the parameters and the
    filtered probability of each state per observation. Scalar loops: two states do not repay numpy per step."""
    x = np.asarray(x, dtype=float)
    n = x.size
    if n < 100 or not np.all(np.isfinite(x)) or float(np.std(x)) <= 0:
        raise ModelUnavailable("too few returns for the regime fit")
    m, v = float(np.mean(x)), float(np.var(x))
    sd = math.sqrt(v)
    mu = [m - 0.25 * sd, m + 0.25 * sd] if switching_mean else [0.0, 0.0]
    var = [0.5 * v, 2.0 * v]
    p00, p11 = 0.95, 0.95
    floor = 1e-4 * v
    xs = x.tolist()
    last_ll = -np.inf
    f0 = f1 = q0 = q1 = None

    def filt() -> tuple[list[float], list[float], list[float], list[float], float]:
        b = [np.maximum(np.exp(-0.5 * (x - mu[k]) ** 2 / var[k]) / math.sqrt(2 * math.pi * var[k]), 1e-300).tolist() for k in (0, 1)]
        b0, b1 = b
        pi0 = (1 - p11) / max(2 - p00 - p11, 1e-12)
        f0, f1, q0, q1 = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n        # filtered and one-step predicted
        a0, a1, ll = pi0, 1 - pi0, 0.0
        for t in range(n):
            if t:
                a0, a1 = a0 * p00 + a1 * (1 - p11), a0 * (1 - p00) + a1 * p11
            q0[t], q1[t] = a0, a1
            a0, a1 = a0 * b0[t], a1 * b1[t]
            c = a0 + a1
            a0, a1 = a0 / c, a1 / c
            f0[t], f1[t] = a0, a1
            ll += math.log(c)
        return f0, f1, q0, q1, ll

    for _ in range(n_iter):
        f0, f1, q0, q1, ll = filt()
        if ll - last_ll < tol * n and np.isfinite(last_ll):
            break
        last_ll = ll
        s0, s1 = [0.0] * n, [0.0] * n
        s0[-1], s1[-1] = f0[-1], f1[-1]
        n00 = n01 = n10 = n11 = 0.0
        for t in range(n - 2, -1, -1):
            r0, r1 = s0[t + 1] / max(q0[t + 1], 1e-300), s1[t + 1] / max(q1[t + 1], 1e-300)
            x00, x01 = f0[t] * p00 * r0, f0[t] * (1 - p00) * r1
            x10, x11 = f1[t] * (1 - p11) * r0, f1[t] * p11 * r1
            s0[t], s1[t] = x00 + x01, x10 + x11
            n00, n01, n10, n11 = n00 + x00, n01 + x01, n10 + x10, n11 + x11
        p00 = min(max(n00 / max(n00 + n01, 1e-300), 1e-6), 1 - 1e-6)
        p11 = min(max(n11 / max(n10 + n11, 1e-300), 1e-6), 1 - 1e-6)
        for k, s in enumerate((np.array(s0), np.array(s1))):
            w = max(float(s.sum()), 1e-12)
            if switching_mean:
                mu[k] = float(np.dot(s, xs)) / w
            var[k] = max(float(np.dot(s, (x - mu[k]) ** 2)) / w, floor)
    f0, f1, _, _, ll = filt()
    return {"mu": mu, "var": var, "stay": [p00, p11], "filtered": np.column_stack([f0, f1]), "loglik": ll}


def markov_regime(ctx: Ctx, fit_hours: int = 1000) -> np.ndarray:
    def build() -> pd.Series:
        out = pd.Series(np.nan, index=ctx.bars.index)
        r = (log_close(ctx.bars).diff() * 100).iloc[-fit_hours:].dropna()
        try:
            fit = markov_switching_fit(r.to_numpy(), switching_mean=True)
        except ModelUnavailable:
            return out
        out.loc[r.index] = fit["filtered"][:, int(np.argmax(fit["mu"]))] - 0.5
        return out

    return conviction_picture(ctx, cached(ctx, "markov_regime:signal", build), sign=1, family=FAMILY)


# ── jumps ───────────────────────────────────────────────────

def kou_structure(ctx: Ctx, jump_threshold: float = 4.0, min_jumps: int = 8) -> dict:
    r, z = _standardized(ctx)
    ok = np.isfinite(z)
    r, z = r[ok], z[ok]
    jumps = np.abs(z) > jump_threshold
    n = int(jumps.sum())
    if n < min_jumps:
        return {"active": False, "jumps": n}
    size = np.abs(r[jumps])
    up = r[jumps] > 0
    floor = float(np.quantile(size, 0.1))
    exc_up = size[up] - floor if up.any() else np.array([np.mean(size) - floor])
    exc_dn = size[~up] - floor if (~up).any() else np.array([np.mean(size) - floor])
    return {"active": True, "jumps": n, "lambda_per_hour": n / r.size, "p_up": float(up.mean()), "floor": floor,
            "mean_excess_up": float(max(np.mean(exc_up), 1e-5)), "mean_excess_down": float(max(np.mean(exc_dn), 1e-5)),
            "threshold": jump_threshold}


def kou_jump_paths(ctx: Ctx) -> np.ndarray:
    s = cached(ctx, "kou_jump_paths:fit", lambda: kou_structure(ctx))
    if not s["active"]:
        return _baseline(ctx)
    rng, st, pool = rng_for(ctx, "kou_jump_paths"), _Steps(ctx), _pool(ctx)
    diffusion = _draw(rng, pool[np.abs(pool) <= s["threshold"]], st.n) * st.sd
    shape = (N_PATHS, st.n)
    hit = rng.random(shape) < (1.0 - np.exp(-s["lambda_per_hour"] * st.frac))
    is_up = rng.random(shape) < s["p_up"]
    size = s["floor"] + np.where(is_up, rng.exponential(s["mean_excess_up"], shape), rng.exponential(s["mean_excess_down"], shape))
    return from_samples(ctx, (diffusion + np.where(hit, np.where(is_up, size, -size), 0.0)).sum(axis=1))


def hawkes_loglik(params: np.ndarray, times: np.ndarray, horizon: float) -> float:
    """Negative log-likelihood of an exponential-kernel Hawkes process: lambda(t) = mu + alpha beta sum exp(-beta (t - t_i))."""
    mu, alpha, beta = np.exp(params[0]), 1.0 / (1.0 + np.exp(-params[1])), np.exp(params[2])
    a, ll, prev = 0.0, 0.0, None
    for t in times:
        if prev is not None:
            a = np.exp(-beta * (t - prev)) * (1.0 + a)
        ll += np.log(mu + alpha * beta * a)
        prev = t
    ll -= mu * horizon + alpha * float(np.sum(1.0 - np.exp(-beta * (horizon - times))))
    return -ll


def hawkes_structure(ctx: Ctx, jump_threshold: float = 3.5, min_jumps: int = 15) -> dict:
    # jumps are measured against a slow, robust volatility (30-day median absolute move), so a cluster of jumps is
    # not absorbed by a fast volatility estimate and the self-excitation stays visible
    rd = _inputs(ctx)["rd"]
    slow = pd.Series(np.abs(rd)).rolling(720, min_periods=168).median().shift(1).to_numpy() / 0.6745
    z = np.where(np.isfinite(slow) & (slow > 0), rd / np.where(slow > 0, slow, 1.0), 0.0)
    times = np.nonzero(np.abs(z) > jump_threshold)[0].astype(float) + 1.0
    horizon, n = float(z.size), times.size
    if n < min_jumps:
        return {"active": False, "jumps": int(n)}
    ll_poisson = n * np.log(n / horizon) - n
    x0 = np.array([np.log(0.5 * n / horizon), 0.0, np.log(0.2)])
    res = minimize(hawkes_loglik, x0, args=(times, horizon), method="L-BFGS-B", bounds=[(-15, 2), (-6, 4), (np.log(0.01), np.log(5.0))])
    mu, alpha, beta = float(np.exp(res.x[0])), float(1 / (1 + np.exp(-res.x[1]))), float(np.exp(res.x[2]))
    lr = 2.0 * (-float(res.fun) - ll_poisson)
    excitation = float(np.sum(np.exp(-beta * (horizon - times))))
    return {"active": bool(np.isfinite(res.fun)) and lr > 5.99 and alpha > 0.02, "jumps": int(n), "mu": mu, "alpha": alpha,
            "beta": beta, "lr_stat": lr, "lambda_now": mu + alpha * beta * excitation, "threshold": jump_threshold,
            "slow_vol_now": float(np.nanmedian(np.abs(rd[-720:])) / 0.6745), "jump_z": z[np.abs(z) > jump_threshold]}


def hawkes_jump_paths(ctx: Ctx) -> np.ndarray:
    s = cached(ctx, "hawkes_jump_paths:fit", lambda: hawkes_structure(ctx))
    if not s["active"]:
        return _baseline(ctx)
    rng, st, pool = rng_for(ctx, "hawkes_jump_paths"), _Steps(ctx), _pool(ctx)
    # ordinary moves: the baseline's increments with the baseline's own large moves removed (they are the jumps)
    diffusion = _draw(rng, pool[np.abs(pool) <= s["threshold"]], st.n) * st.sd
    jump_z, jump_scale = s["jump_z"], s["slow_vol_now"] * st.profile
    mu, alpha, beta = s["mu"], s["alpha"], s["beta"]
    lam = np.full(N_PATHS, s["lambda_now"])
    x = np.zeros(N_PATHS)
    for j in range(st.n):
        hit = rng.random(N_PATHS) < 1.0 - np.exp(-lam * st.frac[j])
        jz = jump_z[rng.integers(0, jump_z.size, N_PATHS)]
        x = x + diffusion[:, j] + np.where(hit, jz * jump_scale[j], 0.0)
        lam = mu + (lam - mu) * np.exp(-beta * st.frac[j]) + alpha * beta * hit
    return from_samples(ctx, x)


# ── stochastic volatility, persistence, regimes ─────────────

def stochastic_vol_structure(ctx: Ctx) -> dict:
    i = _inputs(ctx)
    h = np.log(np.maximum(i["sigma"], 1e-8) ** 2)
    hd = h[::24]
    if hd.size < 60:
        return {"active": False, "reason": "too little history"}
    x, y = hd[:-1], hd[1:]
    xm = x.mean()
    sxx = float(np.dot(x - xm, x - xm))
    phi = float(np.dot(x - xm, y - y.mean()) / sxx) if sxx > 0 else 0.0
    c = float(y.mean() - phi * xm)
    e = y - c - phi * x
    if not 0.0 < phi < 0.999:
        return {"active": False, "reason": f"daily log-variance persistence {phi:.3f}"}
    theta = c / (1.0 - phi)
    phi1 = phi ** (1.0 / 24.0)
    xi1 = float(np.std(e)) * np.sqrt((1.0 - phi1**2) / (1.0 - phi**2))
    _, z = _standardized(ctx)
    zd = np.add.reduceat(np.where(np.isfinite(z), z, 0.0), np.arange(0, z.size, 24))[: e.size] / np.sqrt(24)
    m = min(zd.size, e.size)
    rho = float(np.corrcoef(zd[:m], e[:m])[0, 1]) if m > 30 else 0.0
    rho = rho if np.isfinite(rho) else 0.0
    t_rho = rho * np.sqrt(max(m - 2, 1)) / np.sqrt(max(1 - rho * rho, 1e-9))
    rho = float(np.clip(rho, -0.9, 0.9)) if abs(t_rho) > 2 else 0.0
    return {"active": xi1 > 0, "kappa_per_hour": 1.0 - phi1, "theta": float(theta), "vol_of_vol_per_hour": float(xi1), "rho": rho}


def stochastic_vol_paths(ctx: Ctx) -> np.ndarray:
    s = cached(ctx, "stochastic_vol_paths:fit", lambda: stochastic_vol_structure(ctx))
    if not s["active"]:
        return _baseline(ctx)
    rng, st = rng_for(ctx, "stochastic_vol_paths"), _Steps(ctx)
    draws = _draw(rng, _pool(ctx), st.n)
    k, xi, rho, theta = s["kappa_per_hour"], s["vol_of_vol_per_hour"], s["rho"], s["theta"]
    h = np.full(N_PATHS, np.log(max(ctx.scale.s2_next, 1e-16)))
    x = np.zeros(N_PATHS)
    for j in range(st.n):
        x = x + draws[:, j] * st.unit[j] * np.exp(h / 2)
        ev = rho * draws[:, j] + np.sqrt(1 - rho * rho) * rng.standard_normal(N_PATHS)
        h = h + k * st.frac[j] * (theta - h) + xi * np.sqrt(st.frac[j]) * ev
    return from_samples(ctx, x)


def dfa_hurst(z: np.ndarray, scales: np.ndarray | None = None) -> float:
    """Hurst exponent by detrended fluctuation analysis (Peng et al. 1994) of a stationary increment series."""
    z = np.asarray(z, dtype=float)
    y = np.cumsum(z - z.mean())
    n = y.size
    if scales is None:
        scales = np.unique(np.logspace(np.log10(8), np.log10(max(16, n // 8)), 14).astype(int))
    fs, ss = [], []
    for s in scales:
        k = n // s
        if k < 4:
            continue
        seg = y[: k * s].reshape(k, s)
        t = np.arange(s, dtype=float)
        tm = t - t.mean()
        slope = (seg - seg.mean(axis=1, keepdims=True)) @ tm / np.dot(tm, tm)
        fit = seg.mean(axis=1, keepdims=True) + slope[:, None] * tm[None, :]
        f = np.sqrt(np.mean((seg - fit) ** 2))
        if f > 0:
            fs.append(np.log(f))
            ss.append(np.log(s))
    if len(fs) < 3:
        return 0.5
    return float(np.polyfit(ss, fs, 1)[0])


def fbm_structure(ctx: Ctx, fit_hours: int = 4096, surrogates: int = 12) -> dict:
    _, z = _standardized(ctx)
    z = z[np.isfinite(z)][-fit_hours:]
    if z.size < 512:
        return {"active": False, "reason": "too little history"}
    h = dfa_hurst(z)
    rng = np.random.default_rng(7)               # the shuffles are part of the fit: the same bars give the same fit
    null = np.array([dfa_hurst(rng.permutation(z)) for _ in range(surrogates)])
    sd = float(max(np.std(null), 1e-3))
    d = h - float(np.mean(null))
    t = d / sd
    shrunk = 0.5 + d * max(0.0, 1.0 - 4.0 / (t * t)) if t != 0 else 0.5
    shrunk = float(np.clip(shrunk, 0.2, 0.8))
    return {"active": abs(shrunk - 0.5) > 0.005, "hurst_raw": h, "t_stat": t, "hurst": shrunk}


def fbm_hurst_paths(ctx: Ctx) -> np.ndarray:
    s = cached(ctx, "fbm_hurst_paths:fit", lambda: fbm_structure(ctx))
    if not s["active"]:
        return _baseline(ctx)
    rng, st, hurst = rng_for(ctx, "fbm_hurst_paths"), _Steps(ctx), s["hurst"]
    k = np.arange(st.n, dtype=float)
    lag = np.abs(k[:, None] - k[None, :])
    cov = 0.5 * (np.abs(lag + 1) ** (2 * hurst) - 2 * lag ** (2 * hurst) + np.abs(lag - 1) ** (2 * hurst))
    g = rng.standard_normal((N_PATHS, st.n)) @ np.linalg.cholesky(cov + 1e-10 * np.eye(st.n)).T
    pool = np.sort(_pool(ctx))
    zs = np.interp(norm.cdf(g) * (pool.size - 1), np.arange(pool.size), pool)       # the empirical marginal
    zs /= max(float(np.std(zs)), 1e-9)
    return from_samples(ctx, (zs * st.sd).sum(axis=1))


def regime_switching_structure(ctx: Ctx, fit_hours: int = 2000) -> dict:
    rd = _inputs(ctx)["rd"][-fit_hours:] * 100
    try:
        fit = markov_switching_fit(rd[np.isfinite(rd)], switching_mean=False)
    except ModelUnavailable as e:
        return {"active": False, "reason": str(e)}
    order = np.argsort(fit["var"])
    sd = np.sqrt(np.array(fit["var"])[order]) / 100.0
    stay = np.array(fit["stay"])[order]
    ratio = float(sd[1] / max(sd[0], 1e-12))
    return {"active": ratio > 1.3 and bool(np.all(stay < 0.9999)), "vol_calm": float(sd[0]), "vol_storm": float(sd[1]),
            "vol_ratio": ratio, "stay_calm": float(stay[0]), "stay_storm": float(stay[1]),
            "p_storm_now": float(fit["filtered"][-1][order][1])}


def regime_switching_paths(ctx: Ctx) -> np.ndarray:
    s = cached(ctx, "regime_switching_paths:fit", lambda: regime_switching_structure(ctx))
    if not s["active"]:
        return _baseline(ctx)
    rng, st = rng_for(ctx, "regime_switching_paths"), _Steps(ctx)
    draws = _draw(rng, _pool(ctx), st.n)
    vols, stay = np.array([s["vol_calm"], s["vol_storm"]]), np.array([s["stay_calm"], s["stay_storm"]])
    state = (rng.random(N_PATHS) < s["p_storm_now"]).astype(int)
    x = np.zeros(N_PATHS)
    for j in range(st.n):
        x = x + draws[:, j] * st.unit[j] * vols[state]
        state = np.where(rng.random(N_PATHS) > stay[state], 1 - state, state)
    return from_samples(ctx, x)


# ── change points ───────────────────────────────────────────

def bocpd_posterior_mean(z: np.ndarray, hazard: float = 1 / 720, prior_var: float = 0.01, max_run: int = 1500) -> np.ndarray:
    """Posterior mean of the current segment's mean (in the units of z) by Bayesian online change-point detection
    (Adams and MacKay 2007) with a Normal likelihood of unit variance and a N(0, prior_var) prior on each segment mean.
    The value at t uses z[0..t] only."""
    n = z.size
    out = np.full(n, np.nan)
    p, cnt, tot = np.array([1.0]), np.array([0.0]), np.array([0.0])
    prec0 = 1.0 / prior_var
    for t in range(n):
        x = z[t]
        if not np.isfinite(x):
            out[t] = out[t - 1] if t else np.nan
            continue
        mean = tot / (cnt + prec0)
        var = 1.0 + 1.0 / (cnt + prec0)
        joint = p * np.exp(-0.5 * (x - mean) ** 2 / var) / np.sqrt(var)
        p = np.concatenate([[float(joint.sum() * hazard)], joint * (1 - hazard)])
        cnt = np.concatenate([[0.0], cnt + 1])
        tot = np.concatenate([[0.0], tot + x])
        cnt[0], tot[0] = 1.0, x                  # a new run has seen x as well: the change point sits before x
        if p.size > max_run:
            p, cnt, tot = p[:max_run], cnt[:max_run], tot[:max_run]
        total = p.sum()
        if not total > 0:                        # an absurd outlier underflowed every run: start again
            p, cnt, tot, total = np.array([1.0]), np.array([1.0]), np.array([x]), 1.0
        p = p / total
        out[t] = float(np.dot(p, tot / (cnt + prec0)))
    return out


def changepoint_drift(ctx: Ctx) -> np.ndarray:
    def build() -> pd.Series:
        z = (log_close(ctx.bars).diff() / hourly_vol(ctx.bars)).to_numpy()
        return pd.Series(bocpd_posterior_mean(z), index=ctx.bars.index)

    return conviction_picture(ctx, cached(ctx, "changepoint_drift:signal", build), sign=1, family=FAMILY)


# ── GJR-GARCH (replaces the `arch` package) ─────────────────

def gjr_variance(params: np.ndarray, y: np.ndarray, h0: float) -> np.ndarray:
    """h[t] = omega + (alpha + gamma * 1[y[t-1] < 0]) * y[t-1]^2 + beta * h[t-1], as one linear filter."""
    omega, alpha, gamma, beta = params
    u = np.empty(y.size)
    u[0] = h0
    u[1:] = omega + (alpha + gamma * (y[:-1] < 0)) * y[:-1] ** 2
    return np.maximum(lfilter([1.0], [1.0, -beta], u), 1e-12)


def gjr_fit(y: np.ndarray) -> dict:
    """Gaussian quasi-maximum likelihood with variance targeting: omega is set so that the long-run variance equals
    the sample variance, which leaves three coefficients to fit. On a few thousand hourly bars the persistence
    alpha + gamma / 2 + beta usually lands close to one, where a free omega is barely identified."""
    y = np.asarray(y, dtype=float)
    y = y[np.isfinite(y)]
    if y.size < 500:
        raise ModelUnavailable(f"garch_gjr_skewt needs at least 500 hourly returns, has {y.size}")
    v = float(np.var(y))
    if not v > 0:
        raise ModelUnavailable("garch_gjr_skewt: the returns do not move")
    ys = y / np.sqrt(v)                          # unit variance: the same optimiser scale for every asset
    h0 = float(np.mean(ys[:100] ** 2))

    def full(p: np.ndarray) -> np.ndarray:
        return np.array([1.0 - p[0] - 0.5 * p[1] - p[2], p[0], p[1], p[2]])

    def nll(p: np.ndarray) -> float:
        if p[0] + 0.5 * p[1] + p[2] >= 0.999:
            return 1e10
        h = gjr_variance(full(p), ys, h0)
        return 0.5 * float(np.sum(np.log(h) + ys**2 / h))

    best = None
    for x0 in ((0.03, 0.04, 0.92), (0.08, 0.05, 0.80), (0.02, 0.02, 0.95)):
        res = minimize(nll, np.array(x0), method="L-BFGS-B", bounds=[(0.0, 0.5), (0.0, 0.7), (0.0, 0.999)])
        if np.isfinite(res.fun) and res.fun < 1e9 and (best is None or res.fun < best.fun):
            best = res
    if best is None:
        raise ModelUnavailable("garch_gjr_skewt: the variance fit did not converge")
    p = full(best.x)
    h = gjr_variance(p, ys, h0)
    z = ys / np.sqrt(h)
    h_next = p[0] + (p[1] + p[2] * (ys[-1] < 0)) * ys[-1] ** 2 + p[3] * h[-1]
    # back to the units of y: omega and h scale with the variance, the other coefficients do not
    return {"omega": float(p[0] * v), "alpha": float(p[1]), "gamma": float(p[2]), "beta": float(p[3]), "h_next": float(h_next * v),
            "z": z[24:] if z.size > 524 else z, "persistence": float(p[1] + 0.5 * p[2] + p[3])}


def garch_gjr_skewt(ctx: Ctx, window_hours: int = 8760) -> np.ndarray:
    fit = cached(ctx, "garch_gjr_skewt:fit", lambda: gjr_fit(_inputs(ctx)["rd"][-window_hours:]))
    rng, st = rng_for(ctx, "garch_gjr_skewt"), _Steps(ctx)
    omega, alpha, gamma, beta, z = fit["omega"], fit["alpha"], fit["gamma"], fit["beta"], fit["z"]
    h = np.full(N_PATHS, fit["h_next"])
    x = np.zeros(N_PATHS)
    # the variance recursion runs on whole deseasonalised hours; only the price move of a partial hour is scaled down
    for j in range(st.n):
        e = z[rng.integers(0, z.size, N_PATHS)] * np.sqrt(h)
        x = x + e * st.unit[j]
        h = omega + (alpha + gamma * (e < 0)) * e * e + beta * h
    return from_samples(ctx, x)


_SHAPED = ("randomness", "tails", "shape")

MODELS = [
    Model("markov_regime", "Markov Regime", FAMILY, "Decides which of two markets we are currently in",
          "Two markets hide inside one price series: a better regime with a higher average hourly return and a worse one with "
          "a lower average, each with its own volatility, and the market switches between them and tends to stay where it is. "
          "Hamilton's two-state Markov-switching model sorts the recent hours into those regimes from their returns alone. The "
          "picture leans up when the latest hours look like the better regime and down when they look like the worse one.",
          markov_regime, factors=("direction",), reference="Markov-switching regression (Hamilton 1989)",
          inputs="Hourly closes: the last 1,000 hourly log returns, in percent. The fit needs at least 100 of them (fewer, or a "
                 "constant series, and the signal is empty and the picture is the baseline); readings exist for those 1,000 bars only "
                 "and the percentile needs 200 of them. Refitted once per new bar.",
          maths="Two Gaussian states with their own mean μ_k and variance v_k and a first-order Markov chain with stay probabilities "
                "p00, p11, fitted on r_t = 100·(ℓ_t − ℓ_t−1) by EM: Hamilton's filter forward, Kim's smoother backward, at most 40 "
                "iterations, stopped when the log-likelihood gains less than 10⁻⁶ per observation. Starting values: means at the "
                "sample mean ∓ 0.25 sd, variances 0.5 and 2 times the sample variance, p00 = p11 = 0.95; variances floored at 10⁻⁴ of "
                "the sample variance, stay probabilities kept inside [10⁻⁶, 1 − 10⁻⁶]. The signal at bar t is the filtered probability "
                "of the state with the higher fitted mean, given returns up to t, minus 0.5 (the parameters are fitted on the whole "
                "window, so only the filtering is causal). sign = +1, family Regime & jumps: timing ride (full drift at every "
                "horizon), no shape tilt, conviction 0.6. Terminal version: the lab fits the same model with statsmodels on 2,000 "
                "hours. A positive reading, the better regime more likely than not, leans the picture up.",
          trades="When the filtered probability of the better regime is high by its own history the whole bell shifts up by up to "
                 "0.6σ, so it puts more weight than the market on every range above roughly +0.3σ, increasingly so further out, and "
                 "buys those; it thins every range below spot and never buys them. A low probability is the mirror image. A reading "
                 "near the middle of its history leaves the baseline and the bot buys nothing.",
          pipeline="conviction"),
    Model("changepoint_drift", "Change-point Drift", FAMILY, "Leans with the average return since the last regime change",
          "Markets change their minds, and the useful average return is the one since the last change, not the one over a fixed "
          "window. Bayesian online change-point detection keeps, after every hour, a probability for every possible age of the "
          "current regime, from one hour to many weeks, and the average return within each. The signal is that average blended "
          "across the possible ages: soon after a genuine shift it shows the new direction, and when the evidence is mixed it "
          "stays near zero. A fresh up-regime leans the picture up, a fresh down-regime down.",
          changepoint_drift, factors=("direction",), reference="Bayesian online changepoint detection (Adams and MacKay 2007)",
          inputs="Hourly closes and the EWMA hourly volatility from signals.hourly_vol (span 168 hours, 7 days, needing 48 bars). Each "
                 "hourly log return is divided by that volatility; readings start at about bar 49 and the percentile needs 200 of "
                 "them. A missing return carries the previous reading forward.",
          maths="With z_t = (ℓ_t − ℓ_t−1)/v_t, the run-length recursion of Adams and MacKay with a constant hazard of 1/720 per hour "
                "(one change every 30 days on average), a Normal likelihood of unit variance, a N(0, 0.01) prior on every segment's "
                "mean, and run lengths truncated at 1,500 hours. For a run of n hours with sum S of its z's the posterior mean is "
                "S/(n + 100) and the predictive variance 1 + 1/(n + 100); each hour the run probabilities are multiplied by the "
                "predictive density of z_t, a new run gets the hazard times their total (and counts z_t as its first observation), "
                "the rest continue with 1 − hazard, and everything is renormalised. The signal at bar t is Σ_runs p(run)·S/(n + 100): "
                "the posterior mean of the current segment's mean hourly return in volatility units, using z_0..z_t only. sign = +1, "
                "family Regime & jumps: timing ride, no shape tilt, conviction 0.6. A positive reading leans the picture up.",
          trades="After a fresh shift up the bell shifts up by up to 0.6σ at every horizon, so the picture puts more weight than the "
                 "market on every range above roughly +0.3σ and buys those, never the ranges below spot; after a shift down the "
                 "mirror image. While the average since the likely change point sits mid-history, near zero, the picture is the "
                 "baseline and the bot buys nothing.",
          pipeline="conviction"),
    Model("kou_jump_paths", "Kou Gap Risk", FAMILY, "Up-gaps and down-gaps sized separately, in dollars",
          "Gap risk as a picture. Kou's jump-diffusion splits hourly moves into ordinary noise and rare jumps, with up-jumps and "
          "down-jumps each drawn from their own exponential size distribution, so the tails can be fat and lopsided while the body "
          "stays ordinary. The model counts the jump hours in its bars, measures how often they came and how large they were in "
          "log-price terms rather than volatility terms, and adds jumps of that kind to calm-noise paths: when the market is quiet "
          "the baseline forgets that jumps happen, and this picture does not.",
          kou_jump_paths, factors=_SHAPED, reference="Double exponential jump-diffusion (Kou 2002, Management Science)",
          inputs="Hourly closes: raw hourly log returns r and the same returns deseasonalised by the hour-of-day profile and divided "
                 "by the EWMA (λ = 0.94) volatility forecast of their own hour, z. A jump is an hour with |z| > 4; with fewer than 8 "
                 "jumps in the loaded bars (the lab counts a year, the terminal about twelve weeks) the picture is the baseline. "
                 "Refitted once per bar.",
          maths="Jump rate λ = number of jump hours / number of returns, per hour; p_up = share of jumps that were positive; floor = "
                "10th percentile of the jump hours' |r|; mean excess sizes m_up, m_dn = mean of |r| − floor over the up and the down "
                "jumps (each at least 10⁻⁵; a side with no jumps uses the mean of all jump sizes). Simulation: 4,000 paths; each "
                "hourly step adds a draw from the baseline's standardised residual pool with |z| > 4 removed (not rescaled, so the "
                "noise is slightly narrower than the baseline's) times that step's baseline σ, plus, with probability 1 − exp(−λ·frac) "
                "where frac is the share of the hour the step covers, one jump of size floor + Exponential(m_up) upward with "
                "probability p_up or floor + Exponential(m_dn) downward otherwise, in log-price units untouched by the volatility "
                "clock. Jumps are not compensated, so more or bigger up-jumps than down-jumps in the history lean the centre up by "
                "about λ·h·(p_up·(floor + m_up) − (1 − p_up)·(floor + m_dn)) over h hours. The summed log moves go to from_samples.",
          trades="It puts more weight than the market on the ranges at least a historic jump's size from spot on each side (several σ for a "
                 "close within hours, under one σ days out), more on the side whose jumps were more frequent or larger, and buys those; the "
                 "body stays close to the baseline, so ranges near spot are seldom bought. With fewer than 8 jumps in the bars the picture "
                 "is the baseline and the bot buys nothing.",
          pipeline="paths"),
    Model("hawkes_jump_paths", "Hawkes Cascade", FAMILY, "Reacts to bursts of big moves, the way aftershocks follow a quake",
          "Aftershocks. In a Hawkes process every jump raises the chance of another one soon after, the way earthquakes cluster, "
          "and the excitement fades over hours. The model fits that self-exciting intensity to the large hourly moves in its bars, "
          "measured against a slow monthly volatility so a cluster of jumps is not hidden by a fast volatility estimate, reads "
          "today's intensity from the most recent jumps, and simulates paths in which jumps can trigger more jumps. Right after a "
          "burst of big moves the picture has much fatter tails for the coming hours and days; after a quiet stretch it sits close "
          "to the baseline. It trusts the clustering only when a likelihood-ratio test prefers it to jumps arriving at random.",
          hawkes_jump_paths, factors=_SHAPED,
          reference="Self-exciting point processes (Hawkes 1971, Biometrika); jump clustering (Ait-Sahalia, Cacho-Diaz and Laeven 2015)",
          inputs="Hourly closes: deseasonalised hourly log returns rd, and a slow volatility, the rolling 720-hour (30-day) median of "
                 "|rd| over the previous bars (at least 168 of them) divided by 0.6745. A jump is an hour with |rd| above 3.5 slow "
                 "volatilities; it needs at least 15 jumps in the loaded bars (about twelve weeks; the lab uses a year), otherwise "
                 "the baseline. Refitted once per bar.",
          maths="Jump times t_i (in hours) feed an exponential-kernel Hawkes intensity λ(t) = μ + α·β·Σ_i exp(−β(t − t_i)), α the "
                "branching ratio, fitted by maximum likelihood with L-BFGS-B on (ln μ, logit α, ln β) with bounds ln μ ∈ [−15, 2], "
                "logit α ∈ [−6, 4], β ∈ [0.01, 5] per hour, starting from μ = 0.5·n/T, α = 0.5, β = 0.2. The fit is used only if "
                "2·(ℓ_Hawkes − ℓ_Poisson) > 5.99 (χ² with 2 degrees of freedom at 5%) and α > 0.02. Simulation: 4,000 paths, "
                "λ₀ = μ + αβ·Σ_i exp(−β(T − t_i)) at the last bar; each hourly step adds a draw from the baseline residual pool with "
                "|z| > 3.5 removed times the step's baseline σ, plus, with probability 1 − exp(−λ·frac) for a step covering frac of "
                "an hour, one jump equal to a randomly chosen historic jump's signed z times today's slow volatility (median |rd| of "
                "the last 720 hours / 0.6745) times the hour-of-day multiplier; then λ ← μ + (λ − μ)·exp(−β·frac) + αβ if a jump "
                "fired. from_samples on the summed log moves. A high current intensity fattens both tails; the sign mix of the "
                "historic jumps sets which tail more.",
          trades="After a burst of big moves it puts more weight than the market on the far ranges on both sides, roughly beyond 2σ for a "
                 "close within a day, lopsided the way the recent jumps were, and buys those, never the body. The extra tail weight fades "
                 "with the intensity at rate β per hour. With too few jumps, or a fit no better than Poisson, the picture is the baseline "
                 "and the bot buys nothing.",
          pipeline="paths"),
    Model("stochastic_vol_paths", "Stochastic Volatility", FAMILY, "Volatility wanders on every path: some turn calm, some stormy",
          "Volatility has a life of its own. Instead of freezing today's volatility all the way to the close, each simulated path "
          "lets its log-variance wander: pulled back towards a long-run level at the speed seen in the bars, shocked by its own "
          "noise and, when history supports it, pushed up when the price falls (the leverage effect). Some paths turn calm and "
          "others stormy, so a picture days ahead has fatter tails and a sharper centre than a single-volatility bell, and a "
          "sell-off within a path tends to widen that path's later moves.",
          stochastic_vol_paths, factors=_SHAPED,
          reference="Stochastic volatility with leverage (Heston 1993; log-variance form after Taylor 1986)",
          inputs="Hourly closes: the EWMA (λ = 0.94) variance of deseasonalised hourly returns per bar, sampled every 24th bar as a "
                 "daily log-variance series, and the standardised residuals summed over the same 24-hour blocks. It needs 60 daily "
                 "samples (about 1,417 bars); with fewer, a persistence outside (0, 0.999) or a zero volatility of volatility, the "
                 "picture is the baseline. Refitted once per bar.",
          maths="Daily log variance h_d follows an AR(1) fitted by OLS: h_d+1 = c + φ·h_d + e_d, long-run level θ = c/(1 − φ). "
                "Converted to hours: φ₁ = φ^(1/24), κ = 1 − φ₁ per hour, ξ₁ = sd(e)·√((1 − φ₁²)/(1 − φ²)) per √hour. Leverage ρ = "
                "correlation of the daily standardised return (24 hourly residuals summed, over √24) with e over the m aligned days; "
                "kept only if m > 30 and |ρ|·√(m − 2)/√(1 − ρ²) > 2, then clipped to ±0.9, else 0. Simulation: 4,000 paths starting "
                "at h = ln s²_next, the volatility clock's next-hour deseasonalised variance; each step j covering frac of an hour "
                "with hour-of-day multiplier p: x += ε·p·√frac·exp(h/2) with ε from the baseline residual pool, then "
                "h += κ·frac·(θ − h) + ξ₁·√frac·(ρ·ε + √(1 − ρ²)·η), η standard normal. from_samples on x. κ, θ, ξ₁ and ρ are all "
                "estimated; the lab's Heston form is replaced by this log-variance form. Today's variance below exp(θ) widens the "
                "picture towards the long-run level, above it narrows it.",
          trades="For a close days out it puts more weight than the market on the far ranges on both sides and on the ranges nearest spot, "
                 "less in between, and buys those tails and that centre; with ρ < 0 the downside tail gets more. Today's volatility below "
                 "its long-run level makes the whole picture wider and the tails what it buys; above it, narrower and the centre. Within a "
                 "few hours of the close it buys little.",
          pipeline="paths"),
    Model("fbm_hurst_paths", "Fractional Memory (Hurst)", FAMILY, "Measures whether moves persist or undo themselves",
          "Does Bitcoin have memory? The Hurst exponent H says: 0.5 is a memoryless random walk, above 0.5 moves tend to persist "
          "(a run feeds on itself, so the spread of a close days out grows faster than the square root of time and the picture fans "
          "out wider), below 0.5 they tend to undo themselves (the picture stays tighter). The model measures H on its hourly "
          "residuals, checks it against shuffled copies of the same residuals to see whether the memory is real, shrinks it towards "
          "0.5 accordingly, and simulates fractionally correlated paths built from Bitcoin's own fat-tailed moves.",
          fbm_hurst_paths, factors=_SHAPED,
          reference="Fractional Brownian motion (Mandelbrot and Van Ness 1968); detrended fluctuation analysis (Peng et al. 1994)",
          inputs="Hourly closes: the standardised hourly residuals (deseasonalised returns over their EWMA volatility forecast) of "
                 "the last 4,096 hours, at least 512 of them (the lab uses 24 weeks; the terminal the twelve or so it has), otherwise "
                 "the baseline. Refitted once per bar.",
          maths="Detrended fluctuation analysis: the profile y = cumulative sum of z − mean(z) is cut into segments of length s for 14 "
                "log-spaced scales from 8 to max(16, n/8) hours (a scale needs at least 4 segments), each segment is linearly detrended, "
                "F(s) is the root-mean-square residual and H is the slope of ln F on ln s (fewer than 3 usable scales gives 0.5). The null "
                "is the same statistic on 12 random shuffles of z (seed 7, so the fit is reproducible): d = H − mean(null), t = "
                "d/max(sd(null), 10⁻³), shrunk H = 0.5 + d·max(0, 1 − 4/t²), clipped to [0.2, 0.8], used only if it differs from 0.5 by "
                "more than 0.005 (so |t| > 2). Simulation: 4,000 paths of fractional Gaussian noise over the n hourly steps, covariance "
                "γ(k) = ½(|k + 1|^2H − 2|k|^2H + |k − 1|^2H), by Cholesky; each Gaussian is mapped through the normal CDF onto the sorted "
                "empirical residual pool (a Gaussian copula with Bitcoin's own marginal), the draws are rescaled to unit standard "
                "deviation, multiplied by each step's baseline σ and summed, so a close n hours out has about n^(H − ½) times the "
                "baseline's spread; from_samples on the sums. H above 0.5 widens the picture, below 0.5 narrows it.",
          trades="With H above 0.5 the picture is wider than the baseline for a close more than a few hours out, growing with the horizon, "
                 "so it puts more weight than the market on the ranges beyond about ±1σ on both sides and buys those, never the centre; "
                 "with H below 0.5 it is tighter and buys the ranges within about ±1σ of spot. When the shuffle test cannot tell H from 0.5 "
                 "the picture is the baseline and the bot buys nothing.",
          pipeline="paths"),
    Model("regime_switching_paths", "Calm and Storm Regimes", FAMILY, "Two moods, calm and stormy, and the odds of switching",
          "Two moods, calm and stormy, with no direction in either. A two-state Markov-switching model learns from the last 2,000 "
          "hours how volatile each mood is, how long each tends to last and how likely the market is to be in each right now. "
          "Every simulated path starts in a mood drawn from those odds and can switch hour by hour, so the picture is a blend: "
          "judged calm, it stays tight but keeps a tail for a storm arriving; judged stormy, it is wide but allows for things "
          "settling down. The regimes are used only when their volatilities clearly differ.",
          regime_switching_paths, factors=_SHAPED, reference="Markov-switching model (Hamilton 1989)",
          inputs="Hourly closes: the last 2,000 deseasonalised hourly log returns (hour-of-day profile removed), in percent. The fit "
                 "needs at least 100 of them; the picture is the baseline unless the stormy volatility exceeds the calm one by more "
                 "than 30% and both stay probabilities are below 0.9999. Refitted once per bar.",
          maths="Two zero-mean Gaussian states with variances v_calm < v_storm and stay probabilities p_cc, p_ss, fitted by EM "
                "(Hamilton filter, Kim smoother, at most 40 iterations, stopped at a log-likelihood gain under 10⁻⁶ per observation; "
                "starting variances 0.5 and 2 times the sample variance, stay probabilities 0.95, variances floored at 10⁻⁴ of the "
                "sample variance). p_storm is the filtered probability of the storm state at the last bar. Simulation: 4,000 paths; "
                "each starts stormy with probability p_storm; each step j adds ε·p_j·√frac_j·σ_state with ε from the baseline "
                "residual pool, p_j the hour-of-day multiplier, frac_j the share of the hour and σ_state the fitted hourly volatility "
                "of the current state in return units, so the width comes from the regime fit, not from the EWMA volatility clock; "
                "after each step the state flips with probability 1 − p_stay of its state, unscaled for a partial hour. from_samples "
                "on the summed moves. Terminal version: the lab fits the same model with statsmodels; here the parameters come from "
                "the terminal's own EM. A high p_storm widens the picture, a low one narrows its body and leaves a storm tail.",
          trades="Judged calm, the picture is tighter in the body but keeps a storm tail, so it buys the ranges near spot and, for a close "
                 "days out, the far ranges beyond about 2σ, thinning the ranges between. Judged stormy it is wide and buys the tails, not "
                 "the centre. Its overall width follows the fitted regime volatilities, not the market's clock, so it can also be wider or "
                 "narrower than the baseline throughout. With the two volatilities within 30% of each other the picture is the baseline and "
                 "the bot buys nothing.",
          pipeline="paths"),
    Model("garch_gjr_skewt", "GARCH-skew", "GARCH", "Knows that falls are faster than rallies",
          "Volatility clusters, and a fall raises the next hour's volatility more than a rise of the same size. GJR-GARCH(1,1) "
          "writes each hour's variance as a constant plus a share of the last squared return, a larger share when that return was "
          "negative, plus a share of the last variance, so it decays from today's level towards a long-run level on its own clock. "
          "The picture comes from simulated paths that carry that variance forward, with innovations drawn from the model's own "
          "past standardised residuals, so skew and fat tails come from history rather than from a fitted distribution.",
          garch_gjr_skewt, factors=("volatility", "tails"),
          reference="GJR-GARCH(1,1) (Glosten, Jagannathan, Runkle 1993); filtered historical simulation (Barone-Adesi et al. 1999)",
          inputs="Hourly closes: deseasonalised hourly log returns over the last 8,760 hours (a year; the terminal has about twelve "
                 "weeks, so all of them). It needs at least 500 finite returns and a fit that converges, otherwise it draws no picture "
                 "and the runner skips it. Refitted once per bar.",
          maths="Returns y are scaled to unit variance; h_t = ω + (α + γ·1[y_t−1 < 0])·y²_t−1 + β·h_t−1 with h_0 the mean of the "
                "first 100 squared returns and ω = 1 − α − γ/2 − β (variance targeting: the long-run variance is the sample "
                "variance). Gaussian quasi-maximum likelihood minimises ½Σ(ln h_t + y²_t/h_t) by L-BFGS-B with α ∈ [0, 0.5], "
                "γ ∈ [0, 0.7], β ∈ [0, 0.999] and α + γ/2 + β < 0.999, from three starts (0.03, 0.04, 0.92), (0.08, 0.05, 0.80), "
                "(0.02, 0.02, 0.95), keeping the best. Standardised residuals z_t = y_t/√h_t (the first 24 dropped when more than 524) form the "
                "innovation pool; h_next is the recursion applied to the last return, scaled back to return units. Simulation: 4,000 "
                "paths from h = h_next; each step draws e = z·√h from the pool, adds e times the hour-of-day multiplier times √(share "
                "of the hour) to the path, then h ← ω + (α + γ·1[e < 0])·e² + β·h on whole deseasonalised hours. from_samples on the "
                "summed moves. Terminal version: the lab fits skewed Student-t innovations with the arch package; here the "
                "innovations are the empirical residuals (filtered historical simulation). A high h_next widens the picture, a "
                "persistence near one keeps it wide for far-out closes.",
          trades="Its width follows the GARCH variance, decaying from h_next at rate α + γ/2 + β towards the sample variance, rather than "
                 "the EWMA clock. After a shock it is wider than the baseline for a close within a day or two and buys the ranges beyond "
                 "about ±1σ, more on the downside because γ > 0 and the residuals are skewed; when today's variance sits above the sample "
                 "average and the close is far out it narrows and buys the ranges near spot. The centre stays on spot.",
          pipeline="paths"),
]
