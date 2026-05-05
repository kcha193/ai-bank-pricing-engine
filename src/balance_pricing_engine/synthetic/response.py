"""Ground-truth customer response simulation.

These functions are deliberately explicit. The known elasticities let later ML work check whether
trained response models recover sensible pricing behaviour.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from balance_pricing_engine.config import (
    DepositResponseCoefficientsConfig,
    LoanResponseCoefficientsConfig,
)


def sigmoid(value: np.ndarray) -> np.ndarray:
    """Numerically stable logistic transform."""
    return 1 / (1 + np.exp(-np.clip(value, -40, 40)))


def loan_acceptance_probability(
    customers: pd.DataFrame,
    offered_rate: float,
    competitor_rate: float,
    rate_elasticity: float,
    competitor_gap_elasticity: float,
    coefficients: LoanResponseCoefficientsConfig | None = None,
) -> np.ndarray:
    """Estimate true home loan acceptance probability for an offered rate."""
    coefficients = coefficients or LoanResponseCoefficientsConfig()
    competitor_gap = offered_rate - competitor_rate
    utility = (
        coefficients.intercept
        + rate_elasticity * (offered_rate - coefficients.rate_anchor)
        + competitor_gap_elasticity * competitor_gap
        + coefficients.risk_score_coef * customers["risk_score"].to_numpy()
        + coefficients.credit_score_coef * ((customers["credit_score"].to_numpy() - 700) / 100)
        + coefficients.lvr_coef * ((customers["lvr"].to_numpy() - 0.75) / 0.10)
    )
    return sigmoid(utility)


def deposit_uptake_probability(
    customers: pd.DataFrame,
    offered_rate: float,
    competitor_rate: float,
    rate_elasticity: float,
    competitor_gap_elasticity: float,
    coefficients: DepositResponseCoefficientsConfig | None = None,
) -> np.ndarray:
    """Estimate true term deposit uptake probability for an offered rate."""
    coefficients = coefficients or DepositResponseCoefficientsConfig()
    competitor_gap = offered_rate - competitor_rate
    utility = (
        coefficients.intercept
        + rate_elasticity * (offered_rate - coefficients.rate_anchor)
        + competitor_gap_elasticity * competitor_gap
        + coefficients.rate_sensitivity_coef * customers["rate_sensitivity"].to_numpy()
        + coefficients.relationship_coef * np.minimum(
            customers["relationship_years"].to_numpy(),
            15,
        )
        + coefficients.digital_coef * customers["digital_engagement"].to_numpy()
    )
    return sigmoid(utility)
