"""End-to-end: logger <-> a local fake Kalshi WebSocket server (no internet)."""

import asyncio
import json

import websockets
from cryptography.hazmat.primitives.asymmetric import rsa

from weatherbot.data.logger import DataLogger
from weatherbot.data.storage import Storage
from weatherbot.kalshi.auth import Signer
from weatherbot.kalshi.ws import KalshiWs

TICKER = "KXHIGHCHI-26NOV05-B54.5"


def test_ws_records_books_and_trades_and_resyncs_after_gap():
    seen = {"connections": 0, "auth_ok": True, "subscribed": []}

    async def server(conn):
        seen["connections"] += 1
        h = conn.request.headers
        seen["auth_ok"] &= all(
            k in h
            for k in ("KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE", "KALSHI-ACCESS-TIMESTAMP")
        )
        sub = json.loads(await conn.recv())
        seen["subscribed"].append(sub)
        msgs = [
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": 1,
                "msg": {
                    "market_ticker": TICKER,
                    "yes_dollars_fp": [["0.3000", "100.00"]],
                    "no_dollars_fp": [["0.6500", "40.00"]],
                },
            },
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 2,
                "msg": {
                    "market_ticker": TICKER,
                    "side": "yes",
                    "price_dollars": "0.3100",
                    "delta_fp": "10.00",
                },
            },
            {
                "type": "trade",
                "sid": 2,
                "seq": 1,
                "msg": {
                    "trade_id": f"t{seen['connections']}",
                    "market_ticker": TICKER,
                    "yes_price_dollars": "0.3100",
                    "count_fp": "5.00",
                    "taker_side": "yes",
                    "ts": 1762300000,
                },
            },
        ]
        if seen["connections"] == 1:
            # Skip seq 3 -> the logger must detect the gap and reconnect.
            msgs.append(
                {
                    "type": "orderbook_delta",
                    "sid": 1,
                    "seq": 9,
                    "msg": {
                        "market_ticker": TICKER,
                        "side": "yes",
                        "price_dollars": "0.3100",
                        "delta_fp": "1.00",
                    },
                }
            )
        for m in msgs:
            await conn.send(json.dumps(m))
        await asyncio.sleep(5)

    async def go():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ws = KalshiWs(f"ws://127.0.0.1:{port}", Signer("k1", key))
            storage = Storage(":memory:")
            dl = DataLogger({"logger": {}}, storage, rest=None, ws=ws, nws=None)
            dl.tracked = {TICKER}
            task = asyncio.create_task(dl.ws_loop())
            for _ in range(100):
                await asyncio.sleep(0.05)
                if seen["connections"] >= 2 and storage.query(
                    "SELECT 1 FROM trades WHERE trade_id='t2'"
                ):
                    break
            dl.stop()
            await asyncio.wait_for(task, 5)
            return storage

    storage = asyncio.run(go())
    assert seen["auth_ok"]
    assert seen["subscribed"][0]["cmd"] == "subscribe"
    assert seen["subscribed"][0]["params"]["channels"] == ["orderbook_delta", "trade"]
    assert seen["subscribed"][0]["params"]["market_tickers"] == [TICKER]
    assert seen["connections"] >= 2, "gap should trigger a reconnect"
    reasons = [r["reason"] for r in storage.query("SELECT reason FROM book_snapshots")]
    assert reasons.count("snapshot") >= 2 and "top_of_book" in reasons
    last = storage.query("SELECT * FROM book_snapshots ORDER BY id DESC LIMIT 1")[0]
    assert last["best_yes_bid_e4"] == 3100 and last["best_yes_ask_e4"] == 3500
    assert len(storage.query("SELECT * FROM trades")) == 2
    assert (
        storage.query("SELECT COUNT(*) n FROM events WHERE message LIKE '%sequence gap%'")[0]["n"]
        >= 1
    )
