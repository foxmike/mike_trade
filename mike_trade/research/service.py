"""Research service: shared fetch, bounded cache, immutable context.

Mirrors the market-data shared-fetch and immutable-snapshot patterns: each
``(symbol, input key)`` pair is fetched once per refresh cycle and shared
across strategies and scenarios. Freshness gating mirrors the market-data
quality reason-code conventions (UPPER_SNAKE codes, fail-closed for mandatory
inputs).

Strategies receive only :class:`ResearchContext` and never the provider.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from mike_trade.research.ports import (
    MISSING_REASON_NOT_CONFIGURED,
    MISSING_REASON_PROVIDER_ERROR,
    MISSING_REASON_PROVIDER_RETURNED_NONE,
    ResearchDataProvider,
    ResearchRecord,
    ResearchRequirement,
    declare_research_requirement,
    missing_record,
)

__all__ = [
    "RESEARCH_MISSING_MANDATORY",
    "RESEARCH_STALE_MANDATORY",
    "ResearchPermission",
    "ResearchContext",
    "NullResearchProvider",
    "ResearchService",
    "declare_research_requirement",
    "VWAP_REFERENCE_REQUIREMENTS",
    "EXAMPLE_VALUE_REQUIREMENTS",
    "EXAMPLE_MOMENTUM_REQUIREMENTS",
    "EXAMPLE_RESEARCH_DECLARATIONS",
]

RESEARCH_MISSING_MANDATORY = "RESEARCH_MISSING_MANDATORY"
RESEARCH_STALE_MANDATORY = "RESEARCH_STALE_MANDATORY"


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _strategy_key(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError("strategy_key must be a non-empty string")
    return normalized


def _symbol(value: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError("symbol must be a non-empty string")
    return normalized


@dataclass(frozen=True, slots=True)
class ResearchPermission:
    """Signal permission for one (strategy, symbol) pair."""

    strategy_key: str
    symbol: str
    allowed: bool
    reason_code: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_key", _strategy_key(self.strategy_key))
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be bool")
        if self.reason_code is not None:
            if not isinstance(self.reason_code, str) or not self.reason_code.strip():
                raise ValueError("reason_code must be a non-empty string or None")
            object.__setattr__(self, "reason_code", self.reason_code.strip())
        if self.allowed and self.reason_code is not None:
            raise ValueError("allowed permissions must have reason_code=None")


@dataclass(frozen=True, slots=True)
class ResearchContext:
    """Immutable snapshot handed to strategies for evaluation.

    Exposes records and per-strategy signal permission only. It holds no
    provider, network client, or mutable handle.
    """

    as_of: datetime
    records: tuple[ResearchRecord, ...] = ()
    requirements: tuple[tuple[str, ResearchRequirement], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        records = tuple(self.records)
        for record in records:
            if not isinstance(record, ResearchRecord):
                raise ValueError("records must contain ResearchRecord objects")
        object.__setattr__(self, "records", records)
        normalized: list[tuple[str, ResearchRequirement]] = []
        for pair in tuple(self.requirements):
            try:
                key, requirement = pair
            except (TypeError, ValueError):
                raise ValueError(
                    "requirements must contain (strategy_key, ResearchRequirement) pairs"
                ) from None
            if not isinstance(requirement, ResearchRequirement):
                raise ValueError("requirements must contain ResearchRequirement objects")
            normalized.append((_strategy_key(key), requirement))
        object.__setattr__(self, "requirements", tuple(normalized))

    # -- record access --------------------------------------------------

    def get(self, symbol: str, input_key: str) -> ResearchRecord | None:
        """Return the record for ``(symbol, input_key)`` or None."""
        symbol = _symbol(symbol)
        key = input_key.strip().lower() if isinstance(input_key, str) else ""
        for record in self.records:
            if record.symbol == symbol and record.input_key == key:
                return record
        return None

    def is_missing(self, symbol: str, input_key: str) -> bool:
        """True when no usable value exists for ``(symbol, input_key)``."""
        record = self.get(symbol, input_key)
        return record is None or record.missing

    @property
    def symbols(self) -> frozenset[str]:
        """Symbols covered by this context's requirements."""
        return frozenset(
            symbol for _, requirement in self.requirements for symbol in requirement.symbols
        ) | frozenset(record.symbol for record in self.records)

    def records_for(self, symbol: str) -> Mapping[str, ResearchRecord]:
        """Immutable mapping of input_key -> record for ``symbol``."""
        symbol = _symbol(symbol)
        return MappingProxyType(
            {record.input_key: record for record in self.records if record.symbol == symbol}
        )

    # -- permission -----------------------------------------------------

    def _applicable(self, strategy_key: str, symbol: str) -> tuple[ResearchRequirement, ...]:
        strategy_key = _strategy_key(strategy_key)
        symbol = _symbol(symbol)
        return tuple(
            requirement
            for key, requirement in self.requirements
            if key == strategy_key and requirement.applies_to(symbol)
        )

    def permission(self, strategy_key: str, symbol: str) -> ResearchPermission:
        """Evaluate signal permission for one (strategy, symbol) pair."""
        strategy_key = _strategy_key(strategy_key)
        symbol = _symbol(symbol)
        for requirement in self._applicable(strategy_key, symbol):
            record = self.get(symbol, requirement.input_key)
            if record is None or record.missing:
                if requirement.mandatory:
                    return ResearchPermission(strategy_key, symbol, False, RESEARCH_MISSING_MANDATORY)
                continue
            age = (self.as_of - record.publication_ts).total_seconds()
            if age > requirement.max_age_seconds:
                if requirement.mandatory:
                    return ResearchPermission(strategy_key, symbol, False, RESEARCH_STALE_MANDATORY)
                continue
        return ResearchPermission(strategy_key, symbol, True, None)

    def signal_permitted(self, strategy_key: str, symbol: str) -> bool:
        """True when evaluation may emit a signal for (strategy, symbol)."""
        return self.permission(strategy_key, symbol).allowed

    def reason_code(self, strategy_key: str, symbol: str) -> str | None:
        """UPPER_SNAKE reason code blocking signals, or None when permitted."""
        return self.permission(strategy_key, symbol).reason_code

    def permissions(self) -> tuple[ResearchPermission, ...]:
        """Permission for every registered (strategy, applicable symbol) pair."""
        seen: set[tuple[str, str]] = set()
        ordered: list[ResearchPermission] = []
        symbols = sorted({record.symbol for record in self.records})
        strategies = sorted({key for key, _ in self.requirements})
        for key in strategies:
            applicable_symbols = sorted(
                {
                    symbol
                    for symbol in symbols
                    if any(
                        req.applies_to(symbol)
                        for k, req in self.requirements
                        if k == key
                    )
                }
            )
            for symbol in applicable_symbols:
                if (key, symbol) not in seen:
                    seen.add((key, symbol))
                    ordered.append(self.permission(key, symbol))
        return tuple(ordered)


class NullResearchProvider:
    """Default provider: no paid source, no network, no files.

    Every fetch returns None, which the service surfaces as a missing record.
    Strategies declaring no inputs are unaffected; strategies with mandatory
    inputs are fail-closed via reason codes.
    """

    def fetch(
        self, symbol: str, input_key: str, as_of: datetime
    ) -> ResearchRecord | None:
        return None


class ResearchService:
    """Build research context for strategy evaluation with shared fetch.

    Only inputs declared by registered strategies for the watched symbols are
    requested. Each distinct ``(symbol, input key)`` is fetched once per
    :meth:`refresh` cycle and shared across strategies and scenarios. Records
    are retained in a bounded LRU cache.
    """

    def __init__(
        self,
        provider: ResearchDataProvider | None = None,
        *,
        clock: Any,
        max_cache_entries: int = 512,
    ) -> None:
        if provider is None:
            provider = NullResearchProvider()
        if not callable(getattr(provider, "fetch", None)):
            raise TypeError("provider must expose fetch(symbol, input_key, as_of)")
        if not callable(getattr(clock, "now", None)):
            raise TypeError("clock must expose now()")
        if (
            isinstance(max_cache_entries, bool)
            or not isinstance(max_cache_entries, int)
            or max_cache_entries < 1
        ):
            raise ValueError("max_cache_entries must be a positive integer")
        self._provider = provider
        self._clock = clock
        self._max_cache_entries = max_cache_entries
        self._registered: dict[str, tuple[ResearchRequirement, ...]] = {}
        self._cache: OrderedDict[tuple[str, str], ResearchRecord] = OrderedDict()

    @property
    def provider(self) -> ResearchDataProvider:
        """The configured provider (never exposed to strategies)."""
        return self._provider

    @property
    def registered_strategies(self) -> frozenset[str]:
        return frozenset(self._registered)

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def max_cache_entries(self) -> int:
        return self._max_cache_entries

    def register_strategy(
        self, strategy_key: str, requirements: Iterable[ResearchRequirement]
    ) -> None:
        """Register the research inputs one strategy needs."""
        strategy_key = _strategy_key(strategy_key)
        if requirements is None:
            raise ValueError("requirements must be an iterable")
        normalized: list[ResearchRequirement] = []
        for requirement in tuple(requirements):
            if not isinstance(requirement, ResearchRequirement):
                raise ValueError("requirements must contain ResearchRequirement objects")
            normalized.append(requirement)
        self._registered[strategy_key] = tuple(normalized)

    # Familiar alias for composition code.
    register = register_strategy

    def _store(self, record: ResearchRecord) -> None:
        key = (record.symbol, record.input_key)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = record
        while len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)

    def refresh(
        self,
        symbols: Iterable[str],
        *,
        strategy_keys: Iterable[str] | None = None,
    ) -> ResearchContext:
        """Fetch declared inputs once per (symbol, key) and build a context."""
        if isinstance(symbols, (str, bytes)):
            raise TypeError("symbols must be an iterable of symbol strings")
        watched = sorted({_symbol(item) for item in tuple(symbols)})
        if strategy_keys is None:
            scope = sorted(self._registered)
        else:
            if isinstance(strategy_keys, (str, bytes)):
                raise TypeError("strategy_keys must be an iterable of strategy keys")
            scope = sorted({_strategy_key(item) for item in tuple(strategy_keys)})
            unknown = [key for key in scope if key not in self._registered]
            if unknown:
                raise KeyError(f"unknown strategies: {', '.join(unknown)}")
        as_of = _utc(self._clock.now(), "clock")

        needed: dict[tuple[str, str], ResearchRequirement] = {}
        for key in scope:
            for requirement in self._registered[key]:
                for symbol in watched:
                    if requirement.applies_to(symbol):
                        needed.setdefault((symbol, requirement.input_key), requirement)

        fetched: dict[tuple[str, str], ResearchRecord] = {}
        for symbol, input_key in sorted(needed):
            try:
                record = self._provider.fetch(symbol, input_key, as_of)
            except Exception:
                record = None
                reason = MISSING_REASON_PROVIDER_ERROR
                source = "research-service"
            else:
                reason = MISSING_REASON_PROVIDER_RETURNED_NONE
                source = "research-service"
            if record is None:
                record = missing_record(
                    input_key, symbol, reason=reason, as_of=as_of, source=source
                )
            elif not isinstance(record, ResearchRecord):
                raise TypeError(
                    f"provider must return ResearchRecord or None; got {type(record).__name__}"
                )
            fetched[(symbol, input_key)] = record
            self._store(record)

        ordered_records = tuple(fetched[key] for key in sorted(fetched))
        ordered_requirements = tuple(
            (key, requirement)
            for key in scope
            for requirement in self._registered[key]
        )
        return ResearchContext(
            as_of=as_of, records=ordered_records, requirements=ordered_requirements
        )

    # Alias emphasizing the snapshot nature of the return value.
    build_context = refresh


# ---------------------------------------------------------------------------
# Reference declarations
# ---------------------------------------------------------------------------

#: ``vwap_reference`` declares no research inputs: it evaluates purely on
#: market-data candles/quotes and is unaffected by research configuration.
VWAP_REFERENCE_REQUIREMENTS: tuple[ResearchRequirement, ...] = ()

#: Example: a value-style reference strategy requiring a slow-moving
#: fundamental input for every watched symbol plus an optional overlay.
EXAMPLE_VALUE_REQUIREMENTS: tuple[ResearchRequirement, ...] = (
    declare_research_requirement("analyst_fair_value", mandatory=True, max_age_seconds=7 * 86400),
    declare_research_requirement("news_sentiment", mandatory=False, max_age_seconds=86400),
)

#: Example: a momentum-style reference strategy scoping a mandatory input to
#: one symbol and an optional input to all watched symbols.
EXAMPLE_MOMENTUM_REQUIREMENTS: tuple[ResearchRequirement, ...] = (
    declare_research_requirement(
        "earnings_quality", mandatory=True, max_age_seconds=30 * 86400, symbols=["AAPL"]
    ),
    declare_research_requirement("news_sentiment", mandatory=False, max_age_seconds=3600),
)

#: Registry of reference declarations keyed by reference strategy name.
EXAMPLE_RESEARCH_DECLARATIONS: Mapping[str, tuple[ResearchRequirement, ...]] = MappingProxyType(
    {
        "vwap_reference": VWAP_REFERENCE_REQUIREMENTS,
        "value_reference": EXAMPLE_VALUE_REQUIREMENTS,
        "momentum_reference": EXAMPLE_MOMENTUM_REQUIREMENTS,
    }
)

#: Backwards-compatible alias for the reference declaration registry.
REFERENCE_DECLARATIONS = EXAMPLE_RESEARCH_DECLARATIONS
