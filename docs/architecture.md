# Architecture Overview

The engine is organized as a small pipeline from assumptions to recommendations and diagnostics.

```text
configs/base.yaml
        |
        v
EngineConfig validation
        |
        v
synthetic customer generation
        |
        v
configured rate grids
  - market-relative spread grids
  - absolute fallback grids
        |
        v
response functions
  - home loan acceptance probability
  - term deposit uptake probability
        |
        v
economic objective
  + loan interest income
  - expected credit loss
  - deposit interest cost
  - wholesale funding cost
  - funding shortfall penalty
  + excess deposit income
        |
        v
joint grid search
        |
        v
adaptive boundary follow-up
        |
        v
scenario diagnostics and sensitivity analysis
```

## Main Modules

- `src/balance_pricing_engine/config.py`: loads and validates YAML configuration.
- `src/balance_pricing_engine/rate_grids.py`: builds configured pricing grids from either
  market-relative spreads or absolute product grids.
- `src/balance_pricing_engine/synthetic/`: generates synthetic customers, market state, and response
  probabilities.
- `src/balance_pricing_engine/pricing/`: calculates objective components such as loan income,
  credit loss, deposit cost, wholesale funding cost, and funding shortfall penalty.
- `src/balance_pricing_engine/optimization/grid_search.py`: evaluates rate pairs and selects the
  highest-objective result.
- `src/balance_pricing_engine/diagnostics.py`: explains objective components, detects boundary
  issues, expands grids when needed, and runs sensitivity cases.
- `src/balance_pricing_engine/scenarios/runner.py`: orchestrates baseline scenario diagnostics and
  artifact persistence.
- `src/balance_pricing_engine/run_ledger/`: creates governed pricing-run ledgers by wrapping
  diagnostics artifacts with metadata, configuration snapshots, pricing memos, and governance
  checklists.
- `src/balance_pricing_engine/modelling/`: builds synthetic observed datasets and trains
  diagnostic-only response models.

## Adaptive Boundary Flow

The baseline and sensitivity runners start with the configured grid. If the accepted result is at a
minimum or maximum boundary, the hit side of the grid is expanded by the current grid span and the
search is rerun. Expansion stops when the accepted result is no longer at a boundary or when the
maximum expansion count is reached.

The final `boundary_warning_flag` reports the accepted result after expansion. The
`initial_boundary_warning_flag` and `expansion_rounds` fields show whether remediation was needed.
If the final flag remains true, the optimum may still sit outside the searched domain and the
boundary issue should remain open for review.

## Artifact Flow

`ScenarioDiagnostics.write_artifacts()` writes baseline regression outputs:

- `grid_results.csv`: every final evaluated rate pair
- `top_results.csv`: highest objective candidates
- `counterfactuals.csv`: one-axis objective decompositions for top candidates
- `cohort_attribution.csv`: expected winning volumes by coarse synthetic customer segment, when
  cohort context is available
- `sensitivity.csv`: sensitivity output, when provided
- `summary.json`: accepted recommendation and boundary metadata

The run ledger layer is the preferred output orchestration path for reproducible records.
`write_run_ledger()` creates a collision-safe directory under `outputs/runs/` using a single
UTC timestamp for both the directory name and `metadata.json`, then writes:

- `metadata.json`: run ID, timestamp, config hash, git/package details, and governance flags
- `config_snapshot.yaml`: exact YAML configuration used for the run
- the diagnostics artifacts listed above
- `pricing_memo.md`: business-facing recommendation, decomposition, sensitivity, and risk notes
- `governance_checklist.md`: manual review checklist for accepting the recommendation

Artifact filenames inside a ledger directory remain fixed. Reusing an output directory can overwrite
prior files, so run ledger directory creation is atomic and appends a numeric suffix if a same-timestamp
directory already exists.
