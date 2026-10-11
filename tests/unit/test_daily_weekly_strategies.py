"""VCP and Cup-and-Handle: daily qualification, causal pivots, intraday breakout timing.

Expected values are derived by hand from the fixture geometry (closed-form prices), not by
calling the implementation under test.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from daily_fixtures import (
    LAST_DAILY,
    SYMBOL,
    TODAY,
    WICK,
    cup_closes,
    cup_volumes,
    daily_candle,
    daily_series,
    minute_candles,
    path,
    vcp_closes,
    vcp_volumes,
)

from mike_trade.market_data.candles import CandleTimeframe, PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus, session_for
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.indicators.daily_patterns import (
    CupParams,
    TrendTemplate,
    VcpParams,
    daily_swing_pivots,
    find_cup_and_handle,
    find_vcp,
    quadratic_bottom_fit,
    trend_template_issue,
)

D = Decimal
REGISTRY = _research_registry()
ARMED = {"eligibility": "WATCHING_BUY"}
M1, D1 = CandleTimeframe.M1, CandleTimeframe.D1
VCP_ID, CUP_ID = "volatility_contraction_pattern", "cup_and_handle"


def make_context(daily, minutes, *, stale: bool = False) -> MarketContext:
    as_of = minutes[-1].end
    return MarketContext(
        symbol=SYMBOL, as_of=as_of, version=len(minutes), feed_id="iex", consolidated=False,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=session_for(TODAY).session_id,
        freshness=ContextFreshness(as_of, timedelta(0), timedelta(minutes=5), stale,
                                   "DATA_TOO_OLD" if stale else None),
        candles={M1: tuple(minutes), D1: tuple(daily)}, indicators={},
    )


def run(strategy_id, daily, minutes, params=None, state=None):
    return REGISTRY.get(strategy_id)().evaluate(
        make_context(daily, minutes), params or {}, dict(ARMED) if state is None else state)


def vcp_daily(closes=None, volumes=None, **kw):
    closes = vcp_closes() if closes is None else closes
    return daily_series(closes, volumes=vcp_volumes() if volumes is None else volumes, **kw)


def cup_daily(closes=None, volumes=None, **kw):
    closes = cup_closes() if closes is None else closes
    return daily_series(closes, volumes=cup_volumes(len(closes)) if volumes is None else volumes, **kw)


# =========================================================================== registration


def test_both_strategies_are_registered_buy_strategies_that_declare_their_daily_history() -> None:
    for strategy_id, sessions in ((VCP_ID, 252), (CUP_ID, 241)):
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side.value == "BUY" and cls.spec.version == "1.0.0"
        assert cls.spec.required_timeframes == (M1, D1) and cls.spec.trigger_timeframe is M1
        assert cls.spec.history_bars == ((D1, sessions),) and cls.spec.requires_volume
        assert cls.required_history({}) == {D1: sessions}
        assert cls.required_bars({}) == cls.spec.warmup_bars == 1
        assert set(cls.spec.persistent_state_keys) == {"last_signal_bar", "daily_pattern"}
        assert cls.spec.source and cls.spec.methodology and cls.spec.execution_limitations
    assert REGISTRY.get(VCP_ID).required_history({"base_sessions": 250}) == {D1: 253 + 0}
    assert REGISTRY.get(CUP_ID).required_history({"max_cup_sessions": 200}) == {D1: 311}


# =================================================================================== VCP


def sma(values, n):
    return sum(values[-n:]) / n


def test_trend_template_matches_an_independent_oracle() -> None:
    closes = vcp_closes()
    daily = vcp_daily()
    t = TrendTemplate()
    last = closes[-1]
    s50, s150, s200 = sma(closes, 50), sma(closes, 150), sma(closes, 200)
    s200_before = sum(closes[-200 - 21:-21]) / 200
    low_52 = min(c.low for c in daily[-252:])
    high_52 = max(c.high for c in daily[-252:])
    assert last > s150 and last > s200 and s150 > s200 and s200 > s200_before
    assert s50 > s150 and s50 > s200 and last > s50
    assert last >= low_52 * D("1.30") and last >= high_52 * D("0.75")
    assert trend_template_issue(daily, t) is None
    assert t.required_sessions == 252
    # each condition is individually enforced: relax nothing, break one at a time
    down = daily_series(path([(0, 120), (230, 60), (259, 61)]), volumes=vcp_volumes())
    assert trend_template_issue(down, t) == "price_above_mid_and_slow"
    flat = daily_series([D(100)] * 260)
    assert trend_template_issue(flat, t) is not None
    far_from_high = daily_series(path([(0, 50), (200, 150), (259, 100)]))
    assert trend_template_issue(far_from_high, t) in ("near_52w_high", "price_above_fast",
                                                      "price_above_mid_and_slow", "fast_above_mid_and_slow")


def test_swing_pivots_require_the_confirming_sessions_to_exist() -> None:
    daily = vcp_daily()
    highs, _ = daily_swing_pivots(daily, 2)
    assert [i for i, _ in highs][-3:] == [185, 205, 222]
    assert highs[-3][1] == D(100) * (1 + WICK)
    prefix_highs, _ = daily_swing_pivots(daily[:224], 2)          # only one session after 222
    assert 222 not in [i for i, _ in prefix_highs]
    prefix_highs, _ = daily_swing_pivots(daily[:225], 2)
    assert 222 in [i for i, _ in prefix_highs]


def test_vcp_base_matches_hand_derived_contractions_and_pivot() -> None:
    daily = vcp_daily()
    base = find_vcp(daily, VcpParams())
    assert not isinstance(base, str)
    # closed-form expectations from the fixture path (high = close*1.005, low = close*0.995)
    h1, l1 = D(100) * D("1.005"), D(80) * D("0.995")
    h2, l2 = D(98) * D("1.005"), D(90) * D("0.995")
    h3, l3 = D("97.5") * D("1.005"), D("94.5") * D("0.995")
    expected = ((h1 - l1) / h1, (h2 - l2) / h2, (h3 - l3) / h3)
    assert base.depths == pytest.approx(expected, rel=D("1e-12")) or base.depths == expected
    assert base.pivot == h3 == D("97.9875")
    assert [(c.high_index, c.low_index) for c in base.contractions] == [(185, 193), (205, 210), (222, 226)]
    assert expected[0] > expected[1] > expected[2] and expected[1] <= expected[0] * D("0.85")
    assert expected[2] <= expected[1] * D("0.85")
    volumes = vcp_volumes()
    assert base.dry_up == (D(7000) / 10) / (sum(volumes[-50:]) / D(50))


def prefix_series(closes, cut):
    """The first ``cut`` sessions with the volume dry-up in the last ten (as seen at that date)."""
    window = closes[:cut]
    return daily_series(window, volumes=[1000] * (cut - 10) + [700] * 10)


def test_pivot_confirmation_lag_no_hindsight_in_the_base() -> None:
    closes = vcp_closes()
    early = find_vcp(prefix_series(closes, 224), VcpParams())    # high @222 has one later session
    assert not isinstance(early, str) and early.pivot == D(98) * D("1.005")     # the @205 swing high
    assert [c.high_index for c in early.contractions] == [185, 205]
    confirmed = find_vcp(prefix_series(closes, 225), VcpParams())               # two later sessions
    assert not isinstance(confirmed, str) and confirmed.pivot == D("97.9875")
    assert [c.high_index for c in confirmed.contractions] == [185, 205, 222]


@pytest.mark.parametrize("name,closes,reason", [
    # a closed pullback followed by one that is not smaller enough (9.5% vs 10.9%, ratio 0.87)
    ("not_tight_enough", path([(0, 50), (185, 100), (195, 90), (215, 99), (235, "90.5"), (259, "98.5")]),
     "contractions_not_successively_smaller"),
    # equal-sized pullbacks (no contraction ordering)
    ("equal", path([(0, 50), (185, 100), (195, 88), (205, 100), (215, 88), (225, 100),
                    (235, 88), (259, 99)]), "contractions_not_successively_smaller"),
    # pullbacks getting deeper (wrong ordering)
    ("widening", path([(0, 50), (185, 100), (195, 95), (205, 100), (215, 88), (225, 100),
                       (235, 78), (259, 99)]), "contractions_not_successively_smaller"),
    # a Bollinger-squeeze style quiet tape after the advance: no pullbacks at all
    ("squeeze", path([(0, 50), (230, 100)]) + [D(100)] * 29, "too_few_swing_highs"),
])
def test_vcp_requires_a_sequence_of_successively_smaller_pullbacks(name, closes, reason) -> None:
    assert len(closes) == 260
    daily = daily_series(closes, volumes=vcp_volumes())
    assert find_vcp(daily, VcpParams()) == reason, name


@pytest.mark.parametrize("closes,volumes,reason", [
    # the base is intact but the close has already pushed above the pivot
    (vcp_closes()[:-1] + [D("98.5")], vcp_volumes(), "already_broken_out"),
    # price far below the pivot
    (path([(0, 50), (185, 100), (193, 80), (205, 98), (210, 90), (222, "97.5"), (226, "92.5"),
           (259, "92.7")]), vcp_volumes(), "far_from_pivot"),
    # volume is not contracting
    (vcp_closes(), [1000] * 260, "volume_not_drying_up"),
])
def test_vcp_pivot_distance_breakout_state_and_volume_are_enforced(closes, volumes, reason) -> None:
    daily = daily_series(closes, volumes=volumes)
    assert find_vcp(daily, VcpParams()) == reason


def test_trend_template_range_conditions_are_enforced_at_their_boundaries() -> None:
    daily = vcp_daily()
    high_52 = max(c.high for c in daily[-252:])
    low_52 = min(c.low for c in daily[-252:])
    last = daily[-1].close
    assert last / high_52 == D("96.5") / D("100.5")                     # 4.0% below the 52-week high
    assert trend_template_issue(daily, TrendTemplate(max_below_high=D("0.05"))) is None
    assert trend_template_issue(daily, TrendTemplate(max_below_high=D("0.03"))) == "near_52w_high"
    assert last / low_52 > D("1.30")
    assert trend_template_issue(daily, TrendTemplate(min_above_low=D("2.0"))) == "above_52w_low"


def test_vcp_with_no_volume_in_the_baseline_is_rejected_not_divided_by_zero() -> None:
    daily = daily_series(vcp_closes(), volumes=[0] * 260)
    assert find_vcp(daily, VcpParams()) == "no_volume"


def test_vcp_strategy_fires_on_the_first_intraday_close_above_the_pivot() -> None:
    daily = vcp_daily()
    pivot = D("97.9875")
    quiet = [D("97.0"), D("97.4"), D("97.9"), D("97.95")]            # all at or below the pivot
    bars = minute_candles(quiet + [D("97.9875")] + [D("98.0")], day=TODAY)  # equality is not a break
    below = run(VCP_ID, daily, bars[:-1])
    assert not below.is_signal and below.reason_codes == (ReasonCode.NO_SIGNAL,)
    result = run(VCP_ID, daily, bars)
    assert result.is_signal and result.bar_time == bars[-1].start
    assert result.reference_price == D("98.0") and bars[-1].start == session_for(TODAY).open_utc + timedelta(minutes=5)
    assert bars[-1].close > pivot >= bars[-2].close
    # the signal is dated on the M1 bar, never on a daily candle
    assert result.bar_time > daily[-1].end
    # a replay of the same bar never fires twice; neither does a later bar above the pivot
    again = run(VCP_ID, daily, bars, state={**ARMED, **result.state_updates})
    assert not again.is_signal
    later = run(VCP_ID, daily, bars + minute_candles([D("98.5")], start_index=6, day=TODAY),
                state={**ARMED, **result.state_updates})
    assert not later.is_signal and later.reason_codes == (ReasonCode.NO_SIGNAL,)
    # a gap-up open above the pivot fires on the very first bar of the session
    gap = run(VCP_ID, daily, minute_candles([D("98.4")], day=TODAY))
    assert gap.is_signal and gap.bar_time == session_for(TODAY).open_utc


def test_vcp_breakout_volume_is_prorated_against_average_daily_volume() -> None:
    daily = vcp_daily()
    average = sum(vcp_volumes()[-50:]) / D(50)                       # 940
    closes = [D("97.0")] * 5 + [D("98.0")]
    minutes = len(closes)
    needed = D("1.4") * average * minutes / D(390)                   # session volume to qualify
    per_bar = int(needed / minutes) + 1
    assert run(VCP_ID, daily, minute_candles(closes, volume=per_bar)).is_signal
    assert not run(VCP_ID, daily, minute_candles(closes, volume=max(1, per_bar // 4))).is_signal
    assert run(VCP_ID, daily, minute_candles(closes, volume=1), {"breakout_volume_ratio": D(0)}).is_signal


def test_vcp_fails_closed_on_insufficient_stale_or_inconsistent_history() -> None:
    bars = minute_candles([D("97.0"), D("98.0")], day=TODAY)
    daily = vcp_daily()
    insufficient = (ReasonCode.INSUFFICIENT_DATA,)
    assert run(VCP_ID, daily[-200:], bars).reason_codes == insufficient       # < 252 sessions
    assert run(VCP_ID, [], bars).reason_codes == insufficient
    missing = daily[:200] + daily[201:]
    assert run(VCP_ID, missing, bars).reason_codes == insufficient            # a missing session
    stale = daily_series(vcp_closes(), volumes=vcp_volumes(), last=LAST_DAILY - timedelta(days=7))
    assert run(VCP_ID, stale, bars).reason_codes == insufficient              # not the last session
    split = daily[:230] + [daily_candle(c.start.astimezone(session_for(TODAY).open_et.tzinfo).date(),
                                        c.close / 2, open_=c.close / 2, volume=c.volume)
                           for c in daily[230:]]
    assert run(VCP_ID, split, bars).reason_codes == insufficient              # possible unadjusted split
    late = minute_candles([D("97.0"), D("98.0")], day=TODAY, start_index=3)
    assert run(VCP_ID, daily, late).reason_codes == insufficient              # session not from the open
    assert run(VCP_ID, daily, bars).is_signal


def test_vcp_guards_eligibility_parameters_and_the_pattern_cache() -> None:
    daily = vcp_daily()
    bars = minute_candles([D("97.0"), D("98.0")], day=TODAY)
    strategy = REGISTRY.get(VCP_ID)()
    for state in ({}, {"eligibility": "WATCHING_SHORT"}):
        assert strategy.evaluate(make_context(daily, bars), {}, state).reason_codes == (
            ReasonCode.NOT_ELIGIBLE,)
    stale = MarketContext(**{**{f: getattr(make_context(daily, bars), f) for f in
                                ("symbol", "as_of", "version", "feed_id", "consolidated", "price_basis",
                                 "session_status", "session_id", "candles", "indicators")},
                             "freshness": ContextFreshness(bars[-1].end, timedelta(hours=1),
                                                           timedelta(minutes=5), True, "DATA_TOO_OLD")})
    assert strategy.evaluate(stale, {}, dict(ARMED)).reason_codes == (ReasonCode.STALE_DATA,)
    for bad in ({"fast_ma": 150}, {"mid_ma": 40}, {"slow_ma": 100}, {"dry_up_sessions": 60},
                {"min_contractions": 5, "max_contractions": 3}, {"min_base_sessions": 130},
                {"fast_ma": 4}, {"contraction_ratio": D("1.1")}, {"strength": 0},
                {"breakout_volume_ratio": D(-1)}, {"max_first_depth_pct": 1.5}, {"unknown": 1}):
        with pytest.raises(ParameterValidationError):
            strategy.evaluate(make_context(daily, bars), bad, dict(ARMED))
    # the daily pattern is cached per settled session and parameter signature, with identical results
    first = strategy.evaluate(make_context(daily, bars[:1]), {}, dict(ARMED))
    cache = first.state_updates["daily_pattern"]
    assert cache["pivot"] == "97.9875" and cache["reason"] == "vcp"
    assert cache["session"] == daily[-1].start.isoformat()
    cached = strategy.evaluate(make_context(daily, bars), {}, {**ARMED, "daily_pattern": cache})
    fresh = strategy.evaluate(make_context(daily, bars), {}, dict(ARMED))
    assert cached.is_signal and fresh.is_signal and "daily_pattern" not in cached.state_updates
    poisoned = {**cache, "pivot": "1000"}                              # a cache for another session
    other = strategy.evaluate(make_context(daily, bars), {}, {**ARMED, "daily_pattern": {
        **poisoned, "session": "stale"}})
    assert other.is_signal and other.state_updates["daily_pattern"]["pivot"] == "97.9875"
    changed = strategy.evaluate(make_context(daily, bars), {"max_pivot_distance_pct": D("6")},
                                {**ARMED, "daily_pattern": poisoned})
    assert changed.state_updates["daily_pattern"]["pivot"] == "97.9875"   # signature differs: recomputed


def test_vcp_a_prefix_of_the_history_yields_what_was_knowable_then() -> None:
    closes = vcp_closes()
    seen = {}
    for cut_at in (224, 225, 240, 260):
        found = find_vcp(prefix_series(closes, cut_at), VcpParams())
        seen[cut_at] = None if isinstance(found, str) else found.pivot
    # before the @222 high is confirmed the pivot is the earlier high; afterwards it never changes
    assert seen[224] == D(98) * D("1.005")
    assert seen[225] == seen[240] == seen[260] == D("97.9875")
    assert REGISTRY.get(VCP_ID).required_history({}) == {D1: 252}


# ================================================================================ cup and handle


def cup_expectations():
    closes = cup_closes()
    il, ir = 110 + 76, 110 + 76 + 60       # left rim (@186) and right rim (@246)
    hl = closes[il] * (1 + WICK)
    bottom_low = min(c * (1 - WICK) for c in closes[il: ir + 1])
    cup_closes_ = closes[il: ir + 1]
    depth_abs = hl - bottom_low
    lower_half = sum(1 for c in cup_closes_ if (hl - c) / depth_abs >= D("0.5"))
    return closes, il, ir, hl, bottom_low, depth_abs, lower_half


def test_cup_geometry_matches_hand_derived_values() -> None:
    closes, il, ir, hl, bottom_low, depth_abs, lower_half = cup_expectations()
    daily = cup_daily()
    base = find_cup_and_handle(daily, CupParams())
    assert not isinstance(base, str)
    assert (base.left_index, base.bottom_index, base.right_index, base.last_index) == (186, 216, 246, 260)
    assert hl == D("100.5") and bottom_low == D(78) * D("0.995")
    assert base.depth == depth_abs / hl and D("0.12") <= base.depth <= D("0.35")
    assert base.u_fraction == D(lower_half) / D(61) and base.u_fraction >= D("0.6")
    assert base.bottom_position == D("0.5")
    handle_low = min(c.low for c in daily[ir + 1:])
    assert handle_low == D(94) * D("0.995")
    assert base.handle_depth == (hl - handle_low) / hl
    assert base.handle_depth <= base.depth / 2 and handle_low >= bottom_low + depth_abs / 2
    assert base.handle_volume_ratio == D("0.6")
    assert base.pivot == hl == D("100.5")
    # the parabola fit puts the vertex mid-cup and opens upward
    fit = quadratic_bottom_fit(closes[il: ir + 1])
    assert fit is not None and fit[0] > 0 and abs(fit[1] - D("0.5")) < D("0.01")


HANDLE = [D(x) for x in ("99", "97.5", "96", "95", "94.5", "94", "94.2", "94.6", "95", "95.4",
                         "95.8", "96.2", "96.5", "96.8")]


def parabola(low: int | str, n: int = 61) -> list[Decimal]:
    """Rim-to-rim parabola from 100 down to ``low`` at the centre and back to 100."""
    mid = D(n - 1) / 2
    return [D(100) - (D(100) - D(str(low))) * (1 - ((D(i) - mid) / mid) ** 2) for i in range(n)]


def cup_fixture(cup=None, handle=None, ramp=None, lead=76):
    rise = (ramp if ramp is not None else path([(0, 50), (110, 100)]))[:110]
    closes = [D(50)] * lead + list(rise) + (cup if cup is not None else parabola(78)) + (
        handle if handle is not None else HANDLE)
    return daily_series(closes, volumes=cup_volumes(len(closes)))


@pytest.mark.parametrize("name,cup,reason", [
    ("v_shape", path([(0, 100), (30, 78), (60, 100)]), "v_shaped"),
    # two bottoms: a long flat low, a rally of 45% of the depth, a slightly lower second low
    ("double_bottom", path([(0, 100), (10, 82), (20, 82), (26, 92), (32, 78), (46, 80), (60, 100)]),
     "left_side_not_smooth"),
    ("too_shallow", parabola(94), "cup_depth"),
    ("too_deep", parabola(45), "cup_depth"),
    ("bottom_at_the_edge", path([(0, 100), (8, 78), (60, 100)]), "bottom_not_central"),
])
def test_cup_rejects_v_shapes_double_bottoms_and_wrong_depths(name, cup, reason) -> None:
    assert len(cup) == 61
    assert find_cup_and_handle(cup_fixture(cup), CupParams()) == reason, name
    assert not isinstance(find_cup_and_handle(cup_fixture(), CupParams()), str)   # control


@pytest.mark.parametrize("handle,reason", [
    # a handle that falls ~20% (deeper than the 15% limit and than half the cup)
    ([D(x) for x in ("97", "92", "88", "84", "81", "80.5", "81", "82", "83", "84", "85", "86", "87", "88")],
     "handle_depth"),
    # a flat, shallow handle (less than 2%)
    ([D(x) for x in ("99.8", "99.5", "99.3", "99.2", "99.1", "99.2", "99.3", "99.4", "99.5", "99.6",
                     "99.7", "99.8", "99.9", "99.9")], "handle_depth"),
    # a handle that has already closed above the right rim (the breakout already happened)
    ([D(x) for x in ("99", "97.5", "96", "95", "94.5", "94", "94.2", "94.6", "95", "96", "98", "99.5",
                     "101", "102")], "already_broken_out"),
])
def test_cup_handle_depth_and_breakout_state_are_enforced(handle, reason) -> None:
    assert find_cup_and_handle(cup_fixture(handle=handle), CupParams()) == reason


def test_cup_requires_a_quiet_handle_a_prior_advance_and_a_real_cup() -> None:
    closes = cup_closes()
    loud = daily_series(closes, volumes=[1000] * (len(closes) - 14) + [1200] * 14)
    assert find_cup_and_handle(loud, CupParams()) == "handle_volume_not_drying_up"
    # an 80 -> 100 advance is only 25%, under the 30% prior-advance requirement
    assert find_cup_and_handle(cup_fixture(ramp=path([(0, 80), (110, 100)])), CupParams()) \
        == "no_prior_uptrend"
    assert not isinstance(find_cup_and_handle(
        cup_fixture(ramp=path([(0, 80), (110, 100)])), CupParams(min_prior_advance=D("0.15"))), str)
    # a 21-session round bottom is shorter than the allowed 35..130 cup
    short = [D(100) - D(22) * (1 - ((D(i) - 10) / 10) ** 2) for i in range(21)]
    assert find_cup_and_handle(cup_fixture(short), CupParams()) == "no_cup"
    # a ramp followed by a flat shelf and no handle is a resistance breakout setup, not a cup
    shelf = path([(0, 50), (180, 100)])[:-1] + [D(100)] * 81
    assert find_cup_and_handle(daily_series(shelf, volumes=cup_volumes(len(shelf))), CupParams()) \
        in ("no_right_rim", "no_cup")
    # the cup is deeper than the configured limit only when the limit says so
    assert find_cup_and_handle(cup_fixture(parabola(60)), CupParams()) == "cup_depth"
    assert not isinstance(find_cup_and_handle(
        cup_fixture(parabola(60)), CupParams(max_depth=D("0.45"))), str)


def test_cup_pivots_are_confirmed_before_use_no_future_rim() -> None:
    daily = cup_daily()
    params = CupParams()
    # the right rim sits at index 246; with strength 3 it is a swing high only once 3 sessions follow
    # and a handle needs at least 5, so before index 251 nothing can be reported
    for last in (247, 248, 249, 250):
        assert find_cup_and_handle(daily[: last + 1], params) == "no_right_rim"
    ready = find_cup_and_handle(daily[:252], params)                   # 5 handle sessions
    assert not isinstance(ready, str) and ready.right_index == 246
    assert ready.pivot == D("100.5")
    # later sessions only deepen the handle's history; the rim and pivot do not move
    full = find_cup_and_handle(daily, params)
    assert not isinstance(full, str) and (full.left_index, full.right_index, full.pivot) == (
        ready.left_index, ready.right_index, ready.pivot)


def test_cup_strategy_fires_on_the_first_intraday_close_above_the_pivot() -> None:
    daily = cup_daily()
    bars = minute_candles([D("100.0"), D("100.3"), D("100.4"), D("100.5"), D("100.6")], day=TODAY)
    assert not run(CUP_ID, daily, bars[:-1]).is_signal                # 100.5 == pivot: not a break
    result = run(CUP_ID, daily, bars)
    assert result.is_signal and result.bar_time == bars[-1].start == (
        session_for(TODAY).open_utc + timedelta(minutes=4))
    assert result.bar_time > daily[-1].end and result.reference_price == D("100.6")
    assert not run(CUP_ID, daily, bars, state={**ARMED, **result.state_updates}).is_signal
    again = run(CUP_ID, daily, bars + minute_candles([D("100.9")], start_index=5, day=TODAY),
                state={**ARMED, **result.state_updates})
    assert not again.is_signal
    # a failed breakout (close back under the pivot) may retry later in the same session only on
    # a first close - the session already closed above once, so it does not
    retry = run(CUP_ID, daily, bars + minute_candles([D("100.2"), D("100.7")], start_index=5, day=TODAY))
    assert not retry.is_signal


def test_cup_and_vcp_fail_closed_without_enough_clean_daily_history_and_validate_parameters() -> None:
    bars = minute_candles([D("100.0"), D("100.6")], day=TODAY)
    daily = cup_daily()
    assert run(CUP_ID, daily[-100:], bars).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    assert run(CUP_ID, daily[:50] + daily[51:], bars).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    stale = cup_daily(last=LAST_DAILY - timedelta(days=7))
    assert run(CUP_ID, stale, bars).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    assert run(CUP_ID, daily, bars).is_signal
    strategy = REGISTRY.get(CUP_ID)()
    for bad in ({"min_cup_sessions": 130}, {"min_depth_pct": D(40)}, {"min_handle_sessions": 2},
                {"min_handle_sessions": 25, "max_handle_sessions": 10}, {"strength": 4, "min_handle_sessions": 4},
                {"min_u_fraction": D("0.4")}, {"max_depth_pct": 1.5}, {"min_handle_depth_pct": D(20)},
                {"unknown": 1}):
        with pytest.raises(ParameterValidationError):
            strategy.evaluate(make_context(daily, bars), bad, dict(ARMED))
    for state in ({}, {"eligibility": "WATCHING_SHORT"}):
        assert strategy.evaluate(make_context(daily, bars), {}, state).reason_codes == (
            ReasonCode.NOT_ELIGIBLE,)
    # configured geometry changes the daily requirement
    assert strategy.required_history({"max_cup_sessions": 200}) == {D1: 311}
