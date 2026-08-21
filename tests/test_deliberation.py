"""Deliberation protocol tests.

No network: the API layer is stubbed, so these exercise the round structure,
the visibility rules and the drift bookkeeping rather than model behaviour.

Run: python3 -W ignore::DeprecationWarning -m unittest discover -s tests -v
"""

import asyncio
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
    STATUS_OK, STATUS_API_ERROR,
)
from team import DeliberationRound, Team, drift_summary


def _agents(n=3):
    roles = ["strategist", "analyst", "critic", "fourth"][:n]
    return [
        AgentConfig(model="test/model", role=r, persona="p", org_id="o", org_name="o")
        for r in roles
    ]


def _team(rounds=2, n=3):
    return Team(
        org_id="o", org_name="o", description="d",
        agents=_agents(n),
        deliberation_style="consensus",
        submitter_rotation="round_robin",
        deliberation_rounds=rounds,
    )


def _proposal(role, move, conf=0.5, status=STATUS_OK):
    return MoveProposal(
        agent_role=role, model="test/model", proposed_move=move,
        reasoning="r", confidence=conf, status=status, legal=bool(move),
    )


class Recorder:
    """Stubs the two API calls and records what each agent was shown."""

    def __init__(self, script=None):
        # script[round_index][agent_role] -> move
        self.script = script or {}
        self.seen: list[dict] = []

    async def proposal(self, agent, board_fen, legal_moves, move_history, color, move_number):
        move = self.script.get(0, {}).get(agent.role, "e2e4")
        return _proposal(agent.role, move)

    async def revision(self, agent, own, others, board_fen, legal_moves,
                       color, move_number, round_index):
        self.seen.append({
            "round": round_index,
            "agent": agent.role,
            "own": own.proposed_move,
            "others": sorted(p.agent_role for p in others),
        })
        move = self.script.get(round_index, {}).get(agent.role, own.proposed_move)
        # Revisions return (public proposal, private note) — the split is what
        # keeps private content out of what teammates are shown.
        return _proposal(agent.role, move), PrivateNote(
            agent_role=agent.role, round_index=round_index, present=False,
        )

    async def submit(self, submitter, proposals, board_fen, legal_moves,
                     color, move_number, deliberation_style):
        return SubmitterDecision(
            submitted_move=proposals[0].proposed_move,
            submitter_role=submitter.role, model="test/model",
            rationale="r", proposals_considered=list(proposals),
            status=STATUS_OK, legal=True,
        )


def run_deliberation(team, script=None):
    rec = Recorder(script)
    async def go():
        with mock.patch.object(team_mod, "get_agent_proposal", new=rec.proposal), \
             mock.patch.object(team_mod, "get_agent_revision", new=rec.revision), \
             mock.patch.object(team_mod, "get_submitter_decision", new=rec.submit):
            return await team.deliberate(chess.Board(), "white")
    decision, rounds = asyncio.run(go())
    return decision, rounds, rec


class TestRoundStructure(unittest.TestCase):
    def test_two_rounds_produce_three_records(self):
        _, rounds, _ = run_deliberation(_team(rounds=2))
        self.assertEqual([r.round_index for r in rounds], [0, 1, 2])

    def test_zero_rounds_reproduces_propose_then_submit(self):
        _, rounds, rec = run_deliberation(_team(rounds=0))
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].round_index, 0)
        self.assertEqual(rec.seen, [], "no revision calls should be made")

    def test_every_agent_speaks_in_every_round(self):
        team = _team(rounds=2)
        _, rounds, _ = run_deliberation(team)
        for r in rounds:
            self.assertEqual(len(r.proposals), len(team.agents))

    def test_round_zero_is_independent(self):
        # Nothing is shown to anyone before the first discussion round, so no
        # revision call can carry round-0 context.
        _, _, rec = run_deliberation(_team(rounds=2))
        self.assertTrue(all(s["round"] >= 1 for s in rec.seen))

    def test_agents_see_every_teammate_and_never_themselves(self):
        _, _, rec = run_deliberation(_team(rounds=1))
        self.assertEqual(len(rec.seen), 3)
        for s in rec.seen:
            self.assertNotIn(s["agent"], s["others"], "an agent was shown its own proposal")
            self.assertEqual(len(s["others"]), 2)

    def test_revisions_build_on_the_previous_round(self):
        script = {0: {"strategist": "e2e4", "analyst": "d2d4", "critic": "g1f3"},
                  1: {"strategist": "b1c3"}}
        _, _, rec = run_deliberation(_team(rounds=2), script)
        round2 = [s for s in rec.seen if s["round"] == 2]
        self.assertEqual(
            next(s["own"] for s in round2 if s["agent"] == "strategist"), "b1c3",
            "round 2 should start from the round-1 position, not round 0",
        )

    def test_submitter_receives_the_final_round(self):
        script = {0: {"strategist": "e2e4"}, 2: {"strategist": "b1c3"}}
        decision, rounds, _ = run_deliberation(_team(rounds=2), script)
        self.assertEqual(rounds[-1].proposals[0].proposed_move, "b1c3")
        self.assertEqual(decision.submitted_move, "b1c3")


class TestUnanimity(unittest.TestCase):
    def test_all_agreeing_is_unanimous(self):
        r = DeliberationRound(0, [_proposal("a", "e2e4"), _proposal("b", "e2e4")])
        self.assertTrue(r.is_unanimous())

    def test_disagreement_is_not_unanimous(self):
        r = DeliberationRound(0, [_proposal("a", "e2e4"), _proposal("b", "d2d4")])
        self.assertFalse(r.is_unanimous())

    def test_a_single_surviving_voice_is_not_consensus(self):
        # Two agents failed; counting the survivor as unanimous would report
        # peak agreement exactly when the team has collapsed.
        r = DeliberationRound(0, [
            _proposal("a", "e2e4"),
            _proposal("b", "", status=STATUS_API_ERROR),
            _proposal("c", "", status=STATUS_API_ERROR),
        ])
        self.assertFalse(r.is_unanimous())

    def test_distinct_move_count_ignores_missing_proposals(self):
        r = DeliberationRound(0, [
            _proposal("a", "e2e4"), _proposal("b", "e2e4"),
            _proposal("c", "", status=STATUS_API_ERROR),
        ])
        self.assertEqual(r.distinct_moves(), 1)


class TestDriftSummary(unittest.TestCase):
    def test_detects_agents_that_changed_position(self):
        script = {0: {"strategist": "e2e4", "analyst": "d2d4", "critic": "g1f3"},
                  1: {"analyst": "e2e4"}, 2: {"critic": "e2e4"}}
        _, rounds, _ = run_deliberation(_team(rounds=2), script)
        d = drift_summary(rounds)
        self.assertEqual(d["drifted_agents"], 2)
        self.assertFalse(d["by_agent"]["strategist"]["drifted"])
        self.assertTrue(d["by_agent"]["analyst"]["drifted"])

    def test_convergence_requires_starting_split(self):
        script = {0: {"strategist": "e2e4", "analyst": "d2d4", "critic": "g1f3"},
                  1: {"analyst": "e2e4", "critic": "e2e4"}}
        _, rounds, _ = run_deliberation(_team(rounds=1), script)
        d = drift_summary(rounds)
        self.assertTrue(d["converged"])
        self.assertEqual(d["distinct_moves_by_round"], [3, 1])

    def test_already_unanimous_teams_do_not_count_as_converging(self):
        _, rounds, _ = run_deliberation(_team(rounds=2))  # all propose e2e4
        d = drift_summary(rounds)
        self.assertFalse(d["converged"], "unanimous from the start is not convergence")
        self.assertEqual(d["drifted_agents"], 0)

    def test_reports_discussion_rounds_not_total_records(self):
        _, rounds, _ = run_deliberation(_team(rounds=2))
        self.assertEqual(drift_summary(rounds)["rounds"], 2)

    def test_empty_input_is_safe(self):
        self.assertEqual(drift_summary([]), {})


class TestPromptNeutrality(unittest.TestCase):
    def test_discussion_prompt_does_not_push_for_or_against_change(self):
        # An anti-conformity instruction would suppress the behaviour H8 is
        # measuring; a change-seeking one would manufacture it. The prompt may
        # legitimise holding, but must not advocate either outcome.
        prompt = agents_mod.DISCUSSION_SYSTEM.lower()
        for banned in ("do not change", "resist", "stand firm", "defend your",
                       "you should change", "reach consensus", "agree with"):
            self.assertNotIn(banned, prompt, f"prompt steers deliberation: {banned!r}")
        self.assertIn("keep your move or change it", prompt)


class TestTeamConfig(unittest.TestCase):
    def test_rounds_come_from_org_config(self):
        cfg = {
            "id": "o", "name": "o", "description": "d",
            "agents": [{"model": "m", "role": "r", "persona": "p"}],
            "deliberation_style": "consensus",
            "submitter_rotation": "round_robin",
            "deliberation_rounds": 5,
        }
        self.assertEqual(team_mod.build_team(cfg, "white").deliberation_rounds, 5)

    def test_rounds_default_when_config_omits_them(self):
        cfg = {
            "id": "o", "name": "o", "description": "d",
            "agents": [{"model": "m", "role": "r", "persona": "p"}],
            "deliberation_style": "consensus",
            "submitter_rotation": "round_robin",
        }
        from config import DELIBERATION_ROUNDS
        self.assertEqual(team_mod.build_team(cfg, "white").deliberation_rounds, DELIBERATION_ROUNDS)


if __name__ == "__main__":
    unittest.main()
