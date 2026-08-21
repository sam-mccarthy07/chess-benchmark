"""Stockfish oracle: ground-truth move quality.

This is what chess gives us that most coordination benchmarks cannot get — a
quality signal on *every individual decision*, not just on the final outcome.
The maze in the Collaboration Gap paper grades only the finished path; here
every deliberation round can be scored independently.

Analysis is deliberately a separate offline pass rather than part of the game
loop. Engine version and depth change centipawn numbers, so being able to
re-analyse a stored game under a different engine without replaying it (and
without spending API credit) is worth the extra step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Optional

import chess
import chess.engine

from config import ENGINE_PARAMS

# Mate scores are folded into centipawns so everything is one comparable scale.
# 10000 is far outside any real evaluation, so a mate always dominates.
MATE_SCORE = 10000

# Win-probability drop thresholds, matching LLM Chess (arXiv:2512.01992) so our
# severity rates are directly comparable to their single-agent baselines.
BLUNDER_THRESHOLD = 30.0
MISTAKE_THRESHOLD = 20.0
INACCURACY_THRESHOLD = 10.0


def win_probability(cp: float) -> float:
    """Centipawns -> win probability (0-100) for the side to move.

    Lichess' logistic conversion. Centipawns are not linear in win chance —
    100cp matters enormously in a level position and barely at all when a queen
    up — so severity is classified on this scale rather than on raw cp.
    """
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0)


def classify_severity(win_prob_drop: float) -> str:
    if win_prob_drop >= BLUNDER_THRESHOLD:
        return "blunder"
    if win_prob_drop >= MISTAKE_THRESHOLD:
        return "mistake"
    if win_prob_drop >= INACCURACY_THRESHOLD:
        return "inaccuracy"
    return "ok"


@dataclass
class MoveEval:
    """Quality of one move in one position, from the mover's perspective."""
    move: str
    cp_after: int           # evaluation once this move is played
    cpl: int                # centipawn loss vs the engine's best move
    win_prob_drop: float
    severity: str
    legal: bool = True
    # True when either side of the comparison is a forced mate. Centipawn loss
    # is then not on a centipawn scale at all — it is the distance between a
    # mate score and an ordinary evaluation, which can exceed MATE_SCORE and
    # would badly distort any mean. Severity, being win-probability based,
    # stays meaningful. Aggregations should exclude or report these separately.
    mate_involved: bool = False


class Oracle:
    """Thin, cached wrapper around a UCI engine.

    Results are cached on (fen, depth) because a single turn asks about the
    same position repeatedly — once for the engine's best move, then once per
    distinct proposal.
    """

    def __init__(
        self,
        engine_path: Optional[str] = None,
        depth: Optional[int] = None,
        threads: Optional[int] = None,
        hash_mb: Optional[int] = None,
    ):
        self.engine_path = engine_path or ENGINE_PARAMS["engine_path"]
        self.depth = depth or ENGINE_PARAMS["depth"]
        self.threads = threads or ENGINE_PARAMS["threads"]
        self.hash_mb = hash_mb or ENGINE_PARAMS["hash_mb"]
        self._engine: Optional[chess.engine.SimpleEngine] = None
        self._cache: dict[tuple[str, int], tuple[int, str]] = {}
        self.engine_id: str = ""

    def __enter__(self) -> "Oracle":
        self._engine = chess.engine.SimpleEngine.popen_uci(self.engine_path)
        # Pinned so centipawn numbers are reproducible on any machine. Stockfish
        # is not deterministic across differing thread counts at fixed depth.
        self._engine.configure({"Threads": self.threads, "Hash": self.hash_mb})
        self.engine_id = self._engine.id.get("name", "unknown")
        return self

    def __exit__(self, *exc):
        if self._engine is not None:
            self._engine.quit()
            self._engine = None
        return False

    def _analyse(self, board: chess.Board) -> tuple[int, str, bool]:
        """(cp from side-to-move's view, best move uci, is_mate) for `board`."""
        key = (board.fen(), self.depth)
        if key in self._cache:
            return self._cache[key]

        if board.is_game_over():
            outcome = board.outcome()
            if outcome is not None and outcome.winner is not None:
                score = -MATE_SCORE if outcome.winner != board.turn else MATE_SCORE
                self._cache[key] = (score, "", True)
            else:
                self._cache[key] = (0, "", False)
            return self._cache[key]

        info = self._engine.analyse(board, chess.engine.Limit(depth=self.depth))
        pov = info["score"].pov(board.turn)
        score = pov.score(mate_score=MATE_SCORE)
        pv = info.get("pv") or []
        best = pv[0].uci() if pv else ""
        self._cache[key] = (score, best, pov.is_mate())
        return self._cache[key]

    def best_move(self, board: chess.Board) -> tuple[int, str]:
        """Engine's evaluation and best move for the side to move."""
        score, best, _ = self._analyse(board)
        return score, best

    def evaluate_move(self, board: chess.Board, uci: str) -> MoveEval:
        """Score a single candidate move in `board`.

        An illegal move has no evaluation — it is recorded as such rather than
        given a placeholder number.
        """
        try:
            move = chess.Move.from_uci(uci)
        except Exception:
            move = None

        if move is None or move not in board.legal_moves:
            return MoveEval(
                move=uci, cp_after=0, cpl=0,
                win_prob_drop=0.0, severity="illegal", legal=False,
            )

        best_cp, _, best_is_mate = self._analyse(board)

        board.push(move)
        try:
            after_cp_opponent, _, after_is_mate = self._analyse(board)
        finally:
            board.pop()
        # Flip to the mover's perspective.
        after_cp = -after_cp_opponent

        # Fixed-depth search is not perfectly monotonic, so a move can score a
        # hair above the root's best line. Clamp rather than report negative loss.
        cpl = max(0, best_cp - after_cp)
        drop = max(0.0, win_probability(best_cp) - win_probability(after_cp))

        return MoveEval(
            move=uci,
            cp_after=after_cp,
            cpl=cpl,
            win_prob_drop=round(drop, 2),
            severity=classify_severity(drop),
            legal=True,
            mate_involved=best_is_mate or after_is_mate,
        )

    def evaluate_moves(self, board: chess.Board, ucis: list[str]) -> dict[str, MoveEval]:
        """Evaluate several candidate moves in the same position."""
        return {u: self.evaluate_move(board, u) for u in dict.fromkeys(ucis)}

    def provenance(self) -> dict:
        """Everything needed to know whether two CPL numbers are comparable."""
        return {
            "engine_id": self.engine_id,
            "depth": self.depth,
            "threads": self.threads,
            "hash_mb": self.hash_mb,
            "mate_score": MATE_SCORE,
            "thresholds": {
                "blunder": BLUNDER_THRESHOLD,
                "mistake": MISTAKE_THRESHOLD,
                "inaccuracy": INACCURACY_THRESHOLD,
            },
        }
