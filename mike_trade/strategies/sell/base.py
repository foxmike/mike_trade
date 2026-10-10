"""Shared, broker-neutral contracts for long-only SELL strategies.

SELL rules only identify when an owned long should be fully exited. They never
submit orders. The ordinary ``SellStrategy.evaluate`` SDK remains the public
trigger interface; ``SellRule`` supplies common data checks before delegating
to ``evaluate_trigger``. Position and minimum-hold checks are available as a
separate pure evaluator for the composition layer that has the confirmed
position snapshot.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import ClassVar, Mapping

from mike_trade.domain.signals import SignalAction
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import ManagementStatus, Position, PositionDirection
from mike_trade.market_data.context import MarketContext
from mike_trade.strategies.base import (
    ReasonCode,
    SellStrategy,
    StrategyEvaluation,
    StrategySpec,
    make_signal_id,
)

__all__ = [
    "SellRule",
    "ExitLongRecommendation",
    "ExitSource",
    "EXIT_PRIORITY",
    "evaluate_exit_eligibility",
    "min_hold_applies",
    "SellReasonCode",
]


class SellReasonCode(Enum):
    """SELL contract diagnostics, including generic evaluation disposition."""

    ELIGIBLE = "ELIGIBLE"
    NO_SIGNAL = "NO_SIGNAL"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    FLAT_POSITION = "FLAT_POSITION"
    UNMANAGED_POSITION = "UNMANAGED_POSITION"
    NON_POSITIVE_QUANTITY = "NON_POSITIVE_QUANTITY"
    NOT_LONG = "NOT_LONG"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"
    NO_DATA = "NO_DATA"
    STALE_DATA = "STALE_DATA"
    INCOMPLETE_DATA = "INCOMPLETE_DATA"
    NOT_WARMED = "NOT_WARMED"
    MINIMUM_HOLD = "MINIMUM_HOLD"


class ExitSource(Enum):
    """Origin of an exit request; only ordinary strategy exits use minimum hold."""

    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    HARD_RISK = "HARD_RISK"
    FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
    STRATEGY_SELL = "STRATEGY_SELL"


# Lower numbers win arbitration. Safety and operator initiated exits precede
# strategy opinions; Phase k.4 consumes this stable ordering.
EXIT_PRIORITY: Mapping[ExitSource, int] = MappingProxyType(
    {
        ExitSource.PROTECTIVE_STOP: 0,
        ExitSource.MANUAL_CLOSE: 1,
        ExitSource.HARD_RISK: 2,
        ExitSource.FORCED_LIQUIDATION: 3,
        ExitSource.STRATEGY_SELL: 4,
    }
)


def min_hold_applies(source: ExitSource) -> bool:
    """Return whether ``source`` must honor the ordinary strategy hold period."""
    if not isinstance(source, ExitSource):
        raise ValueError("source must be ExitSource")
    return source is ExitSource.STRATEGY_SELL


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, init=False)
class ExitLongRecommendation:
    """Advisory to exit the entire confirmed remainder of one managed long."""

    signal_id: str
    strategy_id: str
    version: str
    symbol: str
    position_cycle_id: str
    bar_time: datetime
    quantity: Decimal
    reason_codes: tuple[SellReasonCode, ...]
    action: SignalAction = SignalAction.EXIT_LONG

    def __init__(
        self,
        *,
        signal_id: str,
        strategy_id: str,
        version: str,
        position: Position,
        bar_time: datetime,
        reason_codes: tuple[SellReasonCode, ...],
    ) -> None:
        """Build from the managed position; quantity and cycle id are derived."""
        if not isinstance(position, Position):
            raise ValueError("position must be a Position")
        if position.management is not ManagementStatus.MANAGED or not position.cycle_id:
            raise ValueError("recommendations require an owned managed position")
        if position.quantity <= 0:
            raise ValueError("recommendations require a positive confirmed remainder")
        normalized_bar_time = _utc(bar_time, "bar_time")
        expected = make_signal_id(
            strategy_id, version, position.symbol, StrategyRole.SELL, normalized_bar_time
        )
        if signal_id != expected:
            raise ValueError("signal_id does not match strategy, symbol, side, and bar time")
        if not isinstance(reason_codes, tuple) or not reason_codes or any(
            not isinstance(code, SellReasonCode) for code in reason_codes
        ):
            raise ValueError("reason_codes must be a non-empty tuple of SellReasonCode")
        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "symbol", position.symbol)
        object.__setattr__(self, "position_cycle_id", position.cycle_id)
        object.__setattr__(self, "bar_time", normalized_bar_time)
        object.__setattr__(self, "quantity", position.quantity)
        object.__setattr__(self, "reason_codes", reason_codes)
        object.__setattr__(self, "action", SignalAction.EXIT_LONG)


def evaluate_exit_eligibility(
    *,
    position: Position | None,
    confirmed_remaining_quantity: Decimal | None,
    enabled: bool,
    context: MarketContext | None,
    spec: StrategySpec,
    source: ExitSource = ExitSource.STRATEGY_SELL,
    minimum_hold: timedelta = timedelta(0),
    evaluation_time: datetime | None = None,
    position_opened_at: datetime | None = None,
    direction: PositionDirection = PositionDirection.LONG,
) -> tuple[SellReasonCode, ...]:
    """Return eligibility diagnostics without consulting a wall clock.

    ``evaluation_time`` must be injected when minimum hold applies. The
    confirmed remainder comes from the engine's position/cycle snapshot; this
    function never guesses it from an order or requested exit size.
    Empty and non-empty ``reason_codes`` are not ambiguous: success is exactly
    ``(ELIGIBLE,)``; failure always starts with NO_SIGNAL or NOT_ELIGIBLE.
    """
    if not isinstance(spec, StrategySpec) or spec.side is not StrategyRole.SELL:
        raise ValueError("spec must describe a SELL strategy")
    if not isinstance(source, ExitSource):
        raise ValueError("source must be ExitSource")
    if not isinstance(minimum_hold, timedelta) or minimum_hold < timedelta(0):
        raise ValueError("minimum_hold must be a non-negative timedelta")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be bool")
    if not isinstance(direction, PositionDirection):
        raise ValueError("direction must be PositionDirection")

    failures: list[SellReasonCode] = []
    disposition = SellReasonCode.NOT_ELIGIBLE
    if not enabled:
        disposition = SellReasonCode.NO_SIGNAL
        failures.append(SellReasonCode.STRATEGY_DISABLED)
    if context is None:
        disposition = SellReasonCode.NO_SIGNAL
        failures.append(SellReasonCode.NO_DATA)
    elif context.freshness.stale:
        disposition = SellReasonCode.NO_SIGNAL
        failures.append(SellReasonCode.STALE_DATA)
    elif not context.completed_bars_only:
        disposition = SellReasonCode.NO_SIGNAL
        failures.append(SellReasonCode.INCOMPLETE_DATA)
    elif not context.feed_id or any(not context.candles.get(tf) for tf in spec.required_timeframes):
        disposition = SellReasonCode.NO_SIGNAL
        failures.append(SellReasonCode.NO_DATA)
    elif len(context.candles.get(spec.trigger_timeframe, ())) < spec.warmup_bars:
        disposition = SellReasonCode.NO_SIGNAL
        failures.append(SellReasonCode.NOT_WARMED)

    if position is None:
        failures.append(SellReasonCode.FLAT_POSITION)
    elif position.management is not ManagementStatus.MANAGED or position.cycle_id is None:
        failures.append(SellReasonCode.UNMANAGED_POSITION)
    if direction is not PositionDirection.LONG:
        failures.append(SellReasonCode.NOT_LONG)
    if confirmed_remaining_quantity is None:
        failures.append(SellReasonCode.NON_POSITIVE_QUANTITY)
    elif (
        isinstance(confirmed_remaining_quantity, bool)
        or not isinstance(confirmed_remaining_quantity, Decimal)
        or not confirmed_remaining_quantity.is_finite()
        or confirmed_remaining_quantity <= 0
    ):
        failures.append(SellReasonCode.NON_POSITIVE_QUANTITY)

    if (
        not failures
        and min_hold_applies(source)
        and minimum_hold > timedelta(0)
    ):
        if position is None or position_opened_at is None or evaluation_time is None:
            raise ValueError(
                "minimum-hold evaluation requires position_opened_at and evaluation_time"
            )
        elapsed = _utc(evaluation_time, "evaluation_time") - _utc(
            position_opened_at, "position_opened_at"
        )
        if elapsed < minimum_hold:
            failures.append(SellReasonCode.MINIMUM_HOLD)

    if failures:
        return (disposition, *tuple(dict.fromkeys(failures)))
    return (SellReasonCode.ELIGIBLE,)


class SellRule(SellStrategy):
    """SELL SDK adapter with shared context checks before pure trigger logic."""

    spec: ClassVar[StrategySpec]

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        spec = type(self).spec
        failure = self._context_failure(
            context, spec, type(self).required_bars(params)
        )
        if failure is not None:
            reason, _detail = failure
            return StrategyEvaluation.no_signal(
                spec,
                context.symbol,
                (reason,),
                bar_time=self._bar_time(context, spec),
            )
        return self.evaluate_trigger(context, params, state)

    @staticmethod
    def _bar_time(context: MarketContext, spec: StrategySpec) -> datetime | None:
        bars = context.candles.get(spec.trigger_timeframe, ())
        return bars[-1].start if bars else None

    @classmethod
    def _context_failure(
        cls,
        context: MarketContext | None,
        spec: StrategySpec,
        required_bars: int | None = None,
    ) -> tuple[ReasonCode, str] | None:
        if context is None:
            return (ReasonCode.INSUFFICIENT_DATA, "NO_DATA")
        if context.freshness.stale:
            return (ReasonCode.STALE_DATA, "STALE_DATA")
        if not context.completed_bars_only:
            return (ReasonCode.INSUFFICIENT_DATA, "INCOMPLETE_DATA")
        if not context.feed_id or any(
            not context.candles.get(tf) for tf in spec.required_timeframes
        ):
            return (ReasonCode.INSUFFICIENT_DATA, "NO_DATA")
        needed = spec.warmup_bars if required_bars is None else required_bars
        if len(context.candles.get(spec.trigger_timeframe, ())) < needed:
            return (ReasonCode.INSUFFICIENT_DATA, "NOT_WARMED")
        return None

    @abstractmethod
    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        """Implement only the pure SELL trigger calculation."""
