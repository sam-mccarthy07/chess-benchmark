"""Configuration and constants for chess benchmark."""

import hashlib
import json
import os
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CONFIGS_DIR = PROJECT_ROOT / "configs"
RESULTS_DIR = PROJECT_ROOT / "results"


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


def config_fingerprint(extra: dict | None = None) -> str:
    """Stable hash over everything that can change a result.

    Covers the ablation config, harness parameters and prompt versions. Two
    runs with the same fingerprint are poolable; two runs without are not.
    """
    payload = {
        "config_file": _ACTIVE_CONFIG.name,
        "ablations": load_ablations(),
        "harness": HARNESS_PARAMS,
    }
    if extra:
        payload["extra"] = extra
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_manifest(seed: int | None = None, extra: dict | None = None) -> dict:
    """Run manifest embedded in every saved game."""
    manifest = {
        # 1: original. 2: integrity fields + per-turn records (PR 1).
        # 3: deliberation rounds, drift, and sampled start positions.
        "schema_version": 3,
        "config_fingerprint": config_fingerprint(extra),
        "harness": HARNESS_PARAMS,
        "seed": seed,
    }
    if extra:
        manifest.update(extra)
    return manifest
