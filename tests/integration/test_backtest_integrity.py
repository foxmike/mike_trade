"""Phase-Q backtest integrity verification: offline replay of the ``vwap_reference`` pair.

Runs the production backtest stack end to end -- :class:`BacktestRunner` over
:class:`HistoricalReplayFeed` / :class:`ReplayClock` /
:class:`HistoricalExecutionSimulator` -- with the production ``vwap_bounce``
BUY and ``vwap_breakdown`` SELL strategies resolved through the real strategy
registry from the shipped ``vwap_reference`` pair preset.  Signals are never
hardcoded: the only orders submitted are built by a small test-local intent
factory from real ``StrategyEvaluation`` signals.

Fixtures are bounded synthetic 1-minute regular-session bars built in memory
(one to two symbols, a few dozen bars) with explicit IEX
:class:`DataProvenance`.  No network, no credentials, no live orders: sockets
are blocked, ``ALPACA_*``-style credential variables are poisoned, and this
module makes no profitability claim of any kind.

Current production status: the runner builds each ``MarketContext`` with an
empty indicator mapping and never populates engine-owned eligibility state, so
the VWAP legs cannot emit signals through it (BUY evaluates ``NOT_ELIGIBLE``,
SELL evaluates ``INSUFFICIENT_DATA`` on every bar).  Scenarios that require a
signal are therefore marked ``xfail(strict=True)`` and recorded as open
production defects in ``docs/verification/phase_q_backtest.md``; they fail
today exactly because no signal fires, and they will XPASS loudly if
production ever supplies the missing context.  No production module is edited
by this task.
"""

from __future__ import annotations

import json
import socket
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import (
    AMBIGUOUS_INTRABAR_ORDER,
    ExecutionAssumptions,
    HistoricalExecutionSimulator,
)
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES, compute_fixed_stop_price
from mike_trade.risk.stop_policy import ProtectionMode, ProtectionPolicy
from mike_trade.strategies.base import ReasonCode
from mike_trade.strategies.presets.pairs import (
    FIRST_VALID_EXIT,
    SHADOW_ONLY,
    PairPresetError,
    resolve_pair_preset,
)
from mike_trade.strategies.registry import StrategyRegistry

D = Decimal
MIN = timedelta(minutes=1)
SEED = 20261008
QTY = D("10")
STARTING_CAPITAL = D("100000")

# Regular NYSE Monday session; 09:30 ET == 13:30 UTC (DST in effect).
SESSION_OPEN = datetime(2025, 3, 10, 13, 30, tzinfo=UTC)

#: Explicit provenance for every synthetic bar: IEX, never SIP.
PROVENANCE = DataProvenance(
    source="synthetic", feed="IEX", is_consolidated=False, revision="phase-q-r1"
)
CANDLE_PROVENANCE = FeedProvenance(
    provider="replay", feed="IEX", source="synthetic", is_consolidated=False
)

BUY_ID, SELL_ID = "vwap_bounce", "vwap_breakdown"

POISONED_ENV_VARS = (
    "ALPACA_PAPER_KEY_ID",
    "ALPACA_PAPER_SECRET_KEY",
    "ALPACA_LIVE_KEY_ID",
    "ALPACA_LIVE_SECRET_KEY",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "MIKE_TRADE_ALPACA_PAPER_KEY_ID",
    "MIKE_TRADE_ALPACA_PAPER_SECRET_KEY",
    "MIKE_TRADE_ALPACA_LIVE_KEY_ID",
    "MIKE_TRADE_ALPACA_LIVE_SECRET_KEY",
    "MIKE_TRADE_ALPACA_DATA_KEY_ID",
    "MIKE_TRADE_ALPACA_DATA_SECRET_KEY",
)

# ---------------------------------------------------------------------------
# Fixtures: engineered (but signal-free) price paths.
#
# The VWAP legs read warmed session VWAP from the context indicator service,
# which the production runner never populates, so no path below can produce a
# signal today.  The paths are still engineered as genuine crossing shapes so
# that, once production supplies indicators/eligibility, each scenario's bars
# present the intended trigger without fixture changes:
#   * SELL_PATH: drift down, jump above session VWAP (intended BUY cross),
#     climb, then gap down through it (intended SELL cross).
#   * DUP_PATH: drift down, jump (intended BUY cross), hold high with a second
#     upward jump while already holding (duplicate-buy suppression probe).
#   * AMBIG_PATH: drift down, jump (intended BUY cross), then one wide-range
#     bar whose low crashes through any plausible stop while its high rallies.


def _closes_path(*closes: str) -> list[D]:
    return [D(c) for c in closes]


SELL_PATH = _closes_path(
    "100.00", "99.90", "99.80", "99.70", "99.60", "99.55", "99.50", "99.45",
    "99.40", "99.35", "101.00", "101.20", "101.50", "101.80", "102.00",
    "102.20", "100.00", "99.50", "99.00", "98.70", "98.50", "98.40",
)
DUP_PATH = _closes_path(
    "100.00", "99.90", "99.80", "99.70", "99.60", "99.50", "101.00", "101.10",
    "101.20", "101.30", "101.40", "102.50", "102.60", "102.70",
)
AMBIG_PATH = _closes_path(
    "100.00", "99.90", "99.80", "99.70", "99.60", "99.50", "101.00", "101.20",
    "101.30", "101.30", "101.40", "101.50",
)
#: Index 8 of AMBIG_PATH is replaced by this wide-range bar (o, h, low, c).
AMBIG_WIDE_BAR = (D("101.20"), D("103.00"), D("97.00"), D("101.30"))
FLAT_PATH = _closes_path(*("50.00",) * 12)


def _bars(
    symbol: str,
    closes: list[D],
    *,
    start: datetime = SESSION_OPEN,
    special: dict[int, tuple[D, D, D, D]] | None = None,
) -> list[ReplayEvent]:
    """Build consecutive 1-minute CLOSED bars from a close path.

    Opens chain from the prior close; high/low pad the open-close range by
    0.05 so every bar satisfies low <= open,close <= high.  ``special`` maps a
    bar index to an explicit (open, high, low, close) override.
    """
    events: list[ReplayEvent] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        bar_start = start + index * MIN
        if special and index in special:
            o, h, low, c = special[index]
        else:
            o = previous if index else close
            h, low, c = max(o, close) + D("0.05"), min(o, close) - D("0.05"), close
        events.append(
            ReplayEvent.bar(
                Candle(
                    symbol, Timeframe.M1, bar_start, bar_start + MIN,
                    o, h, low, c, D("1000"), CANDLE_PROVENANCE,
                )
            )
        )
        previous = close
    return events


def _sell_fixture() -> list[ReplayEvent]:
    return _bars("AAA", SELL_PATH) + _bars("BBB", FLAT_PATH)


def _dup_fixture() -> list[ReplayEvent]:
    return _bars("AAA", DUP_PATH)


def _ambig_fixture() -> list[ReplayEvent]:
    return _bars("AAA", AMBIG_PATH, special={8: AMBIG_WIDE_BAR})


# ---------------------------------------------------------------------------
# Harness: real production components, in-memory only.


def _pair_registry() -> tuple[StrategyRegistry, dict, dict]:
    """Resolve ``vwap_reference`` and register both legs; return registry+params."""
    resolution = resolve_pair_preset("vwap_reference", mode="shadow")
    registry = StrategyRegistry()
    registry.register(resolution["buy_cls"])
    registry.register(resolution["sell_cls"])
    return registry, dict(resolution["buy_params"]), dict(resolution["sell_params"])


def _intent_factory(sim: HistoricalExecutionSimulator):
    """Map real strategy signals to broker-neutral intents (test-local glue).

    BUY signals become whole-share DAY market entries with a per-symbol
    idempotent client id, so a repeated BUY signal while holding dedupes to
    the original order instead of creating a duplicate buy.  SELL signals
    become market exits for exactly the confirmed held quantity (the
    production SELL leg is a 100%-remainder exit that never shorts).
    """

    def factory(evaluation, context, request):  # type: ignore[no-untyped-def]
        assert evaluation.is_signal and evaluation.signal_id, "factory sees signals only"
        symbol = context.symbol
        if evaluation.side is StrategyRole.BUY:
            return OrderIntent(
                intent_id=f"phase-q-{evaluation.signal_id}",
                client_order_id=f"phase-q-entry-{symbol}",
                account_scope_id="phase-q-backtest",
                cycle_id=f"phase-q-cycle-{symbol}",
                symbol=symbol,
                side=OrderSide.BUY,
                purpose=OrderPurpose.ENTRY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                created_at=context.as_of,
                quantity=QTY,
            )
        held = sim.held_quantity(symbol)
        if held <= 0:
            raise RuntimeError(f"SELL signal for {symbol} with no held position")
        return OrderIntent(
            intent_id=f"phase-q-{evaluation.signal_id}",
            client_order_id=f"phase-q-exit-{evaluation.signal_id}",
            account_scope_id="phase-q-backtest",
            cycle_id=f"phase-q-cycle-{symbol}",
            symbol=symbol,
            side=OrderSide.SELL,
            purpose=OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=context.as_of,
            quantity=held,
        )

    return factory


async def _run(events: list[ReplayEvent], tmp_path: Path, *, seed: int = SEED):
    """Run the production runner over fresh components; return (result, sim)."""
    registry, buy_params, sell_params = _pair_registry()
    symbols = tuple(sorted({e.symbol for e in events}))
    clock = ReplayClock(min(e.event_time for e in events))
    feed = HistoricalReplayFeed(list(events), PROVENANCE, clock=clock, seed=seed)
    available = [e.available_at for e in feed.events(symbols=symbols)]
    assert available and all(a is not None for a in available)
    start, end = min(available), max(available)
    assert start is not None and end is not None
    sim = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    request = BacktestRequest(
        symbols=tuple(sorted({e.symbol for e in events})),
        window_start=start,
        window_end=end,
        period_role=PeriodRole.RESEARCH,
        config={"mode": "shadow", "pair": "vwap_reference", "phase": "q"},
        seed=seed,
        starting_capital=STARTING_CAPITAL,
        strategy_ids=(BUY_ID, SELL_ID),
        strategy_params={BUY_ID: buy_params, SELL_ID: sell_params},
        result_directory=tmp_path,
    )
    result = await BacktestRunner(
        registry, feed, clock, sim, intent_factory=_intent_factory(sim)
    ).run(request)
    return result, sim


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block sockets and poison credential-style env vars for every test."""

    def _blocked(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network is blocked in the phase-Q offline replay test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    for var in POISONED_ENV_VARS:
        monkeypatch.setenv(var, "PHASE-Q-POISONED-DO-NOT-USE")


# ---------------------------------------------------------------------------
# Passing integrity checks.


async def test_pair_resolves_to_production_legs_shadow_only() -> None:
    resolution = resolve_pair_preset("vwap_reference", mode="shadow")
    preset = resolution["preset"]
    assert preset.preset_id == "vwap_reference"
    assert preset.allowed_modes == SHADOW_ONLY == frozenset({"shadow"})
    assert preset.exit_policy == FIRST_VALID_EXIT
    assert preset.requires_trailing_protection is True
    buy_cls, sell_cls = resolution["buy_cls"], resolution["sell_cls"]
    assert (buy_cls.spec.strategy_id, buy_cls.spec.side) == (BUY_ID, StrategyRole.BUY)
    assert (sell_cls.spec.strategy_id, sell_cls.spec.side) == (SELL_ID, StrategyRole.SELL)
    assert resolution["buy_params"] == dict(preset.buy_params)
    assert resolution["sell_params"] == dict(preset.sell_params)
    for bad_mode in ("paper", "live", "PAPER", ""):
        with pytest.raises(PairPresetError):
            resolve_pair_preset("vwap_reference", mode=bad_mode)
    with pytest.raises(PairPresetError):
        resolve_pair_preset("no-such-pair", mode="shadow")


async def test_runner_replay_evaluates_every_closed_bar_without_signals(
    tmp_path: Path,
) -> None:
    """Characterize the blocking defect: full evaluation coverage, zero signals.

    Every closed bar past warmup is evaluated exactly once per leg (no skipped
    or duplicated triggers), yet no signal can fire: the BUY leg always sees
    ``NOT_ELIGIBLE`` (the runner never sets engine-owned eligibility state)
    and the SELL leg always sees ``INSUFFICIENT_DATA`` (the runner passes an
    empty indicator mapping, so warmed session VWAP is missing).  If
    production ever supplies that context, signals appear and this test fails
    as an intentional tripwire.
    """
    events = _sell_fixture()
    result, sim = await _run(events, tmp_path)
    bars_per_symbol = Counter(e.symbol for e in events if e.kind == ReplayEvent.KIND_BAR)
    by_leg = Counter((e["symbol"], e["strategy"]) for e in result.evaluations)
    for symbol, count in bars_per_symbol.items():
        for leg in (BUY_ID, SELL_ID):
            assert by_leg[(symbol, leg)] == count - 1, (symbol, leg)
    buy_codes = {tuple(e["reason_codes"]) for e in result.evaluations if e["strategy"] == BUY_ID}
    sell_codes = {tuple(e["reason_codes"]) for e in result.evaluations if e["strategy"] == SELL_ID}
    assert buy_codes == {(ReasonCode.NOT_ELIGIBLE.value,)}
    assert sell_codes == {(ReasonCode.INSUFFICIENT_DATA.value,)}
    assert result.signals == () and result.intents == () and result.fills == ()
    assert sim.executions == ()
    assert [f for f in result.fills if f["symbol"] == "BBB"] == []


async def test_no_lookahead_evaluations_use_closed_bars_only(tmp_path: Path) -> None:
    """Every evaluation is timestamped at/after the close of the bar it cites."""
    result, _ = await _run(_sell_fixture(), tmp_path)
    assert result.evaluations, "the replay must evaluate bars"
    seen: dict[tuple[str, str], datetime] = {}
    for row in result.evaluations:
        at = datetime.fromisoformat(str(row["time"]))
        bar_time = datetime.fromisoformat(str(row["bar_time"]))
        assert at >= bar_time + MIN, (row["symbol"], row["strategy"], row["time"])
        key = (row["symbol"], row["strategy"])
        if key in seen:
            assert bar_time == seen[key] + MIN, "bars must advance one minute, no repeats"
        seen[key] = bar_time


async def test_no_lookahead_truncated_prefix_invariance(tmp_path: Path) -> None:
    """Dropping later bars leaves every earlier evaluation bit-identical."""
    full_events = _sell_fixture()
    full, _ = await _run(full_events, tmp_path / "full")
    cutoff = SESSION_OPEN + 16 * MIN  # keep bars closing at/before 09:46 ET
    trunc_events = [e for e in full_events if e.event_time + MIN <= cutoff]
    assert len(trunc_events) < len(full_events)
    trunc, _ = await _run(trunc_events, tmp_path / "trunc")
    expected = [e for e in full.evaluations if e["time"] <= cutoff]
    assert trunc.evaluations == tuple(expected)
    assert trunc.signals == () and full.signals == ()


async def test_deterministic_rerun_identity(tmp_path: Path) -> None:
    """Two fresh runs agree on manifest bytes, config hash, and results hash."""
    first, _ = await _run(_sell_fixture(), tmp_path / "a")
    second, _ = await _run(_sell_fixture(), tmp_path / "b")
    assert first.run_id == second.run_id
    assert first.manifest.canonical() == second.manifest.canonical()
    assert first.manifest.config_hash == second.manifest.config_hash
    assert first.results_hash == second.results_hash
    assert first.results_hash == second.results_hash and len(first.results_hash) == 64
    other, _ = await _run(_sell_fixture(), tmp_path / "c", seed=SEED + 1)
    assert other.manifest.config_hash != first.manifest.config_hash
    assert other.run_id != first.run_id


async def test_offline_without_credentials_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay completes with poisoned credentials and blocked sockets."""
    for var in POISONED_ENV_VARS:
        monkeypatch.setenv(var, "PHASE-Q-POISONED-DO-NOT-USE")
    result, _ = await _run(_sell_fixture(), tmp_path)
    assert result.evaluations, "offline replay must still evaluate bars"
    for var in POISONED_ENV_VARS:
        import os

        assert os.environ[var] == "PHASE-Q-POISONED-DO-NOT-USE"
    with pytest.raises(RuntimeError, match="blocked"):
        socket.create_connection(("127.0.0.1", 9))


async def test_no_profitability_claim(tmp_path: Path) -> None:
    """The replay artifact carries no return/win-rate metrics and none are asserted."""
    result, _ = await _run(_sell_fixture(), tmp_path)
    assert not hasattr(result, "metrics")
    blob = json.dumps(
        {
            "evaluations": result.evaluations,
            "signals": result.signals,
            "intents": result.intents,
            "fills": result.fills,
        },
        default=str,
    ).lower()
    for banned in ("pnl", "total_return", "net_return", "win_rate", "profit_factor", "expectancy"):
        assert banned not in blob


# ---------------------------------------------------------------------------
# Signal-dependent scenarios: blocked by the open production defects recorded
# in docs/verification/phase_q_backtest.md (runner supplies no VWAP indicators
# and no eligibility state, and its single-intent hook cannot arm protection).
# Each test asserts the desired end state over the real replay and fails today
# only because no signal fires; strict xfail keeps them as tripwires.


@pytest.mark.xfail(strict=True, reason="Q-D1/Q-D2: runner emits no signals (no indicators, no eligibility)")
async def test_sell_path_closes_position(tmp_path: Path) -> None:
    result, sim = await _run(_sell_fixture(), tmp_path)
    buys = [f for f in result.fills if f["side"] == "BUY"]
    sells = [f for f in result.fills if f["side"] == "SELL"]
    assert buys and sells, "BUY then SELL fills are required"
    assert sum(f["quantity"] for f in buys) == sum(f["quantity"] for f in sells)
    assert sim.held_quantity("AAA") == D("0")
    first_buy = min(f["time"] for f in buys)
    assert all(str(f["time"]) > str(first_buy) for f in sells)


@pytest.mark.xfail(strict=True, reason="Q-D3: runner has no path to arm protective stops")
async def test_stop_first_path(tmp_path: Path) -> None:
    _, sim = await _run(_sell_fixture(), tmp_path)
    stops = [e for e in sim.executions if e.purpose is OrderPurpose.PROTECTIVE_STOP]
    exits = [e for e in sim.executions if e.purpose is OrderPurpose.STRATEGY_EXIT]
    assert stops, "a protective stop fill is required"
    assert exits, "the strategy exit must also be represented"
    assert all(e.fill.filled_at >= stops[0].fill.filled_at for e in exits)


@pytest.mark.xfail(strict=True, reason="Q-D1/Q-D2: no position exists, so intrabar ambiguity is unreachable")
async def test_ambiguous_intrabar_pessimistic(tmp_path: Path) -> None:
    _, sim = await _run(_ambig_fixture(), tmp_path)
    assert any(
        reason == AMBIGUOUS_INTRABAR_ORDER for _, _, reason in sim.diagnostics
    ), "the wide-range bar must record pessimistic intrabar ambiguity"


@pytest.mark.xfail(strict=True, reason="Q-D1/Q-D2: runner emits no signals, so suppression has nothing to act on")
async def test_no_duplicate_buys(tmp_path: Path) -> None:
    result, _ = await _run(_dup_fixture(), tmp_path)
    buy_fills = [f for f in result.fills if f["side"] == "BUY"]
    assert len(buy_fills) == 1, "exactly one BUY fill across repeated crosses"


@pytest.mark.xfail(strict=True, reason="Q-D3: no protective stop is armed through the runner")
async def test_stop_parity_and_never_moves_down(tmp_path: Path) -> None:
    _, sim = await _run(_dup_fixture(), tmp_path)
    entries = [e for e in sim.executions if e.purpose is OrderPurpose.ENTRY]
    assert entries, "an entry fill is required for stop parity"
    expected = compute_fixed_stop_price(
        ProtectionPolicy(mode=ProtectionMode.FIXED, distance_usd=D("1")),
        entries[0].fill.price,
        tick_rules=ALPACA_EQUITY_TICK_RULES,
    )
    open_orders = await sim.list_open_orders()
    armed = [o for o in open_orders if o.stop_price is not None]
    assert armed, "a protective stop must be armed"
    assert armed[0].stop_price == expected, "armed stop must equal production stop math"
    with pytest.raises(ValueError, match="never move downward"):
        await sim.replace_order(armed[0].broker_order_id, stop_price=expected - D("1"))
