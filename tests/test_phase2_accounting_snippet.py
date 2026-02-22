# File: tests/test_phase2_accounting_snippet.py

from models import Side, Trade
from positions import PositionManager


def _trade(
    *,
    trade_id: int,
    price: int,
    quantity: int,
    maker_trader_id: str,
    taker_trader_id: str,
    aggressor_side: Side,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        symbol="TEST",
        price=price,
        quantity=quantity,
        maker_order_id=trade_id * 10,
        taker_order_id=trade_id * 10 + 1,
        maker_trader_id=maker_trader_id,
        taker_trader_id=taker_trader_id,
        aggressor_side=aggressor_side,
        sequence=trade_id,
    )


def test_phase2_example_buy_then_partial_sell() -> None:
    manager = PositionManager()

    # Trader A buys 10 @ 100 (A is taker BUY; B is maker SELL).
    t1 = _trade(
        trade_id=1,
        price=100,
        quantity=10,
        maker_trader_id="B",
        taker_trader_id="A",
        aggressor_side=Side.BUY,
    )
    manager.update_from_trade(t1)

    # Trader A sells 5 @ 110 (A is taker SELL; C is maker BUY).
    t2 = _trade(
        trade_id=2,
        price=110,
        quantity=5,
        maker_trader_id="C",
        taker_trader_id="A",
        aggressor_side=Side.SELL,
    )
    manager.update_from_trade(t2)

    snapshot = manager.get_position_snapshot("A")
    assert snapshot["position"] == 5
    assert snapshot["cash"] == -450.0
    assert snapshot["avg_entry_price"] == 100.0
    assert snapshot["realized_pnl"] == 50.0
    assert snapshot["unrealized_pnl"] == 50.0
    assert snapshot["total_pnl"] == 100.0
