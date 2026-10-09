"""Trade/strategy performance attribution over persisted projections.

Read-only computations scoped to exactly one account/scenario.  Nothing here
writes to the database, imports fee/accounting writers, converts money to
float, or aggregates balances across scopes.

Design choices:
1. One report covers exactly one ``account_scope_id`` (``ReportFilter``
   requires it); every SQL read is scope-filtered.  :func:`compare_accounts`
   names two scopes explicitly and reports matched/missed/extra trades only.
2. Fee provenance is read as plain strings without importing
   ``mike_trade.reporting.fees``.  Per broker order, fee-bearing broker
   activities that name the order (``order_id`` or a mapped
   ``client_order_id`` in the stored ``payload_json``) give ``CONFIRMED``;
   in SHADOW/BACKTEST scopes the simulated fee recorded on each fill is
   ``ESTIMATED``; anything else -- including a missing status -- is
   ``UNAVAILABLE``.  Unavailable fees are never invented: they are excluded
   from net P&L and force ``fees_complete`` to false.
3. Gross P&L is realized only: exit proceeds minus entry cost of the exited
   quantity (``exit_qty * entry_avg``), so open cycles report the realized
   part and untouched opens report zero rather than a fictitious loss.
4. Entry/exit average prices are fill-quantity-weighted; partial fills are
   handled by exact Decimal arithmetic.
5. Exit attribution: an exit whose fills ran through the cycle's trailing
   stop broker order is ``TRAILING_STOP``; a nearby operator
   FLATTEN/CLOSE_ALL/CLOSE_POSITION command makes it ``FLATTEN``; otherwise
   the exit intents' purposes map (STRATEGY_EXIT/PROTECTIVE_STOP/
   MANUAL_CLOSE/SAFETY_EXIT/EOD_CLOSE).  Non-strategy exits keep the
   originating BUY strategy and leave ``sell_strategy`` empty.
6. Period bounds use the America/New_York session calendar: DAY starts at
   ET midnight of ``as_of``, WEEK at Monday ET midnight; explicit
   since/until intersect the period window.

``source`` for the builders is either a read connection exposing
``execute(sql, params)`` (a ``ReadStore.snapshot`` view, a raw sqlite3
connection, or a ``ReadStore`` -- which is snapshotted per scope) or an
iterable of in-memory projection fixtures: mappings with plain-string money
and timestamp fields (see :func:`build_trade_cycles`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from mike_trade.persistence import schema as _schema

__all__ = [
    "TradeCycleRecord",
    "ExitCause",
    "ReportFilter",
    "PERFORMANCE_DISCLAIMER",
    "build_trade_cycles",
    "summarize_by_strategy",
    "summarize_by_pair",
    "summarize_by_symbol",
    "count_evaluation_outcomes",
    "compare_accounts",
]

PERFORMANCE_DISCLAIMER = (
    "Historical/simulated results do not establish future profitability; "
    "figures are attribution over past fills only."
)

_NEW_YORK = ZoneInfo("America/New_York")
_ZERO = Decimal("0")
_BPS = Decimal("10000")

_FEE_CONFIRMED = "CONFIRMED"
_FEE_ESTIMATED = "ESTIMATED"
_FEE_UNAVAILABLE = "UNAVAILABLE"
_FEE_STATUSES = frozenset({_FEE_CONFIRMED, _FEE_ESTIMATED, _FEE_UNAVAILABLE})

_SHADOW_ENVIRONMENTS = frozenset({"SHADOW", "BACKTEST"})

_FLATTEN_COMMANDS = frozenset({"FLATTEN", "CLOSE_ALL", "CLOSE_POSITION"})
_FLATTEN_WINDOW = timedelta(minutes=15)


class ExitCause(str, Enum):  # noqa: UP042 - exact str values are a cross-phase contract
    """Why a managed position cycle exited."""

    STRATEGY_EXIT = "STRATEGY_EXIT"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    FLATTEN = "FLATTEN"
    END_OF_DAY = "END_OF_DAY"
    SAFETY = "SAFETY"


_PURPOSE_TO_CAUSE: dict[str, ExitCause] = {
    "STRATEGY_EXIT": ExitCause.STRATEGY_EXIT,
    "PROTECTIVE_STOP": ExitCause.PROTECTIVE_STOP,
    "MANUAL_CLOSE": ExitCause.MANUAL_CLOSE,
    "SAFETY_EXIT": ExitCause.SAFETY,
    "EOD_CLOSE": ExitCause.END_OF_DAY,
}

# When exit fills mix purposes, the first hit in this order wins so a single
# protective/manual/safety/end-of-day fill determines attribution.
_CAUSE_PRECEDENCE = (
    ExitCause.TRAILING_STOP,
    ExitCause.FLATTEN,
    ExitCause.PROTECTIVE_STOP,
    ExitCause.MANUAL_CLOSE,
    ExitCause.SAFETY,
    ExitCause.END_OF_DAY,
    ExitCause.STRATEGY_EXIT,
)


@dataclass(frozen=True, slots=True)
class ReportFilter:
    """Scope and facet filters shared by every report builder."""

    account_scope_id: str
    period: str = "lifetime"
    since: datetime | None = None
    until: datetime | None = None
    symbol: str | None = None
    buy_strategy: str | None = None
    sell_strategy: str | None = None
    pair: str | None = None
    exit_cause: ExitCause | str | None = None
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        scope = self.account_scope_id
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("account_scope_id must be a non-empty string")
        object.__setattr__(self, "account_scope_id", scope.strip())
        if self.period not in ("day", "week", "lifetime"):
            raise ValueError("period must be one of 'day', 'week', 'lifetime'")
        for name in ("since", "until", "as_of"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError(f"{name} must be a timezone-aware datetime or None")
        if self.symbol is not None:
            if not isinstance(self.symbol, str) or not self.symbol.strip():
                raise ValueError("symbol must be a non-empty string or None")
            object.__setattr__(self, "symbol", self.symbol.strip().upper())
        for name in ("buy_strategy", "sell_strategy", "pair"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{name} must be a non-empty string or None")
                object.__setattr__(self, name, value.strip())
        cause = self.exit_cause
        if cause is not None and not isinstance(cause, ExitCause):
            try:
                cause = ExitCause(str(cause).strip())
            except ValueError:
                raise ValueError(
                    f"exit_cause must be one of {[c.value for c in ExitCause]} or None"
                ) from None
            object.__setattr__(self, "exit_cause", cause)

    @property
    def resolved_as_of(self) -> datetime:
        """Reference instant for day/week windows (defaults to now, UTC)."""
        if self.as_of is None:
            return datetime.now(UTC)
        return self.as_of.astimezone(UTC)

    def window(self) -> tuple[datetime | None, datetime | None]:
        """Intersected (since, until) bounds: period window meets explicit bounds."""
        period_start, period_end = _period_bounds(self.period, self.resolved_as_of)
        start = period_start
        if self.since is not None:
            moment = self.since.astimezone(UTC)
            start = moment if start is None or moment > start else start
        end = period_end
        if self.until is not None:
            moment = self.until.astimezone(UTC)
            end = moment if end is None or moment < end else end
        return start, end

    @property
    def period_label(self) -> str:
        """Stable label: the period, or 'custom' when explicit bounds are set."""
        if self.since is not None or self.until is not None:
            return f"{self.period}:custom"
        return self.period


def _period_bounds(period: str, as_of: datetime) -> tuple[datetime | None, datetime | None]:
    if period == "lifetime":
        return None, None
    local = as_of.astimezone(_NEW_YORK)
    if period == "day":
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # week: Monday ET midnight
        monday = (local.replace(hour=0, minute=0, second=0, microsecond=0)
                  - timedelta(days=local.weekday()))
        day_start = monday
    start = day_start.astimezone(UTC)
    length = timedelta(days=1) if period == "day" else timedelta(days=7)
    return start, start + length


@dataclass(frozen=True, slots=True)
class TradeCycleRecord:
    """Attribution for one completed or open managed position cycle."""

    account_scope_id: str
    cycle_id: str
    symbol: str
    buy_strategy: str | None
    sell_strategy: str | None
    exit_cause: ExitCause | None
    pair: str | None
    entry_avg_price: Decimal | None
    exit_avg_price: Decimal | None
    filled_quantity: Decimal
    gross_pnl: Decimal
    fee_confirmed: Decimal
    fee_estimated: Decimal
    fee_status: str
    fees_complete: bool
    net_pnl: Decimal
    opened_at: datetime | None
    closed_at: datetime | None
    holding_duration: timedelta | None
    signal_time: datetime | None
    signal_bar_time: datetime | None
    signal_reference_price: Decimal | None
    submit_time: datetime | None
    first_fill_time: datetime | None
    final_fill_time: datetime | None
    latency_signal_to_submit: timedelta | None
    latency_submit_to_first_fill: timedelta | None
    latency_first_to_final_fill: timedelta | None
    slippage_dollars: Decimal | None
    slippage_bps: Decimal | None
    trailing_update_count: int = 0
    last_confirmed_stop: Decimal | None = None
    entry_fill_count: int = 0
    exit_fill_count: int = 0

    def to_row(self) -> dict[str, Any]:
        """Flatten to the 'cycles' export row mapping (Decimal/datetime preserved)."""
        return {
            "cycle_id": self.cycle_id,
            "account_scope_id": self.account_scope_id,
            "symbol": self.symbol,
            "buy_strategy": self.buy_strategy,
            "sell_strategy": self.sell_strategy,
            "exit_cause": self.exit_cause.value if self.exit_cause is not None else None,
            "pair": self.pair,
            "entry_avg_price": self.entry_avg_price,
            "exit_avg_price": self.exit_avg_price,
            "filled_quantity": self.filled_quantity,
            "gross_pnl": self.gross_pnl,
            "fee_confirmed": self.fee_confirmed,
            "fee_estimated": self.fee_estimated,
            "fee_status": self.fee_status,
            "fees_complete": self.fees_complete,
            "net_pnl": self.net_pnl,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "holding_duration_seconds": _duration_seconds(self.holding_duration),
            "signal_time": self.signal_time,
            "signal_bar_time": self.signal_bar_time,
            "submit_time": self.submit_time,
            "first_fill_time": self.first_fill_time,
            "final_fill_time": self.final_fill_time,
            "latency_signal_to_submit_seconds": _duration_seconds(
                self.latency_signal_to_submit
            ),
            "latency_submit_to_first_fill_seconds": _duration_seconds(
                self.latency_submit_to_first_fill
            ),
            "latency_first_to_final_fill_seconds": _duration_seconds(
                self.latency_first_to_final_fill
            ),
            "signal_reference_price": self.signal_reference_price,
            "slippage_dollars": self.slippage_dollars,
            "slippage_bps": self.slippage_bps,
            "trailing_update_count": self.trailing_update_count,
            "last_confirmed_stop": self.last_confirmed_stop,
            "entry_fill_count": self.entry_fill_count,
            "exit_fill_count": self.exit_fill_count,
        }


def _duration_seconds(value: timedelta | None) -> Decimal | None:
    """Exact Decimal seconds for a duration; never passes through float."""
    if value is None:
        return None
    return (
        Decimal(value.days * 86400 + value.seconds)
        + Decimal(value.microseconds) / Decimal(1_000_000)
    )


def _parse_decimal(text: Any, *, field: str) -> Decimal:
    if isinstance(text, Decimal):
        value = text
    elif isinstance(text, str):
        try:
            value = Decimal(text.strip())
        except InvalidOperation:
            raise ValueError(f"{field} is not a valid decimal string: {text!r}") from None
    elif isinstance(text, int) and not isinstance(text, bool):
        value = Decimal(text)
    else:
        raise ValueError(f"{field} must be a Decimal, int, or numeric string")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _parse_timestamp(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)
    if isinstance(value, str):
        text = value.strip()
        try:
            return _schema.from_db_timestamp(text)
        except ValueError:
            pass
        candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            raise ValueError(f"{field} is not a valid timestamp: {value!r}") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return parsed.astimezone(UTC)
    raise ValueError(f"{field} must be a datetime or timestamp string")


@contextmanager
def _view(source: Any, scope: str) -> Iterator[Any]:
    """Yield a read connection with ``execute`` for one scope (read-only)."""
    snapshot = getattr(source, "snapshot", None)
    if callable(snapshot):
        with snapshot(scope) as conn:
            yield conn
        return
    if hasattr(source, "execute"):
        yield source
        return
    raise TypeError("source must be a ReadStore, a read connection, or fixtures")


def _table_exists(conn: Any, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _scope_environment(conn: Any, scope: str) -> str:
    row = conn.execute(
        "SELECT environment FROM account_scopes WHERE account_scope_id = ?", (scope,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown account_scope_id: {scope!r}")
    return str(row[0])


@dataclass(slots=True)
class _FillLeg:
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_status: str
    filled_at: datetime | None
    broker_order_id: str | None


@dataclass(slots=True)
class _CycleProjection:
    cycle_id: str
    symbol: str
    state: str
    entry_strategy_key: str | None
    opened_at: datetime | None
    closed_at: datetime | None
    entry_fills: list[_FillLeg] = field(default_factory=list)
    exit_fills: list[_FillLeg] = field(default_factory=list)
    # order_id -> (purpose, signal_id, submitted_at); entry/exit order maps
    order_purpose: dict[str, str] = field(default_factory=dict)
    order_signal: dict[str, str | None] = field(default_factory=dict)
    order_submitted: dict[str, datetime | None] = field(default_factory=dict)
    intent_created: dict[str, datetime | None] = field(default_factory=dict)


@dataclass(slots=True)
class _SignalInfo:
    instance_id: str | None
    strategy_key: str | None
    role: str | None
    pair_key: str | None
    reference_price: Decimal | None
    bar_time: datetime | None
    generated_at: datetime | None


def _weighted_avg(fills: list[_FillLeg]) -> tuple[Decimal | None, Decimal]:
    quantity = _ZERO
    cost = _ZERO
    for leg in fills:
        quantity += leg.quantity
        cost += leg.quantity * leg.price
    if quantity == _ZERO:
        return None, _ZERO
    return cost / quantity, quantity


def _cycle_fees(entry_fills: list[_FillLeg], exit_fills: list[_FillLeg]) -> tuple[Decimal, Decimal, str, bool]:
    """Return (confirmed, estimated, status, complete) with no invented fees."""
    confirmed = _ZERO
    estimated = _ZERO
    complete = True
    worst = _FEE_CONFIRMED
    for leg in entry_fills + exit_fills:
        status = leg.fee_status if leg.fee_status in _FEE_STATUSES else _FEE_UNAVAILABLE
        if status == _FEE_CONFIRMED:
            confirmed += leg.fee
        elif status == _FEE_ESTIMATED:
            estimated += leg.fee
            worst = _FEE_ESTIMATED
        else:
            complete = False
            worst = _FEE_UNAVAILABLE
    if not (entry_fills or exit_fills):
        return _ZERO, _ZERO, _FEE_UNAVAILABLE, False
    if worst == _FEE_CONFIRMED and not complete:
        worst = _FEE_UNAVAILABLE
    return confirmed, estimated, worst, complete


def _attribution(
    projection: _CycleProjection,
    signals: Mapping[str, _SignalInfo],
    trailing_order_ids: set[str],
    flatten_commands: list[tuple[str, datetime]],
) -> tuple[str | None, str | None, ExitCause | None, str | None, _SignalInfo | None]:
    """Return (buy, sell, cause, pair, entry_signal) for one cycle."""
    entry_signal: _SignalInfo | None = None
    entry_pair: str | None = None
    buy = projection.entry_strategy_key
    for leg in projection.entry_fills:
        signal_id = projection.order_signal.get(leg.broker_order_id or "")
        info = signals.get(signal_id or "")
        if info is not None:
            entry_signal = entry_signal or info
            if buy is None and info.strategy_key is not None:
                buy = info.strategy_key
            if entry_pair is None and info.pair_key is not None:
                entry_pair = info.pair_key
            break
    if not projection.exit_fills:
        pair = entry_pair or buy
        return buy, None, None, pair, entry_signal

    exit_order_ids = {leg.broker_order_id for leg in projection.exit_fills
                      if leg.broker_order_id}
    if exit_order_ids & trailing_order_ids:
        cause: ExitCause | None = ExitCause.TRAILING_STOP
    else:
        cause = None
        if projection.closed_at is not None:
            for symbol, issued in flatten_commands:
                if symbol == projection.symbol and _FLATTEN_WINDOW >= (
                    projection.closed_at - issued
                ) >= -timedelta(minutes=1):
                    cause = ExitCause.FLATTEN
                    break
        if cause is None:
            purposes = {projection.order_purpose.get(order_id or "")
                        for order_id in exit_order_ids}
            ranked = sorted(
                {_PURPOSE_TO_CAUSE[p] for p in purposes if p in _PURPOSE_TO_CAUSE},
                key=lambda c: _CAUSE_PRECEDENCE.index(c),
            )
            cause = ranked[0] if ranked else None

    sell: str | None = None
    sell_pair: str | None = None
    if cause is ExitCause.STRATEGY_EXIT:
        for leg in projection.exit_fills:
            info = signals.get(projection.order_signal.get(leg.broker_order_id or "") or "")
            if info is not None and info.strategy_key is not None:
                sell = info.strategy_key
                sell_pair = info.pair_key
                break
    if entry_pair is not None and sell_pair is not None and entry_pair == sell_pair:
        pair = entry_pair
    elif buy is not None and sell is not None:
        pair = f"{buy}+{sell}"
    else:
        pair = entry_pair or sell_pair or buy
    return buy, sell, cause, pair, entry_signal


def _record_from_projection(
    scope: str,
    projection: _CycleProjection,
    signals: Mapping[str, _SignalInfo],
    trailing_order_ids: set[str],
    trailing_updates: int,
    last_confirmed_stop: Decimal | None,
    flatten_commands: list[tuple[str, datetime]],
) -> TradeCycleRecord:
    entry_avg, entry_qty = _weighted_avg(projection.entry_fills)
    exit_avg, exit_qty = _weighted_avg(projection.exit_fills)
    buy, sell, cause, pair, entry_signal = _attribution(
        projection, signals, trailing_order_ids, flatten_commands
    )
    confirmed, estimated, fee_status, complete = _cycle_fees(
        projection.entry_fills, projection.exit_fills
    )
    if entry_avg is None:
        gross = _ZERO
    elif exit_qty == _ZERO:
        gross = _ZERO
    else:
        gross = sum((leg.quantity * leg.price for leg in projection.exit_fills), _ZERO) - (
            entry_avg * exit_qty
        )
    net = gross - confirmed - estimated
    filled_quantity = exit_qty if exit_qty > _ZERO else entry_qty

    opened = projection.opened_at
    closed = projection.closed_at
    holding = (closed - opened) if (opened is not None and closed is not None) else None

    signal_time = entry_signal.generated_at if entry_signal else None
    bar_time = entry_signal.bar_time if entry_signal else None
    reference = entry_signal.reference_price if entry_signal else None

    submitted: datetime | None = None
    for leg in projection.entry_fills:
        candidate = projection.order_submitted.get(leg.broker_order_id or "")
        if candidate is not None and (submitted is None or candidate < submitted):
            submitted = candidate
    if submitted is None:
        for leg in projection.entry_fills:
            candidate = projection.intent_created.get(leg.broker_order_id or "")
            if candidate is not None and (submitted is None or candidate < submitted):
                submitted = candidate
    entry_times = [leg.filled_at for leg in projection.entry_fills
                   if leg.filled_at is not None]
    exit_times = [leg.filled_at for leg in projection.exit_fills
                  if leg.filled_at is not None]
    first_fill = min(entry_times) if entry_times else None
    final_fill = max(exit_times) if exit_times else (
        max(entry_times) if entry_times else None
    )
    lat_signal_submit = (
        (submitted - signal_time) if (signal_time and submitted) else None
    )
    lat_submit_first = (
        (first_fill - submitted) if (submitted and first_fill) else None
    )
    lat_first_final = (
        (final_fill - first_fill) if (first_fill and final_fill) else None
    )

    slippage_dollars: Decimal | None = None
    slippage_bps: Decimal | None = None
    if entry_avg is not None and reference is not None and reference != _ZERO:
        slippage_dollars = entry_avg - reference
        slippage_bps = slippage_dollars / reference * _BPS

    return TradeCycleRecord(
        account_scope_id=scope,
        cycle_id=projection.cycle_id,
        symbol=projection.symbol,
        buy_strategy=buy,
        sell_strategy=sell,
        exit_cause=cause,
        pair=pair,
        entry_avg_price=entry_avg,
        exit_avg_price=exit_avg,
        filled_quantity=filled_quantity,
        gross_pnl=gross,
        fee_confirmed=confirmed,
        fee_estimated=estimated,
        fee_status=fee_status,
        fees_complete=complete,
        net_pnl=net,
        opened_at=opened,
        closed_at=closed,
        holding_duration=holding,
        signal_time=signal_time,
        signal_bar_time=bar_time,
        signal_reference_price=reference,
        submit_time=submitted,
        first_fill_time=first_fill,
        final_fill_time=final_fill,
        latency_signal_to_submit=lat_signal_submit,
        latency_submit_to_first_fill=lat_submit_first,
        latency_first_to_final_fill=lat_first_final,
        slippage_dollars=slippage_dollars,
        slippage_bps=slippage_bps,
        trailing_update_count=trailing_updates,
        last_confirmed_stop=last_confirmed_stop,
        entry_fill_count=len(projection.entry_fills),
        exit_fill_count=len(projection.exit_fills),
    )


def _matches(record: TradeCycleRecord, report_filter: ReportFilter) -> bool:
    if record.account_scope_id != report_filter.account_scope_id:
        return False
    if report_filter.symbol is not None and record.symbol != report_filter.symbol:
        return False
    if report_filter.buy_strategy is not None and record.buy_strategy != report_filter.buy_strategy:
        return False
    if report_filter.sell_strategy is not None and record.sell_strategy != report_filter.sell_strategy:
        return False
    if report_filter.pair is not None and record.pair != report_filter.pair:
        return False
    if report_filter.exit_cause is not None and record.exit_cause != report_filter.exit_cause:
        return False
    start, end = report_filter.window()
    if start is not None or end is not None:
        moment = record.closed_at if record.closed_at is not None else record.opened_at
        if moment is None:
            return False
        if start is not None and moment < start:
            return False
        if end is not None and moment >= end:
            return False
    return True


# ------------------------------------------------------------------ DB loading


def _load_fee_links(conn: Any, scope: str) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Read fee-bearing activities as plain strings; map order -> CONFIRMED amount."""
    confirmed: dict[str, Decimal] = {}
    client_to_broker: dict[str, str] = {}
    try:
        for client_id, broker_id in conn.execute(
            "SELECT client_order_id, broker_order_id FROM broker_orders "
            "WHERE account_scope_id = ?",
            (scope,),
        ).fetchall():
            client_to_broker[str(client_id)] = str(broker_id)
    except Exception:
        return confirmed, client_to_broker  # pragma: no cover - schema always has it
    if not _table_exists(conn, "broker_activities"):
        return confirmed, client_to_broker
    for (payload_json,) in conn.execute(
        "SELECT payload_json FROM broker_activities WHERE account_scope_id = ?",
        (scope,),
    ).fetchall():
        try:
            payload = json.loads(payload_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        category = str(payload.get("category", ""))
        if category not in ("FEE", "TRADE_FILL"):
            continue
        raw_amount = payload.get("fee_amount")
        if raw_amount is None:
            continue
        try:
            amount = _parse_decimal(raw_amount, field="fee_amount")
        except ValueError:
            continue
        target = payload.get("order_id")
        if target is None and payload.get("client_order_id") is not None:
            target = client_to_broker.get(str(payload.get("client_order_id")))
        if target is None:
            continue  # account-level charge; never attributed to an order here
        key = str(target)
        confirmed[key] = confirmed.get(key, _ZERO) + amount
    return confirmed, client_to_broker


def _load_projections(conn: Any, scope: str) -> tuple[
    dict[str, _CycleProjection], dict[str, _SignalInfo], str,
]:
    try:
        environment = _scope_environment(conn, scope)
    except ValueError:
        return {}, {}, "PAPER"  # unknown scope: no cycles, nothing to attribute
    projections: dict[str, _CycleProjection] = {}
    for row in conn.execute(
        "SELECT cycle_id, symbol, state, entry_strategy_key, opened_at, closed_at "
        "FROM position_cycles WHERE account_scope_id = ? ORDER BY cycle_id",
        (scope,),
    ).fetchall():
        projections[str(row[0])] = _CycleProjection(
            cycle_id=str(row[0]),
            symbol=str(row[1]),
            state=str(row[2]),
            entry_strategy_key=None if row[3] is None else str(row[3]),
            opened_at=_schema.from_db_timestamp(row[4]) if row[4] is not None else None,
            closed_at=_schema.from_db_timestamp(row[5]) if row[5] is not None else None,
        )

    order_purpose: dict[str, str] = {}
    order_signal: dict[str, str | None] = {}
    order_submitted: dict[str, datetime | None] = {}
    intent_created: dict[str, datetime | None] = {}
    order_to_broker: dict[str, str] = {}
    if _table_exists(conn, "order_intents"):
        for row in conn.execute(
            "SELECT intent_id, client_order_id, purpose, signal_id, created_at "
            "FROM order_intents WHERE account_scope_id = ?",
            (scope,),
        ).fetchall():
            order_purpose[str(row[1])] = str(row[2])
            order_signal[str(row[1])] = None if row[3] is None else str(row[3])
            intent_created[str(row[1])] = (
                _schema.from_db_timestamp(row[4]) if row[4] is not None else None
            )
            order_to_broker[str(row[0])] = str(row[1])
    if _table_exists(conn, "broker_orders"):
        for row in conn.execute(
            "SELECT broker_order_id, intent_id, client_order_id, submitted_at "
            "FROM broker_orders WHERE account_scope_id = ?",
            (scope,),
        ).fetchall():
            broker_id, intent_id, client_id = str(row[0]), row[1], str(row[2])
            submitted = _schema.from_db_timestamp(row[3]) if row[3] is not None else None
            order_submitted[broker_id] = submitted
            if intent_id is not None and str(intent_id) in order_to_broker:
                client_key = order_to_broker[str(intent_id)]
                if broker_id not in order_purpose and client_key in order_purpose:
                    order_purpose[broker_id] = order_purpose[client_key]
                    order_signal[broker_id] = order_signal[client_key]
                    intent_created[broker_id] = intent_created[client_key]
            if broker_id not in order_purpose and client_id in order_purpose:
                order_purpose[broker_id] = order_purpose[client_id]
                order_signal[broker_id] = order_signal[client_id]
                intent_created[broker_id] = intent_created[client_id]

    instances: dict[str, tuple[str, str, str | None]] = {}
    if _table_exists(conn, "strategy_instances"):
        for row in conn.execute(
            "SELECT instance_id, strategy_key, role, pair_key FROM strategy_instances "
            "WHERE account_scope_id = ?",
            (scope,),
        ).fetchall():
            instances[str(row[0])] = (str(row[1]), str(row[2]),
                                      None if row[3] is None else str(row[3]))
    signals: dict[str, _SignalInfo] = {}
    if _table_exists(conn, "signals"):
        for row in conn.execute(
            "SELECT signal_id, instance_id, reference_price, bar_time, generated_at "
            "FROM signals WHERE account_scope_id = ?",
            (scope,),
        ).fetchall():
            signal_id, instance_id = str(row[0]), str(row[1])
            key, role, pair_key = instances.get(instance_id, (None, None, None))
            signals[signal_id] = _SignalInfo(
                instance_id=instance_id,
                strategy_key=key,
                role=role,
                pair_key=pair_key,
                reference_price=(
                    _schema.decimal_from_db(row[2]) if row[2] is not None else None
                ),
                bar_time=_schema.from_db_timestamp(row[3]) if row[3] is not None else None,
                generated_at=(
                    _schema.from_db_timestamp(row[4]) if row[4] is not None else None
                ),
            )

    confirmed_links, _ = _load_fee_links(conn, scope)
    shadow = environment in _SHADOW_ENVIRONMENTS

    if _table_exists(conn, "fills"):
        order_legs: dict[str, list[_FillLeg]] = {}
        for row in conn.execute(
            "SELECT broker_order_id, cycle_id, side, quantity, price, fee, filled_at "
            "FROM fills WHERE account_scope_id = ? ORDER BY filled_at, rowid",
            (scope,),
        ).fetchall():
            broker_id, cycle_id = str(row[0]), None if row[1] is None else str(row[1])
            if cycle_id is None or cycle_id not in projections:
                continue
            projection = projections[cycle_id]
            if shadow:
                status = _FEE_ESTIMATED
            elif broker_id in confirmed_links:
                status = _FEE_CONFIRMED
            else:
                status = _FEE_UNAVAILABLE
            leg = _FillLeg(
                quantity=_schema.decimal_from_db(row[3]),
                price=_schema.decimal_from_db(row[4]),
                fee=_schema.decimal_from_db(row[5]),
                fee_status=status,
                filled_at=_schema.from_db_timestamp(row[6]),
                broker_order_id=broker_id,
            )
            if str(row[2]) == "SELL":
                projection.exit_fills.append(leg)
            else:
                projection.entry_fills.append(leg)
            order_legs.setdefault(broker_id, []).append(leg)
        if not shadow:
            # A CONFIRMED fee is the broker activity amount, not the fill
            # row's own fee column (which has no provenance).  Attribute the
            # whole order amount to its first fill so cycle totals stay exact.
            for broker_id, legs in order_legs.items():
                if broker_id in confirmed_links and legs:
                    legs[0].fee = confirmed_links[broker_id]
                    for extra in legs[1:]:
                        extra.fee = _ZERO
    for projection in projections.values():
        projection.order_purpose = order_purpose
        projection.order_signal = order_signal
        projection.order_submitted = order_submitted
        projection.intent_created = intent_created
    return projections, signals, environment


def _load_trailing(conn: Any, scope: str) -> tuple[
    dict[str, set[str]], dict[str, int], dict[str, Decimal | None],
]:
    order_ids: dict[str, set[str]] = {}
    updates: dict[str, int] = {}
    confirmed: dict[str, Decimal | None] = {}
    if _table_exists(conn, "trailing_stop_states"):
        for row in conn.execute(
            "SELECT cycle_id, broker_order_id, stop_price, last_confirmed_at "
            "FROM trailing_stop_states WHERE account_scope_id = ?",
            (scope,),
        ).fetchall():
            cycle_id = str(row[0])
            if row[1] is not None:
                order_ids.setdefault(cycle_id, set()).add(str(row[1]))
            if row[3] is not None and row[2] is not None:
                try:
                    confirmed[cycle_id] = _schema.decimal_from_db(row[2])
                except ValueError:
                    confirmed[cycle_id] = None
            else:
                confirmed.setdefault(cycle_id, None)
    if _table_exists(conn, "trailing_stop_events"):
        for cycle_id, count in conn.execute(
            "SELECT cycle_id, COUNT(*) FROM trailing_stop_events "
            "WHERE account_scope_id = ? GROUP BY cycle_id",
            (scope,),
        ).fetchall():
            updates[str(cycle_id)] = int(count)
    return order_ids, updates, confirmed


def _load_flatten_commands(conn: Any, scope: str) -> list[tuple[str, datetime]]:
    if not _table_exists(conn, "operator_commands"):
        return []
    rows = conn.execute(
        "SELECT symbol, command_kind, issued_at FROM operator_commands "
        "WHERE account_scope_id = ?",
        (scope,),
    ).fetchall()
    result: list[tuple[str, datetime]] = []
    for symbol, kind, issued_at in rows:
        if str(kind) not in _FLATTEN_COMMANDS or symbol is None:
            continue
        try:
            issued = _schema.from_db_timestamp(str(issued_at))
        except ValueError:
            continue
        result.append((str(symbol).upper(), issued))
    return result


# -------------------------------------------------------------- fixture loading


def _fixture_signal(raw: Mapping[str, Any]) -> _SignalInfo:
    return _SignalInfo(
        instance_id=None if raw.get("instance_id") is None else str(raw.get("instance_id")),
        strategy_key=None if raw.get("strategy_key") is None else str(raw.get("strategy_key")),
        role=None if raw.get("role") is None else str(raw.get("role")),
        pair_key=None if raw.get("pair_key") is None else str(raw.get("pair_key")),
        reference_price=(
            None if raw.get("reference_price") is None
            else _parse_decimal(raw.get("reference_price"), field="reference_price")
        ),
        bar_time=_parse_timestamp(raw.get("bar_time"), field="bar_time"),
        generated_at=_parse_timestamp(raw.get("generated_at"), field="generated_at"),
    )


def _fixture_leg(raw: Mapping[str, Any], *, field: str) -> _FillLeg:
    status = raw.get("fee_status")
    status_text = str(status).strip().upper() if status is not None else _FEE_UNAVAILABLE
    if status_text not in _FEE_STATUSES:
        status_text = _FEE_UNAVAILABLE
    return _FillLeg(
        quantity=_parse_decimal(raw.get("quantity"), field=f"{field}.quantity"),
        price=_parse_decimal(raw.get("price"), field=f"{field}.price"),
        fee=_parse_decimal(raw.get("fee", "0"), field=f"{field}.fee"),
        fee_status=status_text,
        filled_at=_parse_timestamp(raw.get("filled_at"), field=f"{field}.filled_at"),
        broker_order_id=(
            None if raw.get("broker_order_id") is None else str(raw.get("broker_order_id"))
        ),
    )


def _projection_from_fixture(raw: Mapping[str, Any]) -> tuple[
    _CycleProjection, dict[str, _SignalInfo], set[str], int, Decimal | None,
    list[tuple[str, datetime]],
]:
    """Build one cycle projection from an in-memory fixture mapping.

    Fixture keys (plain strings): ``cycle_id``, ``symbol``, ``state``,
    ``buy_strategy`` (or ``entry_strategy_key``), ``sell_strategy``,
    ``exit_cause``, ``pair``, ``opened_at``/``closed_at`` timestamps,
    ``entry_fills``/``exit_fills`` lists of ``{quantity, price, fee,
    fee_status, filled_at, broker_order_id}``, ``signal`` mapping with
    ``strategy_key``/``role``/``pair_key``/``reference_price``/``bar_time``/
    ``generated_at``, ``submitted_at``, ``trailing_update_count``,
    ``last_confirmed_stop``, ``trailing_order_ids`` and ``exit_purposes``
    (order_id -> purpose) for exit-cause derivation.
    """
    cycle_id = str(raw["cycle_id"])
    signals: dict[str, _SignalInfo] = {}
    signal_raw = raw.get("signal")
    signal_id: str | None = None
    if isinstance(signal_raw, Mapping):
        signal_id = str(signal_raw.get("signal_id", f"sig:{cycle_id}"))
        signals[signal_id] = _fixture_signal(signal_raw)
    projection = _CycleProjection(
        cycle_id=cycle_id,
        symbol=str(raw.get("symbol", "")),
        state=str(raw.get("state", "CLOSED")),
        entry_strategy_key=(
            None if raw.get("buy_strategy", raw.get("entry_strategy_key")) is None
            else str(raw.get("buy_strategy", raw.get("entry_strategy_key")))
        ),
        opened_at=_parse_timestamp(raw.get("opened_at"), field="opened_at"),
        closed_at=_parse_timestamp(raw.get("closed_at"), field="closed_at"),
    )
    for key, bucket in (("entry_fills", projection.entry_fills),
                        ("exit_fills", projection.exit_fills)):
        items = raw.get(key, ())
        if not isinstance(items, Iterable) or isinstance(items, (str, bytes)):
            raise ValueError(f"{key} must be a list of fill mappings")
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError(f"{key} items must be mappings")
            bucket.append(_fixture_leg(item, field=key))
    submitted = _parse_timestamp(raw.get("submitted_at"), field="submitted_at")
    purposes = raw.get("exit_purposes", {})
    if not isinstance(purposes, Mapping):
        raise ValueError("exit_purposes must be a mapping")
    for leg in projection.entry_fills + projection.exit_fills:
        key = leg.broker_order_id or ""
        if signal_id is not None and leg in projection.entry_fills:
            projection.order_signal[key] = signal_id
        if submitted is not None and leg in projection.entry_fills:
            projection.order_submitted[key] = submitted
        purpose = purposes.get(key)
        if purpose is not None:
            projection.order_purpose[key] = str(purpose)
    # A fixture may pin attribution directly; translate it into the same
    # channels the DB path uses so one code path builds the record.
    sell = raw.get("sell_strategy")
    cause_raw = raw.get("exit_cause")
    if cause_raw is not None and str(cause_raw).strip():
        cause = ExitCause(str(cause_raw).strip())
        if cause is ExitCause.STRATEGY_EXIT and sell is not None:
            exit_signal_id = f"exit-sig:{cycle_id}"
            signals[exit_signal_id] = _SignalInfo(
                instance_id=None, strategy_key=str(sell), role="SELL", pair_key=(
                    None if raw.get("pair") is None else str(raw.get("pair"))
                ),
                reference_price=None, bar_time=None, generated_at=None,
            )
            for leg in projection.exit_fills:
                projection.order_signal[leg.broker_order_id or ""] = exit_signal_id
                projection.order_purpose.setdefault(
                    leg.broker_order_id or "", "STRATEGY_EXIT"
                )
        elif cause is not ExitCause.STRATEGY_EXIT:
            for leg in projection.exit_fills:
                projection.order_purpose.setdefault(
                    leg.broker_order_id or "", _cause_to_purpose(cause)
                )
    trailing_ids = {str(item) for item in raw.get("trailing_order_ids", ())}
    updates = int(raw.get("trailing_update_count", 0))
    stop_raw = raw.get("last_confirmed_stop")
    stop = None if stop_raw is None else _parse_decimal(stop_raw, field="last_confirmed_stop")
    return projection, signals, trailing_ids, updates, stop, []


def _cause_to_purpose(cause: ExitCause) -> str:
    return {
        ExitCause.PROTECTIVE_STOP: "PROTECTIVE_STOP",
        ExitCause.TRAILING_STOP: "PROTECTIVE_STOP",
        ExitCause.MANUAL_CLOSE: "MANUAL_CLOSE",
        ExitCause.FLATTEN: "MANUAL_CLOSE",
        ExitCause.END_OF_DAY: "EOD_CLOSE",
        ExitCause.SAFETY: "SAFETY_EXIT",
        ExitCause.STRATEGY_EXIT: "STRATEGY_EXIT",
    }[cause]


# ------------------------------------------------------------------ builders (1)


def build_trade_cycles(source: Any, report_filter: ReportFilter) -> list[TradeCycleRecord]:
    """Build one :class:`TradeCycleRecord` per managed cycle in scope.

    ``source`` is a read connection (or ``ReadStore``) over the persisted
    projections, or an iterable of in-memory fixture mappings.  Only cycles
    matching ``report_filter`` are returned, ordered by
    ``(symbol, opened_at or closed_at or '', cycle_id)``.
    """
    if not isinstance(report_filter, ReportFilter):
        raise ValueError("report_filter must be a ReportFilter")
    scope = report_filter.account_scope_id
    assembled: list[TradeCycleRecord] = []
    if hasattr(source, "execute") or hasattr(source, "snapshot"):
        with _view(source, scope) as conn:
            projections, signals, _ = _load_projections(conn, scope)
            trailing_orders, updates, confirmed_stops = _load_trailing(conn, scope)
            flatten_commands = _load_flatten_commands(conn, scope)
            for projection in projections.values():
                record = _record_from_projection(
                    scope,
                    projection,
                    signals,
                    trailing_orders.get(projection.cycle_id, set()),
                    updates.get(projection.cycle_id, 0),
                    confirmed_stops.get(projection.cycle_id),
                    flatten_commands,
                )
                if _matches(record, report_filter):
                    assembled.append(record)
    else:
        if isinstance(source, (str, bytes)) or not isinstance(source, Iterable):
            raise TypeError("source must be a read connection or an iterable of fixtures")
        for raw in source:
            if not isinstance(raw, Mapping):
                raise ValueError("fixture rows must be mappings")
            if str(raw.get("account_scope_id", scope)) != scope:
                continue
            projection, signals, trailing_ids, update_count, stop, flatten = (
                _projection_from_fixture(raw)
            )
            record = _record_from_projection(
                scope, projection, signals, trailing_ids, update_count, stop, flatten
            )
            if _matches(record, report_filter):
                assembled.append(record)
    assembled.sort(
        key=lambda r: (
            r.symbol,
            r.opened_at.isoformat() if r.opened_at else (r.closed_at.isoformat() if r.closed_at else ""),
            r.cycle_id,
        )
    )
    return assembled


# --------------------------------------------------------------- aggregations (2)


def _summarize(
    cycles: Iterable[TradeCycleRecord],
    report_filter: ReportFilter | None,
    *,
    group: str,
    key_of: Any,
) -> dict[str, Any]:
    if report_filter is not None and not isinstance(report_filter, ReportFilter):
        raise ValueError("report_filter must be a ReportFilter or None")
    records = list(cycles)
    for record in records:
        if not isinstance(record, TradeCycleRecord):
            raise ValueError("cycles must be TradeCycleRecord instances")
        for money in (record.gross_pnl, record.net_pnl, record.fee_confirmed,
                      record.fee_estimated, record.filled_quantity):
            if not isinstance(money, Decimal):
                raise ValueError("cycle money values must be Decimal")
    if report_filter is not None:
        records = [r for r in records if _matches(r, report_filter)]
        label = report_filter.period_label
        scope = report_filter.account_scope_id
    else:
        scopes = {r.account_scope_id for r in records}
        if len(scopes) > 1:
            raise ValueError("cycles span multiple scopes; pass one ReportFilter")
        label = "lifetime"
        scope = next(iter(scopes)) if scopes else ""
    groups: dict[str, list[TradeCycleRecord]] = {}
    for record in records:
        groups.setdefault(key_of(record), []).append(record)
    rows: list[dict[str, Any]] = []
    for name in sorted(groups, key=lambda v: (v is None, "" if v is None else str(v))):
        rows.append(_group_stats(group, name, groups[name]))
    return {
        "account_scope_id": scope,
        "group_by": group,
        "period_label": label,
        "sample_size": len(records),
        "rows": rows,
        "performance_disclaimer": PERFORMANCE_DISCLAIMER,
    }


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, _ZERO) / Decimal(len(values))


def _mean_seconds(values: list[timedelta]) -> Decimal | None:
    if not values:
        return None
    total = sum((_duration_seconds(v) for v in values), _ZERO)
    return total / Decimal(len(values))


def _group_stats(group: str, name: str | None, records: list[TradeCycleRecord]) -> dict[str, Any]:
    count = Decimal(len(records))
    wins = [r for r in records if r.net_pnl > _ZERO]
    losses = [r for r in records if r.net_pnl < _ZERO]
    gross_wins = sum((r.gross_pnl for r in wins), _ZERO)
    gross_losses = sum((r.gross_pnl for r in losses), _ZERO)
    net_wins = sum((r.net_pnl for r in wins), _ZERO)
    net_losses = sum((r.net_pnl for r in losses), _ZERO)
    total_gross = sum((r.gross_pnl for r in records), _ZERO)
    total_net = sum((r.net_pnl for r in records), _ZERO)
    if count == _ZERO:
        win_rate = _ZERO
    else:
        win_rate = Decimal(len(wins)) / count
    if net_losses == _ZERO:
        profit_factor = None
    else:
        profit_factor = net_wins / (-net_losses)
    row: dict[str, Any] = {
        group: name,
        "trade_count": count,
        "wins": Decimal(len(wins)),
        "losses": Decimal(len(losses)),
        "win_rate": win_rate,
        "avg_win": _mean([r.net_pnl for r in wins]),
        "avg_loss": _mean([r.net_pnl for r in losses]),
        "profit_factor": profit_factor,
        "expectancy": (total_net / count) if count != _ZERO else _ZERO,
        "total_gross": total_gross,
        "total_net": total_net,
        "avg_holding_duration_seconds": _mean_seconds(
            [r.holding_duration for r in records if r.holding_duration is not None]
        ),
        "avg_signal_to_submit_seconds": _mean_seconds(
            [r.latency_signal_to_submit for r in records
             if r.latency_signal_to_submit is not None]
        ),
        "avg_submit_to_first_fill_seconds": _mean_seconds(
            [r.latency_submit_to_first_fill for r in records
             if r.latency_submit_to_first_fill is not None]
        ),
        "avg_first_to_final_fill_seconds": _mean_seconds(
            [r.latency_first_to_final_fill for r in records
             if r.latency_first_to_final_fill is not None]
        ),
        "avg_slippage_dollars": _mean(
            [r.slippage_dollars for r in records if r.slippage_dollars is not None]
        ),
        "avg_slippage_bps": _mean(
            [r.slippage_bps for r in records if r.slippage_bps is not None]
        ),
    }
    return row


def summarize_by_strategy(
    cycles: Iterable[TradeCycleRecord], report_filter: ReportFilter | None = None
) -> dict[str, Any]:
    """Aggregate cycles by originating BUY strategy."""
    return _summarize(cycles, report_filter, group="strategy",
                      key_of=lambda r: r.buy_strategy)


def summarize_by_pair(
    cycles: Iterable[TradeCycleRecord], report_filter: ReportFilter | None = None
) -> dict[str, Any]:
    """Aggregate cycles by strategy pair."""
    return _summarize(cycles, report_filter, group="pair", key_of=lambda r: r.pair)


def summarize_by_symbol(
    cycles: Iterable[TradeCycleRecord], report_filter: ReportFilter | None = None
) -> dict[str, Any]:
    """Aggregate cycles by symbol."""
    return _summarize(cycles, report_filter, group="symbol", key_of=lambda r: r.symbol)


# ------------------------------------------------------------------- counts (3)


def _strategy_scope_ids(
    conn: Any, scope: str, report_filter: ReportFilter
) -> tuple[set[str] | None, set[str] | None]:
    """Resolve buy/sell strategy filters to instance ids (None = unfiltered)."""
    if report_filter.buy_strategy is None and report_filter.sell_strategy is None:
        return None, None
    if not _table_exists(conn, "strategy_instances"):
        return set(), set()
    buys: set[str] = set()
    sells: set[str] = set()
    for instance_id, strategy_key, role in conn.execute(
        "SELECT instance_id, strategy_key, role FROM strategy_instances "
        "WHERE account_scope_id = ?",
        (scope,),
    ).fetchall():
        if report_filter.buy_strategy is not None and str(role) == "BUY" and str(
            strategy_key
        ) == report_filter.buy_strategy:
            buys.add(str(instance_id))
        if report_filter.sell_strategy is not None and str(role) == "SELL" and str(
            strategy_key
        ) == report_filter.sell_strategy:
            sells.add(str(instance_id))
    buy_ids = None if report_filter.buy_strategy is None else buys
    sell_ids = None if report_filter.sell_strategy is None else sells
    return buy_ids, sell_ids


def _in_window(moment: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    if moment is None:
        return start is None and end is None
    if start is not None and moment < start:
        return False
    if end is not None and moment >= end:
        return False
    return True


def count_evaluation_outcomes(source: Any, report_filter: ReportFilter) -> dict[str, Any]:
    """Count evaluation outcomes, rejection reasons and eligibility decisions.

    Reads ``strategy_evaluations`` (including NO_SIGNAL), ``decisions``,
    ``entry_eligibility_counters``, ``entry_lockouts`` and ``entry_cooldowns``
    for one scope, applying the symbol/strategy/period facets of the filter.
    """
    if not isinstance(report_filter, ReportFilter):
        raise ValueError("report_filter must be a ReportFilter")
    if not hasattr(source, "execute") and not hasattr(source, "snapshot"):
        raise TypeError("count_evaluation_outcomes requires a read connection source")
    scope = report_filter.account_scope_id
    start, end = report_filter.window()
    evaluation_counts: dict[str, int] = {}
    rejection_reasons: dict[str, int] = {}
    eligibility: dict[tuple[str, str], int] = {}
    evaluations_total = 0
    with _view(source, scope) as conn:
        buy_ids, sell_ids = _strategy_scope_ids(conn, scope, report_filter)
        if _table_exists(conn, "strategy_evaluations"):
            query = (
                "SELECT e.symbol, e.status, e.evaluated_at, e.instance_id FROM "
                "strategy_evaluations e WHERE e.account_scope_id = ?"
            )
            args: list[Any] = [scope]
            if report_filter.symbol is not None:
                query += " AND e.symbol = ?"
                args.append(report_filter.symbol)
            for symbol, status, evaluated_at, instance_id in conn.execute(
                query, tuple(args)
            ).fetchall():
                if buy_ids is not None and sell_ids is not None:
                    if str(instance_id) not in buy_ids and str(instance_id) not in sell_ids:
                        continue
                elif buy_ids is not None:
                    if str(instance_id) not in buy_ids:
                        continue
                elif sell_ids is not None:
                    if str(instance_id) not in sell_ids:
                        continue
                try:
                    moment = _schema.from_db_timestamp(str(evaluated_at))
                except ValueError:
                    continue
                if not _in_window(moment, start, end):
                    continue
                evaluations_total += 1
                evaluation_counts[str(status)] = evaluation_counts.get(str(status), 0) + 1
        signal_instances: dict[str, str] = {}
        if _table_exists(conn, "signals"):
            for signal_id, instance_id, symbol in conn.execute(
                "SELECT signal_id, instance_id, symbol FROM signals "
                "WHERE account_scope_id = ?",
                (scope,),
            ).fetchall():
                signal_instances[str(signal_id)] = str(instance_id)
        if _table_exists(conn, "decisions"):
            query = (
                "SELECT signal_id, outcome, reason, decided_at FROM decisions "
                "WHERE account_scope_id = ?"
            )
            for signal_id, outcome, reason, decided_at in conn.execute(query, (scope,)).fetchall():
                try:
                    moment = _schema.from_db_timestamp(str(decided_at))
                except ValueError:
                    continue
                if not _in_window(moment, start, end):
                    continue
                instance_id = signal_instances.get(str(signal_id)) if signal_id else None
                if buy_ids is not None and sell_ids is not None:
                    if instance_id not in buy_ids and instance_id not in sell_ids:
                        continue
                elif buy_ids is not None:
                    if instance_id not in buy_ids:
                        continue
                elif sell_ids is not None:
                    if instance_id not in sell_ids:
                        continue
                if str(outcome) == "REJECT":
                    rejection_reasons[str(reason)] = rejection_reasons.get(str(reason), 0) + 1
        if _table_exists(conn, "entry_eligibility_counters"):
            query = (
                "SELECT symbol, filled_entry_cycles, updated_at FROM "
                "entry_eligibility_counters WHERE account_scope_id = ?"
            )
            args = [scope]
            if report_filter.symbol is not None:
                query += " AND symbol = ?"
                args.append(report_filter.symbol)
            for symbol, filled, updated_at in conn.execute(query, tuple(args)).fetchall():
                try:
                    moment = _schema.from_db_timestamp(str(updated_at))
                except ValueError:
                    continue
                if not _in_window(moment, start, end):
                    continue
                if int(filled) > 0:
                    key = ("ENTRY_CAP_FILLED", str(symbol))
                    eligibility[key] = eligibility.get(key, 0) + int(filled)
        if _table_exists(conn, "entry_lockouts"):
            query = (
                "SELECT symbol, reason, locked_at FROM entry_lockouts "
                "WHERE account_scope_id = ?"
            )
            args = [scope]
            if report_filter.symbol is not None:
                query += " AND symbol = ?"
                args.append(report_filter.symbol)
            for symbol, reason, locked_at in conn.execute(query, tuple(args)).fetchall():
                try:
                    moment = _schema.from_db_timestamp(str(locked_at))
                except ValueError:
                    continue
                if not _in_window(moment, start, end):
                    continue
                key = (f"LOSS_LOCKOUT:{reason}", str(symbol))
                eligibility[key] = eligibility.get(key, 0) + 1
        if _table_exists(conn, "entry_cooldowns"):
            query = (
                "SELECT symbol, reason, started_at FROM entry_cooldowns "
                "WHERE account_scope_id = ?"
            )
            args = [scope]
            if report_filter.symbol is not None:
                query += " AND symbol = ?"
                args.append(report_filter.symbol)
            for symbol, reason, started_at in conn.execute(query, tuple(args)).fetchall():
                try:
                    moment = _schema.from_db_timestamp(str(started_at))
                except ValueError:
                    continue
                if not _in_window(moment, start, end):
                    continue
                key = (f"COOLDOWN:{reason}", str(symbol))
                eligibility[key] = eligibility.get(key, 0) + 1
    return {
        "account_scope_id": scope,
        "period_label": report_filter.period_label,
        "sample_size": evaluations_total,
        "evaluation_counts": [
            {"status": status, "count": evaluation_counts[status]}
            for status in sorted(evaluation_counts)
        ],
        "rejection_reasons": [
            {"reason": reason, "count": rejection_reasons[reason]}
            for reason in sorted(rejection_reasons)
        ],
        "eligibility_decisions": [
            {"decision": decision, "symbol": symbol, "count": eligibility[(decision, symbol)]}
            for decision, symbol in sorted(eligibility)
        ],
        "performance_disclaimer": PERFORMANCE_DISCLAIMER,
    }


# -------------------------------------------------------------- comparison (4)


def _comparison_key(record: TradeCycleRecord) -> tuple[str, str, str] | None:
    """Alignment key: (symbol, pair, entry signal bar timestamp or open time)."""
    moment = record.signal_bar_time
    if moment is None:
        moment = record.opened_at
    if moment is None:
        return None
    pair = record.pair or (
        f"{record.buy_strategy}+{record.sell_strategy}"
        if record.buy_strategy and record.sell_strategy
        else (record.buy_strategy or record.sell_strategy or "unpaired")
    )
    return (record.symbol, pair, moment.astimezone(UTC).isoformat())


def compare_accounts(
    source: Any,
    scope_a: str,
    scope_b: str,
    *,
    period: str = "lifetime",
    since: datetime | None = None,
    until: datetime | None = None,
    symbol: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Align two explicitly named accounts/scenarios trade by trade.

    Trades align on ``(symbol, strategy pair, entry signal bar timestamp)``.
    Matched trades report fill-price and P&L differences (side B minus side
    A); trades only on side A are ``missed``, only on side B are ``extra``.
    Balances are never summed across the two sides.
    """
    if not scope_a or not scope_b:
        raise ValueError("both scope_a and scope_b must be named")
    if scope_a == scope_b:
        raise ValueError("scope_a and scope_b must differ")
    filter_a = ReportFilter(
        account_scope_id=scope_a, period=period, since=since, until=until,
        symbol=symbol, as_of=as_of,
    )
    filter_b = ReportFilter(
        account_scope_id=scope_b, period=period, since=since, until=until,
        symbol=symbol, as_of=as_of,
    )
    cycles_a = [c for c in build_trade_cycles(source, filter_a)
                if c.entry_fill_count > 0]
    cycles_b = [c for c in build_trade_cycles(source, filter_b)
                if c.entry_fill_count > 0]
    by_key_a: dict[tuple[str, str, str], list[TradeCycleRecord]] = {}
    by_key_b: dict[tuple[str, str, str], list[TradeCycleRecord]] = {}
    unkeyed_a = 0
    unkeyed_b = 0
    for record in cycles_a:
        key = _comparison_key(record)
        if key is None:
            unkeyed_a += 1
        else:
            by_key_a.setdefault(key, []).append(record)
    for record in cycles_b:
        key = _comparison_key(record)
        if key is None:
            unkeyed_b += 1
        else:
            by_key_b.setdefault(key, []).append(record)
    matched: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    for key in sorted(set(by_key_a) | set(by_key_b)):
        left = sorted(by_key_a.get(key, []), key=lambda r: r.cycle_id)
        right = sorted(by_key_b.get(key, []), key=lambda r: r.cycle_id)
        pairs = min(len(left), len(right))
        for index in range(pairs):
            a_record, b_record = left[index], right[index]
            matched.append({
                "symbol": key[0],
                "pair": key[1],
                "signal_bar_time": key[2],
                "cycle_a": a_record.cycle_id,
                "cycle_b": b_record.cycle_id,
                "entry_price_a": a_record.entry_avg_price,
                "entry_price_b": b_record.entry_avg_price,
                "entry_price_diff": _optional_diff(
                    b_record.entry_avg_price, a_record.entry_avg_price
                ),
                "exit_price_a": a_record.exit_avg_price,
                "exit_price_b": b_record.exit_avg_price,
                "exit_price_diff": _optional_diff(
                    b_record.exit_avg_price, a_record.exit_avg_price
                ),
                "net_a": a_record.net_pnl,
                "net_b": b_record.net_pnl,
                "net_diff": b_record.net_pnl - a_record.net_pnl,
            })
        for record in left[pairs:]:
            missed.append({
                "symbol": key[0],
                "pair": key[1],
                "signal_bar_time": key[2],
                "cycle_a": record.cycle_id,
                "cycle_b": None,
                "net_a": record.net_pnl,
            })
        for record in right[pairs:]:
            extra.append({
                "symbol": key[0],
                "pair": key[1],
                "signal_bar_time": key[2],
                "cycle_a": None,
                "cycle_b": record.cycle_id,
                "net_b": record.net_pnl,
            })
    label = filter_a.period_label
    return {
        "scope_a": scope_a,
        "scope_b": scope_b,
        "period_label": label,
        "sample_size_a": len(cycles_a),
        "sample_size_b": len(cycles_b),
        "sample_size": len(matched),
        "matched": matched,
        "missed": missed,
        "extra": extra,
        "unkeyed_a": unkeyed_a,
        "unkeyed_b": unkeyed_b,
        "performance_disclaimer": PERFORMANCE_DISCLAIMER,
    }


def _optional_diff(new: Decimal | None, old: Decimal | None) -> Decimal | None:
    if new is None or old is None:
        return None
    return new - old
