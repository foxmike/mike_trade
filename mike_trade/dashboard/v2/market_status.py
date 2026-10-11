"""Market Status Monitor REST API v2 endpoints under /api/v2/market-status/."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query

from mike_trade.dashboard.v2.models_market import (
    BenchmarkSymbolResponse,
    CompositeMetricsResponse,
    MarketBarItem,
    MarketBarsResponse,
    MarketState,
    MarketStatusOverviewResponse,
    PermissionLevel,
    PolicyRulesResponse,
    SymbolBarsResponse,
    ThresholdsConfigResponse,
    TradingAuthorizationResponse,
    TransitionRecordResponse,
    TransitionsListResponse,
)
from mike_trade.persistence.readstore import ReadStore, ReadStoreUnavailableError
from mike_trade.persistence.schema import from_db_timestamp


def _dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    try:
        return from_db_timestamp(value)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except Exception:
        return None


def _recovery_requirements(state: str) -> str:
    match state:
        case "NORMAL":
            return (
                "Normal market observation active. All strategy entries authorized. "
                "Drawdown <= -1.5% across consensus of reference symbols triggers WARNING."
            )
        case "WARNING":
            return (
                "To clear WARNING and normalize: Composite drawdown must recover above -0.8% "
                "and confirm for 3 consecutive 1m bars. Relapse: Drawdown breaching -3.5% "
                "escalates to CRASH."
            )
        case "CRASH":
            return (
                "To transition to STABILIZING: Minimum 5 bars in CRASH, establish a trough "
                "with no new lows for 5 bars, and realized volatility must contract by at least "
                "20% (decay ratio <= 0.80)."
            )
        case "STABILIZING":
            return (
                "To advance to RECOVERY: Minimum 5 bars in STABILIZING, composite "
                "drawdown >= -1.2%, momentum >= 0 bps, and breadth weakness <= 34%. "
                "Relapse: New low breaches trough and triggers immediate return to CRASH."
            )
        case "RECOVERY":
            return (
                "To achieve FULL NORMALIZATION: Drawdown must recover above -0.5%, "
                "realized volatility < 25 bps, and momentum >= 0 bps for 3 consecutive bars. "
                "Relapse: Drawdown breaching -3.5% aborts recovery back to CRASH."
            )
        case _:
            return "Awaiting initial market observations from engine."


def _compute_benchmark_stats(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {
            "latest_price": None,
            "peak_price": None,
            "drawdown_percent": None,
            "realized_volatility_bps": None,
            "momentum_bps": None,
            "last_bar_timestamp": None,
            "bar_count": 0,
            "warning_breached": False,
            "crash_breached": False,
            "flash_crash_breached": False,
        }

    bars_sorted = sorted(bars, key=lambda b: str(b.get("timestamp") or b.get("start") or ""))
    latest = bars_sorted[-1]
    latest_price = float(latest.get("close") or 0.0)
    lookback = bars_sorted[-390:]
    peak_price = max((float(b.get("high") or 0.0) for b in lookback), default=latest_price)
    drawdown = (
        ((latest_price - peak_price) / peak_price * 100.0) if peak_price > 0 else 0.0
    )

    # Volatility over last 20 bars
    vol_sample = bars_sorted[-20:]
    vol_bps: float | None = None
    if len(vol_sample) >= 2:
        returns = []
        for i in range(1, len(vol_sample)):
            p0 = float(vol_sample[i - 1].get("close") or 0.0)
            p1 = float(vol_sample[i].get("close") or 0.0)
            if p0 > 0:
                returns.append((p1 - p0) / p0)
        if len(returns) >= 2:
            mean_r = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            vol_bps = math.sqrt(max(0.0, variance)) * 10000.0

    # Momentum vs 20-bar SMA
    mom_bps: float | None = None
    if len(vol_sample) >= 1:
        closes = [float(b.get("close") or 0.0) for b in vol_sample]
        sma = sum(closes) / len(closes)
        if sma > 0:
            mom_bps = ((latest_price - sma) / sma) * 10000.0

    ts_raw = latest.get("timestamp") or latest.get("start")
    last_ts = _dt(ts_raw)

    return {
        "latest_price": round(latest_price, 4),
        "peak_price": round(peak_price, 4),
        "drawdown_percent": round(drawdown, 4),
        "realized_volatility_bps": round(vol_bps, 2) if vol_bps is not None else None,
        "momentum_bps": round(mom_bps, 2) if mom_bps is not None else None,
        "last_bar_timestamp": last_ts,
        "bar_count": len(bars_sorted),
        "warning_breached": drawdown <= -1.5 or (vol_bps is not None and vol_bps >= 25.0),
        "crash_breached": drawdown <= -3.5 or (vol_bps is not None and vol_bps >= 50.0),
        "flash_crash_breached": drawdown <= -5.0,
    }


def create_market_status_v2_router(service: Any) -> APIRouter:
    """Create read-only FastAPI router under /api/v2/market-status."""
    router = APIRouter(prefix="/api/v2/market-status", tags=["market-status"])

    def _resolve_scope_and_store(
        requested_id: str | None,
    ) -> tuple[str, ReadStore, dict[str, Any] | None, dict[str, Any] | None]:
        try:
            store = ReadStore.open(service._db_path)
        except ReadStoreUnavailableError:
            raise

        scopes = store.list_account_scopes()
        scope_id = requested_id.strip() if requested_id else None
        if not scope_id:
            if scopes:
                scope_id = str(scopes[0]["account_scope_id"])
            else:
                scope_id = "default"

        engine_row = store._view.execute(
            "SELECT status, heartbeat_at, observed_at, details_json FROM service_health "
            "WHERE account_scope_id = ? AND component = 'engine'",
            (scope_id,),
        ).fetchone()

        ms_row = store._view.execute(
            "SELECT status, heartbeat_at, observed_at, details_json FROM service_health "
            "WHERE account_scope_id = ? AND component = 'market_status_monitor'",
            (scope_id,),
        ).fetchone()

        engine_data: dict[str, Any] | None = None
        if engine_row:
            engine_data = {
                "status": engine_row[0],
                "heartbeat_at": _dt(engine_row[1]),
                "observed_at": _dt(engine_row[2]),
                "details": json.loads(engine_row[3]) if engine_row[3] else {},
            }

        ms_data: dict[str, Any] | None = None
        if ms_row:
            ms_data = {
                "status": ms_row[0],
                "heartbeat_at": _dt(ms_row[1]),
                "observed_at": _dt(ms_row[2]),
                "details": json.loads(ms_row[3]) if ms_row[3] else {},
            }

        return scope_id, store, engine_data, ms_data

    @router.get("", response_model=MarketStatusOverviewResponse)
    @router.get("/", response_model=MarketStatusOverviewResponse)
    @router.get("/state", response_model=MarketStatusOverviewResponse)
    def get_market_overview(
        account_id: str | None = Query(None, description="Account scope identifier"),
    ) -> MarketStatusOverviewResponse:
        """Comprehensive market state overview, risk metrics, and trading permissions."""
        now = service.server_time()
        scope_id, store, engine_data, ms_data = _resolve_scope_and_store(account_id)

        try:
            engine_hb = engine_data.get("heartbeat_at") if engine_data else None
            engine_age = (now - engine_hb).total_seconds() if engine_hb else 999999.0
            engine_online = bool(engine_hb and engine_age <= 60.0)

            data_available = bool(ms_data and ms_data.get("details"))
            details: dict[str, Any] = ms_data["details"] if data_available else {}

            raw_state = str(details.get("current_state", "NORMAL")).upper()
            state_val: MarketState = (
                raw_state
                if raw_state in {"NORMAL", "WARNING", "CRASH", "STABILIZING", "RECOVERY"}
                else "NORMAL"
            )

            valid_states = {"NORMAL", "WARNING", "CRASH", "STABILIZING", "RECOVERY"}
            raw_prev = details.get("previous_state")
            prev_val: MarketState | None = (
                str(raw_prev).upper()
                if raw_prev and str(raw_prev).upper() in valid_states
                else None
            )

            state_since_dt = _dt(details.get("state_since")) or now
            duration_sec = max(0.0, (now - state_since_dt).total_seconds())
            bars_in_state = int(details.get("bars_in_state", 0))

            last_eval = ms_data.get("observed_at") if ms_data else None
            last_step = _dt(details.get("last_step_end"))

            # Staleness check
            is_stale = not engine_online or (
                last_step is not None and (now - last_step).total_seconds() > 300.0
            )

            fail_closed_reason = details.get("fail_closed_reason")
            fail_closed = bool(fail_closed_reason)

            # Determine entry permissions based on engine state and policy
            buy_allowed = False
            short_allowed = False
            buy_permission: PermissionLevel = "BLOCKED"
            short_permission: PermissionLevel = "BLOCKED"
            reasons: list[str] = []

            if not data_available:
                reasons.append("MARKET_DATA_UNAVAILABLE")
            elif not engine_online:
                reasons.append("ENGINE_OFFLINE")
            elif is_stale:
                reasons.append("MARKET_DATA_STALE")
            elif fail_closed:
                reasons.append(str(fail_closed_reason))
            else:
                match state_val:
                    case "NORMAL":
                        buy_allowed = True
                        short_allowed = True
                        buy_permission = "ALLOWED"
                        short_permission = "ALLOWED"
                    case "WARNING":
                        buy_allowed = False
                        short_allowed = True
                        buy_permission = "RESTRICTED"
                        short_permission = "ALLOWED"
                        reasons.append("MARKET_WARNING_ELEVATED_RISK")
                    case "CRASH":
                        buy_allowed = False
                        short_allowed = True
                        buy_permission = "BLOCKED"
                        short_permission = "ALLOWED"
                        reasons.append("MARKET_CRASH_ACUTE_RISK")
                    case "STABILIZING":
                        buy_allowed = False
                        short_allowed = False
                        buy_permission = "BLOCKED"
                        short_permission = "RESTRICTED"
                        reasons.append("MARKET_STABILIZING_OBSERVATION")
                    case "RECOVERY":
                        buy_allowed = True
                        short_allowed = False
                        buy_permission = "RESTRICTED"
                        short_permission = "RESTRICTED"
                        reasons.append("MARKET_RECOVERY_CONDITIONAL")

            authorization = TradingAuthorizationResponse(
                state=state_val,
                evaluated_at=last_eval or now,
                buy_allowed=buy_allowed,
                short_allowed=short_allowed,
                sell_allowed=True,
                cover_allowed=True,
                protective_stops_allowed=True,
                emergency_liquidation_allowed=True,
                buy_permission=buy_permission,
                short_permission=short_permission,
                reasons=reasons,
            )

            policy_rules = PolicyRulesResponse()

            # Retrieve benchmark ETF bars from market_bars or snapshot candles
            benchmark_cards: list[BenchmarkSymbolResponse] = []
            symbols = ["SPY", "QQQ", "IWM"]
            stored_candles = details.get("candles", {})

            for sym in symbols:
                # First check market_bars table
                rows = store._view.execute(
                    "SELECT bar_start, open_price, high_price, low_price, "
                    "close_price, volume, vwap FROM market_bars "
                    "WHERE account_scope_id = ? AND symbol = ? AND timeframe = '1m' "
                    "ORDER BY bar_start DESC LIMIT 390",
                    (scope_id, sym),
                ).fetchall()

                bars: list[dict[str, Any]] = []
                if rows:
                    for r in rows:
                        bars.append({
                            "timestamp": r[0],
                            "open": float(r[1]),
                            "high": float(r[2]),
                            "low": float(r[3]),
                            "close": float(r[4]),
                            "volume": float(r[5]),
                            "vwap": float(r[6]) if r[6] is not None else None,
                        })
                elif sym in stored_candles and isinstance(stored_candles[sym], list):
                    bars = list(stored_candles[sym])

                stats = _compute_benchmark_stats(bars)
                bar_stale = (
                    is_stale
                    or stats["last_bar_timestamp"] is None
                    or (now - stats["last_bar_timestamp"]).total_seconds() > 300.0
                )

                benchmark_cards.append(
                    BenchmarkSymbolResponse(
                        symbol=sym,
                        latest_price=stats["latest_price"],
                        peak_price=stats["peak_price"],
                        drawdown_percent=stats["drawdown_percent"],
                        realized_volatility_bps=stats["realized_volatility_bps"],
                        momentum_bps=stats["momentum_bps"],
                        last_bar_timestamp=stats["last_bar_timestamp"],
                        bar_count=stats["bar_count"],
                        is_stale=bar_stale,
                        warning_breached=stats["warning_breached"],
                        crash_breached=stats["crash_breached"],
                        flash_crash_breached=stats["flash_crash_breached"],
                    )
                )

            # Compute composite metrics
            active_cards = [b for b in benchmark_cards if b.latest_price is not None]
            comp_dd: float | None = None
            worst_dd: float | None = None
            comp_vol: float | None = None
            comp_mom: float | None = None
            breadth_weak: float | None = None

            if active_cards:
                dds = [b.drawdown_percent for b in active_cards if b.drawdown_percent is not None]
                if dds:
                    comp_dd = round(sum(dds) / len(dds), 4)
                    worst_dd = round(min(dds), 4)
                    weak_count = sum(1 for d in dds if d <= -1.5)
                    breadth_weak = round(weak_count / len(dds), 2)

                vols = [
                    b.realized_volatility_bps
                    for b in active_cards
                    if b.realized_volatility_bps is not None
                ]
                if vols:
                    comp_vol = round(sum(vols) / len(vols), 2)

                moms = [b.momentum_bps for b in active_cards if b.momentum_bps is not None]
                if moms:
                    comp_mom = round(sum(moms) / len(moms), 2)

            trough_val = (
                float(details["trough_drawdown"])
                if details.get("trough_drawdown") is not None
                else (comp_dd or 0.0)
            )
            peak_vol_val = (
                float(details["peak_volatility"])
                if details.get("peak_volatility") is not None
                else (comp_vol or 0.0)
            )
            rebound_val = (
                (comp_dd - trough_val)
                if (comp_dd is not None and trough_val is not None)
                else 0.0
            )

            vix_val = float(details["vix_level"]) if details.get("vix_level") is not None else None
            vix_ts = _dt(details.get("vix_timestamp"))

            composite = CompositeMetricsResponse(
                composite_drawdown_percent=comp_dd,
                worst_drawdown_percent=worst_dd,
                realized_volatility_bps=comp_vol,
                momentum_bps=comp_mom,
                breadth_weakness_ratio=breadth_weak,
                trough_drawdown_percent=round(trough_val, 4),
                bars_since_trough=int(details.get("bars_since_trough", 0)),
                peak_volatility_bps=round(peak_vol_val, 2),
                rebound_from_trough_percent=round(rebound_val, 4),
                vix_level=vix_val,
                vix_timestamp=vix_ts,
            )

            # Determine primary state trigger reason
            reason_str = "Standard operating conditions."
            if not data_available:
                reason_str = "No market observations recorded in database."
            elif not engine_online:
                reason_str = "Trading engine heartbeat offline (>60s)."
            elif transitions := details.get("transitions"):
                last_t = transitions[-1]
                reason_str = str(last_t.get("reason", "Transition recorded."))

            return MarketStatusOverviewResponse(
                account_id=scope_id,
                market_state=state_val,
                previous_state=prev_val,
                state_since=state_since_dt,
                duration_seconds=round(duration_sec, 1),
                bars_in_state=bars_in_state,
                last_evaluation_time=last_eval,
                last_step_end=last_step,
                engine_online=engine_online,
                engine_heartbeat_at=engine_hb,
                is_stale=is_stale,
                data_available=data_available,
                fail_closed=fail_closed,
                fail_closed_reason=fail_closed_reason,
                reason=reason_str,
                recovery_requirements=_recovery_requirements(state_val),
                authorization=authorization,
                policy_rules=policy_rules,
                composite_metrics=composite,
                benchmarks=benchmark_cards,
            )
        finally:
            store.close()

    @router.get("/benchmarks", response_model=list[BenchmarkSymbolResponse])
    def get_benchmarks(
        account_id: str | None = Query(None, description="Account scope identifier"),
    ) -> list[BenchmarkSymbolResponse]:
        """Real-time metrics and threshold checks for SPY, QQQ, and IWM."""
        overview = get_market_overview(account_id=account_id)
        return overview.benchmarks

    @router.get("/transitions", response_model=TransitionsListResponse)
    def get_transitions(
        account_id: str | None = Query(None, description="Account scope identifier"),
        limit: int = Query(50, ge=1, le=500, description="Max transition events to return"),
    ) -> TransitionsListResponse:
        """Historical audit log of market state transitions."""
        scope_id, store, _, ms_data = _resolve_scope_and_store(account_id)
        try:
            records: list[TransitionRecordResponse] = []

            # 1. Try reading from append-only table market_status_transitions
            try:
                rows = store._view.execute(
                    "SELECT from_state, to_state, transition_at, reason, metrics_json "
                    "FROM market_status_transitions WHERE account_scope_id = ? "
                    "ORDER BY transition_at DESC LIMIT ?",
                    (scope_id, limit),
                ).fetchall()
                for r in rows:
                    metrics_summary = json.loads(r[4]) if r[4] else {}
                    t_dt = _dt(r[2]) or service.server_time()
                    records.append(
                        TransitionRecordResponse(
                            from_state=str(r[0]),
                            to_state=str(r[1]),
                            timestamp=t_dt,
                            reason=str(r[3]),
                            metrics_summary=metrics_summary,
                        )
                    )
            except Exception:
                pass

            # 2. Fall back to document transitions if table had none
            if not records and ms_data and ms_data.get("details"):
                raw_list = ms_data["details"].get("transitions", [])
                for t in reversed(raw_list):
                    t_dt = _dt(t.get("timestamp")) or service.server_time()
                    records.append(
                        TransitionRecordResponse(
                            from_state=str(t.get("from_state", "UNKNOWN")),
                            to_state=str(t.get("to_state", "UNKNOWN")),
                            timestamp=t_dt,
                            reason=str(t.get("reason", "Unknown")),
                            metrics_summary=t.get("metrics_summary", {}),
                        )
                    )
                records = records[:limit]

            return TransitionsListResponse(
                account_id=scope_id,
                transitions=records,
                total=len(records),
            )
        finally:
            store.close()

    @router.get("/bars", response_model=MarketBarsResponse)
    def get_market_bars(
        account_id: str | None = Query(None, description="Account scope identifier"),
        symbol: str | None = Query(None, description="Specific ETF symbol (SPY, QQQ, IWM)"),
        limit: int = Query(120, ge=1, le=1000, description="Number of completed 1m bars"),
    ) -> MarketBarsResponse:
        """Completed 1m candlestick series for SPY, QQQ, and IWM."""
        now = service.server_time()
        scope_id, store, _, ms_data = _resolve_scope_and_store(account_id)
        try:
            target_symbols = [symbol.strip().upper()] if symbol else ["SPY", "QQQ", "IWM"]
            stored_candles = (
                ms_data["details"].get("candles", {})
                if (ms_data and ms_data.get("details"))
                else {}
            )

            symbol_results: list[SymbolBarsResponse] = []

            for sym in target_symbols:
                # Query market_bars table
                rows = store._view.execute(
                    "SELECT bar_start, open_price, high_price, low_price, "
                    "close_price, volume, vwap FROM market_bars "
                    "WHERE account_scope_id = ? AND symbol = ? AND timeframe = '1m' "
                    "ORDER BY bar_start DESC LIMIT ?",
                    (scope_id, sym, limit),
                ).fetchall()

                bar_items: list[MarketBarItem] = []
                if rows:
                    for r in reversed(rows):
                        bar_ts = _dt(r[0]) or now
                        bar_items.append(
                            MarketBarItem(
                                timestamp=bar_ts,
                                open=float(r[1]),
                                high=float(r[2]),
                                low=float(r[3]),
                                close=float(r[4]),
                                volume=float(r[5]),
                                vwap=float(r[6]) if r[6] is not None else None,
                            )
                        )
                elif sym in stored_candles and isinstance(stored_candles[sym], list):
                    c_list = stored_candles[sym][-limit:]
                    for c in c_list:
                        bar_ts = _dt(c.get("start")) or now
                        bar_items.append(
                            MarketBarItem(
                                timestamp=bar_ts,
                                open=float(c.get("open", 0.0)),
                                high=float(c.get("high", 0.0)),
                                low=float(c.get("low", 0.0)),
                                close=float(c.get("close", 0.0)),
                                volume=float(c.get("volume", 0.0)),
                                vwap=float(c["vwap"]) if c.get("vwap") is not None else None,
                            )
                        )

                symbol_results.append(
                    SymbolBarsResponse(
                        symbol=sym,
                        timeframe="1m",
                        bars=bar_items,
                    )
                )

            return MarketBarsResponse(
                account_id=scope_id,
                as_of=now,
                symbols=symbol_results,
            )
        finally:
            store.close()

    @router.get("/config", response_model=ThresholdsConfigResponse)
    def get_market_config() -> ThresholdsConfigResponse:
        """Configured market status parameters, thresholds, and observation windows."""
        return ThresholdsConfigResponse()

    return router
