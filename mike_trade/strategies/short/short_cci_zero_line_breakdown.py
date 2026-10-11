"""CCI Zero-Line Breakdown SHORT.

* Strategy: CCI Zero-Line Breakdown
* Identifier: ``short_cci_zero_line_breakdown`` (v1.0.0)
* Category / sub-category: ``technical`` / ``oscillator``
* Role: SHORT (bearish entry signal only)
* Source: Donald Lambert's Commodity Channel Index as documented in StockCharts ChartSchool,
  'Commodity Channel Index (CCI)' (20 periods, 0.015 constant; CCI positive favors
  bulls, negative favors bears).

Calculation (completed bars, oldest first)::

    TP = (high + low + close) / 3
    CCI = (TP - SMA(TP, n)) / (0.015 * mean absolute deviation of TP from SMA(TP, n))

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    CCI[t-1] >= 0  AND  CCI[t] < 0   ->  SHORT

Parameters: `period` (default 20). Lookback: `period + 1` completed bars (21 by default);
`required_bars(params)` returns the requirement of the configured periods. Interval:
completed 1-minute bars (the declared `trigger_timeframe`; the one interval served to both
the historical replay and the SHADOW stream).

Interpretation decisions: StockCharts notes zero-line crossovers produce many whipsaws; no
+100/-100 requirement is added. A zero mean deviation leaves CCI undefined and suppresses
the signal.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The prior and
latest bars of the signal must be adjacent completed bars (no missing bar between them),
otherwise the result is INSUFFICIENT_DATA. Windowed values are computed from the latest
completed bars in the context. Regular session only; stale or non-regular-session contexts
never signal. SHORT eligibility, arbitration, sizing, borrow and margin checks, protection,
and order submission belong to the engine. No additional confirmation is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ShortStrategy,
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
    crossed_below,
    finish,
    gate,
    insufficient,
    known,
)
from mike_trade.strategies.indicators.core import cci

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "ShortCciZeroLineBreakdown",
]

STRATEGY_ID = "short_cci_zero_line_breakdown"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_PERIOD = 20

ZERO = Decimal("0")


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period + 1


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 21

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SHORT,
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
                "period",
                ParameterKind.INT,
                default=DEFAULT_PERIOD,
                min_value=2,
                max_value=200,
                description=(f"CCI look-back in completed bars. Default {DEFAULT_PERIOD}."),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SHORT when the Commodity Channel Index crosses below zero on a completed bar. "
        "Restates a published textbook definition; no alpha or profitability claim."
    ),
    source=(
        "Donald Lambert's Commodity Channel Index as documented in StockCharts "
        "ChartSchool, 'Commodity Channel Index (CCI)' (20 periods, 0.015 constant; CCI "
        "positive favors bulls, negative favors bears). Not backtested here."
    ),
    execution_limitations=(
        "SHORT entry signal only; the engine owns eligibility, arbitration, sizing, borrow, "
        "margin checks, protection, and order submission.",
        "Regular session and completed bars only; never closes a long position.",
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class ShortCciZeroLineBreakdown(ShortStrategy):
    """SHORT when the Commodity Channel Index crosses below zero on a completed bar."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params))

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period = validated["period"]
        assert isinstance(period, int)
        need = _required_bars(validated)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        window = bars[-(period + 1) :]
        values = cci(window, period)  # type: ignore[arg-type]
        if not known(values[-2], values[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_below(values[-2], ZERO, values[-1], ZERO)
        return finish(self.spec, context, bars[-1], state, triggered)
