"""Whole-degree probabilities and Kalshi bracket payouts.

Rules (CLAUDE.md 4 and 5): brackets are inclusive on both ends, and a market's
direction comes from `strike_type`, `floor_strike` and `cap_strike`, never the ticker.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


def yes_if(
    strike_type: str | None, floor: float | None, cap: float | None, v: float
) -> bool | None:
    """Would a market of this strike pay YES if the settled high were v?

    Returns None for strike types we don't model.
    """
    if strike_type == "greater" and floor is not None:
        return v > floor
    if strike_type == "greater_or_equal" and floor is not None:
        return v >= floor
    if strike_type == "less" and cap is not None:
        return v < cap
    if strike_type == "less_or_equal" and cap is not None:
        return v <= cap
    if strike_type == "between" and floor is not None and cap is not None:
        return floor <= v <= cap
    return None


def pmf_from_cdf(cdf: Callable[[float], float], lo: int = -60, hi: int = 140) -> dict[int, float]:
    """P(high == k) = F(k + 0.5) - F(k - 0.5), normalised over [lo, hi]."""
    raw = {k: max(cdf(k + 0.5) - cdf(k - 0.5), 0.0) for k in range(lo, hi + 1)}
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("distribution has no mass in range")
    return {k: p / total for k, p in raw.items() if p > 0}


def add_settlement_noise(pmf: Mapping[int, float], eps: float) -> dict[int, float]:
    """Mix in a chance `eps` that settlement lands 1F above or below (half each)."""
    out: dict[int, float] = {}
    for k, p in pmf.items():
        out[k] = out.get(k, 0.0) + p * (1 - eps)
        out[k - 1] = out.get(k - 1, 0.0) + p * eps / 2
        out[k + 1] = out.get(k + 1, 0.0) + p * eps / 2
    return out


def yes_set(
    pmf: Mapping[int, float], strike_type: str | None, floor: float | None, cap: float | None
) -> frozenset[int] | None:
    """The whole-degree highs (within the pmf's support) at which YES pays."""
    if yes_if(strike_type, floor, cap, 0) is None:
        return None
    return frozenset(k for k in pmf if yes_if(strike_type, floor, cap, k))


def bracket_prob(
    pmf: Mapping[int, float], strike_type: str | None, floor: float | None, cap: float | None
) -> float | None:
    wins = yes_set(pmf, strike_type, floor, cap)
    if wins is None:
        return None
    return sum(pmf[k] for k in wins)
