"""Cohort accounting for the multi-period scenario roll (FIN-002, Phase 3b).

A *booked cohort* is the expected loan and deposit book originated in one period at that
period's chosen rates. It is a historical contractual portfolio, not a new pricing decision:
in later periods it keeps its booked loan rate, deposit rate, expected-loss rate and capital
basis, and only its balances run off.

Conventions (simplified annual accrual; see ``docs/modelling_assumptions.md``):

- A cohort originated in period ``c`` contributes its full economics in period ``c``.
  At period ``t >= c`` its balances are the originals times ``(1 - runoff) ** (t - c)``,
  with independent loan and deposit runoff.
- Cohort-level terms on surviving balances: interest at the booked rate, ECL at the booked
  loss rate (``booked ECL x loan survival``), capital via ``expected_capital_charge`` with the
  booked effective RWA weight and hurdle, deposit interest at the booked deposit rate.
- Funding terms (wholesale funding, excess-deposit income, shortfall penalty) depend on the
  whole active book, so they are calculated once on the aggregate loan and deposit balances
  at the period's market, with the canonical single-period functions.

This is a synthetic scenario projection, not a contractual cash-flow, amortisation or IFRS 9
model.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from balance_pricing_engine.config import BalanceSheetConfig
from balance_pricing_engine.optimization.grid_search import OptimizationResult
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.credit_loss import CreditLossProfile
from balance_pricing_engine.pricing.profit import (
    expected_capital_charge,
    expected_excess_deposit_income,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.synthetic.market import MarketState

# A booked result must reconcile to its cohort terms within floating-point accumulation
# error. Material mismatches mean the cohort is not traceable to its contractual rates.
_RECONCILIATION_REL_TOL = 1e-9
_RECONCILIATION_ABS_TOL = 1e-6


def reconciles(actual: float, expected: float) -> bool:
    """Return whether two money amounts agree within the reconciliation tolerance."""
    return math.isclose(
        actual, expected, rel_tol=_RECONCILIATION_REL_TOL, abs_tol=_RECONCILIATION_ABS_TOL
    )


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}.")


def _require_non_negative(name: str, value: float) -> None:
    _require_finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value!r}.")


@dataclass(frozen=True)
class BookedCohort:
    """The expected book originated in one period, at its contractual (booked) terms.

    Interest income, deposit cost and capital are derived from the canonical fields
    (rate x balance, ``expected_capital_charge``) rather than stored, so they cannot drift
    from the booked rates. ECL is stored because its loss rate varies by customer.
    """

    origination_period: int
    loan_rate: float
    deposit_rate: float
    original_loan_volume: float
    original_deposit_volume: float
    booked_credit_loss: float
    loan_rwa_weight: float
    capital_hurdle_rate: float
    credit_loss_profile: CreditLossProfile | None = None

    def __post_init__(self) -> None:
        if self.origination_period < 0:
            raise ValueError(
                f"origination_period must be non-negative, got {self.origination_period!r}."
            )
        _require_finite("loan_rate", self.loan_rate)
        _require_finite("deposit_rate", self.deposit_rate)
        _require_non_negative("original_loan_volume", self.original_loan_volume)
        _require_non_negative("original_deposit_volume", self.original_deposit_volume)
        _require_non_negative("booked_credit_loss", self.booked_credit_loss)
        _require_non_negative("loan_rwa_weight", self.loan_rwa_weight)
        _require_non_negative("capital_hurdle_rate", self.capital_hurdle_rate)
        if self.original_loan_volume == 0 and self.booked_credit_loss != 0:
            raise ValueError("booked_credit_loss must be zero when no loans were booked.")

    @classmethod
    def from_result(
        cls,
        result: OptimizationResult,
        *,
        origination_period: int,
        loan_rwa_weight: float,
        capital_hurdle_rate: float,
        credit_loss_profile: CreditLossProfile | None = None,
    ) -> "BookedCohort":
        """Book the winning single-period result, checking it is traceable to its rates."""
        for name in (
            "loan_rate",
            "deposit_rate",
            "loan_interest_income",
            "capital_charge",
            "deposit_interest_cost",
        ):
            _require_finite(name, getattr(result, name))
        cohort = cls(
            origination_period=origination_period,
            loan_rate=result.loan_rate,
            deposit_rate=result.deposit_rate,
            original_loan_volume=result.expected_loan_volume,
            original_deposit_volume=result.expected_deposit_volume,
            booked_credit_loss=result.expected_credit_loss,
            loan_rwa_weight=loan_rwa_weight,
            capital_hurdle_rate=capital_hurdle_rate,
            credit_loss_profile=credit_loss_profile,
        )
        checks = (
            (
                "loan interest income",
                result.loan_interest_income,
                cohort.booked_loan_interest_income,
            ),
            (
                "deposit interest cost",
                result.deposit_interest_cost,
                cohort.booked_deposit_interest_cost,
            ),
            ("capital charge", result.capital_charge, cohort.booked_capital_charge),
        )
        for label, reported, derived in checks:
            if not reconciles(reported, derived):
                raise ValueError(
                    f"Period {origination_period} {label} {reported!r} does not reconcile to "
                    f"its booked rate and balance ({derived!r}); the cohort is not traceable."
                )
        return cohort

    @property
    def booked_loan_interest_income(self) -> float:
        return self.original_loan_volume * self.loan_rate

    @property
    def booked_deposit_interest_cost(self) -> float:
        return self.original_deposit_volume * self.deposit_rate

    @property
    def booked_capital_charge(self) -> float:
        return expected_capital_charge(
            self.original_loan_volume, self.loan_rwa_weight, self.capital_hurdle_rate
        )


def survival_factor(runoff_rate: float, periods_elapsed: int) -> float:
    """Fraction of a balance still outstanding after ``periods_elapsed`` annual runoffs."""
    if not math.isfinite(runoff_rate) or not 0.0 <= runoff_rate <= 1.0:
        raise ValueError(f"runoff rate must be within [0, 1], got {runoff_rate!r}.")
    if periods_elapsed < 0:
        raise ValueError(f"periods_elapsed must be non-negative, got {periods_elapsed!r}.")
    return (1.0 - runoff_rate) ** periods_elapsed


@dataclass(frozen=True)
class ActiveCohort:
    """One booked cohort's surviving position in a given period."""

    cohort: BookedCohort
    period: int
    loan_survival: float
    deposit_survival: float

    @property
    def loan_volume(self) -> float:
        return self.cohort.original_loan_volume * self.loan_survival

    @property
    def deposit_volume(self) -> float:
        return self.cohort.original_deposit_volume * self.deposit_survival

    @property
    def loan_interest_income(self) -> float:
        """Surviving balance at the booked contractual loan rate."""
        return self.loan_volume * self.cohort.loan_rate

    @property
    def credit_loss(self) -> float:
        """Booked ECL carried on survivors (constant loss rate, not lifetime ECL)."""
        return self.cohort.booked_credit_loss * self.loan_survival

    @property
    def capital_charge(self) -> float:
        return expected_capital_charge(
            self.loan_volume, self.cohort.loan_rwa_weight, self.cohort.capital_hurdle_rate
        )

    @property
    def deposit_interest_cost(self) -> float:
        """Surviving deposits at the booked contractual deposit rate."""
        return self.deposit_volume * self.cohort.deposit_rate

    @property
    def is_run_off(self) -> bool:
        return self.loan_volume == 0.0 and self.deposit_volume == 0.0


def position_at(
    cohort: BookedCohort,
    period: int,
    loan_runoff_rate: float,
    deposit_runoff_rate: float,
) -> ActiveCohort:
    """Return ``cohort``'s surviving position at the start of ``period``."""
    elapsed = period - cohort.origination_period
    if elapsed < 0:
        raise ValueError(
            f"Cannot position a cohort originated in period {cohort.origination_period} "
            f"before its origination (period {period})."
        )
    return ActiveCohort(
        cohort=cohort,
        period=period,
        loan_survival=survival_factor(loan_runoff_rate, elapsed),
        deposit_survival=survival_factor(deposit_runoff_rate, elapsed),
    )


@dataclass(frozen=True)
class PortfolioEconomics:
    """One period's risk-adjusted contribution for a set of active cohorts."""

    loan_volume: float
    deposit_volume: float
    loan_interest_income: float
    expected_credit_loss: float
    capital_charge: float
    deposit_interest_cost: float
    wholesale_funding_cost: float
    excess_deposit_income: float
    funding_shortfall: float
    funding_shortfall_penalty: float
    total_objective: float


def evaluate_active_portfolio(
    active_cohorts: Sequence[ActiveCohort],
    market: MarketState,
    balance_sheet: BalanceSheetConfig,
) -> PortfolioEconomics:
    """Evaluate the full single-period objective for the whole active book.

    Cohort terms are summed; funding terms are computed once on the aggregate balances, in
    the same arithmetic order as ``grid_search.evaluate_joint_rate_grid``. One cohort at
    full survival reproduces its single-period objective.
    """
    loan_volume = sum(position.loan_volume for position in active_cohorts)
    deposit_volume = sum(position.deposit_volume for position in active_cohorts)
    loan_interest_income = sum(position.loan_interest_income for position in active_cohorts)
    credit_loss = sum(position.credit_loss for position in active_cohorts)
    capital_charge = sum(position.capital_charge for position in active_cohorts)
    deposit_cost = sum(position.deposit_interest_cost for position in active_cohorts)

    wholesale_funding_cost = expected_wholesale_funding_cost(
        loan_volume, deposit_volume, market.wholesale_funding_rate
    )
    excess_deposit_income = expected_excess_deposit_income(
        loan_volume, deposit_volume, market.surplus_reinvestment_rate
    )
    funding = calculate_funding_result(
        loan_volume,
        deposit_volume,
        balance_sheet.required_deposit_funding_ratio,
        balance_sheet.funding_shortfall_penalty_rate,
    )
    total_objective = (
        loan_interest_income
        - credit_loss
        - capital_charge
        - deposit_cost
        - wholesale_funding_cost
        - funding.funding_penalty
        + excess_deposit_income
    )
    economics = PortfolioEconomics(
        loan_volume=float(loan_volume),
        deposit_volume=float(deposit_volume),
        loan_interest_income=float(loan_interest_income),
        expected_credit_loss=float(credit_loss),
        capital_charge=float(capital_charge),
        deposit_interest_cost=float(deposit_cost),
        wholesale_funding_cost=float(wholesale_funding_cost),
        excess_deposit_income=float(excess_deposit_income),
        funding_shortfall=float(funding.funding_shortfall),
        funding_shortfall_penalty=float(funding.funding_penalty),
        total_objective=float(total_objective),
    )
    for name, value in vars(economics).items():
        _require_finite(name, value)
    return economics
