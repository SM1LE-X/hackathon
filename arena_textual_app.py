# File: arena_textual_app.py

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static


def _round4(value: float) -> float:
    rounded = round(value, 4)
    if rounded == 0:
        return 0.0
    return rounded


class ArenaPhase(str, Enum):
    PRE_ROUND = "PRE_ROUND"
    RUNNING = "RUNNING"
    ROUND_COMPLETE = "ROUND_COMPLETE"
    TOURNAMENT_COMPLETE = "TOURNAMENT_COMPLETE"


class ServerStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class ArenaMode(str, Enum):
    SIMULATION = "SIMULATION"
    LIVE = "LIVE"
    OFFLINE = "OFFLINE"


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: float
    quantity: int


@dataclass(frozen=True, slots=True)
class TraderSnapshot:
    trader_id: str
    position: int
    cash: float
    realized: float
    unrealized: float
    total: float
    margin_pct: float
    liquidation_risk: bool
    is_live: bool
    latency: float | None


@dataclass(frozen=True, slots=True)
class RankingEntry:
    rank: int
    trader_id: str
    pnl: float


@dataclass(frozen=True, slots=True)
class RoundSummary:
    round_number: int
    mark_price: float
    spread: float
    rankings: tuple[RankingEntry, ...]


@dataclass(frozen=True, slots=True)
class EngineStats:
    ticks: int
    simulated_trades: int
    messages_processed: int


@dataclass(frozen=True, slots=True)
class ArenaViewState:
    arena_name: str
    phase: ArenaPhase
    mode: ArenaMode
    server_status: ServerStatus
    current_round: int
    total_rounds: int
    countdown_seconds: float
    mark_price: float
    spread: float
    connected_trader: str
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    traders: tuple[TraderSnapshot, ...]
    last_round: RoundSummary | None
    round_history: tuple[RoundSummary, ...]
    tournament_leaderboard: tuple[RankingEntry, ...]
    engine_stats: EngineStats


class TournamentController:
    """
    Tournament lifecycle controller.

    This class manages round transitions and produces immutable snapshots for the TUI.
    Replace `_simulate_live_round` with real exchange snapshots when integrating.
    """

    def __init__(
        self,
        *,
        total_rounds: int = 5,
        round_seconds: int = 60,
        pre_round_seconds: int = 3,
        round_complete_seconds: int = 3,
        connected_trader: str = "trader_1",
        arena_name: str = "OpenMarketSim Arena",
        mode: ArenaMode = ArenaMode.SIMULATION,
        server_status: ServerStatus = ServerStatus.ONLINE,
        traders: Iterable[str] | None = None,
    ) -> None:
        self._arena_name = arena_name
        self._total_rounds = max(1, int(total_rounds))
        self._round_seconds = max(1, int(round_seconds))
        self._pre_round_seconds = max(1, int(pre_round_seconds))
        self._round_complete_seconds = max(1, int(round_complete_seconds))
        self._connected_trader = connected_trader
        self._mode = mode
        self._server_status = server_status
        if self._mode == ArenaMode.OFFLINE:
            self._server_status = ServerStatus.OFFLINE
        self._trader_ids = tuple(traders or ("trader_1", "trader_2", "trader_3", "trader_4"))
        self.restart()

    def restart(self) -> None:
        self._phase = ArenaPhase.PRE_ROUND
        self._phase_remaining = float(self._pre_round_seconds)
        self._current_round = 1
        self._tick_index = 0
        self._mark_price = 100.0
        self._spread = 0.5
        self._bids: tuple[PriceLevel, ...] = tuple()
        self._asks: tuple[PriceLevel, ...] = tuple()
        self._last_round: RoundSummary | None = None
        self._round_history: list[RoundSummary] = []
        self._tournament_score: dict[str, float] = {trader: 0.0 for trader in self._trader_ids}
        self._live_traders: dict[str, TraderSnapshot] = {
            trader: TraderSnapshot(
                trader_id=trader,
                position=0,
                cash=10_000.0,
                realized=0.0,
                unrealized=0.0,
                total=0.0,
                margin_pct=100.0,
                liquidation_risk=False,
                is_live=False,
                latency=None,
            )
            for trader in self._trader_ids
        }
        self._stats_ticks = 0
        self._stats_trades = 0
        self._stats_messages = 0
        if self._server_status == ServerStatus.OFFLINE or self._mode == ArenaMode.OFFLINE:
            self._freeze_connectivity_state()
        else:
            self._simulate_live_round()

    def force_next_round_dev(self) -> None:
        if self._phase == ArenaPhase.TOURNAMENT_COMPLETE:
            return
        if self._phase == ArenaPhase.PRE_ROUND:
            self._phase = ArenaPhase.RUNNING
            self._phase_remaining = float(self._round_seconds)
            return
        if self._phase == ArenaPhase.RUNNING:
            self._finalize_round()
            return
        if self._phase == ArenaPhase.ROUND_COMPLETE:
            self._phase_remaining = 0.0

    def ingest_external_snapshot(self, _snapshot: object) -> None:
        """
        Placeholder integration hook.
        Replace mock simulation with real engine/tournament snapshots here.
        """

    def tick(self, delta_seconds: float = 0.5) -> ArenaViewState:
        self._stats_ticks += 1
        self._stats_messages += 1
        step = max(0.1, float(delta_seconds))

        if self._phase == ArenaPhase.PRE_ROUND:
            self._phase_remaining = max(0.0, self._phase_remaining - step)
            if self._phase_remaining <= 0:
                self._phase = ArenaPhase.RUNNING
                self._phase_remaining = float(self._round_seconds)

        elif self._phase == ArenaPhase.RUNNING:
            if self._server_status == ServerStatus.OFFLINE or self._mode == ArenaMode.OFFLINE:
                # Explicit offline behavior: freeze market data while keeping lifecycle active.
                self._freeze_connectivity_state()
            else:
                self._simulate_live_round()
            self._phase_remaining = max(0.0, self._phase_remaining - step)
            if self._phase_remaining <= 0:
                self._finalize_round()

        elif self._phase == ArenaPhase.ROUND_COMPLETE:
            self._phase_remaining = max(0.0, self._phase_remaining - step)
            if self._phase_remaining <= 0:
                if self._current_round >= self._total_rounds:
                    self._phase = ArenaPhase.TOURNAMENT_COMPLETE
                    self._phase_remaining = 0.0
                else:
                    self._current_round += 1
                    self._phase = ArenaPhase.PRE_ROUND
                    self._phase_remaining = float(self._pre_round_seconds)
                    if self._server_status != ServerStatus.OFFLINE and self._mode != ArenaMode.OFFLINE:
                        self._simulate_live_round()
                    else:
                        self._freeze_connectivity_state()

        return self.get_state()

    def _freeze_connectivity_state(self) -> None:
        frozen: dict[str, TraderSnapshot] = {}
        for trader_id, row in self._live_traders.items():
            frozen[trader_id] = TraderSnapshot(
                trader_id=row.trader_id,
                position=row.position,
                cash=row.cash,
                realized=row.realized,
                unrealized=row.unrealized,
                total=row.total,
                margin_pct=row.margin_pct,
                liquidation_risk=row.liquidation_risk,
                is_live=False,
                latency=None,
            )
        self._live_traders = frozen

    def get_state(self) -> ArenaViewState:
        leaderboard = self._build_tournament_leaderboard()
        traders = tuple(self._live_traders.values())
        return ArenaViewState(
            arena_name=self._arena_name,
            phase=self._phase,
            mode=self._mode,
            server_status=self._server_status,
            current_round=self._current_round,
            total_rounds=self._total_rounds,
            countdown_seconds=_round4(self._phase_remaining),
            mark_price=_round4(self._mark_price),
            spread=_round4(self._spread),
            connected_trader=self._connected_trader,
            bids=self._bids,
            asks=self._asks,
            traders=traders,
            last_round=self._last_round,
            round_history=tuple(self._round_history),
            tournament_leaderboard=leaderboard,
            engine_stats=EngineStats(
                ticks=self._stats_ticks,
                simulated_trades=self._stats_trades,
                messages_processed=self._stats_messages,
            ),
        )

    def _simulate_live_round(self) -> None:
        if self._server_status == ServerStatus.OFFLINE or self._mode == ArenaMode.OFFLINE:
            # Keep last stable values and explicitly mark sessions as offline.
            self._freeze_connectivity_state()
            return

        self._tick_index += 1
        t = self._tick_index + (self._current_round * 11)
        is_live_feed = self._mode == ArenaMode.LIVE and self._server_status == ServerStatus.ONLINE
        is_simulation_feed = self._mode == ArenaMode.SIMULATION and self._server_status == ServerStatus.ONLINE

        # Deterministic mock market dynamics for UI integration.
        self._mark_price = _round4(
            100.0
            + 2.4 * math.sin(t / 5.0)
            + 1.2 * math.cos(t / 9.0)
            + (self._current_round - 1) * 0.25
        )
        self._spread = _round4(0.35 + ((1.0 + math.sin(t / 7.0)) * 0.15))
        best_bid = _round4(self._mark_price - (self._spread / 2))
        best_ask = _round4(self._mark_price + (self._spread / 2))

        depth = 10
        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []
        for level in range(depth):
            bid_price = _round4(best_bid - (level * 0.25))
            ask_price = _round4(best_ask + (level * 0.25))
            bid_qty = 5 + ((t + level * 3) % 19)
            ask_qty = 5 + ((t + level * 5) % 19)
            bids.append(PriceLevel(price=bid_price, quantity=bid_qty))
            asks.append(PriceLevel(price=ask_price, quantity=ask_qty))
        self._bids = tuple(bids)
        self._asks = tuple(asks)

        rows: dict[str, TraderSnapshot] = {}
        for idx, trader_id in enumerate(self._trader_ids):
            position = int(round(14 * math.sin((t + idx * 2.2) / 6.0)))
            realized = _round4((self._current_round - 1) * ((idx - 1.5) * 2.1))
            unrealized = _round4(position * (self._mark_price - (99.5 + idx * 0.45)))
            cash = _round4(10_000.0 + realized - (position * 7.25))
            total = _round4(realized + unrealized)
            margin_pct = _round4(max(1.0, min(100.0, 100.0 - abs(position) * 3.0 - max(0.0, -total) * 0.09)))
            if is_live_feed:
                latency: float | None = float(8 + ((t + idx * 5) % 37))
                trader_live = True
            elif is_simulation_feed:
                latency = 0.0
                trader_live = False
            else:
                latency = None
                trader_live = False
            rows[trader_id] = TraderSnapshot(
                trader_id=trader_id,
                position=position,
                cash=cash,
                realized=realized,
                unrealized=unrealized,
                total=total,
                margin_pct=margin_pct,
                liquidation_risk=margin_pct < 15.0,
                is_live=trader_live,
                latency=latency,
            )

        self._live_traders = rows
        self._stats_trades += int(abs(math.sin(t / 3.0)) * 4)
        self._stats_messages += len(self._trader_ids)

    def _finalize_round(self) -> None:
        ranked_rows = sorted(
            self._live_traders.values(),
            key=lambda row: (-row.total, row.trader_id),
        )
        rankings = tuple(
            RankingEntry(rank=index + 1, trader_id=row.trader_id, pnl=_round4(row.total))
            for index, row in enumerate(ranked_rows)
        )

        summary = RoundSummary(
            round_number=self._current_round,
            mark_price=_round4(self._mark_price),
            spread=_round4(self._spread),
            rankings=rankings,
        )
        self._last_round = summary
        self._round_history.append(summary)

        for entry in rankings:
            self._tournament_score[entry.trader_id] = _round4(
                self._tournament_score.get(entry.trader_id, 0.0) + entry.pnl
            )

        self._phase = ArenaPhase.ROUND_COMPLETE
        self._phase_remaining = float(self._round_complete_seconds)

    def _build_tournament_leaderboard(self) -> tuple[RankingEntry, ...]:
        ranked = sorted(
            self._tournament_score.items(),
            key=lambda item: (-item[1], item[0]),
        )
        return tuple(
            RankingEntry(rank=index + 1, trader_id=trader_id, pnl=_round4(total_pnl))
            for index, (trader_id, total_pnl) in enumerate(ranked)
        )


class HeaderBar(Static):
    """Top status widget: tournament phase, round progress, timer, mark and spread."""

    def update_from_state(self, state: ArenaViewState) -> None:
        phase_style = {
            ArenaPhase.PRE_ROUND: "yellow",
            ArenaPhase.RUNNING: "green",
            ArenaPhase.ROUND_COMPLETE: "cyan",
            ArenaPhase.TOURNAMENT_COMPLETE: "magenta",
        }[state.phase]
        mode_style = {
            ArenaMode.SIMULATION: "yellow",
            ArenaMode.LIVE: "green",
            ArenaMode.OFFLINE: "bold red",
        }[state.mode]
        server_style = "green" if state.server_status == ServerStatus.ONLINE else "bold red"

        text = Text()
        text.append(f"{state.arena_name}\n", style="bold")
        text.append(f"Round {state.current_round}/{state.total_rounds}  ", style="bold")
        text.append("Status: ", style="bold")
        text.append(state.phase.value, style=phase_style)
        text.append(f"  Countdown: {state.countdown_seconds:05.1f}s\n")
        text.append("Mode: ", style="bold")
        text.append(state.mode.value, style=mode_style)
        text.append("  Server: ", style="bold")
        text.append(state.server_status.value, style=server_style)
        text.append("  ")
        text.append(
            f"Mark: {state.mark_price:8.2f}   Spread: {state.spread:6.2f}   Connected: {state.connected_trader}\n",
            style="dim",
        )
        if state.server_status == ServerStatus.OFFLINE or state.mode == ArenaMode.OFFLINE:
            text.append("SERVER OFFLINE \u2014 NO TRADING", style="bold white on red")
        elif state.mode == ArenaMode.SIMULATION:
            text.append("SIMULATION MODE", style="bold yellow")
        self.update(text)


class OrderBookWidget(Static):
    """Two-sided order book widget with best level highlighting."""

    def update_from_state(self, state: ArenaViewState) -> None:
        depth = 10
        table = Table(expand=True, box=None, show_header=True, pad_edge=False)
        table.add_column("Bid Qty", justify="right", style="cyan")
        table.add_column("Bid Px", justify="right", style="cyan")
        table.add_column("Ask Px", justify="right", style="magenta")
        table.add_column("Ask Qty", justify="right", style="magenta")

        for row_index in range(depth):
            bid_qty = ""
            bid_px = ""
            ask_px = ""
            ask_qty = ""
            if row_index < len(state.bids):
                bid = state.bids[row_index]
                bid_qty = str(bid.quantity)
                bid_px = f"{bid.price:,.2f}"
            if row_index < len(state.asks):
                ask = state.asks[row_index]
                ask_px = f"{ask.price:,.2f}"
                ask_qty = str(ask.quantity)

            if row_index == 0:
                table.add_row(
                    Text(bid_qty, style="bold green"),
                    Text(bid_px, style="bold green"),
                    Text(ask_px, style="bold red"),
                    Text(ask_qty, style="bold red"),
                )
            else:
                table.add_row(bid_qty, bid_px, ask_px, ask_qty)

        if state.server_status == ServerStatus.OFFLINE or state.mode == ArenaMode.OFFLINE:
            banner = Text("SERVER OFFLINE \u2014 NO TRADING", style="bold white on red")
            self.update(Group(banner, Text(""), table))
            return

        self.update(table)


class TraderTableWidget(DataTable):
    """Live trader metrics table sorted by total PnL descending."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("Trader", "Session", "Latency", "Pos", "Cash", "Realized", "Unrealized", "Total", "Margin%")

    def update_from_state(self, state: ArenaViewState) -> None:
        self.clear()

        rows = sorted(
            state.traders,
            key=lambda row: (-row.total, row.trader_id),
        )
        for row in rows:
            is_current = row.trader_id == state.connected_trader
            trader_label = f"> {row.trader_id}" if is_current else row.trader_id
            trader_style = "bold cyan" if is_current else ""

            total_style = "green" if row.total >= 0 else "red"
            realized_style = "green" if row.realized >= 0 else "red"
            unrealized_style = "green" if row.unrealized >= 0 else "red"

            margin_style = "green"
            if row.liquidation_risk:
                margin_style = "bold red"
            elif row.margin_pct < 25:
                margin_style = "yellow"

            margin_text = Text(f"{row.margin_pct:6.2f}%", style=margin_style)
            if row.liquidation_risk:
                margin_text.append(" !", style="bold red")

            if state.server_status == ServerStatus.OFFLINE or state.mode == ArenaMode.OFFLINE:
                session_text = Text("OFFLINE", style="bold red")
                latency_text = Text("-", style="dim")
            else:
                session_text = Text("LIVE" if row.is_live else "SIM", style="green" if row.is_live else "yellow")
                if row.latency is None:
                    latency_text = Text("-", style="dim")
                else:
                    latency_style = "cyan" if row.latency > 0 else "yellow"
                    latency_text = Text(f"{row.latency:5.1f}ms", style=latency_style)

            self.add_row(
                Text(trader_label, style=trader_style),
                session_text,
                latency_text,
                Text(f"{row.position:>4}", style=trader_style),
                Text(f"{row.cash:>10.2f}", style=trader_style),
                Text(f"{row.realized:>+10.2f}", style=realized_style),
                Text(f"{row.unrealized:>+10.2f}", style=unrealized_style),
                Text(f"{row.total:>+10.2f}", style=total_style),
                margin_text,
            )


class LastRoundWidget(Static):
    """Round summary widget for the most recently completed round."""

    def update_from_state(self, state: ArenaViewState) -> None:
        summary = state.last_round
        if summary is None:
            self.update(Text("Last Round: waiting for first completion...", style="dim"))
            return

        lines = [
            f"Last Round #{summary.round_number}   Mark: {summary.mark_price:.2f}   Spread: {summary.spread:.2f}",
            "",
            "Rank  Trader         PnL",
            "----  -------------  ----------",
        ]
        for entry in summary.rankings[:8]:
            lines.append(f"{entry.rank:>4}  {entry.trader_id:<13}  {entry.pnl:>+10.2f}")
        self.update("\n".join(lines))


class TournamentLeaderboardWidget(DataTable):
    """Cumulative tournament leaderboard across all completed rounds."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("Rank", "Trader", "Cumulative PnL")

    def update_from_state(self, state: ArenaViewState) -> None:
        self.clear()
        for row in state.tournament_leaderboard:
            pnl_style = "green" if row.pnl >= 0 else "red"
            self.add_row(
                str(row.rank),
                row.trader_id,
                Text(f"{row.pnl:+.2f}", style=pnl_style),
            )


class RoundHistoryWidget(Static):
    """Optional historical panel with compact summaries for all completed rounds."""

    def update_from_state(self, state: ArenaViewState) -> None:
        if not state.round_history:
            self.update(Text("Round History: no completed rounds yet.", style="dim"))
            return

        lines = ["Round History", "", "Round  Mark     Winner         Winner PnL"]
        lines.append("-----  -------  -------------  ----------")
        for summary in state.round_history[-12:]:
            winner = summary.rankings[0] if summary.rankings else RankingEntry(0, "-", 0.0)
            lines.append(
                f"{summary.round_number:>5}  {summary.mark_price:>7.2f}  "
                f"{winner.trader_id:<13}  {winner.pnl:>+10.2f}"
            )
        self.update("\n".join(lines))


class FooterBar(Static):
    """Bottom control hint and engine stat bar."""

    def update_from_state(self, state: ArenaViewState) -> None:
        controls = "q Quit   r Restart   n Next Round (dev)   h Toggle History"
        stats = (
            f"Ticks: {state.engine_stats.ticks}   "
            f"SimTrades: {state.engine_stats.simulated_trades}   "
            f"Msgs: {state.engine_stats.messages_processed}"
        )
        self.update(f"{controls}\n{stats}")


class ArenaApp(App):
    """Textual TUI entrypoint for the competitive arena."""

    CSS = """
    Screen {
        layout: vertical;
        background: #0f1115;
        color: #d8dde6;
    }

    #header {
        height: 4;
        border: round #3a414b;
        padding: 0 1;
        margin: 0 1;
    }

    #body {
        height: 1fr;
        margin: 0 1;
    }

    #middle {
        height: 16;
    }

    #orderbook {
        width: 38%;
        border: round #2f3640;
        padding: 0 1;
        margin-right: 1;
    }

    #trader-table {
        width: 62%;
        border: round #2f3640;
        padding: 0 1;
    }

    #last-round {
        height: 8;
        border: round #2f3640;
        padding: 0 1;
        margin-top: 1;
    }

    #tournament-board {
        height: 10;
        border: round #2f3640;
        padding: 0 1;
        margin-top: 1;
    }

    #round-history {
        height: 10;
        border: round #2f3640;
        padding: 0 1;
        margin-top: 1;
    }

    #round-history.hidden {
        display: none;
    }

    #footer {
        height: 2;
        border: round #3a414b;
        padding: 0 1;
        margin: 1;
        color: #a9b2bf;
    }
    """

    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("r", "restart_tournament", "Restart"),
        Binding("n", "next_round", "Next Round"),
        Binding("h", "toggle_history", "History"),
    ]

    def __init__(self, controller: TournamentController) -> None:
        super().__init__()
        self._controller = controller
        self._state = controller.get_state()
        self._show_history = False

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with Vertical(id="body"):
            with Horizontal(id="middle"):
                yield OrderBookWidget(id="orderbook")
                yield TraderTableWidget(id="trader-table")
            yield LastRoundWidget(id="last-round")
            yield TournamentLeaderboardWidget(id="tournament-board")
            yield RoundHistoryWidget(id="round-history", classes="hidden")
        yield FooterBar(id="footer")

    def on_mount(self) -> None:
        self._header = self.query_one("#header", HeaderBar)
        self._orderbook = self.query_one("#orderbook", OrderBookWidget)
        self._traders = self.query_one("#trader-table", TraderTableWidget)
        self._last_round = self.query_one("#last-round", LastRoundWidget)
        self._leaderboard = self.query_one("#tournament-board", TournamentLeaderboardWidget)
        self._history = self.query_one("#round-history", RoundHistoryWidget)
        self._footer = self.query_one("#footer", FooterBar)

        self._apply_state(self._state)
        self.set_interval(0.5, self.update_loop)

    def update_loop(self) -> None:
        self._state = self._controller.tick(0.5)
        self._apply_state(self._state)

    def _apply_state(self, state: ArenaViewState) -> None:
        self._header.update_from_state(state)
        self._orderbook.update_from_state(state)
        self._traders.update_from_state(state)
        self._last_round.update_from_state(state)
        self._leaderboard.update_from_state(state)
        self._history.update_from_state(state)
        self._footer.update_from_state(state)

    def action_quit_app(self) -> None:
        self.exit()

    def action_restart_tournament(self) -> None:
        self._controller.restart()
        self._state = self._controller.get_state()
        self._apply_state(self._state)

    def action_next_round(self) -> None:
        self._controller.force_next_round_dev()
        self._state = self._controller.get_state()
        self._apply_state(self._state)

    def action_toggle_history(self) -> None:
        self._show_history = not self._show_history
        if self._show_history:
            self._history.remove_class("hidden")
        else:
            self._history.add_class("hidden")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Production-grade Textual TUI arena")
    parser.add_argument("--rounds", type=int, default=5, help="Tournament rounds")
    parser.add_argument("--duration", type=int, default=60, help="Round duration in seconds")
    parser.add_argument("--trader", type=str, default="trader_1", help="Connected trader identifier")
    parser.add_argument(
        "--mode",
        type=str,
        default=ArenaMode.SIMULATION.value,
        choices=[mode.value for mode in ArenaMode],
        help="Connectivity mode: SIMULATION, LIVE, or OFFLINE",
    )
    parser.add_argument(
        "--server-status",
        type=str,
        default=ServerStatus.ONLINE.value,
        choices=[status.value for status in ServerStatus],
        help="Server connectivity state shown in the UI",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    controller = TournamentController(
        total_rounds=args.rounds,
        round_seconds=args.duration,
        connected_trader=args.trader,
        mode=ArenaMode(args.mode),
        server_status=ServerStatus(args.server_status),
    )
    app = ArenaApp(controller=controller)
    app.run()


if __name__ == "__main__":
    main()

