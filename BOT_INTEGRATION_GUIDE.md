# Bot Integration Guide

## Purpose
This guide explains how strategy developers integrate custom strategy modules with the local exchange infrastructure.

The exchange remains infrastructure-only. Strategy code runs in bot processes.

## Integration Model

Your strategy bot should:
1. subscribe to market data (`ws://127.0.0.1:9010`)
2. consume feed messages
3. produce orders
4. submit orders to exchange gateway (`ws://127.0.0.1:9001`)

No direct exchange memory access is available.

## Strategy Interface

Custom strategy classes are loaded with:
- `--strategy module_name:ClassName`

Required constructor signature:

```python
def __init__(self, *, trader_id: str, rng: random.Random, params: dict[str, str]) -> None:
    ...
```

Required decision method:

```python
def next_order(self, context: StrategyContext) -> OrderRequest | None:
    ...
```

`None` means skip this decision cycle.

## Strategy Context

`StrategyContext` includes:
- `trader_id`
- `best_bid`
- `best_ask`
- `mid_price`
- `spread`
- `timestamp`

## Minimal Custom Strategy Example

```python
import random
from bot_strategies import StrategyContext
from message_schemas import OrderRequest, OrderType, Side

class MyStrategy:
    def __init__(self, *, trader_id: str, rng: random.Random, params: dict[str, str]) -> None:
        self.trader_id = trader_id
        self.rng = rng
        self.qty = int(params.get("qty", "1"))

    def next_order(self, context: StrategyContext) -> OrderRequest | None:
        if context.best_bid is None and context.best_ask is None:
            return None
        side = Side.BUY if self.rng.random() < 0.5 else Side.SELL
        price = context.best_bid if side == Side.BUY else context.best_ask
        return OrderRequest(
            trader_id=self.trader_id,
            side=side,
            order_type=OrderType.LIMIT,
            price=price,
            qty=self.qty,
        )
```

## Run a Single Custom Bot

```powershell
python bot_client.py ^
  --trader-id my_bot ^
  --strategy my_strategy_module:MyStrategy ^
  --strategy-param qty=2 ^
  --market-data-uri ws://127.0.0.1:9010 ^
  --order-gateway-uri ws://127.0.0.1:9001
```

## Launch Multiple Bots from Config

Use `bot_battle_runner.py` with `bots_config.json`.

```powershell
python bot_battle_runner.py --config bots_config.json
```

Config shape:

```json
{
  "market_data_uri": "ws://127.0.0.1:9010",
  "order_gateway_uri": "ws://127.0.0.1:9001",
  "bots": [
    {
      "trader_id": "maker_1",
      "strategy": "maker",
      "seed": 101,
      "decision_interval": 0.55,
      "strategy_params": { "qty": "2" }
    }
  ]
}
```

## Built-In Demo Strategies

For infrastructure demos:
- `maker`
- `taker`
- `random`

These are demonstration strategies only. Production strategy logic should live in your own modules.

## Migration Path to External Venues

Your strategy logic can be preserved while changing transport targets:
- replace local `--order-gateway-uri`
- replace local `--market-data-uri`
- adapt wire protocol mapping as needed

This allows local research first, external integration later.

## Research Sandbox Positioning

This platform is intended for:
- local execution research
- queue/fill behavior testing
- multi-bot interaction experiments
- integration testing for strategy runtime

It is not intended to execute real capital.
