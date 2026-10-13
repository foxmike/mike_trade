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
from mike_trade.risk import michelle_stop

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

    def buy(self, quantity=D("10"), price=D("100")):
        intent = OrderIntent(
            intent_id="entry-1", client_order_id="entry-client-1", account_scope_id=SCOPE.scope_id,
            cycle_id="cycle-1", symbol="ABC", side=OrderSide.BUY, purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY, created_at=NOW,
            quantity=quantity,
        )
        order = run(self.account.submit_order(intent))
        self.account.apply_fill(Fill("entry-fill-1", order.broker_order_id, SCOPE.scope_id,
                                     "ABC", OrderSide.BUY, quantity, price, D("0"), NOW))

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


def _trade(price: str, sec: int) -> TradeSnapshot:
    timestamp = NOW + timedelta(seconds=sec)
    return TradeSnapshot("ABC", D(price), D("1"), timestamp, timestamp, "sip")


def test_michelle_fill_based_stop_activates_only_above_initial_and_ratchets_up(tmp_path):
    # Spec example: fill 400.00 -> initial 399.75; the entry-time 0.2% candidate (399.20) is lower.
    h = Harness(tmp_path / "michelle-transition.db")
    h.account.update_quote("ABC", D("400"))
    h.buy(price=D("400"))
    sim = ShadowStopSimulator(h.account)
    policy = michelle_stop.policy_for_fill(D("400"))
    armed = run(sim.arm("cycle-1", "ABC", policy, at=NOW))
    assert armed.stop_price == D("399.75") and armed.high_water_mark == D("400")

    def step(price: str, sec: int):
        return run(sim.evaluate("cycle-1", h.quote(price, sec), at=NOW + timedelta(seconds=sec),
                                trade=_trade(price, sec)))

    fixed_state = step("400.10", 1)       # candidate floor(399.2998)=399.29 < 399.75
    at_boundary = step("400.50", 2)       # candidate floor(399.699)=399.69 < 399.75
    activated = step("401.00", 3)         # 400.198 -> 400.19 > 399.75
    pullback = step("400.60", 4)
    higher = step("402.00", 5)            # 401.196 -> 401.19
    assert fixed_state.stop_price == at_boundary.stop_price == D("399.75")
    assert activated.high_water_mark == D("401.00") and activated.stop_price == D("400.19")
    assert pullback.stop_price == D("400.19")
    assert higher.stop_price == D("401.19")
    # 0.2%, not 0.5%: a 0.5% trail from 402.00 would be 399.99
    assert higher.stop_price > D("402") * D("0.995")


def test_michelle_shadow_stop_equal_to_initial_does_not_activate(tmp_path):
    # Fill 500.00 -> initial 499.75. floor(0.998 * H) == 499.75 needs H == 500.7515..: use
    # H = 500.75 -> 499.749 -> 499.74 (below); H = 500.76 -> 499.75848 -> 499.75 (equal, no activation).
    h = Harness(tmp_path / "michelle-equal.db")
    h.account.update_quote("ABC", D("500"))
    h.buy(price=D("500"))
    sim = ShadowStopSimulator(h.account)
    armed = run(sim.arm("cycle-1", "ABC", michelle_stop.policy_for_fill(D("500")), at=NOW))
    assert armed.stop_price == D("499.75")
    equal = run(sim.evaluate("cycle-1", h.quote("500.76", 1), at=NOW + timedelta(seconds=1),
                             trade=_trade("500.76", 1)))
    assert equal.stop_price == D("499.75")
    above = run(sim.evaluate("cycle-1", h.quote("500.78", 2), at=NOW + timedelta(seconds=2),
                             trade=_trade("500.78", 2)))
    assert above.stop_price == D("499.77")  # floor(499.77844) > 499.75


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
