"""Stateful in-memory Alpaca Trading API double (no network, no credentials).

``FakeAlpacaExchange.transport`` is an ``httpx.MockTransport`` so the production
``AlpacaHttpClient`` -> order/account/activity services -> ``AlpacaBrokerAdapter`` stack is
exercised end to end against simulated broker responses.  Behavior mirrors the pieces of
Alpaca the engine depends on:

* a sell order (including a resting stop) *holds* shares: another sell that would exceed the
  unheld quantity is rejected with HTTP 403 ``insufficient qty available``;
* ``PATCH /v2/orders/{id}`` replaces a working order by creating a NEW order id and marking the
  old one ``replaced`` (``replaced_by``);
* a market order fills immediately at the symbol's current price (or stays ``accepted`` when
  ``auto_fill`` is off), a sell stop triggers when the price trades at or below its stop;
* every execution also produces an account ``FILL`` activity with its own activity id and a
  ``trade_updates`` message, mirroring Alpaca's two independent execution identifiers;
* human actions are modeled explicitly (``external_*`` helpers) and leave the same footprints
  a website / mobile / DAS Trader action leaves: orders whose ``client_order_id`` does not
  belong to Mike_Trade plus fills.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx

D = Decimal
ZERO = D("0")


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 10, 12, 14, 30, tzinfo=UTC)  # 10:30 New York

    def now(self) -> datetime:
        return self.t

    def advance(self, **kwargs: float) -> datetime:
        self.t = self.t + timedelta(**kwargs)
        return self.t


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class FakeAlpacaExchange:
    """One PAPER account's broker-side state."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.prices: dict[str, Decimal] = {}
        self.positions: dict[str, dict[str, Decimal]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.activities: list[dict[str, Any]] = []
        self.stream_messages: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, Any]] = []
        self.hosts: set[str] = set()
        self.auto_fill = True
        self.cancel_pending_polls = 0
        self.strict_stop_validation = True
        self.market_open = True
        self.next_close = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
        self.account_flags: dict[str, Any] = {}
        self._failures: list[dict[str, Any]] = []
        self._pending_cancel_polls: dict[str, int] = {}
        self._seq = 0
        self.transport = httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------ test controls

    def inject_failure(
        self, method: str, path_contains: str, *, status: int | None = 500,
        timeout: bool = False, times: int = 1, after_effect: bool = False,
    ) -> None:
        """Fail the next ``times`` matching requests.

        ``after_effect`` applies the mutation first and THEN fails (an unknown-outcome POST).
        """
        self._failures.append({
            "method": method.upper(), "path": path_contains, "status": status,
            "timeout": timeout, "times": times, "after_effect": after_effect,
        })

    def set_price(self, symbol: str, price: str | Decimal) -> None:
        """Trade at ``price``; resting sell stops at or above it trigger and fill."""
        price = D(str(price))
        self.prices[symbol] = price
        for order in list(self.orders.values()):
            if order["symbol"] != symbol or order["status"] not in ("accepted", "new"):
                continue
            if order["type"] == "stop" and order["side"] == "sell" and price <= D(order["stop_price"]):
                self._fill(order, D(order["qty"]) - D(order["filled_qty"]), price)

    def fill_market_orders(self) -> None:
        """Fill every working market order at the current price (when ``auto_fill`` is off)."""
        for order in list(self.orders.values()):
            if order["type"] == "market" and order["status"] in ("accepted", "new"):
                self._fill(order, D(order["qty"]) - D(order["filled_qty"]), self.prices[order["symbol"]])

    def partial_fill(self, order_id: str, qty: str | Decimal, price: str | Decimal | None = None) -> None:
        order = self.orders[order_id]
        self._fill(order, D(str(qty)), D(str(price)) if price is not None else self.prices[order["symbol"]])

    def external_buy(self, symbol: str, qty: str | Decimal, price: str | Decimal | None = None) -> str:
        """A human buys through another interface."""
        price = D(str(price)) if price is not None else self.prices[symbol]
        order = self._new_order(
            symbol=symbol, side="buy", type="market", qty=D(str(qty)), tif="day",
            client_order_id=f"web-{uuid4().hex[:12]}",
        )
        self._fill(order, D(str(qty)), price)
        return order["id"]

    def external_sell(self, symbol: str, qty: str | Decimal, price: str | Decimal | None = None) -> str:
        """A human sells ``qty`` shares.  Alpaca refuses it while open sells hold the shares."""
        price = D(str(price)) if price is not None else self.prices[symbol]
        order = self._submit_order_body({
            "symbol": symbol, "qty": str(qty), "side": "sell", "type": "market",
            "time_in_force": "day", "client_order_id": f"web-{uuid4().hex[:12]}",
        }, price_override=price)
        return order["id"]

    def external_cancel(self, order_id: str) -> None:
        """A human cancels one of our orders in the Alpaca UI."""
        order = self.orders[order_id]
        if order["status"] in ("accepted", "new", "pending_new", "partially_filled"):
            order["status"] = "canceled"
            order["canceled_at"] = _stamp(self.clock.now())
            order["updated_at"] = _stamp(self.clock.now())
            self._emit_update("canceled", order)

    def external_close_position(self, symbol: str, price: str | Decimal | None = None) -> None:
        """'Close position' in the UI / API: cancels the symbol's open orders, sells everything."""
        for order in list(self.orders.values()):
            if order["symbol"] == symbol and order["status"] in ("accepted", "new", "partially_filled"):
                self.external_cancel(order["id"])
        position = self.positions.get(symbol)
        if position and position["qty"] > 0:
            self.external_sell(symbol, position["qty"], price)

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [
            o for o in self.orders.values()
            if o["status"] in ("accepted", "new", "pending_new", "partially_filled", "pending_cancel")
            and (symbol is None or o["symbol"] == symbol)
        ]

    def open_stops(self, symbol: str) -> list[dict[str, Any]]:
        return [o for o in self.open_orders(symbol) if o["type"] == "stop"]

    def position_qty(self, symbol: str) -> Decimal:
        position = self.positions.get(symbol)
        return position["qty"] if position else ZERO

    def posts(self) -> list[Any]:
        return [body for method, path, body in self.requests if method == "POST" and path == "/v2/orders"]

    # --------------------------------------------------------------------- state helpers

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    def _new_order(self, *, symbol: str, side: str, type: str, qty: Decimal, tif: str,
                   client_order_id: str, stop_price: Decimal | None = None,
                   limit_price: Decimal | None = None, status: str = "accepted") -> dict[str, Any]:
        now = _stamp(self.clock.now())
        order = {
            "id": self._next_id("ord"), "client_order_id": client_order_id,
            "created_at": now, "updated_at": now, "submitted_at": now, "filled_at": None,
            "expired_at": None, "canceled_at": None, "failed_at": None, "replaced_at": None,
            "replaced_by": None, "replaces": None, "asset_id": f"asset-{symbol}", "symbol": symbol,
            "asset_class": "us_equity", "qty": str(qty), "filled_qty": "0",
            "filled_avg_price": None, "order_class": "simple", "order_type": type, "type": type,
            "side": side, "time_in_force": tif, "limit_price": None if limit_price is None else str(limit_price),
            "stop_price": None if stop_price is None else str(stop_price), "status": status,
            "extended_hours": False, "legs": None, "trail_percent": None, "trail_price": None,
            "hwm": None,
        }
        self.orders[order["id"]] = order
        self._emit_update("new" if status == "accepted" else status, order)
        return order

    def _held_sell_qty(self, symbol: str, exclude: str | None = None) -> Decimal:
        return sum(
            (D(o["qty"]) - D(o["filled_qty"]) for o in self.open_orders(symbol)
             if o["side"] == "sell" and o["id"] != exclude),
            ZERO,
        )

    def _fill(self, order: dict[str, Any], qty: Decimal, price: Decimal) -> None:
        if qty <= 0 or order["status"] in ("filled", "canceled", "replaced", "rejected", "expired"):
            return
        qty = min(qty, D(order["qty"]) - D(order["filled_qty"]))
        symbol, side = order["symbol"], order["side"]
        position = self.positions.setdefault(symbol, {"qty": ZERO, "avg": ZERO})
        if side == "buy":
            cost = position["avg"] * position["qty"] + price * qty
            position["qty"] += qty
            position["avg"] = cost / position["qty"]
        else:
            position["qty"] -= qty
            if position["qty"] < 0:
                raise AssertionError("fake exchange went short")
            if position["qty"] == 0:
                position["avg"] = ZERO
        previous_filled = D(order["filled_qty"])
        previous_cost = previous_filled * D(order["filled_avg_price"] or "0")
        filled = previous_filled + qty
        order["filled_qty"] = str(filled)
        order["filled_avg_price"] = str((previous_cost + qty * price) / filled)
        done = filled == D(order["qty"])
        order["status"] = "filled" if done else "partially_filled"
        now = _stamp(self.clock.now())
        order["updated_at"] = now
        if done:
            order["filled_at"] = now
        activity = {
            "id": f"{self.clock.now().strftime('%Y%m%d%H%M%S%f')}::{uuid4()}",
            "activity_type": "FILL", "type": "fill" if done else "partial_fill",
            "transaction_time": now, "price": str(price), "qty": str(qty),
            "side": side, "symbol": symbol, "leaves_qty": str(D(order["qty"]) - filled),
            "order_id": order["id"], "cum_qty": str(filled), "order_status": order["status"],
        }
        self.activities.append(activity)
        self._emit_update("fill" if done else "partial_fill", order, price=price, qty=qty,
                          position_qty=position["qty"])

    def _emit_update(self, event: str, order: dict[str, Any], **extra: Any) -> None:
        message: dict[str, Any] = {
            "stream": "trade_updates",
            "data": {
                "event": event, "execution_id": str(uuid4()),
                "timestamp": _stamp(self.clock.now()), "order": dict(order),
            },
        }
        for key, value in extra.items():
            message["data"][key] = str(value)
        self.stream_messages.append(message)

    # -------------------------------------------------------------------- REST handling

    def _handle(self, request: httpx.Request) -> httpx.Response:
        split = urlsplit(str(request.url))
        path = split.path
        params = {k: v[0] for k, v in parse_qs(split.query).items()}
        body = json.loads(request.content) if request.content else None
        method = request.method.upper()
        self.requests.append((method, path, body))
        self.hosts.add(split.hostname or "")
        failure = self._match_failure(method, path)
        if failure is not None and not failure["after_effect"]:
            return self._fail(failure)
        try:
            response = self._route(method, path, params, body)
        except _HttpError as error:
            return httpx.Response(error.status, json={"code": error.status * 10000, "message": error.message})
        if failure is not None:
            return self._fail(failure)
        return response

    def _match_failure(self, method: str, path: str) -> dict[str, Any] | None:
        for failure in self._failures:
            if failure["method"] == method and failure["path"] in path and failure["times"] > 0:
                failure["times"] -= 1
                return failure
        return None

    @staticmethod
    def _fail(failure: dict[str, Any]) -> httpx.Response:
        if failure["timeout"]:
            raise httpx.ReadTimeout("injected timeout")
        return httpx.Response(failure["status"], json={"message": "injected failure"})

    def _route(self, method: str, path: str, params: dict[str, str], body: Any) -> httpx.Response:
        if path == "/v2/account" and method == "GET":
            return httpx.Response(200, json=self._account())
        if path == "/v2/clock" and method == "GET":
            return httpx.Response(200, json={
                "is_open": self.market_open, "timestamp": _stamp(self.clock.now()),
                "next_open": _stamp(self.clock.now() + timedelta(hours=18)),
                "next_close": _stamp(self.next_close),
            })
        if path == "/v2/positions" and method == "GET":
            return httpx.Response(200, json=[self._position(s) for s, p in self.positions.items() if p["qty"] > 0])
        match = re.fullmatch(r"/v2/positions/([A-Z.]+)", path)
        if match and method == "GET":
            symbol = match.group(1)
            if self.position_qty(symbol) <= 0:
                raise _HttpError(404, "position does not exist")
            return httpx.Response(200, json=self._position(symbol))
        match = re.fullmatch(r"/v2/assets/([A-Z.]+)", path)
        if match and method == "GET":
            return httpx.Response(200, json=self._asset(match.group(1)))
        if path == "/v2/account/activities" and method == "GET":
            return httpx.Response(200, json=self._activities(params))
        if path == "/v2/orders" and method == "GET":
            status = params.get("status", "open")
            orders = self.open_orders() if status == "open" else list(self.orders.values())
            return httpx.Response(200, json=[self._poll(o) for o in orders])
        if path == "/v2/orders" and method == "POST":
            order = self._submit_order_body(body)
            return httpx.Response(200, json=order)
        if path == "/v2/orders:by_client_order_id" and method == "GET":
            for order in self.orders.values():
                if order["client_order_id"] == params.get("client_order_id"):
                    return httpx.Response(200, json=self._poll(order))
            raise _HttpError(404, "order not found")
        match = re.fullmatch(r"/v2/orders/([\w-]+)", path)
        if match:
            order = self.orders.get(match.group(1))
            if order is None:
                raise _HttpError(404, "order not found")
            if method == "GET":
                return httpx.Response(200, json=self._poll(order))
            if method == "DELETE":
                return self._delete(order)
            if method == "PATCH":
                return httpx.Response(200, json=self._replace(order, body))
        raise _HttpError(404, f"unhandled {method} {path}")

    def _poll(self, order: dict[str, Any]) -> dict[str, Any]:
        remaining = self._pending_cancel_polls.get(order["id"])
        if remaining is not None:
            if remaining <= 0:
                self._pending_cancel_polls.pop(order["id"])
                if order["status"] == "pending_cancel":
                    order["status"] = "canceled"
                    order["canceled_at"] = _stamp(self.clock.now())
                    self._emit_update("canceled", order)
            else:
                self._pending_cancel_polls[order["id"]] = remaining - 1
        return dict(order)

    def _delete(self, order: dict[str, Any]) -> httpx.Response:
        if order["status"] not in ("accepted", "new", "pending_new", "partially_filled", "pending_cancel"):
            raise _HttpError(422, "order is not cancelable")
        if self.cancel_pending_polls:
            order["status"] = "pending_cancel"
            self._pending_cancel_polls[order["id"]] = self.cancel_pending_polls
        else:
            order["status"] = "canceled"
            order["canceled_at"] = _stamp(self.clock.now())
            self._emit_update("canceled", order)
        order["updated_at"] = _stamp(self.clock.now())
        return httpx.Response(204)

    def _submit_order_body(self, body: dict[str, Any], price_override: Decimal | None = None) -> dict[str, Any]:
        symbol = body["symbol"]
        side, kind = body["side"], body["type"]
        if body.get("qty") is None:
            raise _HttpError(422, "qty is required")
        qty = D(body["qty"])
        if any(o["client_order_id"] == body["client_order_id"] for o in self.orders.values()):
            raise _HttpError(422, "client_order_id must be unique")
        price = price_override if price_override is not None else self.prices.get(symbol)
        if side == "sell":
            available = self.position_qty(symbol) - self._held_sell_qty(symbol)
            if qty > available:
                raise _HttpError(
                    403, f"insufficient qty available for order (requested: {qty}, available: {available})"
                )
        if kind == "stop":
            stop = D(body["stop_price"])
            if self.strict_stop_validation and price is not None and side == "sell" and stop >= price:
                raise _HttpError(422, "stop price must be less than the current price for a sell stop")
            order = self._new_order(symbol=symbol, side=side, type="stop", qty=qty,
                                    tif=body["time_in_force"], client_order_id=body["client_order_id"],
                                    stop_price=stop)
            return dict(order)
        if kind in ("market", "limit"):
            order = self._new_order(symbol=symbol, side=side, type=kind, qty=qty,
                                    tif=body["time_in_force"], client_order_id=body["client_order_id"],
                                    limit_price=D(body["limit_price"]) if body.get("limit_price") else None)
            if self.auto_fill or price_override is not None:
                if price is None:
                    raise _HttpError(422, "no price for market order")
                self._fill(order, qty, price)
            return dict(order)
        if kind == "trailing_stop":
            order = self._new_order(symbol=symbol, side=side, type="trailing_stop", qty=qty,
                                    tif=body["time_in_force"], client_order_id=body["client_order_id"])
            order["trail_percent"] = body.get("trail_percent")
            order["trail_price"] = body.get("trail_price")
            return dict(order)
        raise _HttpError(422, f"unsupported order type {kind}")

    def _replace(self, order: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        if order["status"] not in ("accepted", "new", "partially_filled"):
            raise _HttpError(422, "order is not replaceable")
        qty = D(body["qty"]) if body.get("qty") else D(order["qty"])
        available = self.position_qty(order["symbol"]) - self._held_sell_qty(order["symbol"], exclude=order["id"])
        if order["side"] == "sell" and qty > available:
            raise _HttpError(403, f"insufficient qty available for order (requested: {qty}, available: {available})")
        stop = D(body["stop_price"]) if body.get("stop_price") else (
            D(order["stop_price"]) if order["stop_price"] else None
        )
        replacement = self._new_order(
            symbol=order["symbol"], side=order["side"], type=order["type"], qty=qty,
            tif=body.get("time_in_force", order["time_in_force"]),
            client_order_id=body.get("client_order_id") or f"{order['client_order_id']}-r",
            stop_price=stop,
        )
        replacement["replaces"] = order["id"]
        order["status"] = "replaced"
        order["replaced_by"] = replacement["id"]
        order["replaced_at"] = _stamp(self.clock.now())
        order["updated_at"] = order["replaced_at"]
        self._emit_update("replaced", order)
        return dict(replacement)

    def _account(self) -> dict[str, Any]:
        base = {
            "id": "acct-1", "account_number": "PA0001", "status": "ACTIVE", "currency": "USD",
            "cash": "100000", "equity": "100000", "last_equity": "100000",
            "buying_power": "200000", "regt_buying_power": "200000",
            "daytrading_buying_power": "400000", "non_marginable_buying_power": "100000",
            "portfolio_value": "100000", "multiplier": "2", "pattern_day_trader": False,
            "trading_blocked": False, "transfers_blocked": False, "account_blocked": False,
            "trade_suspended_by_user": False, "shorting_enabled": True, "daytrade_count": 0,
        }
        base.update(self.account_flags)
        return base

    def _position(self, symbol: str) -> dict[str, Any]:
        position = self.positions[symbol]
        price = self.prices.get(symbol, position["avg"])
        held = self._held_sell_qty(symbol)
        return {
            "asset_id": f"asset-{symbol}", "symbol": symbol, "exchange": "NASDAQ",
            "asset_class": "us_equity", "side": "long", "qty": str(position["qty"]),
            "qty_available": str(position["qty"] - held), "avg_entry_price": str(position["avg"]),
            "market_value": str(position["qty"] * price), "cost_basis": str(position["qty"] * position["avg"]),
            "unrealized_pl": str((price - position["avg"]) * position["qty"]), "current_price": str(price),
        }

    def _asset(self, symbol: str) -> dict[str, Any]:
        return {
            "id": f"asset-{symbol}", "class": "us_equity", "exchange": "NASDAQ", "symbol": symbol,
            "status": "active", "tradable": True, "marginable": True, "shortable": True,
            "easy_to_borrow": True, "fractionable": False, "price_increment": "0.01",
            "min_order_size": "1", "min_trade_increment": "1",
        }

    def _activities(self, params: dict[str, str]) -> list[dict[str, Any]]:
        items = list(self.activities)
        after = params.get("after")
        if after:
            cutoff = datetime.fromisoformat(after.replace("Z", "+00:00"))
            items = [a for a in items if datetime.fromisoformat(a["transaction_time"].replace("Z", "+00:00")) > cutoff]
        return items


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def trade_update_frames(exchange: FakeAlpacaExchange) -> list[str]:
    return [json.dumps(message) for message in exchange.stream_messages]


__all__ = ["FakeAlpacaExchange", "FakeClock", "trade_update_frames"]
