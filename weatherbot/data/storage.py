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

-- Historical daily highs from the Iowa Environmental Mesonet CLI archive (backfill).
CREATE TABLE IF NOT EXISTS cli_history (
    station TEXT NOT NULL,        -- ICAO, e.g. KMDW
    climate_date TEXT NOT NULL,   -- YYYY-MM-DD (local standard time day)
    high_f INTEGER,               -- NULL when missing
    low_f INTEGER,
    product TEXT,                 -- NWS product id the value came from
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (station, climate_date)
);

-- Settled Kalshi markets pulled for the settlement check (kept apart from `markets`
-- so the logger never polls these old tickers).
CREATE TABLE IF NOT EXISTS kalshi_settled (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series_ticker TEXT,
    event_date TEXT,              -- YYYY-MM-DD from the event ticker
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    result TEXT,
    expiration_value TEXT,        -- the value Kalshi settled on, as sent
    settlement_ts TEXT,
    source TEXT,                  -- 'live' | 'historical'
    rules_primary TEXT,
    fetched_at TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_settled_series_date ON kalshi_settled(series_ticker, event_date);

-- NBM probabilistic text bulletin (NBP): daily max-temperature distribution per station.
CREATE TABLE IF NOT EXISTS nbm_forecasts (
    station TEXT NOT NULL,        -- ICAO, e.g. KMDW
    cycle_time TEXT NOT NULL,     -- model run, ISO UTC
    available_at TEXT NOT NULL,   -- earliest time a backtest may use it (cycle + lag)
    valid_time TEXT NOT NULL,     -- bulletin column time, ISO UTC
    fhr INTEGER NOT NULL,         -- forecast hour
    target_date TEXT NOT NULL,    -- local day the max belongs to (YYYY-MM-DD)
    mean_f REAL, sd_f REAL,
    p10_f REAL, p25_f REAL, p50_f REAL, p75_f REAL, p90_f REAL,
    nbm_version TEXT,
    PRIMARY KEY (station, cycle_time, target_date)
);
CREATE INDEX IF NOT EXISTS ix_nbm_station_target ON nbm_forecasts(station, target_date);

-- One row per NBP file attempted, so the backfill can resume.
CREATE TABLE IF NOT EXISTS nbm_files (
    cycle_time TEXT PRIMARY KEY,
    status TEXT NOT NULL,         -- 'ok' | 'missing' | 'error'
    bytes INTEGER,
    rows INTEGER,
    note TEXT,
    fetched_at TEXT NOT NULL
);
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
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(markets)")}
        if "expiration_value" not in cols:
            self.conn.execute("ALTER TABLE markets ADD COLUMN expiration_value TEXT")

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
        self,
        ticker: str,
        status: str | None,
        result: str | None,
        value_e4: int | None,
        expiration_value: str | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE markets SET status=?, result=?, settlement_value_e4=?,
                 expiration_value=COALESCE(?, expiration_value),
                 settled_seen_at=COALESCE(settled_seen_at, ?), last_updated=?
               WHERE ticker=?""",
            (status, result, value_e4, expiration_value, utcnow(), utcnow(), ticker),
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

    # --- backfill: CLI history and settled markets --------------------------------

    def upsert_cli_history(
        self, station: str, rows: Iterable[tuple[str, int | None, int | None, str | None]]
    ) -> int:
        """rows: (climate_date, high_f, low_f, product). Returns rows written."""
        now = utcnow()
        data = [(station, d, hi, lo, prod, now) for d, hi, lo, prod in rows]
        self.conn.executemany(
            """INSERT INTO cli_history (station, climate_date, high_f, low_f, product, fetched_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(station, climate_date) DO UPDATE SET high_f=excluded.high_f,
                 low_f=excluded.low_f, product=excluded.product, fetched_at=excluded.fetched_at""",
            data,
        )
        return len(data)

    def upsert_settled_market(
        self, m: dict[str, Any], series_ticker: str, event_date: str | None, source: str
    ) -> None:
        self.conn.execute(
            """INSERT INTO kalshi_settled (ticker, event_ticker, series_ticker, event_date,
                 strike_type, floor_strike, cap_strike, result, expiration_value, settlement_ts,
                 source, rules_primary, fetched_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET result=excluded.result,
                 expiration_value=excluded.expiration_value,
                 settlement_ts=excluded.settlement_ts, fetched_at=excluded.fetched_at,
                 raw_json=excluded.raw_json""",
            (
                m["ticker"],
                m.get("event_ticker"),
                series_ticker,
                event_date,
                m.get("strike_type"),
                m.get("floor_strike"),
                m.get("cap_strike"),
                m.get("result") or None,
                m.get("expiration_value") or None,
                m.get("settlement_ts"),
                source,
                m.get("rules_primary"),
                utcnow(),
                json.dumps(m),
            ),
        )

    # --- NBM ---------------------------------------------------------------------------

    def upsert_nbm_forecasts(self, rows: Iterable[dict[str, Any]]) -> int:
        cols = [
            "station", "cycle_time", "available_at", "valid_time", "fhr", "target_date",
            "mean_f", "sd_f", "p10_f", "p25_f", "p50_f", "p75_f", "p90_f", "nbm_version",
        ]  # fmt: skip
        data = [tuple(r.get(c) for c in cols) for r in rows]
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols[2:])
        self.conn.executemany(
            f"""INSERT INTO nbm_forecasts ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})
                ON CONFLICT(station, cycle_time, target_date) DO UPDATE SET {updates}""",
            data,
        )
        return len(data)

    def record_nbm_file(
        self, cycle_time: str, status: str, size: int | None, rows: int | None, note: str = ""
    ) -> None:
        self.conn.execute(
            """INSERT INTO nbm_files (cycle_time, status, bytes, rows, note, fetched_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(cycle_time) DO UPDATE SET status=excluded.status, bytes=excluded.bytes,
                 rows=excluded.rows, note=excluded.note, fetched_at=excluded.fetched_at""",
            (cycle_time, status, size, rows, note[:500], utcnow()),
        )

    def nbm_done_cycles(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT cycle_time FROM nbm_files WHERE status IN ('ok', 'missing')"
        ).fetchall()
        return {r["cycle_time"] for r in rows}

    # --- events ----------------------------------------------------------------------

    def log_event(self, level: str, component: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, level, component, message) VALUES (?,?,?,?)",
            (utcnow(), level, component, message[:2000]),
        )

    # --- reporting -------------------------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()
