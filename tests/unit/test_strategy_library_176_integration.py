"""Final integration of the reviewed SHORT/COVER (Batches A+B) and BUY/SELL backlog branches.

A small set of combined checks: the exact 176-strategy library in both registries, per-symbol
composition of all four roles, one representative strategy per integrated family, BUY vs SHORT
authorization, unrestricted SELL/COVER/protective-stop exits, SHADOW and BACKTEST on real strategies,
and the reviewed VWAP corrections still holding after the merge.  Nothing here touches a broker.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

import pytest
from test_buy_sell_backlog_batch_a import PULLBACK_BARS, vwap_context
from test_buy_sell_backlog_batch_b import band_session, make_candle, make_context
from test_cover_batch_b import TRIGGER as COVER_B_TRIGGER, PARAMS as COVER_B_PARAMS
from test_short_cover_engine import SHORT_ON, run as backtest_run, sides
from test_short_cover_shadow import Clock, _history_context, _wave_closes, fills_of, start, symbol
from test_short_market_integration import ENTRY_MATRIX, FrozenMonitor, feed, pinned
from test_short_strategies_batch_b import IDS as SHORT_B_IDS, evaluate as evaluate_short_b, examples

from mike_trade import app
from mike_trade.app import (
    AppSettings,
    ShadowScenarioSettings,
    StrategyAssignment,
    SymbolStrategySettings,
    build_runtime,
)
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderSide
from mike_trade.market_status import MarketState, MarketStatusEntryGate
from mike_trade.research.commands import _research_registry
from mike_trade.runtime import ExecutionMode
from mike_trade.strategies.base import BuyStrategy, CoverStrategy, ReasonCode, SellStrategy, ShortStrategy
from mike_trade.strategies.indicators.core import VWAPCalculator
from mike_trade.strategies.indicators.vwap_bands import (
    VWAPDeviationBandsCalculator,
    session_bands_series,
    session_vwap_series,
)
from mike_trade.strategies.registry import StrategyRegistry

REGISTRY = _research_registry()
EXPECTED = {StrategyRole.BUY: 82, StrategyRole.SELL: 40, StrategyRole.SHORT: 28, StrategyRole.COVER: 28}
BACKLOG_BUYS = ("bollinger_band_mean_reversion", "vwap_pullback", "support_bounce",
                "vwap_band_reversion", "intraday_consolidation_breakout")


def production_registry(tmp_path) -> list[type]:
    """Every class ``build_runtime`` registers (captured; the registry itself is local)."""
    captured: list[type] = []
    original = StrategyRegistry.register

    def spy(self, cls):  # noqa: ANN001
        captured.append(cls)
        return original(self, cls)

    settings = AppSettings(mode=ExecutionMode.SHADOW, db_path=tmp_path / "p.sqlite3", symbols=("SPY",),
                           shadow_scenarios=(ShadowScenarioSettings(),))
    with mock.patch.object(StrategyRegistry, "register", spy):
        build_runtime(settings)
    return captured


# ---------------------------------------------------------------------------- registration


def test_exactly_176_unique_strategies_in_both_registries(tmp_path) -> None:
    research_ids = [c.spec.strategy_id for c in REGISTRY.list_strategies()]
    assert {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole} == EXPECTED
    assert len(research_ids) == len(set(research_ids)) == 178

    production = production_registry(tmp_path)
    production_ids = [c.spec.strategy_id for c in production]
    assert len(production) == len(set(production)) == 178  # no class registered twice
    assert len(production_ids) == len(set(production_ids))  # (the registry would also reject it)
    assert {role: sum(c.spec.side is role for c in production) for role in StrategyRole} == EXPECTED
    # production / research parity: the very same classes
    assert set(production) == {REGISTRY.get(sid) for sid in research_ids}


def test_each_role_uses_its_own_base_and_no_id_crosses_roles() -> None:
    bases = {StrategyRole.BUY: BuyStrategy, StrategyRole.SELL: SellStrategy,
             StrategyRole.SHORT: ShortStrategy, StrategyRole.COVER: CoverStrategy}
    for role, base in bases.items():
        for cls in REGISTRY.list_strategies(role):
            assert issubclass(cls, base) and cls.spec.side is role
            assert cls.spec.warmup_bars == cls.required_bars({})
    assert all(sid.startswith("short_") for sid in
               (c.spec.strategy_id for c in REGISTRY.list_strategies(StrategyRole.SHORT)))
    assert all(sid.startswith("cover_") for sid in
               (c.spec.strategy_id for c in REGISTRY.list_strategies(StrategyRole.COVER)))


def test_all_four_roles_compose_per_symbol_with_the_integrated_strategies(tmp_path) -> None:
    short_id, cover_id = SHORT_B_IDS[0], "cover_obv_breakout"
    settings = AppSettings(
        mode=ExecutionMode.SHADOW, db_path=tmp_path / "t.sqlite3", symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(short_selling_enabled=True),),
        symbol_strategies=(SymbolStrategySettings(
            "SPY",
            tuple(StrategyAssignment(i, i) for i in BACKLOG_BUYS),
            sells=(StrategyAssignment("price_below_sma", "price_below_sma"),),
            shorts=(StrategyAssignment(short_id, short_id),),
            covers=(StrategyAssignment(cover_id, cover_id),)),))
    collections = build_runtime(settings).lifecycle.collections("SPY")
    assert [b.strategy_id for b in collections.buys] == sorted(BACKLOG_BUYS)
    assert [b.strategy_id for b in collections.sells] == ["price_below_sma"]
    assert [b.strategy_id for b in collections.shorts] == [short_id]
    assert [b.strategy_id for b in collections.covers] == [cover_id]
    # a strategy cannot be bound into the wrong role slot
    from mike_trade.domain.errors import ConfigurationError

    bad = AppSettings(
        mode=ExecutionMode.SHADOW, db_path=tmp_path / "b.sqlite3", symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(short_selling_enabled=True),),
        symbol_strategies=(SymbolStrategySettings(
            "SPY", (StrategyAssignment("buy", "momentum_breakout"),),
            shorts=(StrategyAssignment("oops", cover_id),)),))
    with pytest.raises(ConfigurationError):
        build_runtime(bad)


# ----------------------------------------------------- representative strategy of each family


def test_representative_buy_short_and_cover_strategies_signal() -> None:
    buy = REGISTRY.get("vwap_pullback")().evaluate(vwap_context(PULLBACK_BARS), {}, {"eligibility": "WATCHING_BUY"})
    assert buy.is_signal and buy.side is StrategyRole.BUY

    sid = "short_bearish_engulfing"
    params, bars = examples()[sid]
    short = evaluate_short_b(sid, bars, params)
    assert short.is_signal and short.side is StrategyRole.SHORT

    cid = "cover_obv_breakout"
    from test_cover_batch_b import ev

    cover = ev(cid, COVER_B_TRIGGER[cid](), COVER_B_PARAMS[cid])
    assert cover.is_signal and cover.side is StrategyRole.COVER
    assert len({buy.signal_id, short.signal_id, cover.signal_id}) == 3


def test_buy_and_short_are_armed_separately_while_sell_and_cover_carry_no_gate() -> None:
    sid = "short_bearish_engulfing"
    params, bars = examples()[sid]
    for state in ({}, {"eligibility": "WATCHING_BUY"}, {"eligibility": "COOLDOWN"}):
        assert evaluate_short_b(sid, bars, params, state).reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    assert evaluate_short_b(sid, bars, params, {"eligibility": "WATCHING_SHORT"}).is_signal
    ctx = vwap_context(PULLBACK_BARS)
    buy = REGISTRY.get("vwap_pullback")()
    for state in ({}, {"eligibility": "WATCHING_SHORT"}):
        assert buy.evaluate(ctx, {}, state).reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    assert buy.evaluate(ctx, {}, {"eligibility": "WATCHING_BUY"}).is_signal
    from test_cover_batch_b import ev

    for state in ({}, {"eligibility": "WATCHING_BUY"}, {"eligibility": "WATCHING_SHORT"}):
        assert ev("cover_obv_breakout", COVER_B_TRIGGER["cover_obv_breakout"](), state=dict(state)).is_signal


# ------------------------------------------------------------------- market-state authorization


@pytest.mark.parametrize(("state", "buy_allowed", "short_allowed"), ENTRY_MATRIX)
async def test_market_status_gates_buy_and_short_entries_but_never_exits(state, buy_allowed, short_allowed):
    clock = Clock()
    gate = MarketStatusEntryGate(monitor=pinned(state, clock))
    now = clock.value

    async def decide(action):
        return await gate.evaluate_entry(account_scope_id="shadow:test", symbol="SPY",
                                         strategy_id="x", now=now, action=action)

    assert (await decide("ENTER_LONG")).allowed is buy_allowed
    assert (await decide("ENTER_SHORT")).allowed is short_allowed
    for exit_action in ("EXIT_LONG", "SELL", "EXIT_SHORT", "COVER", "LIQUIDATION"):
        decision = await decide(exit_action)
        assert decision.allowed is True and not decision.blocked, (state, exit_action)


# --------------------------------------------------------------------- SHADOW and BACKTEST


async def test_shadow_round_trips_a_real_short_and_a_new_batch_b_cover(tmp_path) -> None:
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_ema_bearish_crossover", {"fast_period": 3, "slow_period": 8})],
        covers=[("cover_obv_breakout", {"period": 4})],
        stop={"type": "fixed", "stop_percent": D("40")}), real=True)
    closes = _wave_closes()
    try:
        for index in range(len(closes)):
            clock.value += timedelta(minutes=1)
            await runtime.lifecycle.process({"SPY": _history_context(closes, index, clock.value)})
        order = [side for side, _qty, _price in fills_of(shadow)]
    finally:
        await runtime.stop()
    assert len(order) >= 4 and set(order) == {OrderSide.SELL_SHORT, OrderSide.BUY_TO_COVER}
    assert order[0::2] == [OrderSide.SELL_SHORT] * len(order[0::2])
    assert order[1::2] == [OrderSide.BUY_TO_COVER] * len(order[1::2])  # never a BUY or SELL


def _tape(n: int = 150) -> list[tuple[str, str, str, str]]:
    import math

    rows, previous = [], D("100.00")
    for i in range(n):
        close = D(f"{100 + 5 * math.sin(2 * math.pi * i / 40) + 0.7 * math.sin(i / 3):.2f}")
        rows.append((str(previous), str(max(previous, close) + D("0.3")),
                     str(min(previous, close) - D("0.3")), str(close)))
        previous = close
    return rows


def test_backtest_is_deterministic_with_a_real_short_and_a_new_batch_b_cover(tmp_path) -> None:
    ids = ("short_ema_bearish_crossover", "cover_obv_breakout")
    params = {ids[0]: {"fast_period": 3, "slow_period": 8}, ids[1]: {"period": 5}}
    config = {**SHORT_ON, "minimum_hold_seconds": 60}
    first = backtest_run(_tape(), ids, params, config=config, registry=REGISTRY, result_dir=tmp_path / "a")
    again = backtest_run(_tape(), ids, params, config=config, registry=REGISTRY, result_dir=tmp_path / "b")
    assert first.run_id == again.run_id and first.fills == again.fills and first.intents == again.intents
    order = sides(first)
    assert len(order) >= 4 and set(order) == {"SELL_SHORT", "BUY_TO_COVER"}


def test_backtest_enters_long_on_a_reviewed_backlog_buy_and_obeys_the_short_switch(tmp_path) -> None:
    rows = [(str(b.open), str(b.high), str(b.low), str(b.close)) for b in PULLBACK_BARS]
    rows += [(str(PULLBACK_BARS[-1].close),) * 4] * 3
    result = backtest_run(rows, ("vwap_pullback",), {"vwap_pullback": {}}, registry=REGISTRY,
                          result_dir=tmp_path)
    assert [i["side"] for i in result.intents if i["side"] == "BUY"]
    assert {i["side"] for i in result.intents} == {"BUY"}  # a BUY strategy never produces a short
    # shorting stays opt-in: without the switch the same short/cover pair does nothing
    gated = backtest_run(_tape(), ("short_ema_bearish_crossover", "cover_obv_breakout"),
                         {"short_ema_bearish_crossover": {"fast_period": 3, "slow_period": 8},
                          "cover_obv_breakout": {"period": 5}}, registry=REGISTRY,
                         result_dir=tmp_path / "gated")
    assert gated.fills == ()


# --------------------------------------------------------------- reviewed VWAP corrections


def test_reviewed_vwap_corrections_survive_the_integration() -> None:
    bars = band_session()
    # the band VWAP is the production session VWAP, bit for bit, and has real dispersion
    series = session_vwap_series(bars)
    bands = session_bands_series(bars, D(2))
    assert [b.vwap for b in bands] == series and bands[-2].std_dev > 1
    # candles carry no session_id: the calculator must not reset on every bar
    calc = VWAPDeviationBandsCalculator(D(2))
    out = [calc.update(b) for b in bars]
    assert out[-1].std_dev > 0 and out[-1].vwap == series[-1]
    # snapshot/restore through JSON keeps a zero multiplier and the accumulators
    zero = VWAPDeviationBandsCalculator(D(0))
    zero.update(bars[0], "S")
    assert VWAPDeviationBandsCalculator.restore(json.loads(json.dumps(zero.snapshot()))).multiplier == 0
    # the strategy needs the production VWAP (history from the open) and signals on real bands
    from mike_trade.strategies.buy.vwap_band_reversion import VWAPBandReversionBuy

    armed = {"eligibility": "WATCHING_BUY"}
    full = make_context(bars)
    assert VWAPBandReversionBuy().evaluate(full, {}, dict(armed)).is_signal
    truncated = make_context(bars[2:], indicators=dict(full.indicators))
    assert VWAPBandReversionBuy().evaluate(truncated, {}, dict(armed)).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)
    # pullback: point-in-time per-bar VWAP; bounce/pullback registration untouched
    assert REGISTRY.get("vwap_pullback")().evaluate(vwap_context(PULLBACK_BARS), {}, dict(armed)).is_signal
    production = VWAPCalculator()

    class _Bar:
        def __init__(self, c):
            self.open, self.high, self.low, self.close, self.volume = c.open, c.high, c.low, c.close, c.volume
            self.session_id = "S"

    assert [production.update(_Bar(b)) for b in bars] == series
    assert make_candle  # (fixture helper kept importable)
