"""Break above VWAP anchored at the first causally confirmed session swing low."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
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
from mike_trade.strategies.buy._session import current_session_bars, latest_session_bars
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.vwap_bands import (
    VWAPDeviationBandsCalculator,
    session_key,
)
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "anchored_vwap_breakout"
STATE_ANCHOR = "anchor_state"


class AnchoredVwapBreakoutBuy(BuyStrategy):
    """The first confirmed low fixes the anchor; only later closes may break AVWAP."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.BUY,
        category="technical",
        sub_category="vwap",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=6,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "strength",
                    ParameterKind.INT,
                    2,
                    1,
                    10,
                    description="Bars on each side confirming the first session swing low.",
                ),
            )
        ),
        persistent_state_keys=(STATE_ANCHOR, STATE_LAST_SIGNAL_BAR),
        methodology=(
            "Anchor cumulative typical-price VWAP to the first confirmed swing low of the "
            "regular session. After confirmation, BUY only when a completed close moves "
            "from at/below its preceding anchored VWAP to strictly above the current one."
        ),
        source=(
            "StockCharts ChartSchool, Anchored VWAP (a significant low is a valid event anchor; "
            "VWAP is volume-weighted typical price from that bar): "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-"
            "overlays/technical-overlays/anchored-vwap. First confirmed fractal low and "
            "close-cross entry are engineering choices, not published thresholds."
        ),
        execution_limitations=(
            "The anchor is unknown until the right-hand strength bars close; no trade occurs "
            "on the pivot or confirmation bar.",
            "On confirmation VWAP is computed retroactively from the pivot bar through the "
            "confirmation bar (an explicit convention: the pivot is only known then); entries "
            "are considered from the following bar and never on a bar before confirmation.",
            "Pivot neighbours must be regular-session bars; pre-market bars never confirm a pivot.",
            "The earliest confirmed session low is retained; a later lower low does not re-anchor.",
            "Missing bars or a restart without a valid snapshot and full session prefix "
            "fail closed.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        strength = cls.spec.parameter_schema.validate(params)["strength"]
        assert isinstance(strength, int)
        return 2 * strength + 2

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        strength = self.spec.parameter_schema.validate(params)["strength"]
        assert isinstance(strength, int)
        reason = gate(self.spec, context, state)
        found = latest_session_bars(context, 1)
        if found is None:
            return insufficient(self.spec, context)
        session_open = found[0].open_utc
        latest = found[1][-1]
        session = session_key(latest)
        supplied = context.candles.get(CandleTimeframe.M1, ())
        saved = state.get(STATE_ANCHOR) if isinstance(state, Mapping) else None
        valid = (
            isinstance(saved, Mapping)
            and saved.get("version") == 1
            and saved.get("session") == session
            and saved.get("strength") == strength
        )
        if valid:
            try:
                last_end = datetime.fromisoformat(str(saved["last_end"]))
                anchor_time = saved["anchor_time"]
                previous_close = Decimal(str(saved["last_close"]))
                previous_vwap = (
                    Decimal(str(saved["last_vwap"])) if saved.get("last_vwap") is not None else None
                )
                calc = (
                    VWAPDeviationBandsCalculator.restore(saved["calc"])
                    if isinstance(anchor_time, str)
                    else None
                )
                if (
                    not session_open < last_end <= latest.end
                    or not previous_close.is_finite()
                    or previous_close <= 0
                    or (
                        anchor_time is not None
                        and (
                            not isinstance(anchor_time, str)
                            or calc is None
                            or calc.snapshot().get("session_id") != f"{session}|{anchor_time}"
                            or previous_vwap is None
                            or previous_vwap <= 0
                            or not previous_vwap.is_finite()
                        )
                    )
                ):
                    valid = False
            except (KeyError, TypeError, ValueError, InvalidOperation):
                valid = False
        if valid:
            assert isinstance(saved, Mapping)
            if last_end == latest.end:
                if reason is not None:
                    return blocked(self.spec, context, reason)
                return finish(self.spec, context, latest, state, False)
            pending = tuple(bar for bar in supplied if last_end <= bar.start <= latest.start)
            if not pending or pending[0].start != last_end:
                return insufficient(self.spec, context)
            if any(a.end != b.start for a, b in zip(pending, pending[1:], strict=False)):
                return insufficient(self.spec, context)
        else:
            complete = current_session_bars(context)
            if complete is None:
                return insufficient(self.spec, context)
            pending = complete[1]
            anchor_time = None
            previous_close = None
            previous_vwap = None
            calc = None

        triggered = False
        for bar in pending:
            if bar.volume <= 0 or session_key(bar) != session:
                return insufficient(self.spec, context)
            if calc is None:
                # Regular-session bars only: pre-market neighbours must not confirm a pivot.
                prefix = tuple(
                    item for item in supplied if session_open <= item.start and item.end <= bar.end
                )
                group = prefix[-(2 * strength + 1) :]
                if len(group) == 2 * strength + 1:
                    _, lows = swing_pivots(group, strength)
                    if lows:
                        center = group[strength]
                        anchor_time = center.start.isoformat()
                        calc = VWAPDeviationBandsCalculator(Decimal(0))
                        for source_bar in group[strength:]:
                            result = calc.update(source_bar, f"{session}|{anchor_time}")
                        previous_vwap = result.vwap if result else None
                previous_close = bar.close
                continue  # confirmation bar is not an entry bar
            prior_vwap = previous_vwap
            result = calc.update(bar, f"{session}|{anchor_time}")
            current_vwap = result.vwap if result else None
            if (
                bar is latest
                and previous_close is not None
                and prior_vwap is not None
                and current_vwap is not None
                and previous_close <= prior_vwap
                and bar.close > current_vwap
            ):
                triggered = True
            previous_close, previous_vwap = bar.close, current_vwap

        updates = {
            STATE_ANCHOR: {
                "version": 1,
                "session": session,
                "strength": strength,
                "last_end": latest.end.isoformat(),
                "anchor_time": anchor_time,
                "last_close": str(previous_close),
                "last_vwap": str(previous_vwap) if previous_vwap is not None else None,
                "calc": calc.snapshot() if calc is not None else None,
            }
        }
        if reason is not None:
            return blocked(self.spec, context, reason, updates)
        return finish(self.spec, context, latest, state, triggered, updates)
