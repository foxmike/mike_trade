"""Immutable application-capital budget policy and snapshot values.

Broker buying power is an input to a snapshot, not application-owned state.  In
particular, unmanaged positions are deliberately absent from the committed
notional inputs: their effect is already present in the broker's buying power.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["BudgetPolicy", "BudgetSnapshot"]

_ZERO = Decimal("0")


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < _ZERO or (positive and value == _ZERO):
        operator = "> 0" if positive else ">= 0"
        raise ValueError(f"{name} must be {operator}")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Capital limits for one execution account/scenario.

    ``total_budget_usd`` covers managed open-position entry notional plus
    pending entry reservations. ``symbol_budget_usd`` is the maximum size of
    one symbol's entry cycle; the reservation ledger separately prevents a
    second reservation while that cycle is pending or open.

    With margin disabled (the default), callers should supply settled cash or
    non-margin buying power to snapshots.  If both are supplied, the most
    conservative value is used.
    """

    total_budget_usd: Decimal
    symbol_budget_usd: Decimal
    reserve_usd: Decimal = _ZERO
    margin_enabled: bool = False

    def __post_init__(self) -> None:
        total = _decimal(self.total_budget_usd, "total_budget_usd", positive=True)
        symbol = _decimal(self.symbol_budget_usd, "symbol_budget_usd", positive=True)
        reserve = _decimal(self.reserve_usd, "reserve_usd")
        if symbol > total:
            raise ValueError("symbol_budget_usd must not exceed total_budget_usd")
        if not isinstance(self.margin_enabled, bool):
            raise TypeError("margin_enabled must be bool")
        object.__setattr__(self, "total_budget_usd", total)
        object.__setattr__(self, "symbol_budget_usd", symbol)
        object.__setattr__(self, "reserve_usd", reserve)

    @property
    def broker_reserve_usd(self) -> Decimal:
        """Explicit alias for integrations that call the reserve broker-facing."""
        return self.reserve_usd

    @property
    def total_application_budget(self) -> Decimal:
        """Compatibility name used by the existing configuration domain."""
        return self.total_budget_usd

    @property
    def per_symbol_budget(self) -> Decimal:
        """Compatibility name used by the existing configuration domain."""
        return self.symbol_budget_usd


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """A point-in-time view of usable managed capital for one symbol."""

    account_scope_id: str
    symbol: str
    application_budget_usd: Decimal
    symbol_budget_usd: Decimal
    committed_open_notional_usd: Decimal
    pending_reserved_usd: Decimal
    remaining_application_usd: Decimal
    remaining_symbol_usd: Decimal
    broker_available_usd: Decimal
    effective_cap_usd: Decimal
    remaining_total_usd: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "account_scope_id", _text(self.account_scope_id, "account_scope_id")
        )
        symbol = _text(self.symbol, "symbol").upper()
        if any(character.isspace() for character in symbol):
            raise ValueError("symbol must not contain whitespace")
        object.__setattr__(self, "symbol", symbol)
        for name in (
            "application_budget_usd",
            "symbol_budget_usd",
            "committed_open_notional_usd",
            "pending_reserved_usd",
            "remaining_application_usd",
            "remaining_symbol_usd",
            "broker_available_usd",
            "effective_cap_usd",
            "remaining_total_usd",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.remaining_total_usd != self.effective_cap_usd:
            raise ValueError("remaining_total_usd must equal effective_cap_usd")

    @property
    def remaining_per_symbol_usd(self) -> Decimal:
        return self.remaining_symbol_usd

    @property
    def committed_open_notional(self) -> Decimal:
        return self.committed_open_notional_usd

    @property
    def pending_reserved(self) -> Decimal:
        return self.pending_reserved_usd

    @property
    def remaining_total(self) -> Decimal:
        return self.remaining_total_usd

    @classmethod
    def calculate(
        cls,
        *,
        policy: BudgetPolicy,
        account_scope_id: str,
        symbol: str,
        committed_open_notional_usd: Decimal,
        pending_reserved_usd: Decimal,
        symbol_committed_open_notional_usd: Decimal,
        symbol_pending_reserved_usd: Decimal,
        broker_buying_power_usd: Decimal,
        settled_cash_usd: Decimal | None = None,
        non_margin_buying_power_usd: Decimal | None = None,
        non_marginable_buying_power_usd: Decimal | None = None,
    ) -> BudgetSnapshot:
        """Build a snapshot without counting external positions as commitment.

        Broker buying power already incorporates external/unmanaged holdings.
        Pending application reservations are deducted because they can exist
        before the broker has reflected their orders.
        """
        if not isinstance(policy, BudgetPolicy):
            raise TypeError("policy must be BudgetPolicy")
        committed = _decimal(
            committed_open_notional_usd, "committed_open_notional_usd"
        )
        pending = _decimal(pending_reserved_usd, "pending_reserved_usd")
        symbol_committed = _decimal(
            symbol_committed_open_notional_usd,
            "symbol_committed_open_notional_usd",
        )
        symbol_pending = _decimal(
            symbol_pending_reserved_usd, "symbol_pending_reserved_usd"
        )
        broker_power = _decimal(broker_buying_power_usd, "broker_buying_power_usd")

        non_margin_values: list[Decimal] = []
        if settled_cash_usd is not None:
            non_margin_values.append(_decimal(settled_cash_usd, "settled_cash_usd"))
        if non_margin_buying_power_usd is not None:
            non_margin_values.append(
                _decimal(non_margin_buying_power_usd, "non_margin_buying_power_usd")
            )
        if non_marginable_buying_power_usd is not None:
            non_margin_values.append(
                _decimal(
                    non_marginable_buying_power_usd,
                    "non_marginable_buying_power_usd",
                )
            )
        if policy.margin_enabled:
            eligible_broker_power = broker_power
        elif non_margin_values:
            eligible_broker_power = min(broker_power, *non_margin_values)
        else:
            # Some cash-only brokers expose their non-margin value solely as
            # ``buying_power``.  The caller remains responsible for provenance.
            eligible_broker_power = broker_power

        application_remaining = max(
            _ZERO, policy.total_budget_usd - committed - pending
        )
        symbol_remaining = max(
            _ZERO, policy.symbol_budget_usd - symbol_committed - symbol_pending
        )
        broker_available = max(
            _ZERO, eligible_broker_power - policy.reserve_usd - pending
        )
        effective = min(application_remaining, broker_available)
        return cls(
            account_scope_id=account_scope_id,
            symbol=symbol,
            application_budget_usd=policy.total_budget_usd,
            symbol_budget_usd=policy.symbol_budget_usd,
            committed_open_notional_usd=committed,
            pending_reserved_usd=pending,
            remaining_application_usd=application_remaining,
            remaining_symbol_usd=symbol_remaining,
            broker_available_usd=broker_available,
            effective_cap_usd=effective,
            remaining_total_usd=effective,
        )
