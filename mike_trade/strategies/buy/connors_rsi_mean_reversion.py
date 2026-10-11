"""Long entry when ConnorsRSI drops into oversold territory."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, crossed_below, finish, gate, insufficient, known
from mike_trade.strategies.indicators.extended import ConnorsRSICalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "connors_rsi_mean_reversion"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class ConnorsRsiMeanReversionBuy(BuyStrategy):
    """BUY when ConnorsRSI crosses from at-or-above to below `threshold` (default 10).

    ``ConnorsRSI = (RSI(close, 3) + RSI(streak, 2) + PercentRank(ROC(1), 100)) / 3`` where the
    streak is the signed count of consecutive up/down closes, and PercentRank is the share of
    the previous 100 one-bar percent changes strictly below the current one.  Entering the
    oversold zone is a buy-the-weakness mean-reversion entry.  Distinct from
    ``rsi_oversold_reversal`` (a single RSI turning up).  Recursive RSIs and the ROC window are
    resumed from persisted state.  Needs ``rank_period + 3`` completed bars (103 by default), so
    SHADOW warmup must be configured accordingly.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="mean_reversion", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=103,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("rsi_period", ParameterKind.INT, 3, 2, 50,
                          description="Wilder RSI length of the close. Default 3."),
            ParameterSpec("streak_period", ParameterKind.INT, 2, 2, 50,
                          description="Wilder RSI length of the up/down streak. Default 2."),
            ParameterSpec("rank_period", ParameterKind.INT, 100, 10, 300,
                          description="PercentRank look-back for the one-bar change. Default 100."),
            ParameterSpec("threshold", ParameterKind.DECIMAL, Decimal(10), Decimal(1), Decimal(50),
                          description="Oversold level; entry on a cross below it. Default 10."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when ConnorsRSI(3,2,100) falls from at-or-above to below the oversold "
                     "threshold on a completed bar."),
        source=("Larry Connors and Cesar Alvarez, Short Term Trading Strategies That Work; "
                "Connors Research ConnorsRSI; StockCharts ChartSchool, ConnorsRSI (oversold below 10)."),
        execution_limitations=("No trend filter or exit is applied here (Connors' published "
                               "variants add a 200-bar trend filter; that is left to composition).",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        rank = cls.spec.parameter_schema.validate(params)["rank_period"]
        assert isinstance(rank, int)
        return rank + 3

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        rsi, streak, rank, threshold = (v["rsi_period"], v["streak_period"],
                                        v["rank_period"], v["threshold"])
        assert isinstance(rsi, int) and isinstance(streak, int) and isinstance(rank, int)
        assert isinstance(threshold, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, rank + 3)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{rsi}|{streak}|{rank}",
                      fresh=lambda: ConnorsRSICalculator(rsi, streak, rank),
                      restore=ConnorsRSICalculator.restore,
                      feed=lambda calc, bar: calc.update(bar.close))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]) or not known(adv.prior, adv.latest):
            return insufficient(self.spec, context, adv.updates)
        return finish(self.spec, context, bars[-1], state,
                      crossed_below(adv.prior, threshold, adv.latest, threshold), adv.updates)
