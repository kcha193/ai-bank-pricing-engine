# P2 ML Diagnostics & Dashboard Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement four P2 backlog items — partial dependence + monotonicity checks, champion/challenger comparison, auto-generated model cards, and profit-surface visualisation — on a new branch, then open a PR.

**Architecture:** All additions follow Option A: new functions added to existing files (`evaluation.py`, `agentic.py`, `plotting.py`) plus a new `scripts/streamlit_dashboard.py`. No new source modules created. `compare_models` lives in `evaluation.py` so it is importable from tests.

**Tech Stack:** Python 3.9+, scikit-learn, pandas, numpy, matplotlib (already in `dev` extra), streamlit (new `dashboard` optional extra).

---

## File Map

| Action | Path | Responsibility |
|--------|------|---------------|
| Modify | `src/balance_pricing_engine/modelling/evaluation.py` | Add `MonotonicityCheck`, `compute_partial_dependence`, `check_monotonicity`, `compare_models` |
| Modify | `examples/train_response_models.py` | Train LR + GBM side-by-side, print comparison, use `compare_models` from evaluation |
| Modify | `src/balance_pricing_engine/modelling/agentic.py` | Add `write_model_card`, call it from `ModelTrainingAgent.train()` |
| Modify | `src/balance_pricing_engine/plotting.py` | Add `plot_profit_surface` (file-output variant) |
| Create | `scripts/streamlit_dashboard.py` | Interactive Streamlit profit-surface dashboard |
| Modify | `pyproject.toml` | Add `[dashboard]` optional extra with `streamlit>=1.30` |
| Modify | `tests/unit/test_modelling_evaluation.py` | Tests for PDP, monotonicity, compare_models |
| Modify | `tests/unit/test_modelling_agentic.py` | Test write_model_card + train() auto-generation |
| Modify | `tests/unit/test_plotting.py` | Test plot_profit_surface writes file |

---

## Task 1: Create feature branch

- [ ] **Step 1: Create and switch to feature branch**

```bash
git checkout -b feat/p2-ml-diagnostics-and-dashboard
```

Expected: `Switched to a new branch 'feat/p2-ml-diagnostics-and-dashboard'`

---

## Task 2: P2-1 — `MonotonicityCheck` dataclass + `compute_partial_dependence`

**Files:**
- Modify: `src/balance_pricing_engine/modelling/evaluation.py`
- Test: `tests/unit/test_modelling_evaluation.py`

- [ ] **Step 1: Write failing tests for `compute_partial_dependence`**

Add to `tests/unit/test_modelling_evaluation.py`:

```python
import pytest
from balance_pricing_engine.modelling.evaluation import (
    compute_partial_dependence,
)


class OfferedRateDecreasingModel:
    """Toy model: acceptance = 0.9 - 5 * offered_rate (clipped)."""

    def predict_proba(self, features):
        prob = 0.9 - 5.0 * features["offered_rate"].to_numpy()
        prob = np.clip(prob, 0.01, 0.99)
        return np.column_stack([1 - prob, prob])


def _minimal_dataset(n: int = 100) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    return pd.DataFrame(
        {
            "offered_rate": rng.uniform(0.07, 0.09, n),
            "feat_b": rng.standard_normal(n),
        }
    )


def test_compute_partial_dependence_returns_sorted_rows():
    dataset = _minimal_dataset()
    model = OfferedRateDecreasingModel()
    feature_cols = ["offered_rate", "feat_b"]
    grid = [0.085, 0.075, 0.080]  # intentionally unsorted

    result = compute_partial_dependence(model, dataset, feature_cols, "offered_rate", grid)

    assert list(result.columns) == ["offered_rate", "mean_predicted_probability"]
    assert list(result["offered_rate"]) == sorted(grid)
    assert len(result) == 3


def test_compute_partial_dependence_mean_decreases_for_decreasing_model():
    dataset = _minimal_dataset()
    model = OfferedRateDecreasingModel()
    feature_cols = ["offered_rate", "feat_b"]
    grid = [0.075, 0.080, 0.085]

    result = compute_partial_dependence(model, dataset, feature_cols, "offered_rate", grid)

    probs = result["mean_predicted_probability"].to_numpy()
    assert probs[0] > probs[1] > probs[2]


def test_compute_partial_dependence_raises_on_single_grid_value():
    dataset = _minimal_dataset()
    model = OfferedRateDecreasingModel()
    with pytest.raises(ValueError, match="at least 2"):
        compute_partial_dependence(model, dataset, ["offered_rate", "feat_b"], "offered_rate", [0.08])


def test_compute_partial_dependence_raises_when_feature_not_in_feature_columns():
    dataset = _minimal_dataset()
    model = OfferedRateDecreasingModel()
    with pytest.raises(ValueError, match="not in feature_columns"):
        compute_partial_dependence(model, dataset, ["feat_b"], "offered_rate", [0.07, 0.08])
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_evaluation.py -k "partial_dependence" -v
```

Expected: `FAILED` — `ImportError` or `AttributeError` for `compute_partial_dependence`.

- [ ] **Step 3: Add `MonotonicityCheck` dataclass and `compute_partial_dependence` to `evaluation.py`**

Add the following to `src/balance_pricing_engine/modelling/evaluation.py` after the `RateResponseCheck` dataclass and before `evaluate_probabilities`:

```python
@dataclass(frozen=True)
class MonotonicityCheck:
    feature: str
    expected_direction: str
    is_monotone: bool
    violations: int


def compute_partial_dependence(
    model: Any,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    pdp_feature: str,
    grid_values: list[float],
) -> pd.DataFrame:
    """Compute marginal partial dependence of one feature on predicted probability."""
    if len(grid_values) < 2:
        raise ValueError("grid_values must have at least 2 entries.")
    if pdp_feature not in feature_columns:
        raise ValueError(f"pdp_feature '{pdp_feature}' is not in feature_columns.")
    rows = []
    for value in sorted(grid_values):
        scenario = dataset.copy()
        scenario[pdp_feature] = value
        probability = predict_response_probability(model, scenario, feature_columns)
        rows.append({pdp_feature: value, "mean_predicted_probability": float(np.mean(probability))})
    return pd.DataFrame(rows)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_modelling_evaluation.py -k "partial_dependence" -v
```

Expected: 4 tests `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/evaluation.py tests/unit/test_modelling_evaluation.py
git commit -m "feat: add MonotonicityCheck dataclass and compute_partial_dependence to evaluation"
```

---

## Task 3: P2-1 — `check_monotonicity`

**Files:**
- Modify: `src/balance_pricing_engine/modelling/evaluation.py`
- Test: `tests/unit/test_modelling_evaluation.py`

- [ ] **Step 1: Write failing tests for `check_monotonicity`**

Add to `tests/unit/test_modelling_evaluation.py`:

```python
from balance_pricing_engine.modelling.evaluation import (
    MonotonicityCheck,
    check_monotonicity,
)


def test_check_monotonicity_pass_decreasing():
    pdp = pd.DataFrame(
        {
            "offered_rate": [0.07, 0.08, 0.09],
            "mean_predicted_probability": [0.80, 0.60, 0.40],
        }
    )
    result = check_monotonicity(pdp, "offered_rate", "decreasing")
    assert isinstance(result, MonotonicityCheck)
    assert result.is_monotone is True
    assert result.violations == 0
    assert result.feature == "offered_rate"


def test_check_monotonicity_fail_decreasing_one_violation():
    pdp = pd.DataFrame(
        {
            "offered_rate": [0.07, 0.08, 0.09],
            "mean_predicted_probability": [0.80, 0.90, 0.40],  # 0.80→0.90 violates decreasing
        }
    )
    result = check_monotonicity(pdp, "offered_rate", "decreasing")
    assert result.is_monotone is False
    assert result.violations == 1


def test_check_monotonicity_pass_increasing():
    pdp = pd.DataFrame(
        {
            "deposit_rate": [0.04, 0.05, 0.06],
            "mean_predicted_probability": [0.30, 0.50, 0.70],
        }
    )
    result = check_monotonicity(pdp, "deposit_rate", "increasing")
    assert result.is_monotone is True
    assert result.violations == 0


def test_check_monotonicity_raises_on_bad_direction():
    pdp = pd.DataFrame(
        {"x": [1, 2], "mean_predicted_probability": [0.4, 0.6]}
    )
    with pytest.raises(ValueError, match="expected_direction"):
        check_monotonicity(pdp, "x", "sideways")


def test_check_monotonicity_raises_on_too_few_rows():
    pdp = pd.DataFrame({"x": [1], "mean_predicted_probability": [0.5]})
    with pytest.raises(ValueError, match="at least 2 rows"):
        check_monotonicity(pdp, "x", "increasing")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_evaluation.py -k "monotonicity" -v
```

Expected: `FAILED` — `ImportError` for `check_monotonicity`.

- [ ] **Step 3: Add `check_monotonicity` to `evaluation.py`**

Add after `compute_partial_dependence`:

```python
def check_monotonicity(
    pdp_frame: pd.DataFrame,
    pdp_feature: str,
    expected_direction: str,
) -> MonotonicityCheck:
    """Check whether a partial dependence curve is monotone in the expected direction."""
    if expected_direction not in {"increasing", "decreasing"}:
        raise ValueError("expected_direction must be 'increasing' or 'decreasing'.")
    if len(pdp_frame) < 2:
        raise ValueError("pdp_frame must have at least 2 rows.")
    probs = pdp_frame["mean_predicted_probability"].to_numpy()
    deltas = np.diff(probs)
    tolerance = 1e-6
    if expected_direction == "decreasing":
        violations = int(np.sum(deltas > tolerance))
    else:
        violations = int(np.sum(deltas < -tolerance))
    return MonotonicityCheck(
        feature=pdp_feature,
        expected_direction=expected_direction,
        is_monotone=violations == 0,
        violations=violations,
    )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_modelling_evaluation.py -k "monotonicity" -v
```

Expected: 5 tests `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/evaluation.py tests/unit/test_modelling_evaluation.py
git commit -m "feat: add check_monotonicity to evaluation — P2-1 complete"
```

---

## Task 4: P2-2 — `compare_models` helper

**Files:**
- Modify: `src/balance_pricing_engine/modelling/evaluation.py`
- Test: `tests/unit/test_modelling_evaluation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_modelling_evaluation.py`:

```python
from balance_pricing_engine.modelling.evaluation import compare_models


def test_compare_models_recommends_challenger_when_it_improves():
    lr_metrics = {"auc": 0.85, "log_loss": 0.42, "brier_score": 0.18}
    gbm_metrics = {"auc": 0.88, "log_loss": 0.40, "brier_score": 0.17}
    result = compare_models(
        "logistic_regression", lr_metrics, 0.05,
        "hist_gradient_boosting", gbm_metrics, 0.04,
    )
    assert result["recommended"] == "hist_gradient_boosting"
    assert result["improvement"] == pytest.approx(0.02, abs=1e-6)


def test_compare_models_retains_baseline_when_improvement_below_threshold():
    lr_metrics = {"auc": 0.85, "log_loss": 0.42, "brier_score": 0.18}
    gbm_metrics = {"auc": 0.86, "log_loss": 0.415, "brier_score": 0.175}  # only 0.005 improvement
    result = compare_models(
        "logistic_regression", lr_metrics, 0.05,
        "hist_gradient_boosting", gbm_metrics, 0.04,
    )
    assert result["recommended"] == "logistic_regression"


def test_compare_models_retains_baseline_when_challenger_fails_calibration():
    lr_metrics = {"auc": 0.85, "log_loss": 0.42, "brier_score": 0.18}
    gbm_metrics = {"auc": 0.88, "log_loss": 0.40, "brier_score": 0.17}
    result = compare_models(
        "logistic_regression", lr_metrics, 0.05,
        "hist_gradient_boosting", gbm_metrics, 0.15,  # cal_mae > 0.10
    )
    assert result["recommended"] == "logistic_regression"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_evaluation.py -k "compare_models" -v
```

Expected: `FAILED` — `ImportError` for `compare_models`.

- [ ] **Step 3: Add `compare_models` to `evaluation.py`**

Add after `check_monotonicity`:

```python
def compare_models(
    model_name_a: str,
    metrics_a: dict[str, float],
    cal_mae_a: float,
    model_name_b: str,
    metrics_b: dict[str, float],
    cal_mae_b: float,
    *,
    min_log_loss_improvement: float = 0.01,
    max_calibration_error: float = 0.10,
) -> dict[str, Any]:
    """Compare two models by log-loss improvement and calibration; return recommendation dict."""
    improvement = metrics_a["log_loss"] - metrics_b["log_loss"]
    b_qualifies = improvement >= min_log_loss_improvement and cal_mae_b <= max_calibration_error
    if b_qualifies:
        recommended = model_name_b
        reason = (
            f"challenger improved log_loss by {improvement:.4f} "
            "and passed calibration threshold"
        )
    else:
        recommended = model_name_a
        reason = "baseline retained (challenger did not meet improvement or calibration threshold)"
    return {"recommended": recommended, "reason": reason, "improvement": improvement}
```

Also add `Any` to the imports at the top of `evaluation.py` if not already present:

```python
from typing import Any
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_modelling_evaluation.py -k "compare_models" -v
```

Expected: 3 tests `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/evaluation.py tests/unit/test_modelling_evaluation.py
git commit -m "feat: add compare_models to evaluation for champion/challenger comparison"
```

---

## Task 5: P2-2 — Extend `train_response_models.py` for champion/challenger

**Files:**
- Modify: `examples/train_response_models.py`

- [ ] **Step 1: Update imports in `train_response_models.py`**

Add to the imports block:

```python
from balance_pricing_engine.modelling.evaluation import (
    RateResponseCheck,
    check_rate_response_direction,
    compare_models,
    evaluate_binary_classifier,
    evaluate_calibration,
)
from balance_pricing_engine.modelling.models import (
    build_hist_gradient_boosting_model,
    build_logistic_regression_model,
    fit_response_model,
)
```

(Replace the existing individual imports for these symbols.)

- [ ] **Step 2: Replace `train_and_report` with the champion/challenger version**

Replace the entire `train_and_report` function with:

```python
def train_and_report(
    title: str,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
    offered_rates: list[float],
    competitor_rate: float,
    expected_direction: str,
    random_seed: int,
) -> None:
    train, test = train_test_split(
        dataset,
        test_size=0.30,
        random_state=random_seed,
        stratify=dataset[target_column],
    )
    lr_model = fit_response_model(
        build_logistic_regression_model(random_seed), train, feature_columns, target_column
    )
    gbm_model = fit_response_model(
        build_hist_gradient_boosting_model(random_seed), train, feature_columns, target_column
    )

    lr_metrics = evaluate_binary_classifier(lr_model, test, feature_columns, target_column)
    gbm_metrics = evaluate_binary_classifier(gbm_model, test, feature_columns, target_column)

    lr_cal = evaluate_calibration(lr_model, test, feature_columns, target_column, n_bins=5)
    gbm_cal = evaluate_calibration(gbm_model, test, feature_columns, target_column, n_bins=5)
    lr_cal_mae = float((lr_cal["mean_predicted_probability"] - lr_cal["observed_rate"]).abs().mean())
    gbm_cal_mae = float(
        (gbm_cal["mean_predicted_probability"] - gbm_cal["observed_rate"]).abs().mean()
    )

    comparison = compare_models(
        "logistic_regression", lr_metrics, lr_cal_mae,
        "hist_gradient_boosting", gbm_metrics, gbm_cal_mae,
    )

    lr_check = check_rate_response_direction(
        lr_model, test.head(1_000), feature_columns, offered_rates, competitor_rate,
        expected_direction,
    )
    gbm_check = check_rate_response_direction(
        gbm_model, test.head(1_000), feature_columns, offered_rates, competitor_rate,
        expected_direction,
    )

    print(title)
    _print_comparison_table(lr_metrics, gbm_metrics, lr_cal_mae, gbm_cal_mae)
    print(f"  Recommendation: {comparison['recommended']}")
    print(f"    reason: {comparison['reason']}")
    print("  Logistic regression rate response")
    print_rate_response_check(lr_check)
    print("  Hist gradient boosting rate response")
    print_rate_response_check(gbm_check)
```

- [ ] **Step 3: Add `_print_comparison_table` helper**

Add after `train_and_report`:

```python
def _print_comparison_table(
    lr_metrics: dict,
    gbm_metrics: dict,
    lr_cal_mae: float,
    gbm_cal_mae: float,
) -> None:
    header = f"  {'metric':<22}  {'logistic_regression':>22}  {'hist_gradient_boosting':>22}"
    print(header)
    for key in ["auc", "log_loss", "brier_score"]:
        print(f"  {key:<22}  {lr_metrics[key]:>22.4f}  {gbm_metrics[key]:>22.4f}")
    print(f"  {'calibration_mae':<22}  {lr_cal_mae:>22.4f}  {gbm_cal_mae:>22.4f}")
```

- [ ] **Step 4: Run the example to verify it executes cleanly**

```bash
python3 examples/train_response_models.py
```

Expected: prints comparison table with both models, calibration, recommendation line, and rate response for both — no errors.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
python3 -m pytest -q --tb=short
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add examples/train_response_models.py
git commit -m "feat: add champion/challenger comparison to train_response_models — P2-2 complete"
```

---

## Task 6: P2-3 — `write_model_card` function

**Files:**
- Modify: `src/balance_pricing_engine/modelling/agentic.py`
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_modelling_agentic.py`:

```python
from pathlib import Path
from balance_pricing_engine.modelling.agentic import ModelRecord, write_model_card


def _sample_record() -> ModelRecord:
    return ModelRecord(
        model_name="LogisticResponseModel",
        model_type="LogisticResponseModel",
        version="20260520T120000",
        trained_at_utc="2026-05-20T12:00:00+00:00",
        artifact_path="/tmp/model.joblib",
        metrics={"auc": 0.87, "log_loss": 0.41, "brier_score": 0.17},
        calibration_mae=0.04,
        governance_status="pending_human_approval",
        champion_status="challenger",
        config_hash="abc123",
    )


def test_write_model_card_creates_file(tmp_path):
    record = _sample_record()
    path = write_model_card(record, tmp_path)
    assert path.exists()
    assert path.stat().st_size > 0
    assert path.name == "LogisticResponseModel_20260520T120000.md"


def test_write_model_card_contains_required_sections(tmp_path):
    record = _sample_record()
    path = write_model_card(record, tmp_path)
    content = path.read_text(encoding="utf-8")
    for section in ["## Identity", "## Performance Metrics", "## Governance", "## Approval"]:
        assert section in content, f"Missing section: {section}"


def test_write_model_card_includes_metrics(tmp_path):
    record = _sample_record()
    path = write_model_card(record, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "0.8700" in content  # auc
    assert "0.4100" in content  # log_loss
    assert "0.0400" in content  # calibration_mae


def test_write_model_card_with_feature_columns(tmp_path):
    record = _sample_record()
    path = write_model_card(
        record, tmp_path,
        feature_columns=["offered_rate", "credit_score"],
        target_column="accepted",
    )
    content = path.read_text(encoding="utf-8")
    assert "offered_rate, credit_score" in content
    assert "accepted" in content


def test_write_model_card_without_feature_columns_shows_fallback(tmp_path):
    record = _sample_record()
    path = write_model_card(record, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "see config_hash" in content


def test_write_model_card_creates_output_dir(tmp_path):
    record = _sample_record()
    nested = tmp_path / "a" / "b" / "model_cards"
    write_model_card(record, nested)
    assert nested.exists()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -k "write_model_card" -v
```

Expected: `FAILED` — `ImportError` for `write_model_card`.

- [ ] **Step 3: Add `write_model_card` to `agentic.py`**

Add the following module-level function near the bottom of `agentic.py`, before `ModelTrainingAgent`:

```python
def write_model_card(
    record: ModelRecord,
    output_dir: Path,
    feature_columns: list[str] | None = None,
    target_column: str | None = None,
) -> Path:
    """Write a markdown model card for a trained model record."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    card_path = output_dir / f"{record.model_name}_{record.version}.md"

    features_line = ", ".join(feature_columns) if feature_columns else "see config_hash"
    target_line = target_column if target_column else "see config_hash"
    artifact_line = record.artifact_path if record.artifact_path else "not persisted"
    metrics = record.metrics
    auc = metrics.get("auc", float("nan"))
    log_loss_val = metrics.get("log_loss", float("nan"))
    brier = metrics.get("brier_score", float("nan"))

    content = (
        f"# Model Card: {record.model_name}\n\n"
        f"## Identity\n"
        f"- **Model type:** {record.model_type}\n"
        f"- **Version:** {record.version}\n"
        f"- **Trained at (UTC):** {record.trained_at_utc}\n"
        f"- **Config hash:** {record.config_hash}\n\n"
        f"## Features & Target\n"
        f"- **Features:** {features_line}\n"
        f"- **Target:** {target_line}\n\n"
        f"## Performance Metrics\n"
        f"| Metric | Value |\n"
        f"|--------|-------|\n"
        f"| AUC | {auc:.4f} |\n"
        f"| Log loss | {log_loss_val:.4f} |\n"
        f"| Brier score | {brier:.4f} |\n"
        f"| Calibration MAE | {record.calibration_mae:.4f} |\n\n"
        f"## Governance\n"
        f"- **Governance status:** {record.governance_status}\n"
        f"- **Champion status:** {record.champion_status}\n"
        f"- **Artifact path:** {artifact_line}\n\n"
        f"## Approval\n"
        f"- **Human approval required:** true\n"
    )
    card_path.write_text(content, encoding="utf-8")
    return card_path
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -k "write_model_card" -v
```

Expected: 6 tests `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: add write_model_card to agentic module"
```

---

## Task 7: P2-3 — Wire `write_model_card` into `ModelTrainingAgent.train()`

**Files:**
- Modify: `src/balance_pricing_engine/modelling/agentic.py`
- Test: `tests/unit/test_modelling_agentic.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_modelling_agentic.py`:

```python
def test_training_agent_writes_model_cards(tmp_path):
    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    registry = ModelRegistry(tmp_path / "registry.json")
    agent.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=7),
        },
        registry=registry,
        run_name="card_test",
    )
    card_dir = tmp_path / "model_cards"
    assert card_dir.exists()
    cards = list(card_dir.glob("*.md"))
    assert len(cards) == 2  # one per candidate
    content = cards[0].read_text(encoding="utf-8")
    assert "## Identity" in content
    assert "## Approval" in content
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -k "writes_model_cards" -v
```

Expected: `FAILED` — card directory does not exist.

- [ ] **Step 3: Wire `write_model_card` into `ModelTrainingAgent.train()`**

In `ModelTrainingAgent.train()`, find the line:

```python
        records.append(record)
```

Add the call immediately after it:

```python
        records.append(record)
        write_model_card(
            record,
            self.artifacts_dir / "model_cards",
            feature_columns if self.config is None else self.config.feature_columns,
            target_column if self.config is None else self.config.target_column,
        )
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
python3 -m pytest tests/unit/test_modelling_agentic.py -k "writes_model_cards" -v
```

Expected: `PASSED`.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
python3 -m pytest -q --tb=short
```

Expected: all existing tests pass plus the new test.

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/modelling/agentic.py tests/unit/test_modelling_agentic.py
git commit -m "feat: auto-generate model cards from ModelTrainingAgent.train() — P2-3 complete"
```

---

## Task 8: P2-7 — `plot_profit_surface` (static PNG)

**Files:**
- Modify: `src/balance_pricing_engine/plotting.py`
- Test: `tests/unit/test_plotting.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_plotting.py`. First extend `FakePyplot` with `savefig` and `close` methods by adding them inside the existing class definition:

```python
    def savefig(self, path, **kwargs) -> None:
        self.savefig_calls = getattr(self, "savefig_calls", [])
        self.savefig_calls.append(path)

    def close(self, fig=None) -> None:
        self.close_calls = getattr(self, "close_calls", [])
        self.close_calls.append(fig)
```

Then add the test:

```python
from types import SimpleNamespace


def test_plot_profit_surface_calls_savefig_and_close(monkeypatch, tmp_path):
    fake_plt = install_fake_plotting(monkeypatch)

    grid_rows = []
    for lr in [0.080, 0.085]:
        for dr in [0.050, 0.055]:
            grid_rows.append(
                {
                    "loan_rate": lr,
                    "deposit_rate": dr,
                    "total_objective": 2_000_000.0,
                    "funding_shortfall": 100_000.0,
                }
            )
    grid_results = pd.DataFrame(grid_rows)
    result = SimpleNamespace(loan_rate=0.080, deposit_rate=0.050)
    output_path = tmp_path / "surface.png"

    plotting.plot_profit_surface(grid_results, result, output_path)

    assert hasattr(fake_plt, "savefig_calls")
    assert len(fake_plt.savefig_calls) == 1
    assert str(fake_plt.savefig_calls[0]) == str(output_path)
    assert hasattr(fake_plt, "close_calls")
    assert len(fake_plt.close_calls) == 1


def test_plot_profit_surface_no_op_on_empty_grid(monkeypatch, tmp_path):
    fake_plt = install_fake_plotting(monkeypatch)
    plotting.plot_profit_surface(pd.DataFrame(), SimpleNamespace(loan_rate=0.08, deposit_rate=0.05), tmp_path / "out.png")
    assert not hasattr(fake_plt, "savefig_calls")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_plotting.py -k "profit_surface" -v
```

Expected: `FAILED` — `AttributeError` for `plot_profit_surface`.

- [ ] **Step 3: Add `plot_profit_surface` to `plotting.py`**

Add after `plot_grid_search_results`:

```python
def plot_profit_surface(
    grid_results: pd.DataFrame,
    result: Any,
    output_path: Any,
) -> None:
    """Save a 2×2 profit-surface figure to output_path as PNG."""
    if len(grid_results) == 0:
        return
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, func_formatter, percent_formatter = plotting

    pivot = (
        grid_results.pivot_table(
            index="deposit_rate",
            columns="loan_rate",
            values="total_objective",
            aggfunc="max",
        )
        .sort_index()
        .sort_index(axis=1)
    )
    loan_rates = pivot.columns.to_numpy(dtype=float)
    deposit_rates = pivot.index.to_numpy(dtype=float)
    best_loan_idx = int(np.abs(loan_rates - result.loan_rate).argmin())
    best_deposit_idx = int(np.abs(deposit_rates - result.deposit_rate).argmin())

    best_by_loan = (
        grid_results.groupby("loan_rate", as_index=False)["total_objective"]
        .max()
        .sort_values("loan_rate")
    )
    best_by_deposit = (
        grid_results.groupby("deposit_rate", as_index=False)["total_objective"]
        .max()
        .sort_values("deposit_rate")
    )
    top_10 = grid_results.sort_values("total_objective", ascending=False).head(10).copy()

    fig, axes = plt.subplots(2, 2, figsize=(15, 10))

    heatmap = axes[0, 0].imshow(
        pivot.to_numpy() / 1_000_000,
        aspect="auto",
        origin="lower",
        cmap="viridis",
    )
    axes[0, 0].scatter(
        best_loan_idx, best_deposit_idx,
        marker="*", s=220, color=CHART_COLORS["highlight"], edgecolor="black",
    )
    axes[0, 0].set_title("Objective surface by rate pair")
    axes[0, 0].set_xlabel("Home loan rate")
    axes[0, 0].set_ylabel("Term deposit rate")
    axes[0, 0].set_xticks(range(len(loan_rates)))
    axes[0, 0].set_xticklabels(
        [fmt_rate(r) for r in loan_rates], rotation=45, ha="right"
    )
    axes[0, 0].set_yticks(range(len(deposit_rates)))
    axes[0, 0].set_yticklabels([fmt_rate(r) for r in deposit_rates])
    fig.colorbar(heatmap, ax=axes[0, 0], label="Objective ($M)")

    axes[0, 1].plot(
        best_by_loan["loan_rate"], best_by_loan["total_objective"],
        marker="o", color=CHART_COLORS["loan"],
    )
    axes[0, 1].axvline(result.loan_rate, color=CHART_COLORS["highlight"], linestyle="--", label="winner")
    axes[0, 1].set_title("Best objective by home loan rate")
    axes[0, 1].set_xlabel("Home loan rate")
    axes[0, 1].set_ylabel("Objective")
    axes[0, 1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[0, 1].yaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[0, 1].legend()

    axes[1, 0].plot(
        best_by_deposit["deposit_rate"], best_by_deposit["total_objective"],
        marker="o", color=CHART_COLORS["deposit"],
    )
    axes[1, 0].axvline(result.deposit_rate, color=CHART_COLORS["highlight"], linestyle="--", label="winner")
    axes[1, 0].set_title("Best objective by term deposit rate")
    axes[1, 0].set_xlabel("Term deposit rate")
    axes[1, 0].set_ylabel("Objective")
    axes[1, 0].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 0].yaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[1, 0].legend()

    shortfall = top_10["funding_shortfall"].clip(lower=0)
    sizes = (
        90 + 320 * shortfall / shortfall.max()
        if shortfall.max() > 0
        else np.full(len(top_10), 120.0)
    )
    scatter = axes[1, 1].scatter(
        top_10["loan_rate"],
        top_10["deposit_rate"],
        c=top_10["total_objective"] / 1_000_000,
        s=sizes,
        cmap="magma",
        edgecolor="black",
        linewidth=0.6,
        alpha=0.85,
    )
    axes[1, 1].scatter(
        result.loan_rate, result.deposit_rate,
        marker="*", s=240, color=CHART_COLORS["highlight"], edgecolor="black", label="winner",
    )
    axes[1, 1].set_title("Top 10 pricing candidates")
    axes[1, 1].set_xlabel("Home loan rate")
    axes[1, 1].set_ylabel("Term deposit rate")
    axes[1, 1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 1].yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 1].legend()
    fig.colorbar(scatter, ax=axes[1, 1], label="Objective ($M)")

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_plotting.py -k "profit_surface" -v
```

Expected: 2 tests `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/plotting.py tests/unit/test_plotting.py
git commit -m "feat: add plot_profit_surface to plotting — saves 2x2 PNG"
```

---

## Task 9: P2-7 — Streamlit dashboard + `pyproject.toml` update

**Files:**
- Create: `scripts/streamlit_dashboard.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `dashboard` optional extra to `pyproject.toml`**

In `pyproject.toml`, add after the `dev` optional dependency block:

```toml
dashboard = ["streamlit>=1.30"]
```

So the full `[project.optional-dependencies]` section becomes:

```toml
[project.optional-dependencies]
dev = [
    "ipykernel>=6.29",
    "notebook>=7.0",
    "matplotlib>=3.8",
    "hypothesis>=6.100",
    "pytest>=8.0",
    "pytest-cov>=6.0",
    "ruff>=0.8",
]
dashboard = ["streamlit>=1.30"]
```

- [ ] **Step 2: Create `scripts/streamlit_dashboard.py`**

```python
"""Interactive profit-surface dashboard.

Launch with:
    streamlit run scripts/streamlit_dashboard.py

Requires: pip install -e ".[dashboard]"
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import streamlit as st

from balance_pricing_engine.plotting import plot_profit_surface


_RUNS_DIR = Path("data/outputs/runs")


def _list_runs() -> list[str]:
    if not _RUNS_DIR.exists():
        return []
    return sorted([d.name for d in _RUNS_DIR.iterdir() if d.is_dir()], reverse=True)


def _load_run(run_name: str) -> tuple[pd.DataFrame, dict]:
    run_dir = _RUNS_DIR / run_name
    grid_path = run_dir / "grid_results.csv"
    summary_path = run_dir / "summary.json"
    if not grid_path.exists() or not summary_path.exists():
        st.error(
            f"Run '{run_name}' is missing grid_results.csv or summary.json. "
            "Run examples/run_baseline.py to generate a run."
        )
        st.stop()
    grid_results = pd.read_csv(grid_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return grid_results, summary


def main() -> None:
    st.set_page_config(page_title="Profit Surface Dashboard", layout="wide")
    st.title("Pricing Optimisation — Profit Surface Dashboard")

    runs = _list_runs()
    if not runs:
        st.warning(
            f"No run directories found under `{_RUNS_DIR}`. "
            "Run `python3 examples/run_baseline.py` first."
        )
        return

    with st.sidebar:
        st.header("Run")
        selected_run = st.selectbox("Select run", runs)
        max_obj_millions = 20.0
        min_objective = st.slider(
            "Min objective ($M)", min_value=0.0, max_value=max_obj_millions, value=0.0, step=0.1
        )

    grid_results, summary = _load_run(selected_run)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Best loan rate", f"{summary['best_loan_rate']:.2%}")
    col2.metric("Best deposit rate", f"{summary['best_deposit_rate']:.2%}")
    col3.metric("Objective", f"${summary['objective_value'] / 1_000_000:.2f}M")
    col4.metric("Funding shortfall", f"${summary['funding_shortfall'] / 1_000_000:.2f}M")

    st.subheader("Profit surface")
    result_proxy = SimpleNamespace(
        loan_rate=summary["best_loan_rate"],
        deposit_rate=summary["best_deposit_rate"],
    )
    buf = io.BytesIO()
    plot_profit_surface(grid_results, result_proxy, buf)
    buf.seek(0)
    st.image(buf, use_container_width=True)

    threshold = min_objective * 1_000_000
    filtered = (
        grid_results[grid_results["total_objective"] >= threshold]
        .sort_values("total_objective", ascending=False)
        .head(20)
        .reset_index(drop=True)
    )
    st.subheader(f"Top candidates (objective ≥ ${min_objective:.1f}M)")
    st.dataframe(
        filtered[["loan_rate", "deposit_rate", "total_objective", "funding_shortfall"]]
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify the script is importable (syntax check)**

```bash
python3 -c "import ast; ast.parse(open('scripts/streamlit_dashboard.py').read()); print('syntax ok')"
```

Expected: `syntax ok`

- [ ] **Step 4: Commit**

```bash
git add scripts/streamlit_dashboard.py pyproject.toml
git commit -m "feat: add Streamlit profit-surface dashboard and dashboard optional extra — P2-7 complete"
```

---

## Task 10: Final verification and ruff

- [ ] **Step 1: Run full test suite**

```bash
python3 -m pytest -q --tb=short
```

Expected: all 244 existing tests pass plus the ~15 new tests. Output ends with `X passed`.

- [ ] **Step 2: Run ruff**

```bash
python3 -m ruff check .
```

Expected: no errors. If any, fix them and re-run before continuing.

- [ ] **Step 3: Open the PR**

```bash
git push -u origin feat/p2-ml-diagnostics-and-dashboard
gh pr create \
  --title "feat: P2 ML diagnostics and dashboard bundle (P2-1/2/3/7)" \
  --body "$(cat <<'EOF'
## Summary

- **P2-1:** `compute_partial_dependence` + `check_monotonicity` added to `evaluation.py` — tabular marginal PDP and monotonicity checks for any feature
- **P2-2:** `compare_models` added to `evaluation.py`; `train_response_models.py` now trains LR + GBM side-by-side and prints a champion/challenger comparison table
- **P2-3:** `write_model_card` added to `agentic.py`; `ModelTrainingAgent.train()` auto-generates one markdown card per candidate into `artifacts/model_cards/`
- **P2-7:** `plot_profit_surface` added to `plotting.py` (saves 2×2 PNG); `scripts/streamlit_dashboard.py` adds interactive profit-surface dashboard (optional `[dashboard]` extra)

## Test plan

- [ ] `pytest -q` — all tests pass (244 existing + ~15 new)
- [ ] `ruff check .` — clean
- [ ] `python3 examples/train_response_models.py` — prints comparison table, no errors
- [ ] `python3 examples/run_baseline.py` — verify model cards written to run artifacts dir
- [ ] `streamlit run scripts/streamlit_dashboard.py` — dashboard loads, surface renders (manual)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review notes

- All function signatures in later tasks match definitions in earlier tasks.
- `compare_models` is in `evaluation.py` (not `train_response_models.py`) so it is importable from tests without adding `examples/` to `pythonpath`.
- `FakePyplot.savefig` and `FakePyplot.close` are added to the existing `FakePyplot` class in `test_plotting.py` to support the new `plot_profit_surface` test.
- `plot_profit_surface` accepts `Any` for `output_path` so `BytesIO` works from the Streamlit dashboard without a type error at runtime.
- Task ordering: evaluation additions (Tasks 2–4) before example script changes (Task 5) which import from evaluation. Agentic additions (Tasks 6–7) are independent and can be done in any order relative to Tasks 2–5.
