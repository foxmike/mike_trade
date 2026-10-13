"""XGBoost probability BUY strategy (pinned XGBoost triple-barrier model).

A thin registered identity over :class:`ModelProbabilityBuy`; decision logic, parameters and
validation are shared with the other model BUY strategy.  See ``docs/STRATEGIES.md`` and
``mike_trade.strategies.model_probability``.  No XGBoost import appears here: predictions arrive
as immutable data on ``MarketContext.model_inputs``.
"""

from __future__ import annotations

from typing import ClassVar

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.model_probability import ModelProbabilityBuy, model_strategy_spec

__all__ = ["XgboostProbabilityBuy"]


class XgboostProbabilityBuy(ModelProbabilityBuy):
    """BUY when the pinned XGBoost model's outcome probabilities clear the thresholds."""

    ALGORITHM: ClassVar[str] = "xgboost"
    spec = model_strategy_spec(
        strategy_id="xgboost_probability_buy", algorithm="xgboost", side=StrategyRole.BUY
    )
