# File: message_schemas.py

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from time import time_ns
from typing import Any


class ProtocolError(ValueError):
    """Raised when an inbound/outbound message violates the schema."""


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


def utc_ms() -> int:
    return time_ns() // 1_000_000


def round4(value: float) -> float:
    rounded = round(float(value), 4)
    if rounded == 0:
        return 0.0
    return rounded


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"'{key}' must be a non-empty string")
    return value.strip()


def _require_int(payload: dict[str, Any], key: str, *, min_value: int = 1) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ProtocolError(f"'{key}' must be an integer")
    if value < min_value:
        raise ProtocolError(f"'{key}' must be >= {min_value}")
    return value


def _optional_price(payload: dict[str, Any], key: str = "price") -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ProtocolError(f"'{key}' must be numeric when provided")
    price = float(value)
    if price <= 0:
        raise ProtocolError(f"'{key}' must be > 0")
    return round4(price)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    trader_id: str
    side: Side
    qty: int
    order_type: OrderType
    price: float | None = None
    client_order_id: str | None = None

    @staticmethod
    def from_message(payload: dict[str, Any]) -> "OrderRequest":
        msg_type = payload.get("type")
        if msg_type != "order":
            raise ProtocolError("'type' must be 'order'")

        trader_id = _require_string(payload, "trader_id")
        side_raw = _require_string(payload, "side").lower()
        order_type_raw = str(payload.get("order_type", "limit")).lower()
        qty = _require_int(payload, "qty", min_value=1)
        price = _optional_price(payload, "price")
        client_order_id = payload.get("client_order_id")
        if client_order_id is not None and not isinstance(client_order_id, str):
            raise ProtocolError("'client_order_id' must be a string when provided")

        try:
            side = Side(side_raw)
        except ValueError as exc:
            raise ProtocolError("'side' must be 'buy' or 'sell'") from exc

        try:
            order_type = OrderType(order_type_raw)
        except ValueError as exc:
            raise ProtocolError("'order_type' must be 'limit' or 'market'") from exc

        if order_type == OrderType.LIMIT and price is None:
            raise ProtocolError("'price' is required for limit orders")
        if order_type == OrderType.MARKET and price is not None:
            raise ProtocolError("'price' must be null/omitted for market orders")

        return OrderRequest(
            trader_id=trader_id,
            side=side,
            qty=qty,
            order_type=order_type,
            price=price,
            client_order_id=client_order_id,
        )

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "order",
            "trader_id": self.trader_id,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "price": self.price,
            "qty": self.qty,
            "client_order_id": self.client_order_id,
        }


@dataclass(frozen=True, slots=True)
class OrderAccepted:
    order_id: int
    trader_id: str
    timestamp: int
    client_order_id: str | None = None

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "order_accepted",
            "order_id": self.order_id,
            "trader_id": self.trader_id,
            "client_order_id": self.client_order_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class OrderRejected:
    reason: str
    details: dict[str, Any]
    trader_id: str | None
    timestamp: int
    client_order_id: str | None = None

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "order_rejected",
            "reason": self.reason,
            "details": self.details,
            "trader_id": self.trader_id,
            "client_order_id": self.client_order_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class TradeEvent:
    trade_id: int
    price: float
    qty: int
    buy_trader_id: str
    sell_trader_id: str
    timestamp: int

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "trade",
            "trade_id": self.trade_id,
            "price": round4(self.price),
            "qty": self.qty,
            "buy_trader_id": self.buy_trader_id,
            "sell_trader_id": self.sell_trader_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class BookUpdateEvent:
    best_bid: float | None
    best_ask: float | None
    bids: list[tuple[float, int]]
    asks: list[tuple[float, int]]
    timestamp: int

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "book_update",
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "bids": self.bids,
            "asks": self.asks,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class LiquidationEvent:
    trader_id: str
    reason: str
    qty: int
    side: Side
    timestamp: int

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "liquidation",
            "trader_id": self.trader_id,
            "reason": self.reason,
            "qty": self.qty,
            "side": self.side.value,
            "timestamp": self.timestamp,
        }
