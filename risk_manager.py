# File: risk_manager.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


STARTING_CASH = 10_000.0
MAX_POSITION = 50
MAX_NOTIONAL = 5_000.0


@dataclass(frozen=True, slots=True)
class RiskConfig:
    starting_cash: float = STARTING_CASH
    max_position: int = MAX_POSITION
    max_notional: float = MAX_NOTIONAL


class RiskManager:
    """
    Pure pre-trade risk validator.

    This component does not mutate exchange state and does not depend on
    matching internals. It consumes an order payload and a trader snapshot.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self._config = config or RiskConfig()

    def validate_order(
        self,
        trader_id: str,
        order: dict[str, Any],
        position_snapshot: dict[str, Any],
    ) -> tuple[bool, str | None]:
        _ = trader_id  # explicit for signature parity and future policy hooks

        side_raw = order.get("side")
        price = int(order.get("price"))
        quantity = int(order.get("quantity"))
        side = str(side_raw).upper()

        current_position = int(position_snapshot.get("position", 0))
        cash_ledger = float(position_snapshot.get("cash", 0.0))

        # PositionManager cash is a trade-ledger delta from 0; available cash
        # for risk checks includes starting capital baseline.
        available_cash = round(self._config.starting_cash + cash_ledger, 4)

        if side == "BUY":
            required_cash = round(price * quantity, 4)
            if available_cash < required_cash:
                return False, "insufficient_cash"
            delta = quantity
        elif side == "SELL":
            # Explicit rule from spec.
            if abs(current_position - quantity) > self._config.max_position:
                return False, "position_limit"
            delta = -quantity
        else:
            # Should be unreachable due to protocol validation.
            return False, "position_limit"

        new_position = current_position + delta
        if abs(new_position) > self._config.max_position:
            return False, "position_limit"

        if abs(new_position * price) > self._config.max_notional:
            return False, "notional_limit"

        return True, None
