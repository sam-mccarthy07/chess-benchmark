"""Integrity contract tests.

These exist to prevent a specific regression: the harness must never record a
move the model did not produce. Before this suite, a failed proposal was
silently replaced with `legal_moves[0]` and logged as though the agent had
proposed it, which made illegal-move rates unmeasurable and made a crashed
agent indistinguishable from a competent one.

Run: python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chess

from agents import (
    MoveProposal,
    SubmitterDecision,
    _parse_move,
    _classify,
    STATUS_OK,
    STATUS_ILLEGAL,
    STATUS_UNPARSEABLE,
    STATUS_API_ERROR,
)
from game import resolve_move, integrity_summary
from config import config_fingerprint, build_manifest, HARNESS_PARAMS


START_FEN = chess.Board().fen()


def _proposal(role, move, confidence=0.5, status=STATUS_OK, legal=True):
    return MoveProposal(
        agent_role=role,
        model="test/model",
        proposed_move=move,
        reasoning="because",
        confidence=confidence,
        status=status,
        legal=legal,
    )


def _decision(move, status=STATUS_OK, legal=True, proposals=None):
    return SubmitterDecision(
        submitted_move=move,
        submitter_role="leader",
        model="test/model",
        rationale="because",
        proposals_considered=proposals or [],
        status=status,
        legal=legal,
    )


class TestParsing(unittest.TestCase):
    """Parsing recovers what the model meant. It never invents a move."""

    def test_parses_json_uci(self):
        self.assertEqual(_parse_move('{"move": "e2e4"}', START_FEN), "e2e4")

    def test_parses_bare_uci_in_prose(self):
        self.assertEqual(_parse_move("I think e2e4 is best.", START_FEN), "e2e4")

    def test_parses_san_to_uci(self):
        # SAN -> UCI is interpretation, not substitution.
        self.assertEqual(_parse_move("I play Nf3", START_FEN), "g1f3")

    def test_returns_illegal_move_verbatim(self):
        # e2e5 is not legal from the start position. It must survive parsing
        # unchanged so that legality can be judged and recorded.
        self.assertEqual(_parse_move('{"move": "e2e5"}', START_FEN), "e2e5")

    def test_returns_empty_when_no_move_present(self):
        self.assertEqual(_parse_move("I refuse to answer.", START_FEN), "")

    def test_no_move_is_not_replaced_with_a_legal_one(self):
        legal = [m.uci() for m in chess.Board().legal_moves]
        parsed = _parse_move("nothing move-shaped here", START_FEN)
        self.assertNotIn(parsed, legal)
        self.assertEqual(parsed, "")


class TestClassification(unittest.TestCase):
    def setUp(self):
        self.legal = [m.uci() for m in chess.Board().legal_moves]

    def test_legal_move_is_ok(self):
        self.assertEqual(_classify("e2e4", self.legal), (STATUS_OK, True))

    def test_illegal_move_flagged_not_replaced(self):
        status, legal = _classify("e2e5", self.legal)
        self.assertEqual(status, STATUS_ILLEGAL)
        self.assertFalse(legal)

    def test_empty_move_is_unparseable(self):
        self.assertEqual(_classify("", self.legal), (STATUS_UNPARSEABLE, False))


class TestResolution(unittest.TestCase):
    """The board gets a playable move; the record keeps the truth."""

    def setUp(self):
        self.board = chess.Board()

    def test_legal_decision_played_as_decided(self):
        d = _decision("e2e4")
        move, res = resolve_move(d, [_proposal("a", "e2e4")], self.board)
        self.assertEqual(move.uci(), "e2e4")
        self.assertEqual(res["method"], "as_decided")

    def test_illegal_decision_is_not_mutated(self):
        # The critical assertion: falling back must not rewrite the decision.
        d = _decision("e2e5", status=STATUS_ILLEGAL, legal=False)
        props = [_proposal("a", "d2d4", confidence=0.9)]
        move, res = resolve_move(d, props, self.board)

        self.assertEqual(d.submitted_move, "e2e5", "decision was rewritten")
        self.assertEqual(d.status, STATUS_ILLEGAL)
        self.assertEqual(move.uci(), "d2d4")
        self.assertEqual(res["method"], "fallback_to_proposal")
        self.assertEqual(res["played_move"], "d2d4")

    def test_proposals_are_not_mutated_by_resolution(self):
        d = _decision("e2e5", status=STATUS_ILLEGAL, legal=False)
        bad = _proposal("a", "h1h8", status=STATUS_ILLEGAL, legal=False)
        good = _proposal("b", "g1f3", confidence=0.3)
        resolve_move(d, [bad, good], self.board)
        self.assertEqual(bad.proposed_move, "h1h8")
        self.assertEqual(bad.status, STATUS_ILLEGAL)
        self.assertEqual(good.proposed_move, "g1f3")

    def test_falls_back_to_first_legal_when_nothing_playable(self):
        d = _decision("e2e5", status=STATUS_ILLEGAL, legal=False)
        props = [_proposal("a", "h1h8", status=STATUS_ILLEGAL, legal=False)]
        move, res = resolve_move(d, props, self.board)
        self.assertEqual(res["method"], "fallback_first_legal")
        self.assertIn(move, self.board.legal_moves)
        self.assertEqual(d.submitted_move, "e2e5")

    def test_fallback_is_deterministic(self):
        d = _decision("", status=STATUS_UNPARSEABLE, legal=False)
        a, _ = resolve_move(d, [], chess.Board())
        b, _ = resolve_move(d, [], chess.Board())
        self.assertEqual(a.uci(), b.uci())

    def test_empty_decision_does_not_crash(self):
        d = _decision("", status=STATUS_API_ERROR, legal=False)
        move, res = resolve_move(d, [], self.board)
        self.assertIn(move, self.board.legal_moves)
        self.assertEqual(res["method"], "fallback_first_legal")


class TestIntegritySummary(unittest.TestCase):
    """Illegal, unparseable and API-error must never be conflated."""

    def test_failure_modes_counted_separately(self):
        props = [
            _proposal("a", "e2e4"),
            _proposal("b", "e2e5", status=STATUS_ILLEGAL, legal=False),
            _proposal("c", "", status=STATUS_UNPARSEABLE, legal=False),
            _proposal("d", "", status=STATUS_API_ERROR, legal=False),
        ]
        s = integrity_summary(props, _decision("e2e4"))
        self.assertEqual(s["ok_proposals"], 1)
        self.assertEqual(s["illegal_proposals"], 1)
        self.assertEqual(s["unparseable_proposals"], 1)
        self.assertEqual(s["api_error_proposals"], 1)

    def test_false_consensus_flagged_when_team_ratifies_illegal_move(self):
        props = [_proposal("a", "e2e5", status=STATUS_ILLEGAL, legal=False)]
        s = integrity_summary(props, _decision("e2e5", status=STATUS_ILLEGAL, legal=False))
        self.assertTrue(s["false_consensus"])

    def test_false_consensus_not_flagged_on_legal_decision(self):
        s = integrity_summary([_proposal("a", "e2e4")], _decision("e2e4"))
        self.assertFalse(s["false_consensus"])

    def test_distinct_moves_ignores_empty(self):
        props = [
            _proposal("a", "e2e4"),
            _proposal("b", "e2e4"),
            _proposal("c", "", status=STATUS_UNPARSEABLE, legal=False),
        ]
        self.assertEqual(integrity_summary(props, _decision("e2e4"))["distinct_proposed_moves"], 1)


class TestManifest(unittest.TestCase):
    def test_fingerprint_is_stable(self):
        self.assertEqual(config_fingerprint(), config_fingerprint())

    def test_fingerprint_changes_with_harness_params(self):
        before = config_fingerprint()
        original = HARNESS_PARAMS["temperature_proposal"]
        HARNESS_PARAMS["temperature_proposal"] = original + 0.1
        try:
            self.assertNotEqual(before, config_fingerprint())
        finally:
            HARNESS_PARAMS["temperature_proposal"] = original
        self.assertEqual(before, config_fingerprint())

    def test_manifest_records_seed_and_fingerprint(self):
        m = build_manifest(seed=42)
        self.assertEqual(m["seed"], 42)
        self.assertEqual(m["schema_version"], 2)
        self.assertIn("config_fingerprint", m)
        self.assertFalse(m["harness"]["legal_moves_truncated"])


if __name__ == "__main__":
    unittest.main()
