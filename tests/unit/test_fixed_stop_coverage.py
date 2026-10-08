from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mike_trade.domain.trading import (
    BrokerOrderStatus,
    Fill,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.risk.fixed_stop import (
    ALPACA_EQUITY_TICK_RULES,
    CoverageState,
    FixedStopCycleState,
    InvalidStopPrice,
    ProtectiveOrderView,
    RecommendedAction,
    build_fixed_stop_order,
    check_coverage,
    compute_fixed_stop_price,
    reconcile_after_fill,
)
from mike_trade.risk.stop_policy import ProtectionMode, ProtectionPolicy

D = Decimal
TICKS = ALPACA_EQUITY_TICK_RULES
NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


def policy(**changes) -> ProtectionPolicy:
    values = {"mode": ProtectionMode.FIXED, "distance_percent": D("5")}
    if any(k in changes for k in ("distance_usd", "atr_multiple", "support_level")):
        values.pop("distance_percent")
    values.update(changes)
    return ProtectionPolicy(**values)


def stop_for(pol: ProtectionPolicy, reference: str, **kw) -> Decimal:
    return compute_fixed_stop_price(pol, D(reference), tick_rules=TICKS, **kw)


# ----------------------------------------------------------------- distance types


def test_percent_below_reference():
    assert stop_for(policy(distance_percent=D("5")), "100") == D("95.00")


def test_usd_below_reference():
    assert stop_for(policy(distance_usd=D("2.50")), "100") == D("97.50")


def test_atr_multiple_below_reference():
    assert stop_for(policy(atr_multiple=D("2")), "100", atr=D("1.25")) == D("97.50")


def test_atr_missing_is_rejected():
    with pytest.raises(InvalidStopPrice) as exc:
        stop_for(policy(atr_multiple=D("2")), "100")
    assert exc.value.reason_code == "STOP_INPUT_MISSING"


def test_support_level_with_buffer():
    pol = policy(support_level=D("90"))
    assert stop_for(pol, "100", support=D("93.40"), support_buffer=D("0.50")) == D("92.90")
    assert stop_for(pol, "100") == D("90.00")  # falls back to the policy level


def test_negative_support_buffer_rejected():
    with pytest.raises(ValueError):
        stop_for(policy(support_level=D("90")), "100", support_buffer=D("-1"))


def test_non_fixed_policy_rejected():
    trailing = ProtectionPolicy(mode=ProtectionMode.TRAILING_NATIVE, trail_percent=D("3"))
    with pytest.raises(ValueError):
        stop_for(trailing, "100")


def test_float_inputs_rejected():
    with pytest.raises(TypeError):
        compute_fixed_stop_price(policy(), 100.0, tick_rules=TICKS)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        stop_for(policy(atr_multiple=D("2")), "100", atr=1.5)  # type: ignore[arg-type]


# ------------------------------------------------------------------- tick rounding


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("0.99999", "0.9999"),  # below $1: 0.0001 grid, floored
        ("0.9999", "0.9999"),
        ("1.00", "1.00"),  # at $1: 0.01 grid
        ("1.0099", "1.00"),
        ("1.019", "1.01"),
        ("95.019999", "95.01"),
    ],
)
def test_tick_floor_never_rounds_up(price, expected):
    floored = TICKS.floor(D(price))
    assert floored == D(expected)
    assert floored <= D(price)


def test_stop_rounds_down_across_one_dollar_boundary():
    # 1.05 * 95% = 0.9975 -> below $1, so the 0.0001 grid applies and it is kept.
    assert stop_for(policy(), "1.05") == D("0.9975")
    # 1.0099 - 0.005 = 1.0049 -> floored to 1.00 on the 0.01 grid, never up to 1.01.
    assert stop_for(policy(distance_usd=D("0.005")), "1.0099") == D("1.00")
    # 0.9999 reference at 5% = 0.949905 -> 0.9499
    assert stop_for(policy(), "0.9999") == D("0.9499")


def test_stop_exactly_at_one_dollar_is_valid():
    assert stop_for(policy(distance_usd=D("1")), "2.00") == D("1.00")


# ------------------------------------------------------------------ invalid stops


def test_stop_below_zero_rejected():
    with pytest.raises(InvalidStopPrice) as exc:
        stop_for(policy(distance_usd=D("10")), "5")
    assert exc.value.reason_code == "STOP_NOT_POSITIVE"


def test_stop_floors_to_zero_rejected():
    with pytest.raises(InvalidStopPrice) as exc:
        stop_for(policy(distance_usd=D("0.99995")), "1")
    assert exc.value.reason_code == "STOP_NOT_POSITIVE"


def test_stop_at_or_above_reference_rejected():
    with pytest.raises(InvalidStopPrice) as exc:
        stop_for(policy(support_level=D("100")), "100")
    assert exc.value.reason_code == "STOP_NOT_BELOW_REFERENCE"
    with pytest.raises(InvalidStopPrice):
        stop_for(policy(support_level=D("100")), "100", support=D("120"))


def test_sub_tick_distance_still_floors_below_reference():
    # raw 99.999 floors to 99.99, so flooring cannot land on or above the reference.
    assert stop_for(policy(distance_usd=D("0.001")), "100") == D("99.99")


# ------------------------------------------------------------------ order building


def test_build_stop_market_order():
    order = build_fixed_stop_order("cyc-1", "aapl", D("10"), D("95"), "STOP_MARKET", "GTC")
    assert order.side is OrderSide.SELL
    assert order.purpose is OrderPurpose.PROTECTIVE_STOP
    assert order.order_type is OrderType.STOP
    assert order.symbol == "AAPL"
    assert order.quantity == D("10")
    assert order.stop_price == D("95")
    assert order.limit_price is None
    assert order.time_in_force is TimeInForce.GTC
    assert order.risk_notes == ()


def test_build_stop_limit_order_limit_not_above_stop_and_noted():
    order = build_fixed_stop_order(
        "cyc-1",
        "AAPL",
        D("10"),
        D("95"),
        "STOP_LIMIT",
        TimeInForce.DAY,
        limit_offset=D("0.505"),
        tick_rules=TICKS,
    )
    assert order.order_type is OrderType.STOP_LIMIT
    assert order.limit_price == D("94.49")  # 94.495 floored
    assert order.limit_price is not None and order.limit_price <= order.stop_price
    assert any("may not fill" in note for note in order.risk_notes)


def test_stop_limit_requires_offset_and_ticks():
    with pytest.raises(ValueError):
        build_fixed_stop_order("c", "AAPL", D("1"), D("95"), "STOP_LIMIT", "GTC")


def test_stop_limit_with_nonpositive_limit_rejected():
    with pytest.raises(InvalidStopPrice):
        build_fixed_stop_order(
            "c", "AAPL", D("1"), D("0.50"), "STOP_LIMIT", "GTC",
            limit_offset=D("0.50"), tick_rules=TICKS,
        )  # fmt: skip


def test_stop_market_rejects_limit_offset():
    with pytest.raises(ValueError):
        build_fixed_stop_order(
            "c", "AAPL", D("1"), D("95"), "STOP_MARKET", "GTC", limit_offset=D("1")
        )


def test_client_order_id_deterministic_per_cycle_and_revision():
    def cid(cycle, revision):
        return build_fixed_stop_order(
            cycle, "AAPL", D("1"), D("95"), "STOP_MARKET", "GTC", revision=revision
        ).client_order_id

    assert cid("c1", 1) == cid("c1", 1)
    assert cid("c1", 1) != cid("c1", 2)
    assert cid("c1", 1) != cid("c2", 1)
    assert len(cid("c" * 80, 12)) <= 48


@pytest.mark.parametrize("qty", [D("0"), D("-1")])
def test_build_rejects_nonpositive_qty(qty):
    with pytest.raises(ValueError):
        build_fixed_stop_order("c", "AAPL", qty, D("95"), "STOP_MARKET", "GTC")


def test_build_rejects_float_qty():
    with pytest.raises(TypeError):
        build_fixed_stop_order("c", "AAPL", 1.0, D("95"), "STOP_MARKET", "GTC")  # type: ignore[arg-type]


def test_build_rejects_downward_move():
    with pytest.raises(InvalidStopPrice) as exc:
        build_fixed_stop_order(
            "c", "AAPL", D("1"), D("94.99"), "STOP_MARKET", "GTC", previous_stop_price=D("95")
        )
    assert exc.value.reason_code == "STOP_DOWNWARD_MOVE"
    same = build_fixed_stop_order(
        "c", "AAPL", D("1"), D("95"), "STOP_MARKET", "GTC", previous_stop_price=D("95")
    )
    assert same.stop_price == D("95")


# ----------------------------------------------------------------------- coverage


def view(cid: str, qty: str, filled: str = "0", status=BrokerOrderStatus.ACCEPTED, stop="95"):
    return ProtectiveOrderView(cid, D(qty), D(filled), status, D(stop))


def test_covered():
    result = check_coverage(D("10"), [view("a", "10")])
    assert result.state is CoverageState.COVERED
    assert result.action is RecommendedAction.NO_OP
    assert result.cancel_order_ids == ()


def test_covered_uses_open_quantity_after_partial_fill():
    result = check_coverage(D("7"), [view("a", "10", "3", BrokerOrderStatus.PARTIALLY_FILLED)])
    assert result.state is CoverageState.COVERED
    assert result.open_qty == D("7")


def test_under_covered():
    result = check_coverage(D("10"), [view("a", "3")])
    assert result.state is CoverageState.UNDER_COVERED
    assert result.action is RecommendedAction.REPLACE_TO_QTY
    assert result.cancel_order_ids == ("a",)
    assert result.target_qty == D("10")


def test_over_covered_single_order_cancels_it():
    result = check_coverage(D("7"), [view("a", "10")])
    assert result.state is CoverageState.OVER_COVERED
    assert result.action is RecommendedAction.CANCEL_EXCESS
    assert result.cancel_order_ids == ("a",)


def test_multiple_effective_keeps_one():
    result = check_coverage(D("10"), [view("a", "4"), view("b", "6")])
    assert result.state is CoverageState.MULTIPLE_EFFECTIVE
    assert result.action is RecommendedAction.CANCEL_EXCESS
    assert result.cancel_order_ids == ("a",)  # keep the largest ("b")


def test_multiple_oversold_reports_over_covered_and_keeps_fitting_order():
    result = check_coverage(D("10"), [view("a", "10"), view("b", "10")])
    assert result.state is CoverageState.OVER_COVERED
    assert result.cancel_order_ids == ("b",)


def test_none():
    place = check_coverage(D("10"), [])
    assert (place.state, place.action) == (CoverageState.NONE, RecommendedAction.PLACE)
    assert place.target_qty == D("10")


def test_terminal_orders_do_not_count():
    orders = [
        view("a", "10", "10", BrokerOrderStatus.FILLED),
        view("b", "10", status=BrokerOrderStatus.CANCELED),
        view("c", "10", status=BrokerOrderStatus.EXPIRED),
    ]
    assert check_coverage(D("10"), orders).state is CoverageState.NONE


@pytest.mark.parametrize("status", [BrokerOrderStatus.PENDING_CANCEL, BrokerOrderStatus.UNKNOWN])
def test_possibly_live_orders_count_as_effective(status):
    assert check_coverage(D("10"), [view("a", "10", status=status)]).state is CoverageState.COVERED


def test_zero_remaining_cancels_all_protection():
    result = check_coverage(D("0"), [view("a", "10"), view("b", "5")])
    assert result.state is CoverageState.OVER_COVERED
    assert result.action is RecommendedAction.CANCEL_EXCESS
    assert set(result.cancel_order_ids) == {"a", "b"}


def test_zero_remaining_without_orders_is_noop():
    result = check_coverage(D("0"), [])
    assert (result.state, result.action) == (CoverageState.NONE, RecommendedAction.NO_OP)


def test_check_coverage_rejects_float_and_negative():
    with pytest.raises(TypeError):
        check_coverage(10.0, [])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        check_coverage(D("-1"), [])


# ------------------------------------------------------------- reconcile sequence


def fill(fill_id: str, qty: str, price: str, side=OrderSide.BUY) -> Fill:
    return Fill(fill_id, "b-1", "acct", "AAPL", side, D(qty), D(price), D("0"), NOW)


def new_state(**changes) -> FixedStopCycleState:
    values = {"cycle_id": "cyc-1", "symbol": "AAPL", "policy": policy(), "tick_rules": TICKS}
    values.update(changes)
    return FixedStopCycleState(**values)


def as_view(intent, status=BrokerOrderStatus.ACCEPTED) -> ProtectiveOrderView:
    return ProtectiveOrderView(
        intent.client_order_id, intent.quantity, D("0"), status, intent.stop_price
    )


def test_first_fill_places_protection_for_filled_quantity():
    result = reconcile_after_fill(new_state(), fill("f1", "3", "100"))
    assert result.coverage.state is CoverageState.NONE
    assert result.place is not None
    assert result.place.quantity == D("3")
    assert result.place.stop_price == D("95.00")
    assert result.cancel_order_ids == ()
    assert result.state.last_stop_price == D("95.00")
    assert result.state.revision == 1


def test_partial_fill_sequence_3_then_7_yields_single_covering_order():
    # first partial fill: place 3
    step1 = reconcile_after_fill(new_state(), fill("f1", "3", "100"))
    first = step1.place
    assert first is not None and first.quantity == D("3")
    state = replace(step1.state, protective_orders=(as_view(first),))

    # second partial fill (7 more): cancel the 3-share order; do not place yet
    step2 = reconcile_after_fill(state, fill("f2", "7", "100"))
    assert step2.coverage.state is CoverageState.UNDER_COVERED
    assert step2.cancel_order_ids == (first.client_order_id,)
    assert step2.place is None  # no overlap while the old order may still be live
    state = step2.state

    # cancel in flight: nothing new is planned
    pending = replace(state, protective_orders=(as_view(first, BrokerOrderStatus.PENDING_CANCEL),))
    wait = reconcile_after_fill(pending)
    assert wait.awaiting_cancel
    assert wait.cancel_order_ids == () and wait.place is None

    # cancel confirmed: place one order for the cumulative 10
    canceled = replace(state, protective_orders=(as_view(first, BrokerOrderStatus.CANCELED),))
    step3 = reconcile_after_fill(canceled)
    assert step3.place is not None
    assert step3.place.quantity == D("10")
    assert step3.place.revision == 2
    assert step3.place.client_order_id != first.client_order_id

    final = replace(
        step3.state,
        protective_orders=(
            as_view(first, BrokerOrderStatus.CANCELED),
            as_view(step3.place),
        ),
    )
    covered = check_coverage(final.remaining_qty, final.protective_orders)
    assert covered.state is CoverageState.COVERED
    assert covered.effective_order_ids == (step3.place.client_order_id,)
    assert reconcile_after_fill(final).place is None  # settled: no further orders


def test_duplicate_fill_is_not_double_counted():
    first = reconcile_after_fill(new_state(), fill("f1", "3", "100"))
    again = reconcile_after_fill(first.state, fill("f1", "3", "100"))
    assert again.state.entry_qty == D("3")


def test_no_placement_while_effective_order_exists():
    first = reconcile_after_fill(new_state(), fill("f1", "3", "100"))
    state = replace(first.state, protective_orders=(as_view(first.place),))
    for qty_fill in (fill("f2", "2", "100"), fill("f3", "5", "100")):
        result = reconcile_after_fill(state, qty_fill)
        assert result.place is None
        state = result.state


def test_stop_never_moves_downward_across_replacements():
    first = reconcile_after_fill(new_state(), fill("f1", "3", "100"))  # stop 95
    state = replace(
        first.state,
        protective_orders=(as_view(first.place, BrokerOrderStatus.CANCELED),),
    )
    # later fill at a lower price drags the average (and computed stop) down
    state = reconcile_after_fill(state, fill("f2", "7", "99")).state
    result = reconcile_after_fill(state)
    assert result.place is not None
    computed = stop_for(policy(), str(result.state.avg_fill_price))
    assert computed < D("95.00")
    assert result.place.stop_price == D("95.00")  # max(previous, computed)


def test_stop_ratchets_up_when_computed_stop_is_higher():
    first = reconcile_after_fill(new_state(), fill("f1", "3", "100"))
    state = replace(
        first.state,
        protective_orders=(as_view(first.place, BrokerOrderStatus.CANCELED),),
    )
    state = reconcile_after_fill(state, fill("f2", "7", "110")).state
    result = reconcile_after_fill(state)
    assert result.place is not None and result.place.stop_price > D("95.00")


def test_ratcheted_stop_at_or_above_reference_is_not_placed():
    first = reconcile_after_fill(new_state(), fill("f1", "3", "100"))  # stop 95
    state = replace(first.state, protective_orders=(as_view(first.place),))
    # average falls to 91.9: the preserved 95 stop would sit above the reference
    result = reconcile_after_fill(state, fill("f2", "7", "89"))
    assert result.reason_code == "STOP_NOT_BELOW_REFERENCE"
    assert result.place is None
    assert result.cancel_order_ids == ()  # keep the existing, smaller protection


def test_explicit_downward_stop_rejected_in_module():
    with pytest.raises(InvalidStopPrice):
        build_fixed_stop_order(
            "c", "AAPL", D("1"), D("90"), "STOP_MARKET", "GTC", previous_stop_price=D("95")
        )


def test_stop_limit_policy_flows_through_reconcile():
    pol = policy(order_semantics="STOP_LIMIT", limit_offset=D("0.50"))
    result = reconcile_after_fill(new_state(policy=pol), fill("f1", "10", "100"))
    assert result.place is not None
    assert result.place.order_type is OrderType.STOP_LIMIT
    assert result.place.limit_price == D("94.50")
    assert result.place.risk_notes


def test_exit_fill_shrinks_coverage_to_remaining():
    entry = reconcile_after_fill(new_state(), fill("f1", "10", "100"))
    state = replace(entry.state, protective_orders=(as_view(entry.place),))
    assert entry.place is not None
    result = reconcile_after_fill(state, fill("x1", "4", "101", OrderSide.SELL))
    assert result.state.remaining_qty == D("6")
    assert result.coverage.state is CoverageState.OVER_COVERED
    assert result.cancel_order_ids == (entry.place.client_order_id,)
    assert result.place is None

    gone = replace(
        result.state,
        protective_orders=(as_view(entry.place, BrokerOrderStatus.CANCELED),),
    )
    replacement = reconcile_after_fill(gone)
    assert replacement.place is not None and replacement.place.quantity == D("6")


def test_zero_remaining_recommends_cancel_and_places_nothing():
    entry = reconcile_after_fill(new_state(), fill("f1", "10", "100"))
    state = replace(entry.state, protective_orders=(as_view(entry.place),))
    assert entry.place is not None
    result = reconcile_after_fill(state, fill("x1", "10", "102", OrderSide.SELL))
    assert result.state.remaining_qty == D("0")
    assert result.coverage.action is RecommendedAction.CANCEL_EXCESS
    assert result.cancel_order_ids == (entry.place.client_order_id,)
    assert result.place is None

    closed = replace(
        result.state, protective_orders=(as_view(entry.place, BrokerOrderStatus.CANCELED),)
    )
    final = reconcile_after_fill(closed)
    assert final.coverage.action is RecommendedAction.NO_OP
    assert final.place is None and final.cancel_order_ids == ()


def test_exit_larger_than_position_rejected():
    entry = reconcile_after_fill(new_state(), fill("f1", "3", "100"))
    with pytest.raises(ValueError):
        reconcile_after_fill(entry.state, fill("x1", "4", "101", OrderSide.SELL))


def test_fill_for_other_symbol_rejected():
    other = Fill("f1", "b", "acct", "MSFT", OrderSide.BUY, D("1"), D("1"), D("0"), NOW)
    with pytest.raises(ValueError):
        reconcile_after_fill(new_state(), other)


def test_unresolved_non_default_reference_fails_closed():
    pol = policy(fixed_reference="PRIOR_CLOSE")
    result = reconcile_after_fill(new_state(policy=pol), fill("f1", "3", "100"))
    assert result.reason_code == "STOP_INPUT_MISSING"
    assert result.place is None
    resolved = reconcile_after_fill(
        new_state(policy=pol, reference_price=D("100")), fill("f1", "3", "100")
    )
    assert resolved.place is not None


def test_multiple_effective_orders_never_get_a_new_overlapping_order():
    state = new_state(
        entry_qty=D("10"),
        entry_cost=D("1000"),
        protective_orders=(view("a", "4"), view("b", "6")),
    )
    result = reconcile_after_fill(state)
    assert result.place is None
    assert result.cancel_order_ids == ("a",)
