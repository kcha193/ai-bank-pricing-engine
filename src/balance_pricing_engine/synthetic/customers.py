"""Synthetic customer generation for home loans and term deposits."""

from __future__ import annotations

import numpy as np
import pandas as pd


def generate_loan_customers(n_customers: int, rng: np.random.Generator) -> pd.DataFrame:
    """Generate synthetic home loan applicants with risk and balance attributes."""
    credit_score = np.clip(rng.normal(710, 70, n_customers), 500, 850)
    lvr = np.clip(rng.beta(5, 2, n_customers), 0.45, 0.98)
    loan_amount = np.clip(rng.lognormal(mean=np.log(550_000), sigma=0.35, size=n_customers), 150_000, 1_500_000)
    income = np.clip(rng.lognormal(mean=np.log(145_000), sigma=0.4, size=n_customers), 45_000, 500_000)

    risk_score = (
        0.55 * (1 - (credit_score - 500) / 350)
        + 0.35 * ((lvr - 0.45) / 0.53)
        + 0.10 * np.clip(loan_amount / np.maximum(income, 1) / 6, 0, 1)
    )
    expected_loss_rate = 0.0015 + 0.018 * np.clip(risk_score, 0, 1) ** 1.4

    return pd.DataFrame(
        {
            "customer_id": [f"L{i:06d}" for i in range(n_customers)],
            "product": "home_loan",
            "loan_amount": loan_amount,
            "credit_score": credit_score,
            "lvr": lvr,
            "income": income,
            "risk_score": np.clip(risk_score, 0, 1),
            "expected_loss_rate": expected_loss_rate,
        }
    )


def generate_deposit_customers(n_customers: int, rng: np.random.Generator) -> pd.DataFrame:
    """Generate synthetic term deposit prospects with balance and price sensitivity attributes."""
    deposit_amount = np.clip(rng.lognormal(mean=np.log(85_000), sigma=0.75, size=n_customers), 5_000, 1_000_000)
    relationship_years = np.clip(rng.gamma(shape=2.5, scale=2.0, size=n_customers), 0, 35)
    digital_engagement = rng.beta(2.0, 2.5, n_customers)
    rate_sensitivity = np.clip(rng.normal(0.55, 0.18, n_customers), 0.05, 0.95)

    return pd.DataFrame(
        {
            "customer_id": [f"D{i:06d}" for i in range(n_customers)],
            "product": "term_deposit",
            "deposit_amount": deposit_amount,
            "relationship_years": relationship_years,
            "digital_engagement": digital_engagement,
            "rate_sensitivity": rate_sensitivity,
        }
    )

