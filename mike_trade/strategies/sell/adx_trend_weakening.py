"""Exit a long when Wilder's ADX falls below the no-trend threshold."""

from __future__ import annotations

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
    parts,
)
from mike_trade.strategies.indicators.core import DMICalculator
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.candles import Candle
    from mike_trade.market_data.context import MarketContext


class _ADXCalculator:
    """Wilder ADX layered on the existing snapshot-capable DMI calculator."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._dmi = DMICalculator(period)
        self._seed: list[Decimal] = []
        self._value: Decimal | None = None

    def update(self, bar: Candle) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        plus, minus = self._dmi.update(bar)  # type: ignore[arg-type]
        if plus is None or minus is None or plus + minus == 0:
            return plus, minus, None
        with localcontext() as arithmetic:
            arithmetic.prec = 28
            arithmetic.rounding = ROUND_HALF_EVEN
            dx = Decimal(100) * abs(plus - minus) / (plus + minus)
            if self._value is None:
                self._seed.append(dx)
                if len(self._seed) < self.period:
                    return plus, minus, None
                self._value = sum(self._seed, Decimal(0)) / Decimal(self.period)
                self._seed.clear()
            else:
                count = Decimal(self.period)
                self._value = (self._value * (count - 1) + dx) / count
        return plus, minus, self._value

    def snapshot(self) -> dict[str, object]:
        return {
            "period": self.period,
            "dmi": self._dmi.snapshot(),
            "seed": [str(value) for value in self._seed],
            "value": None if self._value is None else str(self._value),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> _ADXCalculator:
        period, dmi_snapshot, seed = snapshot["period"], snapshot["dmi"], snapshot["seed"]
        if not isinstance(period, int) or not isinstance(dmi_snapshot, Mapping):
            raise ValueError("invalid ADX snapshot")
        if not isinstance(seed, list) or any(not isinstance(value, str) for value in seed):
            raise ValueError("invalid ADX seed")
        calc = cls(period)
        calc._dmi = DMICalculator.restore(dmi_snapshot)
        if calc._dmi.period != period:
            raise ValueError("ADX and DMI periods differ")
        calc._seed = [Decimal(value) for value in seed]
        stored = snapshot["value"]
        calc._value = None if stored is None else Decimal(str(stored))
        return calc


class AdxTrendWeakeningSell(SellRule):
    spec = StrategySpec(
        strategy_id="adx_trend_weakening",
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="trend_following",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=29,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec("period", ParameterKind.INT, 14, 2, 100),
                ParameterSpec(
                    "threshold", ParameterKind.DECIMAL, Decimal("20"), Decimal("1"), Decimal("99")
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "SELL when Wilder ADX crosses from at-or-above to below the configured "
            "no-trend threshold (20 by default)."
        ),
        source=(
            "J. Welles Wilder, New Concepts in Technical Trading Systems (1978); "
            "StockCharts ChartSchool, Average Directional Index (ADX): "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-"
            "and-overlays/technical-indicators/average-directional-index-adx"
        ),
        execution_limitations=(
            "ADX measures trend strength, not direction; this rule exits a long when "
            "trend strength dissipates.",
            "Recursive DMI/ADX is snapshot-restored; adjacent completed regular-session "
            "M1 bars only.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return 2 * period + 1

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        period, threshold = values["period"], values["threshold"]
        assert isinstance(period, int) and isinstance(threshold, Decimal)
        bars = closed_bars(self.spec, context, 2 * period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        adv = advance(
            state,
            bars,
            f"adx_trend_weakening|1.0.0|{period}",
            fresh=lambda: _ADXCalculator(period),
            restore=_ADXCalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        adx_prior, adx_latest = parts(adv.prior, 3)[2], parts(adv.latest, 3)[2]
        if not known(adx_prior, adx_latest):
            return insufficient(self.spec, context, adv.updates)
        return finish(
            self.spec,
            context,
            bars[-1],
            state,
            crossed_below(adx_prior, threshold, adx_latest, threshold),
            adv.updates,
        )
