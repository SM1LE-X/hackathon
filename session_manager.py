# File: session_manager.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engine import MatchingEngine
from positions import PositionManager


SESSION_DURATION_SECONDS = 60


def _round4(value: float) -> float:
    rounded = round(value, 4)
    if rounded == 0:
        return 0.0
    return rounded


@dataclass(slots=True)
class SessionConfig:
    duration_seconds: int = SESSION_DURATION_SECONDS

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")


class SessionManager:
    """
    Timed competitive session lifecycle controller.

    Responsibilities:
    - Track session round state and timing windows.
    - Build deterministic session events.
    - Force-close all positions at session end mark price.
    - Reset exchange state between rounds.
    """

    def __init__(self, config: SessionConfig | None = None) -> None:
        self._config = config or SessionConfig()
        self._round_id = 0
        self._session_active = False
        self._ends_at_monotonic = 0.0

    @property
    def duration_seconds(self) -> int:
        return self._config.duration_seconds

    @property
    def round_id(self) -> int:
        return self._round_id

    @property
    def is_active(self) -> bool:
        return self._session_active

    @property
    def ends_at_monotonic(self) -> float:
        return self._ends_at_monotonic

    def start_session(self, now_monotonic: float) -> dict[str, Any]:
        self._round_id += 1
        self._session_active = True
        self._ends_at_monotonic = now_monotonic + float(self._config.duration_seconds)
        return {
            "type": "session_start",
            "round": self._round_id,
            "duration_seconds": self._config.duration_seconds,
        }

    def is_order_window_open(self, now_monotonic: float) -> bool:
        return self._session_active and now_monotonic < self._ends_at_monotonic

    def end_session(self, rankings: list[dict[str, Any]], mark_price: float) -> dict[str, Any]:
        self._session_active = False
        return self.broadcast_leaderboard(rankings=rankings, mark_price=mark_price)

    def force_close_positions(
        self,
        positions: PositionManager,
        mark_price: float,
    ) -> list[dict[str, Any]]:
        return positions.force_close_all(mark_price=mark_price)

    def broadcast_leaderboard(
        self,
        rankings: list[dict[str, Any]],
        mark_price: float,
    ) -> dict[str, Any]:
        ranked_rows: list[dict[str, Any]] = []
        for idx, row in enumerate(rankings, start=1):
            ranked_rows.append(
                {
                    "rank": idx,
                    "trader_id": str(row["trader_id"]),
                    "pnl": _round4(float(row["total_pnl"])),
                }
            )
        return {
            "type": "session_end",
            "round": self._round_id,
            "mark_price": _round4(mark_price),
            "rankings": ranked_rows,
        }

    def reset_exchange_state(self, engine: MatchingEngine, positions: PositionManager) -> None:
        engine.reset_state()
        positions.reset()
