"""Shadow scenario isolation: concurrent scenarios on one shared feed.

Positive/negative coverage only through the public shadow path
(``ScenarioConfig`` / ``ScenarioRegistry`` / ``ScenarioRunner`` /
``ScenarioProjection`` over ``ShadowAccount``/``ShadowLedger`` and the
injected fill/stop simulators).  A deterministic counting fake feed proves
the shared snapshot is fetched once per event while per-scenario copies
mutate independently.  No network, no Alpaca, no credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mike_trade.brokers.shadow.ledger import ShadowLedger
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import EntryBlockedError
from mike_trade.domain.trading import OrderIntent, OrderPurpose, OrderSide, OrderType, TimeInForce
from mike_trade.market_data.candles import PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext, QuoteSnapshot
from mike_trade.persistence.schema import initialize_database
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.shadow.scenarios import (
    ScenarioConfig,
    ScenarioProjection,
    ScenarioRegistry,
    ScenarioRunner,
)

D = Decimal
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)  # Thursday, 10:30 ET
BID = D("99.50")
ASK = D("100.00")
QTY = D("10")


class FakeClock:
    def now(self) -> datetime:
        return NOW


def make_quote(symbol: str, *, bid: Decimal = BID, ask: Decimal = ASK) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        bid=bid,
        ask=ask,
        bid_size=D("100"),
        ask_size=D("100"),
        provider_ts=NOW,
        receive_ts=NOW,
        feed_id="iex",
    )


def make_context(symbol: str, *, bid: Decimal = BID, ask: Decimal = ASK) -> MarketContext:
    quote = make_quote(symbol, bid=bid, ask=ask)
    return MarketContext(
        symbol=symbol,
        as_of=NOW,
        version=1,
        feed_id="iex",
        consolidated=False,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id="2026-10-08",
        freshness=ContextFreshness(
            newest_data_ts=NOW,
            age=timedelta(0),
            max_age=timedelta(seconds=30),
            stale=False,
            reason=None,
        ),
        candles={},
        indicators={},
        latest_quote=quote,
    )


class CountingFeed:
    """Shared-feed double: counts invocations, replays one immutable snapshot."""

    def __init__(self, snapshot: Mapping[str, MarketContext]) -> None:
        self._snapshot = dict(snapshot)
        self.calls = 0
        self.seen: list[tuple[str, ...]] = []

    async def snapshot(self, symbols: tuple[str, ...]) -> Mapping[str, MarketContext]:
        self.calls += 1
        self.seen.append(tuple(symbols))
        return self._snapshot


def buy_plugin(symbol: str, quantity: Decimal = QTY):
    """BUY strategy plugin submitting one MARKET entry for ``symbol`` only."""

    def _decide(config: ScenarioConfig, context: MarketContext) -> OrderIntent | None:
        if context.symbol != symbol:
            return None
        scope_id = config.scope.scope_id
        return OrderIntent(
            intent_id=f"{config.scenario_id}:{symbol}:buy:1",
            client_order_id=f"{config.scenario_id}-{symbol}-buy-1",
            account_scope_id=scope_id,
            cycle_id=f"{config.scenario_id}-{symbol}-c1",
            symbol=symbol,
            side=OrderSide.BUY,
            purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=NOW,
            quantity=quantity,
        )

    return _decide


def make_config(
    scenario_id: str,
    starting_cash: Decimal,
    *,
    per_symbol_budget: Decimal = D("2000"),
    total_budget: Decimal = D("3000"),
    buy: tuple[str, ...] = ("buy-ten",),
    sell: tuple[str, ...] = (),
) -> ScenarioConfig:
    return ScenarioConfig(
        scenario_id=scenario_id,
        starting_cash=starting_cash,
        per_symbol_budget=per_symbol_budget,
        total_budget=total_budget,
        buy_strategies=buy,
        sell_strategies=sell,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "shadow.db"
    initialize_database(path, AccountEnvironment.SHADOW, now=NOW).close()
    return path


def make_runner(
    db_path: Path, configs: Sequence[ScenarioConfig], *, buy_symbol: str = "AAPL"
) -> ScenarioRunner:
    runner = ScenarioRunner(db_path, clock=FakeClock(), max_concurrency=2)
    runner.register_strategy("buy-ten", buy_plugin(buy_symbol), side="BUY")
    for config in configs:
        runner.add_scenario(config)
    return runner


async def test_scenarios_settle_independently(db_path: Path) -> None:
    runner = make_runner(
        db_path,
        [make_config("alpha", D("10000")), make_config("beta", D("5000"))],
    )
    try:
        feed = CountingFeed({"AAPL": make_context("AAPL")})
        submitted = await runner.run_once(("AAPL",), feed=feed)
        assert sorted(submitted) == ["alpha", "beta"]
        assert feed.calls == 1

        alpha = await runner.projection("alpha")
        beta = await runner.projection("beta")
        # 10 shares @ 100.00 ask, zero fees: exact Decimal independence.
        assert alpha.cash == D("9000.00") or alpha.cash == D("9000")
        assert beta.cash == D("4000.00") or beta.cash == D("4000")
        assert alpha.cash - beta.cash == D("5000")
        for projection in (alpha, beta):
            assert len(projection.positions) == 1
            position = projection.positions[0]
            assert position.symbol == "AAPL"
            assert position.quantity == QTY
            assert position.avg_entry_price == ASK
    finally:
        runner.close()


async def test_mutation_isolation_between_scenarios(db_path: Path) -> None:
    active = make_config("active", D("10000"), buy=("buy-ten",))
    idle = make_config("idle", D("7000"), buy=())
    runner = make_runner(db_path, [active, idle])
    try:
        await runner.run_once(("AAPL",), feed=CountingFeed({"AAPL": make_context("AAPL")}))
        active_view = await runner.projection("active")
        idle_view = await runner.projection("idle")
        assert len(active_view.positions) == 1
        assert idle_view.positions == ()
        assert idle_view.cash == D("7000")
        fills = await runner.get_account("idle").list_fills(NOW - timedelta(minutes=1))
        assert fills == []
    finally:
        runner.close()


async def test_reload_preserves_cash_without_reapplying(db_path: Path) -> None:
    runner = make_runner(db_path, [make_config("alpha", D("10000"))])
    try:
        await runner.run_once(("AAPL",), feed=CountingFeed({"AAPL": make_context("AAPL")}))
        before = await runner.projection("alpha")
        assert before.cash != D("10000")

        runner.reload_scenario("alpha")
        after = await runner.projection("alpha")
        assert after.cash == before.cash
        # Quote marks are in-memory inputs, not ledger facts, so a bare reload
        # values the position at cost basis; cash and the position itself persist.
        assert [(p.symbol, p.quantity, p.avg_entry_price) for p in after.positions] == [
            (p.symbol, p.quantity, p.avg_entry_price) for p in before.positions
        ]
    finally:
        runner.close()

    # A fresh runner over the same database replays; starting cash is not reset.
    reopened = make_runner(db_path, [make_config("alpha", D("10000"))])
    try:
        replayed = await reopened.projection("alpha")
        assert replayed.cash == before.cash
        assert len(replayed.positions) == 1
    finally:
        reopened.close()


async def test_shared_snapshot_copied_per_scenario(db_path: Path) -> None:
    runner = make_runner(
        db_path,
        [make_config("alpha", D("10000")), make_config("beta", D("10000"))],
    )
    try:
        snapshot = {"AAPL": make_context("AAPL")}
        before = snapshot["AAPL"].latest_quote
        await runner.dispatch(snapshot)
        # The shared input is untouched by per-scenario fan-out.
        assert snapshot["AAPL"].latest_quote == before
        alpha = await runner.projection("alpha")
        beta = await runner.projection("beta")
        assert alpha.cash == beta.cash
        assert alpha.account_scope_id != beta.account_scope_id
    finally:
        runner.close()


def test_duplicate_and_reserved_ids_rejected(db_path: Path) -> None:
    registry = ScenarioRegistry()
    registry.register(make_config("alpha", D("10000")))
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(make_config("alpha", D("10000")))

    for reserved in ("PAPER", "paper", "LIVE", "live"):
        with pytest.raises(ValueError, match="[Cc]ollides|reserved"):
            make_config(reserved, D("10000"))

    runner = ScenarioRunner(db_path, clock=FakeClock())
    try:
        runner.add_scenario(make_config("alpha", D("10000")))
        with pytest.raises(ValueError, match="duplicate"):
            runner.add_scenario(make_config("alpha", D("10000")))
        with pytest.raises(ValueError, match="unknown scenario"):
            runner.get_account("ghost")
    finally:
        runner.close()


async def test_projections_keyed_shadow_estimated(db_path: Path) -> None:
    runner = make_runner(
        db_path,
        [make_config("alpha", D("10000")), make_config("beta", D("5000"))],
    )
    try:
        for scenario_id in ("alpha", "beta"):
            projection = await runner.projection(scenario_id)
            assert isinstance(projection, ScenarioProjection)
            assert projection.scenario_id == scenario_id
            assert projection.mode == "SHADOW"
            assert projection.estimated is True
            assert projection.broker_confirmed is False
            assert projection.account_scope_id == (
                f"shadow:ACCT:{scenario_id}"
            )
        # No aggregate/portfolio API may exist on the runner.
        for name in ("portfolio", "aggregate", "combined", "total_equity", "net_all"):
            assert not hasattr(runner, name), name
    finally:
        runner.close()


async def test_per_scenario_budgets_enforced(db_path: Path) -> None:
    # 10 shares @ ~100 needs 1000 notional; a 100 budget must block entry.
    tight = make_config("tight", D("10000"), per_symbol_budget=D("100"))
    runner = make_runner(db_path, [tight])
    try:
        with pytest.raises(EntryBlockedError):
            await runner.run_once(("AAPL",), feed=CountingFeed({"AAPL": make_context("AAPL")}))
        projection = await runner.projection("tight")
        assert projection.cash == D("10000")
        assert projection.positions == ()
    finally:
        runner.close()


def test_no_credentials_or_remote_references() -> None:
    import re

    source = Path("mike_trade/shadow/scenarios.py").read_text()
    lowered = source.lower()
    for banned in (
        "alpaca",
        "credential",
        "environ",
        "getenv",
        "http",
        "socket",
        "requests",
    ):
        assert re.search(rf"(?<![a-z]){banned}(?![a-z])", lowered) is None, banned
    ledger_source = Path("mike_trade/brokers/shadow/ledger.py").read_text().lower()
    assert "alpaca" not in ledger_source


def test_only_allowed_symbols_exported() -> None:
    import mike_trade.shadow.scenarios as scenarios

    assert sorted(scenarios.__all__) == [
        "ScenarioConfig",
        "ScenarioProjection",
        "ScenarioRegistry",
        "ScenarioRunner",
    ]


async def test_bounded_concurrent_fanout(db_path: Path) -> None:
    configs = [make_config(f"s{i}", D("10000")) for i in range(4)]
    runner = make_runner(db_path, configs)
    try:
        feed = CountingFeed({"AAPL": make_context("AAPL")})
        submitted = await asyncio.wait_for(
            runner.run_once(("AAPL",), feed=feed), timeout=30
        )
        assert feed.calls == 1
        assert sorted(submitted) == sorted(f"s{i}" for i in range(4))
        for i in range(4):
            projection = await runner.projection(f"s{i}")
            assert len(projection.positions) == 1
    finally:
        runner.close()


def test_shadow_ledger_scope_matches_scenario(db_path: Path) -> None:
    from mike_trade.persistence.transaction import WriterSession

    runner = make_runner(db_path, [make_config("alpha", D("10000"))])
    try:
        scope = runner.get_account("alpha").scope
        assert scope == AccountScope(AccountEnvironment.SHADOW, "ACCT", "alpha")
    finally:
        runner.close()
    # After close the single-writer lock is released; the ledger holds only
    # this scenario's events in its own scope stream.
    session = WriterSession(db_path)
    try:
        scope = AccountScope(AccountEnvironment.SHADOW, "ACCT", "alpha")
        ledger = ShadowLedger(session.bind_scope(scope.scope_id), scope, clock=FakeClock())
        events = ledger.read_events()
        assert events, "scenario ledger must hold this scenario's events only"
        assert {event.scope.scope_id for event in events} == {scope.scope_id}
    finally:
        session.close()
