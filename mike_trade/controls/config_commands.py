"""Read-only operator configuration commands for the ``mike-trade config`` group.

This module implements the non-mutating configuration surface that
``mike_trade.cli`` can wire in later via :func:`register_config_commands`.
Every subcommand works with the engine offline: no database is opened, no
network calls are made, and no broker, market-data, or persistence-writer
modules are imported (only the standard library, PyYAML, and
``mike_trade.domain.errors``).

Subcommands: ``show``, ``validate``, ``fingerprint``, ``sources``,
``providers``. There are deliberately no ``set``/``edit``/``apply``/``diff``/
``history``/``preview`` subcommands: guarded configuration changes go through
``ControlInbox`` ``APPLY_CONFIG_MUTATION`` instead, and ``--set`` overrides
are one-shot, in-memory only, and never written anywhere.

Design choices (3, the task's decision budget):
1. Self-contained d.1/d.2 contract. ``mike_trade.config.models`` and
   ``mike_trade.config.loader`` do not exist in this tree yet, so this module
   implements that contract internally (built-in defaults, precedence,
   strict validation, secret scoping, provider-catalog checks, redaction, and
   fingerprinting) exactly as specified for d.1/d.2. Only the standard
   library, PyYAML, and ``mike_trade.domain.errors`` are imported, so every
   subcommand stays offline and side-effect free.
2. Output envelope. Each subcommand renders one pre-redacted text document.
   ``show``, ``sources`` and ``providers`` lead with a ``#`` header comment
   line carrying ``account_mode``, ``live_trading_enabled`` and the
   fingerprint (JSON consumers skip ``#`` lines), while ``fingerprint``
   prints only the bare hex digest. An ``iex`` feed is annotated with a
   ``feed_note`` key inside ``market_data`` recording its non-consolidated
   IEX provenance.
3. Catalog reuse and secret silence. ``providers`` resolves the provider
   catalog with the same default-and-fallback path rule as ``validate``
   (``config/providers.yaml``, falling back to
   ``config/providers.example.yaml``) and takes no extra flag. Credential
   env names outside the effective mode are reported as ``unread`` and are
   never queried from the environment.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from mike_trade.domain.errors import MikeTradeError

__all__ = ["register_config_commands", "run_config_command"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_SCHEMA_VERSION = 1

SECRET_ENV_VARS: tuple[str, ...] = (
    "MIKE_TRADE_ALPACA_DATA_KEY_ID",
    "MIKE_TRADE_ALPACA_DATA_SECRET_KEY",
    "MIKE_TRADE_ALPACA_PAPER_KEY_ID",
    "MIKE_TRADE_ALPACA_PAPER_SECRET_KEY",
    "MIKE_TRADE_ALPACA_LIVE_KEY_ID",
    "MIKE_TRADE_ALPACA_LIVE_SECRET_KEY",
)

_DATA_SECRETS = (
    "MIKE_TRADE_ALPACA_DATA_KEY_ID",
    "MIKE_TRADE_ALPACA_DATA_SECRET_KEY",
)
_PAPER_SECRETS = (
    "MIKE_TRADE_ALPACA_PAPER_KEY_ID",
    "MIKE_TRADE_ALPACA_PAPER_SECRET_KEY",
)
_LIVE_SECRETS = (
    "MIKE_TRADE_ALPACA_LIVE_KEY_ID",
    "MIKE_TRADE_ALPACA_LIVE_SECRET_KEY",
)

_MODE_SECRETS: dict[str, tuple[str, ...]] = {
    "shadow": _DATA_SECRETS,
    "paper": _DATA_SECRETS + _PAPER_SECRETS,
    "live": _DATA_SECRETS + _LIVE_SECRETS,
}

_SECRET_KEY_TOKENS = ("api_key", "secret", "password", "token", "key_id", "private_key")

TOP_LEVEL_KEYS: tuple[str, ...] = (
    "schema_version",
    "service",
    "market_data",
    "research",
    "scheduler",
    "execution",
    "risk",
    "symbols",
    "strategies",
    "shadow_scenarios",
    "reporting",
)

_SECTION_KEYS: tuple[str, ...] = tuple(k for k in TOP_LEVEL_KEYS if k != "schema_version")

_ENV_DATA_DIR = "MIKE_TRADE_DATA_DIR"
_ENV_DASHBOARD_BIND = "MIKE_TRADE_DASHBOARD_BIND"
_ENV_ACCOUNT_MODE = "MIKE_TRADE_ACCOUNT_MODE"
_ENV_CONFIG = "MIKE_TRADE_CONFIG"
_ENV_MODE_CONFIG = "MIKE_TRADE_MODE_CONFIG"

_DEFAULT_CONFIG_PATH = "config/mike_trade.yaml"
_DEFAULT_PROVIDERS_PATH = "config/providers.yaml"
_FALLBACK_PROVIDERS_PATH = "config/providers.example.yaml"

_TICKER_RE = re.compile(r"^[A-Z][A-Z.]{0,9}$")
_STRATEGY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCENARIO_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HHMM_RE = re.compile(r"^(\d{2}):(\d{2})$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

_DEFAULTS: dict[str, Any] = {
    "service": {
        "timezone": "America/New_York",
        "data_dir": "/var/lib/mike_trade",
        "engine_heartbeat_interval_seconds": 5,
        "dashboard": {
            "bind_host": "127.0.0.1",
            "port": 8765,
            "heartbeat_stale_seconds": 15,
            "heartbeat_offline_seconds": 60,
        },
    },
    "market_data": {
        "provider": "alpaca",
        "feed": "iex",
        "mode": "stream",
        "max_bar_age_seconds": 90,
        "warmup_bars": 60,
        "timeframes": ["1Min"],
    },
    "research": {"providers": []},
    "scheduler": {
        "cadence_seconds": 5,
        "max_workers": 4,
        "queue_max": 100,
        "evaluation_timeout_seconds": 10,
    },
    "execution": {
        "broker": "alpaca",
        "account_mode": "shadow",
        "live_trading_enabled": False,
        "fractional_enabled": False,
        "entry_order_type": "market",
        "exit_order_type": "market",
        "regular_hours_only": True,
        "entry_cutoff": "15:30",
        "end_of_day_close": {"enabled": False, "at": "15:55"},
        "stop_required": True,
        "block_new_entries_when_unprotected": True,
        "margin_enabled": False,
        "short_selling_enabled": False,
    },
    "risk": {"max_open_positions": 3, "require_buying_power_check": True},
    "symbols": {},
    "strategies": {},
    "shadow_scenarios": [],
    "reporting": {
        "evidence_dir": "evidence",
        "evidence_retention_days": 90,
        "log_retention_days": 30,
    },
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigError(MikeTradeError):
    """Base class for configuration load/validation failures."""


class ConfigValidationError(ConfigError):
    """Strict-schema validation failed; ``errors`` holds (dotted path, message) pairs."""

    def __init__(
        self,
        errors: list[tuple[str, str]] | tuple[tuple[str, str], ...],
        message: str | None = None,
    ) -> None:
        self.errors: tuple[tuple[str, str], ...] = tuple(errors)
        text = message if message is not None else "; ".join(f"{p}: {m}" for p, m in self.errors)
        super().__init__(text, code="config_validation_error")


class SecretInConfigError(ConfigError):
    """A secret-like key was found in YAML; the message names the path, never the value."""


class UnsupportedConfigVersionError(ConfigError):
    """The config schema_version is missing or not the supported version."""


class LiveEnableViaOverrideError(ConfigError):
    """Environment or CLI overrides attempted to enable live trading."""


class ProviderCapabilityError(ConfigError):
    """The effective config is incompatible with the provider catalog."""

    def __init__(
        self,
        errors: list[tuple[str, str]] | tuple[tuple[str, str], ...],
        message: str | None = None,
    ) -> None:
        self.errors: tuple[tuple[str, str], ...] = tuple(errors)
        text = message if message is not None else "; ".join(f"{p}: {m}" for p, m in self.errors)
        super().__init__(text, code="provider_capability_error")


# ---------------------------------------------------------------------------
# Strict YAML loading
# ---------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader that rejects duplicate mapping keys."""


def _reject_duplicates(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in seen
        except TypeError:
            duplicate = False
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _reject_duplicates
)


def _read_yaml_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a YAML mapping file; missing files raise FileNotFoundError."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        doc = yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(
            [(str(path), f"invalid YAML: {exc}")],
        ) from None
    if doc is None:
        doc = {}
    if not isinstance(doc, Mapping):
        raise ConfigValidationError([(str(path), "top-level YAML mapping required")])
    return dict(doc)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge mappings; lists and scalars from ``override`` replace."""
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------

_ErrorList = list


def _err(errors: _ErrorList[tuple[str, str]], path: str, message: str) -> None:
    errors.append((path, message))


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_unknown(
    value: Mapping[str, Any], allowed: set[str] | frozenset[str], path: str, errors: _ErrorList
) -> None:
    for key in value:
        if not isinstance(key, str):
            _err(errors, path or "config", f"invalid key {key!r}: keys must be strings")
        elif key not in allowed:
            _err(errors, _join(path, key), f"unknown key {key!r}")


def _req_mapping(value: Any, path: str, errors: _ErrorList, what: str) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        _err(errors, path, f"{what} must be a mapping")
        return None
    return dict(value)


def _req_text(value: Any, path: str, errors: _ErrorList, what: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        _err(errors, path, f"{what} must be a non-empty string")
        return None
    return value


def _req_bool(value: Any, path: str, errors: _ErrorList, what: str) -> bool | None:
    if not isinstance(value, bool):
        _err(errors, path, f"{what} must be true or false")
        return None
    return value


def _req_int(
    value: Any,
    path: str,
    errors: _ErrorList,
    what: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if not _is_int(value):
        _err(errors, path, f"{what} must be an integer")
        return None
    if minimum is not None and value < minimum:
        _err(errors, path, f"{what} must be >= {minimum}")
        return None
    if maximum is not None and value > maximum:
        _err(errors, path, f"{what} must be <= {maximum}")
        return None
    return value


def _req_number(
    value: Any, path: str, errors: _ErrorList, what: str, *, minimum: float | None = None
) -> int | float | None:
    if not _is_number(value):
        _err(errors, path, f"{what} must be a number")
        return None
    if minimum is not None and value < minimum:
        _err(errors, path, f"{what} must be > 0")
        return None
    return value


def _coerce_decimal(value: Any, path: str, errors: _ErrorList, what: str) -> Decimal | None:
    """Money/percent rule: quoted decimal strings or ints only; floats are refused."""
    if isinstance(value, bool):
        _err(errors, path, f"{what} must be a quoted decimal string or integer")
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        _err(
            errors,
            path,
            f"{what} must be a quoted decimal string; quote the value "
            f"(got unquoted {value!r})",
        )
        return None
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            _err(errors, path, f"{what} must be a decimal number (got {value!r})")
            return None
        if not parsed.is_finite():
            _err(errors, path, f"{what} must be finite")
            return None
        return parsed
    _err(errors, path, f"{what} must be a quoted decimal string or integer")
    return None


def _parse_hhmm(value: Any, path: str, errors: _ErrorList, what: str) -> tuple[str, int] | None:
    if not isinstance(value, str):
        _err(errors, path, f"{what} must be 'HH:MM'")
        return None
    match = _HHMM_RE.match(value.strip())
    if match is None:
        _err(errors, path, f"{what} must be 'HH:MM' (got {value!r})")
        return None
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    if int(match.group(1)) > 23 or int(match.group(2)) > 59:
        _err(errors, path, f"{what} must be a valid time (got {value!r})")
        return None
    return value, minutes


# ---------------------------------------------------------------------------
# Section validators (each returns a normalized section or None on type errors)
# ---------------------------------------------------------------------------


def _validate_service(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "service", errors, "service")
    if node is None:
        return None
    _check_unknown(node, {"timezone", "data_dir", "engine_heartbeat_interval_seconds",
                           "dashboard"}, "service", errors)
    timezone = _req_text(node.get("timezone", _DEFAULTS["service"]["timezone"]),
                         "service.timezone", errors, "service.timezone")
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except Exception:
            _err(errors, "service.timezone", f"unknown IANA timezone {timezone!r}")
    data_dir = _req_text(node.get("data_dir", _DEFAULTS["service"]["data_dir"]),
                         "service.data_dir", errors, "service.data_dir")
    heartbeat = _req_number(
        node.get("engine_heartbeat_interval_seconds", 5),
        "service.engine_heartbeat_interval_seconds", errors,
        "service.engine_heartbeat_interval_seconds", minimum=0,
    )
    if heartbeat is not None and heartbeat <= 0:
        _err(errors, "service.engine_heartbeat_interval_seconds",
             "service.engine_heartbeat_interval_seconds must be > 0")
        heartbeat = None
    dash_raw = node.get("dashboard", {})
    dash = _req_mapping(dash_raw, "service.dashboard", errors, "service.dashboard")
    dashboard: dict[str, Any] | None = None
    if dash is not None:
        _check_unknown(dash, {"bind_host", "port", "heartbeat_stale_seconds",
                              "heartbeat_offline_seconds"}, "service.dashboard", errors)
        bind_host = _req_text(dash.get("bind_host", "127.0.0.1"),
                              "service.dashboard.bind_host", errors,
                              "service.dashboard.bind_host")
        port = _req_int(dash.get("port", 8765), "service.dashboard.port",
                        errors, "service.dashboard.port", minimum=1, maximum=65535)
        stale = _req_number(dash.get("heartbeat_stale_seconds", 15),
                            "service.dashboard.heartbeat_stale_seconds", errors,
                            "service.dashboard.heartbeat_stale_seconds", minimum=0)
        offline = _req_number(dash.get("heartbeat_offline_seconds", 60),
                              "service.dashboard.heartbeat_offline_seconds", errors,
                              "service.dashboard.heartbeat_offline_seconds", minimum=0)
        if stale is not None and offline is not None and stale >= offline:
            _err(errors, "service.dashboard.heartbeat_stale_seconds",
                 "heartbeat_stale_seconds must be less than heartbeat_offline_seconds")
        if heartbeat is not None:
            if stale is not None and stale <= heartbeat:
                _err(errors, "service.dashboard.heartbeat_stale_seconds",
                     "heartbeat_stale_seconds must be greater than "
                     "engine_heartbeat_interval_seconds")
            if offline is not None and offline <= heartbeat:
                _err(errors, "service.dashboard.heartbeat_offline_seconds",
                     "heartbeat_offline_seconds must be greater than "
                     "engine_heartbeat_interval_seconds")
        dashboard = {"bind_host": bind_host, "port": port,
                     "heartbeat_stale_seconds": stale,
                     "heartbeat_offline_seconds": offline}
    if timezone is None or data_dir is None or heartbeat is None or dashboard is None:
        return None
    return {"timezone": timezone, "data_dir": data_dir,
            "engine_heartbeat_interval_seconds": heartbeat, "dashboard": dashboard}


def _validate_market_data(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "market_data", errors, "market_data")
    if node is None:
        return None
    _check_unknown(node, {"provider", "feed", "mode", "max_bar_age_seconds",
                          "warmup_bars", "timeframes"}, "market_data", errors)
    provider = _req_text(node.get("provider", "alpaca"), "market_data.provider",
                         errors, "market_data.provider")
    feed = node.get("feed", "iex")
    if feed not in ("iex", "sip"):
        _err(errors, "market_data.feed", f"unknown feed {feed!r}; expected iex or sip")
        feed = None
    mode = node.get("mode", "stream")
    if mode not in ("stream", "poll"):
        _err(errors, "market_data.mode", f"unknown mode {mode!r}; expected stream or poll")
        mode = None
    max_age = _req_number(node.get("max_bar_age_seconds", 90),
                          "market_data.max_bar_age_seconds", errors,
                          "market_data.max_bar_age_seconds", minimum=0)
    warmup = _req_int(node.get("warmup_bars", 60), "market_data.warmup_bars",
                      errors, "market_data.warmup_bars", minimum=2)
    timeframes_raw = node.get("timeframes", ["1Min"])
    timeframes: list[str] | None = None
    if not isinstance(timeframes_raw, list) or not timeframes_raw:
        _err(errors, "market_data.timeframes", "market_data.timeframes must be a non-empty list")
    else:
        allowed = {"1Min", "5Min", "15Min", "1Hour", "1Day"}
        timeframes = []
        for frame in timeframes_raw:
            if frame not in allowed:
                _err(errors, "market_data.timeframes",
                     f"unknown timeframe {frame!r}; expected one of 1Min, 5Min, 15Min, 1Hour, 1Day")
            else:
                timeframes.append(frame)
        if timeframes and "1Min" not in timeframes:
            _err(errors, "market_data.timeframes", "market_data.timeframes must include 1Min")
    if provider is None or feed is None or mode is None or max_age is None \
            or warmup is None or not timeframes:
        return None
    return {"provider": provider, "feed": feed, "mode": mode,
            "max_bar_age_seconds": max_age, "warmup_bars": warmup, "timeframes": timeframes}


def _validate_research(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "research", errors, "research")
    if node is None:
        return None
    _check_unknown(node, {"providers"}, "research", errors)
    providers_raw = node.get("providers", [])
    providers: list[dict[str, Any]] | None = None
    if not isinstance(providers_raw, list):
        _err(errors, "research.providers", "research.providers must be a list")
    else:
        providers = []
        for index, entry in enumerate(providers_raw):
            path = f"research.providers[{index}]"
            item = _req_mapping(entry, path, errors, path)
            if item is None:
                continue
            _check_unknown(item, {"name", "enabled"}, path, errors)
            name = _req_text(item.get("name"), f"{path}.name", errors, f"{path}.name")
            enabled = item.get("enabled", False)
            if _req_bool(enabled, f"{path}.enabled", errors, f"{path}.enabled") is None:
                continue
            if name is None:
                continue
            providers.append({"name": name, "enabled": enabled})
    if providers is None:
        return None
    return {"providers": providers}


def _validate_scheduler(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "scheduler", errors, "scheduler")
    if node is None:
        return None
    _check_unknown(node, {"cadence_seconds", "max_workers", "queue_max",
                          "evaluation_timeout_seconds"}, "scheduler", errors)
    cadence = _req_number(node.get("cadence_seconds", 5), "scheduler.cadence_seconds",
                          errors, "scheduler.cadence_seconds", minimum=0)
    workers = _req_int(node.get("max_workers", 4), "scheduler.max_workers",
                       errors, "scheduler.max_workers", minimum=1, maximum=32)
    queue_max = _req_int(node.get("queue_max", 100), "scheduler.queue_max",
                         errors, "scheduler.queue_max", minimum=1)
    timeout = _req_number(node.get("evaluation_timeout_seconds", 10),
                          "scheduler.evaluation_timeout_seconds", errors,
                          "scheduler.evaluation_timeout_seconds", minimum=0)
    if cadence is None or workers is None or queue_max is None or timeout is None:
        return None
    return {"cadence_seconds": cadence, "max_workers": workers,
            "queue_max": queue_max, "evaluation_timeout_seconds": timeout}


def _validate_execution(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "execution", errors, "execution")
    if node is None:
        return None
    _check_unknown(node, {"broker", "account_mode", "live_trading_enabled",
                          "fractional_enabled", "entry_order_type", "exit_order_type",
                          "regular_hours_only", "entry_cutoff", "end_of_day_close",
                          "stop_required", "block_new_entries_when_unprotected",
                          "margin_enabled", "short_selling_enabled"}, "execution", errors)
    broker = _req_text(node.get("broker", "alpaca"), "execution.broker",
                       errors, "execution.broker")
    account_mode = node.get("account_mode", "shadow")
    if account_mode not in ("shadow", "paper", "live"):
        _err(errors, "execution.account_mode",
             f"unknown account_mode {account_mode!r}; expected shadow, paper, or live")
        account_mode = None
    live_enabled = _req_bool(node.get("live_trading_enabled", False),
                             "execution.live_trading_enabled", errors,
                             "execution.live_trading_enabled")
    fractional = _req_bool(node.get("fractional_enabled", False),
                           "execution.fractional_enabled", errors,
                           "execution.fractional_enabled")
    entry_type = node.get("entry_order_type", "market")
    if entry_type not in ("market", "limit"):
        _err(errors, "execution.entry_order_type",
             f"unknown entry_order_type {entry_type!r}; expected market or limit")
        entry_type = None
    exit_type = node.get("exit_order_type", "market")
    if exit_type not in ("market", "limit"):
        _err(errors, "execution.exit_order_type",
             f"unknown exit_order_type {exit_type!r}; expected market or limit")
        exit_type = None
    regular_hours = _req_bool(node.get("regular_hours_only", True),
                              "execution.regular_hours_only", errors,
                              "execution.regular_hours_only")
    cutoff = _parse_hhmm(node.get("entry_cutoff", "15:30"), "execution.entry_cutoff",
                         errors, "execution.entry_cutoff")
    if cutoff is not None and not (9 * 60 + 30 < cutoff[1] < 16 * 60):
        _err(errors, "execution.entry_cutoff",
             "execution.entry_cutoff must be strictly between 09:30 and 16:00")
    eod_raw = node.get("end_of_day_close", {})
    eod = _req_mapping(eod_raw, "execution.end_of_day_close", errors,
                       "execution.end_of_day_close")
    eod_norm: dict[str, Any] | None = None
    if eod is not None:
        _check_unknown(eod, {"enabled", "at"}, "execution.end_of_day_close", errors)
        eod_enabled = _req_bool(eod.get("enabled", False),
                                "execution.end_of_day_close.enabled", errors,
                                "execution.end_of_day_close.enabled")
        eod_at = _parse_hhmm(eod.get("at", "15:55"), "execution.end_of_day_close.at",
                             errors, "execution.end_of_day_close.at")
        if eod_enabled is None or eod_at is None:
            eod_norm = None
        else:
            eod_norm = {"enabled": eod_enabled, "at": eod_at[0]}
    stop_required = _req_bool(node.get("stop_required", True), "execution.stop_required",
                              errors, "execution.stop_required")
    block_unprotected = _req_bool(node.get("block_new_entries_when_unprotected", True),
                                  "execution.block_new_entries_when_unprotected", errors,
                                  "execution.block_new_entries_when_unprotected")
    margin = _req_bool(node.get("margin_enabled", False), "execution.margin_enabled",
                       errors, "execution.margin_enabled")
    short = _req_bool(node.get("short_selling_enabled", False),
                      "execution.short_selling_enabled", errors,
                      "execution.short_selling_enabled")
    if broker is None or account_mode is None or live_enabled is None or fractional is None \
            or entry_type is None or exit_type is None or regular_hours is None \
            or cutoff is None or eod_norm is None or stop_required is None \
            or block_unprotected is None or margin is None or short is None:
        return None
    return {"broker": broker, "account_mode": account_mode,
            "live_trading_enabled": live_enabled, "fractional_enabled": fractional,
            "entry_order_type": entry_type, "exit_order_type": exit_type,
            "regular_hours_only": regular_hours, "entry_cutoff": cutoff[0],
            "end_of_day_close": eod_norm, "stop_required": stop_required,
            "block_new_entries_when_unprotected": block_unprotected,
            "margin_enabled": margin, "short_selling_enabled": short}


def _validate_risk(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "risk", errors, "risk")
    if node is None:
        return None
    _check_unknown(node, {"total_application_budget", "max_open_positions",
                          "daily_loss_limit", "require_buying_power_check"},
                   "risk", errors)
    total = None
    if "total_application_budget" not in node:
        _err(errors, "risk.total_application_budget",
             "risk.total_application_budget is required")
    else:
        total = _coerce_decimal(node["total_application_budget"],
                                "risk.total_application_budget", errors,
                                "risk.total_application_budget")
        if total is not None and total <= 0:
            _err(errors, "risk.total_application_budget",
                 "risk.total_application_budget must be > 0")
            total = None
    max_open = _req_int(node.get("max_open_positions", 3), "risk.max_open_positions",
                        errors, "risk.max_open_positions", minimum=1, maximum=10)
    daily = None
    if "daily_loss_limit" not in node:
        _err(errors, "risk.daily_loss_limit", "risk.daily_loss_limit is required")
    else:
        daily = _coerce_decimal(node["daily_loss_limit"], "risk.daily_loss_limit",
                                errors, "risk.daily_loss_limit")
        if daily is not None and daily <= 0:
            _err(errors, "risk.daily_loss_limit", "risk.daily_loss_limit must be > 0")
            daily = None
    buying_power = _req_bool(node.get("require_buying_power_check", True),
                             "risk.require_buying_power_check", errors,
                             "risk.require_buying_power_check")
    if total is None or max_open is None or daily is None or buying_power is None:
        return None
    return {"total_application_budget": total, "max_open_positions": max_open,
            "daily_loss_limit": daily, "require_buying_power_check": buying_power}


def _validate_stop_policy(raw: Any, path: str, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, path, errors, path)
    if node is None:
        return None
    policy_type = node.get("type")
    if policy_type not in ("fixed", "native_trailing", "stepped_trailing"):
        _err(errors, _join(path, "type"),
             f"unknown stop policy type {policy_type!r}; "
             "expected fixed, native_trailing, or stepped_trailing")
        return None

    def percent(key: str) -> Decimal | None:
        if key not in node:
            return None
        value = _coerce_decimal(node[key], _join(path, key), errors, _join(path, key))
        if value is not None and not Decimal(0) < value < Decimal(50):
            _err(errors, _join(path, key), f"{_join(path, key)} must be within 0 and 50")
            return None
        return value

    def amount(key: str) -> Decimal | None:
        if key not in node:
            return None
        value = _coerce_decimal(node[key], _join(path, key), errors, _join(path, key))
        if value is not None and value <= 0:
            _err(errors, _join(path, key), f"{_join(path, key)} must be > 0")
            return None
        return value

    if policy_type == "fixed":
        _check_unknown(node, {"type", "stop_percent", "stop_amount"}, path, errors)
        stop_percent = percent("stop_percent")
        stop_amount = amount("stop_amount")
        has_percent = "stop_percent" in node
        has_amount = "stop_amount" in node
        if has_percent == has_amount:
            _err(errors, path, "fixed stop policy needs exactly one of "
                               "stop_percent or stop_amount")
            return None
        key = "stop_percent" if has_percent else "stop_amount"
        value = stop_percent if has_percent else stop_amount
        if value is None:
            return None
        return {"type": "fixed", key: value}
    if policy_type == "native_trailing":
        _check_unknown(node, {"type", "trail_percent", "trail_amount"}, path, errors)
        trail_percent = percent("trail_percent")
        trail_amount = amount("trail_amount")
        if ("trail_percent" in node) == ("trail_amount" in node):
            _err(errors, path, "native_trailing stop policy needs exactly one of "
                               "trail_percent or trail_amount")
            return None
        if "trail_percent" in node and trail_percent is None:
            return None
        if "trail_amount" in node and trail_amount is None:
            return None
        key = "trail_percent" if "trail_percent" in node else "trail_amount"
        value = trail_percent if "trail_percent" in node else trail_amount
        return {"type": "native_trailing", key: value}
    _check_unknown(node, {"type", "initial_stop_percent", "step_trigger_percent",
                          "step_percent"}, path, errors)
    initial = percent("initial_stop_percent")
    trigger = percent("step_trigger_percent")
    step = percent("step_percent")
    missing = [k for k in ("initial_stop_percent", "step_trigger_percent", "step_percent")
               if k not in node]
    if missing:
        _err(errors, path, f"stepped_trailing stop policy is missing {', '.join(missing)}")
        return None
    if initial is None or trigger is None or step is None:
        return None
    return {"type": "stepped_trailing", "initial_stop_percent": initial,
            "step_trigger_percent": trigger, "step_percent": step}


def _strategy_collection(item: Mapping[str, Any], side: str, path: str,
                         errors: _ErrorList, *, required: bool) -> list[str] | None:
    """Normalize ``<side>_strategy`` / ``<side>_strategies`` into one ordered list.

    A symbol carries any number of strategies per side. ``<side>_strategy`` (one
    name) and ``<side>_strategies`` (a list) are alternative spellings of the
    same collection and may not both appear. The SELL side may be empty because
    protective stops (``stop_policy``) never depend on a SELL strategy.
    """
    single, plural = f"{side}_strategy", f"{side}_strategies"
    if single in item and plural in item:
        _err(errors, _join(path, plural),
             f"{_join(path, single)} and {_join(path, plural)} are alternatives; use one")
        return None
    if plural in item:
        raw = item[plural]
        field = _join(path, plural)
        if not isinstance(raw, list) or any(not isinstance(n, str) or not n.strip()
                                            for n in raw):
            _err(errors, field, f"{field} must be a list of strategy names")
            return None
        names = [n.strip() for n in raw]
    elif single in item:
        name = _req_text(item[single], _join(path, single), errors, _join(path, single))
        if name is None:
            return None
        names = [name]
    else:
        names = []
    if required and not names:
        field = _join(path, plural)
        _err(errors, field, f"{path} needs at least one {side} strategy "
                            f"({single} or {plural})")
        return None
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        _err(errors, _join(path, plural), f"duplicate {side} strategies {duplicates}")
        return None
    return names


def _validate_symbols(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "symbols", errors, "symbols")
    if node is None:
        return None
    if not node:
        _err(errors, "symbols", "at least one symbol is required")
        return None
    if len(node) > 10:
        _err(errors, "symbols", "at most 10 symbols are supported")
        return None
    normalized: dict[str, Any] = {}
    for ticker, entry in node.items():
        path = f"symbols.{ticker}"
        if not isinstance(ticker, str) or not _TICKER_RE.match(ticker):
            _err(errors, path, f"invalid ticker {ticker!r}; "
                               "expected 1-10 uppercase letters or dots")
            continue
        item = _req_mapping(entry, path, errors, path)
        if item is None:
            continue
        _check_unknown(item, {"enabled", "budget", "planned_loss_cap", "min_hold_seconds",
                              "max_entry_cycles_per_session", "consecutive_loss_limit",
                              "consecutive_loss_reset", "consecutive_loss_scope",
                              "post_sale_cooldown_seconds", "buy_strategy", "sell_strategy",
                              "buy_strategies", "sell_strategies",
                              "stop_policy"}, path, errors)
        enabled = _req_bool(item.get("enabled", True), _join(path, "enabled"),
                            errors, _join(path, "enabled"))
        budget = None
        if "budget" not in item:
            _err(errors, _join(path, "budget"), f"{_join(path, 'budget')} is required")
        else:
            budget = _coerce_decimal(item["budget"], _join(path, "budget"),
                                     errors, _join(path, "budget"))
            if budget is not None and budget <= 0:
                _err(errors, _join(path, "budget"), f"{_join(path, 'budget')} must be > 0")
                budget = None
        cap = None
        if item.get("planned_loss_cap") is not None:
            cap = _coerce_decimal(item["planned_loss_cap"], _join(path, "planned_loss_cap"),
                                  errors, _join(path, "planned_loss_cap"))
            if cap is not None and cap <= 0:
                _err(errors, _join(path, "planned_loss_cap"),
                     f"{_join(path, 'planned_loss_cap')} must be > 0")
                cap = None
        min_hold = _req_int(item.get("min_hold_seconds", 0),
                            _join(path, "min_hold_seconds"), errors,
                            _join(path, "min_hold_seconds"), minimum=0)
        max_cycles = _req_int(item.get("max_entry_cycles_per_session", 1),
                              _join(path, "max_entry_cycles_per_session"), errors,
                              _join(path, "max_entry_cycles_per_session"), minimum=1)
        loss_limit = _req_int(item.get("consecutive_loss_limit", 2),
                              _join(path, "consecutive_loss_limit"), errors,
                              _join(path, "consecutive_loss_limit"), minimum=1)
        loss_reset = item.get("consecutive_loss_reset", "session")
        if loss_reset not in ("session", "manual", "win"):
            _err(errors, _join(path, "consecutive_loss_reset"),
                 f"unknown consecutive_loss_reset {loss_reset!r}; "
                 "expected session, manual, or win")
            loss_reset = None
        loss_scope = item.get("consecutive_loss_scope", "symbol")
        if loss_scope not in ("symbol", "symbol_and_buy_strategy"):
            _err(errors, _join(path, "consecutive_loss_scope"),
                 f"unknown consecutive_loss_scope {loss_scope!r}; "
                 "expected symbol or symbol_and_buy_strategy")
            loss_scope = None
        cooldown = _req_int(item.get("post_sale_cooldown_seconds", 0),
                            _join(path, "post_sale_cooldown_seconds"), errors,
                            _join(path, "post_sale_cooldown_seconds"), minimum=0)
        buys = _strategy_collection(item, "buy", path, errors, required=True)
        sells = _strategy_collection(item, "sell", path, errors, required=False)
        policy = None
        if "stop_policy" not in item:
            _err(errors, _join(path, "stop_policy"), f"{_join(path, 'stop_policy')} is required")
        else:
            policy = _validate_stop_policy(item["stop_policy"], _join(path, "stop_policy"),
                                           errors)
        if enabled is None or budget is None or min_hold is None or max_cycles is None \
                or loss_limit is None or loss_reset is None or loss_scope is None \
                or cooldown is None or buys is None or sells is None or policy is None:
            continue
        normalized[ticker] = {"enabled": enabled, "budget": budget,
                              "planned_loss_cap": cap, "min_hold_seconds": min_hold,
                              "max_entry_cycles_per_session": max_cycles,
                              "consecutive_loss_limit": loss_limit,
                              "consecutive_loss_reset": loss_reset,
                              "consecutive_loss_scope": loss_scope,
                              "post_sale_cooldown_seconds": cooldown,
                              "buy_strategies": buys, "sell_strategies": sells,
                              # First configured entry of each side, kept for
                              # single-strategy consumers of the settings.
                              "buy_strategy": buys[0],
                              "sell_strategy": sells[0] if sells else None,
                              "stop_policy": policy}
    return normalized


def _validate_strategies(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "strategies", errors, "strategies")
    if node is None:
        return None
    normalized: dict[str, Any] = {}
    for name, entry in node.items():
        path = f"strategies.{name}"
        if not isinstance(name, str) or not _STRATEGY_RE.match(name):
            _err(errors, path, f"invalid strategy name {name!r}; expected lowercase "
                               "letters, digits, or underscores")
            continue
        item = _req_mapping(entry, path, errors, path)
        if item is None:
            continue
        _check_unknown(item, {"plugin", "side", "pair", "parameters"}, path, errors)
        plugin = _req_text(item.get("plugin"), _join(path, "plugin"),
                           errors, _join(path, "plugin"))
        side = item.get("side")
        if side not in ("buy", "sell"):
            _err(errors, _join(path, "side"),
                 f"unknown side {side!r}; expected buy or sell")
            side = None
        pair = item.get("pair")
        if pair is not None and (not isinstance(pair, str) or not pair.strip()):
            _err(errors, _join(path, "pair"), f"{_join(path, 'pair')} must be a string")
            pair = None
        params_raw = item.get("parameters", {})
        params = _req_mapping(params_raw, _join(path, "parameters"), errors,
                              _join(path, "parameters"))
        if params is not None:
            for key, value in params.items():
                if isinstance(value, (dict, list)) or (
                    isinstance(value, float)
                ):
                    _err(errors, _join(_join(path, "parameters"), str(key)),
                         "strategy parameters must be scalar values")
        if plugin is None or side is None or params is None:
            continue
        normalized[name] = {"plugin": plugin, "side": side, "pair": pair,
                            "parameters": params}
    return normalized


def _validate_scenarios(raw: Any, errors: _ErrorList) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        _err(errors, "shadow_scenarios", "shadow_scenarios must be a list")
        return None
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        path = f"shadow_scenarios[{index}]"
        item = _req_mapping(entry, path, errors, path)
        if item is None:
            continue
        _check_unknown(item, {"name", "starting_cash", "enabled", "symbols"}, path, errors)
        name = _req_text(item.get("name"), _join(path, "name"), errors, _join(path, "name"))
        if name is not None:
            if not _SCENARIO_RE.match(name):
                _err(errors, _join(path, "name"), f"invalid scenario name {name!r}")
                name = None
            elif name in seen:
                _err(errors, _join(path, "name"), f"duplicate scenario name {name!r}")
                name = None
            else:
                seen.add(name)
        cash = None
        if "starting_cash" not in item:
            _err(errors, _join(path, "starting_cash"),
                 f"{_join(path, 'starting_cash')} is required")
        else:
            cash = _coerce_decimal(item["starting_cash"], _join(path, "starting_cash"),
                                   errors, _join(path, "starting_cash"))
            if cash is not None and cash <= 0:
                _err(errors, _join(path, "starting_cash"),
                     f"{_join(path, 'starting_cash')} must be > 0")
                cash = None
        enabled = _req_bool(item.get("enabled", True), _join(path, "enabled"),
                            errors, _join(path, "enabled"))
        symbols_raw = item.get("symbols", [])
        symbols: list[str] | None = None
        if not isinstance(symbols_raw, list) or any(not isinstance(s, str) for s in symbols_raw):
            _err(errors, _join(path, "symbols"), f"{_join(path, 'symbols')} must be "
                                                 "a list of symbol names")
        else:
            symbols = list(symbols_raw)
        if name is None or cash is None or enabled is None or symbols is None:
            continue
        normalized.append({"name": name, "starting_cash": cash, "enabled": enabled,
                           "symbols": symbols})
    return normalized


def _validate_reporting(raw: Any, errors: _ErrorList) -> dict[str, Any] | None:
    node = _req_mapping(raw, "reporting", errors, "reporting")
    if node is None:
        return None
    _check_unknown(node, {"evidence_dir", "evidence_retention_days",
                          "log_retention_days"}, "reporting", errors)
    evidence_dir = _req_text(node.get("evidence_dir", "evidence"),
                             "reporting.evidence_dir", errors, "reporting.evidence_dir")
    evidence_retention = _req_int(node.get("evidence_retention_days", 90),
                                  "reporting.evidence_retention_days", errors,
                                  "reporting.evidence_retention_days", minimum=1)
    log_retention = _req_int(node.get("log_retention_days", 30),
                             "reporting.log_retention_days", errors,
                             "reporting.log_retention_days", minimum=1)
    if evidence_dir is None or evidence_retention is None or log_retention is None:
        return None
    return {"evidence_dir": evidence_dir,
            "evidence_retention_days": evidence_retention,
            "log_retention_days": log_retention}


def _validate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate the merged raw mapping into normalized settings."""
    errors: list[tuple[str, str]] = []
    for key in raw:
        if not isinstance(key, str):
            _err(errors, "config", f"invalid key {key!r}: keys must be strings")
        elif key not in TOP_LEVEL_KEYS:
            _err(errors, key, f"unknown key {key!r}")
    node = dict(raw)
    service = _validate_service(node.get("service", {}), errors)
    market_data = _validate_market_data(node.get("market_data", {}), errors)
    research = _validate_research(node.get("research", {}), errors)
    scheduler = _validate_scheduler(node.get("scheduler", {}), errors)
    execution = _validate_execution(node.get("execution", {}), errors)
    risk = _validate_risk(node.get("risk", {}), errors)
    symbols = _validate_symbols(node.get("symbols", {}), errors)
    strategies = _validate_strategies(node.get("strategies", {}), errors)
    scenarios = _validate_scenarios(node.get("shadow_scenarios", []), errors)
    reporting = _validate_reporting(node.get("reporting", {}), errors)
    if errors:
        raise ConfigValidationError(errors)
    assert service is not None and market_data is not None and research is not None
    assert scheduler is not None and execution is not None and risk is not None
    assert symbols is not None and strategies is not None and scenarios is not None
    assert reporting is not None
    settings = {"schema_version": CONFIG_SCHEMA_VERSION, "service": service,
                "market_data": market_data, "research": research, "scheduler": scheduler,
                "execution": execution, "risk": risk, "symbols": symbols,
                "strategies": strategies, "shadow_scenarios": scenarios,
                "reporting": reporting}
    _check_cross_field(settings, errors)
    if errors:
        raise ConfigValidationError(errors)
    return settings


def _cutoff_minutes(value: str) -> int | None:
    match = _HHMM_RE.match(value.strip())
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _check_cross_field(settings: dict[str, Any], errors: _ErrorList) -> None:
    execution = settings["execution"]
    risk = settings["risk"]
    symbols = settings["symbols"]
    strategies = settings["strategies"]
    total = risk["total_application_budget"]
    for ticker, symbol in symbols.items():
        if symbol["budget"] > total:
            _err(errors, f"symbols.{ticker}.budget",
                 f"symbols.{ticker}.budget exceeds risk.total_application_budget")
        cap = symbol["planned_loss_cap"]
        if cap is not None and cap >= symbol["budget"]:
            _err(errors, f"symbols.{ticker}.planned_loss_cap",
                 f"symbols.{ticker}.planned_loss_cap must be less than "
                 f"symbols.{ticker}.budget")
        plugins: dict[str, str] = {}
        for side in ("buy", "sell"):
            for name in symbol[f"{side}_strategies"]:
                entry = strategies.get(name)
                if entry is None or entry["side"] != side:
                    _err(errors, f"symbols.{ticker}.{side}_strategies",
                         f"symbols.{ticker}.{side}_strategy {name!r} does not reference "
                         f"a {side} strategy")
                    continue
                # Signal identity, persisted state and arbitration are keyed by
                # plugin id, so one plugin may serve a symbol only once.
                other = plugins.setdefault(entry["plugin"], name)
                if other != name:
                    _err(errors, f"symbols.{ticker}.{side}_strategies",
                         f"symbols.{ticker} configures plugin {entry['plugin']!r} twice "
                         f"({other!r}, {name!r})")
    if risk["daily_loss_limit"] > total:
        _err(errors, "risk.daily_loss_limit",
             "risk.daily_loss_limit exceeds risk.total_application_budget")
    mode = execution["account_mode"]
    if mode == "live":
        if not execution["live_trading_enabled"]:
            _err(errors, "execution.live_trading_enabled",
                 "live account_mode requires live_trading_enabled: true in a config file")
        if not execution["stop_required"]:
            _err(errors, "execution.stop_required",
                 "live account_mode requires stop_required: true")
        if not execution["block_new_entries_when_unprotected"]:
            _err(errors, "execution.block_new_entries_when_unprotected",
                 "live account_mode requires block_new_entries_when_unprotected: true")
    elif execution["live_trading_enabled"]:
        _err(errors, "execution.live_trading_enabled",
             "live_trading_enabled may only be true with account_mode: live")
    if execution["short_selling_enabled"]:
        _err(errors, "execution.short_selling_enabled",
             "short selling is not supported; the product is long-only")
    if not execution["regular_hours_only"]:
        _err(errors, "execution.regular_hours_only",
             "only regular-hours trading is supported")
    cutoff = _cutoff_minutes(execution["entry_cutoff"])
    eod_at = _cutoff_minutes(execution["end_of_day_close"]["at"])
    if cutoff is not None and eod_at is not None:
        if not eod_at > cutoff:
            _err(errors, "execution.end_of_day_close.at",
                 "execution.end_of_day_close.at must be after execution.entry_cutoff")
        if not eod_at < 16 * 60:
            _err(errors, "execution.end_of_day_close.at",
                 "execution.end_of_day_close.at must be before 16:00")
    for index, scenario in enumerate(settings["shadow_scenarios"]):
        for name in scenario["symbols"]:
            if name not in symbols:
                _err(errors, f"shadow_scenarios[{index}].symbols",
                     f"scenario {scenario['name']!r} lists unconfigured symbol {name!r}")
    if mode == "shadow" and not any(s["enabled"] for s in settings["shadow_scenarios"]):
        _err(errors, "shadow_scenarios",
             "shadow account_mode requires at least one enabled scenario")


# ---------------------------------------------------------------------------
# Secrets: scanning YAML and resolving mode-relevant env names
# ---------------------------------------------------------------------------


def _scan_secrets(node: Any, path: str) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = _join(path, str(key))
            if isinstance(key, str) and any(t in key.lower() for t in _SECRET_KEY_TOKENS):
                raise SecretInConfigError(
                    f"secret-like key {key!r} is not allowed in configuration; "
                    "credentials live only in environment variables",
                    code="secret_in_config",
                    context={"path": child},
                )
            _scan_secrets(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_secrets(value, f"{path}[{index}]")


def _resolve_secrets(mode: str, environ: Mapping[str, str]) -> dict[str, str | None]:
    """Resolve only the credential names relevant to the effective mode."""
    resolved: dict[str, str | None] = {}
    for name in _MODE_SECRETS[mode]:
        value = environ.get(name)
        resolved[name] = value if value else None
    return resolved


# ---------------------------------------------------------------------------
# Layered loading: defaults -> main -> mode -> env -> CLI overrides
# ---------------------------------------------------------------------------


def _check_schema_version(doc: Mapping[str, Any], what: str) -> None:
    if "schema_version" not in doc:
        raise UnsupportedConfigVersionError(
            f"{what} is missing schema_version; expected {CONFIG_SCHEMA_VERSION}",
            code="unsupported_config_version",
            context={"path": "schema_version"},
        )
    version = doc["schema_version"]
    if isinstance(version, bool) or version != CONFIG_SCHEMA_VERSION:
        raise UnsupportedConfigVersionError(
            f"unsupported schema_version {version!r} in {what}; "
            f"expected {CONFIG_SCHEMA_VERSION}",
            code="unsupported_config_version",
            context={"path": "schema_version"},
        )


def _apply_env(raw: dict[str, Any], environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """Apply the allowlisted runtime environment; returns (section, var name) pairs."""
    contributed: list[tuple[str, str]] = []
    data_dir = environ.get(_ENV_DATA_DIR)
    if data_dir:
        raw.setdefault("service", {})["data_dir"] = data_dir
        contributed.append(("service", _ENV_DATA_DIR))
    bind = environ.get(_ENV_DASHBOARD_BIND)
    if bind:
        host, sep, port_text = bind.rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            port = -1
        if not sep or not host.strip() or not 1 <= port <= 65535:
            raise ConfigValidationError(
                [("service.dashboard.bind_host",
                  f"{_ENV_DASHBOARD_BIND} must be 'host:port' (got {bind!r})")]
            )
        dashboard = raw.setdefault("service", {}).setdefault("dashboard", {})
        dashboard["bind_host"] = host.strip()
        dashboard["port"] = port
        contributed.append(("service", _ENV_DASHBOARD_BIND))
    account_mode = environ.get(_ENV_ACCOUNT_MODE)
    if account_mode:
        lowered = account_mode.strip().lower()
        if lowered == "live":
            raise LiveEnableViaOverrideError(
                f"live override refused: {_ENV_ACCOUNT_MODE}=live must be declared "
                "in a config file, not via the environment",
                code="live_enable_via_override",
                context={"path": "execution.account_mode"},
            )
        if lowered not in ("shadow", "paper"):
            raise ConfigValidationError(
                [("execution.account_mode",
                  f"unknown account_mode {account_mode!r}; expected shadow or paper "
                  "via the environment (live must be declared in a config file)")]
            )
        raw.setdefault("execution", {})["account_mode"] = lowered
        contributed.append(("execution", _ENV_ACCOUNT_MODE))
    return contributed


def _is_truthy_override(value: Any) -> bool:
    if value is True:
        return True
    if _is_int(value) and value == 1:
        return True
    return isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes", "on"}


def _apply_cli_overrides(
    raw: dict[str, Any], cli_overrides: tuple[str, ...] | list[str],
) -> list[tuple[str, str]]:
    """Apply one-shot dotted.path=value overrides; returns (section, path) pairs."""
    contributed: list[tuple[str, str]] = []
    for item in cli_overrides:
        name, sep, text = item.partition("=")
        path = name.strip()
        segments = path.split(".")
        if not sep or not path or any(not s for s in segments):
            raise ConfigValidationError(
                [("cli", f"invalid --set override {item!r}; expected dotted.path=value")]
            )
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigValidationError(
                [(path, f"invalid --set value: {exc}")]
            ) from None
        if segments[0] not in _SECTION_KEYS:
            raise ConfigValidationError([(path, f"unknown config path {path!r}")])
        if segments[:2] == ["execution", "account_mode"] and isinstance(parsed, str) \
                and parsed.strip().lower() == "live":
            raise LiveEnableViaOverrideError(
                "live override refused: execution.account_mode=live must be declared "
                "in a config file, not via --set",
                code="live_enable_via_override",
                context={"path": "execution.account_mode"},
            )
        if segments[:2] == ["execution", "live_trading_enabled"] and _is_truthy_override(parsed):
            raise LiveEnableViaOverrideError(
                "live override refused: execution.live_trading_enabled must be declared "
                "in a config file, not via --set",
                code="live_enable_via_override",
                context={"path": "execution.live_trading_enabled"},
            )
        target: Any = raw
        for segment in segments[:-1]:
            if not isinstance(target, Mapping):
                raise ConfigValidationError([(path, f"unknown config path {path!r}")])
            child = target.get(segment)
            if not isinstance(child, Mapping):
                raise ConfigValidationError([(path, f"unknown config path {path!r}")])
            target = child
        target[segments[-1]] = parsed
        contributed.append((segments[0], path))
    return contributed


def _load_effective(
    config_path: str,
    mode_path: str | None,
    environ: Mapping[str, str],
    cli_overrides: tuple[str, ...] | list[str],
) -> tuple[dict[str, Any], dict[str, str | None], dict[str, list[str]]]:
    """Load, merge, and strictly validate; returns (settings, secrets, sources)."""
    main_doc = _read_yaml_file(config_path)
    _check_schema_version(main_doc, f"main config {config_path}")
    _scan_secrets(main_doc, "")
    merged = _deep_merge(_DEFAULTS, main_doc)
    sources: dict[str, list[str]] = {
        section: ["built-in defaults"] for section in _SECTION_KEYS
    }
    for section in _SECTION_KEYS:
        if section in main_doc:
            sources[section].append(str(config_path))
    if mode_path:
        mode_doc = _read_yaml_file(mode_path)
        _check_schema_version(mode_doc, f"mode config {mode_path}")
        _scan_secrets(mode_doc, "")
        merged = _deep_merge(merged, mode_doc)
        for section in _SECTION_KEYS:
            if section in mode_doc:
                sources[section].append(str(mode_path))
    for section, var_name in _apply_env(merged, environ):
        sources[section].append(var_name)
    for section, dotted in _apply_cli_overrides(merged, cli_overrides):
        sources[section].append(f"cli --set {dotted}")
    settings = _validate_config(merged)
    secrets = _resolve_secrets(settings["execution"]["account_mode"], environ)
    return settings, secrets, sources


def load_effective_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    mode_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    cli_overrides: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Load validated effective configuration without returning secret values."""
    source = os.environ if environ is None else environ
    resolved_config = str(config_path or source.get(_ENV_CONFIG) or _DEFAULT_CONFIG_PATH)
    resolved_mode = mode_path or source.get(_ENV_MODE_CONFIG) or None
    settings, _secrets, _sources = _load_effective(
        resolved_config,
        str(resolved_mode) if resolved_mode else None,
        source,
        list(cli_overrides),
    )
    return settings


# ---------------------------------------------------------------------------
# Audit helpers: trade-relevant config, fingerprint, redacted effective config
# ---------------------------------------------------------------------------


def _displayable(value: Any) -> Any:
    """Convert settings to plain output values; Decimals print as strings."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _displayable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_displayable(v) for v in value]
    return value


def _normalized_str(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _normalized_str(value)
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def trade_relevant_config(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Return the trade-relevant subset of settings (no secrets, no bind info)."""
    market_data = settings["market_data"]
    return {
        "execution": copy.deepcopy(dict(settings["execution"])),
        "risk": copy.deepcopy(dict(settings["risk"])),
        "symbols": copy.deepcopy(dict(settings["symbols"])),
        "strategies": copy.deepcopy(dict(settings["strategies"])),
        "shadow_scenarios": copy.deepcopy(list(settings["shadow_scenarios"])),
        "market_data": {
            "provider": market_data["provider"],
            "feed": market_data["feed"],
            "mode": market_data["mode"],
            "max_bar_age_seconds": market_data["max_bar_age_seconds"],
            "warmup_bars": market_data["warmup_bars"],
            "timeframes": list(market_data["timeframes"]),
        },
    }


def compute_config_fingerprint(settings: Mapping[str, Any]) -> str:
    """Lowercase sha256 hex over canonical JSON of the trade-relevant config."""
    canonical_json = json.dumps(
        _canonical(trade_relevant_config(settings)),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def redacted_effective_config(
    settings: Mapping[str, Any], secrets: Mapping[str, str | None]
) -> dict[str, Any]:
    """Full effective config as plain values plus a set/unset secrets section."""
    mode = settings["execution"]["account_mode"]  # type: ignore[index]
    redacted = _displayable(dict(settings))
    redacted["secrets"] = {
        name: ("set" if secrets.get(name) else "unset") for name in _MODE_SECRETS[mode]
    }
    return redacted


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------


def load_provider_catalog(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and shape-validate the provider catalog (env var names only, no values)."""
    doc = _read_yaml_file(path)
    errors: list[tuple[str, str]] = []
    if doc.get("schema_version") != CONFIG_SCHEMA_VERSION or isinstance(
        doc.get("schema_version"), bool
    ):
        _err(errors, "schema_version",
             f"unsupported catalog schema_version {doc.get('schema_version')!r}; "
             f"expected {CONFIG_SCHEMA_VERSION}")
    for key in doc:
        if key not in ("schema_version", "brokers", "market_data", "research"):
            _err(errors, str(key), f"unknown key {key!r}")

    def env_name(value: Any, path: str) -> None:
        if not isinstance(value, str) or not _ENV_NAME_RE.match(value):
            _err(errors, path, f"expected an environment variable name (got {value!r})")

    brokers = doc.get("brokers", {})
    if not isinstance(brokers, Mapping) or not brokers:
        _err(errors, "brokers", "brokers must be a non-empty mapping")
        brokers = {}
    for broker_name, broker in brokers.items():
        bpath = f"brokers.{broker_name}"
        if not isinstance(broker, Mapping):
            _err(errors, bpath, f"{bpath} must be a mapping")
            continue
        for key in broker:
            if key not in ("paper", "live", "capabilities"):
                _err(errors, f"{bpath}.{key}", f"unknown key {key!r}")
        for env in ("paper", "live"):
            entry = broker.get(env)
            epath = f"{bpath}.{env}"
            if not isinstance(entry, Mapping):
                _err(errors, epath, f"{epath} must be a mapping")
                continue
            for key in entry:
                if key not in ("trading_base_url", "key_id_env", "secret_key_env"):
                    _err(errors, f"{epath}.{key}", f"unknown key {key!r}")
            if not isinstance(entry.get("trading_base_url"), str) \
                    or not entry.get("trading_base_url", "").strip():
                _err(errors, f"{epath}.trading_base_url",
                     f"{epath}.trading_base_url must be a non-empty string")
            env_name(entry.get("key_id_env"), f"{epath}.key_id_env")
            env_name(entry.get("secret_key_env"), f"{epath}.secret_key_env")
        caps = broker.get("capabilities")
        cpath = f"{bpath}.capabilities"
        if not isinstance(caps, Mapping):
            _err(errors, cpath, f"{cpath} must be a mapping")
            continue
        for key in caps:
            if key not in ("order_types", "native_trailing_stop", "fractional_order_types",
                           "fractional_native_trailing", "fractional_demonstrated_in_paper",
                           "short_selling_enabled"):
                _err(errors, f"{cpath}.{key}", f"unknown key {key!r}")
        for key in ("order_types", "fractional_order_types"):
            orders = caps.get(key, [])
            if not isinstance(orders, list) or any(not isinstance(o, str) for o in orders):
                _err(errors, f"{cpath}.{key}", f"{cpath}.{key} must be a list of strings")
        for key in ("native_trailing_stop", "fractional_native_trailing",
                    "fractional_demonstrated_in_paper", "short_selling_enabled"):
            if not isinstance(caps.get(key), bool):
                _err(errors, f"{cpath}.{key}", f"{cpath}.{key} must be true or false")
    data = doc.get("market_data", {})
    if not isinstance(data, Mapping) or not data:
        _err(errors, "market_data", "market_data must be a non-empty mapping")
        data = {}
    for provider_name, provider in data.items():
        ppath = f"market_data.{provider_name}"
        if not isinstance(provider, Mapping):
            _err(errors, ppath, f"{ppath} must be a mapping")
            continue
        for key in provider:
            if key not in ("key_id_env", "secret_key_env", "feeds"):
                _err(errors, f"{ppath}.{key}", f"unknown key {key!r}")
        env_name(provider.get("key_id_env"), f"{ppath}.key_id_env")
        env_name(provider.get("secret_key_env"), f"{ppath}.secret_key_env")
        feeds = provider.get("feeds", {})
        if not isinstance(feeds, Mapping) or not feeds:
            _err(errors, f"{ppath}.feeds", f"{ppath}.feeds must be a non-empty mapping")
            continue
        for feed_name, feed in feeds.items():
            fpath = f"{ppath}.feeds.{feed_name}"
            if not isinstance(feed, Mapping):
                _err(errors, fpath, f"{fpath} must be a mapping")
                continue
            for key in feed:
                if key not in ("stream_url", "rest_base_url", "consolidated", "provenance"):
                    _err(errors, f"{fpath}.{key}", f"unknown key {key!r}")
            for key in ("stream_url", "rest_base_url", "provenance"):
                if not isinstance(feed.get(key), str) or not feed.get(key, "").strip():
                    _err(errors, f"{fpath}.{key}", f"{fpath}.{key} must be "
                                                   "a non-empty string")
            if not isinstance(feed.get("consolidated"), bool):
                _err(errors, f"{fpath}.consolidated",
                     f"{fpath}.consolidated must be true or false")
    research = doc.get("research", {})
    if not isinstance(research, Mapping):
        _err(errors, "research", "research must be a mapping")
    if errors:
        raise ConfigValidationError(errors)
    return {"schema_version": CONFIG_SCHEMA_VERSION, "brokers": copy.deepcopy(dict(brokers)),
            "market_data": copy.deepcopy(dict(data)),
            "research": copy.deepcopy(dict(research))}


def validate_against_catalog(
    settings: Mapping[str, Any], catalog: Mapping[str, Any]
) -> None:
    """Raise ProviderCapabilityError when the config exceeds catalog capabilities."""
    errors: list[tuple[str, str]] = []
    execution = settings["execution"]
    market_data = settings["market_data"]
    broker_name = execution["broker"]
    broker = catalog.get("brokers", {}).get(broker_name)
    if broker is None:
        _err(errors, "execution.broker",
             f"broker {broker_name!r} is not in the provider catalog")
        raise ProviderCapabilityError(errors)
    capabilities = broker["capabilities"]
    provider_name = market_data["provider"]
    provider = catalog.get("market_data", {}).get(provider_name)
    if provider is None:
        _err(errors, "market_data.provider",
             f"market-data provider {provider_name!r} is not in the provider catalog")
    elif market_data["feed"] not in provider.get("feeds", {}):
        _err(errors, "market_data.feed",
             f"feed {market_data['feed']!r} is not offered by provider {provider_name!r}")
    order_types = capabilities.get("order_types", [])
    for key in ("entry_order_type", "exit_order_type"):
        if execution[key] not in order_types:
            _err(errors, f"execution.{key}",
                 f"{execution[key]!r} is not in the provider catalog order_types")
    uses_native_trailing = any(
        symbol["stop_policy"]["type"] == "native_trailing"
        for symbol in settings["symbols"].values()
    )
    if uses_native_trailing and not capabilities.get("native_trailing_stop", False):
        _err(errors, "execution",
             "native_trailing stop policies require the native_trailing_stop capability")
    if execution["fractional_enabled"] and uses_native_trailing and not (
        capabilities.get("fractional_native_trailing", False)
        and capabilities.get("fractional_demonstrated_in_paper", False)
    ):
        _err(errors, "execution.fractional_enabled",
             "fractional orders combined with native trailing stops are rejected "
             "until demonstrated in paper (fail closed)")
    if execution["short_selling_enabled"]:
        _err(errors, "execution.short_selling_enabled",
             "short selling is not supported by the provider catalog")
    if errors:
        raise ProviderCapabilityError(errors)


# ---------------------------------------------------------------------------
# Defensive redaction
# ---------------------------------------------------------------------------


def _redact_text(text: str, secret_values: list[str]) -> str:
    """Replace every resolved secret value, then assert none remains."""
    for value in secret_values:
        if value:
            text = text.replace(value, "***")
    for value in secret_values:
        if value:
            assert value not in text, "secret value leaked into command output"
    return text


def _secret_values(secrets: Mapping[str, str | None]) -> list[str]:
    return [v for v in secrets.values() if isinstance(v, str) and v]


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def _header_line(settings: Mapping[str, Any], fingerprint: str) -> str:
    execution = settings["execution"]
    live = "true" if execution["live_trading_enabled"] else "false"
    return (f"# account_mode={execution['account_mode']} "
            f"live_trading_enabled={live} fingerprint={fingerprint}")


def _annotate_feed(body: dict[str, Any]) -> dict[str, Any]:
    """Note the non-consolidated IEX provenance on the market_data section."""
    market_data = body.get("market_data")
    if isinstance(market_data, dict) and market_data.get("feed") == "iex":
        body = dict(body)
        annotated = dict(market_data)
        annotated["feed_note"] = "iex is a non-consolidated single-venue feed (provenance IEX)"
        body["market_data"] = annotated
    return body


def _render(obj: Any, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(_displayable(obj), indent=2, sort_keys=True)
    return yaml.safe_dump(_displayable(obj), sort_keys=True, default_flow_style=False,
                          allow_unicode=True)


# ---------------------------------------------------------------------------
# CLI registration and dispatch
# ---------------------------------------------------------------------------


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="main config file (default: $MIKE_TRADE_CONFIG, "
                             "else config/mike_trade.yaml)")
    parser.add_argument("--mode-config", default=None, metavar="PATH",
                        help="optional mode config file (default: $MIKE_TRADE_MODE_CONFIG)")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="dotted.path=value",
                        help="one-shot override (repeatable; never written to disk)")


def register_config_commands(subparsers: Any) -> None:
    """Add the read-only ``config`` command group to an argparse subparsers action."""
    config_parser = subparsers.add_parser(
        "config", help="read-only configuration inspection (never mutates files)")
    _add_common_options(config_parser)
    actions = config_parser.add_subparsers(dest="config_subcommand",
                                           metavar="show|validate|fingerprint|sources|providers")

    show = actions.add_parser("show", help="print the redacted effective config")
    _add_common_options(show)
    show.add_argument("--format", choices=("yaml", "json"), default="yaml")
    show.add_argument("--section", default=None, metavar="NAME",
                      help="limit output to one top-level section")
    show.add_argument("--trade-relevant", action="store_true",
                      help="print only the trade-relevant config subset")
    show.set_defaults(config_subcommand="show")

    validate = actions.add_parser("validate",
                                  help="validate the config against the provider catalog")
    _add_common_options(validate)
    validate.add_argument("--providers", default=None, metavar="PATH",
                          help="provider catalog file (default: config/providers.yaml, "
                               "falling back to config/providers.example.yaml)")
    validate.set_defaults(config_subcommand="validate")

    fingerprint = actions.add_parser("fingerprint", help="print the config fingerprint")
    _add_common_options(fingerprint)
    fingerprint.set_defaults(config_subcommand="fingerprint")

    sources = actions.add_parser("sources",
                                 help="print which layers contributed each section")
    _add_common_options(sources)
    sources.set_defaults(config_subcommand="sources")

    providers = actions.add_parser("providers",
                                   help="print the redacted provider catalog and verdict")
    _add_common_options(providers)
    providers.set_defaults(config_subcommand="providers")


def _resolve_paths(args: Any, environ: Mapping[str, str]) -> tuple[str, str | None, list[str]]:
    config_path = (
        getattr(args, "config", None)
        or environ.get(_ENV_CONFIG)
        or _DEFAULT_CONFIG_PATH
    )
    mode_path = getattr(args, "mode_config", None) or environ.get(_ENV_MODE_CONFIG) or None
    overrides = list(getattr(args, "overrides", None) or [])
    return str(config_path), (str(mode_path) if mode_path else None), overrides


def _resolve_providers_path(args: Any) -> str:
    explicit = getattr(args, "providers", None)
    if explicit:
        return str(explicit)
    if Path(_DEFAULT_PROVIDERS_PATH).exists():
        return _DEFAULT_PROVIDERS_PATH
    return _FALLBACK_PROVIDERS_PATH


def _config_error_lines(exc: ConfigError) -> list[str]:
    errors = getattr(exc, "errors", None)
    if errors:
        return [f"{path}: {message}" for path, message in errors]
    context = getattr(exc, "context", None) or {}
    path = context.get("path", "config") if isinstance(context, Mapping) else "config"
    message = getattr(exc, "message", None) or str(exc)
    return [f"{path}: {message}"]


def _cmd_show(args: Any, environ: Mapping[str, str]) -> tuple[str, dict[str, str | None]]:
    config_path, mode_path, overrides = _resolve_paths(args, environ)
    settings, secrets, _sources = _load_effective(config_path, mode_path, environ, overrides)
    fingerprint = compute_config_fingerprint(settings)
    if getattr(args, "trade_relevant", False):
        body = _displayable(trade_relevant_config(settings))
    else:
        body = redacted_effective_config(settings, secrets)
    body = _annotate_feed(body)
    section = getattr(args, "section", None)
    if section is not None:
        if section not in body:
            raise ConfigValidationError(
                [("section", f"unknown section {section!r}; expected one of "
                              f"{', '.join(sorted(body))}")]
            )
        body = {section: body[section]}
    fmt = getattr(args, "format", "yaml") or "yaml"
    text = _header_line(settings, fingerprint) + "\n" + _render(body, fmt)
    if not text.endswith("\n"):
        text += "\n"
    return text, secrets


def _cmd_validate(args: Any, environ: Mapping[str, str]) -> tuple[str, dict[str, str | None]]:
    config_path, mode_path, overrides = _resolve_paths(args, environ)
    settings, secrets, _sources = _load_effective(config_path, mode_path, environ, overrides)
    catalog = load_provider_catalog(_resolve_providers_path(args))
    validate_against_catalog(settings, catalog)
    fingerprint = compute_config_fingerprint(settings)
    return f"valid fingerprint={fingerprint}\n", secrets


def _cmd_fingerprint(args: Any, environ: Mapping[str, str]) -> tuple[str, dict[str, str | None]]:
    config_path, mode_path, overrides = _resolve_paths(args, environ)
    settings, secrets, _sources = _load_effective(config_path, mode_path, environ, overrides)
    return compute_config_fingerprint(settings) + "\n", secrets


def _cmd_sources(args: Any, environ: Mapping[str, str]) -> tuple[str, dict[str, str | None]]:
    config_path, mode_path, overrides = _resolve_paths(args, environ)
    settings, secrets, sources = _load_effective(config_path, mode_path, environ, overrides)
    fingerprint = compute_config_fingerprint(settings)
    body = {"precedence": ["built-in defaults", "main config file", "mode config file",
                           "environment", "cli overrides"],
            "config_file": str(config_path),
            "mode_config_file": str(mode_path) if mode_path else None,
            "sections": sources}
    text = _header_line(settings, fingerprint) + "\n" + _render(body, "yaml")
    if not text.endswith("\n"):
        text += "\n"
    return text, secrets


def _cmd_providers(args: Any, environ: Mapping[str, str]) -> tuple[str, dict[str, str | None]]:
    config_path, mode_path, overrides = _resolve_paths(args, environ)
    settings, secrets, _sources = _load_effective(config_path, mode_path, environ, overrides)
    catalog = load_provider_catalog(_resolve_providers_path(args))
    fingerprint = compute_config_fingerprint(settings)
    mode = settings["execution"]["account_mode"]
    relevant = set(_MODE_SECRETS[mode])

    def status(name: str) -> str:
        if name not in relevant:
            return "unread"
        return "set" if environ.get(name) else "unset"

    brokers: dict[str, Any] = {}
    for broker_name, broker in catalog["brokers"].items():
        entry: dict[str, Any] = {}
        for env in ("paper", "live"):
            leg = broker[env]
            entry[env] = {"trading_base_url": leg["trading_base_url"],
                          "key_id_env": leg["key_id_env"],
                          "key_id_status": status(leg["key_id_env"]),
                          "secret_key_env": leg["secret_key_env"],
                          "secret_key_status": status(leg["secret_key_env"])}
        entry["capabilities"] = copy.deepcopy(broker["capabilities"])
        brokers[broker_name] = entry
    data: dict[str, Any] = {}
    for provider_name, provider in catalog["market_data"].items():
        data[provider_name] = {
            "key_id_env": provider["key_id_env"],
            "key_id_status": status(provider["key_id_env"]),
            "secret_key_env": provider["secret_key_env"],
            "secret_key_status": status(provider["secret_key_env"]),
            "feeds": copy.deepcopy(provider["feeds"]),
        }
    try:
        validate_against_catalog(settings, catalog)
        verdict: dict[str, Any] = {"ok": True, "reasons": []}
    except ProviderCapabilityError as exc:
        verdict = {"ok": False,
                   "reasons": [f"{path}: {message}" for path, message in exc.errors]}
    body = {"account_mode": mode, "broker": settings["execution"]["broker"],
            "data_provider": settings["market_data"]["provider"],
            "feed": settings["market_data"]["feed"], "brokers": brokers,
            "market_data": data, "research": copy.deepcopy(catalog["research"]),
            "verdict": verdict}
    text = _header_line(settings, fingerprint) + "\n" + _render(body, "yaml")
    if not text.endswith("\n"):
        text += "\n"
    return text, secrets


_COMMANDS = {
    "show": _cmd_show,
    "validate": _cmd_validate,
    "fingerprint": _cmd_fingerprint,
    "sources": _cmd_sources,
    "providers": _cmd_providers,
}


def run_config_command(
    args: Any,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Dispatch a parsed ``config`` command; returns a process exit code."""
    if environ is None:
        environ = os.environ
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr
    subcommand = getattr(args, "config_subcommand", None)
    if subcommand not in _COMMANDS:
        stderr.write(
            "config: a subcommand is required: "
            "show|validate|fingerprint|sources|providers\n"
        )
        return 2
    try:
        text, secrets = _COMMANDS[subcommand](args, environ)
    except ConfigError as exc:
        lines = _config_error_lines(exc)
        guarded = _redact_text("\n".join(lines) + "\n", _secret_values({}))
        stderr.write(guarded)
        return 2
    except FileNotFoundError as exc:
        missing = str(getattr(exc, "filename", None) or exc)
        stderr.write(f"config: file not found: {missing}\n")
        return 2
    except OSError as exc:
        stderr.write(f"config: cannot read file: {exc}\n")
        return 2
    guarded = _redact_text(text, _secret_values(secrets))
    stdout.write(guarded)
    return 0
