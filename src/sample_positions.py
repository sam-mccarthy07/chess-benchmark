#!/usr/bin/env python3
"""Build a versioned set of balanced middlegame start positions.

Source PGN is not bundled — Lichess dumps are far too large to vendor. Get one:

  Full monthly dump (very large, .zst):
    https://database.lichess.org/

  Lichess Elite database (2300+ only, much smaller, usually the better option):
    https://database.nikonoel.fr/

.pgn, .pgn.gz and .pgn.bz2 work out of the box. .zst needs `pip install
zstandard`, or decompress first with `zstd -d`.

Usage:
  python3 sample_positions.py --pgn games.pgn --target 200 --seed 1
  python3 sample_positions.py --pgn a.pgn --pgn b.pgn --target 500 --depth 16
  python3 sample_positions.py --pgn games.pgn --describe          # dry run, no engine
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import PROJECT_ROOT, ENGINE_PARAMS
from oracle import Oracle
from positions import (
    PositionFilters,
    build_position_set,
    candidate_from_game,
    iter_games,
    sample_positions,
)

POSITIONS_DIR = PROJECT_ROOT / "positions"


def describe(pgn_paths, filters, max_games):
    """Cheap-filter-only pass, so you can size a source before paying for engine time."""
    import random
    rng = random.Random(0)
    games = eligible = 0
    for path in pgn_paths:
        for game in iter_games(path, filters.min_elo, max_games):
            games += 1
            if candidate_from_game(game, filters, rng) is not None:
                eligible += 1
    print(f"Games clearing Elo {filters.min_elo}: {games}")
    print(f"Of those, with a position passing cheap filters: {eligible}")
    if games:
        print(f"Cheap-filter yield: {eligible/games:.1%}")
    print("\nEngine filters (evaluation, best-move margin) will reduce this further.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sample balanced middlegame positions")
    ap.add_argument("--pgn", action="append", required=True, type=Path,
                    help="Source PGN (repeatable). .pgn/.gz/.bz2/.zst")
    ap.add_argument("--target", type=int, default=200, help="Positions wanted")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--version", default="v1")
    ap.add_argument("--depth", type=int, default=ENGINE_PARAMS["depth"])
    ap.add_argument("--max-games", type=int, default=None,
                    help="Stop after reading this many games per file")
    ap.add_argument("--describe", action="store_true",
                    help="Report cheap-filter yield without running the engine")
    ap.add_argument("--out", type=Path, default=None)

    ap.add_argument("--min-elo", type=int, default=2000)
    ap.add_argument("--ply-min", type=int, default=20)
    ap.add_argument("--ply-max", type=int, default=30)
    ap.add_argument("--max-eval", type=int, default=50,
                    help="Max |eval| in centipawns (50 accommodates White's "
                         "first-move advantage; see PositionFilters)")
    ap.add_argument("--max-margin", type=int, default=200,
                    help="Reject if best move beats second-best by more than this")
    args = ap.parse_args()

    missing = [p for p in args.pgn if not p.is_file()]
    if missing:
        for p in missing:
            print(f"Not found: {p}")
        return 1

    filters = PositionFilters(
        min_elo=args.min_elo,
        ply_min=args.ply_min,
        ply_max=args.ply_max,
        max_abs_eval_cp=args.max_eval,
        max_best_move_margin_cp=args.max_margin,
    )

    if args.describe:
        describe(args.pgn, filters, args.max_games)
        return 0

    def progress(n, target, stats):
        if n % 10 == 0 or n == target:
            print(f"  {n}/{target} accepted "
                  f"({stats['games_read']} games read)", flush=True)

    try:
        oracle_cm = Oracle(depth=args.depth)
        oracle_cm.__enter__()
    except FileNotFoundError:
        print("Stockfish not found. brew install stockfish, or set STOCKFISH_PATH.")
        return 1

    try:
        print(f"Engine: {oracle_cm.engine_id} | depth {args.depth}")
        print(f"Filters: Elo>={filters.min_elo}, ply {filters.ply_min}-{filters.ply_max}, "
              f"|eval|<={filters.max_abs_eval_cp}cp, margin<={filters.max_best_move_margin_cp}cp\n")
        positions, stats = sample_positions(
            pgn_paths=args.pgn,
            oracle=oracle_cm,
            filters=filters,
            target=args.target,
            seed=args.seed,
            max_games=args.max_games,
            progress=progress,
        )
        provenance = oracle_cm.provenance()
    finally:
        oracle_cm.__exit__(None, None, None)

    if not positions:
        print("\nNo positions accepted. Try relaxing --max-eval or --max-margin, "
              "or supplying more games.")
        return 1

    payload = build_position_set(
        positions=positions,
        filters=filters,
        seed=args.seed,
        version=args.version,
        engine_provenance=provenance,
        sources=[p.name for p in args.pgn],
        stats=stats,
    )

    POSITIONS_DIR.mkdir(exist_ok=True)
    out = args.out or POSITIONS_DIR / f"positions_{args.version}.json"
    out.write_text(json.dumps(payload, indent=2))

    print(f"\nWrote {len(positions)} positions to {out}")
    print(f"  games read:        {stats['games_read']}")
    print(f"  no eligible ply:   {stats['rejected_no_eligible_ply']}")
    print(f"  duplicate FEN:     {stats['rejected_duplicate']}")
    print(f"  failed evaluation: {stats['rejected_eval']}")
    print(f"  failed margin:     {stats['rejected_margin']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
