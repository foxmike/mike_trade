"""Pure rules engine for Michelle's corrected VWAP Bounce and Break (business rules 1-55).

Authority: ``docs/strategies/michelle/vwap_bounce_break_business_rules_final.docx``.  Nothing
here performs I/O, reads a clock or mutates its inputs; the BUY and SELL strategy classes are
thin adapters over these functions.

Structure
---------
* :func:`prepare_session` turns one date's completed M1 candles into :class:`SessionBar`
  values: regular-session-only VWAP (Rules 2-3), EMA9/EMA20 seeded from premarket bars
  (Rule 2).
* :func:`scan` is the explicit setup state machine (Rules 7-15, 26-32, 38-39, 53-54).  It is a
  deterministic replay of the completed bars after a purge ``barrier``; the only state the
  engines persist is that barrier (a purge/consumption marker), never ad hoc signal flags.
* :func:`bar_entry` (OHLC replay) and :func:`live_entry` (trade ticks) are the intrabar entry
  window (Rules 33-37).  Both apply the same filters (Rules 41-44).
* Pattern detection (Rules 13-25) lives in :func:`detect_pattern`.

Boundary conventions (all from the document unless marked INTERPRETATION): strict ``<`` / ``>``
where the text says "strictly", inclusive otherwise; a purge never lets the purging candle
seed the next setup (INTERPRETATION of Rule 54).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from mike_trade.market_data.candles import Candle, CandleStatus, CandleTimeframe
from mike_trade.scheduling.calendar import NEW_YORK, session_for
from mike_trade.strategies.michelle_common import ema_values

ZERO = Decimal("0")
HALF = Decimal("0.5")

EARLIEST_ENTRY = time(9, 34)  # Rules 4-5
ENTRY_DEADLINE = time(11, 30)  # Rule 5: strictly before
QQQ_SWITCH = time(9, 45)  # Rules 42-43
MAX_DIP = Decimal("3.00")  # Rule 11
SETUP_TIMEOUT = timedelta(minutes=30)  # Rules 12, 30
SHELF_MAX_RANGE = Decimal("0.50")  # Rule 22
SHELF_MIN_BARS = 3  # Rule 20
SHELF_MAX_BARS = 6
HAMMER_WICK_MULTIPLE = Decimal(2)  # Rule 17
PULLBACK_MIN_BARS = 3  # Rules 4, 26
ENTRY_WINDOW_BARS = 5  # Rule 33: C+1 .. C+5
VWAP_CEILING = Decimal("0.50")  # Rules 34, 35, 37
CROSS_LOOKBACK = 20  # Rule 44
CROSS_BLOCK_AT = 7
MINUTE = timedelta(minutes=1)


class PatternKind(StrEnum):
    HAMMER = "HAMMER"  # priority 1 (Rule 14)
    SUPPORT_SHELF_BASE = "SUPPORT_SHELF_BASE"  # priority 2
    BULLISH_ENGULFING = "BULLISH_ENGULFING"  # priority 3


class EntryMode(StrEnum):
    OPEN = "OPEN"  # market buy at the open of C+1 (Rule 34)
    TRIGGER = "TRIGGER"  # resting intrabar trigger at Confirmation_Close (Rule 35)
    LIVE = "LIVE"  # trade-tick evaluation (SHADOW / PAPER / LIVE)


@dataclass(frozen=True, slots=True)
class SessionBar:
    """One completed regular-session M1 candle with its contemporaneous indicators."""

    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None  # session VWAP including this candle (Rule 3)
    ema9: Decimal | None  # premarket-seeded (Rule 2), including this candle's close
    ema20: Decimal | None
    cum_pv: Decimal  # running sum(typical * volume) - lets a developing candle extend it
    cum_volume: Decimal

    @property
    def is_green(self) -> bool:
        return self.close > self.open

    @property
    def is_red(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True, slots=True)
class Pattern:
    kind: PatternKind
    low: Decimal  # absolute low of the pattern (Rule 15)
    formed_at: datetime  # start of the candle on which the pattern completed
    stage: int  # 1 = Pattern 1, 2 = Pattern 2
    bar_count: int = 1

    def facts(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "low": str(self.low),
            "formed_at": self.formed_at.isoformat(),
            "stage": str(self.stage),
            "bar_count": str(self.bar_count),
        }


@dataclass(slots=True)
class _Setup:
    """Mutable working state of the setup currently being replayed."""

    start: datetime
    start_index: int
    low: Decimal
    low_vwap: Decimal
    run: int = 0
    pullback_ok: bool = False
    pattern1: Pattern | None = None
    pattern2: Pattern | None = None
    pattern1_disqualified: bool = False
    confirmed: bool = False
    confirm_start: datetime | None = None
    confirm_end: datetime | None = None
    confirm_close: Decimal | None = None
    confirm_index: int | None = None


class Phase(StrEnum):
    NONE = "NONE"  # no active setup
    SETUP = "SETUP"  # bearish setup active, waiting for pattern + confirmation
    WINDOW = "WINDOW"  # confirmed: entry window C+1..C+5 is open


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Result of replaying the completed bars: the active setup (if any) and the last purge."""

    phase: Phase
    setup_start: datetime | None = None
    setup_low: Decimal | None = None
    setup_low_vwap: Decimal | None = None
    pullback_ok: bool = False
    pattern: Pattern | None = None  # the active bottoming pattern (P2 once formed, else P1)
    pattern1: Pattern | None = None
    pattern1_disqualified: bool = False
    confirm_start: datetime | None = None
    confirm_end: datetime | None = None
    confirm_close: Decimal | None = None
    last_purge_reason: str | None = None
    last_purge_at: datetime | None = None

    def facts(self) -> dict[str, str]:
        """Genuine values available at evaluation time (evidence for audit / future JEV)."""
        values: dict[str, str] = {"phase": self.phase.value}
        for name in (
            "setup_start", "setup_low", "setup_low_vwap", "confirm_start", "confirm_end",
            "confirm_close",
        ):
            value = getattr(self, name)
            if value is not None:
                values[name] = value.isoformat() if isinstance(value, datetime) else str(value)
        if self.pattern is not None:
            values["pattern"] = self.pattern.kind.value
            values["pattern_stage"] = str(self.pattern.stage)
            values["pattern_low"] = str(self.pattern.low)
        if self.pattern1 is not None:
            values["pattern1_kind"] = self.pattern1.kind.value
            values["pattern1_low"] = str(self.pattern1.low)
            values["pattern1_disqualified"] = str(self.pattern1_disqualified)
        return values


# --------------------------------------------------------------------------- session prep


def regular_bounds(day: date) -> tuple[datetime, datetime] | None:
    """UTC ``(open, close)`` of the regular session, honouring early closes; ``None`` if closed."""
    try:
        session = session_for(day)
    except ValueError:
        return None
    return session.open_utc, session.close_utc


def prepare_session(candles: Iterable[Candle], day: date) -> list[SessionBar]:
    """Completed regular-session bars of ``day`` (Rules 2-3).

    EMA9/EMA20 are SMA-seeded over *every* completed M1 candle of the date, premarket
    included.  VWAP (typical price x volume) accumulates over regular-session candles only,
    so premarket never reaches VWAP, setup counts or pattern detection.
    """
    bounds = regular_bounds(day)
    if bounds is None:
        return []
    open_utc, close_utc = bounds
    by_start: dict[datetime, Candle] = {}
    for candle in candles:
        if (
            isinstance(candle, Candle)
            and candle.timeframe is CandleTimeframe.M1
            and candle.status is CandleStatus.CLOSED
            and candle.start.astimezone(NEW_YORK).date() == day
        ):
            by_start[candle.start] = candle
    ordered = [by_start[key] for key in sorted(by_start)]
    if not ordered:
        return []
    ema9 = ema_values(ordered, 9)  # type: ignore[arg-type]
    ema20 = ema_values(ordered, 20)  # type: ignore[arg-type]
    bars: list[SessionBar] = []
    weighted = ZERO
    volume = ZERO
    for index, candle in enumerate(ordered):
        if candle.start < open_utc or candle.end > close_utc:
            continue
        if candle.volume > ZERO:
            typical = (candle.high + candle.low + candle.close) / Decimal(3)
            weighted += typical * candle.volume
            volume += candle.volume
        bars.append(
            SessionBar(
                candle.start, candle.end, candle.open, candle.high, candle.low, candle.close,
                candle.volume, None if volume <= ZERO else weighted / volume,
                ema9[index], ema20[index], weighted, volume,
            )
        )
    return bars


def vwap_with(bars: Sequence[SessionBar], developing: Candle | None) -> Decimal | None:
    """Point-in-time session VWAP, extended by the developing candle when one is supplied."""
    weighted = bars[-1].cum_pv if bars else ZERO
    volume = bars[-1].cum_volume if bars else ZERO
    if developing is not None and developing.volume > ZERO:
        typical = (developing.high + developing.low + developing.close) / Decimal(3)
        weighted += typical * developing.volume
        volume += developing.volume
    return None if volume <= ZERO else weighted / volume


# --------------------------------------------------------------------------- patterns


def _adjacent(bars: Sequence[SessionBar]) -> bool:
    return all(cur.start == prior.end for prior, cur in zip(bars, bars[1:], strict=False))


def is_hammer(bar: SessionBar) -> bool:
    """Rules 16-19: body strictly below VWAP, lower wick >= 2x body, body in the upper half."""
    if bar.vwap is None or bar.high <= bar.low:
        return False
    top, bottom = max(bar.open, bar.close), min(bar.open, bar.close)
    if not top < bar.vwap:
        return False
    if bottom - bar.low < HAMMER_WICK_MULTIPLE * (top - bottom):  # a doji always passes
        return False
    return bottom >= bar.low + HALF * (bar.high - bar.low)


def support_shelf(bars: Sequence[SessionBar], index: int, first_index: int) -> int | None:
    """Rules 20-22: size (3-6) of the longest qualifying cluster ending at ``index``, else None.

    Every close is strictly below its own VWAP; colors are irrelevant; the spread between the
    highest and the lowest candle low is at most $0.50 (equality allowed).
    """
    longest = min(SHELF_MAX_BARS, index - first_index + 1)
    for size in range(longest, SHELF_MIN_BARS - 1, -1):
        cluster = bars[index - size + 1 : index + 1]
        if not _adjacent(cluster):
            continue
        if any(b.vwap is None or not b.close < b.vwap for b in cluster):
            continue
        lows = [b.low for b in cluster]
        if max(lows) - min(lows) <= SHELF_MAX_RANGE:
            return size
    return None


def is_bullish_engulfing(prior: SessionBar, current: SessionBar) -> bool:
    """Rules 23-24: red then green, both closing below VWAP; body engulf (equality counts)."""
    if prior.vwap is None or current.vwap is None or current.start != prior.end:
        return False
    return (
        prior.close < prior.open
        and current.close > current.open
        and prior.close < prior.vwap
        and current.close < current.vwap
        and current.open <= prior.close
        and current.close >= prior.open
    )


def detect_pattern(
    bars: Sequence[SessionBar], index: int, first_index: int, *, below: Decimal | None = None
) -> Pattern | None:
    """Best bottoming pattern completing on ``bars[index]`` (priority Hammer > Shelf > Engulf).

    ``below`` makes a pattern valid only when its absolute low is strictly lower (Pattern 2 must
    be a *lower* pattern than the disqualified Pattern 1).  Only candles at or after
    ``first_index`` (the setup's first candle) may take part.
    """
    bar = bars[index]
    candidates: list[tuple[PatternKind, Decimal, int]] = []
    if is_hammer(bar):
        candidates.append((PatternKind.HAMMER, bar.low, 1))
    size = support_shelf(bars, index, first_index)
    if size is not None:
        low = min(b.low for b in bars[index - size + 1 : index + 1])
        candidates.append((PatternKind.SUPPORT_SHELF_BASE, low, size))
    if index - 1 >= first_index and is_bullish_engulfing(bars[index - 1], bar):
        candidates.append((PatternKind.BULLISH_ENGULFING, min(bars[index - 1].low, bar.low), 2))
    for kind, low, count in candidates:  # already in priority order
        if below is None or low < below:
            return Pattern(kind, low, bar.start, 2 if below is not None else 1, count)
    return None


# --------------------------------------------------------------------------- state machine


def starts_setup(bar: SessionBar) -> bool:
    """Rules 7-8: close below VWAP, EMA9 and EMA20 with both EMAs strictly below VWAP."""
    if bar.vwap is None or bar.ema9 is None or bar.ema20 is None:
        return False
    return (
        bar.close < bar.vwap
        and bar.close < bar.ema9
        and bar.close < bar.ema20
        and bar.ema9 < bar.vwap
        and bar.ema20 < bar.vwap
    )


def window_number(confirm_end: datetime, bar_start: datetime) -> int:
    """1-based entry-window candle number (C+1 = 1); values above 5 are outside the window."""
    return int((bar_start - confirm_end) // MINUTE) + 1


@dataclass(slots=True)
class _Purges:
    reason: str | None = None
    at: datetime | None = None


def scan(bars: Sequence[SessionBar], barrier: datetime | None = None) -> Snapshot:
    """Replay completed ``bars`` (ascending, regular session) from ``barrier`` onward.

    Bars starting before ``barrier`` are ignored (Rule 54: nothing from before a purge or a
    consumed setup may be reused).  The result describes the setup that is live after the last
    bar, if any.  A bar that purges a setup can never seed the next one.
    """
    setup: _Setup | None = None
    purge = _Purges()

    def purged(reason: str, bar: SessionBar) -> None:
        purge.reason, purge.at = reason, bar.end

    for index, bar in enumerate(bars):
        if barrier is not None and bar.start < barrier:
            continue
        if bar.vwap is None:
            continue
        # Rule 12: the 30-minute confirmation clock (candle start to candle start, inclusive).
        if setup is not None and not setup.confirmed and bar.start - setup.start > SETUP_TIMEOUT:
            purged("CONFIRMATION_CLOCK_EXPIRED", bar)
            setup = None
        # Rule 39: C+6 or later can never fill under this confirmation.
        if (
            setup is not None
            and setup.confirmed
            and setup.confirm_end is not None
            and window_number(setup.confirm_end, bar.start) > ENTRY_WINDOW_BARS
        ):
            purged("ENTRY_WINDOW_EXPIRED", bar)
            setup = None
        if setup is None:
            if not starts_setup(bar):
                continue
            setup = _Setup(bar.start, index, bar.low, bar.vwap)
        # Rules 10-11: strictly lower lows re-anchor the VWAP; equal lows never do.
        if bar.low < setup.low:
            setup.low, setup.low_vwap = bar.low, bar.vwap
        if setup.low_vwap - setup.low > MAX_DIP:  # exactly $3.00 is allowed
            purged("MAX_DIP_BREACHED", bar)
            setup = None
            continue
        # Rule 15: a completed candle closing strictly below Pattern 2's low ends the setup.
        if setup.pattern2 is not None and bar.close < setup.pattern2.low:
            purged("PATTERN2_CLOSE_BREAK", bar)
            setup = None
            continue
        if setup.confirmed:
            number = window_number(setup.confirm_end, bar.start)  # type: ignore[arg-type]
            if bar.close < bar.vwap:  # Rule 38: intrabar wicks never cancel, a close does
                purged("ENTRY_WINDOW_CLOSE_BELOW_VWAP", bar)
                setup = None
            elif number >= ENTRY_WINDOW_BARS:  # Rule 39
                purged("ENTRY_WINDOW_EXPIRED", bar)
                setup = None
            continue

        # ---- unconfirmed setup: Rule 15 disqualification, patterns, pullback, confirmation
        if (
            setup.pattern1 is not None
            and setup.pattern2 is None
            and not setup.pattern1_disqualified
            and bar.low < setup.pattern1.low
        ):
            setup.pattern1_disqualified = True
        if setup.pattern2 is None and (setup.pattern1 is None or setup.pattern1_disqualified):
            found = detect_pattern(
                bars, index, setup.start_index,
                below=setup.pattern1.low if setup.pattern1 is not None else None,
            )
            if found is not None:
                if setup.pattern1 is None:
                    setup.pattern1 = found
                else:
                    setup.pattern2 = found
        pattern_valid = setup.pattern2 is not None or (
            setup.pattern1 is not None and not setup.pattern1_disqualified
        )
        if (
            setup.pullback_ok
            and pattern_valid
            and bar.close > bar.open
            and bar.low <= bar.vwap
            and bar.close > bar.vwap
        ):
            setup.confirmed = True
            setup.confirm_start, setup.confirm_end = bar.start, bar.end
            setup.confirm_close, setup.confirm_index = bar.close, index
        setup.run = setup.run + 1 if bar.close < bar.vwap else 0
        if setup.run >= PULLBACK_MIN_BARS:
            setup.pullback_ok = True

    if setup is None:
        return Snapshot(Phase.NONE, last_purge_reason=purge.reason, last_purge_at=purge.at)
    return Snapshot(
        Phase.WINDOW if setup.confirmed else Phase.SETUP,
        setup.start, setup.low, setup.low_vwap, setup.pullback_ok,
        setup.pattern2 or setup.pattern1, setup.pattern1, setup.pattern1_disqualified,
        setup.confirm_start, setup.confirm_end, setup.confirm_close,
        purge.reason, purge.at,
    )


# --------------------------------------------------------------------------- filters


def ema_momentum(prior: SessionBar, latest: SessionBar) -> tuple[bool, str]:
    """Rule 41 over the two most recently completed candles (P = prior, L = latest)."""
    if None in (prior.ema9, prior.ema20, latest.ema9, latest.ema20):
        return False, "UNAVAILABLE"
    p9, p20, l9, l20 = prior.ema9, prior.ema20, latest.ema9, latest.ema20
    assert p9 is not None and p20 is not None and l9 is not None and l20 is not None
    if p9 == p20 and l9 == l20:
        return False, "FLAT_EQUAL"
    if p9 <= p20 and l9 >= l20:
        return True, "BULLISH_CROSSOVER"
    gap_p, gap_l = abs(p9 - p20), abs(l9 - l20)
    if p9 > p20 and l9 > l20 and gap_l > gap_p:
        return True, "BULLISH_WIDENING"
    if p9 < p20 and l9 < l20 and gap_l < gap_p:
        return True, "BEARISH_CONVERGING"
    if p9 > p20 and l9 <= l20:
        return False, "BEARISH_ROLLOVER"
    return False, "NO_MOMENTUM"


def vwap_crossings(bars: Sequence[SessionBar]) -> int | None:
    """Rule 44: close-based VWAP crossings in the latest 20 completed candles.

    ``None`` while fewer than 20 regular-session candles have completed (filter inactive).  A
    crossing is a change between the strictly-above and strictly-below side of VWAP; a close
    exactly on VWAP has no side and neither creates nor resets one (INTERPRETATION).
    """
    if len(bars) < CROSS_LOOKBACK:
        return None
    side = 0
    crossings = 0
    for bar in bars[-CROSS_LOOKBACK:]:
        if bar.vwap is None or bar.close == bar.vwap:
            continue
        current = 1 if bar.close > bar.vwap else -1
        if side and current != side:
            crossings += 1
        side = current
    return crossings


def qqq_check(
    entry_minute: datetime,
    qqq_bars: Sequence[SessionBar],
    qqq_price: Decimal | None,
) -> tuple[bool, dict[str, str]]:
    """Rules 42-43.  ``entry_minute`` is the entry time floored to its minute (UTC)."""
    local = entry_minute.astimezone(NEW_YORK).time()
    by_start = {b.start: b for b in qqq_bars}
    if local < QQQ_SWITCH:
        a, b, c = (by_start.get(entry_minute - MINUTE * n) for n in (3, 2, 1))
        if a is None or b is None or c is None:
            return False, {"qqq_rule": "42", "qqq_result": "MISSING_PRECEDING_CANDLES"}
        passed = c.close > b.close > a.close and c.close > a.open
        return passed, {
            "qqq_rule": "42",
            "qqq_close_a": str(a.close), "qqq_close_b": str(b.close), "qqq_close_c": str(c.close),
            "qqq_open_a": str(a.open), "qqq_result": "PASS" if passed else "FAIL",
        }
    if not qqq_bars or qqq_price is None or qqq_bars[-1].vwap is None:
        return False, {"qqq_rule": "43", "qqq_result": "MISSING_PRICE_OR_VWAP"}
    vwap = qqq_bars[-1].vwap
    passed = qqq_price > vwap
    return passed, {
        "qqq_rule": "43", "qqq_price": str(qqq_price), "qqq_vwap": str(vwap),
        "qqq_result": "PASS" if passed else "FAIL",
    }


def time_gate(moment: datetime) -> bool:
    """Rules 4-5: 09:34:00 <= entry time < 11:30:00 New York."""
    local = moment.astimezone(NEW_YORK).time()
    return EARLIEST_ENTRY <= local < ENTRY_DEADLINE


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """An entry that satisfied every document condition (or why it did not)."""

    enter: bool
    mode: EntryMode | None = None
    price: Decimal | None = None  # modeled / observed execution reference price
    at: datetime | None = None  # bar open (OPEN), bar end (TRIGGER) or the tick time (LIVE)
    window_number: int | None = None
    vwap: Decimal | None = None
    reason: str = ""
    facts: dict[str, str] = field(default_factory=dict)


def _filters(
    completed: Sequence[SessionBar],
    entry_minute: datetime,
    qqq_bars: Sequence[SessionBar],
    qqq_price: Decimal | None,
) -> tuple[str | None, dict[str, str]]:
    """Rules 41-44.  Returns ``(reason_for_rejection | None, facts)``; ``completed`` ends at L."""
    facts: dict[str, str] = {}
    if len(completed) < 2:
        return "EMA_MOMENTUM_UNAVAILABLE", facts
    ok, label = ema_momentum(completed[-2], completed[-1])
    facts["ema_momentum"] = label
    for name, bar in (("p", completed[-2]), ("l", completed[-1])):
        facts[f"ema9_{name}"] = str(bar.ema9)
        facts[f"ema20_{name}"] = str(bar.ema20)
    if not ok:
        return "EMA_MOMENTUM_FAILED", facts
    qqq_ok, qqq_facts = qqq_check(entry_minute, qqq_bars, qqq_price)
    facts.update(qqq_facts)
    if not qqq_ok:
        return "QQQ_FILTER_FAILED", facts
    crossings = vwap_crossings(completed)
    facts["vwap_crossings"] = "inactive" if crossings is None else str(crossings)
    if crossings is not None and crossings >= CROSS_BLOCK_AT:
        return "VWAP_CROSSINGS_BLOCK", facts
    return None, facts


def _price_ok(price: Decimal, vwap: Decimal, confirm_close: Decimal) -> bool:
    """Rules 34-37: at/above Confirmation_Close, strictly above VWAP, never above VWAP+0.50."""
    return price >= confirm_close and vwap < price <= vwap + VWAP_CEILING


def bar_entry(
    snapshot: Snapshot,
    completed: Sequence[SessionBar],
    window_bar: SessionBar,
    qqq_bars: Sequence[SessionBar],
    qqq_price: Decimal | None,
) -> EntryDecision:
    """OHLC-only emulation of the entry window for the just-completed candle ``window_bar``.

    ``snapshot`` must describe the state *before* ``window_bar`` and ``completed`` the bars
    before it.  The honest limits of one-minute OHLC are encoded as conservative rules:

    * C+1..C+5 open leg (Rule 34): exact - the open print uses the VWAP through the prior bar.
    * Deferred trigger leg (Rule 35): only when the candle opens below Confirmation_Close and
      its high reaches it.  The fill is modeled AT Confirmation_Close, stamped at the bar end
      (latest possible), and the VWAP window must hold at BOTH the prior-bar VWAP and this
      bar's final VWAP because the contemporaneous VWAP at the touch is unknowable.
    * Anything else (open at/above the floor but outside the VWAP band or failing a filter) is
      NOT guessed into a fill: no entry for that candle.
    * The $3.00 dip is treated as having occurred before an intrabar trigger when this
      candle's own low breaches it (the open leg precedes every other print).
    """
    if snapshot.phase is not Phase.WINDOW or snapshot.confirm_end is None:
        return EntryDecision(False, reason="NO_ACTIVE_WINDOW")
    number = window_number(snapshot.confirm_end, window_bar.start)
    if not 1 <= number <= ENTRY_WINDOW_BARS:
        return EntryDecision(False, reason="OUTSIDE_ENTRY_WINDOW")
    if not time_gate(window_bar.start):
        return EntryDecision(False, window_number=number, reason="OUTSIDE_ENTRY_HOURS")
    if not completed or completed[-1].vwap is None or window_bar.vwap is None:
        return EntryDecision(False, window_number=number, reason="VWAP_UNAVAILABLE")
    assert snapshot.confirm_close is not None
    floor = snapshot.confirm_close
    vwap_open = completed[-1].vwap
    rejection, facts = _filters(completed, window_bar.start, qqq_bars, qqq_price)
    facts.update(snapshot.facts())
    facts["window_number"] = str(number)
    if rejection is not None:
        return EntryDecision(False, window_number=number, reason=rejection, facts=facts)
    if _price_ok(window_bar.open, vwap_open, floor):
        facts["entry_mode"] = EntryMode.OPEN.value
        return EntryDecision(
            True, EntryMode.OPEN, window_bar.open, window_bar.start, number, vwap_open,
            "OPEN_FILL", facts,
        )
    if window_bar.open < floor <= window_bar.high:
        low_vwap, high_vwap = sorted((vwap_open, window_bar.vwap))
        setup_low = snapshot.setup_low
        if (
            setup_low is not None
            and window_bar.low < setup_low
            and window_bar.vwap - window_bar.low > MAX_DIP
        ):
            return EntryDecision(False, window_number=number, reason="DIP_BEFORE_TRIGGER", facts=facts)
        if _price_ok(floor, low_vwap, floor) and _price_ok(floor, high_vwap, floor):
            facts["entry_mode"] = EntryMode.TRIGGER.value
            return EntryDecision(
                True, EntryMode.TRIGGER, floor, window_bar.end, number, window_bar.vwap,
                "TRIGGER_FILL", facts,
            )
    return EntryDecision(False, window_number=number, reason="NO_QUALIFYING_PRINT", facts=facts)


def live_entry(
    snapshot: Snapshot,
    completed: Sequence[SessionBar],
    developing: Candle,
    price: Decimal,
    at: datetime,
    vwap_now: Decimal,
    ask: Decimal | None,
    qqq_bars: Sequence[SessionBar],
    qqq_price: Decimal | None,
) -> EntryDecision:
    """Trade-tick evaluation of the entry window (Rules 33-37) for ``price`` printed at ``at``.

    The window opens with the first print of C+1; ``developing`` is the live M1 candle.  The
    maximum dip is enforced with the developing low (Rule 11 applies continuously until the
    entry is filled).  When a fresh ``ask`` is known the expected execution price must also
    respect the VWAP ceiling (Rule 37: never execute above VWAP + $0.50).
    """
    if snapshot.phase is not Phase.WINDOW or snapshot.confirm_end is None:
        return EntryDecision(False, reason="NO_ACTIVE_WINDOW")
    number = window_number(snapshot.confirm_end, developing.start)
    if not 1 <= number <= ENTRY_WINDOW_BARS:
        return EntryDecision(False, reason="OUTSIDE_ENTRY_WINDOW")
    if not time_gate(at):
        return EntryDecision(False, window_number=number, reason="OUTSIDE_ENTRY_HOURS")
    assert snapshot.confirm_close is not None
    if snapshot.setup_low is not None and snapshot.setup_low_vwap is not None:
        low, low_vwap = snapshot.setup_low, snapshot.setup_low_vwap
        if developing.low < low:
            low, low_vwap = developing.low, vwap_now
        if low_vwap - low > MAX_DIP:
            return EntryDecision(False, window_number=number, reason="MAX_DIP_BREACHED_INTRABAR")
    rejection, facts = _filters(completed, developing.start, qqq_bars, qqq_price)
    facts.update(snapshot.facts())
    facts["window_number"] = str(number)
    if rejection is not None:
        return EntryDecision(False, window_number=number, reason=rejection, facts=facts)
    if not _price_ok(price, vwap_now, snapshot.confirm_close):
        return EntryDecision(False, window_number=number, reason="PRICE_OUTSIDE_BAND", facts=facts)
    if ask is not None and ask > vwap_now + VWAP_CEILING:
        return EntryDecision(False, window_number=number, reason="ASK_ABOVE_VWAP_CEILING", facts=facts)
    facts["entry_mode"] = EntryMode.LIVE.value
    facts["entry_price"] = str(price)
    facts["entry_vwap"] = str(vwap_now)
    facts["entry_vwap_distance"] = str(price - vwap_now)
    return EntryDecision(True, EntryMode.LIVE, price, at, number, vwap_now, "LIVE_TRIGGER", facts)


def live_dip_breached(
    snapshot: Snapshot, developing: Candle, vwap_now: Decimal | None
) -> bool:
    """Rule 11 intrabar: the developing candle's low breaches the $3.00 dip of an active setup."""
    if snapshot.phase is Phase.NONE or snapshot.setup_low is None or vwap_now is None:
        return False
    assert snapshot.setup_low_vwap is not None
    low, low_vwap = snapshot.setup_low, snapshot.setup_low_vwap
    if developing.low < low:
        low, low_vwap = developing.low, vwap_now
    return low_vwap - low > MAX_DIP


def as_state_facts(values: dict[str, Any]) -> dict[str, str]:
    return {key: str(value) for key, value in values.items()}
