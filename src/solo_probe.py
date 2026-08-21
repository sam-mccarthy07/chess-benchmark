#!/usr/bin/env python3
"""Ask each agent, alone, what it would have played.

The revealed counterfactual behind Collaborative Advantage. For every position
a team actually faced, each of its agents is asked the same question with no
teammates present.

Why a probe rather than a solo *game*: an agent playing its own game diverges
from the team's game after the first ply, so it never faces the positions the
team faced and per-move centipawn loss is not comparable. Only the outcome
would be. The probe replays the team's exact position sequence, which is what
`CA = min_i CPL_solo(i) - CPL_team` requires.

Run as a separate offline pass over saved games, like backfill.py, so it does
not slow a game run, can be re-run, and can be skipped entirely when budget is
tight. It costs roughly a 30% surcharge on the game it probes.

Usage:
  python3 solo_probe.py                      # probe every unprobed game
  python3 solo_probe.py --game 2d5e8f8a      # one game
  python3 solo_probe.py --max-calls 500      # stop cleanly at a ceiling
  python3 solo_probe.py --force              # re-probe
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chess

from agents import AgentConfig, get_solo_move
from config import PROMPT_VERSIONS, RESULTS_DIR, REQUESTS_PER_MINUTE, MAX_CONCURRENT_CALLS
from throttle import BudgetExceeded, CallGate, set_gate


def agents_from_turn(turn: dict) -> list[AgentConfig]:
    """Reconstruct the agent roster from the round-0 proposals.

    Taken from the record rather than from the config file so a probe always
    matches the agents that actually played, even if the config has since
    changed.
    """
    rounds = turn.get("rounds") or []
    source = rounds[0]["proposals"] if rounds else turn.get("proposals", [])
    return [
        AgentConfig(
            model=p.get("model", ""),
            role=p.get("agent_role", ""),
            persona="",  # filled from config below when available
            org_id=turn.get("org_id", ""),
            org_name=turn.get("org_id", ""),
        )
        for p in source
        if p.get("model")
    ]


def personas_for(org_id: str, config: dict) -> dict[str, str]:
    for org in config.get("orgs", []):
        if org["id"] == org_id:
            return {a["role"]: a.get("persona", "") for a in org["agents"]}
    return {}


async def probe_turn(turn: dict, personas: dict[str, str]) -> dict:
    """Probe every agent on one position."""
    board = chess.Board(turn["fen_before"])
    legal = [m.uci() for m in board.legal_moves]
    history = []  # the record does not retain pre-position history

    agents = agents_from_turn(turn)
    for a in agents:
        a.persona = personas.get(a.role, "")

    probes = await asyncio.gather(*[
        get_solo_move(
            agent=a,
            board_fen=turn["fen_before"],
            legal_moves=legal,
            move_history=history,
            color=turn.get("color", "white"),
            move_number=turn.get("move_number", 0),
        )
        for a in agents
    ])

    return {
        "ply": turn.get("ply"),
        "fen": turn["fen_before"],
        "probes": [
            {
                "agent_role": p.agent_role,
                "model": p.model,
                "move": p.proposed_move,
                "status": p.status,
                "legal": p.legal,
                "confidence": p.confidence,
                "reasoning": p.reasoning[:300],
                "tokens_used": p.tokens_used,
                "error": p.error,
            }
            for p in probes
        ],
    }


async def probe_game(data: dict, config: dict, verbose: bool = True) -> dict:
    turns = data.get("moves") or []
    if not turns:
        return {"schema": "none", "reason": "record predates per-turn logging"}

    by_ply = []
    for turn in turns:
        personas = personas_for(turn.get("org_id", ""), config)
        by_ply.append(await probe_turn(turn, personas))
        if verbose:
            print(f"    ply {turn.get('ply')}: "
                  f"{sum(1 for p in by_ply[-1]['probes'] if p['legal'])}/"
                  f"{len(by_ply[-1]['probes'])} legal", flush=True)

    return {
        "schema": "by_ply",
        "provenance": {
            "prompt_version": PROMPT_VERSIONS["solo"],
            "probed_at": datetime.now(timezone.utc).isoformat(),
        },
        "by_ply": by_ply,
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="Probe each agent's solo move on played positions")
    ap.add_argument("--game", help="Only this game_id")
    ap.add_argument("--force", action="store_true", help="Re-probe games already probed")
    ap.add_argument("--max-calls", type=int, default=None)
    ap.add_argument("--rpm", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--config", type=Path, default=None,
                    help="Org config supplying personas (defaults to the one in the manifest)")
    args = ap.parse_args()

    paths = sorted(RESULTS_DIR.glob("game_*.json"))
    if args.game:
        paths = [p for p in paths if args.game in p.name]
    if not paths:
        print("No game files found.")
        return 1

    gate = CallGate(
        per_minute=args.rpm if args.rpm else REQUESTS_PER_MINUTE,
        max_concurrent=args.concurrency or MAX_CONCURRENT_CALLS,
        max_calls=args.max_calls,
    )
    set_gate(gate)

    config = {}
    if args.config and args.config.is_file():
        config = json.loads(args.config.read_text())

    probed = skipped = failed = 0
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            print(f"  {path.name}: unreadable ({e})")
            failed += 1
            continue

        if data.get("solo_probes") and not args.force:
            skipped += 1
            continue

        print(f"  {path.name}: probing {len(data.get('moves') or [])} turns")
        try:
            data["solo_probes"] = await probe_game(data, config)
        except BudgetExceeded as e:
            print(f"\nStopping: {e}")
            break
        except Exception as e:
            print(f"  {path.name}: probe failed ({e})")
            failed += 1
            continue

        path.write_text(json.dumps(data, indent=2))
        probed += 1

    s = gate.stats.as_dict()
    print(f"\nProbed {probed}, skipped {skipped}, failed {failed}.")
    print(f"Calls: {s['succeeded']:,} ok, {s['failed']:,} failed, "
          f"{s['retried']:,} retried | {s['total_tokens']:,} tokens | {s['elapsed_s']:.0f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
