"""NBM NBP bulletin parsing, backfill resume, and accuracy (no network)."""

import asyncio
from datetime import UTC, date, datetime

import httpx

from weatherbot.data.nbm import (
    accuracy,
    backfill,
    cycles_between,
    is_max_column,
    max_target_date,
    nbm_version_for,
    nbp_url,
    parse_nbp_block,
    parse_nbp_file,
    split_bulletins,
)
from weatherbot.data.storage import Storage


def row(label: str, values: list) -> str:
    """One bulletin row laid out like the real file: right-aligned 3-wide values, a '|'
    after every second column (the 12Z one), first column alone before the first '|'."""
    out = f" {label:<6}"
    for i, v in enumerate(values):
        cell = f"{'' if v is None else v:>3}"
        sep = "|" if i % 2 == 0 else " "
        out += cell + (sep if i < len(values) - 1 else "")
    return out


# Shape copied from a real NBP file (blend.20261103/13): header without a version number,
# a "1" page line, numeric and ICAO ids, '|' separators and blank cells. Values made up.
FHR = [23, 35, 47, 59, 71]
BULLETIN = "\n".join(
    [
        "1",
        "",
        " 086092  NBM  NBP GUIDANCE    11/03/2026  1300 UTC",
        row("UTC", [12, 0, 12, 0, 12]),
        row("FHR", FHR),
        row("TXNMN", [76, 82, 75, 82, 73]),
        "1",
        "",
        " KMDW    NBM  NBP GUIDANCE    11/03/2026  1300 UTC",
        "    WED 04| THU 05| FRI 06|",
        row("UTC", [12, 0, 12, 0, 12]),
        row("FHR", FHR),
        row("TXNMN", [45, 62, 40, 58, 38]),
        row("TXNSD", [1, 2, 3, None, 3]),
        row("TXNP1", [44, 59, 36, 54, 35]),
        row("TXNP2", [45, 61, 38, 56, 36]),
        row("TXNP5", [45, 62, 40, 58, 38]),
        row("TXNP7", [46, 63, 42, 60, 40]),
        row("TXNP9", [47, 65, 44, 62, 41]),
        row("W24P1", [None, 16, None, 12, None]),
        " KNYC    NBM V4.2 NBP GUIDANCE    11/03/2026  1300 UTC",
        row("FHR", FHR),
        row("TXNP5", [50, -99, 48, 57, 45]),
        row("TXNMN", [50, 60, 48, 57, 45]),
    ]
)
CYCLE = datetime(2026, 11, 3, 13, tzinfo=UTC)


def test_url_and_cycles():
    assert nbp_url(CYCLE) == (
        "https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.20261103/13/text/blend_nbptx.t13z"
    )
    cs = cycles_between(date(2026, 1, 1), date(2026, 1, 2), (1, 13))
    assert [c.isoformat() for c in cs][-1] == "2026-01-02T13:00:00+00:00" and len(cs) == 4


def test_max_columns_and_target_day():
    assert is_max_column(datetime(2026, 11, 5, 0, tzinfo=UTC))
    assert is_max_column(datetime(2026, 11, 5, 1, tzinfo=UTC))
    assert not is_max_column(datetime(2026, 11, 5, 12, tzinfo=UTC))
    # 00Z on Nov 6 is the evening of Nov 5 in the US.
    assert max_target_date(datetime(2026, 11, 6, 0, tzinfo=UTC)) == date(2026, 11, 5)


def test_split_reads_real_headers():
    assert set(split_bulletins(BULLETIN)) == {"086092", "KMDW", "KNYC"}
    blocks = split_bulletins(BULLETIN, {"KMDW", "KNYC"})
    assert set(blocks) == {"KMDW", "KNYC"}
    assert blocks["KMDW"][0] == "" and blocks["KNYC"][0] == "4.2"


def test_three_letter_ids_map_to_icao():
    text = BULLETIN.replace(" KMDW    NBM", " MDW     NBM")
    assert "KMDW" in split_bulletins(text, {"KMDW"})


def test_version_inferred_from_run_date():
    assert nbm_version_for(datetime(2026, 4, 30, 12, tzinfo=UTC)) == "4.x"
    assert nbm_version_for(datetime(2026, 5, 1, 13, tzinfo=UTC)) == "transition"
    assert nbm_version_for(datetime(2026, 5, 4, 13, tzinfo=UTC)) == "5.0"


def test_parse_block_takes_max_columns_with_no_lookahead_stamp():
    ver, block = split_bulletins(BULLETIN, {"KMDW"})["KMDW"]
    res = parse_nbp_block("KMDW", ver, block, CYCLE, lag_min=120)
    assert res.problem is None
    # 13Z Nov 3 run: FHR 35 -> 00Z Nov 5 = Wed Nov 4's max; FHR 59 -> Thu Nov 5's.
    assert [r["target_date"] for r in res.rows] == ["2026-11-04", "2026-11-05"]
    tomorrow = res.rows[0]
    assert tomorrow["fhr"] == 35 and tomorrow["valid_time"] == "2026-11-05T00:00:00Z"
    assert (tomorrow["p10_f"], tomorrow["p50_f"], tomorrow["p90_f"]) == (59, 62, 65)
    assert (tomorrow["p25_f"], tomorrow["p75_f"]) == (61, 63)
    assert tomorrow["mean_f"] == 62 and tomorrow["sd_f"] == 2
    assert tomorrow["available_at"] == "2026-11-03T15:00:00Z"
    assert tomorrow["cycle_time"] == "2026-11-03T13:00:00Z"
    assert tomorrow["nbm_version"] == "5.0"


def test_blank_cell_stays_in_its_column():
    ver, block = split_bulletins(BULLETIN, {"KMDW"})["KMDW"]
    rows = parse_nbp_block("KMDW", ver, block, CYCLE).rows
    assert rows[1]["sd_f"] is None and rows[1]["p50_f"] == 58


def test_missing_values_become_none():
    ver, block = split_bulletins(BULLETIN, {"KNYC"})["KNYC"]
    rows = parse_nbp_block("KNYC", ver, block, CYCLE).rows
    assert rows[0]["p50_f"] is None and rows[0]["mean_f"] == 60
    assert rows[0]["nbm_version"] == "4.2"


def test_misaligned_block_is_rejected():
    block = row("FHR", FHR) + "\n" + row("TXNP5", [45, 62, 40, 58, 38]) + " 99\n"
    res = parse_nbp_block("KMDW", "", block, CYCLE)
    assert res.rows == [] and "TXNP5" in res.problem


def test_negative_values_packed_together():
    block = row("FHR", FHR) + "\n" + row("TXNP5", [-12, -5, -15, -10, -20])
    rows = parse_nbp_block("KMSP", "", block, CYCLE).rows
    assert [r["p50_f"] for r in rows] == [-5, -10]


def test_file_reports_absent_stations():
    rows, problems = parse_nbp_file(BULLETIN, CYCLE, {"KMDW", "KSEA"})
    assert {r["station"] for r in rows} == {"KMDW"}
    assert problems == ["KSEA: not in bulletin"]


def test_backfill_resumes_and_records_missing():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if "/13/" in req.url.path:
            return httpx.Response(200, text=BULLETIN)
        return httpx.Response(404)

    s = Storage(":memory:")
    cycles = [CYCLE, CYCLE.replace(hour=1)]
    t = httpx.MockTransport(handler)
    stats = asyncio.run(backfill(s, {"KMDW"}, cycles, 120, transport=t))
    assert (stats["ok"], stats["missing"], stats["rows"]) == (1, 1, 2)
    again = asyncio.run(backfill(s, {"KMDW"}, cycles, 120, transport=t))
    assert again["todo"] == 0 and len(calls) == 2


def test_accuracy_day_ahead_by_version():
    s = Storage(":memory:")
    rows = parse_nbp_file(BULLETIN, CYCLE, {"KMDW"})[0]
    s.upsert_nbm_forecasts(rows)
    s.upsert_cli_history("KMDW", [("2026-11-04", 64, 40, "x"), ("2026-11-05", 99, 40, "y")])
    [a] = accuracy(s, ["KMDW"])
    # Only the next-day forecast (Nov 4, median 62) counts; the Nov 5 row is two days out.
    assert a.n == 1 and a.version == "5.0" and a.bias == 2 and a.mae == 2
    assert a.cover80 == 1.0 and a.below50 == 0.0
