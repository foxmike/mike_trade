"""Read-only dashboard API: typed responses, read service, and GET-only router.

Design choices (within the task's decision budget):
1. The dashboard opens SQLite only through ``ReadStore`` (``mode=ro`` URI,
   ``query_only``, write-denying authorizer) and one ``BEGIN DEFERRED`` snapshot
   per request, so every value in a response comes from one consistent WAL view.
   It never runs migrations, never writes, and never imports broker, market-data
   or research code.  Existing ``projections`` readers are reused; a few extra
   scoped SELECTs cover fields the projections do not expose.
2. ``account_id`` in the URL is the persisted ``account_scope_id``.  Every query
   is scoped by it and unknown ids raise :class:`UnknownAccountError` (HTTP 404).
3. Money, quantity, price and P&L are ``Decimal`` and serialize as JSON strings.
   Timestamps are timezone-aware UTC; ``*_et`` siblings carry exchange-local text.
4. Engine OFFLINE is computed here from heartbeat age against the configured
   threshold using the injected clock (:func:`compute_engine_state`); the
   ``service_health.status`` column written by the engine is never trusted for it.
5. Every dashboard value is a :class:`Sourced` record (``value``, ``as_of``,
   ``fresh``, ``stale_reason``, ``provenance``).  Values that need an authority
   (engine, broker link, data feed) are ``fresh=false`` with a ``stale_reason``
   whenever that authority is OFFLINE, stale or disconnected.  A field with no
   persisted source is ``null`` with ``stale_reason='unavailable'``.
6. Component links are read from ``service_health`` rows named ``engine``,
   ``broker``, ``feed``, ``scheduler`` and ``persistence`` (``details_json`` keys
   are documented at their use sites).  SHADOW/BACKTEST scopes have no broker
   link; their ledger is local, so broker authority follows engine liveness.
7. Operator command text only names commands that exist in the Makefile/CLI at
   this phase (``make doctor``); see :data:`OPERATOR_COMMANDS`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AfterValidator, BaseModel, ConfigDict

from mike_trade.dashboard.trades import (
    ResearchRuns,
    ResearchRunDetail,
    TradeDiagnostics,
    TradeInspection,
    TradeJournalService,
    TradeList,
    TradeQuery,
)
from mike_trade.persistence import projections, schema
from mike_trade.persistence.readstore import ReadStore, ReadStoreUnavailableError
from mike_trade.runtime import ClockPort
from mike_trade.scheduling.calendar import (
    NEW_YORK,
    SessionStatus,
    is_trading_day,
    next_session,
    session_for,
    session_status,
)

__all__ = [
    "DashboardSettings",
    "SymbolSettings",
    "DashboardReadService",
    "UnknownAccountError",
    "DatabaseUnavailableError",
    "compute_engine_state",
    "create_api_router",
    "OPERATOR_COMMANDS",
]

EngineStateName = Literal["RUNNING", "PAUSED", "DEGRADED", "OFFLINE", "RECONCILING"]

# Only commands that exist in the Makefile/CLI at this phase may be shown to the
# operator; update this one table when more recipes land.
OPERATOR_COMMANDS: Mapping[str, str] = {
    "ENGINE_OFFLINE": "make doctor",
    "ENGINE_DEGRADED": "make doctor",
    "ENGINE_ERROR": "make doctor",
    "FEED_DISCONNECTED": "make doctor",
    "FEED_STALE": "make doctor",
    "BROKER_DISCONNECTED": "make doctor",
    "BROKER_STALE": "make doctor",
    "BROKER_RATE_LIMITED": "make doctor",
    "SCHEDULER_LAG": "make doctor",
    "PROTECTION_GAP": "make doctor",
    "RECONCILIATION_DRIFT": "make doctor",
    "BACKUP_UNAVAILABLE": "make doctor",
    "BACKUP_FAILED": "make doctor",
    "BACKUP_STALE": "make doctor",
}

_ZERO = Decimal("0")
_LIVE_PROTECTION = ("PENDING", "ACTIVE", "REPLACING", "UNKNOWN")
_COVERING_PROTECTION = ("PENDING", "ACTIVE", "REPLACING")
_ACCEPTED_OUTCOMES = frozenset({"SELECTED", "ACCEPTED", "APPROVED"})
_BROKER_MODES = frozenset({"LIVE", "PAPER"})
_MAX_WATCHLIST = 10
_ENGINE = "engine"


# --------------------------------------------------------------------------- errors


class UnknownAccountError(LookupError):
    """The account id is not a registered scope in the database (HTTP 404)."""


class DatabaseUnavailableError(RuntimeError):
    """The operational database cannot be opened read-only (HTTP 503)."""


# ------------------------------------------------------------------------- settings


@dataclass(frozen=True, slots=True)
class SymbolSettings:
    """Per-symbol operator limits shown on the watchlist (all optional)."""

    budget: Decimal | None = None
    planned_loss_cap: Decimal | None = None
    min_hold_seconds: int | None = None
    max_entry_cycles_per_session: int | None = None
    consecutive_loss_limit: int | None = None
    consecutive_loss_reset: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardSettings:
    """Dashboard-only settings; defaults mirror ``config/mike_trade.example.yaml``."""

    bind_host: str = "127.0.0.1"
    port: int = 8765
    allow_non_loopback: bool = False
    heartbeat_stale_seconds: int = 15
    heartbeat_offline_seconds: int = 60
    max_bar_age_seconds: int = 90
    snapshot_stale_seconds: int = 300
    scheduler_lag_warn_seconds: int = 30
    entry_cutoff: time = time(15, 30)
    total_application_budget: Decimal | None = None
    symbols: Mapping[str, SymbolSettings] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    data_dir: Path = Path("/var/lib/mike_trade")
    backup_dir: Path | None = None
    backup_status_file: Path | None = None
    research_root: Path = Path("research")
    research_dataset_root: Path = Path("research/data")

    def __post_init__(self) -> None:
        if self.heartbeat_stale_seconds <= 0 or self.heartbeat_offline_seconds <= 0:
            raise ValueError("heartbeat thresholds must be positive")
        if self.heartbeat_offline_seconds < self.heartbeat_stale_seconds:
            raise ValueError("heartbeat_offline_seconds must be >= heartbeat_stale_seconds")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DashboardSettings:
        """Build settings from the allowlisted ``MIKE_TRADE_*`` environment keys."""
        env = os.environ if environ is None else environ
        defaults = cls()
        host, port = defaults.bind_host, defaults.port
        bind = env.get("MIKE_TRADE_DASHBOARD_BIND", "").strip()
        if bind:
            host, _, raw_port = bind.rpartition(":") if ":" in bind else (bind, "", "")
            host = host or bind
            port = int(raw_port) if raw_port else defaults.port
        data_dir = Path(env.get("MIKE_TRADE_DATA_DIR", str(defaults.data_dir)))
        backup = env.get("MIKE_TRADE_BACKUP_DIR")
        backup_status = env.get("MIKE_TRADE_BACKUP_STATUS_FILE")
        return cls(
            bind_host=host,
            port=port,
            allow_non_loopback=env.get("MIKE_TRADE_DASHBOARD_ALLOW_NON_LOOPBACK", "") == "1",
            data_dir=data_dir,
            backup_dir=Path(backup) if backup else None,
            backup_status_file=Path(backup_status) if backup_status else None,
            research_root=Path(env.get("MIKE_TRADE_RESEARCH_ROOT", "research")),
            research_dataset_root=Path(
                env.get("MIKE_TRADE_RESEARCH_DATASET_ROOT", "research/data")
            ),
        )


# --------------------------------------------------------------------------- models


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Sourced[T](_Model):
    """One dashboard value with its as-of time, freshness and provenance."""

    value: T | None = None
    as_of: UtcDatetime | None = None
    fresh: bool
    stale_reason: str | None = None
    provenance: str | None = None


class LiveResponse(_Model):
    status: Literal["ok"]
    server_time: UtcDatetime


class AccountSummary(_Model):
    account_id: str
    mode: str
    scenario: str | None
    label: str


class AccountsResponse(_Model):
    accounts: list[AccountSummary]
    server_time: UtcDatetime


class LinkStatus(_Model):
    connected: bool | None
    status: Literal["CONNECTED", "DISCONNECTED", "UNKNOWN"]
    as_of: UtcDatetime | None
    age_seconds: float | None
    fresh: bool
    stale_reason: str | None
    provenance: str | None


class ExchangeClock(_Model):
    exchange: str
    timezone: str
    now_et: str
    session_date: date | None
    session_status: str
    is_early_close: bool | None
    close_at: UtcDatetime | None
    close_at_et: str | None


class EntryCutoff(_Model):
    at: UtcDatetime | None
    at_et: str | None
    session_date: date | None
    passed_today: bool


class AccountStatus(_Model):
    account_id: str
    mode: str
    scenario: str | None
    engine_state: EngineStateName
    last_heartbeat_at: UtcDatetime | None
    last_heartbeat_at_et: str | None
    heartbeat_age_seconds: float | None
    is_stale: bool
    fresh: bool
    stale_reason: str | None
    broker: LinkStatus
    data: LinkStatus
    market: Literal["OPEN", "CLOSED", "HALTED"]
    exchange_clock: ExchangeClock
    next_entry_cutoff: EntryCutoff
    entries_allowed: bool
    entries_blocked_reasons: list[str]
    server_time: UtcDatetime
    server_time_et: str


class KpisResponse(_Model):
    account_id: str
    mode: str
    engine_state: EngineStateName
    server_time: UtcDatetime
    cash: Sourced[Decimal]
    buying_power: Sourced[Decimal]
    equity: Sourced[Decimal]
    configured_total_budget: Sourced[Decimal]
    reserved_pending_buys: Sourced[Decimal]
    deployed_capital: Sourced[Decimal]
    open_position_count: Sourced[int]
    pending_order_count: Sourced[int]
    daily_realized_pnl: Sourced[Decimal]
    daily_unrealized_pnl: Sourced[Decimal]
    lifetime_realized_pnl: Sourced[Decimal]
    lifetime_pnl: Sourced[Decimal]
    protection_error_count: Sourced[int]
    data_error_count: Sourced[int]
    reconciliation_error_count: Sourced[int]


class EvaluationSummary(_Model):
    evaluated_at: UtcDatetime
    status: str
    reason: str
    strategy_key: str | None
    role: str | None
    bar_time: UtcDatetime | None


class SignalSummary(_Model):
    signal_id: str
    action: str
    reference_price: Decimal | None
    reason: str
    generated_at: UtcDatetime
    disposition: Literal["ACCEPTED", "REJECTED", "UNDECIDED"]
    disposition_reason: str | None


class WatchRow(_Model):
    symbol: str
    configured: bool
    price: Sourced[Decimal]
    bid: Sourced[Decimal]
    ask: Sourced[Decimal]
    data_age_seconds: Sourced[float]
    data_stale: bool
    feed: Sourced[str]
    phase: Sourced[str]
    active_buy_strategies: Sourced[list[str]]
    active_sell_strategies: Sourced[list[str]]
    next_due_trigger: Sourced[datetime]
    last_evaluation: Sourced[EvaluationSummary]
    last_signal: Sourced[SignalSummary]
    confirmed_quantity: Sourced[Decimal]
    cost_basis: Sourced[Decimal]
    purchase_time: Sourced[datetime]
    symbol_budget: Sourced[Decimal]
    commitment: Sourced[Decimal]
    planned_loss: Sourced[Decimal]
    daily_entry_count: Sourced[int]
    daily_entry_cap: Sourced[int]
    loss_streak: Sourced[int]
    lockout_active: Sourced[bool]
    lockout_until: Sourced[datetime]
    loss_reset_mode: Sourced[str]
    cooldown_until: Sourced[datetime]
    next_eligible_at: Sourced[datetime]
    min_hold_remaining_seconds: Sourced[int]
    entry_cutoff: Sourced[datetime]
    protection_mode: Sourced[str]
    protection_status: Sourced[str]
    protection_price: Sourced[Decimal]
    protection_quantity: Sourced[Decimal]
    native_broker_hwm: Sourced[Decimal]
    stepped_anchor: Sourced[Decimal]
    desired_stop: Sourced[Decimal]
    confirmed_stop: Sourced[Decimal]
    last_protection_update: Sourced[datetime]
    realized_pnl_today: Sourced[Decimal]
    unrealized_pnl: Sourced[Decimal]
    lifetime_realized_pnl: Sourced[Decimal]
    lifetime_pnl: Sourced[Decimal]


class WatchlistResponse(_Model):
    account_id: str
    mode: str
    engine_state: EngineStateName
    server_time: UtcDatetime
    symbols: list[WatchRow]


class TimelineEntry(_Model):
    kind: Literal["EVALUATION", "SIGNAL"]
    at: UtcDatetime
    status: str
    reason: str
    strategy_key: str | None = None
    role: str | None = None
    bar_time: UtcDatetime | None = None
    action: str | None = None
    reference_price: Decimal | None = None
    disposition: str | None = None


class EvidenceEntry(_Model):
    signal_id: str
    generated_at: UtcDatetime
    provenance: dict[str, Any]


class FeedProvenance(_Model):
    feed: str | None
    provenance: str | None
    consolidated: bool | None
    last_bar_at: UtcDatetime | None
    recorded_at: UtcDatetime | None


class OrderNode(_Model):
    broker_order_id: str
    client_order_id: str
    parent_broker_order_id: str | None
    purpose: str | None
    side: str
    order_type: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal
    filled_avg_price: Decimal | None
    limit_price: Decimal | None
    stop_price: Decimal | None
    submitted_at: UtcDatetime
    updated_at: UtcDatetime


class CycleOrderTree(_Model):
    cycle_id: str
    state: str
    orders: list[OrderNode]


class ProtectionEvent(_Model):
    at: UtcDatetime
    cycle_id: str
    event_type: str
    previous_stop_price: Decimal | None
    new_stop_price: Decimal | None
    proposed_stop_price: Decimal | None
    confirmed_stop_price: Decimal | None
    payload: dict[str, Any]


class ManagedCycle(_Model):
    cycle_id: str
    state: str
    entry_strategy_key: str | None
    opened_at: UtcDatetime | None
    closed_at: UtcDatetime | None
    holding_seconds: int | None
    realized_pnl: Decimal | None
    entry_reference_price: Decimal | None
    entry_average_price: Decimal | None
    entry_slippage: Decimal | None
    exit_reference_price: Decimal | None
    exit_average_price: Decimal | None
    exit_slippage: Decimal | None
    decision_reasons: list[str]


class SymbolDetailResponse(_Model):
    account_id: str
    mode: str
    symbol: str
    server_time: UtcDatetime
    fresh: bool
    stale_reason: str | None
    timeline: list[TimelineEntry]
    evidence: list[EvidenceEntry]
    feed_provenance: FeedProvenance
    order_tree: list[CycleOrderTree]
    protection_history: list[ProtectionEvent]
    managed_cycles: list[ManagedCycle]


class PositionRow(_Model):
    symbol: str
    cycle_id: str | None
    management: str
    direction: str
    quantity: Decimal
    average_entry_price: Decimal
    market_price: Decimal | None
    market_value: Decimal | None
    unrealized_pl: Decimal | None
    as_of: UtcDatetime
    fresh: bool
    stale_reason: str | None


class Balances(_Model):
    cash: Decimal | None
    equity: Decimal | None
    buying_power: Decimal | None
    portfolio_value: Decimal | None
    source: str | None
    as_of: UtcDatetime | None
    fresh: bool
    stale_reason: str | None


class OrderRow(_Model):
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal
    submitted_at: UtcDatetime
    updated_at: UtcDatetime
    is_open: bool
    fresh: bool
    stale_reason: str | None


class ActivityRow(_Model):
    kind: Literal["BROKER_ACTIVITY", "CASH_EVENT"]
    activity_type: str
    occurred_at: UtcDatetime
    amount: Decimal | None
    description: str | None
    payload: dict[str, Any]


class DriftRow(_Model):
    drift_id: str
    drift_type: str
    entity_ref: str | None
    expected: Any
    actual: Any
    detected_at: UtcDatetime


class ReconciliationDetail(_Model):
    last_run_at: UtcDatetime | None
    last_result: str | None
    unresolved_drift_count: int
    drifts: list[DriftRow]


class DetailResponse(_Model):
    account_id: str
    mode: str
    server_time: UtcDatetime
    balances: Balances
    positions: list[PositionRow]
    external_holdings: list[PositionRow]
    orders: list[OrderRow]
    total_fees: Decimal
    activity: list[ActivityRow]
    reconciliation: ReconciliationDetail


class FeedHealth(_Model):
    link: LinkStatus
    age_seconds: Sourced[float]
    reconnects: Sourced[int]
    subscription_count: Sourced[int]


class BrokerHealth(_Model):
    link: LinkStatus
    rate_limit_errors: Sourced[int]


class PersistenceHealth(_Model):
    database_path: str
    database_id: str | None
    schema_version: int | None
    size_bytes: int | None
    wal_size_bytes: int | None
    last_backup_at: Sourced[datetime]
    last_backup_integrity_ok: bool | None


class ActionableError(_Model):
    code: str
    severity: Literal["critical", "warning", "info"]
    message: str
    command: str


class HealthResponse(_Model):
    account_id: str
    mode: str
    engine_state: EngineStateName
    server_time: UtcDatetime
    feed: FeedHealth
    broker: BrokerHealth
    scheduler_lag_seconds: Sourced[float]
    persistence: PersistenceHealth
    last_reconciliation_at: Sourced[datetime]
    errors: list[ActionableError]


# ---- operations cockpit & activity (read-only projections) ----------------


class StrategyInstanceState(_Model):
    """One configured strategy instance with its recorded engine state."""

    instance_id: str
    role: str
    strategy_key: str
    strategy_version: str | None = None
    enabled: bool
    # Recorded pipeline stage: derived strictly from persisted evidence
    # (strategy_state, strategy_evaluations, signals+decisions, open orders,
    # positions).  NOT a guess from unrelated tables.
    stage: str
    stage_detail: str | None = None
    stage_as_of: UtcDatetime | None = None
    engine_state: str | None = None
    armed: bool | None = None
    rearm_required: bool | None = None
    last_signal_bar: UtcDatetime | None = None


class PipelineCycle(_Model):
    cycle_id: str
    state: str
    direction: str | None = None
    entry_strategy_key: str | None = None
    opened_at: UtcDatetime | None = None


class PipelineOrder(_Model):
    broker_order_id: str
    client_order_id: str | None = None
    purpose: str | None = None
    side: str
    order_type: str | None = None
    status: str
    quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    submitted_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
    is_open: bool


class PipelineProtection(_Model):
    protection_order_id: str
    cycle_id: str
    kind: str | None = None
    status: str
    stop_price: Decimal | None = None
    high_water_mark: Decimal | None = None
    covered_quantity: Decimal | None = None
    remaining_quantity: Decimal | None = None
    coverage_gap: bool
    updated_at: UtcDatetime | None = None


class SymbolPipeline(_Model):
    """Per-symbol trading pipeline state from the persisted engine tables."""

    symbol: str
    configured: bool
    phase: str
    direction: str | None = None
    buy_strategies: list[str]
    sell_strategies: list[str]
    short_strategies: list[str]
    cover_strategies: list[str]
    instances: list[StrategyInstanceState]
    last_evaluation: EvaluationSummary | None = None
    last_signal: SignalSummary | None = None
    cycle: PipelineCycle | None = None
    position_quantity: Decimal | None = None
    position_entry_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    pending_orders: list[PipelineOrder]
    pending_entry_orders: int
    pending_exit_orders: int
    reserved_budget: Decimal | None = None
    cooldown_until: UtcDatetime | None = None
    lockout_until: UtcDatetime | None = None
    block_reasons: list[str]
    protections: list[PipelineProtection]
    last_activity_at: UtcDatetime | None = None


class CockpitResponse(_Model):
    account_id: str
    mode: str
    engine_state: EngineStateName
    server_time: UtcDatetime
    entries_allowed: bool
    entries_blocked_reasons: list[str]
    symbols: list[SymbolPipeline]


class ActivityEntry(_Model):
    """One timeline event from the recorded operational history."""

    at: UtcDatetime
    category: Literal[
        "SYSTEM", "STRATEGY", "EVALUATION", "SIGNAL", "ORDER", "FILL",
        "PROTECTION", "LIFECYCLE",
    ]
    event_type: str
    symbol: str | None = None
    strategy_key: str | None = None
    strategy_role: str | None = None
    cycle_id: str | None = None
    ref_id: str | None = None
    side: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    stop_price: Decimal | None = None
    disposition: str | None = None
    summary: str
    payload: dict[str, Any] = {}


class ActivityResponse(_Model):
    account_id: str
    mode: str
    engine_state: EngineStateName
    server_time: UtcDatetime
    entries: list[ActivityEntry]
    next_before: UtcDatetime | None = None


# ------------------------------------------------------------------ pure helpers


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(UTC)


def _et(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(NEW_YORK).isoformat()


def _seconds(delta: timedelta | None) -> float | None:
    return None if delta is None else round(delta.total_seconds(), 3)


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _int(value: object) -> int | None:
    try:
        return None if value is None else int(str(value))
    except ValueError:
        return None


def _json_dict(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dt(text: str | None) -> datetime | None:
    return None if text is None else schema.from_db_timestamp(text)


def _iso(text: object) -> datetime | None:
    """Parse an ISO-8601 timestamp stored inside a JSON payload (schema-extracted)."""
    if text is None or not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _req(text: str) -> datetime:
    return schema.from_db_timestamp(text)


def compute_engine_state(
    *,
    heartbeat_at: datetime | None,
    now: datetime,
    offline_after: timedelta,
    reported_state: str | None = None,
    paused: bool = False,
    feed_stale: bool = False,
) -> EngineStateName:
    """Derive the engine state from heartbeat age (OFFLINE is never read from a flag).

    OFFLINE when no heartbeat exists or its age is strictly greater than
    ``offline_after``, or when a fresh heartbeat says the engine STOPPED.
    Otherwise RECONCILING > DEGRADED (reported degraded/draining, or the data
    feed is stale) > PAUSED > RUNNING.
    """
    current = _utc(now)
    if heartbeat_at is None or current - _utc(heartbeat_at) > offline_after:
        return "OFFLINE"
    reported = (reported_state or "").upper()
    if reported == "STOPPED":
        return "OFFLINE"
    if reported in ("STARTING", "RECONCILING"):
        return "RECONCILING"
    if reported in ("DEGRADED", "DRAINING") or feed_stale:
        return "DEGRADED"
    if paused or reported == "PAUSED":
        return "PAUSED"
    return "RUNNING"


@dataclass(frozen=True, slots=True)
class _Link:
    connected: bool | None
    as_of: datetime | None
    age: timedelta | None
    fresh: bool
    reason: str | None
    provenance: str | None
    details: Mapping[str, Any]

    def model(self) -> LinkStatus:
        status: Literal["CONNECTED", "DISCONNECTED", "UNKNOWN"] = (
            "UNKNOWN" if self.connected is None
            else "CONNECTED" if self.connected else "DISCONNECTED"
        )
        return LinkStatus(
            connected=self.connected, status=status, as_of=self.as_of,
            age_seconds=_seconds(self.age), fresh=self.fresh,
            stale_reason=self.reason, provenance=self.provenance,
        )


@dataclass(frozen=True, slots=True)
class _Trust:
    """Which authorities are usable right now; yields the stale reason for a value."""

    engine_offline: bool
    broker_reason: str | None
    feed_reason: str | None

    def reason(self, *needs: str) -> str | None:
        if self.engine_offline:
            return "engine_offline"
        if "broker" in needs and self.broker_reason:
            return self.broker_reason
        if "feed" in needs and self.feed_reason:
            return self.feed_reason
        return None


@dataclass(frozen=True, slots=True)
class _Facts:
    """Everything the endpoints share, computed once per request snapshot."""

    now: datetime
    heartbeat_at: datetime | None
    heartbeat_age: timedelta | None
    state: EngineStateName
    broker: _Link
    feed: _Link
    market: Literal["OPEN", "CLOSED", "HALTED"]
    session: SessionStatus
    cutoff_today: datetime | None
    cutoff_next: datetime | None
    cutoff_session: date | None
    reasons: tuple[str, ...]
    trust: _Trust
    engine_details: Mapping[str, Any]
    paused: bool
    kill_switch: bool


@dataclass(frozen=True, slots=True)
class _Ctx:
    conn: Any
    scope: str
    mode: str
    scenario: str | None
    now: datetime


class _Values:
    """Builds :class:`Sourced` records against one request's trust state."""

    def __init__(self, trust: _Trust) -> None:
        self._trust = trust

    def of(self, value: Any, *needs: str, as_of: datetime | None = None,
           provenance: str | None = None) -> Sourced[Any]:
        reason = self._trust.reason(*needs)
        return Sourced(value=value, as_of=as_of, fresh=reason is None,
                       stale_reason=reason, provenance=provenance)

    def static(self, value: Any, provenance: str) -> Sourced[Any]:
        return Sourced(value=value, fresh=True, provenance=provenance)

    @staticmethod
    def unavailable(provenance: str | None = None) -> Sourced[Any]:
        return Sourced(value=None, fresh=False, stale_reason="unavailable", provenance=provenance)


# ------------------------------------------------------------------ read service


class DashboardReadService:
    """Read-only view over the operational database, scoped per account."""

    def __init__(self, settings: DashboardSettings, clock: ClockPort,
                 db_path: str | Path) -> None:
        self._settings = settings
        self._clock = clock
        self._db_path = Path(db_path)
        self._trade_journal = TradeJournalService(
            self._db_path,
            settings.research_root,
            settings.research_dataset_root,
            now=self.server_time,
        )

    @property
    def trades(self) -> TradeJournalService:
        return self._trade_journal

    # ---- access ------------------------------------------------------------

    def server_time(self) -> datetime:
        return _utc(self._clock.now())

    @contextmanager
    def _open(self, account_id: str) -> Iterator[_Ctx]:
        try:
            store = ReadStore.open(self._db_path)
        except ReadStoreUnavailableError as error:
            raise DatabaseUnavailableError(str(error)) from error
        try:
            try:
                scopes = {row["account_scope_id"]: row for row in store.list_account_scopes()}
            except sqlite3.DatabaseError as error:
                raise DatabaseUnavailableError(f"database not initialized: {error}") from error
            row = scopes.get(account_id)
            if row is None:
                raise UnknownAccountError(account_id)
            with store.snapshot(account_id) as conn:
                yield _Ctx(conn, account_id, str(row["environment"]),
                           row["scenario_name"], self.server_time())
        finally:
            store.close()

    # ---- /accounts ---------------------------------------------------------

    def list_accounts(self) -> AccountsResponse:
        try:
            store = ReadStore.open(self._db_path)
        except ReadStoreUnavailableError as error:
            raise DatabaseUnavailableError(str(error)) from error
        try:
            scopes = store.list_account_scopes()
        except sqlite3.DatabaseError as error:
            raise DatabaseUnavailableError(f"database not initialized: {error}") from error
        finally:
            store.close()
        return AccountsResponse(
            accounts=[
                AccountSummary(
                    account_id=str(row["account_scope_id"]),
                    mode=str(row["environment"]),
                    scenario=row["scenario_name"],
                    label=self._settings.labels.get(
                        str(row["account_scope_id"]), str(row["account_scope_id"])
                    ),
                )
                for row in scopes
            ],
            server_time=self.server_time(),
        )

    # ---- shared facts ------------------------------------------------------

    def _health_row(self, ctx: _Ctx, component: str) -> dict[str, Any] | None:
        row = ctx.conn.execute(
            "SELECT status, heartbeat_at, observed_at, details_json FROM service_health "
            "WHERE account_scope_id = ? AND component = ?", (ctx.scope, component),
        ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "heartbeat_at": _dt(row[1]), "observed_at": _dt(row[2]),
                "details": _json_dict(row[3])}

    def _link(self, ctx: _Ctx, component: str, name: str, stale_after: timedelta,
              row: dict[str, Any] | None) -> _Link:
        if row is None:
            return _Link(None, None, None, False, "unavailable", None, {})
        details: dict[str, Any] = row["details"]
        as_of = row["heartbeat_at"] or row["observed_at"]
        age = max(ctx.now - as_of, timedelta(0)) if as_of else None
        declared = str(details.get("connected", "")).lower()
        if declared in ("true", "false"):
            connected: bool | None = declared == "true"
        else:
            connected = {"HEALTHY": True, "DEGRADED": True, "OFFLINE": False}.get(row["status"])
        reason: str | None = None
        if connected is False:
            reason = f"{name}_disconnected"
        elif connected is None:
            reason = f"{name}_unknown"
        elif age is None or age > stale_after:
            reason = f"{name}_stale"
        return _Link(connected, as_of, age, reason is None, reason, f"service_health:{component}",
                     details)

    def _facts(self, ctx: _Ctx) -> _Facts:
        s = self._settings
        now = ctx.now
        engine = self._health_row(ctx, _ENGINE)
        details: dict[str, Any] = engine["details"] if engine else {}
        heartbeat = engine["heartbeat_at"] if engine else None
        age = max(now - heartbeat, timedelta(0)) if heartbeat else None
        offline_after = timedelta(seconds=s.heartbeat_offline_seconds)
        controls = ctx.conn.execute(
            "SELECT paused, kill_switch_enabled FROM entry_controls WHERE account_scope_id = ?",
            (ctx.scope,),
        ).fetchone()
        paused = bool(controls[0]) if controls else False
        kill = bool(controls[1]) if controls else False
        reported = details.get("state") or (
            {"DEGRADED": "DEGRADED"}.get(engine["status"]) if engine else None
        )
        pre_state = compute_engine_state(
            heartbeat_at=heartbeat, now=now, offline_after=offline_after,
            reported_state=reported, paused=paused,
        )
        offline = pre_state == "OFFLINE"

        session = session_status(now)
        market: Literal["OPEN", "CLOSED", "HALTED"] = "OPEN" if session is SessionStatus.REGULAR \
            else "CLOSED"
        if market == "OPEN" and (
            str(details.get("market_status", "")).upper() == "HALTED"
            or str(details.get("market_halted", "")).lower() == "true"
        ):
            market = "HALTED"

        # Broker authority: real brokers use the broker link; local ledgers follow the engine.
        if ctx.mode in _BROKER_MODES:
            broker = self._link(ctx, "broker", "broker",
                                timedelta(seconds=s.heartbeat_stale_seconds),
                                self._health_row(ctx, "broker"))
        else:
            broker = _Link(not offline, heartbeat, age, not offline,
                           "engine_offline" if offline else None, "local_ledger", {})
        feed_row = self._health_row(ctx, "feed")
        feed = self._link(ctx, "feed", "feed", timedelta(seconds=s.max_bar_age_seconds), feed_row)
        if str(details.get("feed_freshness", "")).upper() == "STALE" and feed.reason is None:
            feed = _Link(feed.connected, feed.as_of, feed.age, False, "feed_stale",
                         feed.provenance, feed.details)
        elif feed_row is None and str(details.get("feed_freshness", "")).upper() == "FRESH":
            feed = _Link(True, heartbeat, age, True, None, "service_health:engine", {})
        feed_degrades = market == "OPEN" and not feed.fresh and feed.reason != "unavailable"
        state = compute_engine_state(
            heartbeat_at=heartbeat, now=now, offline_after=offline_after,
            reported_state=reported, paused=paused, feed_stale=feed_degrades,
        )

        today_cutoff, next_cutoff, cutoff_day = self._cutoffs(now)
        reasons: list[str] = []
        if state == "OFFLINE":
            reasons.append("engine_offline")
        if state == "RECONCILING":
            reasons.append("reconciling")
        if paused or state == "PAUSED":
            reasons.append("paused")
        if kill:
            reasons.append("kill_switch")
        if state != "OFFLINE" and feed_degrades:
            reasons.append(feed.reason or "feed_stale")
        if ctx.mode in _BROKER_MODES and state != "OFFLINE" and not broker.fresh:
            reasons.append(broker.reason or "broker_unavailable")
        if market == "CLOSED":
            reasons.append("market_closed")
        elif market == "HALTED":
            reasons.append("market_halted")
        elif today_cutoff is not None and now >= today_cutoff:
            reasons.append("past_entry_cutoff")
        drift = ctx.conn.execute(
            "SELECT 1 FROM reconciliation_drifts d LEFT JOIN reconciliation_resolutions r "
            "ON r.account_scope_id = d.account_scope_id AND r.drift_id = d.drift_id "
            "WHERE d.account_scope_id = ? AND r.resolution_id IS NULL LIMIT 1", (ctx.scope,),
        ).fetchone()
        if drift:
            reasons.append("unresolved_reconciliation_drift")
        reported_blocks = str(details.get("entry_block_reasons", ""))
        for item in (part.strip() for part in reported_blocks.split(",")):
            if item and item not in reasons and state != "OFFLINE":
                reasons.append(item)
        if str(details.get("entries_allowed", "")).lower() == "false" and not reasons:
            reasons.append("engine_blocked_entries")

        trust = _Trust(
            engine_offline=offline,
            broker_reason=None if broker.fresh else (broker.reason or "broker_unavailable"),
            feed_reason=(
                None if market == "OPEN" and feed.fresh
                else ("market_closed" if market != "OPEN" else (feed.reason or "feed_stale"))
            ),
        )
        return _Facts(now, heartbeat, age, state, broker, feed, market, session, today_cutoff,
                      next_cutoff, cutoff_day, tuple(dict.fromkeys(reasons)), trust, details,
                      paused, kill)

    def _cutoffs(self, now: datetime) -> tuple[datetime | None, datetime | None, date | None]:
        """Return (today's cutoff if a trading day, next cutoff after now, its session date)."""
        try:
            local_day = now.astimezone(NEW_YORK).date()
            today: datetime | None = None
            if is_trading_day(local_day):
                today = self._cutoff_for(local_day)
            candidate, day = today, local_day
            if candidate is None or now >= candidate:
                session = next_session(local_day)
                day, candidate = session.session_date, self._cutoff_for(session.session_date)
            return today, candidate, day
        except ValueError:
            return None, None, None

    def _cutoff_for(self, day: date) -> datetime:
        session = session_for(day)
        wall = datetime.combine(day, self._settings.entry_cutoff, NEW_YORK)
        return min(wall, session.close_et).astimezone(UTC)

    # ---- config fallbacks ----------------------------------------------------

    def _run_config(self, ctx: _Ctx) -> dict[str, Any]:
        row = ctx.conn.execute(
            "SELECT config_json FROM run_configs WHERE account_scope_id = ? "
            "ORDER BY recorded_at DESC LIMIT 1", (ctx.scope,),
        ).fetchone()
        return _json_dict(row[0]) if row else {}

    def _total_budget(self, ctx: _Ctx) -> Decimal | None:
        if self._settings.total_application_budget is not None:
            return self._settings.total_application_budget
        cfg = self._run_config(ctx)
        risk = cfg.get("risk")
        raw = risk.get("total_application_budget") if isinstance(risk, dict) else None
        return _dec(raw if raw is not None else cfg.get("budget"))

    def _symbol_settings(self, ctx: _Ctx) -> dict[str, SymbolSettings]:
        merged: dict[str, SymbolSettings] = {}
        symbols = self._run_config(ctx).get("symbols")
        if isinstance(symbols, dict):
            for name, raw in symbols.items():
                if isinstance(raw, dict):
                    merged[str(name).upper()] = SymbolSettings(
                        budget=_dec(raw.get("budget")),
                        planned_loss_cap=_dec(raw.get("planned_loss_cap")),
                        min_hold_seconds=_int(raw.get("min_hold_seconds")),
                        max_entry_cycles_per_session=_int(raw.get("max_entry_cycles_per_session")),
                        consecutive_loss_limit=_int(raw.get("consecutive_loss_limit")),
                        consecutive_loss_reset=raw.get("consecutive_loss_reset"),
                    )
                else:
                    merged.setdefault(str(name).upper(), SymbolSettings())
        for name, cfg in self._settings.symbols.items():
            merged[name.upper()] = cfg
        return merged

    # ---- /status -------------------------------------------------------------

    def status(self, account_id: str) -> AccountStatus:
        with self._open(account_id) as ctx:
            facts = self._facts(ctx)
        return self._status_model(ctx, facts)

    def _status_model(self, ctx: _Ctx, f: _Facts) -> AccountStatus:
        s = self._settings
        is_stale = f.heartbeat_age is None or f.heartbeat_age > timedelta(
            seconds=s.heartbeat_stale_seconds)
        session_day: date | None = None
        session_close: datetime | None = None
        early: bool | None = None
        try:
            local_day = ctx.now.astimezone(NEW_YORK).date()
            if is_trading_day(local_day):
                session = session_for(local_day)
                session_day, session_close, early = (
                    session.session_date, session.close_utc, session.is_early_close)
        except ValueError:
            pass
        reason = "engine_offline" if f.state == "OFFLINE" else ("heartbeat_stale" if is_stale
                                                                  else None)
        return AccountStatus(
            account_id=ctx.scope, mode=ctx.mode, scenario=ctx.scenario, engine_state=f.state,
            last_heartbeat_at=f.heartbeat_at, last_heartbeat_at_et=_et(f.heartbeat_at),
            heartbeat_age_seconds=_seconds(f.heartbeat_age), is_stale=is_stale,
            fresh=reason is None, stale_reason=reason,
            broker=f.broker.model(), data=f.feed.model(), market=f.market,
            exchange_clock=ExchangeClock(
                exchange="XNYS", timezone="America/New_York",
                now_et=ctx.now.astimezone(NEW_YORK).isoformat(), session_date=session_day,
                session_status=f.session.value, is_early_close=early, close_at=session_close,
                close_at_et=_et(session_close),
            ),
            next_entry_cutoff=EntryCutoff(
                at=f.cutoff_next, at_et=_et(f.cutoff_next), session_date=f.cutoff_session,
                passed_today=f.cutoff_today is not None and ctx.now >= f.cutoff_today,
            ),
            entries_allowed=not f.reasons, entries_blocked_reasons=list(f.reasons),
            server_time=ctx.now, server_time_et=ctx.now.astimezone(NEW_YORK).isoformat(),
        )

    # ---- shared projections ----------------------------------------------------

    def _positions(self, ctx: _Ctx) -> tuple[projections.PositionView, ...]:
        return projections.positions(ctx.conn, ctx.scope, now=ctx.now)

    def _trades(self, ctx: _Ctx) -> tuple[projections.RecentTradeView, ...]:
        return projections.recent_trades(ctx.conn, ctx.scope, now=ctx.now, limit=500)

    @staticmethod
    def _managed(items: tuple[projections.PositionView, ...]) -> list[projections.PositionView]:
        return [p for p in items if p.management != "UNMANAGED"]

    @staticmethod
    def _protection_issues(
        positions: list[projections.PositionView],
        protections: tuple[projections.ProtectionView, ...],
    ) -> dict[str, str]:
        """Map symbol -> issue for managed positions with missing/unknown/short stops."""
        issues: dict[str, str] = {}
        for pos in positions:
            rows = [p for p in protections if p.cycle_id == pos.cycle_id
                    and p.status in _LIVE_PROTECTION]
            covering = [p for p in rows if p.status in _COVERING_PROTECTION]
            if not rows:
                issues[pos.symbol] = "UNPROTECTED"
            elif any(p.status == "UNKNOWN" for p in rows):
                issues[pos.symbol] = "UNKNOWN_STOP"
            elif not covering or sum((p.covered_quantity for p in covering), _ZERO) < pos.quantity:
                issues[pos.symbol] = "COVERAGE_GAP"
        return issues

    def _latest_eval_problems(self, ctx: _Ctx) -> set[str]:
        rows = ctx.conn.execute(
            "SELECT e.symbol, e.status FROM strategy_evaluations e WHERE e.account_scope_id = ? "
            "AND e.evaluated_at = (SELECT MAX(x.evaluated_at) FROM strategy_evaluations x "
            "WHERE x.account_scope_id = e.account_scope_id AND x.symbol = e.symbol)",
            (ctx.scope,),
        ).fetchall()
        return {r[0] for r in rows if r[1] in ("ERROR", "STALE_DATA")}

    # ---- /kpis -----------------------------------------------------------------

    def kpis(self, account_id: str) -> KpisResponse:
        with self._open(account_id) as ctx:
            f = self._facts(ctx)
            v = _Values(f.trust)
            account = projections.account_snapshot(
                ctx.conn, ctx.scope, now=ctx.now,
                stale_after=timedelta(seconds=self._settings.snapshot_stale_seconds))
            source_row = ctx.conn.execute(
                "SELECT source FROM equity_snapshots WHERE account_scope_id = ? "
                "ORDER BY as_of DESC LIMIT 1", (ctx.scope,)).fetchone()
            positions = self._positions(ctx)
            managed = self._managed(positions)
            orders = projections.orders(ctx.conn, ctx.scope, now=ctx.now, limit=500)
            protections = projections.protections(ctx.conn, ctx.scope, now=ctx.now, limit=500)
            trades = self._trades(ctx)
            drifts = projections.reconciliation_status(ctx.conn, ctx.scope, now=ctx.now)
            stale_symbols = self._stale_symbols(ctx, f)
            eval_problems = self._latest_eval_problems(ctx)
            total_budget = self._total_budget(ctx)

        snap_prov = f"equity_snapshots:{source_row[0]}" if source_row else None

        def balance(value: Decimal | None) -> Sourced[Any]:
            if value is None:
                return v.unavailable(snap_prov)
            reason = f.trust.reason("broker") or (
                "snapshot_stale" if account.is_stale else None)
            return Sourced(value=value, as_of=account.snapshot_as_of, fresh=reason is None,
                           stale_reason=reason, provenance=snap_prov)

        unrealized: Decimal | None = None
        if all(p.unrealized_pl is not None for p in managed):
            unrealized = sum((p.unrealized_pl for p in managed if p.unrealized_pl is not None),
                             _ZERO)
        position_as_of = max((p.as_of for p in positions), default=None)
        today = ctx.now.astimezone(NEW_YORK).date()
        daily = sum((t.realized_pnl for t in trades
                     if t.closed_at.astimezone(NEW_YORK).date() == today), _ZERO)
        lifetime_pnl = account.realized_pnl_total + unrealized if unrealized is not None else None
        issues = self._protection_issues(managed, protections)
        open_orders = [o for o in orders if o.is_open]
        return KpisResponse(
            account_id=ctx.scope, mode=ctx.mode, engine_state=f.state, server_time=ctx.now,
            cash=balance(account.cash), buying_power=balance(account.buying_power),
            equity=balance(account.equity),
            configured_total_budget=(
                v.static(total_budget, "config") if total_budget is not None
                else v.unavailable("config")),
            reserved_pending_buys=v.of(account.active_budget_reservations, "engine",
                                       as_of=ctx.now, provenance="budget_reservations"),
            deployed_capital=v.of(
                sum((p.quantity * p.average_entry_price for p in managed), _ZERO), "broker",
                as_of=position_as_of, provenance="positions:cost_basis"),
            open_position_count=v.of(len(managed), "broker", as_of=position_as_of,
                                     provenance="positions"),
            pending_order_count=v.of(len(open_orders), "broker",
                                     as_of=max((o.updated_at for o in orders), default=None),
                                     provenance="broker_orders"),
            daily_realized_pnl=v.of(daily, "engine", as_of=ctx.now, provenance="fills:session_et"),
            daily_unrealized_pnl=(
                v.of(unrealized, "broker", as_of=position_as_of, provenance="positions")
                if unrealized is not None else v.unavailable("positions")),
            lifetime_realized_pnl=v.of(account.realized_pnl_total, "engine", as_of=ctx.now,
                                       provenance="fills"),
            lifetime_pnl=(
                v.of(lifetime_pnl, "engine", "broker", as_of=position_as_of,
                     provenance="fills+positions")
                if lifetime_pnl is not None else v.unavailable("fills+positions")),
            protection_error_count=v.of(len(issues), "broker", as_of=ctx.now,
                                        provenance="protection_orders"),
            data_error_count=v.of(len(stale_symbols | eval_problems), "engine", as_of=ctx.now,
                                  provenance="market_bars+strategy_evaluations"),
            reconciliation_error_count=v.of(len(drifts.unresolved_drift_items), "engine",
                                            as_of=drifts.last_run_at,
                                            provenance="reconciliation_drifts"),
        )

    def _latest_bar(self, ctx: _Ctx, symbol: str) -> tuple[Any, ...] | None:
        row: tuple[Any, ...] | None = ctx.conn.execute(
            "SELECT bar_start, close_price, feed, provenance, recorded_at FROM market_bars "
            "WHERE account_scope_id = ? AND symbol = ? AND timeframe IN ('1m','1Min') "
            "ORDER BY bar_start DESC LIMIT 1", (ctx.scope, symbol),
        ).fetchone()
        return row

    def _bar_stale(self, ctx: _Ctx, bar_start: datetime | None) -> bool:
        return bar_start is None or (
            ctx.now - bar_start > timedelta(seconds=self._settings.max_bar_age_seconds))

    def _stale_symbols(self, ctx: _Ctx, f: _Facts) -> set[str]:
        """Symbols whose data is stale while the market is open."""
        if f.market != "OPEN":
            return set()
        stale: set[str] = set()
        for symbol in self._watch_symbols(ctx, self._symbol_settings(ctx)):
            bar = self._latest_bar(ctx, symbol)
            if self._bar_stale(ctx, _dt(bar[0]) if bar else None):
                stale.add(symbol)
        return stale

    # ---- /watchlist --------------------------------------------------------------

    def _watch_symbols(self, ctx: _Ctx, configured: Mapping[str, SymbolSettings]) -> list[str]:
        names = list(configured)
        for row in projections.symbol_watch(ctx.conn, ctx.scope, now=ctx.now):
            if row.symbol not in names:
                names.append(row.symbol)
        return names[:_MAX_WATCHLIST]

    def watchlist(self, account_id: str) -> WatchlistResponse:
        with self._open(account_id) as ctx:
            f = self._facts(ctx)
            configured = self._symbol_settings(ctx)
            symbols = self._watch_symbols(ctx, configured)
            positions = {p.symbol: p for p in self._positions(ctx)}
            protections = projections.protections(ctx.conn, ctx.scope, now=ctx.now, limit=500)
            eligibility = {e.symbol: e for e in
                           projections.eligibility(ctx.conn, ctx.scope, now=ctx.now)}
            watch = {w.symbol: w for w in
                     projections.symbol_watch(ctx.conn, ctx.scope, now=ctx.now)}
            trades = self._trades(ctx)
            rows = [
                self._watch_row(ctx, f, symbol, symbol in configured,
                                configured.get(symbol, SymbolSettings()), positions.get(symbol),
                                protections, eligibility.get(symbol), watch.get(symbol), trades)
                for symbol in symbols
            ]
        return WatchlistResponse(account_id=ctx.scope, mode=ctx.mode, engine_state=f.state,
                                 server_time=ctx.now, symbols=rows)

    def _watch_row(
        self, ctx: _Ctx, f: _Facts, symbol: str, is_configured: bool, cfg: SymbolSettings,
        position: projections.PositionView | None,
        protections: tuple[projections.ProtectionView, ...],
        elig: projections.EligibilityView | None, watch: projections.SymbolWatchRow | None,
        trades: tuple[projections.RecentTradeView, ...],
    ) -> WatchRow:
        v = _Values(f.trust)
        unavailable = v.unavailable
        now = ctx.now
        q = ctx.conn.execute

        # market data
        bar = self._latest_bar(ctx, symbol)
        bar_at = _dt(bar[0]) if bar else None
        stale = self._bar_stale(ctx, bar_at)
        if bar is None:
            price, feed, age = unavailable("market_bars"), unavailable("market_bars"), \
                unavailable("market_bars")
        else:
            prov = f"market_bars:{bar[2]}" + (f":{bar[3]}" if bar[3] else "")
            market_reason = f.trust.reason("feed") or ("bar_stale" if stale else None)
            price = Sourced(value=_dec(bar[1]), as_of=bar_at, fresh=market_reason is None,
                            stale_reason=market_reason, provenance=prov)
            feed = Sourced(value=f"{bar[2]} ({bar[3]})" if bar[3] else str(bar[2]), as_of=bar_at,
                           fresh=market_reason is None, stale_reason=market_reason,
                           provenance="market_bars")
            age = Sourced(value=_seconds(now - bar_at) if bar_at else None, as_of=bar_at,
                          fresh=market_reason is None, stale_reason=market_reason,
                          provenance="market_bars")

        # strategies and evaluations
        instances = q(
            "SELECT role, strategy_key FROM strategy_instances WHERE account_scope_id = ? "
            "AND symbol = ? AND enabled = 1 ORDER BY strategy_key", (ctx.scope, symbol)
        ).fetchall()
        buys = [r[1] for r in instances if r[0] == "BUY"]
        sells = [r[1] for r in instances if r[0] == "SELL"]
        last_eval_row = q(
            "SELECT e.evaluated_at, e.status, e.reason, e.bar_time, i.strategy_key, i.role "
            "FROM strategy_evaluations e LEFT JOIN strategy_instances i "
            "ON i.account_scope_id = e.account_scope_id AND i.instance_id = e.instance_id "
            "WHERE e.account_scope_id = ? AND e.symbol = ? "
            "ORDER BY e.evaluated_at DESC LIMIT 1", (ctx.scope, symbol),
        ).fetchone()
        last_eval = (
            v.of(EvaluationSummary(
                evaluated_at=_req(last_eval_row[0]), status=last_eval_row[1],
                reason=last_eval_row[2], strategy_key=last_eval_row[4], role=last_eval_row[5],
                bar_time=_dt(last_eval_row[3])), "engine", as_of=_dt(last_eval_row[0]),
                provenance="strategy_evaluations")
            if last_eval_row else unavailable("strategy_evaluations"))
        signal = self._last_signal(ctx, symbol)
        last_signal = (
            v.of(signal, "engine", as_of=signal.generated_at, provenance="signals+decisions")
            if signal else unavailable("signals"))

        # cycle, position, budget
        cycle = q(
            "SELECT cycle_id, state, opened_at FROM position_cycles WHERE account_scope_id = ? "
            "AND symbol = ? AND state IN ('PENDING_ENTRY','OPEN','EXITING') "
            "ORDER BY updated_at DESC LIMIT 1", (ctx.scope, symbol),
        ).fetchone()
        opened_at = _dt(cycle[2]) if cycle else None
        quantity = position.quantity if position else _ZERO
        cost = quantity * position.average_entry_price if position else _ZERO
        reserved = sum((schema.decimal_from_db(r[0]) for r in q(
            "SELECT r.amount FROM budget_reservations r JOIN position_cycles c "
            "ON c.account_scope_id = r.account_scope_id AND c.cycle_id = r.cycle_id "
            "WHERE r.account_scope_id = ? AND r.state = 'ACTIVE' AND c.symbol = ?",
            (ctx.scope, symbol)).fetchall()), _ZERO)
        position_as_of = position.as_of if position else None

        # protection
        live = [p for p in protections if p.symbol == symbol and p.status in _LIVE_PROTECTION]
        prot = live[0] if live else None
        trailing = q(
            "SELECT t.kind, t.stop_price, t.high_water_mark, t.broker_order_id, "
            "t.last_confirmed_at, t.updated_at FROM trailing_stop_states t "
            "JOIN position_cycles c ON c.account_scope_id = t.account_scope_id "
            "AND c.cycle_id = t.cycle_id WHERE t.account_scope_id = ? AND t.symbol = ? "
            "AND c.state IN ('PENDING_ENTRY','OPEN','EXITING') "
            "ORDER BY t.updated_at DESC LIMIT 1", (ctx.scope, symbol),
        ).fetchone()
        confirmed_stop: Decimal | None = None
        broker_order_id = (trailing[3] if trailing else None) or (prot.broker_order_id
                                                                   if prot else None)
        if broker_order_id:
            order_row = q("SELECT stop_price FROM broker_orders WHERE account_scope_id = ? "
                          "AND broker_order_id = ?", (ctx.scope, broker_order_id)).fetchone()
            confirmed_stop = _dec(order_row[0]) if order_row else None
        issue = self._protection_issues([position], protections).get(symbol) if (
            position and position.management != "UNMANAGED") else None
        protection_status = prot.status if prot else issue
        kind = prot.kind if prot else (trailing[0] if trailing else None)
        desired = _dec(trailing[1]) if trailing else (prot.stop_price if prot else None)
        hwm = _dec(trailing[2]) if trailing else None
        last_update = _dt(trailing[5]) if trailing else (prot.as_of if prot else None)

        # realized / unrealized
        today = now.astimezone(NEW_YORK).date()
        mine = [t for t in trades if t.symbol == symbol]
        realized_today = sum((t.realized_pnl for t in mine
                              if t.closed_at.astimezone(NEW_YORK).date() == today), _ZERO)
        realized_all = sum((t.realized_pnl for t in mine), _ZERO)
        unrealized = position.unrealized_pl if position else _ZERO
        lifetime = realized_all + unrealized if unrealized is not None else None

        # eligibility
        cap = cfg.max_entry_cycles_per_session
        lock_until = elig.lockout_until if elig else None
        cool_until = elig.cooldown_until if elig else None
        eligible_at = max((t for t in (lock_until, cool_until) if t is not None), default=None)
        min_hold = cfg.min_hold_seconds
        hold_remaining = (
            max(0, int((opened_at + timedelta(seconds=min_hold) - now).total_seconds()))
            if min_hold is not None and opened_at is not None and position else None)
        phase = self._phase(f, cycle[1] if cycle else None, position, protection_status,
                            elig.block_reasons if elig else ())

        return WatchRow(
            symbol=symbol, configured=is_configured, price=price,
            bid=unavailable("quotes"), ask=unavailable("quotes"), data_age_seconds=age,
            data_stale=stale or f.trust.reason("feed") is not None, feed=feed,
            phase=v.of(phase, "engine", as_of=now, provenance="position_cycles+entry_controls"),
            active_buy_strategies=v.of(buys, "engine", provenance="strategy_instances"),
            active_sell_strategies=v.of(sells, "engine", provenance="strategy_instances"),
            next_due_trigger=unavailable("scheduler"),
            last_evaluation=last_eval, last_signal=last_signal,
            confirmed_quantity=v.of(quantity, "broker", as_of=position_as_of,
                                    provenance="positions"),
            cost_basis=v.of(cost, "broker", as_of=position_as_of, provenance="positions"),
            purchase_time=v.of(opened_at, "engine", as_of=opened_at,
                               provenance="position_cycles"),
            symbol_budget=(v.static(cfg.budget, "config") if cfg.budget is not None
                           else unavailable("config")),
            commitment=v.of(cost + reserved, "engine", "broker", as_of=position_as_of,
                            provenance="positions+budget_reservations"),
            planned_loss=(v.static(cfg.planned_loss_cap, "config")
                          if cfg.planned_loss_cap is not None else unavailable("config")),
            daily_entry_count=v.of(elig.filled_entry_cycles_today if elig else 0, "engine",
                                   as_of=elig.as_of if elig else None,
                                   provenance="entry_eligibility_counters"),
            daily_entry_cap=(v.static(cap, "config") if cap is not None
                             else unavailable("config")),
            loss_streak=v.of(elig.consecutive_losses if elig else 0, "engine",
                             as_of=elig.as_of if elig else None,
                             provenance="entry_eligibility_counters"),
            lockout_active=v.of(bool(elig and "LOCKOUT" in elig.block_reasons), "engine",
                                provenance="entry_lockouts"),
            lockout_until=v.of(lock_until, "engine", provenance="entry_lockouts"),
            loss_reset_mode=(v.static(cfg.consecutive_loss_reset, "config")
                             if cfg.consecutive_loss_reset else unavailable("config")),
            cooldown_until=v.of(cool_until, "engine", provenance="entry_cooldowns"),
            next_eligible_at=v.of(eligible_at, "engine",
                                  provenance="entry_lockouts+entry_cooldowns"),
            min_hold_remaining_seconds=(
                v.of(hold_remaining, "engine", provenance="position_cycles+config")
                if min_hold is not None else unavailable("config")),
            entry_cutoff=v.of(f.cutoff_next, provenance="calendar"),
            protection_mode=v.of(kind, "broker", provenance="protection_orders"),
            protection_status=v.of(protection_status, "broker", as_of=last_update,
                                   provenance="protection_orders"),
            protection_price=v.of(prot.stop_price if prot else desired, "broker",
                                  as_of=last_update, provenance="protection_orders"),
            protection_quantity=v.of(prot.covered_quantity if prot else None, "broker",
                                     as_of=last_update, provenance="protection_orders"),
            native_broker_hwm=v.of(hwm if kind == "BROKER_TRAILING_STOP" else None, "broker",
                                   as_of=last_update, provenance="trailing_stop_states"),
            stepped_anchor=v.of(hwm if kind == "STEPPED_TRAILING_STOP" else None, "broker",
                                as_of=last_update, provenance="trailing_stop_states"),
            desired_stop=v.of(desired, "broker", as_of=last_update,
                              provenance="trailing_stop_states"),
            confirmed_stop=v.of(confirmed_stop, "broker", as_of=last_update,
                                provenance="broker_orders"),
            last_protection_update=v.of(last_update, "broker", as_of=last_update,
                                        provenance="trailing_stop_states"),
            realized_pnl_today=v.of(realized_today, "engine", as_of=now, provenance="fills"),
            unrealized_pnl=v.of(unrealized, "broker", as_of=position_as_of,
                                provenance="positions"),
            lifetime_realized_pnl=v.of(realized_all, "engine", as_of=now, provenance="fills"),
            lifetime_pnl=v.of(lifetime, "engine", "broker", as_of=position_as_of,
                              provenance="fills+positions"),
        )

    @staticmethod
    def _phase(f: _Facts, cycle_state: str | None, position: projections.PositionView | None,
               protection_status: str | None, blocks: tuple[str, ...]) -> str:
        if position is not None and position.management == "UNMANAGED":
            return "UNMANAGED"
        if f.state == "RECONCILING":
            return "RECONCILING"
        if cycle_state == "PENDING_ENTRY":
            return "BUY_PENDING"
        if cycle_state == "EXITING":
            return "SELL_PENDING"
        if cycle_state == "OPEN":
            return ("PROTECTIVE_PENDING" if protection_status in (None, "PENDING", "UNPROTECTED")
                    else "WATCHING_SELL")
        if f.state == "PAUSED":
            return "PAUSED"
        return "WATCHING_BUY"

    def _last_signal(self, ctx: _Ctx, symbol: str) -> SignalSummary | None:
        row = ctx.conn.execute(
            "SELECT s.signal_id, s.action, s.reference_price, s.reason, s.generated_at, "
            "d.outcome, d.reason FROM signals s LEFT JOIN decisions d "
            "ON d.account_scope_id = s.account_scope_id AND d.signal_id = s.signal_id "
            "WHERE s.account_scope_id = ? AND s.symbol = ? "
            "ORDER BY s.generated_at DESC, d.decided_at DESC LIMIT 1", (ctx.scope, symbol),
        ).fetchone()
        return None if row is None else self._signal_model(row)

    @staticmethod
    def _signal_model(row: tuple[Any, ...]) -> SignalSummary:
        outcome = row[5]
        disposition: Literal["ACCEPTED", "REJECTED", "UNDECIDED"] = (
            "UNDECIDED" if outcome is None
            else "ACCEPTED" if str(outcome).upper() in _ACCEPTED_OUTCOMES else "REJECTED")
        return SignalSummary(
            signal_id=row[0], action=row[1], reference_price=_dec(row[2]), reason=row[3],
            generated_at=_req(row[4]), disposition=disposition, disposition_reason=row[6])

    # ---- /cockpit (symbol pipeline & strategy activity) ----------------------

    def cockpit(self, account_id: str) -> CockpitResponse:
        with self._open(account_id) as ctx:
            f = self._facts(ctx)
            q = ctx.conn.execute
            configured = self._symbol_settings(ctx)
            symbols = self._watch_symbols(ctx, configured)
            positions = {p.symbol: p for p in self._positions(ctx)}
            protections = projections.protections(ctx.conn, ctx.scope, now=ctx.now, limit=500)

            pipelines: list[SymbolPipeline] = []
            for symbol in symbols:
                position = positions.get(symbol)
                sym_protections = [p for p in protections if p.symbol == symbol
                                   and p.status in _LIVE_PROTECTION]
                protection_status = next((p.status for p in sym_protections), None)
                pipelines.append(self._symbol_pipeline(
                    ctx, f, symbol, symbol in configured, configured.get(symbol, SymbolSettings()),
                    position, sym_protections, protection_status))
        return CockpitResponse(
            account_id=ctx.scope, mode=ctx.mode, engine_state=f.state, server_time=ctx.now,
            entries_allowed=not f.reasons, entries_blocked_reasons=list(f.reasons),
            symbols=pipelines)

    def _symbol_pipeline(
        self, ctx: _Ctx, f: _Facts, symbol: str, is_configured: bool, cfg: SymbolSettings,
        position: projections.PositionView | None,
        sym_protections: list[projections.ProtectionView],
        protection_status: str | None,
    ) -> SymbolPipeline:
        q = ctx.conn.execute
        scope, now = ctx.scope, ctx.now

        # configured strategy instances per role
        instance_rows = q(
            "SELECT instance_id, strategy_key, strategy_version, role, enabled "
            "FROM strategy_instances WHERE account_scope_id = ? AND symbol = ? "
            "ORDER BY role, strategy_key", (scope, symbol)).fetchall()
        buys = [r[1] for r in instance_rows if r[3] == "BUY" and r[4]]
        sells = [r[1] for r in instance_rows if r[3] == "SELL" and r[4]]
        shorts = [r[1] for r in instance_rows if r[3] == "SHORT" and r[4]]
        covers = [r[1] for r in instance_rows if r[3] == "COVER" and r[4]]

        # persisted engine state per instance
        state_by_instance = {
            r[0]: r for r in q(
                "SELECT instance_id, state_json, armed, rearm_required, updated_at "
                "FROM strategy_state WHERE account_scope_id = ? AND symbol = ?",
                (scope, symbol)).fetchall()
        }

        # latest evaluation + latest signal for the symbol
        last_eval_row = q(
            "SELECT e.evaluated_at, e.status, e.reason, e.bar_time, i.strategy_key, i.role "
            "FROM strategy_evaluations e LEFT JOIN strategy_instances i "
            "ON i.account_scope_id = e.account_scope_id AND i.instance_id = e.instance_id "
            "WHERE e.account_scope_id = ? AND e.symbol = ? "
            "ORDER BY e.evaluated_at DESC LIMIT 1", (scope, symbol)).fetchone()
        last_evaluation = (
            EvaluationSummary(
                evaluated_at=_req(last_eval_row[0]), status=last_eval_row[1],
                reason=last_eval_row[2], strategy_key=last_eval_row[4], role=last_eval_row[5],
                bar_time=_dt(last_eval_row[3]))
            if last_eval_row else None)
        last_signal = self._last_signal(ctx, symbol)

        # open lifecycle cycle (engine truth, both directions)
        cycle_row = q(
            "SELECT cycle_id, state, direction, entry_strategy_key, opened_at, updated_at "
            "FROM position_cycles WHERE account_scope_id = ? AND symbol = ? "
            "AND state IN ('PENDING_ENTRY','OPEN','EXITING') "
            "ORDER BY updated_at DESC LIMIT 1", (scope, symbol)).fetchone()
        cycle = (
            PipelineCycle(cycle_id=cycle_row[0], state=cycle_row[1], direction=cycle_row[2],
                          entry_strategy_key=cycle_row[3], opened_at=_dt(cycle_row[4]))
            if cycle_row else None)

        # broker orders for the symbol with intent purpose attribution
        order_rows = q(
            "SELECT b.broker_order_id, b.client_order_id, i.purpose, b.side, b.order_type, "
            "b.status, b.quantity, b.filled_quantity, b.limit_price, b.stop_price, "
            "b.submitted_at, b.updated_at FROM broker_orders b "
            "LEFT JOIN order_intents i ON i.account_scope_id = b.account_scope_id "
            "AND i.intent_id = b.intent_id WHERE b.account_scope_id = ? AND b.symbol = ? "
            "ORDER BY b.updated_at DESC LIMIT 50", (scope, symbol)).fetchall()
        orders = [
            PipelineOrder(
                broker_order_id=r[0], client_order_id=r[1], purpose=r[2], side=r[3],
                order_type=r[4], status=r[5], quantity=_dec(r[6]), filled_quantity=_dec(r[7]),
                limit_price=_dec(r[8]), stop_price=_dec(r[9]), submitted_at=_dt(r[10]),
                updated_at=_dt(r[11]), is_open=r[5] in projections._OPEN_ORDER)
            for r in order_rows
        ]
        pending = [o for o in orders if o.is_open]
        entry_sides = {"BUY", "SELL_SHORT"}
        exit_sides = {"SELL", "BUY_TO_COVER"}
        pending_entry = [o for o in pending if (o.purpose or "").upper() in ("ENTRY", "")
                         or (not o.purpose and o.side.upper() in entry_sides)]
        pending_exit = [o for o in pending if (o.purpose or "").upper() in
                        ("EXIT", "STRATEGY_EXIT", "PROTECTIVE_STOP", "TRAILING_STOP")
                        or (not o.purpose and o.side.upper() in exit_sides)]

        reserved = sum((schema.decimal_from_db(r[0]) for r in q(
            "SELECT r.amount FROM budget_reservations r JOIN position_cycles c "
            "ON c.account_scope_id = r.account_scope_id AND c.cycle_id = r.cycle_id "
            "WHERE r.account_scope_id = ? AND r.state = 'ACTIVE' AND c.symbol = ?",
            (scope, symbol)).fetchall()), _ZERO)

        # cooldown / lockout from the authoritative gate tables
        cooldown_until = _dt(q(
            "SELECT MAX(cooldown_until) FROM entry_cooldowns WHERE account_scope_id = ? "
            "AND symbol = ? AND cleared_at IS NULL AND cooldown_until > ?",
            (scope, symbol, schema.to_db_timestamp(now))).fetchone()[0])
        lockout_until = _dt(q(
            "SELECT MAX(locked_until) FROM entry_lockouts WHERE account_scope_id = ? "
            "AND symbol = ? AND cleared_at IS NULL AND locked_until IS NOT NULL",
            (scope, symbol)).fetchone()[0])
        block_reasons: list[str] = []
        if cooldown_until is not None:
            block_reasons.append("COOLDOWN")
        if lockout_until is not None:
            block_reasons.append("LOCKOUT")
        if f.state == "OFFLINE":
            block_reasons.append("engine_offline")
        elif f.state == "PAUSED":
            block_reasons.append("paused")
        if f.kill_switch:
            block_reasons.append("kill_switch")
        for reason in f.reasons:
            if reason not in ("LOCKOUT", "COOLDOWN"):
                block_reasons.append(reason)
        block_reasons = list(dict.fromkeys(block_reasons))

        protections = [
            PipelineProtection(
                protection_order_id=p.protection_order_id, cycle_id=p.cycle_id, kind=p.kind,
                status=p.status, stop_price=p.stop_price, high_water_mark=p.high_water_mark,
                covered_quantity=p.covered_quantity, remaining_quantity=p.remaining_quantity,
                coverage_gap=p.coverage_gap, updated_at=p.as_of)
            for p in sym_protections
        ]

        # per-instance stage: only recorded evidence, newest first
        instances: list[StrategyInstanceState] = []
        for r in instance_rows:
            instance_id, key, version, role, enabled = r[0], r[1], r[2], r[3], bool(r[4])
            state_row = state_by_instance.get(instance_id)
            engine_state_value: str | None = None
            stage_as_of: datetime | None = None
            if state_row is not None:
                state_payload = _json_dict(state_row[1])
                eligibility = state_payload.get("eligibility")
                engine_state_value = str(eligibility) if eligibility else None
                stage_as_of = _dt(state_row[4])
            instances.append(self._instance_stage(
                ctx, symbol, instance_id, role, key, version, enabled,
                state_row, engine_state_value, stage_as_of, pending))

        # phase reuses the engine's own naming (watchlist-compatible)
        phase = self._phase(f, cycle.state if cycle else None, position, protection_status, ())
        last_activity = q(
            "SELECT MAX(occurred_at) FROM domain_events WHERE account_scope_id = ? "
            "AND (json_extract(payload_json, '$.data.symbol') = ? OR stream_id LIKE '%' || ?)",
            (scope, symbol, f":{symbol}")).fetchone()[0]

        return SymbolPipeline(
            symbol=symbol, configured=is_configured, phase=phase,
            direction=cycle.direction if cycle else None,
            buy_strategies=buys, sell_strategies=sells, short_strategies=shorts,
            cover_strategies=covers, instances=instances,
            last_evaluation=last_evaluation, last_signal=last_signal, cycle=cycle,
            position_quantity=position.quantity if position else None,
            position_entry_price=position.average_entry_price if position else None,
            unrealized_pnl=position.unrealized_pl if position else None,
            pending_orders=pending, pending_entry_orders=len(pending_entry),
            pending_exit_orders=len(pending_exit),
            reserved_budget=reserved if reserved else None,
            cooldown_until=cooldown_until, lockout_until=lockout_until,
            block_reasons=block_reasons, protections=protections,
            last_activity_at=_dt(last_activity) if last_activity else None,
        )

    def _instance_stage(
        self, ctx: _Ctx, symbol: str, instance_id: str, role: str, strategy_key: str,
        version: str | None, enabled: bool, state_row: Any, engine_state_value: str | None,
        stage_as_of: datetime | None, pending: list[PipelineOrder],
    ) -> StrategyInstanceState:
        """Pipeline stage strictly from recorded evidence for this instance."""
        q = ctx.conn.execute
        scope = ctx.scope

        # 1. a pending order matching this instance's role for the symbol
        role_sides = {"BUY": "BUY", "SELL": "SELL", "SHORT": "SELL_SHORT",
                      "COVER": "BUY_TO_COVER"}.get(role.upper())
        for order in pending:
            if order.side.upper() == (role_sides or "") and (
                    (order.purpose or "").upper() in ("ENTRY", "EXIT", "STRATEGY_EXIT")):
                return StrategyInstanceState(
                    instance_id=instance_id, role=role, strategy_key=strategy_key,
                    strategy_version=version, enabled=enabled, engine_state=engine_state_value,
                    armed=bool(state_row[2]) if state_row else None,
                    rearm_required=bool(state_row[3]) if state_row else None,
                    stage="ORDER_SUBMITTED",
                    stage_detail=f"{order.side} {order.quantity} {symbol} "
                                 f"{order.order_type or ''}".strip(),
                    stage_as_of=order.submitted_at,
                    last_signal_bar=_iso(_json_dict(state_row[1]).get("last_signal_bar"))
                    if state_row else None)

        # 2. an open position entered by this strategy
        if role.upper() in ("BUY", "SHORT"):
            entry = q(
                "SELECT c.cycle_id, c.state, p.average_entry_price, p.quantity, c.updated_at "
                "FROM position_cycles c LEFT JOIN positions p "
                "ON p.account_scope_id = c.account_scope_id AND p.cycle_id = c.cycle_id "
                "WHERE c.account_scope_id = ? AND c.symbol = ? AND c.entry_strategy_key = ? "
                "AND c.state IN ('PENDING_ENTRY','OPEN','EXITING') "
                "ORDER BY c.updated_at DESC LIMIT 1", (scope, symbol, strategy_key)).fetchone()
            if entry is not None:
                return StrategyInstanceState(
                    instance_id=instance_id, role=role, strategy_key=strategy_key,
                    strategy_version=version, enabled=enabled, engine_state=engine_state_value,
                    armed=bool(state_row[2]) if state_row else None,
                    rearm_required=bool(state_row[3]) if state_row else None,
                    stage="ORDER_FILLED",
                    stage_detail=(f"cycle {entry[1]} qty {entry[4] if entry[4] is not None else '—'}"
                                  + (f" @ {entry[2]}" if entry[2] is not None else "")),
                    stage_as_of=_dt(entry[4]),
                    last_signal_bar=_iso(_json_dict(state_row[1]).get("last_signal_bar"))
                    if state_row else None)

        # 3. the instance's latest signal and its recorded decision
        signal_row = q(
            "SELECT s.signal_id, s.action, s.reference_price, s.reason, s.generated_at, "
            "d.outcome, d.reason FROM signals s LEFT JOIN decisions d "
            "ON d.account_scope_id = s.account_scope_id AND d.signal_id = s.signal_id "
            "WHERE s.account_scope_id = ? AND s.instance_id = ? "
            "ORDER BY s.generated_at DESC LIMIT 1", (scope, instance_id)).fetchone()
        if signal_row is not None:
            summary = self._signal_model(signal_row)
            if summary.disposition == "REJECTED":
                return StrategyInstanceState(
                    instance_id=instance_id, role=role, strategy_key=strategy_key,
                    strategy_version=version, enabled=enabled, engine_state=engine_state_value,
                    armed=bool(state_row[2]) if state_row else None,
                    rearm_required=bool(state_row[3]) if state_row else None,
                    stage="SIGNAL_REJECTED",
                    stage_detail=summary.disposition_reason or summary.reason,
                    stage_as_of=summary.generated_at,
                    last_signal_bar=_iso(_json_dict(state_row[1]).get("last_signal_bar"))
                    if state_row else None)
            return StrategyInstanceState(
                instance_id=instance_id, role=role, strategy_key=strategy_key,
                strategy_version=version, enabled=enabled, engine_state=engine_state_value,
                armed=bool(state_row[2]) if state_row else None,
                rearm_required=bool(state_row[3]) if state_row else None,
                stage="SIGNAL_PRODUCED",
                stage_detail=f"{summary.action} @ {summary.reference_price} ({summary.reason})"
                if summary.reference_price is not None else summary.reason,
                stage_as_of=summary.generated_at,
                last_signal_bar=_iso(_json_dict(state_row[1]).get("last_signal_bar"))
                if state_row else None)

        # 4. the instance's latest evaluation (NO SIGNAL is not a rejection)
        eval_row = q(
            "SELECT e.evaluated_at, e.status, e.reason FROM strategy_evaluations e "
            "WHERE e.account_scope_id = ? AND e.instance_id = ? "
            "ORDER BY e.evaluated_at DESC LIMIT 1", (scope, instance_id)).fetchone()
        if eval_row is not None:
            status = str(eval_row[1])
            stage = "NO_SIGNAL" if status.upper() in ("NO_SIGNAL", "NO-SIGNAL") else f"EVALUATED_{status}"
            return StrategyInstanceState(
                instance_id=instance_id, role=role, strategy_key=strategy_key,
                strategy_version=version, enabled=enabled, engine_state=engine_state_value,
                armed=bool(state_row[2]) if state_row else None,
                rearm_required=bool(state_row[3]) if state_row else None,
                stage=stage, stage_detail=eval_row[2], stage_as_of=_dt(eval_row[0]),
                last_signal_bar=_iso(_json_dict(state_row[1]).get("last_signal_bar"))
                if state_row else None)

        # 5. recorded engine state (e.g. WATCHING_BUY) — carries its as-of time
        if engine_state_value is not None:
            return StrategyInstanceState(
                instance_id=instance_id, role=role, strategy_key=strategy_key,
                strategy_version=version, enabled=enabled, engine_state=engine_state_value,
                armed=bool(state_row[2]) if state_row else None,
                rearm_required=bool(state_row[3]) if state_row else None,
                stage=engine_state_value, stage_detail="last recorded engine state",
                stage_as_of=stage_as_of,
                last_signal_bar=_iso(_json_dict(state_row[1]).get("last_signal_bar"))
                if state_row else None)

        # 6. nothing recorded for this instance
        return StrategyInstanceState(
            instance_id=instance_id, role=role, strategy_key=strategy_key,
            strategy_version=version, enabled=enabled, engine_state=None, armed=None,
            rearm_required=None, stage="NOT_EVALUATED", stage_detail=None, stage_as_of=None,
            last_signal_bar=None)

    # ---- /activity (operational event timeline) -------------------------------

    _DOMAIN_CATEGORIES: Mapping[str, str] = {
        "RunConfigRecorded": "SYSTEM",
        "ShadowAccountCreated": "SYSTEM",
        "StrategyStateSaved": "STRATEGY",
        "ShadowOrderSubmitted": "ORDER",
        "ShadowOrderReplaced": "ORDER",
        "ShadowOrderCanceled": "ORDER",
        "ShadowFillRecorded": "FILL",
    }

    @staticmethod
    def _domain_category(event_type: str) -> str:
        known = DashboardReadService._DOMAIN_CATEGORIES.get(event_type)
        if known is not None:
            return known
        upper = event_type.upper()
        if "FILL" in upper:
            return "FILL"
        if "ORDER" in upper:
            return "ORDER"
        if "SIGNAL" in upper:
            return "SIGNAL"
        if "PROTECT" in upper or "STOP" in upper:
            return "PROTECTION"
        if "CYCLE" in upper or "POSITION" in upper:
            return "LIFECYCLE"
        if "STRATEGY" in upper or "STATE" in upper:
            return "STRATEGY"
        return "SYSTEM"

    @staticmethod
    def _domain_summary(event_type: str, data: Mapping[str, Any], state: Mapping[str, Any],
                        stream_hint: Mapping[str, str] | None = None) -> str:
        purpose = str(data.get("purpose") or "").upper()
        side = str(data.get("side") or "").upper()
        quantity = data.get("quantity")
        symbol = data.get("symbol")
        price = data.get("price")
        stop = data.get("stop_price")
        order_type = str(data.get("order_type") or "").upper()
        strategy = data.get("strategy_key")
        hint = stream_hint or {}
        if event_type == "ShadowOrderSubmitted":
            label = {"ENTRY": "Entry order", "PROTECTIVE_STOP": "Protective stop order",
                     "STRATEGY_EXIT": "Exit order", "EXIT": "Exit order"}.get(purpose, "Order")
            parts = [label, side, str(quantity or ""), str(symbol or ""), order_type]
            if stop is not None:
                parts.append(f"stop {stop}")
            if strategy:
                parts.append(f"strategy {strategy}")
            return " ".join(part for part in parts if part)
        if event_type == "ShadowFillRecorded":
            return (f"Fill {side} {quantity} {symbol} @ {price}"
                    + (f" (fee {data.get('fee')})" if data.get("fee") is not None else ""))
        if event_type == "ShadowOrderReplaced":
            record = data.get("shadow_stop_record")
            if stop is not None:
                return f"Protective stop replaced — stop now {stop}"
            if isinstance(record, dict) and record.get("covered_quantity") is not None:
                return (f"Protective stop record updated (covered {record.get('covered_quantity')}"
                        + (f", stop {record.get('stop_price')}"
                           if record.get("stop_price") is not None else "") + ")")
            return "Order replaced"
        if event_type == "ShadowOrderCanceled":
            return f"Order canceled ({side} {symbol})".replace(" (None None)", "").replace(" ()", "")
        if event_type == "StrategyStateSaved":
            eligibility = state.get("eligibility")
            subject = " ".join(part for part in (hint.get("strategy_key"), hint.get("role"),
                                                 hint.get("symbol")) if part)
            if eligibility is not None:
                return f"{subject}: engine state {eligibility}".strip(": ") if subject \
                    else f"Engine strategy state: {eligibility}"
            return f"{subject}: engine strategy state saved" if subject \
                else "Engine strategy state saved"
        if event_type == "RunConfigRecorded":
            return "Run configuration recorded"
        if event_type == "ShadowAccountCreated":
            return "Shadow account created"
        return event_type

    @staticmethod
    def _stream_hint(scope: str, stream_type: str, stream_id: str) -> dict[str, str]:
        """Symbol/strategy/role parsed from the event stream id when present."""
        hint: dict[str, str] = {}
        if stream_type == "strategy_state" and stream_id.startswith(f"{scope}:"):
            parts = stream_id[len(scope) + 1:].split(":")
            # <pair>:<role>:<symbol>[:<symbol>] (strategy_state streams)
            if len(parts) >= 3:
                hint["pair"] = parts[0]
                hint["role"] = parts[1].upper()
                hint["strategy_key"] = parts[0]
                hint["symbol"] = parts[2]
        return hint

    def activity(
        self, account_id: str, *, symbol: str | None = None, category: str | None = None,
        strategy: str | None = None, since: datetime | None = None,
        until: datetime | None = None, before: datetime | None = None, limit: int = 100,
    ) -> ActivityResponse:
        fetch = max(1, min(int(limit), 500))
        symbol = symbol.strip().upper() if symbol else None
        category = category.strip().upper() if category else None
        strategy = strategy.strip() if strategy else None
        with self._open(account_id) as ctx:
            f = self._facts(ctx)
            entries = self._domain_activity(
                ctx, fetch, symbol=symbol, since=since, until=until, before=before)
            entries += self._table_activity(
                ctx, fetch, symbol=symbol, since=since, until=until, before=before)
        entries.sort(key=lambda entry: (entry.at, entry.event_type), reverse=True)
        if category is not None:
            entries = [e for e in entries if e.category == category]
        if strategy is not None:
            entries = [e for e in entries
                       if (e.strategy_key or "").lower() == strategy.lower()]
        entries = entries[:fetch]
        next_before = entries[-1].at if len(entries) >= fetch else None
        return ActivityResponse(
            account_id=ctx.scope, mode=ctx.mode, engine_state=f.state, server_time=ctx.now,
            entries=entries, next_before=next_before)

    def _domain_activity(
        self, ctx: _Ctx, fetch: int, *, symbol: str | None, since: datetime | None,
        until: datetime | None, before: datetime | None,
    ) -> list[ActivityEntry]:
        sql = ("SELECT event_seq, event_type, occurred_at, stream_type, stream_id, payload_json "
               "FROM domain_events WHERE account_scope_id = ?")
        params: list[object] = [ctx.scope]
        if symbol is not None:
            sql += (" AND (json_extract(payload_json, '$.data.symbol') = ?"
                    " OR json_extract(payload_json, '$.data.cycle_id') LIKE ?"
                    " OR stream_id LIKE ?)")
            params += [symbol, f"{symbol}-%", f"%:{symbol}%"]
        if since is not None:
            sql += " AND occurred_at >= ?"
            params.append(schema.to_db_timestamp(_utc(since)))
        if until is not None:
            sql += " AND occurred_at <= ?"
            params.append(schema.to_db_timestamp(_utc(until)))
        if before is not None:
            sql += " AND occurred_at < ?"
            params.append(schema.to_db_timestamp(_utc(before)))
        sql += " ORDER BY occurred_at DESC, event_seq DESC LIMIT ?"
        params.append(fetch)
        entries: list[ActivityEntry] = []
        for seq, event_type, occurred, stream_type, stream_id, payload_text in ctx.conn.execute(
                sql, params).fetchall():
            payload = _json_dict(payload_text)
            raw_data = payload.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            raw_state = payload.get("state")
            state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
            hint = self._stream_hint(ctx.scope, str(stream_type), str(stream_id))
            # symbol resolution order: payload data, cycle id prefix, stream id, filter
            symbol_value: str | None = None
            if data.get("symbol"):
                symbol_value = str(data["symbol"]).upper()
            elif data.get("cycle_id"):
                symbol_value = str(data["cycle_id"]).split("-", 1)[0].upper()
            elif data.get("broker_order_id"):
                marker = re.search(r"-stop-([A-Z]{1,6})-", str(data["broker_order_id"]))
                symbol_value = marker.group(1) if marker else None
            elif hint.get("symbol"):
                symbol_value = hint["symbol"].upper()
            strategy_value = data.get("strategy_key")
            role_value = data.get("role") or hint.get("role")
            if not strategy_value and stream_type == "strategy_state" and stream_id:
                # resolve the pair/role stream to the registered strategy instance;
                # strategy_state stream ids carry the symbol twice (<instance>:<symbol>)
                candidates = [str(stream_id)]
                stripped = str(stream_id).rsplit(":", 1)[0]
                if stripped and stripped not in candidates:
                    candidates.append(stripped)
                inst = ctx.conn.execute(
                    "SELECT strategy_key, role, symbol FROM strategy_instances "
                    f"WHERE account_scope_id = ? AND instance_id IN "
                    f"({', '.join('?' for _ in candidates)})",
                    (ctx.scope, *candidates)).fetchone()
                if inst is not None:
                    strategy_value, role_value, inst_symbol = inst[0], inst[1], inst[2]
                    symbol_value = symbol_value or (str(inst_symbol).upper() if inst_symbol else None)
            ref_id = (data.get("broker_order_id") or data.get("fill_id")
                      or data.get("signal_id") or (str(seq) if not data else None))
            detail_payload: dict[str, Any] = {"stream_type": stream_type, "stream_id": stream_id}
            if data:
                detail_payload["data"] = {k: v for k, v in data.items() if k != "assumptions"}
            if state:
                detail_payload["state"] = state
            entries.append(ActivityEntry(
                at=_req(occurred),
                category=self._domain_category(str(event_type)),  # type: ignore[arg-type]
                event_type=str(event_type),
                symbol=symbol_value,
                strategy_key=strategy_value,
                strategy_role=role_value,
                cycle_id=data.get("cycle_id"), ref_id=ref_id,
                side=data.get("side"), quantity=_dec(data.get("quantity")),
                price=_dec(data.get("price")), stop_price=_dec(data.get("stop_price")),
                disposition=None,
                summary=self._domain_summary(
                    str(event_type), data, state,
                    {"strategy_key": str(strategy_value), "role": str(role_value or ""),
                     "symbol": str(symbol_value or "")}
                if stream_type == "strategy_state" else hint),
                payload=detail_payload,
            ))
        return entries

    def _table_activity(
        self, ctx: _Ctx, fetch: int, *, symbol: str | None, since: datetime | None,
        until: datetime | None, before: datetime | None,
    ) -> list[ActivityEntry]:
        """Events from the structured ledger tables (populated on broker modes)."""
        entries: list[ActivityEntry] = []
        scope = ctx.scope

        def windowed(sql: str, params: list[object], at_expr: str) -> tuple[str, list[object]]:
            if since is not None:
                sql += f" AND {at_expr} >= ?"
                params.append(schema.to_db_timestamp(_utc(since)))
            if until is not None:
                sql += f" AND {at_expr} <= ?"
                params.append(schema.to_db_timestamp(_utc(until)))
            if before is not None:
                sql += f" AND {at_expr} < ?"
                params.append(schema.to_db_timestamp(_utc(before)))
            return sql, params

        # signals + recorded decisions
        sql = ("SELECT s.generated_at, s.signal_id, s.symbol, s.action, s.reason, "
               "i.strategy_key, i.role, d.outcome, d.reason FROM signals s "
               "LEFT JOIN decisions d ON d.account_scope_id = s.account_scope_id "
               "AND d.signal_id = s.signal_id "
               "LEFT JOIN strategy_instances i ON i.account_scope_id = s.account_scope_id "
               "AND i.instance_id = s.instance_id WHERE s.account_scope_id = ?")
        params: list[object] = [scope]
        if symbol is not None:
            sql += " AND s.symbol = ?"
            params.append(symbol)
        sql, params = windowed(sql, params, "s.generated_at")
        sql += " ORDER BY s.generated_at DESC LIMIT ?"
        params.append(fetch)
        for at, signal_id, sym, action, reason, key, role, outcome, dreason in ctx.conn.execute(
                sql, params).fetchall():
            disposition = ("UNDECIDED" if outcome is None
                           else "ACCEPTED" if str(outcome).upper() in _ACCEPTED_OUTCOMES
                           else "REJECTED")
            entries.append(ActivityEntry(
                at=_req(at), category="SIGNAL", event_type=f"SIGNAL_{disposition}",
                symbol=sym, strategy_key=key, strategy_role=role, ref_id=signal_id,
                side=action, disposition=disposition,
                summary=(f"Signal {action} {sym} — {disposition.lower()}"
                         + (f": {dreason or reason}" if (dreason or reason) else "")),
                payload={"signal_id": signal_id, "action": action, "reason": reason,
                         "decision_outcome": outcome, "decision_reason": dreason}))

        # strategy evaluations with an explicit recorded status
        sql = ("SELECT e.evaluated_at, e.status, e.reason, e.symbol, e.bar_time, "
               "i.strategy_key, i.role FROM strategy_evaluations e "
               "LEFT JOIN strategy_instances i ON i.account_scope_id = e.account_scope_id "
               "AND i.instance_id = e.instance_id WHERE e.account_scope_id = ?")
        params = [scope]
        if symbol is not None:
            sql += " AND e.symbol = ?"
            params.append(symbol)
        sql, params = windowed(sql, params, "e.evaluated_at")
        sql += " ORDER BY e.evaluated_at DESC LIMIT ?"
        params.append(fetch)
        for at, status, reason, sym, bar_time, key, role in ctx.conn.execute(sql, params).fetchall():
            entries.append(ActivityEntry(
                at=_req(at), category="EVALUATION", event_type=f"EVALUATION_{status}",
                symbol=sym, strategy_key=key, strategy_role=role,
                summary=(f"{key or 'Strategy'} evaluated {sym or ''}: {status}"
                         + (f" ({reason})" if reason else "")),
                payload={"status": status, "reason": reason, "bar_time": bar_time}))

        # fills
        sql = ("SELECT filled_at, fill_id, symbol, side, quantity, price, fee, cycle_id "
               "FROM fills WHERE account_scope_id = ?")
        params = [scope]
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        sql, params = windowed(sql, params, "filled_at")
        sql += " ORDER BY filled_at DESC LIMIT ?"
        params.append(fetch)
        for at, fill_id, sym, side, quantity, price, fee, cycle_id in ctx.conn.execute(
                sql, params).fetchall():
            entries.append(ActivityEntry(
                at=_req(at), category="FILL", event_type="FILL_RECORDED", symbol=sym,
                cycle_id=cycle_id, ref_id=fill_id, side=side, quantity=_dec(quantity),
                price=_dec(price),
                summary=f"Fill {side} {quantity} {sym} @ {price} (fee {fee})",
                payload={"fill_id": fill_id, "fee": fee, "cycle_id": cycle_id}))

        # broker order events (status transitions)
        sql = ("SELECT e.occurred_at, e.broker_order_id, e.event_type, e.status, "
               "b.symbol, b.side, b.quantity FROM broker_order_events e "
               "LEFT JOIN broker_orders b ON b.account_scope_id = e.account_scope_id "
               "AND b.broker_order_id = e.broker_order_id "
               "WHERE e.account_scope_id = ?")
        params = [scope]
        if symbol is not None:
            sql += " AND b.symbol = ?"
            params.append(symbol)
        sql, params = windowed(sql, params, "e.occurred_at")
        sql += " ORDER BY e.occurred_at DESC LIMIT ?"
        params.append(fetch)
        for at, order_id, event_type, status, sym, side, quantity in ctx.conn.execute(
                sql, params).fetchall():
            entries.append(ActivityEntry(
                at=_req(at), category="ORDER", event_type=str(event_type or "ORDER_EVENT"),
                symbol=sym, ref_id=order_id, side=side, quantity=_dec(quantity),
                summary=f"Order {order_id[:24]}… → {status or event_type}",
                payload={"status": status, "broker_order_id": order_id}))

        # trailing-stop / protection events
        sql = ("SELECT t.occurred_at, t.event_type, t.previous_stop_price, t.new_stop_price, "
               "c.symbol, t.cycle_id FROM trailing_stop_events t "
               "LEFT JOIN position_cycles c ON c.account_scope_id = t.account_scope_id "
               "AND c.cycle_id = t.cycle_id WHERE t.account_scope_id = ?")
        params = [scope]
        if symbol is not None:
            sql += " AND c.symbol = ?"
            params.append(symbol)
        sql, params = windowed(sql, params, "t.occurred_at")
        sql += " ORDER BY t.occurred_at DESC LIMIT ?"
        params.append(fetch)
        for at, event_type, previous, new, sym, cycle_id in ctx.conn.execute(sql, params).fetchall():
            entries.append(ActivityEntry(
                at=_req(at), category="PROTECTION",
                event_type=str(event_type or "TRAILING_STOP_EVENT"),
                symbol=sym, cycle_id=cycle_id, stop_price=_dec(new),
                summary=(f"Protective stop {sym or ''} {previous or '—'} → {new or '—'}").strip(),
                payload={"previous_stop_price": previous, "new_stop_price": new,
                         "cycle_id": cycle_id}))

        return entries

    # ---- /symbols/{symbol} ---------------------------------------------------------

    def symbol_detail(self, account_id: str, symbol: str) -> SymbolDetailResponse:
        sym = symbol.strip().upper()
        with self._open(account_id) as ctx:
            f = self._facts(ctx)
            q = ctx.conn.execute
            timeline: list[TimelineEntry] = []
            for r in q(
                "SELECT e.evaluated_at, e.status, e.reason, e.bar_time, i.strategy_key, i.role "
                "FROM strategy_evaluations e LEFT JOIN strategy_instances i "
                "ON i.account_scope_id = e.account_scope_id AND i.instance_id = e.instance_id "
                "WHERE e.account_scope_id = ? AND e.symbol = ? "
                "ORDER BY e.evaluated_at DESC LIMIT 100", (ctx.scope, sym)).fetchall():
                timeline.append(TimelineEntry(
                    kind="EVALUATION", at=_req(r[0]), status=r[1], reason=r[2],
                    strategy_key=r[4], role=r[5], bar_time=_dt(r[3])))
            signal_rows = q(
                "SELECT s.signal_id, s.action, s.reference_price, s.reason, s.generated_at, "
                "d.outcome, d.reason, s.provenance_json, s.bar_time, i.strategy_key, i.role "
                "FROM signals s LEFT JOIN decisions d "
                "ON d.account_scope_id = s.account_scope_id AND d.signal_id = s.signal_id "
                "LEFT JOIN strategy_instances i ON i.account_scope_id = s.account_scope_id "
                "AND i.instance_id = s.instance_id "
                "WHERE s.account_scope_id = ? AND s.symbol = ? "
                "ORDER BY s.generated_at DESC LIMIT 100", (ctx.scope, sym)).fetchall()
            evidence: list[EvidenceEntry] = []
            for r in signal_rows:
                summary = self._signal_model(r)
                timeline.append(TimelineEntry(
                    kind="SIGNAL", at=summary.generated_at, status=summary.disposition,
                    reason=summary.disposition_reason or summary.reason, strategy_key=r[9],
                    role=r[10], bar_time=_dt(r[8]), action=summary.action,
                    reference_price=summary.reference_price, disposition=summary.disposition))
                if len(evidence) < 20:
                    evidence.append(EvidenceEntry(
                        signal_id=summary.signal_id, generated_at=summary.generated_at,
                        provenance=_json_dict(r[7])))
            timeline.sort(key=lambda item: item.at, reverse=True)
            bar = self._latest_bar(ctx, sym)
            feed_name = str(bar[2]) if bar else None
            feed_prov = FeedProvenance(
                feed=feed_name, provenance=bar[3] if bar else None,
                consolidated=False if feed_name and feed_name.lower() == "iex" else None,
                last_bar_at=_dt(bar[0]) if bar else None,
                recorded_at=_dt(bar[4]) if bar else None)
            order_tree = self._order_tree(ctx, sym)
            history = self._protection_history(ctx, sym)
            cycles = self._managed_cycles(ctx, sym)
        reason = f.trust.reason()
        return SymbolDetailResponse(
            account_id=ctx.scope, mode=ctx.mode, symbol=sym, server_time=ctx.now,
            fresh=reason is None, stale_reason=reason, timeline=timeline[:100],
            evidence=evidence, feed_provenance=feed_prov, order_tree=order_tree,
            protection_history=history, managed_cycles=cycles)

    def _order_tree(self, ctx: _Ctx, symbol: str) -> list[CycleOrderTree]:
        rows = ctx.conn.execute(
            "SELECT c.cycle_id, c.state, o.broker_order_id, o.client_order_id, o.side, "
            "o.order_type, o.status, o.quantity, o.filled_quantity, o.filled_avg_price, "
            "o.limit_price, o.stop_price, o.submitted_at, o.updated_at, n.purpose "
            "FROM position_cycles c JOIN order_intents n "
            "ON n.account_scope_id = c.account_scope_id AND n.cycle_id = c.cycle_id "
            "JOIN broker_orders o ON o.account_scope_id = n.account_scope_id "
            "AND o.client_order_id = n.client_order_id "
            "WHERE c.account_scope_id = ? AND c.symbol = ? "
            "ORDER BY c.created_at DESC, o.submitted_at ASC LIMIT 200", (ctx.scope, symbol),
        ).fetchall()
        cycles: dict[str, tuple[str, list[OrderNode]]] = {}
        entry_order: dict[str, str] = {}
        for r in rows:
            if r[14] == "ENTRY" and r[0] not in entry_order:
                entry_order[r[0]] = r[2]
        for r in rows:
            parent = entry_order.get(r[0]) if r[14] == "PROTECTIVE_STOP" else None
            node = OrderNode(
                broker_order_id=r[2], client_order_id=r[3], parent_broker_order_id=parent,
                purpose=r[14], side=r[4], order_type=r[5], status=r[6],
                quantity=schema.decimal_from_db(r[7]),
                filled_quantity=schema.decimal_from_db(r[8]), filled_avg_price=_dec(r[9]),
                limit_price=_dec(r[10]), stop_price=_dec(r[11]), submitted_at=_req(r[12]),
                updated_at=_req(r[13]))
            cycles.setdefault(r[0], (r[1], []))[1].append(node)
        return [CycleOrderTree(cycle_id=cid, state=state, orders=nodes)
                for cid, (state, nodes) in cycles.items()]

    def _protection_history(self, ctx: _Ctx, symbol: str) -> list[ProtectionEvent]:
        rows = ctx.conn.execute(
            "SELECT e.occurred_at, e.cycle_id, e.event_type, e.previous_stop_price, "
            "e.new_stop_price, e.payload_json FROM trailing_stop_events e "
            "JOIN position_cycles c ON c.account_scope_id = e.account_scope_id "
            "AND c.cycle_id = e.cycle_id WHERE e.account_scope_id = ? AND c.symbol = ? "
            "ORDER BY e.occurred_at DESC LIMIT 200", (ctx.scope, symbol),
        ).fetchall()
        events = []
        for r in rows:
            payload = _json_dict(r[5])
            kind = str(r[2]).upper()
            new = _dec(r[4])
            proposed = _dec(payload.get("proposed_stop_price"))
            confirmed = _dec(payload.get("confirmed_stop_price"))
            if proposed is None and any(w in kind for w in ("PROPOS", "REQUEST")):
                proposed = new
            if confirmed is None and "CONFIRM" in kind:
                confirmed = new
            events.append(ProtectionEvent(
                at=_req(r[0]), cycle_id=r[1], event_type=r[2], previous_stop_price=_dec(r[3]),
                new_stop_price=new, proposed_stop_price=proposed, confirmed_stop_price=confirmed,
                payload=payload))
        return events

    def _managed_cycles(self, ctx: _Ctx, symbol: str) -> list[ManagedCycle]:
        q = ctx.conn.execute
        trades = {t.cycle_id: t for t in self._trades(ctx)}
        rows = q(
            "SELECT cycle_id, state, entry_strategy_key, opened_at, closed_at FROM position_cycles "
            "WHERE account_scope_id = ? AND symbol = ? AND state IN "
            "('OPEN','EXITING','CLOSED') ORDER BY COALESCE(closed_at, updated_at) DESC LIMIT 20",
            (ctx.scope, symbol)).fetchall()
        result = []
        for cycle_id, state, strategy, opened_s, closed_s in rows:
            opened, closed = _dt(opened_s), _dt(closed_s)
            refs: dict[str, Decimal | None] = {}
            for purpose_group, key in (("ENTRY", "entry"),
                                       ("STRATEGY_EXIT", "exit")):
                ref = q(
                    "SELECT s.reference_price FROM order_intents n JOIN signals s "
                    "ON s.account_scope_id = n.account_scope_id AND s.signal_id = n.signal_id "
                    "WHERE n.account_scope_id = ? AND n.cycle_id = ? AND n.purpose = ? "
                    "ORDER BY n.created_at LIMIT 1", (ctx.scope, cycle_id, purpose_group),
                ).fetchone()
                refs[key] = _dec(ref[0]) if ref else None
            avg: dict[str, Decimal | None] = {}
            for sides, key in ((("BUY", "SELL_SHORT"), "entry"), (("SELL", "BUY_TO_COVER"), "exit")):
                fills = q("SELECT quantity, price FROM fills WHERE account_scope_id = ? "
                          "AND cycle_id = ? AND side IN (?, ?)",
                          (ctx.scope, cycle_id, *sides)).fetchall()
                qty = sum((schema.decimal_from_db(x[0]) for x in fills), _ZERO)
                avg[key] = (sum((schema.decimal_from_db(x[0]) * schema.decimal_from_db(x[1])
                                 for x in fills), _ZERO) / qty) if qty else None
            reasons = [r[0] for r in q(
                "SELECT d.reason FROM decisions d JOIN order_intents n "
                "ON n.account_scope_id = d.account_scope_id AND n.signal_id = d.signal_id "
                "WHERE d.account_scope_id = ? AND n.cycle_id = ? ORDER BY d.decided_at",
                (ctx.scope, cycle_id)).fetchall()]
            trade = trades.get(cycle_id)
            result.append(ManagedCycle(
                cycle_id=cycle_id, state=state, entry_strategy_key=strategy, opened_at=opened,
                closed_at=closed,
                holding_seconds=int((closed - opened).total_seconds()) if opened and closed
                else None,
                realized_pnl=trade.realized_pnl if trade else None,
                entry_reference_price=refs["entry"], entry_average_price=avg["entry"],
                entry_slippage=(avg["entry"] - refs["entry"]
                                if avg["entry"] is not None and refs["entry"] is not None
                                else None),
                exit_reference_price=refs["exit"], exit_average_price=avg["exit"],
                exit_slippage=(refs["exit"] - avg["exit"]
                               if avg["exit"] is not None and refs["exit"] is not None else None),
                decision_reasons=reasons))
        return result

    # ---- /detail -------------------------------------------------------------------

    def detail(self, account_id: str) -> DetailResponse:
        with self._open(account_id) as ctx:
            f = self._facts(ctx)
            q = ctx.conn.execute
            reason = f.trust.reason("broker")
            positions = [
                PositionRow(
                    symbol=p.symbol, cycle_id=p.cycle_id, management=p.management,
                    direction=p.direction, quantity=p.quantity,
                    average_entry_price=p.average_entry_price, market_price=p.market_price,
                    market_value=p.market_value, unrealized_pl=p.unrealized_pl, as_of=p.as_of,
                    fresh=reason is None, stale_reason=reason)
                for p in self._positions(ctx)]
            snap = q("SELECT cash, equity, buying_power, portfolio_value, source, as_of "
                     "FROM equity_snapshots WHERE account_scope_id = ? "
                     "ORDER BY as_of DESC LIMIT 1", (ctx.scope,)).fetchone()
            snap_at = _dt(snap[5]) if snap else None
            snap_reason = reason or (
                "snapshot_stale" if snap_at is not None and ctx.now - snap_at > timedelta(
                    seconds=self._settings.snapshot_stale_seconds) else None)
            if snap is None:
                balances = Balances(cash=None, equity=None, buying_power=None,
                                    portfolio_value=None, source=None, as_of=None, fresh=False,
                                    stale_reason="unavailable")
            else:
                balances = Balances(
                    cash=_dec(snap[0]), equity=_dec(snap[1]), buying_power=_dec(snap[2]),
                    portfolio_value=_dec(snap[3]), source=snap[4], as_of=snap_at,
                    fresh=snap_reason is None, stale_reason=snap_reason)
            orders = [
                OrderRow(
                    broker_order_id=o.broker_order_id, client_order_id=o.client_order_id,
                    symbol=o.symbol, side=o.side, order_type=o.order_type, status=o.status,
                    quantity=o.quantity, filled_quantity=o.filled_quantity,
                    submitted_at=o.submitted_at, updated_at=o.updated_at, is_open=o.is_open,
                    fresh=reason is None, stale_reason=reason)
                for o in projections.orders(ctx.conn, ctx.scope, now=ctx.now, limit=100)]
            fees = sum((schema.decimal_from_db(r[0]) for r in q(
                "SELECT fee FROM fills WHERE account_scope_id = ?", (ctx.scope,)).fetchall()),
                _ZERO)
            activity: list[ActivityRow] = []
            for r in q("SELECT activity_type, payload_json, occurred_at FROM broker_activities "
                       "WHERE account_scope_id = ? ORDER BY occurred_at DESC LIMIT 50",
                       (ctx.scope,)).fetchall():
                payload = _json_dict(r[1])
                activity.append(ActivityRow(
                    kind="BROKER_ACTIVITY", activity_type=r[0], occurred_at=_req(r[2]),
                    amount=_dec(payload.get("amount") or payload.get("net_amount")),
                    description=None, payload=payload))
            for r in q("SELECT event_type, amount, description, occurred_at FROM cash_events "
                       "WHERE account_scope_id = ? ORDER BY occurred_at DESC LIMIT 50",
                       (ctx.scope,)).fetchall():
                activity.append(ActivityRow(
                    kind="CASH_EVENT", activity_type=r[0], occurred_at=_req(r[3]),
                    amount=_dec(r[1]), description=r[2], payload={}))
            activity.sort(key=lambda a: a.occurred_at, reverse=True)
            recon = projections.reconciliation_status(ctx.conn, ctx.scope, now=ctx.now)
            drifts = [
                DriftRow(drift_id=r[0], drift_type=r[1], entity_ref=r[2],
                         expected=_json_dict(r[3]) or r[3], actual=_json_dict(r[4]) or r[4],
                         detected_at=_req(r[5]))
                for r in q(
                    "SELECT d.drift_id, d.drift_type, d.entity_ref, d.expected_json, "
                    "d.actual_json, d.detected_at FROM reconciliation_drifts d "
                    "LEFT JOIN reconciliation_resolutions x "
                    "ON x.account_scope_id = d.account_scope_id AND x.drift_id = d.drift_id "
                    "WHERE d.account_scope_id = ? AND x.resolution_id IS NULL "
                    "ORDER BY d.detected_at DESC LIMIT 100", (ctx.scope,)).fetchall()]
        return DetailResponse(
            account_id=ctx.scope, mode=ctx.mode, server_time=ctx.now, balances=balances,
            positions=[p for p in positions if p.management != "UNMANAGED"],
            external_holdings=[p for p in positions if p.management == "UNMANAGED"],
            orders=orders, total_fees=fees, activity=activity[:100],
            reconciliation=ReconciliationDetail(
                last_run_at=recon.last_run_at, last_result=recon.last_result,
                unresolved_drift_count=len(recon.unresolved_drift_items), drifts=drifts))

    # ---- /health -------------------------------------------------------------------

    def health(self, account_id: str) -> HealthResponse:
        s = self._settings
        with self._open(account_id) as ctx:
            f = self._facts(ctx)
            v = _Values(f.trust)
            scheduler = self._health_row(ctx, "scheduler")
            recon = projections.reconciliation_status(ctx.conn, ctx.scope, now=ctx.now)
            managed = self._managed(self._positions(ctx))
            protections = projections.protections(ctx.conn, ctx.scope, now=ctx.now, limit=500)
            issues = self._protection_issues(managed, protections)
            version_row = ctx.conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
            identity = ctx.conn.execute(
                "SELECT database_id FROM db_identity WHERE id = 1").fetchone()

        def detail_of(link: _Link, key: str) -> int | None:
            return _int(link.details.get(key))

        def counter(link: _Link, key: str, *needs: str) -> Sourced[Any]:
            value = detail_of(link, key)
            if value is None:
                return v.unavailable(link.provenance)
            return v.of(value, *needs, as_of=link.as_of, provenance=link.provenance)

        feed_age = (v.of(_seconds(f.feed.age), "feed", as_of=f.feed.as_of,
                         provenance=f.feed.provenance)
                    if f.feed.age is not None else v.unavailable(f.feed.provenance))
        lag_value = _dec(scheduler["details"].get("lag_seconds")) if scheduler else None
        lag = (v.of(float(lag_value), "engine", as_of=scheduler["heartbeat_at"],
                    provenance="service_health:scheduler")
               if lag_value is not None and scheduler
               else v.unavailable("service_health:scheduler"))
        backup_at, backup_ok = self._last_backup(identity[0] if identity else None)
        size = wal = None
        try:
            size = self._db_path.stat().st_size
            wal_path = Path(f"{self._db_path}-wal")
            wal = wal_path.stat().st_size if wal_path.exists() else 0
        except OSError:
            pass
        persistence = PersistenceHealth(
            database_path=str(self._db_path), database_id=identity[0] if identity else None,
            schema_version=int(version_row[0]) if version_row else None, size_bytes=size,
            wal_size_bytes=wal,
            last_backup_at=(Sourced(value=backup_at, as_of=backup_at,
                                    fresh=backup_ok is True and ctx.now - backup_at <= timedelta(hours=36),
                                    stale_reason=("integrity_failed" if not backup_ok else
                                                  "backup_stale" if ctx.now - backup_at > timedelta(hours=36)
                                                  else None),
                                    provenance="backup_manifest")
                            if backup_at else v.unavailable("backup_manifest")),
            last_backup_integrity_ok=backup_ok)

        errors: list[ActionableError] = []

        def add(code: str, severity: Literal["critical", "warning", "info"], message: str) -> None:
            errors.append(ActionableError(code=code, severity=severity, message=message,
                                          command=OPERATOR_COMMANDS[code]))

        if f.state == "OFFLINE":
            seconds = None if f.heartbeat_age is None else int(f.heartbeat_age.total_seconds())
            age = "no heartbeat" if seconds is None else f"{seconds}s old"
            add("ENGINE_OFFLINE", "critical", f"Engine heartbeat is missing or stale ({age}).")
        elif f.state == "DEGRADED":
            add("ENGINE_DEGRADED", "warning", "Engine reports DEGRADED; new entries are blocked.")
        last_error = f.engine_details.get("last_error")
        if last_error and f.state != "OFFLINE":
            add("ENGINE_ERROR", "warning", f"Engine last error: {last_error}")
        if f.state != "OFFLINE" and f.market == "OPEN" and not f.feed.fresh:
            if f.feed.connected is False:
                add("FEED_DISCONNECTED", "critical", "Market data feed is disconnected.")
            elif f.feed.reason != "unavailable":
                add("FEED_STALE", "warning", "Market data is stale while the market is open.")
        if ctx.mode in _BROKER_MODES and f.state != "OFFLINE" and not f.broker.fresh:
            if f.broker.connected is False:
                add("BROKER_DISCONNECTED", "critical", "Broker link is disconnected.")
            else:
                add("BROKER_STALE", "warning",
                    f"Broker link is {f.broker.reason or 'unavailable'}; broker truth is stale.")
        rate = detail_of(f.broker, "rate_limit_errors")
        if rate:
            add("BROKER_RATE_LIMITED", "warning", f"{rate} broker rate-limit errors reported.")
        if lag_value is not None and lag_value > s.scheduler_lag_warn_seconds:
            add("SCHEDULER_LAG", "warning", f"Scheduler lag is {lag_value}s.")
        if issues:
            add("PROTECTION_GAP", "critical",
                "Protection problems: " + ", ".join(f"{k}={x}" for k, x in sorted(issues.items())))
        if recon.unresolved_drift_items:
            add("RECONCILIATION_DRIFT", "critical",
                f"{len(recon.unresolved_drift_items)} unresolved reconciliation drift item(s).")
        if backup_at is None:
            add("BACKUP_UNAVAILABLE", "info", "No backup manifest found for this database.")
        elif not backup_ok:
            add("BACKUP_FAILED", "warning", "Latest database backup failed verification.")
        elif ctx.now - backup_at > timedelta(hours=36):
            add("BACKUP_STALE", "warning", "Last verified database backup is over 36 hours old.")
        status_file = self._settings.backup_status_file
        if status_file is not None:
            try:
                backup_status = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                add("BACKUP_FAILED", "warning", "Backup status is unavailable.")
            else:
                failed = [name for name in ("operational", "research")
                          if backup_status.get(name, {}).get("last_status") == "failed"]
                if failed:
                    add("BACKUP_FAILED", "warning",
                        "Latest backup failed: " + ", ".join(failed) + ".")

        return HealthResponse(
            account_id=ctx.scope, mode=ctx.mode, engine_state=f.state, server_time=ctx.now,
            feed=FeedHealth(
                link=f.feed.model(), age_seconds=feed_age,
                reconnects=counter(f.feed, "reconnects", "engine"),
                subscription_count=counter(f.feed, "subscriptions", "engine")),
            broker=BrokerHealth(link=f.broker.model(),
                                rate_limit_errors=counter(f.broker, "rate_limit_errors", "engine")),
            scheduler_lag_seconds=lag, persistence=persistence,
            last_reconciliation_at=(
                v.of(recon.last_run_at, "engine", as_of=recon.last_run_at,
                     provenance="reconciliation_runs")
                if recon.last_run_at else v.unavailable("reconciliation_runs")),
            errors=errors)

    def _last_backup(self, database_id: str | None) -> tuple[datetime | None, bool | None]:
        directory = self._settings.backup_dir
        if directory is None or not directory.is_dir() or database_id is None:
            return None, None
        best: tuple[datetime, bool | None] | None = None
        for path in directory.glob("*.manifest.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("database_id") != database_id:
                    continue
                created = datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00"))
            except (OSError, ValueError, KeyError):
                continue
            if created.tzinfo is None:
                continue
            created = created.astimezone(UTC)
            if best is None or created > best[0]:
                from mike_trade.persistence.maintenance import verify_backup

                backup = path.with_name(path.name.removesuffix(".manifest.json"))
                try:
                    best = (created, verify_backup(backup).ok)
                except OSError:
                    best = (created, False)
        return (best[0], best[1]) if best else (None, None)


# --------------------------------------------------------------------------- router


def create_api_router(service: DashboardReadService) -> APIRouter:
    """Build the GET-only ``/api`` router bound to one read service."""
    router = APIRouter(prefix="/api")

    def guarded(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except UnknownAccountError as error:
            raise HTTPException(status_code=404, detail=f"unknown account_id: {error}") from error
        except LookupError as error:
            raise HTTPException(status_code=404, detail=f"unknown trade or account: {error}") from error
        except DatabaseUnavailableError as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        except RuntimeError as error:
            if str(error) == "database unavailable":
                raise HTTPException(status_code=503, detail="database unavailable") from error
            raise

    @router.get("/live", response_model=LiveResponse)
    def live() -> LiveResponse:
        return LiveResponse(status="ok", server_time=service.server_time())

    @router.get("/accounts", response_model=AccountsResponse)
    def accounts() -> Any:
        return guarded(service.list_accounts)

    @router.get("/accounts/{account_id}/status", response_model=AccountStatus)
    def status(account_id: str) -> Any:
        return guarded(lambda: service.status(account_id))

    @router.get("/accounts/{account_id}/kpis", response_model=KpisResponse)
    def kpis(account_id: str) -> Any:
        return guarded(lambda: service.kpis(account_id))

    @router.get("/accounts/{account_id}/watchlist", response_model=WatchlistResponse)
    def watchlist(account_id: str) -> Any:
        return guarded(lambda: service.watchlist(account_id))

    @router.get("/accounts/{account_id}/symbols/{symbol}", response_model=SymbolDetailResponse)
    def symbol_detail(account_id: str, symbol: str) -> Any:
        return guarded(lambda: service.symbol_detail(account_id, symbol))

    @router.get("/accounts/{account_id}/cockpit", response_model=CockpitResponse)
    def cockpit(account_id: str) -> Any:
        return guarded(lambda: service.cockpit(account_id))

    @router.get("/accounts/{account_id}/activity", response_model=ActivityResponse)
    def activity(
        account_id: str, symbol: str | None = None, category: str | None = None,
        strategy: str | None = None, since: datetime | None = None,
        until: datetime | None = None, before: datetime | None = None,
        limit: int = Query(100, ge=1, le=500),
    ) -> Any:
        return guarded(lambda: service.activity(
            account_id, symbol=symbol, category=category, strategy=strategy,
            since=since, until=until, before=before, limit=limit))

    @router.get("/accounts/{account_id}/detail", response_model=DetailResponse)
    def detail(account_id: str) -> Any:
        return guarded(lambda: service.detail(account_id))

    @router.get("/accounts/{account_id}/health", response_model=HealthResponse)
    def health(account_id: str) -> Any:
        return guarded(lambda: service.health(account_id))

    def trade_query(
        page: int, page_size: int, symbol: str | None, date_from: datetime | None,
        date_to: datetime | None, entry_strategy: str | None, exit_filter: str | None,
        outcome: str | None, position_status: str | None, sort: str, direction: str,
    ) -> TradeQuery:
        try:
            return TradeQuery(page, page_size, symbol, date_from, date_to, entry_strategy,
                              exit_filter, outcome, position_status, sort, direction)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/accounts/{account_id}/trades", response_model=TradeList)
    def trades(
        account_id: str, page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200), symbol: str | None = None,
        date_from: datetime | None = None, date_to: datetime | None = None,
        entry_strategy: str | None = None, exit_filter: str | None = None,
        outcome: str | None = None, position_status: str | None = None,
        sort: str = "entry_timestamp", direction: str = "desc",
    ) -> Any:
        query = trade_query(page, page_size, symbol, date_from, date_to, entry_strategy,
                            exit_filter, outcome, position_status, sort, direction)
        return guarded(lambda: service.trades.operational_list(account_id, query))

    def inspection(account_id: str, trade_id: str, as_of: datetime | None = None) -> Any:
        return guarded(lambda: service.trades.operational_inspection(account_id, trade_id, as_of))

    @router.get("/accounts/{account_id}/trades/{trade_id}", response_model=TradeInspection)
    def trade_detail(account_id: str, trade_id: str, as_of: datetime | None = None) -> Any:
        return inspection(account_id, trade_id, as_of)

    @router.get("/accounts/{account_id}/trades/{trade_id}/bars")
    def trade_bars(account_id: str, trade_id: str, as_of: datetime | None = None) -> Any:
        return inspection(account_id, trade_id, as_of).bars

    @router.get("/accounts/{account_id}/trades/{trade_id}/events")
    def trade_events(account_id: str, trade_id: str, as_of: datetime | None = None) -> Any:
        return inspection(account_id, trade_id, as_of).events

    @router.get("/accounts/{account_id}/trades/{trade_id}/diagnostics",
                response_model=TradeDiagnostics)
    def trade_diagnostics(account_id: str, trade_id: str,
                          as_of: datetime | None = None) -> Any:
        return inspection(account_id, trade_id, as_of).diagnostics

    @router.get("/research/runs", response_model=ResearchRuns)
    def research_runs() -> Any:
        return service.trades.research_runs()

    @router.get("/research/runs/{selection_id}", response_model=ResearchRunDetail)
    def research_run_detail(selection_id: str) -> Any:
        try:
            return service.trades.research_run_detail(selection_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="unknown research run") from error

    @router.get("/research/runs/{run_id}/trades", response_model=TradeList)
    def research_trades(
        run_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
        symbol: str | None = None, date_from: datetime | None = None,
        date_to: datetime | None = None, entry_strategy: str | None = None,
        exit_filter: str | None = None, outcome: str | None = None,
        position_status: str | None = None, sort: str = "entry_timestamp",
        direction: str = "desc",
    ) -> Any:
        query = trade_query(page, page_size, symbol, date_from, date_to, entry_strategy,
                            exit_filter, outcome, position_status, sort, direction)
        try:
            return service.trades.research_list(run_id, query)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=f"unknown research run: {run_id}") from error

    def research_inspection(run_id: str, trade_id: str, as_of: datetime | None = None) -> Any:
        try:
            return service.trades.research_inspection(run_id, trade_id, as_of)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="unknown research run or trade") from error

    @router.get("/research/runs/{run_id}/trades/{trade_id}", response_model=TradeInspection)
    def research_trade_detail(run_id: str, trade_id: str,
                              as_of: datetime | None = None) -> Any:
        return research_inspection(run_id, trade_id, as_of)

    @router.get("/research/runs/{run_id}/trades/{trade_id}/bars")
    def research_trade_bars(run_id: str, trade_id: str, as_of: datetime | None = None) -> Any:
        return research_inspection(run_id, trade_id, as_of).bars

    @router.get("/research/runs/{run_id}/trades/{trade_id}/events")
    def research_trade_events(run_id: str, trade_id: str,
                              as_of: datetime | None = None) -> Any:
        return research_inspection(run_id, trade_id, as_of).events

    @router.get("/research/runs/{run_id}/trades/{trade_id}/diagnostics",
                response_model=TradeDiagnostics)
    def research_trade_diagnostics(run_id: str, trade_id: str,
                                   as_of: datetime | None = None) -> Any:
        return research_inspection(run_id, trade_id, as_of).diagnostics

    @router.get("/{unknown:path}", include_in_schema=False)
    def unknown_api(unknown: str, request: Request) -> Any:
        raise HTTPException(status_code=404, detail="unknown API route")

    return router
