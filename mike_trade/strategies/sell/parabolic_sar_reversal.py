"""Parabolic SAR Reversal SELL.

* Strategy: Parabolic SAR Reversal
* Identifier: ``parabolic_sar_reversal`` (v1.0.0)
* Category / sub-category: ``technical`` / ``trend_following``
* Role: SELL (advisory full exit of an owned long only)
* Source: J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in
  StockCharts ChartSchool, 'Parabolic SAR' (AF 0.02 step, 0.20 maximum; prior-two-bar
  clamp; reversal when price penetrates SAR).

Calculation (completed 1-minute bars, oldest first)::

    SAR[t] = SAR[t-1] + AF * (EP - SAR[t-1]);  AF starts at `step`, +step on each new EP,
    capped at `maximum`.  Uptrend: SAR never above the prior two lows (downtrend: never
    below the prior two highs).  Reversal when low < SAR (uptrend) or high > SAR
    (downtrend): new SAR = prior EP, new EP = the penetrating bar's extreme, AF = step.

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    trend[t-1] = up  AND  trend[t] = down   ->  SELL   (SAR flips above price)

Parameters: `step` (default 0.02), `maximum` (default 0.20); maximum >= step. Lookback: 3
completed bars minimum; the whole supplied history defines the SAR path. Interval: completed
1-minute bars (trigger and only required timeframe; the one interval served to both the
historical replay and the SHADOW stream).

Interpretation decisions: StockCharts and Wilder's text leave the starting trend open; the
first trend is up if the second close >= the first (SAR = first low, EP = first high) else
down. The SAR path depends on the supplied history, so it is evaluated over the whole
history and only the latest flip is used. Penetration is strict (low < SAR / high > SAR).

Indicators run over the completed-bar history supplied by the context, which may span
sessions like a continuous chart. The crossing itself must occur between two bars of the
same New York trading date, so an overnight gap never signals. Regular session only; stale
or non-regular-session contexts never signal. Exit signal only: the composition layer owns
position checks, minimum hold, arbitration and full-remainder sizing; no order is submitted
here. No additional confirmation condition is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
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
    TIMEFRAME,
    closed_bars,
    finish,
    gate,
    insufficient,
    require_param,
    same_session,
)
from mike_trade.strategies.indicators.core import parabolic_sar
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_STEP",
    "DEFAULT_MAXIMUM",
    "WARMUP_BARS",
    "ParabolicSarReversalSell",
]

STRATEGY_ID = "parabolic_sar_reversal"
STRATEGY_VERSION = "1.0.0"

DEFAULT_STEP = Decimal("0.02")
DEFAULT_MAXIMUM = Decimal("0.20")
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (3 completed bars at the defaults) is enforced dynamically in the rule.
WARMUP_BARS = 3

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="trend_following",
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
                "step", ParameterKind.DECIMAL, default=DEFAULT_STEP,
                min_value=Decimal("0.001"), max_value=Decimal("0.5"),
                description=(
                    "Acceleration factor start and increment. Default "
                    f"{DEFAULT_STEP}."
                ),
            ),
            ParameterSpec(
                "maximum", ParameterKind.DECIMAL, default=DEFAULT_MAXIMUM,
                min_value=Decimal("0.001"), max_value=Decimal("1"),
                description=(
                    "Acceleration factor cap; must be at least step. Default "
                    f"{DEFAULT_MAXIMUM}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SELL on the completed bar where the Parabolic SAR trend flips from uptrend to "
        "downtrend (price penetrates the SAR from above). Restates a published textbook "
        "definition; no alpha or profitability claim."
    ),
    source=(
        "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
        "documented in StockCharts ChartSchool, 'Parabolic SAR' (AF 0.02 step, 0.20 "
        "maximum; prior-two-bar clamp; reversal when price penetrates SAR). Not "
        "backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission; minimum hold, arbitration and "
        "position checks belong to the composition layer.",
        "Long exits only (full remainder); regular session only; completed 1-minute bars only.",
        "The prior and latest bars of the crossing must share a New York trading date.",
    ),
    reason_codes=REASON_CODES,
)


class ParabolicSarReversalSell(SellRule):
    """SELL on the completed bar where the Parabolic SAR trend flips from uptrend to downtrend
    (price penetrates the SAR from above)."""

    spec = _SPEC

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        step = validated["step"]
        maximum = validated["maximum"]
        assert isinstance(step, Decimal)
        assert isinstance(maximum, Decimal)
        require_param(maximum >= step, "maximum >= step", "maximum")

        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, 3)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        path = parabolic_sar(bars, step, maximum)
        prior, latest = path[-2], path[-1]
        if prior is None or latest is None:
            return insufficient(self.spec, context)
        triggered = prior[1] and not latest[1]
        return finish(self.spec, context, bars[-1], state, triggered)
