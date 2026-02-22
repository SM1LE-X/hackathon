# File: tournament_manager.py

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from arena_config import ArenaConfig


def _round4(value: float) -> float:
    rounded = round(value, 4)
    if rounded == 0:
        return 0.0
    return rounded


@dataclass(slots=True)
class InterruptReport:
    rounds_completed: int
    total_rounds: int
    current_round: int
    current_round_status: str
    cumulative_rows: list[tuple[int, str, float]]


class TournamentManager:
    """
    Tournament orchestration with deterministic interrupt handling.

    Notes:
    - Rendering remains read-only in `arena_cli.py`.
    - Matching/risk logic stays in engine/risk modules.
    - This manager handles runtime lifecycle only.
    """

    def __init__(
        self,
        config: ArenaConfig,
        host: str = "localhost",
        port: int = 8000,
    ) -> None:
        self._config = config
        self._host = host
        self._port = port

        self._runner: asyncio.Runner | None = None
        self._server: Any | None = None
        self._ws_server: Any | None = None
        self._round_event: asyncio.Event | None = None
        self._shutdown_done = False

        self._rounds_completed = 0
        self._recorded_round_ids: set[int] = set()
        self._cumulative_pnl: dict[str, float] = {}
        self._last_seen_round = 0
        self._last_round_was_partial = False

    def run(self) -> None:
        if self._runner is not None:
            raise RuntimeError("tournament already running")

        self._runner = asyncio.Runner()
        self._runner.__enter__()
        try:
            self._runner.run(self._run_async())
        except Exception:
            # Keep runner open for `handle_interrupt()` or caller cleanup.
            raise
        else:
            self._close_runner()

    def handle_interrupt(self) -> None:
        if self._runner is None:
            report = self._build_interrupt_report(current_status="complete")
            self._print_interrupt_report(report)
            raise SystemExit(0)

        try:
            self._runner.run(self._handle_interrupt_async())
        finally:
            self._close_runner()

        status = "partial" if self._last_round_was_partial else "complete"
        report = self._build_interrupt_report(current_status=status)
        self._print_interrupt_report(report)
        raise SystemExit(0)

    async def _run_async(self) -> None:
        import websockets

        from server import ExchangeServer

        self._round_event = asyncio.Event()
        self._server = ExchangeServer(session_duration_seconds=self._config.duration)
        self._server.set_session_end_callback(self._on_session_end)

        self._ws_server = await websockets.serve(
            self._server.handle_connection,
            self._host,
            self._port,
        )
        await self._server.start()

        while self._rounds_completed < self._config.rounds:
            await self._round_event.wait()
            self._round_event.clear()

        if self._server is not None:
            await self._server.broadcast_event(self._build_tournament_complete_event())

        # Stop any additional round starts once target rounds are completed.
        self._server.request_stop_after_current_round()
        await self._shutdown_async()

    async def _handle_interrupt_async(self) -> None:
        if self._server is None:
            return

        # Freeze intake first so no new orders are accepted after interrupt.
        await self._server.begin_shutdown_mode()
        await self._server.stop()

        current_round = self._server.current_round()
        self._last_seen_round = max(self._last_seen_round, current_round)

        if self._server.is_session_active():
            # Case B: interrupt during active round.
            # Finalize the in-flight round deterministically without reset,
            # so partial progress is accounted exactly once.
            event = await self._server.finalize_current_round_for_interrupt()
            if event is not None:
                self._record_session_end(event)
                self._last_round_was_partial = True
        # Case A (between rounds) naturally falls through without force-close.

        if self._server is not None:
            await self._server.broadcast_event(self._build_tournament_complete_event())

        await self._shutdown_async()

    async def _shutdown_async(self) -> None:
        if self._shutdown_done:
            return

        if self._server is not None:
            await self._server.begin_shutdown_mode()
            await self._server.stop()

        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None

        self._shutdown_done = True

    def _on_session_end(self, event: dict[str, Any]) -> None:
        self._record_session_end(event)

        if self._round_event is not None:
            self._round_event.set()

        if self._rounds_completed >= self._config.rounds and self._server is not None:
            self._server.request_stop_after_current_round()

    def _record_session_end(self, event: dict[str, Any]) -> None:
        round_id = int(event.get("round", 0))
        if round_id <= 0 or round_id in self._recorded_round_ids:
            return

        self._recorded_round_ids.add(round_id)
        self._rounds_completed += 1
        self._last_seen_round = max(self._last_seen_round, round_id)

        rankings = event.get("rankings", [])
        if not isinstance(rankings, list):
            return
        for row in rankings:
            trader_id = str(row.get("trader_id", ""))
            if not trader_id:
                continue
            pnl = _round4(float(row.get("pnl", 0.0)))
            self._cumulative_pnl[trader_id] = _round4(self._cumulative_pnl.get(trader_id, 0.0) + pnl)

    def _build_interrupt_report(self, current_status: str) -> InterruptReport:
        ranked = sorted(
            self._cumulative_pnl.items(),
            key=lambda item: (-item[1], item[0]),
        )
        rows: list[tuple[int, str, float]] = []
        for idx, (trader_id, pnl) in enumerate(ranked, start=1):
            rows.append((idx, trader_id, _round4(pnl)))

        return InterruptReport(
            rounds_completed=self._rounds_completed,
            total_rounds=self._config.rounds,
            current_round=self._last_seen_round,
            current_round_status=current_status,
            cumulative_rows=rows,
        )

    def _build_tournament_complete_event(self) -> dict[str, Any]:
        ranked = sorted(
            self._cumulative_pnl.items(),
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
        return {
            "type": "tournament_complete",
            "rounds_completed": self._rounds_completed,
            "total_rounds": self._config.rounds,
            "rankings": rows,
        }

    @staticmethod
    def _print_interrupt_report(report: InterruptReport) -> None:
        print("==================================")
        print("TOURNAMENT INTERRUPTED")
        print("==================================")
        print()
        print(f"Rounds completed: {report.rounds_completed} / {report.total_rounds}")
        print(f"Current round: {report.current_round} ({report.current_round_status})")
        print()
        print("CUMULATIVE SCORE")
        print("----------------------------------")
        print("Rank   Trader      Total PnL")
        print("----------------------------------")
        if not report.cumulative_rows:
            print("N/A    N/A         +0.00")
        else:
            for rank, trader_id, pnl in report.cumulative_rows:
                sign = "+" if pnl >= 0 else "-"
                print(f"{rank:<6} {trader_id:<11} {sign}{abs(pnl):.2f}")
        print("----------------------------------")
        print()
        print("Thank you for trading.")

    def _close_runner(self) -> None:
        if self._runner is None:
            return
        self._runner.close()
        self._runner = None


def _format_startup_banner(config: ArenaConfig, starting_capital: float) -> str:
    return (
        "==================================\n"
        "CLI TRADING ARENA\n"
        f"Rounds: {config.rounds}\n"
        f"Session Duration: {config.duration}s\n"
        f"Starting Capital: {starting_capital:.0f}\n"
        "=================================="
    )


def startup_and_run(config: ArenaConfig) -> None:
    from margin_risk_manager import STARTING_CAPITAL

    print(_format_startup_banner(config, STARTING_CAPITAL))
    input("\nPress Enter to begin...")

    tournament = TournamentManager(config=config)
    try:
        tournament.run()
    except KeyboardInterrupt:
        tournament.handle_interrupt()
    else:
        # Normal completion path.
        raise SystemExit(0)
