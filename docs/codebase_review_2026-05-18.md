# Codebase Review — 2026-05-18

> **Status (2026-06-13): RESOLVED.** All P0, P1, and P2 items below are closed and verified
> against current source (P2 via PR #36, P3 via PR #37; see `agentic_modelling_roadmap.md`).
> The "Recommended next PR" section is historical. This file is retained as a dated review
> record — do not treat its items as live backlog.

Produced by a full-repository audit covering architecture, implementation vs design,
code quality, banking/pricing model, diagnostic ML, competitor-rate ingestion, and tests.

---

## Verification status at time of review

- 238 tests pass (`python3 -m pytest -q`)
- Ruff clean (`python3 -m ruff check .`)
- `run_baseline.py` — 8.00% loan, 5.15% deposit, $12.5M objective, no final boundary warning
- `train_response_models.py` — loan AUC ~0.71, deposit AUC ~0.63, both sanity checks pass
- Golden file matches current baseline output (`tests/fixtures/baseline_golden.json`)

---

## Architecture (confirmed)

```
YAML config
  → EngineConfig (Pydantic v2)
  → rate_grids.build_configured_rate_grids()
       → pricing_strategy (market-relative spreads) OR products (absolute grids)
  → synthetic/customers.py  → loan/deposit DataFrames
  → synthetic/market.py     → MarketState
  → optimization/grid_search.py
       precomputes deposit side (O(D)), loops loan×deposit (O(L×D))
       → OptimizationResult (frozen dataclass + backward-compat aliases)
  → diagnostics.evaluate_adaptive_joint_rate_grid()
       → boundary detection → expand → rerun (max 2 rounds default)
       → AdaptiveGridResult
  → scenarios/runner.run_baseline_diagnostics()
       → ScenarioDiagnostics (frozen, carries customers + market for cohort attribution)
  → run_ledger/writer.write_run_ledger()
       → metadata.json, config_snapshot.yaml, summary.json, grid_results.csv,
          top_results.csv, sensitivity.csv, counterfactuals.csv, cohort_attribution.csv,
          pricing_memo.md, governance_checklist.md
  → modelling/ (diagnostic only — not wired to optimiser)
  → competitor_rates/ (gated behind enabled flag + CLI)
```

---

## P0 — Must fix (confirmed bugs / active data problems) — ✅ all resolved (2026-06-13)

### P0-1 — `datetime.utcnow()` deprecation in run-ledger (3 files)

Files:
- `src/balance_pricing_engine/run_ledger/writer.py:35`
- `src/balance_pricing_engine/run_ledger/directory.py:18`
- `src/balance_pricing_engine/run_ledger/metadata.py:73`

Produces 36 `DeprecationWarning` entries in the test suite. Run timestamps are timezone-naive,
breaking ISO-8601 UTC semantics. Python 3.14 will remove `datetime.utcnow()`.

Patch (all three files):
```python
# Before
from datetime import datetime
run_timestamp = datetime.utcnow()

# After
from datetime import datetime, timezone
run_timestamp = datetime.now(timezone.utc)
```

Test: assert `metadata["timestamp"]` ends with `Z` or `+00:00`.

### P0-2 — `_parameter_family("baseline")` returns `"funding"` (labeling bug)

File: `src/balance_pricing_engine/diagnostics.py`, function `_parameter_family` (~line 594).

The "baseline" scenario name matches no keyword and falls to `return "funding"`. Every sensitivity
DataFrame has row 0 mislabeled. Filtering by `parameter_family == "funding"` silently includes
the baseline row.

Patch:
```python
def _parameter_family(scenario_name: str) -> str:
    name = scenario_name.lower()
    if name == "baseline":
        return "baseline"          # add this
    if "elasticity" in name or "coef" in name:
        return "elasticity"
    if "competitor" in name:
        return "competitor"
    if "wholesale" in name:
        return "wholesale"
    if "penalty" in name or "ratio" in name:
        return "funding"
    return "other"                 # change fallback
```

Test: `_parameter_family("baseline") == "baseline"`.

### P0-3 — `configs/base.yaml` `competitor_rates.enabled: true` vs docs saying `false`

`CLAUDE.md` states "competitor_rates.enabled: false by default" but `configs/base.yaml` has
`enabled: true`. The baseline runner never loads the competitor file (it uses `config.market`
directly), so nothing breaks today, but any new scenario runner that respects the flag will
unexpectedly pull a live-data dependency.

Fix: set `enabled: false` in `configs/base.yaml`, or add a comment explaining the flag is
respected only by `run_with_competitor_rates.py` and the CLI `apply` command.

### P0-4 — `usage_guide.md` internal inconsistency ($384M vs $394M deposits)

`docs/usage_guide.md` line ~368 says "The model expects about `$384M` of term deposits raised"
but actual output is $393.9M, and the same section's result box already correctly shows
`$393,936,646`. Prose and box contradict each other.

---

## P1 — High-value next improvements — ✅ all resolved (2026-06-13)

### P1-1 — `excess_deposit_income` missing from `SENSITIVITY_RESULT_COLUMNS`

File: `src/balance_pricing_engine/diagnostics.py`, `SENSITIVITY_RESULT_COLUMNS` and
`_run_sensitivity_analysis_for_seed`.

`delta_objective_pct` captures total objective change but cannot be decomposed when excess
deposit income is a significant contributor (e.g. in `deposit_elasticity_stronger`). The
column needs to be added to both the constant and the row-builder dict.

### P1-2 — No dedicated test for adaptive boundary expand-then-recheck

Still open from the 2026-04-26 diagnostic review. Need a test that:
(a) starts with a narrow grid so the optimum is at the boundary on the first pass,
(b) verifies `initial_boundary_warning_flag=True`,
(c) verifies `expansion_rounds >= 1`,
(d) verifies `boundary_warning_flag=False` after expansion.

File: `tests/unit/test_diagnostics.py`.

### P1-3 — Rate response sanity check validates rates far below optimizer range

`train_response_models.py` tests rates ~4.75–5.75% for loans, but the optimizer recommends
7.75–8.25%. A logistic model could be non-monotone at 8%+ while passing the check. The
`check_rate_response_direction` call should span the actual optimizer range.

### P1-4 — CLI `validate` doesn't check staleness or expose `--previous-file`

`validate_staleness` is only called from `load_and_resolve_competitor_rates` (in `apply`).
The CLI `validate` subcommand calls `validate_rate_run` only — no staleness, no movement check.
`--max-staleness-hours` and `--previous-file` flags should be added to the `validate` subparser.

### P1-5 — Multi-seed (`n_seeds > 1`) produces extra columns outside `SENSITIVITY_RESULT_COLUMNS`

When `n_seeds > 1`, the merged frame gets `{col}_mean` and `{col}_std_error` columns that are
not listed in `SENSITIVITY_RESULT_COLUMNS`. `write_artifacts` writes these to `sensitivity.csv`,
breaking the documented column contract for downstream consumers.

### P1-6 — `evaluate_binary_classifier` alias exists but CLAUDE.md says it is gone

`CLAUDE.md`: "evaluate_binary_classifier is gone". `modelling/evaluation.py:53`: alias is still
present as a forward to `evaluate_model`. The alias is harmless to keep; update CLAUDE.md.

### P1-7 — `apply_competitor_rates_to_config` overwrites `home_loan_reference_rate`

File: `src/balance_pricing_engine/competitor_rates/apply.py:24-31`.

After applying competitor rates, `home_loan_reference_rate` is set to the competitor mortgage
average, making `home_loan_spread_to_market_bps` equivalent to `spread_to_competitor_bps` (not
an independent benchmark). This is a design choice but is undocumented. The pricing memo should
flag when reference rate was overwritten by competitor data.

### P1-8 — Formalise deposit funding ratio as hard target vs soft risk appetite

`docs/model_governance.md` does not clearly state whether `required_deposit_funding_ratio` is a
hard strategic floor or a soft management signal. The current shortfall penalty is calibrated to
steer, not to enforce a hard constraint. Needs a one-paragraph governance note.

---

## P2 — Strategic extensions — ✅ all resolved (PR #36)

| # | Title | Effort |
|---|-------|--------|
| P2-1 | Partial dependence plots + monotonicity checks for ML diagnostic models | M |
| P2-2 | Champion/challenger comparison report in `train_response_models.py` | M |
| P2-3 | Auto-generate model card from `ModelRegistry` entries | M |
| P2-4 | Multi-period (2+ scenario roll) with tenor and runoff dynamics | L |
| P2-5 | Capital/RWA term and hurdle-rate constraint in the objective | L |
| P2-6 | FastAPI thin layer: POST /run-scenario → ScenarioDiagnostics JSON | L |
| P2-7 | Dashboard / profit-surface visualization (Streamlit or matplotlib) | M |
| P2-8 | Segmented / product-tier pricing (rate bands, not per-customer) | L |
| P2-9 | `validate` CLI: `--max-staleness-hours` and `--previous-file` flags | S |
| P2-10 | Run-ledger checklist sign-off from markdown convention to JSON schema | M |

---

## Recommended next PR (historical — completed)

> This recommendation was actioned; both fixes are in current source. Retained for context.

Fix P0-1 + P0-2 together:

- `run_ledger/writer.py`, `run_ledger/directory.py`, `run_ledger/metadata.py` — timezone-aware timestamps
- `diagnostics.py` `_parameter_family` — baseline label bug

Suggested PR title: `fix: timezone-aware run-ledger timestamps and baseline parameter-family label`

---

## What works well (do not break)

- All 238 tests pass; golden file regression locks baseline numerics.
- `OptimizationResult` backward-compat aliases (`objective_value`, `deposit_cost`, `funding_penalty`,
  `loan_income_after_losses`) are present and tested.
- `write_artifacts()` contract fields (`boundary_warning_flag`, `initial_boundary_warning_flag`,
  `expansion_rounds`, `expansion_capped`, `policy_band_breached`) are semantically correct.
- Dual elasticity structure (own-rate + competitor-gap for loan and deposit) is intact.
- Tie-break policy (objective → funding_shortfall → market spread distance) is deterministic and tested.
- `build_inclusive_numeric_grid` validates divisibility; boundary expansion always produces valid grids.
- Run-ledger collision-safe directory naming and SHA-256 config hash are correct.
- Self-consistency bias in ML diagnostics is documented; diagnostic layer is not wired to optimiser.
- Strict competitor-rate failures error correctly; non-strict path warns and continues.
