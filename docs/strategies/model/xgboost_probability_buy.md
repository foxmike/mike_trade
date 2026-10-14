# `xgboost_probability_buy`

## Identity and status

- Role: independent BUY
- Algorithm: XGBoost (native UBJSON); the strategy accepts only an XGBoost artifact
- Category: `machine_learning` / `gradient_boosted_trees`; version `1.1.0`
- Implementation: `mike_trade.strategies.buy.xgboost_probability_buy:XgboostProbabilityBuy`, decision logic in
  `mike_trade.strategies.model_probability.ModelProbabilityBuy`
- Status: **implemented; ready for targeted historical backtesting and SHADOW evaluation**. No
  profitability claim is made for any model or threshold.

## Contract

Purpose: enter a long when an explicitly pinned XGBoost BUY model favors the favorable
outcome over the adverse outcome for the latest completed 1-minute bar.

Required data (supplied by the composition root, never fetched by the strategy):

- `context.candles[1m]` — at least 30 completed regular-session bars, fresh, `completed_bars_only`.
- `context.model_inputs` holding a `ServedPrediction` for exactly this pin: same id, version,
  optional checksum, algorithm `xgboost`, role `buy`, symbol, feed (= `context.feed_id`), `1m`/`raw`,
  `observation_end` equal to the latest completed candle's end and not after `as_of`, age within
  `maximum_prediction_age_seconds`, label horizon equal to `expected_horizon_bars` when set, symbol in the
  model's training universe unless `require_trained_symbol: false`.
- Engine-owned `state["eligibility"] == "WATCHING_BUY"`.

Exact rule (classification, e.g. triple-barrier `-1/0/1`):

1. Require every data condition above; otherwise `NO_SIGNAL` with the reason code in the table below.
2. In `MODEL_DIRECTED` mode, signal when `P(favorable_class) > P(adverse_class)`. The neutral
   class adds no gate, and ties decline.
3. In optional `THRESHOLD` mode, apply `minimum_favorable_probability_bps`,
   `maximum_adverse_probability_bps`, and the optional expected-value gate.
4. Emit one `SIGNAL` for the bar (`reference_price` = the bar's close). The same bar never signals twice.

In `THRESHOLD` mode, a regression model (`forward_return` label) signals when the predicted net return,
in basis points, is at least `minimum_expected_value_bps`. Regression is unsupported in
`MODEL_DIRECTED` because it has no favorable/adverse class probabilities.

A probability above 50% is **not** treated as positive expected value; the threshold is a probability
rule, and the optional expected-value test is a separate, explicitly estimated condition.

Parameters (`STRING` ids are validated at configuration time; thresholds are integer basis points):

| Name | Kind | Default | Range / pattern |
| --- | --- | --- | --- |
| `decision_mode` | ENUM | `MODEL_DIRECTED` | `MODEL_DIRECTED`, `THRESHOLD` |
| `model_id` | STRING | required | `[a-z][a-z0-9_]{0,62}` |
| `model_version` | STRING | required | `MAJOR.MINOR.PATCH` |
| `artifact_sha256` | STRING | `""` | 64 lowercase hex or empty |
| `minimum_favorable_probability_bps` | INT | 5000 | 1–10000 |
| `maximum_adverse_probability_bps` | INT | 3000 | 0–10000 (10000 disables) |
| `use_expected_value` | BOOL | false | |
| `minimum_expected_value_bps` | INT | 0 | −10000–10000 |
| `favorable_class` / `adverse_class` | STRING | `"1"` / `"-1"` | `[A-Za-z0-9_-]{1,32}` |
| `maximum_prediction_age_seconds` | INT | 90 | 1–600 |
| `expected_horizon_bars` | INT | 0 | 0–10000 (0 = any) |
| `require_trained_symbol` | BOOL | true | |

Persistent state keys: `last_signal_bar`, `model_audit`.

No-signal reason codes: `NOT_ELIGIBLE` (not armed, closed session, untrained symbol), `STALE_DATA` (stale
context, stale/future prediction, prediction not for the latest bar), `INSUFFICIENT_DATA` (warm-up, no
inputs, prediction missing/unavailable/mismatched), `NO_SIGNAL` (favorable outcome not preferred,
threshold rejection, or expected value below minimum). The finer reason (for example `BELOW_FAVORABLE_THRESHOLD`,
`PREDICTION_UNAVAILABLE:REFERENCE_BAR_MISSING`) is in the `model_audit` record.

The strategy only reports that an entry is warranted. Position sizing, entry arbitration, the
market-status entry gate, budgets, buying power, order creation, protective stops and broker execution
remain in the engine and always take precedence.

## Traceability

| Rule | Python | Verification |
| --- | --- | --- |
| Model-directed comparison and optional threshold gates | `model_probability.ModelProbabilityBuy.decide` | `test_model_directed_buy_has_no_absolute_or_neutral_probability_gate`, `test_model_directed_buy_holds_when_adverse_is_at_least_favorable`, `test_threshold_buy_mode_retains_absolute_probability_gates` |
| Probability > 50% is not expected value; regression | same | `test_favorable_probability_above_half_is_not_treated_as_positive_expectation`, `test_regression_model_uses_the_predicted_net_return` |
| Exact pin, no `latest`, no algorithm/role/checksum drift | parameter schema; `ModelInferenceService._load` | `test_parameters_pin_the_model_and_reject_latest_or_malformed_values`, `test_exact_version_pinning_never_resolves_latest`, `test_algorithm_role_and_checksum_pins_are_enforced`, `test_buy_refuses_mismatched_or_unsuitable_predictions` |
| Provenance, freshness, earlier-candle, invalid probabilities | `_Model._prediction`; `ServedPrediction` | `test_stale_and_future_dated_predictions_are_refused`, `test_prediction_for_an_earlier_candle_is_never_used_for_a_later_one`, `test_invalid_probability_distributions_cannot_be_constructed` |
| Missing/corrupt artifact, wrong feed, schema | `ModelInferenceService`, `build_runtime` | `test_missing_and_corrupt_artifacts_fail_at_startup`, `test_feed_mismatch_is_refused_not_substituted`, `test_startup_fails_clearly_for_missing_unpinned_and_wrong_feed_models` |
| Participates in normal BUY arbitration and mixes with technical/other-algorithm strategies | existing `arbitrate_entry_decisions` | `test_simultaneous_buy_signals_resolve_through_the_existing_arbitration`, `test_mixed_algorithm_and_technical_collections` |
| Market crash still blocks the entry; protective stop still governs | existing entry gate and stops | `test_market_crash_blocks_a_model_entry`, `test_shadow_market_crash_still_blocks_a_model_entry`, `test_protective_stop_outranks_a_model_that_prefers_to_hold` |
| Inference failure isolates only this strategy | `_evaluate_binding`, `ModelPredictionResult` | `test_missing_reference_series_blocks_only_the_model_strategy`, `test_shadow_inference_failure_isolates_only_the_model_strategy` |
| BACKTEST ↔ live stream path give the same prediction | `ModelInputProvider` | `test_live_stream_path_re_evaluates_when_the_reference_bar_closes_late` |
| No model library, broker, I/O or clock in the strategy | module imports | `test_strategy_modules_import_no_model_library_broker_or_io` |

Known limitations: see [MODEL_STRATEGIES.md](../../MODEL_STRATEGIES.md) §11. The model describes the label
it was trained on over roughly one year of data; thresholds are not fitted here.
