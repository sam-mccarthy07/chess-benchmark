"""Start-position wiring tests.

The sampler existed for a while before anything used it: games still began at
move 1, so the whole reason for sampling — skipping the opening, where
deliberation is recall rather than reasoning — was unrealised. These tests pin
the wiring shut.

No network: the deliberation layer is stubbed.

Run: python3 -W ignore::DeprecationWarning -m unittest discover -s tests -v
"""

import asyncio
import io
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

warnings.filterwarnings("ignore", message=".*iscoroutinefunction.*", category=DeprecationWarning)

import chess
import chess.pgn

import game as game_mod
from agents import MoveProposal, SubmitterDecision, STATUS_OK
from monitor import MoveAnalysis
from positions import load_position_set, position_set_provenance
from team import DeliberationRound, Team

# A real middlegame position: black to move, move 27.
MIDGAME_FEN = "r1b2bnr/pq1n1p2/3pp1kp/1pp5/P1P3pP/1PNP4/2Q1PPP1/2BRKBNR b - - 11 14"


def _org(org_id):
    return {
        "id": org_id, "name": org_id, "description": "d",
        "agents": [
            {"model": "test/model", "role": r, "persona": "p"}
            for r in ("strategist", "analyst", "critic")
        ],
        "deliberation_style": "consensus",
        "submitter_rotation": "round_robin",
        "deliberation_rounds": 0,
    }


async def _fake_deliberate(self, board, color):
    pick = sorted(m.uci() for m in board.legal_moves)[0]
    proposals = [
        MoveProposal(agent_role=a.role, model="test/model", proposed_move=pick,
                     reasoning="r", confidence=0.5, status=STATUS_OK, legal=True)
        for a in self.agents
    ]
    decision = SubmitterDecision(
        submitted_move=pick, submitter_role=self.agents[0].role, model="test/model",
        rationale="r", proposals_considered=proposals, status=STATUS_OK, legal=True,
    )
    return decision, [DeliberationRound(0, proposals)]


def _fake_analysis(**kw):
    return MoveAnalysis(
        move_number=kw.get("move_number", 0), org_id=kw.get("org_id", "o"),
        submitted_move=kw["decision"]["submitted_move"],
        agreement_level="unanimous", dominant_behavior="positional",
        deliberation_quality="high", key_insight="", dissent_detected=False,
        tokens_total=0, latency_total_ms=0.0,
    )


def play(**kwargs):
    saved = {}
    async def go():
        with mock.patch.object(Team, "deliberate", new=_fake_deliberate), \
             mock.patch.object(game_mod, "analyze_move", side_effect=_fake_analysis), \
             mock.patch.object(game_mod, "save_game", new=lambda r: saved.setdefault("r", r)):
            return await game_mod.play_game(
                white_team=game_mod.build_team(_org("white-org"), "white"),
                black_team=game_mod.build_team(_org("black-org"), "black"),
                verbose=False,
                **kwargs,
            )
    return asyncio.run(go())


class TestStartPosition(unittest.TestCase):
    def test_game_begins_from_the_supplied_position(self):
        r = play(max_moves=2, start_fen=MIDGAME_FEN, position_id="abc123")
        self.assertEqual(r.start_fen, MIDGAME_FEN)
        self.assertEqual(r.position_id, "abc123")
        self.assertEqual(r.moves[0]["fen_before"], MIDGAME_FEN)

    def test_side_to_move_is_taken_from_the_fen(self):
        # The FEN says black to move; the standard opening would say white.
        r = play(max_moves=1, start_fen=MIDGAME_FEN, position_id="abc123")
        self.assertEqual(r.moves[0]["color"], "black")

    def test_move_numbering_continues_from_the_position(self):
        # Restarting at 1 would mislabel every move in the record.
        r = play(max_moves=2, start_fen=MIDGAME_FEN, position_id="abc123")
        self.assertEqual(r.moves[0]["move_number"], chess.Board(MIDGAME_FEN).fullmove_number)

    def test_max_moves_counts_from_the_start_position(self):
        r = play(max_moves=3, start_fen=MIDGAME_FEN, position_id="abc123")
        self.assertLessEqual(r.total_moves, 6)

    def test_omitting_a_position_still_plays_the_standard_opening(self):
        r = play(max_moves=1)
        self.assertEqual(r.start_fen, "")
        self.assertEqual(r.moves[0]["fen_before"], chess.Board().fen())


class TestPgnRoundTrip(unittest.TestCase):
    """A custom start needs SetUp/FEN headers, or replay silently breaks."""

    def test_pgn_carries_setup_headers(self):
        r = play(max_moves=2, start_fen=MIDGAME_FEN, position_id="abc123")
        self.assertIn("[SetUp ", r.pgn)
        self.assertIn("[FEN ", r.pgn)

    def test_pgn_replays_to_the_same_moves(self):
        r = play(max_moves=3, start_fen=MIDGAME_FEN, position_id="abc123")
        game = chess.pgn.read_game(io.StringIO(r.pgn))
        self.assertEqual(game.board().fen(), MIDGAME_FEN)

        board = game.board()
        replayed = []
        for mv in game.mainline_moves():
            self.assertIn(mv, board.legal_moves, "PGN does not replay legally")
            replayed.append(mv.uci())
            board.push(mv)
        self.assertEqual(replayed, [m["resolution"]["played_move"] for m in r.moves])

    def test_standard_opening_pgn_has_no_setup_headers(self):
        r = play(max_moves=1)
        self.assertNotIn("[SetUp ", r.pgn)


class TestManifestProvenance(unittest.TestCase):
    def test_manifest_records_the_position_and_its_set(self):
        meta = {"file": "positions_v1.json", "version": "v1", "seed": 1, "count": 200}
        r = play(max_moves=1, start_fen=MIDGAME_FEN, position_id="abc123", position_set=meta)
        self.assertEqual(r.manifest["position_id"], "abc123")
        self.assertEqual(r.manifest["start_fen"], MIDGAME_FEN)
        self.assertEqual(r.manifest["position_set"], meta)

    def test_schema_version_reflects_the_current_record_shape(self):
        r = play(max_moves=1, start_fen=MIDGAME_FEN, position_id="abc123")
        self.assertGreaterEqual(r.manifest["schema_version"], 3)

    def test_manifest_records_rounds_per_org(self):
        r = play(max_moves=1, start_fen=MIDGAME_FEN, position_id="abc123")
        self.assertIn("deliberation_rounds", r.manifest)


class TestPositionSetLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _write(self, payload):
        p = self.tmp / "positions.json"
        p.write_text(json.dumps(payload))
        return p

    def test_loads_a_well_formed_set(self):
        p = self._write({
            "version": "v1", "seed": 1, "count": 1,
            "positions": [{"position_id": "a", "fen": MIDGAME_FEN}],
        })
        self.assertEqual(load_position_set(p)["count"], 1)

    def test_declared_count_must_match_reality(self):
        # A truncated or hand-edited file must not quietly shorten a run.
        p = self._write({
            "version": "v1", "count": 5,
            "positions": [{"position_id": "a", "fen": MIDGAME_FEN}],
        })
        with self.assertRaises(RuntimeError) as ctx:
            load_position_set(p)
        self.assertIn("count", str(ctx.exception))

    def test_empty_set_is_rejected(self):
        with self.assertRaises(RuntimeError):
            load_position_set(self._write({"version": "v1", "count": 0, "positions": []}))

    def test_unparseable_fen_is_rejected_at_load_not_mid_run(self):
        p = self._write({
            "version": "v1", "count": 1,
            "positions": [{"position_id": "a", "fen": "not a fen"}],
        })
        with self.assertRaises(ValueError):
            load_position_set(p)

    def test_provenance_captures_set_identity(self):
        payload = {"version": "v1", "seed": 7, "count": 1, "filters": {"x": 1},
                   "positions": [{"position_id": "a", "fen": MIDGAME_FEN}]}
        p = self._write(payload)
        meta = position_set_provenance(load_position_set(p), p)
        self.assertEqual(meta["version"], "v1")
        self.assertEqual(meta["seed"], 7)
        self.assertEqual(meta["file"], "positions.json")


class TestShippedPositionSet(unittest.TestCase):
    """The released set must actually be loadable by the runner."""

    def test_positions_v1_loads_and_every_fen_is_legal(self):
        path = Path(__file__).parent.parent / "positions" / "positions_v1.json"
        if not path.is_file():
            self.skipTest("positions_v1.json not present")
        data = load_position_set(path)
        self.assertEqual(data["count"], len(data["positions"]))
        for p in data["positions"]:
            board = chess.Board(p["fen"])
            self.assertTrue(any(board.legal_moves), f"{p['position_id']} has no legal moves")
            self.assertFalse(board.is_game_over(), f"{p['position_id']} is already over")


class TestPairedSeries(unittest.TestCase):
    """The paired design: every condition plays the same positions."""

    def _run(self, positions, swap=True):
        saved = []
        async def go():
            with mock.patch.object(Team, "deliberate", new=_fake_deliberate), \
                 mock.patch.object(game_mod, "analyze_move", side_effect=_fake_analysis), \
                 mock.patch.object(game_mod, "save_game", new=lambda r: saved.append(r)):
                return await game_mod.run_position_series(
                    white_org=_org("alpha"), black_org=_org("beta"),
                    positions=positions, max_moves=1, verbose=False,
                    seed=1, swap_colors=swap,
                )
        return asyncio.run(go())

    def _positions(self, n=2):
        return [
            {"position_id": f"p{i}", "fen": MIDGAME_FEN}
            for i in range(n)
        ]

    def test_each_position_is_played_under_both_colour_assignments(self):
        results = self._run(self._positions(2))
        self.assertEqual(len(results), 4)
        for pid in ("p0", "p1"):
            games = [r for r in results if r.position_id == pid]
            self.assertEqual(len(games), 2)
            self.assertEqual({g.white_org for g in games}, {"alpha", "beta"})

    def test_colour_swap_can_be_disabled(self):
        self.assertEqual(len(self._run(self._positions(3), swap=False)), 3)

    def test_every_game_records_which_position_it_used(self):
        for r in self._run(self._positions(2)):
            self.assertTrue(r.position_id)
            self.assertEqual(r.start_fen, MIDGAME_FEN)

    def test_submitter_rotation_does_not_leak_between_games(self):
        # Teams are rebuilt per game. Reusing one would carry rotation phase
        # across otherwise independent games, so which agent submits on move 1
        # would depend on how many games happened to precede it.
        results = self._run(self._positions(2))
        firsts = {r.moves[0]["submitter_role"] for r in results}
        self.assertEqual(len(firsts), 1, f"rotation phase leaked: {firsts}")


class TestOracleHandlesCustomStart(unittest.TestCase):
    """The analysis pass must not assume games begin at move 1.

    `analyse_pgn` is the fallback path used for records without per-turn data.
    If it replayed a middlegame PGN from the standard opening, every move would
    come back illegal and the whole game would score as garbage.
    """

    def test_pgn_analysis_starts_from_the_recorded_position(self):
        import shutil
        if shutil.which("stockfish") is None:
            self.skipTest("stockfish not installed")

        from metrics import analyse_pgn
        from oracle import Oracle

        r = play(max_moves=3, start_fen=MIDGAME_FEN, position_id="abc123")
        with Oracle(depth=8) as o:
            plies = analyse_pgn(o, r.pgn)

        self.assertEqual(len(plies), r.total_moves)
        for p in plies:
            self.assertIsNotNone(p["cpl"], f"ply {p['ply']} scored as illegal")
        # The FEN says black to move, so the first analysed ply must be black's.
        self.assertEqual(plies[0]["color"], "black")


if __name__ == "__main__":
    unittest.main()
