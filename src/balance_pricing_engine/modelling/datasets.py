"""Build observed response modelling datasets from synthetic ground-truth behaviour."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)


LOAN_TARGET_COLUMN = "accepted"
DEPOSIT_TARGET_COLUMN = "opened"

LOAN_FEATURE_COLUMNS = [
    "offered_rate",
    "competitor_rate",
    "rate_gap_to_competitor",
    "loan_amount",
    "credit_score",
    "lvr",
    "income",
    "risk_score",
    "expected_loss_rate",
    "risk_segment",
]

DEPOSIT_FEATURE_COLUMNS = [
    "offered_rate",
    "competitor_rate",
    "rate_gap_to_competitor",
    "deposit_amount",
    "relationship_years",
    "digital_engagement",
    "rate_sensitivity",
    "wealth_segment",
]


def build_loan_response_dataset(
    customers: pd.DataFrame,
    offered_rates: Sequence[float] | np.ndarray,
    competitor_rate: float,
    rate_elasticity: float,
    competitor_gap_elasticity: float,
    rng: np.random.Generator,
    n_samples: int | None = None,
) -> pd.DataFrame:
    """Build a binary home loan acceptance dataset from synthetic ground truth."""
    dataset = _sample_customer_offers(
        customers,
        offered_rates,
        rng,
        n_samples,
    )
    dataset["competitor_rate"] = competitor_rate
    dataset["rate_gap_to_competitor"] = dataset["offered_rate"] - dataset["competitor_rate"]
    dataset["risk_segment"] = _risk_segment(dataset["risk_score"])

    probability = _loan_probability_by_row(
        dataset,
        competitor_rate,
        rate_elasticity,
        competitor_gap_elasticity,
    )
    dataset["ground_truth_probability"] = probability
    dataset[LOAN_TARGET_COLUMN] = rng.binomial(1, probability)
    return dataset[
        [
            "customer_id",
            "product",
            *LOAN_FEATURE_COLUMNS,
            "ground_truth_probability",
            LOAN_TARGET_COLUMN,
        ]
    ]


def build_deposit_response_dataset(
    customers: pd.DataFrame,
    offered_rates: Sequence[float] | np.ndarray,
    competitor_rate: float,
    rate_elasticity: float,
    competitor_gap_elasticity: float,
    rng: np.random.Generator,
    n_samples: int | None = None,
) -> pd.DataFrame:
    """Build a binary term deposit uptake dataset from synthetic ground truth."""
    dataset = _sample_customer_offers(
        customers,
        offered_rates,
        rng,
        n_samples,
    )
    dataset["competitor_rate"] = competitor_rate
    dataset["rate_gap_to_competitor"] = dataset["offered_rate"] - dataset["competitor_rate"]
    dataset["wealth_segment"] = _wealth_segment(dataset["deposit_amount"])

    probability = _deposit_probability_by_row(
        dataset,
        competitor_rate,
        rate_elasticity,
        competitor_gap_elasticity,
    )
    dataset["ground_truth_probability"] = probability
    dataset[DEPOSIT_TARGET_COLUMN] = rng.binomial(1, probability)
    return dataset[
        [
            "customer_id",
            "product",
            *DEPOSIT_FEATURE_COLUMNS,
            "ground_truth_probability",
            DEPOSIT_TARGET_COLUMN,
        ]
    ]


def _sample_customer_offers(
    customers: pd.DataFrame,
    offered_rates: Sequence[float] | np.ndarray,
    rng: np.random.Generator,
    n_samples: int | None,
) -> pd.DataFrame:
    if len(customers) == 0:
        raise ValueError("At least one customer is required.")

    rate_grid = np.asarray(offered_rates, dtype=float)
    if len(rate_grid) == 0:
        raise ValueError("At least one offered rate is required.")

    sample_size = len(customers) if n_samples is None else n_samples
    if sample_size <= 0:
        raise ValueError("n_samples must be positive.")

    sample_index = rng.integers(0, len(customers), sample_size)
    dataset = customers.iloc[sample_index].reset_index(drop=True).copy()
    dataset["offered_rate"] = rng.choice(rate_grid, sample_size)
    return dataset


def _loan_probability_by_row(
    dataset: pd.DataFrame,
    competitor_rate: float,
    rate_elasticity: float,
    competitor_gap_elasticity: float,
) -> np.ndarray:
    probability = np.zeros(len(dataset))
    for offered_rate in dataset["offered_rate"].unique():
        mask = dataset["offered_rate"] == offered_rate
        probability[mask.to_numpy()] = loan_acceptance_probability(
            dataset.loc[mask],
            float(offered_rate),
            competitor_rate,
            rate_elasticity,
            competitor_gap_elasticity,
        )
    return probability


def _deposit_probability_by_row(
    dataset: pd.DataFrame,
    competitor_rate: float,
    rate_elasticity: float,
    competitor_gap_elasticity: float,
) -> np.ndarray:
    probability = np.zeros(len(dataset))
    for offered_rate in dataset["offered_rate"].unique():
        mask = dataset["offered_rate"] == offered_rate
        probability[mask.to_numpy()] = deposit_uptake_probability(
            dataset.loc[mask],
            float(offered_rate),
            competitor_rate,
            rate_elasticity,
            competitor_gap_elasticity,
        )
    return probability


def _risk_segment(risk_score: pd.Series) -> pd.Series:
    return pd.cut(
        risk_score,
        bins=[-np.inf, 0.33, 0.66, np.inf],
        labels=[0, 1, 2],
    ).astype(int)


def _wealth_segment(deposit_amount: pd.Series) -> pd.Series:
    return pd.cut(
        deposit_amount,
        bins=[-np.inf, 50_000, 150_000, np.inf],
        labels=[0, 1, 2],
    ).astype(int)
