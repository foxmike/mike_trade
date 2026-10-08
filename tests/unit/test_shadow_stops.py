from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.brokers.shadow.account import ShadowAccount
from mike_trade.brokers.shadow.ledger import ShadowLedger
from mike_trade.brokers.shadow.stops import (
    ShadowStopSimulator,
    StopTriggerAssumptions,
    StopTriggerOutcome,
)
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.domain.trading import (
    EntryEligibilityPolicy,
    Fill,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.market_data.context import QuoteSnapshot, TradeSnapshot
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
SCOPE = AccountScope(AccountEnvironment.SHADOW, "STOP-TEST", "scenario")


class Clock:
    def now(self):
        return NOW


def run(coro):
    return asyncio.run(coro)


class Harness:
    def __init__(self, path):
        initialize_database(path, AccountEnvironment.SHADOW, now=NOW).close()
        self.session = WriterSession(path)
        self.ledger = ShadowLedger(self.session.bind_scope(SCOPE.scope_id), SCOPE, clock=Clock())
        self.account = ShadowAccount.create(
            self.ledger,
            starting_cash=D("10000"),
            per_symbol_budget=D("5000"),
            total_budget=D("8000"),
            eligibility=EntryEligibilityPolicy(max_filled_entry_cycles_per_symbol_per_session=3),
        )
        self.account.update_quote("ABC", D("100"))

    def buy(self, quantity=D("10")):
        intent = OrderIntent(
            intent_id="entry-1", client_order_id="entry-client-1", account_scope_id=SCOPE.scope_id,
            cycle_id="cycle-1", symbol="ABC", side=OrderSide.BUY, purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY, created_at=NOW,
            quantity=quantity,
        )
        order = run(self.account.submit_order(intent))
        self.account.apply_fill(Fill("entry-fill-1", order.broker_order_id, SCOPE.scope_id,
                                     "ABC", OrderSide.BUY, quantity, D("100"), D("0"), NOW))

    def quote(self, bid, sec=0):
        timestamp = NOW + timedelta(seconds=sec)
        return QuoteSnapshot("ABC", D(str(bid)), D(str(bid)) + D("0.01"), D("100"), D("100"),
                             timestamp, timestamp, "sip")


def fixed():
    return ProtectionPolicy(ProtectionKind.FIXED_STOP, stop_offset_amount=D("5"))


def test_fixed_stop_triggers_at_bid_minus_slippage_and_records_provenance(tmp_path):
    h = Harness(tmp_path / "fixed.db")
    h.buy()
    simulator = ShadowStopSimulator(h.account, StopTriggerAssumptions(stop_slippage=D("0.25")))
    armed = run(simulator.arm("cycle-1", "ABC", fixed(), at=NOW))
    result = run(simulator.evaluate("cycle-1", h.quote("94.50", 1), at=NOW + timedelta(seconds=1)))
    assert armed.stop_price == D("95")
    assert result.outcome is StopTriggerOutcome.TRIGGERED
    fill = next(fill for fill in h.account.state.fills.values() if fill.side is OrderSide.SELL)
    assert fill.quantity == D("10")
    assert fill.price == D("94.25")
    assert "sip:quote" in result.provenance
    assert result.estimated is True and result.broker_confirmed is False


def test_continuous_trail_tracks_quote_high_and_stop_never_decreases(tmp_path):
    h = Harness(tmp_path / "continuous.db")
    h.buy()
    policy = ProtectionPolicy(ProtectionKind.BROKER_TRAILING_STOP, trail_amount=D("5"))
    sim = ShadowStopSimulator(h.account)
    run(sim.arm("cycle-1", "ABC", policy, at=NOW))
    states = [run(sim.evaluate("cycle-1", h.quote(price, i + 1), at=NOW + timedelta(seconds=i + 1)))
              for i, price in enumerate(("102", "106", "103"))]
    assert [state.high_water_mark for state in states] == [D("102"), D("106"), D("106")]
    assert [state.stop_price for state in states] == [D("97"), D("101"), D("101")]


def test_stepped_trail_ratchets_at_thresholds_only(tmp_path):
    h = Harness(tmp_path / "stepped.db")
    h.buy()
    policy = ProtectionPolicy(ProtectionKind.STEPPED_TRAILING_STOP, trail_amount=D("5"),
                              step_size=D("2"), activation_gain=D("2"))
    sim = ShadowStopSimulator(h.account)
    run(sim.arm("cycle-1", "ABC", policy, at=NOW))
    states = [run(sim.evaluate("cycle-1", h.quote(price, i + 1), at=NOW + timedelta(seconds=i + 1)))
              for i, price in enumerate(("101", "102", "103", "104", "99"))]
    assert [state.stop_price for state in states] == [D("95"), D("97"), D("97"), D("99"), D("99")]


def test_stop_waits_for_positive_fill_and_partial_fill_coverage_is_exact(tmp_path):
    h = Harness(tmp_path / "partial.db")
    intent = OrderIntent("entry", "entry-partial", SCOPE.scope_id, "cycle-1", "ABC", OrderSide.BUY,
                         OrderPurpose.ENTRY, OrderType.MARKET, TimeInForce.DAY, NOW, quantity=D("10"))
    order = run(h.account.submit_order(intent))
    sim = ShadowStopSimulator(h.account)
    assert run(sim.arm("cycle-1", "ABC", fixed(), at=NOW)) is None
    h.account.apply_fill(Fill("partial-fill", order.broker_order_id, SCOPE.scope_id, "ABC",
                              OrderSide.BUY, D("4"), D("100"), D("0"), NOW))
    state = run(sim.arm("cycle-1", "ABC", fixed(), at=NOW))
    assert state.covered_quantity == D("4")
    assert h.account.state.orders[state.order_id].quantity == D("4")


def test_gap_fills_available_bid_not_stop(tmp_path):
    h = Harness(tmp_path / "gap.db")
    h.buy()
    sim = ShadowStopSimulator(h.account)
    run(sim.arm("cycle-1", "ABC", fixed(), at=NOW))
    run(sim.evaluate("cycle-1", h.quote("88.75", 1), at=NOW + timedelta(seconds=1)))
    sell = next(fill for fill in h.account.state.fills.values() if fill.side is OrderSide.SELL)
    assert sell.price == D("88.75")
    assert sell.price != D("95")


def test_manual_exit_cancels_stop_before_fill_and_cannot_oversell(tmp_path):
    h = Harness(tmp_path / "exit.db")
    h.buy()
    sim = ShadowStopSimulator(h.account)
    state = run(sim.arm("cycle-1", "ABC", fixed(), at=NOW))
    exit_intent = OrderIntent("manual", "manual-client", SCOPE.scope_id, "cycle-1", "ABC",
        OrderSide.SELL, OrderPurpose.MANUAL_CLOSE, OrderType.MARKET, TimeInForce.DAY,
        NOW + timedelta(seconds=1), quantity=D("10"))
    exit_order = run(sim.submit_discretionary_exit(exit_intent))
    h.account.apply_fill(Fill("manual-fill", exit_order.broker_order_id, SCOPE.scope_id,
                              "ABC", OrderSide.SELL, D("10"), D("99"), D("0"), NOW + timedelta(seconds=2)))
    assert h.account.state.positions == {}
    assert h.account.state.orders[state.order_id].status.value == "CANCELED"
    assert sum((fill.quantity for fill in h.account.state.fills.values() if fill.side is OrderSide.SELL), D("0")) == D("10")


def test_missing_stale_or_missing_last_quote_are_unknown_without_trigger(tmp_path):
    h = Harness(tmp_path / "unknown.db")
    h.buy()
    sim = ShadowStopSimulator(h.account, StopTriggerAssumptions(trigger_source="LAST", max_quote_age=timedelta(seconds=2)))
    run(sim.arm("cycle-1", "ABC", fixed(), at=NOW))
    missing = run(sim.evaluate("cycle-1", None, at=NOW + timedelta(seconds=1)))
    assert missing.outcome is StopTriggerOutcome.UNKNOWN and missing.reason == "QUOTE_MISSING"
    stale = run(sim.evaluate("cycle-1", h.quote("94", -5), at=NOW + timedelta(seconds=1)))
    assert stale.outcome is StopTriggerOutcome.UNKNOWN and stale.reason == "QUOTE_STALE"
    quote = h.quote("94", 2)
    no_trade = run(sim.evaluate("cycle-1", quote, at=NOW + timedelta(seconds=2)))
    assert no_trade.outcome is StopTriggerOutcome.UNKNOWN and no_trade.reason == "TRIGGER_INPUT_MISSING"
    assert not any(fill.side is OrderSide.SELL for fill in h.account.state.fills.values())


def test_last_trade_trigger_uses_bid_for_execution(tmp_path):
    h = Harness(tmp_path / "last.db")
    h.buy()
    sim = ShadowStopSimulator(h.account, StopTriggerAssumptions(trigger_source="LAST"))
    run(sim.arm("cycle-1", "ABC", fixed(), at=NOW))
    quote = h.quote("96", 1)
    trade = TradeSnapshot("ABC", D("94"), D("1"), quote.provider_ts, quote.receive_ts, "sip")
    run(sim.evaluate("cycle-1", quote, trade=trade, at=quote.provider_ts))
    sell = next(fill for fill in h.account.state.fills.values() if fill.side is OrderSide.SELL)
    assert sell.price == D("96")


def test_state_restores_identically_from_ledger_with_labels_and_assumptions(tmp_path):
    h = Harness(tmp_path / "restore.db")
    h.buy()
    sim = ShadowStopSimulator(h.account, StopTriggerAssumptions(stop_slippage=D("0.10")))
    run(sim.arm("cycle-1", "ABC", fixed(), at=NOW))
    state = run(sim.evaluate("cycle-1", h.quote("97", 1), at=NOW + timedelta(seconds=1)))
    events = h.ledger.read_events()
    state_events = [event for event in events if "shadow_stop_record" in event.data]
    assert state_events
    last = state_events[-1]
    assert last.simulator_version == state.simulator_version
    assert last.assumptions["stop_slippage"] == "0.10"
    assert "sip:quote" in (last.data["shadow_stop_record"]["provenance"] or "")
    assert last.data["shadow_stop_record"]["estimated"] is True
    assert last.data["shadow_stop_record"]["broker_confirmed"] is False
    restored_account = ShadowAccount.open(h.ledger)
    restored = ShadowStopSimulator(restored_account, StopTriggerAssumptions(stop_slippage=D("0.10"))).state("cycle-1")
    assert restored == state


def test_policy_functions_are_shared_and_no_alpaca_dependency():
    source = open("mike_trade/brokers/shadow/stops.py", encoding="utf-8").read()
    tree = ast.parse(source)
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert "mike_trade.risk.trailing" in imports
    assert "mike_trade.domain.protection" in imports
    assert not any("alpaca" in module.lower() for module in imports)
