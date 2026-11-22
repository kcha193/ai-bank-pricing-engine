"""Reporting-only pre-steering economic ROE (FIN-003, Phase 4d).

The model ROE (``portfolio_return_on_equity``) divides profit before the capital charge by
required capital. Its numerator includes management steering penalties: the deposit
funding shortfall penalty and, in supply simulations, the concentration penalty. This
module reports a separate economic ROE that adds those penalties back. It is a disclosure
measure only. ``ROE_FLOOR_MET`` and ``min_return_on_equity`` keep using the model ROE;
changing that metric needs separate approval.

Basis: pre-tax and pre-operating-expense. Neither tax nor operating expense is modelled,
so neither is excluded silently. Genuine economic costs that the engine models (credit
loss, deposit interest, deposit acquisition cost, external wholesale funding) stay in the
numerator.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from balance_pricing_engine.pricing.profit import (
    TIER1_CAPITAL_RATIO,
    portfolio_return_on_equity,
)

REPORTING_BASIS = (
    "Pre-tax, pre-operating-expense synthetic economic contribution over required tier-1 "
    "capital. Reporting only: not used by ROE_FLOOR_MET or rate selection."
)
NOT_MODELLED = "not modelled; nothing excluded"

_REL_TOL = 1e-9
_ABS_TOL = 1e-6


@dataclass(frozen=True)
class EconomicROEBridge:
    """Bridge from economic profit to the model objective, with both ROE measures."""

    loan_interest_income: float
    expected_credit_loss: float
    deposit_interest_cost: float
    deposit_acquisition_cost: float
    external_wholesale_funding_cost: float
    surplus_investment_income: float
    funding_shortfall_penalty: float
    concentration_penalty: float
    capital_charge: float
    loan_volume: float
    rwa_weight: float
    tier1_capital_ratio: float = TIER1_CAPITAL_RATIO

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not math.isfinite(value):
                raise ValueError(f"ROE bridge {name} must be finite.")

    @property
    def economic_profit_before_capital(self) -> float:
        return math.fsum(
            (
                self.loan_interest_income,
                -self.expected_credit_loss,
                -self.deposit_interest_cost,
                -self.deposit_acquisition_cost,
                -self.external_wholesale_funding_cost,
                self.surplus_investment_income,
            )
        )

    @property
    def steering_penalties(self) -> float:
        return self.funding_shortfall_penalty + self.concentration_penalty

    @property
    def model_roe_numerator(self) -> float:
        return self.economic_profit_before_capital - self.steering_penalties

    @property
    def model_objective(self) -> float:
        return self.model_roe_numerator - self.capital_charge

    @property
    def required_capital(self) -> float:
        return self.loan_volume * self.rwa_weight * self.tier1_capital_ratio

    @property
    def model_return_on_equity(self) -> float:
        """The established model ROE, unchanged (0.0 at zero required capital)."""
        return float(
            portfolio_return_on_equity(
                self.loan_interest_income,
                self.expected_credit_loss,
                self.deposit_interest_cost,
                self.external_wholesale_funding_cost,
                self.funding_shortfall_penalty,
                self.surplus_investment_income,
                self.loan_volume,
                self.rwa_weight,
                self.tier1_capital_ratio,
                acquisition_cost=self.deposit_acquisition_cost,
                concentration_penalty=self.concentration_penalty,
            )
        )

    @property
    def economic_return_on_equity(self) -> float | None:
        """``None`` (not available) when no capital is required, never an invented 0%."""
        capital = self.required_capital
        if not capital > 0:
            return None
        return self.economic_profit_before_capital / capital

    @property
    def steering_roe_difference(self) -> float | None:
        economic = self.economic_return_on_equity
        return None if economic is None else economic - self.model_return_on_equity

    def to_dict(self) -> dict[str, Any]:
        return {
            "reporting_basis": REPORTING_BASIS,
            "numerator_bridge": {
                "loan_interest_income": self.loan_interest_income,
                "expected_credit_loss": -self.expected_credit_loss,
                "deposit_interest_cost": -self.deposit_interest_cost,
                "deposit_acquisition_cost": -self.deposit_acquisition_cost,
                "external_wholesale_funding_cost": -self.external_wholesale_funding_cost,
                "surplus_investment_income": self.surplus_investment_income,
                "economic_profit_before_capital": self.economic_profit_before_capital,
                "funding_shortfall_penalty": -self.funding_shortfall_penalty,
                "concentration_penalty": -self.concentration_penalty,
                "model_roe_numerator": self.model_roe_numerator,
                "capital_charge": -self.capital_charge,
                "model_objective": self.model_objective,
            },
            "capital_denominator": {
                "loan_volume": self.loan_volume,
                "rwa_weight": self.rwa_weight,
                "tier1_capital_ratio": self.tier1_capital_ratio,
                "required_capital": self.required_capital,
            },
            "model_return_on_equity": self.model_return_on_equity,
            "economic_return_on_equity": self.economic_return_on_equity,
            "steering_penalty_difference": self.steering_penalties,
            "steering_roe_difference": self.steering_roe_difference,
            "excluded_operating_expenses": NOT_MODELLED,
            "excluded_tax": NOT_MODELLED,
        }


def economic_roe_bridge(
    result: Any, *, rwa_weight: float, tier1_capital_ratio: float = TIER1_CAPITAL_RATIO
) -> EconomicROEBridge:
    """Build the bridge for any pricing result and check it reproduces the objective."""
    bridge = EconomicROEBridge(
        loan_interest_income=float(result.loan_interest_income),
        expected_credit_loss=float(result.expected_credit_loss),
        deposit_interest_cost=float(result.deposit_interest_cost),
        deposit_acquisition_cost=float(getattr(result, "deposit_acquisition_cost", 0.0)),
        external_wholesale_funding_cost=float(result.wholesale_funding_cost),
        surplus_investment_income=float(result.excess_deposit_income),
        funding_shortfall_penalty=float(result.funding_shortfall_penalty),
        concentration_penalty=float(getattr(result, "deposit_concentration_penalty", 0.0)),
        capital_charge=float(result.capital_charge),
        loan_volume=float(result.expected_loan_volume),
        rwa_weight=rwa_weight,
        tier1_capital_ratio=tier1_capital_ratio,
    )
    if not math.isclose(
        bridge.model_objective, float(result.total_objective), rel_tol=_REL_TOL, abs_tol=_ABS_TOL
    ):
        raise ValueError(
            f"ROE bridge objective {bridge.model_objective!r} does not reconcile to the "
            f"result objective {result.total_objective!r}."
        )
    return bridge
