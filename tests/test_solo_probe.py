"""Solo probe and Collaborative Advantage tests.

CA is the headline metric, so the cases that matter most are the ones where it
must be *undefined* rather than zero — filling in a number when a comparison
cannot be made would quietly bias the mean toward whichever side failed.

Run: python3 -W ignore::DeprecationWarning -m unittest discover -s tests -v
"""

import asyncio
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
import solo_probe as sp
from agents import AgentConfig, MoveProposal, STATUS_OK, STATUS_ILLEGAL, STATUS_API_ERROR
from metrics import analyse_turn, collaborative_advantage, revealed_influence

ENGINE = shutil.which("stockfish")
requires_engine = unittest.skipIf(ENGINE is None, "stockfish not installed")
DEPTH = 8
START = chess.Board().fen()


def _turn(decision, opening=None):
    opening = opening or {"a": decision}
    return {
        "fen_before": START,
        "ply": 1,
        "org_id": "o",
        "color": "white",
        "move_number": 1,
        "rounds": [{"round_index": 0, "proposals": [
            {"agent_role": r, "model": "m", "proposed_move": mv, "status": STATUS_OK}
            for r, mv in opening.items()
        ]}],
        "proposals": [
            {"agent_role": r, "model": "m", "proposed_move": mv,
             "status": STATUS_OK, "confidence": 0.5}
            for r, mv in opening.items()
        ],
        "decision": {"submitted_move": decision},
        "resolution": {"played_move": decision},
        "private_notes": [],
    }


def _probes(moves: dict, legal=True):
    return {"ply": 1, "fen": START, "probes": [
        {"agent_role": r, "model": "m", "move": mv,
         "status": STATUS_OK if mv else STATUS_API_ERROR,
         "legal": legal and bool(mv), "confidence": 0.5}
        for r, mv in moves.items()
    ]}


class TestAgentReconstruction(unittest.TestCase):
    def test_roster_comes_from_the_record_not_the_config(self):
        # A config edited after the run must not change who gets probed.
        turn = _turn("e2e4", {"strategist": "e2e4", "analyst": "d2d4"})
        roles = [a.role for a in sp.agents_from_turn(turn)]
        self.assertEqual(roles, ["strategist", "analyst"])

    def test_agents_without_a_model_are_skipped(self):
        turn = _turn("e2e4")
        turn["rounds"][0]["proposals"].append({"agent_role": "ghost", "proposed_move": "d2d4"})
        self.assertEqual(len(sp.agents_from_turn(turn)), 1)

    def test_personas_are_looked_up_by_role(self):
        cfg = {"orgs": [{"id": "o", "agents": [
            {"role": "a", "persona": "aggressive"}, {"role": "b", "persona": "cautious"}]}]}
        self.assertEqual(sp.personas_for("o", cfg)["a"], "aggressive")
        self.assertEqual(sp.personas_for("missing", cfg), {})


class TestProbeCall(unittest.TestCase):
    def test_solo_prompt_does_not_mention_teammates(self):
        # If the solo prompt implied a team, the counterfactual would not be a
        # counterfactual.
        p = agents_mod.SOLO_SYSTEM.lower()
        for banned in ("team", "teammate", "colleague", "propose to", "your teammates"):
            self.assertNotIn(banned, p, f"solo prompt mentions {banned!r}")
        self.assertIn("deciding alone", p)

    def test_probe_asks_every_agent_on_the_position(self):
        seen = []

        async def fake(agent, board_fen, legal_moves, move_history, color, move_number):
            seen.append((agent.role, board_fen, len(legal_moves)))
            return MoveProposal(agent_role=agent.role, model="m", proposed_move="e2e4",
                                reasoning="r", confidence=0.5, status=STATUS_OK, legal=True)

        turn = _turn("d2d4", {"a": "d2d4", "b": "g1f3"})
        with mock.patch.object(sp, "get_solo_move", new=fake):
            out = asyncio.run(sp.probe_turn(turn, {}))

        self.assertEqual(sorted(r for r, _, _ in seen), ["a", "b"])
        self.assertTrue(all(fen == START for _, fen, _ in seen))
        self.assertTrue(all(n == 20 for _, _, n in seen), "full legal move list must be passed")
        self.assertEqual(len(out["probes"]), 2)
        self.assertEqual(out["ply"], 1)


@requires_engine
class TestCollaborativeAdvantage(unittest.TestCase):
    def _ca(self, decision, solo_moves):
        from oracle import Oracle
        with Oracle(depth=DEPTH) as o:
            turn = _turn(decision)
            tm = analyse_turn(o, turn, _probes(solo_moves))
            return tm["collaborative_advantage"]

    def test_positive_when_the_team_beats_its_best_member(self):
        from oracle import Oracle
        with Oracle(depth=DEPTH) as o:
            board = chess.Board()
            _, best = o.best_move(board)
            worst = max((m.uci() for m in board.legal_moves),
                        key=lambda u: o.evaluate_move(board, u).cpl)
        ca = self._ca(best, {"a": worst, "b": worst})
        self.assertGreater(ca["collaborative_advantage"], 0)
        self.assertTrue(ca["team_beat_best_member"])

    def test_negative_when_the_team_underperforms_its_best_member(self):
        # The collaboration gap: the team plays worse than a member would alone.
        from oracle import Oracle
        with Oracle(depth=DEPTH) as o:
            board = chess.Board()
            _, best = o.best_move(board)
            worst = max((m.uci() for m in board.legal_moves),
                        key=lambda u: o.evaluate_move(board, u).cpl)
        ca = self._ca(worst, {"a": best, "b": worst})
        self.assertLess(ca["collaborative_advantage"], 0)
        self.assertFalse(ca["team_beat_best_member"])
        self.assertEqual(ca["best_solo_role"], "a")

    def test_undefined_when_no_solo_move_was_legal(self):
        ca = self._ca("e2e4", {"a": "", "b": ""})
        self.assertIsNone(ca["collaborative_advantage"])
        self.assertIsNone(ca["best_solo_cpl"])

    def test_undefined_when_the_team_decision_was_illegal(self):
        # No comparison exists; a zero here would bias the mean.
        ca = self._ca("e2e5", {"a": "e2e4"})
        self.assertIsNone(ca["collaborative_advantage"])
        self.assertIsNotNone(ca["best_solo_cpl"])

    def test_absent_probes_yield_no_block_rather_than_zeros(self):
        from oracle import Oracle
        with Oracle(depth=DEPTH) as o:
            tm = analyse_turn(o, _turn("e2e4"), None)
        self.assertEqual(tm["collaborative_advantage"], {})
        self.assertEqual(tm["revealed_influence"], {})

    def test_illegal_solo_moves_are_excluded_from_the_minimum(self):
        from oracle import Oracle
        with Oracle(depth=DEPTH) as o:
            turn = _turn("e2e4")
            probes = _probes({"a": "e2e5", "b": "d2d4"})
            probes["probes"][0]["legal"] = False
            ca = collaborative_advantage(o, turn, analyse_turn(o, turn), probes)
        self.assertEqual(ca["best_solo_role"], "b")
        self.assertIsNone(ca["by_agent"]["a"]["cpl"])


class TestRevealedInfluence(unittest.TestCase):
    def test_agent_that_would_have_played_the_same_move_was_not_influenced(self):
        r = revealed_influence(_turn("e2e4"), _probes({"a": "e2e4"}))
        self.assertFalse(r["by_agent"]["a"]["ir_revealed"])
        self.assertEqual(r["ir_revealed_rate"], 0.0)

    def test_agent_that_would_have_played_differently_was_influenced(self):
        r = revealed_influence(_turn("e2e4"), _probes({"a": "d2d4", "b": "g1f3"}))
        self.assertEqual(r["ir_revealed_rate"], 1.0)

    def test_missing_solo_move_leaves_that_agent_undefined(self):
        r = revealed_influence(_turn("e2e4"), _probes({"a": "", "b": "d2d4"}))
        self.assertIsNone(r["by_agent"]["a"]["ir_revealed"])
        self.assertEqual(r["ir_revealed_rate"], 1.0, "rate should ignore undefined agents")

    def test_no_probes_yields_empty(self):
        self.assertEqual(revealed_influence(_turn("e2e4"), None), {})


if __name__ == "__main__":
    unittest.main()
