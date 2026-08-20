"""Report builders for synthetic response model training workflows."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

from balance_pricing_engine.modelling.evaluation import (
    check_rate_response_direction,
    check_lift_monotonicity,
    compute_lift_table,
    evaluate_calibration,
    evaluate_model,
)
from balance_pricing_engine.modelling.models import (
    build_logistic_regression_model,
    fit_response_model,
    predict_response_probability,
)

LIFT_STABILITY_COLUMNS = [
    "decile",
    "mean_lift",
    "std_lift",
    "coefficient_of_variation",
    "min_lift",
    "max_lift",
]


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
    metrics = evaluate_model(model, test, feature_columns, target_column)
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
    n_deciles = min(10, len(test))
    lift_table = compute_lift_table(
        test[target_column].to_numpy(),
        predict_response_probability(model, test, feature_columns),
        n_deciles=n_deciles,
    )
    lift_check = check_lift_monotonicity(lift_table)
    return {
        "title": title,
        "model": model,
        "metrics": metrics,
        "calibration": calibration,
        "response_check": response_check,
        "lift_table": lift_table,
        "lift_monotone": lift_check.is_monotone,
        "lift_violations": lift_check.violations,
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
    max_calibration_error: float = 0.08,
) -> dict[str, Any]:
    """Build a structured side-by-side comparison report for two candidate models.

    Returns a dict with:
      rows: list of metric dicts with keys 'metric', champion_name, challenger_name
      recommendation: name of the recommended model
      reason: human-readable explanation
    """
    # Determine recommendation — challenger qualifies only if it clears both thresholds
    # AND the rate-response sanity check passes. A failing champion rate check is noted
    # but does not by itself override the metric gates.
    log_loss_improvement = champion_metrics["log_loss"] - challenger_metrics["log_loss"]
    calibration_ok = challenger_cal_mae <= max_calibration_error
    improvement_ok = log_loss_improvement >= min_log_loss_improvement
    champion_rate_warning = not champion_rate_check_pass

    if not challenger_rate_check_pass:
        recommendation = champion_name
        reason = (
            f"champion retained — challenger failed rate response sanity check "
            f"(log_loss improvement was {log_loss_improvement:.4f})"
        )
        if champion_rate_warning:
            reason += "; NOTE: champion also failed rate response check — manual review required"
    elif not calibration_ok:
        recommendation = champion_name
        reason = (
            f"champion retained — challenger calibration MAE {challenger_cal_mae:.4f} "
            f"exceeds threshold {max_calibration_error:.2f}"
        )
        if champion_rate_warning:
            reason += "; NOTE: champion also failed rate response check — manual review required"
    elif not improvement_ok:
        recommendation = champion_name
        reason = (
            f"champion retained — log_loss improvement {log_loss_improvement:.4f} "
            f"below threshold {min_log_loss_improvement:.2f}"
        )
        if champion_rate_warning:
            reason += "; NOTE: champion also failed rate response check — manual review required"
    else:
        recommendation = challenger_name
        reason = (
            f"challenger recommended — log_loss improved by {log_loss_improvement:.4f}, "
            f"calibration MAE {challenger_cal_mae:.4f} within threshold"
        )
        if champion_rate_warning:
            reason += "; champion rate response check also failed"

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


def build_lift_stability_report(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    n_deciles: int = 10,
    n_bootstrap: int = 200,
    random_state: int = 42,
    max_cv_threshold: float = 0.5,
) -> dict[str, Any]:
    """Bootstrap-resample scored predictions to assess decile lift stability under sampling noise.

    Resamples (y_true, y_pred) pairs with replacement `n_bootstrap` times, recomputing the
    decile lift table each draw. Reports how often the lift ordering stays monotone and how
    much each decile's lift varies across draws — not model-refit stability across training
    splits, which is a different (and much more expensive) question.
    """
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    rng = np.random.default_rng(random_state)
    n = len(y_true)

    lift_draws: list[list[float]] = [[] for _ in range(n_deciles)]
    monotone_count = 0
    for _ in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        table = compute_lift_table(y_true[idx], y_pred[idx], n_deciles=n_deciles)
        if check_lift_monotonicity(table).is_monotone:
            monotone_count += 1
        for row in table.itertuples():
            lift_draws[row.decile - 1].append(row.lift)

    rows = []
    for decile, lifts in enumerate(lift_draws, start=1):
        arr = np.array(lifts)
        mean_lift = float(np.mean(arr))
        std_lift = float(np.std(arr))
        rows.append(
            {
                "decile": decile,
                "mean_lift": mean_lift,
                "std_lift": std_lift,
                "coefficient_of_variation": std_lift / mean_lift if mean_lift else float("nan"),
                "min_lift": float(np.min(arr)),
                "max_lift": float(np.max(arr)),
            }
        )
    stability_table = pd.DataFrame(rows, columns=LIFT_STABILITY_COLUMNS)

    monotonicity_pass_rate = monotone_count / n_bootstrap
    max_cv = float(stability_table["coefficient_of_variation"].max())
    is_stable = monotonicity_pass_rate >= 0.8 and max_cv <= max_cv_threshold
    reason = (
        f"lift ordering stayed monotone in {monotonicity_pass_rate:.0%} of {n_bootstrap} "
        f"bootstrap resamples; max decile coefficient of variation was {max_cv:.3f} "
        f"(threshold {max_cv_threshold:.2f})"
    )

    return {
        "stability_table": stability_table,
        "monotonicity_pass_rate": monotonicity_pass_rate,
        "is_stable": is_stable,
        "reason": reason,
        "n_bootstrap": n_bootstrap,
    }
