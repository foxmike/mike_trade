"""Independent formula checks and contract checks for the finite-window strategy additions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleOrigin, CandleStatus, CandleTimeframe, PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.indicators.finite import bollinger, chaikin_money_flow, engulfing, money_flow_index
from mike_trade.strategies.base import ParameterValidationError
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.research.commands import _research_registry

D = Decimal
NY = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=NY).astimezone(UTC)


def bar(i: int, high: str, low: str, close: str, *, open_: str | None = None, volume: str = "1") -> Candle:
    start = OPEN + timedelta(minutes=i)
    return Candle(symbol="SPY", timeframe=CandleTimeframe.M1, start=start,
        end=start + timedelta(minutes=1), open=D(open_ or close), high=D(high), low=D(low),
        close=D(close), volume=D(volume), feed_id="sip", consolidated=True,
        origin=CandleOrigin.REPLAY, price_basis=PriceBasis.RAW, status=CandleStatus.CLOSED)


def ctx(bars: list[Candle]) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=1)
    return MarketContext(symbol="SPY", as_of=as_of, version=len(bars), feed_id="sip",
        consolidated=True, price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id="2026-10-08", freshness=ContextFreshness(newest_data_ts=bars[-1].end,
        age=timedelta(seconds=1), max_age=timedelta(minutes=5), stale=False, reason=None),
        candles={CandleTimeframe.M1: tuple(bars)}, indicators={})


def test_finite_indicator_helpers_match_hand_calculated_values() -> None:
    lower, middle, upper = bollinger([D("1"), D("2"), D("3")], 3, D("1"))
    assert middle == D("2")
    assert lower == D("2") - (D(2) / D(3)).sqrt()
    assert upper == D("2") + (D(2) / D(3)).sqrt()

    # Two typical-price comparisons: positive money flow 11*2=22 and negative 9*1=9.
    mf = money_flow_index([
        bar(0, "10", "10", "10", volume="1"),
        bar(1, "11", "11", "11", volume="2"),
        bar(2, "9", "9", "9", volume="1"),
    ], 2)
    assert mf == D(100) * D(22) / D(31)

    # Multipliers +1 and -1, weighted by volumes 2 and 1: CMF = 1/3.
    cmf = chaikin_money_flow([
        bar(0, "11", "1", "11", volume="2"),
        bar(1, "11", "1", "1", volume="1"),
    ])
    assert cmf == D(1) / D(3)
    assert chaikin_money_flow([bar(0, "5", "5", "5", volume="0")]) is None


def test_engulfing_uses_real_body_not_shadows() -> None:
    bearish = bar(0, "12", "7", "9", open_="10")
    bullish_engulf = bar(1, "11", "8", "11", open_="8")
    bullish_partial = bar(1, "12", "7", "9.8", open_="9.2")
    assert engulfing(bearish, bullish_engulf, bullish=True)
    assert not engulfing(bearish, bullish_partial, bullish=True)

    bullish = bar(0, "12", "7", "10", open_="9")
    bearish_engulf = bar(1, "11", "8", "8", open_="11")
    assert engulfing(bullish, bearish_engulf, bullish=False)


def test_new_strategy_ids_are_registered_with_correct_roles_and_metadata() -> None:
    import inspect

    from mike_trade import app

    registry: StrategyRegistry = _research_registry()
    expected = {
        "bollinger_band_breakout": StrategyRole.BUY,
        "cci_reversion": StrategyRole.BUY,
        "stochastic_oversold_reversal": StrategyRole.BUY,
        "money_flow_index": StrategyRole.BUY,
        "chaikin_money_flow": StrategyRole.BUY,
        "engulfing_candle_reversal": StrategyRole.BUY,
        "cci_bearish_reversal": StrategyRole.SELL,
        "money_flow_index_reversal": StrategyRole.SELL,
        "chaikin_money_flow_reversal": StrategyRole.SELL,
        "bearish_engulfing_exit": StrategyRole.SELL,
    }
    for strategy_id, role in expected.items():
        strategy = registry.get(strategy_id)
        assert strategy.spec.side is role
        assert strategy.spec.category == "technical"
        assert strategy.spec.source.strip()
        assert strategy.spec.warmup_bars >= 2
        assert inspect.getsource(app.build_runtime).count(f"{strategy.__name__},") == 1


def test_engulfing_strategies_emit_only_on_completed_consecutive_bars() -> None:
    from mike_trade.strategies.buy.engulfing_candle_reversal import EngulfingCandleReversalBuy
    from mike_trade.strategies.sell.bearish_engulfing_exit import BearishEngulfingExitSell

    bullish = [bar(0, "10", "8", "9", open_="10"),
               bar(1, "11", "8", "11", open_="8.5")]
    assert EngulfingCandleReversalBuy().evaluate(ctx(bullish), {}, {"eligibility": "WATCHING_BUY"}).is_signal

    bearish = [bar(0, "11", "8", "10", open_="9"),
               bar(1, "11", "7", "8", open_="10.5")]
    assert BearishEngulfingExitSell().evaluate(ctx(bearish), {}, {}).is_signal

    gap = [bearish[0], bar(2, "11", "7", "8", open_="10.5")]
    assert not BearishEngulfingExitSell().evaluate(ctx(gap), {}, {}).is_signal


def test_each_indicator_strategy_fires_on_a_hand_built_threshold_cross() -> None:
    from mike_trade.strategies.buy.bollinger_band_breakout import BollingerBandBreakoutBuy
    from mike_trade.strategies.buy.cci_reversion import CciReversionBuy
    from mike_trade.strategies.buy.stochastic_oversold_reversal import StochasticOversoldReversalBuy
    from mike_trade.strategies.buy.money_flow_index import MoneyFlowIndexBuy
    from mike_trade.strategies.buy.chaikin_money_flow import ChaikinMoneyFlowBuy
    from mike_trade.strategies.sell.cci_bearish_reversal import CciBearishReversalSell
    from mike_trade.strategies.sell.money_flow_index_reversal import MoneyFlowIndexReversalSell
    from mike_trade.strategies.sell.chaikin_money_flow_reversal import ChaikinMoneyFlowReversalSell

    def closes(values: list[str], *, volume: str = "1") -> list[Candle]:
        return [bar(i, v, v, v, volume=volume) for i, v in enumerate(values)]

    # Prior close equals the flat upper band; the final close exceeds the new upper band.
    prices = ["9"] * 51 + ["10"]
    assert BollingerBandBreakoutBuy().evaluate(ctx(closes(prices)),
        {"period": 2, "deviations": D("0.5")}, {"eligibility": "WATCHING_BUY"}).is_signal

    # CCI(3): prior [12,11,10] is -100; latest [11,10,12] is +100.
    prices = ["10"] * 48 + ["12", "11", "10", "12"]
    assert CciReversionBuy().evaluate(ctx(closes(prices)), {"period": 3},
        {"eligibility": "WATCHING_BUY"}).is_signal
    # Reverse the last CCI legs: prior +100 then latest -100.
    prices = ["10"] * 48 + ["10", "11", "12", "10"]
    assert CciBearishReversalSell().evaluate(ctx(closes(prices)), {"period": 3}, {}).is_signal

    # Fast %K(2) crosses from 0 to 40 while the range remains 1..11.
    stoch = [bar(i, "11", "1", "1") for i in range(51)] + [bar(51, "11", "1", "5")]
    assert StochasticOversoldReversalBuy().evaluate(ctx(stoch), {"period": 2},
        {"eligibility": "WATCHING_BUY"}).is_signal

    # MFI(2) moves from 0 to above 20 after the final rise.
    prices = ["10"] * 48 + ["10", "9", "8", "10"]
    assert MoneyFlowIndexBuy().evaluate(ctx(closes(prices)), {"period": 2},
        {"eligibility": "WATCHING_BUY"}).is_signal
    # MFI(2) moves from 100 to below 80 after the final decline.
    prices = ["10"] * 48 + ["8", "9", "10", "9"]
    assert MoneyFlowIndexReversalSell().evaluate(ctx(closes(prices)), {"period": 2}, {}).is_signal

    # CMF(2): the last high-close bar has greater volume than the preceding low-close bar.
    neg = [bar(i, "11", "1", "1") for i in range(50)]
    cmf_up = neg + [bar(50, "11", "1", "1"), bar(51, "11", "1", "10", volume="3")]
    assert ChaikinMoneyFlowBuy().evaluate(ctx(cmf_up), {"period": 2},
        {"eligibility": "WATCHING_BUY"}).is_signal
    pos = [bar(i, "11", "1", "10") for i in range(50)]
    cmf_down = pos + [bar(50, "11", "1", "10"), bar(51, "11", "1", "1", volume="3")]
    assert ChaikinMoneyFlowReversalSell().evaluate(ctx(cmf_down), {"period": 2}, {}).is_signal


def test_bounded_lookback_defaults_validate_and_reject_invalid_periods() -> None:
    from mike_trade.strategies.buy.chaikin_money_flow import ChaikinMoneyFlowBuy
    from mike_trade.strategies.sell.money_flow_index_reversal import MoneyFlowIndexReversalSell

    assert ChaikinMoneyFlowBuy.spec.parameter_schema.validate({})["period"] == 20
    with pytest.raises(ParameterValidationError):
        MoneyFlowIndexReversalSell.spec.parameter_schema.validate({"period": 1})
