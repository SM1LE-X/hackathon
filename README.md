# OpenMarketSim

OpenMarketSim is a deterministic local exchange simulator for testing trading infrastructure, execution logic, and tournament-style workflows without live market risk.

It includes:
- a distributed exchange stack (order gateway + market data relay + multi-bot runner + monitor)
- a tournament arena stack (session rounds over a single server endpoint)
- CLI and Textual UIs

## Highlights

- Deterministic FIFO price-time matching.
- Position, PnL, margin, and liquidation flows.
- WebSocket-based components with process isolation.
- Strategy plugin support via `module:Class`.
- Local telemetry export to `trades.csv` and `performance.csv`.
- Pytest coverage for engine/risk/session/tournament snippets.

## Repository Layout

- `models.py`: shared enums/dataclasses and protocol parsing.
- `orderbook.py`, `engine.py`: matching and book behavior.
- `positions.py`, `risk_manager.py`, `margin_risk_manager.py`: accounting and risk checks.
- `server.py`, `session_manager.py`, `tournament_manager.py`: round/session tournament runtime.
- `exchange_server.py`, `market_data_server.py`: distributed exchange + market data relay.
- `bot_client.py`, `bot_strategies.py`, `bot_battle_runner.py`: multi-bot strategy runtime.
- `monitor_client.py`, `arena_cli.py`, `arena_textual_app.py`: terminal and Textual monitoring/UI.
- `tests/`: pytest test suite (`test_phase*_snippet.py`).
- `web-dashboard/`: React + Vite dashboard for market-data feed.

## Prerequisites

- Python 3.10+
- `pip`
- (Optional) Node.js 18+ for `web-dashboard/`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Run Options

### 1) Distributed Exchange Stack (Recommended)

Start each in a separate terminal:

```powershell
# Terminal 1: exchange core
python exchange_server.py

# Terminal 2: market data relay
python market_data_server.py

# Terminal 3: launch bots from config
python bot_battle_runner.py --config bots_config.json

# Terminal 4: read-only monitor
python monitor_client.py
```

Default endpoints:
- Order gateway: `ws://127.0.0.1:9001`
- Internal event stream: `ws://127.0.0.1:9002`
- Broadcast market feed: `ws://127.0.0.1:9010`

### 2) Tournament Arena Stack

Run tournament server:

```powershell
python arena_tournament.py --rounds 5 --duration 60
```

Connect terminal arena UI:

```powershell
python arena_cli.py --uri ws://localhost:8000
```

Optional sample bot for this stack:

```powershell
python bot.py --trader-id bot1 --uri ws://localhost:8000
```

### 3) Textual Arena App

```powershell
python arena_textual_app.py --rounds 5 --duration 60 --mode SIMULATION --server-status ONLINE
```

Modes:
- `SIMULATION`
- `LIVE`
- `OFFLINE`

## Tests

```powershell
python -m pytest -q
python -m pytest tests/test_phase4_tournament_ux_snippet.py -q
```

## Web Dashboard (Optional)

```powershell
cd web-dashboard
npm install
npm run dev
```

The dashboard consumes the market-data feed at `ws://127.0.0.1:9010`.

## CSV Telemetry

When using `exchange_server.py`, the exporter writes append-only files:

- `trades.csv`: `timestamp,price,qty,buy_trader,sell_trader`
- `performance.csv`: `timestamp,trader_id,position,cash,realized_pnl,total_equity`

## Protocol and Design Docs

- `API_SPEC.md`: WebSocket message schema and endpoint contract.
- `ARCHITECTURE.md`: component boundaries and event flow.
- `SYSTEM_DESIGN.md`: design rationale.
- `BOT_INTEGRATION_GUIDE.md`: custom strategy integration details.
- `DEMO_GUIDE.md`, `WALKTHROUGH.md`: guided runtime flows.

## License

MIT (`LICENSE`)
