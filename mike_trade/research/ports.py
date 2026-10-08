"""Optional research data port and immutable research records.

Strategies never touch a provider directly; the research service fetches
through :class:`ResearchDataProvider` and hands strategies an immutable
:class:`~mike_trade.research.service.ResearchContext` snapshot.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ResearchRecord",
    "ResearchRequirement",
    "ResearchDataProvider",
    "declare_research_requirement",
    "missing_record",
    "MISSING_REASON_PROVIDER_RETURNED_NONE",
    "MISSING_REASON_PROVIDER_ERROR",
    "MISSING_REASON_NOT_CONFIGURED",
]

MISSING_REASON_PROVIDER_RETURNED_NONE = "PROVIDER_RETURNED_NONE"
MISSING_REASON_PROVIDER_ERROR = "PROVIDER_ERROR"
MISSING_REASON_NOT_CONFIGURED = "NOT_CONFIGURED"

_INPUT_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _symbol(value: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError("symbol must be a non-empty string")
    if any(ch.isspace() for ch in normalized):
        raise ValueError("symbol must not contain whitespace")
    return normalized


def _input_key(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if _INPUT_KEY_RE.fullmatch(normalized) is None:
        raise ValueError(f"input_key must be lowercase snake_case; got {value!r}")
    return normalized


def _source(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError("source must be a non-empty string")
    return normalized


def _freeze(value: Any) -> Any:
    """Recursively freeze a compact research value or reject mutables."""
    if value is None or isinstance(value, (str, bytes, int, bool, Decimal, datetime, Enum)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, dict):
        return tuple((_freeze(k), _freeze(v)) for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if is_dataclass(value):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is not None and params.frozen:
            for item in fields(value):
                _freeze(getattr(value, item.name))
            return value
    raise TypeError(f"research values must be immutable; got {type(value).__name__}")


def _max_age(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number of seconds")
    result = float(value)
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


@dataclass(frozen=True, slots=True)
class ResearchRecord:
    """One immutable research input value for a single symbol.

    ``publication_ts`` is the provider's publication instant, ``observed_ts``
    is when the fact was observed in the world, and ``ingestion_ts`` is when
    this system ingested it. All three are timezone-aware UTC.
    ``max_age_seconds`` is the provider's freshness assertion; each strategy
    requirement carries its own maximum age that governs signal permission.
    When ``missing`` is True the record carries no usable value and
    ``missing_reason`` holds an UPPER_SNAKE reason code.
    Licensing/usage metadata (``license_id``, ``usage``, ``redistributable``)
    travels with every record so downstream consumers honor redistribution
    terms without contacting the provider.
    """

    input_key: str
    symbol: str
    value: Any
    source: str
    publication_ts: datetime
    observed_ts: datetime
    ingestion_ts: datetime
    revision_id: str
    revision_number: int
    max_age_seconds: float
    missing: bool = False
    missing_reason: str | None = None
    license_id: str = "unknown"
    usage: str = "internal-research-only"
    redistributable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_key", _input_key(self.input_key))
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "value", _freeze(self.value))
        object.__setattr__(self, "source", _source(self.source))
        object.__setattr__(self, "publication_ts", _utc(self.publication_ts, "publication_ts"))
        object.__setattr__(self, "observed_ts", _utc(self.observed_ts, "observed_ts"))
        object.__setattr__(self, "ingestion_ts", _utc(self.ingestion_ts, "ingestion_ts"))
        if not isinstance(self.revision_id, str) or not self.revision_id.strip():
            raise ValueError("revision_id must be a non-empty string")
        object.__setattr__(self, "revision_id", self.revision_id.strip())
        if isinstance(self.revision_number, bool) or not isinstance(self.revision_number, int):
            raise ValueError("revision_number must be an int")
        if self.revision_number < 0:
            raise ValueError("revision_number must be >= 0")
        object.__setattr__(self, "max_age_seconds", _max_age(self.max_age_seconds, "max_age_seconds"))
        if not isinstance(self.missing, bool):
            raise ValueError("missing must be bool")
        if self.missing:
            if not isinstance(self.missing_reason, str) or not self.missing_reason.strip():
                raise ValueError("missing records require a missing_reason code")
            object.__setattr__(self, "missing_reason", self.missing_reason.strip())
        else:
            if self.missing_reason is not None:
                raise ValueError("non-missing records must have missing_reason=None")
        if not isinstance(self.license_id, str) or not self.license_id.strip():
            raise ValueError("license_id must be a non-empty string")
        object.__setattr__(self, "license_id", self.license_id.strip())
        if not isinstance(self.usage, str) or not self.usage.strip():
            raise ValueError("usage must be a non-empty string")
        object.__setattr__(self, "usage", self.usage.strip())
        if not isinstance(self.redistributable, bool):
            raise ValueError("redistributable must be bool")

    @property
    def is_missing(self) -> bool:
        """Alias for the missing flag."""
        return self.missing

    def age_seconds(self, as_of: datetime) -> float:
        """Age in seconds measured from publication to ``as_of`` (UTC)."""
        return (_utc(as_of, "as_of") - self.publication_ts).total_seconds()


def missing_record(
    input_key: str,
    symbol: str,
    *,
    reason: str,
    as_of: datetime,
    source: str = "null",
    max_age_seconds: float = 3600.0,
    license_id: str = "unknown",
    usage: str = "internal-research-only",
) -> ResearchRecord:
    """Build a missing-flagged record stamped at ``as_of`` (UTC)."""
    now = _utc(as_of, "as_of")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    return ResearchRecord(
        input_key=_input_key(input_key),
        symbol=_symbol(symbol),
        value=None,
        source=_source(source),
        publication_ts=now,
        observed_ts=now,
        ingestion_ts=now,
        revision_id="missing",
        revision_number=0,
        max_age_seconds=_max_age(max_age_seconds, "max_age_seconds"),
        missing=True,
        missing_reason=reason.strip(),
        license_id=license_id,
        usage=usage,
        redistributable=False,
    )


@dataclass(frozen=True, slots=True)
class ResearchRequirement:
    """A strategy's declaration for one research input.

    ``mandatory`` True means a missing or stale value blocks signals for the
    applicable symbols; False means the input is optional and surfaced as
    missing without blocking. ``max_age_seconds`` is the strategy's maximum
    acceptable age measured from the record's ``publication_ts``.
    ``symbols`` holds the per-symbol applicability as an uppercase frozenset;
    an empty frozenset means the requirement applies to every watched symbol.
    """

    input_key: str
    mandatory: bool = True
    max_age_seconds: float = 86400.0
    symbols: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_key", _input_key(self.input_key))
        if not isinstance(self.mandatory, bool):
            raise ValueError("mandatory must be bool")
        object.__setattr__(
            self, "max_age_seconds", _max_age(self.max_age_seconds, "max_age_seconds")
        )
        raw = self.symbols
        if raw is None:
            object.__setattr__(self, "symbols", frozenset())
            return
        if isinstance(raw, (str, bytes)):
            raise TypeError("symbols must be an iterable of symbol strings")
        try:
            items = tuple(raw)
        except TypeError:
            raise TypeError("symbols must be an iterable of symbol strings") from None
        object.__setattr__(self, "symbols", frozenset(_symbol(item) for item in items))

    @property
    def required(self) -> bool:
        """Alias: mandatory inputs are required for signal permission."""
        return self.mandatory

    @property
    def optional(self) -> bool:
        """Alias: True when this input never blocks signal permission."""
        return not self.mandatory

    def applies_to(self, symbol: str) -> bool:
        """Return True when this requirement applies to ``symbol``."""
        normalized = _symbol(symbol)
        return not self.symbols or normalized in self.symbols


def declare_research_requirement(
    input_key: str,
    *,
    mandatory: bool = True,
    max_age_seconds: float = 86400.0,
    symbols: Iterable[str] | None = None,
) -> ResearchRequirement:
    """Declare one research input a strategy needs.

    Example::

        declare_research_requirement("analyst_rating", mandatory=True,
                                     max_age_seconds=86400)
        declare_research_requirement("news_sentiment", mandatory=False,
                                     max_age_seconds=3600, symbols=["AAPL"])

    Pass ``symbols`` to restrict the requirement to specific symbols; omit it
    (or pass None/empty) to apply to every watched symbol. Returns an
    immutable :class:`ResearchRequirement`.
    """
    return ResearchRequirement(
        input_key=input_key,
        mandatory=mandatory,
        max_age_seconds=max_age_seconds,
        symbols=frozenset(symbols) if symbols is not None else frozenset(),
    )


@runtime_checkable
class ResearchDataProvider(Protocol):
    """Provider-facing port consumed only by the research service.

    ``fetch`` returns the immutable record for ``(symbol, input_key)`` as of
    ``as_of``, or None when the provider has no value. Providers must not
    require network access for the null/default path, must never be handed to
    strategies, and must return timezone-aware UTC timestamps on records.
    """

    def fetch(
        self, symbol: str, input_key: str, as_of: datetime
    ) -> ResearchRecord | None:
        """Return the record for ``(symbol, input_key)`` or None if missing."""
        ...
