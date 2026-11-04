"""Read-only Kalshi REST client (GET requests only)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .auth import Signer
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class KalshiReadClient:
    """GET-only client. There is intentionally no method that sends POST/PUT/DELETE."""

    def __init__(
        self,
        rest_base: str,
        signer: Signer,
        reads_per_s: float = 8,
        timeout_s: float = 20,
        max_retries: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.rest_base = rest_base.rstrip("/")
        self.signer = signer
        self.bucket = TokenBucket(reads_per_s)
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.rest_base}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            await self.bucket.acquire()
            headers = self.signer.headers("GET", url)
            try:
                resp = await self._http.get(url, params=clean, headers=headers)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise
                log.warning("kalshi GET %s transport error %s; retrying", path, exc)
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code not in RETRY_STATUSES or attempt == self.max_retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                log.warning("kalshi GET %s -> %s; backing off %.1fs", path, resp.status_code, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        raise RuntimeError("unreachable")

    async def paginate(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        params = dict(params or {})
        while True:
            data = await self.get(path, params)
            for item in data.get(key) or []:
                yield item
            cursor = data.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    # --- endpoints used by the logger -------------------------------------------------

    async def list_series(self) -> list[dict[str, Any]]:
        data = await self.get("/series")
        return list(data.get("series") or [])

    async def list_markets(
        self, series_ticker: str, status: str | None = "open"
    ) -> list[dict[str, Any]]:
        params = {"series_ticker": series_ticker, "status": status, "limit": 1000}
        return [m async for m in self.paginate("/markets", "markets", params)]

    async def get_market(self, ticker: str) -> dict[str, Any]:
        data = await self.get(f"/markets/{ticker}")
        return data.get("market") or {}

    async def list_api_keys(self) -> list[dict[str, Any]]:
        data = await self.get("/api_keys")
        return list(data.get("api_keys") or [])
