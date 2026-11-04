"""NBM forecast backfill from NOAA's public archive (NBP probabilistic text bulletin).

The NBP bulletin (`blend_nbptx.tHHz`) lists, for every station and run, the daily max
temperature distribution: QMD mean (TXNMN), standard deviation (TXNSD) and the 10th,
25th, 50th, 75th and 90th percentiles (TXNP1, TXNP2, TXNP5, TXNP7, TXNP9). The same
lines hold the daily min at the 12Z columns; we keep only the max columns (~00Z).

    python -m weatherbot.data.nbm sample                 # check the format on one file
    python -m weatherbot.data.nbm backfill               # Nov 2024 -> today, resumable
    python -m weatherbot.data.nbm backfill --start 2026-01-01 --cycles 13
    python -m weatherbot.data.nbm report                 # NBM vs NWS CLI accuracy per city

No look-ahead: each row gets `available_at = cycle_time + lag` (default 120 minutes,
override with `nbm.available_lag_minutes` in config.yaml). Backtests must filter on
`available_at`, never on `valid_time`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .logger import load_config, setup_logging
from .settlement_check import series_stations
from .storage import Storage

log = logging.getLogger("weatherbot.nbm")

NBM_BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
USER_AGENT = "weatherbot-nbm-backfill/0.1"
DEFAULT_CYCLES = (1, 7, 13, 19)  # the cycles with all NBP elements
DEFAULT_START = date(2024, 11, 1)
DEFAULT_LAG_MIN = 120

ELEMENTS = {
    "TXNMN": "mean_f",
    "TXNSD": "sd_f",
    "TXNP1": "p10_f",
    "TXNP2": "p25_f",
    "TXNP5": "p50_f",
    "TXNP7": "p75_f",
    "TXNP9": "p90_f",
}
_HEADER_RE = re.compile(r"^\s*([A-Z0-9]{3,6})\s+NBM\s+V?\s*(\d+(?:\.\d+)*)\s+NBP\b", re.I)
_ROW_RE = re.compile(r"^\s*(UTC|FHR|TXNMN|TXNSD|TXNP[12579])\b(.*)$")
_INT_RE = re.compile(r"-?\d+")
MISSING = -99


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def nbp_url(cycle: datetime) -> str:
    d, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    return f"{NBM_BASE}/blend.{d}/{hh}/text/blend_nbptx.t{hh}z"


def is_max_column(valid: datetime) -> bool:
    """Max is listed near 00Z (window 12Z-06Z); min near 12Z."""
    return valid.hour >= 18 or valid.hour <= 6


def max_target_date(valid: datetime) -> date:
    """The local calendar day a max column belongs to (00Z Nov 6 -> Nov 5 in the US)."""
    return (valid - timedelta(hours=12)).date()


def split_bulletins(text: str, wanted: set[str] | None = None) -> dict[str, tuple[str, str]]:
    """Station -> (nbm_version, block text). Keeps only `wanted` stations when given."""
    out: dict[str, tuple[str, str]] = {}
    cur: str | None = None
    ver = ""
    lines: list[str] = []

    def close() -> None:
        if cur and (wanted is None or cur in wanted):
            out[cur] = (ver, "\n".join(lines))

    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            close()
            cur, ver, lines = m.group(1).upper(), m.group(2), [line]
        elif cur is not None:
            lines.append(line)
    close()
    return out


@dataclass
class ParseResult:
    rows: list[dict[str, Any]]
    problem: str | None = None


def parse_nbp_block(
    station: str, version: str, block: str, cycle: datetime, lag_min: int = DEFAULT_LAG_MIN
) -> ParseResult:
    """Max-temperature rows from one station's NBP block.

    Columns are placed by forecast hour (valid = cycle + FHR). If any element line has a
    different number of values than the FHR line, the block is rejected rather than
    risk misaligned columns.
    """
    found: dict[str, list[int]] = {}
    for line in block.splitlines():
        m = _ROW_RE.match(line)
        if m and m.group(1) not in found:
            found[m.group(1)] = [int(x) for x in _INT_RE.findall(m.group(2))]
    fhrs = found.get("FHR")
    if not fhrs:
        return ParseResult([], "no FHR line")
    for name in ELEMENTS:
        vals = found.get(name)
        if vals is not None and len(vals) != len(fhrs):
            return ParseResult([], f"{name} has {len(vals)} values for {len(fhrs)} hours")
    if "TXNP5" not in found and "TXNMN" not in found:
        return ParseResult([], "no max-temperature lines")

    available = iso(cycle + timedelta(minutes=lag_min))
    rows = []
    for i, fhr in enumerate(fhrs):
        valid = cycle + timedelta(hours=fhr)
        if not is_max_column(valid):
            continue
        row: dict[str, Any] = {
            "station": station,
            "cycle_time": iso(cycle),
            "available_at": available,
            "valid_time": iso(valid),
            "fhr": fhr,
            "target_date": max_target_date(valid).isoformat(),
            "nbm_version": version,
        }
        for name, col in ELEMENTS.items():
            vals = found.get(name)
            v = vals[i] if vals is not None else None
            row[col] = None if v is None or v <= MISSING else v
        if row["p50_f"] is None and row["mean_f"] is None:
            continue
        rows.append(row)
    return ParseResult(rows)


def parse_nbp_file(
    text: str, cycle: datetime, stations: set[str], lag_min: int = DEFAULT_LAG_MIN
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    blocks = split_bulletins(text, stations)
    for st in sorted(stations - set(blocks)):
        problems.append(f"{st}: not in bulletin")
    for st, (ver, block) in blocks.items():
        res = parse_nbp_block(st, ver, block, cycle, lag_min)
        rows += res.rows
        if res.problem:
            problems.append(f"{st}: {res.problem}")
    return rows, problems


def cycles_between(start: date, end: date, hours: tuple[int, ...]) -> list[datetime]:
    out = []
    d = start
    while d <= end:
        for h in hours:
            out.append(datetime(d.year, d.month, d.day, h, tzinfo=UTC))
        d += timedelta(days=1)
    return out


# --- fetching -------------------------------------------------------------------------


async def fetch_text(http: httpx.AsyncClient, url: str, retries: int = 3) -> str | None:
    """File text, or None if the archive doesn't have it (404/403)."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            resp = await http.get(url)
            if resp.status_code in (403, 404):
                return None
            if resp.status_code < 400:
                return resp.text
            if attempt == retries:
                resp.raise_for_status()
        except httpx.TransportError:
            if attempt == retries:
                raise
        await asyncio.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


async def backfill(
    storage: Storage,
    stations: set[str],
    cycles: list[datetime],
    lag_min: int,
    concurrency: int = 4,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    done = storage.nbm_done_cycles()
    todo = [c for c in cycles if iso(c) not in done]
    stats = {"todo": len(todo), "ok": 0, "missing": 0, "error": 0, "rows": 0, "bytes": 0}
    if not todo:
        return stats
    sem = asyncio.Semaphore(concurrency)
    started = datetime.now(UTC)

    async def one(http: httpx.AsyncClient, cycle: datetime) -> None:
        async with sem:
            key = iso(cycle)
            try:
                text = await fetch_text(http, nbp_url(cycle))
            except Exception as exc:  # noqa: BLE001 - recorded, retried next run
                storage.record_nbm_file(key, "error", None, None, repr(exc))
                stats["error"] += 1
                return
            if text is None:
                storage.record_nbm_file(key, "missing", None, None)
                stats["missing"] += 1
                return
            rows, problems = parse_nbp_file(text, cycle, stations, lag_min)
            storage.upsert_nbm_forecasts(rows)
            note = "; ".join(problems[:5])
            status = "ok" if rows else "error"
            storage.record_nbm_file(key, status, len(text), len(rows), note)
            stats[status] += 1
            stats["rows"] += len(rows)
            stats["bytes"] += len(text)
            n = stats["ok"] + stats["missing"] + stats["error"]
            if n % 20 == 0 or n == len(todo):
                elapsed = (datetime.now(UTC) - started).total_seconds()
                eta = elapsed / n * (len(todo) - n)
                log.info(
                    "nbm: %d/%d files (%d ok, %d missing, %d error), %.0f MB, ~%.0f min left",
                    n,
                    len(todo),
                    stats["ok"],
                    stats["missing"],
                    stats["error"],
                    stats["bytes"] / 1e6,
                    eta / 60,
                )

    async with httpx.AsyncClient(
        timeout=120, headers={"User-Agent": USER_AGENT}, transport=transport
    ) as http:
        await asyncio.gather(*(one(http, c) for c in todo))
    return stats


# --- accuracy report ------------------------------------------------------------------


@dataclass
class Accuracy:
    station: str
    version: str
    n: int
    bias: float  # mean(CLI - p50): positive = NBM too cold
    mae: float  # mean |CLI - p50|
    cover80: float  # share of CLI highs inside [p10, p90]; ~0.80 if calibrated
    below50: float  # share of CLI highs below p50; ~0.50 if unbiased


def accuracy(storage: Storage, stations: list[str], cycle_hour: int = 13) -> list[Accuracy]:
    """Day-ahead skill: the `cycle_hour` run on day D forecasting the max for day D+1."""
    out = []
    for st in stations:
        rows = storage.query(
            """SELECT f.nbm_version v, f.p10_f, f.p50_f, f.p90_f, c.high_f
               FROM nbm_forecasts f JOIN cli_history c
                 ON c.station = f.station AND c.climate_date = f.target_date
               WHERE f.station = ? AND c.high_f IS NOT NULL AND f.p50_f IS NOT NULL
                 AND CAST(substr(f.cycle_time, 12, 2) AS INTEGER) = ?
                 AND f.target_date = date(substr(f.cycle_time, 1, 10), '+1 day')""",
            (st, cycle_hour),
        )
        by_ver: dict[str, list[Any]] = defaultdict(list)
        for r in rows:
            by_ver[r["v"] or "?"].append(r)
        for ver, rs in sorted(by_ver.items()):
            err = [r["high_f"] - r["p50_f"] for r in rs]
            inside = [
                r["p10_f"] <= r["high_f"] <= r["p90_f"]
                for r in rs
                if r["p10_f"] is not None and r["p90_f"] is not None
            ]
            out.append(
                Accuracy(
                    st,
                    ver,
                    len(rs),
                    statistics.fmean(err),
                    statistics.fmean(abs(e) for e in err),
                    sum(inside) / len(inside) if inside else float("nan"),
                    sum(r["high_f"] < r["p50_f"] for r in rs) / len(rs),
                )
            )
    return out


def format_accuracy(acc: list[Accuracy], station_series: dict[str, str]) -> str:
    lines = [
        "NBM day-ahead accuracy (13Z run, next day's high) vs NWS CLI high",
        "",
        f"{'Station':<8}{'Series':<14}{'NBM':>6}{'Days':>6}{'Bias F':>8}{'MAE F':>7}"
        f"{'In 10-90%':>11}{'Below p50':>11}",
    ]
    latest = max((a.version for a in acc), default="")
    for a in sorted(acc, key=lambda a: (a.version != latest, a.mae)):
        lines.append(
            f"{a.station:<8}{station_series.get(a.station, ''):<14}{a.version:>6}{a.n:>6}"
            f"{a.bias:>+8.2f}{a.mae:>7.2f}{a.cover80:>11.0%}{a.below50:>11.0%}"
        )
    lines += [
        "",
        "Bias = CLI minus NBM median (positive: NBM runs cold). MAE = typical miss.",
        "In 10-90% should be near 80% and Below p50 near 50% if NBM is well calibrated.",
        f"Sorted by MAE, current NBM version ({latest}) first.",
    ]
    return "\n".join(lines)


# --- main -----------------------------------------------------------------------------


def _stations(storage: Storage, cfg: dict[str, Any]) -> dict[str, str]:
    """Station -> series for weather series with a known station."""
    return {st: s for s, st in series_stations(storage, cfg, None).items() if st}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weatherbot.data.nbm")
    parser.add_argument("command", choices=["sample", "backfill", "report"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--cycles", default=",".join(str(h) for h in DEFAULT_CYCLES))
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)

    setup_logging("INFO")
    cfg = load_config(args.config)
    lag = int((cfg.get("nbm") or {}).get("available_lag_minutes", DEFAULT_LAG_MIN))
    db_path = Path(cfg.get("logger", {}).get("db_path", "data_store/weatherbot.db"))
    if not db_path.exists():
        print(f"No database at {db_path}. Run the logger first so the stations are known.")
        return 1
    storage = Storage(db_path)
    try:
        station_series = _stations(storage, cfg)
        stations = set(station_series)
        if not stations:
            print("No stations known yet. Run the logger and pin stations in config.yaml.")
            return 1

        if args.command == "sample":
            return asyncio.run(_sample(db_path.parent, stations, lag))

        if args.command == "backfill":
            hours = tuple(int(h) for h in args.cycles.split(","))
            yesterday = datetime.now(UTC).date() - timedelta(days=1)
            cycles = cycles_between(args.start, args.end or yesterday, hours)
            print(
                f"NBM backfill: {len(cycles)} runs, {args.start}..{args.end or yesterday}, "
                f"cycles {hours}, {len(stations)} stations. Safe to stop and rerun."
            )
            stats = asyncio.run(backfill(storage, stations, cycles, lag, args.concurrency))
            print(
                f"Done: {stats['ok']} ok, {stats['missing']} not in archive, "
                f"{stats['error']} errors (rerun to retry), {stats['rows']} forecasts saved."
            )
            bad = storage.query(
                "SELECT cycle_time, note FROM nbm_files WHERE note != '' ORDER BY cycle_time "
                "DESC LIMIT 5"
            )
            for r in bad:
                print(f"  note {r['cycle_time']}: {r['note'][:200]}")
            return 0

        acc = accuracy(storage, sorted(stations))
        if not acc:
            print("Nothing to compare yet. Run the settlement check and the NBM backfill first.")
            return 1
        print(format_accuracy(acc, station_series))
        return 0
    finally:
        storage.close()


async def _sample(out_dir: Path, stations: set[str], lag: int) -> int:
    """Fetch yesterday's 13Z bulletin, save a few station blocks, and show what parses."""
    cycle = datetime.now(UTC).replace(hour=13, minute=0, second=0, microsecond=0) - timedelta(
        days=1
    )
    url = nbp_url(cycle)
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": USER_AGENT}) as http:
        text = await fetch_text(http, url)
    if text is None:
        print(f"Not found in the archive: {url}")
        return 1
    blocks = split_bulletins(text, None)
    ours = split_bulletins(text, stations)
    out = out_dir / "nbm_sample.txt"
    keep = [s for s in ("KMDW", "KNYC", "KAUS") if s in ours] or sorted(ours)[:3]
    first_lines = "\n".join(text.splitlines()[:40])
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"# {url}\n# {len(text):,} bytes, {len(blocks)} station blocks\n\n")
        fh.write("# --- first 40 lines of the file ---\n" + first_lines + "\n\n")
        for st in keep:
            fh.write(f"# --- {st} ---\n{ours[st][1]}\n\n")
    rows, problems = parse_nbp_file(text, cycle, stations, lag)
    print(f"File: {url}")
    print(f"Size: {len(text) / 1e6:.1f} MB, {len(blocks)} station blocks in the file")
    print(f"Our stations found: {len(ours)}/{len(stations)}; forecast rows parsed: {len(rows)}")
    for p in problems[:10]:
        print(f"  problem: {p}")
    for r in [r for r in rows if r["station"] == (keep[0] if keep else "")][:3]:
        print(
            f"  {r['station']} {r['target_date']} (fhr {r['fhr']}): median {r['p50_f']}, "
            f"10-90% {r['p10_f']}-{r['p90_f']}, mean {r['mean_f']}, NBM v{r['nbm_version']}"
        )
    print(f"\nSaved a sample to {out}. Open it and send it to Claude.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
