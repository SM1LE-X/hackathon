# Repository Guidelines

## Project Structure & Module Organization
Core exchange modules live at the repository root:
- `models.py`: shared enums/dataclasses and protocol shapes.
- `orderbook.py`, `engine.py`: deterministic FIFO matching and book state.
- `positions.py`, `risk_manager.py`, `margin_risk_manager.py`: accounting and risk checks.
- `server.py`: async WebSocket orchestration.
- `session_manager.py`, `tournament_manager.py`: round/session lifecycle.
- `arena_cli.py`, `arena_textual_app.py`, `arena_tournament.py`: CLI/TUI runtime layers.
- `bot.py`: sample trading client.

Tests are in `tests/` (`test_phase*_snippet.py`). Extended learning docs are in `Documents/`.

## Build, Test, and Development Commands
Use Python 3.10+ and a virtual environment.

```powershell
python -m venv .venv
.\\.venv\\Scripts\\activate
pip install -r requirements.txt
```

Run services:

```powershell
python server.py
python bot.py --trader-id bot1
python arena_tournament.py --rounds 5 --duration 60
python arena_textual_app.py --rounds 5 --duration 60 --mode SIMULATION
```

Run tests:

```powershell
python -m pytest -q
python -m pytest tests/test_phase4_tournament_ux_snippet.py -q
```

## Coding Style & Naming Conventions
- Follow PEP 8, 4-space indentation, and explicit type hints on public APIs.
- Use `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Keep separation strict: matching logic stays out of UI/network/accounting layers.
- Preserve determinism: stable ordering, no hidden global state, no random behavior in engine/risk paths.

## Testing Guidelines
- Framework: `pytest`.
- Add tests near the relevant phase (`tests/test_phaseX_*.py`).
- Test names should describe behavior, e.g., `test_self_match_prevention_skips_own_resting_order`.
- For engine/risk/accounting changes, include deterministic assertions for ordering, PnL math, and state transitions.

## Commit & Pull Request Guidelines
Git history is not included in this workspace snapshot, so use a clear convention:
- Commit format: `type(scope): short summary` (example: `fix(engine): prevent crossed snapshot after match`).
- Keep commits focused (one subsystem per commit).
- PRs should include: purpose, changed files, test evidence (`pytest` output), and protocol/UI screenshots when behavior changes.

## Security & Configuration Notes
- Never commit secrets or external credentials.
- Keep runtime config explicit via CLI flags (`--rounds`, `--duration`, `--mode`, `--server-status`).
- Reject invalid input at the server boundary; do not trust client payload fields.
