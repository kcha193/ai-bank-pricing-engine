"""Transparent joint grid-search optimizer."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from balance_pricing_engine.grid_utils import build_inclusive_numeric_grid
from balance_pricing_engine.market import (
    MarketRates,
    SpreadGridConfig,
    build_market_relative_grid,
)
from balance_pricing_engine.config import ResponseCoefficientsConfig
from balance_pricing_engine.optimization.response_fn import ResponseFn
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_capital_charge,
    expected_credit_loss,
    expected_deposit_cost,
    expected_deposit_volume,
    expected_excess_deposit_income,
    expected_loan_interest_income,
    expected_loan_volume,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)


@dataclass(frozen=True)
class OptimizationResult:
    loan_rate: float
    deposit_rate: float
    loan_interest_income: float
    expected_credit_loss: float
    deposit_interest_cost: float
    wholesale_funding_cost: float
    excess_deposit_income: float
    funding_shortfall_penalty: float
    total_objective: float
    expected_loan_volume: float
    expected_deposit_volume: float
    funding_shortfall: float
    home_loan_spread_to_market_bps: float = float("nan")
    term_deposit_spread_to_market_bps: float = float("nan")
    capital_charge: float = 0.0

    @property
    def objective_value(self) -> float:
        """Backward-compatible alias for the total objective."""
        return self.total_objective

    @property
    def loan_income_after_losses(self) -> float:
        """Loan interest income after expected credit loss."""
        return self.loan_interest_income - self.expected_credit_loss

    @property
    def deposit_cost(self) -> float:
        """Backward-compatible alias for deposit interest cost."""
        return self.deposit_interest_cost

    @property
    def funding_penalty(self) -> float:
        """Backward-compatible alias for funding shortfall penalty."""
        return self.funding_shortfall_penalty


GRID_RESULT_COLUMNS = [
    "loan_rate",
    "deposit_rate",
    "home_loan_spread_to_market_bps",
    "term_deposit_spread_to_market_bps",
    "loan_interest_income",
    "expected_credit_loss",
    "capital_charge",
    "deposit_interest_cost",
    "wholesale_funding_cost",
    "excess_deposit_income",
    "funding_shortfall_penalty",
    "total_objective",
    "expected_loan_volume",
    "expected_deposit_volume",
    "funding_shortfall",
    "return_on_equity",
]


def build_rate_grid(start: float, stop: float, step: float) -> np.ndarray:
    """Build an inclusive decimal rate grid with stable rounding."""
    return build_inclusive_numeric_grid(start, stop, step)


def evaluate_joint_rate_grid(
    loan_customers: pd.DataFrame,
    deposit_customers: pd.DataFrame,
    market: MarketState,
    loan_rate_grid: np.ndarray,
    deposit_rate_grid: np.ndarray,
    loan_rate_elasticity: float,
    loan_competitor_gap_elasticity: float,
    deposit_rate_elasticity: float,
    deposit_competitor_gap_elasticity: float,
    required_deposit_funding_ratio: float,
    funding_shortfall_penalty_rate: float,
    rwa_weight: float = 0.35,
    capital_hurdle_rate: float = 0.12,
    home_loan_reference_rate: float | None = None,
    term_deposit_reference_rate: float | None = None,
    response_coefficients: ResponseCoefficientsConfig | None = None,
    loan_response_fn: ResponseFn | None = None,
    deposit_response_fn: ResponseFn | None = None,
) -> pd.DataFrame:
    """Evaluate every loan and deposit rate pair in the joint pricing grid."""
    rows: list[dict[str, float]] = []
    deposit_precomputes: list[tuple[float, float, float, float]] = []
    response_coefficients = response_coefficients or ResponseCoefficientsConfig()

    for deposit_rate in deposit_rate_grid:
        if deposit_response_fn is not None:
            deposit_probs = deposit_response_fn(
                deposit_customers,
                float(deposit_rate),
                market.competitor_deposit_rate,
            )
        else:
            deposit_probs = deposit_uptake_probability(
                deposit_customers,
                float(deposit_rate),
                market.competitor_deposit_rate,
                deposit_rate_elasticity,
                deposit_competitor_gap_elasticity,
                coefficients=response_coefficients.deposit,
            )
        deposit_volume = expected_deposit_volume(deposit_customers, deposit_probs)
        deposit_cost = expected_deposit_cost(
            deposit_customers,
            deposit_probs,
            float(deposit_rate),
        )
        term_deposit_spread_to_market_bps = _spread_to_market_bps(
            float(deposit_rate),
            term_deposit_reference_rate,
        )
        deposit_precomputes.append(
            (
                float(deposit_rate),
                deposit_volume,
                deposit_cost,
                term_deposit_spread_to_market_bps,
            )
        )

    for loan_rate in loan_rate_grid:
        if loan_response_fn is not None:
            loan_probs = loan_response_fn(
                loan_customers,
                float(loan_rate),
                market.competitor_loan_rate,
            )
        else:
            loan_probs = loan_acceptance_probability(
                loan_customers,
                float(loan_rate),
                market.competitor_loan_rate,
                loan_rate_elasticity,
                loan_competitor_gap_elasticity,
                coefficients=response_coefficients.loan,
            )
        loan_volume = expected_loan_volume(loan_customers, loan_probs)
        capital_charge = expected_capital_charge(
            loan_volume,
            rwa_weight,
            capital_hurdle_rate,
        )
        loan_interest_income = expected_loan_interest_income(
            loan_customers,
            loan_probs,
            float(loan_rate),
        )
        credit_loss = expected_credit_loss(loan_customers, loan_probs)
        home_loan_spread_to_market_bps = _spread_to_market_bps(
            float(loan_rate),
            home_loan_reference_rate,
        )

        for (
            deposit_rate,
            deposit_volume,
            deposit_cost,
            term_deposit_spread_to_market_bps,
        ) in deposit_precomputes:
            wholesale_funding_cost = expected_wholesale_funding_cost(
                loan_volume,
                deposit_volume,
                market.wholesale_funding_rate,
            )
            excess_deposit_income = expected_excess_deposit_income(
                loan_volume,
                deposit_volume,
                market.cash_rate,
            )
            funding = calculate_funding_result(
                loan_volume,
                deposit_volume,
                required_deposit_funding_ratio,
                funding_shortfall_penalty_rate,
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
            tier1_capital_ratio = 0.10
            required_capital = loan_volume * rwa_weight * tier1_capital_ratio
            return_on_equity = (
                (loan_interest_income - credit_loss - deposit_cost - wholesale_funding_cost)
                / required_capital
                if required_capital > 0
                else 0.0
            )

            rows.append(
                {
                    "loan_rate": float(loan_rate),
                    "deposit_rate": deposit_rate,
                    "home_loan_spread_to_market_bps": home_loan_spread_to_market_bps,
                    "term_deposit_spread_to_market_bps": term_deposit_spread_to_market_bps,
                    "loan_interest_income": loan_interest_income,
                    "expected_credit_loss": credit_loss,
                    "capital_charge": capital_charge,
                    "deposit_interest_cost": deposit_cost,
                    "wholesale_funding_cost": wholesale_funding_cost,
                    "excess_deposit_income": excess_deposit_income,
                    "funding_shortfall_penalty": funding.funding_penalty,
                    "total_objective": total_objective,
                    "expected_loan_volume": loan_volume,
                    "expected_deposit_volume": deposit_volume,
                    "funding_shortfall": funding.funding_shortfall,
                    "return_on_equity": return_on_equity,
                }
            )

    if not rows:
        raise ValueError("At least one loan rate and one deposit rate are required.")
    return pd.DataFrame(rows, columns=GRID_RESULT_COLUMNS)


def optimization_result_from_row(row: pd.Series) -> OptimizationResult:
    """Convert a grid result row into the public optimization result dataclass."""
    return OptimizationResult(
        loan_rate=float(row["loan_rate"]),
        deposit_rate=float(row["deposit_rate"]),
        home_loan_spread_to_market_bps=float(row["home_loan_spread_to_market_bps"]),
        term_deposit_spread_to_market_bps=float(row["term_deposit_spread_to_market_bps"]),
        loan_interest_income=float(row["loan_interest_income"]),
        expected_credit_loss=float(row["expected_credit_loss"]),
        capital_charge=float(row["capital_charge"]),
        deposit_interest_cost=float(row["deposit_interest_cost"]),
        wholesale_funding_cost=float(row["wholesale_funding_cost"]),
        excess_deposit_income=float(row["excess_deposit_income"]),
        funding_shortfall_penalty=float(row["funding_shortfall_penalty"]),
        total_objective=float(row["total_objective"]),
        expected_loan_volume=float(row["expected_loan_volume"]),
        expected_deposit_volume=float(row["expected_deposit_volume"]),
        funding_shortfall=float(row["funding_shortfall"]),
    )


def select_best_result(
    grid_results: pd.DataFrame,
    min_return_on_equity: float | None = None,
) -> OptimizationResult:
    """Select the highest-objective pricing result from evaluated grid results.

    When min_return_on_equity is set, prefer rows meeting the floor; fall back
    to unconstrained best if no row qualifies (avoids hard infeasibility).
    """
    if grid_results.empty:
        raise ValueError("At least one loan rate and one deposit rate are required.")

    candidates = grid_results
    if min_return_on_equity is not None and "return_on_equity" in grid_results.columns:
        return_on_equity = grid_results["return_on_equity"]
        if return_on_equity.notna().any():
            qualified = grid_results[return_on_equity >= min_return_on_equity]
            if not qualified.empty:
                candidates = qualified

    market_spread_distance = (
        candidates["home_loan_spread_to_market_bps"].abs()
        + candidates["term_deposit_spread_to_market_bps"].abs()
    )
    best_row = (
        candidates.assign(_market_spread_distance=market_spread_distance)
        .sort_values(
            by=["total_objective", "funding_shortfall", "_market_spread_distance"],
            ascending=[False, True, True],
            kind="mergesort",
        )
        .iloc[0]
    )
    return optimization_result_from_row(best_row)


def optimize_joint_rates(
    loan_customers: pd.DataFrame,
    deposit_customers: pd.DataFrame,
    market: MarketState,
    loan_rate_grid: np.ndarray,
    deposit_rate_grid: np.ndarray,
    loan_rate_elasticity: float,
    loan_competitor_gap_elasticity: float,
    deposit_rate_elasticity: float,
    deposit_competitor_gap_elasticity: float,
    required_deposit_funding_ratio: float,
    funding_shortfall_penalty_rate: float,
    rwa_weight: float = 0.35,
    capital_hurdle_rate: float = 0.12,
    home_loan_reference_rate: float | None = None,
    term_deposit_reference_rate: float | None = None,
    response_coefficients: ResponseCoefficientsConfig | None = None,
    loan_response_fn: ResponseFn | None = None,
    deposit_response_fn: ResponseFn | None = None,
) -> OptimizationResult:
    """Optimize portfolio-level loan and deposit rates by exhaustive grid search."""
    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=loan_rate_grid,
        deposit_rate_grid=deposit_rate_grid,
        loan_rate_elasticity=loan_rate_elasticity,
        loan_competitor_gap_elasticity=loan_competitor_gap_elasticity,
        deposit_rate_elasticity=deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=funding_shortfall_penalty_rate,
        rwa_weight=rwa_weight,
        capital_hurdle_rate=capital_hurdle_rate,
        home_loan_reference_rate=home_loan_reference_rate,
        term_deposit_reference_rate=term_deposit_reference_rate,
        response_coefficients=response_coefficients,
        loan_response_fn=loan_response_fn,
        deposit_response_fn=deposit_response_fn,
    )
    return select_best_result(grid_results)


def optimize_joint_market_relative_rates(
    loan_customers: pd.DataFrame,
    deposit_customers: pd.DataFrame,
    market: MarketState,
    market_rates: MarketRates,
    home_loan_spread_bps: SpreadGridConfig,
    term_deposit_spread_bps: SpreadGridConfig,
    loan_rate_elasticity: float,
    loan_competitor_gap_elasticity: float,
    deposit_rate_elasticity: float,
    deposit_competitor_gap_elasticity: float,
    required_deposit_funding_ratio: float,
    funding_shortfall_penalty_rate: float,
    rwa_weight: float = 0.35,
    capital_hurdle_rate: float = 0.12,
    response_coefficients: ResponseCoefficientsConfig | None = None,
    loan_response_fn: ResponseFn | None = None,
    deposit_response_fn: ResponseFn | None = None,
) -> OptimizationResult:
    """Optimize rates over spreads to reference market rates."""
    loan_rate_grid, deposit_rate_grid = build_market_relative_grid(
        market_rates=market_rates,
        home_loan_spread_bps=home_loan_spread_bps,
        term_deposit_spread_bps=term_deposit_spread_bps,
    )
    return optimize_joint_rates(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=loan_rate_grid,
        deposit_rate_grid=deposit_rate_grid,
        loan_rate_elasticity=loan_rate_elasticity,
        loan_competitor_gap_elasticity=loan_competitor_gap_elasticity,
        deposit_rate_elasticity=deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=funding_shortfall_penalty_rate,
        rwa_weight=rwa_weight,
        capital_hurdle_rate=capital_hurdle_rate,
        home_loan_reference_rate=market_rates.home_loan_reference_rate,
        term_deposit_reference_rate=market_rates.term_deposit_reference_rate,
        response_coefficients=response_coefficients,
        loan_response_fn=loan_response_fn,
        deposit_response_fn=deposit_response_fn,
    )


def _spread_to_market_bps(rate: float, reference_rate: float | None) -> float:
    if reference_rate is None:
        return float("nan")
    return round((rate - reference_rate) * 10_000, 6)
