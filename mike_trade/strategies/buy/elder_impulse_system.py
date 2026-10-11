"""Long entry when Elder's Impulse System turns green (EMA and MACD histogram both rising)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, finish, gate, insufficient, parts, require_param
from mike_trade.strategies.indicators.extended import ElderImpulseCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "elder_impulse_system"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class ElderImpulseSystemBuy(BuyStrategy):
    """BUY on the bar where the Impulse System becomes green after a non-green bar.

    A bar is green when its EMA(13) is higher than the previous bar's *and* its MACD histogram
    (MACD(12,26,9) minus its signal line) is higher than the previous bar's.  Signal: previous
    bar not green, latest bar green.  This is the MACD-histogram momentum rule of Elder's
    system, not a MACD line crossover (see ``macd_signal_crossover``).  All EMAs are SMA-seeded
    and resumed from persisted state.  Needs ``slow + signal + 1`` completed bars (36).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="momentum", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=36,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("ema_period", ParameterKind.INT, 13, 2, 100,
                          description="Trend EMA length. Default 13."),
            ParameterSpec("fast_period", ParameterKind.INT, 12, 2, 100,
                          description="MACD fast EMA length. Default 12."),
            ParameterSpec("slow_period", ParameterKind.INT, 26, 3, 200,
                          description="MACD slow EMA length; must exceed fast_period. Default 26."),
            ParameterSpec("signal_period", ParameterKind.INT, 9, 2, 100,
                          description="MACD signal EMA length. Default 9."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when both the 13-EMA and the MACD histogram rise versus the previous "
                     "bar (Impulse System green) and the previous bar was not green."),
        source=("Alexander Elder, The New Trading for a Living (2014), the Impulse System "
                "(green = rising 13-EMA and rising MACD-Histogram); StockCharts ChartSchool, "
                "Elder Impulse System."),
        execution_limitations=("Entry signal only: Elder's rule that red bars forbid buying is "
                               "implicit because only a turn to green signals.",
                               "Elder's daily-bar parameters adapted to M1 bars.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        v = cls.spec.parameter_schema.validate(params)
        return max(v["slow_period"] + v["signal_period"] + 1, v["ema_period"] + 2)  # type: ignore[operator]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        ema, fast, slow, signal = (v["ema_period"], v["fast_period"], v["slow_period"],
                                   v["signal_period"])
        assert isinstance(ema, int) and isinstance(fast, int)
        assert isinstance(slow, int) and isinstance(signal, int)
        require_param(fast < slow, "fast_period < slow_period", "fast_period")
        need = self.required_bars(params)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{ema}|{fast}|{slow}|{signal}",
                      fresh=lambda: ElderImpulseCalculator(ema, fast, slow, signal),
                      restore=ElderImpulseCalculator.restore,
                      feed=lambda calc, bar: calc.update(bar.close))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        was_green = parts(adv.prior, 3)[2]
        is_green = parts(adv.latest, 3)[2]
        if was_green is None or is_green is None:
            return insufficient(self.spec, context, adv.updates)
        return finish(self.spec, context, bars[-1], state, is_green and not was_green,
                      adv.updates)
