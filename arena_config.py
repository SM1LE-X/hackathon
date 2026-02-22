# File: arena_config.py

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArenaConfig:
    rounds: int
    duration: int


def _validate_positive(value: int, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return value


def _prompt_positive_int(prompt: str) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            parsed = int(raw)
        except ValueError:
            print("Please enter a valid integer.")
            continue
        if parsed <= 0:
            print("Value must be > 0.")
            continue
        return parsed


def parse_arena_config(argv: list[str] | None = None) -> ArenaConfig:
    parser = argparse.ArgumentParser(description="CLI Competitive Trading Arena")
    parser.add_argument("--rounds", type=int, default=None, help="Number of rounds (>0)")
    parser.add_argument("--duration", type=int, default=None, help="Session duration in seconds (>0)")
    args = parser.parse_args(argv)

    rounds = args.rounds
    duration = args.duration

    if rounds is None:
        rounds = _prompt_positive_int("Enter number of rounds: ")
    if duration is None:
        duration = _prompt_positive_int("Enter session duration (seconds): ")

    rounds = _validate_positive(int(rounds), "rounds")
    duration = _validate_positive(int(duration), "duration")
    return ArenaConfig(rounds=rounds, duration=duration)
