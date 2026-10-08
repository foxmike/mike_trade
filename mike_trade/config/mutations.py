"""Safe, atomic, audited operator configuration mutations.

Single path for operator configuration changes. Operates on the raw YAML
mapping (schema_version 1 key names) and never imports the typed config
models/loader layers; validation is injected as a callable.

Design choices (4):
1. No-op requests (every value already equal) skip validation, safety
   checks, file writes, and audit recording, and return an unrecorded
   audit entry with empty changed_paths and identical before/after hashes.
2. Secret matching is case-insensitive substring on key names; any effective
   change whose leaf key is secret-like, or whose new mapping value contains
   a secret-like nested key, is refused. Audit values are scrubbed
   recursively so nested secret-like keys inside recorded values become '***'.
3. ``shadow_scenarios`` list items are addressed by name
   (``shadow_scenarios[name].field``); setting ``shadow_scenarios[name]`` to
   a mapping appends a new scenario, removing an absent element raises
   MutationPathError, and new symbols/strategies must be set whole (nested
   parents must already exist). Numeric stop-policy edits on exposed symbols
   append a forward-only note to the recorded audit reason.
4. ``live_confirmed`` echoes ``request.confirm_live``; the unknown-exposure
   rule is evaluated on effective (post-no-op-filter) changes only; naive
   clock values are assumed UTC and aware values are converted to UTC.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigMutationError(Exception):
    """Base exception for configuration mutation failures."""


class MutationPathError(ConfigMutationError):
    """Raised for unknown parent paths or attempts to change schema_version."""


class ConcurrentConfigModificationError(ConfigMutationError):
    """Raised when expected_file_sha256 does not match the file on disk."""


class SecretMutationRefusedError(ConfigMutationError):
    """Raised when a mutation introduces a secret-like key."""


class UnsafeConfigMutationError(ConfigMutationError):
    """Raised when a safety rule refuses a mutation."""

    def __init__(
        self,
        reasons: Sequence[str] | str,
        message: str | None = None,
    ) -> None:
        if isinstance(reasons, str):
            reasons = [reasons]
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__(message if message is not None else "; ".join(self.reasons))


class LiveEnableRefusedError(UnsafeConfigMutationError):
    """Raised when live trading would be enabled without confirm_live."""


class ConfigMutationValidationError(ConfigMutationError):
    """Wraps an exception raised by the injected validate callable."""

    def __init__(self, message: str, *, original: BaseException | None = None) -> None:
        self.original = original
        super().__init__(message)


class ConfigAuditFailedError(ConfigMutationError):
    """Raised when the audit sink fails; the file is restored first."""

    def __init__(self, message: str, *, original: BaseException | None = None) -> None:
        self.original = original
        super().__init__(message)


# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------


class _RemoveSentinel:
    __slots__ = ()

    def __repr__(self) -> str:
        return "REMOVE"


REMOVE = _RemoveSentinel()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigMutation:
    path: str
    value: Any

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("path must be a non-empty string")


@dataclass(frozen=True, slots=True)
class MutationRequest:
    mutations: tuple[ConfigMutation, ...]
    actor: str
    reason: str
    expected_file_sha256: str | None = None
    confirm_live: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "mutations", tuple(self.mutations))
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("actor must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        for mutation in self.mutations:
            if not isinstance(mutation, ConfigMutation):
                raise TypeError("mutations must contain ConfigMutation items")


@dataclass(frozen=True, slots=True)
class OpenExposure:
    symbol: str
    account_mode: str
    scenario: str | None = None
    sell_strategy: str | None = None
    has_active_protection: bool = False
    has_unresolved_orders: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    positions: tuple[OpenExposure, ...] = ()
    known: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", tuple(self.positions))


@dataclass(frozen=True, slots=True)
class ConfigAuditEntry:
    occurred_at: datetime
    actor: str
    reason: str
    changed_paths: tuple[str, ...] = ()
    redacted_old_values: Mapping[str, Any] = field(default_factory=dict)
    redacted_new_values: Mapping[str, Any] = field(default_factory=dict)
    file_sha256_before: str = ""
    file_sha256_after: str = ""
    live_confirmed: bool = False

    def __post_init__(self) -> None:
        occurred_at = self.occurred_at
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be a tz-aware datetime")
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths))
        object.__setattr__(self, "redacted_old_values", dict(self.redacted_old_values))
        object.__setattr__(self, "redacted_new_values", dict(self.redacted_new_values))


@dataclass(frozen=True, slots=True)
class MutationResult:
    changed_paths: tuple[str, ...]
    file_sha256_before: str
    file_sha256_after: str
    audit_entry: ConfigAuditEntry

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths))


@runtime_checkable
class ConfigAuditSink(Protocol):
    def record(self, entry: ConfigAuditEntry) -> None:
        """Persist an audit entry."""
        ...


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

SECRET_TOKENS = ("api_key", "secret", "password", "token", "key_id", "private_key")

_SEGMENT_RE = re.compile(r"([^.\[\]]+)(?:\[([^\]]+)\])?")


def _is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in SECRET_TOKENS)


def _parse_path(path: str) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(path, str) or not path.strip():
        raise MutationPathError(f"invalid mutation path: {path!r}")
    if path.strip() != path or ".." in path or path.startswith(".") or path.endswith("."):
        raise MutationPathError(f"invalid mutation path: {path!r}")
    raw_segments = path.split(".")
    parsed: list[tuple[str, str | None]] = []
    for raw in raw_segments:
        match = _SEGMENT_RE.fullmatch(raw)
        if match is None:
            raise MutationPathError(f"invalid mutation path: {path!r}")
        name, bracket = match.group(1), match.group(2)
        if not name:
            raise MutationPathError(f"invalid mutation path: {path!r}")
        parsed.append((name, bracket))
    if not parsed:
        raise MutationPathError(f"invalid mutation path: {path!r}")
    return tuple(parsed)


def _find_scenario(container: list[Any], name: str, path: str) -> dict[str, Any]:
    for item in container:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    raise MutationPathError(f"unknown scenario {name!r} in path {path!r}")


_PathTokens = tuple[tuple[str, str | None], ...]


def _resolve_container(doc: dict[str, Any], tokens: _PathTokens, path: str) -> Any:
    current: Any = doc
    for name, bracket in tokens:
        if bracket is not None:
            if not isinstance(current, dict) or name not in current:
                raise MutationPathError(f"unknown parent path for {path!r}")
            child = current[name]
            if not isinstance(child, list):
                raise MutationPathError(f"unknown parent path for {path!r}")
            current = _find_scenario(child, bracket, path)
        else:
            if not isinstance(current, dict) or name not in current:
                raise MutationPathError(f"unknown parent path for {path!r}")
            current = current[name]
    return current


def _get_leaf(doc: dict[str, Any], tokens: _PathTokens, path: str) -> tuple[bool, Any]:
    *parent_tokens, (leaf_name, leaf_bracket) = tokens
    if leaf_bracket is not None:
        try:
            parent = _resolve_container(doc, tuple(parent_tokens) + ((leaf_name, None),), path)
        except MutationPathError:
            return False, None
        if not isinstance(parent, list):
            raise MutationPathError(f"unknown parent path for {path!r}")
        for item in parent:
            if isinstance(item, dict) and item.get("name") == leaf_bracket:
                return True, item
        return False, None
    try:
        parent = _resolve_container(doc, tuple(parent_tokens), path) if parent_tokens else doc
    except MutationPathError:
        raise
    if not isinstance(parent, dict):
        raise MutationPathError(f"unknown parent path for {path!r}")
    if leaf_name not in parent:
        return False, None
    return True, parent[leaf_name]


def _apply_single(doc: dict[str, Any], mutation: ConfigMutation) -> None:
    tokens = _parse_path(mutation.path)
    if tokens[0][0] == "schema_version":
        raise MutationPathError("schema_version is immutable")
    *parent_tokens, (leaf_name, leaf_bracket) = tokens
    if leaf_bracket is not None:
        if parent_tokens:
            grandparent = _resolve_container(doc, tuple(parent_tokens), mutation.path)
        else:
            grandparent = doc
        if not isinstance(grandparent, dict) or leaf_name not in grandparent:
            raise MutationPathError(f"unknown parent path for {mutation.path!r}")
        container = grandparent[leaf_name]
        if not isinstance(container, list):
            raise MutationPathError(f"unknown parent path for {mutation.path!r}")
        index = next(
            (
                i
                for i, item in enumerate(container)
                if isinstance(item, dict) and item.get("name") == leaf_bracket
            ),
            None,
        )
        if mutation.value is REMOVE:
            if index is None:
                raise MutationPathError(f"unknown path {mutation.path!r}")
            del container[index]
            return
        if not isinstance(mutation.value, Mapping):
            raise MutationPathError(
                f"new scenario {leaf_bracket!r} must be a mapping for path {mutation.path!r}"
            )
        new_item = dict(mutation.value)
        new_item["name"] = leaf_bracket
        if index is None:
            container.append(new_item)
        else:
            container[index] = new_item
        return
    parent = _resolve_container(doc, tuple(parent_tokens), mutation.path) if parent_tokens else doc
    if not isinstance(parent, dict):
        raise MutationPathError(f"unknown parent path for {mutation.path!r}")
    if mutation.value is REMOVE:
        if leaf_name not in parent:
            raise MutationPathError(f"unknown path {mutation.path!r}")
        del parent[leaf_name]
        return
    parent[leaf_name] = copy.deepcopy(mutation.value)


def _scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: ("***" if isinstance(key, str) and _is_secret_name(key) else _scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value


def _redact(path: str, value: Any) -> Any:
    if value is None:
        return None
    for name, _bracket in _parse_path(path):
        if _is_secret_name(name):
            return "***"
    return _scrub(value)


def _nested_secret_paths(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(key, str) and _is_secret_name(key):
                found.append(dotted)
            found.extend(_nested_secret_paths(item, dotted))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_nested_secret_paths(item, f"{prefix}[{index}]"))
    return found


def _check_secrets(effective: list[tuple[str, Any, Any]]) -> None:
    for path, _old, new in effective:
        tokens = _parse_path(path)
        leaf_name, _leaf_bracket = tokens[-1]
        if _is_secret_name(leaf_name):
            raise SecretMutationRefusedError(f"secret-like key refused: {path!r}")
        if isinstance(new, Mapping):
            nested = _nested_secret_paths(new)
            if nested:
                raise SecretMutationRefusedError(
                    f"secret-like key refused: {path!r} introduces {nested[0]!r}"
                )


def _as_bool(value: Any) -> bool | None:
    if value is True or value is False:
        return value
    return None


def _mode_is_live(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "live"


def _collect_safety_reasons(
    old_doc: Mapping[str, Any],
    new_doc: dict[str, Any],
    effective: list[tuple[str, Any, Any]],
    request: MutationRequest,
    exposure: ExposureSnapshot,
) -> tuple[list[str], bool]:
    reasons: list[str] = []
    live_refused = False
    if not effective:
        return reasons, live_refused

    changed = {path for path, _o, _n in effective}
    old_execution: Mapping[str, Any] = old_doc.get("execution", {})  # type: ignore[assignment]
    new_execution: Mapping[str, Any] = new_doc.get("execution", {})  # type: ignore[assignment]
    if not isinstance(old_execution, Mapping):
        old_execution = {}
    if not isinstance(new_execution, Mapping):
        new_execution = {}

    def _new_flag(name: str) -> bool | None:
        return _as_bool(new_execution.get(name)) if isinstance(new_execution, Mapping) else None

    def _old_flag(name: str) -> bool | None:
        return _as_bool(old_execution.get(name)) if isinstance(old_execution, Mapping) else None

    # Live enablement.
    old_mode = old_execution.get("account_mode")
    new_mode = new_execution.get("account_mode")
    mode_enables_live = _mode_is_live(new_mode) and not _mode_is_live(old_mode)
    if "execution.account_mode" in changed and mode_enables_live and not request.confirm_live:
        reasons.append("enabling live account_mode requires confirm_live=True")
        live_refused = True
    old_live_flag = _old_flag("live_trading_enabled")
    new_live_flag = _new_flag("live_trading_enabled")
    if (
        "execution.live_trading_enabled" in changed
        and new_live_flag is True
        and old_live_flag is not True
        and not request.confirm_live
    ):
        reasons.append("enabling live_trading_enabled requires confirm_live=True")
        live_refused = True

    # Unknown exposure.
    if not exposure.known:
        for path in sorted(changed):
            tokens = _parse_path(path)
            if tokens[0][0] != "reporting":
                reasons.append(
                    f"unknown exposure refuses non-reporting change {path!r}"
                )
                break

    positions = list(exposure.positions)
    has_position = len(positions) > 0
    has_activity = (
        has_position
        or any(p.has_active_protection for p in positions)
        or any(p.has_unresolved_orders for p in positions)
    )
    exposed_symbols = {p.symbol for p in positions}

    # Account mode and broker changes.
    for guarded in ("execution.account_mode", "execution.broker"):
        if guarded in changed and has_activity:
            reasons.append(
                f"changing {guarded} is refused while positions, protection, or orders exist"
            )

    # Symbols with exposure.
    new_symbols = new_doc.get("symbols", {})
    if not isinstance(new_symbols, Mapping):
        new_symbols = {}
    new_strategies = new_doc.get("strategies", {})
    strategy_names = set(new_strategies.keys()) if isinstance(new_strategies, Mapping) else set()
    for symbol in sorted(exposed_symbols):
        if f"symbols.{symbol}" in changed:
            # Determine removal: leaf missing in the new document.
            if not isinstance(new_symbols, Mapping) or symbol not in new_symbols:
                reasons.append(f"removing symbol {symbol!r} with an open position is refused")
        enabled_path = f"symbols.{symbol}.enabled"
        if enabled_path in changed:
            new_enabled = None
            if isinstance(new_symbols, Mapping) and isinstance(new_symbols.get(symbol), Mapping):
                new_enabled = new_symbols[symbol].get("enabled")
            if new_enabled is False:
                reasons.append(f"disabling symbol {symbol!r} with an open position is refused")
        stop_type_path = f"symbols.{symbol}.stop_policy.type"
        if stop_type_path in changed:
            reasons.append(
                f"changing stop_policy.type for symbol {symbol!r} with active exposure is refused"
            )
        sell_path = f"symbols.{symbol}.sell_strategy"
        if sell_path in changed:
            new_sell = None
            if isinstance(new_symbols, Mapping) and isinstance(new_symbols.get(symbol), Mapping):
                new_sell = new_symbols[symbol].get("sell_strategy")
            if new_sell is not None and new_sell not in strategy_names:
                reasons.append(
                    f"changing sell_strategy for symbol {symbol!r} to undefined "
                    f"strategy {new_sell!r} is refused"
                )

    # Strategies in use.
    removed_strategies = [
        _parse_path(path)[1][0]
        for path in changed
        if len(_parse_path(path)) == 2
        and _parse_path(path)[0] == ("strategies", None)
        and _parse_path(path)[1][1] is None
    ]
    if removed_strategies:
        referenced: set[str] = set()
        if isinstance(new_symbols, Mapping):
            for cfg in new_symbols.values():
                if isinstance(cfg, Mapping):
                    for key in ("buy_strategy", "sell_strategy"):
                        value = cfg.get(key)
                        if isinstance(value, str):
                            referenced.add(value)
        old_symbols = old_doc.get("symbols", {})
        if isinstance(old_symbols, Mapping):
            for cfg in old_symbols.values():
                if isinstance(cfg, Mapping):
                    for key in ("buy_strategy", "sell_strategy"):
                        value = cfg.get(key)
                        if isinstance(value, str):
                            referenced.add(value)
        for position in positions:
            if position.sell_strategy is not None:
                referenced.add(position.sell_strategy)
        for name in removed_strategies:
            if name in referenced:
                reasons.append(f"removing strategy {name!r} that is still referenced is refused")

    # Scenarios in use.
    scenario_positions: set[str] = {
        p.scenario for p in positions if p.scenario is not None
    }
    for path in changed:
        tokens = _parse_path(path)
        if len(tokens) == 1 and tokens[0][0] == "shadow_scenarios" and tokens[0][1] is not None:
            scenario_name = tokens[0][1]
            assert scenario_name is not None
            if scenario_name in scenario_positions:
                reasons.append(
                    f"removing scenario {scenario_name!r} with open positions is refused"
                )

    # Protection settings.
    resulting_live = _mode_is_live(new_execution.get("account_mode"))
    for guard in ("execution.stop_required", "execution.block_new_entries_when_unprotected"):
        if guard in changed and _as_bool(new_execution.get(guard.split(".")[1])) is False:
            if has_position:
                reasons.append(
                    f"setting {guard} false is refused while positions are open"
                )
            if resulting_live:
                reasons.append(
                    f"setting {guard} false is refused when account_mode is live"
                )

    return reasons, live_refused


def _fsync_directory(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(target: str, data: bytes, mode: int) -> None:
    directory = os.path.dirname(os.path.abspath(target)) or "."
    tmp: str | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, prefix=".mutations-", suffix=".tmp", delete=False
        )
        tmp = handle.name
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.chmod(tmp, mode)
        os.replace(tmp, target)
        tmp = None
        _fsync_directory(directory)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if not isinstance(now, datetime):
        raise TypeError("clock must return a datetime")
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def apply_config_mutation(
    config_path: str | os.PathLike[str],
    request: MutationRequest,
    *,
    validate: Callable[[Mapping[str, Any]], object],
    exposure: ExposureSnapshot,
    audit: ConfigAuditSink,
    clock: Callable[[], datetime],
) -> MutationResult:
    """Apply operator mutations atomically with safety checks and auditing."""
    import yaml  # Local import keeps module import light; PyYAML is required.

    path_str = os.fspath(config_path)
    with open(path_str, "rb") as handle:
        original_bytes = handle.read()
    sha_before = hashlib.sha256(original_bytes).hexdigest()
    if request.expected_file_sha256 is not None and request.expected_file_sha256 != sha_before:
        raise ConcurrentConfigModificationError("config file changed since expected sha256")

    try:
        text = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigMutationValidationError("config file is not valid UTF-8", original=exc) from exc
    parsed = yaml.safe_load(text) if text.strip() else {}
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ConfigMutationValidationError("config top level must be a mapping", original=None)
    old_doc: dict[str, Any] = parsed
    new_doc: dict[str, Any] = copy.deepcopy(old_doc)

    for mutation in request.mutations:
        _apply_single(new_doc, mutation)

    # Effective (non-no-op) changes.
    effective: list[tuple[str, Any, Any]] = []
    for mutation in request.mutations:
        tokens = _parse_path(mutation.path)
        old_found, old_value = _get_leaf(old_doc, tokens, mutation.path)
        if mutation.value is REMOVE:
            if old_found:
                effective.append((mutation.path, copy.deepcopy(old_value), None))
        else:
            _new_found, new_value = _get_leaf(new_doc, tokens, mutation.path)
            if not old_found or old_value != new_value:
                old_copy = copy.deepcopy(old_value) if old_found else None
                effective.append((mutation.path, old_copy, copy.deepcopy(new_value)))

    try:
        original_mode = stat.S_IMODE(os.stat(path_str).st_mode)
    except OSError as exc:
        raise ConfigMutationError(f"cannot stat config file: {exc}") from exc

    if not effective:
        entry = ConfigAuditEntry(
            occurred_at=_utc_now(clock),
            actor=request.actor,
            reason=request.reason,
            changed_paths=(),
            redacted_old_values={},
            redacted_new_values={},
            file_sha256_before=sha_before,
            file_sha256_after=sha_before,
            live_confirmed=request.confirm_live,
        )
        return MutationResult(
            changed_paths=(),
            file_sha256_before=sha_before,
            file_sha256_after=sha_before,
            audit_entry=entry,
        )

    _check_secrets(effective)

    reasons, live_refused = _collect_safety_reasons(old_doc, new_doc, effective, request, exposure)
    if reasons:
        if live_refused:
            raise LiveEnableRefusedError(reasons)
        raise UnsafeConfigMutationError(reasons)

    try:
        validate(new_doc)
    except Exception as exc:
        raise ConfigMutationValidationError(
            f"mutated config failed validation: {exc}", original=exc
        ) from exc

    new_text = yaml.safe_dump(new_doc, sort_keys=False, default_flow_style=False,
                              allow_unicode=True)
    new_bytes = new_text.encode("utf-8")
    sha_after = hashlib.sha256(new_bytes).hexdigest()

    _atomic_write(path_str, new_bytes, original_mode)

    # Forward-only note for numeric stop-policy edits on exposed symbols.
    exposed = {p.symbol for p in exposure.positions}
    forward_only = any(
        len(_parse_path(path)) == 4
        and _parse_path(path)[0] == ("symbols", None)
        and _parse_path(path)[2] == ("stop_policy", None)
        and _parse_path(path)[3][0] != "type"
        and _parse_path(path)[1][0] in exposed
        for path, _o, _n in effective
    )
    audit_reason = request.reason
    if forward_only:
        audit_reason += (
            " [forward-only: stop-policy numeric change applies to new protection"
            " decisions only; the stop ratchet is enforced by the protection layer]"
        )

    entry = ConfigAuditEntry(
        occurred_at=_utc_now(clock),
        actor=request.actor,
        reason=audit_reason,
        changed_paths=tuple(path for path, _o, _n in effective),
        redacted_old_values={path: _redact(path, old) for path, old, _n in effective},
        redacted_new_values={path: _redact(path, new) for path, _o, new in effective},
        file_sha256_before=sha_before,
        file_sha256_after=sha_after,
        live_confirmed=request.confirm_live,
    )
    try:
        audit.record(entry)
    except Exception as exc:
        try:
            _atomic_write(path_str, original_bytes, original_mode)
        except Exception:
            pass
        raise ConfigAuditFailedError(
            "audit sink failed; config change was rolled back", original=exc
        ) from exc

    return MutationResult(
        changed_paths=tuple(path for path, _o, _n in effective),
        file_sha256_before=sha_before,
        file_sha256_after=sha_after,
        audit_entry=entry,
    )


__all__ = [
    "ConfigMutation",
    "MutationRequest",
    "REMOVE",
    "OpenExposure",
    "ExposureSnapshot",
    "ConfigAuditEntry",
    "ConfigAuditSink",
    "MutationResult",
    "apply_config_mutation",
    "ConfigMutationError",
    "MutationPathError",
    "ConcurrentConfigModificationError",
    "SecretMutationRefusedError",
    "UnsafeConfigMutationError",
    "LiveEnableRefusedError",
    "ConfigMutationValidationError",
    "ConfigAuditFailedError",
]
