# Run reports

One directory per run, written automatically when a run finishes.

- `run_report.md` — health block then headline metrics, as they stood at the
  end of that run
- `turns.csv` — tidy per-turn rows, the substrate for analysis
- `games.txt` — the game ids this run produced

These are committed; `results/` is not. Regenerating a report later against an
accumulated `results/` answers a different question, so the copy written at the
time is the record.
