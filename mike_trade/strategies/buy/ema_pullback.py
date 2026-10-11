"""Long entry on a pullback into the fast-EMA value zone of a rising trend."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, finish, gate, insufficient, known, parts, require_param
from mike_trade.strategies.indicators.core import MACDCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "ema_pullback"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class EmaPullbackBuy(BuyStrategy):
    """BUY the first bar whose low dips to the fast EMA while the slow EMA is rising.

    Elder's "value zone" is the band between a fast and a slow EMA.  With the slow EMA rising
    and the fast above it (uptrend), the bar buys when ``low[t] <= fast EMA[t]`` (price has
    pulled back into value), ``close[t] > slow EMA[t]`` (the trend has not broken) and the
    previous bar had not yet reached the fast EMA (``low[t-1] > fast EMA[t-1]``: the first
    touch).  EMAs are SMA-seeded and resumed from persisted state.  Needs ``slow + 1``
    completed bars (27 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="moving_average", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=27,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("fast_period", ParameterKind.INT, 13, 2, 100,
                          description="Fast EMA length (upper edge of the value zone). Default 13."),
            ParameterSpec("slow_period", ParameterKind.INT, 26, 3, 200,
                          description="Slow EMA length (trend and lower edge); must exceed fast_period. Default 26."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when, with the fast EMA above a rising slow EMA, the bar's low first "
                     "reaches the fast EMA while the close stays above the slow EMA."),
        source=("Alexander Elder, Trading for a Living (1993) and The New Trading for a Living "
                "(2014): buy pullbacks into the value zone between the 13- and 26-day EMAs "
                "when the slow EMA is rising."),
        execution_limitations=("Entry signal only: no stops, sizing, targets, or orders.",
                               "Elder's daily-bar 13/26 EMAs adapted to M1 bars.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        slow = cls.spec.parameter_schema.validate(params)["slow_period"]
        assert isinstance(slow, int)
        return slow + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        fast, slow = v["fast_period"], v["slow_period"]
        assert isinstance(fast, int) and isinstance(slow, int)
        require_param(fast < slow, "fast_period < slow_period", "fast_period")
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, slow + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{fast}|{slow}",
                      fresh=lambda: MACDCalculator(fast, slow, None),
                      restore=MACDCalculator.restore,
                      feed=lambda calc, bar: calc.update(bar.close))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        fast_prior, slow_prior = parts(adv.prior, 4)[:2]
        fast_latest, slow_latest = parts(adv.latest, 4)[:2]
        if not known(fast_prior, slow_prior, fast_latest, slow_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = (fast_latest > slow_latest and slow_latest > slow_prior
                     and bars[-1].low <= fast_latest and bars[-1].close > slow_latest
                     and bars[-2].low > fast_prior)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
