# Phase 2: model and backtest (approved 2026-11-04)

Full design, with reasoning: the "Phase 2 Design: Model and Backtest" doc
(https://claude.ai/artifact/VRoYU2npi47srQAi7BfvmT). This file is the short version
for coding sessions. All numbers live in `config.yaml` (`model`, `backtest`, `risk`,
`regions`, `trading_exclude_series`).

## Question
Does a calibrated NBM model price KXHIGH brackets better than the Kalshi market did,
after fees, out of sample? Gate A decides.

## Gate A (all must hold; judged on all 23 eligible cities)
- Lower end of a 90% confidence range on profit per contract > 0. Bootstrap by whole
  days, so same-day trades count as one draw.
- Still profitable with every fill 1c worse.
- Model log loss below the market's, with each event's mids rescaled to sum to 1, on
  brackets with spreads of 10c or less.
- No single city, region or month supplies more than half the profit; max drawdown
  under 20% of the test bankroll.
- Each 10% probability band's hit rate inside its 90% confidence range.
- All of the above also hold on the locked months (Aug-Oct 2026), run once at the end.

Tuning uses only days up to `backtest.tuning_end` (2026-07-31). Never look at the locked
months until the design is frozen. The city shortlist is reported separately, chosen
only from data before the test months.

## Model
1. NBM NBP percentiles (10/25/50/75/90) -> smooth CDF; tails extended with NBM's spread.
2. Per city: shift every percentile by a bias, stretch gaps around the median by a
   spread factor (keeps NBM's skew). Both drift smoothly through the year.
3. Fit only on days before the forecast day; thin cities shrink to the all-city average.
4. NBM v5.0 starts from the old version's fit and takes over by
   `model.version_handover_days`.
5. Whole degrees: P(k) = F(k+0.5) - F(k-0.5); brackets inclusive; direction from
   `strike_type`; `model.settlement_noise` mixed in (`weatherbot/model/brackets.py`).
6. Trading probability = learned blend of model and market price.
7. Extra winter widening (`model.winter_spread_factor`) until v5.0 has seen a winter.

## Backtest
- One decision per day at `backtest.decision_hour_utc` (16 UTC) the day before.
- Forecast: latest NBM run with `available_at` <= decision time. Calibration: NWS highs
  for days before the decision day only.
- Buy YES at the latest yes ask, NO at 1 - latest yes bid; quotes older than
  `backtest.quote_max_age_hours` count as none. Taker fee on every trade
  (`weatherbot/kalshi/fees.py`).
- Trade only if `edge_ok` (absolute and relative edge after fee).
- Size per city-day with joint Kelly (`weatherbot/model/kelly.py`), times
  `risk.kelly_fraction`; caps per city-day, region and day; daily stop; drawdown halt.
- Payout from Kalshi's actual settlement (`kalshi_settled`), not the NWS proxy.
- Walk-forward: refit monthly, score the next month.
- Headline = taker run. Maker run is labelled optimistic and can't pass Gate A alone.
- Diagnostics: raw NBM vs market log loss, edge decay at 15/16/18 UTC, tails on their
  own, predicted vs realised edge, max drawdown.

## Build order
1. Config split (`config.local.yaml`) and Phase 2 settings. DONE
2. Price backfill: `python -m weatherbot.backtest.prices` -> `kalshi_candles`. DONE
3. Fees, bracket math, event Kelly, test-first. DONE
4. Calibrated model (`weatherbot/model/nbm_dist.py`, `calibrate.py`) and scoring report
   (`python -m weatherbot.backtest.score`, tuning months only). DONE (code); real-data
   scores pending the price backfill.
5. Backtest engine (taker and maker runs, trades CSV, profit reports).
6. Gate A report.
