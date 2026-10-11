"""Focused signal, guard, math and continuity checks for BUY strategy batch 3."""

from __future__ import annotations

import asyncio
import inspect
import math
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
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
from mike_trade.market_data.candles import Candle, CandleOrigin, CandleStatus, CandleTimeframe, PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.indicators.core import RSICalculator
from mike_trade.strategies.indicators.extended import (
    ADXCalculator, ConnorsRSICalculator, ElderImpulseCalculator, KAMACalculator,
    KeltnerChannelCalculator, SnapshotATRCalculator, SupertrendCalculator,
)
from mike_trade.strategies.indicators.windows import (
    bandwidth, ease_of_movement, ichimoku_cloud, is_hammer, squeeze_momentum, squeeze_on,
    swing_pivots,
)

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()
NEW = (
    "adaptive_moving_average", "adx_trend_following", "atr_expansion_breakout",
    "bollinger_squeeze", "connors_rsi_mean_reversion", "ease_of_movement",
    "elder_impulse_system", "ema_pullback", "hammer_reversal", "higher_high_higher_low",
    "ichimoku_cloud_breakout", "keltner_channel_reversion", "moving_average_ribbon",
    "resistance_breakout", "supertrend", "volatility_squeeze_breakout",
)
RECURSIVE = ("adaptive_moving_average", "adx_trend_following", "atr_expansion_breakout",
             "connors_rsi_mean_reversion", "elder_impulse_system", "ema_pullback",
             "keltner_channel_reversion", "moving_average_ribbon", "supertrend")


def candle(i: int, o: object, h: object, l: object, c: object, v: int = 100) -> Candle:
    start = OPEN + timedelta(minutes=i)
    return Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1),
                  D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(v), "sip", True,
                  CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)


def px(i: int, close: object, half: str = "0.5") -> Candle:
    c = D(str(close))
    return candle(i, c, c + D(half), c - D(half), c)


def series(closes: Sequence[object], half: str = "0.5") -> list[Candle]:
    return [px(i, c, half) for i, c in enumerate(closes)]


def context(bars: list[Candle], *, stale: bool = False,
            session: SessionStatus = SessionStatus.REGULAR) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=1)
    return MarketContext(
        symbol="SPY", as_of=as_of, version=len(bars), feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=session, session_id="2026-10-08",
        freshness=ContextFreshness(bars[-1].end, timedelta(seconds=1), timedelta(minutes=5),
                                   stale, "DATA_TOO_OLD" if stale else None),
        candles={CandleTimeframe.M1: tuple(bars)}, indicators={},
    )


ELIGIBLE = {"eligibility": "WATCHING_BUY"}


def cases() -> dict[str, tuple[dict[str, object], list[Candle], list[Candle]]]:
    """strategy -> (params, bars whose last bar signals, same-length bars that do not)."""
    mids = [20, 11, 21, 12, 22, 13, 23, 14, 24, 15, 25]
    bad_mids = [20, 11, 21, 12, 22, 13, 23, 14, 24, 13, 25]
    return {
        "adx_trend_following": (
            {"period": 3}, series([10] * 8 + [11]), series([10] * 9)),
        "adaptive_moving_average": (
            {"er_period": 3, "fast_period": 2, "slow_period": 10},
            series([10, 10, 10, 10, 9, 12]), series([10, 10, 10, 10, 9, 9.5])),
        "moving_average_ribbon": (
            {}, series([100] * 60 + [101]), series([100] * 61)),
        "atr_expansion_breakout": (
            {"period": 3, "multiplier": D(2)}, series([10] * 5 + [13]), series([10] * 5 + [11])),
        "supertrend": (
            {"atr_period": 3, "multiplier": D(1)},
            series([20, 19, 18, 17, 16, 15, 14, 15, 17]),
            series([20, 19, 18, 17, 16, 15, 14, 15, 14.5])),
        "ema_pullback": (
            {"fast_period": 3, "slow_period": 6},
            series([10 + i for i in range(12)]) + [candle(12, 21, "21.5", 19, 21)],
            series([10 + i for i in range(13)])),
        "keltner_channel_reversion": (
            {"ema_period": 3, "atr_period": 2, "multiplier": D("0.5")},
            series([10] * 6 + [8, "10.5"]), series([10] * 6 + [8, 7])),
        "connors_rsi_mean_reversion": (
            {"rsi_period": 2, "streak_period": 2, "rank_period": 10, "threshold": D(30)},
            series([round(10 + 0.2 * i, 1) for i in range(16)] + [12.5]),
            series([round(10 + 0.2 * i, 1) for i in range(16)] + [13.2])),
        "elder_impulse_system": (
            {}, series([round(100 + 8 * math.sin(i / 4), 2) for i in range(50)]),
            series([round(100 + 8 * math.sin(i / 4), 2) for i in range(49)] + [90])),
        "bollinger_squeeze": (
            {"period": 5, "deviations": D("1.5"), "lookback": 10, "release_window": 2},
            series([10, 14, 9, 15, 8, 13, 10, 14, 9, 12, 11.1, 11, 11, 11, 11, 11, 11.6], "0.05"),
            series([10, 14, 9, 15, 8, 13, 10, 14, 9, 12, 10, 14, 9, 13, 10, 13, 19], "0.05")),
        "volatility_squeeze_breakout": (
            {"period": 5}, [candle(i, 10, "10.1", "9.9", 10) for i in range(8)]
            + [candle(8, 10, "11.1", "9.9", 11)],
            [candle(i, 10, "10.1", "9.9", 10) for i in range(9)]),
        "ichimoku_cloud_breakout": (
            {}, [candle(i, 100, 101, 99, 100) for i in range(78)] + [candle(78, 100, 106, 100, 105)],
            [candle(i, 100, 101, 99, 100) for i in range(79)]),
        "ease_of_movement": (
            {"period": 3}, [candle(i, m, m + D("0.5"), m - D("0.5"), m)
                            for i, m in enumerate([10, 9, 8, 7, 12])],
            [candle(i, m, m + D("0.5"), m - D("0.5"), m) for i, m in enumerate([10, 9, 8, 7, 6])]),
        "hammer_reversal": (
            {"trend_bars": 2},
            [px(0, 12), px(1, 11), px(2, 10), candle(3, 10, "10.1", 8, "10.05"), px(4, "10.5")],
            [px(0, 12), px(1, 11), px(2, 10), candle(3, 10, "10.1", 8, "10.05"), px(4, "10.05")]),
        "higher_high_higher_low": (
            {"strength": 1, "lookback": 10}, series(mids), series(bad_mids)),
        "resistance_breakout": (
            {"strength": 1, "lookback": 10},
            series([10, 12, 10, 13, 11, 12, "11.5", 12, "12.5", 12, 14]),
            series([10, 12, 10, 13, 11, 12, "11.5", 12, "12.5", 12, 12.8])),
    }


def run(strategy_id: str, bars: list[Candle], params: dict[str, object] | None = None,
        state: dict[str, object] | None = None, **ctx: object):
    cls = REGISTRY.get(strategy_id)
    return cls().evaluate(context(bars, **ctx), params if params is not None else {},
                          state if state is not None else dict(ELIGIBLE))


def test_sixteen_new_buys_registered_in_production_and_research() -> None:
    source = inspect.getsource(app.build_runtime)
    assert set(cases()) == set(NEW) and len(NEW) == 16
    for strategy_id in NEW:
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side is StrategyRole.BUY and cls.spec.category == "technical"
        assert cls.spec.trigger_timeframe is CandleTimeframe.M1
        assert source.count(f"{cls.__name__},") == 1
        assert cls.spec.warmup_bars == cls.required_bars({}), strategy_id
    assert len(REGISTRY.list_strategies(StrategyRole.BUY)) == 69
    assert len(REGISTRY.list_strategies(StrategyRole.SELL)) == 40


@pytest.mark.parametrize("strategy_id", NEW)
def test_signal_non_signal_warmup_guards_and_replay(strategy_id: str) -> None:
    params, bars, quiet = cases()[strategy_id]
    cls = REGISTRY.get(strategy_id)
    need = cls.required_bars(params)
    assert need <= len(bars)
    evaluation = run(strategy_id, bars, params)
    assert evaluation.is_signal and evaluation.bar_time == bars[-1].start
    assert evaluation.reference_price == bars[-1].close
    assert not run(strategy_id, quiet, params).is_signal
    assert run(strategy_id, bars[-(need - 1):], params).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)
    replay = {**ELIGIBLE, **evaluation.state_updates}
    assert not run(strategy_id, bars, params, replay).is_signal
    assert run(strategy_id, bars, params, stale=True).reason_codes == (ReasonCode.STALE_DATA,)
    assert run(strategy_id, bars, params, session=SessionStatus.CLOSED).reason_codes == (
        ReasonCode.NOT_ELIGIBLE,)
    assert run(strategy_id, bars, params, {}).reason_codes == (ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", NEW)
def test_missing_latest_minute_cannot_create_an_edge(strategy_id: str) -> None:
    bars = series([100] * 200)
    bars[-1] = px(201, 100)  # minute 199 is absent
    assert run(strategy_id, bars).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize("strategy_id,invalid", [
    ("adx_trend_following", {"period": 1}), ("adx_trend_following", {"threshold": D(4)}),
    ("adaptive_moving_average", {"fast_period": 30}), ("atr_expansion_breakout", {"period": 1}),
    ("bollinger_squeeze", {"release_window": 0}), ("connors_rsi_mean_reversion", {"rank_period": 9}),
    ("ease_of_movement", {"period": 1}), ("elder_impulse_system", {"fast_period": 26}),
    ("ema_pullback", {"fast_period": 26}), ("hammer_reversal", {"shadow_ratio": D("0.5")}),
    ("higher_high_higher_low", {"strength": 5, "lookback": 20}),
    ("keltner_channel_reversion", {"multiplier": D(0)}),
    ("resistance_breakout", {"strength": 10, "lookback": 10}),
    ("supertrend", {"multiplier": D(0)}), ("volatility_squeeze_breakout", {"period": 4}),
    ("ichimoku_cloud_breakout", {"period": 9}), ("moving_average_ribbon", {"period": 9}),
])
def test_invalid_parameters_are_rejected(strategy_id: str, invalid: dict[str, object]) -> None:
    bars = series([100] * 200)
    with pytest.raises(ParameterValidationError):
        run(strategy_id, bars, invalid)


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_recursive_state_does_not_depend_on_history_window(strategy_id: str) -> None:
    rng = random.Random(strategy_id)
    price, closes = 100.0, []
    for _ in range(320):
        price *= 1 + rng.uniform(-0.01, 0.0105)
        closes.append(round(price, 3))
    bars = [candle(i, c, c * 1.002, c * 0.998, c) for i, c in enumerate(closes)]
    cls = REGISTRY.get(strategy_id)
    params = {"multiplier": D("0.8")} if strategy_id == "atr_expansion_breakout" else {}
    need = cls.required_bars(params)

    def trace(window: int | None) -> list[tuple[int, bool, object]]:
        state: dict[str, object] = dict(ELIGIBLE)
        out = []
        for n in range(need, len(bars) + 1):
            view = bars[:n] if window is None else bars[max(0, n - window):n]
            result = cls().evaluate(context(view), params, state)
            state.update(result.state_updates)
            snapshot = state.get("indicator_state")
            out.append((n, result.is_signal,
                        snapshot["latest"] if isinstance(snapshot, dict) else None))  # type: ignore[index]
        return out

    unbounded = trace(None)
    assert any(signal for _, signal, _ in unbounded)
    assert unbounded == trace(need + 2) == trace(need + 60)


def test_corrupt_or_foreign_indicator_state_is_reseeded() -> None:
    params, bars, _ = cases()["atr_expansion_breakout"]
    state = {**ELIGIBLE, "indicator_state": {"version": 1, "signature": "other", "calc": {}}}
    assert run("atr_expansion_breakout", bars, params, state).is_signal
    state["indicator_state"] = "garbage"
    assert run("atr_expansion_breakout", bars, params, state).is_signal


# ------------------------------------------------------------------ indicator math


def test_adx_matches_independent_wilder_reference() -> None:
    rng = random.Random(5)
    price, hlc = 50.0, []
    for _ in range(60):
        price += rng.uniform(-1, 1.2)
        hlc.append((price + rng.uniform(0.1, 0.8), price - rng.uniform(0.1, 0.8), price))
    hlc = [(D(str(round(h, 4))), D(str(round(l, 4))), D(str(round(c, 4)))) for h, l, c in hlc]
    n = 5
    tr, pdm, mdm = [], [], []
    for (ph, pl, pc), (h, l, c) in zip(hlc, hlc[1:]):
        up, down = h - ph, pl - l
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdm.append(up if up > down and up > 0 else D(0))
        mdm.append(down if down > up and down > 0 else D(0))
    sm = [sum(x[:n]) for x in (tr, pdm, mdm)]
    dx = []
    expected_di = {n: (100 * sm[1] / sm[0], 100 * sm[2] / sm[0])}
    dx.append(100 * abs(sm[1] - sm[2]) / (sm[1] + sm[2]))
    for i in range(n, len(tr)):
        sm = [s - s / n + x[i] for s, x in zip(sm, (tr, pdm, mdm))]
        expected_di[i + 1] = (100 * sm[1] / sm[0], 100 * sm[2] / sm[0])
        dx.append(100 * abs(sm[1] - sm[2]) / (sm[1] + sm[2]))
    adx = [sum(dx[:n]) / n]
    for value in dx[n:]:
        adx.append((adx[-1] * (n - 1) + value) / n)
    calc = ADXCalculator(n)
    for index, (h, l, c) in enumerate(hlc):
        bar = candle(index, c, h, l, c)
        plus, minus, got = calc.update(bar)
        if index >= 2 * n - 1:
            assert abs(got - adx[index - (2 * n - 1)]) < D("1e-20")
        if index >= n:
            assert abs(plus - expected_di[index][0]) < D("1e-20")
        else:
            assert got is None
    restored = ADXCalculator.restore(calc.snapshot())
    nxt = candle(60, 51, 52, 50, 51)
    assert restored.update(nxt) == calc.update(nxt)


def test_kama_matches_direct_formula_and_seed() -> None:
    closes = [D(str(x)) for x in (10, 11, 10.5, 12, 12.5, 12, 13, 14, 13.5, 15)]
    n, fast, slow = 4, 2, 10
    fsc, ssc = D(2) / (fast + 1), D(2) / (slow + 1)
    calc = KAMACalculator(n, fast, slow)
    got = [calc.update(c) for c in closes]
    assert got[: n - 1] == [None] * (n - 1)
    expected = sum(closes[:n]) / n
    assert got[n - 1] == expected
    for i in range(n, len(closes)):
        change = abs(closes[i] - closes[i - n])
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(i - n + 1, i + 1))
        sc = ((change / path) * (fsc - ssc) + ssc) ** 2
        expected = expected + sc * (closes[i] - expected)
        assert abs(got[i] - expected) < D("1e-24")
    clone = KAMACalculator.restore(calc.snapshot())
    assert clone.update(D(16)) == calc.update(D(16))


def test_connors_rsi_matches_component_reference() -> None:
    rng = random.Random(11)
    closes, price = [], D(50)
    for _ in range(40):
        price += D(str(round(rng.uniform(-1, 1), 2)))
        closes.append(price)
    r_period, s_period, rank = 3, 2, 12
    price_rsi, streak_rsi = RSICalculator(r_period), RSICalculator(s_period)
    streak, rocs, expected = 0, [], []
    for i, close in enumerate(closes):
        p = price_rsi.update(close)
        if i == 0:
            expected.append(None)
            continue
        prev = closes[i - 1]
        streak = (max(streak, 0) + 1 if close > prev else min(streak, 0) - 1 if close < prev else 0)
        s = streak_rsi.update(D(streak))
        roc = 100 * (close / prev - 1)
        window = rocs[-rank:]
        pr = None if len(window) < rank else 100 * D(sum(1 for r in window if r < roc)) / rank
        rocs.append(roc)
        expected.append(None if None in (p, s, pr) else (p + s + pr) / 3)
    calc = ConnorsRSICalculator(r_period, s_period, rank)
    got = [calc.update(c) for c in closes]
    assert got == expected and got[rank + 1] is not None and got[rank] is None
    clone = ConnorsRSICalculator.restore(calc.snapshot())
    assert clone.update(D(51)) == calc.update(D(51))


def test_streak_counts_and_flat_close_reset() -> None:
    calc = ConnorsRSICalculator(2, 2, 10)
    for close in (10, 11, 12, 12, 11, 10):
        calc.update(D(close))
    assert calc.snapshot()["streak"] == -2


def test_supertrend_matches_reference_bands_and_flip() -> None:
    bars = [candle(i, c, c + D("0.5"), c - D("0.5"), c) for i, c in enumerate(
        [D(x) for x in (20, 19, 18, 17, 16, 15, 14, 15, 17, 16, 15, 12)])]
    period, mult = 3, D(1)
    atr = SnapshotATRCalculator(period)
    calc = SupertrendCalculator(period, mult)
    upper = lower = prev_close = None
    up = False
    for bar in bars:
        a = atr.update(bar)
        out = calc.update(bar)
        if a is None:
            assert out is None
            prev_close = bar.close
            continue
        mid = (bar.high + bar.low) / 2
        bu, bl = mid + mult * a, mid - mult * a
        if upper is None:
            upper, lower, up = bu, bl, False
        else:
            upper = bu if bu < upper or prev_close > upper else upper
            lower = bl if bl > lower or prev_close < lower else lower
            up = (bar.close > upper) if not up else not (bar.close < lower)
        assert out == ((lower if up else upper), up)
        prev_close = bar.close
    assert up is False  # fell back through the lower band at the end
    assert SupertrendCalculator.restore(calc.snapshot()).update(bars[-1]) == calc.update(bars[-1])


def test_keltner_and_impulse_snapshots_resume_exactly() -> None:
    closes = [D(str(round(100 + 5 * math.sin(i / 3), 3))) for i in range(70)]
    for make, restore, feed in (
        (lambda: KeltnerChannelCalculator(5, 3, D(2)), KeltnerChannelCalculator.restore,
         lambda c, i, x: c.update(candle(i, x, x + 1, x - 1, x))),
        (lambda: ElderImpulseCalculator(), ElderImpulseCalculator.restore,
         lambda c, i, x: c.update(x)),
    ):
        whole, half = make(), make()
        for i, x in enumerate(closes[:40]):
            feed(half, i, x)
        resumed = restore(half.snapshot())
        outs = [feed(whole, i, x) for i, x in enumerate(closes)]
        again = [feed(resumed, i, x) for i, x in enumerate(closes) if i >= 40]
        assert outs[40:] == again and outs[-1] is not None


def test_ichimoku_cloud_uses_only_window_ending_displacement_bars_back() -> None:
    bars = [candle(i, 100 + i, 101 + 2 * i, 99 - i, 100 + i) for i in range(90)]
    span_a, span_b = ichimoku_cloud(bars, 9, 26, 52, 26)
    end = len(bars) - 1 - 26  # index of the bar the plotted values were computed on

    def mid(length: int) -> D:
        part = bars[end - length + 1:end + 1]
        return (max(b.high for b in part) + min(b.low for b in part)) / 2

    assert span_a == (mid(9) + mid(26)) / 2 and span_b == mid(52)
    changed = bars[:-26] + [candle(64 + i, 1, 2, "0.5", 1) for i in range(26)]
    assert ichimoku_cloud(changed, 9, 26, 52, 26) == (span_a, span_b)  # later bars are ignored
    with pytest.raises(ValueError):
        ichimoku_cloud(bars[:77], 9, 26, 52, 26)


def test_ease_of_movement_hand_value_and_edge_cases() -> None:
    bars = [candle(i, m, m + 1, m - 1, m, v) for i, (m, v) in enumerate(
        [(10, 100), (11, 200), (13, 400), (12, 100)])]
    # distance*range*scale/volume: 1*2e8/200 + 2*2e8/400 + (-1)*2e8/100
    expected = (D(1_000_000) + D(1_000_000) - D(2_000_000)) / 3
    assert ease_of_movement(bars, 3) == expected == 0
    flat = [candle(0, 10, 10, 10, 10), candle(1, 11, 11, 11, 11)]
    assert ease_of_movement(flat, 1) == 0  # zero range -> limit 0
    assert ease_of_movement([candle(0, 10, 11, 9, 10), candle(1, 11, 12, 10, 11, 0)], 1) is None


def test_squeeze_helpers_follow_definition() -> None:
    tight = [candle(i, 10, "10.1", "9.9", 10) for i in range(6)]
    assert squeeze_on(tight, 5, D(2), D("1.5"))  # zero standard deviation sits inside the channel
    wide = tight[:5] + [candle(5, 10, 14, 10, 14)]
    assert not squeeze_on(wide, 5, D(2), D("1.5"))
    bars = [candle(i, 10 + i, 10 + i, 10 + i, 10 + i) for i in range(9)]
    assert squeeze_momentum(bars, 5) > 0  # steadily rising source -> positive regression value
    falling = [candle(i, 20 - i, 20 - i, 20 - i, 20 - i) for i in range(9)]
    assert squeeze_momentum(falling, 5) < 0
    assert bandwidth([D(10)] * 5, 5, D(2)) == 0


def test_swing_pivots_need_confirming_bars_and_adjacency() -> None:
    bars = series([10, 12, 10, 13, 11, 12])
    highs, lows = swing_pivots(bars, 1)
    assert [i for i, _ in highs] == [1, 3] and [i for i, _ in lows] == [2, 4]
    assert swing_pivots(bars[:4], 1)[0] == [(1, D("12.5"))]  # bar 3 is not yet confirmed
    gapped = bars[:3] + [px(4, 13)] + bars[4:]
    assert [i for i, _ in swing_pivots(gapped, 1)[0]] == [1]  # missing minute blocks pivot 3


def test_hammer_shape_boundaries() -> None:
    ratio, upper = D(2), D("0.1")
    assert is_hammer(candle(0, 10, "10.1", 8, "10.05"), ratio, upper)
    assert not is_hammer(candle(0, 10, "10.6", "9.5", "10.5"), ratio, upper)  # shadow under 2x body
    assert not is_hammer(candle(0, 10, 11, 8, "10.05"), ratio, upper)  # long upper shadow
    assert not is_hammer(candle(0, 10, 10, 10, 10), ratio, upper)  # no range


def test_atr_snapshot_resumes_to_identical_value() -> None:
    bars = [candle(i, 10 + i % 3, 11 + i % 3, 9, 10 + i % 2) for i in range(30)]
    full, part = SnapshotATRCalculator(5), SnapshotATRCalculator(5)
    for bar in bars[:15]:
        part.update(bar)
    resumed = SnapshotATRCalculator.restore(part.snapshot())
    values = [full.update(bar) for bar in bars]
    assert [resumed.update(bar) for bar in bars[15:]] == values[15:]
    with localcontext() as ctx:
        ctx.prec = 4
        assert SnapshotATRCalculator(5).update(bars[0]) is None


# ------------------------------------------------------------------ integration


def test_representative_buys_run_through_the_backtest_runner(tmp_path: Path) -> None:
    prices = [10] * 12 + [13, 13, 13, 13, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12]
    minute = timedelta(minutes=1)
    provenance = domain_market.FeedProvenance("replay", "IEX", "synthetic", False)
    events = []
    for index, value in enumerate(prices):
        start = OPEN + index * minute
        close = D(value)
        events.append(ReplayEvent.bar(domain_market.Candle(
            "SPY", domain_market.Timeframe.M1, start, start + minute,
            close, close + D("0.5"), close - D("0.5"), close, D(100000), provenance)))
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(events, DataProvenance("synthetic", "IEX", False, "r1"),
                                clock=clock, seed=13)
    simulator = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())

    def intent(evaluation, market_context, request):  # noqa: ANN001, ARG001
        return OrderIntent(
            intent_id=f"i-{evaluation.signal_id}", client_order_id=f"c-{evaluation.signal_id}",
            account_scope_id="backtest:test", cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol, side=OrderSide.BUY, purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=market_context.as_of, quantity=D(10))

    ids = ("atr_expansion_breakout", "adaptive_moving_average", "supertrend")
    request = BacktestRequest(
        symbols=("SPY",), window_start=OPEN, window_end=OPEN + timedelta(hours=1),
        period_role=PeriodRole.RESEARCH, config={}, seed=13, starting_capital=D(100000),
        strategy_ids=ids,
        strategy_params={"atr_expansion_breakout": {"period": 3, "multiplier": D(2)},
                         "adaptive_moving_average": {"er_period": 3, "fast_period": 2, "slow_period": 10},
                         "supertrend": {"atr_period": 3, "multiplier": D(1)}},
        initial_strategy_state={i: dict(ELIGIBLE) for i in ids},
        context_history_bars=50, result_directory=tmp_path)
    result = asyncio.run(BacktestRunner(REGISTRY, feed, clock, simulator,
                                       intent_factory=intent).run(request))
    assert {row["strategy"] for row in result.evaluations} >= set(ids)
    assert any(row["strategy"] == "atr_expansion_breakout" for row in result.signals)
    assert any(fill["side"] == "BUY" for fill in result.fills)
    assert all(row["trigger_bar_end"] <= row["time"] for row in result.signals)
