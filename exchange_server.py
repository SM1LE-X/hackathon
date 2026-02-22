# File: exchange_server.py

from __future__ import annotations

import argparse
import asyncio
import bisect
import contextlib
import json
import logging
import signal
from collections import deque
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServerProtocol

from exporter import CSVExporter
from message_schemas import (
    BookUpdateEvent,
    LiquidationEvent,
    OrderAccepted,
    OrderRejected,
    OrderRequest,
    OrderType,
    ProtocolError,
    Side,
    TradeEvent,
    round4,
    utc_ms,
)

LOGGER = logging.getLogger("exchange_server")

LIQUIDATION_COOLDOWN_MS = 500


@dataclass(slots=True)
class BookOrder:
    order_id: int
    trader_id: str
    side: Side
    price: float
    remaining_qty: int


@dataclass(frozen=True, slots=True)
class Execution:
    trade_id: int
    price: float
    qty: int
    buy_trader_id: str
    sell_trader_id: str


class MatchingEngine:
    """
    Deterministic FIFO matching engine.

    - Price-time priority via sorted price ladders + deque per level.
    - No async in matching path.
    - Full matching completes before book snapshot/assertions.
    """

    def __init__(self) -> None:
        self._bid_levels: dict[float, deque[BookOrder]] = {}
        self._ask_levels: dict[float, deque[BookOrder]] = {}
        self._bid_prices_desc: list[float] = []
        self._ask_prices_asc: list[float] = []
        self._next_trade_id = 1

    @property
    def best_bid(self) -> float | None:
        return self._bid_prices_desc[0] if self._bid_prices_desc else None

    @property
    def best_ask(self) -> float | None:
        return self._ask_prices_asc[0] if self._ask_prices_asc else None

    def get_book_snapshot(self, depth: int = 10) -> dict[str, list[tuple[float, int]]]:
        bids: list[tuple[float, int]] = []
        asks: list[tuple[float, int]] = []

        for price in self._bid_prices_desc[: max(0, depth)]:
            level = self._bid_levels[price]
            total = sum(order.remaining_qty for order in level)
            bids.append((price, total))
        for price in self._ask_prices_asc[: max(0, depth)]:
            level = self._ask_levels[price]
            total = sum(order.remaining_qty for order in level)
            asks.append((price, total))
        return {"bids": bids, "asks": asks}

    def process(self, order: OrderRequest, order_id: int) -> tuple[list[Execution], bool]:
        executions: list[Execution] = []
        book_changed = False

        remaining = order.qty
        if order.side == Side.BUY:
            executions, remaining, matched = self._match_buy(order, remaining, order_id)
        else:
            executions, remaining, matched = self._match_sell(order, remaining, order_id)
        book_changed = book_changed or matched

        if order.order_type == OrderType.LIMIT and remaining > 0:
            assert order.price is not None
            self._rest_limit_order(
                BookOrder(
                    order_id=order_id,
                    trader_id=order.trader_id,
                    side=order.side,
                    price=order.price,
                    remaining_qty=remaining,
                )
            )
            book_changed = True

        best_bid = self.best_bid
        best_ask = self.best_ask
        if best_bid is not None and best_ask is not None:
            assert best_bid < best_ask, "crossed-book invariant violated after matching"

        return executions, book_changed

    def _match_buy(self, order: OrderRequest, remaining: int, order_id: int) -> tuple[list[Execution], int, bool]:
        executions: list[Execution] = []
        book_changed = False
        while remaining > 0 and self._ask_prices_asc:
            best_ask = self._ask_prices_asc[0]
            if order.order_type == OrderType.LIMIT and order.price is not None and best_ask > order.price:
                break

            level = self._ask_levels[best_ask]
            while remaining > 0 and level:
                resting = level[0]
                fill = min(remaining, resting.remaining_qty)
                resting.remaining_qty -= fill
                remaining -= fill

                executions.append(
                    Execution(
                        trade_id=self._next_trade_id,
                        price=best_ask,
                        qty=fill,
                        buy_trader_id=order.trader_id,
                        sell_trader_id=resting.trader_id,
                    )
                )
                self._next_trade_id += 1
                book_changed = True

                if resting.remaining_qty == 0:
                    level.popleft()

            if not level:
                del self._ask_levels[best_ask]
                self._ask_prices_asc.pop(0)
        return executions, remaining, book_changed

    def _match_sell(self, order: OrderRequest, remaining: int, order_id: int) -> tuple[list[Execution], int, bool]:
        executions: list[Execution] = []
        book_changed = False
        while remaining > 0 and self._bid_prices_desc:
            best_bid = self._bid_prices_desc[0]
            if order.order_type == OrderType.LIMIT and order.price is not None and best_bid < order.price:
                break

            level = self._bid_levels[best_bid]
            while remaining > 0 and level:
                resting = level[0]
                fill = min(remaining, resting.remaining_qty)
                resting.remaining_qty -= fill
                remaining -= fill

                executions.append(
                    Execution(
                        trade_id=self._next_trade_id,
                        price=best_bid,
                        qty=fill,
                        buy_trader_id=resting.trader_id,
                        sell_trader_id=order.trader_id,
                    )
                )
                self._next_trade_id += 1
                book_changed = True

                if resting.remaining_qty == 0:
                    level.popleft()

            if not level:
                del self._bid_levels[best_bid]
                self._bid_prices_desc.pop(0)
        return executions, remaining, book_changed

    def _rest_limit_order(self, order: BookOrder) -> None:
        if order.side == Side.BUY:
            if order.price not in self._bid_levels:
                self._bid_levels[order.price] = deque()
                bisect.insort_left(self._bid_prices_desc, order.price)
                self._bid_prices_desc.sort(reverse=True)
            self._bid_levels[order.price].append(order)
            return

        if order.price not in self._ask_levels:
            self._ask_levels[order.price] = deque()
            bisect.insort_left(self._ask_prices_asc, order.price)
        self._ask_levels[order.price].append(order)

    def cancel_trader_orders(self, trader_id: str) -> bool:
        """
        Remove all resting orders for a trader.

        Returns True when the visible book changed.
        """
        changed = False

        bid_prices = list(self._bid_prices_desc)
        for price in bid_prices:
            level = self._bid_levels[price]
            kept = deque(order for order in level if order.trader_id != trader_id)
            if len(kept) != len(level):
                changed = True
            if kept:
                self._bid_levels[price] = kept
            else:
                del self._bid_levels[price]
                self._bid_prices_desc.remove(price)

        ask_prices = list(self._ask_prices_asc)
        for price in ask_prices:
            level = self._ask_levels[price]
            kept = deque(order for order in level if order.trader_id != trader_id)
            if len(kept) != len(level):
                changed = True
            if kept:
                self._ask_levels[price] = kept
            else:
                del self._ask_levels[price]
                self._ask_prices_asc.remove(price)

        return changed


@dataclass(slots=True)
class PositionState:
    trader_id: str
    position: int
    cash: float
    avg_entry_price: float
    realized_pnl: float
    last_trade_price: float


class PositionEngine:
    """Tracks per-trader position, cash, average entry, and realized PnL."""

    def __init__(self, starting_cash: float = 10_000.0) -> None:
        self._starting_cash = round4(starting_cash)
        self._positions: dict[str, PositionState] = {}

    def _ensure(self, trader_id: str) -> PositionState:
        state = self._positions.get(trader_id)
        if state is not None:
            return state
        state = PositionState(
            trader_id=trader_id,
            position=0,
            cash=self._starting_cash,
            avg_entry_price=0.0,
            realized_pnl=0.0,
            last_trade_price=0.0,
        )
        self._positions[trader_id] = state
        return state

    def apply_execution(self, trade: Execution) -> None:
        self._apply_fill(trade.buy_trader_id, Side.BUY, trade.qty, trade.price)
        self._apply_fill(trade.sell_trader_id, Side.SELL, trade.qty, trade.price)

    def _apply_fill(self, trader_id: str, side: Side, qty: int, price: float) -> None:
        state = self._ensure(trader_id)
        sign = 1 if side == Side.BUY else -1
        old_position = state.position
        new_position = old_position + sign * qty

        if side == Side.BUY:
            state.cash = round4(state.cash - (price * qty))
        else:
            state.cash = round4(state.cash + (price * qty))
        state.last_trade_price = round4(price)

        if old_position == 0:
            state.position = new_position
            state.avg_entry_price = round4(price) if new_position != 0 else 0.0
            return

        if old_position > 0 and sign > 0:
            total_qty = old_position + qty
            state.avg_entry_price = round4(
                ((state.avg_entry_price * old_position) + (price * qty)) / total_qty
            )
            state.position = total_qty
            return

        if old_position < 0 and sign < 0:
            total_qty = abs(old_position) + qty
            state.avg_entry_price = round4(
                ((state.avg_entry_price * abs(old_position)) + (price * qty)) / total_qty
            )
            state.position = -total_qty
            return

        closed_qty = min(abs(old_position), qty)
        if old_position > 0:
            state.realized_pnl = round4(state.realized_pnl + ((price - state.avg_entry_price) * closed_qty))
        else:
            state.realized_pnl = round4(state.realized_pnl + ((state.avg_entry_price - price) * closed_qty))

        state.position = new_position
        if new_position == 0:
            state.avg_entry_price = 0.0
        elif old_position > 0 and new_position < 0:
            state.avg_entry_price = round4(price)
        elif old_position < 0 and new_position > 0:
            state.avg_entry_price = round4(price)

    def get(self, trader_id: str) -> PositionState:
        return self._ensure(trader_id)

    def unrealized_pnl(self, trader_id: str, mark_price: float) -> float:
        state = self._ensure(trader_id)
        if state.position == 0:
            return 0.0
        return round4((mark_price - state.avg_entry_price) * state.position)

    def equity(self, trader_id: str, mark_price: float) -> float:
        state = self._ensure(trader_id)
        return round4(state.cash + self.unrealized_pnl(trader_id, mark_price))


class RiskEngine:
    """Pure risk checks and deterministic liquidation order generation."""

    def __init__(self, initial_margin_rate: float = 0.20, maintenance_margin_rate: float = 0.10) -> None:
        self.initial_margin_rate = round4(initial_margin_rate)
        self.maintenance_margin_rate = round4(maintenance_margin_rate)

    def validate_initial_margin(
        self,
        order: OrderRequest,
        positions: PositionEngine,
        mark_price: float,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        state = positions.get(order.trader_id)
        projected = state.position + (order.qty if order.side == Side.BUY else -order.qty)
        ref_price = order.price if order.order_type == OrderType.LIMIT else mark_price
        if ref_price <= 0:
            return False, "invalid_price_reference", {"mark_price": mark_price}

        required_margin = round4(abs(projected * ref_price) * self.initial_margin_rate)
        equity = positions.equity(order.trader_id, mark_price)
        if equity + 1e-9 < required_margin:
            return False, "initial_margin_insufficient", {"equity": equity, "required_margin": required_margin}
        return True, None, {}

    def maintenance_breached(self, trader_id: str, positions: PositionEngine, mark_price: float) -> bool:
        state = positions.get(trader_id)
        if state.position == 0:
            return False
        equity = positions.equity(trader_id, mark_price)
        requirement = round4(abs(state.position * mark_price) * self.maintenance_margin_rate)
        return equity + 1e-9 < requirement

    def maintenance_requirement(self, trader_id: str, positions: PositionEngine, mark_price: float) -> float:
        state = positions.get(trader_id)
        return round4(abs(state.position * mark_price) * self.maintenance_margin_rate)

    def required_liquidation_qty(self, trader_id: str, positions: PositionEngine, mark_price: float) -> int:
        """
        Compute deterministic close quantity needed to restore maintenance margin.

        Returns qty in [0, abs(position)].
        """
        state = positions.get(trader_id)
        abs_pos = abs(state.position)
        if abs_pos == 0:
            return 0

        equity = positions.equity(trader_id, mark_price)
        requirement = self.maintenance_requirement(trader_id, positions, mark_price)
        if equity + 1e-9 >= requirement:
            return 0

        if mark_price <= 0 or self.maintenance_margin_rate <= 0:
            return abs_pos

        if equity <= 0:
            return abs_pos

        target_abs = int(equity / (mark_price * self.maintenance_margin_rate))
        if target_abs < 0:
            target_abs = 0
        if target_abs >= abs_pos:
            return 1

        needed = abs_pos - target_abs
        if needed < 1:
            return 1
        if needed > abs_pos:
            return abs_pos
        return needed

    def build_liquidation_order(self, trader_id: str, position: int) -> OrderRequest:
        if position == 0:
            raise ValueError("cannot liquidate flat position")
        side = Side.SELL if position > 0 else Side.BUY
        return OrderRequest(
            trader_id=trader_id,
            side=side,
            qty=abs(position),
            order_type=OrderType.MARKET,
            price=None,
            client_order_id="liquidation",
        )


@dataclass(slots=True)
class OrderResult:
    accepted: bool
    response: dict[str, Any]
    events: list[dict[str, Any]]


class ExchangeServer:
    def __init__(
        self,
        *,
        host: str,
        order_port: int,
        events_port: int,
        book_depth: int = 10,
        debug_events: bool = False,
        exporter: CSVExporter | None = None,
    ) -> None:
        self._host = host
        self._order_port = order_port
        self._events_port = events_port
        self._book_depth = book_depth
        self._debug_events = debug_events

        self._engine = MatchingEngine()
        self._positions = PositionEngine()
        self._risk = RiskEngine()

        self._next_order_id = 1
        self._last_mark_price = 100.0
        self._liquidation_cooldown_until: dict[str, int] = {}
        self._liquidation_in_progress: set[str] = set()
        self._bankrupt_traders: set[str] = set()
        self._state_lock = asyncio.Lock()
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_subscribers: set[WebSocketServerProtocol] = set()
        self._shutdown = asyncio.Event()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._exporter = exporter or CSVExporter()

    def _next_id(self) -> int:
        order_id = self._next_order_id
        self._next_order_id += 1
        return order_id

    def _mark_price(self, fallback_price: float | None = None) -> float:
        best_bid = self._engine.best_bid
        best_ask = self._engine.best_ask
        if best_bid is not None and best_ask is not None:
            self._last_mark_price = round4((best_bid + best_ask) / 2.0)
            return self._last_mark_price
        if fallback_price is not None:
            self._last_mark_price = round4(fallback_price)
            return self._last_mark_price
        if self._last_mark_price > 0:
            return self._last_mark_price
        if best_bid is not None:
            return best_bid
        if best_ask is not None:
            return best_ask
        return 100.0

    def _build_book_event(self) -> dict[str, Any]:
        snapshot = self._engine.get_book_snapshot(depth=self._book_depth)
        event = BookUpdateEvent(
            best_bid=self._engine.best_bid,
            best_ask=self._engine.best_ask,
            bids=snapshot["bids"],
            asks=snapshot["asks"],
            timestamp=utc_ms(),
        )
        return event.to_message()

    def _build_position_event(self, trader_id: str) -> dict[str, Any]:
        state = self._positions.get(trader_id)
        mark = self._mark_price(state.last_trade_price if state.last_trade_price > 0 else None)
        unrealized = self._positions.unrealized_pnl(trader_id, mark)
        total_equity = round4(state.cash + unrealized)
        return {
            "type": "position_update",
            "trader_id": trader_id,
            "position": state.position,
            "cash": round4(state.cash),
            "avg_entry_price": round4(state.avg_entry_price),
            "realized_pnl": round4(state.realized_pnl),
            "unrealized_pnl": unrealized,
            "total_equity": total_equity,
            "mark_price": mark,
            "timestamp": utc_ms(),
        }

    def _enqueue_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            self._exporter.handle_event(event)
            if self._debug_events:
                LOGGER.debug("emit event: %s", event.get("type"))
            self._event_queue.put_nowait(event)

    def _process_order_locked(self, order: OrderRequest, *, bypass_risk: bool = False) -> OrderResult:
        events: list[dict[str, Any]] = []
        now = utc_ms()

        if order.trader_id in self._bankrupt_traders:
            rejected = OrderRejected(
                reason="account_bankrupt",
                details={"message": "trader is bankrupt and cannot submit orders"},
                trader_id=order.trader_id,
                client_order_id=order.client_order_id,
                timestamp=now,
            )
            return OrderResult(accepted=False, response=rejected.to_message(), events=events)

        cooldown_until = self._liquidation_cooldown_until.get(order.trader_id, 0)
        if now < cooldown_until or order.trader_id in self._liquidation_in_progress:
            rejected = OrderRejected(
                reason="account_frozen",
                details={"message": "trader temporarily frozen during liquidation"},
                trader_id=order.trader_id,
                client_order_id=order.client_order_id,
                timestamp=now,
            )
            return OrderResult(accepted=False, response=rejected.to_message(), events=events)

        mark = self._mark_price()

        if not bypass_risk:
            ok, reason, details = self._risk.validate_initial_margin(order, self._positions, mark)
            if not ok:
                rejected = OrderRejected(
                    reason=reason or "risk_rejected",
                    details=details,
                    trader_id=order.trader_id,
                    client_order_id=order.client_order_id,
                    timestamp=utc_ms(),
                )
                return OrderResult(accepted=False, response=rejected.to_message(), events=events)

        order_id = self._next_id()
        executions, book_changed = self._engine.process(order, order_id=order_id)

        if order.order_type == OrderType.MARKET and not executions:
            rejected = OrderRejected(
                reason="no_liquidity",
                details={"message": "market order could not be filled"},
                trader_id=order.trader_id,
                client_order_id=order.client_order_id,
                timestamp=utc_ms(),
            )
            return OrderResult(accepted=False, response=rejected.to_message(), events=events)

        touched_traders: set[str] = set()
        for execution in executions:
            self._positions.apply_execution(execution)
            self._last_mark_price = round4(execution.price)
            events.append(
                TradeEvent(
                    trade_id=execution.trade_id,
                    price=execution.price,
                    qty=execution.qty,
                    buy_trader_id=execution.buy_trader_id,
                    sell_trader_id=execution.sell_trader_id,
                    timestamp=utc_ms(),
                ).to_message()
            )
            touched_traders.add(execution.buy_trader_id)
            touched_traders.add(execution.sell_trader_id)

        if book_changed or executions:
            events.append(self._build_book_event())
        for trader_id in sorted(touched_traders):
            events.append(self._build_position_event(trader_id))

        breached_traders: list[str] = []
        if executions:
            participants = sorted(
                {trade.buy_trader_id for trade in executions}.union({trade.sell_trader_id for trade in executions})
            )
            for trader_id in participants:
                if trader_id in self._bankrupt_traders:
                    continue
                if trader_id in self._liquidation_in_progress:
                    continue
                cooldown_until = self._liquidation_cooldown_until.get(trader_id, 0)
                if now < cooldown_until:
                    continue
                if self._risk.maintenance_breached(trader_id, self._positions, self._mark_price()):
                    breached_traders.append(trader_id)

        for trader_id in breached_traders:
            liquidation_events = self._run_liquidation_locked(trader_id=trader_id)
            events.extend(liquidation_events)

        accepted = OrderAccepted(
            order_id=order_id,
            trader_id=order.trader_id,
            client_order_id=order.client_order_id,
            timestamp=utc_ms(),
        )
        return OrderResult(accepted=True, response=accepted.to_message(), events=events)

    def _run_liquidation_locked(self, trader_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = utc_ms()
        if trader_id in self._liquidation_in_progress:
            return events

        self._liquidation_in_progress.add(trader_id)
        self._liquidation_cooldown_until[trader_id] = now + LIQUIDATION_COOLDOWN_MS
        try:
            mark = self._mark_price()
            state = self._positions.get(trader_id)
            if state.position == 0:
                return events
            if not self._risk.maintenance_breached(trader_id, self._positions, mark):
                return events

            # Step 1: freeze + cancel resting orders from this trader.
            book_changed = self._engine.cancel_trader_orders(trader_id)
            if book_changed:
                events.append(self._build_book_event())

            required_qty = self._risk.required_liquidation_qty(trader_id, self._positions, mark)
            if required_qty <= 0:
                return events

            liq_state = self._positions.get(trader_id)
            liq_side = Side.SELL if liq_state.position > 0 else Side.BUY
            events.append(
                LiquidationEvent(
                    trader_id=trader_id,
                    reason="maintenance_margin_breach",
                    qty=required_qty,
                    side=liq_side,
                    timestamp=utc_ms(),
                ).to_message()
            )

            liquidation_order = OrderRequest(
                trader_id=trader_id,
                side=liq_side,
                qty=required_qty,
                order_type=OrderType.MARKET,
                price=None,
                client_order_id="liquidation",
            )
            touched_traders: set[str] = set()
            order_id = self._next_id()
            executions, cycle_book_changed = self._engine.process(liquidation_order, order_id=order_id)
            book_changed = book_changed or cycle_book_changed

            for execution in executions:
                self._positions.apply_execution(execution)
                self._last_mark_price = round4(execution.price)
                events.append(
                    TradeEvent(
                        trade_id=execution.trade_id,
                        price=execution.price,
                        qty=execution.qty,
                        buy_trader_id=execution.buy_trader_id,
                        sell_trader_id=execution.sell_trader_id,
                        timestamp=utc_ms(),
                    ).to_message()
                )
                touched_traders.add(execution.buy_trader_id)
                touched_traders.add(execution.sell_trader_id)

            # If still underwater, attempt full flatten once in the same cycle.
            mark = self._mark_price()
            state = self._positions.get(trader_id)
            still_breached = self._risk.maintenance_breached(trader_id, self._positions, mark)
            if still_breached and state.position != 0:
                force_side = Side.SELL if state.position > 0 else Side.BUY
                flatten_qty = abs(state.position)
                events.append(
                    LiquidationEvent(
                        trader_id=trader_id,
                        reason="maintenance_margin_breach_force_flatten",
                        qty=flatten_qty,
                        side=force_side,
                        timestamp=utc_ms(),
                    ).to_message()
                )

                flatten_order = OrderRequest(
                    trader_id=trader_id,
                    side=force_side,
                    qty=flatten_qty,
                    order_type=OrderType.MARKET,
                    price=None,
                    client_order_id="liquidation_flatten",
                )
                order_id = self._next_id()
                exec2, changed2 = self._engine.process(flatten_order, order_id=order_id)
                book_changed = book_changed or changed2
                for execution in exec2:
                    self._positions.apply_execution(execution)
                    self._last_mark_price = round4(execution.price)
                    events.append(
                        TradeEvent(
                            trade_id=execution.trade_id,
                            price=execution.price,
                            qty=execution.qty,
                            buy_trader_id=execution.buy_trader_id,
                            sell_trader_id=execution.sell_trader_id,
                            timestamp=utc_ms(),
                        ).to_message()
                    )
                    touched_traders.add(execution.buy_trader_id)
                    touched_traders.add(execution.sell_trader_id)

                mark = self._mark_price()
                state = self._positions.get(trader_id)
                equity = self._positions.equity(trader_id, mark)
                if state.position == 0 and equity < 0:
                    self._bankrupt_traders.add(trader_id)
                    events.append(
                        LiquidationEvent(
                            trader_id=trader_id,
                            reason="bankruptcy",
                            qty=0,
                            side=Side.SELL,
                            timestamp=utc_ms(),
                        ).to_message()
                    )
                elif state.position != 0 and self._risk.maintenance_breached(trader_id, self._positions, mark):
                    self._bankrupt_traders.add(trader_id)
                    events.append(
                        LiquidationEvent(
                            trader_id=trader_id,
                            reason="bankruptcy",
                            qty=abs(state.position),
                            side=Side.SELL if state.position > 0 else Side.BUY,
                            timestamp=utc_ms(),
                        ).to_message()
                    )

            if book_changed or executions:
                events.append(self._build_book_event())
            for touched in sorted(touched_traders.union({trader_id})):
                events.append(self._build_position_event(touched))
        finally:
            self._liquidation_in_progress.discard(trader_id)
        return events

    async def _submit_order(self, order: OrderRequest) -> OrderResult:
        async with self._state_lock:
            result = self._process_order_locked(order)
        self._enqueue_events(result.events)
        return result

    async def _order_gateway_handler(self, websocket: WebSocketServerProtocol) -> None:
        LOGGER.info("order client connected: %s", websocket.remote_address)
        try:
            async for raw in websocket:
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ProtocolError("payload must be a JSON object")
                    order = OrderRequest.from_message(payload)
                    result = await self._submit_order(order)
                    await websocket.send(json.dumps(result.response))
                except ProtocolError as exc:
                    rejected = OrderRejected(
                        reason="invalid_message",
                        details={"error": str(exc)},
                        trader_id=None,
                        client_order_id=None,
                        timestamp=utc_ms(),
                    )
                    await websocket.send(json.dumps(rejected.to_message()))
                except json.JSONDecodeError:
                    rejected = OrderRejected(
                        reason="invalid_json",
                        details={"error": "message must be valid JSON"},
                        trader_id=None,
                        client_order_id=None,
                        timestamp=utc_ms(),
                    )
                    await websocket.send(json.dumps(rejected.to_message()))
        except ConnectionClosed:
            LOGGER.info("order client disconnected: %s", websocket.remote_address)

    async def _events_handler(self, websocket: WebSocketServerProtocol) -> None:
        LOGGER.info("event subscriber connected: %s", websocket.remote_address)
        self._event_subscribers.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            self._event_subscribers.discard(websocket)
            LOGGER.info("event subscriber disconnected: %s", websocket.remote_address)

    async def _dispatcher_loop(self) -> None:
        while not self._shutdown.is_set():
            event = await self._event_queue.get()
            payload = json.dumps(event)
            subscribers = tuple(self._event_subscribers)
            if not subscribers:
                continue
            send_tasks = [subscriber.send(payload) for subscriber in subscribers]
            results = await asyncio.gather(*send_tasks, return_exceptions=True)
            for subscriber, result in zip(subscribers, results):
                if isinstance(result, Exception):
                    self._event_subscribers.discard(subscriber)

    async def run(self) -> None:
        LOGGER.info("starting exchange order gateway on ws://%s:%s", self._host, self._order_port)
        LOGGER.info("starting exchange event stream on ws://%s:%s", self._host, self._events_port)

        await self._exporter.start()
        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop(), name="exchange-event-dispatcher")
        try:
            async with websockets.serve(self._order_gateway_handler, self._host, self._order_port):
                async with websockets.serve(self._events_handler, self._host, self._events_port):
                    await self._shutdown.wait()
        finally:
            if self._dispatcher_task is not None:
                self._dispatcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._dispatcher_task
            await self._exporter.stop()

    def shutdown(self) -> None:
        self._shutdown.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Distributed exchange server (order gateway + event stream)")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--order-port", type=int, default=9001)
    parser.add_argument("--events-port", type=int, default=9002)
    parser.add_argument("--log-level", type=str, default="INFO")
    parser.add_argument("--debug-events", action="store_true")
    return parser.parse_args()


async def _main_async() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = ExchangeServer(
        host=args.host,
        order_port=args.order_port,
        events_port=args.events_port,
        debug_events=args.debug_events,
    )
    loop = asyncio.get_running_loop()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, server.shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    await server.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        pass
