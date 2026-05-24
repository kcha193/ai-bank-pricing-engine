import numpy as np

from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_attrition_dataset,
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.utils.random import make_rng


def test_loan_response_dataset_returns_expected_columns():
    rng = make_rng(100)
    customers = generate_loan_customers(100, rng)

    dataset = build_loan_response_dataset(
        customers,
        offered_rates=np.array([0.06, 0.07]),
        competitor_rate=0.0675,
        rate_elasticity=-85.0,
        competitor_gap_elasticity=-65.0,
        rng=rng,
        n_samples=200,
    )

    expected_columns = [
        "customer_id",
        "product",
        *LOAN_FEATURE_COLUMNS,
        "ground_truth_probability",
        LOAN_TARGET_COLUMN,
    ]
    assert list(dataset.columns) == expected_columns
    assert dataset[LOAN_TARGET_COLUMN].isin([0, 1]).all()
    assert dataset["ground_truth_probability"].between(0, 1).all()


def test_deposit_response_dataset_returns_expected_columns():
    rng = make_rng(101)
    customers = generate_deposit_customers(100, rng)

    dataset = build_deposit_response_dataset(
        customers,
        offered_rates=np.array([0.04, 0.05]),
        competitor_rate=0.045,
        rate_elasticity=95.0,
        competitor_gap_elasticity=75.0,
        rng=rng,
        n_samples=200,
    )

    expected_columns = [
        "customer_id",
        "product",
        *DEPOSIT_FEATURE_COLUMNS,
        "ground_truth_probability",
        DEPOSIT_TARGET_COLUMN,
    ]
    assert list(dataset.columns) == expected_columns
    assert dataset[DEPOSIT_TARGET_COLUMN].isin([0, 1]).all()
    assert dataset["ground_truth_probability"].between(0, 1).all()


def test_build_attrition_dataset_returns_dataframe_with_required_columns():
    rng = np.random.default_rng(42)
    n = 200
    acceptance_probs = rng.uniform(0.2, 0.8, n)

    frame = build_attrition_dataset(
        acceptance_probabilities=acceptance_probs,
        n_rows=n,
        hold_period_months=12,
        rng=rng,
    )

    assert "duration" in frame.columns
    assert "event_observed" in frame.columns
    assert len(frame) == n
    assert frame["duration"].min() >= 1
    assert frame["event_observed"].isin([0, 1]).all()


def test_build_attrition_dataset_high_acceptance_implies_longer_duration():
    rng = np.random.default_rng(0)
    n = 1000
    high_probs = np.full(n, 0.9)
    low_probs = np.full(n, 0.1)

    high_frame = build_attrition_dataset(high_probs, n_rows=n, rng=rng)
    rng2 = np.random.default_rng(0)
    low_frame = build_attrition_dataset(low_probs, n_rows=n, rng=rng2)

    assert high_frame["duration"].mean() > low_frame["duration"].mean()
