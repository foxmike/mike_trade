"""Bullish opening-drive BUY on completed regular-session one-minute bars.

Selected mechanical interpretation: the first ``drive_minutes`` bars are all
bullish, their closes strictly increase, and no bar after the first trades below
the regular-session opening price. The signal occurs only when the final drive
bar closes. This captures the published definition of a sustained one-directional
push from the open with little retracement; it makes no profitability claim.
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
from mike_trade.strategies.buy._session import current_session_bars
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    finish,
    gate,
    insufficient,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "opening_drive"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_DRIVE_MINUTES = 5
WARMUP_BARS = DEFAULT_DRIVE_MINUTES

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="intraday_session", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema((ParameterSpec(
        "drive_minutes", ParameterKind.INT, default=DEFAULT_DRIVE_MINUTES,
        min_value=2, max_value=30,
        description="Number of completed M1 bars from the 09:30 ET open in the drive.",
    ),)),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "At the close of the configured opening-drive window, BUY when every completed "
        "M1 bar is bullish, closes strictly increase, and no bar after the first trades "
        "below the first bar's open."
    ),
    source=(
        "Mike Bellafiore, The PlayBook (FT Press, 2013), opening-drive playbook; "
        "Swoopr, 'Opening Drive in Trading Explained' (sustained directional push "
        "from 09:30 with little pullback): https://www.getswoopr.com/technical-analysis/"
        "price-action/opening-drive/"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Requires every completed M1 bar from the exact NYSE regular-session open.",
        "Signals only once, exactly when the configured opening window completes.",
    ), reason_codes=REASON_CODES,
)


class OpeningDriveBuy(BuyStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return cls.spec.parameter_schema.validate(params)["drive_minutes"]  # type: ignore[return-value]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        minutes = self.spec.parameter_schema.validate(params)["drive_minutes"]
        assert isinstance(minutes, int)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        found = current_session_bars(context)
        if found is None or len(found[1]) < minutes:
            return insufficient(self.spec, context)
        bars = found[1]
        latest = bars[-1]
        if len(bars) != minutes:
            return finish(self.spec, context, latest, state, False)
        opening_price = bars[0].open
        bullish = all(bar.close > bar.open for bar in bars)
        rising = all(
            prior.close < bar.close for prior, bar in zip(bars, bars[1:], strict=False)
        )
        holds_open = all(bar.low >= opening_price for bar in bars[1:])
        return finish(self.spec, context, latest, state, bullish and rising and holds_open)
