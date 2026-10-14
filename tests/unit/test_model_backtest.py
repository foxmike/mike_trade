"""BACKTEST integration: pinned XGBoost/LightGBM artifacts through the real runner.

Real native tiny models (``model_fixtures``) drive the four registered strategies through the
unchanged lifecycle: entry/exit arbitration, minimum hold, protective stops, market-status
entry blocking and failure isolation.  Synthetic bars only; nothing touches a broker.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from model_fixtures import build_model
from scripted_strategies import build_registry

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestResult, BacktestRunner
from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.modeling.serving import build_inference_service
from mike_trade.strategies.buy.lightgbm_probability_buy import LightgbmProbabilityBuy
from mike_trade.strategies.buy.xgboost_probability_buy import XgboostProbabilityBuy
from mike_trade.strategies.sell.lightgbm_probability_sell import LightgbmProbabilitySell
from mike_trade.strategies.sell.xgboost_probability_sell import XgboostProbabilitySell

OPEN = datetime(2026, 3, 10, 13, 30, tzinfo=UTC)  # 09:30 ET, regular session, development period
MINUTE = timedelta(minutes=1)
IEX = DataProvenance("synthetic", "IEX", False, "r1")
SIP = DataProvenance("synthetic", "SIP", True, "r1")
FEED = FeedProvenance("replay", "IEX", "synthetic", False)
SIP_FEED = FeedProvenance("replay", "SIP", "synthetic", True)
STOP = {"protective_stop": {"type": "native_trailing", "trail_percent": "1.5"}}

# 35 quiet minutes (the strategies' warm-up), then a rally: the BUY models see strong
# positive 1-minute returns from minute 35 on.  The later reversal gaps through a stop.
QUIET = ["100"] * 35
RALLY = [str(D("100") + D("0.30") * i) for i in range(1, 11)]  # 100.3 .. 103.0
CRASH = ["101.0"] * 4


@pytest.fixture(scope="module")
def model_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("backtest-models")
    for algorithm in ("xgboost", "lightgbm"):
        for role in ("buy", "sell"):
            build_model(root, algorithm=algorithm, role=role, model_id=f"{algorithm}_{role}_m")
    return root


def pins(**selected: str) -> dict[str, dict[str, str]]:
    names = {
        "xgboost_probability_buy": "xgboost_buy_m",
        "lightgbm_probability_buy": "lightgbm_buy_m",
        "xgboost_probability_sell": "xgboost_sell_m",
        "lightgbm_probability_sell": "lightgbm_sell_m",
    }
    return {
        strategy: {"model_id": names[strategy], "model_version": "1.0.0", **extra}
        for strategy, extra in ({key: {} for key in selected} | {}).items()
    }


def events(
    symbol: str,
    closes: list[str],
    *,
    vwap: bool = True,
    provenance: FeedProvenance = FEED,
    start: datetime = OPEN,
    volume: str = "100000",
) -> list[ReplayEvent]:
    out = []
    previous = D(closes[0])
    for index, text in enumerate(closes):
        close = D(text)
        begin = start + index * MINUTE
        high = max(previous, close) + D("0.05")
        low = min(previous, close) - D("0.05")
        out.append(
            ReplayEvent.bar(
                Candle(
                    symbol,
                    Timeframe.M1,
                    begin,
                    begin + MINUTE,
                    previous,
                    high,
                    low,
                    close,
                    D(volume),
                    provenance,
                    (close if vwap else None),
                )
            )
        )
        previous = close
    return out


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


def run(
    tmp_path: Path,
    model_root: Path | None,
    bars: dict[str, list[ReplayEvent]],
    strategy_ids: tuple[str, ...],
    params: dict,
    *,
    config: dict | None = None,
    provenance: DataProvenance = IEX,
    symbols: tuple[str, ...] = ("AAA",),
    service=None,
    with_service: bool = True,
) -> BacktestResult:
    all_events = [event for series in bars.values() for event in series]
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(all_events, provenance, clock=clock, seed=3)
    sim = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    request = BacktestRequest(
        symbols=symbols,
        window_start=OPEN,
        window_end=OPEN + timedelta(hours=6),
        period_role=PeriodRole.RESEARCH,
        config=config or {},
        seed=3,
        starting_capital=D("100000"),
        strategy_ids=strategy_ids,
        strategy_params=params,
        context_history_bars=80,
        result_directory=tmp_path,
    )

    def factory(evaluation, context, _request):
        buy = evaluation.side is StrategyRole.BUY
        return OrderIntent(
            intent_id=f"i-{evaluation.signal_id}",
            client_order_id=f"c-{evaluation.signal_id}",
            account_scope_id="backtest:test",
            cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol,
            side=OrderSide.BUY if buy else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if buy else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=context.as_of,
            quantity=D("10") if buy else sim.held_quantity(evaluation.symbol),
        )

    if service is None and with_service and model_root is not None:
        references = []
        for strategy_id in strategy_ids:
            cls = registry().get(strategy_id)
            if hasattr(cls, "model_reference"):
                validated = cls.spec.parameter_schema.validate(params[strategy_id])
                references.append(cls.model_reference(validated))
        service = build_inference_service(model_root, references, clock.now)
    runner = BacktestRunner(
        registry(), feed, clock, sim, intent_factory=factory, model_service=service
    )
    return asyncio.run(runner.run(request))


def fills(result: BacktestResult, side: str) -> list[dict]:
    return [fill for fill in result.fills if fill["side"] == side]


def spy_flat(count: int) -> list[ReplayEvent]:
    return events("SPY", ["400"] * count)


def audit_rows(result: BacktestResult, strategy: str) -> list[dict]:
    return [
        {**row, "audit": json.loads(row["model_audit"])}
        for row in result.evaluations
        if row["strategy"] == strategy and row.get("model_audit")
    ]


TAPE = QUIET + RALLY + CRASH
# A short, single-cycle rally: entry fills at 100.3; at 100.9 the actual position is +0.6%.
SHORT_TAPE = [*QUIET, "100.3", "100.6", "100.9", *["100.9"] * 8]
LONG_HOLD_TAPE = [*QUIET, "100.3", "100.6", "100.9", *["100.9"] * 25]


# ------------------------------------------------------------------ entries


@pytest.mark.parametrize("algorithm", ["xgboost", "lightgbm"])
def test_model_buy_enters_once_on_the_rally_and_the_protective_stop_governs(
    tmp_path, model_root, algorithm
):
    strategy = f"{algorithm}_probability_buy"
    result = run(
        tmp_path,
        model_root,
        {"AAA": events("AAA", TAPE), "SPY": spy_flat(len(TAPE))},
        (strategy,),
        pins(**{strategy: ""}),
        config=STOP,
    )
    assert len(fills(result, "BUY")) == 1
    entry = fills(result, "BUY")[0]
    assert entry["strategy"] == strategy and entry["quantity"] == D("10")
    # NEXT_BAR_OPEN: the signal bar closed at 100.3, so the fill is at the following open
    assert entry["price"] == D("100.3")
    assert result.cycles[0]["entry_strategy"] == strategy
    (signal,) = [s for s in result.signals if s["strategy"] == strategy]
    audit = json.loads(
        next(r for r in audit_rows(result, strategy) if r["signal_id"])["model_audit"]
    )
    assert audit["decision"] == "SIGNAL"
    assert audit["prediction"]["algorithm"] == algorithm
    assert audit["prediction"]["probabilities"]["1"] > 0.5
    assert audit["prediction"]["observation_end"] == (OPEN + 36 * MINUTE).isoformat()
    # no stop-protection bypass: the stop exits the position, not a model
    assert any(f["purpose"] == "PROTECTIVE_STOP" for f in result.fills)
    assert not fills(result, "SELL")[0]["strategy"]
    assert signal["bar_time"] == OPEN + 35 * MINUTE


def test_model_buy_declines_on_quiet_and_falling_tapes(tmp_path, model_root):
    strategy = "xgboost_probability_buy"
    for name, tape in (
        ("flat", ["100"] * 45),
        ("falling", ["100"] * 35 + [str(D("100") - D("0.3") * i) for i in range(1, 11)]),
    ):
        result = run(
            tmp_path / name,
            model_root,
            {"AAA": events("AAA", tape), "SPY": spy_flat(len(tape))},
            (strategy,),
            pins(**{strategy: ""}),
        )
        assert not result.fills and not result.signals
        rows = audit_rows(result, strategy)
        assert rows
        assert {row["audit"]["reason"] for row in rows} == {
            "FAVORABLE_NOT_PREFERRED"
        }


def test_evaluations_are_deterministic_and_the_manifest_records_model_lineage(tmp_path, model_root):
    strategy = "lightgbm_probability_buy"
    bars = {"AAA": events("AAA", TAPE), "SPY": spy_flat(len(TAPE))}
    first = run(tmp_path / "a", model_root, bars, (strategy,), pins(**{strategy: ""}), config=STOP)
    second = run(tmp_path / "b", model_root, bars, (strategy,), pins(**{strategy: ""}), config=STOP)
    for name in (
        "evaluations",
        "signals",
        "intents",
        "decisions",
        "fills",
        "cycles",
        "equity_curve",
    ):
        assert getattr(first, name) == getattr(second, name), name  # replay is deterministic
    assert first.manifest.config_hash == second.manifest.config_hash
    lineage = first.manifest.configuration["model_lineage"]
    assert len(lineage) == 1
    (item,) = lineage
    assert item["model"] == "lightgbm_buy_m@1.0.0" and item["role"] == "buy"
    assert len(item["artifact_sha256"]) == 64
    # 2026-03-10 lies inside the model's development period: the run is in-sample
    assert item["window_overlaps_training"] is True


def test_buy_is_unavailable_not_fabricated_without_bar_vwap(tmp_path, model_root):
    strategy = "xgboost_probability_buy"
    result = run(
        tmp_path,
        model_root,
        {"AAA": events("AAA", TAPE, vwap=False), "SPY": spy_flat(len(TAPE))},
        (strategy,),
        pins(**{strategy: ""}),
    )
    assert not result.fills
    reasons = {row["audit"]["reason"] for row in audit_rows(result, strategy)}
    assert reasons == {"PREDICTION_UNAVAILABLE:VWAP_UNAVAILABLE"}


def test_missing_reference_series_blocks_only_the_model_strategy(tmp_path, model_root):
    ids = ("xgboost_probability_buy", "buy_alpha")
    params = pins(xgboost_probability_buy="") | {"buy_alpha": {"fire_at": 38}}
    # no SPY events: the model has no reference series; the technical BUY is unaffected
    result = run(tmp_path, model_root, {"AAA": events("AAA", TAPE)}, ids, params)
    assert len(fills(result, "BUY")) == 1
    assert fills(result, "BUY")[0]["strategy"] == "buy_alpha"
    reasons = {row["audit"]["reason"] for row in audit_rows(result, "xgboost_probability_buy")}
    assert reasons == {"PREDICTION_UNAVAILABLE:REFERENCE_BAR_MISSING"}


def test_reference_coverage_does_not_depend_on_symbol_spelling(tmp_path, model_root):
    """A ticker sorting before SPY sees the same-minute SPY bar, exactly like one after it."""
    strategy = "lightgbm_probability_buy"
    outcomes = {}
    for symbol in ("AAA", "ZZZ"):
        bars = {symbol: events(symbol, TAPE), "SPY": spy_flat(len(TAPE))}
        result = run(
            tmp_path / symbol,
            model_root,
            bars,
            (strategy,),
            pins(**{strategy: ""}),
            symbols=(symbol,),
            config=STOP,
        )
        outcomes[symbol] = [
            (row["bar_time"] - OPEN, row["audit"].get("favorable_probability_bps"))
            for row in audit_rows(result, strategy)
        ]
    assert outcomes["AAA"] == outcomes["ZZZ"] and outcomes["AAA"]


# ------------------------------------------------------------------ exits with actual entry fill


@pytest.mark.parametrize("algorithm", ["xgboost", "lightgbm"])
def test_model_sell_receives_the_actual_entry_fill_and_exits_a_winner(
    tmp_path, model_root, algorithm
):
    buy, sell = f"{algorithm}_probability_buy", f"{algorithm}_probability_sell"
    ids = (buy, sell)
    result = run(
        tmp_path,
        model_root,
        {"AAA": events("AAA", SHORT_TAPE), "SPY": spy_flat(len(SHORT_TAPE))},
        ids,
        pins(**{buy: "", sell: ""}),
        config=STOP,
    )
    (entry,) = fills(result, "BUY")
    exits = fills(result, "SELL")
    assert len(exits) == 1 and exits[0]["strategy"] == sell
    assert exits[0]["exit_reason"] == "STRATEGY_SELL"
    assert exits[0]["quantity"] == entry["quantity"]  # full exit, no partial liquidation
    sell_rows = audit_rows(result, sell)
    assert sell_rows
    signal_row = next(row for row in sell_rows if row["signal_id"])
    position = signal_row["audit"]["position"]
    assert D(position["entry_price"]) == entry["price"]  # the ACTUAL fill, not a model entry
    assert position["entry_time"] == entry["time"].isoformat()
    features = signal_row["audit"]["prediction"]["position_features"]
    assert features["position_unrealized_return"] == pytest.approx(
        float(D(str(signal_row["trigger_bar_ohlcv"]["close"])) / entry["price"] - 1), abs=1e-7
    )
    earlier = [row for row in sell_rows if row["bar_time"] < signal_row["bar_time"]]
    assert all(row["audit"]["reason"] in {"HOLD_PREFERRED", "SIGNAL"} for row in earlier)


def test_sell_strategy_is_inert_without_a_position(tmp_path, model_root):
    sell = "xgboost_probability_sell"
    result = run(
        tmp_path,
        model_root,
        {"AAA": events("AAA", ["100"] * 45), "SPY": spy_flat(45)},
        ("buy_alpha", sell),
        {"buy_alpha": {"fire_at": 300}} | pins(**{sell: ""}),
    )
    assert not result.fills and not result.signals
    assert not audit_rows(result, sell)  # flat: advance-only, no inference, no audit noise


# ------------------------------------------------------------------ mixed collections


def test_mixed_algorithm_and_technical_collections(tmp_path, model_root):
    scenarios = {
        "E_xgb_buy_lgbm_sell": (("xgboost_probability_buy", "lightgbm_probability_sell"), {}),
        "G_technical_buy_lgbm_sell": (
            ("buy_alpha", "lightgbm_probability_sell"),
            {"buy_alpha": {"fire_at": 36}},
        ),
        "F_technical_buy_xgb_sell": (
            ("buy_alpha", "xgboost_probability_sell"),
            {"buy_alpha": {"fire_at": 36}},
        ),
        "H_xgb_buy_technical_sell": (
            ("xgboost_probability_buy", "sell_alpha"),
            {"sell_alpha": {"fire_at": 40}},
        ),
    }
    for name, (ids, extra) in scenarios.items():
        params = {
            sid: dict(p) for sid, p in pins(**{s: "" for s in ids if "probability" in s}).items()
        }
        params |= extra
        result = run(
            tmp_path / name,
            model_root,
            {"AAA": events("AAA", SHORT_TAPE), "SPY": spy_flat(len(SHORT_TAPE))},
            ids,
            params,
            config=STOP,
        )
        assert len(fills(result, "BUY")) == 1, name
        buy = fills(result, "BUY")[0]["strategy"]
        sell = [f for f in fills(result, "SELL") if f["exit_reason"] == "STRATEGY_SELL"]
        assert buy == ids[0], name
        assert len(sell) == 1 and sell[0]["strategy"] == ids[1], name


def test_simultaneous_buy_signals_resolve_through_the_existing_arbitration(tmp_path, model_root):
    ids = ("xgboost_probability_buy", "lightgbm_probability_buy", "buy_alpha")
    params = pins(xgboost_probability_buy="", lightgbm_probability_buy="") | {
        "buy_alpha": {"fire_at": 36}
    }
    bars = {"AAA": events("AAA", TAPE), "SPY": spy_flat(len(TAPE))}
    result = run(tmp_path / "a", model_root, bars, ids, params, config=STOP)
    assert len(fills(result, "BUY")) == 1  # never two entries
    entry = next(d for d in result.decisions if d["kind"] == "ENTRY")
    reasons = {r["strategy_id"]: r["reason_codes"][0] for r in entry["records"]}
    assert set(reasons) == set(ids)
    assert list(reasons.values()).count("SELECTED") == 1
    assert set(reasons.values()) <= {"SELECTED", "SUPERSEDED_BY_TIE_BREAK"}
    reordered = run(tmp_path / "b", model_root, bars, tuple(reversed(ids)), params, config=STOP)
    assert reordered.fills == result.fills


def test_model_sell_respects_the_existing_minimum_hold_rule(tmp_path, model_root):
    ids = ("xgboost_probability_buy", "xgboost_probability_sell")
    result = run(
        tmp_path,
        model_root,
        {"AAA": events("AAA", LONG_HOLD_TAPE), "SPY": spy_flat(len(LONG_HOLD_TAPE))},
        ids,
        pins(**{i: "" for i in ids}),
        config={**STOP, "minimum_hold_seconds": 600},
    )
    (entry,) = fills(result, "BUY")
    (exit_fill,) = fills(result, "SELL")
    assert exit_fill["time"] - entry["time"] >= timedelta(minutes=10)
    exits = [d for d in result.decisions if d["kind"] == "EXIT"]
    rejected = [d for d in exits if d["records"][0]["outcome"] == "REJECTED"]
    assert rejected and all(
        d["records"][0]["reason_codes"] == ["MIN_HOLD_ACTIVE"] for d in rejected
    )


def test_multiple_sell_strategies_close_once(tmp_path, model_root):
    ids = (
        "xgboost_probability_buy",
        "xgboost_probability_sell",
        "lightgbm_probability_sell",
        "sell_alpha",
    )
    params = pins(
        xgboost_probability_buy="", xgboost_probability_sell="", lightgbm_probability_sell=""
    ) | {"sell_alpha": {"fire_at": 38}}
    result = run(
        tmp_path,
        model_root,
        {"AAA": events("AAA", SHORT_TAPE), "SPY": spy_flat(len(SHORT_TAPE))},
        ids,
        params,
        config=STOP,
    )
    sells = [f for f in fills(result, "SELL") if f["exit_reason"] == "STRATEGY_SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == fills(result, "BUY")[0]["quantity"]
    exit_decision = next(d for d in result.decisions if d["kind"] == "EXIT")
    outcomes = [r["outcome"] for r in exit_decision["records"]]
    assert outcomes.count("SELECTED") == 1


# ------------------------------------------------------------------ risk controls stay in force


def test_protective_stop_outranks_a_model_that_prefers_to_hold(tmp_path, model_root):
    buy, sell = "xgboost_probability_buy", "lightgbm_probability_sell"
    never_exit = {
        "decision_mode": "THRESHOLD",
        "minimum_exit_probability_bps": 10000,
    }  # threshold mode makes the model always hold
    tape = QUIET + RALLY + ["97.0"] * 5  # a 6% gap down through the trailing stop
    result = run(
        tmp_path,
        model_root,
        {"AAA": events("AAA", tape), "SPY": spy_flat(len(tape))},
        (buy, sell),
        pins(**{buy: "", sell: ""}) | {sell: {**pins(**{sell: ""})[sell], **never_exit}},
        config=STOP,
    )
    exits = fills(result, "SELL")
    assert len(exits) == 1
    assert exits[0]["purpose"] == "PROTECTIVE_STOP"
    assert not [f for f in exits if f["exit_reason"] == "STRATEGY_SELL"]


def test_market_crash_blocks_a_model_entry(tmp_path, model_root):
    strategy = "xgboost_probability_buy"
    count = len(TAPE) + 10
    tape = QUIET + RALLY + ["103.0"] * 10
    crash_path = ["500"] * 20 + [str(D("500") - D("4") * i) for i in range(1, count - 19)]
    refs = {
        "SPY": events("SPY", crash_path),
        "QQQ": events("QQQ", [str(D(p) * D("0.8")) for p in crash_path]),
        "IWM": events("IWM", [str(D(p) * D("0.4")) for p in crash_path]),
    }
    blocked = run(
        tmp_path / "blocked",
        model_root,
        {"AAA": events("AAA", tape), **refs},
        (strategy,),
        pins(**{strategy: ""}),
        config={**STOP, "market_status_enabled": True},
    )
    assert not fills(blocked, "BUY"), "a crash must block the model entry"
    refused = [d for d in blocked.decisions if d["kind"] == "ENTRY_REFUSED"]
    assert refused
    assert {(d["strategy_id"], d["reason"], d["market_state"]) for d in refused} == {
        (strategy, "MARKET_CRASH_ACUTE_RISK", "CRASH")
    }
    assert blocked.signals  # the model did signal; the existing market gate refused it
    unblocked = run(
        tmp_path / "unblocked",
        model_root,
        {"AAA": events("AAA", tape), "SPY": spy_flat(count)},
        (strategy,),
        pins(**{strategy: ""}),
        config=STOP,
    )
    assert fills(unblocked, "BUY")


# ------------------------------------------------------------------ fail-closed configuration


def test_runner_refuses_model_strategies_without_a_model_service(tmp_path, model_root):
    strategy = "xgboost_probability_buy"
    with pytest.raises(ValueError, match="model_service"):
        run(
            tmp_path,
            model_root,
            {"AAA": events("AAA", TAPE), "SPY": spy_flat(len(TAPE))},
            (strategy,),
            pins(**{strategy: ""}),
            with_service=False,
        )


def test_runner_refuses_an_unloaded_pin_and_a_feed_mismatch(tmp_path, model_root):
    strategy = "xgboost_probability_buy"
    other = "lightgbm_probability_buy"
    service = build_inference_service(
        model_root,
        [
            XgboostProbabilityBuy.model_reference(
                XgboostProbabilityBuy.spec.parameter_schema.validate(
                    pins(**{strategy: ""})[strategy]
                )
            )
        ],
        lambda: OPEN,
    )
    bars = {"AAA": events("AAA", TAPE), "SPY": spy_flat(len(TAPE))}
    with pytest.raises(ValueError, match="not loaded"):
        run(tmp_path / "a", model_root, bars, (other,), pins(**{other: ""}), service=service)
    sip_bars = {
        "AAA": events("AAA", TAPE, provenance=SIP_FEED),
        "SPY": events("SPY", ["400"] * len(TAPE), provenance=SIP_FEED),
    }
    with pytest.raises(ValueError, match="feeds are never mixed"):
        run(
            tmp_path / "b",
            model_root,
            sip_bars,
            (strategy,),
            pins(**{strategy: ""}),
            provenance=SIP,
            service=service,
        )


def test_existing_technical_backtests_are_unchanged_by_the_model_hooks(tmp_path):
    result = run(
        tmp_path,
        None,
        {"AAA": events("AAA", ["100"] * 20)},
        ("buy_alpha", "sell_alpha"),
        {"buy_alpha": {"fire_at": 3}, "sell_alpha": {"fire_at": 7}},
        with_service=False,
    )
    assert len(fills(result, "BUY")) == 1 and len(fills(result, "SELL")) == 1
    assert "model_lineage" not in result.manifest.configuration
    assert all("model_audit" not in row for row in result.evaluations)
