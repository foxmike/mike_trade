"""Per-account/scenario ledger reports with an explicit equity reconciliation.

Design choices (the task's decision budget):
1. One report covers exactly one account scope (PAPER/LIVE account or SHADOW
   scenario).  Every query is scoped, the database identity must match the
   scope's mode, and :func:`require_single_scope` rejects any request that
   names more than one account, scenario, or mode.  Nothing is ever summed
   across scopes.
2. Windows are ``(start, end]`` in UTC.  DAY and WEEK start at midnight
   America/New_York (Monday for WEEK); LIFETIME starts at the baseline.  For
   PAPER/LIVE the effective window is bounded by the equity snapshots that
   supply starting and ending equity, so events and equity share one clock
   (``window_start``/``window_end`` report it).  Date-only provider timestamps
   are midnight UTC and land in the ET session that contains that instant.
3. Valuation.  PAPER/LIVE: equity, cash and buying power come from the
   ``equity_snapshots`` rows (the broker's own figures).  Unrealized P&L is
   ``snapshot equity - snapshot cash - open cost basis`` where the cost basis is
   replayed from the fills ledger by average cost; the mark source/as-of are the
   snapshot's, and the mark is stale when older than ``max_mark_age`` at the
   requested time.  SHADOW: equity is replayed cash plus open cost basis plus
   unrealized P&L from caller-supplied marks; unmarked positions are valued at
   cost (unrealized zero, mark flagged stale).  Because the PAPER/LIVE
   unrealized figure absorbs mark differences, the residual measures unexplained
   *cash* movement: missing or mis-estimated fees, unrecorded fills, and
   unclassified cash events are surfaced as drift, never hidden.
4. Rounding.  All internal math is unrounded Decimal; only
   :meth:`AccountLedgerReport.presented` quantizes to cents (ROUND_HALF_EVEN).
   ``reconciled`` compares the unrounded residual with ``tolerance``.
5. Fees.  PAPER/LIVE confirmed fees are every fee-bearing broker activity in the
   window (``unattributed_confirmed_fees`` is the part naming no order);
   estimated fees come from the configured schedule for in-window orders with no
   linked activity; an order with neither makes ``fees_complete`` false and its
   fee is excluded from net P&L.  A broker fill row's own ``fee`` column has no
   provenance and is ignored.  SHADOW fees are the simulated fees, ESTIMATED.
6. Commitments are measured now, not at the period end: ACTIVE reservations
   (reserved pending buys) plus deployed cost basis, per symbol.  Reservations
   with no symbol cannot be attributed and are excluded.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Any

from mike_trade.brokers.shadow.account import ShadowAccountState
from mike_trade.brokers.shadow.ledger import ShadowLedger
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.domain.trading import BudgetPolicy, Fill, OrderSide
from mike_trade.persistence.readstore import ReadStore
from mike_trade.persistence.schema import (
    decimal_from_db,
    from_db_timestamp,
    to_db_timestamp,
)
from mike_trade.reporting.fees import (
    ActivityCategory,
    BrokerActivityRecord,
    FeeSchedule,
    FeeStatus,
    attribute_order_fees,
    load_ingested_activities,
)
from mike_trade.scheduling.calendar import NEW_YORK

__all__ = [
    "ReportPeriod",
    "ReportDataError",
    "SymbolCommitment",
    "EquityReconciliation",
    "AccountLedgerReport",
    "PRESENTATION_QUANTUM",
    "PRESENTATION_ROUNDING",
    "DEFAULT_TOLERANCE",
    "VALUATION_CONVENTION",
    "ROUNDING_CONVENTION",
    "quantize_cents",
    "require_single_scope",
    "reconcile_equity",
    "build_account_ledger_report",
]

_ZERO = Decimal("0")
PRESENTATION_QUANTUM = Decimal("0.01")
PRESENTATION_ROUNDING = ROUND_HALF_EVEN
DEFAULT_TOLERANCE = Decimal("0.01")
ROUNDING_CONVENTION = (
    "internal math is unrounded Decimal; presentation is quantized to 0.01 with "
    "ROUND_HALF_EVEN; reconciled compares the unrounded residual to the tolerance"
)
VALUATION_CONVENTION = (
    "PAPER/LIVE: equity, cash and buying power from equity_snapshots; unrealized = snapshot "
    "equity - snapshot cash - open average-cost basis replayed from fills. "
    "SHADOW: cash plus open cost basis plus unrealized from supplied marks (unmarked = cost). "
    "mark_source and mark_as_of identify the figure used."
)
_FLOW = ActivityCategory.EXTERNAL_FLOW.value
_INCOME = ActivityCategory.INCOME.value
_CORPORATE = ActivityCategory.CORPORATE_ACTION.value
_BROKER_ENVIRONMENTS = frozenset({AccountEnvironment.PAPER, AccountEnvironment.LIVE})


class ReportPeriod(str, Enum):  # noqa: UP042 - str values serialize directly
    DAY = "day"
    WEEK = "week"
    LIFETIME = "lifetime"


class ReportDataError(ValueError):
    """The persisted data cannot support the requested report."""


def quantize_cents(value: Decimal) -> Decimal:
    """Presentation rounding: cents, ROUND_HALF_EVEN."""
    return value.quantize(PRESENTATION_QUANTUM, rounding=PRESENTATION_ROUNDING)


# ------------------------------------------------------------------------ records


@dataclass(frozen=True, slots=True)
class SymbolCommitment:
    symbol: str
    reserved_pending_buys: Decimal
    deployed_cost_basis: Decimal

    @property
    def commitment(self) -> Decimal:
        return self.reserved_pending_buys + self.deployed_cost_basis


@dataclass(frozen=True, slots=True)
class EquityReconciliation:
    starting_equity: Decimal
    expected_ending_equity: Decimal
    ending_equity: Decimal
    residual: Decimal
    tolerance: Decimal
    reconciled: bool

    @property
    def drift(self) -> bool:
        return not self.reconciled


def reconcile_equity(
    *,
    starting_equity: Decimal,
    external_flows: Decimal,
    income: Decimal,
    corporate_adjustments: Decimal,
    net_realized_pnl: Decimal,
    change_in_unrealized_pnl: Decimal,
    ending_equity: Decimal,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> EquityReconciliation:
    """Apply the reconciliation identity at full Decimal precision.

    ``starting + flows + income + corporate + net realized + change in
    unrealized = ending``; ``residual = ending - expected``.
    """
    for name, value in (
        ("starting_equity", starting_equity),
        ("external_flows", external_flows),
        ("income", income),
        ("corporate_adjustments", corporate_adjustments),
        ("net_realized_pnl", net_realized_pnl),
        ("change_in_unrealized_pnl", change_in_unrealized_pnl),
        ("ending_equity", ending_equity),
        ("tolerance", tolerance),
    ):
        if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"{name} must be a finite Decimal")
    if tolerance < _ZERO:
        raise ValueError("tolerance must be >= 0")
    expected = (
        starting_equity
        + external_flows
        + income
        + corporate_adjustments
        + net_realized_pnl
        + change_in_unrealized_pnl
    )
    residual = ending_equity - expected
    return EquityReconciliation(
        starting_equity=starting_equity,
        expected_ending_equity=expected,
        ending_equity=ending_equity,
        residual=residual,
        tolerance=tolerance,
        reconciled=abs(residual) <= tolerance,
    )


@dataclass(frozen=True, slots=True)
class AccountLedgerReport:
    """One account/scenario, one period.  Money is unrounded; see ``presented``."""

    account_scope_id: str
    environment: str
    scenario: str | None
    period: ReportPeriod
    as_of: datetime
    window_start: datetime
    window_end: datetime
    session_start_date: date
    session_end_date: date
    estimated: bool
    # cash and equity
    starting_cash: Decimal
    ending_cash: Decimal
    starting_equity: Decimal
    ending_equity: Decimal
    # buying power vs authorized budget vs commitment
    account_buying_power: Decimal
    authorized_total_budget: Decimal | None
    authorized_per_symbol_budget: Decimal | None
    total_commitment: Decimal
    symbol_commitments: tuple[SymbolCommitment, ...]
    # trading P&L
    realized_gross_pnl: Decimal
    confirmed_fees: Decimal
    unattributed_confirmed_fees: Decimal
    estimated_fees: Decimal
    net_realized_pnl: Decimal
    fees_complete: bool
    unavailable_fee_order_ids: tuple[str, ...]
    # unrealized
    unrealized_pnl: Decimal
    starting_unrealized_pnl: Decimal
    change_in_unrealized_pnl: Decimal
    mark_source: str
    mark_as_of: datetime | None
    mark_stale: bool
    # non-trading cash
    external_deposits: Decimal
    external_withdrawals: Decimal
    external_flows: Decimal
    income: Decimal
    corporate_adjustments: Decimal
    # reconciliation
    expected_ending_equity: Decimal
    residual: Decimal
    tolerance: Decimal
    reconciled: bool
    valuation_convention: str = VALUATION_CONVENTION
    rounding_convention: str = ROUNDING_CONVENTION

    @property
    def drift(self) -> bool:
        """True when the residual exceeds tolerance; never hidden or rounded away."""
        return not self.reconciled

    def presented(self) -> dict[str, Any]:
        """Cent-quantized (ROUND_HALF_EVEN) view for display; the report is unchanged."""
        out: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, Decimal):
                value = quantize_cents(value)
            elif item.name == "symbol_commitments":
                value = [
                    {
                        "symbol": c.symbol,
                        "reserved_pending_buys": quantize_cents(c.reserved_pending_buys),
                        "deployed_cost_basis": quantize_cents(c.deployed_cost_basis),
                        "commitment": quantize_cents(c.commitment),
                    }
                    for c in value
                ]
            out[item.name] = value
        out["drift"] = self.drift
        return out


# ------------------------------------------------------------------ scope isolation


def _parse_scope_id(scope_id: str) -> AccountScope:
    prefix, _, rest = scope_id.partition(":")
    try:
        environment = AccountEnvironment(prefix.upper())
    except ValueError:
        raise ModeIsolationError(
            f"unrecognized scope {scope_id!r}", code="report_scope_invalid"
        ) from None
    if environment in (AccountEnvironment.SHADOW, AccountEnvironment.BACKTEST):
        account, _, scenario = rest.partition(":")
        return AccountScope(environment, account, scenario or None)
    return AccountScope(environment, rest)


def require_single_scope(
    requested: AccountScope | str | Iterable[AccountScope | str],
) -> AccountScope:
    """Resolve a report request to exactly one scope or raise ``ModeIsolationError``.

    Several scopes naming different modes, accounts, or scenarios are refused;
    repeating the same scope is harmless.
    """
    items: list[AccountScope | str]
    if isinstance(requested, AccountScope | str):
        items = [requested]
    else:
        items = list(requested)
    scopes = {
        item.scope_id if isinstance(item, AccountScope) else item: item for item in items
    }
    if not scopes:
        raise ModeIsolationError("a report needs exactly one account scope", code="report_no_scope")
    if len(scopes) > 1:
        environments = {_parse_scope_id(scope_id).environment for scope_id in scopes}
        code = "report_mixes_modes" if len(environments) > 1 else "report_mixes_accounts"
        raise ModeIsolationError(
            f"a report cannot combine scopes {sorted(scopes)!r}",
            code=code,
            context={"scopes": sorted(scopes)},
        )
    (scope_id, item), = scopes.items()
    return item if isinstance(item, AccountScope) else _parse_scope_id(scope_id)


# ------------------------------------------------------------------------- windows


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _period_start(period: ReportPeriod, as_of: datetime) -> datetime | None:
    """Nominal window start: midnight America/New_York, or None for LIFETIME."""
    if period is ReportPeriod.LIFETIME:
        return None
    local_day = as_of.astimezone(NEW_YORK).date()
    if period is ReportPeriod.WEEK:
        local_day -= timedelta(days=local_day.weekday())
    return datetime.combine(local_day, time.min, NEW_YORK).astimezone(UTC)


# ---------------------------------------------------------------- fills replay


@dataclass(frozen=True, slots=True)
class _Replay:
    realized_gross: Decimal
    basis_start: dict[str, Decimal]
    basis_end: dict[str, Decimal]
    window_fills: tuple[Fill, ...]


def _replay_fills(fills: Sequence[Fill], start: datetime, end: datetime) -> _Replay:
    """Average-cost replay.  Realized P&L counts only sells inside ``(start, end]``."""
    held: dict[str, list[Decimal]] = {}  # symbol -> [quantity, total cost]

    def basis() -> dict[str, Decimal]:
        return {s: v[1] for s, v in held.items() if v[0] > _ZERO}

    ordered = sorted(
        (f for f in fills if f.filled_at <= end), key=lambda f: f.filled_at
    )
    realized = _ZERO
    basis_start: dict[str, Decimal] | None = None
    in_window: list[Fill] = []
    for fill in ordered:
        if basis_start is None and fill.filled_at > start:
            basis_start = basis()
        state = held.setdefault(fill.symbol, [_ZERO, _ZERO])
        if fill.side is OrderSide.BUY:
            state[0] += fill.quantity
            state[1] += fill.quantity * fill.price
        else:
            if fill.quantity > state[0]:
                raise ReportDataError(
                    f"sell of {fill.quantity} {fill.symbol} exceeds held quantity {state[0]}"
                )
            # The closing sell takes the exact remaining cost, so no residue survives.
            closing = fill.quantity == state[0]
            cost_out = state[1] if closing else state[1] * fill.quantity / state[0]
            state[0] -= fill.quantity
            state[1] -= cost_out
            if fill.filled_at > start:
                realized += fill.quantity * fill.price - cost_out
        if fill.filled_at > start:
            in_window.append(fill)
    return _Replay(
        realized_gross=realized,
        basis_start=basis() if basis_start is None else basis_start,
        basis_end=basis(),
        window_fills=tuple(in_window),
    )


# ---------------------------------------------------------------- shared assembly


@dataclass(frozen=True, slots=True)
class _Facts:
    """Everything the pure assembly step needs, already limited to one scope."""

    window_start: datetime
    window_end: datetime
    starting_cash: Decimal
    ending_cash: Decimal
    starting_equity: Decimal
    ending_equity: Decimal
    starting_unrealized: Decimal
    ending_unrealized: Decimal
    mark_source: str
    mark_as_of: datetime | None
    mark_stale: bool
    buying_power: Decimal
    replay: _Replay
    confirmed_fees: Decimal
    unattributed_fees: Decimal
    estimated_fees: Decimal
    unavailable_orders: tuple[str, ...]
    deposits: Decimal
    withdrawals: Decimal
    income: Decimal
    corporate: Decimal
    commitments: tuple[SymbolCommitment, ...]
    budget_total: Decimal | None
    budget_per_symbol: Decimal | None


def _assemble(
    scope: AccountScope,
    period: ReportPeriod,
    as_of: datetime,
    facts: _Facts,
    tolerance: Decimal,
) -> AccountLedgerReport:
    gross = facts.replay.realized_gross
    net = gross - facts.confirmed_fees - facts.estimated_fees
    flows = facts.deposits + facts.withdrawals
    change_unrealized = facts.ending_unrealized - facts.starting_unrealized
    check = reconcile_equity(
        starting_equity=facts.starting_equity,
        external_flows=flows,
        income=facts.income,
        corporate_adjustments=facts.corporate,
        net_realized_pnl=net,
        change_in_unrealized_pnl=change_unrealized,
        ending_equity=facts.ending_equity,
        tolerance=tolerance,
    )
    return AccountLedgerReport(
        account_scope_id=scope.scope_id,
        environment=scope.environment.value,
        scenario=scope.scenario,
        period=period,
        as_of=as_of,
        window_start=facts.window_start,
        window_end=facts.window_end,
        session_start_date=facts.window_start.astimezone(NEW_YORK).date(),
        session_end_date=facts.window_end.astimezone(NEW_YORK).date(),
        estimated=scope.environment is AccountEnvironment.SHADOW,
        starting_cash=facts.starting_cash,
        ending_cash=facts.ending_cash,
        starting_equity=facts.starting_equity,
        ending_equity=facts.ending_equity,
        account_buying_power=facts.buying_power,
        authorized_total_budget=facts.budget_total,
        authorized_per_symbol_budget=facts.budget_per_symbol,
        total_commitment=sum((c.commitment for c in facts.commitments), _ZERO),
        symbol_commitments=facts.commitments,
        realized_gross_pnl=gross,
        confirmed_fees=facts.confirmed_fees,
        unattributed_confirmed_fees=facts.unattributed_fees,
        estimated_fees=facts.estimated_fees,
        net_realized_pnl=net,
        fees_complete=not facts.unavailable_orders,
        unavailable_fee_order_ids=facts.unavailable_orders,
        unrealized_pnl=facts.ending_unrealized,
        starting_unrealized_pnl=facts.starting_unrealized,
        change_in_unrealized_pnl=change_unrealized,
        mark_source=facts.mark_source,
        mark_as_of=facts.mark_as_of,
        mark_stale=facts.mark_stale,
        external_deposits=facts.deposits,
        external_withdrawals=facts.withdrawals,
        external_flows=flows,
        income=facts.income,
        corporate_adjustments=facts.corporate,
        expected_ending_equity=check.expected_ending_equity,
        residual=check.residual,
        tolerance=check.tolerance,
        reconciled=check.reconciled,
    )


# ------------------------------------------------------------- PAPER/LIVE loader


@dataclass(frozen=True, slots=True)
class _Snapshot:
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    source: str
    as_of: datetime


def _snapshot_row(row: Any) -> _Snapshot:
    return _Snapshot(
        cash=decimal_from_db(row[0]),
        equity=decimal_from_db(row[1]),
        buying_power=decimal_from_db(row[2]),
        source=str(row[3]),
        as_of=from_db_timestamp(row[4]),
    )


_SNAPSHOT_COLUMNS = (
    "SELECT cash, equity, buying_power, source, as_of FROM equity_snapshots "
    "WHERE account_scope_id = ? "
)


def _load_broker_facts(
    conn: Any,
    scope: AccountScope,
    period: ReportPeriod,
    as_of: datetime,
    *,
    budget: BudgetPolicy | None,
    fee_schedule: FeeSchedule | None,
    max_mark_age: timedelta,
) -> _Facts:
    scope_id = scope.scope_id
    if conn.execute(
        "SELECT 1 FROM account_scopes WHERE account_scope_id = ?", (scope_id,)
    ).fetchone() is None:
        raise ReportDataError(f"unknown account scope {scope_id!r}")

    end_row = conn.execute(
        _SNAPSHOT_COLUMNS + "AND as_of <= ? ORDER BY as_of DESC, equity_snapshot_id DESC LIMIT 1",
        (scope_id, to_db_timestamp(as_of)),
    ).fetchone()
    if end_row is None:
        raise ReportDataError("no equity snapshot at or before the report time")
    ending = _snapshot_row(end_row)

    nominal_start = _period_start(period, as_of)
    start_row = None
    if nominal_start is not None:
        start_row = conn.execute(
            _SNAPSHOT_COLUMNS
            + "AND as_of <= ? ORDER BY as_of DESC, equity_snapshot_id DESC LIMIT 1",
            (scope_id, to_db_timestamp(nominal_start)),
        ).fetchone()
    if start_row is None:  # LIFETIME, or no snapshot before the window: first one is baseline
        start_row = conn.execute(
            _SNAPSHOT_COLUMNS
            + "AND as_of <= ? ORDER BY as_of, equity_snapshot_id LIMIT 1",
            (scope_id, to_db_timestamp(ending.as_of)),
        ).fetchone()
    starting = _snapshot_row(start_row)
    window_start, window_end = starting.as_of, ending.as_of

    fills: list[Fill] = []
    for row in conn.execute(
        "SELECT external_activity_id, broker_order_id, symbol, side, quantity, price, fee, "
        "filled_at FROM fills WHERE account_scope_id = ? AND filled_at <= ? "
        "ORDER BY filled_at, rowid",
        (scope_id, to_db_timestamp(window_end)),
    ).fetchall():
        fills.append(
            Fill(
                fill_id=row[0],
                broker_order_id=row[1],
                account_scope_id=scope_id,
                symbol=row[2],
                side=OrderSide(row[3]),
                quantity=decimal_from_db(row[4]),
                price=decimal_from_db(row[5]),
                fee=decimal_from_db(row[6]),
                filled_at=from_db_timestamp(row[7]),
            )
        )
    replay = _replay_fills(fills, window_start, window_end)

    deposits = withdrawals = income = corporate = _ZERO
    for event_type, amount_text in conn.execute(
        "SELECT event_type, amount FROM cash_events WHERE account_scope_id = ? "
        "AND occurred_at > ? AND occurred_at <= ?",
        (scope_id, to_db_timestamp(window_start), to_db_timestamp(window_end)),
    ).fetchall():
        amount = decimal_from_db(amount_text)
        if event_type == _FLOW:
            if amount >= _ZERO:
                deposits += amount
            else:
                withdrawals += amount
        elif event_type == _INCOME:
            income += amount
        elif event_type == _CORPORATE:
            corporate += amount

    activities = load_ingested_activities(conn, scope_id, until=window_end)
    client_ids = {
        str(client): str(broker)
        for client, broker in conn.execute(
            "SELECT client_order_id, broker_order_id FROM broker_orders "
            "WHERE account_scope_id = ?",
            (scope_id,),
        ).fetchall()
    }
    confirmed = unattributed = _ZERO
    for record in _unique(activities):
        if record.is_fee_charge and window_start < record.occurred_at <= window_end:
            assert record.fee_amount is not None
            confirmed += record.fee_amount
            if record.order_id is None and record.client_order_id is None:
                unattributed += record.fee_amount
    estimated = _ZERO
    unavailable: list[str] = []
    for order_fee in attribute_order_fees(
        replay.window_fills,
        activities,
        schedule=fee_schedule,
        environment=scope.environment,
        client_order_ids=client_ids,
    ):
        if order_fee.status is FeeStatus.ESTIMATED:
            assert order_fee.amount is not None
            estimated += order_fee.amount
        elif order_fee.status is FeeStatus.UNAVAILABLE:
            unavailable.append(order_fee.broker_order_id)

    reserved: dict[str, Decimal] = {}
    for symbol, amount_text in conn.execute(
        "SELECT c.symbol, r.amount FROM budget_reservations r "
        "LEFT JOIN position_cycles c ON c.account_scope_id = r.account_scope_id "
        "AND c.cycle_id = r.cycle_id WHERE r.account_scope_id = ? AND r.state = 'ACTIVE'",
        (scope_id,),
    ).fetchall():
        if symbol is not None:
            reserved[symbol] = reserved.get(symbol, _ZERO) + decimal_from_db(amount_text)
    deployed: dict[str, Decimal] = {}
    for symbol, quantity, average in conn.execute(
        "SELECT symbol, quantity, average_entry_price FROM positions WHERE account_scope_id = ?",
        (scope_id,),
    ).fetchall():
        deployed[symbol] = deployed.get(symbol, _ZERO) + decimal_from_db(
            quantity
        ) * decimal_from_db(average)

    return _Facts(
        window_start=window_start,
        window_end=window_end,
        starting_cash=starting.cash,
        ending_cash=ending.cash,
        starting_equity=starting.equity,
        ending_equity=ending.equity,
        starting_unrealized=(
            starting.equity - starting.cash - sum(replay.basis_start.values(), _ZERO)
        ),
        ending_unrealized=ending.equity - ending.cash - sum(replay.basis_end.values(), _ZERO),
        mark_source=f"equity_snapshots:{ending.source}",
        mark_as_of=ending.as_of,
        mark_stale=as_of - ending.as_of > max_mark_age,
        buying_power=ending.buying_power,
        replay=replay,
        confirmed_fees=confirmed,
        unattributed_fees=unattributed,
        estimated_fees=estimated,
        unavailable_orders=tuple(unavailable),
        deposits=deposits,
        withdrawals=withdrawals,
        income=income,
        corporate=corporate,
        commitments=_commitments(reserved, deployed),
        budget_total=None if budget is None else budget.total_application_budget,
        budget_per_symbol=None if budget is None else budget.per_symbol_budget,
    )


def _unique(records: Iterable[BrokerActivityRecord]) -> list[BrokerActivityRecord]:
    seen: dict[str, BrokerActivityRecord] = {}
    for record in records:
        seen.setdefault(record.activity_id, record)
    return list(seen.values())


def _commitments(
    reserved: Mapping[str, Decimal], deployed: Mapping[str, Decimal]
) -> tuple[SymbolCommitment, ...]:
    return tuple(
        SymbolCommitment(symbol, reserved.get(symbol, _ZERO), deployed.get(symbol, _ZERO))
        for symbol in sorted(set(reserved) | set(deployed))
    )


# ------------------------------------------------------------------ SHADOW loader


def _shadow_state(
    events: Sequence[Any], scope: AccountScope, until: datetime
) -> ShadowAccountState:
    """Replay the time-ordered ledger prefix up to ``until`` (inclusive)."""
    state = ShadowAccountState(scope)
    for event in events:
        if event.occurred_at > until:
            break
        state.apply(event)
    return state


def _load_shadow_facts(
    ledger: ShadowLedger,
    scope: AccountScope,
    period: ReportPeriod,
    as_of: datetime,
    marks: Mapping[str, Decimal] | None,
) -> _Facts:
    events = ledger.read_events()
    created = next((e for e in events if e.kind == "ShadowAccountCreated"), None)
    if created is None:
        raise ReportDataError("shadow ledger has no account-created event")
    nominal_start = _period_start(period, as_of)
    window_start = created.occurred_at if nominal_start is None else max(
        nominal_start, created.occurred_at
    )
    window_end = max(as_of, window_start)
    start_state = _shadow_state(events, scope, window_start)
    end_state = _shadow_state(events, scope, window_end)

    replay = _replay_fills(tuple(end_state.fills.values()), window_start, window_end)
    basis_start = sum(replay.basis_start.values(), _ZERO)
    basis_end = sum(replay.basis_end.values(), _ZERO)

    marks = {k.strip().upper(): v for k, v in (marks or {}).items()}
    unrealized = _ZERO
    unmarked = False
    for symbol, position in end_state.positions.items():
        mark = marks.get(symbol)
        if mark is None:
            unmarked = True
        else:
            basis = replay.basis_end.get(symbol)
            if basis is None:
                raise ReportDataError(f"ledger position {symbol} has no matching fills")
            unrealized += position.quantity * mark - basis

    deposits = withdrawals = _ZERO
    for _, amount, _, _, at in end_state.adjustments:
        if window_start < at <= window_end:
            if amount >= _ZERO:
                deposits += amount
            else:
                withdrawals += amount

    estimated = _ZERO
    for order_fee in attribute_order_fees(
        replay.window_fills, environment=AccountEnvironment.SHADOW
    ):
        assert order_fee.amount is not None
        estimated += order_fee.amount

    reserved: dict[str, Decimal] = {}
    for order in end_state.orders.values():
        price = order.limit_price if order.limit_price is not None else order.reference_price
        if order.is_open and order.side is OrderSide.BUY and price is not None:
            reserved[order.symbol] = reserved.get(order.symbol, _ZERO) + order.remaining * price

    return _Facts(
        window_start=window_start,
        window_end=window_end,
        starting_cash=start_state.cash,
        ending_cash=end_state.cash,
        starting_equity=start_state.cash + basis_start,
        ending_equity=end_state.cash + basis_end + unrealized,
        starting_unrealized=_ZERO,
        ending_unrealized=unrealized,
        mark_source="shadow:quotes" if marks and not unmarked else "shadow:cost_basis",
        mark_as_of=as_of if marks else None,
        mark_stale=unmarked,
        buying_power=end_state.cash,
        replay=replay,
        confirmed_fees=_ZERO,
        unattributed_fees=_ZERO,
        estimated_fees=estimated,
        unavailable_orders=(),
        deposits=deposits,
        withdrawals=withdrawals,
        income=_ZERO,
        corporate=_ZERO,
        commitments=_commitments(reserved, replay.basis_end),
        budget_total=end_state.total_budget,
        budget_per_symbol=end_state.per_symbol_budget,
    )


# ------------------------------------------------------------------------- builder


def build_account_ledger_report(
    scope: AccountScope | str | Iterable[AccountScope | str],
    period: ReportPeriod,
    as_of: datetime,
    *,
    store: ReadStore | None = None,
    shadow_ledger: ShadowLedger | None = None,
    budget: BudgetPolicy | None = None,
    fee_schedule: FeeSchedule | None = None,
    marks: Mapping[str, Decimal] | None = None,
    tolerance: Decimal = DEFAULT_TOLERANCE,
    max_mark_age: timedelta = timedelta(minutes=15),
) -> AccountLedgerReport:
    """Build the report for exactly one account/scenario and period.

    PAPER/LIVE scopes need ``store`` (a read-only store whose database identity
    matches the scope's mode); SHADOW scopes need ``shadow_ledger`` bound to the
    same scope.  Supplying the other mode's source is refused.
    """
    resolved = require_single_scope(scope)
    as_of = _utc(as_of, "as_of")
    if not isinstance(period, ReportPeriod):
        raise ValueError("period must be a ReportPeriod")
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, Decimal)
        or not tolerance.is_finite()
        or tolerance < _ZERO
    ):
        raise ValueError("tolerance must be a finite Decimal >= 0")

    if resolved.environment is AccountEnvironment.SHADOW:
        if store is not None or shadow_ledger is None:
            raise ModeIsolationError(
                "a SHADOW report needs its own shadow ledger and no broker store",
                code="report_mode_mismatch",
            )
        if shadow_ledger.scope != resolved:
            raise ModeIsolationError(
                f"ledger scope {shadow_ledger.scope.scope_id!r} differs from report scope "
                f"{resolved.scope_id!r}",
                code="report_scope_mismatch",
            )
        facts = _load_shadow_facts(shadow_ledger, resolved, period, as_of, marks)
    elif resolved.environment in _BROKER_ENVIRONMENTS:
        if shadow_ledger is not None or store is None:
            raise ModeIsolationError(
                "a PAPER/LIVE report needs a read store and no shadow ledger",
                code="report_mode_mismatch",
            )
        identity = store.database_identity()
        if identity is None or identity["environment"] != resolved.environment.value:
            raise ModeIsolationError(
                f"database identity {None if identity is None else identity['environment']!r} "
                f"differs from report mode {resolved.environment.value!r}",
                code="report_mode_mismatch",
            )
        with store.snapshot(resolved.scope_id) as conn:
            facts = _load_broker_facts(
                conn,
                resolved,
                period,
                as_of,
                budget=budget,
                fee_schedule=fee_schedule,
                max_mark_age=max_mark_age,
            )
    else:
        raise ModeIsolationError(
            f"reports do not support {resolved.environment.value}", code="report_mode_unsupported"
        )
    return _assemble(resolved, period, as_of, facts, tolerance)
