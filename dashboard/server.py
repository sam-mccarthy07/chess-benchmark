#!/usr/bin/env python3
"""Dashboard server for the chess benchmark. Stdlib only, no extra deps.

Serves the static frontend plus a small read-only JSON API over the
existing results/*.json game files and configs/ablations.json. Never
touches the OpenRouter API or any credentials.
"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).parent.parent
RESULTS_DIR = PROJECT_ROOT / "results"
CONFIGS_DIR = PROJECT_ROOT / "configs"
STATIC_DIR = Path(__file__).parent / "static"

GAME_ID_RE = re.compile(r"^[a-f0-9]{6,10}$")

STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def load_games():
    games = []
    for fp in sorted(RESULTS_DIR.glob("game_*.json")):
        with open(fp) as f:
            games.append(json.load(f))
    games.sort(key=lambda g: g.get("timestamp", ""))
    return games


def coerce_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


def build_leaderboard(games):
    orgs = {}
    for g in games:
        for org_id, name, outcome in (
            (g["white_org"], g["white_name"], g["result"]),
            (g["black_org"], g["black_name"], g["result"]),
        ):
            o = orgs.setdefault(org_id, {
                "org_id": org_id, "name": name,
                "wins": 0, "losses": 0, "draws": 0,
                "games": 0, "total_tokens": 0,
            })
            o["games"] += 1
            is_white = org_id == g["white_org"]
            if g["result"] == "draw":
                o["draws"] += 1
            elif (g["result"] == "white") == is_white:
                o["wins"] += 1
            else:
                o["losses"] += 1
            o["total_tokens"] += g["white_tokens"] if is_white else g["black_tokens"]

    board = []
    for o in orgs.values():
        score = o["wins"] + 0.5 * o["draws"]
        board.append({
            **o,
            "score": score,
            "win_pct": round(100 * o["wins"] / o["games"], 1) if o["games"] else 0,
            "avg_tokens": round(o["total_tokens"] / o["games"]) if o["games"] else 0,
        })
    board.sort(key=lambda o: -o["score"])
    return board


def build_analysis(games):
    per_org = {}
    for g in games:
        for m in g["move_analyses"]:
            org_id = m["org_id"]
            a = per_org.setdefault(org_id, {
                "org_id": org_id,
                "agreement_counts": {},
                "behavior_counts": {},
                "quality_counts": {},
                "dissent_moves": 0,
                "total_moves": 0,
                "total_tokens": 0,
                "total_latency_ms": 0.0,
            })
            a["total_moves"] += 1
            a["total_tokens"] += m.get("tokens_total", 0)
            a["total_latency_ms"] += m.get("latency_total_ms", 0) or 0
            agreement = m.get("agreement_level") or "unknown"
            behavior = m.get("dominant_behavior") or "unknown"
            quality = m.get("deliberation_quality") or "unknown"
            a["agreement_counts"][agreement] = a["agreement_counts"].get(agreement, 0) + 1
            a["behavior_counts"][behavior] = a["behavior_counts"].get(behavior, 0) + 1
            a["quality_counts"][quality] = a["quality_counts"].get(quality, 0) + 1
            if coerce_bool(m.get("dissent_detected")):
                a["dissent_moves"] += 1

    for a in per_org.values():
        n = a["total_moves"] or 1
        a["dissent_rate"] = round(100 * a["dissent_moves"] / n, 1)
        a["avg_tokens_per_move"] = round(a["total_tokens"] / n)
        a["avg_latency_ms"] = round(a["total_latency_ms"] / n)

    return list(per_org.values())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path):
        if rel_path == "" or rel_path == "/":
            rel_path = "index.html"
        target = (STATIC_DIR / rel_path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return
        mime = STATIC_MIME.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/games":
            games = load_games()
            summary = [{
                "game_id": g["game_id"],
                "timestamp": g["timestamp"],
                "white_org": g["white_org"],
                "black_org": g["black_org"],
                "white_name": g["white_name"],
                "black_name": g["black_name"],
                "result": g["result"],
                "result_reason": g["result_reason"],
                "total_moves": g["total_moves"],
            } for g in games]
            self._send_json(summary)
            return

        m = re.match(r"^/api/games/([a-zA-Z0-9]+)$", path)
        if m:
            game_id = m.group(1)
            if not GAME_ID_RE.match(game_id):
                self.send_error(400, "invalid game id")
                return
            fp = RESULTS_DIR / f"game_{game_id}.json"
            if not fp.is_file():
                self.send_error(404, "game not found")
                return
            with open(fp) as f:
                self._send_json(json.load(f))
            return

        if path == "/api/leaderboard":
            self._send_json(build_leaderboard(load_games()))
            return

        if path == "/api/analysis":
            self._send_json(build_analysis(load_games()))
            return

        if path == "/api/orgs":
            with open(CONFIGS_DIR / "ablations.json") as f:
                self._send_json(json.load(f))
            return

        if path.startswith("/api/"):
            self.send_error(404)
            return

        self._send_static(path)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Chess benchmark dashboard: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
