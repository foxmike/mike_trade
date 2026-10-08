"""Unit tests for the VWAP breakdown and momentum reversal SELL strategies."""

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
from mike_trade.strategies.sell.vwap_breakdown import (
    STRATEGY_ID as VWAP_ID,
    STRATEGY_VERSION as VWAP_VERSION,
    WARMUP_BARS as VWAP_WARMUP,
    VwapBreakdownSell,
)
from mike_trade.strategies.sell.momentum_reversal import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_REVERSAL_THRESHOLD,
    STRATEGY_ID as MOM_ID,
    STRATEGY_VERSION as MOM_VERSION,
    WARMUP_BARS as MOM_WARMUP,
    MomentumReversalSell,
)

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 8)
DAY2 = date(2026, 10, 9)
SYMBOL = "SPY"
VWAP_PARAMS: dict[str, object] = {}
MOM_LOOKBACK = 3
MOM_THRESHOLD = Decimal("-0.02")
MOM_PARAMS: dict[str, object] = {
    "lookback_bars": MOM_LOOKBACK,
    "reversal_threshold": MOM_THRESHOLD,
}
OPENED = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)


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
    indicators: Mapping[str, Decimal | None] | None = None,
) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=10)
    newest = bars[-1].end
    values = (
        {"vwap": Decimal("100"), "vwap_prev": Decimal("100")}
        if indicators is None
        else dict(indicators)
    )
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
    session_status: SessionStatus = SessionStatus.REGULAR,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=SYMBOL,
        freshness=SimpleNamespace(stale=False),
        session_status=session_status,
        session_id=DAY.isoformat(),
        completed_bars_only=True,
        feed_id="sip",
        candles={CandleTimeframe.M1: bars},
        indicators=dict(indicators)
        if indicators is not None
        else {"vwap": Decimal("100"), "vwap_prev": Decimal("100")},
    )


def _crossing_pair(
    day: date = DAY,
    *,
    prior_close: str = "100",
    latest_close: str = "99",
    vwap: str = "100",
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


def _mom_bars(
    closes: list[str], day: date = DAY, volume: str = "100"
) -> list[Candle]:
    start = _open_utc(day)
    bars = []
    for i, c in enumerate(closes):
        cc = Decimal(c)
        bars.append(
            _bar(
                start + timedelta(minutes=i),
                c,
                str(cc + 1),
                str(cc - 1),
                c,
                volume,
            )
        )
    return bars


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
def vwap() -> VwapBreakdownSell:
    return VwapBreakdownSell()


@pytest.fixture()
def mom() -> MomentumReversalSell:
    return MomentumReversalSell()


# ------------------------------------------------------------------ specs


def test_vwap_spec_contract_and_registration() -> None:
    assert issubclass(VwapBreakdownSell, SellStrategy)
    assert issubclass(VwapBreakdownSell, SellRule)
    spec = VwapBreakdownSell.spec
    assert spec.strategy_id == VWAP_ID == "vwap_breakdown"
    assert spec.version == VWAP_VERSION
    assert spec.version_key == (1, 0, 0)
    assert spec.side is StrategyRole.SELL
    assert spec.required_timeframes == (CandleTimeframe.M1,)
    assert spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.completed_bars_only is True
    assert spec.requires_volume is True
    assert spec.trigger_kind is TriggerKind.EDGE_ON_CLOSED_BAR
    assert spec.warmup_bars == VWAP_WARMUP == 2
    assert spec.persistent_state_keys == ("session_id", "last_signal_bar")
    assert "cross" in spec.methodology.lower()
    assert "vwap" in spec.methodology.lower()
    assert "profitability" in (spec.methodology + spec.source).lower()
    assert "not" in (spec.methodology + spec.source).lower()
    assert spec.source.strip() and spec.methodology.strip()
    assert len(spec.execution_limitations) >= 1
    joined = " ".join(spec.execution_limitations).lower()
    assert "advisory" in joined
    assert "min hold" in joined or "minimum hold" in joined
    assert "arbitration" in joined
    assert "short" in joined
    for code in (
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ):
        assert code in spec.reason_codes

    registry = StrategyRegistry()
    assert registry.register(VwapBreakdownSell) is VwapBreakdownSell
    assert registry.get("vwap_breakdown") is VwapBreakdownSell
    assert registry.get("vwap_breakdown", "1.0.0") is VwapBreakdownSell
    assert registry.validate_parameters("vwap_breakdown", {}) == {}
    # Registered independently: no BUY partner is required or present.
    assert registry.list_strategies(StrategyRole.BUY) == ()
    assert registry.list_strategies(StrategyRole.SELL) == (VwapBreakdownSell,)


def test_mom_spec_contract_and_registration() -> None:
    assert issubclass(MomentumReversalSell, SellStrategy)
    assert issubclass(MomentumReversalSell, SellRule)
    spec = MomentumReversalSell.spec
    assert spec.strategy_id == MOM_ID == "momentum_reversal"
    assert spec.version == MOM_VERSION
    assert spec.version_key == (1, 0, 0)
    assert spec.side is StrategyRole.SELL
    assert spec.required_timeframes == (CandleTimeframe.M1,)
    assert spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.completed_bars_only is True
    assert spec.requires_volume is True
    assert spec.trigger_kind is TriggerKind.EDGE_ON_CLOSED_BAR
    assert spec.warmup_bars == MOM_WARMUP == 4
    assert spec.persistent_state_keys == ("session_id", "last_signal_bar")
    assert "roc" in spec.methodology.lower() or "rate" in spec.methodology.lower()
    assert "profitability" in (spec.methodology + spec.source).lower()
    assert spec.source.strip() and spec.methodology.strip()
    assert len(spec.execution_limitations) >= 1
    joined = " ".join(spec.execution_limitations).lower()
    assert "advisory" in joined
    assert "short" in joined
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
    assert validated["reversal_threshold"] == DEFAULT_REVERSAL_THRESHOLD
    assert isinstance(validated["reversal_threshold"], Decimal)

    registry = StrategyRegistry()
    assert registry.register(MomentumReversalSell) is MomentumReversalSell
    assert registry.get("momentum_reversal") is MomentumReversalSell
    assert registry.list_strategies(StrategyRole.BUY) == ()


def test_both_register_independently() -> None:
    registry = StrategyRegistry()
    registry.register(VwapBreakdownSell)
    registry.register(MomentumReversalSell)
    assert registry.list_strategies(StrategyRole.BUY) == ()
    assert {c.spec.strategy_id for c in registry.list_strategies(StrategyRole.SELL)} == {
        "vwap_breakdown",
        "momentum_reversal",
    }


def test_unknown_parameter_is_rejected(vwap: VwapBreakdownSell) -> None:
    bars, vwap_val, vwap_prev = _crossing_pair()
    ctx = _context(bars, indicators={"vwap": vwap_val, "vwap_prev": vwap_prev})
    with pytest.raises(ParameterValidationError):
        vwap.evaluate(ctx, {"bogus": 1}, {})


# ------------------------------------------------- vwap crossing boundaries


def test_prior_close_equal_prior_vwap_with_latest_below_signals(
    vwap: VwapBreakdownSell,
) -> None:
    bars, vwap_val, vwap_prev = _crossing_pair(prior_close="100", latest_close="99")
    ctx = _context(bars, indicators={"vwap": vwap_val, "vwap_prev": vwap_prev})
    first = vwap.evaluate(ctx, VWAP_PARAMS, {})
    assert first.is_signal
    assert first.reason_codes == (ReasonCode.SIGNAL,)
    assert first.bar_time == bars[-1].start
    assert isinstance(first.reference_price, Decimal)
    assert first.reference_price == Decimal("99")
    assert not hasattr(first, "confidence")
    expected = make_signal_id(
        VWAP_ID, VWAP_VERSION, SYMBOL, StrategyRole.SELL, bars[-1].start
    )
    assert first.signal_id == expected
    second = vwap.evaluate(ctx, VWAP_PARAMS, {})
    assert second.signal_id == first.signal_id


def test_latest_close_equal_latest_vwap_is_no_signal(vwap: VwapBreakdownSell) -> None:
    bars, _, _ = _crossing_pair(prior_close="101", latest_close="100")
    result = vwap.evaluate(
        _context(bars, indicators={"vwap": Decimal("100"), "vwap_prev": Decimal("100")}),
        VWAP_PARAMS,
        {},
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_prior_close_below_prior_vwap_is_no_signal(vwap: VwapBreakdownSell) -> None:
    bars, _, _ = _crossing_pair(prior_close="99", latest_close="98")
    result = vwap.evaluate(
        _context(bars, indicators={"vwap": Decimal("100"), "vwap_prev": Decimal("100")}),
        VWAP_PARAMS,
        {},
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_upward_cross_is_no_signal(vwap: VwapBreakdownSell) -> None:
    bars, _, _ = _crossing_pair(prior_close="99", latest_close="101")
    result = vwap.evaluate(
        _context(bars, indicators={"vwap": Decimal("100"), "vwap_prev": Decimal("100")}),
        VWAP_PARAMS,
        {},
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_above_to_above_is_no_signal(vwap: VwapBreakdownSell) -> None:
    bars, _, _ = _crossing_pair(prior_close="101", latest_close="102")
    result = vwap.evaluate(
        _context(bars, indicators={"vwap": Decimal("100"), "vwap_prev": Decimal("100")}),
        VWAP_PARAMS,
        {},
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


# ------------------------------------------------- vwap replay / rearm


def test_same_bar_replay_is_suppressed(vwap: VwapBreakdownSell) -> None:
    bars, vwap_val, vwap_prev = _crossing_pair()
    ctx = _context(bars, indicators={"vwap": vwap_val, "vwap_prev": vwap_prev})
    state: dict[str, object] = {}
    first = vwap.evaluate(ctx, VWAP_PARAMS, state)
    assert first.is_signal
    replay = vwap.evaluate(ctx, VWAP_PARAMS, _merged(state, first.state_updates))
    assert not replay.is_signal
    assert replay.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert replay.signal_id is None


def test_rearm_on_later_fresh_crossing_with_new_id(vwap: VwapBreakdownSell) -> None:
    bars, vwap_val, vwap_prev = _crossing_pair()
    first = vwap.evaluate(
        _context(bars, indicators={"vwap": vwap_val, "vwap_prev": vwap_prev}),
        VWAP_PARAMS,
        {},
    )
    assert first.is_signal and first.signal_id is not None
    state = _merged({}, first.state_updates)
    start = _open_utc(DAY)
    extended = [
        *bars,
        _bar(start + timedelta(minutes=2), "100", "102", "99", "101"),
        _bar(start + timedelta(minutes=3), "101", "102", "98", "99"),
    ]
    again = vwap.evaluate(
        _context(
            extended,
            indicators={"vwap": Decimal("100"), "vwap_prev": Decimal("100")},
        ),
        VWAP_PARAMS,
        state,
    )
    assert again.is_signal
    assert again.signal_id is not None
    assert again.signal_id != first.signal_id
    assert again.bar_time == extended[-1].start


# ------------------------------------------------- vwap gating


def test_stale_data(vwap: VwapBreakdownSell) -> None:
    bars, vwap_val, vwap_prev = _crossing_pair()
    result = vwap.evaluate(
        _context(bars, stale=True, indicators={"vwap": vwap_val, "vwap_prev": vwap_prev}),
        VWAP_PARAMS,
        {},
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.STALE_DATA,)


def test_incomplete_bar_is_insufficient_data(vwap: VwapBreakdownSell) -> None:
    start = _open_utc(DAY)
    developing = Candle(
        symbol=SYMBOL,
        timeframe=CandleTimeframe.M1,
        start=start,
        end=start + timedelta(minutes=1),
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
    closed = _bar(start + timedelta(minutes=1), "101", "102", "98", "99")
    result = vwap.evaluate(_stub_context((developing, closed)), VWAP_PARAMS, {})
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_missing_vwap_is_insufficient_data(vwap: VwapBreakdownSell) -> None:
    bars, _, _ = _crossing_pair()
    for indicators in ({}, {"vwap": Decimal("100")}, {"vwap_prev": Decimal("100")}):
        result = vwap.evaluate(_context(bars, indicators=indicators), VWAP_PARAMS, {})
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_not_warmed_vwap_is_insufficient_data(vwap: VwapBreakdownSell) -> None:
    bars, _, _ = _crossing_pair()
    for indicators in (
        {"vwap": None, "vwap_prev": Decimal("100")},
        {"vwap": Decimal("100"), "vwap_prev": None},
        {"vwap": None, "vwap_prev": None},
    ):
        result = vwap.evaluate(_context(bars, indicators=indicators), VWAP_PARAMS, {})
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize(
    "indicators",
    [
        {"vwap": "100", "vwap_prev": Decimal("100")},
        {"vwap": 100.0, "vwap_prev": Decimal("100")},
        {"vwap": Decimal("NaN"), "vwap_prev": Decimal("100")},
        {"vwap": Decimal("-1"), "vwap_prev": Decimal("100")},
        {"vwap": Decimal("0"), "vwap_prev": Decimal("100")},
        {"vwap": Decimal("100"), "vwap_prev": Decimal("Infinity")},
    ],
)
def test_invalid_vwap_is_insufficient_data(
    vwap: VwapBreakdownSell, indicators: dict[str, object]
) -> None:
    bars, _, _ = _crossing_pair()
    result = vwap.evaluate(
        _stub_context((bars[0], bars[1]), indicators=indicators), VWAP_PARAMS, {}
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_invalid_bar_is_insufficient_data(vwap: VwapBreakdownSell) -> None:
    bars, _, _ = _crossing_pair()
    foreign = _bar(bars[0].start, "100", "101", "99", "100")
    object.__setattr__(foreign, "symbol", "AAPL")
    result = vwap.evaluate(_stub_context((foreign, bars[1])), VWAP_PARAMS, {})
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_zero_volume_bar_is_insufficient_data(vwap: VwapBreakdownSell) -> None:
    start = _open_utc(DAY)
    for zero_first in (False, True):
        first_vol, second_vol = ("0", "100") if zero_first else ("100", "0")
        bars = [
            _bar(start, "100", "101", "99", "100", first_vol),
            _bar(start + timedelta(minutes=1), "101", "102", "98", "99", second_vol),
        ]
        result = vwap.evaluate(
            _context(bars, indicators={"vwap": Decimal("100"), "vwap_prev": Decimal("100")}),
            VWAP_PARAMS,
            {},
        )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_fewer_than_two_completed_session_bars(vwap: VwapBreakdownSell) -> None:
    bars, vwap_val, vwap_prev = _crossing_pair()
    result = vwap.evaluate(
        _context(bars[:1], indicators={"vwap": vwap_val, "vwap_prev": vwap_prev}),
        VWAP_PARAMS,
        {},
    )
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_session_reset_prior_session_bar_is_not_prior(vwap: VwapBreakdownSell) -> None:
    day1_bars, day1_vwap, day1_prev = _crossing_pair(day=DAY)
    day1_signal = vwap.evaluate(
        _context(day1_bars, day=DAY, indicators={"vwap": day1_vwap, "vwap_prev": day1_prev}),
        VWAP_PARAMS,
        {},
    )
    assert day1_signal.is_signal
    old_state = _merged({}, day1_signal.state_updates)
    assert old_state["session_id"] == DAY.isoformat()

    day2_lone = _bar(_open_utc(DAY2), "101", "102", "98", "99")
    mixed = [*day1_bars, day2_lone]
    short = vwap.evaluate(
        _context(mixed, day=DAY2, indicators={"vwap": Decimal("100"), "vwap_prev": Decimal("100")}),
        VWAP_PARAMS,
        old_state,
    )
    assert not short.is_signal
    assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)

    day2_bars, day2_vwap, day2_prev = _crossing_pair(day=DAY2)
    fresh = vwap.evaluate(
        _context(day2_bars, day=DAY2, indicators={"vwap": day2_vwap, "vwap_prev": day2_prev}),
        VWAP_PARAMS,
        old_state,
    )
    assert fresh.is_signal
    assert fresh.signal_id != day1_signal.signal_id
    assert fresh.state_updates["session_id"] == DAY2.isoformat()


def test_not_eligible_outside_regular_session(vwap: VwapBreakdownSell) -> None:
    bars, vwap_val, vwap_prev = _crossing_pair()
    for status in (
        SessionStatus.CLOSED,
        SessionStatus.PRE_OPEN,
        SessionStatus.HOLIDAY,
        SessionStatus.UNKNOWN,
    ):
        result = vwap.evaluate(
            _context(
                bars,
                session_status=status,
                indicators={"vwap": vwap_val, "vwap_prev": vwap_prev},
            ),
            VWAP_PARAMS,
            {},
        )
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.NOT_ELIGIBLE,)


# ------------------------------------------------- position / eligibility


def _eligibility_context() -> MarketContext:
    bars, vwap_val, vwap_prev = _crossing_pair()
    return _context(bars, indicators={"vwap": vwap_val, "vwap_prev": vwap_prev})


def _eligibility(**overrides: object) -> tuple[SellReasonCode, ...]:
    args: dict[str, object] = dict(
        position=_managed_position(),
        confirmed_remaining_quantity=Decimal("7.5"),
        enabled=True,
        context=_eligibility_context(),
        spec=VwapBreakdownSell.spec,
        source=ExitSource.STRATEGY_SELL,
        minimum_hold=timedelta(0),
        evaluation_time=datetime(2026, 10, 8, 14, 30, tzinfo=UTC),
        position_opened_at=OPENED,
    )
    args.update(overrides)
    return evaluate_exit_eligibility(**args)  # type: ignore[arg-type]


def test_flat_position_produces_no_exit() -> None:
    result = _eligibility(position=None)
    assert result[0] is SellReasonCode.NOT_ELIGIBLE
    assert SellReasonCode.FLAT_POSITION in result


def test_unmanaged_position_produces_no_exit() -> None:
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


def test_short_direction_never_eligible() -> None:
    result = _eligibility(direction=PositionDirection.SHORT)
    assert result[0] is SellReasonCode.NOT_ELIGIBLE
    assert SellReasonCode.NOT_LONG in result


def test_minimum_hold_blocks_strategy_exit() -> None:
    result = _eligibility(
        minimum_hold=timedelta(hours=1),
        evaluation_time=datetime(2026, 10, 8, 14, 30, tzinfo=UTC),
        position_opened_at=datetime(2026, 10, 8, 14, 0, tzinfo=UTC),
    )
    assert result[0] is SellReasonCode.NOT_ELIGIBLE
    assert SellReasonCode.MINIMUM_HOLD in result


def test_minimum_hold_elapsed_is_eligible() -> None:
    result = _eligibility(
        minimum_hold=timedelta(minutes=30),
        evaluation_time=datetime(2026, 10, 8, 14, 30, tzinfo=UTC),
        position_opened_at=OPENED,
    )
    assert result == (SellReasonCode.ELIGIBLE,)


def test_full_remaining_quantity_after_partial_fill(vwap: VwapBreakdownSell) -> None:
    # Entered 10, partially exited 4: confirmed remainder is 6.
    remaining = Decimal("6")
    position = _managed_position("6")
    bars, vwap_val, vwap_prev = _crossing_pair()
    ctx = _context(bars, indicators={"vwap": vwap_val, "vwap_prev": vwap_prev})
    evaluation = vwap.evaluate(ctx, VWAP_PARAMS, {})
    assert evaluation.is_signal and evaluation.signal_id is not None
    assert evaluation.bar_time is not None
    recommendation = ExitLongRecommendation(
        signal_id=make_signal_id(
            VWAP_ID, VWAP_VERSION, SYMBOL, StrategyRole.SELL, evaluation.bar_time
        ),
        strategy_id=VWAP_ID,
        version=VWAP_VERSION,
        position=position,
        bar_time=evaluation.bar_time,
        reason_codes=(SellReasonCode.ELIGIBLE,),
    )
    assert recommendation.signal_id == evaluation.signal_id
    assert recommendation.quantity == remaining
    assert recommendation.action is SignalAction.EXIT_LONG
    assert recommendation.symbol == SYMBOL


def test_recommendation_never_shorts_or_partial(vwap: VwapBreakdownSell) -> None:
    bars, vwap_val, vwap_prev = _crossing_pair()
    ctx = _context(bars, indicators={"vwap": vwap_val, "vwap_prev": vwap_prev})
    evaluation = vwap.evaluate(ctx, VWAP_PARAMS, {})
    assert evaluation.is_signal and evaluation.bar_time is not None
    recommendation = ExitLongRecommendation(
        signal_id=make_signal_id(
            VWAP_ID, VWAP_VERSION, SYMBOL, StrategyRole.SELL, evaluation.bar_time
        ),
        strategy_id=VWAP_ID,
        version=VWAP_VERSION,
        position=_managed_position(),
        bar_time=evaluation.bar_time,
        reason_codes=(SellReasonCode.ELIGIBLE,),
    )
    assert recommendation.action is SignalAction.EXIT_LONG
    assert recommendation.quantity == Decimal("7.5")
    assert recommendation.action not in (
        SignalAction.ENTER_SHORT,
        SignalAction.COVER_SHORT,
        SignalAction.ENTER_LONG,
    )


# ------------------------------------------------- momentum reversal


def test_momentum_trigger(mom: MomentumReversalSell) -> None:
    # n=3: roc_prior=(102-100)/100=0.02>0, roc_latest=(97-100)/100=-0.03<=-0.02.
    bars = _mom_bars(["100", "100", "100", "102", "97"])
    ctx = _context(bars, indicators={})
    first = mom.evaluate(ctx, MOM_PARAMS, {})
    assert first.is_signal
    assert first.reason_codes == (ReasonCode.SIGNAL,)
    assert first.bar_time == bars[-1].start
    assert isinstance(first.reference_price, Decimal)
    assert first.reference_price == Decimal("97")
    assert not hasattr(first, "confidence")
    expected = make_signal_id(
        MOM_ID, MOM_VERSION, SYMBOL, StrategyRole.SELL, bars[-1].start
    )
    assert first.signal_id == expected
    second = mom.evaluate(ctx, MOM_PARAMS, {})
    assert second.signal_id == first.signal_id


def test_momentum_no_trigger_while_positive(mom: MomentumReversalSell) -> None:
    bars = _mom_bars(["100", "100", "100", "102", "101"])
    result = mom.evaluate(_context(bars, indicators={}), MOM_PARAMS, {})
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_momentum_boundary_latest_equal_threshold_signals(
    mom: MomentumReversalSell,
) -> None:
    # roc_latest=(98-100)/100=-0.02 exactly at threshold -> signal.
    bars = _mom_bars(["100", "100", "100", "102", "98"])
    result = mom.evaluate(_context(bars, indicators={}), MOM_PARAMS, {})
    assert result.is_signal
    assert result.reason_codes == (ReasonCode.SIGNAL,)


def test_momentum_boundary_prior_equal_zero_does_not_signal(
    mom: MomentumReversalSell,
) -> None:
    # roc_prior=(100-100)/100=0 -> not >0, even with a crash on the latest bar.
    bars = _mom_bars(["100", "100", "100", "100", "97"])
    result = mom.evaluate(_context(bars, indicators={}), MOM_PARAMS, {})
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


def test_momentum_replay_suppressed_and_rearm_new_id(mom: MomentumReversalSell) -> None:
    bars = _mom_bars(["100", "100", "100", "102", "97"])
    ctx = _context(bars, indicators={})
    state: dict[str, object] = {}
    first = mom.evaluate(ctx, MOM_PARAMS, state)
    assert first.is_signal and first.signal_id is not None
    replay = mom.evaluate(ctx, MOM_PARAMS, _merged(state, first.state_updates))
    assert not replay.is_signal
    assert replay.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert replay.signal_id is None
    state = _merged(state, first.state_updates)
    later = _mom_bars(["100", "100", "102", "97", "103", "104", "105", "107", "104"])
    again = mom.evaluate(_context(later, indicators={}), MOM_PARAMS, state)
    assert again.bar_time == later[-1].start
    if again.is_signal:
        assert again.signal_id != first.signal_id
    else:
        # A clean second reversal must still produce a fresh deterministic ID.
        second = _mom_bars(["105", "105", "105", "105", "107", "102"])
        fresh = mom.evaluate(_context(second, indicators={}), MOM_PARAMS, {})
        assert fresh.is_signal
        assert fresh.signal_id != first.signal_id


@pytest.mark.parametrize(
    "params",
    [
        {"lookback_bars": 1, "reversal_threshold": Decimal("-0.02")},
        {"lookback_bars": 101, "reversal_threshold": Decimal("-0.02")},
        {"lookback_bars": 3.0, "reversal_threshold": Decimal("-0.02")},
        {"lookback_bars": "3", "reversal_threshold": Decimal("-0.02")},
        {"lookback_bars": True, "reversal_threshold": Decimal("-0.02")},
        {"lookback_bars": 3, "reversal_threshold": 0.02},
        {"lookback_bars": 3, "reversal_threshold": "−0.02"},
        {"lookback_bars": 3, "reversal_threshold": Decimal("0.02")},
        {"lookback_bars": 3, "reversal_threshold": Decimal("0")},
        {"lookback_bars": 3, "reversal_threshold": Decimal("-0.00001")},
        {"lookback_bars": 3, "reversal_threshold": Decimal("-0.6")},
        {"bogus": 1},
    ],
)
def test_momentum_parameter_validation_failures(
    mom: MomentumReversalSell, params: dict[str, object]
) -> None:
    bars = _mom_bars(["100", "100", "100", "102", "97"])
    with pytest.raises(ParameterValidationError):
        mom.evaluate(_context(bars, indicators={}), params, {})


def test_momentum_decimal_threshold_accepts_decimal() -> None:
    schema = MomentumReversalSell.spec.parameter_schema
    validated = schema.validate({"lookback_bars": 3, "reversal_threshold": Decimal("-0.03")})
    assert validated["reversal_threshold"] == Decimal("-0.03")
    assert isinstance(validated["reversal_threshold"], Decimal)
    assert validated["lookback_bars"] == 3


def test_momentum_stale_and_incomplete(mom: MomentumReversalSell) -> None:
    bars = _mom_bars(["100", "100", "100", "102", "97"])
    stale = mom.evaluate(_context(bars, stale=True, indicators={}), MOM_PARAMS, {})
    assert not stale.is_signal
    assert stale.reason_codes == (ReasonCode.STALE_DATA,)
    short = mom.evaluate(_context(bars[:3], indicators={}), MOM_PARAMS, {})
    assert not short.is_signal
    assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    developing = Candle(
        symbol=SYMBOL,
        timeframe=CandleTimeframe.M1,
        start=bars[0].start,
        end=bars[0].start + timedelta(minutes=1),
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
    stub = _stub_context(tuple([developing, *bars[1:]]), indicators={})
    result = mom.evaluate(stub, MOM_PARAMS, {})  # type: ignore[arg-type]
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    closed = mom.evaluate(
        _context(bars, session_status=SessionStatus.CLOSED, indicators={}),
        MOM_PARAMS,
        {},
    )
    assert not closed.is_signal
    assert closed.reason_codes == (ReasonCode.NOT_ELIGIBLE,)


def test_momentum_zero_volume_is_insufficient(mom: MomentumReversalSell) -> None:
    start = _open_utc(DAY)
    bars = _mom_bars(["100", "100", "100", "102", "97"])
    zeroed = [
        _bar(
            start + timedelta(minutes=i),
            str(b.open),
            str(b.high),
            str(b.low),
            str(b.close),
            "0" if i == len(bars) - 1 else "100",
        )
        for i, b in enumerate(bars)
    ]
    result = mom.evaluate(_context(zeroed, indicators={}), MOM_PARAMS, {})
    assert not result.is_signal
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_momentum_full_exit_recommendation(mom: MomentumReversalSell) -> None:
    bars = _mom_bars(["100", "100", "100", "102", "97"])
    evaluation = mom.evaluate(_context(bars, indicators={}), MOM_PARAMS, {})
    assert evaluation.is_signal and evaluation.bar_time is not None
    recommendation = ExitLongRecommendation(
        signal_id=make_signal_id(
            MOM_ID, MOM_VERSION, SYMBOL, StrategyRole.SELL, evaluation.bar_time
        ),
        strategy_id=MOM_ID,
        version=MOM_VERSION,
        position=_managed_position("6"),
        bar_time=evaluation.bar_time,
        reason_codes=(SellReasonCode.ELIGIBLE,),
    )
    assert recommendation.quantity == Decimal("6")
    assert recommendation.action is SignalAction.EXIT_LONG


# ------------------------------------------------------------------ hygiene


def _assert_no_forbidden_imports(path: Path) -> None:
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


def test_sell_modules_have_no_buy_or_forbidden_references() -> None:
    base = Path(__file__).resolve().parent.parent.parent / "mike_trade" / "strategies" / "sell"
    for filename in ("vwap_breakdown.py", "momentum_reversal.py"):
        path = base / filename
        source = path.read_text(encoding="utf-8")
        for marker in (
            "vwap_bounce",
            "momentum_breakout",
            "BuyStrategy",
            "strategies.buy",
            "strategies/buy",
        ):
            assert marker not in source, f"{filename}: forbidden reference {marker!r}"
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
        assert "confidence" not in names, f"{filename}: must carry no confidence scores"
        _assert_no_forbidden_imports(path)
