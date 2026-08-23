"""Analysis and reporting tests.

The properties that matter here are about honesty rather than correctness of
arithmetic: aggregates must expose what they dropped, games from different
configurations must never be pooled, and a report must not imply findings when
the underlying data is missing.

Run: python3 -W ignore::DeprecationWarning -m unittest discover -s tests -v
"""

import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

warnings.filterwarnings("ignore", message=".*iscoroutinefunction.*", category=DeprecationWarning)

from analysis import (
    TURN_COLUMNS, bootstrap_ci, delta_share, group_games, group_key,
    headline_summary, health_summary, rate, summarise, turn_rows,
)
from report import game_report, run_report


def _game(game_id="g1", fingerprint="fp1", pset="v1", org="alpha",
          turns=2, with_oracle=True, with_probe=True, illegal=0):
    moves, analysis = [], []
    for i in range(turns):
        moves.append({
            "ply": i + 1, "move_number": 10 + i, "org_id": org, "color": "white",
            "fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "submitter_role": "strategist", "submitter_model": "m",
            "rounds": [
                {"round_index": 0, "proposals": [
                    {"agent_role": "a", "model": "m", "proposed_move": "e2e4",
                     "status": "ok", "confidence": 0.6, "reasoning": "centre"},
                    {"agent_role": "b", "model": "m2", "proposed_move": "d2d4",
                     "status": "ok", "confidence": 0.5, "reasoning": "queen pawn"},
                ]},
                {"round_index": 1, "proposals": [
                    {"agent_role": "a", "model": "m", "proposed_move": "e2e4",
                     "status": "ok", "confidence": 0.7, "reasoning": "holding"},
                    {"agent_role": "b", "model": "m2", "proposed_move": "e2e4",
                     "status": "ok", "confidence": 0.4, "reasoning": "persuaded"},
                ]},
            ],
            "private_notes": [
                {"agent_role": "b", "round_index": 1, "solo_move": "d2d4",
                 "solo_rationale": "still prefer d4", "present": True},
            ],
            "proposals": [
                {"agent_role": "a", "model": "m", "proposed_move": "e2e4",
                 "status": "ok", "confidence": 0.7},
                {"agent_role": "b", "model": "m2", "proposed_move": "e2e4",
                 "status": "ok", "confidence": 0.4},
            ],
            "decision": {"submitted_move": "e2e4", "submitter_role": "strategist",
                         "status": "ok", "legal": True, "rationale": "majority",
                         "tokens_used": 40},
            "resolution": {"played_move": "e2e4", "method": "as_decided", "note": ""},
            "integrity": {"ok_proposals": 2, "illegal_proposals": illegal,
                          "unparseable_proposals": 0, "api_error_proposals": 0,
                          "decision_legal": True, "false_consensus": False,
                          "off_slate": False, "distinct_proposed_moves": 1},
            "drift": {"rounds": 1, "drifted_agents": 1,
                      "distinct_moves_by_round": [2, 1],
                      "unanimous_by_round": [False, True], "converged": True,
                      "by_agent": {}},
        })
        if with_oracle:
            analysis.append({
                "engine_best_move": "e2e4", "engine_best_cp": 44,
                "proposals": [{"agent_role": "a", "move": "e2e4", "cpl": 0},
                              {"agent_role": "b", "move": "e2e4", "cpl": 0}],
                "delta_ceiling": 0, "cpl_decision": 0, "delta_selection": 0,
                "submitter_picked_best": True, "cpl_played": 0,
                "decision_severity": "ok", "played_severity": "ok",
                "distinct_candidates": 1, "mate_involved": False,
                "influence": {"ir_proposal_rate": 0.5, "ir_stated_rate": 0.5,
                              "introspective_gap": 0.0},
                "influence_quality": {"productive_persuasion": 1,
                                      "destructive_conformity": 0},
                "collaborative_advantage": (
                    {"collaborative_advantage": 25, "best_solo_cpl": 25,
                     "mean_solo_cpl": 30.0, "cpl_team": 0,
                     "team_beat_best_member": True} if with_probe else {}),
                "revealed_influence": (
                    {"ir_revealed_rate": 0.5} if with_probe else {}),
            })

    g = {
        "game_id": game_id, "white_org": org, "black_org": "beta",
        "white_name": org, "black_name": "beta", "result": "draw",
        "result_reason": "max-moves", "total_moves": turns,
        "position_id": "p1", "start_fen": "",
        "moves": moves,
        "manifest": {
            "config_fingerprint": fingerprint, "seed": 1,
            "position_set": {"version": pset},
            "orgs": {org: {"name": org, "deliberation_style": "consensus",
                           "submitter_rotation": "round_robin",
                           "deliberation_rounds": 2, "models": ["m", "m2"],
                           "roles": ["a", "b"], "homogeneous": False}},
        },
    }
    if with_oracle:
        g["oracle_analysis"] = {"schema": "turns", "turns": analysis}
    if with_probe:
        g["solo_probes"] = {"schema": "by_ply", "by_ply": [
            {"ply": i + 1, "fen": "", "probes": [
                {"agent_role": "a", "move": "e2e4", "legal": True, "status": "ok"},
                {"agent_role": "b", "move": "d2d4", "legal": True, "status": "ok"},
            ]} for i in range(turns)]}
    return g


class TestUncertainty(unittest.TestCase):
    def test_ci_is_undefined_below_two_values(self):
        self.assertEqual(bootstrap_ci([]), (None, None))
        self.assertEqual(bootstrap_ci([5]), (None, None))

    def test_ci_brackets_the_mean(self):
        lo, hi = bootstrap_ci([10, 12, 14, 11, 13, 9, 15], seed=1)
        self.assertLessEqual(lo, 12)
        self.assertGreaterEqual(hi, 12)

    def test_ci_is_deterministic_under_a_seed(self):
        self.assertEqual(bootstrap_ci([1, 5, 3, 9], seed=7), bootstrap_ci([1, 5, 3, 9], seed=7))

    def test_summarise_counts_what_it_dropped(self):
        s = summarise([1, 2, None, 4, None], "x")
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["excluded"], 2, "dropped values must be visible, not silent")

    def test_rate_interval_stays_inside_zero_one(self):
        r = rate([True] * 5, "all true")
        self.assertEqual(r["rate"], 1.0)
        self.assertLessEqual(r["ci_high"], 1.0)
        self.assertGreaterEqual(r["ci_low"], 0.0)

    def test_rate_ignores_undefined_but_reports_them(self):
        r = rate([True, False, None], "x")
        self.assertEqual(r["n"], 2)
        self.assertEqual(r["excluded"], 1)


class TestTurnRows(unittest.TestCase):
    def test_every_declared_column_is_produced(self):
        rows = turn_rows(_game())
        self.assertTrue(rows)
        for col in TURN_COLUMNS:
            self.assertIn(col, rows[0], f"missing column {col}")

    def test_condition_is_read_from_the_record_not_a_config_file(self):
        # A config edited after the run must not change how the run is described.
        row = turn_rows(_game())[0]
        self.assertEqual(row["deliberation_style"], "consensus")
        self.assertEqual(row["deliberation_rounds"], 2)
        self.assertFalse(row["homogeneous"])

    def test_metrics_are_joined_onto_the_right_turn(self):
        row = turn_rows(_game())[0]
        self.assertEqual(row["ca"], 25)
        self.assertEqual(row["delta_selection"], 0)
        self.assertEqual(row["ir_revealed_rate"], 0.5)

    def test_rows_exist_without_oracle_analysis(self):
        # Integrity must be reportable before Stockfish has ever run.
        rows = turn_rows(_game(with_oracle=False, with_probe=False))
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["cpl_decision"])
        self.assertEqual(rows[0]["ok_proposals"], 2)

    def test_opponent_is_resolved(self):
        self.assertEqual(turn_rows(_game())[0]["opponent_org"], "beta")


class TestGrouping(unittest.TestCase):
    def test_fingerprint_and_position_set_both_define_a_group(self):
        self.assertNotEqual(group_key(_game(fingerprint="a")), group_key(_game(fingerprint="b")))
        self.assertNotEqual(group_key(_game(pset="v1")), group_key(_game(pset="v2")))

    def test_games_from_different_configs_are_never_pooled(self):
        groups = group_games([_game("g1", fingerprint="a"), _game("g2", fingerprint="b"),
                              _game("g3", fingerprint="a")])
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(len(v) for v in groups.values()), [1, 2])


class TestHealth(unittest.TestCase):
    def test_counts_proposals_by_outcome(self):
        rows = turn_rows(_game(illegal=1))
        h = health_summary([_game(illegal=1)], rows)
        self.assertEqual(h["proposal_illegal"], 2)
        self.assertLess(h["proposal_legal_rate"], 1.0)

    def test_coverage_reflects_missing_analysis(self):
        g = _game(with_oracle=False, with_probe=False)
        h = health_summary([g], turn_rows(g))
        self.assertEqual(h["oracle_coverage"], 0.0)
        self.assertEqual(h["probe_coverage"], 0.0)

    def test_full_coverage_when_everything_ran(self):
        g = _game()
        h = health_summary([g], turn_rows(g))
        self.assertEqual(h["oracle_coverage"], 1.0)
        self.assertEqual(h["probe_coverage"], 1.0)


class TestHeadline(unittest.TestCase):
    def test_reports_per_condition_with_n(self):
        head = headline_summary(turn_rows(_game()))
        self.assertIn("alpha", head)
        s = head["alpha"]
        self.assertEqual(s["turns"], 2)
        self.assertEqual(s["condition"]["style"], "consensus")
        self.assertEqual(s["collaborative_advantage"]["mean"], 25)

    def test_mate_turns_are_excluded_from_centipawn_means(self):
        rows = turn_rows(_game())
        rows[0]["mate_involved"] = True
        s = headline_summary(rows)["alpha"]
        self.assertEqual(s["collaborative_advantage"]["n"], 1, "mate turn should be dropped")

    def test_delta_share_expresses_aggregation_loss(self):
        s = headline_summary(turn_rows(_game()))["alpha"]
        s["delta_ceiling"]["mean"] = 30
        s["delta_selection"]["mean"] = 90
        self.assertEqual(delta_share(s), 0.75)

    def test_delta_share_is_undefined_without_both_components(self):
        s = headline_summary(turn_rows(_game()))["alpha"]
        s["delta_selection"]["mean"] = None
        self.assertIsNone(delta_share(s))


class TestReports(unittest.TestCase):
    def test_run_report_separates_configurations(self):
        text = run_report([_game("g1", fingerprint="aaa"), _game("g2", fingerprint="bbb")])
        self.assertIn("2 distinct configurations", text)
        self.assertIn("aaa", text)
        self.assertIn("bbb", text)
        self.assertIn("never pooled", text)

    def test_run_report_leads_with_health(self):
        text = run_report([_game()])
        self.assertLess(text.index("Data health"), text.index("Headline metrics"),
                        "health must precede findings")

    def test_run_report_says_so_when_analysis_is_missing(self):
        text = run_report([_game(with_oracle=False, with_probe=False)])
        self.assertIn("backfill.py", text)
        self.assertIn("solo_probe.py", text)

    def test_run_report_reports_no_actual_p_values(self):
        # Significance testing belongs in the pre-registered analysis, under
        # FDR control — not sprayed across every metric in a report. Matching
        # on reported *values* rather than the word, since the report says in
        # prose that it deliberately carries none.
        import re
        text = run_report([_game()])
        pattern = re.compile(r"\bp\s*[=<>]\s*0?\.\d+", re.I)
        self.assertIsNone(pattern.search(text), "report should not state p-values")
        self.assertIn("No p-values", text, "and should say why")

    def test_run_report_shows_intervals_not_bare_means(self):
        text = run_report([_game()])
        self.assertIn("n=", text)
        self.assertRegex(text, r"\[-?[\d.]+, -?[\d.]+\]")

    def test_game_report_shows_each_round_separately(self):
        text = game_report(_game())
        self.assertIn("Round 0 (independent)", text)
        self.assertIn("Round 1", text)

    def test_game_report_shows_private_notes_and_probes(self):
        text = game_report(_game())
        self.assertIn("not shown to teammates", text)
        self.assertIn("still prefer d4", text)
        self.assertIn("Solo probe", text)

    def test_game_report_flags_missing_analysis(self):
        text = game_report(_game(with_oracle=False, with_probe=False))
        self.assertIn("No oracle analysis", text)
        self.assertIn("No solo probes", text)


if __name__ == "__main__":
    unittest.main()
