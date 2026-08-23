"""Run identity, artifact persistence and record pruning.

Two properties matter here. First, a report must be reconstructible: results/
accumulates across runs, so without a run id a report generated later silently
answers a different question than the one produced at the time. Second, pruning
must only ever remove exact duplication — never something a reader would need.

Run: python3 -W ignore::DeprecationWarning -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

warnings.filterwarnings("ignore", message=".*iscoroutinefunction.*", category=DeprecationWarning)

import config as config_mod
from agents import MoveProposal, PrivateNote, STATUS_OK, STATUS_ILLEGAL, STATUS_API_ERROR
from game import serialise_private, serialise_proposal
from metrics import final_proposals
from report import write_run_artifacts

sys.path.insert(0, str(Path(__file__).parent))
from test_analysis import _game  # noqa: E402


RAW = json.dumps({"public": {"move": "e2e4", "reasoning": "centre", "confidence": 0.7}})


def _prop(status=STATUS_OK, split="split"):
    return MoveProposal(
        agent_role="a", model="m", proposed_move="e2e4", reasoning="centre",
        confidence=0.7, status=status, legal=status == STATUS_OK,
        raw_response=RAW, stream_split=split,
    )


class TestRawPruning(unittest.TestCase):
    """Raw output is kept exactly when something went wrong."""

    def setUp(self):
        self._orig = config_mod.RETAIN_RAW_RESPONSES

    def tearDown(self):
        config_mod.RETAIN_RAW_RESPONSES = self._orig
        import game
        game.RETAIN_RAW_RESPONSES = self._orig

    def _set_retain(self, value):
        import game
        game.RETAIN_RAW_RESPONSES = value

    def test_clean_parse_drops_raw_but_keeps_the_parsed_fields(self):
        self._set_retain(False)
        d = serialise_proposal(_prop())
        self.assertEqual(d["raw_response"], "")
        self.assertTrue(d["raw_response_pruned"])
        # Everything the raw blob contained is still present individually.
        self.assertEqual(d["proposed_move"], "e2e4")
        self.assertEqual(d["reasoning"], "centre")
        self.assertEqual(d["confidence"], 0.7)

    def test_illegal_move_keeps_raw(self):
        self._set_retain(False)
        self.assertEqual(serialise_proposal(_prop(status=STATUS_ILLEGAL))["raw_response"], RAW)

    def test_api_error_keeps_raw(self):
        self._set_retain(False)
        self.assertEqual(serialise_proposal(_prop(status=STATUS_API_ERROR))["raw_response"], RAW)

    def test_unhonoured_stream_split_keeps_raw(self):
        # A model that ignored the public/private structure is exactly the case
        # someone will want to inspect by hand.
        self._set_retain(False)
        self.assertEqual(serialise_proposal(_prop(split="flat"))["raw_response"], RAW)

    def test_retain_flag_keeps_everything(self):
        self._set_retain(True)
        d = serialise_proposal(_prop())
        self.assertEqual(d["raw_response"], RAW)
        self.assertNotIn("raw_response_pruned", d)

    def test_private_note_follows_the_same_rule(self):
        self._set_retain(False)
        note = PrivateNote(agent_role="a", round_index=1, solo_move="d2d4",
                           solo_rationale="prefer d4", present=True, raw="{...}")
        d = serialise_private(note)
        self.assertEqual(d["raw"], "")
        self.assertEqual(d["solo_move"], "d2d4", "parsed fields must survive")
        self.assertEqual(d["solo_rationale"], "prefer d4")

    def test_absent_private_note_is_untouched(self):
        self._set_retain(False)
        note = PrivateNote(agent_role="a", round_index=1, present=False, raw="garbage")
        self.assertEqual(serialise_private(note)["raw"], "garbage")


class TestFinalProposals(unittest.TestCase):
    """The final round is derived, not stored twice."""

    def test_derived_from_the_last_round(self):
        turn = {"rounds": [
            {"round_index": 0, "proposals": [{"agent_role": "a", "proposed_move": "d2d4"}]},
            {"round_index": 2, "proposals": [{"agent_role": "a", "proposed_move": "e2e4"}]},
        ]}
        self.assertEqual(final_proposals(turn)[0]["proposed_move"], "e2e4")

    def test_falls_back_to_a_stored_key_for_older_records(self):
        turn = {"proposals": [{"agent_role": "a", "proposed_move": "g1f3"}]}
        self.assertEqual(final_proposals(turn)[0]["proposed_move"], "g1f3")

    def test_empty_turn_is_safe(self):
        self.assertEqual(final_proposals({}), [])


class TestRunIdentity(unittest.TestCase):
    def tearDown(self):
        config_mod.set_run_id(None)

    def test_run_id_is_a_sortable_utc_stamp(self):
        rid = config_mod.new_run_id()
        self.assertRegex(rid, r"^\d{8}T\d{6}Z$")
        self.assertLessEqual(rid, config_mod.new_run_id())

    def test_manifest_carries_the_active_run_id(self):
        config_mod.set_run_id("run-xyz")
        self.assertEqual(config_mod.build_manifest()["run_id"], "run-xyz")

    def test_manifest_run_id_is_none_when_unset(self):
        config_mod.set_run_id(None)
        self.assertIsNone(config_mod.build_manifest()["run_id"])


class TestArtifactPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_writes_report_csv_and_manifest_of_games(self):
        out = write_run_artifacts("run-1", [_game("g1"), _game("g2")], reports_dir=self.tmp)
        base = out["dir"]
        self.assertTrue((base / "run_report.md").is_file())
        self.assertTrue((base / "turns.csv").is_file())
        self.assertTrue((base / "games.txt").is_file())
        self.assertEqual(out["games"], 2)
        self.assertGreater(out["turn_rows"], 0)

    def test_artifacts_are_namespaced_by_run(self):
        a = write_run_artifacts("run-a", [_game("g1")], reports_dir=self.tmp)
        b = write_run_artifacts("run-b", [_game("g2")], reports_dir=self.tmp)
        self.assertNotEqual(a["dir"], b["dir"])
        self.assertIn("g1", (a["dir"] / "games.txt").read_text())
        self.assertNotIn("g1", (b["dir"] / "games.txt").read_text())

    def test_report_captures_the_run_as_it_stood(self):
        # Regenerating later against more games must not overwrite the record
        # of what this run looked like.
        first = write_run_artifacts("run-1", [_game("g1")], reports_dir=self.tmp)
        text = (first["dir"] / "run_report.md").read_text()
        self.assertIn("Data health", text)
        self.assertIn("1 / 2 / 1", text, "should report one game")


if __name__ == "__main__":
    unittest.main()
