# OpenMarketSim: Project Overview

## Purpose

OpenMarketSim is a local exchange infrastructure for strategy developers.

It lets developers:
- connect strategy bots over WebSocket
- test order routing and execution behavior
- validate book/trade/PnL outcomes in a safe environment

It is not a brokerage, not a production venue, and not a strategy service.

## Core Design Goals

- Deterministic matching behavior
- Clean process boundaries
- Realistic execution semantics
- Simple local deployment
- Strategy plug-in compatibility

## Process Model

### Exchange Server
- Owns matching, positions, risk, and liquidation
- Accepts orders via WebSocket
- Emits trade/book/position/liquidation events
- Persists `trade` and `position_update` telemetry to CSV

### Market Data Server
- Subscribes to exchange event stream
- Rebroadcasts to all feed consumers
- Stateless and infra-only

### Bot Clients
- Run strategy code in separate processes
- Consume feed and submit orders
- No exchange memory access

### Monitor Client
- Read-only observer
- Computes local analytics from feed events
- Never sends orders

## Exchange Behavior

- FIFO price-time priority
- Partial fills
- Limit and market orders
- Risk checks before execution
- Market order rejection when no liquidity
- Deterministic liquidation loop for maintenance breaches

## Developer Workflow

1. Start `exchange_server.py`
2. Start `market_data_server.py`
3. Start `bot_battle_runner.py --config bots_config.json`
4. Start `monitor_client.py`
5. Add or swap custom strategies in bot processes

Detailed runbook: `WALKTHROUGH.md`

## Key Files

- `exchange_server.py`
- `market_data_server.py`
- `exporter.py`
- `message_schemas.py`
- `bot_client.py`
- `bot_strategies.py`
- `bot_battle_runner.py`
- `monitor_client.py`
- `bots_config.json`
