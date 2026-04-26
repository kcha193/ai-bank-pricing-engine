"""Scenario orchestration for the first pricing engine milestone."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pandas as pd

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.diagnostics import (
    BoundaryDiagnostics,
    ObjectiveDecomposition,
    evaluate_adaptive_joint_rate_grid,
    objective_decomposition_from_result,
)
from balance_pricing_engine.optimization.grid_search import (
    OptimizationResult,
)
from balance_pricing_engine.rate_grids import build_configured_rate_grids
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
    initial_boundary_diagnostics: BoundaryDiagnostics
    expansion_rounds: int = 0

    def top_results(self, n: int = 5) -> pd.DataFrame:
        """Return the top objective rows for reporting."""
        return self.grid_results.sort_values("total_objective", ascending=False).head(n).reset_index(drop=True)

    def winner_decomposition(self) -> ObjectiveDecomposition:
        """Return an objective decomposition for the winning result."""
        return objective_decomposition_from_result(self.best_result)

    def write_artifacts(
        self,
        output_dir: str | Path,
        sensitivity: pd.DataFrame | None = None,
        top_n: int = 5,
    ) -> None:
        """Persist scenario outputs for regression monitoring."""
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)

        self.grid_results.to_csv(path / "grid_results.csv", index=False)
        self.top_results(top_n).to_csv(path / "top_results.csv", index=False)
        if sensitivity is not None:
            sensitivity.to_csv(path / "sensitivity.csv", index=False)

        summary = {
            "best_loan_rate": self.best_result.loan_rate,
            "best_deposit_rate": self.best_result.deposit_rate,
            "objective_value": self.best_result.total_objective,
            "expected_loan_volume": self.best_result.expected_loan_volume,
            "expected_deposit_volume": self.best_result.expected_deposit_volume,
            "funding_shortfall": self.best_result.funding_shortfall,
            "boundary_warning_flag": self.boundary_diagnostics.has_warning,
            "initial_boundary_warning_flag": self.initial_boundary_diagnostics.has_warning,
            "expansion_rounds": self.expansion_rounds,
        }
        (path / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def run_baseline_diagnostics(
    config: EngineConfig,
    max_boundary_expansions: int = 2,
) -> ScenarioDiagnostics:
    """Run the baseline scenario and keep all evaluated pricing candidates."""
    rng = make_rng(config.random_seed)
    loan_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    deposit_customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
    market = MarketState(**config.market.model_dump())

    loan_grid, deposit_grid = build_configured_rate_grids(config)

    diagnostics = evaluate_adaptive_joint_rate_grid(
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
        max_boundary_expansions=max_boundary_expansions,
    )

    return ScenarioDiagnostics(
        best_result=diagnostics.best_result,
        grid_results=diagnostics.grid_results,
        boundary_diagnostics=diagnostics.boundary_diagnostics,
        initial_boundary_diagnostics=diagnostics.initial_boundary_diagnostics,
        expansion_rounds=diagnostics.expansion_rounds,
    )


def run_baseline_scenario(config: EngineConfig) -> OptimizationResult:
    """Run the baseline synthetic pricing scenario."""
    return run_baseline_diagnostics(config).best_result
