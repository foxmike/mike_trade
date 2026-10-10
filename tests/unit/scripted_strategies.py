"""Deterministic scripted BUY/SELL strategy fixtures for lifecycle tests.

These are test doubles for the strategy SDK, not trading strategies: each one
fires on a scripted completed-bar count and keeps an evaluation counter in its
own persistent state, which lets tests prove arbitration, per-symbol
collections, parameter isolation and state isolation without market maths.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.market_data.context import MarketContext
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ReasonCode,
    SellStrategy,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.registry import StrategyRegistry

__all__ = ["build_registry", "make_strategy", "STUBS"]

_SCHEMA = ParameterSchema(
    (
        # Fire when ``context.version`` (completed bars seen) equals ``fire_at``.
        ParameterSpec("fire_at", ParameterKind.INT, default=-1, min_value=-1, max_value=10_000),
        # When positive, keep firing every ``fire_every`` bars after ``fire_at``.
        ParameterSpec("fire_every", ParameterKind.INT, default=0, min_value=0, max_value=10_000),
        # Alternatively fire on this strategy's own Nth evaluation (state driven).
        ParameterSpec("fire_on_nth", ParameterKind.INT, default=-1, min_value=-1, max_value=10_000),
    )
)


def make_strategy(strategy_id: str, side: StrategyRole) -> type:
    base = BuyStrategy if side is StrategyRole.BUY else SellStrategy
    spec = StrategySpec(
        strategy_id=strategy_id,
        version="1.0.0",
        side=side,
        category="technical",
        sub_category="momentum",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=1,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=_SCHEMA,
        persistent_state_keys=("count",),
        methodology="Scripted test double: fires on a configured completed-bar count.",
        source="test fixture; not a trading strategy",
        execution_limitations=("test double only",),
        reason_codes=(ReasonCode.SIGNAL, ReasonCode.NO_SIGNAL),
    )

    def evaluate(
        self: object,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = spec.parameter_schema.validate(params)
        count = int(state.get("count", 0)) + 1  # type: ignore[call-overload]
        updates = {"count": count}
        bars = context.candles[CandleTimeframe.M1]
        fire_at, every, nth = (
            int(validated["fire_at"]),  # type: ignore[call-overload]
            int(validated["fire_every"]),  # type: ignore[call-overload]
            int(validated["fire_on_nth"]),  # type: ignore[call-overload]
        )
        version = context.version
        fire = (
            (fire_at >= 0 and version == fire_at)
            or (fire_at >= 0 and every > 0 and version > fire_at and (version - fire_at) % every == 0)
            or (nth >= 0 and count == nth)
        )
        if fire:
            return StrategyEvaluation.for_signal(
                spec, context.symbol, bars[-1].start,
                reference_price=bars[-1].close if side is StrategyRole.BUY else None,
                state_updates=updates,
            )
        return StrategyEvaluation.no_signal(
            spec, context.symbol, (ReasonCode.NO_SIGNAL,),
            bar_time=bars[-1].start, state_updates=updates,
        )

    name = "".join(part.title() for part in strategy_id.split("_")) + side.value.title()
    return type(name, (base,), {"spec": spec, "evaluate": evaluate})


#: Stable catalogue: three BUY and three SELL doubles with distinct ids.
STUBS: Mapping[str, type] = {
    **{
        sid: make_strategy(sid, StrategyRole.BUY)
        for sid in ("buy_alpha", "buy_bravo", "buy_charlie")
    },
    **{
        sid: make_strategy(sid, StrategyRole.SELL)
        for sid in ("sell_alpha", "sell_bravo", "sell_charlie")
    },
}


def build_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    for cls in STUBS.values():
        registry.register(cls)
    return registry


def price(value: str) -> Decimal:
    return Decimal(value)
