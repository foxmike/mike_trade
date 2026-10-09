"""Fee ingestion, fee attribution and equity reconciliation reports.

Everything runs on temporary migrated SQLite databases and fake broker activity
fixtures: no network, no credentials, no broker orders.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mike_trade.brokers.alpaca.activities import AlpacaActivityService
from mike_trade.brokers.alpaca.auth import AlpacaEnvironment
from mike_trade.brokers.shadow.account import ShadowAccount
from mike_trade.brokers.shadow.fills import FillAssumptions
from mike_trade.brokers.shadow.ledger import ShadowLedger
from mike_trade.domain.accounts import AccountEnvironment, AccountScope, AccountSnapshot
from mike_trade.domain.errors import ModeIsolationError
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
    CashEquityRepository,
    FillRepository,
    OrderIntentRepository,
    PositionCycleRepository,
)
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import ScopedWriter, WriterSession
from mike_trade.reporting import accounting, fees
from mike_trade.reporting.accounting import (
    DEFAULT_TOLERANCE,
    ReportDataError,
    ReportPeriod,
    build_account_ledger_report,
    quantize_cents,
    reconcile_equity,
    require_single_scope,
)
from mike_trade.reporting.fees import (
    ActivityCategory,
    FeeKind,
    FeeSchedule,
    FeeStatus,
    attribute_order_fees,
    ingest_broker_activities,
    load_ingested_activities,
    normalize_broker_activity,
)

D = Decimal
ZERO = D("0")
SCOPE = AccountScope(AccountEnvironment.PAPER, "ACCT1")
OTHER_SCOPE = AccountScope(AccountEnvironment.PAPER, "ACCT2")
BUDGET = BudgetPolicy(
    per_symbol_budget=D("5000"), total_application_budget=D("8000"), max_open_positions=5
)
SCHEDULE = FeeSchedule(name="test-schedule", per_share=D("0.005"))

PRIOR = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)  # Wed 16:00 ET
DEPOSIT_AT = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
BUY_1 = datetime(2026, 10, 8, 14, 31, tzinfo=UTC)
BUY_2 = datetime(2026, 10, 8, 14, 32, tzinfo=UTC)
SELL_1 = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
SELL_2 = datetime(2026, 10, 8, 15, 1, tzinfo=UTC)
FEE_AT = datetime(2026, 10, 8, 15, 5, tzinfo=UTC)
DIV_AT = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
CLOSE = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)  # Thu 16:00 ET
AS_OF = datetime(2026, 10, 8, 20, 5, tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeClock:
    def __init__(self) -> None:
        self.current = CLOSE

    def now(self) -> datetime:
        return self.current


# ------------------------------------------------------------------ PAPER fixture


class Account:
    """The write side for one PAPER account scope, built from existing repositories."""

    def __init__(self, session: WriterSession, scope: AccountScope, clock: FakeClock) -> None:
        self.scope = scope
        self.clock = clock
        self.writer: ScopedWriter = session.bind_scope(scope.scope_id)
        self.scopes = AccountScopeRepository(self.writer, clock)
        self.scopes.register_scope(scope)
        self.cycles = PositionCycleRepository(self.writer, clock)
        self.intents = OrderIntentRepository(self.writer, clock, BUDGET)
        self.orders = BrokerOrderRepository(self.writer, clock)
        self.fills = FillRepository(self.writer, clock)
        self.cash = CashEquityRepository(self.writer, clock)

    def count(self, table: str) -> int:
        row = self.writer.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE account_scope_id = ?", (self.scope.scope_id,)
        ).fetchone()
        return int(row[0])

    def snapshot(self, at: datetime, cash: str, equity: str, bp: str = "20000") -> None:
        self.cash.append_equity_snapshot(
            AccountSnapshot(
                scope=self.scope,
                as_of=at,
                cash=D(cash),
                equity=D(equity),
                buying_power=D(bp),
                source="alpaca:account",
            )
        )

    def _intent(
        self, intent_id: str, cycle_id: str, symbol: str, side: OrderSide, qty: str
    ) -> None:
        self.intents.record_intent(
            OrderIntent(
                intent_id=intent_id,
                client_order_id=f"client-{intent_id}",
                account_scope_id=self.scope.scope_id,
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                purpose=OrderPurpose.ENTRY if side is OrderSide.BUY else OrderPurpose.STRATEGY_EXIT,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                created_at=PRIOR,
                quantity=D(qty),
            ),
            D("1010") if side is OrderSide.BUY else ZERO,
        )

    def _order(self, order_id: str, intent_id: str, symbol: str, side: OrderSide, qty: str) -> None:
        self.orders.upsert_broker_order(
            BrokerOrder(
                broker_order_id=order_id,
                client_order_id=f"client-{intent_id}",
                account_scope_id=self.scope.scope_id,
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                status=BrokerOrderStatus.ACCEPTED,
                quantity=D(qty),
                filled_quantity=ZERO,
                submitted_at=PRIOR,
                updated_at=PRIOR + timedelta(seconds=1),
            )
        )

    def open_cycle(self, n: str, symbol: str, qty: str) -> tuple[str, str]:
        """Cycle, ENTRY intent and entry order; returns (cycle_id, entry order id)."""
        cycle_id = f"cycle-{n}"
        self.cycles.open_cycle(
            PositionCycle(cycle_id=cycle_id, account_scope_id=self.scope.scope_id, symbol=symbol)
        )
        self._intent(f"buy-{n}", cycle_id, symbol, OrderSide.BUY, qty)
        self._order(f"b-{n}", f"buy-{n}", symbol, OrderSide.BUY, qty)
        return cycle_id, f"b-{n}"

    def exit_order(self, n: str, cycle_id: str, symbol: str, qty: str) -> str:
        self._intent(f"sell-{n}", cycle_id, symbol, OrderSide.SELL, qty)
        self._order(f"s-{n}", f"sell-{n}", symbol, OrderSide.SELL, qty)
        return f"s-{n}"

    def fill(
        self, fill_id: str, order_id: str, side: OrderSide, qty: str, price: str,
        at: datetime, symbol: str = "AAPL", fee: str = "0",
    ) -> None:
        result = self.fills.apply_fill(
            Fill(
                fill_id=fill_id,
                broker_order_id=order_id,
                account_scope_id=self.scope.scope_id,
                symbol=symbol,
                side=side,
                quantity=D(qty),
                price=D(price),
                fee=D(fee),
                filled_at=at,
            )
        )
        assert result.applied

    def ingest(self, activities: Any) -> fees.IngestResult:
        return ingest_broker_activities(self.writer, self.clock, activities)


@dataclass
class Db:
    path: Path
    session: WriterSession
    clock: FakeClock

    def account(self, scope: AccountScope = SCOPE) -> Account:
        return Account(self.session, scope, self.clock)

    def read(self) -> ReadStore:
        return ReadStore.open(self.path)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Db]:
    path = tmp_path / "ops.db"
    initialize_database(path, AccountEnvironment.PAPER, now=PRIOR).close()
    session = WriterSession(path)
    yield Db(path, session, FakeClock())
    session.close()


def deposit(
    activity_id: str = "dep-1", amount: str = "500", at: datetime = DEPOSIT_AT
) -> dict[str, Any]:
    return {
        "id": activity_id,
        "activity_type": "CSD",
        "net_amount": amount,
        "transaction_time": iso(at),
        "account_id": "ACCT-0123456789",
        "description": "ACH deposit from account 000123456789",
    }


def withdrawal(
    activity_id: str = "wd-1", amount: str = "-200", at: datetime = DEPOSIT_AT
) -> dict[str, Any]:
    return {"id": activity_id, "activity_type": "CSW", "net_amount": amount,
            "transaction_time": iso(at)}


def dividend(activity_id: str = "div-1", amount: str = "2.00") -> dict[str, Any]:
    return {"id": activity_id, "activity_type": "DIV", "net_amount": amount,
            "symbol": "AAPL", "transaction_time": iso(DIV_AT)}


def taf(
    order_id: str | None = "s-1", amount: str = "-0.03", activity_id: str = "fee-1"
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": activity_id,
        "activity_type": "TAF",
        "net_amount": amount,
        "transaction_time": iso(FEE_AT),
    }
    if order_id is not None:
        item["order_id"] = order_id
    return item


def round_trip(account: Account, *, n: str = "1") -> tuple[str, str]:
    """Buy 10 AAPL (4@100, 6@101 = 1006), sell all (4@104, 6@106 = 1052): gross +46."""
    cycle_id, buy_order = account.open_cycle(n, "AAPL", "10")
    account.fill(f"fa-{n}1", buy_order, OrderSide.BUY, "4", "100", BUY_1)
    account.fill(f"fa-{n}2", buy_order, OrderSide.BUY, "6", "101", BUY_2)
    sell_order = account.exit_order(n, cycle_id, "AAPL", "10")
    account.fill(f"fa-{n}3", sell_order, OrderSide.SELL, "4", "104", SELL_1)
    account.fill(f"fa-{n}4", sell_order, OrderSide.SELL, "6", "106", SELL_2)
    return buy_order, sell_order


def consistent_world(db: Db) -> Account:
    """A day whose broker cash movements are fully explained.

    10000 + 500 deposit + 46 gross - 0.03 confirmed (sell) - 0.05 estimated (buys)
    + 2.00 dividend = 10547.92 (the position is flat, so unrealized is zero).
    """
    account = db.account()
    account.snapshot(PRIOR, "10000", "10000")
    round_trip(account)
    account.ingest([deposit(), taf("s-1"), dividend()])
    account.snapshot(CLOSE, "10547.92", "10547.92")
    return account


def report(
    db: Db, period: ReportPeriod = ReportPeriod.DAY, **kwargs: Any
) -> accounting.AccountLedgerReport:
    kwargs.setdefault("fee_schedule", SCHEDULE)
    with db.read() as store:
        return build_account_ledger_report(SCOPE, period, AS_OF, store=store, **kwargs)


# ------------------------------------------------------------- enum and normalization


def test_fee_status_values_are_exact() -> None:
    assert [(m.name, m.value) for m in FeeStatus] == [
        ("CONFIRMED", "CONFIRMED"),
        ("ESTIMATED", "ESTIMATED"),
        ("UNAVAILABLE", "UNAVAILABLE"),
    ]
    assert isinstance(FeeStatus.CONFIRMED, str)
    assert FeeStatus("ESTIMATED") is FeeStatus.ESTIMATED


@pytest.mark.parametrize(
    ("activity_type", "category", "fee_kind"),
    [
        ("FILL", ActivityCategory.TRADE_FILL, None),
        ("TAF", ActivityCategory.FEE, FeeKind.REGULATORY),
        ("SEC", ActivityCategory.FEE, FeeKind.REGULATORY),
        ("COMM", ActivityCategory.FEE, FeeKind.COMMISSION),
        ("FEE", ActivityCategory.FEE, FeeKind.OTHER),
        ("CSD", ActivityCategory.EXTERNAL_FLOW, None),
        ("CSW", ActivityCategory.EXTERNAL_FLOW, None),
        ("JNLC", ActivityCategory.EXTERNAL_FLOW, None),
        ("ACATC", ActivityCategory.EXTERNAL_FLOW, None),
        ("DIV", ActivityCategory.INCOME, None),
        ("DIVNRA", ActivityCategory.INCOME, None),
        ("INT", ActivityCategory.INCOME, None),
        ("SPLIT", ActivityCategory.CORPORATE_ACTION, None),
        ("MA", ActivityCategory.CORPORATE_ACTION, None),
        ("MISC", ActivityCategory.OTHER, None),
    ],
)
def test_activity_classification(
    activity_type: str, category: ActivityCategory, fee_kind: FeeKind | None
) -> None:
    record = normalize_broker_activity(
        {"id": "a1", "activity_type": activity_type, "net_amount": "-1",
         "transaction_time": iso(FEE_AT)}
    )
    assert record.category is category
    assert record.fee_kind is fee_kind


def test_normalization_keeps_only_the_allowlist_and_drops_secrets() -> None:
    raw = {
        "id": "20261008::abc-1",
        "activity_type": "TAF",
        "activity_sub_type": "taf_sell",
        "order_id": "s-1",
        "client_order_id": "client-sell-1",
        "symbol": "aapl",
        "qty": "10",
        "price": "104.25",
        "net_amount": "-0.03",
        "transaction_time": "2026-10-08T15:05:00-04:00",
        "description": "FINRA TAF for account 000123456789",
        "account_id": "ACCT-0123456789",
        "api_key": "PKSECRETKEY123",
        "authorization": "Bearer top-secret-token",
        "extra": {"nested": "secret-nested-value"},
    }
    record = normalize_broker_activity(raw)

    assert {f.name for f in dataclasses.fields(record)} == {
        "activity_id", "activity_type", "category", "occurred_at", "fee_kind", "order_id",
        "client_order_id", "symbol", "qty", "price", "net_amount", "fee_amount",
        "description_category",
    }
    assert record.symbol == "AAPL"
    assert record.qty == D("10") and record.price == D("104.25")
    assert record.net_amount == D("-0.03") and record.fee_amount == D("0.03")
    assert record.occurred_at == datetime(2026, 10, 8, 19, 5, tzinfo=UTC)
    assert record.occurred_at.utcoffset() == timedelta(0)
    assert record.description_category == "TAF_SELL"
    blob = repr(record) + json.dumps(record.to_payload(), default=str)
    for leaked in ("PKSECRET", "top-secret", "secret-nested", "0123456789", "000123456789"):
        assert leaked not in blob


def test_normalization_rejects_floats_and_drops_unsafe_identifiers() -> None:
    with pytest.raises(ValueError):
        normalize_broker_activity(
            {"id": "a1", "activity_type": "FEE", "net_amount": 0.03,
             "transaction_time": iso(FEE_AT)}
        )
    with pytest.raises(ValueError):
        normalize_broker_activity({"id": "a1", "activity_type": "FEE", "net_amount": "-1"})
    record = normalize_broker_activity(
        {"id": "a1", "activity_type": "FEE", "net_amount": "-1", "transaction_time": iso(FEE_AT),
         "order_id": "key=PKSECRET value with spaces", "description": "free text"}
    )
    assert record.order_id is None and record.description_category is None


def test_alpaca_adapter_activities_can_be_ingested_without_their_raw_payload(db: Db) -> None:
    class FakeClient:
        def get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            assert path == "/v2/account/activities"
            return [
                {"id": "f1", "activity_type": "FILL", "type": "fill", "order_id": "b-1",
                 "symbol": "AAPL", "side": "buy", "qty": "4", "cum_qty": "4", "leaves_qty": "0",
                 "price": "100", "transaction_time": iso(BUY_1), "api_key": "PKSECRETKEY123"},
                {"id": "fee-9", "activity_type": "FEE", "net_amount": "-0.02",
                 "order_id": "b-1", "date": "2026-10-08", "description": "acct 000123456789"},
            ]

    service = AlpacaActivityService(
        FakeClient(), environment=AlpacaEnvironment.PAPER, fingerprint="fp-test"
    )
    account = db.account()
    result = account.ingest(service.list_activities())

    assert [r.category for r in result.new_records] == [
        ActivityCategory.TRADE_FILL, ActivityCategory.FEE
    ]
    stored = account.writer.connection.execute(
        "SELECT payload_json FROM broker_activities WHERE account_scope_id = ?",
        (SCOPE.scope_id,),
    ).fetchall()
    assert len(stored) == 2
    assert "PKSECRET" not in "".join(row[0] for row in stored)
    assert "000123456789" not in "".join(row[0] for row in stored)


# ------------------------------------------------------------------ ingestion


def test_reingesting_the_same_activities_adds_no_record_or_event(db: Db) -> None:
    account = db.account()
    batch = [deposit(), taf(), dividend(), {"id": "x1", "activity_type": "MISC",
                                             "net_amount": "1", "transaction_time": iso(FEE_AT)}]

    first = account.ingest(batch)
    tables = ("broker_activities", "cash_events", "domain_events")
    before = {t: account.count(t) for t in tables}
    second = account.ingest(batch)

    assert len(first.new_records) == 4 and first.cash_events_added == 2
    assert second.new_records == () and second.duplicates == 4 and second.cash_events_added == 0
    assert {t: account.count(t) for t in tables} == before
    assert before["broker_activities"] == 4 and before["cash_events"] == 2


def test_duplicate_ids_inside_one_batch_are_stored_once(db: Db) -> None:
    account = db.account()
    result = account.ingest([deposit(), deposit()])
    assert len(result.new_records) == 1 and result.duplicates == 1
    assert account.count("broker_activities") == 1 and account.count("cash_events") == 1


def test_ingest_stores_only_filtered_payloads(db: Db) -> None:
    account = db.account()
    leaky = dict(deposit(), api_key="PKSECRETKEY123", authorization="Bearer top-secret-token")
    account.ingest([leaky])

    rows = account.writer.connection.execute(
        "SELECT activity_type, payload_json FROM broker_activities"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert set(payload) <= {
        "activity_id", "activity_type", "category", "fee_kind", "order_id", "client_order_id",
        "symbol", "qty", "price", "net_amount", "fee_amount", "occurred_at",
        "description_category",
    }
    text = json.dumps(payload) + json.dumps(
        [list(r) for r in account.writer.connection.execute("SELECT * FROM cash_events")],
        default=str,
    )
    for leaked in ("PKSECRET", "top-secret", "ACCT-0123456789", "000123456789"):
        assert leaked not in text


def test_ingest_without_a_source_does_nothing_and_malformed_items_are_reported(db: Db) -> None:
    account = db.account()
    assert account.ingest(None) == fees.IngestResult()
    result = account.ingest([{"id": "bad-1", "activity_type": "FEE"}, "not-a-mapping"])
    assert result.new_records == () and result.rejected == ("bad-1", "<unidentified>")
    assert account.count("broker_activities") == 0


def test_stored_activities_round_trip_through_the_reader(db: Db) -> None:
    account = db.account()
    account.ingest([taf("s-1"), deposit()])
    with db.read() as store, store.snapshot(SCOPE.scope_id) as conn:
        loaded = load_ingested_activities(conn, SCOPE.scope_id)
    assert {r.activity_id for r in loaded} == {"fee-1", "dep-1"}
    fee_record = next(r for r in loaded if r.activity_id == "fee-1")
    assert fee_record == normalize_broker_activity(taf("s-1"))


# ----------------------------------------------------------------- fee attribution


def make_fill(order_id: str, qty: str, price: str, *, side: OrderSide = OrderSide.BUY,
              fee: str = "0", scope: AccountScope = SCOPE, fill_id: str | None = None) -> Fill:
    return Fill(
        fill_id=fill_id or f"{order_id}-{qty}-{price}", broker_order_id=order_id,
        account_scope_id=scope.scope_id, symbol="AAPL", side=side, quantity=D(qty),
        price=D(price), fee=D(fee), filled_at=BUY_1,
    )


def test_fee_is_confirmed_only_when_an_activity_links_it_to_the_order() -> None:
    fills = [make_fill("b-1", "4", "100"), make_fill("s-1", "4", "104", side=OrderSide.SELL)]
    records = [normalize_broker_activity(taf("s-1", "-0.03")),
               normalize_broker_activity(taf(None, "-9.99", "fee-account"))]

    result = {f.broker_order_id: f for f in attribute_order_fees(fills, records)}

    assert result["s-1"].status is FeeStatus.CONFIRMED
    assert result["s-1"].amount == D("0.03") and result["s-1"].activity_ids == ("fee-1",)
    # The unlinked account-level fee never leaks onto an order.
    assert result["b-1"].status is FeeStatus.UNAVAILABLE and result["b-1"].amount is None


def test_unlinked_orders_are_estimated_from_the_schedule_and_labeled_so() -> None:
    fills = [make_fill("b-1", "4", "100"), make_fill("b-1", "6", "101", fill_id="x2")]
    (fee,) = attribute_order_fees(fills, [], schedule=SCHEDULE)

    assert fee.status is FeeStatus.ESTIMATED
    assert fee.amount == D("0.02") + D("0.03")  # per fill, rounded up to the cent
    assert fee.source == "fee_schedule:test-schedule" and fee.activity_ids == ()


def test_fee_is_unavailable_without_a_schedule_or_activity_source() -> None:
    (fee,) = attribute_order_fees([make_fill("b-1", "4", "100")])
    assert (fee.status, fee.amount) == (FeeStatus.UNAVAILABLE, None)
    with pytest.raises(ValueError):
        fees.OrderFee("b-1", FeeStatus.UNAVAILABLE, D("0"), "x")
    with pytest.raises(ValueError):
        fees.OrderFee("b-1", FeeStatus.CONFIRMED, D("1"), "x")  # no cited activity


def test_client_order_id_links_through_the_supplied_mapping() -> None:
    record = normalize_broker_activity(
        {"id": "fee-c", "activity_type": "FEE", "net_amount": "-0.10",
         "client_order_id": "client-sell-1", "transaction_time": iso(FEE_AT)}
    )
    (fee,) = attribute_order_fees(
        [make_fill("s-1", "4", "104", side=OrderSide.SELL)], [record],
        client_order_ids={"client-sell-1": "s-1"},
    )
    assert fee.status is FeeStatus.CONFIRMED and fee.amount == D("0.10")


def test_shadow_fees_come_only_from_the_simulated_model_and_are_estimated() -> None:
    fills = [make_fill("b-1", "4", "100", fee="0.02", scope=AccountScope(
        AccountEnvironment.SHADOW, "ACCT", "scn"))]
    confirming = normalize_broker_activity(taf("b-1", "-5.00"))

    (fee,) = attribute_order_fees(
        fills, [confirming], schedule=SCHEDULE, environment=AccountEnvironment.SHADOW
    )

    assert fee.status is FeeStatus.ESTIMATED and fee.amount == D("0.02")
    assert fee.source == "shadow_fee_model"
    with pytest.raises(ModeIsolationError):
        attribute_order_fees(fills, [], environment=AccountEnvironment.BACKTEST)


def test_fee_schedule_matches_the_shadow_simulator_model() -> None:
    assumptions = FillAssumptions(fee_per_fill=D("0.10"), fee_per_share=D("0.004"), fee_bps=D("2"))
    schedule = FeeSchedule.from_assumptions("scn", assumptions)
    # 0.10 + 0.004*7 + 7*50.5*2/10000 = 0.1 + 0.028 + 0.0707 = 0.1987 -> 0.20
    assert schedule.estimate(D("7"), D("50.5")) == D("0.20")


# ---------------------------------------------------------------- the report


def test_consistent_fixture_reconciles_to_a_zero_residual(db: Db) -> None:
    consistent_world(db)
    r = report(db)

    assert r.account_scope_id == SCOPE.scope_id and r.period is ReportPeriod.DAY
    assert r.starting_equity == D("10000") and r.ending_equity == D("10547.92")
    assert r.starting_cash == D("10000") and r.ending_cash == D("10547.92")
    assert r.expected_ending_equity == D("10547.92")
    assert r.residual == ZERO and r.reconciled and not r.drift
    assert r.change_in_unrealized_pnl == ZERO and r.unrealized_pnl == ZERO
    assert r.session_start_date.isoformat() == "2026-10-07"
    assert r.session_end_date.isoformat() == "2026-10-08"
    assert "equity_snapshots" in r.valuation_convention
    assert "ROUND_HALF_EVEN" in r.rounding_convention


def test_deposits_and_withdrawals_are_excluded_from_trading_pnl(db: Db) -> None:
    account = db.account()
    account.snapshot(PRIOR, "10000", "10000")
    account.ingest([deposit(amount="500"), withdrawal(amount="-200")])
    account.snapshot(CLOSE, "10300", "10300")

    r = report(db)

    assert r.external_deposits == D("500") and r.external_withdrawals == D("-200")
    assert r.external_flows == D("300")
    assert r.realized_gross_pnl == ZERO and r.net_realized_pnl == ZERO
    assert r.income == ZERO and r.corporate_adjustments == ZERO
    assert r.reconciled and r.residual == ZERO


def test_flows_do_not_change_trading_pnl(db: Db) -> None:
    consistent_world(db)
    with_flows = report(db)
    assert with_flows.external_flows == D("500") and with_flows.income == D("2.00")
    assert with_flows.net_realized_pnl == D("46") - D("0.03") - D("0.05")


def test_partial_fills_give_the_correct_realized_pnl(db: Db) -> None:
    account = db.account()
    account.snapshot(PRIOR, "10000", "10000")
    cycle_id, buy_order = account.open_cycle("1", "AAPL", "10")
    account.fill("fa-1", buy_order, OrderSide.BUY, "4", "100", BUY_1)
    account.fill("fa-2", buy_order, OrderSide.BUY, "6", "101", BUY_2)
    sell_order = account.exit_order("1", cycle_id, "AAPL", "10")
    account.fill("fa-3", sell_order, OrderSide.SELL, "4", "104", SELL_1)
    # Mid-exit: 6 shares remain at cost 606; cash = 10000 - 1006 + 416.
    account.snapshot(SELL_1 + timedelta(minutes=1), "9410", "10016")
    # Average cost 100.6: realized on 4 shares = 416 - 402.4; 6 shares remain at 603.6.
    mid = report(db, fee_schedule=None, tolerance=D("0"))
    assert mid.realized_gross_pnl == D("13.6")
    assert mid.unrealized_pnl == D("10016") - D("9410") - D("603.6") == D("2.4")
    assert mid.fees_complete is False and mid.reconciled

    account.fill("fa-4", sell_order, OrderSide.SELL, "6", "106", SELL_2)
    account.snapshot(CLOSE, "10052", "10052")
    done = report(db, fee_schedule=None)
    assert done.realized_gross_pnl == D("46")  # 1052 - 1006, exact across the exit fills


def test_estimated_fees_are_labeled_and_never_reported_as_confirmed(db: Db) -> None:
    consistent_world(db)
    r = report(db)
    assert r.confirmed_fees == D("0.03") and r.unattributed_confirmed_fees == ZERO
    assert r.estimated_fees == D("0.05")
    assert r.fees_complete and r.unavailable_fee_order_ids == ()
    assert r.net_realized_pnl == D("45.92")


def test_unavailable_fees_flag_net_pnl_incomplete(db: Db) -> None:
    consistent_world(db)
    r = report(db, fee_schedule=None)  # the buy order has neither activity nor schedule

    assert r.fees_complete is False
    assert r.unavailable_fee_order_ids == ("b-1",)
    assert r.estimated_fees == ZERO and r.confirmed_fees == D("0.03")
    assert r.net_realized_pnl == D("46") - D("0.03")  # unavailable fee is not invented
    assert r.residual == D("-0.05")  # the cash the broker never charged for it is surfaced
    assert r.reconciled is False and r.drift


def test_unlinked_broker_fee_is_confirmed_but_reported_as_unattributed(db: Db) -> None:
    account = db.account()
    account.snapshot(PRIOR, "10000", "10000")
    account.ingest([taf(None, "-0.40", "fee-acct")])
    account.snapshot(CLOSE, "9999.60", "9999.60")

    r = report(db)
    assert r.confirmed_fees == D("0.40") and r.unattributed_confirmed_fees == D("0.40")
    assert r.net_realized_pnl == D("-0.40") and r.fees_complete and r.reconciled


def test_inconsistent_fixture_reports_drift_instead_of_hiding_it(db: Db) -> None:
    account = consistent_world(db)
    account.snapshot(CLOSE + timedelta(minutes=1), "10542.92", "10542.92")  # $5 unexplained

    r = report(db)

    assert r.ending_equity == D("10542.92")
    assert r.residual == D("-5.00")
    assert r.reconciled is False and r.drift
    assert r.presented()["drift"] is True and r.presented()["residual"] == D("-5.00")


def test_drift_below_a_cent_is_still_drift_with_a_tight_tolerance(db: Db) -> None:
    account = consistent_world(db)
    account.snapshot(CLOSE + timedelta(minutes=1), "10547.924", "10547.924")

    loose = report(db)
    tight = report(db, tolerance=D("0.001"))

    assert loose.residual == tight.residual == D("0.004")
    assert loose.reconciled and not tight.reconciled
    assert quantize_cents(tight.residual) == D("0.00")  # presentation rounds; the flag does not


def test_unrealized_pnl_uses_the_snapshot_mark_and_flags_staleness(db: Db) -> None:
    account = db.account()
    account.snapshot(PRIOR, "10000", "10000")
    _, buy_order = account.open_cycle("1", "AAPL", "10")
    account.fill("fa-1", buy_order, OrderSide.BUY, "10", "100", BUY_1)
    # Cash 10000 - 1000 - 0.05 estimated fee; position marked at 103.
    account.snapshot(CLOSE, "8999.95", "10029.95")

    r = report(db)

    assert r.unrealized_pnl == D("30") and r.change_in_unrealized_pnl == D("30")
    assert r.net_realized_pnl == D("-0.05") and r.reconciled
    assert r.mark_source == "equity_snapshots:alpaca:account" and r.mark_as_of == CLOSE
    assert r.mark_stale is False  # 5 minutes old at AS_OF
    with db.read() as store:
        late = build_account_ledger_report(
            SCOPE, ReportPeriod.DAY, CLOSE + timedelta(hours=2), store=store,
            fee_schedule=SCHEDULE,
        )
    assert late.mark_stale is True


def test_budget_buying_power_and_commitment_are_separate_fields(db: Db) -> None:
    account = db.account()
    account.snapshot(PRIOR, "10000", "10000", bp="25000")
    _, buy_order = account.open_cycle("1", "AAPL", "10")
    account.fill("fa-1", buy_order, OrderSide.BUY, "4", "100", BUY_1)  # 400 deployed, 606 reserved
    account.snapshot(CLOSE, "9599.98", "9599.98", bp="24000")

    r = report(db, budget=BUDGET)

    assert r.account_buying_power == D("24000")
    assert r.authorized_total_budget == D("8000") and r.authorized_per_symbol_budget == D("5000")
    (commitment,) = r.symbol_commitments
    assert commitment.symbol == "AAPL"
    assert commitment.deployed_cost_basis == D("400")
    assert commitment.reserved_pending_buys == D("606")
    assert commitment.commitment == D("1006") == r.total_commitment
    assert r.account_buying_power != r.authorized_total_budget != r.total_commitment


def test_day_week_and_lifetime_windows_use_new_york_session_boundaries(db: Db) -> None:
    account = db.account()
    monday = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    tuesday_deposit = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
    account.snapshot(monday, "9000", "9000")
    account.ingest([dict(deposit("dep-wk", "1000", tuesday_deposit))])
    account.snapshot(PRIOR, "10000", "10000")
    account.ingest([deposit()])
    account.snapshot(CLOSE, "10500", "10500")

    day = report(db, ReportPeriod.DAY)
    week = report(db, ReportPeriod.WEEK)
    life = report(db, ReportPeriod.LIFETIME)

    assert day.window_start == PRIOR and day.starting_equity == D("10000")
    assert day.external_flows == D("500") and day.reconciled
    # The week began Monday 00:00 ET; the earliest snapshot is the baseline.
    assert week.window_start == monday and week.starting_equity == D("9000")
    assert week.external_flows == D("1500") and week.reconciled
    assert (life.window_start, life.starting_equity, life.external_flows) == (
        monday, D("9000"), D("1500")
    )
    assert accounting._period_start(ReportPeriod.WEEK, AS_OF) == datetime(
        2026, 10, 5, 4, 0, tzinfo=UTC
    )
    assert accounting._period_start(ReportPeriod.DAY, AS_OF) == datetime(
        2026, 10, 8, 4, 0, tzinfo=UTC
    )


def test_report_without_a_snapshot_cannot_be_built(db: Db) -> None:
    db.account()
    with pytest.raises(ReportDataError):
        report(db)


# ------------------------------------------------------------------- rounding


def test_presentation_rounds_half_even_but_internal_values_are_unrounded(db: Db) -> None:
    assert quantize_cents(D("0.125")) == D("0.12")
    assert quantize_cents(D("0.135")) == D("0.14")
    assert quantize_cents(D("0.005")) == D("0.00")
    assert quantize_cents(D("0.015")) == D("0.02")
    assert quantize_cents(D("-0.025")) == D("-0.02")
    assert accounting.PRESENTATION_ROUNDING == "ROUND_HALF_EVEN"

    consistent_world(db)
    r = dataclasses.replace(report(db), income=D("0.125"), residual=D("0.0049"))
    shown = r.presented()
    assert r.income == D("0.125") and shown["income"] == D("0.12")
    assert r.residual == D("0.0049") and shown["residual"] == D("0.00")
    assert shown["starting_equity"] == D("10000.00")
    assert all(not isinstance(v, float) for v in shown.values())


def test_reconcile_equity_identity_and_residual_precision() -> None:
    exact = reconcile_equity(
        starting_equity=D("1000"), external_flows=D("100"), income=D("1.5"),
        corporate_adjustments=D("0.5"), net_realized_pnl=D("20.25"),
        change_in_unrealized_pnl=D("-2.25"), ending_equity=D("1120"),
    )
    assert exact.expected_ending_equity == D("1120") and exact.residual == ZERO and exact.reconciled

    tiny = reconcile_equity(
        starting_equity=D("1000"), external_flows=ZERO, income=ZERO, corporate_adjustments=ZERO,
        net_realized_pnl=ZERO, change_in_unrealized_pnl=ZERO,
        ending_equity=D("1000.0000000000000000000001"), tolerance=ZERO,
    )
    assert tiny.residual == D("0.0000000000000000000001") and tiny.drift
    assert DEFAULT_TOLERANCE >= ZERO
    with pytest.raises(ValueError):
        reconcile_equity(
            starting_equity=1000.0, external_flows=ZERO, income=ZERO,  # type: ignore[arg-type]
            corporate_adjustments=ZERO, net_realized_pnl=ZERO, change_in_unrealized_pnl=ZERO,
            ending_equity=D("1000"),
        )


# ---------------------------------------------------------------- isolation


def test_accounts_in_one_database_are_reported_separately(db: Db) -> None:
    first = consistent_world(db)
    second = db.account(OTHER_SCOPE)
    second.snapshot(PRIOR, "5000", "5000")
    second.ingest([deposit("dep-other", "50")])
    second.snapshot(CLOSE, "5050", "5050")

    mine = report(db)
    with db.read() as store:
        other = build_account_ledger_report(
            OTHER_SCOPE, ReportPeriod.DAY, AS_OF, store=store, fee_schedule=SCHEDULE
        )

    assert mine.account_scope_id == SCOPE.scope_id
    assert other.account_scope_id == OTHER_SCOPE.scope_id
    assert (mine.starting_equity, mine.external_flows) == (D("10000"), D("500"))
    assert (other.starting_equity, other.external_flows) == (D("5000"), D("50"))
    assert other.realized_gross_pnl == ZERO and other.income == ZERO and other.reconciled
    assert mine.reconciled
    assert first.count("broker_activities") == 3 and second.count("broker_activities") == 1


def test_requests_that_mix_accounts_or_modes_are_rejected() -> None:
    shadow = AccountScope(AccountEnvironment.SHADOW, "ACCT", "scn-a")
    assert require_single_scope(SCOPE) == SCOPE
    assert require_single_scope([SCOPE.scope_id, SCOPE]) == SCOPE
    assert require_single_scope("shadow:ACCT:scn-a") == shadow

    with pytest.raises(ModeIsolationError) as accounts:
        require_single_scope([SCOPE, OTHER_SCOPE])
    assert accounts.value.code == "report_mixes_accounts"
    with pytest.raises(ModeIsolationError) as modes:
        require_single_scope([SCOPE, shadow])
    assert modes.value.code == "report_mixes_modes"
    with pytest.raises(ModeIsolationError):
        require_single_scope([shadow, AccountScope(AccountEnvironment.SHADOW, "ACCT", "scn-b")])
    with pytest.raises(ModeIsolationError):
        require_single_scope([])


def test_builder_refuses_mixed_requests_and_mismatched_sources(db: Db) -> None:
    consistent_world(db)
    shadow = AccountScope(AccountEnvironment.SHADOW, "ACCT", "scn-a")
    with db.read() as store:
        with pytest.raises(ModeIsolationError):
            build_account_ledger_report(
                [SCOPE, OTHER_SCOPE], ReportPeriod.DAY, AS_OF, store=store
            )
        with pytest.raises(ModeIsolationError):  # a SHADOW scope never reads a broker store
            build_account_ledger_report(shadow, ReportPeriod.DAY, AS_OF, store=store)
        live = AccountScope(AccountEnvironment.LIVE, "ACCT1")  # PAPER database, LIVE scope
        with pytest.raises(ModeIsolationError):
            build_account_ledger_report(live, ReportPeriod.DAY, AS_OF, store=store)
        with pytest.raises(ModeIsolationError):  # no store at all for a PAPER scope
            build_account_ledger_report(SCOPE, ReportPeriod.DAY, AS_OF)


def test_ingestion_is_refused_for_shadow_databases(tmp_path: Path) -> None:
    path = tmp_path / "shadow.db"
    initialize_database(path, AccountEnvironment.SHADOW, now=PRIOR).close()
    with WriterSession(path) as session:
        writer = session.bind_scope("shadow:ACCT:scn-a")
        with pytest.raises(ModeIsolationError):
            ingest_broker_activities(writer, FakeClock(), [deposit()])


# ------------------------------------------------------------------- SHADOW


class ShadowHarness:
    def __init__(self, tmp_path: Path, scenario: str = "scn-a") -> None:
        self.path = tmp_path / "shadow.db"
        initialize_database(self.path, AccountEnvironment.SHADOW, now=PRIOR).close()
        self.session = WriterSession(self.path)
        self.scope = AccountScope(AccountEnvironment.SHADOW, "ACCT", scenario)
        self.ledger = ShadowLedger(
            self.session.bind_scope(self.scope.scope_id), self.scope, clock=FakeClock()
        )
        self.account = ShadowAccount.create(
            self.ledger,
            starting_cash=D("10000"),
            per_symbol_budget=D("2000"),
            total_budget=D("3000"),
            eligibility=EntryEligibilityPolicy(max_filled_entry_cycles_per_symbol_per_session=3),
            at=PRIOR,
        )

    def trade(
        self, client_id: str, side: OrderSide, qty: str, price: str, fee: str, at: datetime
    ) -> None:
        intent = OrderIntent(
            intent_id=f"i-{client_id}",
            client_order_id=client_id,
            account_scope_id=self.scope.scope_id,
            cycle_id="cycle-AAPL",
            symbol="AAPL",
            side=side,
            purpose=OrderPurpose.ENTRY if side is OrderSide.BUY else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            created_at=at,
            quantity=D(qty),
            limit_price=D(price),
        )
        order = asyncio.run(self.account.submit_order(intent))
        self.account.apply_fill(
            Fill(
                fill_id=f"f-{client_id}", broker_order_id=order.broker_order_id,
                account_scope_id=self.scope.scope_id, symbol="AAPL", side=side,
                quantity=D(qty), price=D(price), fee=D(fee), filled_at=at,
            )
        )

    def report(self, **kwargs: Any) -> accounting.AccountLedgerReport:
        return build_account_ledger_report(
            self.scope, ReportPeriod.DAY, AS_OF, shadow_ledger=self.ledger, **kwargs
        )


@pytest.fixture
def shadow(tmp_path: Path) -> Iterator[ShadowHarness]:
    harness = ShadowHarness(tmp_path)
    yield harness
    harness.session.close()


def test_shadow_report_uses_simulated_fees_labeled_estimated(shadow: ShadowHarness) -> None:
    schedule = FeeSchedule.from_assumptions("scn-a", FillAssumptions(fee_per_share=D("0.005")))
    fee = schedule.estimate(D("10"), D("100"))
    assert fee == D("0.05")
    shadow.account.adjust_cash(
        adjustment_id="topup", amount=D("200"), reason="top up", actor="operator", at=DEPOSIT_AT
    )
    shadow.trade("b1", OrderSide.BUY, "10", "100", str(fee), BUY_1)
    shadow.trade("s1", OrderSide.SELL, "10", "105", str(fee), SELL_1)

    r = shadow.report()

    assert r.environment == "SHADOW" and r.estimated is True and r.scenario == "scn-a"
    assert r.realized_gross_pnl == D("50")
    assert r.estimated_fees == D("0.10") and r.confirmed_fees == ZERO
    assert r.net_realized_pnl == D("49.90") and r.fees_complete
    assert r.external_flows == D("200") and r.income == ZERO
    assert (r.starting_equity, r.ending_equity) == (D("10000"), D("10249.90"))
    assert r.residual == ZERO and r.reconciled
    assert r.authorized_total_budget == D("3000") and r.authorized_per_symbol_budget == D("2000")


def test_shadow_open_position_is_marked_or_flagged_as_valued_at_cost(shadow: ShadowHarness) -> None:
    shadow.trade("b1", OrderSide.BUY, "10", "100", "0.05", BUY_1)

    unmarked = shadow.report()
    marked = shadow.report(marks={"AAPL": D("103")})

    assert unmarked.unrealized_pnl == ZERO and unmarked.mark_stale is True
    assert unmarked.mark_source == "shadow:cost_basis" and unmarked.reconciled
    assert marked.unrealized_pnl == D("30") and marked.mark_stale is False
    assert marked.mark_source == "shadow:quotes" and marked.reconciled
    assert marked.ending_equity == D("10029.95") and marked.net_realized_pnl == D("-0.05")
    (commitment,) = marked.symbol_commitments
    assert commitment.deployed_cost_basis == D("1000") and commitment.reserved_pending_buys == ZERO


def test_shadow_scenarios_are_reported_apart_and_refuse_foreign_ledgers(
    shadow: ShadowHarness,
) -> None:
    shadow.trade("b1", OrderSide.BUY, "10", "100", "0.05", BUY_1)
    other_scope = AccountScope(AccountEnvironment.SHADOW, "ACCT", "scn-b")
    other_ledger = ShadowLedger(
        shadow.session.bind_scope(other_scope.scope_id), other_scope, clock=FakeClock()
    )
    ShadowAccount.create(
        other_ledger, starting_cash=D("777"), per_symbol_budget=D("100"), total_budget=D("200"),
        eligibility=EntryEligibilityPolicy(max_filled_entry_cycles_per_symbol_per_session=3),
        at=PRIOR,
    )

    mine = shadow.report()
    other = build_account_ledger_report(
        other_scope, ReportPeriod.DAY, AS_OF, shadow_ledger=other_ledger
    )

    assert mine.scenario == "scn-a" and other.scenario == "scn-b"
    assert other.starting_equity == D("777") and other.realized_gross_pnl == ZERO
    assert mine.starting_equity == D("10000")
    with pytest.raises(ModeIsolationError):
        build_account_ledger_report(
            other_scope, ReportPeriod.DAY, AS_OF, shadow_ledger=shadow.ledger
        )
    with ReadStore.open(shadow.path) as store, pytest.raises(ModeIsolationError):
        build_account_ledger_report(shadow.scope, ReportPeriod.DAY, AS_OF, store=store)


# ------------------------------------------------------- no manual fee-file path


def test_there_is_no_manual_fee_file_loader() -> None:
    suspicious = re.compile(r"(csv|file|path|import|upload|manual)", re.IGNORECASE)
    for module in (fees, accounting):
        public = [
            name for name, member in vars(module).items()
            if not name.startswith("_") and getattr(member, "__module__", None) == module.__name__
        ]
        assert public
        assert [name for name in public if suspicious.search(name)] == []
        used: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Import):
                used.update(part for alias in node.names for part in alias.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                used.update((node.module or "").split("."))
        assert not used & {
            "csv", "pathlib", "Path", "open", "openpyxl", "pandas", "read_text", "read_bytes",
            "io", "os",
        }
    for func in (
        fees.ingest_broker_activities, fees.attribute_order_fees, fees.normalize_broker_activity,
        accounting.build_account_ledger_report,
    ):
        assert not any(
            re.search(r"(file|path|csv)", p, re.IGNORECASE)
            for p in inspect.signature(func).parameters
        )


def test_money_is_never_float() -> None:
    for cls in (fees.BrokerActivityRecord, fees.OrderFee, accounting.AccountLedgerReport,
                accounting.SymbolCommitment, accounting.EquityReconciliation):
        for field in dataclasses.fields(cls):
            assert "float" not in str(field.type)
