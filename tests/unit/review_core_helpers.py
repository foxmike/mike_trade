"""Small builders shared by the decision-review tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import (
    ContextFreshness,
    MarketContext,
    QuoteSnapshot,
    TradeSnapshot,
)
from mike_trade.review.evidence import DecisionEvidence
from mike_trade.review.proposal import Contributor, PositionView
from mike_trade.scheduling.calendar import SessionStatus

AS_OF = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)  # 10:30 ET on a regular session


def simple_context(symbol: str = "AAA", *, as_of: datetime = AS_OF, bars: int = 35) -> MarketContext:
    candles = tuple(
        Candle(
            symbol, CandleTimeframe.M1, as_of - timedelta(minutes=bars - i),
            as_of - timedelta(minutes=bars - i - 1), D("100"), D("100.2"), D("99.8"),
            D("100") + D(i) / D(1000), D("1000"), "iex", False, CandleOrigin.REPLAY,
            PriceBasis.RAW, CandleStatus.CLOSED,
        )
        for i in range(bars)
    )
    return MarketContext(
        symbol=symbol, as_of=as_of, version=1, feed_id="iex", consolidated=False,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id="XNYS-2026-03-10",
        freshness=ContextFreshness(as_of, timedelta(0), timedelta(seconds=90), False, None),
        candles={CandleTimeframe.M1: candles},
        indicators={"vwap": D("100.1"), "rsi": None},
        latest_quote=QuoteSnapshot(symbol, D("100.30"), D("100.32"), D("5"), D("6"),
                                   as_of, as_of, "iex"),
        latest_trade=TradeSnapshot(symbol, D("100.31"), D("100"), as_of, as_of, "iex"),
    )


def position_view(entry: str = "99.5", quantity: str = "10") -> PositionView:
    return PositionView("AAA:1", D(quantity), D(entry), AS_OF - timedelta(minutes=20))


def contributor(
    signal: str = "s1", strategy: str = "ev_buy_a", *, action: str = "BUY", selected: bool = True
) -> Contributor:
    """A selected contributor carrying a real evidence document (evidence is mandatory)."""
    evidence = DecisionEvidence(
        strategy_id=strategy, strategy_version="1.0.0", action=action, symbol="AAA",
        timestamp=AS_OF, reason_code="SCRIPTED", reason="scripted", signal_id=signal,
        evidence={"note": "test evidence", "close": D("100.03")},
    )
    return Contributor(strategy, "1.0.0", signal, selected, evidence)
