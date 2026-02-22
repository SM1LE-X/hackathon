# Demo Guide

This guide is optimized for a live demo or judging session.

## Goal
Show a realistic local exchange infrastructure run where independent bots trade through WebSocket endpoints and a read-only monitor displays live market behavior.

## Terminal Setup

### Terminal 1 - Exchange Core
```powershell
python exchange_server.py
```

Expected:
- order gateway started (`ws://127.0.0.1:9001`)
- internal event stream started (`ws://127.0.0.1:9002`)
- local CSV writer active (`trades.csv`, `performance.csv`)

### Terminal 2 - Market Data Relay
```powershell
python market_data_server.py
```

Expected:
- upstream connection to exchange event stream
- broadcast server started (`ws://127.0.0.1:9010`)

### Terminal 3 - Bot Battle
```powershell
python bot_battle_runner.py --config bots_config.json
```

Expected:
- multiple bot processes launched
- bots connecting to market data + order gateway
- intermittent `order_accepted` and risk/liquidity rejections

### Terminal 4 - Read-Only Monitor
```powershell
python monitor_client.py
```

Expected:
- status line shows `CONNECTED`
- top-of-book levels moving
- trade count increasing
- per-trader position/cash/realized/unrealized/total PnL changing

## What Judges Should See

1. **Live execution environment**
   - continuous trade and book updates
   - changing queue state and spread behavior

2. **Infrastructure separation**
   - exchange runs independently from bots
   - market data relay is a separate process
   - monitor is subscriber-only

3. **Independent strategy processes**
   - multiple bots with different strategy configurations
   - no shared memory shortcuts

4. **Operational resilience**
   - monitor shows `CONNECTED / DISCONNECTED`
   - reconnect behavior if feed path is interrupted

5. **Persistent telemetry export**
   - `trades.csv` grows as executions occur
   - `performance.csv` grows as `position_update` events are emitted

## Optional Live Checks

- Temporarily stop market data server and restart it:
  - monitor should switch to `DISCONNECTED` then return to `CONNECTED`
  - bots should reconnect and continue

- Modify bot strategy mix in `bots_config.json`:
  - restart terminal 3
  - observe behavior changes without exchange modifications

- Confirm CSV append behavior:
  - keep exchange running
  - verify rows keep increasing in `trades.csv` / `performance.csv`

## Demo Closing Statement

This is a local exchange infrastructure where strategy developers can deploy independent bots and test execution logic safely before connecting to external venues.
