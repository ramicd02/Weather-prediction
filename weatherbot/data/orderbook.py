"""Local order book maintained from WebSocket orderbook_snapshot / orderbook_delta messages.

Kalshi books hold bids only: YES bids and NO bids. The best YES ask is derived from the
best NO bid (1 - price).

Sequence numbers (`seq`) are per subscription (`sid`), not per market. Any gap means the
local books for that subscription can no longer be trusted; the caller must resync
(the logger reconnects, which delivers fresh snapshots).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from weatherbot.kalshi.prices import dollars_to_e4, no_bid_to_yes_ask, size_to_e2


class SequenceGap(Exception):
    def __init__(self, sid: int, expected: int, got: int):
        super().__init__(f"sid {sid}: expected seq {expected}, got {got}")
        self.sid, self.expected, self.got = sid, expected, got


class SequenceTracker:
    """Checks that seq increases by exactly 1 per sid."""

    def __init__(self) -> None:
        self._last: dict[int, int] = {}

    def check(self, sid: int, seq: int) -> None:
        last = self._last.get(sid)
        if last is not None and seq != last + 1:
            raise SequenceGap(sid, last + 1, seq)
        self._last[sid] = seq

    def reset(self) -> None:
        self._last.clear()


def _levels(raw: Any) -> dict[int, int]:
    """[["0.0800", "300.00"], ...] -> {800: 30000}. Zero-size levels are dropped."""
    out: dict[int, int] = {}
    for level in raw or []:
        price, size = level[0], level[1]
        s = size_to_e2(size)
        if s > 0:
            out[dollars_to_e4(price)] = s
    return out


@dataclass
class OrderBook:
    ticker: str
    yes: dict[int, int] = field(default_factory=dict)  # price_e4 -> size_e2 (YES bids)
    no: dict[int, int] = field(default_factory=dict)  # price_e4 -> size_e2 (NO bids)
    seq: int | None = None
    has_snapshot: bool = False

    def apply_snapshot(self, msg: dict[str, Any], seq: int | None = None) -> None:
        self.yes = _levels(msg.get("yes_dollars_fp", msg.get("yes_dollars")))
        self.no = _levels(msg.get("no_dollars_fp", msg.get("no_dollars")))
        self.seq = seq
        self.has_snapshot = True

    def apply_delta(self, msg: dict[str, Any], seq: int | None = None) -> None:
        if not self.has_snapshot:
            raise ValueError(f"{self.ticker}: delta before snapshot")
        side = msg["side"]
        book = self.yes if side == "yes" else self.no if side == "no" else None
        if book is None:
            raise ValueError(f"unknown side {side!r}")
        price = dollars_to_e4(msg.get("price_dollars", msg.get("price")))
        delta = size_to_e2(msg.get("delta_fp", msg.get("delta")))
        new_size = book.get(price, 0) + delta
        if new_size < 0:
            raise ValueError(f"{self.ticker}: negative size at {price} after delta {delta}")
        if new_size == 0:
            book.pop(price, None)
        else:
            book[price] = new_size
        self.seq = seq

    # --- derived values --------------------------------------------------------------

    @property
    def best_yes_bid(self) -> int | None:
        return max(self.yes) if self.yes else None

    @property
    def best_no_bid(self) -> int | None:
        return max(self.no) if self.no else None

    @property
    def best_yes_ask(self) -> int | None:
        b = self.best_no_bid
        return None if b is None else no_bid_to_yes_ask(b)

    def top_of_book(self) -> tuple[int | None, int | None, int | None, int | None]:
        """(best_yes_bid, size, best_yes_ask, size) in e4 / e2 units."""
        yb, nb = self.best_yes_bid, self.best_no_bid
        return (
            yb,
            self.yes.get(yb) if yb is not None else None,
            self.best_yes_ask,
            self.no.get(nb) if nb is not None else None,
        )

    def ladders(self) -> tuple[list[list[int]], list[list[int]]]:
        """Both ladders as [[price_e4, size_e2], ...], best price first."""
        return (
            [[p, s] for p, s in sorted(self.yes.items(), reverse=True)],
            [[p, s] for p, s in sorted(self.no.items(), reverse=True)],
        )
