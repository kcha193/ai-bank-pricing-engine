"""Report builders for synthetic response model training workflows."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sklearn.model_selection import train_test_split

from balance_pricing_engine.modelling.evaluation import (
    check_rate_response_direction,
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
