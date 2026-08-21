"""Chain-of-thought monitor: classifies behavior and outcomes per move."""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from openai import AsyncOpenAI

from config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    MAX_TOKENS_MONITOR,
    TEMPERATURE_MONITOR,
)


client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)


@dataclass
class MoveAnalysis:
    move_number: int
    org_id: str
    submitted_move: str
    agreement_level: str       # "unanimous", "majority", "override", "solo"
    dominant_behavior: str     # "aggressive", "defensive", "positional", "tactical", "blunder"
    deliberation_quality: str  # "high", "medium", "low"
    key_insight: str           # one-sentence summary of what drove the decision
    dissent_detected: bool
    tokens_total: int
    latency_total_ms: float


MONITOR_SYSTEM = """You are an AI behavior analyst observing a multi-agent chess system.
Analyze the deliberation and classify the team's decision-making behavior.

Respond ONLY with valid JSON:
{
  "agreement_level": "<unanimous|majority|override|solo>",
  "dominant_behavior": "<aggressive|defensive|positional|tactical|blunder>",
  "deliberation_quality": "<high|medium|low>",
  "key_insight": "<one sentence>",
  "dissent_detected": <true|false>
}"""


async def analyze_move(
    move_number: int,
    org_id: str,
    board_fen: str,
    proposals: list[dict],
    decision: dict,
    monitor_model: str,
) -> MoveAnalysis:
    """Analyze a team's deliberation and decision for a single move."""
    proposals_text = "\n".join([
        f"- {p['agent_role']}: proposed {p['proposed_move']} (confidence {p['confidence']:.2f}) - {p['reasoning'][:150]}"
        for p in proposals
    ])

    total_tokens = sum(p.get("tokens_used", 0) for p in proposals) + decision.get("tokens_used", 0)
    total_latency = sum(p.get("latency_ms", 0) for p in proposals) + decision.get("latency_ms", 0)

    user_msg = f"""Move {move_number} analysis for org: {org_id}

Board state (FEN): {board_fen}

Agent proposals:
{proposals_text}

Submitter ({decision['submitter_role']}) chose: {decision['submitted_move']}
Submitter rationale: {decision['rationale'][:250]}

Analyze the deliberation quality and team dynamics."""

    try:
        resp = await client.chat.completions.create(
            model=monitor_model,
            messages=[
                {"role": "system", "content": MONITOR_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=MAX_TOKENS_MONITOR,
            temperature=TEMPERATURE_MONITOR,
        )
        content = resp.choices[0].message.content or ""

        try:
            data = json.loads(content)
        except Exception:
            # Try to extract JSON from mixed content
            m = re.search(r'\{[^{}]+\}', content, re.DOTALL)
            data = json.loads(m.group()) if m else {}

        dissent_raw = data.get("dissent_detected", False)
        dissent = dissent_raw if isinstance(dissent_raw, bool) else str(dissent_raw).lower() == "true"
        return MoveAnalysis(
            move_number=move_number,
            org_id=org_id,
            submitted_move=decision["submitted_move"],
            agreement_level=data.get("agreement_level", "unknown"),
            dominant_behavior=data.get("dominant_behavior", "unknown"),
            deliberation_quality=data.get("deliberation_quality", "unknown"),
            key_insight=data.get("key_insight", ""),
            dissent_detected=dissent,
            tokens_total=total_tokens,
            latency_total_ms=total_latency,
        )
    except Exception as e:
        return MoveAnalysis(
            move_number=move_number,
            org_id=org_id,
            submitted_move=decision["submitted_move"],
            agreement_level="unknown",
            dominant_behavior="unknown",
            deliberation_quality="unknown",
            key_insight=f"Monitor error: {e}",
            dissent_detected=False,
            tokens_total=total_tokens,
            latency_total_ms=total_latency,
        )
