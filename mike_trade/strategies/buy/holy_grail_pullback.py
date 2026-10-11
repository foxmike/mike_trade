"""Long entry on Raschke's Holy Grail pullback continuation."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, finish, gate, insufficient, known, parts
from mike_trade.strategies.indicators.oscillators import ADXEMACalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "holy_grail_pullback"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required(adx_period: int, ema_period: int) -> int:
    return max(2 * adx_period + 2, ema_period + 2)


class HolyGrailPullbackBuy(BuyStrategy):
    """BUY the bar that closes above the high of a setup bar that pulled back to the EMA in a strong trend.

    Setup bar ``s = t-1``: ADX(`adx_period`) above `adx_threshold` and rising, +DI > -DI, and
    ``low[s] <= EMA(`ema_period`)[s]`` (the pullback touch).  Signal bar ``t``: ``close[t] > high[s]``
    (Raschke buys a stop at the setup bar's high; the completed-bar form requires the close
    above it).  ADX/DI and EMA are resumed from persisted state.  Needs
    ``max(2 * adx_period + 2, ema_period + 2)`` completed bars (30 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="trend_following", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=30,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("adx_period", ParameterKind.INT, 14, 2, 100,
                          description="Wilder ADX/DI length. Default 14."),
            ParameterSpec("adx_threshold", ParameterKind.DECIMAL, Decimal(30), Decimal(10), Decimal(60),
                          description="Minimum ADX at the setup bar. Default 30."),
            ParameterSpec("ema_period", ParameterKind.INT, 20, 2, 200,
                          description="Pullback EMA length. Default 20."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when a bar closes above the high of the prior bar whose low touched the EMA "
                     "while ADX was above its threshold and rising with +DI above -DI."),
        source=("Linda Bradford Raschke and Laurence Connors, Street Smarts (1995), the Holy Grail "
                "setup (ADX > 30 and rising, pullback to the 20-period EMA, buy through the high of "
                "the touch bar)."),
        execution_limitations=("Published summaries differ on EMA versus SMA and on stop-order versus "
                               "close confirmation; the EMA and close-above-high form are used.",
                               "Entry signal only; Raschke's stops and targets are not included.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        v = cls.spec.parameter_schema.validate(params)
        return _required(v["adx_period"], v["ema_period"])  # type: ignore[arg-type]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        adx_period, threshold, ema_period = v["adx_period"], v["adx_threshold"], v["ema_period"]
        assert isinstance(adx_period, int) and isinstance(ema_period, int)
        assert isinstance(threshold, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required(adx_period, ema_period))
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{adx_period}|{ema_period}",
                      fresh=lambda: ADXEMACalculator(adx_period, ema_period),
                      restore=ADXEMACalculator.restore, feed=lambda calc, bar: calc.update(bar))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        plus, minus, adx, ema, rising = parts(adv.prior, 5)
        if not adjacent(bars[-2], bars[-1]) or not known(plus, minus, adx, ema, rising):
            return insufficient(self.spec, context, adv.updates)
        setup, signal = bars[-2], bars[-1]
        triggered = (adx > threshold and rising and plus > minus
                     and setup.low <= ema and signal.close > setup.high)
        return finish(self.spec, context, signal, state, triggered, adv.updates)
