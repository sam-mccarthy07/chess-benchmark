"""Computation layer for reporting.

Metrics are computed here, once, and every report is a view over the result.
Embedding computation in a renderer is how you end up with a leaderboard and a
report disagreeing about the same blunder rate with no way to tell which is
right.

Three products, in order of importance:

  turn_rows()        one flat row per (game, ply, org) — the substrate every
                     statistical analysis needs. The pre-registration calls
                     for mixed-effects models with position and model-set as
                     random effects, which requires long-format data.
  health_summary()   whether the data is worth reading at all.
  headline_summary() the pre-registered metrics, with n and uncertainty.

Health comes before findings everywhere. A Collaborative Advantage computed
over a run that was 40% illegal moves is not a weak result, it is not a result.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from typing import Iterable, Optional


# --------------------------------------------------------------------------
# Uncertainty
# --------------------------------------------------------------------------

def bootstrap_ci(
    values: list[float], confidence: float = 0.95, iterations: int = 2000, seed: int = 0
) -> tuple[Optional[float], Optional[float]]:
    """Percentile bootstrap CI. Returns (None, None) below n=2.

    Bootstrap rather than a t-interval because per-turn centipawn loss is
    heavily skewed — a handful of blunders dominate the tail — and a normal
    approximation would understate the interval exactly where it matters.
    """
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return (None, None)
    rng = random.Random(seed)
    n = len(vals)
    means = []
    for _ in range(iterations):
        means.append(sum(rng.choice(vals) for _ in range(n)) / n)
    means.sort()
    lo = means[int((1 - confidence) / 2 * iterations)]
    hi = means[min(int((1 + confidence) / 2 * iterations), iterations - 1)]
    return (round(lo, 2), round(hi, 2))


def summarise(values: list, label: str = "") -> dict:
    """Mean with n, CI and an explicit count of what was dropped.

    Excluded counts are part of the statistic, not a footnote: silently
    dropping undefined values is how a mean ends up biased toward whichever
    side happened to fail.
    """
    total = len(values)
    vals = [v for v in values if v is not None]
    lo, hi = bootstrap_ci(vals)
    return {
        "label": label,
        "n": len(vals),
        "excluded": total - len(vals),
        "mean": round(statistics.mean(vals), 2) if vals else None,
        "median": round(statistics.median(vals), 2) if vals else None,
        "ci_low": lo,
        "ci_high": hi,
    }


def rate(flags: list, label: str = "") -> dict:
    """Proportion of True among non-None, with a Wilson interval.

    Wilson rather than normal approximation because pilot cells are small and
    rates near 0 or 1 are expected — a normal interval would run outside
    [0, 1] and imply impossible values.
    """
    vals = [bool(v) for v in flags if v is not None]
    n = len(vals)
    if n == 0:
        return {"label": label, "n": 0, "excluded": len(flags), "rate": None,
                "ci_low": None, "ci_high": None}
    k = sum(vals)
    p = k / n
    z = 1.96
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {
        "label": label, "n": n, "excluded": len(flags) - n,
        "count": k, "rate": round(p, 4),
        "ci_low": round(max(0.0, centre - margin), 4),
        "ci_high": round(min(1.0, centre + margin), 4),
    }


# --------------------------------------------------------------------------
# Tidy turn-level rows
# --------------------------------------------------------------------------

TURN_COLUMNS = [
    # keys
    "game_id", "config_fingerprint", "position_id", "ply", "move_number",
    "org_id", "color", "opponent_org",
    # condition
    "deliberation_style", "submitter_rotation", "deliberation_rounds",
    "homogeneous", "submitter_role", "submitter_model",
    # quality
    "engine_best_cp", "cpl_decision", "cpl_played", "delta_ceiling",
    "delta_selection", "submitter_picked_best", "decision_severity",
    "played_severity", "mate_involved",
    # collaborative advantage
    "ca", "best_solo_cpl", "mean_solo_cpl", "team_beat_best_member",
    # influence
    "ir_proposal_rate", "ir_stated_rate", "ir_revealed_rate",
    "introspective_gap", "productive_persuasion", "destructive_conformity",
    # deliberation dynamics
    "distinct_candidates", "distinct_moves_r0", "distinct_moves_final",
    "unanimous_r0", "unanimous_final", "converged", "drifted_agents",
    # integrity
    "ok_proposals", "illegal_proposals", "unparseable_proposals",
    "api_error_proposals", "decision_legal", "false_consensus", "off_slate",
    "resolution_method",
    # cost
    "turn_tokens",
]


def _org_meta(game: dict, org_id: str) -> dict:
    return ((game.get("manifest") or {}).get("orgs") or {}).get(org_id, {})


def turn_rows(game: dict) -> list[dict]:
    """Flatten one game into tidy per-turn rows.

    Oracle metrics are joined by ply when present. A game without analysis
    still yields rows — the quality columns are simply empty, which is
    honest and lets integrity be reported before Stockfish has ever run.
    """
    manifest = game.get("manifest") or {}
    turns = game.get("moves") or []
    analysis = ((game.get("oracle_analysis") or {}).get("turns")) or []
    by_index = {i: a for i, a in enumerate(analysis)}

    rows = []
    for i, turn in enumerate(turns):
        org_id = turn.get("org_id", "")
        meta = _org_meta(game, org_id)
        a = by_index.get(i, {})
        integ = turn.get("integrity") or {}
        drift = turn.get("drift") or {}
        infl = a.get("influence") or {}
        ca = a.get("collaborative_advantage") or {}
        rev = a.get("revealed_influence") or {}
        iq = a.get("influence_quality") or {}
        distinct = drift.get("distinct_moves_by_round") or []
        unanimous = drift.get("unanimous_by_round") or []

        opponent = (
            game.get("black_org") if org_id == game.get("white_org")
            else game.get("white_org")
        )

        rows.append({
            "game_id": game.get("game_id"),
            "config_fingerprint": manifest.get("config_fingerprint"),
            "position_id": game.get("position_id") or "",
            "ply": turn.get("ply"),
            "move_number": turn.get("move_number"),
            "org_id": org_id,
            "color": turn.get("color"),
            "opponent_org": opponent,

            "deliberation_style": meta.get("deliberation_style"),
            "submitter_rotation": meta.get("submitter_rotation"),
            "deliberation_rounds": meta.get("deliberation_rounds"),
            "homogeneous": meta.get("homogeneous"),
            "submitter_role": turn.get("submitter_role"),
            "submitter_model": turn.get("submitter_model"),

            "engine_best_cp": a.get("engine_best_cp"),
            "cpl_decision": a.get("cpl_decision"),
            "cpl_played": a.get("cpl_played"),
            "delta_ceiling": a.get("delta_ceiling"),
            "delta_selection": a.get("delta_selection"),
            "submitter_picked_best": a.get("submitter_picked_best"),
            "decision_severity": a.get("decision_severity"),
            "played_severity": a.get("played_severity"),
            "mate_involved": a.get("mate_involved"),

            "ca": ca.get("collaborative_advantage"),
            "best_solo_cpl": ca.get("best_solo_cpl"),
            "mean_solo_cpl": ca.get("mean_solo_cpl"),
            "team_beat_best_member": ca.get("team_beat_best_member"),

            "ir_proposal_rate": infl.get("ir_proposal_rate"),
            "ir_stated_rate": infl.get("ir_stated_rate"),
            "ir_revealed_rate": rev.get("ir_revealed_rate"),
            "introspective_gap": infl.get("introspective_gap"),
            "productive_persuasion": iq.get("productive_persuasion"),
            "destructive_conformity": iq.get("destructive_conformity"),

            "distinct_candidates": a.get("distinct_candidates"),
            "distinct_moves_r0": distinct[0] if distinct else None,
            "distinct_moves_final": distinct[-1] if distinct else None,
            "unanimous_r0": unanimous[0] if unanimous else None,
            "unanimous_final": unanimous[-1] if unanimous else None,
            "converged": drift.get("converged"),
            "drifted_agents": drift.get("drifted_agents"),

            "ok_proposals": integ.get("ok_proposals"),
            "illegal_proposals": integ.get("illegal_proposals"),
            "unparseable_proposals": integ.get("unparseable_proposals"),
            "api_error_proposals": integ.get("api_error_proposals"),
            "decision_legal": integ.get("decision_legal"),
            "false_consensus": integ.get("false_consensus"),
            "off_slate": integ.get("off_slate"),
            "resolution_method": (turn.get("resolution") or {}).get("method"),

            "turn_tokens": sum(
                p.get("tokens_used", 0)
                for r in (turn.get("rounds") or [])
                for p in r.get("proposals", [])
            ) + ((turn.get("decision") or {}).get("tokens_used") or 0),
        })
    return rows


# --------------------------------------------------------------------------
# Grouping and aggregation
# --------------------------------------------------------------------------

def group_key(game: dict) -> tuple:
    """What makes two games poolable.

    Fingerprint covers the org config, prompts and harness parameters. The
    position-set version is separate because a run over a different position
    set is a different experiment even under an identical config.
    """
    manifest = game.get("manifest") or {}
    pset = manifest.get("position_set") or {}
    return (
        manifest.get("config_fingerprint") or "unknown",
        pset.get("version") or "none",
    )


def group_games(games: Iterable[dict]) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = defaultdict(list)
    for g in games:
        out[group_key(g)].append(g)
    return dict(out)


def health_summary(games: list[dict], rows: list[dict]) -> dict:
    """Is this data worth reading?

    Reported before any finding. Every quantity here can invalidate the
    interpretation of everything below it.
    """
    proposals_ok = sum(r["ok_proposals"] or 0 for r in rows)
    proposals_illegal = sum(r["illegal_proposals"] or 0 for r in rows)
    proposals_unparseable = sum(r["unparseable_proposals"] or 0 for r in rows)
    proposals_error = sum(r["api_error_proposals"] or 0 for r in rows)
    total_proposals = proposals_ok + proposals_illegal + proposals_unparseable + proposals_error

    analysed = sum(1 for r in rows if r["cpl_decision"] is not None)
    probed = sum(1 for r in rows if r["ca"] is not None)
    with_rounds = sum(1 for r in rows if r["distinct_moves_r0"] is not None)

    return {
        "games": len(games),
        "turns": len(rows),
        "positions": len({r["position_id"] for r in rows if r["position_id"]}),
        "total_proposals": total_proposals,
        "proposal_legal_rate": round(proposals_ok / total_proposals, 4) if total_proposals else None,
        "proposal_illegal": proposals_illegal,
        "proposal_unparseable": proposals_unparseable,
        "proposal_api_error": proposals_error,
        "decision_legal_rate": rate([r["decision_legal"] for r in rows], "decision legal"),
        "false_consensus_turns": sum(1 for r in rows if r["false_consensus"]),
        "off_slate_turns": sum(1 for r in rows if r["off_slate"]),
        "fallback_turns": sum(1 for r in rows if r["resolution_method"] not in (None, "as_decided")),
        "turns_with_rounds": with_rounds,
        "turns_with_oracle": analysed,
        "turns_with_solo_probe": probed,
        "oracle_coverage": round(analysed / len(rows), 4) if rows else None,
        "probe_coverage": round(probed / len(rows), 4) if rows else None,
        "total_tokens": sum(r["turn_tokens"] or 0 for r in rows),
    }


def headline_summary(rows: list[dict]) -> dict:
    """Pre-registered metrics, per condition, with uncertainty.

    Effect sizes and intervals only — no p-values. The pre-registration
    applies FDR control across four confirmatory hypotheses; a report
    spraying significance across twenty metrics would reintroduce exactly the
    multiple-comparison problem that control exists to prevent.
    """
    by_org: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_org[r["org_id"]].append(r)

    out = {}
    for org, rs in sorted(by_org.items()):
        cp_rows = [r for r in rs if not r["mate_involved"]]
        out[org] = {
            "condition": {
                "style": rs[0]["deliberation_style"],
                "rotation": rs[0]["submitter_rotation"],
                "rounds": rs[0]["deliberation_rounds"],
                "homogeneous": rs[0]["homogeneous"],
            },
            "turns": len(rs),
            # H1 — the headline
            "collaborative_advantage": summarise([r["ca"] for r in cp_rows], "CA (cp)"),
            "team_beat_best_member": rate(
                [r["team_beat_best_member"] for r in rs], "team beat best member"),
            # H3 — where the loss lives
            "delta_ceiling": summarise([r["delta_ceiling"] for r in cp_rows], "idea quality (cp)"),
            "delta_selection": summarise([r["delta_selection"] for r in cp_rows], "aggregation loss (cp)"),
            "submitter_picked_best": rate(
                [r["submitter_picked_best"] for r in rs], "submitter took the best proposal"),
            # H4 — silent conformity
            "introspective_gap": summarise(
                [r["introspective_gap"] for r in rs], "stated minus revealed"),
            "ir_revealed": summarise([r["ir_revealed_rate"] for r in rs], "revealed influence"),
            "ir_proposal": summarise([r["ir_proposal_rate"] for r in rs], "influence vs round 0"),
            # H8 — conformity vs persuasion
            "unanimity_r0": rate([r["unanimous_r0"] for r in rs], "unanimous at round 0"),
            "unanimity_final": rate([r["unanimous_final"] for r in rs], "unanimous at final round"),
            "converged": rate([r["converged"] for r in rs], "split team converged"),
            "distinct_moves_r0": summarise(
                [r["distinct_moves_r0"] for r in rs], "distinct moves, round 0"),
            # direction of influence
            "productive_persuasion": sum(r["productive_persuasion"] or 0 for r in rs),
            "destructive_conformity": sum(r["destructive_conformity"] or 0 for r in rs),
            # move quality
            "cpl_played": summarise([r["cpl_played"] for r in cp_rows], "played move CPL"),
            "blunder_rate": rate(
                [r["played_severity"] == "blunder" if r["played_severity"] else None for r in rs],
                "blunder rate"),
        }
    return out


def delta_share(headline_org: dict) -> Optional[float]:
    """Aggregation loss as a share of total loss.

    H3 predicts selection failure dominates idea-quality failure — that teams
    hold the better move and fail to choose it.
    """
    ceiling = headline_org["delta_ceiling"]["mean"]
    selection = headline_org["delta_selection"]["mean"]
    if ceiling is None or selection is None:
        return None
    total = ceiling + selection
    return round(selection / total, 4) if total else None
