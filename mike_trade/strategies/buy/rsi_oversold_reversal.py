"""RSI Oversold Reversal BUY.

* Strategy: RSI Oversold Reversal
* Identifier: ``rsi_oversold_reversal`` (v1.0.0)
* Category / sub-category: ``technical`` / ``oscillator``
* Role: BUY (long entry signal only)
* Source: J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in
  StockCharts ChartSchool, 'Relative Strength Index (RSI)' (14-period Wilder smoothing;
  70/30 overbought/oversold; 50 centerline).

Calculation (completed 1-minute bars, oldest first)::

    Wilder RSI as in the RSI Centerline Crossover strategy.

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    RSI[t-1] <= oversold  AND  RSI[t] > oversold   ->  BUY   (default oversold = 30)

Parameters: `rsi_period` (default 14), `oversold` (default 30). Lookback: `rsi_period + 2`
completed bars (16 by default). Interval: completed 1-minute bars (trigger and only required
timeframe; the one interval served to both the historical replay and the SHADOW stream).

Interpretation decisions: Wilder defines readings below 30 as oversold; the simple re-cross
above the level is used as the reversal trigger (mirror of the existing rsi_reversal SELL).
Wilder's failure-swing pattern is a different, multi-step rule and is not implemented.

Indicators run over the completed-bar history supplied by the context, which may span
sessions like a continuous chart. The crossing itself must occur between two bars of the
same New York trading date, so an overnight gap never signals. Regular session only; stale
or non-regular-session contexts never signal. Entry signal only: no stops, sizing or orders
(protective stops belong to the existing stop policy). No additional confirmation condition
is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
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
    TIMEFRAME,
    closed_bars,
    crossed_above,
    finish,
    gate,
    insufficient,
    known,
    same_session,
)
from mike_trade.strategies.indicators.core import rsi

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_RSI_PERIOD",
    "DEFAULT_OVERSOLD",
    "WARMUP_BARS",
    "RsiOversoldReversalBuy",
]

STRATEGY_ID = "rsi_oversold_reversal"
STRATEGY_VERSION = "1.0.0"

DEFAULT_RSI_PERIOD = 14
DEFAULT_OVERSOLD = Decimal("30")
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (16 completed bars at the defaults) is enforced dynamically in the rule.
WARMUP_BARS = 4

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="oscillator",
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
                "rsi_period", ParameterKind.INT, default=DEFAULT_RSI_PERIOD,
                min_value=2, max_value=100,
                description=(
                    "Wilder RSI length in completed bars. Default "
                    f"{DEFAULT_RSI_PERIOD}."
                ),
            ),
            ParameterSpec(
                "oversold", ParameterKind.DECIMAL, default=DEFAULT_OVERSOLD,
                min_value=Decimal("1"), max_value=Decimal("50"),
                description=(
                    "Oversold level; RSI must rise back above it. Default "
                    f"{DEFAULT_OVERSOLD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when the Wilder RSI, at or below the oversold level on the prior bar, closes "
        "back above it on the latest completed bar. Restates a published textbook "
        "definition; no alpha or profitability claim."
    ),
    source=(
        "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
        "documented in StockCharts ChartSchool, 'Relative Strength Index (RSI)' "
        "(14-period Wilder smoothing; 70/30 overbought/oversold; 50 centerline). Not "
        "backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; regular session only; completed 1-minute bars only.",
        "The prior and latest bars of the crossing must share a New York trading date.",
    ),
    reason_codes=REASON_CODES,
)


class RsiOversoldReversalBuy(BuyStrategy):
    """BUY when the Wilder RSI, at or below the oversold level on the prior bar, closes back
    above it on the latest completed bar."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        rsi_period = validated["rsi_period"]
        oversold = validated["oversold"]
        assert isinstance(rsi_period, int)
        assert isinstance(oversold, Decimal)

        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, rsi_period + 2)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        values = rsi([bar.close for bar in bars], rsi_period)
        if not known(values[-2], values[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(values[-2], oversold, values[-1], oversold)
        return finish(self.spec, context, bars[-1], state, triggered)
