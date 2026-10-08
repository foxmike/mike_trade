"""Phase P SHADOW restart ledger balance and live-nonexecution acceptance tests.

Verification-only: this module writes no production code and touches no broker.
Every test uses a temporary SQLite WAL database plus fixed, deterministic
quotes/timestamps.  Shadow outcomes are simulator *estimates*, never
broker-confirmed, and imply no profitability claim.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mike_trade.brokers.alpaca import account as alpaca_account
from mike_trade.brokers.alpaca import http as alpaca_http
from mike_trade.brokers.alpaca import orders as alpaca_orders
from mike_trade.brokers.shadow.fills import ShadowFillSimulator
from mike_trade.brokers.shadow.ledger import SIMULATOR_VERSION, ShadowLedger
from mike_trade.brokers.shadow.stops import ShadowStopSimulator, StopTriggerAssumptions
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.market_data.candles import PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext, QuoteSnapshot
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.shadow.scenarios import ScenarioConfig, ScenarioRunner

D = Decimal
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)  # Thursday, 10:30 ET (regular session)
SESSION = date(2026, 10, 8)
ALPHA = "restart-alpha"
BETA = "restart-beta"
MARKS = {"AAPL": D("100"), "MSFT": D("210"), "NVDA": D("50")}


class FakeClock:
    def now(self) -> datetime:
        return NOW


def make_quote(
    symbol: str,
    bid: str,
    ask: str,
    offset_s: int,
    *,
    size: str = "100",
    feed: str = "iex",
) -> QuoteSnapshot:
    ts = NOW + timedelta(seconds=offset_s)
    return QuoteSnapshot(
        symbol=symbol,
        bid=D(bid),
        ask=D(ask),
        bid_size=D(size),
        ask_size=D(size),
        provider_ts=ts,
        receive_ts=ts,
        feed_id=feed,
    )


def make_context(symbol: str, quote: QuoteSnapshot) -> MarketContext:
    return MarketContext(
        symbol=symbol,
        as_of=quote.provider_ts,
        version=1,
        feed_id=quote.feed_id,
        consolidated=False,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id="2026-10-08",
        freshness=ContextFreshness(
            newest_data_ts=quote.provider_ts,
            age=timedelta(0),
            max_age=timedelta(seconds=30),
            stale=False,
            reason=None,
        ),
        candles={},
        indicators={},
        latest_quote=quote,
    )


def init_shadow_db(path: Path) -> Path:
    initialize_database(path, AccountEnvironment.SHADOW, now=NOW).close()
    mode = sqlite3.connect(str(path)).execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal", f"shadow test db must be WAL, got {mode!r}"
    return path


def alpha_config() -> ScenarioConfig:
    return ScenarioConfig(
        scenario_id=ALPHA,
        starting_cash=D("50000"),
        per_symbol_budget=D("5000"),
        total_budget=D("20000"),
        max_entries_per_symbol_per_session=10,
        loss_streak_lockout=5,
        post_sale_cooldown=timedelta(minutes=10),
    )


def beta_config() -> ScenarioConfig:
    return ScenarioConfig(
        scenario_id=BETA,
        starting_cash=D("7000"),
        per_symbol_budget=D("3000"),
        total_budget=D("6000"),
        max_entries_per_symbol_per_session=10,
        loss_streak_lockout=5,
        post_sale_cooldown=timedelta(minutes=10),
    )


def entry_intent(
    scope_id: str, client_id: str, cycle_id: str, symbol: str, quantity: str, limit: str, at: datetime
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"intent-{client_id}",
        client_order_id=client_id,
        account_scope_id=scope_id,
        cycle_id=cycle_id,
        symbol=symbol,
        side=OrderSide.BUY,
        purpose=OrderPurpose.ENTRY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        created_at=at,
        quantity=D(quantity),
        limit_price=D(limit),
    )


async def drive_alpha(runner: ScenarioRunner) -> dict[str, Any]:
    """Deterministic fixed quote sequence covering every required lifecycle event."""
    account = runner.get_account(ALPHA)
    scope_id = account.scope.scope_id
    fills = ShadowFillSimulator()
    stops = ShadowStopSimulator(account, StopTriggerAssumptions())
    evidence: dict[str, Any] = {"fills": [], "stop_mid": None}

    # Buy fill, split across two quotes so the first is a partial fill.
    order = await account.submit_order(
        entry_intent(scope_id, "alpha-aapl-1", f"{ALPHA}-AAPL-c1", "AAPL", "10", "100.50", NOW)
    )
    first = fills.apply_fill_to_account(
        account,
        order.broker_order_id,
        [make_quote("AAPL", "99.98", "100.00", 1, size="6")],
        as_of=NOW + timedelta(seconds=2),
    )
    assert first.quantity == D("6")  # partial fill: participation-capped at size 6
    evidence["fills"].append(first)
    second = fills.apply_fill_to_account(
        account,
        order.broker_order_id,
        [make_quote("AAPL", "99.98", "100.00", 3)],
        as_of=NOW + timedelta(seconds=4),
    )
    assert second.quantity == D("4")  # buy fill completing the order
    evidence["fills"].append(second)

    # Stop arming, then a trailing-stop update on a rising quote.
    policy = ProtectionPolicy(ProtectionKind.BROKER_TRAILING_STOP, trail_amount=D("5"))
    armed = await stops.arm(f"{ALPHA}-AAPL-c1", "AAPL", policy, at=NOW + timedelta(seconds=60))
    assert armed is not None and armed.stop_price == D("95")
    moved = await stops.evaluate(
        f"{ALPHA}-AAPL-c1",
        make_quote("AAPL", "102.00", "102.02", 120),
        at=NOW + timedelta(seconds=121),
    )
    assert moved.stop_price == D("97")  # trailing stop update: 102 - 5
    evidence["stop_mid"] = moved

    # Stop-triggered exit at a loss: drives the loss-streak counter and cooldown.
    triggered = await stops.evaluate(
        f"{ALPHA}-AAPL-c1",
        make_quote("AAPL", "94.00", "94.02", 180),
        at=NOW + timedelta(seconds=181),
    )
    assert triggered.status == "TRIGGERED"
    assert account.state.losing_streak("AAPL") == 1
    assert account.state.cooldown_expiry("AAPL") is not None

    # A second, still-open position plus a pending open order: restart must keep both.
    msft = await account.submit_order(
        entry_intent(
            scope_id,
            "alpha-msft-1",
            f"{ALPHA}-MSFT-c1",
            "MSFT",
            "5",
            "200.50",
            NOW + timedelta(seconds=200),
        )
    )
    msft_fill = fills.apply_fill_to_account(
        account,
        msft.broker_order_id,
        [make_quote("MSFT", "199.98", "200.00", 201)],
        as_of=NOW + timedelta(seconds=202),
    )
    evidence["fills"].append(msft_fill)
    await account.submit_order(
        entry_intent(
            scope_id,
            "alpha-nvda-1",
            f"{ALPHA}-NVDA-c1",
            "NVDA",
            "2",
            "50.50",
            NOW + timedelta(seconds=210),
        )
    )

    # Audited cash adjustment.
    assert account.adjust_cash(
        adjustment_id="ops-topup-1",
        amount=D("250"),
        reason="acceptance top-up",
        actor="ops",
        at=NOW + timedelta(seconds=220),
    )
    evidence["stops"] = stops
    return evidence


async def drive_beta(runner: ScenarioRunner) -> None:
    """A smaller independent scenario sharing the same database file."""
    account = runner.get_account(BETA)
    scope_id = account.scope.scope_id
    fills = ShadowFillSimulator()
    order = await account.submit_order(
        entry_intent(scope_id, "beta-aapl-1", f"{BETA}-AAPL-c1", "AAPL", "2", "100.50", NOW)
    )
    result = fills.apply_fill_to_account(
        account,
        order.broker_order_id,
        [make_quote("AAPL", "99.98", "100.00", 11)],
        as_of=NOW + timedelta(seconds=12),
    )
    assert result.quantity == D("2")
    assert account.adjust_cash(
        adjustment_id="ops-fee-1",
        amount=D("-100"),
        reason="acceptance fee",
        actor="ops",
        at=NOW + timedelta(seconds=20),
    )


async def snapshot(runner: ScenarioRunner, scenario_id: str) -> dict[str, Any]:
    account = runner.get_account(scenario_id)
    state = account.state
    open_orders = await account.list_open_orders()
    positions = await account.list_positions()
    stops = ShadowStopSimulator(account, StopTriggerAssumptions())
    return {
        "cash": state.cash,
        "starting_cash": state.starting_cash,
        "realized": state.realized_pnl,
        "unrealized": account.unrealized_pnl(MARKS),
        "equity": account.equity(MARKS),
        "positions": sorted(
            (
                p.symbol,
                p.quantity,
                p.avg_entry_price,
                p.cost_basis,
                p.market_value,
                p.cycle_id,
            )
            for p in positions
        ),
        "position_detail": sorted(
            (
                symbol,
                (pos.quantity, pos.cost_basis, pos.entry_fees, pos.strategy_key,
                 pos.cycle_id, pos.entry_order_id, pos.opened_at, pos.entry_cost_total,
                 pos.entry_fees_total, pos.realized_pnl, pos.exit_proceeds, pos.exit_fees),
            )
            for symbol, pos in state.positions.items()
        ),
        "open_orders": sorted(
            (
                o.broker_order_id, o.client_order_id, o.symbol, o.side, o.order_type,
                o.status, o.quantity, o.filled_quantity, o.submitted_at, o.updated_at,
                o.filled_avg_price, o.limit_price, o.stop_price, o.trail_price,
                o.trail_percent,
            )
            for o in open_orders
        ),
        "stop_states": {
            cycle: stops.state(cycle)
            for cycle in (f"{scenario_id}-AAPL-c1", f"{scenario_id}-MSFT-c1")
            if stops.state(cycle) is not None
        },
        "entry_cycles": {
            symbol: state.filled_entry_cycles(symbol, SESSION) for symbol in ("AAPL", "MSFT", "NVDA")
        },
        "loss_streaks": {symbol: state.losing_streak(symbol) for symbol in ("AAPL", "MSFT")},
        "cooldowns": {symbol: state.cooldown_expiry(symbol) for symbol in ("AAPL", "MSFT")},
        "closed_cycles": state.closed_cycles,
    }


def start_runner(db_path: Path, *configs: ScenarioConfig) -> ScenarioRunner:
    runner = ScenarioRunner(db_path, clock=FakeClock())
    for config in configs:
        runner.add_scenario(config)
    return runner


async def test_shadow_restart_ledger_balance(tmp_path: Path) -> None:
    db_path = init_shadow_db(tmp_path / "shadow.db")
    runner = start_runner(db_path, alpha_config(), beta_config())
    await drive_alpha(runner)
    await drive_beta(runner)

    alpha_before = await snapshot(runner, ALPHA)
    beta_before = await snapshot(runner, BETA)

    # Pre-restart lifecycle evidence: every required event actually happened.
    assert alpha_before["entry_cycles"] == {"AAPL": 1, "MSFT": 1, "NVDA": 0}
    assert alpha_before["loss_streaks"]["AAPL"] == 1
    assert alpha_before["cooldowns"]["AAPL"] is not None
    assert alpha_before["loss_streaks"]["MSFT"] == 0
    assert alpha_before["cooldowns"]["MSFT"] is None
    assert alpha_before["realized"] == D("-60")  # 10 x 94.00 - 10 x 100.00
    assert alpha_before["cash"] == D("49190")  # 50000 - 1000 + 940 - 1000 + 250
    assert alpha_before["unrealized"] == D("50")  # 5 x 210 - 5 x 200, zero fees
    assert len(alpha_before["open_orders"]) == 1  # the untouched NVDA entry
    assert alpha_before["open_orders"][0][2] == "NVDA"
    assert beta_before["cash"] == D("6700")  # 7000 - 200 - 100
    assert len(beta_before["positions"]) == 1  # beta holds its own open position
    assert beta_before["loss_streaks"]["AAPL"] == 0  # alpha's loss never leaks into beta
    assert beta_before["cooldowns"]["AAPL"] is None

    runner.close()

    # Reopen from the same file with fresh runner/ledger/account instances.  The
    # replayed starting cash differs on purpose: it must NOT be re-applied.
    reopened = start_runner(
        db_path,
        ScenarioConfig(
            scenario_id=ALPHA,
            starting_cash=D("1"),
            per_symbol_budget=D("5000"),
            total_budget=D("20000"),
            max_entries_per_symbol_per_session=10,
            loss_streak_lockout=5,
            post_sale_cooldown=timedelta(minutes=10),
        ),
        beta_config(),
    )
    try:
        alpha_after = await snapshot(reopened, ALPHA)
        beta_after = await snapshot(reopened, BETA)

        assert alpha_after == alpha_before
        assert beta_after == beta_before
        assert alpha_after != beta_after  # each scenario restores independently

        account = reopened.get_account(ALPHA)
        assert account.state.starting_cash == D("50000")  # original creation value kept
        assert account.state.cash == D("49190")  # starting cash was not re-applied
        assert "MSFT" in account.state.positions  # positions were not reset
        assert beta_after["positions"] != []  # beta's open position survives too
    finally:
        reopened.close()


async def test_shadow_records_estimated_with_provenance(tmp_path: Path) -> None:
    db_path = init_shadow_db(tmp_path / "shadow.db")
    runner = start_runner(db_path, alpha_config(), beta_config())
    evidence = await drive_alpha(runner)
    await drive_beta(runner)
    runner.close()

    for result in evidence["fills"]:
        assert result.estimated is True
        assert result.broker_confirmed is False
        assert result.simulator_version == SIMULATOR_VERSION
        assert result.feed_id == "iex"
        assert result.feed_label == "iex:not_consolidated"

    mid = evidence["stop_mid"]
    assert mid is not None
    assert mid.estimated is True and mid.broker_confirmed is False
    assert mid.simulator_version == SIMULATOR_VERSION
    assert mid.provenance is not None and "iex:quote" in mid.provenance

    session = WriterSession(db_path)
    try:
        for scenario_id in (ALPHA, BETA):
            scope = AccountScope(AccountEnvironment.SHADOW, "ACCT", scenario_id)
            ledger = ShadowLedger(session.bind_scope(scope.scope_id), scope, clock=FakeClock())
            events = ledger.read_events()
            assert events, f"{scenario_id} ledger must hold its own events"
            for event in events:
                assert event.estimated is True
                assert event.broker_confirmed is False
                assert event.simulator_version == SIMULATOR_VERSION
            by_kind = {}
            for event in events:
                by_kind.setdefault(event.kind, []).append(event)
            assert by_kind["ShadowFillRecorded"], f"{scenario_id} must record fills"
            for event in by_kind["ShadowFillRecorded"]:
                # Entry fills use the fill-simulator feed label; the stop-triggered
                # exit fill carries the triggering quote's provenance.  Both are
                # simulator provenance, never a broker execution venue.
                assert event.feed and event.feed.startswith("iex:")
            assert any(
                event.feed == "iex:not_consolidated"
                for event in by_kind["ShadowFillRecorded"]
            ), f"{scenario_id} must record at least one fill-simulator fill"
            stop_records = [
                event.data["shadow_stop_record"]
                for event in by_kind.get("ShadowOrderReplaced", [])
                if "shadow_stop_record" in event.data
            ]
            if scenario_id == ALPHA:
                assert stop_records, "stop simulator must persist audit records"
                for record in stop_records:
                    assert record["estimated"] is True
                    assert record["broker_confirmed"] is False
                    # The arming record cites the position fill; evaluations cite quotes.
                    assert record["provenance"]
                assert any(
                    "iex:quote" in (record["provenance"] or "") for record in stop_records
                ), "stop evaluations must persist quote provenance"
    finally:
        session.close()


async def test_shadow_live_nonexecution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_PAPER_SECRET_KEY",
        "ALPACA_LIVE_KEY_ID",
        "ALPACA_LIVE_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "MIKE_TRADE_ALPACA_PAPER_KEY_ID",
        "MIKE_TRADE_ALPACA_PAPER_SECRET_KEY",
        "MIKE_TRADE_ALPACA_LIVE_KEY_ID",
        "MIKE_TRADE_ALPACA_LIVE_SECRET_KEY",
        "MIKE_TRADE_ALPACA_DATA_KEY_ID",
        "MIKE_TRADE_ALPACA_DATA_SECRET_KEY",
    ):
        monkeypatch.setenv(var, "POISONED-DO-NOT-USE")

    calls = {"http_client": 0, "order_service": 0, "submit_order": 0, "account_service": 0}

    def _boom(name: str):  # type: ignore[no-untyped-def]
        def _init(self: object, *args: Any, **kwargs: Any) -> None:
            calls[name] += 1
            raise AssertionError(f"alpaca {name} must never run during a SHADOW run")

        return _init

    def _boom_submit(self: object, *args: Any, **kwargs: Any) -> None:
        calls["submit_order"] += 1
        raise AssertionError("alpaca order submission must never run during a SHADOW run")

    monkeypatch.setattr(alpaca_http.AlpacaHttpClient, "__init__", _boom("http_client"))
    monkeypatch.setattr(alpaca_orders.AlpacaOrderService, "__init__", _boom("order_service"))
    monkeypatch.setattr(alpaca_orders.AlpacaOrderService, "submit_order", _boom_submit)
    monkeypatch.setattr(alpaca_account.AlpacaAccountService, "__init__", _boom("account_service"))

    db_path = init_shadow_db(tmp_path / "shadow.db")
    config = ScenarioConfig(
        scenario_id=ALPHA,
        starting_cash=D("50000"),
        per_symbol_budget=D("5000"),
        total_budget=D("20000"),
        buy_strategies=("buy-aapl",),
        max_entries_per_symbol_per_session=10,
        loss_streak_lockout=5,
        post_sale_cooldown=timedelta(minutes=10),
    )
    runner = start_runner(db_path, config)
    runner.register_strategy("buy-aapl", _buy_aapl_plugin(), side="BUY")
    try:
        quote = make_quote("AAPL", "99.50", "100.00", 0)
        feed = _StaticFeed({"AAPL": make_context("AAPL", quote)})
        submitted = await runner.run_once(("AAPL",), feed=feed)
        assert submitted[ALPHA], "the shadow run must do real simulated work"

        account = runner.get_account(ALPHA)
        assert account.state.positions["AAPL"].quantity == D("10")
        stops = ShadowStopSimulator(account, StopTriggerAssumptions())
        policy = ProtectionPolicy(ProtectionKind.FIXED_STOP, stop_offset_amount=D("5"))
        await stops.arm(f"{ALPHA}-AAPL-c1", "AAPL", policy, at=NOW + timedelta(seconds=60))
        state = await stops.evaluate(
            f"{ALPHA}-AAPL-c1",
            make_quote("AAPL", "90.00", "90.02", 120),
            at=NOW + timedelta(seconds=121),
        )
        assert state.status == "TRIGGERED"
        assert "AAPL" not in account.state.positions
    finally:
        runner.close()

    assert calls == {
        "http_client": 0,
        "order_service": 0,
        "submit_order": 0,
        "account_service": 0,
    }, f"alpaca paths were invoked during a SHADOW run: {calls}"


def test_shadow_ledger_rejects_paper_live_ids(tmp_path: Path) -> None:
    for reserved in ("PAPER", "paper", "LIVE", "live"):
        with pytest.raises(ValueError):
            ScenarioConfig(scenario_id=reserved, starting_cash=D("1"),
                           per_symbol_budget=D("1"), total_budget=D("1"))
        with pytest.raises(ValueError):
            ScenarioConfig(scenario_id="ok", account_id=reserved, starting_cash=D("1"),
                           per_symbol_budget=D("1"), total_budget=D("1"))

    db_path = init_shadow_db(tmp_path / "shadow.db")
    session = WriterSession(db_path)
    try:
        scope = AccountScope(AccountEnvironment.SHADOW, "ACCT", "probe")
        writer = session.bind_scope(scope.scope_id)
        for environment in (AccountEnvironment.PAPER, AccountEnvironment.LIVE):
            with pytest.raises(ModeIsolationError):
                ShadowLedger(writer, AccountScope(environment, "ACCT"), clock=FakeClock())
    finally:
        session.close()


class _StaticFeed:
    def __init__(self, snapshot: dict[str, MarketContext]) -> None:
        self._snapshot = dict(snapshot)

    async def snapshot(self, symbols: tuple[str, ...]) -> dict[str, MarketContext]:
        assert tuple(symbols) == ("AAPL",)
        return dict(self._snapshot)


def _buy_aapl_plugin():  # type: ignore[no-untyped-def]
    def _decide(config: ScenarioConfig, context: MarketContext):  # type: ignore[no-untyped-def]
        if context.symbol != "AAPL":
            return None
        return OrderIntent(
            intent_id=f"{config.scenario_id}-intent-1",
            client_order_id=f"{config.scenario_id}-aapl-buy-1",
            account_scope_id=config.scope.scope_id,
            cycle_id=f"{config.scenario_id}-AAPL-c1",
            symbol="AAPL",
            side=OrderSide.BUY,
            purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=NOW,
            quantity=D("10"),
        )

    return _decide
