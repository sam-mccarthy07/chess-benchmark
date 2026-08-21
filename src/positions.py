"""Balanced middlegame position sampling.

Games start from a sampled position rather than move 1, for two reasons.

Opening play is largely recall, so every agent proposes the same book move and
the influence metrics floor near zero for the first twenty-odd plies — no
deliberative signal, full API cost. And a team handed a winning or losing
position is not being measured on coordination, so start positions must be
balanced or the effect we care about is confounded with luck of the draw.

Filters, in the order applied (cheapest first):

  Elo             both players above a rating floor
  ply window      established middlegame, before endgame simplification
  piece count     enough material left to give real branching
  material        roughly symmetric
  evaluation      |eval| within a threshold, so neither side is already better
  best-move gap   best move must NOT dominate the alternatives; a position with
                  one obviously correct move has nothing to deliberate about

At most one position is taken per source game. Positions from the same game
share structure and would not be independent samples.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import random
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator, Optional

import chess
import chess.pgn

PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
}


@dataclass
class PositionFilters:
    """Every threshold that decides whether a position enters the set."""
    min_elo: int = 2000
    ply_min: int = 20
    ply_max: int = 30
    min_piece_count: int = 20
    max_material_diff: int = 2
    # 50, not 30. White's first-move advantage is real and persistent: measured
    # against Stockfish 18, the starting position is +44 at depth 20, and
    # textbook-balanced openings sit between +29 and +57 (1.e4 e5 = +41,
    # 1.d4 d5 = +34, Berlin = +29). A ±30 gate would reject the starting
    # position itself and would not select "balanced" positions at all — it
    # would select the biased subset where White's normal edge has already been
    # given away. ±50 admits the ordinary range while still excluding
    # positions where one side is genuinely better.
    max_abs_eval_cp: int = 50
    # Below this margin the best move does not stand clear of the second-best,
    # which is what we want: a genuine choice.
    min_best_move_margin_cp: int = 0
    max_best_move_margin_cp: int = 200


@dataclass
class SampledPosition:
    position_id: str
    fen: str
    ply: int
    eval_cp: int
    best_move: str
    best_move_margin_cp: Optional[int]
    piece_count: int
    material_diff: int
    source: dict = field(default_factory=dict)


def open_pgn(path: Path) -> io.TextIOBase:
    """Open a PGN, transparently handling the compressions the sources ship.

    Lichess Elite ships .zip; the full Lichess dumps ship .zst, which needs the
    optional `zstandard` package. zip, gz and bz2 are all stdlib. Uncompressed
    .pgn always works.

    Everything streams — a monthly Elite file is ~270MB of PGN uncompressed and
    the full dumps are far larger, so nothing is read into memory whole.
    """
    suffix = path.suffix.lower()
    if suffix == ".zip":
        zf = zipfile.ZipFile(path)
        members = [n for n in zf.namelist() if n.lower().endswith(".pgn")]
        if not members:
            raise RuntimeError(f"{path.name} contains no .pgn file")
        if len(members) > 1:
            raise RuntimeError(
                f"{path.name} contains {len(members)} PGN files; extract the one you want"
            )
        return io.TextIOWrapper(zf.open(members[0]), encoding="utf-8", errors="replace")
    if suffix == ".bz2":
        return io.TextIOWrapper(bz2.open(path, "rb"), encoding="utf-8", errors="replace")
    if suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    if suffix == ".zst":
        try:
            import zstandard
        except ImportError as e:
            raise RuntimeError(
                f"{path.name} is zstd-compressed. Either `pip install zstandard`, "
                f"or decompress it first: `zstd -d {path.name}`"
            ) from e
        dctx = zstandard.ZstdDecompressor()
        return io.TextIOWrapper(
            dctx.stream_reader(path.open("rb")), encoding="utf-8", errors="replace"
        )
    return path.open("r", encoding="utf-8", errors="replace")


def _elo(headers, key: str) -> Optional[int]:
    try:
        return int(headers.get(key, ""))
    except (TypeError, ValueError):
        return None


def iter_games(path: Path, min_elo: int, max_games: Optional[int] = None) -> Iterator[chess.pgn.Game]:
    """Stream games from a PGN, keeping those where both players clear min_elo.

    Streaming matters: Lichess monthly dumps are far too large to hold in memory.
    """
    seen = 0
    with open_pgn(path) as fh:
        while True:
            if max_games is not None and seen >= max_games:
                return
            try:
                game = chess.pgn.read_game(fh)
            except Exception:
                continue
            if game is None:
                return
            seen += 1
            w, b = _elo(game.headers, "WhiteElo"), _elo(game.headers, "BlackElo")
            if w is None or b is None or w < min_elo or b < min_elo:
                continue
            yield game


def material_profile(board: chess.Board) -> tuple[int, int]:
    """(piece count excluding kings, |material difference| in pawns)."""
    count = 0
    diff = 0
    for piece_type, value in PIECE_VALUES.items():
        w = len(board.pieces(piece_type, chess.WHITE))
        b = len(board.pieces(piece_type, chess.BLACK))
        count += w + b
        diff += (w - b) * value
    return count, abs(diff)


def candidate_from_game(
    game: chess.pgn.Game,
    filters: PositionFilters,
    rng: random.Random,
) -> Optional[tuple[chess.Board, int]]:
    """Pick at most one cheap-filter-passing position from a game.

    Chosen at random within the ply window rather than always taking the same
    ply, so the set is not biased toward one phase, and only one position per
    game so samples stay independent.
    """
    board = game.board()
    eligible: list[tuple[str, int]] = []

    for ply, move in enumerate(game.mainline_moves(), start=1):
        board.push(move)
        if ply < filters.ply_min:
            continue
        if ply > filters.ply_max:
            break
        count, diff = material_profile(board)
        if count < filters.min_piece_count or diff > filters.max_material_diff:
            continue
        if board.is_game_over():
            continue
        eligible.append((board.fen(), ply))

    if not eligible:
        return None
    fen, ply = rng.choice(eligible)
    return chess.Board(fen), ply


def position_id(fen: str) -> str:
    return hashlib.sha256(fen.encode()).hexdigest()[:12]


def engine_accepts(
    oracle, board: chess.Board, filters: PositionFilters
) -> tuple[bool, Optional[int], str, Optional[int]]:
    """Apply the two expensive filters. Returns (ok, eval_cp, best_move, margin)."""
    eval_cp, best = oracle.best_move(board)

    if abs(eval_cp) > filters.max_abs_eval_cp:
        return False, eval_cp, best, None

    margin = oracle.best_move_margin(board)
    if margin is None:
        return False, eval_cp, best, None
    if not (filters.min_best_move_margin_cp <= margin <= filters.max_best_move_margin_cp):
        return False, eval_cp, best, margin

    return True, eval_cp, best, margin


def sample_positions(
    pgn_paths: list[Path],
    oracle,
    filters: PositionFilters,
    target: int,
    seed: int,
    max_games: Optional[int] = None,
    progress=None,
) -> tuple[list[SampledPosition], dict]:
    """Build a position set. Returns (positions, rejection tally)."""
    rng = random.Random(seed)
    out: list[SampledPosition] = []
    seen_fens: set[str] = set()
    stats = {
        "games_read": 0,
        "rejected_no_eligible_ply": 0,
        "rejected_duplicate": 0,
        "rejected_eval": 0,
        "rejected_margin": 0,
        "accepted": 0,
    }

    for path in pgn_paths:
        for game in iter_games(path, filters.min_elo, max_games):
            if len(out) >= target:
                break
            stats["games_read"] += 1

            picked = candidate_from_game(game, filters, rng)
            if picked is None:
                stats["rejected_no_eligible_ply"] += 1
                continue

            board, ply = picked
            fen = board.fen()
            if fen in seen_fens:
                stats["rejected_duplicate"] += 1
                continue

            ok, eval_cp, best, margin = engine_accepts(oracle, board, filters)
            if not ok:
                if eval_cp is not None and abs(eval_cp) > filters.max_abs_eval_cp:
                    stats["rejected_eval"] += 1
                else:
                    stats["rejected_margin"] += 1
                continue

            seen_fens.add(fen)
            count, diff = material_profile(board)
            out.append(SampledPosition(
                position_id=position_id(fen),
                fen=fen,
                ply=ply,
                eval_cp=eval_cp,
                best_move=best,
                best_move_margin_cp=margin,
                piece_count=count,
                material_diff=diff,
                source={
                    "event": game.headers.get("Event", ""),
                    # Lichess Elite strips Site to "?" but carries LichessURL,
                    # which is the only header that uniquely identifies the
                    # game. Without it a position cannot be traced back to its
                    # source, and independence cannot be audited.
                    "game_url": (
                        game.headers.get("LichessURL")
                        or game.headers.get("Site", "")
                    ),
                    # Opening identity matters as much as game identity: 200
                    # positions drawn from 200 distinct games are still
                    # correlated if they are all the same opening.
                    "eco": game.headers.get("ECO", ""),
                    "opening": game.headers.get("Opening", ""),
                    "time_control": game.headers.get("TimeControl", ""),
                    "date": game.headers.get("UTCDate", "") or game.headers.get("Date", ""),
                    "white_elo": _elo(game.headers, "WhiteElo"),
                    "black_elo": _elo(game.headers, "BlackElo"),
                },
            ))
            stats["accepted"] += 1
            if progress:
                progress(len(out), target, stats)

        if len(out) >= target:
            break

    return out, stats


def load_position_set(path: Path) -> dict:
    """Load a released position set, failing loudly on anything malformed.

    A position set is part of the experimental design, so a truncated or
    hand-edited file must not quietly produce a shorter run.
    """
    import json

    data = json.loads(path.read_text())
    positions = data.get("positions")
    if not positions:
        raise RuntimeError(f"{path.name} contains no positions")
    if data.get("count") != len(positions):
        raise RuntimeError(
            f"{path.name} declares count={data.get('count')} but holds "
            f"{len(positions)} positions"
        )
    for p in positions:
        if not p.get("fen") or not p.get("position_id"):
            raise RuntimeError(f"{path.name} has a position missing fen/position_id")
        chess.Board(p["fen"])  # raises if unparseable
    return data


def position_set_provenance(data: dict, path: Path) -> dict:
    """Identity of a position set, for the run manifest.

    Without this, games started from different position sets would carry
    indistinguishable manifests and could be pooled by mistake — the same
    failure mode the engine provenance block exists to prevent.
    """
    return {
        "file": path.name,
        "version": data.get("version"),
        "seed": data.get("seed"),
        "count": data.get("count"),
        "filters": data.get("filters"),
    }


def build_position_set(
    positions: list[SampledPosition],
    filters: PositionFilters,
    seed: int,
    version: str,
    engine_provenance: dict,
    sources: list[str],
    stats: dict,
) -> dict:
    """Assemble the released artefact.

    Carries everything needed to regenerate it: seed, filters, engine
    provenance and source files. A position set is part of the experimental
    design, so it is versioned and shipped alongside results.
    """
    return {
        "version": version,
        "seed": seed,
        "count": len(positions),
        "filters": asdict(filters),
        "engine_provenance": engine_provenance,
        "sources": sources,
        "sampling_stats": stats,
        "notes": (
            "One position per source game, chosen at random within the ply "
            "window. Positions from the same game are not independent samples."
        ),
        "positions": [asdict(p) for p in positions],
    }
