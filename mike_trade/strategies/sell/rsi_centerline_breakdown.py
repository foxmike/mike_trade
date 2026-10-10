"""RSI Centerline Breakdown SELL.

* Strategy: RSI Centerline Breakdown
* Identifier: ``rsi_centerline_breakdown`` (v1.0.0)
* Category / sub-category: ``technical`` / ``oscillator``
* Role: SELL (advisory full exit of an owned long only)
* Source: J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in
  StockCharts ChartSchool, 'Relative Strength Index (RSI)' (14-period Wilder smoothing;
  70/30 overbought/oversold; 50 centerline).

Calculation (completed bars, oldest first)::

    Wilder RSI = 100 - 100 / (1 + avg gain / avg loss); first averages are simple means
    of the first n changes, then avg = (previous avg * (n - 1) + current) / n.

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    RSI[t-1] >= 50  AND  RSI[t] < 50   ->  SELL   (centerline = 50)

Parameters: `rsi_period` (default 14). Lookback: `rsi_period + 2` completed bars (16 by
default); `required_bars(params)` returns the requirement of the configured periods.
Interval: completed 1-minute bars (the declared `trigger_timeframe`; the one interval served
to both the historical replay and the SHADOW stream).

Interpretation decisions: StockCharts names RSI 'centerline crossovers' as signals without
stating the level in text; 50 is the oscillator's midpoint and is fixed (not a parameter).

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The prior and
latest bars of the signal must be adjacent completed bars (no missing bar between them),
otherwise the result is INSUFFICIENT_DATA. The recursive indicator state is persisted under
`indicator_state` in the strategy's own state and resumed bar by bar, so values do not
depend on the size of the candle history window; absent or unusable state is re-seeded from
the supplied history. Regular session only; stale or non-regular-session contexts never
signal. Exit signal only: the composition layer owns position checks, minimum hold,
arbitration and full-remainder sizing; no order is submitted here. No additional
confirmation condition is applied.
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
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_INDICATOR,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    advance,
    blocked,
    closed_bars,
    crossed_below,
    finish,
    gate,
    insufficient,
    known,
)
from mike_trade.strategies.indicators.core import RSICalculator
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_RSI_PERIOD",
    "WARMUP_BARS",
    "RsiCenterlineBreakdownSell",
]

STRATEGY_ID = "rsi_centerline_breakdown"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_RSI_PERIOD = 14

CENTERLINE = Decimal("50")

def _required_bars(validated: Mapping[str, object]) -> int:
    rsi_period = validated["rsi_period"]
    assert isinstance(rsi_period, int)
    return rsi_period + 2


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 16

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
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
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
    methodology=(
        "SELL when the Wilder RSI crosses below the 50 centerline on a completed bar "
        "(prior RSI >= 50 and latest RSI < 50). Restates a published textbook definition; "
        "no alpha or profitability claim."
    ),
    source=(
        "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
        "documented in StockCharts ChartSchool, 'Relative Strength Index (RSI)' "
        "(14-period Wilder smoothing; 70/30 overbought/oversold; 50 centerline). Not "
        "backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission; minimum hold, arbitration and "
        "position checks belong to the composition layer.",
        "Long exits only (full remainder); regular session only; completed bars only.",
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class RsiCenterlineBreakdownSell(SellRule):
    """SELL when the Wilder RSI crosses below the 50 centerline on a completed bar (prior RSI
    >= 50 and latest RSI < 50)."""

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
        rsi_period = validated["rsi_period"]
        assert isinstance(rsi_period, int)
        need = _required_bars(validated)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        signature = f"{STRATEGY_ID}|{STRATEGY_VERSION}|{rsi_period}"
        adv = advance(
            state, bars, signature,
            fresh=lambda: RSICalculator(rsi_period),
            restore=RSICalculator.restore,
            feed=lambda calc, bar: calc.update(bar.close),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)

        rsi_prior, rsi_latest = adv.prior, adv.latest
        if not known(rsi_prior, rsi_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = crossed_below(rsi_prior, CENTERLINE, rsi_latest, CENTERLINE)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
