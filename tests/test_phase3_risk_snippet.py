# File: tests/test_phase3_risk_snippet.py

from risk_manager import RiskManager


def test_reject_insufficient_cash_buy_200_at_100() -> None:
    risk = RiskManager()
    snapshot = {
        "trader_id": "trader_1",
        "position": 0,
        "cash": 0.0,
    }
    order = {
        "type": "place_order",
        "side": "BUY",
        "price": 100,
        "quantity": 200,
    }
    ok, reason = risk.validate_order("trader_1", order, snapshot)
    assert ok is False
    assert reason == "insufficient_cash"


def test_reject_exceeds_position_limit() -> None:
    risk = RiskManager()
    snapshot = {
        "trader_id": "trader_2",
        "position": 0,
        "cash": 0.0,
    }
    order = {
        "type": "place_order",
        "side": "SELL",
        "price": 90,
        "quantity": 51,
    }
    ok, reason = risk.validate_order("trader_2", order, snapshot)
    assert ok is False
    assert reason == "position_limit"


def test_valid_trade_passes_risk() -> None:
    risk = RiskManager()
    snapshot = {
        "trader_id": "trader_3",
        "position": 0,
        "cash": 0.0,
    }
    order = {
        "type": "place_order",
        "side": "BUY",
        "price": 100,
        "quantity": 10,
    }
    ok, reason = risk.validate_order("trader_3", order, snapshot)
    assert ok is True
    assert reason is None
