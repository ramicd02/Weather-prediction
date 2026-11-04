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
    nbp_url,
    parse_nbp_block,
    parse_nbp_file,
    split_bulletins,
)
from weatherbot.data.storage import Storage

CYCLE = datetime(2026, 11, 4, 13, tzinfo=UTC)

# Layout per the NBM text-card key: header, UTC and FHR rows, then element rows whose
# 00Z columns hold the max and 12Z columns the min. Values here are made up.
BULLETIN = """\
 KMDW    NBM V5.0 NBP GUIDANCE   11/04/2026  1300 UTC
 UTC  00  12  00  12  00
 FHR  11  23  35  47  59
 TXNMN  62  45  58  40  55
 TXNSD   2   3   3   3   4
 TXNP1  59  41  54  36  50
 TXNP2  61  43  56  38  52
 TXNP5  62  45  58  40  55
 TXNP7  63  47  60  42  58
 TXNP9  65  49  62  44  60
 KNYC    NBM V5.0 NBP GUIDANCE   11/04/2026  1300 UTC
 UTC  00  12  00
 FHR  11  23  35
 TXNMN  60  50  57
 TXNP1  57  47  53
 TXNP5  60  50 -99
 TXNP9  63  53  61
 KXXX    NBM V5.0 NBP GUIDANCE   11/04/2026  1300 UTC
 UTC  00  12
 FHR  11  23
 TXNP5  70  50
"""


def test_url_and_cycles():
    assert nbp_url(CYCLE) == (
        "https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.20261104/13/text/blend_nbptx.t13z"
    )
    cs = cycles_between(date(2026, 1, 1), date(2026, 1, 2), (1, 13))
    assert [c.isoformat() for c in cs][-1] == "2026-01-02T13:00:00+00:00" and len(cs) == 4


def test_max_columns_and_target_day():
    assert is_max_column(datetime(2026, 11, 5, 0, tzinfo=UTC))
    assert is_max_column(datetime(2026, 11, 5, 1, tzinfo=UTC))
    assert not is_max_column(datetime(2026, 11, 5, 12, tzinfo=UTC))
    # 00Z on Nov 6 is the evening of Nov 5 in the US.
    assert max_target_date(datetime(2026, 11, 6, 0, tzinfo=UTC)) == date(2026, 11, 5)


def test_split_keeps_only_wanted_stations():
    blocks = split_bulletins(BULLETIN, {"KMDW", "KNYC"})
    assert set(blocks) == {"KMDW", "KNYC"} and blocks["KMDW"][0] == "5.0"


def test_parse_block_takes_max_columns_with_no_lookahead_stamp():
    ver, block = split_bulletins(BULLETIN, {"KMDW"})["KMDW"]
    res = parse_nbp_block("KMDW", ver, block, CYCLE, lag_min=120)
    assert res.problem is None
    assert [r["target_date"] for r in res.rows] == ["2026-11-04", "2026-11-05", "2026-11-06"]
    tomorrow = res.rows[1]
    assert tomorrow["fhr"] == 35 and tomorrow["valid_time"] == "2026-11-06T00:00:00Z"
    assert (tomorrow["p10_f"], tomorrow["p50_f"], tomorrow["p90_f"]) == (54, 58, 62)
    assert tomorrow["mean_f"] == 58 and tomorrow["sd_f"] == 3
    assert tomorrow["available_at"] == "2026-11-04T15:00:00Z"
    assert tomorrow["cycle_time"] == "2026-11-04T13:00:00Z"


def test_missing_values_become_none():
    ver, block = split_bulletins(BULLETIN, {"KNYC"})["KNYC"]
    rows = parse_nbp_block("KNYC", ver, block, CYCLE).rows
    assert rows[1]["p50_f"] is None and rows[1]["mean_f"] == 57 and rows[1]["sd_f"] is None


def test_misaligned_block_is_rejected():
    block = " KMDW NBM V5.0 NBP\n FHR  11  23  35\n TXNP5  62  45\n"
    res = parse_nbp_block("KMDW", "5.0", block, CYCLE)
    assert res.rows == [] and "TXNP5" in res.problem


def test_negative_values_packed_together():
    block = " KMSP NBM V5.0 NBP\n FHR  11  23\n TXNP5  -5-12\n"
    rows = parse_nbp_block("KMSP", "5.0", block, CYCLE).rows
    assert rows[0]["p50_f"] == -5


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
    assert (stats["ok"], stats["missing"], stats["rows"]) == (1, 1, 3)
    again = asyncio.run(backfill(s, {"KMDW"}, cycles, 120, transport=t))
    assert again["todo"] == 0 and len(calls) == 2


def test_accuracy_day_ahead_by_version():
    s = Storage(":memory:")
    rows = parse_nbp_file(BULLETIN, CYCLE, {"KMDW"})[0]
    s.upsert_nbm_forecasts(rows)
    s.upsert_cli_history("KMDW", [("2026-11-05", 60, 40, "x"), ("2026-11-04", 99, 40, "y")])
    [a] = accuracy(s, ["KMDW"])
    # Only the next-day forecast (median 58) counts; the same-day row is ignored.
    assert a.n == 1 and a.version == "5.0" and a.bias == 2 and a.mae == 2
    assert a.cover80 == 1.0 and a.below50 == 0.0
