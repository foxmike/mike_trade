"""Long entry on a confirmed rebound from previously established technical support."""

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
from mike_trade.strategies.indicators.patterns import contiguous
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "support_bounce"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_STRENGTH = 2
DEFAULT_LOOKBACK = 50
DEFAULT_TOLERANCE_PCT = Decimal("0.2")


def _required_bars(lookback: int) -> int:
    return lookback + 2


WARMUP_BARS = _required_bars(DEFAULT_LOOKBACK)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="price_action",
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
                "strength",
                ParameterKind.INT,
                DEFAULT_STRENGTH,
                1,
                10,
                description="Bars on each side of a swing low. Default 2.",
            ),
            ParameterSpec(
                "lookback",
                ParameterKind.INT,
                DEFAULT_LOOKBACK,
                10,
                200,
                description="Completed bars before setup searched for the support pivot. Default 50.",
            ),
            ParameterSpec(
                "tolerance_pct",
                ParameterKind.DECIMAL,
                DEFAULT_TOLERANCE_PCT,
                Decimal("0.0"),
                Decimal("2.0"),
                description="Maximum distance from support for test, percent. Default 0.2%.",
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when price tests previously established technical support (the latest confirmed "
        "swing low within the prior lookback window) and confirms a bullish rebound by closing "
        "above the setup bar's high and above the support level."
    ),
    source=(
        "Concept: support as a price level where earlier buying halted a decline (Edwards and "
        "Magee, Technical Analysis of Stock Trends; John J. Murphy, Technical Analysis of the "
        "Financial Markets (1999), ch. 4). Pivot: Bill Williams fractal swing low (Trading "
        "Chaos), a published indicator definition. No published source defines this exact "
        "trigger: the touch tolerance (tolerance_pct), the support-invalidation test and the "
        "confirmation (close above the setup bar's high and above support) are this library's "
        "engineering choices."
    ),
    execution_limitations=(
        "Entry signal only: no stops, sizing, targets, or orders.",
        "Support pivot S is confirmed strictly prior to the test candle (strength bars on each side); no look-ahead.",
        "Setup bar must test support within tolerance_pct without closing below S * (1 - tolerance_pct/100).",
        "Support is invalidated, and nothing signals, if any bar after the pivot (through the setup "
        "bar) traded below S * (1 - tolerance_pct/100) - a lower low that is not yet a confirmed pivot "
        "must not leave an older, already-broken level in force.",
        "Every bar from the pivot to the signal bar must be consecutive (an overnight or intraday hole "
        "yields INSUFFICIENT_DATA), so support from a previous session is never carried across the gap.",
        "Distinct from double_bottom_reversal (which requires two lows and enters on an interim peak breakout) "
        "and resistance_breakout (which breaks above a swing high).",
        "Completed adjacent M1 bars; regular session and BUY eligibility apply.",
    ),
    reason_codes=REASON_CODES,
)


class SupportBounceBuy(BuyStrategy):
    """BUY on a confirmed rebound from previously confirmed swing low support."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        lookback = cls.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        return _required_bars(lookback)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        strength = values["strength"]
        lookback = values["lookback"]
        tolerance_pct = values["tolerance_pct"]
        assert (
            isinstance(strength, int)
            and isinstance(lookback, int)
            and isinstance(tolerance_pct, Decimal)
        )
        require_param(
            lookback >= 2 * strength + 2, "lookback >= 2 * strength + 2", "lookback"
        )
        reason = gate(self.spec, context, state)
        need = _required_bars(lookback)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        history = bars[-need:-2]
        _, lows = swing_pivots(history, strength)
        if not lows:
            return finish(self.spec, context, bars[-1], state, False)

        pivot_index, support_price = lows[-1]
        # absolute position of the pivot bar inside ``bars``; history is bars[-need:-2]
        pivot_position = len(bars) - need + pivot_index
        if not contiguous(bars[pivot_position:]):
            return insufficient(self.spec, context)
        setup = bars[-2]
        signal = bars[-1]

        upper_bound = support_price * (Decimal(1) + tolerance_pct / Decimal(100))
        lower_bound = support_price * (Decimal(1) - tolerance_pct / Decimal(100))

        # 1. Setup bar touched or reached into the support zone
        # 2. Support held (did not crash below lower_bound, close held >= lower_bound)
        # 3. Signal bar confirmed rebound: close > setup.high and close > support_price
        # Support is only "held" if no bar since the pivot (the setup bar included) broke below it
        held = all(bar.low >= lower_bound for bar in bars[pivot_position + 1:-1])
        test_ok = setup.low <= upper_bound and setup.close >= lower_bound and held
        confirm_ok = signal.close > setup.high and signal.close > support_price
        triggered = test_ok and confirm_ok

        return finish(self.spec, context, signal, state, triggered)
