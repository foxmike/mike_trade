"""Pure rules engine for Michelle's corrected VWAP Bounce and Break (business rules V2, 1-51).

Authority: ``docs/strategies/michelle/vwap_bounce_break_business_rules_final_2.docx``.  Nothing
here performs I/O, reads a clock or mutates its inputs; the BUY and SELL strategy classes are
thin adapters over these functions.

Structure
---------
* :func:`prepare_session` turns one date's completed M1 candles into :class:`SessionBar`
  values: regular-session-only VWAP (Rules 2-3), EMA9/EMA20 seeded from premarket bars
  (Rule 2).
* :func:`scan` is the explicit setup state machine (Rules 6-15, 26-31, 37-38, 49-50).  It is a
  deterministic replay of the completed bars after a purge ``barrier``; the only state the
  engines persist is that barrier (a purge/consumption marker), never ad hoc signal flags.
* :func:`bar_entry` (OHLC replay) and :func:`live_entry` (trade ticks) are the intrabar entry
  window (Rules 32-38).  Both apply the same filters (Rules 39-40).
* Pattern detection (Rules 13-25) lives in :func:`detect_pattern`, :func:`extend_shelf`,
  :func:`is_hammer` and :func:`is_bullish_engulfing`.

Boundary conventions (all from the document unless marked INTERPRETATION): strict ``<`` / ``>``
where the text says "strictly", inclusive otherwise; a purge never lets the purging candle
seed the next setup (INTERPRETATION of Rule 50).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from mike_trade.market_data.candles import Candle, CandleStatus, CandleTimeframe
from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES, TickRules
from mike_trade.scheduling.calendar import NEW_YORK, session_for
from mike_trade.strategies.michelle_common import ema_values

ZERO = Decimal("0")
HALF = Decimal("0.5")

EARLIEST_ENTRY = time(9, 34)  # Rule 4
ENTRY_DEADLINE = time(11, 30)  # Rule 4: strictly before
MAX_DIP = Decimal("3.00")  # Rule 10
SETUP_TIMEOUT = timedelta(minutes=30)  # Rules 11, 29
SHELF_MAX_RANGE = Decimal("0.25")  # Rules 20, 22
SHELF_MIN_BARS = 2  # Rule 20
HAMMER_WICK_MULTIPLE = Decimal(2)  # Rule 17
PULLBACK_MIN_BARS = 3  # Rule 12
ENTRY_WINDOW_BARS = 5  # Rule 32: C+1 .. C+5
ENTRY_CEILING_OFFSET = Decimal("0.50")  # Rule 30: Candle C's VWAP + $0.50
MINUTE = timedelta(minutes=1)


class PatternKind(StrEnum):
    HAMMER = "HAMMER"  # priority 1 (Rule 14)
    SUPPORT_SHELF_BASE = "SUPPORT_SHELF_BASE"  # priority 2
    BULLISH_ENGULFING = "BULLISH_ENGULFING"  # priority 3


class EntryMode(StrEnum):
    OPEN = "OPEN"  # OHLC replay: the candle's opening print already qualifies (exact)
    TRIGGER = "TRIGGER"  # OHLC replay: modeled at the nearest edge of the qualifying price band
    LIVE = "LIVE"  # trade-tick evaluation (SHADOW / PAPER / LIVE)


class EntryScenario(StrEnum):
    """Rules 33-34: which entry condition set the frozen confirmation values select."""

    WITHIN_CEILING = "SCENARIO_1"  # confirmation close <= entry ceiling
    ABOVE_CEILING = "SCENARIO_2"  # confirmation close > entry ceiling (wait for a pullback)


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
    low: Decimal  # pattern low (Rule 15): lowest price, or lowest close for a Support Shelf Base
    formed_at: datetime  # start of the candle on which the pattern completed
    stage: int  # 1 = Pattern 1, 2 = Pattern 2
    bar_count: int = 1
    anchor: datetime | None = None  # first candle of a Support Shelf Base (identifies the base)

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
    entry_ceiling: Decimal | None = None  # Rule 30: Candle C's VWAP + $0.50, frozen
    chain: list[SessionBar] = field(default_factory=list)  # open Support Shelf Base candidate


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
    entry_ceiling: Decimal | None = None
    last_purge_reason: str | None = None
    last_purge_at: datetime | None = None

    @property
    def scenario(self) -> EntryScenario | None:
        """Rules 33-34: fixed the moment Candle C completes (``None`` before then)."""
        if self.confirm_close is None or self.entry_ceiling is None:
            return None
        if self.confirm_close > self.entry_ceiling:
            return EntryScenario.ABOVE_CEILING
        return EntryScenario.WITHIN_CEILING

    def facts(self) -> dict[str, str]:
        """Genuine values available at evaluation time (evidence for audit / future JEV)."""
        values: dict[str, str] = {"phase": self.phase.value}
        for name in (
            "setup_start", "setup_low", "setup_low_vwap", "confirm_start", "confirm_end",
            "confirm_close", "entry_ceiling",
        ):
            value = getattr(self, name)
            if value is not None:
                values[name] = value.isoformat() if isinstance(value, datetime) else str(value)
        if self.scenario is not None:
            values["entry_scenario"] = self.scenario.value
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


def extend_shelf(chain: Sequence[SessionBar], bar: SessionBar) -> list[SessionBar]:
    """Rules 20-22: the open Support Shelf Base candidate after completed candle ``bar``.

    Every candle of a base closes strictly below its own VWAP (Rule 21; colors are irrelevant).
    A candle extends the base while the highest close stays no more than $0.25 above the lowest
    close (Rule 22, exactly $0.25 allowed) and the candles are adjacent.  Any other candle - one
    closing higher than the range (the base is complete and locked), one closing lower than the
    range, or one closing at/above VWAP - ends that base; a candle still closing below VWAP
    starts the next candidate.  A candidate of at least two candles is a qualified base (Rule 20).
    """
    if bar.vwap is None or not bar.close < bar.vwap:
        return []
    if chain and bar.start == chain[-1].end:
        closes = [member.close for member in chain]
        closes.append(bar.close)
        if max(closes) - min(closes) <= SHELF_MAX_RANGE:
            return [*chain, bar]
    return [bar]


def shelf_pattern(chain: Sequence[SessionBar]) -> Pattern | None:
    """The qualified base for ``chain`` (>= 2 candles); its low is the LOWEST CLOSE (Rule 15)."""
    if len(chain) < SHELF_MIN_BARS:
        return None
    return Pattern(
        PatternKind.SUPPORT_SHELF_BASE, min(b.close for b in chain), chain[-1].start, 1,
        len(chain), chain[0].start,
    )


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
    bars: Sequence[SessionBar],
    index: int,
    first_index: int,
    chain: Sequence[SessionBar] = (),
    *,
    below: Decimal | None = None,
) -> Pattern | None:
    """Best bottoming pattern completing on ``bars[index]`` (priority Hammer > Shelf > Engulf).

    ``chain`` is the open Support Shelf Base candidate that includes ``bars[index]`` (see
    :func:`extend_shelf`).  ``below`` makes a pattern valid only when its pattern low is
    strictly lower (Pattern 2 must be a *lower* pattern than the discarded Pattern 1).  Only
    candles at or after ``first_index`` (the setup-start candle, Rule 8) may take part.  The
    pattern low is the lowest price in the pattern, except that for a Support Shelf Base it is
    the lowest close (Rule 15).
    """
    bar = bars[index]
    candidates: list[Pattern] = []
    stage = 2 if below is not None else 1
    if is_hammer(bar):
        candidates.append(Pattern(PatternKind.HAMMER, bar.low, bar.start, stage, 1))
    shelf = shelf_pattern(chain)
    if shelf is not None:
        candidates.append(replace(shelf, stage=stage))
    if index - 1 >= first_index and is_bullish_engulfing(bars[index - 1], bar):
        candidates.append(
            Pattern(
                PatternKind.BULLISH_ENGULFING, min(bars[index - 1].low, bar.low), bar.start,
                stage, 2,
            )
        )
    for candidate in candidates:  # already in priority order (Rule 14)
        if below is None or candidate.low < below:
            return candidate
    return None


# --------------------------------------------------------------------------- state machine


def starts_setup(bar: SessionBar) -> bool:
    """Rules 6-7: close below VWAP, EMA9 and EMA20 with both EMAs strictly below VWAP."""
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


def _refresh_shelf(setup: _Setup) -> None:
    """Rule 20: an open Support Shelf Base that is the live pattern follows its own extension.

    While closes keep extending the base, the lowest close of the base (the pattern low) can
    move lower; the extending close is part of the base, never a break of it.
    """
    chain = setup.chain
    if len(chain) < SHELF_MIN_BARS:
        return
    live, name = setup.pattern2, "pattern2"
    if live is None and not setup.pattern1_disqualified:
        live, name = setup.pattern1, "pattern1"
    if live is None or live.kind is not PatternKind.SUPPORT_SHELF_BASE:
        return
    if live.anchor == chain[0].start:
        setattr(setup, name, replace(live, low=min(b.close for b in chain), bar_count=len(chain)))


def scan(bars: Sequence[SessionBar], barrier: datetime | None = None) -> Snapshot:
    """Replay completed ``bars`` (ascending, regular session) from ``barrier`` onward.

    Bars starting before ``barrier`` are ignored (Rules 49-50: nothing from before a purge or a
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
        # Rule 11: the 30-minute confirmation clock (candle start to candle start, inclusive).
        if setup is not None and not setup.confirmed and bar.start - setup.start > SETUP_TIMEOUT:
            purged("CONFIRMATION_CLOCK_EXPIRED", bar)
            setup = None
        # Rule 38: C+6 or later can never fill under this confirmation.
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
            # Rule 8: the setup-start candle is part of the pullback and of any pattern.
            setup = _Setup(bar.start, index, bar.low, bar.vwap)
        # Rules 9-10: strictly lower lows re-anchor the VWAP; equal lows never do.
        if bar.low < setup.low:
            setup.low, setup.low_vwap = bar.low, bar.vwap
        if setup.low_vwap - setup.low > MAX_DIP:  # exactly $3.00 is allowed
            purged("MAX_DIP_BREACHED", bar)
            setup = None
            continue
        if not setup.confirmed:
            setup.chain = extend_shelf(setup.chain, bar)
            _refresh_shelf(setup)
        # Rule 15: a completed candle closing strictly below Pattern 2's low ends the setup
        # (this also covers a third, lower pattern, which can only form after such a close).
        if setup.pattern2 is not None and bar.close < setup.pattern2.low:
            purged("PATTERN2_CLOSE_BREAK", bar)
            setup = None
            continue
        if setup.confirmed:
            number = window_number(setup.confirm_end, bar.start)  # type: ignore[arg-type]
            if bar.close < bar.vwap:  # Rule 37: intrabar wicks never cancel, a close does
                purged("ENTRY_WINDOW_CLOSE_BELOW_VWAP", bar)
                setup = None
            elif number >= ENTRY_WINDOW_BARS:  # Rule 38
                purged("ENTRY_WINDOW_EXPIRED", bar)
                setup = None
            continue

        # ---- unconfirmed setup: Rule 15 discard, patterns, pullback, confirmation
        # Only a COMPLETED close below Pattern 1's low discards it; wicks never do.
        if (
            setup.pattern1 is not None
            and setup.pattern2 is None
            and not setup.pattern1_disqualified
            and bar.close < setup.pattern1.low
        ):
            setup.pattern1_disqualified = True
        if setup.pattern2 is None and (setup.pattern1 is None or setup.pattern1_disqualified):
            found = detect_pattern(
                bars, index, setup.start_index, setup.chain,
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
        # Rules 26-30: the FIRST completed candle meeting every confirmation requirement.  The
        # run of below-VWAP closes is updated after this test, so Candle C never counts toward
        # its own pullback (Rule 12: the three candles complete before Candle C starts).
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
            setup.entry_ceiling = bar.vwap + ENTRY_CEILING_OFFSET  # frozen (Rule 30)
        setup.run = setup.run + 1 if bar.close < bar.vwap else 0
        if setup.run >= PULLBACK_MIN_BARS:
            setup.pullback_ok = True

    if setup is None:
        return Snapshot(Phase.NONE, last_purge_reason=purge.reason, last_purge_at=purge.at)
    return Snapshot(
        Phase.WINDOW if setup.confirmed else Phase.SETUP,
        setup.start, setup.low, setup.low_vwap, setup.pullback_ok,
        setup.pattern2 or setup.pattern1, setup.pattern1, setup.pattern1_disqualified,
        setup.confirm_start, setup.confirm_end, setup.confirm_close, setup.entry_ceiling,
        purge.reason, purge.at,
    )


# --------------------------------------------------------------------------- filters


def ema_momentum(prior: SessionBar, latest: SessionBar) -> tuple[bool, str]:
    """Rule 39 over the two most recently completed candles (P = prior, L = latest).

    The spread is ``|EMA9 - EMA20|``.  Passes when (a) bullish crossover: EMA9 <= EMA20 on P and
    EMA9 >= EMA20 on L, unless the EMAs are equal on both candles; (b) EMA9 > EMA20 on both and
    the spread is strictly wider on L; (c) EMA9 < EMA20 on both and the spread is strictly
    narrower on L.  Everything else fails (bearish rollover, unchanged spread, widening while
    below, narrowing while above, equal on both candles).
    """
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


def qqq_momentum(
    entry_minute: datetime, qqq_bars: Sequence[SessionBar]
) -> tuple[bool, dict[str, str]]:
    """Rule 40: the IDENTICAL EMA momentum test on QQQ's two most recently completed candles.

    QQQ's price is not compared with its VWAP.  ``entry_minute`` is the start of the candle in
    which the entry would happen: the latest QQQ candle must be the one that just completed
    (INTERPRETATION: a QQQ series that is behind the stock fails closed rather than silently
    testing stale candles).  EMA9/EMA20 need QQQ premarket candles to be seeded (Rule 2).
    """
    if len(qqq_bars) < 2:
        return False, {"qqq_result": "MISSING_CANDLES"}
    prior, latest = qqq_bars[-2], qqq_bars[-1]
    if latest.end != entry_minute:
        return False, {"qqq_result": "STALE_CANDLES", "qqq_latest_end": latest.end.isoformat()}
    ok, label = ema_momentum(prior, latest)
    return ok, {
        "qqq_momentum": label,
        "qqq_ema9_p": str(prior.ema9), "qqq_ema20_p": str(prior.ema20),
        "qqq_ema9_l": str(latest.ema9), "qqq_ema20_l": str(latest.ema20),
        "qqq_result": "PASS" if ok else "FAIL",
    }


def time_gate(moment: datetime) -> bool:
    """Rule 4: 09:34:00 <= entry time < 11:30:00 New York."""
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
    vwap: Decimal | None = None  # the benchmark VWAP: the most recently completed candle's
    reason: str = ""
    facts: dict[str, str] = field(default_factory=dict)


def _filters(
    completed: Sequence[SessionBar],
    entry_minute: datetime,
    qqq_bars: Sequence[SessionBar],
) -> tuple[str | None, dict[str, str]]:
    """Rules 39-40.  Returns ``(reason_for_rejection | None, facts)``; ``completed`` ends at L."""
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
    qqq_ok, qqq_facts = qqq_momentum(entry_minute, qqq_bars)
    facts.update(qqq_facts)
    if not qqq_ok:
        return "QQQ_FILTER_FAILED", facts
    return None, facts


def price_qualifies(price: Decimal, snapshot: Snapshot, last: SessionBar) -> bool:
    """Rules 33-36 for a trade at ``price`` while the entry window is open.

    ``last`` is the most recently COMPLETED candle: its VWAP (and, in Scenario 2, its EMA9 and
    EMA20) are the benchmarks, all taken from that one candle.  Scenario 1 (confirmation close
    <= ceiling): ``confirmation close <= price <= ceiling``.  Scenario 2: ``price <= ceiling`` and
    strictly above VWAP, EMA9 and EMA20 (entry below the confirmation close is allowed).  Both:
    strictly above ``last`` VWAP (Rule 36).
    """
    ceiling, confirm_close = snapshot.entry_ceiling, snapshot.confirm_close
    if ceiling is None or confirm_close is None or last.vwap is None:
        return False
    if not last.vwap < price <= ceiling:
        return False
    if snapshot.scenario is EntryScenario.WITHIN_CEILING:
        return price >= confirm_close
    return (
        last.ema9 is not None
        and last.ema20 is not None
        and price > last.ema9
        and price > last.ema20
    )


def _first_above(value: Decimal, rules: TickRules) -> Decimal:
    """Smallest grid price strictly greater than ``value``."""
    base = rules.floor(value)
    return base + rules.increment_for(base)


def entry_band(
    snapshot: Snapshot, last: SessionBar, rules: TickRules = ALPACA_EQUITY_TICK_RULES
) -> tuple[Decimal, Decimal] | None:
    """Lowest and highest tick-grid prices satisfying :func:`price_qualifies` (``None`` = empty).

    The benchmarks are fixed while a candle develops, so the qualifying prices form one
    interval; only the moment price first enters it is unknown to one-minute OHLC data.
    """
    ceiling, confirm_close = snapshot.entry_ceiling, snapshot.confirm_close
    if ceiling is None or confirm_close is None or last.vwap is None:
        return None
    if snapshot.scenario is EntryScenario.WITHIN_CEILING:
        low = rules.ceil(confirm_close)
        if low <= last.vwap:
            low = _first_above(last.vwap, rules)
    else:
        if last.ema9 is None or last.ema20 is None:
            return None
        low = _first_above(max(last.vwap, last.ema9, last.ema20), rules)
    high = rules.floor(ceiling)
    return (low, high) if low <= high else None


def bar_entry(
    snapshot: Snapshot,
    completed: Sequence[SessionBar],
    window_bar: SessionBar,
    qqq_bars: Sequence[SessionBar],
) -> EntryDecision:
    """OHLC-only emulation of the entry window for the just-completed candle ``window_bar``.

    ``snapshot`` must describe the state *before* ``window_bar``, ``completed`` the bars before
    it and ``qqq_bars`` the QQQ candles completed before it started.  Every benchmark (VWAP,
    EMAs, momentum) comes from completed candles and is therefore fixed for the whole candle;
    the only unknown is WHEN price first enters the qualifying band ``[low, high]``.  One-minute
    OHLC cannot say, so the emulation never invents a favorable path:

    * The candle's open already qualifies: the open print is the first print of the candle, so
      the fill is exact (``OPEN``, stamped at the candle start).
    * The open is below the band and the high reaches it: price must have risen through the
      band's lowest price; the fill is modeled there (``TRIGGER``, stamped at the candle end,
      the latest possible moment).  A jump over the band is not modeled.  If the candle's own
      low would breach the $3.00 dip (Rule 10) the order of the low and the trigger is unknown,
      so no fill is guessed.
    * The open is above the band and the low reaches it: price must have fallen through the
      band's highest price (the entry ceiling), the worst price a pullback entry can print; the
      fill is modeled there (``TRIGGER``).  Any lower low comes after that print.
    * Otherwise nothing: there is no fall-back to a later close or open.
    """
    if snapshot.phase is not Phase.WINDOW or snapshot.confirm_end is None:
        return EntryDecision(False, reason="NO_ACTIVE_WINDOW")
    number = window_number(snapshot.confirm_end, window_bar.start)
    if not 1 <= number <= ENTRY_WINDOW_BARS:
        return EntryDecision(False, reason="OUTSIDE_ENTRY_WINDOW")
    if not time_gate(window_bar.start):
        return EntryDecision(False, window_number=number, reason="OUTSIDE_ENTRY_HOURS")
    if not completed or completed[-1].vwap is None:
        return EntryDecision(False, window_number=number, reason="VWAP_UNAVAILABLE")
    last = completed[-1]
    rejection, facts = _filters(completed, window_bar.start, qqq_bars)
    facts.update(snapshot.facts())
    facts["window_number"] = str(number)
    facts["benchmark_vwap"] = str(last.vwap)
    if rejection is not None:
        return EntryDecision(False, window_number=number, reason=rejection, facts=facts)
    band = entry_band(snapshot, last)
    if band is None:
        return EntryDecision(False, window_number=number, reason="EMPTY_PRICE_BAND", facts=facts)
    low, high = band
    facts["band_low"], facts["band_high"] = str(low), str(high)
    if price_qualifies(window_bar.open, snapshot, last):
        facts["entry_mode"] = EntryMode.OPEN.value
        return EntryDecision(
            True, EntryMode.OPEN, window_bar.open, window_bar.start, number, last.vwap,
            "OPEN_FILL", facts,
        )
    if window_bar.open < low <= window_bar.high:
        setup_low = snapshot.setup_low
        if (
            setup_low is not None
            and window_bar.low < setup_low
            and window_bar.vwap is not None
            and window_bar.vwap - window_bar.low > MAX_DIP
        ):
            return EntryDecision(
                False, window_number=number, reason="DIP_BEFORE_TRIGGER", facts=facts
            )
        facts["entry_mode"] = EntryMode.TRIGGER.value
        return EntryDecision(
            True, EntryMode.TRIGGER, low, window_bar.end, number, last.vwap, "TRIGGER_FILL", facts
        )
    if window_bar.low <= high < window_bar.open:
        facts["entry_mode"] = EntryMode.TRIGGER.value
        return EntryDecision(
            True, EntryMode.TRIGGER, high, window_bar.end, number, last.vwap, "TRIGGER_FILL", facts
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
) -> EntryDecision:
    """Trade-tick evaluation of the entry window (Rules 32-38) for ``price`` printed at ``at``.

    The window opens with the first print of C+1; ``developing`` is the live M1 candle and
    ``completed`` ends at the most recently completed candle (the benchmark of Rules 33-36).
    The maximum dip is enforced with the developing low (Rule 10 applies until the entry is
    filled).  When a fresh ``ask`` is known it must not exceed the frozen entry ceiling: that is
    an execution safeguard on top of the document (the rules test the trade price) that can only
    defer an entry, never create one.
    """
    if snapshot.phase is not Phase.WINDOW or snapshot.confirm_end is None:
        return EntryDecision(False, reason="NO_ACTIVE_WINDOW")
    number = window_number(snapshot.confirm_end, developing.start)
    if not 1 <= number <= ENTRY_WINDOW_BARS:
        return EntryDecision(False, reason="OUTSIDE_ENTRY_WINDOW")
    if not time_gate(at):
        return EntryDecision(False, window_number=number, reason="OUTSIDE_ENTRY_HOURS")
    assert snapshot.confirm_close is not None and snapshot.entry_ceiling is not None
    if snapshot.setup_low is not None and snapshot.setup_low_vwap is not None:
        low, low_vwap = snapshot.setup_low, snapshot.setup_low_vwap
        if developing.low < low:
            low, low_vwap = developing.low, vwap_now
        if low_vwap - low > MAX_DIP:
            return EntryDecision(False, window_number=number, reason="MAX_DIP_BREACHED_INTRABAR")
    if not completed or completed[-1].vwap is None:
        return EntryDecision(False, window_number=number, reason="VWAP_UNAVAILABLE")
    last = completed[-1]
    rejection, facts = _filters(completed, developing.start, qqq_bars)
    facts.update(snapshot.facts())
    facts["window_number"] = str(number)
    if rejection is not None:
        return EntryDecision(False, window_number=number, reason=rejection, facts=facts)
    if not price_qualifies(price, snapshot, last):
        return EntryDecision(False, window_number=number, reason="PRICE_OUTSIDE_BAND", facts=facts)
    if ask is not None and ask > snapshot.entry_ceiling:
        return EntryDecision(
            False, window_number=number, reason="ASK_ABOVE_ENTRY_CEILING", facts=facts
        )
    facts["entry_mode"] = EntryMode.LIVE.value
    facts["entry_price"] = str(price)
    facts["benchmark_vwap"] = str(last.vwap)
    facts["entry_vwap_distance"] = str(price - last.vwap)
    return EntryDecision(True, EntryMode.LIVE, price, at, number, last.vwap, "LIVE_TRIGGER", facts)


def live_dip_breached(
    snapshot: Snapshot, developing: Candle, vwap_now: Decimal | None
) -> bool:
    """Rule 10 intrabar: the developing candle's low breaches the $3.00 dip of an active setup."""
    if snapshot.phase is Phase.NONE or snapshot.setup_low is None or vwap_now is None:
        return False
    assert snapshot.setup_low_vwap is not None
    low, low_vwap = snapshot.setup_low, snapshot.setup_low_vwap
    if developing.low < low:
        low, low_vwap = developing.low, vwap_now
    return low_vwap - low > MAX_DIP


def as_state_facts(values: dict[str, Any]) -> dict[str, str]:
    return {key: str(value) for key, value in values.items()}
