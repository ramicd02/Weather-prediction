"""Async token bucket for client-side rate limiting."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float | None = None):
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be positive")
        self.rate = rate_per_s
        self.capacity = capacity if capacity is not None else rate_per_s
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep((tokens - self._tokens) / self.rate)
