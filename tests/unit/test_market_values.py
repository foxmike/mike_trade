"""Positive and negative tests for domain value objects (instruments + market)."""

import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest

from mike_trade.domain import instruments as instr_mod
from mike_trade.domain import market as market_mod
from mike_trade.domain.instruments import (
    AssetClass,
    Instrument,
    MarketSession,
    Money,
    SessionKind,
    TradingSessionPolicy,
    require_non_negative,
    require_positive,
    require_utc,
    to_decimal,
)
from mike_trade.domain.market import (
    Candle,
    DataFreshness,
    FeedProvenance,
    MarketContext,
    MarketEvent,
    MarketEventKind,
    Timeframe,
)

NY = ZoneInfo("America/New_York")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def iex_provenance() -> FeedProvenance:
    return FeedProvenance(provider="alpaca", feed="iex", source="ws", is_consolidated=False)


def sample_session() -> MarketSession:
    return MarketSession(
        session_date=date(2025, 3, 10),
        opens_at=datetime(2025, 3, 10, 8, 0, tzinfo=UTC),
        closes_at=datetime(2025, 3, 11, 0, 0, tzinfo=UTC),
    )


def sample_candle(**over) -> Candle:
    start = over.pop("start", utc(2025, 3, 10, 13, 30))
    params = {
        "symbol": "AAPL",
        "timeframe": Timeframe.M1,
        "start": start,
        "end": start + timedelta(minutes=1),
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100.5"),
        "volume": Decimal("1000"),
        "provenance": iex_provenance(),
    }
    params.update(over)
    return Candle(**params)


# --- numeric helpers ---


@pytest.mark.parametrize("raw", [Decimal("0.0001"), "0.0001", 5, " 12.50 "])
def test_to_decimal_preserves_exact_value(raw):
    out = to_decimal(raw, field="x")
    assert out == to_decimal(str(raw).strip(), field="x")
    assert to_decimal(Decimal("0.5"), field="x") == Decimal("0.5")
    assert to_decimal("0.0001", field="x").as_tuple() == Decimal("0.0001").as_tuple()


def test_to_decimal_int_and_decimal_passthrough():
    assert to_decimal(7, field="x") == Decimal(7)
    assert to_decimal(Decimal("3.14159"), field="x") == Decimal("3.14159")


@pytest.mark.parametrize(
    "raw",
    [1.5, 0.1, True, False, float("nan"), "NaN", "sNaN", "Infinity", "-Infinity",
     "abc", "", "   "],
)
def test_to_decimal_rejects_bad_inputs(raw):
    with pytest.raises(ValueError):
        to_decimal(raw, field="x")


def test_to_decimal_rejects_decimal_nan_and_infinity():
    for bad in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError):
            to_decimal(bad, field="x")


def test_require_non_negative_and_positive():
    assert require_non_negative("0", field="x") == Decimal(0)
    assert require_non_negative(Decimal("0.5"), field="x") == Decimal("0.5")
    with pytest.raises(ValueError):
        require_non_negative("-0.0001", field="x")
    assert require_positive("0.0001", field="x") == Decimal("0.0001")
    with pytest.raises(ValueError):
        require_positive("0", field="x")
    with pytest.raises(ValueError):
        require_positive(-2, field="x")
    with pytest.raises(ValueError):
        require_positive(1.5, field="x")


# --- require_utc ---


def test_require_utc_normalizes_aware_non_utc():
    ny_ts = datetime(2025, 3, 10, 9, 30, tzinfo=NY)
    out = require_utc(ny_ts, field="ts")
    assert out.tzinfo == UTC
    assert out == utc(2025, 3, 10, 13, 30)


def test_require_utc_rejects_naive():
    with pytest.raises(ValueError):
        require_utc(datetime(2025, 3, 10, 9, 30), field="ts")
    with pytest.raises(ValueError):
        require_utc("2025-03-10", field="ts")  # type: ignore[arg-type]


# --- Money ---


def test_money_add_subtract_same_currency():
    m = Money(Decimal("10.25"))
    assert m.currency == "USD"
    assert m.add(Money("5.25")) == Money(Decimal("15.50"))
    assert m.subtract(Money(Decimal("0.25"))) == Money(Decimal("10.00"))


def test_money_rejects_currency_mismatch_and_bad_amounts():
    with pytest.raises(ValueError):
        Money(Decimal("1"), currency="usd")
    with pytest.raises(ValueError):
        Money(Decimal("1"), currency="US")
    with pytest.raises(ValueError):
        Money(Decimal("NaN"))
    with pytest.raises(ValueError):
        Money(Decimal("Infinity"))
    with pytest.raises(ValueError):
        Money(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Money(Decimal("1"), currency="USD").add(Money(Decimal("1"), currency="EUR"))
    with pytest.raises(ValueError):
        Money(Decimal("1")).subtract(Money(Decimal("1"), currency="EUR"))


def test_money_is_frozen():
    m = Money(Decimal("1"))
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        m.amount = Decimal("2")  # type: ignore[misc]


# --- Instrument ---


def test_instrument_normalizes_symbol_and_defaults():
    inst = Instrument(symbol=" aapl ", asset_class=AssetClass.US_EQUITY)
    assert inst.symbol == "AAPL"
    assert inst.currency == "USD"
    assert inst.tradable is None and inst.fractionable is None and inst.shortable is None
    assert inst.primary_exchange is None


def test_instrument_with_flags():
    inst = Instrument(
        symbol="BRK.B",
        asset_class=AssetClass.US_EQUITY,
        primary_exchange="XNYS",
        tradable=True,
        fractionable=False,
        shortable=True,
    )
    assert inst.symbol == "BRK.B"


@pytest.mark.parametrize("bad", ["", " ", "aapl!", "TOOLONGSYMBOL1", "1ABC", "A B", "a_b", 123])
def test_instrument_rejects_bad_symbols(bad):
    with pytest.raises(ValueError):
        Instrument(symbol=bad, asset_class=AssetClass.US_EQUITY)  # type: ignore[arg-type]


def test_instrument_rejects_bad_flags_and_class():
    with pytest.raises(ValueError):
        Instrument(symbol="AAPL", asset_class="US_EQUITY")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Instrument(symbol="AAPL", asset_class=AssetClass.US_EQUITY, tradable=1)  # type: ignore[arg-type]
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        inst = Instrument(symbol="AAPL", asset_class=AssetClass.US_EQUITY)
        inst.symbol = "MSFT"  # type: ignore[misc]


# --- MarketSession ---


def test_market_session_defaults_and_utc_normalization():
    ny_open = datetime(2025, 3, 10, 9, 30, tzinfo=NY)
    s = MarketSession(
        session_date=date(2025, 3, 10),
        opens_at=ny_open,
        closes_at=datetime(2025, 3, 10, 16, 0, tzinfo=NY),
    )
    assert s.exchange == "XNYS"
    assert s.timezone_name == "America/New_York"
    assert s.opens_at == utc(2025, 3, 10, 13, 30)
    assert s.closes_at == utc(2025, 3, 10, 20, 0)


def test_market_session_rejects_bad_bounds():
    with pytest.raises(ValueError):
        MarketSession(
            session_date=date(2025, 3, 10),
            opens_at=datetime(2025, 3, 10, 9, 30),  # naive
            closes_at=utc(2025, 3, 10, 20, 0),
        )
    with pytest.raises(ValueError):
        MarketSession(
            session_date=date(2025, 3, 10),
            opens_at=utc(2025, 3, 10, 20, 0),
            closes_at=utc(2025, 3, 10, 13, 30),
        )
    with pytest.raises(ValueError):
        MarketSession(
            session_date=date(2025, 3, 10),
            opens_at=utc(2025, 3, 10, 13, 30),
            closes_at=utc(2025, 3, 10, 13, 30),
        )


def test_session_local_and_kind_across_dst_boundary():
    # Friday 2025-03-07 is EST (UTC-5); Monday 2025-03-10 is EDT (UTC-4).
    fri = MarketSession(
        session_date=date(2025, 3, 7),
        opens_at=utc(2025, 3, 7, 9, 0),
        closes_at=utc(2025, 3, 8, 1, 0),
    )
    mon = MarketSession(
        session_date=date(2025, 3, 10),
        opens_at=utc(2025, 3, 10, 8, 0),
        closes_at=utc(2025, 3, 11, 0, 0),
    )
    # 9:30 New York maps to different UTC offsets on each side of DST.
    assert fri.local(utc(2025, 3, 7, 14, 30)) == datetime(2025, 3, 7, 9, 30, tzinfo=NY)
    assert mon.local(utc(2025, 3, 10, 13, 30)) == datetime(2025, 3, 10, 9, 30, tzinfo=NY)
    assert fri.local(utc(2025, 3, 7, 14, 30)).utcoffset() == timedelta(hours=-5)
    assert mon.local(utc(2025, 3, 10, 13, 30)).utcoffset() == timedelta(hours=-4)
    # kind_at across the boundary
    assert mon.kind_at(utc(2025, 3, 10, 12, 0)) == SessionKind.PRE_MARKET
    assert mon.kind_at(utc(2025, 3, 10, 13, 30)) == SessionKind.REGULAR
    assert mon.kind_at(utc(2025, 3, 10, 20, 30)) == SessionKind.AFTER_HOURS
    assert mon.kind_at(utc(2025, 3, 11, 1, 0)) == SessionKind.CLOSED
    assert mon.kind_at(utc(2025, 3, 10, 7, 59)) == SessionKind.CLOSED
    with pytest.raises(ValueError):
        mon.kind_at(datetime(2025, 3, 10, 9, 30))  # naive rejected
    with pytest.raises(ValueError):
        mon.local(datetime(2025, 3, 10, 9, 30))


# --- TradingSessionPolicy ---


def test_trading_session_policy_defaults():
    p = TradingSessionPolicy()
    assert p.regular_hours_only is True
    assert p.eod_close_enabled is False
    assert p.entry_cutoff is None
    assert p.eod_close_minutes_before_close is None


def test_trading_session_policy_valid_cutoffs():
    assert TradingSessionPolicy(entry_cutoff=time(9, 45)).entry_cutoff == time(9, 45)
    assert TradingSessionPolicy(entry_cutoff=time(16, 0)).entry_cutoff == time(16, 0)
    assert TradingSessionPolicy(entry_cutoff=30).entry_cutoff == 30
    assert TradingSessionPolicy(entry_cutoff=0).entry_cutoff == 0
    assert TradingSessionPolicy(entry_cutoff=390).entry_cutoff == 390
    p = TradingSessionPolicy(eod_close_enabled=True, eod_close_minutes_before_close=15)
    assert p.eod_close_minutes_before_close == 15


@pytest.mark.parametrize(
    "bad",
    [time(9, 29), time(16, 1), time(8, 0), time(20, 0), -1, 391, True, "15:00"],
)
def test_trading_session_policy_rejects_bad_cutoffs(bad):
    with pytest.raises(ValueError):
        TradingSessionPolicy(entry_cutoff=bad)  # type: ignore[arg-type]


def test_trading_session_policy_rejects_bad_eod():
    with pytest.raises(ValueError):
        TradingSessionPolicy(eod_close_minutes_before_close=-1)
    with pytest.raises(ValueError):
        TradingSessionPolicy(eod_close_minutes_before_close=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TradingSessionPolicy(eod_close_minutes_before_close=1.5)  # type: ignore[arg-type]


# --- Timeframe / provenance ---


def test_timeframe_durations():
    assert Timeframe.M1.duration == timedelta(minutes=1)
    assert Timeframe.M5.duration == timedelta(minutes=5)
    assert Timeframe.M15.duration == timedelta(minutes=15)
    assert Timeframe.H1.duration == timedelta(hours=1)
    assert Timeframe.D1.duration == timedelta(days=1)


def test_iex_provenance_not_consolidated():
    prov = FeedProvenance(provider="alpaca", feed="iex", source="ws", is_consolidated=False)
    assert prov.is_consolidated is False
    sip = FeedProvenance(provider="alpaca", feed="sip", source="ws", is_consolidated=True)
    assert sip.is_consolidated is True
    with pytest.raises(ValueError):
        FeedProvenance(provider="", feed="iex", source="ws", is_consolidated=False)
    with pytest.raises(ValueError):
        FeedProvenance(provider="alpaca", feed="iex", source="ws", is_consolidated=0)  # type: ignore[arg-type]
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        prov.feed = "sip"  # type: ignore[misc]


# --- MarketEvent ---


def test_market_event_trade_positive():
    ev = MarketEvent(
        symbol="aapl",
        kind=MarketEventKind.TRADE,
        event_time=utc(2025, 3, 10, 13, 30, 1),
        received_at=utc(2025, 3, 10, 13, 30, 2),
        provenance=iex_provenance(),
        price=Decimal("0.0001"),
        size=Decimal("0.5"),
    )
    assert ev.symbol == "AAPL"
    assert ev.price == Decimal("0.0001")
    assert ev.size == Decimal("0.5")


def test_market_event_quote_positive():
    ev = MarketEvent(
        symbol="AAPL",
        kind=MarketEventKind.QUOTE,
        event_time=datetime(2025, 3, 10, 9, 30, tzinfo=NY),
        received_at=utc(2025, 3, 10, 13, 30, 1),
        provenance=iex_provenance(),
        bid=Decimal("100"),
        ask=Decimal("100.05"),
        bid_size=Decimal("3"),
        ask_size=Decimal("2"),
    )
    assert ev.event_time == utc(2025, 3, 10, 13, 30)  # aware non-UTC normalized
    assert ev.bid <= ev.ask


@pytest.mark.parametrize(
    "kwargs",
    [
        {"price": Decimal("0"), "size": Decimal("1")},  # zero price
        {"price": Decimal("-1"), "size": Decimal("1")},  # negative price
        {"price": Decimal("1"), "size": Decimal("-1")},  # negative size
        {"price": 1.5, "size": Decimal("1")},  # float price
        {"price": Decimal("NaN"), "size": Decimal("1")},
        {"price": Decimal("Infinity"), "size": Decimal("1")},
        {"size": Decimal("1")},  # missing price
        {"price": Decimal("1")},  # missing size
    ],
)
def test_market_event_trade_negative(kwargs):
    with pytest.raises(ValueError):
        MarketEvent(
            symbol="AAPL",
            kind=MarketEventKind.TRADE,
            event_time=utc(2025, 3, 10, 13, 30),
            received_at=utc(2025, 3, 10, 13, 30, 1),
            provenance=iex_provenance(),
            **kwargs,
        )


def test_market_event_quote_rejects_crossed_and_naive():
    with pytest.raises(ValueError):  # bid > ask
        MarketEvent(
            symbol="AAPL",
            kind=MarketEventKind.QUOTE,
            event_time=utc(2025, 3, 10, 13, 30),
            received_at=utc(2025, 3, 10, 13, 30, 1),
            provenance=iex_provenance(),
            bid=Decimal("101"),
            ask=Decimal("100"),
        )
    with pytest.raises(ValueError):  # negative bid
        MarketEvent(
            symbol="AAPL",
            kind=MarketEventKind.QUOTE,
            event_time=utc(2025, 3, 10, 13, 30),
            received_at=utc(2025, 3, 10, 13, 30, 1),
            provenance=iex_provenance(),
            bid=Decimal("-1"),
            ask=Decimal("100"),
        )
    with pytest.raises(ValueError):  # naive event_time
        MarketEvent(
            symbol="AAPL",
            kind=MarketEventKind.QUOTE,
            event_time=datetime(2025, 3, 10, 13, 30),
            received_at=utc(2025, 3, 10, 13, 30, 1),
            provenance=iex_provenance(),
            bid=Decimal("1"),
            ask=Decimal("2"),
        )
    with pytest.raises(ValueError):  # naive received_at
        MarketEvent(
            symbol="AAPL",
            kind=MarketEventKind.QUOTE,
            event_time=utc(2025, 3, 10, 13, 30),
            received_at=datetime(2025, 3, 10, 13, 30),
            provenance=iex_provenance(),
            bid=Decimal("1"),
            ask=Decimal("2"),
        )


def test_market_event_is_frozen():
    ev = MarketEvent(
        symbol="AAPL",
        kind=MarketEventKind.TRADE,
        event_time=utc(2025, 3, 10, 13, 30),
        received_at=utc(2025, 3, 10, 13, 30, 1),
        provenance=iex_provenance(),
        price=Decimal("1"),
        size=Decimal("1"),
    )
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        ev.price = Decimal("2")  # type: ignore[misc]


# --- Candle ---


def test_candle_positive_and_exact_duration():
    c = sample_candle()
    assert c.end == c.start + Timeframe.M1.duration
    assert c.closed is True
    # aware non-UTC start normalized to UTC
    c2 = sample_candle(
        start=datetime(2025, 3, 10, 9, 30, tzinfo=NY),
        end=datetime(2025, 3, 10, 9, 31, tzinfo=NY),
    )
    assert c2.start == utc(2025, 3, 10, 13, 30)


@pytest.mark.parametrize(
    "over",
    [
        {"high": Decimal("100.4")},  # high < max(open, close)
        {"low": Decimal("100.1")},  # low > min(open, close)
        {"open": Decimal("0")},  # zero open
        {"close": Decimal("-5")},  # negative close
        {"volume": Decimal("-1")},  # negative volume
        {"open": 100.5},  # float open
        {"high": Decimal("NaN")},
        {"low": Decimal("Infinity")},
        {"trade_count": -1},
        {"trade_count": True},
        {"vwap": Decimal("0")},
    ],
)
def test_candle_rejects_bad_ohlc(over):
    with pytest.raises(ValueError):
        sample_candle(**over)


def test_candle_rejects_bad_end_and_naive():
    start = utc(2025, 3, 10, 13, 30)
    with pytest.raises(ValueError):
        sample_candle(start=start, end=start + timedelta(minutes=2))
    with pytest.raises(ValueError):
        sample_candle(start=datetime(2025, 3, 10, 13, 30), end=start + timedelta(minutes=1))
    with pytest.raises(ValueError):
        sample_candle(
            start=start, end=(start + timedelta(minutes=1)).replace(tzinfo=None)
        )


def test_candle_is_frozen():
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        sample_candle().close = Decimal("1")  # type: ignore[misc]


# --- MarketContext ---


def make_context(**over) -> MarketContext:
    c1 = sample_candle(start=utc(2025, 3, 10, 13, 30))
    c2 = sample_candle(start=utc(2025, 3, 10, 13, 31), open=Decimal("100.5"),
                       high=Decimal("102"), low=Decimal("100"), close=Decimal("101"))
    params = {
        "symbol": "AAPL",
        "as_of": utc(2025, 3, 10, 13, 32),
        "session": sample_session(),
        "freshness": DataFreshness.FRESH,
        "candles": {Timeframe.M1: (c1, c2)},
        "indicators": {"vwap": Decimal("100.75"), "rsi": None},
        "provenance": iex_provenance(),
    }
    params.update(over)
    return MarketContext(**params)


def test_market_context_positive_and_latest_closed():
    ctx = make_context()
    assert isinstance(ctx.candles, MappingProxyType)
    assert isinstance(ctx.indicators, MappingProxyType)
    assert isinstance(ctx.candles[Timeframe.M1], tuple)
    assert ctx.latest_closed(Timeframe.M1).close == Decimal("101")
    assert ctx.latest_closed(Timeframe.M5) is None


def test_market_context_latest_closed_ignores_unclosed():
    c1 = sample_candle(start=utc(2025, 3, 10, 13, 30))
    c2 = sample_candle(start=utc(2025, 3, 10, 13, 31), closed=False)
    ctx = make_context(candles={Timeframe.M1: (c1, c2)})
    assert ctx.latest_closed(Timeframe.M1) == c1


def test_market_context_accepts_session_kind_and_research():
    ctx = make_context(
        session=SessionKind.REGULAR,
        research={"note": "test"},
        research_provenance="unit",
    )
    assert ctx.session is SessionKind.REGULAR
    assert isinstance(ctx.research, MappingProxyType)
    assert ctx.research_provenance == "unit"
    assert make_context().research_provenance is None


def test_market_context_immutable_mappings():
    ctx = make_context()
    with pytest.raises(TypeError):
        ctx.candles[Timeframe.M1] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.indicators["vwap"] = Decimal("1")  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.research["x"] = 1  # type: ignore[index]
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        ctx.as_of = utc(2025, 3, 10, 14, 0)  # type: ignore[misc]


def test_market_context_rejects_mutable_aliasing_and_bad_shapes():
    candles = {Timeframe.M1: [sample_candle(start=utc(2025, 3, 10, 13, 30))]}
    indicators = {"vwap": Decimal("1")}
    ctx = MarketContext(
        symbol="AAPL",
        as_of=utc(2025, 3, 10, 13, 32),
        session=sample_session(),
        freshness=DataFreshness.FRESH,
        candles=candles,
        indicators=indicators,
        provenance=iex_provenance(),
    )
    candles[Timeframe.M1].append(sample_candle(start=utc(2025, 3, 10, 13, 31)))
    indicators["vwap"] = Decimal("999")
    assert len(ctx.candles[Timeframe.M1]) == 1  # defensive copy
    assert ctx.indicators["vwap"] == Decimal("1")
    # bad shapes
    with pytest.raises(ValueError):
        make_context(candles={Timeframe.M1: (
            sample_candle(start=utc(2025, 3, 10, 13, 31)),
            sample_candle(start=utc(2025, 3, 10, 13, 30)),
        )})  # not ascending
    with pytest.raises(ValueError):
        dup = sample_candle(start=utc(2025, 3, 10, 13, 30))
        make_context(candles={Timeframe.M1: (dup, dup)})  # overlapping
    with pytest.raises(ValueError):
        make_context(candles={Timeframe.M5: (sample_candle(),)})  # timeframe mismatch
    with pytest.raises(ValueError):
        make_context(candles={Timeframe.M1: (sample_candle(symbol="MSFT"),)})  # symbol mismatch
    with pytest.raises(ValueError):
        make_context(as_of=datetime(2025, 3, 10, 13, 32))  # naive as_of
    with pytest.raises(ValueError):
        make_context(indicators={"vwap": 1.5})  # float indicator
    with pytest.raises(ValueError):
        make_context(indicators={"vwap": Decimal("NaN")})
    with pytest.raises(ValueError):
        make_context(session="REGULAR")  # type: ignore[arg-type]


# --- dependency hygiene ---


def test_domain_modules_do_not_import_brokers_strategies_or_sdk():
    import pathlib

    for name in ("mike_trade/domain/instruments.py", "mike_trade/domain/market.py"):
        lines = pathlib.Path(name).read_text().splitlines()
        imports = [ln.strip() for ln in lines if ln.strip().startswith(("import ", "from "))]
        blob = "\n".join(imports)
        assert "alpaca" not in blob.lower()
        assert "mike_trade.brokers" not in blob
        assert "mike_trade.strategies" not in blob
        assert "mike_trade.domain.signals" not in blob
        assert "strategy_types" not in blob
        assert "domain.trading" not in blob and "domain.protection" not in blob
        assert "domain.accounts" not in blob
    for mod in (instr_mod, market_mod):
        assert getattr(mod, "alpaca", None) is None
