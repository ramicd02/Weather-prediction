"""Kalshi candlestick backfill (no network)."""

import asyncio

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from weatherbot.backtest.prices import backfill, parse_candles, to_unix
from weatherbot.data.storage import Storage
from weatherbot.kalshi.auth import Signer
from weatherbot.kalshi.rest import KalshiReadClient

BASE = "https://api.test/trade-api/v2"
CANDLE = {
    "end_period_ts": 1762300800,
    "yes_bid": {"close_dollars": "0.3100"},
    "yes_ask": {"close_dollars": "0.3400"},
    "price": {"close_dollars": "0.3300", "low_dollars": "0.3000", "high_dollars": "0.3500"},
    "volume_fp": "120.00",
    "open_interest_fp": "900.50",
}


def test_to_unix_accepts_iso_seconds_and_millis():
    assert to_unix("2025-11-05T04:59:00Z") == 1762318740
    assert to_unix("2025-11-05T04:59:00+00:00") == 1762318740
    assert to_unix(1762318740) == 1762318740
    assert to_unix(1762318740000) == 1762318740
    assert to_unix("") is None and to_unix("junk") is None


def test_parse_candles_to_integer_units():
    [c] = parse_candles({"candlesticks": [CANDLE]})
    assert (c["yes_bid_e4"], c["yes_ask_e4"], c["price_e4"]) == (3100, 3400, 3300)
    assert (c["price_low_e4"], c["price_high_e4"]) == (3000, 3500)
    assert (c["volume_e2"], c["open_interest_e2"]) == (12000, 90050)


def test_parse_candles_no_trade_hour():
    c = dict(CANDLE, price={"close_dollars": None}, volume_fp="0.00")
    [row] = parse_candles({"candlesticks": [c]})
    assert row["price_e4"] is None and row["yes_bid_e4"] == 3100 and row["volume_e2"] == 0


def _market(ticker, series="KXHIGHCHI", settled="2025-01-02T06:00:00Z"):
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "strike_type": "between",
        "floor_strike": 30,
        "cap_strike": 31,
        "result": "no",
        "settlement_ts": settled,
        "open_time": "2024-12-31T15:00:00Z",
        "close_time": "2025-01-02T04:59:00Z",
    }


def _client(handler):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return KalshiReadClient(
        BASE, Signer("k1", key), reads_per_s=1000, transport=httpx.MockTransport(handler)
    )


def test_backfill_uses_historical_for_old_markets_and_resumes():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        path = req.url.path.removeprefix("/trade-api/v2")
        calls.append(path)
        if path == "/historical/cutoff":
            return httpx.Response(200, json={"market_settled_ts": "2026-01-01T00:00:00Z"})
        if path.startswith("/historical/markets/"):
            assert req.url.params["period_interval"] == "60"
            assert int(req.url.params["start_ts"]) < int(req.url.params["end_ts"])
            return httpx.Response(200, json={"candlesticks": [CANDLE]})
        return httpx.Response(404)

    s = Storage(":memory:")
    s.upsert_settled_market(_market("KXHIGHCHI-25JAN01-B30.5"), "KXHIGHCHI", "2025-01-01", "h")
    s.upsert_settled_market(_market("KXHIGHMIA-25JAN01-B80.5"), "KXHIGHMIA", "2025-01-01", "h")
    rest = _client(handler)
    stats = asyncio.run(backfill(rest, s))
    # Miami is still priced: trading excludes it, but data and diagnostics don't.
    assert stats["ok"] == 2 and not any("/series/" in c for c in calls)
    assert s.query("SELECT yes_ask_e4 FROM kalshi_candles LIMIT 1")[0]["yes_ask_e4"] == 3400
    again = asyncio.run(backfill(rest, s))
    assert again["todo"] == 0


def test_backfill_falls_back_to_live_endpoint_on_404():
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/trade-api/v2")
        if path == "/historical/cutoff":
            return httpx.Response(500)
        if path.startswith("/series/KXHIGHCHI/markets/"):
            return httpx.Response(200, json={"candlesticks": []})
        return httpx.Response(404)

    s = Storage(":memory:")
    s.upsert_settled_market(
        _market("KXHIGHCHI-26NOV01-B50.5", settled="2026-11-02T06:00:00Z"),
        "KXHIGHCHI",
        "2026-11-01",
        "live",
    )
    rest = _client(handler)
    rest.max_retries = 0
    stats = asyncio.run(backfill(rest, s))
    assert stats["empty"] == 1
    row = s.query("SELECT status, source FROM kalshi_candle_fetch")[0]
    assert (row["status"], row["source"]) == ("empty", "live")
