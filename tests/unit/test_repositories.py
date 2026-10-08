from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    ControlCommand,
    ControlCommandKind,
    HealthStatus,
)
from mike_trade.domain.errors import BrokerRejectedError, ModeIsolationError, PersistenceError
from mike_trade.domain.protection import ProtectionKind, ProtectionStatus, TrailingStopState
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    BudgetPolicy,
    EntryEligibilityPolicy,
    Fill,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    PositionCycleState,
    TimeInForce,
)
from mike_trade.persistence.journal import DomainEventJournal, JournalWriteError
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    BrokerOrderRepository,
    BudgetExceededError,
    CashEquityRepository,
    EntryControlRepository,
    FillRepository,
    IntentReceipt,
    OperatorCommandRepository,
    OrderIntentRepository,
    PositionCycleRepository,
    ProtectionRepository,
    ReconciliationRepository,
    RunConfigRepository,
    ServiceHealthRepository,
    StrategyStateRepository,
)
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import (
    CrossScopeWriteError,
    ScopedWriter,
    WriterBusyError,
    WriterLock,
    WriterLockHeldError,
    WriterSession,
)

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
SCOPE = AccountScope(AccountEnvironment.PAPER, "ACCT1")
OTHER_SCOPE = AccountScope(AccountEnvironment.PAPER, "ACCT2")
POLICY = BudgetPolicy(
    per_symbol_budget=Decimal("1000"),
    total_application_budget=Decimal("1500"),
    max_open_positions=5,
)
D = Decimal
TEN = D("10")
ZERO = D("0")


class FakeClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


class Env:
    def __init__(self, db_path: Path, session: WriterSession, clock: FakeClock) -> None:
        self.db_path = db_path
        self.session = session
        self.clock = clock
        self.writer: ScopedWriter = session.bind_scope(SCOPE.scope_id)
        self.scopes = AccountScopeRepository(self.writer, clock)
        self.cycles = PositionCycleRepository(self.writer, clock)
        self.intents = OrderIntentRepository(self.writer, clock, POLICY)
        self.orders = BrokerOrderRepository(self.writer, clock)
        self.fills = FillRepository(self.writer, clock)
        self.protection = ProtectionRepository(self.writer, clock)
        self.controls = EntryControlRepository(self.writer, clock)
        self.recon = ReconciliationRepository(self.writer, clock)

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in self.writer.connection.execute(sql, args).fetchall()]

    def count(self, table: str) -> int:
        return int(self.rows(f"SELECT COUNT(*) FROM {table}")[0][0])

    def event_types(self) -> list[str]:
        rows = self.rows("SELECT event_type FROM domain_events ORDER BY event_seq")
        return [row[0] for row in rows]

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        tables = (
            "fills",
            "broker_orders",
            "position_cycles",
            "budget_reservations",
            "positions",
            "trailing_stop_states",
            "trailing_stop_events",
            "domain_events",
        )
        return {table: self.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in tables}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    db_path = tmp_path / "ops.db"
    initialize_database(db_path, AccountEnvironment.PAPER, now=NOW).close()
    session = WriterSession(db_path)
    environment = Env(db_path, session, FakeClock())
    environment.scopes.register_scope(SCOPE)
    yield environment
    session.close()


def new_cycle(env: Env, cycle_id: str = "c1", symbol: str = "AAPL") -> PositionCycle:
    return env.cycles.open_cycle(
        PositionCycle(cycle_id=cycle_id, account_scope_id=SCOPE.scope_id, symbol=symbol)
    )


def make_intent(
    intent_id: str = "i1",
    *,
    cycle_id: str = "c1",
    symbol: str = "AAPL",
    side: OrderSide = OrderSide.BUY,
    purpose: OrderPurpose = OrderPurpose.ENTRY,
    quantity: Decimal = TEN,
    scope_id: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        client_order_id=f"client-{intent_id}",
        account_scope_id=scope_id or SCOPE.scope_id,
        cycle_id=cycle_id,
        symbol=symbol,
        side=side,
        purpose=purpose,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        created_at=NOW,
        quantity=quantity,
    )


def make_order(
    order_id: str,
    intent_id: str,
    *,
    symbol: str = "AAPL",
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = TEN,
    status: BrokerOrderStatus = BrokerOrderStatus.ACCEPTED,
    filled: Decimal = ZERO,
    average: Decimal | None = None,
    updated_offset: int = 1,
    scope_id: str | None = None,
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=order_id,
        client_order_id=f"client-{intent_id}",
        account_scope_id=scope_id or SCOPE.scope_id,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        status=status,
        quantity=quantity,
        filled_quantity=filled,
        filled_avg_price=average,
        submitted_at=NOW,
        updated_at=NOW + timedelta(seconds=updated_offset),
    )


def make_fill(
    activity_id: str,
    order_id: str,
    quantity: str,
    price: str,
    *,
    side: OrderSide = OrderSide.BUY,
    symbol: str = "AAPL",
    fee: str = "0",
    offset: int = 5,
    scope_id: str | None = None,
) -> Fill:
    return Fill(
        fill_id=activity_id,
        broker_order_id=order_id,
        account_scope_id=scope_id or SCOPE.scope_id,
        symbol=symbol,
        side=side,
        quantity=D(quantity),
        price=D(price),
        fee=D(fee),
        filled_at=NOW + timedelta(seconds=offset),
    )


def enter(env: Env, *, quantity: str = "10", reservation: str = "1000") -> IntentReceipt:
    """Cycle c1 + ENTRY intent i1 + broker order b1 for AAPL."""
    new_cycle(env)
    receipt = env.intents.record_intent(make_intent(quantity=D(quantity)), D(reservation))
    env.orders.upsert_broker_order(make_order("b1", "i1", quantity=D(quantity)))
    return receipt


def add_exit_order(env: Env, quantity: str, *, intent_id: str = "i2", order_id: str = "b2") -> None:
    env.intents.record_intent(
        make_intent(
            intent_id,
            side=OrderSide.SELL,
            purpose=OrderPurpose.STRATEGY_EXIT,
            quantity=D(quantity),
        ),
        D("0"),
    )
    env.orders.upsert_broker_order(
        make_order(order_id, intent_id, side=OrderSide.SELL, quantity=D(quantity))
    )


def reservation_row(env: Env, intent_id: str = "i1") -> tuple[Any, ...]:
    rows = env.rows(
        "SELECT state, amount FROM budget_reservations WHERE reservation_id = ?",
        (f"rsv:{intent_id}",),
    )
    return rows[0]


# ------------------------------------------------------------------ writer and scope


def test_second_writer_lock_raises_and_release_allows_reacquire(env: Env) -> None:
    with pytest.raises(WriterLockHeldError):
        WriterLock(env.db_path)
    with pytest.raises(WriterLockHeldError):
        WriterSession(env.db_path)

    env.session.close()
    with WriterLock(env.db_path) as lock:
        assert lock.held
    assert not lock.held


def test_bind_scope_rejects_foreign_environment(env: Env) -> None:
    with pytest.raises(ModeIsolationError):
        env.session.bind_scope("live:ACCT1")


def test_writing_another_scope_raises_cross_scope_error(env: Env) -> None:
    enter(env)
    before = env.snapshot()

    with pytest.raises(CrossScopeWriteError):
        env.intents.record_intent(
            make_intent("x1", scope_id=OTHER_SCOPE.scope_id), D("10")
        )
    with pytest.raises(CrossScopeWriteError):
        env.orders.upsert_broker_order(
            make_order("bx", "x1", scope_id=OTHER_SCOPE.scope_id)
        )
    with pytest.raises(CrossScopeWriteError):
        env.fills.apply_fill(make_fill("ax", "b1", "1", "100", scope_id=OTHER_SCOPE.scope_id))
    with pytest.raises(CrossScopeWriteError):
        env.cycles.open_cycle(
            PositionCycle(cycle_id="cx", account_scope_id=OTHER_SCOPE.scope_id, symbol="AAPL")
        )
    with pytest.raises(CrossScopeWriteError):
        env.scopes.register_scope(OTHER_SCOPE)
    assert env.snapshot() == before


def test_unit_of_work_exception_rolls_back_everything(env: Env) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with env.writer.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO position_cycles(cycle_id, account_scope_id, symbol, direction, "
                "state, created_at, updated_at) VALUES ('cz', ?, 'AAPL', 'LONG', "
                "'PENDING_ENTRY', 't', 't')",
                (SCOPE.scope_id,),
            )
            conn.execute(
                "INSERT INTO budget_reservations(reservation_id, account_scope_id, cycle_id, "
                "amount, state, created_at) VALUES ('rz', ?, 'cz', '5', 'ACTIVE', 't')",
                (SCOPE.scope_id,),
            )
            raise RuntimeError("boom")
    assert env.count("position_cycles") == 0
    assert env.count("budget_reservations") == 0
    assert not env.writer.connection.in_transaction


def test_nested_unit_of_work_raises(env: Env) -> None:
    with env.writer.unit_of_work():
        with pytest.raises(PersistenceError, match="nested"):
            with env.writer.unit_of_work():
                pass
    with env.writer.unit_of_work():  # the outer scope still closed cleanly
        pass


def test_busy_database_raises_writer_busy_error(env: Env) -> None:
    other = sqlite3.connect(env.db_path, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        env.writer.connection.execute("PRAGMA busy_timeout = 50")
        with pytest.raises(WriterBusyError):
            with env.writer.unit_of_work():
                pass
    finally:
        other.execute("ROLLBACK")
        other.close()
    with env.writer.unit_of_work():
        pass


def test_scope_and_run_config_registration_are_idempotent(env: Env) -> None:
    assert env.scopes.register_scope(SCOPE) == SCOPE
    assert env.scopes.get_scope() == SCOPE
    assert env.count("account_scopes") == 1

    configs = RunConfigRepository(env.writer, env.clock)
    first = configs.record_run_config({"budget": D("1500"), "symbols": ["AAPL"]})
    assert configs.record_run_config({"symbols": ["AAPL"], "budget": D("1500")}) == first
    assert env.count("run_configs") == 1
    assert env.rows("SELECT config_fingerprint_sha256 FROM run_configs") == [(first,)]


# ------------------------------------------------------------------------- intents


def test_record_intent_commits_intent_reservation_and_event_together(env: Env) -> None:
    new_cycle(env)
    receipt = env.intents.record_intent(make_intent(), D("1000"))

    assert receipt.reservation_id == "rsv:i1"
    assert not receipt.duplicate
    assert env.rows("SELECT status, quantity FROM order_intents") == [("RECORDED", "10")]
    assert reservation_row(env) == ("ACTIVE", "1000")
    events = env.rows(
        "SELECT event_seq, event_type, idempotency_key FROM domain_events "
        "WHERE event_type = 'OrderIntentRecorded'"
    )
    assert events == [(receipt.event_seq, "OrderIntentRecorded", "client-i1")]
    assert env.writer.connection.in_transaction is False

    again = env.intents.record_intent(make_intent(), D("1000"))
    assert again.duplicate and again.event_seq == receipt.event_seq
    assert env.count("order_intents") == 1
    assert env.count("budget_reservations") == 1


def test_exit_intents_reserve_nothing_and_reject_nonzero_reservation(env: Env) -> None:
    enter(env)
    with pytest.raises(ValueError):
        env.intents.record_intent(
            make_intent("i2", side=OrderSide.SELL, purpose=OrderPurpose.STRATEGY_EXIT), D("5")
        )
    receipt = env.intents.record_intent(
        make_intent("i2", side=OrderSide.SELL, purpose=OrderPurpose.STRATEGY_EXIT), D("0")
    )
    assert receipt.reservation_id is None
    assert env.count("budget_reservations") == 1


def test_journal_failure_leaves_no_rows_and_submit_is_never_called(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_cycle(env)
    events_before = env.count("domain_events")

    def fail(*args: object, **kwargs: object) -> None:
        raise JournalWriteError("injected journal failure")

    monkeypatch.setattr(DomainEventJournal, "append_in_transaction", fail)
    calls: list[IntentReceipt] = []

    with pytest.raises(JournalWriteError):
        receipt = env.intents.record_intent(make_intent(), D("1000"))
        env.intents.submit_recorded_intent(receipt, lambda r: calls.append(r))  # type: ignore[arg-type,return-value]

    assert calls == []
    assert env.count("order_intents") == 0
    assert env.count("budget_reservations") == 0
    assert env.count("domain_events") == events_before


def test_submit_request_journal_failure_keeps_intent_and_skips_submit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = enter(env)
    calls: list[IntentReceipt] = []

    def fail(*args: object, **kwargs: object) -> None:
        raise JournalWriteError("injected journal failure")

    monkeypatch.setattr(DomainEventJournal, "append", fail)
    with pytest.raises(JournalWriteError):
        env.intents.submit_recorded_intent(receipt, lambda r: calls.append(r))  # type: ignore[arg-type,return-value]

    assert calls == []
    assert env.rows("SELECT status FROM order_intents") == [("RECORDED",)]


def test_successful_submit_marks_submitted_after_durable_record(env: Env) -> None:
    receipt = enter(env)
    seen: list[str] = []

    def submit(r: IntentReceipt) -> str:
        # By the time the broker call runs, the intent and its request are durable.
        durable = sqlite3.connect(env.db_path)
        try:
            seen.append(durable.execute("SELECT status FROM order_intents").fetchone()[0])
            seen.append(
                durable.execute(
                    "SELECT event_type FROM domain_events ORDER BY event_seq DESC LIMIT 1"
                ).fetchone()[0]
            )
        finally:
            durable.close()
        return "b1"

    assert env.intents.submit_recorded_intent(receipt, submit) == "b1"
    assert seen == ["RECORDED", "OrderSubmitRequested"]
    assert env.rows("SELECT status FROM order_intents") == [("SUBMITTED",)]
    assert "OrderIntentSubmitted" in env.event_types()
    with pytest.raises(PersistenceError):  # never submitted twice
        env.intents.submit_recorded_intent(receipt, lambda r: "b1")


def test_submit_failure_marks_unknown_and_keeps_intent_and_reservation(env: Env) -> None:
    receipt = enter(env)

    def submit(r: IntentReceipt) -> str:
        raise TimeoutError("broker did not answer")

    with pytest.raises(TimeoutError):
        env.intents.submit_recorded_intent(receipt, submit)

    assert env.rows("SELECT status FROM order_intents") == [("UNKNOWN",)]
    assert reservation_row(env)[0] == "ACTIVE"
    assert "OrderIntentUnknown" in env.event_types()
    with pytest.raises(PersistenceError):
        env.intents.submit_recorded_intent(receipt, lambda r: None)
    assert env.count("order_intents") == 1


def test_broker_rejection_marks_submit_failed_and_releases_reservation(env: Env) -> None:
    receipt = enter(env)

    def submit(r: IntentReceipt) -> str:
        raise BrokerRejectedError("insufficient buying power")

    with pytest.raises(BrokerRejectedError):
        env.intents.submit_recorded_intent(receipt, submit)

    assert env.rows("SELECT status FROM order_intents") == [("SUBMIT_FAILED",)]
    assert reservation_row(env)[0] == "RELEASED"
    assert "OrderIntentSubmitFailed" in env.event_types()


def test_total_budget_exceeded_across_symbols(env: Env) -> None:
    new_cycle(env, "c1", "AAPL")
    new_cycle(env, "c2", "MSFT")
    env.intents.record_intent(make_intent("i1"), D("900"))
    before = env.snapshot()

    with pytest.raises(BudgetExceededError) as raised:
        env.intents.record_intent(
            make_intent("i2", cycle_id="c2", symbol="MSFT"), D("700")
        )

    assert raised.value.context["limit"] == "total"
    assert env.snapshot() == before
    assert env.count("order_intents") == 1


def test_per_symbol_budget_exceeded_within_total(env: Env) -> None:
    new_cycle(env, "c1", "AAPL")
    env.intents.record_intent(make_intent("i1"), D("900"))

    with pytest.raises(BudgetExceededError) as raised:
        env.intents.record_intent(make_intent("i2"), D("200"))

    assert raised.value.context["limit"] == "per_symbol"
    assert env.count("order_intents") == 1
    assert env.count("budget_reservations") == 1


def test_open_position_cost_counts_toward_budget(env: Env) -> None:
    enter(env, reservation="900")
    env.fills.apply_fill(make_fill("a1", "b1", "10", "90"))
    assert reservation_row(env)[0] == "CONSUMED"
    env.cycles.open_cycle(
        PositionCycle(cycle_id="c2", account_scope_id=SCOPE.scope_id, symbol="AAPL")
    )

    with pytest.raises(BudgetExceededError) as raised:
        env.intents.record_intent(make_intent("i2", cycle_id="c2"), D("200"))
    assert raised.value.context["limit"] == "per_symbol"
    env.intents.record_intent(make_intent("i3", cycle_id="c2"), D("100"))


# ---------------------------------------------------------------------------- fills


def test_partial_buy_fills_accumulate_into_open_cycle_and_position(env: Env) -> None:
    enter(env)

    first = env.fills.apply_fill(make_fill("a1", "b1", "4", "100", fee="1"))
    assert first.applied and not first.duplicate
    assert first.cycle is not None and first.cycle.state is PositionCycleState.OPEN
    assert reservation_row(env) == ("ACTIVE", "600")
    assert env.rows("SELECT quantity, average_entry_price, management FROM positions") == [
        ("4", "100", "MANAGED")
    ]

    env.fills.apply_fill(make_fill("a2", "b1", "6", "101", fee="1", offset=6))

    cycle = env.cycles.get_cycle("c1")
    assert cycle is not None
    assert cycle.state is PositionCycleState.OPEN
    assert cycle.entry_quantity == D("10")
    assert cycle.avg_entry_price == D("100.6")
    assert cycle.opened_at == NOW + timedelta(seconds=5)
    assert env.rows("SELECT quantity, average_entry_price, cycle_id FROM positions") == [
        ("10", "100.6", "c1")
    ]
    assert env.rows("SELECT filled_quantity, filled_avg_price FROM broker_orders") == [
        ("10", "100.6")
    ]
    assert reservation_row(env)[0] == "CONSUMED"
    assert [c.cycle_id for c in env.cycles.list_open()] == ["c1"]
    assert env.event_types().count("FillApplied") == 2
    journal = DomainEventJournal(env.writer.connection, clock=env.clock)
    assert journal.verify_chain(SCOPE.scope_id) == []


def test_duplicate_external_activity_id_is_a_noop(env: Env) -> None:
    enter(env)
    env.fills.apply_fill(make_fill("a1", "b1", "4", "100"))
    before = env.snapshot()

    result = env.fills.apply_fill(make_fill("a1", "b1", "4", "100"))

    assert result.duplicate and not result.applied
    assert env.snapshot() == before
    assert env.count("fills") == 1


def test_over_exit_rolls_back_without_partial_rows(env: Env) -> None:
    enter(env)
    env.fills.apply_fill(make_fill("a1", "b1", "10", "100"))
    add_exit_order(env, "20")
    before = env.snapshot()

    with pytest.raises(ValueError, match="exit quantity"):
        env.fills.apply_fill(make_fill("a2", "b2", "12", "101", side=OrderSide.SELL))

    assert env.snapshot() == before
    cycle = env.cycles.get_cycle("c1")
    assert cycle is not None and cycle.state is PositionCycleState.OPEN


def test_buy_after_exit_rolls_back_without_partial_rows(env: Env) -> None:
    enter(env)
    env.fills.apply_fill(make_fill("a1", "b1", "10", "100"))
    add_exit_order(env, "4")
    env.fills.apply_fill(make_fill("a2", "b2", "4", "101", side=OrderSide.SELL))
    env.orders.upsert_broker_order(
        make_order("b3", "i1", quantity=D("5"), updated_offset=2), intent_id="i1"
    )
    before = env.snapshot()

    with pytest.raises(ValueError, match="entry fills"):
        env.fills.apply_fill(make_fill("a3", "b3", "5", "102", offset=9))

    assert env.snapshot() == before
    cycle = env.cycles.get_cycle("c1")
    assert cycle is not None and cycle.state is PositionCycleState.EXITING


def test_fill_cannot_exceed_broker_order_quantity(env: Env) -> None:
    enter(env)
    before = env.snapshot()
    with pytest.raises(ValueError, match="broker order quantity"):
        env.fills.apply_fill(make_fill("a1", "b1", "11", "100"))
    assert env.snapshot() == before


def test_fill_for_unknown_broker_order_is_rejected(env: Env) -> None:
    enter(env)
    with pytest.raises(PersistenceError, match="unknown broker order"):
        env.fills.apply_fill(make_fill("a1", "nope", "1", "100"))
    assert env.count("fills") == 0


def test_full_exit_closes_cycle_and_releases_reservation(env: Env) -> None:
    enter(env)  # 10 reserved at 1000, only 6 filled
    env.fills.apply_fill(make_fill("a1", "b1", "6", "100"))
    assert reservation_row(env) == ("ACTIVE", "400")
    add_exit_order(env, "6")

    partial = env.fills.apply_fill(make_fill("a2", "b2", "2", "110", side=OrderSide.SELL))
    assert partial.cycle is not None and partial.cycle.state is PositionCycleState.EXITING
    assert env.rows("SELECT quantity FROM positions") == [("4",)]

    done = env.fills.apply_fill(
        make_fill("a3", "b2", "4", "111", side=OrderSide.SELL, fee="1", offset=7)
    )

    assert done.cycle is not None and done.cycle.state is PositionCycleState.CLOSED
    assert done.cycle.realized_pnl == D("2") * D("110") + D("4") * D("111") - D("600") - D("1")
    stored = env.cycles.get_cycle("c1")
    assert stored is not None and stored.state is PositionCycleState.CLOSED
    assert stored.closed_at == NOW + timedelta(seconds=7)
    assert reservation_row(env)[0] == "RELEASED"
    assert env.count("positions") == 0
    assert env.cycles.list_open() == []
    assert env.rows("SELECT filled_quantity FROM broker_orders WHERE broker_order_id = 'b2'") == [
        ("6",)
    ]


def test_trailing_coverage_is_resized_to_remaining_quantity(env: Env) -> None:
    enter(env)
    env.fills.apply_fill(make_fill("a1", "b1", "10", "100"))
    env.protection.save_trailing_state(
        TrailingStopState(
            cycle_id="c1",
            account_scope_id=SCOPE.scope_id,
            symbol="AAPL",
            kind=ProtectionKind.STEPPED_TRAILING_STOP,
            status=ProtectionStatus.ACTIVE,
            covered_quantity=D("10"),
            stop_price=D("95"),
            high_water_mark=D("100"),
        )
    )
    add_exit_order(env, "10")

    env.fills.apply_fill(make_fill("a2", "b2", "4", "101", side=OrderSide.SELL))
    state = env.protection.load_trailing_state("c1")
    assert state is not None
    assert state.covered_quantity == D("6")
    assert state.status is ProtectionStatus.ACTIVE
    assert state.stop_price == D("95")
    assert env.rows("SELECT event_type FROM trailing_stop_events") == [("COVERAGE_RESIZED",)]

    env.fills.apply_fill(make_fill("a3", "b2", "6", "102", side=OrderSide.SELL, offset=8))
    closed = env.protection.load_trailing_state("c1")
    assert closed is not None and closed.status is ProtectionStatus.CANCELED


def test_downward_stop_save_is_rejected(env: Env) -> None:
    enter(env)
    env.fills.apply_fill(make_fill("a1", "b1", "10", "100"))

    def state(stop: str, high: str) -> TrailingStopState:
        return TrailingStopState(
            cycle_id="c1",
            account_scope_id=SCOPE.scope_id,
            symbol="AAPL",
            kind=ProtectionKind.BROKER_TRAILING_STOP,
            status=ProtectionStatus.ACTIVE,
            covered_quantity=D("10"),
            stop_price=D(stop),
            high_water_mark=D(high),
        )

    env.protection.save_trailing_state(state("95", "100"))
    before = env.snapshot()

    with pytest.raises(ValueError, match="downward"):
        env.protection.save_trailing_state(state("94", "100"))
    assert env.snapshot() == before

    env.protection.save_trailing_state(state("96", "101"))
    saved = env.protection.load_trailing_state("c1")
    assert saved is not None and saved.stop_price == D("96")


def test_protection_order_status_can_be_marked_unknown(env: Env) -> None:
    enter(env)
    env.protection.record_protection_order(
        "p1", "c1", ProtectionKind.FIXED_STOP, ProtectionStatus.PENDING, D("10"),
        stop_price=D("95"),
    )
    env.protection.mark_status("p1", ProtectionStatus.UNKNOWN)
    assert env.rows("SELECT status FROM protection_orders") == [("UNKNOWN",)]
    assert "ProtectionStatusChanged" in env.event_types()
    with pytest.raises(PersistenceError):
        env.protection.mark_status("missing", ProtectionStatus.ACTIVE)
    env.protection.append_trailing_event("c1", "NOTE", payload={"k": "v"})
    assert env.count("trailing_stop_events") == 1


# ------------------------------------------------------------------- broker orders


def test_order_status_transitions_are_recorded_and_stale_snapshots_ignored(env: Env) -> None:
    enter(env)  # records CREATED with ACCEPTED
    assert env.orders.upsert_broker_order(
        make_order("b1", "i1", status=BrokerOrderStatus.PARTIALLY_FILLED, filled=D("3"),
                   average=D("100"), updated_offset=2)
    )
    assert not env.orders.upsert_broker_order(
        make_order("b1", "i1", status=BrokerOrderStatus.NEW, updated_offset=1)
    )
    # Same status again: refreshed, but not a new transition.
    env.orders.upsert_broker_order(
        make_order("b1", "i1", status=BrokerOrderStatus.PARTIALLY_FILLED, filled=D("4"),
                   average=D("100"), updated_offset=3)
    )

    assert env.rows("SELECT event_type, status FROM broker_order_events ORDER BY rowid") == [
        ("CREATED", "ACCEPTED"),
        ("STATUS_CHANGED", "PARTIALLY_FILLED"),
    ]
    assert env.rows("SELECT status, filled_quantity FROM broker_orders") == [
        ("PARTIALLY_FILLED", "4")
    ]
    assert env.rows("SELECT intent_id FROM broker_orders") == [("i1",)]


def test_unfilled_remainder_is_released_when_order_ends(env: Env) -> None:
    enter(env)
    env.fills.apply_fill(make_fill("a1", "b1", "4", "100"))
    assert reservation_row(env) == ("ACTIVE", "600")

    env.orders.upsert_broker_order(
        make_order("b1", "i1", status=BrokerOrderStatus.CANCELED, filled=D("4"),
                   average=D("100"), updated_offset=9)
    )

    assert reservation_row(env)[0] == "RELEASED"
    cycle = env.cycles.get_cycle("c1")
    assert cycle is not None and cycle.entry_quantity == D("4")


def test_record_activity_is_idempotent(env: Env) -> None:
    assert env.orders.record_activity("act-1", "FILL", NOW, {"qty": D("1")})
    assert not env.orders.record_activity("act-1", "FILL", NOW, {"qty": D("1")})
    assert env.count("broker_activities") == 1


# --------------------------------------------------------------------- entry control


def test_consecutive_loss_lockout_is_set_at_threshold(env: Env) -> None:
    policy = EntryEligibilityPolicy(
        max_filled_entry_cycles_per_symbol_per_session=3, consecutive_loss_lockout_threshold=2
    )
    session = date(2026, 10, 8)

    assert env.controls.record_trade_result("AAPL", session, D("-5"), policy) == 1
    state = env.controls.load_eligibility_state("AAPL", session)
    assert state.consecutive_losses == 1 and state.block_reasons == ()
    assert env.count("entry_lockouts") == 0

    assert env.controls.record_trade_result("AAPL", session, D("-3"), policy) == 2
    state = env.controls.load_eligibility_state("AAPL", session)
    assert state.consecutive_losses == 2
    assert "consecutive_loss_lockout" in state.entry_blocked(NOW)
    assert env.count("entry_lockouts") == 1
    assert env.controls.load_eligibility_state("MSFT", session).block_reasons == ()

    env.controls.record_trade_result("AAPL", session, D("-1"), policy)
    assert env.count("entry_lockouts") == 1  # one active lockout, not one per loss
    assert env.controls.record_trade_result("AAPL", session, D("4"), policy) == 0


def test_loss_streak_carries_across_sessions_and_counts_entries(env: Env) -> None:
    policy = EntryEligibilityPolicy(
        max_filled_entry_cycles_per_symbol_per_session=3, consecutive_loss_lockout_threshold=3
    )
    day1, day2 = date(2026, 10, 7), date(2026, 10, 8)
    env.controls.record_trade_result("AAPL", day1, D("-1"), policy)
    env.controls.record_trade_result("AAPL", day1, D("-1"), policy)
    assert env.controls.record_trade_result("AAPL", day2, D("-1"), policy) == 3
    assert "consecutive_loss_lockout" in env.controls.load_eligibility_state(
        "AAPL", day2
    ).block_reasons

    assert env.controls.increment_filled_entry_cycles("AAPL", day2) == 1
    assert env.controls.increment_filled_entry_cycles("AAPL", day2, "strat-a") == 1
    assert env.controls.load_eligibility_state("AAPL", day2).filled_entry_cycles == 2
    assert env.controls.load_eligibility_state("AAPL", day1).filled_entry_cycles == 0


def test_kill_switch_pause_and_cooldown_block_entries_only(env: Env) -> None:
    session = date(2026, 10, 8)
    env.controls.set_kill_switch(True, updated_by="op", reason="test")
    env.controls.set_paused(True, updated_by="op")
    until = NOW + timedelta(minutes=10)
    env.controls.set_cooldown("AAPL", until, "post-sale")

    state = env.controls.load_eligibility_state("AAPL", session)
    assert state.block_reasons == ("entry_kill_switch", "paused")
    assert state.locked_until == until
    assert state.entry_blocked(NOW) == ("entry_kill_switch", "paused", "locked_until")

    env.controls.set_kill_switch(False, updated_by="op")
    assert env.controls.load_eligibility_state("AAPL", session).block_reasons == ("paused",)
    assert not hasattr(env.controls, "block_exits")

    # Exits still apply with every control engaged.
    enter(env)
    env.fills.apply_fill(make_fill("a1", "b1", "10", "100"))
    add_exit_order(env, "10")
    closed = env.fills.apply_fill(make_fill("a2", "b2", "10", "101", side=OrderSide.SELL))
    assert closed.cycle is not None and closed.cycle.state is PositionCycleState.CLOSED


# ----------------------------------------------------------------- reconciliation


def test_drift_recording_never_alters_fills_and_resolution_clears_it(env: Env) -> None:
    enter(env)
    env.fills.apply_fill(make_fill("a1", "b1", "10", "100"))
    fills_before = env.rows("SELECT * FROM fills")
    orders_before = env.rows("SELECT * FROM broker_orders")

    assert not env.recon.has_unresolved_drift()
    run_id = env.recon.start_run()
    drift_id = env.recon.record_drift(
        run_id, "POSITION_QUANTITY", entity_ref="AAPL",
        expected={"quantity": D("10")}, actual={"quantity": D("7")},
    )

    assert env.recon.has_unresolved_drift()
    assert env.rows("SELECT * FROM fills") == fills_before
    assert env.rows("SELECT * FROM broker_orders") == orders_before
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.writer.connection.execute("UPDATE reconciliation_drifts SET drift_type = 'X'")

    env.recon.resolve_drift(drift_id, "ACCEPT_BROKER", "operator", {"note": "manual"})

    assert not env.recon.has_unresolved_drift()
    assert env.count("reconciliation_resolutions") == 1
    assert env.count("reconciliation_drifts") == 1
    assert env.rows("SELECT * FROM fills") == fills_before
    env.recon.complete_run(run_id, "DRIFTED", {"drifts": 1})
    assert env.rows("SELECT status FROM reconciliation_runs") == [("DRIFTED",)]


# -------------------------------------------------------------- remaining repositories


def test_operator_command_is_append_only_and_idempotent(env: Env) -> None:
    commands = OperatorCommandRepository(env.writer, env.clock)
    command = ControlCommand(
        command_id="cmd-1",
        kind=ControlCommandKind.PAUSE,
        scope=SCOPE,
        issued_at=NOW,
        issued_by="operator",
        reason="maintenance",
    )
    assert commands.record_command(command)
    assert not commands.record_command(command)
    assert env.count("operator_commands") == 1
    assert "OperatorCommandRecorded" in env.event_types()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.writer.connection.execute("UPDATE operator_commands SET reason = 'x'")

    foreign = ControlCommand(
        command_id="cmd-2",
        kind=ControlCommandKind.PAUSE,
        scope=OTHER_SCOPE,
        issued_at=NOW,
        issued_by="operator",
    )
    with pytest.raises(CrossScopeWriteError):
        commands.record_command(foreign)


def test_strategy_state_round_trips_with_rearm(env: Env) -> None:
    env.writer.connection.execute(
        "INSERT INTO strategy_instances(instance_id, account_scope_id, strategy_key, "
        "strategy_version, role, symbol, parameters_json, enabled, created_at) "
        "VALUES ('s1', ?, 'k', '1', 'BUY', 'AAPL', '{}', 1, 't')",
        (SCOPE.scope_id,),
    )
    states = StrategyStateRepository(env.writer, env.clock)
    assert states.load_state("s1", "AAPL") is None

    states.save_state("s1", "aapl", {"last_bar": "x", "level": D("1.5")})
    states.save_state("s1", "AAPL", {"last_bar": "y"}, armed=False, rearm_required=True)

    loaded = states.load_state("s1", "AAPL")
    assert loaded is not None
    assert loaded["state"] == {"last_bar": "y"}
    assert loaded["armed"] is False and loaded["rearm_required"] is True
    assert env.count("strategy_state") == 1


def test_cash_equity_and_health_repositories(env: Env) -> None:
    cash = CashEquityRepository(env.writer, env.clock)
    assert cash.append_cash_event("DEPOSIT", D("100.25"), NOW, external_activity_id="cash-1")
    assert not cash.append_cash_event("DEPOSIT", D("100.25"), NOW, external_activity_id="cash-1")
    assert cash.append_cash_event("FEE", D("-1"), NOW)
    assert cash.append_cash_event("FEE", D("-1"), NOW)
    assert env.count("cash_events") == 3

    snapshot = AccountSnapshot(
        scope=SCOPE, as_of=NOW, cash=D("1000"), equity=D("1200.5"), buying_power=D("2000"),
        source="broker",
    )
    assert cash.append_equity_snapshot(snapshot)
    assert not cash.append_equity_snapshot(snapshot)
    assert env.rows("SELECT cash, equity, buying_power FROM equity_snapshots") == [
        ("1000", "1200.5", "2000")
    ]
    with pytest.raises(CrossScopeWriteError):
        cash.append_equity_snapshot(
            AccountSnapshot(scope=OTHER_SCOPE, as_of=NOW, cash=D("1"), equity=D("1"),
                            buying_power=D("1"), source="broker")
        )

    health = ServiceHealthRepository(env.writer, env.clock)
    health.heartbeat("engine", details={"mode": "paper"})
    env.clock.current = NOW + timedelta(seconds=30)
    health.heartbeat("engine", HealthStatus.DEGRADED)
    assert env.rows("SELECT status, heartbeat_at FROM service_health") == [
        ("DEGRADED", "2026-10-08T14:30:30.000000Z")
    ]
