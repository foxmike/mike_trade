"""Unit tests for the momentum breakout BUY strategy (phase J)."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mike_trade.domain.strategy_types import StrategyRole
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
    BuyStrategy,
    ParameterValidationError,
    ReasonCode,
    TriggerKind,
    make_signal_id,
)
from mike_trade.strategies.buy.momentum_breakout import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_VOLUME_MULTIPLE,
    ELIGIBILITY_WATCHING_BUY,
    STRATEGY_ID,
    STRATEGY_VERSION,
    WARMUP_BARS,
    MomentumBreakoutBuy,
)
from mike_trade.strategies.registry import StrategyRegistry

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 8)
DAY2 = date(2026, 10, 9)
SYMBOL = "SPY"
LOOKBACK = 3
PARAMS = {"lookback_bars": LOOKBACK, "volume_multiple": Decimal("1.5")}


def _open_utc(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=NY).replace(
        hour=9, minute=30
    ).astimezone(UTC)


def _bar(
    start: datetime,
    o: str,
    h: str,
    low: str,
    c: str,
    v: str = "100",
) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timeframe=CandleTimeframe.M1,
        start=start,
        end=start + timedelta(minutes=1),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(v),
        feed_id="sip",
        consolidated=True,
        origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
    )


def _flat_bar(start: datetime, v: str = "100") -> Candle:
    return _bar(start, "100", "101", "99", "100", v)


def _breakout_series(
    day: date = DAY, *, close: str = "102", volume: str = "500"
) -> list[Candle]:
    start = _open_utc(day)
    bars = [_flat_bar(start + timedelta(minutes=i)) for i in range(LOOKBACK + 1)]
    bars.append(_bar(start + timedelta(minutes=LOOKBACK + 1), "100", "103", "100", close, volume))
    return bars


def _context(
    bars: list[Candle],
    *,
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
            max_age=timedelta(minutes=5),
            stale=stale,
            reason="DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M1: tuple(bars)},
        indicators={},
    )


def _watching(**extra: object) -> dict[str, object]:
    return {"eligibility": ELIGIBILITY_WATCHING_BUY, **extra}


@pytest.fixture()
def strategy() -> MomentumBreakoutBuy:
    return MomentumBreakoutBuy()


# ------------------------------------------------------------------ spec


def test_spec_contract_and_registration() -> None:
    assert issubclass(MomentumBreakoutBuy, BuyStrategy)
    spec = MomentumBreakoutBuy.spec
    assert spec.strategy_id == STRATEGY_ID == "momentum_breakout"
    assert spec.version == STRATEGY_VERSION
    assert spec.version_key == (1, 0, 0)
    assert spec.side is StrategyRole.BUY
    assert spec.required_timeframes == (CandleTimeframe.M1,)
    assert spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.completed_bars_only is True
    assert spec.requires_volume is True
    assert spec.trigger_kind is TriggerKind.EDGE_ON_CLOSED_BAR
    assert spec.warmup_bars == WARMUP_BARS == DEFAULT_LOOKBACK_BARS + 2
    assert spec.persistent_state_keys == ("session_id", "fired", "last_signal_bar")
    assert "volume" in spec.methodology.lower()
    assert "breakout" in spec.methodology.lower()
    assert "no alpha" in spec.methodology.lower()
    assert "profitability" in (spec.methodology + spec.source).lower()
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
    validated = spec.parameter_schema.validate({})
    assert validated["lookback_bars"] == DEFAULT_LOOKBACK_BARS
    assert validated["volume_multiple"] == DEFAULT_VOLUME_MULTIPLE
    assert isinstance(validated["volume_multiple"], Decimal)

    registry = StrategyRegistry()
    assert registry.register(MomentumBreakoutBuy) is MomentumBreakoutBuy
    assert registry.get("momentum_breakout") is MomentumBreakoutBuy
    assert registry.get("momentum_breakout", "1.0.0") is MomentumBreakoutBuy
    assert registry.validate_parameters("momentum_breakout", {})[
        "lookback_bars"
    ] == DEFAULT_LOOKBACK_BARS


# ------------------------------------------------------------------ signals


def test_valid_breakout_emits_deterministic_signal(
    strategy: MomentumBreakoutBuy,
) -> None:
    bars = _breakout_series()
    ctx = _context(bars)
    first = strategy.evaluate(ctx, PARAMS, _watching())
    assert first.is_signal
    assert first.reason_codes == (ReasonCode.SIGNAL,)
    assert first.bar_time == bars[-1].start
    assert isinstance(first.reference_price, Decimal)
    assert first.reference_price == Decimal("102")
    assert not hasattr(first, "confidence")
    expected = make_signal_id(
        STRATEGY_ID, STRATEGY_VERSION, SYMBOL, StrategyRole.BUY, bars[-1].start
    )
    assert first.signal_id == expected
    second = strategy.evaluate(ctx, PARAMS, _watching())
    assert second.signal_id == first.signal_id
    assert second.state_updates["fired"] is True
    assert second.state_updates["last_signal_bar"] == bars[-1].start.isoformat()
    assert second.state_updates["session_id"] == DAY.isoformat()


def test_close_exactly_equal_to_lookback_high_is_no_signal(
    strategy: MomentumBreakoutBuy,
) -> None:
    bars = _breakout_series(close="101")
    result = strategy.evaluate(_context(bars), PARAMS, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_prior_close_above_its_high_is_not_an_edge(
    strategy: MomentumBreakoutBuy,
) -> None:
    start = _open_utc(DAY)
    bars = [_flat_bar(start + timedelta(minutes=i)) for i in range(LOOKBACK)]
    # Prior bar already broke out: close 102 above prior lookback high 101.
    bars.append(_bar(start + timedelta(minutes=LOOKBACK), "100", "103", "100", "102", "500"))
    bars.append(_bar(start + timedelta(minutes=LOOKBACK + 1), "102", "104", "102", "103", "500"))
    result = strategy.evaluate(_context(bars), PARAMS, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_insufficient_volume_uses_distinct_volume_reason(
    strategy: MomentumBreakoutBuy,
) -> None:
    bars = _breakout_series(volume="100")  # avg 100, need >= 150
    result = strategy.evaluate(_context(bars), PARAMS, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    assert result.reason_codes != (ReasonCode.NO_SIGNAL,)


def test_zero_volume_breakout_bar(strategy: MomentumBreakoutBuy) -> None:
    bars = _breakout_series(volume="0")
    result = strategy.evaluate(_context(bars), PARAMS, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_volume_at_exact_threshold_signals(
    strategy: MomentumBreakoutBuy,
) -> None:
    bars = _breakout_series(volume="150")  # exactly 1.5 x 100
    result = strategy.evaluate(_context(bars), PARAMS, _watching())
    assert result.is_signal


# ------------------------------------------------------------------ gating


def test_warmup_is_insufficient_data(strategy: MomentumBreakoutBuy) -> None:
    bars = _breakout_series()[: LOOKBACK + 1]  # one bar short of lookback + 2
    result = strategy.evaluate(_context(bars), PARAMS, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_missing_bar_inside_window_is_insufficient_data(
    strategy: MomentumBreakoutBuy,
) -> None:
    bars = _breakout_series()
    del bars[1]
    result = strategy.evaluate(_context(bars), PARAMS, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_invalid_bar_is_insufficient_data(strategy: MomentumBreakoutBuy) -> None:
    from types import SimpleNamespace

    bars = _breakout_series()
    # A foreign-symbol bar bypasses MarketContext validation via a stub so the
    # strategy's own invalid-bar guard is exercised directly.
    foreign = _bar(bars[0].start, "100", "101", "99", "100")
    object.__setattr__(foreign, "symbol", "AAPL")
    bars[0] = foreign
    as_of = bars[-1].end + timedelta(seconds=10)
    stub = SimpleNamespace(
        symbol=SYMBOL,
        freshness=SimpleNamespace(stale=False),
        session_status=SessionStatus.REGULAR,
        session_id=DAY.isoformat(),
        candles={CandleTimeframe.M1: tuple(bars)},
    )
    result = strategy.evaluate(stub, PARAMS, _watching())  # type: ignore[arg-type]
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_stale_data(strategy: MomentumBreakoutBuy) -> None:
    bars = _breakout_series()
    result = strategy.evaluate(_context(bars, stale=True), PARAMS, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.STALE_DATA,)


def test_not_eligible_outside_regular_or_not_watching(
    strategy: MomentumBreakoutBuy,
) -> None:
    bars = _breakout_series()
    closed = strategy.evaluate(
        _context(bars, session_status=SessionStatus.CLOSED), PARAMS, _watching()
    )
    assert not closed.is_signal
    assert closed.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    for state in ({}, {"eligibility": "FLAT"}, {"eligibility": "WATCHING_SELL"}):
        result = strategy.evaluate(_context(bars), PARAMS, state)
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.NOT_ELIGIBLE,)


# ------------------------------------------------------------------ rearm


def _merged(state: dict[str, object], updates: object) -> dict[str, object]:
    assert isinstance(updates, Mapping)
    return {**state, **dict(updates)}


def test_same_bar_replay_is_suppressed(strategy: MomentumBreakoutBuy) -> None:
    bars = _breakout_series()
    ctx = _context(bars)
    state = _watching()
    first = strategy.evaluate(ctx, PARAMS, state)
    assert first.is_signal
    replay = strategy.evaluate(ctx, PARAMS, _merged(state, first.state_updates))
    assert not replay.is_signal
    assert replay.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_no_rearm_while_close_stays_above_high(
    strategy: MomentumBreakoutBuy,
) -> None:
    bars = _breakout_series()
    state = _merged(_watching(), strategy.evaluate(_context(bars), PARAMS, _watching()).state_updates)
    higher = [*bars, _bar(_open_utc(DAY) + timedelta(minutes=LOOKBACK + 2), "102", "104", "102", "103")]
    result = strategy.evaluate(_context(higher), PARAMS, state)
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_rearm_after_pullback(strategy: MomentumBreakoutBuy) -> None:
    bars = _breakout_series()
    first_id = strategy.evaluate(_context(bars), PARAMS, _watching()).signal_id
    assert first_id is not None
    state = _merged(
        _watching(),
        strategy.evaluate(_context(bars), PARAMS, _watching()).state_updates,
    )
    pullback = [
        *bars,
        _bar(_open_utc(DAY) + timedelta(minutes=LOOKBACK + 2), "102", "102", "99", "100"),
    ]
    rearmed = strategy.evaluate(_context(pullback), PARAMS, state)
    assert not rearmed.is_signal
    assert rearmed.reason_codes == (ReasonCode.NO_SIGNAL,)
    state = _merged(state, rearmed.state_updates)
    second_break = [
        *pullback,
        _bar(_open_utc(DAY) + timedelta(minutes=LOOKBACK + 3), "100", "105", "100", "104", "500"),
    ]
    again = strategy.evaluate(_context(second_break), PARAMS, state)
    assert again.is_signal
    assert again.signal_id != first_id


def test_session_reset(strategy: MomentumBreakoutBuy) -> None:
    day1 = _breakout_series(day=DAY)
    old_state = _merged(
        _watching(), strategy.evaluate(_context(day1), PARAMS, _watching()).state_updates
    )
    assert old_state["fired"] is True
    day2 = _breakout_series(day=DAY2)
    result = strategy.evaluate(_context(day2, day=DAY2), PARAMS, old_state)
    assert result.is_signal
    assert result.state_updates["session_id"] == DAY2.isoformat()
    # Windows do not span sessions: day-2 bars alone are insufficient without
    # enough same-session history.
    lone = day2[-2:]
    short = strategy.evaluate(_context(lone, day=DAY2), PARAMS, _watching())
    assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ------------------------------------------------------------------ params


@pytest.mark.parametrize(
    "params",
    [
        {"lookback_bars": 1},
        {"lookback_bars": 101},
        {"lookback_bars": 3.0},
        {"lookback_bars": "3"},
        {"lookback_bars": True},
        {"volume_multiple": 1.5},
        {"volume_multiple": "1.5"},
        {"volume_multiple": Decimal("0.5")},
        {"volume_multiple": Decimal("10.01")},
        {"bogus": 1},
    ],
)
def test_parameter_validation_failures(
    strategy: MomentumBreakoutBuy, params: dict[str, object]
) -> None:
    bars = _breakout_series()
    with pytest.raises(ParameterValidationError):
        strategy.evaluate(_context(bars), params, _watching())


def test_decimal_volume_accepts_int_and_normalizes() -> None:
    schema = MomentumBreakoutBuy.spec.parameter_schema
    assert schema.validate({"volume_multiple": 2})["volume_multiple"] == Decimal("2")


# ------------------------------------------------------------------ hygiene


def test_module_imports_no_broker_network_filesystem_or_secrets() -> None:
    path = Path(__file__).resolve().parent.parent.parent / "mike_trade" / "strategies" / "buy" / (
        "momentum_breakout.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_top = {
        "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "websocket",
        "websockets", "socket", "ssl", "ftplib", "smtplib", "alpaca", "grpc", "os",
        "io", "pathlib", "shutil", "tempfile", "glob", "fileinput", "sqlite3",
        "subprocess", "dotenv", "keyring", "secrets", "importlib", "pkgutil",
        "runpy", "imp", "pickle", "marshal", "ctypes",
    }
    forbidden_mike_trade = (
        "mike_trade.brokers", "mike_trade.execution", "mike_trade.persistence",
        "mike_trade.runtime", "mike_trade.engine", "mike_trade.app", "mike_trade.cli",
        "mike_trade.config",
    )
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed"
            imported.add(node.module or "")
    assert imported, "expected at least one import"
    for name in imported:
        assert name.split(".")[0] not in forbidden_top, f"imports {name}"
        assert not name.startswith(forbidden_mike_trade), f"imports {name}"
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"eval", "exec", "compile", "open", "__import__"}
