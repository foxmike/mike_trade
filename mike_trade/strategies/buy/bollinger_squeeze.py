"""Long entry on an upper-band breakout that follows a Bollinger BandWidth squeeze."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient
from mike_trade.strategies.indicators.finite import bollinger
from mike_trade.strategies.indicators.windows import bandwidth

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "bollinger_squeeze"
TIMEFRAME = CandleTimeframe.M1


def _needed(period: int, lookback: int, window: int) -> int:
    """Bars covering every BandWidth in the squeeze window, each with its own lookback."""
    return window + lookback + period - 1


class BollingerSqueezeBuy(BuyStrategy):
    """BUY when the close crosses above the upper Bollinger Band within `release_window` bars of
    a squeeze.

    Squeeze at bar ``j``: BandWidth ``(upper - lower) / middle`` is the lowest of the last
    `lookback` BandWidth values ending at ``j`` (ties count; default 125, a parameter).  Signal at ``t``:
    ``close[t-1] <= upper[t-1]``, ``close[t] > upper[t]`` and a squeeze at some bar in
    ``t - release_window .. t - 1``.  Differs from ``bollinger_band_breakout`` by requiring that
    prior volatility compression.  Bands use the population standard deviation.  Needs
    ``release_window + lookback + period - 1`` completed bars (149 by default), so SHADOW
    warmup must be configured accordingly.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="volatility", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=_needed(20, 125, 5),
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("period", ParameterKind.INT, 20, 5, 100,
                          description="Bollinger SMA/standard-deviation length. Default 20."),
            ParameterSpec("deviations", ParameterKind.DECIMAL, Decimal(2), Decimal("0.5"), Decimal(5),
                          description="Band width in standard deviations. Default 2."),
            ParameterSpec("lookback", ParameterKind.INT, 125, 10, 300,
                          description="BandWidth look-back that defines the squeeze low. Default 125."),
            ParameterSpec("release_window", ParameterKind.INT, 5, 1, 30,
                          description="Bars before the breakout in which a squeeze must have occurred. Default 5."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY when the close crosses above the upper Bollinger Band and BandWidth "
                     "reached its lookback low within the preceding release_window bars."),
        source=("John Bollinger, Bollinger on Bollinger Bands (2001), The Squeeze (a BandWidth "
                "low precedes a significant move; the break of a band gives its direction); "
                "StockCharts ChartSchool, Bollinger BandWidth: "
                "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/bollinger-bandwidth. "
                "The 125-bar BandWidth look-back (about six months of daily bars) is this "
                "strategy's default parameter, not a figure from the cited page."),
        execution_limitations=("Direction is taken from the upper-band breakout (Bollinger treats "
                               "the squeeze itself as direction-neutral).",
                               "Large look-back: needs about 149 M1 bars by default.",
                               "Windows span the supplied completed bars like a continuous chart; "
                               "the crossing bars must be adjacent. Regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        v = cls.spec.parameter_schema.validate(params)
        return _needed(v["period"], v["lookback"], v["release_window"])  # type: ignore[arg-type]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        period, deviations = v["period"], v["deviations"]
        lookback, window = v["lookback"], v["release_window"]
        assert isinstance(period, int) and isinstance(deviations, Decimal)
        assert isinstance(lookback, int) and isinstance(window, int)
        need = _needed(period, lookback, window)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        closes = [bar.close for bar in bars[-need:]]
        width = [bandwidth(closes[j - period + 1:j + 1], period, deviations)
                 if j >= period - 1 else None for j in range(len(closes))]
        last = len(closes) - 1
        squeezed = False
        for j in range(last - window, last):
            span = width[j - lookback + 1:j + 1]
            if width[j] is not None and None not in span and width[j] <= min(span):  # type: ignore[type-var]
                squeezed = True
                break
        prior_upper = bollinger(closes[-period - 1:-1], period, deviations)[2]
        latest_upper = bollinger(closes[-period:], period, deviations)[2]
        triggered = squeezed and closes[-2] <= prior_upper and closes[-1] > latest_upper
        return finish(self.spec, context, bars[-1], state, triggered)
