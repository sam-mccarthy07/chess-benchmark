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

from team import Team, DeliberationRound, build_team, drift_summary
from agents import (
    MoveProposal,
    SubmitterDecision,
    STATUS_OK,
    STATUS_ILLEGAL,
    STATUS_UNPARSEABLE,
    STATUS_API_ERROR,
)
from monitor import analyze_move, MoveAnalysis
from leaderboard import GameResult, load_all_games, save_game
from config import load_ablations, build_manifest, set_seed
from throttle import BudgetExceeded, set_gate

console = Console()


def board_to_unicode(board: chess.Board) -> str:
    """Render board as unicode string."""
    return str(board)


def resolve_move(
    decision: SubmitterDecision,
    proposals: list[MoveProposal],
    board: chess.Board,
) -> tuple[chess.Move, dict]:
    """Choose a move that can actually be played, and record how we got it.

    The team's decision is never rewritten. When the decision is unplayable the
    game still has to continue, so we fall back — but the fallback is recorded
    as a resolution distinct from the decision, so analysis can always tell
    what the team chose from what the board received.
    """
    legal = {m.uci(): m for m in board.legal_moves}

    if decision.submitted_move and decision.submitted_move in legal:
        return legal[decision.submitted_move], {
            "played_move": decision.submitted_move,
            "method": "as_decided",
            "note": "",
        }

    for p in sorted(proposals, key=lambda x: -x.confidence):
        if p.proposed_move and p.proposed_move in legal:
            return legal[p.proposed_move], {
                "played_move": p.proposed_move,
                "method": "fallback_to_proposal",
                "note": (
                    f"decision {decision.submitted_move!r} ({decision.status}) was unplayable; "
                    f"played {p.agent_role}'s legal proposal instead"
                ),
            }

    first = sorted(legal)[0]  # sorted for determinism, not board iteration order
    return legal[first], {
        "played_move": first,
        "method": "fallback_first_legal",
        "note": (
            f"decision {decision.submitted_move!r} ({decision.status}) was unplayable "
            f"and no proposal was legal"
        ),
    }


def integrity_summary(
    proposals: list[MoveProposal], decision: SubmitterDecision
) -> dict:
    """Per-turn integrity counters. Illegal, unparseable and API-error are
    counted separately — conflating them is what made the old logs unusable."""
    return {
        "illegal_proposals": sum(1 for p in proposals if p.status == STATUS_ILLEGAL),
        "unparseable_proposals": sum(1 for p in proposals if p.status == STATUS_UNPARSEABLE),
        "api_error_proposals": sum(1 for p in proposals if p.status == STATUS_API_ERROR),
        "ok_proposals": sum(1 for p in proposals if p.status == STATUS_OK),
        "distinct_proposed_moves": len({p.proposed_move for p in proposals if p.proposed_move}),
        "decision_status": decision.status,
        "decision_legal": decision.legal,
        "off_slate": decision.off_slate,
        # The team settled on a move that cannot be played. How a team talks
        # itself into an impossible move is a first-class object of study.
        "false_consensus": decision.status == STATUS_ILLEGAL,
    }


async def play_game(
    white_team: Team,
    black_team: Team,
    max_moves: int = 80,
    monitor_model: str = "meta-llama/llama-3.1-8b-instruct",
    verbose: bool = True,
    seed: int | None = None,
    start_fen: str | None = None,
    position_id: str | None = None,
    position_set: dict | None = None,
) -> GameResult:
    """Play a full chess game between two multi-agent teams."""
    game_id = str(uuid.uuid4())[:8]

    # Games normally begin from a sampled balanced middlegame position rather
    # than move 1: opening play is largely recall, so agents propose the same
    # book move and deliberation carries no signal for the first twenty-odd
    # plies. start_fen=None falls back to the standard start, which is kept
    # only for smoke tests.
    board = chess.Board(start_fen) if start_fen else chess.Board()
    if not start_fen and verbose:
        console.print(
            "[yellow]No start position given — playing from the standard opening. "
            "Opening play is largely recall, so deliberation carries little signal "
            "here. Use --positions for anything intended as results.[/yellow]"
        )

    chess_game = chess.pgn.Game()
    if start_fen:
        # Without SetUp/FEN the PGN replays from the standard opening and every
        # recorded move becomes illegal — including in the oracle's PGN path.
        chess_game.setup(board)
    chess_game.headers["White"] = white_team.org_name
    chess_game.headers["Black"] = black_team.org_name
    node = chess_game

    manifest = build_manifest(
        seed=seed,
        extra={
            "monitor_model": monitor_model,
            "max_moves": max_moves,
            "white_org": white_team.org_id,
            "black_org": black_team.org_id,
            # Which position this game started from, and which released set it
            # came from. Games from different position sets must never pool.
            "start_fen": start_fen,
            "position_id": position_id,
            "position_set": position_set,
            "deliberation_rounds": {
                white_team.org_id: white_team.deliberation_rounds,
                black_team.org_id: black_team.deliberation_rounds,
            },
        },
    )

    move_analyses: list[dict] = []
    moves: list[dict] = []
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
            f"White style: {white_team.deliberation_style} | Black style: {black_team.deliberation_style}\n"
            f"Start: {('position ' + position_id) if position_id else 'standard opening'}"
            f" (move {board.fullmove_number}, {'white' if board.turn == chess.WHITE else 'black'} to play)\n"
            f"[dim]config {manifest['config_fingerprint']} | seed {seed}[/dim]",
            title="Chess Benchmark"
        ))

    # Continue the source game's numbering rather than restarting at 1, so a
    # logged move number matches the position it was actually played in.
    full_move_number = board.fullmove_number
    # move_stack counts only plies played in *this* game, so max_moves is
    # always moves-from-the-start-position regardless of where we began.
    while not board.is_game_over() and len(board.move_stack) < max_moves * 2:
        is_white = board.turn == chess.WHITE
        current_team = white_team if is_white else black_team
        color = "white" if is_white else "black"

        # Capture pre-move state: everything the agents actually saw.
        fen_before = board.fen()
        legal_before = [m.uci() for m in board.legal_moves]

        if verbose:
            console.rule(f"[bold]Move {full_move_number} - {color.upper()} ({current_team.org_name})[/bold]")

        decision, rounds = await current_team.deliberate(board, color)
        # The submitter chose from the final round, so that is the slate every
        # decision-side metric is measured against.
        proposals = rounds[-1].proposals

        played, resolution = resolve_move(decision, proposals, board)
        board.push(played)
        node = node.add_variation(played)

        # Cost is summed over every round, not just the final one — discussion
        # rounds are most of the spend, and a per-turn token figure that
        # ignored them would understate the cost of the deliberation arm.
        move_tokens = sum(p.tokens_used for r in rounds for p in r.proposals) + decision.tokens_used
        move_latency = sum(p.latency_ms for r in rounds for p in r.proposals) + decision.latency_ms

        if is_white:
            white_tokens_total += move_tokens
            white_latency_total += move_latency
        else:
            black_tokens_total += move_tokens
            black_latency_total += move_latency

        integrity = integrity_summary(proposals, decision)

        moves.append({
            "ply": len(board.move_stack),
            "move_number": full_move_number,
            "org_id": current_team.org_id,
            "color": color,
            "fen_before": fen_before,
            "legal_move_count": len(legal_before),
            "submitter_role": decision.submitter_role,
            "submitter_model": decision.model,
            # Group stream: every round, so position trajectories can be
            # reconstructed. Round 0 is the independent anchor the influence
            # metrics need.
            "rounds": [
                {"round_index": r.round_index, "proposals": [asdict(p) for p in r.proposals]}
                for r in rounds
            ],
            # Solo stream: written and read only by the agent that produced it.
            # Kept as a sibling of `rounds` rather than nested inside it, so
            # anything rendering the group stream cannot show private content
            # by walking one level too deep.
            "private_notes": [
                asdict(n) for r in rounds for n in r.private_notes
            ],
            "drift": drift_summary(rounds),
            # Final round, duplicated under the name every decision-side
            # consumer already reads.
            "proposals": [asdict(p) for p in proposals],
            "decision": {
                "submitted_move": decision.submitted_move,
                "submitter_role": decision.submitter_role,
                "model": decision.model,
                "rationale": decision.rationale,
                "status": decision.status,
                "legal": decision.legal,
                "off_slate": decision.off_slate,
                "error": decision.error,
                "tokens_used": decision.tokens_used,
                "latency_ms": decision.latency_ms,
            },
            "resolution": resolution,
            "integrity": integrity,
        })

        if verbose:
            table = Table(title="Agent Proposals", show_header=True)
            table.add_column("Agent", style="cyan")
            table.add_column("Move")
            table.add_column("Status")
            table.add_column("Conf")
            table.add_column("Reasoning", max_width=44)
            for p in proposals:
                style = "green" if p.status == STATUS_OK else "red"
                table.add_row(
                    p.agent_role,
                    f"[bold]{p.proposed_move or '—'}[/bold]",
                    f"[{style}]{p.status}[/{style}]",
                    f"{p.confidence:.2f}",
                    (p.reasoning[:100] + "...") if len(p.reasoning) > 100 else p.reasoning,
                )
            console.print(table)
            if len(rounds) > 1:
                d = drift_summary(rounds)
                console.print(
                    f"[dim]Deliberation: distinct moves by round "
                    f"{d['distinct_moves_by_round']} | {d['drifted_agents']} agent(s) "
                    f"changed position{' | converged' if d['converged'] else ''}[/dim]"
                )
            dec_style = "green" if decision.legal else "red"
            console.print(
                f"[bold {dec_style}]DECIDED: {decision.submitted_move or '—'} "
                f"({decision.status})[/bold {dec_style}] by {decision.submitter_role}"
            )
            if resolution["method"] != "as_decided":
                console.print(f"[yellow]RESOLVED: {resolution['played_move']} — {resolution['note']}[/yellow]")
            console.print(f"Rationale: {decision.rationale[:200]}")

        # Monitor sees the pre-move position the agents deliberated over,
        # not the post-move position.
        analysis = await analyze_move(
            move_number=len(board.move_stack),
            org_id=current_team.org_id,
            board_fen=fen_before,
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
            "scope": analysis.scope,
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

    if board.is_checkmate():
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

    def _all_round_proposals(m):
        return [p for r in m.get("rounds", []) for p in r["proposals"]]

    totals = {
        # Final-round counts: what the submitter was actually looking at.
        "illegal_proposals": sum(m["integrity"]["illegal_proposals"] for m in moves),
        "unparseable_proposals": sum(m["integrity"]["unparseable_proposals"] for m in moves),
        "api_error_proposals": sum(m["integrity"]["api_error_proposals"] for m in moves),
        # Across every round, including discussion — the true call-level rates.
        "illegal_proposals_all_rounds": sum(
            1 for m in moves for p in _all_round_proposals(m) if p["status"] == STATUS_ILLEGAL
        ),
        "api_error_proposals_all_rounds": sum(
            1 for m in moves for p in _all_round_proposals(m) if p["status"] == STATUS_API_ERROR
        ),
        "false_consensus_events": sum(1 for m in moves if m["integrity"]["false_consensus"]),
        "off_slate_decisions": sum(1 for m in moves if m["integrity"]["off_slate"]),
        "unresolved_decisions": sum(1 for m in moves if m["resolution"]["method"] != "as_decided"),
        # Deliberation dynamics. Convergence rising while quality does not is
        # the conformity signature H8 predicts.
        "converged_turns": sum(1 for m in moves if m.get("drift", {}).get("converged")),
        "drifted_agent_turns": sum(m.get("drift", {}).get("drifted_agents", 0) for m in moves),
        "unanimous_first_round": sum(
            1 for m in moves if (m.get("drift", {}).get("unanimous_by_round") or [False])[0]
        ),
        "unanimous_final_round": sum(
            1 for m in moves if (m.get("drift", {}).get("unanimous_by_round") or [False])[-1]
        ),
        "total_turns": len(moves),
    }

    if verbose:
        color_map = {
            "white": "[bold white]WHITE WINS[/bold white]",
            "black": "[bold yellow]BLACK WINS[/bold yellow]",
            "draw": "[bold blue]DRAW[/bold blue]",
        }
        console.print(Panel(
            f"{color_map[result]} by {result_reason}\n"
            f"Total moves: {len(board.move_stack)}\n"
            f"White tokens: {white_tokens_total:,} | Black tokens: {black_tokens_total:,}\n"
            f"[dim]illegal proposals: {totals['illegal_proposals']} | "
            f"unparseable: {totals['unparseable_proposals']} | "
            f"api errors: {totals['api_error_proposals']} | "
            f"false consensus: {totals['false_consensus_events']} | "
            f"fallbacks: {totals['unresolved_decisions']}/{totals['total_turns']}\n"
            f"unanimity: {totals['unanimous_first_round']} -> "
            f"{totals['unanimous_final_round']} of {totals['total_turns']} turns | "
            f"converged: {totals['converged_turns']}[/dim]",
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
        start_fen=start_fen or "",
        position_id=position_id or "",
        manifest=manifest,
        moves=moves,
        integrity_totals=totals,
    )

    save_game(game_result)
    return game_result


async def run_position_series(
    white_org: dict,
    black_org: dict,
    positions: list[dict],
    position_set: dict | None = None,
    position_set_meta: dict | None = None,
    max_moves: int = 30,
    monitor_model: str = "meta-llama/llama-3.1-8b-instruct",
    verbose: bool = True,
    seed: int | None = None,
    swap_colors: bool = True,
    gate=None,
    resume: bool = False,
):
    """Play a pair of orgs across a fixed set of start positions.

    Each position is played twice with colours swapped, so a position that
    happens to favour the side to move cannot advantage one org. Teams are
    rebuilt per game: a Team carries submitter rotation state, and reusing one
    across positions would leak rotation phase between otherwise independent
    games.
    """
    set_seed(seed)
    results = []

    # Rate-limited runs are long, so a run must be resumable. Completion is
    # keyed on (position, white org, black org) rather than a counter, so
    # resuming after an interruption cannot silently replay or skip a cell.
    done = completed_games() if resume else set()
    if resume and done and verbose:
        console.print(f"[dim]Resuming: {len(done)} games already on disk[/dim]")

    if gate is not None:
        set_gate(gate)

    skipped = 0
    for i, pos in enumerate(positions, start=1):
        orderings = [(white_org, black_org)]
        if swap_colors:
            orderings.append((black_org, white_org))

        for w_org, b_org in orderings:
            key = (pos["position_id"], w_org["id"], b_org["id"])
            if key in done:
                skipped += 1
                continue

            if verbose:
                console.print(
                    f"\n[bold]Position {i}/{len(positions)} "
                    f"({pos['position_id']}) — {w_org['id']} as white[/bold]"
                )
            try:
                result = await play_game(
                    white_team=build_team(w_org, "white"),
                    black_team=build_team(b_org, "black"),
                    max_moves=max_moves,
                    monitor_model=monitor_model,
                    verbose=verbose,
                    seed=seed,
                    start_fen=pos["fen"],
                    position_id=pos["position_id"],
                    position_set=position_set_meta,
                )
            except BudgetExceeded as e:
                # Stop cleanly rather than half-writing the grid. Everything
                # already saved stays valid, and --resume picks up here.
                console.print(f"\n[yellow]Stopping: {e}[/yellow]")
                return results
            results.append(result)

    if verbose and skipped:
        console.print(f"[dim]Skipped {skipped} games already present[/dim]")

    return results


def completed_games() -> set[tuple[str, str, str]]:
    """(position_id, white_org, black_org) for every game already saved.

    Read from disk rather than tracked in memory so a resumed run sees work
    done by an earlier process.
    """
    out = set()
    for g in load_all_games(warn=False):
        if g.position_id:
            out.add((g.position_id, g.white_org, g.black_org))
    return out


async def run_tournament(
    num_games: int = 2,
    max_moves: int = 20,
    verbose: bool = True,
    seed: int | None = None,
):
    """Run a tournament between org ablations."""
    config = load_ablations()
    orgs = config["orgs"]
    monitor_model = config.get("monitor_model", "meta-llama/llama-3.1-8b-instruct")

    if len(orgs) < 2:
        console.print("[red]Need at least 2 org configs to run a tournament[/red]")
        return

    set_seed(seed)

    results = []
    for i in range(num_games):
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
            seed=seed,
        )
        results.append(result)

    return results
