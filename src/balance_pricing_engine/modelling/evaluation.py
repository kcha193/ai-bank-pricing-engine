"""Evaluation helpers for learned response models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from balance_pricing_engine.modelling.models import predict_response_probability


METRIC_KEYS = ["auc", "log_loss", "brier_score"]
CALIBRATION_COLUMNS = [
    "bin",
    "mean_predicted_probability",
    "observed_rate",
    "count",
]


@dataclass(frozen=True)
class RateResponseCheck:
    expected_direction: str
    is_directionally_sane: bool
    rate_response: pd.DataFrame
    violations: int


def evaluate_binary_classifier(
    model: Any,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
) -> dict[str, float]:
    """Evaluate a binary classifier with ranking, likelihood, and calibration metrics."""
    y_true = dataset[target_column].to_numpy()
    y_probability = predict_response_probability(model, dataset, feature_columns)

    return {
        "auc": float(roc_auc_score(y_true, y_probability)),
        "log_loss": float(log_loss(y_true, y_probability, labels=[0, 1])),
        "brier_score": float(brier_score_loss(y_true, y_probability)),
    }


def build_calibration_table(
    y_true: np.ndarray,
    y_probability: np.ndarray,
    n_bins: int = 10,
) -> pd.DataFrame:
    """Build a simple probability calibration table."""
    if n_bins <= 0:
        raise ValueError("n_bins must be positive.")

    calibration = pd.DataFrame(
        {
            "target": y_true,
            "predicted_probability": y_probability,
        }
    )
    calibration["bin"] = pd.cut(
        calibration["predicted_probability"],
        bins=np.linspace(0, 1, n_bins + 1),
        labels=False,
        include_lowest=True,
    )
    grouped = calibration.groupby("bin", observed=True)
    table = grouped.agg(
        mean_predicted_probability=("predicted_probability", "mean"),
        observed_rate=("target", "mean"),
        count=("target", "size"),
    ).reset_index()
    return table[CALIBRATION_COLUMNS]


def evaluate_calibration(
    model: Any,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
    n_bins: int = 10,
) -> pd.DataFrame:
    """Predict probabilities and build a calibration table."""
    y_probability = predict_response_probability(model, dataset, feature_columns)
    return build_calibration_table(
        dataset[target_column].to_numpy(),
        y_probability,
        n_bins,
    )


def check_rate_response_direction(
    model: Any,
    base_dataset: pd.DataFrame,
    feature_columns: list[str],
    offered_rates: list[float],
    competitor_rate: float,
    expected_direction: str,
) -> RateResponseCheck:
    """Check whether average predicted response moves in the expected rate direction."""
    if expected_direction not in {"decreasing", "increasing"}:
        raise ValueError("expected_direction must be 'decreasing' or 'increasing'.")
    if len(offered_rates) < 2:
        raise ValueError("At least two offered rates are required.")

    rows: list[dict[str, float]] = []
    for offered_rate in sorted(offered_rates):
        scenario = base_dataset.copy()
        scenario["offered_rate"] = offered_rate
        scenario["competitor_rate"] = competitor_rate
        scenario["rate_gap_to_competitor"] = offered_rate - competitor_rate
        probability = predict_response_probability(model, scenario, feature_columns)
        rows.append(
            {
                "offered_rate": offered_rate,
                "mean_predicted_probability": float(np.mean(probability)),
            }
        )

    response = pd.DataFrame(rows)
    deltas = np.diff(response["mean_predicted_probability"].to_numpy())
    tolerance = 1e-6
    if expected_direction == "decreasing":
        violations = int(np.sum(deltas > tolerance))
    else:
        violations = int(np.sum(deltas < -tolerance))

    return RateResponseCheck(
        expected_direction=expected_direction,
        is_directionally_sane=violations == 0,
        rate_response=response,
        violations=violations,
    )
