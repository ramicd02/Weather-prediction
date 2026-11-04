"""Turns WebSocket messages into stored order-book snapshots and trades.

Kept separate from the network code so it can be tested with plain dicts.

Snapshot policy, per market:
- every fresh orderbook_snapshot is stored (reason "snapshot")
- any change to best bid/ask price or size is stored immediately ("top_of_book")
- otherwise a changed book is stored at most once per interval ("interval")
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from weatherbot.kalshi.prices import dollars_to_e4, size_to_e2

from .orderbook import OrderBook, SequenceGap, SequenceTracker
from .storage import Storage

log = logging.getLogger(__name__)


class BookRecorder:
    def __init__(self, storage: Storage, interval_s: float = 60):
        self.storage = storage
        self.interval_s = interval_s
        self.books: dict[str, OrderBook] = {}
        self.seq = SequenceTracker()
        self._last_top: dict[str, tuple] = {}
        self._dirty: dict[str, bool] = {}
        self._last_saved: dict[str, float] = {}

    def reset(self) -> None:
        """Forget all local state (call before reconnecting)."""
        self.books.clear()
        self.seq.reset()
        self._last_top.clear()
        self._dirty.clear()

    def handle(self, message: dict[str, Any]) -> None:
        """Process one WebSocket message. Raises SequenceGap if the stream skipped a seq."""
        mtype = message.get("type")
        msg = message.get("msg") or {}
        sid, seq = message.get("sid"), message.get("seq")

        if mtype in ("orderbook_snapshot", "orderbook_delta"):
            if sid is not None and seq is not None:
                self.seq.check(sid, seq)
            ticker = msg["market_ticker"]
            book = self.books.setdefault(ticker, OrderBook(ticker))
            if mtype == "orderbook_snapshot":
                book.apply_snapshot(msg, seq)
                self._save(book, "snapshot")
            else:
                book.apply_delta(msg, seq)
                self._after_change(book)
        elif mtype == "trade":
            self._record_trade(msg)
        elif mtype == "error":
            self.storage.log_event("error", "ws", f"server error: {msg}")
        # 'subscribed', 'ok' and other control messages need no action.

    def flush_interval(self, now: float | None = None) -> int:
        """Save books that changed since their last save and are due. Returns count saved."""
        now = time.monotonic() if now is None else now
        saved = 0
        for ticker, dirty in list(self._dirty.items()):
            if dirty and now - self._last_saved.get(ticker, 0) >= self.interval_s:
                self._save(self.books[ticker], "interval", now)
                saved += 1
        return saved

    # --- internals ------------------------------------------------------------------

    def _after_change(self, book: OrderBook) -> None:
        if book.top_of_book() != self._last_top.get(book.ticker):
            self._save(book, "top_of_book")
        else:
            self._dirty[book.ticker] = True

    def _save(self, book: OrderBook, reason: str, now: float | None = None) -> None:
        top = book.top_of_book()
        yes_levels, no_levels = book.ladders()
        self.storage.insert_book_snapshot(book.ticker, book.seq, reason, top, yes_levels, no_levels)
        self._last_top[book.ticker] = top
        self._dirty[book.ticker] = False
        self._last_saved[book.ticker] = time.monotonic() if now is None else now

    def _record_trade(self, msg: dict[str, Any]) -> None:
        try:
            price = msg.get("yes_price_dollars")
            count = msg.get("count_fp", msg.get("count"))
            ts_raw = msg.get("ts_ms")
            if ts_raw is not None:
                ts = datetime.fromtimestamp(ts_raw / 1000, UTC)
            else:
                ts = datetime.fromtimestamp(int(msg.get("ts", 0)), UTC)
            self.storage.insert_trade(
                trade_id=str(msg["trade_id"]),
                ticker=msg["market_ticker"],
                ts=ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                yes_price_e4=dollars_to_e4(price) if price is not None else None,
                count_e2=size_to_e2(count) if count is not None else None,
                taker_side=msg.get("taker_side"),
                raw=msg,
            )
        except (KeyError, ValueError, TypeError) as exc:
            self.storage.log_event("warning", "trades", f"bad trade message {msg}: {exc}")


__all__ = ["BookRecorder", "SequenceGap"]
