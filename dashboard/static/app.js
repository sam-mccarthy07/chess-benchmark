// Chess Benchmark Dashboard — vanilla JS, no build step.
// Board rendering is a small hand-rolled Unicode-glyph grid (not chessboard.js):
// the published chessboard.js 1.0.0 package doesn't actually ship piece images
// on any CDN, so a from-scratch renderer avoids depending on a third-party
// image host just to draw 32 chess pieces.

const PIECE_GLYPHS = {
  w: { p: "♙", n: "♘", b: "♗", r: "♖", q: "♕", k: "♔" },
  b: { p: "♟", n: "♞", b: "♝", r: "♜", q: "♛", k: "♚" },
};

function renderBoard(fen) {
  const chess = fen && fen !== "start" ? new Chess(fen) : new Chess();
  const rows = chess.board(); // rank 8 -> rank 1, each file a -> h
  const boardEl = document.getElementById("board");
  boardEl.innerHTML = "";
  rows.forEach((row, r) => {
    row.forEach((cell, c) => {
      const sq = document.createElement("div");
      const light = (r + c) % 2 === 0;
      sq.className = `sq ${light ? "light" : "dark"}`;
      if (cell) sq.textContent = PIECE_GLYPHS[cell.color][cell.type];
      if (c === 0) {
        const rank = document.createElement("span");
        rank.className = "rank-label";
        rank.textContent = 8 - r;
        sq.appendChild(rank);
      }
      boardEl.appendChild(sq);
    });
  });
}

function renderFileLabels() {
  const el = document.getElementById("file-labels");
  el.innerHTML = "abcdefgh".split("").map(f => `<span>${f}</span>`).join("");
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

// ---------- Tabs ----------

function initTabs() {
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "analysis" && !window.__chartsLoaded) loadAnalysis();
    });
  });
}

// ---------- Game Replay ----------

const state = {
  board: null,
  games: [],
  current: null,   // full game JSON
  positions: [],    // fen after each move (index 0 = start)
  sans: [],
  idx: 0,           // 0 = start position, i = after move i
  playTimer: null,
  loadToken: 0,     // guards against a slow fetch clobbering a newer game selection
};

function uciToMoveObj(uci) {
  return { from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci.length > 4 ? uci.slice(4) : undefined };
}

function buildPositions(moveAnalyses) {
  const chess = new Chess();
  const positions = [chess.fen()];
  const sans = [null];
  let ok = true;
  for (const m of moveAnalyses) {
    if (ok) {
      const r = chess.move(uciToMoveObj(m.move || ""));
      if (r) {
        positions.push(chess.fen());
        sans.push(r.san);
        continue;
      }
      ok = false; // stop replaying past an illegal/corrupt move, but keep listing rows
    }
    positions.push(positions[positions.length - 1]);
    sans.push(null);
  }
  return { positions, sans };
}

function agreementClass(v) {
  return ["unanimous", "majority", "override"].includes(v) ? v : "other";
}
function qualityClass(v) {
  return ["high", "medium", "low"].includes(v) ? v : "";
}
function isDissent(v) {
  if (typeof v === "boolean") return v;
  if (typeof v === "string") return v.trim().toLowerCase() === "true";
  return false;
}

function renderMoveList(game) {
  const tbody = document.querySelector("#move-list tbody");
  tbody.innerHTML = "";
  game.move_analyses.forEach((m, i) => {
    const tr = document.createElement("tr");
    tr.dataset.idx = i + 1;
    const displayMove = state.sans[i + 1] || m.move || "?";
    tr.innerHTML = `<td>${m.move_number}</td><td>${m.org_id}</td><td>${displayMove}</td>` +
      `<td>${m.agreement_level}</td><td>${m.dominant_behavior}</td>`;
    tr.addEventListener("click", () => setIndex(i + 1));
    tbody.appendChild(tr);
  });
}

function renderAnalysisCard(game, idx) {
  const card = document.getElementById("move-analysis-card");
  if (idx === 0) {
    card.innerHTML = `<p class="placeholder">Start position. Click a move, or press Play, to step through the game.</p>`;
    return;
  }
  const m = game.move_analyses[idx - 1];
  const isWhite = m.org_id === game.white_org;
  const orgName = isWhite ? game.white_name : (m.org_id === game.black_org ? game.black_name : m.org_id);
  const dissent = isDissent(m.dissent_detected);
  const san = state.sans[idx] || m.move;

  card.innerHTML = `
    <h4>Move ${m.move_number} — ${orgName} (${isWhite ? "White" : "Black"})</h4>
    <div class="badges">
      <span class="badge agreement-${agreementClass(m.agreement_level)}">${m.agreement_level}</span>
      <span class="badge">${m.dominant_behavior}</span>
      <span class="badge quality-${qualityClass(m.deliberation_quality)}">${m.deliberation_quality} quality</span>
      ${dissent ? '<span class="badge dissent-true">dissent</span>' : ""}
    </div>
    <p class="insight">${m.key_insight || ""}</p>
    <div class="meta-row">Move: ${san} &nbsp;·&nbsp; ${m.tokens_total.toLocaleString()} tokens &nbsp;·&nbsp; ${Math.round(m.latency_total_ms || 0).toLocaleString()} ms</div>
  `;
}

function setIndex(idx) {
  state.idx = Math.max(0, Math.min(idx, state.positions.length - 1));
  renderBoard(state.positions[state.idx]);
  document.querySelectorAll("#move-list tbody tr").forEach(tr => {
    tr.classList.toggle("current", Number(tr.dataset.idx) === state.idx);
  });
  renderAnalysisCard(state.current, state.idx);
}

function renderGameMeta(game) {
  const el = document.getElementById("game-meta");
  el.textContent = `${game.white_name} vs ${game.black_name} — ${game.result} (${game.result_reason}), ${game.total_moves} plies`;
}

async function loadGame(gameId) {
  stopPlay();
  const token = ++state.loadToken;
  document.querySelector("#move-list tbody").innerHTML = "";
  document.getElementById("move-analysis-card").innerHTML = `<p class="placeholder">Loading…</p>`;
  const game = await getJSON(`/api/games/${gameId}`);
  if (token !== state.loadToken) return; // a newer selection started while this fetch was in flight
  state.current = game;
  const { positions, sans } = buildPositions(game.move_analyses);
  state.positions = positions;
  state.sans = sans;
  renderMoveList(game);
  renderGameMeta(game);
  setIndex(0);
}

function stopPlay() {
  if (state.playTimer) {
    clearInterval(state.playTimer);
    state.playTimer = null;
    document.getElementById("btn-play").textContent = "Play";
  }
}

function togglePlay() {
  if (state.playTimer) {
    stopPlay();
    return;
  }
  document.getElementById("btn-play").textContent = "Pause";
  state.playTimer = setInterval(() => {
    if (state.idx >= state.positions.length - 1) {
      stopPlay();
      return;
    }
    setIndex(state.idx + 1);
  }, 900);
}

async function initGames() {
  renderFileLabels();
  renderBoard("start");

  state.games = await getJSON("/api/games");
  const select = document.getElementById("game-select");
  select.innerHTML = state.games.map(g =>
    `<option value="${g.game_id}">${g.white_name} vs ${g.black_name} — ${g.result} (${g.game_id})</option>`
  ).join("");
  select.addEventListener("change", () => loadGame(select.value));

  document.getElementById("btn-first").addEventListener("click", () => { stopPlay(); setIndex(0); });
  document.getElementById("btn-prev").addEventListener("click", () => { stopPlay(); setIndex(state.idx - 1); });
  document.getElementById("btn-next").addEventListener("click", () => { stopPlay(); setIndex(state.idx + 1); });
  document.getElementById("btn-last").addEventListener("click", () => { stopPlay(); setIndex(state.positions.length - 1); });
  document.getElementById("btn-play").addEventListener("click", togglePlay);

  if (state.games.length) await loadGame(state.games[0].game_id);
}

// ---------- Leaderboard ----------

async function loadLeaderboard() {
  const rows = await getJSON("/api/leaderboard");
  const tbody = document.querySelector("#leaderboard-table tbody");
  tbody.innerHTML = rows.map(o => `
    <tr>
      <td>${o.name}</td>
      <td>${o.games}</td>
      <td>${o.wins}</td>
      <td>${o.losses}</td>
      <td>${o.draws}</td>
      <td>${o.win_pct}%</td>
      <td>${o.score}</td>
      <td>${o.avg_tokens.toLocaleString()}</td>
    </tr>
  `).join("");
}

// ---------- Analysis charts ----------

const PALETTE = ["#6ea8fe", "#4caf7d", "#d9a441", "#e0616b", "#a889f5", "#5bc0be"];

function bucketCounts(counts, known) {
  const out = {};
  known.forEach(k => out[k] = counts[k] || 0);
  let other = 0;
  for (const [k, v] of Object.entries(counts)) {
    if (!known.includes(k)) other += v;
  }
  out.other = other;
  return out;
}

function stackedChart(canvasId, orgIds, series, seriesLabels) {
  new Chart(document.getElementById(canvasId), {
    type: "bar",
    data: {
      labels: orgIds,
      datasets: seriesLabels.map((label, i) => ({
        label,
        data: series.map(s => s[label]),
        backgroundColor: PALETTE[i % PALETTE.length],
      })),
    },
    options: {
      responsive: true,
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } },
      plugins: { legend: { labels: { color: "#e6e8ef" } } },
    },
  });
}

function barChart(canvasId, orgIds, values, label) {
  new Chart(document.getElementById(canvasId), {
    type: "bar",
    data: {
      labels: orgIds,
      datasets: [{ label, data: values, backgroundColor: "#6ea8fe" }],
    },
    options: {
      responsive: true,
      scales: { y: { beginAtZero: true } },
      plugins: { legend: { display: false } },
    },
  });
}

async function loadAnalysis() {
  window.__chartsLoaded = true;
  const data = await getJSON("/api/analysis");
  const orgIds = data.map(d => d.org_id);

  const agreementKnown = ["unanimous", "majority", "override"];
  const agreementSeries = data.map(d => bucketCounts(d.agreement_counts, agreementKnown));
  stackedChart("chart-agreement", orgIds, agreementSeries, [...agreementKnown, "other"]);

  const behaviorKnown = ["tactical", "aggressive", "positional", "defensive"];
  const behaviorSeries = data.map(d => bucketCounts(d.behavior_counts, behaviorKnown));
  stackedChart("chart-behavior", orgIds, behaviorSeries, [...behaviorKnown, "other"]);

  barChart("chart-dissent", orgIds, data.map(d => d.dissent_rate), "Dissent rate (%)");
  barChart("chart-tokens", orgIds, data.map(d => d.avg_tokens_per_move), "Avg tokens / move");
}

// ---------- Orgs ----------

async function loadOrgs() {
  const cfg = await getJSON("/api/orgs");
  const grid = document.getElementById("orgs-grid");
  grid.innerHTML = cfg.orgs.map(org => `
    <div class="org-card">
      <h4>${org.name}</h4>
      <div class="desc">${org.description}</div>
      <div class="meta-row">${org.deliberation_style} &middot; ${org.submitter_rotation}</div>
      ${org.agents.map(a => `
        <div class="agent">
          <div><span class="role">${a.role}</span> <span class="model">${a.model}</span></div>
          <div>${a.persona}</div>
        </div>
      `).join("")}
    </div>
  `).join("");
}

// ---------- Boot ----------

initTabs();
initGames();
loadLeaderboard();
loadOrgs();
