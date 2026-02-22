# File: tests/test_phase1_snippet.py

from engine import MatchingEngine
from models import Side


def test_self_match_prevention_skips_own_resting_order() -> None:
    engine = MatchingEngine(debug=True)

    # Trader A posts two asks at the same price level.
    engine.place_limit_order(trader_id="A", side=Side.SELL, price=100, quantity=2)
    engine.place_limit_order(trader_id="A", side=Side.SELL, price=100, quantity=3)

    # Trader A then sends a crossing buy. SMP prevents matching against own asks.
    trades = engine.place_limit_order(trader_id="A", side=Side.BUY, price=101, quantity=4)
    assert trades == []

    snapshot = engine.get_book_snapshot(depth=5)
    # Original asks remain untouched; incoming crossing buy is not rested to
    # preserve uncrossed-book invariant under SMP.
    assert snapshot["asks"] == [(100, 5)]
    assert snapshot["bids"] == []


def test_book_snapshot_aggregates_and_limits_depth() -> None:
    engine = MatchingEngine(debug=True)

    # Build bid ladder with multiple orders per level to verify aggregation.
    engine.place_limit_order(trader_id="B1", side=Side.BUY, price=99, quantity=2)
    engine.place_limit_order(trader_id="B2", side=Side.BUY, price=99, quantity=3)
    engine.place_limit_order(trader_id="B3", side=Side.BUY, price=98, quantity=4)
    engine.place_limit_order(trader_id="B4", side=Side.BUY, price=97, quantity=5)

    # Build ask ladder.
    engine.place_limit_order(trader_id="S1", side=Side.SELL, price=101, quantity=1)
    engine.place_limit_order(trader_id="S2", side=Side.SELL, price=102, quantity=2)
    engine.place_limit_order(trader_id="S3", side=Side.SELL, price=103, quantity=3)

    snapshot = engine.get_book_snapshot(depth=2)
    assert snapshot["bids"] == [(99, 5), (98, 4)]
    assert snapshot["asks"] == [(101, 1), (102, 2)]
