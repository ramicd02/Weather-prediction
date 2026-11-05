"""Scoring report: look-ahead rules and market comparison (in-memory data)."""

from datetime import date, timedelta

import numpy as np

from weatherbot.backtest.score import decision_time, format_report, score
from weatherbot.data.storage import Storage
from weatherbot.model.nbm_dist import Z

CFG = {
    "stations": {"KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA"},
    "trading_exclude_series": ["KXHIGHMIA"],
    "backtest": {"decision_hour_utc": 16, "tuning_end": "2025-06-30", "quote_max_age_hours": 3},
    "model": {"min_train_days": 30, "settlement_noise": 0.001},
}


def _fc(st, target, mu, sd=2.0, version="4.3", available_shift_h=0):
    cyc = target - timedelta(days=1)
    k = mu + sd * Z
    avail = f"{cyc.isoformat()}T{15 + available_shift_h:02d}:00:00Z"
    return {
        "station": st,
        "cycle_time": f"{cyc.isoformat()}T13:00:00Z",
        "available_at": avail,
        "valid_time": f"{target.isoformat()}T00:00:00Z",
        "fhr": 35,
        "target_date": target.isoformat(),
        "p10_f": round(k[0]),
        "p25_f": round(k[1]),
        "p50_f": round(k[2]),
        "p75_f": round(k[3]),
        "p90_f": round(k[4]),
        "nbm_version": version,
    }


def _ladder(series, d, high):
    out = []
    for lo in range(high - 4, high + 4, 2):
        out.append(
            {
                "ticker": f"{series}-{d:%y%b%d}-B{lo + 0.5}".upper(),
                "event_ticker": f"{series}-{d:%y%b%d}".upper(),
                "strike_type": "between",
                "floor_strike": lo,
                "cap_strike": lo + 1,
                "result": "yes" if lo <= high <= lo + 1 else "no",
            }
        )
    return out


def build(storage, days=200, bias=2):
    rng = np.random.default_rng(0)
    start = date(2025, 1, 1)
    fcs, highs = [], []
    for i in range(days):
        d = start + timedelta(days=i)
        mu = 60 + 10 * np.sin(i / 30)
        fcs.append(_fc("KMDW", d, mu))
        highs.append((d.isoformat(), int(round(mu + bias + 2 * rng.standard_normal())), 0, "x"))
        fcs.append(_fc("KMIA", d, 80))
    storage.upsert_nbm_forecasts(fcs)
    storage.upsert_cli_history("KMDW", highs)
    storage.upsert_cli_history("KMIA", [(h[0], 80, 70, "x") for h in highs])
    return {date.fromisoformat(h[0]): h[1] for h in highs}


def test_calibration_beats_raw_on_a_biased_city_and_miami_is_excluded():
    s = Storage(":memory:")
    build(s)
    scores, days = score(s, CFG)
    assert [x.station for x in scores] == ["KMDW"]
    mdw = scores[0]
    assert mdw.days > 100
    assert np.mean(mdw.deg_cal) < np.mean(mdw.deg_raw)
    # tuning_end is 2025-06-30: nothing later may be scored.
    assert max(r["date"] for r in days) <= "2025-06-30"


def test_forecast_published_after_the_decision_is_not_used():
    s = Storage(":memory:")
    build(s, days=80)
    d = date(2025, 3, 10)
    late = _fc("KMDW", d, 99, available_shift_h=2)  # 17 UTC: after the 16 UTC decision
    late["cycle_time"] = f"{d - timedelta(days=1)}T14:00:00Z"
    s.upsert_nbm_forecasts([late])
    _, days = score(s, CFG)
    row = next(r for r in days if r["date"] == d.isoformat())
    assert row["p50"] != 99


def test_market_comparison_needs_fresh_quotes_on_every_bracket():
    s = Storage(":memory:")
    highs = build(s, days=90)
    d = date(2025, 3, 20)
    ladder = _ladder("KXHIGHCHI", d, highs[d])
    for m in ladder:
        s.upsert_settled_market(m, "KXHIGHCHI", d.isoformat(), "historical")
    ts = int(decision_time(d, 16).timestamp())
    for m in ladder:
        win = m["result"] == "yes"
        s.upsert_candles(
            m["ticker"],
            [
                {
                    "end_ts": ts - 600,
                    "yes_bid_e4": 4000 if win else 1500,
                    "yes_ask_e4": 4400 if win else 1900,
                }
            ],
        )
    scores, days = score(s, CFG)
    row = next(r for r in days if r["date"] == d.isoformat())
    assert "br_ll_mkt" in row and row["br_ll_mkt"] > 0
    # Stale quote (older than 3h) on one bracket -> the event is skipped.
    s.upsert_candles(
        ladder[0]["ticker"], [{"end_ts": ts - 600, "yes_bid_e4": None, "yes_ask_e4": None}]
    )
    _, days = score(s, CFG)
    row = next(r for r in days if r["date"] == d.isoformat())
    assert "br_ll_mkt" not in row
    assert "KMDW" in format_report(scores, date(2025, 6, 30), {"KMDW": "KXHIGHCHI"})
