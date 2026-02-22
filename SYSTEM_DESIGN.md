# System Design

## 1. Overview
The system is a distributed local exchange composed of independent async processes communicating over WebSocket:
- exchange core
- market data relay
- strategy bots
- read-only monitor

The design goal is realism in execution behavior with strict separation between infrastructure and strategy logic.

## 2. Matching Engine Design

### Core Data Structures
- bid side:
  - `bid_levels: dict[price, deque[order]]`
  - `bid_prices_desc: list[price]` (sorted high -> low)
- ask side:
  - `ask_levels: dict[price, deque[order]]`
  - `ask_prices_asc: list[price]` (sorted low -> high)

Each price level stores a FIFO queue (`deque`) of resting orders.

### Price-Time Priority
- price priority first (best bid / best ask)
- time priority second (queue order at same price)

Matching scans top levels only, consuming resting orders in queue order.

### Partial Fill Handling
- fill quantity = `min(incoming_remaining, resting_remaining)`
- reduce both sides
- remove resting order when quantity reaches zero
- remove price level when queue becomes empty
- if incoming limit has remaining quantity, rest residual at its limit price

### Determinism Guarantees
- no concurrent matching execution (single lock around state mutation)
- stable sorted ladders for level traversal
- FIFO queue semantics at each level
- post-match crossed-book invariant assertion (`best_bid < best_ask`)

## 3. Position Engine

Per trader state:
- position
- cash
- average entry price
- realized PnL
- last trade price

Update model:
- both buyer and seller legs are applied per trade
- average entry uses weighted averaging when increasing same-direction exposure
- realized PnL is booked when reducing/closing/reversing positions

## 4. Mark-to-Mid PnL

Monitor computes mark from feed:
- if both sides available: `mark = (best_bid + best_ask) / 2`
- else fallback to last trade or single-sided quote

Derived unrealized PnL:
- `unrealized = (mark - avg_entry) * position`

Total PnL:
- `total = realized + unrealized`

This derived PnL is observer-side and does not mutate exchange state.

## 5. Risk and Liquidation

Pre-trade:
- initial margin check on projected position exposure

Post-trade:
- maintenance check on active positions
- deterministic liquidation order generation when breached
- bounded liquidation loop with stop conditions

Additional guardrail:
- market order rejected with `no_liquidity` when no immediate match is possible

## 6. Async Architecture

### Exchange Process
- WebSocket order gateway handler
- WebSocket internal event subscriber handler
- async event dispatcher loop
- async CSV exporter flush loop (500ms cadence)

### Market Data Process
- upstream subscriber loop to exchange stream
- downstream broadcast server to clients
- reconnect loop for upstream disruptions

### Bot Process
- market-data consume loop
- order-response consume loop
- periodic strategy decision/order loop
- automatic reconnect on socket failure

### Monitor Process
- feed receiver loop with reconnect
- render loop at fixed refresh interval
- explicit connectivity state rendering

## 7. Reconnection Handling

- market data server retries upstream connection with delay
- bot client reconnects both feed and order channels
- monitor reconnects feed and surfaces disconnect reason

No process assumes persistent connectivity.

## 8. Failure Isolation

- exchange state is isolated from bot crashes
- each bot runs in its own process
- market data relay failure does not corrupt exchange state
- monitor is read-only and cannot influence matching

This supports robust local testing under partial process failure scenarios.

## 9. Separation Boundaries

- exchange: execution + state + risk only
- market data: relay only
- bots: strategy + routing only
- monitor: observation only

These boundaries are enforced through process isolation and WebSocket interfaces.

## 10. Operational Notes

- all processes support graceful `Ctrl+C` shutdown
- all interfaces are local by default but URI-configurable
- strategy swap does not require exchange code modification

## 11. Event Export Pipeline

The exchange includes a local persistence sink (`exporter.py`) for audit-style event capture.

- ingestion point: emitted exchange events before queue dispatch
- captured event types: `trade`, `position_update`
- persistence targets:
  - `trades.csv` (`timestamp,price,qty,buy_trader,sell_trader`)
  - `performance.csv` (`timestamp,trader_id,position,cash,realized_pnl,total_equity`)
- write mode: append-only, header auto-create
- scheduling: buffered flush every 500ms in background task
- hot path safety: disk I/O offloaded via `asyncio.to_thread`

This enables production-style local infra workflows for strategy development and validation.
