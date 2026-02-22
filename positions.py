# File: positions.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models import Side, Trade


def _round4(value: float) -> float:
    rounded = round(value, 4)
    # Remove negative-zero artifacts for deterministic snapshots.
    if rounded == 0:
        return 0.0
    return rounded


@dataclass(slots=True)
class Position:
    trader_id: str
    position: int = 0
    cash: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    last_trade_price: float = 0.0


class PositionManager:
    """
    Deterministic position and PnL manager.

    The matching engine emits trades. This manager consumes trades and
    performs accounting updates only after executions occur.
    """

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    @staticmethod
    def trade_counterparties(trade: Trade) -> tuple[str, str]:
        if trade.aggressor_side == Side.BUY:
            return trade.taker_trader_id, trade.maker_trader_id
        return trade.maker_trader_id, trade.taker_trader_id

    def update_from_trade(self, trade: Trade) -> None:
        buyer_id, seller_id = self.trade_counterparties(trade)
        price = float(trade.price)
        quantity = int(trade.quantity)

        self._apply_fill(trader_id=buyer_id, side=Side.BUY, price=price, quantity=quantity)
        self._apply_fill(trader_id=seller_id, side=Side.SELL, price=price, quantity=quantity)

    def get_position_snapshot(self, trader_id: str) -> dict[str, Any]:
        position = self._ensure_position(trader_id)
        return self._snapshot_from_position(position)

    def get_position_snapshot_for_risk(self, trader_id: str) -> dict[str, Any]:
        """
        Return a trader snapshot without mutating internal state.

        Used by pre-trade risk checks to keep validation side-effect free.
        """
        position = self._positions.get(trader_id)
        if position is None:
            return {
                "trader_id": trader_id,
                "position": 0,
                "cash": 0.0,
                "avg_entry_price": 0.0,
                "realized_pnl": 0.0,
                "last_trade_price": 0.0,
                "unrealized_pnl": 0.0,
                "total_pnl": 0.0,
            }
        return self._snapshot_from_position(position)

    def _snapshot_from_position(self, position: Position) -> dict[str, Any]:
        unrealized = self._compute_unrealized_pnl(position)
        total = _round4(position.realized_pnl + unrealized)

        return {
            "trader_id": position.trader_id,
            "position": position.position,
            "cash": position.cash,
            "avg_entry_price": position.avg_entry_price,
            "realized_pnl": position.realized_pnl,
            "last_trade_price": position.last_trade_price,
            "unrealized_pnl": unrealized,
            "total_pnl": total,
        }

    def get_all_positions(self) -> list[dict[str, Any]]:
        trader_ids = sorted(self._positions.keys())
        return [self.get_position_snapshot(trader_id) for trader_id in trader_ids]

    def get_leaderboard(self) -> list[dict[str, Any]]:
        leaderboard = self.get_all_positions()
        # Deterministic sort: total PnL desc, trader_id asc.
        leaderboard.sort(key=lambda row: (-row["total_pnl"], row["trader_id"]))
        return leaderboard

    def force_close_all(self, mark_price: float) -> list[dict[str, Any]]:
        """
        Flatten every open position at a deterministic session-end mark price.
        """
        close_price = _round4(mark_price)
        updated: list[dict[str, Any]] = []

        for trader_id in sorted(self._positions.keys()):
            position_obj = self._positions[trader_id]
            qty = abs(position_obj.position)
            if qty == 0:
                continue

            close_side = Side.SELL if position_obj.position > 0 else Side.BUY
            self._apply_fill(
                trader_id=trader_id,
                side=close_side,
                price=close_price,
                quantity=qty,
            )
            updated.append(self.get_position_snapshot(trader_id))

        return updated

    def reset(self) -> None:
        self._positions.clear()

    def _ensure_position(self, trader_id: str) -> Position:
        position = self._positions.get(trader_id)
        if position is None:
            position = Position(trader_id=trader_id)
            self._positions[trader_id] = position
        return position

    def _apply_fill(self, trader_id: str, side: Side, price: float, quantity: int) -> None:
        position = self._ensure_position(trader_id)
        old_pos = position.position
        delta = quantity if side == Side.BUY else -quantity
        new_pos = old_pos + delta

        # Cash always updates from execution notional.
        notional = price * quantity
        if side == Side.BUY:
            position.cash = _round4(position.cash - notional)
        else:
            position.cash = _round4(position.cash + notional)

        if old_pos == 0:
            position.position = new_pos
            position.avg_entry_price = _round4(price if new_pos != 0 else 0.0)
            position.last_trade_price = _round4(price)
            return

        if old_pos * delta > 0:
            # Increasing exposure in same direction: weighted average entry price.
            old_abs = abs(old_pos)
            add_abs = abs(delta)
            total_abs = old_abs + add_abs
            weighted_avg = ((position.avg_entry_price * old_abs) + (price * add_abs)) / total_abs
            position.position = new_pos
            position.avg_entry_price = _round4(weighted_avg)
            position.last_trade_price = _round4(price)
            return

        # Reducing, closing, or crossing through zero.
        close_qty = min(abs(old_pos), abs(delta))

        if old_pos > 0:
            # Closing long via sell.
            realized_delta = (price - position.avg_entry_price) * close_qty
        else:
            # Closing short via buy.
            realized_delta = (position.avg_entry_price - price) * close_qty
        position.realized_pnl = _round4(position.realized_pnl + realized_delta)

        position.position = new_pos
        if new_pos == 0:
            position.avg_entry_price = 0.0
        elif old_pos * new_pos < 0:
            # Position crossed zero; residual opens at this trade price.
            position.avg_entry_price = _round4(price)
        # If still same sign after reduction, keep prior avg_entry_price unchanged.

        position.last_trade_price = _round4(price)

    @staticmethod
    def _compute_unrealized_pnl(position: Position) -> float:
        if position.position == 0:
            return 0.0
        return _round4(position.position * (position.last_trade_price - position.avg_entry_price))
