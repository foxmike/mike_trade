"""Unit tests for BUY backlog final momentum strategies.

Covers:
1. momentum_continuation (IMPLEMENTED)
2. rsi_momentum (IMPLEMENTED)
3. momentum_ignition (REQUIRES_SOURCE_DEFINITION)
4. volatility_expansion_continuation (EXISTING_EQUIVALENT -> atr_expansion_breakout)
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mike_trade import app
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain import market as domain_market
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderIntent, OrderPurpose, OrderSide, OrderType, TimeInForce
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ParameterValidationError
from mike_trade.strategies.buy.atr_expansion_breakout import AtrExpansionBreakoutBuy
from mike_trade.strategies.buy.momentum_continuation import MomentumContinuationBuy
from mike_trade.strategies.buy.rsi_momentum import RsiMomentumBuy

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()
ELIGIBLE = {"eligibility": "WATCHING_BUY"}


def make_candle(
    index: int,
    open_px: object,
    high_px: object,
    low_px: object,
    close_px: object,
    volume: int = 1000,
) -> Candle:
    start = OPEN + timedelta(minutes=index)
    return Candle(
        "AAPL",
        CandleTimeframe.M1,
        start,
        start + timedelta(minutes=1),
        D(str(open_px)),
        D(str(high_px)),
        D(str(low_px)),
        D(str(close_px)),
        D(volume),
        "sip",
        True,
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
    )


def make_context(
    candles: Sequence[Candle],
    *,
    symbol: str = "AAPL",
    session_status: SessionStatus = SessionStatus.REGULAR,
    stale: bool = False,
    session_id: str = "2026-10-08",
) -> MarketContext:
    as_of = (candles[-1].end + timedelta(seconds=1)) if candles else OPEN
    return MarketContext(
        symbol=symbol,
        as_of=as_of,
        version=len(candles),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=session_status,
        session_id=session_id,
        freshness=ContextFreshness(
            candles[-1].end if candles else OPEN,
            timedelta(seconds=1),
            timedelta(minutes=5),
            stale,
            "DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M1: tuple(candles)},
        indicators={},
    )


# ==============================================================================
# 1. MomentumContinuationBuy Tests
# ==============================================================================


def test_momentum_continuation_valid_trigger() -> None:
    strat = MomentumContinuationBuy()
    # Build 10 bars:
    # 0..3: Warmup base at 100.00
    # 4..6: 3 impulse bars: 100.15 -> 100.50 -> 101.00 (rising closes, advance >= 0.2%)
    # 7..8: 2 pause bars: high 100.80, low 100.60 (holds above origin 100.00, retrace <= 50%)
    # 9: Breakout bar: opens 100.70, closes 101.10 (breaks pause high 100.80, bullish candle)
    candles = [
        make_candle(0, "100.00", "100.05", "99.95", "100.00"),
        make_candle(1, "100.00", "100.05", "99.95", "100.00"),
        make_candle(2, "100.00", "100.05", "99.95", "100.00"),
        make_candle(3, "100.00", "100.05", "99.95", "100.00"),
        make_candle(4, "100.00", "100.20", "100.00", "100.15"),
        make_candle(5, "100.15", "100.60", "100.10", "100.50"),
        make_candle(6, "100.50", "101.05", "100.45", "101.00"),  # peak 101.05
        make_candle(7, "100.70", "100.80", "100.60", "100.65"),  # pause 1
        make_candle(8, "100.65", "100.75", "100.60", "100.70"),  # pause 2: pause_high=100.80
        make_candle(9, "100.70", "101.20", "100.65", "101.10"),  # breakout: close 101.10 > 100.80
    ]
    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval_res.is_signal is True
    assert eval_res.signal_id is not None
    assert eval_res.side is StrategyRole.BUY


def test_momentum_continuation_ineligible_blocked() -> None:
    strat = MomentumContinuationBuy()
    candles = [make_candle(i, "100.00", "100.05", "99.95", "100.00") for i in range(12)]
    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, {"eligibility": "BLOCKED"})
    assert eval_res.is_signal is False
    assert any(r.value == "NOT_ELIGIBLE" for r in eval_res.reason_codes)


def test_momentum_continuation_rejects_bearish_breakout_candle() -> None:
    strat = MomentumContinuationBuy()
    # Same as valid trigger but bar 9 closes lower than open: open 101.20, close 101.10
    candles = [
        make_candle(0, "100.00", "100.05", "99.95", "100.00"),
        make_candle(1, "100.00", "100.05", "99.95", "100.00"),
        make_candle(2, "100.00", "100.05", "99.95", "100.00"),
        make_candle(3, "100.00", "100.05", "99.95", "100.00"),
        make_candle(4, "100.00", "100.20", "100.00", "100.15"),
        make_candle(5, "100.15", "100.60", "100.10", "100.50"),
        make_candle(6, "100.50", "101.05", "100.45", "101.00"),
        make_candle(7, "100.70", "100.80", "100.60", "100.65"),
        make_candle(8, "100.65", "100.75", "100.60", "100.70"),
        make_candle(9, "101.20", "101.30", "100.65", "101.10"),  # open 101.20 > close 101.10
    ]
    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval_res.is_signal is False


def test_momentum_continuation_rejects_excessive_retrace() -> None:
    strat = MomentumContinuationBuy()
    # Impulse origin 100.00, peak 101.05 (range 1.05). Pause low drops to 100.25
    # (retrace 80 / 105 = 76% > 50%)
    candles = [
        make_candle(0, "100.00", "100.05", "99.95", "100.00"),
        make_candle(1, "100.00", "100.05", "99.95", "100.00"),
        make_candle(2, "100.00", "100.05", "99.95", "100.00"),
        make_candle(3, "100.00", "100.05", "99.95", "100.00"),
        make_candle(4, "100.00", "100.20", "100.00", "100.15"),
        make_candle(5, "100.15", "100.60", "100.10", "100.50"),
        make_candle(6, "100.50", "101.05", "100.45", "101.00"),
        make_candle(7, "100.60", "100.80", "100.25", "100.30"),  # low 100.25 (excessive retrace)
        make_candle(8, "100.30", "100.75", "100.30", "100.70"),
        make_candle(9, "100.70", "101.20", "100.65", "101.10"),
    ]
    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval_res.is_signal is False


def test_momentum_continuation_duplicate_suppression() -> None:
    strat = MomentumContinuationBuy()
    candles = [
        make_candle(0, "100.00", "100.05", "99.95", "100.00"),
        make_candle(1, "100.00", "100.05", "99.95", "100.00"),
        make_candle(2, "100.00", "100.05", "99.95", "100.00"),
        make_candle(3, "100.00", "100.05", "99.95", "100.00"),
        make_candle(4, "100.00", "100.20", "100.00", "100.15"),
        make_candle(5, "100.15", "100.60", "100.10", "100.50"),
        make_candle(6, "100.50", "101.05", "100.45", "101.00"),
        make_candle(7, "100.70", "100.80", "100.60", "100.65"),
        make_candle(8, "100.65", "100.75", "100.60", "100.70"),
        make_candle(9, "100.70", "101.20", "100.65", "101.10"),
    ]
    ctx = make_context(candles)
    # First evaluation succeeds
    eval1 = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval1.is_signal is True
    assert eval1.state_updates.get("last_signal_bar") == candles[-1].start.isoformat()

    # Second evaluation with updated state is suppressed
    eval2 = strat.evaluate(
        ctx, {}, {**ELIGIBLE, "last_signal_bar": eval1.state_updates["last_signal_bar"]}
    )
    assert eval2.is_signal is False
    assert any(r.value == "NO_SIGNAL" for r in eval2.reason_codes)


def test_momentum_continuation_insufficient_bars() -> None:
    strat = MomentumContinuationBuy()
    candles = [make_candle(i, "100.00", "100.05", "99.95", "100.00") for i in range(5)]
    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval_res.is_signal is False
    assert any(r.value == "INSUFFICIENT_DATA" for r in eval_res.reason_codes)


def test_momentum_continuation_non_adjacent_bars() -> None:
    strat = MomentumContinuationBuy()
    candles = [make_candle(i, "100.00", "100.05", "99.95", "100.00") for i in range(10)]
    # Introduce a 5-minute gap before the last bar
    late_start = OPEN + timedelta(minutes=15)
    candles[-1] = Candle(
        "AAPL",
        CandleTimeframe.M1,
        late_start,
        late_start + timedelta(minutes=1),
        D("100.00"),
        D("100.05"),
        D("99.95"),
        D("100.00"),
        D(1000),
        "sip",
        True,
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
    )
    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval_res.is_signal is False
    assert any(r.value == "INSUFFICIENT_DATA" for r in eval_res.reason_codes)


def test_momentum_continuation_parameter_validation() -> None:
    strat = MomentumContinuationBuy()
    # Schema validation rejects invalid parameter value bounds
    with pytest.raises(ParameterValidationError):
        strat.spec.parameter_schema.validate({"impulse_bars": 0})
    with pytest.raises(ParameterValidationError):
        strat.spec.parameter_schema.validate({"max_retrace": D("1.5")})

    # Strategy evaluate rejects min_pause_bars > max_pause_bars
    candles = [make_candle(i, "100.00", "100.05", "99.95", "100.00") for i in range(12)]
    ctx = make_context(candles)
    with pytest.raises(ParameterValidationError):
        strat.evaluate(ctx, {"min_pause_bars": 5, "max_pause_bars": 2}, ELIGIBLE)


# ==============================================================================
# 2. RsiMomentumBuy Tests
# ==============================================================================


def test_rsi_momentum_valid_trigger() -> None:
    strat = RsiMomentumBuy()
    # We construct a series of 70 bars.
    # Default params: strength=2, lookback=50, rsi_period=14, min_rsi=30, max_rsi=55.
    # Swing low 1 at bar 35 (price low = 104.00, RSI = ~54.2)
    # Swing low 2 at bar 67 (price low = 106.50 > 104.00, RSI = ~31.2 < 54.2 within 30..55)
    # Bar 69 confirms bar 67 (strength=2: 67 + 2 = 69 == len(bars) - 1).
    closes = [D("100.00")] * 70
    for i in range(10, 34):
        closes[i] = D("100.00") + D(str(i - 9)) * D("0.3")
    closes[34] = D("105.50")
    closes[35] = D("104.50")
    closes[36] = D("105.80")
    closes[37] = D("107.00")
    for i in range(38, 62):
        closes[i] = D("107.00") + D(str(i - 37)) * D("0.2")
    closes[62] = D("111.50")
    closes[63] = D("110.50")
    closes[64] = D("109.50")
    closes[65] = D("108.50")
    closes[66] = D("107.50")
    closes[67] = D("106.80")
    closes[68] = D("107.80")
    closes[69] = D("108.80")

    candles = [
        make_candle(
            i,
            closes[i] - D("0.10"),
            closes[i] + D("0.20"),
            closes[i] - D("0.20"),
            closes[i],
        )
        for i in range(70)
    ]
    # Set explicit swing pivot lows
    candles[35] = make_candle(35, "105.00", "105.20", "104.00", "104.50")
    candles[67] = make_candle(67, "107.20", "107.40", "106.50", "106.80")

    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval_res.is_signal is True
    assert eval_res.signal_id is not None
    assert eval_res.side is StrategyRole.BUY


def test_rsi_momentum_rejects_lower_price_low() -> None:
    strat = RsiMomentumBuy()
    # Same base as valid trigger, but at bar 67 the price drops lower than bar 35
    closes = [D("100.00")] * 70
    for i in range(10, 34):
        closes[i] = D("100.00") + D(str(i - 9)) * D("0.3")
    closes[34] = D("105.50")
    closes[35] = D("104.50")
    closes[36] = D("105.80")
    closes[37] = D("107.00")
    for i in range(38, 62):
        closes[i] = D("107.00") + D(str(i - 37)) * D("0.2")
    closes[62] = D("111.50")
    closes[63] = D("110.50")
    closes[64] = D("109.50")
    closes[65] = D("108.50")
    closes[66] = D("107.50")
    closes[67] = D("103.50")  # lower than bar 35
    closes[68] = D("104.50")
    closes[69] = D("105.50")

    candles = [
        make_candle(
            i,
            closes[i] - D("0.10"),
            closes[i] + D("0.20"),
            closes[i] - D("0.20"),
            closes[i],
        )
        for i in range(70)
    ]
    candles[35] = make_candle(35, "105.00", "105.20", "104.00", "104.50")
    candles[67] = make_candle(67, "104.00", "104.20", "103.00", "103.50")

    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval_res.is_signal is False


def test_rsi_momentum_ineligible_blocked() -> None:
    strat = RsiMomentumBuy()
    candles = [make_candle(i, "100.00", "100.05", "99.95", "100.00") for i in range(70)]
    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, {"eligibility": "BLOCKED"})
    assert eval_res.is_signal is False
    assert any(r.value == "NOT_ELIGIBLE" for r in eval_res.reason_codes)


def test_rsi_momentum_insufficient_bars() -> None:
    strat = RsiMomentumBuy()
    candles = [make_candle(i, "100.00", "100.05", "99.95", "100.00") for i in range(30)]
    ctx = make_context(candles)
    eval_res = strat.evaluate(ctx, {}, ELIGIBLE)
    assert eval_res.is_signal is False
    assert any(r.value == "INSUFFICIENT_DATA" for r in eval_res.reason_codes)


def test_rsi_momentum_parameter_validation() -> None:
    strat = RsiMomentumBuy()
    # Schema validation bounds
    with pytest.raises(ParameterValidationError):
        strat.spec.parameter_schema.validate({"strength": 10})
    with pytest.raises(ParameterValidationError):
        strat.spec.parameter_schema.validate({"rsi_period": 1})

    # Cross-parameter validation inside evaluate
    candles = [make_candle(i, "100.00", "100.05", "99.95", "100.00") for i in range(70)]
    ctx = make_context(candles)
    with pytest.raises(ParameterValidationError):
        strat.evaluate(ctx, {"strength": 4, "lookback": 15}, ELIGIBLE)
    with pytest.raises(ParameterValidationError):
        strat.evaluate(ctx, {"min_rsi_low": D("60"), "max_rsi_low": D("40")}, ELIGIBLE)


# ==============================================================================
# 3. Candidate Determinations & Registry Tests
# ==============================================================================


def test_registration_in_production_and_research() -> None:
    production_src = inspect.getsource(app.build_runtime)
    research_src = inspect.getsource(_research_registry)

    for cls in (MomentumContinuationBuy, RsiMomentumBuy):
        assert production_src.count(f"{cls.__name__},") == 1
        assert 1 <= research_src.count(f"{cls.__name__},") <= 2
        assert REGISTRY.get(cls.spec.strategy_id) is cls
        assert cls.spec.side is StrategyRole.BUY
        assert cls.spec.warmup_bars == cls.required_bars({})


def test_library_counts_after_batch_final_momentum() -> None:
    counts = {r: len(REGISTRY.list_strategies(r)) for r in StrategyRole}
    assert counts == {
        StrategyRole.BUY: 93,
        StrategyRole.SELL: 43,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 28,
    }
    assert len(REGISTRY.list_strategies()) == 192


def test_volatility_expansion_continuation_equivalence() -> None:
    """Volatility Expansion Continuation maps to existing AtrExpansionBreakoutBuy."""
    assert "volatility_expansion_continuation" not in {
        s.spec.strategy_id for s in REGISTRY.list_strategies()
    }
    cls = REGISTRY.get("atr_expansion_breakout")
    assert cls is AtrExpansionBreakoutBuy
    assert cls.spec.side is StrategyRole.BUY


def test_momentum_ignition_requires_source_definition() -> None:
    """Momentum Ignition is a manipulative practice / lacks canonical rule; not registered."""
    assert "momentum_ignition" not in {s.spec.strategy_id for s in REGISTRY.list_strategies()}


# ==============================================================================
# 4. Representative Backtest Behavior
# ==============================================================================


def test_backtest_runner_integration(tmp_path: Path) -> None:
    minute = timedelta(minutes=1)
    provenance = domain_market.FeedProvenance("replay", "IEX", "synthetic", False)

    # Generate 40 bars with a valid momentum continuation setup
    events = []
    prices = [D("100.00")] * 20 + [
        D("100.15"),
        D("100.50"),
        D("101.00"),  # impulse (bars 20..22)
        D("100.65"),
        D("100.70"),  # pause (bars 23..24, pause_high=100.80)
        D("101.10"),  # breakout (bar 25, close 101.10 > 100.80)
    ] + [D("101.15")] * 14

    for index, close in enumerate(prices):
        start = OPEN + index * minute
        open_px = close
        high = close + D("0.05")
        low = close - D("0.05")

        if index == 20:
            open_px = D("100.00")
            high = D("100.20")
            low = D("100.00")
        elif index == 21:
            open_px = D("100.15")
            high = D("100.60")
            low = D("100.10")
        elif index == 22:
            open_px = D("100.50")
            high = D("101.05")
            low = D("100.45")
        elif index == 23:
            open_px = D("100.70")
            high = D("100.80")
            low = D("100.60")
        elif index == 24:
            open_px = D("100.65")
            high = D("100.75")
            low = D("100.60")
        elif index == 25:
            open_px = D("100.70")
            high = D("101.20")
            low = D("100.65")

        events.append(
            ReplayEvent.bar(
                domain_market.Candle(
                    "AAPL",
                    domain_market.Timeframe.M1,
                    start,
                    start + minute,
                    open_px,
                    high,
                    low,
                    close,
                    D(1000),
                    provenance,
                )
            )
        )

    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(
        events, DataProvenance("synthetic", "IEX", False, "r1"), clock=clock, seed=42
    )
    simulator = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())

    def intent(evaluation, market_context, request):  # noqa: ANN001, ARG001
        return OrderIntent(
            intent_id=f"intent-{evaluation.signal_id}",
            client_order_id=f"order-{evaluation.signal_id}",
            account_scope_id="backtest:test",
            cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol,
            side=OrderSide.BUY,
            purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=market_context.as_of,
            quantity=D(10),
        )

    strat_id = "momentum_continuation"
    request = BacktestRequest(
        symbols=("AAPL",),
        window_start=OPEN,
        window_end=OPEN + timedelta(minutes=40),
        period_role=PeriodRole.RESEARCH,
        config={},
        seed=42,
        starting_capital=D(100000),
        strategy_ids=(strat_id,),
        strategy_params={strat_id: {}},
        initial_strategy_state={strat_id: dict(ELIGIBLE)},
        context_history_bars=30,
        result_directory=tmp_path,
    )

    runner = BacktestRunner(REGISTRY, feed, clock, simulator, intent_factory=intent)
    result = asyncio.run(runner.run(request))

    assert {row["strategy"] for row in result.evaluations} == {strat_id}
    assert any(row["strategy"] == strat_id for row in result.signals)
    assert any(fill["side"] == "BUY" for fill in result.fills)
