"""Shared harness: a PAPER runtime graph on the fake Alpaca exchange (no network, no creds)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from tests.support.fake_alpaca import FakeAlpacaExchange, FakeClock

from mike_trade.app import (
    AlpacaBrokerAdapter,
    AppSettings,
    StrategyAssignment,
    SymbolStrategySettings,
    build_runtime,
)
from mike_trade.domain.trading import EntryEligibilityPolicy
from mike_trade.live.capability_records import load_capability_matrix
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext, QuoteSnapshot
from mike_trade.runtime import ExecutionMode
from mike_trade.scheduling.calendar import SessionStatus

ENV = {
    "ALPACA_PAPER_KEY_ID": "PKTEST", "ALPACA_PAPER_SECRET_KEY": "sk-test",
    "ALPACA_LIVE_KEY_ID": "AKTEST", "ALPACA_LIVE_SECRET_KEY": "sk-live-test",
}


def ctx(symbol: str, at: datetime, version: int, price: str = "100", ask: str | None = None) -> MarketContext:
    bid = D(price)
    ask_d = D(ask) if ask is not None else bid + D("0.02")
    bars = tuple(
        Candle(symbol, CandleTimeframe.M1, at - timedelta(minutes=2 - i), at - timedelta(minutes=1 - i),
               bid, bid + D("0.2"), bid - D("0.2"), bid, D("100"), "iex", False,
               CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)
        for i in range(2)
    )
    return MarketContext(
        symbol=symbol, as_of=at, version=version, feed_id="iex", consolidated=False,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=f"XNYS-{at.astimezone(UTC).date().isoformat()}",
        freshness=ContextFreshness(at, timedelta(0), timedelta(seconds=90), False, None),
        candles={CandleTimeframe.M1: bars}, indicators={"vwap_prev": D("100"), "vwap": D("100")},
        latest_quote=QuoteSnapshot(symbol, bid, ask_d, D("100"), D("100"), at, at, "iex"),
    )


def assign(*pairs: tuple[str, dict]) -> tuple[StrategyAssignment, ...]:
    return tuple(StrategyAssignment(f"{pid}_cfg", pid, params) for pid, params in pairs)


def write_capabilities(
    path: Path, *, stop: bool = True, replace: bool = True, environment: str = "PAPER"
) -> None:
    import json

    kind = "paper_verification" if environment == "PAPER" else "operator_record"
    records = []
    if stop:
        records.append({"environment": environment, "kind": kind, "reference": "REC-STOP",
                        "recorded_on": "2026-10-12", "order_type": "stop", "sizing": "whole",
                        "time_in_force": "gtc", "side": "sell", "operation": "submit"})
    if replace:
        records.append({"environment": environment, "kind": kind, "reference": "REC-REPL",
                        "recorded_on": "2026-10-12", "order_type": "stop", "sizing": "whole",
                        "time_in_force": "gtc", "side": "sell", "operation": "replace"})
    path.write_text(json.dumps({"records": records}))


async def start_paper(
    tmp_path: Path, exchange: FakeAlpacaExchange, buys, sells, *, registry: Any = None,
    stop: dict | None = None, symbol: str = "AAA", capabilities: bool = True, eod: bool = False,
    eligibility: EntryEligibilityPolicy | None = None, name: str = "paper.sqlite3",
    mode: ExecutionMode = ExecutionMode.PAPER,
):
    cap_path = tmp_path / "alpaca_capabilities.json"
    if capabilities:
        write_capabilities(cap_path, environment=mode.value)
    adapter = AlpacaBrokerAdapter(
        mode, "live-test", exchange.clock, authorized=mode is ExecutionMode.LIVE, environ=ENV,
        http_transport=exchange.transport,
        capabilities_matrix=load_capability_matrix(cap_path),
    )
    cfg: dict[str, Any] = {
        "symbols": (symbol,),
        "symbol_strategies": (SymbolStrategySettings(symbol, assign(*buys), assign(*sells)),),
    }
    cfg["symbol_stop_policies"] = {symbol: stop or {"type": "fixed", "stop_percent": D("3")}}
    settings = AppSettings(
        mode=mode, enable_live_trading=mode is ExecutionMode.LIVE,
        db_path=tmp_path / name, account_id="live-test",
        per_symbol_budget=D("3000"), total_budget=D("10000"),
        live_eligibility=eligibility or EntryEligibilityPolicy(3, 3, False, timedelta(0), timedelta(0)),
        live_minimum_hold=timedelta(0), auto_eod_liquidation=eod, live_trade_updates=False,
        alpaca_capability_records=cap_path, credential_environment=ENV, **cfg,
    )
    overrides: dict[str, Any] = {"clock": exchange.clock, "broker": adapter}
    if registry is not None:
        overrides["strategies"] = registry
    runtime = build_runtime(settings, overrides=overrides)
    await runtime.start()

    async def no_sleep(_seconds: float) -> None:
        return None

    life = runtime.lifecycle
    life.sleep = no_sleep
    life.executor.sleep = no_sleep
    return runtime


__all__ = ["ENV", "FakeClock", "assign", "ctx", "start_paper", "write_capabilities"]
