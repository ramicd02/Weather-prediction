"""Parse NWS Daily Climate Report (CLI) text products.

What we need from each report:
- the date it summarizes ("...CLIMATE SUMMARY FOR OCTOBER 8 2024...")
- whether it is preliminary (has "VALID TODAY AS OF ..." / "VALID AS OF ...") or final
- the observed MAXIMUM and MINIMUM temperature (°F) under "TEMPERATURE (F)"

Missing values ("MM") parse to None, never 0. A trailing "R" (record) is stripped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "JANUARY",
            "FEBRUARY",
            "MARCH",
            "APRIL",
            "MAY",
            "JUNE",
            "JULY",
            "AUGUST",
            "SEPTEMBER",
            "OCTOBER",
            "NOVEMBER",
            "DECEMBER",
        ],
        start=1,
    )
}

_SUMMARY_RE = re.compile(r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})", re.IGNORECASE)
_PRELIM_RE = re.compile(r"VALID\s+(TODAY\s+)?AS\s+OF", re.IGNORECASE)
_TEMP_ROW_RE = re.compile(r"^\s*(MAXIMUM|MINIMUM)\s+(-?\d+R?|MM)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CliReport:
    climate_date: date | None
    is_preliminary: bool
    max_f: int | None
    min_f: int | None


def _value(token: str) -> int | None:
    token = token.upper()
    if token == "MM":
        return None
    return int(token.rstrip("R"))


def parse_cli(text: str) -> CliReport:
    climate_date = None
    m = _SUMMARY_RE.search(text)
    if m:
        month = MONTHS.get(m.group(1).upper())
        if month:
            climate_date = date(int(m.group(3)), month, int(m.group(2)))

    is_prelim = bool(_PRELIM_RE.search(text))

    max_f = min_f = None
    found_max = found_min = False
    in_temp = False
    for line in text.splitlines():
        upper = line.upper()
        if "TEMPERATURE (F)" in upper:
            in_temp = True
            continue
        if not in_temp:
            continue
        if upper.strip().startswith("PRECIPITATION"):
            break
        row = _TEMP_ROW_RE.match(line)
        if not row:
            continue
        label, token = row.group(1).upper(), row.group(2)
        # Only the first MAXIMUM / MINIMUM rows (the day's values) count.
        if label == "MAXIMUM" and not found_max:
            max_f, found_max = _value(token), True
        elif label == "MINIMUM" and not found_min:
            min_f, found_min = _value(token), True
        if found_max and found_min:
            break

    return CliReport(climate_date, is_prelim, max_f, min_f)
