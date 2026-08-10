"""Multi-agent team orchestration."""

import asyncio
from dataclasses import dataclass, field
from typing import Optional
import chess

from agents import AgentConfig, MoveProposal, SubmitterDecision, get_agent_proposal, get_submitter_decision


@dataclass
class Team:
    org_id: str
    org_name: str
    description: str
    agents: list[AgentConfig]
    deliberation_style: str  # "consensus" | "advisory"
    submitter_rotation: str  # "round_robin" | "fixed_leader"
    move_count: int = 0
    submitter_index: int = 0

    def get_submitter(self) -> AgentConfig:
        """Get the current submitter agent."""
        if self.submitter_rotation == "fixed_leader":
            # First agent is always the leader
            return self.agents[0]
        else:
            # Round-robin rotation
            return self.agents[self.submitter_index % len(self.agents)]

    def get_deliberators(self) -> list[AgentConfig]:
        """Get agents that submit proposals (all agents, including submitter)."""
        return self.agents

    def advance_submitter(self):
        """Move to next submitter for next move."""
        if self.submitter_rotation == "round_robin":
            self.submitter_index = (self.submitter_index + 1) % len(self.agents)

    async def deliberate(
        self,
        board: chess.Board,
        color: str,
    ) -> tuple[SubmitterDecision, list[MoveProposal]]:
        """Run full deliberation cycle and return final decision."""
        self.move_count += 1
        move_number = self.move_count

        board_fen = board.fen()
        legal_moves = [m.uci() for m in board.legal_moves]

        # All agents propose simultaneously
        proposal_tasks = [
            get_agent_proposal(
                agent=agent,
                board_fen=board_fen,
                legal_moves=legal_moves,
                move_history=[str(m) for m in board.move_stack[-10:]],
                color=color,
                move_number=move_number,
            )
            for agent in self.get_deliberators()
        ]
        proposals = await asyncio.gather(*proposal_tasks)
        proposals = list(proposals)

        # Submitter makes final call
        submitter = self.get_submitter()
        decision = await get_submitter_decision(
            submitter=submitter,
            proposals=proposals,
            board_fen=board_fen,
            legal_moves=legal_moves,
            color=color,
            move_number=move_number,
            deliberation_style=self.deliberation_style,
        )

        # Advance rotation for next move
        self.advance_submitter()

        return decision, proposals


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
    )
