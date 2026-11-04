"""Settlement check: does Kalshi's settled high match the NWS Daily Climate Report?

Kalshi's KXHIGH rules name The Weather Company as the settlement source, not the NWS
Daily Climate Report (CLI). This tool measures how often the two agree, so we know
whether NWS data can stand in for the settling number.

    python -m weatherbot.data.settlement_check            # fetch, then report
    python -m weatherbot.data.settlement_check --no-fetch # report from saved data only
    python -m weatherbot.data.settlement_check --series KXHIGHCHI

What it does (read-only against Kalshi; GET requests only):
1. Pulls every settled market for each KXHIGH series: the live `/markets` endpoint
   for recent ones and `/historical/markets` for older ones.
2. Backfills NWS CLI daily highs for each settlement station from the Iowa
   Environmental Mesonet archive into `cli_history`.
3. For each city-day, checks whether the CLI high is consistent with every market's
   result, and compares it to Kalshi's `expiration_value` when that is a number.
4. Prints a per-city summary and writes every disagreement to
   data_store/settlement_mismatches.csv.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from weatherbot.kalshi.auth import Signer, load_private_key
from weatherbot.kalshi.env import load_dotenv, load_env
from weatherbot.kalshi.rest import KalshiReadClient
from weatherbot.model.brackets import yes_if

from .logger import DEFAULT_EXCLUDE, load_config, setup_logging
from .stations import resolve_station
from .storage import Storage

log = logging.getLogger("weatherbot.settlement_check")

IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"
IEM_USER_AGENT = "weatherbot-settlement-check/0.1"
_MONTH_NAMES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN"]
_MONTH_NAMES += ["JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES, 1)}
_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
TEMP_RANGE = range(-80, 141)  # plausible °F values when inferring from results


# --- pure helpers (tested) ------------------------------------------------------------


def event_date_from_ticker(event_ticker: str | None) -> date | None:
    """KXHIGHCHI-26NOV05 -> 2026-11-05. Only the date is read from the ticker."""
    m = _EVENT_DATE_RE.search(event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def parse_expiration_value(value: Any) -> float | None:
    """Kalshi's expiration_value as a number, or None if it isn't one."""
    if value is None:
        return None
    m = _NUMBER_RE.search(str(value))
    return float(m.group(0)) if m else None


def consistent_values(markets: list[dict[str, Any]]) -> list[int]:
    """Integer highs that agree with every yes/no result in one city-day event."""
    graded = [m for m in markets if m.get("result") in ("yes", "no")]
    if not graded:
        return []
    out = []
    for v in TEMP_RANGE:
        ok = True
        for m in graded:
            pays = yes_if(m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"), v)
            if pays is None:
                continue
            if pays != (m["result"] == "yes"):
                ok = False
                break
        if ok:
            out.append(v)
    return out


@dataclass
class EventCheck:
    series: str
    event_ticker: str
    event_date: str
    cli_high: int | None
    kalshi_value: float | None  # numeric expiration_value, when given
    implied: list[int]  # highs consistent with all results
    outcome_match: bool | None  # CLI high consistent with every result
    value_match: bool | None  # CLI high == kalshi_value
    markets: int


def check_event(
    series: str, event_ticker: str, markets: list[dict[str, Any]], cli_high: int | None
) -> EventCheck:
    values = {
        parse_expiration_value(m.get("expiration_value"))
        for m in markets
        if m.get("expiration_value")
    }
    values.discard(None)
    kalshi_value = values.pop() if len(values) == 1 else None
    implied = consistent_values(markets)
    outcome_match = value_match = None
    if cli_high is not None:
        graded = [m for m in markets if m.get("result") in ("yes", "no")]
        if graded:
            outcome_match = all(
                yes_if(m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"), cli_high)
                in (None, m["result"] == "yes")
                for m in graded
            )
        if kalshi_value is not None:
            value_match = float(cli_high) == kalshi_value
    ed = event_date_from_ticker(event_ticker)
    return EventCheck(
        series,
        event_ticker,
        ed.isoformat() if ed else "",
        cli_high,
        kalshi_value,
        implied,
        outcome_match,
        value_match,
        len(markets),
    )


def parse_iem_cli(payload: dict[str, Any]) -> list[tuple[str, int | None, int | None, str | None]]:
    """IEM /json/cli.py results -> (climate_date, high_f, low_f, product)."""

    def num(v: Any) -> int | None:
        if v is None or isinstance(v, str):
            try:
                return int(str(v)) if v is not None else None
            except ValueError:
                return None  # "M" missing, "T" trace, etc.
        return int(round(v))

    rows = []
    for r in payload.get("results") or []:
        d = r.get("valid")
        if not d:
            continue
        rows.append((str(d)[:10], num(r.get("high")), num(r.get("low")), r.get("product")))
    return rows


@dataclass
class SeriesSummary:
    series: str
    station: str | None
    events: int = 0
    with_cli: int = 0
    outcome_agree: int = 0
    outcome_disagree: int = 0
    value_compared: int = 0
    value_equal: int = 0
    diffs: dict[float, int] = field(default_factory=dict)
    first: str = ""
    last: str = ""


def summarize(checks: list[EventCheck], stations: dict[str, str | None]) -> list[SeriesSummary]:
    by: dict[str, SeriesSummary] = {}
    for c in checks:
        s = by.setdefault(c.series, SeriesSummary(c.series, stations.get(c.series)))
        s.events += 1
        if c.event_date:
            s.first = min(s.first or c.event_date, c.event_date)
            s.last = max(s.last, c.event_date)
        if c.cli_high is None:
            continue
        s.with_cli += 1
        if c.outcome_match is True:
            s.outcome_agree += 1
        elif c.outcome_match is False:
            s.outcome_disagree += 1
        if c.value_match is not None:
            s.value_compared += 1
            s.value_equal += int(c.value_match)
            if not c.value_match and c.kalshi_value is not None:
                d = round(c.kalshi_value - c.cli_high, 1)
                s.diffs[d] = s.diffs.get(d, 0) + 1
    return [by[k] for k in sorted(by)]


def format_report(summaries: list[SeriesSummary]) -> str:
    lines = [
        "Settlement check: Kalshi settled high vs NWS Daily Climate Report (CLI) high",
        "",
        f"{'Series':<16}{'Station':<8}{'Days':>6}{'w/CLI':>7}{'Outcome agree':>15}"
        f"{'Value equal':>14}  Kalshi-CLI diffs (deg F: count)   Range",
    ]
    tot = SeriesSummary("TOTAL", None)
    for s in summaries:
        oa = s.outcome_agree + s.outcome_disagree
        pct = f"{100 * s.outcome_agree / oa:.1f}%" if oa else "-"
        vpct = f"{100 * s.value_equal / s.value_compared:.1f}%" if s.value_compared else "-"
        diffs = ", ".join(f"{d:+g}:{n}" for d, n in sorted(s.diffs.items())) or "-"
        lines.append(
            f"{s.series:<16}{s.station or '-':<8}{s.events:>6}{s.with_cli:>7}"
            f"{pct:>15}{vpct:>14}  {diffs:<32} {s.first}..{s.last}"
        )
        tot.events += s.events
        tot.with_cli += s.with_cli
        tot.outcome_agree += s.outcome_agree
        tot.outcome_disagree += s.outcome_disagree
        tot.value_compared += s.value_compared
        tot.value_equal += s.value_equal
    oa = tot.outcome_agree + tot.outcome_disagree
    lines.append("")
    lines.append(
        f"TOTAL: {tot.events} city-days, {tot.with_cli} with a CLI high. "
        f"Outcome agree {tot.outcome_agree}/{oa}"
        + (f" ({100 * tot.outcome_agree / oa:.1f}%)" if oa else "")
        + f". Value equal {tot.value_equal}/{tot.value_compared}"
        + (f" ({100 * tot.value_equal / tot.value_compared:.1f}%)" if tot.value_compared else "")
        + "."
    )
    lines.append(
        "Outcome agree = the CLI high would have settled every market in that city-day the "
        "same way Kalshi did."
    )
    return "\n".join(lines)


# --- fetching -----------------------------------------------------------------------


async def fetch_settled(rest: KalshiReadClient, storage: Storage, series: str) -> int:
    """Pull settled markets for one series from the live and historical endpoints."""
    n = 0
    try:
        for m in await rest.list_settled_markets(series):
            ed = event_date_from_ticker(m.get("event_ticker"))
            storage.upsert_settled_market(m, series, ed.isoformat() if ed else None, "live")
            n += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: live settled markets failed: %s", series, exc)

    # Older markets moved to /historical/markets. We pass series_ticker and verify the
    # filter is honoured; if it isn't, stop rather than page through every market.
    params: dict[str, Any] = {"series_ticker": series, "limit": 1000}
    while True:
        try:
            data = await rest.get("/historical/markets", params)
        except httpx.HTTPStatusError as exc:
            log.warning("%s: historical markets unavailable (%s)", series, exc.response.status_code)
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: historical markets failed: %s", series, exc)
            break
        page = data.get("markets") or []
        foreign = [m for m in page if not str(m.get("event_ticker", "")).startswith(series + "-")]
        if foreign:
            log.warning(
                "%s: /historical/markets ignored the series filter (got %s); skipping history",
                series,
                foreign[0].get("ticker"),
            )
            break
        for m in page:
            ed = event_date_from_ticker(m.get("event_ticker"))
            storage.upsert_settled_market(m, series, ed.isoformat() if ed else None, "historical")
            n += 1
        cursor = data.get("cursor")
        if not cursor or not page:
            break
        params["cursor"] = cursor
    return n


async def fetch_cli_history(
    http: httpx.AsyncClient, storage: Storage, station: str, years: list[int]
) -> int:
    """Backfill IEM CLI highs for a station. Skips years already complete."""
    n = 0
    this_year = datetime.now(UTC).year
    for year in years:
        have = storage.query(
            "SELECT COUNT(*) c FROM cli_history WHERE station=? AND climate_date LIKE ?",
            (station, f"{year}-%"),
        )[0]["c"]
        if year < this_year and have >= 360:
            continue
        try:
            resp = await http.get(IEM_CLI_URL, params={"station": station, "year": year})
            resp.raise_for_status()
            rows = parse_iem_cli(resp.json())
        except Exception as exc:  # noqa: BLE001
            log.warning("IEM CLI %s %s failed: %s", station, year, exc)
            continue
        n += storage.upsert_cli_history(station, rows)
        await asyncio.sleep(1.0)  # be gentle with IEM
    return n


# --- main -----------------------------------------------------------------------------


def series_stations(
    storage: Storage, cfg: dict[str, Any], only: str | None
) -> dict[str, str | None]:
    """Weather series seen by the logger -> settlement station (config pin first)."""
    exclude = set(DEFAULT_EXCLUDE) | set(cfg.get("logger", {}).get("exclude_series") or [])
    overrides = cfg.get("stations") or {}
    out: dict[str, str | None] = {}
    for r in storage.query(
        "SELECT series_ticker s, MAX(rules_primary) rules FROM markets GROUP BY series_ticker"
    ):
        s = r["s"]
        if s in exclude or (only and s != only):
            continue
        out[s] = resolve_station(s, r["rules"], overrides)[0]
    return out


def run_checks(storage: Storage, stations: dict[str, str | None]) -> list[EventCheck]:
    checks: list[EventCheck] = []
    for series, station in stations.items():
        events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in storage.query(
            "SELECT * FROM kalshi_settled WHERE series_ticker=? ORDER BY event_ticker", (series,)
        ):
            events[r["event_ticker"]].append(dict(r))
        cli = {}
        if station:
            cli = {
                r["climate_date"]: r["high_f"]
                for r in storage.query(
                    "SELECT climate_date, high_f FROM cli_history WHERE station=?", (station,)
                )
            }
        for et, ms in events.items():
            ed = event_date_from_ticker(et)
            high = cli.get(ed.isoformat()) if ed else None
            checks.append(check_event(series, et, ms, high))
    return checks


def write_mismatches(checks: list[EventCheck], path: Path) -> int:
    bad = [c for c in checks if c.outcome_match is False or c.value_match is False]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["series", "event_ticker", "event_date", "cli_high_f", "kalshi_value",
             "highs_consistent_with_results", "outcome_match", "value_match"]
        )  # fmt: skip
        for c in sorted(bad, key=lambda c: (c.series, c.event_date)):
            rng = f"{c.implied[0]}..{c.implied[-1]}" if c.implied else ""
            w.writerow(
                [c.series, c.event_ticker, c.event_date, c.cli_high, c.kalshi_value, rng,
                 c.outcome_match, c.value_match]
            )  # fmt: skip
    return len(bad)


async def _fetch(cfg: dict[str, Any], storage: Storage, stations: dict[str, str | None]) -> None:
    env = load_env()
    signer = Signer(env.api_key_id, load_private_key(env.private_key_path))
    lc = cfg.get("logger", {})
    rest = KalshiReadClient(
        env.rest_base, signer, reads_per_s=float(lc.get("kalshi_reads_per_s", 8)) / 2
    )
    try:
        for series in stations:
            n = await fetch_settled(rest, storage, series)
            log.info("kalshi: %s settled markets saved: %d", series, n)
    finally:
        await rest.close()

    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": IEM_USER_AGENT}) as http:
        for series, station in stations.items():
            if not station:
                log.warning("%s: no station; pin it in config.yaml stations:", series)
                continue
            first = storage.query(
                "SELECT MIN(event_date) d FROM kalshi_settled WHERE series_ticker=?", (series,)
            )[0]["d"]
            if not first:
                continue
            years = list(range(int(first[:4]), datetime.now(UTC).year + 1))
            n = await fetch_cli_history(http, storage, station, years)
            log.info("iem: %s CLI history rows saved: %d (%s..)", station, n, years[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weatherbot.data.settlement_check")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--series", help="check one series only, e.g. KXHIGHCHI")
    parser.add_argument("--no-fetch", action="store_true", help="report from saved data only")
    args = parser.parse_args(argv)

    setup_logging("INFO")
    load_dotenv(args.env_file)
    cfg = load_config(args.config)
    db_path = Path(cfg.get("logger", {}).get("db_path", "data_store/weatherbot.db"))
    if not db_path.exists():
        print(f"No database at {db_path}. Run the logger first so the series are known.")
        return 1
    storage = Storage(db_path)
    try:
        stations = series_stations(storage, cfg, args.series)
        if not stations:
            print("No weather series found in the database yet.")
            return 1
        if not args.no_fetch:
            asyncio.run(_fetch(cfg, storage, stations))
        checks = run_checks(storage, stations)
        print(format_report(summarize(checks, stations)))
        out = db_path.parent / "settlement_mismatches.csv"
        n = write_mismatches(checks, out)
        print(f"\n{n} disagreeing city-days written to {out}")
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
