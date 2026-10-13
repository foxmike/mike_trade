"""Michelle's fill-based stop: pure rule, rounding, activation, and atomic entry safety."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from mike_trade.backtest.execution import ExecutionAssumptions
from mike_trade.domain.protection import ProtectionKind
from mike_trade.risk import michelle_stop

ID = D("0.25")


@pytest.mark.parametrize(
    ("fill", "expected"),
    [
        ("400.00", "399.75"),
        ("428.9944", "428.75"),   # raw 428.7444 rounds UP: distance 0.2444 <= 0.25
        ("307.15", "306.90"),
        ("100.001", "99.76"),     # raw 99.751 -> 99.76, never 99.75 (distance would be 0.251)
        ("1.10", "0.8500"),       # sub-dollar stop uses the 0.0001 grid
    ],
)
def test_initial_stop_is_fill_minus_25c_rounded_up_never_beyond_25c(fill, expected):
    stop = michelle_stop.initial_stop(D(fill))
    assert stop == D(expected)
    assert D(fill) - stop <= ID  # the maximum initial distance is never exceeded
    assert D(fill) - stop > ID - D("0.01")  # and rounding gives away less than one tick


def test_no_valid_initial_stop_when_the_fill_cannot_be_protected():
    assert michelle_stop.initial_stop(D("0.25")) is None  # stop would be zero
    assert michelle_stop.initial_stop(D("0")) is None
    assert michelle_stop.policy_for_fill(D("0.20")) is None


def test_spec_example_trailing_candidate_below_then_above_the_initial_stop():
    initial = michelle_stop.initial_stop(D("400"))
    assert initial == D("399.75")
    # at entry the highest price is the fill: candidate 399.20 is below the initial stop
    assert michelle_stop.trailing_candidate(D("400")) == D("399.20")
    assert michelle_stop.next_stop(initial, initial, D("400")) == initial
    # high 401.00: 400.198 -> exchange-valid 400.19 > 399.75
    assert michelle_stop.trailing_candidate(D("401")) == D("400.19")
    active = michelle_stop.next_stop(initial, initial, D("401"))
    assert active == D("400.19")
    # a decline (the highest price does not fall) never lowers it; a new high ratchets it
    assert michelle_stop.next_stop(initial, active, D("401")) == active
    higher = michelle_stop.next_stop(initial, active, D("403"))
    assert higher == D("402.19") and higher > active  # 402.194 -> 402.19
    # a lower observed value (e.g. state replayed with a stale high) cannot lower the stop
    assert michelle_stop.next_stop(initial, higher, D("401")) == higher


def test_equal_candidate_does_not_activate_but_one_tick_above_does():
    initial = michelle_stop.initial_stop(D("500"))
    assert initial == D("499.75")
    assert michelle_stop.trailing_candidate(D("500.76")) == initial  # equality
    assert michelle_stop.next_stop(initial, initial, D("500.76")) == initial
    assert michelle_stop.trailing_candidate(D("500.77")) == D("499.76")
    assert michelle_stop.next_stop(initial, initial, D("500.77")) == D("499.76")


def test_trail_is_two_tenths_of_a_percent_not_half():
    assert michelle_stop.MICHELLE_TRAIL_PERCENT == D("0.2")
    assert michelle_stop.trailing_candidate(D("1000")) == D("998.00")  # 0.5% would be 995.00


def test_policy_is_fill_based_and_identical_for_vwap_and_orb():
    policy = michelle_stop.policy_for_fill(D("400"))
    assert policy.kind is ProtectionKind.FIXED_THEN_TRAILING_STOP
    assert policy.trail_percent == D("0.2")
    assert policy.initial_stop_price == D("399.75")
    assert policy.activation_reference == policy.initial_stop_price  # not reference + $0.25
    assert michelle_stop.is_current_policy(policy)
    sells = ("five_minute_orb_michelle_sell", "vwap_bounce_break_michelle_sell")
    for buy in ("five_minute_orb_michelle_buy", "vwap_bounce_break_michelle_buy"):
        assert michelle_stop.protection_applies(buy, sells)
        assert not michelle_stop.protection_applies(buy, ())
    assert not michelle_stop.protection_applies("vwap_bounce", sells)


def test_legacy_reference_based_policy_is_not_current():
    from mike_trade.domain.protection import ProtectionPolicy

    legacy = ProtectionPolicy(
        ProtectionKind.FIXED_THEN_TRAILING_STOP, trail_percent=D("0.5"),
        initial_stop_price=D("99.75"), activation_reference=D("100.25"),
    )
    assert not michelle_stop.is_current_policy(legacy)


def test_calculations_do_not_use_floats():
    assert isinstance(michelle_stop.trailing_candidate(D("123.45")), D)
    assert michelle_stop.initial_stop(123.45) is None  # a float is rejected, not coerced


def test_zero_cost_assumptions_are_unchanged_defaults():
    assumptions = ExecutionAssumptions()
    assert assumptions.slippage_bps == 0 and assumptions.slippage_ticks == 0
    assert assumptions.fee_per_order == assumptions.fee_per_share == assumptions.fee_bps == 0
