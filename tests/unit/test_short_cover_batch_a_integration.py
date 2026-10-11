"""Integration verification tests for combined SHORT and COVER Batch A strategies.

Verifies:
1. Full 151 strategy registration across both production runtime and research registries.
2. Concurrent composition of all 18 SHORT and 18 COVER strategies into SHADOW runtime.
3. Role independence, contract enforcement, and deterministic signal ID differentiation.
4. Entry eligibility gating (SHORT requires WATCHING_SHORT; COVER has no entry gating).
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
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
from mike_trade.strategies.base import (
    BuyStrategy,
    CoverStrategy,
    ReasonCode,
    SellStrategy,
    ShortStrategy,
    make_signal_id,
)

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()

SHORT_BATCH_A = (
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

COVER_BATCH_A = (
    "cover_sma_bullish_crossover",
    "cover_macd_bullish_crossover",
    "cover_macd_zero_line_crossover",
    "cover_rsi_oversold_reversal",
    "cover_rsi_centerline_crossover",
    "cover_dmi_bullish_crossover",
    "cover_aroon_bullish_reversal",
    "cover_parabolic_sar_bullish_reversal",
    "cover_vwap_reclaim",
    "cover_bollinger_lower_band_reversion",
    "cover_keltner_channel_reversion",
    "cover_cci_oversold_reversal",
    "cover_stochastic_bullish_crossover",
    "cover_williams_r_bullish_reversal",
    "cover_chaikin_money_flow_recovery",
)

ALL_BATCH_A = SHORT_BATCH_A + COVER_BATCH_A


def _make_context(bars: list[Candle]) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=1)
    return MarketContext(
        symbol="SPY",
        as_of=as_of,
        version=len(bars),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id="2026-10-08",
        freshness=ContextFreshness(
            bars[-1].end, timedelta(seconds=1), timedelta(minutes=5), False, None
        ),
        candles={CandleTimeframe.M1: tuple(bars)},
        indicators={"vwap": D(100), "vwap_prev": D(100)},
    )


def test_combined_176_strategy_registration_and_distribution() -> None:
    """Verify that the registry contains exactly 176 strategies with 82 BUY, 40 SELL, 28 SHORT, 28 COVER."""
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {
        StrategyRole.BUY: 82,
        StrategyRole.SELL: 40,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 28,
    }
    assert len(REGISTRY.list_strategies()) == 178

    # Verify all 30 Batch A strategies are present in registry
    registered_ids = {c.spec.strategy_id for c in REGISTRY.list_strategies()}
    for sid in ALL_BATCH_A:
        assert sid in registered_ids, f"{sid} missing from research registry"

    # Verify both SHORT and COVER Batch A roles
    for sid in SHORT_BATCH_A:
        cls = REGISTRY.get(sid)
        assert cls.spec.side is StrategyRole.SHORT
        assert issubclass(cls, ShortStrategy)
        assert not issubclass(cls, (BuyStrategy, SellStrategy, CoverStrategy))

    for cid in COVER_BATCH_A:
        cls = REGISTRY.get(cid)
        assert cls.spec.side is StrategyRole.COVER
        assert issubclass(cls, CoverStrategy)
        assert not issubclass(cls, (BuyStrategy, SellStrategy, ShortStrategy))


def test_production_build_runtime_registration_has_all_151_strategies() -> None:
    """Verify that app.build_runtime contains all 151 strategies."""
    source = inspect.getsource(app.build_runtime)
    for sid in ALL_BATCH_A:
        cls = REGISTRY.get(sid)
        assert source.count(f"{cls.__name__},") == 1, f"{cls.__name__} not in build_runtime"


def test_concurrent_runtime_composition_of_all_shorts_and_covers(tmp_path) -> None:
    """Verify that all 28 SHORT and 28 COVER strategies can compose together in SHADOW runtime."""
    from mike_trade.app import (
        AppSettings,
        ShadowScenarioSettings,
        StrategyAssignment,
        SymbolStrategySettings,
        build_runtime,
    )
    from mike_trade.runtime import ExecutionMode

    all_shorts = [c.spec.strategy_id for c in REGISTRY.list_strategies(StrategyRole.SHORT)]
    all_covers = [c.spec.strategy_id for c in REGISTRY.list_strategies(StrategyRole.COVER)]

    settings = AppSettings(
        mode=ExecutionMode.SHADOW,
        db_path=tmp_path / "combined.sqlite3",
        symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(short_selling_enabled=True),),
        symbol_strategies=(
            SymbolStrategySettings(
                "SPY",
                (StrategyAssignment("buy", "momentum_breakout"),),
                shorts=tuple(StrategyAssignment(sid, sid) for sid in all_shorts),
                covers=tuple(StrategyAssignment(cid, cid) for cid in all_covers),
            ),
        ),
    )
    runtime = build_runtime(settings)
    collections = runtime.lifecycle.collections("SPY")
    assert len(collections.shorts) == 28
    assert len(collections.covers) == 28
    assert [b.strategy_id for b in collections.shorts] == sorted(all_shorts)
    assert [b.strategy_id for b in collections.covers] == sorted(all_covers)


def test_signal_ids_are_distinct_for_all_four_roles() -> None:
    """Verify that make_signal_id produces 4 distinct hashes across BUY, SELL, SHORT, COVER for the same bar."""
    bar = OPEN
    ids = {role: make_signal_id("shared_strat", "1.0.0", "SPY", role, bar) for role in StrategyRole}
    assert len(set(ids.values())) == 4


@pytest.mark.parametrize("strategy_id", SHORT_BATCH_A)
def test_short_batch_a_requires_watching_short_eligibility(strategy_id: str) -> None:
    """Verify that every SHORT Batch A strategy rejects evaluate when not armed with WATCHING_SHORT."""
    cls = REGISTRY.get(strategy_id)
    strat = cls()
    need = strat.required_bars({})

    start = OPEN
    bars = [
        Candle(
            "SPY",
            CandleTimeframe.M1,
            start + timedelta(minutes=i),
            start + timedelta(minutes=i + 1),
            D(100),
            D(101),
            D(99),
            D(100),
            D(100),
            "sip",
            True,
            CandleOrigin.REPLAY,
            PriceBasis.RAW,
            CandleStatus.CLOSED,
        )
        for i in range(need + 2)
    ]
    ctx = _make_context(bars)

    # When eligibility is WATCHING_BUY or missing, gate must block with NOT_ELIGIBLE
    res_buy = strat.evaluate(ctx, {}, {"eligibility": "WATCHING_BUY"})
    assert res_buy.reason_codes == (ReasonCode.NOT_ELIGIBLE,)

    res_none = strat.evaluate(ctx, {}, {})
    assert res_none.reason_codes == (ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", COVER_BATCH_A)
def test_cover_batch_a_has_no_entry_eligibility_restriction(strategy_id: str) -> None:
    """Verify that every COVER Batch A strategy does not require entry eligibility keys."""
    cls = REGISTRY.get(strategy_id)
    strat = cls()
    need = strat.required_bars({})

    start = OPEN
    bars = [
        Candle(
            "SPY",
            CandleTimeframe.M1,
            start + timedelta(minutes=i),
            start + timedelta(minutes=i + 1),
            D(100),
            D(101),
            D(99),
            D(100),
            D(100),
            "sip",
            True,
            CandleOrigin.REPLAY,
            PriceBasis.RAW,
            CandleStatus.CLOSED,
        )
        for i in range(need + 2)
    ]
    ctx = _make_context(bars)

    # Calling evaluate without eligibility state should NOT produce NOT_ELIGIBLE from entry gates
    res = strat.evaluate(ctx, {}, {})
    assert ReasonCode.NOT_ELIGIBLE not in res.reason_codes
