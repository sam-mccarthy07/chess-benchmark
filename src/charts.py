"""Chart generation for chess benchmark results."""

import json
from collections import defaultdict
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

from config import RESULTS_DIR
from leaderboard import load_all_games, compute_leaderboard


# Consistent color palette per org
ORG_COLORS = {
    "nano-consensus":   "#4e79a7",
    "nano-autocratic":  "#f28e2b",
    "qwen-consensus":   "#59a14f",
    "apex-consensus":   "#e15759",
}
DEFAULT_COLOR = "#76b7b2"


def _org_color(org_id: str) -> str:
    return ORG_COLORS.get(org_id, DEFAULT_COLOR)


def _org_label(org_id: str) -> str:
    labels = {
        "nano-consensus":  "Nano\nConsensus",
        "nano-autocratic": "Nano\nAutocratic",
        "qwen-consensus":  "Qwen\nConsensus",
        "apex-consensus":  "Apex\nConsensus",
    }
    return labels.get(org_id, org_id)


def chart_leaderboard(out_path: Path) -> Path:
    """Bar chart: W/L/D per org, sorted by score."""
    lb = compute_leaderboard()
    orgs = lb["leaderboard"]
    if not orgs:
        return None

    org_ids = [o["org_id"] for o in orgs]
    wins = [o["wins"] for o in orgs]
    losses = [o["losses"] for o in orgs]
    draws = [o["draws"] for o in orgs]
    labels = [_org_label(oid) for oid in org_ids]

    x = np.arange(len(org_ids))
    w = 0.25

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(x - w, wins,   w, label="Wins",   color="#59a14f")
    ax.bar(x,     draws,  w, label="Draws",  color="#f28e2b")
    ax.bar(x + w, losses, w, label="Losses", color="#e15759")

    # Score annotation
    for i, o in enumerate(orgs):
        ax.text(x[i], max(wins[i], draws[i], losses[i]) + 0.05, f"Score: {o['score']:.1f}",
                ha="center", fontsize=9, color="black")

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylabel("Games")
    ax.set_title("Leaderboard: Wins / Draws / Losses per Org", fontsize=13, pad=12)
    ax.legend()
    ax.set_ylim(0, max(wins + losses + draws) + 1.5)
    ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def chart_win_rate(out_path: Path) -> Path:
    """Horizontal bar: win rate per org."""
    lb = compute_leaderboard()
    orgs = lb["leaderboard"]
    if not orgs:
        return None

    org_ids = [o["org_id"] for o in orgs]
    win_rates = [o["win_rate"] * 100 for o in orgs]
    labels = [_org_label(oid) for oid in org_ids]
    colors = [_org_color(oid) for oid in org_ids]

    fig, ax = plt.subplots(figsize=(9, 4))
    bars = ax.barh(labels, win_rates, color=colors)
    for bar, wr in zip(bars, win_rates):
        ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
                f"{wr:.1f}%", va="center", fontsize=10)
    ax.set_xlabel("Win Rate (%)")
    ax.set_xlim(0, 110)
    ax.set_title("Win Rate by Org", fontsize=13, pad=12)
    ax.axvline(50, color="gray", linestyle="--", linewidth=0.8, label="50%")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def chart_token_usage(out_path: Path) -> Path:
    """Bar chart: avg tokens per game per org."""
    lb = compute_leaderboard()
    orgs = lb["leaderboard"]
    if not orgs:
        return None

    org_ids = [o["org_id"] for o in orgs]
    avg_tokens = [o["avg_tokens_per_game"] for o in orgs]
    labels = [_org_label(oid) for oid in org_ids]
    colors = [_org_color(oid) for oid in org_ids]

    fig, ax = plt.subplots(figsize=(9, 4))
    bars = ax.bar(labels, avg_tokens, color=colors)
    for bar, t in zip(bars, avg_tokens):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 50,
                f"{t:,.0f}", ha="center", fontsize=9)
    ax.set_ylabel("Avg Tokens per Game")
    ax.set_title("Token Usage per Org (as White)", fontsize=13, pad=12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def chart_agreement_distribution(out_path: Path) -> Path:
    """Stacked bar: agreement_level distribution per org."""
    games = load_all_games()
    if not games:
        return None

    levels = ["unanimous", "majority", "override", "solo", "unknown"]
    level_colors = {
        "unanimous": "#59a14f",
        "majority":  "#4e79a7",
        "override":  "#f28e2b",
        "solo":      "#e15759",
        "unknown":   "#bab0ac",
    }

    org_data: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for game in games:
        for ma in game.move_analyses:
            org = ma.get("org_id", "unknown")
            lvl = ma.get("agreement_level", "unknown")
            org_data[org][lvl] += 1

    if not org_data:
        return None

    org_ids = sorted(org_data.keys())
    totals = {oid: sum(org_data[oid].values()) for oid in org_ids}
    labels = [_org_label(oid) for oid in org_ids]

    x = np.arange(len(org_ids))
    fig, ax = plt.subplots(figsize=(10, 5))

    bottoms = np.zeros(len(org_ids))
    for lvl in levels:
        vals = np.array([
            (org_data[oid].get(lvl, 0) / totals[oid] * 100) if totals[oid] > 0 else 0
            for oid in org_ids
        ])
        ax.bar(x, vals, bottom=bottoms, label=lvl.capitalize(), color=level_colors[lvl])
        bottoms += vals

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylabel("% of Moves")
    ax.set_ylim(0, 110)
    ax.set_title("Agreement Level Distribution per Org", fontsize=13, pad=12)
    ax.legend(loc="upper right", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def chart_behavior_distribution(out_path: Path) -> Path:
    """Stacked bar: dominant_behavior distribution per org."""
    games = load_all_games()
    if not games:
        return None

    behaviors = ["aggressive", "tactical", "positional", "defensive", "blunder", "unknown"]
    bcolors = {
        "aggressive": "#e15759",
        "tactical":   "#f28e2b",
        "positional": "#4e79a7",
        "defensive":  "#59a14f",
        "blunder":    "#b07aa1",
        "unknown":    "#bab0ac",
    }

    org_data: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for game in games:
        for ma in game.move_analyses:
            org = ma.get("org_id", "unknown")
            beh = ma.get("dominant_behavior", "unknown")
            org_data[org][beh] += 1

    if not org_data:
        return None

    org_ids = sorted(org_data.keys())
    totals = {oid: sum(org_data[oid].values()) for oid in org_ids}
    labels = [_org_label(oid) for oid in org_ids]
    x = np.arange(len(org_ids))

    fig, ax = plt.subplots(figsize=(10, 5))
    bottoms = np.zeros(len(org_ids))
    for beh in behaviors:
        vals = np.array([
            (org_data[oid].get(beh, 0) / totals[oid] * 100) if totals[oid] > 0 else 0
            for oid in org_ids
        ])
        ax.bar(x, vals, bottom=bottoms, label=beh.capitalize(), color=bcolors[beh])
        bottoms += vals

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylabel("% of Moves")
    ax.set_ylim(0, 115)
    ax.set_title("Dominant Behavior Distribution per Org", fontsize=13, pad=12)
    ax.legend(loc="upper right", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def chart_deliberation_quality(out_path: Path) -> Path:
    """Stacked bar: deliberation quality distribution per org."""
    games = load_all_games()
    if not games:
        return None

    qualities = ["high", "medium", "low", "unknown"]
    qcolors = {
        "high":    "#59a14f",
        "medium":  "#f28e2b",
        "low":     "#e15759",
        "unknown": "#bab0ac",
    }

    org_data: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for game in games:
        for ma in game.move_analyses:
            org = ma.get("org_id", "unknown")
            q = ma.get("deliberation_quality", "unknown")
            org_data[org][q] += 1

    if not org_data:
        return None

    org_ids = sorted(org_data.keys())
    totals = {oid: sum(org_data[oid].values()) for oid in org_ids}
    labels = [_org_label(oid) for oid in org_ids]
    x = np.arange(len(org_ids))

    fig, ax = plt.subplots(figsize=(10, 5))
    bottoms = np.zeros(len(org_ids))
    for q in qualities:
        vals = np.array([
            (org_data[oid].get(q, 0) / totals[oid] * 100) if totals[oid] > 0 else 0
            for oid in org_ids
        ])
        ax.bar(x, vals, bottom=bottoms, label=q.capitalize(), color=qcolors[q])
        bottoms += vals

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylabel("% of Moves")
    ax.set_ylim(0, 115)
    ax.set_title("Deliberation Quality Distribution per Org", fontsize=13, pad=12)
    ax.legend(loc="upper right", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def chart_latency_per_move(out_path: Path) -> Path:
    """Box plot: latency per move per org."""
    games = load_all_games()
    if not games:
        return None

    org_latencies: dict[str, list[float]] = defaultdict(list)
    for game in games:
        for ma in game.move_analyses:
            org = ma.get("org_id", "unknown")
            lat = ma.get("latency_total_ms", 0)
            if lat > 0:
                org_latencies[org].append(lat / 1000)  # convert to seconds

    if not org_latencies:
        return None

    org_ids = sorted(org_latencies.keys())
    data = [org_latencies[oid] for oid in org_ids]
    labels = [_org_label(oid) for oid in org_ids]
    colors = [_org_color(oid) for oid in org_ids]

    fig, ax = plt.subplots(figsize=(10, 5))
    bp = ax.boxplot(data, patch_artist=True)
    ax.set_xticklabels(labels)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    ax.set_ylabel("Latency per Move (seconds)")
    ax.set_title("Deliberation Latency per Move by Org", fontsize=13, pad=12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def generate_all_charts(out_dir: Path = None) -> list[Path]:
    """Generate all charts and return list of output paths."""
    if out_dir is None:
        out_dir = RESULTS_DIR / "charts"
    out_dir.mkdir(exist_ok=True)

    charts = [
        (chart_leaderboard,            out_dir / "01_leaderboard.png"),
        (chart_win_rate,               out_dir / "02_win_rate.png"),
        (chart_token_usage,            out_dir / "03_token_usage.png"),
        (chart_agreement_distribution, out_dir / "04_agreement.png"),
        (chart_behavior_distribution,  out_dir / "05_behavior.png"),
        (chart_deliberation_quality,   out_dir / "06_quality.png"),
        (chart_latency_per_move,       out_dir / "07_latency.png"),
    ]

    generated = []
    for fn, path in charts:
        try:
            result = fn(path)
            if result:
                generated.append(result)
                print(f"Generated: {path.name}")
        except Exception as e:
            print(f"Chart {path.name} failed: {e}")

    return generated


if __name__ == "__main__":
    paths = generate_all_charts()
    print(f"\nGenerated {len(paths)} charts in {RESULTS_DIR / 'charts'}")
