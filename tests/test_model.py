"""NBM distribution and walk-forward calibration (synthetic data, no network)."""

import math
import random
from datetime import date, timedelta

import numpy as np
import pytest

from weatherbot.model.calibrate import NEW, OLD, Calibrator, TrainRow, family_of, fit
from weatherbot.model.nbm_dist import (
    PROBS,
    Forecast,
    Z,
    adjusted_cdf,
    knots_from_rows,
    prob_of_high,
    raw_cdf,
)


def normal_knots(mu: float, sd: float) -> np.ndarray:
    return mu + sd * Z


def test_cdf_passes_through_the_percentiles():
    k = np.array([[60.0, 63.0, 65.0, 67.0, 71.0]])  # skewed
    for x, p in zip(k[0], PROBS, strict=True):
        assert raw_cdf(k, np.array([x]))[0] == pytest.approx(p, abs=1e-6)


def test_cdf_matches_a_normal_when_nbm_is_normal():
    k = normal_knots(70, 2)[None, :]
    for x in (64.0, 69.0, 70.0, 72.5, 77.0):
        expected = 0.5 * (1 + math.erf((x - 70) / (2 * math.sqrt(2))))
        assert raw_cdf(k, np.array([x]))[0] == pytest.approx(expected, abs=2e-3)


def test_cdf_is_monotone_and_ties_are_handled():
    k = knots_from_rows([{"p10_f": 60, "p25_f": 60, "p50_f": 61, "p75_f": 61, "p90_f": 63}])
    xs = np.linspace(50, 75, 200)
    c = raw_cdf(np.repeat(k, len(xs), axis=0), xs)
    assert np.all(np.diff(c) >= -1e-12) and c[0] < 0.01 and c[-1] > 0.99


def test_missing_quartiles_are_filled():
    k = knots_from_rows([{"p10_f": 60, "p50_f": 65, "p90_f": 70}])
    assert k[0, 1] == pytest.approx(65 - 5 * 0.6745 / 1.2816, abs=1e-3)


def test_shift_and_stretch():
    k = normal_knots(70, 2)[None, :]
    assert adjusted_cdf(k, np.array([71.5]), 1.5, 1.0)[0] == pytest.approx(0.5, abs=1e-6)
    # Stretching by 2 makes the 90th percentile twice as far from the median.
    x90 = 70 + 2 * 2 * Z[4]
    assert adjusted_cdf(k, np.array([x90]), 0.0, 2.0)[0] == pytest.approx(0.9, abs=1e-6)


def test_forecast_pmf_sums_to_one_and_centres_on_median():
    pmf = Forecast(normal_knots(70, 2), b=1.0, s=1.2).pmf()
    assert sum(pmf.values()) == pytest.approx(1.0)
    assert max(pmf, key=pmf.get) == 71


def _synthetic(n: int, bias: float, stretch: float, seed: int = 1, start=date(2025, 1, 1)):
    # Python's random, not numpy.random: some Windows security policies block
    # numpy.random's compiled files, and the bot itself never needs them.
    rng = random.Random(seed)
    mus = np.array([60 + 15 * rng.random() for _ in range(n)])
    sds = np.array([1.5 + rng.random() for _ in range(n)])
    knots = np.stack([normal_knots(m, s) for m, s in zip(mus, sds, strict=True)])
    noise = np.array([rng.gauss(0.0, 1.0) for _ in range(n)])
    highs = np.rint(mus + bias + stretch * sds * noise)
    dates = [start + timedelta(days=i) for i in range(n)]
    return knots, highs, dates


def test_fit_recovers_known_bias_and_stretch():
    knots, highs, _ = _synthetic(3000, bias=1.5, stretch=1.3)
    p = fit(knots, highs, np.ones(len(highs)))
    assert p.b == pytest.approx(1.5, abs=0.15)
    assert p.s == pytest.approx(1.3, abs=0.08)
    raw = np.log(prob_of_high(knots, highs, 0, 1)).mean()
    cal = np.log(prob_of_high(knots, highs, p.b, p.s)).mean()
    assert cal > raw


def _rows(station, family, knots, highs, dates):
    return [
        TrainRow(station, d, family, k, int(h)) for k, h, d in zip(knots, highs, dates, strict=True)
    ]


def test_future_days_never_leak_into_a_fit():
    k1, h1, d1 = _synthetic(400, bias=0.0, stretch=1.0, start=date(2025, 1, 1))
    # After Feb 2026, the truth jumps 5 degrees. A fit for Feb 2026 must not see it.
    k2, h2, d2 = _synthetic(200, bias=5.0, stretch=1.0, seed=2, start=date(2026, 2, 1))
    cal = Calibrator(_rows("KAAA", OLD, k1, h1, d1) + _rows("KAAA", OLD, k2, h2, d2))
    feb = cal.params_for("KAAA", OLD, date(2026, 2, 1))
    assert abs(feb.b) < 0.5
    may = cal.params_for("KAAA", OLD, date(2026, 5, 1))
    assert may.b > 2.0


def test_cutoff_respects_the_decision_day():
    # First decision for March is 16 UTC Feb 28; the last final high known then is Feb 27.
    assert Calibrator.cutoff_for(date(2026, 3, 1)) == date(2026, 2, 27)


def test_thin_city_shrinks_toward_all_cities():
    kb, hb, db = _synthetic(600, bias=0.0, stretch=1.0, start=date(2025, 1, 1))
    kt, ht, dt = _synthetic(12, bias=3.0, stretch=1.0, seed=3, start=date(2025, 12, 1))
    rows = _rows("KBIG", OLD, kb, hb, db) + _rows("KTHN", OLD, kt, ht, dt)
    cal = Calibrator(rows, {"shrink_days": 30})
    thin = cal.params_for("KTHN", OLD, date(2026, 1, 1))
    assert 0.3 < thin.b < 2.5  # pulled well below its own ~3 toward the pooled ~0


def test_new_version_starts_from_old_fit_and_takes_over():
    ko, ho, do = _synthetic(480, bias=2.0, stretch=1.0, start=date(2024, 11, 1))
    kn, hn, dn = _synthetic(200, bias=-1.0, stretch=1.0, seed=4, start=date(2026, 5, 1))
    cal = Calibrator(
        _rows("KAAA", OLD, ko, ho, do) + _rows("KAAA", NEW, kn, hn, dn),
        {"version_handover_days": 120, "shrink_days": 0},
    )
    early = cal.params_for("KAAA", NEW, date(2026, 5, 1))  # no v5 data yet
    later = cal.params_for("KAAA", NEW, date(2026, 11, 1))  # >120 v5 days
    assert early.b == pytest.approx(2.0, abs=0.4)
    assert later.b == pytest.approx(-1.0, abs=0.4)


def test_winter_widening_until_v5_has_a_winter():
    kn, hn, dn = _synthetic(150, bias=0.0, stretch=1.0, start=date(2026, 5, 4))
    cal = Calibrator(
        _rows("KAAA", NEW, kn, hn, dn),
        {"winter_spread_factor": 1.2, "winter_months": [12, 1, 2], "shrink_days": 0},
    )
    nov = cal.params_for("KAAA", NEW, date(2026, 11, 1))
    dec = cal.params_for("KAAA", NEW, date(2026, 12, 1))
    assert dec.s == pytest.approx(nov.s * 1.2, rel=0.1)


def test_family_mapping():
    assert family_of("5.0") == NEW and family_of("4.3") == OLD and family_of("4.x") == OLD
    assert family_of("transition") is None


def test_bot_code_never_loads_numpy_random():
    # Carlos's PC blocks numpy.random's compiled files (Windows Application Control).
    import subprocess
    import sys

    code = (
        "import sys, weatherbot.backtest.score, weatherbot.model.calibrate; "
        "sys.exit('numpy.random' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
