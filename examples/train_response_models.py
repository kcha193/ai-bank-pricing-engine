"""Train ML response models against synthetic ground-truth customer behaviour."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

from balance_pricing_engine.config import load_config
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.evaluation import (
    check_rate_response_direction,
    compare_models,
    evaluate_binary_classifier,
    evaluate_calibration,
)
from balance_pricing_engine.modelling.models import (
    build_hist_gradient_boosting_model,
    build_logistic_regression_model,
    fit_response_model,
)
from balance_pricing_engine.scenarios.runner import build_configured_rate_grids
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.utils.random import make_rng


def main() -> None:
    config = load_config(Path("configs/base.yaml"))
    rng = make_rng(config.random_seed)

    loan_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    deposit_customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
    loan_grid, deposit_grid = build_configured_rate_grids(config)

    loan_dataset = build_loan_response_dataset(
        loan_customers,
        loan_grid,
        config.market.competitor_loan_rate,
        config.response.loan_rate_elasticity,
        config.response.loan_competitor_gap_elasticity,
        rng,
        n_samples=config.simulation.n_loan_customers * 3,
    )
    deposit_dataset = build_deposit_response_dataset(
        deposit_customers,
        deposit_grid,
        config.market.competitor_deposit_rate,
        config.response.deposit_rate_elasticity,
        config.response.deposit_competitor_gap_elasticity,
        rng,
        n_samples=config.simulation.n_deposit_customers * 3,
    )

    train_and_report(
        title="Home loan acceptance model",
        dataset=loan_dataset,
        feature_columns=LOAN_FEATURE_COLUMNS,
        target_column=LOAN_TARGET_COLUMN,
        offered_rates=np.linspace(0.0725, 0.0875, 9).tolist(),
        competitor_rate=config.market.competitor_loan_rate,
        expected_direction="decreasing",
        random_seed=config.random_seed,
    )
    print()
    train_and_report(
        title="Term deposit uptake model",
        dataset=deposit_dataset,
        feature_columns=DEPOSIT_FEATURE_COLUMNS,
        target_column=DEPOSIT_TARGET_COLUMN,
        offered_rates=np.linspace(0.0450, 0.0600, 7).tolist(),
        competitor_rate=config.market.competitor_deposit_rate,
        expected_direction="increasing",
        random_seed=config.random_seed,
    )


def train_and_report(
    title: str,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
    offered_rates: list[float],
    competitor_rate: float,
    expected_direction: str,
    random_seed: int,
) -> None:
    train, test = train_test_split(
        dataset,
        test_size=0.30,
        random_state=random_seed,
        stratify=dataset[target_column],
    )
    lr_model = fit_response_model(
        build_logistic_regression_model(random_seed), train, feature_columns, target_column
    )
    gbm_model = fit_response_model(
        build_hist_gradient_boosting_model(random_seed), train, feature_columns, target_column
    )

    lr_metrics = evaluate_binary_classifier(lr_model, test, feature_columns, target_column)
    gbm_metrics = evaluate_binary_classifier(gbm_model, test, feature_columns, target_column)

    lr_cal = evaluate_calibration(lr_model, test, feature_columns, target_column, n_bins=5)
    gbm_cal = evaluate_calibration(gbm_model, test, feature_columns, target_column, n_bins=5)
    lr_cal_mae = float(
        (lr_cal["mean_predicted_probability"] - lr_cal["observed_rate"]).abs().mean()
    )
    gbm_cal_mae = float(
        (gbm_cal["mean_predicted_probability"] - gbm_cal["observed_rate"]).abs().mean()
    )

    comparison = compare_models(
        "logistic_regression", lr_metrics,
        "hist_gradient_boosting", gbm_metrics, gbm_cal_mae,
    )

    lr_check = check_rate_response_direction(
        lr_model, test.head(1_000), feature_columns, offered_rates, competitor_rate,
        expected_direction,
    )
    gbm_check = check_rate_response_direction(
        gbm_model, test.head(1_000), feature_columns, offered_rates, competitor_rate,
        expected_direction,
    )

    print(title)
    _print_comparison_table(lr_metrics, gbm_metrics, lr_cal_mae, gbm_cal_mae)
    print(f"  Recommendation: {comparison['recommended']}")
    print(f"    reason: {comparison['reason']}")
    print("  Logistic regression rate response")
    print_rate_response_check(lr_check)
    print("  Hist gradient boosting rate response")
    print_rate_response_check(gbm_check)


def _print_comparison_table(
    lr_metrics: dict,
    gbm_metrics: dict,
    lr_cal_mae: float,
    gbm_cal_mae: float,
) -> None:
    header = f"  {'metric':<22}  {'logistic_regression':>22}  {'hist_gradient_boosting':>22}"
    print(header)
    for key in ["auc", "log_loss", "brier_score"]:
        print(f"  {key:<22}  {lr_metrics[key]:>22.4f}  {gbm_metrics[key]:>22.4f}")
    print(f"  {'calibration_mae':<22}  {lr_cal_mae:>22.4f}  {gbm_cal_mae:>22.4f}")


def print_rate_response_check(check) -> None:
    status = "pass" if check.is_directionally_sane else "review"
    print(f"  Rate response sanity check: {status}")
    print(f"    expected_direction={check.expected_direction}")
    print(f"    violations={check.violations}")
    for row in check.rate_response.itertuples(index=False):
        print(f"    rate={row.offered_rate:.2%}, mean_pred={row.mean_predicted_probability:.3f}")


if __name__ == "__main__":
    main()
