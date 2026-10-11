"""COVER on a confirmed bullish price/momentum divergence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    CoverStrategy,
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
    finish,
    gate,
    insufficient,
    require_param,
)
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_bullish_momentum_divergence"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    period, lookback = validated["momentum_period"], validated["lookback"]
    assert isinstance(period, int) and isinstance(lookback, int)
    return period + lookback


class CoverBullishMomentumDivergence(CoverStrategy):
    """COVER when a just-confirmed price swing low is lower than the preceding swing low while
    n-period momentum, sampled at those exact pivot bars, is higher.

    Pivots are fractals with ``strength`` bars each side (strictly below the lows of the
    ``strength`` bars on each side).  Momentum is the close difference
    ``M[i] = close[i] - close[i - momentum_period]``, evaluated at the pivot bar itself (``M`` at
    the pivot timestamp), never at the later bar that confirms it.  With the structure window the
    last ``lookback`` completed bars and pivot lows ``L1`` (earlier) and ``L2`` (latest)::

        L2 confirmed on the latest bar (index t - strength)
        AND  low[L2] < low[L1]  AND  M[L2] > M[L1]                       ->  COVER

    The signal waits ``strength`` completed right-hand bars after the pivot, so the divergence is
    observable at ``t`` using only bars up to ``t``; it fires once, on the confirming bar.  The
    whole ``momentum_period + lookback`` window must be consecutive bars.  Mirror of the
    bearish_momentum_divergence SELL on pivot lows.  Finite window, no persisted state.  Needs
    ``momentum_period + lookback`` completed bars (60 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="oscillator",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=60,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "momentum_period", ParameterKind.INT, 10, 2, 100,
                    description="Close-difference momentum lookback. Default 10.",
                ),
                ParameterSpec(
                    "strength", ParameterKind.INT, 2, 1, 10,
                    description="Completed bars on each side of a confirmed pivot. Default 2.",
                ),
                ParameterSpec(
                    "lookback", ParameterKind.INT, 50, 10, 300,
                    description="Bars searched for the two price pivots. Default 50.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when the just-confirmed price swing low is below the preceding swing low "
            "while n-period momentum (close minus close n bars ago), sampled at those exact "
            "price-pivot timestamps, is higher."
        ),
        source=(
            "John J. Murphy, Technical Analysis of the Financial Markets (1999), momentum "
            "oscillator and divergence; pivots follow Bill Williams' confirmed fractal "
            "construction: https://www.tradingview.com/support/solutions/43000591663-williams-fractal-indicator/. "
            "Pivot-low mirror of the bearish_momentum_divergence SELL."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "The signal waits `strength` completed right-hand bars, so no future observation is "
            "read at the historical signal timestamp.",
            "Momentum is compared only at the two price pivots, not at unrelated oscillator "
            "minima; the entire calculation window must be adjacent. Finite close-difference "
            "momentum, not an RSI or MACD divergence.",
        ),
        reason_codes=REASON_CODES,
    )

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
        period, strength, lookback = (
            validated["momentum_period"], validated["strength"], validated["lookback"],
        )
        assert isinstance(period, int) and isinstance(strength, int) and isinstance(lookback, int)
        require_param(lookback >= 4 * strength + 2, "lookback >= 4 * strength + 2", "lookback")
        need = _required_bars(validated)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        bars = bars[-need:]
        if any(not adjacent(a, b) for a, b in zip(bars, bars[1:], strict=False)):
            return insufficient(self.spec, context)
        structure = bars[-lookback:]
        _, lows = swing_pivots(structure, strength)
        triggered = False
        if len(lows) >= 2 and lows[-1][0] == len(structure) - 1 - strength:
            offset = len(bars) - lookback
            first_index, second_index = offset + lows[-2][0], offset + lows[-1][0]
            first_momentum = bars[first_index].close - bars[first_index - period].close
            second_momentum = bars[second_index].close - bars[second_index - period].close
            triggered = lows[-1][1] < lows[-2][1] and second_momentum > first_momentum
        return finish(self.spec, context, bars[-1], state, triggered)
