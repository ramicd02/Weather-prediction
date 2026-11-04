"""Edge rule and Kelly sizing for one city-day event.

Only one bracket of an event can win, so all candidate trades in an event are sized
together: we maximise expected log wealth over the possible settled highs. Positions
are fractions of bankroll spent (cost at risk), fees included.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    market: str
    side: str  # "yes" | "no"
    price: float  # dollars paid per contract (YES: ask; NO: 1 - YES bid)
    fee: float  # dollars per contract (unrounded rate, for sizing)
    wins: frozenset[int]  # highs at which the market's YES pays

    def pays(self, k: int) -> bool:
        return (k in self.wins) == (self.side == "yes")


def edge_ok(prob: float, price: float, fee: float, min_edge: float, min_rel: float) -> bool:
    """Probability of winning beats price + fee by both an absolute and a relative margin."""
    edge = prob - price - fee
    return edge >= min_edge and edge >= min_rel * price


def _returns(c: Candidate, k: int) -> float:
    """Wealth change per unit of bankroll spent on c, if the high is k."""
    return ((1.0 if c.pays(k) else 0.0) - c.price - c.fee) / c.price


def kelly_fractions(
    cands: Sequence[Candidate], probs: Mapping[int, float], iters: int = 200, tol: float = 1e-9
) -> list[float]:
    """Full-Kelly fractions of bankroll for each candidate (coordinate ascent).

    The objective sum_k p_k log(1 + sum_i f_i r_ik) is concave, so optimising one f_i
    at a time (bisection on its derivative) converges to the joint optimum.
    """
    ks = [k for k, p in probs.items() if p > 0]
    r = [[_returns(c, k) for k in ks] for c in cands]
    p = [probs[k] for k in ks]
    f = [0.0] * len(cands)
    wealth = [1.0] * len(ks)

    for _ in range(iters):
        moved = 0.0
        for i in range(len(cands)):
            base = [w - f[i] * r[i][j] for j, w in enumerate(wealth)]

            def deriv(x: float, i: int = i, base: list[float] = base) -> float:
                return sum(p[j] * r[i][j] / (base[j] + x * r[i][j]) for j in range(len(ks)))

            # Largest x keeping wealth positive in every outcome.
            hi = math.inf
            for j in range(len(ks)):
                if r[i][j] < 0:
                    hi = min(hi, base[j] / -r[i][j])
            hi = min(hi * (1 - 1e-9), 1.0)
            if hi <= 0 or deriv(0.0) <= 0:
                new = 0.0
            elif deriv(hi) >= 0:
                new = hi
            else:
                lo_x, hi_x = 0.0, hi
                for _ in range(80):
                    mid = (lo_x + hi_x) / 2
                    if deriv(mid) > 0:
                        lo_x = mid
                    else:
                        hi_x = mid
                new = (lo_x + hi_x) / 2
            moved = max(moved, abs(new - f[i]))
            f[i] = new
            wealth = [base[j] + new * r[i][j] for j in range(len(ks))]
        if moved < tol:
            break
    return [x if x > 1e-12 else 0.0 for x in f]


def size_event(
    cands: Sequence[Candidate],
    probs: Mapping[int, float],
    bankroll: float,
    kelly_fraction: float,
    cap: float,
    max_contracts: int,
) -> list[int]:
    """Whole contracts per candidate: fractional Kelly, capped per event and per trade."""
    if not cands:
        return []
    f = [x * kelly_fraction for x in kelly_fractions(cands, probs)]
    total = sum(f)
    if total > cap > 0:
        f = [x * cap / total for x in f]
    out = []
    for x, c in zip(f, cands, strict=True):
        n = math.floor(x * bankroll / (c.price + c.fee) + 1e-9)
        out.append(max(0, min(n, max_contracts)))
    return out
