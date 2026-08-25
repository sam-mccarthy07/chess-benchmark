"""Configuration and constants for chess benchmark."""

import ast
import hashlib
import json
import os
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CONFIGS_DIR = PROJECT_ROOT / "configs"
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"


def _load_dotenv():
    """Load PROJECT_ROOT/.env into the environment, if present. Never overrides
    an already-set env var. No external dependency — the file is gitignored
    and this just spares you re-exporting it every session."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "not-set")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

RESULTS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Harness parameters
#
# Every knob that can move a result lives here, is written into the run
# manifest, and is covered by the config fingerprint. Nothing that affects
# measurement should be hardcoded at a call site.
# ---------------------------------------------------------------------------

# Token budgets. The previous values (300/250/200) were too tight for reasoning
# models, which are the only models that function in this domain at all
# (LLM Chess, arXiv:2512.01992, found non-reasoning models at 71.9%
# instruction-following error rates). Reasoning-token allowances are set per
# model and logged rather than capped uniformly, so that reasoning budget is
# not confounded with experimental condition.
MAX_TOKENS_PROPOSAL = 800
# Revisions are responses rather than fresh analyses, but each one now also
# carries the private block (solo counterfactual + rationale), so the budget is
# larger than a bare revision would need.
MAX_TOKENS_DISCUSSION = 700
MAX_TOKENS_SUBMITTER = 800
MAX_TOKENS_MONITOR = 400

# Discussion rounds after the independent opening proposals. 0 reproduces the
# original propose-then-submit behaviour, which is retained as an ablation arm:
# comparing 0 against 2 is how we test whether extra rounds buy agreement
# without buying quality.
DELIBERATION_ROUNDS = 2

# Temperatures. Held fixed across conditions and reported, because temperature
# is a direct confound on proposal diversity — which is a headline metric.
TEMPERATURE_PROPOSAL = 0.7
TEMPERATURE_DISCUSSION = 0.7  # matched to proposal: a revision is the same act
TEMPERATURE_SUBMITTER = 0.5
TEMPERATURE_MONITOR = 0.3

# Prompt versions. Bump whenever prompt text changes; the fingerprint below
# covers these so runs made under different prompts never silently pool.
PROMPT_VERSIONS = {
    "proposal": "p1",
    "solo": "solo1",
    "discussion": "d2",  # d2 adds the private block
    "submitter": "s1",
    "monitor": "m1",
}

HARNESS_PARAMS = {
    "max_tokens_proposal": MAX_TOKENS_PROPOSAL,
    "max_tokens_discussion": MAX_TOKENS_DISCUSSION,
    "max_tokens_submitter": MAX_TOKENS_SUBMITTER,
    "max_tokens_monitor": MAX_TOKENS_MONITOR,
    "temperature_proposal": TEMPERATURE_PROPOSAL,
    "temperature_discussion": TEMPERATURE_DISCUSSION,
    "temperature_submitter": TEMPERATURE_SUBMITTER,
    "temperature_monitor": TEMPERATURE_MONITOR,
    "deliberation_rounds": DELIBERATION_ROUNDS,
    "prompt_versions": PROMPT_VERSIONS,
    # Full legal move list is always sent. The previous 30-move truncation was
    # a systematic bias: python-chess generates moves in deterministic order,
    # so truncation silently removed the same kinds of move every time.
    "legal_moves_truncated": False,
}


# ---------------------------------------------------------------------------
# Oracle (analysis-side) parameters
#
# Deliberately NOT part of config_fingerprint. That fingerprint answers "were
# these games generated the same way, so may they be pooled?" — and a game is
# the same game regardless of which engine scores it afterwards. Whether two
# CPL numbers are comparable is a separate question, answered by the engine
# provenance block written into each analysis (see oracle.Oracle.provenance).
#
# Depth 20 matches LLM Chess (arXiv:2512.01992) so our severity rates line up
# with their published single-agent baselines. Threads is pinned to 1 because
# Stockfish is not deterministic across differing thread counts at fixed depth,
# and reproducible offline analysis is worth more than analysis speed.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Transport parameters
#
# These do not change what a model says, only whether we managed to ask it, so
# they are NOT part of config_fingerprint — a run that needed retries is the
# same experiment as one that did not. Retry counts are recorded in the run
# stats instead, where they belong: as a fact about the run, not the design.
#
# Defaults are tuned for free tiers, which are the tightest case. Raise
# REQUESTS_PER_MINUTE (or set it to None) on paid tiers.
# ---------------------------------------------------------------------------

# Raw model output — the text a proposal's move/reasoning/confidence were
# parsed out of.
#
# PR 10 pruned this on clean parses, on the grounds that it was exact
# duplication of fields already stored (27% of a game record by measurement).
# That reasoning was wrong in one specific way: it is only duplication *given
# the parser that produced those fields*. PR 11 is the proof — fenced-JSON
# handling changed private-note capture from 41% to 97%, and the records
# written before it cannot be re-derived, because the text the new parser would
# read was discarded. `parser_fingerprint` exists precisely because parsing is
# a variable; this is the same fact seen from the storage side.
#
# It is also the corpus the rubric protocol in Build Plan §6 requires — build a
# rubric, hand-label a held-out set, report precision/recall — and untouched
# model output is what "hand-label" means. E3 (public argument vs private
# assessment) reads the same text.
#
# So retention is now the default. Measured cost is ~117KB per game, or roughly
# 190MB across the full 1,600-game grid, against results/ being gitignored and
# never entering git history. Set RETAIN_RAW_RESPONSES=0 to restore pruning for
# throwaway smoke tests where the corpus does not matter.
_retain = os.environ.get("RETAIN_RAW_RESPONSES", "1").lower()
RETAIN_RAW_RESPONSES = _retain not in ("0", "false", "no", "")

MAX_RETRIES = 5
RETRY_BASE_DELAY_S = 2.0
RETRY_MAX_DELAY_S = 60.0
MAX_CONCURRENT_CALLS = int(os.environ.get("MAX_CONCURRENT_CALLS", "4"))
_rpm = os.environ.get("REQUESTS_PER_MINUTE", "20")
REQUESTS_PER_MINUTE = None if _rpm.lower() in ("", "none", "0") else int(_rpm)


ENGINE_PARAMS = {
    "engine_path": os.environ.get("STOCKFISH_PATH", "stockfish"),
    "depth": int(os.environ.get("STOCKFISH_DEPTH", "20")),
    "threads": 1,
    "hash_mb": 64,
}


# Which org config the run uses. Set once by the CLI before anything reads it,
# because config_fingerprint hashes the ablation file: two runs under different
# configs must never share a fingerprint.
_ACTIVE_CONFIG = CONFIGS_DIR / "ablations.json"


def set_active_config(path) -> Path:
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = Path(path)
    if not _ACTIVE_CONFIG.is_file():
        raise FileNotFoundError(f"config not found: {_ACTIVE_CONFIG}")
    return _ACTIVE_CONFIG


def active_config_path() -> Path:
    return _ACTIVE_CONFIG


def load_ablations():
    with open(_ACTIVE_CONFIG) as f:
        return json.load(f)


def set_seed(seed: int | None) -> int | None:
    """Seed process-level RNG. Returns the seed actually used."""
    if seed is None:
        return None
    random.seed(seed)
    return seed


# Per-game identity: recorded in the manifest, deliberately NOT hashed into
# config_fingerprint.
#
# Which position a game started from, and which side each org took, are
# *within-design* variables — the paired design in Pre-Registration v1 §0.2
# requires every condition to play the same position set with colours swapped,
# and then to be compared as pairs. Hashing them gives every position and every
# colour assignment its own fingerprint; since reports never pool across
# fingerprints, that silently dissolves the design into singleton cells (a
# 20-position, 2-colour run would report 40 cells of n=1) while every other
# number still looks healthy.
#
# `position_set` stays in the hash on purpose: two different *released sets*
# are genuinely different experiments and must never pool. The distinction is
# which set you sampled from, not which member of it you drew.
FINGERPRINT_EXCLUDED_KEYS = ("white_org", "black_org", "start_fen", "position_id")


# The module that turns a model response into a record. Its behaviour decides
# what the data *is* — not merely how the run was configured — so it belongs in
# the fingerprint alongside the config. PR 11 is the worked example: fixing
# fenced-JSON parsing changed private-note capture from 41% to 97% with no
# config change at all, so without this the two runs would have carried the
# same fingerprint and been pooled into one average.
PARSER_MODULE = PROJECT_ROOT / "src" / "agents.py"


def _semantic_source_hash(source: str) -> str:
    """Hash what a module *does*, ignoring how it is written.

    Hashing the raw file text would work, but it splits the dataset on a typo
    in a comment: any edit at all would declare every prior game unpoolable.
    That trades one silent failure for a noisy one, and a fingerprint people
    learn to ignore is no better than one that misses things.

    So the source is parsed to an AST, docstrings are dropped, and the tree is
    dumped without line numbers. Comments never reach the AST. Reformatting,
    re-wrapping and prose edits therefore leave the hash alone, while any
    change to executable logic changes it.

    Caveat: `ast.dump` output is not guaranteed stable across Python versions,
    so a interpreter upgrade can shift this hash without any code change. That
    errs toward over-splitting — the safe direction, since it refuses to pool
    rather than pooling wrongly — but record the Python version with a release.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:]
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()[:16]


def parser_fingerprint() -> str:
    """Behavioural hash of the response-parsing module.

    Deliberately not wrapped in a try/except returning "unknown": a fingerprint
    that silently degrades to a constant is exactly the failure this exists to
    prevent, because every run would then share it and pool.
    """
    return _semantic_source_hash(PARSER_MODULE.read_text(encoding="utf-8"))


def config_fingerprint(extra: dict | None = None) -> str:
    """Stable hash over everything that can change a result.

    Covers the ablation config, harness parameters and prompt versions. Two
    runs with the same fingerprint are poolable; two runs without are not.

    Per-game identity (FINGERPRINT_EXCLUDED_KEYS) is stripped before hashing:
    it varies *by design* within a condition, so including it would make every
    game its own condition.
    """
    payload = {
        "config_file": _ACTIVE_CONFIG.name,
        "ablations": load_ablations(),
        "harness": HARNESS_PARAMS,
        "parser": parser_fingerprint(),
    }
    if extra:
        payload["extra"] = {
            k: v for k, v in extra.items() if k not in FINGERPRINT_EXCLUDED_KEYS
        }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# Identifies one invocation. Without it a report cannot tell a second run of
# the same config from the first, because both land in the same results/
# directory under the same fingerprint.
_RUN_ID = None


def set_run_id(run_id: str) -> str:
    global _RUN_ID
    _RUN_ID = run_id
    return run_id


def get_run_id():
    return _RUN_ID


def new_run_id() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_manifest(seed: int | None = None, extra: dict | None = None) -> dict:
    """Run manifest embedded in every saved game."""
    manifest = {
        "run_id": _RUN_ID,
        # 1: original. 2: integrity fields + per-turn records (PR 1).
        # 3: deliberation rounds, drift, and sampled start positions.
        "schema_version": 3,
        "config_fingerprint": config_fingerprint(extra),
        # Surfaced separately as well as folded into the fingerprint above, so
        # a reader can see *which* input changed when two runs will not pool.
        "parser_fingerprint": parser_fingerprint(),
        # Whether this run kept raw model output. Not in the fingerprint —
        # retention changes what was *stored*, not what the models did, so two
        # runs differing only in this are the same experiment. But a reader
        # needs to know whether a given run's corpus is complete enough to
        # hand-label or re-parse, and that cannot be inferred after the fact
        # from a record whose raw fields are simply absent.
        "retained_raw_responses": RETAIN_RAW_RESPONSES,
        "harness": HARNESS_PARAMS,
        "seed": seed,
    }
    if extra:
        manifest.update(extra)
    return manifest
