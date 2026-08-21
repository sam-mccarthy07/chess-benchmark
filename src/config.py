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
MAX_TOKENS_SUBMITTER = 800
MAX_TOKENS_MONITOR = 400

# Temperatures. Held fixed across conditions and reported, because temperature
# is a direct confound on proposal diversity — which is a headline metric.
TEMPERATURE_PROPOSAL = 0.7
TEMPERATURE_SUBMITTER = 0.5
TEMPERATURE_MONITOR = 0.3

# Prompt versions. Bump whenever prompt text changes; the fingerprint below
# covers these so runs made under different prompts never silently pool.
PROMPT_VERSIONS = {
    "proposal": "p1",
    "submitter": "s1",
    "monitor": "m1",
}

HARNESS_PARAMS = {
    "max_tokens_proposal": MAX_TOKENS_PROPOSAL,
    "max_tokens_submitter": MAX_TOKENS_SUBMITTER,
    "max_tokens_monitor": MAX_TOKENS_MONITOR,
    "temperature_proposal": TEMPERATURE_PROPOSAL,
    "temperature_submitter": TEMPERATURE_SUBMITTER,
    "temperature_monitor": TEMPERATURE_MONITOR,
    "prompt_versions": PROMPT_VERSIONS,
    # Full legal move list is always sent. The previous 30-move truncation was
    # a systematic bias: python-chess generates moves in deterministic order,
    # so truncation silently removed the same kinds of move every time.
    "legal_moves_truncated": False,
}


def load_ablations():
    with open(CONFIGS_DIR / "ablations.json") as f:
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
        "schema_version": 2,
        "config_fingerprint": config_fingerprint(extra),
        "harness": HARNESS_PARAMS,
        "seed": seed,
    }
    if extra:
        manifest.update(extra)
    return manifest
