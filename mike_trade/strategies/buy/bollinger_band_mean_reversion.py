"""BUY on a completed close crossing back above the lower Bollinger Band."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.finite import bollinger

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "bollinger_band_mean_reversion"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_PERIOD = 20
DEFAULT_DEVIATIONS = Decimal("2")
MAX_PERIOD = 100


def _required_bars(period: int) -> int:
    return period + 1


WARMUP_BARS = _required_bars(DEFAULT_PERIOD)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="mean_reversion",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "period",
                ParameterKind.INT,
                DEFAULT_PERIOD,
                2,
                MAX_PERIOD,
                description="Bollinger SMA period. Default 20.",
            ),
            ParameterSpec(
                "deviations",
                ParameterKind.DECIMAL,
                DEFAULT_DEVIATIONS,
                Decimal("0.1"),
                Decimal("5"),
                description="Standard deviation multiplier. Default 2.",
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when the prior completed close was strictly below the lower Bollinger Band "
        "and the latest completed close crosses back strictly above the lower Bollinger Band: "
        "close[t-1] < lower[t-1] and close[t] > lower[t]."
    ),
    source=(
        "John Bollinger, Bollinger on Bollinger Bands (McGraw-Hill, 2001); StockCharts "
        "ChartSchool, Bollinger Bands (mean reversion setup after lower band penetration): "
        "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/bollinger-bands"
    ),
    execution_limitations=(
        "Entry signal only: no stops, sizing, targets, or orders.",
        "Requires completed adjacent M1 bars; regular session and BUY eligibility apply.",
        "Population standard deviation is used; no look-ahead.",
    ),
    reason_codes=REASON_CODES,
)


class BollingerBandMeanReversionBuy(BuyStrategy):
    """BUY when close crosses back above the lower Bollinger Band after a close below it."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return _required_bars(period)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        period, deviations = values["period"], values["deviations"]
        assert isinstance(period, int) and isinstance(deviations, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required_bars(period))
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior_closes = [b.close for b in bars[-period - 1 : -1]]
        latest_closes = [b.close for b in bars[-period:]]
        lower_prior = bollinger(prior_closes, period, deviations)[0]
        lower_latest = bollinger(latest_closes, period, deviations)[0]
        triggered = bars[-2].close < lower_prior and bars[-1].close > lower_latest
        return finish(self.spec, context, bars[-1], state, triggered)
