"""Autonomous PAPER/LIVE strategy lifecycle on the existing engine.

``LiveStrategyLifecycle`` is the Alpaca counterpart of the SHADOW ``VwapReferenceLifecycle`` and
*inherits* its strategy machinery unchanged: per-symbol BUY/SELL collections, persisted strategy
state, the shared immutable ``MarketContext``, entry/exit arbitration, Michelle's entry facts and
protection policy.  Only the execution side differs - there is no simulated account; orders go to
the broker through :class:`~mike_trade.live.executor.LiveExecutor`, accounting goes through the
ledger, and :class:`~mike_trade.live.reconciler.LiveReconciler` keeps both aligned with what
the broker actually holds.

Rules shared with SHADOW (one trading rule set in every mode):

* flat -> all BUY strategies evaluate, ``arbitrate_entry_decisions`` selects at most one entry;
  a held or pending symbol can never be entered again (no pyramiding);
* held -> all SELL strategies evaluate, ``arbitrate_exit_decisions`` selects one full-remainder
  exit; the protective stop is engine-owned and is cancelled (confirmed) only to make way for it;
* Michelle SELL strategies are advisory mirrors of the engine-owned fixed-to-trailing stop.

PAPER/LIVE specifics: broker truth is verified (``ensure_fresh``) before any order is decided,
protection is placed from the broker's own fill facts the moment a fill appears, and nothing is
ever marked closed without a broker confirmation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace
from typing import Any, cast

from mike_trade.app import (
    MICHELLE_BUY_IDS,
    VwapReferenceLifecycle,
    _Binding,
    _Collections,
)
from mike_trade.domain.errors import BrokerError, EntryBlockedError
from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.domain.trading import (
    EntryEligibilityPolicy,
    ManagementStatus,
    OrderPurpose,
    Position,
    PositionCycle,
    PositionCycleState,
    PositionDirection,
)
from mike_trade.live.executor import (
    LiveExecutor,
    ProtectionFailure,
    StopRuntime,
    policy_from_dict,
    policy_to_dict,
)
from mike_trade.live.gateway import LiveGateway, OrderNotSubmitted, UnknownOrderOutcome
from mike_trade.live.ledger import LiveLedger
from mike_trade.live.reconciler import LiveReconciler, ReconcileReport, SymbolState
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.market_data.context import MarketContext
from mike_trade.persistence.repositories import EntryControlRepository
from mike_trade.runtime import EntryGate
from mike_trade.scheduling import calendar as exchange_calendar
from mike_trade.signals.decisions import (
    EntryCandidate,
    ExitCandidate,
    arbitrate_entry_decisions,
    arbitrate_exit_decisions,
)
from mike_trade.strategies.base import StrategyEvaluation
from mike_trade.strategies.michelle_common import (
    current_session_vwap,
    decode_attempts,
    encode_attempts,
    entry_attempt_allowed,
    normalized_attempts,
    regular_session_bars,
    safe_decimal,
    signal_facts,
)
from mike_trade.strategies.sell.base import ExitSource

__all__ = ["LiveSettings", "LiveStrategyLifecycle"]

ZERO = Decimal("0")
_MICHELLE_SELL_IDS = frozenset({"five_minute_orb_michelle_sell", "vwap_bounce_break_michelle_sell"})
_DONE = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED", "REPLACED"})


@dataclass(frozen=True, slots=True)
class LiveSettings:
    per_symbol_budget: Decimal
    minimum_hold: timedelta
    eligibility: EntryEligibilityPolicy
    eod_enabled: bool = False
    #: Liquidation starts this long before the session close (default 15:55 on a 16:00 close,
    #: 12:55 on a 13:00 early close).
    eod_offset: timedelta = timedelta(minutes=5)
    eod_max_attempts: int = 6
    entry_polls: int = 6
    entry_poll_interval: float = 0.5
    eod_interval: float = 5.0


class _LiveGroup:
    """Just enough of ``ShadowBrokerGroup`` for the inherited strategy-state helpers."""

    def __init__(self, persistence: Any) -> None:
        self.persistence = persistence

    def validate_startup(self) -> bool:
        return True


class LiveStrategyLifecycle(VwapReferenceLifecycle):
    def __init__(
        self,
        *,
        gateway: LiveGateway,
        ledger_factory: Callable[[], LiveLedger],
        persistence: Any,
        clock: Any,
        entry_gate: EntryGate,
        cutoff_minutes: int,
        protection_policy: ProtectionPolicy,
        buy_parameters: Mapping[str, object],
        sell_parameters: Mapping[str, object],
        assignments: Any,
        registry: Any,
        stop_specs: Mapping[str, Mapping[str, object]],
        settings: LiveSettings,
        symbols: tuple[str, ...],
        reconcile_interval: float = 10.0,
        sleep: Any = asyncio.sleep,
        watcher_factory: Callable[[LiveReconciler], Any] | None = None,
    ) -> None:
        super().__init__(
            cast(Any, _LiveGroup(persistence)), clock, entry_gate, cutoff_minutes,
            protection_policy, buy_parameters, sell_parameters,
            assignments=assignments, registry=registry, stop_specs=stop_specs,
        )
        self.gateway = gateway
        self._ledger_factory = ledger_factory
        self.settings = settings
        self.symbols = tuple(symbols)
        self.sleep = sleep
        self._reconcile_interval = reconcile_interval
        self._watcher_factory = watcher_factory
        self.watcher: Any = None
        self._policies: dict[str, ProtectionPolicy] = {}
        self.eod_active = False
        self._eod_day: date | None = None
        self._eod_attempts: dict[str, int] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._ratchet_inflight: set[str] = set()
        self.alerts: list[str] = []
        self.account_check: Any = None
        self._bound = False
        # Bound once the database is open (``bind``); typed as Any so the SHADOW helpers
        # inherited from VwapReferenceLifecycle accept the duck-typed scenario.
        self.ledger: LiveLedger = cast(Any, None)
        self.executor: LiveExecutor = cast(Any, None)
        self.reconciler: LiveReconciler = cast(Any, None)
        self.controls: EntryControlRepository = cast(Any, None)
        self.scenario: Any = None

    def bind(self) -> None:
        """Create the ledger-backed parts; requires the operational database to be open."""
        if self._bound:
            return
        self.ledger = self._ledger_factory()
        self.executor = LiveExecutor(self.gateway, self.ledger, self.clock)
        self.reconciler = LiveReconciler(
            self.gateway, self.ledger, self.executor, self.clock, symbols=self.symbols,
            policy_for=self._policy_for_cycle, escalate=self.safety_exit,
            interval=self._reconcile_interval, on_cycle_closed=self._cycle_closed,
        )
        self.controls = EntryControlRepository(self.ledger.writer, self.clock)
        self.scenario = SimpleNamespace(
            account=SimpleNamespace(scope=self.ledger.scope), states={},
            settings=SimpleNamespace(
                minimum_hold=self.settings.minimum_hold,
                per_symbol_budget=self.settings.per_symbol_budget,
                allow_fractional=False, short_selling_enabled=False,
            ),
        )
        if self._watcher_factory is not None:
            self.watcher = self._watcher_factory(self.reconciler)
        self.account_check = self.gateway.account_check
        self._bound = True

    def validate_startup(self) -> bool:
        """Runtime's VALIDATING_ENVIRONMENT hook: the database is open, so bind now."""
        self.bind()
        return True

    # ================================================================ startup

    def required_capabilities(self) -> dict[str, set[str]]:
        needs: dict[str, set[str]] = {}
        for symbol in self.symbols:
            kinds = {self._policy_for(symbol, Decimal("100")).kind}
            collections = self.collections(symbol)
            if any(b.strategy_id in MICHELLE_BUY_IDS for b in collections.buys):
                kinds.add(ProtectionKind.FIXED_THEN_TRAILING_STOP)
            slot = needs.setdefault(symbol, set())
            for kind in kinds:
                if kind is ProtectionKind.BROKER_TRAILING_STOP:
                    slot.add("trailing_submit")
                else:
                    slot.add("stop_submit")
                    if kind in (ProtectionKind.FIXED_THEN_TRAILING_STOP, ProtectionKind.STEPPED_TRAILING_STOP):
                        slot.add("stop_replace")
        return needs

    def verify_startup(self) -> object:
        self.bind()
        reasons: list[str] = []
        available = self.gateway.stop_capabilities()
        for symbol, needs in sorted(self.required_capabilities().items()):
            for need in sorted(needs):
                if not available.get(need, False):
                    reasons.append(f"{symbol}:CAPABILITY_UNPROVEN:{need}")
        report = self.reconciler.last
        if report is not None:
            for symbol, state in report.states.items():
                if state.managed_quantity > ZERO and not state.protected and not state.exit_pending:
                    reasons.append(f"{symbol}:PROTECTIVE_COVERAGE_DRIFT")
        return SimpleNamespace(
            clean=not reasons, entries_permitted=not reasons, reasons=tuple(reasons),
            status="CLEAN" if not reasons else "DRIFTED",
        )

    async def reconcile_at_startup(self, _facts: Mapping[str, object] | None = None) -> object:
        """Runtime's RECONCILING phase: repair what can be repaired, report what cannot."""
        self.bind()
        report = await self.reconciler.reconcile("startup")
        reasons: list[str] = []
        if not report.ok:
            reasons.extend(f"BROKER_READ_FAILED:{item}" for item in report.failures)
        for symbol, state in report.states.items():
            for reason in state.reasons:
                if reason.startswith(("UNPROTECTED", "PROTECTION_FAILED")):
                    reasons.append(f"{symbol}:{reason}")
        for symbol, state in report.states.items():
            if state.unmanaged_quantity > ZERO:
                self.alerts.append(f"{symbol}: {state.unmanaged_quantity} unmanaged shares")
        return SimpleNamespace(
            clean=not reasons, entries_permitted=not reasons, reasons=tuple(reasons),
            status="CLEAN" if not reasons else "DRIFTED",
        )

    # ============================================================ background

    async def start_background(self) -> None:
        self.bind()
        self.reconciler.start()
        if self.watcher is not None:
            self._tasks.append(asyncio.get_running_loop().create_task(self.watcher.run(), name="live-trade-updates"))
        if self.settings.eod_enabled:
            self._tasks.append(asyncio.get_running_loop().create_task(self._eod_loop(), name="live-eod"))

    async def stop_background(self) -> None:
        if not self._bound:
            return
        await self.reconciler.stop()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # =========================================================== evaluation

    async def process(self, contexts: Mapping[str, MarketContext]) -> dict[str, list[str]]:
        self.bind()
        result: dict[str, list[str]] = {"live": []}
        self._in_flight += 1
        try:
            for symbol, context in contexts.items():
                if context.symbol != symbol.upper():
                    raise ValueError("context mapping key must match context.symbol")
                async with self.reconciler.lock:
                    result["live"].extend(await self._process_symbol_live(context))
            return result
        finally:
            self._in_flight -= 1

    def observe_trade(self, symbol: str, price: Decimal, at: datetime) -> None:
        """Every traded price feeds the high-water mark; the ratchet runs off the hot path."""
        if not self._bound or symbol not in self.symbols:
            return
        cycle = self.ledger.active_cycle(symbol)
        if cycle is None or cycle.opened_at is None or at < cycle.opened_at:
            return
        runtime = self.executor.stops.get(cycle.cycle_id)
        if runtime is None or runtime.order_id is None:
            return
        if price > runtime.high:
            runtime.high = price
            if symbol not in self._ratchet_inflight:
                self._ratchet_inflight.add(symbol)
                with contextlib.suppress(RuntimeError):
                    asyncio.get_running_loop().create_task(self._ratchet_symbol(symbol))

    async def _ratchet_symbol(self, symbol: str) -> None:
        try:
            async with self.reconciler.lock:
                cycle = self.ledger.active_cycle(symbol)
                if cycle is None:
                    return
                runtime = self.executor.stops.get(cycle.cycle_id)
                if runtime is not None and runtime.order_id is not None:
                    await self._guard_ratchet(runtime, runtime.high)
        finally:
            self._ratchet_inflight.discard(symbol)

    async def _guard_ratchet(self, runtime: StopRuntime, high: Decimal) -> None:
        try:
            await self.executor.ratchet(runtime, high)
        except (BrokerError, ProtectionFailure) as exc:
            self.alerts.append(f"{runtime.symbol}: ratchet failed: {exc}")

    def _bar_highs_since(self, context: MarketContext, since: datetime | None) -> Decimal | None:
        if since is None:
            return None
        highs = [
            bar.high for bar in context.candles.get(CandleTimeframe.M1, ())
            if bar.start >= since
        ]
        developing = (getattr(context, "developing_candles", {}) or {}).get(CandleTimeframe.M1)
        trade = getattr(context, "latest_trade", None)
        if trade is not None and trade.provider_ts >= since:
            highs.append(trade.price)
        if developing is not None and developing.start >= since:
            highs.append(developing.high)
        return max(highs) if highs else None

    async def _process_symbol_live(self, context: MarketContext) -> list[str]:
        symbol = context.symbol
        actions: list[str] = []
        collections = self.collections(symbol)
        cycle = self.ledger.active_cycle(symbol)
        state = self.reconciler.state(symbol)

        # Keep the broker stop tight with whatever the market just printed.
        if cycle is not None and cycle.opened_at is not None:
            runtime = self.executor.stops.get(cycle.cycle_id)
            high = self._bar_highs_since(context, cycle.opened_at)
            if runtime is not None and runtime.order_id is not None and high is not None:
                await self._guard_ratchet(runtime, high)

        holding = (
            cycle is not None
            and cycle.state in (PositionCycleState.OPEN, PositionCycleState.EXITING)
            and cycle.remaining_quantity > ZERO
        )
        if holding and cycle is not None:
            return actions + await self._evaluate_held(context, collections, cycle, state)
        return actions + await self._evaluate_flat(context, collections, cycle, state)

    # ---------------------------------------------------------------- held

    async def _evaluate_held(
        self, context: MarketContext, collections: _Collections, cycle: PositionCycle,
        state: SymbolState,
    ) -> list[str]:
        symbol = context.symbol
        for binding in (*collections.buys, *collections.shorts, *collections.covers):
            persisted = self._state(self.scenario, binding, symbol)
            persisted.pop("eligibility", None)  # held: entry strategies advance, never arm
            evaluation = binding.strategy.evaluate(context, binding.parameters, persisted)
            self._save_state(self.scenario, binding, symbol, evaluation.state_updates)
        exits: dict[str, _Binding] = {}
        for binding in collections.sells:
            persisted = self._state(self.scenario, binding, symbol)
            self._inject_live_michelle_state(collections, binding, cycle, persisted)
            evaluation = binding.strategy.evaluate(context, binding.parameters, persisted)
            self._save_state(self.scenario, binding, symbol, evaluation.state_updates)
            if evaluation.is_signal and binding.strategy_id not in _MICHELLE_SELL_IDS:
                exits[evaluation.signal_id or ""] = binding
        if not exits or state.managed_quantity <= ZERO and not state.exit_pending:
            return []
        quantity = state.managed_quantity or cycle.remaining_quantity
        reference = self._reference_price(cycle)
        position = Position(
            self.ledger.scope_id, symbol, quantity, reference, reference * quantity,
            context.as_of, management=ManagementStatus.MANAGED, cycle_id=cycle.cycle_id,
            direction=PositionDirection.LONG,
        )
        decision = arbitrate_exit_decisions(
            [
                ExitCandidate(
                    signal_id=signal_id, source=ExitSource.STRATEGY_SELL, symbol=symbol,
                    strategy_id=binding.strategy_id, version=binding.version,
                    position_cycle_id=cycle.cycle_id, bar_time=self._bar_time(context),
                )
                for signal_id, binding in exits.items()
            ],
            symbol=symbol, position=position, evaluation_time=context.as_of,
            minimum_hold=self.settings.minimum_hold, entry_filled_at=cycle.opened_at,
            pending_exit_source=ExitSource.STRATEGY_SELL if state.exit_pending else None,
        )
        if decision.selected is None:
            return []
        return await self._exit(context, cycle, decision.selected.signal_id)

    def _reference_price(self, cycle: PositionCycle) -> Decimal:
        price = cycle.avg_entry_price
        if price is not None:
            return price
        rows = [r for r in self.ledger.order_rows_for_cycle(cycle.cycle_id) if r.side == "BUY"]
        order = self.gateway.get_order(rows[0].broker_order_id) if rows else None
        return order.filled_avg_price if order and order.filled_avg_price else Decimal("1")

    def _inject_live_michelle_state(
        self, collections: _Collections, binding: _Binding, cycle: PositionCycle,
        persisted: dict[str, object],
    ) -> None:
        if binding.strategy_id not in _MICHELLE_SELL_IDS:
            return
        facts = self._michelle_entry_facts(self.scenario, collections, cycle.cycle_id, cycle.symbol)
        if facts is None:
            return
        persisted.update(
            {
                "position_cycle_id": cycle.cycle_id,
                "position_fill_price": str(self._reference_price(cycle)),
                "position_fill_time": (cycle.opened_at or self.clock.now()).isoformat(),
            }
        )
        if binding.strategy_id == "five_minute_orb_michelle_sell":
            if "entry_orb_high" in facts:
                persisted["position_orb_high"] = facts["entry_orb_high"]
        elif "position_vwap_at_entry" in facts:
            persisted["position_vwap_at_entry"] = facts["position_vwap_at_entry"]
        runtime = self.executor.stops.get(cycle.cycle_id)
        if runtime is not None:
            persisted["highest_trade_price"] = str(runtime.high)
            persisted["active_stop_price"] = str(runtime.stop_price)
            initial = runtime.policy.initial_stop_price
            persisted["active_stop_mode"] = (
                "TRAILING" if initial is not None and runtime.stop_price > initial else "FIXED"
            )

    async def _exit(self, context: MarketContext, cycle: PositionCycle, signal_id: str) -> list[str]:
        symbol = context.symbol
        try:
            report = await self.reconciler.reconcile_locked("pre-exit")
        except Exception:
            return ["EXIT_SKIPPED:STALE_HOLDINGS"]
        if not report.ok:
            return ["EXIT_SKIPPED:STALE_HOLDINGS"]
        state = report.states.get(symbol) or SymbolState(symbol)
        live_cycle = self.ledger.active_cycle(symbol)
        if live_cycle is None or state.managed_quantity <= ZERO or state.exit_pending:
            return ["EXIT_SKIPPED:NOT_HOLDING" if state.managed_quantity <= ZERO else "EXIT_SKIPPED:PENDING"]
        runtime = self.executor.stops.get(live_cycle.cycle_id)
        outcome = await self.executor.close(
            live_cycle, state.managed_quantity, "STRATEGY", signal_id=signal_id, runtime=runtime
        )
        actions = [f"EXIT:{outcome.status}:{outcome.broker_order_id or ''}"]
        if outcome.status in {"UNPROTECTED", "FROZEN"}:
            await self.safety_exit(live_cycle, f"strategy exit left the position {outcome.status}")
        await self.reconciler.reconcile_locked("post-exit")
        return actions

    # ---------------------------------------------------------------- flat

    async def _evaluate_flat(
        self, context: MarketContext, collections: _Collections, cycle: PositionCycle | None,
        state: SymbolState,
    ) -> list[str]:
        symbol = context.symbol
        for binding in (*collections.sells, *collections.covers):
            persisted = self._state(self.scenario, binding, symbol)
            evaluation = binding.strategy.evaluate(context, binding.parameters, persisted)
            self._save_state(self.scenario, binding, symbol, evaluation.state_updates)
        entries: dict[str, tuple[_Binding, StrategyEvaluation]] = {}
        for binding in collections.buys:
            persisted = self._state(self.scenario, binding, symbol)
            persisted["eligibility"] = "WATCHING_BUY"
            evaluation = binding.strategy.evaluate(context, binding.parameters, persisted)
            self._save_state(self.scenario, binding, symbol, evaluation.state_updates)
            if evaluation.is_signal:
                entries[evaluation.signal_id or ""] = (binding, evaluation)
        for binding in collections.shorts:
            # PAPER/LIVE shorts are not an autonomous path (unverified): advance-only.
            persisted = self._state(self.scenario, binding, symbol)
            persisted.pop("eligibility", None)
            evaluation = binding.strategy.evaluate(context, binding.parameters, persisted)
            self._save_state(self.scenario, binding, symbol, evaluation.state_updates)
        if not entries or not self._entry_permitted(PositionDirection.LONG) or self.eod_active:
            return []
        arbitration = arbitrate_entry_decisions(
            [
                EntryCandidate(
                    signal_id, binding.strategy_id, binding.version, symbol, evaluation.bar_time,
                    PositionDirection.LONG,
                )
                for signal_id, (binding, evaluation) in entries.items()
            ],
            symbol=symbol, evaluation_time=context.as_of, position_open=False,
            entry_pending=cycle is not None or state.entry_pending,
        )
        if arbitration.selected is None:
            return []
        binding, evaluation = entries[arbitration.selected.signal_id]
        return await self._enter(context, collections, binding, evaluation)

    def _eligibility_block(self, symbol: str, strategy_id: str, now: datetime) -> tuple[str, ...]:
        session_day = now.astimezone(exchange_calendar.NEW_YORK).date()
        eligibility = self.controls.load_eligibility_state(
            symbol, session_day,
            strategy_id if self.settings.eligibility.lockout_scope_by_strategy else None,
        )
        reasons = list(eligibility.entry_blocked(now))
        if (
            eligibility.filled_entry_cycles
            >= self.settings.eligibility.max_filled_entry_cycles_per_symbol_per_session
        ):
            reasons.append("max_entry_cycles")
        return tuple(reasons)

    async def _enter(
        self, context: MarketContext, collections: _Collections, binding: _Binding,
        evaluation: StrategyEvaluation,
    ) -> list[str]:
        symbol = context.symbol
        local_day = context.as_of.astimezone(exchange_calendar.NEW_YORK).date()
        session = exchange_calendar.session_for(local_day)
        if context.as_of >= exchange_calendar.entry_cutoff(session, self.cutoff_minutes):
            return []
        if binding.strategy_id in MICHELLE_BUY_IDS and not entry_attempt_allowed(
            self._shared_attempts(self.scenario, collections, symbol), context.as_of
        ):
            return []
        reference = evaluation.reference_price
        if reference is None:
            return []
        sizing_price = context.latest_quote.ask if context.latest_quote is not None else reference
        quantity = (self.settings.per_symbol_budget / sizing_price).to_integral_value(rounding=ROUND_DOWN)
        if quantity <= ZERO:
            return []
        blocked = self._eligibility_block(symbol, binding.strategy_id, context.as_of)
        if blocked:
            return [f"ENTRY_BLOCKED:{','.join(blocked)}"]
        # Broker truth first: never decide an entry from holdings that could not be verified.
        try:
            report = await self.reconciler.reconcile_locked("pre-entry")
        except Exception:
            return ["ENTRY_SKIPPED:STALE_HOLDINGS"]
        if not report.ok or self.reconciler.entries_blocked_reasons:
            return ["ENTRY_SKIPPED:STALE_HOLDINGS"]
        symbol_state = report.states.get(symbol) or SymbolState(symbol)
        if (
            symbol_state.frozen or symbol_state.broker_quantity > ZERO
            or symbol_state.unmanaged_quantity > ZERO or self.ledger.active_cycle(symbol) is not None
        ):
            return [f"ENTRY_BLOCKED:{','.join(symbol_state.reasons) or 'POSITION_EXISTS'}"]
        if self.account_check is not None:
            try:
                ok, why = self.account_check(quantity * sizing_price)
            except Exception:
                return ["ENTRY_SKIPPED:ACCOUNT_UNVERIFIED"]
            if not ok:
                self.alerts.append(f"{symbol}: entry refused, account {why}")
                return [f"ENTRY_BLOCKED:ACCOUNT_{why}"]

        cycle_id = f"{symbol}-{evaluation.signal_id}"
        if self.ledger.cycle(cycle_id) is not None:
            return []  # signal replay: the cycle for this signal already exists
        if binding.strategy_id in MICHELLE_BUY_IDS:
            self._record_michelle_attempt(self.scenario, collections, symbol, context.as_of)
            entry_state = self._state(self.scenario, binding, symbol)
            entry_state["position_cycle_id"] = cycle_id
            if binding.strategy_id == "vwap_bounce_break_michelle_buy":
                entry_state["consumed_after"] = context.as_of.isoformat()
            self._save_state(self.scenario, binding, symbol, entry_state)
            self._entry_facts[(self.ledger.scope_id, cycle_id)] = {
                "entry_strategy_id": binding.strategy_id, **signal_facts(entry_state),
                "position_cycle_id": cycle_id,
            }
        self.ledger.open_cycle(cycle_id, symbol, binding.strategy_id)
        try:
            order = self.executor.submit_entry(
                cycle_id, symbol, quantity, quantity * reference, str(evaluation.signal_id)
            )
        except EntryBlockedError as exc:
            self.ledger.abandon_cycle(cycle_id, f"budget: {exc}")
            return [f"ENTRY_BLOCKED:{getattr(exc, 'code', 'budget')}"]
        except UnknownOrderOutcome as exc:
            # The order may exist at the broker: keep the cycle and the UNKNOWN intent; the
            # reconciler resolves it by client_order_id and either adopts or retires it.
            self.alerts.append(f"{symbol}: entry outcome unknown: {exc}")
            return ["ENTRY_UNKNOWN"]
        except (BrokerError, OrderNotSubmitted) as exc:
            self.ledger.abandon_cycle(cycle_id, f"submit failed: {exc}")
            self.alerts.append(f"{symbol}: entry submit failed: {exc}")
            return [f"ENTRY_FAILED:{type(exc).__name__}"]
        actions = [f"ENTRY:{order.broker_order_id}"]
        order = await self._settle_entry(order.broker_order_id)
        if order is not None and order.filled_quantity > ZERO:
            actions.append(f"FILL:BUY:{order.filled_quantity}")
            self._finalize_entry_facts(context, collections, cycle_id, order.filled_avg_price)
            self._count_entry(cycle_id, symbol, binding.strategy_id, context.as_of)
        report = await self.reconciler.reconcile_locked("post-entry")
        state = report.states.get(symbol)
        if state is not None and state.protected:
            actions.append("PROTECTED")
        return actions

    async def _settle_entry(self, order_id: str) -> Any:
        order = None
        for _ in range(self.settings.entry_polls):
            order = self.gateway.get_order(order_id)
            if order is None:
                return None
            self.ledger.upsert_order(order)
            if order.is_terminal or order.filled_quantity >= order.quantity:
                return order
            await self.sleep(self.settings.entry_poll_interval)
        return order

    def _finalize_entry_facts(
        self, context: MarketContext, collections: _Collections, cycle_id: str,
        fill_price: Decimal | None,
    ) -> None:
        facts = self._michelle_entry_facts(self.scenario, collections, cycle_id, context.symbol)
        if facts is None:
            return
        facts.setdefault("position_fill_price", str(fill_price))
        facts.setdefault("position_fill_time", self.clock.now().isoformat())
        facts["position_cycle_id"] = cycle_id
        if (
            facts.get("entry_strategy_id") == "vwap_bounce_break_michelle_buy"
            and "position_vwap_at_entry" not in facts
        ):
            bars = regular_session_bars(context.candles.get(CandleTimeframe.M1, ()), context.session_id)
            fill_vwap = current_session_vwap(context, bars)
            if fill_vwap is not None:
                facts["position_vwap_at_entry"] = str(fill_vwap)  # frozen at the actual BUY fill
        policy = self._michelle_policy_from_facts(context.symbol, collections, facts)
        if policy is not None:
            facts["stop_policy"] = policy_to_dict(policy)
            self._policies[cycle_id] = policy
        for binding in collections.buys:
            if binding.strategy_id == facts.get("entry_strategy_id"):
                self._save_state(self.scenario, binding, context.symbol, facts)
                break

    def _count_entry(self, cycle_id: str, symbol: str, strategy_id: str, at: datetime) -> None:
        if self.ledger.once(f"entry-counted:{cycle_id}"):
            self.controls.increment_filled_entry_cycles(
                symbol, at.astimezone(exchange_calendar.NEW_YORK).date(), strategy_id
            )

    # ====================================================== protection policy

    def _michelle_policy_from_facts(
        self, symbol: str, collections: _Collections, facts: Mapping[str, object]
    ) -> ProtectionPolicy | None:
        sell_ids = {binding.strategy_id for binding in collections.sells}
        strategy_id = facts.get("entry_strategy_id")
        reference: Decimal | None = None
        if strategy_id == "five_minute_orb_michelle_buy" and "five_minute_orb_michelle_sell" in sell_ids:
            reference = safe_decimal(facts.get("entry_orb_high"))
        elif strategy_id == "vwap_bounce_break_michelle_buy" and "vwap_bounce_break_michelle_sell" in sell_ids:
            reference = safe_decimal(facts.get("position_vwap_at_entry") or facts.get("entry_vwap"))
        if reference is None or reference <= Decimal("0.25"):
            return None
        return ProtectionPolicy(
            ProtectionKind.FIXED_THEN_TRAILING_STOP, trail_percent=Decimal("0.5"),
            initial_stop_price=reference - Decimal("0.25"),
            activation_reference=reference + Decimal("0.25"), require_broker_held=True,
        )

    def _policy_for_cycle(self, cycle: PositionCycle) -> ProtectionPolicy | None:
        cached = self._policies.get(cycle.cycle_id)
        if cached is not None:
            return cached
        collections = self.collections(cycle.symbol)
        facts = self._michelle_entry_facts(self.scenario, collections, cycle.cycle_id, cycle.symbol)
        policy: ProtectionPolicy | None = None
        if facts is not None:
            stored = facts.get("stop_policy")
            if isinstance(stored, Mapping):
                policy = policy_from_dict(stored)
            else:
                policy = self._michelle_policy_from_facts(cycle.symbol, collections, facts)
        if policy is None:
            reference = self._reference_price(cycle)
            policy = self._policy_for(cycle.symbol, reference)
        self._policies[cycle.cycle_id] = policy
        return policy

    # ====================================================== safety and close

    async def safety_exit(self, cycle: PositionCycle, reason: str) -> None:
        """Flatten a position that cannot be kept protected (reconciler / close fallback)."""
        self.alerts.append(f"{cycle.symbol}: SAFETY EXIT: {reason}")
        self.executor.emit("SAFETY_EXIT_REQUESTED", position_id=cycle.cycle_id, symbol=cycle.symbol,
                           reason=reason)
        self.executor.coordinator.unfreeze(cycle.cycle_id)
        quantity = self.reconciler.state(cycle.symbol).managed_quantity or cycle.remaining_quantity
        if quantity <= ZERO:
            return
        runtime = self.executor.stops.get(cycle.cycle_id)
        outcome = await self.executor.close(
            cycle, quantity, "SAFETY_EXIT", signal_id=f"safety-{self.ledger.next_attempt(cycle.cycle_id, OrderPurpose.SAFETY_EXIT)}",
            runtime=runtime,
        )
        if outcome.status in {"UNPROTECTED", "FROZEN", "REJECTED"}:
            self.alerts.append(
                f"{cycle.symbol}: UNPROTECTED POSITION - safety exit {outcome.status}: {outcome.detail}"
            )

    def _cycle_closed(self, cycle: PositionCycle, external: bool) -> None:
        """Entry-eligibility bookkeeping when a cycle closes (once per cycle)."""
        if not self.ledger.once(f"cycle-closed:{cycle.cycle_id}"):
            return
        closed_at = cycle.closed_at or self.clock.now()
        session_day = closed_at.astimezone(exchange_calendar.NEW_YORK).date()
        strategy = cycle.entry_strategy_key
        if not external:
            self.controls.record_trade_result(
                cycle.symbol, session_day, cycle.realized_pnl, self.settings.eligibility,
                strategy if self.settings.eligibility.lockout_scope_by_strategy else None,
            )
        cooldown = self.settings.eligibility.post_sale_cooldown
        if cooldown > timedelta(0):
            self.controls.set_cooldown(
                cycle.symbol, closed_at + cooldown, "post_sale_cooldown",
                strategy if self.settings.eligibility.lockout_scope_by_strategy else None,
            )
        self.executor.retire(cycle.cycle_id)

    # ================================================================== EOD

    def eod_time(self, day: date) -> tuple[datetime, datetime] | None:
        """(liquidation start, session close) for ``day``; early closes are honoured."""
        try:
            session = exchange_calendar.session_for(day)
        except Exception:
            return None
        if not getattr(session, "is_trading_day", True):
            return None
        close = session.close_utc
        return close - self.settings.eod_offset, close

    async def _eod_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.eod_interval)
            try:
                await self.eod_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.alerts.append(f"EOD tick failed: {type(exc).__name__}: {exc}")

    async def eod_tick(self) -> list[str]:
        now = self.clock.now()
        day = now.astimezone(exchange_calendar.NEW_YORK).date()
        if self._eod_day != day:
            self._eod_day, self.eod_active = day, False
            self._eod_attempts.clear()
        window = self.eod_time(day)
        if window is None:
            return []
        start, close = window
        if not (start <= now < close):
            return []
        self.eod_active = True  # no new entries once liquidation has begun
        actions: list[str] = []
        async with self.reconciler.lock:
            report = await self.reconciler.reconcile_locked("eod")
            if not report.ok:
                self.alerts.append("EOD: broker read failed; will retry")
                return ["EOD_RETRY:STALE_HOLDINGS"]
            actions.extend(await self._cancel_pending_entries(report))
            for symbol, state in sorted(report.states.items()):
                if state.managed_quantity > ZERO:
                    actions.extend(await self._eod_close(symbol, state))
            await self.reconciler.reconcile_locked("eod-post")
        return actions

    async def _cancel_pending_entries(self, report: ReconcileReport) -> list[str]:
        actions: list[str] = []
        for row in self.ledger.working_order_rows():
            if row.side == "BUY" and row.purpose == "ENTRY":
                with contextlib.suppress(BrokerError):
                    await self.executor.cancel_order(row.broker_order_id)
                    actions.append(f"EOD_CANCEL_ENTRY:{row.broker_order_id}")
        return actions

    async def _eod_close(self, symbol: str, state: SymbolState) -> list[str]:
        cycle = self.ledger.active_cycle(symbol)
        if cycle is None:
            return []
        if state.exit_pending:
            return [f"EOD_PENDING:{symbol}"]
        attempts = self._eod_attempts.get(symbol, 0)
        if attempts >= self.settings.eod_max_attempts:
            self.alerts.append(f"{symbol}: EOD close failed {attempts} times; position still open")
            return [f"EOD_FAILED:{symbol}"]
        self._eod_attempts[symbol] = attempts + 1
        runtime = self.executor.stops.get(cycle.cycle_id)
        outcome = await self.executor.close(
            cycle, state.managed_quantity, "END_OF_DAY", signal_id=f"eod-{attempts}", runtime=runtime
        )
        if outcome.status in {"UNPROTECTED", "FROZEN"}:
            self.executor.coordinator.unfreeze(cycle.cycle_id)
        return [f"EOD_CLOSE:{symbol}:{outcome.status}"]

