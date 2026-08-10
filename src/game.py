"""Chess game orchestration between two multi-agent teams."""

import asyncio
import uuid
import chess
import chess.pgn
from dataclasses import asdict
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from team import Team, build_team
from monitor import analyze_move, MoveAnalysis
from leaderboard import GameResult, save_game
from config import load_ablations

console = Console()


def board_to_unicode(board: chess.Board) -> str:
    """Render board as unicode string."""
    return str(board)


async def play_game(
    white_team: Team,
    black_team: Team,
    max_moves: int = 80,
    monitor_model: str = "meta-llama/llama-3.1-8b-instruct",
    verbose: bool = True,
) -> GameResult:
    """Play a full chess game between two multi-agent teams."""
    game_id = str(uuid.uuid4())[:8]
    board = chess.Board()
    chess_game = chess.pgn.Game()
    chess_game.headers["White"] = white_team.org_name
    chess_game.headers["Black"] = black_team.org_name
    node = chess_game

    move_analyses: list[dict] = []
    white_tokens_total = 0
    black_tokens_total = 0
    white_latency_total = 0.0
    black_latency_total = 0.0

    result = "draw"
    result_reason = "max-moves"

    if verbose:
        console.print(Panel(
            f"[bold cyan]GAME {game_id}[/bold cyan]\n"
            f"[white]{white_team.org_name}[/white] (White) vs [yellow]{black_team.org_name}[/yellow] (Black)\n"
            f"White style: {white_team.deliberation_style} | Black style: {black_team.deliberation_style}",
            title="Chess Benchmark"
        ))

    full_move_number = 1
    while not board.is_game_over() and len(board.move_stack) < max_moves * 2:
        is_white = board.turn == chess.WHITE
        current_team = white_team if is_white else black_team
        color = "white" if is_white else "black"

        if verbose:
            console.rule(f"[bold]Move {full_move_number} - {color.upper()} ({current_team.org_name})[/bold]")

        # Team deliberates
        decision, proposals = await current_team.deliberate(board, color)

        # Validate and apply move
        try:
            move = chess.Move.from_uci(decision.submitted_move)
            if move not in board.legal_moves:
                # Try to recover: use highest-confidence proposal
                for p in sorted(proposals, key=lambda x: -x.confidence):
                    try:
                        move = chess.Move.from_uci(p.proposed_move)
                        if move in board.legal_moves:
                            decision.submitted_move = p.proposed_move
                            decision.rationale = f"[Recovery from invalid move] {p.reasoning}"
                            break
                    except Exception:
                        continue
                else:
                    # Last resort: first legal move
                    move = list(board.legal_moves)[0]
                    decision.submitted_move = move.uci()

            board.push(move)
            node = node.add_variation(move)

        except Exception as e:
            if verbose:
                console.print(f"[red]Move error: {e}, using first legal move[/red]")
            move = list(board.legal_moves)[0]
            board.push(move)
            node = node.add_variation(move)
            decision.submitted_move = move.uci()

        # Track tokens and latency
        move_tokens = sum(p.tokens_used for p in proposals) + decision.tokens_used
        move_latency = sum(p.latency_ms for p in proposals) + decision.latency_ms

        if is_white:
            white_tokens_total += move_tokens
            white_latency_total += move_latency
        else:
            black_tokens_total += move_tokens
            black_latency_total += move_latency

        if verbose:
            # Print proposals
            table = Table(title="Agent Proposals", show_header=True)
            table.add_column("Agent", style="cyan")
            table.add_column("Move")
            table.add_column("Confidence")
            table.add_column("Reasoning", max_width=50)
            for p in proposals:
                table.add_row(
                    p.agent_role,
                    f"[bold]{p.proposed_move}[/bold]",
                    f"{p.confidence:.2f}",
                    p.reasoning[:100] + "..." if len(p.reasoning) > 100 else p.reasoning
                )
            console.print(table)
            console.print(f"[bold green]SUBMITTED: {decision.submitted_move}[/bold green] by {decision.submitter_role}")
            console.print(f"Rationale: {decision.rationale[:200]}")

        # Monitor analysis (async, don't block game)
        analysis = await analyze_move(
            move_number=len(board.move_stack),
            org_id=current_team.org_id,
            board_fen=board.fen(),
            proposals=[asdict(p) for p in proposals],
            decision={
                "submitted_move": decision.submitted_move,
                "submitter_role": decision.submitter_role,
                "rationale": decision.rationale,
                "tokens_used": decision.tokens_used,
                "latency_ms": decision.latency_ms,
            },
            monitor_model=monitor_model,
        )
        move_analyses.append({
            "move_number": len(board.move_stack),
            "org_id": analysis.org_id,
            "move": analysis.submitted_move,
            "agreement_level": analysis.agreement_level,
            "dominant_behavior": analysis.dominant_behavior,
            "deliberation_quality": analysis.deliberation_quality,
            "key_insight": analysis.key_insight,
            "dissent_detected": analysis.dissent_detected,
            "tokens_total": analysis.tokens_total,
            "latency_total_ms": analysis.latency_total_ms,
        })

        if verbose:
            console.print(
                f"[dim]Monitor: {analysis.agreement_level} | {analysis.dominant_behavior} | "
                f"{analysis.deliberation_quality} quality | {analysis.key_insight[:80]}[/dim]"
            )

        if board.turn == chess.WHITE:
            full_move_number += 1

    # Determine result
    if board.is_checkmate():
        # The side that just moved won
        result = "black" if board.turn == chess.WHITE else "white"
        result_reason = "checkmate"
    elif board.is_stalemate():
        result = "draw"
        result_reason = "stalemate"
    elif board.is_fifty_moves():
        result = "draw"
        result_reason = "50-move"
    elif board.is_insufficient_material():
        result = "draw"
        result_reason = "insufficient-material"
    else:
        result = "draw"
        result_reason = "max-moves"

    chess_game.headers["Result"] = {"white": "1-0", "black": "0-1", "draw": "1/2-1/2"}[result]
    pgn_str = str(chess_game)

    if verbose:
        color_map = {"white": "[bold white]WHITE WINS[/bold white]", "black": "[bold yellow]BLACK WINS[/bold yellow]", "draw": "[bold blue]DRAW[/bold blue]"}
        console.print(Panel(
            f"{color_map[result]} by {result_reason}\n"
            f"Total moves: {len(board.move_stack)}\n"
            f"White tokens: {white_tokens_total:,} | Black tokens: {black_tokens_total:,}",
            title=f"Game {game_id} Complete"
        ))

    game_result = GameResult(
        game_id=game_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        white_org=white_team.org_id,
        black_org=black_team.org_id,
        white_name=white_team.org_name,
        black_name=black_team.org_name,
        result=result,
        result_reason=result_reason,
        total_moves=len(board.move_stack),
        white_tokens=white_tokens_total,
        black_tokens=black_tokens_total,
        white_latency_ms=white_latency_total,
        black_latency_ms=black_latency_total,
        move_analyses=move_analyses,
        pgn=pgn_str,
    )

    save_game(game_result)
    return game_result


async def run_tournament(num_games: int = 2, max_moves: int = 20, verbose: bool = True):
    """Run a tournament between org ablations."""
    config = load_ablations()
    orgs = config["orgs"]
    monitor_model = config.get("monitor_model", "meta-llama/llama-3.1-8b-instruct")

    if len(orgs) < 2:
        console.print("[red]Need at least 2 org configs to run a tournament[/red]")
        return

    results = []
    for i in range(num_games):
        # Alternate colors
        if i % 2 == 0:
            white_org, black_org = orgs[0], orgs[1]
        else:
            white_org, black_org = orgs[1], orgs[0]

        white_team = build_team(white_org, "white")
        black_team = build_team(black_org, "black")

        console.print(f"\n[bold]Game {i+1}/{num_games}[/bold]")
        result = await play_game(
            white_team=white_team,
            black_team=black_team,
            max_moves=max_moves,
            monitor_model=monitor_model,
            verbose=verbose,
        )
        results.append(result)

    return results
