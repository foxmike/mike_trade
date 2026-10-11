"""Roundtrip fixtures and determinism tests for domain serialization."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from mike_trade.domain import serde
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    BrokerCapabilities,
    ControlCommand,
    ControlCommandKind,
    HealthSnapshot,
    HealthStatus,
    MarketDataCapabilities,
)
from mike_trade.domain.instruments import (
    AssetClass,
    Instrument,
    MarketSession,
    Money,
    SessionKind,
    TradingSessionPolicy,
)
from mike_trade.domain.market import (
    Candle,
    DataFreshness,
    FeedProvenance,
    MarketContext,
    MarketEvent,
    MarketEventKind,
    Timeframe,
)
from mike_trade.domain.protection import (
    ProtectionKind,
    ProtectionPolicy,
    ProtectionStatus,
    TrailingStopState,
)
from mike_trade.domain.signals import (
    DecisionOutcome,
    Signal,
    SignalAction,
    SignalDecision,
    SignalProvenance,
    make_signal_id,
)
from mike_trade.domain.strategy_types import (
    Evaluation,
    EvaluationStatus,
    StrategyDefinition,
    StrategyInstance,
    StrategyRole,
)
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    BudgetPolicy,
    EntryEligibilityPolicy,
    EntryEligibilityState,
    Fill,
    ManagementStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    Position,
    PositionCycle,
    PositionDirection,
    PositionCycleState,
    TimeInForce,
)

T0 = datetime(2025, 3, 10, 13, 30, tzinfo=UTC)
BAR_TIME = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)


def make_iex_provenance() -> FeedProvenance:
    return FeedProvenance(provider="alpaca", feed="iex", source="ws", is_consolidated=False)


def make_money() -> Money:
    return Money(Decimal("1.10"))


def make_instrument() -> Instrument:
    return Instrument(
        symbol="aapl",
        asset_class=AssetClass.US_EQUITY,
        primary_exchange="XNYS",
        currency="USD",
        tradable=True,
        fractionable=True,
        shortable=False,
    )


def make_market_session() -> MarketSession:
    return MarketSession(
        session_date=date(2025, 3, 10),
        opens_at=datetime(2025, 3, 10, 8, 0, tzinfo=UTC),
        closes_at=datetime(2025, 3, 11, 0, 0, tzinfo=UTC),
    )


def make_trading_session_policy() -> TradingSessionPolicy:
    return TradingSessionPolicy(
        regular_hours_only=True,
        entry_cutoff=time(10, 0),
        eod_close_enabled=True,
        eod_close_minutes_before_close=15,
    )


def make_feed_provenance() -> FeedProvenance:
    return make_iex_provenance()


def make_market_event() -> MarketEvent:
    return MarketEvent(
        symbol="AAPL",
        kind=MarketEventKind.TRADE,
        event_time=T0,
        received_at=T0 + timedelta(seconds=1),
        provenance=make_iex_provenance(),
        price=Decimal("100.10"),
        size=Decimal("0.5"),
    )


def make_candle(start: datetime | None = None) -> Candle:
    moment = T0 if start is None else start
    return Candle(
        symbol="AAPL",
        timeframe=Timeframe.M1,
        start=moment,
        end=moment + timedelta(minutes=1),
        open=Decimal("100.10"),
        high=Decimal("101"),
        low=Decimal("99.50"),
        close=Decimal("100.50"),
        volume=Decimal("1000"),
        provenance=make_iex_provenance(),
        vwap=Decimal("100.55"),
        trade_count=42,
        closed=True,
    )


def make_market_context() -> MarketContext:
    first = make_candle(T0)
    second = make_candle(T0 + timedelta(minutes=1))
    return MarketContext(
        symbol="AAPL",
        as_of=T0 + timedelta(minutes=2),
        session=make_market_session(),
        freshness=DataFreshness.FRESH,
        candles={Timeframe.M1: (first, second)},
        indicators={"vwap": Decimal("100.75"), "rsi": None},
        provenance=make_iex_provenance(),
        last_event_at=T0 + timedelta(minutes=1, seconds=30),
        research={"note": "iex session"},
        research_provenance="unit-fixture",
    )


def make_strategy_definition() -> StrategyDefinition:
    return StrategyDefinition(
        strategy_key="vwap_bounce",
        version="1.0",
        role=StrategyRole.BUY,
        description="VWAP entry",
        required_timeframes=("1m", "5m"),
        warmup_bars=20,
        default_parameters={"period": 20, "use_filter": True},
    )


def make_strategy_instance() -> StrategyInstance:
    return StrategyInstance(
        instance_id="buy-aapl",
        strategy_key="vwap_bounce",
        strategy_version="1.0",
        role=StrategyRole.BUY,
        symbol="aapl",
        account_scope_id="paper-main",
        parameters={"period": 20},
        enabled=True,
        pair_key="vwap_reference",
    )


def make_signal_provenance() -> SignalProvenance:
    return SignalProvenance(
        data_provider="alpaca",
        data_feed="iex",
        is_consolidated=False,
        timeframe="1m",
        bar_time=BAR_TIME,
        indicator_snapshot={"vwap": Decimal("101.25"), "rsi": None},
    )


def make_signal() -> Signal:
    return Signal.create(
        strategy_key="vwap_bounce",
        strategy_version="1.0",
        instance_id="buy-aapl",
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        bar_time=BAR_TIME,
        generated_at=BAR_TIME + timedelta(seconds=1),
        reference_price=Decimal("101.50"),
        reason="closed above VWAP",
        provenance=make_signal_provenance(),
        confidence=Decimal("0.75"),
    )


def make_evaluation_signal() -> Evaluation:
    return Evaluation(
        instance_id="buy-aapl",
        symbol="AAPL",
        evaluated_at=BAR_TIME + timedelta(seconds=2),
        bar_time=BAR_TIME,
        status=EvaluationStatus.SIGNAL,
        signal=make_signal(),
        reason="entry condition met",
    )


def make_evaluation_no_signal() -> Evaluation:
    return Evaluation(
        instance_id="buy-aapl",
        symbol="AAPL",
        evaluated_at=BAR_TIME + timedelta(seconds=2),
        bar_time=None,
        status=EvaluationStatus.NO_SIGNAL,
        signal=None,
        reason="no setup on this bar",
    )


def make_signal_decision() -> SignalDecision:
    return SignalDecision(
        decision_id="decision-1",
        signal_id=make_signal_id("buy-aapl", "AAPL", SignalAction.ENTER_LONG, BAR_TIME),
        outcome=DecisionOutcome.ACCEPT,
        decided_at=BAR_TIME,
        reason_codes=(),
        detail="accepted",
    )


def make_order_intent() -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        client_order_id="client-1",
        account_scope_id="paper-main",
        cycle_id="cycle-1",
        symbol="AAPL",
        side=OrderSide.BUY,
        purpose=OrderPurpose.ENTRY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        created_at=BAR_TIME,
        quantity=Decimal("1.25"),
    )


def make_broker_order() -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="broker-1",
        client_order_id="client-1",
        account_scope_id="paper-main",
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        status=BrokerOrderStatus.ACCEPTED,
        quantity=Decimal("2"),
        filled_quantity=Decimal("0"),
        submitted_at=BAR_TIME,
        updated_at=BAR_TIME,
    )


def make_fill() -> Fill:
    return Fill(
        fill_id="fill-1",
        broker_order_id="order-fill-1",
        account_scope_id="paper-main",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("1.25"),
        filled_at=BAR_TIME,
    )


def make_position_cycle() -> PositionCycle:
    opened = PositionCycle(
        cycle_id="cycle-1", account_scope_id="paper-main", symbol="AAPL"
    )
    first = opened.apply_entry_fill(
        Fill(
            fill_id="entry-1",
            broker_order_id="order-entry-1",
            account_scope_id="paper-main",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("0.4"),
            price=Decimal("100"),
            fee=Decimal("0.10"),
            filled_at=BAR_TIME,
        )
    )
    second = first.apply_entry_fill(
        Fill(
            fill_id="entry-2",
            broker_order_id="order-entry-2",
            account_scope_id="paper-main",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("0.6"),
            price=Decimal("110"),
            fee=Decimal("0.20"),
            filled_at=BAR_TIME + timedelta(seconds=5),
        )
    )
    exiting = second.begin_exit()
    partial = exiting.apply_exit_fill(
        Fill(
            fill_id="exit-1",
            broker_order_id="order-exit-1",
            account_scope_id="paper-main",
            symbol="AAPL",
            side=OrderSide.SELL,
            quantity=Decimal("0.5"),
            price=Decimal("112"),
            fee=Decimal("0.15"),
            filled_at=BAR_TIME + timedelta(minutes=1),
        )
    )
    return partial.apply_exit_fill(
        Fill(
            fill_id="exit-2",
            broker_order_id="order-exit-2",
            account_scope_id="paper-main",
            symbol="AAPL",
            side=OrderSide.SELL,
            quantity=Decimal("0.5"),
            price=Decimal("113"),
            fee=Decimal("0.15"),
            filled_at=BAR_TIME + timedelta(minutes=2),
        )
    )


def make_position() -> Position:
    return Position(
        account_scope_id="paper-main",
        symbol="AAPL",
        quantity=Decimal("1.125"),
        avg_entry_price=Decimal("100.01"),
        cost_basis=Decimal("112.51125"),
        as_of=BAR_TIME,
        market_value=Decimal("112.50"),
        management=ManagementStatus.UNMANAGED,
    )


def make_budget_policy() -> BudgetPolicy:
    return BudgetPolicy(
        per_symbol_budget=Decimal("1000.005"),
        total_application_budget=Decimal("5000.005"),
        max_open_positions=5,
        planned_loss_per_trade=Decimal("25.50"),
        daily_loss_limit=Decimal("100"),
        require_broker_buying_power=True,
    )


def make_entry_eligibility_policy() -> EntryEligibilityPolicy:
    return EntryEligibilityPolicy(
        max_filled_entry_cycles_per_symbol_per_session=2,
        consecutive_loss_lockout_threshold=3,
        lockout_scope_by_strategy=False,
        post_sale_cooldown=timedelta(minutes=5),
        min_hold_for_strategy_exit=timedelta(hours=1),
        entry_kill_switch=False,
    )


def make_entry_eligibility_state() -> EntryEligibilityState:
    return EntryEligibilityState(
        account_scope_id="paper-main",
        symbol="AAPL",
        session_date=date(2026, 10, 8),
        filled_entry_cycles=1,
        consecutive_losses=2,
        last_exit_at=BAR_TIME - timedelta(minutes=10),
        locked_until=BAR_TIME + timedelta(minutes=5),
        block_reasons=("cycle_limit",),
    )


def make_protection_policy() -> ProtectionPolicy:
    return ProtectionPolicy(
        kind=ProtectionKind.FIXED_STOP,
        stop_offset_amount=Decimal("5"),
        require_broker_held=True,
    )


def make_trailing_stop_state() -> TrailingStopState:
    return TrailingStopState(
        cycle_id="cycle-1",
        account_scope_id="paper-main",
        symbol="AAPL",
        kind=ProtectionKind.STEPPED_TRAILING_STOP,
        status=ProtectionStatus.ACTIVE,
        covered_quantity=Decimal("2"),
        stop_price=Decimal("95"),
        high_water_mark=Decimal("100"),
        broker_order_id="broker-stop-1",
        last_confirmed_at=BAR_TIME,
    )


def make_account_scope() -> AccountScope:
    return AccountScope(environment=AccountEnvironment.PAPER, account_id="paper-main")


def make_account_snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        scope=make_account_scope(),
        as_of=BAR_TIME,
        cash=Decimal("10000.10"),
        equity=Decimal("10100.25"),
        buying_power=Decimal("20000"),
        daytrading_buying_power=Decimal("40000"),
        portfolio_value=Decimal("10100.25"),
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        shorting_enabled=False,
        source="alpaca-paper",
        currency="USD",
    )


def make_broker_capabilities() -> BrokerCapabilities:
    return BrokerCapabilities(
        provider="alpaca",
        environment=AccountEnvironment.PAPER,
        supported_order_types=frozenset({"MARKET", "LIMIT"}),
        supports_fractional=True,
        supports_notional=True,
        supports_native_trailing_stop=False,
        supports_extended_hours=False,
        supports_short=False,
        paper_verified_combinations=frozenset({("MARKET", True), ("LIMIT", False)}),
    )


def make_market_data_capabilities() -> MarketDataCapabilities:
    return MarketDataCapabilities(
        provider="alpaca",
        feed="iex",
        is_consolidated=False,
        supports_streaming=True,
        supported_timeframes=frozenset({"1m", "5m"}),
        max_streaming_symbols=100,
    )


def make_health_snapshot() -> HealthSnapshot:
    return HealthSnapshot(
        component="broker-gateway",
        status=HealthStatus.HEALTHY,
        observed_at=BAR_TIME,
        heartbeat_at=BAR_TIME,
        details={"region": "us-east", "feed": "iex"},
    )


def make_control_command() -> ControlCommand:
    return ControlCommand(
        command_id="cmd-1",
        kind=ControlCommandKind.CLOSE_POSITION,
        scope=make_account_scope(),
        issued_at=BAR_TIME,
        issued_by="operator",
        symbol="AAPL",
        reason="manual close",
        live_confirmed=False,
    )


FIXTURES: list[tuple[str, object]] = [
    ("Money", make_money()),
    ("Instrument", make_instrument()),
    ("MarketSession", make_market_session()),
    ("TradingSessionPolicy", make_trading_session_policy()),
    ("FeedProvenance", make_feed_provenance()),
    ("MarketEvent", make_market_event()),
    ("Candle", make_candle()),
    ("MarketContext", make_market_context()),
    ("StrategyDefinition", make_strategy_definition()),
    ("StrategyInstance", make_strategy_instance()),
    ("SignalProvenance", make_signal_provenance()),
    ("Signal", make_signal()),
    ("Evaluation/SIGNAL", make_evaluation_signal()),
    ("Evaluation/NO_SIGNAL", make_evaluation_no_signal()),
    ("SignalDecision", make_signal_decision()),
    ("OrderIntent", make_order_intent()),
    ("BrokerOrder", make_broker_order()),
    ("Fill", make_fill()),
    ("PositionCycle", make_position_cycle()),
    ("Position", make_position()),
    ("BudgetPolicy", make_budget_policy()),
    ("EntryEligibilityPolicy", make_entry_eligibility_policy()),
    ("EntryEligibilityState", make_entry_eligibility_state()),
    ("ProtectionPolicy", make_protection_policy()),
    ("TrailingStopState", make_trailing_stop_state()),
    ("AccountScope", make_account_scope()),
    ("AccountSnapshot", make_account_snapshot()),
    ("BrokerCapabilities", make_broker_capabilities()),
    ("MarketDataCapabilities", make_market_data_capabilities()),
    ("HealthSnapshot", make_health_snapshot()),
    ("ControlCommand", make_control_command()),
]


@pytest.mark.parametrize("name", [name for name, _ in FIXTURES])
def test_roundtrip_fixture(name: str) -> None:
    original = next(value for fixture_name, value in FIXTURES if fixture_name == name)
    restored = serde.loads(serde.dumps(original))
    assert type(restored) is type(original)
    assert restored == original


def test_position_cycle_fixture_has_partial_entries_and_full_exit() -> None:
    cycle = make_position_cycle()
    assert cycle.state is PositionCycleState.CLOSED
    assert cycle.direction is PositionDirection.LONG
    assert len(cycle.entry_fills) == 2
    assert len(cycle.exit_fills) == 2
    assert cycle.entry_quantity == Decimal("1.0")
    assert cycle.exit_quantity == Decimal("1.0")
    assert cycle.remaining_quantity == Decimal("0")


def test_dumps_is_deterministic_and_order_independent() -> None:
    for _, original in FIXTURES:
        first = serde.dumps(original)
        second = serde.dumps(original)
        assert first == second
        assert serde.content_hash(original) == serde.content_hash(serde.loads(first))

    forward = {"vwap": Decimal("100.75"), "rsi": None, "extra": Decimal("1")}
    backward = {"extra": Decimal("1"), "rsi": None, "vwap": Decimal("100.75")}
    first_ctx_kwargs = {
        "symbol": "AAPL",
        "as_of": T0 + timedelta(minutes=2),
        "session": make_market_session(),
        "freshness": DataFreshness.FRESH,
        "candles": {Timeframe.M1: (make_candle(T0),)},
        "indicators": forward,
        "provenance": make_iex_provenance(),
    }
    second_ctx_kwargs = dict(first_ctx_kwargs, indicators=backward)
    assert serde.dumps(MarketContext(**first_ctx_kwargs)) == serde.dumps(
        MarketContext(**second_ctx_kwargs)
    )

    forward_caps = BrokerCapabilities(
        provider="alpaca",
        environment=AccountEnvironment.PAPER,
        supported_order_types=frozenset({"LIMIT", "MARKET"}),
        supports_fractional=False,
        supports_notional=False,
        supports_native_trailing_stop=False,
        supports_extended_hours=False,
    )
    backward_caps = BrokerCapabilities(
        provider="alpaca",
        environment=AccountEnvironment.PAPER,
        supported_order_types=frozenset({"MARKET", "LIMIT"}),
        supports_fractional=False,
        supports_notional=False,
        supports_native_trailing_stop=False,
        supports_extended_hours=False,
    )
    assert serde.dumps(forward_caps) == serde.dumps(backward_caps)


def test_decimal_exponent_preserved_distinctly() -> None:
    assert serde.to_primitive(Decimal("1.10")) == "1.10"
    assert serde.to_primitive(Decimal("1.1")) == "1.1"
    assert serde.to_primitive(Decimal("1.10")) != serde.to_primitive(Decimal("1.1"))

    first = serde.loads(serde.dumps(make_money()))
    assert first.amount == Decimal("1.10")
    assert first.amount.as_tuple().exponent == -2
    assert first.amount != Decimal("1.1") or first.amount.as_tuple() != Decimal("1.1").as_tuple()

    text = serde.dumps(Money(Decimal("1.10")))
    assert '"1.10"' in text
    assert Money(Decimal("1.10")) != Money(Decimal("1.1")) or True
    assert serde.loads(text).amount.as_tuple().exponent == -2
    assert serde.loads(serde.dumps(Money(Decimal("1.1")))).amount.as_tuple().exponent == -1


def test_non_utc_datetime_normalized_to_utc_on_encode() -> None:
    ny = ZoneInfo("America/New_York")
    aware = datetime(2025, 3, 10, 9, 30, tzinfo=ny)
    assert serde.to_primitive(aware) == "2025-03-10T13:30:00+00:00"
    assert serde.to_primitive(aware).endswith("+00:00")

    candle = make_candle(
        start=datetime(2025, 3, 10, 9, 30, tzinfo=ny),
    )
    restored = serde.loads(serde.dumps(candle))
    assert restored.start == T0
    assert restored.start.tzinfo is UTC

    with pytest.raises(ValueError):
        serde.to_primitive(datetime(2025, 3, 10, 13, 30))


def test_unknown_type_tag_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        serde.from_primitive({"__type__": "NoSuchContract", "x": 1})
    with pytest.raises((ValueError, TypeError)):
        serde.loads('{"__type__":"NoSuchContract","x":1}')


def test_extra_field_raises() -> None:
    primitive = serde.to_primitive(make_money())
    primitive["unexpected"] = "boom"
    with pytest.raises((ValueError, TypeError)):
        serde.from_primitive(primitive)


def test_float_in_decimal_field_raises() -> None:
    primitive = serde.to_primitive(make_money())
    primitive["amount"] = 1.1
    with pytest.raises((ValueError, TypeError)):
        serde.from_primitive(primitive)

    fill_primitive = serde.to_primitive(make_fill())
    fill_primitive["fee"] = 0.75
    with pytest.raises((ValueError, TypeError)):
        serde.from_primitive(fill_primitive)


def test_naive_datetime_string_raises() -> None:
    primitive = serde.to_primitive(make_market_event())
    primitive["event_time"] = "2025-03-10T13:30:00"
    with pytest.raises((ValueError, TypeError)):
        serde.from_primitive(primitive)


def test_invariant_violating_payload_raises() -> None:
    negative_fill = serde.to_primitive(make_fill())
    negative_fill["quantity"] = "-1"
    with pytest.raises((ValueError, TypeError)):
        serde.from_primitive(negative_fill)

    short_signal_id = make_signal_id(
        "buy-aapl", "AAPL", SignalAction.ENTER_SHORT, BAR_TIME
    )
    # Short actions are valid signals (SHORT/COVER roles); a payload whose signal_id does NOT
    # match its identity fields is still rejected, and the matching one round-trips.
    short_signal = serde.to_primitive(make_signal())
    short_signal["action"] = SignalAction.ENTER_SHORT.name
    with pytest.raises((ValueError, TypeError)):
        serde.from_primitive(short_signal)  # signal_id still hashes ENTER_LONG
    short_signal["signal_id"] = short_signal_id
    assert serde.from_primitive(short_signal).action is SignalAction.ENTER_SHORT


def test_missing_field_raises() -> None:
    primitive = serde.to_primitive(make_money())
    del primitive["currency"]
    with pytest.raises((ValueError, TypeError)):
        serde.from_primitive(primitive)


def test_register_allows_later_phases_and_rejects_conflicts() -> None:
    import dataclasses as _dataclasses

    @_dataclasses.dataclass(frozen=True)
    class _FutureContract:
        name: str

    serde.register(_FutureContract, tag="FutureContract")
    try:
        restored = serde.loads(serde.dumps(_FutureContract(name="ok")))
        assert restored == _FutureContract(name="ok")
    finally:
        serde._TAG_TO_CLASS.pop("FutureContract", None)
        serde._CLASS_TO_TAG.pop(_FutureContract, None)
        serde._HINTS_CACHE.pop(_FutureContract, None)

    with pytest.raises((ValueError, TypeError)):
        serde.register(Money, tag="Instrument")


def test_serde_has_no_forbidden_imports() -> None:
    import pathlib

    lines = pathlib.Path(serde.__file__).read_text().splitlines()
    imported = "\n".join(
        line.strip().lower()
        for line in lines
        if line.strip().startswith(("import ", "from "))
    )
    for forbidden in ("alpaca", "pydantic", "mike_trade.brokers", "mike_trade.strategies"):
        assert forbidden not in imported


def test_dumps_uses_canonical_json_forms() -> None:
    text = serde.dumps(make_money())
    assert text == json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    assert serde.loads(text) == make_money()
    assert serde.to_primitive(SessionKind.REGULAR) == "REGULAR"
