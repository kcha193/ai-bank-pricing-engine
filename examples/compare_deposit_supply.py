"""Compare bounded deposit supply profiles on matched synthetic prospects and offers."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
from statistics import fmean, stdev
from typing import Any

import numpy as np

from balance_pricing_engine.application import PricingScenarioRequest, PricingService
from balance_pricing_engine.config import EngineConfig, GridConfig, ProductConfig, load_config
from balance_pricing_engine.config_identity import CONFIG_HASH_BASIS, file_sha256
from balance_pricing_engine.diagnostics import BoundaryDiagnostics, _policy_band_breached
from balance_pricing_engine.features.contracts import FeatureLineage
from balance_pricing_engine.governance.gates import (
    GateStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.governance.lineage import ModelLineage
from balance_pricing_engine.optimization.grid_search import optimization_result_from_row
from balance_pricing_engine.pricing.deposit_supply import (
    ConcentrationMode,
    DepositConcentrationPolicy,
    DepositFlow,
    DepositMarketProfile,
    DepositOpportunitySegment,
    DepositSupplyEstimate,
)
from balance_pricing_engine.pricing_intelligence.run_artifacts import write_json_atomically
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.deposit_supply_simulation import (
    DepositSupplySimulationConfig,
    run_deposit_supply_simulation,
)
from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics

BASE_CONFIG = Path(__file__).resolve().parents[1] / "configs/base.yaml"
SAMPLE_COUNTS = (1000, 5000, 25000)
REPLICATES = 8


def illustrative_profiles() -> dict[str, DepositMarketProfile]:
    """Illustrative annual NZD flows with synthetic, disjoint opportunity pools."""
    external = tuple(
        DepositOpportunitySegment(
            segment_id=segment_id,
            opportunity_nzd=amount,
            channel=channel,
            balance_min=balance_min,
            balance_max=balance_max,
            reachable_share=0.75,
            eligible_participation=0.90,
            conversion_multiplier=0.85,
            funding_eligibility=0.90,
            acquisition_cost_rate=0.0005,
        )
        for segment_id, amount, channel, balance_min, balance_max in (
            ("external_assisted", 380_000_000, "assisted", 0, None),
            ("external_digital_lower", 170_000_000, "digital", 0, 100_000),
            ("external_digital_upper", 90_000_000, "digital", 100_000, None),
        )
    )
    renewal = DepositOpportunitySegment(
        segment_id="renewal",
        opportunity_nzd=80_000_000,
        flow=DepositFlow.RETAINED_MATURITY,
        reachable_share=0.75,
        eligible_participation=0.90,
        conversion_multiplier=0.60,
        funding_eligibility=0.85,
        maturity_runoff_probability=0.55,
        source_interest_rate=0.027,
        acquisition_cost_rate=0.0002,
    )
    migration = DepositOpportunitySegment(
        segment_id="migration",
        opportunity_nzd=60_000_000,
        flow=DepositFlow.INTERNAL_MIGRATION,
        reachable_share=0.75,
        eligible_participation=0.90,
        conversion_multiplier=0.85,
        source_interest_rate=0.027,
    )
    existing = DepositOpportunitySegment(
        segment_id="existing",
        opportunity_nzd=20_000_000,
        flow=DepositFlow.EXISTING_BALANCE,
        reachable_share=0.75,
        eligible_participation=0.90,
        conversion_multiplier=0.85,
        source_interest_rate=0.027,
    )
    opening_sources = (renewal, migration, existing)
    base = DepositMarketProfile(
        profile_id="fixed_market_base",
        version="1.0.0",
        opportunity_basis="FLOW",
        opening_funding_nzd=300_000_000,
        opening_unallocated_interest_rate=0.027,
        segments=(*external, *opening_sources),
        review_notes="Invented annual pools and opening balances for simulation. SME review pending.",
    )
    profiles = {base.profile_id: base}
    for profile_id, field, value, note in (
        ("market_opportunity_low", "opportunity_scale", 0.50, "Half the external market flow."),
        ("market_opportunity_high", "opportunity_scale", 1.50, "1.5 times the external market flow."),
        ("reachable_share_low", "reachable_share", 0.40, "Lower external reachable share."),
        ("reachable_share_high", "reachable_share", 0.95, "Higher external reachable share."),
        ("conversion_low", "conversion_multiplier", 0.55, "Lower external conversion ceiling."),
        ("conversion_high", "conversion_multiplier", 1.00, "Higher external conversion ceiling."),
    ):
        segments = tuple(
            segment.model_copy(update=(
                {"opportunity_nzd": segment.opportunity_nzd * value}
                if field == "opportunity_scale" else {field: value}
            ))
            for segment in external
        )
        profiles[profile_id] = base.model_copy(update={
            "profile_id": profile_id,
            "review_notes": note,
            "segments": (*segments, *opening_sources),
        })

    profiles["renewal_high"] = base.model_copy(update={
        "profile_id": "renewal_high",
        "review_notes": "Higher renewal conversion at the same assumed runoff.",
        "segments": (
            *external, renewal.model_copy(update={"conversion_multiplier": 1}), migration, existing,
        ),
    })
    shift_nzd = 140_000_000
    external_scale = 1 - shift_nzd / math.fsum(segment.opportunity_nzd for segment in external)
    reduced_external = tuple(
        segment.model_copy(update={"opportunity_nzd": segment.opportunity_nzd * external_scale})
        for segment in external
    )
    profiles["cannibalisation_high"] = base.model_copy(update={
        "profile_id": "cannibalisation_high",
        "review_notes": "Shift NZD 140m of external opportunity to internal migration.",
        "segments": (
            *reduced_external, renewal,
            migration.model_copy(update={"opportunity_nzd": migration.opportunity_nzd + shift_nzd}),
            existing,
        ),
    })
    profiles["concentration_cap"] = base.model_copy(update={
        "profile_id": "concentration_cap",
        "review_notes": "Illustrative NZD 100m cap on newly recognised term funding.",
        "concentration": DepositConcentrationPolicy(
            mode=ConcentrationMode.HARD_LIMIT, limit_nzd=100_000_000,
        ),
    })
    return profiles


def _widen(grid: np.ndarray) -> GridConfig:
    """Predeclare a shared, symmetric two-round search envelope on the original step."""
    if grid.size < 2:
        raise ValueError("Comparison grids require at least two rates per product.")
    step = round(float(grid[1] - grid[0]), 6)
    span = float(grid[-1] - grid[0])
    lower_steps = min(int(grid.size - 1) * 3, math.floor((float(grid[0]) + 1e-9) / step))
    return GridConfig(
        start=round(float(grid[0]) - lower_steps * step, 6),
        stop=round(float(grid[-1]) + 3 * span, 6),
        step=step,
    )


def _row(
    diagnostics: ScenarioDiagnostics,
    config: EngineConfig,
    config_hash: str,
    policy_grids: tuple[np.ndarray, np.ndarray],
    model_lineage: dict[str, ModelLineage],
    feature_lineage: dict[str, FeatureLineage],
    *,
    fixed: bool = False,
    estimate: DepositSupplyEstimate | None = None,
) -> dict[str, Any]:
    result = diagnostics.best_result
    diagnostics = replace(
        diagnostics,
        policy_band_breached=_policy_band_breached(
            result, *policy_grids,
            config.market.home_loan_reference_rate, config.market.term_deposit_reference_rate,
        ),
    )
    gates = evaluate_gates(
        config=config, diagnostics=diagnostics, model_lineage=model_lineage,
        feature_lineage=feature_lineage,
    )
    if fixed:
        gates = [gate for gate in gates if gate.code not in {"BOUNDARY_WARNING", "EXPANSION_CAPPED"}]
    reconciled = diagnostics.winner_decomposition().reconciled_total_objective
    if not math.isclose(result.total_objective, reconciled, rel_tol=1e-12, abs_tol=1e-6):
        raise ValueError("Comparison objective failed reconciliation.")
    gross = result.expected_deposit_volume
    row = {
        **asdict(result),
        "return_on_equity": diagnostics.selected_return_on_equity,
        "config_hash": config_hash,
        "sample_count": config.simulation.n_deposit_customers,
        "required_deposit_funding_ratio": config.balance_sheet.required_deposit_funding_ratio,
        "policy_band_breached": diagnostics.policy_band_breached,
        "governance_status": decision_status_from_gates(gates).value,
        "nonpassing_gates": [gate.code for gate in gates if gate.status != GateStatus.PASS],
        "candidate_pairs": len(diagnostics.grid_results),
        "boundary_warning": None if fixed else diagnostics.boundary_diagnostics.has_warning,
        "roe_constraint_relaxed": None if fixed else diagnostics.roe_constraint_relaxed,
        "gross_bookings": gross,
        "net_incremental_funding": None,
        "eligible_incremental_funding": None,
        "usable_funding": gross,
        "gross_interest_cost": result.deposit_interest_cost,
    }
    if estimate is not None:
        row.update(asdict(estimate))
    return row


def _legacy_at_offer(
    diagnostics: ScenarioDiagnostics,
    rates: tuple[float, float],
) -> ScenarioDiagnostics:
    evaluated = diagnostics.grid_results
    fixed = evaluated.loc[(evaluated["loan_rate"] == rates[0]) & (evaluated["deposit_rate"] == rates[1])]
    if len(fixed) != 1:
        raise ValueError("The canonical offer must appear once in the common pricing grid.")
    return replace(
        diagnostics,
        grid_results=fixed,
        best_result=optimization_result_from_row(fixed.iloc[0]),
        selected_return_on_equity=float(fixed["return_on_equity"].iloc[0]),
        boundary_diagnostics=BoundaryDiagnostics(()),
        initial_boundary_diagnostics=BoundaryDiagnostics(()),
        expansion_rounds=0,
        expansion_capped=False,
        roe_constraint_relaxed=False,
    )


def build_report(config_path: Path = BASE_CONFIG) -> dict[str, Any]:
    """Evaluate common-rate counterfactuals and pick winners on identical grids."""
    canonical = load_config(config_path)
    if canonical.pricing_strategy is None or (
        canonical.competitor_rates is not None and canonical.competitor_rates.enabled
    ):
        raise ValueError("Comparison requires market-relative joint pricing and static rates.")

    pricing = PricingService()
    native = pricing.run_scenario(PricingScenarioRequest(config=canonical))
    fixed_rates = (native.best_loan_rate, native.best_deposit_rate)
    policy_grids = build_configured_rate_grids(canonical)
    loan_grid, deposit_grid = map(_widen, policy_grids)
    common = canonical.model_copy(update={
        "pricing_strategy": None,
        "products": ProductConfig(loan_rate_grid=loan_grid, deposit_rate_grid=deposit_grid),
    })
    common_grids = build_configured_rate_grids(common)

    def legacy_comparison(config: EngineConfig) -> dict[str, Any]:
        legacy = pricing.run_scenario(PricingScenarioRequest(config=config, max_boundary_expansions=0))
        diagnostics = legacy.diagnostics
        if not native.diagnostics.loan_customers.equals(diagnostics.loan_customers):
            raise ValueError("Legacy comparisons must retain the canonical loan sample.")
        capacity = float(diagnostics.deposit_customers["deposit_amount"].sum())
        rows = {}
        for mode, evaluated in (
            ("fixed_offer", _legacy_at_offer(diagnostics, fixed_rates)),
            ("reoptimised", diagnostics),
        ):
            rows[mode] = {
                **_row(
                    evaluated, config, legacy.decision_record.config_hash, policy_grids,
                    legacy.model_lineage, legacy.feature_lineage, fixed=mode == "fixed_offer",
                ),
                "addressable_flow": None,
                "eligible_opportunity": capacity,
                "opportunity_basis": "Sum of simulated balances under the legacy assumption.",
            }
        return rows

    def supply_row(
        config: EngineConfig, profile: DepositMarketProfile, *,
        fixed: bool, seed: int | None = None,
    ) -> dict[str, Any]:
        simulation = run_deposit_supply_simulation(DepositSupplySimulationConfig(
            engine_config=config, profile=profile, max_boundary_expansions=0,
            fixed_rates=fixed_rates if fixed else None, deposit_sample_seed=seed,
        ))
        row = _row(
            simulation.diagnostics, config, simulation.config_hash, policy_grids,
            {}, {}, fixed=fixed, estimate=simulation.estimate,
        )
        if fixed and row["expected_credit_loss"] != native.diagnostics.best_result.expected_credit_loss:
            raise ValueError("Fixed-offer credit loss changed across matched simulations.")
        row.update(
            asdict(simulation.funding_value),
            economic_funding_value=simulation.funding_value.economic_funding_value,
            deposit_sample_seed=seed,
            profile_id=profile.profile_id,
            profile_version=profile.version,
        )
        if simulation.concentration_comparison is not None:
            row["concentration_comparison"] = asdict(simulation.concentration_comparison)
        return row

    def supply_comparison(
        config: EngineConfig, profile: DepositMarketProfile, seed: int | None = None,
    ) -> dict[str, Any]:
        return {
            "fixed_offer": supply_row(config, profile, fixed=True, seed=seed),
            "reoptimised": supply_row(config, profile, fixed=False, seed=seed),
        }

    def with_sample_count(count: int) -> EngineConfig:
        return common.model_copy(update={
            "simulation": common.simulation.model_copy(update={"n_deposit_customers": count}),
        })

    profiles = illustrative_profiles()
    base = profiles["fixed_market_base"]
    scenarios = {"legacy": legacy_comparison(common)}
    scenarios.update({
        name: supply_comparison(common, profile) for name, profile in profiles.items()
    })
    stress = common.model_copy(update={
        "balance_sheet": common.balance_sheet.model_copy(update={
            "required_deposit_funding_ratio": 0.85,
        }),
    })
    scenarios["funding_ratio_stress"] = supply_comparison(stress, profiles["market_opportunity_low"])
    independent_replicates = []
    for count in SAMPLE_COUNTS:
        config = with_sample_count(count)
        same_as_base = count == common.simulation.n_deposit_customers
        scenarios[f"sample_{count}"] = (
            scenarios["fixed_market_base"] if same_as_base else supply_comparison(config, base)
        )
        if not same_as_base:
            scenarios[f"legacy_sample_{count}"] = legacy_comparison(config)
        observations = []
        for seed in range(count + 1, count + REPLICATES + 1):
            row = supply_row(config, base, fixed=True, seed=seed)
            observations.append({
                "deposit_sample_seed": seed,
                "fixed_offer_gross_bookings": row["gross_bookings"],
                "fixed_offer_net_incremental_funding": row["net_incremental_funding"],
                "fixed_offer_objective": row["total_objective"],
            })
        bookings = [row["fixed_offer_gross_bookings"] for row in observations]
        independent_replicates.append({
            "sample_count": count,
            "eligible_opportunity": row["eligible_opportunity"],
            "fixed_offer_gross_bookings_mean": fmean(bookings),
            "fixed_offer_gross_bookings_sample_sd": stdev(bookings),
            "observations": observations,
        })

    return {
        "schema_version": 1,
        "classification": "SYNTHETIC / UNCALIBRATED",
        "empirically_validated": False,
        "approved_for_pricing": False,
        "config_hash_basis": CONFIG_HASH_BASIS,
        "source_config_file_sha256": file_sha256(config_path),
        "matched_inputs": {
            "loan_seed": canonical.random_seed,
            "default_deposit_sampling": "Legacy random stream after generating the loan sample.",
            "loan_sample_count": canonical.simulation.n_loan_customers,
            "credit_loss": "Canonical synthetic expected_loss_rate.",
            "market": canonical.market.model_dump(mode="json"),
            "balance_sheet": canonical.balance_sheet.model_dump(mode="json"),
            "response": canonical.response.model_dump(mode="json"),
            "loan_policy_band": [float(policy_grids[0][0]), float(policy_grids[0][-1])],
            "deposit_policy_band": [float(policy_grids[1][0]), float(policy_grids[1][-1])],
            "common_loan_grid": common_grids[0].tolist(),
            "common_deposit_grid": common_grids[1].tolist(),
            "reoptimised_candidate_pairs": len(common_grids[0]) * len(common_grids[1]),
            "fixed_rates": list(fixed_rates),
            "max_boundary_expansions_on_common_grid": 0,
        },
        "legacy_adaptive": {
            **_row(
                native.diagnostics, canonical, native.decision_record.config_hash, policy_grids,
                native.model_lineage, native.feature_lineage,
            ),
            "expansion_rounds": native.expansion_rounds,
            "expansion_capped": native.expansion_capped,
        },
        "profiles": {name: profile.model_dump(mode="json") for name, profile in profiles.items()},
        "scenarios": scenarios,
        "sampling": {
            "design": "Sweep sample count at fixed profile. Resample deposits only in each replicate.",
            "independent_replicates": independent_replicates,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    output = parser.parse_args().output
    if output is not None and (
        output.resolve() == BASE_CONFIG.resolve()
        or (output.exists() and output.samefile(BASE_CONFIG))
    ):
        raise ValueError("Output cannot replace the source configuration.")
    report = build_report()
    validated_json = json.dumps(report, indent=2, allow_nan=False)
    if output is None:
        print(validated_json)
    else:
        write_json_atomically(output, report)


if __name__ == "__main__":
    main()
