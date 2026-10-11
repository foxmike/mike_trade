"""Read-only dashboard projections over the c.1 SQLite schema.

Design choices (four, within the task's decision budget):
1. Realized cycle P&L is derived from scoped fills: exit value less entry value
   and all fees. Closed cycles without fills contribute zero.
2. “Today” means the UTC calendar date of the injected ``now``; timestamps in
   the schema are canonical UTC text.
3. A value becomes stale only when its age is strictly greater than its age
   threshold. Missing heartbeat data is OFFLINE; missing snapshots stay None.
4. Entry maximum is not stored in c.1, so ``max_filled_entry_cycles`` stays
   None. Protection remaining quantity is read from the matching scoped row in
   ``positions``; absent positions have zero remaining quantity.

All functions require an account scope, and every SQL read includes that scope.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Iterator

from mike_trade.persistence import schema
from mike_trade.persistence.readstore import ReadStore

__all__ = [
    "EngineStatusView", "AccountSnapshotView", "SymbolWatchRow", "PositionView",
    "OrderView", "ProtectionView", "EligibilityView", "SignalView",
    "RecentTradeView", "ReconciliationStatusView", "LatestStatus",
    "engine_status", "account_snapshot", "symbol_watch", "positions", "orders",
    "protections", "eligibility", "signals", "recent_trades",
    "reconciliation_status", "load_latest_status",
]

_ZERO = Decimal("0")
_OPEN_ORDER = ("NEW", "PENDING_NEW", "ACCEPTED", "PARTIALLY_FILLED", "PENDING_CANCEL")


@dataclass(frozen=True, slots=True)
class EngineStatusView:
    as_of: datetime
    last_heartbeat_at: datetime | None
    heartbeat_age: timedelta | None
    state: str
    paused: bool
    entry_kill_switch: bool
    has_unresolved_drift: bool


@dataclass(frozen=True, slots=True)
class AccountSnapshotView:
    as_of: datetime
    cash: Decimal | None
    equity: Decimal | None
    buying_power: Decimal | None
    snapshot_as_of: datetime | None
    is_stale: bool | None
    realized_pnl_today: Decimal
    realized_pnl_total: Decimal
    open_position_count: int
    active_budget_reservations: Decimal


@dataclass(frozen=True, slots=True)
class SymbolWatchRow:
    as_of: datetime
    symbol: str
    last_completed_bar_at: datetime | None
    feed_provenance: str | None
    data_stale: bool | None
    last_signal: str | None
    last_signal_at: datetime | None
    open_cycle_state: str | None


@dataclass(frozen=True, slots=True)
class PositionView:
    as_of: datetime
    symbol: str
    cycle_id: str | None
    management: str
    direction: str
    quantity: Decimal
    average_entry_price: Decimal
    market_price: Decimal | None
    market_value: Decimal | None
    unrealized_pl: Decimal | None


@dataclass(frozen=True, slots=True)
class OrderView:
    as_of: datetime
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal
    submitted_at: datetime
    updated_at: datetime
    is_open: bool


@dataclass(frozen=True, slots=True)
class ProtectionView:
    as_of: datetime
    protection_order_id: str
    cycle_id: str
    symbol: str
    broker_order_id: str | None
    kind: str
    status: str
    stop_price: Decimal | None
    high_water_mark: Decimal | None
    covered_quantity: Decimal
    remaining_quantity: Decimal
    coverage_gap: bool


@dataclass(frozen=True, slots=True)
class EligibilityView:
    as_of: datetime
    symbol: str
    session_date: date
    filled_entry_cycles_today: int
    max_filled_entry_cycles: int | None
    consecutive_losses: int
    lockout_until: datetime | None
    cooldown_until: datetime | None
    block_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SignalView:
    as_of: datetime
    signal_id: str
    symbol: str
    action: str
    reference_price: Decimal | None
    reason: str
    bar_time: datetime
    generated_at: datetime


@dataclass(frozen=True, slots=True)
class RecentTradeView:
    as_of: datetime
    cycle_id: str
    symbol: str
    direction: str
    realized_pnl: Decimal
    opened_at: datetime | None
    closed_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationStatusView:
    as_of: datetime
    last_run_at: datetime | None
    last_result: str | None
    unresolved_drift_items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LatestStatus:
    as_of: datetime
    account_scope_id: str
    engine: EngineStatusView
    account: AccountSnapshotView
    symbols: tuple[SymbolWatchRow, ...]
    positions: tuple[PositionView, ...]
    orders: tuple[OrderView, ...]
    protections: tuple[ProtectionView, ...]
    eligibility: tuple[EligibilityView, ...]
    signals: tuple[SignalView, ...]
    recent_trades: tuple[RecentTradeView, ...]
    reconciliation: ReconciliationStatusView


def _now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    return value.astimezone(UTC)


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 500:
        raise ValueError("limit must be an integer from 1 to 500")
    return value


def _dt(value: str | None) -> datetime | None:
    return None if value is None else schema.from_db_timestamp(value)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else schema.decimal_from_db(value)


@contextmanager
def _read(source, account_scope_id: str) -> Iterator:
    if not isinstance(account_scope_id, str) or not account_scope_id.strip():
        raise ValueError("account_scope_id must be a non-empty string")
    if isinstance(source, ReadStore):
        with source.snapshot(account_scope_id) as conn:
            yield conn
    else:
        yield source


def _realized(conn, scope: str, *, closed_after: str | None = None,
              closed_before: str | None = None) -> Decimal:
    predicate = (" AND pc.closed_at >= ?" if closed_after is not None else "")
    predicate += (" AND pc.closed_at < ?" if closed_before is not None else "")
    params: tuple[object, ...] = (scope, "CLOSED")
    if closed_after is not None:
        params += (closed_after,)
    if closed_before is not None:
        params += (closed_before,)
    cycles = conn.execute(
        "SELECT pc.cycle_id FROM position_cycles pc WHERE pc.account_scope_id = ? "
        "AND pc.state = ? AND pc.closed_at IS NOT NULL" + predicate,
        params,
    ).fetchall()
    total = _ZERO
    for (cycle_id,) in cycles:
        rows = conn.execute(
            "SELECT f.side, f.quantity, f.price, f.fee FROM fills f "
            "WHERE f.account_scope_id = ? AND f.cycle_id = ? ORDER BY f.filled_at",
            (scope, cycle_id),
        ).fetchall()
        for side, quantity, price, fee in rows:
            value = schema.decimal_from_db(quantity) * schema.decimal_from_db(price)
            # cash flow: sells (SELL, SELL_SHORT) credit, buys (BUY, BUY_TO_COVER) debit, so the
            # net of a closed cycle is its P&L for a long and for a short alike
            total += (value if side in ("SELL", "SELL_SHORT") else -value) - schema.decimal_from_db(fee)
    return total


def engine_status(source, account_scope_id: str, *, now: datetime,
                  stale_after: timedelta = timedelta(seconds=30),
                  offline_after: timedelta = timedelta(minutes=2)) -> EngineStatusView:
    current = _now(now)
    with _read(source, account_scope_id) as conn:
        row = conn.execute(
            "SELECT heartbeat_at, observed_at FROM service_health "
            "WHERE account_scope_id = ? AND component = 'engine'",
            (account_scope_id,),
        ).fetchone()
        control = conn.execute(
            "SELECT paused, kill_switch_enabled, updated_at FROM entry_controls "
            "WHERE account_scope_id = ?", (account_scope_id,),
        ).fetchone()
        drift = conn.execute(
            "SELECT MAX(d.detected_at) FROM reconciliation_drifts d "
            "LEFT JOIN reconciliation_resolutions r ON r.account_scope_id = d.account_scope_id "
            "AND r.drift_id = d.drift_id WHERE d.account_scope_id = ? AND r.resolution_id IS NULL",
            (account_scope_id,),
        ).fetchone()[0]
    heartbeat = _dt(row[0]) if row else None
    age = current - heartbeat if heartbeat is not None else None
    state = "OFFLINE" if age is None or age > offline_after else "STALE" if age > stale_after else "ONLINE"
    component_times = [_dt(row[1]) if row else None, _dt(control[2]) if control else None, _dt(drift)]
    as_of = max((item for item in component_times if item is not None), default=current)
    return EngineStatusView(as_of, heartbeat, age, state,
                            bool(control[0]) if control else False,
                            bool(control[1]) if control else False, drift is not None)


def account_snapshot(source, account_scope_id: str, *, now: datetime,
                     stale_after: timedelta = timedelta(minutes=5)) -> AccountSnapshotView:
    current = _now(now)
    with _read(source, account_scope_id) as conn:
        row = conn.execute(
            "SELECT cash, equity, buying_power, as_of FROM equity_snapshots "
            "WHERE account_scope_id = ? ORDER BY as_of DESC LIMIT 1", (account_scope_id,),
        ).fetchone()
        open_count = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE account_scope_id = ?", (account_scope_id,),
        ).fetchone()[0]
        budget_rows = conn.execute(
            "SELECT amount FROM budget_reservations WHERE account_scope_id = ? AND state = 'ACTIVE'",
            (account_scope_id,),
        ).fetchall()
        pnl_total = _realized(conn, account_scope_id)
        midnight = datetime.combine(current.date(), datetime.min.time(), tzinfo=UTC)
        tomorrow = midnight + timedelta(days=1)
        pnl_today = _realized(conn, account_scope_id,
                              closed_after=schema.to_db_timestamp(midnight),
                              closed_before=schema.to_db_timestamp(tomorrow))
    snapshot_at = _dt(row[3]) if row else None
    return AccountSnapshotView(
        current, _dec(row[0]) if row else None, _dec(row[1]) if row else None,
        _dec(row[2]) if row else None, snapshot_at,
        (current - snapshot_at > stale_after) if snapshot_at else None,
        pnl_today, pnl_total, int(open_count), sum((schema.decimal_from_db(r[0]) for r in budget_rows), _ZERO),
    )


def symbol_watch(source, account_scope_id: str, *, now: datetime,
                 stale_after: timedelta = timedelta(minutes=5), limit: int = 500) -> tuple[SymbolWatchRow, ...]:
    current = _now(now)
    _limit(limit)
    with _read(source, account_scope_id) as conn:
        symbols = conn.execute(
            "SELECT symbol, observed_at FROM instruments WHERE account_scope_id = ? "
            "ORDER BY observed_at DESC LIMIT ?", (account_scope_id, limit),
        ).fetchall()
        result = []
        completed_before = schema.to_db_timestamp(current.replace(second=0, microsecond=0))
        for symbol, observed_at in symbols:
            bar = conn.execute(
                "SELECT bar_start, feed, provenance FROM market_bars WHERE account_scope_id = ? "
                "AND symbol = ? AND timeframe = '1m' AND bar_start < ? "
                "ORDER BY bar_start DESC LIMIT 1", (account_scope_id, symbol, completed_before),
            ).fetchone()
            signal = conn.execute(
                "SELECT action, generated_at FROM signals WHERE account_scope_id = ? AND symbol = ? "
                "ORDER BY generated_at DESC LIMIT 1", (account_scope_id, symbol),
            ).fetchone()
            cycle = conn.execute(
                "SELECT state FROM position_cycles WHERE account_scope_id = ? AND symbol = ? "
                "AND state IN ('PENDING_ENTRY','OPEN','EXITING') ORDER BY updated_at DESC LIMIT 1",
                (account_scope_id, symbol),
            ).fetchone()
            bar_at = _dt(bar[0]) if bar else None
            result.append(SymbolWatchRow(
                _dt(observed_at) or current, symbol, bar_at,
                (bar[1] + (f" ({bar[2]})" if bar[2] else "")) if bar else None,
                (current - bar_at > stale_after) if bar_at else None,
                signal[0] if signal else None, _dt(signal[1]) if signal else None,
                cycle[0] if cycle else None,
            ))
    return tuple(result)


def positions(source, account_scope_id: str, *, now: datetime, limit: int = 500) -> tuple[PositionView, ...]:
    current = _now(now); _limit(limit)
    with _read(source, account_scope_id) as conn:
        rows = conn.execute(
            "SELECT symbol, cycle_id, management, direction, quantity, average_entry_price, "
            "market_price, market_value, unrealized_pl, as_of FROM positions "
            "WHERE account_scope_id = ? ORDER BY as_of DESC LIMIT ?", (account_scope_id, limit),
        ).fetchall()
    return tuple(PositionView(_dt(r[9]) or current, r[0], r[1], r[2], r[3],
                              schema.decimal_from_db(r[4]), schema.decimal_from_db(r[5]),
                              _dec(r[6]), _dec(r[7]), _dec(r[8])) for r in rows)


def orders(source, account_scope_id: str, *, now: datetime, limit: int = 50) -> tuple[OrderView, ...]:
    current = _now(now); _limit(limit)
    with _read(source, account_scope_id) as conn:
        rows = conn.execute(
            "SELECT broker_order_id, client_order_id, symbol, side, order_type, status, quantity, "
            "filled_quantity, submitted_at, updated_at FROM broker_orders "
            "WHERE account_scope_id = ? ORDER BY updated_at DESC LIMIT ?", (account_scope_id, limit),
        ).fetchall()
    return tuple(OrderView(_dt(r[9]) or current, r[0], r[1], r[2], r[3], r[4], r[5],
                           schema.decimal_from_db(r[6]), schema.decimal_from_db(r[7]),
                           _dt(r[8]) or current, _dt(r[9]) or current, r[5] in _OPEN_ORDER) for r in rows)


def protections(source, account_scope_id: str, *, now: datetime, limit: int = 50) -> tuple[ProtectionView, ...]:
    current = _now(now); _limit(limit)
    with _read(source, account_scope_id) as conn:
        rows = conn.execute(
            "SELECT p.protection_order_id, p.cycle_id, pc.symbol, p.broker_order_id, p.kind, p.status, "
            "COALESCE(t.stop_price, p.stop_price), t.high_water_mark, p.covered_quantity, "
            "COALESCE(pos.quantity, '0'), p.updated_at FROM protection_orders p "
            "JOIN position_cycles pc ON pc.account_scope_id = p.account_scope_id AND pc.cycle_id = p.cycle_id "
            "LEFT JOIN trailing_stop_states t ON t.account_scope_id = p.account_scope_id AND t.cycle_id = p.cycle_id "
            "LEFT JOIN positions pos ON pos.account_scope_id = p.account_scope_id AND pos.cycle_id = p.cycle_id "
            "WHERE p.account_scope_id = ? ORDER BY p.updated_at DESC LIMIT ?", (account_scope_id, limit),
        ).fetchall()
    result = []
    for r in rows:
        covered, remaining = schema.decimal_from_db(r[8]), schema.decimal_from_db(r[9])
        result.append(ProtectionView(_dt(r[10]) or current, r[0], r[1], r[2], r[3], r[4], r[5],
                                    _dec(r[6]), _dec(r[7]), covered, remaining, covered < remaining))
    return tuple(result)


def eligibility(source, account_scope_id: str, *, now: datetime, limit: int = 500) -> tuple[EligibilityView, ...]:
    current = _now(now); _limit(limit); day = current.date()
    with _read(source, account_scope_id) as conn:
        rows = conn.execute(
            "SELECT symbol, SUM(filled_entry_cycles), MAX(consecutive_losses), MAX(updated_at) "
            "FROM entry_eligibility_counters WHERE account_scope_id = ? AND session_date = ? "
            "GROUP BY symbol ORDER BY MAX(updated_at) DESC LIMIT ?",
            (account_scope_id, day.isoformat(), limit),
        ).fetchall()
        result = []
        for symbol, filled, losses, updated in rows:
            lock, lock_present = conn.execute(
                "SELECT MAX(locked_until), COUNT(*) FROM entry_lockouts WHERE account_scope_id = ? AND symbol = ? "
                "AND cleared_at IS NULL AND (locked_until IS NULL OR locked_until > ?)",
                (account_scope_id, symbol, schema.to_db_timestamp(current)),
            ).fetchone()
            cool = conn.execute(
                "SELECT MAX(cooldown_until) FROM entry_cooldowns WHERE account_scope_id = ? AND symbol = ? "
                "AND cleared_at IS NULL AND cooldown_until > ?",
                (account_scope_id, symbol, schema.to_db_timestamp(current)),
            ).fetchone()[0]
            reasons = []
            if lock_present: reasons.append("LOCKOUT")
            if cool is not None: reasons.append("COOLDOWN")
            result.append(EligibilityView(_dt(updated) or current, symbol, day, int(filled), None,
                                          int(losses), _dt(lock), _dt(cool), tuple(reasons)))
    return tuple(result)


def signals(source, account_scope_id: str, *, now: datetime, limit: int = 50) -> tuple[SignalView, ...]:
    current = _now(now); _limit(limit)
    with _read(source, account_scope_id) as conn:
        rows = conn.execute(
            "SELECT signal_id, symbol, action, reference_price, reason, bar_time, generated_at FROM signals "
            "WHERE account_scope_id = ? ORDER BY generated_at DESC LIMIT ?", (account_scope_id, limit),
        ).fetchall()
    return tuple(SignalView(_dt(r[6]) or current, r[0], r[1], r[2], _dec(r[3]), r[4],
                            _dt(r[5]) or current, _dt(r[6]) or current) for r in rows)


def recent_trades(source, account_scope_id: str, *, now: datetime,
                  limit: int = 50) -> tuple[RecentTradeView, ...]:
    current = _now(now); _limit(limit)
    with _read(source, account_scope_id) as conn:
        rows = conn.execute(
            "SELECT cycle_id, symbol, direction, opened_at, closed_at FROM position_cycles "
            "WHERE account_scope_id = ? AND state = 'CLOSED' AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT ?", (account_scope_id, limit),
        ).fetchall()
        result = []
        for cycle_id, symbol, direction, opened, closed in rows:
            pnl = _ZERO
            fills = conn.execute(
                "SELECT side, quantity, price, fee FROM fills WHERE account_scope_id = ? AND cycle_id = ? "
                "ORDER BY filled_at", (account_scope_id, cycle_id),
            ).fetchall()
            for side, quantity, price, fee in fills:
                value = schema.decimal_from_db(quantity) * schema.decimal_from_db(price)
                pnl += (value if side in ("SELL", "SELL_SHORT") else -value) - schema.decimal_from_db(fee)
            result.append(RecentTradeView(_dt(closed) or current, cycle_id, symbol, direction,
                                          pnl, _dt(opened), _dt(closed) or current))
    return tuple(result)


def reconciliation_status(source, account_scope_id: str, *, now: datetime,
                          limit: int = 500) -> ReconciliationStatusView:
    current = _now(now); _limit(limit)
    with _read(source, account_scope_id) as conn:
        run = conn.execute(
            "SELECT completed_at, status, started_at FROM reconciliation_runs "
            "WHERE account_scope_id = ? ORDER BY started_at DESC LIMIT 1", (account_scope_id,),
        ).fetchone()
        drifts = conn.execute(
            "SELECT d.drift_id FROM reconciliation_drifts d LEFT JOIN reconciliation_resolutions r "
            "ON r.account_scope_id = d.account_scope_id AND r.drift_id = d.drift_id "
            "WHERE d.account_scope_id = ? AND r.resolution_id IS NULL "
            "ORDER BY d.detected_at DESC LIMIT ?", (account_scope_id, limit),
        ).fetchall()
    as_of = _dt(run[0] or run[2]) if run else current
    return ReconciliationStatusView(as_of or current, as_of,
                                    run[1] if run else None, tuple(row[0] for row in drifts))


def load_latest_status(read_store, account_scope_id: str, *, now: datetime,
                       stale_after: timedelta = timedelta(seconds=30),
                       offline_after: timedelta = timedelta(minutes=2)) -> LatestStatus:
    current = _now(now)
    if isinstance(read_store, ReadStore):
        with read_store.snapshot(account_scope_id) as conn:
            return _load(conn, account_scope_id, current, stale_after, offline_after)
    return _load(read_store, account_scope_id, current, stale_after, offline_after)


def _load(conn, scope: str, now: datetime, stale_after: timedelta,
          offline_after: timedelta) -> LatestStatus:
    # The caller owns one read transaction around every component query.
    engine = engine_status(conn, scope, now=now, stale_after=stale_after, offline_after=offline_after)
    account = account_snapshot(conn, scope, now=now)
    return LatestStatus(
        now, scope, engine, account, symbol_watch(conn, scope, now=now),
        positions(conn, scope, now=now), orders(conn, scope, now=now),
        protections(conn, scope, now=now), eligibility(conn, scope, now=now),
        signals(conn, scope, now=now), recent_trades(conn, scope, now=now),
        reconciliation_status(conn, scope, now=now),
    )
