"""Strategy trade performance attribution and CSV/JSON export tests.

Everything runs on temporary migrated SQLite databases seeded through the
existing persistence writers (plus direct projection inserts only for tables
that have no writer: strategy_instances, signals, strategy_evaluations,
decisions), or on in-memory projection fixtures.  No network access, no
broker credentials, no floats for money.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.accounts import ControlCommand, ControlCommandKind
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
    TimeInForce,
)
from mike_trade.persistence.readstore import ReadStore
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    BrokerOrderRepository,
    EntryControlRepository,
    FillRepository,
    OperatorCommandRepository,
    OrderIntentRepository,
    PositionCycleRepository,
    ProtectionRepository,
    ReconciliationRepository,
)
from mike_trade.persistence.schema import (
    decimal_to_db,
    from_db_timestamp,
    initialize_database,
    to_db_timestamp,
)
from mike_trade.persistence.transaction import ScopedWriter, WriterSession
from mike_trade.reporting import export as export_mod
from mike_trade.reporting.export import REPORT_SCHEMAS, export_csv, export_json
from mike_trade.reporting.metrics import (
    PERFORMANCE_DISCLAIMER,
    ExitCause,
    ReportFilter,
    build_trade_cycles,
    compare_accounts,
    count_evaluation_outcomes,
    summarize_by_pair,
    summarize_by_strategy,
    summarize_by_symbol,
)

D = Decimal
ZERO = D("0")

SCOPE = AccountScope(AccountEnvironment.PAPER, "RPT1")
SCOPE_B = AccountScope(AccountEnvironment.PAPER, "RPT2")
BUDGET = BudgetPolicy(
    per_symbol_budget=D("5000"), total_application_budget=D("20000"), max_open_positions=5
)

T0 = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
BUY_1 = datetime(2026, 10, 8, 14, 31, tzinfo=UTC)
BUY_2 = datetime(2026, 10, 8, 14, 32, tzinfo=UTC)
SELL_1 = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
SELL_2 = datetime(2026, 10, 8, 15, 1, tzinfo=UTC)
BAR = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = SELL_2) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class Harness:
    """Write side for one scope, built only from existing repositories."""

    def __init__(self, session: WriterSession, scope: AccountScope, clock: FakeClock) -> None:
        self.scope = scope
        self.clock = clock
        self.writer: ScopedWriter = session.bind_scope(scope.scope_id)
        self.session = session
        AccountScopeRepository(self.writer, clock).register_scope(scope)
        self.cycles = PositionCycleRepository(self.writer, clock)
        self.intents = OrderIntentRepository(self.writer, clock, BUDGET)
        self.orders = BrokerOrderRepository(self.writer, clock)
        self.fills = FillRepository(self.writer, clock)
        self.protection = ProtectionRepository(self.writer, clock)
        self.controls = EntryControlRepository(self.writer, clock)

    def seed_instance(self, instance_id: str, strategy_key: str, role: str,
                      symbol: str = "AAPL", pair_key: str | None = None) -> None:
        with self.session.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO strategy_instances(instance_id, account_scope_id, strategy_key, "
                "strategy_version, role, symbol, parameters_json, enabled, pair_key, "
                "created_at) VALUES (?, ?, ?, '1', ?, ?, '{}', 1, ?, ?)",
                (instance_id, self.scope.scope_id, strategy_key, role, symbol, pair_key,
                 to_db_timestamp(T0)),
            )

    def seed_signal(self, signal_id: str, instance_id: str, action: str,
                    symbol: str = "AAPL", reference_price: str | None = None,
                    bar_time: datetime = BAR, generated_at: datetime = T0) -> None:
        with self.session.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO signals(signal_id, account_scope_id, instance_id, symbol, "
                "action, reference_price, confidence, reason, provenance_json, bar_time, "
                "generated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, 'test', '{}', ?, ?)",
                (signal_id, self.scope.scope_id, instance_id, symbol, action,
                 reference_price, to_db_timestamp(bar_time), to_db_timestamp(generated_at)),
            )

    def open_cycle(self, tag: str, symbol: str = "AAPL",
                   buy_strategy: str | None = "vwap_bounce") -> str:
        cycle_id = f"cycle-{tag}"
        self.cycles.open_cycle(
            PositionCycle(cycle_id=cycle_id, account_scope_id=self.scope.scope_id,
                          symbol=symbol, entry_strategy_key=buy_strategy)
        )
        return cycle_id

    def intent(self, tag: str, cycle_id: str, side: OrderSide, qty: str,
               purpose: OrderPurpose, signal_id: str | None = None,
               symbol: str = "AAPL", created_at: datetime = T0) -> str:
        intent = OrderIntent(
            intent_id=f"intent-{tag}", client_order_id=f"client-{tag}",
            account_scope_id=self.scope.scope_id, cycle_id=cycle_id, symbol=symbol,
            side=side, purpose=purpose, order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY, created_at=created_at, quantity=D(qty),
            signal_id=signal_id,
        )
        self.intents.record_intent(intent, D("1500") if purpose is OrderPurpose.ENTRY else ZERO)
        return f"client-{tag}"

    def order(self, tag: str, symbol: str, side: OrderSide, qty: str,
              submitted_at: datetime = T0) -> str:
        order_id = f"order-{tag}"
        self.orders.upsert_broker_order(
            BrokerOrder(
                broker_order_id=order_id, client_order_id=f"client-{tag}",
                account_scope_id=self.scope.scope_id, symbol=symbol, side=side,
                order_type=OrderType.MARKET, status=BrokerOrderStatus.ACCEPTED,
                quantity=D(qty), filled_quantity=ZERO, submitted_at=submitted_at,
                updated_at=submitted_at + timedelta(seconds=1),
            )
        )
        return order_id

    def fill(self, tag: str, order_id: str, side: OrderSide, qty: str, price: str,
             at: datetime, symbol: str = "AAPL", fee: str = "0") -> None:
        result = self.fills.apply_fill(
            Fill(fill_id=f"fill-{tag}", broker_order_id=order_id,
                 account_scope_id=self.scope.scope_id, symbol=symbol, side=side,
                 quantity=D(qty), price=D(price), fee=D(fee), filled_at=at)
        )
        assert result.applied

    def round_trip(self, tag: str, exit_purpose: OrderPurpose = OrderPurpose.STRATEGY_EXIT,
                   exit_signal: str | None = None, symbol: str = "AAPL") -> str:
        """Buy 10 (4@100, 6@101) then sell 10 (4@104, 6@106): gross +46."""
        cycle_id = self.open_cycle(tag, symbol)
        self.intent(f"buy-{tag}", cycle_id, OrderSide.BUY, "10", OrderPurpose.ENTRY,
                    symbol=symbol)
        buy_order = self.order(f"buy-{tag}", symbol, OrderSide.BUY, "10",
                               submitted_at=T0 + timedelta(seconds=10))
        self.fill(f"buy-{tag}-1", buy_order, OrderSide.BUY, "4", "100", BUY_1, symbol)
        self.fill(f"buy-{tag}-2", buy_order, OrderSide.BUY, "6", "101", BUY_2, symbol)
        self.intent(f"sell-{tag}", cycle_id, OrderSide.SELL, "10", exit_purpose,
                    signal_id=exit_signal, symbol=symbol)
        sell_order = self.order(f"sell-{tag}", symbol, OrderSide.SELL, "10",
                                submitted_at=SELL_1 - timedelta(seconds=5))
        self.fill(f"sell-{tag}-1", sell_order, OrderSide.SELL, "4", "104", SELL_1, symbol)
        self.fill(f"sell-{tag}-2", sell_order, OrderSide.SELL, "6", "106", SELL_2, symbol)
        return cycle_id

    def confirm_fee(self, order_id: str, amount: str, activity_id: str) -> None:
        assert self.orders.record_activity(
            activity_id, "TAF", SELL_2,
            {"activity_id": activity_id, "category": "FEE", "fee_amount": amount,
             "order_id": order_id, "occurred_at": to_db_timestamp(SELL_2)},
        )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[tuple[Harness, ReadStore, Path]]:
    path = tmp_path / "ops.db"
    initialize_database(path, AccountEnvironment.PAPER, now=T0).close()
    session = WriterSession(path)
    clock = FakeClock()
    store = ReadStore.open(path)
    yield Harness(session, SCOPE, clock), store, path
    store.close()
    session.close()


def filt(**kwargs) -> ReportFilter:
    kwargs.setdefault("account_scope_id", SCOPE.scope_id)
    return ReportFilter(**kwargs)


# ------------------------------------------------------- P&L and attribution


def test_partial_fill_weighted_pnl(harness) -> None:
    account, store, _ = harness
    account.round_trip("p1")
    (record,) = build_trade_cycles(store, filt(symbol="AAPL"))

    assert record.entry_avg_price == D("100.6")
    assert record.exit_avg_price == D("105.2")
    assert record.filled_quantity == D("10")
    assert record.gross_pnl == D("46")
    assert isinstance(record.gross_pnl, Decimal)
    # No fee provenance: status missing -> UNAVAILABLE, net excludes invented fees.
    assert record.fee_status == "UNAVAILABLE"
    assert record.fees_complete is False
    assert record.fee_confirmed == ZERO and record.fee_estimated == ZERO
    assert record.net_pnl == D("46")


def test_confirmed_fee_reduces_net_and_completes_fees(harness) -> None:
    account, store, _ = harness
    account.round_trip("p1")
    account.confirm_fee("order-buy-p1", "0.05", "fee-buy")
    account.confirm_fee("order-sell-p1", "0.03", "fee-sell")
    (record,) = build_trade_cycles(store, filt())

    assert record.fee_status == "CONFIRMED"
    assert record.fees_complete is True
    assert record.fee_confirmed == D("0.08")
    assert record.fee_estimated == ZERO
    assert record.net_pnl == D("45.92")


def test_partially_confirmed_fees_stay_incomplete(harness) -> None:
    account, store, _ = harness
    account.round_trip("p1")
    account.confirm_fee("order-sell-p1", "0.03", "fee-1")
    (record,) = build_trade_cycles(store, filt())

    # The buy order has no fee provenance: worst status wins, net keeps only
    # the confirmed part, and nothing is fabricated for the missing leg.
    assert record.fee_status == "UNAVAILABLE"
    assert record.fees_complete is False
    assert record.fee_confirmed == D("0.03")
    assert record.net_pnl == D("45.97")


def test_protective_stop_exit_preserves_buy_strategy(harness) -> None:
    account, store, _ = harness
    account.round_trip("ps", exit_purpose=OrderPurpose.PROTECTIVE_STOP)
    (record,) = build_trade_cycles(store, filt())

    assert record.exit_cause is ExitCause.PROTECTIVE_STOP
    assert record.buy_strategy == "vwap_bounce"
    assert record.sell_strategy is None
    assert record.gross_pnl == D("46")


def test_exit_causes_manual_safety_end_of_day_and_flatten(harness) -> None:
    account, store, _ = harness
    account.round_trip("mc", exit_purpose=OrderPurpose.MANUAL_CLOSE)
    account.round_trip("se", exit_purpose=OrderPurpose.SAFETY_EXIT)
    account.round_trip("eod", exit_purpose=OrderPurpose.EOD_CLOSE)
    cycle_flat = account.round_trip("fl", exit_purpose=OrderPurpose.MANUAL_CLOSE,
                                     symbol="MSFT")
    commands = OperatorCommandRepository(account.writer, account.clock)
    account.clock.current = SELL_2 + timedelta(minutes=1)
    assert commands.record_command(
        ControlCommand(command_id="cmd-flat", kind=ControlCommandKind.FLATTEN,
                       scope=SCOPE, issued_at=SELL_2 - timedelta(minutes=2),
                       issued_by="operator", symbol="MSFT", reason="flatten")
    )
    records = {r.cycle_id: r for r in build_trade_cycles(store, filt())}

    assert records["cycle-mc"].exit_cause is ExitCause.MANUAL_CLOSE
    assert records["cycle-se"].exit_cause is ExitCause.SAFETY
    assert records["cycle-eod"].exit_cause is ExitCause.END_OF_DAY
    assert records[cycle_flat].exit_cause is ExitCause.FLATTEN
    for record in records.values():
        assert record.buy_strategy == "vwap_bounce"  # originating BUY preserved


def test_trailing_stop_exit_with_updates_and_confirmed_stop(harness) -> None:
    account, store, _ = harness
    cycle_id = account.open_cycle("ts")
    account.intent("buy-ts", cycle_id, OrderSide.BUY, "10", OrderPurpose.ENTRY)
    buy_order = account.order("buy-ts", "AAPL", OrderSide.BUY, "10")
    account.fill("buy-ts-1", buy_order, OrderSide.BUY, "10", "100", BUY_1)
    account.intent("sell-ts", cycle_id, OrderSide.SELL, "10", OrderPurpose.PROTECTIVE_STOP)
    sell_order = account.order("sell-ts", "AAPL", OrderSide.SELL, "10")
    account.protection.save_trailing_state(
        TrailingStopState(
            cycle_id=cycle_id, account_scope_id=SCOPE.scope_id, symbol="AAPL",
            kind=ProtectionKind.STEPPED_TRAILING_STOP, status=ProtectionStatus.ACTIVE,
            covered_quantity=D("10"), stop_price=D("99"), high_water_mark=D("104"),
            broker_order_id=sell_order, last_confirmed_at=SELL_1,
        )
    )
    account.protection.append_trailing_event(
        cycle_id, "STOP_UPDATED", previous_stop_price=D("98"), new_stop_price=D("99"),
        occurred_at=SELL_1 - timedelta(minutes=1),
    )
    account.protection.append_trailing_event(
        cycle_id, "STOP_UPDATED", previous_stop_price=D("99"), new_stop_price=D("99.5"),
        occurred_at=SELL_1,
    )
    account.fill("sell-ts-1", sell_order, OrderSide.SELL, "10", "99.5", SELL_1)
    (record,) = build_trade_cycles(store, filt())

    assert record.exit_cause is ExitCause.TRAILING_STOP
    assert record.buy_strategy == "vwap_bounce"
    # Two STOP_UPDATED rows plus the terminal PROTECTION_CLOSED row written
    # by the closing fill: every persisted trailing-stop event counts.
    assert record.trailing_update_count == 3
    assert record.last_confirmed_stop == D("99")


def test_strategy_exit_sets_sell_strategy_and_pair(harness) -> None:
    account, store, _ = harness
    account.seed_instance("buy-inst", "vwap_bounce", "BUY", pair_key="vwap_reference")
    account.seed_instance("sell-inst", "vwap_breakdown", "SELL", pair_key="vwap_reference")
    account.seed_signal("sig-entry", "buy-inst", "ENTER_LONG", reference_price="100")
    account.seed_signal("sig-exit", "sell-inst", "EXIT_LONG")
    cycle_id = account.open_cycle("pair")
    account.intent("buy-pair", cycle_id, OrderSide.BUY, "10", OrderPurpose.ENTRY,
                   signal_id="sig-entry")
    buy_order = account.order("buy-pair", "AAPL", OrderSide.BUY, "10")
    account.fill("buy-pair-1", buy_order, OrderSide.BUY, "10", "100", BUY_1)
    account.intent("sell-pair", cycle_id, OrderSide.SELL, "10",
                   OrderPurpose.STRATEGY_EXIT, signal_id="sig-exit")
    sell_order = account.order("sell-pair", "AAPL", OrderSide.SELL, "10")
    account.fill("sell-pair-1", sell_order, OrderSide.SELL, "10", "102", SELL_1)
    (record,) = build_trade_cycles(store, filt())

    assert record.exit_cause is ExitCause.STRATEGY_EXIT
    assert record.buy_strategy == "vwap_bounce"
    assert record.sell_strategy == "vwap_breakdown"
    assert record.pair == "vwap_reference"


def test_pair_aggregation(harness) -> None:
    account, store, _ = harness
    account.round_trip("a")
    account.round_trip("b", exit_purpose=OrderPurpose.PROTECTIVE_STOP)
    cycles = build_trade_cycles(store, filt())
    report = summarize_by_pair(cycles, filt())

    assert report["sample_size"] == 2
    assert report["period_label"] == "lifetime"
    assert report["performance_disclaimer"] == PERFORMANCE_DISCLAIMER
    (row,) = [r for r in report["rows"] if r["pair"] == "vwap_bounce"]
    assert row["trade_count"] == D("2")
    assert row["total_gross"] == D("92") and row["total_net"] == D("92")
    assert isinstance(row["total_net"], Decimal)
    by_strategy = summarize_by_strategy(cycles, filt())
    assert by_strategy["rows"][0]["strategy"] == "vwap_bounce"
    assert by_strategy["rows"][0]["expectancy"] == D("46")
    by_symbol = summarize_by_symbol(cycles, filt(symbol="AAPL"))
    assert by_symbol["rows"][0]["total_gross"] == D("92")


def test_slippage_and_latency_math(harness) -> None:
    account, store, _ = harness
    signal_at = T0
    account.seed_instance("buy-inst", "vwap_bounce", "BUY")
    account.seed_signal("sig-s", "buy-inst", "ENTER_LONG", reference_price="100",
                        bar_time=BAR, generated_at=signal_at)
    cycle_id = account.open_cycle("sl")
    account.intent("buy-sl", cycle_id, OrderSide.BUY, "10", OrderPurpose.ENTRY,
                   signal_id="sig-s", created_at=signal_at + timedelta(seconds=5))
    buy_order = account.order("buy-sl", "AAPL", OrderSide.BUY, "10",
                              submitted_at=signal_at + timedelta(seconds=10))
    account.fill("buy-sl-1", buy_order, OrderSide.BUY, "4", "100", BUY_1)
    account.fill("buy-sl-2", buy_order, OrderSide.BUY, "6", "101", BUY_2)
    account.intent("sell-sl", cycle_id, OrderSide.SELL, "10", OrderPurpose.STRATEGY_EXIT)
    sell_order = account.order("sell-sl", "AAPL", OrderSide.SELL, "10")
    account.fill("sell-sl-1", sell_order, OrderSide.SELL, "10", "105", SELL_2)
    (record,) = build_trade_cycles(store, filt())

    assert record.slippage_dollars == D("0.6")
    assert record.slippage_bps == D("60")
    assert isinstance(record.slippage_bps, Decimal)
    assert record.latency_signal_to_submit == timedelta(seconds=10)
    assert record.latency_submit_to_first_fill == BUY_1 - (signal_at + timedelta(seconds=10))
    assert record.latency_first_to_final_fill == SELL_2 - BUY_1
    assert record.holding_duration == SELL_2 - BUY_1


def test_in_memory_fixture_with_estimated_fees() -> None:
    fixtures = [{
        "account_scope_id": SCOPE.scope_id,
        "cycle_id": "cycle-f1",
        "symbol": "AAPL",
        "state": "CLOSED",
        "buy_strategy": "vwap_bounce",
        "sell_strategy": "vwap_breakdown",
        "exit_cause": "STRATEGY_EXIT",
        "pair": "vwap_reference",
        "opened_at": to_db_timestamp(BUY_1),
        "closed_at": to_db_timestamp(SELL_1),
        "submitted_at": to_db_timestamp(T0),
        "signal": {"strategy_key": "vwap_bounce", "role": "BUY",
                   "pair_key": "vwap_reference", "reference_price": "100",
                   "bar_time": to_db_timestamp(BAR),
                   "generated_at": to_db_timestamp(T0)},
        "entry_fills": [
            {"quantity": "4", "price": "100", "fee": "0.02",
             "fee_status": "ESTIMATED", "filled_at": to_db_timestamp(BUY_1),
             "broker_order_id": "b-1"},
            {"quantity": "6", "price": "101", "fee": "0.03",
             "fee_status": "ESTIMATED", "filled_at": to_db_timestamp(BUY_2),
             "broker_order_id": "b-1"},
        ],
        "exit_fills": [
            {"quantity": "10", "price": "105", "fee": "0.05",
             "fee_status": "ESTIMATED", "filled_at": to_db_timestamp(SELL_1),
             "broker_order_id": "s-1"},
        ],
    }]
    (record,) = build_trade_cycles(fixtures, filt())

    assert record.entry_avg_price == D("100.6")
    assert record.gross_pnl == D("44")  # 1050 - 1006
    assert record.fee_status == "ESTIMATED"
    assert record.fees_complete is True
    assert record.fee_estimated == D("0.10")
    assert record.net_pnl == D("43.90")
    assert record.pair == "vwap_reference"


def test_missing_fee_status_in_fixture_is_unavailable() -> None:
    fixtures = [{
        "account_scope_id": SCOPE.scope_id,
        "cycle_id": "cycle-f2",
        "symbol": "AAPL",
        "state": "CLOSED",
        "buy_strategy": "vwap_bounce",
        "opened_at": to_db_timestamp(BUY_1),
        "closed_at": to_db_timestamp(SELL_1),
        "entry_fills": [{"quantity": "10", "price": "100", "fee": "0",
                          "filled_at": to_db_timestamp(BUY_1)}],
        "exit_fills": [{"quantity": "10", "price": "101", "fee": "0",
                         "filled_at": to_db_timestamp(SELL_1)}],
    }]
    (record,) = build_trade_cycles(fixtures, filt())

    assert record.fee_status == "UNAVAILABLE"
    assert record.fees_complete is False
    assert record.net_pnl == D("10")  # no fabricated fees


# ------------------------------------------------------------------ filters


def test_filter_correctness(harness) -> None:
    account, store, _ = harness
    account.round_trip("f1")
    account.round_trip("f2", exit_purpose=OrderPurpose.PROTECTIVE_STOP, symbol="MSFT")

    assert len(build_trade_cycles(store, filt(symbol="AAPL"))) == 1
    assert len(build_trade_cycles(store, filt(symbol="MSFT"))) == 1
    assert len(build_trade_cycles(store, filt(symbol="TSLA"))) == 0
    assert len(build_trade_cycles(store, filt(buy_strategy="vwap_bounce"))) == 2
    assert len(build_trade_cycles(store, filt(buy_strategy="other"))) == 0
    assert len(build_trade_cycles(store, filt(exit_cause="PROTECTIVE_STOP"))) == 1
    assert len(build_trade_cycles(store, filt(exit_cause=ExitCause.STRATEGY_EXIT))) == 1
    assert len(build_trade_cycles(store, filt(pair="vwap_bounce"))) == 2
    assert len(build_trade_cycles(store, filt(pair="nope"))) == 0
    assert len(build_trade_cycles(store, filt(since=SELL_2 + timedelta(hours=1)))) == 0
    assert len(build_trade_cycles(store, filt(until=BUY_1))) == 0
    day = build_trade_cycles(store, filt(period="day", as_of=SELL_2))
    assert len(day) == 2  # 2026-10-08 ET session
    assert len(build_trade_cycles(store, filt(period="day", as_of=T0 - timedelta(days=30)))) == 0
    other = ReportFilter(account_scope_id=SCOPE_B.scope_id)
    assert build_trade_cycles(store, other) == []
    with pytest.raises(ValueError):
        ReportFilter(account_scope_id="  ")
    with pytest.raises(ValueError):
        build_trade_cycles(store, "not-a-filter")


def test_win_loss_profit_factor_and_expectancy(harness) -> None:
    account, store, _ = harness
    account.round_trip("w")  # +46
    cycle_id = account.open_cycle("l")
    account.intent("buy-l", cycle_id, OrderSide.BUY, "10", OrderPurpose.ENTRY)
    buy_order = account.order("buy-l", "AAPL", OrderSide.BUY, "10")
    account.fill("buy-l-1", buy_order, OrderSide.BUY, "10", "100", BUY_1)
    account.intent("sell-l", cycle_id, OrderSide.SELL, "10", OrderPurpose.STRATEGY_EXIT)
    sell_order = account.order("sell-l", "AAPL", OrderSide.SELL, "10")
    account.fill("sell-l-1", sell_order, OrderSide.SELL, "10", "90", SELL_1)  # -100
    cycles = build_trade_cycles(store, filt())
    (row,) = summarize_by_strategy(cycles, filt())["rows"]

    assert row["trade_count"] == D("2")
    assert row["wins"] == D("1") and row["losses"] == D("1")
    assert row["win_rate"] == D("0.5")
    assert row["avg_win"] == D("46") and row["avg_loss"] == D("-100")
    assert row["profit_factor"] == D("46") / D("100")
    assert row["expectancy"] == D("-54") / D("2")
    assert isinstance(row["profit_factor"], Decimal)


# ---------------------------------------------------------------- evaluation


def test_evaluation_rejection_and_eligibility_counts(harness) -> None:
    account, store, path = harness
    account.seed_instance("buy-e", "vwap_bounce", "BUY")
    account.seed_instance("sell-e", "vwap_breakdown", "SELL")
    account.seed_signal("sig-ok", "buy-e", "ENTER_LONG")
    with account.session.unit_of_work() as conn:
        conn.execute(
            "INSERT INTO strategy_evaluations(evaluation_id, account_scope_id, instance_id, "
            "symbol, bar_time, status, reason, error_detail, evaluated_at) VALUES "
            "('ev1', ?, 'buy-e', 'AAPL', ?, 'SIGNAL', 'trigger', NULL, ?),"
            "('ev2', ?, 'buy-e', 'AAPL', ?, 'NO_SIGNAL', 'no trigger', NULL, ?),"
            "('ev3', ?, 'sell-e', 'AAPL', ?, 'NO_SIGNAL', 'flat', NULL, ?)",
            (SCOPE.scope_id, to_db_timestamp(BAR), to_db_timestamp(T0),
             SCOPE.scope_id, to_db_timestamp(BAR), to_db_timestamp(T0),
             SCOPE.scope_id, to_db_timestamp(BAR), to_db_timestamp(T0)),
        )
        conn.execute(
            "INSERT INTO decisions(decision_id, account_scope_id, signal_id, outcome, "
            "reason, details_json, decided_at) VALUES "
            "('d1', ?, 'sig-ok', 'REJECT', 'ENTRY_CAP', '{}', ?),"
            "('d2', ?, 'sig-ok', 'REJECT', 'STALE_DATA', '{}', ?)",
            (SCOPE.scope_id, to_db_timestamp(T0), SCOPE.scope_id, to_db_timestamp(T0)),
        )
    policy = EntryEligibilityPolicy(max_filled_entry_cycles_per_symbol_per_session=5,
                                    consecutive_loss_lockout_threshold=1)
    account.controls.increment_filled_entry_cycles("AAPL", date(2026, 10, 8),
                                                   "vwap_bounce")
    account.controls.record_trade_result("AAPL", date(2026, 10, 8), D("-5"), policy)
    account.controls.set_cooldown("AAPL", SELL_2 + timedelta(hours=1), "post_sale")

    report = count_evaluation_outcomes(store, filt())
    assert report["sample_size"] == 3
    assert report["performance_disclaimer"] == PERFORMANCE_DISCLAIMER
    by_status = {r["status"]: r["count"] for r in report["evaluation_counts"]}
    assert by_status == {"SIGNAL": 1, "NO_SIGNAL": 2}
    by_reason = {r["reason"]: r["count"] for r in report["rejection_reasons"]}
    assert by_reason == {"ENTRY_CAP": 1, "STALE_DATA": 1}
    decisions = {(r["decision"], r["symbol"]): r["count"]
                 for r in report["eligibility_decisions"]}
    assert decisions[("ENTRY_CAP_FILLED", "AAPL")] == 1
    assert decisions[("LOSS_LOCKOUT:consecutive_losses:1", "AAPL")] == 1
    assert decisions[("COOLDOWN:post_sale", "AAPL")] == 1
    assert count_evaluation_outcomes(store, filt(symbol="MSFT"))["sample_size"] == 0


# ---------------------------------------------------------------- comparison


def _seed_scope_side(session: WriterSession, scope: AccountScope, tag: str,
                     bar_time: datetime, buy_price: str, sell_price: str) -> Harness:
    harness_side = Harness(session, scope, FakeClock())
    stag = f"{tag}-{scope.account_id}"  # intent/order ids are global; keep unique
    cycle_id = harness_side.open_cycle(stag)
    harness_side.intent(f"buy-{stag}", cycle_id, OrderSide.BUY, "10", OrderPurpose.ENTRY)
    buy_order = harness_side.order(f"buy-{stag}", "AAPL", OrderSide.BUY, "10")
    harness_side.fill(f"buy-{stag}-a", buy_order, OrderSide.BUY, "10", buy_price, BUY_1)
    harness_side.intent(f"sell-{stag}", cycle_id, OrderSide.SELL, "10",
                        OrderPurpose.STRATEGY_EXIT)
    sell_order = harness_side.order(f"sell-{stag}", "AAPL", OrderSide.SELL, "10")
    harness_side.fill(f"sell-{stag}-a", sell_order, OrderSide.SELL, "10", sell_price, SELL_1)
    # Pin the same entry bar timestamp on both sides for alignment.
    with session.unit_of_work() as conn:
        conn.execute(
            "INSERT INTO strategy_instances(instance_id, account_scope_id, strategy_key, "
            "strategy_version, role, symbol, parameters_json, enabled, pair_key, "
            "created_at) VALUES (?, ?, 'vwap_bounce', '1', 'BUY', 'AAPL', '{}', 1, "
            "'vwap_reference', ?)",
            (f"inst-{tag}-{scope.account_id}", scope.scope_id, to_db_timestamp(T0)),
        )
        conn.execute(
            "INSERT INTO signals(signal_id, account_scope_id, instance_id, symbol, "
            "action, reference_price, confidence, reason, provenance_json, bar_time, "
            "generated_at) VALUES (?, ?, ?, 'AAPL', 'ENTER_LONG', '100', NULL, 't', "
            "'{}', ?, ?)",
            (f"sig-{tag}-{scope.account_id}", scope.scope_id,
             f"inst-{tag}-{scope.account_id}",
             to_db_timestamp(bar_time), to_db_timestamp(T0)),
        )
        conn.execute(
            "UPDATE order_intents SET signal_id = ? WHERE account_scope_id = ? "
            "AND intent_id = ?",
            (f"sig-{tag}-{scope.account_id}", scope.scope_id, f"intent-buy-{stag}"),
        )
    return harness_side


def test_comparison_matched_missed_extra(tmp_path: Path) -> None:
    path = tmp_path / "cmp.db"
    initialize_database(path, AccountEnvironment.PAPER, now=T0).close()
    session = WriterSession(path)
    scope_a = AccountScope(AccountEnvironment.PAPER, "LIVE1")
    scope_b = AccountScope(AccountEnvironment.PAPER, "SHADOW1")
    Harness(session, scope_a, FakeClock())
    Harness(session, scope_b, FakeClock())
    _seed_scope_side(session, scope_a, "shared", BAR, "100", "105")
    _seed_scope_side(session, scope_b, "shared", BAR, "100.5", "104")
    _seed_scope_side(session, scope_a, "only-a", BAR + timedelta(hours=1), "100", "101")
    _seed_scope_side(session, scope_b, "only-b", BAR + timedelta(hours=2), "100", "102")
    store = ReadStore.open(path)
    try:
        result = compare_accounts(store, scope_a.scope_id, scope_b.scope_id)
    finally:
        store.close()
        session.close()

    assert result["scope_a"] == scope_a.scope_id
    assert result["scope_b"] == scope_b.scope_id
    assert result["sample_size_a"] == 2 and result["sample_size_b"] == 2
    assert result["performance_disclaimer"] == PERFORMANCE_DISCLAIMER
    assert "equity" not in str(result) and "balance" not in str(result)
    assert len(result["matched"]) == 1
    match = result["matched"][0]
    assert match["symbol"] == "AAPL" and match["pair"] == "vwap_reference"
    assert match["entry_price_diff"] == D("0.5")
    assert match["exit_price_diff"] == D("-1")
    assert match["net_diff"] == match["net_b"] - match["net_a"]
    assert isinstance(match["net_diff"], Decimal)
    assert len(result["missed"]) == 1 and result["missed"][0]["cycle_b"] is None
    assert len(result["extra"]) == 1 and result["extra"][0]["cycle_a"] is None
    with pytest.raises(ValueError):
        compare_accounts(store, scope_a.scope_id, scope_a.scope_id)


# ------------------------------------------------------------------- export


def _cycle_rows(harness) -> list:
    account, store, _ = harness
    account.round_trip("x1")
    return build_trade_cycles(store, filt())


def test_export_csv_and_json_are_byte_identical(harness, tmp_path: Path) -> None:
    rows = _cycle_rows(harness)
    first_csv = export_csv(rows, "cycles")
    second_csv = export_csv(rows, "cycles")
    assert first_csv.encode("utf-8") == second_csv.encode("utf-8")
    first_json = export_json(rows, "cycles")
    assert first_json == export_json(rows, "cycles")
    header = first_csv.splitlines()[0]
    assert header.split(",")[0] == "schema_version"
    assert header.split(",")[1:4] == ["cycle_id", "account_scope_id", "symbol"]
    payload = json.loads(first_json)
    assert payload["schema_version"] == "1"
    assert payload["report_type"] == "cycles"
    assert payload["sample_size"] == 1
    assert payload["performance_disclaimer"] == PERFORMANCE_DISCLAIMER


def test_decimal_as_string_and_no_floats(harness) -> None:
    rows = _cycle_rows(harness)
    payload = json.loads(export_json(rows, "cycles"))
    (entry,) = payload["rows"]
    assert D(entry["gross_pnl"]) == D("46") and D(entry["net_pnl"]) == D("46")
    assert D(entry["entry_avg_price"]) == D("100.6")
    assert all(not isinstance(v, float) for v in entry.values())
    raw = export_json(rows, "cycles")
    assert '"gross_pnl": "46' in raw  # quoted Decimal string, never a bare number
    assert '"gross_pnl": 46' not in raw.replace('"gross_pnl": "46', '')
    csv_text = export_csv(rows, "cycles")
    assert ",46.0," in csv_text and "100.6" in csv_text


def test_overwrite_refusal_and_atomic_write(harness, tmp_path: Path) -> None:
    rows = _cycle_rows(harness)
    target = tmp_path / "report.csv"
    export_csv(rows, "cycles", target)
    with pytest.raises(FileExistsError):
        export_csv(rows, "cycles", target)
    with pytest.raises(FileExistsError):
        export_json(rows, "cycles", target)
    before = target.read_bytes()
    export_csv(rows, "cycles", target, overwrite=True)
    assert target.read_bytes() == before  # identical input -> identical bytes
    assert not list(tmp_path.glob(".*.tmp"))  # no temp files left behind


def test_account_masking(harness) -> None:
    rows = _cycle_rows(harness)
    csv_text = export_csv(rows, "cycles")
    payload = json.loads(export_json(rows, "cycles"))
    assert SCOPE.scope_id not in csv_text
    assert SCOPE.scope_id not in json.dumps(payload)
    assert payload["rows"][0]["account_scope_id"].endswith("RPT1")
    assert payload["rows"][0]["account_scope_id"].startswith("*")
    assert SCOPE.account_id not in payload["rows"][0]["account_scope_id"].replace("RPT1", "")


def test_export_stdout(harness, capsys) -> None:
    rows = _cycle_rows(harness)
    content = export_csv(rows, "cycles", to_stdout=True)
    assert capsys.readouterr().out == content
    content_json = export_json(rows, "cycles", to_stdout=True)
    assert capsys.readouterr().out == content_json


def test_all_report_types_have_fixed_schemas(harness) -> None:
    rows = _cycle_rows(harness)
    cycles = rows
    assert set(REPORT_SCHEMAS) == {
        "cycles", "strategy summary", "pair summary", "symbol summary",
        "signals/evaluations", "rejections", "trailing updates",
        "eligibility decisions", "reconciliation drift", "comparison",
    }
    for report_type, schema in REPORT_SCHEMAS.items():
        assert schema["schema_version"] == "1"
        assert len(schema["columns"]) == len(set(schema["columns"]))
    strategy_rows = summarize_by_strategy(cycles, filt())["rows"]
    pair_rows = summarize_by_pair(cycles, filt())["rows"]
    symbol_rows = summarize_by_symbol(cycles, filt())["rows"]
    export_csv(strategy_rows, "strategy summary")
    export_csv(pair_rows, "pair summary")
    export_csv(symbol_rows, "symbol summary")
    export_csv([{"symbol": "AAPL", "status": "NO_SIGNAL", "count": 2}],
               "signals/evaluations")
    export_csv([{"reason": "ENTRY_CAP", "count": 1}], "rejections")
    export_csv([{"cycle_id": "c", "symbol": "AAPL", "update_count": 2,
                 "last_confirmed_stop": D("99")}], "trailing updates")
    export_csv([{"decision": "COOLDOWN:x", "symbol": "AAPL", "count": 1}],
               "eligibility decisions")
    export_csv([{"drift_id": "d", "drift_type": "CASH",
                 "entity_ref": "e", "detected_at": SELL_1}],
               "reconciliation drift")
    comparison_rows = [{
        "match": "matched", "symbol": "AAPL", "pair": "p",
        "signal_bar_time": BAR, "cycle_a": "a", "cycle_b": "b",
        "entry_price_a": D("100"), "entry_price_b": D("100.5"),
        "entry_price_diff": D("0.5"), "exit_price_a": D("105"),
        "exit_price_b": D("104"), "exit_price_diff": D("-1"),
        "net_a": D("50"), "net_b": D("35"), "net_diff": D("-15"),
    }]
    out = export_json(comparison_rows, "comparison",
                      context={"scope_a": SCOPE.scope_id, "scope_b": SCOPE_B.scope_id})
    payload = json.loads(out)
    assert payload["scope_a"].endswith("PT1") and SCOPE.scope_id not in out
    with pytest.raises(ValueError):
        export_csv(rows, "nope")
    with pytest.raises(ValueError):
        export_csv([{"cycle_id": 1, "gross_pnl": 46.0}], "cycles")  # float rejected
    naive = dict(rows[0].to_row(), opened_at=datetime(2026, 10, 8, 14, 31))
    with pytest.raises(ValueError):
        export_csv([naive], "cycles")  # naive datetime rejected


def test_reconciliation_drift_export(harness, tmp_path: Path) -> None:
    account, store, _ = harness
    recon = ReconciliationRepository(account.writer, account.clock)
    run_id = recon.start_run()
    recon.record_drift(run_id, "CASH", entity_ref="cash:usd")
    recon.complete_run(run_id, "DRIFTED")
    with store.snapshot(SCOPE.scope_id) as conn:
        db_rows = conn.execute(
            "SELECT drift_id, drift_type, entity_ref, detected_at FROM "
            "reconciliation_drifts WHERE account_scope_id = ? ORDER BY detected_at",
            (SCOPE.scope_id,),
        ).fetchall()
    rows = [{"drift_id": r[0], "drift_type": r[1], "entity_ref": r[2],
             "detected_at": from_db_timestamp(r[3])} for r in db_rows]
    target = tmp_path / "drift.json"
    content = export_json(rows, "reconciliation drift", target)
    assert target.read_text(encoding="utf-8") == content
    payload = json.loads(content)
    assert payload["rows"][0]["drift_type"] == "CASH"
    assert payload["rows"][0]["detected_at"].endswith("Z")


def test_every_report_payload_has_sample_size_and_disclaimer(harness) -> None:
    account, store, _ = harness
    account.round_trip("d1")
    cycles = build_trade_cycles(store, filt())
    for report in (summarize_by_strategy(cycles, filt()),
                   summarize_by_pair(cycles, filt()),
                   summarize_by_symbol(cycles, filt()),
                   count_evaluation_outcomes(store, filt())):
        assert report["sample_size"] >= 0
        assert report["performance_disclaimer"] == PERFORMANCE_DISCLAIMER
    text = export_json(cycles, "cycles")
    assert "profitable" not in text.lower()  # no profitability claims


def test_reporting_modules_do_not_touch_fees_or_accounting() -> None:
    import ast
    from pathlib import Path as _Path

    for name in ("metrics.py", "export.py"):
        tree = ast.parse((_Path(export_mod.__file__).parent / name).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not {m for m in imported if m.endswith(("reporting.fees",
                                                       "reporting.accounting"))}
