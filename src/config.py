"""Configuration and constants for chess benchmark."""

import os
import json
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


def load_ablations():
    with open(CONFIGS_DIR / "ablations.json") as f:
        return json.load(f)
