"""Executable, validated BUY presets referencing registered strategies.

Each preset references a registered BUY strategy by ``(strategy_id, version)``
with a parameter dict. Parameter values are the shipped spec defaults
(reference values only, not fitted or optimized); presets carry no
profitability claim and no confidence values.

The ``vwap_reference`` preset references ``vwap_bounce`` and names its SELL
partner id ``vwap_breakdown`` by string only. There is no hard code
dependency between them: this module never imports a SELL strategy module,
and the pairing exists only as configuration. The reference preset is
shadow-only and is never enabled for paper or live modes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Sequence

from mike_trade.strategies.buy.momentum_breakout import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_VOLUME_MULTIPLE,
    STRATEGY_ID as MOMENTUM_BREAKOUT_ID,
    STRATEGY_VERSION as MOMENTUM_BREAKOUT_VERSION,
)
from mike_trade.strategies.buy.opening_range_breakout import (
    DEFAULT_OPENING_RANGE_MINUTES,
    STRATEGY_ID as OPENING_RANGE_BREAKOUT_ID,
    STRATEGY_VERSION as OPENING_RANGE_BREAKOUT_VERSION,
)
from mike_trade.strategies.buy.vwap_bounce import (
    STRATEGY_ID as VWAP_BOUNCE_ID,
    STRATEGY_VERSION as VWAP_BOUNCE_VERSION,
)
from mike_trade.strategies.registry import StrategyRegistry, StrategyRegistryError

__all__ = [
    "BuyPreset",
    "OPENING_RANGE_BASELINE",
    "MOMENTUM_BASELINE",
    "VWAP_REFERENCE",
    "BUY_PRESETS",
    "build_buy_registry",
    "validate_all_presets",
]

#: Allowed execution mode for the shadow-only reference preset.
SHADOW_ONLY: tuple[str, ...] = ("shadow",)

#: SELL partner id named by string only (never imported) for the reference preset.
VWAP_BREAKDOWN_SELL_ID = "vwap_breakdown"


@dataclass(frozen=True, slots=True)
class BuyPreset:
    """One executable BUY preset: a pinned strategy plus validated parameters."""

    preset_id: str
    strategy_id: str
    version: str
    params: Mapping[str, object] = field(default_factory=dict)
    allowed_modes: tuple[str, ...] = SHADOW_ONLY
    sell_partner_id: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.preset_id, str) or not self.preset_id.strip():
            raise ValueError("preset_id must be a non-empty str")
        if not isinstance(self.strategy_id, str) or not self.strategy_id.strip():
            raise ValueError("strategy_id must be a non-empty str")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty str")
        if not isinstance(self.params, Mapping):
            raise ValueError("params must be a mapping")
        object.__setattr__(
            self, "params", MappingProxyType(dict(self.params))
        )
        if not isinstance(self.allowed_modes, tuple) or not self.allowed_modes:
            raise ValueError("allowed_modes must be a non-empty tuple")
        if self.sell_partner_id is not None and (
            not isinstance(self.sell_partner_id, str) or not self.sell_partner_id.strip()
        ):
            raise ValueError("sell_partner_id must be a non-empty str or None")
        if not isinstance(self.description, str):
            raise ValueError("description must be str")


#: Baseline preset: shipped spec defaults for opening_range_breakout.
OPENING_RANGE_BASELINE = BuyPreset(
    preset_id="opening_range_baseline",
    strategy_id=OPENING_RANGE_BREAKOUT_ID,
    version=OPENING_RANGE_BREAKOUT_VERSION,
    params={
        "opening_range_minutes": DEFAULT_OPENING_RANGE_MINUTES,
        "min_breakout_volume": Decimal("0"),
    },
    allowed_modes=SHADOW_ONLY,
    description=(
        "Baseline opening_range_breakout preset using the shipped spec defaults "
        "(reference values only, not a profitability claim)."
    ),
)

#: Baseline preset: shipped spec defaults for momentum_breakout.
MOMENTUM_BASELINE = BuyPreset(
    preset_id="momentum_baseline",
    strategy_id=MOMENTUM_BREAKOUT_ID,
    version=MOMENTUM_BREAKOUT_VERSION,
    params={
        "lookback_bars": DEFAULT_LOOKBACK_BARS,
        "volume_multiple": DEFAULT_VOLUME_MULTIPLE,
    },
    allowed_modes=SHADOW_ONLY,
    description=(
        "Baseline momentum_breakout preset using the shipped spec defaults "
        "(reference values only, not a profitability claim)."
    ),
)

#: Shadow-only reference preset pairing vwap_bounce BUY with vwap_breakdown SELL by string id only.
VWAP_REFERENCE = BuyPreset(
    preset_id="vwap_reference",
    strategy_id=VWAP_BOUNCE_ID,
    version=VWAP_BOUNCE_VERSION,
    params={},
    allowed_modes=SHADOW_ONLY,
    sell_partner_id=VWAP_BREAKDOWN_SELL_ID,
    description=(
        "Shadow-only vwap_reference BUY preset: vwap_bounce paired with the "
        "vwap_breakdown SELL id by configuration only (no hard code dependency)."
    ),
)

#: Every shipped BUY preset, ordered by preset id.
BUY_PRESETS: tuple[BuyPreset, ...] = (
    MOMENTUM_BASELINE,
    OPENING_RANGE_BASELINE,
    VWAP_REFERENCE,
)


def build_buy_registry() -> StrategyRegistry:
    """Return a registry with the three shipped BUY strategies registered."""
    from mike_trade.strategies.buy.momentum_breakout import MomentumBreakoutBuy
    from mike_trade.strategies.buy.opening_range_breakout import OpeningRangeBreakoutBuy
    from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy

    registry = StrategyRegistry()
    registry.register(OpeningRangeBreakoutBuy)
    registry.register(MomentumBreakoutBuy)
    registry.register(VwapBounceBuy)
    return registry


def validate_all_presets(
    registry: StrategyRegistry | None = None,
    presets: Sequence[BuyPreset] | None = None,
) -> Mapping[str, Mapping[str, object]]:
    """Validate every preset against registry parameter schemas; fail closed.

    For each preset, resolves ``(strategy_id, version)`` via ``registry.get``
    (unknown strategy or version raises ``StrategyRegistryError``) and
    validates ``params`` via the spec schema (invalid parameters raise
    ``ParameterValidationError``). Also rejects a ``vwap_reference`` preset
    that allows any mode other than shadow. Returns a read-only mapping of
    ``preset_id`` to validated parameter mappings.
    """
    active = build_buy_registry() if registry is None else registry
    selected = BUY_PRESETS if presets is None else tuple(presets)
    if not selected:
        raise ValueError("no presets to validate")
    seen: set[str] = set()
    validated: dict[str, Mapping[str, object]] = {}
    for preset in selected:
        if not isinstance(preset, BuyPreset):
            raise ValueError(f"expected BuyPreset; got {type(preset).__name__}")
        if preset.preset_id in seen:
            raise ValueError(f"duplicate preset_id {preset.preset_id!r}")
        seen.add(preset.preset_id)
        try:
            strategy_cls = active.get(preset.strategy_id, preset.version)
        except StrategyRegistryError:
            raise
        if (strategy_cls.spec.strategy_id, strategy_cls.spec.version) != (
            preset.strategy_id,
            preset.version,
        ):
            raise StrategyRegistryError(
                f"preset {preset.preset_id!r} pins unknown strategy "
                f"{preset.strategy_id!r} version {preset.version!r}",
                code="not_found",
                context={
                    "strategy_id": preset.strategy_id,
                    "version": preset.version,
                },
            )
        # Delegates to the spec schema; raises ParameterValidationError closed.
        validated_params = active.validate_parameters(
            preset.strategy_id, preset.params, version=preset.version
        )
        if preset.preset_id == VWAP_REFERENCE.preset_id or (
            preset.sell_partner_id is not None
        ):
            if tuple(preset.allowed_modes) != SHADOW_ONLY:
                raise ValueError(
                    f"preset {preset.preset_id!r} is shadow-only; "
                    f"got allowed_modes={preset.allowed_modes!r}"
                )
            for forbidden in ("paper", "live"):
                if forbidden in preset.allowed_modes:
                    raise ValueError(
                        f"preset {preset.preset_id!r} must never enable {forbidden!r}"
                    )
        validated[preset.preset_id] = validated_params
    return MappingProxyType(validated)
