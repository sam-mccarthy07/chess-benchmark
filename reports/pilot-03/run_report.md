# Run report

## Config `ef0b1c6a9cf5d084` · position set `v1`

### Data health

Read this before anything below it. A metric computed over unreliable data is not a weak result.

| | |
|---|---|
| Games / turns / positions | 8 / 160 / 4 |
| **Proposals legal** | **98.3%** of 480 |
| Illegal / unparseable / API error | 8 / 0 / 0 |
| Decision legal | 95.6% [91%, 98%] (153/160) |
| False consensus (team ratified an illegal move) | 7 |
| Off-slate decisions | 7 |
| Turns needing a resolution fallback | 7 |
| Oracle coverage | 96% |
| Solo-probe coverage | 96% |
| Total tokens | 1,309,170 |

### Headline metrics

Effect sizes with 95% intervals. No p-values: the pre-registration applies FDR control across four confirmatory hypotheses, and significance testing belongs there, not here.

#### `pilot-advisory` — advisory / fixed_leader / 2 rounds / heterogeneous (80 turns)

| Metric | Value |
|---|---|
| **Collaborative Advantage** (H1) | **-98.87 [-157.15, -39.83] (n=71, 4 excluded)** |
| Team beat its best member | 16.0% [9%, 26%] (12/75) |
| Idea quality Δ_ceiling (H3) | 339.81 [271.77, 412.61] (n=75) |
| Aggregation loss Δ_selection (H3) | 69.59 [34.72, 110.24] (n=71, 4 excluded) |
| **Aggregation share of total loss** | **17%** |
| Submitter took the best proposal | 69.3% [58%, 79%] (52/75) |
| **Introspective gap** (H4) | **-0.15 [-0.19, -0.1] (n=80)** |
| Revealed influence | 0.74 [0.68, 0.8] (n=80) |
| Unanimity round 0 → final (H8) | 6.2% [3%, 14%] (5/80) → 46.2% [36%, 57%] (37/80) |
| Split teams that converged | 40.0% [30%, 51%] (32/80) |
| Distinct moves at round 0 | 2.5 [2.36, 2.64] (n=80) |
| Productive persuasion / destructive conformity | 60 / 49 |
| Played-move CPL | 405.69 [330.25, 487.88] (n=75) |
| Blunder rate | 41.2% [31%, 52%] (33/80) |

#### `pilot-consensus` — consensus / round_robin / 2 rounds / heterogeneous (80 turns)

| Metric | Value |
|---|---|
| **Collaborative Advantage** (H1) | **-146.41 [-197.57, -95.11] (n=74, 2 excluded)** |
| Team beat its best member | 10.3% [5%, 19%] (8/78) |
| Idea quality Δ_ceiling (H3) | 333.12 [261.49, 409.05] (n=76) |
| Aggregation loss Δ_selection (H3) | 75.74 [47.96, 104.28] (n=74, 2 excluded) |
| **Aggregation share of total loss** | **19%** |
| Submitter took the best proposal | 62.8% [52%, 73%] (49/78) |
| **Introspective gap** (H4) | **-0.13 [-0.17, -0.08] (n=80)** |
| Revealed influence | 0.8 [0.75, 0.85] (n=80) |
| Unanimity round 0 → final (H8) | 0.0% [0%, 5%] (0/80) → 32.5% [23%, 43%] (26/80) |
| Split teams that converged | 32.5% [23%, 43%] (26/80) |
| Distinct moves at round 0 | 2.74 [2.64, 2.84] (n=80) |
| Productive persuasion / destructive conformity | 59 / 63 |
| Played-move CPL | 407.7 [335.34, 481.43] (n=76) |
| Blunder rate | 43.8% [33%, 55%] (35/80) |
