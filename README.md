# Chess Benchmark for Multi-Agent Orchestration

A benchmark platform where teams of AI agents collaborate via majority deliberation to play chess against each other, designed to study AI-AI orchestration dynamics.

## Overview

Each "org" fields a team of 3 AI agents who collectively decide every chess move:

1. **Deliberation phase**: All 3 agents propose a move with reasoning and confidence
2. **Submission phase**: The current "submitter" agent sees all proposals and makes the final call
3. **Monitor phase**: A separate monitor model classifies behavior (agreement, dominant style, quality)
4. **Rotation**: The submitter role rotates every move (or is fixed to a leader, depending on ablation)

Different org configurations ("ablations") compete on a leaderboard, enabling comparison of:
- Consensus vs. autocratic decision-making styles
- Round-robin vs. fixed-leader submitter rotation
- Different agent personas within a team

## Quick Start

```bash
pip install -r requirements.txt

# Run a single 10-move test game
python3 src/main.py --single

# Run a 2-game tournament (20 moves each)
python3 src/main.py

# Run a 4-game tournament (40 moves each)
python3 src/main.py --games 4 --moves 40

# Show leaderboard
python3 src/main.py --leaderboard
```

Set your OpenRouter API key:
```bash
export OPENROUTER_API_KEY=sk-or-v1-...
```

## Dashboard

![Dashboard: move-by-move deliberation replay](dashboard/screenshot.jpg)

An interactive web dashboard for browsing existing results (no API key needed — it only reads `results/*.json` and `configs/ablations.json`):

```bash
python3 dashboard/server.py        # serves http://127.0.0.1:8000
```

- **Game Replay** — step through any recorded game move-by-move on a live board, with each team's monitor-classified deliberation (agreement level, dominant behavior, quality, dissent, key insight) shown per move.
- **Leaderboard** — win/loss/draw record and score per org.
- **Deliberation Analysis** — interactive charts comparing agreement level, dominant behavior, dissent rate, and token usage across orgs.
- **Orgs** — the agent roster, persona, and deliberation style behind each org.

The backend is stdlib-only Python (`http.server`); the frontend is vanilla JS pulling in chess.js/chessboard.js/Chart.js from a CDN.

## Architecture

```
src/
  main.py        - CLI entry point
  game.py        - Game orchestration, move loop
  team.py        - Team coordination (deliberation cycle)
  agents.py      - OpenRouter API calls, proposal + submission
  monitor.py     - Chain-of-thought classifier (per move)
  leaderboard.py - Results tracking and leaderboard
  config.py      - API keys and path constants

configs/
  ablations.json - Org configurations (models, personas, styles)

results/
  game_*.json    - Per-game result files with full move analyses

dashboard/
  server.py      - Stdlib-only HTTP server + read-only JSON API over results/
  static/        - Vanilla JS frontend (board replay, leaderboard, charts)
```

## Org Ablations

Defined in `configs/ablations.json`. Each org specifies:
- `agents`: list of 3 agents, each with `model`, `role`, `persona`
- `deliberation_style`: `"consensus"` (majority-respecting) or `"advisory"` (leader decides)
- `submitter_rotation`: `"round_robin"` or `"fixed_leader"`

### Adding a New Org

Add an entry to `configs/ablations.json`:

```json
{
  "id": "my-org",
  "name": "My Org",
  "description": "...",
  "agents": [
    {"model": "anthropic/claude-3-haiku", "role": "captain", "persona": "..."},
    {"model": "openai/gpt-4o-mini", "role": "analyst", "persona": "..."},
    {"model": "google/gemma-2-9b-it", "role": "critic", "persona": "..."}
  ],
  "deliberation_style": "consensus",
  "submitter_rotation": "round_robin"
}
```

## Output

Each game produces:
- `results/game_<id>.json` with full move-by-move analysis
- PGN string for replay
- Leaderboard updated automatically

### Move Analysis Fields

Per move, the monitor classifies:
- `agreement_level`: unanimous | majority | override | solo
- `dominant_behavior`: aggressive | defensive | positional | tactical | blunder
- `deliberation_quality`: high | medium | low
- `key_insight`: one-sentence summary
- `dissent_detected`: boolean

## Models

Default models (via OpenRouter) use `meta-llama/llama-3.1-8b-instruct` for fast/cheap testing. Upgrade by editing `configs/ablations.json` to use stronger models like `anthropic/claude-3-5-sonnet`, `openai/gpt-4o`, etc.

## Leaderboard

```
============================================================
CHESS BENCHMARK LEADERBOARD (N games)
============================================================
Org                          W    L    D   Win%  Score  Avg Tok
------------------------------------------------------------
consensus                    2    1    1  50.0%    2.5   18500
autocratic                   1    2    1  25.0%    1.5   17800
============================================================
```
