# Design: P2 ML Diagnostics & Dashboard Bundle

**Date:** 2026-05-20
**Branch:** to be created
**Items:** P2-1, P2-2, P2-3, P2-7
**Approach:** Option A — minimal additions to existing files

---

## Scope

Four independent P2 backlog items shipped together on one branch and one PR:

| # | Title | File(s) touched |
|---|-------|-----------------|
| P2-1 | Partial dependence + monotonicity checks | `modelling/evaluation.py` |
| P2-2 | Champion/challenger comparison report | `examples/train_response_models.py` |
| P2-3 | Auto-generate model card from `ModelRegistry` | `modelling/agentic.py` |
| P2-7 | Profit-surface static PNGs + Streamlit dashboard | `plotting.py`, `scripts/streamlit_dashboard.py` |

---

## P2-1: Partial Dependence + Monotonicity (evaluation.py)

### New functions

**`compute_partial_dependence(model, dataset, feature_columns, pdp_feature, grid_values) → pd.DataFrame`**

- For each value in `grid_values`, copy the full dataset, overwrite `pdp_feature` to that value, predict probabilities, take the mean across all rows.
- Returns a two-column DataFrame: `[pdp_feature, "mean_predicted_probability"]`, sorted by `pdp_feature`.
- This is a true marginal PDP — it averages over the full data distribution. Distinct from `check_rate_response_direction`, which builds explicit scenarios by co-varying `offered_rate`, `competitor_rate`, and `rate_gap_to_competitor` together.

**`check_monotonicity(pdp_frame, pdp_feature, expected_direction) → MonotonicityCheck`**

- Accepts a pre-computed PDP DataFrame (output of `compute_partial_dependence` or any similarly shaped frame).
- Checks whether `mean_predicted_probability` is monotonically increasing/decreasing across the sorted `pdp_feature` values.
- Returns a frozen dataclass `MonotonicityCheck(feature, expected_direction, is_monotone, violations)` — same shape as `RateResponseCheck`.
- `violations` counts the number of consecutive pairs that move in the wrong direction (using a tolerance of `1e-6`, matching `check_rate_response_direction`).

### New dataclass

```python
@dataclass(frozen=True)
class MonotonicityCheck:
    feature: str
    expected_direction: str   # "increasing" | "decreasing"
    is_monotone: bool
    violations: int
```

### Output

Tabular only — callers receive DataFrames and `MonotonicityCheck` objects. No matplotlib coupling in the evaluation layer.

### Tests

- PDP on a fitted logistic model: verify mean probability moves in expected direction for `offered_rate`.
- `check_monotonicity` pass case: known-monotone PDP frame.
- `check_monotonicity` fail case: injected non-monotone frame, correct violation count.
- Edge case: `grid_values` with a single value raises `ValueError`.

---

## P2-2: Champion/Challenger Report (train_response_models.py)

### Changes

`train_and_report` is extended to train both logistic regression (LR) and histogram gradient boosting (GBM) via the existing `build_hist_gradient_boosting_model` factory in `models.py`.

After both models are trained and evaluated, the function prints a side-by-side comparison table:

```
  Champion/Challenger comparison
    metric                  logistic_regression  hist_gradient_boosting
    auc                              0.8821                  0.8934
    log_loss                         0.4102                  0.3987
    brier_score                      0.1823                  0.1791
    calibration_mae (5 bins)         0.0412                  0.0389
  Recommendation: hist_gradient_boosting
    reason: challenger improved log_loss by 0.0115 and passed calibration threshold
```

### Recommendation logic

Mirrors `ModelTrainingAgent.train()`:

- Challenger recommended if: `log_loss_improvement >= 0.01` **and** `calibration_mae <= 0.10`.
- Otherwise LR is retained as default.

The `min_log_loss_improvement` threshold (0.01) and `max_calibration_error` (0.10) are module-level constants in the example script — not configurable via CLI for this item.

### Rate response sanity check

Both models run through `check_rate_response_direction` independently. Results printed separately under each model's section.

### No new dependencies

`build_hist_gradient_boosting_model` and all evaluation helpers already exist.

### Tests

- `train_and_report` refactored so the comparison logic is extractable: a helper `compare_models(lr_metrics, gbm_metrics, lr_cal, gbm_cal) → dict` returns `{"recommended": str, "reason": str, "improvement": float}`.
- Unit tests cover: challenger wins, LR retained, tie-break (both pass threshold, challenger wins on log_loss).

---

## P2-3: Auto-generate Model Card (agentic.py)

### New function

**`write_model_card(record: ModelRecord, output_dir: Path, feature_columns: list[str] | None = None, target_column: str | None = None) → Path`**

Standalone module-level function (not a method) so it can be called independently.

Renders a markdown file at `output_dir / f"{record.model_name}_{record.version}.md"` and returns the path.

### Card structure

```markdown
# Model Card: <model_name>

## Identity
- **Model type:** <model_type>
- **Version:** <version>
- **Trained at (UTC):** <trained_at_utc>
- **Config hash:** <config_hash>

## Features & Target
- **Features:** <comma-separated list, or "see config_hash" if not provided>
- **Target:** <target_column, or "see config_hash" if not provided>

## Performance Metrics
| Metric | Value |
|--------|-------|
| AUC | <value> |
| Log loss | <value> |
| Brier score | <value> |
| Calibration MAE | <value> |

## Governance
- **Governance status:** <governance_status>
- **Champion status:** <champion_status>
- **Artifact path:** <artifact_path or "not persisted">

## Approval
- **Human approval required:** true
```

### Wiring into `ModelTrainingAgent.train()`

After writing the JSON training report and updating the registry, `train()` calls:

```python
write_model_card(record, self.artifacts_dir / "model_cards", feature_columns, target_column)
```

for each `record` in `records`. The `model_cards` directory is created if absent.

### Feature columns availability

- When `self.config` is set: `feature_columns` and `target_column` passed from `self.config`.
- When `self.config is None` (bare `train()` call): the `feature_columns` and `target_column` kwargs already resolved earlier in `train()` are passed through. No silent omission.

### Tests

- Card written to a temp dir, file exists at expected path.
- Required sections (`## Identity`, `## Performance Metrics`, `## Governance`, `## Approval`) present in rendered markdown.
- `feature_columns=None` produces "see config_hash" in the Features line.
- Path returned matches the written file.

---

## P2-7: Profit-Surface Visualization (plotting.py + scripts/)

### Static PNGs — `plot_profit_surface(grid_results, result, output_path)`

New function added to `plotting.py`. Saves a 2×2 matplotlib figure to `output_path` as PNG (does **not** call `plt.show()`).

Panels:

| Panel | Content |
|-------|---------|
| [0,0] | Heatmap of `total_objective` across full loan × deposit grid (pivot on `loan_rate` × `deposit_rate`). Best-pair marked with a star. |
| [0,1] | Line: best objective by home loan rate (maximised over deposit rate). Winner loan rate marked. |
| [1,0] | Line: best objective by term deposit rate (maximised over loan rate). Winner deposit rate marked. |
| [1,1] | Scatter: top-10 candidates, x=loan_rate, y=deposit_rate, colour=objective, size∝funding_shortfall. Winner starred. |

Same pivot and formatting logic as `plot_grid_search_results` — no duplication of chart logic; the existing function retains its `plt.show()` notebook path. The new function is the file-output variant.

Signature:

```python
def plot_profit_surface(
    grid_results: pd.DataFrame,
    result: Any,
    output_path: str | Path,
) -> None:
```

### Interactive Streamlit dashboard — `scripts/streamlit_dashboard.py`

Standalone script launched with `streamlit run scripts/streamlit_dashboard.py`.

**Layout:**

- Sidebar: run directory picker (lists subdirs of `data/outputs/runs/`), minimum objective slider.
- Main: 2×2 profit surface (calls `plot_profit_surface` into a `BytesIO` buffer, displayed via `st.image`), metrics summary card from `summary.json`, filtered top-N table from `grid_results.csv`.

**Data loading:** reads `grid_results.csv` and `summary.json` from the selected run directory using `ScenarioDiagnostics`-compatible column names. Fails gracefully with `st.error` if files are absent.

### Dependency

`streamlit` added as optional extra in `pyproject.toml`:

```toml
[project.optional-dependencies]
dashboard = ["streamlit>=1.30"]
```

Install with `pip install -e ".[dashboard]"`. Not required for tests or core engine.

### Tests

- `plot_profit_surface`: given a minimal synthetic `grid_results` DataFrame, file is written to a temp path and is non-empty.
- Streamlit script: not unit-tested (UI layer). Validated manually.

---

## Error handling

- `compute_partial_dependence`: `ValueError` if `grid_values` has fewer than 2 entries or `pdp_feature` is not in `feature_columns`.
- `check_monotonicity`: `ValueError` if `expected_direction` not in `{"increasing", "decreasing"}` or frame has fewer than 2 rows.
- `write_model_card`: `output_dir` created if absent; no error if overwriting an existing card.
- `plot_profit_surface`: returns without writing if `grid_results` is empty (consistent with `get_plotting_tools()` pattern).

---

## Testing strategy

- All new pure-logic functions covered by unit tests in `tests/unit/`.
- No new test fixtures required — synthetic DataFrames built inline in tests.
- `streamlit_dashboard.py` excluded from unit test scope.
- Target: existing 244 tests pass + ~15 new tests added.

---

## Invariants preserved

- Modelling layer remains diagnostic only — no ML predictions wired into the optimiser.
- `ScenarioDiagnostics.write_artifacts()` artifact contract unchanged.
- `OptimizationResult` dataclass unchanged.
- Synthetic-data-only invariant unchanged.
