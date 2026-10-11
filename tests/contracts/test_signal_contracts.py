from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mike_trade.domain.signals import (
    ENABLED_SIGNAL_ACTIONS,
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

BAR_TIME = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)


def provenance(**overrides: object) -> SignalProvenance:
    values: dict[str, object] = {
        "data_provider": "alpaca",
        "data_feed": "iex",
        "is_consolidated": False,
        "timeframe": "1m",
        "bar_time": BAR_TIME,
        "indicator_snapshot": {"vwap": Decimal("101.25"), "rsi": None},
    }
    values.update(overrides)
    return SignalProvenance(**values)  # type: ignore[arg-type]


def signal(**overrides: object) -> Signal:
    values: dict[str, object] = {
        "strategy_key": "vwap_bounce",
        "strategy_version": "1.0",
        "instance_id": "buy-aapl",
        "symbol": "AAPL",
        "action": SignalAction.ENTER_LONG,
        "bar_time": BAR_TIME,
        "generated_at": BAR_TIME + timedelta(seconds=1),
        "reference_price": Decimal("101.50"),
        "reason": "closed above VWAP",
        "provenance": provenance(),
    }
    values.update(overrides)
    return Signal.create(**values)  # type: ignore[arg-type]


def test_strategy_contracts_are_frozen_and_parameter_mappings_are_read_only() -> None:
    defaults = {"period": 20}
    definition = StrategyDefinition(
        strategy_key="vwap_bounce",
        version="1.0",
        role=StrategyRole.BUY,
        description="VWAP entry",
        required_timeframes=("1m",),
        warmup_bars=20,
        default_parameters=defaults,
    )
    instance = StrategyInstance(
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

    defaults["period"] = 30
    assert definition.default_parameters["period"] == 20
    assert instance.symbol == "AAPL"
    with pytest.raises(TypeError):
        definition.default_parameters["new"] = True  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        instance.enabled = False  # type: ignore[misc]


@pytest.mark.parametrize("warmup", [-1, True])
def test_strategy_definition_rejects_invalid_warmup(warmup: object) -> None:
    with pytest.raises(ValueError):
        StrategyDefinition(
            strategy_key="vwap_bounce",
            version="1.0",
            role=StrategyRole.BUY,
            description="",
            required_timeframes=("1m",),
            warmup_bars=warmup,  # type: ignore[arg-type]
            default_parameters={},
        )


def test_evaluation_status_has_exact_members() -> None:
    assert [member.name for member in EvaluationStatus] == [
        "NO_SIGNAL",
        "SIGNAL",
        "INSUFFICIENT_DATA",
        "STALE_DATA",
        "ERROR",
        "NOT_DUE",
    ]


def test_all_four_signal_actions_are_valid_signals() -> None:
    # SHORT/COVER strategies signal ENTER_SHORT/COVER_SHORT.  Whether such a signal may become
    # an order is decided downstream and fails closed (portfolio short_authorized switch, broker
    # short authorization, shortability/margin/protection checks), never by the signal type.
    assert ENABLED_SIGNAL_ACTIONS == frozenset(SignalAction)
    assert {a.value for a in SignalAction} == {
        "ENTER_LONG", "EXIT_LONG", "ENTER_SHORT", "COVER_SHORT"}


def test_decision_outcome_has_exact_members() -> None:
    assert [member.name for member in DecisionOutcome] == ["ACCEPT", "REJECT", "IGNORE", "DEFER"]


@pytest.mark.parametrize("action", [SignalAction.ENTER_SHORT, SignalAction.COVER_SHORT])
def test_short_signal_actions_create_valid_deterministic_signals(action: SignalAction) -> None:
    created = signal(action=action)
    assert created.action is action
    assert created.signal_id == make_signal_id("buy-aapl", "AAPL", action, BAR_TIME)
    assert created.signal_id != signal().signal_id  # distinct identity per action


def test_signal_ids_are_deterministic_and_change_by_bar_or_action() -> None:
    original = make_signal_id("buy-aapl", "AAPL", SignalAction.ENTER_LONG, BAR_TIME)
    duplicate = make_signal_id("buy-aapl", "aapl", SignalAction.ENTER_LONG, BAR_TIME)
    later_bar = make_signal_id(
        "buy-aapl", "AAPL", SignalAction.ENTER_LONG, BAR_TIME + timedelta(minutes=1)
    )
    other_action = make_signal_id("buy-aapl", "AAPL", SignalAction.EXIT_LONG, BAR_TIME)

    assert original == duplicate
    assert original != later_bar
    assert original != other_action
    assert len(original) == 64
    assert set(original) <= set("0123456789abcdef")


def test_signal_create_defaults_confidence_to_none_and_freezes_snapshot() -> None:
    snapshot = {"vwap": Decimal("101.25")}
    created = signal(provenance=provenance(indicator_snapshot=snapshot))

    snapshot["vwap"] = Decimal("999")
    assert created.confidence is None
    assert created.provenance.indicator_snapshot["vwap"] == Decimal("101.25")
    with pytest.raises(TypeError):
        created.provenance.indicator_snapshot["rsi"] = Decimal("50")  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        created.reason = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "confidence",
    [
        Decimal("-0.01"),
        Decimal("1.01"),
        0.5,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_signal_rejects_invalid_confidence(confidence: object) -> None:
    with pytest.raises(ValueError):
        signal(confidence=confidence)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("reference_price", Decimal("0")),
        ("reference_price", Decimal("-0.01")),
        ("reference_price", 101.5),
        ("reference_price", Decimal("NaN")),
        ("reference_price", Decimal("Infinity")),
    ],
)
def test_signal_rejects_invalid_reference_price(field_name: str, value: object) -> None:
    with pytest.raises(ValueError):
        signal(**{field_name: value})


def test_direct_signal_construction_rejects_mismatched_id() -> None:
    created = signal()
    values = {field.name: getattr(created, field.name) for field in fields(Signal)}
    values["signal_id"] = "0" * 64

    with pytest.raises(ValueError, match="signal_id"):
        Signal(**values)


def test_signal_requires_generated_at_not_before_bar() -> None:
    with pytest.raises(ValueError, match="generated_at"):
        signal(generated_at=BAR_TIME - timedelta(microseconds=1))


def test_evaluation_signal_consistency_and_matching_identity() -> None:
    created = signal()
    evaluation = Evaluation(
        instance_id="buy-aapl",
        symbol="AAPL",
        evaluated_at=BAR_TIME + timedelta(seconds=2),
        bar_time=BAR_TIME,
        status=EvaluationStatus.SIGNAL,
        signal=created,
        reason="entry condition met",
    )

    assert evaluation.signal is created
    with pytest.raises(ValueError, match="require a signal"):
        Evaluation(
            instance_id="buy-aapl",
            symbol="AAPL",
            evaluated_at=BAR_TIME,
            bar_time=BAR_TIME,
            status=EvaluationStatus.SIGNAL,
            signal=None,
            reason="bad result",
        )
    with pytest.raises(ValueError, match="must not contain"):
        Evaluation(
            instance_id="buy-aapl",
            symbol="AAPL",
            evaluated_at=BAR_TIME,
            bar_time=BAR_TIME,
            status=EvaluationStatus.NO_SIGNAL,
            signal=created,
            reason="bad result",
        )
    with pytest.raises(ValueError, match="instance_id"):
        Evaluation(
            instance_id="different-instance",
            symbol="AAPL",
            evaluated_at=BAR_TIME,
            bar_time=BAR_TIME,
            status=EvaluationStatus.SIGNAL,
            signal=created,
            reason="bad result",
        )
    with pytest.raises(ValueError, match="symbol"):
        Evaluation(
            instance_id="buy-aapl",
            symbol="MSFT",
            evaluated_at=BAR_TIME,
            bar_time=BAR_TIME,
            status=EvaluationStatus.SIGNAL,
            signal=created,
            reason="bad result",
        )


def test_error_evaluation_requires_error_detail() -> None:
    valid = Evaluation(
        instance_id="buy-aapl",
        symbol="AAPL",
        evaluated_at=BAR_TIME,
        bar_time=BAR_TIME,
        status=EvaluationStatus.ERROR,
        signal=None,
        reason="strategy exception",
        error_detail="division by zero",
    )
    assert valid.error_detail == "division by zero"

    with pytest.raises(ValueError, match="error_detail"):
        Evaluation(
            instance_id="buy-aapl",
            symbol="AAPL",
            evaluated_at=BAR_TIME,
            bar_time=BAR_TIME,
            status=EvaluationStatus.ERROR,
            signal=None,
            reason="strategy exception",
        )


def test_signal_decision_requirements_and_execution_separation() -> None:
    accepted = SignalDecision(
        decision_id="decision-1",
        signal_id="signal-1",
        outcome=DecisionOutcome.ACCEPT,
        decided_at=BAR_TIME,
        reason_codes=(),
        detail="accepted",
    )
    assert accepted.defer_until is None
    rejected = SignalDecision(
        decision_id="decision-rejected",
        signal_id="signal-1",
        outcome=DecisionOutcome.REJECT,
        decided_at=BAR_TIME,
        reason_codes=("ENTRY_BLOCKED",),
        detail="entry blocked",
    )
    deferred = SignalDecision(
        decision_id="decision-deferred",
        signal_id="signal-1",
        outcome=DecisionOutcome.DEFER,
        decided_at=BAR_TIME,
        reason_codes=("COOLDOWN",),
        detail="wait for cooldown",
        defer_until=BAR_TIME + timedelta(minutes=1),
    )
    assert rejected.reason_codes == ("ENTRY_BLOCKED",)
    assert deferred.defer_until == BAR_TIME + timedelta(minutes=1)
    assert {field.name for field in fields(SignalDecision)} == {
        "decision_id",
        "signal_id",
        "outcome",
        "decided_at",
        "reason_codes",
        "detail",
        "defer_until",
    }

    with pytest.raises(ValueError, match="reason_codes"):
        SignalDecision(
            decision_id="decision-2",
            signal_id="signal-1",
            outcome=DecisionOutcome.REJECT,
            decided_at=BAR_TIME,
            reason_codes=(),
            detail="rejected",
        )
    with pytest.raises(ValueError, match="reason_codes"):
        SignalDecision(
            decision_id="decision-3",
            signal_id="signal-1",
            outcome=DecisionOutcome.DEFER,
            decided_at=BAR_TIME,
            reason_codes=(),
            detail="deferred",
            defer_until=BAR_TIME + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="defer_until"):
        SignalDecision(
            decision_id="decision-4",
            signal_id="signal-1",
            outcome=DecisionOutcome.DEFER,
            decided_at=BAR_TIME,
            reason_codes=("COOLDOWN",),
            detail="deferred",
        )


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (lambda: provenance(bar_time=datetime(2026, 10, 8, 14, 30)), "bar_time"),
        (
            lambda: provenance(research_as_of=datetime(2026, 10, 8, 14, 30)),
            "research_as_of",
        ),
        (
            lambda: signal(
                bar_time=datetime(2026, 10, 8, 14, 30),
                provenance=provenance(bar_time=datetime(2026, 10, 8, 14, 30)),
            ),
            "bar_time",
        ),
        (lambda: signal(generated_at=datetime(2026, 10, 8, 14, 30)), "generated_at"),
        (
            lambda: Evaluation(
                instance_id="buy-aapl",
                symbol="AAPL",
                evaluated_at=datetime(2026, 10, 8, 14, 30),
                bar_time=None,
                status=EvaluationStatus.NO_SIGNAL,
                signal=None,
                reason="not due",
            ),
            "evaluated_at",
        ),
        (
            lambda: Evaluation(
                instance_id="buy-aapl",
                symbol="AAPL",
                evaluated_at=BAR_TIME,
                bar_time=datetime(2026, 10, 8, 14, 30),
                status=EvaluationStatus.NO_SIGNAL,
                signal=None,
                reason="not due",
            ),
            "bar_time",
        ),
        (
            lambda: SignalDecision(
                decision_id="decision-1",
                signal_id="signal-1",
                outcome=DecisionOutcome.ACCEPT,
                decided_at=datetime(2026, 10, 8, 14, 30),
                reason_codes=(),
                detail="accepted",
            ),
            "decided_at",
        ),
        (
            lambda: SignalDecision(
                decision_id="decision-1",
                signal_id="signal-1",
                outcome=DecisionOutcome.DEFER,
                decided_at=BAR_TIME,
                reason_codes=("COOLDOWN",),
                detail="deferred",
                defer_until=datetime(2026, 10, 8, 14, 31),
            ),
            "defer_until",
        ),
    ],
)
def test_naive_datetimes_are_rejected(factory: object, field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        factory()  # type: ignore[operator]
