"""Long entry when the close's z-score falls below the entry threshold."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, crossed_below, finish, gate, insufficient, known
from mike_trade.strategies.indicators.patterns import z_score

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "z_score_mean_reversion"
TIMEFRAME = CandleTimeframe.M1


class ZScoreMeanReversionBuy(BuyStrategy):
    """BUY when the rolling z-score of the close crosses from at-or-above to below `-entry_z`.

    ``z = (close - SMA(close, n)) / population SD(close, n)``; entering the stretched-low zone
    is the buy-the-deviation entry of z-score (Bollinger-band) mean-reversion systems.  Unlike
    ``keltner_channel_reversion`` it uses statistical dispersion and fires when the threshold is
    first crossed, not on re-entry.  A flat window has no z-score.  Needs ``period + 1``
    completed bars (21 by default).  Finite windows only.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="mean_reversion", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=21,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("period", ParameterKind.INT, 20, 5, 200,
                          description="Rolling mean/standard-deviation length. Default 20."),
            ParameterSpec("entry_z", ParameterKind.DECIMAL, Decimal(2), Decimal("0.5"), Decimal(5),
                          description="Entry when z falls below minus this value. Default 2."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when the rolling z-score of the close crosses below the negative entry threshold.",
        source=("Ernest P. Chan, Algorithmic Trading: Winning Strategies and Their Rationale (2013), "
                "Bollinger-band/z-score mean reversion (enter when the z-score exceeds an entry "
                "threshold); Hudson & Thames ArbitrageLab, Bollinger Bands Strategy (entryZscore)."),
        execution_limitations=("Long entry on the negative side only; no exit rule is included "
                               "(Chan exits near z = 0, a SELL concern).",
                               "Population standard deviation, as in Bollinger Bands.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        period, entry = v["period"], v["entry_z"]
        assert isinstance(period, int) and isinstance(entry, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        closes = [bar.close for bar in bars[-period - 1:]]
        prior, latest = z_score(closes[:-1]), z_score(closes[1:])
        if not known(prior, latest):
            return insufficient(self.spec, context)
        return finish(self.spec, context, bars[-1], state, crossed_below(prior, -entry, latest, -entry))
