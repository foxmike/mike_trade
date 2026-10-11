"""Focused contract, signal, state, and registration tests for SHORT batch A."""

from __future__ import annotations

import inspect
import json
import random
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from mike_trade.strategies.base import ParameterValidationError, ReasonCode, ShortStrategy

D = Decimal
OPEN = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
ARMED = {"eligibility": "WATCHING_SHORT"}
REGISTRY = _research_registry()

BATCH_A = (
    "short_sma_bearish_crossover",
    "short_macd_bearish_crossover",
    "short_macd_zero_line_breakdown",
    "short_rsi_centerline_breakdown",
    "short_dmi_bearish_crossover",
    "short_aroon_bearish_reversal",
    "short_parabolic_sar_bearish_reversal",
    "short_ichimoku_bearish_breakdown",
    "short_opening_range_breakdown",
    "short_vwap_breakdown",
    "short_bollinger_lower_band_breakdown",
    "short_keltner_channel_breakdown",
    "short_obv_breakdown",
    "short_cci_zero_line_breakdown",
    "short_adx_bearish_trend_confirmation",
)

SELL_REFERENCES = {
    "short_sma_bearish_crossover": "sma_bearish_crossover",
    "short_macd_bearish_crossover": "macd_bearish_crossover",
    "short_macd_zero_line_breakdown": "macd_zero_line_breakdown",
    "short_rsi_centerline_breakdown": "rsi_centerline_breakdown",
    "short_dmi_bearish_crossover": "dmi_bearish_crossover",
    "short_aroon_bearish_reversal": "aroon_bearish_reversal",
    "short_parabolic_sar_bearish_reversal": "parabolic_sar_reversal",
    "short_ichimoku_bearish_breakdown": "ichimoku_bearish_exit",
    "short_opening_range_breakdown": "opening_range_breakdown",
    "short_vwap_breakdown": "vwap_breakdown",
    "short_keltner_channel_breakdown": "keltner_channel_breakdown",
    "short_obv_breakdown": "obv_breakdown",
    "short_cci_zero_line_breakdown": "cci_zero_line_breakdown",
}

RECURSIVE = (
    "short_macd_bearish_crossover",
    "short_macd_zero_line_breakdown",
    "short_rsi_centerline_breakdown",
    "short_dmi_bearish_crossover",
    "short_parabolic_sar_bearish_reversal",
    "short_obv_breakdown",
    "short_adx_bearish_trend_confirmation",
)


def candle(
    index: int,
    close: object,
    *,
    high: object | None = None,
    low: object | None = None,
    volume: object = 100,
) -> Candle:
    value = D(str(close))
    start = OPEN + timedelta(minutes=index)
    return Candle(
        "SPY",
        CandleTimeframe.M1,
        start,
        start + timedelta(minutes=1),
        value,
        D(str(high)) if high is not None else value + D("0.5"),
        D(str(low)) if low is not None else value - D("0.5"),
        value,
        D(str(volume)),
        "sip",
        True,
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
    )


def bars(closes: Sequence[object], *, volumes: Sequence[object] | None = None) -> list[Candle]:
    return [
        candle(index, close, volume=100 if volumes is None else volumes[index])
        for index, close in enumerate(closes)
    ]


def context(
    series: Sequence[Candle],
    *,
    indicators: Mapping[str, object] | None = None,
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
        indicators=indicators or {},
    )


def signal_cases() -> dict[str, tuple[dict[str, object], list[Candle], dict[str, object]]]:
    opening = [
        candle(0, 10, high="10.5", low="9.5"),
        candle(1, 9, high=10, low=9),
        candle(2, "8.5", high=9, low=8),
    ]
    ichimoku = [candle(i, 100, high=101, low=99) for i in range(78)]
    ichimoku.append(candle(78, 95, high=100, low=94))
    return {
        "short_sma_bearish_crossover": (
            {"fast_period": 2, "slow_period": 3},
            bars([10, 10, 10, 8]),
            {},
        ),
        "short_macd_bearish_crossover": (
            {"fast_period": 2, "slow_period": 3, "signal_period": 2},
            bars([10, 10, 10, 10, 8]),
            {},
        ),
        "short_macd_zero_line_breakdown": (
            {"fast_period": 2, "slow_period": 3},
            bars([10, 10, 10, 8]),
            {},
        ),
        "short_rsi_centerline_breakdown": ({"rsi_period": 2}, bars([10, 11, 12, 8]), {}),
        "short_dmi_bearish_crossover": ({"period": 2}, bars([10, 11, 12, 8]), {}),
        "short_aroon_bearish_reversal": ({"period": 2}, bars([10, 11, 12, 8]), {}),
        "short_parabolic_sar_bearish_reversal": ({}, bars([10, 11, 8]), {}),
        "short_ichimoku_bearish_breakdown": ({}, ichimoku, {}),
        "short_opening_range_breakdown": ({"opening_range_minutes": 2}, opening, {}),
        "short_vwap_breakdown": (
            {},
            bars([10, 9]),
            {"vwap_prev": D("9.5"), "vwap": D("9.5")},
        ),
        "short_bollinger_lower_band_breakdown": (
            {"period": 2, "deviations": D("0.5")},
            bars([10, 10, 8]),
            {},
        ),
        "short_keltner_channel_breakdown": ({"period": 2}, bars([10, 10, "7.9"]), {}),
        "short_obv_breakdown": (
            {"support_period": 2},
            bars([10, 11, 12, 9], volumes=[100, 100, 100, 500]),
            {},
        ),
        "short_cci_zero_line_breakdown": ({"period": 2}, bars([10, 11, 8]), {}),
        "short_adx_bearish_trend_confirmation": (
            {"period": 2, "threshold": D(50)},
            bars([10, 11, 12, 11, 7]),
            {},
        ),
    }


def evaluate(
    strategy_id: str,
    series: Sequence[Candle],
    params: Mapping[str, object],
    indicators: Mapping[str, object],
    state: Mapping[str, object] = ARMED,
    **context_options: object,
):
    return REGISTRY.get(strategy_id)().evaluate(
        context(series, indicators=indicators, **context_options), params, state
    )


def test_production_and_research_registration_counts_and_roles() -> None:
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {
        StrategyRole.BUY: 82,
        StrategyRole.SELL: 42,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 28,
    }
    assert len(REGISTRY.list_strategies()) == 180
    source = inspect.getsource(app.build_runtime)
    for strategy_id in BATCH_A:
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side is StrategyRole.SHORT
        assert issubclass(cls, ShortStrategy)
        assert cls.spec.completed_bars_only
        assert cls.spec.warmup_bars == cls.required_bars({})
        assert source.count(f"{cls.__name__},") == 1


@pytest.mark.parametrize("strategy_id", BATCH_A)
def test_bearish_trigger_warmup_guards_duplicates_and_missing_bar(strategy_id: str) -> None:
    params, series, indicators = signal_cases()[strategy_id]
    cls = REGISTRY.get(strategy_id)
    result = evaluate(strategy_id, series, params, indicators)
    assert result.is_signal and result.side is StrategyRole.SHORT
    assert result.reference_price == series[-1].close

    quiet = bars([100] * len(series))
    quiet_indicators = {"vwap_prev": D(100), "vwap": D(100)}
    assert not evaluate(strategy_id, quiet, params, quiet_indicators).is_signal
    assert evaluate(
        strategy_id, series[-(cls.required_bars(params) - 1) :], params, indicators
    ).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    replay_state = {**ARMED, **result.state_updates}
    assert not evaluate(strategy_id, series, params, indicators, replay_state).is_signal
    assert evaluate(
        strategy_id,
        series,
        params,
        indicators,
        {},
    ).reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    assert evaluate(strategy_id, series, params, indicators, stale=True).reason_codes == (
        ReasonCode.STALE_DATA,
    )
    assert evaluate(
        strategy_id, series, params, indicators, session=SessionStatus.CLOSED
    ).reason_codes == (ReasonCode.NOT_ELIGIBLE,)

    gapped = list(series)
    last = gapped[-1]
    gapped[-1] = Candle(
        last.symbol,
        last.timeframe,
        last.start + timedelta(minutes=1),
        last.end + timedelta(minutes=1),
        last.open,
        last.high,
        last.low,
        last.close,
        last.volume,
        last.feed_id,
        last.consolidated,
        last.origin,
        last.price_basis,
        last.status,
    )
    assert evaluate(strategy_id, gapped, params, indicators).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )


@pytest.mark.parametrize("short_id,sell_id", SELL_REFERENCES.items())
def test_short_trigger_matches_established_sell_calculation(short_id: str, sell_id: str) -> None:
    params, series, indicators = signal_cases()[short_id]
    short_result = evaluate(short_id, series, params, indicators)
    sell_result = REGISTRY.get(sell_id)().evaluate(
        context(series, indicators=indicators), params, {}
    )
    assert short_result.is_signal and sell_result.is_signal


@pytest.mark.parametrize("strategy_id", BATCH_A)
def test_parameter_schema_rejects_unknown_values(strategy_id: str) -> None:
    with pytest.raises(ParameterValidationError):
        REGISTRY.get(strategy_id).spec.parameter_schema.validate({"unknown": 1})


@pytest.mark.parametrize(
    "strategy_id,params",
    (
        ("short_sma_bearish_crossover", {"fast_period": 3, "slow_period": 3}),
        ("short_macd_bearish_crossover", {"fast_period": 3, "slow_period": 3}),
        ("short_parabolic_sar_bearish_reversal", {"step": D("0.2"), "maximum": D("0.1")}),
        (
            "short_ichimoku_bearish_breakdown",
            {"tenkan_period": 30, "kijun_period": 20},
        ),
    ),
)
def test_cross_parameter_constraints(strategy_id: str, params: dict[str, object]) -> None:
    _, series, indicators = signal_cases()[strategy_id]
    with pytest.raises(ParameterValidationError):
        evaluate(strategy_id, series, params, indicators)


def random_bars(count: int) -> list[Candle]:
    rng = random.Random(711)
    price = D(100)
    result = []
    for index in range(count):
        price += D(str(round(rng.uniform(-1.2, 1.2), 3)))
        result.append(candle(index, price, volume=rng.randint(50, 500)))
    return result


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_recursive_state_advances_while_ineligible_and_restores(strategy_id: str) -> None:
    cls = REGISTRY.get(strategy_id)
    params = signal_cases()[strategy_id][0]
    need = cls.required_bars(params)
    series = random_bars(need + 2)

    blocked = evaluate(
        strategy_id,
        series[: need + 1],
        params,
        {},
        {"eligibility": "WATCHING_BUY"},
    )
    assert blocked.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    assert "indicator_state" in blocked.state_updates
    json.dumps(dict(blocked.state_updates))

    restored_state = {**ARMED, **blocked.state_updates}
    restored = evaluate(strategy_id, series[1:], params, {}, restored_state)
    fresh = evaluate(strategy_id, series, params, {}, ARMED)
    assert restored.is_signal == fresh.is_signal
    assert (
        restored.state_updates["indicator_state"]["latest"]
        == (fresh.state_updates["indicator_state"]["latest"])
    )


def test_ichimoku_uses_displaced_history_and_opening_range_waits_for_completion() -> None:
    ichimoku = REGISTRY.get("short_ichimoku_bearish_breakdown")
    params = {
        "tenkan_period": 2,
        "kijun_period": 3,
        "senkou_b_period": 4,
        "displacement": 1,
    }
    assert ichimoku.required_bars(params) == 6
    series = bars([10, 10, 10, 10, 10, 8])
    assert evaluate("short_ichimoku_bearish_breakdown", series, params, {}).is_signal

    opening_params = {"opening_range_minutes": 2}
    opening = signal_cases()["short_opening_range_breakdown"][1]
    assert evaluate(
        "short_opening_range_breakdown", opening[:2], opening_params, {}
    ).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    assert evaluate("short_opening_range_breakdown", opening, opening_params, {}).is_signal


def test_vwap_rejects_cross_session_or_missing_shared_values() -> None:
    params, series, indicators = signal_cases()["short_vwap_breakdown"]
    assert evaluate("short_vwap_breakdown", series, params, indicators).is_signal
    assert evaluate("short_vwap_breakdown", series, params, {}).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )
    prior = series[0]
    previous_session = Candle(
        prior.symbol,
        prior.timeframe,
        prior.start - timedelta(days=1),
        prior.end - timedelta(days=1),
        prior.open,
        prior.high,
        prior.low,
        prior.close,
        prior.volume,
        prior.feed_id,
        prior.consolidated,
        prior.origin,
        prior.price_basis,
        prior.status,
    )
    assert evaluate(
        "short_vwap_breakdown", [previous_session, series[-1]], params, indicators
    ).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
