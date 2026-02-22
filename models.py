# File: models.py

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


SYMBOL = "TEST"


class ValidationError(ValueError):
    """Raised when an inbound protocol message is invalid."""


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(slots=True)
class Order:
    order_id: int
    trader_id: str
    symbol: str
    side: Side
    price: int
    quantity: int
    remaining_quantity: int
    sequence: int


@dataclass(slots=True)
class Trade:
    trade_id: int
    symbol: str
    price: int
    quantity: int
    maker_order_id: int
    taker_order_id: int
    maker_trader_id: str
    taker_trader_id: str
    aggressor_side: Side
    sequence: int

    def to_event(self) -> dict[str, Any]:
        return {
            "type": "trade",
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "price": self.price,
            "quantity": self.quantity,
            "maker_order_id": self.maker_order_id,
            "taker_order_id": self.taker_order_id,
            "maker_trader_id": self.maker_trader_id,
            "taker_trader_id": self.taker_trader_id,
            "aggressor_side": self.aggressor_side.value,
            "sequence": self.sequence,
        }


@dataclass(slots=True)
class Position:
    trader_id: str
    net_position: int = 0
    cash: int = 0


def parse_side(raw: Any) -> Side:
    if not isinstance(raw, str):
        raise ValidationError("field 'side' must be a string")
    try:
        return Side(raw.upper())
    except ValueError as exc:
        raise ValidationError("field 'side' must be BUY or SELL") from exc


def parse_positive_int(raw: Any, field_name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError(f"field '{field_name}' must be an integer")
    if raw <= 0:
        raise ValidationError(f"field '{field_name}' must be > 0")
    return raw


def parse_place_order_message(message: dict[str, Any]) -> tuple[Side, int, int]:
    if not isinstance(message, dict):
        raise ValidationError("message must be a JSON object")

    msg_type = message.get("type")
    if msg_type != "place_order":
        raise ValidationError("unsupported message type")

    side = parse_side(message.get("side"))
    price = parse_positive_int(message.get("price"), "price")
    quantity = parse_positive_int(message.get("quantity"), "quantity")
    return side, price, quantity
