"""Observation history: LST day window, METAR max groups, running max (tests first)."""

from datetime import UTC, date, datetime

import pytest

from weatherbot.data.obs_history import (
    DayObs,
    lst_day_bounds,
    parse_iem_csv,
    round_half_up,
    six_hour_max_f,
)


def ts(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


def test_lst_day_is_midnight_to_midnight_standard_time_even_in_summer():
    # Chicago is UTC-6 standard. The CLI day for July 4 runs 06:00Z Jul 4 -> 06:00Z Jul 5,
    # i.e. 1 AM to 1 AM daylight time.
    start, end = lst_day_bounds(date(2026, 7, 4), -6)
    assert start == ts("2026-07-04T06:00:00") and end == ts("2026-07-05T06:00:00")
    start, _ = lst_day_bounds(date(2026, 1, 4), -8)
    assert start == ts("2026-01-04T08:00:00")


def test_round_half_up_matches_whole_degree_reporting():
    assert round_half_up(74.5) == 75 and round_half_up(74.49) == 74
    assert round_half_up(-0.5) == 0 and round_half_up(-1.5) == -1


def test_six_hour_max_group_in_remarks():
    m = "KMDW 042351Z 21008KT 10SM CLR 29/14 A3002 RMK AO2 SLP165 T02890144 10306 20217 53010"
    assert six_hour_max_f(m) == pytest.approx(30.6 * 9 / 5 + 32)
    neg = "KMSP 120551Z 00000KT 10SM CLR M12/M17 A3030 RMK AO2 11106 21150"
    assert six_hour_max_f(neg) == pytest.approx(-10.6 * 9 / 5 + 32)
    assert six_hour_max_f("KMDW 041451Z 21008KT 10SM CLR 25/14 A3002 RMK AO2") is None
    assert six_hour_max_f(None) is None


def test_parse_iem_csv_skips_missing_values():
    text = (
        "station,valid,tmpf,metar\n"
        "MDW,2026-07-04 17:51,84.20,KMDW 041751Z ... RMK AO2 10294\n"
        "MDW,2026-07-04 18:51,M,KMDW 041851Z ...\n"
        "MDW,2026-07-04 19:05,85.00,KMDW 041905Z ...\n"
    )
    rows = parse_iem_csv(text)
    assert [r[0] for r in rows] == [ts("2026-07-04T17:51:00"), ts("2026-07-04T19:05:00")]
    assert rows[0][1] == 84.2 and rows[0][2] == pytest.approx(29.4 * 9 / 5 + 32)


def test_running_max_respects_window_and_publication_lag():
    start, end = lst_day_bounds(date(2026, 7, 4), -6)
    obs = [
        (start - 600, 99.0, None),  # previous CLI day: ignored
        (ts("2026-07-04T15:51:00"), 80.1, None),
        (ts("2026-07-04T18:51:00"), 84.6, None),
        (ts("2026-07-04T19:51:00"), 83.0, None),
    ]
    day = DayObs(start, end, obs, lag_s=600)
    # At 19:00Z the 18:51 report is only 9 minutes old: not yet usable.
    assert day.running_max(ts("2026-07-04T19:00:00")) == (80.1, 80.1)
    raw, current = day.running_max(ts("2026-07-04T20:30:00"))
    assert raw == 84.6 and current == 83.0
    assert day.running_max(ts("2026-07-04T10:00:00")) == (None, None)


def test_six_hour_max_counts_only_if_its_window_is_inside_the_day():
    start, end = lst_day_bounds(date(2026, 7, 4), -6)
    obs = [
        # 05:51Z report: its 6h window began 23:51Z on Jul 3 -> previous CLI day.
        (ts("2026-07-04T05:51:00"), 70.0, 90.0),
        # 17:51Z report: window 11:51Z-17:51Z, inside the day. Max 88.0 between hourlies.
        (ts("2026-07-04T17:51:00"), 85.0, 88.0),
    ]
    day = DayObs(start, end, obs, lag_s=600)
    assert day.running_max(ts("2026-07-04T19:00:00"))[0] == 88.0


def test_backfill_resumes_and_records_errors():
    import asyncio

    import httpx

    from weatherbot.data.obs_history import backfill, ensure_schema
    from weatherbot.data.storage import Storage

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(dict(req.url.params))
        if req.url.params["station"] == "BAD":
            return httpx.Response(404)
        assert req.url.params["report_type"] == "3,4" and req.url.params["tz"] == "Etc/UTC"
        body = "station,valid,tmpf,metar\nMDW,2025-01-05 17:51,30.0,KMDW RMK AO2 10011\n"
        return httpx.Response(200, text=body)

    s = Storage(":memory:")
    ensure_schema(s)
    t = httpx.MockTransport(handler)
    stats = asyncio.run(backfill(s, ["KMDW", "KBAD"], ["2025-01"], ["h"], 0, t))
    assert (stats["ok"], stats["error"], stats["rows"]) == (1, 1, 1)
    assert calls[0]["station"] == "MDW" and calls[0]["month2"] == "2"
    row = s.query("SELECT tmpf, max6_f FROM obs_history")[0]
    assert row["tmpf"] == 30.0 and row["max6_f"] == pytest.approx(1.1 * 9 / 5 + 32)
    again = asyncio.run(backfill(s, ["KMDW", "KBAD"], ["2025-01"], ["h"], 0, t))
    assert again["todo"] == 1  # only the failed station-month is retried
