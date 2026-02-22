# File: tests/test_phase4_arena_cli_snippet.py

from arena_cli import ArenaState
from models import SYMBOL, Side


def test_arena_state_lifecycle_round_and_tournament_completion() -> None:
    state = ArenaState()
    now = 100.0

    state.apply_event(
        {
            "type": "welcome",
            "trader_id": "trader_view",
            "session_round": 1,
            "session_active": True,
        },
        now,
    )
    state.apply_event(
        {
            "type": "session_start",
            "round": 2,
            "duration_seconds": 60,
        },
        now,
    )
    state.apply_event(
        {
            "type": "book_update",
            "best_bid": 100,
            "best_ask": 101,
            "bids": [[100, 5]],
            "asks": [[101, 4]],
        },
        now,
    )
    state.apply_event(
        {
            "type": "trader_table",
            "round": 2,
            "rows": [
                {
                    "trader_id": "trader_1",
                    "position": 3,
                    "cash": -300.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 6.0,
                    "total_pnl": 6.0,
                }
            ],
        },
        now,
    )
    state.apply_event(
        {
            "type": "session_end",
            "round": 2,
            "mark_price": 100.5,
            "rankings": [{"rank": 1, "trader_id": "trader_1", "pnl": 6.0}],
        },
        now + 60.0,
    )
    assert len(state.round_history) == 1
    assert state.round_history[0]["round"] == 2
    assert state.tournament_score["trader_1"] == 6.0

    frame = state.render(now_monotonic=now + 60.0)
    assert "Round 2 Complete" in frame
    assert "Round Leaderboard" in frame
    assert "Tournament Cumulative Leaderboard" in frame
    assert "Next round in:" in frame
    assert state.lifecycle_state == "ROUND_COMPLETE"

    state.apply_event(
        {
            "type": "session_start",
            "round": 3,
            "duration_seconds": 60,
        },
        now + 60.1,
    )
    # Hold summary until 3-second pause elapses.
    hold_frame = state.render(now_monotonic=now + 62.0)
    assert "Round Leaderboard" in hold_frame
    assert state.lifecycle_state == "ROUND_COMPLETE"

    running_frame = state.render(now_monotonic=now + 63.2)
    assert state.lifecycle_state == "RUNNING"
    assert "Round 2" in frame
    assert "Order Book" in running_frame
    assert "Trader Table" in running_frame

    state.apply_event(
        {
            "type": "tournament_complete",
            "rounds_completed": 2,
            "total_rounds": 2,
            "rankings": [{"rank": 1, "trader_id": "trader_1", "pnl": 6.0}],
        },
        now + 120.0,
    )
    final_frame = state.render(now_monotonic=now + 120.0)
    assert state.lifecycle_state == "TOURNAMENT_COMPLETE"
    assert "Tournament Complete" in final_frame
    assert "Press Enter to exit..." in final_frame


def test_arena_state_round_history_and_tournament_score_persist() -> None:
    state = ArenaState()
    t0 = 10.0

    state.apply_event(
        {
            "type": "session_end",
            "round": 1,
            "mark_price": 100.0,
            "rankings": [
                {"rank": 1, "trader_id": "trader_2", "pnl": 10.0},
                {"rank": 2, "trader_id": "trader_1", "pnl": -10.0},
            ],
        },
        t0,
    )
    state.apply_event(
        {
            "type": "session_end",
            "round": 2,
            "mark_price": 101.0,
            "rankings": [
                {"rank": 1, "trader_id": "trader_1", "pnl": 7.5},
                {"rank": 2, "trader_id": "trader_2", "pnl": -2.5},
            ],
        },
        t0 + 60.0,
    )

    assert len(state.round_history) == 2
    assert state.round_history[0]["round"] == 1
    assert state.round_history[1]["round"] == 2
    assert state.tournament_score["trader_1"] == -2.5
    assert state.tournament_score["trader_2"] == 7.5

    # Duplicate end event for same round must not mutate history or cumulative score.
    state.apply_event(
        {
            "type": "session_end",
            "round": 2,
            "mark_price": 101.0,
            "rankings": [
                {"rank": 1, "trader_id": "trader_1", "pnl": 7.5},
                {"rank": 2, "trader_id": "trader_2", "pnl": -2.5},
            ],
        },
        t0 + 61.0,
    )
    assert len(state.round_history) == 2
    assert state.tournament_score["trader_1"] == -2.5
    assert state.tournament_score["trader_2"] == 7.5


def test_server_emits_trader_table_snapshot() -> None:
    try:
        from server import ExchangeServer
    except ModuleNotFoundError:
        # Optional in environments where websocket dependency is absent.
        return

    server = ExchangeServer()
    server._session.start_session(now_monotonic=0.0)

    server._engine.execute_limit_order(
        trader_id="maker_ask",
        side=Side.SELL,
        price=100,
        quantity=3,
        symbol=SYMBOL,
    )
    result = server._engine.execute_limit_order(
        trader_id="taker_buy",
        side=Side.BUY,
        price=100,
        quantity=3,
        symbol=SYMBOL,
    )

    broadcast_events: list[dict] = []
    position_events: list[tuple[str, dict]] = []
    liquidation_queue: list[str] = []
    queued: set[str] = set()
    server._ingest_trades(
        trades=result.trades,
        broadcast_events=broadcast_events,
        position_events=position_events,
        liquidation_queue=liquidation_queue,
        queued_liq_traders=queued,
    )

    trader_table = server._build_trader_table_event()
    assert trader_table["type"] == "trader_table"
    assert trader_table["round"] == 1
    assert trader_table["rows"]
    trader_ids = [row["trader_id"] for row in trader_table["rows"]]
    assert trader_ids == sorted(trader_ids)
