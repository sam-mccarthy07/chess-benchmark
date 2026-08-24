# Run report

## Config `be4f0b9bc8824901` · position set `v1`

### Data health

Read this before anything below it. A metric computed over unreliable data is not a weak result.

| | |
|---|---|
| Games / turns / positions | 2 / 40 / 1 |
| **Proposals legal** | **97.5%** of 120 |
| Illegal / unparseable / API error | 3 / 0 / 0 |
| Decision legal | 92.5% [80%, 97%] (37/40) |
| False consensus (team ratified an illegal move) | 3 |
| Off-slate decisions | 3 |
| Turns needing a resolution fallback | 3 |
| Oracle coverage | 92% |
| Solo-probe coverage | 92% |
| Total tokens | 319,176 |

### Headline metrics

Effect sizes with 95% intervals. No p-values: the pre-registration applies FDR control across four confirmatory hypotheses, and significance testing belongs there, not here.

#### `pilot-advisory` — advisory / fixed_leader / 2 rounds / heterogeneous (20 turns)

| Metric | Value |
|---|---|
| **Collaborative Advantage** (H1) | **-39.41 [-102.29, 5.47] (n=17)** |
| Team beat its best member | 5.0% [1%, 24%] (1/20) |
| Idea quality Δ_ceiling (H3) | 184.94 [89.06, 302.53] (n=17) |
| Aggregation loss Δ_selection (H3) | 10.18 [2.24, 19.41] (n=17) |
| **Aggregation share of total loss** | **5%** |
| Submitter took the best proposal | 75.0% [53%, 89%] (15/20) |
| **Introspective gap** (H4) | **-0.1 [-0.18, -0.02] (n=20)** |
| Revealed influence | 0.7 [0.57, 0.83] (n=20) |
| Unanimity round 0 → final (H8) | 5.0% [1%, 24%] (1/20) → 50.0% [30%, 70%] (10/20) |
| Split teams that converged | 45.0% [26%, 66%] (9/20) |
| Distinct moves at round 0 | 2.45 [2.2, 2.7] (n=20) |
| Productive persuasion / destructive conformity | 16 / 14 |
| Played-move CPL | 195.12 [100.18, 312.12] (n=17) |
| Blunder rate | 10.0% [3%, 30%] (2/20) |

#### `pilot-consensus` — consensus / round_robin / 2 rounds / heterogeneous (20 turns)

| Metric | Value |
|---|---|
| **Collaborative Advantage** (H1) | **-31.79 [-130.71, 66.71] (n=14, 1 excluded)** |
| Team beat its best member | 11.8% [3%, 34%] (2/17) |
| Idea quality Δ_ceiling (H3) | 62.87 [32.4, 101.93] (n=15) |
| Aggregation loss Δ_selection (H3) | 91.29 [21.71, 179.57] (n=14, 1 excluded) |
| **Aggregation share of total loss** | **59%** |
| Submitter took the best proposal | 64.7% [41%, 83%] (11/17) |
| **Introspective gap** (H4) | **-0.05 [-0.12, 0.02] (n=20)** |
| Revealed influence | 0.8 [0.67, 0.92] (n=20) |
| Unanimity round 0 → final (H8) | 5.0% [1%, 24%] (1/20) → 35.0% [18%, 57%] (7/20) |
| Split teams that converged | 30.0% [15%, 52%] (6/20) |
| Distinct moves at round 0 | 2.45 [2.2, 2.7] (n=20) |
| Productive persuasion / destructive conformity | 13 / 10 |
| Played-move CPL | 148.07 [74.93, 232.2] (n=15) |
| Blunder rate | 10.0% [3%, 30%] (2/20) |
