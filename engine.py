# File: engine.py

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from models import Order, SYMBOL, Side, Trade
from orderbook import OrderBook


@dataclass(slots=True)
class OrderExecutionResult:
    trades: list[Trade]
    resting_order_added: bool


class MatchingEngine:
    """
    Deterministic limit-order matching engine for a single symbol.

    Determinism assumptions:
    - Single-threaded access by server orchestration.
    - Monotonic sequence IDs for strict ordering decisions.
    - Ordered price indexes in the order book (no unordered iteration for matching).
    """

    def __init__(self, order_book: OrderBook | None = None, debug: bool = False) -> None:
        self._order_book = order_book or OrderBook(debug=debug)
        self._next_order_id = 1
        self._next_trade_id = 1
        self._next_sequence = 1
        self._debug = debug

    def execute_limit_order(
        self,
        trader_id: str,
        side: Side,
        price: int,
        quantity: int,
        symbol: str = SYMBOL,
    ) -> OrderExecutionResult:
        order = Order(
            order_id=self._allocate_order_id(),
            trader_id=trader_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            remaining_quantity=quantity,
            sequence=self._allocate_sequence(),
        )
        trades: list[Trade] = []

        while order.remaining_quantity > 0:
            maker = self._order_book.find_next_matchable_opposite(
                incoming_side=order.side,
                limit_price=order.price,
                taker_trader_id=order.trader_id,
            )
            if maker is None:
                break

            # Self-match prevention guarantee:
            # find_next_matchable_opposite() only returns non-self candidates.
            # A defensive guard is kept here to preserve safety if that contract changes.
            if maker.trader_id == order.trader_id:
                continue

            fill_quantity = min(order.remaining_quantity, maker.remaining_quantity)
            maker.remaining_quantity -= fill_quantity
            order.remaining_quantity -= fill_quantity

            trades.append(
                Trade(
                    trade_id=self._allocate_trade_id(),
                    symbol=symbol,
                    price=maker.price,
                    quantity=fill_quantity,
                    maker_order_id=maker.order_id,
                    taker_order_id=order.order_id,
                    maker_trader_id=maker.trader_id,
                    taker_trader_id=order.trader_id,
                    aggressor_side=order.side,
                    sequence=self._allocate_sequence(),
                )
            )

            if maker.remaining_quantity == 0:
                self._order_book.remove_order(maker)

            if self._debug:
                self._order_book.validate_book_state()

        # Ensure no stale zero-qty levels remain before best-price checks/snapshots.
        self._order_book.compact()

        resting_added = False
        if order.remaining_quantity > 0:
            # If opposite-side crossing liquidity still exists here, it means
            # matching was blocked by SMP-only candidates. Resting this remainder
            # would cross the book, so we intentionally do not rest it.
            if not self._order_book.has_crossing_opposite(order.side, order.price):
                self._order_book.add_resting(order)
                resting_added = True

        self._order_book.compact()
        self._assert_uncrossed_book()

        if self._debug:
            self._order_book.validate_book_state()

        return OrderExecutionResult(trades=trades, resting_order_added=resting_added)

    def place_limit_order(
        self,
        trader_id: str,
        side: Side,
        price: int,
        quantity: int,
        symbol: str = SYMBOL,
    ) -> List[Trade]:
        return self.execute_limit_order(
            trader_id=trader_id,
            side=side,
            price=price,
            quantity=quantity,
            symbol=symbol,
        ).trades

    def get_book_snapshot(self, depth: int = 5) -> dict[str, list[tuple[int, int]]]:
        return self._order_book.get_snapshot(depth=depth)

    def best_bid(self) -> int | None:
        return self._order_book.best_bid()

    def best_ask(self) -> int | None:
        return self._order_book.best_ask()

    def clear_order_book(self) -> None:
        self._order_book.clear()

    def reset_state(self) -> None:
        self._order_book.clear()
        self._next_order_id = 1
        self._next_trade_id = 1
        self._next_sequence = 1

    def _assert_uncrossed_book(self) -> None:
        best_bid = self._order_book.best_bid()
        best_ask = self._order_book.best_ask()
        if best_bid is not None and best_ask is not None:
            assert best_bid < best_ask, (
                f"crossed book invariant violated: best_bid={best_bid}, best_ask={best_ask}"
            )

    def _allocate_order_id(self) -> int:
        current = self._next_order_id
        self._next_order_id += 1
        return current

    def _allocate_trade_id(self) -> int:
        current = self._next_trade_id
        self._next_trade_id += 1
        return current

    def _allocate_sequence(self) -> int:
        current = self._next_sequence
        self._next_sequence += 1
        return current
