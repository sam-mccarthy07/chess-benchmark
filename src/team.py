"""Multi-agent team orchestration.

A turn runs as:

    round 0   every agent proposes independently, in parallel, seeing nobody
              else. This is the uncontaminated pre-deliberation anchor — the
              only point at which an agent's position is its own.
    round 1+  every agent sees all current positions and may revise. Agents
              revise in parallel within a round and sequentially across
              rounds, so no agent is privileged by ordering.
    submit    the submitter chooses from the final round.

Configuring 0 rounds reproduces the original propose-then-submit behaviour and
is kept as an ablation arm: 0 against 2 is how we test whether discussion buys
agreement without buying quality.
"""

import asyncio
from dataclasses import dataclass, field, asdict
from typing import Optional
import chess

from agents import (
    AgentConfig,
    MoveProposal,
    SubmitterDecision,
    get_agent_proposal,
    get_agent_revision,
    get_submitter_decision,
    STATUS_OK,
)
from config import DELIBERATION_ROUNDS


@dataclass
class DeliberationRound:
    """One synchronous round of positions, one per agent."""
    round_index: int  # 0 == independent opening proposals
    proposals: list[MoveProposal]

    def moves(self) -> list[str]:
        return [p.proposed_move for p in self.proposals if p.proposed_move]

    def distinct_moves(self) -> int:
        return len(set(self.moves()))

    def is_unanimous(self) -> bool:
        """All agents that produced a usable move agree.

        Requires at least two usable moves — one agent agreeing with itself is
        not consensus, and counting it as such would inflate unanimity exactly
        when the team is falling apart.
        """
        moves = self.moves()
        return len(moves) >= 2 and len(set(moves)) == 1


@dataclass
class Team:
    org_id: str
    org_name: str
    description: str
    agents: list[AgentConfig]
    deliberation_style: str  # "consensus" | "advisory"
    submitter_rotation: str  # "round_robin" | "fixed_leader"
    deliberation_rounds: int = DELIBERATION_ROUNDS
    move_count: int = 0
    submitter_index: int = 0

    def get_submitter(self) -> AgentConfig:
        """Get the current submitter agent."""
        if self.submitter_rotation == "fixed_leader":
            return self.agents[0]
        return self.agents[self.submitter_index % len(self.agents)]

    def get_deliberators(self) -> list[AgentConfig]:
        """Agents that submit proposals (all of them, including the submitter)."""
        return self.agents

    def advance_submitter(self):
        """Move to next submitter for next move."""
        if self.submitter_rotation == "round_robin":
            self.submitter_index = (self.submitter_index + 1) % len(self.agents)

    async def deliberate(
        self,
        board: chess.Board,
        color: str,
    ) -> tuple[SubmitterDecision, list[DeliberationRound]]:
        """Run a full deliberation and return the decision plus every round."""
        self.move_count += 1
        move_number = self.move_count

        board_fen = board.fen()
        legal_moves = [m.uci() for m in board.legal_moves]
        history = [str(m) for m in board.move_stack[-10:]]
        deliberators = self.get_deliberators()

        # Round 0 — independent. Nobody sees anybody.
        opening = await asyncio.gather(*[
            get_agent_proposal(
                agent=agent,
                board_fen=board_fen,
                legal_moves=legal_moves,
                move_history=history,
                color=color,
                move_number=move_number,
            )
            for agent in deliberators
        ])
        rounds = [DeliberationRound(round_index=0, proposals=list(opening))]

        # Discussion rounds — simultaneous revision.
        for round_index in range(1, self.deliberation_rounds + 1):
            current = rounds[-1].proposals
            revised = await asyncio.gather(*[
                get_agent_revision(
                    agent=agent,
                    own=current[i],
                    others=[p for j, p in enumerate(current) if j != i],
                    board_fen=board_fen,
                    legal_moves=legal_moves,
                    color=color,
                    move_number=move_number,
                    round_index=round_index,
                )
                for i, agent in enumerate(deliberators)
            ])
            rounds.append(DeliberationRound(round_index=round_index, proposals=list(revised)))

        decision = await get_submitter_decision(
            submitter=self.get_submitter(),
            proposals=rounds[-1].proposals,
            board_fen=board_fen,
            legal_moves=legal_moves,
            color=color,
            move_number=move_number,
            deliberation_style=self.deliberation_style,
        )

        self.advance_submitter()
        return decision, rounds


def drift_summary(rounds: list[DeliberationRound]) -> dict:
    """How positions moved over the course of deliberation.

    `drifted` counts agents whose final move differs from the one they reached
    independently in round 0 — deliberation changed their mind. Paired with
    move quality it separates productive persuasion from mere conformity;
    on its own it is unsigned.
    """
    if not rounds:
        return {}

    first, last = rounds[0].proposals, rounds[-1].proposals
    drifted = sum(
        1
        for a, b in zip(first, last)
        if a.proposed_move and b.proposed_move and a.proposed_move != b.proposed_move
    )
    by_agent = {
        a.agent_role: {
            "initial_move": a.proposed_move,
            "final_move": b.proposed_move,
            "drifted": bool(a.proposed_move and b.proposed_move and a.proposed_move != b.proposed_move),
            "initial_confidence": a.confidence,
            "final_confidence": b.confidence,
        }
        for a, b in zip(first, last)
    }
    return {
        "rounds": len(rounds) - 1,  # discussion rounds, excluding the opening
        "drifted_agents": drifted,
        "distinct_moves_by_round": [r.distinct_moves() for r in rounds],
        "unanimous_by_round": [r.is_unanimous() for r in rounds],
        "converged": (not rounds[0].is_unanimous()) and rounds[-1].is_unanimous(),
        "by_agent": by_agent,
    }


def build_team(org_config: dict, color: str) -> Team:
    """Build a Team from config dict."""
    agents = [
        AgentConfig(
            model=a["model"],
            role=a["role"],
            persona=a["persona"],
            org_id=org_config["id"],
            org_name=org_config["name"],
        )
        for a in org_config["agents"]
    ]
    return Team(
        org_id=org_config["id"],
        org_name=org_config["name"],
        description=org_config["description"],
        agents=agents,
        deliberation_style=org_config["deliberation_style"],
        submitter_rotation=org_config["submitter_rotation"],
        deliberation_rounds=org_config.get("deliberation_rounds", DELIBERATION_ROUNDS),
    )
