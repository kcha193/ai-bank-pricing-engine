"""Train ML response models against synthetic ground-truth customer behaviour."""

from __future__ import annotations

from pathlib import Path

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
    RateResponseCheck,
    check_rate_response_direction,
    evaluate_binary_classifier,
    evaluate_calibration,
)
from balance_pricing_engine.modelling.models import (
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
        offered_rates=[float(rate) for rate in loan_grid],
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
        offered_rates=[float(rate) for rate in deposit_grid],
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
    model = fit_response_model(
        build_logistic_regression_model(random_seed),
        train,
        feature_columns,
        target_column,
    )

    metrics = evaluate_binary_classifier(model, test, feature_columns, target_column)
    calibration = evaluate_calibration(
        model,
        test,
        feature_columns,
        target_column,
        n_bins=5,
    )
    response_check = check_rate_response_direction(
        model,
        test.head(1_000),
        feature_columns,
        offered_rates,
        competitor_rate,
        expected_direction,
    )

    print(title)
    print("  Logistic regression metrics")
    for key, value in metrics.items():
        print(f"    {key}: {value:.4f}")
    print("  Calibration")
    for row in calibration.itertuples(index=False):
        print(
            f"    bin={int(row.bin)}, "
            f"mean_pred={row.mean_predicted_probability:.3f}, "
            f"observed={row.observed_rate:.3f}, "
            f"count={int(row.count)}"
        )
    print_rate_response_check(response_check)


def print_rate_response_check(check: RateResponseCheck) -> None:
    status = "pass" if check.is_directionally_sane else "review"
    print(f"  Rate response sanity check: {status}")
    print(f"    expected_direction={check.expected_direction}")
    print(f"    violations={check.violations}")
    for row in check.rate_response.itertuples(index=False):
        print(f"    rate={row.offered_rate:.2%}, mean_pred={row.mean_predicted_probability:.3f}")


if __name__ == "__main__":
    main()
