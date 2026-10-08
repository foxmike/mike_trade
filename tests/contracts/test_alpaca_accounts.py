"""Fixture-only contracts for Alpaca account/position/clock/asset queries.

No network access and no credentials: a fake HTTP client replays
handwritten provider JSON.
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from typing import Any, Mapping

import pytest

from mike_trade.brokers.alpaca.account import (
    AccountSafetyReason,
    AlpacaAccountService,
    AlpacaClock,
    AlpacaPosition,
    evaluate_account_flags,
)
from mike_trade.brokers.alpaca.assets import (
    AlpacaAsset,
    AlpacaAssetService,
    quantize_price,
    quantize_quantity,
)
from mike_trade.brokers.alpaca.auth import AlpacaEnvironment

FP_PAPER = "fp-paper-001"
FP_LIVE = "fp-live-002"


class FakeNotFound(Exception):
    def __init__(self, message: str = "not found") -> None:
        super().__init__(message)
        self.status_code = 404


class FakeHttpClient:
    """Minimal get(path, params) double replaying canned payloads."""

    def __init__(
        self, routes: dict[str, Any], *, record: list[tuple[str, Any]] | None = None
    ) -> None:
        self._routes = routes
        self.record = record if record is not None else []

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        self.record.append((path, params))
        if path not in self._routes:
            raise FakeNotFound(f"no route for {path}")
        action = self._routes[path]
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(path, params)
        return action


def account_fixture(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "acct-123",
        "account_number": "PA3ABC01",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "100000.25",
        "equity": "100050.75",
        "last_equity": "99000.00",
        "buying_power": "200000.50",
        "regt_buying_power": "200000.50",
        "daytrading_buying_power": "400000.00",
        "non_marginable_buying_power": "100000.25",
        "portfolio_value": "100050.75",
        "multiplier": "4",
        "pattern_day_trader": False,
        "trading_blocked": False,
        "transfers_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
        "shorting_enabled": True,
        "daytrade_count": 0,
    }
    base.update(overrides)
    return base


def position_fixture(symbol: str = "AAPL", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "asset_id": "asset-uuid-1",
        "symbol": symbol,
        "exchange": "NASDAQ",
        "asset_class": "us_equity",
        "side": "long",
        "qty": "10",
        "qty_available": "8",
        "avg_entry_price": "150.25",
        "market_value": "1550.00",
        "cost_basis": "1502.50",
        "unrealized_pl": "47.50",
        "current_price": "155.00",
    }
    base.update(overrides)
    return base


def clock_fixture(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "timestamp": "2024-01-02T15:00:00Z",
        "is_open": True,
        "next_open": "2024-01-03T14:30:00Z",
        "next_close": "2024-01-03T21:00:00Z",
    }
    base.update(overrides)
    return base


def asset_fixture(symbol: str = "AAPL", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "asset-uuid-1",
        "symbol": symbol,
        "exchange": "NASDAQ",
        "class": "us_equity",
        "status": "active",
        "tradable": True,
        "marginable": True,
        "shortable": True,
        "easy_to_borrow": True,
        "fractionable": True,
        "min_order_size": "0.0001",
        "min_trade_increment": "0.0001",
        "price_increment": "0.01",
    }
    base.update(overrides)
    return base


def account_service(routes: dict[str, Any]) -> AlpacaAccountService:
    return AlpacaAccountService(
        FakeHttpClient(routes),
        environment=AlpacaEnvironment.PAPER,
        fingerprint=FP_PAPER,
    )


def asset_service(routes: dict[str, Any]) -> AlpacaAssetService:
    return AlpacaAssetService(
        FakeHttpClient(routes),
        environment=AlpacaEnvironment.PAPER,
        fingerprint=FP_PAPER,
    )


# -- account ---------------------------------------------------------------


def test_account_money_fields_are_decimal() -> None:
    svc = account_service({"/v2/account": account_fixture()})
    snap = svc.get_account()
    for field in (
        "cash",
        "equity",
        "buying_power",
        "regt_buying_power",
        "daytrading_buying_power",
        "non_marginable_buying_power",
    ):
        value = getattr(snap, field)
        assert isinstance(value, Decimal), field
        assert not isinstance(value, float)
    assert snap.cash == Decimal("100000.25")
    assert snap.equity == Decimal("100050.75")
    assert snap.buying_power == Decimal("200000.50")
    assert snap.regt_buying_power == Decimal("200000.50")
    assert snap.daytrading_buying_power == Decimal("400000.00")
    assert snap.non_marginable_buying_power == Decimal("100000.25")
    assert snap.last_equity == Decimal("99000.00")
    assert snap.portfolio_value == Decimal("100050.75")
    assert snap.multiplier == Decimal("4")
    assert snap.account_id == "acct-123"
    assert snap.account_number == "PA3ABC01"
    assert snap.status == "ACTIVE"
    assert snap.currency == "USD"
    assert snap.daytrade_count == 0


def test_account_rejects_float_money() -> None:
    svc = account_service({"/v2/account": account_fixture(cash=12.34)})
    with pytest.raises(ValueError):
        svc.get_account()


def test_clean_account_has_no_unsafe_reasons() -> None:
    svc = account_service({"/v2/account": account_fixture()})
    snap = svc.get_account()
    assert evaluate_account_flags(snap) == ()
    assert snap.unsafe_reasons == ()


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"trading_blocked": True}, AccountSafetyReason.TRADING_BLOCKED),
        ({"account_blocked": True}, AccountSafetyReason.ACCOUNT_BLOCKED),
        (
            {"trade_suspended_by_user": True},
            AccountSafetyReason.TRADE_SUSPENDED_BY_USER,
        ),
        ({"status": "SUSPENDED"}, AccountSafetyReason.NON_ACTIVE_STATUS),
        ({"pattern_day_trader": True}, AccountSafetyReason.PATTERN_DAY_TRADER),
    ],
)
def test_unsafe_flags_detected(override: dict[str, Any], expected: AccountSafetyReason) -> None:
    svc = account_service({"/v2/account": account_fixture(**override)})
    snap = svc.get_account()
    reasons = evaluate_account_flags(snap)
    assert expected in reasons
    assert snap.unsafe_reasons == reasons


def test_multiple_unsafe_flags_all_reported() -> None:
    svc = account_service(
        {
            "/v2/account": account_fixture(
                trading_blocked=True, account_blocked=True, status="CLOSED"
            )
        }
    )
    reasons = evaluate_account_flags(svc.get_account())
    assert AccountSafetyReason.TRADING_BLOCKED in reasons
    assert AccountSafetyReason.ACCOUNT_BLOCKED in reasons
    assert AccountSafetyReason.NON_ACTIVE_STATUS in reasons


# -- positions -------------------------------------------------------------


def test_position_list_normalization() -> None:
    svc = account_service(
        {
            "/v2/positions": [
                position_fixture("AAPL"),
                position_fixture("MSFT", qty="2.5", unrealized_pl="-1.25"),
            ]
        }
    )
    positions = svc.list_positions()
    assert len(positions) == 2
    first = positions[0]
    assert isinstance(first, AlpacaPosition)
    assert first.symbol == "AAPL"
    assert first.qty == Decimal("10")
    assert isinstance(first.qty, Decimal)
    assert first.qty_available == Decimal("8")
    assert first.avg_entry_price == Decimal("150.25")
    assert first.market_value == Decimal("1550.00")
    assert first.cost_basis == Decimal("1502.50")
    assert first.unrealized_pl == Decimal("47.50")
    assert first.current_price == Decimal("155.00")
    assert positions[1].qty == Decimal("2.5")
    assert positions[1].unrealized_pl == Decimal("-1.25")


def test_single_position_lookup() -> None:
    svc = account_service({"/v2/positions/AAPL": position_fixture("AAPL")})
    pos = svc.get_position("aapl")
    assert pos is not None
    assert pos.symbol == "AAPL"
    assert pos.qty == Decimal("10")
    assert pos.current_price == Decimal("155.00")


def test_missing_position_returns_none() -> None:
    svc = account_service({"/v2/positions/ZZZ": FakeNotFound()})
    assert svc.get_position("ZZZ") is None


def test_position_float_rejected() -> None:
    svc = account_service({"/v2/positions": [position_fixture(qty=10.5)]})
    with pytest.raises(ValueError):
        svc.list_positions()


# -- clock -----------------------------------------------------------------


def test_clock_returns_utc_aware_datetimes() -> None:
    svc = account_service({"/v2/clock": clock_fixture()})
    clock = svc.get_clock()
    assert isinstance(clock, AlpacaClock)
    assert clock.is_open is True
    for field in ("timestamp", "next_open", "next_close"):
        value = getattr(clock, field)
        assert value.tzinfo is not None, field
        assert value.utcoffset() is not None, field
        assert value.astimezone(UTC) == value, field


def test_clock_with_offset_normalizes_to_utc() -> None:
    svc = account_service(
        {
            "/v2/clock": clock_fixture(
                timestamp="2024-01-02T10:00:00-05:00",
                next_open="2024-01-03T09:30:00-05:00",
                next_close="2024-01-03T16:00:00-05:00",
            )
        }
    )
    clock = svc.get_clock()
    assert clock.timestamp == clock.timestamp.astimezone(UTC)
    assert clock.timestamp.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_clock_rejects_naive_timestamp() -> None:
    svc = account_service({"/v2/clock": clock_fixture(timestamp="2024-01-02T15:00:00")})
    with pytest.raises(ValueError):
        svc.get_clock()


# -- assets ----------------------------------------------------------------


def test_asset_lookup_and_eligibility() -> None:
    svc = asset_service({"/v2/assets/AAPL": asset_fixture()})
    asset = svc.get_asset("AAPL")
    assert isinstance(asset, AlpacaAsset)
    assert asset.symbol == "AAPL"
    assert asset.tradable is True
    assert asset.fractionable is True
    assert asset.shortable is True
    assert asset.status == "active"
    assert asset.min_trade_increment == Decimal("0.0001")
    assert asset.price_increment == Decimal("0.01")
    verdict = svc.check_eligibility(asset)
    assert verdict.eligible is True
    assert verdict.reasons == ()
    assert asset.eligibility().eligible is True


def test_asset_list_with_filters_forwards_params() -> None:
    record: list[tuple[str, Any]] = []
    client = FakeHttpClient(
        {"/v2/assets": [asset_fixture("AAPL"), asset_fixture("MSFT")]}, record=record
    )
    svc = AlpacaAssetService(
        client, environment=AlpacaEnvironment.PAPER, fingerprint=FP_PAPER
    )
    assets = svc.list_assets({"status": "active", "asset_class": "us_equity"})
    assert [a.symbol for a in assets] == ["AAPL", "MSFT"]
    assert record[0][0] == "/v2/assets"
    assert record[0][1] == {"status": "active", "asset_class": "us_equity"}


@pytest.mark.parametrize(
    ("override", "reason_part"),
    [
        ({"tradable": False}, "non_tradable"),
        ({"status": "inactive"}, "inactive_status"),
        ({"class": "crypto"}, "non_us_equity_class"),
    ],
)
def test_ineligible_assets_report_reason(
    override: dict[str, Any], reason_part: str
) -> None:
    svc = asset_service({"/v2/assets/X": asset_fixture("X", **override)})
    asset = svc.get_asset("X")
    verdict = asset.eligibility()
    assert verdict.eligible is False
    assert any(reason_part in reason for reason in verdict.reasons)


def test_fractional_flag_surfaced() -> None:
    svc = asset_service({"/v2/assets/BRK.A": asset_fixture("BRK.A", fractionable=False)})
    asset = svc.get_asset("BRK.A")
    assert asset.fractionable is False
    assert asset.eligibility().eligible is True  # non-fractional can still be eligible


# -- precision helpers ------------------------------------------------------


def _precision_asset() -> AlpacaAsset:
    return AlpacaAsset(
        environment=AlpacaEnvironment.PAPER,
        fingerprint=FP_PAPER,
        asset_id="asset-uuid-1",
        symbol="AAPL",
        exchange="NASDAQ",
        asset_class="us_equity",
        status="active",
        tradable=True,
        marginable=True,
        shortable=True,
        easy_to_borrow=True,
        fractionable=True,
        min_order_size=Decimal("0.01"),
        min_trade_increment=Decimal("0.01"),
        price_increment=Decimal("0.01"),
    )


def test_quantize_quantity_floors_never_rounds_up() -> None:
    asset = _precision_asset()
    assert quantize_quantity(Decimal("1.239"), asset) == Decimal("1.23")
    assert quantize_quantity("1.239", asset) == Decimal("1.23")
    assert quantize_quantity(Decimal("1.23"), asset) == Decimal("1.23")


def test_quantize_price_floors_to_tick() -> None:
    asset = _precision_asset()
    assert quantize_price(Decimal("150.259"), asset) == Decimal("150.25")
    assert quantize_price("150.259", asset) == Decimal("150.25")


def test_quantize_rejects_below_minimum() -> None:
    asset = _precision_asset()
    with pytest.raises(ValueError):
        quantize_quantity(Decimal("0.005"), asset)
    with pytest.raises(ValueError):
        quantize_quantity(Decimal("-1"), asset)
    with pytest.raises(ValueError):
        quantize_quantity(0, asset)


def test_quantize_rejects_float_and_bad_increment() -> None:
    asset = _precision_asset()
    with pytest.raises(ValueError):
        quantize_quantity(1.23, asset)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        quantize_price(10.5, asset)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        quantize_price(Decimal("-5"), asset)
    with pytest.raises(ValueError):
        quantize_quantity(Decimal("1.235"), asset, strict=True)
    with pytest.raises(ValueError):
        quantize_price(Decimal("1.235"), asset, strict=True)


def test_quantize_without_increments_passes_through() -> None:
    bare = AlpacaAsset(
        environment=AlpacaEnvironment.PAPER,
        fingerprint=FP_PAPER,
        asset_id="a2",
        symbol="XYZ",
        exchange="NASDAQ",
        asset_class="us_equity",
        status="active",
        tradable=True,
        marginable=True,
        shortable=True,
        easy_to_borrow=True,
        fractionable=False,
        min_order_size=None,
        min_trade_increment=None,
        price_increment=None,
    )
    assert quantize_quantity(Decimal("1.234567"), bare) == Decimal("1.234567")
    assert quantize_price(Decimal("1.234567"), bare) == Decimal("1.234567")


# -- environment tagging -----------------------------------------------------


def test_every_object_carries_environment_and_fingerprint() -> None:
    acct_svc = account_service(
        {
            "/v2/account": account_fixture(),
            "/v2/positions": [position_fixture()],
            "/v2/positions/AAPL": position_fixture(),
            "/v2/clock": clock_fixture(),
        }
    )
    snap = acct_svc.get_account()
    assert snap.environment is AlpacaEnvironment.PAPER
    assert snap.fingerprint == FP_PAPER
    for pos in acct_svc.list_positions():
        assert pos.environment is AlpacaEnvironment.PAPER
        assert pos.fingerprint == FP_PAPER
    single = acct_svc.get_position("AAPL")
    assert single is not None
    assert single.environment is AlpacaEnvironment.PAPER
    assert single.fingerprint == FP_PAPER
    clock = acct_svc.get_clock()
    assert clock.environment is AlpacaEnvironment.PAPER
    assert clock.fingerprint == FP_PAPER

    live_client = FakeHttpClient({"/v2/assets/AAPL": asset_fixture()})
    live_svc = AlpacaAssetService(
        live_client, environment=AlpacaEnvironment.LIVE, fingerprint=FP_LIVE
    )
    asset = live_svc.get_asset("AAPL")
    assert asset.environment is AlpacaEnvironment.LIVE
    assert asset.fingerprint == FP_LIVE


def test_no_float_anywhere_in_normalized_decimals() -> None:
    svc = account_service(
        {
            "/v2/account": account_fixture(),
            "/v2/positions": [position_fixture()],
        }
    )
    snap = svc.get_account()
    for name in (
        "cash",
        "equity",
        "last_equity",
        "buying_power",
        "regt_buying_power",
        "daytrading_buying_power",
        "non_marginable_buying_power",
        "portfolio_value",
        "multiplier",
    ):
        assert not isinstance(getattr(snap, name), float), name
    for pos in svc.list_positions():
        for name in (
            "qty",
            "qty_available",
            "avg_entry_price",
            "market_value",
            "cost_basis",
            "unrealized_pl",
            "current_price",
        ):
            assert not isinstance(getattr(pos, name), float), name
