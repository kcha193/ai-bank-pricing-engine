# P2-2: Champion/Challenger Comparison Report

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `build_champion_challenger_report()` function to `reports.py` and use it in `train_response_models.py` to produce a structured side-by-side comparison table that a model risk reviewer can read.

**Architecture:** A new `build_champion_challenger_report()` function in `modelling/reports.py` takes two fitted models plus their pre-computed metrics, calibration MAEs, and rate-response checks, and returns a dict with a `rows` list ready for tabular display. `train_response_models.py` calls it and prints a formatted table. No new dependencies — `compare_models` in `evaluation.py` already provides the recommendation logic.

**Tech Stack:** Python 3.9+, pandas, scikit-learn, pytest. No new packages.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `src/balance_pricing_engine/modelling/reports.py` | Add `build_champion_challenger_report()` |
| Modify | `examples/train_response_models.py` | Replace `_print_comparison_table` + manual prints with `build_champion_challenger_report` + `print_champion_challenger_report` |
| Modify | `tests/unit/test_modelling_reports.py` | Tests for the new function |

---

## Task 1: `build_champion_challenger_report` in `reports.py`

**Files:**
- Modify: `src/balance_pricing_engine/modelling/reports.py`
- Test: `tests/unit/test_modelling_reports.py`

- [ ] **Step 1: Read the existing test file to understand the pattern**

Run:
```bash
cat tests/unit/test_modelling_reports.py
```

- [ ] **Step 2: Write the failing test**

Add to `tests/unit/test_modelling_reports.py`:

```python
from balance_pricing_engine.modelling.reports import build_champion_challenger_report


def _fake_metrics(auc: float = 0.70, log_loss: float = 0.65, brier: float = 0.22) -> dict:
    return {"auc": auc, "log_loss": log_loss, "brier_score": brier}


def test_build_champion_challenger_report_structure():
    lr_metrics = _fake_metrics(auc=0.68, log_loss=0.67, brier=0.23)
    gbm_metrics = _fake_metrics(auc=0.72, log_loss=0.63, brier=0.21)

    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=0.035,
        challenger_cal_mae=0.028,
        champion_rate_check_pass=True,
        challenger_rate_check_pass=True,
    )

    assert "rows" in report
    assert "recommendation" in report
    assert "reason" in report
    metric_names = [row["metric"] for row in report["rows"]]
    assert "auc" in metric_names
    assert "log_loss" in metric_names
    assert "brier_score" in metric_names
    assert "calibration_mae" in metric_names
    assert "rate_response_check" in metric_names


def test_build_champion_challenger_report_recommends_challenger_when_clearly_better():
    lr_metrics = _fake_metrics(auc=0.68, log_loss=0.70, brier=0.23)
    gbm_metrics = _fake_metrics(auc=0.75, log_loss=0.58, brier=0.20)

    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=0.04,
        challenger_cal_mae=0.02,
        champion_rate_check_pass=True,
        challenger_rate_check_pass=True,
    )

    assert report["recommendation"] == "hist_gradient_boosting"


def test_build_champion_challenger_report_retains_champion_on_small_improvement():
    lr_metrics = _fake_metrics(log_loss=0.65)
    gbm_metrics = _fake_metrics(log_loss=0.648)  # < 0.01 improvement threshold

    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=0.03,
        challenger_cal_mae=0.03,
        champion_rate_check_pass=True,
        challenger_rate_check_pass=True,
    )

    assert report["recommendation"] == "logistic_regression"


def test_build_champion_challenger_report_retains_champion_on_poor_calibration():
    lr_metrics = _fake_metrics(log_loss=0.65)
    gbm_metrics = _fake_metrics(log_loss=0.62)

    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=0.03,
        challenger_cal_mae=0.15,  # exceeds 0.10 threshold
        champion_rate_check_pass=True,
        challenger_rate_check_pass=True,
    )

    assert report["recommendation"] == "logistic_regression"
    assert "calibration" in report["reason"]


def test_build_champion_challenger_report_flags_failed_rate_check():
    lr_metrics = _fake_metrics(log_loss=0.65)
    gbm_metrics = _fake_metrics(log_loss=0.62)

    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=0.03,
        challenger_cal_mae=0.02,
        champion_rate_check_pass=True,
        challenger_rate_check_pass=False,
    )

    assert report["recommendation"] == "logistic_regression"
    assert "rate response" in report["reason"].lower()
```

- [ ] **Step 3: Run the tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_modelling_reports.py::test_build_champion_challenger_report_structure -v
```

Expected: `ImportError` or `AttributeError` — `build_champion_challenger_report` not defined.

- [ ] **Step 4: Implement `build_champion_challenger_report` in `reports.py`**

Replace the full content of `src/balance_pricing_engine/modelling/reports.py` with:

```python
"""Report builders for synthetic response model training workflows."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sklearn.model_selection import train_test_split

from balance_pricing_engine.modelling.evaluation import (
    check_rate_response_direction,
    compare_models,
    evaluate_binary_classifier,
    evaluate_calibration,
)
from balance_pricing_engine.modelling.models import (
    build_logistic_regression_model,
    fit_response_model,
)


def train_response_model_report(
    title: str,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
    offered_rates: list[float],
    competitor_rate: float,
    expected_direction: str,
    random_seed: int,
    test_size: float = 0.30,
    calibration_bins: int = 5,
) -> dict[str, Any]:
    """Train a response model and return metrics, calibration, and rate-response checks."""
    train, test = train_test_split(
        dataset,
        test_size=test_size,
        random_state=random_seed,
        stratify=dataset[target_column],
    )
    model = fit_response_model(
        build_logistic_regression_model(random_seed),
        train,
        feature_columns,
        target_column,
    )
    metrics = evaluate_binary_classifier(model, test, feature_columns, target_column)
    calibration = evaluate_calibration(
        model,
        test,
        feature_columns,
        target_column,
        n_bins=calibration_bins,
    )
    response_check = check_rate_response_direction(
        model,
        test.head(1_000),
        feature_columns,
        offered_rates,
        competitor_rate,
        expected_direction,
    )
    return {
        "title": title,
        "model": model,
        "metrics": metrics,
        "calibration": calibration,
        "response_check": response_check,
        "train_rows": len(train),
        "test_rows": len(test),
    }


def build_champion_challenger_report(
    champion_name: str,
    challenger_name: str,
    champion_metrics: dict[str, float],
    challenger_metrics: dict[str, float],
    champion_cal_mae: float,
    challenger_cal_mae: float,
    champion_rate_check_pass: bool,
    challenger_rate_check_pass: bool,
    *,
    min_log_loss_improvement: float = 0.01,
    max_calibration_error: float = 0.10,
) -> dict[str, Any]:
    """Build a structured side-by-side comparison report for two candidate models.

    Returns a dict with:
      rows: list of metric dicts with keys 'metric', champion_name, challenger_name
      recommendation: name of the recommended model
      reason: human-readable explanation
    """
    # Determine recommendation — challenger qualifies only if it clears both thresholds
    # AND the rate-response sanity check passes.
    log_loss_improvement = champion_metrics["log_loss"] - challenger_metrics["log_loss"]
    calibration_ok = challenger_cal_mae <= max_calibration_error
    improvement_ok = log_loss_improvement >= min_log_loss_improvement

    if not challenger_rate_check_pass:
        recommendation = champion_name
        reason = (
            f"champion retained — challenger failed rate response sanity check "
            f"(log_loss improvement was {log_loss_improvement:.4f})"
        )
    elif not calibration_ok:
        recommendation = champion_name
        reason = (
            f"champion retained — challenger calibration MAE {challenger_cal_mae:.4f} "
            f"exceeds threshold {max_calibration_error:.2f}"
        )
    elif not improvement_ok:
        recommendation = champion_name
        reason = (
            f"champion retained — log_loss improvement {log_loss_improvement:.4f} "
            f"below threshold {min_log_loss_improvement:.2f}"
        )
    else:
        recommendation = challenger_name
        reason = (
            f"challenger recommended — log_loss improved by {log_loss_improvement:.4f}, "
            f"calibration MAE {challenger_cal_mae:.4f} within threshold"
        )

    rows = [
        {
            "metric": "auc",
            champion_name: champion_metrics["auc"],
            challenger_name: challenger_metrics["auc"],
        },
        {
            "metric": "log_loss",
            champion_name: champion_metrics["log_loss"],
            challenger_name: challenger_metrics["log_loss"],
        },
        {
            "metric": "brier_score",
            champion_name: champion_metrics["brier_score"],
            challenger_name: challenger_metrics["brier_score"],
        },
        {
            "metric": "calibration_mae",
            champion_name: champion_cal_mae,
            challenger_name: challenger_cal_mae,
        },
        {
            "metric": "rate_response_check",
            champion_name: "pass" if champion_rate_check_pass else "fail",
            challenger_name: "pass" if challenger_rate_check_pass else "fail",
        },
    ]

    return {
        "champion_name": champion_name,
        "challenger_name": challenger_name,
        "rows": rows,
        "recommendation": recommendation,
        "reason": reason,
        "log_loss_improvement": log_loss_improvement,
    }
```

- [ ] **Step 5: Run the new tests**

```bash
python3 -m pytest tests/unit/test_modelling_reports.py -v
```

Expected: all new tests PASS; existing `train_response_model_report` tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/modelling/reports.py tests/unit/test_modelling_reports.py
git commit -m "feat: add build_champion_challenger_report to modelling/reports"
```

---

## Task 2: Wire into `train_response_models.py`

**Files:**
- Modify: `examples/train_response_models.py`
- Test: `tests/unit/test_examples.py` (smoke test already covers this)

- [ ] **Step 1: Check that `test_examples.py` covers the example script**

```bash
grep -n "train_response" tests/unit/test_examples.py
```

If `train_response_models` is listed, the smoke test will catch regressions automatically.

- [ ] **Step 2: Add `print_champion_challenger_report` to `train_response_models.py`**

Add this function after `_print_comparison_table`:

```python
def print_champion_challenger_report(report: dict) -> None:
    champion = report["champion_name"]
    challenger = report["challenger_name"]
    header = f"  {'metric':<22}  {champion:>24}  {challenger:>24}"
    print(header)
    for row in report["rows"]:
        champ_val = row[champion]
        chall_val = row[challenger]
        if isinstance(champ_val, float):
            print(f"  {row['metric']:<22}  {champ_val:>24.4f}  {chall_val:>24.4f}")
        else:
            print(f"  {row['metric']:<22}  {champ_val:>24}  {chall_val:>24}")
    print(f"  Recommendation: {report['recommendation']}")
    print(f"    reason: {report['reason']}")
```

- [ ] **Step 3: Add the import at the top of `train_response_models.py`**

In the imports block, add:

```python
from balance_pricing_engine.modelling.reports import build_champion_challenger_report
```

- [ ] **Step 4: Replace the two separate metric prints in `train_and_report` with the new function**

In `train_and_report`, replace these lines:
```python
    print(title)
    _print_comparison_table(lr_metrics, gbm_metrics, lr_cal_mae, gbm_cal_mae)
    _print_governed_candidate_table(
        dataset=dataset,
        feature_columns=feature_columns,
        target_column=target_column,
        random_seed=random_seed,
    )
    print(f"  Recommendation: {comparison['recommended']}")
    print(f"    reason: {comparison['reason']}")
    print("  Logistic regression rate response")
    print_rate_response_check(lr_check)
    print("  Hist gradient boosting rate response")
    print_rate_response_check(gbm_check)
```

With:
```python
    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=lr_cal_mae,
        challenger_cal_mae=gbm_cal_mae,
        champion_rate_check_pass=lr_check.is_directionally_sane,
        challenger_rate_check_pass=gbm_check.is_directionally_sane,
    )

    print(title)
    print_champion_challenger_report(report)
    _print_governed_candidate_table(
        dataset=dataset,
        feature_columns=feature_columns,
        target_column=target_column,
        random_seed=random_seed,
    )
    print("  Logistic regression rate response")
    print_rate_response_check(lr_check)
    print("  Hist gradient boosting rate response")
    print_rate_response_check(gbm_check)
```

- [ ] **Step 5: Remove the now-unused `compare_models` import and `_print_comparison_table` function**

Remove `compare_models` from the import block in `train_response_models.py`. Remove the `_print_comparison_table` function definition. Confirm `compare_models` is not referenced anywhere else in the file.

- [ ] **Step 6: Run the full example to verify output**

```bash
python3 examples/train_response_models.py
```

Expected: prints a side-by-side table for each model, then recommendation, then governed agentic table, then rate response check.

- [ ] **Step 7: Run the test suite**

```bash
python3 -m pytest tests/unit/test_examples.py tests/unit/test_modelling_reports.py -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add examples/train_response_models.py
git commit -m "feat: use build_champion_challenger_report in train_response_models example"
```

---

## Self-review

**Spec coverage:**
- `reports.py` gets `build_champion_challenger_report` ✓
- `train_response_models.py` uses it ✓
- Tests cover recommendation logic (4 branches: challenger wins, champion retained on improvement, calibration, rate-response) ✓

**Placeholder scan:** None found.

**Type consistency:** `champion_metrics` / `challenger_metrics` typed `dict[str, float]` throughout. `rows` list has consistent keys. `print_champion_challenger_report` accepts the exact dict returned by `build_champion_challenger_report`.
