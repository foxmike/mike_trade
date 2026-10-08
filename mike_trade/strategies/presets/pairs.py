"""Executable, validated BUY/SELL pair presets.

A pair preset names an independently registered BUY strategy and an
independently registered SELL strategy by ``(strategy_id, version)`` through
configuration only. The ``vwap_reference`` pair references ``vwap_bounce``
(BUY) and ``vwap_breakdown`` (SELL); neither strategy module imports the
other, and this module resolves both sides only through
``StrategyRegistry``. The BUY leg reuses the pinned identity from
``mike_trade.strategies.presets.buy_presets``.

The reference pair is shadow-only: it carries an allowed-modes set of
``{"shadow"}`` and validation fails closed on any request to use it in
``paper`` or ``live``. The pair requires trailing protection and uses the
``first_valid_exit`` arbitration policy (the first valid exit candidate in
priority order is selected; there is no voting and no confidence score).

This module makes no profitability claim and carries no confidence values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import ParameterValidationError
from mike_trade.strategies.presets.buy_presets import (
    VWAP_REFERENCE as _BUY_VWAP_REFERENCE,
)
from mike_trade.strategies.registry import StrategyRegistry, StrategyRegistryError
from mike_trade.strategies.sell.vwap_breakdown import (
    STRATEGY_ID as _VWAP_BREAKDOWN_ID,
)
from mike_trade.strategies.sell.vwap_breakdown import (
    STRATEGY_VERSION as _VWAP_BREAKDOWN_VERSION,
)

__all__ = [
    "PairPreset",
    "VWAP_REFERENCE",
    "validate_pair_preset",
    "resolve_pair_preset",
    "PairPresetError",
]

#: The only execution mode the reference pair may be used in.
SHADOW_ONLY: frozenset[str] = frozenset({"shadow"})

#: Exit arbitration policy: the first valid exit candidate wins; no voting.
FIRST_VALID_EXIT = "first_valid_exit"


class PairPresetError(ValueError):
    """A pair preset is unknown, mismatched, invalid, or requested in a bad mode.

    Raised fail-closed for an unknown strategy/version, a BUY/SELL slot side
    mismatch, invalid parameters, a non shadow-only mode set, and any request
    to use the ``vwap_reference`` pair in ``paper`` or ``live``.
    """


@dataclass(frozen=True, slots=True)
class PairPreset:
    """One executable BUY/SELL pair: pinned strategy identities plus policy."""

    preset_id: str
    buy_strategy_id: str
    buy_version: str
    sell_strategy_id: str
    sell_version: str
    buy_params: Mapping[str, object] = field(default_factory=dict)
    sell_params: Mapping[str, object] = field(default_factory=dict)
    trigger_timeframe: CandleTimeframe = CandleTimeframe.M1
    completed_bars_only: bool = True
    allowed_modes: frozenset[str] = SHADOW_ONLY
    requires_trailing_protection: bool = True
    exit_policy: str = FIRST_VALID_EXIT
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.preset_id, str) or not self.preset_id.strip():
            raise PairPresetError("preset_id must be a non-empty str")
        for name in ("buy_strategy_id", "buy_version", "sell_strategy_id", "sell_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PairPresetError(f"{name} must be a non-empty str")
        for name in ("buy_params", "sell_params"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise PairPresetError(f"{name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        if not isinstance(self.trigger_timeframe, CandleTimeframe):
            raise PairPresetError("trigger_timeframe must be a CandleTimeframe")
        if not isinstance(self.completed_bars_only, bool):
            raise PairPresetError("completed_bars_only must be bool")
        modes = self.allowed_modes
        if isinstance(modes, (set, frozenset)):
            modes = frozenset(modes)
            object.__setattr__(self, "allowed_modes", modes)
        if not isinstance(self.allowed_modes, frozenset) or not self.allowed_modes:
            raise PairPresetError("allowed_modes must be a non-empty set")
        for mode in self.allowed_modes:
            if not isinstance(mode, str) or not mode.strip():
                raise PairPresetError("allowed_modes must contain non-empty strings")
        if not isinstance(self.requires_trailing_protection, bool):
            raise PairPresetError("requires_trailing_protection must be bool")
        if not isinstance(self.exit_policy, str) or not self.exit_policy.strip():
            raise PairPresetError("exit_policy must be a non-empty str")
        if not isinstance(self.description, str):
            raise PairPresetError("description must be str")


#: Shadow-only reference pair: vwap_bounce BUY with vwap_breakdown SELL.
VWAP_REFERENCE = PairPreset(
    preset_id="vwap_reference",
    buy_strategy_id=_BUY_VWAP_REFERENCE.strategy_id,
    buy_version=_BUY_VWAP_REFERENCE.version,
    sell_strategy_id=_VWAP_BREAKDOWN_ID,
    sell_version=_VWAP_BREAKDOWN_VERSION,
    buy_params=dict(_BUY_VWAP_REFERENCE.params),
    sell_params={},
    trigger_timeframe=CandleTimeframe.M1,
    completed_bars_only=True,
    allowed_modes=SHADOW_ONLY,
    requires_trailing_protection=True,
    exit_policy=FIRST_VALID_EXIT,
    description=(
        "Shadow-only vwap_reference pair: the independently registered "
        "vwap_bounce BUY leg and vwap_breakdown SELL leg, paired by "
        "(strategy_id, version) through configuration only, on completed "
        "1-minute bars with mandatory trailing protection."
    ),
)

_PAIR_PRESETS: Mapping[str, PairPreset] = MappingProxyType(
    {VWAP_REFERENCE.preset_id: VWAP_REFERENCE}
)


def _build_pair_registry() -> StrategyRegistry:
    """Return a registry with the reference BUY and SELL legs registered."""
    from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy
    from mike_trade.strategies.sell.vwap_breakdown import VwapBreakdownSell

    registry = StrategyRegistry()
    registry.register(VwapBounceBuy)
    registry.register(VwapBreakdownSell)
    return registry


def _normalize_mode(mode: object) -> str:
    if not isinstance(mode, str) or not mode.strip():
        raise PairPresetError(f"mode must be a non-empty str; got {mode!r}")
    return mode.strip().lower()


def validate_pair_preset(
    preset: PairPreset,
    *,
    registry: StrategyRegistry | None = None,
    mode: str = "shadow",
) -> Mapping[str, Mapping[str, object]]:
    """Validate a pair preset against the registry; fail closed.

    Resolves both legs by ``(strategy_id, version)`` through ``registry``
    (unknown strategy or version raises ``PairPresetError``), checks that
    the BUY slot resolved to a BUY strategy and the SELL slot to a SELL
    strategy, validates both parameter dicts via the spec schemas, and
    rejects any mode other than ``shadow`` (including any ``paper`` or
    ``live`` request). Also rejects a preset whose own ``allowed_modes``
    admit anything but ``shadow``. Returns a read-only mapping with the
    validated ``buy`` and ``sell`` parameter mappings.
    """
    if not isinstance(preset, PairPreset):
        raise PairPresetError(f"expected PairPreset; got {type(preset).__name__}")
    active = _build_pair_registry() if registry is None else registry
    if not isinstance(active, StrategyRegistry):
        raise PairPresetError(
            f"registry must be a StrategyRegistry; got {type(active).__name__}"
        )
    requested = _normalize_mode(mode)
    if requested not in SHADOW_ONLY or requested not in preset.allowed_modes:
        raise PairPresetError(
            f"pair preset {preset.preset_id!r} is shadow-only; "
            f"mode {requested!r} is refused"
        )
    if preset.allowed_modes != SHADOW_ONLY:
        raise PairPresetError(
            f"pair preset {preset.preset_id!r} is shadow-only; "
            f"got allowed_modes={sorted(preset.allowed_modes)!r}"
        )
    if preset.trigger_timeframe is not CandleTimeframe.M1:
        raise PairPresetError(
            f"pair preset {preset.preset_id!r} requires the 1m trigger timeframe; "
            f"got {preset.trigger_timeframe!r}"
        )
    if preset.completed_bars_only is not True:
        raise PairPresetError(
            f"pair preset {preset.preset_id!r} requires completed bars only"
        )
    if preset.requires_trailing_protection is not True:
        raise PairPresetError(
            f"pair preset {preset.preset_id!r} requires trailing protection"
        )
    if preset.exit_policy != FIRST_VALID_EXIT:
        raise PairPresetError(
            f"pair preset {preset.preset_id!r} requires exit policy "
            f"{FIRST_VALID_EXIT!r}; got {preset.exit_policy!r}"
        )
    legs = (
        ("buy", preset.buy_strategy_id, preset.buy_version, StrategyRole.BUY,
         preset.buy_params),
        ("sell", preset.sell_strategy_id, preset.sell_version, StrategyRole.SELL,
         preset.sell_params),
    )
    validated: dict[str, Mapping[str, object]] = {}
    for slot, strategy_id, version, expected_side, params in legs:
        try:
            strategy_cls = active.get(strategy_id, version)
        except StrategyRegistryError as exc:
            raise PairPresetError(
                f"pair preset {preset.preset_id!r} {slot} leg pins unknown "
                f"strategy {strategy_id!r} version {version!r}",
            ) from exc
        if (strategy_cls.spec.strategy_id, strategy_cls.spec.version) != (
            strategy_id,
            version,
        ):
            raise PairPresetError(
                f"pair preset {preset.preset_id!r} {slot} leg pins unknown "
                f"strategy {strategy_id!r} version {version!r}",
            )
        if strategy_cls.spec.side is not expected_side:
            raise PairPresetError(
                f"pair preset {preset.preset_id!r} {slot} leg must be "
                f"{expected_side.value}; got {strategy_cls.spec.side.value} "
                f"for {strategy_id!r}",
            )
        if strategy_cls.spec.trigger_timeframe is not CandleTimeframe.M1:
            raise PairPresetError(
                f"pair preset {preset.preset_id!r} {slot} leg "
                f"{strategy_id!r} does not trigger on completed 1m bars",
            )
        if strategy_cls.spec.completed_bars_only is not True:
            raise PairPresetError(
                f"pair preset {preset.preset_id!r} {slot} leg "
                f"{strategy_id!r} does not require completed bars",
            )
        try:
            validated[slot] = active.validate_parameters(
                strategy_id, params, version=version
            )
        except ParameterValidationError as exc:
            raise PairPresetError(
                f"pair preset {preset.preset_id!r} {slot} leg has invalid "
                f"parameters: {exc}",
            ) from exc
    return MappingProxyType(validated)


def resolve_pair_preset(
    preset: PairPreset | str,
    *,
    registry: StrategyRegistry | None = None,
    mode: str = "shadow",
) -> Mapping[str, object]:
    """Resolve a pair preset (or preset id) to registered strategy classes.

    Looks up a ``str`` preset id among the shipped pair presets (unknown ids
    raise ``PairPresetError``), validates via :func:`validate_pair_preset`
    (same fail-closed behaviour), and returns a read-only mapping with the
    preset, both strategy classes, and both validated parameter mappings.
    """
    if isinstance(preset, str):
        try:
            resolved_preset = _PAIR_PRESETS[preset]
        except KeyError:
            raise PairPresetError(
                f"unknown pair preset {preset!r}",
            ) from None
    elif isinstance(preset, PairPreset):
        resolved_preset = preset
    else:
        raise PairPresetError(f"expected PairPreset or str; got {type(preset).__name__}")
    active = _build_pair_registry() if registry is None else registry
    validated = validate_pair_preset(resolved_preset, registry=active, mode=mode)
    buy_cls = active.get(resolved_preset.buy_strategy_id, resolved_preset.buy_version)
    sell_cls = active.get(resolved_preset.sell_strategy_id, resolved_preset.sell_version)
    return MappingProxyType(
        {
            "preset": resolved_preset,
            "buy_cls": buy_cls,
            "sell_cls": sell_cls,
            "buy_params": validated["buy"],
            "sell_params": validated["sell"],
            "exit_policy": resolved_preset.exit_policy,
        }
    )
