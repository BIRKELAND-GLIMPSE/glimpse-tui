"""Offline. Pricing is pinned to server estimates recorded live on 2026-09-17."""
import json
import stat
from pathlib import Path

import pytest

from glimpse_tui import auth, bots, fmt
from glimpse_tui import pricing as P
from glimpse_tui.api import Book, parse_bin

FIX = json.loads((Path(__file__).parent / "fixtures" / "estimates.json").read_text())


@pytest.fixture
def book() -> Book:
    b = Book(topic_id=FIX["topic_id"], title="t", end_time_utc=2**31, quote_mode="live", volume_msat=0,
             option_ids=list(range(1, len(FIX["shares"]) + 1)), bins=[parse_bin(n) for n in FIX["names"]],
             shares=FIX["shares"], alpha=P.alpha_for(len(FIX["shares"])))
    b.reprice()
    return b


@pytest.mark.parametrize("case", FIX["cases"])
def test_ticket_matches_server_estimate(book, case):
    t = P.ticket(book.shares, book.alpha, case["lo"], case["hi"], case["contracts"])
    assert t.cost_sats - t.fee_sats == pytest.approx(case["cost_sats"], abs=0.002)
    assert t.fee_sats == pytest.approx(max(case["fee_sats"], P.MIN_FEE_SATS), abs=0.002)   # the enter route floors the fee at 1 sat


def test_small_tickets_are_allowed_and_pay_the_one_sat_fee(book):
    t = P.ticket(book.shares, book.alpha, 74, 74, 1)
    assert 0 < t.cost_sats < 100 and t.fee_sats == P.MIN_FEE_SATS
    assert not hasattr(t, "below_minimum")


def test_ticket_economics(book):
    t = P.ticket(book.shares, book.alpha, 74, 78, 10)
    assert t.payout_sats == 980                      # one bin wins, net of the 2% settlement fee
    assert t.roi == pytest.approx(t.odds - 1)
    assert t.breakeven_prob == pytest.approx(t.cost_sats / 980)
    assert P.ticket(book.shares, book.alpha, 78, 74, 10) == t    # selection direction is irrelevant


def test_budget_inverts_cost(book):
    n = P.contracts_for_budget(book.shares, book.alpha, 74, 78, 500)
    assert P.ticket(book.shares, book.alpha, 74, 78, n).cost_sats <= 500
    assert P.ticket(book.shares, book.alpha, 74, 78, n + 0.02).cost_sats > 500


def test_distribution_summary(book):
    assert sum(book.probs) == pytest.approx(1)
    sig = P.signal(book.probs)
    lo, hi = P.hdi(sig)
    assert sum(sig[lo:hi + 1]) >= 0.8
    assert book.bins[lo][0] <= P.quantile(book.bins, sig, 0.5) <= book.bins[hi][1]
    assert hi - lo < 60                              # the raw, floor-heavy distribution would span hundreds of bins


def test_no_overflow_when_one_bin_dominates():
    q = [40.0] * 500
    q[10] = 1e6
    assert P.prices(q, P.alpha_for(500))[10] > 99


def test_bot_respects_budget_and_edge(book):
    probs = [1e-6] * len(book.bins)
    probs[90] = 1 - 1e-6 * (len(probs) - 1)          # certain of a bin the market prices near zero
    legs = bots.decide(book, probs, bots.BotConfig(bankroll_sats=1e9, kelly=1.0), budget_sats=300)
    assert [x.index for x in legs] == [90]
    assert sum(x.cost_sats for x in legs) <= 300
    assert bots.decide(book, book.probs, bots.BotConfig(), 300) == []   # agreeing with the market is never an edge


def test_key_file_fallback_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(auth.ENV_VAR, raising=False)
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    key = "glp_live_" + "ab" * 32
    assert auth.save_key(f"  {key}\n") == "file"
    f = tmp_path / "glimpse" / "credentials"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    f.chmod(0o644)
    assert auth.load_key() == (key, "file")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600   # loosened permissions are repaired on read
    monkeypatch.setenv(auth.ENV_VAR, "glp_live_fromenv_0123456789")
    assert auth.load_key()[1] == "env"
    auth.forget_key()
    assert not f.exists()


def test_mask_never_reveals_the_key():
    key = "glp_live_" + "cd" * 32
    assert auth.mask(key) == "glp_live_…cdcd"
    assert auth.mask("short") == "…" and auth.mask(None) == "-"


def test_formatting():
    assert fmt.sats(1234.4) == "₿1,234" and fmt.sats(-5, signed=True) == "−₿5" and fmt.sats(5, signed=True) == "+₿5"
    assert fmt.odds(1.188) == "1.19×" and fmt.odds(12.64) == "12.6×" and fmt.odds(480.2) == "480×"
    assert fmt.pct(0.608) == "60.8%" and fmt.pct(0.0025) == "0.25%" and fmt.pct(1e-7) == "<0.01%"
    assert fmt.roi(5.43) == "+543%" and fmt.roi(-0.081) == "−8.1%"
    assert fmt.countdown(3600 * 27 + 60, now=0) == "1d 3h" and fmt.countdown(95, now=0) == "1m 35s"
    assert fmt.countdown(0, now=5) == "closed"
    assert fmt.bar(0.5, 10) == "█████" and fmt.bar(0, 10) == ""
