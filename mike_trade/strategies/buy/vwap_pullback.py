"""VWAP pullback BUY strategy during an established bullish intraday trend.

Definition: on completed regular-session 1-minute bars, a BUY signal fires
edge-triggered when an established bullish trend above session VWAP produces a
qualifying pullback to test session VWAP, followed by a bullish confirmation
rebound:

1. Trend: the prior session bar(s) preceding the pullback were trading strictly
   above VWAP (`low > vwap * (1 + tolerance_pct/100)` and `close >= vwap`).
2. Pullback (setup bar `t-1`): price pulls back to test session VWAP:
   `setup.low <= vwap_prev * (1 + tolerance_pct/100)` while holding VWAP support:
   `setup.close >= vwap_prev`.
3. Confirmation (signal bar `t`): latest bar confirms continuation:
   `latest.close > setup.high` and `latest.close > vwap_latest`.

Both VWAP values are read from the ``MarketContext`` indicator service under
the ``"vwap"`` (latest completed bar) and ``"vwap_prev"`` (prior completed
bar) keys.

Materially distinct from ``vwap_bounce``: ``vwap_bounce`` requires
`prior.close <= vwap_prev` (an upward crossing from below), whereas
``vwap_pullback`` requires an established trend above VWAP where setup
`close >= vwap_prev` (holding support) confirmed by closing above setup high.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleStatus, CandleTimeframe
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ReasonCode,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "WARMUP_BARS",
    "ELIGIBILITY_KEY",
    "ELIGIBILITY_WATCHING_BUY",
    "STATE_SESSION_ID",
    "STATE_LAST_SIGNAL_BAR",
    "VWAP_LATEST_KEY",
    "VWAP_PRIOR_KEY",
    "VwapPullbackBuy",
]

STRATEGY_ID = "vwap_pullback"
STRATEGY_VERSION = "1.0.0"

DEFAULT_TREND_BARS = 3
DEFAULT_TOLERANCE_PCT = Decimal("0.1")
WARMUP_BARS = DEFAULT_TREND_BARS + 2

ELIGIBILITY_KEY = "eligibility"
ELIGIBILITY_WATCHING_BUY = "WATCHING_BUY"

STATE_SESSION_ID = "session_id"
STATE_LAST_SIGNAL_BAR = "last_signal_bar"

VWAP_LATEST_KEY = "vwap"
VWAP_PRIOR_KEY = "vwap_prev"

_NEW_YORK = ZoneInfo("America/New_York")


def _required_bars(trend_bars: int) -> int:
    return trend_bars + 2


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="vwap",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "trend_bars",
                ParameterKind.INT,
                DEFAULT_TREND_BARS,
                1,
                20,
                description="Minimum prior completed session bars establishing an uptrend above VWAP. Default 3.",
            ),
            ParameterSpec(
                "tolerance_pct",
                ParameterKind.DECIMAL,
                DEFAULT_TOLERANCE_PCT,
                Decimal("0.0"),
                Decimal("2.0"),
                description="Maximum distance between setup bar's low and VWAP as a percentage of VWAP. Default 0.1%.",
            ),
        )
    ),
    persistent_state_keys=(STATE_SESSION_ID, STATE_LAST_SIGNAL_BAR),
    methodology=(
        "BUY when an established intraday bullish trend above session VWAP produces a "
        "qualifying pullback to test session VWAP (setup bar low <= vwap_prev * (1 + tolerance_pct/100) "
        "while close >= vwap_prev), confirmed on the next completed bar by closing above the setup "
        "bar's high (close > setup.high and close > vwap_latest)."
    ),
    source=(
        "Brian Shannon, Technical Analysis Using Multiple Timeframes (2008), Chapter on VWAP "
        "pullback entries; Andrew Aziz, How to Day Trade for a Living (2016), Chapter 7: 'VWAP Trading' "
        "(note: Brian Pezim authored 'How to Swing Trade' (2018)). Indicator definition uses standard "
        "cumulative session volume-weighted average price. General concept is buying an intraday pullback "
        "that tests and holds VWAP support in an uptrend; specific deterministic engineering choices "
        "include parameterizing trend establishment via trend_bars and touch proximity via tolerance_pct."
    ),
    execution_limitations=(
        "Entry signal only: no stops, sizing, targets, or orders.",
        "Long entries only; regular session only; completed 1-minute bars with positive volume.",
        "Requires warmed per-bar session VWAP (`vwap` and `vwap_prev`); missing or invalid VWAP yields INSUFFICIENT_DATA.",
        "Bars and VWAP windows never span sessions; VWAP resets each session.",
        "Materially distinct from vwap_bounce: vwap_bounce requires prior close <= vwap_prev, "
        "whereas vwap_pullback requires setup close >= vwap_prev (holding support) confirmed by closing above setup high.",
        "One signal per completed bar: replaying the same bar never emits twice.",
    ),
    reason_codes=(
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ),
)


def _is_valid_closed_bar(candle: object, symbol: str) -> bool:
    if not isinstance(candle, Candle):
        return False
    if candle.symbol != symbol or candle.timeframe is not CandleTimeframe.M1:
        return False
    if candle.status is not CandleStatus.CLOSED:
        return False
    for price in (candle.open, candle.high, candle.low, candle.close):
        if (
            isinstance(price, bool)
            or not isinstance(price, Decimal)
            or not price.is_finite()
            or price <= 0
        ):
            return False
    if isinstance(candle.volume, bool) or not isinstance(candle.volume, Decimal):
        return False
    if not candle.volume.is_finite() or candle.volume < 0:
        return False
    if candle.high < candle.low:
        return False
    if not candle.low <= candle.open <= candle.high:
        return False
    return candle.low <= candle.close <= candle.high


def _is_valid_vwap(value: object) -> bool:
    return (
        isinstance(value, Decimal)
        and not isinstance(value, bool)
        and value.is_finite()
        and value > 0
    )


class VwapPullbackBuy(BuyStrategy):
    """BUY on a confirmed pullback to session VWAP during an established intraday trend."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        trend_bars = cls.spec.parameter_schema.validate(params)["trend_bars"]
        assert isinstance(trend_bars, int)
        return _required_bars(trend_bars)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        trend_bars = values["trend_bars"]
        tolerance_pct = values["tolerance_pct"]
        assert isinstance(trend_bars, int) and isinstance(tolerance_pct, Decimal)

        symbol = context.symbol

        if context.freshness.stale:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.STALE_DATA,)
            )
        if context.session_status is not SessionStatus.REGULAR:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,)
            )
        if not isinstance(state, Mapping) or state.get(ELIGIBILITY_KEY) != (
            ELIGIBILITY_WATCHING_BUY
        ):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,)
            )

        need_bars = _required_bars(trend_bars)
        bars = context.candles.get(CandleTimeframe.M1, ())
        if len(bars) < need_bars:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        if any(not _is_valid_closed_bar(bar, symbol) for bar in bars):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        session_date = bars[-1].start.astimezone(_NEW_YORK).date()
        session_bars = [
            bar
            for bar in bars
            if bar.start.astimezone(_NEW_YORK).date() == session_date
        ]
        if len(session_bars) < need_bars:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        eval_window = session_bars[-need_bars:]
        if any(
            b.start != a.start + timedelta(minutes=1)
            for a, b in zip(eval_window, eval_window[1:])
        ):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        if any(bar.volume <= 0 for bar in eval_window):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        indicators = context.indicators
        vwap_latest = (
            indicators.get(VWAP_LATEST_KEY) if isinstance(indicators, Mapping) else None
        )
        vwap_prior = (
            indicators.get(VWAP_PRIOR_KEY) if isinstance(indicators, Mapping) else None
        )
        if not _is_valid_vwap(vwap_latest) or not _is_valid_vwap(vwap_prior):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        assert isinstance(vwap_latest, Decimal) and isinstance(vwap_prior, Decimal)

        session_key = context.session_id or session_date.isoformat()
        base_updates: dict[str, object] = {STATE_SESSION_ID: session_key}

        latest = session_bars[-1]
        setup = session_bars[-2]
        pre_setup_bars = session_bars[-need_bars:-2]

        if state.get(STATE_LAST_SIGNAL_BAR) == latest.start.isoformat():
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=latest.start,
                state_updates={
                    **base_updates,
                    STATE_LAST_SIGNAL_BAR: latest.start.isoformat(),
                },
            )

        # 1. Prior trend: pre-setup bars traded strictly above VWAP
        threshold = vwap_prior * (Decimal(1) + tolerance_pct / Decimal(100))
        trend_ok = (
            pre_setup_bars[-1].low > threshold
            and all(bar.close >= vwap_prior for bar in pre_setup_bars)
        )

        # 2. Pullback test of VWAP: setup bar touched or entered tolerance zone, but close held >= VWAP
        pullback_ok = setup.low <= threshold and setup.close >= vwap_prior

        # 3. Confirmation: latest bar closed above setup high and above latest VWAP
        confirmation_ok = latest.close > setup.high and latest.close > vwap_latest

        if trend_ok and pullback_ok and confirmation_ok:
            return StrategyEvaluation.for_signal(
                self.spec,
                symbol,
                latest.start,
                reference_price=latest.close,
                state_updates={
                    **base_updates,
                    STATE_LAST_SIGNAL_BAR: latest.start.isoformat(),
                },
            )

        return StrategyEvaluation.no_signal(
            self.spec,
            symbol,
            (ReasonCode.NO_SIGNAL,),
            bar_time=latest.start,
            state_updates=base_updates,
        )
