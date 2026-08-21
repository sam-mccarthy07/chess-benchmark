"""Position sampler tests.

Cheap-filter and plumbing tests run everywhere. Engine-dependent tests skip
when Stockfish is absent.

Run: python3 -W ignore::DeprecationWarning -m unittest discover -s tests -v
"""

import bz2
import gzip
import random
import shutil
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

warnings.filterwarnings("ignore", message=".*iscoroutinefunction.*", category=DeprecationWarning)

import chess
import chess.pgn

from oracle import Oracle
from positions import (
    PositionFilters,
    build_position_set,
    candidate_from_game,
    engine_accepts,
    iter_games,
    material_profile,
    open_pgn,
    position_id,
    sample_positions,
)

ENGINE = shutil.which("stockfish")
requires_engine = unittest.skipIf(ENGINE is None, "stockfish not installed")
DEPTH = 8

# White to move; the black queen on d5 hangs to exd5. One move dominates, and
# the position is nowhere near balanced — useful for both engine filters.
QUEEN_HANGING = "rnb1kbnr/pppp1ppp/8/3q4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1"


def make_game(seed: int, plies: int = 40, white_elo: int = 2200, black_elo: int = 2100):
    """A deterministic pseudo-game, biased toward quiet moves so material stays high."""
    rng = random.Random(seed)
    board = chess.Board()
    moves = []
    for _ in range(plies):
        if board.is_game_over():
            break
        legal = list(board.legal_moves)
        quiet = [m for m in legal if not board.is_capture(m)]
        moves.append(rng.choice(quiet or legal))
        board.push(moves[-1])

    game = chess.pgn.Game()
    game.headers["WhiteElo"] = str(white_elo)
    game.headers["BlackElo"] = str(black_elo)
    game.headers["Event"] = f"fixture-{seed}"
    node = game
    for m in moves:
        node = node.add_variation(m)
    return game


def write_pgn(path: Path, games) -> Path:
    text = "\n\n".join(str(g) for g in games) + "\n"
    if path.suffix == ".gz":
        path.write_bytes(gzip.compress(text.encode()))
    elif path.suffix == ".bz2":
        path.write_bytes(bz2.compress(text.encode()))
    else:
        path.write_text(text)
    return path


class TestPgnIO(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.games = [make_game(i) for i in range(3)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_plain_gzip_and_bzip2(self):
        for name in ("g.pgn", "g.pgn.gz", "g.pgn.bz2"):
            path = write_pgn(self.tmp / name, self.games)
            with open_pgn(path) as fh:
                self.assertIn("[Event", fh.read(200), f"failed for {name}")

    def test_zst_without_library_explains_itself(self):
        path = self.tmp / "g.pgn.zst"
        path.write_bytes(b"not really zstd")
        try:
            import zstandard  # noqa: F401
            self.skipTest("zstandard installed; the guidance path is not taken")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as ctx:
            open_pgn(path)
        self.assertIn("zstandard", str(ctx.exception))


class TestEloFilter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_both_players_must_clear_the_floor(self):
        games = [
            make_game(1, white_elo=2400, black_elo=2400),
            make_game(2, white_elo=2400, black_elo=1500),  # black too weak
            make_game(3, white_elo=1200, black_elo=1200),
        ]
        path = write_pgn(self.tmp / "g.pgn", games)
        self.assertEqual(len(list(iter_games(path, min_elo=2000))), 1)

    def test_missing_elo_headers_are_excluded(self):
        g = make_game(1)
        del g.headers["WhiteElo"]
        path = write_pgn(self.tmp / "g.pgn", [g])
        self.assertEqual(len(list(iter_games(path, min_elo=2000))), 0)

    def test_max_games_caps_reading(self):
        path = write_pgn(self.tmp / "g.pgn", [make_game(i) for i in range(5)])
        self.assertEqual(len(list(iter_games(path, 2000, max_games=2))), 2)


class TestMaterialProfile(unittest.TestCase):
    def test_start_position(self):
        count, diff = material_profile(chess.Board())
        self.assertEqual(count, 30)  # 32 minus the two kings
        self.assertEqual(diff, 0)

    def test_detects_imbalance(self):
        # Black is a queen down.
        board = chess.Board("rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        _, diff = material_profile(board)
        self.assertEqual(diff, 9)


class TestCandidateSelection(unittest.TestCase):
    def setUp(self):
        self.filters = PositionFilters(ply_min=20, ply_max=30)

    def test_position_falls_inside_the_ply_window(self):
        picked = candidate_from_game(make_game(1), self.filters, random.Random(0))
        self.assertIsNotNone(picked)
        _, ply = picked
        self.assertGreaterEqual(ply, 20)
        self.assertLessEqual(ply, 30)

    def test_seed_makes_selection_reproducible(self):
        game = make_game(1)
        a = candidate_from_game(game, self.filters, random.Random(42))
        b = candidate_from_game(make_game(1), self.filters, random.Random(42))
        self.assertEqual(a[0].fen(), b[0].fen())
        self.assertEqual(a[1], b[1])

    def test_short_game_yields_nothing(self):
        short = make_game(1, plies=10)
        self.assertIsNone(candidate_from_game(short, self.filters, random.Random(0)))

    def test_piece_count_floor_is_enforced(self):
        strict = PositionFilters(ply_min=20, ply_max=30, min_piece_count=31)
        self.assertIsNone(candidate_from_game(make_game(1), strict, random.Random(0)))

    def test_position_id_is_stable_and_fen_specific(self):
        self.assertEqual(position_id("abc"), position_id("abc"))
        self.assertNotEqual(position_id("abc"), position_id("abd"))


@requires_engine
class TestEngineFilters(unittest.TestCase):
    def test_top_moves_returns_ranked_alternatives(self):
        with Oracle(depth=DEPTH) as o:
            top = o.top_moves(chess.Board(), n=3)
            self.assertEqual(len(top), 3)
            self.assertGreaterEqual(top[0][1], top[1][1], "results must be ranked")

    def test_margin_is_large_when_one_move_dominates(self):
        with Oracle(depth=DEPTH) as o:
            margin = o.best_move_margin(chess.Board(QUEEN_HANGING))
            self.assertIsNotNone(margin)
            self.assertGreater(margin, 200, "winning a queen should dominate")

    def test_margin_is_small_in_a_quiet_position(self):
        with Oracle(depth=DEPTH) as o:
            self.assertLess(o.best_move_margin(chess.Board()), 200)

    def test_single_legal_move_has_no_margin(self):
        # Black is in check with exactly one legal reply.
        board = chess.Board("k7/8/8/8/8/8/6q1/7K w - - 0 1")
        self.assertEqual(len(list(board.legal_moves)), 1)
        with Oracle(depth=DEPTH) as o:
            self.assertIsNone(o.best_move_margin(board))

    def test_balanced_quiet_position_is_accepted(self):
        filters = PositionFilters()
        with Oracle(depth=DEPTH) as o:
            ok, eval_cp, best, margin = engine_accepts(o, chess.Board(), filters)
            self.assertTrue(ok)
            self.assertLessEqual(abs(eval_cp), filters.max_abs_eval_cp)
            self.assertTrue(best)
            self.assertLessEqual(margin, filters.max_best_move_margin_cp)

    def test_eval_gate_accommodates_whites_first_move_advantage(self):
        # Regression guard on the threshold itself. A balanced position is not
        # a 0.00 position: White's opening edge is worth roughly +30 to +50cp,
        # so a gate tighter than that selects positions where White has already
        # gone slightly wrong rather than positions that are level.
        with Oracle(depth=DEPTH) as o:
            eval_cp, _ = o.best_move(chess.Board())
        self.assertGreater(
            abs(eval_cp), 30,
            "start position now evaluates under 30cp; the rationale for a 50cp "
            "gate should be re-checked against this engine build",
        )
        self.assertLessEqual(abs(eval_cp), PositionFilters().max_abs_eval_cp)

    def test_lopsided_position_is_rejected(self):
        with Oracle(depth=DEPTH) as o:
            ok, _, _, _ = engine_accepts(o, chess.Board(QUEEN_HANGING), PositionFilters())
            self.assertFalse(ok, "a hanging queen is not a balanced start")

    def test_only_move_position_is_rejected_even_when_level(self):
        # Margin filter alone, with the evaluation gate opened wide.
        filters = PositionFilters(max_abs_eval_cp=10_000, max_best_move_margin_cp=200)
        with Oracle(depth=DEPTH) as o:
            ok, _, _, margin = engine_accepts(o, chess.Board(QUEEN_HANGING), filters)
            self.assertFalse(ok)
            self.assertGreater(margin, 200)


@requires_engine
class TestSampling(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_pgn(self.tmp / "g.pgn", [make_game(i) for i in range(12)])
        # Engine gates opened so this exercises the pipeline, not chess luck.
        self.permissive = PositionFilters(max_abs_eval_cp=10_000, max_best_move_margin_cp=10_000)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stops_at_target(self):
        with Oracle(depth=DEPTH) as o:
            positions, _ = sample_positions([self.path], o, self.permissive, target=3, seed=1)
        self.assertEqual(len(positions), 3)

    def test_positions_are_unique_and_well_formed(self):
        with Oracle(depth=DEPTH) as o:
            positions, stats = sample_positions([self.path], o, self.permissive, target=5, seed=1)
        self.assertEqual(len({p.fen for p in positions}), len(positions))
        self.assertEqual(stats["accepted"], len(positions))
        for p in positions:
            chess.Board(p.fen)  # must parse
            self.assertTrue(p.best_move)
            self.assertGreaterEqual(p.ply, self.permissive.ply_min)

    def test_same_seed_gives_the_same_set(self):
        with Oracle(depth=DEPTH) as o:
            a, _ = sample_positions([self.path], o, self.permissive, target=4, seed=7)
            b, _ = sample_positions([self.path], o, self.permissive, target=4, seed=7)
        self.assertEqual([p.fen for p in a], [p.fen for p in b])

    def test_impossible_filters_yield_nothing_rather_than_failing(self):
        strict = PositionFilters(max_abs_eval_cp=0, max_best_move_margin_cp=0)
        with Oracle(depth=DEPTH) as o:
            positions, stats = sample_positions([self.path], o, strict, target=5, seed=1)
        self.assertEqual(positions, [])
        self.assertGreater(stats["games_read"], 0)

    def test_released_set_carries_everything_needed_to_regenerate_it(self):
        with Oracle(depth=DEPTH) as o:
            positions, stats = sample_positions([self.path], o, self.permissive, target=2, seed=3)
            payload = build_position_set(
                positions, self.permissive, seed=3, version="test",
                engine_provenance=o.provenance(), sources=["g.pgn"], stats=stats,
            )
        self.assertEqual(payload["seed"], 3)
        self.assertEqual(payload["count"], len(positions))
        self.assertEqual(payload["engine_provenance"]["depth"], DEPTH)
        self.assertIn("filters", payload)
        self.assertIn("sampling_stats", payload)


if __name__ == "__main__":
    unittest.main()
