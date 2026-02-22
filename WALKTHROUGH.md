# Walkthrough: Run a Local Strategy-vs-Strategy Session

This walkthrough launches the full local exchange stack and helps you validate that your strategy bots are connected correctly.

## 1) Prerequisites

- Python 3.11+
- Dependencies installed:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 2) Start the Exchange Server (Terminal 1)

```powershell
python exchange_server.py
```

Verify logs show:
- order gateway on `ws://127.0.0.1:9001`
- internal event stream on `ws://127.0.0.1:9002`
- CSV export pipeline active (writes `trades.csv` and `performance.csv`)

## 3) Start Market Data Relay (Terminal 2)

```powershell
python market_data_server.py
```

Verify it connects to upstream exchange stream `ws://127.0.0.1:9002` and starts feed server `ws://127.0.0.1:9010`.

## 4) Launch Bot Battle (Terminal 3)

```powershell
python bot_battle_runner.py --config bots_config.json
```

This starts multiple bot processes from config. Each bot:
- subscribes to `ws://127.0.0.1:9010`
- submits orders to `ws://127.0.0.1:9001`

## 5) Start Read-Only Monitor (Terminal 4)

```powershell
python monitor_client.py
```

You should see:
- `STATUS: CONNECTED`
- live top-of-book updates
- live trade count and last trade
- per-trader position, cash, realized/unrealized/total PnL

## 6) Validate CSV Export

After trades occur, verify local files:

```powershell
Get-Content .\trades.csv -TotalCount 5
Get-Content .\performance.csv -TotalCount 5
```

Expected headers:
- `trades.csv`: `timestamp,price,qty,buy_trader,sell_trader`
- `performance.csv`: `timestamp,trader_id,position,cash,realized_pnl,total_equity`

## 7) Plug In Your Own Strategy

Run one custom bot directly:

```powershell
python bot_client.py --trader-id my_bot --strategy my_module:MyStrategy --strategy-param qty=2
```

Required custom strategy methods:
- `__init__(self, trader_id, rng, params)`
- `next_order(self, context) -> OrderRequest | None`

## 8) Common Checks

- If monitor shows `DISCONNECTED`, confirm market data relay is running.
- If bots get rejections, inspect order schema and risk constraints.
- If market orders reject with `no_liquidity`, there are no opposite resting orders.

## 9) Stop Cleanly

Press `Ctrl+C` in this order:
1. bot battle runner
2. monitor client
3. market data server
4. exchange server
