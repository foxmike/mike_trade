"""Shared, deterministic Market Status Monitor.

Classifies overall market conditions into canonical risk states (NORMAL, WARNING,
CRASH, STABILIZING, RECOVERY) using hysteresis-protected state transitions,
deterministic Decimal metrics across reference symbols, and fail-closed missing-data
handling while guaranteeing unconditional exit authorization.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_status.config import MarketStatusConfig
from mike_trade.market_status.metrics import (
    MarketMetricsSnapshot,
    calculate_market_metrics,
)
from mike_trade.market_status.policy import (
    TradingAuthorizationDecision,
    TradingAuthorizationPolicy,
)
from mike_trade.market_status.state import (
    DIRECT_TRANSITIONS,
    PROGRESSIVE_TRANSITIONS,
    VALID_TRANSITIONS,
    MarketState,
    MarketStateSnapshot,
    StateTransitionRecord,
)

__all__ = ["MarketStatusMonitor"]

_ZERO = Decimal("0")
_ONE = Decimal("1")


class MarketStatusMonitor:
    """Deterministic, stateful market risk monitor and authorization provider."""

    def __init__(
        self,
        config: MarketStatusConfig | None = None,
        policy: TradingAuthorizationPolicy | None = None,
        initial_state: MarketState = MarketState.NORMAL,
        initial_time: datetime | None = None,
        clock: Any = None,
    ) -> None:
        self.config = config or MarketStatusConfig()
        self.policy = policy or TradingAuthorizationPolicy()
        self.clock = clock

        self._current_state = initial_state
        self._previous_state: MarketState | None = None
        now = initial_time or (clock.now() if clock and hasattr(clock, "now") else datetime.now(UTC))
        now = now.astimezone(UTC)
        self._state_since = now
        self._bars_in_state = 0

        # Reference symbol candle buffers: symbol -> deque of Candle
        self._candles: dict[str, deque[Candle]] = {
            s: deque(maxlen=self.config.windows.peak_lookback_bars + 10)
            for s in self.config.symbols.symbols
        }

        # Optional external VIX tracking
        self._vix_level: Decimal | None = None
        self._vix_timestamp: datetime | None = None

        # Episode tracking for stabilization and recovery
        self._trough_drawdown: Decimal = _ZERO
        self._bars_since_trough: int = 0
        self._peak_volatility: Decimal = _ZERO

        # Hysteresis confirmation counters
        self._pending_state: MarketState | None = None
        self._pending_bars: int = 0

        # Transition history
        self._transitions: list[StateTransitionRecord] = []
        self._last_evaluation_time: datetime = now

    @property
    def current_state(self) -> MarketState:
        return self._current_state

    @property
    def state_since(self) -> datetime:
        return self._state_since

    @property
    def bars_in_state(self) -> int:
        return self._bars_in_state

    @property
    def previous_state(self) -> MarketState | None:
        return self._previous_state

    @property
    def transitions(self) -> tuple[StateTransitionRecord, ...]:
        return tuple(self._transitions)

    @property
    def reference_candles(self) -> Mapping[str, tuple[Candle, ...]]:
        return {s: tuple(q) for s, q in self._candles.items()}

    def add_bar(self, bar: Any) -> None:
        """Ingest one completed reference candle."""
        if not hasattr(bar, "symbol") or not hasattr(bar, "close") or not hasattr(bar, "start"):
            raise TypeError("bar must be a Candle instance")
        if not isinstance(bar, Candle):
            bar = Candle(
                symbol=bar.symbol,
                timeframe=CandleTimeframe(getattr(getattr(bar, "timeframe", None), "value", "1m")),
                start=bar.start,
                end=getattr(bar, "end", bar.start + timedelta(minutes=1)),
                open=Decimal(str(bar.open)),
                high=Decimal(str(bar.high)),
                low=Decimal(str(bar.low)),
                close=Decimal(str(bar.close)),
                volume=Decimal(str(bar.volume)),
                feed_id=getattr(getattr(bar, "provenance", None), "feed", "IEX"),
                consolidated=getattr(getattr(bar, "provenance", None), "is_consolidated", False),
                origin=CandleOrigin.REPLAY,
                price_basis=PriceBasis.RAW,
                status=CandleStatus.CLOSED,
                trade_count=getattr(bar, "trade_count", None),
            )
        symbol = bar.symbol.strip().upper()
        if symbol in self._candles:
            buffer = self._candles[symbol]
            # Maintain monotonic chronological ordering and idempotence
            if not buffer or bar.start > buffer[-1].start:
                buffer.append(bar)
            elif bar.start == buffer[-1].start:
                # Replace with latest snapshot for same instant
                buffer[-1] = bar

    def add_bars(self, bars: Sequence[Candle]) -> None:
        """Ingest multiple completed candles in chronological sequence."""
        for bar in bars:
            self.add_bar(bar)

    def set_vix(self, timestamp: datetime, vix_value: Decimal) -> None:
        """Optionally record a VIX reading."""
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        if not isinstance(vix_value, Decimal) or not vix_value.is_finite() or vix_value <= _ZERO:
            raise ValueError("vix_value must be a positive finite Decimal")
        self._vix_level = vix_value
        self._vix_timestamp = timestamp.astimezone(UTC)

    def evaluate(self, as_of: datetime | None = None) -> MarketStateSnapshot:
        """Evaluate market conditions and determine current state and authorization."""
        now_fallback = (
            self.clock.now() if self.clock and hasattr(self.clock, "now") else datetime.now(UTC)
        )
        eval_time = (as_of or self._latest_bar_time() or now_fallback).astimezone(UTC)
        self._last_evaluation_time = eval_time

        # Point-in-time candle view: never look ahead past eval_time
        candles_as_of = {
            s: deque((b for b in buf if b.end <= eval_time), maxlen=buf.maxlen)
            for s, buf in self._candles.items()
        }

        # Check reference data presence and freshness
        data_available, is_stale, staleness_reasons = self._check_data_quality(
            eval_time, candles=candles_as_of
        )

        if not data_available or is_stale:
            # Conservative fail-closed authorization: entries blocked, exits preserved!
            metrics = MarketMetricsSnapshot(
                as_of=eval_time,
                composite_drawdown_percent=_ZERO,
                worst_drawdown_percent=_ZERO,
                realized_volatility_bps=_ZERO,
                momentum_bps=_ZERO,
                breadth_weakness_ratio=_ZERO,
                drawdown_by_symbol={},
                volatility_by_symbol={},
                momentum_by_symbol={},
                vix_level=self._vix_level,
            )
            auth = self.policy.evaluate(
                self._current_state,
                eval_time,
                data_available=data_available,
                is_stale=is_stale,
            )
            return MarketStateSnapshot(
                state=self._current_state,
                as_of=eval_time,
                state_since=self._state_since,
                bars_in_state=self._bars_in_state,
                previous_state=self._previous_state,
                data_available=data_available,
                is_stale=is_stale,
                reasons=staleness_reasons,
                metrics=metrics,
                authorization=auth,
                last_transition=self._transitions[-1] if self._transitions else None,
            )

        prior_trough = self._trough_drawdown

        # Compute deterministic market metrics point-in-time
        metrics = calculate_market_metrics(
            candles_as_of,
            eval_time,
            self.config,
            prior_trough_drawdown=self._trough_drawdown,
            prior_bars_since_trough=self._bars_since_trough,
            prior_peak_volatility_bps=self._peak_volatility,
            vix_level=self._vix_level,
        )

        # Evaluate state machine transitions using metrics and prior state
        self._process_transitions(metrics, eval_time, prior_trough)

        # Update episode tracking
        self._trough_drawdown = metrics.trough_drawdown_percent
        self._bars_since_trough = metrics.bars_since_trough
        self._peak_volatility = metrics.peak_volatility_bps

        # Produce typed authorization decision
        auth = self.policy.evaluate(
            self._current_state,
            eval_time,
            data_available=True,
            is_stale=False,
        )

        self._bars_in_state += 1

        return MarketStateSnapshot(
            state=self._current_state,
            as_of=eval_time,
            state_since=self._state_since,
            bars_in_state=self._bars_in_state,
            previous_state=self._previous_state,
            data_available=True,
            is_stale=False,
            reasons=(),
            metrics=metrics,
            authorization=auth,
            last_transition=self._transitions[-1] if self._transitions else None,
        )

    def _latest_bar_time(self) -> datetime | None:
        """Find the latest bar timestamp across all reference buffers."""
        latest: datetime | None = None
        for buf in self._candles.values():
            if buf:
                bar_end = buf[-1].end
                if latest is None or bar_end > latest:
                    latest = bar_end
        return latest

    def _check_data_quality(
        self,
        eval_time: datetime,
        candles: Mapping[str, Sequence[Candle]] | None = None,
    ) -> tuple[bool, bool, tuple[str, ...]]:
        """Verify minimum reference symbols present and not stale."""
        target_candles = candles if candles is not None else self._candles
        reasons: list[str] = []
        symbols_with_data = [s for s, b in target_candles.items() if len(b) > 0]

        if len(symbols_with_data) < self.config.symbols.min_required_symbols:
            reasons.append(
                f"INSUFFICIENT_REFERENCE_SYMBOLS_HAVE_{len(symbols_with_data)}_"
                f"NEED_{self.config.symbols.min_required_symbols}"
            )
            return False, False, tuple(reasons)

        max_age = timedelta(seconds=self.config.windows.max_observation_age_seconds)
        stale_symbols: list[str] = []
        for sym in symbols_with_data:
            latest_bar = target_candles[sym][-1]
            if eval_time - latest_bar.end > max_age:
                stale_symbols.append(sym)

        if len(symbols_with_data) - len(stale_symbols) < self.config.symbols.min_required_symbols:
            reasons.append(f"STALE_REFERENCE_DATA_{','.join(stale_symbols)}")
            return True, True, tuple(reasons)

        return True, False, ()

    def _transition_to(self, new_state: MarketState, timestamp: datetime, reason: str, metrics: MarketMetricsSnapshot) -> None:
        """Execute a validated state transition with audit recording."""
        if (self._current_state, new_state) not in VALID_TRANSITIONS:
            raise ValueError(f"Illegal transition: {self._current_state} -> {new_state}")

        record = StateTransitionRecord(
            from_state=self._current_state,
            to_state=new_state,
            timestamp=timestamp,
            reason=reason,
            metrics_summary={
                "composite_drawdown": str(metrics.composite_drawdown_percent),
                "worst_drawdown": str(metrics.worst_drawdown_percent),
                "realized_volatility_bps": str(metrics.realized_volatility_bps),
                "momentum_bps": str(metrics.momentum_bps),
                "breadth_weakness_ratio": str(metrics.breadth_weakness_ratio),
                "trough_drawdown": str(metrics.trough_drawdown_percent),
                "volatility_decay_ratio": str(metrics.volatility_decay_ratio),
            },
        )
        self._transitions.append(record)
        self._previous_state = self._current_state
        self._current_state = new_state
        self._state_since = timestamp
        self._bars_in_state = 0
        self._pending_state = None
        self._pending_bars = 0

        # Reset episode trackers on fresh CRASH
        if new_state is MarketState.CRASH:
            self._trough_drawdown = metrics.composite_drawdown_percent
            self._bars_since_trough = 0
            self._peak_volatility = metrics.realized_volatility_bps
        elif new_state is MarketState.NORMAL:
            self._trough_drawdown = _ZERO
            self._bars_since_trough = 0
            self._peak_volatility = _ZERO

    def _process_transitions(
        self,
        metrics: MarketMetricsSnapshot,
        timestamp: datetime,
        prior_trough: Decimal,
    ) -> None:
        """Evaluate transition conditions for current state with hysteresis."""
        current = self._current_state
        dd = metrics.composite_drawdown_percent
        worst_dd = metrics.worst_drawdown_percent
        vol = metrics.realized_volatility_bps
        breadth = metrics.breadth_weakness_ratio
        mom = metrics.momentum_bps

        cfg_dd = self.config.drawdowns
        cfg_vol = self.config.volatility
        cfg_br = self.config.breadth
        cfg_hys = self.config.hysteresis

        match current:
            case MarketState.NORMAL:
                # Acute Flash Crash check (direct transition)
                if worst_dd <= cfg_dd.flash_crash_percent or (dd <= cfg_dd.crash_percent and breadth >= cfg_br.crash_breadth_ratio):
                    self._transition_to(MarketState.CRASH, timestamp, "FLASH_CRASH_ACUTE_DRAWDOWN", metrics)
                    return

                # Warning conditions: requires significant drawdown or elevated volatility with negative drift
                warning_met = (
                    dd <= cfg_dd.warning_percent
                    or (
                        dd <= cfg_dd.warning_cleared_percent
                        and vol >= cfg_vol.warning_bps
                        and mom < _ZERO
                        and breadth >= cfg_br.warning_breadth_ratio
                    )
                )
                if warning_met:
                    self._advance_pending(
                        MarketState.WARNING,
                        cfg_hys.warning_confirmation_bars,
                        timestamp,
                        "PERSISTENT_ELEVATED_DRAWDOWN_AND_VOLATILITY",
                        metrics,
                    )
                else:
                    self._reset_pending()

            case MarketState.WARNING:
                # Crash condition (accelerating deterioration)
                crash_met = (
                    dd <= cfg_dd.crash_percent
                    or worst_dd <= cfg_dd.flash_crash_percent
                    or (vol >= cfg_vol.crash_bps and dd <= cfg_dd.warning_percent and breadth >= cfg_br.crash_breadth_ratio)
                )
                if crash_met:
                    # Flash crash bypasses confirmation delay
                    required_bars = 1 if worst_dd <= cfg_dd.flash_crash_percent else cfg_hys.crash_confirmation_bars
                    self._advance_pending(
                        MarketState.CRASH,
                        required_bars,
                        timestamp,
                        "CRASH_DRAWDOWN_AND_VOLATILITY_CONFIRMED",
                        metrics,
                    )
                    return

                # Cleared back to NORMAL (false alarm recovery)
                # Hysteresis requirement: drawdown must recover above warning_cleared_percent
                cleared_met = (
                    dd >= cfg_dd.warning_cleared_percent
                    and vol < cfg_vol.warning_bps
                    and mom >= _ZERO
                )
                if cleared_met:
                    self._advance_pending(
                        MarketState.NORMAL,
                        cfg_hys.warning_cleared_confirmation_bars,
                        timestamp,
                        "WARNING_CONDITIONS_CLEARED_NORMAL_RESTORED",
                        metrics,
                    )
                else:
                    self._reset_pending()

            case MarketState.CRASH:
                # Can only begin stabilizing after minimum observation bars in crash
                if self._bars_in_state < cfg_hys.stabilizing_min_bars_in_crash:
                    return

                stabilizing_met = (
                    metrics.bars_since_trough >= cfg_hys.stabilizing_no_new_lows_bars
                    and metrics.rebound_from_trough_percent >= cfg_dd.stabilizing_trough_rebound_percent
                )
                if stabilizing_met:
                    self._advance_pending(
                        MarketState.STABILIZING,
                        cfg_hys.stabilizing_confirmation_bars,
                        timestamp,
                        "CRASH_TROUGH_ESTABLISHED_VOLATILITY_CONTRACTING",
                        metrics,
                    )
                else:
                    self._reset_pending()

            case MarketState.STABILIZING:
                # Direct relapse back to CRASH if new low breached or stabilization rebound broken
                relapse_met = (
                    (metrics.bars_since_trough == 0 and metrics.trough_drawdown_percent < prior_trough)
                    or (metrics.rebound_from_trough_percent <= _ZERO)
                )
                if relapse_met:
                    self._advance_pending(
                        MarketState.CRASH,
                        cfg_hys.relapse_to_crash_bars,
                        timestamp,
                        "SECONDARY_BREAKDOWN_RELAPSE_TO_CRASH",
                        metrics,
                    )
                    return

                # Progress to RECOVERY
                if self._bars_in_state < cfg_hys.recovery_min_bars_in_stabilizing:
                    return

                recovery_met = (
                    dd >= cfg_dd.recovery_drawdown_percent
                    and mom >= _ZERO
                    and breadth <= (_ONE - cfg_br.recovery_breadth_ratio)
                )
                if recovery_met:
                    self._advance_pending(
                        MarketState.RECOVERY,
                        cfg_hys.recovery_confirmation_bars,
                        timestamp,
                        "RECOVERY_TREND_RECLAIM_CONFIRMED",
                        metrics,
                    )
                else:
                    self._reset_pending()

            case MarketState.RECOVERY:
                # Direct relapse into CRASH
                if dd <= cfg_dd.crash_percent or worst_dd <= cfg_dd.flash_crash_percent:
                    self._transition_to(
                        MarketState.CRASH,
                        timestamp,
                        "RECOVERY_ABORTED_CRASH_RELAPSE",
                        metrics,
                    )
                    return

                # Relapse into WARNING
                if dd <= cfg_dd.warning_percent:
                    self._transition_to(
                        MarketState.WARNING,
                        timestamp,
                        "RECOVERY_STALLED_ELEVATED_RISK_WARNING",
                        metrics,
                    )
                    return

                # Normalization back to NORMAL
                normal_met = (
                    dd >= cfg_dd.normal_drawdown_percent
                    and vol < cfg_vol.warning_bps
                    and mom >= _ZERO
                )
                if normal_met:
                    self._advance_pending(
                        MarketState.NORMAL,
                        cfg_hys.normal_confirmation_bars,
                        timestamp,
                        "FULL_MARKET_NORMALIZATION_CONFIRMED",
                        metrics,
                    )
                else:
                    self._reset_pending()

    def _advance_pending(
        self,
        candidate_state: MarketState,
        required_bars: int,
        timestamp: datetime,
        reason: str,
        metrics: MarketMetricsSnapshot,
    ) -> None:
        """Advance hysteresis confirmation counter; transition if threshold met."""
        if self._pending_state == candidate_state:
            self._pending_bars += 1
        else:
            self._pending_state = candidate_state
            self._pending_bars = 1

        if self._pending_bars >= required_bars:
            self._transition_to(candidate_state, timestamp, reason, metrics)

    def _reset_pending(self) -> None:
        """Reset pending candidate transition state."""
        self._pending_state = None
        self._pending_bars = 0

    def snapshot(self) -> dict[str, Any]:
        """Serialize state for atomic restart persistence."""
        candles_dict: dict[str, list[dict[str, Any]]] = {}
        for s, buf in self._candles.items():
            recent = list(buf)[-self.config.windows.peak_lookback_bars:]
            candles_dict[s] = [
                {
                    "symbol": c.symbol,
                    "timeframe": c.timeframe.value,
                    "start": c.start.isoformat(),
                    "end": c.end.isoformat(),
                    "open": str(c.open),
                    "high": str(c.high),
                    "low": str(c.low),
                    "close": str(c.close),
                    "volume": str(c.volume),
                    "feed_id": c.feed_id,
                    "consolidated": c.consolidated,
                    "origin": c.origin.value,
                    "price_basis": c.price_basis.value,
                    "status": c.status.value,
                    "trade_count": c.trade_count,
                }
                for c in recent
            ]

        return {
            "current_state": self._current_state.value,
            "previous_state": self._previous_state.value if self._previous_state else None,
            "state_since": self._state_since.isoformat(),
            "bars_in_state": self._bars_in_state,
            "trough_drawdown": str(self._trough_drawdown),
            "bars_since_trough": self._bars_since_trough,
            "peak_volatility": str(self._peak_volatility),
            "pending_state": self._pending_state.value if self._pending_state else None,
            "pending_bars": self._pending_bars,
            "vix_level": str(self._vix_level) if self._vix_level is not None else None,
            "vix_timestamp": self._vix_timestamp.isoformat() if self._vix_timestamp is not None else None,
            "transitions": [
                {
                    "from_state": t.from_state.value,
                    "to_state": t.to_state.value,
                    "timestamp": t.timestamp.isoformat(),
                    "reason": t.reason,
                    "metrics_summary": dict(t.metrics_summary),
                }
                for t in self._transitions[-20:]
            ],
            "candles": candles_dict,
        }

    def restore(self, data: Mapping[str, Any]) -> None:
        """Restore state from serialized persistence dictionary."""
        if not isinstance(data, Mapping):
            raise TypeError("data must be a mapping")

        self._current_state = MarketState(data["current_state"])
        self._previous_state = MarketState(data["previous_state"]) if data.get("previous_state") else None
        self._state_since = datetime.fromisoformat(data["state_since"]).astimezone(UTC)
        self._bars_in_state = int(data.get("bars_in_state", 0))
        self._trough_drawdown = Decimal(str(data.get("trough_drawdown", "0")))
        self._bars_since_trough = int(data.get("bars_since_trough", 0))
        self._peak_volatility = Decimal(str(data.get("peak_volatility", "0")))

        pending = data.get("pending_state")
        self._pending_state = MarketState(pending) if pending else None
        self._pending_bars = int(data.get("pending_bars", 0))

        vix = data.get("vix_level")
        self._vix_level = Decimal(str(vix)) if vix is not None else None
        vix_ts = data.get("vix_timestamp")
        self._vix_timestamp = datetime.fromisoformat(vix_ts).astimezone(UTC) if vix_ts else None

        records: list[StateTransitionRecord] = []
        for raw in data.get("transitions", []):
            records.append(
                StateTransitionRecord(
                    from_state=MarketState(raw["from_state"]),
                    to_state=MarketState(raw["to_state"]),
                    timestamp=datetime.fromisoformat(raw["timestamp"]).astimezone(UTC),
                    reason=str(raw["reason"]),
                    metrics_summary=raw.get("metrics_summary", {}),
                )
            )
        self._transitions = records

        candles_data = data.get("candles")
        if isinstance(candles_data, Mapping):
            for s, bar_list in candles_data.items():
                s_norm = s.strip().upper()
                if s_norm in self._candles and isinstance(bar_list, list):
                    for b_dict in bar_list:
                        try:
                            bar = Candle(
                                symbol=b_dict["symbol"],
                                timeframe=CandleTimeframe(b_dict["timeframe"]),
                                start=datetime.fromisoformat(b_dict["start"]).astimezone(UTC),
                                end=datetime.fromisoformat(b_dict["end"]).astimezone(UTC),
                                open=Decimal(str(b_dict["open"])),
                                high=Decimal(str(b_dict["high"])),
                                low=Decimal(str(b_dict["low"])),
                                close=Decimal(str(b_dict["close"])),
                                volume=Decimal(str(b_dict["volume"])),
                                feed_id=b_dict.get("feed_id", "sip"),
                                consolidated=bool(b_dict.get("consolidated", True)),
                                origin=CandleOrigin(b_dict.get("origin", "historical_rest")),
                                price_basis=PriceBasis(b_dict.get("price_basis", "RAW")),
                                status=CandleStatus(b_dict.get("status", "CLOSED")),
                                trade_count=b_dict.get("trade_count"),
                            )
                            self.add_bar(bar)
                        except Exception:
                            continue
