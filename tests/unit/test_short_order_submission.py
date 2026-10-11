"""Order submission authorization for SHORT entries, and the short order-intent contracts.

A short entry is the only order that can open *unlimited* risk, so the submitter refuses it
before anything is persisted or sent unless EVERY condition holds: an explicit authorization
switch, broker capability, an account that may short with buying power, and an injected
borrowability/margin/protection checker.  Covers and protective exits never need it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_ambiguous_submit import (
    NOW,
    FakeBroker,
    S,
    authorize,
    build_rig,
    make_intent,
)

from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    BrokerCapabilities,
)
from mike_trade.domain.errors import UnsupportedOrderCapabilityError
from mike_trade.domain.trading import OrderSide, OrderType
from mike_trade.execution.intents import OrderIntent, OrderPurpose

SCOPE = AccountScope(AccountEnvironment.PAPER, "account-1")


class ShortCapableBroker(FakeBroker):
    """The ambiguous-submit fake broker plus the account facts the short gate reads."""

    def __init__(self, ledger=None, *, supports_short=True, shorting_enabled=True,
                 buying_power="10000") -> None:
        super().__init__(ledger)
        self.supports_short = supports_short
        self.shorting_enabled = shorting_enabled
        self.buying_power = D(buying_power)

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="fake", environment=AccountEnvironment.PAPER,
            supported_order_types=frozenset({"MARKET", "LIMIT"}), supports_fractional=False,
            supports_notional=False, supports_native_trailing_stop=False,
            supports_extended_hours=False, supports_short=self.supports_short)

    async def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            scope=SCOPE, as_of=NOW, cash=D("10000"), equity=D("10000"),
            buying_power=self.buying_power, shorting_enabled=self.shorting_enabled,
            source="fake")


def short_intent(**overrides):
    values = {"side": OrderSide.SELL_SHORT, "purpose": OrderPurpose.ENTRY_SHORT,
              "client_order_id": "short-entry-1", "intent_id": "intent-short-1",
              "limit_price": D("190.25")}
    values.update(overrides)
    return make_intent(**values)


def rig_for(intent, broker=None, **kwargs):
    rig = build_rig(intent, **kwargs)
    if broker is not None:
        broker.ledger = rig.ledger
        rig.broker = broker
    return rig


async def submit(rig, **submitter_overrides):
    command, decision = authorize(rig.intent)
    return await rig.make(broker=rig.broker, **submitter_overrides).submit(
        command, decision, rig.intent)


ALL_GATES = dict(short_authorized=True, short_entry_checker=lambda _intent: True)


# ------------------------------------------------------------------ allowed only when proven


async def test_a_fully_authorized_short_entry_is_submitted_as_a_sell_short_entry():
    rig = rig_for(short_intent(), ShortCapableBroker())
    result = await submit(rig, **ALL_GATES)
    assert result.outcome == "submitted" and result.status is S.ACCEPTED
    (sent,) = rig.broker.submit_calls
    assert sent.side is OrderSide.SELL_SHORT


@pytest.mark.parametrize("label, broker_kwargs, submitter_kwargs", [
    ("switch off", {}, dict(short_entry_checker=lambda _i: True)),
    ("no checker", {}, dict(short_authorized=True)),
    ("checker says no", {}, dict(short_authorized=True, short_entry_checker=lambda _i: False)),
    ("checker raises", {}, dict(short_authorized=True,
                                short_entry_checker=lambda _i: 1 / 0)),
    ("checker not exactly True", {}, dict(short_authorized=True,
                                          short_entry_checker=lambda _i: "yes")),
    ("broker cannot short", dict(supports_short=False), ALL_GATES),
    ("account cannot short", dict(shorting_enabled=False), ALL_GATES),
    ("no buying power", dict(buying_power="0"), ALL_GATES),
])
async def test_a_short_entry_fails_closed_when_any_condition_is_missing(
    label, broker_kwargs, submitter_kwargs
):
    rig = rig_for(short_intent(), ShortCapableBroker(**broker_kwargs))
    with pytest.raises(UnsupportedOrderCapabilityError) as raised:
        await submit(rig, **submitter_kwargs)
    assert raised.value.code == "short_entry_not_authorized", label
    assert rig.broker.submit_calls == [] and rig.broker.lookup_calls == []   # nothing was sent
    assert rig.ledger.status(rig.cid) is S.INTENT_PERSISTED                  # nothing persisted


async def test_exits_never_need_the_short_gate():
    # a cover and a protective stop must remain possible with every switch OFF
    for purpose in (OrderPurpose.EXIT_SHORT, OrderPurpose.PROTECTIVE_STOP):
        intent = make_intent(
            side=OrderSide.BUY_TO_COVER, purpose=purpose, client_order_id=f"exit-{purpose.value}",
            intent_id=f"i-{purpose.value}",
            order_type=OrderType.STOP if purpose is OrderPurpose.PROTECTIVE_STOP
            else OrderType.LIMIT,
            limit_price=None if purpose is OrderPurpose.PROTECTIVE_STOP else D("190.25"),
            stop_price=D("200") if purpose is OrderPurpose.PROTECTIVE_STOP else None)
        rig = rig_for(intent, ShortCapableBroker(supports_short=False, shorting_enabled=False,
                                                buying_power="0"))
        result = await submit(rig)
        assert result.outcome == "submitted", purpose


async def test_a_short_entry_is_blocked_by_an_unresolved_order_like_a_long_entry():
    rig = rig_for(short_intent(), ShortCapableBroker())
    first = rig.make(broker=rig.broker, **ALL_GATES)
    from mike_trade.execution.submission import UnresolvedOrderMarker

    rig.ledger.put_marker(UnresolvedOrderMarker(
        account_key="account-1", symbol="AAPL", client_order_id="other", intent_id="other-intent",
        reason="unresolved", recorded_at=NOW))
    command, decision = authorize(rig.intent)
    result = await first.submit(command, decision, rig.intent)
    assert result.outcome == "blocked" and rig.broker.submit_calls == []


# ------------------------------------------------------------------ intent contracts


@pytest.mark.parametrize("purpose, side", [
    (OrderPurpose.ENTRY_LONG, OrderSide.SELL_SHORT),      # a long entry cannot be a short sale
    (OrderPurpose.ENTRY_SHORT, OrderSide.BUY),            # a short entry must be SELL_SHORT
    (OrderPurpose.ENTRY_SHORT, OrderSide.SELL),
    (OrderPurpose.EXIT_LONG, OrderSide.BUY_TO_COVER),     # a SELL exit closes a long
    (OrderPurpose.EXIT_SHORT, OrderSide.SELL),            # a cover must be BUY_TO_COVER
    (OrderPurpose.EXIT_SHORT, OrderSide.SELL_SHORT),
    (OrderPurpose.PROTECTIVE_STOP, OrderSide.BUY),        # protection can only close
    (OrderPurpose.PROTECTIVE_STOP, OrderSide.SELL_SHORT),
])
def test_an_intent_whose_side_contradicts_its_purpose_is_rejected(purpose, side):
    with pytest.raises(ValueError):
        make_intent(purpose=purpose, side=side, order_type=OrderType.LIMIT,
                    limit_price=D("1"))


@pytest.mark.parametrize("purpose, side", [
    (OrderPurpose.ENTRY_LONG, OrderSide.BUY),
    (OrderPurpose.EXIT_LONG, OrderSide.SELL),
    (OrderPurpose.ENTRY_SHORT, OrderSide.SELL_SHORT),
    (OrderPurpose.EXIT_SHORT, OrderSide.BUY_TO_COVER),
    (OrderPurpose.PROTECTIVE_STOP, OrderSide.SELL),
    (OrderPurpose.PROTECTIVE_STOP, OrderSide.BUY_TO_COVER),
    (OrderPurpose.SAFETY_EXIT, OrderSide.BUY_TO_COVER),
    (OrderPurpose.MANUAL_CLOSE, OrderSide.SELL),
])
def test_every_valid_purpose_and_side_pairing_is_accepted(purpose, side):
    stop = purpose is OrderPurpose.PROTECTIVE_STOP
    intent = make_intent(
        purpose=purpose, side=side, client_order_id=f"{purpose.value}-{side.value}",
        order_type=OrderType.STOP if stop else OrderType.LIMIT,
        limit_price=None if stop else D("1"), stop_price=D("2") if stop else None)
    assert isinstance(intent, OrderIntent) and intent.side is side
    assert intent.signal_expires_at > NOW - timedelta(days=1)
