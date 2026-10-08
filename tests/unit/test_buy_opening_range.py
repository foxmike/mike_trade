"""Unit tests for the opening-range breakout BUY strategy (phase J)."""

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
from mike_trade.strategies.buy.opening_range_breakout import (
    DEFAULT_OPENING_RANGE_MINUTES,
    ELIGIBILITY_WATCHING_BUY,
    STRATEGY_ID,
    STRATEGY_VERSION,
    OpeningRangeBreakoutBuy,
)
from mike_trade.strategies.registry import StrategyRegistry

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 8)
DAY2 = date(2026, 10, 9)
SYMBOL = "SPY"
RANGE_HIGH = Decimal("101")


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


def _range_bars(day: date = DAY, n: int = DEFAULT_OPENING_RANGE_MINUTES) -> list[Candle]:
    start = _open_utc(day)
    return [
        _bar(start + timedelta(minutes=i), "100", "101", "99", "100") for i in range(n)
    ]


def _breakout_bar(
    day: date = DAY, index: int = DEFAULT_OPENING_RANGE_MINUTES, close: str = "102",
    volume: str = "500",
) -> Candle:
    return _bar(_open_utc(day) + timedelta(minutes=index), "100", "103", "100", close, volume)


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
def strategy() -> OpeningRangeBreakoutBuy:
    return OpeningRangeBreakoutBuy()


# ------------------------------------------------------------------ spec


def test_spec_contract_and_registration() -> None:
    assert issubclass(OpeningRangeBreakoutBuy, BuyStrategy)
    spec = OpeningRangeBreakoutBuy.spec
    assert spec.strategy_id == STRATEGY_ID == "opening_range_breakout"
    assert spec.version == STRATEGY_VERSION
    assert spec.version_key == (1, 0, 0)
    assert spec.side is StrategyRole.BUY
    assert spec.required_timeframes == (CandleTimeframe.M1,)
    assert spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.completed_bars_only is True
    assert spec.requires_volume is True
    assert spec.trigger_kind is TriggerKind.EDGE_ON_CLOSED_BAR
    assert spec.warmup_bars == DEFAULT_OPENING_RANGE_MINUTES + 1 == 31
    assert spec.persistent_state_keys == ("session_id", "fired", "last_signal_bar")
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
    assert validated["opening_range_minutes"] == DEFAULT_OPENING_RANGE_MINUTES
    assert validated["min_breakout_volume"] == Decimal("0")

    registry = StrategyRegistry()
    assert registry.register(OpeningRangeBreakoutBuy) is OpeningRangeBreakoutBuy
    assert registry.get("opening_range_breakout") is OpeningRangeBreakoutBuy
    assert registry.get("opening_range_breakout", "1.0.0") is OpeningRangeBreakoutBuy
    assert registry.validate_parameters("opening_range_breakout", {})[
        "opening_range_minutes"
    ] == DEFAULT_OPENING_RANGE_MINUTES


# ------------------------------------------------------------------ signals


def test_valid_crossing_emits_deterministic_signal(
    strategy: OpeningRangeBreakoutBuy,
) -> None:
    bars = [*_range_bars(), _breakout_bar()]
    ctx = _context(bars)
    first = strategy.evaluate(ctx, {}, _watching())
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
    second = strategy.evaluate(ctx, {}, _watching())
    assert second.signal_id == first.signal_id
    assert second.state_updates["fired"] is True
    assert second.state_updates["last_signal_bar"] == bars[-1].start.isoformat()
    assert second.state_updates["session_id"] == DAY.isoformat()


def test_close_exactly_equal_to_range_high_is_no_signal(
    strategy: OpeningRangeBreakoutBuy,
) -> None:
    bars = [*_range_bars(), _breakout_bar(close="101")]
    result = strategy.evaluate(_context(bars), {}, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_prior_close_above_high_is_not_an_edge(
    strategy: OpeningRangeBreakoutBuy,
) -> None:
    bars = _range_bars()
    bars[-1] = _bar(_open_utc(DAY) + timedelta(minutes=29), "100", "103", "100", "102")
    bars.append(_breakout_bar())
    result = strategy.evaluate(_context(bars), {}, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_custom_range_minutes() -> None:
    strategy = OpeningRangeBreakoutBuy()
    bars = [*_range_bars(n=5), _breakout_bar(index=5)]
    result = strategy.evaluate(_context(bars), {"opening_range_minutes": 5}, _watching())
    assert result.is_signal
    short = strategy.evaluate(
        _context(bars[:5]), {"opening_range_minutes": 5}, _watching()
    )
    assert not short.is_signal
    assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_min_breakout_volume_filter(strategy: OpeningRangeBreakoutBuy) -> None:
    bars = [*_range_bars(), _breakout_bar(volume="500")]
    ctx = _context(bars)
    assert not strategy.evaluate(
        ctx, {"min_breakout_volume": Decimal("1000")}, _watching()
    ).is_signal
    assert strategy.evaluate(
        ctx, {"min_breakout_volume": Decimal("500")}, _watching()
    ).is_signal


# ------------------------------------------------------------------ gating


def test_incomplete_range_and_warmup_is_insufficient_data(
    strategy: OpeningRangeBreakoutBuy,
) -> None:
    assert strategy.evaluate(
        _context(_range_bars()[:20]), {}, _watching()
    ).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    # Full default range but no post-window bar yet: warmup (30 + 1) unmet.
    assert strategy.evaluate(
        _context(_range_bars()), {}, _watching()
    ).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_missing_bar_inside_window_is_insufficient_data(
    strategy: OpeningRangeBreakoutBuy,
) -> None:
    bars = _range_bars()
    del bars[15]
    bars.append(_breakout_bar())
    result = strategy.evaluate(_context(bars), {}, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_stale_data(strategy: OpeningRangeBreakoutBuy) -> None:
    bars = [*_range_bars(), _breakout_bar()]
    result = strategy.evaluate(_context(bars, stale=True), {}, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.STALE_DATA,)


def test_zero_volume_breakout_bar_is_no_signal(
    strategy: OpeningRangeBreakoutBuy,
) -> None:
    bars = [*_range_bars(), _breakout_bar(volume="0")]
    result = strategy.evaluate(_context(bars), {}, _watching())
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_not_eligible_outside_regular_or_not_watching(
    strategy: OpeningRangeBreakoutBuy,
) -> None:
    bars = [*_range_bars(), _breakout_bar()]
    closed = strategy.evaluate(
        _context(bars, session_status=SessionStatus.CLOSED), {}, _watching()
    )
    assert not closed.is_signal
    assert closed.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    for state in ({}, {"eligibility": "FLAT"}, {"eligibility": "WATCHING_SELL"}):
        result = strategy.evaluate(_context(bars), {}, state)
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.NOT_ELIGIBLE,)


# ------------------------------------------------------------------ rearm


def _merged(state: dict[str, object], updates: object) -> dict[str, object]:
    assert isinstance(updates, Mapping)
    return {**state, **dict(updates)}


def test_same_bar_replay_is_suppressed(strategy: OpeningRangeBreakoutBuy) -> None:
    bars = [*_range_bars(), _breakout_bar()]
    ctx = _context(bars)
    state = _watching()
    first = strategy.evaluate(ctx, {}, state)
    assert first.is_signal
    replay = strategy.evaluate(ctx, {}, _merged(state, first.state_updates))
    assert not replay.is_signal
    assert replay.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_no_rearm_while_close_stays_above_high(
    strategy: OpeningRangeBreakoutBuy,
) -> None:
    bars = [*_range_bars(), _breakout_bar()]
    state = _merged(_watching(), strategy.evaluate(_context(bars), {}, _watching()).state_updates)
    higher = [*bars, _bar(_open_utc(DAY) + timedelta(minutes=31), "102", "104", "102", "103")]
    result = strategy.evaluate(_context(higher), {}, state)
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_rearm_after_pullback(strategy: OpeningRangeBreakoutBuy) -> None:
    bars = [*_range_bars(), _breakout_bar()]
    state = _merged(_watching(), strategy.evaluate(_context(bars), {}, _watching()).state_updates)
    pullback = [
        *bars,
        _bar(_open_utc(DAY) + timedelta(minutes=31), "102", "102", "99", "100"),
    ]
    rearmed = strategy.evaluate(_context(pullback), {}, state)
    assert not rearmed.is_signal
    assert rearmed.reason_codes == (ReasonCode.NO_SIGNAL,)
    state = _merged(state, rearmed.state_updates)
    second_break = [
        *pullback,
        _bar(_open_utc(DAY) + timedelta(minutes=32), "100", "103", "100", "102"),
    ]
    again = strategy.evaluate(_context(second_break), {}, state)
    assert again.is_signal
    assert again.signal_id != strategy.evaluate(_context(bars), {}, _watching()).signal_id


def test_range_resets_at_new_session(strategy: OpeningRangeBreakoutBuy) -> None:
    day1 = [*_range_bars(), _breakout_bar()]
    old_state = _merged(
        _watching(), strategy.evaluate(_context(day1), {}, _watching()).state_updates
    )
    assert old_state["fired"] is True
    day2 = [*_range_bars(day=DAY2), _breakout_bar(day=DAY2)]
    result = strategy.evaluate(_context(day2, day=DAY2), {}, old_state)
    assert result.is_signal
    assert result.state_updates["session_id"] == DAY2.isoformat()


# ------------------------------------------------------------------ params


@pytest.mark.parametrize(
    "params",
    [
        {"opening_range_minutes": 0},
        {"opening_range_minutes": 121},
        {"opening_range_minutes": 30.0},
        {"opening_range_minutes": "30"},
        {"opening_range_minutes": True},
        {"min_breakout_volume": 1.5},
        {"min_breakout_volume": "100"},
        {"min_breakout_volume": Decimal("-1")},
        {"bogus": 1},
    ],
)
def test_parameter_validation_failures(
    strategy: OpeningRangeBreakoutBuy, params: dict[str, object]
) -> None:
    bars = [*_range_bars(), _breakout_bar()]
    with pytest.raises(ParameterValidationError):
        strategy.evaluate(_context(bars), params, _watching())


def test_decimal_volume_accepts_int_and_normalizes() -> None:
    schema = OpeningRangeBreakoutBuy.spec.parameter_schema
    assert schema.validate({"min_breakout_volume": 10})["min_breakout_volume"] == Decimal("10")


# ------------------------------------------------------------------ hygiene


def test_module_imports_no_broker_network_filesystem_or_secrets() -> None:
    path = Path(__file__).resolve().parent.parent.parent / "mike_trade" / "strategies" / "buy" / (
        "opening_range_breakout.py"
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
