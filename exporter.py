# File: exporter.py

from __future__ import annotations

import asyncio
import contextlib
import csv
import logging
from collections import deque
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("csv_exporter")


class CSVExporter:
    """
    Buffered CSV exporter for exchange events.

    - `handle_event()` is synchronous and lightweight for hot-path usage.
    - File writes are batched and executed in a background task.
    - Flush cadence defaults to 500ms.
    """

    TRADE_FIELDS: tuple[str, ...] = ("timestamp", "price", "qty", "buy_trader", "sell_trader")
    PERFORMANCE_FIELDS: tuple[str, ...] = (
        "timestamp",
        "trader_id",
        "position",
        "cash",
        "realized_pnl",
        "total_equity",
    )

    def __init__(
        self,
        *,
        trades_path: str | Path = "trades.csv",
        performance_path: str | Path = "performance.csv",
        flush_interval_ms: int = 500,
    ) -> None:
        self._trades_path = Path(trades_path)
        self._performance_path = Path(performance_path)
        self._flush_interval_s = max(0.05, float(flush_interval_ms) / 1000.0)

        self._trade_buffer: deque[tuple[int, float, int, str, str]] = deque()
        self._performance_buffer: deque[tuple[int, str, int, float, float, float]] = deque()

        self._stop_event = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None

    def handle_event(self, event: dict[str, Any]) -> None:
        """
        Route supported events into in-memory buffers.
        This method does not perform I/O.
        """
        event_type = event.get("type")

        if event_type == "trade":
            self._trade_buffer.append(
                (
                    self._to_int(event.get("timestamp")),
                    self._to_float(event.get("price")),
                    self._to_int(event.get("qty")),
                    self._to_text(event.get("buy_trader_id")),
                    self._to_text(event.get("sell_trader_id")),
                )
            )
            return

        if event_type == "position_update":
            self._performance_buffer.append(
                (
                    self._to_int(event.get("timestamp")),
                    self._to_text(event.get("trader_id")),
                    self._to_int(event.get("position")),
                    self._to_float(event.get("cash")),
                    self._to_float(event.get("realized_pnl")),
                    self._to_float(event.get("total_equity")),
                )
            )

    async def start(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._stop_event.clear()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="csv-exporter-flush")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._flush_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        try:
            await self.flush()
        except Exception:
            LOGGER.exception("final CSV flush failed during shutdown")

    async def flush(self) -> None:
        if not self._trade_buffer and not self._performance_buffer:
            return

        trade_rows = [self._trade_buffer.popleft() for _ in range(len(self._trade_buffer))]
        performance_rows = [self._performance_buffer.popleft() for _ in range(len(self._performance_buffer))]
        await asyncio.to_thread(self._write_rows, trade_rows, performance_rows)

    async def _flush_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._flush_interval_s)
            except TimeoutError:
                pass
            try:
                await self.flush()
            except Exception:
                LOGGER.exception("periodic CSV flush failed")
        try:
            await self.flush()
        except Exception:
            LOGGER.exception("final periodic CSV flush failed")

    def _write_rows(
        self,
        trade_rows: list[tuple[int, float, int, str, str]],
        performance_rows: list[tuple[int, str, int, float, float, float]],
    ) -> None:
        if trade_rows:
            self._append_rows(self._trades_path, self.TRADE_FIELDS, trade_rows)
        if performance_rows:
            self._append_rows(self._performance_path, self.PERFORMANCE_FIELDS, performance_rows)

    @staticmethod
    def _append_rows(path: Path, header: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0

        with path.open("a", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            if write_header:
                writer.writerow(header)
            writer.writerows(rows)

    @staticmethod
    def _to_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            rounded = round(float(value), 4)
        except (TypeError, ValueError):
            rounded = 0.0
        if rounded == 0:
            return 0.0
        return rounded
