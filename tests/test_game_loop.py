"""End-to-end game loop test with no network calls.

Verifies that a full game produces a well-formed record: every turn carries a
decision, a resolution, and integrity counters; illegal decisions survive into
the log unmodified; and game-level totals agree with the per-turn records.

Run: python3 -m unittest discover -s tests -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chess

import game as game_mod
from agents import (
    AgentConfig,
    MoveProposal,
    SubmitterDecision,
    STATUS_OK,
    STATUS_ILLEGAL,
)
from monitor import MoveAnalysis
from team import Team


def _team(org_id="test-org"):
    agents = [
        AgentConfig(model="test/model", role=r, persona="p", org_id=org_id, org_name=org_id)
        for r in ("strategist", "analyst", "critic")
    ]
    return Team(
        org_id=org_id,
        org_name=org_id,
        description="test",
        agents=agents,
        deliberation_style="consensus",
        submitter_rotation="round_robin",
    )


def _fake_analysis(**kwargs):
    return MoveAnalysis(
        move_number=kwargs.get("move_number", 0),
        org_id=kwargs.get("org_id", "test-org"),
        submitted_move=kwargs["decision"]["submitted_move"],
        agreement_level="unanimous",
        dominant_behavior="positional",
        deliberation_quality="high",
        key_insight="",
        dissent_detected=False,
        tokens_total=0,
        latency_total_ms=0.0,
    )


class FakeDeliberation:
    """Deterministic stand-in for a team's deliberation.

    Every `illegal_every` turns the team decides on an illegal move, so the
    resolution and false-consensus paths are exercised.
    """

    def __init__(self, illegal_every=3):
        self.illegal_every = illegal_every
        self.turn = 0

    async def __call__(self, board, color):
        self.turn += 1
        legal = [m.uci() for m in board.legal_moves]
        pick = sorted(legal)[0]

        force_illegal = self.illegal_every and self.turn % self.illegal_every == 0
        bad = "a1a1"  # never legal

        proposals = [
            MoveProposal(
                agent_role=role,
                model="test/model",
                proposed_move=pick,
                reasoning="r",
                confidence=0.5,
                status=STATUS_OK,
                legal=True,
            )
            for role in ("strategist", "analyst", "critic")
        ]
        if force_illegal:
            decision = SubmitterDecision(
                submitted_move=bad,
                submitter_role="strategist",
                model="test/model",
                rationale="r",
                proposals_considered=proposals,
                status=STATUS_ILLEGAL,
                legal=False,
            )
        else:
            decision = SubmitterDecision(
                submitted_move=pick,
                submitter_role="strategist",
                model="test/model",
                rationale="r",
                proposals_considered=proposals,
                status=STATUS_OK,
                legal=True,
            )
        return decision, proposals


class TestGameLoop(unittest.TestCase):
    def _run(self, illegal_every=3, max_moves=6):
        saved = {}

        async def go():
            fake = FakeDeliberation(illegal_every=illegal_every)
            with mock.patch.object(Team, "deliberate", new=fake), \
                 mock.patch.object(game_mod, "analyze_move", side_effect=_fake_analysis), \
                 mock.patch.object(game_mod, "save_game", new=lambda r: saved.setdefault("r", r)):
                return await game_mod.play_game(
                    white_team=_team("white-org"),
                    black_team=_team("black-org"),
                    max_moves=max_moves,
                    verbose=False,
                    seed=7,
                )

        return asyncio.run(go()), saved

    def test_produces_well_formed_record(self):
        result, saved = self._run()

        self.assertIs(saved["r"], result, "game was not persisted")
        self.assertTrue(result.moves)
        self.assertEqual(len(result.moves), result.total_moves)

        for turn in result.moves:
            self.assertIn("decision", turn)
            self.assertIn("resolution", turn)
            self.assertIn("integrity", turn)
            self.assertIn("fen_before", turn)
            # The position recorded is the one deliberated over, not the one
            # after the move was played.
            chess.Board(turn["fen_before"])
            self.assertGreater(turn["legal_move_count"], 0)

    def test_illegal_decisions_survive_into_the_log(self):
        result, _ = self._run(illegal_every=3)

        illegal_turns = [t for t in result.moves if t["decision"]["status"] == STATUS_ILLEGAL]
        self.assertTrue(illegal_turns, "fixture never produced an illegal decision")

        for turn in illegal_turns:
            # The decision keeps the impossible move...
            self.assertEqual(turn["decision"]["submitted_move"], "a1a1")
            self.assertFalse(turn["decision"]["legal"])
            self.assertTrue(turn["integrity"]["false_consensus"])
            # ...while the board received something else, recorded separately.
            self.assertNotEqual(turn["resolution"]["played_move"], "a1a1")
            self.assertEqual(turn["resolution"]["method"], "fallback_to_proposal")
            self.assertTrue(turn["resolution"]["note"])

    def test_played_moves_reconstruct_a_legal_game(self):
        result, _ = self._run()
        board = chess.Board()
        for turn in result.moves:
            uci = turn["resolution"]["played_move"]
            move = chess.Move.from_uci(uci)
            self.assertIn(move, board.legal_moves, f"illegal move in record: {uci}")
            board.push(move)

    def test_totals_agree_with_per_turn_records(self):
        result, _ = self._run()
        t = result.integrity_totals
        self.assertEqual(t["total_turns"], len(result.moves))
        self.assertEqual(
            t["false_consensus_events"],
            sum(1 for m in result.moves if m["integrity"]["false_consensus"]),
        )
        self.assertEqual(
            t["unresolved_decisions"],
            sum(1 for m in result.moves if m["resolution"]["method"] != "as_decided"),
        )

    def test_manifest_is_attached(self):
        result, _ = self._run()
        self.assertEqual(result.manifest["seed"], 7)
        self.assertIn("config_fingerprint", result.manifest)
        self.assertEqual(result.manifest["white_org"], "white-org")

    def test_clean_game_has_no_fallbacks(self):
        result, _ = self._run(illegal_every=0)
        self.assertEqual(result.integrity_totals["false_consensus_events"], 0)
        self.assertEqual(result.integrity_totals["unresolved_decisions"], 0)
        for turn in result.moves:
            self.assertEqual(turn["resolution"]["method"], "as_decided")


if __name__ == "__main__":
    unittest.main()
