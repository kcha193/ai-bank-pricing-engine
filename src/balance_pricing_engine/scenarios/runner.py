"""Scenario orchestration for the first pricing engine milestone."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np
import pandas as pd

from balance_pricing_engine.config import EngineConfig, ResponseCoefficientsConfig
from balance_pricing_engine.diagnostics import (
    BoundaryDiagnostics,
    ObjectiveDecomposition,
    evaluate_adaptive_joint_rate_grid,
    objective_decomposition_from_result,
    objective_decomposition_from_row,
)
from balance_pricing_engine.optimization.grid_search import (
    GRID_RESULT_COLUMNS,
    OptimizationResult,
)
from balance_pricing_engine.optimization.segmented import (
    SegmentedOptimizationResult,
    optimize_segmented_rates,
)
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)
from balance_pricing_engine.utils.random import make_rng


@dataclass(frozen=True)
class ScenarioDiagnostics:
    best_result: OptimizationResult
    grid_results: pd.DataFrame
    boundary_diagnostics: BoundaryDiagnostics
    initial_boundary_diagnostics: BoundaryDiagnostics
    expansion_rounds: int = 0
    expansion_capped: bool = False
    policy_band_breached: bool = False
    loan_customers: pd.DataFrame | None = None
    deposit_customers: pd.DataFrame | None = None
    market: MarketState | None = None
    loan_rate_elasticity: float | None = None
    loan_competitor_gap_elasticity: float | None = None
    deposit_rate_elasticity: float | None = None
    deposit_competitor_gap_elasticity: float | None = None
    response_coefficients: ResponseCoefficientsConfig | None = None
    segmented_tier_rates: dict[str, dict[str, float]] | None = None

    def top_results(self, n: int = 5) -> pd.DataFrame:
        """Return the top objective rows for reporting."""
        return self.grid_results.sort_values("total_objective", ascending=False).head(n).reset_index(drop=True)

    def winner_decomposition(self) -> ObjectiveDecomposition:
        """Return an objective decomposition for the winning result."""
        return objective_decomposition_from_result(self.best_result)

    def counterfactual_decompositions(self, top_n: int = 3) -> list[ObjectiveDecomposition]:
        """Return one-axis counterfactual decompositions for top pricing candidates."""
        decompositions: list[ObjectiveDecomposition] = []
        top_rows = self.top_results(top_n)
        for _, row in top_rows.iterrows():
            candidate = self._counterfactual_row(row)
            decompositions.append(objective_decomposition_from_row(candidate))
        return decompositions

    def cohort_attribution(self) -> dict[str, dict[str, float]]:
        """Return expected winning volumes by coarse synthetic customer segment."""
        self._require_cohort_context()
        loan_probabilities = loan_acceptance_probability(
            self.loan_customers,
            self.best_result.loan_rate,
            self.market.competitor_loan_rate,
            self.loan_rate_elasticity,
            self.loan_competitor_gap_elasticity,
            coefficients=self.response_coefficients.loan,
        )
        deposit_probabilities = deposit_uptake_probability(
            self.deposit_customers,
            self.best_result.deposit_rate,
            self.market.competitor_deposit_rate,
            self.deposit_rate_elasticity,
            self.deposit_competitor_gap_elasticity,
            coefficients=self.response_coefficients.deposit,
        )
        loan_segments = _risk_segments(self.loan_customers["risk_score"])
        deposit_segments = _wealth_segments(self.deposit_customers["deposit_amount"])
        return {
            "loan_volume_by_risk_segment": _volume_by_segment(
                loan_segments,
                loan_probabilities * self.loan_customers["loan_amount"].to_numpy(),
            ),
            "deposit_volume_by_wealth_segment": _volume_by_segment(
                deposit_segments,
                deposit_probabilities * self.deposit_customers["deposit_amount"].to_numpy(),
            ),
        }

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
        pd.DataFrame(
            asdict(decomposition)
            for decomposition in self.counterfactual_decompositions(top_n=top_n)
        ).to_csv(path / "counterfactuals.csv", index=False)
        if self._has_cohort_context():
            pd.DataFrame(_cohort_attribution_rows(self.cohort_attribution())).to_csv(
                path / "cohort_attribution.csv",
                index=False,
            )
        if sensitivity is not None:
            sensitivity.to_csv(path / "sensitivity.csv", index=False)

        summary = {
            "best_loan_rate": self.best_result.loan_rate,
            "best_deposit_rate": self.best_result.deposit_rate,
            "objective_value": self.best_result.total_objective,
            "capital_charge": self.best_result.capital_charge,
            "excess_deposit_income": self.best_result.excess_deposit_income,
            "expected_loan_volume": self.best_result.expected_loan_volume,
            "expected_deposit_volume": self.best_result.expected_deposit_volume,
            "funding_shortfall": self.best_result.funding_shortfall,
            "boundary_warning_flag": self.boundary_diagnostics.has_warning,
            "initial_boundary_warning_flag": self.initial_boundary_diagnostics.has_warning,
            "expansion_rounds": self.expansion_rounds,
            "expansion_capped": self.expansion_capped,
            "policy_band_breached": self.policy_band_breached,
        }
        if self.segmented_tier_rates is not None:
            summary["segmented_tier_rates"] = self.segmented_tier_rates
            (path / "segmented_tier_rates.json").write_text(
                json.dumps(self.segmented_tier_rates, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        (path / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _counterfactual_row(self, row: pd.Series) -> pd.Series:
        loan_delta = abs(float(row["loan_rate"]) - self.best_result.loan_rate)
        deposit_delta = abs(float(row["deposit_rate"]) - self.best_result.deposit_rate)
        if loan_delta >= deposit_delta:
            candidates = self.grid_results[
                np.isclose(self.grid_results["deposit_rate"], self.best_result.deposit_rate)
            ]
        else:
            candidates = self.grid_results[
                np.isclose(self.grid_results["loan_rate"], self.best_result.loan_rate)
            ]
        return candidates.sort_values("total_objective", ascending=False).iloc[0]

    def _require_cohort_context(self) -> None:
        if not self._has_cohort_context():
            raise ValueError("Cohort attribution requires baseline customer context.")

    def _has_cohort_context(self) -> bool:
        return not (
            self.loan_customers is None
            or self.deposit_customers is None
            or self.market is None
            or self.loan_rate_elasticity is None
            or self.loan_competitor_gap_elasticity is None
            or self.deposit_rate_elasticity is None
            or self.deposit_competitor_gap_elasticity is None
            or self.response_coefficients is None
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

    if config.segmented_pricing is not None:
        segmented_result = optimize_segmented_rates(
            loan_customers=loan_customers,
            deposit_customers=deposit_customers,
            market=market,
            config=config.segmented_pricing,
            loan_rate_elasticity=config.response.loan_rate_elasticity,
            loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
            deposit_rate_elasticity=config.response.deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
            required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
            funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
            loan_rwa_weight=config.balance_sheet.loan_rwa_weight,
            capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
            response_coefficients=config.response.coefficients,
        )
        return _segmented_diagnostics(segmented_result)

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
        rwa_weight=config.balance_sheet.loan_rwa_weight,
        capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
        home_loan_reference_rate=config.market.home_loan_reference_rate,
        term_deposit_reference_rate=config.market.term_deposit_reference_rate,
        max_boundary_expansions=max_boundary_expansions,
        response_coefficients=config.response.coefficients,
    )

    return ScenarioDiagnostics(
        best_result=diagnostics.best_result,
        grid_results=diagnostics.grid_results,
        boundary_diagnostics=diagnostics.boundary_diagnostics,
        initial_boundary_diagnostics=diagnostics.initial_boundary_diagnostics,
        expansion_rounds=diagnostics.expansion_rounds,
        expansion_capped=diagnostics.expansion_capped,
        policy_band_breached=diagnostics.policy_band_breached,
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_elasticity=config.response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=config.response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
        response_coefficients=config.response.coefficients,
    )


def _segmented_diagnostics(result: SegmentedOptimizationResult) -> ScenarioDiagnostics:
    best_result = _optimization_result_from_segmented_result(result)
    grid_results = _grid_results_from_segmented_result(result, best_result)
    return ScenarioDiagnostics(
        best_result=best_result,
        grid_results=grid_results,
        boundary_diagnostics=BoundaryDiagnostics(warnings=()),
        initial_boundary_diagnostics=BoundaryDiagnostics(warnings=()),
        segmented_tier_rates={
            "loan_tiers": {tier.tier_name: tier.rate for tier in result.loan_tiers},
            "deposit_tiers": {tier.tier_name: tier.rate for tier in result.deposit_tiers},
        },
    )


def _optimization_result_from_segmented_result(
    result: SegmentedOptimizationResult,
) -> OptimizationResult:
    return OptimizationResult(
        loan_rate=_volume_weighted_average_rate(result.loan_tiers),
        deposit_rate=_volume_weighted_average_rate(result.deposit_tiers),
        loan_interest_income=result.total_loan_interest_income,
        expected_credit_loss=result.total_credit_loss,
        capital_charge=result.capital_charge,
        deposit_interest_cost=result.total_deposit_interest_cost,
        wholesale_funding_cost=result.wholesale_funding_cost,
        excess_deposit_income=result.excess_deposit_income,
        funding_shortfall_penalty=result.funding_shortfall_penalty,
        total_objective=result.total_objective,
        expected_loan_volume=result.total_loan_volume,
        expected_deposit_volume=result.total_deposit_volume,
        funding_shortfall=result.funding_shortfall,
    )


def _grid_results_from_segmented_result(
    segmented_result: SegmentedOptimizationResult,
    result: OptimizationResult,
) -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    for tier in segmented_result.loan_tiers:
        rows.append(
            {
                "loan_rate": tier.rate,
                "deposit_rate": result.deposit_rate,
                "home_loan_spread_to_market_bps": float("nan"),
                "term_deposit_spread_to_market_bps": float("nan"),
                "loan_interest_income": tier.interest_income,
                "expected_credit_loss": tier.credit_loss,
                "capital_charge": tier.capital_charge,
                "deposit_interest_cost": result.deposit_interest_cost,
                "wholesale_funding_cost": result.wholesale_funding_cost,
                "excess_deposit_income": result.excess_deposit_income,
                "funding_shortfall_penalty": result.funding_shortfall_penalty,
                "total_objective": result.total_objective,
                "expected_loan_volume": tier.volume,
                "expected_deposit_volume": result.expected_deposit_volume,
                "funding_shortfall": result.funding_shortfall,
            }
        )
    for tier in segmented_result.deposit_tiers:
        rows.append(
            {
                "loan_rate": result.loan_rate,
                "deposit_rate": tier.rate,
                "home_loan_spread_to_market_bps": float("nan"),
                "term_deposit_spread_to_market_bps": float("nan"),
                "loan_interest_income": result.loan_interest_income,
                "expected_credit_loss": result.expected_credit_loss,
                "capital_charge": result.capital_charge,
                "deposit_interest_cost": tier.interest_cost,
                "wholesale_funding_cost": result.wholesale_funding_cost,
                "excess_deposit_income": result.excess_deposit_income,
                "funding_shortfall_penalty": result.funding_shortfall_penalty,
                "total_objective": result.total_objective,
                "expected_loan_volume": result.expected_loan_volume,
                "expected_deposit_volume": tier.volume,
                "funding_shortfall": result.funding_shortfall,
            }
        )
    return pd.DataFrame(rows, columns=GRID_RESULT_COLUMNS)


def _volume_weighted_average_rate(tiers) -> float:
    total_volume = sum(tier.volume for tier in tiers)
    if total_volume <= 0:
        return 0.0
    return sum(tier.rate * tier.volume for tier in tiers) / total_volume


def run_baseline_scenario(config: EngineConfig) -> OptimizationResult:
    """Run the baseline synthetic pricing scenario."""
    return run_baseline_diagnostics(config).best_result


def _risk_segments(risk_score: pd.Series) -> pd.Series:
    return pd.cut(
        risk_score,
        bins=[-np.inf, 0.33, 0.66, np.inf],
        labels=["low", "medium", "high"],
    ).astype(str)


def _wealth_segments(deposit_amount: pd.Series) -> pd.Series:
    return pd.cut(
        deposit_amount,
        bins=[-np.inf, 50_000, 150_000, np.inf],
        labels=["low", "medium", "high"],
    ).astype(str)


def _volume_by_segment(segments: pd.Series, expected_volume: np.ndarray) -> dict[str, float]:
    frame = pd.DataFrame({"segment": segments, "expected_volume": expected_volume})
    grouped = frame.groupby("segment", observed=False)["expected_volume"].sum()
    return {segment: float(grouped.get(segment, 0.0)) for segment in ["low", "medium", "high"]}


def _cohort_attribution_rows(attribution: dict[str, dict[str, float]]) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for segment_type, values in attribution.items():
        rows.extend(
            {
                "segment_type": segment_type,
                "segment": segment,
                "expected_volume": volume,
            }
            for segment, volume in values.items()
        )
    return rows
