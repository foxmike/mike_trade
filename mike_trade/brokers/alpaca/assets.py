"""Read-only Alpaca Trading API asset queries plus Decimal precision helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Mapping

from mike_trade.brokers.alpaca.auth import AlpacaEnvironment

__all__ = [
    "AlpacaAssetService",
    "AlpacaAsset",
    "AssetEligibility",
    "quantize_quantity",
    "quantize_price",
]


def _to_decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a decimal string, int, or Decimal")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} must be a non-empty numeric string")
        try:
            result = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"{field} is not a valid decimal: {value!r}") from None
    else:
        raise ValueError(
            f"{field} must be Decimal, int, or str; got {type(value).__name__}"
        )
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _optional_decimal(value: Any, *, field: str) -> Decimal | None:
    if value is None:
        return None
    return _to_decimal(value, field=field)


def _require_nonempty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be bool")
    return value


@dataclass(frozen=True, slots=True)
class AssetEligibility:
    """Eligibility verdict with explicit reasons when ineligible."""

    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlpacaAsset:
    """Normalized Alpaca asset tagged with its source environment."""

    environment: AlpacaEnvironment
    fingerprint: str
    asset_id: str
    symbol: str
    exchange: str
    asset_class: str
    status: str
    tradable: bool
    marginable: bool
    shortable: bool
    easy_to_borrow: bool
    fractionable: bool
    min_order_size: Decimal | None = None
    min_trade_increment: Decimal | None = None
    price_increment: Decimal | None = None

    def eligibility(self) -> AssetEligibility:
        reasons: list[str] = []
        if not self.tradable:
            reasons.append("non_tradable")
        if self.status.lower() != "active":
            reasons.append(f"inactive_status:{self.status}")
        if self.asset_class.lower() != "us_equity":
            reasons.append(f"non_us_equity_class:{self.asset_class}")
        if reasons:
            return AssetEligibility(eligible=False, reasons=tuple(reasons))
        return AssetEligibility(eligible=True, reasons=())


def _parse_asset(
    data: Mapping[str, Any], *, environment: AlpacaEnvironment, fingerprint: str
) -> AlpacaAsset:
    return AlpacaAsset(
        environment=environment,
        fingerprint=fingerprint,
        asset_id=_require_nonempty(data.get("id"), field="id"),
        symbol=_require_nonempty(data.get("symbol"), field="symbol").upper(),
        exchange=_require_nonempty(data.get("exchange"), field="exchange"),
        asset_class=_require_nonempty(
            data.get("class", data.get("asset_class")), field="class"
        ),
        status=_require_nonempty(data.get("status"), field="status"),
        tradable=_require_bool(data.get("tradable"), field="tradable"),
        marginable=_require_bool(data.get("marginable"), field="marginable"),
        shortable=_require_bool(data.get("shortable"), field="shortable"),
        easy_to_borrow=_require_bool(
            data.get("easy_to_borrow"), field="easy_to_borrow"
        ),
        fractionable=_require_bool(data.get("fractionable"), field="fractionable"),
        min_order_size=_optional_decimal(
            data.get("min_order_size"), field="min_order_size"
        ),
        min_trade_increment=_optional_decimal(
            data.get("min_trade_increment"), field="min_trade_increment"
        ),
        price_increment=_optional_decimal(
            data.get("price_increment"), field="price_increment"
        ),
    )


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    units = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return units * increment


def _check_decimal_input(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be Decimal, int, or str")
    return _to_decimal(value, field=field)


def quantize_quantity(
    quantity: Decimal | int | str, asset: AlpacaAsset, *, strict: bool = False
) -> Decimal:
    """Floor ``quantity`` to the asset's ``min_trade_increment`` (never round up).

    Raises when the value is non-positive, below ``min_order_size``, floors
    to zero, or (with ``strict=True``) is not already an exact multiple of
    the increment. Accepts only Decimal/int/str; float is rejected.
    """
    if not isinstance(asset, AlpacaAsset):
        raise TypeError("asset must be an AlpacaAsset")
    value = _check_decimal_input(quantity, field="quantity")
    if value <= 0:
        raise ValueError("quantity must be > 0")
    increment = asset.min_trade_increment
    if increment is not None:
        if increment <= 0:
            raise ValueError("asset min_trade_increment must be > 0")
        floored = _floor_to_increment(value, increment)
        if strict and floored != value:
            raise ValueError(
                f"quantity {value!s} violates min_trade_increment {increment!s}"
            )
        value = floored
        if value <= 0:
            raise ValueError(
                f"quantity violates min_trade_increment {increment!s}"
            )
    minimum = asset.min_order_size
    if minimum is not None and value < minimum:
        raise ValueError(
            f"quantity {value!s} is below min_order_size {minimum!s}"
        )
    return value


def quantize_price(
    price: Decimal | int | str, asset: AlpacaAsset, *, strict: bool = False
) -> Decimal:
    """Floor ``price`` to the asset's ``price_increment`` (never round up).

    Raises when the value is non-positive, floors to zero, or (with
    ``strict=True``) is not already an exact multiple of the increment.
    """
    if not isinstance(asset, AlpacaAsset):
        raise TypeError("asset must be an AlpacaAsset")
    value = _check_decimal_input(price, field="price")
    if value <= 0:
        raise ValueError("price must be > 0")
    increment = asset.price_increment
    if increment is not None:
        if increment <= 0:
            raise ValueError("asset price_increment must be > 0")
        floored = _floor_to_increment(value, increment)
        if strict and floored != value:
            raise ValueError(
                f"price {value!s} violates price_increment {increment!s}"
            )
        value = floored
        if value <= 0:
            raise ValueError(f"price violates price_increment {increment!s}")
    return value


class AlpacaAssetService:
    """Read-only asset lookup/listing over an injected GET client."""

    def __init__(
        self,
        client: Any,
        *,
        environment: AlpacaEnvironment,
        fingerprint: str,
    ) -> None:
        if not isinstance(environment, AlpacaEnvironment):
            raise TypeError("environment must be an AlpacaEnvironment")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("fingerprint must be a non-empty string")
        if not hasattr(client, "get") or not callable(client.get):
            raise TypeError("client must expose a get(path, params) method")
        self._client = client
        self._environment = environment
        self._fingerprint = fingerprint

    @property
    def environment(self) -> AlpacaEnvironment:
        return self._environment

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def get_asset(self, symbol_or_id: str) -> AlpacaAsset:
        cleaned = _require_nonempty(symbol_or_id, field="symbol_or_id")
        data = self._client.get(f"/v2/assets/{cleaned}", params=None)
        if not isinstance(data, Mapping):
            raise ValueError("GET /v2/assets/{id} must return a JSON object")
        return _parse_asset(
            data, environment=self._environment, fingerprint=self._fingerprint
        )

    def list_assets(self, params: Mapping[str, Any] | None = None) -> list[AlpacaAsset]:
        data = self._client.get(
            "/v2/assets", params=dict(params) if params else None
        )
        if not isinstance(data, list):
            raise ValueError("GET /v2/assets must return a JSON array")
        return [
            _parse_asset(
                item, environment=self._environment, fingerprint=self._fingerprint
            )
            for item in data
        ]

    def check_eligibility(self, asset: AlpacaAsset) -> AssetEligibility:
        if not isinstance(asset, AlpacaAsset):
            raise TypeError("asset must be an AlpacaAsset")
        if asset.environment is not self._environment:
            raise ValueError("asset environment does not match this service")
        if asset.fingerprint != self._fingerprint:
            raise ValueError("asset fingerprint does not match this service")
        return asset.eligibility()
