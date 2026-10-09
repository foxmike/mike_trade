"""Credential and approval gated Alpaca PAPER order lifecycle verification.

This module is deliberately the only test module here that can open a broker
connection.  It constructs the production adapter for PAPER and checks the
resolved endpoint before the first request.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from mike_trade.app import AlpacaBrokerAdapter
from mike_trade.brokers.alpaca.auth import AlpacaEnvironment
from mike_trade.brokers.alpaca.capabilities import (
    CapabilityDescriptor,
    CapabilityStatus,
    CapabilityUnavailable,
    default_alpaca_capabilities,
)
from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.risk.protection import ProtectionCoordinator
from mike_trade.risk.stop_policy import (
    BrokerProtectionCapabilities,
    ProtectionMode,
    ProtectionPolicy,
)
from mike_trade.runtime import ExecutionMode

D = Decimal
PAPER_KEY = "ALPACA_PAPER_KEY_ID"
PAPER_SECRET = "ALPACA_PAPER_SECRET_KEY"
LIVE_CREDENTIAL_NAMES = (
    "ALPACA_LIVE_KEY_ID",
    "ALPACA_LIVE_SECRET_KEY",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "MIKE_TRADE_ALPACA_LIVE_KEY_ID",
    "MIKE_TRADE_ALPACA_LIVE_SECRET_KEY",
)

# Presence alone is enough to stop the module.  Never fetch, print, or use the
# value of any LIVE credential.
_live_names_set = [name for name in LIVE_CREDENTIAL_NAMES if name in os.environ]
if _live_names_set:
    pytest.exit(
        "Alpaca PAPER integration aborted: LIVE credential variable(s) are set: "
        + ", ".join(_live_names_set),
        returncode=4,
    )

_missing = [name for name in (PAPER_KEY, PAPER_SECRET) if not os.environ.get(name)]
if os.environ.get("MIKE_TRADE_PAPER_ORDERS_APPROVED") != "1":
    _missing.append("MIKE_TRADE_PAPER_ORDERS_APPROVED=1 explicit operator opt-in")
if not os.environ.get("MIKE_TRADE_PAPER_SYMBOL", "").strip():
    _missing.append("MIKE_TRADE_PAPER_SYMBOL operator-selected liquid symbol")
if _missing:
    pytest.skip(
        "Alpaca PAPER order cycle skipped; missing precondition(s): " + ", ".join(_missing),
        allow_module_level=True,
    )


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _paper_endpoint() -> str:
    # The production client derives this directly from the adapter's PAPER
    # environment.  This explicit resolution is checked before startup/network.
    return AlpacaEnvironment.PAPER.base_url


class _ProtectionBroker:
    """Small bridge to the adapter's real order and asset services."""

    def __init__(self, adapter: AlpacaBrokerAdapter, symbol: str, cycle_id: str,
                 entry_price: Decimal, qty: Decimal, style: str) -> None:
        self.adapter, self.symbol, self.cycle_id = adapter, symbol, cycle_id
        self.entry_price, self.qty, self.style = entry_price, qty, style
        self.protective_id: str | None = None
        self.exit_id: str | None = None

    async def submit_protective_order(self, request: object) -> dict[str, str]:
        orders = self.adapter._orders
        assets = self.adapter._assets
        assert orders is not None and assets is not None
        asset = assets.get_asset(self.symbol)
        checker = default_alpaca_capabilities().as_legacy_checker(AlpacaEnvironment.PAPER)
        if self.style == "fixed":
            increment = asset.price_increment
            price = (
                (self.entry_price * D("0.97") / increment)
                .to_integral_value(rounding=ROUND_FLOOR)
                * increment
            )
            placed = orders.submit_stop_sell(
                asset=asset, qty=request.quantity, stop_price=price,
                known_position_qty=request.quantity, capability_check=checker,
                time_in_force="gtc", cycle_id=self.cycle_id,
            )
        else:
            placed = orders.submit_trailing_stop_sell(
                asset=asset, qty=request.quantity, known_position_qty=request.quantity,
                capability_check=checker, trail_percent=D("1.0"),
                time_in_force="gtc", cycle_id=self.cycle_id,
            )
        self.protective_id = placed.order_id
        return {"broker_order_id": placed.order_id}

    async def submit_exit_order(self, request: object) -> dict[str, str]:
        intent = OrderIntent(
            intent_id=f"{self.cycle_id}-exit", client_order_id=f"{self.cycle_id}-exit-client",
            account_scope_id=self.adapter.scope.scope_id, cycle_id=self.cycle_id,
            symbol=self.symbol, side=OrderSide.SELL, purpose=OrderPurpose.MANUAL_CLOSE,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=datetime.now(UTC), quantity=request.quantity,
        )
        order = await self.adapter.submit_order(intent)
        self.exit_id = order.broker_order_id
        return {"broker_order_id": self.exit_id}

    async def cancel_order(self, order_id: str) -> object:
        return await self.adapter.cancel_order(order_id)


def _wait_order(adapter: AlpacaBrokerAdapter, order_id: str, timeout: float = 60) -> object:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        order = asyncio.run(adapter.get_order(order_id))
        if order is not None and order.status.value in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            return order
        time.sleep(1)
    raise AssertionError(f"timed out polling paper order {order_id}")


@pytest.mark.parametrize("protection", ["fixed", "native_trailing"])
def test_real_alpaca_paper_buy_protect_and_flat_cycle(protection: str, tmp_path: Path) -> None:
    symbol = os.environ["MIKE_TRADE_PAPER_SYMBOL"].strip().upper()
    quantity_text = os.environ.get("MIKE_TRADE_PAPER_QTY", "1").strip()
    quantity = D(quantity_text)
    assert quantity == quantity.to_integral_value() and D("0") < quantity <= D("5"), (
        "paper cycle quantity must be a whole-share Decimal from 1 through 5"
    )
    configured_mode = ExecutionMode(os.environ.get("MIKE_TRADE_MODE", "PAPER").upper())
    assert configured_mode is ExecutionMode.PAPER
    endpoint = _paper_endpoint()
    assert AlpacaEnvironment.from_base_url(endpoint) is AlpacaEnvironment.PAPER
    assert urlsplit(endpoint).hostname != AlpacaEnvironment.LIVE.base_url.split("//", 1)[1]
    matrix = default_alpaca_capabilities()
    fixed_descriptor = CapabilityDescriptor("stop", "simple", "whole", "gtc", "sell", "submit")
    fixed_status = matrix.status_of(fixed_descriptor, AlpacaEnvironment.PAPER)
    if protection == "fixed" and fixed_status is not CapabilityStatus.PROVEN:
        # The current executable matrix leaves standalone stop SELL
        # unverified. Do not invent evidence or send that order.
        with pytest.raises(CapabilityUnavailable) as blocked:
            matrix.require(fixed_descriptor, AlpacaEnvironment.PAPER)
        assert "unavailable" in str(blocked.value).lower()
        return

    adapter = AlpacaBrokerAdapter(
        ExecutionMode.PAPER, f"paper-it-{uuid4().hex[:10]}", _Clock(),
        authorized=False, environ=os.environ,
    )
    assert adapter.mode is ExecutionMode.PAPER
    adapter.validate_startup()
    assert adapter._client is not None
    assert adapter._client.environment is AlpacaEnvironment.PAPER
    assert adapter._client.base_url == endpoint
    assert urlsplit(adapter._client.base_url).hostname == "paper-api.alpaca.markets"
    assert urlsplit(adapter._client.base_url).hostname != "api.alpaca.markets"

    clock = adapter._account.get_clock()
    if not clock.is_open:
        pytest.skip(f"Alpaca PAPER market clock is closed; next open {clock.next_open.isoformat()}")
    existing = asyncio.run(adapter.list_positions())
    if any(position.symbol == symbol and position.qty > 0 for position in existing):
        pytest.skip(
            f"Alpaca PAPER cycle skipped: {symbol} already has a position; "
            "the test only closes positions it creates"
        )

    cycle = f"paper-it-{uuid4().hex[:14]}"
    start = datetime.now(UTC) - timedelta(minutes=2)
    created_order_ids: set[str] = set()
    bought_qty = D("0")
    try:
        entry = OrderIntent(
            intent_id=f"{cycle}-entry", client_order_id=f"{cycle}-entry-client",
            account_scope_id=adapter.scope.scope_id, cycle_id=cycle, symbol=symbol,
            side=OrderSide.BUY, purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=datetime.now(UTC), quantity=quantity,
        )
        submitted = asyncio.run(adapter.submit_order(entry))
        created_order_ids.add(submitted.broker_order_id)
        entry_result = _wait_order(adapter, submitted.broker_order_id)
        bought_qty = entry_result.filled_quantity
        if bought_qty <= 0:
            pytest.skip(f"paper entry produced no fill (status={entry_result.status.value})")
        assert bought_qty <= quantity
        assert isinstance(entry_result.filled_avg_price, Decimal)

        # REST order polling is the recovery path for the trade_updates stream;
        # the adapter's production normalizer preserves exact Decimal values.
        fills = asyncio.run(adapter.list_fills(start))
        entry_activities = [f for f in fills if f.broker_order_id == submitted.broker_order_id]
        assert sum((f.quantity for f in entry_activities), D("0")) == bought_qty
        assert len({f.fill_id for f in entry_activities}) == len(entry_activities)

        assert fixed_status is CapabilityStatus.PROVEN
        if protection == "native_trailing":
            capability = CapabilityDescriptor("trailing_stop", "simple", "whole", "gtc", "sell", "submit")
            assert matrix.status_of(capability, AlpacaEnvironment.PAPER) is CapabilityStatus.PROVEN
        protective_broker = _ProtectionBroker(
            adapter, symbol, cycle, entry_result.filled_avg_price, bought_qty, protection
        )
        policy = ProtectionPolicy(
            mode=(ProtectionMode.FIXED if protection == "fixed" else ProtectionMode.TRAILING_NATIVE),
            distance_percent=D("3") if protection == "fixed" else None,
            trail_percent=D("1") if protection == "native_trailing" else None,
            time_in_force=TimeInForce.GTC,
        )
        coordinator = ProtectionCoordinator(protective_broker, _Clock(), policy)
        cap = BrokerProtectionCapabilities(
            supported_order_types=frozenset({"STOP_MARKET", "TRAILING_STOP"}),
            paper_proven_order_types=frozenset({"STOP_MARKET", "TRAILING_STOP"}),
            standalone_trailing_parameters=frozenset({"TRAIL_PERCENT"}),
            paper_proven_standalone_trailing_parameters=frozenset({"TRAIL_PERCENT"}),
            supported_tifs=frozenset({TimeInForce.GTC}),
            paper_proven_tifs=frozenset({TimeInForce.GTC}),
        )
        decision = coordinator.check_entry_eligibility(policy, cap, ExecutionMode.PAPER, bought_qty)
        assert decision.entry_allowed, decision.reason_code
        protection_state = asyncio.run(coordinator.on_confirmed_fill(
            cycle, bought_qty, entry_result.filled_avg_price, confirmed=True
        ))
        assert protection_state is not None
        assert protection_state.protective_working_quantity == bought_qty
        created_order_ids.add(protective_broker.protective_id)
        protective_order = asyncio.run(adapter.get_order(protective_broker.protective_id))
        assert protective_order is not None
        assert protective_order.quantity == bought_qty
        assert isinstance(protective_order.quantity, Decimal)
        if protection == "fixed":
            asset = adapter._assets.get_asset(symbol)
            assert protective_order.stop_price is not None
            assert protective_order.stop_price % asset.price_increment == 0
        else:
            assert protective_order.trail_percent == D("1.0")

        # The coordinator requests stop cancellation first.  Only after broker
        # confirms cancellation does it submit a market sell for the actual
        # remaining position, preventing overlapping sell quantity.
        pending = asyncio.run(coordinator.on_exit_requested(cycle, bought_qty, "paper-it-cleanup"))
        assert pending.status.startswith("CANCEL_PENDING")
        _wait_order(adapter, protective_broker.protective_id)
        canceled = asyncio.run(coordinator.on_broker_order_update(
            cycle, protective_broker.protective_id, "CANCELED", filled_qty=D("0")
        ))
        assert canceled.exit_working_quantity == bought_qty
        exit_order = asyncio.run(adapter.get_order(protective_broker.exit_id))
        assert exit_order is not None
        created_order_ids.add(exit_order.broker_order_id)
        exit_result = _wait_order(adapter, exit_order.broker_order_id)
        assert exit_result.filled_quantity == bought_qty
        final = asyncio.run(coordinator.on_broker_order_update(
            cycle, exit_order.broker_order_id, "FILLED", filled_qty=exit_result.filled_quantity,
            avg_fill_price=exit_result.filled_avg_price,
        ))
        assert final.confirmed_remaining_quantity == 0

        positions = asyncio.run(adapter.list_positions())
        assert not any(position.symbol == symbol for position in positions)
        all_fills = asyncio.run(adapter.list_fills(start))
        cycle_fills = sorted(
            (f for f in all_fills if f.broker_order_id in created_order_ids),
            key=lambda fill: fill.filled_at,
        )
        assert sum((f.quantity if f.side is OrderSide.BUY else -f.quantity for f in cycle_fills), D("0")) == 0
        assert len({f.fill_id for f in cycle_fills}) == len(cycle_fills), "activity ingestion must be exactly once"
        assert all(isinstance(f.quantity, Decimal) and isinstance(f.price, Decimal) for f in cycle_fills)

        # Replay the normalized provider activities through the production
        # exactly-once fill ledger, and verify one durable row per broker fill.
        from mike_trade.domain.accounts import AccountScope
        from mike_trade.domain.trading import (
            BrokerOrderStatus, BudgetPolicy, PositionCycle, TimeInForce,
        )
        from mike_trade.execution.fill_processor import FillEvent, FillProcessor
        from mike_trade.persistence.repositories import (
            AccountScopeRepository, BrokerOrderRepository, OrderIntentRepository,
            PositionCycleRepository,
        )
        from mike_trade.persistence.schema import initialize_database
        from mike_trade.persistence.transaction import WriterSession

        scope = adapter.scope
        ledger_path = tmp_path / f"{cycle}.sqlite3"
        initialize_database(ledger_path, AccountEnvironment.PAPER, now=datetime.now(UTC)).close()
        session = WriterSession(ledger_path)
        try:
            writer = session.bind_scope(scope.scope_id)
            AccountScopeRepository(writer, _Clock()).register_scope(scope, broker="alpaca-paper")
            PositionCycleRepository(writer, _Clock()).open_cycle(
                PositionCycle(cycle, scope.scope_id, symbol, entry_strategy_key="paper-it")
            )
            intents = OrderIntentRepository(
                writer, _Clock(), BudgetPolicy(D("100000"), D("100000"), 20)
            )
            order_repo = BrokerOrderRepository(writer, _Clock())
            processor = FillProcessor(writer, _Clock())
            client_ids: dict[str, str] = {}
            for broker_order_id in {fill.broker_order_id for fill in cycle_fills}:
                broker_order = asyncio.run(adapter.get_order(broker_order_id))
                assert broker_order is not None
                purpose = (
                    OrderPurpose.ENTRY if broker_order.side is OrderSide.BUY
                    else OrderPurpose.MANUAL_CLOSE
                )
                intent = OrderIntent(
                    intent_id=f"{cycle}-{broker_order_id}-ledger",
                    client_order_id=broker_order.client_order_id,
                    account_scope_id=scope.scope_id, cycle_id=cycle, symbol=symbol,
                    side=broker_order.side, purpose=purpose,
                    order_type=broker_order.order_type,
                    time_in_force=TimeInForce.DAY,
                    created_at=broker_order.submitted_at,
                    quantity=broker_order.quantity,
                    stop_price=broker_order.stop_price,
                    trail_price=broker_order.trail_price,
                    trail_percent=broker_order.trail_percent,
                )
                receipt = intents.record_intent(
                    intent,
                    broker_order.quantity * (broker_order.filled_avg_price or D("0"))
                    if purpose is OrderPurpose.ENTRY else D("0"),
                )
                order_repo.upsert_broker_order(
                    replace(
                        broker_order,
                        status=BrokerOrderStatus.ACCEPTED,
                        filled_quantity=D("0"),
                        filled_avg_price=None,
                    ),
                    intent_id=receipt.intent.intent_id,
                )
                client_ids[broker_order_id] = broker_order.client_order_id
            for fill in cycle_fills:
                event = FillEvent(
                    client_order_id=client_ids[fill.broker_order_id],
                    event_id=fill.fill_id, broker_order_id=fill.broker_order_id,
                    symbol=fill.symbol, side=fill.side, quantity=fill.quantity,
                    price=fill.price, fee=fill.fee, occurred_at=fill.filled_at,
                )
                assert processor.ingest(event).outcome == "APPLIED"
                assert processor.ingest(event).outcome == "DUPLICATE"
            ledger_fills = writer.connection.execute(
                "SELECT COUNT(*) FROM fills WHERE account_scope_id=?", (scope.scope_id,)
            ).fetchone()[0]
            ledger_activities = writer.connection.execute(
                "SELECT COUNT(*) FROM broker_activities WHERE account_scope_id=?", (scope.scope_id,)
            ).fetchone()[0]
            ledger_positions = writer.connection.execute(
                "SELECT quantity FROM positions WHERE account_scope_id=? AND symbol=?",
                (scope.scope_id, symbol),
            ).fetchall()
            assert ledger_fills == len(cycle_fills)
            assert ledger_activities == len(cycle_fills)
            assert ledger_positions == []
        finally:
            session.close()
    finally:
        # Restrict cleanup to IDs this test received from its own submissions.
        for order_id in tuple(created_order_ids):
            try:
                asyncio.run(adapter.cancel_order(order_id))
            except Exception:
                pass
        positions = asyncio.run(adapter.list_positions())
        test_position = next((p for p in positions if p.symbol == symbol), None)
        if test_position is not None and test_position.qty > 0:
            cleanup = OrderIntent(
                intent_id=f"{cycle}-cleanup", client_order_id=f"{cycle}-cleanup-client",
                account_scope_id=adapter.scope.scope_id, cycle_id=cycle, symbol=symbol,
                side=OrderSide.SELL, purpose=OrderPurpose.MANUAL_CLOSE,
                order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
                created_at=datetime.now(UTC), quantity=test_position.qty,
            )
            try:
                cleanup_order = asyncio.run(adapter.submit_order(cleanup))
                if hasattr(cleanup_order, "broker_order_id"):
                    _wait_order(adapter, cleanup_order.broker_order_id, timeout=30)
            except Exception:
                pytest.fail("paper integration cleanup could not close its test-created position")


def test_unverified_fractional_protection_and_stepped_trailing_are_blocked() -> None:
    matrix = default_alpaca_capabilities()
    fractional_stop = CapabilityDescriptor("stop", "simple", "fractional", "day", "sell", "submit")
    fractional_trail = CapabilityDescriptor("trailing_stop", "simple", "fractional", "day", "sell", "submit")
    stepped = CapabilityDescriptor("stop", "simple", "whole", "gtc", "sell", "replace")
    for descriptor in (fractional_stop, fractional_trail, stepped):
        assert matrix.status_of(descriptor, AlpacaEnvironment.PAPER) is not CapabilityStatus.PROVEN
        with pytest.raises(Exception):
            matrix.require(descriptor, AlpacaEnvironment.PAPER)
    stepped_policy = ProtectionPolicy(
        mode=ProtectionMode.TRAILING_STEPPED,
        distance_usd=D("2"),
        time_in_force=TimeInForce.GTC,
    )
    eligibility = ProtectionCoordinator(object(), _Clock(), stepped_policy).check_entry_eligibility(
        stepped_policy, BrokerProtectionCapabilities(), ExecutionMode.PAPER, D("1")
    )
    assert not eligibility.entry_allowed
    assert eligibility.reason_code == "STEPPED_NOT_AVAILABLE"
