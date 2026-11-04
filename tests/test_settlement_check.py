"""Settlement check: bracket math, CLI parsing, fetching (no network)."""

import asyncio
from datetime import date

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from weatherbot.data.settlement_check import (
    check_event,
    consistent_values,
    event_date_from_ticker,
    fetch_settled,
    format_report,
    parse_expiration_value,
    parse_iem_cli,
    run_checks,
    summarize,
    yes_if,
)
from weatherbot.data.storage import Storage
from weatherbot.kalshi.auth import Signer
from weatherbot.kalshi.rest import KalshiReadClient

BASE = "https://api.test/trade-api/v2"


def mk(ticker, strike_type, floor=None, cap=None, result="no", ev="63"):
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "result": result,
        "expiration_value": ev,
    }


# A Chicago city-day where the settled high was 63.
EVENT = [
    mk("KXHIGHCHI-26NOV05-T58", "less", cap=58),
    mk("KXHIGHCHI-26NOV05-B58.5", "between", 58, 59),
    mk("KXHIGHCHI-26NOV05-B60.5", "between", 60, 61),
    mk("KXHIGHCHI-26NOV05-B62.5", "between", 62, 63, result="yes"),
    mk("KXHIGHCHI-26NOV05-B64.5", "between", 64, 65),
    mk("KXHIGHCHI-26NOV05-T65", "greater", floor=65),
]


def test_event_date_from_ticker():
    assert event_date_from_ticker("KXHIGHCHI-26NOV05") == date(2026, 11, 5)
    assert event_date_from_ticker("KXHIGHTSFO-25JAN31") == date(2025, 1, 31)
    assert event_date_from_ticker("KXHIGHCHI-26FOO05") is None
    assert event_date_from_ticker("KXHIGHCHI-26FEB30") is None
    assert event_date_from_ticker(None) is None


def test_between_is_inclusive_on_both_ends():
    assert yes_if("between", 74, 75, 74) is True
    assert yes_if("between", 74, 75, 75) is True
    assert yes_if("between", 74, 75, 73) is False
    assert yes_if("between", 74, 75, 76) is False


def test_tails_use_strike_type_not_ticker():
    assert yes_if("greater", 62, None, 62) is False
    assert yes_if("greater", 62, None, 63) is True
    assert yes_if("greater_or_equal", 62, None, 62) is True
    assert yes_if("less", None, 58, 58) is False
    assert yes_if("less", None, 58, 57) is True
    assert yes_if("less_or_equal", None, 58, 58) is True
    assert yes_if("functional", 1, 2, 3) is None


def test_parse_expiration_value():
    assert parse_expiration_value("63") == 63
    assert parse_expiration_value("63.0") == 63
    assert parse_expiration_value("63°F") == 63
    assert parse_expiration_value("-4") == -4
    assert parse_expiration_value("") is None
    assert parse_expiration_value(None) is None


def test_consistent_values_brackets_the_settled_high():
    assert consistent_values(EVENT) == [62, 63]
    assert consistent_values([]) == []


def test_check_event_agree_and_disagree():
    ok = check_event("KXHIGHCHI", "KXHIGHCHI-26NOV05", EVENT, 63)
    assert ok.outcome_match is True and ok.value_match is True and ok.kalshi_value == 63
    # CLI says 64: wrong bracket, and the value differs by one degree.
    bad = check_event("KXHIGHCHI", "KXHIGHCHI-26NOV05", EVENT, 64)
    assert bad.outcome_match is False and bad.value_match is False
    # CLI says 62: same bracket (outcome agrees) but the value differs.
    near = check_event("KXHIGHCHI", "KXHIGHCHI-26NOV05", EVENT, 62)
    assert near.outcome_match is True and near.value_match is False
    missing = check_event("KXHIGHCHI", "KXHIGHCHI-26NOV05", EVENT, None)
    assert missing.outcome_match is None and missing.value_match is None


def test_unsettled_markets_are_ignored():
    ms = [dict(m, result="") for m in EVENT]
    c = check_event("KXHIGHCHI", "KXHIGHCHI-26NOV05", ms, 63)
    assert c.outcome_match is None


def test_parse_iem_cli_handles_missing_and_trace():
    payload = {
        "results": [
            {"valid": "2025-07-01", "high": 91, "low": 72, "product": "p1"},
            {"valid": "2025-07-02", "high": "M", "low": 70, "product": "p2"},
            {"valid": "2025-07-03", "high": 88.0, "low": None},
            {"high": 80},
        ]
    }
    assert parse_iem_cli(payload) == [
        ("2025-07-01", 91, 72, "p1"),
        ("2025-07-02", None, 70, "p2"),
        ("2025-07-03", 88, None, None),
    ]


def test_report_counts_and_diffs():
    s = Storage(":memory:")
    for m in EVENT:
        s.upsert_settled_market(m, "KXHIGHCHI", "2026-11-05", "live")
    other = [dict(m, ticker=m["ticker"].replace("NOV05", "NOV06")) for m in EVENT]
    for m in other:
        m["event_ticker"] = "KXHIGHCHI-26NOV06"
        s.upsert_settled_market(m, "KXHIGHCHI", "2026-11-06", "live")
    s.upsert_cli_history("KMDW", [("2026-11-05", 63, 50, "a"), ("2026-11-06", 64, 50, "b")])
    checks = run_checks(s, {"KXHIGHCHI": "KMDW"})
    [summary] = summarize(checks, {"KXHIGHCHI": "KMDW"})
    assert summary.events == 2 and summary.with_cli == 2
    assert summary.outcome_agree == 1 and summary.outcome_disagree == 1
    assert summary.value_equal == 1 and summary.diffs == {-1.0: 1}
    text = format_report([summary])
    assert "KXHIGHCHI" in text and "50.0%" in text and "-1:1" in text


def _client(handler):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return KalshiReadClient(
        BASE, Signer("k1", key), reads_per_s=1000, transport=httpx.MockTransport(handler)
    )


def test_fetch_settled_live_and_historical_pages():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        path = req.url.path.removeprefix("/trade-api/v2")
        seen.append((path, dict(req.url.params)))
        if path == "/markets":
            assert req.url.params["status"] == "settled"
            return httpx.Response(200, json={"markets": EVENT[:2], "cursor": ""})
        if path == "/historical/markets":
            assert req.url.params["series_ticker"] == "KXHIGHCHI"
            if "cursor" not in req.url.params:
                return httpx.Response(200, json={"markets": EVENT[2:4], "cursor": "c2"})
            return httpx.Response(200, json={"markets": EVENT[4:], "cursor": ""})
        return httpx.Response(404)

    s = Storage(":memory:")
    rest = _client(handler)
    n = asyncio.run(fetch_settled(rest, s, "KXHIGHCHI"))
    assert n == 6
    rows = s.query("SELECT source, COUNT(*) n FROM kalshi_settled GROUP BY source ORDER BY source")
    assert [(r["source"], r["n"]) for r in rows] == [("historical", 4), ("live", 2)]
    assert s.query("SELECT DISTINCT event_date d FROM kalshi_settled")[0]["d"] == "2026-11-05"


def test_fetch_settled_stops_if_history_ignores_series_filter():
    pages = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/trade-api/v2")
        if path == "/markets":
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        pages.append(1)
        foreign = mk("KXBTC-26NOV05-T100000", "greater", floor=100000)
        return httpx.Response(200, json={"markets": [foreign], "cursor": "more"})

    s = Storage(":memory:")
    n = asyncio.run(fetch_settled(_client(handler), s, "KXHIGHCHI"))
    assert n == 0 and len(pages) == 1
    assert s.query("SELECT COUNT(*) n FROM kalshi_settled")[0]["n"] == 0


def test_fetch_settled_survives_missing_historical_endpoint():
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/trade-api/v2")
        if path == "/markets":
            return httpx.Response(200, json={"markets": EVENT[:1], "cursor": ""})
        return httpx.Response(404)

    s = Storage(":memory:")
    assert asyncio.run(fetch_settled(_client(handler), s, "KXHIGHCHI")) == 1
