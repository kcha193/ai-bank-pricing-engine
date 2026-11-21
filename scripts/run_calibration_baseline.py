"""Run the offline, synthetic Phase 4A calibration baseline and sensitivities."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from balance_pricing_engine.application import PricingScenarioRequest, PricingService
from balance_pricing_engine.config import EngineConfig, load_config, revalidation_payload
from balance_pricing_engine.config_identity import CONFIG_HASH_BASIS, effective_config_hash
from balance_pricing_engine.diagnostics import (
    BoundaryDiagnostics,
    evaluate_adaptive_joint_rate_grid,
)
from balance_pricing_engine.features.local_store import LocalFeatureStore
from balance_pricing_engine.governance.gates import (
    GateResult,
    GateStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.governance.lineage import synthetic_model_lineage
from balance_pricing_engine.optimization.grid_search import (
    evaluate_joint_rate_grid,
    optimization_result_from_row,
)
from balance_pricing_engine.pricing.profit import (
    TIER1_CAPITAL_RATIO,
    maturity_adjusted_rwa_weight,
)
from balance_pricing_engine.pricing_intelligence.run_artifacts import write_json_atomically
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)

BASE_CONFIG = Path(__file__).resolve().parents[1] / "configs/base.yaml"
CLASSIFICATION = "SYNTHETIC / UNCALIBRATED"
ILLUSTRATIVE = "Illustrative Phase 4A sensitivity; no empirical input"
AUDIT_COUNTS = "Illustrative prospect counts reproduced from the November 13 audit, FIN-001"


@dataclass(frozen=True)
class Case:
    scenario_id: str
    overrides: tuple[tuple[str, float | int], ...] = ()
    ecl_multiplier: float = 1.0
    deposit_balance_multiplier: float = 1.0
    fixed_rates: tuple[float, float] | None = None
    source: str = ILLUSTRATIVE


def _config_field(payload: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    *parents, field = path.split(".")
    for key in parents:
        payload = payload.get(key)
        if not isinstance(payload, dict):
            raise ValueError(f"Unknown config field: {path}")
    if field not in payload:
        raise ValueError(f"Unknown config field: {path}")
    return payload, field


def case_config(base: EngineConfig, case: Case) -> EngineConfig:
    """Validate explicit scenario inputs and keep the canonical model immutable."""
    for factor in (case.ecl_multiplier, case.deposit_balance_multiplier):
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("Multipliers must be finite and positive.")
    if case.fixed_rates is not None and (
        len(case.fixed_rates) != 2 or not all(0 <= rate <= 1 for rate in case.fixed_rates)
    ):
        raise ValueError("Fixed offers must be a pair of rates between zero and one.")
    payload = revalidation_payload(base)
    paths = [path for path, _ in case.overrides]
    if len(set(paths)) != len(paths):
        raise ValueError("A config field can be overridden only once per case.")
    for path, value in case.overrides:
        container, field = _config_field(payload, path)
        container[field] = value
    config = EngineConfig.model_validate(payload)
    if config.segmented_pricing is not None or (
        config.competitor_rates is not None and config.competitor_rates.enabled
    ):
        raise ValueError("Analysis requires a synthetic portfolio and static competitor inputs.")
    if config.random_seed != base.random_seed:
        raise ValueError("The analysis uses the same random seed for every case.")
    if _uses_experimental_grid(case) and config.simulation != base.simulation:
        raise ValueError("Experimental grids reuse the canonical prospect samples.")
    return config


def _uses_experimental_grid(case: Case) -> bool:
    return (
        case.ecl_multiplier != 1
        or case.deposit_balance_multiplier != 1
        or case.fixed_rates is not None
    )


def _rate_delta(previous: float, current: float) -> float:
    return float((Decimal(str(current)) - Decimal(str(previous))) * 10_000)


def _case_definitions(
    base: EngineConfig, selected: tuple[float, float]
) -> tuple[list[Case], dict[str, tuple[str, str, str, str]], dict[str, Any]]:
    def override(name: str, path: str, value: float | int, source: str = ILLUSTRATIVE) -> Case:
        return Case(name, ((path, value),), source=source)

    spread = base.market.wholesale_funding_spread
    intercept = base.response.coefficients.deposit.intercept
    reference_curve = {6: Decimal("0.030"), 12: Decimal("0.032"), 24: Decimal("0.034")}
    liquidity_premium = Decimal("0.010")
    ftp_tenor = 12
    ftp_rate = reference_curve[ftp_tenor] + liquidity_premium
    ftp_curve = {
        "classification": "ILLUSTRATIVE SENSITIVITY ONLY",
        "tenor_months": list(reference_curve),
        "reference_rates": [float(rate) for rate in reference_curve.values()],
        "liquidity_premium": float(liquidity_premium),
        "selected_tenor_months": ftp_tenor,
        "selected_total_rate": float(ftp_rate),
        "treatment": "12m point affects wholesale cost; OCR and TD expense formula held fixed",
    }
    ecl = Case("ecl_half", ecl_multiplier=0.5)
    supply = Case("deposit_market_double", deposit_balance_multiplier=2.0)
    funding = override("funding_ratio_high", "balance_sheet.required_deposit_funding_ratio", 0.80)
    wholesale = override("wholesale_high", "market.wholesale_funding_spread", spread + 0.010)
    ftp = override(
        "ftp_12m",
        "market.wholesale_funding_spread",
        float(ftp_rate - Decimal(str(base.market.cash_rate))),
        "Illustrative 12m curve point plus liquidity premium; see illustrative_ftp_curve",
    )
    capital = override("rwa_high", "balance_sheet.loan_rwa_weight", 0.45)
    cases = [
        ecl,
        Case("ecl_150pct", ecl_multiplier=1.5),
        override("prospects_10000", "simulation.n_deposit_customers", 10000, AUDIT_COUNTS),
        override("prospects_25000", "simulation.n_deposit_customers", 25000, AUDIT_COUNTS),
        Case("deposit_market_half", deposit_balance_multiplier=0.5),
        supply,
        override("conversion_low", "response.coefficients.deposit.intercept", intercept - 0.50),
        override("conversion_high", "response.coefficients.deposit.intercept", intercept + 0.50),
        override("funding_ratio_zero", "balance_sheet.required_deposit_funding_ratio", 0, AUDIT_COUNTS),
        funding,
        override("wholesale_low", "market.wholesale_funding_spread", spread - 0.005),
        wholesale,
        ftp,
        override("rwa_low", "balance_sheet.loan_rwa_weight", 0.25),
        capital,
        override("capital_hurdle_low", "balance_sheet.capital_hurdle_rate", 0.08),
        override("capital_hurdle_high", "balance_sheet.capital_hurdle_rate", 0.16),
    ]
    for product, reference in (("loan", "home_loan"), ("deposit", "term_deposit")):
        competitor = f"competitor_{product}_rate"
        anchor = f"{reference}_reference_rate"
        for sign, suffix in ((-1, "down"), (1, "up")):
            shock = Decimal(sign) * Decimal("0.005")
            cases.append(Case(f"{product}_benchmark_{suffix}", (
                (f"market.{competitor}", float(Decimal(str(getattr(base.market, competitor))) + shock)),
                (f"market.{anchor}", float(Decimal(str(getattr(base.market, anchor))) + shock)),
            )))
    mortgage, deposit = (Decimal(str(rate)) for rate in selected)
    lower_mortgage = (float(mortgage - Decimal("0.0025")), float(deposit))
    higher_deposit = (float(mortgage), float(deposit + Decimal("0.0015")))
    cases.extend([
        replace(ecl, scenario_id="ecl_half_wholesale_high", overrides=wholesale.overrides),
        replace(supply, scenario_id="deposit_market_double_funding_high", overrides=funding.overrides),
        Case("fixed_base", fixed_rates=selected),
        Case("fixed_lower_mortgage", fixed_rates=lower_mortgage),
        Case("fixed_higher_deposit", fixed_rates=higher_deposit),
        replace(supply, scenario_id="fixed_deposit_market_double", fixed_rates=selected),
        replace(supply, scenario_id="fixed_deposit_supply_lower_mortgage", fixed_rates=lower_mortgage),
        replace(ftp, scenario_id="fixed_ftp_12m", fixed_rates=selected),
        replace(ftp, scenario_id="fixed_ftp_higher_deposit", fixed_rates=higher_deposit),
        replace(capital, scenario_id="fixed_rwa_high", fixed_rates=selected),
        replace(capital, scenario_id="fixed_capital_lower_mortgage", fixed_rates=lower_mortgage),
    ])
    groups = {
        "ecl_x_funding_cost": (
            "baseline", "ecl_half", "wholesale_high", "ecl_half_wholesale_high",
        ),
        "deposit_supply_x_funding_requirement": (
            "baseline", "deposit_market_double", "funding_ratio_high",
            "deposit_market_double_funding_high",
        ),
        "deposit_supply_x_mortgage_rate": (
            "fixed_base", "fixed_deposit_market_double", "fixed_lower_mortgage",
            "fixed_deposit_supply_lower_mortgage",
        ),
        "ftp_x_deposit_pricing": (
            "fixed_base", "fixed_ftp_12m", "fixed_higher_deposit", "fixed_ftp_higher_deposit",
        ),
        "capital_x_mortgage_rate": (
            "fixed_base", "fixed_rwa_high", "fixed_lower_mortgage", "fixed_capital_lower_mortgage",
        ),
    }
    return cases, groups, ftp_curve


def _experimental_diagnostics(
    config: EngineConfig, case: Case, canonical: ScenarioDiagnostics
) -> tuple[ScenarioDiagnostics, list[GateResult]]:
    loans = canonical.loan_customers.assign(
        expected_loss_rate=canonical.loan_customers["expected_loss_rate"] * case.ecl_multiplier
    )
    deposits = canonical.deposit_customers.assign(
        deposit_amount=canonical.deposit_customers["deposit_amount"] * case.deposit_balance_multiplier
    )
    if loans["expected_loss_rate"].max() > 1 or not np.isfinite(deposits["deposit_amount"]).all():
        raise ValueError("A multiplier pushes losses or deposit balances outside their domain.")
    loan_grid, deposit_grid = build_configured_rate_grids(config)
    market = MarketState(**config.market.model_dump())
    balance = config.balance_sheet
    response = config.response
    inputs = dict(
        loan_customers=loans,
        deposit_customers=deposits,
        market=market,
        loan_rate_elasticity=response.loan_rate_elasticity,
        loan_competitor_gap_elasticity=response.loan_competitor_gap_elasticity,
        deposit_rate_elasticity=response.deposit_rate_elasticity,
        deposit_competitor_gap_elasticity=response.deposit_competitor_gap_elasticity,
        required_deposit_funding_ratio=balance.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=balance.funding_shortfall_penalty_rate,
        rwa_weight=maturity_adjusted_rwa_weight(balance.loan_rwa_weight, balance.average_loan_tenor_years),
        capital_hurdle_rate=balance.capital_hurdle_rate,
        home_loan_reference_rate=market.home_loan_reference_rate,
        term_deposit_reference_rate=market.term_deposit_reference_rate,
        response_coefficients=response.coefficients,
    )
    if case.fixed_rates is None:
        search = evaluate_adaptive_joint_rate_grid(
            **inputs, loan_rate_grid=loan_grid, deposit_rate_grid=deposit_grid,
            min_return_on_equity=balance.min_return_on_equity,
        )
        diagnostics = ScenarioDiagnostics(**vars(search))
    else:
        loan_rate, deposit_rate = case.fixed_rates
        evaluated = evaluate_joint_rate_grid(
            **inputs, loan_rate_grid=np.array([loan_rate]), deposit_rate_grid=np.array([deposit_rate]),
        )
        row = evaluated.iloc[0]
        diagnostics = ScenarioDiagnostics(
            best_result=optimization_result_from_row(row),
            grid_results=evaluated,
            boundary_diagnostics=BoundaryDiagnostics(()),
            initial_boundary_diagnostics=BoundaryDiagnostics(()),
            policy_band_breached=not (
                loan_grid[0] <= loan_rate <= loan_grid[-1]
                and deposit_grid[0] <= deposit_rate <= deposit_grid[-1]
            ),
            selected_return_on_equity=float(row["return_on_equity"]),
        )

    features = LocalFeatureStore()
    _, loan_lineage = features.serving_features(
        "loan", loans, offered_rate=market.home_loan_reference_rate,
        competitor_rate=market.competitor_loan_rate,
    )
    _, deposit_lineage = features.serving_features(
        "deposit", deposits, offered_rate=market.term_deposit_reference_rate,
        competitor_rate=market.competitor_deposit_rate,
    )
    gates = evaluate_gates(
        config=config,
        diagnostics=diagnostics,
        model_lineage={product: synthetic_model_lineage(product) for product in ("loan", "deposit")},
        feature_lineage={"loan": loan_lineage, "deposit": deposit_lineage},
    )
    if case.fixed_rates is not None:
        # The fixed counterfactual has no optimiser edge or expansion to assess.
        gates = [gate for gate in gates if gate.code not in {"BOUNDARY_WARNING", "EXPANSION_CAPPED"}]
    return diagnostics, gates


def _perturbations(base: EngineConfig, case: Case, baseline: dict[str, Any]) -> dict[str, Any]:
    payload = revalidation_payload(base)
    changes = {}
    for path, value in case.overrides:
        container, field = _config_field(payload, path)
        changes[path] = {"base": container[field], "case": value}
    for field in ("ecl_multiplier", "deposit_balance_multiplier"):
        value = getattr(case, field)
        if value != 1:
            changes[field] = {"base": 1.0, "case": value}
    if case.fixed_rates is not None:
        for field, rate in zip(("loan_rate", "deposit_rate"), case.fixed_rates):
            if rate != baseline[field]:
                changes[field] = {"base": baseline[field], "case": rate}
    return changes


def _result_row(
    config: EngineConfig,
    case: Case,
    diagnostics: ScenarioDiagnostics,
    gates: list[GateResult],
    config_hash: str,
    perturbations: dict[str, Any],
) -> dict[str, Any]:
    result = diagnostics.best_result
    decomposition = diagnostics.winner_decomposition()
    if not math.isclose(
        result.total_objective, decomposition.reconciled_total_objective,
        rel_tol=1e-12, abs_tol=1e-6,
    ):
        raise ValueError(f"Objective failed reconciliation for {case.scenario_id}.")
    loan_grid, deposit_grid = build_configured_rate_grids(config)
    fixed = case.fixed_rates is not None
    balance = config.balance_sheet
    capital = result.expected_loan_volume * maturity_adjusted_rwa_weight(
        balance.loan_rwa_weight, balance.average_loan_tenor_years
    ) * TIER1_CAPITAL_RATIO
    return {
        "scenario_id": case.scenario_id,
        "mode": "fixed_offers" if fixed else "adaptive_search",
        "assumption_source": case.source,
        "perturbations": perturbations,
        "config_hash": config_hash,
        "n_loan_customers": config.simulation.n_loan_customers,
        "n_deposit_customers": config.simulation.n_deposit_customers,
        "home_loan_reference_rate": config.market.home_loan_reference_rate,
        "term_deposit_reference_rate": config.market.term_deposit_reference_rate,
        "home_loan_policy_band": [float(loan_grid[0]), float(loan_grid[-1])],
        "term_deposit_policy_band": [float(deposit_grid[0]), float(deposit_grid[-1])],
        "wholesale_funding_rate": config.market.cash_rate + config.market.wholesale_funding_spread,
        "required_deposit_funding_ratio": balance.required_deposit_funding_ratio,
        "required_deposits": result.expected_loan_volume * balance.required_deposit_funding_ratio,
        "required_tier1_capital": capital,
        **asdict(result),
        "ecl_as_fraction_of_loan_volume": (
            result.expected_credit_loss / result.expected_loan_volume
            if result.expected_loan_volume > 0 else None
        ),
        "return_on_equity": diagnostics.selected_return_on_equity,
        "governance_status": decision_status_from_gates(gates).value,
        "nonpassing_gate_codes": [gate.code for gate in gates if gate.status != GateStatus.PASS],
        "policy_band_breached": diagnostics.policy_band_breached,
        "roe_constraint_relaxed": None if fixed else diagnostics.roe_constraint_relaxed,
        "initial_grid_size": None if fixed else int(loan_grid.size * deposit_grid.size),
        "final_grid_size": len(diagnostics.grid_results),
        "initial_boundary_warning_flag": (
            None if fixed else diagnostics.initial_boundary_diagnostics.has_warning
        ),
        "boundary_warning_flag": None if fixed else diagnostics.boundary_diagnostics.has_warning,
        "expansion_rounds": None if fixed else diagnostics.expansion_rounds,
        "expansion_capped": None if fixed else diagnostics.expansion_capped,
    }


def _ecl_analysis(config: EngineConfig, diagnostic: ScenarioDiagnostics) -> dict[str, Any]:
    loans = diagnostic.loan_customers
    result = diagnostic.best_result
    response = config.response
    loss_rate = loans["expected_loss_rate"].to_numpy()
    amounts = loans["loan_amount"].to_numpy()
    booked = amounts * loan_acceptance_probability(
        loans, result.loan_rate, config.market.competitor_loan_rate,
        response.loan_rate_elasticity, response.loan_competitor_gap_elasticity,
        response.coefficients.loan,
    )
    summary = {
        label: float(np.quantile(loss_rate, quantile))
        for label, quantile in (("min", 0), ("p05", .05), ("median", .5), ("p95", .95), ("max", 1))
    }
    lvr = loans["lvr"].to_numpy()
    bands = []
    for low, high in ((0., .60), (.60, .80), (.80, .90), (.90, 1.00)):
        mask = (lvr >= low) & ((lvr <= high) if high == 1 else (lvr < high))
        booked_volume = float(booked[mask].sum())
        expected_loss = float(np.dot(booked[mask], loss_rate[mask]))
        opportunity = float(amounts[mask].sum())
        bands.append({
            "lower_lvr_inclusive": low,
            "upper_lvr": high,
            "upper_lvr_inclusive": high == 1,
            "prospect_count": int(mask.sum()),
            "loan_opportunity_volume": opportunity,
            "expected_loan_volume": booked_volume,
            "expected_credit_loss": expected_loss,
            "opportunity_balance_weighted_ecl_rate": (
                float(np.dot(amounts[mask], loss_rate[mask]) / opportunity) if opportunity else None
            ),
            "booked_balance_weighted_ecl_rate": expected_loss / booked_volume if booked_volume else None,
        })
    booked_weighted = float(np.dot(booked, loss_rate) / booked.sum())
    return {
        "prospect_rate_distribution": {**summary, "mean": float(loss_rate.mean())},
        "loan_opportunity_volume": float(amounts.sum()),
        "opportunity_balance_weighted_ecl_rate": float(np.average(loss_rate, weights=amounts)),
        "booked_balance_weighted_ecl_rate": booked_weighted,
        "lvr_bands": bands,
    }


def _deposit_analysis(config: EngineConfig, diagnostic: ScenarioDiagnostics) -> dict[str, Any]:
    deposits = diagnostic.deposit_customers
    amounts = deposits["deposit_amount"].to_numpy()
    total = float(amounts.sum())
    response = config.response
    analysis = {
        "opportunity_volume": total,
        "top_ten_prospect_balance_share": float(np.sort(amounts)[-10:].sum() / total),
    }
    for name, offer in (
        ("model_best", diagnostic.best_result.deposit_rate),
        ("reference_offer", config.market.term_deposit_reference_rate),
    ):
        uptake = deposit_uptake_probability(
            deposits, offer, config.market.competitor_deposit_rate, response.deposit_rate_elasticity,
            response.deposit_competitor_gap_elasticity, response.coefficients.deposit,
        )
        expected_balances = amounts * uptake
        booked = float(expected_balances.sum())
        analysis[name] = {
            "offer_rate": offer,
            "prospect_mean_conversion": float(uptake.mean()),
            "expected_deposit_volume": booked,
            "balance_weighted_conversion": booked / total,
            "top_ten_expected_balance_share": float(np.sort(expected_balances)[-10:].sum() / booked),
        }
    return analysis


def _with_deltas(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "delta_objective_nzd": row["total_objective"] - baseline["total_objective"],
        "delta_loan_rate_bps": _rate_delta(baseline["loan_rate"], row["loan_rate"]),
        "delta_deposit_rate_bps": _rate_delta(baseline["deposit_rate"], row["deposit_rate"]),
    }


def build_report(config_path: Path = BASE_CONFIG) -> dict[str, Any]:
    """Return the baseline and matched synthetic sensitivity cases without writing artifacts."""
    base_case = Case("baseline", source="Canonical synthetic config and engine code")
    config = load_config(config_path)
    if config.pricing_strategy is None:
        raise ValueError("The calibration baseline requires market-relative policy bands.")
    base = case_config(config, base_case)
    service = PricingService()
    decision = service.run_scenario(PricingScenarioRequest(config=base, config_path=config_path))
    canonical = decision.diagnostics
    baseline = _result_row(
        base, base_case, canonical, list(decision.governance.gates),
        decision.decision_record.config_hash, {},
    )
    baseline = _with_deltas(baseline, baseline)
    in_band = service.run_scenario(PricingScenarioRequest(config=base, max_boundary_expansions=0))
    deposit = _deposit_analysis(base, canonical)
    ecl = _ecl_analysis(base, canonical)
    deposit["opportunity_as_fraction_of_loan_opportunity"] = (
        deposit["opportunity_volume"] / ecl["loan_opportunity_volume"]
    )
    cases, groups, ftp_curve = _case_definitions(base, (baseline["loan_rate"], baseline["deposit_rate"]))
    rows = {"baseline": baseline}
    for case in cases:
        config = case_config(base, case)
        if _uses_experimental_grid(case):
            diagnostics, gates = _experimental_diagnostics(config, case, canonical)
            config_hash = effective_config_hash(config)
        else:
            run = service.run_scenario(PricingScenarioRequest(config=config, scenario_name=case.scenario_id))
            diagnostics, gates = run.diagnostics, list(run.governance.gates)
            config_hash = run.decision_record.config_hash
        row = _result_row(config, case, diagnostics, gates, config_hash, _perturbations(base, case, baseline))
        rows[case.scenario_id] = _with_deltas(row, baseline)

    interactions = []
    for name, cell_ids in groups.items():
        control, factor_a, factor_b, both = (rows[cell]["total_objective"] for cell in cell_ids)
        interactions.append({
            "interaction_id": name,
            "cell_ids": list(cell_ids),
            "objective_interaction_nzd": both - factor_a - factor_b + control,
        })
    return {
        "schema_version": 1,
        "classification": CLASSIFICATION,
        "production_economic_calibration_applied": False,
        "calibration_approved": False,
        "config_hash_basis": CONFIG_HASH_BASIS,
        "source_config_file_sha256": decision.decision_record.source_config_file_sha256,
        "random_seed": base.random_seed,
        "response_source": "deterministic_synthetic",
        "tier1_capital_ratio": TIER1_CAPITAL_RATIO,
        "policy_spread_bps": base.pricing_strategy.model_dump(),
        "baseline": baseline,
        "in_band_best": {
            "loan_rate": in_band.best_loan_rate,
            "deposit_rate": in_band.best_deposit_rate,
            "total_objective": in_band.objective_value,
        },
        "ecl": ecl,
        "deposit_supply": deposit,
        "illustrative_ftp_curve": ftp_curve,
        "scenarios": [rows[case.scenario_id] for case in cases],
        "interactions": interactions,
    }


def write_report(config_path: Path, output_path: Path, report: dict[str, Any]) -> None:
    """Write deterministic JSON without replacing the source YAML or accepting non-finite data."""
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
