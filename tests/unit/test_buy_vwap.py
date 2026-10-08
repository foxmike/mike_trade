"""Unit tests for the VWAP bounce reference BUY strategy (phase J)."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
from mike_trade.strategies.buy.vwap_bounce import (
    ELIGIBILITY_WATCHING_BUY,
    STRATEGY_ID,
    STRATEGY_VERSION,
    WARMUP_BARS,
    VwapBounceBuy,
)
from mike_trade.strategies.registry import StrategyRegistry

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 8)
DAY2 = date(2026, 10, 9)
SYMBOL = "SPY"
PARAMS: dict[str, object] = {}


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


def _context(
    bars: list[Candle],
    *,
    day: date = DAY,
    session_status: SessionStatus = SessionStatus.REGULAR,
    stale: bool = False,
    vwap: Decimal | None = Decimal("100.5"),
    vwap_prev: Decimal | None = Decimal("100"),
    indicators: Mapping[str, Decimal | None] | None = None,
) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=10)
    newest = bars[-1].end
    values = {"vwap": vwap, "vwap_prev": vwap_prev} if indicators is None else dict(indicators)
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
        indicators=values,
    )


def _stub_context(
    bars: tuple[object, ...],
    *,
    indicators: Mapping[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=SYMBOL,
        freshness=SimpleNamespace(stale=False),
        session_status=SessionStatus.REGULAR,
        session_id=DAY.isoformat(),
        candles={CandleTimeframe.M1: bars},
        indicators=dict(indicators) if indicators is not None else {
            "vwap": Decimal("100.5"), "vwap_prev": Decimal("100"),
        },
    )


def _watching(**extra: object) -> dict[str, object]:
    return {"eligibility": ELIGIBILITY_WATCHING_BUY, **extra}


def _crossing_pair(
    day: date = DAY,
    *,
    prior_close: str = "100",
    latest_close: str = "101",
    vwap: str = "100.5",
    vwap_prev: str = "100",
    volume: str = "100",
) -> tuple[list[Candle], Decimal, Decimal]:
    start = _open_utc(day)
    pc, lc = Decimal(prior_close), Decimal(latest_close)
    band_low, band_high = str(min(pc, lc) - 1), str(max(pc, lc) + 1)
    bars = [
        _bar(start, prior_close, band_high, band_low, prior_close, volume),
        _bar(start + timedelta(minutes=1), latest_close, band_high, band_low, latest_close, volume),
    ]
    return bars, Decimal(vwap), Decimal(vwap_prev)


@pytest.fixture()
def strategy() -> VwapBounceBuy:
    return VwapBounceBuy()


# ------------------------------------------------------------------ spec


def test_spec_contract_and_registration() -> None:
    assert issubclass(VwapBounceBuy, BuyStrategy)
    spec = VwapBounceBuy.spec
    assert spec.strategy_id == STRATEGY_ID == "vwap_bounce"
    assert spec.version == STRATEGY_VERSION
    assert spec.version_key == (1, 0, 0)
    assert spec.side is StrategyRole.BUY
    assert spec.required_timeframes == (CandleTimeframe.M1,)
    assert spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.completed_bars_only is True
    assert spec.requires_volume is True
    assert spec.trigger_kind is TriggerKind.EDGE_ON_CLOSED_BAR
    assert spec.warmup_bars == WARMUP_BARS == 2
    assert spec.persistent_state_keys == ("session_id", "last_signal_bar")
    assert "vwap" in spec.methodology.lower()
    assert "cross" in spec.methodology.lower()
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

    registry = StrategyRegistry()
    assert registry.register(VwapBounceBuy) is VwapBounceBuy
    assert registry.get("vwap_bounce") is VwapBounceBuy
    assert registry.get("vwap_bounce", "1.0.0") is VwapBounceBuy
    assert registry.validate_parameters("vwap_bounce", {}) == {}
    # Registered independently: no SELL partner is required or present.
    assert registry.list_strategies(StrategyRole.SELL) == ()
    assert registry.list_strategies(StrategyRole.BUY) == (VwapBounceBuy,)


def test_unknown_parameter_is_rejected(strategy: VwapBounceBuy) -> None:
    bars, vwap, vwap_prev = _crossing_pair()
    ctx = _context(bars, vwap=vwap, vwap_prev=vwap_prev)
    with pytest.raises(ParameterValidationError):
        strategy.evaluate(ctx, {"bogus": 1}, _watching())


# ------------------------------------------------------------------ crossing boundaries


def test_prior_close_equal_prior_vwap_and_latest_above_signals(
    strategy: VwapBounceBuy,
) -> None:
    bars, vwap, vwap_prev = _crossing_pair(prior_close="100", latest_close="101")
    ctx = _context(bars, vwap=vwap, vwap_prev=vwap_prev)
    first = strategy.evaluate(ctx, PARAMS, _watching())
    assert first.is_signal
    assert first.reason_codes == (ReasonCode.SIGNAL,)
    assert first.bar_time == bars[-1].start
    assert isinstance(first.reference_price, Decimal)
    assert first.reference_price == Decimal("101")
    assert not hasattr(first, "confidence")
    expected = make_signal_id(
        STRATEGY_ID, STRATEGY_VERSION, SYMBOL, StrategyRole.BUY, bars[-1].start
    )
    assert first.signal_id == expected
    second = strategy.evaluate(ctx, PARAMS, _watching())
    assert second.signal_id == first.signal_id
    assert second.state_updates["last_signal_bar"] == bars[-1].start.isoformat()
    assert second.state_updates["session_id"] == DAY.isoformat()


def test_prior_close_below_prior_vwap_and_latest_above_signals(
    strategy: VwapBounceBuy,
) -> None:
    bars, vwap, vwap_prev = _crossing_pair(prior_close="99", latest_close="101")
    result = strategy.evaluate(
        _context(bars, vwap=vwap, vwap_prev=vwap_prev), PARAMS, _watching()
    )
    assert result.is_signal
    assert result.reason_codes == (ReasonCode.SIGNAL,)


def test_latest_close_equal_latest_vwap_is_no_signal(
    strategy: VwapBounceBuy,
) -> None:
    bars, vwap, vwap_prev = _crossing_pair(prior_close="99", latest_close="100.5")
    result = strategy.evaluate(
        _context(bars, vwap=vwap, vwap_prev=vwap_prev), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_prior_close_above_prior_vwap_is_no_signal(
    strategy: VwapBounceBuy,
) -> None:
    bars, vwap, vwap_prev = _crossing_pair(prior_close="101", latest_close="102")
    result = strategy.evaluate(
        _context(bars, vwap=vwap, vwap_prev=vwap_prev), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_downward_cross_is_no_signal(strategy: VwapBounceBuy) -> None:
    bars, vwap, vwap_prev = _crossing_pair(prior_close="99", latest_close="99.5")
    result = strategy.evaluate(
        _context(bars, vwap=vwap, vwap_prev=vwap_prev), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_above_to_below_cross_is_no_signal(strategy: VwapBounceBuy) -> None:
    bars, vwap, vwap_prev = _crossing_pair(prior_close="101", latest_close="99")
    result = strategy.evaluate(
        _context(bars, vwap=vwap, vwap_prev=vwap_prev), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


# ------------------------------------------------------------------ gating


def test_stale_data(strategy: VwapBounceBuy) -> None:
    bars, vwap, vwap_prev = _crossing_pair()
    result = strategy.evaluate(
        _context(bars, stale=True, vwap=vwap, vwap_prev=vwap_prev), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.STALE_DATA,)


def test_incomplete_bar_is_insufficient_data(strategy: VwapBounceBuy) -> None:
    start = _open_utc(DAY)
    developing = Candle(
        symbol=SYMBOL,
        timeframe=CandleTimeframe.M1,
        start=start,
        end=start + timedelta(minutes=1),
        open=Decimal("99"),
        high=Decimal("100"),
        low=Decimal("98"),
        close=Decimal("99"),
        volume=Decimal("100"),
        feed_id="sip",
        consolidated=True,
        origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.DEVELOPING,
    )
    closed = _bar(start + timedelta(minutes=1), "100", "102", "99", "101")
    result = strategy.evaluate(
        _stub_context((developing, closed)), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_missing_vwap_is_insufficient_data(strategy: VwapBounceBuy) -> None:
    bars, _, _ = _crossing_pair()
    for indicators in ({}, {"vwap": Decimal("100.5")}, {"vwap_prev": Decimal("100")}):
        result = strategy.evaluate(
            _context(bars, indicators=indicators), PARAMS, _watching()
        )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_not_warmed_vwap_is_insufficient_data(strategy: VwapBounceBuy) -> None:
    bars, _, _ = _crossing_pair()
    for indicators in (
        {"vwap": None, "vwap_prev": Decimal("100")},
        {"vwap": Decimal("100.5"), "vwap_prev": None},
        {"vwap": None, "vwap_prev": None},
    ):
        result = strategy.evaluate(
            _context(bars, indicators=indicators), PARAMS, _watching()
        )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize(
    "indicators",
    [
        {"vwap": "100.5", "vwap_prev": Decimal("100")},
        {"vwap": 100.5, "vwap_prev": Decimal("100")},
        {"vwap": Decimal("NaN"), "vwap_prev": Decimal("100")},
        {"vwap": Decimal("-1"), "vwap_prev": Decimal("100")},
        {"vwap": Decimal("0"), "vwap_prev": Decimal("100")},
        {"vwap": Decimal("100.5"), "vwap_prev": Decimal("Infinity")},
    ],
)
def test_invalid_vwap_is_insufficient_data(
    strategy: VwapBounceBuy, indicators: dict[str, object]
) -> None:
    bars, _, _ = _crossing_pair()
    result = strategy.evaluate(
        _stub_context((bars[0], bars[1]), indicators=indicators), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_invalid_bar_is_insufficient_data(strategy: VwapBounceBuy) -> None:
    bars, _, _ = _crossing_pair()
    foreign = _bar(bars[0].start, "99", "100", "98", "99")
    object.__setattr__(foreign, "symbol", "AAPL")
    result = strategy.evaluate(
        _stub_context((foreign, bars[1])), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_zero_volume_bar_is_insufficient_data(strategy: VwapBounceBuy) -> None:
    start = _open_utc(DAY)
    for zero_first in (False, True):
        first_vol, second_vol = ("0", "100") if zero_first else ("100", "0")
        bars = [
            _bar(start, "99", "101", "98", "99", first_vol),
            _bar(start + timedelta(minutes=1), "100", "102", "99", "101", second_vol),
        ]
        result = strategy.evaluate(
            _context(bars, vwap=Decimal("100.5"), vwap_prev=Decimal("100")),
            PARAMS,
            _watching(),
        )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_fewer_than_two_completed_session_bars(strategy: VwapBounceBuy) -> None:
    bars, vwap, vwap_prev = _crossing_pair()
    result = strategy.evaluate(
        _context(bars[:1], vwap=vwap, vwap_prev=vwap_prev), PARAMS, _watching()
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_missing_bar_gap_is_insufficient_data(strategy: VwapBounceBuy) -> None:
    start = _open_utc(DAY)
    bars = [
        _bar(start, "99", "100", "98", "99"),
        _bar(start + timedelta(minutes=1), "99", "100", "98", "99"),
        _bar(start + timedelta(minutes=3), "100", "102", "99", "101"),
    ]
    result = strategy.evaluate(
        _context(bars, vwap=Decimal("100.5"), vwap_prev=Decimal("100")),
        PARAMS,
        _watching(),
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_not_eligible_outside_regular_or_not_watching(
    strategy: VwapBounceBuy,
) -> None:
    bars, vwap, vwap_prev = _crossing_pair()
    for status in (
        SessionStatus.CLOSED,
        SessionStatus.PRE_OPEN,
        SessionStatus.HOLIDAY,
        SessionStatus.UNKNOWN,
    ):
        result = strategy.evaluate(
            _context(bars, session_status=status, vwap=vwap, vwap_prev=vwap_prev),
            PARAMS,
            _watching(),
        )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    for state in ({}, {"eligibility": "FLAT"}, {"eligibility": "WATCHING_SELL"}):
        result = strategy.evaluate(
            _context(bars, vwap=vwap, vwap_prev=vwap_prev), PARAMS, state
        )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.NOT_ELIGIBLE,)


# ------------------------------------------------------------------ replay / rearm / sessions


def _merged(state: dict[str, object], updates: object) -> dict[str, object]:
    assert isinstance(updates, Mapping)
    return {**state, **dict(updates)}


def test_same_bar_replay_is_suppressed(strategy: VwapBounceBuy) -> None:
    bars, vwap, vwap_prev = _crossing_pair()
    ctx = _context(bars, vwap=vwap, vwap_prev=vwap_prev)
    state = _watching()
    first = strategy.evaluate(ctx, PARAMS, state)
    assert first.is_signal
    replay = strategy.evaluate(ctx, PARAMS, _merged(state, first.state_updates))
    assert not replay.is_signal
    assert replay.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert replay.signal_id is None


def test_rearm_on_later_fresh_crossing_with_new_id(
    strategy: VwapBounceBuy,
) -> None:
    bars, vwap, vwap_prev = _crossing_pair()
    first = strategy.evaluate(
        _context(bars, vwap=vwap, vwap_prev=vwap_prev), PARAMS, _watching()
    )
    assert first.is_signal and first.signal_id is not None
    state = _merged(_watching(), first.state_updates)
    # A later bar forms a fresh upward crossing against fresh shared VWAP.
    start = _open_utc(DAY)
    extended = [
        *bars,
        _bar(start + timedelta(minutes=2), "101", "103", "100", "102"),
    ]
    again = strategy.evaluate(
        _context(
            extended, vwap=Decimal("101.5"), vwap_prev=Decimal("102"),
        ),
        PARAMS,
        state,
    )
    assert again.is_signal
    assert again.signal_id is not None
    assert again.signal_id != first.signal_id
    assert again.bar_time == extended[-1].start


def test_session_reset_prior_session_bar_is_not_prior(
    strategy: VwapBounceBuy,
) -> None:
    day1_bars, day1_vwap, day1_prev = _crossing_pair(day=DAY)
    day1_signal = strategy.evaluate(
        _context(day1_bars, day=DAY, vwap=day1_vwap, vwap_prev=day1_prev),
        PARAMS,
        _watching(),
    )
    assert day1_signal.is_signal
    old_state = _merged(_watching(), day1_signal.state_updates)
    assert old_state["session_id"] == DAY.isoformat()

    # One new-session bar plus prior-session history: only one completed bar
    # belongs to the new session, so no crossing can be evaluated.
    day2_lone = _bar(_open_utc(DAY2), "100", "102", "99", "101")
    mixed = [*day1_bars, day2_lone]
    short = strategy.evaluate(
        _context(
            mixed, day=DAY2, vwap=Decimal("100.5"), vwap_prev=Decimal("100"),
        ),
        PARAMS,
        old_state,
    )
    assert not short.is_signal
    assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)

    # A full new-session pair crosses cleanly despite the prior-session signal.
    day2_bars, day2_vwap, day2_prev = _crossing_pair(day=DAY2)
    fresh = strategy.evaluate(
        _context(day2_bars, day=DAY2, vwap=day2_vwap, vwap_prev=day2_prev),
        PARAMS,
        old_state,
    )
    assert fresh.is_signal
    assert fresh.signal_id != day1_signal.signal_id
    assert fresh.state_updates["session_id"] == DAY2.isoformat()


# ------------------------------------------------------------------ hygiene


def test_module_has_no_sell_partner_confidence_or_forbidden_access() -> None:
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "mike_trade"
        / "strategies"
        / "buy"
        / "vwap_bounce.py"
    )
    source = path.read_text(encoding="utf-8")
    for marker in (
        "vwap_breakdown",
        "SellStrategy",
        "strategies.sell",
        "strategies/sell",
    ):
        assert marker not in source, f"forbidden reference {marker!r}"
    tree = ast.parse(source)
    names = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    } | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.arg for node in ast.walk(tree) if isinstance(node, ast.keyword)
    }
    assert "confidence" not in names, "strategy must carry no confidence scores"
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
