"""Kalshi trading fees (fee schedule effective 2026-07-07).

Taker: ceil(0.07 x C x P x (1 - P)) dollars, rounded up to the next cent.
Maker: ceil(0.0175 x M x C x P x (1 - P)); M is 0 unless the series is on Kalshi's
non-standard list. KXHIGH isn't listed, so maker fees are 0 for our markets.
All exact fees are integers in 1/10,000 dollars (e4), computed without floats.
"""

from __future__ import annotations

ONE_E4 = 10_000


def _fee_cents(rate_per_10k: int, contracts: int, price_e4: int, multiplier: int) -> int:
    # rate_per_10k: 700 for 0.07, 175 for 0.0175 (in 1/10,000 units).
    # fee in dollars = rate/1e4 * C * (p/1e4) * (1 - p/1e4); in cents multiply by 100.
    num = rate_per_10k * multiplier * contracts * price_e4 * (ONE_E4 - price_e4) * 100
    den = ONE_E4**3
    return -(-num // den)  # ceiling division


def taker_fee_e4(contracts: int, price_e4: int, multiplier: int = 1) -> int:
    if contracts <= 0:
        return 0
    return _fee_cents(700, contracts, price_e4, multiplier) * 100


def maker_fee_e4(contracts: int, price_e4: int, multiplier: int = 0) -> int:
    if contracts <= 0 or multiplier == 0:
        return 0
    return _fee_cents(175, contracts, price_e4, multiplier) * 100


def fee_per_contract(price: float, rate: float = 0.07) -> float:
    """Unrounded taker fee per contract in dollars, for sizing and edge checks."""
    return rate * price * (1 - price)
