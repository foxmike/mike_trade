"""Rule-by-rule tests for the corrected Michelle VWAP Bounce/Break rules engine (V2, Rules 1-51).

Rule numbers refer to ``docs/strategies/michelle/vwap_bounce_break_business_rules_final_2.docx``.
Bars are built directly as :class:`SessionBar` values so each test controls VWAP and EMA values.
Rules 5, 42-48 and 51 (stale data, protective stops, daily loss, end of day) are verified in
``test_michelle_corrected_lifecycle.py``; the traceability table lives in
``docs/strategies/michelle/vwap_bounce_break_michelle_corrected.md``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.scheduling.calendar import session_for
from mike_trade.strategies import michelle_corrected as mc
from mike_trade.strategies.michelle_corrected import (
    EntryMode,
    EntryScenario,
    PatternKind,
    Phase,
    SessionBar,
)

DAY = date(2026, 10, 8)
T0 = session_for(DAY).open_utc  # 09:30 New York
MIN = timedelta(minutes=1)


def sb(
    minute: int,
    o: str,
    h: str,
    low: str,
    c: str,
    *,
    vwap: str | None = "100",
    ema9: str | None = "99.4",
    ema20: str | None = "99.6",
) -> SessionBar:
    start = T0 + minute * MIN
    return SessionBar(
        start, start + MIN, D(o), D(h), D(low), D(c), D(1000),
        None if vwap is None else D(vwap),
        None if ema9 is None else D(ema9),
        None if ema20 is None else D(ema20),
        D(0), D(0),
    )


def bearish(minute: int, close: str = "99.00", low: str | None = None, **kw: str) -> SessionBar:
    """A red candle closing below VWAP/EMA9/EMA20 (a qualifying setup start)."""
    c = D(close)
    return sb(minute, str(c + D("0.10")), str(c + D("0.20")), low or str(c - D("0.10")), close, **kw)


def hammer(minute: int) -> SessionBar:
    # body 99.40-99.50 below VWAP 100; lower wick 0.80 >= 2*0.10; body in the upper half.
    # EMAs sit above the close so the hammer itself is a valid setup start (Rule 6).
    return sb(minute, "99.40", "99.55", "98.60", "99.50", ema9="99.8", ema20="99.9")


def drop(minute: int, low: str) -> SessionBar:
    """A large-bodied red candle printing ``low``: a lower low that is not a hammer."""
    lo = D(low)
    return sb(minute, str(lo + D("0.70")), str(lo + D("0.80")), low, str(lo + D("0.05")))


def confirmation(minute: int, close: str = "100.20", low: str = "99.90", open_: str = "99.95") -> SessionBar:
    return sb(minute, open_, str(D(close) + D("0.05")), low, close)


def chain_at(bars: list[SessionBar], index: int) -> list[SessionBar]:
    chain: list[SessionBar] = []
    for bar in bars[: index + 1]:
        chain = mc.extend_shelf(chain, bar)
    return chain


def _pullback_with_hammer(start: int = 0) -> list[SessionBar]:
    """Hammer (setup start, Pattern 1 low 98.60) then three more closes below VWAP."""
    return [hammer(start), bearish(start + 1), bearish(start + 2), bearish(start + 3)]


# ------------------------------------------------------------------ Rules 2-3: session preparation


def _candle(start: datetime, price: str, volume: str, symbol: str = "ABC") -> Candle:
    return Candle(
        symbol, CandleTimeframe.M1, start, start + MIN, D(price), D(price) + 1, D(price) - 1,
        D(price), D(volume), "sip", True, CandleOrigin.REPLAY, PriceBasis.RAW,
        CandleStatus.CLOSED,
    )


def test_rule2_premarket_seeds_emas_only_and_vwap_is_regular_session_only() -> None:
    pre = [_candle(T0 - (30 - i) * MIN, "50", "10000") for i in range(30)]
    regular = [_candle(T0 + i * MIN, "100", "100") for i in range(3)]
    bars = mc.prepare_session([*pre, *regular], DAY)
    assert [b.start for b in bars] == [c.start for c in regular]  # no premarket setup candles
    assert bars[0].vwap == D("100")  # untouched by the $50 premarket volume
    assert bars[0].ema9 is not None and bars[0].ema9 < D("100")  # seeded by premarket closes
    assert bars[0].ema20 is not None
    no_pre = mc.prepare_session(regular, DAY)
    assert no_pre[0].ema9 is None  # without premarket nothing can seed it (fails closed)
    after_close = _candle(T0 + 390 * MIN, "100", "100")  # 16:00 is outside the regular session
    assert [b.start for b in mc.prepare_session([*regular, after_close], DAY)] == [
        c.start for c in regular
    ]


def test_rule3_session_vwap_is_anchored_at_the_open_and_resets_every_day() -> None:
    yesterday = [_candle(T0 - timedelta(days=1) + i * MIN, "10", "50000") for i in range(5)]
    today = [_candle(T0 + i * MIN, "100", "100") for i in range(2)]
    bars = mc.prepare_session([*yesterday, *today], DAY)
    assert [b.vwap for b in bars] == [D("100"), D("100")]  # yesterday's volume never carries over
    assert bars[1].cum_volume == D("200")


# ------------------------------------------------------------------ Rule 4: entry hours


@pytest.mark.parametrize(
    ("minute", "allowed"),
    [(3, False), (4, True), (119, True), (120, False)],  # 09:33, 09:34, 11:29, 11:30
)
def test_rule4_entry_hours(minute: int, allowed: bool) -> None:
    assert mc.time_gate(T0 + minute * MIN) is allowed


def test_rule4_exactly_1130_is_prohibited_and_one_second_before_is_allowed() -> None:
    assert not mc.time_gate(T0 + 4 * MIN - timedelta(seconds=1))
    assert mc.time_gate(T0 + 4 * MIN)
    assert mc.time_gate(T0 + 120 * MIN - timedelta(seconds=1))
    assert not mc.time_gate(T0 + 120 * MIN)


# ------------------------------------------------------------------ Rules 6-8: setup start


def test_rule6_setup_requires_close_below_vwap_and_both_emas_with_emas_below_vwap() -> None:
    assert mc.starts_setup(bearish(0))
    assert not mc.starts_setup(sb(0, "99.5", "99.6", "98.9", "99.5"))  # close not below EMA9
    assert not mc.starts_setup(bearish(0, ema9="100.00"))  # EMA9 == VWAP is not "strictly below"
    assert not mc.starts_setup(bearish(0, ema20="100.00"))  # EMA20 == VWAP likewise
    assert not mc.starts_setup(bearish(0, ema20="100.10"))
    assert not mc.starts_setup(sb(0, "100.1", "100.2", "99.8", "100.00"))  # close == VWAP
    assert not mc.starts_setup(bearish(0, vwap=None))


def test_rule6_ema9_may_be_on_either_side_of_ema20() -> None:
    assert mc.starts_setup(bearish(0, ema9="99.2", ema20="99.6"))  # EMA9 below EMA20
    assert mc.starts_setup(bearish(0, ema9="99.7", ema20="99.5"))  # EMA9 above EMA20


def test_rule7_an_existing_bearish_alignment_needs_no_fresh_crossover() -> None:
    # The very first regular-session candle already meets the alignment: no prior bar exists.
    assert mc.scan([bearish(0)]).phase is Phase.SETUP


def test_rule8_setup_start_is_the_first_qualifying_candle_and_later_bars_do_not_reset() -> None:
    snap = mc.scan([bearish(2), bearish(3), bearish(4)])
    assert snap.setup_start == T0 + 2 * MIN


def test_rule8_the_setup_start_candle_counts_toward_the_three_below_vwap_closes() -> None:
    three = [hammer(0), bearish(1), bearish(2)]  # start candle + two more = three closes
    assert mc.scan([*three, confirmation(3)]).phase is Phase.WINDOW
    two = three[:2]
    assert mc.scan([*two, confirmation(2)]).phase is Phase.SETUP


def test_rule8_the_setup_start_candle_can_be_the_pattern_or_part_of_one() -> None:
    snap = mc.scan([hammer(0)])
    assert snap.pattern is not None and snap.pattern.kind is PatternKind.HAMMER
    assert snap.pattern.formed_at == T0
    # shelf whose first candle is the setup-start candle
    shelf = mc.scan([bearish(0, close="99.00"), bearish(1, close="99.20")])
    assert shelf.pattern is not None and shelf.pattern.kind is PatternKind.SUPPORT_SHELF_BASE
    assert shelf.pattern.anchor == T0
    # engulfing whose red candle is the setup-start candle
    red = sb(0, "99.30", "99.40", "98.90", "99.00")  # red, closes below VWAP/EMAs: setup start
    green = sb(1, "98.95", "99.40", "98.90", "99.35")
    engulf = mc.scan([red, green])
    assert engulf.pattern is not None and engulf.pattern.kind is PatternKind.BULLISH_ENGULFING


def test_rule13_candles_before_the_setup_start_never_join_a_pattern() -> None:
    # Candle 0 is red but not a setup start (EMA9 above VWAP); candle 1 starts the setup and the
    # pair would be a bullish engulfing and (with candle 2) a shelf if candle 0 were admitted.
    pre = sb(0, "99.00", "99.10", "98.70", "98.80", ema9="100.5")
    start = sb(1, "98.70", "99.40", "98.60", "99.30")
    assert not mc.starts_setup(pre) and mc.starts_setup(start)
    assert mc.is_bullish_engulfing(pre, start)
    snap = mc.scan([pre, start])
    assert snap.setup_start == T0 + MIN and snap.pattern is None
    pre_shelf = bearish(0, close="99.10", ema9="100.5")
    snap = mc.scan([pre_shelf, bearish(1, close="99.00")])
    assert snap.setup_start == T0 + MIN and snap.pattern is None  # a lone candle is no base


# ------------------------------------------------------------------ Rules 9-10: setup low, max dip


def test_rule9_lower_low_updates_associated_vwap_and_equal_low_does_not() -> None:
    bars = [
        bearish(0, low="98.50", vwap="100.00"),
        bearish(1, low="98.50", vwap="100.40"),  # equal low: associated VWAP stays 100.00
    ]
    snap = mc.scan(bars)
    assert (snap.setup_low, snap.setup_low_vwap) == (D("98.50"), D("100.00"))
    snap = mc.scan([*bars, bearish(2, low="98.40", vwap="100.20")])  # strictly lower
    assert (snap.setup_low, snap.setup_low_vwap) == (D("98.40"), D("100.20"))


@pytest.mark.parametrize(
    ("low", "expected_phase"),
    [("97.00", Phase.SETUP), ("96.99", Phase.NONE)],  # exactly $3.00 allowed; more is a breach
)
def test_rule10_dip_of_exactly_three_dollars_is_allowed_but_more_purges(
    low: str, expected_phase: Phase
) -> None:
    snap = mc.scan([bearish(0, close="98.50", low=low)])
    assert snap.phase is expected_phase
    if expected_phase is Phase.NONE:
        assert snap.last_purge_reason == "MAX_DIP_BREACHED"


def test_rule10_the_limit_applies_until_entry_and_the_breaching_bar_cannot_seed_a_new_setup() -> None:
    bars = [bearish(0), bearish(1, low="96.00")]
    snap = mc.scan(bars)
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "MAX_DIP_BREACHED"
    assert mc.scan([*bars, bearish(2)]).setup_start == T0 + 2 * MIN  # strictly after the purge
    # still enforced in the entry window (before any fill)
    window = [*_pullback_with_hammer(), confirmation(4), bearish(5, low="96.00", close="100.30")]
    assert mc.scan(window).last_purge_reason == "MAX_DIP_BREACHED"


# ------------------------------------------------------------------ Rule 11: 30-minute clock


def test_rule11_confirmation_exactly_thirty_minutes_after_setup_start_is_allowed() -> None:
    bars = _pullback_with_hammer()
    for minute in range(4, 30):
        bars.append(bearish(minute))
    assert mc.scan(bars).setup_start == T0
    allowed = mc.scan([*bars, confirmation(30)])  # candle start == setup start + 30:00
    assert allowed.phase is Phase.WINDOW
    late = mc.scan([*bars, bearish(30), confirmation(31)])  # 30:00 + one minute
    assert late.phase is Phase.NONE
    assert late.last_purge_reason == "CONFIRMATION_CLOCK_EXPIRED"


# ------------------------------------------------------------------ Rule 12: three closes below VWAP


def test_rule12_requires_three_consecutive_closes_below_vwap_before_candle_c_starts() -> None:
    two = [hammer(0), bearish(1)]
    assert mc.scan([*two, confirmation(2)]).phase is Phase.SETUP  # C does not count toward its own 3
    three = [hammer(0), bearish(1), bearish(2)]
    assert mc.scan([*three, confirmation(3)]).phase is Phase.WINDOW
    broken = [hammer(0), bearish(1), sb(2, "99.9", "100.4", "99.8", "100.2"), bearish(3)]
    assert mc.scan([*broken, confirmation(4)]).phase is Phase.SETUP  # a close above VWAP resets


# ------------------------------------------------------------------ Rules 16-19: hammer


def test_rule16_17_18_19_hammer_geometry() -> None:
    assert mc.is_hammer(hammer(0))
    assert mc.is_hammer(sb(0, "99.50", "99.55", "98.60", "99.40"))  # red hammer
    assert mc.is_hammer(sb(0, "99.50", "99.60", "98.50", "99.50"))  # doji, long upper wick ok
    assert not mc.is_hammer(sb(0, "99.40", "99.50", "99.40", "99.50"))  # lower wick 0 < 2x body
    assert not mc.is_hammer(sb(0, "99.40", "99.50", "99.40", "99.40"))  # body at the very low
    assert not mc.is_hammer(sb(0, "99.50", "99.50", "99.50", "99.50"))  # zero range
    # upper-half placement: body bottom must be >= low + 0.5*range (exactly midpoint allowed)
    assert not mc.is_hammer(sb(0, "99.00", "100.50", "98.00", "99.10"))
    assert mc.is_hammer(sb(0, "99.25", "99.80", "98.60", "99.30"))
    # wick exactly 2x body qualifies
    assert mc.is_hammer(sb(0, "99.40", "99.60", "99.20", "99.50"))
    assert not mc.is_hammer(sb(0, "99.40", "99.60", "99.21", "99.50"))


def test_rule16_the_whole_body_must_be_below_vwap() -> None:
    assert not mc.is_hammer(sb(0, "100.20", "100.30", "99.00", "100.10"))  # body above VWAP
    assert not mc.is_hammer(sb(0, "99.90", "100.30", "98.90", "100.10"))  # body straddles VWAP
    assert not mc.is_hammer(sb(0, "99.50", "100.00", "98.60", "100.00"))  # close exactly at VWAP
    assert mc.is_hammer(sb(0, "99.90", "100.50", "98.90", "99.95"))  # high above VWAP is fine (25)


def test_rule19_hammer_has_no_upper_wick_cap() -> None:
    # doji at the exact midpoint: the upper wick (0.90) is as long as the lower and is not capped
    assert mc.is_hammer(sb(0, "99.50", "100.40", "98.60", "99.50"))


# ------------------------------------------------------------------ Rules 20-22: support shelf


def test_rule20_a_base_qualifies_with_two_candles_and_extends_while_closes_stay_in_range() -> None:
    one = chain_at([bearish(0, close="99.00")], 0)
    assert mc.shelf_pattern(one) is None  # a single candle is not a base
    two = [bearish(0, close="99.00"), bearish(1, close="99.20")]
    pattern = mc.shelf_pattern(chain_at(two, 1))
    assert pattern is not None and pattern.bar_count == 2
    three = [*two, bearish(2, close="99.10")]
    assert len(chain_at(three, 2)) == 3  # extended
    snap = mc.scan(three)
    assert snap.pattern is not None and snap.pattern.bar_count == 3


def test_rule22_close_range_of_exactly_025_is_allowed_and_more_is_not() -> None:
    exact = [bearish(0, close="99.00"), bearish(1, close="99.25")]
    assert mc.shelf_pattern(chain_at(exact, 1)) is not None
    wide = [bearish(0, close="99.00"), bearish(1, close="99.26")]
    assert mc.shelf_pattern(chain_at(wide, 1)) is None
    # the range is over ALL candles of the base: 99.00 .. 99.25 then 99.26 would make 0.26
    grown = [*exact, bearish(2, close="99.26")]
    assert len(chain_at(grown, 2)) == 1  # the third candle starts the next candidate instead


def test_rule21_every_close_must_be_strictly_below_its_own_vwap() -> None:
    at_vwap = [bearish(0, close="99.00"), sb(1, "99.8", "100.2", "99.7", "100.00")]
    assert chain_at(at_vwap, 1) == []  # a close exactly at VWAP is not below it
    above = [bearish(0, close="99.00"), sb(1, "99.8", "100.5", "99.7", "100.20")]
    assert chain_at(above, 1) == []
    # colors do not matter: red then green
    mixed = [bearish(0, close="99.00"), sb(1, "98.90", "99.30", "98.85", "99.10")]
    assert len(chain_at(mixed, 1)) == 2


def test_rule20_a_candle_closing_above_the_range_locks_the_base_and_starts_a_new_candidate() -> None:
    bars = [bearish(0, close="99.00"), bearish(1, close="99.10"), bearish(2, close="99.40")]
    chain = chain_at(bars, 2)
    assert [b.close for b in chain] == [D("99.40")]
    snap = mc.scan(bars)  # the locked base is still the recorded Pattern 1
    assert snap.pattern1 is not None and snap.pattern1.low == D("99.00")
    assert snap.pattern1.bar_count == 2


def test_rule20_bases_need_adjacent_candles() -> None:
    gap = [bearish(0, close="99.00"), bearish(2, close="99.10")]  # 09:31 is missing
    assert len(chain_at(gap, 1)) == 1


def test_rule15_the_pattern_low_of_a_shelf_is_the_lowest_close_not_the_lowest_wick() -> None:
    # big red bodies keep both candles from being hammers; their wicks (98.50) sit below the closes
    bars = [sb(0, "99.30", "99.35", "98.50", "99.00"), sb(1, "99.60", "99.65", "98.50", "99.20")]
    assert not mc.is_hammer(bars[0]) and not mc.is_hammer(bars[1])
    pattern = mc.shelf_pattern(chain_at(bars, 1))
    assert pattern is not None and pattern.low == D("99.00")  # lowest close, not the 98.50 wick
    # a later wick below that low that closes at/above it does not discard the base
    snap = mc.scan([*bars, sb(2, "99.50", "99.55", "97.50", "99.10"), bearish(3, close="99.00")])
    assert snap.pattern1 is not None and not snap.pattern1_disqualified
    assert snap.pattern1.low == D("99.00") and snap.pattern1.kind is PatternKind.SUPPORT_SHELF_BASE


def test_rule15_an_extending_close_lowers_the_open_bases_low_and_is_not_a_break() -> None:
    bars = [bearish(0, close="99.10"), bearish(1, close="99.20"), bearish(2, close="99.00")]
    snap = mc.scan(bars)
    assert snap.pattern is not None and snap.pattern.low == D("99.00") and snap.pattern.bar_count == 3
    assert not snap.pattern1_disqualified and snap.pattern.stage == 1


# ------------------------------------------------------------------ Rules 23-24: bullish engulfing


def test_rule23_24_bullish_engulfing_equal_boundaries_and_no_wick_requirement() -> None:
    red = sb(0, "99.60", "99.70", "99.00", "99.30")
    green_equal = sb(1, "99.30", "99.65", "99.20", "99.60")  # open == red close, close == red open
    assert mc.is_bullish_engulfing(red, green_equal)
    green_short = sb(1, "99.30", "99.65", "99.20", "99.59")
    assert not mc.is_bullish_engulfing(red, green_short)
    green_gap_up = sb(1, "99.31", "99.65", "99.20", "99.60")  # opens above the red close
    assert not mc.is_bullish_engulfing(red, green_gap_up)
    green_above_vwap = sb(1, "99.30", "100.50", "99.20", "100.10")
    assert not mc.is_bullish_engulfing(red, green_above_vwap)
    red_above_vwap = sb(0, "100.60", "100.70", "99.90", "100.30")
    assert not mc.is_bullish_engulfing(red_above_vwap, green_equal)
    two_green = sb(0, "99.30", "99.70", "99.00", "99.60")
    assert not mc.is_bullish_engulfing(two_green, green_equal)
    not_adjacent = sb(2, "99.30", "99.65", "99.20", "99.60")
    assert not mc.is_bullish_engulfing(red, not_adjacent)
    # the green body engulfs the red body although its wicks do not engulf the red wicks
    narrow_wicks = sb(1, "99.30", "99.60", "99.25", "99.60")
    assert mc.is_bullish_engulfing(red, narrow_wicks)


def test_rule25_highs_and_wicks_may_reach_or_exceed_vwap_for_all_three_patterns() -> None:
    # hammer: the upper wick pokes through VWAP (100) while the whole body stays below it
    assert mc.is_hammer(sb(0, "99.40", "100.60", "98.20", "99.50"))  # open at the midpoint
    # shelf: two candles whose highs are above VWAP but whose closes are below it
    shelf = [sb(0, "99.00", "100.40", "98.90", "99.10"), sb(1, "99.05", "100.20", "98.95", "99.15")]
    assert len(chain_at(shelf, 1)) == 2
    # bullish engulfing: the green candle's high exceeds VWAP while its close stays below it
    red = sb(0, "99.60", "99.70", "99.00", "99.30")
    green = sb(1, "99.30", "101.00", "99.20", "99.65")
    assert mc.is_bullish_engulfing(red, green)
    # but the specified CLOSES must be below VWAP: a close at/above VWAP voids the pattern
    assert not mc.is_bullish_engulfing(red, sb(1, "99.30", "101.00", "99.20", "100.00"))


# ------------------------------------------------------------------ Rules 13-14: which pattern


def test_rule14_pattern_priority_is_hammer_then_shelf_then_engulfing() -> None:
    # A candle that is simultaneously a hammer, completes a shelf and an engulfing pair.
    bars = [
        bearish(0, close="99.20", low="98.60"),
        sb(1, "99.40", "99.50", "98.60", "99.30"),  # red
        sb(2, "99.30", "99.55", "98.60", "99.40"),  # green hammer that engulfs the red body
    ]
    chain = chain_at(bars, 2)
    assert mc.is_hammer(bars[2]) and len(chain) == 3
    assert mc.is_bullish_engulfing(bars[1], bars[2])
    pattern = mc.detect_pattern(bars, 2, 0, chain)
    assert pattern is not None and pattern.kind is PatternKind.HAMMER
    no_hammer = [bars[0], bars[1], sb(2, "99.30", "100.30", "98.60", "99.40")]
    assert not mc.is_hammer(no_hammer[2])
    pattern = mc.detect_pattern(no_hammer, 2, 0, chain_at(no_hammer, 2))
    assert pattern is not None and pattern.kind is PatternKind.SUPPORT_SHELF_BASE
    # an engulfing pair whose closes are too far apart for a shelf is classified third
    engulf_only = [
        sb(0, "99.60", "99.70", "99.00", "99.00"),
        sb(1, "98.90", "99.80", "98.90", "99.65"),
    ]
    assert not mc.is_hammer(engulf_only[1]) and len(chain_at(engulf_only, 1)) == 1
    pattern = mc.detect_pattern(engulf_only, 1, 0, chain_at(engulf_only, 1))
    assert pattern is not None and pattern.kind is PatternKind.BULLISH_ENGULFING
    assert pattern.low == D("98.90")  # the lowest price in the pattern (not a close)


def test_rule13_confirmation_needs_a_completed_bottoming_pattern_first() -> None:
    no_pattern = [drop(0, "98.90"), drop(1, "98.30"), drop(2, "97.70"), drop(3, "97.20")]
    snap = mc.scan([*no_pattern, confirmation(4)])
    assert snap.phase is Phase.SETUP and snap.pattern is None
    snap = mc.scan([*_pullback_with_hammer(), confirmation(4)])
    assert snap.phase is Phase.WINDOW and snap.pattern is not None
    assert snap.pattern.kind is PatternKind.HAMMER and snap.confirm_close == D("100.20")
    # the pattern may also complete during the recovery, before Candle C
    late_hammer = [bearish(0), bearish(1), bearish(2), hammer(3)]
    assert mc.scan([*late_hammer, confirmation(4)]).phase is Phase.WINDOW


# ------------------------------------------------------------------ Rule 15: Pattern 1 / Pattern 2


def test_rule15_a_wick_below_pattern1_never_discards_it_and_does_not_advance_the_count() -> None:
    # A later hammer with a LOWER wick (97.50 < 98.60) that closes at/above Pattern 1's low.
    wick_hammer = sb(4, "99.10", "99.20", "97.50", "99.00")
    assert mc.is_hammer(wick_hammer)
    snap = mc.scan([*_pullback_with_hammer(), wick_hammer, confirmation(5)])
    assert snap.phase is Phase.WINDOW
    assert not snap.pattern1_disqualified
    assert snap.pattern is not None and snap.pattern.stage == 1 and snap.pattern.low == D("98.60")
    # the setup's absolute low (a wick) is not the pattern low used for invalidation
    assert snap.setup_low == D("97.50") != snap.pattern.low


def test_rule15_a_completed_close_below_pattern1_discards_it_and_a_lower_pattern2_must_form() -> None:
    bars = [*_pullback_with_hammer(), drop(4, "98.00")]  # closes 98.05 < 98.60
    snap = mc.scan([*bars, confirmation(5)])
    assert snap.phase is Phase.SETUP and snap.pattern1_disqualified  # confirmation refused
    # a lower hammer forms Pattern 2; confirmation then qualifies
    p2 = sb(5, "99.00", "99.10", "97.90", "99.10")
    assert mc.is_hammer(p2)
    snap = mc.scan([*bars, p2, confirmation(6)])
    assert snap.phase is Phase.WINDOW
    assert snap.pattern is not None and snap.pattern.stage == 2 and snap.pattern.low == D("97.90")
    assert snap.pattern1 is not None and snap.pattern1.low == D("98.60")  # recorded, discarded


def test_rule15_a_close_exactly_at_pattern1_low_is_not_below_it() -> None:
    at_low = sb(4, "98.90", "98.95", "98.50", "98.60")  # close == Pattern 1 low 98.60
    snap = mc.scan([*_pullback_with_hammer(), at_low, confirmation(5)])
    assert not snap.pattern1_disqualified and snap.phase is Phase.WINDOW


def test_rule15_pattern2_must_be_strictly_lower_than_pattern1() -> None:
    bars = [*_pullback_with_hammer(), drop(4, "98.00")]
    equal_low = sb(5, "99.00", "99.10", "98.60", "99.10")  # hammer-like but not lower than 98.60
    snap = mc.scan([*bars, equal_low, confirmation(6)])
    assert snap.phase is Phase.SETUP


def test_rule15_pattern2_low_uses_the_same_method_including_shelf_lowest_close() -> None:
    bars = [*_pullback_with_hammer(), drop(4, "98.00")]  # closes 98.05: discards Pattern 1 (98.60)
    base_candle = sb(5, "98.80", "98.85", "97.80", "98.30")  # not a hammer; closes within $0.25
    assert not mc.is_hammer(base_candle)
    snap = mc.scan([*bars, base_candle])
    assert snap.pattern is not None and snap.pattern.stage == 2
    assert snap.pattern.kind is PatternKind.SUPPORT_SHELF_BASE
    assert snap.pattern.low == D("98.05")  # the lowest CLOSE of the base (not the 97.80 wick)
    assert snap.pattern.bar_count == 2


def test_rule15_wicks_below_pattern2_are_allowed_but_a_close_below_it_cancels_the_setup() -> None:
    p2_path = [*_pullback_with_hammer(), drop(4, "98.00"), sb(5, "99.00", "99.10", "97.90", "99.10")]
    wick = sb(6, "99.10", "99.20", "97.00", "99.00")  # wick far below the Pattern 2 low, closes above
    snap = mc.scan([*p2_path, wick])
    assert snap.phase is Phase.SETUP and snap.pattern is not None and snap.pattern.stage == 2
    close_at = sb(6, "99.00", "99.05", "97.80", "97.90")  # close == Pattern 2 low: not below
    assert mc.scan([*p2_path, close_at]).phase is Phase.SETUP
    close_below = sb(6, "99.00", "99.05", "97.80", "97.85")
    snap = mc.scan([*p2_path, close_below])
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "PATTERN2_CLOSE_BREAK"


def test_rule15_no_third_pattern_replaces_pattern2_and_a_lower_close_cancels() -> None:
    p2_path = [*_pullback_with_hammer(), drop(4, "98.00"), sb(5, "99.00", "99.10", "97.90", "99.10")]
    third = sb(6, "98.50", "98.60", "97.50", "98.60")  # a lower-wick hammer that closes above P2 low
    assert mc.is_hammer(third)
    snap = mc.scan([*p2_path, third])
    assert snap.pattern is not None and snap.pattern.stage == 2 and snap.pattern.low == D("97.90")
    # a third, successively lower pattern after a close below Pattern 2's low: cancelled already
    lower = sb(6, "98.00", "98.10", "97.00", "97.80")
    assert mc.is_hammer(lower)
    snap = mc.scan([*p2_path, lower])
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "PATTERN2_CLOSE_BREAK"


def test_rule15_a_window_candle_closing_below_pattern2_low_cancels_before_any_fill() -> None:
    p2_path = [*_pullback_with_hammer(), drop(4, "98.00"), sb(5, "99.00", "99.10", "97.90", "99.10"),
               confirmation(6)]
    assert mc.scan(p2_path).phase is Phase.WINDOW
    snap = mc.scan([*p2_path, sb(7, "100.0", "100.1", "97.0", "97.80")])
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "PATTERN2_CLOSE_BREAK"


# ------------------------------------------------------------------ Rules 26-31: confirmation


def test_rule26_27_confirmation_candle_geometry() -> None:
    base = _pullback_with_hammer()
    ok = mc.scan([*base, confirmation(4)])
    assert ok.phase is Phase.WINDOW
    red = sb(4, "100.30", "100.35", "99.90", "100.20")
    assert mc.scan([*base, red]).phase is Phase.SETUP  # must be green (close > open)
    doji = sb(4, "100.20", "100.35", "99.90", "100.20")
    assert mc.scan([*base, doji]).phase is Phase.SETUP
    above = sb(4, "100.10", "100.40", "100.05", "100.30")
    assert mc.scan([*base, above]).phase is Phase.SETUP  # never touched VWAP
    touch = sb(4, "100.05", "100.40", "100.00", "100.30")  # low == VWAP counts
    assert mc.scan([*base, touch]).phase is Phase.WINDOW
    not_above = sb(4, "99.90", "100.10", "99.85", "100.00")
    assert mc.scan([*base, not_above]).phase is Phase.SETUP  # close must be strictly above VWAP


def test_rule28_candle_c_may_open_above_vwap_and_follow_an_above_vwap_bar() -> None:
    base = _pullback_with_hammer()
    weak_pre = sb(4, "99.95", "100.05", "99.90", "100.01")  # green; closes just above VWAP
    snap = mc.scan([*base, weak_pre])
    # The first candle that satisfies all confirmation requirements IS the confirmation.
    assert snap.phase is Phase.WINDOW and snap.confirm_close == D("100.01")
    opens_above = sb(4, "100.10", "100.40", "100.00", "100.30")  # opens above VWAP, low == VWAP
    assert mc.scan([*base, opens_above]).phase is Phase.WINDOW
    after_above_close = [*base, sb(4, "99.90", "100.10", "99.85", "100.05"), ]
    assert mc.scan(after_above_close).phase is Phase.WINDOW  # prior candle need not close below


def test_rule29_candle_c_must_start_within_thirty_minutes_of_the_setup_start() -> None:
    bars = _pullback_with_hammer() + [bearish(m) for m in range(4, 30)]
    assert mc.scan([*bars, confirmation(30)]).phase is Phase.WINDOW  # exactly 30:00 later
    assert mc.scan([*bars, bearish(30), confirmation(31)]).phase is Phase.NONE


def test_rule30_first_qualifying_candle_wins_and_close_and_ceiling_are_frozen() -> None:
    base = _pullback_with_hammer()
    first = confirmation(4, close="100.20")
    snap = mc.scan([*base, first, confirmation(5, close="100.60")])
    assert snap.confirm_close == D("100.20") and snap.confirm_start == T0 + 4 * MIN
    assert snap.entry_ceiling == D("100.50")  # Candle C's session VWAP (100) + 0.50
    # VWAP drifting afterwards changes nothing: the ceiling was fixed when C completed
    drifted = confirmation(5, close="100.30", low="100.10", open_="100.15")
    drifted = replace(drifted, vwap=D("99.80"))
    later = mc.scan([*base, first, drifted])
    assert (later.confirm_close, later.entry_ceiling) == (D("100.20"), D("100.50"))


def test_rule31_candle_c_cannot_be_the_entry_candle() -> None:
    snap, bars = window_snapshot()
    assert mc.window_number(snap.confirm_end, bars[-1].start) == 0  # C itself is not in 1..5
    done = passing_context(bars[:-1])
    decision = mc.bar_entry(snap, done, bars[-1], qqq_pair(4))
    assert not decision.enter and decision.reason == "OUTSIDE_ENTRY_WINDOW"


# ------------------------------------------------------------------ Rules 32-38: entry window


def window_snapshot(close: str = "100.20", offset: int = 0) -> tuple[mc.Snapshot, list[SessionBar]]:
    """Setup + Candle C (09:34 + ``offset``); C+1 starts one minute after C."""
    bars = [*_pullback_with_hammer(offset), confirmation(offset + 4, close=close)]
    return mc.scan(bars), bars


def passing_context(bars: list[SessionBar], e9_step: str = "0.1") -> list[SessionBar]:
    """Give every completed bar bullish-widening EMAs so the Rule 39 filter passes."""
    return [
        replace(bar, ema9=D("99.0") + D(e9_step) * i, ema20=D("99.0"))
        for i, bar in enumerate(bars)
    ]


def qb(minute: int, e9: str, e20: str, vwap: str = "500") -> SessionBar:
    """A QQQ candle whose price sits far BELOW its own VWAP (irrelevant under Rule 40)."""
    return sb(minute, "400", "401", "399", "400", vwap=vwap, ema9=e9, ema20=e20)


def qqq_pair(entry_minute: int, passing: bool = True) -> list[SessionBar]:
    """The two QQQ candles completed just before the entry candle starting at ``entry_minute``."""
    if passing:
        return [qb(entry_minute - 2, "401.0", "400.0"), qb(entry_minute - 1, "401.5", "400.0")]
    return [qb(entry_minute - 2, "401.0", "400.0"), qb(entry_minute - 1, "400.5", "400.0")]


def window_bar(minute: int, o: str, h: str, low: str, c: str, vwap: str = "100") -> SessionBar:
    return sb(minute, o, h, low, c, vwap=vwap)


def enter(snap, done, bar, qqq=None):  # noqa: ANN001, ANN201
    return mc.bar_entry(snap, done, bar, qqq if qqq is not None else qqq_pair(_minute(bar)))


def _minute(bar: SessionBar) -> int:
    return int((bar.start - T0) // MIN)


def test_rule32_entry_is_allowed_only_in_c_plus_one_through_c_plus_five() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    for n, expected in ((1, True), (5, True), (6, False)):
        bar = window_bar(4 + n, "100.30", "100.50", "100.10", "100.40")
        assert enter(snap, done, bar).enter is expected, n


def test_rule33_scenario_one_requires_confirmation_close_to_ceiling_and_the_vwap_benchmark() -> None:
    snap, bars = window_snapshot()  # confirmation close 100.20, ceiling 100.50
    assert snap.scenario is EntryScenario.WITHIN_CEILING
    done = passing_context(bars)
    entry = enter(snap, done, window_bar(5, "100.30", "100.50", "100.10", "100.40"))
    assert entry.enter and entry.mode is EntryMode.OPEN and entry.price == D("100.30")
    assert entry.at == T0 + 5 * MIN and entry.window_number == 1
    assert entry.facts["entry_scenario"] == "SCENARIO_1"
    # open == confirmation close qualifies (>=); open == ceiling qualifies (<=)
    assert enter(snap, done, window_bar(5, "100.20", "100.50", "100.10", "100.40")).enter
    assert enter(snap, done, window_bar(5, "100.50", "100.70", "100.40", "100.60")).price == D("100.50")
    # a candle that stays above the ceiling never fills; one that stays below the floor neither
    assert not enter(snap, done, window_bar(5, "100.60", "100.90", "100.51", "100.70")).enter
    assert not enter(snap, done, window_bar(5, "100.10", "100.19", "100.00", "100.15")).enter


def test_rule33_the_ceiling_is_the_frozen_value_not_the_developing_vwap() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    # the window candle's own VWAP is far higher/lower: only Candle C's VWAP + 0.50 matters
    high_vwap = window_bar(5, "100.30", "100.60", "100.25", "100.40", vwap="101.90")
    assert enter(snap, done, high_vwap).enter
    low_vwap = window_bar(5, "100.60", "100.80", "100.55", "100.70", vwap="99.00")
    assert not enter(snap, done, low_vwap).enter  # VWAP + 0.50 would be 99.50; frozen is 100.50


def test_rule33_equality_of_confirmation_close_and_ceiling_is_scenario_one() -> None:
    snap, bars = window_snapshot(close="100.50")  # C closes exactly at VWAP 100 + 0.50
    assert snap.confirm_close == snap.entry_ceiling == D("100.50")
    assert snap.scenario is EntryScenario.WITHIN_CEILING
    done = passing_context(bars)
    assert enter(snap, done, window_bar(5, "100.50", "100.60", "100.40", "100.55")).enter
    assert not enter(snap, done, window_bar(5, "100.49", "100.49", "100.40", "100.45")).enter


def test_rule33_35_a_later_move_into_the_range_triggers_the_entry() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    # opens below the confirmation close and trades through it: fill modeled at the lowest
    # qualifying price, stamped at the candle end, even though the candle closes red
    red_trigger = window_bar(5, "100.10", "100.25", "100.05", "100.08")
    entry = enter(snap, done, red_trigger)
    assert entry.enter and entry.mode is EntryMode.TRIGGER
    assert entry.price == D("100.20") and entry.at == red_trigger.end
    # high one cent short of the floor: nothing
    assert not enter(snap, done, window_bar(5, "100.10", "100.19", "100.05", "100.15")).enter
    # opens above the ceiling, falls back into the range: fill modeled at the ceiling (the worst
    # qualifying price) - the entry is not cancelled by having been above the range
    fall = window_bar(5, "100.80", "100.90", "100.40", "100.45")
    entry = enter(snap, done, fall)
    assert entry.enter and entry.mode is EntryMode.TRIGGER and entry.price == D("100.50")
    # later candle of the window as well
    assert enter(snap, done, window_bar(8, "100.80", "100.90", "100.40", "100.45")).enter


def test_rule33_trigger_is_refused_when_the_candles_own_low_may_precede_it_with_a_dip_breach() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    # the low (95.50) breaches the $3.00 dip against this candle's VWAP 100; it may have printed
    # BEFORE the price rose into the band, so nothing is guessed (open fills are unaffected)
    risky = window_bar(5, "100.10", "100.25", "95.50", "100.20")
    decision = enter(snap, done, risky)
    assert not decision.enter and decision.reason == "DIP_BEFORE_TRIGGER"
    assert enter(snap, done, window_bar(5, "100.30", "100.40", "95.50", "100.20")).enter


def window_snapshot_two(offset: int = 0) -> tuple[mc.Snapshot, list[SessionBar]]:
    return window_snapshot(close="101.20", offset=offset)  # ceiling 100.50 < close


def test_rule34_scenario_two_waits_for_a_pullback_and_may_enter_below_the_confirmation_close() -> None:
    snap, bars = window_snapshot_two()
    assert snap.scenario is EntryScenario.ABOVE_CEILING and snap.phase is Phase.WINDOW
    done = passing_context(bars)  # last completed (Candle C): VWAP 100, EMA9 99.4, EMA20 99.0
    # price at/above the confirmation close is above the ceiling: the setup waits (not cancelled)
    assert not enter(snap, done, window_bar(5, "101.30", "101.60", "101.00", "101.40")).enter
    # pulled back to the ceiling: entry BELOW the confirmation close (101.20) is allowed
    inside = enter(snap, done, window_bar(5, "100.40", "100.55", "100.30", "100.45"))
    assert inside.enter and inside.mode is EntryMode.OPEN and inside.price == D("100.40")
    assert inside.price < snap.confirm_close
    assert inside.facts["entry_scenario"] == "SCENARIO_2"
    # exactly at the ceiling qualifies, one cent above does not
    assert enter(snap, done, window_bar(5, "100.50", "100.60", "100.45", "100.55")).enter
    assert not enter(snap, done, window_bar(5, "100.51", "100.60", "100.51", "100.55")).enter
    # a candle that opens above the ceiling and falls through it triggers at the ceiling
    fall = enter(snap, done, window_bar(5, "101.50", "101.60", "100.30", "100.40"))
    assert fall.enter and fall.mode is EntryMode.TRIGGER and fall.price == D("100.50")


def test_rule34_scenario_two_price_must_be_above_vwap_ema9_and_ema20_of_the_same_candle() -> None:
    snap, bars = window_snapshot_two()
    done = passing_context(bars)
    # all three indicators are taken from the most recently completed candle (Candle C here)
    last = replace(done[-1], vwap=D("100.00"), ema9=D("100.30"), ema20=D("99.00"))
    prior = replace(done[-2], ema9=D("99.00"), ema20=D("99.00"))  # momentum: widening above
    ctx = [*done[:-2], prior, last]
    assert not enter(snap, ctx, window_bar(5, "100.30", "100.30", "100.25", "100.28")).enter  # == EMA9
    assert enter(snap, ctx, window_bar(5, "100.31", "100.40", "100.25", "100.35")).enter
    # EMA20 is the highest of the three here: it binds (momentum: spread 0.01 -> 0.05, widening)
    last20 = replace(last, ema9=D("100.45"), ema20=D("100.40"))
    prior20 = replace(prior, ema9=D("100.31"), ema20=D("100.30"))
    ctx20 = [*done[:-2], prior20, last20]
    assert not enter(snap, ctx20, window_bar(5, "100.40", "100.40", "100.35", "100.38")).enter
    assert enter(snap, ctx20, window_bar(5, "100.46", "100.50", "100.40", "100.48")).enter
    # a high EMA on the candle BEFORE the latest completed one is irrelevant (same-candle rule)
    stale_high = replace(done[-2], ema9=D("99.00"), ema20=D("98.90"), vwap=D("100.00"))
    ok_ctx = [*done[:-2], stale_high, replace(done[-1], ema9=D("99.20"), ema20=D("99.00"))]
    assert enter(snap, ok_ctx, window_bar(5, "100.30", "100.40", "100.25", "100.35")).enter


def test_rule34_scenario_two_is_never_cancelled_by_the_price_being_above_the_ceiling() -> None:
    snap, bars = window_snapshot_two()
    gap = [*bars, window_bar(5, "101.50", "101.90", "101.40", "101.80")]  # closes above VWAP
    still = mc.scan(gap)
    assert still.phase is Phase.WINDOW and still.scenario is EntryScenario.ABOVE_CEILING


def test_rule36_price_must_be_strictly_above_the_last_completed_candles_vwap() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    # the benchmark is the most recently completed candle's VWAP, NOT the window candle's own
    done_hi = [*done[:-1], replace(done[-1], vwap=D("100.30"))]  # C+2 would see this benchmark
    assert enter(snap, done, window_bar(5, "100.25", "100.40", "100.20", "100.30")).enter
    assert not enter(snap, done_hi, window_bar(5, "100.30", "100.30", "100.25", "100.28")).enter
    assert enter(snap, done_hi, window_bar(5, "100.31", "100.40", "100.25", "100.30")).enter
    # when the benchmark VWAP is above the confirmation close the first qualifying tick is above it
    trig = enter(snap, done_hi, window_bar(5, "100.10", "100.35", "100.05", "100.20"))
    assert trig.enter and trig.price == D("100.31")
    # Scenario 2 shares the benchmark rule
    snap2, bars2 = window_snapshot_two()
    done2 = passing_context(bars2)
    assert not enter(snap2, done2, window_bar(5, "100.00", "100.00", "99.80", "99.90")).enter
    # empty band: the benchmark VWAP is at/above the ceiling
    done_x = [*done[:-1], replace(done[-1], vwap=D("100.60"))]
    assert enter(snap, done_x, window_bar(5, "100.55", "100.70", "100.50", "100.60")).reason == "EMPTY_PRICE_BAND"


def test_rule37_a_completed_window_candle_closing_below_vwap_cancels_before_any_later_fill() -> None:
    bars = [*_pullback_with_hammer(), confirmation(4), sb(5, "100.0", "100.1", "99.7", "99.80")]
    snap = mc.scan(bars)
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "ENTRY_WINDOW_CLOSE_BELOW_VWAP"
    wick_only = [*_pullback_with_hammer(), confirmation(4), sb(5, "100.3", "100.4", "99.50", "100.30")]
    assert mc.scan(wick_only).phase is Phase.WINDOW  # an intrabar wick below VWAP never cancels
    at_vwap = [*_pullback_with_hammer(), confirmation(4), sb(5, "100.3", "100.4", "99.9", "100.00")]
    assert mc.scan(at_vwap).phase is Phase.WINDOW  # a close exactly at VWAP is not below it


def test_rule38_c_plus_five_without_a_fill_expires_and_purges() -> None:
    held = [sb(5 + i, "100.30", "100.50", "100.10", "100.40") for i in range(4)]
    base = [*_pullback_with_hammer(), confirmation(4)]
    assert mc.scan([*base, *held]).phase is Phase.WINDOW  # C+4 done, C+5 still pending
    snap = mc.scan([*base, *held, sb(9, "100.30", "100.50", "100.10", "100.40")])
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "ENTRY_WINDOW_EXPIRED"


def test_rule38_entry_during_c_plus_six_is_never_allowed() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    decision = enter(snap, done, window_bar(10, "100.30", "100.50", "100.10", "100.40"))
    assert not decision.enter and decision.reason == "OUTSIDE_ENTRY_WINDOW"


def test_rule4_and_32_an_entry_candle_starting_at_1130_is_refused() -> None:
    snap, bars = window_snapshot(offset=115)  # Candle C = 11:29; C+1 starts 11:30
    assert snap.phase is Phase.WINDOW
    done = passing_context(bars)
    decision = enter(snap, done, window_bar(120, "100.30", "100.50", "100.10", "100.40"))
    assert not decision.enter and decision.reason == "OUTSIDE_ENTRY_HOURS"
    snap, bars = window_snapshot(offset=114)  # Candle C = 11:28; C+1 starts 11:29: allowed
    done = passing_context(bars)
    assert enter(snap, done, window_bar(119, "100.30", "100.50", "100.10", "100.40")).enter
    assert not mc.time_gate(T0 + 3 * MIN)  # no entry before 09:34:00


def _developing(minute: int, o: str, h: str, low: str, c: str) -> Candle:
    start = T0 + minute * MIN
    return Candle(
        "ABC", CandleTimeframe.M1, start, start + MIN, D(o), D(h), D(low), D(c), D("500"), "sip",
        True, CandleOrigin.STREAM_TRADE, PriceBasis.RAW, CandleStatus.DEVELOPING,
    )


def test_rule33_34_entry_can_happen_during_a_developing_candle_on_the_first_qualifying_print() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    qqq = qqq_pair(5)
    dev = _developing(5, "100.00", "100.10", "99.95", "100.05")
    at = T0 + 5 * MIN + timedelta(seconds=20)

    def live(price: str, ask: str | None = None, snapshot=snap, completed=done):  # noqa: ANN001, ANN202
        return mc.live_entry(
            snapshot, completed, dev, D(price), at, D("100.00"), None if ask is None else D(ask), qqq
        )

    assert not live("100.10").enter  # below the confirmation close (and not above the benchmark)
    first = live("100.20")
    assert first.enter and first.mode is EntryMode.LIVE and first.vwap == D("100")
    assert first.facts["entry_scenario"] == "SCENARIO_1"
    assert live("100.50").enter and not live("100.51").enter
    assert not live("100.30", ask="100.60").enter  # ask above the frozen ceiling defers the entry
    assert live("100.30", ask="100.40").enter
    snap2, bars2 = window_snapshot_two()
    done2 = passing_context(bars2)
    assert live("100.40", snapshot=snap2, completed=done2).enter  # Scenario 2 pullback
    assert not live("101.30", snapshot=snap2, completed=done2).enter


# ------------------------------------------------------------------ Rule 39: EMA momentum


def ema_bar(minute: int, e9: str, e20: str) -> SessionBar:
    return sb(minute, "100", "101", "99", "100", ema9=e9, ema20=e20)


@pytest.mark.parametrize(
    ("prior", "latest", "expected", "label"),
    [
        (("99.0", "99.5"), ("99.6", "99.5"), True, "BULLISH_CROSSOVER"),
        (("99.5", "99.5"), ("99.6", "99.5"), True, "BULLISH_CROSSOVER"),  # touch counts
        (("99.0", "99.5"), ("99.5", "99.5"), True, "BULLISH_CROSSOVER"),  # equality on L counts
        (("100.0", "99.5"), ("100.4", "99.5"), True, "BULLISH_WIDENING"),
        (("99.0", "99.5"), ("99.2", "99.5"), True, "BEARISH_CONVERGING"),
        (("100.0", "99.5"), ("100.2", "100.0"), False, "NO_MOMENTUM"),  # above but narrowing
        (("100.0", "99.5"), ("100.0", "99.5"), False, "NO_MOMENTUM"),  # above, unchanged spread
        (("100.0", "99.5"), ("99.4", "99.5"), False, "BEARISH_ROLLOVER"),
        (("100.0", "99.5"), ("99.5", "99.5"), False, "BEARISH_ROLLOVER"),  # L equality after above
        (("99.0", "99.5"), ("98.8", "99.5"), False, "NO_MOMENTUM"),  # widening while below
        (("99.0", "99.5"), ("99.0", "99.5"), False, "NO_MOMENTUM"),  # unchanged bearish gap
        (("99.5", "99.5"), ("99.5", "99.5"), False, "FLAT_EQUAL"),  # equal on both candles
        (("99.5", "99.5"), ("99.4", "99.5"), False, "NO_MOMENTUM"),  # equal then below
    ],
)
def test_rule39_ema_momentum_variants(prior, latest, expected, label) -> None:  # noqa: ANN001
    ok, name = mc.ema_momentum(ema_bar(0, *prior), ema_bar(1, *latest))
    assert (ok, name) == (expected, label)


def test_rule39_missing_emas_fail_closed() -> None:
    assert mc.ema_momentum(ema_bar(0, "99", "99.5"), sb(1, "1", "2", "1", "1", ema9=None)) == (
        False, "UNAVAILABLE",
    )


def test_rule39_a_failing_stock_momentum_blocks_the_entry() -> None:
    snap, bars = window_snapshot()
    flat = [replace(bar, ema9=D("99.0"), ema20=D("99.0")) for bar in bars]
    decision = enter(snap, flat, window_bar(5, "100.30", "100.50", "100.10", "100.40"))
    assert not decision.enter and decision.reason == "EMA_MOMENTUM_FAILED"


# ------------------------------------------------------------------ Rule 40: QQQ momentum


def test_rule40_qqq_uses_the_identical_momentum_test_on_its_two_latest_candles() -> None:
    entry = T0 + 5 * MIN
    assert mc.qqq_momentum(entry, qqq_pair(5))[0]
    assert not mc.qqq_momentum(entry, qqq_pair(5, passing=False))[0]  # above but narrowing
    cases = [
        (("400.0", "400.5"), ("400.6", "400.5"), True),  # crossover
        (("401.0", "400.0"), ("400.5", "400.0"), False),  # above, narrowing
        (("399.0", "400.0"), ("399.5", "400.0"), True),  # below, converging
        (("399.0", "400.0"), ("398.5", "400.0"), False),  # below, widening
        (("400.0", "400.0"), ("400.0", "400.0"), False),  # equal on both
        (("401.0", "400.0"), ("399.9", "400.0"), False),  # bearish rollover
    ]
    for prior, latest, expected in cases:
        pair = [qb(3, *prior), qb(4, *latest)]
        assert mc.qqq_momentum(entry, pair)[0] is expected, (prior, latest)


def test_rule40_qqq_does_not_need_to_be_above_or_below_its_vwap() -> None:
    entry = T0 + 5 * MIN
    for vwap in ("1", "400", "900"):  # price below, at and far above the QQQ VWAP
        pair = [qb(3, "401.0", "400.0", vwap=vwap), qb(4, "401.5", "400.0", vwap=vwap)]
        assert mc.qqq_momentum(entry, pair)[0]


def test_rule40_missing_stale_or_unseeded_qqq_data_fails_closed() -> None:
    entry = T0 + 5 * MIN
    assert mc.qqq_momentum(entry, [])[1]["qqq_result"] == "MISSING_CANDLES"
    assert mc.qqq_momentum(entry, qqq_pair(5)[1:])[1]["qqq_result"] == "MISSING_CANDLES"
    late = [qb(1, "401.0", "400.0"), qb(2, "401.5", "400.0")]  # two minutes behind the entry candle
    assert mc.qqq_momentum(entry, late)[1]["qqq_result"] == "STALE_CANDLES"
    unseeded = [qqq_pair(5)[0], sb(4, "400", "401", "399", "400", ema9=None)]
    assert not mc.qqq_momentum(entry, unseeded)[0]


def test_rule40_qqq_must_pass_for_every_entry_and_blocks_when_it_fails() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    bar = window_bar(5, "100.30", "100.50", "100.10", "100.40")
    assert enter(snap, done, bar).enter
    blocked = enter(snap, done, bar, qqq_pair(5, passing=False))
    assert not blocked.enter and blocked.reason == "QQQ_FILTER_FAILED"
    assert enter(snap, done, bar, []).reason == "QQQ_FILTER_FAILED"
    # also for later candles of the window
    assert not enter(snap, done, window_bar(8, "100.30", "100.50", "100.10", "100.40"),
                     qqq_pair(8, passing=False)).enter


# ------------------------------------------------------------------ Rule 41: VWAP slope


def test_rule41_a_flat_or_falling_vwap_never_blocks_setup_confirmation_or_entry() -> None:
    base = _pullback_with_hammer()
    falling = [replace(bar, vwap=D(v)) for bar, v in zip(base, ("101.0", "100.8", "100.6", "100.4"))]
    falling.append(replace(confirmation(4, close="100.20"), vwap=D("100.00")))
    snap = mc.scan(falling)
    assert snap.phase is Phase.WINDOW  # a declining VWAP: setup, pattern and confirmation stand
    done = passing_context(falling)
    assert enter(snap, done, window_bar(5, "100.30", "100.50", "100.10", "100.40")).enter
    flat = [replace(b, vwap=D("100")) for b in [*base, confirmation(4)]]
    assert mc.scan(flat).phase is Phase.WINDOW


# ------------------------------------------------------------------ Rules 49-50: purge and reset


@pytest.mark.parametrize(
    ("bars", "reason"),
    [
        ([bearish(0), bearish(1, low="96.00")], "MAX_DIP_BREACHED"),
        (
            [*_pullback_with_hammer(), drop(4, "98.00"),
             sb(5, "99.00", "99.10", "97.90", "99.10"), sb(6, "99.0", "99.1", "97.8", "97.85")],
            "PATTERN2_CLOSE_BREAK",
        ),
        (
            [*_pullback_with_hammer(), confirmation(4), sb(5, "100.0", "100.1", "99.7", "99.80")],
            "ENTRY_WINDOW_CLOSE_BELOW_VWAP",
        ),
        (
            [*_pullback_with_hammer(), confirmation(4),
             *[sb(5 + i, "100.3", "100.5", "100.1", "100.4") for i in range(5)]],
            "ENTRY_WINDOW_EXPIRED",
        ),
    ],
)
def test_rule49_every_invalidation_purges_all_setup_state(bars, reason) -> None:  # noqa: ANN001
    snap = mc.scan(bars)
    assert snap.phase is Phase.NONE and snap.last_purge_reason == reason
    # nothing of the old setup survives in the snapshot
    for name in ("setup_start", "setup_low", "setup_low_vwap", "pattern", "pattern1",
                 "confirm_start", "confirm_end", "confirm_close", "entry_ceiling"):
        assert getattr(snap, name) is None, name
    assert not snap.pullback_ok and not snap.pattern1_disqualified


def test_rule49_the_expired_clock_purges_the_old_setup_and_only_a_fresh_one_can_follow() -> None:
    old = [*_pullback_with_hammer(), *[bearish(m) for m in range(4, 31)]]
    snap = mc.scan([*old, bearish(31)])  # the candle starting 31:00 after the setup start
    assert snap.last_purge_reason == "CONFIRMATION_CLOCK_EXPIRED"
    # the new setup begins at that later candle: no old low, pattern or pullback survives
    assert snap.setup_start == T0 + 31 * MIN and snap.setup_low == D("98.90")
    assert snap.pattern is None and not snap.pullback_ok
    assert mc.scan([*old, bearish(31), confirmation(32)]).phase is Phase.SETUP


def test_rule49_a_filled_trade_that_later_exits_purges_everything_via_the_barrier() -> None:
    bars = [*_pullback_with_hammer(), confirmation(4)]
    assert mc.scan(bars).phase is Phase.WINDOW
    barrier = bars[-1].end  # the engine records the consumption/exit instant
    assert mc.scan(bars, barrier).phase is Phase.NONE


def test_rule50_a_new_setup_cannot_reuse_earlier_pullback_candles_or_patterns() -> None:
    purge_bar = bearish(5, low="90.00")  # dip breach
    cycle = [*_pullback_with_hammer(), purge_bar, confirmation(6)]
    snap = mc.scan(cycle)
    assert snap.phase is Phase.NONE  # old hammer / pullback cannot be reused by the confirmation
    # a fresh alignment after the purge needs its own three closes AND its own pattern
    fresh_no_pattern = [*cycle, drop(7, "98.9"), drop(8, "98.3"), drop(9, "97.7"), confirmation(10)]
    snap = mc.scan(fresh_no_pattern)
    assert snap.phase is Phase.SETUP and snap.setup_start == T0 + 7 * MIN and snap.pattern is None
    fresh_ok = [*cycle, *[hammer(7)], bearish(8), bearish(9), confirmation(10)]
    snap = mc.scan(fresh_ok)
    assert snap.phase is Phase.WINDOW and snap.setup_start == T0 + 7 * MIN
    assert snap.pattern is not None and snap.pattern.formed_at == T0 + 7 * MIN


def test_rule50_nothing_from_before_a_purge_or_barrier_is_reused() -> None:
    bars = [*_pullback_with_hammer(), confirmation(4)]
    assert mc.scan(bars).phase is Phase.WINDOW
    barrier = bars[-1].end
    assert mc.scan(bars, barrier).phase is Phase.NONE
    again = [*bars, *[bearish(10 + i) for i in range(4)]]
    snap = mc.scan(again, barrier)
    assert snap.phase is Phase.SETUP and snap.setup_start == T0 + 10 * MIN
    assert snap.pattern is not None and snap.pattern.formed_at >= T0 + 10 * MIN  # fresh pattern
    assert snap.confirm_close is None and snap.entry_ceiling is None
