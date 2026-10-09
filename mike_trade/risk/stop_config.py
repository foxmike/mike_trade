"""Operator ``stop_policy`` mapping onto the shared domain protection policy.

One pure translation is shared by the SHADOW lifecycle and the historical
backtester so both realize exactly the same protective-stop definitions:

* ``fixed`` — ``stop_percent`` or ``stop_amount`` below the confirmed fill.
* ``native_trailing`` — ``trail_percent`` or ``trail_amount``; the broker (or
  its simulation) ratchets the stop and never lowers it.
* ``stepped_trailing`` — engine-managed ratchet: initial distance
  ``initial_stop_percent``, activation after a ``step_trigger_percent`` gain,
  then whole steps of ``step_percent``. Percent values are taken on the entry
  reference, exactly like ``risk.trailing``, so they resolve to constant USD
  amounts for the life of the position.

The result is a ``domain.protection.ProtectionPolicy``; this module performs no
I/O, reads no clock and never touches a broker.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy

__all__ = [
    "STOP_POLICY_TYPES",
    "normalize_stop_spec",
    "protection_policy_for_entry",
]

STOP_POLICY_TYPES = ("fixed", "native_trailing", "stepped_trailing")

_KEYS: Mapping[str, tuple[str, ...]] = {
    "fixed": ("stop_percent", "stop_amount"),
    "native_trailing": ("trail_percent", "trail_amount"),
    "stepped_trailing": ("initial_stop_percent", "step_trigger_percent", "step_percent"),
}
_HUNDRED = Decimal("100")


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must be a Decimal or decimal string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a Decimal or decimal string") from None
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be a finite number > 0")
    return result


def normalize_stop_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a ``stop_policy`` mapping and coerce its numbers to ``Decimal``.

    Accepts exactly the shapes the configuration validator produces; anything
    else fails closed with ``ValueError``.
    """
    if not isinstance(spec, Mapping):
        raise ValueError("stop policy must be a mapping")
    kind = spec.get("type")
    if kind not in STOP_POLICY_TYPES:
        raise ValueError(
            f"unknown stop policy type {kind!r}; expected one of {STOP_POLICY_TYPES}"
        )
    allowed = set(_KEYS[kind]) | {"type"}
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise ValueError(f"unknown {kind} stop policy key(s): {unknown}")
    present = [key for key in _KEYS[kind] if key in spec]
    if kind == "stepped_trailing":
        if len(present) != 3:
            raise ValueError(
                "stepped_trailing needs initial_stop_percent, step_trigger_percent "
                "and step_percent"
            )
    elif len(present) != 1:
        raise ValueError(f"{kind} needs exactly one of {' or '.join(_KEYS[kind])}")
    result: dict[str, Any] = {"type": kind}
    for key in present:
        result[key] = _decimal(spec[key], key)
        if key.endswith("percent") and result[key] >= 50:
            raise ValueError(f"{key} must be below 50")
    return result


def protection_policy_for_entry(
    spec: Mapping[str, Any], entry_price: Decimal
) -> ProtectionPolicy:
    """Return the domain policy for one entry anchored at ``entry_price``.

    ``entry_price`` is the confirmed average fill; only the stepped mode needs
    it (to turn percent steps into constant USD amounts).
    """
    normalized = normalize_stop_spec(spec)
    kind = normalized["type"]
    if kind == "fixed":
        return ProtectionPolicy(
            ProtectionKind.FIXED_STOP,
            stop_offset_percent=normalized.get("stop_percent"),
            stop_offset_amount=normalized.get("stop_amount"),
        )
    if kind == "native_trailing":
        return ProtectionPolicy(
            ProtectionKind.BROKER_TRAILING_STOP,
            trail_percent=normalized.get("trail_percent"),
            trail_amount=normalized.get("trail_amount"),
        )
    entry = _decimal(entry_price, "entry_price")
    return ProtectionPolicy(
        ProtectionKind.STEPPED_TRAILING_STOP,
        trail_percent=normalized["initial_stop_percent"],
        step_size=entry * normalized["step_percent"] / _HUNDRED,
        activation_gain=entry * normalized["step_trigger_percent"] / _HUNDRED,
    )
