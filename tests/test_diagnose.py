"""Diagnostics: blend weight and timing checks on synthetic events with known answers."""

import math
import random
from datetime import date, timedelta

import numpy as np
import pytest

from weatherbot.backtest.diagnose import (
    HOURS,
    EventRec,
    best_weight,
    blend,
    paired_diff,
    report,
    walk_forward,
)


def _softmax(xs):
    m = max(xs)
    e = [math.exp(x - m) for x in xs]
    s = sum(e)
    return np.array([v / s for v in e])


def _events(n, market_knows, model_knows, seed=0):
    """Truth: a random distribution over 6 brackets; the winner is drawn from it.

    `market_knows` / `model_knows` in [0, 1]: how much of the truth each one sees
    (the rest is noise of its own).
    """
    rng = random.Random(seed)
    out = []
    for i in range(n):
        logits = [rng.gauss(0, 1.2) for _ in range(6)]
        truth = _softmax(logits)
        winner = rng.choices(range(6), weights=list(truth))[0]

        def view(knows, logits=logits):
            return _softmax([knows * x + (1 - knows) * rng.gauss(0, 1.2) for x in logits])

        mk = view(market_knows)
        out.append(
            EventRec(
                "KAAA",
                date(2025, 1, 1) + timedelta(days=i // 3),
                winner,
                view(model_knows),
                view(model_knows),
                {h: mk for h in HOURS},
                {h: 300 for h in HOURS},
            )
        )
    return out


def test_blend_endpoints():
    m, p = np.array([0.7, 0.3]), np.array([0.2, 0.8])
    assert blend(m, p, 0.0) == pytest.approx(m)
    assert blend(m, p, 1.0) == pytest.approx(p)


def test_no_weight_when_market_already_knows_everything():
    ev = _events(1500, market_knows=1.0, model_knows=0.5)
    w, _ = best_weight(ev, "p_cal", 16)
    assert w <= 0.1
    wf = walk_forward(ev, "p_cal")
    assert wf.blend_vs_market.lo90 < 0.005  # no reliable gain


def test_weight_found_when_model_sees_what_market_misses():
    ev = _events(1500, market_knows=0.4, model_knows=1.0)
    w, _ = best_weight(ev, "p_cal", 16)
    assert w >= 0.5
    wf = walk_forward(ev, "p_cal")
    assert wf.blend_vs_market.lo90 > 0  # reliable gain, out of sample


def test_paired_diff_clusters_by_day():
    days = [date(2025, 1, 1)] * 5 + [date(2025, 1, 2)] * 5 + [date(2025, 1, 3)] * 5
    a = [1.0] * 5 + [0.0] * 5 + [1.0] * 5
    b = [0.0] * 15
    d = paired_diff(a, b, days)
    assert d.mean == pytest.approx(2 / 3) and d.days == 3 and d.lo90 < d.mean < d.hi90


def test_report_runs():
    text = report(_events(400, 0.8, 0.8))
    assert "walk-forward" in text and "Market at 15 UTC" in text


def test_collect_end_to_end_needs_quotes_at_every_hour():
    from test_score import CFG, _ladder, build

    from weatherbot.backtest.diagnose import collect
    from weatherbot.backtest.score import decision_time
    from weatherbot.data.storage import Storage

    s = Storage(":memory:")
    highs = build(s, days=90)
    d1, d2 = date(2025, 3, 20), date(2025, 3, 21)
    for d, hours in ((d1, HOURS), (d2, HOURS[:2])):  # d2 lacks 18 and 21 UTC quotes
        ladder = _ladder("KXHIGHCHI", d, highs[d])
        for m in ladder:
            s.upsert_settled_market(m, "KXHIGHCHI", d.isoformat(), "historical")
            win = m["result"] == "yes"
            s.upsert_candles(
                m["ticker"],
                [
                    {
                        "end_ts": int(decision_time(d, h).timestamp()) - 60,
                        "yes_bid_e4": 4000 if win else 1500,
                        "yes_ask_e4": 4300 if win else 1800,
                    }
                    for h in hours
                ],
            )
    ev = collect(s, CFG)
    assert [e.day for e in ev] == [d1]
    e = ev[0]
    assert e.mkt[16][e.winner] == max(e.mkt[16]) and e.spread[16] == 300
    assert e.p_cal.sum() == pytest.approx(1.0)
