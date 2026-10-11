"""Targeted unit tests for BUY/SELL Backlog Batch D strategies.

Verifies:
- RelativeVolumeMomentumBuy (BUY)
- MovingAverageCompressionBuy (BUY)
- HighVolumeReversalSell (SELL)
- Moving Average Mean Reversion equivalence mapping
- Registration in production and research registries
- Non-circular signal calculations, edge triggers, parameter validation,
  warmup requirements, gaps, same-bar replay suppression, and lifecycle gating.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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
from mike_trade.strategies.buy.moving_average_compression import (
    MovingAverageCompressionBuy,
)
from mike_trade.strategies.buy.relative_volume_momentum import (
    RelativeVolumeMomentumBuy,
)
from mike_trade.strategies.sell.high_volume_reversal import (
    HighVolumeReversalSell,
)

D = Decimal
NY = ZoneInfo("America/New_York")
OPEN_NY = datetime(2026, 10, 8, 9, 30, tzinfo=NY)
OPEN_UTC = OPEN_NY.astimezone(UTC)
ARMED_BUY = {"eligibility": "WATCHING_BUY"}
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


def make_context(
    candles: Sequence[Candle],
    *,
    symbol: str = "SPY",
    session_status: SessionStatus = SessionStatus.REGULAR,
    stale: bool = False,
    session_id: str = "2026-10-08",
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
        indicators={},
    )


# =====================================================================
# 1. Registration & Integrity Tests
# =====================================================================


def test_batch_d_strategies_registered_in_production_and_research() -> None:
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {
        StrategyRole.BUY: 82,
        StrategyRole.SELL: 41,
        StrategyRole.SHORT: 3,
        StrategyRole.COVER: 3,
    }
    assert len(REGISTRY.list_strategies()) == 129

    rvol_cls = REGISTRY.get("relative_volume_momentum")
    assert issubclass(rvol_cls, RelativeVolumeMomentumBuy)
    assert rvol_cls.spec.side is StrategyRole.BUY

    comp_cls = REGISTRY.get("moving_average_compression")
    assert issubclass(comp_cls, MovingAverageCompressionBuy)
    assert comp_cls.spec.side is StrategyRole.BUY

    hvr_cls = REGISTRY.get("high_volume_reversal")
    assert issubclass(hvr_cls, HighVolumeReversalSell)
    assert hvr_cls.spec.side is StrategyRole.SELL

    prod_source = inspect.getsource(app.build_runtime)
    research_source = inspect.getsource(_research_registry)
    for name in (
        "RelativeVolumeMomentumBuy",
        "MovingAverageCompressionBuy",
        "HighVolumeReversalSell",
    ):
        assert prod_source.count(f"{name},") == 1
        assert research_source.count(f"{name},") == 1


def test_moving_average_mean_reversion_equivalence_documented() -> None:
    # Verify the four canonical strategies forming the existing MA mean reversion
    # suite are registered
    for equiv_id in (
        "bollinger_band_mean_reversion",
        "z_score_mean_reversion",
        "keltner_channel_reversion",
        "moving_average_envelope",
    ):
        strat = REGISTRY.get(equiv_id)
        assert strat is not None
        assert strat.spec.side is StrategyRole.BUY


# =====================================================================
# 2. RelativeVolumeMomentumBuy Tests
# =====================================================================


def test_rvol_momentum_trigger_happy_path() -> None:
    # 22 bars needed for default volume_period=20, momentum_bars=3
    # Bars 0..19: volume=100, price=100. Baseline at bar 20 is exactly 100.
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(20)]
    # Bar 20 (prior bar t-1): volume 150 (RVOL = 150/100 = 1.5 < 2.0)
    candles.append(make_candle(20, 100, 100.5, 99.5, 100, volume=150))
    # Baseline for bar 21 (latest bar t): 19 bars of 100 + 1 bar of 150 -> avg = 2050/20 = 102.5
    # For RVOL >= 2.0 at bar 21, volume must be >= 205 (we use 250 -> RVOL = 250/102.5 = 2.439)
    # Price action: open 100, close 101.5 (green candle, higher than prior close 100,
    # return from bar 21-3=18 close of 100 is 1.5% >= 0.1%)
    candles.append(make_candle(21, 100, 102, 99.8, 101.5, volume=250))

    ctx = make_context(candles)
    strat = RelativeVolumeMomentumBuy()
    eval_res = strat.evaluate(ctx, {}, ARMED_BUY)

    assert eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.SIGNAL,)
    assert eval_res.reference_price == D("101.5")
    assert eval_res.bar_time == candles[-1].start


def test_rvol_momentum_no_trigger_insufficient_volume_surge() -> None:
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(20)]
    candles.append(make_candle(20, 100, 100.5, 99.5, 100, volume=150))
    # Bar 21 volume 180 -> RVOL = 180 / 102.5 = 1.756 < 2.0
    candles.append(make_candle(21, 100, 102, 99.8, 101.5, volume=180))

    ctx = make_context(candles)
    strat = RelativeVolumeMomentumBuy()
    eval_res = strat.evaluate(ctx, {}, ARMED_BUY)

    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_rvol_momentum_no_trigger_bearish_candle() -> None:
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(20)]
    candles.append(make_candle(20, 100, 100.5, 99.5, 100, volume=150))
    # Bar 21 has high volume 250, but is red: open 102, close 101
    candles.append(make_candle(21, 102, 102.5, 100.5, 101, volume=250))

    ctx = make_context(candles)
    strat = RelativeVolumeMomentumBuy()
    eval_res = strat.evaluate(ctx, {}, ARMED_BUY)

    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_rvol_momentum_no_trigger_prior_volume_already_surging() -> None:
    # If prior bar already had RVOL >= 2.0, latest is not an edge crossing
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(20)]
    # Bar 20 volume 220 (RVOL = 2.2 >= 2.0)
    candles.append(make_candle(20, 100, 100.5, 99.5, 100, volume=220))
    # Bar 21 volume 300
    candles.append(make_candle(21, 100, 102, 99.8, 101.5, volume=300))

    ctx = make_context(candles)
    strat = RelativeVolumeMomentumBuy()
    eval_res = strat.evaluate(ctx, {}, ARMED_BUY)

    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_rvol_momentum_parameter_validation() -> None:
    strat = RelativeVolumeMomentumBuy()
    # momentum_bars >= volume_period is rejected
    with pytest.raises(ParameterValidationError):
        strat.required_bars({"volume_period": 10, "momentum_bars": 10})
    with pytest.raises(ParameterValidationError):
        strat.required_bars({"volume_period": 10, "momentum_bars": 15})
    # Out of range parameter bounds
    with pytest.raises(ParameterValidationError):
        strat.spec.parameter_schema.validate({"volume_period": 4})
    with pytest.raises(ParameterValidationError):
        strat.spec.parameter_schema.validate({"rvol_threshold": D("1.0")})


def test_rvol_momentum_gaps_and_warmup() -> None:
    strat = RelativeVolumeMomentumBuy()
    # 21 bars (needs 22)
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(21)]
    ctx = make_context(candles)
    assert strat.evaluate(ctx, {}, ARMED_BUY).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)

    # 22 bars but with a gap at minute 10
    candles_gap = [
        make_candle(i if i < 10 else i + 2, 100, 100.5, 99.5, 100, volume=100) for i in range(22)
    ]
    ctx_gap = make_context(candles_gap)
    assert strat.evaluate(ctx_gap, {}, ARMED_BUY).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_rvol_momentum_same_bar_replay_suppression() -> None:
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(20)]
    candles.append(make_candle(20, 100, 100.5, 99.5, 100, volume=150))
    candles.append(make_candle(21, 100, 102, 99.8, 101.5, volume=250))

    ctx = make_context(candles)
    strat = RelativeVolumeMomentumBuy()
    res1 = strat.evaluate(ctx, {}, ARMED_BUY)
    assert res1.is_signal

    # Second evaluation on same bar with updated state
    res2 = strat.evaluate(ctx, {}, {**ARMED_BUY, **res1.state_updates})
    assert not res2.is_signal
    assert res2.reason_codes == (ReasonCode.NO_SIGNAL,)


# =====================================================================
# 3. MovingAverageCompressionBuy Tests
# =====================================================================


def test_ma_compression_trigger_happy_path() -> None:
    # Warmup is slow (50) + compression (5) = 55 bars
    # Bars 0..53: all flat at 100.0.
    # At bars 49..53 (5 compression bars), fast (10), med (20), slow (50) are all 100.0.
    # spread = 0.0% <= 0.75%. Prior bar (53) close is 100 <= max MA (100).
    candles = [make_candle(i, 100, 100.5, 99.5, 100) for i in range(54)]
    # Bar 54 (latest bar t): opens at 100, breaks out to close at 102 (> max MA ~ 100.2)
    candles.append(make_candle(54, 100, 102.5, 99.8, 102))

    ctx = make_context(candles)
    strat = MovingAverageCompressionBuy()
    eval_res = strat.evaluate(ctx, {}, ARMED_BUY)

    assert eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.SIGNAL,)
    assert eval_res.reference_price == D("102")


def test_ma_compression_no_trigger_uncompressed_ribbon() -> None:
    # If the moving averages are spread apart, compression fails
    # Let price trend strongly so fast, med, slow differ by > 0.75%
    # e.g., price climbs steadily from 80 to 120
    candles = [make_candle(i, 80 + i, 81 + i, 79 + i, 80 + i) for i in range(54)]
    candles.append(make_candle(54, 134, 136, 133, 135))

    ctx = make_context(candles)
    strat = MovingAverageCompressionBuy()
    eval_res = strat.evaluate(ctx, {}, ARMED_BUY)

    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_ma_compression_no_trigger_prior_bar_already_above() -> None:
    candles = [make_candle(i, 100, 100.5, 99.5, 100) for i in range(53)]
    # Bar 53 was already broken out above MA cluster
    candles.append(make_candle(53, 100, 102, 99.5, 101.5))
    # Bar 54 stays above
    candles.append(make_candle(54, 101.5, 102.5, 101, 102))

    ctx = make_context(candles)
    strat = MovingAverageCompressionBuy()
    eval_res = strat.evaluate(ctx, {}, ARMED_BUY)

    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_ma_compression_parameter_validation() -> None:
    strat = MovingAverageCompressionBuy()
    with pytest.raises(ParameterValidationError):
        strat.required_bars({"fast_period": 20, "medium_period": 20, "slow_period": 50})
    with pytest.raises(ParameterValidationError):
        strat.required_bars({"fast_period": 30, "medium_period": 20, "slow_period": 50})
    with pytest.raises(ParameterValidationError):
        strat.required_bars({"fast_period": 10, "medium_period": 60, "slow_period": 50})


def test_ma_compression_warmup_and_replay_suppression() -> None:
    strat = MovingAverageCompressionBuy()
    # 54 bars (needs 55)
    candles = [make_candle(i, 100, 100.5, 99.5, 100) for i in range(54)]
    ctx = make_context(candles)
    assert strat.evaluate(ctx, {}, ARMED_BUY).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)

    # 55 bars happy path
    candles.append(make_candle(54, 100, 102.5, 99.8, 102))
    ctx2 = make_context(candles)
    res1 = strat.evaluate(ctx2, {}, ARMED_BUY)
    assert res1.is_signal

    # Second evaluation on same bar suppressed
    res2 = strat.evaluate(ctx2, {}, {**ARMED_BUY, **res1.state_updates})
    assert not res2.is_signal
    assert res2.reason_codes == (ReasonCode.NO_SIGNAL,)


# =====================================================================
# 4. HighVolumeReversalSell Tests
# =====================================================================


def test_high_volume_reversal_trigger_happy_path() -> None:
    # 22 bars needed for volume_period=20, trend_bars=5
    # Bars 0..14: flat at 100, volume 100
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(15)]
    # Bars 15..20: uptrend from 100 to 105, volume 100
    # Bar 15 is bar t - 1 - trend = 21 - 1 - 5 = 15 (close=100)
    # Bar 20 is bar t - 1 = 20 (close=105 > 100, uptrend established!)
    for i in range(15, 21):
        candles.append(
            make_candle(
                i, 100 + (i - 15), 100.5 + (i - 15), 99.5 + (i - 15), 100 + (i - 15), volume=100
            )
        )

    # Bar 21 (latest bar t):
    # Baseline volume over preceding 20 bars (bars 1..20) is 100.
    # Volume expands to 250 (>= 2.0 * 100 = 200).
    # Reversal candle: open 106, high 107, low 103, close 103.5
    # (close 103.5 < open 106, close 103.5 < prior close 105,
    # range = 4.0, close - low = 0.5 <= 0.5 * 4.0 = 2.0 -> bottom quartile rejection!)
    candles.append(make_candle(21, 106, 107, 103, 103.5, volume=250))

    ctx = make_context(candles)
    strat = HighVolumeReversalSell()
    # SELL rules do not require WATCHING_BUY state
    eval_res = strat.evaluate(ctx, {}, {})

    assert eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.SIGNAL,)
    assert eval_res.reference_price == D("103.5")


def test_high_volume_reversal_no_trigger_prior_downtrend() -> None:
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(15)]
    # Bars 15..20 in a downtrend (100 down to 95)
    for i in range(15, 21):
        candles.append(
            make_candle(
                i, 100 - (i - 15), 100.5 - (i - 15), 99.5 - (i - 15), 100 - (i - 15), volume=100
            )
        )
    # Reversal bar
    candles.append(make_candle(21, 96, 97, 93, 93.5, volume=250))

    ctx = make_context(candles)
    strat = HighVolumeReversalSell()
    eval_res = strat.evaluate(ctx, {}, {})

    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_high_volume_reversal_no_trigger_low_volume() -> None:
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(15)]
    for i in range(15, 21):
        candles.append(
            make_candle(
                i, 100 + (i - 15), 100.5 + (i - 15), 99.5 + (i - 15), 100 + (i - 15), volume=100
            )
        )
    # Volume is only 150 (< 200)
    candles.append(make_candle(21, 106, 107, 103, 103.5, volume=150))

    ctx = make_context(candles)
    strat = HighVolumeReversalSell()
    eval_res = strat.evaluate(ctx, {}, {})

    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_high_volume_reversal_no_trigger_upper_range_close() -> None:
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(15)]
    for i in range(15, 21):
        candles.append(
            make_candle(
                i, 100 + (i - 15), 100.5 + (i - 15), 99.5 + (i - 15), 100 + (i - 15), volume=100
            )
        )
    # Bar 21 is red (open 106, close 104.5), but range is high 107, low 100 (range 7).
    # close - low = 4.5 > 0.5 * 7 = 3.5 (closed in upper half)
    candles.append(make_candle(21, 106, 107, 100, 104.5, volume=250))

    ctx = make_context(candles)
    strat = HighVolumeReversalSell()
    eval_res = strat.evaluate(ctx, {}, {})

    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_high_volume_reversal_contract_and_replay_suppression() -> None:
    strat = HighVolumeReversalSell()
    assert strat.spec.side is StrategyRole.SELL
    assert strat.spec.requires_volume

    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(15)]
    for i in range(15, 21):
        candles.append(
            make_candle(
                i, 100 + (i - 15), 100.5 + (i - 15), 99.5 + (i - 15), 100 + (i - 15), volume=100
            )
        )
    candles.append(make_candle(21, 106, 107, 103, 103.5, volume=250))

    ctx = make_context(candles)
    res1 = strat.evaluate(ctx, {}, {})
    assert res1.is_signal

    # Second evaluation on same bar suppressed
    res2 = strat.evaluate(ctx, {}, res1.state_updates)
    assert not res2.is_signal
    assert res2.reason_codes == (ReasonCode.NO_SIGNAL,)


# =====================================================================
# 5. Lifecycle Gating & Edge Cases Tests
# =====================================================================


def test_batch_d_gating_behavior() -> None:
    # 1. Stale context
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(22)]
    ctx_stale = make_context(candles, stale=True)
    assert RelativeVolumeMomentumBuy().evaluate(ctx_stale, {}, ARMED_BUY).reason_codes == (
        ReasonCode.STALE_DATA,
    )
    assert HighVolumeReversalSell().evaluate(ctx_stale, {}, {}).reason_codes == (
        ReasonCode.STALE_DATA,
    )

    # 2. Non-regular session status
    ctx_closed = make_context(candles, session_status=SessionStatus.CLOSED)
    assert RelativeVolumeMomentumBuy().evaluate(ctx_closed, {}, ARMED_BUY).reason_codes == (
        ReasonCode.NOT_ELIGIBLE,
    )
    assert HighVolumeReversalSell().evaluate(ctx_closed, {}, {}).reason_codes == (
        ReasonCode.NOT_ELIGIBLE,
    )

    # 3. Not eligible (BUY strategies without WATCHING_BUY)
    ctx_reg = make_context(candles)
    ctx_reg_55 = make_context([make_candle(i, 100, 100.5, 99.5, 100) for i in range(55)])
    for unarm in ({}, {"eligibility": "WATCHING_SHORT"}, {"eligibility": "COOLDOWN"}):
        assert RelativeVolumeMomentumBuy().evaluate(ctx_reg, {}, unarm).reason_codes == (
            ReasonCode.NOT_ELIGIBLE,
        )
        assert MovingAverageCompressionBuy().evaluate(ctx_reg_55, {}, unarm).reason_codes == (
            ReasonCode.NOT_ELIGIBLE,
        )


def test_rvol_momentum_zero_volume_baseline() -> None:
    # All preceding bars have 0 volume
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=0) for i in range(21)]
    candles.append(make_candle(21, 100, 102, 99.8, 101.5, volume=250))
    ctx = make_context(candles)
    eval_res = RelativeVolumeMomentumBuy().evaluate(ctx, {}, ARMED_BUY)
    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_high_volume_reversal_zero_range_candle() -> None:
    # Reversal bar with high == low (doji with 0 range)
    candles = [make_candle(i, 100, 100.5, 99.5, 100, volume=100) for i in range(15)]
    for i in range(15, 21):
        candles.append(
            make_candle(
                i, 100 + (i - 15), 100.5 + (i - 15), 99.5 + (i - 15), 100 + (i - 15), volume=100
            )
        )
    # Bar 21: high=low=close=open=103 (zero range, cannot close below open)
    candles.append(make_candle(21, 103, 103, 103, 103, volume=250))
    ctx = make_context(candles)
    eval_res = HighVolumeReversalSell().evaluate(ctx, {}, {})
    assert not eval_res.is_signal
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)
