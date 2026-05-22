"""Response-function protocols and adapters for the optimizer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import pandas as pd

from balance_pricing_engine.config import (
    DepositResponseCoefficientsConfig,
    LoanResponseCoefficientsConfig,
)
from balance_pricing_engine.modelling.models import predict_response_probability
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)


class ResponseFn(Protocol):
    """Callable interface the optimizer uses to compute response probabilities."""

    def __call__(
        self,
        customers: pd.DataFrame,
        offered_rate: float,
        competitor_rate: float,
    ) -> np.ndarray: ...


class GovernanceError(ValueError):
    """Raised when a response function is used without human approval."""


def require_approved(governance_status: str, model_name: str) -> None:
    """Raise GovernanceError if governance_status is not 'approved'."""
    if governance_status != "approved":
        raise GovernanceError(
            f"Model '{model_name}' has governance_status='{governance_status}'; "
            "call registry.approve_model() before using this model in the optimizer."
        )


@dataclass(frozen=True)
class SyntheticLoanResponseFn:
    """Wraps the deterministic synthetic loan response function."""

    elasticity: float
    competitor_gap_elasticity: float
    coefficients: LoanResponseCoefficientsConfig | None = None

    def __call__(
        self,
        customers: pd.DataFrame,
        offered_rate: float,
        competitor_rate: float,
    ) -> np.ndarray:
        return loan_acceptance_probability(
            customers,
            offered_rate,
            competitor_rate,
            self.elasticity,
            self.competitor_gap_elasticity,
            coefficients=self.coefficients,
        )


@dataclass(frozen=True)
class SyntheticDepositResponseFn:
    """Wraps the deterministic synthetic deposit response function."""

    elasticity: float
    competitor_gap_elasticity: float
    coefficients: DepositResponseCoefficientsConfig | None = None

    def __call__(
        self,
        customers: pd.DataFrame,
        offered_rate: float,
        competitor_rate: float,
    ) -> np.ndarray:
        return deposit_uptake_probability(
            customers,
            offered_rate,
            competitor_rate,
            self.elasticity,
            self.competitor_gap_elasticity,
            coefficients=self.coefficients,
        )


@dataclass
class MLResponseFnAdapter:
    """Wraps a fitted ResponseModel for use inside the optimizer grid search."""

    model: Any  # ResponseModel or sklearn estimator/pipeline
    feature_columns: list[str]

    def __call__(
        self,
        customers: pd.DataFrame,
        offered_rate: float,
        competitor_rate: float,
    ) -> np.ndarray:
        df = customers.copy()
        df["offered_rate"] = offered_rate
        df["competitor_rate"] = competitor_rate
        df["rate_gap_to_competitor"] = offered_rate - competitor_rate
        if "risk_segment" in self.feature_columns and "risk_segment" not in df.columns:
            df["risk_segment"] = pd.cut(
                df["risk_score"],
                bins=[-np.inf, 0.33, 0.66, np.inf],
                labels=[0, 1, 2],
            ).astype(int)
        if "wealth_segment" in self.feature_columns and "wealth_segment" not in df.columns:
            amount_column = "deposit_amount" if "deposit_amount" in df.columns else "loan_amount"
            df["wealth_segment"] = pd.cut(
                df[amount_column],
                bins=[-np.inf, 50_000, 150_000, np.inf],
                labels=[0, 1, 2],
            ).astype(int)
        if hasattr(self.model, "predict_probability"):
            return self.model.predict_probability(df, self.feature_columns)
        return predict_response_probability(self.model, df, self.feature_columns)
