"""Price and size conversions.

Kalshi sends prices as 4-decimal dollar strings (e.g. "0.0800") because markets can
trade in sub-penny increments. We store prices as integers in units of 1/10,000 of a
dollar ("e4"), so 0.0800 -> 800 and $1.00 -> 10000. Sizes arrive as fixed-point
strings (e.g. "300.00") and are stored in hundredths of a contract ("e2").
No floats anywhere in this path.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

ONE_DOLLAR_E4 = 10_000


def _to_scaled_int(value: str | int | Decimal, scale: int, what: str) -> int:
    try:
        d = Decimal(str(value)) * scale
    except InvalidOperation as exc:
        raise ValueError(f"invalid {what}: {value!r}") from exc
    if d != d.to_integral_value():
        raise ValueError(f"{what} {value!r} has more precision than 1/{scale}")
    return int(d)


def dollars_to_e4(value: str | int | Decimal) -> int:
    """'0.0800' -> 800. Raises if the value has finer precision than 1/10,000."""
    p = _to_scaled_int(value, ONE_DOLLAR_E4, "price")
    if not 0 <= p <= ONE_DOLLAR_E4:
        raise ValueError(f"price out of range: {value!r}")
    return p


def size_to_e2(value: str | int | Decimal) -> int:
    """'300.00' -> 30000 (hundredths of a contract)."""
    return _to_scaled_int(value, 100, "size")


def no_bid_to_yes_ask(no_bid_e4: int) -> int:
    """A NO bid at X is a YES ask at (1 - X). In e4 units: 10000 - X."""
    if not 0 <= no_bid_e4 <= ONE_DOLLAR_E4:
        raise ValueError(f"price out of range: {no_bid_e4}")
    return ONE_DOLLAR_E4 - no_bid_e4


def e4_to_dollars_str(p: int) -> str:
    return f"{Decimal(p) / ONE_DOLLAR_E4:.4f}"
