# File: market_data_server.py

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServerProtocol

LOGGER = logging.getLogger("market_data_server")


class MarketDataServer:
    """
    Stateless market-data process.

    - Subscribes to exchange event stream WebSocket.
    - Rebroadcasts each event to all downstream clients.
    - No matching, no risk, no position state.
    """

    def __init__(
        self,
        *,
        upstream_uri: str,
        host: str = "127.0.0.1",
        port: int = 9010,
        reconnect_delay: float = 1.0,
    ) -> None:
        self._upstream_uri = upstream_uri
        self._host = host
        self._port = port
        self._reconnect_delay = max(0.1, reconnect_delay)
        self._clients: set[WebSocketServerProtocol] = set()
        self._shutdown = asyncio.Event()

    async def _client_handler(self, websocket: WebSocketServerProtocol) -> None:
        LOGGER.info("market-data client connected: %s", websocket.remote_address)
        self._clients.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            self._clients.discard(websocket)
            LOGGER.info("market-data client disconnected: %s", websocket.remote_address)

    async def _broadcast(self, message: str) -> None:
        clients = tuple(self._clients)
        if not clients:
            return
        tasks = [client.send(message) for client in clients]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for client, result in zip(clients, results):
            if isinstance(result, Exception):
                self._clients.discard(client)

    async def _upstream_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                LOGGER.info("connecting to exchange stream: %s", self._upstream_uri)
                async with websockets.connect(self._upstream_uri) as upstream:
                    LOGGER.info("connected to exchange stream")
                    async for message in upstream:
                        await self._broadcast(message)
            except ConnectionClosed:
                LOGGER.warning("exchange stream connection closed")
            except Exception as exc:
                LOGGER.warning("exchange stream error: %s", exc)
            if not self._shutdown.is_set():
                await asyncio.sleep(self._reconnect_delay)

    async def run(self) -> None:
        LOGGER.info("starting market-data WS server on ws://%s:%s", self._host, self._port)
        upstream_task = asyncio.create_task(self._upstream_loop(), name="market-data-upstream")
        async with websockets.serve(self._client_handler, self._host, self._port):
            await self._shutdown.wait()
        upstream_task.cancel()
        try:
            await upstream_task
        except asyncio.CancelledError:
            pass

    def shutdown(self) -> None:
        self._shutdown.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Market-data broadcaster process")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9010, help="downstream market-data websocket port")
    parser.add_argument(
        "--upstream-uri",
        type=str,
        default="ws://127.0.0.1:9002",
        help="exchange event stream websocket URI",
    )
    parser.add_argument("--log-level", type=str, default="INFO")
    return parser.parse_args()


async def _main_async() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = MarketDataServer(upstream_uri=args.upstream_uri, host=args.host, port=args.port)

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, app.shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        pass
