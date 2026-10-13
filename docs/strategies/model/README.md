# Model-driven strategies (XGBoost, LightGBM)

Four independent identities, each configured with its own pinned model, thresholds and symbols:

| Strategy ID | Role | Specification |
| --- | --- | --- |
| `xgboost_probability_buy` | BUY | [specification](xgboost_probability_buy.md) |
| `xgboost_probability_sell` | SELL | [specification](xgboost_probability_sell.md) |
| `lightgbm_probability_buy` | BUY | [specification](lightgbm_probability_buy.md) |
| `lightgbm_probability_sell` | SELL | [specification](lightgbm_probability_sell.md) |

The XGBoost and LightGBM identities differ only in the algorithm of the artifact they accept; all four
share `mike_trade.strategies.model_probability`. Inference, pinning, provenance, failure behavior, audit,
mode wiring and operator guidance are in [../../MODEL_STRATEGIES.md](../../MODEL_STRATEGIES.md). Training is in
[../../MODEL_TRAINING_ARCHITECTURE.md](../../MODEL_TRAINING_ARCHITECTURE.md). Any BUY may be combined with any SELL
(model of either algorithm, or technical) on one symbol; the existing arbitration, minimum hold,
protective stops and market-status entry gate apply unchanged.

These documents specify decision rules over a trained model's output. They make no claim that any model or
threshold is profitable.
