"""LightGBM probability SELL strategy (pinned LightGBM continuation model).

A thin registered identity over :class:`ModelProbabilitySell`; decision logic, parameters and
validation are shared with the other model SELL strategy.  See ``docs/STRATEGIES.md`` and
``mike_trade.strategies.model_probability``.  No LightGBM import appears here: predictions arrive
as immutable data on ``MarketContext.model_inputs``.
"""

from __future__ import annotations

from typing import ClassVar

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.model_probability import ModelProbabilitySell, model_strategy_spec

__all__ = ["LightgbmProbabilitySell"]


class LightgbmProbabilitySell(ModelProbabilitySell):
    """SELL an open long when the pinned LightGBM continuation model favors exiting."""

    ALGORITHM: ClassVar[str] = "lightgbm"
    spec = model_strategy_spec(
        strategy_id="lightgbm_probability_sell", algorithm="lightgbm", side=StrategyRole.SELL
    )
