"""Moving average compression BUY strategy (bullish expansion out of converging moving averages).

A BUY signal fires edge-triggered on completed regular-session 1-minute bars when:
1. Moving Average Compression: For at least ``compression_bars`` consecutive completed bars
   strictly preceding the breakout bar (bars ``t - compression_bars`` to ``t - 1``), the spread
   across three moving averages (fast, medium, and slow SMAs) remained compressed:
       spread = (
           (max(SMA_fast, SMA_med, SMA_slow) - min(SMA_fast, SMA_med, SMA_slow))
           / SMA_slow * 100
       )
       spread <= compression_threshold_pct
2. Prior Position: On the prior completed bar (``t - 1``), the close was at or below the
   highest of the three moving averages:
       close[t - 1] <= max(SMA_fast[t - 1], SMA_med[t - 1], SMA_slow[t - 1])
3. Breakout Confirmation: On the latest completed bar (``t``), price breaks out strictly above
   the moving average cluster ceiling with a bullish candle:
       close[t] > max(SMA_fast[t], SMA_med[t], SMA_slow[t])
       close[t] > open[t]

Distinct from:
- ``moving_average_ribbon``: uses 12 EMAs (Guppy) and triggers on entire group alignment crossover;
  does not evaluate compression or breakout.
- ``triple_moving_average``: triggers on fast SMA crossing above medium and slow SMAs; does not
  measure compression or price breakout.
- ``bollinger_squeeze`` / ``volatility_squeeze_breakout``: measure volatility band contractions
  (BandWidth or Bollinger inside Keltner), not multi-moving-average convergence.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
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
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
    require_param,
)
from mike_trade.strategies.indicators.patterns import contiguous

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_FAST_PERIOD",
    "DEFAULT_MEDIUM_PERIOD",
    "DEFAULT_SLOW_PERIOD",
    "DEFAULT_COMPRESSION_BARS",
    "DEFAULT_COMPRESSION_THRESHOLD_PCT",
    "WARMUP_BARS",
    "MovingAverageCompressionBuy",
]

STRATEGY_ID = "moving_average_compression"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1

DEFAULT_FAST_PERIOD = 10
DEFAULT_MEDIUM_PERIOD = 20
DEFAULT_SLOW_PERIOD = 50
DEFAULT_COMPRESSION_BARS = 5
DEFAULT_COMPRESSION_THRESHOLD_PCT = Decimal("0.75")


def _required_bars(slow_period: int, compression_bars: int) -> int:
    return slow_period + compression_bars


WARMUP_BARS = _required_bars(DEFAULT_SLOW_PERIOD, DEFAULT_COMPRESSION_BARS)

_SCHEMA = ParameterSchema(
    (
        ParameterSpec(
            "fast_period",
            ParameterKind.INT,
            DEFAULT_FAST_PERIOD,
            2,
            50,
            description="Fast simple moving average length. Default 10.",
        ),
        ParameterSpec(
            "medium_period",
            ParameterKind.INT,
            DEFAULT_MEDIUM_PERIOD,
            3,
            100,
            description="Medium simple moving average length. Default 20.",
        ),
        ParameterSpec(
            "slow_period",
            ParameterKind.INT,
            DEFAULT_SLOW_PERIOD,
            4,
            200,
            description="Slow simple moving average length. Default 50.",
        ),
        ParameterSpec(
            "compression_bars",
            ParameterKind.INT,
            DEFAULT_COMPRESSION_BARS,
            2,
            30,
            description="Consecutive bars preceding breakout requiring compression. Default 5.",
        ),
        ParameterSpec(
            "compression_threshold_pct",
            ParameterKind.DECIMAL,
            DEFAULT_COMPRESSION_THRESHOLD_PCT,
            Decimal("0.05"),
            Decimal("5.0"),
            description=(
                "Maximum percentage spread between moving averages relative to slow MA. "
                "Default 0.75%."
            ),
        ),
    )
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="moving_average",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=_SCHEMA,
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when three simple moving averages (fast, medium, slow) have remained compressed "
        "within compression_threshold_pct for compression_bars completed bars preceding "
        "the signal bar, and the latest completed bar breaks out strictly above the highest "
        "moving average with close[t] > open[t] after prior close[t-1] <= max(MA)[t-1]."
    ),
    source=(
        "Indicator: Simple Moving Averages (SMA); Type A indicator definition. "
        "Trigger: Engineering choice (Type C) implementing quantifiable moving average compression "
        "and bullish breakout confirmation out of the compressed cluster."
    ),
    execution_limitations=(
        "Entry signal only; no stops, sizing, targets, or orders.",
        "Requires completed adjacent regular-session M1 bars; gaps invalidate the evaluation.",
        "Edge-triggered on the first breakout bar above the compressed cluster.",
    ),
    reason_codes=REASON_CODES,
)


class MovingAverageCompressionBuy(BuyStrategy):
    """BUY on bullish breakout following moving average compression."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        p = cls.spec.parameter_schema.validate(params)
        fast, med, slow = p["fast_period"], p["medium_period"], p["slow_period"]
        comp_bars = p["compression_bars"]
        assert isinstance(fast, int) and isinstance(med, int) and isinstance(slow, int)
        assert isinstance(comp_bars, int)
        require_param(
            fast < med < slow,
            "fast_period < medium_period < slow_period required",
            "fast_period",
        )
        return _required_bars(slow, comp_bars)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)
        fast, med, slow = p["fast_period"], p["medium_period"], p["slow_period"]
        comp_bars = p["compression_bars"]
        thresh_pct = p["compression_threshold_pct"]
        assert isinstance(fast, int) and isinstance(med, int) and isinstance(slow, int)
        assert isinstance(comp_bars, int) and isinstance(thresh_pct, Decimal)
        require_param(
            fast < med < slow,
            "fast_period < medium_period < slow_period required",
            "fast_period",
        )

        needed = _required_bars(slow, comp_bars)
        bars = closed_bars(self.spec, context, needed)
        if bars is None:
            return insufficient(self.spec, context)

        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)

        if not contiguous(bars[-needed:]):
            return insufficient(self.spec, context)

        n = len(bars)

        # Helper to compute (fast, med, slow, spread_pct, max_ma) at a bar index
        def _ma_at(idx: int) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
            fast_ma = sum(
                (bars[j].close for j in range(idx - fast + 1, idx + 1)), Decimal("0")
            ) / Decimal(fast)
            med_ma = sum(
                (bars[j].close for j in range(idx - med + 1, idx + 1)), Decimal("0")
            ) / Decimal(med)
            slow_ma = sum(
                (bars[j].close for j in range(idx - slow + 1, idx + 1)), Decimal("0")
            ) / Decimal(slow)
            max_ma = max(fast_ma, med_ma, slow_ma)
            min_ma = min(fast_ma, med_ma, slow_ma)
            spread_pct = (
                Decimal("0") if slow_ma <= 0 else ((max_ma - min_ma) / slow_ma) * Decimal("100")
            )
            return fast_ma, med_ma, slow_ma, spread_pct, max_ma

        # Verify compression across preceding compression_bars:
        # indices (n - 1 - comp_bars) to (n - 2)
        prior_max_ma: Decimal | None = None
        for idx in range(n - 1 - comp_bars, n - 1):
            _f, _m, _s, spread_pct, max_ma = _ma_at(idx)
            if spread_pct > thresh_pct:
                return finish(self.spec, context, bars[-1], state, False)
            if idx == n - 2:
                prior_max_ma = max_ma

        assert prior_max_ma is not None

        # Prior bar close must have been at or below the MA cluster ceiling
        if bars[-2].close > prior_max_ma:
            return finish(self.spec, context, bars[-1], state, False)

        # Latest bar (breakout bar): price breaks out strictly above the latest MA cluster ceiling
        _f_lat, _m_lat, _s_lat, _spread_lat, latest_max_ma = _ma_at(n - 1)

        breakout = (bars[-1].close > latest_max_ma) and (bars[-1].close > bars[-1].open)

        return finish(self.spec, context, bars[-1], state, breakout)
