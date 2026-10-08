from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.portfolio.budgets import BudgetPolicy
from mike_trade.portfolio.reservations import ReservationLedger

D = Decimal
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
PAPER = AccountScope(AccountEnvironment.PAPER, "ACCT")


class Clock:
    def now(self) -> datetime:
        return NOW


class MemoryRepository:
    """Test adapter implementing the repository's atomic scope replacement contract."""

    def __init__(self) -> None:
        self.rows = {}

    def load(self, account_scope_id):
        return self.rows.get(account_scope_id)

    def save(self, account_scope_id, reservations, processed_activity_ids):
        self.rows[account_scope_id] = (reservations, processed_activity_ids)


def ledger(
    *, total: str = "1000", symbol: str = "600", reserve: str = "0", margin=False, repo=None
):
    return ReservationLedger(
        BudgetPolicy(D(total), D(symbol), reserve_usd=D(reserve), margin_enabled=margin),
        repository=repo,
        clock=Clock(),
    )


async def reserve(subject, symbol, order_id, amount, scope=PAPER.scope_id, broker="10000"):
    return await subject.reserve(
        scope,
        symbol,
        order_id,
        D(amount),
        broker_buying_power_usd=D(broker),
        non_margin_buying_power_usd=D(broker),
        occurred_at=NOW,
    )


def test_all_money_and_quantity_inputs_reject_float() -> None:
    with pytest.raises(TypeError):
        BudgetPolicy(1000.0, D("100"))  # type: ignore[arg-type]

    async def exercise() -> None:
        subject = ledger()
        with pytest.raises(TypeError):
            await subject.reserve(
                PAPER.scope_id,
                "AAPL",
                "order-1",
                10.0,  # type: ignore[arg-type]
                broker_buying_power_usd=D("1000"),
            )
        await reserve(subject, "AAPL", "order-1", "100")
        with pytest.raises(TypeError):
            await subject.apply_fill(
                PAPER.scope_id,
                "AAPL",
                "order-1",
                "fill-1",
                1.0,  # type: ignore[arg-type]
                D("10"),
            )

    asyncio.run(exercise())


def test_concurrent_reservations_share_one_account_budget_lock() -> None:
    async def exercise() -> None:
        subject = ledger(total="100", symbol="80")
        results = await asyncio.gather(
            reserve(subject, "AAPL", "a", "60"),
            reserve(subject, "MSFT", "m", "60"),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, Exception) for item in results) == 1
        assert sum(isinstance(item, ValueError) for item in results) == 1
        snap = await subject.snapshot(
            PAPER.scope_id,
            "AAPL",
            broker_buying_power_usd=D("10000"),
            non_margin_buying_power_usd=D("10000"),
        )
        assert snap.pending_reserved_usd == D("60")
        assert snap.committed_open_notional_usd == D("0")
        assert snap.remaining_total_usd == D("40")

    asyncio.run(exercise())


def test_same_symbol_cannot_top_up_pending_or_open_cycle() -> None:
    async def exercise() -> None:
        subject = ledger()
        await reserve(subject, "aapl", "first", "100")
        with pytest.raises(ValueError, match="already pending or open"):
            await reserve(subject, "AAPL", "second", "50")
        await subject.apply_fill(
            PAPER.scope_id, "AAPL", "first", "fill-1", D("2"), D("10"), occurred_at=NOW
        )
        await subject.release_pending(
            PAPER.scope_id, "AAPL", "first", "cancel-1", occurred_at=NOW
        )
        with pytest.raises(ValueError, match="already pending or open"):
            await reserve(subject, "AAPL", "third", "50")

    asyncio.run(exercise())


def test_partial_fill_then_cancel_releases_only_unused_reservation_and_is_idempotent() -> None:
    async def exercise() -> None:
        subject = ledger()
        await reserve(subject, "AAPL", "entry", "100")
        first_fill = await subject.apply_fill(
            PAPER.scope_id,
            "AAPL",
            "entry",
            "fill-1",
            D("2"),
            D("20"),
            occurred_at=NOW,
        )
        assert first_fill.pending_reserved_usd == D("60")
        assert first_fill.committed_open_notional_usd == D("40")
        canceled = await subject.apply_terminal_event(
            PAPER.scope_id,
            "AAPL",
            "entry",
            "cancel-1",
            "CANCELED",
            occurred_at=NOW,
        )
        assert canceled.pending_reserved_usd == D("0")
        assert canceled.committed_open_notional_usd == D("40")
        assert canceled.state == "OPEN"

        duplicate_fill = await subject.apply_fill(
            PAPER.scope_id,
            "AAPL",
            "entry",
            "fill-1",
            D("2"),
            D("20"),
            occurred_at=NOW,
        )
        duplicate_cancel = await subject.apply_terminal_event(
            PAPER.scope_id,
            "AAPL",
            "entry",
            "cancel-1",
            "CANCELED",
            occurred_at=NOW,
        )
        assert duplicate_fill.committed_open_notional_usd == D("40")
        assert duplicate_cancel == duplicate_fill

    asyncio.run(exercise())


def test_account_and_scenario_keys_are_fully_isolated() -> None:
    async def exercise() -> None:
        subject = ledger(total="100", symbol="100")
        scenario_a = AccountScope(AccountEnvironment.SHADOW, "ACCT", "scenario-a")
        scenario_b = AccountScope(AccountEnvironment.SHADOW, "ACCT", "scenario-b")
        await reserve(subject, "AAPL", "a", "80", scope=scenario_a.scope_id)
        a = await subject.snapshot(
            scenario_a,
            "AAPL",
            broker_buying_power_usd=D("1000"),
            non_margin_buying_power_usd=D("1000"),
        )
        b = await subject.snapshot(
            scenario_b,
            "AAPL",
            broker_buying_power_usd=D("1000"),
            non_margin_buying_power_usd=D("1000"),
        )
        paper = await subject.snapshot(
            PAPER,
            "AAPL",
            broker_buying_power_usd=D("1000"),
            non_margin_buying_power_usd=D("1000"),
        )
        assert a.remaining_application_usd == D("20")
        assert b.remaining_application_usd == D("100")
        assert paper.remaining_application_usd == D("100")

    asyncio.run(exercise())


def test_broker_reserve_and_non_margin_power_limit_effective_cap() -> None:
    async def exercise() -> None:
        cash_only = ledger(total="1000", symbol="1000", reserve="100")
        cash_snapshot = await cash_only.snapshot(
            PAPER,
            "AAPL",
            broker_buying_power_usd=D("5000"),
            settled_cash_usd=D("350"),
            non_margin_buying_power_usd=D("400"),
        )
        assert cash_snapshot.broker_available_usd == D("250")
        assert cash_snapshot.effective_cap_usd == D("250")

        margin = ledger(total="1000", symbol="1000", reserve="100", margin=True)
        margin_snapshot = await margin.snapshot(
            PAPER,
            "AAPL",
            broker_buying_power_usd=D("5000"),
            settled_cash_usd=D("350"),
            non_margin_buying_power_usd=D("400"),
        )
        assert margin_snapshot.effective_cap_usd == D("1000")

    asyncio.run(exercise())


def test_deterministic_signal_priority_and_stable_tie_breakers() -> None:
    candidates = [
        {
            "symbol": "AAPL",
            "strategy_id": "zeta",
            "client_order_id": "zeta-order",
            "amount_usd": D("60"),
            "symbol_priority": 1,
            "strategy_priority": 1,
        },
        {
            "symbol": "AAPL",
            "strategy_id": "alpha",
            "client_order_id": "alpha-order",
            "amount_usd": D("60"),
            "symbol_priority": 1,
            "strategy_priority": 1,
        },
        {
            "symbol": "MSFT",
            "strategy_id": "first",
            "client_order_id": "msft-order",
            "amount_usd": D("60"),
            "symbol_priority": 2,
            "strategy_priority": 0,
        },
    ]

    async def one_run(values):
        subject = ledger(total="100", symbol="100")
        result = await subject.reserve_many(
            PAPER,
            values,
            broker_buying_power_usd=D("1000"),
            non_margin_buying_power_usd=D("1000"),
            occurred_at=NOW,
        )
        return tuple(key for key, value in result.items() if value is not None), result

    accepted_1, result_1 = asyncio.run(one_run(candidates))
    accepted_2, result_2 = asyncio.run(one_run(reversed(candidates)))
    assert accepted_1 == accepted_2 == ("alpha-order",)
    assert tuple(result_1) == tuple(result_2) == (
        "alpha-order",
        "zeta-order",
        "msft-order",
    )


def test_closing_cycle_releases_committed_notional() -> None:
    async def exercise() -> None:
        subject = ledger(total="500", symbol="500")
        await reserve(subject, "AAPL", "entry", "200")
        await subject.apply_fill(
            PAPER.scope_id,
            "AAPL",
            "entry",
            "fill",
            D("10"),
            D("20"),
            occurred_at=NOW,
            final=True,
        )
        before = await subject.snapshot(
            PAPER,
            "AAPL",
            broker_buying_power_usd=D("10000"),
            non_margin_buying_power_usd=D("10000"),
        )
        assert before.committed_open_notional_usd == D("200")
        assert before.remaining_application_usd == D("300")
        await subject.close_cycle(
            PAPER, "AAPL", "entry", "close-1", occurred_at=NOW + timedelta(minutes=1)
        )
        after = await subject.snapshot(
            PAPER,
            "AAPL",
            broker_buying_power_usd=D("10000"),
            non_margin_buying_power_usd=D("10000"),
        )
        assert after.committed_open_notional_usd == D("0")
        assert after.remaining_application_usd == D("500")
        await reserve(subject, "AAPL", "next-cycle", "500")

    asyncio.run(exercise())


def test_repository_hook_survives_service_restart_and_persists_dedupe_ids() -> None:
    async def exercise() -> None:
        repository = MemoryRepository()
        first = ledger(repo=repository)
        await reserve(first, "AAPL", "entry", "100")
        await first.apply_fill(
            PAPER.scope_id,
            "AAPL",
            "entry",
            "fill-1",
            D("2"),
            D("10"),
            occurred_at=NOW,
        )

        restarted = ledger(repo=repository)
        duplicate = await restarted.apply_fill(
            PAPER.scope_id,
            "AAPL",
            "entry",
            "fill-1",
            D("2"),
            D("10"),
            occurred_at=NOW,
        )
        snapshot = await restarted.snapshot(
            PAPER,
            "AAPL",
            broker_buying_power_usd=D("10000"),
            non_margin_buying_power_usd=D("10000"),
        )
        assert duplicate.committed_open_notional_usd == D("20")
        assert snapshot.committed_open_notional_usd == D("20")
        assert snapshot.pending_reserved_usd == D("80")

    asyncio.run(exercise())


def test_reservation_timestamps_must_be_utc() -> None:
    async def exercise() -> None:
        subject = ledger()
        with pytest.raises(ValueError, match="UTC"):
            await subject.reserve(
                PAPER,
                "AAPL",
                "entry",
                D("100"),
                broker_buying_power_usd=D("1000"),
                occurred_at=NOW.astimezone(timezone(timedelta(hours=-4))),
            )

    asyncio.run(exercise())
