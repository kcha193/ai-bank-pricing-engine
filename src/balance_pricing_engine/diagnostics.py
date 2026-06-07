"""Diagnostics for explaining and challenging pricing optimizer outputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

import numpy as np
import pandas as pd

from balance_pricing_engine.config import EngineConfig, ResponseCoefficientsConfig
from balance_pricing_engine.grid_utils import build_inclusive_numeric_grid
from balance_pricing_engine.optimization.grid_search import (
    OptimizationResult,
    evaluate_joint_rate_grid,
    select_best_result,
)
from balance_pricing_engine.optimization.response_fn import ResponseFn
from balance_pricing_engine.pricing.profit import maturity_adjusted_rwa_weight
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.utils.random import make_rng


COMPETITOR_GAP_WARNING_THRESHOLD_BPS = 150.0
FUNDING_SHORTFALL_NEAR_ZERO_RATIO = 0.01
MIN_BOUNDARY_EXPANSION_HORIZON = 2

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
    "excess_deposit_income",
    "delta_loan_rate_bps",
    "delta_deposit_rate_bps",
    "delta_objective_pct",
    "parameter_family",
]

_SENSITIVITY_AGGREGATED_COLUMNS = [
    "best_loan_rate",
    "best_deposit_rate",
    "objective_value",
    "expected_loan_volume",
    "expected_deposit_volume",
    "funding_shortfall",
    "excess_deposit_income",
]


def sensitivity_result_columns(n_seeds: int = 1) -> list[str]:
    """Return the documented column contract for sensitivity results."""
    if n_seeds == 1:
        return SENSITIVITY_RESULT_COLUMNS
    extra = [
        f"{c}_{suffix}"
        for c in _SENSITIVITY_AGGREGATED_COLUMNS
        for suffix in ("mean", "std_error")
    ]
    return SENSITIVITY_RESULT_COLUMNS + extra


@dataclass(frozen=True)
class ObjectiveDecomposition:
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
    capital_charge: float = 0.0

    @property
    def reconciled_total_objective(self) -> float:
        """Recalculate the objective from individual economic components."""
        return (
            self.loan_interest_income
            - self.expected_credit_loss
            - self.capital_charge
            - self.deposit_interest_cost
            - self.wholesale_funding_cost
            - self.funding_shortfall_penalty
            + self.excess_deposit_income
        )

    @property
    def loan_income_after_losses(self) -> float:
        """Loan income net of expected credit losses."""
        return self.loan_interest_income - self.expected_credit_loss

    @property
    def funding_penalty(self) -> float:
        """Funding shortfall penalty component."""
        return self.funding_shortfall_penalty


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
    loan_competitor_gap_elasticity: float
    deposit_rate_elasticity: float
    deposit_competitor_gap_elasticity: float
    funding_shortfall_penalty_rate: float
    loan_rwa_weight: float
    capital_hurdle_rate: float
    wholesale_funding_rate: float
    competitor_loan_rate: float
    competitor_deposit_rate: float
    response_coefficients: ResponseCoefficientsConfig


@dataclass(frozen=True)
class AdaptiveGridResult:
    best_result: OptimizationResult
    grid_results: pd.DataFrame
    boundary_diagnostics: BoundaryDiagnostics
    initial_boundary_diagnostics: BoundaryDiagnostics
    expansion_rounds: int
    expansion_capped: bool
    policy_band_breached: bool


def objective_decomposition_from_result(result: OptimizationResult) -> ObjectiveDecomposition:
    """Build an objective decomposition for the selected pricing result."""
    return ObjectiveDecomposition(
        loan_interest_income=result.loan_interest_income,
        expected_credit_loss=result.expected_credit_loss,
        deposit_interest_cost=result.deposit_interest_cost,
        wholesale_funding_cost=result.wholesale_funding_cost,
        excess_deposit_income=result.excess_deposit_income,
        funding_shortfall_penalty=result.funding_shortfall_penalty,
        total_objective=result.total_objective,
        expected_loan_volume=result.expected_loan_volume,
        expected_deposit_volume=result.expected_deposit_volume,
        funding_shortfall=result.funding_shortfall,
        capital_charge=result.capital_charge,
    )


def objective_decomposition_from_row(row: pd.Series) -> ObjectiveDecomposition:
    """Build an objective decomposition from one evaluated grid row."""
    return ObjectiveDecomposition(
        loan_interest_income=float(row["loan_interest_income"]),
        expected_credit_loss=float(row["expected_credit_loss"]),
        deposit_interest_cost=float(row["deposit_interest_cost"]),
        wholesale_funding_cost=float(row["wholesale_funding_cost"]),
        excess_deposit_income=float(row["excess_deposit_income"]),
        funding_shortfall_penalty=float(row["funding_shortfall_penalty"]),
        total_objective=float(row["total_objective"]),
        expected_loan_volume=float(row["expected_loan_volume"]),
        expected_deposit_volume=float(row["expected_deposit_volume"]),
        funding_shortfall=float(row["funding_shortfall"]),
        capital_charge=float(row["capital_charge"]),
    )


def infer_boundary_warning_cause(
    decomposition: ObjectiveDecomposition,
    loan_rate: float,
    deposit_rate: float,
    competitor_loan_rate: float | None = None,
    competitor_deposit_rate: float | None = None,
) -> str:
    """Infer the most likely reason a boundary optimum needs review."""
    competitor_gap_bps = _largest_competitor_gap_bps(
        loan_rate=loan_rate,
        deposit_rate=deposit_rate,
        competitor_loan_rate=competitor_loan_rate,
        competitor_deposit_rate=competitor_deposit_rate,
    )
    if competitor_gap_bps >= COMPETITOR_GAP_WARNING_THRESHOLD_BPS:
        return "winner is far from competitor rates"

    if _funding_shortfall_near_zero(decomposition):
        return "funding shortfall is near zero"

    if decomposition.loan_income_after_losses >= decomposition.funding_penalty:
        return "loan income after losses dominates funding penalty"
    return "funding penalty dominates loan income after losses"


def evaluate_boundary_diagnostics(
    result: OptimizationResult,
    loan_rate_grid: np.ndarray,
    deposit_rate_grid: np.ndarray,
    market: MarketState | None = None,
) -> BoundaryDiagnostics:
    """Flag optimizer wins that land on the edge of either pricing grid."""
    warnings: list[str] = []
    loan_min = float(np.min(loan_rate_grid))
    loan_max = float(np.max(loan_rate_grid))
    deposit_min = float(np.min(deposit_rate_grid))
    deposit_max = float(np.max(deposit_rate_grid))
    cause = infer_boundary_warning_cause(
        objective_decomposition_from_result(result),
        loan_rate=result.loan_rate,
        deposit_rate=result.deposit_rate,
        competitor_loan_rate=None if market is None else market.competitor_loan_rate,
        competitor_deposit_rate=None if market is None else market.competitor_deposit_rate,
    )

    warnings.extend(
        _rate_boundary_warnings(
            product_name="home loan",
            rate=result.loan_rate,
            minimum_rate=loan_min,
            maximum_rate=loan_max,
            cause=cause,
        )
    )
    warnings.extend(
        _rate_boundary_warnings(
            product_name="term deposit",
            rate=result.deposit_rate,
            minimum_rate=deposit_min,
            maximum_rate=deposit_max,
            cause=cause,
        )
    )
    return BoundaryDiagnostics(warnings=tuple(warnings))


def expand_rate_grids_for_boundary(
    result: OptimizationResult,
    loan_rate_grid: np.ndarray,
    deposit_rate_grid: np.ndarray,
    span_multiplier: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, bool]:
    """Expand grids outward when the selected result lands on a boundary."""
    expanded_loan_grid, loan_expanded = _expand_grid_for_selected_rate(
        loan_rate_grid,
        result.loan_rate,
        span_multiplier=span_multiplier,
    )
    expanded_deposit_grid, deposit_expanded = _expand_grid_for_selected_rate(
        deposit_rate_grid,
        result.deposit_rate,
        span_multiplier=span_multiplier,
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
    rwa_weight: float = 0.35,
    capital_hurdle_rate: float = 0.12,
    home_loan_reference_rate: float | None = None,
    term_deposit_reference_rate: float | None = None,
    max_boundary_expansions: int = 2,
    response_coefficients: ResponseCoefficientsConfig | None = None,
    min_return_on_equity: float | None = None,
    loan_response_fn: ResponseFn | None = None,
    deposit_response_fn: ResponseFn | None = None,
) -> AdaptiveGridResult:
    """Evaluate a joint grid, expanding boundaries before accepting edge optima."""
    initial_boundary_diagnostics: BoundaryDiagnostics | None = None
    original_loan_rate_grid = np.array(loan_rate_grid, dtype=float)
    original_deposit_rate_grid = np.array(deposit_rate_grid, dtype=float)
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
            rwa_weight=rwa_weight,
            capital_hurdle_rate=capital_hurdle_rate,
            home_loan_reference_rate=home_loan_reference_rate,
            term_deposit_reference_rate=term_deposit_reference_rate,
            response_coefficients=response_coefficients,
            loan_response_fn=loan_response_fn,
            deposit_response_fn=deposit_response_fn,
        )
        best_result = select_best_result(
            grid_results,
            min_return_on_equity=min_return_on_equity,
        )
        boundary_diagnostics = evaluate_boundary_diagnostics(
            best_result,
            loan_rate_grid,
            deposit_rate_grid,
            market=market,
        )
        if initial_boundary_diagnostics is None:
            initial_boundary_diagnostics = boundary_diagnostics
        if not boundary_diagnostics.has_warning or expansion_rounds >= max_boundary_expansions:
            break

        span_multiplier = _boundary_expansion_span_multiplier(max_boundary_expansions)
        loan_rate_grid, deposit_rate_grid, expanded = expand_rate_grids_for_boundary(
            best_result,
            loan_rate_grid,
            deposit_rate_grid,
            span_multiplier=span_multiplier,
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
        expansion_capped=(
            boundary_diagnostics.has_warning
            and expansion_rounds >= max_boundary_expansions
        ),
        policy_band_breached=_policy_band_breached(
            best_result,
            original_loan_rate_grid=original_loan_rate_grid,
            original_deposit_rate_grid=original_deposit_rate_grid,
            home_loan_reference_rate=home_loan_reference_rate,
            term_deposit_reference_rate=term_deposit_reference_rate,
        ),
    )


# capital_hurdle_rate and loan_rwa_weight are multiplicatively confounded in the
# capital_charge formula (charge = volume × rwa_weight × tier1_ratio × hurdle_rate).
# A ±N% perturbation of either parameter produces identical objective sensitivity.
# They are kept as separate cases to verify this invariant, not as independent levers.
def build_default_sensitivity_cases(config: EngineConfig) -> list[SensitivityCase]:
    """Create a small set of one-at-a-time economic sensitivity cases."""
    base_wholesale_rate = config.market.cash_rate + config.market.wholesale_funding_spread
    lower_wholesale_rate = max(base_wholesale_rate - 0.005, config.market.cash_rate)

    baseline = SensitivityCase(
        name="baseline",
        loan_rate_elasticity=config.response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=config.response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
        funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
        loan_rwa_weight=config.balance_sheet.loan_rwa_weight,
        capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
        wholesale_funding_rate=base_wholesale_rate,
        competitor_loan_rate=config.market.competitor_loan_rate,
        competitor_deposit_rate=config.market.competitor_deposit_rate,
        response_coefficients=config.response.coefficients,
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
            name="loan_competitor_gap_elasticity_weaker",
            loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity
            * 0.75,
        ),
        replace(
            baseline,
            name="loan_competitor_gap_elasticity_stronger",
            loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity
            * 1.25,
        ),
        replace(
            baseline,
            name="deposit_competitor_gap_elasticity_weaker",
            deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity
            * 0.75,
        ),
        replace(
            baseline,
            name="deposit_competitor_gap_elasticity_stronger",
            deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity
            * 1.25,
        ),
        replace(
            baseline,
            name="loan_risk_score_coef_weaker",
            response_coefficients=_with_loan_risk_score_coef(
                config.response.coefficients,
                config.response.coefficients.loan.risk_score_coef * 0.75,
            ),
        ),
        replace(
            baseline,
            name="loan_risk_score_coef_stronger",
            response_coefficients=_with_loan_risk_score_coef(
                config.response.coefficients,
                config.response.coefficients.loan.risk_score_coef * 1.25,
            ),
        ),
        replace(
            baseline,
            name="deposit_rate_sensitivity_coef_weaker",
            response_coefficients=_with_deposit_rate_sensitivity_coef(
                config.response.coefficients,
                config.response.coefficients.deposit.rate_sensitivity_coef * 0.75,
            ),
        ),
        replace(
            baseline,
            name="deposit_rate_sensitivity_coef_stronger",
            response_coefficients=_with_deposit_rate_sensitivity_coef(
                config.response.coefficients,
                config.response.coefficients.deposit.rate_sensitivity_coef * 1.25,
            ),
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
            name="capital_hurdle_rate_lower",
            capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate * 0.75,
        ),
        replace(
            baseline,
            name="capital_hurdle_rate_higher",
            capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate * 1.25,
        ),
        replace(
            baseline,
            name="loan_rwa_weight_lower",
            loan_rwa_weight=config.balance_sheet.loan_rwa_weight * 0.75,
        ),
        replace(
            baseline,
            name="loan_rwa_weight_higher",
            loan_rwa_weight=config.balance_sheet.loan_rwa_weight * 1.25,
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
    n_seeds: int = 1,
) -> pd.DataFrame:
    """Run one-at-a-time sensitivity cases against the same synthetic customers."""
    if config.segmented_pricing is not None:
        raise NotImplementedError(
            "Sensitivity analysis is not supported for segmented pricing configurations. "
            "Use run_baseline_diagnostics directly for segmented runs."
        )
    if n_seeds <= 0:
        raise ValueError("n_seeds must be positive.")
    selected_cases = cases if cases is not None else build_default_sensitivity_cases(config)
    frames = [
        _run_sensitivity_analysis_for_seed(
            config=config,
            cases=selected_cases,
            seed=config.random_seed + seed_offset,
            max_boundary_expansions=max_boundary_expansions,
        )
        for seed_offset in range(n_seeds)
    ]
    if n_seeds == 1:
        return frames[0]
    combined = pd.concat(
        [frame.assign(seed_index=seed_index) for seed_index, frame in enumerate(frames)],
        ignore_index=True,
    )
    base = frames[0].copy()
    for column in _SENSITIVITY_AGGREGATED_COLUMNS:
        grouped = combined.groupby("scenario_name")[column]
        stats = grouped.agg(["mean", "std"]).reset_index()
        stats[f"{column}_mean"] = stats["mean"]
        stats[f"{column}_std_error"] = stats["std"].fillna(0.0) / np.sqrt(n_seeds)
        base = base.merge(
            stats[["scenario_name", f"{column}_mean", f"{column}_std_error"]],
            on="scenario_name",
            how="left",
        )
    return base[sensitivity_result_columns(n_seeds)]


def _run_sensitivity_analysis_for_seed(
    config: EngineConfig,
    cases: Sequence[SensitivityCase],
    seed: int,
    max_boundary_expansions: int,
) -> pd.DataFrame:
    rng = make_rng(seed)
    loan_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    deposit_customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
    loan_grid, deposit_grid = build_configured_rate_grids(config)

    rows: list[dict[str, float | str | bool]] = []
    for case in cases:
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
            loan_competitor_gap_elasticity=case.loan_competitor_gap_elasticity,
            deposit_rate_elasticity=case.deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=case.deposit_competitor_gap_elasticity,
            required_deposit_funding_ratio=config.balance_sheet.required_deposit_funding_ratio,
            funding_shortfall_penalty_rate=case.funding_shortfall_penalty_rate,
            rwa_weight=maturity_adjusted_rwa_weight(
                case.loan_rwa_weight,
                config.balance_sheet.average_loan_tenor_years,
            ),
            capital_hurdle_rate=case.capital_hurdle_rate,
            home_loan_reference_rate=config.market.home_loan_reference_rate,
            term_deposit_reference_rate=config.market.term_deposit_reference_rate,
            max_boundary_expansions=max_boundary_expansions,
            response_coefficients=case.response_coefficients,
            min_return_on_equity=config.balance_sheet.min_return_on_equity,
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
                "excess_deposit_income": best_result.excess_deposit_income,
                "initial_boundary_warning_flag": (
                    diagnostics.initial_boundary_diagnostics.has_warning
                ),
                "boundary_warning_flag": diagnostics.boundary_diagnostics.has_warning,
                "expansion_rounds": diagnostics.expansion_rounds,
            }
        )

    frame = pd.DataFrame(rows)
    baseline = frame.iloc[0]
    frame["delta_loan_rate_bps"] = (
        frame["best_loan_rate"] - float(baseline["best_loan_rate"])
    ) * 10_000
    frame["delta_deposit_rate_bps"] = (
        frame["best_deposit_rate"] - float(baseline["best_deposit_rate"])
    ) * 10_000
    baseline_objective = float(baseline["objective_value"])
    if np.isclose(baseline_objective, 0.0):
        frame["delta_objective_pct"] = 0.0
    else:
        frame["delta_objective_pct"] = (
            frame["objective_value"] - baseline_objective
        ) / abs(baseline_objective)
    frame["parameter_family"] = frame["scenario_name"].map(_parameter_family)
    return frame[SENSITIVITY_RESULT_COLUMNS]


def _parameter_family(scenario_name: str) -> str:
    name = scenario_name.lower()
    if name == "baseline":
        return "baseline"
    if "elasticity" in name or "coef" in name:
        return "elasticity"
    if "competitor" in name:
        return "competitor"
    if "wholesale" in name:
        return "wholesale"
    if "penalty" in name or "ratio" in name:
        return "funding"
    if "capital" in name or "rwa" in name or "hurdle" in name:
        return "capital"
    return "other"


def _with_loan_risk_score_coef(
    coefficients: ResponseCoefficientsConfig,
    risk_score_coef: float,
) -> ResponseCoefficientsConfig:
    return coefficients.model_copy(
        update={
            "loan": coefficients.loan.model_copy(
                update={"risk_score_coef": risk_score_coef}
            )
        },
        deep=True,
    )


def _with_deposit_rate_sensitivity_coef(
    coefficients: ResponseCoefficientsConfig,
    rate_sensitivity_coef: float,
) -> ResponseCoefficientsConfig:
    return coefficients.model_copy(
        update={
            "deposit": coefficients.deposit.model_copy(
                update={"rate_sensitivity_coef": rate_sensitivity_coef}
            )
        },
        deep=True,
    )


def _rate_boundary_warnings(
    product_name: str,
    rate: float,
    minimum_rate: float,
    maximum_rate: float,
    cause: str,
) -> list[str]:
    if np.isclose(rate, minimum_rate):
        return [
            (
                f"Winning {product_name} rate {rate:.2%} is at the minimum grid boundary. "
                f"Likely cause: {cause}."
            )
        ]
    if np.isclose(rate, maximum_rate):
        return [
            (
                f"Winning {product_name} rate {rate:.2%} is at the maximum grid boundary. "
                f"Likely cause: {cause}."
            )
        ]
    return []


def _largest_competitor_gap_bps(
    loan_rate: float,
    deposit_rate: float,
    competitor_loan_rate: float | None,
    competitor_deposit_rate: float | None,
) -> float:
    gaps: list[float] = []
    if competitor_loan_rate is not None:
        gaps.append(abs(loan_rate - competitor_loan_rate))
    if competitor_deposit_rate is not None:
        gaps.append(abs(deposit_rate - competitor_deposit_rate))
    if not gaps:
        return 0.0
    return max(gaps) * 10_000


def _funding_shortfall_near_zero(decomposition: ObjectiveDecomposition) -> bool:
    if decomposition.funding_shortfall < 0:
        return False
    scale = max(abs(decomposition.expected_loan_volume), 1.0)
    return decomposition.funding_shortfall / scale <= FUNDING_SHORTFALL_NEAR_ZERO_RATIO


def _policy_band_breached(
    result: OptimizationResult,
    original_loan_rate_grid: np.ndarray,
    original_deposit_rate_grid: np.ndarray,
    home_loan_reference_rate: float | None,
    term_deposit_reference_rate: float | None,
) -> bool:
    return _outside_original_band(
        rate=result.loan_rate,
        spread_bps=result.home_loan_spread_to_market_bps,
        original_rate_grid=original_loan_rate_grid,
        reference_rate=home_loan_reference_rate,
    ) or _outside_original_band(
        rate=result.deposit_rate,
        spread_bps=result.term_deposit_spread_to_market_bps,
        original_rate_grid=original_deposit_rate_grid,
        reference_rate=term_deposit_reference_rate,
    )


def _outside_original_band(
    rate: float,
    spread_bps: float,
    original_rate_grid: np.ndarray,
    reference_rate: float | None,
) -> bool:
    minimum_rate = float(np.min(original_rate_grid))
    maximum_rate = float(np.max(original_rate_grid))
    if reference_rate is None or np.isnan(spread_bps):
        return bool(rate < minimum_rate and not np.isclose(rate, minimum_rate)) or bool(
            rate > maximum_rate and not np.isclose(rate, maximum_rate)
        )

    minimum_spread_bps = (minimum_rate - reference_rate) * 10_000
    maximum_spread_bps = (maximum_rate - reference_rate) * 10_000
    return bool(
        spread_bps < minimum_spread_bps and not np.isclose(spread_bps, minimum_spread_bps)
    ) or bool(
        spread_bps > maximum_spread_bps and not np.isclose(spread_bps, maximum_spread_bps)
    )


def _expand_grid_for_selected_rate(
    rate_grid: np.ndarray,
    selected_rate: float,
    span_multiplier: float = 1.0,
) -> tuple[np.ndarray, bool]:
    sorted_grid = np.array(sorted({float(rate) for rate in rate_grid}))
    if len(sorted_grid) < 2:
        return sorted_grid, False

    step = float(np.diff(sorted_grid)[0])
    minimum_rate = float(sorted_grid[0])
    maximum_rate = float(sorted_grid[-1])
    span = maximum_rate - minimum_rate

    if np.isclose(selected_rate, minimum_rate):
        expanded_minimum = minimum_rate - span * span_multiplier
        if expanded_minimum < 0.0:
            increments_to_zero = int(np.ceil((0.0 - expanded_minimum) / step))
            expanded_minimum = expanded_minimum + increments_to_zero * step
            expanded_minimum = max(0.0, expanded_minimum)
        # Snap to the nearest step boundary reachable from maximum_rate going down, so
        # that non-integer span_multiplier values don't produce a misaligned stop.
        steps_below_max = int(np.ceil(round((maximum_rate - expanded_minimum) / step, 9)))
        expanded_minimum = maximum_rate - steps_below_max * step
        if np.isclose(expanded_minimum, minimum_rate):
            return sorted_grid, False
        return build_inclusive_numeric_grid(expanded_minimum, maximum_rate, step), True

    if np.isclose(selected_rate, maximum_rate):
        raw_expanded_maximum = maximum_rate + span * span_multiplier
        # Snap up to the nearest step boundary from minimum_rate so that non-integer
        # span_multiplier values don't produce a misaligned stop.
        steps_above_min = int(np.ceil(round((raw_expanded_maximum - minimum_rate) / step, 9)))
        expanded_maximum = minimum_rate + steps_above_min * step
        return build_inclusive_numeric_grid(minimum_rate, expanded_maximum, step), True

    return sorted_grid, False


def _boundary_expansion_span_multiplier(max_boundary_expansions: int) -> float:
    # When max_boundary_expansions >= MIN_BOUNDARY_EXPANSION_HORIZON (the common case),
    # multiple rounds of 1× expansion already provide sufficient cumulative coverage,
    # so no multiplier is needed.
    #
    # When max_boundary_expansions < MIN_BOUNDARY_EXPANSION_HORIZON, we compensate by
    # expanding more per round so a single pass covers the same cumulative ground as
    # MIN_BOUNDARY_EXPANSION_HORIZON rounds of 1× expansion would have.
    # With 1× per round, cumulative span coverage after k rounds compounds geometrically:
    # after round 1 the grid is 2× original, after round 2 it is 4× original, etc.
    # Total coverage = (2^k − 1) × original_span. Setting one-shot coverage equal to the
    # MIN_BOUNDARY_EXPANSION_HORIZON-round target gives:
    #   multiplier = (2^H − 1) / (2^N − 1)
    # where H = MIN_BOUNDARY_EXPANSION_HORIZON and N = max_boundary_expansions.
    # For N=1, H=2: multiplier = 3, so the single expansion reaches 4× original span —
    # the same peak grid size that the default two-round approach reaches on its second
    # round.
    if max_boundary_expansions <= 0:
        return 1.0
    if max_boundary_expansions >= MIN_BOUNDARY_EXPANSION_HORIZON:
        return 1.0
    return float(
        (2**MIN_BOUNDARY_EXPANSION_HORIZON - 1) / (2**max_boundary_expansions - 1)
    )
