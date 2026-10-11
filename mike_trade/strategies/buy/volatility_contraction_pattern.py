"""Volatility Contraction Pattern (VCP) BUY on completed daily candles with an intraday breakout."""

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
from mike_trade.strategies.indicators.daily_patterns import (
    TrendTemplate,
    VcpParams,
    find_vcp,
    trend_template_issue,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "volatility_contraction_pattern"
STRATEGY_VERSION = "1.0.0"
M1, D1 = CandleTimeframe.M1, CandleTimeframe.D1
PCT = Decimal(100)
STATE_DAILY_PATTERN = "daily_pattern"


def _int(name: str, default: int, low: int, high: int, text: str) -> ParameterSpec:
    return ParameterSpec(name, ParameterKind.INT, default, low, high, description=text)


def _dec(name: str, default: str, low: str, high: str, text: str) -> ParameterSpec:
    return ParameterSpec(
        name, ParameterKind.DECIMAL, Decimal(default), Decimal(low), Decimal(high), description=text
    )


def _required_sessions(v: Mapping[str, object]) -> int:
    template = _template(v)
    base, strength, volume = v["base_sessions"], v["strength"], v["volume_sessions"]
    assert isinstance(base, int) and isinstance(strength, int) and isinstance(volume, int)
    return max(template.required_sessions, base + strength + 1, volume)


def _template(v: Mapping[str, object]) -> TrendTemplate:
    return TrendTemplate(
        fast=v["fast_ma"], mid=v["mid_ma"], slow=v["slow_ma"],  # type: ignore[arg-type]
        rising_sessions=v["rising_sessions"],  # type: ignore[arg-type]
        range_sessions=v["range_sessions"],  # type: ignore[arg-type]
        min_above_low=v["min_above_low_pct"] / PCT,  # type: ignore[operator]
        max_below_high=v["max_below_high_pct"] / PCT,  # type: ignore[operator]
    )


def _base_params(v: Mapping[str, object]) -> VcpParams:
    return VcpParams(
        strength=v["strength"],  # type: ignore[arg-type]
        base_sessions=v["base_sessions"],  # type: ignore[arg-type]
        min_base_sessions=v["min_base_sessions"],  # type: ignore[arg-type]
        min_contractions=v["min_contractions"],  # type: ignore[arg-type]
        max_contractions=v["max_contractions"],  # type: ignore[arg-type]
        max_first_depth=v["max_first_depth_pct"] / PCT,  # type: ignore[operator]
        max_last_depth=v["max_last_depth_pct"] / PCT,  # type: ignore[operator]
        min_depth=v["min_depth_pct"] / PCT,  # type: ignore[operator]
        contraction_ratio=v["contraction_ratio"],  # type: ignore[arg-type]
        high_tolerance=v["high_tolerance_pct"] / PCT,  # type: ignore[operator]
        max_pivot_distance=v["max_pivot_distance_pct"] / PCT,  # type: ignore[operator]
        dry_up_sessions=v["dry_up_sessions"],  # type: ignore[arg-type]
        volume_sessions=v["volume_sessions"],  # type: ignore[arg-type]
        dry_up_ratio=v["dry_up_ratio"],  # type: ignore[arg-type]
    )


class VolatilityContractionPatternBuy(BuyStrategy):
    """BUY the intraday breakout of a Minervini-style volatility contraction base.

    **Daily qualification** (completed D1 candles through the last settled session):

    1. *Stage-2 trend template* (``TrendTemplate``): price above the 150- and 200-day averages,
       150 above 200, the 200-day rising, 50 above both, price above the 50, at least 30% above the
       52-week low and within 25% of the 52-week high.
    2. *Successive contractions.* Between confirmed swing highs, each pullback (swing high to the
       lowest low before the next swing high; the last one runs to the latest session) is at most
       ``contraction_ratio`` times the previous one, there are ``min_contractions``..``max_contractions``
       of them, the first is at most ``max_first_depth_pct`` deep and the last at most
       ``max_last_depth_pct``, none shallower than ``min_depth_pct``, and the contraction highs stay
       within ``high_tolerance_pct`` of each other.
    3. *Volume contraction.* The mean volume of the last ``dry_up_sessions`` is at most ``dry_up_ratio``
       of the last ``volume_sessions``' mean.
    4. *Pivot.* The high of the last contraction; the latest daily close is within
       ``max_pivot_distance_pct`` below it and no daily close since it has been above it.

    **Intraday trigger** (see ``_daily_breakout``): the first completed M1 close of the current session
    above the pivot, optionally with pro-rated volume at least ``breakout_volume_ratio`` times the
    average daily volume.  Needs ``max(slow_ma + rising_sessions, range_sessions, ...)`` = 252 completed
    daily candles by default and a history with no missing session, current to the last settled
    session, with no overnight move beyond ``max_overnight_move_pct`` (a possible unadjusted split).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.BUY,
        category="technical",
        sub_category="volatility",
        reference_symbols=(),
        required_timeframes=(M1, D1),
        warmup_bars=1,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=M1,
        history_bars=((D1, 252),),
        parameter_schema=ParameterSchema((
            _int("fast_ma", 50, 5, 100, "Fast daily SMA of the trend template. Default 50."),
            _int("mid_ma", 150, 20, 300, "Middle daily SMA of the trend template. Default 150."),
            _int("slow_ma", 200, 50, 400, "Slow daily SMA of the trend template. Default 200."),
            _int("rising_sessions", 21, 5, 126,
                 "Sessions over which the slow SMA must have risen. Default 21 (one month)."),
            _int("range_sessions", 252, 60, 504,
                 "Sessions of the 52-week high/low range. Default 252."),
            _dec("min_above_low_pct", "30", "0", "300",
                 "Minimum percent the close is above the range low. Default 30."),
            _dec("max_below_high_pct", "25", "0", "100",
                 "Maximum percent the close is below the range high. Default 25."),
            _int("strength", 2, 1, 5, "Sessions on each side of a daily swing high. Default 2."),
            _int("base_sessions", 120, 20, 300, "Longest base searched, in sessions. Default 120."),
            _int("min_base_sessions", 15, 5, 120,
                 "Shortest base, first contraction high to latest session. Default 15."),
            _int("min_contractions", 2, 2, 6, "Minimum successive contractions. Default 2."),
            _int("max_contractions", 6, 2, 8, "Maximum contractions considered. Default 6."),
            _dec("max_first_depth_pct", "35", "5", "60",
                 "Maximum depth of the first contraction, percent. Default 35."),
            _dec("max_last_depth_pct", "10", "1", "30",
                 "Maximum depth of the last contraction, percent. Default 10."),
            _dec("min_depth_pct", "1", "0.1", "10",
                 "Shallowest pullback that counts as a contraction, percent. Default 1."),
            _dec("contraction_ratio", "0.85", "0.2", "1",
                 "Each contraction is at most this fraction of the previous one. Default 0.85."),
            _dec("high_tolerance_pct", "10", "1", "30",
                 "Contraction highs must be within this percent of the highest. Default 10."),
            _dec("max_pivot_distance_pct", "5", "0.5", "20",
                 "Latest daily close at most this far below the pivot, percent. Default 5."),
            _int("dry_up_sessions", 10, 3, 60, "Sessions of the recent volume mean. Default 10."),
            _int("volume_sessions", 50, 10, 200, "Sessions of the baseline volume mean. Default 50."),
            _dec("dry_up_ratio", "0.9", "0.1", "1",
                 "Recent mean volume at most this fraction of the baseline. Default 0.9."),
            _dec("breakout_volume_ratio", "1.4", "0", "10",
                 "Session volume vs pro-rated average daily volume at the breakout; 0 disables. "
                 "Default 1.4."),
            _dec("max_overnight_move_pct", "25", "5", "100",
                 "A larger overnight move in the daily history is treated as a possible "
                 "unadjusted split and disables the pattern. Default 25."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_DAILY_PATTERN),
        methodology=(
            "BUY the first completed M1 close above the pivot of a volatility contraction base: a "
            "Stage-2 uptrend (trend template on daily closes), two or more successively smaller "
            "pullbacks between confirmed daily swing highs, drying volume and a daily close that has "
            "not yet broken out. The pattern is evaluated on completed D1 candles only."
        ),
        source=(
            "Concept: Mark Minervini's volatility contraction pattern and trend template (Trade Like "
            "a Stock Market Wizard; Think & Trade Like a Champion): a Stage-2 uptrend, successively "
            "tighter pullbacks, volume drying up, entry on a breakout through the pivot. The "
            "published description is qualitative about contraction counts, ratios and depths, so "
            "every threshold here (including the trend-template percentages, taken as commonly "
            "documented) is a configurable engineering default, not a quoted rule; the template's "
            "relative-strength rating needs cross-sectional data and is not used. The intraday "
            "trigger and the breakout-volume test are this library's signal-timing choices."
        ),
        execution_limitations=(
            "Entry signal only: no stops, sizing, targets, or orders.",
            "Needs completed D1 candles (252 by default) from the shared market-data service: "
            "RAW basis, same feed as the M1 candles, no missing session, newest candle = last "
            "settled session, no overnight move beyond max_overnight_move_pct; otherwise "
            "INSUFFICIENT_DATA. Daily volume is that feed's (IEX-only on the free tier).",
            "Not a Bollinger squeeze or a narrow-range breakout: it requires the trend template and "
            "a sequence of at least two successively smaller pullbacks.",
            "Regular session only; one signal per session at most; the pivot and base are "
            "recomputed once per settled session and cached in strategy state.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_history(cls, params: Mapping[str, object]) -> Mapping[CandleTimeframe, int]:
        return {D1: _required_sessions(cls.spec.parameter_schema.validate(params))}

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        require_param(
            v["fast_ma"] < v["mid_ma"] < v["slow_ma"],  # type: ignore[operator]
            "fast_ma < mid_ma < slow_ma", "fast_ma",
        )
        require_param(
            v["dry_up_sessions"] < v["volume_sessions"],  # type: ignore[operator]
            "dry_up_sessions < volume_sessions", "dry_up_sessions",
        )
        require_param(
            v["min_contractions"] <= v["max_contractions"],  # type: ignore[operator]
            "min_contractions <= max_contractions", "min_contractions",
        )
        require_param(
            v["min_base_sessions"] <= v["base_sessions"],  # type: ignore[operator]
            "min_base_sessions <= base_sessions", "min_base_sessions",
        )
        template, base = _template(v), _base_params(v)

        def detect(window: tuple) -> tuple[Decimal | None, str]:  # type: ignore[type-arg]
            failed = trend_template_issue(window, template)
            if failed is not None:
                return None, f"trend_template:{failed}"
            found = find_vcp(window, base)
            return (None, found) if isinstance(found, str) else (found.pivot, "vcp")

        signature = f"{STRATEGY_ID}|{STRATEGY_VERSION}|" + "|".join(
            f"{k}={v[k]}" for k in sorted(v) if k != "breakout_volume_ratio"
        )
        return evaluate_daily_breakout(
            self.spec, context, state,
            signature=signature,
            required_sessions=_required_sessions(v),
            detect=detect,
            breakout_volume_ratio=v["breakout_volume_ratio"],  # type: ignore[arg-type]
            volume_sessions=v["volume_sessions"],  # type: ignore[arg-type]
            max_overnight_move=v["max_overnight_move_pct"] / PCT,  # type: ignore[operator]
        )
