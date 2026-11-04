"""24/7 read-only data logger for Kalshi KXHIGH markets plus NWS weather data.

Run:    python -m weatherbot.data.logger            (same as `run`)
Status: python -m weatherbot.data.logger status

This module never places, amends or cancels orders.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import random
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from weatherbot.kalshi.auth import Signer, load_private_key
from weatherbot.kalshi.env import ConfigError, load_dotenv, load_env
from weatherbot.kalshi.prices import dollars_to_e4
from weatherbot.kalshi.rest import KalshiReadClient
from weatherbot.kalshi.ws import KalshiWs

from .book_recorder import BookRecorder
from .cli_parser import parse_cli
from .nws import NwsClient, parse_observation
from .orderbook import SequenceGap
from .stations import cli_code_for, resolve_station
from .storage import Storage, utcnow

log = logging.getLogger("weatherbot.logger")

PLACEHOLDER_UA = "REPLACE_WITH_YOUR_EMAIL"


# --- logging -------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --- the service ---------------------------------------------------------------------


class DataLogger:
    def __init__(
        self,
        cfg: dict[str, Any],
        storage: Storage,
        rest: KalshiReadClient,
        ws: KalshiWs,
        nws: NwsClient | None,
    ):
        lc = cfg.get("logger", {})
        self.cfg = cfg
        self.storage = storage
        self.rest = rest
        self.ws = ws
        self.nws = nws
        self.prefix: str = lc.get("series_prefix", "KXHIGH")
        self.discovery_s = float(lc.get("discovery_interval_s", 900))
        self.settle_s = float(lc.get("settlement_poll_interval_s", 1800))
        self.obs_s = float(lc.get("obs_interval_s", 600))
        self.cli_s = float(lc.get("cli_interval_s", 10800))
        self.allow_write_key = bool(lc.get("allow_write_scoped_key", False))
        self.station_overrides: dict[str, str] = cfg.get("stations") or {}
        self.cli_overrides: dict[str, str] = cfg.get("cli_codes") or {}
        self.recorder = BookRecorder(storage, float(lc.get("snapshot_interval_s", 60)))
        self.tracked: set[str] = set()
        self._tracked_changed = asyncio.Event()
        self._stop = asyncio.Event()

    def event(self, level: str, component: str, message: str) -> None:
        getattr(log, "warning" if level == "warning" else level, log.info)(
            "%s: %s", component, message
        )
        self.storage.log_event(level, component, message)

    # --- startup safety check ---------------------------------------------------------

    async def check_key_scope(self) -> None:
        try:
            keys = await self.rest.list_api_keys()
        except Exception as exc:  # noqa: BLE001 - any failure means "can't verify"
            self.event(
                "warning",
                "startup",
                f"could not verify API key scopes ({exc}); make sure the key is read-only",
            )
            return
        mine = next((k for k in keys if k.get("api_key_id") == self.rest.signer.api_key_id), None)
        if mine is None:
            self.event(
                "warning",
                "startup",
                "API key not found in /api_keys; cannot verify scopes. Use a read-only key.",
            )
            return
        scopes = mine.get("scopes") or ["read", "write"]  # omitted scopes = full access
        # Block both broad "write" and granular scopes such as "write::trade".
        write_scopes = [s for s in scopes if str(s).split("::", 1)[0] == "write"]
        if write_scopes:
            msg = (
                "this API key has WRITE scope. The logger only needs read access. "
                "Create a read-only key, or set logger.allow_write_scoped_key: true."
            )
            if not self.allow_write_key:
                raise ConfigError(msg)
            self.event("warning", "startup", msg)
        else:
            log.info("startup: API key is read-only (scopes=%s)", scopes)

    # --- discovery ---------------------------------------------------------------------

    async def discover_once(self) -> None:
        series = [
            s
            for s in await self.rest.list_series()
            if str(s.get("ticker", "")).startswith(self.prefix)
        ]
        found: set[str] = set()
        new = 0
        for s in series:
            self.storage.upsert_series(s)
            try:
                markets = await self.rest.list_markets(s["ticker"], status="open")
            except Exception as exc:  # noqa: BLE001 - one series must not stop discovery
                self.event("error", "discovery", f"{s['ticker']}: {exc}")
                continue
            for m in markets:
                station, source = resolve_station(
                    s["ticker"], m.get("rules_primary"), self.station_overrides
                )
                if self.storage.upsert_market(m, s["ticker"], station, source):
                    new += 1
                found.add(m["ticker"])
        log.info("discovery: %d series, %d open markets (%d new)", len(series), len(found), new)
        if found != self.tracked:
            self.tracked = found
            self._tracked_changed.set()

    async def discovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.discover_once()
            except Exception as exc:  # noqa: BLE001
                self.event("error", "discovery", repr(exc))
            await self._sleep(self.discovery_s)

    # --- websocket ---------------------------------------------------------------------

    async def ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self.tracked:
                await self._sleep(5)
                continue
            self._tracked_changed.clear()
            tickers = sorted(self.tracked)
            self.recorder.reset()
            try:
                await self.ws.connect()
                await self.ws.subscribe(["orderbook_delta", "trade"], tickers)
                log.info("ws: subscribed to %d markets", len(tickers))
                backoff = 1.0
                await self._consume()
            except SequenceGap as gap:
                self.event("warning", "ws", f"sequence gap ({gap}); resyncing")
                await self._sleep(1 + random.random())
            except Exception as exc:  # noqa: BLE001
                self.event("error", "ws", f"connection error: {exc!r}")
                await self._sleep(backoff + random.random())
                backoff = min(backoff * 2, 60)
            finally:
                await self.ws.close()

    async def _consume(self) -> None:
        async def reader() -> None:
            async for message in self.ws.messages():
                try:
                    self.recorder.handle(message)
                except SequenceGap:
                    raise
                except (KeyError, ValueError) as exc:
                    # A bad delta means the local book can't be trusted: resync.
                    raise SequenceGap(-1, -1, -1) from exc
            raise ConnectionError("websocket closed by server")

        read_task = asyncio.create_task(reader())
        change_task = asyncio.create_task(self._tracked_changed.wait())
        stop_task = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            {read_task, change_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if read_task in done:
            read_task.result()  # re-raise SequenceGap / errors
        elif change_task in done:
            log.info("ws: tracked markets changed; reconnecting with the new set")

    async def flush_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.recorder.flush_interval()
            except Exception as exc:  # noqa: BLE001
                self.event("error", "flush", repr(exc))
            await self._sleep(5)

    # --- settlements -------------------------------------------------------------------

    async def settlement_loop(self) -> None:
        while not self._stop.is_set():
            for ticker in self.storage.markets_awaiting_result(utcnow()):
                try:
                    m = await self.rest.get_market(ticker)
                except Exception as exc:  # noqa: BLE001
                    self.event("error", "settlement", f"{ticker}: {exc}")
                    continue
                status, result = m.get("status"), m.get("result") or None
                value = m.get("settlement_value_dollars")
                value_e4 = dollars_to_e4(value) if value not in (None, "") else None
                if result or status in ("determined", "finalized", "amended", "disputed"):
                    self.storage.record_settlement(ticker, status, result, value_e4)
            await self._sleep(self.settle_s)

    # --- NWS ---------------------------------------------------------------------------

    def _stations(self) -> list[str]:
        return sorted({st for _, st in self.storage.stations()})

    async def observation_loop(self) -> None:
        assert self.nws is not None
        while not self._stop.is_set():
            for station in self._stations():
                try:
                    payload = await self.nws.latest_observation(station)
                    obs_ts, temp_c, raw = parse_observation(payload)
                    if obs_ts:
                        self.storage.insert_observation(station, obs_ts, temp_c, raw, payload)
                except Exception as exc:  # noqa: BLE001 - one station must not stop the rest
                    self.event("warning", "nws_obs", f"{station}: {exc}")
                await asyncio.sleep(0.5)  # be gentle with api.weather.gov
            await self._sleep(self.obs_s)

    async def cli_loop(self) -> None:
        assert self.nws is not None
        while not self._stop.is_set():
            for station in self._stations():
                code = cli_code_for(station, self.cli_overrides)
                try:
                    products = await self.nws.cli_product_list(code)
                except Exception as exc:  # noqa: BLE001
                    self.event("warning", "nws_cli", f"{station}/{code}: {exc}")
                    continue
                for p in products[:6]:  # newest first; covers ~2 days of prelim + final
                    pid = p.get("id")
                    if not pid or self.storage.has_cli_product(pid):
                        continue
                    try:
                        prod = await self.nws.product(pid)
                        text = prod.get("productText") or ""
                        rep = parse_cli(text)
                        self.storage.upsert_cli_report(
                            {
                                "product_id": pid,
                                "station": station,
                                "cli_code": code,
                                "issuance_time": prod.get("issuanceTime") or p.get("issuanceTime"),
                                "climate_date": rep.climate_date.isoformat()
                                if rep.climate_date
                                else None,
                                "is_preliminary": rep.is_preliminary,
                                "max_f": rep.max_f,
                                "min_f": rep.min_f,
                                "raw_text": text,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.event("warning", "nws_cli", f"{station} product {pid}: {exc}")
                    await asyncio.sleep(0.5)
            await self._sleep(self.cli_s)

    # --- lifecycle ---------------------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.check_key_scope()
        tasks = [
            self.discovery_loop(),
            self.ws_loop(),
            self.flush_loop(),
            self.settlement_loop(),
        ]
        if self.nws is not None:
            tasks += [self.observation_loop(), self.cli_loop()]
        log.info("logger started")
        await asyncio.gather(*tasks)
        log.info("logger stopped")


# --- status report -------------------------------------------------------------------


def status_report(storage: Storage, db_path: Path) -> str:
    q = storage.query
    lines: list[str] = []
    size_mb = sum(p.stat().st_size for p in db_path.parent.glob(db_path.name + "*")) / 1e6
    lines.append(f"Database: {db_path}  ({size_mb:.1f} MB)")

    now = utcnow()
    open_markets = q("SELECT COUNT(*) n FROM markets WHERE close_time > ?", (now,))[0]["n"]
    total = q("SELECT COUNT(*) n FROM markets")[0]["n"]
    settled = q("SELECT COUNT(*) n FROM markets WHERE result IS NOT NULL")[0]["n"]
    lines.append(f"Markets: {open_markets} open, {total} seen, {settled} with a result")

    lines.append("")
    lines.append(
        "Rows: "
        + ", ".join(
            f"{t}={q(f'SELECT COUNT(*) n FROM {t}')[0]['n']}"
            for t in ("book_snapshots", "trades", "observations", "cli_reports")
        )
    )

    lines.append("")
    lines.append("Order books (latest snapshot per series):")
    for r in q(
        """SELECT m.series_ticker s, COUNT(DISTINCT b.ticker) mk, MAX(b.ts) last
           FROM book_snapshots b JOIN markets m ON m.ticker=b.ticker
           GROUP BY m.series_ticker ORDER BY m.series_ticker"""
    ):
        lines.append(f"  {r['s']:<16} markets={r['mk']:<4} last={r['last']}")

    lines.append("")
    lines.append("Stations:")
    for r in q(
        """SELECT m.series_ticker s, m.station st, m.station_source src,
             (SELECT MAX(obs_ts) FROM observations o WHERE o.station=m.station) last_obs,
             (SELECT MAX(climate_date) FROM cli_reports c WHERE c.station=m.station) last_cli
           FROM markets m GROUP BY m.series_ticker ORDER BY m.series_ticker"""
    ):
        flag = (
            ""
            if r["src"] == "config"
            else "  <- confirm in config.yaml"
            if r["st"]
            else "  <- UNKNOWN: add to config.yaml stations"
        )
        lines.append(
            f"  {r['s']:<16} {r['st'] or '-':<6} ({r['src'] or 'none'}) "
            f"obs={r['last_obs'] or '-'} cli={r['last_cli'] or '-'}{flag}"
        )

    lines.append("")
    hour_ago = datetime.fromtimestamp(datetime.now(UTC).timestamp() - 3600, UTC)
    errors = q(
        """SELECT ts, level, component, message FROM events
           WHERE level IN ('error','warning') AND ts > ? ORDER BY ts DESC LIMIT 20""",
        (hour_ago.isoformat(timespec="milliseconds").replace("+00:00", "Z"),),
    )
    lines.append(f"Errors/warnings in the last hour: {len(errors)}")
    for e in errors:
        lines.append(f"  {e['ts']} {e['level']:<7} {e['component']}: {e['message'][:160]}")
    return "\n".join(lines)


# --- entry point ---------------------------------------------------------------------


def build(cfg: dict[str, Any]) -> tuple[DataLogger, Storage]:
    env = load_env()
    if env.name != "prod":
        log.warning(
            "KALSHI_ENV=%s: the demo exchange has almost no real order flow. "
            "Set KALSHI_ENV=prod (with a read-only key) to collect real data.",
            env.name,
        )
    signer = Signer(env.api_key_id, load_private_key(env.private_key_path))
    lc = cfg.get("logger", {})
    storage = Storage(lc.get("db_path", "data_store/weatherbot.db"))
    rest = KalshiReadClient(
        env.rest_base, signer, reads_per_s=float(lc.get("kalshi_reads_per_s", 8))
    )
    ws = KalshiWs(env.ws_url, signer)
    ua = (cfg.get("nws") or {}).get("user_agent", "")
    nws = None
    if not ua or PLACEHOLDER_UA in ua:
        log.warning(
            "nws.user_agent still has the placeholder; NWS logging is OFF. "
            "Put your contact email in config.yaml to enable it."
        )
    else:
        nws = NwsClient(ua)
    return DataLogger(cfg, storage, rest, ws, nws), storage


async def _run(cfg: dict[str, Any]) -> None:
    dl, storage = build(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, dl.stop)
    try:
        await dl.run()
    finally:
        await dl.rest.close()
        if dl.nws:
            await dl.nws.close()
        storage.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weatherbot.data.logger")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "status"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    load_dotenv(args.env_file)
    cfg = load_config(args.config)

    if args.command == "status":
        db_path = Path(cfg.get("logger", {}).get("db_path", "data_store/weatherbot.db"))
        if not db_path.exists():
            print(f"No database yet at {db_path}. Start the logger first.")
            return 1
        storage = Storage(db_path)
        print(status_report(storage, db_path))
        storage.close()
        return 0

    try:
        asyncio.run(_run(cfg))
    except ConfigError as exc:
        log.error("config: %s", exc)
        return 2
    except KeyboardInterrupt:
        log.info("stopped by user")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
