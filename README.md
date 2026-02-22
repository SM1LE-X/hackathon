# Local Exchange Infrastructure for Strategy Developers

## Project Overview
This repository provides a local, headless exchange infrastructure for testing execution logic in a controlled environment.

It is not a bot framework and it does not contain strategy logic in the exchange process.

## Why This Exists
Strategy developers need a realistic but safe environment to validate:
- order routing
- queue priority behavior
- fill quality
- inventory impact
- PnL and risk-side effects

This system offers exchange-like behavior locally, without live market risk.

## Who This Is For
- quant researchers prototyping execution logic
- strategy developers building custom bots
- infrastructure engineers validating market connectivity paths
- teams running local integration tests before external deployment

## System Components
- `exchange_server.py`: matching engine, positions, risk, liquidation, order gateway
- `market_data_server.py`: stateless WebSocket relay for market events
- `bot_client.py`: strategy wrapper client (plugin-based)
- `bot_strategies.py`: built-in demo strategies + plugin loader
- `bot_battle_runner.py`: multi-bot launcher from JSON config
- `monitor_client.py`: read-only monitor (book, trades, derived PnL)
- `exporter.py`: buffered CSV event exporter (`trade`, `position_update`)
- `message_schemas.py`: shared protocol validation and message models

## Quick Start
1. Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

2. Start services:

```powershell
# Terminal 1
python exchange_server.py

# Terminal 2
python market_data_server.py

# Terminal 3
python bot_battle_runner.py --config bots_config.json

# Terminal 4
python monitor_client.py
```

## Demo Instructions
Use the same 4-terminal flow above. In the monitor you should see:
- moving top-of-book
- trade count increasing
- per-trader position/cash/PnL changing
- `CONNECTED / DISCONNECTED` connectivity state

Detailed script: `DEMO_GUIDE.md`

## CSV Export
The exchange writes two append-only CSV files in the current working directory:
- `trades.csv`: `timestamp,price,qty,buy_trader,sell_trader`
- `performance.csv`: `timestamp,trader_id,position,cash,realized_pnl,total_equity`

Behavior:
- headers are auto-created if files do not exist
- rows are buffered in-memory and flushed every 500ms
- flushing is asynchronous and does not block matching

## Architecture Summary
- Exchange is the source of truth for matching, state, and risk.
- Market data process only relays events.
- Bots are independent processes and communicate only over WebSocket.
- Monitor is subscriber-only and never submits orders.

Details: `ARCHITECTURE.md` and `SYSTEM_DESIGN.md`

## Key Design Principles
- strict separation of concerns
- deterministic matching behavior
- process-level isolation
- schema-first message validation
- async, reconnect-capable networking
- no shared memory across processes

## Folder Structure
```text
.
|-- exchange_server.py
|-- market_data_server.py
|-- message_schemas.py
|-- bot_client.py
|-- bot_strategies.py
|-- bot_battle_runner.py
|-- bots_config.json
|-- monitor_client.py
|-- exporter.py
|-- README.md
|-- ARCHITECTURE.md
|-- API_SPEC.md
|-- BOT_INTEGRATION_GUIDE.md
|-- SYSTEM_DESIGN.md
`-- DEMO_GUIDE.md
```
