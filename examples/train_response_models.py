"""Train ML response models against synthetic ground-truth customer behaviour."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

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
from balance_pricing_engine.modelling.agentic import (
    ElasticityBaselineModel,
    HistGBMResponseModel,
    LogisticResponseModel,
    ModelTrainingAgent,
    TrainingConfig,
)
from balance_pricing_engine.modelling.evaluation import (
    check_rate_response_direction,
    evaluate_binary_classifier,
    evaluate_calibration,
)
from balance_pricing_engine.modelling.models import (
    build_hist_gradient_boosting_model,
    build_logistic_regression_model,
    fit_response_model,
)
from balance_pricing_engine.modelling.reports import build_champion_challenger_report
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

    lr_check = check_rate_response_direction(
        lr_model, test.head(1_000), feature_columns, offered_rates, competitor_rate,
        expected_direction,
    )
    gbm_check = check_rate_response_direction(
        gbm_model, test.head(1_000), feature_columns, offered_rates, competitor_rate,
        expected_direction,
    )

    report = build_champion_challenger_report(
        champion_name="logistic_regression",
        challenger_name="hist_gradient_boosting",
        champion_metrics=lr_metrics,
        challenger_metrics=gbm_metrics,
        champion_cal_mae=lr_cal_mae,
        challenger_cal_mae=gbm_cal_mae,
        champion_rate_check_pass=lr_check.is_directionally_sane,
        challenger_rate_check_pass=gbm_check.is_directionally_sane,
    )

    print(title)
    print_champion_challenger_report(report)
    _print_governed_candidate_table(
        dataset=dataset,
        feature_columns=feature_columns,
        target_column=target_column,
        random_seed=random_seed,
    )
    print("  Logistic regression rate response")
    print_rate_response_check(lr_check)
    print("  Hist gradient boosting rate response")
    print_rate_response_check(gbm_check)


def print_champion_challenger_report(report: dict) -> None:
    champion = report["champion_name"]
    challenger = report["challenger_name"]
    header = f"  {'metric':<22}  {champion:>24}  {challenger:>24}"
    print(header)
    for row in report["rows"]:
        champ_val = row[champion]
        chall_val = row[challenger]
        if isinstance(champ_val, float):
            print(f"  {row['metric']:<22}  {champ_val:>24.4f}  {chall_val:>24.4f}")
        else:
            print(f"  {row['metric']:<22}  {champ_val:>24}  {chall_val:>24}")
    print(f"  Recommendation: {report['recommendation']}")
    print(f"    reason: {report['reason']}")


def _print_governed_candidate_table(
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
    random_seed: int,
) -> None:
    candidates = {
        "elasticity_baseline": ElasticityBaselineModel(),
        "logistic": LogisticResponseModel(random_state=random_seed),
        "histgbm": HistGBMResponseModel(random_state=random_seed),
    }
    with TemporaryDirectory(prefix="bank-pricing-agentic-") as artifacts_dir:
        agent = ModelTrainingAgent(
            config=TrainingConfig(
                feature_columns=feature_columns,
                target_column=target_column,
                random_seed=random_seed,
            ),
            artifacts_dir=artifacts_dir,
        )
        result = agent.train(
            dataset=dataset,
            candidates=candidates,
            registry=agent.registry,
            run_name="example_response_models",
        )

    print("  Governed agentic workflow")
    print(f"  {'model_name':<30}  {'auc':>8}  {'log_loss':>8}  {'cal_mae':>8}")
    for candidate in result.report["candidates"]:
        metrics = candidate["metrics"]
        print(
            f"  {candidate['model_name']:<30}  "
            f"{metrics['auc']:>8.4f}  "
            f"{metrics['log_loss']:>8.4f}  "
            f"{candidate['calibration_error']:>8.4f}"
        )


def print_rate_response_check(check) -> None:
    status = "pass" if check.is_directionally_sane else "review"
    print(f"  Rate response sanity check: {status}")
    print(f"    expected_direction={check.expected_direction}")
    print(f"    violations={check.violations}")
    for row in check.rate_response.itertuples(index=False):
        print(f"    rate={row.offered_rate:.2%}, mean_pred={row.mean_predicted_probability:.3f}")


if __name__ == "__main__":
    main()
