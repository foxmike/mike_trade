"""Long entry when ADX confirms a developing uptrend (ADX crosses above its trend threshold)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, crossed_above, finish, gate, insufficient, known, parts
from mike_trade.strategies.indicators.extended import ADXCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "adx_trend_following"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class AdxTrendFollowingBuy(BuyStrategy):
    """BUY when ADX crosses above `threshold` while +DI is above -DI.

    Wilder's ADX measures trend strength; an ADX rising through 25 marks a trend that is
    becoming strong, and +DI > -DI says its direction is up.  This differs from
    ``dmi_crossover`` (a +DI/-DI cross with no strength condition).  ADX and DIs are Wilder's
    14-period values (StockCharts smoothing) resumed from persisted state.  Needs
    ``2 * period + 1`` completed bars (29 by default): the first ADX appears on bar
    ``2 * period`` and the crossing needs the prior value too.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="trend_following", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=29,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("period", ParameterKind.INT, 14, 2, 100,
                          description="Wilder smoothing length for DI and ADX. Default 14."),
            ParameterSpec("threshold", ParameterKind.DECIMAL, Decimal(25), Decimal(5), Decimal(60),
                          description="ADX level that marks a strong trend. Default 25."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when ADX crosses from at-or-below to above the threshold on a completed "
                     "bar and +DI is above -DI. Rule definition only; no profitability claim."),
        source=("J. Welles Wilder, New Concepts in Technical Trading Systems (1978); StockCharts "
                "ChartSchool, Average Directional Index (ADX): trend strength above 25, direction "
                "from +DI versus -DI."),
        execution_limitations=("Entry signal only: no stops, sizing, targets, or orders.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply.",
                               "A flat window (zero smoothed true range) has no DI and contributes DX = 0."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return 2 * period + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period, threshold = validated["period"], validated["threshold"]
        assert isinstance(period, int) and isinstance(threshold, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, 2 * period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{period}",
                      fresh=lambda: ADXCalculator(period), restore=ADXCalculator.restore,
                      feed=lambda calc, bar: calc.update(bar))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        _, _, adx_prior = parts(adv.prior, 3)
        plus, minus, adx_latest = parts(adv.latest, 3)
        if not known(adx_prior, adx_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = (crossed_above(adx_prior, threshold, adx_latest, threshold)
                     and plus is not None and minus is not None and plus > minus)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
