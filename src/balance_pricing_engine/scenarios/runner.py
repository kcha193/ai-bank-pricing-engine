"""Scenario orchestration for the first pricing engine milestone."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.optimization.grid_search import (
    OptimizationResult,
    build_rate_grid,
    evaluate_joint_rate_grid,
    select_best_result,
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

    def top_results(self, n: int = 5) -> pd.DataFrame:
        """Return the top objective rows for reporting."""
        return self.grid_results.sort_values("objective_value", ascending=False).head(n).reset_index(drop=True)


def run_baseline_diagnostics(config: EngineConfig) -> ScenarioDiagnostics:
    """Run the baseline scenario and keep all evaluated pricing candidates."""
    rng = make_rng(config.random_seed)
    loan_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    deposit_customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
    market = MarketState(**config.market.model_dump())

    loan_grid = build_rate_grid(**config.products.loan_rate_grid.model_dump())
    deposit_grid = build_rate_grid(**config.products.deposit_rate_grid.model_dump())

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
    )
    return ScenarioDiagnostics(
        best_result=select_best_result(grid_results),
        grid_results=grid_results,
    )


def run_baseline_scenario(config: EngineConfig) -> OptimizationResult:
    """Run the baseline synthetic pricing scenario."""
    return run_baseline_diagnostics(config).best_result
