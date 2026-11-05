"""Same-day "lock-in" test: does the high observed so far tell us more than the market?

    python -m weatherbot.backtest.sameday
    python -m weatherbot.backtest.sameday --five-minute    # also use 5-minute data

At several local (standard) hours on the market's own day, we know the highest
temperature reported so far, M, and the latest temperature. The final NWS high is
usually M or a little above. This builds an empirical model of

    final high - M   given   hour of day, how far the temperature has dropped from M,
                             and how much higher the morning NBM forecast still is,

fitted month by month on earlier days only, and compares it to the market's prices at
the same moments, using the same checks as `diagnose` (log loss, blend weight,
walk-forward gain with a 90% range clustered by date). Tuning months only.

It also reports "dead" brackets: ones whose every outcome is already below the
observed high. They should trade at about 0c; a bid there would be nearly free money.
"""

from __future__ import annotations

import argparse
import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from weatherbot.data.logger import DEFAULT_EXCLUDE, load_config, setup_logging
from weatherbot.data.obs_history import DayObs, ensure_schema, lst_day_bounds, round_half_up
from weatherbot.data.storage import Storage
from weatherbot.model.brackets import add_settlement_noise, yes_if

from .diagnose import EventRec, best_weight, ll, walk_forward
from .score import load_events, load_highs

LOCAL_HOURS = (10, 13, 15, 17, 19)
DELTA_MIN, DELTA_MAX = -3, 15
DROP_EDGES = (0.5, 2.0, 4.0, 8.0)  # F below the max so far
HEAD_EDGES = (-2.0, 0.0, 2.0, 4.0)  # NBM median minus the max so far, F
SMOOTH = 20.0  # pseudo-counts toward the coarser estimate
QUOTE_MAX_AGE_S = 3600  # same-day prices move; use quotes from the last hour only


def bucket(x: float, edges: Sequence[float]) -> int:
    return bisect.bisect_right(list(edges), x)


@dataclass(frozen=True)
class Situation:
    hour: int
    m: int  # max so far, whole degrees
    drop_b: int
    head_b: int


def situation(hour: int, raw_max: float, current: float, nbm_p50: float | None) -> Situation:
    head = (nbm_p50 - raw_max) if nbm_p50 is not None else 0.0
    return Situation(
        hour,
        round_half_up(raw_max),
        bucket(raw_max - current, DROP_EDGES),
        bucket(head, HEAD_EDGES),
    )


DELTAS = list(range(DELTA_MIN, DELTA_MAX + 1))


@dataclass
class LockInModel:
    """Counts of (final - M) by situation, with back-off smoothing."""

    full: dict[tuple[int, int, int], np.ndarray] = field(default_factory=dict)
    by_drop: dict[tuple[int, int], np.ndarray] = field(default_factory=dict)
    by_hour: dict[int, np.ndarray] = field(default_factory=dict)
    base: np.ndarray = field(default_factory=lambda: np.zeros(len(DELTAS)))

    def add(self, s: Situation, final: int) -> None:
        d = min(max(final - s.m, DELTA_MIN), DELTA_MAX) - DELTA_MIN
        for table, key in (
            (self.full, (s.hour, s.drop_b, s.head_b)),
            (self.by_drop, (s.hour, s.drop_b)),
            (self.by_hour, s.hour),
        ):
            if key not in table:
                table[key] = np.zeros(len(DELTAS))
            table[key][d] += 1
        self.base[d] += 1

    def predict(self, s: Situation) -> dict[int, float]:
        """P(final high = k) for whole degrees k."""
        p = (self.base + 1.0) / (self.base.sum() + len(DELTAS))  # Laplace floor
        for table, key in (
            (self.by_hour, s.hour),
            (self.by_drop, (s.hour, s.drop_b)),
            (self.full, (s.hour, s.drop_b, s.head_b)),
        ):
            c = table.get(key)
            if c is not None:
                p = (c + SMOOTH * p) / (c.sum() + SMOOTH)
        return {s.m + d: float(v) for d, v in zip(DELTAS, p, strict=True)}


@dataclass
class Moment:
    station: str
    day: date
    sit: Situation
    final: int
    event: list[dict[str, Any]] | None = None  # Kalshi brackets, if any
    quotes: list[tuple[int, int]] | None = None  # (bid, ask) per bracket at that moment


def quote_window(storage: Storage, ticker: str, t: int) -> tuple[int, int] | None:
    rows = storage.query(
        """SELECT yes_bid_e4, yes_ask_e4 FROM kalshi_candles
           WHERE ticker = ? AND end_ts <= ? AND end_ts >= ?
             AND yes_bid_e4 IS NOT NULL AND yes_ask_e4 IS NOT NULL
           ORDER BY end_ts DESC LIMIT 1""",
        (ticker, t, t - QUOTE_MAX_AGE_S),
    )
    return (rows[0]["yes_bid_e4"], rows[0]["yes_ask_e4"]) if rows else None


def nbm_medians(storage: Storage, station: str, end: date) -> dict[date, float]:
    """Morning-before NBM median (13Z run the day before) per target day."""
    out = {}
    for r in storage.query(
        """SELECT target_date, p50_f FROM nbm_forecasts
           WHERE station = ? AND target_date <= ? AND p50_f IS NOT NULL
             AND CAST(substr(cycle_time, 12, 2) AS INTEGER) = 13
             AND target_date = date(substr(cycle_time, 1, 10), '+1 day')""",
        (station, end.isoformat()),
    ):
        out[date.fromisoformat(r["target_date"])] = float(r["p50_f"])
    return out


def collect(storage: Storage, cfg: dict[str, Any], five_minute: bool = False) -> list[Moment]:
    bt = cfg.get("backtest") or {}
    end = date.fromisoformat(str(bt.get("tuning_end", "2026-07-31")))
    offsets: dict[str, int] = cfg.get("station_utc_offset") or {}
    excluded = set(DEFAULT_EXCLUDE) | set(cfg.get("trading_exclude_series") or [])
    series_station = {s: st for s, st in (cfg.get("stations") or {}).items() if s not in excluded}
    stations = sorted(st for st in set(series_station.values()) if st in offsets)
    highs = load_highs(storage, stations, end)
    events = load_events(storage, series_station, end)
    rtypes = ("h", "f") if five_minute else ("h",)
    marks = ",".join("?" * len(rtypes))

    out: list[Moment] = []
    for st in stations:
        off = offsets[st]
        rows = storage.query(
            f"""SELECT valid_ts, tmpf, max6_f FROM obs_history
                WHERE station = ? AND rtype IN ({marks}) ORDER BY valid_ts""",
            (st, *rtypes),
        )
        obs = [(r["valid_ts"], r["tmpf"], r["max6_f"]) for r in rows]
        ts = [o[0] for o in obs]
        nbm = nbm_medians(storage, st, end)
        for (s2, day), final in sorted(highs.items()):
            if s2 != st:
                continue
            start, stop = lst_day_bounds(day, off)
            i, j = bisect.bisect_left(ts, start), bisect.bisect_left(ts, stop)
            if i == j:
                continue
            dobs = DayObs(start, stop, obs[i:j])
            ev = sorted(
                events.get((st, day), []),
                key=lambda m: (m["floor_strike"] or -999, m["cap_strike"] or 999),
            )
            for h in LOCAL_HOURS:
                t = start + h * 3600
                raw_max, current = dobs.running_max(t)
                if raw_max is None or current is None:
                    continue
                sit = situation(h, raw_max, current, nbm.get(day))
                mom = Moment(st, day, sit, final)
                if ev:
                    qs = [quote_window(storage, m["ticker"], t) for m in ev]
                    if all(qs):
                        mom.event, mom.quotes = ev, qs  # type: ignore[assignment]
                out.append(mom)
    return out


def fit_month(moments: Sequence[Moment], month_start: date) -> LockInModel:
    """Train on days whose final high was public before the month began."""
    cut = month_start - timedelta(days=1)
    model = LockInModel()
    for m in moments:
        if m.day < cut:
            model.add(m.sit, m.final)
    return model


def bracket_probs(pmf: dict[int, float], event: list[dict[str, Any]]) -> np.ndarray:
    p = []
    for m in event:
        p.append(
            sum(
                v
                for k, v in pmf.items()
                if yes_if(m["strike_type"], m["floor_strike"], m["cap_strike"], k)
            )
        )
    arr = np.clip(np.array(p, float), 1e-4, None)
    return arr / arr.sum()


@dataclass
class HourResult:
    hour: int
    days: int
    locked: float  # share of days the final high equalled the max so far
    events: list[EventRec]
    dead_n: int  # dead brackets seen
    dead_bid: float  # their average YES bid, cents
    dead_won: int  # how many of them won anyway


def evaluate(
    moments: Sequence[Moment], noise: float, min_prior_days: int = 120
) -> list[HourResult]:
    months = sorted({m.day.replace(day=1) for m in moments})
    models = {mo: fit_month(moments, mo) for mo in months}
    first_day = min(m.day for m in moments) if moments else date.min
    out = []
    for h in LOCAL_HOURS:
        hm = [m for m in moments if m.sit.hour == h]
        recs: list[EventRec] = []
        dead_bids: list[float] = []
        dead_won = 0
        for m in hm:
            if m.event is None or m.quotes is None:
                continue
            winners = [i for i, b in enumerate(m.event) if b["result"] == "yes"]
            if len(winners) != 1:
                continue
            mo = m.day.replace(day=1)
            if (mo - first_day).days < min_prior_days:
                continue
            pmf = add_settlement_noise(models[mo].predict(m.sit), noise)
            p = bracket_probs(pmf, m.event)
            mids = np.array([(q[0] + q[1]) / 2 for q in m.quotes], float)
            mk = np.clip(mids, 1e-4, None)
            mk = mk / mk.sum()
            recs.append(
                EventRec(
                    m.station,
                    m.day,
                    winners[0],
                    p,
                    p,
                    {h: mk},
                    {h: max(q[1] - q[0] for q in m.quotes)},
                )
            )
            for b, q in zip(m.event, m.quotes, strict=True):
                top = (
                    b["cap_strike"]
                    if b["strike_type"] in ("between", "less", "less_or_equal")
                    else None
                )
                if top is not None and top < m.sit.m - 1:
                    dead_bids.append(q[0] / 100)
                    dead_won += b["result"] == "yes"
        locked = float(np.mean([m.final == m.sit.m for m in hm])) if hm else math.nan
        out.append(
            HourResult(
                h, len(hm), locked, recs, len(dead_bids),
                float(np.mean(dead_bids)) if dead_bids else math.nan, dead_won,
            )
        )  # fmt: skip
    return out


def report(results: Sequence[HourResult]) -> str:
    lines = [
        "Same-day lock-in test, tuning months only. Times are local standard time.",
        "Log loss: lower is better. 'Locked' = share of days the final NWS high equalled the",
        "highest temperature reported by that hour.",
        "",
        f"{'Hour':>5}{'Days':>7}{'Locked':>8}{'Events':>8}{'Market LL':>11}{'Model LL':>10}"
        f"{'Best w':>8}  Walk-forward gain of blend over market (90% range)",
    ]
    for r in results:
        if not r.events:
            lines.append(f"{r.hour:>5}{r.days:>7}{r.locked:>8.0%}{0:>8}")
            continue
        mk = float(np.mean([ll(e.mkt[r.hour], e.winner) for e in r.events]))
        md = float(np.mean([ll(e.p_cal, e.winner) for e in r.events]))
        w, _ = best_weight(r.events, "p_cal", r.hour)
        wf = walk_forward(r.events, "p_cal", r.hour, min_prior=200)
        d = wf.blend_vs_market
        gain = (
            f"{d.mean:+.4f} ({d.lo90:+.4f} to {d.hi90:+.4f}; {d.n} events)"
            if wf.months
            else "not enough months"
        )
        lines.append(
            f"{r.hour:>5}{r.days:>7}{r.locked:>8.0%}{len(r.events):>8}{mk:>11.4f}{md:>10.4f}"
            f"{w:>8.2f}  {gain}"
        )
    lines += ["", "Dead brackets (every outcome already below the high so far, minus 1F):"]
    for r in results:
        if r.dead_n:
            lines.append(
                f"  {r.hour:>2}:00  {r.dead_n} brackets, average YES bid {r.dead_bid:.2f}c, "
                f"won {r.dead_won} times"
            )
    lines += [
        "",
        "Reading it: a best weight w well above 0 with a walk-forward range above 0 means the",
        "observations add information the market hasn't priced at that hour. Then check",
        "whether the gain survives spreads and fees (next step). Dead brackets with bids of",
        "several cents that never win would be a small, nearly risk-free trade.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weatherbot.backtest.sameday")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--five-minute", action="store_true")
    args = parser.parse_args(argv)
    setup_logging("INFO")
    cfg = load_config(args.config)
    db_path = Path(cfg.get("logger", {}).get("db_path", "data_store/weatherbot.db"))
    if not db_path.exists():
        print(f"No database at {db_path}.")
        return 1
    storage = Storage(db_path)
    ensure_schema(storage)
    try:
        moments = collect(storage, cfg, args.five_minute)
        if not moments:
            print("No observations yet. Run `python -m weatherbot.data.obs_history backfill`.")
            return 1
        noise = float((cfg.get("model") or {}).get("settlement_noise", 0.0))
        print(report(evaluate(moments, noise)))
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
