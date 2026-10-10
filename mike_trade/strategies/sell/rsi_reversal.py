"""RSI overbought-reversal SELL strategy (Wilder RSI edge on closed bars).

Precise rule: on completed regular-session bars of the configured timeframe,
let ``rsi_prev`` be the prior completed-bar RSI and ``rsi`` the latest
completed-bar RSI, both read from the shared centrally computed Wilder RSI in
the ``MarketContext``/indicator service. A SELL trigger fires edge-triggered
when the prior bar was at or above the overbought threshold while the latest
bar has crossed back below it::

    rsi_prev >= overbought AND rsi < overbought  ->  SIGNAL

The boundary is inclusive on the prior leg (``>=``) and strict on the latest
leg (``<``): ``rsi_prev`` exactly equal to the threshold with ``rsi`` below
it signals, while ``rsi`` exactly equal to the threshold does not, and an
upward cross never signals.

Both RSI values are the warmed, fresh, completed-bar values supplied by the
``MarketContext``/indicator service; this strategy never computes RSI from
fetched data, never fetches data, and performs no I/O of any kind.

On a trigger the composition layer builds an ``ExitLongRecommendation`` for
100% of the confirmed remaining quantity of the owned managed long position
(see ``mike_trade.strategies.sell.base``); this module itself emits only the
pure ``StrategyEvaluation`` signal with a deterministic ID derived from the
completed-bar UTC timestamp and never emits a short, a partial quantity, a
confidence score, or an order.

This module states a widely known textbook reference definition only (Wilder
RSI); it makes no alpha, edge, or profitability claim. The strategy is a pure
function of an immutable ``MarketContext``, validated parameters, and
read-only persisted state: no broker, network, filesystem, order, sizing,
confidence-score, or secrets access (only the standard library and pure
domain/market-data value types are imported).

State convention (read from the ``state`` mapping supplied by the engine):

* ``"armed"`` and ``"last_signal_bar"`` are owned by this strategy (see
  ``persistent_state_keys``): ``armed`` is the rearm latch (True means the
  rule may fire) and ``last_signal_bar`` is the ISO start of the bar that
  fired (same-bar replay suppression). After a signal the rule is disarmed
  (``armed`` False) and stays disarmed until RSI first falls to or below the
  configured ``reset_threshold``; when no reset threshold is configured (the
  ``reset_threshold`` key is absent from ``params``), it stays disarmed until
  RSI has re-exceeded the overbought threshold (latest RSI >= overbought).
  Only then can it fire again. The rearming bar itself never fires. The engine
  (not this strategy) persists ``state_updates``; the strategy never writes to
  the filesystem or database. Re-evaluating the same completed bar never
  duplicates a signal because the signal ID is deterministic from the
  completed-bar UTC timestamp.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleStatus, CandleTimeframe
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ParameterValidationError,
    ReasonCode,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_RSI_PERIOD",
    "DEFAULT_OVERBOUGHT",
    "DEFAULT_RESET_THRESHOLD",
    "DEFAULT_TIMEFRAME_LABEL",
    "TIMEFRAME_CHOICES",
    "TIMEFRAME_MAP",
    "WARMUP_BARS",
    "STATE_ARMED",
    "STATE_LAST_SIGNAL_BAR",
    "RSI_LATEST_KEY",
    "RSI_PRIOR_KEY",
    "RsiReversalSell",
]

STRATEGY_ID = "rsi_reversal"
STRATEGY_VERSION = "1.0.0"

#: Default Wilder RSI period (completed bars).
DEFAULT_RSI_PERIOD = 14
#: Default overbought threshold (Decimal only, float is rejected).
DEFAULT_OVERBOUGHT = Decimal("70")
#: Default reset threshold used only when ``reset_threshold`` is supplied.
DEFAULT_RESET_THRESHOLD = Decimal("50")
#: Default timeframe label.
DEFAULT_TIMEFRAME_LABEL = "5m"

#: Supported completed-bar timeframe labels for the ``timeframe`` parameter.
TIMEFRAME_CHOICES = ("1m", "5m", "15m", "30m", "60m")

#: Label to completed-bar timeframe mapping.
TIMEFRAME_MAP: Mapping[str, CandleTimeframe] = {
    "1m": CandleTimeframe.M1,
    "5m": CandleTimeframe.M5,
    "15m": CandleTimeframe.M15,
    "30m": CandleTimeframe.M30,
    "60m": CandleTimeframe.M60,
}

#: ``spec.warmup_bars`` equals the default RSI period plus required history:
#: ``period + 1`` for Wilder warmup plus one extra bar for the prior/latest
#: pair. Larger configured periods are enforced dynamically in the trigger.
WARMUP_BARS = DEFAULT_RSI_PERIOD + 2

#: Strategy-owned persistent state keys (also declared in the spec).
STATE_ARMED = "armed"
STATE_LAST_SIGNAL_BAR = "last_signal_bar"

#: Indicator-service key holding the latest completed-bar Wilder RSI.
RSI_LATEST_KEY = "rsi"
#: Indicator-service key holding the prior completed-bar Wilder RSI.
RSI_PRIOR_KEY = "rsi_prev"


def _parameter_schema() -> ParameterSchema:
    return ParameterSchema(
        (
            ParameterSpec(
                "rsi_period",
                ParameterKind.INT,
                default=DEFAULT_RSI_PERIOD,
                min_value=2,
                max_value=50,
                description=(
                    "Wilder RSI period in completed bars "
                    f"(default {DEFAULT_RSI_PERIOD})."
                ),
            ),
            ParameterSpec(
                "overbought",
                ParameterKind.DECIMAL,
                default=DEFAULT_OVERBOUGHT,
                min_value=Decimal("50"),
                max_value=Decimal("100"),
                description=(
                    "Overbought threshold as a Decimal RSI reading strictly "
                    f"inside (50, 100) (default {DEFAULT_OVERBOUGHT}). "
                    "Decimal only, float is rejected."
                ),
            ),
            ParameterSpec(
                "reset_threshold",
                ParameterKind.DECIMAL,
                default=DEFAULT_RESET_THRESHOLD,
                min_value=Decimal("0"),
                max_value=Decimal("100"),
                description=(
                    "Optional Decimal RSI reset level that must be strictly "
                    f"below overbought (default {DEFAULT_RESET_THRESHOLD} when "
                    "supplied). Omit the key entirely to rearm only after RSI "
                    "has re-exceeded the overbought threshold. Decimal only, "
                    "float is rejected."
                ),
            ),
            ParameterSpec(
                "timeframe",
                ParameterKind.ENUM,
                default=DEFAULT_TIMEFRAME_LABEL,
                choices=TIMEFRAME_CHOICES,
                description=(
                    "Completed-bar timeframe label "
                    f"(default {DEFAULT_TIMEFRAME_LABEL})."
                ),
            ),
        )
    )


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="oscillator",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M5,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=CandleTimeframe.M5,
    parameter_schema=_parameter_schema(),
    persistent_state_keys=(STATE_ARMED, STATE_LAST_SIGNAL_BAR),
    methodology=(
        "Wilder RSI overbought-reversal reference: on completed "
        "regular-session bars, with `rsi_prev` the prior completed-bar RSI "
        "and `rsi` the latest completed-bar RSI (both the warmed, fresh, "
        "shared Wilder RSI supplied by the MarketContext/indicator service, "
        "never computed here), a SELL trigger fires edge-triggered when "
        "`rsi_prev >= overbought` while `rsi < overbought`. After a signal "
        "the rule is disarmed until RSI first falls to or below "
        "`reset_threshold`, or -- when no reset threshold is configured -- "
        "until RSI has re-exceeded the overbought threshold, and only then "
        "can fire again. This restates a textbook reference definition of "
        "Wilder RSI and makes no alpha, edge, or profitability claim; it is "
        "not a profitability claim and has not been backtested here."
    ),
    source=(
        "Wilder RSI reference definition (textbook reference: Wilder RSI on "
        "completed bars); not an alpha claim, not a profitability claim, and "
        "not backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission and no execution.",
        "Subject to minimum hold and exit arbitration; protective, manual, "
        "hard-risk, and forced-liquidation exits outrank strategy exits.",
        "Long exits only; never emits shorts or opposite-side actions.",
        "Full-remainder exits only; never emits partial-quantity recommendations.",
        "Regular session only; no overnight or pre/post-market handling.",
        "Closed bars only; developing, incomplete, stale, or missing bars "
        "invalidate the evaluation instead of signalling.",
        "Requires warmed shared Wilder RSI (`rsi` and `rsi_prev`); a missing "
        "or not-warmed RSI invalidates the evaluation.",
        "One signal per completed bar: replaying the same bar never emits "
        "twice (deterministic signal ID from the completed-bar UTC timestamp).",
        "Disarmed after each signal until the documented reset condition is "
        "met; disarmed evaluations never signal.",
        "The strategy never writes to the filesystem or database; the engine "
        "persists the declared `armed`/`last_signal_bar` state.",
    ),
    reason_codes=(
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ),
)


def _is_valid_closed_bar(candle: object, symbol: str, timeframe: CandleTimeframe) -> bool:
    """Return True when ``candle`` is a usable completed bar."""
    if not isinstance(candle, Candle):
        return False
    if candle.symbol != symbol or candle.timeframe is not timeframe:
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


def _is_valid_rsi(value: object) -> bool:
    """Return True when ``value`` is a usable warmed Wilder RSI reading."""
    return (
        isinstance(value, Decimal)
        and not isinstance(value, bool)
        and value.is_finite()
        and Decimal("0") <= value <= Decimal("100")
    )


class RsiReversalSell(SellRule):
    """Edge-triggered Wilder RSI overbought-reversal SELL on closed bars."""

    spec = _SPEC

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period = validated["rsi_period"]
        overbought = validated["overbought"]
        timeframe_label = validated["timeframe"]
        assert isinstance(period, int) and isinstance(overbought, Decimal)
        assert isinstance(timeframe_label, str)
        timeframe = TIMEFRAME_MAP[timeframe_label]

        if overbought <= Decimal("50") or overbought >= Decimal("100"):
            raise ParameterValidationError(
                f"parameter overbought={overbought!s} is outside (50, 100)",
                code="out_of_range",
                parameter="overbought",
            )
        reset: Decimal | None = None
        if isinstance(params, Mapping) and "reset_threshold" in params:
            candidate = validated["reset_threshold"]
            assert isinstance(candidate, Decimal)
            if candidate >= overbought:
                raise ParameterValidationError(
                    "parameter reset_threshold must be strictly below overbought",
                    code="out_of_range",
                    parameter="reset_threshold",
                )
            reset = candidate

        symbol = context.symbol

        if context.freshness.stale:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.STALE_DATA,)
            )
        if context.session_status is not SessionStatus.REGULAR:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,)
            )

        bars = context.candles.get(timeframe, ())
        if len(bars) < period + 2:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        if any(not _is_valid_closed_bar(bar, symbol, timeframe) for bar in bars):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        prior_bar = bars[-2]
        latest_bar = bars[-1]
        assert isinstance(prior_bar, Candle) and isinstance(latest_bar, Candle)
        if latest_bar.start != prior_bar.start + timeframe.duration:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        indicators = context.indicators
        rsi_latest = indicators.get(RSI_LATEST_KEY) if isinstance(indicators, Mapping) else None
        rsi_prior = indicators.get(RSI_PRIOR_KEY) if isinstance(indicators, Mapping) else None
        if not _is_valid_rsi(rsi_latest) or not _is_valid_rsi(rsi_prior):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        assert isinstance(rsi_latest, Decimal) and isinstance(rsi_prior, Decimal)

        armed: bool = True
        if isinstance(state, Mapping):
            raw_armed = state.get(STATE_ARMED)
            if isinstance(raw_armed, bool):
                armed = raw_armed
        bar_iso = latest_bar.start.isoformat()

        preserved: dict[str, object] = {}
        if isinstance(state, Mapping):
            previous_last = state.get(STATE_LAST_SIGNAL_BAR)
            if isinstance(previous_last, str) and previous_last:
                preserved[STATE_LAST_SIGNAL_BAR] = previous_last

        if isinstance(state, Mapping) and state.get(STATE_LAST_SIGNAL_BAR) == bar_iso:
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=latest_bar.start,
                state_updates={
                    STATE_ARMED: armed,
                    STATE_LAST_SIGNAL_BAR: bar_iso,
                },
            )

        if not armed:
            if reset is not None:
                if rsi_latest <= reset:
                    return StrategyEvaluation.no_signal(
                        self.spec,
                        symbol,
                        (ReasonCode.NO_SIGNAL,),
                        bar_time=latest_bar.start,
                        state_updates={STATE_ARMED: True, **preserved},
                    )
            elif rsi_latest >= overbought:
                return StrategyEvaluation.no_signal(
                    self.spec,
                    symbol,
                    (ReasonCode.NO_SIGNAL,),
                    bar_time=latest_bar.start,
                    state_updates={STATE_ARMED: True, **preserved},
                )
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=latest_bar.start,
                state_updates={STATE_ARMED: False, **preserved},
            )

        if rsi_prior >= overbought and rsi_latest < overbought:
            return StrategyEvaluation.for_signal(
                self.spec,
                symbol,
                latest_bar.start,
                reference_price=latest_bar.close,
                state_updates={
                    STATE_ARMED: False,
                    STATE_LAST_SIGNAL_BAR: bar_iso,
                },
            )
        return StrategyEvaluation.no_signal(
            self.spec,
            symbol,
            (ReasonCode.NO_SIGNAL,),
            bar_time=latest_bar.start,
            state_updates={STATE_ARMED: True, **preserved},
        )
