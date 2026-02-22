# File: tests/test_phase4_tournament_ux_snippet.py

from __future__ import annotations

from arena_config import ArenaConfig, parse_arena_config
from tournament_manager import TournamentManager


def test_arena_config_cli_args() -> None:
    cfg = parse_arena_config(["--rounds", "3", "--duration", "45"])
    assert cfg == ArenaConfig(rounds=3, duration=45)


def test_tournament_interrupt_report_from_cumulative_scores() -> None:
    manager = TournamentManager(config=ArenaConfig(rounds=5, duration=60))

    manager._record_session_end(  # noqa: SLF001 - targeted snippet test
        {
            "type": "session_end",
            "round": 1,
            "rankings": [
                {"rank": 1, "trader_id": "trader_2", "pnl": 12.5},
                {"rank": 2, "trader_id": "trader_1", "pnl": -12.5},
            ],
        }
    )
    manager._record_session_end(  # noqa: SLF001 - targeted snippet test
        {
            "type": "session_end",
            "round": 2,
            "rankings": [
                {"rank": 1, "trader_id": "trader_1", "pnl": 5.0},
                {"rank": 2, "trader_id": "trader_2", "pnl": -1.0},
            ],
        }
    )

    report = manager._build_interrupt_report("partial")  # noqa: SLF001 - targeted snippet test
    assert report.rounds_completed == 2
    assert report.total_rounds == 5
    assert report.current_round == 2
    assert report.current_round_status == "partial"
    assert report.cumulative_rows == [
        (1, "trader_2", 11.5),
        (2, "trader_1", -7.5),
    ]
