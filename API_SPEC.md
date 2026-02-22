# WebSocket API Specification

## Endpoints

- Exchange Order Gateway: `ws://127.0.0.1:9001`
- Exchange Internal Event Stream (infrastructure): `ws://127.0.0.1:9002`
- Market Data Broadcast Feed: `ws://127.0.0.1:9010`

## Message Conventions

- Encoding: JSON
- Timestamps: Unix epoch milliseconds (`timestamp`)
- Prices: numeric (`float` semantics)
- Quantity: integer shares/contracts in current implementation (`qty`)

## Client -> Exchange

### Order Placement (Current Canonical Format)

```json
{
  "type": "order",
  "trader_id": "maker_1",
  "side": "buy",
  "order_type": "limit",
  "price": 100.05,
  "qty": 2,
  "client_order_id": "maker_1-001"
}
```

Field definitions:
- `type`: must be `"order"`
- `trader_id`: logical trader identity string
- `side`: `"buy"` or `"sell"`
- `order_type`: `"limit"` or `"market"`
- `price`: required for limit, must be null/omitted for market
- `qty`: positive integer quantity
- `client_order_id`: optional client correlation id

### Requested External Shape (Mapping)

If a developer uses this shape:

```json
{
  "type": "place_order",
  "trader_id": "...",
  "side": "BUY",
  "price": 100.05,
  "quantity": 2
}
```

Map it to canonical:
- `type: "place_order"` -> `"order"`
- `side: "BUY"/"SELL"` -> `"buy"/"sell"`
- `quantity` -> `qty`
- add `order_type` (`"limit"` if `price` provided, else `"market"`)

## Exchange -> Order Client Responses

### Order Accepted

```json
{
  "type": "order_accepted",
  "order_id": 42,
  "trader_id": "maker_1",
  "client_order_id": "maker_1-001",
  "timestamp": 1739980000000
}
```

### Order Rejected

```json
{
  "type": "order_rejected",
  "reason": "initial_margin_insufficient",
  "details": {
    "equity": 9500.0,
    "required_margin": 10000.0
  },
  "trader_id": "maker_1",
  "client_order_id": "maker_1-001",
  "timestamp": 1739980000001
}
```

Common `reason` values:
- `invalid_json`
- `invalid_message`
- `initial_margin_insufficient`
- `invalid_price_reference`
- `no_liquidity`

## Exchange Events (Broadcast via Market Data Server)

### Book Update

```json
{
  "type": "book_update",
  "best_bid": 100.0,
  "best_ask": 100.1,
  "bids": [[100.0, 5], [99.95, 3]],
  "asks": [[100.1, 4], [100.15, 2]],
  "timestamp": 1739980000100
}
```

Field definitions:
- `best_bid` / `best_ask`: top-of-book prices or `null`
- `bids` / `asks`: depth arrays `[price, total_qty_at_level]`
- `timestamp`: event generation time

### Trade Event

```json
{
  "type": "trade",
  "trade_id": 101,
  "price": 100.1,
  "qty": 2,
  "buy_trader_id": "taker_1",
  "sell_trader_id": "maker_1",
  "timestamp": 1739980000101
}
```

Field definitions:
- `trade_id`: exchange-generated monotonic id
- `price`: execution price
- `qty`: filled quantity
- `buy_trader_id` / `sell_trader_id`: trade counterparties
- `timestamp`: execution event time

### Liquidation Event

```json
{
  "type": "liquidation",
  "trader_id": "taker_1",
  "reason": "maintenance_margin_breach",
  "qty": 3,
  "side": "sell",
  "timestamp": 1739980000200
}
```

### Position Update

```json
{
  "type": "position_update",
  "trader_id": "maker_1",
  "position": 3,
  "cash": 9700.0,
  "avg_entry_price": 99.8,
  "realized_pnl": 12.5,
  "unrealized_pnl": 4.2,
  "total_equity": 9716.7,
  "mark_price": 100.05,
  "timestamp": 1739980000300
}
```

Field definitions:
- `trader_id`: trader account identifier
- `position`: signed net position
- `cash`: post-trade cash balance
- `avg_entry_price`: weighted average entry for open inventory
- `realized_pnl`: closed PnL from executed trades
- `unrealized_pnl`: mark-to-mid PnL at event time
- `total_equity`: `cash + unrealized_pnl`
- `mark_price`: reference mark used for unrealized/equity
- `timestamp`: event generation time

## Local CSV Export Artifacts

The exchange also persists a local audit trail through `exporter.py`.

- `trades.csv` fields:
  - `timestamp,price,qty,buy_trader,sell_trader`
- `performance.csv` fields:
  - `timestamp,trader_id,position,cash,realized_pnl,total_equity`

Notes:
- append-only writes
- headers auto-created when files are absent
- buffered flush every 500ms
