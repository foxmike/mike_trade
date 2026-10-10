"""Williams %R Reversion BUY.

* Strategy: Williams %R Reversion
* Identifier: ``williams_r_reversion`` (v1.0.0)
* Category / sub-category: ``technical`` / ``oscillator``
* Role: BUY (long entry signal only)
* Source: Larry Williams' %R as documented in StockCharts ChartSchool, 'Williams %R' (formula, 14
  default, -20/-80 levels); the -80/-20 re-cross trigger follows the cTrader and
  TrendSpider indicator documentation (sources differ on the trigger).

Calculation (completed 1-minute bars, oldest first)::

    %R = -100 * (highest high(n) - close) / (highest high(n) - lowest low(n))

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    %R[t-1] <= oversold  AND  %R[t] > oversold   ->  BUY   (default oversold = -80)

Parameters: `period` (default 14), `oversold` (default -80). Lookback: `period + 1`
completed bars (15 by default). Interval: completed 1-minute bars (trigger and only required
timeframe; the one interval served to both the historical replay and the SHADOW stream).

Interpretation decisions: StockCharts also describes a cross above -50 after an oversold
reading, which needs an unspecified look-back; the single-step -80 re-cross documented by
cTrader/TrendSpider is used. A flat range leaves %R undefined and suppresses the signal.

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
from mike_trade.strategies.indicators.core import williams_r

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_PERIOD",
    "DEFAULT_OVERSOLD",
    "WARMUP_BARS",
    "WilliamsRReversionBuy",
]

STRATEGY_ID = "williams_r_reversion"
STRATEGY_VERSION = "1.0.0"

DEFAULT_PERIOD = 14
DEFAULT_OVERSOLD = Decimal("-80")
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (15 completed bars at the defaults) is enforced dynamically in the rule.
WARMUP_BARS = 3

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
                min_value=2, max_value=100,
                description=(
                    "Williams %R look-back in completed bars. Default "
                    f"{DEFAULT_PERIOD}."
                ),
            ),
            ParameterSpec(
                "oversold", ParameterKind.DECIMAL, default=DEFAULT_OVERSOLD,
                min_value=Decimal("-100"), max_value=Decimal("-50"),
                description=(
                    "Oversold level; %R must rise back above it. Default "
                    f"{DEFAULT_OVERSOLD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when Williams %R rises back above the oversold level (-80) on a completed "
        "bar after being at or below it on the prior bar. Restates a published textbook "
        "definition; no alpha or profitability claim."
    ),
    source=(
        "Larry Williams' %R as documented in StockCharts ChartSchool, 'Williams %R' "
        "(formula, 14 default, -20/-80 levels); the -80/-20 re-cross trigger follows the "
        "cTrader and TrendSpider indicator documentation (sources differ on the trigger). "
        "Not backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; regular session only; completed 1-minute bars only.",
        "The prior and latest bars of the crossing must share a New York trading date.",
    ),
    reason_codes=REASON_CODES,
)


class WilliamsRReversionBuy(BuyStrategy):
    """BUY when Williams %R rises back above the oversold level (-80) on a completed bar after
    being at or below it on the prior bar."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period = validated["period"]
        oversold = validated["oversold"]
        assert isinstance(period, int)
        assert isinstance(oversold, Decimal)

        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, period + 1)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        window = bars[-(period + 1):]
        values = williams_r(window, period)
        if not known(values[-2], values[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(values[-2], oversold, values[-1], oversold)
        return finish(self.spec, context, bars[-1], state, triggered)
