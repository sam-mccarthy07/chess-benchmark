#!/usr/bin/env python3
"""Chess Benchmark for Multi-Agent Orchestration.

Usage:
  python3 main.py                              # Run 2-game tournament (15 moves)
  python3 main.py --tournament                 # Full round-robin across all orgs
  python3 main.py --games 4 --moves 20        # Custom tournament
  python3 main.py --single                    # Quick single game (10 moves)
  python3 main.py --leaderboard               # Show leaderboard
  python3 main.py --charts                    # Generate charts only
"""

import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from game import run_tournament, play_game
from team import build_team
from tournament import run_round_robin
from leaderboard import print_leaderboard, load_all_games
from charts import generate_all_charts
from config import load_ablations, set_seed
from rich.console import Console

console = Console()


async def main():
    parser = argparse.ArgumentParser(description="Chess Benchmark for Multi-Agent Orchestration")
    parser.add_argument("--games", type=int, default=2, help="Number of games to play")
    parser.add_argument("--moves", type=int, default=15, help="Max moves per game")
    parser.add_argument("--tournament", action="store_true", help="Full round-robin across all orgs")
    parser.add_argument("--leaderboard", action="store_true", help="Show leaderboard")
    parser.add_argument("--single", action="store_true", help="Run single quick test game")
    parser.add_argument("--charts", action="store_true", help="Generate charts from existing results")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    parser.add_argument("--seed", type=int, default=None, help="Seed for reproducible runs")
    args = parser.parse_args()

    if args.leaderboard:
        games = load_all_games()
        if not games:
            console.print("[yellow]No games played yet.[/yellow]")
        else:
            print_leaderboard()
        return

    if args.charts:
        paths = generate_all_charts()
        console.print(f"[green]Generated {len(paths)} charts[/green]")
        for p in paths:
            console.print(f"  {p}")
        return

    verbose = not args.quiet

    set_seed(args.seed)

    if args.single:
        config = load_ablations()
        orgs = config["orgs"]
        monitor_model = config.get("monitor_model", "meta-llama/llama-3.1-8b-instruct")
        white_team = build_team(orgs[0], "white")
        black_team = build_team(orgs[-1], "black")
        console.print("[bold green]Running single test game (10 moves)...[/bold green]")
        result = await play_game(
            white_team=white_team,
            black_team=black_team,
            max_moves=10,
            monitor_model=monitor_model,
            verbose=verbose,
            seed=args.seed,
        )
        console.print(f"\nResult: {result.result} by {result.result_reason}")
        console.print(f"White tokens: {result.white_tokens:,} | Black tokens: {result.black_tokens:,}")
        print_leaderboard()
        generate_all_charts()
        return

    if args.tournament:
        results = await run_round_robin(
            games_per_matchup=1,
            max_moves=args.moves,
            verbose=verbose,
        )
    else:
        console.print(f"[bold]Running {args.games}-game tournament ({args.moves} max moves each)[/bold]")
        results = await run_tournament(
            num_games=args.games,
            max_moves=args.moves,
            verbose=verbose,
            seed=args.seed,
        )

    print_leaderboard()
    paths = generate_all_charts()
    console.print(f"\n[green]Generated {len(paths)} charts[/green]")
    for p in paths:
        console.print(f"  {p}")


if __name__ == "__main__":
    asyncio.run(main())
