from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.trading import Fill, OrderSide, PositionCycle, PositionCycleState
from mike_trade.risk import entry_eligibility as module
from mike_trade.risk.entry_eligibility import (
    EligibilityDecision,
    EntryBlockReason,
    EntryCycleCounter,
    EntryEligibilityEvaluator,
    LockoutResetMode,
    LosingStreakTracker,
    PostSaleCooldown,
)

D = Decimal
SCOPE = AccountScope(AccountEnvironment.PAPER, "ACCT").scope_id
OTHER = AccountScope(AccountEnvironment.SHADOW, "ACCT", "scenario-1").scope_id
THU = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)  # 10:30 ET Thursday
FRI = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)
SAT = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
MON = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


class MemoryRepository:
    """Atomic section-replacement adapter; survives 'restarts' (new components)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    def load(self, account_scope_id, section):
        return self.rows.get((account_scope_id, section))

    def save(self, account_scope_id, section, document):
        self.rows[(account_scope_id, section)] = document


def fill(fid, order="o1", side=OrderSide.BUY, qty="10", price="10", fee="0", at=THU,
         symbol="AAPL", scope=SCOPE):
    return Fill(fid, order, scope, symbol, side, D(qty), D(price), D(fee), at)


def closed_cycle(cid, exit_price, *, entry_price="10", fee="0", strategy="vwap",
                 symbol="AAPL", scope=SCOPE, closed_at=THU + timedelta(hours=1)):
    cycle = PositionCycle(cid, scope, symbol, entry_strategy_key=strategy)
    cycle = cycle.apply_entry_fill(
        fill(f"{cid}-b", f"{cid}-bo", price=entry_price, fee=fee, at=THU,
             symbol=symbol, scope=scope)
    )
    cycle = cycle.begin_exit().apply_exit_fill(
        fill(f"{cid}-s", f"{cid}-so", OrderSide.SELL, price=exit_price, fee=fee,
             at=closed_at, symbol=symbol, scope=scope)
    )
    assert cycle.state is PositionCycleState.CLOSED
    return cycle


def closed_at(cycle: PositionCycle) -> datetime:
    assert cycle.closed_at is not None
    return cycle.closed_at


def build(repo=None, *, cycles=3, threshold: int | None = 2, mode=LockoutResetMode.NEXT_SESSION,
          by_strategy=False, cooldown=0, max_open=None, daily=None, now=THU):
    clock = lambda: now  # noqa: E731
    counter = EntryCycleCounter(cycles, repo, clock)
    tracker = LosingStreakTracker(
        threshold, mode, scope_by_strategy=by_strategy, repository=repo, clock=clock
    )
    cool = PostSaleCooldown(cooldown, repo, clock)
    evaluator = EntryEligibilityEvaluator(
        counter, tracker, cool, max_open_positions=max_open, daily_loss_limit=daily, clock=clock
    )
    return counter, tracker, cool, evaluator


# ------------------------------------------------------------------ entry cycle limit


def test_first_positive_fill_consumes_one_slot_and_partials_consume_nothing() -> None:
    async def go():
        counter, *_ = build()
        assert await counter.record_entry_fill(fill("f1", qty="3")) is True
        assert await counter.record_entry_fill(fill("f2", qty="7")) is False  # same entry
        assert await counter.record_entry_fill(fill("f1", qty="3")) is False  # replay
        assert await counter.count(SCOPE, "AAPL", now=THU) == 1
        assert await counter.record_entry_fill(fill("f3", order="o2")) is True
        assert await counter.count(SCOPE, "aapl", now=THU) == 2

    run(go())


def test_unfilled_entries_consume_nothing_and_sells_are_ignored() -> None:
    async def go():
        counter, _, _, evaluator = build(cycles=1)
        # A rejected/canceled/expired entry never produces a fill, so nothing is recorded.
        assert (await evaluator.evaluate(SCOPE, "AAPL")).allowed
        assert await counter.record_entry_fill(fill("s1", side=OrderSide.SELL)) is False
        assert await counter.count(SCOPE, "AAPL", now=THU) == 0

    run(go())


def test_cycle_limit_blocks_then_resets_next_session_and_is_symbol_and_scope_scoped() -> None:
    async def go():
        counter, _, _, evaluator = build(cycles=1)
        await counter.record_entry_fill(fill("f1"))
        blocked = await evaluator.evaluate(SCOPE, "AAPL")
        assert blocked.reasons == (EntryBlockReason.MAX_ENTRY_CYCLES_REACHED,)
        assert (await evaluator.evaluate(SCOPE, "MSFT")).allowed
        assert (await evaluator.evaluate(OTHER, "AAPL")).allowed
        assert (await evaluator.evaluate(SCOPE, "AAPL", now=FRI)).allowed

    run(go())


def test_session_boundary_uses_new_york_time_and_weekend_keeps_prior_session() -> None:
    async def go():
        counter, *_ = build(cycles=1)
        # 2026-10-09 02:00 UTC is still Thursday 22:00 ET: same session as Thursday.
        late = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)
        await counter.record_entry_fill(fill("f1", at=THU))
        assert await counter.count(SCOPE, "AAPL", now=late) == 1
        # Saturday belongs to Friday's session, Monday is a fresh one.
        await counter.record_entry_fill(fill("f2", order="o2", at=FRI))
        assert await counter.count(SCOPE, "AAPL", now=SAT) == 1
        assert await counter.count(SCOPE, "AAPL", now=MON) == 0
        assert await counter.count(SCOPE, "AAPL", session_date=date(2026, 10, 9)) == 1

    run(go())


def test_entry_that_fills_across_sessions_is_one_cycle() -> None:
    async def go():
        counter, *_ = build()
        await counter.record_entry_fill(fill("f1", at=THU))
        assert await counter.record_entry_fill(fill("f2", at=FRI)) is False
        assert await counter.count(SCOPE, "AAPL", now=FRI) == 0

    run(go())


def test_counts_survive_restart() -> None:
    async def go():
        repo = MemoryRepository()
        counter, *_ = build(repo, cycles=1)
        await counter.record_entry_fill(fill("f1"))
        restarted, _, _, evaluator = build(repo, cycles=1)
        assert await restarted.count(SCOPE, "AAPL", now=THU) == 1
        assert (await evaluator.evaluate(SCOPE, "AAPL")).blocked
        assert await restarted.record_entry_fill(fill("f1")) is False

    run(go())


# ----------------------------------------------------------------- losing streak lockout


def test_loss_increments_win_resets_breakeven_unchanged() -> None:
    async def go():
        _, tracker, *_ = build(threshold=5)
        await tracker.record_closed_cycle(closed_cycle("c1", "9"))
        await tracker.record_closed_cycle(closed_cycle("c2", "9"))
        assert await tracker.streak(SCOPE, "AAPL", now=THU) == 2
        await tracker.record_closed_cycle(closed_cycle("c3", "10"))  # exact breakeven
        assert await tracker.streak(SCOPE, "AAPL", now=THU) == 2
        await tracker.record_closed_cycle(closed_cycle("c4", "11"))
        assert await tracker.streak(SCOPE, "AAPL", now=THU) == 0

    run(go())


def test_fees_are_included_in_net_pnl() -> None:
    async def go():
        _, tracker, *_ = build(threshold=5)
        # Gross +0.00 at 10 -> 10, fees 0.05 each side make it a net loss.
        await tracker.record_closed_cycle(closed_cycle("c1", "10", fee="0.05"))
        assert await tracker.streak(SCOPE, "AAPL", now=THU) == 1
        assert await tracker.daily_realized_pnl(SCOPE, now=THU) == D("-0.10")

    run(go())


def test_lockout_triggers_at_threshold_and_is_idempotent() -> None:
    async def go():
        _, tracker, _, evaluator = build(threshold=2)
        cycle = closed_cycle("c1", "9")
        assert await tracker.record_closed_cycle(cycle) is True
        assert await tracker.record_closed_cycle(cycle) is False
        assert (await evaluator.evaluate(SCOPE, "AAPL")).allowed
        await tracker.record_closed_cycle(closed_cycle("c2", "9"))
        await tracker.record_closed_cycle(closed_cycle("c2", "9"))
        assert await tracker.streak(SCOPE, "AAPL", now=THU) == 2
        decision = await evaluator.evaluate(SCOPE, "AAPL")
        assert decision.reasons == (EntryBlockReason.LOSING_STREAK_LOCKOUT,)
        assert not decision.provisional

    run(go())


def test_next_session_mode_releases_automatically_with_fresh_streak() -> None:
    async def go():
        _, tracker, _, evaluator = build(threshold=1, mode=LockoutResetMode.NEXT_SESSION)
        await tracker.record_closed_cycle(closed_cycle("c1", "9"))
        assert (await evaluator.evaluate(SCOPE, "AAPL", now=THU + timedelta(hours=5))).blocked
        assert (await evaluator.evaluate(SCOPE, "AAPL", now=FRI)).allowed
        assert await tracker.streak(SCOPE, "AAPL", now=FRI) == 0
        # A new loss in the next session starts a streak of one, then locks again.
        later = FRI + timedelta(hours=1)
        await tracker.record_closed_cycle(closed_cycle("c2", "9", closed_at=later))
        assert (await evaluator.evaluate(SCOPE, "AAPL", now=FRI + timedelta(hours=2))).blocked

    run(go())


def test_manual_mode_persists_across_sessions_until_audited_reset() -> None:
    async def go():
        repo = MemoryRepository()
        _, tracker, _, evaluator = build(repo, threshold=1, mode=LockoutResetMode.MANUAL)
        await tracker.record_closed_cycle(closed_cycle("c1", "9"))
        assert (await evaluator.evaluate(SCOPE, "AAPL", now=MON)).blocked

        # A restart cannot clear it either.
        _, tracker2, _, evaluator2 = build(repo, threshold=1, mode=LockoutResetMode.MANUAL)
        assert (await evaluator2.evaluate(SCOPE, "AAPL", now=MON)).blocked

        event = await tracker2.reset_lockout(
            SCOPE, "AAPL", reset_id="r1", operator="mike", reason="reviewed", now=MON
        )
        assert event["operator"] == "mike" and event["reason"] == "reviewed"
        assert event["reset_at"] == MON.isoformat() and event["was_locked"] is True
        assert (await evaluator2.evaluate(SCOPE, "AAPL", now=MON)).allowed

    run(go())


def test_reset_requires_audit_fields_and_is_append_only_and_idempotent() -> None:
    async def go():
        _, tracker, *_ = build(threshold=1, mode=LockoutResetMode.MANUAL)
        await tracker.record_closed_cycle(closed_cycle("c1", "9"))
        for kwargs in (
            {"operator": "", "reason": "x"},
            {"operator": "mike", "reason": "  "},
        ):
            with pytest.raises(ValueError):
                await tracker.reset_lockout(SCOPE, "AAPL", reset_id="r1", now=MON, **kwargs)
        assert await tracker.reset_events(SCOPE) == ()
        with pytest.raises(ValueError):
            await tracker.reset_lockout(
                SCOPE, "AAPL", reset_id="r1", operator="m", reason="x", now=datetime(2026, 1, 1)
            )

        first = await tracker.reset_lockout(
            SCOPE, "AAPL", reset_id="r1", operator="mike", reason="ok", now=MON
        )
        again = await tracker.reset_lockout(
            SCOPE, "AAPL", reset_id="r1", operator="other", reason="changed", now=MON
        )
        assert again == first
        # Relock, reset again: earlier events are retained, in order.
        await tracker.record_closed_cycle(closed_cycle("c2", "9", closed_at=MON))
        await tracker.reset_lockout(
            SCOPE, "AAPL", reset_id="r2", operator="mike", reason="again", now=MON
        )
        events = await tracker.reset_events(SCOPE)
        assert [e["reset_id"] for e in events] == ["r1", "r2"]
        assert events[0] == first

    run(go())


def test_lockout_scope_per_symbol_and_optionally_per_strategy() -> None:
    async def go():
        _, tracker, _, evaluator = build(threshold=1, by_strategy=False)
        await tracker.record_closed_cycle(closed_cycle("c1", "9", strategy="vwap"))
        assert (await evaluator.evaluate(SCOPE, "AAPL", strategy_key="momentum")).blocked
        assert (await evaluator.evaluate(SCOPE, "MSFT")).allowed

        _, tracker, _, evaluator = build(threshold=1, by_strategy=True)
        await tracker.record_closed_cycle(closed_cycle("c1", "9", strategy="vwap"))
        assert (await evaluator.evaluate(SCOPE, "AAPL", strategy_key="vwap")).blocked
        assert (await evaluator.evaluate(SCOPE, "AAPL", strategy_key="momentum")).allowed

    run(go())


def test_estimated_or_unavailable_fees_are_recorded_provisional() -> None:
    async def go():
        repo = MemoryRepository()
        _, tracker, _, evaluator = build(repo, threshold=1)
        await tracker.record_closed_cycle(closed_cycle("c1", "9"), fee_status="estimated")
        decision = await evaluator.evaluate(SCOPE, "AAPL")
        assert decision.blocked and decision.provisional
        assert "provisional" in decision.details[0]
        stored = repo.rows[(SCOPE, "losing_streaks")]["cycles"]["c1"]
        assert stored["provisional"] is True and stored["fee_status"] == "estimated"
        await tracker.record_closed_cycle(closed_cycle("c2", "9"), fee_status="unavailable")
        assert repo.rows[(SCOPE, "losing_streaks")]["cycles"]["c2"]["fee_status"] == "unavailable"
        with pytest.raises(ValueError):
            await tracker.record_closed_cycle(closed_cycle("c3", "9"), fee_status="guess")

    run(go())


def test_final_loss_is_not_marked_provisional() -> None:
    async def go():
        repo = MemoryRepository()
        _, tracker, *_ = build(repo, threshold=1)
        await tracker.record_closed_cycle(closed_cycle("c1", "9"))
        assert repo.rows[(SCOPE, "losing_streaks")]["cycles"]["c1"]["provisional"] is False

    run(go())


def test_open_cycle_is_not_a_closed_result() -> None:
    async def go():
        _, tracker, *_ = build()
        open_cycle = PositionCycle("c1", SCOPE, "AAPL").apply_entry_fill(fill("f1"))
        with pytest.raises(ValueError):
            await tracker.record_closed_cycle(open_cycle)

    run(go())


# ------------------------------------------------------------------- post-sale cooldown


def test_cooldown_measured_from_final_closing_fill_and_survives_restart() -> None:
    async def go():
        repo = MemoryRepository()
        _, _, cool, evaluator = build(repo, cooldown=300, threshold=None)
        cycle = closed_cycle("c1", "11", closed_at=THU + timedelta(hours=1))
        assert await cool.record_closed_cycle(cycle) is True
        assert await cool.record_closed_cycle(cycle) is False
        closed = closed_at(cycle)

        _, _, _, restarted = build(repo, cooldown=300, threshold=None)
        inside = await restarted.evaluate(SCOPE, "AAPL", now=closed + timedelta(seconds=299))
        assert inside.reasons == (EntryBlockReason.POST_SALE_COOLDOWN,)
        assert inside.cooldown_until == closed + timedelta(seconds=300)
        expired = await restarted.evaluate(SCOPE, "AAPL", now=closed + timedelta(seconds=300))
        assert expired.allowed
        assert (await evaluator.evaluate(SCOPE, "MSFT", now=closed)).allowed

    run(go())


def test_cooldown_uses_latest_exit_and_ignores_stale_replays() -> None:
    async def go():
        _, _, cool, _ = build(cooldown=60)
        assert await cool.record_final_exit(SCOPE, "AAPL", "x2", THU + timedelta(minutes=10))
        assert not await cool.record_final_exit(SCOPE, "AAPL", "x1", THU)
        assert await cool.cooldown_until(SCOPE, "AAPL") == THU + timedelta(minutes=11)

    run(go())


def test_zero_cooldown_never_blocks() -> None:
    async def go():
        _, _, cool, evaluator = build(cooldown=0)
        await cool.record_final_exit(SCOPE, "AAPL", "x1", THU)
        assert (await evaluator.evaluate(SCOPE, "AAPL")).allowed

    run(go())


# ----------------------------------------------------- account guards and combined result


def test_daily_loss_guard_uses_ny_session_and_blocks_all_symbols() -> None:
    async def go():
        _, tracker, _, evaluator = build(threshold=None, daily=D("15"))
        await tracker.record_closed_cycle(closed_cycle("c1", "9"))  # -10
        assert (await evaluator.evaluate(SCOPE, "MSFT")).allowed
        await tracker.record_closed_cycle(closed_cycle("c2", "8", symbol="MSFT"))  # -20 more
        decision = await evaluator.evaluate(SCOPE, "TSLA")
        assert decision.reasons == (EntryBlockReason.DAILY_LOSS_LIMIT_REACHED,)
        assert (await evaluator.evaluate(SCOPE, "TSLA", now=FRI)).allowed
        assert (await evaluator.evaluate(OTHER, "TSLA")).allowed

    run(go())


def test_max_open_positions() -> None:
    async def go():
        *_, evaluator = build(max_open=2)
        assert (await evaluator.evaluate(SCOPE, "AAPL", open_position_count=1)).allowed
        decision = await evaluator.evaluate(SCOPE, "AAPL", open_position_count=2)
        assert decision.reasons == (EntryBlockReason.MAX_OPEN_POSITIONS_REACHED,)
        with pytest.raises(TypeError):
            await evaluator.evaluate(SCOPE, "AAPL")  # fail closed: count is required

    run(go())


def test_decision_lists_every_blocking_reason() -> None:
    async def go():
        counter, tracker, cool, evaluator = build(
            cycles=1, threshold=1, cooldown=3600, max_open=1, daily=D("5")
        )
        await counter.record_entry_fill(fill("f1"))
        cycle = closed_cycle("c1", "9")
        await tracker.record_closed_cycle(cycle)
        await cool.record_closed_cycle(cycle)
        decision = await evaluator.evaluate(
            SCOPE, "AAPL", open_position_count=1, now=closed_at(cycle) + timedelta(minutes=1)
        )
        assert isinstance(decision, EligibilityDecision)
        assert set(decision.reasons) == set(EntryBlockReason)
        assert len(decision.details) == len(decision.reasons) == 5
        assert decision.session_date == date(2026, 10, 8) and decision.blocked

    run(go())


def test_money_rejects_float_and_naive_times() -> None:
    with pytest.raises(TypeError):
        EntryEligibilityEvaluator(
            *build()[:3], daily_loss_limit=10.0  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        EntryEligibilityEvaluator(*build()[:3], daily_loss_limit=D("0"))
    with pytest.raises(ValueError):
        run(build()[3].evaluate(SCOPE, "AAPL", now=datetime(2026, 10, 8, 14, 30)))
    with pytest.raises(ValueError):
        run(build()[2].record_final_exit(SCOPE, "AAPL", "x", datetime(2026, 10, 8, 14, 30)))
    with pytest.raises(ValueError):
        run(build()[3].evaluate("ACCT", "AAPL"))  # not environment-qualified


# ------------------------------------------------------- entry-only: exits unaffected


def test_blocked_entries_never_block_ingestion_or_exit_paths() -> None:
    async def go():
        counter, tracker, cool, evaluator = build(
            cycles=1, threshold=1, mode=LockoutResetMode.MANUAL, cooldown=3600, max_open=1,
            daily=D("1"),
        )
        await counter.record_entry_fill(fill("f1"))
        await tracker.record_closed_cycle(closed_cycle("c0", "9"))
        assert (await evaluator.evaluate(SCOPE, "AAPL", open_position_count=1)).blocked

        # Fill ingestion, exits and closes continue to work while entries are blocked.
        assert await counter.record_entry_fill(fill("f2", order="o2")) is True  # still recorded
        assert await counter.count(SCOPE, "AAPL", now=THU) == 2
        cycle = PositionCycle("c9", SCOPE, "AAPL", entry_strategy_key="vwap")
        cycle = cycle.apply_entry_fill(fill("f9", order="o9"))
        cycle = cycle.begin_exit()
        cycle = cycle.apply_exit_fill(fill("s9", "so9", OrderSide.SELL, price="9", at=THU))
        assert cycle.state is PositionCycleState.CLOSED
        assert await tracker.record_closed_cycle(cycle) is True
        assert await cool.record_closed_cycle(cycle) is True
        assert (await evaluator.evaluate(SCOPE, "AAPL", open_position_count=1)).blocked

    run(go())


def test_module_exposes_no_exit_or_protection_gating_api() -> None:
    forbidden_params = {"purpose", "order_purpose", "order", "intent", "stop_price"}
    forbidden_words = ("exit", "stop", "protect", "reconcil", "manual_close", "gate", "ingest")
    for name in module.__all__:
        assert not any(word in name.lower() for word in forbidden_words), name
        obj = getattr(module, name)
        if not inspect.isclass(obj):
            continue
        for member_name, member in inspect.getmembers(obj, callable):
            if member_name.startswith("_"):
                continue
            assert not any(
                word in member_name.lower() for word in ("protect", "reconcil", "manual_close")
            ), member_name
            assert not forbidden_params & set(inspect.signature(member).parameters), member_name
    # Only `evaluate` (entry path) returns a block decision.
    deciders = [
        f"{cls}.{m}"
        for cls in module.__all__
        if inspect.isclass(obj := getattr(module, cls))
        for m, fn in inspect.getmembers(obj, inspect.isfunction)
        if not m.startswith("_")
        and inspect.signature(fn).return_annotation in ("EligibilityDecision", EligibilityDecision)
    ]
    assert deciders == ["EntryEligibilityEvaluator.evaluate"]
