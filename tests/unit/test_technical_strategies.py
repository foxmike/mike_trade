"""Focused tests for the technical strategy batch (14 BUY + 14 SELL) and its shared helpers.

The core check evaluates every strategy bar by bar, *the way the engines do* (persisted
strategy state carried between evaluations), and compares the bars that fire with an
independent exact-arithmetic oracle (``Fraction`` re-implementations of the published
rules, written separately from ``indicators.core``).  Further tests cover: rolling candle
buffers and process restarts (recursive indicators must not depend on the history window),
parameter-specific warmup, the timeframe contract, missing candles and session breaks,
guards, replay suppression, parameter validation, and the real ``BacktestRunner``.
"""

from __future__ import annotations

import asyncio
import json
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
from mike_trade.persistence.journal import canonical_payload_json
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
from mike_trade.strategies import closed_bar
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

EXTENSION_BUY = {
    "bollinger_band_breakout", "cci_reversion", "stochastic_oversold_reversal",
    "money_flow_index", "chaikin_money_flow", "engulfing_candle_reversal",
}
EXTENSION_SELL = {
    "cci_bearish_reversal", "money_flow_index_reversal", "chaikin_money_flow_reversal",
    "bearish_engulfing_exit",
}
BATCH2_BUY = {
    "rate_of_change_momentum", "moving_average_envelope", "moving_average_reclaim",
    "triple_moving_average", "hull_moving_average", "linear_regression_trend",
    "nr7_breakout", "awesome_oscillator", "keltner_channel_breakout",
}
BATCH2_SELL = {
    "awesome_oscillator_reversal", "keltner_channel_breakdown", "previous_swing_low_break",
}
BATCH3_BUY = {
    "adaptive_moving_average", "adx_trend_following", "atr_expansion_breakout",
    "bollinger_squeeze", "connors_rsi_mean_reversion", "ease_of_movement",
    "elder_impulse_system", "ema_pullback", "hammer_reversal", "higher_high_higher_low",
    "ichimoku_cloud_breakout", "keltner_channel_reversion", "moving_average_ribbon",
    "resistance_breakout", "supertrend", "volatility_squeeze_breakout",
}
BATCH4_BUY = {
    "obv_breakout", "accumulation_distribution_breakout", "volume_price_trend_breakout",
    "tsi_momentum", "stochastic_rsi", "ultimate_oscillator", "holy_grail_pullback",
    "rsi2_trend_pullback", "z_score_mean_reversion", "double_bottom_reversal",
    "inverse_head_and_shoulders", "ascending_triangle_breakout", "trendline_breakout",
    "triangle_breakout", "bull_flag_breakout",
}
SESSION_BUY = {
    "afternoon_breakout", "high_of_day_momentum", "midday_range_breakout",
    "opening_drive", "opening_range_retest", "power_hour_momentum",
}
DAILY_PATTERN_BUY = {"volatility_contraction_pattern", "cup_and_handle"}
GAP_BUY = {
    "gap_and_go", "gap_fill", "gap_reversal", "previous_close_reclaim",
    "previous_day_high_breakout", "previous_day_low_reversal",
}
BACKLOG_BUY_A = {
    "bollinger_band_mean_reversion", "vwap_pullback", "support_bounce",
}
BACKLOG_BUY_B = {
    "vwap_band_reversion", "intraday_consolidation_breakout",
}
BACKLOG_BUY_C = {"anchored_vwap_breakout", "vwap_rejection", "first_pullback_buy"}
FINAL_PATTERN_BUY = {"gap_exhaustion", "bull_pennant_breakout"}
BACKLOG_BUY_D = {
    "relative_volume_momentum", "moving_average_compression",
}
FINAL_MOMENTUM_BUY = {"momentum_continuation", "rsi_momentum"}
FINAL_REVERSAL_BUY = {
    "intraday_exhaustion_reversal", "volume_dry_up_pullback",
}
FINAL_REVERSAL_SELL = {
    "volatility_expansion_reversal", "volume_climax_exit",
}
BATCH3_SELL = {
    "adx_trend_weakening", "bollinger_upper_band_reversal", "failed_breakout_exit",
    "lower_low_confirmation", "opening_range_breakdown", "stochastic_rsi_reversal",
    "trendline_breakdown", "ultimate_oscillator_reversal",
}
BATCH5_SELL = {
    "accumulation_distribution_breakdown", "bearish_momentum_divergence",
    "double_top_reversal", "head_and_shoulders_breakdown", "ichimoku_bearish_exit",
    "obv_breakdown", "shooting_star_reversal", "supertrend_bearish_flip",
}
BACKLOG_SELL_D = {
    "high_volume_reversal",
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


#: strategies whose indicators are recursive (state-resumed) rather than windowed
RECURSIVE = {
    "ema_crossover", "ema_bearish_crossover", "macd_signal_crossover", "macd_bearish_crossover",
    "macd_zero_line_crossover", "macd_zero_line_breakdown", "rsi_centerline_crossover",
    "rsi_centerline_breakdown", "rsi_oversold_reversal", "price_below_ema", "dmi_crossover",
    "dmi_bearish_crossover", "parabolic_sar", "parabolic_sar_reversal",
}
EDGE = sorted(k for k, v in CATALOGUE.items() if v[2] is TriggerKind.EDGE_ON_CLOSED_BAR)

_CONTEXTS: dict[tuple, MarketContext] = {}


def window_context(bars: list[Candle], lo: int, hi: int, *,
                   status: SessionStatus = SessionStatus.REGULAR) -> MarketContext:
    key = (id(bars), lo, hi, status)
    if key not in _CONTEXTS:
        _CONTEXTS[key] = context(bars[lo:hi], status=status)
    return _CONTEXTS[key]


def simulate(
    strategy_id: str,
    bars: list[Candle] | None = None,
    *,
    params: Mapping[str, object] | None = None,
    buffer: int | None = None,
    restarts: tuple[int, ...] = (),
    lose_state: tuple[int, ...] = (),
    gated: range | tuple[int, ...] = (),
    skipped: range | tuple[int, ...] = (),
    cold: bool = False,
) -> list[int]:
    """Bars at which the strategy signals when driven bar by bar like an engine.

    ``buffer`` is the rolling candle history size; ``restarts`` JSON-round-trip the persisted
    state and build a fresh strategy object; ``lose_state`` drops it; ``gated`` evaluates in a
    non-regular session; ``skipped`` never evaluates; ``cold`` discards state every time.
    """
    series = BARS if bars is None else bars
    cls = strategy_class(strategy_id)
    chosen = CATALOGUE[strategy_id][4] if params is None else params
    strategy, state, signals = cls(), state_for(strategy_id), []
    for i in range(len(series)):
        if i in skipped:
            continue
        lo = 0 if buffer is None else max(0, i + 1 - buffer)
        status = SessionStatus.CLOSED if i in gated else SessionStatus.REGULAR
        if cold:
            state = state_for(strategy_id)
        result = strategy.evaluate(window_context(series, lo, i + 1, status=status), chosen, state)
        state.update(result.state_updates)
        if result.is_signal:
            signals.append(i)
        if i in restarts:
            state, strategy = json.loads(canonical_payload_json(state)), cls()
        if i in lose_state:
            state = state_for(strategy_id)
    return signals


def scan(strategy_id: str) -> list[int]:
    return simulate(strategy_id)


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
def test_existing_six_strategies_keep_identity_classification_and_static_warmup(
    strategy_id: str,
) -> None:
    cls = strategy_class(strategy_id)
    spec = cls.spec
    side, sub_category = EXISTING[strategy_id]
    assert spec.strategy_id == strategy_id and spec.version == "1.0.0"
    assert spec.side.value == side
    assert (spec.category, spec.sub_category) == ("technical", sub_category)
    # the new warmup hook defaults to the unchanged static value for strategies that do not
    # override it
    assert cls.required_bars({}) == spec.warmup_bars


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_new_strategy_metadata_registration_and_role(strategy_id: str) -> None:
    side, sub_category, trigger_kind, lookback, _ = CATALOGUE[strategy_id]
    cls = strategy_class(strategy_id)
    spec = cls.spec
    assert isinstance(spec, StrategySpec)
    assert (spec.strategy_id, spec.version) == (strategy_id, "1.0.0")
    assert spec.category == "technical" and spec.sub_category == sub_category
    assert spec.side is StrategyRole[side]
    assert (issubclass(cls, BuyStrategy), issubclass(cls, SellStrategy)) == (
        side == "BUY", side == "SELL")
    assert issubclass(cls, SellRule) == (side == "SELL")
    assert spec.trigger_kind is trigger_kind
    assert spec.required_timeframes == (CandleTimeframe.M1,)
    assert spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.completed_bars_only is True and spec.reference_symbols == ()
    expected_keys = ("last_signal_bar", "indicator_state") if strategy_id in RECURSIVE else (
        "last_signal_bar",)
    assert spec.persistent_state_keys == expected_keys
    assert "no alpha or profitability claim" in spec.methodology and spec.source
    assert REGISTRY.validate_parameters(strategy_id, {}) is not None
    assert REGISTRY.get(strategy_id, "1.0.0") is cls
    # the declared warmup is the requirement at the default parameters, and the hook agrees
    assert spec.warmup_bars == lookback == cls.required_bars({})


def test_registry_inventory_counts() -> None:
    buys = {cls.spec.strategy_id for cls in REGISTRY.list_strategies(StrategyRole.BUY)}
    sells = {cls.spec.strategy_id for cls in REGISTRY.list_strategies(StrategyRole.SELL)}
    assert buys == (
        set(NEW_BUY)
        | EXTENSION_BUY
        | BATCH2_BUY
        | BATCH3_BUY
        | BATCH4_BUY
        | SESSION_BUY
        | GAP_BUY
        | BACKLOG_BUY_A
        | BACKLOG_BUY_B
        | BACKLOG_BUY_C
        | BACKLOG_BUY_D
        | FINAL_PATTERN_BUY
        | FINAL_MOMENTUM_BUY
        | FINAL_REVERSAL_BUY
        | DAILY_PATTERN_BUY
        | {k for k, v in EXISTING.items() if v[0] == "BUY"}
    )
    assert sells == (
        set(NEW_SELL)
        | EXTENSION_SELL
        | BATCH2_SELL
        | BATCH3_SELL
        | BATCH5_SELL
        | BACKLOG_SELL_D
        | FINAL_REVERSAL_SELL
        | {k for k, v in EXISTING.items() if v[0] == "SELL"}
    )
    assert (len(NEW_BUY), len(NEW_SELL)) == (14, 14)
    assert (len(buys), len(sells)) == (93, 43)
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
    from dataclasses import replace

    spec = strategy_class("sma_crossover").spec
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
def test_engine_style_evaluation_matches_the_independent_oracle(strategy_id: str) -> None:
    expected = expected_signals(strategy_id)
    assert expected, "scan path must exercise the rule at least once"
    assert simulate(strategy_id) == expected


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_cold_start_from_the_full_history_matches_the_oracle(strategy_id: str) -> None:
    # No state is ever carried: every evaluation re-seeds from the whole (unbounded) history.
    assert simulate(strategy_id, cold=True) == expected_signals(strategy_id)


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
    assert first.state_updates["last_signal_bar"] == bar.start.isoformat()
    assert set(first.state_updates) <= set(spec.persistent_state_keys)


# --------------------------------------------------------------------------- continuity


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_a_rolling_candle_buffer_does_not_change_signals(strategy_id: str) -> None:
    """Identical market sequence, different history windows -> identical signals.

    The smallest possible buffer drops the oldest candle on every bar.
    """
    expected = expected_signals(strategy_id)
    minimum = strategy_class(strategy_id).required_bars(CATALOGUE[strategy_id][4])
    for buffer in (minimum, minimum + 7, 3 * minimum):
        assert simulate(strategy_id, buffer=buffer) == expected, f"buffer={buffer}"


def test_without_persisted_state_a_rolling_window_would_change_recursive_signals() -> None:
    """Negative control: proves the continuity tests are sensitive to the defect."""
    differing = []
    for strategy_id in sorted(RECURSIVE):
        minimum = strategy_class(strategy_id).required_bars(CATALOGUE[strategy_id][4])
        windowed = simulate(strategy_id, buffer=minimum + 4, cold=True)
        if windowed != expected_signals(strategy_id):
            differing.append(strategy_id)
    assert differing, "stateless rolling-window evaluation unexpectedly matched everywhere"


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_restart_with_persisted_state_matches_an_uninterrupted_run(strategy_id: str) -> None:
    expected = expected_signals(strategy_id)
    # state goes through the real canonical JSON serializer used for SHADOW persistence
    assert simulate(strategy_id, restarts=(70, 110)) == expected
    minimum = strategy_class(strategy_id).required_bars(CATALOGUE[strategy_id][4])
    assert simulate(strategy_id, restarts=(70, 110), buffer=minimum + 3) == expected


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_restart_with_lost_state_reseeds_from_the_available_history(strategy_id: str) -> None:
    # With the whole sequence still in the history the re-seed reproduces the same values.
    assert simulate(strategy_id, lose_state=(100,)) == expected_signals(strategy_id)


@pytest.mark.parametrize("strategy_id", sorted(RECURSIVE))
def test_saved_state_older_than_the_history_window_is_discarded_deterministically(
    strategy_id: str,
) -> None:
    ctx_old = window_context(BARS, 0, 61)
    ctx_new = window_context(BARS, 100, 141)  # 40-bar window that no longer reaches bar 60
    strategy = strategy_class(strategy_id)()
    params = CATALOGUE[strategy_id][4]
    state = state_for(strategy_id)
    state.update(strategy.evaluate(ctx_old, params, state).state_updates)
    resumed = strategy.evaluate(ctx_new, params, dict(state))
    fresh = strategy.evaluate(ctx_new, params, state_for(strategy_id))
    assert resumed == fresh  # nothing is guessed about the unseen bars: it re-seeds


@pytest.mark.parametrize(
    ("strategy_id", "other"),
    [
        ("ema_crossover", {"fast_period": 4, "slow_period": 12}),
        ("macd_signal_crossover", {"fast_period": 4, "slow_period": 10, "signal_period": 3}),
        ("rsi_centerline_crossover", {"rsi_period": 5}),
        ("dmi_crossover", {"period": 5}),
        ("parabolic_sar", {"step": Decimal("0.03"), "maximum": Decimal("0.3")}),
        ("price_below_ema", {"period": 8}),
    ],
)
def test_changing_calculation_parameters_discards_saved_state(
    strategy_id: str, other: dict[str, object]
) -> None:
    strategy = strategy_class(strategy_id)()
    state = state_for(strategy_id)
    state.update(strategy.evaluate(PREFIX[100], CATALOGUE[strategy_id][4], state).state_updates)
    with_old_state = strategy.evaluate(PREFIX[101], other, dict(state))
    assert with_old_state == strategy.evaluate(PREFIX[101], other, state_for(strategy_id))


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_gated_or_skipped_bars_still_leave_the_indicators_continuous(strategy_id: str) -> None:
    expected = expected_signals(strategy_id)
    minimum = strategy_class(strategy_id).required_bars(CATALOGUE[strategy_id][4])
    quiet = range(60, 90)
    # evaluated in a non-regular session: the state advances, nothing fires
    gated = simulate(strategy_id, gated=quiet)
    assert gated == [i for i in expected if i not in quiet]
    # not evaluated at all (e.g. a BUY strategy while a position is open): the next evaluation
    # catches up from the history, provided the buffer still reaches back to the last bar seen
    skipped = simulate(strategy_id, skipped=quiet, buffer=len(quiet) + minimum + 5)
    assert skipped == [i for i in expected if i not in quiet]


# --- the shared advance() helper in isolation


class _RunningSum:
    """Minimal recursive calculator exercising the snapshot/restore protocol."""

    def __init__(self) -> None:
        self.total, self.count = Decimal(0), 0

    def update(self, bar: Candle) -> tuple[Decimal, bool]:
        self.total += bar.close
        self.count += 1
        return self.total, self.count % 2 == 0

    def snapshot(self) -> dict[str, object]:
        return {"total": str(self.total), "count": self.count}

    @classmethod
    def restore(cls, snap: Mapping[str, object]) -> _RunningSum:
        calc = cls()
        calc.total, calc.count = Decimal(str(snap["total"])), int(snap["count"])  # type: ignore[call-overload]
        return calc


def _advance(state: Mapping[str, object], lo: int, hi: int, signature: str = "s"):
    return closed_bar.advance(
        state, tuple(BARS[lo:hi]), signature, _RunningSum, _RunningSum.restore,
        lambda calc, bar: calc.update(bar),
    )


def _closes(lo: int, hi: int) -> Decimal:
    return sum((b.close for b in BARS[lo:hi]), Decimal(0))


def test_advance_seeds_resumes_replays_and_reseeds() -> None:
    first = _advance({}, 0, 5)
    assert first.latest[0] == _closes(0, 5) and first.prior[0] == _closes(0, 4)
    stored = json.loads(canonical_payload_json(first.updates))  # JSON-safe
    # resume with a window that has advanced: identical to having fed every bar in order
    resumed = _advance(stored, 2, 9)
    assert resumed.latest[0] == _closes(0, 9) and resumed.prior[0] == _closes(0, 8)
    assert resumed.latest[1] is False and resumed.prior[1] is True  # parity of 9th/8th bar
    # replaying the same bar returns the stored outputs and asks for no state change
    again = _advance(stored, 0, 5)
    assert again.updates == {} and again.latest == first.latest and again.prior == first.prior
    # the history no longer reaches the last processed bar: re-seed from what is supplied
    assert _advance(stored, 9, 14).latest[0] == _closes(9, 14)
    # state from a later bar than the context, a different signature, or a corrupt snapshot
    ahead = json.loads(canonical_payload_json(_advance({}, 0, 20).updates))
    assert _advance(ahead, 0, 5).latest[0] == _closes(0, 5)
    assert _advance(stored, 0, 8, signature="other").latest[0] == _closes(0, 8)
    broken = json.loads(canonical_payload_json(first.updates))
    broken["indicator_state"]["calc"] = {"total": "not-a-number", "count": 1}
    assert _advance(broken, 0, 8).latest[0] == _closes(0, 8)


# --------------------------------------------------------------------------- warmup

#: (parameters, completed bars required) worked out by hand from each rule's definition
WARMUP_VARIANTS: dict[str, list[tuple[dict[str, object], int]]] = {
    **{k: [({"fast_period": 3, "slow_period": 5}, 6), ({"fast_period": 20, "slow_period": 100}, 101)]
       for k in ("sma_crossover", "sma_bearish_crossover", "ema_crossover",
                 "ema_bearish_crossover")},
    **{k: [({"fast_period": 3, "slow_period": 5, "signal_period": 2}, 7),
           ({"fast_period": 8, "slow_period": 20, "signal_period": 5}, 25)]
       for k in ("macd_signal_crossover", "macd_bearish_crossover")},
    **{k: [({"fast_period": 3, "slow_period": 5}, 6), ({"fast_period": 8, "slow_period": 40}, 41)]
       for k in ("macd_zero_line_crossover", "macd_zero_line_breakdown")},
    **{k: [({"rsi_period": 3}, 5), ({"rsi_period": 30}, 32)]
       for k in ("rsi_centerline_crossover", "rsi_centerline_breakdown",
                 "rsi_oversold_reversal")},
    **{k: [({"period": 3}, 5), ({"period": 50}, 52)]
       for k in ("donchian_channel_breakout", "dmi_crossover", "dmi_bearish_crossover",
                 "aroon_trend", "aroon_bearish_reversal")},
    **{k: [({"k_period": 3, "k_smooth": 1, "d_period": 1}, 4),
           ({"k_period": 21, "k_smooth": 5, "d_period": 5}, 30)]
       for k in ("stochastic_crossover", "stochastic_bearish_crossover")},
    **{k: [({"period": 3}, 4), ({"period": 40}, 41)]
       for k in ("williams_r_reversion", "williams_r_reversal")},
    **{k: [({"period": 3}, 4), ({"period": 60}, 61)]
       for k in ("cci_zero_line_crossover", "cci_zero_line_breakdown")},
    "parabolic_sar": [({"step": Decimal("0.05"), "maximum": Decimal("0.5")}, 3)],
    "parabolic_sar_reversal": [({"step": Decimal("0.05"), "maximum": Decimal("0.5")}, 3)],
    "inside_bar_breakout": [({}, 3)],
    "price_below_ema": [({"period": 3}, 3), ({"period": 100}, 100)],
    "price_below_sma": [({"period": 3}, 3), ({"period": 100}, 100)],
    "donchian_lower_channel_exit": [({"period": 3}, 4), ({"period": 50}, 51)],
}


def test_every_strategy_has_warmup_variants() -> None:
    assert set(WARMUP_VARIANTS) == set(CATALOGUE)


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_required_bars_follow_the_configured_parameters_exactly(strategy_id: str) -> None:
    cls = strategy_class(strategy_id)
    for params, required in WARMUP_VARIANTS[strategy_id]:
        assert cls.required_bars(params) == required, params
        bars = candles(path(required + 3))
        short = evaluate(strategy_id, context(bars[: required - 1]), params)
        assert not short.is_signal and short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,), (
            params, short)
        exact = evaluate(strategy_id, context(bars[:required]), params)
        assert ReasonCode.INSUFFICIENT_DATA not in exact.reason_codes, (params, exact)


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_default_configuration_never_signals_before_its_documented_lookback(
    strategy_id: str,
) -> None:
    lookback = CATALOGUE[strategy_id][3]
    bars = candles(path(lookback))
    for count in range(1, lookback):
        result = evaluate(strategy_id, context(bars[:count]), params={})
        assert not result.is_signal and result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_sell_contract_gates_on_the_configured_lookback_not_the_default() -> None:
    cls = strategy_class("sma_bearish_crossover")
    bars = candles(path(12))
    ctx = context(bars)
    # default (50/200) needs 201 bars: not warmed; a valid 3/5 configuration is evaluated
    assert evaluate("sma_bearish_crossover", ctx, {}).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)
    short = cls().evaluate(ctx, {"fast_period": 3, "slow_period": 5}, {})
    assert ReasonCode.INSUFFICIENT_DATA not in short.reason_codes
    assert SellRule._context_failure(ctx, cls.spec, 6) is None
    assert SellRule._context_failure(ctx, cls.spec, 13) == (
        ReasonCode.INSUFFICIENT_DATA, "NOT_WARMED")


# --------------------------------------------------------------------------- timeframe contract


class _FiveMinuteProbe(BuyStrategy):
    """Test double: a strategy that declares 5m through its own spec."""

    spec = StrategySpec(
        strategy_id="five_minute_probe", version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="momentum", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M5,), warmup_bars=2, completed_bars_only=True,
        requires_volume=False, trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M5, parameter_schema=strategy_class(
            "inside_bar_breakout").spec.parameter_schema,
        persistent_state_keys=(), methodology="probe", source="test double",
        execution_limitations=("test",), reason_codes=closed_bar.REASON_CODES,
    )

    def evaluate(self, context, params, state):  # noqa: ANN001
        bars = closed_bar.closed_bars(self.spec, context, 2)
        if bars is None:
            return closed_bar.insufficient(self.spec, context)
        return closed_bar.finish(self.spec, context, bars[-1], state, closed_bar.adjacent(
            bars[-2], bars[-1]))


def _five_minute_context(m1_count: int, m5_count: int, *, m5_gap: bool = False) -> MarketContext:
    m1 = candles(path(m1_count))
    m5 = []
    for i in range(m5_count):
        start = open_utc() + timedelta(minutes=5 * i + (5 if m5_gap and i == m5_count - 1 else 0))
        m5.append(Candle(
            symbol=SYMBOL, timeframe=CandleTimeframe.M5, start=start,
            end=start + timedelta(minutes=5), open=Decimal("10"), high=Decimal("11"),
            low=Decimal("9"), close=Decimal("10"), volume=Decimal("5"), feed_id="sip",
            consolidated=True, origin=CandleOrigin.REPLAY, price_basis=PriceBasis.RAW,
            status=CandleStatus.CLOSED))
    as_of = max(m1[-1].end, m5[-1].end) + timedelta(seconds=5)
    return MarketContext(
        symbol=SYMBOL, as_of=as_of, version=1, feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR, session_id="d",
        freshness=ContextFreshness(as_of, timedelta(0), timedelta(minutes=5), False, None),
        candles={CandleTimeframe.M1: tuple(m1), CandleTimeframe.M5: tuple(m5)}, indicators={})


def test_shared_helpers_use_the_timeframe_the_strategy_declares() -> None:
    assert not hasattr(closed_bar, "TIMEFRAME")  # no universal timeframe in the helpers
    ctx = _five_minute_context(m1_count=30, m5_count=3)
    m5_spec, m1_spec = _FiveMinuteProbe.spec, strategy_class("sma_crossover").spec
    assert closed_bar.closed_bars(m5_spec, ctx, 3) == ctx.candles[CandleTimeframe.M5]
    assert closed_bar.closed_bars(m5_spec, ctx, 4) is None  # only three 5m bars exist
    assert closed_bar.closed_bars(m1_spec, ctx, 30) == ctx.candles[CandleTimeframe.M1]
    # adjacency is expressed through the bars' own end, so it holds for any timeframe
    assert _FiveMinuteProbe().evaluate(ctx, {}, {}).is_signal  # adjacent 5m bars
    assert closed_bar.adjacent(*ctx.candles[CandleTimeframe.M5][:2])
    gapped = _five_minute_context(m1_count=30, m5_count=3, m5_gap=True)
    assert not closed_bar.adjacent(*gapped.candles[CandleTimeframe.M5][-2:])
    assert _FiveMinuteProbe().evaluate(gapped, {}, {}).is_signal is False


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_the_documented_one_minute_behavior_is_declared_not_assumed(strategy_id: str) -> None:
    module = __import__(strategy_class(strategy_id).__module__, fromlist=["TIMEFRAME"])
    spec = strategy_class(strategy_id).spec
    assert module.TIMEFRAME is CandleTimeframe.M1
    assert spec.required_timeframes == (module.TIMEFRAME,)
    assert spec.trigger_timeframe is module.TIMEFRAME
    # a context that only carries 5m candles gives this 1m strategy nothing to read
    only_m5 = _five_minute_context(m1_count=30, m5_count=3)
    stripped = MarketContext(
        symbol=SYMBOL, as_of=only_m5.as_of, version=1, feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR, session_id="d",
        freshness=only_m5.freshness, candles={CandleTimeframe.M5: only_m5.candles[
            CandleTimeframe.M5]}, indicators={})
    result = evaluate(strategy_id, stripped, {})
    assert not result.is_signal and result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# --------------------------------------------------------------------------- missing candles

GAP_AT, DAY_BREAK_AT = 74, 123  # chosen where several rules fire


def gapped_bars() -> list[Candle]:
    """The scan path with a missing 15-minute interval at bar 74 and a new session at bar 123."""
    out = []
    for i, (h, low, c) in enumerate(ROWS):
        if i < GAP_AT:
            out.append(candle(i, h, low, c))
        elif i < DAY_BREAK_AT:
            out.append(candle(i, h, low, c, offset=i + 15))
        else:
            out.append(candle(i, h, low, c, day=DAY + timedelta(days=1), offset=i - DAY_BREAK_AT))
    return out


GAPPED = gapped_bars()


def _suppressed(strategy_id: str) -> set[int]:
    if strategy_id == "inside_bar_breakout":  # needs three consecutive bars
        return {GAP_AT, GAP_AT + 1, DAY_BREAK_AT, DAY_BREAK_AT + 1}
    return {GAP_AT, DAY_BREAK_AT}


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_missing_intervals_and_session_breaks_are_never_read_as_continuous(
    strategy_id: str,
) -> None:
    expected = expected_signals(strategy_id)
    if CATALOGUE[strategy_id][2] is TriggerKind.EDGE_ON_CLOSED_BAR:
        expected = [i for i in expected if i not in _suppressed(strategy_id)]
    assert simulate(strategy_id, GAPPED) == expected
    minimum = strategy_class(strategy_id).required_bars(CATALOGUE[strategy_id][4])
    assert simulate(strategy_id, GAPPED, buffer=minimum + 5, restarts=(60, 100)) == expected


def test_the_gapped_scan_actually_suppresses_signals() -> None:
    lost = sum(
        len(expected_signals(sid)) - len([i for i in expected_signals(sid)
                                          if i not in _suppressed(sid)])
        for sid in EDGE
    )
    assert lost >= 7


@pytest.mark.parametrize("strategy_id", EDGE)
def test_a_missing_quarter_hour_before_the_trigger_bar_is_not_a_signal(strategy_id: str) -> None:
    index = expected_signals(strategy_id)[0]
    assert evaluate(strategy_id, PREFIX[index]).is_signal
    shifted = BARS[:index] + [candle(index, *ROWS[index], offset=index + 15)]
    result = evaluate(strategy_id, context(shifted))  # same values, 15 minutes missing
    assert not result.is_signal and result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize("strategy_id", EDGE)
def test_a_crossing_across_the_overnight_close_is_not_a_signal(strategy_id: str) -> None:
    index = expected_signals(strategy_id)[0]
    next_day = DAY + timedelta(days=1)
    rebased = BARS[:index] + [candle(index, *ROWS[index], day=next_day, offset=0)]
    result = evaluate(strategy_id, context(rebased))
    assert not result.is_signal and result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_level_rules_do_not_require_adjacent_bars() -> None:
    falling = [(Decimal(100 - i) + Decimal("0.5"), Decimal(100 - i) - Decimal("0.5"),
                Decimal(100 - i)) for i in range(40)]
    bars = [candle(i, *row, offset=i + (15 if i > 20 else 0)) for i, row in enumerate(falling)]
    assert evaluate("price_below_sma", context(bars), {"period": 5}).is_signal


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
    # even when only the indicator state survived, the replay yields the same signal id
    if strategy_id in RECURSIVE:
        partial = {**state_for(strategy_id), "indicator_state": state["indicator_state"]}
        assert evaluate(strategy_id, ctx, state=partial).signal_id == first.signal_id


@pytest.mark.parametrize("strategy_id", sorted(CATALOGUE))
def test_state_is_json_safe_declared_and_isolated_per_strategy(strategy_id: str) -> None:
    spec = strategy_class(strategy_id).spec
    state = state_for(strategy_id)
    result = evaluate(strategy_id, PREFIX[120], state=state)
    assert set(result.state_updates) <= set(spec.persistent_state_keys)
    json.loads(canonical_payload_json(dict(result.state_updates)))  # no floats/Decimals
    assert dict(state) == state_for(strategy_id)  # evaluate never mutates the supplied state
    if strategy_id in RECURSIVE:
        assert result.state_updates["indicator_state"]["signature"].startswith(
            f"{strategy_id}|{spec.version}|")


def test_pairs_sharing_an_indicator_keep_independent_state() -> None:
    buy_state, sell_state = state_for("macd_signal_crossover"), state_for(
        "macd_bearish_crossover")
    params = CATALOGUE["macd_signal_crossover"][4]
    buy = evaluate("macd_signal_crossover", PREFIX[100], params, buy_state)
    buy_state.update(buy.state_updates)
    assert "indicator_state" not in sell_state
    sell = evaluate("macd_bearish_crossover", PREFIX[100], params, sell_state)
    assert sell.state_updates["indicator_state"]["signature"] != (
        buy.state_updates["indicator_state"]["signature"])


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
    with pytest.raises(ParameterValidationError):
        strategy_class(strategy_id).required_bars({"surprise": 1})
    for param in [p for p in spec.parameter_schema.parameters if p.kind.value == "DECIMAL"]:
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


def _replay_events(symbol: str, count: int, skip: range | tuple[int, ...] = ()) -> list[ReplayEvent]:
    events = []
    for index, (high, low, close) in enumerate(path(count)):
        if index in skip:
            continue
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


def _backtest(tmp_path: Path, strategy_ids: tuple[str, ...], params: dict[str, dict], *,
              bars: int = 150, history: int = 120, skip: range | tuple[int, ...] = ()):
    events = _replay_events("AAA", bars, skip)
    clock = ReplayClock(OPEN_BACKTEST)
    feed = HistoricalReplayFeed(events, PROVENANCE, clock=clock, seed=7)
    sim = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    request = BacktestRequest(
        symbols=("AAA",), window_start=OPEN_BACKTEST, window_end=OPEN_BACKTEST + timedelta(hours=6),
        period_role=PeriodRole.RESEARCH, config={}, seed=7, starting_capital=Decimal("100000"),
        strategy_ids=strategy_ids, strategy_params=params,
        initial_strategy_state={
            sid: dict(WATCHING) for sid in strategy_ids if CATALOGUE.get(sid, ("",))[0] == "BUY"},
        context_history_bars=history, result_directory=tmp_path,
    )
    runner = BacktestRunner(REGISTRY, feed, clock, sim, intent_factory=_intent_factory(sim))
    return asyncio.run(runner.run(request))


RECURSIVE_PAIRS = (
    "parabolic_sar", "parabolic_sar_reversal", "macd_zero_line_crossover",
    "macd_zero_line_breakdown", "ema_crossover", "ema_bearish_crossover",
    "rsi_centerline_crossover", "rsi_centerline_breakdown",
)
RECURSIVE_PARAMS = {
    "macd_zero_line_crossover": {"fast_period": 5, "slow_period": 12},
    "macd_zero_line_breakdown": {"fast_period": 5, "slow_period": 12},
    "ema_crossover": {"fast_period": 5, "slow_period": 15},
    "ema_bearish_crossover": {"fast_period": 5, "slow_period": 15},
    "rsi_centerline_crossover": {"rsi_period": 7},
    "rsi_centerline_breakdown": {"rsi_period": 7},
}


def test_new_strategies_run_through_the_backtest_runner(tmp_path: Path) -> None:
    result = _backtest(tmp_path / "a", RECURSIVE_PAIRS, RECURSIVE_PARAMS)
    sides = {row["strategy"]: row["side"] for row in result.evaluations}
    assert sides["parabolic_sar"] == "BUY" and sides["parabolic_sar_reversal"] == "SELL"
    assert sides["macd_zero_line_crossover"] == "BUY" and sides["ema_bearish_crossover"] == "SELL"
    buy_fills = [f for f in result.fills if f["side"] == "BUY"]
    sell_fills = [f for f in result.fills if f["side"] == "SELL"]
    assert buy_fills and sell_fills
    assert len(sell_fills) in (len(buy_fills), len(buy_fills) - 1)  # one position at a time
    for row in result.signals:
        assert row["bar_time"] <= row["time"]  # a signal never precedes its completed bar
        assert row["trigger_bar_end"] <= row["time"]
        assert row["context_max_bar_end"] <= row["time"]
    again = _backtest(tmp_path / "b", RECURSIVE_PAIRS, RECURSIVE_PARAMS)
    assert again.evaluations == result.evaluations
    assert again.signals == result.signals and again.fills == result.fills


def test_backtest_signals_do_not_depend_on_the_history_buffer_size(tmp_path: Path) -> None:
    small = _backtest(tmp_path / "s", RECURSIVE_PAIRS, RECURSIVE_PARAMS, history=60)
    large = _backtest(tmp_path / "l", RECURSIVE_PAIRS, RECURSIVE_PARAMS, history=140)
    key = [(r["strategy"], r["bar_time"], r["signal_id"]) for r in small.signals]
    assert key and key == [(r["strategy"], r["bar_time"], r["signal_id"]) for r in large.signals]
    assert [f["price"] for f in small.fills] == [f["price"] for f in large.fills]


def test_backtest_treats_missing_minutes_like_the_shadow_engine_does(tmp_path: Path) -> None:
    skip = range(30, 45)  # a missing quarter hour in the replayed feed
    result = _backtest(tmp_path, RECURSIVE_PAIRS, RECURSIVE_PARAMS, skip=skip)
    starts = {OPEN_BACKTEST + i * MIN for i in range(150) if i not in skip}
    assert result.signals
    for row in result.signals:
        # the bar that fired always has its immediate predecessor in the feed
        assert row["bar_time"] - MIN in starts, row["bar_time"]


def test_valid_short_configurations_are_evaluated_before_the_default_lookback(
    tmp_path: Path,
) -> None:
    short = {"sma_crossover": {"fast_period": 2, "slow_period": 3},
             "sma_bearish_crossover": {"fast_period": 2, "slow_period": 3}}
    result = _backtest(tmp_path / "short", ("sma_crossover", "sma_bearish_crossover"), short,
                       bars=80)
    counts = {row["strategy"]: [] for row in result.evaluations}
    for row in result.evaluations:
        counts[row["strategy"]].append(row["trigger_history_count"])
    assert counts["sma_crossover"][0] == 4  # required_bars of a 2/3 configuration
    assert min(counts["sma_bearish_crossover"]) < 201 and result.fills
    default = _backtest(tmp_path / "default", ("sma_crossover",), {}, bars=80)
    assert not default.evaluations  # 201 bars are needed at the defaults: never evaluated


def test_existing_five_minute_strategy_is_still_compatible_with_the_backtest(
    tmp_path: Path,
) -> None:
    result = _backtest(tmp_path, ("parabolic_sar", "rsi_reversal"), {}, bars=150)
    rows = [r for r in result.evaluations if r["strategy"] == "rsi_reversal"]
    # rsi_reversal is a 5m strategy: evaluated on completed 5m bars while a position is held,
    # gated on its unchanged static warmup (the default of the new required_bars hook)
    assert rows and all(r["trigger_timeframe"] == "5m" for r in rows)
    assert rows[0]["trigger_history_count"] == strategy_class("rsi_reversal").spec.warmup_bars
    assert any(r["strategy"] == "parabolic_sar" and r["trigger_timeframe"] == "1m"
               for r in result.evaluations)


# --------------------------------------------------------------------------- finite-window library extension

#: id -> (default period requirement, [(parameters, requirement)]) worked out by hand:
#: a prior and a latest value of an n-bar window need n + 1 bars (MFI compares typical
#: prices, so each value needs n + 1 bars and two values n + 2).
EXTENSION_WARMUP: dict[str, tuple[int, list[tuple[dict[str, object], int]]]] = {
    "bollinger_band_breakout": (21, [({"period": 5}, 6), ({"period": 50}, 51)]),
    "cci_reversion": (21, [({"period": 3}, 4), ({"period": 50}, 51)]),
    "cci_bearish_reversal": (21, [({"period": 3}, 4), ({"period": 50}, 51)]),
    "stochastic_oversold_reversal": (15, [({"period": 2}, 3), ({"period": 50}, 51)]),
    "money_flow_index": (16, [({"period": 2}, 4), ({"period": 50}, 52)]),
    "money_flow_index_reversal": (16, [({"period": 2}, 4), ({"period": 50}, 52)]),
    "chaikin_money_flow": (21, [({"period": 2}, 3), ({"period": 50}, 51)]),
    "chaikin_money_flow_reversal": (21, [({"period": 2}, 3), ({"period": 50}, 51)]),
    "engulfing_candle_reversal": (2, [({}, 2)]),
    "bearish_engulfing_exit": (2, [({}, 2)]),
}


def _extension_state(strategy_id: str) -> dict[str, object]:
    return dict(WATCHING) if strategy_id in EXTENSION_BUY else {}


def test_bollinger_middle_band_of_ten_through_fourteen_is_twelve() -> None:
    from mike_trade.strategies.indicators.finite import bollinger

    lower, middle, upper = bollinger([Decimal(v) for v in (10, 11, 12, 13, 14)], 5, Decimal(2))
    assert middle == Decimal(12)
    width = Decimal(2) * Decimal(2).sqrt()  # population SD of 10..14 is sqrt(2); 2 deviations
    assert abs((upper - middle) - width) < Decimal("1e-20")
    assert abs((middle - lower) - width) < Decimal("1e-20")


@pytest.mark.parametrize("strategy_id", sorted(EXTENSION_WARMUP))
def test_extension_warmup_is_parameter_specific(strategy_id: str) -> None:
    default, variants = EXTENSION_WARMUP[strategy_id]
    cls = strategy_class(strategy_id)
    assert cls.spec.warmup_bars == default == cls.required_bars({})
    for params, required in variants:
        assert cls.required_bars(params) == required
        bars = candles(path(required + 3))
        short = cls().evaluate(context(bars[: required - 1]), params, _extension_state(strategy_id))
        if required > 1:
            assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
        exact = cls().evaluate(context(bars[:required]), params, _extension_state(strategy_id))
        assert ReasonCode.INSUFFICIENT_DATA not in exact.reason_codes, (params, exact)


@pytest.mark.parametrize("strategy_id", sorted(EXTENSION_WARMUP))
def test_extension_strategies_treat_a_missing_candle_as_a_gap(strategy_id: str) -> None:
    params = EXTENSION_WARMUP[strategy_id][1][0][0]
    cls = strategy_class(strategy_id)
    need = cls.required_bars(params)
    bars = candles(path(need + 5))
    bars = bars[:-1] + [candle(len(bars) - 1, *ROWS[len(bars) - 1], offset=len(bars) + 14)]
    result = cls().evaluate(context(bars), params, _extension_state(strategy_id))
    assert not result.is_signal and result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize("strategy_id", sorted(EXTENSION_WARMUP))
def test_extension_strategies_honor_the_shared_guards(strategy_id: str) -> None:
    cls = strategy_class(strategy_id)
    ctx_bars = candles(path(60))
    assert cls().evaluate(context(ctx_bars, stale=True), {}, _extension_state(strategy_id)
                          ).reason_codes == (ReasonCode.STALE_DATA,)
    assert cls().evaluate(context(ctx_bars, status=SessionStatus.CLOSED), {},
                          _extension_state(strategy_id)).reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    if strategy_id in EXTENSION_BUY:
        assert cls().evaluate(context(ctx_bars), {}, {}).reason_codes == (
            ReasonCode.NOT_ELIGIBLE,)
    assert cls.spec.trigger_timeframe is CandleTimeframe.M1
    assert cls.spec.required_timeframes == (CandleTimeframe.M1,)


def test_new_strategies_run_through_the_real_evaluation_paths_and_a_backtest(
    tmp_path: Path,
) -> None:
    # representative BUY and SELL through the real evaluate()/SellRule path ...
    bars = candles(path(60))
    for index in range(len(bars)):  # sweep until the CCI rules each fire once
        buy = strategy_class("cci_reversion")().evaluate(
            context(bars[: index + 1]), {"period": 8}, dict(WATCHING))
        if buy.is_signal:
            assert buy.side is StrategyRole.BUY and buy.reference_price == bars[index].close
            break
    else:
        pytest.fail("cci_reversion never fired on the scan path")
    assert any(
        strategy_class("cci_bearish_reversal")().evaluate(
            context(bars[: i + 1]), {"period": 8}, {}).is_signal
        for i in range(len(bars))
    )
    # ... and one small BacktestRunner run with both sides
    params = {"cci_reversion": {"period": 8}, "cci_bearish_reversal": {"period": 8}}
    result = _backtest(tmp_path, ("cci_reversion", "cci_bearish_reversal"), params)
    assert any(f["side"] == "BUY" for f in result.fills)
    assert any(f["side"] == "SELL" for f in result.fills)
    assert {r["strategy"]: r["side"] for r in result.evaluations} == {
        "cci_reversion": "BUY", "cci_bearish_reversal": "SELL"}
