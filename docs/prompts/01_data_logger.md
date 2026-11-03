# Claude Code prompt #1 — Kalshi + NWS data logger

Paste everything below the line into Claude Code, from the repo root, after `CLAUDE.md` and `docs/PLAN.md` are in place. Start in plan mode.

---

Read `CLAUDE.md` and `docs/PLAN.md`. We're starting Phase 1. The first deliverable is a **data logger** that runs 24/7 and saves data we can't get later. Order book history isn't archived anywhere, so every day this logger isn't running is backtest data lost for good.

NBM forecasts **don't** need live logging, because NOAA keeps a full archive on AWS. Leave NBM out of this task.

## Goal
A long-running Python service that records, for **all** KXHIGH city series (not just one city, since we'll use this data to rank cities later):

1. **Market metadata**, once per new market: ticker, event ticker, series ticker, `strike_type`, `floor_strike`, `cap_strike`, open/close times, and the settlement station/source as stated in the market's own rules.
2. **Order book snapshots**: full yes-bid and no-bid ladders for every open KXHIGH market, saved **once per minute** and on every top-of-book change, built from the WebSocket `orderbook_delta` channel (snapshot, then deltas). Keep sequence numbers, and resubscribe for a fresh snapshot whenever a gap is detected.
3. **Trades**: every public trade on those markets (WebSocket `trade` channel, or the REST trades endpoint if that turns out to be more reliable; check the docs).
4. **Settlements**: final result for each market once it settles.
5. **NWS observations**: latest observation for each settlement station every 10 minutes from `api.weather.gov/stations/{ICAO}/observations/latest`, de-duplicated by observation timestamp.
6. **CLI reports**: once a day around 9:00 AM ET, the previous day's preliminary and final CLI high/low for each station, recording issuance time and whether each is preliminary or final.

## Requirements
- **Discovery:** find KXHIGH series and their open markets dynamically through the API. Check `https://docs.kalshi.com/llms.txt` for the right endpoints. Re-check every 15 minutes so new days' markets (opening about 10 AM ET the day before) are picked up automatically.
- **Read-only:** this service never places or cancels orders. Use a production API key created with **read scope only**. Add a startup check that refuses to run if the key has write scope, if that can be detected; otherwise log a loud warning.
- **Storage:** SQLite to start (one file, WAL mode), with a clean schema and indexes on `(ticker, ts)`. Prices as integer cents, timestamps in UTC. Put storage behind a small interface so it can move to Postgres or Parquet later.
- **Resilience:** auto-reconnect the WebSocket with backoff, heartbeat/ping handling, a client-side rate limiter (Basic tier: 20 reads/s), and backoff on HTTP 429. One failing station or market must never crash the service.
- **NWS etiquette:** send a `User-Agent` header with an app name and contact email (read from config), and keep requests modest.
- **Config:** `config.yaml` for poll intervals, the station list override (optional), the DB path and the user-agent contact. Secrets come only from environment variables.
- **Observability:** structured JSON logs, plus a `status` CLI command showing markets tracked, last snapshot time per market, last observation per station, DB size, and any errors in the last hour.
- **Running it:** a single entry point (`python -m weatherbot.data.logger`), plus a sample `systemd` unit file and a short `docs/RUNNING.md` explaining how to run it on an always-on Linux box or VPS.

## Reuse
I already have a Python Kalshi API client with RSA-PSS signing, market-data reads and order placement. Before writing a new client, ask me to add it to the repo, then refactor it into `weatherbot/kalshi/`, keeping the order-placement code out of the logger's import path.

## Tests (write these first)
- RSA-PSS signature string excludes the query string and includes `/trade-api/v2`.
- No-bid → yes-ask conversion: (100 − X).
- Order book delta application, including sequence-gap detection, which triggers a resnapshot.
- Observation de-duplication.
- CLI parsing handles missing values (`MM`) as null, not zero, and distinguishes preliminary from final.

## Definition of done
- `pytest` and `ruff` pass.
- Running against production with a read-only key for 30 minutes shows growing rows in every table and a clean `status` output.
- `docs/RUNNING.md` lets me deploy it on a fresh machine without asking you anything.

Propose the design and schema first. Don't write code until I approve the plan.
