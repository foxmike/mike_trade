"""Intraday consolidation breakout BUY strategy.

Captures an upside breakout from a tight intraday consolidation or base:

1. Base Formation: Over the preceding `consolidation_bars` completed bars
   (strictly before the breakout bar, bars `t - consolidation_bars` to `t - 1`),
   the consolidation range is bounded by:
       Ceiling = max(High_{t-consolidation_bars .. t-1})
       Floor = min(Low_{t-consolidation_bars .. t-1})
2. Compression Qualification: The consolidation base must be tight:
       (Ceiling - Floor) / Floor * 100 <= max_range_pct
3. First breakout (prior position): the prior completed bar (t-1) did not itself close
   above the base that preceded it, i.e. with PriorCeiling = max(High_{t-1-consolidation_bars .. t-2}):
       Close_{t-1} <= PriorCeiling
   (Without this, a slow steady advance in which every bar closes above the previous N highs
   satisfies the narrow-range test on each bar and signals repeatedly as the window rolls
   forward; testing the prior bar against the *same* ceiling that includes bar t-1 is always
   true because a close never exceeds its own bar's high.)
4. Breakout Confirmation: The latest completed bar (t) closes strictly above the
   consolidation ceiling:
       Close_t > Ceiling

The breakout candle (t) DOES NOT participate in defining the consolidation range,
ensuring a point-in-time test against previously established resistance.

Materially distinct from:
- `opening_range_breakout`: ORB is strictly anchored to the opening minutes from 09:30.
  `intraday_consolidation_breakout` evaluates rolling intraday bases formed at any
  time during the regular session.
- `donchian_channel_breakout`: Donchian breakouts trigger on any N-period channel high
  regardless of channel width. `intraday_consolidation_breakout` strictly enforces
  a tight range compression threshold (`max_range_pct`).
- `volatility_squeeze_breakout`: Uses Bollinger Bands inside Keltner Channels and
  linear regression momentum. `intraday_consolidation_breakout` uses direct price
  action rectangle boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleStatus, CandleTimeframe
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ReasonCode,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    ELIGIBILITY_KEY,
    ELIGIBILITY_WATCHING_BUY,
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    finish,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_CONSOLIDATION_BARS",
    "DEFAULT_MAX_RANGE_PCT",
    "WARMUP_BARS",
    "IntradayConsolidationBreakoutBuy",
]

STRATEGY_ID = "intraday_consolidation_breakout"
STRATEGY_VERSION = "1.0.0"

DEFAULT_CONSOLIDATION_BARS = 15
DEFAULT_MAX_RANGE_PCT = Decimal("0.75")
WARMUP_BARS = DEFAULT_CONSOLIDATION_BARS + 2

_NEW_YORK = ZoneInfo("America/New_York")


def _required_bars(consolidation_bars: int) -> int:
    # the base, the breakout bar, and one earlier bar for the prior bar's own base test
    return consolidation_bars + 2


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="breakout",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "consolidation_bars",
                ParameterKind.INT,
                DEFAULT_CONSOLIDATION_BARS,
                5,
                60,
                description="Number of completed bars in consolidation base strictly preceding breakout bar. Default 15.",
            ),
            ParameterSpec(
                "max_range_pct",
                ParameterKind.DECIMAL,
                DEFAULT_MAX_RANGE_PCT,
                Decimal("0.1"),
                Decimal("3.0"),
                description="Maximum range width of consolidation base as percentage of floor. Default 0.75%.",
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when price completes a tight intraday rectangular consolidation over N bars "
        "((Ceiling - Floor) / Floor * 100 <= max_range_pct) and the latest bar confirms "
        "an upside breakout by closing strictly above the consolidation ceiling (close[t] > Ceiling), "
        "provided the prior bar did not itself close above the base that preceded it "
        "(close[t-1] <= max high of the N bars before t-1), so only the first breakout bar signals. "
        "The breakout candle does not participate in defining the consolidation range."
    ),
    source=(
        "Concept: a price range that contracts and is then broken, discussed under rectangles and "
        "continuation patterns (Thomas Bulkowski, Encyclopedia of Chart Patterns; John J. Murphy, "
        "Technical Analysis of the Financial Markets (1999), ch. 6) and narrow-range contraction "
        "before expansion (Toby Crabel, Day Trading with Short Term Price Patterns and Opening Range "
        "Breakout). No published source gives this exact definition: the 15-bar window, the 0.75% "
        "maximum range (as a percentage of the range floor), the close-through-ceiling confirmation "
        "and the first-breakout condition are this library's engineering choices."
    ),
    execution_limitations=(
        "Entry signal only: no stops, sizing, targets, or orders.",
        "Long entries only; regular session only; completed adjacent 1-minute bars.",
        "Consolidation window strictly precedes the breakout bar (no look-ahead).",
        "Only the first breakout bar signals: a bar whose predecessor already closed above its own "
        "preceding base does not (a steadily rising price does not signal on every bar).",
        "A narrow range does not exclude a slow drift: the rule requires compression of the highs and "
        "lows, not a flat trend.",
        "Consolidation and breakout bars must belong to the same regular intraday session.",
        "One signal per completed bar: replaying the same bar never emits twice.",
    ),
    reason_codes=REASON_CODES,
)


def _is_valid_closed_bar(candle: object, symbol: str) -> bool:
    if not isinstance(candle, Candle):
        return False
    if candle.symbol != symbol or candle.timeframe is not CandleTimeframe.M1:
        return False
    if candle.status is not CandleStatus.CLOSED:
        return False
    for price in (candle.open, candle.high, candle.low, candle.close):
        if (
            isinstance(price, bool)
            or not isinstance(price, Decimal)
            or not price.is_finite()
            or price <= 0
        ):
            return False
    return True


class IntradayConsolidationBreakoutBuy(BuyStrategy):
    """BUY on a confirmed upside close breakout from a tight intraday consolidation base."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        n = cls.spec.parameter_schema.validate(params)["consolidation_bars"]
        assert isinstance(n, int)
        return _required_bars(n)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        consolidation_bars = values["consolidation_bars"]
        max_range_pct = values["max_range_pct"]
        assert isinstance(consolidation_bars, int) and isinstance(max_range_pct, Decimal)

        symbol = context.symbol

        if context.freshness.stale:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.STALE_DATA,)
            )
        if context.session_status is not SessionStatus.REGULAR:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,)
            )
        if not isinstance(state, Mapping) or state.get(ELIGIBILITY_KEY) != (
            ELIGIBILITY_WATCHING_BUY
        ):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,)
            )

        need_bars = _required_bars(consolidation_bars)
        bars = context.candles.get(CandleTimeframe.M1, ())
        if len(bars) < need_bars:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        if any(not _is_valid_closed_bar(bar, symbol) for bar in bars):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        # Check session boundary: all bars in consolidation window + breakout bar must be in same session
        session_date = bars[-1].start.astimezone(_NEW_YORK).date()
        window = bars[-need_bars:]
        if any(b.start.astimezone(_NEW_YORK).date() != session_date for b in window):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        # Ensure all bars in the evaluation window are strictly adjacent
        if any(
            b.start != a.start + timedelta(minutes=1)
            for a, b in zip(window, window[1:])
        ):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        consolidation_window = window[1:-1]  # the N bars before the breakout bar
        breakout_bar = window[-1]
        prior_bar = consolidation_window[-1]
        prior_ceiling = max(b.high for b in window[:-2])  # the base as it stood before bar t-1

        ceiling = max(b.high for b in consolidation_window)
        floor = min(b.low for b in consolidation_window)

        if floor <= Decimal("0"):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        range_pct = (ceiling - floor) / floor * Decimal("100")

        # 1. Base must satisfy compression threshold
        # 2. Prior bar did not already break out of the base that preceded it (first breakout)
        # 3. Breakout bar closed strictly above ceiling
        is_compressed = range_pct <= max_range_pct
        is_breakout = prior_bar.close <= prior_ceiling and breakout_bar.close > ceiling

        triggered = is_compressed and is_breakout

        return finish(self.spec, context, breakout_bar, state, triggered)
