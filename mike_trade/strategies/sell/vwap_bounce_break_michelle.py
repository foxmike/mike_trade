"""Michelle VWAP fixed-to-trailing protective SELL policy."""

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterSchema,
    ReasonCode,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.sell._michelle_protective import MichelleProtectiveSell

STRATEGY_ID = "vwap_bounce_break_michelle_sell"
STRATEGY_VERSION = "1.0.0"
STATE_KEYS = (
    "position_cycle_id",
    "position_fill_price",
    "position_fill_time",
    "position_vwap_at_entry",
    "highest_trade_price",
    "active_stop_mode",
    "active_stop_price",
    "last_signal_event",
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="vwap",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=1,
    completed_bars_only=False,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_TRADE,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(()),
    persistent_state_keys=STATE_KEYS,
    methodology=(
        "Freeze fill-time VWAP V and protect a managed long initially at V-$0.25. "
        "Track highest traded price H and activate a monotonic 0.5% trail when "
        "0.995*H > V+$0.25."
    ),
    source="Michelle, VWAP_Bounce_Break_Formatted.docx, approved protective-stop clarification.",
    execution_limitations=(
        "Engine-owned protection precedes the advisory SELL and closes the full position.",
        "Fill-time VWAP is the latest causally available session VWAP captured with the fill.",
        "PAPER/LIVE require broker-safe stop replacement; unsupported adapters fail closed.",
    ),
    reason_codes=(
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ),
)


class VwapBounceBreakMichelleSell(MichelleProtectiveSell):
    spec = _SPEC
    reference_state_key = "position_vwap_at_entry"
