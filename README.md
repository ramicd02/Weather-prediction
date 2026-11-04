# Weather-prediction

A Python bot that prices Kalshi daily-high temperature markets (KXHIGH) from NOAA forecast data and trades where its fair value beats the order book.

- `docs/PLAN.md` — full build plan, phases and go/no-go gates
- `docs/RUNNING.md` — **how to install and run the data logger (start here)**
- `CLAUDE.md` — rules and facts for Claude Code
- `docs/prompts/` — Claude Code prompts, one per task

## Status

**Phase 2 — model and backtest** (see `docs/PHASE2.md`). Phase 1, the data pipeline, is done.

**Phase 1 — data pipeline.** The read-only data logger is built and tested:

```
python -m weatherbot.data.logger          # run 24/7
python -m weatherbot.data.logger status   # health check
```

It records, for every KXHIGH city: market metadata and results, full order-book ladders, every public trade, NWS observations at each settlement station, and preliminary + final Daily Climate Reports.

Settlement check (backfills historical CLI highs and compares them to Kalshi's settled values):

```
python -m weatherbot.data.settlement_check
```

Kalshi's rules name The Weather Company as the settlement source, so the NWS CLI high is only a proxy until this check shows it matches.

NBM forecast backfill (NOAA's NBP bulletin: tomorrow's high as percentiles, per station):

```
python -m weatherbot.data.nbm sample
python -m weatherbot.data.nbm backfill
python -m weatherbot.data.nbm report
```

Not financial advice. No real orders until Gate B in the plan passes.
