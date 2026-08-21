"""Leaderboard tracking for chess benchmark results."""

import json
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path

from config import RESULTS_DIR


@dataclass
class GameResult:
    game_id: str
    timestamp: str
    white_org: str
    black_org: str
    white_name: str
    black_name: str
    result: str              # "white", "black", "draw"
    result_reason: str       # "checkmate", "stalemate", "50-move", "max-moves", "resign"
    total_moves: int
    white_tokens: int
    black_tokens: int
    white_latency_ms: float
    black_latency_ms: float
    move_analyses: list[dict] = field(default_factory=list)
    pgn: str = ""
    # Run provenance. Games with different fingerprints must not be pooled.
    manifest: dict = field(default_factory=dict)
    # Full per-turn record: proposals, decision, resolution, integrity counters.
    moves: list[dict] = field(default_factory=list)
    integrity_totals: dict = field(default_factory=dict)


def save_game(result: GameResult):
    """Save game result to results directory."""
    path = RESULTS_DIR / f"game_{result.game_id}.json"
    with open(path, "w") as f:
        json.dump(asdict(result), f, indent=2)
    print(f"Game saved: {path}")
    return path


def load_all_games(warn: bool = True) -> list[GameResult]:
    """Load all game results.

    Unreadable files are reported rather than silently dropped — a results
    directory that quietly loses games is how you end up analysing a biased
    subset without knowing it.
    """
    games = []
    for path in sorted(RESULTS_DIR.glob("game_*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            known = {f.name for f in fields(GameResult)}
            unknown = set(data) - known
            if unknown and warn:
                print(f"[warn] {path.name}: ignoring unknown fields {sorted(unknown)}")
            games.append(GameResult(**{k: v for k, v in data.items() if k in known}))
        except Exception as e:
            if warn:
                print(f"[warn] could not load {path.name}: {e}")
    return sorted(games, key=lambda g: g.timestamp, reverse=True)


def compute_leaderboard() -> dict:
    """Compute leaderboard stats from all games."""
    games = load_all_games()
    orgs: dict[str, dict] = {}

    for game in games:
        for org_id, color, opponent in [
            (game.white_org, "white", game.black_org),
            (game.black_org, "black", game.white_org),
        ]:
            if org_id not in orgs:
                orgs[org_id] = {
                    "org_id": org_id,
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "draws": 0,
                    "total_tokens": 0,
                    "total_latency_ms": 0.0,
                }

            orgs[org_id]["games"] += 1
            if color == "white":
                orgs[org_id]["total_tokens"] += game.white_tokens
                orgs[org_id]["total_latency_ms"] += game.white_latency_ms
            else:
                orgs[org_id]["total_tokens"] += game.black_tokens
                orgs[org_id]["total_latency_ms"] += game.black_latency_ms

            if game.result == color:
                orgs[org_id]["wins"] += 1
            elif game.result == "draw":
                orgs[org_id]["draws"] += 1
            else:
                orgs[org_id]["losses"] += 1

    # Compute win rate and sort
    leaderboard = []
    for org in orgs.values():
        g = org["games"]
        org["win_rate"] = org["wins"] / g if g > 0 else 0.0
        org["avg_tokens_per_game"] = org["total_tokens"] / g if g > 0 else 0
        org["score"] = org["wins"] + 0.5 * org["draws"]
        leaderboard.append(org)

    leaderboard.sort(key=lambda o: (-o["score"], -o["win_rate"]))
    return {"leaderboard": leaderboard, "total_games": len(games)}


def print_leaderboard():
    """Print leaderboard to stdout."""
    lb = compute_leaderboard()
    print(f"\n{'='*60}")
    print(f"CHESS BENCHMARK LEADERBOARD ({lb['total_games']} games)")
    print(f"{'='*60}")
    print(f"{'Org':<25} {'W':>4} {'L':>4} {'D':>4} {'Win%':>6} {'Score':>6} {'Avg Tok':>8}")
    print(f"{'-'*60}")
    for org in lb["leaderboard"]:
        print(
            f"{org['org_id']:<25} "
            f"{org['wins']:>4} "
            f"{org['losses']:>4} "
            f"{org['draws']:>4} "
            f"{org['win_rate']:>6.1%} "
            f"{org['score']:>6.1f} "
            f"{org['avg_tokens_per_game']:>8.0f}"
        )
    print(f"{'='*60}\n")
