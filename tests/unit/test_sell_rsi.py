"""Unit tests for the RSI reversal SELL strategy."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from mike_trade.domain.signals import SignalAction
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import ManagementStatus, Position, PositionDirection
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    ParameterValidationError,
    ReasonCode,
    SellStrategy,
    TriggerKind,
    make_signal_id,
)
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.strategies.sell.base import (
    ExitLongRecommendation,
    ExitSource,
    SellReasonCode,
    SellRule,
    evaluate_exit_eligibility,
)
from mike_trade.strategies.sell.rsi_reversal import (
    DEFAULT_OVERBOUGHT,
    DEFAULT_RESET_THRESHOLD,
    DEFAULT_RSI_PERIOD,
    RSI_LATEST_KEY,
    RSI_PRIOR_KEY,
    STRATEGY_ID,
    STRATEGY_VERSION,
    WARMUP_BARS,
    RsiReversalSell,
)

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 8)
SYMBOL = "SPY"
OPENED = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
OB = Decimal("70")
RESET = Decimal("50")
PERIOD = 3
PARAMS_RESET: dict[str, object] = {
    "rsi_period": PERIOD,
    "overbought": OB,
    "reset_threshold": RESET,
    "timeframe": "5m",
}
PARAMS_NORESET: dict[str, object] = {
    "rsi_period": PERIOD,
    "overbought": OB,
    "timeframe": "5m",
}


def _open_utc(day: date = DAY) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=NY).replace(
        hour=9, minute=30
    ).astimezone(UTC)


def _bar(start: datetime, close: str = "100", volume: str = "100") -> Candle:
    c = Decimal(close)
    return Candle(
        symbol=SYMBOL,
        timeframe=CandleTimeframe.M5,
        start=start,
        end=start + timedelta(minutes=5),
        open=Decimal(close),
        high=c + 1,
        low=c - 1,
        close=c,
        volume=Decimal(volume),
        feed_id="sip",
        consolidated=True,
        origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
    )


def _bars(count: int, day: date = DAY) -> list[Candle]:
    start = _open_utc(day)
    return [_bar(start + timedelta(minutes=5 * i)) for i in range(count)]


def _context(
    bars: list[Candle],
    *,
    rsi: Decimal | None = Decimal("60"),
    rsi_prev: Decimal | None = Decimal("75"),
    day: date = DAY,
    session_status: SessionStatus = SessionStatus.REGULAR,
    stale: bool = False,
) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=10)
    newest = bars[-1].end
    return MarketContext(
        symbol=SYMBOL,
        as_of=as_of,
        version=1,
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=session_status,
        session_id=day.isoformat(),
        freshness=ContextFreshness(
            newest_data_ts=newest,
            age=as_of - newest,
            max_age=timedelta(minutes=30),
            stale=stale,
            reason="DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M5: tuple(bars)},
        indicators={RSI_LATEST_KEY: rsi, RSI_PRIOR_KEY: rsi_prev},
    )


def _stub_context(
    bars: tuple[object, ...],
    *,
    indicators: Mapping[str, object] | None = None,
    session_status: SessionStatus = SessionStatus.REGULAR,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=SYMBOL,
        freshness=SimpleNamespace(stale=False),
        session_status=session_status,
        session_id=DAY.isoformat(),
        completed_bars_only=True,
        feed_id="sip",
        candles={CandleTimeframe.M5: bars},
        indicators=dict(indicators)
        if indicators is not None
        else {RSI_LATEST_KEY: Decimal("60"), RSI_PRIOR_KEY: Decimal("75")},
    )


def _managed_position(quantity: str = "7.5") -> Position:
    return Position(
        account_scope_id="acct",
        symbol=SYMBOL,
        quantity=Decimal(quantity),
        avg_entry_price=Decimal("100"),
        cost_basis=Decimal("750"),
        as_of=datetime(2026, 10, 8, 14, 0, tzinfo=UTC),
        management=ManagementStatus.MANAGED,
        cycle_id="cycle-1",
    )


def _merged(state: dict[str, object], updates: object) -> dict[str, object]:
    assert isinstance(updates, Mapping)
    return {**state, **dict(updates)}


@pytest.fixture()
def strat() -> RsiReversalSell:
    return RsiReversalSell()


def test_spec_contract_and_registration() -> None:
    assert issubclass(RsiReversalSell, SellStrategy)
    assert issubclass(RsiReversalSell, SellRule)
    spec = RsiReversalSell.spec
    assert spec.strategy_id == STRATEGY_ID == "rsi_reversal"
    assert spec.version == STRATEGY_VERSION
    assert spec.version_key == (1, 0, 0)
    assert spec.side is StrategyRole.SELL
    assert spec.completed_bars_only is True
    assert spec.trigger_kind is TriggerKind.EDGE_ON_CLOSED_BAR
    assert spec.trigger_timeframe is CandleTimeframe.M5
    assert CandleTimeframe.M5 in spec.required_timeframes
    assert spec.warmup_bars == WARMUP_BARS == DEFAULT_RSI_PERIOD + 2
    assert set(spec.persistent_state_keys) >= {"armed", "last_signal_bar"}
    assert "wilder" in spec.methodology.lower()
    assert "rsi" in spec.methodology.lower()
    assert "profitability" in (spec.methodology + spec.source).lower()
    assert "not" in (spec.methodology + spec.source).lower()
    assert spec.source.strip() and spec.methodology.strip()
    assert len(spec.execution_limitations) >= 1
    for code in (
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ):
        assert code in spec.reason_codes
    schema = spec.parameter_schema
    names = {p.name for p in schema.parameters}
    assert {"rsi_period", "overbought", "reset_threshold", "timeframe"} <= names
    validated = schema.validate({})
    assert validated["rsi_period"] == DEFAULT_RSI_PERIOD
    assert validated["overbought"] == DEFAULT_OVERBOUGHT
    assert isinstance(validated["overbought"], Decimal)
    registry = StrategyRegistry()
    assert registry.register(RsiReversalSell) is RsiReversalSell
    assert registry.get("rsi_reversal") is RsiReversalSell
    assert registry.list_strategies(StrategyRole.BUY) == ()


def test_prior_equal_threshold_with_latest_below_signals(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    ctx = _context(bars, rsi=Decimal("69.9"), rsi_prev=Decimal("70"))
    first = strat.evaluate(ctx, PARAMS_RESET, {})
    assert first.is_signal
    assert first.reason_codes == (ReasonCode.SIGNAL,)
    assert first.bar_time == bars[-1].start
    assert isinstance(first.reference_price, Decimal)
    assert not hasattr(first, "confidence")
    expected = make_signal_id(STRATEGY_ID, STRATEGY_VERSION, SYMBOL, StrategyRole.SELL, bars[-1].start)
    assert first.signal_id == expected
    assert first.state_updates["armed"] is False


def test_latest_equal_threshold_does_not_signal(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    result = strat.evaluate(
        _context(bars, rsi=Decimal("70"), rsi_prev=Decimal("75")), PARAMS_RESET, {}
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_upward_cross_does_not_signal(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    result = strat.evaluate(
        _context(bars, rsi=Decimal("75"), rsi_prev=Decimal("65")), PARAMS_RESET, {}
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_same_bar_replay_suppressed(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    ctx = _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("75"))
    state: dict[str, object] = {}
    first = strat.evaluate(ctx, PARAMS_RESET, state)
    assert first.is_signal and first.signal_id is not None
    replay = strat.evaluate(ctx, PARAMS_RESET, _merged(state, first.state_updates))
    assert not replay.is_signal
    assert replay.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert replay.signal_id is None
    # Deterministic ID: unevaluated repeat without state still yields same ID.
    again = strat.evaluate(ctx, PARAMS_RESET, {})
    assert again.signal_id == first.signal_id


def test_disarm_and_reset_rearm(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    ctx_fire = _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("75"))
    first = strat.evaluate(ctx_fire, PARAMS_RESET, {})
    assert first.is_signal
    state = _merged({}, first.state_updates)
    assert state["armed"] is False
    # Still crossing but disarmed -> suppressed.
    suppressed = strat.evaluate(
        _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("80")), PARAMS_RESET, state
    )
    assert not suppressed.is_signal
    assert suppressed.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert suppressed.state_updates["armed"] is False
    # RSI above reset keeps disarmed (new bar, still above reset).
    nxt = _bar(bars[-1].start + timedelta(minutes=5))
    high = strat.evaluate(
        _context([*bars, nxt], rsi=Decimal("60"), rsi_prev=Decimal("60")), PARAMS_RESET, state
    )
    assert not high.is_signal
    assert high.state_updates["armed"] is False
    # RSI at/below reset rearms (no fire on the rearming bar).
    nxt2 = _bar(bars[-1].start + timedelta(minutes=5))
    rearmed = strat.evaluate(
        _context([*bars, nxt2], rsi=RESET, rsi_prev=Decimal("60")), PARAMS_RESET, state
    )
    assert not rearmed.is_signal
    assert rearmed.state_updates["armed"] is True
    state2 = _merged(state, rearmed.state_updates)
    # Next crossing fires again with a new bar/ID.
    extended = [*bars, _bar(bars[-1].start + timedelta(minutes=5))]
    refire = strat.evaluate(
        _context(extended, rsi=Decimal("60"), rsi_prev=Decimal("75")), PARAMS_RESET, state2
    )
    assert refire.is_signal
    assert refire.signal_id != first.signal_id
    assert refire.bar_time == extended[-1].start


def test_no_reset_rearm_requires_reexceed(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    first = strat.evaluate(
        _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("75")), PARAMS_NORESET, {}
    )
    assert first.is_signal
    state = _merged({}, first.state_updates)
    still = strat.evaluate(
        _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("80")), PARAMS_NORESET, state
    )
    assert not still.is_signal
    assert still.state_updates["armed"] is False
    mid = strat.evaluate(
        _context([*bars, _bar(bars[-1].start + timedelta(minutes=5))], rsi=Decimal("65"), rsi_prev=Decimal("60")), PARAMS_NORESET, state
    )
    assert not mid.is_signal
    assert mid.state_updates["armed"] is False
    rearm = strat.evaluate(
        _context([*bars, _bar(bars[-1].start + timedelta(minutes=5))], rsi=Decimal("70"), rsi_prev=Decimal("60")), PARAMS_NORESET, state
    )
    assert not rearm.is_signal
    assert rearm.state_updates["armed"] is True


def test_state_round_trip_restart(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    first = strat.evaluate(
        _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("75")), PARAMS_RESET, {}
    )
    assert first.is_signal
    declared = RsiReversalSell.spec.persistent_state_keys
    restored = {k: dict(first.state_updates)[k] for k in declared if k in dict(first.state_updates)}
    assert set(restored) == set(declared)
    # Simulate restart: new instance, restored state only.
    fresh = RsiReversalSell()
    suppressed_restored = fresh.evaluate(
        _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("80")), PARAMS_RESET, dict(restored)
    )
    uninterrupted = strat.evaluate(
        _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("80")),
        PARAMS_RESET,
        _merged({}, first.state_updates),
    )
    assert suppressed_restored.reason_codes == uninterrupted.reason_codes
    assert suppressed_restored.signal_id == uninterrupted.signal_id is None
    assert dict(suppressed_restored.state_updates) == dict(uninterrupted.state_updates)


def test_incomplete_stale_unwarmed_out_of_session(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    stale = strat.evaluate(_context(bars, stale=True), PARAMS_RESET, {})
    assert not stale.is_signal
    assert stale.reason_codes == (ReasonCode.STALE_DATA,)
    developing = Candle(
        symbol=SYMBOL,
        timeframe=CandleTimeframe.M5,
        start=bars[0].start,
        end=bars[0].start + timedelta(minutes=5),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("100"),
        feed_id="sip",
        consolidated=True,
        origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.DEVELOPING,
    )
    stub = _stub_context(
        tuple([developing, *bars[1:]]),
        indicators={RSI_LATEST_KEY: Decimal("60"), RSI_PRIOR_KEY: Decimal("75")},
    )
    incomplete = strat.evaluate(stub, PARAMS_RESET, {})  # type: ignore[arg-type]
    assert not incomplete.is_signal
    assert incomplete.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    for indicators in (
        {RSI_LATEST_KEY: None, RSI_PRIOR_KEY: Decimal("75")},
        {RSI_LATEST_KEY: Decimal("60"), RSI_PRIOR_KEY: None},
        {},
    ):
        result = strat.evaluate(_context(bars, rsi=None, rsi_prev=None), PARAMS_RESET, {}) \
            if not indicators else strat.evaluate(
                _stub_context(tuple(bars), indicators=indicators),  # type: ignore[arg-type]
                PARAMS_RESET, {},
            )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    short = strat.evaluate(_context(bars[:PERIOD], rsi=Decimal("60"), rsi_prev=Decimal("75")), PARAMS_RESET, {})
    assert not short.is_signal
    assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    for status in (SessionStatus.CLOSED, SessionStatus.PRE_OPEN, SessionStatus.HOLIDAY, SessionStatus.UNKNOWN):
        result = strat.evaluate(
            _context(bars, session_status=status), PARAMS_RESET, {}
        )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.NOT_ELIGIBLE,)


def _eligibility(**overrides: object) -> tuple[SellReasonCode, ...]:
    bars = _bars(WARMUP_BARS)
    args: dict[str, object] = dict(
        position=_managed_position(),
        confirmed_remaining_quantity=Decimal("7.5"),
        enabled=True,
        context=_context(bars),
        spec=RsiReversalSell.spec,
        source=ExitSource.STRATEGY_SELL,
        minimum_hold=timedelta(0),
        evaluation_time=datetime(2026, 10, 8, 14, 30, tzinfo=UTC),
        position_opened_at=OPENED,
    )
    args.update(overrides)
    return evaluate_exit_eligibility(**args)  # type: ignore[arg-type]


def test_flat_unmanaged_produce_no_exit() -> None:
    result = _eligibility(position=None)
    assert result[0] is SellReasonCode.NOT_ELIGIBLE
    assert SellReasonCode.FLAT_POSITION in result
    unmanaged = Position(
        account_scope_id="acct",
        symbol=SYMBOL,
        quantity=Decimal("7.5"),
        avg_entry_price=Decimal("100"),
        cost_basis=Decimal("750"),
        as_of=datetime(2026, 10, 8, 14, 0, tzinfo=UTC),
        management=ManagementStatus.UNMANAGED,
    )
    result = _eligibility(position=unmanaged)
    assert result[0] is SellReasonCode.NOT_ELIGIBLE
    assert SellReasonCode.UNMANAGED_POSITION in result
    result = _eligibility(direction=PositionDirection.SHORT)
    assert result[0] is SellReasonCode.NOT_ELIGIBLE
    assert SellReasonCode.NOT_LONG in result


def test_minimum_hold_blocking() -> None:
    result = _eligibility(
        minimum_hold=timedelta(hours=1),
        evaluation_time=datetime(2026, 10, 8, 14, 30, tzinfo=UTC),
        position_opened_at=datetime(2026, 10, 8, 14, 0, tzinfo=UTC),
    )
    assert result[0] is SellReasonCode.NOT_ELIGIBLE
    assert SellReasonCode.MINIMUM_HOLD in result


def test_full_quantity_only_after_partial_fill(strat: RsiReversalSell) -> None:
    bars = _bars(WARMUP_BARS)
    evaluation = strat.evaluate(
        _context(bars, rsi=Decimal("60"), rsi_prev=Decimal("75")), PARAMS_RESET, {}
    )
    assert evaluation.is_signal and evaluation.bar_time is not None
    position = _managed_position("6")
    recommendation = ExitLongRecommendation(
        signal_id=make_signal_id(STRATEGY_ID, STRATEGY_VERSION, SYMBOL, StrategyRole.SELL, evaluation.bar_time),
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        position=position,
        bar_time=evaluation.bar_time,
        reason_codes=(SellReasonCode.ELIGIBLE,),
    )
    assert recommendation.signal_id == evaluation.signal_id
    assert recommendation.quantity == Decimal("6")
    assert recommendation.action is SignalAction.EXIT_LONG


@pytest.mark.parametrize(
    "params",
    [
        {"rsi_period": PERIOD, "overbought": OB, "reset_threshold": OB, "timeframe": "5m"},
        {"rsi_period": PERIOD, "overbought": OB, "reset_threshold": OB + 1, "timeframe": "5m"},
        {"rsi_period": 1, "overbought": OB, "reset_threshold": RESET, "timeframe": "5m"},
        {"rsi_period": 51, "overbought": OB, "reset_threshold": RESET, "timeframe": "5m"},
        {"rsi_period": PERIOD, "overbought": Decimal("50"), "reset_threshold": RESET, "timeframe": "5m"},
        {"rsi_period": PERIOD, "overbought": Decimal("100"), "reset_threshold": RESET, "timeframe": "5m"},
        {"rsi_period": PERIOD, "overbought": 70.0, "reset_threshold": RESET, "timeframe": "5m"},
        {"rsi_period": PERIOD, "overbought": OB, "reset_threshold": 50.0, "timeframe": "5m"},
        {"rsi_period": PERIOD, "overbought": OB, "timeframe": "9m"},
    ],
)
def test_parameter_validation_rejections(strat: RsiReversalSell, params: dict[str, object]) -> None:
    bars = _bars(WARMUP_BARS)
    with pytest.raises(ParameterValidationError):
        strat.evaluate(_context(bars), params, {})


def test_module_hygiene() -> None:
    path = Path(__file__).resolve().parent.parent.parent / "mike_trade" / "strategies" / "sell" / "rsi_reversal.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed"
            imported.add(node.module or "")
    forbidden = ("broker", "adapter", "persistence", "http", "secret", "os", "subprocess", "socket", "requests")
    assert not [name for name in imported if any(word in name.lower() for word in forbidden)]
    names = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "confidence" not in names
    assert "fetch" not in source.lower() or "never fetches" in source.lower()
    for token in ("Decimal",):
        assert token in source
