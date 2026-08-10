"""Agent communication via OpenRouter."""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from openai import AsyncOpenAI
import chess

from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL


@dataclass
class AgentConfig:
    model: str
    role: str
    persona: str
    org_id: str
    org_name: str


@dataclass
class MoveProposal:
    agent_role: str
    model: str
    proposed_move: str  # UCI notation
    reasoning: str
    confidence: float  # 0-1
    tokens_used: int = 0
    latency_ms: float = 0.0


@dataclass
class SubmitterDecision:
    submitted_move: str
    submitter_role: str
    model: str
    rationale: str
    proposals_considered: list[MoveProposal] = field(default_factory=list)
    tokens_used: int = 0
    latency_ms: float = 0.0


client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)


def _extract_move(text: str, board_fen: str = "", legal_uci: list[str] = None) -> str:
    """Extract UCI move from text, with SAN fallback using board context."""
    candidates = []

    # Try JSON first
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            candidates.append(data.get("move", "").strip())
    except Exception:
        pass

    # Look for explicit move: field (UCI pattern)
    m = re.search(r'"?move"?\s*[:=]\s*["\']?([a-h][1-8][a-h][1-8][qrbn]?)["\']?', text, re.IGNORECASE)
    if m:
        candidates.append(m.group(1).lower())

    # Look for standalone UCI move pattern
    for m in re.finditer(r'\b([a-h][1-8][a-h][1-8][qrbn]?)\b', text):
        candidates.append(m.group(1).lower())

    # If we have a board, validate UCI candidates and try SAN fallback
    if board_fen and legal_uci is not None:
        try:
            b = chess.Board(board_fen)
            legal_set = set(legal_uci)
            for c in candidates:
                if c in legal_set:
                    return c

            # SAN fallback: try to parse any chess notation found
            # Look for SAN-like tokens (O-O, O-O-O, piece moves, pawn moves)
            san_pattern = r'\b(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?|[a-h]x?[a-h][1-8](?:=[QRBN])?|[a-h][1-8])\b'
            for m in re.finditer(san_pattern, text):
                san = m.group(1)
                try:
                    move = b.parse_san(san)
                    return move.uci()
                except Exception:
                    pass
        except Exception:
            pass
        # Return best raw candidate even if not validated
        for c in candidates:
            if c:
                return c

    return candidates[0] if candidates else ""


def _extract_confidence(text: str) -> float:
    """Extract confidence score from text."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            v = data.get("confidence", 0.7)
            return float(v)
    except Exception:
        pass
    m = re.search(r'confidence["\s:]+([0-9.]+)', text, re.IGNORECASE)
    if m:
        try:
            return min(1.0, max(0.0, float(m.group(1))))
        except Exception:
            pass
    return 0.7


PROPOSAL_SYSTEM = """You are a chess player in a multi-agent team. Your job is to propose the best next move.

Respond with ONLY valid JSON in this exact format:
{
  "move": "<UCI notation e.g. e2e4>",
  "reasoning": "<2-3 sentences explaining why>",
  "confidence": <0.0-1.0>
}

UCI notation: from-square + to-square + optional promotion piece (q/r/b/n).
Example: e2e4, d7d5, e1g1 (castling), e7e8q (promotion)."""


async def get_agent_proposal(
    agent: AgentConfig,
    board_fen: str,
    legal_moves: list[str],
    move_history: list[str],
    color: str,
    move_number: int,
) -> MoveProposal:
    """Get a move proposal from a single agent."""
    moves_str = ", ".join(legal_moves[:30])
    if len(legal_moves) > 30:
        moves_str += f" ... ({len(legal_moves)} total)"

    history_str = " ".join(move_history[-10:]) if move_history else "none"

    user_msg = f"""You are playing as {color} on move {move_number}.

Board (FEN): {board_fen}
Recent moves: {history_str}
Legal moves available: {moves_str}

Your persona: {agent.persona}

Analyze the position and propose your best move."""

    start = time.time()
    try:
        resp = await client.chat.completions.create(
            model=agent.model,
            messages=[
                {"role": "system", "content": PROPOSAL_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=300,
            temperature=0.7,
        )
        latency = (time.time() - start) * 1000
        content = resp.choices[0].message.content or ""
        tokens = resp.usage.total_tokens if resp.usage else 0

        move = _extract_move(content, board_fen, legal_moves)
        # Validate move is legal
        if move not in legal_moves:
            lower_legal = {m.lower(): m for m in legal_moves}
            move = lower_legal.get(move.lower(), legal_moves[0] if legal_moves else "")

        confidence = _extract_confidence(content)

        # Extract reasoning
        try:
            data = json.loads(content)
            reasoning = data.get("reasoning", content[:200])
        except Exception:
            reasoning = content[:300]

        return MoveProposal(
            agent_role=agent.role,
            model=agent.model,
            proposed_move=move,
            reasoning=reasoning,
            confidence=confidence,
            tokens_used=tokens,
            latency_ms=latency,
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        # Fallback to first legal move
        fallback = legal_moves[0] if legal_moves else "a1a1"
        return MoveProposal(
            agent_role=agent.role,
            model=agent.model,
            proposed_move=fallback,
            reasoning=f"Error during deliberation: {e}",
            confidence=0.1,
            tokens_used=0,
            latency_ms=latency,
        )


SUBMITTER_SYSTEM = """You are the submitter agent in a multi-agent chess team. You receive proposals from your teammates and must select the final move.

Respond with ONLY valid JSON in this exact format:
{
  "move": "<UCI notation>",
  "rationale": "<2-3 sentences explaining why you chose this move over alternatives>"
}"""


async def get_submitter_decision(
    submitter: AgentConfig,
    proposals: list[MoveProposal],
    board_fen: str,
    legal_moves: list[str],
    color: str,
    move_number: int,
    deliberation_style: str,
) -> SubmitterDecision:
    """Get the final move decision from the submitter agent."""
    proposals_text = "\n".join([
        f"- {p.agent_role} ({p.model}) proposes {p.proposed_move} "
        f"[confidence: {p.confidence:.2f}]: {p.reasoning}"
        for p in proposals
    ])

    style_note = ""
    if deliberation_style == "consensus":
        style_note = "You must respect majority opinion unless you have compelling strategic reason to override."
    elif deliberation_style == "advisory":
        style_note = "Your teammates are advisors. You have final authority but should weigh their advice."

    user_msg = f"""Move {move_number} - Playing as {color}

Board (FEN): {board_fen}

Team proposals:
{proposals_text}

{style_note}

Select the final move for your team."""

    start = time.time()
    try:
        resp = await client.chat.completions.create(
            model=submitter.model,
            messages=[
                {"role": "system", "content": SUBMITTER_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=250,
            temperature=0.5,
        )
        latency = (time.time() - start) * 1000
        content = resp.choices[0].message.content or ""
        tokens = resp.usage.total_tokens if resp.usage else 0

        move = _extract_move(content, board_fen, legal_moves)
        if move not in legal_moves:
            lower_legal = {m.lower(): m for m in legal_moves}
            move = lower_legal.get(move.lower(), "")
            if not move:
                best = max(proposals, key=lambda p: p.confidence)
                move = best.proposed_move

        try:
            data = json.loads(content)
            rationale = data.get("rationale", content[:300])
        except Exception:
            rationale = content[:300]

        return SubmitterDecision(
            submitted_move=move,
            submitter_role=submitter.role,
            model=submitter.model,
            rationale=rationale,
            proposals_considered=proposals,
            tokens_used=tokens,
            latency_ms=latency,
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        best = max(proposals, key=lambda p: p.confidence) if proposals else None
        fallback = best.proposed_move if best else (legal_moves[0] if legal_moves else "")
        return SubmitterDecision(
            submitted_move=fallback,
            submitter_role=submitter.role,
            model=submitter.model,
            rationale=f"Fallback due to error: {e}",
            proposals_considered=proposals,
            tokens_used=0,
            latency_ms=latency,
        )
