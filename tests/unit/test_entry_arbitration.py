"""Deterministic BUY-side arbitration and stop-policy configuration mapping."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.domain.protection import ProtectionKind
from mike_trade.risk.stop_config import normalize_stop_spec, protection_policy_for_entry
from mike_trade.signals.decisions import (
    DecisionOutcome,
    EntryCandidate,
    EntryDecisionReason,
    arbitrate_entry_decisions,
)

NOW = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
BAR = NOW - timedelta(minutes=1)


def cand(strategy: str, *, bar: datetime = BAR, signal: str | None = None, symbol: str = "SPY"):
    return EntryCandidate(signal or f"sig-{strategy}", strategy, "1.0.0", symbol, bar)


def arbitrate(candidates, **kw):
    kw.setdefault("position_open", False)
    return arbitrate_entry_decisions(candidates, symbol="SPY", evaluation_time=NOW, **kw)


def test_exactly_one_entry_selected_by_stable_tie_break_in_any_input_order():
    base = [cand("momentum_breakout"), cand("vwap_bounce"), cand("opening_range_breakout")]
    expected = None
    for seed in range(20):
        shuffled = list(base)
        random.Random(seed).shuffle(shuffled)
        result = arbitrate(shuffled)
        assert result.selected is not None
        assert result.selected.strategy_id == "momentum_breakout"  # first by strategy_id
        outcomes = {r.strategy_id: (r.outcome, r.reason) for r in result.records}
        assert sum(o is DecisionOutcome.SELECTED for o, _ in outcomes.values()) == 1
        assert outcomes["vwap_bounce"] == (
            DecisionOutcome.REJECTED, EntryDecisionReason.SUPERSEDED_BY_TIE_BREAK)
        expected = expected or result.to_dict()
        assert result.to_dict() == expected  # identical audit record every time


def test_earlier_completed_bar_wins_before_strategy_id():
    older = cand("zeta_entry", bar=BAR - timedelta(minutes=1))
    result = arbitrate([cand("alpha_entry"), older])
    assert result.selected.strategy_id == "zeta_entry"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"position_open": True}, EntryDecisionReason.POSITION_OPEN),
        ({"entry_pending": True}, EntryDecisionReason.ENTRY_ALREADY_PENDING),
        ({"seen_signal_ids": {"sig-a"}}, EntryDecisionReason.DUPLICATE_SIGNAL),
    ],
)
def test_no_entry_when_a_position_or_entry_order_exists_or_signal_replayed(kwargs, reason):
    result = arbitrate([cand("a", signal="sig-a")], **kwargs)
    assert result.selected is None
    assert [r.reason for r in result.records] == [reason]


def test_symbol_mismatch_is_rejected_and_audit_is_json_safe():
    result = arbitrate([cand("a", symbol="QQQ"), cand("b")])
    assert result.selected.strategy_id == "b"
    assert result.records[0].reason is EntryDecisionReason.SYMBOL_MISMATCH
    json.dumps(result.to_dict())


def test_no_candidates_selects_nothing():
    result = arbitrate([])
    assert result.selected is None and result.records == ()


# ------------------------------------------------------------ stop configuration


def test_fixed_percent_and_amount_map_to_fixed_stop_policies():
    percent = protection_policy_for_entry({"type": "fixed", "stop_percent": "1.5"}, D("100"))
    amount = protection_policy_for_entry({"type": "fixed", "stop_amount": "2.25"}, D("100"))
    assert percent.kind is ProtectionKind.FIXED_STOP and percent.stop_offset_percent == D("1.5")
    assert amount.kind is ProtectionKind.FIXED_STOP and amount.stop_offset_amount == D("2.25")


def test_native_trailing_percent_and_amount_map_to_broker_trailing_policies():
    percent = protection_policy_for_entry(
        {"type": "native_trailing", "trail_percent": D("1.5")}, D("100"))
    amount = protection_policy_for_entry(
        {"type": "native_trailing", "trail_amount": "0.75"}, D("100"))
    assert percent.kind is ProtectionKind.BROKER_TRAILING_STOP
    assert (percent.trail_percent, amount.trail_amount) == (D("1.5"), D("0.75"))


def test_stepped_trailing_resolves_percent_steps_on_the_entry_price():
    policy = protection_policy_for_entry(
        {"type": "stepped_trailing", "initial_stop_percent": "1.5",
         "step_trigger_percent": "1.0", "step_percent": "0.5"}, D("200"))
    assert policy.kind is ProtectionKind.STEPPED_TRAILING_STOP
    assert (policy.trail_percent, policy.activation_gain, policy.step_size) == (
        D("1.5"), D("2.0"), D("1.0"))


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "bogus"},
        {"type": "fixed"},
        {"type": "fixed", "stop_percent": "1", "stop_amount": "1"},
        {"type": "native_trailing", "trail_percent": "0"},
        {"type": "native_trailing", "trail_percent": "60"},
        {"type": "native_trailing", "trail_percent": 1.5},  # floats are refused
        {"type": "stepped_trailing", "initial_stop_percent": "1"},
        {"type": "fixed", "stop_percent": "1", "extra": "x"},
    ],
)
def test_invalid_stop_specs_fail_closed(spec):
    with pytest.raises(ValueError):
        normalize_stop_spec(spec)
