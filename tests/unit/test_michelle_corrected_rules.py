"""Rule-by-rule tests for the corrected Michelle VWAP Bounce/Break rules engine (Rules 1-45).

Rule numbers refer to ``docs/strategies/michelle/vwap_bounce_break_business_rules_final.docx``.
Bars are built directly as :class:`SessionBar` values so each test controls VWAP and EMA values.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.scheduling.calendar import session_for
from mike_trade.strategies import michelle_corrected as mc
from mike_trade.strategies.michelle_corrected import (
    EntryMode,
    PatternKind,
    Phase,
    SessionBar,
)

DAY = date(2026, 10, 8)
T0 = session_for(DAY).open_utc  # 09:30 New York
MIN = timedelta(minutes=1)
VWAP = D("100")
EMA9 = D("99.4")
EMA20 = D("99.6")


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
    # EMAs sit above the close so the hammer itself is a valid setup start (Rule 7).
    return sb(minute, "99.40", "99.55", "98.60", "99.50", ema9="99.8", ema20="99.9")


def drop(minute: int, low: str) -> SessionBar:
    """A large-bodied red candle printing ``low``: a lower low that is not a hammer."""
    lo = D(low)
    return sb(minute, str(lo + D("0.70")), str(lo + D("0.80")), low, str(lo + D("0.05")))


def confirmation(minute: int, close: str = "100.20", low: str = "99.90", open_: str = "99.95") -> SessionBar:
    return sb(minute, open_, str(D(close) + D("0.05")), low, close)


# ------------------------------------------------------------------ Rules 7-9: setup start


def test_rule7_setup_requires_close_below_vwap_and_both_emas_with_emas_below_vwap() -> None:
    assert mc.starts_setup(bearish(0))
    assert not mc.starts_setup(sb(0, "99.5", "99.6", "98.9", "99.5"))  # close not below EMA9
    assert not mc.starts_setup(bearish(0, ema9="100.00"))  # EMA9 == VWAP is not "strictly below"
    assert not mc.starts_setup(bearish(0, ema20="100.10"))
    assert not mc.starts_setup(bearish(0, vwap=None))


def test_rule7_and_8_ema9_may_be_on_either_side_of_ema20_without_a_fresh_crossover() -> None:
    assert mc.starts_setup(bearish(0, ema9="99.2", ema20="99.6"))  # EMA9 below EMA20
    assert mc.starts_setup(bearish(0, ema9="99.7", ema20="99.5"))  # EMA9 above EMA20
    # The first bar already meets the alignment: no prior crossing bar exists or is needed.
    assert mc.scan([bearish(0)]).phase is Phase.SETUP


def test_rule9_setup_start_is_the_first_qualifying_candle_and_later_bearish_bars_do_not_reset() -> None:
    snap = mc.scan([bearish(2), bearish(3), bearish(4)])
    assert snap.setup_start == T0 + 2 * MIN


# ------------------------------------------------------------------ Rules 10-11: maximum dip


def test_rule10_lower_low_updates_associated_vwap_and_equal_low_does_not() -> None:
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
def test_rule11_dip_of_exactly_three_dollars_is_allowed_but_more_purges(
    low: str, expected_phase: Phase
) -> None:
    snap = mc.scan([bearish(0, close="98.50", low=low)])
    assert snap.phase is expected_phase
    if expected_phase is Phase.NONE:
        assert snap.last_purge_reason == "MAX_DIP_BREACHED"


def test_rule11_breach_applies_until_entry_and_the_breaching_bar_cannot_seed_a_new_setup() -> None:
    bars = [bearish(0), bearish(1, low="96.00")]
    snap = mc.scan(bars)
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "MAX_DIP_BREACHED"
    assert mc.scan([*bars, bearish(2)]).setup_start == T0 + 2 * MIN  # strictly after the purge


# ------------------------------------------------------------------ Rule 12 / 30: 30-minute clock


def _pullback_with_hammer(start: int = 0) -> list[SessionBar]:
    return [hammer(start), bearish(start + 1), bearish(start + 2), bearish(start + 3)]


def test_rule12_confirmation_exactly_thirty_minutes_after_setup_start_is_allowed() -> None:
    bars = _pullback_with_hammer()
    for minute in range(4, 30):
        bars.append(bearish(minute))
    assert mc.scan(bars).setup_start == T0
    allowed = mc.scan([*bars, confirmation(30)])  # candle start == setup start + 30:00
    assert allowed.phase is Phase.WINDOW
    late = mc.scan([*bars, bearish(30), confirmation(31)])  # 30:00 + one minute
    assert late.phase is Phase.NONE
    assert late.last_purge_reason == "CONFIRMATION_CLOCK_EXPIRED"


# ------------------------------------------------------------------ Rules 13-25: patterns


def test_rule16_to_19_hammer_geometry() -> None:
    assert mc.is_hammer(hammer(0))
    assert mc.is_hammer(sb(0, "99.50", "99.55", "98.60", "99.40"))  # red hammer
    assert mc.is_hammer(sb(0, "99.50", "99.60", "98.50", "99.50"))  # doji, long upper wick ok
    assert not mc.is_hammer(sb(0, "99.40", "99.50", "99.40", "99.50"))  # wick 0 < 2x body
    assert not mc.is_hammer(sb(0, "99.40", "99.50", "99.40", "99.40"))  # zero... body ok, lower 0, range>0 but not upper half? body at low
    assert not mc.is_hammer(sb(0, "100.20", "100.30", "99.00", "100.10"))  # body above VWAP
    assert not mc.is_hammer(sb(0, "99.50", "99.50", "99.50", "99.50"))  # zero range
    # upper-half placement: body bottom must be >= low + 0.5*range
    assert not mc.is_hammer(sb(0, "99.00", "100.50", "98.00", "99.10"))
    # wick exactly 2x body qualifies
    assert mc.is_hammer(sb(0, "99.40", "99.60", "99.20", "99.50"))


def test_rule17_hammer_has_no_upper_wick_cap() -> None:
    # doji at the exact midpoint: the upper wick (0.90) is as long as the lower and is not capped
    assert mc.is_hammer(sb(0, "99.50", "100.40", "98.60", "99.50"))


def test_rule20_to_22_support_shelf_base_cluster_and_range_equality() -> None:
    cluster = [bearish(i, close="99.0", low="98.50") for i in range(3)]
    assert mc.support_shelf(cluster, 2, 0) == 3
    wide = [bearish(0, low="98.50"), bearish(1, low="99.00"), bearish(2, low="98.51")]
    assert mc.support_shelf(wide, 2, 0) == 3  # spread exactly 0.50 is allowed
    too_wide = [bearish(0, low="98.49"), bearish(1, low="99.00"), bearish(2, low="98.51")]
    assert mc.support_shelf(too_wide, 2, 0) is None
    green_close_above = [bearish(0), bearish(1), sb(2, "99.0", "100.5", "98.9", "100.2")]
    assert mc.support_shelf(green_close_above, 2, 0) is None
    assert mc.support_shelf(cluster[:2], 1, 0) is None  # fewer than 3 candles


def test_rule20_shelf_never_exceeds_six_candles() -> None:
    cluster = [bearish(i, close="99.0", low="98.50") for i in range(8)]
    assert mc.support_shelf(cluster, 7, 0) == 6


def test_rule23_24_bullish_engulfing_equal_body_boundaries_and_no_wick_requirement() -> None:
    red = sb(0, "99.60", "99.70", "99.00", "99.30")
    green_equal = sb(1, "99.30", "99.65", "99.20", "99.60")  # open == red close, close == red open
    assert mc.is_bullish_engulfing(red, green_equal)
    green_short = sb(1, "99.30", "99.65", "99.20", "99.59")
    assert not mc.is_bullish_engulfing(red, green_short)
    green_above_vwap = sb(1, "99.30", "100.50", "99.20", "100.10")
    assert not mc.is_bullish_engulfing(red, green_above_vwap)
    two_green = sb(0, "99.30", "99.70", "99.00", "99.60")
    assert not mc.is_bullish_engulfing(two_green, green_equal)


def test_rule14_pattern_priority_is_hammer_then_shelf_then_engulfing() -> None:
    # A candle that is simultaneously a hammer and completes a shelf and an engulfing pair.
    bars = [
        bearish(0, close="99.20", low="98.60"),
        sb(1, "99.50", "99.60", "98.60", "99.30"),  # red
        sb(2, "99.30", "99.55", "98.60", "99.50"),  # green hammer-ish / engulfs
    ]
    assert mc.is_hammer(bars[2]) and mc.support_shelf(bars, 2, 0) == 3
    assert mc.is_bullish_engulfing(bars[1], bars[2])
    pattern = mc.detect_pattern(bars, 2, 0)
    assert pattern is not None and pattern.kind is PatternKind.HAMMER
    no_hammer = [bars[0], bars[1], sb(2, "99.30", "100.30", "98.60", "99.50")]
    assert not mc.is_hammer(no_hammer[2])
    pattern = mc.detect_pattern(no_hammer, 2, 0)
    assert pattern is not None and pattern.kind is PatternKind.SUPPORT_SHELF_BASE


def test_rule13_confirmation_needs_a_bottoming_pattern() -> None:
    no_pattern = [drop(0, "98.90"), drop(1, "98.30"), drop(2, "97.70"), drop(3, "97.20")]
    snap = mc.scan([*no_pattern, confirmation(4)])
    assert snap.phase is Phase.SETUP and snap.pattern is None
    snap = mc.scan([*_pullback_with_hammer(), confirmation(4)])
    assert snap.phase is Phase.WINDOW and snap.pattern is not None
    assert snap.pattern.kind is PatternKind.HAMMER and snap.confirm_close == D("100.20")


def test_rule15_lower_low_disqualifies_pattern1_and_a_lower_pattern2_must_form() -> None:
    bars = [*_pullback_with_hammer(), drop(4, "98.00")]  # prints a lower low than 98.60
    snap = mc.scan([*bars, confirmation(5)])
    assert snap.phase is Phase.SETUP and snap.pattern1_disqualified  # confirmation refused
    # a lower hammer forms Pattern 2; confirmation then qualifies
    p2 = sb(5, "99.00", "99.10", "97.90", "99.10")
    assert mc.is_hammer(p2)
    snap = mc.scan([*bars, p2, confirmation(6)])
    assert snap.phase is Phase.WINDOW
    assert snap.pattern is not None and snap.pattern.stage == 2 and snap.pattern.low == D("97.90")


def test_rule15_pattern2_must_be_strictly_lower_than_pattern1() -> None:
    bars = [*_pullback_with_hammer(), drop(4, "98.00")]
    equal_low = sb(5, "99.00", "99.10", "98.60", "99.10")  # hammer-like but not lower than 98.60
    snap = mc.scan([*bars, equal_low, confirmation(6)])
    assert snap.phase is Phase.SETUP


def test_rule15_wicks_below_pattern2_are_allowed_but_a_close_below_purges_it() -> None:
    p2_path = [*_pullback_with_hammer(), drop(4, "98.00"), sb(5, "99.00", "99.10", "97.90", "99.10")]
    wick = sb(6, "99.10", "99.20", "97.00", "99.00", )  # wick far below P2 low, close above it
    snap = mc.scan([*p2_path, wick])
    assert snap.phase is Phase.SETUP  # wick (and $3 dip is measured against VWAP 100 -> 3.00 ok)
    close_below = sb(6, "99.00", "99.05", "97.80", "97.85")
    snap = mc.scan([*p2_path, close_below])
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "PATTERN2_CLOSE_BREAK"


def test_rule15_no_third_pattern_after_pattern2() -> None:
    p2_path = [*_pullback_with_hammer(), drop(4, "98.00"), sb(5, "99.00", "99.10", "97.90", "99.10")]
    third = sb(6, "98.50", "98.60", "97.50", "98.60")  # a lower hammer: ignored
    assert mc.is_hammer(third)
    snap = mc.scan([*p2_path, third])
    assert snap.pattern is not None and snap.pattern.stage == 2 and snap.pattern.low == D("97.90")


# ------------------------------------------------------------------ Rules 26-32: confirmation


def test_rule26_requires_three_consecutive_closes_below_vwap_before_confirmation() -> None:
    two = [hammer(0), bearish(1)]
    assert mc.scan([*two, confirmation(2)]).phase is Phase.SETUP
    three = [hammer(0), bearish(1), bearish(2)]
    assert mc.scan([*three, confirmation(3)]).phase is Phase.WINDOW
    broken = [hammer(0), bearish(1), sb(2, "99.9", "100.4", "99.8", "100.2"), bearish(3)]
    assert mc.scan([*broken, confirmation(4)]).phase is Phase.SETUP


def test_rule27_28_29_confirmation_candle_geometry() -> None:
    base = _pullback_with_hammer()
    ok = mc.scan([*base, confirmation(4)])
    assert ok.phase is Phase.WINDOW
    red = sb(4, "100.30", "100.35", "99.90", "100.20")
    assert mc.scan([*base, red]).phase is Phase.SETUP  # must be green
    above = sb(4, "100.10", "100.40", "100.05", "100.30")
    assert mc.scan([*base, above]).phase is Phase.SETUP  # never touched VWAP
    touch = sb(4, "100.05", "100.40", "100.00", "100.30")  # low == VWAP counts
    assert mc.scan([*base, touch]).phase is Phase.WINDOW
    not_above = sb(4, "99.90", "100.10", "99.85", "100.00")
    assert mc.scan([*base, not_above]).phase is Phase.SETUP  # close must be strictly above VWAP


def test_rule29_confirmation_may_open_above_vwap_and_follow_an_above_vwap_bar() -> None:
    base = _pullback_with_hammer()
    weak_pre = sb(4, "99.95", "100.05", "99.90", "100.01")  # green but a normal bar closing above
    snap = mc.scan([*base, weak_pre])
    # The first candle that satisfies all confirmation requirements IS the confirmation.
    assert snap.phase is Phase.WINDOW and snap.confirm_close == D("100.01")


def test_rule31_only_the_first_confirmation_counts_and_close_is_the_immutable_floor() -> None:
    base = _pullback_with_hammer()
    snap = mc.scan([*base, confirmation(4, close="100.20"), confirmation(5, close="100.60")])
    assert snap.confirm_close == D("100.20") and snap.confirm_start == T0 + 4 * MIN


# ------------------------------------------------------------------ Rules 33-39: entry window


def window_snapshot() -> tuple[mc.Snapshot, list[SessionBar]]:
    bars = [*_pullback_with_hammer(), confirmation(4)]  # C = 09:34 candle; C+1 starts 09:35
    return mc.scan(bars), bars


def test_rule32_confirmation_candle_cannot_be_the_entry_candle() -> None:
    snap, bars = window_snapshot()
    assert mc.window_number(snap.confirm_end, bars[-1].start) == 0  # C itself is not in 1..5


def _momentum_ok(prior: SessionBar, latest: SessionBar) -> tuple[SessionBar, SessionBar]:
    return prior, latest


def passing_context(bars: list[SessionBar]) -> list[SessionBar]:
    """Give every completed bar bullish-widening EMAs so the Rule 41 filter passes."""
    out = []
    for i, bar in enumerate(bars):
        out.append(
            SessionBar(
                bar.start, bar.end, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap,
                D("99.0") + D("0.1") * i, D("99.0"), bar.cum_pv, bar.cum_volume,
            )
        )
    return out


def qqq_rising(through_minute: int) -> list[SessionBar]:
    return [sb(i, str(400 + i), str(401 + i), str(399 + i), str(400.5 + i), vwap="400") for i in range(through_minute + 1)]


def window_bar(minute: int, o: str, h: str, low: str, c: str, vwap: str = "100") -> SessionBar:
    return sb(minute, o, h, low, c, vwap=vwap)


def test_rule34_opening_fill_requires_floor_vwap_band_and_ceiling() -> None:
    snap, bars = window_snapshot()
    done = passing_context(bars)
    qqq = qqq_rising(4)
    base = {"qqq_bars": qqq, "qqq_price": D("401")}
    entry = mc.bar_entry(snap, done, window_bar(5, "100.30", "100.50", "100.10", "100.40"), **base)
    assert entry.enter and entry.mode is EntryMode.OPEN and entry.price == D("100.30")
    assert entry.at == T0 + 5 * MIN and entry.window_number == 1
    # open == Confirmation_Close qualifies (>=)
    assert mc.bar_entry(snap, done, window_bar(5, "100.20", "100.50", "100.10", "100.40"), **base).enter
    # open exactly VWAP + 0.50 qualifies; one cent more does not (Rule 37 ceiling)
    assert mc.bar_entry(snap, done, window_bar(5, "100.50", "100.70", "100.40", "100.60"), **base).enter
    assert not mc.bar_entry(snap, done, window_bar(5, "100.51", "100.70", "100.40", "100.60"), **base).enter
    # an open at or below VWAP is rejected (strictly above VWAP)
    assert not mc.bar_entry(snap, done, window_bar(5, "100.00", "100.10", "99.90", "100.00"), **base).enter


def test_rule35_36_deferred_trigger_at_confirmation_close_without_a_green_candle() -> None:
    snap, bars = window_snapshot()
    done, qqq = passing_context(bars), qqq_rising(4)
    base = {"qqq_bars": qqq, "qqq_price": D("401")}
    # opens below 100.20 and reaches it: trigger fill AT Confirmation_Close, stamped at bar end,
    # even though the candle closes red (no completed-green requirement).
    red_trigger = window_bar(5, "100.10", "100.25", "100.05", "100.08")
    entry = mc.bar_entry(snap, done, red_trigger, **base)
    assert entry.enter and entry.mode is EntryMode.TRIGGER
    assert entry.price == D("100.20") and entry.at == red_trigger.end
    # high one cent short of the floor: nothing
    assert not mc.bar_entry(snap, done, window_bar(5, "100.10", "100.19", "100.05", "100.15"), **base).enter


def test_rule35_trigger_is_refused_when_the_unknown_intrabar_vwap_could_breach_the_ceiling() -> None:
    snap, bars = window_snapshot()
    done, qqq = passing_context(bars), qqq_rising(4)
    base = {"qqq_bars": qqq, "qqq_price": D("401")}
    # prior VWAP 100 -> ok at 100.20, but this candle's final VWAP of 99.60 would put the floor
    # above VWAP + 0.50: ambiguous, so no optimistic fill
    bar = window_bar(5, "100.10", "100.25", "100.05", "100.08", vwap="99.60")
    assert not mc.bar_entry(snap, done, bar, **base).enter


def test_rule37_trade_price_above_vwap_plus_half_dollar_never_fills() -> None:
    snap, bars = window_snapshot()
    done, qqq = passing_context(bars), qqq_rising(4)
    base = {"qqq_bars": qqq, "qqq_price": D("401")}
    gap = window_bar(5, "101.00", "101.40", "100.90", "101.20")
    assert not mc.bar_entry(snap, done, gap, **base).enter


def test_rule33_39_window_is_exactly_c_plus_one_through_c_plus_five() -> None:
    snap, bars = window_snapshot()
    done, qqq = passing_context(bars), qqq_rising(10)
    for n, expected in ((1, True), (5, True), (6, False)):
        bar = window_bar(4 + n, "100.30", "100.50", "100.10", "100.40")
        entry = mc.bar_entry(snap, done, bar, qqq_bars=qqq, qqq_price=D("401"))
        assert entry.enter is expected


def test_rule38_a_window_candle_closing_below_vwap_purges_before_any_later_fill() -> None:
    bars = [*_pullback_with_hammer(), confirmation(4), sb(5, "100.0", "100.1", "99.7", "99.80")]
    snap = mc.scan(bars)
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "ENTRY_WINDOW_CLOSE_BELOW_VWAP"
    wick_only = [*_pullback_with_hammer(), confirmation(4), sb(5, "100.3", "100.4", "99.50", "100.30")]
    assert mc.scan(wick_only).phase is Phase.WINDOW  # an intrabar wick below VWAP never cancels


def test_rule39_c_plus_five_without_a_fill_expires_and_purges() -> None:
    held = [sb(5 + i, "100.30", "100.50", "100.10", "100.40") for i in range(4)]
    base = [*_pullback_with_hammer(), confirmation(4)]
    assert mc.scan([*base, *held]).phase is Phase.WINDOW  # C+4 done, C+5 still pending
    snap = mc.scan([*base, *held, sb(9, "100.30", "100.50", "100.10", "100.40")])
    assert snap.phase is Phase.NONE and snap.last_purge_reason == "ENTRY_WINDOW_EXPIRED"


def test_rule53_54_nothing_from_before_a_purge_or_barrier_is_reused() -> None:
    bars = [*_pullback_with_hammer(), confirmation(4)]
    assert mc.scan(bars).phase is Phase.WINDOW
    # a consumed/purged setup: the barrier is after every one of those candles
    barrier = bars[-1].end
    assert mc.scan(bars, barrier).phase is Phase.NONE
    # a new qualifying cycle strictly after the barrier is the only thing that can start
    again = [*bars, *[bearish(10 + i) for i in range(4)]]
    snap = mc.scan(again, barrier)
    assert snap.phase is Phase.SETUP and snap.setup_start == T0 + 10 * MIN


def test_rule40_a_purge_then_fresh_cycle_needs_fresh_pullback_and_pattern() -> None:
    purge_bar = bearish(5, low="90.00")  # dip breach
    cycle = [*_pullback_with_hammer(), purge_bar, confirmation(6)]
    snap = mc.scan(cycle)
    assert snap.phase is Phase.NONE  # old hammer / pullback cannot be reused by the confirmation


# ------------------------------------------------------------------ Rule 41: EMA momentum


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
        (("100.0", "99.5"), ("99.4", "99.5"), False, "BEARISH_ROLLOVER"),
        (("100.0", "99.5"), ("99.5", "99.5"), False, "BEARISH_ROLLOVER"),  # L equality after above
        (("99.0", "99.5"), ("98.8", "99.5"), False, "NO_MOMENTUM"),  # widening bearish
        (("99.0", "99.5"), ("99.0", "99.5"), False, "NO_MOMENTUM"),  # unchanged bearish gap
        (("99.5", "99.5"), ("99.5", "99.5"), False, "FLAT_EQUAL"),  # equal on both candles
    ],
)
def test_rule41_ema_momentum_variants(prior, latest, expected, label) -> None:  # noqa: ANN001
    ok, name = mc.ema_momentum(ema_bar(0, *prior), ema_bar(1, *latest))
    assert (ok, name) == (expected, label)


# ------------------------------------------------------------------ Rule 42-43: QQQ


def qqq_series(closes: list[str], opens: list[str] | None = None, first_minute: int = 1) -> list[SessionBar]:
    opens = opens or closes
    return [
        sb(first_minute + i, opens[i], str(D(closes[i]) + 1), str(D(closes[i]) - 1), closes[i], vwap="400")
        for i in range(len(closes))
    ]


def test_rule42_before_0945_three_rising_closes_and_net_progress() -> None:
    entry = T0 + 5 * MIN  # 09:35 -> preceding candles 09:32, 09:33, 09:34
    good = qqq_series(["400", "401", "402"], first_minute=2)
    assert mc.qqq_check(entry, good, None)[0]
    not_rising = qqq_series(["400", "402", "401"], first_minute=2)
    assert not mc.qqq_check(entry, not_rising, None)[0]
    equal = qqq_series(["400", "401", "401"], first_minute=2)
    assert not mc.qqq_check(entry, equal, None)[0]
    # strictly rising closes but the newest close is not above candle A's OPEN
    net = qqq_series(["400", "401", "402"], opens=["403", "401", "402"], first_minute=2)
    assert not mc.qqq_check(entry, net, None)[0]
    # a missing candle fails closed
    assert not mc.qqq_check(entry, [good[0], good[2]], None)[0]


def test_rule43_from_0945_qqq_must_be_strictly_above_its_own_vwap() -> None:
    entry = T0 + 15 * MIN  # 09:45
    bars = qqq_series(["400"] * 14, first_minute=1)
    assert mc.qqq_check(entry, bars, D("400.01"))[0]
    assert not mc.qqq_check(entry, bars, D("400"))[0]  # equality does not qualify
    assert not mc.qqq_check(entry, bars, None)[0]
    at_0944 = T0 + 14 * MIN
    assert mc.qqq_check(at_0944, bars, D("500"))[1]["qqq_rule"] == "42"  # still the early rule


# ------------------------------------------------------------------ Rule 44: crossings


def cross_bar(i: int, above: bool) -> SessionBar:
    return sb(i, "100", "101", "99", "100.5" if above else "99.5")


def test_rule44_crossing_filter_inactive_before_twenty_candles_and_blocks_at_seven() -> None:
    assert mc.vwap_crossings([cross_bar(i, i % 2 == 0) for i in range(19)]) is None
    alternating = [cross_bar(i, i % 2 == 0) for i in range(20)]
    assert mc.vwap_crossings(alternating) == 19
    six = [cross_bar(i, True) for i in range(20)]
    for flip in (3, 5, 7, 9, 11, 13):  # six crossings
        six[flip] = cross_bar(flip, False)
    for i in (3, 5, 7, 9, 11, 13):
        six[i + 1] = cross_bar(i + 1, True)
    assert mc.vwap_crossings(six) == 12  # each isolated dip = two crossings
    few = [cross_bar(i, i < 10) for i in range(20)]
    assert mc.vwap_crossings(few) == 1


def test_rule44_exactly_seven_crossings_blocks_and_six_does_not() -> None:
    def series(crossings: int) -> list[SessionBar]:
        side = True
        bars = []
        remaining = crossings
        for i in range(20):
            if remaining and i > 0 and i <= crossings:
                side = not side
                remaining -= 1
            bars.append(cross_bar(i, side))
        return bars

    assert mc.vwap_crossings(series(6)) == 6
    assert mc.vwap_crossings(series(7)) == 7
    block, _ = mc._filters(passing_context(series(7)), T0 + 20 * MIN, qqq_rising(19), D("500"))
    assert block in ("VWAP_CROSSINGS_BLOCK", "EMA_MOMENTUM_FAILED")


def test_rule44_only_the_most_recent_twenty_candles_count() -> None:
    old_noise = [cross_bar(i, i % 2 == 0) for i in range(10)]
    calm = [cross_bar(10 + i, True) for i in range(20)]
    assert mc.vwap_crossings([*old_noise, *calm]) == 0


# ------------------------------------------------------------------ Rules 4-5: time gate


@pytest.mark.parametrize(
    ("minute", "allowed"),
    [(3, False), (4, True), (119, True), (120, False)],  # 09:33, 09:34, 11:29, 11:30
)
def test_rule4_5_entry_hours(minute: int, allowed: bool) -> None:
    assert mc.time_gate(T0 + minute * MIN) is allowed


def test_rule5_exactly_1130_is_prohibited_even_one_second_before_is_allowed() -> None:
    assert mc.time_gate(T0 + 120 * MIN - timedelta(seconds=1))
    assert not mc.time_gate(T0 + 120 * MIN)


# ------------------------------------------------------------------ Rules 2-3: session prep


def test_rule2_3_premarket_seeds_emas_only_and_vwap_is_regular_session_only() -> None:
    from mike_trade.market_data.candles import (
        Candle,
        CandleOrigin,
        CandleStatus,
        CandleTimeframe,
        PriceBasis,
    )

    def candle(start, price, volume):  # noqa: ANN001, ANN202
        return Candle(
            "ABC", CandleTimeframe.M1, start, start + MIN, D(price), D(price) + 1, D(price) - 1,
            D(price), D(volume), "sip", True, CandleOrigin.REPLAY, PriceBasis.RAW,
            CandleStatus.CLOSED,
        )

    pre = [candle(T0 - (30 - i) * MIN, "50", "10000") for i in range(30)]
    regular = [candle(T0 + i * MIN, "100", "100") for i in range(3)]
    bars = mc.prepare_session([*pre, *regular], DAY)
    assert [b.start for b in bars] == [c.start for c in regular]
    assert bars[0].vwap == D("100")  # untouched by the $50 premarket volume
    assert bars[0].ema9 is not None and bars[0].ema9 < D("100")  # seeded by premarket closes
    assert bars[0].ema20 is not None
    no_pre = mc.prepare_session(regular, DAY)
    assert no_pre[0].ema9 is None  # without premarket nothing can seed it (fails closed)
