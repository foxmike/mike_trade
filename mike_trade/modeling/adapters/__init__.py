"""Trusted native tree-model adapters.

Shared configuration types import freely; the XGBoost and LightGBM adapters (which import the
native libraries) are loaded on first access, so serving or configuring models never pays for
a library no configured model uses.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from mike_trade.modeling.adapters.common import (
    ClassBalance,
    ComputeDevice,
    FitResult,
    LightGBMTrainingConfig,
    XGBoostTrainingConfig,
)

if TYPE_CHECKING:
    from mike_trade.modeling.adapters.lightgbm import (
        LightGBMAdapterFactory,
        LightGBMPredictor,
        LightGBMTrainer,
    )
    from mike_trade.modeling.adapters.xgboost import (
        XGBoostAdapterFactory,
        XGBoostPredictor,
        XGBoostTrainer,
    )

_LAZY = {
    "LightGBMAdapterFactory": "lightgbm",
    "LightGBMPredictor": "lightgbm",
    "LightGBMTrainer": "lightgbm",
    "XGBoostAdapterFactory": "xgboost",
    "XGBoostPredictor": "xgboost",
    "XGBoostTrainer": "xgboost",
}

__all__ = [
    "ClassBalance",
    "ComputeDevice",
    "FitResult",
    "LightGBMAdapterFactory",
    "LightGBMPredictor",
    "LightGBMTrainer",
    "LightGBMTrainingConfig",
    "XGBoostAdapterFactory",
    "XGBoostPredictor",
    "XGBoostTrainer",
    "XGBoostTrainingConfig",
]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"mike_trade.modeling.adapters.{module}"), name)
    globals()[name] = value
    return value
