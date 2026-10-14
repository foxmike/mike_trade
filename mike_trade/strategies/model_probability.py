"""Shared pure decision core for model-driven (XGBoost/LightGBM) BUY and SELL strategies.

Four registered strategies sit on this core and differ only in algorithm and side:
``xgboost_probability_buy``, ``lightgbm_probability_buy``, ``xgboost_probability_sell`` and
``lightgbm_probability_sell``.  This module imports only the standard library and pure
domain/market-data value types.  It never imports XGBoost, LightGBM, ``mike_trade.modeling``,
a broker, persistence, or an HTTP client; a strategy never loads a model file.

A model-driven strategy is an ordinary strategy: ``evaluate(context, params, state)`` is a pure
function and returns SIGNAL or NO_SIGNAL through the existing SDK.  The only new input is
``context.model_inputs``, which the composition root fills with already-computed, immutable,
provenance-carrying predictions from explicitly pinned model artifacts.  A missing, failed,
stale, mismatched or unsuitable prediction never produces a signal, and it never raises: the
affected strategy reports a reason and every other strategy evaluates independently.

What the probabilities mean (see ``docs/STRATEGIES.md``):

* BUY models are triple-barrier classifiers: class ``1`` = the take-profit barrier was reached
  first, ``-1`` = the stop barrier was reached first (ties resolve to loss), ``0`` = neither
  within the horizon.  The numbers are calibrated *class probabilities*, not return forecasts.
  An optional expected-value test uses the modeled net return of each class from the model's
  own label schema (timeouts assumed flat before costs) and is skipped unless enabled.
* SELL models are continuation classifiers over an actual open long: ``-1`` = holding would hit
  the risk barrier, ``0`` = exiting now beats holding, ``1`` = holding beats exiting now.

No claim is made that any threshold is profitable; defaults are neutral reference values.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from mike_trade.domain.model_signals import (
    ModelPredictionResult,
    ModelReference,
    PositionFacts,
    PredictionFailure,
    ServedPrediction,
)
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleTimeframe
from mike_trade.review.evidence import (
    EVIDENCE_INTERFACE_VERSION,
    DecisionEvidence,
    EvidenceError,
    make_evidence,
)
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ReasonCode,
    SellStrategy,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import ELIGIBILITY_KEY, ELIGIBILITY_WATCHING_BUY

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STATE_LAST_SIGNAL_BAR",
    "STATE_MODEL_AUDIT",
    "MODEL_REASON_CODES",
    "ModelDecisionMode",
    "ModelReason",
    "ModelDecision",
    "buy_parameter_schema",
    "sell_parameter_schema",
    "model_strategy_spec",
    "ModelProbabilityBuy",
    "ModelProbabilitySell",
]

#: Strategy-owned persisted state: ISO start of the last bar that fired (replay suppression).
STATE_LAST_SIGNAL_BAR = "last_signal_bar"
#: Strategy-owned persisted state: the latest compact, JSON-text decision audit record.
STATE_MODEL_AUDIT = "model_audit"

MODEL_REASON_CODES: tuple[ReasonCode, ...] = (
    ReasonCode.SIGNAL,
    ReasonCode.NO_SIGNAL,
    ReasonCode.INSUFFICIENT_DATA,
    ReasonCode.STALE_DATA,
    ReasonCode.NOT_ELIGIBLE,
)

#: Warm-up in completed 1-minute bars before the strategy is evaluated at all; the model's own
#: feature window (21 bars for ``intraday_core`` v1) is verified by the inference service.
_WARMUP_BARS = 30
_BPS = Decimal(10000)
_IDENTIFIER_PATTERN = r"[a-z][a-z0-9_]{0,62}"
_SEMVER_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_CLASS_PATTERN = r"[A-Za-z0-9_\-]{1,32}"
_CHECKSUM_PATTERN = r"[0-9a-f]{64}|"
_SELL_CLASSES = ("-1", "0", "1")
_CLASS_RISK_EXIT, _CLASS_EXIT_FAVORABLE, _CLASS_HOLD_FAVORABLE = _SELL_CLASSES


class ModelDecisionMode(StrEnum):
    """How a classification model's probabilities become a trading decision."""

    MODEL_DIRECTED = "MODEL_DIRECTED"
    THRESHOLD = "THRESHOLD"


class ModelReason(StrEnum):
    """Fine-grained, stable explanation of one model-strategy decision."""

    SIGNAL = "SIGNAL"
    NOT_ARMED = "NOT_ARMED"
    NOT_REGULAR_SESSION = "NOT_REGULAR_SESSION"
    STALE_CONTEXT = "STALE_CONTEXT"
    NOT_WARMED = "NOT_WARMED"
    INCOMPLETE_BARS = "INCOMPLETE_BARS"
    NO_MODEL_INPUTS = "NO_MODEL_INPUTS"
    PREDICTION_MISSING = "PREDICTION_MISSING"
    PREDICTION_UNAVAILABLE = "PREDICTION_UNAVAILABLE"
    MODEL_MISMATCH = "MODEL_MISMATCH"
    ALGORITHM_MISMATCH = "ALGORITHM_MISMATCH"
    ROLE_MISMATCH = "ROLE_MISMATCH"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    FEED_MISMATCH = "FEED_MISMATCH"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    HORIZON_MISMATCH = "HORIZON_MISMATCH"
    OBSERVATION_NOT_LATEST_BAR = "OBSERVATION_NOT_LATEST_BAR"
    FUTURE_OBSERVATION = "FUTURE_OBSERVATION"
    STALE_PREDICTION = "STALE_PREDICTION"
    UNTRAINED_SYMBOL = "UNTRAINED_SYMBOL"
    UNSUPPORTED_OBJECTIVE = "UNSUPPORTED_OBJECTIVE"
    FAVORABLE_NOT_PREFERRED = "FAVORABLE_NOT_PREFERRED"
    BELOW_FAVORABLE_THRESHOLD = "BELOW_FAVORABLE_THRESHOLD"
    ABOVE_ADVERSE_THRESHOLD = "ABOVE_ADVERSE_THRESHOLD"
    BELOW_EXPECTED_VALUE = "BELOW_EXPECTED_VALUE"
    EXPECTED_VALUE_UNAVAILABLE = "EXPECTED_VALUE_UNAVAILABLE"
    NO_POSITION = "NO_POSITION"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    HOLD_PREFERRED = "HOLD_PREFERRED"
    EVALUATION_ERROR = "EVALUATION_ERROR"


#: Evaluations that carry no model decision (not armed, no position, outside the session).
_IDLE_REASONS = frozenset(
    {ModelReason.NOT_ARMED, ModelReason.NO_POSITION, ModelReason.NOT_REGULAR_SESSION}
)


@dataclass(frozen=True, slots=True)
class ModelDecision:
    """The outcome of one pure evaluation plus the facts that explain it."""

    signal: bool
    reason_code: ReasonCode
    reason: str
    audit: Mapping[str, object] = field(default_factory=dict)

    def audit_text(self) -> str:
        return json.dumps(self.audit, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- parameters


def _pin_parameters() -> tuple[ParameterSpec, ...]:
    return (
        ParameterSpec(
            "decision_mode",
            ParameterKind.ENUM,
            default=ModelDecisionMode.MODEL_DIRECTED.value,
            choices=tuple(mode.value for mode in ModelDecisionMode),
            description=(
                "MODEL_DIRECTED compares the trained outcome directions; THRESHOLD applies "
                "the optional experimental absolute probability gates."
            ),
        ),
        ParameterSpec(
            "model_id",
            ParameterKind.STRING,
            pattern=_IDENTIFIER_PATTERN,
            description="Pinned model id (never 'latest'); must match the trained artifact.",
        ),
        ParameterSpec(
            "model_version",
            ParameterKind.STRING,
            pattern=_SEMVER_PATTERN,
            description="Pinned MAJOR.MINOR.PATCH model version.",
        ),
        ParameterSpec(
            "artifact_sha256",
            ParameterKind.STRING,
            default="",
            pattern=_CHECKSUM_PATTERN,
            description="Optional artifact SHA-256 the loaded model must match; empty = unpinned.",
        ),
        ParameterSpec(
            "maximum_prediction_age_seconds",
            ParameterKind.INT,
            default=90,
            min_value=1,
            max_value=600,
            description="Maximum age of the observed bar when the decision is made.",
        ),
        ParameterSpec(
            "expected_horizon_bars",
            ParameterKind.INT,
            default=0,
            min_value=0,
            max_value=10000,
            description="Required model label horizon in bars; 0 accepts the pinned model's own.",
        ),
        ParameterSpec(
            "require_trained_symbol",
            ParameterKind.BOOL,
            default=True,
            description="Refuse instruments that were not in the model's training universe.",
        ),
    )


def buy_parameter_schema() -> ParameterSchema:
    return ParameterSchema(
        (
            *_pin_parameters(),
            ParameterSpec(
                "minimum_favorable_probability_bps",
                ParameterKind.INT,
                default=5000,
                min_value=1,
                max_value=10000,
                description="Minimum probability (basis points) of the favorable class.",
            ),
            ParameterSpec(
                "maximum_adverse_probability_bps",
                ParameterKind.INT,
                default=3000,
                min_value=0,
                max_value=10000,
                description="Maximum probability (bps) of the adverse class; 10000 disables.",
            ),
            ParameterSpec(
                "use_expected_value",
                ParameterKind.BOOL,
                default=False,
                description="Also require the barrier-implied expected net return to qualify.",
            ),
            ParameterSpec(
                "minimum_expected_value_bps",
                ParameterKind.INT,
                default=0,
                min_value=-10000,
                max_value=10000,
                description="Minimum expected net return (bps) when evaluated; regression models "
                "always use it.",
            ),
            ParameterSpec(
                "favorable_class",
                ParameterKind.STRING,
                default="1",
                pattern=_CLASS_PATTERN,
                description="Class label that denotes a favorable outcome.",
            ),
            ParameterSpec(
                "adverse_class",
                ParameterKind.STRING,
                default="-1",
                pattern=_CLASS_PATTERN,
                description="Class label that denotes an adverse outcome.",
            ),
        )
    )


def sell_parameter_schema() -> ParameterSchema:
    return ParameterSchema(
        (
            *_pin_parameters(),
            ParameterSpec(
                "minimum_exit_probability_bps",
                ParameterKind.INT,
                default=6000,
                min_value=1,
                max_value=10000,
                description="Exit when P(risk exit) + P(exit favorable) reaches this level (bps).",
            ),
            ParameterSpec(
                "minimum_risk_exit_probability_bps",
                ParameterKind.INT,
                default=0,
                min_value=0,
                max_value=10000,
                description="Also exit when P(risk exit) alone reaches this level; 0 disables.",
            ),
        )
    )


_DISPLAY = {"xgboost": "XGBoost", "lightgbm": "LightGBM"}

_BUY_METHODOLOGY = (
    "Pinned-model probability entry: on a completed regular-session 1-minute bar, while the "
    "symbol is flat and armed, read the prediction the composition root supplied for the "
    "explicitly pinned {algo} BUY model (model_id@model_version). The model is a triple-barrier "
    "classifier (class 1 = take-profit barrier first, -1 = stop barrier first, 0 = neither "
    "within the horizon) whose output is a calibrated class probability, not a return "
    "forecast. A BUY signal requires the prediction to be valid, to describe the latest "
    "completed bar, to come from the pinned artifact and the runtime feed, to be fresh, and to "
    "show P(favorable) > P(adverse) in MODEL_DIRECTED mode. The neutral class imposes no "
    "additional gate. THRESHOLD mode retains the optional absolute favorable/adverse gates and "
    "barrier-implied expected-value filter. Regression models remain available only through "
    "THRESHOLD mode. One signal per "
    "bar; any invalid prediction yields no signal. This restates a decision rule and makes no "
    "profitability claim."
)
_SELL_METHODOLOGY = (
    "Pinned-model continuation exit: while a real long position is open, read the prediction "
    "computed for that position by the explicitly pinned {algo} SELL model. The model is a "
    "continuation classifier over position-aware features that include the actual entry fill "
    "price (class -1 = holding would hit the risk barrier, 0 = exiting now beats holding, 1 = "
    "holding beats exiting now). A SELL signal requires a valid, fresh prediction bound to "
    "this exact position (cycle, entry price and entry time). MODEL_DIRECTED mode exits when "
    "P(RISK_EXIT) + P(EXIT_FAVORABLE) > P(HOLD_FAVORABLE), using the semantics recorded in the "
    "trained model manifest. THRESHOLD mode retains the optional absolute exit gates. An ML "
    "SELL is an ordinary discretionary exit: minimum "
    "hold and exit arbitration apply and it never outranks protective, manual, hard-risk or "
    "forced-liquidation exits. The engine closes the whole position. This restates a decision "
    "rule and makes no profitability claim."
)


def model_strategy_spec(*, strategy_id: str, algorithm: str, side: StrategyRole) -> StrategySpec:
    """Build the formal :class:`StrategySpec` shared by the four model strategies."""
    buy = side is StrategyRole.BUY
    schema = buy_parameter_schema() if buy else sell_parameter_schema()
    methodology = (_BUY_METHODOLOGY if buy else _SELL_METHODOLOGY).format(algo=_DISPLAY[algorithm])
    return StrategySpec(
        strategy_id=strategy_id,
        version="1.1.0",
        side=side,
        category="machine_learning",
        sub_category="gradient_boosted_trees",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=_WARMUP_BARS,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.LEVEL_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=schema,
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_MODEL_AUDIT),
        methodology=methodology,
        source=(
            f"Operator-trained {algorithm} model artifact produced by the mike_trade modeling "
            "framework and pinned by id/version; no profitability claim is made for any model "
            "or threshold."
        ),
        execution_limitations=(
            "Advisory signal only: no order submission, sizing, stops or execution.",
            "Consumes only an explicitly pinned, checksum-verified model prediction supplied "
            "by the composition root; it never loads a model, never falls back to another "
            "model or feed, and never signals from a missing, stale or mismatched prediction.",
            "Probabilities describe the model's training label, not guaranteed outcomes.",
            "Regular-session 1-minute bars with the model's own feed only (IEX and SIP are "
            "never mixed or substituted).",
            "Entry arbitration, market-status entry blocks, risk limits and protective stops "
            "remain in the engine and always take precedence.",
        ),
        reason_codes=MODEL_REASON_CODES,
    )


# --------------------------------------------------------------------------- decisions


def _int(params: Mapping[str, object], name: str) -> int:
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"parameter {name} must be an int")
    return value


def _bps(value: float) -> int:
    """Floor ``value`` (a fraction) to whole basis points, deterministically."""
    return math.floor(value * 10000 + 1e-9)


class _Model:
    """Common validation of the pinned prediction a strategy may act on."""

    ALGORITHM: ClassVar[str]
    ROLE: ClassVar[str]
    spec: ClassVar[StrategySpec]

    #: Implements the decision-evidence interface (``mike_trade.review.evidence``).
    EVIDENCE_INTERFACE_VERSION: ClassVar[int] = EVIDENCE_INTERFACE_VERSION

    def build_evidence(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
        evaluation: StrategyEvaluation,
    ) -> DecisionEvidence:
        """The model facts behind this signal: probabilities, thresholds, provenance, features.

        Everything comes from the decision audit this very evaluation recorded plus the validated
        prediction it consumed; nothing is estimated and no score is derived.  ``state`` already
        contains this evaluation's ``model_audit``.
        """
        spec = self.spec
        text = state.get(STATE_MODEL_AUDIT)
        try:
            audit = json.loads(text) if isinstance(text, str) else None
        except ValueError:
            audit = None
        if (
            not isinstance(audit, dict)
            or audit.get("decision") != "SIGNAL"
            or audit.get("strategy") != spec.strategy_id
        ):
            raise EvidenceError(f"{spec.strategy_id}: no signal decision audit to explain")
        recorded = audit.get("prediction") if isinstance(audit.get("prediction"), dict) else {}
        prediction = self._served_prediction(context, params)
        if prediction is not None and (
            prediction.observation_end.isoformat() != recorded.get("observation_end")
        ):
            prediction = None  # not the prediction this decision used: never mix them

        raw_probabilities: Mapping[str, float] = (
            prediction.probabilities if prediction is not None
            else recorded.get("probabilities", {})  # type: ignore[assignment]
        )
        evidence: dict[str, object] = {
            "model": {
                "model_id": params["model_id"],
                "model_version": params["model_version"],
                "algorithm": self.ALGORITHM,
                "role": self.ROLE,
                "artifact_sha256": recorded.get("artifact_sha256"),
                "task": recorded.get("task"),
                "feature_schema": recorded.get("feature_schema"),
                "feature_schema_sha256": recorded.get("feature_schema_sha256"),
                "feed": recorded.get("feed"),
                "timeframe": recorded.get("timeframe"),
                "horizon_bars": recorded.get("horizon_bars"),
                "observation_end": recorded.get("observation_end"),
                "predicted_at": recorded.get("predicted_at"),
            },
            "decision_mode": audit.get("decision_mode"),
            "thresholds": audit.get("thresholds", {}),
            "class_probabilities": dict(raw_probabilities),
        }
        if prediction is not None:
            evidence["features"] = dict(prediction.feature_values)
            evidence["model"]["label_id"] = prediction.label_id  # type: ignore[index]
            evidence["model"]["symbol_in_training_universe"] = (  # type: ignore[index]
                prediction.symbol_in_training_universe
            )
        else:
            evidence["features_unavailable"] = "the consumed prediction is no longer in the context"
        mode = str(audit.get("decision_mode"))
        if self.ROLE == "buy":
            favorable, adverse = str(params["favorable_class"]), str(params["adverse_class"])
            others = {k: v for k, v in raw_probabilities.items() if k not in (favorable, adverse)}
            probabilities: dict[str, object] = {
                "favorable": raw_probabilities.get(favorable),
                "adverse": raw_probabilities.get(adverse),
            }
            if len(others) == 1:
                probabilities["neutral"] = next(iter(others.values()))
            elif others:
                probabilities["other"] = others
            evidence["class_roles"] = {"favorable": favorable, "adverse": adverse}
            evidence["probabilities"] = probabilities
            evidence["decision_inputs"] = {
                k: audit[k] for k in (
                    "favorable_probability_bps", "adverse_probability_bps",
                    "expected_value_bps", "expected_value_basis",
                ) if k in audit
            }
            reason_code = (
                "MODEL_FAVORABLE" if mode == ModelDecisionMode.MODEL_DIRECTED.value
                else "MODEL_THRESHOLDS_MET"
            )
            reason = (
                "P(favorable) exceeds P(adverse)" if reason_code == "MODEL_FAVORABLE"
                else "favorable/adverse probability thresholds were met"
            )
        else:
            evidence["class_roles"] = {
                "risk_exit": _CLASS_RISK_EXIT,
                "exit_favorable": _CLASS_EXIT_FAVORABLE,
                "hold_favorable": _CLASS_HOLD_FAVORABLE,
            }
            evidence["probabilities"] = {
                "risk_exit": raw_probabilities.get(_CLASS_RISK_EXIT),
                "exit_favorable": raw_probabilities.get(_CLASS_EXIT_FAVORABLE),
                "hold_favorable": raw_probabilities.get(_CLASS_HOLD_FAVORABLE),
            }
            evidence["decision_inputs"] = {
                k: audit[k] for k in (
                    "exit_probability_bps", "risk_exit_probability_bps", "hold_probability_bps",
                ) if k in audit
            }
            if audit.get("position") is not None:
                evidence["position"] = audit["position"]
            if prediction is not None:
                evidence["position_features"] = dict(prediction.position_features)
            reason_code = (
                "MODEL_EXIT_PREFERRED" if mode == ModelDecisionMode.MODEL_DIRECTED.value
                else "MODEL_EXIT_THRESHOLD_MET"
            )
            reason = (
                "P(risk exit)+P(exit favorable) exceeds P(hold favorable)"
                if reason_code == "MODEL_EXIT_PREFERRED"
                else "the absolute exit probability thresholds were met"
            )
        return make_evidence(
            spec, evaluation, context, reason_code=reason_code, reason=reason, evidence=evidence
        )

    @classmethod
    def _served_prediction(
        cls, context: MarketContext, params: Mapping[str, object]
    ) -> ServedPrediction | None:
        inputs = context.model_inputs
        if inputs is None:
            return None
        result = inputs.result_for(cls.model_reference(params).key)
        return None if result is None else result.prediction

    @classmethod
    def model_reference(cls, params: Mapping[str, object]) -> ModelReference:
        checksum = str(params.get("artifact_sha256", "") or "")
        return ModelReference(
            model_id=str(params["model_id"]),
            model_version=str(params["model_version"]),
            algorithm=cls.ALGORITHM,
            role=cls.ROLE,
            artifact_sha256=checksum or None,
        )

    @classmethod
    def _base_audit(
        cls,
        context: MarketContext,
        params: Mapping[str, object],
        latest: Candle | None,
    ) -> dict[str, object]:
        return {
            "strategy": cls.spec.strategy_id,
            "version": cls.spec.version,
            "symbol": context.symbol,
            "bar_start": None if latest is None else latest.start.isoformat(),
            "bar_end": None if latest is None else latest.end.isoformat(),
            "model": f"{params.get('model_id')}@{params.get('model_version')}",
            "thresholds": {
                key: params[key]
                for key in sorted(params)
                if key.endswith("_bps") or key in ("favorable_class", "adverse_class")
            },
            "decision_mode": params.get(
                "decision_mode", ModelDecisionMode.MODEL_DIRECTED.value
            ),
        }

    @classmethod
    def _gate(
        cls, context: MarketContext, bars: tuple[Candle, ...]
    ) -> tuple[ReasonCode, ModelReason] | None:
        if context.freshness.stale:
            return ReasonCode.STALE_DATA, ModelReason.STALE_CONTEXT
        if context.session_status is not SessionStatus.REGULAR:
            return ReasonCode.NOT_ELIGIBLE, ModelReason.NOT_REGULAR_SESSION
        if not context.completed_bars_only:
            return ReasonCode.INSUFFICIENT_DATA, ModelReason.INCOMPLETE_BARS
        if len(bars) < _WARMUP_BARS:
            return ReasonCode.INSUFFICIENT_DATA, ModelReason.NOT_WARMED
        return None

    @classmethod
    def _prediction(
        cls,
        context: MarketContext,
        params: Mapping[str, object],
        latest: Candle,
        position: PositionFacts | None,
    ) -> tuple[ServedPrediction | None, tuple[ReasonCode, str] | None]:
        """Return the validated prediction, or the (reason code, reason) that rejects it."""
        reference = cls.model_reference(params)
        inputs = context.model_inputs
        if inputs is None:
            return None, (ReasonCode.INSUFFICIENT_DATA, ModelReason.NO_MODEL_INPUTS)
        result: ModelPredictionResult | None = inputs.result_for(reference.key)
        if result is None:
            return None, (ReasonCode.INSUFFICIENT_DATA, ModelReason.PREDICTION_MISSING)
        if result.reference != reference:
            return None, (ReasonCode.INSUFFICIENT_DATA, ModelReason.MODEL_MISMATCH)
        if result.prediction is None:
            failure = result.failure
            code = (
                ReasonCode.STALE_DATA
                if failure
                in (PredictionFailure.STALE_OBSERVATION, PredictionFailure.FUTURE_OBSERVATION)
                else ReasonCode.INSUFFICIENT_DATA
            )
            return None, (code, f"{ModelReason.PREDICTION_UNAVAILABLE}:{failure}")
        prediction = result.prediction
        insufficient = ReasonCode.INSUFFICIENT_DATA
        if (
            prediction.model_id != reference.model_id
            or prediction.model_version != reference.model_version
        ):
            return None, (insufficient, ModelReason.MODEL_MISMATCH)
        if prediction.algorithm != cls.ALGORITHM:
            return None, (insufficient, ModelReason.ALGORITHM_MISMATCH)
        if prediction.role != cls.ROLE:
            return None, (insufficient, ModelReason.ROLE_MISMATCH)
        if reference.artifact_sha256 is not None and (
            prediction.artifact_sha256 != reference.artifact_sha256
        ):
            return None, (insufficient, ModelReason.CHECKSUM_MISMATCH)
        if prediction.symbol != context.symbol:
            return None, (insufficient, ModelReason.SYMBOL_MISMATCH)
        if context.feed_id is None or prediction.feed != context.feed_id:
            return None, (insufficient, ModelReason.FEED_MISMATCH)
        if prediction.timeframe != latest.timeframe.value or prediction.adjustment != "raw":
            return None, (insufficient, ModelReason.PROVENANCE_MISMATCH)
        horizon = _int(params, "expected_horizon_bars")
        if horizon and prediction.horizon_bars != horizon:
            return None, (insufficient, ModelReason.HORIZON_MISMATCH)
        if prediction.observation_end > context.as_of:
            return None, (ReasonCode.STALE_DATA, ModelReason.FUTURE_OBSERVATION)
        if prediction.observation_end != latest.end or prediction.observation_start != latest.start:
            return None, (ReasonCode.STALE_DATA, ModelReason.OBSERVATION_NOT_LATEST_BAR)
        limit = timedelta(seconds=int(params["maximum_prediction_age_seconds"]))  # type: ignore[call-overload]
        if (
            context.as_of - prediction.observation_end > limit
            or prediction.predicted_at - prediction.observation_end > limit
        ):
            return None, (ReasonCode.STALE_DATA, ModelReason.STALE_PREDICTION)
        if bool(params.get("require_trained_symbol", True)) and not (
            prediction.symbol_in_training_universe
        ):
            return None, (ReasonCode.NOT_ELIGIBLE, ModelReason.UNTRAINED_SYMBOL)
        if cls.ROLE == "sell":
            if position is None or prediction.position_fingerprint != position.fingerprint:
                return None, (insufficient, ModelReason.POSITION_MISMATCH)
        elif prediction.position_fingerprint is not None:
            return None, (insufficient, ModelReason.POSITION_MISMATCH)
        return prediction, None

    @classmethod
    def _finish(
        cls,
        context: MarketContext,
        latest: Candle | None,
        state: Mapping[str, object],
        decision: ModelDecision,
    ) -> StrategyEvaluation:
        spec = cls.spec
        updates: dict[str, object] = {}
        if decision.reason not in _IDLE_REASONS:  # idle evaluations are not worth persisting
            updates[STATE_MODEL_AUDIT] = decision.audit_text()
        bar_time = None if latest is None else latest.start
        if decision.signal and latest is not None:
            iso = latest.start.isoformat()
            if state.get(STATE_LAST_SIGNAL_BAR) != iso:
                updates[STATE_LAST_SIGNAL_BAR] = iso
                return StrategyEvaluation.for_signal(
                    spec,
                    context.symbol,
                    latest.start,
                    reference_price=latest.close,
                    state_updates=updates,
                )
            return StrategyEvaluation.no_signal(
                spec,
                context.symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=bar_time,
                state_updates=updates,
            )
        return StrategyEvaluation.no_signal(
            spec,
            context.symbol,
            (decision.reason_code,),
            bar_time=bar_time,
            state_updates=updates,
        )

    @classmethod
    def _rejected(
        cls,
        base: dict[str, object],
        code: ReasonCode,
        reason: str,
        extra: Mapping[str, object] | None = None,
    ) -> ModelDecision:
        audit = {**base, "decision": "NO_SIGNAL", "reason": str(reason), **(extra or {})}
        return ModelDecision(False, code, str(reason), audit)


class ModelProbabilityBuy(_Model, BuyStrategy):
    """BUY when a pinned model's calibrated outcome probabilities clear configured thresholds."""

    ROLE: ClassVar[str] = "buy"

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _WARMUP_BARS

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        bars = context.candles.get(CandleTimeframe.M1, ())
        latest = bars[-1] if bars else None
        try:
            decision = self.decide(context, params, state)
        except Exception as exc:  # isolation: a defect here must not stop other strategies
            base = self._base_audit(context, params, latest)
            decision = self._rejected(
                base,
                ReasonCode.INSUFFICIENT_DATA,
                ModelReason.EVALUATION_ERROR,
                {"error": type(exc).__name__},
            )
        return self._finish(context, latest, state, decision)

    @classmethod
    def decide(
        cls,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> ModelDecision:
        bars = context.candles.get(CandleTimeframe.M1, ())
        latest = bars[-1] if bars else None
        base = cls._base_audit(context, params, latest)
        if state.get(ELIGIBILITY_KEY) != ELIGIBILITY_WATCHING_BUY:
            return cls._rejected(base, ReasonCode.NOT_ELIGIBLE, ModelReason.NOT_ARMED)
        gate = cls._gate(context, bars)
        if gate is not None or latest is None:
            code, reason = gate or (ReasonCode.INSUFFICIENT_DATA, ModelReason.NOT_WARMED)
            return cls._rejected(base, code, reason)
        prediction, rejection = cls._prediction(context, params, latest, None)
        if prediction is None:
            assert rejection is not None
            return cls._rejected(base, rejection[0], rejection[1])
        base["prediction"] = prediction.to_audit()
        return cls._classify(base, prediction, params)

    @classmethod
    def _classify(
        cls,
        base: dict[str, object],
        prediction: ServedPrediction,
        params: Mapping[str, object],
    ) -> ModelDecision:
        no_signal = ReasonCode.NO_SIGNAL
        mode = ModelDecisionMode(str(params["decision_mode"]))
        minimum_bps = _int(params, "minimum_favorable_probability_bps")
        maximum_adverse_bps = _int(params, "maximum_adverse_probability_bps")
        use_ev = bool(params["use_expected_value"])
        minimum_ev_bps = _int(params, "minimum_expected_value_bps")
        if prediction.value is not None:  # regression: the value is the modeled net return
            if mode is ModelDecisionMode.MODEL_DIRECTED:
                return cls._rejected(
                    base, ReasonCode.INSUFFICIENT_DATA, ModelReason.UNSUPPORTED_OBJECTIVE
                )
            if prediction.label_id != "forward_return":
                return cls._rejected(
                    base, ReasonCode.INSUFFICIENT_DATA, ModelReason.UNSUPPORTED_OBJECTIVE
                )
            regression_bps = prediction.value * 10000
            regression_extra: dict[str, object] = {"expected_value_bps": round(regression_bps, 4)}
            if regression_bps < minimum_ev_bps:
                return cls._rejected(
                    base, no_signal, ModelReason.BELOW_EXPECTED_VALUE, regression_extra
                )
            return ModelDecision(
                True,
                ReasonCode.SIGNAL,
                ModelReason.SIGNAL,
                {**base, "decision": "SIGNAL", "reason": "SIGNAL", **regression_extra},
            )
        favorable = str(params["favorable_class"])
        adverse = str(params["adverse_class"])
        if (
            favorable == adverse
            or favorable not in prediction.probabilities
            or adverse not in prediction.probabilities
        ):
            return cls._rejected(
                base, ReasonCode.INSUFFICIENT_DATA, ModelReason.UNSUPPORTED_OBJECTIVE
            )
        p_favorable = prediction.probabilities[favorable]
        p_adverse = prediction.probabilities[adverse]
        extra: dict[str, object] = {
            "favorable_probability_bps": _bps(p_favorable),
            "adverse_probability_bps": _bps(p_adverse),
        }
        if mode is ModelDecisionMode.MODEL_DIRECTED:
            if p_favorable <= p_adverse:
                return cls._rejected(
                    base, no_signal, ModelReason.FAVORABLE_NOT_PREFERRED, extra
                )
        else:
            if _bps(p_favorable) < minimum_bps:
                return cls._rejected(
                    base, no_signal, ModelReason.BELOW_FAVORABLE_THRESHOLD, extra
                )
            if _bps(p_adverse) > maximum_adverse_bps:
                return cls._rejected(base, no_signal, ModelReason.ABOVE_ADVERSE_THRESHOLD, extra)
        if mode is ModelDecisionMode.THRESHOLD and use_ev:
            returns = prediction.class_net_returns
            if not returns or set(returns) != set(prediction.probabilities):
                return cls._rejected(base, no_signal, ModelReason.EXPECTED_VALUE_UNAVAILABLE, extra)
            expected = sum(
                (
                    Decimal(repr(p)) * returns[label]
                    for label, p in prediction.probabilities.items()
                ),
                Decimal(0),
            )
            expected_bps = float(expected * _BPS)
            extra["expected_value_bps"] = round(expected_bps, 4)
            extra["expected_value_basis"] = prediction.class_return_basis
            if expected_bps < minimum_ev_bps:
                return cls._rejected(base, no_signal, ModelReason.BELOW_EXPECTED_VALUE, extra)
        return ModelDecision(
            True,
            ReasonCode.SIGNAL,
            ModelReason.SIGNAL,
            {**base, "decision": "SIGNAL", "reason": "SIGNAL", **extra},
        )


class ModelProbabilitySell(_Model, SellStrategy):
    """SELL an actual open long when a pinned continuation model favors exiting now."""

    ROLE: ClassVar[str] = "sell"

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _WARMUP_BARS

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        bars = context.candles.get(CandleTimeframe.M1, ())
        latest = bars[-1] if bars else None
        try:
            decision = self.decide(context, params, state)
        except Exception as exc:  # isolation: a defect here must not stop other strategies
            base = self._base_audit(context, params, latest)
            decision = self._rejected(
                base,
                ReasonCode.INSUFFICIENT_DATA,
                ModelReason.EVALUATION_ERROR,
                {"error": type(exc).__name__},
            )
        return self._finish(context, latest, state, decision)

    @classmethod
    def decide(
        cls,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> ModelDecision:
        bars = context.candles.get(CandleTimeframe.M1, ())
        latest = bars[-1] if bars else None
        base = cls._base_audit(context, params, latest)
        inputs = context.model_inputs
        position = None if inputs is None else inputs.position
        if position is None:
            return cls._rejected(base, ReasonCode.NOT_ELIGIBLE, ModelReason.NO_POSITION)
        base["position"] = {
            "cycle_id": position.cycle_id,
            "entry_price": str(position.entry_price),
            "entry_time": position.entry_time.isoformat(),
            "quantity": str(position.quantity),
            "fingerprint": position.fingerprint,
        }
        gate = cls._gate(context, bars)
        if gate is not None or latest is None:
            code, reason = gate or (ReasonCode.INSUFFICIENT_DATA, ModelReason.NOT_WARMED)
            return cls._rejected(base, code, reason)
        if position.symbol != context.symbol or position.entry_time > latest.end:
            return cls._rejected(base, ReasonCode.INSUFFICIENT_DATA, ModelReason.POSITION_MISMATCH)
        prediction, rejection = cls._prediction(context, params, latest, position)
        if prediction is None:
            assert rejection is not None
            return cls._rejected(base, rejection[0], rejection[1])
        base["prediction"] = prediction.to_audit()
        if (
            prediction.label_id != "sell_continuation"
            or tuple(prediction.class_labels) != _SELL_CLASSES
            or not prediction.probabilities
        ):
            return cls._rejected(
                base, ReasonCode.INSUFFICIENT_DATA, ModelReason.UNSUPPORTED_OBJECTIVE
            )
        p_risk = prediction.probabilities[_CLASS_RISK_EXIT]
        p_exit = p_risk + prediction.probabilities[_CLASS_EXIT_FAVORABLE]
        extra: dict[str, object] = {
            "exit_probability_bps": _bps(p_exit),
            "risk_exit_probability_bps": _bps(p_risk),
            "hold_probability_bps": _bps(prediction.probabilities[_CLASS_HOLD_FAVORABLE]),
        }
        mode = ModelDecisionMode(str(params["decision_mode"]))
        if mode is ModelDecisionMode.MODEL_DIRECTED:
            if p_exit > prediction.probabilities[_CLASS_HOLD_FAVORABLE]:
                return ModelDecision(
                    True,
                    ReasonCode.SIGNAL,
                    ModelReason.SIGNAL,
                    {**base, "decision": "SIGNAL", "reason": "SIGNAL", **extra},
                )
            return cls._rejected(base, ReasonCode.NO_SIGNAL, ModelReason.HOLD_PREFERRED, extra)
        minimum_exit = _int(params, "minimum_exit_probability_bps")
        minimum_risk = _int(params, "minimum_risk_exit_probability_bps")
        if _bps(p_exit) >= minimum_exit or (minimum_risk > 0 and _bps(p_risk) >= minimum_risk):
            return ModelDecision(
                True,
                ReasonCode.SIGNAL,
                ModelReason.SIGNAL,
                {**base, "decision": "SIGNAL", "reason": "SIGNAL", **extra},
            )
        return cls._rejected(base, ReasonCode.NO_SIGNAL, ModelReason.HOLD_PREFERRED, extra)
