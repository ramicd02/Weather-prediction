# Kalshi Weather Bot — Build Plan

As of Nov 3, 2026. Living version: https://claude.ai/artifact/Ghab1Ew8f36WqSixGWXUxv

## Summary

Build a Python bot that prices Kalshi daily-high temperature brackets (KXHIGH) from forecast ensembles, then rests limit orders where its fair value beats the book. Start with one city, paper-trade first, and only go live after the model shows a measurable edge net of fees.

- **Market:** KXHIGH daily high temperature, one city first (chosen by the data-driven ranking below), then expand.
- **Style:** maker-first (resting limit orders), with taker orders only for late-day lock-in trades where the edge clears the fee.
- **Sizing:** quarter-Kelly, with the 5% minimum edge measured after fees.
- **Timeline:** roughly 10–12 weeks from first code to small live size, most of it spent collecting data and paper trading.
- **Go-live bar:** at least 30 days of shadow trading with a positive average edge after fees, and a model that beats the market's own prices on log loss.

## How the market works

Each city-day is one event with about six contracts: four 2°F brackets plus an "or below" and an "or above" tail. Exactly one pays $1.

| Item | Detail |
| --- | --- |
| Contract | `KXHIGH<CITY>-<YYMONDD>-B<center>` for brackets, `-T<strike>` for tails. Bracket B74.5 pays if the settled high is 74 or 75 (both ends inclusive). |
| Tail direction | Read `strike_type` (`greater`, `less`, `between`) and `floor_strike`/`cap_strike` from the API. Never infer it from the ticker. |
| Settlement | Highest temperature in the final NWS Daily Climate Report (CLI) for the named station, usually issued the next morning. |
| Day window | Midnight to midnight local standard time, even during daylight saving time. |
| Trading hours | Opens about 10:00 AM ET the day before; closes 11:59 PM ET on the weather date. |
| Cities | 20 US cities. Station traps: NYC = Central Park (KNYC), Chicago = Midway (KMDW), Dallas = DFW, Houston = Hobby. |
| Taker fee | 0.07 × contracts × P × (1 − P), rounded up. Peaks at 1.75¢ per contract at 50¢. |
| Maker fee | Zero unless the series is on Kalshi's non-standard list. Confirm KXHIGH is not listed at https://kalshi.com/fee-schedule before go-live. |
| Liquidity incentives | Some markets pay for resting orders that improve the book, even unfilled. Eligibility shows on each market page. |

Backtest trap: treating the 2°F brackets as half-open intervals creates a large fake edge. Score every bracket as inclusive on both ends.

## Where the edge comes from

1. **Calibrated day-ahead distribution (main strategy, maker).** Start from NBM percentiles (1st–99th) for daily max temperature. Correct them per city and season against past CLI highs, then convert to a probability per bracket. NWS max-temperature forecasts have a documented warm bias of a degree or two that grows in winter.
2. **Station-specific quirks.** The CLI high can differ from the highest hourly METAR reading because the peak often falls between reports. Fit a per-city, per-season gap between model output and CLI settlement.
3. **Same-day lock-in (taker, late day).** After the usual peak hour, a drop of 4°F or more from the day's observed peak suggests the high is very likely set (one desk reports ~87–92%). Treat that as a hypothesis to verify in your own backtest.

## Model features (Phase 2)

Base: NBM percentiles. Features that explain where NBM tends to miss: forecast cloud cover and its timing, wind direction and speed (onshore/lake breeze, downslope), 850 mb temperature and mixing depth, front timing, snow cover, recent rainfall / soil moisture, model spread, smoke/haze. Learn per-station, per-season corrections from backfilled CLI history. Use the same features as skip flags.

## Market and day filters

The bot trades only when every filter passes. Rank all 20 cities by historical forecast error and liquidity, and start with the best one.

| Filter | Skip when | Why it costs money |
| --- | --- | --- |
| City forecast error | The city's error spread (NBM vs CLI high, same season) is in the worst third of the 20 cities | Wide distributions mean few real edges; the apparent ones are usually noise. |
| Regime days | A front, sea breeze or marine layer is forecast near the expected peak, or the NBM 10th–90th spread is unusually wide | Timing errors of an hour or two swing the high by several degrees. |
| Midnight highs | The forecast high may occur near midnight local standard time | The settlement window doesn't match the calendar day during DST. |
| Thin books | Spread wider than ~10¢, or only a few hundred dollars resting near your price | Quotes don't fill, or fill only against better-informed traders. |
| Adverse selection | Your fills come mostly right before the market moves against you | Faster traders are picking you off. |
| Stale forecast | A new NBM run is due within minutes, or the last run is late | Other bots reprice first; old quotes are a free option for them. |
| Settlement doubt | Preliminary CLI and hourly METAR peaks disagree by 2°F or more | Kalshi can delay settlement; fair value may be anchored to the wrong number. |
| Correlated cities | Two traded cities sit under the same weather system | One bad forecast hits both. Count them as one event for the exposure cap. |

Log every skipped market and its reason, and tune filters from results.

Risks the filters can't remove: edge fading as more weather bots arrive, Kalshi changing fees or rules, and server or data outages. Gate C and the drawdown stop are the backstop.

## Data sources

| Need | Source | Cost and limits |
| --- | --- | --- |
| Forecast distribution | NBM on AWS (`noaa-nbm-grib2-pds`), live on NOMADS | Free. Archive back to ~May 2020, so no live NBM logging needed. |
| NBM text bulletins | NOMADS `blend.YYYYMMDD/CC/text` | Free. Station-level tables, easier than GRIB2. |
| Live observations | api.weather.gov `/stations/{ICAO}/observations` | Free, no key. |
| Settlement values (recent) | api.weather.gov CLI products | Free, keeps ~7 days only. |
| Settlement history | Iowa Environmental Mesonet `/json/cli.py` | Free. Training labels. |
| Kalshi price history | Kalshi API candlesticks, trades, settled markets | Free with an account. |
| Optional second model | Open-Meteo | Free tier is non-commercial only; skip it. |

Start logging Kalshi order books from day one. Historical order book depth isn't archived anywhere and is what tells you whether quotes would have filled.

## Kalshi API

| Item | Detail |
| --- | --- |
| REST base | Prod `https://api.elections.kalshi.com/trade-api/v2`; demo `https://demo-api.kalshi.co/trade-api/v2`. |
| Auth | RSA-PSS SHA-256 over `{timestamp_ms}{METHOD}{path}`; path includes `/trade-api/v2`, excludes query string. Separate demo/prod keys. Keys support read/write scopes. |
| WebSocket | `wss://external-api-ws.kalshi.com/trade-api/ws/v2`; auth required. Channels: `orderbook_delta`, `trade`, `fill`, `user_orders`. |
| Order book shape | Yes bids and no bids only. A no bid at X = yes ask at 100 − X. |
| Rate limits | Basic 20 reads/s, 10 writes/s; Advanced (application form) 30/30. |
| Demo limitation | Demo book is near-empty: test order logic, not fills or pricing. |

## System architecture

Inputs (NBM forecasts, NWS observations, Kalshi WebSocket) → data store + logger → pricing model → quote engine → risk manager → order manager → Kalshi. Fills flow back from Kalshi to the risk manager. The pricing model is the only part that creates edge; everything after it protects that edge.

## Phased build plan

| Phase | Weeks | Work |
| --- | --- | --- |
| 1 · Data pipeline | 1–3 | API keys, confirm maker fees, order-book/trade/obs/CLI logger for all cities, CLI + NBM backfill |
| 2 · Model + backtest | 3–5 | Bias correction, bracket probabilities, score vs Kalshi prices on held-out days |
| **Gate A** | | Model beats the market's own prices on log loss over held-out days |
| 3 · Execution on demo | 5–6 | Order manager, risk manager, WebSocket book, kill switch, tested on demo |
| 4 · Shadow trading | 6–10 | Production data, no real orders; count fills only when the book traded through |
| **Gate B** | | 30+ days, positive average edge after fees, realistic fill rate |
| 5 · Small live | 10–12+ | One city, small bankroll, all caps on; add a city after 30 live days |
| **Gate C** | | Live P&L within the shadow-trading range before adding cities or size |

If a gate fails, go back one phase.

## Risk rules and kill switches

- **Edge after fees:** quote only where fair value beats the price by 5%+ after fees.
- **Sizing:** quarter-Kelly, capped at 2% of bankroll per city-day event.
- **Event cap:** total exposure across one city-day under 5% of bankroll.
- **Daily loss stop:** pause for the day at −5% of bankroll.
- **Drawdown stop:** halt at −15% from peak; manual restart required.
- **Stale data:** cancel all resting orders if the forecast is past its next scheduled run or the WebSocket is down 30+ seconds.
- **Forecast jump:** cancel and requote when a new NBM run moves the median by 2°F+.
- **Kill switch:** one command that batch-cancels every open order and stops the loop.

Percentages are starting points. This plan isn't financial advice and guarantees nothing.
