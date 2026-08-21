"""Two-stream logging tests.

The property that matters is containment: private content must never reach
another agent, and must never be mistaken for public content by the parser.
These tests pin both, plus the influence metrics the split exists to enable.

Run: python3 -W ignore::DeprecationWarning -m unittest discover -s tests -v
"""

import asyncio
import json
import shutil
import sys
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

warnings.filterwarnings("ignore", message=".*iscoroutinefunction.*", category=DeprecationWarning)

import chess

import agents as agents_mod
import team as team_mod
from agents import (
    AgentConfig, MoveProposal, PrivateNote, SubmitterDecision,
    _split_streams, _parse_private, STATUS_OK, STATUS_API_ERROR,
)
from metrics import influence_metrics, influence_quality
from team import DeliberationRound, Team

ENGINE = shutil.which("stockfish")
LEGAL = [m.uci() for m in chess.Board().legal_moves]
START = chess.Board().fen()


def _response(public_move="e2e4", private_move="d2d4", split=True):
    if split:
        return json.dumps({
            "public": {"move": public_move, "reasoning": "public reason", "confidence": 0.8},
            "private": {
                "solo_move": private_move,
                "solo_rationale": "private reason",
                "process_note": "private process",
            },
        })
    return json.dumps({"move": public_move, "reasoning": "flat reason", "confidence": 0.8})


class TestStreamSplitting(unittest.TestCase):
    def test_split_response_separates_the_halves(self):
        pub, priv, status = _split_streams(_response())
        self.assertEqual(status, "split")
        self.assertIn("e2e4", pub)
        self.assertNotIn("d2d4", pub, "private move leaked into the public half")
        self.assertIn("d2d4", priv)

    def test_flat_response_is_treated_as_public_only(self):
        pub, priv, status = _split_streams(_response(split=False))
        self.assertEqual(status, "flat")
        self.assertEqual(priv, "", "private data must never be invented")

    def test_non_json_is_marked_unparsed(self):
        pub, priv, status = _split_streams("I choose e2e4.")
        self.assertEqual(status, "unparsed")
        self.assertEqual(priv, "")

    def test_public_parse_cannot_pick_up_the_private_move(self):
        # The whole point of splitting before parsing: the bare-UCI regex would
        # otherwise happily return the private solo_move as the proposal.
        pub, _, _ = _split_streams(_response(public_move="e2e4", private_move="a2a3"))
        self.assertEqual(agents_mod._parse_move(pub, START), "e2e4")


class TestPrivateNoteParsing(unittest.TestCase):
    def test_parses_a_well_formed_note(self):
        _, priv, _ = _split_streams(_response(private_move="d2d4"))
        note = _parse_private(priv, "analyst", 1, START, LEGAL)
        self.assertTrue(note.present)
        self.assertEqual(note.solo_move, "d2d4")
        self.assertTrue(note.solo_move_legal)
        self.assertEqual(note.solo_rationale, "private reason")

    def test_absent_block_yields_a_note_marked_absent(self):
        note = _parse_private("", "analyst", 1, START, LEGAL)
        self.assertFalse(note.present)
        self.assertEqual(note.solo_move, "")

    def test_illegal_solo_move_is_recorded_not_discarded(self):
        _, priv, _ = _split_streams(_response(private_move="e2e5"))
        note = _parse_private(priv, "analyst", 1, START, LEGAL)
        self.assertEqual(note.solo_move, "e2e5")
        self.assertFalse(note.solo_move_legal)

    def test_malformed_private_json_does_not_raise(self):
        note = _parse_private("{not json", "analyst", 1, START, LEGAL)
        self.assertFalse(note.present)


class TestContainment(unittest.TestCase):
    """Private content must not reach another agent."""

    def setUp(self):
        self.shown = []

    async def _proposal(self, agent, board_fen, legal_moves, move_history, color, move_number):
        return MoveProposal(
            agent_role=agent.role, model="m", proposed_move="e2e4",
            reasoning="opening reason", confidence=0.5, status=STATUS_OK, legal=True,
        )

    async def _revision(self, agent, own, others, board_fen, legal_moves,
                        color, move_number, round_index):
        # Record exactly what this agent was handed about its teammates.
        self.shown.append(json.dumps([o.__dict__ for o in others], default=str))
        p = MoveProposal(
            agent_role=agent.role, model="m", proposed_move="e2e4",
            reasoning="public only", confidence=0.5, status=STATUS_OK, legal=True,
        )
        n = PrivateNote(
            agent_role=agent.role, round_index=round_index,
            solo_move="d2d4", solo_rationale="SECRET_RATIONALE",
            process_note="SECRET_PROCESS", present=True,
        )
        return p, n

    async def _submit(self, submitter, proposals, board_fen, legal_moves,
                      color, move_number, deliberation_style):
        return SubmitterDecision(
            submitted_move="e2e4", submitter_role=submitter.role, model="m",
            rationale="r", proposals_considered=list(proposals),
            status=STATUS_OK, legal=True,
        )

    def _team(self, rounds=2):
        return Team(
            org_id="o", org_name="o", description="d",
            agents=[AgentConfig(model="m", role=r, persona="p", org_id="o", org_name="o")
                    for r in ("strategist", "analyst", "critic")],
            deliberation_style="consensus", submitter_rotation="round_robin",
            deliberation_rounds=rounds,
        )

    def _run(self, rounds=2):
        async def go():
            with mock.patch.object(team_mod, "get_agent_proposal", new=self._proposal), \
                 mock.patch.object(team_mod, "get_agent_revision", new=self._revision), \
                 mock.patch.object(team_mod, "get_submitter_decision", new=self._submit):
                return await self._team(rounds).deliberate(chess.Board(), "white")
        return asyncio.run(go())

    def test_no_private_content_is_shown_to_teammates(self):
        self._run()
        self.assertTrue(self.shown)
        for payload in self.shown:
            self.assertNotIn("SECRET_RATIONALE", payload)
            self.assertNotIn("SECRET_PROCESS", payload)
            self.assertNotIn("solo_move", payload)

    def test_private_notes_are_captured_on_the_round(self):
        _, rounds = self._run(rounds=2)
        self.assertEqual(rounds[0].private_notes, [], "round 0 has no private half")
        for r in rounds[1:]:
            self.assertEqual(len(r.private_notes), 3)
            self.assertTrue(all(n.present for n in r.private_notes))

    def test_proposals_carry_no_private_fields(self):
        _, rounds = self._run()
        for r in rounds:
            for p in r.proposals:
                self.assertNotIn("solo_move", p.__dict__)
                self.assertNotIn("process_note", p.__dict__)


class TestInfluenceMetrics(unittest.TestCase):
    def _turn(self, opening, decision, stated=None):
        return {
            "fen_before": START,
            "rounds": [{
                "round_index": 0,
                "proposals": [
                    {"agent_role": r, "proposed_move": m, "status": STATUS_OK}
                    for r, m in opening.items()
                ],
            }],
            "private_notes": [
                {"agent_role": r, "round_index": 1, "solo_move": m, "present": True}
                for r, m in (stated or {}).items()
            ],
            "decision": {"submitted_move": decision},
        }

    def test_agent_whose_opening_was_adopted_was_not_influenced(self):
        m = influence_metrics(self._turn({"a": "e2e4"}, "e2e4"))
        self.assertFalse(m["by_agent"]["a"]["ir_proposal"])
        self.assertEqual(m["ir_proposal_rate"], 0.0)

    def test_agent_moved_off_its_opening_is_flagged(self):
        m = influence_metrics(self._turn({"a": "d2d4"}, "e2e4"))
        self.assertTrue(m["by_agent"]["a"]["ir_proposal"])
        self.assertEqual(m["ir_proposal_rate"], 1.0)

    def test_stated_and_revealed_are_tracked_separately(self):
        # Agent opened on d2d4, team played e2e4, but the agent privately
        # claims it would have played e2e4 alone: it does not report the move.
        turn = self._turn({"a": "d2d4"}, "e2e4", stated={"a": "e2e4"})
        m = influence_metrics(turn)
        self.assertTrue(m["by_agent"]["a"]["ir_proposal"])
        self.assertFalse(m["by_agent"]["a"]["ir_stated"])
        self.assertEqual(m["introspective_gap"], -1.0, "silent conformity should read negative")

    def test_honest_self_report_gives_no_gap(self):
        turn = self._turn({"a": "d2d4"}, "e2e4", stated={"a": "d2d4"})
        self.assertEqual(influence_metrics(turn)["introspective_gap"], 0.0)

    def test_missing_private_notes_leave_stated_undefined(self):
        m = influence_metrics(self._turn({"a": "d2d4"}, "e2e4"))
        self.assertIsNone(m["ir_stated_rate"])
        self.assertIsNone(m["introspective_gap"])
        self.assertEqual(m["private_notes_present"], 0)

    def test_no_rounds_yields_empty(self):
        self.assertEqual(influence_metrics({"decision": {"submitted_move": "e2e4"}}), {})


@unittest.skipIf(ENGINE is None, "stockfish not installed")
class TestInfluenceQuality(unittest.TestCase):
    def test_separates_productive_persuasion_from_destructive_conformity(self):
        from oracle import Oracle
        from metrics import analyse_turn

        with Oracle(depth=8) as o:
            board = chess.Board()
            _, best = o.best_move(board)
            weak = min(
                (m.uci() for m in board.legal_moves if m.uci() != best),
                key=lambda u: -o.evaluate_move(board, u).cpl,
            )

            # Agent opened on the weak move; the team played the engine's best.
            turn = {
                "fen_before": START,
                "rounds": [{"round_index": 0, "proposals": [
                    {"agent_role": "a", "proposed_move": weak, "status": STATUS_OK},
                ]}],
                "private_notes": [],
                "decision": {"submitted_move": best},
                "proposals": [
                    {"agent_role": "a", "model": "m", "proposed_move": best,
                     "status": STATUS_OK, "confidence": 0.5},
                ],
                "resolution": {"played_move": best},
            }
            tm = analyse_turn(o, turn)
            q = influence_quality(o, turn, tm)

        self.assertEqual(q["destructive_conformity"], 0)
        self.assertEqual(q["productive_persuasion"], 1)
        self.assertEqual(q["by_agent"]["a"]["outcome"], "productive_persuasion")

    def test_agent_whose_move_was_adopted_is_not_counted_as_moved(self):
        from oracle import Oracle
        from metrics import analyse_turn

        with Oracle(depth=8) as o:
            _, best = o.best_move(chess.Board())
            turn = {
                "fen_before": START,
                "rounds": [{"round_index": 0, "proposals": [
                    {"agent_role": "a", "proposed_move": best, "status": STATUS_OK},
                ]}],
                "private_notes": [],
                "decision": {"submitted_move": best},
                "proposals": [
                    {"agent_role": "a", "model": "m", "proposed_move": best,
                     "status": STATUS_OK, "confidence": 0.5},
                ],
                "resolution": {"played_move": best},
            }
            q = influence_quality(o, turn, analyse_turn(o, turn))
        self.assertFalse(q["by_agent"]["a"]["moved"])


if __name__ == "__main__":
    unittest.main()
