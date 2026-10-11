"""Bullish same-bar rejection of session VWAP support."""

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
from mike_trade.strategies.buy._session import current_session_bars
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.vwap_bands import session_vwap_series, vwap_matches

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "vwap_rejection"


class VwapRejectionBuy(BuyStrategy):
    """Buy a completed bullish candle that pierces VWAP and closes back above it."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.BUY,
        category="technical",
        sub_category="vwap",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=3,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "max_penetration_pct",
                    ParameterKind.DECIMAL,
                    Decimal("0.5"),
                    Decimal("0.1"),
                    Decimal("5"),
                    description="Maximum intrabar penetration below VWAP, percent. Default 0.5.",
                ),
            )
        ),
        persistent_state_keys=("session_id", STATE_LAST_SIGNAL_BAR),
        methodology=(
            "BUY when the prior bar traded wholly above its own session VWAP and the "
            "latest bullish bar pierces its current VWAP by no more than the configured "
            "percentage, then closes strictly back above it."
        ),
        source=(
            "Charles Schwab, How to Use Volume-Weighted Indicators in Trading "
            "(VWAP as support; a dip below followed by recovery can indicate buying): "
            "https://www.schwab.com/learn/story/how-to-use-volume-weighted-indicators-trading; "
            "StockCharts ChartSchool, Candlesticks and Support: "
            "https://chartschool.stockcharts.com/table-of-contents/chart-analysis/"
            "candlestick-charts/candlesticks-and-support. Same-bar bullish close, first-touch "
            "edge and penetration bound are deterministic engineering choices, "
            "not published thresholds."
        ),
        execution_limitations=(
            "Requires complete current-session M1 history and shared VWAP values "
            "matching recomputation.",
            "A bullish close is required; later-bar confirmation is not part of this rule.",
            "Entry only; execution and risk remain with the engine.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        cls.spec.parameter_schema.validate(params)
        return 3

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        penetration = self.spec.parameter_schema.validate(params)["max_penetration_pct"]
        assert isinstance(penetration, Decimal)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        found = current_session_bars(context)
        if found is None or len(found[1]) < 3:
            return insufficient(self.spec, context)
        session, bars = found
        if any(bar.volume <= 0 for bar in bars):
            return insufficient(self.spec, context)
        series = session_vwap_series(bars)
        prior_vwap, latest_vwap = series[-2:]
        if not (
            vwap_matches(prior_vwap, context.indicators.get("vwap_prev"))
            and vwap_matches(latest_vwap, context.indicators.get("vwap"))
        ):
            return insufficient(self.spec, context)
        assert prior_vwap is not None and latest_vwap is not None
        prior, latest = bars[-2:]
        qualifies = (
            prior.low > prior_vwap
            and latest.low < latest_vwap
            and latest.low >= latest_vwap * (Decimal(1) - penetration / Decimal(100))
            and latest.close > latest_vwap
            and latest.close > latest.open
        )
        return finish(
            self.spec,
            context,
            latest,
            state,
            qualifies,
            {"session_id": session.session_date.isoformat()},
        )
