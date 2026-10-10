"""Focused tests for the first technical strategy batch (14 BUY + 14 SELL).

The core check evaluates every strategy on every prefix of a deterministic 1-minute path
and compares the bars that fire with an *independent* exact-arithmetic oracle (``Fraction``
re-implementations of the published rules, written separately from ``indicators.core``).
Because each evaluation sees only the candles up to that bar, the scan also proves no
future candle is used.  Further tests cover metadata, registration, roles, guards, history
boundaries, the overnight rule, replay suppression, undefined indicators, parameter
validation and the real ``BacktestRunner`` contract.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain import market as domain_market
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
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    STRATEGY_CATEGORIES,
    TECHNICAL_SUB_CATEGORIES,
    BuyStrategy,
    ParameterValidationError,
    ReasonCode,
    SellStrategy,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.strategies.sell.base import SellRule

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 8)
SYMBOL = "SPY"
WATCHING = {"eligibility": "WATCHING_BUY"}

# --------------------------------------------------------------------------- catalogue

#: id -> (side, sub_category, trigger kind, default lookback in completed bars, scan params)
CATALOGUE: dict[str, tuple[str, str, TriggerKind, int, dict[str, object]]] = {
    "sma_crossover": ("BUY", "moving_average", TriggerKind.EDGE_ON_CLOSED_BAR, 201,
                      {"fast_period": 5, "slow_period": 15}),
    "ema_crossover": ("BUY", "moving_average", TriggerKind.EDGE_ON_CLOSED_BAR, 51,
                      {"fast_period": 5, "slow_period": 15}),
    "macd_signal_crossover": ("BUY", "momentum", TriggerKind.EDGE_ON_CLOSED_BAR, 35,
                              {"fast_period": 5, "slow_period": 12, "signal_period": 4}),
    "macd_zero_line_crossover": ("BUY", "momentum", TriggerKind.EDGE_ON_CLOSED_BAR, 27,
                                 {"fast_period": 5, "slow_period": 12}),
    "rsi_centerline_crossover": ("BUY", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 16,
                                 {"rsi_period": 7}),
    "rsi_oversold_reversal": ("BUY", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 16,
                              {"rsi_period": 7, "oversold": Decimal("35")}),
    "donchian_channel_breakout": ("BUY", "breakout", TriggerKind.EDGE_ON_CLOSED_BAR, 22,
                                  {"period": 10}),
    "stochastic_crossover": ("BUY", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 19,
                             {"k_period": 7, "k_smooth": 3, "d_period": 3}),
    "williams_r_reversion": ("BUY", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 15,
                             {"period": 7}),
    "cci_zero_line_crossover": ("BUY", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 21,
                                {"period": 10}),
    "parabolic_sar": ("BUY", "trend_following", TriggerKind.EDGE_ON_CLOSED_BAR, 3, {}),
    "dmi_crossover": ("BUY", "trend_following", TriggerKind.EDGE_ON_CLOSED_BAR, 16,
                      {"period": 7}),
    "aroon_trend": ("BUY", "trend_following", TriggerKind.EDGE_ON_CLOSED_BAR, 27,
                    {"period": 10}),
    "inside_bar_breakout": ("BUY", "price_action", TriggerKind.EDGE_ON_CLOSED_BAR, 3, {}),
    "macd_bearish_crossover": ("SELL", "momentum", TriggerKind.EDGE_ON_CLOSED_BAR, 35,
                               {"fast_period": 5, "slow_period": 12, "signal_period": 4}),
    "macd_zero_line_breakdown": ("SELL", "momentum", TriggerKind.EDGE_ON_CLOSED_BAR, 27,
                                 {"fast_period": 5, "slow_period": 12}),
    "ema_bearish_crossover": ("SELL", "moving_average", TriggerKind.EDGE_ON_CLOSED_BAR, 51,
                              {"fast_period": 5, "slow_period": 15}),
    "sma_bearish_crossover": ("SELL", "moving_average", TriggerKind.EDGE_ON_CLOSED_BAR, 201,
                              {"fast_period": 5, "slow_period": 15}),
    "price_below_ema": ("SELL", "moving_average", TriggerKind.LEVEL_ON_CLOSED_BAR, 20,
                        {"period": 10}),
    "price_below_sma": ("SELL", "moving_average", TriggerKind.LEVEL_ON_CLOSED_BAR, 50,
                        {"period": 10}),
    "rsi_centerline_breakdown": ("SELL", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 16,
                                 {"rsi_period": 7}),
    "stochastic_bearish_crossover": ("SELL", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 19,
                                     {"k_period": 7, "k_smooth": 3, "d_period": 3}),
    "williams_r_reversal": ("SELL", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 15,
                            {"period": 7}),
    "cci_zero_line_breakdown": ("SELL", "oscillator", TriggerKind.EDGE_ON_CLOSED_BAR, 21,
                                {"period": 10}),
    "parabolic_sar_reversal": ("SELL", "trend_following", TriggerKind.EDGE_ON_CLOSED_BAR, 3, {}),
    "dmi_bearish_crossover": ("SELL", "trend_following", TriggerKind.EDGE_ON_CLOSED_BAR, 16,
                              {"period": 7}),
    "aroon_bearish_reversal": ("SELL", "trend_following", TriggerKind.EDGE_ON_CLOSED_BAR, 27,
                               {"period": 10}),
    "donchian_lower_channel_exit": ("SELL", "breakout", TriggerKind.LEVEL_ON_CLOSED_BAR, 21,
                                    {"period": 10}),
}
NEW_BUY = sorted(k for k, v in CATALOGUE.items() if v[0] == "BUY")
NEW_SELL = sorted(k for k, v in CATALOGUE.items() if v[0] == "SELL")

EXISTING = {
    "vwap_bounce": ("BUY", "vwap"),
    "momentum_breakout": ("BUY", "momentum"),
    "opening_range_breakout": ("BUY", "breakout"),
    "vwap_breakdown": ("SELL", "vwap"),
    "momentum_reversal": ("SELL", "momentum"),
    "rsi_reversal": ("SELL", "oscillator"),
}

REGISTRY = _research_registry()


def strategy_class(strategy_id: str) -> type:
    return REGISTRY.get(strategy_id)


# --------------------------------------------------------------------------- data builders


def open_utc(day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).astimezone(UTC)


def dec(value: float) -> Decimal:
    return Decimal(f"{value:.2f}")


def path(count: int) -> list[tuple[Decimal, Decimal, Decimal]]:
    """Deterministic (high, low, close) rows: two sines plus drift, jittered ranges."""
    rows = []
    for i in range(count):
        close = 100 + 5 * math.sin(i / 4) + 3 * math.sin(i / 11) + 0.02 * i
        up = 0.2 + 0.8 * ((i * 7919) % 13) / 13
        down = 0.2 + 0.8 * ((i * 104729) % 11) / 11
        rows.append((dec(close + up), dec(close - down), dec(close)))
    return rows


def candle(index: int, high: Decimal, low: Decimal, close: Decimal, *, day: date = DAY,
           offset: int | None = None) -> Candle:
    start = open_utc(day) + timedelta(minutes=index if offset is None else offset)
    return Candle(
        symbol=SYMBOL, timeframe=CandleTimeframe.M1, start=start,
        end=start + timedelta(minutes=1), open=close, high=high, low=low, close=close,
        volume=Decimal("1000"), feed_id="sip", consolidated=True, origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW, status=CandleStatus.CLOSED,
    )


def candles(rows: list[tuple[Decimal, Decimal, Decimal]]) -> list[Candle]:
    return [candle(i, h, low, c) for i, (h, low, c) in enumerate(rows)]


def context(bars: list[Candle], *, stale: bool = False,
            status: SessionStatus = SessionStatus.REGULAR) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=5)
    return MarketContext(
        symbol=SYMBOL, as_of=as_of, version=1, feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=status,
        session_id=bars[-1].start.astimezone(NY).date().isoformat(),
        freshness=ContextFreshness(
            newest_data_ts=bars[-1].end, age=as_of - bars[-1].end,
            max_age=timedelta(minutes=5), stale=stale,
            reason="DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M1: tuple(bars)}, indicators={},
    )


ROWS = path(160)
BARS = candles(ROWS)
PREFIX = [context(BARS[: k + 1]) for k in range(len(BARS))]


def state_for(strategy_id: str) -> dict[str, object]:
    return dict(WATCHING) if CATALOGUE[strategy_id][0] == "BUY" else {}


def evaluate(strategy_id: str, ctx: MarketContext, params: Mapping[str, object] | None = None,
             state: Mapping[str, object] | None = None):
    cls = strategy_class(strategy_id)
    chosen = CATALOGUE[strategy_id][4] if params is None else params
    return cls().evaluate(ctx, chosen, state_for(strategy_id) if state is None else state)


def scan(strategy_id: str) -> list[int]:
    return [i for i, ctx in enumerate(PREFIX) if evaluate(strategy_id, ctx).is_signal]


# --------------------------------------------------------------------------- oracle (Fractions)

H = [Fraction(h) for h, _, _ in ROWS]
L = [Fraction(low) for _, low, _ in ROWS]
C = [Fraction(c) for _, _, c in ROWS]
N = len(ROWS)
Series = list[Any]  # Fraction | None per element


def sma_ref(values: list[Fraction], n: int) -> Series:
    return [None if i < n - 1 else sum(values[i - n + 1 : i + 1]) / n for i in range(len(values))]


def ema_ref(values: list[Fraction], n: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) >= n:
        k = Fraction(2, n + 1)
        e = sum(values[:n]) / n
        out[n - 1] = e
        for i in range(n, len(values)):
            e = k * values[i] + (1 - k) * e
            out[i] = e
    return out


def macd_ref(n_fast: int, n_slow: int, n_signal: int | None) -> tuple[Series, Series]:
    fast, slow = ema_ref(C, n_fast), ema_ref(C, n_slow)
    line: Series = [None if a is None or b is None else a - b for a, b in zip(fast, slow)]
    signal: Series = [None] * N
    if n_signal:
        first = next(i for i, v in enumerate(line) if v is not None)
        tail = ema_ref([v for v in line[first:] if v is not None], n_signal)
        signal = [None] * first + tail
    return line, signal


def rsi_ref(n: int) -> Series:
    out: Series = [None] * N
    gains = [max(C[i] - C[i - 1], Fraction(0)) for i in range(1, N)]
    losses = [max(C[i - 1] - C[i], Fraction(0)) for i in range(1, N)]

    def value(g: Fraction, loss: Fraction) -> Fraction:
        if loss == 0:
            return Fraction(100) if g > 0 else Fraction(50)
        return 100 - 100 / (1 + g / loss)

    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = value(ag, al)
    for i in range(n + 1, N):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = value(ag, al)
    return out


def stoch_ref(kp: int, ks: int, dp: int) -> tuple[Series, Series]:
    raw: Series = []
    for i in range(N):
        if i < kp - 1:
            raw.append(None)
            continue
        hh, ll = max(H[i - kp + 1 : i + 1]), min(L[i - kp + 1 : i + 1])
        raw.append(None if hh == ll else 100 * (C[i] - ll) / (hh - ll))

    def smooth(values: Series, n: int) -> Series:
        out: Series = []
        for i in range(N):
            window = values[i - n + 1 : i + 1] if i >= n - 1 else [None]
            out.append(None if any(v is None for v in window) else sum(window) / n)
        return out

    k = smooth(raw, ks)
    return k, smooth(k, dp)


def wr_ref(n: int) -> Series:
    out: Series = []
    for i in range(N):
        if i < n - 1:
            out.append(None)
            continue
        hh, ll = max(H[i - n + 1 : i + 1]), min(L[i - n + 1 : i + 1])
        out.append(None if hh == ll else -100 * (hh - C[i]) / (hh - ll))
    return out


def cci_ref(n: int) -> Series:
    tp = [(H[i] + L[i] + C[i]) / 3 for i in range(N)]
    out: Series = []
    for i in range(N):
        if i < n - 1:
            out.append(None)
            continue
        window = tp[i - n + 1 : i + 1]
        mean = sum(window) / n
        deviation = sum(abs(v - mean) for v in window) / n
        out.append(None if deviation == 0 else (tp[i] - mean) / (Fraction(15, 1000) * deviation))
    return out


def donchian_ref(n: int) -> tuple[Series, Series]:
    upper = [None if i < n else max(H[i - n : i]) for i in range(N)]
    lower = [None if i < n else min(L[i - n : i]) for i in range(N)]
    return upper, lower


def aroon_ref(n: int) -> tuple[Series, Series]:
    up: Series = [None] * N
    down: Series = [None] * N
    for i in range(n, N):
        highs, lows = H[i - n : i + 1], L[i - n : i + 1]
        last_high = max(j for j, v in enumerate(highs) if v == max(highs))
        last_low = max(j for j, v in enumerate(lows) if v == min(lows))
        up[i] = Fraction(100) * last_high / n
        down[i] = Fraction(100) * last_low / n
    return up, down


def dmi_ref(n: int) -> tuple[Series, Series]:
    tr, pdm, mdm = [], [], []
    for i in range(1, N):
        tr.append(max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])))
        up, down = H[i] - H[i - 1], L[i - 1] - L[i]
        pdm.append(up if up > down and up > 0 else Fraction(0))
        mdm.append(down if down > up and down > 0 else Fraction(0))
    plus: Series = [None] * N
    minus: Series = [None] * N
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    plus[n], minus[n] = 100 * s_p / s_tr, 100 * s_m / s_tr
    for i in range(n + 1, N):
        s_tr = s_tr - s_tr / n + tr[i - 1]
        s_p = s_p - s_p / n + pdm[i - 1]
        s_m = s_m - s_m / n + mdm[i - 1]
        plus[i], minus[i] = 100 * s_p / s_tr, 100 * s_m / s_tr
    return plus, minus


def psar_ref() -> list[bool | None]:
    step, ceiling = Fraction(1, 50), Fraction(1, 5)
    trend: list[bool | None] = [None] * N
    up = C[1] >= C[0]
    sar, ep, af = (L[0], H[0], step) if up else (H[0], L[0], step)
    for i in range(1, N):
        nxt = sar + af * (ep - sar)
        prior = [j for j in (i - 1, i - 2) if j >= 0]
        nxt = min([nxt] + [L[j] for j in prior]) if up else max([nxt] + [H[j] for j in prior])
        if (up and L[i] < nxt) or (not up and H[i] > nxt):
            nxt, up = ep, not up
            ep, af = (H[i], step) if up else (L[i], step)
        elif up and H[i] > ep:
            ep, af = H[i], min(af + step, ceiling)
        elif not up and L[i] < ep:
            ep, af = L[i], min(af + step, ceiling)
        sar = nxt
        trend[i] = up
    return trend


def _defined(a: Series, b: Series, i: int) -> bool:
    return None not in (a[i - 1], b[i - 1], a[i], b[i])


def up_cross(a: Series, b: Series) -> list[int]:
    return [i for i in range(1, N) if _defined(a, b, i) and a[i - 1] <= b[i - 1] and a[i] > b[i]]


def down_cross(a: Series, b: Series) -> list[int]:
    return [i for i in range(1, N) if _defined(a, b, i) and a[i - 1] >= b[i - 1] and a[i] < b[i]]


def cross(bullish: bool, a: Series, b: Series) -> list[int]:
    return up_cross(a, b) if bullish else down_cross(a, b)


def level(value: Any) -> Series:
    return [Fraction(value)] * N


def below(ma: Series) -> list[int]:
    return [i for i in range(N) if ma[i] is not None and C[i] < ma[i]]


def expected_signals(strategy_id: str) -> list[int]:
    """Bars at which the published rule fires, from the independent oracle."""
    p: dict[str, Any] = CATALOGUE[strategy_id][4]
    bullish = CATALOGUE[strategy_id][0] == "BUY"
    close: Series = list(C)
    if strategy_id.startswith("sma_"):
        return cross(bullish, sma_ref(C, p["fast_period"]), sma_ref(C, p["slow_period"]))
    if strategy_id in ("ema_crossover", "ema_bearish_crossover"):
        return cross(bullish, ema_ref(C, p["fast_period"]), ema_ref(C, p["slow_period"]))
    if strategy_id in ("macd_signal_crossover", "macd_bearish_crossover"):
        line, sig = macd_ref(p["fast_period"], p["slow_period"], p["signal_period"])
        return cross(bullish, line, sig)
    if strategy_id in ("macd_zero_line_crossover", "macd_zero_line_breakdown"):
        line, _ = macd_ref(p["fast_period"], p["slow_period"], None)
        return cross(bullish, line, level(0))
    if strategy_id in ("rsi_centerline_crossover", "rsi_centerline_breakdown"):
        return cross(bullish, rsi_ref(p["rsi_period"]), level(50))
    if strategy_id == "rsi_oversold_reversal":
        return up_cross(rsi_ref(p["rsi_period"]), level(p["oversold"]))
    if strategy_id == "donchian_channel_breakout":
        return up_cross(close, donchian_ref(p["period"])[0])
    if strategy_id == "donchian_lower_channel_exit":
        lower = donchian_ref(p["period"])[1]
        return [i for i in range(N) if lower[i] is not None and C[i] < lower[i]]
    if strategy_id in ("stochastic_crossover", "stochastic_bearish_crossover"):
        k, d = stoch_ref(p["k_period"], p["k_smooth"], p["d_period"])
        return cross(bullish, k, d)
    if strategy_id == "williams_r_reversion":
        return up_cross(wr_ref(p["period"]), level(-80))
    if strategy_id == "williams_r_reversal":
        return down_cross(wr_ref(p["period"]), level(-20))
    if strategy_id in ("cci_zero_line_crossover", "cci_zero_line_breakdown"):
        return cross(bullish, cci_ref(p["period"]), level(0))
    if strategy_id in ("parabolic_sar", "parabolic_sar_reversal"):
        trend = psar_ref()
        want = (False, True) if bullish else (True, False)
        return [i for i in range(2, N) if (trend[i - 1], trend[i]) == want]
    if strategy_id in ("dmi_crossover", "dmi_bearish_crossover"):
        plus, minus = dmi_ref(p["period"])
        return up_cross(plus, minus) if bullish else up_cross(minus, plus)
    if strategy_id in ("aroon_trend", "aroon_bearish_reversal"):
        up, down = aroon_ref(p["period"])
        return up_cross(up, down) if bullish else up_cross(down, up)
    if strategy_id == "inside_bar_breakout":
        return [
            i for i in range(2, N)
            if H[i - 1] < H[i - 2] and L[i - 1] > L[i - 2] and C[i] > H[i - 2]
        ]
    if strategy_id == "price_below_ema":
        return below(ema_ref(C, p["period"]))
    if strategy_id == "price_below_sma":
        return below(sma_ref(C, p["period"]))
    raise AssertionError(strategy_id)


# --------------------------------------------------------------------------- metadata


def test_category_constants_are_the_specified_stable_identifiers() -> None:
    assert STRATEGY_CATEGORIES == (
        "technical", "statistical", "machine_learning", "deep_learning",
        "reinforcement_learning", "llm_ai", "microstructure", "event_fundamental",
        "relative_value", "alternative_data",
    )
    assert TECHNICAL_SUB_CATEGORIES == (
        "momentum", "breakout", "vwap", "moving_average", "mean_reversion", "trend_following",
        "volatility", "price_action", "gap_trading", "intraday_session", "oscillator", "volume",
    )


@pytest.mark.parametrize("strategy_id", sorted(EXISTING))
def test_existing_six_strategies_carry_the_assigned_classification(strategy_id: str) -> None:
    spec = strategy_class(strategy_id).spec
    side, sub_category = EXISTING[strategy_id]
    assert spec.strategy_id == strategy_id and spec.version == "1.0.0"
    assert spec.side.value == side
    assert (spec.category, spec.sub_category) == ("technical", sub_category)


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_new_strategy_metadata_registration_and_role(strategy_id: str) -> None:
    side, sub_category, trigger_kind, lookback, _ = CATALOGUE[strategy_id]
    cls = strategy_class(strategy_id)
    spec = cls.spec
    assert isinstance(spec, StrategySpec)
    assert (spec.strategy_id, spec.version) == (strategy_id, "1.0.0")
    assert spec.category == "technical" and spec.sub_category == sub_category
    assert spec.side is StrategyRole[side]
    assert issubclass(cls, BuyStrategy if side == "BUY" else SellStrategy)
    assert issubclass(cls, SellRule) == (side == "SELL")
    assert (issubclass(cls, BuyStrategy), issubclass(cls, SellStrategy)) == (
        side == "BUY", side == "SELL")
    assert spec.trigger_kind is trigger_kind
    assert spec.required_timeframes == (CandleTimeframe.M1,)
    assert spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.completed_bars_only is True and spec.reference_symbols == ()
    assert spec.persistent_state_keys == ("last_signal_bar",)
    assert 1 <= spec.warmup_bars <= lookback
    assert "no alpha or profitability claim" in spec.methodology and spec.source
    # the documented default parameters validate and the registry resolves the class
    assert REGISTRY.validate_parameters(strategy_id, {}) is not None
    assert REGISTRY.get(strategy_id, "1.0.0") is cls


def test_registry_inventory_counts() -> None:
    buys = {cls.spec.strategy_id for cls in REGISTRY.list_strategies(StrategyRole.BUY)}
    sells = {cls.spec.strategy_id for cls in REGISTRY.list_strategies(StrategyRole.SELL)}
    assert buys == set(NEW_BUY) | {k for k, v in EXISTING.items() if v[0] == "BUY"}
    assert sells == set(NEW_SELL) | {k for k, v in EXISTING.items() if v[0] == "SELL"}
    assert (len(NEW_BUY), len(NEW_SELL)) == (14, 14)
    assert not buys & sells


def test_production_registry_registers_every_strategy_once() -> None:
    import inspect

    from mike_trade import app

    source = inspect.getsource(app.build_runtime)
    for strategy_id in CATALOGUE:
        cls_name = strategy_class(strategy_id).__name__
        assert source.count(f"{cls_name},") == 1, cls_name
    registry = StrategyRegistry()
    for strategy_id in CATALOGUE:
        registry.register(strategy_class(strategy_id))  # no duplicate (id, version)


def test_spec_rejects_unknown_classification() -> None:
    spec = strategy_class("sma_crossover").spec
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(spec, category="astrology")
    with pytest.raises(ValueError):
        replace(spec, sub_category="vibes")  # technical sub-categories are enumerated
    with pytest.raises(ValueError):
        replace(spec, category="Technical")
    assert replace(spec, category="statistical", sub_category="anything_snake").category == (
        "statistical")


# --------------------------------------------------------------------------- signals vs oracle


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_signals_match_independent_oracle_on_every_prefix(strategy_id: str) -> None:
    expected = expected_signals(strategy_id)
    assert expected, "scan path must exercise the rule at least once"
    assert scan(strategy_id) == expected


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_signal_payload_is_deterministic_and_prices_the_trigger_bar(strategy_id: str) -> None:
    index = expected_signals(strategy_id)[0]
    ctx = PREFIX[index]
    first, second = evaluate(strategy_id, ctx), evaluate(strategy_id, ctx)
    assert first == second and first.is_signal
    bar = BARS[index]
    spec = strategy_class(strategy_id).spec
    assert first.side is spec.side and first.symbol == SYMBOL
    assert first.bar_time == bar.start and first.reference_price == bar.close
    assert first.reason_codes == (ReasonCode.SIGNAL,)
    assert first.state_updates == {"last_signal_bar": bar.start.isoformat()}


# --------------------------------------------------------------------------- history boundaries


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_insufficient_history_never_signals_and_boundary_matches_documented_lookback(
    strategy_id: str,
) -> None:
    lookback = CATALOGUE[strategy_id][3]
    long_rows = path(lookback + 4)
    bars = candles(long_rows)
    for count in sorted({1, 2, lookback - 1}):
        if count < 1:
            continue
        result = evaluate(strategy_id, context(bars[:count]), params={})
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,), (count, result)
    at_boundary = evaluate(strategy_id, context(bars[:lookback]), params={})
    assert ReasonCode.INSUFFICIENT_DATA not in at_boundary.reason_codes, at_boundary


# --------------------------------------------------------------------------- guards


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_stale_non_regular_and_ineligible_contexts_never_signal(strategy_id: str) -> None:
    index = expected_signals(strategy_id)[0]
    bars = BARS[: index + 1]
    assert evaluate(strategy_id, context(bars)).is_signal
    stale = evaluate(strategy_id, context(bars, stale=True))
    assert stale.reason_codes == (ReasonCode.STALE_DATA,)
    for status in (SessionStatus.CLOSED, SessionStatus.PRE_OPEN):
        closed = evaluate(strategy_id, context(bars, status=status))
        assert not closed.is_signal and closed.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    if CATALOGUE[strategy_id][0] == "BUY":
        for state in ({}, {"eligibility": "HOLDING"}, {"eligibility": None}):
            blocked = evaluate(strategy_id, context(bars), state=state)
            assert not blocked.is_signal and blocked.reason_codes == (ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_same_completed_bar_never_emits_twice(strategy_id: str) -> None:
    index = expected_signals(strategy_id)[0]
    ctx = PREFIX[index]
    state = state_for(strategy_id)
    first = evaluate(strategy_id, ctx, state=state)
    assert first.is_signal
    state.update(first.state_updates)
    replay = evaluate(strategy_id, ctx, state=state)
    assert not replay.is_signal and replay.reason_codes == (ReasonCode.NO_SIGNAL,)


@pytest.mark.parametrize(
    "strategy_id",
    sorted(k for k, v in CATALOGUE.items() if v[2] is TriggerKind.EDGE_ON_CLOSED_BAR),
)
def test_a_crossing_across_an_overnight_gap_is_not_a_signal(strategy_id: str) -> None:
    index = expected_signals(strategy_id)[0]
    assert evaluate(strategy_id, PREFIX[index]).is_signal
    next_day = DAY + timedelta(days=1)
    rebased = [candle(i, *rows_i, day=DAY) for i, rows_i in enumerate(ROWS[:index])]
    rebased.append(candle(0, *ROWS[index], day=next_day, offset=0))
    result = evaluate(strategy_id, context(rebased))
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


FLAT_ROWS = [(Decimal("100"), Decimal("100"), Decimal("100"))] * 60
UNDEFINED_ON_FLAT = {
    "stochastic_crossover", "stochastic_bearish_crossover", "williams_r_reversion",
    "williams_r_reversal", "cci_zero_line_crossover", "cci_zero_line_breakdown",
    "dmi_crossover", "dmi_bearish_crossover",
}


@pytest.mark.parametrize(
    "strategy_id", sorted(set(CATALOGUE) - {"price_below_sma", "price_below_ema"})
)
def test_flat_prices_never_signal_and_undefined_indicators_report_insufficient_data(
    strategy_id: str,
) -> None:
    result = evaluate(strategy_id, context(candles(FLAT_ROWS)))
    assert not result.is_signal
    if strategy_id in UNDEFINED_ON_FLAT:
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_level_rules_signal_on_each_bar_while_the_condition_holds() -> None:
    falling = [(Decimal(100 - i) + Decimal("0.5"), Decimal(100 - i) - Decimal("0.5"),
                Decimal(100 - i)) for i in range(40)]
    bars = candles(falling)
    hits = [
        evaluate("price_below_sma", context(bars[: k + 1]), {"period": 5}).is_signal
        for k in range(len(bars))
    ]
    assert hits[:4] == [False] * 4 and all(hits[4:])
    ids = {
        evaluate("price_below_sma", context(bars[: k + 1]), {"period": 5}).signal_id
        for k in range(4, len(bars))
    }
    assert len(ids) == len(bars) - 4  # distinct deterministic id per bar


# --------------------------------------------------------------------------- literal rule examples


def test_inside_bar_breakout_rule_variants() -> None:
    def run(rows: list[tuple[str, str, str]]) -> bool:
        bars = candles([(Decimal(h), Decimal(low), Decimal(c)) for h, low, c in rows])
        return evaluate("inside_bar_breakout", context(bars), {}).is_signal

    mother = ("10", "5", "8")
    assert run([mother, ("9", "6", "8"), ("11", "8", "10.5")])  # inside, then close > 10
    assert not run([mother, ("9", "6", "8"), ("11", "8", "10")])  # close == mother high
    assert not run([mother, ("10", "6", "8"), ("11", "8", "10.5")])  # high equal: not inside
    assert not run([mother, ("9", "5", "8"), ("11", "8", "10.5")])  # low equal: not inside
    assert not run([mother, ("10.5", "6", "8"), ("12", "8", "11")])  # not inside
    assert not run([("9", "6", "8"), mother, ("11", "8", "10.5")])  # inside bar is bar t-2


def test_donchian_breakout_uses_prior_bars_only_and_the_close() -> None:
    flat = [(Decimal("10"), Decimal("9"), Decimal("9.5"))] * 6
    broke = candles(flat + [(Decimal("12"), Decimal("9.5"), Decimal("11"))])
    # close 11 > the prior 5-bar high 10 while the prior close 9.5 was inside its channel
    assert evaluate("donchian_channel_breakout", context(broke), {"period": 5}).is_signal
    touched = candles(flat + [(Decimal("12"), Decimal("9.5"), Decimal("10"))])
    # the intraday high 12 pierced the channel, but the close (10) did not exceed it
    assert not evaluate("donchian_channel_breakout", context(touched), {"period": 5}).is_signal
    # the current bar is excluded from its own channel: a lone spike bar cannot raise it
    again = candles(flat + [(Decimal("12"), Decimal("9.5"), Decimal("11")),
                            (Decimal("12"), Decimal("9.5"), Decimal("11.5"))])
    second = evaluate("donchian_channel_breakout", context(again), {"period": 5})
    assert not second.is_signal  # prior close 11 was already above its channel: no new edge


def test_williams_r_level_is_strict_on_the_latest_bar() -> None:
    # window high 11, low 1: close 2 -> %R -90; close 3 -> exactly -80; close 3.5 -> -75
    base = [(Decimal("11"), Decimal("1"), Decimal("2"))] * 6
    at_level = candles(base + [(Decimal("11"), Decimal("1"), Decimal("3"))])
    assert not evaluate("williams_r_reversion", context(at_level), {"period": 5}).is_signal
    above = candles(base + [(Decimal("11"), Decimal("1"), Decimal("3.5"))])
    assert evaluate("williams_r_reversion", context(above), {"period": 5}).is_signal


# --------------------------------------------------------------------------- parameters


@pytest.mark.parametrize(
    ("strategy_id", "params"),
    [
        ("sma_crossover", {"fast_period": 20, "slow_period": 10}),
        ("sma_bearish_crossover", {"fast_period": 10, "slow_period": 10}),
        ("ema_crossover", {"fast_period": 30, "slow_period": 20}),
        ("macd_signal_crossover", {"fast_period": 26, "slow_period": 12}),
        ("macd_zero_line_breakdown", {"fast_period": 12, "slow_period": 12}),
        ("parabolic_sar", {"step": Decimal("0.2"), "maximum": Decimal("0.1")}),
        ("parabolic_sar_reversal", {"step": Decimal("0.2"), "maximum": Decimal("0.1")}),
    ],
)
def test_inconsistent_parameters_fail_closed(strategy_id: str, params: dict[str, object]) -> None:
    with pytest.raises(ParameterValidationError):
        evaluate(strategy_id, PREFIX[-1], params)


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_unknown_and_float_parameters_are_rejected(strategy_id: str) -> None:
    spec = strategy_class(strategy_id).spec
    with pytest.raises(ParameterValidationError):
        spec.parameter_schema.validate({"surprise": 1})
    decimal_params = [p for p in spec.parameter_schema.parameters if p.kind.value == "DECIMAL"]
    for param in decimal_params:
        with pytest.raises(ParameterValidationError):
            spec.parameter_schema.validate({param.name: 0.5})


def test_default_parameters_match_the_documented_conventional_values() -> None:
    def defaults(strategy_id: str) -> dict[str, object]:
        return dict(REGISTRY.validate_parameters(strategy_id, {}))

    assert defaults("sma_crossover") == {"fast_period": 50, "slow_period": 200}
    assert defaults("ema_crossover") == {"fast_period": 10, "slow_period": 50}
    assert defaults("macd_signal_crossover") == {
        "fast_period": 12, "slow_period": 26, "signal_period": 9}
    assert defaults("macd_zero_line_crossover") == {"fast_period": 12, "slow_period": 26}
    assert defaults("rsi_oversold_reversal") == {"rsi_period": 14, "oversold": Decimal("30")}
    assert defaults("donchian_channel_breakout") == {"period": 20}
    assert defaults("stochastic_crossover") == {"k_period": 14, "k_smooth": 3, "d_period": 3}
    assert defaults("williams_r_reversion") == {"period": 14, "oversold": Decimal("-80")}
    assert defaults("williams_r_reversal") == {"period": 14, "overbought": Decimal("-20")}
    assert defaults("cci_zero_line_crossover") == {"period": 20}
    assert defaults("parabolic_sar") == {"step": Decimal("0.02"), "maximum": Decimal("0.20")}
    assert defaults("dmi_crossover") == {"period": 14}
    assert defaults("aroon_trend") == {"period": 25}


# --------------------------------------------------------------------------- backtest contract

OPEN_BACKTEST = datetime(2026, 3, 10, 13, 30, tzinfo=UTC)
MIN = timedelta(minutes=1)
PROVENANCE = DataProvenance("synthetic", "IEX", False, "r1")
FEED = domain_market.FeedProvenance("replay", "IEX", "synthetic", False)


def _replay_events(symbol: str, count: int) -> list[ReplayEvent]:
    events = []
    for index, (high, low, close) in enumerate(path(count)):
        start = OPEN_BACKTEST + index * MIN
        events.append(ReplayEvent.bar(domain_market.Candle(
            symbol, domain_market.Timeframe.M1, start, start + MIN, close, high, low, close,
            Decimal("100000"), FEED)))
    return events


def _intent_factory(sim: HistoricalExecutionSimulator):
    def factory(evaluation, context, request):  # noqa: ANN001
        buy = evaluation.side is StrategyRole.BUY
        return OrderIntent(
            intent_id=f"i-{evaluation.signal_id}", client_order_id=f"c-{evaluation.signal_id}",
            account_scope_id="backtest:test", cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol, side=OrderSide.BUY if buy else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if buy else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=context.as_of,
            quantity=Decimal("10") if buy else sim.held_quantity(evaluation.symbol),
        )

    return factory


def _backtest(tmp_path: Path, strategy_ids: tuple[str, ...], params: dict[str, dict]):
    events = _replay_events("AAA", 150)
    clock = ReplayClock(OPEN_BACKTEST)
    feed = HistoricalReplayFeed(events, PROVENANCE, clock=clock, seed=7)
    sim = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    request = BacktestRequest(
        symbols=("AAA",), window_start=OPEN_BACKTEST, window_end=OPEN_BACKTEST + timedelta(hours=6),
        period_role=PeriodRole.RESEARCH, config={}, seed=7, starting_capital=Decimal("100000"),
        strategy_ids=strategy_ids, strategy_params=params,
        initial_strategy_state={
            sid: dict(WATCHING) for sid in strategy_ids if CATALOGUE[sid][0] == "BUY"},
        context_history_bars=120, result_directory=tmp_path,
    )
    runner = BacktestRunner(REGISTRY, feed, clock, sim, intent_factory=_intent_factory(sim))
    return asyncio.run(runner.run(request))


def test_new_strategies_run_through_the_backtest_runner(tmp_path: Path) -> None:
    ids = ("parabolic_sar", "parabolic_sar_reversal", "macd_zero_line_crossover",
           "macd_zero_line_breakdown")
    params = {"macd_zero_line_crossover": {"fast_period": 5, "slow_period": 12},
              "macd_zero_line_breakdown": {"fast_period": 5, "slow_period": 12}}
    result = _backtest(tmp_path / "a", ids, params)
    sides = {row["strategy"]: row["side"] for row in result.evaluations}
    assert sides["parabolic_sar"] == "BUY" and sides["parabolic_sar_reversal"] == "SELL"
    assert sides["macd_zero_line_crossover"] == "BUY"
    buy_fills = [f for f in result.fills if f["side"] == "BUY"]
    sell_fills = [f for f in result.fills if f["side"] == "SELL"]
    assert buy_fills and sell_fills
    assert len(sell_fills) in (len(buy_fills), len(buy_fills) - 1)  # one position at a time
    for row in result.signals:
        assert row["bar_time"] <= row["time"]  # a signal never precedes its completed bar
        assert row["trigger_bar_end"] <= row["time"]
        assert row["context_max_bar_end"] <= row["time"]
    # deterministic: the same inputs reproduce the same run
    again = _backtest(tmp_path / "b", ids, params)
    assert again.evaluations == result.evaluations
    assert again.signals == result.signals and again.fills == result.fills
