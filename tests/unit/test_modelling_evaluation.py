import numpy as np
import pandas as pd
import pytest

from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    build_deposit_response_dataset,
)
from balance_pricing_engine.modelling.evaluation import (
    CALIBRATION_COLUMNS,
    METRIC_KEYS,
    check_rate_response_direction,
    evaluate_model,
    evaluate_probabilities,
    evaluate_calibration,
)
from balance_pricing_engine.modelling.models import (
    build_logistic_regression_model,
    fit_response_model,
)
from balance_pricing_engine.synthetic.customers import generate_deposit_customers
from balance_pricing_engine.utils.random import make_rng


class DecreasingRateModel:
    def predict_proba(self, features):
        probability = 0.9 - 5 * features["offered_rate"].to_numpy()
        probability = np.clip(probability, 0.01, 0.99)
        return np.column_stack([1 - probability, probability])


class IncreasingRateModel:
    def predict_proba(self, features):
        probability = 0.1 + 8 * features["offered_rate"].to_numpy()
        probability = np.clip(probability, 0.01, 0.99)
        return np.column_stack([1 - probability, probability])


def test_evaluation_metrics_and_calibration_return_expected_outputs():
    rng = make_rng(300)
    customers = generate_deposit_customers(250, rng)
    dataset = build_deposit_response_dataset(
        customers,
        offered_rates=np.array([0.04, 0.05, 0.06]),
        competitor_rate=0.045,
        rate_elasticity=95.0,
        competitor_gap_elasticity=75.0,
        rng=rng,
        n_samples=800,
    )
    model = fit_response_model(
        build_logistic_regression_model(),
        dataset,
        DEPOSIT_FEATURE_COLUMNS,
        DEPOSIT_TARGET_COLUMN,
    )

    metrics = evaluate_model(
        model,
        dataset,
        DEPOSIT_FEATURE_COLUMNS,
        DEPOSIT_TARGET_COLUMN,
    )
    calibration = evaluate_calibration(
        model,
        dataset,
        DEPOSIT_FEATURE_COLUMNS,
        DEPOSIT_TARGET_COLUMN,
        n_bins=5,
    )

    assert list(metrics.keys()) == METRIC_KEYS
    assert list(calibration.columns) == CALIBRATION_COLUMNS
    assert calibration["count"].sum() == len(dataset)


def test_evaluate_probabilities_returns_binary_classifier_metrics():
    y_true = np.array([0, 0, 1, 1])
    y_probability = np.array([0.1, 0.2, 0.8, 0.9])

    metrics = evaluate_probabilities(y_true, y_probability)

    assert list(metrics.keys()) == METRIC_KEYS
    assert metrics["auc"] == 1.0


def test_rate_response_sanity_check_detects_expected_direction():
    rng = make_rng(301)
    customers = generate_deposit_customers(20, rng)
    dataset = build_deposit_response_dataset(
        customers,
        offered_rates=np.array([0.04]),
        competitor_rate=0.045,
        rate_elasticity=95.0,
        competitor_gap_elasticity=75.0,
        rng=rng,
        n_samples=20,
    )

    decreasing_check = check_rate_response_direction(
        DecreasingRateModel(),
        dataset,
        DEPOSIT_FEATURE_COLUMNS,
        offered_rates=[0.04, 0.05, 0.06],
        competitor_rate=0.045,
        expected_direction="decreasing",
    )
    increasing_check = check_rate_response_direction(
        IncreasingRateModel(),
        dataset,
        DEPOSIT_FEATURE_COLUMNS,
        offered_rates=[0.04, 0.05, 0.06],
        competitor_rate=0.045,
        expected_direction="increasing",
    )

    assert decreasing_check.is_directionally_sane
    assert increasing_check.is_directionally_sane
    assert decreasing_check.violations == 0
    assert increasing_check.violations == 0


# --- P2-1: compute_partial_dependence ---


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
    from balance_pricing_engine.modelling.evaluation import compute_partial_dependence

    dataset = _minimal_dataset()
    model = OfferedRateDecreasingModel()
    feature_cols = ["offered_rate", "feat_b"]
    grid = [0.085, 0.075, 0.080]  # intentionally unsorted

    result = compute_partial_dependence(model, dataset, feature_cols, "offered_rate", grid)

    assert list(result.columns) == ["offered_rate", "mean_predicted_probability"]
    assert list(result["offered_rate"]) == sorted(grid)
    assert len(result) == 3


def test_compute_partial_dependence_mean_decreases_for_decreasing_model():
    from balance_pricing_engine.modelling.evaluation import compute_partial_dependence

    dataset = _minimal_dataset()
    model = OfferedRateDecreasingModel()
    feature_cols = ["offered_rate", "feat_b"]
    grid = [0.075, 0.080, 0.085]

    result = compute_partial_dependence(model, dataset, feature_cols, "offered_rate", grid)

    probs = result["mean_predicted_probability"].to_numpy()
    assert probs[0] > probs[1] > probs[2]


def test_compute_partial_dependence_raises_on_single_grid_value():
    from balance_pricing_engine.modelling.evaluation import compute_partial_dependence

    dataset = _minimal_dataset()
    model = OfferedRateDecreasingModel()
    with pytest.raises(ValueError, match="at least 2"):
        compute_partial_dependence(model, dataset, ["offered_rate", "feat_b"], "offered_rate", [0.08])


def test_compute_partial_dependence_raises_when_feature_not_in_feature_columns():
    from balance_pricing_engine.modelling.evaluation import compute_partial_dependence

    dataset = _minimal_dataset()
    model = OfferedRateDecreasingModel()
    with pytest.raises(ValueError, match="not in feature_columns"):
        compute_partial_dependence(model, dataset, ["feat_b"], "offered_rate", [0.07, 0.08])


# --- P2-1: check_monotonicity ---


def test_check_monotonicity_pass_decreasing():
    from balance_pricing_engine.modelling.evaluation import MonotonicityCheck, check_monotonicity

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
    from balance_pricing_engine.modelling.evaluation import check_monotonicity

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
    from balance_pricing_engine.modelling.evaluation import check_monotonicity

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
    from balance_pricing_engine.modelling.evaluation import check_monotonicity

    pdp = pd.DataFrame({"x": [1, 2], "mean_predicted_probability": [0.4, 0.6]})
    with pytest.raises(ValueError, match="expected_direction"):
        check_monotonicity(pdp, "x", "sideways")


def test_check_monotonicity_raises_on_too_few_rows():
    from balance_pricing_engine.modelling.evaluation import check_monotonicity

    pdp = pd.DataFrame({"x": [1], "mean_predicted_probability": [0.5]})
    with pytest.raises(ValueError, match="at least 2 rows"):
        check_monotonicity(pdp, "x", "increasing")
