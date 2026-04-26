"""Scenario orchestration for the first pricing engine milestone."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import numpy as np

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.diagnostics import (
    BoundaryDiagnostics,
    ObjectiveDecomposition,
    evaluate_boundary_diagnostics,
    objective_decomposition_from_result,
)
from balance_pricing_engine.optimization.grid_search import (
    OptimizationResult,
    build_rate_grid,
    evaluate_joint_rate_grid,
    select_best_result,
)
from balance_pricing_engine.market import (
    MarketRates,
    SpreadGridConfig,
    build_market_relative_grid,
)
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.utils.random import make_rng


@dataclass(frozen=True)
class ScenarioDiagnostics:
    best_result: OptimizationResult
    grid_results: pd.DataFrame
    boundary_diagnostics: BoundaryDiagnostics

    def top_results(self, n: int = 5) -> pd.DataFrame:
        """Return the top objective rows for reporting."""
        return self.grid_results.sort_values("total_objective", ascending=False).head(n).reset_index(drop=True)

    def winner_decomposition(self) -> ObjectiveDecomposition:
        """Return an objective decomposition for the winning result."""
        return objective_decomposition_from_result(self.best_result)


def run_baseline_diagnostics(config: EngineConfig) -> ScenarioDiagnostics:
    """Run the baseline scenario and keep all evaluated pricing candidates."""
    rng = make_rng(config.random_seed)
    loan_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    deposit_customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
    market = MarketState(**config.market.model_dump())

    loan_grid, deposit_grid = build_configured_rate_grids(config)

    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=loan_grid,
        deposit_rate_grid=deposit_grid,
        loan_rate_elasticity=config.response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=config.response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
        home_loan_reference_rate=config.market.home_loan_reference_rate,
        term_deposit_reference_rate=config.market.term_deposit_reference_rate,
    )
    best_result = select_best_result(grid_results)
    return ScenarioDiagnostics(
        best_result=best_result,
        grid_results=grid_results,
        boundary_diagnostics=evaluate_boundary_diagnostics(
            best_result,
            loan_grid,
            deposit_grid,
        ),
    )


def run_baseline_scenario(config: EngineConfig) -> OptimizationResult:
    """Run the baseline synthetic pricing scenario."""
    return run_baseline_diagnostics(config).best_result


def build_configured_rate_grids(config: EngineConfig) -> tuple[np.ndarray, np.ndarray]:
    """Build pricing grids from market-relative spreads, falling back to absolute grids."""
    if config.pricing_strategy is not None:
        market_rates = MarketRates(
            home_loan_reference_rate=float(config.market.home_loan_reference_rate),
            term_deposit_reference_rate=float(config.market.term_deposit_reference_rate),
        )
        return build_market_relative_grid(
            market_rates=market_rates,
            home_loan_spread_bps=SpreadGridConfig(
                **config.pricing_strategy.home_loan_spread_bps.model_dump()
            ),
            term_deposit_spread_bps=SpreadGridConfig(
                **config.pricing_strategy.term_deposit_spread_bps.model_dump()
            ),
        )

    if config.products is None:
        raise ValueError("Either pricing_strategy or products rate grids must be provided.")
    return (
        build_rate_grid(**config.products.loan_rate_grid.model_dump()),
        build_rate_grid(**config.products.deposit_rate_grid.model_dump()),
    )
