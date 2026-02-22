# File: arena_cli.py

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any


ANSI_HOME = "\033[H"
ANSI_CLEAR_TO_END = "\033[J"
ANSI_HIDE_CURSOR = "\033[?25l"
ANSI_SHOW_CURSOR = "\033[?25h"


def _round4(value: float) -> float:
    rounded = round(value, 4)
    if rounded == 0:
        return 0.0
    return rounded


@dataclass(slots=True)
class ArenaState:
    trader_id: str = "-"
    round_id: int = 0
    session_active: bool = False
    session_duration: float = 60.0
    session_started_monotonic: float | None = None
    best_bid: int | None = None
    best_ask: int | None = None
    bids: list[tuple[int, int]] = field(default_factory=list)
    asks: list[tuple[int, int]] = field(default_factory=list)
    trader_rows: list[dict[str, Any]] = field(default_factory=list)
    round_history: list[dict[str, Any]] = field(default_factory=list)
    tournament_score: dict[str, float] = field(default_factory=dict)
    last_rankings: list[dict[str, Any]] = field(default_factory=list)
    last_session_end_round: int = 0
    last_session_end_mark: float = 0.0
    max_depth: int = 8
    max_trader_rows: int = 14
    max_leaderboard_rows: int = 10
    lifecycle_state: str = "RUNNING"
    lifecycle_started_monotonic: float = 0.0
    next_round_seen: bool = False
    tournament_rounds_completed: int = 0
    tournament_total_rounds: int = 0
    tournament_rankings: list[dict[str, Any]] = field(default_factory=list)
    connection_closed: bool = False

    def apply_event(self, event: dict[str, Any], now_monotonic: float) -> None:
        event_type = event.get("type")
        if event_type == "welcome":
            self.trader_id = str(event.get("trader_id", "-"))
            self.round_id = int(event.get("session_round", self.round_id))
            self.session_active = bool(event.get("session_active", self.session_active))
            self.session_duration = float(
                event.get("session_duration_seconds", self.session_duration)
            )
            remaining = float(event.get("session_remaining_seconds", 0.0))
            if self.session_active:
                elapsed = max(0.0, self.session_duration - remaining)
                self.session_started_monotonic = now_monotonic - elapsed
            return

        if event_type == "session_start":
            self.round_id = int(event.get("round", self.round_id))
            self.session_duration = float(event.get("duration_seconds", self.session_duration))
            self.session_active = True
            self.session_started_monotonic = now_monotonic
            self.connection_closed = False
            if self.lifecycle_state == "ROUND_COMPLETE":
                self.next_round_seen = True
            elif self.lifecycle_state != "TOURNAMENT_COMPLETE":
                self.lifecycle_state = "RUNNING"
                self.lifecycle_started_monotonic = now_monotonic
            return

        if event_type == "session_end":
            self.session_active = False
            rankings = event.get("rankings", [])
            round_id = int(event.get("round", self.round_id))
            mark_price = float(event.get("mark_price", 0.0))
            normalized_rankings = self._normalize_rankings(rankings)

            if not self._has_round_summary(round_id):
                round_summary = {
                    "round": round_id,
                    "mark_price": _round4(mark_price),
                    "rankings": normalized_rankings,
                }
                # Preserve complete history in arrival order (deterministic by round id).
                self.round_history.append(round_summary)
                self.round_history.sort(key=lambda row: int(row["round"]))

                for row in normalized_rankings:
                    trader_id = str(row["trader_id"])
                    pnl = _round4(float(row["pnl"]))
                    self.tournament_score[trader_id] = _round4(
                        self.tournament_score.get(trader_id, 0.0) + pnl
                    )

            self.last_session_end_round = round_id
            self.last_session_end_mark = mark_price
            self.last_rankings = normalized_rankings
            self.tournament_rankings = self._build_cumulative_rankings()

            if self.lifecycle_state != "TOURNAMENT_COMPLETE":
                self.lifecycle_state = "ROUND_COMPLETE"
                self.lifecycle_started_monotonic = now_monotonic
                self.next_round_seen = False
            return

        if event_type == "book_update":
            self.best_bid = event.get("best_bid")
            self.best_ask = event.get("best_ask")
            self.bids = [tuple(level) for level in event.get("bids", [])]
            self.asks = [tuple(level) for level in event.get("asks", [])]
            return

        if event_type == "trader_table":
            self.round_id = int(event.get("round", self.round_id))
            rows = event.get("rows", [])
            self.trader_rows = rows if isinstance(rows, list) else []
            return

        if event_type == "tournament_complete":
            self.lifecycle_state = "TOURNAMENT_COMPLETE"
            self.lifecycle_started_monotonic = now_monotonic
            self.tournament_rounds_completed = int(event.get("rounds_completed", 0))
            self.tournament_total_rounds = int(event.get("total_rounds", 0))
            rankings = event.get("rankings", [])
            normalized_rankings = self._normalize_rankings(rankings)
            for row in normalized_rankings:
                trader_id = str(row["trader_id"])
                if trader_id not in self.tournament_score:
                    self.tournament_score[trader_id] = _round4(float(row["pnl"]))
            self.tournament_rankings = self._build_cumulative_rankings()
            self.session_active = False
            return

    @staticmethod
    def _normalize_rankings(rankings: Any) -> list[dict[str, Any]]:
        if not isinstance(rankings, list):
            return []
        normalized: list[dict[str, Any]] = []
        for row in rankings:
            if not isinstance(row, dict):
                continue
            normalized.append(
                {
                    "rank": int(row.get("rank", 0)),
                    "trader_id": str(row.get("trader_id", "-")),
                    "pnl": _round4(float(row.get("pnl", 0.0))),
                }
            )
        normalized.sort(key=lambda row: (int(row["rank"]), str(row["trader_id"])))
        return normalized

    def _has_round_summary(self, round_id: int) -> bool:
        for summary in self.round_history:
            if int(summary.get("round", 0)) == round_id:
                return True
        return False

    def _build_cumulative_rankings(self) -> list[dict[str, Any]]:
        ranked = sorted(
            self.tournament_score.items(),
            key=lambda item: (-item[1], item[0]),
        )
        rows: list[dict[str, Any]] = []
        for idx, (trader_id, pnl) in enumerate(ranked, start=1):
            rows.append(
                {
                    "rank": idx,
                    "trader_id": trader_id,
                    "pnl": _round4(pnl),
                }
            )
        return rows

    def on_connection_closed(self) -> None:
        self.connection_closed = True

    def advance_lifecycle(self, now_monotonic: float) -> None:
        if self.lifecycle_state == "ROUND_COMPLETE":
            if (now_monotonic - self.lifecycle_started_monotonic) >= 3.0 and self.next_round_seen:
                self.lifecycle_state = "RUNNING"
                self.lifecycle_started_monotonic = now_monotonic

    def countdown_seconds(self, now_monotonic: float) -> float:
        if not self.session_active or self.session_started_monotonic is None:
            return 0.0
        elapsed = now_monotonic - self.session_started_monotonic
        return max(0.0, self.session_duration - elapsed)

    def _render_running(self, now_monotonic: float, width: int, sep: str) -> list[str]:
        status = "ACTIVE" if self.session_active else "ENDED"
        countdown = self.countdown_seconds(now_monotonic)
        lines: list[str] = [
            f"OpenMarketSim Arena | Round {self.round_id} | Status {status} | Countdown {countdown:05.1f}s",
            f"Connected as: {self.trader_id}",
            sep,
            "Order Book",
            "  BID(px,qty)            | ASK(px,qty)",
        ]

        for idx in range(self.max_depth):
            bid_text = ""
            ask_text = ""
            if idx < len(self.bids):
                bid_px, bid_qty = self.bids[idx]
                bid_text = f"{bid_px:>6},{bid_qty:<6}"
            if idx < len(self.asks):
                ask_px, ask_qty = self.asks[idx]
                ask_text = f"{ask_px:>6},{ask_qty:<6}"
            lines.append(f"  {bid_text:<22}| {ask_text:<22}")

        best_bid_text = "-" if self.best_bid is None else str(self.best_bid)
        best_ask_text = "-" if self.best_ask is None else str(self.best_ask)
        lines.extend(
            [
                f"  Best Bid: {best_bid_text} | Best Ask: {best_ask_text}",
                sep,
                "Trader Table",
                "  trader_id      pos        cash      realized    unrealized      total",
            ]
        )

        sorted_rows = sorted(
            self.trader_rows,
            key=lambda row: str(row.get("trader_id", "")),
        )
        for idx in range(self.max_trader_rows):
            if idx >= len(sorted_rows):
                lines.append("  ")
                continue
            row = sorted_rows[idx]
            trader_id = str(row.get("trader_id", "-"))
            position = int(row.get("position", 0))
            cash = _round4(float(row.get("cash", 0.0)))
            realized = _round4(float(row.get("realized_pnl", 0.0)))
            unrealized = _round4(float(row.get("unrealized_pnl", 0.0)))
            total = _round4(float(row.get("total_pnl", 0.0)))
            lines.append(
                "  "
                f"{trader_id:<12} "
                f"{position:>6} "
                f"{cash:>11.4f} "
                f"{realized:>11.4f} "
                f"{unrealized:>11.4f} "
                f"{total:>11.4f}"
            )

        lines.extend([sep, "Last Round Result"])
        if not self.last_rankings:
            lines.append("  (waiting for session end)")
            for _ in range(self.max_leaderboard_rows):
                lines.append("  ")
        else:
            lines.append(
                f"  round={self.last_session_end_round} mark={_round4(self.last_session_end_mark):.4f}"
            )
            lines.append("  rank   trader_id          pnl")
            sorted_rankings = sorted(
                self.last_rankings,
                key=lambda row: (
                    int(row.get("rank", 0)),
                    str(row.get("trader_id", "")),
                ),
            )
            for idx in range(self.max_leaderboard_rows):
                if idx >= len(sorted_rankings):
                    lines.append("  ")
                    continue
                row = sorted_rankings[idx]
                rank = int(row.get("rank", 0))
                trader_id = str(row.get("trader_id", "-"))
                pnl = _round4(float(row.get("pnl", 0.0)))
                lines.append(f"  {rank:>4}   {trader_id:<14} {pnl:>11.4f}")

        lines.extend([sep, "Tournament Cumulative Leaderboard", "  rank   trader_id          pnl"])
        cumulative = self._build_cumulative_rankings()
        for idx in range(self.max_leaderboard_rows):
            if idx >= len(cumulative):
                lines.append("  ")
                continue
            row = cumulative[idx]
            lines.append(f"  {row['rank']:>4}   {row['trader_id']:<14} {row['pnl']:>11.4f}")

        lines.append(sep)
        lines.append(f"Updated: {time.strftime('%H:%M:%S')}")
        return lines

    def _render_round_complete(self, now_monotonic: float, sep: str) -> list[str]:
        elapsed = max(0.0, now_monotonic - self.lifecycle_started_monotonic)
        remaining = max(0.0, 3.0 - elapsed)
        lines = [
            f"OpenMarketSim Arena | Round {self.last_session_end_round} Complete",
            sep,
            f"Mark Price: {_round4(self.last_session_end_mark):.4f}",
            f"Next round in: {remaining:04.1f}s",
            sep,
            "Round Leaderboard",
            "  rank   trader_id          pnl",
        ]
        sorted_rankings = sorted(
            self.last_rankings,
            key=lambda row: (
                int(row.get("rank", 0)),
                str(row.get("trader_id", "")),
            ),
        )
        for idx in range(self.max_leaderboard_rows):
            if idx >= len(sorted_rankings):
                lines.append("  ")
                continue
            row = sorted_rankings[idx]
            rank = int(row.get("rank", 0))
            trader_id = str(row.get("trader_id", "-"))
            pnl = _round4(float(row.get("pnl", 0.0)))
            lines.append(f"  {rank:>4}   {trader_id:<14} {pnl:>11.4f}")

        lines.extend([sep, "Tournament Cumulative Leaderboard", "  rank   trader_id          pnl"])
        cumulative = self._build_cumulative_rankings()
        for idx in range(self.max_leaderboard_rows):
            if idx >= len(cumulative):
                lines.append("  ")
                continue
            row = cumulative[idx]
            lines.append(f"  {row['rank']:>4}   {row['trader_id']:<14} {row['pnl']:>11.4f}")
        lines.append(sep)
        lines.append("Waiting for next round...")
        return lines

    def _render_tournament_complete(self, sep: str) -> list[str]:
        lines = [
            "OpenMarketSim Arena | Tournament Complete",
            sep,
            f"Rounds: {self.tournament_rounds_completed} / {self.tournament_total_rounds}",
            sep,
            "Final Leaderboard",
            "  rank   trader_id          pnl",
        ]
        sorted_rankings = self._build_cumulative_rankings()
        for idx in range(max(self.max_leaderboard_rows, len(sorted_rankings))):
            if idx >= len(sorted_rankings):
                lines.append("  ")
                continue
            row = sorted_rankings[idx]
            rank = int(row.get("rank", 0))
            trader_id = str(row.get("trader_id", "-"))
            pnl = _round4(float(row.get("pnl", 0.0)))
            lines.append(f"  {rank:>4}   {trader_id:<14} {pnl:>11.4f}")
        lines.append(sep)
        lines.append("Press Enter to exit...")
        return lines

    def render(self, now_monotonic: float) -> str:
        width = max(80, shutil.get_terminal_size((120, 40)).columns)
        sep = "-" * width
        self.advance_lifecycle(now_monotonic)

        if self.lifecycle_state == "TOURNAMENT_COMPLETE":
            lines = self._render_tournament_complete(sep)
        elif self.lifecycle_state == "ROUND_COMPLETE":
            lines = self._render_round_complete(now_monotonic, sep)
        else:
            lines = self._render_running(now_monotonic, width, sep)

        # Keep a stable minimum frame height to avoid visible jitter.
        min_rows = 44
        if len(lines) < min_rows:
            lines.extend([""] * (min_rows - len(lines)))

        return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenMarketSim terminal competitive arena view")
    parser.add_argument("--uri", default="ws://localhost:8000", help="Exchange WebSocket URI")
    parser.add_argument(
        "--refresh-ms",
        type=int,
        default=500,
        help="Screen refresh interval in milliseconds",
    )
    return parser.parse_args()


async def receiver_loop(
    websocket: Any,
    state: ArenaState,
    state_lock: asyncio.Lock,
    connection_closed_event: asyncio.Event,
) -> None:
    from websockets.exceptions import ConnectionClosed

    try:
        async for raw_message in websocket:
            try:
                event = json.loads(raw_message)
            except json.JSONDecodeError:
                continue
            now = asyncio.get_running_loop().time()
            async with state_lock:
                state.apply_event(event, now)
    except ConnectionClosed:
        pass
    finally:
        async with state_lock:
            state.on_connection_closed()
        connection_closed_event.set()


async def render_loop(
    state: ArenaState,
    state_lock: asyncio.Lock,
    refresh_ms: int,
    connection_closed_event: asyncio.Event,
    exit_event: asyncio.Event,
) -> None:
    refresh_seconds = max(0.1, refresh_ms / 1000.0)
    loop = asyncio.get_running_loop()
    next_tick = loop.time()
    wait_enter_task: asyncio.Task[None] | None = None

    # Enter stable dashboard mode.
    sys.stdout.write(ANSI_HIDE_CURSOR)
    sys.stdout.write(ANSI_HOME + ANSI_CLEAR_TO_END)
    sys.stdout.flush()

    try:
        while not exit_event.is_set():
            now = loop.time()
            async with state_lock:
                frame = state.render(now_monotonic=now)
                lifecycle_state = state.lifecycle_state
                connection_closed = state.connection_closed

            # Required ANSI flow: move to top, clear, render a single frame.
            sys.stdout.write(ANSI_HOME + ANSI_CLEAR_TO_END)
            sys.stdout.write(frame)
            sys.stdout.flush()

            if lifecycle_state == "TOURNAMENT_COMPLETE":
                if wait_enter_task is None:
                    wait_enter_task = asyncio.create_task(asyncio.to_thread(sys.stdin.readline))
                if wait_enter_task.done():
                    exit_event.set()
            elif connection_closed and lifecycle_state != "TOURNAMENT_COMPLETE":
                # Connection dropped before final lifecycle signal. Keep dashboard open.
                # User can still Ctrl+C out; we do not auto-return to shell.
                pass

            next_tick += refresh_seconds
            sleep_for = max(0.0, next_tick - loop.time())
            await asyncio.sleep(sleep_for)
    finally:
        if wait_enter_task is not None and not wait_enter_task.done():
            wait_enter_task.cancel()
            await asyncio.gather(wait_enter_task, return_exceptions=True)
        # Always restore cursor for a clean terminal even on interrupts.
        sys.stdout.write(ANSI_SHOW_CURSOR)
        sys.stdout.flush()


async def run_arena(uri: str, refresh_ms: int) -> None:
    import websockets

    state = ArenaState()
    state_lock = asyncio.Lock()
    connection_closed_event = asyncio.Event()
    exit_event = asyncio.Event()

    async with websockets.connect(uri) as websocket:
        receiver = asyncio.create_task(
            receiver_loop(websocket, state, state_lock, connection_closed_event)
        )
        renderer = asyncio.create_task(
            render_loop(state, state_lock, refresh_ms, connection_closed_event, exit_event)
        )
        await exit_event.wait()
        receiver.cancel()
        renderer.cancel()
        await asyncio.gather(receiver, renderer, return_exceptions=True)


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_arena(args.uri, args.refresh_ms))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
