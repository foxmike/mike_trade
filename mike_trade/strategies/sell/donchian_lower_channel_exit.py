"""Donchian Lower Channel Exit SELL.

* Strategy: Donchian Lower Channel Exit
* Identifier: ``donchian_lower_channel_exit`` (v1.0.0)
* Category / sub-category: ``technical`` / ``breakout``
* Role: SELL (advisory full exit of an owned long only)
* Source: Richard Donchian's price channel as documented in StockCharts ChartSchool, 'Price
  Channels' (also called Donchian Channels; 20 default; the channel uses prices prior to
  the current period).

Calculation (completed bars, oldest first)::

    Lower[t] = lowest low of bars t-n .. t-1   (the current bar is excluded)

Signal (level, evaluated on the latest completed bar `t`)::

    close[t] < Lower[t]   ->  SELL   (level condition, not an edge)

Parameters: `period` (default 20). Lookback: `period + 1` completed bars (21 by default);
`required_bars(params)` returns the requirement of the configured periods. Interval:
completed 1-minute bars (the declared `trigger_timeframe`; the one interval served to both
the historical replay and the SHADOW stream).

Interpretation decisions: An exit rule evaluated as a level: a completed close below the
prior-n-bar low exits an owned long. The channel length defaults to the published 20;
shorter exit channels (for example the Turtle System 1 exit of 10 bars) are available
through `period`. The close, not an intraday touch, is compared.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. Windowed
values are computed from the latest completed bars in the context. Regular session only;
stale or non-regular-session contexts never signal. Exit signal only: the composition layer
owns position checks, minimum hold, arbitration and full-remainder sizing; no order is
submitted here. No additional confirmation condition is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
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
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.core import donchian
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "DonchianLowerChannelExitSell",
]

STRATEGY_ID = "donchian_lower_channel_exit"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_PERIOD = 20


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
    side=StrategyRole.SELL,
    category="technical",
    sub_category="breakout",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.LEVEL_ON_CLOSED_BAR,
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
        "SELL while the latest completed close is below the lowest low of the preceding "
        "`period` bars (lower channel exit). Restates a published textbook definition; no "
        "alpha or profitability claim."
    ),
    source=(
        "Richard Donchian's price channel as documented in StockCharts ChartSchool, "
        "'Price Channels' (also called Donchian Channels; 20 default; the channel uses "
        "prices prior to the current period). Not backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission; minimum hold, arbitration and "
        "position checks belong to the composition layer.",
        "Long exits only (full remainder); regular session only; completed bars only.",
    ),
    reason_codes=REASON_CODES,
)


class DonchianLowerChannelExitSell(SellRule):
    """SELL while the latest completed close is below the lowest low of the preceding `period`
    bars (lower channel exit)."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params))

    def evaluate_trigger(
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

        window = bars[-(period + 1):]
        _, lower = donchian(window, period)
        floor = lower[-1]
        if floor is None:
            return insufficient(self.spec, context)
        triggered = window[-1].close < floor
        return finish(self.spec, context, bars[-1], state, triggered)
