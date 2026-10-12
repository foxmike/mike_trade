"""Michelle ORB fixed-to-trailing protective SELL policy."""

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterSchema,
    ReasonCode,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.sell._michelle_protective import MichelleProtectiveSell

STRATEGY_ID = "five_minute_orb_michelle_sell"
STRATEGY_VERSION = "1.0.0"
STATE_KEYS = (
    "position_cycle_id",
    "position_fill_price",
    "position_fill_time",
    "position_orb_high",
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
    sub_category="breakout",
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
        "Protect a managed long initially at ORB_High-$0.25. Track the highest traded "
        "price H and activate a monotonic 0.5% trail when 0.995*H > ORB_High+$0.25."
    ),
    source="Michelle, Five_Minute_ORB_Formatted.docx, approved protective-stop clarification.",
    execution_limitations=(
        "Engine-owned protection precedes the advisory SELL and closes the full position.",
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


class FiveMinuteOrbMichelleSell(MichelleProtectiveSell):
    spec = _SPEC
    reference_state_key = "position_orb_high"
