"""Donchian Channel Breakout BUY.

* Strategy: Donchian Channel Breakout
* Identifier: ``donchian_channel_breakout`` (v1.0.0)
* Category / sub-category: ``technical`` / ``breakout``
* Role: BUY (long entry signal only)
* Source: Richard Donchian's price channel as documented in StockCharts ChartSchool, 'Price
  Channels' (also called Donchian Channels; 20 default; the channel uses prices prior to
  the current period).

Calculation (completed bars, oldest first)::

    Upper[t] = highest high of bars t-n .. t-1   (the current bar is excluded)

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    close[t-1] <= Upper[t-1]  AND  close[t] > Upper[t]   ->  BUY

Parameters: `period` (default 20). Lookback: `period + 2` completed bars (22 by default);
`required_bars(params)` returns the requirement of the configured periods. Interval:
completed 1-minute bars (the declared `trigger_timeframe`; the one interval served to both
the historical replay and the SHADOW stream).

Interpretation decisions: The signal uses the completed bar's close breaking the channel
built from the prior n bars (StockCharts: channels use prices prior to the current period);
an intraday-touch variant is not used. Unlike momentum_breakout there is no volume
confirmation.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The prior and
latest bars of the signal must be adjacent completed bars (no missing bar between them),
otherwise the result is INSUFFICIENT_DATA. Windowed values are computed from the latest
completed bars in the context. Regular session only; stale or non-regular-session contexts
never signal. Entry signal only: no stops, sizing or orders (protective stops belong to the
existing stop policy). No additional confirmation condition is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    crossed_above,
    finish,
    gate,
    insufficient,
    known,
)
from mike_trade.strategies.indicators.core import donchian

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "DonchianChannelBreakoutBuy",
]

STRATEGY_ID = "donchian_channel_breakout"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_PERIOD = 20


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period + 2


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 22

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="breakout",
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
                min_value=2, max_value=400,
                description=(
                    "Channel look-back in completed bars. Default "
                    f"{DEFAULT_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when the completed close breaks above the highest high of the preceding "
        "`period` bars after the prior close was at or below its own channel. Restates a "
        "published textbook definition; no alpha or profitability claim."
    ),
    source=(
        "Richard Donchian's price channel as documented in StockCharts ChartSchool, "
        "'Price Channels' (also called Donchian Channels; 20 default; the channel uses "
        "prices prior to the current period). Not backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; regular session only; completed bars only.",
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class DonchianChannelBreakoutBuy(BuyStrategy):
    """BUY when the completed close breaks above the highest high of the preceding `period`
    bars after the prior close was at or below its own channel."""

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

        window = bars[-(period + 2):]
        upper, _ = donchian(window, period)
        if not known(upper[-2], upper[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(window[-2].close, upper[-2], window[-1].close, upper[-1])
        return finish(self.spec, context, bars[-1], state, triggered)
