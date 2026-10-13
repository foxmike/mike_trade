"""SHADOW and PAPER runtime integration of the model-driven strategies.

The real runtime graph (simulated SHADOW broker with temporary SQLite; PAPER on the fake Alpaca
exchange) runs the registered model strategies against pinned tiny native artifacts.  Nothing
here touches a network, credentials or a real broker.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from model_fixtures import build_model
from scripted_strategies import build_registry

from mike_trade.app import (
    AppSettings,
    ShadowScenarioSettings,
    SymbolStrategySettings,
    build_runtime,
)
from mike_trade.domain.errors import ConfigurationError
from mike_trade.domain.trading import OrderPurpose
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext, QuoteSnapshot
from mike_trade.market_status.config import MarketStatusConfig
from mike_trade.market_status.monitor import MarketStatusMonitor
from mike_trade.modeling.serving import BarVwapLedger, ModelInputProvider, build_inference_service
from mike_trade.runtime import ExecutionMode
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.buy.lightgbm_probability_buy import LightgbmProbabilityBuy
from mike_trade.strategies.buy.xgboost_probability_buy import XgboostProbabilityBuy
from mike_trade.strategies.sell.lightgbm_probability_sell import LightgbmProbabilitySell
from mike_trade.strategies.sell.xgboost_probability_sell import XgboostProbabilitySell
from tests.live_support import assign, start_paper
from tests.support.fake_alpaca import FakeAlpacaExchange

START = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)  # 10:30 New York, regular session
MINUTE = timedelta(minutes=1)
SCENARIO = "models"
PINS = {
    "xgboost_probability_buy": {"model_id": "xgboost_buy_m", "model_version": "1.0.0"},
    "lightgbm_probability_buy": {"model_id": "lightgbm_buy_m", "model_version": "1.0.0"},
    "xgboost_probability_sell": {"model_id": "xgboost_sell_m", "model_version": "1.0.0"},
    "lightgbm_probability_sell": {"model_id": "lightgbm_sell_m", "model_version": "1.0.0"},
}


class Clock:
    def __init__(self, value: datetime = START) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> datetime:
        self.value += timedelta(**kwargs)
        return self.value


@pytest.fixture(scope="module")
def model_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("runtime-models")
    for algorithm in ("xgboost", "lightgbm"):
        for role in ("buy", "sell"):
            build_model(
                root,
                algorithm=algorithm,
                role=role,
                model_id=f"{algorithm}_{role}_m",
                trained_symbols=("AAA", "BBB", "SPY"),
            )
    return root


def registry():
    reg = build_registry()
    for cls in (
        XgboostProbabilityBuy,
        LightgbmProbabilityBuy,
        XgboostProbabilitySell,
        LightgbmProbabilitySell,
    ):
        reg.register(cls)
    return reg


class Market:
    """Minute tape for AAA plus a flat SPY reference, with each bar's own VWAP recorded.

    The newest bar always ends at the clock's current minute; ``push`` advances the clock by one
    minute and appends the next close.
    """

    def __init__(self, closes: list[str], clock) -> None:
        self.clock = clock
        self.closes = [D(c) for c in closes]
        self.vwaps = BarVwapLedger()
        self.spy: list[Candle] = []
        self.rebuild_reference()

    def candle(self, symbol: str, index: int, close: D, previous: D) -> Candle:
        end = self.clock.now() - (len(self.closes) - 1 - index) * MINUTE
        begin = end - MINUTE
        return Candle(
            symbol,
            CandleTimeframe.M1,
            begin,
            end,
            previous,
            max(previous, close) + D("0.05"),
            min(previous, close) - D("0.05"),
            close,
            D("100000"),
            "iex",
            False,
            CandleOrigin.REPLAY,
            PriceBasis.RAW,
            CandleStatus.CLOSED,
        )

    def rebuild_reference(self) -> None:
        self.spy = [self.candle("SPY", i, D("400"), D("400")) for i in range(len(self.closes))]
        for bar in self.spy:
            self.vwaps.record("SPY", bar.start, bar.close)

    def push(self, close: str) -> None:
        self.clock.advance(minutes=1)
        self.closes.append(D(close))
        self.rebuild_reference()

    def history(self):
        market = self

        class History:
            def get_completed_candles(self, symbol, timeframe, limit=None):
                series = market.spy if symbol == "SPY" else ()
                return tuple(series[-limit:]) if limit else tuple(series)

        return History()

    def context(self, symbol: str = "AAA", *, version: int = 1) -> MarketContext:
        previous = self.closes[0]
        bars = []
        for index, close in enumerate(self.closes):
            bars.append(self.candle(symbol, index, close, previous))
            self.vwaps.record(symbol, bars[-1].start, close)
            previous = close
        bars = tuple(bars[-60:])
        at = bars[-1].end
        close = bars[-1].close
        return MarketContext(
            symbol=symbol,
            as_of=at,
            version=version,
            feed_id="iex",
            consolidated=False,
            price_basis=PriceBasis.RAW,
            session_status=SessionStatus.REGULAR,
            session_id=f"XNYS-{at.date().isoformat()}",
            freshness=ContextFreshness(at, timedelta(0), timedelta(seconds=90), False, None),
            candles={CandleTimeframe.M1: bars},
            indicators={"vwap": close, "vwap_prev": close},
            latest_quote=QuoteSnapshot(
                symbol, close - D("0.01"), close + D("0.01"), D("100"), D("100"), at, at, "iex"
            ),
        )

    def provider(self, model_root: Path, references, clock: Clock) -> ModelInputProvider:
        service = build_inference_service(model_root, references, clock.now)
        return ModelInputProvider(service, self.history(), self.vwaps)


def references(*strategy_ids: str):
    reg = registry()
    out = []
    for sid in strategy_ids:
        cls = reg.get(sid)
        out.append(cls.model_reference(cls.spec.parameter_schema.validate(PINS[sid])))
    return out


async def start(tmp_path, clock, provider, buys, sells, *, extra=None, settings=None):
    scenario = ShadowScenarioSettings(
        name=SCENARIO,
        starting_cash=D("50000"),
        per_symbol_budget=D("3000"),
        total_budget=D("20000"),
        max_entry_cycles_per_session=5,
        consecutive_loss_limit=3,
        post_sale_cooldown=timedelta(0),
        minimum_hold=timedelta(0),
    )
    config = AppSettings(
        mode=ExecutionMode.SHADOW,
        db_path=tmp_path / "t.sqlite3",
        shadow_scenarios=(scenario,),
        symbols=("AAA",),
        symbol_strategies=(SymbolStrategySettings("AAA", assign(*buys), assign(*sells)),),
        symbol_stop_policies={"AAA": {"type": "fixed", "stop_percent": D("3")}},
        **(settings or {}),
    )
    overrides = {
        "clock": clock,
        "strategies": registry(),
        "model_inputs": provider,
        **(extra or {}),
    }
    runtime = build_runtime(config, overrides=overrides)
    await runtime.start()
    return runtime, runtime.broker.scenarios[SCENARIO]


def model(strategy: str, **extra):
    return (strategy, {**PINS[strategy], **extra})


def orders(shadow, purpose):
    return [o for o in shadow.account.state.orders.values() if o.purpose is purpose]


QUIET = ["100"] * 40


# ------------------------------------------------------------------ SHADOW


async def test_shadow_model_buy_then_model_sell_uses_the_real_fill_and_keeps_the_stop(
    tmp_path, model_root, caplog
):
    clock = Clock()
    market = Market(QUIET, clock)
    provider = market.provider(
        model_root, references("xgboost_probability_buy", "lightgbm_probability_sell"), clock
    )
    runtime, shadow = await start(
        tmp_path,
        clock,
        provider,
        [model("xgboost_probability_buy")],
        [model("lightgbm_probability_sell")],
    )
    try:
        life = runtime.lifecycle
        quiet = await life.process({"AAA": market.context()})
        assert not any(a.startswith("ENTRY:") for a in quiet[SCENARIO])  # flat tape: no entry

        caplog.set_level(logging.INFO, logger="mike_trade.model")
        market.push("100.30")  # strong 1-minute return: the BUY model's favorable class
        entered = await life.process({"AAA": market.context()})
        assert sum(a.startswith("ENTRY:") for a in entered[SCENARIO]) == 1
        (entry,) = orders(shadow, OrderPurpose.ENTRY)
        assert entry.strategy_key == "xgboost_probability_buy"
        position = shadow.account.state.positions["AAA"]
        (stop,) = orders(shadow, OrderPurpose.PROTECTIVE_STOP)  # engine-owned protection
        assert stop.quantity == position.quantity
        signal_logs = [r for r in caplog.records if r.getMessage() == "Model strategy signal"]
        assert signal_logs and signal_logs[0].model_audit["model"] == "xgboost_buy_m@1.0.0"
        market.push("100.60")
        held = await life.process({"AAA": market.context()})
        assert not any(a.startswith("EXIT:") for a in held[SCENARIO])  # +0.3%: keep holding
        market.push("100.95")  # +0.6% on the actual fill: the SELL model says exit now
        caplog.clear()
        exited = await life.process({"AAA": market.context()})
        assert sum(a.startswith("EXIT:") for a in exited[SCENARIO]) == 1
        (exit_order,) = orders(shadow, OrderPurpose.STRATEGY_EXIT)
        assert exit_order.quantity == position.quantity  # the whole position
        decision = next(
            r.model_audit for r in caplog.records if r.getMessage() == "Model strategy signal"
        )
        assert decision["position"]["entry_price"] == str(position.avg_cost)
        assert decision["position"]["entry_time"] == position.opened_at.isoformat()
        assert decision["strategy"] == "lightgbm_probability_sell"
        assert decision["prediction"]["role"] == "sell"
        from mike_trade.domain.strategy_types import StrategyRole
        from mike_trade.strategies.base import make_signal_id

        expected = make_signal_id(
            "lightgbm_probability_sell",
            "1.0.0",
            "AAA",
            StrategyRole.SELL,
            datetime.fromisoformat(decision["bar_start"]),
        )
        assert expected in exit_order.intent_id or expected in exit_order.client_order_id
    finally:
        await runtime.stop()


async def test_shadow_protective_stop_is_not_disabled_by_a_model_that_prefers_to_hold(
    tmp_path, model_root
):
    clock = Clock()
    market = Market(QUIET, clock)
    provider = market.provider(
        model_root, references("lightgbm_probability_buy", "xgboost_probability_sell"), clock
    )
    runtime, shadow = await start(
        tmp_path,
        clock,
        provider,
        [model("lightgbm_probability_buy")],
        [model("xgboost_probability_sell", minimum_exit_probability_bps=10000)],
    )
    try:
        life = runtime.lifecycle
        market.push("100.30")
        await life.process({"AAA": market.context()})
        position = shadow.account.state.positions["AAA"]
        (stop,) = orders(shadow, OrderPurpose.PROTECTIVE_STOP)
        assert stop.stop_price is not None and stop.stop_price < position.avg_cost
        # the model keeps saying hold; the broker-side stop is untouched and still the only exit
        for close in ("100.31", "100.32"):
            market.push(close)
            out = await life.process({"AAA": market.context()})
            assert not any(a.startswith("EXIT:") for a in out[SCENARIO])
        assert len(orders(shadow, OrderPurpose.PROTECTIVE_STOP)) == 1
        assert not orders(shadow, OrderPurpose.STRATEGY_EXIT)
    finally:
        await runtime.stop()


async def test_shadow_inference_failure_isolates_only_the_model_strategy(tmp_path, model_root):
    clock = Clock()
    market = Market(QUIET, clock)
    market.spy = []  # reference series unavailable: every model prediction is unavailable
    provider = market.provider(model_root, references("xgboost_probability_buy"), clock)
    market.rebuild_reference = lambda: None  # type: ignore[method-assign]
    runtime, shadow = await start(
        tmp_path,
        clock,
        provider,
        [model("xgboost_probability_buy"), ("buy_alpha", {"fire_at": 3})],
        [],
    )
    try:
        market.push("100.30")
        market.spy = []
        out = await runtime.lifecycle.process({"AAA": market.context(version=3)})
        assert sum(a.startswith("ENTRY:") for a in out[SCENARIO]) == 1
        (entry,) = orders(shadow, OrderPurpose.ENTRY)
        assert entry.strategy_key == "buy_alpha"  # the unrelated technical strategy still trades
    finally:
        await runtime.stop()


async def test_shadow_market_crash_still_blocks_a_model_entry(tmp_path, model_root):
    clock = Clock(START - timedelta(minutes=46))
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    monitor = MarketStatusMonitor(config=config, clock=clock)

    def reference_bar(symbol: str, index: int, price: str) -> Candle:
        begin = START - timedelta(minutes=46) + index * MINUTE
        value = D(price)
        return Candle(
            symbol,
            CandleTimeframe.M1,
            begin,
            begin + MINUTE,
            value,
            value,
            value,
            value,
            D("10000"),
            "IEX",
            False,
            CandleOrigin.HISTORICAL_REST,
            PriceBasis.RAW,
            CandleStatus.CLOSED,
        )

    for index in range(45):
        clock.value += MINUTE
        crashed = index >= 35
        for symbol, normal, low in (
            ("SPY", "500", "460"),
            ("QQQ", "400", "365"),
            ("IWM", "200", "180"),
        ):
            monitor.add_bar(reference_bar(symbol, index, low if crashed else normal))
    assert clock.value == START - MINUTE
    clock.value = START
    market = Market(QUIET, clock)
    provider = market.provider(model_root, references("xgboost_probability_buy"), clock)
    runtime, shadow = await start(
        tmp_path,
        clock,
        provider,
        [model("xgboost_probability_buy")],
        [],
        extra={"market_status_monitor": monitor},
        settings={"market_status_enabled": True},
    )
    try:
        market.push("100.30")
        out = await runtime.lifecycle.process({"AAA": market.context()})
        assert not any(a.startswith("ENTRY:") for a in out[SCENARIO])
        assert not orders(shadow, OrderPurpose.ENTRY)
        # the model alone would have entered (same tape without the market gate)
        clean_dir = tmp_path / "clean"
        clean_dir.mkdir()
        clean_runtime, clean_shadow = await start(
            clean_dir, Clock(), provider, [model("xgboost_probability_buy")], []
        )
        try:
            clean = await clean_runtime.lifecycle.process({"AAA": market.context()})
            assert sum(a.startswith("ENTRY:") for a in clean[SCENARIO]) == 1
        finally:
            await clean_runtime.stop()
    finally:
        await runtime.stop()


async def test_shadow_buy_models_are_not_evaluated_while_holding_and_sell_models_not_while_flat(
    tmp_path, model_root
):
    clock = Clock()
    market = Market(QUIET, clock)
    provider = market.provider(
        model_root, references("xgboost_probability_buy", "lightgbm_probability_sell"), clock
    )
    calls: list[tuple[str, ...]] = []
    original = provider.inputs

    def spy(context, refs, position=None):
        calls.append(tuple(ref.role for ref in refs))
        return original(context, refs, position)

    provider.inputs = spy  # type: ignore[method-assign]
    runtime, _ = await start(
        tmp_path,
        clock,
        provider,
        [model("xgboost_probability_buy")],
        [model("lightgbm_probability_sell")],
    )
    try:
        life = runtime.lifecycle
        await life.process({"AAA": market.context()})
        assert calls == [("buy",)]  # flat: only the BUY model is consulted
        market.push("100.30")
        await life.process({"AAA": market.context()})  # entry
        calls.clear()
        market.push("100.40")
        await life.process({"AAA": market.context()})
        assert calls == [("sell",)]  # holding: only the SELL model is consulted
    finally:
        await runtime.stop()


async def test_shadow_history_of_another_account_does_not_leak_position_facts(tmp_path, model_root):
    """Each scenario's SELL prediction is bound to that scenario's own position."""
    clock = Clock()
    market = Market(QUIET, clock)
    provider = market.provider(
        model_root, references("xgboost_probability_buy", "xgboost_probability_sell"), clock
    )
    scenario_a = ShadowScenarioSettings(
        name="a",
        starting_cash=D("50000"),
        per_symbol_budget=D("3000"),
        total_budget=D("20000"),
        max_entry_cycles_per_session=5,
        consecutive_loss_limit=3,
        post_sale_cooldown=timedelta(0),
        minimum_hold=timedelta(0),
    )
    scenario_b = ShadowScenarioSettings(
        name="b",
        starting_cash=D("5000"),
        per_symbol_budget=D("1000"),
        total_budget=D("5000"),
        max_entry_cycles_per_session=5,
        consecutive_loss_limit=3,
        post_sale_cooldown=timedelta(0),
        minimum_hold=timedelta(0),
    )
    runtime = build_runtime(
        AppSettings(
            mode=ExecutionMode.SHADOW,
            db_path=tmp_path / "two.sqlite3",
            shadow_scenarios=(scenario_a, scenario_b),
            symbols=("AAA",),
            symbol_strategies=(
                SymbolStrategySettings(
                    "AAA",
                    assign(model("xgboost_probability_buy")),
                    assign(model("xgboost_probability_sell")),
                ),
            ),
            symbol_stop_policies={"AAA": {"type": "fixed", "stop_percent": D("3")}},
        ),
        overrides={"clock": clock, "strategies": registry(), "model_inputs": provider},
    )
    await runtime.start()
    try:
        market.push("100.30")
        await runtime.lifecycle.process({"AAA": market.context()})
        for scenario in ("a", "b"):
            assert "AAA" in runtime.broker.scenarios[scenario].account.state.positions
        service = provider.service
        before = service.diagnostics()["predictions"]
        market.push("100.50")
        await runtime.lifecycle.process({"AAA": market.context()})
        # both scenarios hold the same fill, so the two SELL evaluations share one prediction
        assert service.diagnostics()["predictions"] - before == 1
        assert service.diagnostics()["cache_hits"] >= 1
    finally:
        await runtime.stop()


# ------------------------------------------------------------------ startup validation


def settings_for(tmp_path: Path, model_root: Path, **overrides):
    return AppSettings(
        mode=ExecutionMode.SHADOW,
        db_path=tmp_path / "s.sqlite3",
        shadow_scenarios=(
            ShadowScenarioSettings(
                name=SCENARIO,
                starting_cash=D("50000"),
                per_symbol_budget=D("3000"),
                total_budget=D("20000"),
                max_entry_cycles_per_session=5,
                consecutive_loss_limit=3,
                post_sale_cooldown=timedelta(0),
                minimum_hold=timedelta(0),
            ),
        ),
        symbols=("AAA",),
        model_root=model_root,
        symbol_strategies=(
            SymbolStrategySettings("AAA", assign(model("xgboost_probability_buy")), ()),
        ),
        symbol_stop_policies={"AAA": {"type": "fixed", "stop_percent": D("3")}},
        **overrides,
    )


def test_startup_fails_clearly_for_missing_unpinned_and_wrong_feed_models(tmp_path, model_root):
    with pytest.raises(ConfigurationError) as missing:
        build_runtime(settings_for(tmp_path, tmp_path / "nowhere"), overrides={"clock": Clock()})
    assert missing.value.code == "MODEL_MODEL_MISSING"
    assert "xgboost_buy_m@1.0.0" in str(missing.value)

    base = settings_for(tmp_path, model_root)
    wrong_version = replace(
        base,
        symbol_strategies=(
            SymbolStrategySettings(
                "AAA",
                assign(
                    (
                        "xgboost_probability_buy",
                        {**PINS["xgboost_probability_buy"], "model_version": "9.9.9"},
                    )
                ),
                (),
            ),
        ),
    )
    with pytest.raises(ConfigurationError) as version:
        build_runtime(wrong_version, overrides={"clock": Clock()})
    assert version.value.code == "MODEL_MODEL_MISSING"  # never resolves to "latest"

    with pytest.raises(ConfigurationError) as feed:
        build_runtime(
            settings_for(tmp_path, model_root, market_data_feed="sip"), overrides={"clock": Clock()}
        )
    assert feed.value.code == "MODEL_FEED_MISMATCH"


def approve(model_root: Path, model_id: str, stage: str = "shadow") -> None:
    from mike_trade.modeling.approvals import ApprovalRepository, approve_model

    if ApprovalRepository(model_root).get(model_id, "1.0.0", stage) is None:
        approve_model(model_root, model_id, "1.0.0", stage, approved_by="test", reason="test")


def test_startup_requires_an_operator_approval_for_the_mode(tmp_path):
    fresh = tmp_path / "fresh-models"
    build_model(fresh, algorithm="xgboost", role="buy", model_id="xgboost_buy_m")
    with pytest.raises(ConfigurationError) as raised:
        build_runtime(settings_for(tmp_path, fresh), overrides={"clock": Clock()})
    assert raised.value.code == "MODEL_NOT_APPROVED"
    assert "model-approve" in str(raised.value)


def test_startup_loads_pinned_models_once_and_binds_the_lifecycle(tmp_path, model_root):
    approve(model_root, "xgboost_buy_m")
    runtime = build_runtime(settings_for(tmp_path, model_root), overrides={"clock": Clock()})
    provider = runtime.lifecycle._model_inputs
    assert provider is not None
    assert [ref.key for ref in provider.service.references] == ["xgboost_buy_m@1.0.0"]
    assert runtime.market_data._model_reference_symbols == ("SPY",)  # the reference is subscribed
    assert runtime.market_data._bar_vwaps is not None


def test_a_deployment_without_model_strategies_loads_no_model_library(tmp_path):
    import subprocess
    import sys

    code = (
        "import sys\n"
        "from mike_trade.app import AppSettings, build_runtime\n"
        "from mike_trade.runtime import ExecutionMode\n"
        "from decimal import Decimal\n"
        f"build_runtime(AppSettings(mode=ExecutionMode.SHADOW, db_path=r'{tmp_path}/n.sqlite3'))\n"
        "raise SystemExit(int('xgboost' in sys.modules or 'lightgbm' in sys.modules))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# ------------------------------------------------------------------ PAPER


async def test_paper_model_buy_and_sell_use_confirmed_broker_fills(tmp_path, model_root):
    exchange = FakeAlpacaExchange()
    exchange.set_price("AAA", "100.30")
    clock = exchange.clock
    market = Market(QUIET, clock)
    provider = market.provider(
        model_root, references("xgboost_probability_buy", "lightgbm_probability_sell"), clock
    )
    runtime = await start_paper(
        tmp_path,
        exchange,
        [model("xgboost_probability_buy")],
        [model("lightgbm_probability_sell")],
        registry=registry(),
        extra_overrides={"model_inputs": provider},
    )
    try:
        life = runtime.lifecycle
        market.push("100.30")
        actions = await life.process({"AAA": market.context()})
        assert any(a.startswith("FILL:BUY") or "BUY" in a for a in actions["live"]), actions
        assert exchange.position_qty("AAA") > 0
        (stop,) = exchange.open_stops("AAA")  # broker-held protection, independent of any model
        assert stop["side"] == "sell"
        quantity = exchange.position_qty("AAA")
        cycle = life.ledger.active_cycle("AAA")
        assert cycle is not None and cycle.avg_entry_price == D("100.30")

        market.push("100.60")
        exchange.set_price("AAA", "100.60")
        out = await life.process({"AAA": market.context()})
        assert not any(a.startswith("EXIT:") for a in out["live"])
        market.push("100.95")
        exchange.set_price("AAA", "100.95")
        out = await life.process({"AAA": market.context()})
        assert any(a.startswith("EXIT:") for a in out["live"]), out
        sells = [b for b in exchange.posts() if b["side"] == "sell" and b["type"] == "market"]
        assert len(sells) == 1 and D(sells[0]["qty"]) == quantity
        assert exchange.position_qty("AAA") == 0
    finally:
        await runtime.stop()


# ------------------------------------------------------------------ the real live data path


async def test_live_stream_path_re_evaluates_when_the_reference_bar_closes_late(
    tmp_path, model_root, caplog
):
    """Stream bars -> CandleBuilder -> ContextBuilder -> provider -> lifecycle, end to end.

    Each symbol's bars arrive independently.  AAA's minute-N bar arrives (closing N-1) before
    SPY's, so the first evaluation of the rally bar lacks the same-minute reference.  When
    SPY's bar closes it, the waiting symbol is re-evaluated exactly once and enters.
    """
    from mike_trade.app import AlpacaProductionMarketData
    from mike_trade.market_data.alpaca_stream import StreamBar, StreamQuote

    caplog.set_level(logging.INFO, logger="mike_trade.model")

    clock = Clock(datetime(2026, 10, 12, 13, 31, tzinfo=UTC))
    market = AlpacaProductionMarketData(
        clock, timedelta(seconds=90), feed="iex", warmup_bars=20, environ={}
    )
    ledger = BarVwapLedger()
    service = build_inference_service(model_root, references("xgboost_probability_buy"), clock.now)
    provider = ModelInputProvider(service, market.candles, ledger)
    market.bind_bar_vwaps(ledger)
    market.bind_model_reference_symbols(("SPY",))

    class Scheduler:
        def on_candle_closed(self, symbol, timeframe, end):
            return [object()]

    runtime, shadow = await start(tmp_path, clock, provider, [model("xgboost_probability_buy")], [])
    try:
        market._scheduler = Scheduler()
        market._lifecycle = runtime.lifecycle
        market._trading_symbols = ("AAA",)
        base = datetime(2026, 10, 12, 13, 30, tzinfo=UTC)  # 09:30 New York

        async def deliver(symbol: str, index: int, close: str, previous: str) -> None:
            start_ts = base + index * MINUTE
            clock.value = start_ts + MINUTE + timedelta(seconds=2)
            value, prior = D(close), D(previous)
            quote = StreamQuote(
                symbol,
                clock.value,
                clock.value,
                "iex",
                value - D("0.01"),
                D("100"),
                value + D("0.01"),
                D("100"),
            )
            market.cache.store_event(quote, source="stream")
            await market._on_bar(
                StreamBar(
                    symbol,
                    start_ts,
                    clock.value,
                    "iex",
                    prior,
                    max(value, prior) + D("0.05"),
                    min(value, prior) - D("0.05"),
                    value,
                    D("100000"),
                    value,
                    10,
                )
            )

        closes = ["100"] * 40 + ["100.30", "100.35"]
        previous = "100"
        process = runtime.lifecycle.process
        calls: list[str] = []

        async def counting(contexts):
            calls.extend(contexts)
            return await process(contexts)

        runtime.lifecycle.process = counting  # type: ignore[method-assign]
        for index, close in enumerate(closes):
            await deliver("AAA", index, close, previous)  # AAA first: SPY lags every minute
            if index < len(closes) - 1 and index != 41:
                await deliver("SPY", index, "400", "400")
            previous = close
            if index == 40:
                assert not orders(shadow, OrderPurpose.ENTRY)
        # AAA's bar 41 closed AAA bar 40 (the rally bar) but SPY's bar 40 was still open
        assert not orders(shadow, OrderPurpose.ENTRY)
        assert runtime.lifecycle._reference_waits  # recorded, not forgotten
        before = len(calls)
        await deliver("SPY", 41, "400", "400")  # SPY bar 41 closes SPY bar 40
        assert len(calls) == before + 1  # exactly one re-evaluation of the waiting symbol
        (entry,) = orders(shadow, OrderPurpose.ENTRY)
        assert entry.symbol == "AAA"
        assert not runtime.lifecycle._reference_waits  # consumed once
        await deliver("SPY", 42, "400", "400")
        assert len(calls) == before + 1  # a later reference bar does not re-run it again

        # BACKTEST and the live stream path give the model identical inputs and outputs
        import test_model_backtest as backtest

        replay = await asyncio.to_thread(
            backtest.run,
            tmp_path / "replay",
            model_root,
            {"AAA": backtest.events("AAA", closes), "SPY": backtest.spy_flat(len(closes))},
            ("xgboost_probability_buy",),
            backtest.pins(xgboost_probability_buy=""),
        )
        replayed = next(
            row
            for row in backtest.audit_rows(replay, "xgboost_probability_buy")
            if row["signal_id"]
        )["audit"]["prediction"]
        live = next(
            record.model_audit["prediction"]
            for record in caplog.records
            if record.getMessage() == "Model strategy signal"
        )
        for key in (
            "probabilities",
            "artifact_sha256",
            "feature_schema_sha256",
            "feed",
            "observation_end",
            "horizon_bars",
        ):
            if key == "observation_end":  # same wall-clock minute-of-day, different dates
                assert live[key][11:] == replayed[key][11:]
            else:
                assert live[key] == replayed[key], key
    finally:
        await runtime.stop()


def test_example_configuration_is_valid_and_pins_every_model_explicitly():
    from mike_trade.app import load_app_settings
    from mike_trade.controls.config_commands import load_effective_config

    path = Path(__file__).parents[2] / "config" / "model_strategies.example.yaml"
    effective = load_effective_config(path, environ={})
    assert effective["service"]["model_root"] == "research/models"
    settings = load_app_settings(path, environ={})
    assert settings.model_root == Path("research/models")
    assert settings.market_status_enabled is True and settings.market_data_feed == "iex"
    plugins = {
        assigned.plugin: dict(assigned.parameters)
        for item in settings.symbol_strategies
        for assigned in (*item.buys, *item.sells)
        if "probability" in assigned.plugin
    }
    assert set(plugins) == {
        "xgboost_probability_buy",
        "lightgbm_probability_buy",
        "xgboost_probability_sell",
        "lightgbm_probability_sell",
    }
    reg = registry()
    for plugin, parameters in plugins.items():
        validated = reg.validate_parameters(plugin, parameters)
        assert validated["model_version"] == "1.0.0" and validated["model_id"]
        assert "latest" not in str(validated["model_version"])
    # AAPL pairs different algorithms; MSFT runs two entry models and two exits side by side
    by_symbol = {item.symbol: item for item in settings.symbol_strategies}
    assert [a.plugin for a in by_symbol["AAPL"].buys] == ["xgboost_probability_buy"]
    assert [a.plugin for a in by_symbol["AAPL"].sells] == ["lightgbm_probability_sell"]
    assert {a.plugin for a in by_symbol["MSFT"].buys} == {
        "lightgbm_probability_buy",
        "xgboost_probability_buy",
    }
    assert {a.plugin for a in by_symbol["MSFT"].sells} == {
        "xgboost_probability_sell",
        "vwap_breakdown",
    }
