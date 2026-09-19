"""Bitcoin arithmetic the pages compute locally: the issuance schedule, supply from the tip, the halving
countdown, hashprice, and transaction sizes by script type. Pure functions, no network."""
from __future__ import annotations

from dataclasses import dataclass

HALVING_INTERVAL = 210_000
RETARGET_INTERVAL = 2016
COIN = 100_000_000
TARGET_SPACING_S = 600
MAX_SUPPLY_BTC = 20_999_999.9769


def subsidy_sats(height: int) -> int:
    """The block subsidy at `height`: 50 BTC halved every 210,000 blocks, in whole sats, zero after 64 halvings."""
    era = height // HALVING_INTERVAL
    return 0 if era >= 64 else (50 * COIN) >> era


def issued_sats(height: int) -> int:
    """The protocol maximum issued through block `height` inclusive. Some of it is provably unspendable (the genesis
    coinbase, lost coinbases, OP_RETURN burns), so the spendable supply is slightly lower: panels label this."""
    blocks, total, era = height + 1, 0, 0
    while blocks > 0 and era < 64:
        n = min(blocks, HALVING_INTERVAL)
        total += n * ((50 * COIN) >> era)
        blocks -= n
        era += 1
    return total


@dataclass(frozen=True)
class Halving:
    era: int                    # 0 for the 50 BTC era; 4 since April 2024
    next_height: int
    blocks_left: int
    subsidy_now: int            # sats
    subsidy_next: int
    est_seconds: float          # at the ten-minute target, or the observed spacing when given


def halving(height: int, spacing_s: float = TARGET_SPACING_S) -> Halving:
    era = height // HALVING_INTERVAL
    nxt = (era + 1) * HALVING_INTERVAL
    left = nxt - height
    return Halving(era, nxt, left, subsidy_sats(height), subsidy_sats(nxt), left * spacing_s)


def annual_inflation(height: int, blocks_per_day: float = 144.0) -> float:
    """New issuance over a year at the current subsidy, over what has been issued."""
    return subsidy_sats(height) * blocks_per_day * 365.25 / issued_sats(height)


def hashprice(subsidy_sats_: float, mean_fees_sats: float, blocks_per_day: float, price_usd: float, hashrate_hs: float
              ) -> tuple[float, float]:
    """Miner revenue per PH/s per day as (USD, sats): (subsidy + mean fees per block) × blocks per day ÷ hashrate.
    `blocks_per_day` is the observed rate over the window, not the 144 target (TERMINAL.md 5)."""
    if hashrate_hs <= 0:
        return 0.0, 0.0
    sats_per_ph_day = (subsidy_sats_ + mean_fees_sats) * blocks_per_day / (hashrate_hs / 1e15)
    return sats_per_ph_day / COIN * price_usd, sats_per_ph_day


# Virtual sizes in vB, from the script templates (BIP 141 weights; signatures at their usual 72 or 64 bytes).
INPUT_VB = {"p2pkh": 148.0, "p2sh-p2wpkh": 91.0, "p2wpkh": 68.0, "p2wsh-2of3": 104.5, "p2tr": 57.5}
OUTPUT_VB = {"p2pkh": 34.0, "p2sh": 32.0, "p2wpkh": 31.0, "p2wsh": 43.0, "p2tr": 43.0}
OVERHEAD_VB = 10.5              # version, locktime, counts, and the segwit marker and flag


def tx_vsize(inputs: int, outputs: int, in_type: str = "p2wpkh", out_type: str = "p2wpkh") -> float:
    """Virtual size of a transaction of N inputs and M outputs of one script type each."""
    legacy = in_type == "p2pkh"
    return (10.0 if legacy else OVERHEAD_VB) + inputs * INPUT_VB[in_type] + outputs * OUTPUT_VB[out_type]


def fee_sats(vsize: float, rate: float) -> int:
    return int(-(-vsize * rate // 1))           # ceil: a node rounds the fee it requires up


Z80 = 1.2815515655446004        # an 80% band is ±1.2816σ of a normal
YEAR_S = 365.25 * 86400


def implied_vol(median: float, lo: float, hi: float, seconds_to_close: float) -> float | None:
    """Annualised volatility implied by a close's 80% band, read as a lognormal: σ of log price over the time left,
    scaled to a year. The same reading the heatmap's readout uses (PLAN D11g)."""
    import math
    if not (median > 0 and hi > lo > 0 and seconds_to_close > 0):
        return None
    sigma = math.log(hi / lo) / (2 * Z80)
    return sigma * math.sqrt(YEAR_S / seconds_to_close)


def kelly(p: float, odds: float) -> float:
    """The Kelly fraction of bankroll for a bet won with probability `p` that pays `odds` times the stake in total
    (decimal odds, stake included). Negative means do not bet."""
    b = odds - 1.0
    return (p * b - (1.0 - p)) / b if b > 0 else -1.0
