"""Strategy role classification and registry reflection for the 192 trading strategies."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from functools import lru_cache

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.base import (
    BuyStrategy,
    CoverStrategy,
    SellStrategy,
    ShortStrategy,
)

_PACKAGE_CONFIG = (
    ("mike_trade.strategies.buy", BuyStrategy, StrategyRole.BUY),
    ("mike_trade.strategies.sell", SellStrategy, StrategyRole.SELL),
    ("mike_trade.strategies.short", ShortStrategy, StrategyRole.SHORT),
    ("mike_trade.strategies.cover", CoverStrategy, StrategyRole.COVER),
)


@lru_cache(maxsize=1)
def get_strategy_roles() -> dict[str, StrategyRole]:
    """Inspect all strategy packages and return a map of strategy_id -> StrategyRole."""
    roles: dict[str, StrategyRole] = {}
    for pkg_name, base_cls, role in _PACKAGE_CONFIG:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
            try:
                mod = importlib.import_module(f"{pkg_name}.{modname}")
            except Exception:
                continue
            for attr in dir(mod):
                val = getattr(mod, attr)
                if (
                    isinstance(val, type)
                    and issubclass(val, base_cls)
                    and val is not base_cls
                    and hasattr(val, "spec")
                ):
                    roles[val.spec.strategy_id] = role
    return roles


def get_strategy_role(strategy_name: str) -> StrategyRole | None:
    """Return the StrategyRole for a given strategy name, or None if unknown."""
    if not strategy_name:
        return None
    return get_strategy_roles().get(str(strategy_name).strip())


def classify_strategies(
    strategies: Iterable[str | dict[str, object]],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Classify an iterable of strategy names or manifest strategy dicts into role lists.

    Returns:
        (buys, sells, shorts, covers) as sorted lists of unique strategy names.
    """
    buys: set[str] = set()
    sells: set[str] = set()
    shorts: set[str] = set()
    covers: set[str] = set()

    roles_map = get_strategy_roles()

    for item in strategies:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("strategy_id") or "").strip()
        else:
            name = str(item).strip()
        if not name:
            continue

        role = roles_map.get(name)
        if role is StrategyRole.BUY:
            buys.add(name)
        elif role is StrategyRole.SELL:
            sells.add(name)
        elif role is StrategyRole.SHORT:
            shorts.add(name)
        elif role is StrategyRole.COVER:
            covers.add(name)
        else:
            # Fallback for naming conventions if not in official 192 registry
            lower = name.lower()
            if "buy" in lower or "bounce" in lower:
                buys.add(name)
            elif "sell" in lower or "breakdown" in lower:
                sells.add(name)
            elif "short" in lower:
                shorts.add(name)
            elif "cover" in lower:
                covers.add(name)

    return (sorted(buys), sorted(sells), sorted(shorts), sorted(covers))
