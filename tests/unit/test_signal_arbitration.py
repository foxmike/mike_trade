"""Unit tests for exit-signal arbitration and restart-stable rearm suppression."""

from __future__ import annotations

import dataclasses
import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import ManagementStatus, Position, PositionDirection
from mike_trade.signals.decisions import (
    ArbitrationResult,
    DecisionOutcome,
    DecisionReason,
    DecisionRecord,
    ExitCandidate,
    arbitrate_exit_decisions,
)
from mike_trade.signals.rearm import (
    RearmPolicy,
    RearmState,
    check_and_record,
    reset_cycle,
)
from mike_trade.strategies.base import make_signal_id
from mike_trade.strategies.sell.base import ExitLongRecommendation, ExitSource, SellReasonCode

SYMBOL = "SPY"
ENTRY = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
BAR = datetime(2026, 10, 8, 13, 55, tzinfo=UTC)
HOLD = timedelta(minutes=45)
CYCLE = "cycle-1"


def position(
    quantity: str = "100",
    management: ManagementStatus = ManagementStatus.MANAGED,
    cycle_id: str | None = CYCLE,
) -> Position:
    return Position(
        account_scope_id="acct",
        symbol=SYMBOL,
        quantity=Decimal(quantity),
        avg_entry_price=Decimal("100"),
        cost_basis=Decimal(quantity) * Decimal("100"),
        as_of=NOW,
        management=management,
        cycle_id=cycle_id if management is ManagementStatus.MANAGED else None,
    )


def sell(
    strategy_id: str = "rsi_reversal",
    bar: datetime = BAR,
    enabled: bool = True,
    stale: bool = False,
) -> ExitCandidate:
    pos = position()
    rec = ExitLongRecommendation(
        signal_id=make_signal_id(strategy_id, "1.0.0", SYMBOL, StrategyRole.SELL, bar),
        strategy_id=strategy_id,
        version="1.0.0",
        position=pos,
        bar_time=bar,
        reason_codes=(SellReasonCode.ELIGIBLE,),
    )
    return ExitCandidate.from_recommendation(rec, enabled=enabled, stale=stale)


def safety(source: ExitSource, sid: str | None = None) -> ExitCandidate:
    return ExitCandidate(
        signal_id=sid or f"{source.value.lower()}-1",
        source=source,
        symbol=SYMBOL,
        position_cycle_id=CYCLE,
        requested_at=NOW,
    )


def run(candidates, **kwargs) -> ArbitrationResult:
    kwargs.setdefault("position", position())
    kwargs.setdefault("entry_filled_at", ENTRY)
    return arbitrate_exit_decisions(
        candidates, symbol=SYMBOL, evaluation_time=NOW, **kwargs
    )


def record_for(result: ArbitrationResult, signal_id: str) -> DecisionRecord:
    (match,) = [r for r in result.records if r.signal_id == signal_id]
    return match


# ------------------------------------------------------------ arbitration


def test_stop_beats_strategy_sell_on_same_tick() -> None:
    stop = safety(ExitSource.PROTECTIVE_STOP)
    strat = sell()
    result = run([strat, stop])
    assert result.selected is not None
    assert result.selected.signal_id == stop.signal_id
    assert result.selected.source is ExitSource.PROTECTIVE_STOP
    loser = record_for(result, strat.signal_id)
    assert loser.outcome is DecisionOutcome.REJECTED
    assert loser.reason_codes == (DecisionReason.SUPERSEDED_BY_HIGHER_PRIORITY,)
    assert loser.superseded_by == stop.signal_id


def test_priority_order_stop_manual_risk_forced_strategy() -> None:
    ranked = [
        ExitSource.PROTECTIVE_STOP,
        ExitSource.MANUAL_CLOSE,
        ExitSource.HARD_RISK,
        ExitSource.FORCED_LIQUIDATION,
    ]
    pool = [sell()] + [safety(s) for s in reversed(ranked)]
    for expected in ranked:
        assert run(pool).selected.source is expected
        pool = [c for c in pool if c.source is not expected]
    assert run(pool).selected.source is ExitSource.STRATEGY_SELL


@pytest.mark.parametrize(
    "source",
    [
        ExitSource.PROTECTIVE_STOP,
        ExitSource.MANUAL_CLOSE,
        ExitSource.HARD_RISK,
        ExitSource.FORCED_LIQUIDATION,
    ],
)
def test_safety_exits_bypass_minimum_hold_but_strategy_sell_is_blocked(
    source: ExitSource,
) -> None:
    just_entered = NOW - timedelta(minutes=1)
    result = run([safety(source)], minimum_hold=HOLD, entry_filled_at=just_entered)
    assert result.selected is not None and result.selected.source is source

    strat = sell()
    blocked = run([strat], minimum_hold=HOLD, entry_filled_at=just_entered)
    assert blocked.selected is None
    assert record_for(blocked, strat.signal_id).reason_codes == (DecisionReason.MIN_HOLD_ACTIVE,)


def test_strategy_sell_allowed_once_minimum_hold_elapsed() -> None:
    strat = sell()
    result = run([strat], minimum_hold=HOLD, entry_filled_at=NOW - HOLD)
    assert result.selected is not None and result.selected.signal_id == strat.signal_id


def test_minimum_hold_requires_entry_fill_time() -> None:
    with pytest.raises(ValueError, match="entry_filled_at"):
        run([sell()], minimum_hold=HOLD, entry_filled_at=None)


def test_simultaneous_strategy_sells_resolve_identically_across_shuffles() -> None:
    older = BAR - timedelta(minutes=5)
    pool = [
        sell("vwap_breakdown"),
        sell("rsi_reversal"),
        sell("momentum_reversal"),
        sell("rsi_reversal", bar=older),
    ]
    baseline = run(pool)
    # Earliest completed bar wins, then strategy_id order.
    assert baseline.selected.bar_time == older
    assert baseline.selected.strategy_id == "rsi_reversal"
    rng = random.Random(7)
    for _ in range(25):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        again = run(shuffled)
        assert again == baseline
        assert again.to_dict() == baseline.to_dict()
    losers = {r.strategy_id: r for r in baseline.rejected if r.bar_time != older}
    assert all(
        r.reason_codes == (DecisionReason.SUPERSEDED_BY_TIE_BREAK,) for r in losers.values()
    )


def test_tie_break_falls_back_to_strategy_id_then_signal_id() -> None:
    a = sell("alpha_exit")
    b = sell("beta_exit")
    assert run([b, a]).selected.strategy_id == "alpha_exit"
    x = safety(ExitSource.MANUAL_CLOSE, "manual-a")
    y = safety(ExitSource.MANUAL_CLOSE, "manual-b")
    assert run([y, x]).selected.signal_id == "manual-a"


def test_flat_position_selects_nothing() -> None:
    cands = [safety(ExitSource.PROTECTIVE_STOP), sell()]
    result = run(cands, position=None)
    assert result.selected is None
    assert len(result.records) == 2
    assert all(r.reason_codes == (DecisionReason.POSITION_FLAT,) for r in result.records)


@pytest.mark.parametrize("management", [ManagementStatus.UNMANAGED, ManagementStatus.ADOPTED])
def test_unmanaged_position_selects_nothing(management: ManagementStatus) -> None:
    cands = [safety(ExitSource.MANUAL_CLOSE), sell()]
    result = run(cands, position=position(management=management))
    assert result.selected is None
    assert all(DecisionReason.NOT_MANAGED in r.reason_codes for r in result.records)


def short_position(quantity: str = "100") -> Position:
    return Position(
        account_scope_id="acct", symbol=SYMBOL, quantity=Decimal(quantity),
        avg_entry_price=Decimal("100"), cost_basis=Decimal(quantity) * Decimal("100"),
        as_of=NOW, management=ManagementStatus.MANAGED, cycle_id=CYCLE,
        direction=PositionDirection.SHORT,
    )


def cover(strategy_id: str = "cover_ema_bullish_crossover", bar: datetime = BAR) -> ExitCandidate:
    return ExitCandidate(
        signal_id=make_signal_id(strategy_id, "1.0.0", SYMBOL, StrategyRole.COVER, bar),
        source=ExitSource.STRATEGY_COVER, symbol=SYMBOL, strategy_id=strategy_id,
        version="1.0.0", position_cycle_id=CYCLE, bar_time=bar,
    )


def test_a_role_must_match_the_position_it_closes() -> None:
    # a SHORT-direction exit role against a long position selects nothing
    result = run([sell()], direction=PositionDirection.SHORT)
    assert result.selected is None
    assert DecisionReason.NOT_SHORT in result.records[0].reason_codes
    result = run([cover()], position=short_position(), direction=PositionDirection.LONG)
    assert result.selected is None
    assert DecisionReason.NOT_LONG in result.records[0].reason_codes


def test_a_sell_never_closes_a_short_and_a_cover_never_closes_a_long() -> None:
    sold = run([sell()], position=short_position())
    assert sold.selected is None
    assert DecisionReason.NOT_LONG in sold.records[0].reason_codes
    covered = run([cover()])  # default position is a long
    assert covered.selected is None
    assert DecisionReason.NOT_SHORT in covered.records[0].reason_codes


def test_cover_closes_the_whole_short_and_is_subject_to_minimum_hold() -> None:
    chosen = run([cover()], position=short_position("37"), minimum_hold=timedelta(0))
    assert chosen.selected is not None and chosen.selected.quantity == Decimal("37")
    assert chosen.selected.source is ExitSource.STRATEGY_COVER
    held = run([cover()], position=short_position(), minimum_hold=HOLD,
               entry_filled_at=NOW - timedelta(minutes=5))
    assert held.selected is None
    assert DecisionReason.MIN_HOLD_ACTIVE in held.records[0].reason_codes


def test_protective_stop_closes_a_short_and_beats_a_cover_and_ignores_minimum_hold() -> None:
    stop = safety(ExitSource.PROTECTIVE_STOP)
    result = run([cover(), stop], position=short_position(), minimum_hold=HOLD,
                 entry_filled_at=NOW - timedelta(minutes=1))
    assert result.selected is not None and result.selected.signal_id == stop.signal_id
    loser = record_for(result, cover().signal_id)
    assert loser.reason_codes[0] is DecisionReason.MIN_HOLD_ACTIVE or (
        DecisionReason.SUPERSEDED_BY_HIGHER_PRIORITY in loser.reason_codes)


def test_multiple_cover_strategies_select_exactly_one_deterministically() -> None:
    a, b, c = cover("cover_a"), cover("cover_b"), cover("cover_c")
    forward = run([a, b, c], position=short_position())
    backward = run([c, b, a], position=short_position())
    assert forward.selected is not None and backward.selected is not None
    assert forward.selected.signal_id == backward.selected.signal_id == a.signal_id
    assert sum(r.outcome is DecisionOutcome.SELECTED for r in forward.records) == 1


def test_pending_exit_rejects_overlapping_strategy_sell() -> None:
    strat = sell()
    result = run([strat], pending_exit_source=ExitSource.STRATEGY_SELL)
    assert result.selected is None
    assert record_for(result, strat.signal_id).reason_codes == (
        DecisionReason.EXIT_ALREADY_PENDING,
    )


def test_broker_held_stop_fill_in_progress_blocks_sells_and_manual() -> None:
    cands = [sell(), safety(ExitSource.MANUAL_CLOSE)]
    result = run(cands, pending_exit_source=ExitSource.PROTECTIVE_STOP)
    assert result.selected is None
    assert all(r.reason_codes == (DecisionReason.EXIT_ALREADY_PENDING,) for r in result.records)


def test_higher_priority_exit_may_supersede_pending_strategy_sell() -> None:
    stop = safety(ExitSource.PROTECTIVE_STOP)
    result = run([stop, sell()], pending_exit_source=ExitSource.STRATEGY_SELL)
    assert result.selected is not None and result.selected.signal_id == stop.signal_id


def test_selected_targets_full_confirmed_remaining_quantity_after_partial_fill() -> None:
    strat = sell()
    assert run([strat], position=position("100")).selected.quantity == Decimal("100")
    after_partial = run([strat], position=position("37"))
    assert after_partial.selected.quantity == Decimal("37")
    assert after_partial.to_dict()["records"][0]["quantity"] == "37"


def test_every_rejected_candidate_is_recorded_with_a_reason() -> None:
    seen_sell = sell("momentum_reversal")
    cands = [
        safety(ExitSource.PROTECTIVE_STOP),
        safety(ExitSource.MANUAL_CLOSE),
        sell("rsi_reversal"),
        sell("vwap_breakdown", enabled=False),
        sell("trend_exit", stale=True),
        seen_sell,
    ]
    result = run(cands, seen_signal_ids={seen_sell.signal_id}, minimum_hold=HOLD)
    assert len(result.records) == len(cands)
    assert {r.signal_id for r in result.records} == {c.signal_id for c in cands}
    assert result.selected is not None
    assert len([r for r in result.records if r.outcome is DecisionOutcome.SELECTED]) == 1
    for rec in result.rejected:
        assert rec.reason_codes
        assert DecisionReason.SELECTED not in rec.reason_codes
    reasons = {r.strategy_id: r.reason_codes for r in result.rejected if r.strategy_id}
    assert DecisionReason.STRATEGY_DISABLED in reasons["vwap_breakdown"]
    assert DecisionReason.STALE_SIGNAL in reasons["trend_exit"]
    assert DecisionReason.DUPLICATE_SIGNAL in reasons["momentum_reversal"]
    assert DecisionReason.SUPERSEDED_BY_HIGHER_PRIORITY in record_for(
        result, cands[1].signal_id
    ).reason_codes


def test_duplicate_signal_within_one_tick_is_rejected() -> None:
    strat = sell()
    result = run([strat, strat])
    assert len(result.records) == 2
    assert result.selected.signal_id == strat.signal_id
    assert [r.outcome for r in result.records].count(DecisionOutcome.REJECTED) == 1
    assert DecisionReason.DUPLICATE_SIGNAL in result.rejected[0].reason_codes


def test_stale_cycle_recommendation_is_rejected() -> None:
    old = dataclasses.replace(sell(), position_cycle_id="cycle-0")
    result = run([old])
    assert result.selected is None
    assert DecisionReason.CYCLE_MISMATCH in result.records[0].reason_codes


def test_no_candidates_yields_empty_result() -> None:
    result = run([])
    assert result.selected is None and result.records == ()


def test_decision_ids_are_deterministic_and_distinct() -> None:
    cands = [safety(ExitSource.PROTECTIVE_STOP), sell("rsi_reversal"), sell("vwap_breakdown")]
    first = run(cands)
    second = run(list(reversed(cands)))
    ids = [r.decision_id for r in first.records]
    assert ids == [r.decision_id for r in second.records]
    assert len(set(ids)) == len(ids)
    assert all(len(i) == 64 for i in ids)
    later = arbitrate_exit_decisions(
        cands,
        symbol=SYMBOL,
        position=position(),
        evaluation_time=NOW + timedelta(seconds=1),
        entry_filled_at=ENTRY,
    )
    assert not set(ids) & {r.decision_id for r in later.records}


def test_result_is_immutable_and_audit_serializable() -> None:
    result = run([safety(ExitSource.PROTECTIVE_STOP), sell()])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.selected = None  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.records[0].outcome = DecisionOutcome.REJECTED  # type: ignore[misc]
    assert isinstance(result.records, tuple)
    payload = json.loads(json.dumps(result.to_dict(), sort_keys=True))
    rec = payload["records"][0]
    for key in (
        "decision_id", "signal_id", "source", "strategy_id", "version", "symbol",
        "position_cycle_id", "outcome", "reason_codes", "decided_at", "bar_time",
    ):
        assert key in rec
    assert rec["decided_at"].endswith("Z")
    assert rec["position_cycle_id"] == CYCLE
    assert payload["selected_decision_id"] == result.selected.decision_id


def test_no_confidence_fields_anywhere() -> None:
    for cls in (ExitCandidate, DecisionRecord, ArbitrationResult, RearmState, RearmPolicy):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not {n for n in names if "confidence" in n or "weight" in n or "score" in n}
    result = run([sell()])
    assert "confidence" not in json.dumps(result.to_dict())
    assert "confidence" not in json.dumps(fresh_state().to_dict())


def test_arbitration_and_rearm_import_no_io_or_broker_modules() -> None:
    import ast

    import mike_trade.signals.decisions as decisions
    import mike_trade.signals.rearm as rearm

    banned = ("sqlite3", "socket", "requests", "httpx", "asyncio", "pathlib", "os", "time")
    for module in (decisions, rearm):
        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for name in imported:
            root = name.split(".")[0]
            assert root not in banned, (module.__name__, name)
            assert not name.startswith(("mike_trade.brokers", "mike_trade.persistence")), name


def test_invalid_candidate_shapes_are_rejected() -> None:
    with pytest.raises(ValueError):
        ExitCandidate(signal_id="s", source=ExitSource.STRATEGY_SELL, symbol=SYMBOL)
    with pytest.raises(ValueError):
        ExitCandidate(
            signal_id="s",
            source=ExitSource.MANUAL_CLOSE,
            symbol=SYMBOL,
            strategy_id="x",
            version="1.0.0",
        )
    with pytest.raises(ValueError):
        arbitrate_exit_decisions(
            [], symbol=SYMBOL, position=None, evaluation_time=datetime(2026, 1, 1)
        )


# ------------------------------------------------------------------ rearm


def fresh_state() -> RearmState:
    return RearmState(
        strategy_id="rsi_reversal", version="1.0.0", symbol=SYMBOL, side=StrategyRole.SELL
    )


def sig(bar: datetime) -> str:
    return make_signal_id("rsi_reversal", "1.0.0", SYMBOL, StrategyRole.SELL, bar)


def test_first_signal_is_accepted_and_recorded() -> None:
    state = fresh_state()
    check = check_and_record(state, signal_id=sig(BAR), bar_time=BAR)
    assert check.accepted and check.reasons == ()
    assert check.state.last_signal_id == sig(BAR)
    assert check.state.last_bar_time == BAR
    assert check.state.key == ("rsi_reversal", "1.0.0", SYMBOL, "SELL")
    assert state.last_signal_id is None  # input state untouched


def test_replay_of_same_signal_is_duplicate_and_state_unchanged() -> None:
    first = check_and_record(fresh_state(), signal_id=sig(BAR), bar_time=BAR).state
    again = check_and_record(first, signal_id=sig(BAR), bar_time=BAR)
    assert not again.accepted
    assert DecisionReason.DUPLICATE_SIGNAL in again.reasons
    assert again.state is first


def test_older_bar_is_replay_suppressed_and_later_bar_accepted() -> None:
    first = check_and_record(fresh_state(), signal_id=sig(BAR), bar_time=BAR).state
    older = BAR - timedelta(minutes=5)
    stale = check_and_record(first, signal_id=sig(older), bar_time=older)
    assert not stale.accepted and stale.reasons == (DecisionReason.REPLAY_SUPPRESSED,)
    later = BAR + timedelta(minutes=5)
    fresh = check_and_record(first, signal_id=sig(later), bar_time=later)
    assert fresh.accepted
    assert fresh.state.last_bar_time == later


def test_signal_id_must_match_key_and_bar() -> None:
    with pytest.raises(ValueError, match="does not match"):
        check_and_record(fresh_state(), signal_id="bogus", bar_time=BAR)


def test_replay_suppression_survives_simulated_restart() -> None:
    state = check_and_record(fresh_state(), signal_id=sig(BAR), bar_time=BAR).state
    state = state.with_persistent_state({"armed": False, "last_signal_bar": "2026-10-08T13:55:00Z"})
    stored = json.dumps(state.to_dict(), sort_keys=True)  # what persistence would keep
    rehydrated = RearmState.from_dict(json.loads(stored))
    assert rehydrated == state
    assert rehydrated.to_dict() == state.to_dict()
    assert rehydrated.to_json() == state.to_json()
    replay = check_and_record(rehydrated, signal_id=sig(BAR), bar_time=BAR)
    assert not replay.accepted
    assert replay.reasons == check_and_record(state, signal_id=sig(BAR), bar_time=BAR).reasons
    # A different signal id on the same bar is still a same-bar replay.
    assert rehydrated.persistent_state["armed"] is False
    later = BAR + timedelta(minutes=5)
    assert check_and_record(rehydrated, signal_id=sig(later), bar_time=later).accepted


def test_serialization_is_deterministic_and_versioned() -> None:
    a = fresh_state().with_persistent_state({"b": 1, "a": [1, {"z": 2, "y": None}]})
    b = fresh_state().with_persistent_state({"a": [1, {"y": None, "z": 2}], "b": 1})
    assert a.to_json() == b.to_json()
    data = a.to_dict()
    assert data["schema_version"] == 1
    assert RearmState.from_dict(data) == a
    with pytest.raises(ValueError, match="schema_version"):
        RearmState.from_dict({**data, "schema_version": 99})
    with pytest.raises(ValueError):
        RearmState.from_dict({**data, "side": "HOLD"})
    with pytest.raises(ValueError, match="JSON-safe"):
        fresh_state().with_persistent_state({"x": Decimal("1")})


def test_seen_window_is_bounded() -> None:
    policy = RearmPolicy(max_seen_signal_ids=2)
    state = fresh_state()
    for step in range(5):
        bar = BAR + timedelta(minutes=5 * step)
        state = check_and_record(state, signal_id=sig(bar), bar_time=bar, policy=policy).state
    assert len(state.seen_signal_ids) == 2
    oldest = BAR
    replay = check_and_record(state, signal_id=sig(oldest), bar_time=oldest, policy=policy)
    assert not replay.accepted


def test_cycle_reset_rearms_but_same_bar_replay_stays_suppressed() -> None:
    policy = RearmPolicy(disarm_on_emit=True)
    state = check_and_record(fresh_state(), signal_id=sig(BAR), bar_time=BAR, policy=policy).state
    assert state.armed is False
    later = BAR + timedelta(minutes=5)
    blocked = check_and_record(state, signal_id=sig(later), bar_time=later, policy=policy)
    assert blocked.reasons == (DecisionReason.REARM_REQUIRED,)

    new_cycle = reset_cycle(state, cycle_id="cycle-2")
    assert new_cycle.armed is True and new_cycle.cycle_id == "cycle-2"
    replay = check_and_record(new_cycle, signal_id=sig(BAR), bar_time=BAR, policy=policy)
    assert not replay.accepted
    assert DecisionReason.DUPLICATE_SIGNAL in replay.reasons
    assert DecisionReason.REPLAY_SUPPRESSED in replay.reasons
    assert check_and_record(new_cycle, signal_id=sig(later), bar_time=later, policy=policy).accepted


def test_cycle_reset_is_idempotent_and_can_replace_strategy_state() -> None:
    state = fresh_state().with_persistent_state({"armed": False})
    reset = reset_cycle(state, cycle_id="cycle-2", persistent_state={"armed": True})
    assert reset.persistent_state["armed"] is True
    assert state.persistent_state["armed"] is False
    assert reset_cycle(reset, cycle_id="cycle-2") is reset
    kept = reset_cycle(state, cycle_id="cycle-3")
    assert kept.persistent_state["armed"] is False  # preserved when not supplied


def test_reset_survives_restart_without_rearming_twice() -> None:
    policy = RearmPolicy(disarm_on_emit=True)
    state = reset_cycle(fresh_state(), cycle_id="cycle-2")
    state = check_and_record(state, signal_id=sig(BAR), bar_time=BAR, policy=policy).state
    restored = RearmState.from_dict(json.loads(json.dumps(state.to_dict())))
    again = reset_cycle(restored, cycle_id="cycle-2")  # replayed cycle-open event
    assert again.armed is False


def test_rearm_and_arbitration_compose_for_duplicate_suppression() -> None:
    strat = sell()
    state = check_and_record(fresh_state(), signal_id=strat.signal_id, bar_time=BAR).state
    result = run([strat], seen_signal_ids=set(state.seen_signal_ids))
    assert result.selected is None
    assert DecisionReason.DUPLICATE_SIGNAL in result.records[0].reason_codes
