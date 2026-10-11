"""COVER when On-Balance Volume makes a new n-bar high."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    CoverStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_INDICATOR,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    advance,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
    known,
    parts,
)
from mike_trade.strategies.indicators.cumulative import CumulativeLineCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_obv_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period + 2


class CoverObvBreakout(CoverStrategy):
    """COVER when On-Balance Volume rises above its highest value of the previous ``period`` bars.

    OBV adds the bar's volume on an up close, subtracts it on a down close and adds nothing on an
    unchanged close (the first bar adds nothing).  Each bar's breakout threshold is the highest OBV
    of the ``period`` bars *before* it, so the current bar is excluded.  Signal (edge)::

        OBV[t-1] <= max(OBV[t-1-period .. t-2])  AND  OBV[t] > max(OBV[t-period .. t-1])  ->  COVER

    The running sum is recursive and resumed from the strategy's persisted ``indicator_state``
    (the line, the last close and the last ``period`` OBV values) bar by bar, never recomputed
    from the bounded candle window; only comparisons with the line's own recent history are used,
    so the arbitrary starting level is irrelevant.  Absent, foreign-parameter or corrupt state is
    re-seeded from the supplied history.  Needs ``period + 2`` completed bars (22 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="volume",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=22,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period", ParameterKind.INT, 20, 2, 200,
                    description="Bars of OBV history the breakout must exceed. Default 20.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER when OBV exceeds its previous n values (a new n-bar OBV high) for the first "
            "time."
        ),
        source=(
            "Joseph Granville, Granville's New Key to Stock Market Profits (1963), On-Balance "
            "Volume; StockCharts ChartSchool, On Balance Volume (OBV): OBV breaking above "
            "resistance can confirm or lead a price breakout. The n-bar-high form is this "
            "strategy's precise reading, as in the obv_breakout BUY: "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/on-balance-volume-obv"
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Indicator-only breakout; no price confirmation is added.",
            "Completed adjacent M1 bars with volume; regular session only; stale data never signals.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params))

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period = validated["period"]
        assert isinstance(period, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required_bars(validated))
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(
            state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|obv|{period}",
            fresh=lambda: CumulativeLineCalculator("obv", period),
            restore=CumulativeLineCalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        line_prior, high_prior = parts(adv.prior, 2)
        line_latest, high_latest = parts(adv.latest, 2)
        if not adjacent(bars[-2], bars[-1]) or not known(
            line_prior, high_prior, line_latest, high_latest
        ):
            return insufficient(self.spec, context, adv.updates)
        triggered = line_prior <= high_prior and line_latest > high_latest
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
