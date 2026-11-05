"""Score the model on the tuning months: raw NBM vs calibrated model vs the market.

    python -m weatherbot.backtest.score
    python -m weatherbot.backtest.score --station KMDW

Walk-forward and look-ahead free (docs/PHASE2.md):
- The forecast for day D is the latest NBM run available (by `available_at`) at the
  decision time, `backtest.decision_hour_utc` on day D-1.
- Calibration for D's month uses only highs up to two days before the month starts.
- Nothing after `backtest.tuning_end` is ever loaded. The locked months stay locked.

Three scores, lower is better:
- Degree log loss: -log P(actual whole-degree high). Raw NBM vs calibrated.
- Bracket log loss: -log P(the bracket that won) on Kalshi's real ladder, for events
  where every bracket had a fresh quote at decision time with a spread of 10c or less
  (chosen before the outcome is known). Raw, calibrated, and the market (mid-prices rescaled so each
  event adds to 100%).
- 10-90 coverage: share of highs inside the 10th-90th percentile range (ties count half);
  80% is ideal.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from weatherbot.data.logger import DEFAULT_EXCLUDE, load_config, setup_logging
from weatherbot.data.nbm import inside_share
from weatherbot.data.storage import Storage
from weatherbot.model.brackets import add_settlement_noise, bracket_prob
from weatherbot.model.calibrate import Calibrator, TrainRow, family_of
from weatherbot.model.nbm_dist import Forecast, knots_from_rows, prob_of_high

MAX_SPREAD_E4 = 1000  # 10c


def iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def decision_time(target: date, hour: int) -> datetime:
    d = target - timedelta(days=1)
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def load_forecasts(
    storage: Storage, stations: list[str], hour: int, end: date
) -> dict[tuple[str, date], dict[str, Any]]:
    """Latest usable NBM forecast per (station, target day), judged by available_at."""
    out: dict[tuple[str, date], dict[str, Any]] = {}
    for st in stations:
        for r in storage.query(
            """SELECT * FROM nbm_forecasts WHERE station = ? AND target_date <= ?
               AND p10_f IS NOT NULL AND p50_f IS NOT NULL AND p90_f IS NOT NULL
               ORDER BY cycle_time""",
            (st, end.isoformat()),
        ):
            target = date.fromisoformat(r["target_date"])
            if r["available_at"] > iso_z(decision_time(target, hour)):
                continue  # published after the decision: not usable
            out[(st, target)] = dict(r)  # ordered by cycle_time: latest usable wins
    return out


def load_highs(storage: Storage, stations: list[str], end: date) -> dict[tuple[str, date], int]:
    out = {}
    for st in stations:
        for r in storage.query(
            """SELECT climate_date, high_f FROM cli_history
               WHERE station = ? AND climate_date <= ? AND high_f IS NOT NULL""",
            (st, end.isoformat()),
        ):
            out[(st, date.fromisoformat(r["climate_date"]))] = int(r["high_f"])
    return out


def load_events(
    storage: Storage, series_station: dict[str, str], end: date
) -> dict[tuple[str, date], list[dict[str, Any]]]:
    out: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for series, st in series_station.items():
        for r in storage.query(
            """SELECT ticker, event_date, strike_type, floor_strike, cap_strike, result
               FROM kalshi_settled WHERE series_ticker = ? AND event_date <= ?""",
            (series, end.isoformat()),
        ):
            out[(st, date.fromisoformat(r["event_date"]))].append(dict(r))
    return out


def quote_at(
    storage: Storage, ticker: str, ts: datetime, max_age_h: float
) -> tuple[int, int] | None:
    t = int(ts.timestamp())
    rows = storage.query(
        """SELECT yes_bid_e4, yes_ask_e4 FROM kalshi_candles
           WHERE ticker = ? AND end_ts <= ? AND end_ts >= ?
             AND yes_bid_e4 IS NOT NULL AND yes_ask_e4 IS NOT NULL
           ORDER BY end_ts DESC LIMIT 1""",
        (ticker, t, t - int(max_age_h * 3600)),
    )
    return (rows[0]["yes_bid_e4"], rows[0]["yes_ask_e4"]) if rows else None


@dataclass
class StationScore:
    station: str
    days: int = 0
    deg_raw: list[float] = field(default_factory=list)
    deg_cal: list[float] = field(default_factory=list)
    cov_raw: list[float] = field(default_factory=list)
    cov_cal: list[float] = field(default_factory=list)
    br_raw: list[float] = field(default_factory=list)
    br_cal: list[float] = field(default_factory=list)
    br_mkt: list[float] = field(default_factory=list)


def _adj(knots: np.ndarray, b: float, s: float) -> tuple[float, float]:
    med = knots[2]
    return med + b + s * (knots[0] - med), med + b + s * (knots[4] - med)


def score(
    storage: Storage, cfg: dict[str, Any], only: str | None = None
) -> tuple[list[StationScore], list[dict[str, Any]]]:
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
    if only:
        stations = [s for s in stations if s == only]

    fc = load_forecasts(storage, stations, hour, end)
    highs = load_highs(storage, stations, end)
    events = load_events(storage, {k: v for k, v in series_station.items() if v in stations}, end)

    train = []
    for (st, d), r in fc.items():
        fam = family_of(r["nbm_version"])
        if fam and (st, d) in highs:
            train.append(TrainRow(st, d, fam, knots_from_rows([r])[0], highs[(st, d)]))
    cal = Calibrator(train, model_cfg)

    scores = {st: StationScore(st) for st in stations}
    days_out: list[dict[str, Any]] = []
    for (st, d), r in sorted(fc.items()):
        fam = family_of(r["nbm_version"])
        if fam is None or (st, d) not in highs:
            continue
        month = d.replace(day=1)
        if cal.training_days(st, month) < min_train:
            continue
        p = cal.params_for(st, fam, month)
        k = knots_from_rows([r])[0]
        h = highs[(st, d)]
        sc = scores[st]
        sc.days += 1
        lr = -math.log(float(prob_of_high(k[None, :], np.array([h], float), 0.0, 1.0)[0]))
        lc = -math.log(float(prob_of_high(k[None, :], np.array([h], float), p.b, p.s)[0]))
        sc.deg_raw.append(lr)
        sc.deg_cal.append(lc)
        sc.cov_raw.append(inside_share(h, k[0], k[4]))
        lo, hi = _adj(k, p.b, p.s)
        sc.cov_cal.append(inside_share(h, lo, hi))
        row: dict[str, Any] = {
            "station": st, "date": d.isoformat(), "nbm": r["nbm_version"], "high": h,
            "p50": float(k[2]), "b": round(p.b, 3), "s": round(p.s, 3),
            "deg_ll_raw": round(lr, 4), "deg_ll_cal": round(lc, 4),
        }  # fmt: skip

        mk = events.get((st, d))
        if mk:
            winners = [m for m in mk if m["result"] == "yes"]
            quotes = {
                m["ticker"]: quote_at(storage, m["ticker"], decision_time(d, hour), max_age)
                for m in mk
            }
            if len(winners) == 1 and all(quotes.values()):
                w = winners[0]
                # Filter on information known before the outcome only: every bracket's
                # spread, not the winner's (that would pick the sample by the result).
                widest = max(q[1] - q[0] for q in quotes.values())  # type: ignore[index]
                if widest <= MAX_SPREAD_E4:
                    mids = {t: (q[0] + q[1]) / 2 for t, q in quotes.items()}  # type: ignore[index]
                    total = sum(mids.values())
                    pm = mids[w["ticker"]] / total if total > 0 else 0
                    pr = add_settlement_noise(Forecast(k).pmf(), noise)
                    pc = add_settlement_noise(Forecast(k, p.b, p.s).pmf(), noise)
                    args = (w["strike_type"], w["floor_strike"], w["cap_strike"])
                    br_r = bracket_prob(pr, *args)
                    br_c = bracket_prob(pc, *args)
                    if br_r is not None and br_c is not None and pm > 0:
                        sc.br_raw.append(-math.log(max(br_r, 1e-6)))
                        sc.br_cal.append(-math.log(max(br_c, 1e-6)))
                        sc.br_mkt.append(-math.log(max(pm, 1e-6)))
                        row.update(
                            br_ll_raw=round(sc.br_raw[-1], 4),
                            br_ll_cal=round(sc.br_cal[-1], 4),
                            br_ll_mkt=round(sc.br_mkt[-1], 4),
                        )
        days_out.append(row)
    return [scores[s] for s in stations], days_out


def _m(x: list[float]) -> str:
    return f"{statistics.fmean(x):.3f}" if x else "-"


def _pct(x: list[float]) -> str:
    return f"{statistics.fmean(x):.0%}" if x else "-"


def format_report(scores: list[StationScore], end: date, series_of: dict[str, str]) -> str:
    lines = [
        f"Model scoring, tuning months only (through {end}). Lower log loss is better.",
        "",
        f"{'Station':<8}{'Series':<14}{'Days':>6}{'Deg LL raw':>11}{'cal':>7}"
        f"{'10-90 raw':>10}{'cal':>6}{'Events':>8}{'Br LL raw':>10}{'cal':>7}{'market':>8}",
    ]
    allv: dict[str, list[float]] = defaultdict(list)
    for s in sorted(scores, key=lambda s: -s.days):
        if not s.days:
            continue
        lines.append(
            f"{s.station:<8}{series_of.get(s.station, ''):<14}{s.days:>6}"
            f"{_m(s.deg_raw):>11}{_m(s.deg_cal):>7}{_pct(s.cov_raw):>10}{_pct(s.cov_cal):>6}"
            f"{len(s.br_mkt):>8}{_m(s.br_raw):>10}{_m(s.br_cal):>7}{_m(s.br_mkt):>8}"
        )
        for name in ("deg_raw", "deg_cal", "cov_raw", "cov_cal", "br_raw", "br_cal", "br_mkt"):
            allv[name] += getattr(s, name)
    lines += [
        "",
        f"ALL: degree LL raw {_m(allv['deg_raw'])} -> calibrated {_m(allv['deg_cal'])}; "
        f"10-90 coverage {_pct(allv['cov_raw'])} -> {_pct(allv['cov_cal'])} (80% ideal).",
        f"Bracket LL on {len(allv['br_mkt'])} events with clean quotes: raw {_m(allv['br_raw'])}, "
        f"calibrated {_m(allv['br_cal'])}, market {_m(allv['br_mkt'])}.",
        "Events need a fresh quote on every bracket, each with a spread of 10c or less.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weatherbot.backtest.score")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--station", help="one station only, e.g. KMDW")
    args = parser.parse_args(argv)
    setup_logging("INFO")
    cfg = load_config(args.config)
    db_path = Path(cfg.get("logger", {}).get("db_path", "data_store/weatherbot.db"))
    if not db_path.exists():
        print(f"No database at {db_path}.")
        return 1
    storage = Storage(db_path)
    try:
        scores, days = score(storage, cfg, args.station)
        end = date.fromisoformat(str((cfg.get("backtest") or {}).get("tuning_end", "2026-07-31")))
        series_of = {v: k for k, v in (cfg.get("stations") or {}).items()}
        print(format_report(scores, end, series_of))
        out = db_path.parent / "score_days.csv"
        keys: list[str] = []
        for d in days:
            keys += [k for k in d if k not in keys]
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(days)
        print(f"\nPer-day detail: {out}")
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
