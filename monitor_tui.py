# File: monitor_tui.py

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import websockets
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, RichLog, Static
from websockets.exceptions import ConnectionClosed


FEED_URI = "ws://127.0.0.1:9010"
STARTING_CAPITAL = 10_000.0
DEPTH = 10
MAX_TRADES = 20
MAX_LOGS = 400
MAINT_MARGIN_RATE = 0.10
SEEN_CAP = 3000


def round4(value: float) -> float:
    rounded = round(float(value), 4)
    if rounded == 0:
        return 0.0
    return rounded


def fmt_time(ms: int | None) -> str:
    if ms is None:
        return "--:--:--"
    return datetime.fromtimestamp(ms / 1000.0).strftime("%H:%M:%S")


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: float
    qty: float


@dataclass(frozen=True, slots=True)
class TradeRow:
    trade_id: str
    timestamp: int
    price: float
    qty: float
    side: str


@dataclass(slots=True)
class TraderRow:
    trader_id: str
    position: float = 0.0
    cash: float = STARTING_CAPITAL
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = STARTING_CAPITAL
    net_pnl: float = 0.0

    def update_unrealized(self, mark_price: float | None) -> None:
        if mark_price is None:
            self.unrealized_pnl = 0.0
            self.total_equity = round4(self.cash)
            self.net_pnl = round4(self.total_equity - STARTING_CAPITAL)
            return
        self.unrealized_pnl = round4((mark_price - self.avg_entry_price) * self.position)
        self.total_equity = round4(self.cash + self.unrealized_pnl)
        self.net_pnl = round4(self.total_equity - STARTING_CAPITAL)

    def maintenance_margin(self, mark_price: float | None) -> float:
        if mark_price is None:
            return 0.0
        return round4(abs(self.position * mark_price) * MAINT_MARGIN_RATE)

    def near_liquidation(self, mark_price: float | None) -> bool:
        mm = self.maintenance_margin(mark_price)
        if mm <= 0:
            return False
        return self.total_equity <= round4(mm * 1.2)


@dataclass(slots=True)
class MarketStateCache:
    endpoint: str = FEED_URI
    connected: bool = False
    status_text: str = "DISCONNECTED"
    status_error: str = ""
    last_update_ms: int | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    trades: deque[TradeRow] = field(default_factory=lambda: deque(maxlen=MAX_TRADES))
    traders: dict[str, TraderRow] = field(default_factory=dict)
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOGS))
    revision: int = 0

    _seen_trade_ids: set[str] = field(default_factory=set, init=False)
    _seen_liq_keys: set[str] = field(default_factory=set, init=False)
    _seen_order: deque[tuple[str, str]] = field(default_factory=deque, init=False)

    def set_connected(self, connected: bool, *, message: str = "", error: str = "") -> None:
        next_status = "CONNECTED" if connected else "DISCONNECTED"
        changed = (
            self.connected != connected
            or self.status_text != next_status
            or self.status_error != error
            or (message != "")
        )
        self.connected = connected
        self.status_text = next_status
        self.status_error = error
        if message:
            self.logs.append(message)
        if changed:
            self.revision += 1

    def _remember_seen(self, kind: str, key: str) -> bool:
        bucket = self._seen_trade_ids if kind == "trade" else self._seen_liq_keys
        if key in bucket:
            return False
        bucket.add(key)
        self._seen_order.append((kind, key))
        while len(self._seen_order) > SEEN_CAP:
            old_kind, old_key = self._seen_order.popleft()
            old_bucket = self._seen_trade_ids if old_kind == "trade" else self._seen_liq_keys
            old_bucket.discard(old_key)
        return True

    @property
    def mark_price(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round4((self.best_bid + self.best_ask) / 2.0)

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round4(max(0.0, self.best_ask - self.best_bid))

    def _reprice_traders(self) -> None:
        mark = self.mark_price
        for trader in self.traders.values():
            trader.update_unrealized(mark)

    def apply_event(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            return

        if event_type == "book_update":
            self._apply_book(payload)
            return
        if event_type == "trade":
            self._apply_trade(payload)
            return
        if event_type == "position_update":
            self._apply_position(payload)
            return
        if event_type == "liquidation":
            self._apply_liquidation(payload)
            return

    def _apply_book(self, payload: dict[str, Any]) -> None:
        bids = self._parse_levels(payload.get("bids"), reverse=True)
        asks = self._parse_levels(payload.get("asks"), reverse=False)
        ts = payload.get("timestamp")
        if isinstance(ts, int):
            self.last_update_ms = ts

        changed = bids != self.bids or asks != self.asks
        self.bids = bids
        self.asks = asks
        self.best_bid = bids[0].price if bids else None
        self.best_ask = asks[0].price if asks else None
        if changed:
            self._reprice_traders()
            self.revision += 1

    def _apply_trade(self, payload: dict[str, Any]) -> None:
        price = payload.get("price")
        qty = payload.get("qty")
        ts = payload.get("timestamp")
        if not isinstance(price, (int, float)) or not isinstance(qty, (int, float)):
            return

        trade_id = str(payload.get("trade_id") or f"{int(ts) if isinstance(ts, int) else 0}-{price}-{qty}")
        if not self._remember_seen("trade", trade_id):
            return

        timestamp = ts if isinstance(ts, int) else int(datetime.now().timestamp() * 1000)
        self.last_update_ms = timestamp

        side = "unknown"
        side_raw = payload.get("side")
        if isinstance(side_raw, str):
            side = side_raw.lower()
        elif self.mark_price is not None:
            side = "buy" if float(price) >= float(self.mark_price) else "sell"

        self.trades.append(
            TradeRow(
                trade_id=trade_id,
                timestamp=timestamp,
                price=round4(float(price)),
                qty=round4(float(qty)),
                side=side,
            )
        )
        self.revision += 1

    def _apply_position(self, payload: dict[str, Any]) -> None:
        trader_id = payload.get("trader_id")
        if not isinstance(trader_id, str) or not trader_id.strip():
            return
        row = self.traders.get(trader_id)
        if row is None:
            row = TraderRow(trader_id=trader_id)
            self.traders[trader_id] = row

        row.position = float(payload.get("position", row.position))
        row.cash = round4(float(payload.get("cash", row.cash)))
        row.avg_entry_price = round4(float(payload.get("avg_entry_price", row.avg_entry_price)))
        row.realized_pnl = round4(float(payload.get("realized_pnl", row.realized_pnl)))

        unrealized = payload.get("unrealized_pnl")
        total_equity = payload.get("total_equity")
        if isinstance(unrealized, (int, float)):
            row.unrealized_pnl = round4(float(unrealized))
        else:
            row.update_unrealized(self.mark_price)

        if isinstance(total_equity, (int, float)):
            row.total_equity = round4(float(total_equity))
            row.net_pnl = round4(row.total_equity - STARTING_CAPITAL)
        else:
            row.update_unrealized(self.mark_price)

        ts = payload.get("timestamp")
        if isinstance(ts, int):
            self.last_update_ms = ts
        self.revision += 1

    def _apply_liquidation(self, payload: dict[str, Any]) -> None:
        trader_id = payload.get("trader_id")
        reason = payload.get("reason")
        qty = payload.get("qty")
        side = payload.get("side")
        ts = payload.get("timestamp")
        if not isinstance(trader_id, str):
            trader_id = "unknown"
        if not isinstance(reason, str):
            reason = "unspecified"
        key = f"{int(ts) if isinstance(ts, int) else 0}:{trader_id}:{reason}:{qty}:{side}"
        if not self._remember_seen("liq", key):
            return

        stamp = ts if isinstance(ts, int) else int(datetime.now().timestamp() * 1000)
        self.last_update_ms = stamp
        self.logs.append(
            f"{fmt_time(stamp)} liquidation trader={trader_id} side={side} qty={qty} reason={reason}"
        )
        self.revision += 1

    @staticmethod
    def _parse_levels(raw: Any, *, reverse: bool) -> list[PriceLevel]:
        levels: list[PriceLevel] = []
        if not isinstance(raw, list):
            return levels
        for entry in raw:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            px, qty = entry
            if not isinstance(px, (int, float)) or not isinstance(qty, (int, float)):
                continue
            q = round4(float(qty))
            p = round4(float(px))
            if q <= 0 or p <= 0:
                continue
            levels.append(PriceLevel(price=p, qty=q))
        levels.sort(key=lambda x: x.price, reverse=reverse)
        return levels

    def orderbook_depth(self, depth: int = DEPTH) -> tuple[list[PriceLevel | None], list[PriceLevel | None]]:
        bids: list[PriceLevel | None] = [*self.bids[:depth]]
        asks: list[PriceLevel | None] = [*self.asks[:depth]]
        while len(bids) < depth:
            bids.append(None)
        while len(asks) < depth:
            asks.append(None)
        return bids, asks

    def trader_rows(self) -> list[TraderRow]:
        return sorted(self.traders.values(), key=lambda r: (-r.net_pnl, r.trader_id))


class TopBar(Static):
    def update_from_state(self, state: MarketStateCache) -> None:
        status_style = "bold green" if state.connected else "bold red"
        status = Text(state.status_text, style=status_style)
        mark = state.mark_price
        spread = state.spread
        right = Text()
        right.append("Mid ", style="bold #8fa4b8")
        right.append(f"{mark:.4f}" if mark is not None else "-", style="bold #d8dde6")
        right.append("   Spread ", style="bold #8fa4b8")
        right.append(f"{spread:.4f}" if spread is not None else "-", style="bold #d8dde6")
        right.append("   Last ", style="bold #8fa4b8")
        right.append(fmt_time(state.last_update_ms), style="bold #d8dde6")

        content = Text("OpenMarketSim TUI  ", style="bold #4fb0ff")
        content.append("Status: ", style="bold #8fa4b8")
        content.append_text(status)
        if state.status_error:
            content.append("  ")
            content.append(state.status_error[:120], style="italic #ffb4c0")
        content.append("\n")
        content.append_text(right)
        self.update(content)


class OrderBookWidget(Static):
    def update_from_state(self, state: MarketStateCache) -> None:
        bids, asks = state.orderbook_depth(DEPTH)
        max_qty = 1.0
        for row in bids + asks:
            if row is not None:
                max_qty = max(max_qty, row.qty)

        table = Table(expand=True, box=None, pad_edge=False)
        table.add_column("Bid Qty", justify="right", style="green")
        table.add_column("Bid Px", justify="right", style="bold green")
        table.add_column(" ", justify="left")
        table.add_column("Ask Px", justify="right", style="bold red")
        table.add_column("Ask Qty", justify="right", style="red")
        table.add_column(" ", justify="left")

        for i in range(DEPTH):
            bid = bids[i]
            ask = asks[i]

            if bid is None:
                bid_qty = "-"
                bid_px = "-"
                bid_bar = ""
            else:
                bid_qty = f"{bid.qty:.2f}"
                bid_px = f"{bid.price:.4f}"
                bid_bar_len = max(1, int((bid.qty / max_qty) * 16))
                bid_bar = Text("█" * bid_bar_len, style="#2ad38b")

            if ask is None:
                ask_px = "-"
                ask_qty = "-"
                ask_bar = ""
            else:
                ask_px = f"{ask.price:.4f}"
                ask_qty = f"{ask.qty:.2f}"
                ask_bar_len = max(1, int((ask.qty / max_qty) * 16))
                ask_bar = Text("█" * ask_bar_len, style="#ff5a72")

            table.add_row(
                bid_qty,
                bid_px,
                bid_bar,
                ask_px,
                ask_qty,
                ask_bar,
            )

        self.update(table)


class TradesWidget(DataTable):
    auto_follow: bool = True

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("Time", "Price", "Qty", "Side")

    def on_mouse_scroll_up(self, _event) -> None:
        self.auto_follow = False

    def on_mouse_scroll_down(self, _event) -> None:
        self.auto_follow = False

    def on_key(self, event) -> None:  # type: ignore[override]
        if event.key in {"up", "down", "pageup", "pagedown", "home"}:
            self.auto_follow = False

    def follow_latest(self) -> None:
        self.auto_follow = True
        self.call_after_refresh(self._scroll_to_end)

    def _scroll_to_end(self) -> None:
        with contextlib.suppress(Exception):
            self.scroll_end(animate=False)

    def update_from_state(self, state: MarketStateCache) -> None:
        self.clear()
        for trade in state.trades:
            side_style = "green" if trade.side == "buy" else "red" if trade.side == "sell" else "yellow"
            self.add_row(
                fmt_time(trade.timestamp),
                Text(f"{trade.price:.4f}", style=side_style),
                f"{trade.qty:.2f}",
                Text(trade.side.upper(), style=side_style),
                key=trade.trade_id,
            )
        if self.auto_follow:
            self.call_after_refresh(self._scroll_to_end)


class PerformanceWidget(DataTable):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("Trader", "Pos", "Cash", "Realized", "Unrealized", "Net PnL")

    def update_from_state(self, state: MarketStateCache) -> None:
        mark = state.mark_price
        self.clear()
        for row in state.trader_rows():
            near_liq = row.near_liquidation(mark)
            trader_style = "bold yellow" if near_liq else "bold #d8dde6"
            pnl_style = "green" if row.net_pnl >= 0 else "red"
            unreal_style = "green" if row.unrealized_pnl >= 0 else "red"
            realized_style = "green" if row.realized_pnl >= 0 else "red"
            trader_txt = Text(row.trader_id, style=trader_style)
            if near_liq:
                trader_txt.append(" !", style="bold red")
            self.add_row(
                trader_txt,
                f"{row.position:.2f}",
                f"{row.cash:,.2f}",
                Text(f"{row.realized_pnl:+,.2f}", style=realized_style),
                Text(f"{row.unrealized_pnl:+,.2f}", style=unreal_style),
                Text(f"{row.net_pnl:+,.2f}", style=pnl_style),
                key=row.trader_id,
            )


class OpenMarketSimTUI(App):
    CSS = """
    Screen {
        layout: vertical;
        background: #0a1014;
        color: #e8f3fa;
    }

    #topbar {
        height: 3;
        border: round #244459;
        padding: 0 1;
        margin: 0 1;
        background: #101a22;
    }

    #middle {
        height: 1fr;
        layout: horizontal;
        margin: 0 1;
    }

    #orderbook_panel, #trades_panel {
        width: 1fr;
        border: round #244459;
        background: #101a22;
        margin-right: 1;
        padding: 0 1;
    }

    #trades_panel {
        margin-right: 0;
    }

    #logs_panel {
        width: 38;
        border: round #244459;
        background: #0f1820;
        padding: 0 1;
        margin-left: 1;
    }

    #bottom_panel {
        height: 15;
        border: round #244459;
        background: #101a22;
        margin: 0 1;
        padding: 0 1;
    }

    .panel-title {
        height: 1;
        color: #88a2b6;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("l", "toggle_logs", "Toggle Logs"),
        Binding("f", "follow_trades", "Follow Trades"),
        Binding("r", "reconnect", "Reconnect"),
    ]

    def __init__(self, *, endpoint: str, refresh_hz: float = 10.0) -> None:
        super().__init__()
        self._state = MarketStateCache(endpoint=endpoint)
        self._refresh_seconds = max(0.08, 1.0 / max(1.0, refresh_hz))
        self._last_render_revision = -1
        self._last_rendered_log_count = 0
        self._show_logs = False
        self._shutdown = asyncio.Event()
        self._force_reconnect = asyncio.Event()

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="middle"):
            with Vertical(id="orderbook_panel"):
                yield Static("ORDER BOOK (Depth 10)", classes="panel-title")
                yield OrderBookWidget(id="orderbook")
            with Vertical(id="trades_panel"):
                yield Static("RECENT TRADES (Last 20)", classes="panel-title")
                yield TradesWidget(id="trades")
            with Vertical(id="logs_panel"):
                yield Static("STRATEGY / SYSTEM LOGS", classes="panel-title")
                yield RichLog(id="logs", wrap=False, highlight=False, markup=False)
        with Vertical(id="bottom_panel"):
            yield Static("BOT PERFORMANCE", classes="panel-title")
            yield PerformanceWidget(id="performance")

    async def on_mount(self) -> None:
        self.query_one("#logs_panel", Vertical).styles.display = "none"
        self.set_interval(self._refresh_seconds, self._refresh_ui)
        self.run_worker(self._ws_loop(), exclusive=True)
        self._append_log("TUI started.")

    async def on_unmount(self) -> None:
        self._shutdown.set()

    def action_toggle_logs(self) -> None:
        self._show_logs = not self._show_logs
        panel = self.query_one("#logs_panel", Vertical)
        panel.styles.display = "block" if self._show_logs else "none"
        self._append_log(f"logs panel {'enabled' if self._show_logs else 'hidden'}")

    def action_follow_trades(self) -> None:
        trades = self.query_one("#trades", TradesWidget)
        trades.follow_latest()
        self._append_log("trades auto-follow enabled")

    def action_reconnect(self) -> None:
        self._force_reconnect.set()
        self._append_log("manual reconnect requested")

    def _append_log(self, line: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._state.logs.append(f"{timestamp} {line}")
        self._state.revision += 1

    def _refresh_ui(self) -> None:
        if self._state.revision == self._last_render_revision:
            return
        self._last_render_revision = self._state.revision

        self.query_one("#topbar", TopBar).update_from_state(self._state)
        self.query_one("#orderbook", OrderBookWidget).update_from_state(self._state)
        self.query_one("#trades", TradesWidget).update_from_state(self._state)
        self.query_one("#performance", PerformanceWidget).update_from_state(self._state)

        log_widget = self.query_one("#logs", RichLog)
        logs = list(self._state.logs)
        if self._last_rendered_log_count > len(logs):
            log_widget.clear()
            self._last_rendered_log_count = 0
        for line in logs[self._last_rendered_log_count :]:
            log_widget.write(line)
        self._last_rendered_log_count = len(logs)

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while not self._shutdown.is_set():
            self._state.set_connected(False, message=f"connecting to {self._state.endpoint} ...")
            try:
                async with websockets.connect(
                    self._state.endpoint,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                ) as ws:
                    self._state.set_connected(True, message=f"connected to {self._state.endpoint}")
                    backoff = 1.0
                    while not self._shutdown.is_set():
                        if self._force_reconnect.is_set():
                            self._force_reconnect.clear()
                            await ws.close()
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        except ConnectionClosed:
                            break

                        payload: Any
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            self._append_log("invalid json payload dropped")
                            continue
                        if not isinstance(payload, dict):
                            continue
                        self._state.apply_event(payload)
            except ConnectionClosed as exc:
                self._state.set_connected(False, error=f"closed {exc.code} {exc.reason}", message="feed disconnected")
            except Exception as exc:
                self._state.set_connected(False, error=str(exc), message=f"connection error: {exc}")

            if self._shutdown.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.6, 6.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenMarketSim production TUI client")
    parser.add_argument("--uri", type=str, default=FEED_URI, help="market data websocket URI")
    parser.add_argument("--refresh-hz", type=float, default=10.0, help="ui refresh rate")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = OpenMarketSimTUI(endpoint=args.uri, refresh_hz=args.refresh_hz)
    app.run()


if __name__ == "__main__":
    main()
