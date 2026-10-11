from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal

import pytest

from mike_trade.domain.trading import TimeInForce
from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES
from mike_trade.risk.trailing import (
    DistanceKind,
    RatchetResult,
    SteppedTrailingPolicy,
    SteppedTrailingState,
    advance,
    initial_state,
    should_replace,
)

D = Decimal


def policy(**changes):
    values = dict(
        distance_kind=DistanceKind.USD,
        distance_value=D("2"),
        step_kind=DistanceKind.USD,
        step_value=D("1"),
        min_replace_interval=timedelta(seconds=30),
        time_in_force=TimeInForce.GTC,
        policy_revision="rev-1",
    )
    values.update(changes)
    return SteppedTrailingPolicy(**values)


def test_canonical_usd_example():
    p = policy()
    s = initial_state(D("100"), p)
    assert s.candidate_stop == D("98")
    s = advance(s, D("100.80"), p)
    assert (s.candidate_stop, s.steps_advanced, s.high_watermark) == (D("98"), 0, D("100.80"))
    s = advance(s, D("101"), p)
    assert (s.candidate_stop, s.ratchet_anchor, s.steps_advanced) == (D("99"), D("101"), 1)
    s = advance(s, D("103.20"), p)
    assert (s.candidate_stop, s.ratchet_anchor, s.steps_advanced) == (D("101"), D("103"), 2)
    for decline in (D("102"), D("99")):
        s = advance(s, decline, p)
        assert s.candidate_stop == D("101")
        assert s.ratchet_anchor == D("103")
        assert s.high_watermark == D("103.20")
        assert s.steps_advanced == 0


def test_result_type_is_state():
    assert RatchetResult is SteppedTrailingState


def test_percent_distance_and_step_use_entry_reference_basis():
    p = policy(
        distance_kind=DistanceKind.PERCENT,
        distance_value=D("2"),
        step_kind=DistanceKind.PERCENT,
        step_value=D("1"),
    )
    s = initial_state(D("200"), p)
    assert s.candidate_stop == D("196")
    # step is 1% of the *entry* (2.00), not of the moving high
    s = advance(s, D("201.99"), p)
    assert s.candidate_stop == D("196")
    s = advance(s, D("202"), p)
    assert (s.ratchet_anchor, s.candidate_stop) == (D("202"), D("198"))
    s = advance(s, D("300"), p)  # 49 steps of 2.00 from 202 -> 300
    assert (s.ratchet_anchor, s.candidate_stop, s.steps_advanced) == (D("300"), D("296"), 49)


def test_mixed_kinds():
    p = policy(step_kind=DistanceKind.PERCENT, step_value=D("0.5"))
    s = advance(initial_state(D("100"), p), D("101.01"), p)
    assert (s.ratchet_anchor, s.steps_advanced, s.candidate_stop) == (D("101"), 2, D("99"))


def test_never_lowers_stop_over_arbitrary_sequence():
    p = policy()
    s = initial_state(D("50"), p)
    last = s.candidate_stop
    for high in ("51.5", "49", "53.1", "52", "50", "60.9", "10", "61"):
        s = advance(s, D(high), p)
        assert s.candidate_stop >= last
        last = s.candidate_stop


def test_wider_distance_policy_cannot_lower_stop():
    s = advance(initial_state(D("100"), policy()), D("105"), policy())
    assert s.candidate_stop == D("103")
    wider = policy(distance_value=D("10"))
    assert advance(s, D("105"), wider).candidate_stop == D("103")


@pytest.mark.parametrize("bad", [0, -1, 1.0, "1", None, True, D("0"), D("-1"), D("NaN"), D("Infinity")])
@pytest.mark.parametrize("field", ["distance_value", "step_value"])
def test_policy_rejects_bad_amounts(field, bad):
    with pytest.raises(ValueError):
        policy(**{field: bad})


def test_policy_rejects_other_bad_fields():
    with pytest.raises(ValueError):
        policy(distance_kind="EUR")
    with pytest.raises(ValueError):
        policy(step_kind=None)
    with pytest.raises(ValueError):
        policy(min_replace_interval=timedelta(seconds=-1))
    with pytest.raises(ValueError):
        policy(min_replace_interval=30)
    with pytest.raises(ValueError):
        policy(time_in_force="IOC")
    with pytest.raises(ValueError):
        policy(policy_revision=" ")
    with pytest.raises(ValueError):
        policy(requires_stop_replace="yes")


def test_policy_normalizes_and_is_frozen():
    p = policy(distance_kind="PERCENT", time_in_force="day", min_replace_interval=timedelta(0))
    assert p.distance_kind is DistanceKind.PERCENT
    assert p.time_in_force is TimeInForce.DAY
    assert p.requires_standalone_stop and p.requires_stop_replace
    with pytest.raises(AttributeError):
        p.distance_value = D("3")


def test_float_inputs_rejected():
    p = policy()
    with pytest.raises(ValueError):
        initial_state(100.0, p)
    s = initial_state(D("100"), p)
    with pytest.raises(ValueError):
        advance(s, 101.0, p)
    with pytest.raises(ValueError):
        should_replace(99.0, D("98"), lambda x: x)
    with pytest.raises(ValueError):
        should_replace(D("99"), 98.0, lambda x: x)


def test_invalid_entry_and_distance_too_wide():
    p = policy()
    for bad in (D("0"), D("-5"), D("NaN")):
        with pytest.raises(ValueError):
            initial_state(bad, p)
    with pytest.raises(ValueError):
        initial_state(D("2"), p)  # stop would be 0
    with pytest.raises(ValueError):
        advance(initial_state(D("100"), p), D("0"), p)


def test_invalid_side_is_rejected_and_must_match_the_state():
    p = policy()
    s = initial_state(D("100"), p)
    with pytest.raises(ValueError):
        advance(s, D("99"), p, side="FLAT")
    with pytest.raises(ValueError):
        initial_state(D("100"), p, side="SIDEWAYS")
    with pytest.raises(ValueError, match="does not match"):
        advance(s, D("99"), p, side="SHORT")  # a LONG state cannot be advanced as a short


def test_short_stop_starts_above_entry_and_ratchets_down_only():
    p = policy()  # distance $2, step $1
    s = initial_state(D("100"), p, side="SHORT")
    assert s.direction == "SHORT" and s.candidate_stop == D("102")
    # price falls $3.50 -> three whole $1 steps: anchor 97, stop 99 (never above entry + dist)
    s = advance(s, D("96.50"), p)
    assert (s.high_watermark, s.ratchet_anchor, s.candidate_stop, s.steps_advanced) == (
        D("96.50"), D("97"), D("99"), 3)
    # a rebound (higher low) never loosens the stop, anchor or watermark
    rebound = advance(s, D("99.90"), p)
    assert (rebound.high_watermark, rebound.ratchet_anchor, rebound.candidate_stop) == (
        D("96.50"), D("97"), D("99"))
    assert rebound.steps_advanced == 0
    # sub-step improvement does not reprice
    assert advance(s, D("96.10"), p).candidate_stop == D("99")


def test_short_should_replace_rounds_up_and_only_tightens_downward():
    ceil = lambda v: ALPACA_EQUITY_TICK_RULES.ceil(v)  # noqa: E731
    assert should_replace(D("99.004"), D("102"), ceil, side="SHORT") is True  # 99.01 < 102
    assert should_replace(D("102"), D("102"), ceil, side="SHORT") is False   # no change
    assert should_replace(D("103"), D("102"), ceil, side="SHORT") is False   # would loosen
    with pytest.raises(ValueError, match="round up"):
        should_replace(D("99.004"), D("102"), lambda v: v.quantize(D("0.01"), ROUND_FLOOR), side="SHORT")


def test_short_state_invariants():
    with pytest.raises(ValueError):
        SteppedTrailingState(D("100"), D("100"), D("100"), D("99"), 0, "SHORT")  # stop below
    with pytest.raises(ValueError):
        SteppedTrailingState(D("100"), D("101"), D("100"), D("102"), 0, "SHORT")  # watermark up


def test_state_is_frozen_and_validated():
    s = initial_state(D("100"), policy())
    with pytest.raises(AttributeError):
        s.candidate_stop = D("1")
    with pytest.raises(ValueError):
        SteppedTrailingState(D("100"), D("99"), D("100"), D("98"))
    with pytest.raises(ValueError):
        SteppedTrailingState(D("100"), D("100"), D("100"), D("100"))


def floor_to_cent(price):
    return (price / D("0.01")).to_integral_value(rounding=ROUND_FLOOR) * D("0.01")


def test_should_replace_rounds_down_before_comparing():
    # 98.019 floors to 98.01: not above 98.01 -> no replace, though raw > confirmed
    assert should_replace(D("98.019"), D("98.01"), floor_to_cent) is False
    assert should_replace(D("98.021"), D("98.01"), floor_to_cent) is True


def test_should_replace_never_downward_or_equal():
    assert should_replace(D("99"), D("99"), floor_to_cent) is False
    assert should_replace(D("98"), D("99"), floor_to_cent) is False
    assert should_replace(D("99.01"), D("99"), floor_to_cent) is True


def test_should_replace_with_real_tick_rules_and_bad_rounder():
    assert should_replace(D("99.0149"), D("99.01"), ALPACA_EQUITY_TICK_RULES.floor) is False
    with pytest.raises(ValueError):
        should_replace(D("99"), D("98"), lambda x: x + D("0.01"))
    with pytest.raises(ValueError):
        should_replace(D("99"), D("98"), None)
