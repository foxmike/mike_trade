"""Alpaca broker capability matrix and adapter conformance (Phase E).

This module is the fail-closed gate between the order service (e.3) and the
Alpaca Trading API. Every order-type / order-class / sizing / time-in-force /
side / operation combination resolves to exactly one
:class:`CapabilityStatus`:

- ``PROVEN`` -- explicitly recorded evidence exists for this environment.
- ``UNPROVEN`` -- default for anything not explicitly proven. Fails closed:
  :meth:`BrokerCapabilities.require` raises :class:`CapabilityUnavailable`.
- ``UNSUPPORTED`` -- structurally unavailable (long-only desk, native Alpaca
  limits). Can never be promoted by verification evidence.

Paper evidence never auto-promotes LIVE. Promoting a LIVE entry requires a
separate explicit operator-recorded entry via
:meth:`BrokerCapabilities.with_live_promotion`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from mike_trade.brokers.alpaca.auth import AlpacaEnvironment
from mike_trade.domain.errors import UnsupportedOrderCapabilityError

__all__ = [
    "BrokerCapabilities",
    "CapabilityDescriptor",
    "CapabilityStatus",
    "CapabilityEvidence",
    "CapabilityUnavailable",
    "default_alpaca_capabilities",
    "record_paper_verification",
    "check_adapter_conformance",
]


_ORDER_TYPES = frozenset({"market", "limit", "stop", "stop_limit", "trailing_stop"})
_ORDER_CLASSES = frozenset({"simple", "oto", "oco", "bracket"})
_SIZINGS = frozenset({"whole", "fractional", "notional"})
_TIME_IN_FORCE = frozenset({"day", "gtc", "opg", "cls", "ioc", "fok"})
_SIDES = frozenset({"buy", "sell", "short"})
_OPERATIONS = frozenset({"submit", "replace"})
_EVIDENCE_KINDS = frozenset({"documentation", "paper_verification", "operator_record"})

# Public documentation reference for the order endpoint. Recorded by the
# operator (see _OPERATOR_DOC_RECORD) as the initial evidence for the
# documentation-proven combinations.
_DOC_ORDERS_REFERENCE = "https://docs.alpaca.markets/reference/trading-api-orders"
_DOC_TRAILING_REFERENCE = "https://docs.alpaca.markets/docs/orders-at-alpaca"
_OPERATOR_DOC_RECORD = "OP-ALPACA-DOC-2026-001"
_OPERATOR_DOC_DATE = "2026-10-08"

_REQUIRED_OPERATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account", ("get_account",)),
    ("positions", ("list_positions",)),
    ("assets", ("get_asset", "list_assets")),
    ("clock", ("get_clock",)),
    ("open_orders", ("list_open_orders",)),
    ("lookup_by_id", ("get_order",)),
    ("lookup_by_client_order_id", ("get_order_by_client_order_id", "get_order_by_client_id")),
    ("submit", ("submit_order",)),
    ("replace", ("replace_order",)),
    ("cancel", ("cancel_order",)),
    ("activities", ("list_activities", "list_fills")),
    ("trade_update_stream", ("events", "normalize", "subscribe", "listen")),
)

_LEGACY_QTY_WORDS = {"whole", "fractional"}
_LEGACY_TIF_WORDS = {"day", "gtc"}
_LEGACY_REPLACE_FIELDS = {
    "limit_price": "limit",
    "stop_price": "stop",
    "trail_price": "trailing_stop",
    "trail_percent": "trailing_stop",
}


class CapabilityStatus(Enum):
    """Availability verdict for one descriptor in one environment."""

    PROVEN = "proven"
    UNPROVEN = "unproven"
    UNSUPPORTED = "unsupported"


def _require_nonempty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _ensure_date(value: date | str, *, field: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError(f"{field} must be a YYYY-MM-DD date string") from None
    raise ValueError(f"{field} must be a date or YYYY-MM-DD string")


def _ensure_environment(environment: Any) -> AlpacaEnvironment:
    if isinstance(environment, AlpacaEnvironment):
        return environment
    raw = getattr(environment, "value", environment)
    if isinstance(raw, str):
        try:
            return AlpacaEnvironment(raw.strip().upper())
        except ValueError:
            pass
    raise ValueError(
        "environment must be an AlpacaEnvironment (or PAPER/LIVE string); "
        f"got {environment!r}"
    )


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """Explicit evidence backing one matrix entry.

    ``kind`` is one of ``documentation`` (provider doc URL), ``paper_verification``
    (a paper-trading verification record id), or ``operator_record`` (an explicit
    operator entry, required for every LIVE promotion). ``reference`` is the doc
    URL or record id; ``recorded_on`` is the YYYY-MM-DD recording date.
    """

    kind: str
    reference: str
    recorded_on: str
    note: str = ""

    def __post_init__(self) -> None:
        kind = _require_nonempty(self.kind, field="kind").strip().lower()
        if kind not in _EVIDENCE_KINDS:
            raise ValueError(f"kind must be one of {sorted(_EVIDENCE_KINDS)}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "reference", _require_nonempty(self.reference, field="reference"))
        object.__setattr__(self, "recorded_on", _ensure_date(self.recorded_on, field="recorded_on"))
        if not isinstance(self.note, str):
            raise ValueError("note must be a string")
        object.__setattr__(self, "note", self.note)


def _validated_field(value: Any, *, field: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string; got {type(value).__name__}")
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"unsupported {field}: {value!r}; expected one of {sorted(allowed)}")
    return normalized


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """One order capability: type x class x sizing x TIF x side x operation.

    ``sizing`` is ``whole`` (whole-share qty), ``fractional`` (fractional qty),
    or ``notional``. ``side`` is ``buy``, ``sell`` (long exit), or ``short``
    (short sale, always unsupported on the long-only desk). ``operation`` is
    ``submit`` or ``replace``.
    """

    order_type: str
    order_class: str
    sizing: str
    time_in_force: str
    side: str
    operation: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "order_type",
            _validated_field(self.order_type, field="order_type", allowed=_ORDER_TYPES),
        )
        object.__setattr__(
            self,
            "order_class",
            _validated_field(self.order_class, field="order_class", allowed=_ORDER_CLASSES),
        )
        object.__setattr__(
            self, "sizing", _validated_field(self.sizing, field="sizing", allowed=_SIZINGS)
        )
        object.__setattr__(
            self,
            "time_in_force",
            _validated_field(self.time_in_force, field="time_in_force", allowed=_TIME_IN_FORCE),
        )
        object.__setattr__(self, "side", _validated_field(self.side, field="side", allowed=_SIDES))
        object.__setattr__(
            self,
            "operation",
            _validated_field(self.operation, field="operation", allowed=_OPERATIONS),
        )

    def key(self) -> tuple[str, str, str, str, str, str]:
        """Hashable matrix key for this descriptor."""
        return (
            self.order_type,
            self.order_class,
            self.sizing,
            self.time_in_force,
            self.side,
            self.operation,
        )

    def label(self) -> str:
        """Human/operator-readable label used in errors and evidence."""
        return (
            f"{self.order_type}/{self.order_class}/{self.sizing}/"
            f"{self.time_in_force}/{self.side}/{self.operation}"
        )


class CapabilityUnavailable(UnsupportedOrderCapabilityError):
    """A descriptor was refused because it is not PROVEN for the environment."""

    def __init__(self, capability: str, message: str | None = None) -> None:
        self.capability = capability
        super().__init__(
            message or f"Alpaca capability {capability!r} is not available",
            code="capability_unavailable",
            context={"capability": capability},
        )


def _structural_status(descriptor: CapabilityDescriptor) -> CapabilityStatus | None:
    """Structural verdict, or None when the descriptor is structurally possible."""
    if descriptor.side == "short":
        return CapabilityStatus.UNSUPPORTED
    if descriptor.order_type == "trailing_stop" and descriptor.order_class != "simple":
        return CapabilityStatus.UNSUPPORTED
    if descriptor.operation == "replace" and descriptor.sizing == "notional":
        return CapabilityStatus.UNSUPPORTED
    return None


def _structural_reason(descriptor: CapabilityDescriptor) -> str | None:
    """Operator-readable reason for a structural UNSUPPORTED verdict."""
    if descriptor.side == "short":
        return "short selling is UNSUPPORTED: the desk trades long-only"
    if descriptor.order_type == "trailing_stop" and descriptor.order_class != "simple":
        return (
            "trailing_stop with order_class other than simple is never available: "
            "Alpaca supports standalone trailing stops only"
        )
    if descriptor.operation == "replace" and descriptor.sizing == "notional":
        return "notional orders cannot be replaced: Alpaca rejects notional replace"
    return None


def _ensure_descriptor(descriptor: Any) -> CapabilityDescriptor:
    if not isinstance(descriptor, CapabilityDescriptor):
        raise TypeError("descriptor must be a CapabilityDescriptor")
    return descriptor


_Entry = tuple[CapabilityStatus, CapabilityEvidence]


class BrokerCapabilities:
    """Fail-closed capability matrix keyed by environment and descriptor.

    The matrix stores only explicitly recorded entries. :meth:`status_of`
    resolves every other descriptor to ``UNPROVEN`` (structural impossibilities
    resolve to ``UNSUPPORTED``), so lookups never raise ``KeyError``.
    Instances are treated as immutable: promotion helpers return new instances.
    """

    def __init__(
        self,
        entries: Mapping[tuple[str, tuple[str, str, str, str, str, str]], _Entry] | None = None,
    ) -> None:
        self._entries: dict[tuple[str, tuple[str, str, str, str, str, str]], _Entry] = (
            dict(entries) if entries else {}
        )

    def status_of(
        self, descriptor: CapabilityDescriptor, environment: AlpacaEnvironment | str
    ) -> CapabilityStatus:
        """Resolve the status for ``descriptor`` in ``environment`` (never KeyErrors)."""
        checked = _ensure_descriptor(descriptor)
        env = _ensure_environment(environment)
        structural = _structural_status(checked)
        if structural is not None:
            return structural
        stored = self._entries.get((env.value, checked.key()))
        if stored is not None:
            return stored[0]
        return CapabilityStatus.UNPROVEN

    def evidence_of(
        self, descriptor: CapabilityDescriptor, environment: AlpacaEnvironment | str
    ) -> CapabilityEvidence | None:
        """Return the recorded evidence, or None when nothing was recorded."""
        checked = _ensure_descriptor(descriptor)
        env = _ensure_environment(environment)
        stored = self._entries.get((env.value, checked.key()))
        return stored[1] if stored is not None else None

    def is_available(
        self, descriptor: CapabilityDescriptor, environment: AlpacaEnvironment | str
    ) -> bool:
        """Return True only when the descriptor is PROVEN for the environment."""
        return self.status_of(descriptor, environment) is CapabilityStatus.PROVEN

    def require(
        self, descriptor: CapabilityDescriptor, environment: AlpacaEnvironment | str
    ) -> CapabilityDescriptor:
        """Return ``descriptor`` when PROVEN; otherwise raise CapabilityUnavailable."""
        checked = _ensure_descriptor(descriptor)
        env = _ensure_environment(environment)
        if self.is_available(checked, env):
            return checked
        status = self.status_of(checked, env)
        reason = _structural_reason(checked) or f"status is {status.value}"
        evidence = self.evidence_of(checked, env)
        evidence_text = (
            f"; evidence={evidence.kind}:{evidence.reference} recorded {evidence.recorded_on}"
            if evidence is not None
            else "; no evidence recorded"
        )
        raise CapabilityUnavailable(
            checked.label(),
            f"Alpaca {env.value} capability {checked.label()!r} is unavailable "
            f"({reason}{evidence_text}). "
            "Promote it only with explicit paper verification (PAPER) or an "
            "explicit operator record (LIVE).",
        )

    def with_live_promotion(
        self, descriptor: CapabilityDescriptor, evidence: CapabilityEvidence
    ) -> BrokerCapabilities:
        """Return a copy with the LIVE entry promoted to PROVEN.

        ``evidence`` must be an explicit ``operator_record``; paper evidence and
        documentation can never promote LIVE. Structurally UNSUPPORTED
        descriptors can never be promoted.
        """
        checked = _ensure_descriptor(descriptor)
        if not isinstance(evidence, CapabilityEvidence):
            raise TypeError("evidence must be a CapabilityEvidence")
        if _structural_status(checked) is CapabilityStatus.UNSUPPORTED:
            raise CapabilityUnavailable(
                checked.label(),
                f"Alpaca LIVE capability {checked.label()!r} is structurally "
                f"unsupported ({_structural_reason(checked)}); it can never be promoted.",
            )
        if evidence.kind != "operator_record":
            raise ValueError(
                "LIVE promotion requires an explicit operator_record evidence entry; "
                f"got kind {evidence.kind!r}"
            )
        entries = dict(self._entries)
        entries[(AlpacaEnvironment.LIVE.value, checked.key())] = (CapabilityStatus.PROVEN, evidence)
        return BrokerCapabilities(entries)

    def as_legacy_checker(
        self, environment: AlpacaEnvironment | str
    ) -> Callable[[str], bool]:
        """Return a ``Callable[[str], bool]`` for the e.3 order service.

        Maps the order service's string capability names
        (``standalone_stop_sell:{whole|fractional}:{day|gtc}``,
        ``standalone_trailing_stop_sell:{whole|fractional}:{day|gtc}``,
        ``replace_{limit_price|stop_price|trail_price|trail_percent}``) onto
        descriptors. Unknown names fail closed (False). Legacy replace names
        resolve to True when any matching replace descriptor for that order
        type is PROVEN for the environment.
        """
        env = _ensure_environment(environment)

        def check(capability: str) -> bool:
            descriptor = _legacy_to_descriptor(capability)
            if descriptor is None:
                return False
            if descriptor.operation == "replace":
                return self._any_replace_available(descriptor.order_type, env)
            return self.is_available(descriptor, env)

        return check

    def _any_replace_available(self, order_type: str, env: AlpacaEnvironment) -> bool:
        for order_class in sorted(_ORDER_CLASSES):
            for sizing in sorted(_SIZINGS):
                for tif in sorted(_TIME_IN_FORCE):
                    for side in ("buy", "sell"):
                        candidate = CapabilityDescriptor(
                            order_type=order_type,
                            order_class=order_class,
                            sizing=sizing,
                            time_in_force=tif,
                            side=side,
                            operation="replace",
                        )
                        if self.is_available(candidate, env):
                            return True
        return False

    def explicit_entries(
        self,
    ) -> tuple[
        tuple[AlpacaEnvironment, CapabilityDescriptor, CapabilityStatus, CapabilityEvidence], ...
    ]:
        """Return the explicitly recorded entries (for operator tables and tests)."""
        result = []
        for (env_value, key), (status, evidence) in self._entries.items():
            result.append(
                (
                    AlpacaEnvironment(env_value),
                    CapabilityDescriptor(
                        order_type=key[0],
                        order_class=key[1],
                        sizing=key[2],
                        time_in_force=key[3],
                        side=key[4],
                        operation=key[5],
                    ),
                    status,
                    evidence,
                )
            )
        result.sort(key=lambda item: (item[0].value, item[1].label()))
        return tuple(result)


def _legacy_to_descriptor(capability: Any) -> CapabilityDescriptor | None:
    if not isinstance(capability, str):
        return None
    parts = capability.strip().lower().split(":")
    if len(parts) == 3 and parts[0] in {"standalone_stop_sell", "standalone_trailing_stop_sell"}:
        _, qty_word, tif = parts
        if qty_word not in _LEGACY_QTY_WORDS or tif not in _LEGACY_TIF_WORDS:
            return None
        order_type = "stop" if parts[0] == "standalone_stop_sell" else "trailing_stop"
        return CapabilityDescriptor(
            order_type=order_type,
            order_class="simple",
            sizing=qty_word,
            time_in_force=tif,
            side="sell",
            operation="submit",
        )
    if len(parts) == 1 and parts[0].startswith("replace_"):
        field = parts[0][len("replace_") :]
        order_type = _LEGACY_REPLACE_FIELDS.get(field)
        if order_type is None:
            return None
        return CapabilityDescriptor(
            order_type=order_type,
            order_class="simple",
            sizing="whole",
            time_in_force="day",
            side="sell",
            operation="replace",
        )
    return None


def default_alpaca_capabilities() -> BrokerCapabilities:
    """Return the initial Alpaca matrix for PAPER and LIVE.

    PROVEN-by-documentation (explicit operator record ``OP-ALPACA-DOC-2026-001``):

    - whole-share market/limit DAY/GTC simple buy/sell submit;
    - standalone whole-share trailing_stop sell (trail_price or trail_percent)
      DAY/GTC simple submit.

    Everything else defaults to UNPROVEN (stop-only OTO, bracket/OCO,
    fractional stop or linked orders, fractional trailing stop, stop-price
    replace, trailing replace, OTO replace, fractional GTC, extended TIFs),
    except the structural UNSUPPORTED rules (short selling, non-simple
    trailing_stop, notional replace).
    """
    entries: dict[tuple[str, tuple[str, str, str, str, str, str]], _Entry] = {}
    for env in (AlpacaEnvironment.PAPER, AlpacaEnvironment.LIVE):
        if env is AlpacaEnvironment.PAPER:
            basic_evidence = CapabilityEvidence(
                kind="documentation",
                reference=_DOC_ORDERS_REFERENCE,
                recorded_on=_OPERATOR_DOC_DATE,
                note=(
                    "PROVEN by provider documentation; pending paper verification. "
                    f"Operator record {_OPERATOR_DOC_RECORD}."
                ),
            )
            trailing_evidence = CapabilityEvidence(
                kind="documentation",
                reference=_DOC_TRAILING_REFERENCE,
                recorded_on=_OPERATOR_DOC_DATE,
                note=(
                    "Standalone trailing_stop sell with trail_price or trail_percent "
                    "is documented; pending paper verification. "
                    f"Operator record {_OPERATOR_DOC_RECORD}."
                ),
            )
        else:
            basic_evidence = CapabilityEvidence(
                kind="operator_record",
                reference=_OPERATOR_DOC_RECORD,
                recorded_on=_OPERATOR_DOC_DATE,
                note=(
                    "Explicit operator entry: whole-share market/limit simple submit "
                    f"is documented at {_DOC_ORDERS_REFERENCE}; LIVE order flow "
                    "additionally requires live authorization and paper verification "
                    "of any change before use."
                ),
            )
            trailing_evidence = CapabilityEvidence(
                kind="operator_record",
                reference=_OPERATOR_DOC_RECORD,
                recorded_on=_OPERATOR_DOC_DATE,
                note=(
                    "Explicit operator entry: standalone whole-share trailing_stop "
                    "sell is documented at "
                    f"{_DOC_TRAILING_REFERENCE}; LIVE order flow additionally "
                    "requires live authorization."
                ),
            )
        for order_type in ("market", "limit"):
            for tif in ("day", "gtc"):
                for side in ("buy", "sell"):
                    descriptor = CapabilityDescriptor(
                        order_type=order_type,
                        order_class="simple",
                        sizing="whole",
                        time_in_force=tif,
                        side=side,
                        operation="submit",
                    )
                    entries[(env.value, descriptor.key())] = (
                        CapabilityStatus.PROVEN,
                        basic_evidence,
                    )
        for tif in ("day", "gtc"):
            descriptor = CapabilityDescriptor(
                order_type="trailing_stop",
                order_class="simple",
                sizing="whole",
                time_in_force=tif,
                side="sell",
                operation="submit",
            )
            entries[(env.value, descriptor.key())] = (
                CapabilityStatus.PROVEN,
                trailing_evidence,
            )
    return BrokerCapabilities(entries)


def record_paper_verification(
    capabilities: BrokerCapabilities,
    descriptor: CapabilityDescriptor,
    record_id: str,
    verified_on: date | str,
    note: str = "",
) -> BrokerCapabilities:
    """Return a copy with the PAPER entry for ``descriptor`` promoted to PROVEN.

    ``record_id`` is the explicit paper-verification record id and
    ``verified_on`` its YYYY-MM-DD date. The LIVE entry is never touched:
    paper evidence never auto-promotes LIVE. Structurally UNSUPPORTED
    descriptors raise :class:`CapabilityUnavailable` instead of promoting.
    The input matrix is not mutated.
    """
    if not isinstance(capabilities, BrokerCapabilities):
        raise TypeError("capabilities must be a BrokerCapabilities")
    checked = _ensure_descriptor(descriptor)
    cleaned_record = _require_nonempty(record_id, field="record_id")
    if not isinstance(note, str):
        raise ValueError("note must be a string")
    if _structural_status(checked) is CapabilityStatus.UNSUPPORTED:
        raise CapabilityUnavailable(
            checked.label(),
            f"PAPER capability {checked.label()!r} is structurally unsupported "
            f"({_structural_reason(checked)}); paper verification cannot promote it.",
        )
    evidence = CapabilityEvidence(
        kind="paper_verification",
        reference=cleaned_record,
        recorded_on=_ensure_date(verified_on, field="verified_on"),
        note=note,
    )
    entries = dict(capabilities._entries)
    entries[(AlpacaEnvironment.PAPER.value, checked.key())] = (
        CapabilityStatus.PROVEN,
        evidence,
    )
    return BrokerCapabilities(entries)


def _as_components(adapter: Any) -> list[Any]:
    if isinstance(adapter, Mapping):
        return list(adapter.values())
    if isinstance(adapter, (list, tuple, set, frozenset)):
        return list(adapter)
    if isinstance(adapter, (str, bytes)):
        raise TypeError("adapter must be an object, mapping, or collection of objects")
    return [adapter]


def check_adapter_conformance(adapter: Any) -> dict[str, Any]:
    """Check that ``adapter`` exposes the required broker port operations.

    ``adapter`` may be a single adapter instance (or class) or a mapping /
    collection of service objects whose union is checked (the e.1-e.4 Alpaca
    services are separate objects: account, assets, orders, activities,
    trade-update stream). Returns ``{"ok": bool, "present": tuple,
    "missing": tuple}`` where names are the canonical operations: account,
    positions, assets, clock, open_orders, lookup_by_id,
    lookup_by_client_order_id, submit, replace, cancel, activities,
    trade_update_stream. An operation counts as present when any candidate
    method is callable on any component.
    """
    components = _as_components(adapter)
    if not components:
        raise ValueError("adapter must provide at least one component to check")
    present: list[str] = []
    missing: list[str] = []
    for operation, candidates in _REQUIRED_OPERATIONS:
        found = any(
            callable(getattr(component, name, None))
            for component in components
            for name in candidates
        )
        (present if found else missing).append(operation)
    return {
        "ok": not missing,
        "present": tuple(present),
        "missing": tuple(missing),
    }
