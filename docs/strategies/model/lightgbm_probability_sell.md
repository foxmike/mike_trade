# `lightgbm_probability_sell`

## Identity and status

- Role: independent SELL (exits a long; never opens or shorts)
- Algorithm: LightGBM (native text); the strategy accepts only an LightGBM artifact
- Category: `machine_learning` / `gradient_boosted_trees`; version `1.1.0`
- Implementation: `mike_trade.strategies.sell.lightgbm_probability_sell:LightgbmProbabilitySell`, decision logic in
  `mike_trade.strategies.model_probability.ModelProbabilitySell`
- Status: **implemented; ready for targeted historical backtesting and SHADOW evaluation**. No
  profitability claim is made for any model or threshold.

## Contract

Purpose: exit the entire open long when an explicitly pinned LightGBM position-continuation model favors
an exit outcome over holding.

The SELL model is trained and configured **independently of the BUY model**. Its classes are
`-1` = holding would reach the risk barrier (`RISK_EXIT`), `0` = exiting now beats holding
(`EXIT_FAVORABLE`), `1` = holding beats exiting now (`HOLD_FAVORABLE`). Its feature vector is the shared
completed-bar features plus four position features computed from the **actual** position
(`position_unrealized_return`, `position_holding_bars`, `position_max_favorable_return`,
`position_max_adverse_return`; definitions in [MODEL_STRATEGIES.md](../../MODEL_STRATEGIES.md) §4), using the real
average entry fill price and entry fill time.

Required data (supplied by the composition root):

- `context.model_inputs.position`: the real open long (`PositionFacts`: symbol, cycle id, quantity,
  entry price, entry time). Absent when flat → `NOT_ELIGIBLE` (`NO_POSITION`), no inference.
- A `ServedPrediction` for this pin with the BUY-strategy conditions (identity, checksum, algorithm `lightgbm`,
  role `sell`, symbol, feed, `1m`/`raw`, latest bar, freshness, horizon, training universe) **and**
  `position_fingerprint == position.fingerprint`, classes exactly `-1, 0, 1`.
- 30+ completed bars, fresh context, regular session.

Exact rule:

1. Require a real open long and a valid prediction bound to it; otherwise `NO_SIGNAL`/`NOT_ELIGIBLE`/
   `INSUFFICIENT_DATA`/`STALE_DATA` with a finer audit reason.
2. Require the manifest to record label `sell_continuation` and the exact class meanings above.
3. In `MODEL_DIRECTED` mode, exit when `P(-1) + P(0) > P(1)`; ties hold.
4. In optional `THRESHOLD` mode, apply `minimum_exit_probability_bps` and the independent
   `minimum_risk_exit_probability_bps` gate.
5. Emit one `SIGNAL`; normal exit arbitration closes the whole position.

An ML SELL is an ordinary discretionary exit. It never overrides a protective stop, manual close,
hard-risk or forced-liquidation exit, and it is silent for positions the model cannot judge
(held across sessions, or longer than its trained holding support).

| Name | Kind | Default | Range / pattern |
| --- | --- | --- | --- |
| `decision_mode` | ENUM | `MODEL_DIRECTED` | `MODEL_DIRECTED`, `THRESHOLD` |
| `model_id`, `model_version`, `artifact_sha256` | STRING | required, required, `""` | as BUY |
| `minimum_exit_probability_bps` | INT | 6000 | 1–10000 |
| `minimum_risk_exit_probability_bps` | INT | 0 | 0–10000 (0 disables) |
| `maximum_prediction_age_seconds` | INT | 90 | 1–600 |
| `expected_horizon_bars` | INT | 0 | 0–10000 |
| `require_trained_symbol` | BOOL | true | |

Persistent state keys: `last_signal_bar`, `model_audit`.

## Traceability

| Rule | Python | Verification |
| --- | --- | --- |
| Model-directed exit/hold comparison and optional thresholds | `model_probability.ModelProbabilitySell.decide` | `test_model_directed_sell_requires_exit_direction_to_strictly_beat_hold`, `test_sell_holds_when_continuing_is_favored`, `test_sell_risk_trigger_is_independent_and_disabled_by_default` |
| Actual entry fill price and time reach the model | `ModelInferenceService._position_vector`; `position_feature_values` | `test_sell_prediction_uses_the_actual_entry_fill_price`, `test_position_features_equal_the_training_builder_features`, `test_model_sell_receives_the_actual_entry_fill_and_exits_a_winner`, `test_paper_model_buy_and_sell_use_confirmed_broker_fills` |
| Never signals without a position; prediction bound to this exact position | `decide`, `PositionFacts.fingerprint` | `test_sell_never_signals_without_a_real_position`, `test_sell_prediction_must_belong_to_this_exact_position`, `test_sell_strategy_is_inert_without_a_position` |
| Out-of-support positions and missing entry bar | `ModelInferenceService` | `test_sell_requires_a_valid_position_inside_training_support` |
| Minimum hold and full exit through existing arbitration | existing `arbitrate_exit_decisions` | `test_model_sell_respects_the_existing_minimum_hold_rule`, `test_multiple_sell_strategies_close_once`, `test_shadow_model_buy_then_model_sell_uses_the_real_fill_and_keeps_the_stop` |
| Protective stop still governs; model "hold" cannot disable it | existing stops | `test_protective_stop_outranks_a_model_that_prefers_to_hold`, `test_shadow_protective_stop_is_not_disabled_by_a_model_that_prefers_to_hold` |
| Per-scenario positions do not leak | provider/fingerprint | `test_shadow_history_of_another_account_does_not_leak_position_facts` |
| Mixing with BUY models of the other algorithm and with technical BUY/SELL | existing collections | `test_mixed_algorithm_and_technical_collections` |

Known limitations: see [MODEL_STRATEGIES.md](../../MODEL_STRATEGIES.md) §11.
