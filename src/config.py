"""Configuration and constants for chess benchmark."""

import os
import json
from pathlib import Path

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "not-set")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

PROJECT_ROOT = Path(__file__).parent.parent
CONFIGS_DIR = PROJECT_ROOT / "configs"
RESULTS_DIR = PROJECT_ROOT / "results"

RESULTS_DIR.mkdir(exist_ok=True)


def load_ablations():
    with open(CONFIGS_DIR / "ablations.json") as f:
        return json.load(f)
