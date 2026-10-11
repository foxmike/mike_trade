import ast
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from mike_trade.domain.signals import SignalAction
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import ManagementStatus, Position, PositionDirection
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterSchema,
    ReasonCode,
    SellStrategy,
    StrategySpec,
    TriggerKind,
    make_signal_id,
)
from mike_trade.strategies.sell.base import (
    EXIT_PRIORITY,
    ExitLongRecommendation,
    ExitSource,
    SellReasonCode,
    SellRule,
    evaluate_exit_eligibility,
    min_hold_applies,
)


BAR = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
OPENED = BAR - timedelta(minutes=30)
NOW = BAR


def spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="test_sell",
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="momentum",
        reference_symbols=("AAPL",),
        required_timeframes=(CandleTimeframe.M5,),
        warmup_bars=2,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M5,
        parameter_schema=ParameterSchema(()),
        persistent_state_keys=(),
        methodology="contract test",
        source="test",
        execution_limitations=("Long-only",),
        reason_codes=(ReasonCode.SIGNAL, ReasonCode.NO_SIGNAL, ReasonCode.INSUFFICIENT_DATA,
                      ReasonCode.STALE_DATA, ReasonCode.NOT_ELIGIBLE),
    )


def position(management=ManagementStatus.MANAGED) -> Position:
    return Position(
        account_scope_id="acct",
        symbol="AAPL",
        quantity=Decimal("7.5"),
        avg_entry_price=Decimal("100"),
        cost_basis=Decimal("750"),
        as_of=NOW,
        management=management,
        cycle_id="cycle-1" if management is ManagementStatus.MANAGED else None,
    )


def context(*, stale=False, completed=True, bars=2):
    return SimpleNamespace(
        freshness=SimpleNamespace(stale=stale),
        completed_bars_only=completed,
        feed_id="sip",
        candles={CandleTimeframe.M5: tuple(range(bars))},
        symbol="AAPL",
    )


def eligibility(**overrides):
    args = dict(
        position=position(),
        confirmed_remaining_quantity=Decimal("7.5"),
        enabled=True,
        context=context(),
        spec=spec(),
        source=ExitSource.STRATEGY_SELL,
        minimum_hold=timedelta(0),
        evaluation_time=NOW,
        position_opened_at=OPENED,
    )
    args.update(overrides)
    return evaluate_exit_eligibility(**args)


def test_recommendation_carries_exact_full_fractional_remainder_and_is_immutable():
    result = ExitLongRecommendation(
        signal_id=make_signal_id("test_sell", "1.0.0", "AAPL", StrategyRole.SELL, BAR),
        strategy_id="test_sell",
        version="1.0.0",
        position=position(),
        bar_time=BAR,
        reason_codes=(SellReasonCode.ELIGIBLE,),
    )
    assert result.quantity == Decimal("7.5")
    assert result.action is SignalAction.EXIT_LONG
    with pytest.raises(FrozenInstanceError):
        result.quantity = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"position": None}, SellReasonCode.FLAT_POSITION),
        ({"position": position(ManagementStatus.UNMANAGED)}, SellReasonCode.UNMANAGED_POSITION),
        ({"confirmed_remaining_quantity": Decimal("0")}, SellReasonCode.NON_POSITIVE_QUANTITY),
        ({"confirmed_remaining_quantity": Decimal("-1")}, SellReasonCode.NON_POSITIVE_QUANTITY),
        ({"direction": PositionDirection.SHORT}, SellReasonCode.NOT_LONG),
    ],
)
def test_ineligible_position_never_yields_eligibility(updates, expected):
    result = eligibility(**updates)
    assert result[0] is SellReasonCode.NOT_ELIGIBLE
    assert expected in result


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"enabled": False}, SellReasonCode.STRATEGY_DISABLED),
        ({"context": None}, SellReasonCode.NO_DATA),
        ({"context": context(stale=True)}, SellReasonCode.STALE_DATA),
        ({"context": context(completed=False)}, SellReasonCode.INCOMPLETE_DATA),
        ({"context": context(bars=1)}, SellReasonCode.NOT_WARMED),
    ],
)
def test_disabled_or_unusable_data_is_no_signal(updates, expected):
    result = eligibility(**updates)
    assert result[0] is SellReasonCode.NO_SIGNAL
    assert expected in result


@pytest.mark.parametrize("source", tuple(ExitSource))
def test_minimum_hold_only_blocks_strategy_exits(source):
    result = eligibility(
        source=source,
        minimum_hold=timedelta(hours=1),
        evaluation_time=NOW,
        position_opened_at=OPENED,
    )
    if source in (ExitSource.STRATEGY_SELL, ExitSource.STRATEGY_COVER):
        assert result[0] is SellReasonCode.NOT_ELIGIBLE
        assert SellReasonCode.MINIMUM_HOLD in result
    else:
        assert result == (SellReasonCode.ELIGIBLE,)
        assert min_hold_applies(source) is False


def test_minimum_hold_uses_injected_timestamps_and_allows_elapsed_period():
    result = eligibility(
        minimum_hold=timedelta(minutes=30),
        evaluation_time=NOW,
        position_opened_at=OPENED,
    )
    assert result == (SellReasonCode.ELIGIBLE,)
    assert min_hold_applies(ExitSource.STRATEGY_SELL)


def test_exit_priority_is_stable_and_every_non_strategy_source_outranks_sell():
    assert tuple(EXIT_PRIORITY) == tuple(ExitSource)
    strategy_exits = (ExitSource.STRATEGY_SELL, ExitSource.STRATEGY_COVER)
    strategy_priority = EXIT_PRIORITY[ExitSource.STRATEGY_SELL]
    assert EXIT_PRIORITY[ExitSource.STRATEGY_COVER] == strategy_priority  # same tier
    assert all(EXIT_PRIORITY[source] < strategy_priority for source in ExitSource if source not in strategy_exits)


def test_signal_id_is_deterministic_and_changes_with_completed_bar_time():
    first = make_signal_id("test_sell", "1.0.0", "AAPL", StrategyRole.SELL, BAR)
    again = make_signal_id("test_sell", "1.0.0", "AAPL", StrategyRole.SELL, BAR)
    next_bar = make_signal_id(
        "test_sell", "1.0.0", "AAPL", StrategyRole.SELL, BAR + timedelta(minutes=5)
    )
    assert first == again
    assert first != next_bar


def test_recommendation_has_no_partial_confidence_order_or_broker_fields():
    names = {field.name for field in fields(ExitLongRecommendation)}
    assert not any(
        token in name.lower()
        for name in names
        for token in ("partial", "confidence", "probability", "order", "broker")
    )
    assert "quantity" in names
    assert ExitLongRecommendation.__dataclass_params__.frozen


def test_sell_rule_is_a_sellstrategy_contract_and_trigger_is_abstract():
    assert issubclass(SellRule, SellStrategy)
    assert "evaluate_trigger" in SellRule.__abstractmethods__


def test_base_imports_no_execution_or_secret_modules():
    path = Path(__file__).parents[2] / "mike_trade/strategies/sell/base.py"
    tree = ast.parse(path.read_text())
    forbidden = ("broker", "adapter", "persistence", "http", "secret")
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name.lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.module or "").lower())
    assert not [name for name in imports if any(word in name for word in forbidden)]
