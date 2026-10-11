"""Cup and Handle BUY on completed daily candles with an intraday pivot breakout."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.buy._daily_breakout import evaluate_daily_breakout
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, require_param
from mike_trade.strategies.indicators.daily_patterns import CupParams, find_cup_and_handle

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cup_and_handle"
STRATEGY_VERSION = "1.0.0"
M1, D1 = CandleTimeframe.M1, CandleTimeframe.D1
PCT = Decimal(100)
STATE_DAILY_PATTERN = "daily_pattern"
VOLUME_SESSIONS = 50


def _int(name: str, default: int, low: int, high: int, text: str) -> ParameterSpec:
    return ParameterSpec(name, ParameterKind.INT, default, low, high, description=text)


def _dec(name: str, default: str, low: str, high: str, text: str) -> ParameterSpec:
    return ParameterSpec(
        name, ParameterKind.DECIMAL, Decimal(default), Decimal(low), Decimal(high), description=text
    )


def _params(v: Mapping[str, object]) -> CupParams:
    return CupParams(
        strength=v["strength"],  # type: ignore[arg-type]
        min_cup_sessions=v["min_cup_sessions"],  # type: ignore[arg-type]
        max_cup_sessions=v["max_cup_sessions"],  # type: ignore[arg-type]
        min_depth=v["min_depth_pct"] / PCT,  # type: ignore[operator]
        max_depth=v["max_depth_pct"] / PCT,  # type: ignore[operator]
        max_rim_difference=v["max_rim_difference_pct"] / PCT,  # type: ignore[operator]
        min_u_fraction=v["min_u_fraction"],  # type: ignore[arg-type]
        min_bottom_position=v["min_bottom_position"],  # type: ignore[arg-type]
        max_counter_move=v["max_counter_move"],  # type: ignore[arg-type]
        min_handle_sessions=v["min_handle_sessions"],  # type: ignore[arg-type]
        max_handle_sessions=v["max_handle_sessions"],  # type: ignore[arg-type]
        min_handle_depth=v["min_handle_depth_pct"] / PCT,  # type: ignore[operator]
        max_handle_depth=v["max_handle_depth_pct"] / PCT,  # type: ignore[operator]
        handle_in_upper=v["handle_in_upper"],  # type: ignore[arg-type]
        handle_volume_ratio=v["handle_volume_ratio"],  # type: ignore[arg-type]
        prior_uptrend_sessions=v["prior_uptrend_sessions"],  # type: ignore[arg-type]
        min_prior_advance=v["min_prior_advance_pct"] / PCT,  # type: ignore[operator]
    )


class CupAndHandleBuy(BuyStrategy):
    """BUY the intraday breakout of a William O'Neil-style cup with handle on daily candles.

    **Daily qualification** (completed D1 candles through the last settled session; pivots are
    confirmed swing highs, ``strength`` sessions on each side):

    * *Cup.* Left rim ``L`` and right rim ``R`` are ``min_cup_sessions``..``max_cup_sessions`` apart (35..130
      by default: seven to twenty-six weeks), ``R`` no more than ``max_rim_difference_pct`` below ``L``;
      depth ``D = high_L - lowest low`` is ``min_depth_pct``..``max_depth_pct`` of ``high_L``.
    * *Rounded, not V-shaped (explicit maths).* With ``y = (high_L - close) / D`` over the cup, at least
      ``min_u_fraction`` (0.60) of the closes have ``y >= 1/2`` - a straight V spends exactly half its time
      there, a parabola ``0.707`` - and the lowest low lies in the central ``[min_bottom_position,
      1 - min_bottom_position]`` of the cup width.
    * *Smooth sides.* From ``L`` down to the bottom no rally, and from the bottom up to ``R`` no decline,
      exceeds ``max_counter_move * D``; a double bottom's W fails this.
    * *Handle.* ``min_handle_sessions``..``max_handle_sessions`` after ``R``: depth ``min_handle_depth_pct``..
      ``max_handle_depth_pct`` below ``high_R`` and at most half the cup depth, its low in the upper
      ``handle_in_upper`` of the cup, no handle close above ``high_R``, and mean handle volume at most
      ``handle_volume_ratio`` of the cup's.
    * *Prior advance.* ``L`` is at least ``min_prior_advance_pct`` above the lowest low of the
      ``prior_uptrend_sessions`` before it.
    * *Pivot.* The highest high from ``R`` through the latest session.

    **Intraday trigger:** the first completed M1 close of the current session above the pivot, optionally
    with pro-rated session volume at least ``breakout_volume_ratio`` times the average daily volume
    (see ``_daily_breakout``).  A V-shaped recovery, a double bottom or a plain resistance breakout does
    not qualify.  Candidates are tried from the latest right rim back, highest eligible left rim first,
    so the choice is deterministic and uses only completed sessions.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.BUY,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(M1, D1),
        warmup_bars=1,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=M1,
        history_bars=((D1, 241),),
        parameter_schema=ParameterSchema((
            _int("strength", 3, 1, 8, "Sessions on each side of a daily swing high. Default 3."),
            _int("min_cup_sessions", 35, 15, 200, "Shortest cup, left rim to right rim. Default 35."),
            _int("max_cup_sessions", 130, 40, 325, "Longest cup, left rim to right rim. Default 130."),
            _dec("min_depth_pct", "12", "3", "50", "Minimum cup depth, percent of the left rim. Default 12."),
            _dec("max_depth_pct", "35", "10", "60", "Maximum cup depth, percent of the left rim. Default 35."),
            _dec("max_rim_difference_pct", "8", "0", "25",
                 "Right rim at most this percent below the left rim. Default 8."),
            _dec("min_u_fraction", "0.6", "0.5", "0.95",
                 "Minimum share of cup closes in the lower half of the depth (V = 0.5). Default 0.6."),
            _dec("min_bottom_position", "0.2", "0.05", "0.45",
                 "The lowest low lies within [p, 1-p] of the cup width. Default 0.2."),
            _dec("max_counter_move", "0.35", "0.05", "1",
                 "Largest rally on the way down / decline on the way up, fraction of depth. Default 0.35."),
            _int("min_handle_sessions", 5, 3, 30, "Shortest handle, in sessions. Default 5."),
            _int("max_handle_sessions", 25, 5, 60, "Longest handle, in sessions. Default 25."),
            _dec("min_handle_depth_pct", "2", "0.5", "20", "Minimum handle depth, percent. Default 2."),
            _dec("max_handle_depth_pct", "15", "2", "40", "Maximum handle depth, percent. Default 15."),
            _dec("handle_in_upper", "0.5", "0.3", "0.9",
                 "Handle low at least this fraction up the cup depth. Default 0.5."),
            _dec("handle_volume_ratio", "0.9", "0.1", "1",
                 "Mean handle volume at most this fraction of the cup's. Default 0.9."),
            _int("prior_uptrend_sessions", 80, 20, 250,
                 "Sessions before the left rim searched for the advance. Default 80."),
            _dec("min_prior_advance_pct", "30", "5", "300",
                 "Left rim at least this percent above the prior low. Default 30."),
            _dec("breakout_volume_ratio", "1.4", "0", "10",
                 "Session volume vs pro-rated average daily volume at the breakout; 0 disables. "
                 "Default 1.4."),
            _dec("max_overnight_move_pct", "25", "5", "100",
                 "A larger overnight move in the daily history is treated as a possible "
                 "unadjusted split and disables the pattern. Default 25."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_DAILY_PATTERN),
        methodology=(
            "BUY the first completed M1 close above the pivot of a cup with handle: a rounded (not "
            "V-shaped) base of 35-130 sessions and 12-35% depth after a prior advance, a shallow, "
            "quiet handle in the upper half of the cup, and no daily close yet above the pivot. The "
            "pattern is evaluated on completed D1 candles only."
        ),
        source=(
            "Concept: William J. O'Neil's cup with handle (How to Make Money in Stocks): a rounded "
            "base after an uptrend, a short shallow handle, entry on a breakout above the pivot. "
            "The published guidance is expressed in weeks and gives typical ranges rather than a "
            "formula; the rounding has no published numeric test. The geometry above - including "
            "the time-in-lower-half rounding test, the smooth-side test and every range - is this "
            "library's explicit, configurable engineering definition, not a quoted rule. The "
            "intraday trigger and breakout-volume test are this library's signal-timing choices."
        ),
        execution_limitations=(
            "Entry signal only: no stops, sizing, targets, or orders.",
            "Needs completed D1 candles (241 by default) from the shared market-data service: RAW "
            "basis, same feed as the M1 candles, no missing session, newest candle = last settled "
            "session, no overnight move beyond max_overnight_move_pct; otherwise INSUFFICIENT_DATA.",
            "Not a double bottom (the W fails the smooth-side test), not a V recovery (fails the "
            "rounding test) and not a plain resistance breakout (needs the cup, prior advance and handle).",
            "Regular session only; one signal per session at most; the pivot is recomputed once per "
            "settled session and cached in strategy state.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_history(cls, params: Mapping[str, object]) -> Mapping[CandleTimeframe, int]:
        v = cls.spec.parameter_schema.validate(params)
        return {D1: max(_params(v).required_sessions, VOLUME_SESSIONS)}

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        require_param(
            v["min_cup_sessions"] < v["max_cup_sessions"],  # type: ignore[operator]
            "min_cup_sessions < max_cup_sessions", "min_cup_sessions",
        )
        require_param(
            v["min_depth_pct"] < v["max_depth_pct"],  # type: ignore[operator]
            "min_depth_pct < max_depth_pct", "min_depth_pct",
        )
        require_param(
            v["min_handle_sessions"] <= v["max_handle_sessions"],  # type: ignore[operator]
            "min_handle_sessions <= max_handle_sessions", "min_handle_sessions",
        )
        require_param(
            v["min_handle_depth_pct"] < v["max_handle_depth_pct"],  # type: ignore[operator]
            "min_handle_depth_pct < max_handle_depth_pct", "min_handle_depth_pct",
        )
        require_param(
            v["min_handle_sessions"] > v["strength"],  # type: ignore[operator]
            "min_handle_sessions > strength", "min_handle_sessions",
        )
        cup = _params(v)

        def detect(window: tuple) -> tuple[Decimal | None, str]:  # type: ignore[type-arg]
            found = find_cup_and_handle(window, cup)
            return (None, found) if isinstance(found, str) else (found.pivot, "cup_and_handle")

        signature = f"{STRATEGY_ID}|{STRATEGY_VERSION}|" + "|".join(
            f"{k}={v[k]}" for k in sorted(v) if k != "breakout_volume_ratio"
        )
        return evaluate_daily_breakout(
            self.spec, context, state,
            signature=signature,
            required_sessions=max(cup.required_sessions, VOLUME_SESSIONS),
            detect=detect,
            breakout_volume_ratio=v["breakout_volume_ratio"],  # type: ignore[arg-type]
            volume_sessions=VOLUME_SESSIONS,
            max_overnight_move=v["max_overnight_move_pct"] / PCT,  # type: ignore[operator]
        )
