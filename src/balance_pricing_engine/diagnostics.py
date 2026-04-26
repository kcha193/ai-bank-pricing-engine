"""Diagnostics for explaining and challenging pricing optimizer outputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

import numpy as np
import pandas as pd

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.optimization.grid_search import (
    OptimizationResult,
    evaluate_joint_rate_grid,
    select_best_result,
)
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.utils.random import make_rng


BOUNDARY_WARNING_CAUSES = (
    "search range too narrow",
    "weak elasticity",
    "objective over-rewarding margin",
    "funding penalty too weak or strong",
    "competitor effects too small",
)

SENSITIVITY_RESULT_COLUMNS = [
    "scenario_name",
    "best_loan_rate",
    "best_deposit_rate",
    "objective_value",
    "expected_loan_volume",
    "expected_deposit_volume",
    "funding_shortfall",
    "initial_boundary_warning_flag",
    "boundary_warning_flag",
    "expansion_rounds",
]


@dataclass(frozen=True)
class ObjectiveDecomposition:
    loan_interest_income: float
    expected_credit_loss: float
    deposit_interest_cost: float
    wholesale_funding_cost: float
    funding_shortfall_penalty: float
    total_objective: float
    expected_loan_volume: float
    expected_deposit_volume: float
    funding_shortfall: float

    @property
    def reconciled_total_objective(self) -> float:
        """Recalculate the objective from individual economic components."""
        return (
            self.loan_interest_income
            - self.expected_credit_loss
            - self.deposit_interest_cost
            - self.wholesale_funding_cost
            - self.funding_shortfall_penalty
        )


@dataclass(frozen=True)
class BoundaryDiagnostics:
    warnings: tuple[str, ...]

    @property
    def has_warning(self) -> bool:
        """Whether at least one winning rate sits on a grid boundary."""
        return bool(self.warnings)


@dataclass(frozen=True)
class SensitivityCase:
    name: str
    loan_rate_elasticity: float
    deposit_rate_elasticity: float
    funding_shortfall_penalty_rate: float
    wholesale_funding_rate: float
    competitor_loan_rate: float
    competitor_deposit_rate: float


@dataclass(frozen=True)
class AdaptiveGridResult:
    best_result: OptimizationResult
    grid_results: pd.DataFrame
    boundary_diagnostics: BoundaryDiagnostics
    initial_boundary_diagnostics: BoundaryDiagnostics
    expansion_rounds: int


def objective_decomposition_from_result(result: OptimizationResult) -> ObjectiveDecomposition:
    """Build an objective decomposition for the selected pricing result."""
    return ObjectiveDecomposition(
        loan_interest_income=result.loan_interest_income,
        expected_credit_loss=result.expected_credit_loss,
        deposit_interest_cost=result.deposit_interest_cost,
        wholesale_funding_cost=result.wholesale_funding_cost,
        funding_shortfall_penalty=result.funding_shortfall_penalty,
        total_objective=result.total_objective,
        expected_loan_volume=result.expected_loan_volume,
        expected_deposit_volume=result.expected_deposit_volume,
        funding_shortfall=result.funding_shortfall,
    )


def evaluate_boundary_diagnostics(
    result: OptimizationResult,
    loan_rate_grid: np.ndarray,
    deposit_rate_grid: np.ndarray,
) -> BoundaryDiagnostics:
    """Flag optimizer wins that land on the edge of either pricing grid."""
    warnings: list[str] = []
    loan_min = float(np.min(loan_rate_grid))
    loan_max = float(np.max(loan_rate_grid))
    deposit_min = float(np.min(deposit_rate_grid))
    deposit_max = float(np.max(deposit_rate_grid))

    warnings.extend(
        _rate_boundary_warnings(
            product_name="home loan",
            rate=result.loan_rate,
            minimum_rate=loan_min,
            maximum_rate=loan_max,
        )
    )
    warnings.extend(
        _rate_boundary_warnings(
            product_name="term deposit",
            rate=result.deposit_rate,
            minimum_rate=deposit_min,
            maximum_rate=deposit_max,
        )
    )
    return BoundaryDiagnostics(warnings=tuple(warnings))


def expand_rate_grids_for_boundary(
    result: OptimizationResult,
    loan_rate_grid: np.ndarray,
    deposit_rate_grid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, bool]:
    """Expand grids outward when the selected result lands on a boundary."""
    expanded_loan_grid, loan_expanded = _expand_grid_for_selected_rate(
        loan_rate_grid,
        result.loan_rate,
    )
    expanded_deposit_grid, deposit_expanded = _expand_grid_for_selected_rate(
        deposit_rate_grid,
        result.deposit_rate,
    )
    return expanded_loan_grid, expanded_deposit_grid, loan_expanded or deposit_expanded


def evaluate_adaptive_joint_rate_grid(
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
    home_loan_reference_rate: float | None = None,
    term_deposit_reference_rate: float | None = None,
    max_boundary_expansions: int = 2,
) -> AdaptiveGridResult:
    """Evaluate a joint grid, expanding boundaries before accepting edge optima."""
    initial_boundary_diagnostics: BoundaryDiagnostics | None = None
    expansion_rounds = 0

    while True:
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
            home_loan_reference_rate=home_loan_reference_rate,
            term_deposit_reference_rate=term_deposit_reference_rate,
        )
        best_result = select_best_result(grid_results)
        boundary_diagnostics = evaluate_boundary_diagnostics(
            best_result,
            loan_rate_grid,
            deposit_rate_grid,
        )
        if initial_boundary_diagnostics is None:
            initial_boundary_diagnostics = boundary_diagnostics
        if not boundary_diagnostics.has_warning or expansion_rounds >= max_boundary_expansions:
            break

        loan_rate_grid, deposit_rate_grid, expanded = expand_rate_grids_for_boundary(
            best_result,
            loan_rate_grid,
            deposit_rate_grid,
        )
        if not expanded:
            break
        expansion_rounds += 1

    return AdaptiveGridResult(
        best_result=best_result,
        grid_results=grid_results,
        boundary_diagnostics=boundary_diagnostics,
        initial_boundary_diagnostics=initial_boundary_diagnostics,
        expansion_rounds=expansion_rounds,
    )


def build_default_sensitivity_cases(config: EngineConfig) -> list[SensitivityCase]:
    """Create a small set of one-at-a-time economic sensitivity cases."""
    base_wholesale_rate = config.market.cash_rate + config.market.wholesale_funding_spread
    lower_wholesale_rate = max(base_wholesale_rate - 0.005, config.market.cash_rate)

    baseline = SensitivityCase(
        name="baseline",
        loan_rate_elasticity=config.response.loan_rate_elasticity,
        deposit_rate_elasticity=config.response.deposit_rate_elasticity,
        funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
        wholesale_funding_rate=base_wholesale_rate,
        competitor_loan_rate=config.market.competitor_loan_rate,
        competitor_deposit_rate=config.market.competitor_deposit_rate,
    )
    return [
        baseline,
        replace(
            baseline,
            name="loan_elasticity_weaker",
            loan_rate_elasticity=config.response.loan_rate_elasticity * 0.75,
        ),
        replace(
            baseline,
            name="loan_elasticity_stronger",
            loan_rate_elasticity=config.response.loan_rate_elasticity * 1.25,
        ),
        replace(
            baseline,
            name="deposit_elasticity_weaker",
            deposit_rate_elasticity=config.response.deposit_rate_elasticity * 0.75,
        ),
        replace(
            baseline,
            name="deposit_elasticity_stronger",
            deposit_rate_elasticity=config.response.deposit_rate_elasticity * 1.25,
        ),
        replace(
            baseline,
            name="funding_penalty_lower",
            funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate
            * 0.5,
        ),
        replace(
            baseline,
            name="funding_penalty_higher",
            funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate
            * 1.5,
        ),
        replace(
            baseline,
            name="wholesale_funding_rate_lower",
            wholesale_funding_rate=lower_wholesale_rate,
        ),
        replace(
            baseline,
            name="wholesale_funding_rate_higher",
            wholesale_funding_rate=base_wholesale_rate + 0.005,
        ),
        replace(
            baseline,
            name="competitor_loan_rate_lower",
            competitor_loan_rate=max(config.market.competitor_loan_rate - 0.005, 0.0),
        ),
        replace(
            baseline,
            name="competitor_loan_rate_higher",
            competitor_loan_rate=config.market.competitor_loan_rate + 0.005,
        ),
        replace(
            baseline,
            name="competitor_deposit_rate_lower",
            competitor_deposit_rate=max(config.market.competitor_deposit_rate - 0.005, 0.0),
        ),
        replace(
            baseline,
            name="competitor_deposit_rate_higher",
            competitor_deposit_rate=config.market.competitor_deposit_rate + 0.005,
        ),
        replace(
            baseline,
            name="loan_elasticity_stronger__funding_penalty_higher",
            loan_rate_elasticity=config.response.loan_rate_elasticity * 1.25,
            funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate
            * 1.5,
        ),
        replace(
            baseline,
            name="competitor_loan_rate_higher__wholesale_funding_rate_higher",
            wholesale_funding_rate=base_wholesale_rate + 0.005,
            competitor_loan_rate=config.market.competitor_loan_rate + 0.005,
        ),
        replace(
            baseline,
            name="competitor_deposit_rate_lower__funding_penalty_higher",
            funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate
            * 1.5,
            competitor_deposit_rate=max(config.market.competitor_deposit_rate - 0.005, 0.0),
        ),
    ]


def run_sensitivity_analysis(
    config: EngineConfig,
    cases: Sequence[SensitivityCase] | None = None,
    max_boundary_expansions: int = 2,
) -> pd.DataFrame:
    """Run one-at-a-time sensitivity cases against the same synthetic customers."""
    rng = make_rng(config.random_seed)
    loan_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    deposit_customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
    loan_grid, deposit_grid = build_configured_rate_grids(config)

    rows: list[dict[str, float | str | bool]] = []
    selected_cases = cases if cases is not None else build_default_sensitivity_cases(config)
    for case in selected_cases:
        market = MarketState(
            cash_rate=config.market.cash_rate,
            wholesale_funding_spread=case.wholesale_funding_rate - config.market.cash_rate,
            competitor_loan_rate=case.competitor_loan_rate,
            competitor_deposit_rate=case.competitor_deposit_rate,
        )
        diagnostics = evaluate_adaptive_joint_rate_grid(
            loan_customers=loan_customers,
            deposit_customers=deposit_customers,
            market=market,
            loan_rate_grid=loan_grid,
            deposit_rate_grid=deposit_grid,
            loan_rate_elasticity=case.loan_rate_elasticity,
            loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
            deposit_rate_elasticity=case.deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
            required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
            funding_shortfall_penalty_rate=case.funding_shortfall_penalty_rate,
            home_loan_reference_rate=config.market.home_loan_reference_rate,
            term_deposit_reference_rate=config.market.term_deposit_reference_rate,
            max_boundary_expansions=max_boundary_expansions,
        )
        best_result = diagnostics.best_result
        rows.append(
            {
                "scenario_name": case.name,
                "best_loan_rate": best_result.loan_rate,
                "best_deposit_rate": best_result.deposit_rate,
                "objective_value": best_result.total_objective,
                "expected_loan_volume": best_result.expected_loan_volume,
                "expected_deposit_volume": best_result.expected_deposit_volume,
                "funding_shortfall": best_result.funding_shortfall,
                "initial_boundary_warning_flag": (
                    diagnostics.initial_boundary_diagnostics.has_warning
                ),
                "boundary_warning_flag": diagnostics.boundary_diagnostics.has_warning,
                "expansion_rounds": diagnostics.expansion_rounds,
            }
        )

    return pd.DataFrame(rows, columns=SENSITIVITY_RESULT_COLUMNS)


def _rate_boundary_warnings(
    product_name: str,
    rate: float,
    minimum_rate: float,
    maximum_rate: float,
) -> list[str]:
    causes = "; ".join(BOUNDARY_WARNING_CAUSES)
    if np.isclose(rate, minimum_rate):
        return [
            (
                f"Winning {product_name} rate {rate:.2%} is at the minimum grid boundary. "
                f"Possible causes: {causes}."
            )
        ]
    if np.isclose(rate, maximum_rate):
        return [
            (
                f"Winning {product_name} rate {rate:.2%} is at the maximum grid boundary. "
                f"Possible causes: {causes}."
            )
        ]
    return []


def _expand_grid_for_selected_rate(
    rate_grid: np.ndarray,
    selected_rate: float,
) -> tuple[np.ndarray, bool]:
    sorted_grid = np.array(sorted({float(rate) for rate in rate_grid}))
    if len(sorted_grid) < 2:
        return sorted_grid, False

    step = float(np.diff(sorted_grid)[0])
    minimum_rate = float(sorted_grid[0])
    maximum_rate = float(sorted_grid[-1])
    span = maximum_rate - minimum_rate

    if np.isclose(selected_rate, minimum_rate):
        expanded_minimum = max(0.0, minimum_rate - span)
        if np.isclose(expanded_minimum, minimum_rate):
            return sorted_grid, False
        n_steps = int(round((maximum_rate - expanded_minimum) / step))
        return np.round(expanded_minimum + np.arange(n_steps + 1) * step, 6), True

    if np.isclose(selected_rate, maximum_rate):
        expanded_maximum = maximum_rate + span
        n_steps = int(round((expanded_maximum - minimum_rate) / step))
        return np.round(minimum_rate + np.arange(n_steps + 1) * step, 6), True

    return sorted_grid, False
