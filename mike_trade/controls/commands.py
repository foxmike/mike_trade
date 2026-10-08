"""Operator control command vocabulary: types, the command record, and errors.

A ``ControlCommand`` is the operator's request; it carries no behaviour.  The
CLI submits it to the durable inbox (``mike_trade.controls.inbox``) and the
engine consumes it.  Commands fall into three categories:

* STATE: tighten or relax entry permission (pause, disable entries).
* CONFIG: apply a safe, audited configuration mutation.
* EXECUTABLE: act on positions or orders.  Later phases implement the handlers;
  this phase only defines and gates them.

``loosens_risk`` marks every command that can widen what the system may do; on
a LIVE scope those need explicit operator confirmation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import MikeTradeError
from mike_trade.persistence.journal import JournalPayloadError, canonical_payload_json

__all__ = [
    "CommandCategory",
    "ControlCommandType",
    "ControlCommand",
    "CommandStatus",
    "CommandOutcome",
    "ControlCommandError",
    "IdempotencyConflictError",
    "LiveConfirmationRequiredError",
    "EngineOfflineError",
    "CommandExpiredError",
    "UnknownCommandTypeError",
]

_MAX_IDEMPOTENCY_KEY_LENGTH = 128
_COMMAND_ENVIRONMENTS = frozenset(
    {AccountEnvironment.LIVE, AccountEnvironment.PAPER, AccountEnvironment.SHADOW}
)


class ControlCommandError(MikeTradeError):
    """Base class for operator control command failures."""


class IdempotencyConflictError(ControlCommandError):
    """An idempotency key was reused with a different command type or payload."""


class LiveConfirmationRequiredError(ControlCommandError):
    """A risk-loosening command on a LIVE scope was submitted without confirmation."""


class EngineOfflineError(ControlCommandError):
    """An executable command was refused because the engine is not online."""


class CommandExpiredError(ControlCommandError):
    """An executable command was already past its expiry when submitted."""


class UnknownCommandTypeError(ControlCommandError):
    """A command type name is not part of the control vocabulary."""


class CommandCategory(StrEnum):
    STATE = "STATE"
    CONFIG = "CONFIG"
    EXECUTABLE = "EXECUTABLE"


class ControlCommandType(StrEnum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    DISABLE_ENTRIES = "DISABLE_ENTRIES"
    ENABLE_ENTRIES = "ENABLE_ENTRIES"
    APPLY_CONFIG_MUTATION = "APPLY_CONFIG_MUTATION"
    CLOSE_POSITION = "CLOSE_POSITION"
    CLOSE_ALL = "CLOSE_ALL"
    FLATTEN = "FLATTEN"
    CANCEL_ORDER = "CANCEL_ORDER"
    ADOPT_POSITION = "ADOPT_POSITION"
    RECONCILE_NOW = "RECONCILE_NOW"
    RESET_ENTRY_LOCKOUT = "RESET_ENTRY_LOCKOUT"
    CLEAR_COOLDOWN = "CLEAR_COOLDOWN"

    @classmethod
    def parse(cls, value: object) -> ControlCommandType:
        """Return the member for ``value`` or raise ``UnknownCommandTypeError``."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        raise UnknownCommandTypeError(
            f"unknown control command type: {value!r}",
            code="unknown_command_type",
            context={"command_type": repr(value)},
        )

    @property
    def category(self) -> CommandCategory:
        return _CATEGORY_BY_TYPE[self]

    @property
    def loosens_risk(self) -> bool:
        """True unless the command can only make the system more conservative."""
        return self not in _TIGHTENING_TYPES


_CATEGORY_BY_TYPE: dict[ControlCommandType, CommandCategory] = {
    ControlCommandType.PAUSE: CommandCategory.STATE,
    ControlCommandType.RESUME: CommandCategory.STATE,
    ControlCommandType.DISABLE_ENTRIES: CommandCategory.STATE,
    ControlCommandType.ENABLE_ENTRIES: CommandCategory.STATE,
    ControlCommandType.APPLY_CONFIG_MUTATION: CommandCategory.CONFIG,
    ControlCommandType.CLOSE_POSITION: CommandCategory.EXECUTABLE,
    ControlCommandType.CLOSE_ALL: CommandCategory.EXECUTABLE,
    ControlCommandType.FLATTEN: CommandCategory.EXECUTABLE,
    ControlCommandType.CANCEL_ORDER: CommandCategory.EXECUTABLE,
    ControlCommandType.ADOPT_POSITION: CommandCategory.EXECUTABLE,
    ControlCommandType.RECONCILE_NOW: CommandCategory.EXECUTABLE,
    ControlCommandType.RESET_ENTRY_LOCKOUT: CommandCategory.EXECUTABLE,
    ControlCommandType.CLEAR_COOLDOWN: CommandCategory.EXECUTABLE,
}
_TIGHTENING_TYPES = frozenset({ControlCommandType.PAUSE, ControlCommandType.DISABLE_ENTRIES})

if set(_CATEGORY_BY_TYPE) != set(ControlCommandType):  # pragma: no cover - import-time guard
    raise RuntimeError("every ControlCommandType needs a category")


class CommandStatus(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class CommandOutcome:
    """The current decision on a command; ``decided_at`` is None while PENDING."""

    command_id: str
    status: CommandStatus
    detail: str
    decided_at: datetime | None


def _require_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a UTC-aware datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC (zero offset)")


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True, kw_only=True)
class ControlCommand:
    """One operator request, immutable once built.

    ``payload`` is normalised to canonical JSON form (``Decimal`` becomes a
    string, floats are refused) and ``payload_sha256`` is the SHA-256 of that
    canonical JSON, so equal requests always hash equally.
    """

    command_id: str = field(default_factory=lambda: str(uuid4()))
    idempotency_key: str
    account_scope_id: str
    environment: AccountEnvironment
    command_type: ControlCommandType
    payload: Mapping[str, Any] = field(default_factory=dict, hash=False)
    actor: str
    reason: str
    live_confirmed: bool = False
    created_at: datetime
    expires_at: datetime | None = None
    payload_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.command_id, "command_id")
        _require_text(self.idempotency_key, "idempotency_key")
        if len(self.idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError(
                f"idempotency_key must be at most {_MAX_IDEMPOTENCY_KEY_LENGTH} characters"
            )
        _require_text(self.account_scope_id, "account_scope_id")
        if self.account_scope_id != self.account_scope_id.strip():
            raise ValueError("account_scope_id must not have surrounding whitespace")
        if self.environment not in _COMMAND_ENVIRONMENTS:
            raise ValueError("environment must be LIVE, PAPER, or SHADOW")
        object.__setattr__(self, "command_type", ControlCommandType.parse(self.command_type))
        _require_text(self.actor, "actor")
        _require_text(self.reason, "reason")
        if not isinstance(self.live_confirmed, bool):
            raise TypeError("live_confirmed must be bool")
        _require_utc(self.created_at, "created_at")
        if self.expires_at is not None:
            _require_utc(self.expires_at, "expires_at")
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must be after created_at")
        try:
            canonical = canonical_payload_json(self.payload)
        except JournalPayloadError as exc:
            raise ValueError(f"payload must be JSON-serializable: {exc}") from exc
        object.__setattr__(self, "payload", MappingProxyType(json.loads(canonical)))
        object.__setattr__(
            self, "payload_sha256", hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
