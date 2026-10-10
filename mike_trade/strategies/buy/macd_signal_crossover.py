"""MACD Signal Crossover BUY.

* Strategy: MACD Signal Crossover
* Identifier: ``macd_signal_crossover`` (v1.0.0)
* Category / sub-category: ``technical`` / ``momentum``
* Role: BUY (long entry signal only)
* Source: Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving Average
  Convergence/Divergence Oscillator)' (MACD = 12-EMA - 26-EMA, signal = 9-EMA of MACD;
  signal-line and centerline crossovers).

Calculation (completed bars, oldest first)::

    MACD[t]   = EMA(fast)[t] - EMA(slow)[t]            (closes; EMAs SMA-seeded)
    Signal[t] = EMA(signal) of MACD, SMA-seeded over its first `signal` values

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    MACD[t-1] <= Signal[t-1]  AND  MACD[t] > Signal[t]   ->  BUY   (bullish crossover)

Parameters: `fast_period` (default 12), `slow_period` (default 26), `signal_period` (default
9); fast_period < slow_period. Lookback: `slow_period + signal_period` completed bars (35 by
default); `required_bars(params)` returns the requirement of the configured periods.
Interval: completed 1-minute bars (the declared `trigger_timeframe`; the one interval served
to both the historical replay and the SHADOW stream).

Interpretation decisions: StockCharts does not state the EMA seeding; the SMA seed shared
with the EMA indicator is used for all three EMAs.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The prior and
latest bars of the signal must be adjacent completed bars (no missing bar between them),
otherwise the result is INSUFFICIENT_DATA. The recursive indicator state is persisted under
`indicator_state` in the strategy's own state and resumed bar by bar, so values do not
depend on the size of the candle history window; absent or unusable state is re-seeded from
the supplied history. Regular session only; stale or non-regular-session contexts never
signal. Entry signal only: no stops, sizing or orders (protective stops belong to the
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
    require_param,
)
from mike_trade.strategies.indicators.core import MACDCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_FAST_PERIOD",
    "DEFAULT_SLOW_PERIOD",
    "DEFAULT_SIGNAL_PERIOD",
    "WARMUP_BARS",
    "MacdSignalCrossoverBuy",
]

STRATEGY_ID = "macd_signal_crossover"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_FAST_PERIOD = 12
DEFAULT_SLOW_PERIOD = 26
DEFAULT_SIGNAL_PERIOD = 9


def _required_bars(validated: Mapping[str, object]) -> int:
    slow_period = validated["slow_period"]
    assert isinstance(slow_period, int)
    signal_period = validated["signal_period"]
    assert isinstance(signal_period, int)
    return slow_period + signal_period


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 35

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="momentum",
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
                "fast_period", ParameterKind.INT, default=DEFAULT_FAST_PERIOD,
                min_value=2, max_value=100,
                description=(
                    "Fast EMA length of closes. Default "
                    f"{DEFAULT_FAST_PERIOD}."
                ),
            ),
            ParameterSpec(
                "slow_period", ParameterKind.INT, default=DEFAULT_SLOW_PERIOD,
                min_value=3, max_value=200,
                description=(
                    "Slow EMA length of closes; must exceed fast_period. Default "
                    f"{DEFAULT_SLOW_PERIOD}."
                ),
            ),
            ParameterSpec(
                "signal_period", ParameterKind.INT, default=DEFAULT_SIGNAL_PERIOD,
                min_value=2, max_value=100,
                description=(
                    "EMA length of the MACD line (signal line). Default "
                    f"{DEFAULT_SIGNAL_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
    methodology=(
        "BUY when the MACD line crosses above its signal line (bullish signal-line "
        "crossover) on a completed bar. Restates a published textbook definition; no "
        "alpha or profitability claim."
    ),
    source=(
        "Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving "
        "Average Convergence/Divergence Oscillator)' (MACD = 12-EMA - 26-EMA, signal = "
        "9-EMA of MACD; signal-line and centerline crossovers). Not backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; regular session only; completed bars only.",
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class MacdSignalCrossoverBuy(BuyStrategy):
    """BUY when the MACD line crosses above its signal line (bullish signal-line crossover) on
    a completed bar."""

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
        fast_period = validated["fast_period"]
        slow_period = validated["slow_period"]
        signal_period = validated["signal_period"]
        assert isinstance(fast_period, int)
        assert isinstance(slow_period, int)
        assert isinstance(signal_period, int)
        require_param(fast_period < slow_period, "fast_period < slow_period", "fast_period")
        need = _required_bars(validated)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        signature = f"{STRATEGY_ID}|{STRATEGY_VERSION}|{fast_period}|{slow_period}|{signal_period}"
        adv = advance(
            state, bars, signature,
            fresh=lambda: MACDCalculator(fast_period, slow_period, signal_period),
            restore=MACDCalculator.restore,
            feed=lambda calc, bar: calc.update(bar.close),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)

        _, _, macd_prior, signal_prior = parts(adv.prior, 4)
        _, _, macd_latest, signal_latest = parts(adv.latest, 4)
        if not known(macd_prior, signal_prior, macd_latest, signal_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = crossed_above(macd_prior, signal_prior, macd_latest, signal_latest)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
