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
