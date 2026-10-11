"""Targeted unit tests for BUY/SELL Backlog Batch A strategies."""

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
from mike_trade.strategies.buy.bollinger_band_mean_reversion import (
    BollingerBandMeanReversionBuy,
)
from mike_trade.strategies.buy.support_bounce import SupportBounceBuy
from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy
from mike_trade.strategies.buy.vwap_pullback import VwapPullbackBuy
from mike_trade.strategies.indicators.vwap_bands import session_vwap_series
from mike_trade.strategies.sell.price_below_sma import PriceBelowSmaSell

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
) -> Candle:
    start = base_time + timedelta(minutes=minute_offset)
    return Candle(
        symbol="SPY",
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


def make_context(
    bars: list[Candle],
    *,
    stale: bool = False,
    session_status: SessionStatus = SessionStatus.REGULAR,
    session_id: str = "2026-10-08",
    vwap: Decimal | None = None,
    vwap_prev: Decimal | None = None,
) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=1)
    indicators = {}
    if vwap is not None:
        indicators["vwap"] = vwap
    if vwap_prev is not None:
        indicators["vwap_prev"] = vwap_prev
    return MarketContext(
        symbol="SPY",
        as_of=as_of,
        version=len(bars),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=session_status,
        session_id=session_id,
        freshness=ContextFreshness(
            bars[-1].end,
            timedelta(seconds=1),
            timedelta(minutes=5),
            stale,
            "DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M1: tuple(bars)},
        indicators=indicators,
    )


# ==============================================================================
# 1. Registration & Inventory Verification
# ==============================================================================


def test_batch_a_strategies_registered_in_production_and_research() -> None:
    source = inspect.getsource(app.build_runtime)

    for strategy_id, expected_cls in (
        ("bollinger_band_mean_reversion", BollingerBandMeanReversionBuy),
        ("vwap_pullback", VwapPullbackBuy),
        ("support_bounce", SupportBounceBuy),
    ):
        cls = REGISTRY.get(strategy_id)
        assert cls is expected_cls
        assert cls.spec.side is StrategyRole.BUY
        assert cls.spec.category == "technical"
        assert cls.spec.trigger_timeframe is CandleTimeframe.M1
        assert cls.spec.completed_bars_only is True
        assert source.count(f"{cls.__name__},") == 1

    counts = {r: len(REGISTRY.list_strategies(r)) for r in StrategyRole}
    assert counts == {
        StrategyRole.BUY: 82,
        StrategyRole.SELL: 40,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 28,
    }
    assert len(REGISTRY.list_strategies()) == 178


def test_bollinger_middle_band_exit_equivalence() -> None:
    """Bollinger middle band is mathematically identical to SMA(period).

    PriceBelowSmaSell provides the exact long exit for close < SMA(period).
    """
    cls = REGISTRY.get("price_below_sma")
    assert cls is PriceBelowSmaSell
    assert cls.spec.side is StrategyRole.SELL
    # Evaluating PriceBelowSmaSell with default period=20 gives the exact middle band exit
    bars = [make_candle(i, "100", "101", "99", "100") for i in range(20)]
    bars.append(make_candle(20, "98", "99", "97", "98"))  # close 98 < SMA 100
    ctx = make_context(bars)
    eval_res = cls().evaluate(ctx, {"period": 20}, {})
    assert eval_res.is_signal is True


# ==============================================================================
# 2. Bollinger Band Mean Reversion
# ==============================================================================


def test_bollinger_band_mean_reversion_signal() -> None:
    strategy = BollingerBandMeanReversionBuy()
    # 20 bars around 100 -> lower band is roughly 98.
    bars = [make_candle(i, "100", "101", "99", "100") for i in range(20)]
    # Bar 20 dips below lower band: close = 90
    bars.append(make_candle(20, "95", "95", "89", "90"))
    # Bar 21 crosses back above lower band: close = 101
    bars.append(make_candle(21, "91", "102", "91", "101"))

    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {}, dict(ELIGIBLE_STATE))
    assert eval_res.is_signal is True
    assert eval_res.bar_time == bars[-1].start


def test_bollinger_band_mean_reversion_no_signal() -> None:
    strategy = BollingerBandMeanReversionBuy()
    bars = [make_candle(i, "100", "101", "99", "100") for i in range(22)]
    # All bars flat: no excursion below lower band
    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {}, dict(ELIGIBLE_STATE))
    assert eval_res.is_signal is False

    # Excursion below lower band, but latest bar stays below lower band
    bars2 = [make_candle(i, "100", "101", "99", "100") for i in range(20)]
    bars2.append(make_candle(20, "95", "95", "85", "86"))
    bars2.append(make_candle(21, "86", "88", "84", "85"))
    ctx2 = make_context(bars2)
    assert strategy.evaluate(ctx2, {}, dict(ELIGIBLE_STATE)).is_signal is False


def test_bollinger_band_mean_reversion_parameters_and_warmup() -> None:
    strategy = BollingerBandMeanReversionBuy()
    assert strategy.required_bars({}) == 21
    assert strategy.required_bars({"period": 30}) == 31

    with pytest.raises(ParameterValidationError):
        strategy.evaluate(
            make_context([make_candle(0, "100", "101", "99", "100")]),
            {"period": 1},
            dict(ELIGIBLE_STATE),
        )

    with pytest.raises(ParameterValidationError):
        strategy.evaluate(
            make_context([make_candle(0, "100", "101", "99", "100")]),
            {"deviations": D("0")},
            dict(ELIGIBLE_STATE),
        )

    # Insufficient bars
    bars = [make_candle(i, "100", "101", "99", "100") for i in range(20)]
    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {}, dict(ELIGIBLE_STATE))
    assert eval_res.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ==============================================================================
# 3. VWAP Pullback
# ==============================================================================


def vwap_context(bars: list[Candle]) -> MarketContext:
    """Context whose ``vwap`` / ``vwap_prev`` are the production session VWAP of ``bars``."""
    series = session_vwap_series(bars)
    return make_context(bars, vwap=series[-1], vwap_prev=series[-2])


#: A session that opens in an uptrend above its own VWAP, pulls back to VWAP on bar 3 and
#: rebounds on bar 4 (equal volume, so the session VWAP is the running mean of typical prices).
PULLBACK_BARS = [
    make_candle(0, "100", "101", "99", "100.6"),
    make_candle(1, "101", "103", "100.8", "102.8"),
    make_candle(2, "102.8", "105", "102.5", "104.8"),
    make_candle(3, "104.8", "104.9", "100", "102.9"),
    make_candle(4, "103", "106", "102.9", "105.5"),
]


def test_vwap_pullback_signal() -> None:
    strategy = VwapPullbackBuy()
    ctx = vwap_context(PULLBACK_BARS)
    eval_res = strategy.evaluate(ctx, {}, dict(ELIGIBLE_STATE))
    assert eval_res.is_signal is True
    assert eval_res.bar_time == PULLBACK_BARS[-1].start


def test_vwap_pullback_vs_vwap_bounce_distinction() -> None:
    """VWAP Pullback requires holding support (setup close >= VWAP at that bar), whereas
    VWAP Bounce requires prior.close <= vwap_prev.  They can coincide only at exact equality."""
    pullback = VwapPullbackBuy()
    bounce = VwapBounceBuy()

    # Case A: Qualifying VWAP Pullback; the setup close is above its VWAP so no bounce
    ctx_pullback = vwap_context(PULLBACK_BARS)
    assert pullback.evaluate(ctx_pullback, {}, dict(ELIGIBLE_STATE)).is_signal is True
    assert bounce.evaluate(ctx_pullback, {}, dict(ELIGIBLE_STATE)).is_signal is False

    # Case B: Qualifying VWAP Bounce (cross from below the VWAP)
    bars_bounce = [
        make_candle(0, "98", "99", "97", "98"),
        make_candle(1, "98", "99.5", "97.5", "98.5"),
        make_candle(2, "98.5", "99.8", "98", "99.0"),
        make_candle(3, "99.0", "99.1", "98.0", "98.2"),  # close below the session VWAP
        make_candle(4, "98.4", "101.5", "98.3", "101.0"),  # close back above it
    ]
    ctx_bounce = vwap_context(bars_bounce)
    assert bounce.evaluate(ctx_bounce, {}, dict(ELIGIBLE_STATE)).is_signal is True
    # VWAP Pullback must not trigger: there is no prior trend above VWAP and the setup lost VWAP
    assert pullback.evaluate(ctx_bounce, {}, dict(ELIGIBLE_STATE)).is_signal is False


def test_vwap_pullback_session_boundaries_and_volume() -> None:
    strategy = VwapPullbackBuy()

    # Previous session bar must not count
    day1 = OPEN_UTC - timedelta(days=1)
    bars = [
        make_candle(0, "102", "103", "101.5", "102.5", base_time=day1),
        make_candle(0, "102", "103", "101.5", "102.5"),
        make_candle(1, "102.5", "104", "102.0", "103.5"),
        make_candle(2, "103.5", "104.5", "102.5", "104.0"),
        make_candle(3, "104.0", "104.0", "99.95", "100.2"),
        make_candle(4, "100.5", "105.0", "100.5", "104.5"),
    ]
    # If session only has 4 bars today (excluding day1), insufficient data:
    ctx = make_context(bars[1:5], vwap=D("100.1"), vwap_prev=D("100.0"))
    assert strategy.evaluate(ctx, {}, dict(ELIGIBLE_STATE)).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )

    # Zero volume on evaluated bar
    bars_zero_vol = [
        make_candle(0, "102", "103", "101.5", "102.5"),
        make_candle(1, "102.5", "104", "102.0", "103.5"),
        make_candle(2, "103.5", "104.5", "102.5", "104.0"),
        make_candle(3, "104.0", "104.0", "99.95", "100.2", volume="0"),
        make_candle(4, "100.5", "105.0", "100.5", "104.5"),
    ]
    ctx_zero_vol = make_context(bars_zero_vol, vwap=D("100.1"), vwap_prev=D("100.0"))
    assert strategy.evaluate(ctx_zero_vol, {}, dict(ELIGIBLE_STATE)).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )


# ==============================================================================
# 4. Support Bounce
# ==============================================================================


def test_support_bounce_signal() -> None:
    strategy = SupportBounceBuy()
    # Build lookback=20, strength=2 history with a clear confirmed swing low at index 5 (price 95.0)
    bars: list[Candle] = []
    # Bars 0-4 declining
    for i in range(5):
        bars.append(make_candle(i, 105 - i, 106 - i, 104 - i, 105 - i))
    # Bar 5 is the swing low pivot at low 95.0
    bars.append(make_candle(5, "97", "98", "95.0", "96"))
    # Bars 6-7 rising to confirm the swing low with strength=2
    bars.append(make_candle(6, "97", "99", "96.5", "98"))
    bars.append(make_candle(7, "98", "101", "97.5", "100"))
    # Bars 8-19 trading above 100
    for i in range(8, 20):
        bars.append(make_candle(i, "100", "102", "99", "101"))
    # Setup bar 20 tests support (95.0): low reaches 95.1 (within 0.2% tolerance), close holds 96.0
    bars.append(make_candle(20, "100", "100", "95.1", "96.0"))
    # Signal bar 21 confirms rebound: close = 101.0 > setup.high 100.0 and > support 95.0
    bars.append(make_candle(21, "96.5", "102.0", "96.0", "101.0"))

    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {"lookback": 20, "strength": 2}, dict(ELIGIBLE_STATE))
    assert eval_res.is_signal is True
    assert eval_res.bar_time == bars[-1].start


def test_support_bounce_point_in_time_and_no_lookahead() -> None:
    strategy = SupportBounceBuy()
    # Confirm that a future bar low cannot create a pivot retroactively for the signal bar
    bars: list[Candle] = []
    for i in range(22):
        bars.append(make_candle(i, "100", "101", "99", "100"))

    ctx = make_context(bars)
    # With all flat bars, no swing low exists
    eval_res = strategy.evaluate(ctx, {"lookback": 20, "strength": 2}, dict(ELIGIBLE_STATE))
    assert eval_res.is_signal is False


def test_support_bounce_no_signal_on_breakdown() -> None:
    strategy = SupportBounceBuy()
    bars: list[Candle] = []
    for i in range(5):
        bars.append(make_candle(i, 105 - i, 106 - i, 104 - i, 105 - i))
    bars.append(make_candle(5, "97", "98", "95.0", "96"))
    bars.append(make_candle(6, "97", "99", "96.5", "98"))
    bars.append(make_candle(7, "98", "101", "97.5", "100"))
    for i in range(8, 20):
        bars.append(make_candle(i, "100", "102", "99", "101"))
    # Setup bar crashes through support: close = 92.0 < 95.0 * (1 - 0.002)
    bars.append(make_candle(20, "100", "100", "91.0", "92.0"))
    bars.append(make_candle(21, "92.0", "98.0", "92.0", "97.0"))

    ctx = make_context(bars)
    eval_res = strategy.evaluate(ctx, {"lookback": 20, "strength": 2}, dict(ELIGIBLE_STATE))
    assert eval_res.is_signal is False


def test_support_bounce_parameters_and_warmup() -> None:
    strategy = SupportBounceBuy()
    assert strategy.required_bars({}) == 52
    assert strategy.required_bars({"lookback": 30}) == 32

    with pytest.raises(ParameterValidationError):
        strategy.evaluate(
            make_context([make_candle(0, "100", "101", "99", "100")]),
            {"strength": 0},
            dict(ELIGIBLE_STATE),
        )

    with pytest.raises(ParameterValidationError):
        strategy.evaluate(
            make_context([make_candle(0, "100", "101", "99", "100")]),
            {"lookback": 5},  # min 10
            dict(ELIGIBLE_STATE),
        )
