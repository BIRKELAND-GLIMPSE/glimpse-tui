"""Baselines: the pictures every other model has to beat."""
from __future__ import annotations

import numpy as np

from .core import Ctx, Model, base_pdf, from_samples, rng_for, t_pdf, to_bins


def rw_student_t(ctx: Ctx) -> np.ndarray:
    return to_bins(ctx, base_pdf(ctx))


def rw_bootstrap(ctx: Ctx, n_paths: int = 4000) -> np.ndarray:
    """Filtered historical simulation: past standardised hours, redrawn for every step to the close and rescaled
    by today's volatility and the hour-of-day profile."""
    rng = rng_for(ctx, "rw_bootstrap")
    sd = np.sqrt(ctx.scale.step_variances(ctx.now, ctx.end))
    pool = ctx.scale.z_pool / max(float(np.std(ctx.scale.z_pool)), 1e-12)
    draws = pool[rng.integers(0, pool.size, size=(n_paths, sd.size))]
    return from_samples(ctx, (draws * sd).sum(axis=1))


def rw_normal(ctx: Ctx) -> np.ndarray:
    return to_bins(ctx, t_pdf(200.0))


MODELS = [
    Model("rw_bootstrap", "Random Walk (bootstrap)", "Baseline", "History's own hourly moves, replayed at today's volatility",
          "Filtered historical simulation: rather than assume a shape for the move to the close, replay the market's own "
          "past hours. Each past hourly move is stripped of its time-of-day and volatility-regime scale to leave a "
          "standardised residual, and the picture is built by redrawing those residuals for every hour to the close and "
          "rescaling them to today's volatility. No view on direction, width or shape beyond what history's residuals "
          "carry: this is the picture every other model must beat.",
          rw_bootstrap, factors=("randomness",),
          inputs="Hourly closes only, through the shared volatility clock: the pool of standardised hourly residuals of the "
                 "loaded candles (the first 24 dropped when there are more than 48), the next-hour EWMA variance and the "
                 "hour-of-day profile, plus the time to the close. The 400-bar minimum and nothing more; there is no "
                 "signal and so no history requirement.",
          maths="Let ẑ be the clock's residual pool divided by its own standard deviation (unit variance) and σ_j the "
                "baseline standard deviation of hourly step j from now to the close (partial first and last steps). "
                "4,000 paths: for each path and each step draw one ẑ uniformly with replacement and multiply by σ_j; the "
                "simulated log move is Σ_j ẑ_j·σ_j. The paths go to from_samples (199 quantiles, linear tails to the most "
                "extreme path); the generator is seeded by rng_for with id rw_bootstrap. No drift, tilt or width "
                "multiplier: the picture's variance is the clock's σ² and its shape is the convolution of the residuals. "
                "There is no reading and no lean; the centre stays on spot.",
          trades="Centred on spot at the clock's width, it differs from the market only in shape: it buys the ranges the "
                 "market prices below the replayed history, typically the centre when the market's ladder is wider than "
                 "the clock and the shoulders and tails when it is narrower. Beyond the 0.5% and 99.5% quantiles the "
                 "tails are straight lines to the most extreme of 4,000 paths, so it rarely buys far-tail ranges. A "
                 "market priced at the clock's own shape gets no trades.",
          pipeline="paths"),
    Model("rw_student_t", "Random Walk (Student-t)", "Baseline", "No direction, fat tails fitted to Bitcoin's hours",
          "A driftless random walk whose hourly shock has fat tails fitted to Bitcoin's own hours. Each hour's "
          "standardised move is Student-t with degrees of freedom estimated from the loaded candles, and the move to the "
          "close is the sum of those hourly shocks, so its tails thin towards a bell curve as the horizon lengthens. No "
          "view on direction: this is the baseline itself, the density every conviction model bends.",
          rw_student_t, factors=("randomness", "tails"),
          inputs="Hourly closes only, through the shared volatility clock: the standardised residual pool for the "
                 "degrees-of-freedom fit (ν = 5 is assumed with fewer than 20 residuals), the next-hour EWMA variance and "
                 "hour-of-day profile for σ, and the number of hourly steps to the close. The 400-bar minimum and nothing "
                 "more.",
          maths="ν is the maximum-likelihood degrees of freedom of a unit-variance Student-t fitted to the clock's "
                "standardised residuals (rescaled to unit standard deviation), bounded to [2.5, 200]. With n the number "
                "of hourly steps to the close (partial steps count), the one-hour excess kurtosis is k₁ = 6/(ν − 4), "
                "capped at 12 (a fitted ν ≤ 4.5 is treated as 4.5), and the n-step degrees of freedom are "
                "ν_n = 4 + 6·n/k₁ = 4 + n·(ν − 4), at most 6004. The picture is the unit-variance Student-t with ν_n on "
                "the grid, integrated to the bins with no shift and unit width (mean_z = 0, vol_mult = 1). Terminal "
                "version: the lab bootstraps the hourly shocks; here the thinning of the tails with n is the closed "
                "form, 1/n of the excess kurtosis. There is no reading and no lean; the centre stays on spot.",
          trades="Centred on spot at the clock's width. Against a market priced as a bell curve it puts more weight than "
                 "the market on the far ranges beyond about 2.5σ on both sides and on the centre within about 0.5σ, and "
                 "buys those; it thins the shoulders and does not buy them. Against a market wider than the clock it "
                 "buys the centre, narrower the tails; a market priced at the same Student-t gets no trades. The tails "
                 "are fattest, and tail trades likeliest, when few hours remain to the close.",
          pipeline="baseline"),
    Model("rw_normal", "Random Walk (bell curve)", "Baseline", "The textbook lognormal, at today's volatility",
          "The textbook picture: a driftless lognormal at today's volatility, a bell curve for the log move to the "
          "close. It believes nothing about direction and ignores the fat tails Bitcoin's hours actually show, so it is "
          "the yardstick for what the fitted tails are worth. Terminal only: it replaces the old built-in lognormal bot.",
          rw_normal, factors=("randomness",),
          inputs="Hourly closes only, through the shared volatility clock: the next-hour EWMA variance and hour-of-day "
                 "profile that set σ to the close. It uses neither the fitted degrees of freedom nor the residual pool. "
                 "The 400-bar minimum and nothing more.",
          maths="The standardised move z is a unit-variance Student-t with ν = 200, a Gaussian to within a fraction of "
                "a percent (excess kurtosis 6/196 ≈ 0.03), placed on the grid with no shift and unit width (mean_z = 0, "
                "vol_mult = 1) and integrated between the price edges: the close is lognormal with median spot and log "
                "standard deviation σ. Nothing is fitted beyond σ; the shape is assumed and does not change with the "
                "hours to the close. There is no reading and no lean; the centre stays on spot.",
          trades="Centred on spot at the clock's width with thin tails. Against a market carrying Bitcoin's fat tails it "
                 "puts more weight than the market on the shoulders, roughly 0.75σ to 2.5σ either side of spot, and buys "
                 "those; it thins the far tails and the centre within about 0.5σ and buys neither. Against a market "
                 "priced as a bell curve at the same σ it buys nothing.",
          pipeline="baseline"),
]
