"""SHADOW account accounting for SHORT positions (ledger-backed, replayable).

Short-sale proceeds are credited to cash but are *restricted collateral*, never buying power;
equity subtracts the short's liability; P&L has the correct sign; an exit can never reverse a
position; and exposure/margin ceilings apply to shorts exactly as to longs.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_shadow_account import (  # noqa: F401 - make_harness is a fixture
    NOW,
    TEN,
    Harness,
    make_harness,
    run,
)

from mike_trade.domain.errors import (
    BrokerRejectedError,
    EntryBlockedError,
    InsufficientBuyingPowerError,
)
from mike_trade.domain.trading import OrderPurpose, OrderSide, PositionDirection

HUNDRED = D("100")


def short_entry(h: Harness, client: str, qty=TEN, price=HUNDRED, fee=D("0"), *, symbol="AAPL",
                when=NOW) -> str:
    order = run(h.account.submit_order(
        h.intent(client, symbol, OrderSide.SELL_SHORT, qty, price=price,
                 purpose=OrderPurpose.ENTRY, when=when), strategy_key="short_s"))
    h.account.apply_fill(h.fill(f"f-{client}", order.broker_order_id, qty, price, fee,
                                symbol=symbol, side=OrderSide.SELL_SHORT, when=when))
    return str(order.broker_order_id)


def cover(h: Harness, client: str, price: D, fee=D("0"), *, symbol="AAPL", qty=None,
          when=NOW) -> str:
    quantity = qty if qty is not None else h.account.state.positions[symbol].quantity
    order = run(h.account.submit_order(
        h.intent(client, symbol, OrderSide.BUY_TO_COVER, quantity, price=price,
                 purpose=OrderPurpose.STRATEGY_EXIT, when=when)))
    h.account.apply_fill(h.fill(f"f-{client}", order.broker_order_id, quantity, price, fee,
                                symbol=symbol, side=OrderSide.BUY_TO_COVER, when=when))
    return str(order.broker_order_id)


def rejected(call, code):
    with pytest.raises(BrokerRejectedError) as raised:
        call()
    assert raised.value.code == code


# ------------------------------------------------------------------ cash, collateral, equity


def test_a_short_credits_proceeds_but_locks_them_as_collateral(make_harness):
    h = make_harness()
    account = h.create(per_symbol_budget=D("2000"), total_budget=D("5000"))
    short_entry(h, "s1", fee=D("1"))
    state = account.state
    position = state.positions["AAPL"]
    assert position.direction is PositionDirection.SHORT and position.quantity == TEN
    assert position.cost_basis == D("1000")            # the sale proceeds
    assert state.cash == D("10000") + D("1000") - D("1")
    assert state.restricted_collateral() == D("1500")  # 1.5 x proceeds stay locked
    snapshot = run(account.get_account())
    assert snapshot.shorting_enabled is True
    assert snapshot.buying_power == state.cash - D("1500")   # proceeds are NOT buying power
    assert snapshot.buying_power < D("10000")


def test_equity_subtracts_the_short_liability_and_has_no_edge_at_the_entry_price(make_harness):
    h = make_harness()
    account = h.create(per_symbol_budget=D("2000"), total_budget=D("5000"))
    short_entry(h, "s1", fee=D("1"))
    assert account.equity({"AAPL": HUNDRED}) == D("9999")            # only the entry fee
    assert account.equity({"AAPL": D("90")}) == D("9999") + D("100")  # price fell: +$100
    assert account.equity({"AAPL": D("110")}) == D("9999") - D("100")  # price rose: -$100
    assert account.unrealized_pnl({"AAPL": D("90")}) == D("100") - D("1")
    assert account.equity() == D("9999")  # unmarked: valued at the entry (no phantom gain)


# ------------------------------------------------------------------------- P&L


@pytest.mark.parametrize("cover_price, gross", [("94", "60"), ("106", "-60")])
def test_short_pnl_is_entry_minus_cover_including_fees(make_harness, cover_price, gross):
    h = make_harness()
    account = h.create(per_symbol_budget=D("2000"), total_budget=D("5000"))
    short_entry(h, "s1", price=D("100"), fee=D("1"))
    cover(h, "c1", D(cover_price), fee=D("1"))
    state = account.state
    assert "AAPL" not in state.positions
    assert state.realized_pnl == D(gross) - D("2")            # (100 - cover) x 10 - fees
    assert state.cash == D("10000") + D(gross) - D("2")       # all collateral released
    assert state.restricted_collateral() == D("0")
    assert state.closed_cycles == 1


def test_borrow_cost_is_an_explicit_estimated_assumption_charged_on_cover(make_harness):
    h = make_harness()
    account = h.create(per_symbol_budget=D("2000"), total_budget=D("5000"),
                       short_borrow_bps_per_day=D("10"))
    short_entry(h, "s1", price=D("100"), when=NOW)
    cover(h, "c1", D("100"), when=NOW + timedelta(days=1))    # held over one session day
    state = account.state
    borrow = D("1000") * D("10") / D("10000") * 1             # proceeds x bps/day x days
    assert borrow == D("1")
    assert state.realized_pnl == -borrow and state.cash == D("10000") - borrow
    # the default assumes no charge: borrow fees are never presented as known
    h2 = make_harness(type(h.scope)(h.scope.environment, "ACCT", "scenario-b"))
    other = h2.create(per_symbol_budget=D("2000"), total_budget=D("5000"))
    short_entry(h2, "s2", when=NOW)
    cover(h2, "c2", D("100"), when=NOW + timedelta(days=1))
    assert other.state.realized_pnl == D("0")


# ---------------------------------------------------- margin and exposure ceilings


@pytest.mark.parametrize("margin_enabled", [False, True])
def test_a_short_needs_own_cash_margin_even_when_margin_is_enabled(make_harness, margin_enabled):
    h = make_harness()
    h.create(starting_cash=D("400"), per_symbol_budget=D("2000"), total_budget=D("5000"),
             margin_enabled=margin_enabled)
    with pytest.raises(InsufficientBuyingPowerError) as raised:
        short_entry(h, "s1")  # $1,000 notional needs $500 own cash; only $400 exists
    assert raised.value.code == "cash_negative"
    assert h.account.state.orders == {}


def test_short_entries_respect_the_per_symbol_and_gross_total_budgets(make_harness):
    h = make_harness()
    h.create(per_symbol_budget=D("1500"), total_budget=D("2000"))
    with pytest.raises(EntryBlockedError) as raised:
        short_entry(h, "s0", qty=D("20"))  # $2,000 notional > $1,500 per symbol
    assert raised.value.code == "per_symbol_budget"
    short_entry(h, "s1", symbol="AAPL")                       # $1,000 gross exposure
    with pytest.raises(EntryBlockedError) as raised:
        short_entry(h, "s2", symbol="MSFT", qty=D("11"))      # + $1,100 > $2,000 total
    assert raised.value.code == "total_budget"


def test_a_long_and_a_short_share_the_same_exposure_ceiling(make_harness):
    h = make_harness()
    h.create(per_symbol_budget=D("1500"), total_budget=D("2000"))
    h.buy("b1", TEN, HUNDRED, symbol="AAPL")                  # $1,000 long
    with pytest.raises(EntryBlockedError) as raised:
        short_entry(h, "s1", symbol="MSFT", qty=D("11"))
    assert raised.value.code == "total_budget"


# --------------------------------------------------- no reversal, full exits only


def test_no_order_can_reverse_a_position(make_harness):
    h = make_harness()
    h.create(per_symbol_budget=D("2000"), total_budget=D("5000"))
    short_entry(h, "s1")
    # a BUY entry or a second short against an open short: refused, never netted
    rejected(lambda: run(h.account.submit_order(h.intent("b1", side=OrderSide.BUY))),
             "reversal")
    rejected(lambda: short_entry(h, "s2"), "pyramiding")
    # a SELL cannot close a short, and a cover needs a short
    rejected(lambda: run(h.account.submit_order(
        h.intent("x1", side=OrderSide.SELL, quantity=TEN, price=HUNDRED))), "wrong_direction")
    cover(h, "c1", D("95"))
    rejected(lambda: run(h.account.submit_order(h.intent(
        "c2", side=OrderSide.BUY_TO_COVER, purpose=OrderPurpose.STRATEGY_EXIT))), "no_position")
    # ... and the mirror image for a long
    h.buy("b2")
    rejected(lambda: short_entry(h, "s3"), "reversal")
    rejected(lambda: run(h.account.submit_order(h.intent(
        "c3", side=OrderSide.BUY_TO_COVER, purpose=OrderPurpose.STRATEGY_EXIT))),
        "wrong_direction")


def test_a_cover_must_close_the_whole_short_and_never_exceed_it(make_harness):
    h = make_harness()
    h.create(per_symbol_budget=D("2000"), total_budget=D("5000"))
    short_entry(h, "s1", qty=TEN)
    rejected(lambda: cover(h, "c1", D("95"), qty=D("4")), "partial_exit")
    rejected(lambda: cover(h, "c2", D("95"), qty=D("11")), "overcover")
    cover(h, "c3", D("95"))
    assert "AAPL" not in h.account.state.positions


# -------------------------------------------------------- ledger replay / restart


def test_a_short_replays_exactly_from_the_ledger_after_a_restart(make_harness):
    h = make_harness()
    h.create(per_symbol_budget=D("2000"), total_budget=D("5000"),
             short_margin_multiplier=D("2"), short_borrow_bps_per_day=D("5"))
    short_entry(h, "s1", fee=D("1"))
    before = h.account.state
    after = h.reopen().state
    position = after.positions["AAPL"]
    assert position.direction is PositionDirection.SHORT and position.quantity == TEN
    assert (after.cash, after.realized_pnl) == (before.cash, before.realized_pnl)
    assert after.short_margin_multiplier == D("2") and after.short_borrow_bps_per_day == D("5")
    assert after.restricted_collateral() == D("2000")   # the creation parameters replayed
    cover(h, "c1", D("90"), fee=D("1"))
    assert h.account.state.realized_pnl == D("98") - D("0")  # (100-90) x 10 - fees(2)
