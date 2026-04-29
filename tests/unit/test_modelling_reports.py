import pandas as pd

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
