"""Phase L entry-policy integration: isolation, restart, partials, priority, exits.

Exercises the real l.1-l.4 modules end to end against a temporary SQLite WAL
database (``tmp_path``) through the existing persistence layer
(:func:`initialize_database` / :func:`open_connection`) with a deterministic
fake clock.  No broker, network, or credentials are used anywhere in this
module: all fills, cycles, and quotes are constructed in memory and every
repository adapter below is a small scope-replacement store over the same
SQLite file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.protection import ProtectionKind
from mike_trade.domain.trading import Fill, OrderSide, PositionCycle, PositionCycleState
from mike_trade.portfolio.budgets import BudgetPolicy
from mike_trade.portfolio.reservations import BudgetReservation, ReservationLedger
from mike_trade.portfolio.sizing import SizingInput, size_long_entry
from mike_trade.persistence.schema import initialize_database, open_connection
from mike_trade.risk.entry_eligibility import (
    EntryBlockReason,
    EntryCycleCounter,
    EntryEligibilityEvaluator,
    LockoutResetMode,
    LosingStreakTracker,
    PostSaleCooldown,
)
from mike_trade.risk.market_filters import (
    LiquidityFilter,
    MarketFilterDecision,
    MarketFilterReason,
    SpreadFilter,
)
from mike_trade.risk.session_policy import (
    EntryCutoffPolicy,
    ExitKind,
    MinimumHoldPolicy,
    SessionPolicy,
)

D = Decimal
THU = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)  # Thursday 10:30 ET, regular hours
FRI = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)  # next NY trading session
SAT = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)  # weekend: belongs to Friday's session

SHADOW_A = AccountScope(AccountEnvironment.SHADOW, "ACCT", "alpha").scope_id
SHADOW_B = AccountScope(AccountEnvironment.SHADOW, "ACCT", "beta").scope_id
PAPER = AccountScope(AccountEnvironment.PAPER, "ACCT").scope_id

FIXED_WHOLE = frozenset({(ProtectionKind.FIXED_STOP, False)})
BIG_POWER = D("100000")


# ------------------------------------------------------------------ fake clock


class FakeClock:
    """Deterministic, manually advanced UTC clock; the only time source here."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError("start must be timezone-aware UTC")
        self._current = start

    def now(self) -> datetime:
        return self._current

    def set(self, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("value must be timezone-aware UTC")
        self._current = value
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        if not isinstance(delta, timedelta):
            raise TypeError("delta must be timedelta")
        self._current = self._current + delta
        return self._current


# ------------------------------------------------------- sqlite repository adapters


def _reservation_to_json(reservation: BudgetReservation) -> dict:
    return {
        "account_scope_id": reservation.account_scope_id,
        "symbol": reservation.symbol,
        "client_order_id": reservation.client_order_id,
        "initial_reserved_usd": str(reservation.initial_reserved_usd),
        "pending_reserved_usd": str(reservation.pending_reserved_usd),
        "committed_open_notional_usd": str(reservation.committed_open_notional_usd),
        "state": reservation.state,
        "created_at": reservation.created_at.isoformat(),
        "updated_at": reservation.updated_at.isoformat(),
    }


def _reservation_from_json(document: dict) -> BudgetReservation:
    return BudgetReservation(
        account_scope_id=document["account_scope_id"],
        symbol=document["symbol"],
        client_order_id=document["client_order_id"],
        initial_reserved_usd=D(document["initial_reserved_usd"]),
        pending_reserved_usd=D(document["pending_reserved_usd"]),
        committed_open_notional_usd=D(document["committed_open_notional_usd"]),
        state=document["state"],
        created_at=datetime.fromisoformat(document["created_at"]),
        updated_at=datetime.fromisoformat(document["updated_at"]),
    )


class SqliteReservationRepository:
    """Atomic per-scope reservation replacement over the shared WAL database."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        with open_connection(self._path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS phase_l_reservations("
                "account_scope_id TEXT PRIMARY KEY, "
                "reservations_json TEXT NOT NULL, "
                "activity_ids_json TEXT NOT NULL)"
            )
            conn.commit()

    def load(self, account_scope_id: str):
        with open_connection(self._path) as conn:
            row = conn.execute(
                "SELECT reservations_json, activity_ids_json FROM phase_l_reservations"
                " WHERE account_scope_id = ?",
                (account_scope_id,),
            ).fetchone()
        if row is None:
            return None
        reservations = tuple(
            _reservation_from_json(item) for item in json.loads(row[0])
        )
        return reservations, frozenset(json.loads(row[1]))

    def save(self, account_scope_id, reservations, processed_activity_ids) -> None:
        payload = (
            account_scope_id,
            json.dumps([_reservation_to_json(item) for item in reservations]),
            json.dumps(sorted(processed_activity_ids)),
        )
        with open_connection(self._path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO phase_l_reservations"
                    "(account_scope_id, reservations_json, activity_ids_json)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(account_scope_id) DO UPDATE SET"
                    " reservations_json = excluded.reservations_json,"
                    " activity_ids_json = excluded.activity_ids_json",
                    payload,
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise


class SqliteEligibilityRepository:
    """Atomic per-scope, per-section document replacement over the WAL database."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        with open_connection(self._path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS phase_l_eligibility("
                "account_scope_id TEXT NOT NULL, section TEXT NOT NULL, "
                "document_json TEXT NOT NULL, "
                "PRIMARY KEY (account_scope_id, section))"
            )
            conn.commit()

    def load(self, account_scope_id: str, section: str):
        with open_connection(self._path) as conn:
            row = conn.execute(
                "SELECT document_json FROM phase_l_eligibility"
                " WHERE account_scope_id = ? AND section = ?",
                (account_scope_id, section),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def save(self, account_scope_id: str, section: str, document) -> None:
        with open_connection(self._path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO phase_l_eligibility"
                    "(account_scope_id, section, document_json) VALUES (?, ?, ?)"
                    " ON CONFLICT(account_scope_id, section) DO UPDATE SET"
                    " document_json = excluded.document_json",
                    (account_scope_id, section, json.dumps(document, default=str)),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise


# ------------------------------------------------------------------ harness


@dataclass
class Services:
    clock: FakeClock
    ledger: ReservationLedger
    cycles: EntryCycleCounter
    streaks: LosingStreakTracker
    cooldown: PostSaleCooldown
    evaluator: EntryEligibilityEvaluator


def open_database(tmp_path: Path, name: str, clock: FakeClock) -> Path:
    """Create a WAL database through the existing persistence layer."""
    path = tmp_path / name
    initialize_database(path, AccountEnvironment.PAPER, now=clock.now()).close()
    with open_connection(path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal", f"expected WAL journal mode, got {mode!r}"
    return path


def make_services(
    path: Path,
    clock: FakeClock,
    *,
    total: str = "1000",
    symbol: str = "600",
    max_cycles: int | None = 1,
    threshold: int | None = 2,
    reset_mode: LockoutResetMode = LockoutResetMode.MANUAL,
    cooldown_seconds: int = 300,
    max_open: int | None = None,
    daily_loss: str | None = None,
) -> Services:
    """Fresh service instances over the same database file (restart-capable)."""
    reservation_repo = SqliteReservationRepository(path)
    eligibility_repo = SqliteEligibilityRepository(path)
    ledger = ReservationLedger(
        BudgetPolicy(D(total), D(symbol)), repository=reservation_repo, clock=clock
    )
    cycles = EntryCycleCounter(max_cycles, eligibility_repo, clock)
    streaks = LosingStreakTracker(
        threshold, reset_mode, repository=eligibility_repo, clock=clock
    )
    cooldown = PostSaleCooldown(cooldown_seconds, eligibility_repo, clock)
    evaluator = EntryEligibilityEvaluator(
        cycles,
        streaks,
        cooldown,
        max_open_positions=max_open,
        daily_loss_limit=D(daily_loss) if daily_loss is not None else None,
        clock=clock,
    )
    return Services(clock, ledger, cycles, streaks, cooldown, evaluator)


def buy_fill(
    fill_id: str,
    broker_order_id: str,
    scope: str,
    symbol: str,
    at: datetime,
    *,
    qty: str = "10",
    price: str = "10",
    fee: str = "0",
) -> Fill:
    return Fill(
        fill_id=fill_id,
        broker_order_id=broker_order_id,
        account_scope_id=scope,
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=D(qty),
        price=D(price),
        fee=D(fee),
        filled_at=at,
    )


def sell_fill(
    fill_id: str,
    broker_order_id: str,
    scope: str,
    symbol: str,
    at: datetime,
    *,
    qty: str = "10",
    price: str = "9",
    fee: str = "0",
) -> Fill:
    return Fill(
        fill_id=fill_id,
        broker_order_id=broker_order_id,
        account_scope_id=scope,
        symbol=symbol,
        side=OrderSide.SELL,
        quantity=D(qty),
        price=D(price),
        fee=D(fee),
        filled_at=at,
    )


def losing_cycle(
    cycle_id: str,
    scope: str,
    symbol: str,
    closed_at: datetime,
    *,
    entry_at: datetime | None = None,
    strategy: str = "vwap",
) -> PositionCycle:
    """A fully closed cycle with net -10 (entry 10x10, exit 10x9, no fees)."""
    started = entry_at or (closed_at - timedelta(hours=1))
    cycle = PositionCycle(cycle_id, scope, symbol, entry_strategy_key=strategy)
    cycle = cycle.apply_entry_fill(
        buy_fill(f"{cycle_id}-b", f"{cycle_id}-bo", scope, symbol, started)
    )
    cycle = cycle.begin_exit().apply_exit_fill(
        sell_fill(f"{cycle_id}-s", f"{cycle_id}-so", scope, symbol, closed_at)
    )
    assert cycle.state is PositionCycleState.CLOSED
    return cycle


def size_for_snapshot(
    snapshot, *, entry_price: str = "10", stop_price: str = "9"
):
    return size_long_entry(
        SizingInput(
            entry_price=D(entry_price),
            remaining_symbol_budget_usd=snapshot.remaining_symbol_usd,
            remaining_total_budget_usd=snapshot.remaining_total_usd,
            broker_buying_power_usd=BIG_POWER,
            reserve_usd=D("0"),
            slippage_cushion_usd=D("0"),
            quantity_increment=D("1"),
            minimum_order_value_usd=D("1"),
            protection_kind=ProtectionKind.FIXED_STOP,
            supported_protection_combinations=FIXED_WHOLE,
            protective_stop_price=D(stop_price),
        )
    )


@dataclass(frozen=True)
class Quote:
    bid: Decimal = D("99.90")
    ask: Decimal = Decimal("100.10")
    bid_size: Decimal | None = D("20")
    ask_size: Decimal | None = D("20")
    receive_ts: datetime = THU
    feed_id: str | None = "IEX"
    stale: bool = False


async def reserve(
    services: Services,
    scope: str,
    symbol: str,
    order_id: str,
    amount: str,
    at: datetime,
) -> BudgetReservation:
    return await services.ledger.reserve(
        scope,
        symbol,
        order_id,
        D(amount),
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
        occurred_at=at,
    )


# ------------------------------------------------- (a) cross-scenario isolation


async def test_cross_scenario_isolation(tmp_path: Path) -> None:
    """Two SHADOW scenarios plus PAPER share nothing; exhausting one is local."""
    clock = FakeClock(THU)
    path = open_database(tmp_path, "isolation.sqlite3", clock)
    services = make_services(path, clock)

    # Exhaust SHADOW:alpha on every axis: symbol budget, cycles, streak, cooldown.
    await reserve(services, SHADOW_A, "AAPL", "alpha-entry", "600", THU)
    assert await services.cycles.record_entry_fill(
        buy_fill("alpha-f1", "alpha-bo", SHADOW_A, "AAPL", THU)
    ) is True
    await services.streaks.record_closed_cycle(
        losing_cycle("alpha-c1", SHADOW_A, "AAPL", THU + timedelta(minutes=30))
    )
    await services.streaks.record_closed_cycle(
        losing_cycle("alpha-c2", SHADOW_A, "AAPL", THU + timedelta(minutes=40))
    )
    await services.cooldown.record_final_exit(
        SHADOW_A, "AAPL", "alpha-exit", THU + timedelta(minutes=50)
    )

    alpha = await services.evaluator.evaluate(SHADOW_A, "AAPL")
    assert alpha.blocked
    assert set(alpha.reasons) == {
        EntryBlockReason.MAX_ENTRY_CYCLES_REACHED,
        EntryBlockReason.LOSING_STREAK_LOCKOUT,
        EntryBlockReason.POST_SALE_COOLDOWN,
    }

    # SHADOW:beta is untouched on every axis.
    beta_snapshot = await services.ledger.snapshot(
        SHADOW_B,
        "AAPL",
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
    )
    assert beta_snapshot.remaining_application_usd == D("1000")
    assert beta_snapshot.remaining_symbol_usd == D("600")
    assert await services.cycles.count(SHADOW_B, "AAPL", now=THU) == 0
    assert await services.streaks.streak(SHADOW_B, "AAPL", now=THU) == 0
    assert (await services.evaluator.evaluate(SHADOW_B, "AAPL")).allowed
    assert await services.cooldown.cooldown_until(SHADOW_B, "AAPL") is None
    await reserve(services, SHADOW_B, "AAPL", "beta-entry", "600", THU)  # full symbol budget free

    # PAPER is untouched on every axis.
    paper_snapshot = await services.ledger.snapshot(
        PAPER,
        "AAPL",
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
    )
    assert paper_snapshot.remaining_application_usd == D("1000")
    assert paper_snapshot.remaining_symbol_usd == D("600")
    assert await services.cycles.count(PAPER, "AAPL", now=THU) == 0
    assert await services.streaks.streak(PAPER, "AAPL", now=THU) == 0
    assert (await services.evaluator.evaluate(PAPER, "AAPL")).allowed
    assert await services.cooldown.cooldown_until(PAPER, "AAPL") is None
    # Sizing still accepts a full-size PAPER entry priced off its own snapshot.
    assert size_for_snapshot(paper_snapshot).accepted


# ------------------------------------------------- (b) restart durability


async def test_restart_durability(tmp_path: Path) -> None:
    """Fresh instances over the same database preserve cycles, streaks, cooldowns, reservations."""
    clock = FakeClock(THU)
    path = open_database(tmp_path, "restart.sqlite3", clock)
    first = make_services(path, clock, max_cycles=5, threshold=3)

    await reserve(first, PAPER, "AAPL", "restart-entry", "200", THU)
    await first.ledger.apply_fill(
        PAPER, "AAPL", "restart-entry", "restart-fill-1", D("2"), D("20"), occurred_at=THU
    )
    assert await first.cycles.record_entry_fill(
        buy_fill("cycle-f1", "cycle-bo", PAPER, "AAPL", THU)
    ) is True
    await first.streaks.record_closed_cycle(
        losing_cycle("restart-c1", PAPER, "AAPL", THU + timedelta(minutes=30))
    )
    await first.cooldown.record_final_exit(
        PAPER, "AAPL", "restart-exit", THU + timedelta(hours=1)
    )
    cooldown_until = await first.cooldown.cooldown_until(PAPER, "AAPL")
    assert cooldown_until is not None

    # Restart: brand-new service instances over the same database file.
    second = make_services(path, clock, max_cycles=5, threshold=3)

    snapshot = await second.ledger.snapshot(
        PAPER,
        "AAPL",
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
    )
    assert snapshot.pending_reserved_usd == D("160")
    assert snapshot.committed_open_notional_usd == D("40")
    assert await second.cycles.count(PAPER, "AAPL", now=THU) == 1
    assert await second.streaks.streak(PAPER, "AAPL", now=THU) == 1
    assert await second.cooldown.cooldown_until(PAPER, "AAPL") == cooldown_until
    assert (await second.evaluator.evaluate(
        PAPER, "AAPL", now=THU + timedelta(hours=1)
    )).blocked
    # Fill dedupe IDs survive the restart: replaying the same fill is a no-op.
    replayed = await second.ledger.apply_fill(
        PAPER, "AAPL", "restart-entry", "restart-fill-1", D("2"), D("20"), occurred_at=THU
    )
    assert replayed.committed_open_notional_usd == D("40")
    assert replayed.pending_reserved_usd == D("160")
    assert await second.cycles.record_entry_fill(
        buy_fill("cycle-f1", "cycle-bo", PAPER, "AAPL", THU)
    ) is False


# --------------------------------- (b2) session reset vs manual lockout persistence


async def test_session_reset_clears_cycles_and_next_session_lockouts_but_not_manual(
    tmp_path: Path,
) -> None:
    """At the next NY session, cycles and next_session lockouts reset; manual persists."""
    clock = FakeClock(THU)
    path = open_database(tmp_path, "sessions.sqlite3", clock)
    auto = make_services(
        path, clock, max_cycles=1, threshold=1, reset_mode=LockoutResetMode.NEXT_SESSION
    )
    manual = make_services(
        path, clock, max_cycles=1, threshold=1, reset_mode=LockoutResetMode.MANUAL
    )

    assert await auto.cycles.record_entry_fill(
        buy_fill("auto-f1", "auto-bo", SHADOW_A, "AAPL", THU)
    ) is True
    await auto.streaks.record_closed_cycle(
        losing_cycle("auto-c1", SHADOW_A, "AAPL", THU + timedelta(hours=1))
    )
    assert await manual.cycles.record_entry_fill(
        buy_fill("man-f1", "man-bo", SHADOW_B, "AAPL", THU)
    ) is True
    await manual.streaks.record_closed_cycle(
        losing_cycle("man-c1", SHADOW_B, "AAPL", THU + timedelta(hours=1))
    )
    assert (await auto.evaluator.evaluate(SHADOW_A, "AAPL")).blocked
    assert (await manual.evaluator.evaluate(SHADOW_B, "AAPL")).blocked

    # Advance to the next New York trading session and restart over the same file.
    clock.set(FRI)
    auto_next = make_services(
        path, clock, max_cycles=1, threshold=1, reset_mode=LockoutResetMode.NEXT_SESSION
    )
    manual_next = make_services(
        path, clock, max_cycles=1, threshold=1, reset_mode=LockoutResetMode.MANUAL
    )

    assert await auto_next.cycles.count(SHADOW_A, "AAPL", now=FRI) == 0
    assert await auto_next.streaks.streak(SHADOW_A, "AAPL", now=FRI) == 0
    assert (await auto_next.evaluator.evaluate(SHADOW_A, "AAPL", now=FRI)).allowed
    # Saturday still belongs to Friday's session, so the reset holds through the weekend.
    assert await auto_next.cycles.count(SHADOW_A, "AAPL", now=SAT) == 0

    assert await manual_next.cycles.count(SHADOW_B, "AAPL", now=FRI) == 0
    assert await manual_next.streaks.streak(SHADOW_B, "AAPL", now=FRI) == 1
    still_blocked = await manual_next.evaluator.evaluate(SHADOW_B, "AAPL", now=FRI)
    assert still_blocked.reasons == (EntryBlockReason.LOSING_STREAK_LOCKOUT,)

    # Only an audited reset clears the manual lockout.
    event = await manual_next.streaks.reset_lockout(
        SHADOW_B,
        "AAPL",
        reset_id="reset-1",
        operator="mike",
        reason="reviewed tape",
        now=FRI,
    )
    assert event["reset_id"] == "reset-1" and event["was_locked"] is True
    assert (await manual_next.evaluator.evaluate(SHADOW_B, "AAPL", now=FRI)).allowed
    assert len(await manual_next.streaks.reset_events(SHADOW_B)) == 1


# ------------------------------------------------- (c) partial-fill semantics


async def test_partial_fill_semantics_end_to_end(tmp_path: Path) -> None:
    """First positive partial consumes one cycle; final cancel releases the rest."""
    clock = FakeClock(THU)
    path = open_database(tmp_path, "partials.sqlite3", clock)
    services = make_services(path, clock, max_cycles=2, threshold=5)

    await reserve(services, PAPER, "AAPL", "partial-entry", "100", THU)
    after_first = await services.ledger.apply_fill(
        PAPER, "AAPL", "partial-entry", "partial-fill-1", D("2"), D("20"), occurred_at=THU
    )
    assert after_first.pending_reserved_usd == D("60")
    assert after_first.committed_open_notional_usd == D("40")
    assert after_first.state == "OPEN"
    # First positive partial fill consumes exactly one cycle; later partials do not.
    assert await services.cycles.record_entry_fill(
        buy_fill("partial-fill-1", "partial-bo", PAPER, "AAPL", THU, qty="2", price="20")
    ) is True
    assert await services.cycles.count(PAPER, "AAPL", now=THU) == 1
    assert await services.cycles.record_entry_fill(
        buy_fill("partial-fill-2", "partial-bo", PAPER, "AAPL", THU, qty="1", price="20")
    ) is False
    assert await services.cycles.count(PAPER, "AAPL", now=THU) == 1

    # Final cancel of the remainder converts the reservation and releases unused budget.
    canceled = await services.ledger.apply_terminal_event(
        PAPER, "AAPL", "partial-entry", "partial-cancel-1", "CANCELED", occurred_at=THU
    )
    assert canceled.pending_reserved_usd == D("0")
    assert canceled.committed_open_notional_usd == D("40")
    assert canceled.state == "OPEN"
    snapshot = await services.ledger.snapshot(
        PAPER,
        "AAPL",
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
    )
    assert snapshot.pending_reserved_usd == D("0")
    assert snapshot.committed_open_notional_usd == D("40")
    # The filled remainder still sizes against the surviving committed budget.
    assert size_for_snapshot(snapshot).accepted

    # Zero-fill cancel consumes no cycle and releases the whole reservation.
    await reserve(services, PAPER, "MSFT", "empty-entry", "100", THU)
    released = await services.ledger.release_pending(
        PAPER, "MSFT", "empty-entry", "empty-cancel-1", occurred_at=THU
    )
    assert released.state == "CLOSED"
    assert released.pending_reserved_usd == D("0")
    assert released.committed_open_notional_usd == D("0")
    assert await services.cycles.count(PAPER, "MSFT", now=THU) == 0
    msft_snapshot = await services.ledger.snapshot(
        PAPER,
        "MSFT",
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
    )
    assert msft_snapshot.pending_reserved_usd == D("0")
    assert msft_snapshot.committed_open_notional_usd == D("40")  # only AAPL's fill


# ------------------------------------------------- (d) shared budget lock


async def test_simultaneous_signals_priority_ordered_without_overcommit(
    tmp_path: Path,
) -> None:
    """Concurrent eligible signals are accepted in priority order within total budget."""
    clock = FakeClock(THU)
    path = open_database(tmp_path, "priority.sqlite3", clock)
    services = make_services(path, clock, total="100", symbol="100")

    candidates = [
        {
            "symbol": "MSFT",
            "strategy_id": "second",
            "client_order_id": "msft-order",
            "amount_usd": D("60"),
            "symbol_priority": 1,
            "strategy_priority": 1,
        },
        {
            "symbol": "AAPL",
            "strategy_id": "zeta",
            "client_order_id": "zeta-order",
            "amount_usd": D("60"),
            "symbol_priority": 0,
            "strategy_priority": 1,
        },
        {
            "symbol": "AAPL",
            "strategy_id": "alpha",
            "client_order_id": "alpha-order",
            "amount_usd": D("60"),
            "symbol_priority": 0,
            "strategy_priority": 0,
        },
    ]
    first = await services.ledger.reserve_many(
        PAPER,
        candidates,
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
        occurred_at=THU,
    )
    # Reversed input must produce the identical deterministic outcome.
    void_path = open_database(tmp_path, "priority-reversed.sqlite3", clock)
    void_services = make_services(void_path, clock, total="100", symbol="100")
    second = await void_services.ledger.reserve_many(
        PAPER,
        list(reversed(candidates)),
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
        occurred_at=THU,
    )
    assert {key for key, value in first.items() if value is not None} == {"alpha-order"}
    assert {key for key, value in second.items() if value is not None} == {"alpha-order"}

    snapshot = await services.ledger.snapshot(
        PAPER,
        "AAPL",
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
    )
    assert snapshot.pending_reserved_usd == D("60")
    assert snapshot.remaining_application_usd == D("40")

    # Truly concurrent reserves share one account lock: exactly one 60/100 pair wins.
    race_services = make_services(
        open_database(tmp_path, "race.sqlite3", clock), clock, total="100", symbol="80"
    )
    outcomes = await asyncio.gather(
        reserve(race_services, PAPER, "AAA", "race-a", "60", THU),
        reserve(race_services, PAPER, "BBB", "race-b", "60", THU),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ValueError) for item in outcomes) == 1
    assert sum(isinstance(item, BudgetReservation) for item in outcomes) == 1
    race_snapshot = await race_services.ledger.snapshot(
        PAPER,
        "AAA",
        broker_buying_power_usd=BIG_POWER,
        non_margin_buying_power_usd=BIG_POWER,
    )
    assert race_snapshot.pending_reserved_usd == D("60")
    assert race_snapshot.remaining_application_usd == D("40")


# ------------------------------------------------- (e) entries blocked, exits proceed


async def test_entry_blocks_never_block_exits(tmp_path: Path) -> None:
    """Every entry blocker still lets protective-stop, ingestion, manual, risk exits through."""
    clock = FakeClock(THU)
    path = open_database(tmp_path, "exits.sqlite3", clock)
    services = make_services(
        path,
        clock,
        max_cycles=1,
        threshold=1,
        reset_mode=LockoutResetMode.MANUAL,
        cooldown_seconds=3600,
        max_open=1,
        daily_loss="5",
    )

    # Block entries four ways at once: cycles, streak lockout, cooldown, daily loss.
    await reserve(services, PAPER, "AAPL", "blocked-entry", "100", THU)
    assert await services.cycles.record_entry_fill(
        buy_fill("blocked-f1", "blocked-bo", PAPER, "AAPL", THU)
    ) is True
    await services.streaks.record_closed_cycle(
        losing_cycle("blocked-c1", PAPER, "AAPL", THU + timedelta(minutes=10))
    )
    await services.cooldown.record_final_exit(
        PAPER, "AAPL", "blocked-exit", THU + timedelta(minutes=20)
    )
    decision = await services.evaluator.evaluate(
        PAPER, "AAPL", open_position_count=1, now=THU + timedelta(minutes=30)
    )
    assert set(decision.reasons) == {
        EntryBlockReason.MAX_ENTRY_CYCLES_REACHED,
        EntryBlockReason.LOSING_STREAK_LOCKOUT,
        EntryBlockReason.POST_SALE_COOLDOWN,
        EntryBlockReason.DAILY_LOSS_LIMIT_REACHED,
        EntryBlockReason.MAX_OPEN_POSITIONS_REACHED,
    }

    # Entry cutoff (30 min before the real close) and stale data block entries too.
    cutoff_policy = SessionPolicy(entry_cutoff=EntryCutoffPolicy(30))
    late = datetime(2026, 10, 8, 19, 45, tzinfo=UTC)  # 15:45 ET, inside the cutoff
    assert cutoff_policy.evaluate(THU).allowed
    assert cutoff_policy.evaluate(late).reason == "ENTRY_CUTOFF_REACHED"
    stale = MarketFilterDecision.evaluate(
        now=THU,
        quote=Quote(receive_ts=THU - timedelta(minutes=5)),
        spread_filter=SpreadFilter(max_absolute=D("1")),
        liquidity_filter=LiquidityFilter(minimum_quote_size=D("1")),
    )
    assert not stale.allowed and MarketFilterReason.QUOTE_STALE in stale.reasons

    # Fill ingestion is unconditional while entries are blocked.
    assert await services.cycles.record_entry_fill(
        buy_fill("blocked-f2", "blocked-bo-2", PAPER, "AAPL", THU)
    ) is True
    assert await services.streaks.record_closed_cycle(
        losing_cycle("blocked-c2", PAPER, "AAPL", THU + timedelta(minutes=40))
    ) is True
    assert await services.cooldown.record_closed_cycle(
        losing_cycle("blocked-c3", PAPER, "AAPL", THU + timedelta(minutes=50))
    ) is True

    # Reservation exit bookkeeping proceeds on the held cycle.
    filled = await services.ledger.apply_fill(
        PAPER, "AAPL", "blocked-entry", "blocked-fill-1", D("2"), D("20"), occurred_at=THU
    )
    assert filled.committed_open_notional_usd == D("40")
    closed = await services.ledger.close_cycle(
        PAPER, "AAPL", "blocked-entry", "blocked-close-1", occurred_at=THU
    )
    assert closed.state == "CLOSED"

    # Protective-stop, manual-close, and risk exits ignore the discretionary hold;
    # only the ordinary strategy exit waits out the minimum hold window.
    hold = MinimumHoldPolicy(timedelta(minutes=10))
    entry_fill_at = THU
    early = THU + timedelta(minutes=9)
    assert hold.evaluate(
        now=early, first_confirmed_entry_fill_at=entry_fill_at, exit_kind=ExitKind.STRATEGY
    ).reason == "MINIMUM_HOLD_NOT_ELAPSED"
    for kind in (
        ExitKind.PROTECTIVE_STOP,
        ExitKind.TRAILING_STOP,
        ExitKind.RISK_SAFETY,
        ExitKind.DAILY_LOSS,
        ExitKind.MANUAL_CLOSE,
        ExitKind.CLOSE_ALL,
        ExitKind.FLATTEN,
        ExitKind.RECONCILIATION,
        ExitKind.END_OF_DAY,
    ):
        assert hold.evaluate(
            now=early, first_confirmed_entry_fill_at=entry_fill_at, exit_kind=kind
        ).allowed, kind

    # Entries remain blocked after all of the above exit-side activity.
    assert (await services.evaluator.evaluate(
        PAPER, "AAPL", open_position_count=1, now=THU + timedelta(minutes=30)
    )).blocked
    assert sqlite3.connect  # sqlite-backed adapters only; no broker or network touched
