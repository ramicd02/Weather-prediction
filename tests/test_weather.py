from datetime import date

from weatherbot.data.cli_parser import parse_cli
from weatherbot.data.nws import parse_observation
from weatherbot.data.stations import cli_code_for, resolve_station
from weatherbot.data.storage import Storage

PRELIM = """
CLIMATE REPORT
NATIONAL WEATHER SERVICE CHICAGO IL
434 PM CST TUE NOV 3 2026

...THE CHICAGO-MIDWAY CLIMATE SUMMARY FOR NOVEMBER 3 2026...
VALID TODAY AS OF 0400 PM LOCAL TIME.

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
                VALUE   (LST)  VALUE       VALUE  FROM      YEAR
...................................................................
TEMPERATURE (F)
 TODAY
  MAXIMUM         61    232 PM  78    1938  55      6       52
  MINIMUM         44    712 AM  20    1951  38      6       40
  AVERAGE         53                        47      6       46

PRECIPITATION (IN)
  TODAY            0.00          1.20 1990   0.09  -0.09     0.00
"""

FINAL = """
CLIMATE REPORT
NATIONAL WEATHER SERVICE CHICAGO IL
1258 AM CST WED NOV 4 2026

...THE CHICAGO-MIDWAY CLIMATE SUMMARY FOR NOVEMBER 3 2026...

TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         62R   345 PM  61    1938  55      7       52
  MINIMUM         MM
"""


def test_cli_preliminary():
    r = parse_cli(PRELIM)
    assert r.climate_date == date(2026, 11, 3)
    assert r.is_preliminary is True
    assert (r.max_f, r.min_f) == (61, 44)


def test_cli_final_record_and_missing():
    r = parse_cli(FINAL)
    assert r.is_preliminary is False
    assert r.max_f == 62  # record flag stripped
    assert r.min_f is None  # MM is missing, not zero


def test_cli_ignores_rows_outside_temperature_block():
    text = "MAXIMUM 99\nTEMPERATURE (F)\n  MAXIMUM  50\nPRECIPITATION\n  MAXIMUM 7"
    assert parse_cli(text).max_f == 50


def test_station_resolution():
    rules = (
        "If the highest temperature recorded at Chicago Midway, IL ... as reported by "
        "the National Weather Service's Climatological Report (Daily) ..."
    )
    assert resolve_station("KXHIGHCHI", rules) == ("KMDW", "rules_match")
    assert resolve_station("KXHIGHCHI", rules, {"KXHIGHCHI": "kord"}) == ("KORD", "config")
    assert resolve_station("KXHIGHUS", "highest temperature in the United States") == (None, None)
    assert resolve_station("KXHIGHNY", "Central Park, New York") == ("KNYC", "rules_match")


def test_cli_codes():
    assert cli_code_for("KMDW") == "MDW"
    assert cli_code_for("KNYC") == "NYC"
    assert cli_code_for("KMDW", {"KMDW": "mdw2"}) == "MDW2"


def test_observation_dedup():
    s = Storage(":memory:")
    payload = {
        "properties": {
            "timestamp": "2026-11-04T15:53:00+00:00",
            "temperature": {"value": 12.2, "unitCode": "wmoUnit:degC"},
            "rawMessage": "KMDW 041553Z ... T01220056",
        }
    }
    ts, temp, raw = parse_observation(payload)
    assert (ts, temp) == ("2026-11-04T15:53:00+00:00", 12.2)
    assert s.insert_observation("KMDW", ts, temp, raw, payload) is True
    assert s.insert_observation("KMDW", ts, temp, raw, payload) is False
    assert len(s.query("SELECT * FROM observations")) == 1


def test_observation_missing_temperature_is_null():
    ts, temp, _ = parse_observation({"properties": {"timestamp": "t", "temperature": {}}})
    assert temp is None
