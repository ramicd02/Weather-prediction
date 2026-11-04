"""Logger tests against a fake Kalshi REST API (no network)."""

import asyncio

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from weatherbot.data.logger import DataLogger, status_report
from weatherbot.data.storage import Storage
from weatherbot.kalshi.auth import Signer
from weatherbot.kalshi.env import ConfigError
from weatherbot.kalshi.rest import KalshiReadClient

BASE = "https://api.test/trade-api/v2"

SERIES = {
    "series": [
        {"ticker": "KXHIGHCHI", "title": "Highest temperature in Chicago"},
        {"ticker": "KXHIGHNY", "title": "Highest temperature in NYC"},
        {"ticker": "KXBTC", "title": "Bitcoin"},
    ]
}
MARKETS = {
    "KXHIGHCHI": [
        {
            "ticker": "KXHIGHCHI-26NOV05-B54.5",
            "event_ticker": "KXHIGHCHI-26NOV05",
            "strike_type": "between",
            "floor_strike": 54,
            "cap_strike": 55,
            "status": "active",
            "close_time": "2026-11-06T04:59:00Z",
            "rules_primary": "highest temperature recorded at Chicago Midway",
        }
    ],
    "KXHIGHNY": [
        {
            "ticker": "KXHIGHNY-26NOV05-T60",
            "event_ticker": "KXHIGHNY-26NOV05",
            "strike_type": "greater",
            "floor_strike": 60,
            "status": "active",
            "close_time": "2020-01-01T00:00:00Z",
            "rules_primary": "Central Park, New York",
        }
    ],
}


def fake_api(scopes):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET", "logger must never send non-GET requests"
        assert "KALSHI-ACCESS-SIGNATURE" in request.headers
        path = request.url.path.removeprefix("/trade-api/v2")
        if path == "/series":
            return httpx.Response(200, json=SERIES)
        if path == "/markets":
            st = request.url.params["series_ticker"]
            return httpx.Response(200, json={"markets": MARKETS.get(st, []), "cursor": ""})
        if path == "/api_keys":
            return httpx.Response(200, json={"api_keys": [{"api_key_id": "k1", "scopes": scopes}]})
        if path.startswith("/markets/"):
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": path.split("/")[-1],
                        "status": "finalized",
                        "result": "no",
                        "settlement_value_dollars": "0.0000",
                    }
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def make_logger(scopes=("read",), cfg=None):
    transport, calls = fake_api(list(scopes))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rest = KalshiReadClient(BASE, Signer("k1", key), reads_per_s=1000, transport=transport)
    storage = Storage(":memory:")
    dl = DataLogger(cfg or {"logger": {}}, storage, rest, ws=None, nws=None)
    return dl, storage, calls


def test_discovery_tracks_only_prefix_series_and_maps_stations():
    dl, s, _ = make_logger()
    asyncio.run(dl.discover_once())
    assert dl.tracked == {"KXHIGHCHI-26NOV05-B54.5", "KXHIGHNY-26NOV05-T60"}
    rows = {r["ticker"]: r for r in s.query("SELECT * FROM markets")}
    chi = rows["KXHIGHCHI-26NOV05-B54.5"]
    assert (chi["station"], chi["station_source"]) == ("KMDW", "rules_match")
    assert chi["strike_type"] == "between" and chi["floor_strike"] == 54
    assert rows["KXHIGHNY-26NOV05-T60"]["station"] == "KNYC"


def test_write_scoped_key_is_refused():
    dl, _, _ = make_logger(scopes=("read", "write"))
    with pytest.raises(ConfigError):
        asyncio.run(dl.check_key_scope())


def test_write_scoped_key_allowed_only_with_flag():
    dl, _, _ = make_logger(
        scopes=("read", "write"), cfg={"logger": {"allow_write_scoped_key": True}}
    )
    asyncio.run(dl.check_key_scope())  # warns, does not raise


@pytest.mark.parametrize(
    "scopes",
    [("read", "write::trade"), ("read", "write::transfer"), ("write::block_trade_accept",)],
)
def test_granular_write_scopes_are_refused(scopes):
    dl, _, _ = make_logger(scopes=scopes)
    with pytest.raises(ConfigError):
        asyncio.run(dl.check_key_scope())


def test_granular_read_scopes_pass():
    dl, _, _ = make_logger(scopes=("read", "read::portfolio_balance", "read::block_trade_accept"))
    asyncio.run(dl.check_key_scope())


def test_read_only_key_passes():
    dl, _, _ = make_logger(scopes=("read",))
    asyncio.run(dl.check_key_scope())


def test_settlement_recorded_for_closed_markets(tmp_path):
    dl, s, _ = make_logger()
    asyncio.run(dl.discover_once())
    dl.settle_s = 0

    async def one_pass():
        task = asyncio.create_task(dl.settlement_loop())
        await asyncio.sleep(0.05)
        dl.stop()
        await task

    asyncio.run(one_pass())
    ny = s.query("SELECT * FROM markets WHERE ticker='KXHIGHNY-26NOV05-T60'")[0]
    assert ny["result"] == "no" and ny["settlement_value_e4"] == 0
    chi = s.query("SELECT * FROM markets WHERE ticker='KXHIGHCHI-26NOV05-B54.5'")[0]
    assert chi["result"] is None  # not closed yet


def test_status_report_runs(tmp_path):
    db = tmp_path / "w.db"
    s = Storage(db)
    s.upsert_market({"ticker": "M", "close_time": "2099-01-01T00:00:00Z"}, "KXHIGHX", None, None)
    s.log_event("error", "ws", "boom")
    out = status_report(s, db)
    assert "1 open" in out and "UNKNOWN" in out and "boom" in out


def test_rest_retries_on_429():
    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"series": []})

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rest = KalshiReadClient(
        BASE, Signer("k", key), reads_per_s=1000, transport=httpx.MockTransport(handler)
    )

    async def go():
        import weatherbot.kalshi.rest as r

        orig = r.asyncio.sleep

        async def fast(_):
            await orig(0)

        r.asyncio.sleep = fast
        try:
            return await rest.list_series()
        finally:
            r.asyncio.sleep = orig

    assert asyncio.run(go()) == [] and hits["n"] == 2
