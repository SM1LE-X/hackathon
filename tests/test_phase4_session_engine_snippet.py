# File: tests/test_phase4_session_engine_snippet.py

from engine import MatchingEngine
from models import SYMBOL, Side
from positions import PositionManager
from session_manager import SessionConfig, SessionManager


def _apply_trades(positions: PositionManager, trades: list) -> None:
    for trade in trades:
        positions.update_from_trade(trade)


def test_session_engine_force_close_leaderboard_and_round_reset() -> None:
    engine = MatchingEngine()
    positions = PositionManager()
    session = SessionManager(config=SessionConfig(duration_seconds=60))

    session_start_1 = session.start_session(now_monotonic=0.0)
    assert session_start_1["type"] == "session_start"
    assert session_start_1["round"] == 1
    assert session.is_active is True

    # Round 1: open positions through normal matching.
    engine.execute_limit_order(
        trader_id="lp_ask",
        side=Side.SELL,
        price=100,
        quantity=10,
        symbol=SYMBOL,
    )
    result_a = engine.execute_limit_order(
        trader_id="trader_a",
        side=Side.BUY,
        price=100,
        quantity=10,
        symbol=SYMBOL,
    )
    _apply_trades(positions, result_a.trades)

    engine.execute_limit_order(
        trader_id="lp_bid",
        side=Side.BUY,
        price=101,
        quantity=8,
        symbol=SYMBOL,
    )
    result_b = engine.execute_limit_order(
        trader_id="trader_b",
        side=Side.SELL,
        price=101,
        quantity=8,
        symbol=SYMBOL,
    )
    _apply_trades(positions, result_b.trades)

    # Build a deterministic, uncrossed top-of-book for session-end mark.
    engine.execute_limit_order(
        trader_id="mark_bid",
        side=Side.BUY,
        price=99,
        quantity=2,
        symbol=SYMBOL,
    )
    engine.execute_limit_order(
        trader_id="mark_ask",
        side=Side.SELL,
        price=103,
        quantity=2,
        symbol=SYMBOL,
    )
    best_bid = engine.best_bid()
    best_ask = engine.best_ask()
    assert best_bid == 99
    assert best_ask == 103
    mark_price = (best_bid + best_ask) / 2.0

    # Session-expiry flow: cancel restings, flatten positions at mark, emit rankings.
    engine.clear_order_book()
    flattened = session.force_close_positions(positions=positions, mark_price=mark_price)
    assert flattened
    for snapshot in flattened:
        assert snapshot["position"] == 0
        assert snapshot["unrealized_pnl"] == 0.0

    leaderboard = positions.get_leaderboard()
    session_end_event = session.end_session(rankings=leaderboard, mark_price=mark_price)
    assert session_end_event["type"] == "session_end"
    assert session_end_event["round"] == 1
    assert session_end_event["rankings"]

    # Full reset between rounds.
    session.reset_exchange_state(engine=engine, positions=positions)
    assert engine.best_bid() is None
    assert engine.best_ask() is None
    assert positions.get_all_positions() == []

    # Round 2 starts from fresh state and IDs restart.
    session_start_2 = session.start_session(now_monotonic=61.0)
    assert session_start_2["round"] == 2

    engine.execute_limit_order(
        trader_id="r2_ask",
        side=Side.SELL,
        price=100,
        quantity=1,
        symbol=SYMBOL,
    )
    result_r2 = engine.execute_limit_order(
        trader_id="r2_bid",
        side=Side.BUY,
        price=100,
        quantity=1,
        symbol=SYMBOL,
    )
    assert result_r2.trades
    assert result_r2.trades[0].trade_id == 1
