"""Backfill hourly Kalshi candlesticks for every settled weather market.

    python -m weatherbot.backtest.prices            # all settled markets, resumable
    python -m weatherbot.backtest.prices --series KXHIGHCHI --limit 50

Read-only (GET only). Markets settled before Kalshi's historical cutoff are served by
`/historical/markets/{ticker}/candlesticks`; newer ones by
`/series/{series}/markets/{ticker}/candlesticks`. We try the likely one first and fall
back to the other on a 404. Prices are stored as integer 1/10,000 dollars and sizes as
1/100 contracts, like the rest of the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from weatherbot.data.logger import DEFAULT_EXCLUDE, load_config, setup_logging
from weatherbot.data.storage import Storage
from weatherbot.kalshi.auth import Signer, load_private_key
from weatherbot.kalshi.env import load_dotenv, load_env
from weatherbot.kalshi.prices import dollars_to_e4, size_to_e2
from weatherbot.kalshi.rest import KalshiReadClient

log = logging.getLogger("weatherbot.prices")

PERIOD_MIN = 60
DEFAULT_LOOKBACK_H = 72  # markets open ~38h before close; take a margin


def to_unix(value: Any) -> int | None:
    """Unix seconds from an ISO string, unix seconds, or unix milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) or str(value).lstrip("-").isdigit():
        n = int(value)
        return n // 1000 if n > 10**11 else n
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _e4(obj: Any, key: str) -> int | None:
    """Dollar string at obj[key + '_dollars'] (live API) or obj[key] (historical API)."""
    if not isinstance(obj, dict):
        return None
    v = obj.get(f"{key}_dollars")
    if v in (None, ""):
        v = obj.get(key)
    if v in (None, "") or isinstance(v, int | float):
        return None  # only dollar strings are accepted; never guess cents
    try:
        return dollars_to_e4(v)
    except ValueError:
        return None


def _e2(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return size_to_e2(v)
    except ValueError:
        return None


def has_data(row: dict[str, Any]) -> bool:
    keys = ("yes_bid_e4", "yes_ask_e4", "price_e4", "volume_e2", "open_interest_e2")
    return any(row.get(k) is not None for k in keys)


def parse_candles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in payload.get("candlesticks") or []:
        ts = to_unix(c.get("end_period_ts"))
        if ts is None:
            continue
        price = c.get("price") or {}
        out.append(
            {
                "end_ts": ts,
                "yes_bid_e4": _e4(c.get("yes_bid"), "close"),
                "yes_ask_e4": _e4(c.get("yes_ask"), "close"),
                "price_e4": _e4(price, "close"),
                "price_low_e4": _e4(price, "low"),
                "price_high_e4": _e4(price, "high"),
                # Live API: volume_fp / open_interest_fp. Historical API: volume / open_interest.
                "volume_e2": _e2(c.get("volume_fp", c.get("volume"))),
                "open_interest_e2": _e2(c.get("open_interest_fp", c.get("open_interest"))),
            }
        )
    return out


async def historical_cutoff(rest: KalshiReadClient) -> int | None:
    try:
        data = await rest.get("/historical/cutoff")
    except Exception as exc:  # noqa: BLE001 - we can still work without it
        log.warning("historical cutoff unavailable (%s); will try both endpoints", exc)
        return None
    return to_unix(data.get("market_settled_ts"))


def _window(m: dict[str, Any]) -> tuple[int, int] | None:
    raw = json.loads(m["raw_json"]) if m.get("raw_json") else {}
    end = to_unix(raw.get("close_time")) or to_unix(m.get("settlement_ts"))
    if end is None:
        return None
    start = to_unix(raw.get("open_time")) or end - DEFAULT_LOOKBACK_H * 3600
    return start, end


async def fetch_market(
    rest: KalshiReadClient, storage: Storage, m: dict[str, Any], cutoff: int | None
) -> str:
    ticker, series = m["ticker"], m["series_ticker"]
    win = _window(m)
    if win is None:
        storage.record_candle_fetch(ticker, "error", None, 0, "no close time")
        return "error"
    start, end = win
    params = {"start_ts": start, "end_ts": end, "period_interval": PERIOD_MIN}
    live = ("live", f"/series/{series}/markets/{ticker}/candlesticks")
    hist = ("historical", f"/historical/markets/{ticker}/candlesticks")
    settled = to_unix(m.get("settlement_ts")) or end
    order = [hist, live] if cutoff is not None and settled < cutoff else [live, hist]
    last_err = ""
    for source, path in order:
        try:
            payload = await rest.get(path, params)
        except httpx.HTTPStatusError as exc:
            last_err = f"{source}: HTTP {exc.response.status_code}"
            if exc.response.status_code == 404:
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_err = f"{source}: {exc!r}"
            break
        rows = parse_candles(payload)
        if rows and not any(has_data(r) for r in rows):
            # Candles came back but no field was readable: a format we don't know.
            # Count it as an error, never as success, so it can't hide.
            keys = sorted((payload.get("candlesticks") or [{}])[0])
            storage.record_candle_fetch(
                ticker, "error", source, 0, f"{source}: unreadable candle fields {keys}"
            )
            return "error"
        storage.upsert_candles(ticker, rows)
        status = "ok" if rows else "empty"
        storage.record_candle_fetch(ticker, status, source, len(rows))
        return status
    storage.record_candle_fetch(ticker, "error", None, 0, last_err)
    return "error"


def mark_unreadable_for_refetch(storage: Storage) -> int:
    """Markets saved as 'ok' whose candles have no readable field at all -> refetch."""
    bad = storage.query(
        """SELECT f.ticker FROM kalshi_candle_fetch f
           WHERE f.status = 'ok' AND NOT EXISTS (
             SELECT 1 FROM kalshi_candles c WHERE c.ticker = f.ticker AND (
               c.yes_bid_e4 IS NOT NULL OR c.yes_ask_e4 IS NOT NULL OR c.price_e4 IS NOT NULL
               OR c.volume_e2 IS NOT NULL OR c.open_interest_e2 IS NOT NULL))"""
    )
    storage.conn.execute("BEGIN")  # one transaction: tens of thousands of rows
    try:
        for r in bad:
            storage.conn.execute("DELETE FROM kalshi_candles WHERE ticker = ?", (r["ticker"],))
            storage.record_candle_fetch(r["ticker"], "error", None, 0, "redo: unreadable prices")
        storage.conn.execute("COMMIT")
    except Exception:
        storage.conn.execute("ROLLBACK")
        raise
    return len(bad)


async def backfill(
    rest: KalshiReadClient,
    storage: Storage,
    series: str | None = None,
    limit: int | None = None,
    concurrency: int = 6,
) -> dict[str, int]:
    exclude = set(DEFAULT_EXCLUDE)
    sql = """SELECT s.* FROM kalshi_settled s
             LEFT JOIN kalshi_candle_fetch f ON f.ticker = s.ticker
             WHERE (f.ticker IS NULL OR f.status = 'error')"""
    params: list[Any] = []
    if series:
        sql += " AND s.series_ticker = ?"
        params.append(series)
    sql += " ORDER BY s.event_date DESC"
    todo = [dict(r) for r in storage.query(sql, params) if r["series_ticker"] not in exclude]
    if limit:
        todo = todo[:limit]
    stats = {"todo": len(todo), "ok": 0, "empty": 0, "error": 0}
    if not todo:
        return stats
    cutoff = await historical_cutoff(rest)
    sem = asyncio.Semaphore(concurrency)
    started = datetime.now(UTC)

    async def one(m: dict[str, Any]) -> None:
        async with sem:
            status = await fetch_market(rest, storage, m, cutoff)
        stats[status] += 1
        n = stats["ok"] + stats["empty"] + stats["error"]
        if n % 500 == 0 or n == len(todo):
            el = (datetime.now(UTC) - started).total_seconds()
            left = timedelta(seconds=int(el / n * (len(todo) - n)))
            log.info(
                "prices: %d/%d markets (%d ok, %d empty, %d error), ~%s left",
                n,
                len(todo),
                stats["ok"],
                stats["empty"],
                stats["error"],
                left,
            )

    await asyncio.gather(*(one(m) for m in todo))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weatherbot.backtest.prices")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--series", help="one series only, e.g. KXHIGHCHI")
    parser.add_argument("--limit", type=int, help="fetch at most this many markets (testing)")
    parser.add_argument(
        "--redo-unreadable",
        action="store_true",
        help="refetch markets whose saved candles have no readable prices",
    )
    args = parser.parse_args(argv)

    setup_logging("INFO")
    load_dotenv(args.env_file)
    cfg = load_config(args.config)
    db_path = Path(cfg.get("logger", {}).get("db_path", "data_store/weatherbot.db"))
    if not db_path.exists():
        print(f"No database at {db_path}.")
        return 1
    storage = Storage(db_path)
    n_settled = storage.query("SELECT COUNT(*) n FROM kalshi_settled")[0]["n"]
    if not n_settled:
        print("No settled markets saved. Run `python -m weatherbot.data.settlement_check` first.")
        storage.close()
        return 1
    if args.redo_unreadable:
        n = mark_unreadable_for_refetch(storage)
        print(f"Marked {n} markets with unreadable saved prices for refetch.")
    env = load_env()
    signer = Signer(env.api_key_id, load_private_key(env.private_key_path))
    reads = float(cfg.get("logger", {}).get("kalshi_reads_per_s", 8))
    # Share Kalshi's read limit with the running logger: use at most ~3/4 of our cap.
    rest = KalshiReadClient(env.rest_base, signer, reads_per_s=max(1.0, reads * 0.75))

    async def go() -> dict[str, int]:
        try:
            return await backfill(rest, storage, args.series, args.limit)
        finally:
            await rest.close()

    try:
        print(f"Kalshi price backfill: {n_settled} settled markets known. Safe to stop and rerun.")
        stats = asyncio.run(go())
        print(
            f"Done: {stats['ok']} with prices, {stats['empty']} with no candles, "
            f"{stats['error']} errors (rerun to retry)."
        )
        for r in storage.query(
            "SELECT ticker, note FROM kalshi_candle_fetch WHERE status='error' LIMIT 5"
        ):
            print(f"  error {r['ticker']}: {r['note']}")
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
