# File: margin_risk_manager.py

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Any


STARTING_CAPITAL = 10_000.0
INITIAL_MARGIN_RATE = 0.20
MAINT_MARGIN_RATE = 0.10


def _round4(value: float) -> float:
    rounded = round(value, 4)
    if rounded == 0:
        return 0.0
    return rounded


@dataclass(frozen=True, slots=True)
class MarginConfig:
    starting_capital: float = STARTING_CAPITAL
    initial_margin_rate: float = INITIAL_MARGIN_RATE
    maintenance_margin_rate: float = MAINT_MARGIN_RATE


class MarginRiskManager:
    """
    Pure futures-style margin risk manager.

    - validate_initial_margin(): pre-trade initial margin check
    - check_maintenance(): post-trade maintenance margin check
    - perform_liquidation(): deterministic liquidation order proposal
    """

    def __init__(self, config: MarginConfig | None = None) -> None:
        self._config = config or MarginConfig()

    def validate_initial_margin(
        self,
        trader_id: str,
        order: dict[str, Any],
        position_snapshot: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | None]:
        _ = trader_id

        side = str(order["side"]).upper()
        price = float(order["price"])
        quantity = int(order["quantity"])
        current_position = int(position_snapshot.get("position", 0))

        delta = quantity if side == "BUY" else -quantity
        projected_position = current_position + delta
        projected_notional = abs(projected_position * price)
        required_initial_margin = _round4(projected_notional * self._config.initial_margin_rate)
        equity = self._compute_equity(position_snapshot)

        if equity < required_initial_margin:
            return False, {
                "type": "order_rejected",
                "reason": "initial_margin_insufficient",
                "required_margin": required_initial_margin,
                "equity": equity,
                "order": order,
            }
        return True, None

    def check_maintenance(
        self,
        trader_id: str,
        position_snapshot: dict[str, Any],
        mark_price: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        _ = trader_id

        position = int(position_snapshot.get("position", 0))
        avg_entry_price = float(position_snapshot.get("avg_entry_price", 0.0))
        if mark_price is None:
            mark_price = float(position_snapshot.get("last_trade_price", 0.0))

        unrealized = _round4(position * (mark_price - avg_entry_price))
        equity = _round4(self._account_cash(position_snapshot) + unrealized)
        maintenance_requirement = _round4(
            abs(position * mark_price) * self._config.maintenance_margin_rate
        )
        breach = position != 0 and equity <= maintenance_requirement

        return breach, {
            "equity": equity,
            "maintenance_requirement": maintenance_requirement,
            "mark_price": _round4(mark_price),
            "position": position,
        }

    def perform_liquidation(
        self,
        trader_id: str,
        position_snapshot: dict[str, Any],
        best_bid: int | None,
        best_ask: int | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        position = int(position_snapshot.get("position", 0))
        if position == 0:
            return None, {
                "type": "liquidation",
                "trader_id": trader_id,
                "reason": "maintenance_margin_breach",
            }

        avg_entry_price = float(position_snapshot.get("avg_entry_price", 0.0))
        account_cash = self._account_cash(position_snapshot)

        # Deterministic mark-price calculation. If one side is absent, use the
        # available side as a synthetic midpoint anchor for risk computation.
        if best_bid is None and best_ask is None:
            return None, {
                "type": "liquidation",
                "trader_id": trader_id,
                "reason": "maintenance_margin_breach",
            }
        mark_bid = float(best_bid if best_bid is not None else best_ask)
        mark_ask = float(best_ask if best_ask is not None else best_bid)
        mark_price = _round4((mark_bid + mark_ask) / 2.0)

        unrealized = _round4(position * (mark_price - avg_entry_price))
        equity = _round4(account_cash + unrealized)
        maintenance_requirement = _round4(
            abs(position * mark_price) * self._config.maintenance_margin_rate
        )

        if equity > maintenance_requirement:
            return None, {
                "type": "liquidation",
                "trader_id": trader_id,
                "reason": "maintenance_margin_breach",
                "mark_price": mark_price,
                "equity": equity,
                "maintenance_requirement": maintenance_requirement,
            }

        denominator = mark_price * self._config.initial_margin_rate
        if denominator <= 0:
            return None, {
                "type": "liquidation",
                "trader_id": trader_id,
                "reason": "maintenance_margin_breach",
            }

        raw_target_abs = floor(equity / denominator)
        safe_target_abs = max(0, raw_target_abs)
        target_abs = min(abs(position), safe_target_abs)
        target_position = target_abs if position > 0 else -target_abs

        # Signed distance from current to target; absolute value is liquidation size.
        signed_liquidation = position - target_position
        qty = abs(signed_liquidation)
        qty = max(1, qty)
        qty = min(qty, abs(position))

        if position > 0:
            if best_bid is None:
                return None, {
                    "type": "liquidation",
                    "trader_id": trader_id,
                    "reason": "maintenance_margin_breach",
                }
            order = {"side": "SELL", "price": int(best_bid), "quantity": qty}
        else:
            if best_ask is None:
                return None, {
                    "type": "liquidation",
                    "trader_id": trader_id,
                    "reason": "maintenance_margin_breach",
                }
            order = {"side": "BUY", "price": int(best_ask), "quantity": qty}

        return order, {
            "type": "liquidation",
            "trader_id": trader_id,
            "reason": "maintenance_margin_breach",
            "mark_price": mark_price,
            "equity": equity,
            "maintenance_requirement": maintenance_requirement,
            "target_position": target_position,
            "requested_quantity": qty,
        }

    def _compute_equity(self, position_snapshot: dict[str, Any]) -> float:
        cash = self._account_cash(position_snapshot)
        unrealized_pnl = float(position_snapshot.get("unrealized_pnl", 0.0))
        return _round4(cash + unrealized_pnl)

    def _account_cash(self, position_snapshot: dict[str, Any]) -> float:
        """
        Convert trade-ledger cash to account cash by adding starting capital.
        """
        cash_ledger = float(position_snapshot.get("cash", 0.0))
        return _round4(self._config.starting_capital + cash_ledger)
