# Architecture

## Components

### 1) Exchange Server (`exchange_server.py`)
Responsibilities:
- accepts orders on WebSocket order gateway
- validates inbound messages
- performs deterministic matching (FIFO price-time)
- updates position and realized PnL state
- runs margin checks and liquidation flow
- emits internal events (`trade`, `book_update`, `position_update`, `liquidation`)
- forwards emitted events to a local CSV sink

What it does not do:
- no strategy logic
- no UI rendering
- no downstream broadcast formatting logic beyond event emission

### 2) Market Data Server (`market_data_server.py`)
Responsibilities:
- subscribes to exchange internal event stream
- rebroadcasts raw events to all connected clients
- reconnects to upstream exchange feed

What it does not do:
- no matching
- no risk checks
- no position tracking

### 3) Bot Client (`bot_client.py`)
Responsibilities:
- subscribes to market data feed
- converts strategy output into order messages
- sends orders to exchange gateway
- handles order accept/reject responses

What it does not do:
- no exchange-side state mutation
- no direct memory access to exchange internals

### 4) Strategy Framework (`bot_strategies.py`)
Responsibilities:
- defines strategy interface (`next_order(context)`)
- provides built-in demo strategies (`maker`, `taker`, `random`)
- loads custom strategy classes via `module:Class`

What it does not do:
- no execution logic in exchange process

### 5) Monitor Client (`monitor_client.py`)
Responsibilities:
- read-only feed subscriber
- renders order book/trade metrics
- consumes `position_update` and market data events for PnL display
- indicates connectivity (`CONNECTED` / `DISCONNECTED`)

What it does not do:
- never sends orders
- never simulates exchange matching

### 6) CSV Exporter (`exporter.py`)
Responsibilities:
- buffers `trade` and `position_update` events in memory
- appends rows to `trades.csv` and `performance.csv`
- flushes every 500ms on a background async task

What it does not do:
- no matching, risk, or position mutation
- no network or broadcast responsibilities

## Event Flow (ASCII)

```text
                +------------------------------------+
                |          Exchange Server           |
                | matching + positions + risk        |
                | ws://127.0.0.1:9001 (orders)       |
                | ws://127.0.0.1:9002 (events out)   |
                +-------------------+----------------+
                                    |
                     +--------------+--------------+
                     |                             |
                     | internal event stream       | buffered event sink
                     v                             v
                +------------------------------------+   +----------------------+
                |        Market Data Server          |   |     CSV Exporter     |
                | stateless relay                    |   | trades/performance   |
                | ws://127.0.0.1:9010 (broadcast)    |   | local append-only    |
                +-----------+----------------+--------+   +----------------------+
                            |                |
                            |                |
                            v                v
                 +----------------+   +------------------+
                 | Bot Client(s)  |   | Monitor Client   |
                 | strategy plugin|   | read-only        |
                 +----------------+   +------------------+
                            |
                            | orders
                            +--------------------------> ws://127.0.0.1:9001
```

## Data Flow
1. Bot receives market events from `ws://127.0.0.1:9010`.
2. Strategy returns next order decision.
3. Bot sends order to exchange gateway `ws://127.0.0.1:9001`.
4. Exchange validates, matches, updates state, emits events.
5. Exchange buffers supported events for CSV persistence (`trade`, `position_update`).
6. Market data server relays events to bots and monitor.

## Separation of Concerns
- Exchange state and matching are centralized in one process.
- Market data relay is isolated and stateless.
- Strategy execution is isolated in bot processes.
- Monitoring is read-only and does not influence execution.

This prevents coupling between strategy code and exchange state.

## Why WebSocket-Based
- low-overhead bidirectional transport
- suitable for streaming book/trade updates
- easy local-to-remote endpoint migration
- uniform protocol across components

## Why Independent Bot Processes
- failure isolation (one bot crash does not stop exchange)
- no shared-memory side effects
- realistic deployment model
- independent strategy lifecycle and resource tuning
