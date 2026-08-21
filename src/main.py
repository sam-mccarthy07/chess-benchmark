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

from game import run_tournament, run_position_series, play_game
from positions import load_position_set, position_set_provenance
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
    parser.add_argument("--positions", type=Path, default=None,
                        help="Position set JSON. Games start from these sampled "
                             "balanced middlegame positions instead of move 1.")
    parser.add_argument("--limit-positions", type=int, default=None,
                        help="Use only the first N positions (for pilots)")
    parser.add_argument("--no-swap-colors", action="store_true",
                        help="Play each position once instead of twice with colours swapped")
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

    if args.positions:
        config = load_ablations()
        orgs = config["orgs"]
        if len(orgs) < 2:
            console.print("[red]Need at least 2 org configs[/red]")
            return
        try:
            pset = load_position_set(args.positions)
        except Exception as e:
            console.print(f"[red]Could not load position set: {e}[/red]")
            return

        positions = pset["positions"]
        if args.limit_positions:
            positions = positions[: args.limit_positions]

        meta = position_set_provenance(pset, args.positions)
        console.print(
            f"[bold]{len(positions)} positions from {meta['file']} "
            f"(version {meta['version']}, seed {meta['seed']})[/bold]"
        )
        results = await run_position_series(
            white_org=orgs[0],
            black_org=orgs[1],
            positions=positions,
            position_set_meta=meta,
            max_moves=args.moves,
            monitor_model=config.get("monitor_model", "meta-llama/llama-3.1-8b-instruct"),
            verbose=verbose,
            seed=args.seed,
            swap_colors=not args.no_swap_colors,
        )
        print_leaderboard()
        console.print(f"\n[green]Played {len(results)} games[/green]")
        return

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
