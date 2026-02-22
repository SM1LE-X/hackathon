# File: arena_tournament.py

from __future__ import annotations

from arena_config import parse_arena_config
from tournament_manager import startup_and_run


def main() -> None:
    config = parse_arena_config()
    startup_and_run(config)


if __name__ == "__main__":
    main()
