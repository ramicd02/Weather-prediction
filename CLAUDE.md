# CLAUDE.md — Kalshi Weather Bot

## What this project is
A Python bot that prices Kalshi daily-high temperature markets (KXHIGH series) from NOAA forecast data and trades where its fair value beats the order book. The full build plan is in `docs/PLAN.md`. Read it before starting any phase, and don't start work outside the current phase without asking.

Current phase: **Phase 1, Data pipeline.**

## Hard rules (never break these)
1. **Never commit secrets.** API key IDs and private key PEM files live outside the repo and load from environment variables (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`). `.env`, `*.pem` and `*.key` stay in `.gitignore`.
2. **Demo by default.** `KALSHI_ENV` defaults to `demo`. Production requires `KALSHI_ENV=prod` set explicitly. Code must never silently fall back to prod.
3. **No order placement outside `weatherbot/trading/`.** Data and model code is read-only against Kalshi. Every order path goes through the risk manager. No exceptions, no shortcuts "for testing."
4. **Brackets are inclusive on both ends.** Bracket `B74.5` pays if the settled high is 74 **or** 75. Never model brackets as half-open intervals.
5. **Read `strike_type`, `floor_strike` and `cap_strike` from the API.** Never infer a tail market's direction from its ticker.
6. **Settlement source is the final NWS Daily Climate Report (CLI)** for the market's named station, using the midnight-to-midnight **local standard time** window, even during daylight saving time. Get each market's station from its own rules, not from a hardcoded guess.
7. **All risk limits live in `config.yaml`.** No magic numbers for sizing, caps or stops anywhere else.
8. **No look-ahead in backtests.** A backtest at time T may only use data that was published before T, judged by issuance time and not by valid time.

## Kalshi API facts
- REST: prod `https://api.elections.kalshi.com/trade-api/v2` (serves all markets), demo `https://demo-api.kalshi.co/trade-api/v2`.
- WebSocket: prod `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, demo `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`. Auth is required even for public channels.
- Auth: RSA-PSS with SHA-256 (MGF1 SHA-256, salt length = digest length). Sign `{timestamp_ms}{METHOD}{path}`, where the path includes `/trade-api/v2` and **excludes the query string**. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.
- The order book returns **yes bids and no bids only**. A no bid at X¢ is a yes ask at (100 − X)¢.
- Rate limits (Basic tier): 20 reads/s, 10 writes/s. Implement a client-side token bucket, prefer WebSocket over polling, and back off on HTTP 429.
- Taker fee per order: `ceil(0.07 × contracts × P × (1 − P))` dollars, where P is the price in dollars. Maker fee is 0 unless the series is on Kalshi's non-standard fee list.
- When unsure about any endpoint, field name or parameter, check `https://docs.kalshi.com/llms.txt` and the OpenAPI spec. Don't guess.

## Weather data facts
- Forecasts: NOAA National Blend of Models (NBM). Archive on AWS bucket `noaa-nbm-grib2-pds` (no account needed, back to about May 2020); live on NOMADS.
- Observations: `api.weather.gov/stations/{ICAO}/observations` (no key; send a descriptive `User-Agent` header with contact info, as NWS requests).
- Recent CLI reports: api.weather.gov products (keeps about 7 days only).
- CLI history: Iowa Environmental Mesonet, `https://mesonet.agron.iastate.edu/json/cli.py`.
- Don't use Open-Meteo's free API. Its terms restrict it to non-commercial use.

## Conventions
- Python 3.11+, type hints everywhere, `ruff` for lint and format, `pytest` for tests.
- Prices are stored as integer cents or `Decimal`, never float. Temperatures are integer °F to match CLI.
- Timestamps are stored in UTC (ISO 8601). Convert to the station's local standard time only for settlement-day logic.
- Long-running processes log in structured JSON and reconnect automatically.
- Write tests first for anything touching money, settlement or time windows: bracket math, fees, Kelly sizing, the local standard time day window, and every risk stop.

## Layout
```
weatherbot/
  kalshi/     # API client: auth, REST, WebSocket, rate limiter
  data/       # loggers and fetchers: Kalshi books, NWS obs, CLI, NBM
  model/      # bias correction, bracket probabilities
  trading/    # quote engine, risk manager, order manager (ONLY place orders exist)
  backtest/
config.yaml
tests/
docs/PLAN.md
```

## Working style
- Start each phase in plan mode and propose the design before writing code.
- Keep changes small and tested. Run `pytest` and `ruff` before calling anything done.
- If something in this file conflicts with what you find in the official docs, stop and flag it instead of picking one silently.
