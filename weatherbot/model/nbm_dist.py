"""NBM forecast distribution from the NBP percentiles, with per-city shift and stretch.

The NBP bulletin gives the daily max at the 10th, 25th, 50th, 75th and 90th
percentiles. We build a CDF by interpolating in normal-score (z) space between those
five points, and extend the tails beyond the 10th and 90th as normal tails whose scale
matches the nearest half of the distribution. NBM's skew is kept.

Calibration applies a shift `b` and a stretch `s` around the median:
    adjusted percentile = p50 + b + s * (percentile - p50)
which is the same as evaluating the raw CDF at  x0 = p50 + (x - p50 - b) / s.

Everything is vectorised over forecasts with numpy, because fitting evaluates the CDF
many thousands of times.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

PROBS = (0.10, 0.25, 0.50, 0.75, 0.90)
Z = np.array(
    [-1.2815515655446004, -0.6744897501960817, 0.0, 0.6744897501960817, 1.2815515655446004]
)
KNOT_COLS = ("p10_f", "p25_f", "p50_f", "p75_f", "p90_f")
MIN_GAP = 0.1  # NBM percentiles are whole degrees and often tie; keep knots increasing
MIN_TAIL_SCALE = 0.5  # degrees F


def _erf(x: np.ndarray) -> np.ndarray:
    """Vectorised erf (Abramowitz & Stegun 7.1.26, max error 1.5e-7). No scipy needed."""
    sign = np.sign(x)
    a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    poly = t * (
        0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429)))
    )
    return sign * (1.0 - poly * np.exp(-a * a))


def _phi(z: np.ndarray) -> np.ndarray:
    """Standard normal CDF, vectorised."""
    return 0.5 * (1.0 + _erf(z / math.sqrt(2.0)))


def knots_from_rows(rows: Sequence[Mapping[str, float | None]]) -> np.ndarray:
    """(n, 5) array of percentile values, forced strictly increasing.

    Rows missing p25/p75 get them from p10/p50/p90 by z-space interpolation.
    """
    out = np.empty((len(rows), 5))
    for i, r in enumerate(rows):
        v = [r.get(c) for c in KNOT_COLS]
        if v[0] is None or v[2] is None or v[4] is None:
            raise ValueError("p10, p50 and p90 are required")
        if v[1] is None:
            v[1] = v[2] + (v[0] - v[2]) * (Z[1] / Z[0])
        if v[3] is None:
            v[3] = v[2] + (v[4] - v[2]) * (Z[3] / Z[4])
        out[i] = v
    for j in range(1, 5):
        out[:, j] = np.maximum(out[:, j], out[:, j - 1] + MIN_GAP)
    return out


def raw_cdf(knots: np.ndarray, x: np.ndarray) -> np.ndarray:
    """CDF of the raw NBM distribution, one forecast per row, evaluated at x (shape (n,))."""
    lo_scale = np.maximum((knots[:, 2] - knots[:, 0]) / -Z[0], MIN_TAIL_SCALE)
    hi_scale = np.maximum((knots[:, 4] - knots[:, 2]) / Z[4], MIN_TAIL_SCALE)
    z = np.empty_like(x, dtype=float)
    below = x <= knots[:, 0]
    above = x >= knots[:, 4]
    z[below] = Z[0] + (x[below] - knots[below, 0]) / lo_scale[below]
    z[above] = Z[4] + (x[above] - knots[above, 4]) / hi_scale[above]
    mid = ~(below | above)
    for j in range(4):
        seg = mid & (x >= knots[:, j]) & (x <= knots[:, j + 1])
        if not seg.any():
            continue
        t = (x[seg] - knots[seg, j]) / (knots[seg, j + 1] - knots[seg, j])
        z[seg] = Z[j] + t * (Z[j + 1] - Z[j])
    return _phi(z)


def adjusted_cdf(knots: np.ndarray, x: np.ndarray, b: float, s: float) -> np.ndarray:
    """CDF after shifting by b and stretching by s around the median."""
    med = knots[:, 2]
    return raw_cdf(knots, med + (x - med - b) / s)


def prob_of_high(knots: np.ndarray, highs: np.ndarray, b: float, s: float) -> np.ndarray:
    """P(settled high == k) for each forecast, using whole-degree edges k +/- 0.5."""
    p = adjusted_cdf(knots, highs + 0.5, b, s) - adjusted_cdf(knots, highs - 0.5, b, s)
    return np.clip(p, 1e-9, 1.0)


@dataclass(frozen=True)
class Forecast:
    """One forecast's distribution, adjusted, ready to price brackets."""

    knots: np.ndarray  # (5,)
    b: float = 0.0
    s: float = 1.0

    def cdf(self, x: float) -> float:
        return float(
            adjusted_cdf(self.knots[None, :], np.array([x], dtype=float), self.b, self.s)[0]
        )

    def pmf(self, lo: int | None = None, hi: int | None = None) -> dict[int, float]:
        """Whole-degree probabilities, normalised, over a range wide enough for the tails."""
        med = float(self.knots[2])
        width = 12 + 6 * self.s * float(self.knots[4] - self.knots[0])
        lo = int(math.floor(med + self.b - width)) if lo is None else lo
        hi = int(math.ceil(med + self.b + width)) if hi is None else hi
        ks = np.arange(lo, hi + 1, dtype=float)
        knots = np.repeat(self.knots[None, :], len(ks), axis=0)
        p = adjusted_cdf(knots, ks + 0.5, self.b, self.s) - adjusted_cdf(
            knots, ks - 0.5, self.b, self.s
        )
        p = np.clip(p, 0.0, 1.0)
        total = float(p.sum())
        return {int(k): float(v) / total for k, v in zip(ks, p, strict=True) if v > 0}
