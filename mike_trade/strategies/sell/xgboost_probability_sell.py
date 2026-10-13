"""XGBoost probability SELL strategy (pinned XGBoost continuation model).

A thin registered identity over :class:`ModelProbabilitySell`; decision logic, parameters and
validation are shared with the other model SELL strategy.  See ``docs/STRATEGIES.md`` and
``mike_trade.strategies.model_probability``.  No XGBoost import appears here: predictions arrive
as immutable data on ``MarketContext.model_inputs``.
"""

from __future__ import annotations

from typing import ClassVar

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.model_probability import ModelProbabilitySell, model_strategy_spec

__all__ = ["XgboostProbabilitySell"]


class XgboostProbabilitySell(ModelProbabilitySell):
    """SELL an open long when the pinned XGBoost continuation model favors exiting."""

    ALGORITHM: ClassVar[str] = "xgboost"
    spec = model_strategy_spec(
        strategy_id="xgboost_probability_sell", algorithm="xgboost", side=StrategyRole.SELL
    )
