from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mike_trade.brokers.ports import BrokerPort
from mike_trade.brokers.shadow import account as account_module
from mike_trade.brokers.shadow import ledger as ledger_module
from mike_trade.brokers.shadow.account import ShadowAccount, ShadowAccountState, ShadowPosition
from mike_trade.brokers.shadow.ledger import (
    CASH_ADJUSTED,
    ShadowLedger,
    ShadowLedgerEvent,
)
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import (
    BrokerRejectedError,
    EntryBlockedError,
    InsufficientBuyingPowerError,
    ModeIsolationError,
    PersistenceError,
    UnsupportedOrderCapabilityError,
)
from mike_trade.domain.trading import (
    BrokerOrderStatus,
    EntryEligibilityPolicy,
    Fill,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    TimeInForce,
)
from mike_trade.persistence.journal import (
    DomainEventJournal,
    DuplicateEventIdError,
    NewEvent,
)
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession

D = Decimal
TEN, HUNDRED, ZERO = D("10"), D("100"), D("0")
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)  # Thursday 10:30 ET
SHADOW_SCOPE = AccountScope(AccountEnvironment.SHADOW, "ACCT", "scenario-a")
OTHER_SCENARIO = AccountScope(AccountEnvironment.SHADOW, "ACCT", "scenario-b")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def at(minutes: int = 0, *, base: datetime = NOW) -> datetime:
    return base + timedelta(minutes=minutes)


class FakeClock:
    def now(self) -> datetime:
        return NOW


def policy(**overrides: Any) -> EntryEligibilityPolicy:
    values: dict[str, Any] = {"max_filled_entry_cycles_per_symbol_per_session": 3}
    values.update(overrides)
    return EntryEligibilityPolicy(**values)


class Harness:
    def __init__(self, db_path: Path, scope: AccountScope = SHADOW_SCOPE) -> None:
        self.db_path = db_path
        self.scope = scope
        self.session = WriterSession(db_path)
        self.ledger = self._ledger(scope)
        self.account: ShadowAccount

    def _ledger(self, scope: AccountScope) -> ShadowLedger:
        writer = self.session.bind_scope(scope.scope_id)
        return ShadowLedger(writer, scope, clock=FakeClock())

    def create(self, **overrides: Any) -> ShadowAccount:
        values: dict[str, Any] = {
            "starting_cash": D("10000"),
            "per_symbol_budget": D("2000"),
            "total_budget": D("3000"),
            "eligibility": policy(),
            "feed": "IEX",
            "assumptions": {"slippage": "estimated"},
        }
        values.update(overrides)
        self.account = ShadowAccount.create(self.ledger, **values)
        return self.account

    def reopen(self) -> ShadowAccount:
        self.session.close()
        self.session = WriterSession(self.db_path)
        self.ledger = self._ledger(self.scope)
        self.account = ShadowAccount.open(self.ledger)
        return self.account

    def intent(
        self,
        client_id: str,
        symbol: str = "AAPL",
        side: OrderSide = OrderSide.BUY,
        quantity: Decimal = TEN,
        *,
        price: Decimal | None = HUNDRED,
        purpose: OrderPurpose | None = None,
        order_type: OrderType | None = None,
        stop_price: Decimal | None = None,
        when: datetime = NOW,
        notional: Decimal | None = None,
    ) -> OrderIntent:
        if purpose is None:
            purpose = OrderPurpose.ENTRY if side is OrderSide.BUY else OrderPurpose.STRATEGY_EXIT
        if order_type is None:
            order_type = OrderType.LIMIT if price is not None else OrderType.MARKET
        if order_type is OrderType.STOP:
            price = None
        return OrderIntent(
            intent_id=f"i-{client_id}",
            client_order_id=client_id,
            account_scope_id=self.scope.scope_id,
            cycle_id=f"cycle-{symbol}",
            symbol=symbol,
            side=side,
            purpose=purpose,
            order_type=order_type,
            time_in_force=TimeInForce.DAY,
            created_at=when,
            quantity=None if notional is not None else quantity,
            notional=notional,
            limit_price=price if order_type is OrderType.LIMIT else None,
            stop_price=stop_price,
        )

    def fill(
        self,
        fill_id: str,
        order_id: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = ZERO,
        *,
        symbol: str = "AAPL",
        side: OrderSide = OrderSide.BUY,
        when: datetime = NOW,
    ) -> Fill:
        return Fill(
            fill_id=fill_id,
            broker_order_id=order_id,
            account_scope_id=self.scope.scope_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            filled_at=when,
        )

    def buy(
        self,
        client_id: str,
        quantity: Decimal = TEN,
        price: Decimal = HUNDRED,
        fee: Decimal = ZERO,
        *,
        symbol: str = "AAPL",
        strategy_key: str | None = None,
        when: datetime = NOW,
    ) -> str:
        order = run(
            self.account.submit_order(
                self.intent(client_id, symbol, OrderSide.BUY, quantity, price=price, when=when),
                strategy_key=strategy_key,
            )
        )
        self.account.apply_fill(
            self.fill(
                f"f-{client_id}",
                order.broker_order_id,
                quantity,
                price,
                fee,
                symbol=symbol,
                when=when,
            )
        )
        return str(order.broker_order_id)

    def sell(
        self,
        client_id: str,
        price: Decimal,
        fee: Decimal = ZERO,
        *,
        symbol: str = "AAPL",
        quantity: Decimal | None = None,
        when: datetime = NOW,
    ) -> str:
        qty = quantity if quantity is not None else self.account.state.positions[symbol].quantity
        order = run(
            self.account.submit_order(
                self.intent(client_id, symbol, OrderSide.SELL, qty, price=price, when=when)
            )
        )
        self.account.apply_fill(
            self.fill(
                f"f-{client_id}",
                order.broker_order_id,
                qty,
                price,
                fee,
                symbol=symbol,
                side=OrderSide.SELL,
                when=when,
            )
        )
        return str(order.broker_order_id)

    def events(self) -> list[dict[str, Any]]:
        rows = self.session.connection.execute(
            "SELECT event_id, event_type, payload_json FROM domain_events "
            "WHERE stream_type = 'shadow_ledger' ORDER BY event_seq"
        ).fetchall()
        return [{"event_id": r[0], "event_type": r[1], "payload": json.loads(r[2])} for r in rows]


@pytest.fixture
def make_harness(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    created: list[Harness] = []
    db_path = tmp_path / "shadow.db"
    initialize_database(db_path, AccountEnvironment.SHADOW, now=NOW).close()

    def factory(scope: AccountScope = SHADOW_SCOPE) -> Harness:
        if created:
            harness = Harness.__new__(Harness)
            harness.db_path = db_path
            harness.scope = scope
            harness.session = created[0].session
            harness.ledger = harness._ledger(scope)
            created.append(harness)
            return harness
        harness = Harness(db_path, scope)
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        harness.session.close()


@pytest.fixture
def h(make_harness: Callable[..., Harness]) -> Harness:
    harness = make_harness()
    harness.create()
    return harness


# ----------------------------------------------------------------- identity and creation


def test_account_is_shadow_tagged_and_implements_broker_port(h: Harness) -> None:
    account = h.account
    port: object = account
    assert isinstance(port, BrokerPort)
    assert account.environment is AccountEnvironment.SHADOW
    assert account.mode is AccountEnvironment.SHADOW
    assert account.account_id == "ACCT"
    assert account.scenario_id == "scenario-a"
    assert account.scope.scope_id == "shadow:ACCT:scenario-a"
    snapshot = run(account.get_account())
    assert snapshot.cash == D("10000") and snapshot.equity == D("10000")
    assert snapshot.scope.environment is AccountEnvironment.SHADOW
    assert "estimated=True" in snapshot.source and "broker_confirmed=False" in snapshot.source
    assert account.estimated is True and account.broker_confirmed is False
    assert account.state.estimated is True and account.state.broker_confirmed is False
    capabilities = account.capabilities()
    assert capabilities.provider == "shadow"
    assert capabilities.environment is AccountEnvironment.SHADOW
    assert capabilities.supports_short and not capabilities.supports_notional


def test_scenarios_have_independent_accounts(make_harness: Callable[..., Harness]) -> None:
    first = make_harness(SHADOW_SCOPE)
    first.create()
    second = make_harness(OTHER_SCENARIO)
    second.create(starting_cash=D("500"), per_symbol_budget=D("100"), total_budget=D("200"))
    first.buy("b1", D("10"), D("100"))
    assert first.account.state.cash == D("9000")
    assert second.account.state.cash == D("500")
    assert second.account.state.positions == {}
    assert second.account.scenario_id == "scenario-b"
    assert {e["payload"]["scenario_id"] for e in first.events()} == {"scenario-a", "scenario-b"}
    assert len(first.ledger.read_events()) == 3
    assert len(second.ledger.read_events()) == 1


def test_starting_cash_can_only_be_set_once(h: Harness) -> None:
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.set_starting_cash(D("1"))
    assert raised.value.code == "starting_cash_already_set"
    with pytest.raises(BrokerRejectedError) as raised:
        h.create(starting_cash=D("999999"))
    assert raised.value.code == "starting_cash_already_set"
    reopened = h.reopen()
    assert reopened.state.cash == D("10000")
    assert reopened.state.starting_cash == D("10000")


def test_open_without_creation_is_refused(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness()
    with pytest.raises(PersistenceError) as raised:
        ShadowAccount.open(harness.ledger)
    assert raised.value.code == "ledger_empty"


def test_create_rejects_bad_configuration(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness()
    with pytest.raises(ValueError):
        harness.create(per_symbol_budget=D("5000"), total_budget=D("100"))
    with pytest.raises(ValueError):
        harness.create(starting_cash=D("-1"))
    with pytest.raises(TypeError):
        harness.create(margin_enabled="yes")
    assert harness.ledger.read_events() == ()


# ----------------------------------------------------------------- Decimal and UTC rules


def test_floats_are_rejected_everywhere(h: Harness, make_harness: Callable[..., Harness]) -> None:
    with pytest.raises(ValueError):
        make_harness(OTHER_SCENARIO).create(starting_cash=1000.0)
    with pytest.raises(ValueError):
        h.account.adjust_cash(adjustment_id="a", amount=1.5, reason="r", actor="u")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        h.account.update_quote("AAPL", 101.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        h.fill("f", "o", 1.0, D("1"))  # type: ignore[arg-type]
    order = run(h.account.submit_order(h.intent("b1")))
    with pytest.raises(ValueError):
        h.account.apply_fill(
            h.fill("f1", order.broker_order_id, D("10"), D("100")),
            slippage=0.01,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        ShadowLedgerEvent(
            event_id="e",
            kind=CASH_ADJUSTED,
            scope=SHADOW_SCOPE,
            occurred_at=NOW,
            data={"amount": 1.5},
        )
    with pytest.raises(ValueError):
        ShadowPosition(
            symbol="AAPL",
            quantity=1.0,
            cost_basis=D("1"),
            entry_fees=D("0"),  # type: ignore[arg-type]
            strategy_key=None,
            cycle_id="c",
            entry_order_id="o",
            opened_at=NOW,
            entry_cost_total=D("1"),
            entry_fees_total=D("0"),
        )
    assert h.account.state.cash == D("10000")


def test_naive_and_non_utc_times_are_rejected(h: Harness) -> None:
    naive = datetime(2026, 10, 8, 14, 30)
    eastern = datetime(2026, 10, 8, 10, 30, tzinfo=UTC).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    for bad in (naive, eastern):
        with pytest.raises(ValueError):
            h.account.adjust_cash(adjustment_id="t", amount=D("1"), reason="r", actor="u", at=bad)
        with pytest.raises(ValueError):
            ShadowLedgerEvent(
                event_id="e", kind=CASH_ADJUSTED, scope=SHADOW_SCOPE, occurred_at=bad, data={}
            )
    with pytest.raises(ValueError):
        run(h.account.list_fills(naive))


# ----------------------------------------------------------------- cycles and accounting


def test_full_cycle_cash_realized_pnl_and_labels(h: Harness) -> None:
    h.buy("b1", D("10"), D("100"), D("1"))
    state = h.account.state
    assert state.cash == D("8999")
    position = state.positions["AAPL"]
    assert position.quantity == D("10") and position.avg_cost == D("100")
    assert position.estimated is True and position.broker_confirmed is False
    h.sell("s1", D("110"), D("1"))
    state = h.account.state
    assert state.positions == {}
    assert state.cash == D("10098")
    assert state.realized_pnl == D("98")
    assert state.closed_cycles == 1
    assert state.realized_pnl == state.cash - state.starting_cash
    assert h.account.equity() == D("10098") and h.account.unrealized_pnl() == D("0")


def test_partial_fills_accumulate_and_match_domain_cycle_accounting(h: Harness) -> None:
    order = run(h.account.submit_order(h.intent("b1", quantity=D("3"), price=D("34"))))
    oid = order.broker_order_id
    first = h.account.apply_fill(h.fill("e1", oid, D("1"), D("33.33"), D("0.1")))
    assert first.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert [o.broker_order_id for o in run(h.account.list_open_orders())] == [oid]
    second = h.account.apply_fill(h.fill("e2", oid, D("2"), D("33.34"), D("0.2")))
    assert second.status is BrokerOrderStatus.FILLED
    assert second.filled_avg_price == D("100.01") / D("3")
    assert run(h.account.list_open_orders()) == []
    position = h.account.state.positions["AAPL"]
    assert position.quantity == D("3") and position.cost_basis == D("100.01")
    assert position.entry_fees == D("0.3")

    sell = run(
        h.account.submit_order(h.intent("s1", side=OrderSide.SELL, quantity=D("3"), price=D("40")))
    )
    sid = sell.broker_order_id
    h.account.apply_fill(h.fill("x1", sid, D("1"), D("40"), D("0.1"), side=OrderSide.SELL))
    assert h.account.state.positions["AAPL"].quantity == D("2")
    assert h.account.state.cooldown_expiry("AAPL") is None
    h.account.apply_fill(h.fill("x2", sid, D("2"), D("41"), D("0.2"), side=OrderSide.SELL))
    state = h.account.state
    assert state.positions == {}
    # exact: proceeds 122 - cost 100.01 - fees 0.3 - 0.3
    assert state.realized_pnl == D("21.39")
    assert state.cash == D("10000") + state.realized_pnl

    entry = [state.fills["e1"], state.fills["e2"]]
    exits = [state.fills["x1"], state.fills["x2"]]
    cycle = PositionCycle(
        cycle_id="c",
        account_scope_id=h.scope.scope_id,
        symbol="AAPL",
        entry_fills=tuple(entry),
        exit_fills=tuple(exits),
        state=__import__("mike_trade.domain.trading", fromlist=["x"]).PositionCycleState.CLOSED,
        opened_at=NOW,
        closed_at=NOW,
    )
    assert state.realized_pnl == cycle.realized_pnl


def test_fractional_quantities_require_configuration(
    h: Harness, make_harness: Callable[..., Harness]
) -> None:
    with pytest.raises(UnsupportedOrderCapabilityError) as raised:
        run(h.account.submit_order(h.intent("b1", quantity=D("1.5"))))
    assert raised.value.code == "fractional_unsupported"
    assert run(h.account.get_order_by_client_id("b1")) is None

    other = make_harness(OTHER_SCENARIO)
    other.create(allow_fractional=True)
    other.buy("b1", D("1.5"), D("100"))
    assert other.account.state.positions["AAPL"].quantity == D("1.5")
    assert other.account.capabilities().supports_fractional
    assert other.account.capabilities().supports_order("market", True)
    other.sell("s1", D("100"))
    assert other.account.state.positions == {}


def test_unrealized_pnl_and_equity_use_supplied_quotes(h: Harness) -> None:
    h.buy("b1", D("10"), D("100"), D("1"))
    # no quote yet: valued at cost, only the paid entry fee shows as unrealized
    assert h.account.unrealized_pnl() == D("-1")
    assert "valued_at_cost=AAPL" in run(h.account.get_account()).source
    h.account.update_quote("aapl", D("105"))
    assert h.account.unrealized_pnl() == D("49")
    assert h.account.equity() == D("10049")
    assert h.account.unrealized_pnl({"AAPL": D("90")}) == D("-101")
    snapshot = run(h.account.get_account())
    assert snapshot.equity == D("10049") and "valued_at_cost" not in snapshot.source
    [position] = run(h.account.list_positions())
    assert position.market_value == D("1050") and position.avg_entry_price == D("100")
    state = h.account.state
    assert h.account.equity() == (
        state.starting_cash + state.realized_pnl + h.account.unrealized_pnl()
    )
    with pytest.raises(ValueError):
        h.account.update_quote("AAPL", D("0"))


# ----------------------------------------------------------------- cycle-shape rules


def test_averaging_down_and_pyramiding_are_rejected(h: Harness) -> None:
    h.buy("b1", D("10"), D("100"))
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.submit_order(h.intent("b2", price=D("90"))))
    assert raised.value.code == "averaging_down"
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.submit_order(h.intent("b3", price=D("110"))))
    assert raised.value.code == "pyramiding"
    assert run(h.account.get_order_by_client_id("b2")) is None
    assert h.account.state.positions["AAPL"].quantity == D("10")


def test_second_pending_entry_order_is_rejected_and_other_symbols_allowed(h: Harness) -> None:
    run(h.account.submit_order(h.intent("b1")))
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.submit_order(h.intent("b2")))
    assert raised.value.code == "pyramiding"
    run(h.account.submit_order(h.intent("m1", "MSFT")))


def test_fill_from_a_second_order_cannot_add_to_a_position(h: Harness) -> None:
    h.buy("b1")
    # simulate a (buggy) simulator filling an order that could never have been accepted
    state = h.account.state
    assert state.positions["AAPL"].entry_order_id == "shadow-b1"
    other = run(h.account.submit_order(h.intent("m1", "MSFT")))
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(h.fill("bad", other.broker_order_id, D("1"), D("1"), symbol="AAPL"))
    assert raised.value.code == "fill_order_mismatch"


def test_planned_partial_exit_oversell_and_short_are_rejected(h: Harness) -> None:
    h.buy("b1", D("10"), D("100"))
    for client_id, quantity, code in (
        ("s-part", D("4"), "partial_exit"),
        ("s-over", D("11"), "oversell"),
    ):
        with pytest.raises(BrokerRejectedError) as raised:
            run(
                h.account.submit_order(
                    h.intent(client_id, side=OrderSide.SELL, quantity=quantity, price=D("100"))
                )
            )
        assert raised.value.code == code
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.submit_order(h.intent("s-short", "MSFT", OrderSide.SELL, D("1"))))
    assert raised.value.code == "no_position"
    assert h.account.state.positions["AAPL"].quantity == D("10")
    assert run(h.account.list_open_orders()) == []


def test_sell_fill_never_exceeds_held_quantity(h: Harness) -> None:
    h.buy("b1", D("10"), D("100"))
    first = run(h.account.submit_order(h.intent("s1", side=OrderSide.SELL, quantity=D("10"))))
    second = run(h.account.submit_order(h.intent("s2", side=OrderSide.SELL, quantity=D("10"))))
    h.account.apply_fill(h.fill("x1", first.broker_order_id, D("7"), D("101"), side=OrderSide.SELL))
    # the second exit order was valid when submitted, but only 3 remain held
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(
            h.fill("x2", second.broker_order_id, D("5"), D("101"), side=OrderSide.SELL)
        )
    assert raised.value.code == "oversell"
    h.account.apply_fill(h.fill("x3", first.broker_order_id, D("3"), D("101"), side=OrderSide.SELL))
    assert h.account.state.positions == {}
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(
            h.fill("x4", second.broker_order_id, D("1"), D("101"), side=OrderSide.SELL)
        )
    assert raised.value.code == "no_position"


def test_overfill_unknown_order_and_mismatched_fill_are_rejected(h: Harness) -> None:
    order = run(h.account.submit_order(h.intent("b1", quantity=D("10"))))
    oid = order.broker_order_id
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(h.fill("f1", oid, D("11"), D("100")))
    assert raised.value.code == "overfill"
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(h.fill("f2", "shadow-none", D("1"), D("100")))
    assert raised.value.code == "unknown_order"
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(h.fill("f3", oid, D("1"), D("100"), symbol="MSFT"))
    assert raised.value.code == "fill_order_mismatch"
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(h.fill("f4", oid, D("1"), D("100"), side=OrderSide.SELL))
    assert raised.value.code == "fill_order_mismatch"
    assert h.account.state.cash == D("10000")


def test_buy_must_be_an_entry_and_notional_is_unsupported(h: Harness) -> None:
    # A BUY can only be an ENTRY: the mismatch is refused when the intent is constructed.
    with pytest.raises(ValueError, match="exit/protection intents require"):
        h.intent("b1", purpose=OrderPurpose.STRATEGY_EXIT)
    with pytest.raises(UnsupportedOrderCapabilityError):
        run(h.account.submit_order(h.intent("b2", notional=D("500"))))
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.submit_order(h.intent("b3", price=None)))
    assert raised.value.code == "reference_price_required"
    h.account.update_quote("AAPL", D("100"))
    assert run(h.account.submit_order(h.intent("b3", price=None))).quantity == D("10")


# ----------------------------------------------------------------- cash and budgets


def test_cash_may_not_go_negative_unless_margin_is_enabled(
    make_harness: Callable[..., Harness],
) -> None:
    cash_only = make_harness(SHADOW_SCOPE)
    cash_only.create(starting_cash=D("1000"), per_symbol_budget=D("5000"), total_budget=D("5000"))
    with pytest.raises(InsufficientBuyingPowerError) as raised:
        run(cash_only.account.submit_order(cash_only.intent("b1", quantity=D("20"))))
    assert raised.value.code == "cash_negative"
    # notional fits cash, but the fee pushes the fill below zero
    order = run(cash_only.account.submit_order(cash_only.intent("b2", quantity=D("10"))))
    with pytest.raises(InsufficientBuyingPowerError):
        cash_only.account.apply_fill(
            cash_only.fill("f", order.broker_order_id, D("10"), D("100"), D("1"))
        )
    assert cash_only.account.state.cash == D("1000")
    assert cash_only.account.state.positions == {}
    with pytest.raises(InsufficientBuyingPowerError):
        cash_only.account.adjust_cash(
            adjustment_id="w", amount=D("-1001"), reason="withdraw", actor="ops"
        )

    margin = make_harness(OTHER_SCENARIO)
    margin.create(
        starting_cash=D("1000"),
        per_symbol_budget=D("5000"),
        total_budget=D("5000"),
        margin_enabled=True,
    )
    margin.buy("b1", D("10"), D("100"), D("1"))
    assert margin.account.state.cash == D("-1")


def test_open_buy_orders_reserve_cash(h: Harness, make_harness: Callable[..., Harness]) -> None:
    other = make_harness(OTHER_SCENARIO)
    other.create(starting_cash=D("1500"), per_symbol_budget=D("1000"), total_budget=D("3000"))
    run(other.account.submit_order(other.intent("b1", "AAPL", quantity=D("10"))))
    assert run(other.account.get_account()).buying_power == D("500")
    with pytest.raises(InsufficientBuyingPowerError):
        run(other.account.submit_order(other.intent("b2", "MSFT", quantity=D("10"))))


def test_per_symbol_and_total_budgets(h: Harness) -> None:
    with pytest.raises(EntryBlockedError) as raised:
        run(h.account.submit_order(h.intent("b1", quantity=D("25"))))  # 2500 > 2000
    assert raised.value.code == "per_symbol_budget"
    h.buy("b2", D("19"), D("100"), symbol="AAPL")  # 1900
    with pytest.raises(EntryBlockedError) as raised:
        run(h.account.submit_order(h.intent("b3", "MSFT", quantity=D("12"))))  # 1900+1200 > 3000
    assert raised.value.code == "total_budget"
    # pending entry reservations also count against the total budget
    run(h.account.submit_order(h.intent("b4", "MSFT", quantity=D("11"))))
    with pytest.raises(EntryBlockedError) as raised:
        run(h.account.submit_order(h.intent("b5", "NVDA", quantity=D("1"))))
    assert raised.value.code == "total_budget"


def test_cash_adjustments_are_audited_and_idempotent(h: Harness) -> None:
    assert h.account.adjust_cash(
        adjustment_id="dep-1", amount=D("250.50"), reason="top up", actor="mike", at=at(1)
    )
    assert h.account.state.cash == D("10250.50")
    assert h.account.state.starting_cash == D("10000")
    assert not h.account.adjust_cash(
        adjustment_id="dep-1", amount=D("250.50"), reason="top up", actor="mike", at=at(1)
    )
    assert h.account.state.cash == D("10250.50")
    with pytest.raises(DuplicateEventIdError):
        h.account.adjust_cash(
            adjustment_id="dep-1", amount=D("999"), reason="top up", actor="mike", at=at(1)
        )
    audit = [e for e in h.events() if e["event_type"] == CASH_ADJUSTED]
    assert len(audit) == 1
    assert audit[0]["payload"]["data"]["reason"] == "top up"
    assert audit[0]["payload"]["data"]["actor"] == "mike"
    for kwargs in (
        {"reason": "", "actor": "mike", "amount": D("1")},
        {"reason": "x", "actor": " ", "amount": D("1")},
        {"reason": "x", "actor": "mike", "amount": D("0")},
    ):
        with pytest.raises(ValueError):
            h.account.adjust_cash(adjustment_id="bad", **kwargs)
    assert h.account.state.cash == D("10250.50")
    assert h.reopen().state.adjustments == h.account.state.adjustments


# ----------------------------------------------------------------- eligibility counters


def test_filled_entry_cycles_are_counted_per_ny_session(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness()
    harness.create(eligibility=policy(max_filled_entry_cycles_per_symbol_per_session=2))
    harness.buy("b1", D("1"), D("100"), when=at(0))
    harness.sell("s1", D("101"), when=at(1))
    harness.buy("b2", D("1"), D("100"), when=at(2))
    harness.sell("s2", D("101"), when=at(3))
    thursday = date(2026, 10, 8)
    assert harness.account.state.filled_entry_cycles("AAPL", thursday) == 2
    assert harness.account.state.filled_entry_cycles("MSFT", thursday) == 0
    with pytest.raises(EntryBlockedError) as raised:
        run(harness.account.submit_order(harness.intent("b3", quantity=D("1"), when=at(4))))
    assert raised.value.code == "MAX_ENTRY_CYCLES_REACHED"
    # 23:30 ET Thursday is still Thursday's session
    late = datetime(2026, 10, 9, 3, 30, tzinfo=UTC)
    with pytest.raises(EntryBlockedError):
        run(harness.account.submit_order(harness.intent("b4", quantity=D("1"), when=late)))
    # next session resets the count
    friday = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)
    harness.buy("b5", D("1"), D("100"), when=friday)
    assert harness.account.state.filled_entry_cycles("AAPL", date(2026, 10, 9)) == 1
    # a Saturday instant belongs to Friday's session
    harness.sell("s5", D("101"), when=friday + timedelta(minutes=1))
    saturday = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
    harness.buy("b6", D("1"), D("100"), when=saturday)
    assert harness.account.state.filled_entry_cycles("AAPL", date(2026, 10, 9)) == 2
    assert harness.account.state.filled_entry_cycles("AAPL", date(2026, 10, 10)) == 0


def test_unfilled_or_canceled_entries_do_not_consume_the_session_limit(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness()
    harness.create(eligibility=policy(max_filled_entry_cycles_per_symbol_per_session=1))
    order = run(harness.account.submit_order(harness.intent("b1")))
    run(harness.account.cancel_order(order.broker_order_id))
    assert harness.account.state.filled_entry_cycles("AAPL", date(2026, 10, 8)) == 0
    harness.buy("b2")
    assert harness.account.state.filled_entry_cycles("AAPL", date(2026, 10, 8)) == 1


def test_losing_streak_lockout_clear_and_win_reset(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness()
    harness.create(
        eligibility=policy(
            max_filled_entry_cycles_per_symbol_per_session=10,
            consecutive_loss_lockout_threshold=2,
        )
    )
    harness.buy("b1", when=at(0))
    harness.sell("s1", D("90"), when=at(1))
    assert harness.account.state.losing_streak("AAPL") == 1
    harness.buy("b2", when=at(2))
    harness.sell("s2", D("110"), when=at(3))  # a win resets the streak
    assert harness.account.state.losing_streak("AAPL") == 0
    harness.buy("b3", when=at(4))
    harness.sell("s3", D("99"), when=at(5))
    harness.buy("b4", when=at(6))
    harness.sell("s4", D("99"), when=at(7))
    assert harness.account.state.losing_streak("AAPL") == 2
    with pytest.raises(EntryBlockedError) as raised:
        run(harness.account.submit_order(harness.intent("b5", when=at(8))))
    assert raised.value.code == "LOSING_STREAK_LOCKOUT"
    run(harness.account.submit_order(harness.intent("m1", "MSFT", when=at(8))))  # other symbol
    harness.account.clear_lockout("aapl", reason="reviewed", actor="mike", at=at(9))
    assert harness.account.state.losing_streak("AAPL") == 0
    run(harness.account.submit_order(harness.intent("b6", when=at(10))))
    with pytest.raises(ValueError):
        harness.account.clear_lockout("AAPL", reason="", actor="mike")


def test_fee_only_breakeven_counts_as_loss(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness()
    harness.create(
        eligibility=policy(
            max_filled_entry_cycles_per_symbol_per_session=10,
            consecutive_loss_lockout_threshold=1,
        )
    )
    harness.buy("b1", D("10"), D("100"), D("1"))
    harness.sell("s1", D("100"), D("0"))  # gross flat, net -1 from the entry fee
    assert harness.account.state.realized_pnl == D("-1")
    assert harness.account.state.losing_streak("AAPL") == 1


def test_strategy_scoped_lockout(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness()
    harness.create(
        eligibility=policy(
            max_filled_entry_cycles_per_symbol_per_session=10,
            consecutive_loss_lockout_threshold=2,
            lockout_scope_by_strategy=True,
        )
    )
    with pytest.raises(BrokerRejectedError) as raised:
        run(harness.account.submit_order(harness.intent("b0")))
    assert raised.value.code == "strategy_key_required"
    for index in range(2):
        harness.buy(f"a{index}", strategy_key="vwap", when=at(index * 2))
        harness.sell(f"as{index}", D("90"), when=at(index * 2 + 1))
    state = harness.account.state
    assert state.losing_streak("AAPL", "vwap") == 2
    assert state.losing_streak("AAPL", "momentum") == 0
    assert state.positions == {}
    with pytest.raises(EntryBlockedError) as raised:
        run(harness.account.submit_order(harness.intent("b1", when=at(10)), strategy_key="vwap"))
    assert raised.value.code == "LOSING_STREAK_LOCKOUT"
    harness.buy("b2", strategy_key="momentum", when=at(10))
    assert harness.account.state.positions["AAPL"].strategy_key == "momentum"
    harness.account.clear_lockout("AAPL", strategy_key="vwap", reason="r", actor="mike")
    assert harness.account.state.losing_streak("AAPL", "vwap") == 0


def test_post_sale_cooldown_expiry(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness()
    harness.create(eligibility=policy(post_sale_cooldown=timedelta(minutes=10)))
    harness.buy("b1", when=at(0))
    harness.sell("s1", D("101"), when=at(1))
    assert harness.account.state.cooldown_expiry("AAPL") == at(11)
    with pytest.raises(EntryBlockedError) as raised:
        run(harness.account.submit_order(harness.intent("b2", when=at(5))))
    assert raised.value.code == "POST_SALE_COOLDOWN"
    run(harness.account.submit_order(harness.intent("m1", "MSFT", when=at(5))))
    assert run(harness.account.submit_order(harness.intent("b3", when=at(11)))).symbol == "AAPL"


def test_entry_kill_switch_blocks_entries_but_not_exits(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness()
    harness.create(eligibility=policy(entry_kill_switch=True))
    with pytest.raises(EntryBlockedError) as raised:
        run(harness.account.submit_order(harness.intent("b1")))
    assert raised.value.code == "entry_kill_switch"


# ----------------------------------------------------------------- orders


def test_submit_is_idempotent_by_client_order_id_and_scope_checked(h: Harness) -> None:
    first = run(h.account.submit_order(h.intent("b1")))
    again = run(h.account.submit_order(h.intent("b1")))
    assert first == again
    assert len(run(h.account.list_open_orders())) == 1
    assert run(h.account.get_order(first.broker_order_id)) == first
    assert run(h.account.get_order_by_client_id("b1")) == first
    assert first.account_scope_id == h.scope.scope_id
    foreign = OrderIntent(
        intent_id="i",
        client_order_id="x",
        account_scope_id="paper:ACCT",
        cycle_id="c",
        symbol="AAPL",
        side=OrderSide.BUY,
        purpose=OrderPurpose.ENTRY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        created_at=NOW,
        quantity=D("1"),
    )
    with pytest.raises(ModeIsolationError):
        run(h.account.submit_order(foreign))
    foreign_fill = Fill(
        fill_id="ff",
        broker_order_id=first.broker_order_id,
        account_scope_id="paper:ACCT",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=D("1"),
        price=D("1"),
        fee=D("0"),
        filled_at=NOW,
    )
    with pytest.raises(ModeIsolationError):
        h.account.apply_fill(foreign_fill)


def test_duplicate_fill_is_idempotent_and_conflicting_duplicate_rejected(h: Harness) -> None:
    order = run(h.account.submit_order(h.intent("b1")))
    fill = h.fill("f1", order.broker_order_id, D("4"), D("100"), D("1"))
    h.account.apply_fill(fill)
    cash = h.account.state.cash
    again = h.account.apply_fill(fill)
    assert again.filled_quantity == D("4")
    assert h.account.state.cash == cash
    assert len(h.account.state.fills) == 1
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(h.fill("f1", order.broker_order_id, D("4"), D("101"), D("1")))
    assert raised.value.code == "duplicate_fill"
    fills = run(h.account.list_fills(NOW))
    assert [f.fill_id for f in fills] == ["f1"]
    assert run(h.account.list_fills(at(1))) == []


def test_cancel_and_replace(h: Harness) -> None:
    h.buy("b1", D("10"), D("100"))
    stop = run(
        h.account.submit_order(
            h.intent(
                "s1",
                side=OrderSide.SELL,
                quantity=D("10"),
                order_type=OrderType.STOP,
                stop_price=D("95"),
            )
        )
    )
    replaced = run(h.account.replace_order(stop.broker_order_id, stop_price=D("97")))
    assert replaced.stop_price == D("97")
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.replace_order(stop.broker_order_id, quantity=D("5")))
    assert raised.value.code == "partial_exit"
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.replace_order(stop.broker_order_id, trail=D("1")))
    assert raised.value.code == "invalid_replace"
    with pytest.raises(ValueError):
        run(h.account.replace_order(stop.broker_order_id))
    run(h.account.cancel_order(stop.broker_order_id))
    run(h.account.cancel_order(stop.broker_order_id))  # idempotent
    assert run(h.account.list_open_orders()) == []
    canceled = run(h.account.get_order(stop.broker_order_id))
    assert canceled is not None and canceled.status is BrokerOrderStatus.CANCELED
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.replace_order(stop.broker_order_id, stop_price=D("96")))
    assert raised.value.code == "order_not_open"
    with pytest.raises(BrokerRejectedError):
        run(h.account.cancel_order("shadow-unknown"))
    with pytest.raises(BrokerRejectedError) as raised:
        h.account.apply_fill(
            h.fill("late", stop.broker_order_id, D("10"), D("96"), side=OrderSide.SELL)
        )
    assert raised.value.code == "order_not_open"
    assert h.account.state.positions["AAPL"].quantity == D("10")


def test_entry_order_cannot_be_replaced_with_a_larger_quantity(h: Harness) -> None:
    order = run(h.account.submit_order(h.intent("b1", quantity=D("10"))))
    with pytest.raises(BrokerRejectedError) as raised:
        run(h.account.replace_order(order.broker_order_id, quantity=D("12")))
    assert raised.value.code == "invalid_replace_quantity"
    assert run(h.account.replace_order(order.broker_order_id, quantity=D("8"))).quantity == D("8")


def test_canceling_a_partially_filled_entry_keeps_the_position(h: Harness) -> None:
    order = run(h.account.submit_order(h.intent("b1", quantity=D("10"))))
    h.account.apply_fill(h.fill("f1", order.broker_order_id, D("4"), D("100")))
    run(h.account.cancel_order(order.broker_order_id))
    assert h.account.state.positions["AAPL"].quantity == D("4")
    h.sell("s1", D("101"))
    assert h.account.state.positions == {}


# ----------------------------------------------------------------- restart and ledger


def test_restart_reproduces_identical_state_and_never_resets(h: Harness) -> None:
    h.account.adjust_cash(adjustment_id="d", amount=D("100"), reason="r", actor="mike", at=at(0))
    h.buy("b1", D("10"), D("100"), D("1"), strategy_key="vwap", when=at(1))
    h.sell("s1", D("95"), D("1"), when=at(2))
    h.buy("m1", D("5"), D("200"), D("0.5"), symbol="MSFT", when=at(3))
    pending = run(h.account.submit_order(h.intent("n1", "NVDA", quantity=D("2"), when=at(4))))
    h.account.apply_fill(
        h.fill("nf", pending.broker_order_id, D("1"), D("100"), symbol="NVDA", when=at(5))
    )
    before = h.account.state
    before_snapshot = (h.account.equity({"MSFT": D("210")}), run(h.account.list_open_orders()))
    assert before.positions["MSFT"].quantity == D("5")
    assert before.losing_streak("AAPL") == 1

    reopened = h.reopen()
    assert reopened.state == before
    assert reopened.state.cash == before.cash
    assert reopened.state.realized_pnl == before.realized_pnl == D("-52")
    after_snapshot = (reopened.equity({"MSFT": D("210")}), run(reopened.list_open_orders()))
    assert after_snapshot == before_snapshot
    with pytest.raises(BrokerRejectedError):
        h.create()
    again = h.reopen()
    assert again.state == before
    # the reopened account keeps working from the replayed state
    run(again.submit_order(h.intent("b9", "AAPL", quantity=D("1"), when=at(6))))
    assert again.state.orders["shadow-b9"].status is BrokerOrderStatus.ACCEPTED


def test_replayed_counters_and_cooldowns_survive_restart(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness()
    harness.create(
        eligibility=policy(
            max_filled_entry_cycles_per_symbol_per_session=1,
            consecutive_loss_lockout_threshold=1,
            post_sale_cooldown=timedelta(minutes=30),
        )
    )
    harness.buy("b1", when=at(0))
    harness.sell("s1", D("90"), when=at(1))
    state = harness.reopen().state
    assert state.filled_entry_cycles("AAPL", date(2026, 10, 8)) == 1
    assert state.losing_streak("AAPL") == 1
    assert state.cooldown_expiry("AAPL") == at(31)
    with pytest.raises(EntryBlockedError):
        run(harness.account.submit_order(harness.intent("b2", when=at(40))))


def test_ledger_rows_carry_provenance_and_estimated_labels(h: Harness) -> None:
    h.buy("b1")
    order = run(h.account.submit_order(h.intent("m1", "MSFT")))
    h.account.apply_fill(
        h.fill("mf", order.broker_order_id, D("1"), D("100"), symbol="MSFT"),
        slippage=D("0.02"),
        feed="SIP",
    )
    events = h.events()
    assert [e["event_type"] for e in events][0] == "ShadowAccountCreated"
    for event in events:
        payload = event["payload"]
        assert payload["mode"] == "SHADOW"
        assert payload["account_id"] == "ACCT" and payload["scenario_id"] == "scenario-a"
        assert payload["simulator_version"] == "shadow-sim/1"
        assert payload["assumptions"] == {"slippage": "estimated"}
        assert payload["estimated"] is True and payload["broker_confirmed"] is False
    feeds = {e["event_id"]: e["payload"]["feed"] for e in events}
    assert feeds["shadow:ACCT:scenario-a:created"] == "IEX"
    assert feeds["shadow:ACCT:scenario-a:fill:mf"] == "SIP"
    fill_event = next(e for e in events if e["event_id"].endswith(":fill:mf"))
    assert fill_event["payload"]["data"]["slippage"] == "0.02"
    assert h.account.state.fill_slippage["mf"] == D("0.02")
    replayed = {e.event_id: e for e in h.ledger.read_events()}
    assert replayed["shadow:ACCT:scenario-a:created"].feed == "IEX"
    assert replayed["shadow:ACCT:scenario-a:created"].estimated is True
    assert replayed["shadow:ACCT:scenario-a:created"].broker_confirmed is False


def test_ledger_append_is_idempotent_by_event_id(h: Harness) -> None:
    event = ShadowLedgerEvent(
        event_id="shadow:ACCT:scenario-a:cash:manual",
        kind=CASH_ADJUSTED,
        scope=h.scope,
        occurred_at=NOW,
        data={"adjustment_id": "manual", "amount": D("1"), "reason": "r", "actor": "a"},
    )
    count = len(h.ledger.read_events())
    assert h.ledger.append(event) is True
    assert h.ledger.append(event) is False
    assert h.ledger.contains(event.event_id)
    assert len(h.ledger.read_events()) == count + 1
    changed = ShadowLedgerEvent(
        event_id=event.event_id,
        kind=CASH_ADJUSTED,
        scope=h.scope,
        occurred_at=NOW,
        data={"adjustment_id": "manual", "amount": D("2"), "reason": "r", "actor": "a"},
    )
    with pytest.raises(DuplicateEventIdError):
        h.ledger.append(changed)
    assert len(h.ledger.read_events()) == count + 1


def test_ledger_is_append_only(h: Harness) -> None:
    h.buy("b1")
    public = {name for name in dir(h.ledger) if not name.startswith("_")}
    assert not {n for n in public if any(w in n for w in ("update", "delete", "remove", "clear"))}
    connection = h.session.connection
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("UPDATE domain_events SET payload_json = '{}'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("DELETE FROM domain_events")
    assert h.reopen().state.positions["AAPL"].quantity == D("10")


def test_ledger_refuses_other_modes_and_scenarios(
    h: Harness, tmp_path: Path, make_harness: Callable[..., Harness]
) -> None:
    writer = h.session.bind_scope(h.scope.scope_id)
    paper = AccountScope(AccountEnvironment.PAPER, "ACCT")
    with pytest.raises(ModeIsolationError):
        ShadowLedger(writer, paper, clock=FakeClock())
    with pytest.raises(ModeIsolationError):
        ShadowLedger(writer, OTHER_SCENARIO, clock=FakeClock())  # writer bound elsewhere
    paper_db = tmp_path / "paper.db"
    initialize_database(paper_db, AccountEnvironment.PAPER, now=NOW).close()
    with WriterSession(paper_db) as session:
        with pytest.raises(ModeIsolationError):
            session.bind_scope(h.scope.scope_id)
        with pytest.raises(ModeIsolationError):
            ShadowLedger(session.bind_scope(paper.scope_id), h.scope, clock=FakeClock())
        with pytest.raises(ModeIsolationError):
            ShadowLedger(session.bind_scope(paper.scope_id), paper, clock=FakeClock())
    before = len(h.ledger.read_events())
    for foreign in (paper, OTHER_SCENARIO):
        event = ShadowLedgerEvent(
            event_id=f"x-{foreign.scope_id}",
            kind=CASH_ADJUSTED,
            scope=foreign,
            occurred_at=NOW,
            data={},
        )
        with pytest.raises(ModeIsolationError):
            h.ledger.append(event)
    assert len(h.ledger.read_events()) == before


@pytest.mark.parametrize(
    "overrides",
    [{"mode": "PAPER"}, {"mode": "LIVE"}, {"scenario_id": "scenario-b"}, {"account_id": "OTHER"}],
)
def test_ledger_refuses_to_read_foreign_rows(h: Harness, overrides: dict[str, str]) -> None:
    payload: dict[str, Any] = {
        "mode": "SHADOW",
        "account_id": "ACCT",
        "scenario_id": "scenario-a",
        "simulator_version": "x",
        "assumptions": {},
        "feed": None,
        "estimated": True,
        "broker_confirmed": False,
        "data": {},
    }
    payload.update(overrides)
    DomainEventJournal(h.session.connection, clock=FakeClock()).append(
        [
            NewEvent(
                account_scope_id=h.scope.scope_id,
                stream_type="shadow_ledger",
                stream_id=h.scope.scope_id,
                event_type=CASH_ADJUSTED,
                payload=payload,
                occurred_at=NOW,
            )
        ]
    )
    with pytest.raises(ModeIsolationError) as raised:
        h.ledger.read_events()
    assert raised.value.code == "ledger_row_scope_mismatch"
    with pytest.raises(ModeIsolationError):
        h.reopen()


def test_ledger_refuses_rows_claiming_broker_confirmation(h: Harness) -> None:
    payload = {
        "mode": "SHADOW",
        "account_id": "ACCT",
        "scenario_id": "scenario-a",
        "simulator_version": "x",
        "assumptions": {},
        "feed": None,
        "estimated": False,
        "broker_confirmed": True,
        "data": {},
    }
    DomainEventJournal(h.session.connection, clock=FakeClock()).append(
        [
            NewEvent(
                account_scope_id=h.scope.scope_id,
                stream_type="shadow_ledger",
                stream_id=h.scope.scope_id,
                event_type=CASH_ADJUSTED,
                payload=payload,
                occurred_at=NOW,
            )
        ]
    )
    with pytest.raises(PersistenceError) as raised:
        h.ledger.read_events()
    assert raised.value.code == "ledger_row_unlabeled"


def test_replay_rejects_a_ledger_that_breaks_account_rules(h: Harness) -> None:
    # an event that no valid account operation could have written: a sell with no position
    payload = {
        "mode": "SHADOW",
        "account_id": "ACCT",
        "scenario_id": "scenario-a",
        "simulator_version": "x",
        "assumptions": {},
        "feed": None,
        "estimated": True,
        "broker_confirmed": False,
        "data": {"adjustment_id": "w", "amount": "-99999", "reason": "r", "actor": "a"},
    }
    DomainEventJournal(h.session.connection, clock=FakeClock()).append(
        [
            NewEvent(
                account_scope_id=h.scope.scope_id,
                stream_type="shadow_ledger",
                stream_id=h.scope.scope_id,
                event_type=CASH_ADJUSTED,
                payload=payload,
                occurred_at=NOW,
            )
        ]
    )
    with pytest.raises(PersistenceError) as raised:
        h.reopen()
    assert raised.value.code == "ledger_replay_invalid"


def test_state_copy_is_detached(h: Harness) -> None:
    h.buy("b1")
    copy_ = h.account.state
    assert isinstance(copy_, ShadowAccountState)
    copy_.cash = D("0")
    copy_.positions.clear()
    assert h.account.state.cash == D("9000")
    assert "AAPL" in h.account.state.positions


def test_shadow_modules_never_import_alpaca_or_use_floats() -> None:
    for module in (account_module, ledger_module):
        tree = ast.parse(Path(module.__file__ or "").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                names = []
            assert not any("alpaca" in name.lower() for name in names), names
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float)
            # `isinstance(x, float)` guards are allowed; float-typed values are not
            annotation = getattr(node, "annotation", None) or getattr(node, "returns", None)
            if annotation is not None:
                assert "float" not in ast.unparse(annotation)
