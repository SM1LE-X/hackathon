# File: server.py

from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import json
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from engine import MatchingEngine
from margin_risk_manager import MarginRiskManager
from models import SYMBOL, Side, ValidationError, parse_place_order_message
from positions import PositionManager
from session_manager import SessionConfig, SessionManager

class ExchangeServer:
    """Async websocket exchange server orchestrating engine and positions."""

    def __init__(self, debug: bool = False, session_duration_seconds: int = 60) -> None:
        self._engine = MatchingEngine(debug=debug)
        self._positions = PositionManager()
        self._margin_risk = MarginRiskManager()
        self._session = SessionManager(config=SessionConfig(duration_seconds=session_duration_seconds))
        self._connections: set[Any] = set()
        self._connection_traders: dict[Any, str] = {}
        self._trader_connections: dict[str, Any] = {}
        self._next_trader_id = 1
        self._last_mark_price: float | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._accepting_orders = True
        self._stop_after_round = False
        self._session_end_callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
        # Serialize engine/position mutation and outbound sequencing.
        self._state_lock = asyncio.Lock()

    def set_session_end_callback(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
    ) -> None:
        self._session_end_callback = callback

    def request_stop_after_current_round(self) -> None:
        self._stop_after_round = True

    async def begin_shutdown_mode(self) -> None:
        async with self._state_lock:
            self._accepting_orders = False
            self._stop_after_round = True

    async def finalize_current_round_for_interrupt(self) -> dict[str, Any] | None:
        async with self._state_lock:
            return await self._end_session_locked(reset_after=False)

    def is_session_active(self) -> bool:
        return self._session.is_active

    def current_round(self) -> int:
        return self._session.round_id

    async def start(self) -> None:
        if self._session_task is not None:
            return
        self._accepting_orders = True
        self._stop_after_round = False
        loop = asyncio.get_running_loop()
        async with self._state_lock:
            session_start_event = self._session.start_session(loop.time())
            book_event = self._build_book_update_event()
            trader_table_event = self._build_trader_table_event()
        await self._broadcast(session_start_event)
        await self._broadcast(book_event)
        await self._broadcast(trader_table_event)
        self._session_task = asyncio.create_task(
            self._run_session_loop(),
            name="exchange-session-loop",
        )

    async def stop(self) -> None:
        task = self._session_task
        self._session_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run_session_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._session.duration_seconds)

            async with self._state_lock:
                await self._end_session_locked(reset_after=True)
                if self._stop_after_round:
                    return
                session_start_event = self._session.start_session(loop.time())
                book_event = self._build_book_update_event()
                trader_table_event = self._build_trader_table_event()
            await self._broadcast(session_start_event)
            await self._broadcast(book_event)
            await self._broadcast(trader_table_event)

    async def _end_session_locked(self, reset_after: bool = True) -> dict[str, Any] | None:
        if not self._session.is_active:
            return None

        best_bid = self._engine.best_bid()
        best_ask = self._engine.best_ask()
        mark_price = self._compute_session_mark_price(best_bid=best_bid, best_ask=best_ask)
        self._last_mark_price = mark_price

        # Timer expiry: clear resting liquidity and flatten at the session mark.
        self._engine.clear_order_book()

        flattened_snapshots = self._session.force_close_positions(
            positions=self._positions,
            mark_price=mark_price,
        )
        for snapshot in flattened_snapshots:
            await self._send_position_update(str(snapshot["trader_id"]), snapshot)

        leaderboard = self._positions.get_leaderboard()
        session_end_event = self._session.end_session(
            rankings=leaderboard,
            mark_price=mark_price,
        )
        await self._broadcast(session_end_event)

        if self._session_end_callback is not None:
            callback_result = self._session_end_callback(session_end_event)
            if inspect.isawaitable(callback_result):
                await callback_result

        if reset_after:
            self._session.reset_exchange_state(engine=self._engine, positions=self._positions)
            self._last_mark_price = None
            await self._broadcast(self._build_book_update_event())
            await self._broadcast(self._build_trader_table_event())

        return session_end_event

    async def handle_connection(self, websocket: Any) -> None:
        trader_id = self._allocate_trader_id()
        self._connections.add(websocket)
        self._connection_traders[websocket] = trader_id
        self._trader_connections[trader_id] = websocket
        now_monotonic = asyncio.get_running_loop().time()
        remaining = 0.0
        if self._session.is_active:
            remaining = max(0.0, self._session.ends_at_monotonic - now_monotonic)

        await websocket.send(
            json.dumps(
                {
                    "type": "welcome",
                    "trader_id": trader_id,
                    "symbol": SYMBOL,
                    "session_round": self._session.round_id,
                    "session_active": self._session.is_active,
                    "session_duration_seconds": self._session.duration_seconds,
                    "session_remaining_seconds": round(remaining, 4),
                }
            )
        )

        try:
            async for raw_message in websocket:
                await self._handle_raw_message(websocket, raw_message)
        except ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)
            disconnected_trader_id = self._connection_traders.pop(websocket, None)
            if disconnected_trader_id is not None:
                self._trader_connections.pop(disconnected_trader_id, None)

    async def _handle_raw_message(self, websocket: Any, raw_message: str) -> None:
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            await self._send_error(websocket, "invalid JSON payload")
            return

        trader_id = self._connection_traders.get(websocket)
        if trader_id is None:
            await self._send_error(websocket, "unknown connection")
            return

        try:
            side, price, quantity = parse_place_order_message(message)
        except ValidationError as exc:
            await self._send_error(websocket, str(exc))
            return

        async with self._state_lock:
            if not self._accepting_orders:
                rejection = {
                    "type": "order_rejected",
                    "reason": "exchange_shutting_down",
                    "order": {
                        "type": "place_order",
                        "side": side.value,
                        "price": price,
                        "quantity": quantity,
                    },
                }
                await self._send_safe(websocket, json.dumps(rejection))
                return

            now_monotonic = asyncio.get_running_loop().time()
            if not self._session.is_order_window_open(now_monotonic):
                rejection = {
                    "type": "order_rejected",
                    "reason": "session_inactive",
                    "order": {
                        "type": "place_order",
                        "side": side.value,
                        "price": price,
                        "quantity": quantity,
                    },
                }
                await self._send_safe(websocket, json.dumps(rejection))
                return

            order_payload = {
                "type": "place_order",
                "side": side.value,
                "price": price,
                "quantity": quantity,
            }
            risk_snapshot = self._positions.get_position_snapshot_for_risk(trader_id)
            is_valid, rejection_payload = self._margin_risk.validate_initial_margin(
                trader_id=trader_id,
                order=order_payload,
                position_snapshot=risk_snapshot,
            )
            if not is_valid:
                await self._send_safe(websocket, json.dumps(rejection_payload))
                return

            result = self._engine.execute_limit_order(
                trader_id=trader_id,
                side=side,
                price=price,
                quantity=quantity,
                symbol=SYMBOL,
            )

            broadcast_events: list[dict[str, Any]] = []
            position_events: list[tuple[str, dict[str, Any]]] = []
            liquidation_queue: list[str] = []
            queued_liq_traders: set[str] = set()

            self._ingest_trades(
                trades=result.trades,
                broadcast_events=broadcast_events,
                position_events=position_events,
                liquidation_queue=liquidation_queue,
                queued_liq_traders=queued_liq_traders,
            )
            self._process_liquidations(
                broadcast_events=broadcast_events,
                position_events=position_events,
                liquidation_queue=liquidation_queue,
                queued_liq_traders=queued_liq_traders,
            )

            # Snapshot is computed only after full engine processing completes.
            book_event = self._build_book_update_event()

            # Deterministic ordering:
            # receive -> margin validate -> process -> positions -> maintenance checks/liquidations
            # -> snapshot -> broadcasts
            await self._broadcast(book_event)
            for event in broadcast_events:
                await self._broadcast(event)
            for trader, snapshot in position_events:
                await self._send_position_update(trader, snapshot)
            await self._broadcast(self._build_trader_table_event())

    def _build_book_update_event(self, depth: int = 5) -> dict[str, Any]:
        snapshot = self._engine.get_book_snapshot(depth=depth)
        return {
            "type": "book_update",
            "best_bid": self._engine.best_bid(),
            "best_ask": self._engine.best_ask(),
            "bids": snapshot["bids"],
            "asks": snapshot["asks"],
        }

    def _build_trader_table_event(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for snapshot in self._positions.get_all_positions():
            rows.append(
                {
                    "trader_id": snapshot["trader_id"],
                    "position": snapshot["position"],
                    "cash": snapshot["cash"],
                    "realized_pnl": snapshot["realized_pnl"],
                    "unrealized_pnl": snapshot["unrealized_pnl"],
                    "total_pnl": snapshot["total_pnl"],
                }
            )
        return {
            "type": "trader_table",
            "round": self._session.round_id,
            "rows": rows,
        }

    def _ingest_trades(
        self,
        trades: list[Any],
        broadcast_events: list[dict[str, Any]],
        position_events: list[tuple[str, dict[str, Any]]],
        liquidation_queue: list[str],
        queued_liq_traders: set[str],
    ) -> None:
        for trade in trades:
            self._last_mark_price = float(trade.price)
            self._positions.update_from_trade(trade)
            broadcast_events.append(trade.to_event())

            buyer_id, seller_id = self._positions.trade_counterparties(trade)
            buyer_snapshot = self._positions.get_position_snapshot(buyer_id)
            seller_snapshot = self._positions.get_position_snapshot(seller_id)
            position_events.append((buyer_id, buyer_snapshot))
            position_events.append((seller_id, seller_snapshot))

            self._enqueue_maintenance_breaches(
                liquidation_queue=liquidation_queue,
                queued_liq_traders=queued_liq_traders,
            )

    def _enqueue_maintenance_breaches(
        self,
        liquidation_queue: list[str],
        queued_liq_traders: set[str],
    ) -> None:
        current_mark = self._current_mark_price()
        for snapshot in self._positions.get_all_positions():
            trader_id = str(snapshot["trader_id"])
            breach, _ = self._margin_risk.check_maintenance(
                trader_id=trader_id,
                position_snapshot=snapshot,
                mark_price=current_mark,
            )
            if breach and trader_id not in queued_liq_traders:
                queued_liq_traders.add(trader_id)
                liquidation_queue.append(trader_id)

    def _process_liquidations(
        self,
        broadcast_events: list[dict[str, Any]],
        position_events: list[tuple[str, dict[str, Any]]],
        liquidation_queue: list[str],
        queued_liq_traders: set[str],
    ) -> None:
        processed_traders = 0
        max_traders = 1_000

        while liquidation_queue and processed_traders < max_traders:
            processed_traders += 1
            trader_id = liquidation_queue.pop(0)
            # Keep the trader marked during processing so _enqueue_maintenance_breaches()
            # cannot enqueue duplicate entries for the same trader mid-loop.
            queued_liq_traders.add(trader_id)
            try:
                self._liquidate_trader_progressively(
                    trader_id=trader_id,
                    broadcast_events=broadcast_events,
                    position_events=position_events,
                    liquidation_queue=liquidation_queue,
                    queued_liq_traders=queued_liq_traders,
                )
            finally:
                queued_liq_traders.discard(trader_id)

    def _liquidate_trader_progressively(
        self,
        trader_id: str,
        broadcast_events: list[dict[str, Any]],
        position_events: list[tuple[str, dict[str, Any]]],
        liquidation_queue: list[str],
        queued_liq_traders: set[str],
    ) -> None:
        """
        Deterministic maintenance liquidation loop.

        Repeats liquidation chunks for one trader until margin is restored or
        the trader is flat. This is intentionally iterative (no recursion).
        """
        start_snapshot = self._positions.get_position_snapshot_for_risk(trader_id)
        max_iterations = max(1, abs(int(start_snapshot.get("position", 0))) * 2)
        iterations = 0

        while iterations < max_iterations:
            iterations += 1
            trader_snapshot = self._positions.get_position_snapshot_for_risk(trader_id)

            # Failsafe: once flat we stop even if account equity is negative.
            if int(trader_snapshot.get("position", 0)) == 0:
                return

            current_mark = self._current_mark_price()
            breach, _ = self._margin_risk.check_maintenance(
                trader_id=trader_id,
                position_snapshot=trader_snapshot,
                mark_price=current_mark,
            )
            if not breach:
                return

            liquidation_order, liquidation_event = self._margin_risk.perform_liquidation(
                trader_id=trader_id,
                position_snapshot=trader_snapshot,
                best_bid=self._engine.best_bid(),
                best_ask=self._engine.best_ask(),
            )
            broadcast_events.append(liquidation_event)

            if liquidation_order is None:
                # Cannot trade out (no opposing liquidity); stop to avoid a tight loop.
                return

            result = self._engine.execute_limit_order(
                trader_id=trader_id,
                side=Side(liquidation_order["side"]),
                price=int(liquidation_order["price"]),
                quantity=int(liquidation_order["quantity"]),
                symbol=SYMBOL,
            )
            if not result.trades:
                # SMP or empty book prevented execution; no progress possible now.
                return

            self._ingest_trades(
                trades=result.trades,
                broadcast_events=broadcast_events,
                position_events=position_events,
                liquidation_queue=liquidation_queue,
                queued_liq_traders=queued_liq_traders,
            )

    @staticmethod
    def _compute_session_mark_price(best_bid: int | None, best_ask: int | None) -> float:
        if best_bid is not None and best_ask is not None:
            return (float(best_bid) + float(best_ask)) / 2.0
        if best_bid is not None:
            return float(best_bid)
        if best_ask is not None:
            return float(best_ask)
        return 0.0

    def _current_mark_price(self) -> float | None:
        best_bid = self._engine.best_bid()
        best_ask = self._engine.best_ask()
        if best_bid is not None and best_ask is not None:
            return (float(best_bid) + float(best_ask)) / 2.0
        if best_bid is not None:
            return float(best_bid)
        if best_ask is not None:
            return float(best_ask)
        return self._last_mark_price

    async def _broadcast(self, event: dict[str, Any]) -> None:
        if not self._connections:
            return

        payload = json.dumps(event)
        # Sort recipients by assigned trader_id to avoid nondeterministic set iteration.
        snapshot = sorted(
            self._connections,
            key=lambda ws: self._connection_traders.get(ws, ""),
        )
        send_tasks = [self._send_safe(ws, payload) for ws in snapshot]
        alive_flags = await asyncio.gather(*send_tasks)

        for websocket, is_alive in zip(snapshot, alive_flags):
            if not is_alive:
                self._connections.discard(websocket)
                stale_trader_id = self._connection_traders.pop(websocket, None)
                if stale_trader_id is not None:
                    self._trader_connections.pop(stale_trader_id, None)

    async def broadcast_event(self, event: dict[str, Any]) -> None:
        await self._broadcast(event)

    @staticmethod
    async def _send_safe(websocket: Any, payload: str) -> bool:
        try:
            await websocket.send(payload)
            return True
        except ConnectionClosed:
            return False

    @staticmethod
    async def _send_error(websocket: Any, message: str) -> None:
        try:
            await websocket.send(json.dumps({"type": "error", "message": message}))
        except ConnectionClosed:
            return

    async def _send_position_update(self, trader_id: str, snapshot: dict[str, Any]) -> None:
        websocket = self._trader_connections.get(trader_id)
        if websocket is None:
            return

        payload = {
            "type": "position_update",
            "trader_id": trader_id,
            "position": snapshot["position"],
            "cash": snapshot["cash"],
            "realized_pnl": snapshot["realized_pnl"],
            "unrealized_pnl": snapshot["unrealized_pnl"],
            "total_pnl": snapshot["total_pnl"],
        }

        is_alive = await self._send_safe(websocket, json.dumps(payload))
        if not is_alive:
            self._connections.discard(websocket)
            stale_trader_id = self._connection_traders.pop(websocket, None)
            if stale_trader_id is not None:
                self._trader_connections.pop(stale_trader_id, None)

    def _allocate_trader_id(self) -> str:
        trader_id = f"trader_{self._next_trader_id}"
        self._next_trader_id += 1
        return trader_id


async def main(host: str = "localhost", port: int = 8000) -> None:
    server = ExchangeServer()
    async with websockets.serve(server.handle_connection, host, port):
        await server.start()
        try:
            await asyncio.Future()
        finally:
            await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
