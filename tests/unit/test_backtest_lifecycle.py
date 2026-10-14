"""Historical lifecycle parity: strategy collections, arbitration and protective stops.

Deterministic synthetic bars and scripted strategy doubles only; no network,
credentials, broker or stored market data.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_registry

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.commands import BacktestResultsIndex
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import (
    BacktestRequest,
    BacktestResult,
    BacktestRunner,
    compute_metrics,
)
from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)

OPEN = datetime(2025, 3, 10, 13, 30, tzinfo=UTC)  # 09:30 ET
MIN = timedelta(minutes=1)
PROV = DataProvenance("synthetic", "IEX", False, "r1")
FEED = FeedProvenance("replay", "IEX", "synthetic", False)

Bar = tuple[str, str, str, str]  # open, high, low, close


def _flat(price: str = "100") -> Bar:
    return (price, price, price, price)


def _events(symbol: str, bars: list[Bar], volumes: dict[int, str] | None = None) -> list[ReplayEvent]:
    events = []
    for index, (o, h, low, c) in enumerate(bars):
        start = OPEN + index * MIN
        events.append(
            ReplayEvent.bar(
                Candle(symbol, Timeframe.M1, start, start + MIN, D(o), D(h), D(low), D(c),
                       D((volumes or {}).get(index, "100000")), FEED)
            )
        )
    return events


def _intent_factory(sim: HistoricalExecutionSimulator, quantity: str = "10"):
    def factory(evaluation, context, request):  # noqa: ANN001
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
            quantity=D(quantity) if buy else sim.held_quantity(evaluation.symbol),
        )

    return factory


def _run_sync(
    bars_by_symbol: dict[str, list[Bar]],
    strategy_ids: tuple[str, ...] = (),
    *,
    assumptions: ExecutionAssumptions | None = None,
    quantity: str = "10",
    params: dict | None = None,
    symbol_strategies: dict | None = None,
    symbol_params: dict | None = None,
    config: dict | None = None,
    result_dir: Path,
    seed: int = 3,
    volumes: dict[int, str] | None = None,
) -> BacktestResult:
    events = [e for sym, bars in bars_by_symbol.items() for e in _events(sym, bars, volumes)]
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(events, PROV, clock=clock, seed=seed)
    sim = HistoricalExecutionSimulator(feed, clock, assumptions or ExecutionAssumptions())
    request = BacktestRequest(
        symbols=tuple(bars_by_symbol),
        window_start=OPEN,
        window_end=OPEN + timedelta(hours=6),
        period_role=PeriodRole.RESEARCH,
        config=config or {},
        seed=seed,
        starting_capital=D("100000"),
        strategy_ids=strategy_ids,
        strategy_params=params or {},
        symbol_strategies=symbol_strategies or {},
        symbol_strategy_params=symbol_params or {},
        context_history_bars=50,
        result_directory=result_dir,
    )
    runner = BacktestRunner(
        build_registry(), feed, clock, sim, intent_factory=_intent_factory(sim, quantity)
    )
    return asyncio.run(runner.run(request))


def _fills(result: BacktestResult, side: str) -> list[dict]:
    return [f for f in result.fills if f["side"] == side]


def _quiet(n: int) -> list[Bar]:
    return [_flat()] * n


# ---------------------------------------------------------------- collections


def test_one_buy_and_one_sell_strategy(tmp_path):
    result = _run_sync(
        {"AAA": _quiet(12)},
        ("buy_alpha", "sell_alpha"),
        params={"buy_alpha": {"fire_at": 3}, "sell_alpha": {"fire_at": 7}},
        result_dir=tmp_path,
    )
    buys, sells = _fills(result, "BUY"), _fills(result, "SELL")
    assert len(buys) == 1 and len(sells) == 1
    assert buys[0]["purpose"] == "ENTRY" and buys[0]["strategy"] == "buy_alpha"
    assert sells[0]["exit_reason"] == "STRATEGY_SELL" and sells[0]["strategy"] == "sell_alpha"
    trade = compute_metrics(result, starting_capital=D("100000")).trades[0]
    assert (trade["entry_strategy"], trade["exit_strategy"], trade["exit_reason"]) == (
        "buy_alpha", "sell_alpha", "STRATEGY_SELL")
    cycle = result.cycles[0]
    assert cycle["entry_strategy"] == "buy_alpha" and cycle["exit_reason"] == "STRATEGY_SELL"


def test_eod_close_enabled_closes_using_next_bar_and_keeps_protective_stop_safe(tmp_path):
    result = _run_sync(
        {"AAA": _quiet(9)},
        ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        config={
            "end_of_day_close": {"enabled": True, "at": "09:35"},
            "protective_stop": {"type": "fixed", "stop_amount": "5"},
        },
        result_dir=tmp_path,
    )
    close = next(fill for fill in result.fills if fill["purpose"] == "EOD_CLOSE")
    entry = next(fill for fill in result.fills if fill["side"] == "BUY")
    order = next(intent for intent in result.intents if intent["purpose"] == "EOD_CLOSE")
    assert any(
        event["event_type"] == "INITIAL_STOP_ESTABLISHED"
        for event in result.protection_events
    )
    assert close["exit_reason"] == "END_OF_DAY"
    assert close["time"] > entry["time"]
    assert close["time"] >= order["time"]
    assert close["evidence"] == "BAR"
    assert result.cycles[0]["closed_at"] == close["time"]


def test_eod_close_disabled_preserves_open_position(tmp_path):
    result = _run_sync(
        {"AAA": _quiet(9)},
        ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        config={"end_of_day_close": {"enabled": False, "at": "09:35"}},
        result_dir=tmp_path,
    )
    assert not [fill for fill in result.fills if fill["purpose"] == "EOD_CLOSE"]
    assert result.cycles[0]["closed_at"] is None


def test_eod_close_with_missing_late_bar_does_not_fabricate_fill(tmp_path):
    # The 09:35 trigger is the final observed event; no later executable bar exists.
    result = _run_sync(
        {"AAA": _quiet(5)},
        ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        config={"end_of_day_close": {"enabled": True, "at": "09:35"}},
        result_dir=tmp_path,
    )
    assert any(intent["purpose"] == "EOD_CLOSE" for intent in result.intents)
    assert not [fill for fill in result.fills if fill["purpose"] == "EOD_CLOSE"]
    assert result.cycles[0]["closed_at"] is None


def test_eod_close_does_not_override_a_configured_technical_sell(tmp_path):
    result = _run_sync(
        {"AAA": _quiet(9)},
        ("buy_alpha", "sell_alpha"),
        params={"buy_alpha": {"fire_at": 2}, "sell_alpha": {"fire_at": 4}},
        config={"end_of_day_close": {"enabled": True, "at": "09:36"}},
        result_dir=tmp_path,
    )
    assert any(fill["exit_reason"] == "STRATEGY_SELL" for fill in result.fills)
    assert not [fill for fill in result.fills if fill["purpose"] == "EOD_CLOSE"]


def test_multiple_buy_strategies_signalling_together_open_one_position(tmp_path):
    ids = ("buy_charlie", "buy_alpha", "buy_bravo", "sell_alpha")
    params = {b: {"fire_at": 3} for b in ids[:3]} | {"sell_alpha": {"fire_at": 8}}
    result = _run_sync({"AAA": _quiet(12)}, ids, params=params, result_dir=tmp_path / "a")
    assert len(_fills(result, "BUY")) == 1  # no duplicate purchase, no scaling
    assert _fills(result, "BUY")[0]["quantity"] == D("10")
    entry = next(d for d in result.decisions if d["kind"] == "ENTRY")
    reasons = {r["strategy_id"]: r["reason_codes"][0] for r in entry["records"]}
    assert reasons == {
        "buy_alpha": "SELECTED",
        "buy_bravo": "SUPERSEDED_BY_TIE_BREAK",
        "buy_charlie": "SUPERSEDED_BY_TIE_BREAK",
    }
    assert _fills(result, "BUY")[0]["strategy"] == "buy_alpha"
    # Arbitration is independent of configuration order.
    reordered = _run_sync(
        {"AAA": _quiet(12)}, tuple(reversed(ids)), params=params, result_dir=tmp_path / "b"
    )
    assert reordered.fills == result.fills
    assert [d for d in reordered.decisions if d["kind"] == "ENTRY"] == [
        d for d in result.decisions if d["kind"] == "ENTRY"
    ]


def test_one_buy_and_multiple_sell_strategies_close_once(tmp_path):
    ids = ("buy_alpha", "sell_charlie", "sell_alpha", "sell_bravo")
    params = {"buy_alpha": {"fire_at": 3}} | {s: {"fire_at": 7} for s in ids[1:]}
    result = _run_sync({"AAA": _quiet(12)}, ids, params=params, result_dir=tmp_path)
    sells = _fills(result, "SELL")
    assert len(sells) == 1 and sells[0]["quantity"] == D("10")  # no oversell, no short
    assert sells[0]["strategy"] == "sell_alpha"
    exit_decision = next(d for d in result.decisions if d["kind"] == "EXIT")
    outcomes = Counter(r["outcome"] for r in exit_decision["records"])
    assert outcomes == {"SELECTED": 1, "REJECTED": 2}
    assert sum(f["quantity"] for f in _fills(result, "BUY")) == sells[0]["quantity"]


def test_multiple_buy_and_multiple_sell_strategies(tmp_path):
    ids = ("buy_bravo", "buy_alpha", "sell_bravo", "sell_alpha")
    params = {
        "buy_alpha": {"fire_at": 3}, "buy_bravo": {"fire_at": 3},
        "sell_alpha": {"fire_at": 7}, "sell_bravo": {"fire_at": 7},
    }
    result = _run_sync({"AAA": _quiet(12)}, ids, params=params, result_dir=tmp_path)
    assert (len(_fills(result, "BUY")), len(_fills(result, "SELL"))) == (1, 1)
    assert result.cycles[0]["entry_strategy"] == "buy_alpha"
    assert result.cycles[0]["exit_strategy"] == "sell_alpha"


def test_sell_strategy_is_independent_of_the_originating_buy(tmp_path):
    # Entry by buy_bravo; exit by an unrelated sell_charlie.
    result = _run_sync(
        {"AAA": _quiet(12)}, ("buy_bravo", "sell_charlie"),
        params={"buy_bravo": {"fire_at": 3}, "sell_charlie": {"fire_at": 7}},
        result_dir=tmp_path,
    )
    assert result.cycles[0]["entry_strategy"] == "buy_bravo"
    assert result.cycles[0]["exit_strategy"] == "sell_charlie"


def test_buy_signals_while_holding_never_scale_the_position(tmp_path):
    result = _run_sync(
        {"AAA": _quiet(20)}, ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3, "fire_every": 1}},  # signals every bar
        result_dir=tmp_path,
    )
    assert len(_fills(result, "BUY")) == 1
    assert len(result.cycles) == 1 and result.cycles[0]["entry_quantity"] == D("10")


def test_buy_state_advances_through_the_bars_it_is_held(tmp_path):
    # buy_alpha's own evaluation count is persisted state. Its 12th evaluation falls on the
    # 12th bar whether or not a position was open in between, so re-entry is on that bar.
    result = _run_sync(
        {"AAA": _quiet(30)}, ("buy_alpha", "sell_alpha"),
        params={"buy_alpha": {"fire_at": 2, "fire_on_nth": 12}, "sell_alpha": {"fire_at": 6}},
        result_dir=tmp_path,
    )
    assert len(_fills(result, "SELL")) == 1
    assert [f["time"] for f in _fills(result, "BUY")] == [
        OPEN + timedelta(minutes=2), OPEN + timedelta(minutes=12)]


def test_sell_state_advances_while_flat_and_never_exits_an_empty_book(tmp_path):
    # sell_alpha's 8th evaluation happens while flat; that signal must be discarded, and
    # the counter must not restart once a position opens.
    result = _run_sync(
        {"AAA": _quiet(30)}, ("buy_alpha", "sell_alpha"),
        params={"buy_alpha": {"fire_at": 10}, "sell_alpha": {"fire_on_nth": 8}},
        result_dir=tmp_path,
    )
    assert len(_fills(result, "BUY")) == 1
    assert _fills(result, "SELL") == []


def test_symbol_without_sell_strategies_still_has_stop_protection(tmp_path):
    bars = _quiet(5) + [("100", "100", "97", "97.5")] + _quiet(4)
    result = _run_sync(
        {"AAA": bars}, ("buy_alpha",), params={"buy_alpha": {"fire_at": 3}},
        config={"protective_stop": {"type": "fixed", "stop_percent": "2"}},
        result_dir=tmp_path,
    )
    sells = _fills(result, "SELL")
    assert len(sells) == 1
    assert sells[0]["exit_reason"] == "PROTECTIVE_STOP" and sells[0]["purpose"] == "PROTECTIVE_STOP"
    assert sells[0]["price"] == D("98.00")  # fixed stop below the 100 entry fill
    assert result.cycles[0]["exit_reason"] == "PROTECTIVE_STOP"


def test_protective_stop_acts_when_strategy_exits_are_not_eligible(tmp_path):
    bars = _quiet(5) + [("100", "100", "97", "97.5")] + _quiet(3)
    result = _run_sync(
        {"AAA": bars}, ("buy_alpha", "sell_alpha"),
        params={"buy_alpha": {"fire_at": 3}, "sell_alpha": {"fire_at": 5, "fire_every": 1}},
        config={
            "protective_stop": {"type": "fixed", "stop_percent": "2"},
            "minimum_hold_seconds": 3600,  # SELL strategy exits are held off
        },
        result_dir=tmp_path,
    )
    sells = _fills(result, "SELL")
    assert [s["exit_reason"] for s in sells] == ["PROTECTIVE_STOP"]
    exit_decisions = [d for d in result.decisions if d["kind"] == "EXIT"]
    assert exit_decisions and all(
        r["reason_codes"] == ["MIN_HOLD_ACTIVE"] for d in exit_decisions for r in d["records"]
    )


def test_stop_covers_the_whole_position_through_partial_entry_fills(tmp_path):
    dip: list[Bar] = [("100", "100", "97", "97.5"), ("97.5", "97.5", "97", "97.2"),
                      ("97.2", "97.2", "97", "97.1")]
    bars = _quiet(9) + dip + _quiet(2)
    result = _run_sync(
        {"AAA": bars}, ("buy_alpha",), params={"buy_alpha": {"fire_at": 3}},
        assumptions=ExecutionAssumptions(participation=D("0.0001")),  # 10 shares per bar
        quantity="25",
        config={"protective_stop": {"type": "fixed", "stop_percent": "2"}},
        result_dir=tmp_path,
    )
    buys = _fills(result, "BUY")
    assert [b["quantity"] for b in buys] == [D("10"), D("10"), D("5")]  # partial fills
    assert len(result.cycles) == 1  # still one cycle, no second entry while filling
    sells = _fills(result, "SELL")
    assert sum(s["quantity"] for s in sells) == D("25")  # the stop covered every share
    assert all(s["exit_reason"] == "PROTECTIVE_STOP" for s in sells)


def test_same_bar_partial_entry_and_stop_rearms_confirmed_remainder(tmp_path):
    bars = (
        _quiet(4)
        + [("100", "100", "97", "98"), ("98", "98", "97", "97.5")]
        + [_flat("97.5")] * 3
    )
    result = _run_sync(
        {"AAA": bars},
        ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        assumptions=ExecutionAssumptions(participation=D("0.002")),  # 200 shares per bar
        quantity="327",
        config={"protective_stop": {"type": "fixed", "stop_percent": "2"}},
        result_dir=tmp_path,
    )

    # On the second entry bar, the remaining 127 BUY shares fill before the old
    # protection consumes the bar's remaining 73-share liquidity.  Re-protection
    # must use the simulator-confirmed 254 shares while lifecycle bookkeeping catches up.
    assert [fill["quantity"] for fill in _fills(result, "BUY")] == [D("200"), D("127")]
    sells = _fills(result, "SELL")
    assert sells[0]["quantity"] == D("73")
    assert sum(fill["quantity"] for fill in sells) == D("327")
    assert all(fill["exit_reason"] == "PROTECTIVE_STOP" for fill in sells)


def test_same_bar_entry_remainder_and_full_stop_fill_reprotects_confirmed_holdings(tmp_path):
    # The campaign failure ("SELL quantity 327 exceeds held 227"): 100 shares are held and
    # fully protected; on the next bar the remaining 227 BUY shares fill and then the
    # protective stop fills completely.  The simulator has committed both fills (held 227)
    # before the lifecycle sees the BUY, whose stale local quantity of 327 once sized the
    # replacement stop.
    bars = _quiet(4) + [("100", "100", "97", "97.5")] + [_flat("97.5")] * 3
    result = _run_sync(
        {"AAA": bars},
        ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        assumptions=ExecutionAssumptions(participation=D("0.002")),
        quantity="327",
        config={"protective_stop": {"type": "fixed", "stop_percent": "2"}},
        result_dir=tmp_path,
        volumes={3: "50000", 4: "200000"},  # participation caps of 100, then 400 shares
    )

    assert [fill["quantity"] for fill in _fills(result, "BUY")] == [D("100"), D("227")]
    sells = _fills(result, "SELL")
    assert sells[0]["quantity"] == D("100")  # the original stop, filled in full
    assert all(fill["exit_reason"] == "PROTECTIVE_STOP" for fill in sells)
    assert sum(fill["quantity"] for fill in sells) == D("327")  # re-protected exactly 227
    assert len(result.cycles) == 1 and result.cycles[0]["closed_at"] is not None


def test_initial_stop_can_exit_on_the_entry_fill_candle(tmp_path):
    bars = _quiet(3) + [("100", "101", "97", "99")] + [_flat("99")] * 3
    result = _run_sync(
        {"AAA": bars},
        ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        config={"protective_stop": {"type": "fixed", "stop_percent": "2"}},
        result_dir=tmp_path,
    )

    (buy,), (sell,) = _fills(result, "BUY"), _fills(result, "SELL")
    assert (buy["time"], buy["price"], buy["quantity"]) == (
        OPEN + 3 * MIN,
        D("100"),
        D("10"),
    )
    assert (sell["time"], sell["price"], sell["quantity"]) == (
        OPEN + 4 * MIN,
        D("98"),
        D("10"),
    )
    assert sell["purpose"] == sell["exit_reason"] == "PROTECTIVE_STOP"
    assert result.cycles[0]["closed_at"] == OPEN + 4 * MIN
    assert compute_metrics(result, starting_capital=D("100000")).trades[0]["gross_pnl"] == D("-20")


@pytest.mark.parametrize("mode", ["native", "stepped"])
def test_entry_bar_with_both_extremes_stops_before_trailing_can_advance(tmp_path, mode):
    stop = (
        {"type": "native_trailing", "trail_percent": "2"}
        if mode == "native"
        else {
            "type": "stepped_trailing",
            "initial_stop_percent": "2",
            "step_trigger_percent": "1",
            "step_percent": "0.5",
        }
    )
    result = _run_sync(
        {"AAA": _quiet(3) + [("100", "110", "97", "105")] + [_flat("105")] * 3},
        ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        config={"protective_stop": stop},
        result_dir=tmp_path,
    )

    (sell,) = _fills(result, "SELL")
    assert sell["price"] == D("98")
    assert [event["event_type"] for event in result.protection_events] == [
        "INITIAL_STOP_ESTABLISHED"
    ]
    assert (
        result.cycles[0]["initial_stop_price"]
        == result.cycles[0]["final_stop_price"]
        == D("98")
    )


def test_surviving_entry_bar_activates_stepped_trailing_for_the_next_bar(tmp_path):
    bars = (
        _quiet(3)
        + [("100", "104", "99", "103")]
        + [("103", "103", "102", "102.2")]
        + [_flat("102.2")] * 2
    )
    result = _run_sync(
        {"AAA": bars},
        ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        config={
            "protective_stop": {
                "type": "stepped_trailing",
                "initial_stop_percent": "1.5",
                "step_trigger_percent": "1",
                "step_percent": "0.5",
            }
        },
        result_dir=tmp_path,
    )

    assert [event["stop_price"] for event in result.protection_events] == [
        D("98.50"),
        D("102.50"),
    ]
    (sell,) = _fills(result, "SELL")
    assert (sell["price"], sell["time"]) == (D("102.50"), OPEN + 5 * MIN)


# ----------------------------------------------------------------- stop modes

_RISE_AND_FALL: list[Bar] = (
    _quiet(5)
    + [("100", "104", "100", "104")]       # new high 104
    + [("103.7", "103.8", "103.5", "103.6")]  # lower high, dip stays above the stop
    + [("103", "103", "102", "102.2")]     # falls through the trailed stop
    + _quiet(3)
)


@pytest.mark.parametrize(
    ("stop", "expected_exit"),
    [
        ({"type": "native_trailing", "trail_percent": "1.5"}, D("102.44")),
        ({"type": "native_trailing", "trail_amount": "1.50"}, D("102.50")),
        ({"type": "stepped_trailing", "initial_stop_percent": "1.5",
          "step_trigger_percent": "1.0", "step_percent": "0.5"}, D("102.50")),
    ],
)
def test_trailing_stop_ratchets_up_and_never_down_protecting_gains(tmp_path, stop, expected_exit):
    result = _run_sync(
        {"AAA": list(_RISE_AND_FALL)}, ("buy_alpha",), params={"buy_alpha": {"fire_at": 3}},
        config={"protective_stop": stop}, result_dir=tmp_path,
    )
    (sell,) = _fills(result, "SELL")
    assert sell["exit_reason"] == "PROTECTIVE_STOP"
    # The stop followed the 104 high (not the entry, not the later lower high)...
    assert sell["price"] == expected_exit
    # ...so the accumulated gain was protected.
    assert sell["price"] > _fills(result, "BUY")[0]["price"]


def test_artifact_preserves_selected_signal_versions_cycles_and_stop_changes(tmp_path):
    result = _run_sync(
        {"AAA": list(_RISE_AND_FALL)}, ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        config={"protective_stop": {"type": "stepped_trailing",
                "initial_stop_percent": "1.5", "step_trigger_percent": "1.0",
                "step_percent": "0.5"}}, result_dir=tmp_path,
    )
    buy = _fills(result, "BUY")[0]
    cycle = result.cycles[0]
    assert buy["cycle_id"] == cycle["cycle_id"]
    assert buy["signal_id"] == cycle["entry_signal_id"]
    assert buy["strategy_version"] == cycle["entry_strategy_version"] == "1.0.0"
    assert cycle["entry_decision"] == "SELECTED"
    assert cycle["protection_mode"] == "TRAILING_STEPPED"
    assert [event["event_type"] for event in result.protection_events] == [
        "INITIAL_STOP_ESTABLISHED", "STOP_MOVED"
    ]
    artifact_dir = tmp_path / "executions" / str(result.manifest.execution_id)
    payload = __import__("json").loads(
        (artifact_dir / f"{result.run_id}.results.json").read_text()
    )
    assert payload["schema_version"] == "2"
    assert payload["execution_id"] == result.manifest.execution_id
    assert result.manifest.started_at is not None
    assert result.manifest.completed_at is not None
    assert result.manifest.created_at != result.manifest.window_start
    assert payload["protection_events"][1]["new_stop_price"] == str(
        result.protection_events[1]["new_stop_price"]
    )


def test_identical_executions_get_distinct_artifacts_and_index_rows(tmp_path):
    args = ({"AAA": [_flat(), _flat(), _flat(), _flat()]}, ("buy_alpha",))
    options = {"params": {"buy_alpha": {"fire_at": 3}}, "result_dir": tmp_path}
    first = _run_sync(*args, **options)
    second = _run_sync(*args, **options)

    assert first.run_id == second.run_id
    assert first.manifest.execution_id
    assert second.manifest.execution_id
    assert first.manifest.execution_id != second.manifest.execution_id
    for result in (first, second):
        execution_dir = tmp_path / "executions" / str(result.manifest.execution_id)
        manifest_path = execution_dir / f"{result.run_id}.manifest.json"
        result_path = execution_dir / f"{result.run_id}.results.json"
        manifest = __import__("json").loads(manifest_path.read_text())
        payload = __import__("json").loads(result_path.read_text())
        assert manifest["run_id"] == payload["run_id"] == result.run_id
        assert manifest["execution_id"] == payload["execution_id"] == result.manifest.execution_id
        assert manifest["completion_status"] == "COMPLETED"
        assert manifest["started_at"] and manifest["completed_at"]

    index = BacktestResultsIndex(tmp_path)
    rows = index.list_runs()
    assert {row["execution_id"] for row in rows} == {
        first.manifest.execution_id, second.manifest.execution_id
    }
    assert index.get(str(second.manifest.execution_id))["run_id"] == first.run_id


def test_native_trailing_artifact_records_actual_simulated_stop_movements(tmp_path):
    result = _run_sync(
        {"AAA": list(_RISE_AND_FALL)}, ("buy_alpha",),
        params={"buy_alpha": {"fire_at": 3}},
        config={"protective_stop": {"type": "native_trailing", "trail_percent": "1.5"}},
        result_dir=tmp_path,
    )
    prices = [event["stop_price"] for event in result.protection_events]
    assert len(prices) >= 2
    assert prices == sorted(prices)
    assert result.protection_events[-1]["protection_mode"] == "TRAILING_NATIVE"


def test_legacy_trailing_percent_key_is_the_native_trailing_alias(tmp_path):
    legacy = _run_sync(
        {"AAA": list(_RISE_AND_FALL)}, ("buy_alpha",), params={"buy_alpha": {"fire_at": 3}},
        config={"protective_trailing_percent": "1.5"}, result_dir=tmp_path / "l",
    )
    explicit = _run_sync(
        {"AAA": list(_RISE_AND_FALL)}, ("buy_alpha",), params={"buy_alpha": {"fire_at": 3}},
        config={"protective_stop": {"type": "native_trailing", "trail_percent": "1.5"}},
        result_dir=tmp_path / "e",
    )
    assert legacy.fills == explicit.fills


def test_protective_stop_is_gtc_and_survives_the_session(tmp_path):
    # Entry late on day one; the next session gaps down through the stop.
    day1 = _quiet(8)
    events = _events("AAA", day1)
    next_open = OPEN + timedelta(days=1)
    events += [
        ReplayEvent.bar(
            Candle("AAA", Timeframe.M1, next_open + i * MIN, next_open + (i + 1) * MIN,
                   D(o), D(h), D(low), D(c), D("100000"), FEED)
        )
        for i, (o, h, low, c) in enumerate([("95", "95", "94", "94.5")] * 2)
    ]
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(events, PROV, clock=clock, seed=3)
    sim = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    request = BacktestRequest(
        ("AAA",), OPEN, OPEN + timedelta(days=2), PeriodRole.RESEARCH,
        {"protective_stop": {"type": "fixed", "stop_percent": "2"}}, 3, D("100000"),
        ("buy_alpha",), strategy_params={"buy_alpha": {"fire_at": 3}},
        context_history_bars=50, result_directory=tmp_path,
    )
    result = asyncio.run(
        BacktestRunner(build_registry(), feed, clock, sim,
                       intent_factory=_intent_factory(sim)).run(request)
    )
    (sell,) = _fills(result, "SELL")
    assert sell["exit_reason"] == "PROTECTIVE_STOP"
    assert sell["price"] == D("95")  # gapped open, filled where the market was


# ----------------------------------------------- per-symbol collections / state


def test_symbols_have_independent_collections_parameters_and_state(tmp_path):
    result = _run_sync(
        {"AAA": _quiet(14), "BBB": _quiet(14)},
        ("buy_alpha", "sell_alpha"),  # default collection (unused: both overridden)
        symbol_strategies={
            "AAA": ("buy_alpha", "buy_bravo", "sell_alpha"),
            "BBB": ("buy_charlie",),  # no SELL strategy at all
        },
        symbol_params={
            "AAA": {"buy_alpha": {"fire_on_nth": 4}, "buy_bravo": {"fire_on_nth": 99},
                    "sell_alpha": {"fire_on_nth": 7}},  # counts flat bars too: 7th bar is held
            "BBB": {"buy_charlie": {"fire_on_nth": 6}},
        },
        result_dir=tmp_path,
    )
    by_symbol = {s: [f for f in result.fills if f["symbol"] == s] for s in ("AAA", "BBB")}
    # State is per (symbol, strategy): each fired on ITS OWN Nth evaluation. Shared
    # state would have fired both symbols' strategies on different bars.
    entry_times = {s: next(f["time"] for f in fs if f["side"] == "BUY") for s, fs in by_symbol.items()}
    assert entry_times["AAA"] == OPEN + 4 * MIN  # signal on bar 4 -> fill at bar 5 open
    assert entry_times["BBB"] == OPEN + 6 * MIN
    assert [f["strategy"] for f in by_symbol["AAA"]] == ["buy_alpha", "sell_alpha"]
    assert [f["side"] for f in by_symbol["BBB"]] == ["BUY"]  # held: no SELL collection
    evaluated = {(e["symbol"], e["strategy"]) for e in result.evaluations}
    assert ("BBB", "sell_alpha") not in evaluated and ("AAA", "buy_charlie") not in evaluated
    assert ("AAA", "buy_bravo") in evaluated  # evaluated while flat only
    assert all(
        e["bar_time"] < OPEN + 5 * MIN for e in result.evaluations
        if (e["symbol"], e["strategy"]) == ("AAA", "buy_alpha") and e["signal_id"]
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"symbol_strategies": {"ZZZ": ("buy_alpha",)}}, "outside the request"),
        ({"symbol_strategies": {"AAA": ("buy_alpha", "buy_alpha")}}, "only once per symbol"),
        ({"symbol_params": {"AAA": {"sell_alpha": {}}}}, "unconfigured strategies"),
        ({"symbol_params": {"AAA": {"buy_alpha": {"bogus": 1}}}}, "unknown parameter"),
    ],
)
def test_collection_configuration_is_strictly_validated(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        _run_sync({"AAA": _quiet(4)}, ("buy_alpha",), result_dir=tmp_path, **kwargs)


def test_unsupported_strategy_timeframe_fails_closed(tmp_path):
    from mike_trade.strategies.sell.rsi_reversal import RsiReversalSell

    registry = build_registry()
    registry.register(RsiReversalSell)
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(_events("AAA", _quiet(4)), PROV, clock=clock, seed=3)
    sim = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    request = BacktestRequest(
        ("AAA",), OPEN, OPEN + timedelta(hours=1), PeriodRole.RESEARCH, {}, 3, D("1000"),
        ("buy_alpha", "rsi_reversal"), result_directory=tmp_path,
    )
    with pytest.raises(ValueError, match="needs 5m bars"):
        asyncio.run(BacktestRunner(registry, feed, clock, sim).run(request))


# ------------------------------------------------------- serial vs parallel


def _job(seed_dir: tuple[int, str]) -> tuple[str, str, tuple]:
    seed, directory = seed_dir
    result = _run_sync(
        {"AAA": list(_RISE_AND_FALL), "BBB": _quiet(12)},
        ("buy_bravo", "buy_alpha", "sell_alpha"),
        params={"buy_alpha": {"fire_at": 3}, "buy_bravo": {"fire_at": 3},
                "sell_alpha": {"fire_at": 9}},
        config={"protective_stop": {"type": "native_trailing", "trail_percent": "1.5"}},
        result_dir=Path(directory), seed=seed,
    )
    return result.run_id, result.results_hash, tuple(f["price"] for f in result.fills)


def test_serial_and_parallel_execution_are_equivalent(tmp_path):
    jobs = [(seed, str(tmp_path / f"s{seed}")) for seed in (1, 2, 3)]
    serial = [_job(job) for job in jobs]
    parallel_jobs = [(seed, str(tmp_path / f"p{seed}")) for seed in (1, 2, 3)]
    with ProcessPoolExecutor(max_workers=2) as pool:
        parallel = list(pool.map(_job, parallel_jobs))
    assert serial == parallel
