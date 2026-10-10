"""CCI Zero-Line Crossover BUY.

* Strategy: CCI Zero-Line Crossover
* Identifier: ``cci_zero_line_crossover`` (v1.0.0)
* Category / sub-category: ``technical`` / ``oscillator``
* Role: BUY (long entry signal only)
* Source: Donald Lambert's Commodity Channel Index as documented in StockCharts ChartSchool,
  'Commodity Channel Index (CCI)' (20 periods, 0.015 constant; CCI positive favors
  bulls, negative favors bears).

Calculation (completed 1-minute bars, oldest first)::

    TP = (high + low + close) / 3
    CCI = (TP - SMA(TP, n)) / (0.015 * mean absolute deviation of TP from SMA(TP, n))

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    CCI[t-1] <= 0  AND  CCI[t] > 0   ->  BUY

Parameters: `period` (default 20). Lookback: `period + 1` completed bars (21 by default).
Interval: completed 1-minute bars (trigger and only required timeframe; the one interval
served to both the historical replay and the SHADOW stream).

Interpretation decisions: StockCharts notes zero-line crossovers produce many whipsaws; no
+100/-100 requirement is added. A zero mean deviation (flat typical price) leaves CCI
undefined and suppresses the signal.

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
from mike_trade.strategies.indicators.core import cci

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "CciZeroLineCrossoverBuy",
]

STRATEGY_ID = "cci_zero_line_crossover"
STRATEGY_VERSION = "1.0.0"

DEFAULT_PERIOD = 20
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (21 completed bars at the defaults) is enforced dynamically in the rule.
WARMUP_BARS = 3

ZERO = Decimal("0")

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
                "period", ParameterKind.INT, default=DEFAULT_PERIOD,
                min_value=2, max_value=200,
                description=(
                    "CCI look-back in completed bars. Default "
                    f"{DEFAULT_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when the Commodity Channel Index crosses above zero on a completed bar. "
        "Restates a published textbook definition; no alpha or profitability claim."
    ),
    source=(
        "Donald Lambert's Commodity Channel Index as documented in StockCharts "
        "ChartSchool, 'Commodity Channel Index (CCI)' (20 periods, 0.015 constant; CCI "
        "positive favors bulls, negative favors bears). Not backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; regular session only; completed 1-minute bars only.",
        "The prior and latest bars of the crossing must share a New York trading date.",
    ),
    reason_codes=REASON_CODES,
)


class CciZeroLineCrossoverBuy(BuyStrategy):
    """BUY when the Commodity Channel Index crosses above zero on a completed bar."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period = validated["period"]
        assert isinstance(period, int)

        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, period + 1)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        window = bars[-(period + 1):]
        values = cci(window, period)
        if not known(values[-2], values[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(values[-2], ZERO, values[-1], ZERO)
        return finish(self.spec, context, bars[-1], state, triggered)
