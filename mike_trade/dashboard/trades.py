"""Read-only unified trade journal projections for operations and research artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from mike_trade.persistence import schema
from mike_trade.persistence.readstore import ReadStore, ReadStoreUnavailableError

NY = ZoneInfo("America/New_York")
ZERO = Decimal("0")
ACCEPTED = frozenset({"SELECTED", "ACCEPTED", "APPROVED"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TradeFill(_Model):
    fill_id: str
    order_id: str | None = None
    side: str
    timestamp: datetime
    price: Decimal
    quantity: Decimal
    fee: Decimal


class TradeRecord(_Model):
    trade_id: str
    execution_mode: str
    account_id: str
    scenario_id: str | None = None
    research_run_id: str | None = None
    symbol: str
    cycle_id: str
    entry_timestamp: datetime | None = None
    exit_timestamp: datetime | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    filled_quantity: Decimal
    exited_quantity: Decimal
    open_quantity: Decimal
    entry_notional: Decimal | None = None
    exit_notional: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_return_percent: Decimal | None = None
    position_status: str
    entry_strategy_id: str | None = None
    entry_strategy_version: str | None = None
    entry_signal_id: str | None = None
    entry_signal_timestamp: datetime | None = None
    entry_decision: str | None = None
    exit_strategy_id: str | None = None
    exit_strategy_version: str | None = None
    exit_signal_id: str | None = None
    exit_signal_timestamp: datetime | None = None
    exit_reason: str | None = None
    protective_stop_mode: str | None = None
    initial_stop_price: Decimal | None = None
    final_stop_price: Decimal | None = None
    holding_duration_seconds: Decimal | None = None
    data_provenance: str | None = None
    attribution_complete: bool


class TradeList(_Model):
    items: list[TradeRecord]
    page: int
    page_size: int
    total: int
    pages: int
    available_entry_strategies: list[str]
    available_exit_reasons: list[str]


class TradeBar(_Model):
    timestamp: datetime
    timestamp_et: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vwap: Decimal | None = None
    volume: Decimal
    feed: str | None = None
    provenance: str | None = None


class TradeEvent(_Model):
    timestamp: datetime
    timestamp_et: str
    event_type: str
    source: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    signal_id: str | None = None
    order_id: str | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    reason: str | None = None
    details: dict[str, Any] = {}


class TradeDiagnostics(_Model):
    basis: str
    observations_available: bool
    observations_through: datetime | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    quantity: Decimal
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_return_percent: Decimal | None = None
    holding_duration_seconds: Decimal | None = None
    maximum_favorable_excursion: Decimal | None = None
    maximum_adverse_excursion: Decimal | None = None
    maximum_unrealized_gain: Decimal | None = None
    maximum_unrealized_loss: Decimal | None = None
    best_price: Decimal | None = None
    worst_price: Decimal | None = None
    initial_stop_distance: Decimal | None = None
    final_stop_distance: Decimal | None = None
    exit_reason: str | None = None
    limitation: str | None = None


class TradeInspection(_Model):
    trade: TradeRecord
    fills: list[TradeFill]
    bars: list[TradeBar]
    events: list[TradeEvent]
    diagnostics: TradeDiagnostics
    bars_available: bool
    bars_limitation: str | None = None
    timezone: Literal["America/New_York"] = "America/New_York"


class ResearchRun(_Model):
    run_id: str
    created_at: datetime | None = None
    symbols: list[str]
    period_role: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    dataset_id: str | None = None
    config_fingerprint: str | None = None
    artifact_version: str | None = None


class ResearchRuns(_Model):
    runs: list[ResearchRun]


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _weighted(rows: list[dict[str, Any]]) -> tuple[Decimal, Decimal | None, Decimal, Decimal]:
    qty = sum((_decimal(row.get("quantity")) or ZERO for row in rows), ZERO)
    notional = sum(
        (
            (_decimal(row.get("quantity")) or ZERO) * (_decimal(row.get("price")) or ZERO)
            for row in rows
        ),
        ZERO,
    )
    fees = sum((_decimal(row.get("fee")) or ZERO for row in rows), ZERO)
    return qty, (notional / qty if qty else None), notional, fees


def _status(state: str, bought: Decimal, sold: Decimal) -> str:
    if state == "CLOSED" or (bought > ZERO and sold >= bought):
        return "CLOSED"
    if sold > ZERO:
        return "PARTIALLY_CLOSED"
    return state


@dataclass(frozen=True)
class TradeQuery:
    page: int = 1
    page_size: int = 50
    symbol: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    entry_strategy: str | None = None
    exit_filter: str | None = None
    outcome: str | None = None
    status: str | None = None
    sort: str = "entry_timestamp"
    direction: str = "desc"

    def __post_init__(self) -> None:
        if self.page < 1 or not 1 <= self.page_size <= 200:
            raise ValueError("page must be >= 1 and page_size must be between 1 and 200")


class TradeJournalService:
    """Project immutable dashboard data without importing execution or strategy code."""

    def __init__(
        self,
        db_path: Path,
        research_root: Path,
        dataset_root: Path,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.research_root = Path(research_root)
        self.dataset_root = Path(dataset_root)
        self._now = now or (lambda: datetime.now(UTC))

    def _operational(self, account_id: str):
        try:
            store = ReadStore.open(self.db_path)
        except ReadStoreUnavailableError as exc:
            raise RuntimeError("database unavailable") from exc
        scopes = {str(row["account_scope_id"]): row for row in store.list_account_scopes()}
        if account_id not in scopes:
            store.close()
            raise LookupError(account_id)
        return store, scopes[account_id]

    @staticmethod
    def _attribution(conn: Any, scope: str, cycle: str, purpose: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT s.signal_id,s.generated_at,i.strategy_key,i.strategy_version,d.outcome,"
            "d.reason,n.created_at FROM order_intents n "
            "LEFT JOIN signals s ON s.account_scope_id=n.account_scope_id AND s.signal_id=n.signal_id "
            "LEFT JOIN strategy_instances i ON i.account_scope_id=s.account_scope_id "
            "AND i.instance_id=s.instance_id LEFT JOIN decisions d "
            "ON d.account_scope_id=s.account_scope_id AND d.signal_id=s.signal_id "
            "WHERE n.account_scope_id=? AND n.cycle_id=? AND n.purpose=? "
            "ORDER BY n.created_at LIMIT 1",
            (scope, cycle, purpose),
        ).fetchone()
        if row is None:
            return {}
        return {
            "signal_id": row[0],
            "signal_time": _time(row[1]),
            "strategy": row[2],
            "version": row[3],
            "decision": row[4],
            "decision_reason": row[5],
            "intent_time": _time(row[6]),
        }

    def _operation_records(self, account_id: str) -> list[TradeRecord]:
        store, scope_row = self._operational(account_id)
        try:
            with store.snapshot(account_id) as conn:
                cycles = conn.execute(
                    "SELECT cycle_id,symbol,state,entry_strategy_key,opened_at,closed_at,created_at "
                    "FROM position_cycles WHERE account_scope_id=? AND state IN "
                    "('OPEN','EXITING','CLOSED') ORDER BY COALESCE(opened_at,created_at)",
                    (account_id,),
                ).fetchall()
                records: list[TradeRecord] = []
                for cycle, symbol, state, legacy_strategy, opened, closed, created in cycles:
                    raw_fills = conn.execute(
                        "SELECT fill_id,broker_order_id,side,quantity,price,fee,filled_at "
                        "FROM fills WHERE account_scope_id=? AND cycle_id=? ORDER BY filled_at,fill_id",
                        (account_id, cycle),
                    ).fetchall()
                    fills = [
                        {
                            "fill_id": r[0],
                            "order_id": r[1],
                            "side": r[2],
                            "quantity": r[3],
                            "price": r[4],
                            "fee": r[5],
                            "time": r[6],
                        }
                        for r in raw_fills
                    ]
                    buys = [f for f in fills if f["side"] == "BUY"]
                    sells = [f for f in fills if f["side"] == "SELL"]
                    bq, bp, bn, bf = _weighted(buys)
                    sq, sp, sn, sf = _weighted(sells)
                    entry = self._attribution(conn, account_id, cycle, "ENTRY")
                    exit_attr = self._attribution(conn, account_id, cycle, "STRATEGY_EXIT")
                    exit_intents = conn.execute(
                        "SELECT n.purpose,n.signal_id,n.created_at,b.broker_order_id "
                        "FROM order_intents n LEFT JOIN broker_orders b "
                        "ON b.account_scope_id=n.account_scope_id AND b.intent_id=n.intent_id "
                        "WHERE n.account_scope_id=? AND n.cycle_id=? AND n.side='SELL' "
                        "ORDER BY n.created_at",
                        (account_id, cycle),
                    ).fetchall()
                    purposes = list(dict.fromkeys(str(r[0]) for r in exit_intents))
                    exit_reason = (
                        purposes[0] if len(purposes) == 1 else ("MULTIPLE" if purposes else None)
                    )
                    protection = conn.execute(
                        "SELECT kind,stop_price,created_at FROM protection_orders "
                        "WHERE account_scope_id=? AND cycle_id=? ORDER BY created_at",
                        (account_id, cycle),
                    ).fetchall()
                    stop_events = conn.execute(
                        "SELECT new_stop_price FROM trailing_stop_events WHERE account_scope_id=? "
                        "AND cycle_id=? AND new_stop_price IS NOT NULL ORDER BY occurred_at",
                        (account_id, cycle),
                    ).fetchall()
                    initial_stop = _decimal(protection[0][1]) if protection else None
                    stop_values = [
                        _decimal(r[0]) for r in stop_events if _decimal(r[0]) is not None
                    ]
                    final_stop = (
                        stop_values[-1]
                        if stop_values
                        else (_decimal(protection[-1][1]) if protection else None)
                    )
                    pos = conn.execute(
                        "SELECT unrealized_pl FROM positions WHERE account_scope_id=? AND cycle_id=?",
                        (account_id, cycle),
                    ).fetchone()
                    realized = None
                    realized_return = None
                    if bp is not None and sq > ZERO:
                        entry_fee_share = bf * min(Decimal(1), sq / bq) if bq else ZERO
                        realized = sn - bp * sq - sf - entry_fee_share
                        basis = bp * sq + entry_fee_share
                        realized_return = realized / basis * Decimal(100) if basis else None
                    entry_at = _time(buys[0]["time"]) if buys else _time(opened or created)
                    exit_at = _time(sells[-1]["time"]) if sells else _time(closed)
                    duration_end = exit_at if exit_at is not None else self._now().astimezone(UTC)
                    records.append(
                        TradeRecord(
                            trade_id=str(cycle),
                            execution_mode=str(scope_row["environment"]),
                            account_id=account_id,
                            scenario_id=scope_row["scenario_name"],
                            research_run_id=scope_row.get("run_id"),
                            symbol=str(symbol),
                            cycle_id=str(cycle),
                            entry_timestamp=entry_at,
                            exit_timestamp=exit_at,
                            entry_price=bp,
                            exit_price=sp,
                            filled_quantity=bq,
                            exited_quantity=sq,
                            open_quantity=max(ZERO, bq - sq),
                            entry_notional=bn if buys else None,
                            exit_notional=sn if sells else None,
                            realized_pnl=realized,
                            unrealized_pnl=_decimal(pos[0]) if pos else None,
                            realized_return_percent=realized_return,
                            position_status=_status(str(state), bq, sq),
                            entry_strategy_id=entry.get("strategy") or legacy_strategy,
                            entry_strategy_version=entry.get("version"),
                            entry_signal_id=entry.get("signal_id"),
                            entry_signal_timestamp=entry.get("signal_time"),
                            entry_decision=entry.get("decision"),
                            exit_strategy_id=exit_attr.get("strategy")
                            if exit_reason == "STRATEGY_EXIT"
                            else None,
                            exit_strategy_version=exit_attr.get("version")
                            if exit_reason == "STRATEGY_EXIT"
                            else None,
                            exit_signal_id=exit_attr.get("signal_id")
                            if exit_reason == "STRATEGY_EXIT"
                            else None,
                            exit_signal_timestamp=exit_attr.get("signal_time")
                            if exit_reason == "STRATEGY_EXIT"
                            else None,
                            exit_reason=exit_reason,
                            protective_stop_mode=str(protection[0][0]) if protection else None,
                            initial_stop_price=initial_stop,
                            final_stop_price=final_stop,
                            holding_duration_seconds=(
                                Decimal(str((duration_end - entry_at).total_seconds()))
                                if entry_at
                                else None
                            ),
                            data_provenance="operational SQLite fills/events/completed 1m bars",
                            attribution_complete=bool(
                                entry.get("signal_id") and entry.get("strategy")
                            ),
                        )
                    )
                return records
        finally:
            store.close()

    @staticmethod
    def _apply_query(records: list[TradeRecord], query: TradeQuery) -> TradeList:
        all_entries = sorted({r.entry_strategy_id for r in records if r.entry_strategy_id})
        all_exits = sorted({r.exit_reason for r in records if r.exit_reason})
        filtered = records
        if query.symbol:
            filtered = [r for r in filtered if r.symbol == query.symbol.upper()]
        if query.date_from:
            bound = query.date_from.astimezone(UTC)
            filtered = [r for r in filtered if r.entry_timestamp and r.entry_timestamp >= bound]
        if query.date_to:
            bound = query.date_to.astimezone(UTC)
            filtered = [r for r in filtered if r.entry_timestamp and r.entry_timestamp <= bound]
        if query.entry_strategy:
            filtered = [r for r in filtered if r.entry_strategy_id == query.entry_strategy]
        if query.exit_filter:
            filtered = [
                r for r in filtered if query.exit_filter in {r.exit_reason, r.exit_strategy_id}
            ]
        if query.status:
            filtered = [r for r in filtered if r.position_status == query.status.upper()]
        if query.outcome == "WIN":
            filtered = [r for r in filtered if r.realized_pnl is not None and r.realized_pnl > ZERO]
        elif query.outcome == "LOSS":
            filtered = [r for r in filtered if r.realized_pnl is not None and r.realized_pnl < ZERO]
        keys = {
            "entry_timestamp": lambda r: r.entry_timestamp or datetime.min.replace(tzinfo=UTC),
            "exit_timestamp": lambda r: r.exit_timestamp or datetime.min.replace(tzinfo=UTC),
            "symbol": lambda r: r.symbol,
            "realized_pnl": lambda r: r.realized_pnl or ZERO,
            "holding_duration": lambda r: r.holding_duration_seconds or ZERO,
        }
        filtered.sort(
            key=keys.get(query.sort, keys["entry_timestamp"]),
            reverse=query.direction.lower() != "asc",
        )
        total = len(filtered)
        start = (query.page - 1) * query.page_size
        return TradeList(
            items=filtered[start : start + query.page_size],
            page=query.page,
            page_size=query.page_size,
            total=total,
            pages=(total + query.page_size - 1) // query.page_size,
            available_entry_strategies=all_entries,
            available_exit_reasons=all_exits,
        )

    def operational_list(self, account_id: str, query: TradeQuery) -> TradeList:
        return self._apply_query(self._operation_records(account_id), query)

    def _operation_bars(self, account_id: str, trade: TradeRecord) -> list[TradeBar]:
        if trade.entry_timestamp is None:
            return []
        local = trade.entry_timestamp.astimezone(NY)
        start = datetime.combine(local.date(), datetime.min.time(), NY).astimezone(UTC)
        end = start + timedelta(days=1)
        store, _ = self._operational(account_id)
        try:
            with store.snapshot(account_id) as conn:
                rows = conn.execute(
                    "SELECT bar_start,open_price,high_price,low_price,close_price,vwap,volume,"
                    "feed,provenance FROM market_bars WHERE account_scope_id=? AND symbol=? "
                    "AND timeframe='1m' AND bar_start>=? AND bar_start<? ORDER BY bar_start",
                    (
                        account_id,
                        trade.symbol,
                        schema.to_db_timestamp(start),
                        schema.to_db_timestamp(end),
                    ),
                ).fetchall()
            return [
                TradeBar(
                    timestamp=schema.from_db_timestamp(r[0]),
                    timestamp_et=schema.from_db_timestamp(r[0]).astimezone(NY).isoformat(),
                    open=Decimal(r[1]),
                    high=Decimal(r[2]),
                    low=Decimal(r[3]),
                    close=Decimal(r[4]),
                    vwap=_decimal(r[5]),
                    volume=Decimal(r[6]),
                    feed=r[7],
                    provenance=r[8],
                )
                for r in rows
            ]
        finally:
            store.close()

    def _operation_fills_events(
        self, account_id: str, trade: TradeRecord
    ) -> tuple[list[TradeFill], list[TradeEvent]]:
        store, _ = self._operational(account_id)
        events: list[TradeEvent] = []
        fills_out: list[TradeFill] = []
        try:
            with store.snapshot(account_id) as conn:
                signals = conn.execute(
                    "SELECT s.generated_at,s.action,s.signal_id,s.reference_price,s.reason,"
                    "i.strategy_key,i.strategy_version,d.outcome,d.reason,d.decided_at FROM signals s "
                    "LEFT JOIN strategy_instances i ON i.account_scope_id=s.account_scope_id "
                    "AND i.instance_id=s.instance_id LEFT JOIN decisions d "
                    "ON d.account_scope_id=s.account_scope_id AND d.signal_id=s.signal_id "
                    "WHERE s.account_scope_id=? AND (s.signal_id IN (SELECT signal_id FROM order_intents "
                    "WHERE account_scope_id=? AND cycle_id=?) OR (s.symbol=? AND s.bar_time=(SELECT es.bar_time "
                    "FROM order_intents en JOIN signals es ON es.account_scope_id=en.account_scope_id "
                    "AND es.signal_id=en.signal_id WHERE en.account_scope_id=? AND en.cycle_id=? "
                    "AND en.purpose='ENTRY' LIMIT 1))) ORDER BY s.generated_at,s.signal_id",
                    (
                        account_id,
                        account_id,
                        trade.cycle_id,
                        trade.symbol,
                        account_id,
                        trade.cycle_id,
                    ),
                ).fetchall()
                for r in signals:
                    at = _time(r[0])
                    assert at
                    events.append(
                        TradeEvent(
                            timestamp=at,
                            timestamp_et=at.astimezone(NY).isoformat(),
                            event_type="SIGNAL_GENERATED",
                            source=r[1],
                            strategy_id=r[5],
                            strategy_version=r[6],
                            signal_id=r[2],
                            price=_decimal(r[3]),
                            reason=r[4],
                        )
                    )
                    if r[7] is not None:
                        decided_at = _time(r[9]) or at
                        events.append(
                            TradeEvent(
                                timestamp=decided_at,
                                timestamp_et=decided_at.astimezone(NY).isoformat(),
                                event_type="SIGNAL_ACCEPTED"
                                if str(r[7]).upper() in ACCEPTED
                                else "SIGNAL_REJECTED",
                                source=r[1],
                                strategy_id=r[5],
                                strategy_version=r[6],
                                signal_id=r[2],
                                reason=r[8],
                                details={"decision": r[7]},
                            )
                        )
                orders = conn.execute(
                    "SELECT b.submitted_at,n.purpose,b.broker_order_id,b.side,b.stop_price,b.quantity "
                    "FROM order_intents n JOIN broker_orders b ON b.account_scope_id=n.account_scope_id "
                    "AND b.intent_id=n.intent_id WHERE n.account_scope_id=? AND n.cycle_id=?",
                    (account_id, trade.cycle_id),
                ).fetchall()
                for r in orders:
                    at = _time(r[0])
                    assert at
                    events.append(
                        TradeEvent(
                            timestamp=at,
                            timestamp_et=at.astimezone(NY).isoformat(),
                            event_type="ORDER_SUBMITTED",
                            source=r[1],
                            order_id=r[2],
                            price=_decimal(r[4]),
                            quantity=_decimal(r[5]),
                            details={"side": r[3]},
                        )
                    )
                fills = conn.execute(
                    "SELECT fill_id,broker_order_id,side,quantity,price,fee,filled_at FROM fills "
                    "WHERE account_scope_id=? AND cycle_id=? ORDER BY filled_at,fill_id",
                    (account_id, trade.cycle_id),
                ).fetchall()
                for r in fills:
                    at = _time(r[6])
                    assert at
                    fill = TradeFill(
                        fill_id=r[0],
                        order_id=r[1],
                        side=r[2],
                        timestamp=at,
                        quantity=Decimal(r[3]),
                        price=Decimal(r[4]),
                        fee=Decimal(r[5]),
                    )
                    fills_out.append(fill)
                    events.append(
                        TradeEvent(
                            timestamp=at,
                            timestamp_et=at.astimezone(NY).isoformat(),
                            event_type=f"{r[2]}_FILL",
                            source="execution",
                            order_id=r[1],
                            price=Decimal(r[4]),
                            quantity=Decimal(r[3]),
                            details={"fill_id": r[0]},
                        )
                    )
                protections = conn.execute(
                    "SELECT created_at,kind,protection_order_id,stop_price,covered_quantity FROM protection_orders "
                    "WHERE account_scope_id=? AND cycle_id=? ORDER BY created_at",
                    (account_id, trade.cycle_id),
                ).fetchall()
                for idx, r in enumerate(protections):
                    at = _time(r[0])
                    assert at
                    events.append(
                        TradeEvent(
                            timestamp=at,
                            timestamp_et=at.astimezone(NY).isoformat(),
                            event_type="INITIAL_STOP_ESTABLISHED"
                            if idx == 0
                            else "PROTECTION_UPDATED",
                            source=r[1],
                            order_id=r[2],
                            price=_decimal(r[3]),
                            quantity=_decimal(r[4]),
                        )
                    )
                movements = conn.execute(
                    "SELECT occurred_at,event_type,previous_stop_price,new_stop_price,payload_json "
                    "FROM trailing_stop_events WHERE account_scope_id=? AND cycle_id=? ORDER BY occurred_at",
                    (account_id, trade.cycle_id),
                ).fetchall()
                for r in movements:
                    at = _time(r[0])
                    assert at
                    events.append(
                        TradeEvent(
                            timestamp=at,
                            timestamp_et=at.astimezone(NY).isoformat(),
                            event_type="STOP_MOVED",
                            source=trade.protective_stop_mode,
                            price=_decimal(r[3]),
                            reason=r[1],
                            details={
                                "previous_stop_price": r[2],
                                **(json.loads(r[4]) if r[4] else {}),
                            },
                        )
                    )
            if trade.exit_timestamp:
                events.append(
                    TradeEvent(
                        timestamp=trade.exit_timestamp,
                        timestamp_et=trade.exit_timestamp.astimezone(NY).isoformat(),
                        event_type="POSITION_CLOSED",
                        source=trade.exit_reason,
                    )
                )
            if trade.entry_timestamp:
                events.append(
                    TradeEvent(
                        timestamp=trade.entry_timestamp,
                        timestamp_et=trade.entry_timestamp.astimezone(NY).isoformat(),
                        event_type="POSITION_OPENED",
                        source=trade.entry_strategy_id,
                        price=trade.entry_price,
                        quantity=trade.filled_quantity,
                    )
                )
            events.sort(key=lambda e: (e.timestamp, e.event_type))
            return fills_out, events
        finally:
            store.close()

    @staticmethod
    def _diagnostics(
        trade: TradeRecord, bars: list[TradeBar], fills: list[TradeFill], as_of: datetime
    ) -> TradeDiagnostics:
        end = trade.exit_timestamp or as_of
        # Boundary candles can contain prices from before the entry or after the
        # exit. Exclude them unless the complete minute is inside the holding
        # interval; OHLC cannot tell us the intrabar order.
        holding = [
            b
            for b in bars
            if trade.entry_timestamp
            and b.timestamp >= trade.entry_timestamp
            and b.timestamp + timedelta(minutes=1) <= end
        ]
        best = max((b.high for b in holding), default=None)
        worst = min((b.low for b in holding), default=None)
        favorable: list[Decimal] = []
        adverse: list[Decimal] = []
        for bar in holding:
            known = [fill for fill in fills if fill.timestamp <= bar.timestamp]
            buys = [fill for fill in known if fill.side == "BUY"]
            sells = [fill for fill in known if fill.side == "SELL"]
            bought = sum((fill.quantity for fill in buys), ZERO)
            held = bought - sum((fill.quantity for fill in sells), ZERO)
            if bought <= ZERO or held <= ZERO:
                continue
            average = sum((fill.quantity * fill.price for fill in buys), ZERO) / bought
            favorable.append((bar.high - average) * held)
            adverse.append((bar.low - average) * held)
        if holding and not favorable and trade.entry_price is not None:
            # Legacy cycles can retain bars without individual fills.
            quantity = trade.open_quantity or trade.filled_quantity
            favorable = [(bar.high - trade.entry_price) * quantity for bar in holding]
            adverse = [(bar.low - trade.entry_price) * quantity for bar in holding]
        mfe = max(ZERO, max(favorable)) if favorable else None
        mae = min(ZERO, min(adverse)) if adverse else None
        return TradeDiagnostics(
            basis="actual execution prices plus completed one-minute bar highs/lows",
            observations_available=bool(holding),
            observations_through=holding[-1].timestamp if holding else None,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            quantity=trade.filled_quantity,
            realized_pnl=trade.realized_pnl,
            unrealized_pnl=trade.unrealized_pnl,
            realized_return_percent=trade.realized_return_percent,
            holding_duration_seconds=trade.holding_duration_seconds,
            maximum_favorable_excursion=mfe,
            maximum_adverse_excursion=mae,
            maximum_unrealized_gain=mfe,
            maximum_unrealized_loss=mae,
            best_price=best,
            worst_price=worst,
            initial_stop_distance=(
                trade.entry_price - trade.initial_stop_price
                if trade.entry_price is not None and trade.initial_stop_price is not None
                else None
            ),
            final_stop_distance=(
                trade.entry_price - trade.final_stop_price
                if trade.entry_price is not None and trade.final_stop_price is not None
                else None
            ),
            exit_reason=trade.exit_reason,
            limitation=None
            if holding
            else "No authoritative completed one-minute bars were recorded for the holding interval. "
            "Intrabar ordering cannot be established from OHLC bars.",
        )

    def operational_inspection(
        self, account_id: str, trade_id: str, as_of: datetime | None = None
    ) -> TradeInspection:
        trade = next(
            (r for r in self._operation_records(account_id) if r.trade_id == trade_id), None
        )
        if trade is None:
            raise LookupError(trade_id)
        bars = self._operation_bars(account_id, trade)
        fills, events = self._operation_fills_events(account_id, trade)
        return TradeInspection(
            trade=trade,
            fills=fills,
            bars=bars,
            events=events,
            diagnostics=self._diagnostics(
                trade, bars, fills, (as_of or self._now()).astimezone(UTC)
            ),
            bars_available=bool(bars),
            bars_limitation=None
            if bars
            else "No persisted completed one-minute bars are available for this session.",
        )

    def _artifact_paths(self) -> dict[str, tuple[Path, Path]]:
        found: dict[str, tuple[Path, Path]] = {}
        if not self.research_root.is_dir():
            return found
        for manifest in islice(self.research_root.rglob("*.manifest.json"), 2000):
            results = manifest.with_name(manifest.name.replace(".manifest.json", ".results.json"))
            if not results.is_file():
                continue
            try:
                run_id = str(json.loads(manifest.read_text(encoding="utf-8"))["run_id"])
            except (OSError, ValueError, KeyError):
                continue
            found.setdefault(run_id, (manifest, results))
        return found

    def research_runs(self) -> ResearchRuns:
        rows: list[ResearchRun] = []
        for run_id, (path, _) in self._artifact_paths().items():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append(
                ResearchRun(
                    run_id=run_id,
                    created_at=_time(raw.get("created_at")),
                    symbols=[str(x) for x in raw.get("symbols", [])],
                    period_role=raw.get("period_role"),
                    window_start=_time(raw.get("window_start")),
                    window_end=_time(raw.get("window_end")),
                    dataset_id=raw.get("data_revision") or raw.get("data_vintage"),
                    config_fingerprint=raw.get("config_hash"),
                    artifact_version=raw.get("schema_version"),
                )
            )
        rows.sort(
            key=lambda r: (r.created_at or datetime.min.replace(tzinfo=UTC), r.run_id), reverse=True
        )
        return ResearchRuns(runs=rows[:500])

    def _artifact(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        paths = self._artifact_paths().get(run_id)
        if paths is None:
            raise LookupError(run_id)
        return (
            json.loads(paths[0].read_text(encoding="utf-8")),
            json.loads(paths[1].read_text(encoding="utf-8")),
        )

    def _research_records(self, run_id: str) -> list[TradeRecord]:
        manifest, result = self._artifact(run_id)
        fills = list(result.get("fills", []))
        cycles = list(result.get("cycles", []))
        records = []
        versions = {
            str(x.get("name")): str(x.get("version")) for x in manifest.get("strategies", [])
        }
        for idx, cycle in enumerate(cycles):
            opened = _time(cycle.get("opened_at"))
            closed = _time(cycle.get("closed_at"))
            symbol = str(cycle.get("symbol", "UNKNOWN"))
            cycle_id = str(cycle.get("cycle_id") or f"{run_id}:{symbol}:{idx + 1}")
            related = []
            for fill in fills:
                at = _time(fill.get("time"))
                if str(fill.get("symbol", "")).upper() != symbol.upper() or at is None:
                    continue
                side = str(fill.get("side", "")).upper()
                legacy_match = bool(
                    opened
                    and (
                        (side == "BUY" and at >= opened and (closed is None or at < closed))
                        or (side == "SELL" and at > opened and (closed is None or at <= closed))
                    )
                )
                if fill.get("cycle_id") == cycle_id or (not fill.get("cycle_id") and legacy_match):
                    related.append(fill)
            buys = [f for f in related if str(f.get("side")).upper() == "BUY"]
            sells = [f for f in related if str(f.get("side")).upper() == "SELL"]
            bq, bp, bn, bf = _weighted(buys)
            sq, sp, sn, sf = _weighted(sells)
            realized = (
                (sn - (bp or ZERO) * sq - bf * (sq / bq if bq else ZERO) - sf)
                if sq and bp is not None
                else None
            )
            basis = (bp or ZERO) * sq + (bf * (sq / bq if bq else ZERO) if bq else ZERO)
            entry_strategy = cycle.get("entry_strategy") or (
                buys[0].get("strategy") if buys else None
            )
            exit_strategy = cycle.get("exit_strategy") or (
                sells[-1].get("strategy") if sells else None
            )
            exit_reason = cycle.get("exit_reason") or (
                sells[-1].get("exit_reason") if sells else None
            )
            if exit_reason == "STRATEGY_SELL":
                exit_reason = "STRATEGY_EXIT"
            if exit_reason == "PROTECTIVE_STOP":
                exit_strategy = None
            protection_mode = cycle.get("protection_mode")
            protection_mode = {
                "FIXED": "FIXED_STOP",
                "TRAILING_NATIVE": "BROKER_TRAILING_STOP",
                "TRAILING_STEPPED": "STEPPED_TRAILING_STOP",
            }.get(protection_mode, protection_mode)
            protection_events = [
                e for e in result.get("protection_events", []) if e.get("cycle_id") == cycle_id
            ]
            stop_prices = [
                _decimal(e.get("stop_price") or e.get("new_stop_price")) for e in protection_events
            ]
            stop_prices = [x for x in stop_prices if x is not None]
            end = closed or self._now().astimezone(UTC)
            records.append(
                TradeRecord(
                    trade_id=cycle_id,
                    execution_mode="BACKTEST",
                    account_id=f"BACKTEST:{run_id}",
                    scenario_id=None,
                    research_run_id=run_id,
                    symbol=symbol.upper(),
                    cycle_id=cycle_id,
                    entry_timestamp=_time(buys[0].get("time")) if buys else opened,
                    exit_timestamp=_time(sells[-1].get("time")) if sells else closed,
                    entry_price=bp,
                    exit_price=sp,
                    filled_quantity=bq,
                    exited_quantity=sq,
                    open_quantity=max(ZERO, bq - sq),
                    entry_notional=bn if buys else None,
                    exit_notional=sn if sells else None,
                    realized_pnl=realized,
                    realized_return_percent=realized / basis * Decimal(100)
                    if realized is not None and basis
                    else None,
                    position_status="CLOSED" if closed else ("PARTIALLY_CLOSED" if sq else "OPEN"),
                    entry_strategy_id=entry_strategy,
                    entry_strategy_version=cycle.get("entry_strategy_version")
                    or versions.get(str(entry_strategy)),
                    entry_signal_id=cycle.get("entry_signal_id")
                    or (buys[0].get("signal_id") if buys else None),
                    entry_signal_timestamp=_time(
                        cycle.get("entry_signal_timestamp")
                        or (buys[0].get("signal_time") if buys else None)
                    ),
                    entry_decision=cycle.get("entry_decision"),
                    exit_strategy_id=exit_strategy,
                    exit_strategy_version=cycle.get("exit_strategy_version")
                    or versions.get(str(exit_strategy)),
                    exit_signal_id=cycle.get("exit_signal_id")
                    or (sells[-1].get("signal_id") if sells else None),
                    exit_signal_timestamp=_time(
                        cycle.get("exit_signal_timestamp")
                        or (sells[-1].get("signal_time") if sells else None)
                    ),
                    exit_reason=exit_reason,
                    protective_stop_mode=protection_mode,
                    initial_stop_price=stop_prices[0]
                    if stop_prices
                    else _decimal(cycle.get("initial_stop_price")),
                    final_stop_price=stop_prices[-1]
                    if stop_prices
                    else _decimal(cycle.get("final_stop_price")),
                    holding_duration_seconds=Decimal(str((end - opened).total_seconds()))
                    if opened
                    else None,
                    data_provenance=json.dumps(manifest.get("data_provenance", {}), sort_keys=True),
                    attribution_complete=bool(
                        entry_strategy
                        and (cycle.get("entry_signal_id") or (buys and buys[0].get("signal_id")))
                    ),
                )
            )
        return records

    def research_list(self, run_id: str, query: TradeQuery) -> TradeList:
        return self._apply_query(self._research_records(run_id), query)

    def _research_bars(self, manifest: dict[str, Any], trade: TradeRecord) -> list[TradeBar]:
        dataset = manifest.get("data_revision") or manifest.get("data_vintage")
        if not dataset or trade.entry_timestamp is None:
            return []
        mp = self.dataset_root / "manifests" / f"{dataset}.json"
        if not mp.is_file():
            return []
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            path = self.dataset_root / str(meta["data_file"])
            if not path.is_file():
                return []
            if meta.get("sha256") and _sha256_path(path) != meta["sha256"]:
                return []
            import duckdb

            local = trade.entry_timestamp.astimezone(NY)
            start = datetime.combine(local.date(), datetime.min.time(), NY).astimezone(UTC)
            end = start + timedelta(days=1)
            con = duckdb.connect(":memory:")
            try:
                rows = con.execute(
                    "SELECT timestamp,open,high,low,close,vwap,volume FROM read_parquet(?) WHERE symbol=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
                    [str(path), trade.symbol, start, end],
                ).fetchall()
            finally:
                con.close()
            return [
                TradeBar(
                    timestamp=_time(r[0]),
                    timestamp_et=_time(r[0]).astimezone(NY).isoformat(),
                    open=Decimal(str(r[1])),
                    high=Decimal(str(r[2])),
                    low=Decimal(str(r[3])),
                    close=Decimal(str(r[4])),
                    vwap=_decimal(r[5]),
                    volume=Decimal(str(r[6])),
                    feed=meta.get("feed"),
                    provenance=str(dataset),
                )
                for r in rows
            ]
        except (OSError, ValueError, KeyError, ImportError):
            return []

    def research_inspection(
        self, run_id: str, trade_id: str, as_of: datetime | None = None
    ) -> TradeInspection:
        manifest, result = self._artifact(run_id)
        trade = next((r for r in self._research_records(run_id) if r.trade_id == trade_id), None)
        if trade is None:
            raise LookupError(trade_id)
        related = []
        for raw_fill in result.get("fills", []):
            at = _time(raw_fill.get("time"))
            side = str(raw_fill.get("side", "")).upper()
            legacy_match = bool(
                at
                and trade.entry_timestamp
                and str(raw_fill.get("symbol", "")).upper() == trade.symbol
                and (
                    (
                        side == "BUY"
                        and at >= trade.entry_timestamp
                        and (trade.exit_timestamp is None or at < trade.exit_timestamp)
                    )
                    or (
                        side == "SELL"
                        and at > trade.entry_timestamp
                        and (trade.exit_timestamp is None or at <= trade.exit_timestamp)
                    )
                )
            )
            if raw_fill.get("cycle_id") == trade.cycle_id or (
                not raw_fill.get("cycle_id") and legacy_match
            ):
                related.append(raw_fill)
        fills = [
            TradeFill(
                fill_id=str(f.get("fill_id") or f.get("order_id") or i),
                order_id=f.get("order_id"),
                side=str(f.get("side")),
                timestamp=_time(f.get("time")),
                price=_decimal(f.get("price")),
                quantity=_decimal(f.get("quantity")),
                fee=_decimal(f.get("fee")) or ZERO,
            )
            for i, f in enumerate(related)
        ]
        events = []
        relevant_signals = {
            value for value in (trade.entry_signal_id, trade.exit_signal_id) if value
        }
        for raw in result.get("signals", []):
            at = _time(raw.get("time"))
            if at is None or str(raw.get("symbol", "")).upper() != trade.symbol:
                continue
            if relevant_signals and raw.get("signal_id") not in relevant_signals:
                # Preserve candidates evaluated alongside the accepted entry/exit.
                accepted_times = {
                    _time(item.get("time"))
                    for item in result.get("signals", [])
                    if item.get("signal_id") in relevant_signals
                }
                if at not in accepted_times:
                    continue
            events.append(
                TradeEvent(
                    timestamp=at,
                    timestamp_et=at.astimezone(NY).isoformat(),
                    event_type="SIGNAL_GENERATED",
                    source=raw.get("side"),
                    strategy_id=raw.get("strategy"),
                    strategy_version=raw.get("version"),
                    signal_id=raw.get("signal_id"),
                    price=_decimal(raw.get("reference_price")),
                    reason=",".join(raw.get("reason_codes", [])),
                )
            )
        for decision in result.get("decisions", []):
            at = _time(decision.get("time") or decision.get("evaluated_at"))
            if at is None or str(decision.get("symbol", "")).upper() != trade.symbol:
                continue
            records = decision.get("records", [])
            if relevant_signals and not any(
                row.get("signal_id") in relevant_signals for row in records
            ):
                continue
            for row in records:
                event_type = (
                    "SIGNAL_ACCEPTED"
                    if str(row.get("outcome")).upper() == "SELECTED"
                    else "SIGNAL_REJECTED"
                )
                events.append(
                    TradeEvent(
                        timestamp=at,
                        timestamp_et=at.astimezone(NY).isoformat(),
                        event_type=event_type,
                        source=decision.get("kind") or row.get("source"),
                        strategy_id=row.get("strategy_id"),
                        strategy_version=row.get("version"),
                        signal_id=row.get("signal_id"),
                        reason=",".join(row.get("reason_codes", [])),
                        details={
                            "decision_id": row.get("decision_id"),
                            "superseded_by": row.get("superseded_by"),
                        },
                    )
                )
        for raw in result.get("intents", []):
            if (
                raw.get("cycle_id") != trade.cycle_id
                and raw.get("signal_id") not in relevant_signals
            ):
                continue
            at = _time(raw.get("time"))
            if at:
                events.append(
                    TradeEvent(
                        timestamp=at,
                        timestamp_et=at.astimezone(NY).isoformat(),
                        event_type="ORDER_SUBMITTED",
                        source=raw.get("purpose"),
                        strategy_id=raw.get("strategy"),
                        strategy_version=raw.get("strategy_version"),
                        signal_id=raw.get("signal_id"),
                        order_id=raw.get("order_id"),
                    )
                )
        for f in fills:
            events.append(
                TradeEvent(
                    timestamp=f.timestamp,
                    timestamp_et=f.timestamp.astimezone(NY).isoformat(),
                    event_type=f"{f.side}_FILL",
                    source="backtest execution",
                    order_id=f.order_id,
                    price=f.price,
                    quantity=f.quantity,
                )
            )
        for raw in result.get("protection_events", []):
            if raw.get("cycle_id") != trade.cycle_id:
                continue
            at = _time(raw.get("time"))
            if at:
                events.append(
                    TradeEvent(
                        timestamp=at,
                        timestamp_et=at.astimezone(NY).isoformat(),
                        event_type=str(raw.get("event_type", "PROTECTION_UPDATED")),
                        source=raw.get("protection_mode"),
                        order_id=raw.get("order_id"),
                        price=_decimal(raw.get("stop_price") or raw.get("new_stop_price")),
                        reason=raw.get("reason"),
                    )
                )
        if trade.exit_timestamp:
            events.append(
                TradeEvent(
                    timestamp=trade.exit_timestamp,
                    timestamp_et=trade.exit_timestamp.astimezone(NY).isoformat(),
                    event_type="POSITION_CLOSED",
                    source=trade.exit_reason,
                )
            )
        if trade.entry_timestamp:
            events.append(
                TradeEvent(
                    timestamp=trade.entry_timestamp,
                    timestamp_et=trade.entry_timestamp.astimezone(NY).isoformat(),
                    event_type="POSITION_OPENED",
                    source=trade.entry_strategy_id,
                    price=trade.entry_price,
                    quantity=trade.filled_quantity,
                )
            )
        events.sort(key=lambda e: e.timestamp)
        bars = self._research_bars(manifest, trade)
        return TradeInspection(
            trade=trade,
            fills=fills,
            bars=bars,
            events=events,
            diagnostics=self._diagnostics(
                trade, bars, fills, (as_of or self._now()).astimezone(UTC)
            ),
            bars_available=bool(bars),
            bars_limitation=None
            if bars
            else "The immutable dataset referenced by this research manifest is unavailable on this host.",
        )
