"""Bounded, read-only normalization of Alpaca account activities.

The service deliberately accepts an injected ``get`` client.  It therefore
contains no networking of its own and is safe to exercise with fixture-only
clients.  Numeric provider fields are converted to :class:`~decimal.Decimal`
and every returned activity is scoped to an Alpaca environment and account
fingerprint.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as Date
from decimal import Decimal, InvalidOperation
from typing import Any

from mike_trade.brokers.alpaca.auth import AlpacaEnvironment

__all__ = [
    "AlpacaActivityService",
    "FillActivity",
    "FeeActivity",
    "CorporateActivity",
    "UnknownActivity",
    "activity_idempotency_key",
    "dedupe_activities",
]

_FEE_TYPES = frozenset(
    {
        "FEE",
        "CFEE",
        "PTC",
        "PTR",
        # Some feeds report the fee subtype directly as the activity type.
        "TAF",
        "REG",
        "SEC",
        "CAT",
        "ORF",
        "OCC",
        "NRV",
        "NRC",
        "LCT",
    }
)
_DIVIDEND_TYPES = frozenset(
    {
        "DIV",
        "CGD",
        "DIVCGL",
        "DIVCGS",
        "DIVFEE",
        "DIVFT",
        "DIVNRA",
        "DIVROC",
        "DIVTW",
        "DIVTXEX",
    }
)
_CORPORATE_TYPES = frozenset(
    {
        "SPLIT",
        "SSP",
        "SPIN",
        "SSO",
        "MA",
        "NC",
        "SC",
        "REO",
        "REORG",
        "ACATS",
        "FOPT",
        "OPCA",
        "OPASN",
        "OPEXP",
        "OPXRC",
    }
)


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(value: Any, *, field: str, optional: bool = False) -> Decimal | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field} is required")
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be Decimal, int, or str")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{field} is not a valid decimal") from None
    else:
        raise ValueError(f"{field} must be Decimal, int, or str")
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty timestamp or date")
    text = value.strip()
    if len(text) == 10:
        try:
            parsed_date = Date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{field} is not a valid date") from None
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} is not a valid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _activity_time(data: Mapping[str, Any]) -> datetime:
    for name in ("transaction_time", "executed_at", "at", "date", "settle_date"):
        if data.get(name) is not None:
            return _timestamp(data[name], field=name)
    raise ValueError("activity has no transaction_time or date")


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field=field)


def activity_idempotency_key(
    activity_id: str,
    environment: AlpacaEnvironment | str,
    fingerprint: str | None = None,
    *,
    account_fingerprint: str | None = None,
) -> str:
    """Return a stable scope-qualified key for one broker activity ID."""

    broker_id = _require_text(activity_id, field="activity_id")
    raw_environment = getattr(environment, "value", environment)
    environment_name = _require_text(raw_environment, field="environment").upper()
    if environment_name not in {member.value for member in AlpacaEnvironment}:
        raise ValueError(f"unsupported Alpaca environment: {environment_name!r}")
    if fingerprint is not None and account_fingerprint is not None:
        if fingerprint != account_fingerprint:
            raise ValueError("fingerprint and account_fingerprint disagree")
    scope = fingerprint if fingerprint is not None else account_fingerprint
    resolved_fingerprint = _require_text(scope, field="fingerprint")
    # Length prefixes make the representation unambiguous while retaining the
    # broker ID and account scope for operator inspection.
    return (
        f"alpaca:{len(environment_name)}:{environment_name}:"
        f"{len(resolved_fingerprint)}:{resolved_fingerprint}:"
        f"{len(broker_id)}:{broker_id}"
    )


@dataclass(frozen=True, slots=True)
class FillActivity:
    environment: AlpacaEnvironment
    fingerprint: str
    idempotency_key: str
    id: str
    order_id: str
    symbol: str
    side: str
    qty: Decimal
    cum_qty: Decimal
    leaves_qty: Decimal
    price: Decimal
    transaction_time: datetime
    type: str
    activity_type: str = "FILL"
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FeeActivity:
    environment: AlpacaEnvironment
    fingerprint: str
    idempotency_key: str
    id: str
    activity_type: str
    amount: Decimal
    transaction_time: datetime
    symbol: str | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    description: str | None = None
    activity_sub_type: str | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CorporateActivity:
    """A dividend or position-changing corporate action.

    Corporate actions have heterogeneous payloads.  Common numeric values are
    normalized explicitly and ``amounts`` preserves any additional documented
    amount/quantity/price fields as Decimal values.
    """

    environment: AlpacaEnvironment
    fingerprint: str
    idempotency_key: str
    id: str
    activity_type: str
    transaction_time: datetime
    symbol: str | None = None
    amount: Decimal | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    per_share_amount: Decimal | None = None
    description: str | None = None
    activity_sub_type: str | None = None
    is_dividend: bool = False
    amounts: Mapping[str, Decimal] | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class UnknownActivity:
    environment: AlpacaEnvironment
    fingerprint: str
    idempotency_key: str
    id: str
    activity_type: str
    transaction_time: datetime | None
    raw: Mapping[str, Any]


NormalizedActivity = FillActivity | FeeActivity | CorporateActivity | UnknownActivity


def dedupe_activities(activities: Iterable[NormalizedActivity]) -> list[NormalizedActivity]:
    """Pure, stable-order deduplication by normalized idempotency key."""

    result: list[NormalizedActivity] = []
    seen: set[str] = set()
    for activity in activities:
        key = activity.idempotency_key
        if key not in seen:
            seen.add(key)
            result.append(activity)
    return result


def _all_decimal_fields(data: Mapping[str, Any]) -> dict[str, Decimal]:
    markers = ("amount", "qty", "quantity", "price", "cash", "rate", "ratio", "fee")
    values: dict[str, Decimal] = {}
    for name, value in data.items():
        if value is None or not any(marker in name.lower() for marker in markers):
            continue
        try:
            parsed = _decimal(value, field=name, optional=True)
        except ValueError:
            continue
        if parsed is not None:
            values[name] = parsed
    return values


class AlpacaActivityService:
    """Read and normalize bounded pages from ``GET /v2/account/activities``."""

    def __init__(
        self,
        client: Any,
        *,
        environment: AlpacaEnvironment | None = None,
        fingerprint: str | None = None,
        page_size: int = 100,
        max_pages: int = 10,
    ) -> None:
        if not callable(getattr(client, "get", None)):
            raise TypeError("client must expose get(path, params=...)")
        resolved_environment = environment or getattr(client, "environment", None)
        if not isinstance(resolved_environment, AlpacaEnvironment):
            raise TypeError("environment must be supplied or exposed by the client")
        resolved_fingerprint = fingerprint or getattr(client, "fingerprint", None)
        if not isinstance(resolved_fingerprint, str) or not resolved_fingerprint.strip():
            raise ValueError("fingerprint must be supplied or exposed by the client")
        self._validate_page_size(page_size)
        self._validate_max_pages(max_pages)
        self._client = client
        self._environment = resolved_environment
        self._fingerprint = resolved_fingerprint.strip()
        self._page_size = page_size
        self._max_pages = max_pages

    @staticmethod
    def _validate_page_size(page_size: int) -> None:
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ValueError("page_size must be an int from 1 through 100")

    @staticmethod
    def _validate_max_pages(max_pages: int) -> None:
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be an int >= 1")

    @property
    def environment(self) -> AlpacaEnvironment:
        return self._environment

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def normalize_activity(self, data: Mapping[str, Any]) -> NormalizedActivity:
        if not isinstance(data, Mapping):
            raise ValueError("activity must be a JSON object")
        activity_id = _require_text(data.get("id"), field="id")
        activity_type = _require_text(data.get("activity_type"), field="activity_type").upper()
        idempotency_key = activity_idempotency_key(
            activity_id, self._environment, self._fingerprint
        )
        raw = dict(data)
        if activity_type == "FILL":
            fill_type = _require_text(data.get("type"), field="type").lower()
            if fill_type not in {"fill", "partial_fill"}:
                raise ValueError(f"unsupported FILL type: {fill_type!r}")
            return FillActivity(
                environment=self._environment,
                fingerprint=self._fingerprint,
                idempotency_key=idempotency_key,
                id=activity_id,
                order_id=_require_text(data.get("order_id"), field="order_id"),
                symbol=_require_text(data.get("symbol"), field="symbol").upper(),
                side=_require_text(data.get("side"), field="side").lower(),
                qty=_decimal(data.get("qty"), field="qty"),  # type: ignore[arg-type]
                cum_qty=_decimal(data.get("cum_qty"), field="cum_qty"),  # type: ignore[arg-type]
                leaves_qty=_decimal(data.get("leaves_qty"), field="leaves_qty"),  # type: ignore[arg-type]
                price=_decimal(data.get("price"), field="price"),  # type: ignore[arg-type]
                transaction_time=_activity_time(data),
                type=fill_type,
                raw=raw,
            )

        amount = data.get("net_amount", data.get("amount", data.get("fee")))
        if activity_type in _FEE_TYPES:
            return FeeActivity(
                environment=self._environment,
                fingerprint=self._fingerprint,
                idempotency_key=idempotency_key,
                id=activity_id,
                activity_type=activity_type,
                amount=_decimal(amount, field="net_amount"),  # type: ignore[arg-type]
                transaction_time=_activity_time(data),
                symbol=_optional_text(data.get("symbol"), field="symbol"),
                qty=_decimal(data.get("qty"), field="qty", optional=True),
                price=_decimal(data.get("price"), field="price", optional=True),
                description=_optional_text(data.get("description"), field="description"),
                activity_sub_type=_optional_text(
                    data.get("activity_sub_type", data.get("activity_subtype")),
                    field="activity_sub_type",
                ),
                raw=raw,
            )

        if activity_type in _DIVIDEND_TYPES | _CORPORATE_TYPES:
            symbol = _optional_text(data.get("symbol"), field="symbol")
            return CorporateActivity(
                environment=self._environment,
                fingerprint=self._fingerprint,
                idempotency_key=idempotency_key,
                id=activity_id,
                activity_type=activity_type,
                transaction_time=_activity_time(data),
                symbol=None if symbol is None else symbol.upper(),
                amount=_decimal(amount, field="net_amount", optional=True),
                qty=_decimal(data.get("qty"), field="qty", optional=True),
                price=_decimal(data.get("price"), field="price", optional=True),
                per_share_amount=_decimal(
                    data.get("per_share_amount"), field="per_share_amount", optional=True
                ),
                description=_optional_text(data.get("description"), field="description"),
                activity_sub_type=_optional_text(
                    data.get("activity_sub_type", data.get("activity_subtype")),
                    field="activity_sub_type",
                ),
                is_dividend=activity_type in _DIVIDEND_TYPES,
                amounts=_all_decimal_fields(data),
                raw=raw,
            )

        return UnknownActivity(
            environment=self._environment,
            fingerprint=self._fingerprint,
            idempotency_key=idempotency_key,
            id=activity_id,
            activity_type=activity_type,
            transaction_time=(
                _activity_time(data)
                if any(
                    data.get(name) is not None
                    for name in ("transaction_time", "executed_at", "at", "date", "settle_date")
                )
                else None
            ),
            raw=raw,
        )

    def list_activities(
        self,
        *,
        activity_types: str | Sequence[str] | None = None,
        activity_type: str | None = None,
        date: str | Date | datetime | None = None,
        after: str | Date | datetime | None = None,
        until: str | Date | datetime | None = None,
        direction: str | None = None,
        order_id: str | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
    ) -> list[NormalizedActivity]:
        """Read activities using page tokens, stopping at the hard page cap."""

        if activity_types is not None and activity_type is not None:
            raise ValueError("pass activity_types or activity_type, not both")
        resolved_page_size = self._page_size if page_size is None else page_size
        resolved_max_pages = self._max_pages if max_pages is None else max_pages
        self._validate_page_size(resolved_page_size)
        self._validate_max_pages(resolved_max_pages)
        if resolved_max_pages > self._max_pages:
            raise ValueError(
                f"max_pages cannot exceed the configured hard cap of {self._max_pages}"
            )
        params: dict[str, Any] = {"page_size": resolved_page_size}
        chosen_types: str | Sequence[str] | None = (
            activity_types if activity_types is not None else activity_type
        )
        if chosen_types is not None:
            if isinstance(chosen_types, str):
                normalized_types = _require_text(chosen_types, field="activity_types").upper()
            else:
                normalized_types = ",".join(
                    _require_text(item, field="activity_types item").upper()
                    for item in chosen_types
                )
                if not normalized_types:
                    raise ValueError("activity_types must not be empty")
            params["activity_types"] = normalized_types
        for name, value in (("date", date), ("after", after), ("until", until)):
            if value is not None:
                params[name] = self._format_filter(value, field=name)
        if direction is not None:
            normalized_direction = _require_text(direction, field="direction").lower()
            if normalized_direction not in {"asc", "desc"}:
                raise ValueError("direction must be 'asc' or 'desc'")
            params["direction"] = normalized_direction
        if order_id is not None:
            params["order_id"] = _require_text(order_id, field="order_id")

        result: list[NormalizedActivity] = []
        previous_token: str | None = None
        for _page_number in range(resolved_max_pages):
            response = self._client.get("/v2/account/activities", params=dict(params))
            items, explicit_token = self._page(response)
            result.extend(self.normalize_activity(item) for item in items)
            if explicit_token is not None:
                next_token = explicit_token
            elif len(items) < resolved_page_size:
                break
            elif items:
                next_token = _require_text(items[-1].get("id"), field="last activity id")
            else:
                break
            if next_token == previous_token:
                raise ValueError("activity pagination repeated a page token")
            previous_token = next_token
            params["page_token"] = next_token
        return result

    @staticmethod
    def _format_filter(value: str | Date | datetime, *, field: str) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field} datetime must be timezone-aware")
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(value, Date):
            return value.isoformat()
        return _require_text(value, field=field)

    @staticmethod
    def _page(response: Any) -> tuple[list[Mapping[str, Any]], str | None]:
        token: str | None = None
        if isinstance(response, list):
            items = response
        elif isinstance(response, Mapping):
            candidate = response.get("activities", response.get("data"))
            if not isinstance(candidate, list):
                raise ValueError("activity response object must contain an activities array")
            items = candidate
            raw_token = response.get("next_page_token")
            if raw_token is not None:
                token = _require_text(raw_token, field="next_page_token")
        else:
            raise ValueError("GET /v2/account/activities must return a JSON array")
        if not all(isinstance(item, Mapping) for item in items):
            raise ValueError("account activity entries must be JSON objects")
        return items, token
