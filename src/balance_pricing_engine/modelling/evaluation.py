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
LIFT_COLUMNS = ["decile", "mean_predicted_probability", "observed_rate", "lift", "count"]


@dataclass(frozen=True)
class MonotonicityCheck:
    feature: str
    expected_direction: str
    is_monotone: bool
    violations: int


@dataclass(frozen=True)
class RateResponseCheck:
    expected_direction: str
    is_directionally_sane: bool
    rate_response: pd.DataFrame
    violations: int
    slope_range: float
    min_slope_delta: float


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


def compute_rate_uplift(
    model: Any,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    rate_feature: str,
    baseline_rate: float,
    treatment_rate: float,
) -> dict[str, float]:
    """Estimate the uplift in predicted response probability from changing one rate feature.

    Scores an already-fitted model at two rate scenarios (holding all other features at their
    observed values) via compute_partial_dependence and reports the probability delta. This is
    a two-point scenario comparison, not a trained causal uplift model — the synthetic
    generators have no treatment/control structure to train one against.
    """
    pdp = compute_partial_dependence(
        model, dataset, feature_columns, rate_feature, [baseline_rate, treatment_rate]
    )
    baseline_probability = float(
        pdp.loc[pdp[rate_feature] == baseline_rate, "mean_predicted_probability"].iloc[0]
    )
    treatment_probability = float(
        pdp.loc[pdp[rate_feature] == treatment_rate, "mean_predicted_probability"].iloc[0]
    )
    uplift = treatment_probability - baseline_probability
    uplift_pct = (uplift / baseline_probability * 100) if baseline_probability else float("nan")
    return {
        "baseline_probability": baseline_probability,
        "treatment_probability": treatment_probability,
        "uplift": uplift,
        "uplift_pct": uplift_pct,
    }


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
    if pdp_feature not in pdp_frame.columns:
        raise ValueError(f"pdp_feature '{pdp_feature}' not found in pdp_frame columns: {list(pdp_frame.columns)}")
    sort_column = pdp_feature
    sorted_pdp_frame = pdp_frame.sort_values(sort_column, ascending=True).copy()
    probs = sorted_pdp_frame["mean_predicted_probability"].to_numpy()
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


def compare_models(
    model_name_a: str,
    metrics_a: dict[str, float],
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


def evaluate_probabilities(y_true: np.ndarray, y_probability: np.ndarray) -> dict[str, float]:
    """Evaluate binary response probabilities with ranking and calibration metrics."""
    return {
        "auc": float(roc_auc_score(y_true, y_probability)),
        "log_loss": float(log_loss(y_true, y_probability, labels=[0, 1])),
        "brier_score": float(brier_score_loss(y_true, y_probability)),
    }


def evaluate_model(
    model: Any,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
) -> dict[str, float]:
    """Evaluate a fitted binary classifier model."""
    y_true = dataset[target_column].to_numpy()
    y_probability = predict_response_probability(model, dataset, feature_columns)
    return evaluate_probabilities(y_true, y_probability)


def evaluate_binary_classifier(
    model: Any,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
) -> dict[str, float]:
    """Backward-compatible alias for evaluate_model."""
    import warnings

    warnings.warn(
        "evaluate_binary_classifier is a backward-compat alias; use evaluate_model().",
        DeprecationWarning,
        stacklevel=2,
    )
    return evaluate_model(model, dataset, feature_columns, target_column)


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


def compute_lift_table(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    n_deciles: int = 10,
) -> pd.DataFrame:
    """Build a decile lift table sorted by descending predicted probability."""
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must have the same length.")
    if n_deciles < 2 or n_deciles > len(y_true):
        raise ValueError("n_deciles must be between 2 and len(y_true).")
    population_rate = float(np.mean(y_true))
    order = np.argsort(y_pred)[::-1]
    sorted_true = y_true[order]
    sorted_pred = y_pred[order]
    bins = np.array_split(np.arange(len(y_true)), n_deciles)
    rows = []
    for i, idx in enumerate(bins):
        obs = float(np.mean(sorted_true[idx]))
        pred = float(np.mean(sorted_pred[idx]))
        lift = obs / population_rate if population_rate > 0 else float("nan")
        rows.append(
            {
                "decile": i + 1,
                "mean_predicted_probability": pred,
                "observed_rate": obs,
                "lift": lift,
                "count": len(idx),
            }
        )
    return pd.DataFrame(rows, columns=LIFT_COLUMNS)


def check_lift_monotonicity(lift_table: pd.DataFrame) -> MonotonicityCheck:
    """Check whether the observed_rate column decreases monotonically across deciles."""
    return check_monotonicity(
        lift_table[["decile", "observed_rate"]].rename(columns={"observed_rate": "mean_predicted_probability"}),
        pdp_feature="decile",
        expected_direction="decreasing",
    )


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
    min_slope_delta: float = 0.02,
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
    slope_range = float(
        response["mean_predicted_probability"].max()
        - response["mean_predicted_probability"].min()
    )
    tolerance = 1e-6
    if expected_direction == "decreasing":
        violations = int(np.sum(deltas > tolerance))
    else:
        violations = int(np.sum(deltas < -tolerance))

    return RateResponseCheck(
        expected_direction=expected_direction,
        is_directionally_sane=violations == 0 and slope_range >= min_slope_delta,
        rate_response=response,
        violations=violations,
        slope_range=slope_range,
        min_slope_delta=min_slope_delta,
    )
