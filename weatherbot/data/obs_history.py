"""Station observation history (METAR) from the Iowa Environmental Mesonet, for the
same-day "lock-in" test.

    python -m weatherbot.data.obs_history sample        # one station-month; check the format
    python -m weatherbot.data.obs_history backfill      # Nov 2024 -> yesterday, resumable
    python -m weatherbot.data.obs_history backfill --five-minute   # also 5-minute data

Two kinds of reports are stored separately:
- 'h': routine hourly METARs plus specials (IEM report types 3 and 4). Every METAR
  includes the temperature; the reports at about 23:51, 05:51, 11:51 and 17:51 UTC also
  carry the official 6-hour maximum (remark group 1snTTT, tenths of a degree C), which
  catches peaks between hourly readings.
- 'f': 5-minute ASOS data (IEM report type 1), stored only with --five-minute.

The CLI day is midnight to midnight local *standard* time (CLAUDE.md rule 6), so the
window is fixed in UTC by each station's standard offset, even during daylight time.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import io
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .logger import load_config, setup_logging
from .settlement_check import series_stations
from .storage import Storage, utcnow

log = logging.getLogger("weatherbot.obs_history")

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
USER_AGENT = "weatherbot-obs-history/0.1"
REPORT_TYPES = {"h": "3,4", "f": "1"}
_SIX_HR_MAX = re.compile(r"^1([01])(\d{3})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs_history (
    station TEXT NOT NULL,      -- ICAO, e.g. KMDW
    valid_ts INTEGER NOT NULL,  -- unix seconds (UTC) of the observation
    rtype TEXT NOT NULL,        -- 'h' hourly + specials, 'f' 5-minute
    tmpf REAL,                  -- temperature, F (from tenths of C when reported)
    max6_f REAL,                -- 6-hour max group in this report, F (NULL if none)
    PRIMARY KEY (station, valid_ts, rtype)
);
CREATE TABLE IF NOT EXISTS obs_fetch (
    station TEXT NOT NULL,
    month TEXT NOT NULL,        -- YYYY-MM
    rtype TEXT NOT NULL,
    status TEXT NOT NULL,       -- 'ok' | 'empty' | 'error'
    rows INTEGER,
    note TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (station, month, rtype)
);
"""


# --- pure helpers (tested) -------------------------------------------------------------


def lst_day_bounds(day: date, utc_offset_hours: int) -> tuple[int, int]:
    """[start, end) of a CLI day in unix seconds: local standard midnight to midnight."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(hours=utc_offset_hours)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def six_hour_max_f(metar: str | None) -> float | None:
    """The 6-hour maximum (remark group 1snTTT) in F, or None."""
    if not metar or " RMK " not in metar:
        return None
    for tok in metar.split(" RMK ", 1)[1].split():
        m = _SIX_HR_MAX.match(tok)
        if m:
            c = int(m.group(2)) / 10
            return c_to_f(-c if m.group(1) == "1" else c)
    return None


def parse_iem_csv(text: str) -> list[tuple[int, float | None, float | None]]:
    """IEM asos.py CSV -> [(valid_ts, tmpf, six_hour_max_f)], skipping empty rows."""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    out = []
    for r in csv.DictReader(io.StringIO("\n".join(lines))):
        valid = (r.get("valid") or "").strip()
        if not valid:
            continue
        try:
            t = int(datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp())
        except ValueError:
            continue
        raw = (r.get("tmpf") or "").strip()
        try:
            tmpf = float(raw) if raw not in ("", "M", "null") else None
        except ValueError:
            tmpf = None
        mx = six_hour_max_f(r.get("metar"))
        if tmpf is None and mx is None:
            continue
        out.append((t, tmpf, mx))
    return out


@dataclass
class DayObs:
    """One station's observations for one CLI day, for running-max queries."""

    start: int
    end: int
    obs: list[tuple[int, float | None, float | None]]  # (valid_ts, tmpf, max6_f)
    lag_s: int = 600  # a report counts only this long after its valid time

    def __post_init__(self) -> None:
        self.obs = sorted(o for o in self.obs if self.start <= o[0] < self.end)
        self._ts = [o[0] for o in self.obs]

    def running_max(self, t: int) -> tuple[float | None, float | None]:
        """(highest temperature so far, latest temperature) using reports public by t."""
        n = bisect.bisect_right(self._ts, t - self.lag_s)
        hi: float | None = None
        current: float | None = None
        for valid, tmpf, mx in self.obs[:n]:
            if tmpf is not None:
                hi = tmpf if hi is None else max(hi, tmpf)
                current = tmpf
            if mx is not None and valid - 6 * 3600 >= self.start:
                hi = mx if hi is None else max(hi, mx)
        return hi, current


# --- storage and fetching ---------------------------------------------------------------


def ensure_schema(storage: Storage) -> None:
    storage.conn.executescript(SCHEMA)


def iem_id(icao: str) -> str:
    return icao[1:] if len(icao) == 4 and icao.startswith("K") else icao


def month_range(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt


def months_between(start: date, end: date) -> list[str]:
    out, d = [], start.replace(day=1)
    while d <= end:
        out.append(d.strftime("%Y-%m"))
        d = month_range(d.strftime("%Y-%m"))[1]
    return out


def request_params(icao: str, month: str, rtype: str) -> dict[str, str]:
    first, nxt = month_range(month)
    return {
        "station": iem_id(icao),
        "data": "tmpf,metar",
        "year1": str(first.year),
        "month1": str(first.month),
        "day1": str(first.day),
        "year2": str(nxt.year),
        "month2": str(nxt.month),
        "day2": str(nxt.day),
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",
        "report_type": REPORT_TYPES[rtype],
    }


def save_month(
    storage: Storage, icao: str, month: str, rtype: str, rows: list[tuple[int, Any, Any]]
) -> None:
    storage.conn.execute("BEGIN")
    try:
        storage.conn.executemany(
            """INSERT INTO obs_history (station, valid_ts, rtype, tmpf, max6_f)
               VALUES (?,?,?,?,?)
               ON CONFLICT(station, valid_ts, rtype) DO UPDATE SET tmpf=excluded.tmpf,
                 max6_f=excluded.max6_f""",
            [(icao, t, rtype, tmpf, mx) for t, tmpf, mx in rows],
        )
        storage.conn.execute(
            """INSERT INTO obs_fetch (station, month, rtype, status, rows, note, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(station, month, rtype) DO UPDATE SET status=excluded.status,
                 rows=excluded.rows, note=excluded.note, fetched_at=excluded.fetched_at""",
            (icao, month, rtype, "ok" if rows else "empty", len(rows), "", utcnow()),
        )
        storage.conn.execute("COMMIT")
    except Exception:
        storage.conn.execute("ROLLBACK")
        raise


def record_error(storage: Storage, icao: str, month: str, rtype: str, note: str) -> None:
    storage.conn.execute(
        """INSERT INTO obs_fetch (station, month, rtype, status, rows, note, fetched_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(station, month, rtype) DO UPDATE SET status=excluded.status,
             note=excluded.note, fetched_at=excluded.fetched_at""",
        (icao, month, rtype, "error", 0, note[:500], utcnow()),
    )


async def fetch_month(http: httpx.AsyncClient, icao: str, month: str, rtype: str) -> str:
    delay = 5.0
    for attempt in range(4):
        try:
            resp = await http.get(IEM_ASOS_URL, params=request_params(icao, month, rtype))
            if resp.status_code < 400:
                return resp.text
            if resp.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                resp.raise_for_status()
        except httpx.TransportError:
            if attempt == 3:
                raise
        await asyncio.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


async def backfill(
    storage: Storage,
    stations: list[str],
    months: list[str],
    rtypes: list[str],
    pause_s: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    done = {
        (r["station"], r["month"], r["rtype"])
        for r in storage.query(
            "SELECT station, month, rtype FROM obs_fetch WHERE status != 'error'"
        )
    }
    current = datetime.now(UTC).strftime("%Y-%m")
    todo = [
        (st, m, rt)
        for rt in rtypes
        for st in stations
        for m in months
        if (st, m, rt) not in done or m >= current  # always refresh the current month
    ]
    stats = {"todo": len(todo), "ok": 0, "empty": 0, "error": 0, "rows": 0}
    async with httpx.AsyncClient(
        timeout=180, headers={"User-Agent": USER_AGENT}, transport=transport
    ) as http:
        for i, (st, m, rt) in enumerate(todo, 1):
            try:
                rows = parse_iem_csv(await fetch_month(http, st, m, rt))
                save_month(storage, st, m, rt, rows)
                stats["ok" if rows else "empty"] += 1
                stats["rows"] += len(rows)
            except Exception as exc:  # noqa: BLE001 - recorded; rerun retries
                record_error(storage, st, m, rt, repr(exc))
                stats["error"] += 1
            if i % 25 == 0 or i == len(todo):
                log.info(
                    "obs: %d/%d station-months (%d ok, %d empty, %d error), %d rows",
                    i, len(todo), stats["ok"], stats["empty"], stats["error"], stats["rows"],
                )  # fmt: skip
            await asyncio.sleep(pause_s)  # be gentle with IEM
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weatherbot.data.obs_history")
    parser.add_argument("command", choices=["sample", "backfill"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 11, 1))
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--five-minute", action="store_true", help="also fetch 5-minute data")
    args = parser.parse_args(argv)
    setup_logging("INFO")
    cfg = load_config(args.config)
    db_path = Path(cfg.get("logger", {}).get("db_path", "data_store/weatherbot.db"))
    if not db_path.exists():
        print(f"No database at {db_path}.")
        return 1
    storage = Storage(db_path)
    ensure_schema(storage)
    try:
        stations = sorted({st for st in series_stations(storage, cfg, None).values() if st})
        if args.command == "sample":
            return asyncio.run(_sample(db_path.parent))
        end = args.end or (datetime.now(UTC).date() - timedelta(days=1))
        months = months_between(args.start, end)
        rtypes = ["h", "f"] if args.five_minute else ["h"]
        print(
            f"Observation backfill: {len(stations)} stations x {len(months)} months, "
            f"types {rtypes}. Safe to stop and rerun."
        )
        stats = asyncio.run(backfill(storage, stations, months, rtypes))
        print(
            f"Done: {stats['ok']} ok, {stats['empty']} empty, {stats['error']} errors "
            f"(rerun to retry), {stats['rows']} observations saved."
        )
        for r in storage.query(
            "SELECT station, month, rtype, note FROM obs_fetch WHERE status='error' LIMIT 5"
        ):
            print(f"  error {r['station']} {r['month']} {r['rtype']}: {r['note'][:150]}")
    finally:
        storage.close()
    return 0


async def _sample(out_dir: Path) -> int:
    """Fetch one month of Chicago Midway hourly and 5-minute data; save the first lines."""
    month = (datetime.now(UTC).date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    out = out_dir / "obs_sample.txt"
    async with httpx.AsyncClient(timeout=180, headers={"User-Agent": USER_AGENT}) as http:
        with open(out, "w", encoding="utf-8") as fh:
            for rt in ("h", "f"):
                text = await fetch_month(http, "KMDW", month, rt)
                rows = parse_iem_csv(text)
                with_max = sum(1 for r in rows if r[2] is not None)
                print(
                    f"KMDW {month} type {rt}: {len(text) / 1e6:.2f} MB, {len(rows)} rows parsed, "
                    f"{with_max} with a 6-hour max"
                )
                fh.write(f"# type {rt}\n" + "\n".join(text.splitlines()[:15]) + "\n\n")
                await asyncio.sleep(1.0)
    print(f"Saved the first lines to {out}. Send it to Claude.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
