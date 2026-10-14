"""Composition-root glue: completed candles -> inference requests -> immutable model inputs.

This is the single path every execution mode (BACKTEST, SHADOW, PAPER, LIVE) uses to give a
pure strategy its model predictions.  It reads only data the composition root already holds
(the shared :class:`MarketContext`, the completed-candle store, the per-bar VWAP ledger) and
calls the :class:`ModelInferenceService`.  It performs no I/O, opens no connections, places no
orders, and never loads a model.

Why the VWAP ledger exists: the trained ``vwap_distance`` feature uses each *bar's own* VWAP as
published with the historical bar.  Operational candles carry no bar VWAP, so the composition
root records it as bars arrive (:meth:`BarVwapLedger.record`).  A missing VWAP makes the model
unavailable for that bar; it is never replaced by a different VWAP definition.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from mike_trade.domain.model_signals import (
    ModelInputs,
    ModelPredictionResult,
    ModelReference,
    PositionFacts,
    PredictionFailure,
)
from mike_trade.market_data.candles import Candle, CandleTimeframe
from mike_trade.market_data.context import MarketContext
from mike_trade.modeling.aggregation import is_candle_boundary
from mike_trade.modeling.contracts import ModelBar
from mike_trade.modeling.inference import InferenceRequest, ModelInferenceService
from mike_trade.modeling.inputs import model_bar_from_candle

__all__ = ["BarVwapLedger", "CandleHistory", "ModelInputProvider", "build_inference_service"]


class CandleHistory(Protocol):
    """Read-only completed-candle history (``CandleBuilder`` satisfies this)."""

    def get_completed_candles(
        self, symbol: str, timeframe: CandleTimeframe, limit: int | None = None
    ) -> Sequence[Candle]: ...


class BarVwapLedger:
    """Bounded record of each completed 1-minute bar's own VWAP, keyed by (symbol, bar start)."""

    def __init__(self, per_symbol: int = 1024) -> None:
        if per_symbol < 1:
            raise ValueError("per_symbol must be positive")
        self._per_symbol = per_symbol
        self._lock = threading.Lock()
        self._values: dict[str, OrderedDict[datetime, Decimal]] = {}

    def record(self, symbol: str, start: datetime, vwap: Decimal | None) -> None:
        """Remember ``vwap`` for the bar starting at ``start``; ``None`` is ignored."""
        if vwap is None or not isinstance(vwap, Decimal) or not vwap.is_finite() or vwap <= 0:
            return
        key = symbol.strip().upper()
        with self._lock:
            series = self._values.setdefault(key, OrderedDict())
            series[start] = vwap
            series.move_to_end(start)
            while len(series) > self._per_symbol:
                series.popitem(last=False)

    def get(self, symbol: str, start: datetime) -> Decimal | None:
        with self._lock:
            series = self._values.get(symbol.strip().upper())
            return None if series is None else series.get(start)


class ModelInputProvider:
    """Builds :class:`ModelInputs` for a symbol from completed candles and optional position."""

    def __init__(
        self,
        service: ModelInferenceService,
        history: CandleHistory,
        vwaps: BarVwapLedger,
        *,
        on_result: Callable[[ModelPredictionResult], None] | None = None,
    ) -> None:
        self._service = service
        self._history = history
        self._vwaps = vwaps
        self._on_result = on_result

    @property
    def service(self) -> ModelInferenceService:
        return self._service

    @property
    def vwaps(self) -> BarVwapLedger:
        return self._vwaps

    # ------------------------------------------------------------------ bars

    def _model_bars(
        self, symbol: str, candles: Sequence[Candle], count: int
    ) -> tuple[ModelBar, ...]:
        """The last ``count`` completed regular-session candles as :class:`ModelBar`.

        Walking backwards stops at the first candle outside the regular session (earlier bars
        belong to an earlier session, which the feature pipeline resets on anyway).
        """
        if not candles:
            return ()
        converted: list[ModelBar] = []
        for candle in reversed(candles[-count:]):
            try:
                converted.append(
                    model_bar_from_candle(candle, vwap=self._vwaps.get(symbol, candle.start))
                )
            except ValueError:
                break
        converted.reverse()
        return tuple(converted)

    @staticmethod
    def _count_since(candles: Sequence[Candle], since: datetime) -> int:
        floor = since.replace(second=0, microsecond=0)
        return sum(1 for candle in candles if candle.start >= floor)

    # ------------------------------------------------------------------ inputs

    def inputs(
        self,
        context: MarketContext,
        references: Sequence[ModelReference],
        position: PositionFacts | None = None,
    ) -> ModelInputs:
        symbol = context.symbol
        candles = context.candles.get(CandleTimeframe.M1, ())
        results: dict[str, ModelPredictionResult] = {}
        for reference in references:
            result = self._one(symbol, candles, reference, position)
            results[reference.key] = result
            if self._on_result is not None:
                self._on_result(result)
        return ModelInputs(symbol, context.as_of, results, position)

    def _one(
        self,
        symbol: str,
        candles: Sequence[Candle],
        reference: ModelReference,
        position: PositionFacts | None,
    ) -> ModelPredictionResult:
        loaded = self._service.loaded(reference)
        if loaded is None:
            return ModelPredictionResult(
                reference,
                failure=PredictionFailure.MODEL_NOT_LOADED,
                detail=f"{reference.key} is not served by this runtime",
            )
        if not candles:
            return ModelPredictionResult(
                reference,
                failure=PredictionFailure.INSUFFICIENT_HISTORY,
                detail="no completed 1m candles in the context",
            )
        if loaded.candle is not None and not is_candle_boundary(
            candles[-1].end, loaded.candle.policy.minutes
        ):
            # A candle model only looks at a candle that has just completed; skip the cheap
            # idle minutes without converting any history.
            return ModelPredictionResult(
                reference,
                failure=PredictionFailure.NOT_CANDLE_BOUNDARY,
                detail=f"not the end of a {loaded.candle.policy.timeframe} candle",
            )
        window = loaded.feature_window
        count = window
        if position is not None:
            count = max(count, self._count_since(candles, position.entry_time))
        bars = self._model_bars(symbol, candles, count)
        if not bars or bars[-1].start != candles[-1].start:
            return ModelPredictionResult(
                reference,
                failure=PredictionFailure.NOT_REGULAR_SESSION,
                detail="the latest completed candle is outside the regular session",
            )
        reference_bars: dict[str, tuple[ModelBar, ...]] = {}
        for name in loaded.manifest.feature_schema.required_reference_symbols:
            series = self._history.get_completed_candles(name, CandleTimeframe.M1, window + 2)
            reference_bars[name] = self._model_bars(name, tuple(series), window + 2)
        return self._service.predict(
            InferenceRequest(
                reference=reference,
                symbol=symbol,
                bars=bars,
                references=reference_bars,
                position=position,
            )
        )

    def enrich(
        self,
        context: MarketContext,
        references: Sequence[ModelReference],
        position: PositionFacts | None = None,
    ) -> MarketContext:
        """Return ``context`` carrying immutable model inputs (the original is untouched)."""
        return replace(context, model_inputs=self.inputs(context, references, position))


def build_inference_service(
    model_root: str | Path,
    references: Sequence[ModelReference],
    clock: Callable[[], datetime],
) -> ModelInferenceService:
    """Load every pinned artifact once from the immutable repository at ``model_root``.

    The only place the native tree libraries are imported for serving, and only when at least
    one model-driven strategy is configured.  Raises :class:`ModelLoadError` for a missing,
    corrupt, mismatched or incompatible artifact; nothing is trained or substituted.
    """
    from mike_trade.modeling.adapters import LightGBMAdapterFactory, XGBoostAdapterFactory
    from mike_trade.modeling.artifacts import FileModelRepository
    from mike_trade.modeling.registry import ModelAdapterRegistry

    adapters = ModelAdapterRegistry()
    needed = {reference.algorithm for reference in references}
    if "xgboost" in needed:
        adapters.register(XGBoostAdapterFactory())
    if "lightgbm" in needed:
        adapters.register(LightGBMAdapterFactory())
    return ModelInferenceService(FileModelRepository(model_root), adapters, references, clock=clock)
