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
    LIFT_COLUMNS,
    METRIC_KEYS,
    MonotonicityCheck,
    check_lift_monotonicity,
    check_rate_response_direction,
    compute_lift_table,
    evaluate_binary_classifier,
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


# --- P2-2: compare_models ---


def test_compare_models_recommends_challenger_when_it_improves():
    from balance_pricing_engine.modelling.evaluation import compare_models

    lr_metrics = {"auc": 0.85, "log_loss": 0.42, "brier_score": 0.18}
    gbm_metrics = {"auc": 0.88, "log_loss": 0.40, "brier_score": 0.17}
    result = compare_models(
        "logistic_regression", lr_metrics,
        "hist_gradient_boosting", gbm_metrics, 0.04,
    )
    assert result["recommended"] == "hist_gradient_boosting"
    assert result["improvement"] == pytest.approx(0.02, abs=1e-6)


def test_compare_models_retains_baseline_when_improvement_below_threshold():
    from balance_pricing_engine.modelling.evaluation import compare_models

    lr_metrics = {"auc": 0.85, "log_loss": 0.42, "brier_score": 0.18}
    gbm_metrics = {"auc": 0.86, "log_loss": 0.415, "brier_score": 0.175}  # only 0.005 improvement
    result = compare_models(
        "logistic_regression", lr_metrics,
        "hist_gradient_boosting", gbm_metrics, 0.04,
    )
    assert result["recommended"] == "logistic_regression"


def test_compare_models_retains_baseline_when_challenger_fails_calibration():
    from balance_pricing_engine.modelling.evaluation import compare_models

    lr_metrics = {"auc": 0.85, "log_loss": 0.42, "brier_score": 0.18}
    gbm_metrics = {"auc": 0.88, "log_loss": 0.40, "brier_score": 0.17}
    result = compare_models(
        "logistic_regression", lr_metrics,
        "hist_gradient_boosting", gbm_metrics, 0.15,  # cal_mae > 0.10
    )
    assert result["recommended"] == "logistic_regression"


# --- flat-model slope detection ---


class FlatModel:
    """Always predicts 0.5 regardless of input."""

    def predict_proba(self, features):
        n = len(features)
        return np.column_stack([np.full(n, 0.5), np.full(n, 0.5)])


def _flat_dataset(n: int = 50) -> pd.DataFrame:
    return pd.DataFrame({"offered_rate": np.full(n, 0.05)})


def test_check_rate_response_direction_fails_for_flat_model():
    check = check_rate_response_direction(
        FlatModel(),
        _flat_dataset(),
        feature_columns=["offered_rate"],
        offered_rates=[0.045, 0.050, 0.055, 0.060],
        competitor_rate=0.045,
        expected_direction="increasing",
    )
    assert not check.is_directionally_sane
    assert check.violations == 0  # flat, no direction violations — failure is from slope


def test_check_rate_response_direction_passes_with_sufficient_slope():
    check = check_rate_response_direction(
        IncreasingRateModel(),
        _flat_dataset(),
        feature_columns=["offered_rate"],
        offered_rates=[0.04, 0.05, 0.06],
        competitor_rate=0.045,
        expected_direction="increasing",
    )
    assert check.is_directionally_sane


def test_check_rate_response_direction_slope_range_is_reported():
    check = check_rate_response_direction(
        FlatModel(),
        _flat_dataset(),
        feature_columns=["offered_rate"],
        offered_rates=[0.04, 0.05, 0.06],
        competitor_rate=0.045,
        expected_direction="increasing",
    )
    assert hasattr(check, "slope_range")
    assert hasattr(check, "min_slope_delta")
    assert check.slope_range == pytest.approx(0.0, abs=1e-9)


def test_check_rate_response_direction_custom_min_slope_delta():
    check = check_rate_response_direction(
        FlatModel(),
        _flat_dataset(),
        feature_columns=["offered_rate"],
        offered_rates=[0.04, 0.05, 0.06],
        competitor_rate=0.045,
        expected_direction="increasing",
        min_slope_delta=0.0,
    )
    assert check.is_directionally_sane


# --- P1-C: evaluate_binary_classifier deprecation ---


def test_evaluate_binary_classifier_emits_deprecation_warning():
    rng = make_rng(302)
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
    with pytest.warns(DeprecationWarning, match="backward-compat alias"):
        evaluate_binary_classifier(model, dataset, DEPOSIT_FEATURE_COLUMNS, DEPOSIT_TARGET_COLUMN)


# --- P2/P3-B: compute_lift_table ---


def test_compute_lift_table_has_correct_columns():
    y_true = np.array([0, 1] * 50)
    y_pred = np.linspace(0, 1, 100)
    result = compute_lift_table(y_true, y_pred)
    assert list(result.columns) == LIFT_COLUMNS


def test_compute_lift_table_decile_count():
    y_true = np.array([0, 1] * 50)
    y_pred = np.linspace(0, 1, 100)
    result = compute_lift_table(y_true, y_pred, n_deciles=5)
    assert len(result) == 5


def test_compute_lift_table_lift_of_perfect_model():
    n = 100
    y_true = np.array([1] * (n // 2) + [0] * (n // 2))
    y_pred = np.linspace(1, 0, n)
    result = compute_lift_table(y_true, y_pred)
    assert result.iloc[0]["lift"] > 1.5


def test_compute_lift_table_lift_of_random_model():
    rng = np.random.default_rng(42)
    n = 1000
    y_true = rng.integers(0, 2, size=n).astype(float)
    y_pred = rng.uniform(0, 1, size=n)
    result = compute_lift_table(y_true, y_pred)
    assert all(abs(row["lift"] - 1.0) < 0.5 for _, row in result.iterrows())


# --- P2/P3-B: check_lift_monotonicity ---


def test_check_lift_monotonicity_passes_for_good_model():
    lift_table = pd.DataFrame(
        {
            "decile": list(range(1, 11)),
            "mean_predicted_probability": [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15, 0.1],
            "observed_rate": [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15, 0.1],
            "lift": [1.8, 1.6, 1.4, 1.2, 1.0, 0.8, 0.6, 0.4, 0.3, 0.2],
            "count": [100] * 10,
        }
    )
    result = check_lift_monotonicity(lift_table)
    assert isinstance(result, MonotonicityCheck)
    assert result.is_monotone is True


def test_check_lift_monotonicity_fails_for_random_predictions():
    rng = np.random.default_rng(99)
    observed_rates = rng.uniform(0.1, 0.9, size=10).tolist()
    lift_table = pd.DataFrame(
        {
            "decile": list(range(1, 11)),
            "mean_predicted_probability": sorted(observed_rates, reverse=True),
            "observed_rate": observed_rates,
            "lift": [r / 0.5 for r in observed_rates],
            "count": [100] * 10,
        }
    )
    result = check_lift_monotonicity(lift_table)
    assert result.violations > 0
