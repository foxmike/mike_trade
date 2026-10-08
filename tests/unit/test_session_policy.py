from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mike_trade.risk.market_filters import (
    LiquidityFilter,
    MarketFilterDecision,
    MarketFilterReason,
    SpreadFilter,
)
from mike_trade.risk.session_policy import (
    EntryCutoffPolicy,
    ExitKind,
    MinimumHoldPolicy,
    SessionPolicy,
)

UTC = timezone.utc


@dataclass(frozen=True)
class Quote:
    bid: Decimal = Decimal("99.90")
    ask: Decimal = Decimal("100.10")
    bid_size: Decimal | None = Decimal("20")
    ask_size: Decimal | None = Decimal("20")
    receive_ts: datetime = datetime(2025, 3, 10, 14, 0, tzinfo=UTC)
    feed_id: str | None = "IEX"
    stale: bool = False


def test_regular_hours_gate_uses_new_york_dst_session_boundaries():
    policy = SessionPolicy()
    # DST began on March 9, 2025; Monday's regular open is now 13:30 UTC.
    assert not policy.evaluate(datetime(2025, 3, 10, 13, 29, tzinfo=UTC)).allowed
    assert policy.evaluate(datetime(2025, 3, 10, 13, 30, tzinfo=UTC)).allowed
    assert not policy.evaluate(datetime(2025, 3, 10, 20, 0, tzinfo=UTC)).allowed


def test_cutoff_uses_actual_early_close_time():
    policy = SessionPolicy(entry_cutoff=EntryCutoffPolicy(30))
    # Nov 28, 2025 is the day after Thanksgiving and closes at 13:00 ET.
    assert policy.evaluate(datetime(2025, 11, 28, 17, 29, tzinfo=UTC)).allowed
    blocked = policy.evaluate(datetime(2025, 11, 28, 17, 30, tzinfo=UTC))
    assert blocked.reason == "ENTRY_CUTOFF_REACHED"
    assert blocked.cutoff_at == datetime(2025, 11, 28, 17, 30, tzinfo=UTC)


def test_market_filter_blocks_absolute_and_basis_point_spread():
    now = datetime(2025, 3, 10, 14, 0, tzinfo=UTC)
    quote = Quote()
    absolute = MarketFilterDecision.evaluate(
        now=now, quote=quote, spread_filter=SpreadFilter(max_absolute=Decimal("0.10"))
    )
    assert MarketFilterReason.SPREAD_TOO_WIDE in absolute.reasons
    bps = MarketFilterDecision.evaluate(
        now=now, quote=quote, spread_filter=SpreadFilter(max_basis_points=Decimal("10"))
    )
    assert MarketFilterReason.SPREAD_TOO_WIDE in bps.reasons


def test_market_filter_blocks_insufficient_liquidity_and_preserves_feed():
    now = datetime(2025, 3, 10, 14, 0, tzinfo=UTC)
    decision = MarketFilterDecision.evaluate(
        now=now, quote=Quote(),
        liquidity_filter=LiquidityFilter(
            minimum_recent_volume=Decimal("1000"), minimum_quote_size=Decimal("30")
        ),
        recent_volume=Decimal("999"),
    )
    assert MarketFilterReason.INSUFFICIENT_LIQUIDITY in decision.reasons
    assert decision.feed_provenance == "IEX"


@pytest.mark.parametrize(
    ("quote", "now", "reason"),
    [
        (None, datetime(2025, 3, 10, 14, 0, tzinfo=UTC), MarketFilterReason.QUOTE_MISSING),
        (Quote(receive_ts=datetime(2025, 3, 10, 13, 59, tzinfo=UTC)), datetime(2025, 3, 10, 14, 0, tzinfo=UTC), MarketFilterReason.QUOTE_STALE),
        (Quote(bid=Decimal("100.2"), ask=Decimal("100.1")), datetime(2025, 3, 10, 14, 0, tzinfo=UTC), MarketFilterReason.QUOTE_CROSSED),
        (Quote(bid=Decimal("100.1"), ask=Decimal("100.1")), datetime(2025, 3, 10, 14, 0, tzinfo=UTC), MarketFilterReason.QUOTE_LOCKED),
        (Quote(feed_id=None), datetime(2025, 3, 10, 14, 0, tzinfo=UTC), MarketFilterReason.UNKNOWN_PROVENANCE),
    ],
)
def test_market_filter_fails_closed_for_unusable_quote(quote, now, reason):
    decision = MarketFilterDecision.evaluate(
        now=now, quote=quote,
        liquidity_filter=LiquidityFilter(minimum_quote_size=Decimal("1")),
    )
    assert not decision.allowed
    assert reason in decision.reasons


def test_minimum_hold_blocks_strategy_exit_until_elapsed():
    policy = MinimumHoldPolicy(timedelta(minutes=10))
    fill_at = datetime(2025, 3, 10, 14, 0, tzinfo=UTC)
    before = policy.evaluate(
        now=fill_at + timedelta(minutes=9), first_confirmed_entry_fill_at=fill_at,
        exit_kind=ExitKind.STRATEGY,
    )
    after = policy.evaluate(
        now=fill_at + timedelta(minutes=10), first_confirmed_entry_fill_at=fill_at,
        exit_kind=ExitKind.STRATEGY,
    )
    assert before.reason == "MINIMUM_HOLD_NOT_ELAPSED"
    assert after.allowed


@pytest.mark.parametrize("kind", [
    ExitKind.PROTECTIVE_STOP, ExitKind.TRAILING_STOP, ExitKind.RISK_SAFETY,
    ExitKind.DAILY_LOSS, ExitKind.MANUAL_CLOSE, ExitKind.CLOSE_ALL,
    ExitKind.FLATTEN, ExitKind.RECONCILIATION, ExitKind.END_OF_DAY,
])
def test_minimum_hold_does_not_block_exempt_exit_kinds(kind):
    decision = MinimumHoldPolicy(timedelta(days=1)).evaluate(
        now=datetime(2025, 3, 10, 14, 1, tzinfo=UTC),
        first_confirmed_entry_fill_at=datetime(2025, 3, 10, 14, 0, tzinfo=UTC),
        exit_kind=kind,
    )
    assert decision.allowed


def test_end_of_day_close_is_disabled_by_default():
    assert SessionPolicy().end_of_day_close_enabled is False
