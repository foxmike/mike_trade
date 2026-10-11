"""Long entry when the close returns inside the lower Keltner band."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, finish, gate, insufficient, known, parts
from mike_trade.strategies.indicators.extended import KeltnerChannelCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "keltner_channel_reversion"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class KeltnerChannelReversionBuy(BuyStrategy):
    """BUY when a close below the lower Keltner band is followed by a close back above it.

    Channel (Raschke's modification): ``EMA(close, ema_period) -/+ multiplier * ATR(atr_period)``
    with Wilder ATR.  Signal: ``close[t-1] < lower[t-1]`` and ``close[t] > lower[t]`` - an
    over-extended close that has re-entered the channel.  Unlike ``keltner_channel_breakout``
    (Keltner's original SMA/range channel, upper-band momentum entry) this fades a lower-band
    excursion.  EMA and ATR are recursive and resumed from persisted state.  Needs
    ``max(ema_period, atr_period) + 1`` completed bars (21 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="mean_reversion", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=21,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("ema_period", ParameterKind.INT, 20, 2, 200,
                          description="Centre-line EMA length. Default 20."),
            ParameterSpec("atr_period", ParameterKind.INT, 10, 1, 100,
                          description="Wilder ATR length for the band width. Default 10."),
            ParameterSpec("multiplier", ParameterKind.DECIMAL, Decimal(2), Decimal("0.5"), Decimal(10),
                          description="ATR multiple of the band offset. Default 2."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when the prior close was below the lower Keltner band and the latest "
                     "close is back above it."),
        source=("Chester Keltner, How to Make Money in Commodities (1960), as modified by Linda "
                "Raschke (EMA centre line, ATR width); StockCharts ChartSchool, Keltner Channels "
                "(EMA 20, ATR 10, multiplier 2)."),
        execution_limitations=("Entry signal only: no stops, sizing, targets, or orders.",
                               "Re-entry above the band is a defined confirmation of the reversion; "
                               "it does not predict continuation to the centre line.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        v = cls.spec.parameter_schema.validate(params)
        return max(v["ema_period"], v["atr_period"]) + 1  # type: ignore[type-var,operator]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        ema, atr, multiplier = v["ema_period"], v["atr_period"], v["multiplier"]
        assert isinstance(ema, int) and isinstance(atr, int) and isinstance(multiplier, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, max(ema, atr) + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{ema}|{atr}|{multiplier}",
                      fresh=lambda: KeltnerChannelCalculator(ema, atr, multiplier),
                      restore=KeltnerChannelCalculator.restore,
                      feed=lambda calc, bar: calc.update(bar))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        lower_prior = parts(adv.prior, 3)[0]
        lower_latest = parts(adv.latest, 3)[0]
        if not known(lower_prior, lower_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = bars[-2].close < lower_prior and bars[-1].close > lower_latest
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
