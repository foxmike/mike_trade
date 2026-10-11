"""Regression tests from the independent review of BUY/SELL backlog batches A + B.

Each test either pins a defect that was found and corrected (the scenario is built so that
the pre-review behaviour gives a different answer, and says so) or verifies a mathematical or
point-in-time property that the original tests did not exercise.
"""

from __future__ import annotations

import inspect
import json
import random
from datetime import timedelta
from decimal import Decimal

import pytest
from test_buy_sell_backlog_batch_a import PULLBACK_BARS, vwap_context
from test_buy_sell_backlog_batch_b import OPEN_UTC, band_session, make_candle, make_context
from test_short_cover_engine import OPEN as ENGINE_OPEN
from test_short_cover_engine import run as backtest_run

from mike_trade import app
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle
from mike_trade.research.commands import _research_registry
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.indicators.core import VWAPCalculator, sma
from mike_trade.strategies.indicators.finite import bollinger
from mike_trade.strategies.indicators.vwap_bands import (
    VWAPDeviationBandsCalculator,
    session_bands_series,
    session_key,
    session_vwap_series,
    vwap_matches,
)

D = Decimal
YESTERDAY = OPEN_UTC - timedelta(days=1)
REGISTRY = _research_registry()
ARMED = {"eligibility": "WATCHING_BUY"}
FIVE = (
    "bollinger_band_mean_reversion",
    "vwap_pullback",
    "support_bounce",
    "vwap_band_reversion",
    "intraday_consolidation_breakout",
)


def evaluate(strategy_id: str, ctx, params=None, state=None):
    return REGISTRY.get(strategy_id)().evaluate(
        ctx, {} if params is None else params, dict(ARMED if state is None else state))


def bar(i: int, low: object, high: object, close: object, *, volume: object = 100,
        base=None) -> Candle:
    kw = {} if base is None else {"base_time": base}
    return make_candle(i, close, high, low, close, volume=volume, **kw)


# ============================================================ integration and registration


def test_the_five_strategies_are_registered_once_in_production_and_research() -> None:
    production = inspect.getsource(app.build_runtime)
    research = inspect.getsource(inspect.getmodule(_research_registry)._research_registry)  # type: ignore[union-attr]
    ids = [c.spec.strategy_id for c in REGISTRY.list_strategies()]
    assert len(ids) == len(set(ids))
    for strategy_id in FIVE:
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side is StrategyRole.BUY and cls.spec.version == "1.0.0"
        # production lists the class once in build_runtime; the research function lists it in its
        # (possibly parenthesised) import and once in the registration tuple, and the registry
        # itself rejects a duplicate (strategy_id, version)
        assert production.count(f"{cls.__name__},") == 1
        assert 1 <= research.count(f"{cls.__name__},") <= 2 and cls.__name__ in research
        assert cls.spec.warmup_bars == cls.required_bars({})
        assert cls.spec.parameter_schema.validate({}) is not None
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {StrategyRole.BUY: 83, StrategyRole.SELL: 40, StrategyRole.SHORT: 28,
                      StrategyRole.COVER: 28}
    assert len(REGISTRY.list_strategies()) == 179


def test_the_five_strategies_compose_into_a_shadow_runtime_as_per_symbol_buys(tmp_path) -> None:
    from mike_trade.app import (
        AppSettings,
        ShadowScenarioSettings,
        StrategyAssignment,
        SymbolStrategySettings,
        build_runtime,
    )
    from mike_trade.runtime import ExecutionMode

    settings = AppSettings(
        mode=ExecutionMode.SHADOW, db_path=tmp_path / "t.sqlite3", symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(),),
        symbol_strategies=(SymbolStrategySettings(
            "SPY", tuple(StrategyAssignment(i, i) for i in FIVE)),))
    buys = build_runtime(settings).lifecycle.collections("SPY").buys
    assert [b.strategy_id for b in buys] == sorted(FIVE)
    assert {b.role for b in buys} == {"buy"}


@pytest.mark.parametrize("strategy_id", FIVE)
def test_the_strategies_only_enter_when_the_engine_arms_them(strategy_id: str) -> None:
    ctx = make_context(ic_base(60))
    for state in ({}, {"eligibility": "WATCHING_SHORT"}, {"eligibility": "COOLDOWN"}):
        assert evaluate(strategy_id, ctx, state=state).reason_codes == (ReasonCode.NOT_ELIGIBLE,)


# =========================================================== VWAP bands: the mathematics


def test_defect_the_band_calculator_must_not_reset_on_every_bar_without_a_session_id() -> None:
    """Candles carry no ``session_id``; the calculator used to reset on each bar, collapsing the
    bands onto the bar's own typical price (std 0), so no real VWAP band ever existed."""
    bars = [bar(0, 99, 101, 100), bar(1, 101, 103, 102, volume=300), bar(2, 100, 104, 101, volume=200)]
    calc = VWAPDeviationBandsCalculator(D(2))
    out = [calc.update(b) for b in bars]
    assert out[1].vwap == (D(100) * 100 + D(102) * 300) / 400
    assert out[2].vwap == (D(100) * 100 + D(102) * 300 + (D(104) + 100 + 101) / 3 * 200) / 600
    assert out[2].std_dev > 0 and out[2].lower < out[2].vwap < out[2].upper
    # and equals the explicit-session-id path
    explicit = VWAPDeviationBandsCalculator(D(2))
    assert [explicit.update(b, "S") for b in bars] == out


def test_the_band_vwap_is_bit_identical_to_the_production_session_vwap() -> None:
    rng = random.Random(5)
    bars = []
    for i in range(80):
        close = D(f"{100 + rng.uniform(-3, 3):.2f}")
        bars.append(bar(i, close - D("0.4"), close + D("0.6"), close,
                        volume=0 if i % 17 == 3 else rng.randint(1, 900)))
    production = VWAPCalculator()
    class _S:
        def __init__(self, c): self.open, self.high, self.low = c.open, c.high, c.low; self.close, self.volume = c.close, c.volume; self.session_id = "S"  # noqa: E702
    expected = [production.update(_S(b)) for b in bars]
    assert session_vwap_series(bars) == expected
    got = session_bands_series(bars, D(2))
    assert [None if g is None else g.vwap for g in got] == expected  # same price basis, same volume gating


def test_the_band_standard_deviation_is_the_volume_weighted_deviation_of_typical_price() -> None:
    rng = random.Random(11)
    bars = [bar(i, (c := D(f"{100 + rng.uniform(-4, 4):.2f}")) - D("0.5"), c + D("0.7"), c,
                volume=rng.randint(1, 700)) for i in range(40)]
    result = session_bands_series(bars, D("2.5"))[-1]
    tps = [((b.high + b.low + b.close) / 3, b.volume) for b in bars]
    total = sum(v for _, v in tps)
    vwap = sum(tp * v for tp, v in tps) / total
    variance = sum(v * (tp - vwap) ** 2 for tp, v in tps) / total  # direct definition
    assert abs(result.vwap - vwap) < D("1e-20")
    assert abs(result.std_dev - variance.sqrt()) < D("1e-18")
    assert abs(result.lower - (vwap - D("2.5") * variance.sqrt())) < D("1e-18")
    assert abs(result.upper - (vwap + D("2.5") * variance.sqrt())) < D("1e-18")


def test_a_zero_volume_bar_changes_no_accumulator_and_the_variance_is_never_negative() -> None:
    calc = VWAPDeviationBandsCalculator(D(2))
    first = calc.update(bar(0, 99, 101, 100), "S")
    assert calc.update(bar(1, 50, 150, 70, volume=0), "S") == first
    flat = VWAPDeviationBandsCalculator(D(2))
    for i in range(30):
        out = flat.update(bar(i, D("100.1"), D("100.1"), D("100.1")), "S")
        assert out.std_dev >= 0 and out.lower <= out.vwap <= out.upper


def test_the_session_series_resets_per_new_york_session_and_is_point_in_time() -> None:
    day1 = [bar(i, 99 + i, 101 + i, 100 + i, base=YESTERDAY) for i in range(6)]
    day2 = [bar(i, 199 - i, 201 - i, 200 - i) for i in range(6)]
    series = session_vwap_series(day1 + day2)
    assert session_key(day1[0]) != session_key(day2[0])
    assert series[6:] == session_vwap_series(day2)  # day 2 starts afresh; yesterday leaks nothing
    # point in time: entry i depends only on candles 0..i
    for i in range(len(day2)):
        assert session_vwap_series(day2[: i + 1])[-1] == series[6 + i]
    bands = session_bands_series(day1 + day2, D(2))
    assert bands[6:] == session_bands_series(day2, D(2))
    for i in range(1, len(day2)):  # the band at t-1 is unchanged by bar t
        assert session_bands_series(day2[:i], D(2))[-1] == bands[6 + i - 1]


def test_snapshot_restore_reproduces_uninterrupted_evaluation_through_json() -> None:
    rng = random.Random(3)
    bars = [bar(i, (c := D(f"{100 + rng.uniform(-3, 3):.2f}")) - D("0.3"), c + D("0.4"), c,
                volume=rng.randint(0, 400)) for i in range(70)]
    whole = VWAPDeviationBandsCalculator(D("1.5"))
    expected = [whole.update(b, "S1") for b in bars]
    for cut in (1, 9, 33, 69):
        first = VWAPDeviationBandsCalculator(D("1.5"))
        for b in bars[:cut]:
            first.update(b, "S1")
        restored = VWAPDeviationBandsCalculator.restore(json.loads(json.dumps(first.snapshot())))
        assert [restored.update(b, "S1") for b in bars[cut:]] == expected[cut:]


def test_defect_restore_preserves_a_zero_multiplier_and_rejects_malformed_snapshots() -> None:
    calc = VWAPDeviationBandsCalculator(D(0))  # zero is valid: the bands collapse onto VWAP
    calc.update(bar(0, 99, 101, 100), "S")
    restored = VWAPDeviationBandsCalculator.restore(calc.snapshot())
    assert restored.multiplier == 0  # was silently replaced by 2.0
    good = calc.snapshot()
    for broken in ({**good, "cum_volume": "-1"}, {k: v for k, v in good.items() if k != "multiplier"},
                   {**good, "session_id": 7}, {**good, "cum_weighted_price": None}):
        with pytest.raises((ValueError, KeyError, TypeError)):
            VWAPDeviationBandsCalculator.restore(broken)
    non_str = VWAPDeviationBandsCalculator(D(2))
    non_str.update(bar(0, 99, 101, 100), 20261008)
    with pytest.raises(TypeError):  # a non-str id would come back as str and cause a false reset
        non_str.snapshot()


def test_vwap_matches_requires_agreement_with_the_shared_indicator() -> None:
    assert vwap_matches(D("100.0000000001"), D(100)) and not vwap_matches(D("100.1"), D(100))
    for bad in (None, 0, D(0), D("-1"), "100"):
        assert not vwap_matches(D(100), bad)
    assert not vwap_matches(None, D(100))


# =========================================================== VWAP band reversion strategy


def test_defect_band_reversion_no_longer_trades_on_a_bar_closing_below_its_own_typical_price() -> None:
    """With the degenerate bands the rule reduced to ``close[t-1] < TP[t-1]`` and ``close[t] >
    TP[t]``: any pair of candles with a weak then a strong close. Here the prior close is far
    inside the real lower band, so nothing may fire."""
    base = band_session()[:8]  # real dispersion: the lower band sits several points below 99.5
    weak = make_candle(8, "100.2", "102", "99", "99.5", volume=100)   # close below its own typical price
    strong = make_candle(9, "99.6", "101", "99.5", "100.8", volume=100)  # close above its own typical price
    bars = base + [weak, strong]
    degenerate = [(b.high + b.low + b.close) / 3 for b in bars]
    assert bars[-2].close < degenerate[-2] and bars[-1].close > degenerate[-1]  # old rule: signal
    bands = session_bands_series(bars, D(2))
    assert bars[-2].close > bands[-2].lower  # real band: not an excursion at all
    result = evaluate("vwap_band_reversion", make_context(bars))
    assert not result.is_signal and result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_band_reversion_signal_is_an_actual_excursion_and_re_entry_edge() -> None:
    bars = band_session()
    bands = session_bands_series(bars, D(2))
    assert bars[-2].close < bands[-2].lower and bars[-1].close > bands[-1].lower
    assert evaluate("vwap_band_reversion", make_context(bars)).is_signal
    # still below the lower band on the latest bar: no re-entry
    deeper = bars[:-1] + [make_candle(9, 92, 93, 91, "91.5", volume=100)]
    assert not evaluate("vwap_band_reversion", make_context(deeper)).is_signal
    # a bar that stays inside the band after an inside close: a level, not an edge
    inside = bars + [make_candle(10, 97, 99, 96.5, 98.5, volume=100)]
    assert not evaluate("vwap_band_reversion", make_context(inside)).is_signal
    # bar t does not alter the band used for bar t-1
    prior_band = session_bands_series(bars[:-1], D(2))[-1]
    assert prior_band == bands[-2]


def test_band_reversion_fails_closed_when_the_history_does_not_reach_the_session_open() -> None:
    bars = band_session()
    full = make_context(bars)  # indicators = the production VWAP of the whole session
    assert evaluate("vwap_band_reversion", full).is_signal
    truncated = make_context(bars[2:], indicators=dict(full.indicators))  # history starts mid-session
    anchored = session_bands_series(bars[2:], D(2))[-1].vwap
    assert not vwap_matches(anchored, full.indicators["vwap"])
    assert evaluate("vwap_band_reversion", truncated).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    for missing in ({}, {"vwap": full.indicators["vwap"]}, {"vwap": None, "vwap_prev": None}):
        assert evaluate("vwap_band_reversion", make_context(bars, indicators=missing)).reason_codes == (
            ReasonCode.INSUFFICIENT_DATA,)


def test_band_reversion_session_isolation_missing_bars_and_replay() -> None:
    bars = band_session()
    yesterday = [bar(i, 150, 151, 150.5, base=YESTERDAY) for i in range(3)]
    # yesterday's bars never enter today's bands; the indicators are today's own
    ctx = make_context(yesterday + bars, indicators=dict(make_context(bars).indicators))
    assert evaluate("vwap_band_reversion", ctx).is_signal
    gap = bars[:-1] + [make_candle(10, 94, 98, 93, 97, volume=100)]  # minute 9 missing
    assert evaluate("vwap_band_reversion", make_context(gap)).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    zero = bars[:-1] + [make_candle(9, 94, 98, 93, 97, volume=0)]
    assert evaluate("vwap_band_reversion", make_context(zero)).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    first = evaluate("vwap_band_reversion", make_context(bars))
    replay = evaluate("vwap_band_reversion", make_context(bars), state={**ARMED, **first.state_updates})
    assert first.is_signal and not replay.is_signal
    # the multiplier changes the band, not the session accumulators
    narrow = evaluate("vwap_band_reversion", make_context(bars), {"multiplier": D("0.5")})
    wide = evaluate("vwap_band_reversion", make_context(bars), {"multiplier": D("4.0")})
    assert narrow.reason_codes in ((ReasonCode.SIGNAL,), (ReasonCode.NO_SIGNAL,))
    assert wide.reason_codes in ((ReasonCode.SIGNAL,), (ReasonCode.NO_SIGNAL,))
    with pytest.raises(ParameterValidationError):
        evaluate("vwap_band_reversion", make_context(bars), {"min_session_bars": 61})


# ===================================================================== VWAP pullback


def test_defect_each_trend_bar_is_compared_with_its_own_vwap_not_the_setup_bars() -> None:
    """The original rule compared every trend bar with ``vwap_prev`` (the setup bar's VWAP, a
    later point in time).  In a falling session VWAP drifts down, so that accepted a trend bar
    whose low was never above the VWAP that existed when it traded."""
    bars = [
        make_candle(0, "110.2", "111", "109.5", "110.5"),
        make_candle(1, "108", "109.5", "105", "109.2"),
        make_candle(2, "109.3", "111", "109.2", "110.6"),
    ]
    series = session_vwap_series(bars)
    ctx = vwap_context(bars)
    tol = D("0.1") / 100
    # the pre-review rule, reproduced: trend bar vs the SETUP bar's VWAP
    old_trend = bars[0].low > series[1] * (1 + tol) and bars[0].close >= series[1]
    old_pullback = bars[1].low <= series[1] * (1 + tol) and bars[1].close >= series[1]
    old_confirm = bars[2].close > bars[1].high and bars[2].close > series[2]
    assert old_trend and old_pullback and old_confirm  # the old code signalled here
    assert not bars[0].low > series[0] * (1 + tol)      # but bar 0 never traded above its own VWAP zone
    result = evaluate("vwap_pullback", ctx, {"trend_bars": 1})
    assert not result.is_signal and result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_pullback_signals_when_every_bar_satisfies_its_own_point_in_time_vwap() -> None:
    series = session_vwap_series(PULLBACK_BARS)
    zone = 1 + D("0.1") / 100
    trend = PULLBACK_BARS[:3]
    assert all(b.close >= v for b, v in zip(trend, series[:3], strict=True))
    assert trend[-1].low > series[2] * zone
    assert PULLBACK_BARS[3].low <= series[3] * zone and PULLBACK_BARS[3].close >= series[3]
    assert PULLBACK_BARS[4].close > PULLBACK_BARS[3].high and PULLBACK_BARS[4].close > series[4]
    assert evaluate("vwap_pullback", vwap_context(PULLBACK_BARS)).is_signal


@pytest.mark.parametrize("break_bar", (0, 1, 2))
def test_pullback_rejects_a_trend_bar_that_closed_below_its_own_vwap(break_bar: int) -> None:
    bars = list(PULLBACK_BARS)
    b = bars[break_bar]
    # close well below the typical price puts the bar's close under its own running VWAP
    bars[break_bar] = make_candle(break_bar, b.open, b.high, b.low - D("1"), b.low - D("1"))
    assert not evaluate("vwap_pullback", vwap_context(bars)).is_signal


def pullback_oracle(bars: list[Candle], trend_bars: int, tolerance_pct: Decimal) -> bool:
    """The documented rule, written independently from the session VWAP series."""
    series = session_vwap_series(bars)
    zone = 1 + tolerance_pct / 100
    n = trend_bars + 2
    trend, setup, latest = bars[-n:-2], bars[-2], bars[-1]
    v = series[-n:-2]
    return (
        all(b.close >= vk for b, vk in zip(trend, v, strict=True))
        and trend[-1].low > v[-1] * zone
        and setup.low <= series[-2] * zone and setup.close >= series[-2]
        and latest.close > setup.high and latest.close > series[-1]
    )


def test_pullback_matches_an_independent_point_in_time_oracle_on_random_sessions() -> None:
    rng = random.Random(4)
    signals = 0
    for _ in range(4000):
        price, bars = D(100), []
        for i in range(7):
            price += D(f"{rng.uniform(-0.25, 0.55):.2f}")
            low = price - D(f"{rng.uniform(0.0, 0.5):.2f}")
            high = price + D(f"{rng.uniform(0.0, 0.5):.2f}")
            bars.append(make_candle(i, price, high, low, price, volume=rng.choice((100, 100, 250, 40))))
        for trend_bars, tol in ((2, D("0.1")), (1, D("0.0")), (3, D("0.3"))):
            expected = pullback_oracle(bars, trend_bars, tol)
            got = evaluate("vwap_pullback", vwap_context(bars),
                           {"trend_bars": trend_bars, "tolerance_pct": tol}).is_signal
            assert got == expected, (trend_bars, tol)
            signals += expected
    assert signals >= 20, "the random sessions must exercise the signal path"


def test_pullback_needs_the_session_open_in_the_history_and_never_mixes_sessions() -> None:
    full = vwap_context(PULLBACK_BARS)
    assert evaluate("vwap_pullback", full).is_signal
    # a context whose candles start after the open cannot reproduce the production VWAP
    ctx = make_context(PULLBACK_BARS[1:], indicators=dict(full.indicators))
    assert evaluate("vwap_pullback", ctx, {"trend_bars": 2}).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    # yesterday's bars never contribute to today's VWAP or window
    y = [make_candle(i, "300", "301", "299", "300.6", base_time=YESTERDAY)
         for i in range(5)]
    both = make_context(y + PULLBACK_BARS, indicators=dict(full.indicators))
    assert evaluate("vwap_pullback", both).is_signal


def test_pullback_missing_zero_volume_and_replay_guards() -> None:
    bars = list(PULLBACK_BARS)
    gap = [*bars[:4], make_candle(5, "103", "106", "102.9", "105.5")]
    assert evaluate("vwap_pullback", make_context(gap, indicators=dict(vwap_context(bars).indicators))
                    ).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    zero = [*bars[:2], make_candle(2, "102.8", "105", "102.5", "104.8", volume=0), *bars[3:]]
    assert evaluate("vwap_pullback", vwap_context(zero)).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    first = evaluate("vwap_pullback", vwap_context(bars))
    replay = evaluate("vwap_pullback", vwap_context(bars), state={**ARMED, **first.state_updates})
    assert first.is_signal and not replay.is_signal
    for bad in ({"trend_bars": 0}, {"trend_bars": 21}, {"tolerance_pct": D("-0.1")},
                {"tolerance_pct": D("2.1")}):
        with pytest.raises(ParameterValidationError):
            evaluate("vwap_pullback", vwap_context(bars), bad)


def test_pullback_and_bounce_overlap_only_on_an_exact_vwap_close() -> None:
    from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy

    # vwap_bounce: prior.close <= vwap_prev and latest.close > vwap.  The pullback's setup holds
    # VWAP (close >= vwap), so the two can both be true only when the setup close == vwap_prev.
    assert not VwapBounceBuy().evaluate(vwap_context(PULLBACK_BARS), {}, dict(ARMED)).is_signal
    ctx = make_context(PULLBACK_BARS, indicators={"vwap": D(100), "vwap_prev": PULLBACK_BARS[3].close})
    assert VwapBounceBuy().evaluate(ctx, {}, dict(ARMED)).is_signal  # equality is on the bounce side


# ===================================================================== support bounce


def sb_bars(lows: list[object], *, close_offset: str = "0.5", start: int = 0) -> list[Candle]:
    return [bar(start + i, low, D(str(low)) + 1, D(str(low)) + D(close_offset)) for i, low in enumerate(lows)]


SB = {"strength": 1, "lookback": 10, "tolerance_pct": D("0.2")}
#: pivot low 12 at index 3 (strictly below both neighbours); confirmed long before the setup bar
SB_HISTORY = [15, 14, 13, 12, 13, 14, 15, 16, 17, 18]


def sb_case(setup_low: object = "12.01", signal_close: object = "13.5") -> list[Candle]:
    return sb_bars(SB_HISTORY) + [bar(10, setup_low, 13, "12.5"), bar(11, "12.6", 14, signal_close)]


def test_support_bounce_signal_and_boundaries() -> None:
    assert evaluate("support_bounce", make_context(sb_case()), SB).is_signal
    s = D(12)
    upper, lower = s * (1 + D("0.2") / 100), s * (1 - D("0.2") / 100)
    assert evaluate("support_bounce", make_context(sb_case(upper)), SB).is_signal       # touch at the edge
    assert not evaluate("support_bounce", make_context(sb_case(upper + D("0.001"))), SB).is_signal
    assert evaluate("support_bounce", make_context(sb_case(lower)), SB).is_signal        # held at the floor
    assert not evaluate("support_bounce", make_context(sb_case(lower - D("0.001"))), SB).is_signal
    # confirmation must be strictly above the setup high (13) and above support
    assert not evaluate("support_bounce", make_context(sb_case(signal_close="13")), SB).is_signal
    assert evaluate("support_bounce", make_context(sb_case(signal_close="13.01")), SB).is_signal


def test_support_pivot_is_unavailable_until_its_right_hand_bars_have_completed() -> None:
    """Strictly falling lows have no confirmed pivot; the lowest bar becomes a pivot only if the
    setup bar is allowed to act as its right-hand neighbour - which the strategy must not do."""
    falling = sb_bars([25, 24, 23, 22, 21, 20, 19, 18, 17, 16])
    setup = bar(10, "16.02", 17, "16.5")
    confirm = bar(11, "16.6", 18, "17.5")
    bars = falling + [setup, confirm]
    # with the setup bar as the right neighbour a pivot low (16) WOULD exist in the full window
    from mike_trade.strategies.indicators.windows import swing_pivots

    assert [p for p in swing_pivots(bars[:-1], 1)[1] if p[0] == 9]  # look-ahead pivot exists
    assert [p for p in swing_pivots(bars[:-2], 1)[1] if p[0] == 9] == []  # not at the setup bar
    result = evaluate("support_bounce", make_context(bars), SB)
    assert not result.is_signal and result.reason_codes == (ReasonCode.NO_SIGNAL,)
    # positive control: one rising bar after the minimum completes the pivot, then the same test signals
    confirmed = falling + [bar(10, 17, 18, "17.5"), bar(11, "16.02", 17, "16.5"), bar(12, "16.6", 18, "17.8")]
    assert evaluate("support_bounce", make_context(confirmed), {**SB, "lookback": 11}).is_signal


def test_the_confirmation_candle_cannot_complete_the_support_pivot() -> None:
    # lows fall to a minimum at the setup bar; the signal bar would be its right neighbour
    bars = sb_bars([25, 24, 23, 22, 21, 20, 19, 18, 17, 16]) + [bar(10, "15.5", 16.5, "16"), bar(11, "16", 18, "17.5")]
    assert not evaluate("support_bounce", make_context(bars), SB).is_signal


def test_defect_support_broken_by_an_unconfirmed_lower_low_is_no_longer_support() -> None:
    """A bar after the pivot traded through support but is too recent to be a confirmed pivot, so
    the old code kept using the broken level and 'bounced' off it."""
    broken = sb_bars(SB_HISTORY[:-1]) + [bar(9, "11", 12, "11.5")]  # last history bar trades below 12
    bars = broken + [bar(10, "12.01", 13, "12.5"), bar(11, "12.6", 14, "13.5")]
    from mike_trade.strategies.indicators.windows import swing_pivots

    pivots = swing_pivots(bars[:-2], 1)[1]
    assert pivots[-1] == (3, D(12))  # the stale pivot is still the latest confirmed one
    result = evaluate("support_bounce", make_context(bars), SB)
    assert not result.is_signal  # the pre-review code signalled here
    # the same level with no break in between does signal (the control for the scenario)
    assert evaluate("support_bounce", make_context(sb_case()), SB).is_signal


def test_defect_support_across_a_missing_bar_or_session_gap_is_not_used() -> None:
    bars = sb_case()
    holed = bars[:6] + [Candle(b.symbol, b.timeframe, b.start + timedelta(minutes=1), b.end + timedelta(minutes=1),
                               b.open, b.high, b.low, b.close, b.volume, b.feed_id, b.consolidated,
                               b.origin, b.price_basis, b.status) for b in bars[6:]]
    assert evaluate("support_bounce", make_context(holed), SB).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    # a pivot from the previous session, then the overnight gap, then today's test
    yesterday = [bar(i, low, D(str(low)) + 1, D(str(low)) + D("0.5"), base=YESTERDAY)
                 for i, low in enumerate(SB_HISTORY)]
    today = [bar(0, "12.01", 13, "12.5"), bar(1, "12.6", 14, "13.5")]
    assert evaluate("support_bounce", make_context(yesterday + today), SB).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)


def test_support_bounce_uses_the_most_recent_confirmed_pivot_and_stays_distinct() -> None:
    # two confirmed pivots, 12 (older) and 14 (latest); a test of 12 must not signal
    lows = [16, 15, 14, 13, 12, 13, 14, 15, 14.5, 14.0, 14.5, 15]
    older = sb_bars(lows) + [bar(12, "12.01", 13, "12.5"), bar(13, "12.6", 15, "13.9")]
    assert not evaluate("support_bounce", make_context(older), {**SB, "lookback": 12}).is_signal
    latest = sb_bars(lows) + [bar(12, "14.01", 15, "14.5"), bar(13, "14.6", 16, "15.6")]
    assert evaluate("support_bounce", make_context(latest), {**SB, "lookback": 12}).is_signal
    # a single-pivot bounce is not a double bottom
    assert not evaluate("double_bottom_reversal", make_context(sb_case()), {"strength": 1, "lookback": 20}).is_signal


# ============================================================ intraday consolidation breakout


def ic_base(n: int = 17) -> list[Candle]:
    return [make_candle(i, "100.10", "100.40", "100.00", "100.20") for i in range(n)]


def test_consolidation_breakout_candle_is_excluded_and_a_narrow_range_must_pre_exist() -> None:
    bars = ic_base() + [make_candle(17, "100.35", "103.00", "100.30", "100.65")]  # huge-range breakout bar
    assert evaluate("intraday_consolidation_breakout", make_context(bars)).is_signal
    # the same base, if the breakout bar defined the range, would be far too wide (2.7%)
    window = bars[-16:]
    assert (max(b.high for b in window) - min(b.low for b in window)) / min(b.low for b in window) * 100 > D("0.75")
    # a wide base is not a consolidation even though the close breaks its high
    wide = [make_candle(i, "100.5", "101.5", "100.00", "100.8") for i in range(17)] + [
        make_candle(17, "101.4", "102", "101.3", "101.8")]
    assert not evaluate("intraday_consolidation_breakout", make_context(wide)).is_signal


def test_consolidation_range_and_breakout_boundaries() -> None:
    def case(ceiling: str, breakout_close: str):
        base = [make_candle(i, "100.10", ceiling, "100.00", "100.20") for i in range(17)]
        return base + [make_candle(17, "100.3", "101.5", "100.2", breakout_close)]

    # range exactly 0.75% of the floor qualifies; a hair over does not
    assert evaluate("intraday_consolidation_breakout", make_context(case("100.75", "100.80"))).is_signal
    assert not evaluate("intraday_consolidation_breakout", make_context(case("100.76", "100.80"))).is_signal
    # close exactly at the ceiling is not above it
    assert not evaluate("intraday_consolidation_breakout", make_context(case("100.40", "100.40"))).is_signal
    assert evaluate("intraday_consolidation_breakout", make_context(case("100.40", "100.41"))).is_signal
    with pytest.raises(ParameterValidationError):
        evaluate("intraday_consolidation_breakout", make_context(case("100.4", "100.5")),
                 {"max_range_pct": D("0.09")})


def test_defect_a_steadily_rising_price_must_not_signal_on_every_rolling_window() -> None:
    """In a slow, steady advance each close exceeds the previous N highs while the N-bar range
    stays inside the compression limit, so the old rule fired on every bar (its 'prior close <=
    ceiling' test compared a bar with a ceiling that includes its own high and is always true)."""
    bars = [make_candle(i, 100 + D("0.02") * i, 100 + D("0.02") * i + D("0.01"),
                        100 + D("0.02") * i - D("0.01"), 100 + D("0.02") * i + D("0.005"))
            for i in range(60)]
    n, limit = 15, D("0.75")

    def old_rule(window: list[Candle]) -> bool:
        base, last = window[-n - 1:-1], window[-1]
        ceiling, floor = max(b.high for b in base), min(b.low for b in base)
        return (ceiling - floor) / floor * 100 <= limit and base[-1].close <= ceiling and last.close > ceiling

    old = sum(old_rule(bars[: i + 1]) for i in range(n, len(bars)))
    state: dict[str, object] = {}
    new = 0
    for i in range(n + 1, len(bars)):
        result = evaluate("intraday_consolidation_breakout", make_context(bars[: i + 1]), state={**ARMED, **state})
        state.update(result.state_updates)
        new += result.is_signal
    assert old > 30   # the pre-review rule fired on nearly every bar
    assert new == 0   # a slow drift never breaks above a prior close that was itself a breakout


def test_only_the_first_breakout_bar_signals_and_a_prior_breakout_disqualifies_the_next() -> None:
    base = ic_base()
    first = base + [make_candle(17, "100.35", "100.60", "100.30", "100.45")]
    second = first + [make_candle(18, "100.45", "100.70", "100.40", "100.65")]
    assert evaluate("intraday_consolidation_breakout", make_context(first)).is_signal
    assert not evaluate("intraday_consolidation_breakout", make_context(second)).is_signal
    # a flat, genuine consolidation after a breakout rebuilds a new base and can signal again
    rebuilt = second + [make_candle(19 + i, "100.60", "100.70", "100.55", "100.62") for i in range(16)]
    rebuilt += [make_candle(35, "100.65", "100.95", "100.6", "100.85")]
    assert evaluate("intraday_consolidation_breakout", make_context(rebuilt)).is_signal


def test_consolidation_windows_do_not_cross_sessions_or_missing_minutes() -> None:
    bars = ic_base() + [make_candle(17, "100.35", "100.70", "100.30", "100.65")]
    gap = bars[:9] + [Candle(b.symbol, b.timeframe, b.start + timedelta(minutes=1), b.end + timedelta(minutes=1),
                             b.open, b.high, b.low, b.close, b.volume, b.feed_id, b.consolidated, b.origin,
                             b.price_basis, b.status) for b in bars[9:]]
    assert evaluate("intraday_consolidation_breakout", make_context(gap)).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)
    yesterday = [make_candle(i, "100.10", "100.40", "100.00", "100.20", base_time=YESTERDAY)
                 for i in range(10)]
    today = [make_candle(i, "100.10", "100.40", "100.00", "100.20") for i in range(8)] + [
        make_candle(8, "100.35", "100.70", "100.30", "100.65")]
    assert evaluate("intraday_consolidation_breakout", make_context(yesterday + today)).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)
    assert evaluate("intraday_consolidation_breakout", make_context(bars[:16])).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)  # 15 + 2 = 17 bars are required
    first = evaluate("intraday_consolidation_breakout", make_context(bars))
    assert not evaluate("intraday_consolidation_breakout", make_context(bars),
                        state={**ARMED, **first.state_updates}).is_signal


def test_consolidation_breakout_requires_a_narrow_range_unlike_a_plain_channel_breakout() -> None:
    wide = [make_candle(i, "100.5", "101.5", "100.00", "100.8") for i in range(17)] + [
        make_candle(17, "101.4", "102", "101.3", "101.8")]
    assert evaluate("donchian_channel_breakout", make_context(wide), {"period": 15}).is_signal
    assert not evaluate("intraday_consolidation_breakout", make_context(wide)).is_signal
    tight = ic_base() + [make_candle(17, "100.35", "100.70", "100.30", "100.65")]
    assert evaluate("donchian_channel_breakout", make_context(tight), {"period": 15}).is_signal
    assert evaluate("intraday_consolidation_breakout", make_context(tight)).is_signal


# =============================================================== Bollinger mean reversion


def closes_to_bars(closes: list[Decimal]) -> list[Candle]:
    return [make_candle(i, c, c + D("0.2"), c - D("0.2"), c) for i, c in enumerate(closes)]


def test_bollinger_lower_band_at_each_bar_uses_only_data_up_to_that_bar() -> None:
    base = [D(100) + D("0.1") * (i % 3) for i in range(19)]
    distinguishing = 0
    for a in range(0, 200, 3):
        c19 = D("98.0") + D("0.01") * a
        for b in range(0, 100, 2):
            closes = base + [c19, D("99.0") + D("0.02") * b]
            lower_prior = bollinger(closes[0:20], 20, D(2))[0]   # bars t-20 .. t-1: known at t-1
            lower_latest = bollinger(closes[1:21], 20, D(2))[0]  # bars t-19 .. t:   known at t
            expected = closes[-2] < lower_prior and closes[-1] > lower_latest
            wrong = closes[-2] < lower_latest and closes[-1] > lower_latest  # prior bar vs the later band
            got = evaluate("bollinger_band_mean_reversion", make_context(closes_to_bars(closes))).is_signal
            assert got == expected
            distinguishing += expected != wrong
    assert distinguishing > 20, "the scenarios must separate the correct band timing from the wrong one"


def test_bollinger_equality_edges_and_flat_windows_are_not_signals() -> None:
    flat = closes_to_bars([D(100)] * 20 + [D(101)])
    assert not evaluate("bollinger_band_mean_reversion", make_context(flat)).is_signal  # prior == lower
    closes = [D(100) + D("0.1") * (i % 3) for i in range(19)] + [D(90), D(99)]
    bars = closes_to_bars(closes)
    prior = bollinger(closes[0:20], 20, D(2))[0]
    latest = bollinger(closes[1:21], 20, D(2))[0]
    assert closes[-2] < prior and closes[-1] > latest
    assert evaluate("bollinger_band_mean_reversion", make_context(bars)).is_signal


def test_bollinger_warmup_gap_replay_params_and_distinctness() -> None:
    closes = [D(100) + D("0.1") * (i % 3) for i in range(19)] + [D(90), D(99)]
    bars = closes_to_bars(closes)
    assert evaluate("bollinger_band_mean_reversion", make_context(bars[-20:])).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)
    gap = bars[:-1] + [Candle(bars[-1].symbol, bars[-1].timeframe, bars[-1].start + timedelta(minutes=1),
                              bars[-1].end + timedelta(minutes=1), bars[-1].open, bars[-1].high,
                              bars[-1].low, bars[-1].close, bars[-1].volume, bars[-1].feed_id,
                              bars[-1].consolidated, bars[-1].origin, bars[-1].price_basis, bars[-1].status)]
    assert evaluate("bollinger_band_mean_reversion", make_context(gap)).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    first = evaluate("bollinger_band_mean_reversion", make_context(bars))
    assert not evaluate("bollinger_band_mean_reversion", make_context(bars),
                        state={**ARMED, **first.state_updates}).is_signal
    for bad in ({"period": 1}, {"period": 101}, {"deviations": D("0.05")}, {"deviations": D(6)}, {"period": 2.5}):
        with pytest.raises(ParameterValidationError):
            evaluate("bollinger_band_mean_reversion", make_context(bars), bad)
    # not the upper-band breakout, not the Keltner reversion
    rng = random.Random(21)
    walk = closes_to_bars([D(f"{100 + 4 * __import__('math').sin(i / 6) + rng.uniform(-1, 1):.2f}") for i in range(300)])
    ids = ("bollinger_band_mean_reversion", "bollinger_band_breakout", "keltner_channel_reversion")
    fired = {sid: {i for i in range(60, len(walk)) if evaluate(sid, make_context(walk[: i + 1])).is_signal}
             for sid in ids}
    assert fired[ids[0]] and fired[ids[0]] != fired[ids[1]] and fired[ids[0]] != fired[ids[2]]


# ================================================================= equivalence decisions


def test_vwap_reclaim_is_the_vwap_bounce_crossing() -> None:
    from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy

    bounce = VwapBounceBuy()

    def go(prior_close: str, latest_close: str, vwap_prev: str = "100", vwap: str = "100"):
        bars = [make_candle(0, 100, 101, 99, 100), make_candle(1, prior_close, "101", "98", prior_close),
                make_candle(2, "100", "103", "99", latest_close)]
        ctx = make_context(bars, indicators={"vwap": D(vwap), "vwap_prev": D(vwap_prev)})
        return bounce.evaluate(ctx, {}, dict(ARMED)).is_signal

    assert go("99", "101")             # below -> above: the reclaim
    assert go("100", "101")            # at VWAP -> above (the only extra case vs a strict 'below' reclaim)
    assert not go("101", "102")        # already above: not a reclaim
    assert not go("99", "100")         # latest exactly on VWAP is not above it
    # session reset: yesterday's bars never serve as the prior observation
    y = make_candle(0, 99, 101, 98, 99, base_time=YESTERDAY)
    t = make_candle(0, 100, 103, 99, 102)
    ctx = make_context([y, t], indicators={"vwap": D(100), "vwap_prev": D(100)})
    assert bounce.evaluate(ctx, {}, dict(ARMED)).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_bollinger_middle_band_equals_sma_but_price_below_sma_is_a_level_rule() -> None:
    from mike_trade.strategies.sell.price_below_sma import PriceBelowSmaSell

    rng = random.Random(2)
    closes = [D(f"{100 + rng.uniform(-3, 3):.2f}") for _ in range(60)]
    for end in range(20, 61):  # the middle band IS the 20-bar SMA of closes, exactly
        window = closes[end - 20:end]
        assert bollinger(window, 20, D(2))[1] == sma(window, 20)[-1]
    sell = PriceBelowSmaSell()
    below = closes_to_bars([D(100)] * 20 + [D(99), D(98)])  # crosses below once, then stays below
    results = [sell.evaluate(make_context(below[: i + 1]), {"period": 20}, {}).is_signal for i in (20, 21)]
    assert results == [True, True]  # signals on EVERY bar below the SMA: a level, not an edge
    # ... so it also fires on the first eligible bar after an entry made below the middle band
    entry = closes_to_bars([D(100)] * 20 + [D(95)])  # e.g. a lower-band mean-reversion entry
    assert sell.evaluate(make_context(entry), {"period": 20}, {}).is_signal


# ======================================================================= BACKTEST consistency


def engine_rows(bars: list[Candle]) -> list[tuple[str, str, str, str]]:
    return [(str(b.open), str(b.high), str(b.low), str(b.close)) for b in bars]


def first_direct_signal(strategy_id: str, bars: list[Candle], params: dict[str, object]) -> int | None:
    state: dict[str, object] = {}
    for i in range(len(bars)):
        window = bars[: i + 1]
        ctx = vwap_context(window) if len(window) > 1 else make_context(window)
        result = evaluate(strategy_id, ctx, params, {**ARMED, **state})
        state.update(result.state_updates)
        if result.is_signal:
            return i
    return None


@pytest.mark.parametrize("strategy_id,bars,params", [
    ("vwap_pullback", PULLBACK_BARS, {}),
    ("vwap_band_reversion", band_session(), {}),
    ("intraday_consolidation_breakout",
     ic_base() + [make_candle(17, "100.35", "100.70", "100.30", "100.65")], {}),
    ("bollinger_band_mean_reversion", closes_to_bars(
        [D(100) + D("0.1") * (i % 3) for i in range(19)] + [D(90), D(99)]), {}),
    ("support_bounce", sb_case(), SB),
])
def test_backtest_enters_on_the_bar_the_strategy_signals(strategy_id, bars, params, tmp_path) -> None:
    expected = first_direct_signal(strategy_id, bars, params)
    assert expected is not None
    result = backtest_run(engine_rows(bars) + [(str(bars[-1].close),) * 4] * 3, (strategy_id,),
                          {strategy_id: params}, registry=REGISTRY, result_dir=tmp_path)
    buys = [row for row in result.intents if row["side"] == "BUY"]
    assert buys, "BACKTEST should enter"
    index = int((buys[0]["signal_bar_time"] - ENGINE_OPEN).total_seconds() // 60)
    assert index == expected
    assert {row["side"] for row in result.intents} <= {"BUY"}  # no short/cover/sell from a BUY strategy
