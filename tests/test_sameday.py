"""Same-day lock-in model and evaluation (synthetic data, no network)."""

import random
from datetime import UTC, date, datetime, timedelta

import pytest

from weatherbot.backtest.sameday import (
    LOCAL_HOURS,
    LockInModel,
    Moment,
    Situation,
    bracket_probs,
    bucket,
    collect,
    evaluate,
    fit_month,
    report,
    situation,
)
from weatherbot.data.obs_history import ensure_schema, lst_day_bounds
from weatherbot.data.storage import Storage


def test_buckets_and_situation():
    assert bucket(0.0, (0.5, 2.0)) == 0 and bucket(0.5, (0.5, 2.0)) == 1
    s = situation(17, 84.6, 80.0, 86.0)
    assert (s.m, s.drop_b, s.head_b) == (85, 3, 2)
    assert situation(17, 84.6, 84.6, None).head_b == bucket(0.0, (-2.0, 0.0, 2.0, 4.0))


def test_model_learns_that_late_day_highs_are_locked():
    model = LockInModel()
    late = Situation(19, 80, 4, 1)
    early = Situation(10, 70, 0, 4)
    for _ in range(200):
        model.add(late, 80)  # always locked by 7 PM
        model.add(early, 78)  # morning max still 8F short
    p_late = model.predict(late)
    assert sum(p_late.values()) == pytest.approx(1.0)
    assert p_late[80] > 0.85
    assert max(model.predict(early), key=model.predict(early).get) == 78


def test_unseen_situation_backs_off_to_coarser_counts():
    model = LockInModel()
    for _ in range(100):
        model.add(Situation(19, 80, 4, 1), 80)
    p = model.predict(Situation(19, 60, 4, 3))  # same hour and drop, new headroom
    assert max(p, key=p.get) == 60


def test_bracket_probs_use_strike_type_and_inclusive_ends():
    pmf = {79: 0.1, 80: 0.6, 81: 0.3}
    ev = [
        {"strike_type": "less", "floor_strike": None, "cap_strike": 79},
        {"strike_type": "between", "floor_strike": 79, "cap_strike": 80},
        {"strike_type": "greater", "floor_strike": 80, "cap_strike": None},
    ]
    assert bracket_probs(pmf, ev) == pytest.approx(
        [1e-4 / 1.0002, 0.7 / 1.0002, 0.3 / 1.0002], rel=1e-3
    )


def test_training_never_uses_the_current_month():
    moms = [Moment("KAAA", date(2025, 3, d), Situation(19, 80, 4, 1), 90) for d in range(1, 29)]
    moms += [Moment("KAAA", date(2025, 2, 10), Situation(19, 80, 4, 1), 80)]
    model = fit_month(moms, date(2025, 3, 1))
    assert model.base.sum() == 1  # only the February day


def _ladder(final, lo=70):
    out = []
    for f in range(lo, lo + 30, 2):
        out.append(
            {
                "ticker": f"T{f}",
                "strike_type": "between",
                "floor_strike": f,
                "cap_strike": f + 1,
                "result": "yes" if f <= final <= f + 1 else "no",
            }
        )
    return out


def test_evaluate_finds_information_an_uninformed_market_lacks():
    rng = random.Random(1)
    moms = []
    start = date(2025, 1, 1)
    for i in range(500):
        d = start + timedelta(days=i)
        m = 72 + rng.randrange(0, 16)
        final = m if rng.random() < 0.85 else m + 1
        ev = _ladder(final)
        flat = [(500, 1500)] * len(ev)  # market: every bracket 5-15c, knows nothing
        moms.append(Moment("KAAA", d, Situation(19, m, 4, 1), final, ev, flat))
    res = {r.hour: r for r in evaluate(moms, noise=0.001)}
    r = res[19]
    assert r.locked == pytest.approx(0.85, abs=0.06)
    assert r.dead_n > 0 and r.dead_won == 0 and r.dead_bid == pytest.approx(5.0)
    text = report(list(res.values()))
    assert "19" in text and "Dead brackets" in text
    from weatherbot.backtest.diagnose import walk_forward

    wf = walk_forward(r.events, "p_cal", 19, min_prior=100)
    assert wf.blend_vs_market.lo90 > 0


def ts(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


def test_collect_uses_lst_hours_and_quotes_from_the_last_hour():
    s = Storage(":memory:")
    ensure_schema(s)
    day = date(2025, 7, 4)
    start, _ = lst_day_bounds(day, -6)  # Chicago: 06:00Z
    obs = [(start + h * 3600 + 51 * 60, "h", 70.0 + min(h, 15), None) for h in range(0, 23)]
    s.conn.executemany(
        "INSERT INTO obs_history (valid_ts, rtype, tmpf, max6_f, station) VALUES (?,?,?,?,'KMDW')",
        obs,
    )
    s.upsert_cli_history("KMDW", [(day.isoformat(), 85, 70, "x")])
    ev = _ladder(85)
    for m in ev:
        m["event_ticker"] = "KXHIGHCHI-25JUL04"
        s.upsert_settled_market(m, "KXHIGHCHI", day.isoformat(), "historical")
    t17 = start + 17 * 3600
    for m in ev:
        s.upsert_candles(
            m["ticker"], [{"end_ts": t17 - 1800, "yes_bid_e4": 100, "yes_ask_e4": 300}]
        )
    cfg = {
        "stations": {"KXHIGHCHI": "KMDW"},
        "station_utc_offset": {"KMDW": -6},
        "backtest": {"tuning_end": "2025-12-31"},
    }
    moms = collect(s, cfg)
    by_hour = {m.sit.hour: m for m in moms}
    assert set(by_hour) == set(LOCAL_HOURS)
    # Temps rise 1F an hour from 70F and level off at 85F. Reports count 10 minutes after
    # their time, so at 10:00 LST the 09:51 report (79F) isn't usable yet: max is 78F.
    assert by_hour[10].sit.m == 78 and by_hour[17].sit.m == 85
    assert by_hour[17].quotes is not None  # quote 30 minutes old: fresh
    assert by_hour[19].quotes is None  # 2.5 hours old: stale for same-day
