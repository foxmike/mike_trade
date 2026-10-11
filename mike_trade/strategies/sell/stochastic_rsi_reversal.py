"""Exit a long when Stochastic RSI leaves its overbought region."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
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
    crossed_below,
    finish,
    gate,
    insufficient,
    known,
)
from mike_trade.strategies.indicators.core import RSICalculator
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class _StochasticRSICalculator:
    def __init__(self, rsi_period: int, stochastic_period: int) -> None:
        self.rsi_period, self.stochastic_period = rsi_period, stochastic_period
        self._rsi = RSICalculator(rsi_period)
        self._values: deque[Decimal] = deque(maxlen=stochastic_period)

    def update(self, close: Decimal) -> Decimal | None:
        value = self._rsi.update(close)
        if value is None:
            return None
        self._values.append(value)
        if len(self._values) < self.stochastic_period:
            return None
        low, high = min(self._values), max(self._values)
        if high == low:
            return None
        with localcontext() as arithmetic:
            arithmetic.prec = 28
            arithmetic.rounding = ROUND_HALF_EVEN
            return Decimal(100) * (value - low) / (high - low)

    def snapshot(self) -> dict[str, object]:
        return {
            "rsi_period": self.rsi_period,
            "stochastic_period": self.stochastic_period,
            "rsi": self._rsi.snapshot(),
            "values": [str(value) for value in self._values],
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> _StochasticRSICalculator:
        rsi_period = snapshot["rsi_period"]
        stochastic_period = snapshot["stochastic_period"]
        rsi_snapshot, values = snapshot["rsi"], snapshot["values"]
        if not isinstance(rsi_period, int) or not isinstance(stochastic_period, int):
            raise ValueError("invalid StochRSI periods")
        if not isinstance(rsi_snapshot, Mapping) or not isinstance(values, list):
            raise ValueError("invalid StochRSI snapshot")
        if any(not isinstance(value, str) for value in values):
            raise ValueError("invalid StochRSI values")
        calc = cls(rsi_period, stochastic_period)
        calc._rsi = RSICalculator.restore(rsi_snapshot)
        if calc._rsi.period != rsi_period or len(values) > stochastic_period:
            raise ValueError("inconsistent StochRSI snapshot")
        calc._values.extend(Decimal(value) for value in values)
        return calc


class StochasticRsiReversalSell(SellRule):
    spec = StrategySpec(
        strategy_id="stochastic_rsi_reversal",
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="oscillator",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=29,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec("rsi_period", ParameterKind.INT, 14, 2, 100),
                ParameterSpec("stochastic_period", ParameterKind.INT, 14, 2, 100),
                ParameterSpec(
                    "overbought", ParameterKind.DECIMAL, Decimal("80"), Decimal("1"), Decimal("99")
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "SELL when raw Stochastic RSI crosses down through 80; StochRSI = 100 * "
            "(RSI - lowest RSI) / (highest RSI - lowest RSI)."
        ),
        source=(
            "Tushar Chande and Stanley Kroll, The New Technical Trader (1994); "
            "StockCharts ChartSchool, StochRSI: https://chartschool.stockcharts.com/"
            "table-of-contents/technical-indicators-and-overlays/technical-indicators/"
            "stochrsi"
        ),
        execution_limitations=(
            "Uses unsmoothed StochRSI and a conventional 80 boundary; flat RSI windows "
            "are undefined.",
            "Wilder RSI and its rolling RSI range are snapshot-restored; adjacent "
            "completed M1 bars only.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        values = cls.spec.parameter_schema.validate(params)
        rsi_period, stochastic_period = values["rsi_period"], values["stochastic_period"]
        assert isinstance(rsi_period, int) and isinstance(stochastic_period, int)
        return rsi_period + stochastic_period + 1

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        rp, sp, threshold = values["rsi_period"], values["stochastic_period"], values["overbought"]
        assert isinstance(rp, int) and isinstance(sp, int) and isinstance(threshold, Decimal)
        bars = closed_bars(self.spec, context, rp + sp + 1)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        adv = advance(
            state,
            bars,
            f"stochastic_rsi_reversal|1.0.0|{rp}|{sp}",
            fresh=lambda: _StochasticRSICalculator(rp, sp),
            restore=_StochasticRSICalculator.restore,
            feed=lambda calc, bar: calc.update(bar.close),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        if not known(adv.prior, adv.latest):
            return insufficient(self.spec, context, adv.updates)
        return finish(
            self.spec,
            context,
            bars[-1],
            state,
            crossed_below(adv.prior, threshold, adv.latest, threshold),
            adv.updates,
        )
