"""Unit tests for immutable market contexts, selection, and the reference binder."""

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from mike_trade.market_data.cache import MarketDataCache, MarketDataUpdate
from mike_trade.market_data.candles import (
    Candle,
    CandleBuilder,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    MinuteBar,
    PriceBasis,
    TradeTick,
)
from mike_trade.market_data.context import (
    CODE_CLOCK_REGRESSION,
    CODE_DEVELOPING_IN_COMPLETED,
    CODE_FEED_MISMATCH,
    CODE_FUTURE_DATA,
    CODE_PRICE_BASIS_MISMATCH,
    REASON_DATA_TOO_OLD,
    REASON_NO_DATA,
    REASON_PROVIDER_STALE,
    BoundContexts,
    ContextBindingError,
    ContextBuilder,
    ContextFreshness,
    ContextIntegrityError,
    IndicatorInput,
    IndicatorSpec,
    InvalidContextInputError,
    MarketContext,
    ReferenceSymbolBinder,
    SelectionStatus,
    StrategyInputs,
    UndeclaredSymbolError,
    UnknownIndicatorError,
    select_context,
)
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.indicators.core import (
    CumulativeSessionVolumeCalculator,
    EMACalculator,
    RelativeVolumeCalculator,
    VWAPCalculator,
)

NY = ZoneInfo("America/New_York")
M1, M5 = CandleTimeframe.M1, CandleTimeframe.M5
D = Decimal


def ny(h: int, m: int = 0, s: int = 0) -> datetime:
    """2026-10-08 (EDT, a Thursday trading day) wall time in New York."""
    return datetime(2026, 10, 8, h, m, s, tzinfo=NY)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


def bar(symbol: str, start: datetime, close: str = "100.5", feed: str = "sip", **kw) -> MinuteBar:
    kw.setdefault("consolidated", feed != "iex")
    return MinuteBar(
        symbol=symbol, start=start, open="100", high="101", low="99", close=close,
        volume="1000", feed_id=feed, **kw,
    )


def feed_bars(builder: CandleBuilder, symbol: str, first: datetime, count: int, **kw) -> None:
    for i in range(count):
        builder.on_minute_bar(bar(symbol, first + timedelta(minutes=i), **kw))


def update(symbol: str, kind: str, ts: datetime, feed: str = "sip", **values) -> MarketDataUpdate:
    stale = values.pop("stale", False)
    lag = values.pop("lag", timedelta(0))
    if kind == "quote":
        values = {"bid": D("100.10"), "ask": D("100.20"), "bid_size": 3, "ask_size": 4, **values}
    else:
        values = {"price": D("100.15"), "size": 50, **values}
    return MarketDataUpdate(
        symbol, kind, ts, ts + lag, feed, "test", values, stale
    )


class Env:
    """Candle builder + cache + clock wired into a ContextBuilder."""

    def __init__(self, now: datetime, **builder_kw) -> None:
        self.candles = CandleBuilder()
        self.cache = MarketDataCache()
        self.clock = FakeClock(now)
        builder_kw.setdefault(
            "indicator_specs", {"ema3": IndicatorSpec(M1, lambda: EMACalculator(3))}
        )
        self.builder = ContextBuilder(
            candle_builder=self.candles, quote_trade_cache=self.cache, clock=self.clock,
            **builder_kw,
        )


@pytest.fixture
def env() -> Env:
    e = Env(ny(10, 10))
    feed_bars(e.candles, "AAPL", ny(10, 0), 10)  # 10:00..10:09 closed once 10:10 bar arrives
    e.candles.on_minute_bar(bar("AAPL", ny(10, 10)))  # closes 10:09; 10:10 developing
    lag = timedelta(milliseconds=5)
    e.cache.store_event(update("AAPL", "quote", ny(10, 9, 59), lag=lag))
    e.cache.store_event(update("AAPL", "trade", ny(10, 9, 59), lag=lag))
    return e


# ---------------------------------------------------------------- value object


def test_build_carries_all_sections(env: Env) -> None:
    ctx = env.builder.build("aapl", timeframes=(M1,), indicators=("ema3",))

    assert ctx.key == ("AAPL", ny(10, 10).astimezone(ctx.as_of.tzinfo), 1)
    assert ctx.version == 1
    assert ctx.feed_id == "sip" and ctx.consolidated is True
    assert ctx.price_basis is PriceBasis.RAW
    assert ctx.session_status is SessionStatus.REGULAR
    assert ctx.session_id == "XNYS-2026-10-08"
    assert len(ctx.candles[M1]) == 10
    assert all(c.status is CandleStatus.CLOSED for c in ctx.candles[M1])
    assert ctx.latest_quote.bid == D("100.10") and ctx.latest_quote.ask == D("100.20")
    assert ctx.latest_quote.provider_ts == ny(10, 9, 59)
    assert ctx.latest_quote.receive_ts == ny(10, 9, 59) + timedelta(milliseconds=5)
    assert ctx.latest_trade.price == D("100.15") and ctx.latest_trade.size == D("50")
    assert ctx.indicators["ema3"] == D("100.5")
    assert ctx.research is None
    assert not ctx.freshness.stale and ctx.freshness.reason is None


def test_iex_feed_is_not_consolidated() -> None:
    e = Env(ny(10, 10))
    feed_bars(e.candles, "AAPL", ny(10, 0), 3, feed="iex")
    e.cache.store_event(update("AAPL", "trade", ny(10, 2), feed="iex"))
    e.clock.value = ny(10, 3)
    ctx = e.builder.build("AAPL", timeframes=(M1,))
    assert ctx.feed_id == "iex" and ctx.consolidated is False


def test_collections_are_read_only(env: Env) -> None:
    ctx = env.builder.build(
        "AAPL", timeframes=(M1,), indicators=("ema3",), completed_bars_only=False
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.version = 9  # type: ignore[misc]
    with pytest.raises(TypeError):
        ctx.candles[M5] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.indicators["x"] = D(1)  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.developing_candles[M1] = ctx.candles[M1][0]  # type: ignore[index]
    assert isinstance(ctx.candles[M1], tuple)
    with pytest.raises(AttributeError):
        ctx.candles[M1].append(None)  # type: ignore[attr-defined]


def test_source_mutation_after_build_does_not_leak(env: Env) -> None:
    ctx = env.builder.build("AAPL", timeframes=(M1,))
    before = ctx.candles[M1]
    env.candles.on_minute_bar(bar("AAPL", ny(10, 11)))
    assert ctx.candles[M1] == before


def test_value_object_rejects_developing_candle_in_completed(env: Env) -> None:
    developing = env.candles.get_developing_candle("AAPL", M1)
    assert developing is not None
    ctx = env.builder.build("AAPL", timeframes=(M1,))
    with pytest.raises(ContextIntegrityError) as err:
        dataclasses.replace(ctx, candles={M1: (developing,)})
    assert err.value.code == CODE_DEVELOPING_IN_COMPLETED


def test_value_object_rejects_candle_closing_after_as_of(env: Env) -> None:
    ctx = env.builder.build("AAPL", timeframes=(M1,))
    with pytest.raises(ContextIntegrityError) as err:
        dataclasses.replace(ctx, as_of=ny(10, 5))
    assert err.value.code == CODE_FUTURE_DATA


def test_value_object_rejects_iex_consolidated(env: Env) -> None:
    ctx = env.builder.build("AAPL", timeframes=(M1,))
    with pytest.raises(InvalidContextInputError):
        dataclasses.replace(ctx, feed_id="iex", consolidated=True)


def test_value_object_rejects_non_raw_price_basis(env: Env) -> None:
    ctx = env.builder.build("AAPL", timeframes=(M1,))
    with pytest.raises(ContextIntegrityError) as err:
        dataclasses.replace(ctx, price_basis=PriceBasis.SPLIT_ADJUSTED)
    assert err.value.code == CODE_PRICE_BASIS_MISMATCH


def test_freshness_validates_fields() -> None:
    with pytest.raises(InvalidContextInputError):
        ContextFreshness(None, None, timedelta(seconds=1), True, None)
    with pytest.raises(InvalidContextInputError):
        ContextFreshness(None, None, timedelta(seconds=1), False, "X")


# ---------------------------------------------------------------- versions / clock


def test_versions_increase_monotonically_per_symbol(env: Env) -> None:
    feed_bars(env.candles, "MSFT", ny(10, 0), 2)
    a1 = env.builder.build("AAPL", timeframes=(M1,))
    a2 = env.builder.build("AAPL", timeframes=(M1,))
    m1 = env.builder.build("MSFT", timeframes=(M1,))
    env.clock.value = ny(10, 11)
    a3 = env.builder.build("AAPL", timeframes=(M1,))
    assert (a1.version, a2.version, a3.version, m1.version) == (1, 2, 3, 1)
    assert a1.as_of == a2.as_of and a1.key != a2.key and a3.as_of > a2.as_of


def test_clock_regression_fails_closed_without_burning_a_version(env: Env) -> None:
    env.builder.build("AAPL", timeframes=(M1,))
    env.clock.value = ny(10, 9)
    with pytest.raises(ContextIntegrityError) as err:
        env.builder.build("AAPL", timeframes=(M1,))
    assert err.value.code == CODE_CLOCK_REGRESSION
    env.clock.value = ny(10, 10)
    assert env.builder.build("AAPL", timeframes=(M1,)).version == 2


def test_naive_clock_is_rejected() -> None:
    e = Env(datetime(2026, 10, 8, 10, 0))
    with pytest.raises(InvalidContextInputError):
        e.builder.build("AAPL")


# ---------------------------------------------------------------- completed bars rule


def test_completed_only_context_has_no_developing_candle(env: Env) -> None:
    ctx = env.builder.build("AAPL", timeframes=(M1,), completed_bars_only=True)
    assert ctx.completed_bars_only is True
    assert dict(ctx.developing_candles) == {}
    assert ctx.candles[M1][-1].start == ny(10, 9)  # 10:10 bar is still developing


def test_developing_candle_only_in_separate_field_when_declared(env: Env) -> None:
    ctx = env.builder.build("AAPL", timeframes=(M1,), completed_bars_only=False)
    developing = ctx.developing_candles[M1]
    assert developing.status is CandleStatus.DEVELOPING and developing.start == ny(10, 10)
    assert all(c.status is CandleStatus.CLOSED for c in ctx.candles[M1])
    assert developing not in ctx.candles[M1]


def test_indicators_ignore_developing_candle(env: Env) -> None:
    spec = {"last_close": IndicatorSpec(M1, lambda: EMACalculator(1))}
    e = Env(ny(10, 10), indicator_specs=spec)
    feed_bars(e.candles, "AAPL", ny(10, 0), 3, close="100.5")
    e.candles.on_minute_bar(bar("AAPL", ny(10, 3), close="100.9"))  # closes 10:02
    e.clock.value = ny(10, 4)
    ctx = e.builder.build(
        "AAPL", timeframes=(M1,), indicators=("last_close",), completed_bars_only=False
    )
    assert ctx.developing_candles[M1].close == D("100.9")
    assert ctx.indicators["last_close"] == D("100.5")


def test_default_is_completed_only(env: Env) -> None:
    assert dict(env.builder.build("AAPL", timeframes=(M1,)).developing_candles) == {}


# ---------------------------------------------------------------- indicators


def test_unready_indicator_is_none(env: Env) -> None:
    e = Env(ny(10, 5), indicator_specs={"ema20": IndicatorSpec(M1, lambda: EMACalculator(20))})
    feed_bars(e.candles, "AAPL", ny(10, 0), 3)
    assert e.builder.build("AAPL", timeframes=(M1,), indicators=("ema20",)).indicators == {
        "ema20": None
    }


def test_repeated_builds_do_not_share_calculator_state(env: Env) -> None:
    first = env.builder.build("AAPL", timeframes=(M1,), indicators=("ema3",))
    second = env.builder.build("AAPL", timeframes=(M1,), indicators=("ema3",))
    assert first.indicators["ema3"] == second.indicators["ema3"]


def test_session_and_volume_indicators() -> None:
    specs = {
        "vwap": IndicatorSpec(M1, VWAPCalculator, IndicatorInput.SESSION_BAR),
        "cumvol": IndicatorSpec(
            M1, CumulativeSessionVolumeCalculator, IndicatorInput.SESSION_VOLUME
        ),
        "rvol": IndicatorSpec(M1, lambda: RelativeVolumeCalculator(2), IndicatorInput.VOLUME),
    }
    e = Env(ny(10, 5), indicator_specs=specs)
    feed_bars(e.candles, "AAPL", ny(10, 0), 4)  # 10:00..10:03; 10:03 closes at 10:04 only
    e.candles.on_minute_bar(bar("AAPL", ny(10, 4)))
    ctx = e.builder.build("AAPL", timeframes=(M1,), indicators=("vwap", "cumvol", "rvol"))
    assert ctx.indicators["cumvol"] == D(4000)
    assert abs(ctx.indicators["vwap"] - D("100.1666667")) < D("0.000001")  # (101+99+100.5)/3
    assert ctx.indicators["rvol"] == D(1)


def test_unknown_indicator_raises(env: Env) -> None:
    with pytest.raises(UnknownIndicatorError):
        env.builder.build("AAPL", timeframes=(M1,), indicators=("nope",))


# ---------------------------------------------------------------- feed / basis integrity


def test_mixed_candle_feeds_fail_closed() -> None:
    sip, iex = CandleBuilder(), CandleBuilder()
    feed_bars(sip, "AAPL", ny(10, 0), 3, feed="sip")
    feed_bars(iex, "AAPL", ny(10, 3), 3, feed="iex")

    class MixedSource:
        """Serves 1m candles from sip and 5m candles from iex (non-overlapping history)."""

        def get_completed_candles(self, symbol, timeframe, limit=None):
            return (sip if timeframe is M1 else iex).get_completed_candles(symbol, M1, limit)

        def get_developing_candle(self, symbol, timeframe):
            return None

    assert sip.get_completed_candles("AAPL", M1) and iex.get_completed_candles("AAPL", M1)
    builder = ContextBuilder(
        candle_builder=MixedSource(), quote_trade_cache=MarketDataCache(),  # type: ignore[arg-type]
        clock=FakeClock(ny(10, 10)),
    )
    with pytest.raises(ContextIntegrityError) as err:
        builder.build("AAPL", timeframes=(M1,))  # single feed is fine
        builder.build("AAPL", timeframes=(M1, M5))
    assert err.value.code == CODE_FEED_MISMATCH


def test_value_object_rejects_mixed_feeds_within_candles() -> None:
    sip, iex = CandleBuilder(), CandleBuilder()
    feed_bars(sip, "AAPL", ny(10, 0), 3, feed="sip")
    feed_bars(iex, "AAPL", ny(10, 3), 3, feed="iex")
    ctx = Env(ny(10, 10)).builder.build("AAPL")
    mixed = (*sip.get_completed_candles("AAPL", M1), *iex.get_completed_candles("AAPL", M1))
    with pytest.raises(ContextIntegrityError) as err:
        dataclasses.replace(ctx, candles={M1: mixed})
    assert err.value.code == CODE_FEED_MISMATCH


def test_quote_feed_differing_from_candle_feed_fails_closed(env: Env) -> None:
    env.cache.reset_feed("AAPL")
    env.cache.store_event(update("AAPL", "quote", ny(10, 10), feed="iex"))
    with pytest.raises(ContextIntegrityError) as err:
        env.builder.build("AAPL", timeframes=(M1,))
    assert err.value.code == CODE_FEED_MISMATCH
    # fail closed means no version consumed and no context returned
    env.cache.reset_feed("AAPL")
    env.cache.store_event(update("AAPL", "quote", ny(10, 10)))
    assert env.builder.build("AAPL", timeframes=(M1,)).version == 1


def test_trade_and_quote_feed_mismatch_fails_closed() -> None:
    e = Env(ny(10, 10))
    e.cache.store_event(update("AAPL", "quote", ny(10, 10), feed="sip"))
    ctx = e.builder.build("AAPL")
    trade = update("AAPL", "trade", ny(10, 10), feed="iex")
    from mike_trade.market_data.context import TradeSnapshot  # noqa: PLC0415

    snap = TradeSnapshot(
        "AAPL", D("100"), D("1"), trade.provider_source_ts, trade.receive_ts, "iex"
    )
    with pytest.raises(ContextIntegrityError) as err:
        dataclasses.replace(ctx, latest_trade=snap)
    assert err.value.code == CODE_FEED_MISMATCH


def test_split_adjusted_history_with_raw_prices_fails_closed() -> None:
    e = Env(ny(10, 10))
    feed_bars(e.candles, "AAPL", ny(10, 0), 3, price_basis=PriceBasis.SPLIT_ADJUSTED)
    e.candles.on_minute_bar(
        bar("AAPL", ny(10, 3), price_basis=PriceBasis.SPLIT_ADJUSTED)
    )
    e.cache.store_event(update("AAPL", "quote", ny(10, 4)))
    with pytest.raises(ContextIntegrityError) as err:
        e.builder.build("AAPL", timeframes=(M1,))
    assert err.value.code == CODE_PRICE_BASIS_MISMATCH


def test_split_adjusted_history_without_quotes_still_fails_closed() -> None:
    e = Env(ny(10, 10))
    feed_bars(e.candles, "AAPL", ny(10, 0), 3, price_basis=PriceBasis.SPLIT_ADJUSTED)
    e.candles.on_minute_bar(bar("AAPL", ny(10, 3), price_basis=PriceBasis.SPLIT_ADJUSTED))
    with pytest.raises(ContextIntegrityError) as err:
        e.builder.build("AAPL", timeframes=(M1,))
    assert err.value.code == CODE_PRICE_BASIS_MISMATCH


def test_split_adjusted_developing_candle_fails_closed() -> None:
    e = Env(ny(10, 10))
    e.candles.on_trade(
        TradeTick("AAPL", ny(10, 0, 5), D(100), D(1), "sip", True, PriceBasis.SPLIT_ADJUSTED)
    )
    with pytest.raises(ContextIntegrityError) as err:
        e.builder.build("AAPL", timeframes=(M1,), completed_bars_only=False)
    assert err.value.code == CODE_PRICE_BASIS_MISMATCH


def test_future_quote_is_rejected(env: Env) -> None:
    env.cache.store_event(update("AAPL", "quote", ny(10, 11)))
    with pytest.raises(ContextIntegrityError) as err:
        env.builder.build("AAPL", timeframes=(M1,))
    assert err.value.code == CODE_FUTURE_DATA


def test_malformed_quote_is_rejected() -> None:
    e = Env(ny(10, 10))
    e.cache.store_event(update("AAPL", "quote", ny(10, 10), bid=D(0)))
    with pytest.raises(InvalidContextInputError):
        e.builder.build("AAPL")


# ---------------------------------------------------------------- freshness


def test_no_data_is_stale_with_reason() -> None:
    ctx = Env(ny(10, 10)).builder.build("AAPL", timeframes=(M1,))
    assert ctx.freshness.stale and ctx.freshness.reason == REASON_NO_DATA
    assert ctx.freshness.age is None and ctx.feed_id is None and ctx.consolidated is False


def test_old_data_is_stale() -> None:
    e = Env(ny(10, 10), max_age=timedelta(seconds=30))
    e.cache.store_event(update("AAPL", "quote", ny(10, 9)))
    ctx = e.builder.build("AAPL")
    assert ctx.freshness.stale and ctx.freshness.reason == REASON_DATA_TOO_OLD
    assert ctx.freshness.age == timedelta(minutes=1)
    assert ctx.freshness.newest_data_ts == ny(10, 9)


def test_age_boundary_is_not_stale() -> None:
    e = Env(ny(10, 10), max_age=timedelta(seconds=60))
    e.cache.store_event(update("AAPL", "quote", ny(10, 9)))
    assert not e.builder.build("AAPL").freshness.stale


def test_provider_stale_flag_marks_context_stale() -> None:
    e = Env(ny(10, 10))
    e.cache.store_event(update("AAPL", "quote", ny(10, 10), stale=True))
    ctx = e.builder.build("AAPL")
    assert ctx.freshness.stale and ctx.freshness.reason == REASON_PROVIDER_STALE


def test_freshness_uses_newest_of_quote_trade_and_closed_candle() -> None:
    e = Env(ny(10, 10), max_age=timedelta(seconds=30))
    feed_bars(e.candles, "AAPL", ny(10, 0), 2)
    e.candles.on_minute_bar(bar("AAPL", ny(10, 2)))  # closed candle 10:01 ends 10:02
    e.cache.store_event(update("AAPL", "trade", ny(10, 9, 50)))
    ctx = e.builder.build("AAPL", timeframes=(M1,))
    assert ctx.freshness.newest_data_ts == ny(10, 9, 50) and not ctx.freshness.stale


def test_developing_candle_does_not_refresh_staleness() -> None:
    e = Env(ny(10, 10), max_age=timedelta(seconds=30))
    e.candles.on_trade(TradeTick("AAPL", ny(10, 9, 59), D(100), D(1), "sip", True))
    ctx = e.builder.build("AAPL", timeframes=(M1,), completed_bars_only=False)
    assert ctx.freshness.stale and ctx.freshness.reason == REASON_NO_DATA


# ---------------------------------------------------------------- session


@pytest.mark.parametrize(
    ("now", "status"),
    [
        (ny(8, 0), SessionStatus.PRE_OPEN),
        (ny(12, 0), SessionStatus.REGULAR),
        (ny(17, 0), SessionStatus.CLOSED),
    ],
)
def test_session_status_and_id_from_calendar(now: datetime, status: SessionStatus) -> None:
    ctx = Env(now).builder.build("AAPL")
    assert ctx.session_status is status
    assert ctx.session_id == "XNYS-2026-10-08"


def test_non_trading_day_has_no_session_id() -> None:
    ctx = Env(datetime(2026, 10, 10, 12, 0, tzinfo=NY)).builder.build("AAPL")  # Saturday
    assert ctx.session_status is SessionStatus.CLOSED and ctx.session_id is None


def test_injected_calendar_is_used() -> None:
    class FixedCalendar:
        def session_status(self, at_utc: datetime) -> SessionStatus:
            return SessionStatus.UNKNOWN

        def session_id(self, at_utc: datetime) -> str | None:
            return "TEST-1"

    ctx = Env(ny(12), calendar=FixedCalendar()).builder.build("AAPL")
    assert (ctx.session_status, ctx.session_id) == (SessionStatus.UNKNOWN, "TEST-1")


# ---------------------------------------------------------------- research


def test_research_is_optional_and_passed_through(env: Env) -> None:
    from mike_trade.research.service import ResearchContext  # noqa: PLC0415

    research = ResearchContext(as_of=ny(10, 10))
    ctx = env.builder.build("AAPL", timeframes=(M1,), research=research)
    assert ctx.research is research
    with pytest.raises(InvalidContextInputError):
        env.builder.build("AAPL", timeframes=(M1,), research={"a": 1})  # type: ignore[arg-type]


# ---------------------------------------------------------------- no I/O / no fetch


def test_builder_only_reads_sources(env: Env) -> None:
    class Spy:
        def __init__(self, inner):
            self.inner, self.calls = inner, []

        def __getattr__(self, name):
            self.calls.append(name)
            return getattr(self.inner, name)

    candles, cache = Spy(env.candles), Spy(env.cache)
    builder = ContextBuilder(
        candle_builder=candles, quote_trade_cache=cache, clock=env.clock,  # type: ignore[arg-type]
        indicator_specs={"ema3": IndicatorSpec(M1, lambda: EMACalculator(3))},
    )
    builder.build("AAPL", timeframes=(M1,), indicators=("ema3",), completed_bars_only=False)
    assert set(candles.calls) <= {"get_completed_candles", "get_developing_candle"}
    assert set(cache.calls) == {"latest_quote", "latest_trade"}


def test_empty_cache_symbol_yields_empty_but_valid_context() -> None:
    ctx = Env(ny(10, 10)).builder.build("ZZZZ", timeframes=(M1, M5))
    assert ctx.candles[M1] == () and ctx.candles[M5] == ()
    assert ctx.latest_quote is None and ctx.latest_trade is None


# ---------------------------------------------------------------- selection


def make_history(env: Env, times: list[datetime]) -> list[MarketContext]:
    history = []
    for t in times:
        env.clock.value = t
        env.cache.store_event(update("AAPL", "quote", t))
        history.append(env.builder.build("AAPL"))
    return history


def test_select_returns_newest_at_or_before_evaluation_time() -> None:
    e = Env(ny(10, 0))
    history = make_history(e, [ny(10, 0), ny(10, 0, 10), ny(10, 0, 20)])
    result = select_context(history, ny(10, 0, 15), timedelta(seconds=30))
    assert result.status is SelectionStatus.FRESH and result.is_fresh
    assert result.context is history[1]
    assert result.age == timedelta(seconds=5)


def test_select_exact_boundaries() -> None:
    e = Env(ny(10, 0))
    history = make_history(e, [ny(10, 0)])
    assert select_context(history, ny(10, 0), timedelta(seconds=1)).context is history[0]
    assert select_context(history, ny(10, 0, 1), timedelta(seconds=1)).is_fresh
    late = select_context(history, ny(10, 0, 2), timedelta(seconds=1))
    assert late.status is SelectionStatus.STALE


def test_select_never_uses_future_context() -> None:
    e = Env(ny(10, 0))
    history = make_history(e, [ny(10, 1)])
    result = select_context(history, ny(10, 0, 30), timedelta(minutes=5))
    assert result.status is SelectionStatus.MISSING and result.context is None
    assert result.reason


def test_select_empty_history_is_missing() -> None:
    result = select_context([], ny(10, 0), timedelta(seconds=5))
    assert result.status is SelectionStatus.MISSING and result.context is None


def test_select_too_old_is_explicit_stale_not_old_snapshot() -> None:
    e = Env(ny(10, 0))
    history = make_history(e, [ny(10, 0), ny(10, 0, 5)])
    result = select_context(history, ny(10, 5), timedelta(seconds=30))
    assert result.status is SelectionStatus.STALE
    assert result.context is None and not result.is_fresh
    assert result.reason == REASON_DATA_TOO_OLD
    assert result.age == timedelta(minutes=4, seconds=55)


def test_select_skips_context_flagged_stale_by_data() -> None:
    e = Env(ny(10, 0), max_age=timedelta(seconds=10))
    e.cache.store_event(update("AAPL", "quote", ny(10, 0)))
    good = e.builder.build("AAPL")
    e.clock.value = ny(10, 0, 20)  # quote is now 20s old -> context flagged stale
    stale = e.builder.build("AAPL")
    assert stale.is_stale and not good.is_stale

    only_stale = select_context([stale], ny(10, 0, 21), timedelta(seconds=30))
    assert only_stale.status is SelectionStatus.STALE and only_stale.context is None
    assert only_stale.reason == REASON_DATA_TOO_OLD

    # an older context that was genuinely fresh and is within max_age remains eligible
    both = select_context([good, stale], ny(10, 0, 21), timedelta(seconds=30))
    assert both.context is good


def test_select_breaks_as_of_ties_by_version() -> None:
    e = Env(ny(10, 0))
    e.cache.store_event(update("AAPL", "quote", ny(10, 0)))
    first, second = e.builder.build("AAPL"), e.builder.build("AAPL")
    assert select_context([second, first], ny(10, 0), timedelta(seconds=5)).context is second


def test_select_validates_inputs() -> None:
    e = Env(ny(10, 0))
    history = make_history(e, [ny(10, 0)])
    with pytest.raises(InvalidContextInputError):
        select_context(history, datetime(2026, 10, 8, 10, 0), timedelta(seconds=1))
    with pytest.raises(InvalidContextInputError):
        select_context(history, ny(10, 0), timedelta(0))
    with pytest.raises(InvalidContextInputError):
        select_context(["x"], ny(10, 0), timedelta(seconds=1))  # type: ignore[list-item]
    other = Env(ny(10, 0))
    other.cache.store_event(update("MSFT", "quote", ny(10, 0)))
    with pytest.raises(InvalidContextInputError):
        select_context([*history, other.builder.build("MSFT")], ny(10, 0), timedelta(seconds=1))


# ---------------------------------------------------------------- strategy inputs / binder


def test_strategy_inputs_normalise_and_validate() -> None:
    inputs = StrategyInputs(" aapl ", ("spy", "qqq"), ("1m", M5, "1m"), ("ema3", "ema3"))
    assert inputs.primary_symbol == "AAPL"
    assert inputs.reference_symbols == ("SPY", "QQQ")
    assert inputs.timeframes == (M1, M5)
    assert inputs.indicators == ("ema3",)
    assert inputs.symbols == ("AAPL", "SPY", "QQQ")
    assert inputs.completed_bars_only is True
    with pytest.raises(InvalidContextInputError):
        StrategyInputs("AAPL", ("aapl",))
    with pytest.raises(InvalidContextInputError):
        StrategyInputs("AAPL", ("SPY", "spy"))
    with pytest.raises(InvalidContextInputError):
        StrategyInputs("AAPL", timeframes=("2m",))  # type: ignore[arg-type]
    with pytest.raises(InvalidContextInputError):
        StrategyInputs("AAPL", reference_symbols="SPY")  # type: ignore[arg-type]


@pytest.fixture
def multi() -> Env:
    e = Env(ny(10, 10))
    for symbol in ("AAPL", "SPY", "QQQ"):
        feed_bars(e.candles, symbol, ny(10, 0), 10)
        e.candles.on_minute_bar(bar(symbol, ny(10, 10)))
        e.cache.store_event(update(symbol, "quote", ny(10, 10)))
    return e


def test_binder_exposes_only_declared_symbols(multi: Env) -> None:
    inputs = StrategyInputs("AAPL", ("SPY",), (M1,), ("ema3",))
    bound = ReferenceSymbolBinder(multi.builder).build(inputs)

    assert isinstance(bound, BoundContexts)
    assert bound.symbols == ("AAPL", "SPY")
    assert bound.primary.symbol == "AAPL"
    assert bound["spy"].symbol == "SPY" and bound.reference("SPY").symbol == "SPY"
    assert "SPY" in bound and "QQQ" not in bound
    with pytest.raises(UndeclaredSymbolError) as err:
        bound["QQQ"]
    assert err.value.code == "undeclared_symbol"
    with pytest.raises(UndeclaredSymbolError):
        bound.reference("QQQ")
    with pytest.raises(UndeclaredSymbolError):
        bound.reference("AAPL")  # primary is not a reference symbol
    with pytest.raises(UndeclaredSymbolError):
        bound[None]  # type: ignore[index]
    assert "QQQ" not in str(bound.inputs)


def test_binder_builds_declared_symbols_at_one_shared_as_of(multi: Env) -> None:
    bound = ReferenceSymbolBinder(multi.builder).build(
        StrategyInputs("AAPL", ("SPY", "QQQ"), (M1,))
    )
    assert len({bound[s].as_of for s in bound.symbols}) == 1


def test_binder_does_not_build_undeclared_symbols(multi: Env) -> None:
    ReferenceSymbolBinder(multi.builder).build(StrategyInputs("AAPL", ("SPY",), (M1,)))
    assert multi.builder._versions.keys() == {"AAPL", "SPY"}


def test_binder_restricts_each_context_to_declared_inputs(multi: Env) -> None:
    prebuilt = {
        s: multi.builder.build(
            s, timeframes=(M1, M5), indicators=("ema3",), completed_bars_only=False
        )
        for s in ("AAPL", "SPY", "QQQ")
    }
    inputs = StrategyInputs("AAPL", ("SPY",), (M1,), (), completed_bars_only=True)
    bound = ReferenceSymbolBinder().bind(inputs, prebuilt)
    spy = bound["SPY"]
    assert set(spy.candles) == {M1} and dict(spy.indicators) == {}
    assert dict(spy.developing_candles) == {} and spy.completed_bars_only is True
    assert "QQQ" not in bound
    assert prebuilt["SPY"].developing_candles  # source context untouched


def test_binder_keeps_declared_developing_candle(multi: Env) -> None:
    inputs = StrategyInputs("AAPL", ("SPY",), (M1,), completed_bars_only=False)
    bound = ReferenceSymbolBinder(multi.builder).build(inputs)
    assert bound["SPY"].developing_candles[M1].status is CandleStatus.DEVELOPING
    assert bound["SPY"].completed_bars_only is False


def test_binder_completed_only_reference_has_no_developing(multi: Env) -> None:
    bound = ReferenceSymbolBinder(multi.builder).build(StrategyInputs("AAPL", ("SPY",), (M1,)))
    for symbol in bound.symbols:
        assert dict(bound[symbol].developing_candles) == {}
        assert all(c.is_closed for c in bound[symbol].candles[M1])


def test_binder_fails_when_declared_context_or_input_missing(multi: Env) -> None:
    aapl = multi.builder.build("AAPL", timeframes=(M1,))
    binder = ReferenceSymbolBinder()
    with pytest.raises(ContextBindingError) as err:
        binder.bind(StrategyInputs("AAPL", ("SPY",), (M1,)), {"AAPL": aapl})
    assert err.value.code == "missing_context"
    with pytest.raises(ContextBindingError) as err:
        binder.bind(StrategyInputs("AAPL", (), (M5,)), {"AAPL": aapl})
    assert err.value.code == "missing_declared_input"
    with pytest.raises(ContextBindingError):
        binder.bind(StrategyInputs("AAPL", (), (M1,)), {"AAPL": multi.builder.build("SPY")})
    with pytest.raises(ContextBindingError):
        binder.build(StrategyInputs("AAPL"))  # no builder injected


def test_binder_propagates_integrity_failures_for_reference_symbols(multi: Env) -> None:
    multi.cache.reset_feed("SPY")
    multi.cache.store_event(update("SPY", "quote", ny(10, 10), feed="iex"))
    with pytest.raises(ContextIntegrityError) as err:
        ReferenceSymbolBinder(multi.builder).build(StrategyInputs("AAPL", ("SPY",), (M1,)))
    assert err.value.code == CODE_FEED_MISMATCH


def test_bound_view_is_immutable(multi: Env) -> None:
    bound = ReferenceSymbolBinder(multi.builder).build(StrategyInputs("AAPL", ("SPY",), (M1,)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        bound.inputs = None  # type: ignore[misc]
    with pytest.raises(TypeError):
        bound._contexts["QQQ"] = bound.primary  # type: ignore[index]


# ---------------------------------------------------------------- misc contract checks


def test_candle_types_are_h1_candles(env: Env) -> None:
    ctx = env.builder.build("AAPL", timeframes=(M1,))
    assert all(isinstance(c, Candle) for c in ctx.candles[M1])
    assert ctx.candles[M1][0].origin is CandleOrigin.STREAM_BAR


def test_context_layer_makes_no_trading_decisions() -> None:
    import mike_trade.market_data.context as module  # noqa: PLC0415

    public = {name.lower() for name in module.__all__}
    assert not any(word in name for name in public for word in ("signal", "buy", "sell", "order"))
