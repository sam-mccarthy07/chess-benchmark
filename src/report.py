#!/usr/bin/env python3
"""Reports over saved games.

Three views, all reading the same computed metrics so they cannot disagree:

  --game <id>   deliberation transcript for one game. What each agent proposed
                each round, who changed position, what it privately said it
                would have played alone, and what the engine thought. This is
                how you verify agents actually coordinated rather than talked
                past each other.
  --run         aggregate across games, grouped by config fingerprint. Health
                first, findings second.
  --csv <path>  tidy per-turn export. One row per (game, ply, org) — the
                substrate for the mixed-effects models the pre-registration
                calls for.

Usage:
  python3 report.py --run
  python3 report.py --run --markdown > run.md
  python3 report.py --game 2d5e8f8a
  python3 report.py --csv analysis/turns.csv
"""

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from metrics import final_proposals
from analysis import (
    TURN_COLUMNS, delta_share, group_games, headline_summary,
    health_summary, turn_rows,
)
from config import RESULTS_DIR
from leaderboard import load_all_games


def _fmt(s: dict) -> str:
    """Mean with interval, n, and what was dropped."""
    if s.get("mean") is None:
        return f"— (n=0, {s.get('excluded', 0)} excluded)"
    ci = ""
    if s.get("ci_low") is not None:
        ci = f" [{s['ci_low']:g}, {s['ci_high']:g}]"
    excl = f", {s['excluded']} excluded" if s.get("excluded") else ""
    return f"{s['mean']:g}{ci} (n={s['n']}{excl})"


def _fmt_rate(r: dict) -> str:
    if r.get("rate") is None:
        return f"— (n=0, {r.get('excluded', 0)} excluded)"
    ci = ""
    if r.get("ci_low") is not None:
        ci = f" [{r['ci_low']:.0%}, {r['ci_high']:.0%}]"
    return f"{r['rate']:.1%}{ci} ({r.get('count', 0)}/{r['n']})"


def load_games(game_id=None, run_id=None):
    """Games matching the filters.

    `run_id` is what makes a report reconstructible after the fact: results/
    accumulates across runs, so without it a report generated later silently
    includes games the original run never saw.
    """
    games = load_all_games(warn=False)
    if game_id:
        games = [g for g in games if game_id in (g.game_id or "")]
    if run_id:
        games = [g for g in games if (g.run_id or "") == run_id]
    return games


def as_dict(g):
    from dataclasses import asdict
    return asdict(g)


# --------------------------------------------------------------------------
# Single-game transcript
# --------------------------------------------------------------------------

def game_report(game: dict) -> str:
    m = game.get("manifest") or {}
    orgs = m.get("orgs") or {}
    analysis = ((game.get("oracle_analysis") or {}).get("turns")) or []
    probes = {p.get("ply"): p for p in ((game.get("solo_probes") or {}).get("by_ply") or [])}

    out = []
    out.append(f"# Game {game.get('game_id')}\n")
    out.append(f"- **{game.get('white_name')}** (white) vs **{game.get('black_name')}** (black)")
    out.append(f"- Result: {game.get('result')} by {game.get('result_reason')}, "
               f"{game.get('total_moves')} plies")
    if game.get("position_id"):
        out.append(f"- Start position `{game['position_id']}` — `{game.get('start_fen')}`")
    out.append(f"- Config `{m.get('config_fingerprint')}` | seed {m.get('seed')}")
    for oid, meta in orgs.items():
        out.append(f"- `{oid}`: {meta.get('deliberation_style')} / "
                   f"{meta.get('submitter_rotation')} / {meta.get('deliberation_rounds')} rounds / "
                   f"{'homogeneous' if meta.get('homogeneous') else 'heterogeneous'}")
    if not analysis:
        out.append("\n> No oracle analysis on this game. Run `backfill.py` for move quality.")
    if not probes:
        out.append("> No solo probes on this game. Run `solo_probe.py` for Collaborative Advantage.")
    out.append("")

    for i, turn in enumerate(game.get("moves") or []):
        a = analysis[i] if i < len(analysis) else {}
        drift = turn.get("drift") or {}
        integ = turn.get("integrity") or {}
        dec = turn.get("decision") or {}
        res = turn.get("resolution") or {}

        out.append(f"## Ply {turn.get('ply')} — move {turn.get('move_number')}, "
                   f"{turn.get('color')} ({turn.get('org_id')})\n")
        if a.get("engine_best_move"):
            out.append(f"Engine: `{a['engine_best_move']}` at {a.get('engine_best_cp')}cp\n")

        for rnd in turn.get("rounds") or []:
            label = "Round 0 (independent)" if rnd["round_index"] == 0 else f"Round {rnd['round_index']}"
            out.append(f"**{label}**\n")
            for p in rnd["proposals"]:
                mark = "" if p.get("status") == "ok" else f" ⚠️ {p.get('status')}"
                cpl = ""
                for pp in (a.get("proposals") or []):
                    if pp.get("agent_role") == p.get("agent_role") and pp.get("move") == p.get("proposed_move"):
                        if pp.get("cpl") is not None:
                            cpl = f" · {pp['cpl']}cp loss"
                out.append(f"- `{p.get('proposed_move') or '—'}`{mark}{cpl} "
                           f"— **{p.get('agent_role')}** ({p.get('confidence', 0):.2f}): "
                           f"{(p.get('reasoning') or '').strip()}")
            out.append("")

        private = [n for n in (turn.get("private_notes") or []) if n.get("present")]
        if private:
            out.append("**Private (not shown to teammates)**\n")
            for n in private:
                solo = n.get("solo_move") or "—"
                out.append(f"- **{n.get('agent_role')}** would play `{solo}` alone"
                           f"{' · ' + n['solo_rationale'].strip() if n.get('solo_rationale') else ''}")
                # The agent's read on the deliberation itself. Collected since
                # PR 6 and stored, but never rendered until now — it is the
                # half of the private stream that speaks to E3 (public argument
                # vs private assessment) rather than to the counterfactual.
                if n.get("process_note"):
                    out.append(f"  - *on the discussion:* {n['process_note'].strip()}")
            out.append("")

        if probes.get(turn.get("ply")):
            out.append("**Solo probe (asked alone, no teammates)**\n")
            for p in probes[turn["ply"]]["probes"]:
                out.append(f"- **{p.get('agent_role')}**: `{p.get('move') or '—'}`"
                           f"{'' if p.get('legal') else ' ⚠️ illegal'}")
            out.append("")

        flag = "" if dec.get("legal") else f" ⚠️ {dec.get('status')}"
        out.append(f"**Decision** `{dec.get('submitted_move') or '—'}`{flag} "
                   f"by {dec.get('submitter_role')}"
                   f"{' · OFF-SLATE' if integ.get('off_slate') else ''}")
        if dec.get("rationale"):
            out.append(f"> {dec['rationale'].strip()}")
        if res.get("method") and res["method"] != "as_decided":
            out.append(f"\n⚠️ **Resolution**: played `{res.get('played_move')}` — {res.get('note')}")

        bits = []
        if drift.get("distinct_moves_by_round"):
            bits.append(f"distinct by round {drift['distinct_moves_by_round']}")
        if drift.get("drifted_agents") is not None:
            bits.append(f"{drift['drifted_agents']} drifted")
        if drift.get("converged"):
            bits.append("converged")
        if a.get("delta_selection") is not None:
            bits.append(f"Δ_selection {a['delta_selection']}cp")
        ca = (a.get("collaborative_advantage") or {}).get("collaborative_advantage")
        if ca is not None:
            bits.append(f"CA {ca:+}cp")
        if bits:
            out.append(f"\n`{' · '.join(bits)}`")
        out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------
# Cross-game run report
# --------------------------------------------------------------------------

def run_report(games: list[dict]) -> str:
    out = ["# Run report\n"]
    groups = group_games(games)

    if len(groups) > 1:
        out.append(f"> **{len(groups)} distinct configurations found.** Reported separately — "
                   f"games generated under different configs or position sets are different "
                   f"experiments and are never pooled.\n")

    for (fingerprint, pset), gs in sorted(groups.items()):
        rows = [r for g in gs for r in turn_rows(g)]
        out.append(f"## Config `{fingerprint}` · position set `{pset}`\n")

        h = health_summary(gs, rows)
        out.append("### Data health\n")
        out.append("Read this before anything below it. A metric computed over "
                   "unreliable data is not a weak result.\n")
        out.append(f"| | |\n|---|---|")
        out.append(f"| Games / turns / positions | {h['games']} / {h['turns']} / {h['positions']} |")
        if h["proposal_legal_rate"] is not None:
            out.append(f"| **Proposals legal** | **{h['proposal_legal_rate']:.1%}** "
                       f"of {h['total_proposals']:,} |")
        out.append(f"| Illegal / unparseable / API error | {h['proposal_illegal']} / "
                   f"{h['proposal_unparseable']} / {h['proposal_api_error']} |")
        out.append(f"| Decision legal | {_fmt_rate(h['decision_legal_rate'])} |")
        out.append(f"| False consensus (team ratified an illegal move) | {h['false_consensus_turns']} |")
        out.append(f"| Off-slate decisions | {h['off_slate_turns']} |")
        out.append(f"| Turns needing a resolution fallback | {h['fallback_turns']} |")
        out.append(f"| Oracle coverage | {h['oracle_coverage']:.0%} |" if h["oracle_coverage"] is not None else "| Oracle coverage | — |")
        out.append(f"| Solo-probe coverage | {h['probe_coverage']:.0%} |" if h["probe_coverage"] is not None else "| Solo-probe coverage | — |")
        out.append(f"| Total tokens | {h['total_tokens']:,} |")
        out.append("")

        if h["oracle_coverage"] == 0:
            out.append("> No oracle analysis present — run `backfill.py`. "
                       "Findings below will be empty.\n")
        if h["probe_coverage"] == 0:
            out.append("> No solo probes present — run `solo_probe.py`. "
                       "Collaborative Advantage cannot be computed.\n")

        head = headline_summary(rows)
        out.append("### Headline metrics\n")
        out.append("Effect sizes with 95% intervals. No p-values: the "
                   "pre-registration applies FDR control across four confirmatory "
                   "hypotheses, and significance testing belongs there, not here.\n")

        for org, s in head.items():
            c = s["condition"]
            out.append(f"#### `{org}` — {c['style']} / {c['rotation']} / "
                       f"{c['rounds']} rounds / "
                       f"{'homogeneous' if c['homogeneous'] else 'heterogeneous'} "
                       f"({s['turns']} turns)\n")
            out.append("| Metric | Value |\n|---|---|")
            out.append(f"| **Collaborative Advantage** (H1) | **{_fmt(s['collaborative_advantage'])}** |")
            out.append(f"| Team beat its best member | {_fmt_rate(s['team_beat_best_member'])} |")
            share = delta_share(s)
            out.append(f"| Idea quality Δ_ceiling (H3) | {_fmt(s['delta_ceiling'])} |")
            out.append(f"| Aggregation loss Δ_selection (H3) | {_fmt(s['delta_selection'])} |")
            if share is not None:
                out.append(f"| **Aggregation share of total loss** | **{share:.0%}** |")
            out.append(f"| Submitter took the best proposal | {_fmt_rate(s['submitter_picked_best'])} |")
            out.append(f"| **Introspective gap** (H4) | **{_fmt(s['introspective_gap'])}** |")
            out.append(f"| Revealed influence | {_fmt(s['ir_revealed'])} |")
            out.append(f"| Unanimity round 0 → final (H8) | {_fmt_rate(s['unanimity_r0'])} → "
                       f"{_fmt_rate(s['unanimity_final'])} |")
            out.append(f"| Split teams that converged | {_fmt_rate(s['converged'])} |")
            out.append(f"| Distinct moves at round 0 | {_fmt(s['distinct_moves_r0'])} |")
            out.append(f"| Productive persuasion / destructive conformity | "
                       f"{s['productive_persuasion']} / {s['destructive_conformity']} |")
            out.append(f"| Played-move CPL | {_fmt(s['cpl_played'])} |")
            out.append(f"| Blunder rate | {_fmt_rate(s['blunder_rate'])} |")
            out.append("")

    return "\n".join(out)


def export_csv(games: list[dict], path: Path) -> int:
    rows = [r for g in games for r in turn_rows(g)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TURN_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in TURN_COLUMNS})
    return len(rows)


def write_run_artifacts(run_id: str, games: list[dict], reports_dir=None) -> dict:
    """Persist a run's report and tidy export at the moment it finishes.

    An audit trail needs the report as it stood when the run ended.
    Regenerating it later against an accumulated results/ directory answers a
    different question, and nothing on disk would say which was which.
    """
    from config import REPORTS_DIR
    base = Path(reports_dir or REPORTS_DIR) / run_id
    base.mkdir(parents=True, exist_ok=True)

    (base / "run_report.md").write_text(run_report(games))
    rows = export_csv(games, base / "turns.csv")
    (base / "games.txt").write_text(
        "\n".join(sorted(g.get("game_id", "") for g in games)) + "\n")

    # Transcripts are written for every game, not on request. results/ is
    # gitignored, so without this the qualitative record — every word the
    # models actually said — exists only on the machine that ran the games,
    # and a collaborator cloning the repo gets metrics with nothing behind
    # them. turns.csv carries no text at all: it can say a turn converged and
    # ratified an illegal move, but not how the team talked itself there,
    # which Build Plan §3.5 makes a first-class object of study.
    transcripts = 0
    for g in games:
        gid = g.get("game_id")
        if not gid:
            continue
        (base / f"game_{gid}.md").write_text(game_report(g))
        transcripts += 1

    return {
        "dir": base,
        "games": len(games),
        "turn_rows": rows,
        "transcripts": transcripts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Reports over saved games")
    ap.add_argument("--game", help="Deliberation transcript for one game id")
    ap.add_argument("--run", action="store_true", help="Aggregate report across games")
    ap.add_argument("--csv", type=Path, help="Write tidy per-turn rows here")
    ap.add_argument("--out", type=Path, help="Write the report to a file instead of stdout")
    ap.add_argument("--run-id", help="Restrict to one run (see reports/ or a game manifest)")
    ap.add_argument("--list-runs", action="store_true", help="Show runs present in results/")
    args = ap.parse_args()

    if args.list_runs:
        runs = {}
        for g in load_all_games(warn=False):
            key = (g.run_id or "(no run id)", (g.manifest or {}).get("config_fingerprint", "?"))
            runs[key] = runs.get(key, 0) + 1
        if not runs:
            print("No games in results/.")
            return 1
        print(f"{'run_id':<22} {'config':<18} {'games':>6}")
        for (rid, fp), n in sorted(runs.items()):
            print(f"{rid:<22} {fp:<18} {n:>6}")
        return 0

    if not (args.game or args.run or args.csv):
        ap.print_help()
        return 1

    games = [as_dict(g) for g in load_games(args.game, args.run_id)]
    if not games:
        print("No matching games in results/.")
        return 1

    text = ""
    if args.game:
        text = game_report(games[0])
    elif args.run:
        text = run_report(games)

    if args.csv:
        n = export_csv(games, args.csv)
        print(f"Wrote {n} turn rows to {args.csv}")

    if text:
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
            print(f"Wrote report to {args.out}")
        else:
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
