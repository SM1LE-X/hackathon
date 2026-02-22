# File: bot.py

from __future__ import annotations

import argparse
import asyncio
import json
import random
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from models import Side


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenMarketSim random order bot")
    parser.add_argument("--trader-id", default="bot", help="Local label used in bot logs")
    parser.add_argument("--uri", default="ws://localhost:8000", help="Exchange WebSocket URI")
    parser.add_argument("--interval-ms", type=int, default=500, help="Order send interval")
    parser.add_argument("--seed", type=int, default=42, help="PRNG seed for deterministic bot flow")
    return parser.parse_args()


async def sender_loop(
    websocket: Any,
    local_label: str,
    interval_ms: int,
    rng: random.Random,
) -> None:
    while True:
        side = Side.BUY if rng.random() < 0.5 else Side.SELL
        price = rng.randint(95, 105)
        quantity = rng.randint(1, 5)

        message = {
            "type": "place_order",
            "side": side.value,
            "price": price,
            "quantity": quantity,
        }
        await websocket.send(json.dumps(message))
        print(f"[{local_label}] sent order: {message}")
        await asyncio.sleep(interval_ms / 1000.0)


async def receiver_loop(websocket: Any, local_label: str) -> None:
    async for raw_message in websocket:
        event = json.loads(raw_message)
        event_type = event.get("type")
        if event_type == "trade":
            print(
                f"[{local_label}] trade "
                f"id={event['trade_id']} qty={event['quantity']} px={event['price']} "
                f"maker={event['maker_trader_id']} taker={event['taker_trader_id']} "
                f"aggr={event['aggressor_side']}"
            )
            continue
        if event_type == "welcome":
            print(f"[{local_label}] welcome: assigned {event.get('trader_id')}")
            continue
        if event_type == "error":
            print(f"[{local_label}] error: {event.get('message')}")
            continue
        print(f"[{local_label}] event: {event}")


async def run_bot(args: argparse.Namespace) -> None:
    local_label = args.trader_id
    seed_offset = sum(ord(char) for char in local_label)
    rng = random.Random(args.seed + seed_offset)

    async with websockets.connect(args.uri) as websocket:
        sender_task = asyncio.create_task(
            sender_loop(websocket, local_label, args.interval_ms, rng)
        )
        receiver_task = asyncio.create_task(receiver_loop(websocket, local_label))

        done, pending = await asyncio.wait(
            {sender_task, receiver_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_bot(args))
    except ConnectionClosed:
        print(f"[{args.trader_id}] disconnected")
    except KeyboardInterrupt:
        print(f"[{args.trader_id}] stopped")


if __name__ == "__main__":
    main()
