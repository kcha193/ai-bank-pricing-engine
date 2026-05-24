"""Scenario orchestration for the first pricing engine milestone."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np
import pandas as pd

from balance_pricing_engine.config import (
    DepositTierConfig,
    EngineConfig,
    GridConfig,
    LoanTierConfig,
    ResponseCoefficientsConfig,
    SegmentedPricingConfig,
)
from balance_pricing_engine.diagnostics import (
    BoundaryDiagnostics,
    ObjectiveDecomposition,
    evaluate_adaptive_joint_rate_grid,
    infer_boundary_warning_cause,
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
from balance_pricing_engine.pricing.profit import maturity_adjusted_rwa_weight
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
        return _evaluate_adaptive_segmented_rates(
            config=config,
            loan_customers=loan_customers,
            deposit_customers=deposit_customers,
            market=market,
            max_boundary_expansions=max_boundary_expansions,
        )

    loan_grid, deposit_grid = build_configured_rate_grids(config)
    effective_rwa_weight = maturity_adjusted_rwa_weight(
        config.balance_sheet.loan_rwa_weight,
        config.balance_sheet.average_loan_tenor_years,
    )

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
        rwa_weight=effective_rwa_weight,
        capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
        home_loan_reference_rate=config.market.home_loan_reference_rate,
        term_deposit_reference_rate=config.market.term_deposit_reference_rate,
        max_boundary_expansions=max_boundary_expansions,
        response_coefficients=config.response.coefficients,
        min_return_on_equity=config.balance_sheet.min_return_on_equity,
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


def _evaluate_adaptive_segmented_rates(
    config: EngineConfig,
    loan_customers: pd.DataFrame,
    deposit_customers: pd.DataFrame,
    market: MarketState,
    max_boundary_expansions: int,
) -> ScenarioDiagnostics:
    segmented_config = config.segmented_pricing
    if segmented_config is None:
        raise ValueError("Segmented pricing config is required.")

    original_segmented_config = segmented_config
    initial_boundary_diagnostics: BoundaryDiagnostics | None = None
    expansion_rounds = 0
    effective_rwa_weight = maturity_adjusted_rwa_weight(
        config.balance_sheet.loan_rwa_weight,
        config.balance_sheet.average_loan_tenor_years,
    )

    while True:
        result = optimize_segmented_rates(
            loan_customers=loan_customers,
            deposit_customers=deposit_customers,
            market=market,
            config=segmented_config,
            loan_rate_elasticity=config.response.loan_rate_elasticity,
            loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
            deposit_rate_elasticity=config.response.deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
            required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
            funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
            loan_rwa_weight=effective_rwa_weight,
            capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
            response_coefficients=config.response.coefficients,
        )
        diagnostics = _segmented_diagnostics(
            result,
            segmented_config,
            market,
            expansion_rounds=expansion_rounds,
            initial_boundary_diagnostics=initial_boundary_diagnostics,
            original_config=original_segmented_config,
            expansion_capped=False,
        )
        if initial_boundary_diagnostics is None:
            initial_boundary_diagnostics = diagnostics.boundary_diagnostics
        if (
            not diagnostics.boundary_diagnostics.has_warning
            or expansion_rounds >= max_boundary_expansions
        ):
            return _segmented_diagnostics(
                result,
                segmented_config,
                market,
                expansion_rounds=expansion_rounds,
                initial_boundary_diagnostics=initial_boundary_diagnostics,
                original_config=original_segmented_config,
                expansion_capped=(
                    diagnostics.boundary_diagnostics.has_warning
                    and expansion_rounds >= max_boundary_expansions
                ),
            )

        segmented_config, expanded = _expand_segmented_config_for_boundary(
            result,
            segmented_config,
        )
        if not expanded:
            return _segmented_diagnostics(
                result,
                segmented_config,
                market,
                expansion_rounds=expansion_rounds,
                initial_boundary_diagnostics=initial_boundary_diagnostics,
                original_config=original_segmented_config,
                expansion_capped=diagnostics.boundary_diagnostics.has_warning,
            )
        expansion_rounds += 1


def _segmented_diagnostics(
    result: SegmentedOptimizationResult,
    config: SegmentedPricingConfig,
    market: MarketState,
    *,
    expansion_rounds: int = 0,
    initial_boundary_diagnostics: BoundaryDiagnostics | None = None,
    original_config: SegmentedPricingConfig | None = None,
    expansion_capped: bool = False,
) -> ScenarioDiagnostics:
    best_result = _optimization_result_from_segmented_result(result)
    grid_results = _grid_results_from_segmented_result(result, best_result)
    boundary_diagnostics = _segmented_boundary_diagnostics(
        result,
        config,
        best_result,
        market,
    )
    return ScenarioDiagnostics(
        best_result=best_result,
        grid_results=grid_results,
        boundary_diagnostics=boundary_diagnostics,
        initial_boundary_diagnostics=initial_boundary_diagnostics or boundary_diagnostics,
        expansion_rounds=expansion_rounds,
        expansion_capped=expansion_capped,
        policy_band_breached=_segmented_policy_band_breached(
            result,
            original_config or config,
        ),
        segmented_tier_rates={
            "loan_tiers": {tier.tier_name: tier.rate for tier in result.loan_tiers},
            "deposit_tiers": {tier.tier_name: tier.rate for tier in result.deposit_tiers},
        },
    )


def _segmented_boundary_diagnostics(
    result: SegmentedOptimizationResult,
    config: SegmentedPricingConfig,
    best_result: OptimizationResult,
    market: MarketState,
) -> BoundaryDiagnostics:
    cause = infer_boundary_warning_cause(
        objective_decomposition_from_result(best_result),
        loan_rate=best_result.loan_rate,
        deposit_rate=best_result.deposit_rate,
        competitor_loan_rate=market.competitor_loan_rate,
        competitor_deposit_rate=market.competitor_deposit_rate,
    )
    warnings: list[str] = []
    for tier_result, tier_config in zip(result.loan_tiers, config.loan_tiers):
        warnings.extend(
            _tier_boundary_warnings(
                product_name=f"home loan tier {tier_config.name}",
                rate=tier_result.rate,
                rate_grid=tier_config.rate_grid,
                cause=cause,
            )
        )
    for tier_result, tier_config in zip(result.deposit_tiers, config.deposit_tiers):
        warnings.extend(
            _tier_boundary_warnings(
                product_name=f"term deposit tier {tier_config.name}",
                rate=tier_result.rate,
                rate_grid=tier_config.rate_grid,
                cause=cause,
            )
        )
    return BoundaryDiagnostics(warnings=tuple(warnings))


def _segmented_policy_band_breached(
    result: SegmentedOptimizationResult,
    original_config: SegmentedPricingConfig,
) -> bool:
    loan_breached = any(
        _rate_outside_tier_grid(tier_result.rate, tier_config.rate_grid)
        for tier_result, tier_config in zip(result.loan_tiers, original_config.loan_tiers)
    )
    deposit_breached = any(
        _rate_outside_tier_grid(tier_result.rate, tier_config.rate_grid)
        for tier_result, tier_config in zip(result.deposit_tiers, original_config.deposit_tiers)
    )
    return loan_breached or deposit_breached


def _rate_outside_tier_grid(rate: float, rate_grid: GridConfig) -> bool:
    return bool(rate < rate_grid.start and not np.isclose(rate, rate_grid.start)) or bool(
        rate > rate_grid.stop and not np.isclose(rate, rate_grid.stop)
    )


def _tier_boundary_warnings(
    product_name: str,
    rate: float,
    rate_grid: GridConfig,
    cause: str,
) -> list[str]:
    if np.isclose(rate, rate_grid.start):
        return [
            (
                f"Winning {product_name} rate {rate:.2%} is at the minimum grid boundary. "
                f"Likely cause: {cause}."
            )
        ]
    if np.isclose(rate, rate_grid.stop):
        return [
            (
                f"Winning {product_name} rate {rate:.2%} is at the maximum grid boundary. "
                f"Likely cause: {cause}."
            )
        ]
    return []


def _expand_segmented_config_for_boundary(
    result: SegmentedOptimizationResult,
    config: SegmentedPricingConfig,
) -> tuple[SegmentedPricingConfig, bool]:
    loan_tiers, loan_expanded = _expand_tiers_for_boundary(
        config.loan_tiers,
        result.loan_tiers,
    )
    deposit_tiers, deposit_expanded = _expand_tiers_for_boundary(
        config.deposit_tiers,
        result.deposit_tiers,
    )
    return (
        config.model_copy(
            update={
                "loan_tiers": loan_tiers,
                "deposit_tiers": deposit_tiers,
            }
        ),
        loan_expanded or deposit_expanded,
    )


def _expand_tiers_for_boundary(
    tier_configs: list[LoanTierConfig] | list[DepositTierConfig],
    tier_results,
):
    expanded_tiers = []
    expanded_any = False
    for tier_config, tier_result in zip(tier_configs, tier_results):
        expanded_grid, expanded = _expand_tier_grid_for_selected_rate(
            tier_config.rate_grid,
            tier_result.rate,
        )
        expanded_tiers.append(tier_config.model_copy(update={"rate_grid": expanded_grid}))
        expanded_any = expanded_any or expanded
    return expanded_tiers, expanded_any


def _expand_tier_grid_for_selected_rate(
    rate_grid: GridConfig,
    selected_rate: float,
) -> tuple[GridConfig, bool]:
    span = rate_grid.stop - rate_grid.start
    if span <= 0.0:
        return rate_grid, False
    if np.isclose(selected_rate, rate_grid.start):
        expanded_start = max(0.0, rate_grid.start - span)
        if np.isclose(expanded_start, rate_grid.start):
            return rate_grid, False
        return rate_grid.model_copy(update={"start": expanded_start}), True
    if np.isclose(selected_rate, rate_grid.stop):
        return rate_grid.model_copy(update={"stop": rate_grid.stop + span}), True
    return rate_grid, False


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
    # Segmented grid rows are synthetic decomposition rows, not independent alternatives.
    # total_objective is always the actual portfolio value so rows never dominate the winner.
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
                "return_on_equity": float("nan"),
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
                "return_on_equity": float("nan"),
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
