"""The calendar in the clocks: the zoo's shared Scale and the Session Walk's Clock both know the quiet hours, CME's closed
hours for gold and oil and the weekend for Bitcoin, and every picture narrows through them. Offline, synthetic bars."""
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from zoo_contract import ctx_for

from glimpse_tui import clock
from glimpse_tui.zoo import core as C


def at(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()


def bars_with_quiet(session: str, busy: float = 0.004, quiet: float = 0.002, n: int = 2000, seed: int = 11) -> pd.DataFrame:
    """Hourly bars whose quiet hours (by `session`) move at `quiet` and the rest at `busy`, plus an afternoon bulge."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-08-01", periods=n, freq="h", tz="UTC")
    still = np.array([clock.quiet_at(session, t.timestamp()) for t in idx])
    bulge = np.where(np.isin(idx.hour, (13, 14, 15)), 1.4, 0.95)
    r = rng.standard_t(5, n) / np.sqrt(5 / 3) * np.where(still, quiet, busy) * bulge
    close = 78_000 * np.exp(np.cumsum(r))
    return pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1.0}, index=idx)


def test_the_calendar_names_the_quiet_hours_and_the_ratio_is_measured_with_guards():
    assert clock.session_of("XAU") == clock.session_of("WTIBTC") == clock.session_of("xaut") == "cme"
    assert clock.session_of("BTC") == clock.session_of("ETH") == "always"
    assert clock.quiet_at("always", at("2026-10-31 12:00")) and clock.quiet_at("always", at("2026-11-01 23:30"))    # Saturday, Sunday night
    assert not clock.quiet_at("always", at("2026-10-30 23:00")) and not clock.quiet_at("always", at("2026-11-02 00:30"))
    assert clock.quiet_at("cme", at("2026-10-30 23:00"))                                           # CME: Friday night off
    assert not clock.quiet_at("cme", at("2026-11-01 23:30"))                                       # and on again Sunday 23:00
    rng = np.random.default_rng(1)
    busy, still = rng.normal(0, 1, 1000), rng.normal(0, 0.5, 400)
    assert clock.quiet_ratio(busy, still) == pytest.approx(0.5, abs=0.06)
    assert clock.quiet_ratio(busy, rng.normal(0, 1.1, 400)) == 1.0                                  # never above one
    assert clock.quiet_ratio(busy, rng.normal(0, 0.96, 400)) == 1.0                                 # a lull under 10% is noise
    assert clock.quiet_ratio(busy, still[:50], "cme") == 0.3 and clock.quiet_ratio(busy, still[:50], "always") == 1.0   # too few: the default


def test_the_shared_scale_fits_a_quiet_sigma_and_narrows_every_picture_through_the_weekend():
    sc = C.Scale.fit(bars_with_quiet("always"), asset="BTC")
    assert sc.session == "always" and 0.4 < sc.quiet < 0.62
    assert 1.2 < sc.profile[14] / sc.profile[3] < 2.2                                               # the profile is read from busy hours
    sat, wed = at("2026-10-31 12:00"), at("2026-10-28 12:00")
    v_sat, v_wed = sc.step_variances(sat, sat + 6 * 3600).sum(), sc.step_variances(wed, wed + 6 * 3600).sum()
    assert v_sat == pytest.approx(v_wed * sc.quiet**2, rel=1e-9)
    assert sc.hours_quiet(at("2026-10-30 20:00"), at("2026-11-02 04:00")) == pytest.approx(48.0)  # Friday 20:00 to Monday 04:00
    gold = C.Scale.fit(bars_with_quiet("cme", 0.0025, 0.0007), asset="XAU")
    assert gold.session == "cme" and 0.2 < gold.quiet < 0.4
    fri = at("2026-10-30 21:00")
    assert gold.hours_quiet(fri, fri + 4 * 3600) == pytest.approx(3.0)                              # 21:00 open, then CME shuts at 22:00
    flat = C.Scale.fit(bars_with_quiet("always", 0.004, 0.004), asset="BTC")
    assert flat.quiet == 1.0 and flat.step_variances(sat, sat + 3600).sum() == pytest.approx(flat.step_variances(wed, wed + 3600).sum(), rel=0.3)


def test_models_drawn_over_a_weekend_are_narrower_and_the_calendar_reaches_them_through_the_context():
    from glimpse_tui import zoo

    bars = bars_with_quiet("always")
    end_sat = at("2026-11-01 12:00")                                                                # Sunday noon: the hours before are quiet
    end_wed = at("2026-10-28 12:00")
    now_sat, now_wed = end_sat - 24 * 3600, end_wed - 24 * 3600
    c_sat = C.Ctx(bars=bars, spot=78_000.0, edges=28_000.0 + 200.0 * np.arange(501), now=now_sat, end=end_sat, asset="BTC")
    c_wed = C.Ctx(bars=bars, spot=78_000.0, edges=28_000.0 + 200.0 * np.arange(501), now=now_wed, end=end_wed, asset="BTC")
    assert c_sat.scale.quiet == c_wed.scale.quiet < 0.62 and c_sat.sigma == pytest.approx(c_wed.sigma * c_sat.scale.quiet, rel=0.05)
    for mid in ("rw_student_t", "garch_gjr_skewt", "ema_crossover", "dist_student_t", "session_walk"):
        m = zoo.get(mid)
        p_sat, p_wed = np.asarray(m.fn(c_sat)), np.asarray(m.fn(c_wed))
        width = lambda p: float(np.sum(p * (np.arange(500) - np.dot(p, np.arange(500))) ** 2)) ** 0.5   # noqa: E731
        assert width(p_sat) < 0.8 * width(p_wed), mid
    # the zoo contract's synthetic bars have no calendar, so the clock stays as it was
    ctx = ctx_for(hours=6.0)
    assert ctx.scale.quiet == 1.0 and ctx.scale.session == "always"
