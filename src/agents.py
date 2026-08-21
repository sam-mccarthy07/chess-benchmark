"""Agent communication via OpenRouter.

Integrity contract for this module
----------------------------------
A recorded proposal or decision is ALWAYS what the model actually produced.
Nothing in here substitutes a different move when a model fails. If a model
proposes an illegal move, emits something unparseable, or the API call errors,
that fact is recorded in `status` and the move field reflects reality (an
illegal move, or an empty string) rather than a stand-in.

This matters because the previous implementation silently replaced failures
with `legal_moves[0]` and logged the substitute as if the agent had proposed
it — which made illegal-move rates unmeasurable and meant an agent that
reasoned well and one that crashed produced identical log entries.

Choosing something legal so the game can continue is the *game loop's* job,
and it records that choice separately as a resolution. See game.py.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from openai import AsyncOpenAI
import chess

from config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    MAX_TOKENS_PROPOSAL,
    MAX_TOKENS_DISCUSSION,
    MAX_TOKENS_SUBMITTER,
    TEMPERATURE_PROPOSAL,
    TEMPERATURE_DISCUSSION,
    TEMPERATURE_SUBMITTER,
    PROMPT_VERSIONS,
)

# Proposal / decision status values.
STATUS_OK = "ok"                    # parsed a move, and it is legal
STATUS_ILLEGAL = "illegal"          # parsed a move, but it is not legal here
STATUS_UNPARSEABLE = "unparseable"  # no move could be extracted from the response
STATUS_API_ERROR = "api_error"      # the API call itself failed


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
    proposed_move: str  # UCI. May be illegal. "" if unparseable/errored. Never substituted.
    reasoning: str
    confidence: float   # 0-1. Meaningless unless status == "ok"; filter on status.
    tokens_used: int = 0
    latency_ms: float = 0.0
    status: str = STATUS_OK
    legal: bool = True
    raw_response: str = ""
    error: Optional[str] = None
    # "split" when the model emitted both blocks, "flat" when it ignored the
    # structure (whole response treated as public, no private data invented),
    # "unparsed" when the response was not JSON, "n/a" for calls with no
    # private half. Tracked because a model that will not honour the split
    # yields no influence data, and that should be visible rather than
    # showing up as a silently empty column.
    stream_split: str = "n/a"


@dataclass
class PrivateNote:
    """An agent's private record for one deliberation round.

    Never shown to any other agent. Kept in a separate structure from
    MoveProposal rather than as a field on it, so that leaking it into the
    group stream would require deliberately reaching for a different object
    rather than merely forgetting to strip a field.

    `solo_move` is elicited *before* the submitter decides. The agent therefore
    reports what it would play alone without knowing the team's actual choice,
    which keeps the stated counterfactual uncontaminated by the outcome. The
    cost is that it cannot explain a delta it has not yet seen — so the prompt
    asks why its solo move differs from where the group is heading, which is
    visible to it and is the substantive question anyway.
    """
    agent_role: str
    round_index: int
    solo_move: str = ""          # UCI; what this agent would play with no teammates
    solo_move_legal: bool = False
    solo_rationale: str = ""
    process_note: str = ""
    present: bool = False        # False when the model emitted no private block
    raw: str = ""


@dataclass
class SubmitterDecision:
    submitted_move: str  # UCI. May be illegal. "" if unparseable/errored. Never substituted.
    submitter_role: str
    model: str
    rationale: str
    proposals_considered: list[MoveProposal] = field(default_factory=list)
    tokens_used: int = 0
    latency_ms: float = 0.0
    status: str = STATUS_OK
    legal: bool = True
    off_slate: bool = False  # submitted a move no agent proposed
    raw_response: str = ""
    error: Optional[str] = None


client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)


def _parse_move(text: str, board_fen: str = "") -> str:
    """Best-effort extraction of the move the model intended.

    This is parsing, not coercion: SAN is translated to UCI because that is
    interpreting what the model said. A move that parses but is illegal is
    returned as-is — judging legality is the caller's job.

    Returns "" when nothing move-shaped can be found.
    """
    candidates: list[str] = []

    # Structured JSON response is the documented contract.
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            v = str(data.get("move", "")).strip()
            if v:
                candidates.append(v.lower())
    except Exception:
        pass

    # Explicit "move": field embedded in prose.
    m = re.search(
        r'"?move"?\s*[:=]\s*["\']?([a-h][1-8][a-h][1-8][qrbn]?)["\']?',
        text,
        re.IGNORECASE,
    )
    if m:
        candidates.append(m.group(1).lower())

    # Bare UCI token anywhere in the response.
    for m in re.finditer(r"\b([a-h][1-8][a-h][1-8][qrbn]?)\b", text):
        candidates.append(m.group(1).lower())

    for c in candidates:
        if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", c):
            return c

    # SAN fallback — needs the board to disambiguate.
    if board_fen:
        try:
            b = chess.Board(board_fen)
            san_pattern = (
                r"\b(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?"
                r"|[a-h]x?[a-h][1-8](?:=[QRBN])?|[a-h][1-8])\b"
            )
            for m in re.finditer(san_pattern, text):
                try:
                    return b.parse_san(m.group(1)).uci()
                except Exception:
                    continue
        except Exception:
            pass

    return candidates[0] if candidates else ""


def _split_streams(text: str) -> tuple[str, str, str]:
    """Split a response into (public_json, private_json, status).

    Both halves come back as JSON text so the existing parsers can run over
    each in isolation. That isolation is load-bearing: run over the whole
    response, the bare-UCI regex would happily pick the agent's *private*
    solo_move out and record it as the public proposal.

    status is "split" when both blocks were present, "flat" when the model
    ignored the structure (whole response treated as public, no private data
    invented), or "unparsed" when it was not JSON at all.
    """
    try:
        data = json.loads(text)
    except Exception:
        return text, "", "unparsed"

    if not isinstance(data, dict):
        return text, "", "unparsed"

    if "public" in data and isinstance(data["public"], dict):
        private = data.get("private")
        private_json = json.dumps(private) if isinstance(private, dict) else ""
        return json.dumps(data["public"]), private_json, "split"

    # No structure: treat everything as public. Never synthesise a private
    # block from a response that did not supply one.
    return text, "", "flat"


def _parse_private(
    private_json: str,
    agent_role: str,
    round_index: int,
    board_fen: str,
    legal_moves: list[str],
) -> PrivateNote:
    """Build a PrivateNote from the private half of a response."""
    if not private_json:
        return PrivateNote(agent_role=agent_role, round_index=round_index, present=False)

    try:
        data = json.loads(private_json)
    except Exception:
        return PrivateNote(
            agent_role=agent_role, round_index=round_index,
            present=False, raw=private_json,
        )

    solo = _parse_move(json.dumps({"move": data.get("solo_move", "")}), board_fen)
    if not solo:
        solo = _parse_move(str(data.get("solo_move", "")), board_fen)

    return PrivateNote(
        agent_role=agent_role,
        round_index=round_index,
        solo_move=solo,
        solo_move_legal=bool(solo) and solo in legal_moves,
        solo_rationale=str(data.get("solo_rationale", ""))[:600],
        process_note=str(data.get("process_note", ""))[:600],
        present=True,
        raw=private_json,
    )


def _extract_confidence(text: str) -> float:
    """Extract confidence score. Defaults to 0.5 when absent — a neutral prior,
    not the previous 0.7, which quietly inflated unstated confidence."""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "confidence" in data:
            return min(1.0, max(0.0, float(data["confidence"])))
    except Exception:
        pass
    m = re.search(r'confidence["\s:]+([0-9.]+)', text, re.IGNORECASE)
    if m:
        try:
            return min(1.0, max(0.0, float(m.group(1))))
        except Exception:
            pass
    return 0.5


def _classify(move: str, legal_moves: list[str]) -> tuple[str, bool]:
    """Map a parsed move to (status, legal)."""
    if not move:
        return STATUS_UNPARSEABLE, False
    if move in legal_moves:
        return STATUS_OK, True
    return STATUS_ILLEGAL, False


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
    """Get a move proposal from a single agent.

    The returned proposal records what the agent actually said, including when
    that is illegal or unparseable.
    """
    # Full legal move list. Never truncated — see config.HARNESS_PARAMS.
    moves_str = ", ".join(legal_moves)
    history_str = " ".join(move_history[-10:]) if move_history else "none"

    user_msg = f"""You are playing as {color} on move {move_number}.

Board (FEN): {board_fen}
Recent moves: {history_str}
Legal moves available ({len(legal_moves)} total): {moves_str}

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
            max_tokens=MAX_TOKENS_PROPOSAL,
            temperature=TEMPERATURE_PROPOSAL,
        )
    except Exception as e:
        return MoveProposal(
            agent_role=agent.role,
            model=agent.model,
            proposed_move="",
            reasoning="",
            confidence=0.0,
            tokens_used=0,
            latency_ms=(time.time() - start) * 1000,
            status=STATUS_API_ERROR,
            legal=False,
            raw_response="",
            error=str(e),
        )

    latency = (time.time() - start) * 1000
    content = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else 0

    move = _parse_move(content, board_fen)
    status, legal = _classify(move, legal_moves)

    try:
        data = json.loads(content)
        reasoning = data.get("reasoning", content[:300])
    except Exception:
        reasoning = content[:300]

    return MoveProposal(
        agent_role=agent.role,
        model=agent.model,
        proposed_move=move,
        reasoning=reasoning,
        confidence=_extract_confidence(content),
        tokens_used=tokens,
        latency_ms=latency,
        status=status,
        legal=legal,
        raw_response=content,
        error=None,
    )


DISCUSSION_SYSTEM = """You are a chess player in a multi-agent team deliberating on the next move.

You have already proposed a move. You can now see what your teammates proposed and why.

You may keep your move or change it. Both are legitimate outcomes of deliberation.

Respond with ONLY valid JSON in this exact format:
{
  "public": {
    "move": "<UCI notation e.g. e2e4>",
    "reasoning": "<why you are keeping or changing your move>",
    "confidence": <0.0-1.0>
  },
  "private": {
    "solo_move": "<UCI: the move you would play if you were deciding alone, with no teammates>",
    "solo_rationale": "<if solo_move differs from your public move, why>",
    "process_note": "<your assessment of how this deliberation is going>"
  }
}

Your teammates see only the "public" block. The "private" block is not shown to them."""


async def get_agent_revision(
    agent: AgentConfig,
    own: MoveProposal,
    others: list[MoveProposal],
    board_fen: str,
    legal_moves: list[str],
    color: str,
    move_number: int,
    round_index: int,
) -> tuple[MoveProposal, PrivateNote]:
    """One discussion round for one agent.

    Returns the public proposal and the private note as *separate* objects.
    Only the proposal is ever passed to another agent.

    On prompt neutrality: the instruction says only that keeping or changing
    are both legitimate. It deliberately does NOT tell agents to resist peer
    pressure. An anti-conformity nudge would suppress exactly the behaviour
    H8 is trying to measure — and it belongs in the mechanism-treatment axis
    as a manipulation, not baked into the baseline protocol. The one thing
    the prompt does assert is that holding is a valid outcome, without which
    a model may infer that it is expected to produce a change.
    """
    lines = []
    for p in others:
        if p.status in (STATUS_API_ERROR, STATUS_UNPARSEABLE):
            lines.append(f"- {p.agent_role}: no usable proposal this round")
            continue
        flag = "" if p.legal else "  [NOTE: not legal in this position]"
        lines.append(
            f"- {p.agent_role} proposes {p.proposed_move} "
            f"[confidence {p.confidence:.2f}]: {p.reasoning}{flag}"
        )
    others_text = "\n".join(lines) if lines else "(no teammate proposals available)"

    own_text = (
        f"{own.proposed_move} [confidence {own.confidence:.2f}]: {own.reasoning}"
        if own.proposed_move
        else "(you did not produce a usable proposal)"
    )

    moves_str = ", ".join(legal_moves)

    user_msg = f"""Move {move_number}, playing as {color}. Discussion round {round_index}.

Board (FEN): {board_fen}
Legal moves available ({len(legal_moves)} total): {moves_str}

Your current proposal:
{own_text}

Your teammates:
{others_text}

Your persona: {agent.persona}

State your move for this round."""

    start = time.time()
    try:
        resp = await client.chat.completions.create(
            model=agent.model,
            messages=[
                {"role": "system", "content": DISCUSSION_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=MAX_TOKENS_DISCUSSION,
            temperature=TEMPERATURE_DISCUSSION,
        )
    except Exception as e:
        return (
            MoveProposal(
                agent_role=agent.role,
                model=agent.model,
                proposed_move="",
                reasoning="",
                confidence=0.0,
                tokens_used=0,
                latency_ms=(time.time() - start) * 1000,
                status=STATUS_API_ERROR,
                legal=False,
                raw_response="",
                error=str(e),
            ),
            PrivateNote(agent_role=agent.role, round_index=round_index, present=False),
        )

    latency = (time.time() - start) * 1000
    content = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else 0

    # Parse each stream in isolation. Running the move parser over the whole
    # response would let the private solo_move be picked up as the public
    # proposal by the bare-UCI regex.
    public_json, private_json, split_status = _split_streams(content)

    move = _parse_move(public_json, board_fen)
    status, legal = _classify(move, legal_moves)
    private = _parse_private(private_json, agent.role, round_index, board_fen, legal_moves)

    try:
        data = json.loads(public_json)
        reasoning = data.get("reasoning", public_json[:300])
    except Exception:
        reasoning = public_json[:300]

    proposal = MoveProposal(
        agent_role=agent.role,
        model=agent.model,
        proposed_move=move,
        reasoning=reasoning,
        confidence=_extract_confidence(public_json),
        tokens_used=tokens,
        latency_ms=latency,
        status=status,
        legal=legal,
        # Only the public half is retained on the proposal. The full response
        # lives on the private note, so the group-stream object never carries
        # private text even in its raw field.
        raw_response=public_json,
        error=None,
    )
    proposal.stream_split = split_status
    return proposal, private


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
    """Get the final move decision from the submitter agent.

    Illegal proposals are shown to the submitter marked as illegal rather than
    hidden, because how a team handles a teammate's impossible suggestion is
    part of what this benchmark measures.
    """
    lines = []
    for p in proposals:
        if p.status == STATUS_API_ERROR:
            lines.append(f"- {p.agent_role} ({p.model}): no proposal (agent error)")
        elif p.status == STATUS_UNPARSEABLE:
            lines.append(f"- {p.agent_role} ({p.model}): response contained no readable move")
        else:
            flag = "" if p.legal else "  [NOTE: this move is not legal in this position]"
            lines.append(
                f"- {p.agent_role} ({p.model}) proposes {p.proposed_move} "
                f"[confidence: {p.confidence:.2f}]: {p.reasoning}{flag}"
            )
    proposals_text = "\n".join(lines)

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
            max_tokens=MAX_TOKENS_SUBMITTER,
            temperature=TEMPERATURE_SUBMITTER,
        )
    except Exception as e:
        return SubmitterDecision(
            submitted_move="",
            submitter_role=submitter.role,
            model=submitter.model,
            rationale="",
            proposals_considered=proposals,
            tokens_used=0,
            latency_ms=(time.time() - start) * 1000,
            status=STATUS_API_ERROR,
            legal=False,
            off_slate=False,
            raw_response="",
            error=str(e),
        )

    latency = (time.time() - start) * 1000
    content = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else 0

    move = _parse_move(content, board_fen)
    status, legal = _classify(move, legal_moves)

    try:
        data = json.loads(content)
        rationale = data.get("rationale", content[:300])
    except Exception:
        rationale = content[:300]

    proposed_set = {p.proposed_move for p in proposals if p.proposed_move}
    off_slate = bool(move) and move not in proposed_set

    return SubmitterDecision(
        submitted_move=move,
        submitter_role=submitter.role,
        model=submitter.model,
        rationale=rationale,
        proposals_considered=proposals,
        tokens_used=tokens,
        latency_ms=latency,
        status=status,
        legal=legal,
        off_slate=off_slate,
        raw_response=content,
        error=None,
    )
