# File: tests/test_phase4_margin_risk_snippet.py

from engine import MatchingEngine
from margin_risk_manager import MarginRiskManager
from models import SYMBOL, Side
from positions import PositionManager


def test_scenario_1_leveraged_position_within_margin_is_valid() -> None:
    risk = MarginRiskManager()

    # Flat trader with starting capital baseline.
    snapshot = {
        "trader_id": "trader_1",
        "position": 0,
        "cash": 0.0,
        "unrealized_pnl": 0.0,
    }
    # Projected notional = 50 * 100 = 5000; required initial margin = 1000.
    order = {
        "type": "place_order",
        "side": "BUY",
        "price": 100,
        "quantity": 50,
    }
    ok, rejection = risk.validate_initial_margin("trader_1", order, snapshot)
    assert ok is True
    assert rejection is None


def test_scenario_2_excessive_exposure_rejected_pre_trade() -> None:
    risk = MarginRiskManager()

    snapshot = {
        "trader_id": "trader_2",
        "position": 0,
        "cash": 0.0,
        "unrealized_pnl": 0.0,
    }
    # Projected notional = 600 * 100 = 60000; required initial margin = 12000 > equity 10000.
    order = {
        "type": "place_order",
        "side": "BUY",
        "price": 100,
        "quantity": 600,
    }
    ok, rejection = risk.validate_initial_margin("trader_2", order, snapshot)
    assert ok is False
    assert rejection is not None
    assert rejection["reason"] == "initial_margin_insufficient"
    assert rejection["required_margin"] == 12000.0
    assert rejection["equity"] == 10000.0


def test_scenario_3_maintenance_breach_triggers_progressive_liquidation() -> None:
    risk = MarginRiskManager()
    engine = MatchingEngine()
    positions = PositionManager()

    # Build leveraged long exposure: trader buys 90 @ 100.
    engine.execute_limit_order(
        trader_id="maker_open",
        side=Side.SELL,
        price=100,
        quantity=90,
        symbol=SYMBOL,
    )
    entry_result = engine.execute_limit_order(
        trader_id="trader_3",
        side=Side.BUY,
        price=100,
        quantity=90,
        symbol=SYMBOL,
    )
    assert sum(trade.quantity for trade in entry_result.trades) == 90
    for trade in entry_result.trades:
        positions.update_from_trade(trade)

    # Provide deterministic top-of-book for mark and executable liquidation.
    # mark_price = (94 + 96) / 2 = 95.
    engine.execute_limit_order(
        trader_id="maker_bid",
        side=Side.BUY,
        price=94,
        quantity=500,
        symbol=SYMBOL,
    )
    engine.execute_limit_order(
        trader_id="maker_ask",
        side=Side.SELL,
        price=96,
        quantity=500,
        symbol=SYMBOL,
    )

    # Mark drops from entry 100 to 95, creating a maintenance breach:
    # equity = 550, maintenance = 855.
    snapshot = positions.get_position_snapshot_for_risk("trader_3")
    breach, details = risk.check_maintenance(
        trader_id="trader_3",
        position_snapshot=snapshot,
        mark_price=95.0,
    )
    assert breach is True
    assert details["equity"] < details["maintenance_requirement"]

    first_liquidation_order: dict[str, int | str] | None = None
    liquidation_steps = 0

    # Progressive deterministic liquidation:
    # while breach and position != 0, sell chunks through the matching engine.
    while liquidation_steps < 200:
        trader_snapshot = positions.get_position_snapshot_for_risk("trader_3")
        breach, _ = risk.check_maintenance(
            trader_id="trader_3",
            position_snapshot=trader_snapshot,
            mark_price=95.0,
        )
        if not breach or trader_snapshot["position"] == 0:
            break

        liquidation_order, liquidation_event = risk.perform_liquidation(
            trader_id="trader_3",
            position_snapshot=trader_snapshot,
            best_bid=engine.best_bid(),
            best_ask=engine.best_ask(),
        )
        assert liquidation_event["type"] == "liquidation"
        assert liquidation_order is not None

        if first_liquidation_order is None:
            first_liquidation_order = liquidation_order

        liquidation_result = engine.execute_limit_order(
            trader_id="trader_3",
            side=Side(liquidation_order["side"]),
            price=int(liquidation_order["price"]),
            quantity=int(liquidation_order["quantity"]),
            symbol=SYMBOL,
        )
        assert liquidation_result.trades
        for trade in liquidation_result.trades:
            positions.update_from_trade(trade)

        liquidation_steps += 1

    assert liquidation_steps > 1
    # target_position = floor(550 / (95 * 0.20)) = 28 => liquidation_qty = 62
    assert first_liquidation_order == {"side": "SELL", "price": 94, "quantity": 62}

    final_snapshot = positions.get_position_snapshot_for_risk("trader_3")
    final_breach, _ = risk.check_maintenance(
        trader_id="trader_3",
        position_snapshot=final_snapshot,
        mark_price=95.0,
    )
    assert final_snapshot["position"] == 28
    assert final_breach is False
