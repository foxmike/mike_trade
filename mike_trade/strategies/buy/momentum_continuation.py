"""Long entry on momentum continuation after a brief consolidation pause."""

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
    adjacent,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
    require_param,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "momentum_continuation"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
PERCENT = Decimal("100")


class MomentumContinuationBuy(BuyStrategy):
    """BUY on breakout resumption following a momentum impulse and tight consolidation pause.

    An initial bullish impulse of consecutive rising closes establishes short-term momentum.
    Price then enters a shallow consolidation pause where it holds strictly above the impulse
    origin and retraces no more than max_retrace of the impulse height.  The signal triggers
    on the causal edge where the latest completed bar breaks above the consolidation high
    with a bullish closing candle.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.BUY,
        category="technical",
        sub_category="momentum",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=10,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "impulse_bars",
                    ParameterKind.INT,
                    3,
                    2,
                    10,
                    description="Completed bars forming the bullish momentum impulse. Default 3.",
                ),
                ParameterSpec(
                    "min_impulse_pct",
                    ParameterKind.DECIMAL,
                    Decimal("0.2"),
                    Decimal("0.05"),
                    Decimal("5.0"),
                    description="Minimum percentage gain across the impulse bars. Default 0.2.",
                ),
                ParameterSpec(
                    "min_pause_bars",
                    ParameterKind.INT,
                    2,
                    1,
                    10,
                    description="Minimum consolidation pause bars before breakout. Default 2.",
                ),
                ParameterSpec(
                    "max_pause_bars",
                    ParameterKind.INT,
                    5,
                    2,
                    20,
                    description="Maximum consolidation pause bars. Default 5.",
                ),
                ParameterSpec(
                    "max_retrace",
                    ParameterKind.DECIMAL,
                    Decimal("0.5"),
                    Decimal("0.1"),
                    Decimal("0.9"),
                    description="Maximum fraction of the impulse height retraced. Default 0.5.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "BUY when price closes above the high of a shallow consolidation pause that "
            "immediately follows a multi-bar bullish momentum impulse, provided price held "
            "above the impulse origin and retraced no more than max_retrace of the advance."
        ),
        source=(
            "Jeff Cooper, Hit & Run Trading (1996), momentum continuation patterns; "
            "Martin Pring, Technical Analysis Explained (McGraw-Hill), momentum continuation "
            "setups. Bullish thrust followed by tight consolidation pause and causal breakout "
            "resumption."
        ),
        execution_limitations=(
            "Finite rolling window; completed adjacent M1 bars; regular session only.",
            "Edge trigger: fires only on the first bar closing above the consolidation high.",
            "Advisory long entry only; no stop, sizing, or orders provided.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        values = cls.spec.parameter_schema.validate(params)
        impulse_bars, max_pause = values["impulse_bars"], values["max_pause_bars"]
        assert isinstance(impulse_bars, int) and isinstance(max_pause, int)
        return impulse_bars + max_pause + 2

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        impulse_bars = values["impulse_bars"]
        min_impulse_pct = values["min_impulse_pct"]
        min_pause_bars = values["min_pause_bars"]
        max_pause_bars = values["max_pause_bars"]
        max_retrace = values["max_retrace"]

        assert isinstance(impulse_bars, int)
        assert isinstance(min_impulse_pct, Decimal)
        assert isinstance(min_pause_bars, int)
        assert isinstance(max_pause_bars, int)
        assert isinstance(max_retrace, Decimal)

        require_param(
            min_pause_bars <= max_pause_bars,
            "min_pause_bars <= max_pause_bars",
            "max_pause_bars",
        )

        need = impulse_bars + max_pause_bars + 2
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        bars = bars[-need:]

        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)

        if any(not adjacent(a, b) for a, b in zip(bars, bars[1:], strict=False)):
            return insufficient(self.spec, context)

        latest = bars[-1]
        if latest.close <= latest.open:
            return finish(self.spec, context, latest, state, False)

        triggered = False
        for k in range(min_pause_bars, max_pause_bars + 1):
            pause = bars[-(k + 1) : -1]
            pause_high = max(b.high for b in pause)
            pause_low = min(b.low for b in pause)

            if not (latest.close > pause_high and bars[-2].close <= pause_high):
                continue

            impulse_start = -(k + 1 + impulse_bars)
            impulse_end = -(k + 1)
            impulse = bars[impulse_start:impulse_end]
            if len(impulse) != impulse_bars:
                continue

            if not all(b.close > a.close for a, b in zip(impulse, impulse[1:], strict=False)):
                continue

            origin_low = min(b.low for b in impulse)
            peak_high = max(b.high for b in impulse)
            impulse_range = peak_high - origin_low

            if impulse_range <= 0 or origin_low <= 0:
                continue

            advance_pct = (impulse_range / origin_low) * PERCENT
            if advance_pct < min_impulse_pct:
                continue

            if pause_high > peak_high or pause_low <= origin_low:
                continue

            retrace = (peak_high - pause_low) / impulse_range
            if retrace > max_retrace:
                continue

            triggered = True
            break

        return finish(self.spec, context, latest, state, triggered)
