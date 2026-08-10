"""Round-robin tournament runner across all org ablations."""

import asyncio
import itertools
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from game import play_game
from team import build_team
from leaderboard import GameResult, print_leaderboard
from config import load_ablations

console = Console()


async def run_round_robin(
    games_per_matchup: int = 2,
    max_moves: int = 15,
    verbose: bool = False,
) -> list[GameResult]:
    """Run full round-robin between all orgs in ablations.json."""
    config = load_ablations()
    orgs = config["orgs"]
    monitor_model = config.get("monitor_model", "meta-llama/llama-3.1-8b-instruct")

    pairs = list(itertools.combinations(range(len(orgs)), 2))
    total_games = len(pairs) * games_per_matchup

    console.print(f"\n[bold cyan]Round-Robin Tournament[/bold cyan]")
    console.print(f"Orgs: {[o['id'] for o in orgs]}")
    console.print(f"Matchups: {len(pairs)} pairs × {games_per_matchup} games = {total_games} games")
    console.print(f"Max moves per game: {max_moves}\n")

    all_results: list[GameResult] = []
    game_num = 0

    for i, j in pairs:
        for game_idx in range(games_per_matchup):
            game_num += 1
            # Alternate colors each game in the matchup
            if game_idx % 2 == 0:
                white_org, black_org = orgs[i], orgs[j]
            else:
                white_org, black_org = orgs[j], orgs[i]

            white_team = build_team(white_org, "white")
            black_team = build_team(black_org, "black")

            console.print(
                f"[bold]Game {game_num}/{total_games}:[/bold] "
                f"[white]{white_team.org_name}[/white] vs "
                f"[yellow]{black_team.org_name}[/yellow]"
            )

            result = await play_game(
                white_team=white_team,
                black_team=black_team,
                max_moves=max_moves,
                monitor_model=monitor_model,
                verbose=verbose,
            )
            all_results.append(result)

            winner = {
                "white": white_team.org_name,
                "black": black_team.org_name,
                "draw": "Draw",
            }[result.result]
            console.print(
                f"  → {winner} ({result.result_reason}) | "
                f"Moves: {result.total_moves} | "
                f"Tokens W:{result.white_tokens:,} B:{result.black_tokens:,}\n"
            )

    print_leaderboard()
    return all_results
