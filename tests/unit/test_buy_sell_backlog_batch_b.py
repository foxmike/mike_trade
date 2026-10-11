"""Targeted unit tests for BUY/SELL Backlog Batch B strategies."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from mike_trade import app
from mike_trade.domain.strategy_types import StrategyRole
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
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.buy.intraday_consolidation_breakout import (
    IntradayConsolidationBreakoutBuy,
)
from mike_trade.strategies.buy.vwap_band_reversion import VWAPBandReversionBuy
from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy
from mike_trade.strategies.indicators.vwap_bands import (
    VWAPBands,
    VWAPDeviationBandsCalculator,
    session_bands_series,
    session_vwap_series,
)

D = Decimal
NY = ZoneInfo("America/New_York")
OPEN_NY = datetime(2026, 10, 8, 9, 30, tzinfo=NY)
OPEN_UTC = OPEN_NY.astimezone(UTC)
ELIGIBLE_STATE = {"eligibility": "WATCHING_BUY"}
REGISTRY = _research_registry()


def make_candle(
    minute_offset: int,
    open_price: object,
    high: object,
    low: object,
    close: object,
    volume: object = "100",
    *,
    base_time: datetime = OPEN_UTC,
    symbol: str = "SPY",
) -> Candle:
    start = base_time + timedelta(minutes=minute_offset)
    return Candle(
        symbol=symbol,
        timeframe=CandleTimeframe.M1,
        start=start,
        end=start + timedelta(minutes=1),
        open=D(str(open_price)),
        high=D(str(high)),
        low=D(str(low)),
        close=D(str(close)),
        volume=D(str(volume)),
        feed_id="sip",
        consolidated=True,
        origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
    )


def consistent_vwap(candles: Sequence[Candle]) -> dict[str, object]:
    """The shared ``vwap`` / ``vwap_prev`` indicators for a session that starts at the first bar."""
    series = session_vwap_series(list(candles))
    return {"vwap": series[-1], "vwap_prev": series[-2] if len(series) > 1 else None}


def make_context(
    candles: Sequence[Candle],
    *,
    symbol: str = "SPY",
    session_status: SessionStatus = SessionStatus.REGULAR,
    stale: bool = False,
    session_id: str = "2026-10-08",
    indicators: dict[str, object] | None = None,
) -> MarketContext:
    as_of = (candles[-1].end + timedelta(seconds=1)) if candles else OPEN_UTC
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
            candles[-1].end if candles else OPEN_UTC,
            timedelta(seconds=1),
            timedelta(minutes=5),
            stale,
            "DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M1: tuple(candles)},
        indicators=consistent_vwap(candles) if indicators is None else indicators,
    )


# ==============================================================================
# 1. Registration & Inventory Tests
# ==============================================================================


def test_batch_b_strategies_registered_in_production_and_research() -> None:
    source = inspect.getsource(app.build_runtime)

    expected = {
        "vwap_band_reversion": VWAPBandReversionBuy,
        "intraday_consolidation_breakout": IntradayConsolidationBreakoutBuy,
    }

    for strategy_id, expected_cls in expected.items():
        cls = REGISTRY.get(strategy_id)
        assert cls is expected_cls
        assert cls.spec.side is StrategyRole.BUY
        assert cls.spec.category == "technical"
        assert cls.spec.trigger_timeframe is CandleTimeframe.M1
        assert cls.spec.completed_bars_only is True
        assert source.count(f"{cls.__name__},") == 1

    counts = {r: len(REGISTRY.list_strategies(r)) for r in StrategyRole}
    assert counts == {
        StrategyRole.BUY: 85,
        StrategyRole.SELL: 41,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 28,
    }
    assert len(REGISTRY.list_strategies()) == 182


def test_vwap_reclaim_equivalence_with_vwap_bounce() -> None:
    """VWAP Reclaim is equivalent to existing VwapBounceBuy.

    VwapBounceBuy triggers when prior.close <= vwap_prev and latest.close > vwap_latest,
    which is the exact textbook reclaim of VWAP after trading at or below it.
    Confirm no redundant vwap_reclaim class is registered.
    """
    assert "vwap_reclaim" not in {s.spec.strategy_id for s in REGISTRY.list_strategies()}
    bounce_cls = REGISTRY.get("vwap_bounce")
    assert bounce_cls is VwapBounceBuy
    assert bounce_cls.spec.side is StrategyRole.BUY


def test_high_volume_reversal_status_resolved_in_batch_d() -> None:
    """High Volume Reversal was unresolved in Batch B; resolved and implemented in Batch D."""
    assert "high_volume_reversal" in {s.spec.strategy_id for s in REGISTRY.list_strategies()}


# ==============================================================================
# 2. VWAP Deviation Bands Indicator Tests
# ==============================================================================


def test_vwap_bands_calculator_exact_math() -> None:
    calc = VWAPDeviationBandsCalculator(multiplier=D("2.0"))

    # Bar 0: Constant price 100, volume 100
    b0 = make_candle(0, 100, 100, 100, 100, volume=100)
    res0 = calc.update(b0, session_id="S1")
    assert res0 is not None
    assert res0.vwap == D("100")
    assert res0.std_dev == D("0")
    assert res0.lower == D("100")
    assert res0.upper == D("100")

    # Bar 1: Price 106, volume 100
    # TP0 = 100, TP1 = 106
    # CumV = 200, CumVP = 10000 + 10600 = 20600 -> VWAP = 103
    # CumVPSq = 1000000 + (106^2 * 100) = 1000000 + 1123600 = 2123600
    # Variance = 2123600 / 200 - 103^2 = 10618 - 10609 = 9
    # StdDev = sqrt(9) = 3
    # Lower = 103 - 2 * 3 = 97, Upper = 103 + 2 * 3 = 109
    b1 = make_candle(1, 106, 106, 106, 106, volume=100)
    res1 = calc.update(b1, session_id="S1")
    assert res1 is not None
    assert res1.vwap == D("103")
    assert res1.std_dev == D("3")
    assert res1.lower == D("97")
    assert res1.upper == D("109")


def test_vwap_bands_calculator_zero_volume_and_session_reset() -> None:
    calc = VWAPDeviationBandsCalculator(multiplier=D("2.0"))
    b0 = make_candle(0, 100, 100, 100, 100, volume=0)
    assert calc.update(b0, session_id="S1") is None
    assert not calc.ready

    b1 = make_candle(1, 100, 100, 100, 100, volume=50)
    res1 = calc.update(b1, session_id="S1")
    assert res1 is not None
    assert calc.ready

    # Session change resets accumulators
    b2 = make_candle(2, 200, 200, 200, 200, volume=50)
    res2 = calc.update(b2, session_id="S2")
    assert res2 is not None
    assert res2.vwap == D("200")
    assert res2.std_dev == D("0")


def test_vwap_bands_calculator_snapshot_restore() -> None:
    calc = VWAPDeviationBandsCalculator(multiplier=D("2.0"))
    b0 = make_candle(0, 100, 100, 100, 100, volume=100)
    calc.update(b0, session_id="S1")
    b1 = make_candle(1, 106, 106, 106, 106, volume=100)
    calc.update(b1, session_id="S1")

    snap = calc.snapshot()
    restored = VWAPDeviationBandsCalculator.restore(snap)
    assert restored.multiplier == D("2.0")

    # Feed next bar to both and verify identical results
    b2 = make_candle(2, 100, 100, 100, 100, volume=100)
    res_orig = calc.update(b2, session_id="S1")
    res_rest = restored.update(b2, session_id="S1")
    assert res_orig == res_rest


# ==============================================================================
# 3. VWAPBandReversionBuy Strategy Tests
# ==============================================================================


#: Eight baseline bars around 100, a sharp dip below the lower band, then a rebound into it.
BASELINE = [(100, 100), (102, 102), (98, 98), (101, 101), (99, 100), (101, 100), (100, 101), (99, 100)]


def band_session() -> list[Candle]:
    bars = [make_candle(i, o, o + 1, o - 1, c, volume=100) for i, (o, c) in enumerate(BASELINE)]
    bars.append(make_candle(8, 96, 96, 92, 93, volume=100))  # close below the lower band
    bars.append(make_candle(9, 94, 98, 93, 97, volume=100))  # close back above the lower band
    return bars


def test_vwap_band_reversion_signal() -> None:
    strategy = VWAPBandReversionBuy()
    bars = band_session()
    all_bands = session_bands_series(bars, D("2.0"))
    # the bands are genuinely session-cumulative: the eight-bar baseline has a real dispersion
    assert all_bands[-2].std_dev > D("1") and all_bands[-2].lower < all_bands[-2].vwap
    assert bars[-2].close < all_bands[-2].lower
    assert bars[-1].close > all_bands[-1].lower

    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {}, ELIGIBLE_STATE)
    assert eval_res.is_signal is True
    assert eval_res.bar_time == bars[-1].start
    assert eval_res.reference_price == bars[-1].close


def test_vwap_band_reversion_no_signal_when_setup_did_not_penetrate() -> None:
    strategy = VWAPBandReversionBuy()
    # Baseline steady bars where close never penetrates lower band
    bars = [
        make_candle(0, 100, 101, 99, 100, volume=100),
        make_candle(1, 102, 103, 101, 102, volume=100),
        make_candle(2, 98, 99, 97, 98, volume=100),
        make_candle(3, 101, 102, 100, 101, volume=100),
        make_candle(4, 100, 101, 99, 100, volume=100),
        make_candle(5, 101, 102, 100, 101, volume=100),
    ]
    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {}, ELIGIBLE_STATE)
    assert eval_res.is_signal is False
    assert ReasonCode.NO_SIGNAL in eval_res.reason_codes


def test_vwap_band_reversion_guards_and_replay() -> None:
    strategy = VWAPBandReversionBuy()
    bars = band_session()

    # Stale context
    stale_ctx = make_context(bars, stale=True)
    assert ReasonCode.STALE_DATA in strategy.evaluate(stale_ctx, {}, ELIGIBLE_STATE).reason_codes

    # Non-regular session
    closed_ctx = make_context(bars, session_status=SessionStatus.CLOSED)
    assert ReasonCode.NOT_ELIGIBLE in strategy.evaluate(closed_ctx, {}, ELIGIBLE_STATE).reason_codes

    # Not armed (missing or wrong eligibility)
    unarmed_state = {"eligibility": "INELIGIBLE"}
    reg_ctx = make_context(bars)
    assert ReasonCode.NOT_ELIGIBLE in strategy.evaluate(reg_ctx, {}, unarmed_state).reason_codes

    # Replay protection
    fired_state = {**ELIGIBLE_STATE, "last_signal_bar": bars[-1].start.isoformat()}
    assert ReasonCode.NO_SIGNAL in strategy.evaluate(reg_ctx, {}, fired_state).reason_codes


def test_vwap_band_reversion_parameter_validation() -> None:
    strategy = VWAPBandReversionBuy()
    # Out of range multiplier
    with pytest.raises(ParameterValidationError):
        strategy.spec.parameter_schema.validate({"multiplier": D("0.0")})
    with pytest.raises(ParameterValidationError):
        strategy.spec.parameter_schema.validate({"multiplier": D("10.0")})
    # Out of range min_session_bars
    with pytest.raises(ParameterValidationError):
        strategy.spec.parameter_schema.validate({"min_session_bars": 1})
    with pytest.raises(ParameterValidationError):
        strategy.spec.parameter_schema.validate({"min_session_bars": 100})


# ==============================================================================
# 4. IntradayConsolidationBreakoutBuy Strategy Tests
# ==============================================================================


def test_consolidation_breakout_signal() -> None:
    strategy = IntradayConsolidationBreakoutBuy()
    n = 15  # default consolidation_bars

    # Create 15 bars of tight consolidation between 100.00 and 100.40
    # Floor = 100.00, Ceiling = 100.40
    # Range pct = (100.40 - 100.00) / 100.00 * 100 = 0.40% <= 0.75%
    bars = []
    for i in range(n + 1):
        bars.append(
            make_candle(
                i,
                open_price=100.10,
                high=100.40,
                low=100.00,
                close=100.20,
            )
        )

    # 16th bar (breakout bar, index 15): breaks above ceiling 100.40 with close 100.65
    bars.append(
        make_candle(
            n + 1,
            open_price=100.35,
            high=100.70,
            low=100.30,
            close=100.65,
        )
    )

    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {}, ELIGIBLE_STATE)
    assert eval_res.is_signal is True
    assert eval_res.bar_time == bars[-1].start
    assert eval_res.reference_price == bars[-1].close


def test_consolidation_breakout_uncompressed_no_signal() -> None:
    strategy = IntradayConsolidationBreakoutBuy()
    n = 15

    # 15 bars with wide range: Floor = 100.00, Ceiling = 101.50
    # Range pct = 1.50% > 0.75%
    bars = []
    for i in range(n + 1):
        bars.append(
            make_candle(
                i,
                open_price=100.50,
                high=101.50,
                low=100.00,
                close=100.80,
            )
        )

    # Breakout bar closes above 101.50
    bars.append(
        make_candle(
            n + 1,
            open_price=101.40,
            high=102.00,
            low=101.30,
            close=101.80,
        )
    )

    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {}, ELIGIBLE_STATE)
    assert eval_res.is_signal is False
    assert ReasonCode.NO_SIGNAL in eval_res.reason_codes


def test_consolidation_breakout_candle_not_in_range_definition() -> None:
    strategy = IntradayConsolidationBreakoutBuy()
    n = 15

    # 15 bars tightly bounded with ceiling 100.30
    bars = [
        make_candle(i, 100.10, 100.30, 100.00, 100.15)
        for i in range(n + 1)
    ]

    # Breakout bar with high=101.00, close=100.45.
    # Because breakout bar does NOT participate in defining ceiling,
    # ceiling remains 100.30, and close 100.45 > 100.30 triggers signal.
    bars.append(make_candle(n + 1, 100.25, 101.00, 100.20, 100.45))

    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {}, ELIGIBLE_STATE)
    assert eval_res.is_signal is True


def test_consolidation_breakout_guards_and_replay() -> None:
    strategy = IntradayConsolidationBreakoutBuy()
    n = 15
    bars = [
        make_candle(i, 100.10, 100.40, 100.00, 100.20)
        for i in range(n + 1)
    ]
    bars.append(make_candle(n + 1, 100.35, 100.70, 100.30, 100.65))

    # Stale context
    stale_ctx = make_context(bars, stale=True)
    assert ReasonCode.STALE_DATA in strategy.evaluate(stale_ctx, {}, ELIGIBLE_STATE).reason_codes

    # Non-regular session
    closed_ctx = make_context(bars, session_status=SessionStatus.CLOSED)
    assert ReasonCode.NOT_ELIGIBLE in strategy.evaluate(closed_ctx, {}, ELIGIBLE_STATE).reason_codes

    # Insufficient bars (fewer than n + 2)
    short_ctx = make_context(bars[:10])
    assert ReasonCode.INSUFFICIENT_DATA in strategy.evaluate(short_ctx, {}, ELIGIBLE_STATE).reason_codes

    # Replay protection
    fired_state = {**ELIGIBLE_STATE, "last_signal_bar": bars[-1].start.isoformat()}
    reg_ctx = make_context(bars)
    assert ReasonCode.NO_SIGNAL in strategy.evaluate(reg_ctx, {}, fired_state).reason_codes


def test_consolidation_breakout_parameter_validation() -> None:
    strategy = IntradayConsolidationBreakoutBuy()
    with pytest.raises(ParameterValidationError):
        strategy.spec.parameter_schema.validate({"consolidation_bars": 3})
    with pytest.raises(ParameterValidationError):
        strategy.spec.parameter_schema.validate({"consolidation_bars": 100})
    with pytest.raises(ParameterValidationError):
        strategy.spec.parameter_schema.validate({"max_range_pct": D("0.05")})
    with pytest.raises(ParameterValidationError):
        strategy.spec.parameter_schema.validate({"max_range_pct": D("5.0")})
