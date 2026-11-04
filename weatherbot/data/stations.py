"""Map Kalshi KXHIGH series to their NWS settlement station.

Resolution order:
1. `stations:` in config.yaml (source = "config"): treat as confirmed.
2. A phrase match against the market's own rules text (source = "rules_match").
   This is a best-effort guess; `status` lists these so you can confirm them in config.
3. Otherwise unknown (None): the market is still logged, but no NWS data is pulled.

Order matters: more specific phrases come first (e.g. "Midway" before "Chicago").
Each phrase -> ICAO id below should be checked against the market rules before trading.
"""

from __future__ import annotations

RULES_PHRASES: list[tuple[str, str]] = [
    ("central park", "KNYC"),
    ("new york", "KNYC"),
    ("midway", "KMDW"),
    ("chicago", "KMDW"),
    ("miami", "KMIA"),
    ("los angeles", "KLAX"),
    ("denver", "KDEN"),
    ("philadelphia", "KPHL"),
    ("austin", "KAUS"),
    ("hobby", "KHOU"),
    ("houston", "KHOU"),
    ("dallas", "KDFW"),
    ("atlanta", "KATL"),
    ("boston", "KBOS"),
    ("seattle", "KSEA"),
    ("phoenix", "KPHX"),
    ("las vegas", "KLAS"),
    ("san francisco", "KSFO"),
    ("minneapolis", "KMSP"),
    ("new orleans", "KMSY"),
    ("oklahoma city", "KOKC"),
    ("san antonio", "KSAT"),
    ("reagan", "KDCA"),
    ("washington", "KDCA"),
]


def resolve_station(
    series_ticker: str,
    rules_text: str | None,
    overrides: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return (ICAO station, source) for a series."""
    overrides = overrides or {}
    if series_ticker in overrides and overrides[series_ticker]:
        return overrides[series_ticker].upper(), "config"
    text = (rules_text or "").lower()
    for phrase, icao in RULES_PHRASES:
        if phrase in text:
            return icao, "rules_match"
    return None, None


def cli_code_for(station: str, overrides: dict[str, str] | None = None) -> str:
    """NWS CLI location code. Usually the ICAO id without its leading K (KMDW -> MDW)."""
    overrides = overrides or {}
    if station in overrides:
        return overrides[station].upper()
    return station[1:] if len(station) == 4 and station.startswith("K") else station
