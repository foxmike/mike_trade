# XGBoost BUY on 5-minute and 15-minute candle windows

Two alternative experiments (the original `xgboost_buy_tb_pooled` model is untouched):

| | 5-minute | 15-minute |
|---|---|---|
| config | `config/modeling/xgboost_buy_5m_tb100_050.yaml` | `config/modeling/xgboost_buy_15m_tb100_050.yaml` |
| model id / version | `xgboost_buy_5m_tb100_050` / `1.0.0` | `xgboost_buy_15m_tb100_050` / `1.0.0` |
| experiment id | `xgboost_buy_5m_tb100_050_v1` | `xgboost_buy_15m_tb100_050_v1` |
| lookback | 10 completed 5m candles (50 min) | 6 completed 15m candles (90 min) |
| label horizon | 12 candles = 60 min | 6 candles = 90 min |
| recency half-life | 3 candles (15 min) | 2 candles (30 min) |
| features | 83 | 55 |

Question: *does price rise +1.0% before a -0.5% adverse move from the entry price, within the horizon?*
Run both with `scripts/train_xgboost_intraday_5m_15m.sh`.

## Data
Candles are derived locally from the stored IEX one-minute raw datasets (`mike_trade/modeling/aggregation.py`);
no data is downloaded or duplicated. Buckets start at the 09:30 ET open (also on 13:00 early-close days), contain
only regular-session minutes, and are emitted only when their window has elapsed. A missing minute is never filled
(the IEX feed is one venue: no bar means no IEX trade); a bucket with no bar at all leaves a gap that resets the
feature window. Training/early-stopping/calibration/evaluation dates, universe, seed, embargo and hyperparameters
are copied from `xgboost_buy.yaml`; the reserved final holdout is rejected by the range validation. No search is run.

## Features (`mike_trade/modeling/candle_features.py`, family `intraday_candle_window@1.0.0`)
All features use only the last `lookback_bars` completed candles of the same session; position is in the name
(`lag0` = most recent, `lag{N-1}` = oldest).
* per candle: `body_pct`, `upper_wick`, `lower_wick` (fractions of the range), `range_pct`, `relative_volume`
  (vs the window mean) for every lag; `return` (close-to-close) for lags 0..N-2; `acceleration` (return change) for
  lags 0..N-3 - the oldest candle has no earlier close inside the window, so those entries do not exist;
* recency-weighted summaries `rw_<metric>` with weights `0.5 ** (lag / half_life_bars)` normalized over the lags a
  metric has (config: `features.recency_decay.half_life_bars`). These weight candles inside the input window; no
  sample weighting by training date is used;
* window features (`window_return`, `window_range_pct`, `window_close_position`, `window_volatility`,
  `close_vs_window_mean`), `vwap_distance`, `time_of_day_sin/cos`, `relative_strength_spy`.
Training is the streaming `update()` applied to a sequence, so training and live-style features are one computation.

## Labels (`AggregatedTripleBarrierLabelGenerator`)
Entry = next candle's open (+1 bp cost as in the original model); barriers = entry x (1 + 0.010) and entry x (1 - 0.005).
The full horizon must exist, contiguous, inside the signal's session - otherwise no label is made (no label crosses
the close or the next day, and late-session signals are excluded rather than given truncated windows, which would
bias the class mix). If one candle touches both barriers the order is taken from the underlying one-minute bars; if
a single minute still touches both (or the minute bars are unavailable) the outcome is the conservative LOSS.
Gaps through a barrier are filled at the open for a loss and at the barrier for a win. `horizon_bars` counts candles
(`label.parameters`: `bar_minutes`, `horizon_minutes`).

## Outputs
`model-prepare` lock (exact datasets + counts), `model-train` artifact/manifest/evaluation, `model-report`
(`model_report.md/json`, CSVs incl. `favorable_deciles.csv`), and `outcome_summary.txt` (barrier frequencies per
partition, confusion matrix, calibration, score distribution, fit-vs-held-out metrics, decile table, AUC). All are
predictive/label statistics and not trading results.

## Limits
* Serving: `ModelInferenceService` serves candle-window models from the same one-minute bars the
  1m models use (see "Backtesting and serving" below).
* Signals exist only after the lookback is full (A: from 10:20 ET, B: from 11:00 ET) and while the full horizon fits
  in the session (A: until 15:00 ET, B: until 14:30 ET).
* IEX coverage is thin for some symbols; they contribute few contiguous windows (see the lock's counts and
  `build_stats`).

## Backtesting and serving

The inference service, the model-directed strategies and the backtest runner serve a candle-window
model through the unchanged lifecycle (entries, protective stops, end-of-day liquidation):

* **Same code as training.** The one-minute bars of the lookback span are aggregated by the training
  `CandleAggregator` (session-open-aligned buckets, early closes included, no bridging of missing
  minutes) and featurized by the training `CandleWindowFeaturePipeline`.  The feature names and
  order are checked against the manifest on load.
* **Half-life.** The manifest records the feature names and the SHA-256 of the training
  configuration, not the recency half-life.  On load the half-life, lookback and candle policy are
  read from the training configuration under `config/modeling/` whose SHA-256 equals the
  manifest's, and the rebuilt pipeline must reproduce the recorded feature schema exactly; if the
  file is missing or differs the model is refused (`SCHEMA_MISMATCH`), never guessed.
* **Completed candles only.** A prediction is made only when the latest completed one-minute bar
  is the last minute of an N-minute candle (`NOT_CANDLE_BOUNDARY` on every other minute).  Bars
  that ended after the observation are never used; a reference candle is closed by the observation
  time.  The prediction's `observation_start`/`observation_end` span the candle; `timeframe`
  stays the source series (`1m`) and `horizon_bars` stays in candles.
* **Signals** therefore occur at candle closes (5m from 10:20 ET, 15m from 11:00 ET at the
  earliest), and the entry fills at the next minute's open, which is the next candle's open.
* **Missing minute at a candle close.** If IEX printed no trade in a candle's last minute, there
  is no one-minute bar to trigger an evaluation at that instant, so that candle is not scored (it
  is still part of later windows).  Training would have scored it; the candle features are never
  approximated.
* **Scope.** Verified for BACKTEST.  Only BUY classification models of the `intraday_candle_window`
  family are served; SELL candle models do not exist.

`scripts/backtest_xgboost_intraday_ibm_tsla.sh` runs the two models on IBM and TSLA over the
models' evaluation window (4 concurrent jobs) and writes a consolidated comparison to
`research/analysis/xgboost_intraday_ibm_tsla/`.
