"""Authenticated Kalshi WebSocket connection (read-only market data)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .auth import Signer
from .env import WS_SIGN_PATH

log = logging.getLogger(__name__)


class KalshiWs:
    def __init__(self, ws_url: str, signer: Signer):
        self.ws_url = ws_url
        self.signer = signer
        self._conn: ClientConnection | None = None
        self._next_id = 1

    async def connect(self) -> None:
        headers = self.signer.headers("GET", WS_SIGN_PATH)
        # The server sends ping frames; the library answers them automatically.
        self._conn = await websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=30,
            max_size=2**24,
            open_timeout=20,
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def send(self, cmd: str, params: dict[str, Any]) -> int:
        if self._conn is None:
            raise RuntimeError("not connected")
        msg_id = self._next_id
        self._next_id += 1
        await self._conn.send(json.dumps({"id": msg_id, "cmd": cmd, "params": params}))
        return msg_id

    async def subscribe(self, channels: list[str], market_tickers: list[str]) -> int:
        return await self.send(
            "subscribe", {"channels": channels, "market_tickers": market_tickers}
        )

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("not connected")
        async for raw in self._conn:
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                log.warning("non-JSON websocket message: %.200s", raw)
