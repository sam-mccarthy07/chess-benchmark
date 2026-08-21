#!/usr/bin/env python3
"""Annotate stored games with Stockfish ground truth.

Analysis is a separate pass over saved results rather than part of the game
loop, so a game can be re-scored under a different engine or depth without
replaying it and without spending API credit.

Usage:
  python3 backfill.py                 # analyse every unanalysed game
  python3 backfill.py --force         # re-analyse everything
  python3 backfill.py --depth 12      # faster, shallower pass
  python3 backfill.py --game 2d5e8f8a # one game
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import RESULTS_DIR, ENGINE_PARAMS
from metrics import analyse_turn, summarise_game, analyse_pgn
from oracle import Oracle


def analyse_game(oracle: Oracle, data: dict) -> dict:
    """Return the oracle analysis block for one game record."""
    moves = data.get("moves") or []

    if moves:
        turn_metrics = [analyse_turn(oracle, t) for t in moves]
        return {
            "schema": "turns",
            "provenance": oracle.provenance(),
            "turns": turn_metrics,
            "by_org": summarise_game(turn_metrics, moves),
        }

    # Pre-PR1 games have no per-turn record. Played-move quality is all that
    # can be recovered, and even that is only pipeline-validation data: those
    # games were generated under the old silent-coercion behaviour, so some
    # "moves" were never chosen by any agent.
    return {
        "schema": "pgn_only",
        "provenance": oracle.provenance(),
        "plies": analyse_pgn(oracle, data.get("pgn", "")),
        "caveat": (
            "Generated before the integrity fix; proposals were silently "
            "coerced, so this is pipeline validation only, not results."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill Stockfish analysis into saved games")
    ap.add_argument("--depth", type=int, default=ENGINE_PARAMS["depth"])
    ap.add_argument("--engine", default=ENGINE_PARAMS["engine_path"])
    ap.add_argument("--force", action="store_true", help="Re-analyse already-analysed games")
    ap.add_argument("--game", help="Analyse only this game_id")
    args = ap.parse_args()

    paths = sorted(RESULTS_DIR.glob("game_*.json"))
    if args.game:
        paths = [p for p in paths if args.game in p.name]
    if not paths:
        print("No game files found.")
        return 1

    try:
        oracle_cm = Oracle(engine_path=args.engine, depth=args.depth)
        oracle_cm.__enter__()
    except FileNotFoundError:
        print(f"Engine not found: {args.engine!r}. Install it (brew install stockfish) "
              f"or set STOCKFISH_PATH.")
        return 1

    analysed = skipped = failed = 0
    try:
        print(f"Engine: {oracle_cm.engine_id} | depth {args.depth} | "
              f"threads {oracle_cm.threads}\n")
        for path in paths:
            try:
                data = json.loads(path.read_text())
            except Exception as e:
                print(f"  {path.name}: unreadable ({e})")
                failed += 1
                continue

            if data.get("oracle_analysis") and not args.force:
                skipped += 1
                continue

            start = time.time()
            try:
                data["oracle_analysis"] = analyse_game(oracle_cm, data)
            except Exception as e:
                print(f"  {path.name}: analysis failed ({e})")
                failed += 1
                continue

            path.write_text(json.dumps(data, indent=2))
            block = data["oracle_analysis"]
            n = len(block.get("turns") or block.get("plies") or [])
            print(f"  {path.name}: {block['schema']}, {n} records, {time.time()-start:.1f}s")
            analysed += 1
    finally:
        oracle_cm.__exit__(None, None, None)

    print(f"\nAnalysed {analysed}, skipped {skipped}, failed {failed}.")
    if skipped:
        print("Skipped games already carry analysis; use --force to redo them.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
