# File: bot_battle_runner.py

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("bot_battle_runner")


@dataclass(frozen=True, slots=True)
class BotSpec:
    trader_id: str
    strategy: str
    seed: int
    decision_interval: float
    strategy_params: dict[str, str]
    market_data_uri: str
    order_gateway_uri: str
    log_level: str


class BotBattleRunner:
    """
    Developer launcher for strategy-vs-strategy sessions.

    Each bot runs in its own process (no shared memory) and communicates
    with exchange/market-data only through WebSocket endpoints.
    """

    def __init__(self, *, bot_specs: list[BotSpec], python_executable: str | None = None) -> None:
        self._bot_specs = bot_specs
        self._python = python_executable or sys.executable
        self._processes: list[asyncio.subprocess.Process] = []
        self._log_tasks: list[asyncio.Task[None]] = []
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        for spec in self._bot_specs:
            process = await self._spawn_bot(spec)
            self._processes.append(process)
            if process.stdout is not None:
                self._log_tasks.append(asyncio.create_task(self._pipe_logs(process.stdout, spec.trader_id, "OUT")))
            if process.stderr is not None:
                self._log_tasks.append(asyncio.create_task(self._pipe_logs(process.stderr, spec.trader_id, "ERR")))

        LOGGER.info("launched %s bot processes", len(self._processes))

        monitor = asyncio.create_task(self._monitor_processes(), name="bot-process-monitor")
        await self._shutdown.wait()
        monitor.cancel()

        await self._stop_all()

        for task in self._log_tasks:
            task.cancel()
        await asyncio.gather(*self._log_tasks, return_exceptions=True)

    async def _monitor_processes(self) -> None:
        while not self._shutdown.is_set():
            for process in list(self._processes):
                if process.returncode is None:
                    continue
                LOGGER.warning("bot process exited early with code %s; stopping runner", process.returncode)
                self._shutdown.set()
                return
            await asyncio.sleep(0.5)

    async def _spawn_bot(self, spec: BotSpec) -> asyncio.subprocess.Process:
        command: list[str] = [
            self._python,
            "bot_client.py",
            "--trader-id",
            spec.trader_id,
            "--strategy",
            spec.strategy,
            "--seed",
            str(spec.seed),
            "--decision-interval",
            str(spec.decision_interval),
            "--market-data-uri",
            spec.market_data_uri,
            "--order-gateway-uri",
            spec.order_gateway_uri,
            "--log-level",
            spec.log_level,
        ]
        for key, value in sorted(spec.strategy_params.items()):
            command.extend(["--strategy-param", f"{key}={value}"])

        LOGGER.info("starting bot %s with strategy=%s", spec.trader_id, spec.strategy)
        return await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _pipe_logs(self, stream: asyncio.StreamReader, trader_id: str, channel: str) -> None:
        while not stream.at_eof():
            line = await stream.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                print(f"[{trader_id} {channel}] {text}")

    async def _stop_all(self) -> None:
        for process in self._processes:
            if process.returncode is None:
                process.terminate()
        await asyncio.sleep(0.5)
        for process in self._processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(process.wait() for process in self._processes), return_exceptions=True)

    def shutdown(self) -> None:
        self._shutdown.set()


def _parse_bot_spec(raw: dict[str, Any], default_market_data_uri: str, default_order_gateway_uri: str) -> BotSpec:
    if not isinstance(raw, dict):
        raise ValueError("each bot entry must be an object")
    trader_id = raw.get("trader_id")
    if not isinstance(trader_id, str) or not trader_id.strip():
        raise ValueError("bot.trader_id must be a non-empty string")
    strategy = raw.get("strategy", "random")
    if not isinstance(strategy, str) or not strategy.strip():
        raise ValueError("bot.strategy must be a non-empty string")

    params_raw = raw.get("strategy_params", {})
    if not isinstance(params_raw, dict):
        raise ValueError("bot.strategy_params must be an object")
    strategy_params: dict[str, str] = {str(k): str(v) for k, v in params_raw.items()}

    seed = int(raw.get("seed", 7))
    decision_interval = float(raw.get("decision_interval", 0.7))
    market_data_uri = str(raw.get("market_data_uri", default_market_data_uri))
    order_gateway_uri = str(raw.get("order_gateway_uri", default_order_gateway_uri))
    log_level = str(raw.get("log_level", "INFO"))

    return BotSpec(
        trader_id=trader_id.strip(),
        strategy=strategy.strip(),
        seed=seed,
        decision_interval=decision_interval,
        strategy_params=strategy_params,
        market_data_uri=market_data_uri,
        order_gateway_uri=order_gateway_uri,
        log_level=log_level,
    )


def load_config(config_path: Path) -> list[BotSpec]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("config file root must be an object")
    bots_raw = payload.get("bots")
    if not isinstance(bots_raw, list) or not bots_raw:
        raise ValueError("config must contain non-empty 'bots' list")

    default_market_data_uri = str(payload.get("market_data_uri", "ws://127.0.0.1:9010"))
    default_order_gateway_uri = str(payload.get("order_gateway_uri", "ws://127.0.0.1:9001"))
    specs = [_parse_bot_spec(item, default_market_data_uri, default_order_gateway_uri) for item in bots_raw]
    seen: set[str] = set()
    for spec in specs:
        if spec.trader_id in seen:
            raise ValueError(f"duplicate trader_id in config: {spec.trader_id}")
        seen.add(spec.trader_id)
    return specs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch multiple strategy bots for dev testing")
    parser.add_argument(
        "--config",
        type=str,
        default="bots_config.json",
        help="path to bot battle JSON config",
    )
    parser.add_argument("--python", type=str, default=None, help="python executable for spawned bot processes")
    parser.add_argument("--log-level", type=str, default="INFO")
    return parser.parse_args()


async def _main_async() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(args.config).resolve()
    bot_specs = load_config(config_path)
    app = BotBattleRunner(bot_specs=bot_specs, python_executable=args.python)

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
