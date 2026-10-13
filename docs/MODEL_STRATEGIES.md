# Model-driven BUY/SELL strategies (XGBoost and LightGBM)

This document describes how trained XGBoost and LightGBM artifacts take part in Mike_Trade's
ordinary BUY/SELL strategy lifecycle. Training is documented in
[MODEL_TRAINING_ARCHITECTURE.md](MODEL_TRAINING_ARCHITECTURE.md); the per-strategy specifications
are in [strategies/model/](strategies/model/README.md).

**Probabilistic inference is kept apart from deterministic policy.** A model produces calibrated
class probabilities for the label it was trained on. Everything that decides what happens next —
thresholds, entry/exit arbitration, minimum hold, sizing, budgets, buying-power checks, protective
stops, market-status entry blocks, kill switches, reconciliation — is the existing deterministic
engine and is unchanged. Nothing in this document, and no configuration shipped with it, claims
that any model or threshold is profitable. Whether one is must be established by historical
evidence on data the model did not see.

## 1. What was added

| Strategy ID | Role | Consumes |
| --- | --- | --- |
| `xgboost_probability_buy` | BUY | a pinned XGBoost triple-barrier (or regression) BUY model |
| `lightgbm_probability_buy` | BUY | a pinned LightGBM triple-barrier (or regression) BUY model |
| `xgboost_probability_sell` | SELL | a pinned XGBoost position-continuation SELL model |
| `lightgbm_probability_sell` | SELL | a pinned LightGBM position-continuation SELL model |

They are ordinary registered strategies (`category: machine_learning`) sharing one pure decision
core, `mike_trade.strategies.model_probability`. The algorithm is the only difference between an
XGBoost and a LightGBM identity. They participate in the same collections, the same entry and exit
arbitration, the same lifecycle (`WATCHING_BUY → BUY_PENDING → … → COOLDOWN`), and the same
single-position, full-exit rules as every technical strategy. There is no ML-only engine, broker
path, arbitration, or dashboard.

## 2. How a prediction reaches a pure strategy

```text
 completed 1m candles ─┐                                   ┌─ pure strategy.evaluate(context, params, state)
 per-bar VWAP ledger  ─┼─► ModelInputProvider ──► MarketContext.model_inputs ─┤   returns SIGNAL / NO_SIGNAL only
 SPY (reference) bars ─┤        │                                              └─ reasons → audit (state, log, backtest row)
 real position (SELL) ─┘        ▼
                      ModelInferenceService  (artifacts loaded once, checksum-verified,
                       shared feature pipeline, bounded caches, no broker, no training)
```

* **`mike_trade.domain.model_signals`** holds the immutable values (`ModelReference`,
  `PositionFacts`, `ServedPrediction`, `ModelPredictionResult`, `ModelInputs`). It imports only the
  standard library. `MarketContext` gained one optional field, `model_inputs`, which is `None` for
  every non-model strategy.
* **`mike_trade.modeling.inference.ModelInferenceService`** is the only place an artifact is loaded.
  The composition root creates it with explicit pins; it never trains, reloads, or swaps a model.
* **`mike_trade.modeling.serving.ModelInputProvider`** turns the already-held completed candles
  (and the real position, for SELL) into an inference request and returns `ModelInputs`.
  The same provider serves BACKTEST, SHADOW, PAPER and LIVE.
* The strategy classes import neither XGBoost, LightGBM, `numpy`, `mike_trade.modeling`, a broker,
  persistence, the network, nor a clock (enforced by an AST test). Strategies never load model files.

Only a strategy that can act receives inputs: a BUY model while the symbol is flat and armed, a SELL
model while a real long position is open. A BUY model while holding, or a SELL model while flat,
is evaluated advance-only exactly like any other ineligible role and triggers no inference.

## 3. Pinning and startup validation

A model is identified by `model_id` and an exact `MAJOR.MINOR.PATCH` `model_version` carried in the
strategy's own parameters (so different symbols or scenarios can use different models). `latest`
does not parse. An optional `artifact_sha256` pins the artifact bytes as well. There is no implicit
promotion: training a new version leaves every running deployment on the version it names.

At startup (`build_runtime`), when at least one model-driven strategy is configured, every distinct
pin is loaded once from `service.model_root` (default `research/models`, read-only) and verified:

| Check | Startup error code |
| --- | --- |
| manifest/artifact present for exactly that id and version | `MODEL_MODEL_MISSING` |
| artifact size and SHA-256 match the manifest (and the optional pin) | `MODEL_CHECKSUM_MISMATCH` |
| schema-2 manifest, internally consistent | `MODEL_MANIFEST_INVALID` |
| manifest algorithm and adapter equal the strategy's algorithm (no XGBoost↔LightGBM fallback) | `MODEL_ALGORITHM_MISMATCH` |
| BUY strategy ↔ BUY model, SELL ↔ SELL (derived from the feature schema) | `MODEL_ROLE_MISMATCH` |
| the shared feature pipeline reproduces the manifest's exact feature names and order; SELL ends with the four position features; native model feature order equals the manifest | `MODEL_SCHEMA_MISMATCH` |
| 1-minute RAW observations only | `MODEL_PROVENANCE_UNSUPPORTED` |
| the model's training feed equals `market_data.feed` (IEX is never used for a SIP model or vice versa) | `MODEL_FEED_MISMATCH` |
| the adapter can load the native artifact (no pickle is ever used) | `MODEL_ADAPTER_UNAVAILABLE` |

These are `ConfigurationError`s: the runtime does not start. No model library is imported unless a
model-driven strategy is configured.

## 4. Inputs: the point-in-time contract

Features come from `IntradayFeaturePipeline`, the single implementation used for training. For each
prediction the service replays the last *window* completed bars (21 for `intraday_core` v1) through a
fresh pipeline, which is value-for-value what the streaming and batch paths produce (verified by
tests against `transform()` and against the SELL training builder). Consequently:

* Only completed regular-session bars are used. The observation is the last completed bar; its end
  (`observation_end`) is the timestamp the prediction is valid for.
* The window must be contiguous within one session; a missing minute, a session change, or a feed or
  adjustment change resets the series and no prediction is made (`FEATURES_UNAVAILABLE` /
  `INSUFFICIENT_HISTORY`).
* `vwap_distance` uses **each bar's own VWAP** exactly as training did (Alpaca's per-bar `vw`).
  Operational candles carry none, so the composition root records it from the stream, the warm-up
  history, or the replay dataset (`BarVwapLedger`). A bar without one yields `VWAP_UNAVAILABLE`; it is
  never replaced by session VWAP or any other definition.
* Declared reference series (SPY for the shipped configurations) must have the same-minute bar with the
  same feed. The runtime subscribes them even when the market-status monitor is off. A model never
  predicts for one of its own reference symbols.
* Every bar must have the model's feed, timeframe (`1m`) and adjustment (`raw`)
  (`FEED_MISMATCH`, `TIMEFRAME_MISMATCH`, `ADJUSTMENT_MISMATCH`). IEX and SIP are never mixed or
  substituted — at startup, per bar, and in historical replay.
* A model scores any symbol, but `symbol_in_training_universe` is recorded and strategies refuse
  untrained symbols unless `require_trained_symbol: false`.

### SELL position features (actual position facts)

The SELL model's schema is the base features plus four position features, defined once in
`mike_trade.modeling.objectives.position_feature_values` and called by both the training builder and
inference:

| Feature | Definition |
| --- | --- |
| `position_unrealized_return` | `close(latest completed bar) / entry_price − 1` (fraction) |
| `position_holding_bars` | completed bars from the **entry bar** (inclusive) through the latest completed bar, so the entry bar's own completion is `1` |
| `position_max_favorable_return` | `max(high over those bars) / entry_price − 1` |
| `position_max_adverse_return` | `min(low over those bars) / entry_price − 1` |

`entry_price` is the **actual average entry fill price** of the real position and the entry bar is
the 1-minute bar containing the **actual entry fill time** (floored to the minute), taken from the
SHADOW account, the PAPER/LIVE ledger's confirmed fills, or the BACKTEST cycle. It is never a
training-time hypothetical. Position state is built per call from that one position
(`PositionFacts`: symbol, cycle id, quantity, entry price, entry time) and fingerprinted; the
prediction records the fingerprint and the strategy refuses a prediction whose fingerprint is not the
current position's, so nothing leaks between symbols, scenarios, accounts or runs.

Fail-closed position cases: entry price or time unavailable (no facts → no SELL), entry bar not in the
completed-bar history (`POSITION_ENTRY_BAR_UNAVAILABLE`), position opened in an earlier session or held
longer than the model saw in training (`POSITION_OUTSIDE_TRAINING_SUPPORT`, limit =
`maximum_holding_bars − horizon_bars` from the manifest, 35 for the shipped example). Outside that
support the model is simply silent and other exits and the protective stop remain.

*Known differences from training:* training entered at the next bar's open plus a modeled adverse
bps; the live feature uses the real fill. The entry bar's high/low include the part of that bar before
the fill, as in training (entry at the bar open).

## 5. Predictions and provenance

`ServedPrediction` is immutable and carries: model id, version, **artifact SHA-256**, algorithm, role,
task, symbol, feature schema id/version/SHA-256, feed, timeframe, adjustment, `observation_start`,
`observation_end`, `predicted_at`, label horizon, class labels with probabilities (or the regression
value), modeled per-class net returns (when the label schema defines them), the SELL position
fingerprint and position features, and the inference time. It is rejected at construction if
probabilities are not finite, in `[0, 1]`, summing to 1 across exactly the class labels, or if it is
dated before its observation completed.

Every failure is a typed `PredictionFailure` (never an exception into a strategy, never a prediction):

`MODEL_NOT_LOADED`, `ROLE_MISMATCH`, `ALGORITHM_MISMATCH`, `SYMBOL_MISMATCH`,
`SYMBOL_NOT_IN_TRAINING_UNIVERSE`, `PRIMARY_IS_REFERENCE_SYMBOL`, `FEED_MISMATCH`,
`TIMEFRAME_MISMATCH`, `ADJUSTMENT_MISMATCH`, `SCHEMA_MISMATCH`, `PROVENANCE_MISMATCH`,
`NOT_REGULAR_SESSION`, `INSUFFICIENT_HISTORY`, `FEATURES_UNAVAILABLE`, `REFERENCE_BAR_MISSING`,
`VWAP_UNAVAILABLE`, `FUTURE_OBSERVATION`, `STALE_OBSERVATION`, `NONFINITE_PREDICTION`,
`INVALID_PROBABILITIES`, `INFERENCE_ERROR`, `INFERENCE_TIMEOUT`, `POSITION_REQUIRED`,
`POSITION_INVALID`, `POSITION_ENTRY_BAR_UNAVAILABLE`, `POSITION_OUTSIDE_TRAINING_SUPPORT`.

The strategy independently re-verifies identity, version, optional checksum, algorithm, role, symbol,
feed, timeframe/adjustment, horizon, that `observation_end` equals the **latest** completed candle's
end (a prediction for an earlier candle is never presented as the later one), that it is not after
`context.as_of`, and its age (`maximum_prediction_age_seconds`). XGBoost's float32 softmax output is
renormalized only for rounding error (≤ 1e-4); a genuinely invalid distribution is rejected.

Predictions are cached by (model, symbol, observation end, position fingerprint, bar-content digest)
in a bounded LRU; base features are cached by (feature schema, symbol, observation end, digest) so an
XGBoost and a LightGBM model on one symbol share one feature computation. A revised bar with the same
timestamp changes the digest and cannot hit a stale entry. Models are loaded once; each model has its
own predict lock; single-row prediction is pinned to one thread in both libraries; a call slower than `max_inference_seconds`
(default 2) is refused. `ModelInferenceService.diagnostics()` reports requests, cache hits, feature
reuse, predictions, slow calls, latency, and failure counts by code.

## 6. Strategy parameters and decision rules

Thresholds are integer basis points (5000 = 50.00%) because strategy parameters are scalar and floats
are not accepted by the configuration schema. `STRING` is a new parameter kind with an optional
`pattern`; model ids and versions are validated at configuration time.

### BUY (`*_probability_buy`)

| Parameter | Default | Meaning |
| --- | --- | --- |
| `model_id`, `model_version` | required | exact pin (`[a-z][a-z0-9_]*`, `X.Y.Z`) |
| `artifact_sha256` | `""` | optional 64-hex checksum pin |
| `minimum_favorable_probability_bps` | 5000 | required `P(favorable_class)` |
| `maximum_adverse_probability_bps` | 3000 | cap on `P(adverse_class)`; 10000 disables |
| `use_expected_value` / `minimum_expected_value_bps` | false / 0 | also require the barrier-implied expected net return |
| `favorable_class` / `adverse_class` | `"1"` / `"-1"` | class labels (triple barrier: `1` win, `-1` loss, `0` timeout) |
| `maximum_prediction_age_seconds` | 90 | age of the observed bar (see §10) |
| `expected_horizon_bars` | 0 | required label horizon; 0 accepts the model's own |
| `require_trained_symbol` | true | refuse instruments outside the training universe |

The defaults are neutral reference values, not fitted. **A probability above 50% is not an expected
profit.** The favorable-class probability is the model's belief that the take-profit barrier is hit
first; whether that pays depends on barrier sizes, the loss and timeout classes, and costs. With
`use_expected_value` the strategy computes `Σ P(c)·r(c)` using the model's own label schema
(`r(win) = (1+tp)(1−exit_bps) − 1`, `r(loss) = (1−sl)(1−exit_bps) − 1`, timeout assumed flat before the
exit cost; returns are measured from the modeled entry fill exactly as the training label does) and requires it to reach
`minimum_expected_value_bps`. It is an estimate and says so (`expected_value_basis` in the audit);
it is unavailable (and the strategy declines) for models whose label schema does not define class
returns. A **regression** BUY model (forward-return label) compares its predicted net return directly
with `minimum_expected_value_bps`. No monetary value is ever fabricated from class probabilities.

A BUY signal is `SIGNAL` with the closed bar's start and its close as reference price. Sizing, entry
arbitration, budgets, buying power, order creation and protection stay in the engine.

### SELL (`*_probability_sell`)

Classes: `-1` holding would reach the risk barrier, `0` exiting now beats holding, `1` holding beats
exiting now.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `model_id`, `model_version`, `artifact_sha256`, `maximum_prediction_age_seconds`, `expected_horizon_bars`, `require_trained_symbol` | as BUY | |
| `minimum_exit_probability_bps` | 6000 | exit when `P(−1) + P(0)` reaches this |
| `minimum_risk_exit_probability_bps` | 0 | also exit when `P(−1)` alone reaches this; 0 disables |

A SELL signal needs a real open long, a valid fresh prediction bound to that exact position, and either
threshold. It is an ordinary discretionary exit: `arbitrate_exit_decisions` applies the configured
minimum hold and pending-exit rules, protective/manual/hard-risk/forced exits outrank it, and the engine
closes the entire position. It never signals for a nonexistent position, never partially liquidates,
and never pyramids.

Reasons are reported through the existing SDK as `NO_SIGNAL` (below/above threshold, hold preferred),
`INSUFFICIENT_DATA` (no inputs, missing/failed/mismatched prediction, warm-up), `STALE_DATA` (stale
context, stale or future-dated prediction, prediction not for the latest bar), or `NOT_ELIGIBLE`
(not armed, no position, closed session, untrained symbol). A finer `reason` string is in the audit.

## 7. Configuration

A complete SHADOW example is [`config/model_strategies.example.yaml`](../config/model_strategies.example.yaml):

```yaml
service:
  model_root: research/models        # read-only repository written by `make model-train`
market_data:
  feed: iex                          # must equal every pinned model's training feed
symbols:
  AAPL:
    buy_strategies:  [aapl_xgb_buy]
    sell_strategies: [aapl_lgbm_sell]       # XGBoost entry, LightGBM exit
    stop_policy: {type: native_trailing, trail_percent: "1.5"}
  MSFT:
    buy_strategies:  [msft_lgbm_buy, msft_xgb_buy]          # compete in normal entry arbitration
    sell_strategies: [msft_xgb_sell, vwap_breakdown_default] # ML and technical exits together
strategies:
  aapl_xgb_buy:
    plugin: xgboost_probability_buy
    side: buy
    parameters: {model_id: xgboost_buy_tb_pooled, model_version: 1.0.0,
                 minimum_favorable_probability_bps: 5500, maximum_adverse_probability_bps: 2500}
  aapl_lgbm_sell:
    plugin: lightgbm_probability_sell
    side: sell
    parameters: {model_id: lightgbm_sell_continuation_pooled, model_version: 1.0.0,
                 minimum_exit_probability_bps: 6500}
```

Per-symbol model assignments are simply per-symbol strategy entries with different parameters. BUY and
SELL models, algorithms, versions and thresholds are independent: every combination in
(XGBoost|LightGBM|technical) × (BUY|SELL) is supported, including several BUY and several SELL
strategies on one symbol. A plugin may appear once per symbol and side (existing rule). A change of
pins or thresholds is an ordinary configuration change (restart or the existing config controls);
nothing changes a running model automatically.

## 8. Execution modes

| Mode | Wiring |
| --- | --- |
| BACKTEST | `BacktestRunner(..., model_service=service)`. Same-minute reference bars are looked up by **availability time** (`ReplayReferenceHistory`), so coverage never depends on ticker spelling. Per-bar VWAP comes from the replay candle. The runner refuses model strategies without a service, an unloaded pin, or a feed that differs from the dataset's. The manifest configuration records `model_lineage` (artifact checksum, schemas, training/validation ranges, and whether the run window overlaps them). `research` runs take `ExperimentSpec.model_root` and load reference series from the **same feed**; a missing or other-feed reference snapshot is an error. |
| SHADOW | `VwapReferenceLifecycle.bind_model_inputs(provider)`; `_evaluate_binding` attaches inputs for an armed BUY and a SELL with the account's real position (`avg_cost`, `opened_at`). The existing simulated broker, ledger and protective stops are untouched; each scenario's SELL prediction is bound to its own position. |
| PAPER / LIVE | Same hook in `LiveStrategyLifecycle`. Position facts come from the ledger cycle's confirmed fills (`avg_entry_price`, `opened_at`) and are absent (so no model SELL) if unconfirmed. PAPER/LIVE remain governed by the existing capability, authorization, reconciliation and protection requirements. |

The same artifact, feature code, inference contract and decision code run in all four. This work did not
enable PAPER or LIVE, place orders, or modify any running service.

**Timing.** Because the production candle builder closes a bar when the next minute's bar arrives, the
newest completed bar is normally ~60–75 s old at evaluation. Keep `maximum_prediction_age_seconds ≥
market_data.max_bar_age_seconds`. Reference and traded symbols are separate streams, so SPY's bar for a
minute can close just after the traded symbol's. The lifecycle remembers a bar that waited only for the
reference series and re-evaluates that symbol once, when the reference bar closes (existing same-bar
duplicate suppression makes this safe); otherwise that minute is skipped.

## 9. Protective stops, crash monitor, and failure isolation

* A model failure never disables broker-native protection or any deterministic strategy. An inference
  problem yields an unavailable prediction for that one strategy; a defect inside a model strategy is
  caught and reported as `EVALUATION_ERROR` without a signal.
* The protective stop is armed and managed by the engine independently of any SELL strategy; a model that
  says "hold" cannot loosen or cancel it, and a stop that triggers wins (tested in BACKTEST and SHADOW).
  A model SELL cannot override an emergency or protective exit.
* The market-status monitor is unchanged. It authorizes **new entries** (BUY/SHORT); it is not an
  automatic liquidation system. A model BUY during CRASH produces a signal that the existing gate refuses
  before arbitration (`ENTRY_REFUSED … MARKET_CRASH_ACUTE_RISK` in BACKTEST). Exits stay authorized.
* Missing, corrupt or unapproved artifacts are startup errors, not silent downgrades.

## 10. Audit and diagnostics

Each evaluation that is not idle produces a compact JSON record (`model_audit`): strategy and version,
symbol, bar start/end, model, configured thresholds, decision and reason, the observed probabilities in
basis points, the provenance of the prediction (`ServedPrediction.to_audit()`: checksum, schema hash,
feed, observation and prediction timestamps, probabilities), and for SELL the position facts and
position features. It is exposed in three existing places:

* the strategy's persisted state under `model_audit` (latest decision; idle evaluations are not stored);
* a structured `mike_trade.model` log record for every signal, and for each *change* of reason
  (unavailability is a WARNING);
* the BACKTEST `evaluations` rows (`model_audit`) of the run artifacts.

`StrategyEvaluation` is unchanged and carries no confidence field. Assignment 4's reporting should read
these records rather than create another channel.

## 11. Operator checklist and limitations

1. Train with the documented commands; inspect `model-inspect`; do not trade a model without
   out-of-sample evidence. Backtests whose window overlaps the model's training/validation ranges are
   in-sample (`window_overlaps_*` in the manifest). The reserved final holdout still requires its lock
   token and a frozen configuration; this work added no way around it.
2. Pin explicit versions (and optionally checksums); set `market_data.feed` to the training feed.
3. Confirm the reference symbol (SPY) bars flow and `market_data.warmup_bars ≥ 25`.
4. Start in SHADOW. Watch the `mike_trade.model` WARNINGs and `ModelInferenceService.diagnostics()`.
5. Keep a protective stop policy on every symbol.

Limitations: training data is roughly one year for 100 symbols; per-minute IEX data is single-venue;
models see only the features and horizons they were trained on (SELL is silent for positions older than
its training support); expected-value estimates rest on label-schema assumptions; the inference runs
inside the event loop (about a millisecond for the small test boosters; measure real models with `diagnostics()` before enabling many symbols);
reference-bar ordering in the live stream is handled by one bounded re-evaluation per bar.
