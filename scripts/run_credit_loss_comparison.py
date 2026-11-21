"""Compare annual mortgage loss models on a matched synthetic portfolio, offline."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from balance_pricing_engine.application import PricingScenarioRequest, PricingService
from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.config_identity import CONFIG_HASH_BASIS, file_sha256
from balance_pricing_engine.diagnostics import objective_decomposition_from_result
from balance_pricing_engine.optimization.grid_search import (
    OptimizationResult,
    evaluate_joint_rate_grid,
    optimization_result_from_row,
)
from balance_pricing_engine.pricing.credit_loss import illustrative_profile
from balance_pricing_engine.pricing.profit import maturity_adjusted_rwa_weight
from balance_pricing_engine.pricing_intelligence.run_artifacts import write_json_atomically
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.credit_loss_simulation import (
    CreditLossSimulationConfig,
    run_credit_loss_simulation,
)
from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import loan_acceptance_probability

BASE_CONFIG = Path(__file__).resolve().parents[1] / "configs/base.yaml"
MAX_BOUNDARY_EXPANSIONS = 2


def _fixed_offer(
    config: EngineConfig,
    loans: pd.DataFrame,
    deposits: pd.DataFrame,
    offers: tuple[float, float],
) -> tuple[OptimizationResult, float]:
    response = config.response
    balance = config.balance_sheet
    market = MarketState(**config.market.model_dump())
    row = evaluate_joint_rate_grid(
        loan_customers=loans,
        deposit_customers=deposits,
        market=market,
        loan_rate_grid=np.array([offers[0]]),
        deposit_rate_grid=np.array([offers[1]]),
        loan_rate_elasticity=response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=response.deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=balance.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=balance.funding_shortfall_penalty_rate,
        rwa_weight=maturity_adjusted_rwa_weight(
            balance.loan_rwa_weight, balance.average_loan_tenor_years
        ),
        capital_hurdle_rate=balance.capital_hurdle_rate,
        home_loan_reference_rate=market.home_loan_reference_rate,
        term_deposit_reference_rate=market.term_deposit_reference_rate,
        response_coefficients=response.coefficients,
    ).iloc[0]
    return optimization_result_from_row(row), float(row["return_on_equity"])


def _loss_attribution(
    config: EngineConfig,
    loans: pd.DataFrame,
    annual_losses: np.ndarray,
    bands: np.ndarray,
    band_names: tuple[str, ...],
    loan_rate: float,
) -> dict[str, Any]:
    response = config.response
    acceptance = loan_acceptance_probability(
        loans, loan_rate, config.market.competitor_loan_rate,
        response.loan_rate_elasticity, response.loan_competitor_gap_elasticity,
        coefficients=response.coefficients.loan,
    )
    principal = loans["loan_amount"].to_numpy()
    expected_principal = acceptance * principal
    expected_loss = acceptance * annual_losses
    rows = []
    for name in band_names:
        in_band = bands == name
        booked = float(expected_principal[in_band].sum())
        loss = float(expected_loss[in_band].sum())
        rows.append({
            "band": name,
            "prospect_count": int(in_band.sum()),
            "loan_opportunity_volume": float(principal[in_band].sum()),
            "expected_loan_volume": booked,
            "expected_credit_loss": loss,
            "booked_balance_weighted_annual_loss_rate": loss / booked if booked else None,
        })

    positive = principal > 0
    opportunity = float(principal.sum())
    booked = float(expected_principal.sum())
    return {
        "mean_annual_loss_rate": (
            float(np.mean(annual_losses[positive] / principal[positive]))
            if positive.any() else None
        ),
        "opportunity_balance_weighted_annual_loss_rate": (
            float(annual_losses.sum()) / opportunity if opportunity else None
        ),
        "booked_balance_weighted_annual_loss_rate": (
            float(expected_loss.sum()) / booked if booked else None
        ),
        "lvr_bands": rows,
    }


def _result_row(
    result: OptimizationResult,
    return_on_equity: float | None,
    config_hash: str,
    governance_status: str,
    policy_band_breached: bool,
    loss: dict[str, Any],
    diagnostics: ScenarioDiagnostics | None = None,
) -> dict[str, Any]:
    objective = objective_decomposition_from_result(result).reconciled_total_objective
    if not math.isclose(result.total_objective, objective, rel_tol=1e-12, abs_tol=1e-6):
        raise ValueError("Comparison objective failed reconciliation.")
    attributed_loss = math.fsum(row["expected_credit_loss"] for row in loss["lvr_bands"])
    if not math.isclose(result.expected_credit_loss, attributed_loss, rel_tol=1e-12, abs_tol=1e-6):
        raise ValueError("Comparison LVR losses failed reconciliation.")

    row = {
        **asdict(result),
        "return_on_equity": return_on_equity,
        "config_hash": config_hash,
        "governance_status": governance_status,
        "policy_band_breached": policy_band_breached,
        "loss": loss,
    }
    if diagnostics is not None:
        row.update(
            final_grid_size=len(diagnostics.grid_results),
            initial_boundary_warning_flag=diagnostics.initial_boundary_diagnostics.has_warning,
            boundary_warning_flag=diagnostics.boundary_diagnostics.has_warning,
            expansion_rounds=diagnostics.expansion_rounds,
            expansion_capped=diagnostics.expansion_capped,
            roe_constraint_relaxed=diagnostics.roe_constraint_relaxed,
        )
    return row


def _comparison(legacy: dict[str, Any], alternate: dict[str, Any]) -> dict[str, Any]:
    return {
        "legacy_synthetic": legacy,
        "pd_lgd_ead": alternate,
        "pd_lgd_ead_minus_legacy": {
            key: alternate[key] - legacy[key]
            for key in ("expected_loan_volume", "expected_credit_loss", "total_objective")
        },
    }


def build_report(config_path: Path = BASE_CONFIG) -> dict[str, Any]:
    """Vary only credit loss, holding the seeded populations and pricing inputs fixed."""
    config = load_config(config_path)
    if config.pricing_strategy is None or (
        config.competitor_rates is not None and config.competitor_rates.enabled
    ):
        raise ValueError("Comparison requires market-relative joint pricing and static rates.")

    legacy = PricingService().run_scenario(PricingScenarioRequest(
        config=config, max_boundary_expansions=MAX_BOUNDARY_EXPANSIONS,
    ))
    profile = illustrative_profile()
    alternate = run_credit_loss_simulation(CreditLossSimulationConfig(
        engine_config=config, profile=profile, max_boundary_expansions=MAX_BOUNDARY_EXPANSIONS,
    ))
    legacy_loans = legacy.diagnostics.loan_customers
    alternate_loans = alternate.diagnostics.loan_customers
    deposits = legacy.diagnostics.deposit_customers
    if not legacy_loans.equals(alternate_loans[legacy_loans.columns]) or not deposits.equals(
        alternate.diagnostics.deposit_customers
    ):
        raise ValueError("Comparison requires identical seeded loan and deposit prospects.")
    legacy_losses = (legacy_loans["expected_loss_rate"] * legacy_loans["loan_amount"]).to_numpy()
    estimate = alternate.loss_estimate
    band_names = (*(band.name for band in profile.bands), profile.unknown_band_name)
    rows: dict[str, list[dict[str, Any]]] = {"fixed_offer": [], "reoptimised": []}
    offers = (legacy.best_loan_rate, legacy.best_deposit_rate)
    rate_columns = ["loan_rate", "deposit_rate"]
    shared_final_grid = legacy.diagnostics.grid_results[rate_columns].equals(
        alternate.diagnostics.grid_results[rate_columns]
    )
    loan_grid, deposit_grid = build_configured_rate_grids(config)
    fixed_offer_breaches_policy = legacy.diagnostics.policy_band_breached

    for loans, losses, diagnostics, config_hash, status in (
        (
            legacy_loans, legacy_losses, legacy.diagnostics, legacy.decision_record.config_hash,
            legacy.governance.status.value,
        ),
        (
            alternate_loans, estimate.annual_expected_loss_nzd, alternate.diagnostics,
            alternate.config_hash, alternate.governance_status.value,
        ),
    ):
        fixed, fixed_roe = _fixed_offer(config, loans, deposits, offers)
        rows["fixed_offer"].append(_result_row(
            fixed, fixed_roe, config_hash, "NOT_ASSESSED", fixed_offer_breaches_policy,
            _loss_attribution(
                config, loans, losses, estimate.band_names, band_names, fixed.loan_rate
            ),
        ))
        winner = diagnostics.best_result
        rows["reoptimised"].append(_result_row(
            winner, diagnostics.selected_return_on_equity, config_hash, status,
            diagnostics.policy_band_breached,
            _loss_attribution(
                config, loans, losses, estimate.band_names, band_names, winner.loan_rate
            ),
            diagnostics=diagnostics,
        ))

    return {
        "schema_version": 1,
        "classification": profile.evidence_label,
        "empirically_validated": False,
        "approved_for_pricing": False,
        "config_hash_basis": CONFIG_HASH_BASIS,
        "source_config_file_sha256": file_sha256(config_path),
        "matched_inputs": {
            "random_seed": config.random_seed,
            "population": config.simulation.model_dump(mode="json"),
            "market": config.market.model_dump(mode="json"),
            "balance_sheet": config.balance_sheet.model_dump(mode="json"),
            "response": config.response.model_dump(mode="json"),
            "initial_loan_grid": loan_grid.tolist(),
            "initial_deposit_grid": deposit_grid.tolist(),
            "max_boundary_expansions": MAX_BOUNDARY_EXPANSIONS,
        },
        "profile": profile.model_dump(mode="json"),
        "fixed_offer_basis": "Legacy optimiser winner evaluated under both loss models.",
        "fixed_offer_governance_basis": "Fixed offers are economic counterfactuals.",
        "alternate_simulation_governance_status": alternate.governance_status.value,
        "fixed_offer": _comparison(*rows["fixed_offer"]),
        "reoptimised_shared_final_grid": shared_final_grid,
        "reoptimised": _comparison(*rows["reoptimised"]),
    }


def write_report(config_path: Path, output_path: Path, report: dict[str, Any]) -> None:
    """Write deterministic JSON, preserving the source configuration."""
    if output_path.resolve() == config_path.resolve() or (
        output_path.exists() and output_path.samefile(config_path)
    ):
        raise ValueError("Output cannot replace the source configuration.")
    json.dumps(report, allow_nan=False)
    write_json_atomically(output_path, report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_report(BASE_CONFIG, args.output, build_report())


if __name__ == "__main__":
    main()
