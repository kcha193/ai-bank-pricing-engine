import numpy as np

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
