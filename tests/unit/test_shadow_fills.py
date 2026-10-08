from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mike_trade.brokers.shadow import fills as fills_module
from mike_trade.brokers.shadow.account import ShadowAccount
from mike_trade.brokers.shadow.fills import (
    SIMULATOR_VERSION,
    FillAssumptions,
    FillOutcome,
    ShadowFillSimulator,
    SimulatedFill,
)
from mike_trade.brokers.shadow.ledger import SIMULATOR_VERSION as LEDGER_VERSION
from mike_trade.brokers.shadow.ledger import ShadowLedger
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import InsufficientBuyingPowerError
from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    EntryEligibilityPolicy,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.market_data.candles import PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext, QuoteSnapshot
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession
from mike_trade.scheduling.calendar import SessionStatus

D = Decimal
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)  # Thursday 10:30 ET, regular session
SEC = timedelta(seconds=1)
SCOPE = AccountScope(AccountEnvironment.SHADOW, "ACCT", "fills-a")
OTHER_SCOPE = AccountScope(AccountEnvironment.SHADOW, "ACCT", "fills-b")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def quote(
    bid: str = "99.98",
    ask: str = "100.02",
    *,
    ts: datetime = NOW + SEC,
    receive: datetime | None = None,
    bid_size: str | None = "500",
    ask_size: str | None = "500",
    feed: str = "iex",
    symbol: str = "AAPL",
) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        bid=D(bid),
        ask=D(ask),
        bid_size=None if bid_size is None else D(bid_size),
        ask_size=None if ask_size is None else D(ask_size),
        provider_ts=ts,
        receive_ts=receive or ts,
        feed_id=feed,
    )


def order(
    side: OrderSide = OrderSide.BUY,
    quantity: str = "10",
    *,
    limit: str | None = None,
    order_type: OrderType | None = None,
    submitted: datetime = NOW,
    status: BrokerOrderStatus = BrokerOrderStatus.ACCEPTED,
    filled: str = "0",
    symbol: str = "AAPL",
) -> BrokerOrder:
    if order_type is None:
        order_type = OrderType.LIMIT if limit is not None else OrderType.MARKET
    return BrokerOrder(
        broker_order_id="shadow-o1",
        client_order_id="o1",
        account_scope_id=SCOPE.scope_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        status=status,
        quantity=D(quantity),
        filled_quantity=D(filled),
        submitted_at=submitted,
        updated_at=submitted,
        filled_avg_price=D("100") if D(filled) > 0 else None,
        limit_price=None if limit is None else D(limit),
    )


def sim(**overrides: Any) -> ShadowFillSimulator:
    return ShadowFillSimulator(FillAssumptions(**overrides))


def match(
    simulator: ShadowFillSimulator,
    o: BrokerOrder,
    quotes: list[QuoteSnapshot],
    *,
    as_of: datetime = NOW + 2 * SEC,
    tif: TimeInForce = TimeInForce.DAY,
) -> SimulatedFill:
    return simulator.simulate(o, quotes, as_of=as_of, time_in_force=tif)


# ------------------------------------------------------------------ prices and evidence


def test_market_buy_fills_at_ask_and_sell_at_bid_with_full_evidence() -> None:
    buy = match(sim(), order(), [quote()])
    assert buy.outcome is FillOutcome.FILLED
    assert (buy.quantity, buy.price, buy.slippage, buy.fee) == (
        D("10"),
        D("100.02"),
        D("0"),
        D("0"),
    )
    assert buy.filled_at == NOW + SEC
    assert buy.quote_price == D("100.02")
    assert (buy.quote_bid, buy.quote_ask) == (D("99.98"), D("100.02"))
    assert (buy.quote_bid_size, buy.quote_ask_size) == (D("500"), D("500"))
    assert (buy.quote_provider_ts, buy.quote_receive_ts) == (NOW + SEC, NOW + SEC)
    sell = match(sim(), order(OrderSide.SELL), [quote()])
    assert (sell.outcome, sell.price, sell.quote_price) == (
        FillOutcome.FILLED,
        D("99.98"),
        D("99.98"),
    )


def test_results_are_labeled_estimated_with_version_assumptions_and_feed_provenance() -> None:
    result = match(sim(slippage_bps=D("2")), order(), [quote()])
    assert SIMULATOR_VERSION == LEDGER_VERSION == result.simulator_version
    assert result.estimated is True and result.broker_confirmed is False
    assert (result.feed_id, result.consolidated, result.feed_label) == (
        "iex",
        False,
        "iex:not_consolidated",
    )
    assumptions = result.assumptions
    assert assumptions["slippage_bps"] == "2"
    assert {"latency_us", "participation", "fee_bps", "max_quote_age_us"} <= set(assumptions)
    assert assumptions["session"] == "regular_only;America/New_York"
    sip = match(sim(), order(), [quote(feed="sip")])
    assert (sip.consolidated, sip.feed_label) == (True, "sip:consolidated")
    with pytest.raises(AttributeError):
        result.broker_confirmed = True  # type: ignore[misc]


def test_every_price_quantity_and_fee_is_decimal_and_times_are_utc() -> None:
    result = match(sim(fee_per_share=D("0.005")), order(), [quote()])
    for value in (
        result.quantity,
        result.price,
        result.fee,
        result.slippage,
        result.requested_quantity,
    ):
        assert type(value) is Decimal
    assert result.filled_at is not None and result.filled_at.utcoffset() == timedelta(0)


def test_slippage_in_bps_and_ticks_always_moves_price_against_the_trader() -> None:
    simulator = sim(slippage_bps=D("10"), slippage_ticks=D("2"))
    buy = match(simulator, order(), [quote()])
    assert buy.price == D("100.1401") and buy.slippage == D("0.1201")  # ceil: against the buyer
    sell = match(simulator, order(OrderSide.SELL), [quote()])
    assert sell.price == D("99.8600") and sell.slippage == D("0.1200")  # floor: against the seller
    assert buy.price is not None and buy.price > D("100.02")
    assert sell.price is not None and sell.price < D("99.98")


def test_ticks_only_and_bps_only_slippage() -> None:
    assert match(sim(slippage_ticks=D("3")), order(), [quote()]).price == D("100.05")
    assert match(sim(slippage_bps=D("100")), order(OrderSide.SELL), [quote()]).price == D("98.9802")


def test_limit_buy_fills_at_better_of_limit_and_quote_plus_slippage_never_better_than_quote() -> (
    None
):
    simulator = sim(slippage_ticks=D("5"))  # quote + slippage = 100.07
    capped = match(simulator, order(limit="100.05"), [quote()])
    assert capped.price == D("100.05") and capped.slippage == D("0.03")
    loose = match(simulator, order(limit="100.50"), [quote()])
    assert loose.price == D("100.07")
    at_quote = match(sim(), order(limit="100.02"), [quote()])
    assert at_quote.price == D("100.02")  # crossing exactly at the limit fills, not better than ask
    for result in (capped, loose, at_quote):
        assert result.price is not None and result.price >= D("100.02")


def test_limit_sell_fills_at_better_of_limit_and_quote_minus_slippage_never_better_than_quote() -> (
    None
):
    simulator = sim(slippage_ticks=D("5"))  # quote - slippage = 99.93
    assert match(simulator, order(OrderSide.SELL, limit="99.90"), [quote()]).price == D("99.93")
    capped = match(simulator, order(OrderSide.SELL, limit="99.95"), [quote()])
    assert capped.price == D("99.95") and capped.slippage == D("0.03")


def test_limit_that_the_quote_does_not_cross_does_not_fill() -> None:
    buy = match(sim(), order(limit="100.01"), [quote()])
    assert (buy.outcome, buy.reason, buy.quantity, buy.price) == (
        FillOutcome.NO_FILL,
        fills_module.REASON_LIMIT_NOT_CROSSED,
        D("0"),
        None,
    )
    sell = match(sim(), order(OrderSide.SELL, limit="99.99"), [quote()])
    assert sell.reason == fills_module.REASON_LIMIT_NOT_CROSSED


def test_limit_fills_on_the_first_later_quote_that_crosses() -> None:
    quotes = [
        quote("99.90", "100.10", ts=NOW + SEC),
        quote("99.95", "100.05", ts=NOW + 2 * SEC),
        quote("99.99", "100.01", ts=NOW + 3 * SEC),
    ]
    result = match(sim(), order(limit="100.05"), quotes, as_of=NOW + 4 * SEC)
    assert result.outcome is FillOutcome.FILLED
    assert result.filled_at == NOW + 2 * SEC and result.price == D("100.05")


def test_a_bar_is_not_fill_evidence() -> None:
    candle = Candle(
        symbol="AAPL",
        timeframe=Timeframe.M1,
        start=NOW,
        end=NOW + timedelta(minutes=1),
        open=D("100"),
        high=D("101"),
        low=D("99"),
        close=D("100.5"),
        volume=D("1000"),
        provenance=FeedProvenance("alpaca", "iex", "stream", False),
    )
    with pytest.raises(TypeError):
        match(sim(), order(limit="100.50"), [candle])  # type: ignore[list-item]
    # Without any quote the order stays unfilled even though a bar "crossed" the limit.
    empty = match(sim(), order(limit="100.50"), [])
    assert (empty.outcome, empty.reason) == (
        FillOutcome.UNCERTAIN,
        fills_module.REASON_QUOTE_MISSING,
    )
    assert empty.price is None and empty.quantity == D("0")


def test_quotes_for_other_symbols_are_ignored() -> None:
    result = match(sim(), order(), [quote(symbol="MSFT")])
    assert (result.outcome, result.reason) == (
        FillOutcome.UNCERTAIN,
        fills_module.REASON_QUOTE_MISSING,
    )


def test_market_context_latest_quote_is_accepted_as_evidence() -> None:
    context = MarketContext(
        symbol="AAPL",
        as_of=NOW + 2 * SEC,
        version=1,
        feed_id="iex",
        consolidated=False,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id="XNYS-2026-10-08",
        freshness=ContextFreshness(NOW + SEC, SEC, timedelta(seconds=5), False, None),
        candles={},
        indicators={},
        latest_quote=quote(),
    )
    result = sim().simulate(order(), context, as_of=NOW + 2 * SEC)
    assert result.outcome is FillOutcome.FILLED and result.price == D("100.02")
    bare = MarketContext(
        symbol="AAPL",
        as_of=NOW,
        version=1,
        feed_id=None,
        consolidated=False,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id=None,
        freshness=ContextFreshness(None, None, timedelta(seconds=5), True, "NO_DATA"),
        candles={},
        indicators={},
    )
    assert sim().simulate(order(), bare, as_of=NOW).outcome is FillOutcome.UNCERTAIN


# ------------------------------------------------------------------ latency and lookahead


def test_only_quotes_at_or_after_order_time_plus_latency_are_eligible() -> None:
    simulator = sim(latency=timedelta(seconds=3))
    early = quote(ts=NOW + 2 * SEC)
    exact = quote("99.90", "100.10", ts=NOW + 3 * SEC)
    result = match(simulator, order(), [early, exact], as_of=NOW + 4 * SEC)
    assert result.price == D("100.10") and result.filled_at == NOW + 3 * SEC  # early quote skipped
    only_early = match(simulator, order(), [early], as_of=NOW + 4 * SEC)
    assert (only_early.outcome, only_early.reason) == (
        FillOutcome.NO_FILL,
        fills_module.REASON_AWAITING_QUOTE,
    )


def test_quote_exactly_at_order_time_is_eligible_with_zero_latency_and_earlier_is_not() -> None:
    assert match(sim(), order(), [quote(ts=NOW)], as_of=NOW + SEC).outcome is FillOutcome.FILLED
    before = match(sim(), order(), [quote(ts=NOW - SEC)], as_of=NOW + SEC)
    assert before.outcome is FillOutcome.NO_FILL


def test_quotes_not_yet_known_at_as_of_are_never_used() -> None:
    future = quote(ts=NOW + 5 * SEC)
    result = match(sim(), order(), [future], as_of=NOW + 2 * SEC)
    assert result.outcome is FillOutcome.NO_FILL and result.price is None
    late_receipt = quote(ts=NOW + SEC, receive=NOW + 9 * SEC)
    assert match(sim(), order(), [late_receipt], as_of=NOW + 2 * SEC).outcome is FillOutcome.NO_FILL


# ------------------------------------------------------------------ uncertain / missing data


@pytest.mark.parametrize(
    ("bid", "ask"),
    [("100.05", "100.00"), ("100.00", "100.00")],
    ids=["crossed", "locked"],
)
def test_crossed_or_locked_quote_is_uncertain(bid: str, ask: str) -> None:
    result = match(sim(), order(), [quote(bid, ask)])
    assert (result.outcome, result.reason) == (
        FillOutcome.UNCERTAIN,
        fills_module.REASON_QUOTE_CROSSED,
    )
    assert result.price is None and result.quantity == D("0") and result.fee == D("0")
    assert result.quote_bid == D(bid)  # the evidence behind the refusal is kept


def test_stale_quote_is_uncertain_but_exact_threshold_is_not_stale() -> None:
    stale = match(sim(), order(), [quote()], as_of=NOW + SEC + timedelta(seconds=5, microseconds=1))
    assert (stale.outcome, stale.reason) == (FillOutcome.UNCERTAIN, fills_module.REASON_QUOTE_STALE)
    edge = match(sim(), order(), [quote()], as_of=NOW + SEC + timedelta(seconds=5))
    assert edge.outcome is FillOutcome.FILLED
    tight = match(sim(max_quote_age=timedelta(seconds=1)), order(), [quote()], as_of=NOW + 3 * SEC)
    assert tight.reason == fills_module.REASON_QUOTE_STALE


def test_zero_or_missing_size_on_the_needed_side_is_uncertain() -> None:
    zero_ask = match(sim(), order(), [quote(ask_size="0")])
    assert (zero_ask.outcome, zero_ask.reason) == (
        FillOutcome.UNCERTAIN,
        fills_module.REASON_SIZE_ZERO,
    )
    zero_bid = match(sim(), order(OrderSide.SELL), [quote(bid_size="0")])
    assert zero_bid.reason == fills_module.REASON_SIZE_ZERO
    missing = match(sim(), order(), [quote(ask_size=None)])
    assert (missing.outcome, missing.reason) == (
        FillOutcome.UNCERTAIN,
        fills_module.REASON_SIZE_MISSING,
    )
    # the opposite side's size is irrelevant
    assert match(sim(), order(), [quote(bid_size="0", bid="99.98")]).outcome is FillOutcome.FILLED


def test_uncertain_stops_the_walk_instead_of_filling_at_a_later_quote() -> None:
    quotes = [quote("100.05", "100.00", ts=NOW + SEC), quote(ts=NOW + 2 * SEC)]
    result = match(sim(), order(), quotes, as_of=NOW + 3 * SEC)
    assert result.outcome is FillOutcome.UNCERTAIN


def test_sell_price_that_slippage_pushes_non_positive_is_uncertain() -> None:
    result = match(sim(slippage_ticks=D("1000")), order(OrderSide.SELL), [quote("5.00", "5.02")])
    assert (result.outcome, result.reason) == (
        FillOutcome.UNCERTAIN,
        fills_module.REASON_PRICE_NON_POSITIVE,
    )


# ------------------------------------------------------------------ session and order lifetime


@pytest.mark.parametrize(
    ("when", "label"),
    [
        (datetime(2026, 10, 8, 13, 29, 59, tzinfo=UTC), "one second before the open"),
        (datetime(2026, 10, 8, 20, 0, tzinfo=UTC), "exactly at the close"),
        (datetime(2026, 10, 8, 23, 0, tzinfo=UTC), "after hours"),
        (datetime(2026, 10, 10, 15, 0, tzinfo=UTC), "Saturday"),
        (datetime(2026, 11, 26, 15, 0, tzinfo=UTC), "Thanksgiving"),
    ],
)
def test_quotes_outside_the_regular_ny_session_never_fill(when: datetime, label: str) -> None:
    gtc_order = order(submitted=when - timedelta(days=1, hours=1))
    result = match(sim(), gtc_order, [quote(ts=when)], as_of=when + SEC, tif=TimeInForce.GTC)
    assert (result.outcome, result.reason) == (
        FillOutcome.NO_FILL,
        fills_module.REASON_OUTSIDE_SESSION,
    ), label


def test_quote_at_the_open_fills_and_session_is_judged_in_new_york_time() -> None:
    open_ts = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)  # 09:30 EDT
    pre_open = order(submitted=open_ts - timedelta(minutes=30))
    result = match(sim(), pre_open, [quote(ts=open_ts)], as_of=open_ts + SEC)
    assert result.outcome is FillOutcome.FILLED and result.filled_at == open_ts
    # In winter the same UTC wall time is before the open (09:30 EST == 14:30 UTC).
    winter = datetime(2026, 12, 8, 13, 30, tzinfo=UTC)
    blocked = match(
        sim(),
        order(submitted=winter - timedelta(minutes=30)),
        [quote(ts=winter)],
        as_of=winter + SEC,
    )
    assert blocked.reason == fills_module.REASON_OUTSIDE_SESSION


def test_day_order_does_not_fill_on_a_later_session_but_gtc_does() -> None:
    next_day = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)
    day = match(sim(), order(), [quote(ts=next_day)], as_of=next_day + SEC)
    assert (day.outcome, day.reason) == (FillOutcome.NO_FILL, fills_module.REASON_DAY_EXPIRED)
    gtc = match(sim(), order(), [quote(ts=next_day)], as_of=next_day + SEC, tif=TimeInForce.GTC)
    assert gtc.outcome is FillOutcome.FILLED


def test_day_order_placed_after_hours_or_on_a_weekend_is_good_for_the_next_session() -> None:
    friday = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)
    after_close = order(submitted=datetime(2026, 10, 8, 21, 0, tzinfo=UTC))
    assert (
        match(sim(), after_close, [quote(ts=friday)], as_of=friday + SEC).outcome
        is FillOutcome.FILLED
    )
    monday = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)
    weekend = order(submitted=datetime(2026, 10, 10, 15, 0, tzinfo=UTC))
    assert (
        match(sim(), weekend, [quote(ts=monday)], as_of=monday + SEC).outcome is FillOutcome.FILLED
    )
    tuesday = datetime(2026, 10, 13, 14, 30, tzinfo=UTC)
    assert match(sim(), weekend, [quote(ts=tuesday)], as_of=tuesday + SEC).reason == (
        fills_module.REASON_DAY_EXPIRED
    )


def test_calendar_out_of_range_fails_closed() -> None:
    far = datetime(2040, 1, 3, 15, 0, tzinfo=UTC)
    result = match(sim(), order(submitted=far - SEC), [quote(ts=far)], as_of=far + SEC)
    assert result.outcome is FillOutcome.NO_FILL and result.price is None


def test_order_that_is_not_open_or_already_complete_never_fills() -> None:
    for status in (BrokerOrderStatus.CANCELED, BrokerOrderStatus.FILLED, BrokerOrderStatus.NEW):
        result = match(sim(), order(status=status), [quote()])
        assert (result.outcome, result.reason) == (
            FillOutcome.NO_FILL,
            fills_module.REASON_ORDER_NOT_OPEN,
        )
    done = order(status=BrokerOrderStatus.PARTIALLY_FILLED, filled="10")
    assert match(sim(), done, [quote()]).reason == fills_module.REASON_ORDER_NOT_OPEN


# ------------------------------------------------------------------ participation and precision


def test_fill_is_capped_at_the_participation_fraction_of_displayed_size() -> None:
    simulator = sim(participation=D("0.5"))
    result = match(simulator, order(quantity="80"), [quote(ask_size="100")])
    assert (result.outcome, result.reason) == (
        FillOutcome.PARTIALLY_FILLED,
        fills_module.REASON_PARTIAL,
    )
    assert result.quantity == D("50") and result.remaining_quantity == D("30")
    sell = match(simulator, order(OrderSide.SELL, quantity="80"), [quote(bid_size="60")])
    assert sell.quantity == D("30")  # sells are limited by the bid size, not the ask size
    full = match(simulator, order(quantity="50"), [quote(ask_size="100")])
    assert full.outcome is FillOutcome.FILLED and full.remaining_quantity == D("0")


def test_quote_size_multiplier_converts_displayed_units_to_shares() -> None:
    result = match(
        sim(quote_size_multiplier=D("100")), order(quantity="500"), [quote(ask_size="2")]
    )
    assert result.quantity == D("200") and result.outcome is FillOutcome.PARTIALLY_FILLED


def test_remaining_quantity_of_a_partly_filled_order_is_what_is_matched() -> None:
    partly = order(quantity="80", status=BrokerOrderStatus.PARTIALLY_FILLED, filled="50")
    result = match(sim(), partly, [quote()])
    assert (result.requested_quantity, result.quantity, result.outcome) == (
        D("30"),
        D("30"),
        FillOutcome.FILLED,
    )


def test_whole_share_orders_floor_to_whole_shares_and_tiny_caps_do_not_fill() -> None:
    simulator = sim(participation=D("0.5"))
    result = match(simulator, order(quantity="10"), [quote(ask_size="15")])  # cap 7.5
    assert result.quantity == D("7") and result.outcome is FillOutcome.PARTIALLY_FILLED
    tiny = match(simulator, order(quantity="10"), [quote(ask_size="1")])  # cap 0.5
    assert (tiny.outcome, tiny.reason, tiny.quantity) == (
        FillOutcome.NO_FILL,
        fills_module.REASON_BELOW_LOT,
        D("0"),
    )


def test_fractional_orders_fill_fractionally_only_when_configured() -> None:
    simulator = sim(allow_fractional=True, participation=D("0.5"))
    result = match(simulator, order(quantity="10.5"), [quote(ask_size="15")])
    assert result.quantity == D("7.5") and result.outcome is FillOutcome.PARTIALLY_FILLED
    exact = match(simulator, order(quantity="0.25"), [quote()])
    assert exact.outcome is FillOutcome.FILLED and exact.quantity == D("0.25")
    coarse = sim(allow_fractional=True, quantity_quantum=D("0.1"))
    assert match(coarse, order(quantity="3.37"), [quote(ask_size="3.37")]).quantity == D("3.3")


def test_fractional_orders_fail_closed_when_unsupported() -> None:
    off = match(sim(), order(quantity="0.5"), [quote()])
    assert (off.outcome, off.reason, off.price) == (
        FillOutcome.UNSUPPORTED,
        fills_module.REASON_FRACTIONAL_UNSUPPORTED,
        None,
    )
    market_off = sim(allow_fractional=True, fractional_order_types=frozenset({OrderType.LIMIT}))
    blocked = match(market_off, order(quantity="0.5"), [quote()])
    assert blocked.reason == fills_module.REASON_FRACTIONAL_ORDER_TYPE
    assert (
        match(market_off, order(quantity="0.5", limit="100.5"), [quote()]).outcome
        is FillOutcome.FILLED
    )
    gtc = match(sim(allow_fractional=True), order(quantity="0.5"), [quote()], tif=TimeInForce.GTC)
    assert (gtc.outcome, gtc.reason) == (
        FillOutcome.UNSUPPORTED,
        fills_module.REASON_FRACTIONAL_TIF,
    )


def test_unsupported_order_types_fail_closed() -> None:
    for order_type in (OrderType.TRAILING_STOP, OrderType.STOP_LIMIT):
        o = BrokerOrder(
            broker_order_id="shadow-o1",
            client_order_id="o1",
            account_scope_id=SCOPE.scope_id,
            symbol="AAPL",
            side=OrderSide.SELL,
            order_type=order_type,
            status=BrokerOrderStatus.ACCEPTED,
            quantity=D("10"),
            filled_quantity=D("0"),
            submitted_at=NOW,
            updated_at=NOW,
            limit_price=D("99") if order_type is OrderType.STOP_LIMIT else None,
            stop_price=D("98") if order_type is OrderType.STOP_LIMIT else None,
            trail_percent=D("1") if order_type is OrderType.TRAILING_STOP else None,
        )
        result = match(sim(), o, [quote()])
        assert (result.outcome, result.reason) == (
            FillOutcome.UNSUPPORTED,
            fills_module.REASON_ORDER_TYPE_UNSUPPORTED,
        )
        assert result.price is None


# ------------------------------------------------------------------ fees


def test_fee_model_combines_per_fill_per_share_and_bps_rounded_up_to_the_cent() -> None:
    simulator = sim(fee_per_fill=D("1"), fee_per_share=D("0.005"), fee_bps=D("5"))
    result = match(simulator, order(), [quote()])
    # 1 + 10*0.005 + 1000.20*5/10000 = 1.5501 -> 1.56
    assert result.fee == D("1.56")


def test_fee_minimum_applies_and_default_fee_is_zero() -> None:
    assert match(sim(fee_per_share=D("0.001"), fee_minimum=D("2")), order(), [quote()]).fee == D(
        "2.00"
    )
    assert match(sim(), order(), [quote()]).fee == D("0")


def test_no_fee_is_charged_without_a_fill() -> None:
    result = match(sim(fee_per_fill=D("5"), fee_minimum=D("5")), order(limit="90"), [quote()])
    assert result.fee == D("0") and result.outcome is FillOutcome.NO_FILL


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"slippage_bps": -D("1")},
        {"slippage_ticks": 1.5},
        {"slippage_bps": 5},
        {"participation": D("0")},
        {"participation": D("1.01")},
        {"tick_size": D("0")},
        {"fee_per_share": -D("0.01")},
        {"fee_bps": float("nan")},
        {"latency": timedelta(seconds=-1)},
        {"max_quote_age": timedelta(0)},
        {"allow_fractional": "yes"},
        {"fractional_order_types": frozenset({OrderType.STOP})},
        {"quote_size_multiplier": D("0")},
    ],
)
def test_assumptions_reject_invalid_or_float_values(overrides: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        FillAssumptions(**overrides)


def test_simulate_requires_utc_as_of_and_typed_inputs() -> None:
    with pytest.raises(ValueError):
        sim().simulate(order(), [quote()], as_of=datetime(2026, 10, 8, 14, 31))
    with pytest.raises(ValueError):
        sim().simulate(
            order(), [quote()], as_of=(NOW + SEC).astimezone(ZoneInfo("America/New_York"))
        )
    with pytest.raises(TypeError):
        sim().simulate("order", [quote()], as_of=NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ShadowFillSimulator("assumptions")  # type: ignore[arg-type]


def test_simulate_is_pure_and_repeatable() -> None:
    simulator = sim(participation=D("0.5"))
    first = match(simulator, order(quantity="80"), [quote(ask_size="100")])
    second = match(simulator, order(quantity="80"), [quote(ask_size="100")])
    assert first == second


def test_result_invariants_are_enforced() -> None:
    result = match(sim(), order(), [quote()])
    with pytest.raises(ValueError):
        SimulatedFill(
            outcome=FillOutcome.NO_FILL,
            reason="x",
            symbol="AAPL",
            side=OrderSide.BUY,
            broker_order_id="o",
            requested_quantity=D("1"),
            simulator_version=SIMULATOR_VERSION,
            assumptions={},
            quantity=D("1"),
        )
    with pytest.raises(ValueError):
        SimulatedFill(
            outcome=FillOutcome.FILLED,
            reason="x",
            symbol="AAPL",
            side=OrderSide.BUY,
            broker_order_id="o",
            requested_quantity=D("1"),
            simulator_version=SIMULATOR_VERSION,
            assumptions={},
            quantity=D("1"),
            price=D("1"),  # fill without time or quote evidence
        )
    none = match(sim(), order(), [])
    with pytest.raises(ValueError):
        none.to_fill(SCOPE.scope_id)
    fill = result.to_fill(SCOPE.scope_id)
    assert (fill.price, fill.quantity, fill.fee, fill.filled_at) == (
        result.price,
        result.quantity,
        result.fee,
        result.filled_at,
    )


# ------------------------------------------------------------------ account integration


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.db_path = tmp_path / "fills.db"
        initialize_database(self.db_path, AccountEnvironment.SHADOW, now=NOW).close()
        self.session = WriterSession(self.db_path)

    def account(self, scope: AccountScope = SCOPE, **overrides: Any) -> ShadowAccount:
        class Clock:
            def now(self) -> datetime:
                return NOW

        ledger = ShadowLedger(self.session.bind_scope(scope.scope_id), scope, clock=Clock())
        values: dict[str, Any] = {
            "starting_cash": D("50000"),
            "per_symbol_budget": D("20000"),
            "total_budget": D("30000"),
            "eligibility": EntryEligibilityPolicy(max_filled_entry_cycles_per_symbol_per_session=3),
        }
        values.update(overrides)
        return ShadowAccount.create(ledger, **values)

    @staticmethod
    def submit(
        account: ShadowAccount,
        client_id: str,
        quantity: str,
        *,
        side: OrderSide = OrderSide.BUY,
        limit: str | None = "100.50",
        symbol: str = "AAPL",
        tif: TimeInForce = TimeInForce.DAY,
    ) -> str:
        intent = OrderIntent(
            intent_id=f"i-{client_id}",
            client_order_id=client_id,
            account_scope_id=account.scope.scope_id,
            cycle_id=f"cycle-{symbol}",
            symbol=symbol,
            side=side,
            purpose=OrderPurpose.ENTRY if side is OrderSide.BUY else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.LIMIT if limit else OrderType.MARKET,
            time_in_force=tif,
            created_at=NOW,
            quantity=D(quantity),
            limit_price=None if limit is None else D(limit),
        )
        return str(run(account.submit_order(intent)).broker_order_id)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    h = Harness(tmp_path)
    yield h
    h.session.close()


def test_fill_is_applied_to_the_account_with_slippage_fee_and_feed_provenance(
    harness: Harness,
) -> None:
    account = harness.account()
    order_id = harness.submit(account, "b1", "10")
    simulator = sim(slippage_ticks=D("1"), fee_per_fill=D("1"))
    result = simulator.apply_fill_to_account(account, order_id, [quote()], as_of=NOW + 2 * SEC)
    assert result.outcome is FillOutcome.FILLED and result.price == D("100.03")
    state = account.state
    assert state.cash == D("50000") - D("10") * D("100.03") - D("1.00")
    assert state.positions["AAPL"].quantity == D("10")
    assert state.fill_slippage[result.fill_id or ""] == D("0.01")
    recorded = state.fills[result.fill_id or ""]
    assert (recorded.price, recorded.fee, recorded.filled_at) == (D("100.03"), D("1.00"), NOW + SEC)
    stored = run(account.get_order(order_id))
    assert stored.status is BrokerOrderStatus.FILLED
    fill_rows = [
        json.loads(row[0])
        for row in harness.session.connection.execute(
            "SELECT payload_json FROM domain_events WHERE event_type = 'ShadowFillRecorded'"
        )
    ]
    assert len(fill_rows) == 1
    assert fill_rows[0]["feed"] == "iex:not_consolidated"
    assert fill_rows[0]["estimated"] is True and fill_rows[0]["broker_confirmed"] is False


def test_sell_fill_closes_the_position_at_the_bid(harness: Harness) -> None:
    account = harness.account()
    buy_id = harness.submit(account, "b1", "10")
    simulator = sim()
    simulator.apply_fill_to_account(account, buy_id, [quote()], as_of=NOW + 2 * SEC)
    sell_id = harness.submit(account, "s1", "10", side=OrderSide.SELL, limit=None)
    later = NOW + 10 * SEC
    result = simulator.apply_fill_to_account(
        account, sell_id, [quote("100.20", "100.24", ts=later)], as_of=later + SEC
    )
    assert result.price == D("100.20")
    assert "AAPL" not in account.state.positions
    assert account.state.cash == D("50000") - D("1000.20") + D("1002.00")


def test_partial_fills_accumulate_across_quotes_and_never_reuse_a_quote(harness: Harness) -> None:
    account = harness.account()
    order_id = harness.submit(account, "b1", "80")
    simulator = sim(participation=D("0.5"))
    first_quote = quote(ask_size="100")
    one = simulator.apply_fill_to_account(account, order_id, [first_quote], as_of=NOW + 2 * SEC)
    assert (one.outcome, one.quantity) == (FillOutcome.PARTIALLY_FILLED, D("50"))
    assert run(account.get_order(order_id)).status is BrokerOrderStatus.PARTIALLY_FILLED
    # The same quote cannot supply liquidity twice, even from a brand new simulator.
    for engine in (simulator, sim(participation=D("0.5"))):
        again = engine.apply_fill_to_account(account, order_id, [first_quote], as_of=NOW + 2 * SEC)
        assert again.outcome is FillOutcome.NO_FILL and again.quantity == D("0")
    assert account.state.positions["AAPL"].quantity == D("50")
    later = quote("99.99", "100.03", ts=NOW + 4 * SEC, ask_size="100")
    two = simulator.apply_fill_to_account(
        account, order_id, [first_quote, later], as_of=NOW + 5 * SEC
    )
    assert (two.outcome, two.quantity, two.price) == (FillOutcome.FILLED, D("30"), D("100.03"))
    final = run(account.get_order(order_id))
    assert final.status is BrokerOrderStatus.FILLED and final.filled_quantity == D("80")
    assert account.state.positions["AAPL"].quantity == D("80")
    assert len(account.state.fills) == 2


def test_displayed_liquidity_is_shared_between_orders_on_the_same_simulator(
    harness: Harness,
) -> None:
    first = harness.account(SCOPE)
    second = harness.account(OTHER_SCOPE)
    first_id = harness.submit(first, "b1", "60")
    second_id = harness.submit(second, "b1", "60")
    simulator = sim()
    shared = quote(ask_size="100")
    a = simulator.apply_fill_to_account(first, first_id, [shared], as_of=NOW + 2 * SEC)
    b = simulator.apply_fill_to_account(second, second_id, [shared], as_of=NOW + 2 * SEC)
    assert (a.quantity, b.quantity) == (D("60"), D("40"))
    assert b.outcome is FillOutcome.PARTIALLY_FILLED
    c = simulator.apply_fill_to_account(second, second_id, [shared], as_of=NOW + 2 * SEC)
    assert c.outcome is FillOutcome.NO_FILL  # nothing left on that quote


def test_account_remains_the_authority_for_negative_cash(harness: Harness) -> None:
    account = harness.account(starting_cash=D("500.50"))
    # Submission only checks quantity x limit (500.50); the simulated fee then breaks the cash rule.
    order_id = harness.submit(account, "b1", "5", limit="100.10")
    simulator = sim(fee_per_fill=D("1"))
    with pytest.raises(InsufficientBuyingPowerError):
        simulator.apply_fill_to_account(account, order_id, [quote()], as_of=NOW + 2 * SEC)
    assert account.state.cash == D("500.50") and not account.state.fills
    assert not account.state.positions


def test_nothing_is_recorded_or_consumed_for_non_fill_outcomes(harness: Harness) -> None:
    account = harness.account()
    order_id = harness.submit(account, "b1", "10", limit="99")
    simulator = sim()
    for bad in (
        quote("98.50", "98.40"),
        quote("98.50", "98.90", ask_size="0"),
        quote("99.50", "99.90"),
    ):
        result = simulator.apply_fill_to_account(account, order_id, [bad], as_of=NOW + 2 * SEC)
        assert not result.is_fill and result.quantity == D("0")
    assert not account.state.fills and account.state.cash == D("50000")
    empty = simulator.apply_fill_to_account(account, order_id, [], as_of=NOW + 2 * SEC)
    assert empty.outcome is FillOutcome.UNCERTAIN
    ok = simulator.apply_fill_to_account(
        account, order_id, [quote("98.50", "98.90", ask_size="9")], as_of=NOW + 2 * SEC
    )
    assert ok.outcome is FillOutcome.PARTIALLY_FILLED and ok.quantity == D("9")


def test_account_without_fractional_support_fails_closed_for_unsupported_combinations(
    harness: Harness,
) -> None:
    account = harness.account(allow_fractional=True)
    order_id = harness.submit(account, "b1", "2.5")
    unsupported = sim().apply_fill_to_account(account, order_id, [quote()], as_of=NOW + 2 * SEC)
    assert unsupported.outcome is FillOutcome.UNSUPPORTED and not account.state.fills
    supported = sim(allow_fractional=True).apply_fill_to_account(
        account, order_id, [quote()], as_of=NOW + 2 * SEC
    )
    assert supported.outcome is FillOutcome.FILLED and supported.quantity == D("2.5")
    assert account.state.positions["AAPL"].quantity == D("2.5")


def test_gtc_order_time_in_force_is_read_from_the_account(harness: Harness) -> None:
    account = harness.account()
    day_id = harness.submit(account, "d1", "10", tif=TimeInForce.DAY)
    gtc_id = harness.submit(account, "g1", "10", symbol="MSFT", tif=TimeInForce.GTC)
    next_day = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)
    simulator = sim()
    day = simulator.apply_fill_to_account(
        account, day_id, [quote(ts=next_day)], as_of=next_day + SEC
    )
    assert day.reason == fills_module.REASON_DAY_EXPIRED
    gtc = simulator.apply_fill_to_account(
        account, gtc_id, [quote(ts=next_day, symbol="MSFT")], as_of=next_day + SEC
    )
    assert gtc.outcome is FillOutcome.FILLED


def test_unknown_order_and_wrong_account_type_are_rejected(harness: Harness) -> None:
    account = harness.account()
    with pytest.raises(ValueError):
        sim().apply_fill_to_account(account, "shadow-missing", [quote()], as_of=NOW + SEC)
    with pytest.raises(TypeError):
        sim().apply_fill_to_account(object(), "x", [quote()], as_of=NOW + SEC)  # type: ignore[arg-type]


# ------------------------------------------------------------------ static guarantees


def _module_ast() -> ast.Module:
    return ast.parse(Path(fills_module.__file__).read_text(encoding="utf-8"))


def test_module_imports_no_broker_client_or_network_code() -> None:
    imported: list[str] = []
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    lowered = " ".join(imported).lower()
    for forbidden in ("alpaca", "requests", "httpx", "aiohttp", "urllib", "socket", "websocket"):
        assert forbidden not in lowered


def test_module_uses_no_float_and_reads_no_candle_close() -> None:
    tree = _module_ast()
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "float" not in names
    assert not any(
        isinstance(n, ast.Constant) and isinstance(n.value, float) for n in ast.walk(tree)
    )
    assert "close" not in attributes and "Candle" not in names
    assert "candles" not in attributes
