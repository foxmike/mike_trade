"""Strategy adapters, two-stage protection, daily-loss guard and the backtest lifecycle of the
corrected Michelle VWAP Bounce/Break (business rules V2, 1-51).

The end-to-end tests replay a synthetic session through the real ``BacktestRunner`` and
execution simulator; unit tests cover the BUY/SELL adapters on hand-built contexts.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain.market import Candle as ReplayCandle
from mike_trade.domain.market import FeedProvenance, Timeframe
from mike_trade.domain.protection import ProtectionKind
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext, TradeSnapshot
from mike_trade.risk import michelle_corrected_stop as stops
from mike_trade.risk.daily_loss import (
    DEFAULT_DAILY_LOSS_LIMIT,
    DailyLossGuard,
    effective_limit,
)
from mike_trade.scheduling.calendar import SessionStatus, session_for
from mike_trade.signals.decisions import ExitCandidate, arbitrate_exit_decisions
from mike_trade.strategies import michelle_corrected as mc
from mike_trade.strategies.base import ReasonCode
from mike_trade.strategies.buy.vwap_bounce_break_michelle import VwapBounceBreakMichelleBuy
from mike_trade.strategies.buy.vwap_bounce_break_michelle_corrected import (
    VwapBounceBreakMichelleCorrectedBuy,
)
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.strategies.sell.base import EXIT_PRIORITY, ExitSource
from mike_trade.strategies.sell.vwap_bounce_break_michelle import VwapBounceBreakMichelleSell
from mike_trade.strategies.sell.vwap_bounce_break_michelle_corrected import (
    VwapBounceBreakMichelleCorrectedSell,
)

from michelle_corrected_support import (  # noqa: E402  (tests/unit is on sys.path)
    BUY_ID,
    DAY,
    MIN,
    OPEN,
    REGULAR,
    SELL_ID,
    SESSION,
    cd,
    ctx,
    developing_candle,
    premarket,
    qqq_bars,
    qqq_session,
    regular_bars,
)


# ----------------------------------------------------------------------- registration


def test_corrected_strategies_register_independently_of_the_originals() -> None:
    registry = StrategyRegistry()
    for cls in (
        VwapBounceBreakMichelleBuy,
        VwapBounceBreakMichelleSell,
        VwapBounceBreakMichelleCorrectedBuy,
        VwapBounceBreakMichelleCorrectedSell,
    ):
        registry.register(cls)
    ids = {item.spec.strategy_id for item in registry.list_strategies()}
    assert ids == {
        "vwap_bounce_break_michelle_buy",
        "vwap_bounce_break_michelle_sell",
        BUY_ID,
        SELL_ID,
    }
    assert VwapBounceBreakMichelleBuy.spec.version == "1.0.0"  # originals untouched
    assert VwapBounceBreakMichelleSell.spec.version == "1.1.0"
    assert VwapBounceBreakMichelleCorrectedBuy.spec.reference_symbols == ("QQQ",)
    assert stops.CORRECTED_BUY_ID == BUY_ID and stops.CORRECTED_SELL_ID == SELL_ID


def test_rule1_buy_is_a_long_entry_strategy_on_one_minute_candles() -> None:
    spec = VwapBounceBreakMichelleCorrectedBuy.spec
    assert spec.side is StrategyRole.BUY
    assert spec.required_timeframes == (CandleTimeframe.M1,)
    assert spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.version == "2.0.0"  # V2 rules: results of V1 runs are not comparable
    assert "final_2.docx" in spec.source


def test_corrected_buy_is_not_subject_to_the_original_attempt_limiter() -> None:
    from mike_trade.strategies.michelle_common import CORRECTED_BUY_IDS, MICHELLE_BUY_IDS

    assert not CORRECTED_BUY_IDS & MICHELLE_BUY_IDS


# ------------------------------------------------------------------ Rules 42-46: stops


@pytest.mark.parametrize(
    ("fill", "initial"),
    [("100.00", "99.25"), ("100.004", "99.25"), ("100.0099", "99.25"), ("10.00", "9.25")],
)
def test_rule42_initial_stop_is_fill_minus_075_and_never_follows_vwap(fill: str, initial: str) -> None:
    assert stops.initial_stop(D(fill)) == D(initial)


def test_rule42_no_valid_stop_below_a_fill_at_or_under_075() -> None:
    assert stops.initial_stop(D("0.75")) is None
    assert stops.initial_stop(D("0.50")) is None
    assert stops.initial_stop(D("0.76")) == D("0.0100")


def test_rule43_activation_is_exactly_fill_plus_050() -> None:
    fill = D("100.00")
    assert not stops.stage_two_reached(D("100.49"), fill)
    assert stops.stage_two_reached(D("100.50"), fill)
    assert not stops.stage_two_reached(None, fill)


def test_rule43_policy_carries_initial_activation_and_breakeven() -> None:
    policy = stops.policy_for_fill(D("100.004"))
    assert policy is not None and policy.kind is ProtectionKind.FIXED_THEN_BREAKEVEN_STOP
    assert policy.initial_stop_price == D("99.25")
    assert policy.activation_reference == D("100.504")
    assert policy.breakeven_price == D("100.00")  # price <= 100.004 is price <= 100.00 on the grid
    assert stops.policy_for_fill(D("0.70")) is None


def test_rule44_stage_one_only_the_initial_stop_applies() -> None:
    fill = D("100.00")
    assert stops.exit_trigger(D("99.26"), fill, D("99.0"), stage_two=False) is None
    assert stops.exit_trigger(D("99.25"), fill, D("99.0"), stage_two=False) is stops.ExitTrigger.INITIAL_STOP
    # below breakeven but above the initial stop: nothing before Stage 2
    assert stops.exit_trigger(D("99.90"), fill, D("101.0"), stage_two=False) is None


def test_rule44_stage_two_breakeven_touch_exits_inclusive() -> None:
    fill = D("100.00")
    assert stops.exit_trigger(D("100.01"), fill, None, stage_two=True) is None
    assert stops.exit_trigger(D("100.00"), fill, None, stage_two=True) is stops.ExitTrigger.BREAKEVEN
    assert stops.exit_trigger(D("99.99"), fill, None, stage_two=True) is stops.ExitTrigger.BREAKEVEN


def test_rule44_stage_two_ema9_is_strictly_below_and_uses_the_last_completed_candle() -> None:
    fill = D("100.00")
    ema9 = D("100.40")
    assert stops.exit_trigger(D("100.40"), fill, ema9, stage_two=True) is None  # not strictly below
    assert stops.exit_trigger(D("100.39"), fill, ema9, stage_two=True) is stops.ExitTrigger.EMA9
    assert stops.exit_trigger(D("100.41"), fill, ema9, stage_two=True) is None


def test_rule45_effective_stop_never_drops_below_breakeven_and_ema_part_can_ease() -> None:
    fill = D("100.00")
    assert stops.effective_stop_level(fill, None, stage_two=False) == D("99.25")
    assert stops.effective_stop_level(fill, D("99.0"), stage_two=True) == D("100.00")  # floor
    assert stops.effective_stop_level(fill, D("101.234"), stage_two=True) == D("101.23")
    assert stops.effective_stop_level(fill, D("101.20"), stage_two=True) == D("101.19")
    # price < EMA9 is price <= the last grid price strictly below it
    assert stops.ema9_stop_level(D("101.20")) == D("101.19")
    assert stops.ema9_stop_level(D("101.201")) == D("101.20")


def test_rule46_policy_validation_rejects_a_backward_ladder() -> None:
    from mike_trade.domain.protection import ProtectionPolicy

    with pytest.raises(ValueError):
        ProtectionPolicy(
            ProtectionKind.FIXED_THEN_BREAKEVEN_STOP,
            initial_stop_price=D("100"), activation_reference=D("100.5"), breakeven_price=D("99"),
        )


# --------------------------------------------------------------- Rules 47-48: account loss


def test_rule47_daily_loss_reaches_or_exceeds_150_realized_plus_unrealized() -> None:
    at = OPEN + 10 * MIN
    guard = DailyLossGuard()
    assert guard.limit == DEFAULT_DAILY_LOSS_LIMIT == D("150.00")
    below = guard.observe(at, D("-100"), D("-49.99"))
    assert not below.latched and not guard.entries_blocked(at)
    exact = guard.observe(at, D("-100"), D("-50"))
    assert exact.latched and exact.newly_latched and guard.entries_blocked(at)
    assert guard.liquidation_required(at, 1) and not guard.liquidation_required(at, 0)


def test_rule47_latch_survives_a_pnl_recovery_and_resets_next_trading_day() -> None:
    at = OPEN + 10 * MIN
    guard = DailyLossGuard()
    guard.observe(at, D("-200"), D("0"))
    recovered = guard.observe(at + 5 * MIN, D("-200"), D("+190"))
    assert recovered.latched and not recovered.breached_now and not recovered.newly_latched
    assert guard.entries_blocked(at + 6 * MIN)
    next_day = at + timedelta(days=1)
    guard.observe(next_day, D("0"), D("0"))
    assert not guard.entries_blocked(next_day)


def test_rule47_stricter_account_limit_is_honoured_and_default_is_not_loosened() -> None:
    assert effective_limit(None) == D("150.00")
    assert effective_limit(D("100")) == D("100")
    assert effective_limit(D("300")) == D("150.00")


@pytest.mark.parametrize(
    ("day", "expected_close_hour"),
    [(date(2026, 10, 8), 16), (date(2026, 11, 27), 13), (date(2026, 12, 24), 13)],
)
def test_rule48_liquidation_is_five_minutes_before_close_on_standard_and_early_sessions(
    day: date, expected_close_hour: int
) -> None:
    from mike_trade.scheduling.calendar import NEW_YORK, eod_close_time

    session = session_for(day)
    start = eod_close_time(session, 15, 55).astimezone(NEW_YORK)
    close = session.close_utc.astimezone(NEW_YORK)
    assert close.hour == expected_close_hour
    assert close - start == timedelta(minutes=5)


# ------------------------------------------------------ JEV exemption: protective source


def test_corrected_sell_is_a_protective_exit_that_outranks_and_bypasses_minimum_hold() -> None:
    from mike_trade.domain.trading import ManagementStatus, Position, PositionDirection

    assert EXIT_PRIORITY[ExitSource.PROTECTIVE_STOP] < EXIT_PRIORITY[ExitSource.STRATEGY_SELL]
    position = Position(
        "scope", "ABC", D("10"), D("100"), D("1000"), OPEN, management=ManagementStatus.MANAGED,
        cycle_id="c1", direction=PositionDirection.LONG,
    )
    result = arbitrate_exit_decisions(
        [
            ExitCandidate(
                signal_id="p", source=ExitSource.PROTECTIVE_STOP, symbol="ABC",
                position_cycle_id="c1", bar_time=OPEN,
            ),
            ExitCandidate(
                signal_id="s", source=ExitSource.STRATEGY_SELL, symbol="ABC", strategy_id="other",
                version="1.0.0", position_cycle_id="c1", bar_time=OPEN,
            ),
        ],
        symbol="ABC", position=position, evaluation_time=OPEN + MIN,
        minimum_hold=timedelta(hours=1), entry_filled_at=OPEN,
    )
    assert result.selected is not None and result.selected.signal_id == "p"


# ----------------------------------------------------------- BUY/SELL adapter contexts


def test_fixture_reaches_a_confirmed_window_with_a_hammer() -> None:
    bars = mc.prepare_session(premarket() + regular_bars(), DAY)
    snapshot = mc.scan(bars)
    assert snapshot.phase is mc.Phase.WINDOW
    assert snapshot.setup_start == OPEN + 3 * MIN
    assert snapshot.pattern is not None and snapshot.pattern.kind is mc.PatternKind.HAMMER
    assert snapshot.confirm_close == D("100.3")


def test_rule33_buy_live_tick_in_c_plus_one_signals_with_evidence_and_intrabar_semantics() -> None:
    candles = regular_bars()
    dev = developing_candle(7, "100.35", "100.45", "100.35", "100.45")
    context, state = ctx(candles, developing=dev, trade="100.45", qqq=qqq_session(7), qqq_price="408")
    evaluation = VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state)
    assert evaluation.is_signal, evaluation.reason_codes
    assert evaluation.reference_price == D("100.45")
    updates = evaluation.state_updates
    assert updates["entry_mode"] == "LIVE"
    assert "HAMMER" in str(updates["entry_evidence"])
    assert "window_number" in str(updates["entry_evidence"])
    assert evaluation.bar_time == dev.start


def test_rule33_35_buy_live_tick_below_confirmation_close_waits_then_triggers_without_green_candle() -> None:
    candles = regular_bars()
    below = developing_candle(7, "100.10", "100.15", "100.05", "100.10")
    context, state = ctx(candles, developing=below, trade="100.10", qqq=qqq_session(7), qqq_price="408")
    assert not VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal
    # the same (still red) candle later prints the confirmation close: immediate market buy
    hit = developing_candle(7, "100.10", "100.30", "100.05", "100.30")
    context, state = ctx(candles, developing=hit, trade="100.30", qqq=qqq_session(7), qqq_price="408")
    assert VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal


def test_rule30_33_38_buy_live_rejects_trades_above_the_frozen_ceiling_and_c_plus_six() -> None:
    candles = regular_bars()
    snapshot = mc.scan(mc.prepare_session(premarket() + candles, DAY))
    assert snapshot.entry_ceiling is not None
    ceiling = snapshot.entry_ceiling  # Candle C's VWAP + 0.50, fixed when C completed
    rich = str((ceiling + D("0.01")).quantize(D("0.01")))
    dev = developing_candle(7, rich, rich, rich, rich)
    context, state = ctx(candles, developing=dev, trade=rich, qqq=qqq_session(7), qqq_price="408")
    assert not VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal
    ok = str(ceiling.quantize(D("0.01"), rounding="ROUND_DOWN"))  # on the ceiling: allowed
    dev = developing_candle(7, ok, ok, ok, ok)
    context, state = ctx(candles, developing=dev, trade=ok, qqq=qqq_session(7), qqq_price="408")
    assert VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal
    late_candles = regular_bars([("100.4", "100.5", "100.3", "100.4", "1000")] * 5)
    late = developing_candle(12, "100.4", "100.5", "100.4", "100.5")  # C+6
    context, state = ctx(
        late_candles, developing=late, trade="100.45", qqq=qqq_session(12), qqq_price="410"
    )
    assert not VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal


def test_rule33_buy_live_ask_above_the_frozen_ceiling_defers_the_entry() -> None:
    candles = regular_bars()
    dev = developing_candle(7, "100.55", "100.60", "100.55", "100.60")
    context, state = ctx(
        candles, developing=dev, trade="100.60", qqq=qqq_session(7), quote=("100.60", "100.70")
    )
    assert not VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal
    context, state = ctx(
        candles, developing=dev, trade="100.60", qqq=qqq_session(7), quote=("100.60", "100.62")
    )
    assert VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal


#: Candle C closes 101.80 while its VWAP is 100.2764, so the frozen ceiling (100.7764) is below the
#: confirmation close: Scenario 2 (wait for a pullback; EMA9 99.755 / EMA20 99.474 are below it).
SCENARIO_TWO = [
    *REGULAR[:6],
    ("98.7", "101.9", "98.6", "101.8", "9000"),
]


def test_rule34_buy_live_scenario_two_waits_for_a_pullback_to_the_ceiling_above_vwap_and_emas() -> None:
    candles = regular_bars()[:6] + [cd("ABC", OPEN + 6 * MIN, *SCENARIO_TWO[6])]
    snapshot = mc.scan(mc.prepare_session(premarket() + candles, DAY))
    assert snapshot.scenario is mc.EntryScenario.ABOVE_CEILING
    assert snapshot.confirm_close == D("101.8") and snapshot.entry_ceiling < snapshot.confirm_close
    strategy = VwapBounceBreakMichelleCorrectedBuy()
    for trade, expected in (
        ("101.80", False),  # at the confirmation close: above the ceiling, wait
        ("100.80", False),  # still above the ceiling
        ("100.60", True),  # pulled back below the confirmation close: allowed in Scenario 2
        ("100.15", False),  # at/under the last completed candle's VWAP
    ):
        dev = developing_candle(7, trade, trade, trade, trade)
        context, state = ctx(candles, developing=dev, trade=trade, qqq=qqq_session(7))
        assert strategy.evaluate(context, {}, state).is_signal is expected, trade


def test_rule40_buy_requires_qqq_momentum_and_fails_closed_without_qqq_data() -> None:
    candles = regular_bars()
    dev = developing_candle(7, "100.35", "100.45", "100.35", "100.45")
    context, state = ctx(candles, developing=dev, trade="100.45")  # no QQQ data at all
    assert not VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal
    falling = premarket("QQQ", "410") + [
        cd("QQQ", OPEN + i * MIN, str(410 - i), str(411 - i), str(409 - i), str(410 - i), "5000")
        for i in range(7)
    ]
    context, state = ctx(candles, developing=dev, trade="100.45", qqq=falling, qqq_price="403")
    assert not VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal
    # without QQQ premarket candles the EMAs are undefined: fails closed
    context, state = ctx(candles, developing=dev, trade="100.45", qqq=qqq_bars(7), qqq_price="403")
    assert not VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state).is_signal


def test_rule5_buy_ignores_stale_data_non_eligible_symbols_and_held_positions() -> None:
    candles = regular_bars()
    dev = developing_candle(7, "100.35", "100.45", "100.35", "100.45")
    stale_ctx, state = ctx(candles, developing=dev, trade="100.45", qqq=qqq_session(7), stale=True)
    ev = VwapBounceBreakMichelleCorrectedBuy().evaluate(stale_ctx, {}, state)
    assert ev.reason_codes == (ReasonCode.STALE_DATA,)
    held_ctx, held_state = ctx(
        candles, developing=dev, trade="100.45", qqq=qqq_session(7), qqq_price="408", eligibility=False
    )
    ev = VwapBounceBreakMichelleCorrectedBuy().evaluate(held_ctx, {}, held_state)
    assert not ev.is_signal and ev.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    assert ev.state_updates["was_held"] == "1"  # the held position keeps the setup purged


def test_rule38_49_one_setup_one_trade_consumption_and_exit_purge_need_a_fresh_setup() -> None:
    candles = regular_bars()
    dev = developing_candle(7, "100.35", "100.45", "100.35", "100.45")
    context, state = ctx(candles, developing=dev, trade="100.45", qqq=qqq_session(7), qqq_price="408")
    strategy = VwapBounceBreakMichelleCorrectedBuy()
    first = strategy.evaluate(context, {}, state)
    assert first.is_signal
    state.update(first.state_updates)
    state["consumed_after"] = context.as_of.isoformat()  # what the engine records on submit
    again = strategy.evaluate(context, {}, state)
    assert not again.is_signal  # the filled setup is permanently consumed
    # hold (position open), then flat again: nothing before the exit may be reused
    held_ctx, _ = ctx(candles, developing=dev, trade="100.45", qqq=qqq_session(7), eligibility=False)
    held = strategy.evaluate(held_ctx, {}, {**state, "eligibility": "HELD"})
    state.update(held.state_updates)
    flat_after_exit = strategy.evaluate(context, {}, state)
    assert not flat_after_exit.is_signal


def test_rule50_setup_state_never_leaks_into_the_next_session() -> None:
    candles = regular_bars()
    dev = developing_candle(7, "100.35", "100.45", "100.35", "100.45")
    context, state = ctx(candles, developing=dev, trade="100.45", qqq=qqq_session(7))
    # yesterday's consumption / purge / held markers must not block (or resurrect) anything today
    state.update(
        {
            "session_id": "2026-10-07",
            "consumed_after": "2026-10-08T20:00:00+00:00",
            "purge_barrier": "2026-10-08T20:00:00+00:00",
            "was_held": "1",
        }
    )
    ev = VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state)
    assert ev.is_signal
    assert ev.state_updates["session_id"] == "2026-10-08"
    assert ev.state_updates["consumed_after"] == "" and ev.state_updates["purge_barrier"] == ""


def test_rule10_buy_intrabar_dip_breach_purges_the_setup_and_persists_the_barrier() -> None:
    candles = regular_bars()
    # developing low 2.4 below the setup's low breaches $3.00 against the VWAP of that print
    dev = developing_candle(7, "100.35", "100.45", "95.00", "100.45")
    context, state = ctx(candles, developing=dev, trade="100.45", qqq=qqq_session(7), qqq_price="408")
    ev = VwapBounceBreakMichelleCorrectedBuy().evaluate(context, {}, state)
    assert not ev.is_signal
    assert ev.state_updates["purge_barrier"] == dev.end.isoformat()


def test_rule43_44_sell_adapter_stage_one_and_two_with_intrabar_ticks() -> None:
    candles = regular_bars()
    base_state = {
        "position_cycle_id": "cycle-1",
        "position_fill_price": "100.00",
        "position_fill_time": (OPEN + 7 * MIN).isoformat(),
    }
    sell = VwapBounceBreakMichelleCorrectedSell()

    def tick(price: str, high: str | None = None) -> tuple[MarketContext, dict[str, object]]:
        dev = developing_candle(8, price, high or price, price, price)
        context, _ = ctx(candles, developing=dev, trade=price)
        return context, dict(base_state)

    context, state = tick("99.26")
    assert not sell.evaluate(context, {}, state).is_signal  # inside Stage 1, above the stop
    context, state = tick("99.25")
    ev = sell.evaluate(context, {}, state)
    assert ev.is_signal and ev.state_updates["exit_trigger"] == "INITIAL_STOP"
    # before Stage 2 a drop under breakeven is NOT an exit
    context, state = tick("99.80")
    assert not sell.evaluate(context, {}, state).is_signal
    # Stage 2 reached (engine/stop tracker supplied the high): breakeven touch exits
    context, state = tick("100.00")
    state["highest_trade_price"] = "100.50"
    ev = sell.evaluate(context, {}, state)
    assert ev.is_signal and ev.state_updates["exit_trigger"] == "BREAKEVEN"
    # the activation print itself is not an exit
    context, state = tick("100.50")
    assert not sell.evaluate(context, {}, state).is_signal
    assert sell.evaluate(context, {}, state).state_updates["stage_two_active"] == "1"


def test_rule44_sell_adapter_ema9_trailing_exit_uses_last_completed_candle_not_the_forming_one() -> None:
    # price run-up long enough that the last completed candle's EMA9 sits above the fill
    run = [
        (str(100 + 0.2 * i - 0.1), str(100 + 0.2 * i + 0.1), str(100 + 0.2 * i - 0.15),
         str(100 + 0.2 * i), "1000")
        for i in range(1, 30)
    ]
    candles = [cd("ABC", OPEN + i * MIN, *spec) for i, spec in enumerate([*REGULAR, *run])]
    bars = mc.prepare_session(premarket() + candles, DAY)
    ema9 = bars[-1].ema9
    assert ema9 is not None and ema9 > D("100.5")
    sell = VwapBounceBreakMichelleCorrectedSell()
    state = {
        "position_cycle_id": "c", "position_fill_price": "100.00",
        "position_fill_time": (OPEN + 8 * MIN).isoformat(), "highest_trade_price": "110",
    }
    minute = len(candles)
    below = str(ema9 - D("0.01"))
    context, _ = ctx(candles, developing=developing_candle(minute, below, below, below, below),
                     trade=below)
    ev = sell.evaluate(context, {}, dict(state))
    assert ev.is_signal and ev.state_updates["exit_trigger"] == "EMA9"
    at = str(ema9)
    context, _ = ctx(candles, developing=developing_candle(minute, at, at, at, at), trade=at)
    assert not sell.evaluate(context, {}, dict(state)).is_signal  # not STRICTLY below


def test_rule51_sell_adapter_bar_replay_resolves_a_both_touched_candle_as_the_initial_stop() -> None:
    candles = regular_bars([("100.4", "101.5", "99.2", "100.6", "1000")])  # spans stop and +0.50
    state = {
        "position_cycle_id": "c", "position_fill_price": "100.00",
        "position_fill_time": candles[-1].start.isoformat(),
    }
    context, _ = ctx(candles)
    ev = VwapBounceBreakMichelleCorrectedSell().evaluate(context, {}, state)
    assert ev.is_signal and ev.state_updates["exit_trigger"] == "INITIAL_STOP"  # rule 51


# --------------------------------------------------------------------- backtest lifecycle


def replay_event(candle: Candle) -> ReplayEvent:
    return ReplayEvent.bar(
        ReplayCandle(
            candle.symbol, Timeframe.M1, candle.start, candle.end, candle.open, candle.high,
            candle.low, candle.close, candle.volume, FeedProvenance("replay", "IEX", "synthetic", False),
        )
    )


def run_backtest(  # noqa: ANN202
    tmp_path, tail, *, quantity: str = "10", qqq: int = 24, confirmation=None  # noqa: ANN001
):
    regular = regular_bars(list(tail))
    if confirmation is not None:  # replace Candle C (09:36) to select Scenario 2
        regular[6] = cd("ABC", OPEN + 6 * MIN, *confirmation)
    abc = premarket() + regular
    last_end = abc[-1].end
    events = [replay_event(c) for c in abc]
    events += [replay_event(c) for c in premarket("QQQ", "400")]
    events += [replay_event(c) for c in qqq_bars(qqq)]
    start = OPEN - 40 * MIN
    clock = ReplayClock(start)
    feed = HistoricalReplayFeed(
        events, DataProvenance("synthetic", "IEX", False, "corrected-v1"), clock=clock, seed=5
    )
    execution = HistoricalExecutionSimulator(feed, clock)
    registry = StrategyRegistry()
    registry.register(VwapBounceBreakMichelleCorrectedBuy)
    registry.register(VwapBounceBreakMichelleCorrectedSell)

    def intent_factory(evaluation, market_context, _request):  # noqa: ANN001
        buy = evaluation.side is StrategyRole.BUY
        return OrderIntent(
            intent_id=f"intent-{evaluation.signal_id}",
            client_order_id=f"client-{evaluation.signal_id}",
            account_scope_id="backtest:corrected",
            cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol,
            side=OrderSide.BUY if buy else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if buy else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=market_context.as_of,
            quantity=D(quantity) if buy else execution.held_quantity(evaluation.symbol),
        )

    request = BacktestRequest(
        symbols=("ABC",),
        window_start=start,
        window_end=last_end,
        period_role=PeriodRole.RESEARCH,
        config={},
        seed=5,
        starting_capital=D("100000"),
        strategy_ids=(BUY_ID, SELL_ID),
        context_history_bars=500,
        result_directory=tmp_path,
    )
    result = asyncio.run(
        BacktestRunner(registry, feed, clock, execution, intent_factory=intent_factory).run(request)
    )
    return result, abc


def test_rule33_42_backtest_entry_is_modeled_at_the_c_plus_one_open_with_stage_one_stop(tmp_path) -> None:
    result, bars = run_backtest(tmp_path, [("100.40", "100.60", "100.30", "100.50", "1000")])
    buys = [f for f in result.fills if f["side"] == "BUY"]
    assert len(buys) == 1
    assert buys[0]["price"] == D("100.40")  # the open of C+1 (09:37), not the next bar's open
    assert buys[0]["time"] == bars[-1].start
    events = [e for e in result.protection_events if e["event_type"] == "INITIAL_STOP_ESTABLISHED"]
    assert events and events[0]["stop_price"] == D("99.65")  # fill - 0.75, fixed
    assert result.cycles[0]["protection_mode"] == "FIXED_THEN_BREAKEVEN"
    assert result.cycles[0]["entry_strategy"] == BUY_ID


def test_rule43_44_46_backtest_stage_two_moves_to_breakeven_then_breach_exits_as_a_protective_stop(tmp_path) -> None:
    result, bars = run_backtest(
        tmp_path,
        [
            ("100.40", "101.20", "100.30", "101.00", "1000"),  # reaches fill + 0.50 = 100.90
            ("101.00", "101.05", "100.30", "100.40", "1000"),  # falls through breakeven 100.40
        ],
    )
    moved = [e for e in result.protection_events if e["event_type"] == "STOP_MOVED"]
    assert moved and moved[0]["new_stop_price"] == D("100.40")  # breakeven = fill
    sells = [f for f in result.fills if f["side"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["exit_reason"] == "PROTECTIVE_STOP"
    assert sells[0]["price"] == D("100.40")
    # one setup = one trade: no second entry after the stop-out
    assert len([f for f in result.fills if f["side"] == "BUY"]) == 1


def test_rule51_backtest_candle_touching_stop_and_activation_is_the_stop_first(tmp_path) -> None:
    result, _ = run_backtest(
        tmp_path, [("100.40", "101.50", "99.50", "100.60", "1000")]  # spans 99.65 and 100.90
    )
    sells = [f for f in result.fills if f["side"] == "SELL"]
    assert len(sells) == 1 and sells[0]["price"] == D("99.65")  # rule 51: initial stop first
    assert not [e for e in result.protection_events if e["event_type"] == "STOP_MOVED"]


def test_rule33_35_backtest_deferred_trigger_fills_at_confirmation_close_inside_the_candle(tmp_path) -> None:
    # C+1 opens below Confirmation_Close 100.30 and trades through it
    result, bars = run_backtest(tmp_path, [("100.10", "100.40", "100.05", "100.20", "1000")])
    buys = [f for f in result.fills if f["side"] == "BUY"]
    assert len(buys) == 1
    assert buys[0]["price"] == D("100.30")
    assert buys[0]["time"] == bars[-1].end  # modeled at the latest possible instant


def test_rule33_backtest_no_entry_when_c_plus_one_stays_above_the_frozen_ceiling(tmp_path) -> None:
    # ceiling = 100.1264 + 0.50 (Candle C's VWAP, frozen): the whole candle trades above it
    result, _ = run_backtest(tmp_path, [("101.50", "101.80", "101.40", "101.60", "1000")])
    assert not [f for f in result.fills if f["side"] == "BUY"]


def test_rule35_backtest_scenario_one_gap_above_the_ceiling_fills_when_price_falls_to_the_ceiling(
    tmp_path,
) -> None:
    result, bars = run_backtest(tmp_path, [("101.00", "101.10", "100.50", "100.70", "1000")])
    (buy,) = [f for f in result.fills if f["side"] == "BUY"]
    assert buy["price"] == D("100.62")  # highest tick at/below the frozen ceiling 100.6264
    assert buy["time"] == bars[-1].end  # modeled at the latest possible instant


SCENARIO_TWO_C = ("98.7", "101.9", "98.6", "101.8", "9000")  # close 101.80 > ceiling 100.7764


def test_rule34_backtest_scenario_two_enters_on_a_pullback_below_the_confirmation_close(tmp_path) -> None:
    # opens above the ceiling, falls to it: entry BELOW the confirmation close (Scenario 2 only)
    result, _ = run_backtest(
        tmp_path, [("101.20", "101.30", "100.50", "100.60", "1000")], confirmation=SCENARIO_TWO_C
    )
    (buy,) = [f for f in result.fills if f["side"] == "BUY"]
    assert buy["price"] == D("100.77") and buy["price"] < D("101.80")
    # an opening print already inside the band is an exact open fill
    result, bars = run_backtest(
        tmp_path / "open", [("100.50", "100.70", "100.40", "100.60", "1000")],
        confirmation=SCENARIO_TWO_C,
    )
    (buy,) = [f for f in result.fills if f["side"] == "BUY"]
    assert buy["price"] == D("100.50") and buy["time"] == bars[-1].start


def test_rule36_backtest_scenario_two_never_enters_at_or_below_the_vwap_benchmark(tmp_path) -> None:
    # band is (100.2764, 100.7764]: the candle only trades at/under the last completed VWAP
    result, _ = run_backtest(
        tmp_path, [("100.25", "100.27", "100.10", "100.20", "1000")], confirmation=SCENARIO_TWO_C
    )
    assert not [f for f in result.fills if f["side"] == "BUY"]
    # rising into the band: the first qualifying tick above the benchmark VWAP (100.28)
    result, _ = run_backtest(
        tmp_path / "rise", [("100.20", "100.40", "100.10", "100.30", "1000")],
        confirmation=SCENARIO_TWO_C,
    )
    (buy,) = [f for f in result.fills if f["side"] == "BUY"]
    assert buy["price"] == D("100.28")


def test_rule37_backtest_candle_closing_below_vwap_in_the_window_purges_before_any_later_fill(tmp_path) -> None:
    tail = [
        ("100.0", "100.05", "99.20", "99.40", "1000"),  # C+1 gaps under the floor, closes below VWAP
        ("100.40", "100.60", "100.30", "100.50", "1000"),  # a later qualifying print: purged
    ]
    result, _ = run_backtest(tmp_path, tail)
    assert not [f for f in result.fills if f["side"] == "BUY"]


def test_rule48_backtest_forces_end_of_day_liquidation_for_the_corrected_strategies(tmp_path) -> None:
    # EOD is a property of the corrected strategies even when the run config does not enable it
    result, bars = run_backtest(
        tmp_path,
        [("100.40", "100.80", "100.30", "100.60", "1000")]
        + [("100.60", "100.80", "100.50", "100.70", "1000")] * 2,
    )
    assert [f for f in result.fills if f["side"] == "BUY"]
    # the replay ends at 09:39, long before 15:55: the position is still open there
    assert result.cycles[-1]["closed_at"] is None


def test_backtest_original_michelle_strategies_do_not_see_the_corrected_machinery(tmp_path) -> None:
    from mike_trade.strategies.michelle_common import MICHELLE_BUY_IDS

    assert BUY_ID not in MICHELLE_BUY_IDS
    # Originals keep their own protection kind
    from mike_trade.risk import michelle_stop

    original = michelle_stop.policy_for_fill(D("100"))
    assert original is not None and original.kind is ProtectionKind.FIXED_THEN_TRAILING_STOP


# ------------------------------------------------------------------------- SHADOW stops


def test_shadow_stop_moves_to_breakeven_at_activation_and_fills_on_breach(tmp_path) -> None:
    from test_shadow_stops import Harness, _trade, run as run_async

    from mike_trade.brokers.shadow.stops import ShadowStopSimulator, StopTriggerOutcome

    from test_shadow_stops import NOW

    h = Harness(tmp_path / "corrected-shadow.db")
    h.account.update_quote("ABC", D("100"))
    h.buy(price=D("100"))
    sim = ShadowStopSimulator(h.account)
    policy = stops.policy_for_fill(D("100"))
    armed = run_async(sim.arm("cycle-1", "ABC", policy, at=NOW))
    assert armed.stop_price == D("99.25")

    def step(price: str, sec: int):  # noqa: ANN202
        return run_async(
            sim.evaluate("cycle-1", h.quote(price, sec), at=NOW + timedelta(seconds=sec),
                         trade=_trade(price, sec))
        )

    assert step("100.49", 1).stop_price == D("99.25")  # Stage 1 until fill + 0.50
    assert step("100.50", 2).stop_price == D("100.00")  # breakeven, never VWAP/percent based
    assert step("100.20", 3).stop_price == D("100.00")  # never lowered
    final = step("99.99", 4)
    assert final.outcome is StopTriggerOutcome.TRIGGERED


def test_shadow_account_books_realized_pnl_per_new_york_trading_day(tmp_path) -> None:
    from test_shadow_stops import NOW, Harness

    h = Harness(tmp_path / "corrected-day.db")
    h.buy(price=D("100"))
    from mike_trade.domain.trading import Fill

    sell = OrderIntent(
        "exit-1", "exit-client-1", h.account.scope.scope_id, "cycle-1", "ABC", OrderSide.SELL,
        OrderPurpose.STRATEGY_EXIT, OrderType.MARKET, TimeInForce.DAY, NOW, quantity=D("10"),
    )
    order = asyncio.run(h.account.submit_order(sell))
    h.account.apply_fill(
        Fill("exit-fill-1", order.broker_order_id, h.account.scope.scope_id, "ABC",
             OrderSide.SELL, D("10"), D("90"), D("0"), NOW)
    )
    from mike_trade.scheduling.calendar import NEW_YORK

    assert h.account.state.realized_on(NOW.astimezone(NEW_YORK).date()) == D("-100")
