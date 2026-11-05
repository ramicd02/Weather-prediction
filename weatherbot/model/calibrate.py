"""Per-city calibration of the NBM distribution: a shift `b` and a stretch `s`.

Fitting (all rules from docs/PHASE2.md):
- No peeking: parameters used for month M are fitted only on highs settled by the day
  before the first decision of M (target dates up to M's first day minus 2).
- Seasonal drift: training days are weighted by closeness in day-of-year to mid-M
  (Gaussian, `model.season_bandwidth_days`) and by recency (`model.recency_days`).
- Thin cities shrink toward the all-city fit (`model.shrink_days` pseudo-days).
- NBM v5.0 starts from the old version's fit and takes over by
  `model.version_handover_days` days of its own data.
- Until v5.0 has seen a winter at a city, winter months get `model.winter_spread_factor`.

The objective is the weighted log probability of the actual whole-degree high.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from .nbm_dist import prob_of_high

OLD, NEW = "old", "new"


def family_of(version: str | None) -> str | None:
    """'5.0' -> new; 4.x versions -> old; the switch-over week -> None (not used)."""
    if version == "5.0":
        return NEW
    if version in (None, "", "transition"):
        return None
    return OLD


@dataclass(frozen=True)
class Params:
    b: float = 0.0
    s: float = 1.0
    n_eff: float = 0.0  # sum of training weights


@dataclass
class TrainRow:
    station: str
    target: date
    family: str
    knots: np.ndarray  # (5,)
    high: int


def fit(knots: np.ndarray, highs: np.ndarray, w: np.ndarray) -> Params:
    """Weighted max-likelihood shift and stretch: coarse grid, then two refinements."""
    if len(highs) == 0 or float(w.sum()) <= 0:
        return Params()
    best = (-math.inf, 0.0, 0.0)  # (score, b, log s)

    def scan(bs: np.ndarray, ls: np.ndarray) -> None:
        nonlocal best
        for b in bs:
            for lsv in ls:
                score = float(np.dot(w, np.log(prob_of_high(knots, highs, b, math.exp(lsv)))))
                if score > best[0]:
                    best = (score, float(b), float(lsv))

    scan(np.arange(-4.0, 4.01, 0.5), np.linspace(math.log(0.5), math.log(2.5), 13))
    for step_b, step_ls in ((0.125, 0.0335), (0.03125, 0.0084)):
        _, b0, l0 = best
        scan(b0 + step_b * np.arange(-4, 5), l0 + step_ls * np.arange(-4, 5))
    _, b, ls = best
    return Params(b, math.exp(ls), float(w.sum()))


def _shrink(own: Params, pooled: Params, k: float) -> Params:
    if own.n_eff <= 0:
        return Params(pooled.b, pooled.s, 0.0)
    a = own.n_eff / (own.n_eff + k)
    b = a * own.b + (1 - a) * pooled.b
    ls = a * math.log(own.s) + (1 - a) * math.log(pooled.s)
    return Params(b, math.exp(ls), own.n_eff)


def _blend(new: Params, old: Params, alpha: float) -> Params:
    b = alpha * new.b + (1 - alpha) * old.b
    ls = alpha * math.log(new.s) + (1 - alpha) * math.log(old.s)
    return Params(b, math.exp(ls), new.n_eff + old.n_eff)


@dataclass
class Calibrator:
    rows: Sequence[TrainRow]
    cfg: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        m = self.cfg
        self.bandwidth = float(m.get("season_bandwidth_days", 45))
        self.recency = float(m.get("recency_days", 365))
        self.shrink_k = float(m.get("shrink_days", 30))
        self.handover = float(m.get("version_handover_days", 120))
        self.winter_factor = float(m.get("winter_spread_factor", 1.0))
        self.winter_months = set(m.get("winter_months", [12, 1, 2]))
        self.min_winter_days = int(m.get("min_winter_days", 60))
        by: dict[tuple[str, str], list[TrainRow]] = defaultdict(list)
        for r in self.rows:
            by[(r.station, r.family)].append(r)
        self._arrays: dict[tuple[str, str], tuple[np.ndarray, ...]] = {}
        for key, rs in by.items():
            self._arrays[key] = (
                np.array([r.target.toordinal() for r in rs]),
                np.array([r.target.timetuple().tm_yday for r in rs]),
                np.stack([r.knots for r in rs]),
                np.array([r.high for r in rs], dtype=float),
            )
        self._pooled: dict[tuple[str, date], Params] = {}
        self._own: dict[tuple[str, str, date], Params] = {}

    @staticmethod
    def cutoff_for(month_start: date) -> date:
        """Latest target date usable for decisions in a month (exclusive bound is +1).

        The first decision of the month is at 16 UTC the day before month_start; the
        last final NWS high known then is for month_start - 2 days.
        """
        return month_start - timedelta(days=2)

    def _weights(self, key: tuple[str, str], month_start: date) -> tuple[np.ndarray, ...]:
        if key not in self._arrays:
            empty = np.array([])
            return empty, np.empty((0, 5)), empty, 0
        ords, doys, knots, highs = self._arrays[key]
        cut = self.cutoff_for(month_start).toordinal()
        keep = ords <= cut
        mid = month_start.replace(day=15)
        mid_doy, mid_ord = mid.timetuple().tm_yday, mid.toordinal()
        d = np.abs(doys[keep] - mid_doy)
        d = np.minimum(d, 365 - d)
        age = mid_ord - ords[keep]
        w = np.exp(-0.5 * (d / self.bandwidth) ** 2) * np.exp(-age / self.recency)
        return w, knots[keep], highs[keep], int(keep.sum())

    def _own_fit(self, station: str, family: str, month_start: date) -> tuple[Params, int]:
        w, k, h, n = self._weights((station, family), month_start)
        key = (station, family, month_start)
        if key not in self._own:
            self._own[key] = fit(k, h, w) if n else Params()
        return self._own[key], n

    def _pooled_fit(self, family: str, month_start: date) -> Params:
        key = (family, month_start)
        if key not in self._pooled:
            parts = [self._weights(k, month_start) for k in self._arrays if k[1] == family]
            parts = [p for p in parts if p[3]]
            if not parts:
                self._pooled[key] = Params()
            else:
                w = np.concatenate([p[0] for p in parts])
                kn = np.concatenate([p[1] for p in parts])
                h = np.concatenate([p[2] for p in parts])
                self._pooled[key] = fit(kn, h, w)
        return self._pooled[key]

    def training_days(self, station: str, month_start: date) -> int:
        """Days of training data (either NBM family) available for this month."""
        return sum(self._weights((station, f), month_start)[3] for f in (OLD, NEW))

    def params_for(self, station: str, family: str, month_start: date) -> Params:
        old_own, _ = self._own_fit(station, OLD, month_start)
        old = _shrink(old_own, self._pooled_fit(OLD, month_start), self.shrink_k)
        if family == OLD:
            return old
        new_own, n_new = self._own_fit(station, NEW, month_start)
        new = _shrink(new_own, self._pooled_fit(NEW, month_start), self.shrink_k)
        alpha = min(1.0, n_new / self.handover) if self.handover > 0 else 1.0
        p = _blend(new, old, alpha) if old.n_eff > 0 else new
        if month_start.month in self.winter_months and self.winter_factor != 1.0:
            ords, *_ = self._arrays.get((station, NEW), (np.array([]),))
            cut = self.cutoff_for(month_start).toordinal()
            winter_days = sum(
                1 for o in ords if o <= cut and date.fromordinal(int(o)).month in self.winter_months
            )
            if winter_days < self.min_winter_days:
                p = Params(p.b, p.s * self.winter_factor, p.n_eff)
        return p
