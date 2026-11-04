"""api.weather.gov client: latest station observations and CLI text products."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"


class NwsClient:
    def __init__(
        self,
        user_agent: str,
        timeout_s: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=NWS_BASE,
            timeout=timeout_s,
            headers={"User-Agent": user_agent, "Accept": "application/geo+json"},
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, retries: int = 3) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(retries + 1):
            try:
                resp = await self._http.get(path)
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code not in (429, 500, 502, 503, 504) or attempt == retries:
                    resp.raise_for_status()
            except httpx.TransportError:
                if attempt == retries:
                    raise
            await asyncio.sleep(delay)
            delay *= 2
        raise RuntimeError("unreachable")

    async def latest_observation(self, station: str) -> dict[str, Any]:
        return await self._get(f"/stations/{station}/observations/latest")

    async def cli_product_list(self, cli_code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/products/types/CLI/locations/{cli_code}")
        return list(data.get("@graph") or [])

    async def product(self, product_id: str) -> dict[str, Any]:
        return await self._get(f"/products/{product_id}")


def parse_observation(payload: dict[str, Any]) -> tuple[str | None, float | None, str | None]:
    """(timestamp, temperature °C, raw METAR) from an observation GeoJSON payload."""
    props = payload.get("properties") or {}
    temp = (props.get("temperature") or {}).get("value")
    return props.get("timestamp"), temp, props.get("rawMessage")
