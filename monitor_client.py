# File: monitor_client.py

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

FEED_URI = "ws://127.0.0.1:9010"
STARTING_CAPITAL = 10_000.0
RECONNECT_DELAY_SECONDS = 1.0
REFRESH_SECONDS = 0.5
MAX_TRADES = 10
BOOK_DEPTH = 5


def round4(value: float) -> float:
    rounded = round(float(value), 4)
    if rounded == 0:
        return 0.0
    return rounded


def supports_color() -> bool:
    return sys.stdout.isatty() and os.getenv("NO_COLOR") is None


class Palette:
    def __init__(self, enabled: bool) -> None:
        if enabled:
            self.reset = "\033[0m"
            self.green = "\033[32m"
            self.red = "\033[31m"
            self.yellow = "\033[33m"
            self.cyan = "\033[36m"
            self.bold = "\033[1m"
        else:
            self.reset = ""
            self.green = ""
            self.red = ""
            self.yellow = ""
            self.cyan = ""
            self.bold = ""

    def colorize(self, text: str, color: str) -> str:
        return f"{color}{text}{self.reset}" if color else text


@dataclass(slots=True)
class TraderState:
    position: float = 0.0
    cash: float = STARTING_CAPITAL
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = STARTING_CAPITAL


@dataclass(slots=True)
class MonitorState:
    order_book: dict[str, list[tuple[float, float]]] = field(
        default_factory=lambda: {"bids": [], "asks": []}
    )
    trades: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_TRADES))
    traders: dict[str, TraderState] = field(default_factory=dict)
    connected: bool = False
    last_error: str = ""
    last_event_ts: int | None = None

    @property
    def best_bid(self) -> float | None:
        bids = self.order_book["bids"]
        return bids[0][0] if bids else None

    @property
    def best_ask(self) -> float | None:
        asks = self.order_book["asks"]
        return asks[0][0] if asks else None

    def mid_price(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round4((self.best_bid + self.best_ask) / 2.0)

    def recalc_trader_metrics(self) -> None:
        mid = self.mid_price()
        if mid is None:
            for row in self.traders.values():
                row.unrealized_pnl = 0.0
                row.total_equity = round4(row.cash)
            return

        for row in self.traders.values():
            unrealized = round4(row.position * mid)
            total_equity = round4(row.cash + unrealized)
            row.unrealized_pnl = unrealized
            row.total_equity = total_equity


class MonitorDashboard:
    def __init__(self, uri: str = FEED_URI) -> None:
        self._uri = uri
        self._state = MonitorState()
        self._palette = Palette(supports_color())
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        receiver_task = asyncio.create_task(self._receiver_loop(), name="receiver")
        render_task = asyncio.create_task(self._render_loop(), name="renderer")
        try:
            await self._shutdown.wait()
        finally:
            receiver_task.cancel()
            render_task.cancel()
            await asyncio.gather(receiver_task, render_task, return_exceptions=True)

    async def _receiver_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._state.connected = False
                async with websockets.connect(self._uri) as websocket:
                    self._state.connected = True
                    self._state.last_error = ""
                    async for raw in websocket:
                        payload = self._safe_json(raw)
                        if payload is None:
                            continue
                        self._apply_event(payload)
            except ConnectionClosed as exc:
                self._state.connected = False
                self._state.last_error = f"connection closed: {exc.code} {exc.reason}"
            except Exception as exc:
                self._state.connected = False
                self._state.last_error = f"connection error: {exc}"

            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _render_loop(self) -> None:
        while not self._shutdown.is_set():
            self._state.recalc_trader_metrics()
            self._render()
            await asyncio.sleep(REFRESH_SECONDS)

    def _safe_json(self, raw: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._state.last_error = "received invalid JSON payload"
            return None
        if not isinstance(payload, dict):
            self._state.last_error = "received non-object JSON payload"
            return None
        return payload

    def _apply_event(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            return

        if event_type == "book_update":
            self._handle_book_update(payload)
            return
        if event_type == "trade":
            self._handle_trade(payload)
            return
        if event_type == "position_update":
            self._handle_position_update(payload)
            return

    def _handle_book_update(self, payload: dict[str, Any]) -> None:
        bids = self._parse_levels(payload.get("bids"))
        asks = self._parse_levels(payload.get("asks"))
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        self._state.order_book["bids"] = bids
        self._state.order_book["asks"] = asks
        ts = payload.get("timestamp")
        if isinstance(ts, int):
            self._state.last_event_ts = ts

    def _handle_trade(self, payload: dict[str, Any]) -> None:
        price = payload.get("price")
        qty = payload.get("qty")
        ts = payload.get("timestamp")
        if not isinstance(price, (int, float)) or not isinstance(qty, (int, float)):
            return
        trade = {
            "price": round4(price),
            "qty": round4(qty),
            "timestamp": ts if isinstance(ts, int) else None,
            "buy_trader_id": payload.get("buy_trader_id"),
            "sell_trader_id": payload.get("sell_trader_id"),
        }
        self._state.trades.appendleft(trade)
        if isinstance(ts, int):
            self._state.last_event_ts = ts

    def _handle_position_update(self, payload: dict[str, Any]) -> None:
        trader_id = payload.get("trader_id")
        if not isinstance(trader_id, str) or not trader_id.strip():
            return
        trader = self._state.traders.get(trader_id)
        if trader is None:
            trader = TraderState()
            self._state.traders[trader_id] = trader

        trader.position = float(payload.get("position", trader.position))
        trader.cash = round4(float(payload.get("cash", trader.cash)))
        trader.realized_pnl = round4(float(payload.get("realized_pnl", trader.realized_pnl)))

        ts = payload.get("timestamp")
        if isinstance(ts, int):
            self._state.last_event_ts = ts

    def _parse_levels(self, raw_levels: Any) -> list[tuple[float, float]]:
        levels: list[tuple[float, float]] = []
        if not isinstance(raw_levels, list):
            return levels
        for level in raw_levels:
            if not isinstance(level, (list, tuple)) or len(level) != 2:
                continue
            px, qty = level
            if not isinstance(px, (int, float)) or not isinstance(qty, (int, float)):
                continue
            qty_val = round4(qty)
            if qty_val <= 0:
                continue
            levels.append((round4(px), qty_val))
        return levels

    def _render(self) -> None:
        print("\033[H\033[J", end="")
        p = self._palette
        status = p.colorize("CONNECTED", p.green) if self._state.connected else p.colorize("DISCONNECTED", p.red)
        mid = self._state.mid_price()

        print("===============================")
        print("OpenMarketSim Monitor")
        print("===============================")
        print(f"Feed: {self._uri}")
        print(f"Status: {status}")
        if self._state.last_error:
            print(f"Last Error: {self._state.last_error}")
        print(f"Best Bid: {self._fmt_px(self._state.best_bid)}   Best Ask: {self._fmt_px(self._state.best_ask)}   Mid: {self._fmt_px(mid)}")
        print("")

        print("ORDER BOOK (top 5)")
        print("-------------------------------")
        print("    BID_QTY      BID_PX |    ASK_PX     ASK_QTY")
        bids = self._state.order_book["bids"][:BOOK_DEPTH]
        asks = self._state.order_book["asks"][:BOOK_DEPTH]
        for i in range(BOOK_DEPTH):
            bid_px, bid_qty = ("", "")
            ask_px, ask_qty = ("", "")
            if i < len(bids):
                bid_px = f"{bids[i][0]:.2f}"
                bid_qty = f"{bids[i][1]:.4f}".rstrip("0").rstrip(".")
            if i < len(asks):
                ask_px = f"{asks[i][0]:.2f}"
                ask_qty = f"{asks[i][1]:.4f}".rstrip("0").rstrip(".")
            print(f"{bid_qty:>11} {bid_px:>11} | {ask_px:>9} {ask_qty:>11}")
        print("")

        print("RECENT TRADES")
        print("-------------------------------")
        print("    TS(ms)        PRICE      QTY")
        for trade in list(self._state.trades)[:MAX_TRADES]:
            ts = trade.get("timestamp")
            ts_txt = str(ts) if isinstance(ts, int) else "-"
            px_txt = f"{float(trade['price']):.2f}"
            qty_txt = f"{float(trade['qty']):.4f}".rstrip("0").rstrip(".")
            print(f"{ts_txt:>12} {px_txt:>12} {qty_txt:>8}")
        if not self._state.trades:
            print("   (no trades yet)")
        print("")

        rows = self._leaderboard_rows()
        print("BOT PERFORMANCE")
        print("Trader | Pos | Cash | Unreal | Total | PnL")
        print("-------------------------------------------")
        for row in rows:
            pnl_color = p.green if row["pnl"] >= 0 else p.red
            pnl_text = p.colorize(f"{row['pnl']:.2f}", pnl_color)
            print(
                f"{row['trader_id']:<10} {row['position']:>6.2f} {row['cash']:>10.2f} "
                f"{row['unrealized']:>10.2f} {row['total_equity']:>10.2f} {pnl_text:>10}"
            )
        if not rows:
            print("(no trader state yet; waiting for position_update events)")
        print("")

        print("LEADERBOARD")
        print("-------------------------------------------")
        for i, row in enumerate(rows, 1):
            print(f"{i:>2}. {row['trader_id']:<12} PnL: {row['pnl']:>10.2f}")
        if not rows:
            print("(leaderboard unavailable)")

        sys.stdout.flush()

    def _leaderboard_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for trader_id, state in self._state.traders.items():
            unrealized = state.unrealized_pnl
            total_equity = state.total_equity
            pnl = round4(total_equity - STARTING_CAPITAL)
            rows.append(
                {
                    "trader_id": trader_id,
                    "position": state.position,
                    "cash": state.cash,
                    "realized_pnl": state.realized_pnl,
                    "unrealized": unrealized,
                    "total_equity": total_equity,
                    "pnl": pnl,
                }
            )
        rows.sort(key=lambda x: (-x["pnl"], x["trader_id"]))
        return rows

    @staticmethod
    def _fmt_px(value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.2f}"


async def main() -> None:
    dashboard = MonitorDashboard(uri=FEED_URI)
    await dashboard.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
