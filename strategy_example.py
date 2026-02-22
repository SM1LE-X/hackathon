# File: strategy_example.py

from __future__ import annotations

import random

from bot_strategies import StrategyContext
from message_schemas import OrderRequest, OrderType, Side, round4


class MyStrategy:
    """
    Example custom strategy for bot_client --strategy module:Class integration.

    Launch:
    python bot_client.py --trader-id dev_1 --strategy strategy_example:MyStrategy --strategy-param qty=2
    """

    def __init__(self, *, trader_id: str, rng: random.Random, params: dict[str, str]) -> None:
        self._trader_id = trader_id
        self._rng = rng
        self._qty = max(1, int(params.get("qty", "2")))

    def next_order(self, context: StrategyContext) -> OrderRequest | None:
        if context.best_bid is None and context.best_ask is None:
            return None

        side = Side.BUY if self._rng.random() < 0.5 else Side.SELL
        if side == Side.BUY:
            price = context.best_bid if context.best_bid is not None else context.mid_price
        else:
            price = context.best_ask if context.best_ask is not None else context.mid_price

        return OrderRequest(
            trader_id=self._trader_id,
            side=side,
            qty=self._qty,
            order_type=OrderType.LIMIT,
            price=round4(max(0.01, price)),
            client_order_id=f"{self._trader_id}-custom-{self._rng.randint(1000, 9999)}",
        )
