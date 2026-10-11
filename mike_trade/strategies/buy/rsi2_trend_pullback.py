"""Long entry when a very short RSI turns oversold inside a long-term uptrend."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, crossed_below, finish, gate, insufficient, known
from mike_trade.strategies.indicators.core import RSICalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "rsi2_trend_pullback"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required(rsi_period: int, trend_period: int) -> int:
    return max(rsi_period + 2, trend_period)


class Rsi2TrendPullbackBuy(BuyStrategy):
    """BUY when RSI(2) falls below `threshold` while the close is above its long SMA.

    Connors' pullback-in-an-uptrend: the trend filter is ``close > SMA(close, trend_period)``
    (200 by default) and the trigger is the RSI(`rsi_period`) crossing from at-or-above to below
    `threshold` (10 by default; Connors tested 5 and 10) on the latest completed bar.  Wilder's RSI is
    resumed from persisted state; the SMA is a finite window.  Needs
    ``max(rsi_period + 2, trend_period)`` completed bars (200 by default), so SHADOW warmup must be
    configured accordingly.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="mean_reversion", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=200,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("rsi_period", ParameterKind.INT, 2, 2, 14,
                          description="Wilder RSI length. Default 2."),
            ParameterSpec("threshold", ParameterKind.DECIMAL, Decimal(10), Decimal(1), Decimal(40),
                          description="Entry when RSI crosses below this level. Default 10."),
            ParameterSpec("trend_period", ParameterKind.INT, 200, 20, 500,
                          description="SMA length of the uptrend filter. Default 200."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when RSI(2) crosses below the threshold on a completed bar whose close is "
                     "above the long simple moving average."),
        source=("Larry Connors and Cesar Alvarez, Short Term Trading Strategies That Work (2008): "
                "buy when the 2-period RSI is below 10 (or 5) while the close is above the 200-day "
                "moving average."),
        execution_limitations=("Connors' daily-bar rule applied to M1 bars; his exits (close above "
                               "the 5-bar SMA, RSI above 65) belong to SELL strategies.",
                               "Edge-triggered on the first crossing, not on every oversold bar.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        v = cls.spec.parameter_schema.validate(params)
        return _required(v["rsi_period"], v["trend_period"])  # type: ignore[arg-type]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        rsi_period, threshold, trend = v["rsi_period"], v["threshold"], v["trend_period"]
        assert isinstance(rsi_period, int) and isinstance(trend, int) and isinstance(threshold, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required(rsi_period, trend))
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{rsi_period}",
                      fresh=lambda: RSICalculator(rsi_period), restore=RSICalculator.restore,
                      feed=lambda calc, bar: calc.update(bar.close))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]) or not known(adv.prior, adv.latest):
            return insufficient(self.spec, context, adv.updates)
        average = sum((bar.close for bar in bars[-trend:]), Decimal(0)) / trend
        triggered = (crossed_below(adv.prior, threshold, adv.latest, threshold)
                     and bars[-1].close > average)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
