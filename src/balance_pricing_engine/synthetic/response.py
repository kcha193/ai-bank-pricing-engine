"""Ground-truth customer response simulation.

These functions are deliberately explicit. The known elasticities let later ML work check whether
trained response models recover sensible pricing behaviour.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sigmoid(value: np.ndarray) -> np.ndarray:
    """Numerically stable logistic transform."""
    return 1 / (1 + np.exp(-np.clip(value, -40, 40)))


def loan_acceptance_probability(
    customers: pd.DataFrame,
    offered_rate: float,
    competitor_rate: float,
    rate_elasticity: float,
    competitor_gap_elasticity: float,
) -> np.ndarray:
    """Estimate true home loan acceptance probability for an offered rate."""
    competitor_gap = offered_rate - competitor_rate
    utility = (
        1.10
        + rate_elasticity * (offered_rate - 0.065)
        + competitor_gap_elasticity * competitor_gap
        - 1.20 * customers["risk_score"].to_numpy()
        + 0.35 * ((customers["credit_score"].to_numpy() - 700) / 100)
        - 0.25 * ((customers["lvr"].to_numpy() - 0.75) / 0.10)
    )
    return sigmoid(utility)


def deposit_uptake_probability(
    customers: pd.DataFrame,
    offered_rate: float,
    competitor_rate: float,
    rate_elasticity: float,
    competitor_gap_elasticity: float,
) -> np.ndarray:
    """Estimate true term deposit uptake probability for an offered rate."""
    competitor_gap = offered_rate - competitor_rate
    utility = (
        -0.65
        + rate_elasticity * (offered_rate - 0.045)
        + competitor_gap_elasticity * competitor_gap
        + 0.60 * customers["rate_sensitivity"].to_numpy()
        + 0.04 * np.minimum(customers["relationship_years"].to_numpy(), 15)
        - 0.20 * customers["digital_engagement"].to_numpy()
    )
    return sigmoid(utility)

