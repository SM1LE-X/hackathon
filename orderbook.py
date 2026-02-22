# File: orderbook.py

from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque
from typing import Deque, Optional

from models import Order, Side


class OrderBook:
    """Single-symbol order book with price-time priority."""

    def __init__(self, debug: bool = False) -> None:
        self._bids: dict[int, Deque[Order]] = {}
        self._asks: dict[int, Deque[Order]] = {}
        self._bid_prices: list[int] = []  # Ascending; best bid is last.
        self._ask_prices: list[int] = []  # Ascending; best ask is first.
        self._debug = debug

    def best_bid(self) -> Optional[int]:
        return self._bid_prices[-1] if self._bid_prices else None

    def best_ask(self) -> Optional[int]:
        return self._ask_prices[0] if self._ask_prices else None

    def add_resting(self, order: Order) -> None:
        if order.side == Side.BUY:
            self._add_order(self._bids, self._bid_prices, order.price, order)
        else:
            self._add_order(self._asks, self._ask_prices, order.price, order)
        if self._debug:
            self.validate_book_state()

    def peek_best_opposite(self, incoming_side: Side) -> Optional[Order]:
        if incoming_side == Side.BUY:
            best_price = self.best_ask()
            if best_price is None:
                return None
            return self._asks[best_price][0]

        best_price = self.best_bid()
        if best_price is None:
            return None
        return self._bids[best_price][0]

    def find_next_matchable_opposite(
        self,
        incoming_side: Side,
        limit_price: int,
        taker_trader_id: str,
    ) -> Optional[Order]:
        """
        Return the next matchable resting order in deterministic priority order.

        Self-match prevention is implemented by skipping orders owned by the
        taker trader. The skipped resting order is not removed or reordered.
        """
        if incoming_side == Side.BUY:
            for price in self._ask_prices:
                if price > limit_price:
                    break
                for candidate in self._asks[price]:
                    if candidate.trader_id == taker_trader_id:
                        continue
                    return candidate
            return None

        for price in reversed(self._bid_prices):
            if price < limit_price:
                break
            for candidate in self._bids[price]:
                if candidate.trader_id == taker_trader_id:
                    continue
                return candidate
        return None

    def pop_best_opposite(self, incoming_side: Side) -> Optional[Order]:
        if incoming_side == Side.BUY:
            best_price = self.best_ask()
            if best_price is None:
                return None
            return self._popleft(self._asks, self._ask_prices, best_price)

        best_price = self.best_bid()
        if best_price is None:
            return None
        return self._popleft(self._bids, self._bid_prices, best_price)

    def remove_order(self, order: Order) -> None:
        if order.side == Side.BUY:
            self._remove_specific(self._bids, self._bid_prices, order)
        else:
            self._remove_specific(self._asks, self._ask_prices, order)
        if self._debug:
            self.validate_book_state()

    def get_snapshot(self, depth: int = 5) -> dict[str, list[tuple[int, int]]]:
        capped_depth = max(depth, 0)
        bid_levels: list[tuple[int, int]] = []
        ask_levels: list[tuple[int, int]] = []

        for price in reversed(self._bid_prices):
            level = self._bids[price]
            total_quantity = sum(order.remaining_quantity for order in level)
            bid_levels.append((price, total_quantity))
            if len(bid_levels) >= capped_depth:
                break

        for price in self._ask_prices:
            level = self._asks[price]
            total_quantity = sum(order.remaining_quantity for order in level)
            ask_levels.append((price, total_quantity))
            if len(ask_levels) >= capped_depth:
                break

        return {"bids": bid_levels, "asks": ask_levels}

    def has_crossing_opposite(self, incoming_side: Side, limit_price: int) -> bool:
        """
        Return True when resting opposite liquidity still crosses the incoming limit.

        This is used by the engine to avoid resting a remainder that would leave
        the book crossed (for example, when all crossing liquidity is self-owned
        and was skipped by SMP).
        """
        if incoming_side == Side.BUY:
            best_ask = self.best_ask()
            return best_ask is not None and best_ask <= limit_price

        best_bid = self.best_bid()
        return best_bid is not None and best_bid >= limit_price

    def compact(self) -> None:
        """
        Remove zero-quantity orders and empty levels before computing best prices.
        """
        self._compact_side(self._bids, self._bid_prices)
        self._compact_side(self._asks, self._ask_prices)
        if self._debug:
            self.validate_book_state()

    def clear(self) -> None:
        """
        Remove all resting orders and price levels.
        """
        self._bids.clear()
        self._asks.clear()
        self._bid_prices.clear()
        self._ask_prices.clear()

    def validate_book_state(self) -> None:
        self._validate_side(self._bids, self._bid_prices, Side.BUY)
        self._validate_side(self._asks, self._ask_prices, Side.SELL)

        if self._bid_prices != sorted(self._bid_prices):
            raise AssertionError("bid price index must be sorted ascending")
        if self._ask_prices != sorted(self._ask_prices):
            raise AssertionError("ask price index must be sorted ascending")

    @staticmethod
    def _validate_side(
        book: dict[int, Deque[Order]],
        prices: list[int],
        expected_side: Side,
    ) -> None:
        if len(prices) != len(set(prices)):
            raise AssertionError("price index contains duplicates")
        if set(prices) != set(book.keys()):
            raise AssertionError("price index and price-level map diverged")

        for price in prices:
            level = book.get(price)
            if level is None or len(level) == 0:
                raise AssertionError("price level cannot be empty")
            last_sequence = -1
            for order in level:
                if order.side != expected_side:
                    raise AssertionError("order side does not match book side")
                if order.price != price:
                    raise AssertionError("order price does not match price level")
                if order.remaining_quantity <= 0:
                    raise AssertionError("zero or negative remaining quantity found")
                if order.sequence <= last_sequence:
                    raise AssertionError("FIFO sequence integrity violated")
                last_sequence = order.sequence

    @staticmethod
    def _add_order(
        book: dict[int, Deque[Order]],
        prices: list[int],
        price: int,
        order: Order,
    ) -> None:
        level = book.get(price)
        if level is None:
            level = deque()
            book[price] = level
            insort(prices, price)
        level.append(order)

    @staticmethod
    def _popleft(
        book: dict[int, Deque[Order]],
        prices: list[int],
        price: int,
    ) -> Order:
        level = book[price]
        order = level.popleft()
        if not level:
            del book[price]
            idx = bisect_left(prices, price)
            if idx < len(prices) and prices[idx] == price:
                prices.pop(idx)
        return order

    @staticmethod
    def _remove_specific(
        book: dict[int, Deque[Order]],
        prices: list[int],
        order: Order,
    ) -> None:
        level = book.get(order.price)
        if level is None:
            raise KeyError(f"price level {order.price} not found")
        level.remove(order)
        if not level:
            del book[order.price]
            idx = bisect_left(prices, order.price)
            if idx < len(prices) and prices[idx] == order.price:
                prices.pop(idx)

    @staticmethod
    def _compact_side(
        book: dict[int, Deque[Order]],
        prices: list[int],
    ) -> None:
        kept_prices: list[int] = []
        for price in prices:
            level = book.get(price)
            if level is None:
                continue
            filtered = deque(order for order in level if order.remaining_quantity > 0)
            if filtered:
                book[price] = filtered
                kept_prices.append(price)
            else:
                del book[price]
        prices[:] = kept_prices
