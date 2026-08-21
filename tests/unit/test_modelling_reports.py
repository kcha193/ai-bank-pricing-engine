import numpy as np
import pandas as pd
import pytest

from balance_pricing_engine.modelling.evaluation import LIFT_COLUMNS
from balance_pricing_engine.modelling.reports import build_champion_challenger_report
from balance_pricing_engine.modelling.reports import build_lift_stability_report
from balance_pricing_engine.modelling.reports import train_response_model_report


def test_train_response_model_report_returns_metrics_and_direction_check():
    dataset = pd.DataFrame(
        {
            "offered_rate": [0.03, 0.04, 0.05, 0.06, 0.07, 0.08],
            "competitor_rate": [0.05] * 6,
            "rate_gap_to_competitor": [-0.02, -0.01, 0.0, 0.01, 0.02, 0.03],
            "accepted": [1, 1, 1, 0, 0, 0],
        }
    )

    report = train_response_model_report(
        title="Tiny response model",
        dataset=dataset,
        feature_columns=["offered_rate", "competitor_rate", "rate_gap_to_competitor"],
        target_column="accepted",
        offered_rates=[0.03, 0.05, 0.08],
        competitor_rate=0.05,
        expected_direction="decreasing",
        random_seed=42,
        test_size=0.5,
        calibration_bins=2,
    )

    assert report["title"] == "Tiny response model"
    assert set(report["metrics"]) == {"auc", "log_loss", "brier_score"}
    assert report["train_rows"] == 3
    assert report["test_rows"] == 3
    assert list(report["calibration"].columns) == [
        "bin",
        "mean_predicted_probability",
        "observed_rate",
        "count",
    ]
    assert report["response_check"].expected_direction == "decreasing"


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
        challenger_cal_mae=0.15,  # exceeds 0.08 threshold
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


def test_build_champion_challenger_report_notes_champion_rate_check_failure():
    lr_metrics = _fake_metrics(log_loss=0.65)
    gbm_metrics = _fake_metrics(log_loss=0.62)

    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=0.03,
        challenger_cal_mae=0.03,
        champion_rate_check_pass=False,
        challenger_rate_check_pass=True,
    )

    assert report["recommendation"] == "hist_gradient_boosting"
    assert "manual review" in report["reason"].lower() or "champion" in report["reason"].lower()


def test_build_champion_challenger_report_notes_both_rate_check_failures():
    lr_metrics = _fake_metrics(log_loss=0.65)
    gbm_metrics = _fake_metrics(log_loss=0.62)

    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=0.03,
        challenger_cal_mae=0.03,
        champion_rate_check_pass=False,
        challenger_rate_check_pass=False,
    )

    assert report["recommendation"] == "logistic_regression"
    assert "manual review" in report["reason"].lower()


def test_train_response_model_report_includes_lift_table():
    dataset = pd.DataFrame(
        {
            "offered_rate": [0.03, 0.04, 0.05, 0.06, 0.07, 0.08],
            "competitor_rate": [0.05] * 6,
            "rate_gap_to_competitor": [-0.02, -0.01, 0.0, 0.01, 0.02, 0.03],
            "accepted": [1, 1, 1, 0, 0, 0],
        }
    )

    report = train_response_model_report(
        title="Lift table test",
        dataset=dataset,
        feature_columns=["offered_rate", "competitor_rate", "rate_gap_to_competitor"],
        target_column="accepted",
        offered_rates=[0.03, 0.05, 0.08],
        competitor_rate=0.05,
        expected_direction="decreasing",
        random_seed=42,
        test_size=0.5,
        calibration_bins=2,
    )

    assert "lift_table" in report
    assert isinstance(report["lift_table"], pd.DataFrame)
    assert list(report["lift_table"].columns) == LIFT_COLUMNS


def _strong_signal_arrays(n: int = 400, seed: int = 0):
    rng = np.random.default_rng(seed)
    y_pred = np.linspace(1.0, 0.0, n)
    y_true = (np.arange(n) < n // 2).astype(int)
    permutation = rng.permutation(n)
    return y_true[permutation], y_pred[permutation]


def _random_signal_arrays(n: int = 400, seed: int = 1):
    rng = np.random.default_rng(seed)
    y_pred = rng.uniform(0, 1, size=n)
    y_true = rng.integers(0, 2, size=n)
    return y_true, y_pred


def test_build_lift_stability_report_structure():
    y_true, y_pred = _strong_signal_arrays()
    report = build_lift_stability_report(
        y_true, y_pred, n_deciles=5, n_bootstrap=50, random_state=42
    )

    assert list(report["stability_table"].columns) == [
        "decile",
        "mean_lift",
        "std_lift",
        "coefficient_of_variation",
        "min_lift",
        "max_lift",
    ]
    assert len(report["stability_table"]) == 5
    assert report["n_bootstrap"] == 50
    assert isinstance(report["monotonicity_pass_rate"], float)
    assert isinstance(report["is_stable"], bool)
    assert isinstance(report["reason"], str) and report["reason"]


def test_build_lift_stability_report_coefficient_of_variation_matches_mean_and_std():
    y_true, y_pred = _strong_signal_arrays()
    report = build_lift_stability_report(
        y_true, y_pred, n_deciles=5, n_bootstrap=50, random_state=42
    )

    table = report["stability_table"]
    for _, row in table.iterrows():
        if row["mean_lift"] != 0:
            expected_cv = row["std_lift"] / row["mean_lift"]
            assert row["coefficient_of_variation"] == pytest.approx(expected_cv)


def test_build_lift_stability_report_is_stable_matches_its_own_thresholds():
    y_true, y_pred = _strong_signal_arrays()
    max_cv_threshold = 0.5
    report = build_lift_stability_report(
        y_true,
        y_pred,
        n_deciles=5,
        n_bootstrap=50,
        random_state=42,
        max_cv_threshold=max_cv_threshold,
    )

    expected_is_stable = (
        report["monotonicity_pass_rate"] >= 0.8
        and report["stability_table"]["coefficient_of_variation"].max() <= max_cv_threshold
    )
    assert report["is_stable"] == expected_is_stable


def test_build_lift_stability_report_pass_rate_higher_for_strong_signal_than_random():
    strong_true, strong_pred = _strong_signal_arrays(n=400, seed=0)
    random_true, random_pred = _random_signal_arrays(n=400, seed=1)

    strong_report = build_lift_stability_report(
        strong_true, strong_pred, n_deciles=5, n_bootstrap=100, random_state=42
    )
    random_report = build_lift_stability_report(
        random_true, random_pred, n_deciles=5, n_bootstrap=100, random_state=42
    )

    assert strong_report["monotonicity_pass_rate"] > random_report["monotonicity_pass_rate"]


def test_build_lift_stability_report_skips_degenerate_single_class_draws():
    """With 2 positives among 100 rows, many bootstrap draws lose the positive class
    entirely, which used to make compute_lift_table() return NaN lift for every decile
    and poison the mean/std aggregation for the whole report.
    """
    rng = np.random.default_rng(3)
    n = 100
    y_true = np.zeros(n, dtype=int)
    y_true[:2] = 1
    y_true = rng.permutation(y_true)
    y_pred = rng.uniform(0, 1, size=n)

    report = build_lift_stability_report(
        y_true, y_pred, n_deciles=5, n_bootstrap=100, random_state=3
    )

    assert report["n_bootstrap"] == 100
    table = report["stability_table"]
    assert not table[["mean_lift", "std_lift", "min_lift", "max_lift"]].isna().any().any()


def test_build_lift_stability_report_deterministic_given_random_state():
    y_true, y_pred = _strong_signal_arrays()
    report1 = build_lift_stability_report(
        y_true, y_pred, n_deciles=5, n_bootstrap=50, random_state=7
    )
    report2 = build_lift_stability_report(
        y_true, y_pred, n_deciles=5, n_bootstrap=50, random_state=7
    )

    pd.testing.assert_frame_equal(report1["stability_table"], report2["stability_table"])
    assert report1["monotonicity_pass_rate"] == report2["monotonicity_pass_rate"]
