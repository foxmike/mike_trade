"""Model-driven BUY/SELL strategies: pure decisions over immutable pinned-model predictions.

No model library, broker, network or clock is involved: contexts carry hand-built
``ServedPrediction`` values, exactly what the composition root supplies at runtime.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from mike_trade.domain.model_signals import (
    ModelInputs,
    ModelPredictionResult,
    PositionFacts,
    PredictionFailure,
    ServedPrediction,
)
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    ParameterValidationError,
    ReasonCode,
    StrategyRole,
)
from mike_trade.strategies.buy.lightgbm_probability_buy import LightgbmProbabilityBuy
from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy
from mike_trade.strategies.buy.xgboost_probability_buy import XgboostProbabilityBuy
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.strategies.sell.lightgbm_probability_sell import LightgbmProbabilitySell
from mike_trade.strategies.sell.xgboost_probability_sell import XgboostProbabilitySell

AS_OF = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)
SHA = "a" * 64
FEATURE_SHA = "b" * 64
ARMED = {"eligibility": "WATCHING_BUY"}

BUY_PARAMS = {"model_id": "buy_model", "model_version": "1.0.0"}
SELL_PARAMS = {"model_id": "sell_model", "model_version": "1.0.0"}

BUY_CLASSES = (XgboostProbabilityBuy, LightgbmProbabilityBuy)
SELL_CLASSES = (XgboostProbabilitySell, LightgbmProbabilitySell)


def candles(symbol: str = "AAA", count: int = 35, end: datetime = AS_OF) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            symbol,
            CandleTimeframe.M1,
            end - timedelta(minutes=count - i),
            end - timedelta(minutes=count - i - 1),
            D("100"),
            D("100.2"),
            D("99.8"),
            D("100"),
            D("1000"),
            "iex",
            False,
            CandleOrigin.REPLAY,
            PriceBasis.RAW,
            CandleStatus.CLOSED,
        )
        for i in range(count)
    )


def served(
    cls,
    *,
    symbol: str = "AAA",
    probabilities=None,
    bars: tuple[Candle, ...] | None = None,
    position: PositionFacts | None = None,
    **overrides,
) -> ServedPrediction:
    bars = bars or candles(symbol)
    algorithm = "xgboost" if cls in (XgboostProbabilityBuy, XgboostProbabilitySell) else "lightgbm"
    role = "buy" if cls in BUY_CLASSES else "sell"
    params = BUY_PARAMS if role == "buy" else SELL_PARAMS
    values = {
        "model_id": params["model_id"],
        "model_version": params["model_version"],
        "artifact_sha256": SHA,
        "algorithm": algorithm,
        "role": role,
        "task": "MULTICLASS_CLASSIFICATION",
        "symbol": symbol,
        "feature_schema_id": "intraday_core",
        "feature_schema_version": "1.0.0",
        "feature_schema_sha256": FEATURE_SHA,
        "feed": "iex",
        "timeframe": "1m",
        "adjustment": "raw",
        "observation_start": bars[-1].start,
        "observation_end": bars[-1].end,
        "predicted_at": bars[-1].end + timedelta(seconds=2),
        "horizon_bars": 30,
        "label_id": "triple_barrier" if role == "buy" else "sell_continuation",
        "class_labels": ("-1", "0", "1"),
        "probabilities": (
            {"-1": 0.1, "0": 0.2, "1": 0.7} if probabilities is None else probabilities
        ),
        "class_net_returns": {"1": D("0.0038"), "-1": D("-0.0032"), "0": D("-0.0001")},
        "class_return_basis": "triple_barrier:test",
        "symbol_in_training_universe": True,
        "position_fingerprint": position.fingerprint if position else None,
    }
    values.update(overrides)
    return ServedPrediction(**values)


def context(
    cls,
    prediction: ServedPrediction | None = None,
    *,
    position: PositionFacts | None = None,
    symbol: str = "AAA",
    bars: tuple[Candle, ...] | None = None,
    result: ModelPredictionResult | None = None,
    params: dict | None = None,
    inputs: bool = True,
    as_of: datetime = AS_OF,
    **context_overrides,
) -> MarketContext:
    bars = bars or candles(symbol)
    chosen = params or (BUY_PARAMS if cls in BUY_CLASSES else SELL_PARAMS)
    reference = cls.model_reference(cls.spec.parameter_schema.validate(chosen))
    if result is None and prediction is not None:
        result = ModelPredictionResult(reference, prediction=prediction)
    model_inputs = (
        ModelInputs(symbol, as_of, {reference.key: result} if result else {}, position)
        if inputs
        else None
    )
    values = {
        "symbol": symbol,
        "as_of": as_of,
        "version": 1,
        "feed_id": "iex",
        "consolidated": False,
        "price_basis": PriceBasis.RAW,
        "session_status": SessionStatus.REGULAR,
        "session_id": "XNYS-2026-03-10",
        "freshness": ContextFreshness(as_of, timedelta(0), timedelta(seconds=90), False, None),
        "candles": {CandleTimeframe.M1: bars},
        "indicators": {},
        "model_inputs": model_inputs,
    }
    values.update(context_overrides)
    return MarketContext(**values)


def position(entry: str = "100", cycle: str = "AAA:1", at: datetime | None = None) -> PositionFacts:
    return PositionFacts("AAA", cycle, D("10"), D(entry), at or AS_OF - timedelta(minutes=5))


def decide(cls, ctx, params=None, state=None):
    params = cls.spec.parameter_schema.validate(
        params or (BUY_PARAMS if cls in BUY_CLASSES else SELL_PARAMS)
    )
    return cls().evaluate(
        ctx, params, ARMED if state is None and cls in BUY_CLASSES else (state or {})
    )


def audit_of(evaluation) -> dict:
    import json

    return json.loads(evaluation.state_updates["model_audit"])


# ------------------------------------------------------------------ registration / schema


def test_four_strategies_register_with_distinct_identities():
    registry = StrategyRegistry()
    for cls in (*BUY_CLASSES, *SELL_CLASSES):
        registry.register(cls)
    ids = {cls.spec.strategy_id: cls.spec.side for cls in registry.list_strategies()}
    assert ids == {
        "lightgbm_probability_buy": StrategyRole.BUY,
        "lightgbm_probability_sell": StrategyRole.SELL,
        "xgboost_probability_buy": StrategyRole.BUY,
        "xgboost_probability_sell": StrategyRole.SELL,
    }
    for cls in (*BUY_CLASSES, *SELL_CLASSES):
        assert cls.spec.category == "machine_learning"
        assert cls.spec.required_timeframes == (CandleTimeframe.M1,)
        assert "profitab" in cls.spec.methodology  # states it makes no profitability claim


def test_parameters_pin_the_model_and_reject_latest_or_malformed_values():
    schema = XgboostProbabilityBuy.spec.parameter_schema
    with pytest.raises(ParameterValidationError, match="model_id"):
        schema.validate({"model_version": "1.0.0"})  # no implicit model
    with pytest.raises(ParameterValidationError, match="model_version"):
        schema.validate({"model_id": "m", "model_version": "latest"})
    with pytest.raises(ParameterValidationError, match="model_version"):
        schema.validate({"model_id": "m", "model_version": "1.0"})
    with pytest.raises(ParameterValidationError, match="artifact_sha256"):
        schema.validate({**BUY_PARAMS, "artifact_sha256": "xyz"})
    with pytest.raises(ParameterValidationError, match="out of range|outside"):
        schema.validate({**BUY_PARAMS, "minimum_favorable_probability_bps": 10001})
    with pytest.raises(ParameterValidationError, match="unknown parameter"):
        schema.validate({**BUY_PARAMS, "thresholds": 1})
    ok = schema.validate({**BUY_PARAMS, "artifact_sha256": SHA})
    assert ok["decision_mode"] == "MODEL_DIRECTED"
    assert schema.validate({**BUY_PARAMS, "decision_mode": "THRESHOLD"})[
        "decision_mode"
    ] == "THRESHOLD"
    with pytest.raises(ParameterValidationError, match="decision_mode"):
        schema.validate({**BUY_PARAMS, "decision_mode": "UNSUPPORTED"})
    assert XgboostProbabilityBuy.model_reference(ok).artifact_sha256 == SHA
    assert XgboostProbabilityBuy.model_reference(ok).algorithm == "xgboost"
    assert (
        LightgbmProbabilitySell.model_reference(
            LightgbmProbabilitySell.spec.parameter_schema.validate(SELL_PARAMS)
        ).role
        == "sell"
    )


# ------------------------------------------------------------------ BUY decisions


@pytest.mark.parametrize("cls", BUY_CLASSES)
def test_model_directed_buy_signals_when_favorable_outcome_beats_adverse(cls):
    prediction = served(cls, probabilities={"-1": 0.1, "0": 0.2, "1": 0.7})
    evaluation = decide(cls, context(cls, prediction))
    assert evaluation.is_signal and evaluation.reason_codes == (ReasonCode.SIGNAL,)
    assert evaluation.side is StrategyRole.BUY
    assert evaluation.bar_time == candles()[-1].start
    assert evaluation.reference_price == D("100")
    audit = audit_of(evaluation)
    assert audit["decision"] == "SIGNAL" and audit["model"] == "buy_model@1.0.0"
    assert audit["prediction"]["probabilities"] == {"-1": 0.1, "0": 0.2, "1": 0.7}
    assert audit["prediction"]["artifact_sha256"] == SHA
    assert audit["decision_mode"] == "MODEL_DIRECTED"
    assert audit["thresholds"]["minimum_favorable_probability_bps"] == 5000


@pytest.mark.parametrize("cls", BUY_CLASSES)
def test_model_directed_buy_has_no_absolute_or_neutral_probability_gate(cls):
    prediction = served(cls, probabilities={"-1": 0.2, "0": 0.4, "1": 0.4})
    evaluation = decide(cls, context(cls, prediction))
    assert evaluation.is_signal
    assert audit_of(evaluation)["favorable_probability_bps"] == 4000


@pytest.mark.parametrize("cls", BUY_CLASSES)
def test_model_directed_buy_holds_when_adverse_is_at_least_favorable(cls):
    prediction = served(cls, probabilities={"-1": 0.4, "0": 0.3, "1": 0.3})
    evaluation = decide(cls, context(cls, prediction))
    assert not evaluation.is_signal and evaluation.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert audit_of(evaluation)["reason"] == "FAVORABLE_NOT_PREFERRED"

    tied = served(cls, probabilities={"-1": 0.3, "0": 0.4, "1": 0.3})
    assert audit_of(decide(cls, context(cls, tied)))["reason"] == (
        "FAVORABLE_NOT_PREFERRED"
    )


@pytest.mark.parametrize("cls", BUY_CLASSES)
def test_threshold_buy_mode_retains_absolute_probability_gates(cls):
    params = {**BUY_PARAMS, "decision_mode": "THRESHOLD"}
    below = served(cls, probabilities={"-1": 0.2, "0": 0.4, "1": 0.4})
    evaluation = decide(cls, context(cls, below, params=params), params)
    assert not evaluation.is_signal
    assert audit_of(evaluation)["reason"] == "BELOW_FAVORABLE_THRESHOLD"


def test_favorable_probability_above_half_is_not_treated_as_positive_expectation():
    cls = XgboostProbabilityBuy
    # 55% win but 40% loss against asymmetric payoffs: expectation is negative
    prediction = served(
        cls,
        probabilities={"-1": 0.40, "0": 0.05, "1": 0.55},
        class_net_returns={"1": D("0.0010"), "-1": D("-0.0030"), "0": D("-0.0001")},
    )
    params = {
        **BUY_PARAMS,
        "decision_mode": "THRESHOLD",
        "maximum_adverse_probability_bps": 10000,
    }
    assert decide(cls, context(cls, prediction), params).is_signal  # probability rule alone
    strict = {**params, "use_expected_value": True, "minimum_expected_value_bps": 1}
    rejected = decide(cls, context(cls, prediction), strict)
    assert not rejected.is_signal and audit_of(rejected)["reason"] == "BELOW_EXPECTED_VALUE"
    assert audit_of(rejected)["expected_value_bps"] < 0


def test_adverse_probability_cap_and_expected_value_accounting():
    cls = LightgbmProbabilityBuy
    prediction = served(cls, probabilities={"-1": 0.35, "0": 0.0, "1": 0.65})
    params = {**BUY_PARAMS, "decision_mode": "THRESHOLD"}
    rejected = decide(cls, context(cls, prediction, params=params), params)
    assert audit_of(rejected)["reason"] == "ABOVE_ADVERSE_THRESHOLD"
    favorable = served(cls, probabilities={"-1": 0.1, "0": 0.1, "1": 0.8})
    params = {
        **BUY_PARAMS,
        "decision_mode": "THRESHOLD",
        "use_expected_value": True,
        "minimum_expected_value_bps": 20,
    }
    accepted = decide(cls, context(cls, favorable), params)
    assert accepted.is_signal
    expected = 0.8 * 38 + 0.1 * -32 + 0.1 * -1  # bps, from the model's own class returns
    assert audit_of(accepted)["expected_value_bps"] == pytest.approx(expected, abs=0.01)
    no_returns = served(cls, probabilities={"-1": 0.1, "0": 0.1, "1": 0.8}, class_net_returns={})
    unavailable = decide(cls, context(cls, no_returns), params)
    assert audit_of(unavailable)["reason"] == "EXPECTED_VALUE_UNAVAILABLE"


def test_regression_model_uses_the_predicted_net_return():
    cls = XgboostProbabilityBuy
    base = {
        "task": "REGRESSION",
        "label_id": "forward_return",
        "class_labels": (),
        "probabilities": {},
        "class_net_returns": {},
        "value": 0.0015,
    }
    params = {
        **BUY_PARAMS,
        "decision_mode": "THRESHOLD",
        "minimum_expected_value_bps": 10,
    }
    assert decide(cls, context(cls, served(cls, **base)), params).is_signal
    weak = decide(cls, context(cls, served(cls, **{**base, "value": 0.0005})), params)
    assert audit_of(weak)["reason"] == "BELOW_EXPECTED_VALUE"
    other = decide(cls, context(cls, served(cls, **{**base, "label_id": "other"})), params)
    assert audit_of(other)["reason"] == "UNSUPPORTED_OBJECTIVE"


def test_buy_requires_an_armed_flat_state_and_a_regular_fresh_session():
    cls = XgboostProbabilityBuy
    prediction = served(cls)
    params = cls.spec.parameter_schema.validate(BUY_PARAMS)
    held = cls().evaluate(context(cls, prediction), params, {})  # engine clears eligibility
    assert (
        held.reason_codes == (ReasonCode.NOT_ELIGIBLE,) and "model_audit" not in held.state_updates
    )
    closed = decide(cls, context(cls, prediction, session_status=SessionStatus.CLOSED))
    assert closed.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    stale = ContextFreshness(
        AS_OF, timedelta(minutes=5), timedelta(seconds=90), True, "DATA_TOO_OLD"
    )
    assert decide(cls, context(cls, prediction, freshness=stale)).reason_codes == (
        ReasonCode.STALE_DATA,
    )
    short = decide(cls, context(cls, prediction, bars=candles(count=10)))
    assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_same_bar_never_signals_twice():
    cls = XgboostProbabilityBuy
    ctx = context(cls, served(cls))
    first = decide(cls, ctx)
    assert first.is_signal
    again = decide(cls, ctx, state={**ARMED, **first.state_updates})
    assert not again.is_signal and again.reason_codes == (ReasonCode.NO_SIGNAL,)


@pytest.mark.parametrize("cls", BUY_CLASSES)
@pytest.mark.parametrize(
    ("override", "extra_params", "code", "reason"),
    [
        ({"model_version": "1.0.1"}, {}, ReasonCode.INSUFFICIENT_DATA, "MODEL_MISMATCH"),
        ({"model_id": "other_model"}, {}, ReasonCode.INSUFFICIENT_DATA, "MODEL_MISMATCH"),
        (
            {"artifact_sha256": "c" * 64},
            {"artifact_sha256": SHA},
            ReasonCode.INSUFFICIENT_DATA,
            "CHECKSUM_MISMATCH",
        ),
        ({"feed": "sip"}, {}, ReasonCode.INSUFFICIENT_DATA, "FEED_MISMATCH"),
        ({"adjustment": "split"}, {}, ReasonCode.INSUFFICIENT_DATA, "PROVENANCE_MISMATCH"),
        (
            {"horizon_bars": 10},
            {"expected_horizon_bars": 30},
            ReasonCode.INSUFFICIENT_DATA,
            "HORIZON_MISMATCH",
        ),
        (
            {"symbol_in_training_universe": False},
            {},
            ReasonCode.NOT_ELIGIBLE,
            "UNTRAINED_SYMBOL",
        ),
        (
            {"role": "sell", "position_fingerprint": "f" * 24},
            {},
            ReasonCode.INSUFFICIENT_DATA,
            "ROLE_MISMATCH",
        ),
    ],
)
def test_buy_refuses_mismatched_or_unsuitable_predictions(
    cls, override, extra_params, code, reason
):
    params = {**BUY_PARAMS, **extra_params}
    prediction = served(cls, **override)
    evaluation = decide(cls, context(cls, prediction, params=params), params)
    assert not evaluation.is_signal and evaluation.reason_codes == (code,)
    assert audit_of(evaluation)["reason"] == reason


def test_a_foreign_symbol_prediction_cannot_even_be_attached():
    cls = XgboostProbabilityBuy
    reference = cls.model_reference(BUY_PARAMS)
    foreign = served(cls, symbol="BBB")
    with pytest.raises(ValueError, match="symbol"):
        ModelInputs("AAA", AS_OF, {reference.key: ModelPredictionResult(reference, foreign)})
    with pytest.raises(ValueError, match="symbol"):
        context(cls, served(cls), symbol="BBB")


@pytest.mark.parametrize("cls", BUY_CLASSES)
def test_buy_refuses_wrong_algorithm_prediction(cls):
    wrong = "lightgbm" if cls is XgboostProbabilityBuy else "xgboost"
    evaluation = decide(cls, context(cls, served(cls, algorithm=wrong)))
    assert audit_of(evaluation)["reason"] == "ALGORITHM_MISMATCH" and not evaluation.is_signal


def test_prediction_for_an_earlier_candle_is_never_used_for_a_later_one():
    cls = XgboostProbabilityBuy
    bars = candles()
    earlier = served(cls, bars=bars[:-1])  # observed the previous completed bar
    evaluation = decide(cls, context(cls, earlier, bars=bars))
    assert evaluation.reason_codes == (ReasonCode.STALE_DATA,)
    assert audit_of(evaluation)["reason"] == "OBSERVATION_NOT_LATEST_BAR"


def test_stale_and_future_dated_predictions_are_refused():
    cls = XgboostProbabilityBuy
    late = AS_OF + timedelta(minutes=5)
    ctx = context(cls, served(cls), as_of=late)
    stale = decide(cls, ctx)
    assert stale.reason_codes == (ReasonCode.STALE_DATA,)
    assert audit_of(stale)["reason"] == "STALE_PREDICTION"
    ahead = candles(end=AS_OF + timedelta(minutes=1))  # an observation that ends after as_of
    future = decide(cls, context(cls, served(cls, bars=ahead)))
    assert future.reason_codes == (ReasonCode.STALE_DATA,)
    assert audit_of(future)["reason"] == "FUTURE_OBSERVATION"
    slow = served(cls, predicted_at=AS_OF + timedelta(minutes=3))
    assert audit_of(decide(cls, context(cls, slow)))["reason"] == "STALE_PREDICTION"


def test_invalid_probability_distributions_cannot_be_constructed():
    cls = XgboostProbabilityBuy
    for bad in (
        {"-1": 0.5, "0": 0.5, "1": 0.5},
        {"-1": -0.1, "0": 0.6, "1": 0.5},
        {"-1": float("nan"), "0": 0.5, "1": 0.5},
        {"-1": 0.5, "0": 0.5},
    ):
        with pytest.raises(ValueError):
            served(cls, probabilities=bad)
    with pytest.raises(ValueError, match="before its observation"):
        served(cls, predicted_at=AS_OF - timedelta(hours=1))


def test_unavailable_or_missing_predictions_never_signal():
    cls = XgboostProbabilityBuy
    reference = cls.model_reference(BUY_PARAMS)
    for failure in PredictionFailure:
        result = ModelPredictionResult(reference, failure=failure, detail="x")
        evaluation = decide(cls, context(cls, result=result))
        assert not evaluation.is_signal
        assert audit_of(evaluation)["reason"] == f"PREDICTION_UNAVAILABLE:{failure}"
    assert audit_of(decide(cls, context(cls)))["reason"] == "PREDICTION_MISSING"
    none = decide(cls, context(cls, inputs=False))
    assert audit_of(none)["reason"] == "NO_MODEL_INPUTS" and not none.is_signal


def test_a_defect_in_a_model_strategy_is_contained():
    cls = XgboostProbabilityBuy
    ctx = context(cls, served(cls))
    params = cls.spec.parameter_schema.validate(BUY_PARAMS)
    broken = dict(params)
    broken["minimum_favorable_probability_bps"] = "n/a"  # impossible after validation
    evaluation = cls().evaluate(ctx, broken, ARMED)
    assert not evaluation.is_signal
    assert audit_of(evaluation)["reason"] == "EVALUATION_ERROR"


# ------------------------------------------------------------------ SELL decisions


@pytest.mark.parametrize("cls", SELL_CLASSES)
def test_sell_signal_uses_the_actual_position_and_prediction(cls):
    actual = position("99.5")
    prediction = served(
        cls,
        probabilities={"-1": 0.15, "0": 0.55, "1": 0.30},
        position=actual,
        position_features={"position_unrealized_return": 0.005},
    )
    evaluation = decide(cls, context(cls, prediction, position=actual))
    assert evaluation.is_signal and evaluation.side is StrategyRole.SELL
    audit = audit_of(evaluation)
    assert audit["position"]["entry_price"] == "99.5"
    assert audit["position"]["fingerprint"] == actual.fingerprint
    assert audit["exit_probability_bps"] == 7000
    assert audit["prediction"]["position_features"]["position_unrealized_return"] == 0.005


@pytest.mark.parametrize("cls", SELL_CLASSES)
def test_sell_holds_when_continuing_is_favored(cls):
    actual = position()
    prediction = served(cls, probabilities={"-1": 0.1, "0": 0.2, "1": 0.7}, position=actual)
    evaluation = decide(cls, context(cls, prediction, position=actual))
    assert not evaluation.is_signal and evaluation.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert audit_of(evaluation)["reason"] == "HOLD_PREFERRED"


def test_sell_risk_trigger_is_independent_and_disabled_by_default():
    cls = XgboostProbabilitySell
    actual = position()
    prediction = served(cls, probabilities={"-1": 0.45, "0": 0.05, "1": 0.50}, position=actual)
    ctx = context(cls, prediction, position=actual)
    threshold = {**SELL_PARAMS, "decision_mode": "THRESHOLD"}
    assert not decide(cls, ctx, threshold).is_signal  # 50% exit < 60% default
    risky = decide(
        cls,
        ctx,
        {
            **threshold,
            "minimum_risk_exit_probability_bps": 4000,
        },
    )
    assert risky.is_signal


@pytest.mark.parametrize("cls", SELL_CLASSES)
def test_model_directed_sell_requires_exit_direction_to_strictly_beat_hold(cls):
    actual = position()
    exit_favored = served(
        cls, probabilities={"-1": 0.2, "0": 0.31, "1": 0.49}, position=actual
    )
    assert decide(cls, context(cls, exit_favored, position=actual)).is_signal
    tied = served(cls, probabilities={"-1": 0.2, "0": 0.3, "1": 0.5}, position=actual)
    evaluation = decide(cls, context(cls, tied, position=actual))
    assert not evaluation.is_signal
    assert audit_of(evaluation)["reason"] == "HOLD_PREFERRED"


@pytest.mark.parametrize("cls", SELL_CLASSES)
def test_sell_never_signals_without_a_real_position(cls):
    prediction = served(cls, position=position())
    for ctx in (context(cls, prediction), context(cls, inputs=False)):
        evaluation = decide(cls, ctx)
        assert not evaluation.is_signal and evaluation.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
        assert "model_audit" not in evaluation.state_updates  # idle: not persisted


def test_sell_prediction_must_belong_to_this_exact_position():
    cls = LightgbmProbabilitySell
    probabilities = {"-1": 0.3, "0": 0.5, "1": 0.2}
    mine = position("100", cycle="AAA:1")
    for stale in (position("101", cycle="AAA:1"), position("100", cycle="AAA:2")):
        prediction = served(cls, probabilities=probabilities, position=stale)
        evaluation = decide(cls, context(cls, prediction, position=mine))
        assert not evaluation.is_signal
        assert audit_of(evaluation)["reason"] == "POSITION_MISMATCH"
    entered_late = position(at=AS_OF + timedelta(minutes=2))
    prediction = served(cls, probabilities=probabilities, position=entered_late)
    ctx = context(cls, prediction, position=entered_late)
    assert audit_of(decide(cls, ctx))["reason"] == "POSITION_MISMATCH"


def test_sell_refuses_wrong_class_layout_and_buy_model():
    cls = XgboostProbabilitySell
    actual = position()
    binary = served(
        cls, position=actual, class_labels=("0", "1"), probabilities={"0": 0.7, "1": 0.3}
    )
    assert audit_of(decide(cls, context(cls, binary, position=actual)))["reason"] == (
        "UNSUPPORTED_OBJECTIVE"
    )
    buy_model = served(cls, role="buy", position=None)
    assert audit_of(decide(cls, context(cls, buy_model, position=actual)))["reason"] == (
        "ROLE_MISMATCH"
    )
    wrong_semantics = served(cls, position=actual, label_id="triple_barrier")
    assert audit_of(
        decide(cls, context(cls, wrong_semantics, position=actual))
    )["reason"] == "UNSUPPORTED_OBJECTIVE"


# ------------------------------------------------------------------ purity and compatibility


def test_strategy_modules_import_no_model_library_broker_or_io():
    root = Path(__file__).parents[2] / "mike_trade" / "strategies"
    forbidden = (
        "xgboost",
        "lightgbm",
        "mike_trade.modeling",
        "numpy",
        "broker",
        "alpaca",
        "sqlite3",
        "persistence",
        "httpx",
        "requests",
        "urllib",
        "socket",
        "subprocess",
        "pickle",
        "joblib",
        "mike_trade.app",
        "mike_trade.live",
        "mike_trade.runtime",
    )
    files = [root / "model_probability.py"] + [
        root / side / f"{algo}_probability_{side}.py"
        for side in ("buy", "sell")
        for algo in ("xgboost", "lightgbm")
    ]
    for path in files:
        tree = ast.parse(path.read_text())
        imported = [
            name
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
        ]
        assert not [n for n in imported if any(word in n.lower() for word in forbidden)], path
        calls = {
            getattr(node.func, "id", getattr(node.func, "attr", ""))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not calls & {"open", "now", "utcnow", "time", "sleep", "load_model", "connect"}, path


def test_domain_prediction_module_is_free_of_model_libraries():
    import mike_trade.domain.model_signals as module

    tree = ast.parse(Path(module.__file__).read_text())
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not [n for n in imports if n.startswith("mike_trade") or "xgboost" in n or "numpy" in n]


def test_technical_strategies_are_unaffected_by_model_inputs():
    from model_fixtures import flat  # noqa: F401  (fixture module import sanity)

    bars = candles()
    base = context(
        XgboostProbabilityBuy,
        None,
        inputs=False,
        bars=bars,
        indicators={"vwap": D("100.0"), "vwap_prev": D("100.5")},
    )
    with_inputs = replace(
        base,
        model_inputs=ModelInputs("AAA", AS_OF, {}, None),
    )
    strategy, params = VwapBounceBuy(), VwapBounceBuy.spec.parameter_schema.validate({})
    state = {"eligibility": "WATCHING_BUY"}
    assert strategy.evaluate(base, params, state) == strategy.evaluate(with_inputs, params, state)
