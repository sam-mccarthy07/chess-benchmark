"""Performance decomposition for a deliberation turn.

Separates the quality of a team's *ideas* from the quality of its
*aggregation* — which is the thing chess makes measurable and most
coordination benchmarks cannot see:

    delta_ceiling    best proposal on the table, in centipawn loss.
                     How good were the team's ideas?

    delta_selection  decision CPL minus delta_ceiling.
                     Did aggregation throw away value the team already had?

A positive delta_selection means the team held a better move and did not play
it. It is negative only when the submitter played something nobody proposed
that beat every proposal — worth knowing, so it is not clamped.

Collaborative Advantage needs solo baseline runs and arrives with those.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

import chess

from oracle import Oracle, MoveEval


def _ceiling_by_round(oracle: Oracle, board: chess.Board, turn: dict) -> list[Optional[int]]:
    """Best legal proposal, in centipawn loss, for each deliberation round.

    None for a round in which no agent produced a legal move. Empty when the
    record predates round logging.
    """
    out: list[Optional[int]] = []
    for rnd in turn.get("rounds", []):
        moves = [p.get("proposed_move") for p in rnd.get("proposals", []) if p.get("proposed_move")]
        evals = oracle.evaluate_moves(board, moves)
        legal = [e.cpl for e in evals.values() if e.legal]
        out.append(min(legal) if legal else None)
    return out


def analyse_turn(oracle: Oracle, turn: dict) -> dict:
    """Oracle metrics for one turn of a schema-v2 game record."""
    board = chess.Board(turn["fen_before"])

    best_cp, engine_best = oracle.best_move(board)

    proposals = turn.get("proposals", [])
    candidate_moves = [p["proposed_move"] for p in proposals if p.get("proposed_move")]

    decision = turn.get("decision", {})
    decision_move = decision.get("submitted_move") or ""
    played_move = turn.get("resolution", {}).get("played_move") or ""

    to_score = [m for m in (candidate_moves + [decision_move, played_move]) if m]
    evals = oracle.evaluate_moves(board, to_score)

    # Per-proposal quality, keyed by role so it can be joined to agents later.
    per_proposal = []
    for p in proposals:
        mv = p.get("proposed_move") or ""
        ev = evals.get(mv)
        per_proposal.append({
            "agent_role": p.get("agent_role"),
            "model": p.get("model"),
            "move": mv,
            "status": p.get("status"),
            "confidence": p.get("confidence"),
            "cpl": ev.cpl if ev and ev.legal else None,
            "severity": ev.severity if ev else "illegal",
            "legal": bool(ev and ev.legal),
            "mate_involved": bool(ev and ev.mate_involved),
        })

    legal_proposals = [p for p in per_proposal if p["legal"]]

    if legal_proposals:
        best = min(legal_proposals, key=lambda p: p["cpl"])
        delta_ceiling = best["cpl"]
        best_role = best["agent_role"]
    else:
        delta_ceiling = None
        best_role = None

    dec_eval = evals.get(decision_move)
    cpl_decision = dec_eval.cpl if dec_eval and dec_eval.legal else None

    if cpl_decision is not None and delta_ceiling is not None:
        delta_selection = cpl_decision - delta_ceiling
    else:
        delta_selection = None

    played_eval = evals.get(played_move)

    out = {
        "engine_best_move": engine_best,
        "engine_best_cp": best_cp,
        "proposals": per_proposal,
        "delta_ceiling": delta_ceiling,
        "best_proposal_role": best_role,
        "cpl_decision": cpl_decision,
        "decision_severity": dec_eval.severity if dec_eval else "illegal",
        "delta_selection": delta_selection,
        # Did the submitter pick the strongest move available to it?
        "submitter_picked_best": (
            None if delta_selection is None else delta_selection == 0
        ),
        "cpl_played": played_eval.cpl if played_eval and played_eval.legal else None,
        "played_severity": played_eval.severity if played_eval else "illegal",
        "distinct_candidates": len(set(candidate_moves)),
        # Best idea available at each round. If agreement climbs across rounds
        # while this does not improve, deliberation is producing conformity
        # rather than insight — the H8 signature.
        "ceiling_by_round": _ceiling_by_round(oracle, board, turn),
        "influence": influence_metrics(turn),
        # Centipawn loss is not on a centipawn scale once a forced mate is on
        # either side of the comparison. Flagged here so aggregates can exclude
        # these turns; win-probability severity remains valid throughout.
        "mate_involved": any(e.mate_involved for e in evals.values() if e.legal),
    }
    # Needs the metrics above, so it is attached rather than inlined.
    out["influence_quality"] = influence_quality(oracle, turn, out)
    return out


def influence_metrics(turn: dict) -> dict:
    """Did the team's decision differ from what each agent held on its own?

    Two anchors, both computed offline from the log:

      IR_proposal   team move vs the agent's round-0 proposal, made before it
                    saw anybody. Free, and available for every turn.
      IR_stated     team move vs the solo move the agent reported privately in
                    the final discussion round.

    `introspective_gap` is stated minus revealed. Negative means the agent
    under-reports having been moved — silent conformity, where an agent
    changed position and does not say so. The fully revealed anchor needs
    actual solo runs and arrives with them; until then IR_proposal stands in,
    and the gap computed here is stated-vs-round-0, which is weaker but
    directionally the same quantity.

    Unsigned on its own: being moved can be good or bad. Cross with move
    quality via `influence_quality` for direction.
    """
    rounds = turn.get("rounds") or []
    if not rounds:
        return {}

    decision_move = (turn.get("decision") or {}).get("submitted_move") or ""
    if not decision_move:
        return {"decision_move": "", "by_agent": {}, "note": "no decision move to compare against"}

    opening = {p["agent_role"]: p.get("proposed_move") or "" for p in rounds[0]["proposals"]}

    # Private notes come from the final round in which the agent supplied one.
    stated: dict[str, str] = {}
    for note in turn.get("private_notes") or []:
        if note.get("present") and note.get("solo_move"):
            stated[note["agent_role"]] = note["solo_move"]

    by_agent = {}
    for role, first in opening.items():
        ir_proposal = (first != decision_move) if first else None
        solo = stated.get(role, "")
        ir_stated = (solo != decision_move) if solo else None
        by_agent[role] = {
            "round0_move": first,
            "stated_solo_move": solo,
            "ir_proposal": ir_proposal,
            "ir_stated": ir_stated,
            "agrees_with_own_opening": (solo == first) if (solo and first) else None,
        }

    def _rate(key):
        vals = [v[key] for v in by_agent.values() if v[key] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    ir_p, ir_s = _rate("ir_proposal"), _rate("ir_stated")
    return {
        "decision_move": decision_move,
        "ir_proposal_rate": ir_p,
        "ir_stated_rate": ir_s,
        "introspective_gap": (
            round(ir_s - ir_p, 3) if (ir_p is not None and ir_s is not None) else None
        ),
        "private_notes_present": len(stated),
        "by_agent": by_agent,
    }


def influence_quality(oracle: Oracle, turn: dict, turn_metrics: dict) -> dict:
    """Direction for the influence measures.

    For each agent whose opening position the team did not adopt, did the
    team's move score better or worse than what that agent proposed alone?
    That separates productive persuasion from destructive conformity — the
    distinction influence rate cannot make, since being moved is unsigned.

    The oracle is needed because an agent's round-0 move is often not among
    the final-round proposals and so was never scored. Lookups are cached, so
    in practice this costs nothing beyond the first evaluation.
    """
    rounds = turn.get("rounds") or []
    cpl_decision = turn_metrics.get("cpl_decision")
    if not rounds or cpl_decision is None:
        return {}

    decision_move = (turn.get("decision") or {}).get("submitted_move") or ""
    board = chess.Board(turn["fen_before"])

    productive = destructive = neutral = 0
    detail: dict[str, dict] = {}

    for p in rounds[0]["proposals"]:
        role = p["agent_role"]
        first = p.get("proposed_move") or ""

        if not first or first == decision_move:
            detail[role] = {"moved": False, "outcome": None}
            continue

        ev = oracle.evaluate_move(board, first)
        if not ev.legal:
            # An agent whose opening move was illegal had no position to be
            # moved off, so the comparison is undefined rather than neutral.
            detail[role] = {"moved": True, "own_cpl": None, "outcome": "opening_illegal"}
            continue

        if cpl_decision < ev.cpl:
            outcome = "productive_persuasion"
            productive += 1
        elif cpl_decision > ev.cpl:
            outcome = "destructive_conformity"
            destructive += 1
        else:
            outcome = "neutral"
            neutral += 1

        detail[role] = {
            "moved": True,
            "own_cpl": ev.cpl,
            "decision_cpl": cpl_decision,
            "outcome": outcome,
        }

    return {
        "productive_persuasion": productive,
        "destructive_conformity": destructive,
        "neutral": neutral,
        "by_agent": detail,
    }


def _mean(xs: list) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def summarise_game(turn_metrics: list[dict], moves: list[dict]) -> dict:
    """Per-org aggregates over a game's analysed turns."""
    by_org: dict[str, dict] = {}

    for tm, turn in zip(turn_metrics, moves):
        org = turn.get("org_id", "unknown")
        b = by_org.setdefault(org, {
            "turns": 0,
            "_ceiling": [], "_selection": [], "_decision": [], "_played": [],
            "picked_best": 0, "picked_best_eligible": 0,
            "blunders": 0, "mistakes": 0, "inaccuracies": 0,
        })
        b["turns"] += 1
        # Mate-involved turns are excluded from centipawn means but still count
        # toward severity rates, which are win-probability based and robust.
        if tm.get("mate_involved"):
            b["mate_turns"] = b.get("mate_turns", 0) + 1
        else:
            b["_ceiling"].append(tm["delta_ceiling"])
            b["_selection"].append(tm["delta_selection"])
            b["_decision"].append(tm["cpl_decision"])
            b["_played"].append(tm["cpl_played"])

        if tm["submitter_picked_best"] is not None:
            b["picked_best_eligible"] += 1
            if tm["submitter_picked_best"]:
                b["picked_best"] += 1

        sev = tm["played_severity"]
        if sev == "blunder":
            b["blunders"] += 1
        elif sev == "mistake":
            b["mistakes"] += 1
        elif sev == "inaccuracy":
            b["inaccuracies"] += 1

    out = {}
    for org, b in by_org.items():
        eligible = b["picked_best_eligible"]
        out[org] = {
            "turns": b["turns"],
            "mate_turns_excluded_from_cp_means": b.get("mate_turns", 0),
            "mean_delta_ceiling": _mean(b["_ceiling"]),
            "mean_delta_selection": _mean(b["_selection"]),
            "mean_cpl_decision": _mean(b["_decision"]),
            "mean_cpl_played": _mean(b["_played"]),
            "submitter_picked_best_rate": (
                round(b["picked_best"] / eligible, 3) if eligible else None
            ),
            "blunder_rate": round(b["blunders"] / b["turns"], 3) if b["turns"] else None,
            "mistake_rate": round(b["mistakes"] / b["turns"], 3) if b["turns"] else None,
            "inaccuracy_rate": round(b["inaccuracies"] / b["turns"], 3) if b["turns"] else None,
        }
    return out


def analyse_pgn(oracle: Oracle, pgn: str) -> list[dict]:
    """Played-move CPL from a PGN alone.

    The fallback path for schema-v1 games, which predate per-turn records.
    Yields move quality but no proposal-level decomposition, because the
    proposals were never stored in a usable form.
    """
    import io
    import chess.pgn

    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        return []

    out = []
    board = game.board()
    for ply, move in enumerate(game.mainline_moves(), start=1):
        ev = oracle.evaluate_move(board, move.uci())
        out.append({
            "ply": ply,
            "color": "white" if board.turn == chess.WHITE else "black",
            "move": move.uci(),
            "cpl": ev.cpl if ev.legal else None,
            "severity": ev.severity,
            "mate_involved": ev.mate_involved,
        })
        board.push(move)
    return out
