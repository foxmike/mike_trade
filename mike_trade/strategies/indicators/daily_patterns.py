"""Pure geometry for multi-session (daily-chart) base patterns: trend template, VCP, cup and handle.

Everything here is a deterministic function of a *completed, consecutive* D1 candle history; it
reads nothing else.  The callers (``volatility_contraction_pattern``, ``cup_and_handle``) first
validate the history (``market_data.session_candles.history_issue``: no missing session, newest
candle is the last settled session, no suspected unadjusted corporate action), so the functions
here may assume consecutive sessions.

Causality: swing pivots are fractals confirmed by ``strength`` later sessions; a pivot is used only
when those sessions exist in the supplied history.  No quantity here looks beyond the last
supplied candle, so evaluating a prefix of a history yields the same result a live system had at
that prefix's date (tested).

Thresholds are explicit parameters.  The published methodologies (Minervini's VCP and trend
template, O'Neil's cup with handle) are largely qualitative or give ranges, so every number in
the dataclasses below is an engineering default, not a quoted rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from mike_trade.market_data.candles import Candle
from mike_trade.strategies.indicators.core import sma

__all__ = [
    "TrendTemplate",
    "Contraction",
    "VcpParams",
    "VcpBase",
    "CupParams",
    "CupBase",
    "daily_swing_pivots",
    "trend_template_issue",
    "find_vcp",
    "find_cup_and_handle",
    "quadratic_bottom_fit",
]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HALF = Decimal("0.5")


def daily_swing_pivots(
    candles: Sequence[Candle], strength: int
) -> tuple[list[tuple[int, Decimal]], list[tuple[int, Decimal]]]:
    """Confirmed fractal swing ``(highs, lows)`` as ``(index, price)`` lists on a D1 history.

    A high (low) is a swing when it is strictly above (below) the highs (lows) of the ``strength``
    sessions on each side, so it is reported only once ``strength`` later sessions exist.
    """
    highs: list[tuple[int, Decimal]] = []
    lows: list[tuple[int, Decimal]] = []
    for centre in range(strength, len(candles) - strength):
        around = [c for i, c in enumerate(candles[centre - strength: centre + strength + 1])
                  if i != strength]
        if candles[centre].high > max(c.high for c in around):
            highs.append((centre, candles[centre].high))
        if candles[centre].low < min(c.low for c in around):
            lows.append((centre, candles[centre].low))
    return highs, lows


# ---------------------------------------------------------------------------- trend template


@dataclass(frozen=True, slots=True)
class TrendTemplate:
    """Stage-2 trend qualification on daily closes (Minervini's trend template, parameterised).

    Defaults follow the template as commonly documented: price above the 150- and 200-day
    averages, 150 above 200, the 200-day rising for at least a month, 50 above both, price above
    the 50, at least 30% above the 52-week low and within 25% of the 52-week high.  The
    relative-strength rating the template also lists needs cross-sectional data and is not used.
    """

    fast: int = 50
    mid: int = 150
    slow: int = 200
    rising_sessions: int = 21
    range_sessions: int = 252
    min_above_low: Decimal = Decimal("0.30")
    max_below_high: Decimal = Decimal("0.25")

    @property
    def required_sessions(self) -> int:
        return max(self.slow + self.rising_sessions, self.range_sessions)


def trend_template_issue(daily: Sequence[Candle], template: TrendTemplate) -> str | None:
    """The first failed template condition (a name), or ``None`` when the stock qualifies.

    ``daily`` must hold at least ``template.required_sessions`` candles (caller checks).
    """
    closes = [c.close for c in daily]
    last = closes[-1]
    fast = sma(closes, template.fast)[-1]
    mid = sma(closes, template.mid)[-1]
    slow_series = sma(closes, template.slow)
    slow, slow_before = slow_series[-1], slow_series[-1 - template.rising_sessions]
    assert fast is not None and mid is not None and slow is not None and slow_before is not None
    window = daily[-template.range_sessions:]
    high_52, low_52 = max(c.high for c in window), min(c.low for c in window)
    checks = (
        ("price_above_mid_and_slow", last > mid and last > slow),
        ("mid_above_slow", mid > slow),
        ("slow_rising", slow > slow_before),
        ("fast_above_mid_and_slow", fast > mid and fast > slow),
        ("price_above_fast", last > fast),
        ("above_52w_low", last >= low_52 * (_ONE + template.min_above_low)),
        ("near_52w_high", last >= high_52 * (_ONE - template.max_below_high)),
    )
    for name, passed in checks:
        if not passed:
            return name
    return None


# ------------------------------------------------------------------------------------- VCP


@dataclass(frozen=True, slots=True)
class Contraction:
    """One pullback: a confirmed swing high and the lowest low that followed it."""

    high_index: int
    high: Decimal
    low_index: int
    low: Decimal

    @property
    def depth(self) -> Decimal:
        return (self.high - self.low) / self.high


@dataclass(frozen=True, slots=True)
class VcpParams:
    strength: int = 2
    base_sessions: int = 120          # longest base searched (sessions, newest last)
    min_base_sessions: int = 15       # first contraction high to the latest session
    min_contractions: int = 2
    max_contractions: int = 6
    max_first_depth: Decimal = Decimal("0.35")
    max_last_depth: Decimal = Decimal("0.10")
    min_depth: Decimal = Decimal("0.01")          # a "contraction" shallower than this is noise
    contraction_ratio: Decimal = Decimal("0.85")  # each depth <= ratio * the previous depth
    high_tolerance: Decimal = Decimal("0.10")     # contraction highs within this of the highest
    max_pivot_distance: Decimal = Decimal("0.05")  # latest close at most this far below the pivot
    dry_up_sessions: int = 10
    volume_sessions: int = 50
    dry_up_ratio: Decimal = Decimal("0.90")       # recent mean volume <= ratio * baseline mean


@dataclass(frozen=True, slots=True)
class VcpBase:
    """A qualifying volatility-contraction base, as known after the last supplied session."""

    pivot: Decimal                       # high of the last contraction: the breakout level
    contractions: tuple[Contraction, ...]
    depths: tuple[Decimal, ...]
    dry_up: Decimal                      # recent mean volume / baseline mean volume
    pivot_index: int
    last_index: int


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values))


def find_vcp(daily: Sequence[Candle], p: VcpParams) -> VcpBase | str:
    """The VCP base ending at the last session, or the name of the first failed requirement."""
    last = len(daily) - 1
    floor_index = max(0, len(daily) - p.base_sessions)
    swing_highs, _ = daily_swing_pivots(daily, p.strength)
    highs = [(i, h) for i, h in swing_highs if i >= floor_index]
    if len(highs) < 2:
        return "too_few_swing_highs"
    # Pullbacks: each swing high to the lowest low before the next swing high; the last one is
    # still open and runs to the latest session (the lowest low observed so far).
    raw: list[Contraction] = []
    for (i, h), (j, _next_high) in zip(highs, highs[1:], strict=False):
        span = daily[i + 1: j + 1]
        low_offset = min(range(len(span)), key=lambda k: (span[k].low, k))
        raw.append(Contraction(i, h, i + 1 + low_offset, span[low_offset].low))
    i_last, h_last = highs[-1]
    if i_last >= last:
        return "no_pullback_after_last_swing_high"
    tail = daily[i_last + 1:]
    tail_offset = min(range(len(tail)), key=lambda k: (tail[k].low, k))
    raw.append(Contraction(i_last, h_last, i_last + 1 + tail_offset, tail[tail_offset].low))

    # Longest run of successively smaller pullbacks ending at the open one.
    run = [raw[-1]]
    for earlier in reversed(raw[:-1]):
        if run[0].depth <= earlier.depth * p.contraction_ratio:
            run.insert(0, earlier)
        else:
            break
    run = run[-p.max_contractions:]
    if len(run) < p.min_contractions:
        return "contractions_not_successively_smaller"
    depths = tuple(c.depth for c in run)
    if any(d < p.min_depth for d in depths):
        return "contraction_too_shallow"
    if depths[0] > p.max_first_depth:
        return "first_contraction_too_deep"
    if depths[-1] > p.max_last_depth:
        return "last_contraction_too_loose"
    top = max(c.high for c in run)
    if any(c.high < top * (_ONE - p.high_tolerance) for c in run):
        return "contraction_highs_drift"
    if not p.min_base_sessions <= last - run[0].high_index <= p.base_sessions:
        return "base_length"
    pivot = run[-1].high
    if any(c.close > pivot for c in daily[run[-1].high_index + 1:]):
        return "already_broken_out"
    if daily[-1].close < pivot * (_ONE - p.max_pivot_distance):
        return "far_from_pivot"
    if len(daily) < p.volume_sessions:
        return "insufficient_volume_history"
    volumes = [c.volume for c in daily]
    baseline = _mean(volumes[-p.volume_sessions:])
    if baseline <= 0:
        return "no_volume"
    dry_up = _mean(volumes[-p.dry_up_sessions:]) / baseline
    if dry_up > p.dry_up_ratio:
        return "volume_not_drying_up"
    return VcpBase(pivot, tuple(run), depths, dry_up, run[-1].high_index, last)


# --------------------------------------------------------------------------- cup and handle


@dataclass(frozen=True, slots=True)
class CupParams:
    strength: int = 3
    min_cup_sessions: int = 35         # left rim to right rim (about seven weeks)
    max_cup_sessions: int = 130        # about twenty-six weeks
    min_depth: Decimal = Decimal("0.12")
    max_depth: Decimal = Decimal("0.35")
    max_rim_difference: Decimal = Decimal("0.08")   # right rim at most this far below the left rim
    min_u_fraction: Decimal = Decimal("0.60")       # share of cup closes in the lower half of depth
    min_bottom_position: Decimal = Decimal("0.20")  # lowest low within [p, 1-p] of the cup width
    max_counter_move: Decimal = Decimal("0.35")     # fraction of depth any rally/decline may reverse
    min_handle_sessions: int = 5
    max_handle_sessions: int = 25
    min_handle_depth: Decimal = Decimal("0.02")
    max_handle_depth: Decimal = Decimal("0.15")
    handle_in_upper: Decimal = Decimal("0.50")      # handle low at least this far up the cup
    handle_volume_ratio: Decimal = Decimal("0.90")  # mean handle volume <= ratio * mean cup volume
    prior_uptrend_sessions: int = 80
    min_prior_advance: Decimal = Decimal("0.30")

    @property
    def required_sessions(self) -> int:
        return self.max_cup_sessions + self.max_handle_sessions + self.prior_uptrend_sessions + 2 * self.strength


@dataclass(frozen=True, slots=True)
class CupBase:
    """A qualifying cup with handle, as known after the last supplied session."""

    pivot: Decimal            # highest high from the right rim through the latest session
    left_index: int
    bottom_index: int
    right_index: int
    last_index: int
    depth: Decimal
    handle_depth: Decimal
    u_fraction: Decimal
    bottom_position: Decimal
    handle_volume_ratio: Decimal


def quadratic_bottom_fit(closes: Sequence[Decimal]) -> tuple[Decimal, Decimal] | None:
    """Least-squares parabola ``close ~ a*x^2 + b*x + c`` on ``x`` in ``[0, 1]``.

    Returns ``(a, x_vertex)``; ``a > 0`` is a cup (concave up) and ``x_vertex`` is where its
    bottom lies.  ``None`` when the system is degenerate.  Exposed for diagnostics and tests; the
    rounding gate itself uses the robust time-in-bottom share (see :func:`find_cup_and_handle`).
    """
    n = len(closes)
    if n < 3:
        return None
    xs = [Decimal(i) / Decimal(n - 1) for i in range(n)]
    powers = [[_ONE, x, x * x, x * x * x, x * x * x * x] for x in xs]  # (0**0 is undefined in Decimal)
    s = [sum((row[k] for row in powers), _ZERO) for k in range(5)]
    t = [sum((closes[i] * powers[i][k] for i in range(n)), _ZERO) for k in range(3)]
    # normal equations for [c, b, a]
    m = [[s[0], s[1], s[2], t[0]], [s[1], s[2], s[3], t[1]], [s[2], s[3], s[4], t[2]]]
    for col in range(3):
        pivot_row = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if m[pivot_row][col] == 0:
            return None
        m[col], m[pivot_row] = m[pivot_row], m[col]
        for r in range(3):
            if r != col:
                factor = m[r][col] / m[col][col]
                m[r] = [m[r][k] - factor * m[col][k] for k in range(4)]
    c, b, a = (m[i][3] / m[i][i] for i in range(3))
    if a == 0:
        return None
    return a, -b / (2 * a)


def _counter_move_ok(
    segment: Sequence[Decimal], depth: Decimal, limit: Decimal, *, descending: bool
) -> bool:
    """No rally (on the way down) / drop (on the way up) larger than ``limit`` of the cup depth."""
    worst = _ZERO
    if descending:
        low = segment[0]
        for value in segment:
            low = min(low, value)
            worst = max(worst, value - low)
    else:
        high = segment[0]
        for value in segment:
            high = max(high, value)
            worst = max(worst, high - value)
    return worst <= limit * depth


def find_cup_and_handle(daily: Sequence[Candle], p: CupParams) -> CupBase | str:
    """The cup with handle ending at the last session, or the first failed requirement.

    Geometry (all from completed sessions; pivots confirmed by ``strength`` later sessions):

    * *Rims.* Left rim ``L`` and right rim ``R`` are confirmed swing highs, ``min/max_cup_sessions``
      apart, with ``R`` no more than ``max_rim_difference`` below ``L``.  Candidates are tried from
      the most recent right rim backwards, and for a right rim the highest eligible left rim first.
    * *Depth.* ``D = high_L - bottom`` where ``bottom`` is the lowest low between the rims; the
      relative depth is within ``[min_depth, max_depth]`` of ``high_L``.
    * *Rounded, not V-shaped.* With ``y_i = (high_L - close_i) / D`` over the cup, at least
      ``min_u_fraction`` of the closes have ``y >= 1/2`` (a straight V spends 1/2 of its time there,
      a parabola 0.707) and the lowest low lies within ``[min_bottom_position, 1 - min_bottom_position]``
      of the cup's width.
    * *Smooth sides.* From ``L`` to the bottom no rally, and from the bottom to ``R`` no decline,
      exceeds ``max_counter_move * D`` (this is what separates a cup from a double bottom's W).
    * *Handle.* The ``min/max_handle_sessions`` after ``R``: its low stays in the upper part of the cup
      (``handle_in_upper``), its depth below ``high_R`` is within the handle limits and at most half
      the cup depth, no handle close is above ``high_R`` (not yet broken out), and its mean volume is
      at most ``handle_volume_ratio`` of the cup's.
    * *Prior advance.* The left rim is at least ``min_prior_advance`` above the lowest low of the
      ``prior_uptrend_sessions`` before it.
    * *Pivot.* The highest high from ``R`` through the latest session; entry needs a later,
      intraday, close above it.
    """
    last = len(daily) - 1
    highs, _ = daily_swing_pivots(daily, p.strength)
    candidate_rights = [
        (i, h) for i, h in highs
        if p.min_handle_sessions <= last - i <= p.max_handle_sessions
    ]
    if not candidate_rights:
        return "no_right_rim"
    reason = "no_cup"
    for ir, hr in reversed(candidate_rights):
        lefts = [
            (i, h) for i, h in highs
            if p.min_cup_sessions <= ir - i <= p.max_cup_sessions
            and hr >= h * (_ONE - p.max_rim_difference)
        ]
        for il, hl in sorted(lefts, key=lambda item: (-item[1], -item[0])):
            outcome = _cup_between(daily, il, hl, ir, hr, last, p)
            if isinstance(outcome, CupBase):
                return outcome
            reason = outcome
    return reason


def _cup_between(
    daily: Sequence[Candle], il: int, hl: Decimal, ir: int, hr: Decimal, last: int, p: CupParams
) -> CupBase | str:
    cup = daily[il: ir + 1]
    bottom_offset = min(range(len(cup)), key=lambda k: (cup[k].low, k))
    bottom = cup[bottom_offset].low
    ib = il + bottom_offset
    depth_abs = hl - bottom
    depth = depth_abs / hl
    if not p.min_depth <= depth <= p.max_depth:
        return "cup_depth"
    width = Decimal(len(cup) - 1)
    position = Decimal(bottom_offset) / width
    if not p.min_bottom_position <= position <= _ONE - p.min_bottom_position:
        return "bottom_not_central"
    closes = [c.close for c in cup]
    in_lower_half = sum(1 for c in closes if (hl - c) / depth_abs >= _HALF)
    u_fraction = Decimal(in_lower_half) / Decimal(len(closes))
    if u_fraction < p.min_u_fraction:
        return "v_shaped"
    if not _counter_move_ok(closes[: bottom_offset + 1], depth_abs, p.max_counter_move,
                            descending=True):
        return "left_side_not_smooth"
    if not _counter_move_ok(closes[bottom_offset:], depth_abs, p.max_counter_move,
                            descending=False):
        return "right_side_not_smooth"
    handle = daily[ir + 1: last + 1]
    handle_low = min(c.low for c in handle)
    handle_depth = (hr - handle_low) / hr
    if not p.min_handle_depth <= handle_depth <= p.max_handle_depth:
        return "handle_depth"
    if handle_depth > depth * _HALF:
        return "handle_deeper_than_half_the_cup"
    if handle_low < bottom + depth_abs * p.handle_in_upper:
        return "handle_not_in_upper_cup"
    if any(c.close > hr for c in handle):
        return "already_broken_out"
    cup_volume = _mean([c.volume for c in cup])
    if cup_volume <= 0:
        return "no_volume"
    volume_ratio = _mean([c.volume for c in handle]) / cup_volume
    if volume_ratio > p.handle_volume_ratio:
        return "handle_volume_not_drying_up"
    before = daily[max(0, il - p.prior_uptrend_sessions): il]
    if len(before) < p.prior_uptrend_sessions:
        return "insufficient_prior_history"
    if hl < min(c.low for c in before) * (_ONE + p.min_prior_advance):
        return "no_prior_uptrend"
    pivot = max(c.high for c in daily[ir: last + 1])
    return CupBase(pivot, il, ib, ir, last, depth, handle_depth, u_fraction, position, volume_ratio)
