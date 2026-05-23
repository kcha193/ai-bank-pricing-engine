import pandas as pd

from balance_pricing_engine.modelling.reports import build_champion_challenger_report
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
