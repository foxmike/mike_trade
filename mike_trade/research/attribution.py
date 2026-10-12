"""Deterministic legacy backtest strategy attribution recovery.

Reconstructs BUY/SELL/SHORT/COVER strategies, trade-level entry/exit strategies,
signal identifiers, and exit reasons from immutable legacy backtest artifacts
without guessing or hallucinating timestamps.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.research.strategy_roles import classify_strategies, get_strategy_role


def recover_run_strategies(
    manifest: Mapping[str, Any],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Recover BUY, SELL, SHORT, and COVER strategy lists for a backtest run.

    Checks configuration.strategy_collections first. If absent or empty,
    dynamically classifies manifest.strategies using the strategy registry.

    Returns:
        (buys, sells, shorts, covers)
    """
    config = manifest.get("configuration")
    if isinstance(config, Mapping):
        collections = config.get("strategy_collections")
        if isinstance(collections, Mapping) and collections:
            buys = sorted({
                str(name) for row in collections.values() for name in row.get("buy", [])
            })
            sells = sorted({
                str(name) for row in collections.values() for name in row.get("sell", [])
            })
            shorts = sorted({
                str(name) for row in collections.values() for name in row.get("short", [])
            })
            covers = sorted({
                str(name) for row in collections.values() for name in row.get("cover", [])
            })
            if buys or sells or shorts or covers:
                return (buys, sells, shorts, covers)

    raw_strategies = manifest.get("strategies")
    if isinstance(raw_strategies, (list, tuple)) and raw_strategies:
        return classify_strategies(raw_strategies)

    return ([], [], [], [])


def recover_stop_configuration(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Recover protective and trailing stop configuration from manifest and configuration."""
    stops: dict[str, Any] = {}
    tokens = ("stop", "protect", "trailing")

    config = manifest.get("configuration")
    if isinstance(config, Mapping):
        for key, value in config.items():
            if any(t in str(key).lower() for t in tokens):
                stops[str(key)] = value

    for key, value in manifest.items():
        if key == "configuration":
            continue
        if any(t in str(key).lower() for t in tokens):
            stops[str(key)] = value

    return stops


class ReconciledTradeAttribution:
    """Provenance-preserving attribution container for a historical trade."""

    __slots__ = (
        "entry_strategy_id",
        "entry_strategy_version",
        "entry_signal_id",
        "entry_signal_timestamp",
        "exit_strategy_id",
        "exit_strategy_version",
        "exit_signal_id",
        "exit_signal_timestamp",
        "exit_reason",
        "attribution_complete",
        "attribution_source",
    )

    def __init__(
        self,
        *,
        entry_strategy_id: str | None = None,
        entry_strategy_version: str | None = None,
        entry_signal_id: str | None = None,
        entry_signal_timestamp: str | None = None,
        exit_strategy_id: str | None = None,
        exit_strategy_version: str | None = None,
        exit_signal_id: str | None = None,
        exit_signal_timestamp: str | None = None,
        exit_reason: str | None = None,
        attribution_complete: bool = False,
        attribution_source: str = "UNRESOLVED",
    ) -> None:
        self.entry_strategy_id = entry_strategy_id
        self.entry_strategy_version = entry_strategy_version
        self.entry_signal_id = entry_signal_id
        self.entry_signal_timestamp = entry_signal_timestamp
        self.exit_strategy_id = exit_strategy_id
        self.exit_strategy_version = exit_strategy_version
        self.exit_signal_id = exit_signal_id
        self.exit_signal_timestamp = exit_signal_timestamp
        self.exit_reason = exit_reason
        self.attribution_complete = attribution_complete
        self.attribution_source = attribution_source


def build_signal_indexes(
    result: Mapping[str, Any],
) -> tuple[
    dict[tuple[str, str], list[dict[str, Any]]],
    dict[tuple[str, str], list[dict[str, Any]]],
]:
    """Index signals and intents by (symbol, time) in UTC ISO format."""
    signals_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for sig in result.get("signals", []):
        sym = str(sig.get("symbol", "")).upper()
        time_str = str(sig.get("time", ""))
        if sym and time_str:
            signals_index.setdefault((sym, time_str), []).append(sig)

    intents_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for intent in result.get("intents", []):
        sym = str(intent.get("symbol", "")).upper()
        time_str = str(intent.get("time", ""))
        if sym and time_str:
            intents_index.setdefault((sym, time_str), []).append(intent)

    return (signals_index, intents_index)


def reconcile_trade_attribution(
    cycle: Mapping[str, Any],
    buys: list[Mapping[str, Any]],
    sells: list[Mapping[str, Any]],
    signals_index: Mapping[tuple[str, str], list[dict[str, Any]]],
    intents_index: Mapping[tuple[str, str], list[dict[str, Any]]],
    strategy_versions: Mapping[str, str],
) -> ReconciledTradeAttribution:
    """Reconcile entry and exit strategy attribution for an individual cycle."""
    symbol = str(cycle.get("symbol", "")).upper()
    opened_at = str(cycle.get("opened_at", ""))
    closed_at = str(cycle.get("closed_at", ""))

    entry_fill_time = str(buys[0].get("time", "")) if buys else opened_at
    exit_fill_time = str(sells[-1].get("time", "")) if sells else closed_at

    # 1. Entry strategy resolution
    entry_strategy: str | None = cycle.get("entry_strategy")
    if not entry_strategy and buys:
        entry_strategy = buys[0].get("strategy")

    entry_version: str | None = cycle.get("entry_strategy_version")
    entry_signal_id: str | None = cycle.get("entry_signal_id")
    if not entry_signal_id and buys:
        entry_signal_id = buys[0].get("signal_id")
    entry_signal_timestamp: str | None = cycle.get("entry_signal_timestamp")
    if not entry_signal_timestamp and buys:
        entry_signal_timestamp = buys[0].get("signal_time")

    attribution_source = "EXPLICIT" if entry_strategy else "UNRESOLVED"

    # Recover missing entry attribution from signals/intents
    if not entry_strategy and symbol and entry_fill_time:
        matched_signals = signals_index.get((symbol, entry_fill_time), [])
        matched_intents = intents_index.get((symbol, entry_fill_time), [])

        # Filter candidate signals to entry roles (BUY or SHORT)
        entry_signal_candidates = [
            s for s in matched_signals
            if get_strategy_role(str(s.get("strategy")))
            in (StrategyRole.BUY, StrategyRole.SHORT, None)
        ]

        distinct_strat_names = {
            str(s.get("strategy")) for s in entry_signal_candidates if s.get("strategy")
        }
        if len(distinct_strat_names) == 1:
            chosen_sig = entry_signal_candidates[0]
            entry_strategy = str(chosen_sig["strategy"])
            entry_version = chosen_sig.get("version") or strategy_versions.get(entry_strategy)
            entry_signal_id = chosen_sig.get("signal_id")
            entry_signal_timestamp = chosen_sig.get("time") or entry_fill_time
            attribution_source = "RECOVERED_SIGNALS"
        elif not entry_signal_candidates and matched_intents:
            distinct_intent_names = {
                str(i.get("strategy")) for i in matched_intents if i.get("strategy")
            }
            if len(distinct_intent_names) == 1:
                chosen_intent = matched_intents[0]
                entry_strategy = str(chosen_intent["strategy"])
                entry_version = strategy_versions.get(entry_strategy)
                entry_signal_id = chosen_intent.get("intent_id")
                entry_signal_timestamp = chosen_intent.get("time") or entry_fill_time
                attribution_source = "RECOVERED_SIGNALS"

    if entry_strategy and not entry_version:
        entry_version = strategy_versions.get(entry_strategy)

    # 2. Exit strategy & reason resolution
    exit_strategy: str | None = cycle.get("exit_strategy")
    if not exit_strategy and sells:
        exit_strategy = sells[-1].get("strategy")

    exit_version: str | None = cycle.get("exit_strategy_version")
    exit_signal_id: str | None = cycle.get("exit_signal_id")
    if not exit_signal_id and sells:
        exit_signal_id = sells[-1].get("signal_id")
    exit_signal_timestamp: str | None = cycle.get("exit_signal_timestamp")
    if not exit_signal_timestamp and sells:
        exit_signal_timestamp = sells[-1].get("signal_time")

    exit_reason: str | None = cycle.get("exit_reason")
    if not exit_reason and sells:
        exit_reason = sells[-1].get("exit_reason")

    if exit_reason in ("STRATEGY_SELL", "STRATEGY_COVER"):
        exit_reason = "STRATEGY_EXIT"

    # Recover missing exit attribution from signals/intents
    if not exit_strategy and symbol and exit_fill_time:
        matched_exit_signals = signals_index.get((symbol, exit_fill_time), [])
        matched_exit_intents = intents_index.get((symbol, exit_fill_time), [])

        exit_signal_candidates = [
            s for s in matched_exit_signals
            if get_strategy_role(str(s.get("strategy")))
            in (StrategyRole.SELL, StrategyRole.COVER, None)
        ]

        distinct_exit_strats = {
            str(s.get("strategy")) for s in exit_signal_candidates if s.get("strategy")
        }
        if len(distinct_exit_strats) == 1:
            chosen_exit_sig = exit_signal_candidates[0]
            exit_strategy = str(chosen_exit_sig["strategy"])
            exit_version = chosen_exit_sig.get("version") or strategy_versions.get(exit_strategy)
            exit_signal_id = chosen_exit_sig.get("signal_id")
            exit_signal_timestamp = chosen_exit_sig.get("time") or exit_fill_time
            if not exit_reason:
                exit_reason = "STRATEGY_EXIT"
        elif not exit_signal_candidates and matched_exit_intents:
            distinct_exit_intent_names = {
                str(i.get("strategy")) for i in matched_exit_intents if i.get("strategy")
            }
            if len(distinct_exit_intent_names) == 1:
                chosen_exit_intent = matched_exit_intents[0]
                exit_strategy = str(chosen_exit_intent["strategy"])
                exit_version = strategy_versions.get(exit_strategy)
                exit_signal_id = chosen_exit_intent.get("intent_id")
                exit_signal_timestamp = chosen_exit_intent.get("time") or exit_fill_time
                if not exit_reason:
                    exit_reason = "STRATEGY_EXIT"

    if exit_strategy and not exit_version:
        exit_version = strategy_versions.get(exit_strategy)

    if exit_reason == "PROTECTIVE_STOP":
        exit_strategy = None

    attribution_complete = bool(entry_strategy and entry_signal_id)

    return ReconciledTradeAttribution(
        entry_strategy_id=entry_strategy,
        entry_strategy_version=entry_version,
        entry_signal_id=entry_signal_id,
        entry_signal_timestamp=entry_signal_timestamp,
        exit_strategy_id=exit_strategy,
        exit_strategy_version=exit_version,
        exit_signal_id=exit_signal_id,
        exit_signal_timestamp=exit_signal_timestamp,
        exit_reason=exit_reason,
        attribution_complete=attribution_complete,
        attribution_source=attribution_source,
    )
