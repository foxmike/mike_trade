"""DMI Bearish Crossover SHORT.

* Strategy: DMI Bearish Crossover
* Identifier: ``short_dmi_bearish_crossover`` (v1.0.0)
* Category / sub-category: ``technical`` / ``trend_following``
* Role: SHORT (bearish entry signal only)
* Source: J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in
  StockCharts ChartSchool, 'Average Directional Index (ADX)' (14-period Wilder
  smoothing; +DI/-DI crossover buy and sell signals).

Calculation (completed bars, oldest first)::

    TR, +DM, -DM from the second bar; first smoothed value = sum of the first n raw values,
    then smoothed = previous - previous / n + current.
    +DI = 100 * smoothed +DM / smoothed TR      -DI = 100 * smoothed -DM / smoothed TR

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    -DI[t-1] <= +DI[t-1]  AND  -DI[t] > +DI[t]   ->  SHORT   (-DI crosses above +DI)

Parameters: `period` (default 14). Lookback: `period + 2` completed bars (16 by default);
the whole supplied history smooths the values; `required_bars(params)` returns the
requirement of the configured periods. Interval: completed 1-minute bars (the declared
`trigger_timeframe`; the one interval served to both the historical replay and the SHADOW
stream).

Interpretation decisions: Wilder/StockCharts also require ADX above 25 (many traders use 20)
and place the initial stop at the signal bar's high. Neither is added here: the strategy is
the plain -DI/+DI crossover and stops belong to the stop policy.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The prior and
latest bars of the signal must be adjacent completed bars (no missing bar between them),
otherwise the result is INSUFFICIENT_DATA. The recursive indicator state is persisted under
`indicator_state` in the strategy's own state and resumed bar by bar, so values do not
depend on the size of the candle history window; absent or unusable state is re-seeded from
the supplied history. Regular session only; stale or non-regular-session contexts never
signal. SHORT eligibility, arbitration, sizing, borrow and margin checks, protection, and
order submission belong to the engine. No additional confirmation is applied.
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
    ShortStrategy,
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
    crossed_above,
    finish,
    gate,
    insufficient,
    known,
    parts,
)
from mike_trade.strategies.indicators.core import DMICalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "ShortDmiBearishCrossover",
]

STRATEGY_ID = "short_dmi_bearish_crossover"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_PERIOD = 14


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period + 2


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 16

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SHORT,
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
                "period",
                ParameterKind.INT,
                default=DEFAULT_PERIOD,
                min_value=2,
                max_value=100,
                description=(
                    f"Wilder smoothing length in completed bars. Default {DEFAULT_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
    methodology=(
        "SHORT when -DI crosses above +DI on a completed bar (prior -DI <= +DI and latest "
        "-DI > +DI). Restates a published textbook definition; no alpha or profitability "
        "claim."
    ),
    source=(
        "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
        "documented in StockCharts ChartSchool, 'Average Directional Index (ADX)' "
        "(14-period Wilder smoothing; +DI/-DI crossover buy and sell signals). Not "
        "backtested here."
    ),
    execution_limitations=(
        "SHORT entry signal only; the engine owns eligibility, arbitration, sizing, borrow, "
        "margin checks, protection, and order submission.",
        "Regular session and completed bars only; never closes a long position.",
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class ShortDmiBearishCrossover(ShortStrategy):
    """SHORT when -DI crosses above +DI on a completed bar (prior -DI <= +DI and latest -DI >
    +DI)."""

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
        signature = f"{STRATEGY_ID}|{STRATEGY_VERSION}|{period}"
        adv = advance(
            state,
            bars,
            signature,
            fresh=lambda: DMICalculator(period),
            restore=DMICalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)

        plus_prior, minus_prior = parts(adv.prior, 2)
        plus_latest, minus_latest = parts(adv.latest, 2)
        if not known(plus_prior, minus_prior, plus_latest, minus_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = crossed_above(minus_prior, plus_prior, minus_latest, plus_latest)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
