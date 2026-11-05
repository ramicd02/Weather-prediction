"""Two checks before building the full backtest (tuning months only).

    python -m weatherbot.backtest.diagnose

1. Does our model add anything the market doesn't already know?
   Blend the two per event: q ~ market^(1-w) * model^w (renormalised across the
   event's brackets). The weight w is fitted on earlier months only and scored on the
   next month (walk-forward). If the best w is about 0, the model carries no extra
   information, and no directional strategy built on it can beat the market.

2. Is the market slow at some time of day?
   Score the market's prices at 15, 16, 18 and 21 UTC the day before, on the same
   events, against our model (the 13Z NBM run, available ~15 UTC). If the market at
   15 UTC is no better than the model, there may be a short window to act in.

Differences come with a 90% confidence range clustered by date, because all cities on
one day share weather and errors.
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from weatherbot.data.logger import DEFAULT_EXCLUDE, load_config, setup_logging
from weatherbot.data.storage import Storage
from weatherbot.model.brackets import add_settlement_noise, bracket_prob
from weatherbot.model.calibrate import Calibrator, TrainRow, family_of
from weatherbot.model.nbm_dist import Forecast, knots_from_rows

from .score import (
    MAX_SPREAD_E4,
    decision_time,
    load_events,
    load_forecasts,
    load_highs,
    quote_at,
)

HOURS = (15, 16, 18, 21)
FLOOR = 1e-4  # smallest probability allowed (keeps logs finite)
W_GRID = np.round(np.arange(0.0, 1.0001, 0.05), 2)


@dataclass
class EventRec:
    station: str
    day: date
    winner: int  # index of the bracket that won
    p_raw: np.ndarray  # model probabilities per bracket, raw NBM
    p_cal: np.ndarray  # calibrated model
    mkt: dict[int, np.ndarray]  # hour -> market probabilities (normalised mids)
    spread: dict[int, int]  # hour -> widest bracket spread (e4)


def _norm(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, FLOOR, None)
    return x / x.sum()


def blend(m: np.ndarray, p: np.ndarray, w: float) -> np.ndarray:
    """Log-linear pool: market^(1-w) * model^w, renormalised."""
    return _norm(np.exp((1 - w) * np.log(_norm(m)) + w * np.log(_norm(p))))


def ll(q: np.ndarray, winner: int) -> float:
    return -math.log(max(float(q[winner]), FLOOR))


def best_weight(events: Sequence[EventRec], model: str, hour: int) -> tuple[float, float]:
    """In-sample weight that minimises mean log loss, and that log loss."""
    best = (0.0, math.inf)
    for w in W_GRID:
        v = float(np.mean([ll(blend(e.mkt[hour], getattr(e, model), w), e.winner) for e in events]))
        if v < best[1] - 1e-12:
            best = (float(w), v)
    return best


@dataclass
class PairedDiff:
    n: int
    days: int
    mean: float  # mean of (A - B) per event
    lo90: float
    hi90: float


def paired_diff(a: Sequence[float], b: Sequence[float], days: Sequence[date]) -> PairedDiff:
    """Mean of a - b with a 90% range clustered by date (cities share a day's weather)."""
    d = np.asarray(a, float) - np.asarray(b, float)
    n = len(d)
    if n == 0:
        return PairedDiff(0, 0, math.nan, math.nan, math.nan)
    mean = float(d.mean())
    by: dict[date, list[float]] = defaultdict(list)
    for x, day in zip(d, days, strict=True):
        by[day].append(float(x) - mean)
    g = len(by)
    if g < 2:
        return PairedDiff(n, g, mean, math.nan, math.nan)
    var = sum(sum(v) ** 2 for v in by.values()) / n**2 * g / (g - 1)
    se = math.sqrt(var)
    return PairedDiff(n, g, mean, mean - 1.645 * se, mean + 1.645 * se)


@dataclass
class WalkForward:
    months: int
    events: int
    weights: list[tuple[str, float]]  # (month, weight used)
    blend_vs_market: PairedDiff  # market LL - blend LL; positive = blend better


def walk_forward(
    events: Sequence[EventRec], model: str, hour: int = 16, min_prior: int = 300
) -> WalkForward:
    by_month: dict[str, list[EventRec]] = defaultdict(list)
    for e in events:
        by_month[e.day.strftime("%Y-%m")].append(e)
    prior: list[EventRec] = []
    weights, mk_ll, bl_ll, days = [], [], [], []
    for month in sorted(by_month):
        cur = by_month[month]
        if len(prior) >= min_prior:
            w, _ = best_weight(prior, model, hour)
            weights.append((month, w))
            for e in cur:
                mk_ll.append(ll(_norm(e.mkt[hour]), e.winner))
                bl_ll.append(ll(blend(e.mkt[hour], getattr(e, model), w), e.winner))
                days.append(e.day)
        prior += cur
    return WalkForward(len(weights), len(mk_ll), weights, paired_diff(mk_ll, bl_ll, days))


def collect(storage: Storage, cfg: dict[str, Any], hours: Sequence[int] = HOURS) -> list[EventRec]:
    """Events with a usable forecast, a single winner and fresh quotes at every hour."""
    bt = cfg.get("backtest") or {}
    model_cfg = cfg.get("model") or {}
    hour = int(bt.get("decision_hour_utc", 16))
    end = date.fromisoformat(str(bt.get("tuning_end", "2026-07-31")))
    max_age = float(bt.get("quote_max_age_hours", 3))
    noise = float(model_cfg.get("settlement_noise", 0.0))
    min_train = int(model_cfg.get("min_train_days", 60))
    excluded = set(DEFAULT_EXCLUDE) | set(cfg.get("trading_exclude_series") or [])
    series_station = {s: st for s, st in (cfg.get("stations") or {}).items() if s not in excluded}
    stations = sorted(set(series_station.values()))

    fc = load_forecasts(storage, stations, hour, end)
    highs = load_highs(storage, stations, end)
    events = load_events(storage, series_station, end)
    train = [
        TrainRow(st, d, fam, knots_from_rows([r])[0], highs[(st, d)])
        for (st, d), r in fc.items()
        if (fam := family_of(r["nbm_version"])) and (st, d) in highs
    ]
    cal = Calibrator(train, model_cfg)

    out = []
    for (st, d), mk in sorted(events.items()):
        r = fc.get((st, d))
        fam = family_of(r["nbm_version"]) if r else None
        if r is None or fam is None:
            continue
        month = d.replace(day=1)
        if cal.training_days(st, month) < min_train:
            continue
        mk = sorted(mk, key=lambda m: (m["floor_strike"] or -999, m["cap_strike"] or 999))
        winners = [i for i, m in enumerate(mk) if m["result"] == "yes"]
        if len(winners) != 1:
            continue
        p = cal.params_for(st, fam, month)
        k = knots_from_rows([r])[0]
        pmf_raw = add_settlement_noise(Forecast(k).pmf(), noise)
        pmf_cal = add_settlement_noise(Forecast(k, p.b, p.s).pmf(), noise)
        args = [(m["strike_type"], m["floor_strike"], m["cap_strike"]) for m in mk]
        pr = [bracket_prob(pmf_raw, *a) for a in args]
        pc = [bracket_prob(pmf_cal, *a) for a in args]
        if any(x is None for x in pr + pc):
            continue
        mkt, spread = {}, {}
        for h in hours:
            qs = [quote_at(storage, m["ticker"], decision_time(d, h), max_age) for m in mk]
            if not all(qs):
                break
            mkt[h] = _norm(np.array([(q[0] + q[1]) / 2 for q in qs], float))  # type: ignore[index]
            spread[h] = max(q[1] - q[0] for q in qs)  # type: ignore[index]
        if len(mkt) != len(hours):
            continue
        out.append(
            EventRec(
                st,
                d,
                winners[0],
                _norm(np.array(pr, float)),
                _norm(np.array(pc, float)),
                mkt,
                spread,
            )
        )
    return out


def _fmt(d: PairedDiff) -> str:
    return f"{d.mean:+.3f} (90% range {d.lo90:+.3f} to {d.hi90:+.3f}; {d.n} events, {d.days} days)"


def report(events: Sequence[EventRec], hour: int = 16) -> str:
    tight = [e for e in events if e.spread[hour] <= MAX_SPREAD_E4]
    lines = [
        "Diagnostics, tuning months only. Log loss: lower is better. 1 nat = winner's",
        "probability multiplied by e (2.7x).",
        "",
        f"Events with fresh quotes on every bracket at {', '.join(map(str, HOURS))} UTC: "
        f"{len(events)}; of those with every spread <= 10c at {hour} UTC: {len(tight)}.",
        "",
        "1) Does the model add information to the market's price? (decision at 16 UTC)",
    ]
    for label, sample in (("all quoted events", events), ("spreads <= 10c", tight)):
        if not sample:
            continue
        lines.append(f"   Sample: {label}")
        for model in ("p_raw", "p_cal"):
            w, v = best_weight(sample, model, hour)
            mk = float(np.mean([ll(e.mkt[hour], e.winner) for e in sample]))
            lines.append(
                f"     {'raw NBM' if model == 'p_raw' else 'calibrated'}: best in-sample weight "
                f"w = {w:.2f}; log loss market {mk:.4f} -> blend {v:.4f} ({mk - v:+.4f})"
            )
        wf = walk_forward(sample, "p_cal", hour)
        if wf.months:
            used = ", ".join(f"{m} {w:.2f}" for m, w in wf.weights)
            lines.append(f"     walk-forward weights by month: {used}")
            lines.append(
                f"     walk-forward gain of blend over market (positive = better): "
                f"{_fmt(wf.blend_vs_market)}"
            )
    lines += ["", "2) Is the market slow? Market vs our model on the same events (spreads <= 10c)."]
    if tight:
        model_ll = [ll(e.p_cal, e.winner) for e in tight]
        days = [e.day for e in tight]
        lines.append(f"   Our calibrated model (13Z NBM): log loss {np.mean(model_ll):.4f}")
        for h in HOURS:
            m = [ll(e.mkt[h], e.winner) for e in tight]
            d = paired_diff(model_ll, m, days)
            lines.append(
                f"   Market at {h:02d} UTC: log loss {np.mean(m):.4f}; model minus market {_fmt(d)}"
            )
        lines.append("   A positive 'model minus market' means the market is better at that hour.")
    lines += [
        "",
        "Reading it: if w is about 0 and the walk-forward range includes 0 or sits below it,",
        "the model adds nothing the market lacks. If the market is better than the model even",
        "at 15 UTC, there is no window where the model is ahead.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weatherbot.backtest.diagnose")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    setup_logging("INFO")
    cfg = load_config(args.config)
    db_path = Path(cfg.get("logger", {}).get("db_path", "data_store/weatherbot.db"))
    if not db_path.exists():
        print(f"No database at {db_path}.")
        return 1
    storage = Storage(db_path)
    try:
        events = collect(storage, cfg)
        if not events:
            print("No events with quotes at every hour. Has the price backfill finished?")
            return 1
        print(report(events, int((cfg.get("backtest") or {}).get("decision_hour_utc", 16))))
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
