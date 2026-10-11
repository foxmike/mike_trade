"""Focused unit tests for the four final reversal and volume strategies:
- intraday_exhaustion_reversal (BUY)
- volume_dry_up_pullback (BUY)
- volatility_expansion_reversal (SELL)
- volume_climax_exit (SELL)
"""

from __future__ import annotations

import inspect
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
from mike_trade.strategies.closed_bar import (
    ELIGIBILITY_KEY,
    ELIGIBILITY_WATCHING_BUY,
    STATE_LAST_SIGNAL_BAR,
)

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()

NEW_STRATEGIES = (
    "intraday_exhaustion_reversal",
    "volume_dry_up_pullback",
    "volatility_expansion_reversal",
    "volume_climax_exit",
)


def candle(
    index: int,
    close: object,
    *,
    open_: object | None = None,
    high: object | None = None,
    low: object | None = None,
    volume: object = 100,
) -> Candle:
    price = D(str(close))
    start = OPEN + timedelta(minutes=index)
    return Candle(
        "SPY",
        CandleTimeframe.M1,
        start,
        start + timedelta(minutes=1),
        price if open_ is None else D(str(open_)),
        price + D("0.5") if high is None else D(str(high)),
        price - D("0.5") if low is None else D(str(low)),
        price,
        D(str(volume)),
        "sip",
        True,
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
    )


def make_context(
    series: list[Candle],
    *,
    stale: bool = False,
    session: SessionStatus = SessionStatus.REGULAR,
) -> MarketContext:
    latest = series[-1]
    return MarketContext(
        symbol="SPY",
        as_of=latest.end + timedelta(seconds=1),
        version=len(series),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=session,
        session_id="2026-10-08",
        freshness=ContextFreshness(
            latest.end,
            timedelta(seconds=1),
            timedelta(minutes=5),
            stale,
            "DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M1: tuple(series)},
        indicators={},
    )


# ==============================================================================
# 1. Registration and Strategy Spec Tests
# ==============================================================================


def test_research_and_production_registries_hold_192_strategies() -> None:
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {
        StrategyRole.BUY: 93,
        StrategyRole.SELL: 43,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 28,
    }
    assert len(REGISTRY.list_strategies()) == 192

    source = inspect.getsource(app.build_runtime)
    for strategy_id in NEW_STRATEGIES:
        cls = REGISTRY.get(strategy_id)
        assert cls is not None
        assert source.count(f"{cls.__name__},") == 1


def test_new_strategy_spec_contracts() -> None:
    for strategy_id in NEW_STRATEGIES:
        cls = REGISTRY.get(strategy_id)
        spec = cls.spec
        assert spec.strategy_id == strategy_id
        assert spec.version == "1.0.0"
        assert spec.category == "technical"
        assert spec.completed_bars_only is True
        assert spec.trigger_timeframe == CandleTimeframe.M1
        assert STATE_LAST_SIGNAL_BAR in spec.persistent_state_keys

    # Check BUY vs SELL sides
    assert REGISTRY.get("intraday_exhaustion_reversal").spec.side is StrategyRole.BUY
    assert REGISTRY.get("volume_dry_up_pullback").spec.side is StrategyRole.BUY
    assert REGISTRY.get("volatility_expansion_reversal").spec.side is StrategyRole.SELL
    assert REGISTRY.get("volume_climax_exit").spec.side is StrategyRole.SELL


def test_parameter_schema_bounds_and_validation() -> None:
    strat = REGISTRY.get("intraday_exhaustion_reversal")()
    with pytest.raises(ParameterValidationError):
        strat.spec.parameter_schema.validate({"trend_bars": 1})  # min is 2
    with pytest.raises(ParameterValidationError):
        strat.spec.parameter_schema.validate({"range_multiple": D("0.5")})  # min is 1.0

    vdu = REGISTRY.get("volume_dry_up_pullback")()
    with pytest.raises(ParameterValidationError):
        vdu.spec.parameter_schema.validate({"dry_up_factor": D("1.0")})  # max is 0.95

    vex = REGISTRY.get("volatility_expansion_reversal")()
    with pytest.raises(ParameterValidationError):
        vex.spec.parameter_schema.validate({"expansion_multiple": D("0.8")})  # min is 1.0

    vcx = REGISTRY.get("volume_climax_exit")()
    with pytest.raises(ParameterValidationError):
        vcx.spec.parameter_schema.validate({"climax_multiple": D("1.0")})  # min is 1.5


# ==============================================================================
# 2. Intraday Exhaustion Reversal (BUY) Tests
# ==============================================================================


def test_intraday_exhaustion_reversal_positive_trigger() -> None:
    cls = REGISTRY.get("intraday_exhaustion_reversal")
    strat = cls()
    needed = strat.required_bars({})

    # Build sequence:
    # 20 baseline bars: close around 100, volume 100, high 100.5, low 99.5 (spread 1.0)
    # Next trend_bars (4): decline: 99, 98, 97, 96
    # Exhaustion bar: low down to 90 (new low), high 94, open 93, close 93.5 (spread 4.0 >= 1.5 ATR),
    # volume 250 (>= 1.5x baseline vol 100), close off low ((93.5 - 90)/4 = 0.875 >= 0.35)
    # Confirmation bar: open 93.6, high 95.0, low 93.5, close 94.5 (bullish, above midpoint 92)
    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5, volume=100))

    # Prior decline (4 bars)
    bars.append(candle(20, 99, open_=100, high=100, low=98.5, volume=100))
    bars.append(candle(21, 98, open_=99, high=99, low=97.5, volume=100))
    bars.append(candle(22, 97, open_=98, high=98, low=96.5, volume=100))
    bars.append(candle(23, 96, open_=97, high=97, low=95.5, volume=100))

    # Exhaustion bar at index 24
    bars.append(candle(24, 93.5, open_=93.0, high=94.0, low=90.0, volume=250))

    # Confirmation bar at index 25
    bars.append(candle(25, 94.5, open_=93.6, high=95.0, low=93.5, volume=120))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY})
    assert eval_res.reason_codes == (ReasonCode.SIGNAL,)
    assert eval_res.is_signal is True
    assert eval_res.bar_time == bars[-1].start


def test_intraday_exhaustion_reversal_no_trigger_when_no_prior_decline() -> None:
    strat = REGISTRY.get("intraday_exhaustion_reversal")()
    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5, volume=100))
    # Flat / rising instead of decline
    for i in range(4):
        bars.append(candle(20 + i, 101 + i, open_=100 + i, high=102 + i, low=100 + i, volume=100))
    # Exhaustion bar
    bars.append(candle(24, 93.5, open_=93.0, high=94.0, low=90.0, volume=250))
    # Confirmation bar
    bars.append(candle(25, 94.5, open_=93.6, high=95.0, low=93.5, volume=120))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY})
    assert eval_res.is_signal is False
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_intraday_exhaustion_reversal_no_trigger_on_insufficient_volume_or_range() -> None:
    strat = REGISTRY.get("intraday_exhaustion_reversal")()
    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5, volume=100))
    for i, c in enumerate([99, 98, 97, 96]):
        bars.append(candle(20 + i, c, open_=c + 1, high=c + 1, low=c - 0.5, volume=100))
    # Low volume exhaustion bar (vol 100 < 1.5x 100)
    bars.append(candle(24, 93.5, open_=93.0, high=94.0, low=90.0, volume=100))
    bars.append(candle(25, 94.5, open_=93.6, high=95.0, low=93.5, volume=120))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY})
    assert eval_res.is_signal is False


def test_intraday_exhaustion_reversal_gate_and_duplicate_suppression() -> None:
    strat = REGISTRY.get("intraday_exhaustion_reversal")()
    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5, volume=100))
    for i, c in enumerate([99, 98, 97, 96]):
        bars.append(candle(20 + i, c, open_=c + 1, high=c + 1, low=c - 0.5, volume=100))
    bars.append(candle(24, 93.5, open_=93.0, high=94.0, low=90.0, volume=250))
    bars.append(candle(25, 94.5, open_=93.6, high=95.0, low=93.5, volume=120))

    ctx = make_context(bars)

    # 1. Not armed with WATCHING_BUY -> NOT_ELIGIBLE
    res_not_armed = strat.evaluate(ctx, {}, {})
    assert res_not_armed.reason_codes == (ReasonCode.NOT_ELIGIBLE,)

    # 2. Non-regular session -> NOT_ELIGIBLE
    ctx_closed = make_context(bars, session=SessionStatus.CLOSED)
    res_closed = strat.evaluate(ctx_closed, {}, {ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY})
    assert res_closed.reason_codes == (ReasonCode.NOT_ELIGIBLE,)

    # 3. Duplicate signal suppression with last_signal_bar
    res_dupe = strat.evaluate(
        ctx,
        {},
        {
            ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY,
            STATE_LAST_SIGNAL_BAR: bars[-1].start.isoformat(),
        },
    )
    assert res_dupe.is_signal is False
    assert res_dupe.reason_codes == (ReasonCode.NO_SIGNAL,)


# ==============================================================================
# 3. Volume Dry-Up Pullback (BUY) Tests
# ==============================================================================


def test_volume_dry_up_pullback_positive_trigger() -> None:
    strat = REGISTRY.get("volume_dry_up_pullback")()

    # 20 baseline bars: average volume 100, price starting at 100
    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100 + i * 0.1, open_=100 + i * 0.1, volume=100))

    # Trend advance (5 bars): rising from ~102 to 106
    for i in range(5):
        bars.append(candle(20 + i, 102 + i, open_=101.5 + i, volume=100))

    # Corrective pullback (3 bars): price drops from 106 to 104
    # Volume contracts (dry-up): 50, 45, 40 (average 45 <= 0.70 * 100, final bar 40 < 100)
    bars.append(candle(25, 105.0, open_=106.0, high=106.2, low=104.8, volume=50))
    bars.append(candle(26, 104.5, open_=105.0, high=105.2, low=104.2, volume=45))
    bars.append(candle(27, 104.0, open_=104.5, high=104.6, low=103.8, volume=40))

    # Resumption bar: closes at 105.0 (above prior high 104.6), volume expands to 95 (> 40 and >= 85)
    bars.append(candle(28, 105.0, open_=104.1, high=105.2, low=104.0, volume=95))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY})
    assert eval_res.reason_codes == (ReasonCode.SIGNAL,)
    assert eval_res.is_signal is True
    assert eval_res.bar_time == bars[-1].start


def test_volume_dry_up_pullback_no_trigger_when_volume_does_not_dry_up() -> None:
    strat = REGISTRY.get("volume_dry_up_pullback")()

    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100 + i * 0.1, open_=100 + i * 0.1, volume=100))
    for i in range(5):
        bars.append(candle(20 + i, 102 + i, open_=101.5 + i, volume=100))

    # Pullback with heavy volume (150, 140, 130) -> no dry-up
    bars.append(candle(25, 105.0, open_=106.0, high=106.2, low=104.8, volume=150))
    bars.append(candle(26, 104.5, open_=105.0, high=105.2, low=104.2, volume=140))
    bars.append(candle(27, 104.0, open_=104.5, high=104.6, low=103.8, volume=130))

    bars.append(candle(28, 105.0, open_=104.1, high=105.2, low=104.0, volume=160))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY})
    assert eval_res.is_signal is False
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_volume_dry_up_pullback_no_trigger_when_resumption_fails_breakout() -> None:
    strat = REGISTRY.get("volume_dry_up_pullback")()

    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100 + i * 0.1, open_=100 + i * 0.1, volume=100))
    for i in range(5):
        bars.append(candle(20 + i, 102 + i, open_=101.5 + i, volume=100))

    # Pullback with dry-up volume
    bars.append(candle(25, 105.0, open_=106.0, high=106.2, low=104.8, volume=50))
    bars.append(candle(26, 104.5, open_=105.0, high=105.2, low=104.2, volume=45))
    bars.append(candle(27, 104.0, open_=104.5, high=104.6, low=103.8, volume=40))

    # Resumption fails to exceed prior high 104.6 (closes at 104.4)
    bars.append(candle(28, 104.4, open_=104.1, high=104.5, low=104.0, volume=95))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY})
    assert eval_res.is_signal is False


# ==============================================================================
# 4. Volatility Expansion Reversal (SELL) Tests
# ==============================================================================


def test_volatility_expansion_reversal_positive_trigger() -> None:
    strat = REGISTRY.get("volatility_expansion_reversal")()

    # 14 ATR baseline bars: price around 100, high 100.5, low 99.5 (spread 1.0, ATR ~ 1.0)
    bars: list[Candle] = []
    for i in range(14):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5))

    # Trend bars (5): rising from 100 to 105
    for i in range(5):
        bars.append(candle(14 + i, 101 + i, open_=100.5 + i, high=101.5 + i, low=100.5 + i))

    # Reversal bar at index 19:
    # Prior bar (index 18) close was 105.0, high 105.5, low 104.5
    # Latest bar: tests higher high 106.5 (> 105.5), but plummets to low 103.5 and closes at 104.0 (< prior low 104.5)
    # Range is 106.5 - 103.5 = 3.0 (>= 1.5 * baseline ATR ~1.0)
    # Close location: (104.0 - 103.5) / 3.0 = 0.167 <= 0.35 (settled near lows)
    # Bearish candle: close 104.0 < open 106.0
    bars.append(candle(19, 104.0, open_=106.0, high=106.5, low=103.5))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {})
    assert eval_res.reason_codes == (ReasonCode.SIGNAL,)
    assert eval_res.is_signal is True
    assert eval_res.bar_time == bars[-1].start


def test_volatility_expansion_reversal_no_trigger_without_expansion() -> None:
    strat = REGISTRY.get("volatility_expansion_reversal")()

    bars: list[Candle] = []
    for i in range(14):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5))
    for i in range(5):
        bars.append(candle(14 + i, 101 + i, open_=100.5 + i, high=101.5 + i, low=100.5 + i))

    # Small range bar (high 105.6, low 104.4, close 104.4, TR = 1.2 < 1.5 * 1.0)
    bars.append(candle(19, 104.4, open_=105.4, high=105.6, low=104.4))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {})
    assert eval_res.is_signal is False
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_volatility_expansion_reversal_no_trigger_without_higher_high() -> None:
    strat = REGISTRY.get("volatility_expansion_reversal")()

    bars: list[Candle] = []
    for i in range(14):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5))
    for i in range(5):
        bars.append(candle(14 + i, 101 + i, open_=100.5 + i, high=101.5 + i, low=100.5 + i))

    # Wide range bar, but does not test higher (high 105.0 <= prior high 105.5)
    bars.append(candle(19, 103.5, open_=105.0, high=105.0, low=103.0))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {})
    assert eval_res.is_signal is False


# ==============================================================================
# 5. Volume Climax Exit (SELL) Tests
# ==============================================================================


def test_volume_climax_exit_positive_trigger() -> None:
    strat = REGISTRY.get("volume_climax_exit")()

    # 20 baseline bars: average volume 100, ATR ~ 1.0, close around 100
    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5, volume=100))

    # Advance of 5 bars: 101, 102, 103, 104, 105
    for i in range(5):
        bars.append(candle(20 + i, 101 + i, open_=100.5 + i, high=101.5 + i, low=100.5 + i, volume=100))

    # Buying Climax bar at index 25 (t-1):
    # Sets new high: high 109.0 (> prior 15-bar highs ~105.5)
    # Climactic volume: volume 300 (>= 2.5 * 100)
    # Wide spread: high 109.0, low 106.0 (spread 3.0 >= 1.25 * 1.0)
    # Upper rejection shadow: open 106.5, close 107.0 -> upper shadow = 109.0 - 107.0 = 2.0 (>= 0.35 * 3.0 = 1.05)
    bars.append(candle(25, 107.0, open_=106.5, high=109.0, low=106.0, volume=300))

    # Automatic Reaction bar at index 26 (t):
    # Downward candle: open 106.8, close 105.5 (< 107.0 and < midpoint 107.5)
    bars.append(candle(26, 105.5, open_=106.8, high=107.0, low=105.0, volume=150))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {})
    assert eval_res.reason_codes == (ReasonCode.SIGNAL,)
    assert eval_res.is_signal is True
    assert eval_res.bar_time == bars[-1].start


def test_volume_climax_exit_no_trigger_on_normal_volume() -> None:
    strat = REGISTRY.get("volume_climax_exit")()

    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5, volume=100))
    for i in range(5):
        bars.append(candle(20 + i, 101 + i, open_=100.5 + i, high=101.5 + i, low=100.5 + i, volume=100))

    # Climax bar with normal volume 150 (< 2.5 * 100 = 250)
    bars.append(candle(25, 107.0, open_=106.5, high=109.0, low=106.0, volume=150))
    bars.append(candle(26, 105.5, open_=106.8, high=107.0, low=105.0, volume=150))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {})
    assert eval_res.is_signal is False
    assert eval_res.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_volume_climax_exit_no_trigger_without_automatic_reaction() -> None:
    strat = REGISTRY.get("volume_climax_exit")()

    bars: list[Candle] = []
    for i in range(20):
        bars.append(candle(i, 100, open_=100, high=100.5, low=99.5, volume=100))
    for i in range(5):
        bars.append(candle(20 + i, 101 + i, open_=100.5 + i, high=101.5 + i, low=100.5 + i, volume=100))

    # Climax bar with climactic volume
    bars.append(candle(25, 107.0, open_=106.5, high=109.0, low=106.0, volume=300))

    # Following bar continues upward (close 108.5 > open 107.0) -> no reaction
    bars.append(candle(26, 108.5, open_=107.0, high=109.2, low=107.0, volume=150))

    ctx = make_context(bars)
    eval_res = strat.evaluate(ctx, {}, {})
    assert eval_res.is_signal is False


# ==============================================================================
# 6. Session Gap & Contiguity Tests
# ==============================================================================


@pytest.mark.parametrize("strategy_id", NEW_STRATEGIES)
def test_missing_data_or_gap_returns_insufficient_data(strategy_id: str) -> None:
    strat = REGISTRY.get(strategy_id)()
    needed = strat.required_bars({})

    bars = [candle(i, 100 + i * 0.1) for i in range(needed)]
    # Introduce a 5-minute gap before the last bar
    bad_start = bars[-2].end + timedelta(minutes=5)
    bars[-1] = Candle(
        "SPY",
        CandleTimeframe.M1,
        bad_start,
        bad_start + timedelta(minutes=1),
        D("105"),
        D("106"),
        D("104"),
        D("105"),
        D("100"),
        "sip",
        True,
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
    )

    ctx = make_context(bars)
    state = {ELIGIBILITY_KEY: ELIGIBILITY_WATCHING_BUY} if strat.spec.side is StrategyRole.BUY else {}
    res = strat.evaluate(ctx, {}, state)
    assert res.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
