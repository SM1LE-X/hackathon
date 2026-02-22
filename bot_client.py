# File: bot_client.py

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import time
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from bot_strategies import StrategyContext, load_strategy, parse_strategy_params
from message_schemas import OrderRequest, round4

LOGGER = logging.getLogger("bot_client")


@dataclass(slots=True)
class LocalBookState:
    best_bid: float | None = None
    best_ask: float | None = None
    last_timestamp: int = 0

    def apply_book_update(self, payload: dict[str, Any]) -> None:
        best_bid = payload.get("best_bid")
        best_ask = payload.get("best_ask")
        if isinstance(best_bid, (int, float)):
            self.best_bid = float(best_bid)
        else:
            self.best_bid = None
        if isinstance(best_ask, (int, float)):
            self.best_ask = float(best_ask)
        else:
            self.best_ask = None
        ts = payload.get("timestamp")
        if isinstance(ts, int):
            self.last_timestamp = ts

    def mid_price(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return round4((self.best_bid + self.best_ask) / 2.0)
        if self.best_bid is not None:
            return round4(self.best_bid)
        if self.best_ask is not None:
            return round4(self.best_ask)
        return 100.0


@dataclass(slots=True)
class LocalTraderState:
    position: int = 0
    cash: float = 10_000.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = 10_000.0
    last_rejection_reason: str | None = None
    last_rejection_ts: int = 0
    last_liquidation_ts: int = 0

    def maintenance_margin(self, mark_price: float, rate: float = 0.10) -> float:
        if mark_price <= 0:
            return 0.0
        return round4(abs(self.position * mark_price) * rate)


class TradingBotClient:
    """
    Bot process:
    - Reads market data from process 2.
    - Maintains local order book state.
    - Sends orders to process 1.
    """

    def __init__(
        self,
        *,
        trader_id: str,
        market_data_uri: str,
        order_gateway_uri: str,
        strategy: str = "random",
        strategy_params: dict[str, str] | None = None,
        decision_interval: float = 0.7,
        seed: int = 7,
    ) -> None:
        self._trader_id = trader_id
        self._market_data_uri = market_data_uri
        self._order_gateway_uri = order_gateway_uri
        self._decision_interval = max(0.1, decision_interval)
        self._rng = random.Random(seed)
        self._book = LocalBookState()
        self._trader = LocalTraderState()
        self._strategy = load_strategy(
            strategy,
            trader_id=trader_id,
            rng=self._rng,
            params=strategy_params or {},
        )
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        while not self._shutdown.is_set():
            try:
                LOGGER.info("connecting market data: %s", self._market_data_uri)
                async with websockets.connect(self._market_data_uri) as md_ws:
                    LOGGER.info("connecting order gateway: %s", self._order_gateway_uri)
                    async with websockets.connect(self._order_gateway_uri) as order_ws:
                        LOGGER.info("bot connected as %s", self._trader_id)
                        tasks = [
                            asyncio.create_task(self._consume_market_data(md_ws), name="bot-market-data"),
                            asyncio.create_task(self._consume_order_responses(order_ws), name="bot-order-responses"),
                            asyncio.create_task(self._order_loop(order_ws), name="bot-order-loop"),
                        ]
                        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                        for task in pending:
                            task.cancel()
                        for task in done:
                            exc = task.exception()
                            if exc is not None:
                                raise exc
            except ConnectionClosed:
                LOGGER.warning("connection closed; reconnecting...")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOGGER.warning("bot runtime error: %s", exc)

            if not self._shutdown.is_set():
                await asyncio.sleep(1.0)

    async def _consume_market_data(self, websocket: websockets.WebSocketClientProtocol) -> None:
        async for raw in websocket:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            msg_type = payload.get("type")
            if msg_type == "book_update":
                self._book.apply_book_update(payload)
            elif msg_type == "trade":
                # Trade prints are intentionally minimal and do not expose exchange internals.
                LOGGER.debug("trade event: price=%s qty=%s", payload.get("price"), payload.get("qty"))
            elif msg_type == "position_update":
                if payload.get("trader_id") == self._trader_id:
                    self._trader.position = int(payload.get("position", self._trader.position))
                    self._trader.cash = float(payload.get("cash", self._trader.cash))
                    self._trader.avg_entry_price = float(payload.get("avg_entry_price", self._trader.avg_entry_price))
                    self._trader.realized_pnl = float(payload.get("realized_pnl", self._trader.realized_pnl))
                    self._trader.unrealized_pnl = float(payload.get("unrealized_pnl", self._trader.unrealized_pnl))
                    self._trader.total_equity = float(payload.get("total_equity", self._trader.total_equity))
            elif msg_type == "liquidation":
                if payload.get("trader_id") == self._trader_id:
                    ts = payload.get("timestamp")
                    self._trader.last_liquidation_ts = int(ts) if isinstance(ts, int) else int(time.time() * 1000)
                LOGGER.info("liquidation event: %s", payload)

    async def _consume_order_responses(self, websocket: websockets.WebSocketClientProtocol) -> None:
        async for raw in websocket:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            msg_type = payload.get("type")
            if msg_type == "order_rejected":
                if payload.get("trader_id") == self._trader_id or payload.get("trader_id") is None:
                    self._trader.last_rejection_reason = str(payload.get("reason", "unknown"))
                    ts = payload.get("timestamp")
                    self._trader.last_rejection_ts = int(ts) if isinstance(ts, int) else int(time.time() * 1000)
                LOGGER.info("order rejected: %s", payload)
            elif msg_type == "order_accepted":
                LOGGER.debug("order accepted: %s", payload)

    def _build_order(self) -> OrderRequest | None:
        spread: float | None = None
        if self._book.best_bid is not None and self._book.best_ask is not None:
            spread = round4(self._book.best_ask - self._book.best_bid)

        context = StrategyContext(
            trader_id=self._trader_id,
            best_bid=self._book.best_bid,
            best_ask=self._book.best_ask,
            mid_price=self._book.mid_price(),
            spread=spread,
            timestamp=self._book.last_timestamp,
            position=self._trader.position,
            cash=round4(self._trader.cash),
            avg_entry_price=round4(self._trader.avg_entry_price),
            realized_pnl=round4(self._trader.realized_pnl),
            unrealized_pnl=round4(self._trader.unrealized_pnl),
            total_equity=round4(self._trader.total_equity),
            maintenance_margin=self._trader.maintenance_margin(self._book.mid_price()),
            last_rejection_reason=self._trader.last_rejection_reason,
            last_rejection_ts=self._trader.last_rejection_ts,
            last_liquidation_ts=self._trader.last_liquidation_ts,
        )
        order = self._strategy.next_order(context)
        return order

    async def _order_loop(self, websocket: websockets.WebSocketClientProtocol) -> None:
        while not self._shutdown.is_set():
            order = self._build_order()
            if order is not None:
                await websocket.send(json.dumps(order.to_message()))
            await asyncio.sleep(self._decision_interval)

    def shutdown(self) -> None:
        self._shutdown.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Distributed bot client")
    parser.add_argument("--trader-id", type=str, required=True)
    parser.add_argument("--market-data-uri", type=str, default="ws://127.0.0.1:9010")
    parser.add_argument("--order-gateway-uri", type=str, default="ws://127.0.0.1:9001")
    parser.add_argument(
        "--strategy",
        type=str,
        default="random",
        help="built-in: random|maker|taker or custom module:Class",
    )
    parser.add_argument(
        "--strategy-param",
        action="append",
        default=[],
        help="strategy parameter in key=value format; repeat for multiple params",
    )
    parser.add_argument("--decision-interval", type=float, default=0.7)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--log-level", type=str, default="INFO")
    return parser.parse_args()


async def _main_async() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = TradingBotClient(
        trader_id=args.trader_id,
        market_data_uri=args.market_data_uri,
        order_gateway_uri=args.order_gateway_uri,
        strategy=args.strategy,
        strategy_params=parse_strategy_params(args.strategy_param),
        decision_interval=args.decision_interval,
        seed=args.seed,
    )

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, bot.shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        pass
