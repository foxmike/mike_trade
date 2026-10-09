"""Broker activity normalization, fee ingestion, and per-order fee attribution.

Design choices (the task's decision budget):
1. The broker port has no account-activities method, so activities arrive as an
   injected iterable.  Each item may be a mapping (a JSON object) or an object
   exposing the same names as attributes (for example the Alpaca adapter's
   normalized activity dataclasses).  With no source wired nothing is ingested
   and order fees stay ``UNAVAILABLE`` unless a fee schedule is configured.
2. Only an allowlist of fields survives normalization.  Free-text descriptions,
   raw payloads, account identifiers and anything else a provider sends are
   dropped; the "description category" is a short upper-case token taken from
   the activity subtype, never the provider's description text.
3. ``fee_amount`` is the cost to the account: a charge is positive and a rebate
   negative.  It is ``-net_amount`` when the provider reports a signed amount
   and the literal value when it reports only a ``fee`` field.
4. A fee is ``CONFIRMED`` only when a fee-bearing activity names the order (by
   ``order_id`` or a ``client_order_id`` mapped to it).  Otherwise it is
   ``ESTIMATED`` from an explicitly configured :class:`FeeSchedule`, or
   ``UNAVAILABLE``.  Fee amounts are never read from operator-supplied files.
5. SHADOW fees come only from the scenario's simulated fee model, i.e. the fee
   recorded on each simulated fill, and are always ``ESTIMATED``.  SHADOW
   databases refuse broker activity ingestion.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as Date
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import Enum
from typing import Any

from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.domain.trading import Fill
from mike_trade.persistence.repositories import (
    BrokerOrderRepository,
    CashEquityRepository,
    Clock,
)
from mike_trade.persistence.schema import from_db_timestamp
from mike_trade.persistence.transaction import ScopedWriter

__all__ = [
    "FeeStatus",
    "ActivityCategory",
    "FeeKind",
    "BrokerActivityRecord",
    "FeeSchedule",
    "OrderFee",
    "IngestResult",
    "classify_activity",
    "normalize_broker_activity",
    "ingest_broker_activities",
    "load_ingested_activities",
    "attribute_order_fees",
]

_ZERO = Decimal("0")
_BPS = Decimal("10000")
_CENT = Decimal("0.01")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
_SAFE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9./\-]{0,15}$")
_SAFE_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_BROKER_ENVIRONMENTS = frozenset({AccountEnvironment.PAPER, AccountEnvironment.LIVE})


class FeeStatus(str, Enum):  # noqa: UP042 - exact str values are a cross-phase contract
    """Provenance of a fee amount; an estimate is never reported as confirmed."""

    CONFIRMED = "CONFIRMED"
    ESTIMATED = "ESTIMATED"
    UNAVAILABLE = "UNAVAILABLE"


class ActivityCategory(str, Enum):  # noqa: UP042
    TRADE_FILL = "TRADE_FILL"
    FEE = "FEE"
    EXTERNAL_FLOW = "EXTERNAL_FLOW"
    INCOME = "INCOME"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    OTHER = "OTHER"


class FeeKind(str, Enum):  # noqa: UP042
    REGULATORY = "REGULATORY"
    COMMISSION = "COMMISSION"
    OTHER = "OTHER"


_REGULATORY_FEES = frozenset({"TAF", "REG", "SEC", "CAT", "ORF", "OCC", "NRV", "NRC", "LCT"})
_COMMISSION_FEES = frozenset({"COMM", "COMMISSION", "CFEE"})
_OTHER_FEES = frozenset({"FEE", "PTC", "PTR"})
_EXTERNAL_FLOWS = frozenset(
    {
        "CSD", "CSW", "JNL", "JNLC", "JNLS", "ACATC", "ACATS", "ACH", "WIRE", "XFER",
        "TRANSFER", "DEPOSIT", "WITHDRAWAL",
    }
)
_INCOME_EXACT = frozenset({"DIV", "CGD", "INT", "INTNRA", "INTTW"})
_CORPORATE_ACTIONS = frozenset(
    {
        "SPLIT", "SSP", "SPIN", "SSO", "MA", "NC", "SC", "REO", "REORG", "FOPT", "OPCA",
        "OPASN", "OPEXP", "OPXRC",
    }
)


def classify_activity(activity_type: str) -> tuple[ActivityCategory, FeeKind | None]:
    """Map a provider activity type to a category (and fee kind for fees)."""
    kind = activity_type.strip().upper()
    if kind in ("FILL", "TRADE"):
        return ActivityCategory.TRADE_FILL, None
    if kind in _REGULATORY_FEES:
        return ActivityCategory.FEE, FeeKind.REGULATORY
    if kind in _COMMISSION_FEES:
        return ActivityCategory.FEE, FeeKind.COMMISSION
    if kind in _OTHER_FEES:
        return ActivityCategory.FEE, FeeKind.OTHER
    if kind in _EXTERNAL_FLOWS:
        return ActivityCategory.EXTERNAL_FLOW, None
    if kind in _INCOME_EXACT or kind.startswith("DIV"):
        return ActivityCategory.INCOME, None
    if kind in _CORPORATE_ACTIONS:
        return ActivityCategory.CORPORATE_ACTION, None
    return ActivityCategory.OTHER, None


# ------------------------------------------------------------------------ records


@dataclass(frozen=True, slots=True)
class BrokerActivityRecord:
    """The allowlisted, typed projection of one broker activity."""

    activity_id: str
    activity_type: str
    category: ActivityCategory
    occurred_at: datetime
    fee_kind: FeeKind | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    symbol: str | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    net_amount: Decimal | None = None
    fee_amount: Decimal | None = None
    description_category: str | None = None

    @property
    def is_fee_charge(self) -> bool:
        """True when this activity is evidence of a fee charged to the account."""
        return self.fee_amount is not None and self.category in (
            ActivityCategory.FEE,
            ActivityCategory.TRADE_FILL,
        )

    def to_payload(self) -> dict[str, Any]:
        """The only mapping ever persisted for an activity."""
        return {
            "activity_id": self.activity_id,
            "activity_type": self.activity_type,
            "category": self.category,
            "fee_kind": self.fee_kind,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": self.qty,
            "price": self.price,
            "net_amount": self.net_amount,
            "fee_amount": self.fee_amount,
            "occurred_at": self.occurred_at,
            "description_category": self.description_category,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BrokerActivityRecord:
        def dec(name: str) -> Decimal | None:
            value = payload.get(name)
            return None if value is None else Decimal(str(value))

        fee_kind = payload.get("fee_kind")
        return cls(
            activity_id=str(payload["activity_id"]),
            activity_type=str(payload["activity_type"]),
            category=ActivityCategory(payload["category"]),
            occurred_at=from_db_timestamp(str(payload["occurred_at"])),
            fee_kind=None if fee_kind is None else FeeKind(fee_kind),
            order_id=payload.get("order_id"),
            client_order_id=payload.get("client_order_id"),
            symbol=payload.get("symbol"),
            qty=dec("qty"),
            price=dec("price"),
            net_amount=dec("net_amount"),
            fee_amount=dec("fee_amount"),
            description_category=payload.get("description_category"),
        )


# ----------------------------------------------------------------- normalization


def _field(activity: Any, *names: str) -> Any:
    for name in names:
        if isinstance(activity, Mapping):
            value = activity.get(name)
        else:
            value = getattr(activity, name, None)
        if value is not None:
            return value
    return None


def _decimal(value: Any, *, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must be Decimal, int, or str")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{name} is not a valid decimal") from None
    else:
        raise ValueError(f"{name} must be Decimal, int, or str")
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("activity timestamp must be timezone-aware")
        return value.astimezone(UTC)
    if isinstance(value, Date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("activity has no usable timestamp")
    text = value.strip()
    if len(text) == 10:
        try:
            parsed_date = Date.fromisoformat(text)
        except ValueError:
            raise ValueError("activity date is invalid") from None
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("activity timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("activity timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _safe(value: Any, pattern: re.Pattern[str], *, upper: bool = False) -> str | None:
    """Return ``value`` only if it is short, plain, and identifier-shaped."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if upper:
        text = text.upper()
    return text if pattern.fullmatch(text) else None


def normalize_broker_activity(activity: Mapping[str, Any] | object) -> BrokerActivityRecord:
    """Reduce one provider activity to its allowlisted, classified record.

    Raises ``ValueError`` when the activity id, type, or timestamp is unusable.
    Optional text that is not identifier-shaped is dropped rather than kept.
    """
    activity_id = _safe(_field(activity, "id", "activity_id"), _SAFE_ID)
    if activity_id is None:
        raise ValueError("activity id is missing or malformed")
    raw_type = _field(activity, "activity_type", "type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValueError("activity type is missing")
    activity_type = raw_type.strip().upper()
    category, fee_kind = classify_activity(activity_type)
    if category is ActivityCategory.OTHER and activity_type in ("FILL", "PARTIAL_FILL"):
        category = ActivityCategory.TRADE_FILL
    occurred_at = _timestamp(
        _field(activity, "transaction_time", "executed_at", "at", "date", "settle_date",
               "occurred_at")
    )

    net_amount = _decimal(_field(activity, "net_amount", "amount"), name="net_amount")
    explicit_fee = _decimal(_field(activity, "fee"), name="fee")
    fee_amount: Decimal | None = None
    if category is ActivityCategory.FEE:
        if net_amount is not None:
            fee_amount = -net_amount
        elif explicit_fee is not None:
            fee_amount = explicit_fee
    elif category is ActivityCategory.TRADE_FILL and explicit_fee is not None:
        fee_amount = explicit_fee

    subtype = _field(activity, "activity_sub_type", "activity_subtype", "sub_type")
    return BrokerActivityRecord(
        activity_id=activity_id,
        activity_type=activity_type,
        category=category,
        occurred_at=occurred_at,
        fee_kind=fee_kind,
        order_id=_safe(_field(activity, "order_id"), _SAFE_ID),
        client_order_id=_safe(_field(activity, "client_order_id"), _SAFE_ID),
        symbol=_safe(_field(activity, "symbol"), _SAFE_SYMBOL, upper=True),
        qty=_decimal(_field(activity, "qty", "quantity"), name="qty"),
        price=_decimal(_field(activity, "price"), name="price"),
        net_amount=net_amount,
        fee_amount=fee_amount,
        description_category=_safe(subtype, _SAFE_TOKEN, upper=True),
    )


# ---------------------------------------------------------------------- ingestion


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one ingestion call; ``rejected`` holds ids only, never payloads."""

    new_records: tuple[BrokerActivityRecord, ...] = ()
    duplicates: int = 0
    rejected: tuple[str, ...] = ()
    cash_events_added: int = 0


_CASH_CATEGORIES = frozenset(
    {ActivityCategory.EXTERNAL_FLOW, ActivityCategory.INCOME, ActivityCategory.CORPORATE_ACTION}
)


def ingest_broker_activities(
    writer: ScopedWriter,
    clock: Clock,
    activities: Iterable[Mapping[str, Any] | object] | None,
) -> IngestResult:
    """Idempotently persist filtered activities through the scoped writer.

    Records are keyed by broker activity id: re-ingesting an id adds neither an
    activity row nor a cash event.  Cash-bearing categories (external flows,
    income, corporate actions) also become ``cash_events`` so reports can keep
    them apart from trading P&L.  ``None`` means no activity source is wired.
    """
    if writer.environment not in _BROKER_ENVIRONMENTS:
        raise ModeIsolationError(
            f"broker activity ingestion refuses {writer.environment.value} databases",
            code="activity_mode_mismatch",
        )
    if activities is None:
        return IngestResult()
    orders = BrokerOrderRepository(writer, clock)
    cash = CashEquityRepository(writer, clock)
    new_records: list[BrokerActivityRecord] = []
    rejected: list[str] = []
    duplicates = 0
    cash_added = 0
    seen: set[str] = set()
    for item in activities:
        try:
            record = normalize_broker_activity(item)
        except ValueError:
            candidate = _safe(_field(item, "id", "activity_id"), _SAFE_ID)
            rejected.append(candidate or "<unidentified>")
            continue
        if record.activity_id in seen:
            duplicates += 1
            continue
        seen.add(record.activity_id)
        # Both writes are idempotent per activity id, so a retry after a crash
        # between them completes the missing half without duplicating the other.
        if (
            record.category in _CASH_CATEGORIES
            and record.net_amount is not None
            and cash.append_cash_event(
                record.category.value,
                record.net_amount,
                record.occurred_at,
                external_activity_id=record.activity_id,
                description=record.description_category or "",
            )
        ):
            cash_added += 1
        if orders.record_activity(
            record.activity_id,
            record.activity_type,
            record.occurred_at,
            record.to_payload(),
        ):
            new_records.append(record)
        else:
            duplicates += 1
    return IngestResult(tuple(new_records), duplicates, tuple(rejected), cash_added)


def load_ingested_activities(
    conn: Any,
    account_scope_id: str,
    *,
    until: datetime | None = None,
) -> list[BrokerActivityRecord]:
    """Read one scope's stored activity records, oldest first.

    ``conn`` is any read connection exposing ``execute`` (for example the
    ``ReadStore.snapshot`` view).  Only the given scope is ever queried.
    """
    sql = (
        "SELECT payload_json FROM broker_activities WHERE account_scope_id = ?"
    )
    args: list[Any] = [account_scope_id]
    if until is not None:
        sql += " AND occurred_at <= ?"
        args.append(until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    sql += " ORDER BY occurred_at, rowid"
    records: list[BrokerActivityRecord] = []
    for (payload_json,) in conn.execute(sql, tuple(args)).fetchall():
        payload = json.loads(payload_json)
        if "category" in payload and "activity_id" in payload:
            records.append(BrokerActivityRecord.from_payload(payload))
    return records


# --------------------------------------------------------------------- attribution


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """An explicitly configured fee model used only to ESTIMATE fees.

    The formula matches the shadow fill simulator: per-fill plus per-share plus
    basis points of notional, floored at ``minimum``, rounded up to ``quantum``.
    """

    name: str
    per_fill: Decimal = _ZERO
    per_share: Decimal = _ZERO
    bps: Decimal = _ZERO
    minimum: Decimal = _ZERO
    quantum: Decimal = _CENT

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("fee schedule name must be a non-empty string")
        for field_name in ("per_fill", "per_share", "bps", "minimum"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, Decimal)
                or not value.is_finite()
                or value < _ZERO
            ):
                raise ValueError(f"{field_name} must be a finite Decimal >= 0")
        if (
            not isinstance(self.quantum, Decimal)
            or not self.quantum.is_finite()
            or self.quantum <= _ZERO
        ):
            raise ValueError("quantum must be a finite Decimal > 0")

    @classmethod
    def from_assumptions(cls, name: str, assumptions: Any) -> FeeSchedule:
        """Build from an object with ``fee_per_fill/fee_per_share/fee_bps`` attributes.

        Works for the shadow ``FillAssumptions`` and ``ScenarioConfig``;
        ``fee_minimum`` and ``fee_quantum`` are optional.
        """
        return cls(
            name=name,
            per_fill=assumptions.fee_per_fill,
            per_share=assumptions.fee_per_share,
            bps=assumptions.fee_bps,
            minimum=getattr(assumptions, "fee_minimum", _ZERO),
            quantum=getattr(assumptions, "fee_quantum", _CENT),
        )

    def estimate(self, quantity: Decimal, price: Decimal) -> Decimal:
        fee = self.per_fill + self.per_share * quantity + quantity * price * self.bps / _BPS
        return max(fee, self.minimum).quantize(self.quantum, rounding=ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class OrderFee:
    """Fee for one order.  ``amount`` is ``None`` exactly when UNAVAILABLE."""

    broker_order_id: str
    status: FeeStatus
    amount: Decimal | None
    source: str
    activity_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.amount is None) != (self.status is FeeStatus.UNAVAILABLE):
            raise ValueError("amount is None if and only if the fee is UNAVAILABLE")
        if self.status is FeeStatus.CONFIRMED and not self.activity_ids:
            raise ValueError("a CONFIRMED fee must cite its broker activities")


def attribute_order_fees(
    fills: Iterable[Fill],
    activities: Iterable[BrokerActivityRecord] = (),
    *,
    schedule: FeeSchedule | None = None,
    environment: AccountEnvironment = AccountEnvironment.PAPER,
    client_order_ids: Mapping[str, str] | None = None,
) -> tuple[OrderFee, ...]:
    """Attribute a fee status and amount to every order that has fills.

    ``client_order_ids`` maps client order id to broker order id so an activity
    that names only the client id can still link explicitly.  Fee activities
    that name no known order are not attributed here (they remain account-level
    charges).  For SHADOW, ``activities`` and ``schedule`` are ignored: the fee
    is the scenario's simulated fee recorded on each fill, labeled ESTIMATED.
    """
    by_order: dict[str, list[Fill]] = {}
    for fill in fills:
        by_order.setdefault(fill.broker_order_id, []).append(fill)

    if environment is AccountEnvironment.SHADOW:
        return tuple(
            OrderFee(
                order_id,
                FeeStatus.ESTIMATED,
                sum((f.fee for f in order_fills), _ZERO),
                "shadow_fee_model",
            )
            for order_id, order_fills in by_order.items()
        )
    if environment not in _BROKER_ENVIRONMENTS:
        raise ModeIsolationError(
            f"fee attribution does not support {environment.value}", code="fee_mode_unsupported"
        )

    links: dict[str, dict[str, BrokerActivityRecord]] = {}
    for record in activities:
        if not record.is_fee_charge:
            continue
        target = record.order_id
        if target is None and record.client_order_id is not None and client_order_ids:
            target = client_order_ids.get(record.client_order_id)
        if target is not None and target in by_order:
            links.setdefault(target, {})[record.activity_id] = record

    result: list[OrderFee] = []
    for order_id, order_fills in by_order.items():
        linked = links.get(order_id)
        if linked:
            amount = sum((r.fee_amount for r in linked.values() if r.fee_amount is not None), _ZERO)
            result.append(
                OrderFee(order_id, FeeStatus.CONFIRMED, amount, "broker_activity", tuple(linked))
            )
        elif schedule is not None:
            amount = sum((schedule.estimate(f.quantity, f.price) for f in order_fills), _ZERO)
            result.append(
                OrderFee(order_id, FeeStatus.ESTIMATED, amount, f"fee_schedule:{schedule.name}")
            )
        else:
            result.append(OrderFee(order_id, FeeStatus.UNAVAILABLE, None, "none"))
    return tuple(result)

