"""SQLite storage for the data logger.

One file, WAL mode. Prices are integer e4 (1/10,000 dollar), sizes integer e2
(1/100 contract), timestamps ISO-8601 UTC strings. All access goes through the
Storage class so the backend can be swapped later.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    ticker TEXT PRIMARY KEY,
    title TEXT,
    settlement_sources_json TEXT,
    first_seen TEXT NOT NULL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series_ticker TEXT,
    title TEXT,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    open_time TEXT,
    close_time TEXT,
    rules_primary TEXT,
    rules_secondary TEXT,
    station TEXT,
    station_source TEXT,          -- 'config' | 'rules_match' | NULL (unknown)
    status TEXT,
    result TEXT,                  -- yes | no | scalar | void, once determined
    settlement_value_e4 INTEGER,
    settled_seen_at TEXT,
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_markets_series ON markets(series_ticker);

CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    seq INTEGER,
    reason TEXT NOT NULL,         -- snapshot | top_of_book | interval
    best_yes_bid_e4 INTEGER,
    best_yes_bid_size_e2 INTEGER,
    best_yes_ask_e4 INTEGER,
    best_yes_ask_size_e2 INTEGER,
    yes_levels_json TEXT NOT NULL, -- [[price_e4, size_e2], ...] YES bids, best first
    no_levels_json TEXT NOT NULL   -- [[price_e4, size_e2], ...] NO bids, best first
);
CREATE INDEX IF NOT EXISTS ix_book_ticker_ts ON book_snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    yes_price_e4 INTEGER,
    count_e2 INTEGER,
    taker_side TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_ticker_ts ON trades(ticker, ts);

CREATE TABLE IF NOT EXISTS observations (
    station TEXT NOT NULL,
    obs_ts TEXT NOT NULL,
    temp_c REAL,                  -- as reported by NWS (Celsius, may carry tenths)
    raw_message TEXT,             -- METAR text
    fetched_at TEXT NOT NULL,
    raw_json TEXT,
    PRIMARY KEY (station, obs_ts)
);

CREATE TABLE IF NOT EXISTS cli_reports (
    product_id TEXT PRIMARY KEY,
    station TEXT NOT NULL,
    cli_code TEXT NOT NULL,
    issuance_time TEXT,
    climate_date TEXT,            -- YYYY-MM-DD the report summarizes
    is_preliminary INTEGER,       -- 1 preliminary, 0 final, NULL unknown
    max_f INTEGER,                -- NULL when missing (MM)
    min_f INTEGER,
    fetched_at TEXT NOT NULL,
    raw_text TEXT
);
CREATE INDEX IF NOT EXISTS ix_cli_station_date ON cli_reports(station, climate_date);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- series / markets ------------------------------------------------------------

    def upsert_series(self, s: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO series (ticker, title, settlement_sources_json, first_seen, raw_json)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET title=excluded.title,
                 settlement_sources_json=excluded.settlement_sources_json,
                 raw_json=excluded.raw_json""",
            (
                s["ticker"],
                s.get("title"),
                json.dumps(s.get("settlement_sources")),
                utcnow(),
                json.dumps(s),
            ),
        )

    def upsert_market(
        self,
        m: dict[str, Any],
        series_ticker: str,
        station: str | None,
        station_source: str | None,
    ) -> bool:
        """Insert or refresh a market. Returns True if it was new."""
        now = utcnow()
        existed = self.conn.execute(
            "SELECT 1 FROM markets WHERE ticker=?", (m["ticker"],)
        ).fetchone()
        self.conn.execute(
            """INSERT INTO markets (ticker, event_ticker, series_ticker, title, strike_type,
                 floor_strike, cap_strike, open_time, close_time, rules_primary, rules_secondary,
                 station, station_source, status, first_seen, last_updated, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET status=excluded.status,
                 close_time=excluded.close_time, station=excluded.station,
                 station_source=excluded.station_source, rules_primary=excluded.rules_primary,
                 rules_secondary=excluded.rules_secondary, last_updated=excluded.last_updated,
                 raw_json=excluded.raw_json""",
            (
                m["ticker"],
                m.get("event_ticker"),
                series_ticker,
                m.get("title"),
                m.get("strike_type"),
                m.get("floor_strike"),
                m.get("cap_strike"),
                m.get("open_time"),
                m.get("close_time"),
                m.get("rules_primary"),
                m.get("rules_secondary"),
                station,
                station_source,
                m.get("status"),
                now,
                now,
                json.dumps(m),
            ),
        )
        return existed is None

    def record_settlement(
        self, ticker: str, status: str | None, result: str | None, value_e4: int | None
    ) -> None:
        self.conn.execute(
            """UPDATE markets SET status=?, result=?, settlement_value_e4=?,
                 settled_seen_at=COALESCE(settled_seen_at, ?), last_updated=?
               WHERE ticker=?""",
            (status, result, value_e4, utcnow(), utcnow(), ticker),
        )

    def markets_awaiting_result(self, now_iso: str) -> list[str]:
        rows = self.conn.execute(
            """SELECT ticker FROM markets
               WHERE (result IS NULL OR result = '' OR status NOT IN ('finalized'))
                 AND close_time IS NOT NULL AND close_time < ?""",
            (now_iso,),
        ).fetchall()
        return [r["ticker"] for r in rows]

    def stations(self) -> list[tuple[str, str]]:
        """Distinct (series_ticker, station) pairs with a known station."""
        rows = self.conn.execute(
            """SELECT DISTINCT series_ticker, station FROM markets
               WHERE station IS NOT NULL ORDER BY series_ticker"""
        ).fetchall()
        return [(r["series_ticker"], r["station"]) for r in rows]

    # --- books / trades --------------------------------------------------------------

    def insert_book_snapshot(
        self,
        ticker: str,
        seq: int | None,
        reason: str,
        top: tuple[int | None, int | None, int | None, int | None],
        yes_levels: list[list[int]],
        no_levels: list[list[int]],
        ts: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO book_snapshots (ticker, ts, seq, reason, best_yes_bid_e4,
                 best_yes_bid_size_e2, best_yes_ask_e4, best_yes_ask_size_e2,
                 yes_levels_json, no_levels_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                ticker,
                ts or utcnow(),
                seq,
                reason,
                *top,
                json.dumps(yes_levels, separators=(",", ":")),
                json.dumps(no_levels, separators=(",", ":")),
            ),
        )

    def insert_trade(
        self,
        trade_id: str,
        ticker: str,
        ts: str,
        yes_price_e4: int | None,
        count_e2: int | None,
        taker_side: str | None,
        raw: dict[str, Any],
    ) -> bool:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO trades (trade_id, ticker, ts, yes_price_e4, count_e2,
                 taker_side, raw_json) VALUES (?,?,?,?,?,?,?)""",
            (trade_id, ticker, ts, yes_price_e4, count_e2, taker_side, json.dumps(raw)),
        )
        return cur.rowcount == 1

    # --- weather ---------------------------------------------------------------------

    def insert_observation(
        self,
        station: str,
        obs_ts: str,
        temp_c: float | None,
        raw_message: str | None,
        raw: dict[str, Any],
    ) -> bool:
        """Returns False when this (station, obs_ts) was already stored."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO observations (station, obs_ts, temp_c, raw_message,
                 fetched_at, raw_json) VALUES (?,?,?,?,?,?)""",
            (station, obs_ts, temp_c, raw_message, utcnow(), json.dumps(raw)),
        )
        return cur.rowcount == 1

    def upsert_cli_report(self, r: dict[str, Any]) -> bool:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO cli_reports (product_id, station, cli_code, issuance_time,
                 climate_date, is_preliminary, max_f, min_f, fetched_at, raw_text)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                r["product_id"],
                r["station"],
                r["cli_code"],
                r.get("issuance_time"),
                r.get("climate_date"),
                None if r.get("is_preliminary") is None else int(r["is_preliminary"]),
                r.get("max_f"),
                r.get("min_f"),
                utcnow(),
                r.get("raw_text"),
            ),
        )
        return cur.rowcount == 1

    def has_cli_product(self, product_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM cli_reports WHERE product_id=?", (product_id,)
            ).fetchone()
            is not None
        )

    # --- events ----------------------------------------------------------------------

    def log_event(self, level: str, component: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, level, component, message) VALUES (?,?,?,?)",
            (utcnow(), level, component, message[:2000]),
        )

    # --- reporting -------------------------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()
