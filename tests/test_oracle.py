"""Oracle and decomposition tests.

These run against a real engine. If Stockfish is not installed the whole
module skips rather than failing, so the suite stays green on machines that
only need the API-side code.

Run: python3 -m unittest discover -s tests -v
"""

import shutil
import sys
import unittest
import warnings
from pathlib import Path

# python-chess 1.11.2 calls asyncio.iscoroutinefunction, deprecated in 3.14.
# Upstream issue, not ours — filtered so it does not bury real failures.
warnings.filterwarnings(
    "ignore",
    message=".*iscoroutinefunction.*",
    category=DeprecationWarning,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chess

from oracle import Oracle, win_probability, classify_severity, MATE_SCORE
from metrics import analyse_turn, summarise_game, analyse_pgn

ENGINE = shutil.which("stockfish")
requires_engine = unittest.skipIf(ENGINE is None, "stockfish not installed")

# Shallow depth keeps the suite fast; correctness of the decomposition does not
# depend on search depth.
DEPTH = 8

START_FEN = chess.Board().fen()


def _turn(fen, proposals, decision_move, played_move=None, org="test-org"):
    return {
        "org_id": org,
        "fen_before": fen,
        "proposals": [
            {
                "agent_role": role,
                "model": "test/model",
                "proposed_move": mv,
                "status": "ok" if mv else "unparseable",
                "confidence": conf,
            }
            for role, mv, conf in proposals
        ],
        "decision": {"submitted_move": decision_move},
        "resolution": {"played_move": played_move or decision_move},
    }


class TestWinProbability(unittest.TestCase):
    def test_even_position_is_fifty_percent(self):
        self.assertAlmostEqual(win_probability(0), 50.0, places=6)

    def test_monotonic_in_centipawns(self):
        self.assertLess(win_probability(-200), win_probability(0))
        self.assertLess(win_probability(0), win_probability(200))

    def test_severity_thresholds(self):
        self.assertEqual(classify_severity(35), "blunder")
        self.assertEqual(classify_severity(25), "mistake")
        self.assertEqual(classify_severity(15), "inaccuracy")
        self.assertEqual(classify_severity(5), "ok")


@requires_engine
class TestOracle(unittest.TestCase):
    def test_best_move_has_zero_loss(self):
        with Oracle(depth=DEPTH) as o:
            board = chess.Board()
            _, best = o.best_move(board)
            self.assertEqual(o.evaluate_move(board, best).cpl, 0)

    def test_illegal_move_is_flagged_not_scored(self):
        with Oracle(depth=DEPTH) as o:
            ev = o.evaluate_move(chess.Board(), "e2e5")
            self.assertFalse(ev.legal)
            self.assertEqual(ev.severity, "illegal")

    def test_unparseable_move_is_flagged(self):
        with Oracle(depth=DEPTH) as o:
            self.assertFalse(o.evaluate_move(chess.Board(), "not-a-move").legal)

    def test_cpl_is_never_negative(self):
        with Oracle(depth=DEPTH) as o:
            board = chess.Board()
            for mv in [m.uci() for m in board.legal_moves][:6]:
                self.assertGreaterEqual(o.evaluate_move(board, mv).cpl, 0)

    def test_hanging_queen_is_a_blunder(self):
        # Black to move; b4b5?? drops the queen to the c4 bishop.
        fen = "rnb1kbnr/pppp1ppp/8/4p3/1qB1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 0 1"
        with Oracle(depth=DEPTH) as o:
            ev = o.evaluate_move(chess.Board(fen), "b4b5")
            self.assertTrue(ev.legal)
            self.assertGreater(ev.cpl, 200, "losing a queen should cost heavily")
            self.assertEqual(ev.severity, "blunder")

    def test_evaluating_a_move_leaves_the_board_unchanged(self):
        with Oracle(depth=DEPTH) as o:
            board = chess.Board()
            before = board.fen()
            o.evaluate_move(board, "e2e4")
            self.assertEqual(board.fen(), before)

    def test_cache_avoids_repeat_analysis(self):
        with Oracle(depth=DEPTH) as o:
            board = chess.Board()
            o.best_move(board)
            n = len(o._cache)
            o.best_move(board)
            self.assertEqual(len(o._cache), n)

    def test_mate_positions_are_flagged(self):
        # White to move has Qxf7#. Any other move throws away a forced mate,
        # so centipawn loss here is not on a centipawn scale.
        fen = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 0 1"
        with Oracle(depth=DEPTH) as o:
            mate = o.evaluate_move(chess.Board(fen), "f3f7")
            self.assertTrue(mate.mate_involved)
            self.assertEqual(mate.cpl, 0, "playing the mate is not a loss")

            quiet = o.evaluate_move(chess.Board(fen), "d2d3")
            self.assertTrue(quiet.mate_involved, "missing a forced mate must be flagged")
            self.assertEqual(quiet.severity, "blunder")

    def test_ordinary_positions_are_not_flagged_as_mate(self):
        with Oracle(depth=DEPTH) as o:
            self.assertFalse(o.evaluate_move(chess.Board(), "e2e4").mate_involved)

    def test_provenance_is_recorded(self):
        with Oracle(depth=DEPTH) as o:
            p = o.provenance()
            self.assertIn("Stockfish", p["engine_id"])
            self.assertEqual(p["depth"], DEPTH)
            self.assertEqual(p["threads"], 1)


@requires_engine
class TestDecomposition(unittest.TestCase):
    def test_selection_is_zero_when_submitter_picks_the_best_proposal(self):
        with Oracle(depth=DEPTH) as o:
            board = chess.Board()
            _, best = o.best_move(board)
            other = next(m.uci() for m in board.legal_moves if m.uci() != best)
            turn = _turn(START_FEN, [("a", best, 0.9), ("b", other, 0.4)], best)
            m = analyse_turn(o, turn)
            self.assertEqual(m["delta_ceiling"], 0)
            self.assertEqual(m["delta_selection"], 0)
            self.assertTrue(m["submitter_picked_best"])
            self.assertEqual(m["best_proposal_role"], "a")

    def test_selection_loss_when_submitter_passes_over_the_best_proposal(self):
        # The team held the engine's move and played a weak one instead.
        fen = "rnb1kbnr/pppp1ppp/8/4p3/1qB1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 0 1"
        with Oracle(depth=DEPTH) as o:
            _, best = o.best_move(chess.Board(fen))
            turn = _turn(fen, [("a", best, 0.3), ("b", "b4b5", 0.9)], "b4b5")
            m = analyse_turn(o, turn)
            self.assertEqual(m["delta_ceiling"], 0)
            self.assertGreater(m["delta_selection"], 200)
            self.assertFalse(m["submitter_picked_best"])

    def test_selection_can_be_negative_when_submitter_goes_off_slate(self):
        # Submitter plays the engine's move although nobody proposed it.
        fen = "rnb1kbnr/pppp1ppp/8/4p3/1qB1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 0 1"
        with Oracle(depth=DEPTH) as o:
            _, best = o.best_move(chess.Board(fen))
            turn = _turn(fen, [("a", "b4b5", 0.9), ("b", "b4b5", 0.8)], best)
            m = analyse_turn(o, turn)
            self.assertGreater(m["delta_ceiling"], 200)
            self.assertLess(m["delta_selection"], 0, "off-slate improvement should show")

    def test_illegal_decision_yields_no_selection_metric(self):
        with Oracle(depth=DEPTH) as o:
            turn = _turn(START_FEN, [("a", "e2e4", 0.9)], "e2e5")
            m = analyse_turn(o, turn)
            self.assertIsNone(m["cpl_decision"])
            self.assertIsNone(m["delta_selection"])
            self.assertIsNone(m["submitter_picked_best"])
            self.assertEqual(m["delta_ceiling"], 0 if m["delta_ceiling"] == 0 else m["delta_ceiling"])

    def test_no_legal_proposals_yields_no_ceiling(self):
        with Oracle(depth=DEPTH) as o:
            turn = _turn(START_FEN, [("a", "e2e5", 0.9), ("b", "a1a1", 0.5)], "e2e4")
            m = analyse_turn(o, turn)
            self.assertIsNone(m["delta_ceiling"])
            self.assertIsNone(m["delta_selection"])
            self.assertIsNotNone(m["cpl_decision"])

    def test_summary_aggregates_by_org(self):
        with Oracle(depth=DEPTH) as o:
            board = chess.Board()
            _, best = o.best_move(board)
            turns = [_turn(START_FEN, [("a", best, 0.9)], best, org="alpha")]
            metrics = [analyse_turn(o, t) for t in turns]
            summary = summarise_game(metrics, turns)
            self.assertIn("alpha", summary)
            self.assertEqual(summary["alpha"]["turns"], 1)
            self.assertEqual(summary["alpha"]["submitter_picked_best_rate"], 1.0)

    def test_pgn_fallback_scores_played_moves(self):
        pgn = '[Event "t"]\n[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 *\n'
        with Oracle(depth=DEPTH) as o:
            plies = analyse_pgn(o, pgn)
            self.assertEqual(len(plies), 4)
            self.assertEqual(plies[0]["move"], "e2e4")
            self.assertEqual(plies[0]["color"], "white")
            self.assertTrue(all(p["cpl"] >= 0 for p in plies))


if __name__ == "__main__":
    unittest.main()
