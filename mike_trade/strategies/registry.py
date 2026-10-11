"""Explicit, trusted registry of Python strategy classes.

Strategy classes are registered by the composition root with a plain call
(``registry.register(MyBuyStrategy)``) after importing them normally.  There is
no dynamic code evaluation, no entry-point or path-based plugin discovery, and
no rules DSL.  One registry holds every side, keyed by ``(strategy_id,
version)``; BUY, SELL, SHORT and COVER entries are independent and never require a partner.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping

from mike_trade.domain.errors import MikeTradeError
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.base import (
    BuyStrategy,
    CoverStrategy,
    SellStrategy,
    ShortStrategy,
    StrategySpec,
)

__all__ = ["StrategyRegistryError", "StrategyRegistry"]

StrategyClass = (
    type[BuyStrategy] | type[SellStrategy] | type[ShortStrategy] | type[CoverStrategy]
)

_SIDE_BASES: dict[StrategyRole, StrategyClass] = {
    StrategyRole.BUY: BuyStrategy,
    StrategyRole.SELL: SellStrategy,
    StrategyRole.SHORT: ShortStrategy,
    StrategyRole.COVER: CoverStrategy,
}


class StrategyRegistryError(MikeTradeError):
    """Registration or lookup failed.

    ``code`` is one of ``duplicate_registration``, ``invalid_strategy`` or
    ``not_found``.
    """


class StrategyRegistry:
    """Maps ``(strategy_id, version)`` to a registered strategy class."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], StrategyClass] = {}

    def register(self, strategy_cls: StrategyClass) -> StrategyClass:
        """Register a concrete BUY, SELL, SHORT or COVER strategy subclass.

        Raises ``StrategyRegistryError`` for an invalid class or a duplicate
        ``(strategy_id, version)``.  Returns the class unchanged.
        """
        if not inspect.isclass(strategy_cls) or not issubclass(
            strategy_cls, (BuyStrategy, SellStrategy, ShortStrategy, CoverStrategy)
        ):
            raise StrategyRegistryError(
                f"{strategy_cls!r} is not a BuyStrategy, SellStrategy, ShortStrategy or CoverStrategy subclass",
                code="invalid_strategy",
            )
        if inspect.isabstract(strategy_cls):
            raise StrategyRegistryError(
                f"{strategy_cls.__name__} is abstract and cannot be registered",
                code="invalid_strategy",
            )
        spec = getattr(strategy_cls, "spec", None)
        if not isinstance(spec, StrategySpec):
            raise StrategyRegistryError(
                f"{strategy_cls.__name__} must define a StrategySpec as `spec`",
                code="invalid_strategy",
            )
        if not issubclass(strategy_cls, _SIDE_BASES[spec.side]):
            raise StrategyRegistryError(
                f"{strategy_cls.__name__} spec side {spec.side.value} does not match its base",
                code="invalid_strategy",
            )
        key = (spec.strategy_id, spec.version)
        if key in self._entries:
            raise StrategyRegistryError(
                f"strategy {spec.strategy_id} {spec.version} is already registered",
                code="duplicate_registration",
                context={"strategy_id": spec.strategy_id, "version": spec.version},
            )
        self._entries[key] = strategy_cls
        return strategy_cls

    def get(self, strategy_id: str, version: str | None = None) -> StrategyClass:
        """Return the pinned ``version``, or the highest semantic version if omitted."""
        if version is not None:
            try:
                return self._entries[(strategy_id, version)]
            except KeyError:
                raise StrategyRegistryError(
                    f"strategy {strategy_id} {version} is not registered",
                    code="not_found",
                    context={"strategy_id": strategy_id, "version": version},
                ) from None
        candidates = [cls for (sid, _), cls in self._entries.items() if sid == strategy_id]
        if not candidates:
            raise StrategyRegistryError(
                f"strategy {strategy_id} is not registered",
                code="not_found",
                context={"strategy_id": strategy_id},
            )
        return max(candidates, key=lambda cls: cls.spec.version_key)

    def list_strategies(self, side: StrategyRole | None = None) -> tuple[StrategyClass, ...]:
        """Return registered classes, optionally for one side, ordered by id then version."""
        if side is not None and not isinstance(side, StrategyRole):
            raise ValueError(f"side must be StrategyRole or None; got {type(side).__name__}")
        selected = [cls for cls in self._entries.values() if side is None or cls.spec.side is side]
        return tuple(sorted(selected, key=lambda cls: (cls.spec.strategy_id, cls.spec.version_key)))

    def validate_parameters(
        self, strategy_id: str, params: Mapping[str, object], version: str | None = None
    ) -> Mapping[str, object]:
        """Validate ``params`` against the strategy spec's schema (delegated)."""
        return self.get(strategy_id, version).spec.parameter_schema.validate(params)
