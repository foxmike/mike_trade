"""Focused signal, guard, math and continuity checks for BUY strategy batch 4."""

from __future__ import annotations

import asyncio
import inspect
import random
import re
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
from mike_trade.market_data.candles import Candle, CandleOrigin, CandleStatus, CandleTimeframe, PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.indicators.core import RSICalculator
from mike_trade.strategies.indicators.cumulative import CumulativeLineCalculator
from mike_trade.strategies.indicators.oscillators import ADXEMACalculator, StochRSICalculator, TSICalculator
from mike_trade.strategies.indicators.patterns import (
    contiguous, first_close_above, line_value, ultimate_oscillator, z_score,
)

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()
NEW = (
    "obv_breakout", "accumulation_distribution_breakout", "volume_price_trend_breakout",
    "tsi_momentum", "stochastic_rsi", "ultimate_oscillator", "holy_grail_pullback",
    "rsi2_trend_pullback", "z_score_mean_reversion", "double_bottom_reversal",
    "inverse_head_and_shoulders", "ascending_triangle_breakout", "trendline_breakout",
    "triangle_breakout", "bull_flag_breakout",
)
RECURSIVE = ("obv_breakout", "accumulation_distribution_breakout", "volume_price_trend_breakout",
             "tsi_momentum", "stochastic_rsi", "holy_grail_pullback", "rsi2_trend_pullback")
ELIGIBLE = {"eligibility": "WATCHING_BUY"}


def candle(i: int, o: object, h: object, l: object, c: object, v: int = 100) -> Candle:
    start = OPEN + timedelta(minutes=i)
    return Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1),
                  D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(v), "sip", True,
                  CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)


def px(i: int, close: object, half: str = "0.05") -> Candle:
    c = D(str(close))
    return candle(i, c, c + D(half), c - D(half), c)


def series(closes: Sequence[object], half: str = "0.05") -> list[Candle]:
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


def run(strategy_id: str, bars: list[Candle], params: dict[str, object] | None = None,
        state: dict[str, object] | None = None, **ctx: object):
    cls = REGISTRY.get(strategy_id)
    return cls().evaluate(context(bars, **ctx), params if params is not None else {},
                          state if state is not None else dict(ELIGIBLE))


def descending(start: int, stop: int) -> list[int]:
    return list(range(start, stop, -1))


def ultimate_closes(final_rally: int) -> list[float]:
    path: list[float] = [100.0] * 28 + [99, 98, 97, 96] + [97.5, 99.0] + [98, 97, 96, 95]
    path += [95 + i for i in range(1, final_rally + 1)]
    return [100.0] * (48 - len(path)) + path


def cases() -> dict[str, tuple[dict[str, object], list[Candle], list[Candle]]]:
    """strategy -> (params, bars whose last bar signals, same-length bars that do not)."""
    flag = {"lookback": 20, "pole_bars": 5, "pole_pct": D(1), "min_flag_bars": 3,
            "max_flag_bars": 8, "max_retrace": D("0.5")}
    flag_path = [100] * 12 + [102.5, 105, 107.5, 110, 109, 108.5, 109.5, 109]
    rising = [10 + 0.1 * i for i in range(25)]
    ramp = [10] * 8 + [10 + 0.5 * i for i in range(1, 13)]
    ihs = descending(118, 100) + [100, 96, 100, 104, 100, 92, 98, 103, 100, 96.5, 99, 102]
    at = descending(117, 100) + [100, 105, 110, 106, 102, 106, 110.05, 107, 104, 108, 109.95, 108, 109]
    tl = descending(120, 109) + [100, 110, 100, 108, 100, 100.5, 101, 101.5, 102]
    tri = descending(113, 99) + [100, 110, 102, 108, 104, 104.5]
    db = descending(108, 100) + [100, 99, 98, 97, 98, 99, 100, 99, 98, 97.1, 98, 99]
    return {
        "obv_breakout": ({"period": 3}, series([10, 9, 9, 10, 11]), series([10, 9, 9, 10, 10])),
        "accumulation_distribution_breakout": (
            {"period": 3},
            [candle(i, 10, 11, 9, 9) for i in range(3)] + [candle(3, 10, 11, 9, 10),
                                                            candle(4, 10, 11, 9, 11, 300)],
            [candle(i, 10, 11, 9, 9) for i in range(3)] + [candle(3, 10, 11, 9, 10),
                                                            candle(4, 10, 11, 9, 11, 100)]),
        "volume_price_trend_breakout": (
            {"period": 3}, series([10, 10, 10, 10, 11]), series([10, 10, 10, 10, 10])),
        "tsi_momentum": ({"long_period": 3, "short_period": 2},
                         series([10, 9, 8, 7, 6, 7, 9]), series([10, 9, 8, 7, 6, 5, 4])),
        "stochastic_rsi": ({"rsi_period": 3, "stoch_period": 3},
                           series([10, 11, 12, 11, 10, 9, 8, 9]), series([10, 11, 12, 11, 10, 9, 8, 7])),
        "ultimate_oscillator": (
            {"strength": 1, "lookback": 20, "oversold": D(45)},
            series(ultimate_closes(4), "0.3"), series(ultimate_closes(3), "0.3")),
        "holy_grail_pullback": (
            {"adx_period": 3, "adx_threshold": D(25), "ema_period": 5},
            series(ramp + [ramp[-1] + 2.5], "2"), series(ramp + [ramp[-1] + 1.0], "2")),
        "rsi2_trend_pullback": (
            {"trend_period": 20, "threshold": D(20)},
            series(rising + [11.9]), series(rising + [12.5])),
        "z_score_mean_reversion": (
            {"period": 5, "entry_z": D("1.5")},
            series([10, 10.1, 10, 10.1, 10, 8]), series([10, 10.1, 10, 10.1, 10, 9.95])),
        "double_bottom_reversal": (
            {"strength": 1, "lookback": 20}, series(db + [100.5]), series(db + [100.0])),
        "inverse_head_and_shoulders": (
            {"strength": 1, "lookback": 30}, series(ihs + [106]), series(ihs + [101])),
        "ascending_triangle_breakout": (
            {"strength": 1, "lookback": 30}, series(at + [110.5]), series(at + [110.0])),
        "trendline_breakout": (
            {"strength": 1, "lookback": 20}, series(tl + [105]), series(tl + [101.9])),
        "triangle_breakout": (
            {"strength": 1, "lookback": 20}, series(tri + [106]),
            series(descending(113, 99) + [100, 110, 102, 108, 101, 104.5, 106])),
        "bull_flag_breakout": (flag, series(flag_path + [110.2]), series(flag_path + [109.3])),
    }


def test_fifteen_new_buys_registered_in_production_and_research() -> None:
    source = inspect.getsource(app.build_runtime)
    assert set(cases()) == set(NEW) and len(NEW) == 15
    for strategy_id in NEW:
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side is StrategyRole.BUY and cls.spec.category == "technical"
        assert cls.spec.trigger_timeframe is CandleTimeframe.M1
        assert len(re.findall(rf"\b{cls.__name__},", source)) == 1
        assert cls.spec.warmup_bars == cls.required_bars({}), strategy_id
    assert len(REGISTRY.list_strategies(StrategyRole.BUY)) == 69
    assert len(REGISTRY.list_strategies(StrategyRole.SELL)) == 32


@pytest.mark.parametrize("strategy_id", NEW)
def test_signal_non_signal_warmup_guards_and_replay(strategy_id: str) -> None:
    params, bars, quiet = cases()[strategy_id]
    need = REGISTRY.get(strategy_id).required_bars(params)
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
    bars = series([100 + (i % 3) for i in range(260)])
    bars[-1] = px(261, 100)  # minute 259 is absent
    assert run(strategy_id, bars).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize("strategy_id,params", [
    ("double_bottom_reversal", {"strength": 1, "lookback": 20}),
    ("inverse_head_and_shoulders", {"strength": 1, "lookback": 30}),
    ("ascending_triangle_breakout", {"strength": 1, "lookback": 30}),
    ("trendline_breakout", {"strength": 1, "lookback": 20}),
    ("triangle_breakout", {"strength": 1, "lookback": 20}),
])
def test_pattern_with_a_gap_inside_it_is_rejected(strategy_id: str, params: dict[str, object]) -> None:
    _, bars, _ = cases()[strategy_id]
    # shift the last three bars by one minute: a hole inside the pattern, none between the final two
    cut = len(bars) - 3
    gapped = bars[:cut] + [candle(i + 1, b.open, b.high, b.low, b.close)
                           for i, b in enumerate(bars[cut:], start=cut)]
    assert not run(strategy_id, gapped, params).is_signal


@pytest.mark.parametrize("strategy_id,invalid", [
    ("obv_breakout", {"period": 1}), ("accumulation_distribution_breakout", {"period": 201}),
    ("volume_price_trend_breakout", {"threshold": 1}), ("tsi_momentum", {"long_period": 1}),
    ("stochastic_rsi", {"oversold": D(0)}), ("ultimate_oscillator", {"oversold": D(60)}),
    ("ultimate_oscillator", {"strength": 10, "lookback": 20}),
    ("holy_grail_pullback", {"adx_threshold": D(5)}), ("rsi2_trend_pullback", {"rsi_period": 1}),
    ("z_score_mean_reversion", {"period": 4}), ("double_bottom_reversal", {"strength": 5, "lookback": 20}),
    ("inverse_head_and_shoulders", {"strength": 5, "lookback": 40}),
    ("ascending_triangle_breakout", {"strength": 5, "lookback": 40}),
    ("trendline_breakout", {"strength": 5, "lookback": 20}),
    ("triangle_breakout", {"strength": 5, "lookback": 20}),
    ("bull_flag_breakout", {"min_flag_bars": 9, "max_flag_bars": 5}),
    ("bull_flag_breakout", {"lookback": 20, "max_flag_bars": 30}),
])
def test_invalid_parameters_are_rejected(strategy_id: str, invalid: dict[str, object]) -> None:
    with pytest.raises(ParameterValidationError):
        run(strategy_id, series([100 + (i % 3) for i in range(300)]), invalid)


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_recursive_state_does_not_depend_on_history_window(strategy_id: str) -> None:
    rng = random.Random(strategy_id)
    price, closes = 100.0, []
    for _ in range(420):
        price *= 1 + rng.uniform(-0.01, 0.0105) + (0.003 if strategy_id == "holy_grail_pullback" else 0)
        closes.append(round(price, 3))
    bars = [candle(i, c, c * 1.002, c * 0.998, c, rng.randint(50, 500)) for i, c in enumerate(closes)]
    cls = REGISTRY.get(strategy_id)
    params: dict[str, object] = {
        "holy_grail_pullback": {"adx_period": 5, "adx_threshold": D(12), "ema_period": 8}, "tsi_momentum": {},
        "rsi2_trend_pullback": {"trend_period": 30, "threshold": D(30)},
        "stochastic_rsi": {"oversold": D("0.3")},
    }.get(strategy_id, {"period": 5} if strategy_id.endswith("breakout") else {})
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
    assert any(signal for _, signal, _ in unbounded), strategy_id
    assert unbounded == trace(need + 2) == trace(need + 60)


# ------------------------------------------------------------------ indicator math


def test_cumulative_lines_match_hand_values_and_resume() -> None:
    bars = [candle(0, 10, 11, 9, 10, 100), candle(1, 10, 12, 10, 11, 200),
            candle(2, 11, 12, 10, 10, 50), candle(3, 10, 11, 10, 10, 70),
            candle(4, 10, 14, 10, 12, 400)]
    expected = {
        "obv": [0, 200, 150, 150, 550],
        # CLV x volume: bars 0-1 sit mid-range (0), bars 2-3 close on their low (-1), bar 4 is mid-range
        "ad": [0, 0, -50, -120, -120],
    }
    for kind, line_values in expected.items():
        calc = CumulativeLineCalculator(kind, 2)
        outputs = [calc.update(b) for b in bars]
        assert outputs[:2] == [None, None]
        for i in (2, 3, 4):
            line, high = outputs[i]  # type: ignore[misc]
            assert line == D(line_values[i]) and high == max(D(v) for v in line_values[i - 2:i])
        clone = CumulativeLineCalculator.restore(calc.snapshot())
        extra = candle(5, 12, 13, 11, 13, 90)
        assert clone.update(extra) == calc.update(extra)
    calc = CumulativeLineCalculator("vpt", 2)
    for bar in bars[:3]:
        out = calc.update(bar)
    # bar 1: 200 * (11-10)/10 = 20; bar 2: 50 * (10-11)/11
    assert out is not None and abs(out[0] - (D(20) - D(50) / 11)) < D("1e-24")
    assert out[1] == D(20)


def _ema(values: list[D], n: int) -> list[D]:
    out = [sum(values[:n]) / n]
    for value in values[n:]:
        out.append(out[-1] + D(2) / (n + 1) * (value - out[-1]))
    return out


def test_tsi_matches_independent_reference() -> None:
    rng = random.Random(4)
    closes = [D(str(round(50 + sum(rng.uniform(-1, 1) for _ in range(i + 1)) * 0.3, 3))) for i in range(60)]
    long_p, short_p = 6, 4
    change = [b - a for a, b in zip(closes, closes[1:])]
    num = _ema(_ema(change, long_p), short_p)
    den = _ema(_ema([abs(c) for c in change], long_p), short_p)
    expected = [100 * n / d for n, d in zip(num, den)]
    calc = TSICalculator(long_p, short_p)
    got = [calc.update(c) for c in closes]
    first = long_p + short_p - 1  # index of the first defined value
    assert got[:first] == [None] * first
    assert all(abs(g - e) < D("1e-20") for g, e in zip(got[first:], expected))
    clone = TSICalculator.restore(calc.snapshot())
    assert clone.update(D(51)) == calc.update(D(51))


def test_stochastic_rsi_matches_rsi_window_formula() -> None:
    rng = random.Random(9)
    closes = [D(str(round(20 + rng.uniform(-3, 3), 2))) for _ in range(50)]
    rsi_calc, calc = RSICalculator(5), StochRSICalculator(5, 4)
    rsis = [rsi_calc.update(c) for c in closes]
    got = [calc.update(c) for c in closes]
    for i, value in enumerate(got):
        window = [r for r in rsis[max(0, i - 3):i + 1]]
        if len(window) < 4 or None in window:
            assert value is None
        else:
            low, high = min(window), max(window)  # type: ignore[type-var]
            assert value == (None if high == low else (window[-1] - low) / (high - low))
    assert all(v is None or D(0) <= v <= D(1) for v in got)
    clone = StochRSICalculator.restore(calc.snapshot())
    assert clone.update(D(21)) == calc.update(D(21))


def test_ultimate_oscillator_matches_hand_formula() -> None:
    bars = [px(i, 100 + (i * 7) % 5, "0.4") for i in range(40)]
    values = ultimate_oscillator(bars)
    assert values[:28] == [None] * 28

    def average(end: int, n: int) -> D:
        bp = tr = D(0)
        for j in range(end - n + 1, end + 1):
            low, high = min(bars[j].low, bars[j - 1].close), max(bars[j].high, bars[j - 1].close)
            bp += bars[j].close - low
            tr += high - low
        return bp / tr

    expected = 100 * (4 * average(35, 7) + 2 * average(35, 14) + average(35, 28)) / 7
    assert abs(values[35] - expected) < D("1e-20")  # type: ignore[operator]
    assert ultimate_oscillator([px(i, 100, "0") for i in range(40)]) == [None] * 40  # zero true range


def test_adx_ema_pair_resumes_exactly() -> None:
    rng = random.Random(2)
    price, bars = 50.0, []
    for i in range(80):
        price += rng.uniform(-0.8, 1.0)
        bars.append(candle(i, price, price + 0.5, price - 0.5, price))
    whole, half = ADXEMACalculator(4, 6), ADXEMACalculator(4, 6)
    for bar in bars[:40]:
        half.update(bar)
    resumed = ADXEMACalculator.restore(half.snapshot())
    outs = [whole.update(b) for b in bars]
    assert [resumed.update(b) for b in bars[40:]] == outs[40:] and outs[-1] is not None


def test_geometry_helpers() -> None:
    assert z_score([D(1), D(2), D(3)]) == D(1) / (D(2) / 3).sqrt()
    assert z_score([D(5), D(5)]) is None
    assert line_value(2, D(10), 4, D(8), 8) == D(4) and line_value(2, D(7), 9, D(7), 5) == D(7)
    bars = series([10, 11, 12, 13])
    assert first_close_above(bars, lambda x: D(12), 1) is True
    assert first_close_above(bars, lambda x: D(11), 1) is False  # bar 2 already closed above
    assert contiguous(bars) and not contiguous([bars[0], bars[2]])


def test_double_bottom_boundaries() -> None:
    params, bars, _ = cases()["double_bottom_reversal"]
    assert run("double_bottom_reversal", bars, params).is_signal
    tight = dict(params, tolerance_pct=D("0.05"))
    assert not run("double_bottom_reversal", bars, tight).is_signal  # lows 0.1 apart exceed 0.05%
    shallow = dict(params, min_rise_pct=D("5"))
    assert not run("double_bottom_reversal", bars, shallow).is_signal  # peak only ~3% above the lows


def test_ihs_requires_head_lowest_and_neckline_slope_is_used() -> None:
    params, bars, _ = cases()["inverse_head_and_shoulders"]
    assert run("inverse_head_and_shoulders", bars, params).is_signal
    deep = dict(params, min_head_depth_pct=D(10))
    assert not run("inverse_head_and_shoulders", bars, deep).is_signal
    # the neckline falls 0.25 per bar: 101.8 at the signal bar, so 102.5 breaks it and 101.5 does not
    for close, expected in ((102.5, True), (101.5, False)):
        variant = bars[:-1] + [px(len(bars) - 1, close)]
        assert run("inverse_head_and_shoulders", variant, params).is_signal is expected


# ------------------------------------------------------------------ integration


def test_representative_buys_run_through_the_backtest_runner(tmp_path: Path) -> None:
    prices = [10] * 12 + [10.1, 10.2, 10.4, 10.7, 11.1, 11.6, 12.2, 12.4, 12.5, 12.3, 12.0, 12.2, 12.9,
                         13.0, 13.1, 13.0, 12.8, 13.2, 13.6, 14.0, 14.5, 14.2, 14.6, 15.0, 15.4]
    minute = timedelta(minutes=1)
    provenance = domain_market.FeedProvenance("replay", "IEX", "synthetic", False)
    events = []
    for index, value in enumerate(prices):
        start = OPEN + index * minute
        close = D(str(value))
        events.append(ReplayEvent.bar(domain_market.Candle(
            "SPY", domain_market.Timeframe.M1, start, start + minute,
            close, close + D("0.2"), close - D("0.2"), close, D(100000 + 1000 * index), provenance)))
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

    ids = ("obv_breakout", "tsi_momentum", "z_score_mean_reversion")
    request = BacktestRequest(
        symbols=("SPY",), window_start=OPEN, window_end=OPEN + timedelta(hours=1),
        period_role=PeriodRole.RESEARCH, config={}, seed=13, starting_capital=D(100000),
        strategy_ids=ids,
        strategy_params={"obv_breakout": {"period": 5}, "tsi_momentum": {"long_period": 3, "short_period": 2},
                         "z_score_mean_reversion": {"period": 5, "entry_z": D("0.8")}},
        initial_strategy_state={i: dict(ELIGIBLE) for i in ids},
        context_history_bars=50, result_directory=tmp_path)
    result = asyncio.run(BacktestRunner(REGISTRY, feed, clock, simulator,
                                       intent_factory=intent).run(request))
    assert {row["strategy"] for row in result.evaluations} >= set(ids)
    assert any(row["strategy"] in ("obv_breakout", "tsi_momentum") for row in result.signals)
    assert any(fill["side"] == "BUY" for fill in result.fills)
    assert all(row["trigger_bar_end"] <= row["time"] for row in result.signals)
